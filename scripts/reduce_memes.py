# -*- coding: utf-8 -*-
"""把 stolen（她自己收回来的那批）压到目标数量以内。

⚠️ 只动 memes/stolen/，绝不碰 .dsh/meme-packs 下的官方包——
   那是插件自带的资源，移走了插件会坏（踩过一次：official-001 从 92 张
   掉到 2 张）。

保留优先级：按情绪标签的"好用程度"排，happy/cute/sad 这些常用的留前面。
"""
import io
# 根目录按脚本位置推算：<root>/napcat/xxx.py -> <root>
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import os
import shutil
import sys

DAFEIYU = os.path.join(ROOT, "\u5927\u80a5\u9c7c")
MEMES = os.path.join(DAFEIYU, "memes")
STOLEN = os.path.join(MEMES, "stolen")
OVERFLOW = os.path.join(MEMES, "_overflow")
TAGFILE = os.path.join(DAFEIYU, "dafeiyu-memes-tags.json")

IMG = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

USEFUL = ["happy", "cute", "sad", "speechless", "angry", "shy",
          "surprise", "confused", "refuse", "pray", "cheer", "thanks",
          "sleepy", "lazy", "hungry", "work", "money", "scared", "cool",
          "other"]


def imgs(d):
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d)
                  if os.path.isfile(os.path.join(d, f))
                  and f.lower().endswith(IMG) and not f.startswith("."))


def collect():
    out = []
    if not os.path.isdir(STOLEN):
        return out
    for tag in sorted(os.listdir(STOLEN)):
        d = os.path.join(STOLEN, tag)
        if not os.path.isdir(d):
            continue
        try:
            rank = USEFUL.index(tag)
        except ValueError:
            rank = 99
        for f in imgs(d):
            out.append((os.path.join(d, f), tag, rank, f))
    return out


def main():
    target = 100
    dry = "--dry" in sys.argv
    if "--target" in sys.argv:
        try:
            target = int(sys.argv[sys.argv.index("--target") + 1])
        except Exception:
            pass

    items = collect()
    print("  stolen 现有 %d 张，目标 %d" % (len(items), target))
    # 按类别轮转挑，保证每个情绪都有份——之前是按优先级一刀切，
    # 结果 96 个名额全给前几个类，cute 从 18 张掉到 4 张。
    from collections import defaultdict
    buckets = defaultdict(list)
    for it in items:
        buckets[it[1]].append(it)

    # 类别排序：常用的在前；同类别里按文件名稳定排序
    order = sorted(buckets.keys(),
                   key=lambda t: (USEFUL.index(t) if t in USEFUL else 99, t))
    for t in order:
        buckets[t].sort(key=lambda x: x[3])

    # 每个类别有个上限：other 是"归不了类"的，没必要占太多名额；
    # 真情绪尽量公平分。
    # 每类最多占 10 个名额——不然 angry 一口气吃掉 34 个，看着像只有一种情绪
    CAP = {"other": 8, "angry": 14, "sad": 14, "happy": 14, "confused": 12,
           "speechless": 10, "cute": 12, "shy": 10, "surprise": 10}
    keep = []
    idx = 0
    while len(keep) < target:
        added = False
        for t in order:
            if len(keep) >= target:
                break
            cap = CAP.get(t, target)
            already = sum(1 for x in keep if x[1] == t)
            if already >= cap:
                continue
            if idx < len(buckets[t]):
                keep.append(buckets[t][idx])
                added = True
        if not added:
            break
        idx += 1

    keep_set = set(id(x) for x in keep)
    drop = [it for it in items if id(it) not in keep_set]
    print("  保留 %d，移走 %d" % (len(keep), len(drop)))
    if dry:
        from collections import Counter
        c = Counter(t for _, t, _, _ in keep)
        for k, v in c.most_common():
            print("    留 %-12s %d" % (k, v))
        return

    tags = {}
    for path, tag, _r, base in keep:
        tags[tag + "/" + base] = [tag]
    with io.open(TAGFILE, "w", encoding="utf-8", newline="") as fh:
        import json
        json.dump(tags, fh, ensure_ascii=False, indent=1)
    print("  标签表 %d 条" % len(tags))

    os.makedirs(OVERFLOW, exist_ok=True)
    moved = 0
    for path, tag, _r, base in drop:
        try:
            dst = os.path.join(OVERFLOW, tag + "__" + base)
            n = 1
            while os.path.exists(dst):
                dst = os.path.join(OVERFLOW, "%s__%d_%s" % (tag, n, base))
                n += 1
            shutil.move(path, dst)
            moved += 1
        except Exception as e:
            print("    move failed: %s" % str(e)[:70])
    print("  移走 %d 张到 _overflow" % moved)


if __name__ == "__main__":
    main()
