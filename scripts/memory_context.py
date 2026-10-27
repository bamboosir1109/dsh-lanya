# -*- coding: utf-8 -*-
"""Read-only memento adapter; management views must never become model context.

Visibility follows dsh-memento lib/snapshot.mjs::visibleEntries. The installed
protocol query's management path does not filter workspace cwd, so filtering
must happen before ranking/limit. This module creates no store or write path.
"""
import hashlib
import json
import ntpath
import os
from pathlib import Path
import sqlite3
import sys


DB = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-memento", "memory.db")
OWNER_FIELDS = ("track", "scope", "workspace_key", "agent_key")


def workspace_key_of(cwd, platform=None):
    if not isinstance(cwd, str) or not cwd:
        return ""
    windows = (platform or sys.platform) == "win32"
    return ntpath.abspath(cwd).lower() if windows else os.path.abspath(cwd)


def agent_key_of(preset, platform=None):
    if not isinstance(preset, str) or not preset:
        return ""
    value = preset.strip()
    return value.lower() if (platform or sys.platform) == "win32" else value


def session_owner(cwd, agent_preset):
    # Explicit '' is a real shared key, not a wildcard or a missing selector.
    if not isinstance(cwd, str) or not isinstance(agent_preset, str):
        raise ValueError("Model context requires explicit session cwd and agent_preset strings")
    return dict(workspace_key=workspace_key_of(cwd), agent_key=agent_key_of(agent_preset))


def maintenance_selection(value):
    if not isinstance(value, dict) or any(key not in value for key in OWNER_FIELDS):
        raise ValueError("Select track, scope, workspace_key and agent_key explicitly")
    selected = {key: value[key] for key in OWNER_FIELDS}
    if any(not isinstance(item, str) for item in selected.values()):
        raise ValueError("Memory owner values must be strings; empty shared keys are allowed")
    if selected["track"] not in ("user", "agent") or selected["scope"] not in ("workspace", "user-global"):
        raise ValueError("Invalid memory track or scope")
    return selected


def connect_readonly(db_path=None):
    connection = sqlite3.connect(Path(db_path or DB).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def visible(row, owner):
    return (row.get("track") in ("user", "agent") and
            isinstance(row.get("workspace_key"), str) and
            isinstance(row.get("agent_key"), str) and
            row["agent_key"] in ("", owner["agent_key"]) and
            (row.get("scope") == "user-global" or
             (row.get("scope") == "workspace" and row.get("workspace_key") == owner["workspace_key"])))


def load_visible_entries(cwd, agent_preset, db_path=None):
    owner = session_owner(cwd, agent_preset)
    if not os.path.isfile(db_path or DB):
        return []
    connection = connect_readonly(db_path)
    try:
        # Match visibleEntries, including genuine shared agent keys. Empty
        # workspace keys are visible only to sessions whose cwd key is empty.
        return _visible_rows(connection, owner)
    finally:
        connection.close()


def _visible_rows(connection, owner):
    return [dict(row) for row in connection.execute(
        "SELECT * FROM entries WHERE track IN ('user','agent') "
        "AND typeof(workspace_key) = 'text' AND typeof(agent_key) = 'text' "
        "AND (agent_key = '' OR agent_key = ?) "
        "AND (scope = 'user-global' OR (scope = 'workspace' AND workspace_key = ?))",
        (owner["agent_key"], owner["workspace_key"]))]


def list_owners(db_path=None):
    if not os.path.isfile(db_path or DB):
        return []
    connection = connect_readonly(db_path)
    try:
        return [dict(row) for row in connection.execute(
            "SELECT track, scope, workspace_key, agent_key, COUNT(*) AS count FROM entries "
            "GROUP BY track, scope, workspace_key, agent_key "
            "ORDER BY track, scope, workspace_key, agent_key")]
    finally:
        connection.close()


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _record(row):
    try:
        tags = json.loads(row.get("tags") or "[]")
        tags = [tag for tag in tags if isinstance(tag, str)] if isinstance(tags, list) else []
    except (TypeError, ValueError):
        tags = []
    fields = ("id", "track", "scope", "workspace_key", "agent_key", "text", "source",
              "version", "created_at", "updated_at", "session_id")
    record = {key: row.get(key) for key in fields}
    record.update(tags=tags, kind="derived" if row.get("source") in ("merged", "memory_plastic:derived")
                  or "derived" in tags else "raw")
    return record


def _lineage(connection, memories, owner, limit):
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('plastic_batches','plastic_archive')")}
    if len(tables) != 2:
        return []
    originals = []
    seen = set()
    for memory in memories:
        if memory["kind"] != "derived":
            continue
        batches = connection.execute(
            "SELECT DISTINCT b.batch_id, b.archive_sha256 FROM plastic_batches b "
            "JOIN plastic_archive a ON a.batch_id = b.batch_id "
            "WHERE b.state = 'applied' AND a.role = 'derived' AND a.entry_id = ?",
            (memory["id"],)).fetchall()
        memory["lineage"] = []
        for batch in batches:
            records = connection.execute("SELECT * FROM plastic_archive WHERE batch_id = ? ORDER BY entry_id",
                                         (batch["batch_id"],)).fetchall()
            sealed = json.dumps([dict(row) for row in records], ensure_ascii=True,
                                sort_keys=True, separators=(",", ":"))
            if _digest(sealed) != batch["archive_sha256"]:
                raise ValueError("Memory maintenance archive checksum mismatch")
            for archived in records:
                if archived["role"] != "removed" or archived["replacement_id"] != memory["id"]:
                    continue
                if _digest(archived["row_json"]) != archived["row_sha256"]:
                    raise ValueError("Memory original checksum mismatch")
                row = json.loads(archived["row_json"])
                if (not visible(row, owner) or any(row.get(key) != memory.get(key) for key in OWNER_FIELDS)
                        or row.get("id") != archived["entry_id"]):
                    raise ValueError("Memory lineage ownership mismatch")
                ref = dict(batch_id=batch["batch_id"], entry_id=row["id"], version=row.get("version"),
                           sha256=archived["row_sha256"])
                memory["lineage"].append(ref)
                identity = (ref["batch_id"], ref["entry_id"])
                if identity not in seen and len(originals) < limit:
                    originals.append(dict(_record(row), batch_id=batch["batch_id"], sha256=ref["sha256"]))
                    seen.add(identity)
    return originals


def build_context(cwd, agent_preset, query="", recent_events=(), *, conversation_key=None,
                  db_path=None, limit=8, recent_limit=12, original_limit=8):
    """Build JSON data for the existing LLM; do not generate or write memories.

    recent_events come from a trusted caller's current conversation queue, not
    a bulk chat-log scan. Require its stable conversation key and exact owner
    metadata; shared long-term agent visibility does not share private events.
    """
    owner = session_owner(cwd, agent_preset)
    if not isinstance(query, str) or not isinstance(recent_events, (list, tuple)):
        raise ValueError("query must be text and recent_events must be a list")
    if conversation_key is not None and not isinstance(conversation_key, str):
        raise ValueError("conversation_key must be a string")
    if any(type(value) is not int or value < 0 or value > 50
           for value in (limit, recent_limit, original_limit)):
        raise ValueError("Context limits must be integers between 0 and 50")
    if recent_events and (not isinstance(conversation_key, str) or not conversation_key):
        raise ValueError("Recent events require the current conversation_key")
    recent = []
    for event in recent_events:
        if not isinstance(event, dict) or any(event.get(key) != owner[key] for key in owner):
            continue
        if event.get("conversation_key") != conversation_key:
            continue
        if any(not isinstance(event.get(key), str) for key in ("id", "source", "text")):
            continue
        if event.get("role") not in ("user", "assistant", "tool", "observation"):
            continue
        fields = ("id", "source", "text", "role", "created_at", "conversation_key", "workspace_key", "agent_key")
        recent.append({key: event.get(key) for key in fields})
    recent = recent[-max(0, int(recent_limit)):] if recent_limit > 0 else []
    from memory_index import Index
    connection = connect_readonly(db_path) if os.path.isfile(db_path or DB) else None
    try:
        if connection is not None:
            connection.execute("BEGIN")
        rows = _visible_rows(connection, owner) if connection is not None else []
        index = Index.for_model(cwd, agent_preset, entries=rows)
        if query:
            ids = [hit["id"] for hit in index.search(query, limit=max(0, int(limit)))]
            by_id = {row["id"]: row for row in index.entries}
            selected = [by_id[eid] for eid in ids]
        else:
            selected = sorted(index.entries, key=lambda row: (row.get("created_at") or 0, row["id"]),
                              reverse=True)[:max(0, int(limit))]
        memories = [_record(row) for row in selected]
        originals = (_lineage(connection, memories, owner, max(0, int(original_limit)))
                     if connection is not None and memories else [])
    finally:
        if connection is not None:
            connection.close()
    return dict(schema="dsh-memento-context/v1", owner=owner, conversation_key=conversation_key,
                recent_events=recent, memories=memories, originals=originals, read_only=True)


def main(argv=None, stdin=None, stdout=None, stderr=None):
    """Local adapter contract: UTF-8 stdin JSON; no shell or payload DB override."""
    argv = sys.argv[1:] if argv is None else argv
    stdin = getattr(sys.stdin, "buffer", sys.stdin) if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        if argv != ["--stdin-json"]:
            raise ValueError("Expected --stdin-json")
        raw = stdin.read(262145)
        if isinstance(raw, bytes):
            if len(raw) > 262144:
                raise ValueError("Input is too large")
            raw = raw.decode("utf-8")
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 262144:
            raise ValueError("Invalid UTF-8 input")
        payload = json.loads(raw)
        allowed = {"cwd", "agent_preset", "query", "recent_events", "conversation_key", "limit"}
        if (not isinstance(payload, dict) or set(payload) - allowed or
                "cwd" not in payload or "agent_preset" not in payload):
            raise ValueError("Invalid context request")
        packet = build_context(**payload)
        stdout.write(json.dumps(packet, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n")
        return 0
    except Exception as error:
        # Never echo user text, JSON, config paths or SQLite error messages.
        stderr.write(type(error).__name__ + "\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
