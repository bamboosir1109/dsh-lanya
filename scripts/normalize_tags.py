# -*- coding: utf-8 -*-
"""把表情包目录名统一成「情绪」标签。

issue：恢复 _overflow 时把官方包的原始分类名（color / cpu / mood / see /
reply …）也带进了 stolen，这些不是使用场景，判断不了什么时候用。

这张表把它们映射到真正的情绪上，然后重命名目录。
"""
import io
# 根目录按脚本位置推算：<root>/napcat/xxx.py -> <root>
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import os

STOLEN = os.path.join(ROOT, "\u5927\u80a5\u9c7c", "memes", "stolen")

# 官方包的分类名 -> 情绪
MAP = {
    "baka": "other",
    "color": "other",
    "cpu": "confused",
    "daily": "other",
    "fight": "angry",
    "fool": "other",
    "givemoney": "money",
    "greet": "happy",
    "like": "cute",
    "meow": "cute",
    "mood": "other",
    "morning": "happy",
    "reply": "other",
    "see": "other",
    "sigh": "sad",
    "sleep": "sleepy",
    "surprised": "surprise",
    "shy": "shy",
    "happy": "happy",
    "sad": "sad",
    "angry": "angry",
    "confused": "confused",
    "work": "work",
}

VALID = {"happy", "sad", "angry", "speechless", "shy", "surprise", "lazy",
         "sleepy", "hungry", "work", "money", "cheer", "thanks", "refuse",
         "confused", "cute", "cool", "pray", "scared", "other"}


def main():
    if not os.path.isdir(STOLEN):
        print("  stolen 不存在")
        return
    moved = 0
    for tag in sorted(os.listdir(STOLEN)):
        d = os.path.join(STOLEN, tag)
        if not os.path.isdir(d):
            continue
        new = MAP.get(tag, tag if tag in VALID else "other")
        if new == tag:
            continue
        dst = os.path.join(STOLEN, new)
        os.makedirs(dst, exist_ok=True)
        n = 0
        for f in os.listdir(d):
            src = os.path.join(d, f)
            if not os.path.isfile(src):
                continue
            tgt = os.path.join(dst, f)
            k = 1
            while os.path.exists(tgt):
                stem, ext = os.path.splitext(f)
                tgt = os.path.join(dst, "%s_%d%s" % (stem, k, ext))
                k += 1
            try:
                os.replace(src, tgt)
                n += 1
            except Exception as e:
                print("    fail %s: %s" % (f, str(e)[:50]))
        moved += n
        try:
            if not os.listdir(d):
                os.rmdir(d)
        except Exception:
            pass
        print("  %-12s -> %-12s %d 张" % (tag, new, n))

    print("\n  映射完 %d 张，现在:" % moved)
    total = 0
    for tag in sorted(os.listdir(STOLEN)):
        d = os.path.join(STOLEN, tag)
        if os.path.isdir(d):
            c = len([f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f))])
            total += c
            print("    %-12s %d" % (tag, c))
    print("  合计 %d 张" % total)


if __name__ == "__main__":
    main()
