"""Explicit, reversible operator maintenance for the existing memento SQLite.

This is not memento's model write service or an approval-policy replacement.
Every writer supplies a complete owner selection; edit/delete also require the
reviewed version. The CLI previews by default and never creates a missing DB.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

from memory_context import maintenance_selection


DB = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-memento", "memory.db")
OWNER_FIELDS = ("track", "scope", "workspace_key", "agent_key")
ARCHIVE = "memory_maintenance_changes"


class MemoryMutationError(ValueError):
    code = "invalid_request"
    status = 400


class MemoryMutationConflict(MemoryMutationError):
    code = "conflict"
    status = 409


class MemoryMutationNotFound(MemoryMutationError):
    code = "not_found"
    status = 404


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _selection(value):
    try:
        return maintenance_selection(value)
    except ValueError as error:
        raise MemoryMutationError(str(error)) from error


def _identifier(value, name):
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise MemoryMutationError(name + " must be an explicit nonempty identifier")
    return value


def _version(value):
    if type(value) is not int or value < 1:
        raise MemoryMutationError("expected_version must be a positive integer")
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 400:
        raise MemoryMutationError("text must contain 1 to 400 characters")
    return value.strip()


@contextmanager
def _connection(db_path, apply):
    # URI mode=rw prevents both preview and apply from creating an accidental DB.
    path = Path(db_path or DB).resolve()
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if apply else "?mode=ro"),
                                 uri=True, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        if not apply:
            connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        yield connection
        connection.execute("COMMIT" if apply else "ROLLBACK")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _entry(connection, entry_id):
    row = connection.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    return dict(row) if row is not None else None


def _owned(row, selection):
    if row is None:
        raise MemoryMutationNotFound("Memory entry not found")
    if any(row.get(key) != selection[key] for key in OWNER_FIELDS):
        raise MemoryMutationConflict("Memory ownership no longer matches the explicit selection")
    _version(row.get("version"))
    return row


def _ensure_archive(connection):
    # Keep memento's schemaVersion, entries and protocol untouched.
    connection.execute("CREATE TABLE IF NOT EXISTS " + ARCHIVE + " ("
        "change_id TEXT PRIMARY KEY, operation TEXT NOT NULL, entry_id TEXT NOT NULL,"
        "selection_json TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL,"
        "before_sha256 TEXT NOT NULL, after_sha256 TEXT NOT NULL, created_at INTEGER NOT NULL,"
        "result_version INTEGER NOT NULL, restores_change_id TEXT, record_sha256 TEXT NOT NULL,"
        "restored_by TEXT)")


def _record_payload(record):
    return {key: record[key] for key in (
        "change_id", "operation", "entry_id", "selection_json", "before_json", "after_json",
        "before_sha256", "after_sha256", "created_at", "result_version", "restores_change_id")}


def _archive(connection, operation, selection, before, after, result_version, restores=None):
    before_json, after_json = _json(before), _json(after)
    record = dict(change_id="memory-change-" + uuid.uuid4().hex, operation=operation,
        entry_id=(after or before)["id"], selection_json=_json(selection),
        before_json=before_json, after_json=after_json, before_sha256=_sha(before_json),
        after_sha256=_sha(after_json), created_at=int(time.time() * 1000),
        result_version=result_version, restores_change_id=restores)
    record["record_sha256"] = _sha(_json(_record_payload(record)))
    columns = list(record)
    connection.execute("INSERT INTO " + ARCHIVE + " (" + ",".join(columns) + ") VALUES (" +
                       ",".join("?" for _ in columns) + ")", [record[key] for key in columns])
    row = after or before
    connection.execute(
        "INSERT INTO audit (ts,action,track,scope,entry_id,text,outcome,source,session_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (record["created_at"], operation + "-maintenance", row["track"], row["scope"], row["id"],
         row["text"], "change_id=" + record["change_id"], "memory_mutations", None))
    return record


def _insert(connection, row):
    allowed = {column[1] for column in connection.execute("PRAGMA table_info(entries)")}
    if not row or not set(row).issubset(allowed):
        raise MemoryMutationConflict("Archived columns no longer match the entries schema")
    columns = list(row)
    names = ",".join('"' + key.replace('"', '""') + '"' for key in columns)
    connection.execute("INSERT INTO entries (" + names + ") VALUES (" +
                       ",".join("?" for _ in columns) + ")", [row[key] for key in columns])


def mutate(operation, body, *, db_path=None, apply=False):
    """Preview or apply an operator add/edit/delete in one SQLite transaction."""
    if operation not in ("add", "edit", "delete") or not isinstance(body, dict) or type(apply) is not bool:
        raise MemoryMutationError("Expected add/edit/delete and an explicit request object")
    selection = _selection(body.get("selection"))
    expected = _version(body.get("expected_version")) if operation != "add" else None
    entry_id = _identifier(body.get("id"), "id") if operation != "add" else "memory-" + uuid.uuid4().hex
    text = _text(body.get("text")) if operation != "delete" else None
    if operation == "add" and selection["scope"] == "workspace" and not selection["workspace_key"]:
        raise MemoryMutationError("New workspace memories require a nonempty explicit workspace_key")
    with _connection(db_path, apply) as connection:
        before = _entry(connection, entry_id)
        if operation != "add":
            _owned(before, selection)
            if before["version"] != expected:
                raise MemoryMutationConflict("Memory version changed; reload before editing")
        elif before is not None:
            raise MemoryMutationConflict("Generated memory ID is occupied")
        now = int(time.time() * 1000)
        if operation == "add":
            after = dict(id=entry_id, **selection, text=text, source="memory_mutations",
                         created_at=now, updated_at=now, session_id=None, tags="[]", version=1)
            version = 1
        elif operation == "edit":
            after = dict(before, text=text, updated_at=now, version=expected + 1)
            version = expected + 1
        else:
            after, version = None, expected + 1
        change_id = None
        if apply:
            _ensure_archive(connection)
            if operation == "add":
                _insert(connection, after)
            elif operation == "edit":
                cursor = connection.execute(
                    "UPDATE entries SET text=?,updated_at=?,version=? WHERE id=? AND version=? "
                    "AND track=? AND scope=? AND workspace_key=? AND agent_key=?",
                    (text, now, version, entry_id, expected, *(selection[key] for key in OWNER_FIELDS)))
                if cursor.rowcount != 1:
                    raise MemoryMutationConflict("Memory changed during the transaction")
            else:
                cursor = connection.execute(
                    "DELETE FROM entries WHERE id=? AND version=? AND track=? AND scope=? "
                    "AND workspace_key=? AND agent_key=?",
                    (entry_id, expected, *(selection[key] for key in OWNER_FIELDS)))
                if cursor.rowcount != 1:
                    raise MemoryMutationConflict("Memory changed during the transaction")
            # Read complete stored rows, including defaults and counters, before sealing.
            after = _entry(connection, entry_id)
            record = _archive(connection, operation, selection, before, after, version)
            change_id = record["change_id"]
        return dict(ok=True, applied=apply, view="maintenance", operation=operation,
                    selection=selection, id=entry_id, version=version, entry=after, change_id=change_id)


def _read_change(connection, change_id, selection):
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                (ARCHIVE,)).fetchone()
    if not exists:
        raise MemoryMutationNotFound("Maintenance change not found")
    row = connection.execute("SELECT * FROM " + ARCHIVE + " WHERE change_id=?", (change_id,)).fetchone()
    if row is None:
        raise MemoryMutationNotFound("Maintenance change not found")
    record = dict(row)
    try:
        if _sha(_json(_record_payload(record))) != record["record_sha256"]:
            raise ValueError()
        if (_sha(record["before_json"]) != record["before_sha256"] or
                _sha(record["after_json"]) != record["after_sha256"]):
            raise ValueError()
        if json.loads(record["selection_json"]) != selection:
            raise MemoryMutationConflict("Change ownership does not match the explicit selection")
        before, after = json.loads(record["before_json"]), json.loads(record["after_json"])
        for entry in (before, after):
            if entry is not None:
                _owned(entry, selection)
                if entry["id"] != record["entry_id"]:
                    raise ValueError()
        _version(record["result_version"])
        if before is None and after is None:
            raise ValueError()
    except MemoryMutationError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise MemoryMutationConflict("Maintenance archive integrity check failed") from error
    return record, before, after


def restore_change(change_id, selection, *, db_path=None, apply=False, expected_fingerprint=None):
    """Preview an exact inverse; apply requires its unchanged preview fingerprint.

    Full current rows, including recall metadata, must still equal archived after.
    A deletion restores only an unoccupied ID. Restoration always advances the
    version watermark and records a separate complete change and memento audit.
    """
    change_id = _identifier(change_id, "change_id")
    selection = _selection(selection)
    if type(apply) is not bool:
        raise MemoryMutationError("apply must be a boolean")
    with _connection(db_path, apply) as connection:
        record, before, after = _read_change(connection, change_id, selection)
        if record["restored_by"] is not None:
            raise MemoryMutationConflict("This change was already restored")
        current = _entry(connection, record["entry_id"])
        if _json(current) != _json(after):
            raise MemoryMutationConflict("Current memory differs from archived after; refusing overwrite")
        # A later maintenance deletion/reuse must not make an older tombstone restorable.
        newer = connection.execute(
            "SELECT 1 FROM " + ARCHIVE + " WHERE entry_id=? AND rowid > "
            "(SELECT rowid FROM " + ARCHIVE + " WHERE change_id=?) LIMIT 1",
            (record["entry_id"], change_id)).fetchone()
        if newer:
            raise MemoryMutationConflict("A later maintenance change exists for this ID")
        version = record["result_version"] + 1
        restored = None if before is None else dict(before, version=version, updated_at=int(time.time() * 1000))
        fingerprint = _sha(_json(dict(change_id=change_id, record_sha256=record["record_sha256"],
                                     current=current, restored_by=record["restored_by"])))
        restore_id = None
        if apply:
            if expected_fingerprint != fingerprint:
                raise MemoryMutationConflict("Restore preview fingerprint changed or missing")
            if current is not None:
                cursor = connection.execute("DELETE FROM entries WHERE id=? AND version=?",
                                            (current["id"], current["version"]))
                if cursor.rowcount != 1:
                    raise MemoryMutationConflict("Current memory changed during restore")
            if restored is not None:
                _insert(connection, restored)
            restored = _entry(connection, record["entry_id"])
            inverse = _archive(connection, "restore", selection, current, restored, version, change_id)
            restore_id = inverse["change_id"]
            cursor = connection.execute("UPDATE " + ARCHIVE + " SET restored_by=? "
                                        "WHERE change_id=? AND restored_by IS NULL",
                                        (restore_id, change_id))
            if cursor.rowcount != 1:
                raise MemoryMutationConflict("Restore was already applied")
        return dict(ok=True, applied=apply, view="maintenance", operation="restore", selection=selection,
                    id=record["entry_id"], version=version, entry=restored, change_id=restore_id,
                    restores_change_id=change_id, fingerprint=fingerprint)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("add", "edit", "delete", "restore"))
    parser.add_argument("--db", required=True, help="Existing operator-selected memento DB")
    parser.add_argument("--request", required=True, help="UTF-8 JSON with explicit selection and operation fields")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-fingerprint", help="Required for restore apply")
    args = parser.parse_args(argv)
    try:
        with open(args.request, encoding="utf-8-sig") as stream:
            body = json.load(stream)
        if not isinstance(body, dict):
            raise MemoryMutationError("Request must be an object")
        if args.operation == "restore":
            result = restore_change(body.get("change_id"), body.get("selection"), db_path=args.db,
                                    apply=args.apply, expected_fingerprint=args.expected_fingerprint)
        else:
            result = mutate(args.operation, body, db_path=args.db, apply=args.apply)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (MemoryMutationError, OSError, sqlite3.Error, ValueError) as error:
        print(json.dumps(dict(ok=False, error=type(error).__name__,
                              code=getattr(error, "code", "maintenance_failed"))), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
