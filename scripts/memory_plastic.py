# -*- coding: utf-8 -*-
"""可塑性记忆：能合并、能压缩、能把日记和刷站记录也用上。

三个问题
--------
1. **写死的记忆** —— 条目一旦写进去就原样躺着，重复的重复、过时的过时。
2. **只有数据库里的算记忆** —— 日记、判重表（刷过哪些视频）都在文件里，
   检索时看不到，等于白记。
3. **只增不减** —— 刷 B 站每一条写一条记忆，70 条还行，700 条既撑爆上下文
   又全是同一种句式。

做法
----
· `Corpus` 把三个来源拼成一个语料：数据库条目 + 日记文件 + 判重表记录
· `consolidate()` 只将同 ownership 候选装入有界、无损的派生记忆胶囊
· `compress()` 按各 ownership 的阈值整理，不为达到条数目标删除独立事实

机械层不调模型，也不根据相似度判定某条事实已被另一条替代。胶囊保留完整正文、
条目对应与时间元数据；语义摘要仍需主 LLM 审阅，并通过原生记忆工具提交。

用法
----
  python memory_plastic.py --stats        看现在有多少、能压多少
  python memory_plastic.py --consolidate --track agent --scope workspace \
      --workspace-key KEY --agent-key KEY  明确范围后合并（真的改库）
  python memory_plastic.py --dry          只看会怎么改，不落库
  python memory_plastic.py --restore-batch ID          恢复预检
  python memory_plastic.py --restore-batch ID --apply  显式恢复
"""
import io
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from project_conf import data_dir, work_dir

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = work_dir()
DATA = data_dir()
DB = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-memento", "memory.db")
DIARY = os.path.join(DATA, "diary")
SEEN = os.path.join(os.path.expanduser("~"), ".dsh", "bili-duty", "seen.md")
# 判重表超过上限会归档，语料侧不能只读 live。
try:
    import seen_cap as _sc
except Exception:
    _sc = None

# 相似候选阈值；不是事实可互相替代的判据。
SIM_MERGE = 0.62
# 库超过这个条数就开始压缩
COMPRESS_OVER = 120
# 压缩后目标
COMPRESS_TO = 80
# Limits apply to a whole capsule, including every original row's metadata.
CAPSULE_SCHEMA = "dsh-memory-capsule/v1"
CAPSULE_MAX_ROWS = 8
CAPSULE_MAX_BYTES = 8192


# ---------------------------------------------------------------------------
# 语料：把文件也算进来
# ---------------------------------------------------------------------------
def _bigrams(text):
    out = set()
    for seg in re.findall(r"[\u4e00-\u9fff]+", text or ""):
        for i in range(len(seg) - 1):
            out.add(seg[i:i + 2])
        if len(seg) == 1:
            out.add(seg)
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9_.\-]{1,30}", text or ""):
        out.add(m.group(0).lower())
    return out


def _sim(a, b):
    """Jaccard。够用，而且不会因为一条长一条短就偏。"""
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


_OWNER_FIELDS = ("track", "scope", "workspace_key", "agent_key")


class MemoryConflict(RuntimeError):
    """The reviewed ownership or row version no longer matches the database."""


def _connect():
    # mode=rw must not create a new database at a misspelled/default path.
    c = sqlite3.connect(Path(DB).resolve().as_uri() + "?mode=rw", uri=True)
    c.row_factory = sqlite3.Row
    return c


def load_db(track=None, scope=None, workspace_key=None, agent_key=None):
    if not os.path.isfile(DB):
        return []
    selection = dict(track=track, scope=scope, workspace_key=workspace_key,
                     agent_key=agent_key)
    fields = [key for key in _OWNER_FIELDS if selection[key] is not None]
    query = "SELECT * FROM entries"
    if fields:
        query += " WHERE " + " AND ".join(key + " = ?" for key in fields)
    c = _connect()
    try:
        rows = c.execute(query, [selection[key] for key in fields]).fetchall()
    finally:
        c.close()
    out = []
    for r in rows:
        d = dict(r)
        d["_src"] = "db"
        d["_bg"] = _bigrams(d.get("text") or "")
        out.append(d)
    return out


def load_diary(max_days=60):
    """日记当记忆用。一天一条，标题里带日期。"""
    if not os.path.isdir(DIARY):
        return []
    files = sorted([f for f in os.listdir(DIARY) if f.endswith(".md")],
                   reverse=True)[:max_days]
    out = []
    for fn in files:
        p = os.path.join(DIARY, fn)
        try:
            with io.open(p, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read().strip()
        except Exception:
            continue
        if not body:
            continue
        out.append({
            "id": "diary:" + fn,
            "text": "[\u65e5\u8bb0 " + fn[:-3] + "] " + body[:600],
            "track": "agent",
            "source": "diary",
            "created_at": int(os.path.getmtime(p) * 1000),
            "tags": "[]",
            "_src": "diary",
            "_bg": _bigrams(body[:600]),
        })
    return out


def load_seen():
    """判重表里的「看过哪些视频」也进语料。

    这些本来就记过，只是记在文件里，检索时看不到。
    判重表有上限（见 seen_cap），超了会归档，所以这里读 live + archive。
    """
    if _sc is not None:
        try:
            rows = _sc.load_seen_capped()
            out = []
            for cells in rows:
                bv, title, up = cells[1], cells[2], cells[3]
                out.append({
                    "id": "seen:" + bv,
                    "text": "\u5728 B \u7ad9\u770b\u4e86\u300a%s\u300b"
                            "\uff08%s\uff09%s"
                            % (title[:50], up or "\u4e0d\u77e5\u9053\u8c01", bv),
                    "track": "agent",
                    "source": "bili-seen",
                    "created_at": 0,
                    "tags": '["bili"]',
                    "_src": "seen",
                })
            return out
        except Exception:
            pass
    if not os.path.isfile(SEEN):
        return []
    out = []
    try:
        with io.open(SEEN, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except Exception:
        return []
    in_video = False
    for ln in lines:
        s = ln.strip()
        if s.startswith("## \u770b\u8fc7\u7684\u89c6\u9891"):
            in_video = True
            continue
        if s.startswith("## "):
            in_video = False
            continue
        if not in_video or not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 5 or cells[0] in ("\u65e5\u671f", "date"):
            continue
        bv, title, up = cells[1], cells[2], cells[3]
        out.append({
            "id": "seen:" + bv,
            "text": "\u5728 B \u7ad9\u770b\u4e86\u300a%s\u300b\uff08%s\uff09%s"
                    % (title[:50], up or "\u4e0d\u77e5\u9053\u8c01", bv),
            "track": "agent",
            "source": "bili-seen",
            "created_at": 0,
            "tags": '["bili"]',
            "_src": "seen",
            "_bg": _bigrams(title + " " + up),
        })
    return out


class Corpus(object):
    def __init__(self):
        self.db = load_db()
        self.diary = load_diary()
        self.seen = load_seen()

    def all(self):
        return self.db + self.diary + self.seen

    def stats(self):
        return {
            "db": len(self.db),
            "diary": len(self.diary),
            "seen": len(self.seen),
            "total": len(self.all()),
        }


# ---------------------------------------------------------------------------
# 相似聚类
# ---------------------------------------------------------------------------
def cluster(items, thr=SIM_MERGE):
    """Cluster only within the same ownership; derived summaries stay separate."""
    groups = []
    for it in items:
        placed = False
        for g in groups:
            if (_owner(it) == _owner(g[0]) and
                    not _derived(it) and not _derived(g[0]) and
                    _sim(it["_bg"], g[0]["_bg"]) >= thr):
                g.append(it)
                placed = True
                break
        if not placed:
            groups.append([it])
    return groups


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------
def _capsule_text(rows, *, allow_nul=False):
    """Lossless text de-duplication, retaining each complete SQL metadata map."""
    if any(not isinstance(row, dict) or any(not isinstance(row.get(key), str) for key in _OWNER_FIELDS)
           or row.get("track") not in ("user", "agent")
           or row.get("scope") not in ("workspace", "user-global") for row in rows):
        raise MemoryConflict("Invalid capsule ownership")
    if len({_owner(row) for row in rows}) != 1:
        raise MemoryConflict("Cannot merge different memory ownerships")
    if any(not isinstance(row.get("id"), str) or not row["id"]
           or not isinstance(row.get("text"), str)
           or (not allow_nul and "\0" in row["text"])
           or type(row.get("version")) is not int or row["version"] < 1
           or any(type(row.get(key)) is not int or row[key] < 0
                  for key in ("created_at", "updated_at")) or _derived(row) for row in rows):
        raise MemoryConflict("Invalid capsule original row")
    if len({row.get("id") for row in rows}) != len(rows):
        raise MemoryConflict("Duplicate capsule entry identity")
    facts = []
    positions = {}
    for row in sorted(rows, key=_entry_order):
        text = row["text"]
        if text not in positions:
            positions[text] = len(facts)
            facts.append(dict(text=text, entries=[]))
        facts[positions[text]]["entries"].append({key: value for key, value in row.items() if key != "text"})
    try:
        parts = [CAPSULE_SCHEMA + "\n"]
        for fact in facts:
            raw = fact["text"].encode("utf-8")
            header = dict(entries=fact["entries"], text_bytes=len(raw),
                          text_sha256=hashlib.sha256(raw).hexdigest())
            parts.append(json.dumps(header, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False) + "\n")
            # Native memento also searches entries.text with SQL LIKE. Keeping
            # each text contiguous here preserves quotes, backslashes/newlines.
            # UTF-8 byte length frames bodies containing apparent header lines.
            parts.extend((fact["text"], "\n"))
        value = "".join(parts)
        value.encode("utf-8")
        return value
    except (TypeError, ValueError, UnicodeError) as error:
        raise MemoryConflict("Capsule original is not losslessly serializable") from error


def merge_group(g):
    """Build one bounded capsule; similarity never licenses dropping a fact."""
    if len(g) < 2:
        return None
    if any(not isinstance(row, dict) for row in g):
        raise MemoryConflict("Invalid capsule original row")
    originals = [_entry_data(row) for row in g]
    # Validate all owners/metadata before declining unsafe text. A NUL-bearing
    # SQLite TEXT can be truncated by the native driver; never place another
    # original after it in a shared capsule. Keep that whole row standalone.
    text = _capsule_text(originals, allow_nul=True)
    if (any("\0" in row["text"] for row in originals) or len(originals) > CAPSULE_MAX_ROWS
            or len(text.encode("utf-8")) > CAPSULE_MAX_BYTES):
        return None
    return dict(text=text, merged_ids=[row["id"] for row in originals],
                merged_n=len(originals), _rows=originals)


def _planned_merges(groups):
    """Split candidate groups on row/byte bounds; oversized single rows remain."""
    result = []
    for group in groups:
        chunk = []
        for row in sorted(group, key=_entry_order):
            if isinstance(row.get("text"), str) and "\0" in row["text"]:
                continue
            if chunk and merge_group(chunk + [row]) is None:
                merge = merge_group(chunk)
                if merge is not None:
                    result.append(merge)
                chunk = []
            chunk.append(row)
        merge = merge_group(chunk)
        if merge is not None:
            result.append(merge)
    return result


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------
def _owner(row):
    return tuple(row.get(key) for key in _OWNER_FIELDS)


def _entry_order(row):
    return (row.get("created_at") or 0, row.get("updated_at") or 0,
            str(row.get("id") or ""))


def _entry_data(row):
    if row.get("_src", "db") != "db":
        raise MemoryConflict("File corpus entries cannot be changed in SQLite")
    return {key: value for key, value in row.items() if key not in ("_src", "_bg")}


def _row_json(row):
    return json.dumps(_entry_data(row), ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"))


def _checksum(payload):
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tags(row):
    try:
        value = json.loads(row.get("tags") or "[]")
        return {tag for tag in value if isinstance(tag, str)} if isinstance(value, list) else set()
    except (TypeError, ValueError):
        return set()


def _derived(row):
    return row.get("source") == "memory_plastic:derived"


def _require_selection(dry, selection):
    # A shared memento DB does not identify the current workspace/agent for us.
    if not dry and any(selection[key] is None for key in _OWNER_FIELDS):
        raise MemoryConflict("Writes require explicit track, scope, workspace_key and agent_key")


def _ensure_archive(c):
    # Maintenance audit only: do not change memento's schema version or protocol.
    c.execute("CREATE TABLE IF NOT EXISTS plastic_batches ("
              "batch_id TEXT PRIMARY KEY, operation TEXT NOT NULL, "
              "created_at INTEGER NOT NULL, state TEXT NOT NULL, restored_at INTEGER, "
              "archive_sha256 TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS plastic_archive ("
              "batch_id TEXT NOT NULL, entry_id TEXT NOT NULL, role TEXT NOT NULL, "
              "row_json TEXT NOT NULL, row_sha256 TEXT NOT NULL, replacement_id TEXT, "
              "PRIMARY KEY (batch_id, entry_id))")


def _archive_row(c, batch_id, row, role, replacement_id=None):
    payload = _row_json(row)
    c.execute("INSERT INTO plastic_archive "
              "(batch_id, entry_id, role, row_json, row_sha256, replacement_id) "
              "VALUES (?,?,?,?,?,?)",
              (batch_id, row["id"], role, payload, _checksum(payload), replacement_id))


def _archive_records(c, batch_id):
    return c.execute("SELECT * FROM plastic_archive WHERE batch_id = ? ORDER BY entry_id",
                     (batch_id,)).fetchall()


def _archive_checksum(records):
    # Seal the complete row set AND its roles/lineage, not only each payload.
    return _checksum(json.dumps([dict(record) for record in records],
                               ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def _check_current(c, row):
    current = c.execute("SELECT * FROM entries WHERE id = ?", (row["id"],)).fetchone()
    if current is None or _row_json(dict(current)) != _row_json(row):
        raise MemoryConflict("Memory row changed since review: " + str(row["id"]))


def _delete_archived(c, batch_id, row):
    archived = c.execute("SELECT row_sha256 FROM plastic_archive "
                         "WHERE batch_id = ? AND entry_id = ?",
                         (batch_id, row["id"])).fetchone()
    if archived is None or archived[0] != _checksum(_row_json(row)):
        raise MemoryConflict("Refusing deletion without the complete archived row")
    _check_current(c, row)
    cursor = c.execute("DELETE FROM entries WHERE id = ? AND version = ?",
                       (row["id"], row["version"]))
    if cursor.rowcount != 1:
        raise MemoryConflict("Memory version changed during maintenance")


def _insert_entry(c, row):
    columns = list(row)
    names = ",".join('"' + key.replace('"', '""') + '"' for key in columns)
    c.execute("INSERT INTO entries (" + names + ") VALUES (" +
              ",".join("?" for _ in columns) + ")", [row[key] for key in columns])


def _audit(c, batch_id, action, row):
    c.execute("INSERT INTO audit (ts, action, track, scope, entry_id, text, "
              "outcome, source, session_id) VALUES (?,?,?,?,?,?,?,?,?)",
              (int(time.time() * 1000), action, row["track"], row["scope"], row["id"],
               row["text"], "maintenance batch " + batch_id, "memory_plastic", None))


def _summary(merge):
    row = dict(max(merge["_rows"], key=_entry_order))
    now = int(time.time() * 1000)
    row.update(id="plastic-" + uuid.uuid4().hex, text=merge["text"],
               source="memory_plastic:derived", created_at=now, updated_at=now,
               session_id=None, version=1)
    row["tags"] = json.dumps(sorted(set().union(*[_tags(item) for item in merge["_rows"]]) |
                                    {"merged", "derived"}), ensure_ascii=True)
    if "last_recalled" in row:
        row["last_recalled"] = None
    if "recall_count" in row:
        row["recall_count"] = 0
    return row


def _review_plan(c, merges, victims):
    if victims:
        raise MemoryConflict("Fact-only trimming requires separate semantic review")
    reviewed = {}
    for merge in merges:
        rows = merge.get("_rows") or []
        capsule = _capsule_text(rows)
        ids = {row["id"] for row in rows}
        keep_id = merge.get("keep_id")
        if (not 2 <= len(rows) <= CAPSULE_MAX_ROWS or len(ids) != len(rows) or
                keep_id is not None or set(merge["merged_ids"]) != ids or
                len(merge["merged_ids"]) != len(ids)):
            raise MemoryConflict("Invalid reviewed merge")
        if merge["text"] != capsule or len(capsule.encode("utf-8")) > CAPSULE_MAX_BYTES:
            raise MemoryConflict("Reviewed capsule does not preserve every original")
        for row in rows:
            if row["id"] in reviewed:
                raise MemoryConflict("Overlapping maintenance groups")
            reviewed[row["id"]] = row
    if len({_owner(row) for row in reviewed.values()}) != 1:
        raise MemoryConflict("A write batch must select exactly one ownership")
    for row in reviewed.values():
        if "version" not in row or any(row.get(key) is None for key in _OWNER_FIELDS):
            raise MemoryConflict("Missing memory ownership/version metadata")
        _check_current(c, row)


def _apply_plan(merges, victims, dry=True, operation="consolidate"):
    if victims:
        raise MemoryConflict("Fact-only trimming requires separate semantic review")
    if not merges and not victims:
        return 0, None
    if dry:
        return len(merges), None
    c = _connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        _review_plan(c, merges, victims)
        _ensure_archive(c)
        batch_id = uuid.uuid4().hex
        c.execute("INSERT INTO plastic_batches (batch_id, operation, created_at, state) "
                  "VALUES (?,?,?,?)", (batch_id, operation, int(time.time() * 1000), "applied"))
        outputs = []
        for merge in merges:
            output = _summary(merge)
            replacement = output["id"]
            for row in merge["_rows"]:
                _archive_row(c, batch_id, row, "removed", replacement)
            _archive_row(c, batch_id, output, "derived")
            outputs.append(output)
        c.execute("UPDATE plastic_batches SET archive_sha256 = ? WHERE batch_id = ?",
                  (_archive_checksum(_archive_records(c, batch_id)), batch_id))
        # Every original and its lineage is durable in this same transaction
        # before any entry is removed. Exceptions roll the entire batch back.
        for merge, output in zip(merges, outputs):
            removed = set(merge["merged_ids"])
            for row in merge["_rows"]:
                if row["id"] in removed:
                    _delete_archived(c, batch_id, row)
                    _audit(c, batch_id, "plastic-merge", row)
            _insert_entry(c, output)
            _audit(c, batch_id, "plastic-derived", output)
        c.commit()
        return len(merges), batch_id
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def apply_merges(merges, dry=True):
    """Apply an explicitly reviewed, single-owner plan; return merge count."""
    return _apply_plan(merges, [], dry=dry)[0]


def restore_batch(batch_id, dry=True):
    """Preflight the whole batch; restore originals only when all rows match."""
    c = _connect()
    try:
        c.execute("BEGIN" if dry else "BEGIN IMMEDIATE")
        batch = c.execute("SELECT state, archive_sha256 FROM plastic_batches WHERE batch_id = ?",
                          (batch_id,)).fetchone()
        if batch is None:
            raise MemoryConflict("Unknown maintenance batch")
        if batch[0] == "restored":
            return {"batch_id": batch_id, "dry": dry, "restored": 0, "state": "restored"}
        if batch[0] != "applied":
            raise MemoryConflict("Maintenance batch is not ready for restore")
        records = _archive_records(c, batch_id)
        if _archive_checksum(records) != batch["archive_sha256"]:
            raise MemoryConflict("Maintenance archive batch checksum mismatch")
        decoded = []
        for record in records:
            if _checksum(record["row_json"]) != record["row_sha256"]:
                raise MemoryConflict("Archived original checksum mismatch")
            row = json.loads(record["row_json"])
            if row.get("id") != record["entry_id"] or record["role"] not in ("kept", "removed", "derived"):
                raise MemoryConflict("Invalid maintenance archive metadata")
            decoded.append((record, row))
        if not decoded or len({_owner(row) for _, row in decoded}) != 1:
            raise MemoryConflict("Invalid maintenance archive ownership")
        by_id = {row["id"]: row for _, row in decoded}
        for record, row in decoded:
            replacement = record["replacement_id"]
            if replacement and (replacement not in by_id or _owner(row) != _owner(by_id[replacement])):
                raise MemoryConflict("Invalid maintenance lineage")
            if record["role"] == "removed":
                if c.execute("SELECT 1 FROM entries WHERE id = ?", (row["id"],)).fetchone():
                    raise MemoryConflict("Original memory ID is occupied; refusing overwrite")
            else:
                # Includes scope, version, content and recall counters: a used
                # or subsequently edited summary must not be silently removed.
                _check_current(c, row)
        originals = [row for record, row in decoded if record["role"] == "removed"]
        outputs = [row for record, row in decoded if record["role"] == "derived"]
        if not dry:
            for row in outputs:
                _delete_archived(c, batch_id, row)
            for row in originals:
                _insert_entry(c, row)
                _audit(c, batch_id, "plastic-restore", row)
            c.execute("UPDATE plastic_batches SET state = ?, restored_at = ? WHERE batch_id = ?",
                      ("restored", int(time.time() * 1000), batch_id))
            c.commit()
        return {"batch_id": batch_id, "dry": dry, "restored": len(originals),
                "removed_summaries": len(outputs), "state": "preview" if dry else "restored"}
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def consolidate(dry=True, track=None, scope=None, workspace_key=None, agent_key=None):
    selection = dict(track=track, scope=scope, workspace_key=workspace_key, agent_key=agent_key)
    _require_selection(dry, selection)
    db_items = load_db(**selection)
    groups = cluster(db_items)
    merges = _planned_merges(groups)
    done, batch_id = _apply_plan(merges, [], dry=dry)
    return {
        "before": len(db_items),
        "groups": len(groups),
        "merges": len(merges),
        "applied": 0 if dry else done,
        "dry": dry,
        "batch_id": batch_id,
        "sample": ["lossless capsule: %d entries, %d UTF-8 bytes" %
                   (len(m["_rows"]), len(m["text"].encode("utf-8"))) for m in merges[:3]],
    }


def compress(dry=True, track=None, scope=None, workspace_key=None, agent_key=None):
    """Consolidate/archive overfull ownerships in one recoverable transaction.

    File-based seen rotation is a separate operation, outside this DB batch.
    """
    selection = dict(track=track, scope=scope, workspace_key=workspace_key, agent_key=agent_key)
    _require_selection(dry, selection)
    items = load_db(**selection)
    partitions = {}
    for item in items:
        partitions.setdefault(_owner(item), []).append(item)
    merges = []
    left = len(items)
    targets_met = []
    for partition in partitions.values():
        if len(partition) <= COMPRESS_OVER:
            targets_met.append(len(partition) <= COMPRESS_TO)
            continue
        planned = _planned_merges(cluster(partition))
        merges.extend(planned)
        reduced = sum(len(m["merged_ids"]) - 1 for m in planned)
        left -= reduced
        targets_met.append(len(partition) - reduced <= COMPRESS_TO)
    done, batch_id = _apply_plan(merges, [], dry=dry, operation="compress")
    return {"merged": len(merges), "applied": 0 if dry else done,
            "trimmed": 0, "left": left, "count": len(items), "soft_target_met": all(targets_met),
            "dry": dry, "batch_id": batch_id, "seen": None}


def _cli():
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="Scoped, recoverable memory maintenance")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--stats", action="store_true")
    actions.add_argument("--consolidate", action="store_true")
    actions.add_argument("--compress", action="store_true")
    actions.add_argument("--restore-batch")
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Execute a reviewed restore")
    for key in _OWNER_FIELDS:
        parser.add_argument("--" + key.replace("_", "-"))
    args = parser.parse_args()
    selection = {key: getattr(args, key) for key in _OWNER_FIELDS}
    if args.apply and not args.restore_batch:
        parser.error("--apply is only used with --restore-batch")
    if args.apply and args.dry:
        parser.error("--dry and --apply cannot be combined")
    try:
        if args.restore_batch:
            result = restore_batch(args.restore_batch, dry=not args.apply)
        elif args.compress:
            result = compress(dry=args.dry, **selection)
        elif args.consolidate or args.dry:
            result = consolidate(dry=args.dry, **selection)
        else:
            cp = Corpus()
            cp.db = load_db(**selection)
            groups = cluster(cp.db)
            result = dict(cp.stats(), mergeable=sum(1 for g in groups if merge_group(g)))
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    except (MemoryConflict, sqlite3.Error) as error:
        print("memory maintenance refused: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
