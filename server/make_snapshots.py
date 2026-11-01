# -*- coding: utf-8 -*-
"""Publish timestamped fallback snapshots; the live API is authoritative.

Only successful reads are published, using a same-directory atomic replace.
A failed read preserves the previous successful snapshot and its old time.
No source business data is changed.
"""
import io
import json
import os
import sys
import datetime
import tempfile

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
    if not isinstance(obj, dict) or obj.get("ok") is not True:
        raise ValueError("SnapshotSourceNotSuccessful")
    if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name):
        raise ValueError("InvalidSnapshotName")
    path = os.path.join(OUT, "lanya-" + name + ".json")
    value = dict(obj)
    value["_snapshot"] = {"schema": 1, "generated_at": datetime.datetime.now(
        datetime.timezone.utc).isoformat().replace("+00:00", "Z")}
    body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".lanya-snapshot-", suffix=".tmp", dir=OUT)
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
        temporary = None
        return True
    except Exception as e:
        print("snapshot_failed %s %s" % (name, type(e).__name__))
        return False
    finally:
        if temporary is not None and os.path.lexists(temporary):
            os.unlink(temporary)


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
        print("snapshot_failed memes %s" % type(e).__name__)

    try:
        budget = lps.unified_budget_snapshot()
        if dump("budget", {"ok": budget.get("availability") in ("available", "not_started"), "budget": budget}):
            ok += 1
    except Exception as e:
        print("snapshot_failed budget %s" % type(e).__name__)

    try:
        with io.open(lps.CONFIG, "r", encoding="utf-8") as fh:
            if dump("config", {"ok": True, "config": json.load(fh)}):
                ok += 1
    except Exception as e:
        print("snapshot_failed config %s" % type(e).__name__)

    try:
        with io.open(lps.TUNABLES, "r", encoding="utf-8") as fh:
            if dump("tunables", {"ok": True, "tunables": json.load(fh)}):
                ok += 1
    except Exception as e:
        print("snapshot_failed tunables %s" % type(e).__name__)

    try:
        if dump("tools", {"ok": True, "tools": lps.tools_snapshot()}):
            ok += 1
    except Exception as e:
        print("snapshot_failed tools %s" % type(e).__name__)

    try:
        if dump("schedule", {"ok": True,
                             "reminders": lps.reminders_snapshot(),
                             "tasks": lps.scheduled_tasks()}):
            ok += 1
    except Exception as e:
        print("snapshot_failed schedule %s" % type(e).__name__)

    try:
        memory = lps.memory_overview()
        if not isinstance(memory, dict) or memory.get("availability", {}).get("status") == "error":
            raise ValueError("MemorySnapshotSourceUnavailable")
        if dump("memory", {"ok": True, "memory": memory}):
            ok += 1
    except Exception as e:
        print("snapshot_failed memory %s" % type(e).__name__)

    try:
        if dump("bili", {"ok": True, "bili": lps.bili_snapshot()}):
            ok += 1
    except Exception as e:
        print("snapshot_failed bili %s" % type(e).__name__)

    try:
        with io.open(lps.MEMECFG,
                     "r", encoding="utf-8") as fh:
            if dump("memes-config", {"ok": True, "config": json.load(fh)}):
                ok += 1
    except Exception as e:
        print("snapshot_failed memes-config %s" % type(e).__name__)

    # 文件列表（带预览的只做前 60 条，别每次读几千个文件）
    for key in ("diary", "notes", "people"):
        try:
            files = lps.list_dir_files(key, with_preview=True, limit=60)
            if dump("files-" + key, {"ok": True, "dir": key, "files": files}):
                ok += 1
        except Exception as e:
            print("snapshot_failed files-%s %s" % (key, type(e).__name__))

    # 作品列表不带预览（只要名字和大小）
    try:
        files = lps.list_dir_files("made", with_preview=False, limit=0)
        if dump("files-made", {"ok": True, "dir": "made", "files": files}):
            pass
            ok += 1
    except Exception as e:
        print("snapshot_failed files-made %s" % type(e).__name__)

    # New works entries include nested applications. The old flat file
    # endpoint/snapshot stays compatible with existing callers.
    try:
        from panel_record_files import works_snapshot
        if dump("works", works_snapshot(lps.FILE_DIRS["made"], excluded=lps.is_panel_artifact)):
            ok += 1
    except Exception as e:
        print("snapshot_failed works %s" % type(e).__name__)

    # 归档目录也要快照，否则作品页的「归档」标签会去读一个不存在的
    # dafeiyu-files-archive.json，表现就是 404。
    try:
        af = lps.list_dir_files("archive", with_preview=False, limit=0)
        dump("files-archive", {"ok": True, "dir": "archive", "files": af})
    except Exception as e:
        print("snapshot_failed files-archive %s" % type(e).__name__)

    print("  snapshots written: %d" % ok)


if __name__ == "__main__":
    main()
