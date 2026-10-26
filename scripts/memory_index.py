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
  python memory_index.py --search "deepseek"
  python memory_index.py --graph
"""
import json
import math
import os
import re
import sqlite3
import time

DB = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-memento", "memory.db")


# ---------------------------------------------------------------------------
# 切词
# ---------------------------------------------------------------------------
_ASCII = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{1,30}")
_BV = re.compile(r"BV[0-9A-Za-z]{10}")
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
def load_entries():
    if not os.path.isfile(DB):
        return []
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT id, track, scope, text, source, created_at, "
        "last_recalled, recall_count, tags FROM entries").fetchall()
    c.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["taglist"] = json.loads(d.get("tags") or "[]")
        except Exception:
            d["taglist"] = []
        d["tok"] = tokens(d.get("text") or "")
        d["ent"] = entities(d.get("text") or "")
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
class Index:
    def __init__(self, entries=None):
        if entries is not None:
            self.entries = entries
        else:
            # 有 memory_plastic 就用它拼的完整语料（数据库 + 日记 + 判重表），
            # 这样"我上个月看过什么"能搜到，而不是只剩数据库里那几条。
            try:
                import memory_plastic as _mp
                self.entries = _mp.Corpus().all()
                # Corpus 来的条目没切过词，补齐；顺便统一 tags 字段
                for e in self.entries:
                    if "tok" not in e:
                        e["tok"] = tokens(e.get("text") or "")
                    if "ent" not in e:
                        # 图要按实体连边，Corpus 来的条目得补上
                        e["ent"] = entities(e.get("text") or "")
                    if "taglist" not in e:
                        try:
                            e["taglist"] = json.loads(e.get("tags") or "[]")
                        except Exception:
                            e["taglist"] = []
            except Exception:
                self.entries = load_entries()
        self.n = len(self.entries)
        self.df = {}
        self.avgdl = 0.0
        total = 0
        for e in self.entries:
            total += len(e["tok"])
            for t in set(e["tok"]):
                self.df[t] = self.df.get(t, 0) + 1
        self.avgdl = (total / self.n) if self.n else 0.0

    def _idf(self, t):
        df = self.df.get(t, 0)
        # BM25 的 idf，加 0.5 防负数
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def search(self, query, limit=8, k1=1.5, b=0.75):
        qt = tokens(query)
        if not qt or not self.entries:
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
    import sys
    if "--search" in sys.argv:
        q = sys.argv[sys.argv.index("--search") + 1]
        idx = Index()
        print("  库里有 %d 条" % idx.n)
        for r in idx.search(q, limit=6):
            print("  %-6s %s" % (r["score"], (r["text"] or "")[:70]))
        return
    if "--graph" in sys.argv:
        g = Index().graph()
        print("  节点 %d，边 %d" % (len(g["nodes"]), len(g["edges"])))
        from collections import Counter
        c = Counter(n["kind"] for n in g["nodes"])
        for k, v in c.most_common():
            print("    %-8s %d" % (k, v))
        return
    print("  usage: --search <词> | --graph")


if __name__ == "__main__":
    _cli()
