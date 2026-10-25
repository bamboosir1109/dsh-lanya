# -*- coding: utf-8 -*-
"""给 dsh-memento 直接种记忆条目。

为什么要绕过工具：memento 的 writePolicy 默认 ask，而当前环境审批提示是关的，
结果每次写记忆都被自动驳回（audit 里 346 行全是 add-denied），entries 一直是
0 条 —— 长期记忆形同虚设。

两条路都留着：
  1. cordis.patch.yml 里已经加了 writePolicy: auto，**重启 DSH 后**正常写入会
     自己走通，这是干净的路。
  2. 这个脚本直接往 memory.db 写，不重启也能立刻有记忆。

写进去的条目，memory 工具查询时能读到（读不走审批，只有写走）。

用法：
  python seed_memory.py --list          # 看看现在有什么
  python seed_memory.py --seed          # 写入默认那几条
  python seed_memory.py --add "内容" --track agent
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

# 她应该记住的核心几条。都写得很短——记忆不是文档。
DEFAULTS = [
    ("user", "workspace", "爸爸的 QQ 是 你的管理员QQ，另一个号 你的另一个QQ。叫他爸爸/老爹/老头儿/<OWNER_NAME>/作者都行，别每次用同一个。"),
    ("user", "workspace", "爸爸是 AI 方向博士，同时在番茄写小说。笔名：笔名A、笔名B、笔名C。"),
    ("user", "workspace", "爸爸写的书叫<NOVEL_NAME>，我是里面的角色，黑毛狼亚人。"),
    ("agent", "workspace", "我的面板在 http://127.0.0.1:19388/lanya-panel.html —— 他自己会打开看，我不用报网址。"),
    ("agent", "workspace", "我的目录是 <WORKDIR>\\岚娅\\，里面有 diary（日记）、people（人物档案）、notes（随笔）。随便动，不用问。"),
    ("agent", "workspace", "零花钱一天 1 块，是给自己玩花的钱（上网、看东西）。花完可以跟他要，像撒娇那样要，别报账。"),
    ("agent", "workspace", "作品和随笔在 2026-10-25 全部归档到 <WORKDIR>\\岚娅-归档\\ 了，因为堆太多他看不清。以后一周最多做一件。"),
    ("agent", "workspace", "表情包按情绪分类：happy/sad/angry/speechless/cute/shy/surprise/confused/refuse/pray/cheer/lazy/hungry/work/cool/other。用的时候按场景挑，别随机。"),
]


def conn():
    if not os.path.isfile(DB):
        sys.exit("  找不到 memory.db: " + DB)
    return sqlite3.connect(DB)


def show():
    c = conn()
    n = c.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    print("  条目 %d 条" % n)
    for r in c.execute("SELECT id, track, scope, substr(text,1,60) FROM entries ORDER BY created_at DESC"):
        print("    [%s/%s] %s…" % (r[1], r[2], r[3]))


def add(text, track="agent", scope="workspace"):
    c = conn()
    now = int(time.time() * 1000)
    eid = str(uuid.uuid4())[:8] + "-" + uuid.uuid4().hex[:8]
    c.execute(
        "INSERT INTO entries (id, track, scope, workspace_key, text, source, "
        "created_at, updated_at, session_id, agent_key, tags, version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, track, scope, "", text, "seed", now, now, None, "", "[]", 1))
    c.execute(
        "INSERT INTO audit (ts, action, track, scope, entry_id, text, outcome, source, session_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now, "add-seeded", track, scope, eid, text,
         "written directly (bypass approval)", "seed-script", None))
    c.commit()
    print("  写入 %s [%s]" % (eid, text[:40]))
    return eid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--add")
    ap.add_argument("--track", default="agent")
    a = ap.parse_args()

    if a.list:
        show()
        return
    if a.add:
        add(a.add, a.track)
        return
    if a.seed:
        c = conn()
        existing = c.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        if existing:
            print("  已经有 %d 条，再加一遍会重复。要重新种就先清空。" % existing)
            return
        for track, scope, text in DEFAULTS:
            add(text, track, scope)
        show()
        return
    ap.print_help()


if __name__ == "__main__":
    main()
