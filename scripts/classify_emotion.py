# -*- coding: utf-8 -*-
"""按情绪给 stolen 里的表情包分类，分到 stolen/<情绪>/ 子目录里。

和 stealmemes.py 的区别：那个只判断「是不是表情包」，这个判断「什么情绪、
什么场景下用」——用户要的是后者。

用法：
    python classify_emotion.py            # 处理全部
    python classify_emotion.py --limit 20 # 只处理 20 张（试跑）
    python classify_emotion.py --dry      # 只看分类结果，不移动文件
"""
import io
# 根目录按脚本位置推算：<root>/napcat/xxx.py -> <root>
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import json
import os
import re
import subprocess
import time
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LANYA = os.path.join(ROOT, "\u5c9a\u5a05")
STOLEN = os.path.join(LANYA, "memes", "stolen")
PROMPT = os.path.join(HERE, "classify-emotion.prompt.txt")
LOG = os.path.join(HERE, "classify-emotion.log")
DSH = os.path.join(os.environ.get("DSH_ROOT", r"D:\DSH"),
                   "resources", "runtime", "cli", "bin", "dsh.cmd")

IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
BATCH = 8   # 一次给模型 8 张，多了它会漏

VALID = {
    "happy", "sad", "angry", "speechless", "shy", "surprise", "lazy",
    "sleepy", "hungry", "work", "money", "cheer", "thanks", "refuse",
    "confused", "cute", "cool", "pray", "scared", "other", "notmeme",
}



# 只允许一个实例在跑。之前 2 分钟一次的触发会跟前一次重叠，两个进程抢同一张图，
# 一个移走了另一个就报 WinError 2，白跑一批。
LOCK = os.path.join(HERE, "classify-emotion.lock")


def acquire_lock():
    try:
        if os.path.exists(LOCK):
            # 超过 10 分钟的老锁当它挂了，抢过来
            if time.time() - os.path.getmtime(LOCK) < 600:
                return False
        with open(LOCK, "w") as fh:
            fh.write(str(os.getpid()))
        return True
    except Exception:
        return False


def release_lock():
    try:
        if os.path.exists(LOCK):
            os.remove(LOCK)
    except Exception:
        pass


def log(msg):
    line = "[%s] %s" % (__import__("datetime").datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with io.open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def ask_agent(task):
    try:
        p = subprocess.run([DSH, "--profile", "headless", "-"],
                           input=task.encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=150)   # 600 太长：一个 agent 挂住会卡十分钟
        raw = p.stdout or b""
        if raw[:2] == b"\xff\xfe":
            return raw.decode("utf-16-le").lstrip("\ufeff").strip()
        try:
            return raw.decode("utf-8").strip()
        except Exception:
            return raw.decode("utf-16-le", "replace").lstrip("\ufeff").strip()
    except Exception as e:
        log("agent failed: " + str(e)[:120])
        return ""


def classify_batch(paths):
    try:
        with io.open(PROMPT, "r", encoding="utf-8") as fh:
            tpl = fh.read()
    except Exception as e:
        log("prompt missing: " + str(e))
        return {}

    listing = "\n".join("%d. %s" % (i + 1, p) for i, p in enumerate(paths))
    task = tpl.replace("{{LIST}}", listing)
    answer = ask_agent(task)
    if not answer:
        return {}

    # 解析 "1. happy" 这种行
    out = {}
    for m in re.finditer(r"(?m)^\s*(\d+)\s*[.)、]\s*([a-zA-Z_]+)", answer):
        idx = int(m.group(1)) - 1
        tag = m.group(2).lower().strip()
        if tag not in VALID:
            continue
        if 0 <= idx < len(paths):
            out[paths[idx]] = tag
    return out


def main():
    limit = 0
    dry = "--dry" in sys.argv
    if "--limit" in sys.argv:
        try:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        except Exception:
            limit = 0

    if not acquire_lock():
        log("另一个分类进程在跑，这次跳过")
        return

    if not os.path.isdir(STOLEN):
        log("stolen 目录不存在")
        release_lock()
        return

    # 只处理平铺在 stolen 根下的，已经是子目录的跳过
    files = sorted(f for f in os.listdir(STOLEN)
                   if f.lower().endswith(IMG_EXT) and not f.startswith("."))
    if limit:
        files = files[:limit]

    total = len(files)
    log("待分类 %d 张%s" % (total, "（试跑，不移动）" if dry else ""))
    if not total:
        return

    done = 0
    moved = 0
    for start in range(0, total, BATCH):
        chunk = [os.path.join(STOLEN, f) for f in files[start:start + BATCH]]
        log("批次 %d-%d / %d" % (start + 1, start + len(chunk), total))
        verdict = classify_batch(chunk)
        done += len(verdict)
        for path, tag in verdict.items():
            if tag == "notmeme":
                # 不是表情包：丢进 rejected，别再让它出现在表情墙里
                dst_dir = os.path.join(LANYA, "memes", "rejected")
            else:
                dst_dir = os.path.join(STOLEN, tag)
            if dry:
                log("  %s -> %s" % (os.path.basename(path), tag))
                continue
            try:
                os.makedirs(dst_dir, exist_ok=True)
                os.replace(path, os.path.join(dst_dir, os.path.basename(path)))
                moved += 1
            except Exception as e:
                log("  move failed: " + str(e)[:80])
        log("  已分类 %d，已移动 %d" % (done, moved))

    log("完成：判定 %d 张，移动 %d 张" % (done, moved))
    release_lock()


if __name__ == "__main__":
    main()
