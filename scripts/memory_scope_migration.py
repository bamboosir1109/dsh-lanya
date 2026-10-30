"""Explicit, reversible memento owner maintenance. No model or implicit owner.

Preview is read-only. Full private snapshots stay in the database audit, never
in CLI output. This module is deliberately not exposed as a model API.
"""
import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time
import uuid


FIELDS = ("track", "scope", "workspace_key", "agent_key")
FORMAT = "dsh-memento-scope-migration/v1"
MAX_VERSION = 2**63 - 1
TABLES = {
    "scope_migration_batches": ("migration_id", "format", "created_at", "db_key", "mappings_json", "schema_json",
                                 "fingerprint", "row_count", "batch_hash", "state"),
    "scope_migration_rows": ("migration_id", "ordinal", "entry_id", "mapping_index", "before_json", "before_sha256",
                              "after_json", "after_sha256"),
    "scope_migration_audit": ("audit_id", "migration_id", "operation", "created_at", "fingerprint", "details_json", "audit_hash"),
}


class MigrationError(ValueError):
    pass


class MigrationConflict(MigrationError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def snapshot(row):
    value = dict(row)
    for key, item in value.items():
        if isinstance(item, bytes):
            value[key] = {"sqlite_blob_base64": base64.b64encode(item).decode("ascii")}
        elif isinstance(item, float) and not math.isfinite(item):
            raise MigrationError("Non-finite SQLite value")
    return value


def owner(value):
    if (not isinstance(value, dict) or set(value) != set(FIELDS) or
            any(not isinstance(value[key], str) for key in FIELDS) or
            value["track"] not in ("user", "agent") or value["scope"] != "workspace"):
        raise MigrationError("Explicit workspace ownership is required")
    return {key: value[key] for key in FIELDS}


def normalize_mappings(mappings):
    if not isinstance(mappings, list) or not mappings:
        raise MigrationError("A nonempty explicit mappings list is required")
    result, sources, destinations = [], set(), set()
    for mapping in mappings:
        if not isinstance(mapping, dict) or set(mapping) != {"source", "destination", "expected_count"}:
            raise MigrationError("Invalid mapping fields")
        source, destination = owner(mapping["source"]), owner(mapping["destination"])
        if not destination["workspace_key"].strip():
            raise MigrationError("Destination workspace must be an explicit nonempty normalized key")
        count = mapping["expected_count"]
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= MAX_VERSION:
            raise MigrationError("Invalid expected count")
        if any(source[key] != destination[key] for key in ("track", "scope")) or source == destination:
            raise MigrationError("Migration must preserve track and scope and change ownership")
        source_key, destination_key = tuple(source.values()), tuple(destination.values())
        if source_key in sources:
            raise MigrationError("Repeated source ownership")
        sources.add(source_key); destinations.add(destination_key)
        result.append(dict(source=source, destination=destination, expected_count=count))
    if sources & destinations:
        raise MigrationError("Source/destination chains and cycles are not allowed")
    return sorted(result, key=canonical)


def db_key(path):
    return os.path.normcase(str(Path(path).absolute()))


def connect(path, *, writable=False):
    absolute = Path(os.path.abspath(path))
    for part in [*reversed(absolute.parents), absolute]:
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise MigrationError("Linked database paths are not supported")
    if not absolute.is_file():
        raise MigrationError("An existing database file is required")
    connection = sqlite3.connect(absolute.as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    if not writable:
        connection.execute("PRAGMA query_only=ON")
    return connection


def schema(connection):
    columns = [dict(row) for row in connection.execute("PRAGMA table_info(entries)")]
    names = {column["name"] for column in columns}
    if not {"id", "version", *FIELDS}.issubset(names) or [c["name"] for c in columns if c["pk"]] != ["id"]:
        raise MigrationError("Unsupported memento entries schema")
    objects = [dict(row) for row in connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE tbl_name='entries' ORDER BY type,name")]
    return dict(columns=columns, objects=objects)


def changed(row, destination):
    if (not isinstance(row.get("id"), str) or not row["id"] or type(row.get("version")) is not int or
            not 0 <= row["version"] < MAX_VERSION):
        raise MigrationError("Unsupported entry identity or version")
    return dict(row, **destination, version=row["version"] + 1)


def migration_plan(connection, path, mappings):
    mappings = normalize_mappings(mappings)
    rows, seen = [], set()
    for index, mapping in enumerate(mappings):
        selected = connection.execute("SELECT * FROM entries WHERE " + " AND ".join(key + "=?" for key in FIELDS) +
                                      " ORDER BY id COLLATE BINARY", tuple(mapping["source"].values())).fetchall()
        if len(selected) != mapping["expected_count"]:
            raise MigrationConflict("Ownership count changed")
        for record in selected:
            before = snapshot(record)
            if type(before.get("version")) is int and before["version"] >= MAX_VERSION - 1:
                raise MigrationError("Entry has no version capacity for migration and restoration")
            if before["id"] in seen:
                raise MigrationConflict("Overlapping entry identities")
            seen.add(before["id"])
            rows.append(dict(mapping_index=index, before=before, after=changed(before, mapping["destination"])))
    plan = dict(format=FORMAT, operation="migrate", db_key=db_key(path), mappings=mappings, schema=schema(connection), rows=rows)
    return plan, digest(plan)


def summary(plan, fingerprint, *, applied=False, migration_id=None):
    result = dict(operation=plan["operation"], applied=applied, row_count=len(plan["rows"]), fingerprint=fingerprint,
                  mappings=[dict(index=i, expected_count=m["expected_count"], count=sum(r["mapping_index"] == i for r in plan["rows"]))
                            for i, m in enumerate(plan["mappings"])])
    if migration_id is not None:
        result["migration_id"] = migration_id
    return result


def audit_schema(connection, *, create=False):
    existing = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")} & set(TABLES)
    if not existing and create:
        statements = [
            "CREATE TABLE scope_migration_batches(migration_id TEXT PRIMARY KEY,format TEXT NOT NULL,created_at INTEGER NOT NULL,db_key TEXT NOT NULL,mappings_json TEXT NOT NULL,schema_json TEXT NOT NULL,fingerprint TEXT NOT NULL,row_count INTEGER NOT NULL,batch_hash TEXT NOT NULL,state TEXT NOT NULL)",
            "CREATE TABLE scope_migration_rows(migration_id TEXT NOT NULL REFERENCES scope_migration_batches(migration_id),ordinal INTEGER NOT NULL,entry_id TEXT NOT NULL,mapping_index INTEGER NOT NULL,before_json TEXT NOT NULL,before_sha256 TEXT NOT NULL,after_json TEXT NOT NULL,after_sha256 TEXT NOT NULL,PRIMARY KEY(migration_id,ordinal),UNIQUE(migration_id,entry_id))",
            "CREATE TABLE scope_migration_audit(audit_id TEXT PRIMARY KEY,migration_id TEXT NOT NULL REFERENCES scope_migration_batches(migration_id),operation TEXT NOT NULL,created_at INTEGER NOT NULL,fingerprint TEXT NOT NULL,details_json TEXT NOT NULL,audit_hash TEXT NOT NULL)",
        ]
        for statement in statements:
            connection.execute(statement)
        existing = set(TABLES)
    if existing != set(TABLES):
        raise MigrationError("Missing or incomplete scope migration audit")
    for table, names in TABLES.items():
        if tuple(row[1] for row in connection.execute("PRAGMA table_info(" + table + ")")) != names:
            raise MigrationError("Unknown scope migration audit schema")


def sealed_batch(header, rows):
    return digest(dict(header={key: value for key, value in header.items() if key not in ("batch_hash", "state")}, rows=rows))


def record_audit(connection, migration_id, operation, fingerprint, details):
    value = dict(audit_id=uuid.uuid4().hex, migration_id=migration_id, operation=operation,
                 created_at=int(time.time() * 1000), fingerprint=fingerprint, details_json=canonical(details))
    value["audit_hash"] = digest(value)
    connection.execute("INSERT INTO scope_migration_audit VALUES (?,?,?,?,?,?,?)", tuple(value.values()))


def update_entry(connection, before, after):
    # Frozen IDs and full version/owner guards avoid dynamic owner re-selection.
    cursor = connection.execute("UPDATE entries SET workspace_key=?,agent_key=?,version=? WHERE id=? AND version=? AND " +
                                " AND ".join(key + "=?" for key in FIELDS),
                                (after["workspace_key"], after["agent_key"], after["version"], before["id"], before["version"],
                                 *(before[key] for key in FIELDS)))
    if cursor.rowcount != 1:
        raise MigrationConflict("Entry changed during migration")
    actual = connection.execute("SELECT * FROM entries WHERE id=?", (before["id"],)).fetchone()
    if actual is None or snapshot(actual) != after:
        raise MigrationConflict("Unexpected entry changes during migration")


def verify_after_rows(connection, rows):
    # A later row's trigger must not change an earlier row after it was checked.
    for row in rows:
        actual = connection.execute("SELECT * FROM entries WHERE id=?", (row["after"]["id"],)).fetchone()
        if actual is None or snapshot(actual) != row["after"]:
            raise MigrationConflict("The complete migrated batch changed before commit")


def migrate(path, mappings, *, apply=False, expected_fingerprint=None):
    connection = connect(path, writable=apply)
    try:
        connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        plan, fingerprint = migration_plan(connection, path, mappings)
        if not apply:
            return summary(plan, fingerprint)
        if not isinstance(expected_fingerprint, str) or expected_fingerprint != fingerprint:
            raise MigrationConflict("Preview fingerprint changed or is missing")
        if not plan["rows"]:
            raise MigrationError("No entries to migrate")
        audit_schema(connection, create=True)
        migration_id = uuid.uuid4().hex
        records = [dict(migration_id=migration_id, ordinal=i, entry_id=row["before"]["id"], mapping_index=row["mapping_index"],
                        before_json=canonical(row["before"]), before_sha256=digest(row["before"]),
                        after_json=canonical(row["after"]), after_sha256=digest(row["after"])) for i, row in enumerate(plan["rows"])]
        header = dict(migration_id=migration_id, format=FORMAT, created_at=int(time.time() * 1000), db_key=plan["db_key"],
                      mappings_json=canonical(plan["mappings"]), schema_json=canonical(plan["schema"]), fingerprint=fingerprint,
                      row_count=len(records))
        batch_hash = sealed_batch(header, records)
        connection.execute("INSERT INTO scope_migration_batches VALUES (?,?,?,?,?,?,?,?,?,?)", (*header.values(), batch_hash, "applied"))
        for record in records:
            connection.execute("INSERT INTO scope_migration_rows VALUES (?,?,?,?,?,?,?,?)", tuple(record.values()))
        record_audit(connection, migration_id, "migrate", fingerprint, dict(batch_hash=batch_hash, row_count=len(records)))
        for row in plan["rows"]:
            update_entry(connection, row["before"], row["after"])
        verify_after_rows(connection, plan["rows"])
        connection.commit()
        return summary(plan, fingerprint, applied=True, migration_id=migration_id)
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def verified_batch(connection, migration_id):
    audit_schema(connection)
    found = connection.execute("SELECT * FROM scope_migration_batches WHERE migration_id=?", (migration_id,)).fetchone()
    if found is None:
        raise MigrationError("Unknown scope migration batch")
    header = dict(found)
    records = [dict(r) for r in connection.execute("SELECT * FROM scope_migration_rows WHERE migration_id=? ORDER BY ordinal", (migration_id,))]
    if (header["format"] != FORMAT or header["row_count"] != len(records) or not records or
            [r["ordinal"] for r in records] != list(range(len(records))) or
            len({r["entry_id"] for r in records}) != len(records) or sealed_batch(header, records) != header["batch_hash"]):
        raise MigrationConflict("Scope migration batch integrity failed")
    mappings = normalize_mappings(json.loads(header["mappings_json"]))
    original_schema = json.loads(header["schema_json"])
    if schema(connection) != original_schema:
        raise MigrationConflict("Entries schema changed")
    rows = []
    for record in records:
        before, after = json.loads(record["before_json"]), json.loads(record["after_json"])
        index = record["mapping_index"]
        if (type(index) is not int or not 0 <= index < len(mappings) or
                before.get("id") != record["entry_id"] or after.get("id") != record["entry_id"] or
                digest(before) != record["before_sha256"] or digest(after) != record["after_sha256"] or
                {key: before.get(key) for key in FIELDS} != mappings[index]["source"] or
                changed(before, mappings[index]["destination"]) != after):
            raise MigrationConflict("Scope migration row integrity failed")
        rows.append(dict(mapping_index=index, before=before, after=after))
    original = dict(format=FORMAT, operation="migrate", db_key=header["db_key"], mappings=mappings, schema=original_schema, rows=rows)
    if digest(original) != header["fingerprint"]:
        raise MigrationConflict("Scope migration fingerprint integrity failed")
    audits = [dict(r) for r in connection.execute("SELECT * FROM scope_migration_audit WHERE migration_id=?", (migration_id,))]
    if len(audits) != 1 or audits[0]["operation"] != "migrate":
        raise MigrationConflict("Batch was already restored or its audit changed")
    audit = audits[0]
    expected_hash = audit.pop("audit_hash")
    if (digest(audit) != expected_hash or audit["fingerprint"] != header["fingerprint"] or
            json.loads(audit["details_json"]) != dict(batch_hash=header["batch_hash"], row_count=len(records))):
        raise MigrationConflict("Migration audit integrity failed")
    if header["state"] != "applied":
        raise MigrationConflict("Scope migration batch is no longer applied")
    return header, mappings, rows


def restore(path, migration_id, *, apply=False, expected_fingerprint=None):
    if not isinstance(migration_id, str) or not migration_id:
        raise MigrationError("An explicit migration ID is required")
    connection = connect(path, writable=apply)
    try:
        connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        header, mappings, archived = verified_batch(connection, migration_id)
        rows = []
        # Check the entire batch before restoring even its first row. Recall,
        # ownership, version, IDs, all original fields and timestamps matter.
        for row in archived:
            found = connection.execute("SELECT * FROM entries WHERE id=?", (row["after"]["id"],)).fetchone()
            if found is None or snapshot(found) != row["after"]:
                raise MigrationConflict("A migrated entry was deleted, replaced, recalled or modified")
            before = snapshot(found)
            after = dict(row["before"], version=changed(before, {})["version"])
            rows.append(dict(mapping_index=row["mapping_index"], before=before, after=after))
        plan = dict(format=FORMAT, operation="restore", db_key=db_key(path), migration_id=migration_id,
                    batch_hash=header["batch_hash"], mappings=mappings, schema=schema(connection), rows=rows)
        fingerprint = digest(plan)
        if not apply:
            return summary(plan, fingerprint, migration_id=migration_id)
        if not isinstance(expected_fingerprint, str) or expected_fingerprint != fingerprint:
            raise MigrationConflict("Restore preview fingerprint changed or is missing")
        record_audit(connection, migration_id, "restore", fingerprint,
                     dict(batch_hash=header["batch_hash"], rows=rows))
        for row in rows:
            update_entry(connection, row["before"], row["after"])
        verify_after_rows(connection, rows)
        cursor = connection.execute("UPDATE scope_migration_batches SET state='restored' WHERE migration_id=? AND state='applied'",
                                    (migration_id,))
        if cursor.rowcount != 1:
            raise MigrationConflict("Migration batch state changed")
        connection.commit()
        return summary(plan, fingerprint, applied=True, migration_id=migration_id)
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def read_mappings(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise MigrationError("Duplicate JSON fields")
            result[key] = value
        return result
    raw = Path(path).read_bytes()
    if len(raw) > 1024 * 1024:
        raise MigrationError("Mappings request is too large")
    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs)
    if not isinstance(value, dict) or set(value) != {"mappings"}:
        raise MigrationError("Request must contain only the explicit mappings list")
    return value["mappings"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--mappings")
    operation.add_argument("--restore", metavar="MIGRATION_ID")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint")
    args = parser.parse_args(argv)
    try:
        if args.restore is not None:
            result = restore(args.db, args.restore, apply=args.apply, expected_fingerprint=args.fingerprint)
        else:
            result = migrate(args.db, read_mappings(args.mappings), apply=args.apply, expected_fingerprint=args.fingerprint)
        print(canonical(result))
        return 0
    except (MigrationError, sqlite3.Error, OSError, ValueError, TypeError, KeyError, IndexError) as error:
        print(type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
