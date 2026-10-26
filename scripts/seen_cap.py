# -*- coding: utf-8 -*-
"""给判重表（刷过哪些视频）和日记加上限。

问题
----
`seen.md` 只增不减：刷一条追加一行，一年下来几千行。
检索时全读进语料，内存和时间都会慢慢涨。

判重表还有**另一个职责**：防止重复刷同一个视频。所以不能直接删——
删了就会把刷过的视频又刷一遍。

做法
----
超过 `SEEN_KEEP` 行就把最早的搬到 `seen-archive.md`，
**判重表读的是 live + archive 两份**，所以判重不受影响，只是 live 文件变小了。
语料侧只读 archive 的最近 `ARCHIVE_IN_CORPUS` 条，避免每次检索都扫全量。

日记同理：超过 `DIARY_KEEP_DAYS` 天的老日记只留最近 `DIARY_IN_CORPUS` 条进语料。

用法
----
  python memory_plastic.py --trim-seen          真的搬
  python memory_plastic.py --trim-seen --dry    只看会搬多少
"""
import io
import os
import re
import shutil
import time

# 这些值也可以从 config/project.json 覆盖（见下方 _conf）
SEEN_KEEP = 400
ARCHIVE_IN_CORPUS = 300
DIARY_KEEP_DAYS = 120
DIARY_IN_CORPUS = 60

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.dirname(HERE)
DATA = os.path.join(WORK, "\u5927\u80a5\u9c7c")
SEEN = os.path.join(os.path.expanduser("~"), ".dsh", "bili-duty", "seen.md")
SEEN_ARCHIVE = os.path.join(os.path.expanduser("~"), ".dsh", "bili-duty",
                            "seen-archive.md")
DIARY = os.path.join(DATA, "diary")


def _conf():
    """有 project.json 就按它覆盖。读不到就用文件顶部的默认值。"""
    global SEEN_KEEP, ARCHIVE_IN_CORPUS, DIARY_KEEP_DAYS, DIARY_IN_CORPUS
    try:
        import project_conf
        SEEN_KEEP = int(project_conf.get("memory.seenKeep", SEEN_KEEP))
        ARCHIVE_IN_CORPUS = int(project_conf.get("memory.archiveInCorpus",
                                                 ARCHIVE_IN_CORPUS))
        DIARY_KEEP_DAYS = int(project_conf.get("memory.diaryKeepDays",
                                               DIARY_KEEP_DAYS))
        DIARY_IN_CORPUS = int(project_conf.get("memory.diaryInCorpus",
                                               DIARY_IN_CORPUS))
    except Exception:
        pass


_conf()

_VIDEO_HEAD = "## \u770b\u8fc7\u7684\u89c6\u9891"
_COMMENT_HEAD = "## \u8bc4\u8bba\u8bb0\u5f55"


def _split_seen(path):
    """把 seen.md 拆成「评论记录段 / 看过的视频段」的原始文本。"""
    try:
        with io.open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return ""


def _video_rows(text):
    """取出「看过的视频」那张表的数据行（不含表头和分隔线）。"""
    i = text.find(_VIDEO_HEAD)
    if i < 0:
        return [], text
    head = text[:i]
    body = text[i:]
    rows = []
    keep = []
    for ln in body.splitlines(True):
        s = ln.strip()
        if s.startswith("|") and not set(s) <= set("|-: "):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if cells and cells[0] not in ("\u65e5\u671f", "date"):
                rows.append(ln)
                continue
        keep.append(ln)
    return rows, head + "".join(keep)


def trim_seen(keep=None, dry=True):
    """看过的视频超过 keep 行，就把最早的搬到 seen-archive.md。"""
    keep = SEEN_KEEP if keep is None else keep
    if not os.path.isfile(SEEN):
        return {"ok": False, "error": "seen.md \u4e0d\u5b58\u5728"}

    with io.open(SEEN, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    rows, skeleton = _video_rows(text)
    n = len(rows)
    if n <= keep:
        return {"ok": True, "rows": n, "moved": 0, "kept": n,
                "note": "\u6ca1\u8d85\u4e0a\u9650"}

    # 行是按新到旧还是旧到新？用日期排一下更稳，但保持原顺序也行——
    # 插件是往后追加的，所以越往后越新。留尾巴。
    move = rows[:n - keep]
    stay = rows[n - keep:]

    if dry:
        return {"ok": True, "rows": n, "moved": len(move), "kept": keep,
                "dry": True}

    # 归档：追加到 archive（保留表头结构）
    first = not os.path.isfile(SEEN_ARCHIVE)
    with io.open(SEEN_ARCHIVE, "a", encoding="utf-8", newline="") as fh:
        if first:
            fh.write("# \u5224\u91cd\u8868\u5f52\u6863\uff08\u8001\u7684\u770b"
                     "\u8fc7\u7684\u89c6\u9891\uff0c\u5224\u91cd\u65f6\u4e00"
                     "\u8d77\u8bfb\uff09\n\n" + _VIDEO_HEAD + "\n\n"
                     "| \u65e5\u671f | BV | \u6807\u9898 | UP | \u72b6\u6001 |\n"
                     "| --- | --- | --- | --- | --- |\n")
        for ln in move:
            fh.write(ln if ln.endswith("\n") else ln + "\n")

    # 重建 live：骨架 + 留下的行
    out = skeleton.rstrip("\n") + "\n"
    if not out.endswith("\n\n"):
        out += "\n"
    for ln in stay:
        out += ln if ln.endswith("\n") else ln + "\n"
    with io.open(SEEN, "w", encoding="utf-8", newline="") as fh:
        fh.write(out)

    return {"ok": True, "rows": n, "moved": len(move), "kept": len(stay),
            "archive": SEEN_ARCHIVE}


def load_seen_capped():
    """语料用：live 全读 + archive 只读最近 N 条。"""
    out = []

    def parse(path, cap=None):
        rows = []
        if not os.path.isfile(path):
            return rows
        with io.open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        # 只看「看过的视频」那一节。评论记录也是 5 列的表，
        # 不筛的话会把评论当视频读进来。
        in_video = False
        for ln in lines:
            t = ln.strip()
            if t.startswith("## "):
                in_video = t.startswith(_VIDEO_HEAD)
                continue
            if not in_video:
                continue
            s = ln.strip()
            if not s.startswith("|") or set(s) <= set("|-: "):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) < 5 or cells[0] in ("\u65e5\u671f", "date"):
                continue
            rows.append(cells)
        if cap is not None and len(rows) > cap:
            rows = rows[-cap:]
        return rows

    for cells in parse(SEEN):
        out.append(cells)
    for cells in parse(SEEN_ARCHIVE, cap=ARCHIVE_IN_CORPUS):
        out.append(cells)
    return out


def trim_diary(dry=True):
    """老日记只留最近 DIARY_IN_CORPUS 条进语料（文件不删）。"""
    if not os.path.isdir(DIARY):
        return {"ok": True, "note": "\u6ca1\u6709\u65e5\u8bb0\u76ee\u5f55"}
    files = [f for f in os.listdir(DIARY) if f.endswith(".md")]
    files.sort(reverse=True)
    if len(files) <= DIARY_IN_CORPUS:
        return {"ok": True, "total": len(files), "moved": 0}
    return {"ok": True, "total": len(files),
            "inCorpus": DIARY_IN_CORPUS,
            "older": files[DIARY_IN_CORPUS:][:5],
            "note": "\u8001\u65e5\u8bb0\u4e0d\u5220\uff0c\u53ea\u662f\u4e0d"
                    "\u8fdb\u8bed\u6599"}


def _cli():
    import sys
    if "--trim-seen" in sys.argv:
        dry = "--dry" in sys.argv
        import json
        print("  " + json.dumps(trim_seen(dry=dry), ensure_ascii=False))
        return
    if "--trim-diary" in sys.argv:
        import json
        print("  " + json.dumps(trim_diary(), ensure_ascii=False))
        return
    print("  usage: --trim-seen [--dry] | --trim-diary")


if __name__ == "__main__":
    _cli()
