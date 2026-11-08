"""Version-bound, append-only annotations; never guesses dates or edits facts.

Confidence and importance are author annotations, not calibrated evidence.
Readers use SELECT only. Native/operator restores that advance the fact version
without a matching annotation intentionally return None, leaving history intact.
"""
import datetime
import hashlib
import json
import re
import sqlite3

FORMAT = "dsh-memory-metadata/v1"
TABLE = "memory_metadata_revisions"
OWNER_FIELDS = ("track", "scope", "workspace_key", "agent_key")
KINDS = ("fact", "preference", "event", "rule", "reflection", "uncertain")
STATUSES = ("active", "uncertain", "superseded")
FACT_FIELDS = ("id",) + OWNER_FIELDS + ("text", "source", "tags", "version",
    "created_at", "updated_at", "session_id")
DDL = """CREATE TABLE memory_metadata_revisions (
entry_id TEXT NOT NULL, version INTEGER NOT NULL, track TEXT NOT NULL,
scope TEXT NOT NULL, workspace_key TEXT NOT NULL, agent_key TEXT NOT NULL,
fact_sha256 TEXT NOT NULL, metadata_json TEXT NOT NULL, metadata_sha256 TEXT NOT NULL,
recorded_at INTEGER NOT NULL, PRIMARY KEY(entry_id,version))"""
TRIGGERS = {
    "memory_metadata_update_refused": "CREATE TRIGGER memory_metadata_update_refused BEFORE UPDATE ON memory_metadata_revisions BEGIN SELECT RAISE(ABORT,'Memory metadata is append-only'); END",
    "memory_metadata_delete_refused": "CREATE TRIGGER memory_metadata_delete_refused BEFORE DELETE ON memory_metadata_revisions BEGIN SELECT RAISE(ABORT,'Memory metadata is append-only'); END",
    "memory_metadata_replace_refused": "CREATE TRIGGER memory_metadata_replace_refused BEFORE INSERT ON memory_metadata_revisions WHEN EXISTS(SELECT 1 FROM memory_metadata_revisions WHERE entry_id=NEW.entry_id AND version=NEW.version) BEGIN SELECT RAISE(ABORT,'Memory metadata identity cannot be replaced'); END",
}


class MemoryMetadataError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256((value if isinstance(value, str) else canonical(value)).encode("utf-8")).hexdigest()


def _date(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2}))?", value):
        raise MemoryMetadataError("Event dates require an explicit ISO date or zoned timestamp")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MemoryMetadataError("Invalid event date") from error
    return parsed.replace(tzinfo=datetime.timezone.utc) if len(value) == 10 else parsed


def normalize_metadata(value, previous=None):
    """Validate a partial annotation, inheriting only an exact previous version."""
    defaults = dict(format=FORMAT, title=None, kind="uncertain", occurred_at=None,
        valid_from=None, valid_until=None, importance=None, confidence=None,
        status="active", links=[])
    if previous is not None:
        defaults.update(previous)
    if not isinstance(value, dict) or set(value) - (set(defaults) | {"clear_fields"}):
        raise MemoryMetadataError("Unknown memory metadata fields")
    value = dict(value)
    clear = value.pop("clear_fields", [])
    if (not isinstance(clear, list) or any(not isinstance(key, str) for key in clear) or len(clear) != len(set(clear)) or
            any(key not in ("title", "occurred_at", "valid_from", "valid_until", "importance", "confidence")
                or key in value for key in clear)):
        raise MemoryMetadataError("Invalid annotation fields to clear")
    result = dict(defaults, **value, **{key: None for key in clear})
    if result["format"] != FORMAT:
        raise MemoryMetadataError("Unsupported memory metadata format")
    title = result["title"]
    if title is not None:
        if (not isinstance(title, str) or not title.strip() or len(title) > 80 or
                any(ord(c) < 32 for c in title)):
            raise MemoryMetadataError("Title requires 1 to 80 printable characters")
        result["title"] = title.strip()
    if result["kind"] not in KINDS or result["status"] not in STATUSES:
        raise MemoryMetadataError("Invalid memory metadata classification")
    for key in ("importance", "confidence"):
        if result[key] is not None and (type(result[key]) is not int or not 0 <= result[key] <= 100):
            raise MemoryMetadataError("Annotation weights require integers from 0 to 100 or null")
    for key in ("occurred_at", "valid_from", "valid_until"):
        _date(result[key])
    if (result["valid_from"] and result["valid_until"] and
            _date(result["valid_from"]) > _date(result["valid_until"])):
        raise MemoryMetadataError("Validity interval is reversed")
    links = result["links"]
    if not isinstance(links, list) or len(links) > 32:
        raise MemoryMetadataError("Memory links require at most 32 exact references")
    seen = set()
    for link in links:
        if (not isinstance(link, dict) or set(link) != {"entry_id", "version", "relation"} or
                not isinstance(link["entry_id"], str) or not link["entry_id"] or len(link["entry_id"]) > 256 or
                any(ord(c) < 32 for c in link["entry_id"]) or type(link["version"]) is not int or
                not 1 <= link["version"] <= 9007199254740991 or
                link["relation"] not in ("updates", "corrects", "consolidates")):
            raise MemoryMetadataError("Invalid exact memory relationship")
        key = (link["entry_id"], link["version"], link["relation"])
        if key in seen:
            raise MemoryMetadataError("Duplicate memory relationship")
        seen.add(key)
    if len(canonical(result).encode("utf-8")) > 8192:
        raise MemoryMetadataError("Memory annotation exceeds bounded size")
    return json.loads(canonical(result))


def fact_hash(row):
    row = dict(row)
    # Older operator fixtures omit optional defaults. They are not invented.
    return digest({key: row.get(key) for key in FACT_FIELDS})


def _schema(connection):
    return {row[0]: " ".join(row[1].split()) for row in connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE name=? OR tbl_name=? AND type='trigger'", (TABLE, TABLE))}


def check_schema(connection, create=False):
    found = _schema(connection)
    if not found and not create:
        return False
    if not found:
        connection.execute(DDL)
        for statement in TRIGGERS.values():
            connection.execute(statement)
        found = _schema(connection)
    expected = {TABLE: " ".join(DDL.split()), **{key: " ".join(value.split()) for key, value in TRIGGERS.items()}}
    if found != expected:
        raise MemoryMetadataError("Memory metadata schema or immutable guards changed")
    return True


def read_metadata(connection, row):
    """Pure SELECT. Missing/stale version is unknown, corrupt history is an error."""
    row = dict(row)
    if not check_schema(connection):
        return None
    record = connection.execute("SELECT * FROM " + TABLE + " WHERE entry_id=? AND version=?",
        (row.get("id"), row.get("version"))).fetchone()
    if record is None:
        return None
    if isinstance(record, sqlite3.Row):
        record = dict(record)
    else:
        names = [column[1] for column in connection.execute("PRAGMA table_info(" + TABLE + ")")]
        record = dict(zip(names, record))
    if any(record[key] != row.get(key) for key in OWNER_FIELDS) or record["fact_sha256"] != fact_hash(row):
        return None
    try:
        data = json.loads(record["metadata_json"])
        if digest(record["metadata_json"]) != record["metadata_sha256"] or normalize_metadata(data) != data:
            raise ValueError()
    except (ValueError, TypeError) as error:
        raise MemoryMetadataError("Memory annotation integrity check failed") from error
    return data


def write_metadata(connection, row, value, *, previous=None, targets=()):
    """Caller holds its fact write transaction. Does not commit or mutate facts."""
    if not connection.in_transaction:
        raise MemoryMetadataError("Memory annotations require the fact transaction")
    row = dict(row)
    data = normalize_metadata(value, previous)
    lookup = {target["id"]: dict(target) for target in targets}
    for link in data["links"]:
        if previous is not None and link in previous["links"]:
            continue
        target = lookup.get(link["entry_id"])
        if target is None:
            result = connection.execute("SELECT * FROM entries WHERE id=?", (link["entry_id"],)).fetchone()
            if result is not None:
                if not isinstance(result, sqlite3.Row):
                    names = [column[1] for column in connection.execute("PRAGMA table_info(entries)")]
                    target = dict(zip(names, result))
                else:
                    target = dict(result)
        if (target is None or target.get("version") != link["version"] or
                any(target.get(key) != row.get(key) for key in OWNER_FIELDS)):
            raise MemoryMetadataError("Memory relationship target changed or belongs to another owner")
    check_schema(connection, create=True)
    raw = canonical(data)
    connection.execute("INSERT INTO " + TABLE + " VALUES (?,?,?,?,?,?,?,?,?,?)", (
        row["id"], row["version"], *(row[key] for key in OWNER_FIELDS), fact_hash(row), raw,
        digest(raw), row["updated_at"]))
    return data
