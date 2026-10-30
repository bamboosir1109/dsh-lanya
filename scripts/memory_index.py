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

from memory_context import connect_readonly, load_visible_entries, session_owner, visible

DB = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-memento", "memory.db")


# ---------------------------------------------------------------------------
# 切词
# ---------------------------------------------------------------------------
_ASCII = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{1,30}")
_BV = re.compile(r"BV[0-9A-Za-z]{10}")
_NUMERIC = re.compile(r"(?<![A-Za-z0-9_])\d+(?:[-./:]\d+)*(?![A-Za-z0-9_])")
_BOOK = re.compile(r"\u300a([^\u300b]{2,30})\u300b")   # 《…》
_QQ = re.compile(r"\b[1-9]\d{5,11}\b")

# 这些词太常见，给了权重反而坏事
_STOP = set("""
的了和是在有就不都一个我你他她它们这那什么怎么这个那个可以因为所以但是
如果还是已经以后之前然后还有没有现在时候觉得知道看看起来出来进去回来
""".split())


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
            out.append(seg)
            continue
        for i in range(len(seg) - 1):
            bg = seg[i:i + 2]
            if bg not in _STOP:
                out.append(bg)
    return out


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
            row["tok"] = tokens(search_text)
            row["ent"] = entities(search_text)
            try:
                tags = row.get("taglist")
                if not isinstance(tags, list):
                    tags = json.loads(row.get("tags") or "[]")
                row["taglist"] = [tag for tag in tags if isinstance(tag, str)] if isinstance(tags, list) else []
            except (TypeError, ValueError):
                row["taglist"] = []
        self.n = len(self.entries)
        self.df = {}
        self.avgdl = 0.0
        total = 0
        for e in self.entries:
            total += len(e["tok"])
            for t in set(e["tok"]):
                self.df[t] = self.df.get(t, 0) + 1
        self.avgdl = (total / self.n) if self.n else 0.0

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

    def search(self, query, limit=8, k1=1.5, b=0.75):
        limit = max(0, int(limit))
        qt = tokens(query)
        if not limit or not qt or not self.entries:
            return []
        now = time.time() * 1000
        scored = []
        for e in self.entries:
            tf = {}
            for t in e["tok"]:
                tf[t] = tf.get(t, 0) + 1
            dl = len(e["tok"]) or 1
            score = 0.0
            for t in qt:
                f = tf.get(t, 0)
                if not f:
                    continue
                denom = f + k1 * (1 - b + b * dl / self.avgdl)
                score += self._idf(t) * (f * (k1 + 1)) / denom
            if score <= 0:
                continue

            # 标签直接命中，加分
            low = query.lower()
            for tg in e["taglist"]:
                if tg and tg.lower() in low:
                    score += 1.5

            # 越新越靠前一点（一个月线性衰减到 0）
            age_d = max(0.0, (now - (e.get("created_at") or now)) / 86400000.0)
            score *= max(0.6, 1.0 - age_d / 30.0)

            # 被召回过的，说明有用，稍稍靠前
            rc = e.get("recall_count") or 0
            if rc:
                score *= 1.0 + min(0.3, rc * 0.03)

            scored.append((score, e))

        scored.sort(key=lambda x: -x[0])
        out = []
        for sc, e in scored[:limit]:
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
            })
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
