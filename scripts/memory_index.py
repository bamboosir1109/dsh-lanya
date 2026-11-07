# -*- coding: utf-8 -*-
"""记忆检索：关键词 + 相似度 + 图。

问题
----
现在取记忆只有两条路：全量塞给模型（70 条还行，700 条就爆），或者前端
`innerText.indexOf(kw)` 那种子串过滤（"看过什么" 搜不到 "在 B 站看了…"）。

思路
----
中文没有空格，最省事又够用的切法是大 **bigram**：`"在B站看了"` ->
`在B  B站  站看  看了`。加上英文单词和数字单独切。

打分用 BM25（就是那个 tf-idf 的加强版）：
  · 词在一条记忆里出现得越多越相关，但边际递减
  · 词在整个库里越少见越有价值（"DeepSeek" 比 "看了" 有用得多）
  · 再加一点：越新的稍稍靠前、被召回过的稍稍靠前、标签命中的加分

图
--
把每条记忆和它提到的实体连起来：人（QQ 号/昵称）、书名（《…》）、
BV 号、标签。既能看"谁被提到过"，也能从一个人跳到跟他有关的记忆。

用法（命令行自测）
------------------
  python memory_index.py --cwd WORKSPACE --agent-preset PRESET --search "keyword"
  python memory_index.py --management --graph
"""
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import datetime
from collections import Counter

from memory_context import connect_readonly, load_visible_entries, session_owner, visible

DB = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-memento", "memory.db")


# ---------------------------------------------------------------------------
# 切词
# ---------------------------------------------------------------------------
_ASCII = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{1,30}")
_BV = re.compile(r"BV[0-9A-Za-z]{10}", re.I)
_NUMERIC = re.compile(r"(?<![A-Za-z0-9_])\d+(?:[-./:]\d+)*(?![A-Za-z0-9_])")
_BOOK = re.compile(r"\u300a([^\u300b]{2,30})\u300b")   # 《…》
_QQ = re.compile(r"\b[1-9]\d{5,11}\b")

# 这些词太常见，给了权重反而坏事
_STOP = set("的 了 和 是 在 有 就 都 一个 我 你 他 她 它 们 这 那 什么 怎么 这个 那个 可以 因为 所以 但是 如果 还是 已经 以后 之前 然后 还有 现在 时候 觉得 知道 看看 起来 出来 进去 回来".split())
_RELATED = (('bilibili', '哔哩哔哩', 'B站', 'B 站'), ('视频', '影片', 'video'),
            ('约定', '承诺', '答应'), ('喜欢', '偏好', '最爱'), ('过敏', '忌口', '不能吃'),
            ('语音', 'voice', '音频'), ('记忆', 'memory'))


def tokens(text):
    """切成可检索的词。中文用 bigram，英文/数字原样。"""
    if not text:
        return []
    out = []
    # 英文单词、BV 号
    for m in _ASCII.finditer(text):
        out.append(m.group(0).lower())
    for m in _BV.finditer(text):
        out.append(m.group(0).lower())
    # Keep a full date/compound number as one token; do not match a numeric
    # suffix inside an ASCII identifier. Capsule metadata is excluded below.
    for m in _NUMERIC.finditer(text):
        out.append(m.group(0))
    # 中文：连续汉字段切 bigram
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(seg) == 1:
            if seg not in _STOP:
                out.append(seg)
            continue
        for i in range(len(seg) - 1):
            bg = seg[i:i + 2]
            if bg not in _STOP:
                out.append(bg)
    return out


def query_terms(query):
    """Small explicit lexical expansion, not embeddings or inferred facts."""
    query = query[:2048]
    weights = dict.fromkeys(tokens(query), 1.0)
    low = query.casefold()
    for group in _RELATED:
        if any(word.casefold() in low for word in group):
            for word in group:
                for token in tokens(word):
                    weights.setdefault(token, 0.35)
    return dict(list(weights.items())[:48])


def timestamp(value):
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                    parsed = parsed.replace(tzinfo=datetime.timezone.utc)
                else:
                    return None
            return parsed.timestamp() * 1000
        except (ValueError, OverflowError):
            pass
    return None


def _recorded_times(row):
    """Container creation is maintenance time, not an original memory date."""
    if row.get('source') == 'memory_plastic:derived' and row.get('text', '').startswith('dsh-memory-capsule/v1\n'):
        raw = row['text'].encode('utf-8'); pos = len(b'dsh-memory-capsule/v1\n'); values = []
        _search_text(row)  # Validate all lengths/hashes before reading metadata.
        while pos < len(raw):
            boundary = raw.index(b'\n', pos); header = json.loads(raw[pos:boundary])
            for entry in header['entries']:
                for key in ('created_at', 'updated_at'):
                    value = timestamp(entry.get(key))
                    if value is not None: values.append(value)
            pos = boundary + 1 + header['text_bytes'] + 1
        return values
    values = [timestamp(row.get(key)) for key in ('created_at', 'updated_at')]
    values = [value for value in values if value is not None]
    return values


def _fact_times(row):
    """A newly created lossless capsule must not rejuvenate old originals."""
    values = _recorded_times(row)
    return (min(values), max(values)) if values else (None, None)


def _validity(metadata, now):
    # Date-only declarations include the entire local calendar day. They are
    # not UTC midnight instants; only timezone-bearing datetimes compare as
    # instants. No recorded_at value is invented as an occurrence date.
    today = datetime.datetime.fromtimestamp(now / 1000.0).date().isoformat()
    start, end = metadata.get('valid_from'), metadata.get('valid_until')
    def after(value):
        return value > today if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) else timestamp(value) is not None and timestamp(value) > now
    def before(value):
        return value < today if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) else timestamp(value) is not None and timestamp(value) < now
    return 'expired' if before(end) else 'not_yet_valid' if after(start) else 'unspecified' if start is None and end is None else 'within_declared_range'


def _search_text(entry):
    """Decode only bounded, hash-checked capsule bodies for lexical ranking."""
    text = entry.get("text") or ""
    magic = b"dsh-memory-capsule/v1\n"
    if entry.get("source") != "memory_plastic:derived" or not text.startswith(magic.decode()):
        return text
    try:
        raw = text.encode("utf-8")
        if len(raw) > 8192:
            raise ValueError("Capsule exceeds bound")
        pos = len(magic)
        bodies = []
        original_count = 0
        while pos < len(raw):
            boundary = raw.index(b"\n", pos)
            header = json.loads(raw[pos:boundary])
            if (not isinstance(header, dict) or set(header) != {"entries", "text_bytes", "text_sha256"}
                    or type(header["text_bytes"]) is not int or header["text_bytes"] < 0
                    or not isinstance(header["entries"], list) or not header["entries"]
                    or any(not isinstance(item, dict) for item in header["entries"])):
                raise ValueError("Invalid capsule header")
            original_count += len(header["entries"])
            start = boundary + 1
            end = start + header["text_bytes"]
            body = raw[start:end]
            if (original_count > 8 or len(body) != header["text_bytes"] or raw[end:end + 1] != b"\n"
                    or hashlib.sha256(body).hexdigest() != header["text_sha256"]):
                raise ValueError("Invalid capsule body")
            bodies.append(body.decode("utf-8"))
            pos = end + 1
        if not bodies:
            raise ValueError("Empty capsule")
        return "\n".join(bodies)
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise ValueError("Invalid memory capsule") from error


def entities(text):
    """从一条记忆里抽实体，建图用。"""
    out = []
    for m in _BOOK.finditer(text or ""):
        out.append(("book", m.group(1).strip()))
    for m in _BV.finditer(text or ""):
        out.append(("bili", m.group(0)))
    for m in _QQ.finditer(text or ""):
        out.append(("person", m.group(0)))
    return out


# ---------------------------------------------------------------------------
# 读库
# ---------------------------------------------------------------------------
def load_entries(cwd=None, agent_preset=None, db_path=None, management=False):
    """Read model-visible rows by default; global reads require management=True."""
    if not management:
        return load_visible_entries(cwd, agent_preset, db_path=db_path or DB)
    if not os.path.isfile(db_path or DB):
        return []
    c = connect_readonly(db_path or DB)
    try:
        return [dict(row) for row in c.execute("SELECT * FROM entries")]
    finally:
        c.close()


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
class Index:
    def __init__(self, entries=None, *, cwd=None, agent_preset=None, management=False, db_path=None):
        owner = None if management else session_owner(cwd, agent_preset)
        if entries is None and management and db_path is None:
            # File corpus is explicitly an administrator view, never a model
            # fallback when ownership metadata or the scoped DB read fails.
            try:
                import memory_plastic as _mp
                entries = _mp.Corpus().all()
            except Exception:
                entries = load_entries(management=True)
        if entries is None:
            entries = load_entries(cwd, agent_preset, db_path=db_path, management=management)
        self.entries = [dict(row) for row in entries if management or visible(row, owner)]
        self.management_view = management
        for row in self.entries:
            search_text = _search_text(row)
            row['_body'] = search_text
            recorded_times = _recorded_times(row)
            row['_fact_times'] = (min(recorded_times), max(recorded_times)) if recorded_times else (None, None)
            row["tok"] = tokens(search_text[:32768])
            row["ent"] = entities(search_text)
            try:
                tags = row.get("taglist")
                if not isinstance(tags, list):
                    tags = json.loads(row.get("tags") or "[]")
                row["taglist"] = [tag for tag in tags if isinstance(tag, str)] if isinstance(tags, list) else []
            except (TypeError, ValueError):
                row["taglist"] = []
            metadata = row.get('_metadata') if isinstance(row.get('_metadata'), dict) else row.get('presentation') if isinstance(row.get('presentation'), dict) else {}
            row['_metadata'] = metadata
            row['_tf'] = Counter(row['tok'])
            declared_dates = [value for key in ('occurred_at', 'valid_from', 'valid_until')
                              for value in (metadata.get(key),) if isinstance(value, str)]
            recorded_dates = []
            for value in recorded_times:
                if value is not None:
                    try: recorded_dates.append(datetime.datetime.fromtimestamp(value / 1000).date().isoformat())
                    except (ValueError, OverflowError, OSError): pass
            for tag in row['taglist'][:32] + [metadata.get('title') or ''] + declared_dates + [value[:10] for value in declared_dates] + recorded_dates:
                for token in tokens(tag[:160]):
                    row['_tf'][token] += 2
        self.n = len(self.entries)
        self.df = {}
        self.avgdl = 0.0
        total = 0
        for e in self.entries:
            total += len(e["tok"])
            for t in e['_tf']:
                self.df[t] = self.df.get(t, 0) + 1
        self.avgdl = (total / self.n) if self.n else 0.0
        by_identity = {(row['id'], row.get('version')): row for row in self.entries}
        for row in self.entries:
            row['_revises'] = []; row['_revised_by'] = []
        for row in self.entries:
            for link in row['_metadata'].get('links', [])[:32]:
                target = by_identity.get((link.get('entry_id'), link.get('version')))
                if target is None or target is row or link.get('relation') not in ('corrects', 'updates') or any(
                    target.get(key) != row.get(key) for key in ('track', 'scope', 'workspace_key', 'agent_key')):
                    continue
                row['_revises'].append((target['id'], target.get('version'), link['relation']))
                target['_revised_by'].append((row['id'], row.get('version'), link['relation']))

    @classmethod
    def for_model(cls, cwd, agent_preset, db_path=None, entries=None):
        return cls(entries, cwd=cwd, agent_preset=agent_preset, db_path=db_path)

    @classmethod
    def management(cls, entries=None, db_path=None):
        return cls(entries, management=True, db_path=db_path)

    def _idf(self, t):
        df = self.df.get(t, 0)
        # BM25 的 idf，加 0.5 防负数
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def search(self, query, limit=8, k1=1.5, b=0.75, now=None, recent=False):
        limit = max(0, int(limit))
        qt = query_terms(query)
        if not limit or (not qt and not recent) or not self.entries:
            return []
        now = time.time() * 1000 if now is None else now
        scored = []
        lexical = {}
        for e in self.entries:
            tf = e['_tf']
            dl = len(e["tok"]) or 1
            score = 1.0 if recent else 0.0
            for t, weight in qt.items():
                f = tf.get(t, 0)
                if not f:
                    continue
                denom = f + k1 * (1 - b + b * dl / max(1, self.avgdl))
                score += weight * self._idf(t) * (f * (k1 + 1)) / denom
            # 标签直接命中，加分
            low = query.lower()
            for tg in e["taglist"]:
                if tg and tg.lower() in low:
                    score += 1.5
            lexical[e['id']] = score
        historical_query = bool(re.search(r'旧|历史|当时|原先|以前|before|previous|historical', query, re.I))
        for e in self.entries:
            direct = lexical[e['id']]
            related = max([lexical.get(target[0], 0) for target in e['_revises']] or [0])
            score = max(direct, related * (0.3 if historical_query else 0.75))
            if score <= 0: continue
            meta = e['_metadata']
            evidence_at = timestamp(meta.get('occurred_at'))
            evidence_at = e['_fact_times'][1] if evidence_at is None else evidence_at
            age_d = max(0.0, (now - evidence_at) / 86400000.0) if evidence_at is not None else None
            activity = meta.get('kind') == 'event' or bool(_BV.search(e['_body']) and re.search(r'看|视频|播放|观看|watched|video', e['_body'], re.I))
            half_life = 14 if activity else 180 if meta.get('kind') == 'preference' else 60
            freshness = 0.75 if age_d is None else 0.55 + 0.45 * math.exp(-math.log(2) * age_d / half_life)
            score *= freshness
            status = meta.get('status') or 'unannotated'
            validity = _validity(meta, now)
            score *= 0.2 if status == 'superseded' else 0.7 if status == 'uncertain' else 1.0
            if e['_revised_by'] and not historical_query: score *= 0.25
            if validity in ('expired', 'not_yet_valid'): score *= 0.35
            importance = meta.get('importance')
            if type(importance) is int and 0 <= importance <= 100: score *= 0.9 + importance / 250.0
            rc = e.get('recall_count')
            if type(rc) is int and rc > 0: score *= 1 + min(0.12, math.log1p(rc) * 0.02)
            placeholder = bool(re.fullmatch(r'\s*effect[-_: ]\d+\s*', e['_body'], re.I))
            if placeholder and 'effect' not in query.casefold(): score *= 0.1
            e['_retrieval'] = dict(method='local_lexical' if direct > 0 else 'linked_revision', score=round(score, 4), freshness=round(freshness, 4),
                                   age_days=round(age_d, 2) if age_d is not None else None, status=status,
                                   validity=validity, activity=activity, fact_time_range=list(e['_fact_times']),
                                   annotation_authority='agent_metadata_not_external_confirmation',
                                   revised_by=[dict(entry_id=ref[0], version=ref[1], relation=ref[2]) for ref in e['_revised_by']],
                                   current_truth='not_verified', relationship_coverage='retrieved_candidates_only')
            scored.append((score, evidence_at or 0, e))

        scored.sort(key=lambda x: (-x[0], -x[1], str(x[2]['id'])))
        out = []
        seen = set()
        for sc, _, e in scored:
            normalized = re.sub(r'\s+', ' ', e['_body']).strip().casefold()
            video = _BV.findall(normalized)
            key = ('video', e.get('role'), tuple(sorted(set(video)))) if e['_retrieval']['activity'] and video and not re.search(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}', query) else ('text', e.get('role'), normalized)
            if key in seen: continue
            seen.add(key)
            out.append({
                "id": e["id"],
                "text": e["text"],
                "track": e["track"],
                "source": e.get("source") or "",
                "tags": e["taglist"],
                "score": round(sc, 3),
                "created_at": e.get("created_at"),
                "updated_at": e.get("updated_at"),
                "version": e.get("version"),
                "scope": e.get("scope"),
                "workspace_key": e.get("workspace_key"),
                "agent_key": e.get("agent_key"),
                "retrieval": e['_retrieval'],
            })
            if len(out) >= limit: break
        return out

    def graph(self, max_nodes=400):
        """记忆 ↔ 实体 的二部图。

        节点：每条记忆 + 每个实体（人 / 书 / BV / 标签）
        边：记忆提到这个实体
        """
        nodes = {}
        edges = []

        def add_node(nid, kind, label, weight=1):
            if nid not in nodes:
                nodes[nid] = {"id": nid, "kind": kind, "label": label,
                              "weight": weight}
            else:
                nodes[nid]["weight"] += weight

        for e in self.entries:
            eid = "m:" + e["id"]
            add_node(eid, "memory", (e["text"] or "")[:40], 1)
            for kind, val in e["ent"]:
                nid = kind + ":" + val
                add_node(nid, kind, val, 1)
                edges.append({"from": eid, "to": nid})
            for tg in e["taglist"]:
                if not tg:
                    continue
                nid = "tag:" + tg
                add_node(nid, "tag", tg, 1)
                edges.append({"from": eid, "to": nid})

        # 节点太多就留权重高的
        if len(nodes) > max_nodes:
            keep = sorted(nodes.values(), key=lambda n: -n["weight"])[:max_nodes]
            ids = {n["id"] for n in keep}
            nodes = {k: v for k, v in nodes.items() if k in ids}
            edges = [e for e in edges if e["from"] in ids and e["to"] in ids]

        return {"nodes": list(nodes.values()), "edges": edges}


def _cli():
    import argparse
    parser = argparse.ArgumentParser(description="Scoped memory retrieval or explicit administrator view")
    parser.add_argument("--search")
    parser.add_argument("--graph", action="store_true")
    parser.add_argument("--management", action="store_true")
    parser.add_argument("--cwd")
    parser.add_argument("--agent-preset")
    args = parser.parse_args()
    if not args.management and (args.cwd is None or args.agent_preset is None):
        parser.error("supply --cwd and --agent-preset, or explicitly choose --management")
    idx = Index.management() if args.management else Index.for_model(args.cwd, args.agent_preset)
    if args.search is not None:
        print("  库里有 %d 条" % idx.n)
        for r in idx.search(args.search, limit=6):
            print("  %-6s %s" % (r["score"], (r["text"] or "")[:70]))
        return
    if args.graph:
        g = idx.graph()
        print("  节点 %d，边 %d" % (len(g["nodes"]), len(g["edges"])))
        from collections import Counter
        c = Counter(n["kind"] for n in g["nodes"])
        for k, v in c.most_common():
            print("    %-8s %d" % (k, v))
        return
    parser.print_help()


if __name__ == "__main__":
    _cli()
