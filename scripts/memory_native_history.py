"""Explicit native memento history inspection and whole-batch restoration.

No default database, model API, implicit owner, force mode or private text output.
Restoration previews are read-only; apply revalidates inside BEGIN IMMEDIATE.
"""
import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

FORMAT = "dsh-memento-native-history/v1"
MAX_INTEGER = 2**53 - 1
FIELDS = ("id", "track", "scope", "workspace_key", "text", "source", "created_at", "updated_at",
          "session_id", "agent_key", "last_recalled", "recall_count", "tags", "version")
OPERATIONS = {"add", "seed", "replace", "remove", "consolidate", "native.restore"}
TABLES = ("native_history_batches", "native_history_rows", "native_history_audit")
DDL = """
CREATE TABLE IF NOT EXISTS native_history_batches (
seq INTEGER PRIMARY KEY, batch_id TEXT NOT NULL UNIQUE, format TEXT NOT NULL,
operation TEXT NOT NULL, created_at INTEGER NOT NULL, session_id TEXT,
schema_json TEXT NOT NULL, row_count INTEGER NOT NULL CHECK(row_count>0),
restores_batch_id TEXT UNIQUE, batch_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS native_history_rows (
batch_id TEXT NOT NULL, ordinal INTEGER NOT NULL, entry_id TEXT NOT NULL,
before_json TEXT NOT NULL, after_json TEXT NOT NULL, before_sha256 TEXT NOT NULL,
after_sha256 TEXT NOT NULL, result_version INTEGER NOT NULL,
PRIMARY KEY(batch_id,ordinal), UNIQUE(batch_id,entry_id));
CREATE INDEX IF NOT EXISTS native_history_entry ON native_history_rows(entry_id);
CREATE TABLE IF NOT EXISTS native_history_audit (
audit_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL UNIQUE, operation TEXT NOT NULL,
created_at INTEGER NOT NULL, batch_hash TEXT NOT NULL, audit_hash TEXT NOT NULL);
"""


class HistoryError(ValueError):
    pass


class HistoryConflict(HistoryError):
    pass


def safe_integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise HistoryError("Unsafe history integer")
    return value


def text(value, *, nullable=False, nonempty=False):
    if value is None and nullable:
        return value
    if not isinstance(value, str) or nonempty and not value:
        raise HistoryError("Invalid history string")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise HistoryError("Invalid UTF-8 history string") from exc
    return value


def canonical(value):
    if value is None or type(value) is bool:
        return json.dumps(value)
    if type(value) is int:
        if not -MAX_INTEGER <= value <= MAX_INTEGER:
            raise HistoryError("Unsafe JSON integer")
        return str(value)
    if isinstance(value, str):
        return json.dumps(text(value), ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(canonical(item) for item in value) + "]"
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        keys = sorted(value, key=lambda key: text(key).encode("utf-16-be"))
        return "{" + ",".join(canonical(key) + ":" + canonical(value[key]) for key in keys) + "}"
    raise HistoryError("Non-lossless JSON value")


def hash_value(value):
    raw = value if isinstance(value, str) else canonical(value)
    return hashlib.sha256(raw.encode("utf-8", "strict")).hexdigest()


def parse_json(raw, *, canonical_required=True):
    text(raw)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise HistoryError("Duplicate JSON key")
            result[key] = value
        return result
    def invalid_constant(_):
        raise HistoryError("Non-finite JSON value")
    try:
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (ValueError, UnicodeError) as exc:
        raise HistoryError("Invalid history JSON") from exc
    normalized = canonical(value)
    if canonical_required and normalized != raw:
        raise HistoryError("Non-canonical history JSON")
    return value


def valid_row(row):
    if row is None:
        return None
    if not isinstance(row, dict) or set(row) != set(FIELDS):
        raise HistoryError("Unknown full entries row schema")
    text(row["id"], nonempty=True)
    if row["track"] not in ("user", "agent") or row["scope"] not in ("workspace", "user-global"):
        raise HistoryError("Invalid native ownership")
    for name in ("track", "scope", "workspace_key", "agent_key", "text", "source", "tags"):
        text(row[name])
    text(row["session_id"], nullable=True)
    safe_integer(row["version"], 1)
    for name in ("created_at", "updated_at", "recall_count"):
        safe_integer(row[name])
    if row["last_recalled"] is not None:
        safe_integer(row["last_recalled"])
    # Native JSON tags may legally contain an escaped lone UTF-16 surrogate.
    # The SQL tags string itself is strict UTF-8 and remains byte-for-byte raw;
    # do not recanonicalize decoded tags into new text or rewrite their escapes.
    try:
        tags = json.loads(row["tags"])
    except (ValueError, UnicodeError) as exc:
        raise HistoryError("Invalid native tags JSON") from exc
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise HistoryError("Invalid native tags")
    canonical(row)
    return row


def entry_schema(connection):
    columns = [dict(row) for row in connection.execute("PRAGMA table_info(entries)")]
    if {row["name"] for row in columns} != set(FIELDS) or [row["name"] for row in columns if row["pk"]] != ["id"]:
        raise HistoryError("Unsupported entries schema")
    sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='entries'").fetchone()
    if sql is None:
        raise HistoryError("Missing entries table")
    value = {"columns": columns, "sql": sql[0]}
    canonical(value)
    return value


def normalized_sql(sql):
    return re.sub(r"\s+", " ", text(sql).strip())


def history_schema(connection, *, allow_missing=False):
    def catalog(db):
        return {(r["type"], r["name"], r["tbl_name"]): None if r["sql"] is None else normalized_sql(r["sql"])
                for r in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name GLOB 'native_history_*' OR tbl_name IN (?,?,?)", TABLES)}
    actual = catalog(connection)
    if not actual and allow_missing:
        return False
    expected = sqlite3.connect(":memory:")
    expected.row_factory = sqlite3.Row
    try:
        expected.executescript(DDL)
        for table in TABLES:
            for action in ("UPDATE", "DELETE"):
                expected.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()}_refused BEFORE {action} ON {table} "
                                 "BEGIN SELECT RAISE(ABORT,'Native history is append-only'); END")
        conflicts = {
            "native_history_batches": "seq=NEW.seq OR batch_id=NEW.batch_id OR (NEW.restores_batch_id IS NOT NULL AND restores_batch_id=NEW.restores_batch_id)",
            "native_history_rows": "batch_id=NEW.batch_id AND (ordinal=NEW.ordinal OR entry_id=NEW.entry_id)",
            "native_history_audit": "audit_id=NEW.audit_id OR batch_id=NEW.batch_id",
        }
        for table, conflict in conflicts.items():
            expected.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_insert_conflict_refused BEFORE INSERT ON {table} "
                             f"WHEN EXISTS(SELECT 1 FROM {table} WHERE {conflict}) "
                             "BEGIN SELECT RAISE(ABORT,'Native history existing identity cannot be replaced'); END")
        if actual != catalog(expected):
            raise HistoryError("Native append-only table or trigger schema changed")
        for table in TABLES:
            if [dict(row) for row in connection.execute(f"PRAGMA table_info({table})")] != [dict(row) for row in expected.execute(f"PRAGMA table_info({table})")]:
                raise HistoryError("Native history column metadata changed")
    finally:
        expected.close()
    return True


def validate_change(row):
    for name in ("batch_id", "entry_id"):
        text(row[name], nonempty=True)
    safe_integer(row["ordinal"])
    safe_integer(row["result_version"], 1)
    before, after = parse_json(row["before_json"]), parse_json(row["after_json"])
    valid_row(before); valid_row(after)
    if before is None and after is None or before == after:
        raise HistoryError("Empty native change")
    if any(value is not None and value["id"] != row["entry_id"] for value in (before, after)):
        raise HistoryError("Native row identity mismatch")
    if before is not None and after is not None and after["version"] <= before["version"]:
        raise HistoryError("Native version did not advance")
    expected_version = after["version"] if after is not None else safe_integer(before["version"] + 1, 1)
    if row["result_version"] != expected_version:
        raise HistoryError("Native tombstone or version mismatch")
    for name in ("before", "after"):
        if row[name + "_sha256"] != hash_value(row[name + "_json"]):
            raise HistoryError("Native row seal mismatch")
    return before, after


def catalog(connection, *, allow_missing=False):
    schema = entry_schema(connection)
    if not history_schema(connection, allow_missing=allow_missing):
        return []
    batches = [dict(row) for row in connection.execute("SELECT * FROM native_history_batches ORDER BY seq")]
    grouped, audits = defaultdict(list), {}
    for row in connection.execute("SELECT * FROM native_history_rows ORDER BY batch_id,ordinal"):
        grouped[row["batch_id"]].append(dict(row))
    for row in connection.execute("SELECT * FROM native_history_audit"):
        if row["batch_id"] in audits:
            raise HistoryError("Duplicate native audit")
        audits[row["batch_id"]] = dict(row)
    if set(grouped) != {batch["batch_id"] for batch in batches} or set(audits) != set(grouped):
        raise HistoryError("Dangling or missing native history records")
    seen, restored, result, versions = {}, set(), [], {}
    for batch in batches:
        safe_integer(batch["seq"], 1)
        if batch["seq"] != len(result) + 1:
            raise HistoryError("Native history sequence gap")
        for name in ("batch_id", "format", "operation", "schema_json", "batch_hash"):
            text(batch[name], nonempty=True)
        text(batch["session_id"], nullable=True)
        text(batch["restores_batch_id"], nullable=True, nonempty=True)
        safe_integer(batch["created_at"])
        safe_integer(batch["row_count"], 1)
        if batch["format"] != FORMAT or batch["operation"] not in OPERATIONS or canonical(parse_json(batch["schema_json"])) != canonical(schema):
            raise HistoryError("Native batch format or entries schema mismatch")
        rows = grouped[batch["batch_id"]]
        if len(rows) != batch["row_count"] or [r["ordinal"] for r in rows] != list(range(len(rows))):
            raise HistoryError("Incomplete native batch")
        ids = [r["entry_id"] for r in rows]
        if len(set(ids)) != len(ids) or ids != sorted(ids, key=lambda item: text(item).encode("utf-16-be")):
            raise HistoryError("Unordered or repeated native entries")
        changes = [validate_change(row) for row in rows]
        for row, (before, _) in zip(rows, changes):
            previous = versions.get(row["entry_id"])
            if previous is not None and (row["result_version"] <= previous or before is None and batch["operation"] != "native.restore"):
                raise HistoryError("Reused native entry ID or historical version regression")
        header = {key: value for key, value in batch.items() if key != "batch_hash"}
        if batch["batch_hash"] != hash_value({"header": header, "rows": rows}):
            raise HistoryError("Native batch seal mismatch")
        audit = audits[batch["batch_id"]]
        text(audit["audit_id"], nonempty=True)
        audit_body = {key: value for key, value in audit.items() if key != "audit_hash"}
        if (audit["operation"] != batch["operation"] or audit["created_at"] != batch["created_at"] or audit["batch_hash"] != batch["batch_hash"] or
                audit["audit_hash"] != hash_value(audit_body)):
            raise HistoryError("Native audit seal mismatch")
        parent = batch["restores_batch_id"]
        if parent is not None:
            if batch["operation"] != "native.restore" or parent not in seen or parent in restored:
                raise HistoryError("Invalid native restore lineage")
            original = seen[parent]
            ids = {row["entry_id"] for row in original["rows"]}
            if any(other["header"]["seq"] > original["header"]["seq"] and ids.intersection(row["entry_id"] for row in other["rows"]) for other in seen.values()):
                raise HistoryError("Inverse batch overwrote later same-ID history")
            expected_changes = inverse_changes(original, versions)
            if changes != expected_changes or batch["session_id"] is not None:
                raise HistoryError("Native inverse batch mismatch")
            restored.add(parent)
        elif batch["operation"] == "native.restore":
            raise HistoryError("Native inverse batch has no parent")
        item = {"header": batch, "rows": rows, "changes": changes}
        result.append(item)
        seen[batch["batch_id"]] = item
        versions.update({row["entry_id"]: row["result_version"] for row in rows})
    return result


def inverse_changes(batch, highest_versions=None):
    changes = []
    for row, (before, after) in zip(batch["rows"], batch["changes"]):
        version = safe_integer(max(row["result_version"], before["version"] if before else 0,
                                   after["version"] if after else 0, (highest_versions or {}).get(row["entry_id"], 0)) + 1, 1)
        restored = dict(before, version=version) if before is not None else None
        changes.append((after, restored))
    return changes


def database_path(value):
    path = Path(os.path.abspath(value))
    for part in [*reversed(path.parents), path]:
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise HistoryError("Linked database paths are refused")
    if not path.is_file():
        raise HistoryError("Existing database required")
    return path


def connect(path, writable=False):
    path = database_path(path)
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    if not writable:
        connection.execute("PRAGMA query_only=ON")
    return connection


def restore_plan(connection, path, batch_id):
    text(batch_id, nonempty=True)
    batches = catalog(connection)
    selected = next((b for b in batches if b["header"]["batch_id"] == batch_id), None)
    if selected is None:
        raise HistoryConflict("Unknown native history batch")
    ids = {row["entry_id"] for row in selected["rows"]}
    for batch in batches:
        if batch["header"]["restores_batch_id"] == batch_id or batch["header"]["seq"] > selected["header"]["seq"] and ids.intersection(row["entry_id"] for row in batch["rows"]):
            raise HistoryConflict("Later same-ID history or prior restoration")
    highest_versions = {}
    for batch in batches:
        for row in batch["rows"]:
            highest_versions[row["entry_id"]] = max(highest_versions.get(row["entry_id"], 0), row["result_version"])
    changes = inverse_changes(selected, highest_versions)
    for row, (expected, _) in zip(selected["rows"], changes):
        found = connection.execute("SELECT * FROM entries WHERE id=?", (row["entry_id"],)).fetchone()
        current = valid_row(dict(found)) if found else None
        if canonical(current) != canonical(expected):
            raise HistoryConflict("Current full row changed or deleted ID was reused")
    identity = database_path(path).stat()
    plan = {"format": FORMAT, "db_key": os.path.normcase(str(database_path(path))),
            "db_identity": [str(identity.st_dev), str(identity.st_ino)], "batch_hash": selected["header"]["batch_hash"],
            "batch_id": batch_id, "schema": entry_schema(connection),
            "changes": [{"before": before, "after": after} for before, after in changes]}
    return plan, hash_value(plan), selected


def append_inverse(connection, selected, changes):
    batch_id = str(uuid.uuid4())
    rows = []
    for ordinal, ((before, after), original) in enumerate(zip(changes, selected["rows"])):
        row = {"batch_id": batch_id, "ordinal": ordinal, "entry_id": original["entry_id"],
               "before_json": canonical(before), "after_json": canonical(after),
               "before_sha256": hash_value(before), "after_sha256": hash_value(after),
               "result_version": after["version"] if after is not None else safe_integer(before["version"] + 1, 1)}
        validate_change(row)
        rows.append(row)
    seq = safe_integer(connection.execute("SELECT COALESCE(MAX(seq),0)+1 FROM native_history_batches").fetchone()[0], 1)
    header = {"seq": seq, "batch_id": batch_id, "format": FORMAT, "operation": "native.restore",
              "created_at": safe_integer(int(time.time() * 1000)), "session_id": None,
              "schema_json": canonical(entry_schema(connection)), "row_count": len(rows),
              "restores_batch_id": selected["header"]["batch_id"]}
    batch_hash = hash_value({"header": header, "rows": rows})
    connection.execute("INSERT INTO native_history_batches VALUES (?,?,?,?,?,?,?,?,?,?)", (*header.values(), batch_hash))
    connection.executemany("INSERT INTO native_history_rows VALUES (?,?,?,?,?,?,?,?)", [tuple(row.values()) for row in rows])
    audit = {"audit_id": str(uuid.uuid4()), "batch_id": batch_id, "operation": "native.restore", "created_at": header["created_at"], "batch_hash": batch_hash}
    connection.execute("INSERT INTO native_history_audit VALUES (?,?,?,?,?,?)", (*audit.values(), hash_value(audit)))
    return batch_id


def write_entry(connection, before, after):
    columns = [row["name"] for row in connection.execute("PRAGMA table_info(entries)")]
    if before is None:
        connection.execute("INSERT INTO entries (" + ",".join('"' + name + '"' for name in columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")", tuple(after[name] for name in columns))
        return
    where = " AND ".join('"' + name + '" IS ?' for name in columns)
    if after is None:
        cursor = connection.execute("DELETE FROM entries WHERE " + where, tuple(before[name] for name in columns))
    else:
        cursor = connection.execute("UPDATE entries SET " + ",".join('"' + name + '"=?' for name in columns) + " WHERE " + where,
                                    (*[after[name] for name in columns], *[before[name] for name in columns]))
    if cursor.rowcount != 1:
        raise HistoryConflict("Restore SQL full-row guard failed")


def restore(path, batch_id, *, apply=False, expected_fingerprint=None, writers_stopped=False, _fault=None):
    if type(apply) is not bool:
        raise HistoryError("Apply must be an explicit boolean")
    if apply and (writers_stopped is not True or not isinstance(expected_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint)):
        raise HistoryConflict("Apply requires stopped-writers assertion and fresh fingerprint")
    connection = connect(path, writable=apply)
    try:
        connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        plan, fingerprint, selected = restore_plan(connection, path, batch_id)
        summary = {"operation": "restore", "applied": False, "batch_id": batch_id,
                   "row_count": len(plan["changes"]), "fingerprint": fingerprint}
        if not apply:
            connection.rollback()
            return summary
        if expected_fingerprint != fingerprint:
            raise HistoryConflict("Fresh restore fingerprint required")
        previous = {row["id"]: valid_row(dict(row)) for row in connection.execute("SELECT * FROM entries")}
        changes = [(row["before"], row["after"]) for row in plan["changes"]]
        inverse_id = append_inverse(connection, selected, changes)
        if _fault:
            _fault("after_history", connection)
        expected = dict(previous)
        for before, after in changes:
            write_entry(connection, before, after)
            entry_id = (after or before)["id"]
            if after is None:
                expected.pop(entry_id, None)
            else:
                expected[entry_id] = after
            if _fault:
                _fault("after_row", connection)
        current = {row["id"]: valid_row(dict(row)) for row in connection.execute("SELECT * FROM entries")}
        if canonical(current) != canonical(expected):
            raise HistoryConflict("Restore affected unexpected facts")
        catalog(connection)  # Verify inverse batch, full seals and lineage before commit.
        if _fault:
            _fault("before_commit", connection)
        connection.commit()
        return {**summary, "applied": True, "inverse_batch_id": inverse_id}
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def list_batches(path):
    connection = connect(path)
    try:
        connection.execute("BEGIN")
        batches = catalog(connection, allow_missing=True)
        restored = {b["header"]["restores_batch_id"] for b in batches}
        return {"operation": "list", "batches": [{**{name: b["header"][name] for name in ("seq", "batch_id", "operation", "created_at", "row_count", "restores_batch_id")},
                                                   "restored": b["header"]["batch_id"] in restored} for b in batches]}
    finally:
        connection.rollback()
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true")
    mode.add_argument("--restore", metavar="BATCH_ID")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writers-stopped", action="store_true")
    parser.add_argument("--fingerprint", "--expected-fingerprint", dest="fingerprint")
    args = parser.parse_args(argv)
    try:
        if args.list and (args.apply or args.fingerprint or args.writers_stopped):
            raise HistoryError("List is read-only")
        result = list_batches(args.db) if args.list else restore(args.db, args.restore, apply=args.apply, expected_fingerprint=args.fingerprint, writers_stopped=args.writers_stopped)
        print(canonical(result))
        return 0
    except Exception as exc:
        # No memory text, IDs other than the explicitly supplied batch ID, or SQL.
        print(canonical({"error": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
