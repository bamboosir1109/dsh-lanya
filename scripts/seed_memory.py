# -*- coding: utf-8 -*-
"""给 dsh-memento 直接种记忆条目。

为什么要绕过工具：memento 的 writePolicy 默认 ask，审批提示关掉的环境里
每次写记忆都会被自动驳回，entries 一直是 0 条 —— 长期记忆形同虚设。

两条路都留着：
  1. cordis.patch.yml 里写 writePolicy: auto，**重启 DSH 后**正常写入会自己
     走通，这是干净的路。
  2. 这个脚本直接往 memory.db 写，不重启也能立刻有记忆。

写进去的条目，memory 工具查询时能读到（读不走审批，只有写走）。

用法：
  python seed_memory.py --list                 # 看看现在有什么
  python seed_memory.py --seed                 # 写入下面的模板
  python seed_memory.py --add "内容" --track agent
  python seed_memory.py --from-json 我的.json   # 从文件批量写

⚠️ 默认那几条是**空模板**，不是某个人的真实情况。想让角色记住你自己的事，
   改 DEFAULTS 或者用 --add / --from-json，别把私事提交到仓库。
"""
import argparse
import io
import json
import os
import sqlite3
import sys
import time
import uuid

DB = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-memento", "memory.db")

# 空模板。里面的方括号是给你填的，不填就删掉那一行。
# 不要在这里写真实的 QQ 号、真名、笔名、小说名 —— 这个文件是公开的。
DEFAULTS = [
    ("user", "workspace",
     "主人的 QQ 是 [填你的号]。称呼可以换着叫：[列表，例如 老板/老大/名字]。"),
    ("user", "workspace",
     "主人平时在做 [填他的事]，喜欢聊 [填话题]。"),
    ("agent", "workspace",
     "我的面板地址是 http://127.0.0.1:19388/dafeiyu-panel.html，"
     "他自己会打开看，我不用报网址。"),
    ("agent", "workspace",
     "我的目录里有 diary（日记）、people（人物档案）、notes（随笔）。"
     "随便动，不用问。"),
    ("agent", "workspace",
     "零花钱一天 1 块，是给自己玩花的钱（上网、看东西）。"
     "花完可以跟他要，像撒娇那样要，别报账。"),
    ("agent", "workspace",
     "做东西要克制：一周最多一件，而且先给主人看，别直接往群里发。"),
]


def conn():
    if not os.path.isfile(DB):
        sys.exit("  找不到 memory.db: " + DB)
    return sqlite3.connect(DB)


def show():
    c = conn()
    n = c.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    print("  条目 %d 条" % n)
    for r in c.execute("SELECT track, scope, substr(text,1,60) "
                       "FROM entries ORDER BY created_at DESC"):
        print("    [%s/%s] %s…" % (r[0], r[1], r[2]))


def add(text, track="agent", scope="workspace", quiet=False):
    c = conn()
    now = int(time.time() * 1000)
    eid = str(uuid.uuid4())[:8] + "-" + uuid.uuid4().hex[:8]
    c.execute(
        "INSERT INTO entries (id, track, scope, workspace_key, text, source, "
        "created_at, updated_at, session_id, agent_key, tags, version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, track, scope, "", text, "seed", now, now, None, "", "[]", 1))
    c.execute(
        "INSERT INTO audit (ts, action, track, scope, entry_id, text, "
        "outcome, source, session_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (now, "add-seeded", track, scope, eid, text,
         "written directly (bypass approval)", "seed-script", None))
    c.commit()
    if not quiet:
        print("  写入 %s [%s]" % (eid, text[:40]))
    return eid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--add")
    ap.add_argument("--track", default="agent")
    ap.add_argument("--from-json", help="一个 JSON 文件，[{\"text\":..,\"track\":..}]")
    a = ap.parse_args()

    if a.list:
        show()
        return

    if a.add:
        add(a.add, a.track)
        return

    if a.from_json:
        with io.open(a.from_json, "r", encoding="utf-8") as fh:
            items = json.load(fh)
        for it in items:
            if not it.get("text"):
                continue
            add(it["text"], it.get("track", "agent"),
                it.get("scope", "workspace"))
        show()
        return

    if a.seed:
        c = conn()
        existing = c.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        if existing:
            print("  已经有 %d 条，再加一遍会重复。要重新种就先清空。" % existing)
            return
        for track, scope, text in DEFAULTS:
            add(text, track, scope)
        print("")
        print("  写的是空模板。想让角色记住你自己的事，用 --add 补，")
        print("  别把私事写进这个文件 —— 它是公开的。")
        show()
        return

    ap.print_help()


if __name__ == "__main__":
    main()
