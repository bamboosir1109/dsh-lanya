# -*- coding: utf-8 -*-
"""验证：她点各种情绪，到底能不能拿到图。

模拟 groupwatch 的选图逻辑：
  1. 先查三个图包的 memes/<mood>/
  2. 再查 stolen/<mood>/
  3. 都没有就落到 other/ + 全部 stolen 兜底
"""
import os
import random

WORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = "\u5927\u80a5\u9c7c"
ROOTS = [
    os.path.join(os.path.expanduser("~"), ".dsh", "profiles", "desktop",
                 "node_modules", "dsh-meme", "memes", "dafeiyu-001"),
    os.path.join(os.path.expanduser("~"), ".dsh", "meme-packs", "official-001"),
    os.path.join(os.path.expanduser("~"), ".dsh", "meme-packs", "whale-girl-emoji"),
]
STOLEN = os.path.join(WORK, DATA, "memes", "stolen")

ALIAS = {
    "see": "surprise", "look": "surprise", "wow": "surprise",
    "sigh": "speechless", "meh": "speechless", "whatever": "speechless",
    "baka": "confused", "huh": "confused", "what": "confused",
    "laugh": "happy", "lol": "happy", "haha": "happy",
    "cry": "sad", "sob": "sad",
    "mad": "angry", "rage": "angry",
    "no": "refuse", "nope": "refuse",
    "tired": "sleepy", "sleep": "sleepy",
    "eat": "hungry", "food": "hungry",
    "rich": "money", "coin": "money",
    "surprised": "surprise", "sleep": "sleepy", "givemoney": "money",
}


def imgs(d):
    out = []
    if not os.path.isdir(d):
        return out
    for r, ds, fs in os.walk(d):
        for f in fs:
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                out.append(os.path.join(r, f))
    return out


def pick(mood):
    m = ALIAS.get(mood, mood)
    cands = []
    for root in ROOTS:
        cands += imgs(os.path.join(root, "memes", m))
    cands += imgs(os.path.join(STOLEN, m))
    if cands:
        return m, len(cands), "mood"
    # 兜底
    fb = []
    for root in ROOTS:
        fb += imgs(os.path.join(root, "memes", "other"))
    fb += imgs(STOLEN)
    if fb:
        return m, len(fb), "fallback"
    return m, 0, "none"


TESTS = ["happy", "sad", "angry", "cute", "shy", "confused", "speechless",
         "surprise", "cheer", "sleepy", "hungry", "money", "refuse",
         "stolen", "other",
         "sigh", "baka", "see", "surprised", "givemoney"]

print("  %-12s %-6s %-8s %s" % ("mood", "张数", "来源", "可用"))
print("  " + "-" * 45)
ok = 0
for t in TESTS:
    m, n, src = pick(t)
    good = n > 0
    if good:
        ok += 1
    print("  %-12s %-6d %-8s %s" % (t + ("->" + m if m != t else ""), n, src,
                                    "OK" if good else "FAIL"))
print("")
print("  %d / %d 能拿到图" % (ok, len(TESTS)))
