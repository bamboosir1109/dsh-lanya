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
· `consolidate()` 把高度相似的条目合并成一条（可塑性：原来那些被改写掉）
· `compress()` 超过阈值时，按"同一类东西太多"合并成年表式的摘要

合并是**机械**的，不调模型：B 站那类条目句式高度一致，抽出 BV 和标题拼一句
「看过 N 个：A、B、C…」比让模型再编一遍更准也更便宜。

用法
----
  python memory_plastic.py --stats        看现在有多少、能压多少
  python memory_plastic.py --consolidate  合并相似条目（真的改库）
  python memory_plastic.py --dry          只看会怎么改，不落库
"""
import io
import json
import os
import re
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.dirname(HERE)
DATA = os.path.join(WORK, "\u5c9a\u5a05")
DB = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-memento", "memory.db")
DIARY = os.path.join(DATA, "diary")
SEEN = os.path.join(os.path.expanduser("~"), ".dsh", "bili-duty", "seen.md")
# 判重表超过上限会归档，语料侧不能只读 live。
try:
    import seen_cap as _sc
except Exception:
    _sc = None

# 合并阈值：相似度多高算"说的是同一件事"
SIM_MERGE = 0.62
# 库超过这个条数就开始压缩
COMPRESS_OVER = 120
# 压缩后目标
COMPRESS_TO = 80


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


def load_db():
    if not os.path.isfile(DB):
        return []
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = c.execute("SELECT * FROM entries").fetchall()
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
    """把相似的条目聚成组。简单的贪心：跟组里第一条比。"""
    groups = []
    for it in items:
        placed = False
        for g in groups:
            if _sim(it["_bg"], g[0]["_bg"]) >= thr:
                g.append(it)
                placed = True
                break
        if not placed:
            groups.append([it])
    return groups


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------
_BV = re.compile(r"BV[0-9A-Za-z]{10}")
_BOOK = re.compile(r"\u300a([^\u300b]{2,60})\u300b")


def merge_group(g):
    """把一组相似条目合成一条。

    B 站那类：句式一致，抽出标题拼成「看过 N 个：A、B、C」。
    其它：留最新的那条，末尾注明合并了几条。
    """
    if len(g) == 1:
        return None

    texts = [x.get("text") or "" for x in g]
    bili = [t for t in texts if "B \u7ad9\u770b\u4e86" in t]

    # 大部分是刷 B 站的 -> 按视频合并
    if len(bili) * 2 > len(texts):
        titles = []
        for t in bili:
            m = _BOOK.search(t)
            if m and m.group(1) not in titles:
                titles.append(m.group(1))
            else:
                mb = _BV.search(t)
                if mb and mb.group(0) not in titles:
                    titles.append(mb.group(0))
        if not titles:
            return None
        merged = "\u5728 B \u7ad9\u770b\u4e86 %d \u4e2a\uff1a%s" % (
            len(titles), "\u3001".join(t[:24] for t in titles[:8]))
        if len(titles) > 8:
            merged += "\u7b49"
        return {
            "text": merged,
            "merged_ids": [x["id"] for x in g],
            "merged_n": len(g),
        }

    # 其它：留最新的一条
    g2 = sorted(g, key=lambda x: x.get("created_at") or 0, reverse=True)
    keep = g2[0]
    txt = (keep.get("text") or "")[:120]
    return {
        "text": txt,
        "merged_ids": [x["id"] for x in g[1:]],
        "merged_n": len(g),
        "keep_id": keep["id"],
    }


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------
def apply_merges(merges, dry=True):
    """真的改库：删掉被合并的，写上合并后的。"""
    if not merges:
        return 0
    if dry:
        return len(merges)
    c = sqlite3.connect(DB)
    now = int(time.time() * 1000)
    n = 0
    for m in merges:
        for eid in m["merged_ids"]:
            # 只删数据库里的，日记/判重表是文件，删不得
            if str(eid).startswith(("diary:", "seen:")):
                continue
            c.execute("DELETE FROM entries WHERE id = ?", (eid,))
        if m.get("keep_id"):
            # 那条原样留着，只更新一下时间
            c.execute("UPDATE entries SET updated_at = ? WHERE id = ?",
                      (now, m["keep_id"]))
        else:
            eid = "merged-" + str(now)[-8:] + "-" + str(n)
            c.execute(
                "INSERT INTO entries (id, track, scope, workspace_key, text, "
                "source, created_at, updated_at, session_id, agent_key, tags, "
                "version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, "agent", "workspace", "", m["text"], "merged", now, now,
                 None, "", '["merged"]', 1))
            c.execute(
                "INSERT INTO audit (ts, action, track, scope, entry_id, text, "
                "outcome, source, session_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (now, "merge", "agent", "workspace", eid, m["text"],
                 "merged %d entries" % m["merged_n"], "memory_plastic", None))
        n += 1
    c.commit()
    c.close()
    return n


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def consolidate(dry=True):
    cp = Corpus()
    db_items = cp.db
    if len(db_items) < 3:
        return {"groups": 0, "merged": 0, "note": "条目太少，不用合"}

    groups = cluster(db_items)
    merges = []
    for g in groups:
        m = merge_group(g)
        if m:
            merges.append(m)

    done = apply_merges(merges, dry=dry)
    return {
        "before": len(db_items),
        "groups": len(groups),
        "merges": len(merges),
        "applied": done,
        "dry": dry,
        "sample": [m["text"][:60] for m in merges[:3]],
    }


def compress(dry=True):
    """超过阈值就压。先合并相似，再按来源比例砍最老的。

    顺手把判重表也收一下 —— 它只增不减，检索时全读进语料。
    """
    seen_info = None
    if _sc is not None:
        try:
            seen_info = _sc.trim_seen(dry=dry)
        except Exception as e:
            seen_info = {"ok": False, "error": str(e)[:80]}

    cp = Corpus()
    n = len(cp.db)
    if n <= COMPRESS_OVER:
        return {"note": "\u6ca1\u8d85\u9608\u503c", "count": n}

    r1 = consolidate(dry=dry)

    # 还是太多就动最老的那批低价值条目（合并过的、没有标签的）
    cp2 = Corpus()
    items = sorted(cp2.db, key=lambda x: x.get("created_at") or 0)
    need = max(0, len(items) - COMPRESS_TO)
    victims = []
    for it in items:
        if need <= 0:
            break
        tags = it.get("tags") or "[]"
        if '"merged"' in tags or '"bili"' in tags:
            victims.append(it["id"])
            need -= 1
    if victims and not dry:
        c = sqlite3.connect(DB)
        for vid in victims:
            c.execute("DELETE FROM entries WHERE id = ?", (vid,))
        c.commit()
        c.close()

    return {"merged": r1.get("merges", 0), "trimmed": len(victims),
            "left": len(items) - len(victims), "dry": dry,
            "seen": seen_info}


def _cli():
    import sys
    cp = Corpus()
    st = cp.stats()
    print("  \u8bed\u6599\uff1a")
    print("    \u6570\u636e\u5e93 %d \u6761" % st["db"])
    print("    \u65e5\u8bb0   %d \u6761" % st["diary"])
    print("    \u5224\u91cd\u8868 %d \u6761" % st["seen"])
    print("    \u5408\u8ba1   %d \u6761" % st["total"])

    if "--stats" in sys.argv or len(sys.argv) == 1:
        groups = cluster(cp.db)
        mergeable = sum(1 for g in groups if merge_group(g))
        print("")
        print("  \u53ef\u5408\u5e76\uff1a%d \u7ec4\uff08\u73b0\u5728 %d \u6761\uff0c\u5408\u5b8c\u7ea6 %d \u6761\uff09"
              % (mergeable, len(cp.db), len(groups)))

    if "--consolidate" in sys.argv or "--dry" in sys.argv:
        dry = "--dry" in sys.argv
        r = consolidate(dry=dry)
        print("")
        print("  " + json.dumps(r, ensure_ascii=False, indent=1))

    if "--compress" in sys.argv:
        r = compress(dry="--dry" in sys.argv)
        print("")
        print("  " + json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    _cli()
