# -*- coding: utf-8 -*-
"""把所有面板要的数据导成静态 JSON，放在页面旁边。

为什么：页面在 DSH 的内嵌视图里打开时，往 19389 发的 fetch 会被挡掉，
表现就是"页面能开但一片空白"。同源读静态文件没有这个问题。

由调度器每 30 秒跑一次。写的都是只读快照，不会动任何源数据。
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts")
                if os.path.basename(HERE).lower() == "server" else HERE)
import project_conf

ROOT = project_conf.work_dir()
OUT = os.path.join(project_conf.data_dir(), "made")

# 直接复用面板服务里的数据函数，保证两边口径一致
import importlib.util

_panel = next((os.path.join(HERE, name) for name in
               ("panel-server.py", "lanya-panel-server.py", "dafeiyu-panel-server.py")
               if os.path.isfile(os.path.join(HERE, name))), None)
if _panel is None:
    raise FileNotFoundError("panel-server.py is not installed beside make_snapshots.py")
spec = importlib.util.spec_from_file_location("lps", _panel)
lps = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(lps)
except SystemExit:
    pass


def dump(name, obj):
    path = os.path.join(OUT, "lanya-" + name + ".json")
    try:
        with io.open(path, "w", encoding="utf-8", newline="") as fh:
            json.dump(obj, fh, ensure_ascii=False)
        return True
    except Exception as e:
        print("  fail %s: %s" % (name, e))
        return False


def main():
    os.makedirs(OUT, exist_ok=True)
    ok = 0

    try:
        groups = lps.meme_groups_with_tags()   # 按多标签表展开
        if dump("memes", {"ok": True,
                          "total": sum(g["count"] for g in groups),
                          "groups": groups}):
            ok += 1
    except Exception as e:
        print("  memes:", e)

    try:
        with io.open(lps.BUDGET, "r", encoding="utf-8") as fh:
            if dump("budget", {"ok": True, "budget": json.load(fh)}):
                ok += 1
    except Exception as e:
        print("  budget:", e)

    try:
        with io.open(lps.CONFIG, "r", encoding="utf-8") as fh:
            if dump("config", {"ok": True, "config": json.load(fh)}):
                ok += 1
    except Exception as e:
        print("  config:", e)

    try:
        with io.open(lps.TUNABLES, "r", encoding="utf-8") as fh:
            if dump("tunables", {"ok": True, "tunables": json.load(fh)}):
                ok += 1
    except Exception as e:
        print("  tunables:", e)

    try:
        if dump("tools", {"ok": True, "tools": lps.tools_snapshot()}):
            ok += 1
    except Exception as e:
        print("  tools:", e)

    try:
        if dump("schedule", {"ok": True,
                             "reminders": lps.reminders_snapshot(),
                             "tasks": lps.scheduled_tasks()}):
            ok += 1
    except Exception as e:
        print("  schedule:", e)

    try:
        if dump("memory", {"ok": True, "memory": lps.memory_overview()}):
            ok += 1
    except Exception as e:
        print("  memory:", e)

    try:
        if dump("bili", {"ok": True, "bili": lps.bili_snapshot()}):
            ok += 1
    except Exception as e:
        print("  bili:", e)

    try:
        with io.open(lps.MEMECFG,
                     "r", encoding="utf-8") as fh:
            if dump("memes-config", {"ok": True, "config": json.load(fh)}):
                ok += 1
    except Exception as e:
        print("  memes-config:", e)

    # 文件列表（带预览的只做前 60 条，别每次读几千个文件）
    for key in ("diary", "notes", "people"):
        try:
            files = lps.list_dir_files(key, with_preview=True, limit=60)
            if dump("files-" + key, {"ok": True, "dir": key, "files": files}):
                ok += 1
        except Exception as e:
            print("  files-%s: %s" % (key, e))

    # 作品列表不带预览（只要名字和大小）
    try:
        files = lps.list_dir_files("made", with_preview=False, limit=0)
        if dump("files-made", {"ok": True, "dir": "made", "files": files}):
            pass
            ok += 1
    except Exception as e:
        print("  files-made:", e)

    # 归档目录也要快照，否则作品页的「归档」标签会去读一个不存在的
    # dafeiyu-files-archive.json，表现就是 404。
    try:
        af = lps.list_dir_files("archive", with_preview=False, limit=0)
        dump("files-archive", {"ok": True, "dir": "archive", "files": af})
    except Exception:
        pass

    print("  snapshots written: %d" % ok)


if __name__ == "__main__":
    main()
