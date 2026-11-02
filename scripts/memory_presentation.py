"""Read-only titles and time provenance for the private memory console.

Display excerpts never rewrite entries or infer when an event occurred. A
record's insertion time and a diary document's date are separate concepts.
"""
import datetime
import json
import os
from pathlib import Path
import re
import stat


KINDS = frozenset(("fact", "preference", "event", "rule", "reflection", "uncertain"))
_FIELDS = ("id", "track", "scope", "workspace_key", "agent_key", "text", "source",
           "tags", "version", "created_at", "updated_at", "recall_count", "last_recalled")
_ZONE = datetime.timezone(datetime.timedelta(hours=8))


def timestamp(value):
    """Return an explicit ISO instant, or None. Never substitute today's date."""
    try:
        if type(value) in (int, float) and value > 0:
            seconds = value / 1000 if value > 100000000000 else value
            instant = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
        elif isinstance(value, str) and value.strip():
            text = value.strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                datetime.date.fromisoformat(text)
                return text
            instant = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                return None
        else:
            return None
        return instant.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def _plain(value, limit):
    return " ".join(str(value or "").replace("\x00", " ").split())[:limit]


def excerpt_title(text, limit=48):
    """A transparent lexical excerpt, not an LLM claim or a rewritten fact."""
    text = str(text or "")
    if re.fullmatch(r"\s*effect-\d+\s*", text):
        return "待整理的活动记录"
    work = re.search(r"《([^》\n]{2,160})》", text)
    if work:
        return _plain(work.group(1), limit)
    line = next((line.strip().lstrip("#* >-").strip()
                 for line in text.splitlines() if line.strip()), "")
    line = re.sub(r"^(?:我自己的|关于我|关于用户|关于爸爸)\s*[:：·]?\s*", "", line)
    first = re.split(r"[。！？\n]", line, maxsplit=1)[0]
    return _plain(first, limit) or "尚无主题的记忆"


def present_entry(row, metadata=None):
    """Only public API fields; metadata must come from the validated reader."""
    from memory_index import _search_text
    result = {key: row.get(key) for key in _FIELDS if key in row}
    try:
        tags = json.loads(row.get("tags") or "[]") if isinstance(row.get("tags"), str) else row.get("tags", [])
    except (ValueError, TypeError):
        tags = []
    result["tags"] = [tag for tag in tags if isinstance(tag, str)][:64] if isinstance(tags, list) else []
    text = _search_text(row)
    supplied = metadata.get("title") if isinstance(metadata, dict) else None
    title = _plain(supplied, 96) if isinstance(supplied, str) and supplied.strip() else excerpt_title(text)
    recorded = timestamp(row.get("created_at"))
    kind = metadata.get("kind") if isinstance(metadata, dict) else None
    result["presentation"] = {
        "schema": "memory-presentation/v1", "title": title,
        "title_origin": "metadata" if supplied else "content_excerpt",
        "excerpt": _plain(text, 220), "display_text": text, "recorded_at": recorded,
        "updated_at": timestamp(row.get("updated_at")),
        "occurred_at": metadata.get("occurred_at") if isinstance(metadata, dict) else None,
        "kind": kind if kind in KINDS else "unspecified",
        "status": metadata.get("status", "active") if isinstance(metadata, dict) else "unannotated",
        "importance": metadata.get("importance") if isinstance(metadata, dict) else None,
        "confidence": metadata.get("confidence") if isinstance(metadata, dict) else None,
        "valid_from": metadata.get("valid_from") if isinstance(metadata, dict) else None,
        "valid_until": metadata.get("valid_until") if isinstance(metadata, dict) else None,
        "links": metadata.get("links", []) if isinstance(metadata, dict) else [],
        "last_access": timestamp(row.get("last_recalled")),
        "read_only": True,
    }
    return result


def project_entries(connection, rows):
    from memory_metadata import read_metadata
    return [present_entry(row, read_metadata(connection, row)) for row in rows]


def in_recorded_range(row, start=None, end=None):
    value = timestamp(row.get("created_at"))
    if not start and not end:
        return True
    if value is None:
        return False
    day = (datetime.date.fromisoformat(value) if len(value) == 10 else
           datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(_ZONE).date())
    return (not start or day >= start) and (not end or day <= end)


def parse_date_range(start=None, end=None):
    def parse(value):
        if value in (None, ""):
            return None
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("InvalidMemoryDateRange")
        return datetime.date.fromisoformat(value)
    start, end = parse(start), parse(end)
    if start and end and end < start:
        raise ValueError("InvalidMemoryDateRange")
    return start, end


def file_title(path, read_limit=8192):
    """Read one bounded ordinary Markdown head; a changed file yields no title."""
    path = Path(path)
    if path.suffix.lower() not in (".md", ".txt"):
        return {}
    try:
        for item in (*reversed(path.parents), path):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return {}
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            return {}
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                return {}
            raw = stream.read(read_limit)
            after = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            return {}
        text = raw.decode("utf-8-sig", errors="replace")
        heading = next((line.lstrip("# ").strip() for line in text.splitlines()
                        if line.startswith("# ")), "")
        title = _plain(heading, 96) or excerpt_title(text)
        date_match = re.match(r"(\d{4}-\d{2}-\d{2})(?:\D|$)", path.name)
        date = timestamp(date_match[1]) if date_match else None
        return {"title": title, "document_date": date, "title_origin": "document_heading" if heading else "content_excerpt"}
    except (OSError, ValueError):
        return {}
