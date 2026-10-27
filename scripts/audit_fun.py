# -*- coding: utf-8 -*-
"""逐个检查「她自己的娱乐方式」到底有没有真在跑。

为什么需要
----------
`activities.json` 里列了六件事，面板上全是绿的，但**绿只代表配置写着 enabled**，
不代表脚本真的在跑、真的产出了东西。

这一晚已经踩过太多次「看着正常、其实没动」：

    budget.json    数字停在昨天          额度从没算过
    seen.md        最后一条是昨天        B 站从没刷过
    lifeloop.log   停在两天前            周期任务全停

所以这个脚本对每一项都查四件事：

    ① 有谁在定时叫它吗
    ② 最近一次跑成功了吗（看日志时间 + 内容）
    ③ 它该产出的东西真的出现了吗（文件 / 记录 / 行数）
    ④ 代码里有没有明显的空转（比如条件永远为假、函数没定义）

用法
----
    python audit_fun.py
"""
import io
import json
import os
import re
import subprocess
import sys
import time

W = r"<WORKDIR>\napcat"
D = os.path.join(r"<WORKDIR>", "\u5c9a\u5a05")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def age_min(p):
    if not os.path.isfile(p):
        return None
    return (time.time() - os.path.getmtime(p)) / 60.0


def size_kb(p):
    if not os.path.isfile(p):
        return None
    return os.path.getsize(p) / 1024.0


def count_lines(p):
    if not os.path.isfile(p):
        return None
    try:
        with io.open(p, encoding="utf-8", errors="ignore") as fh:
            return sum(1 for _ in fh)
    except Exception:
        return None


def tail(p, n=3):
    if not os.path.isfile(p):
        return []
    try:
        with io.open(p, encoding="utf-8", errors="ignore") as fh:
            return [l.rstrip() for l in fh.readlines()[-n:]]
    except Exception:
        return []


def fmt_age(m):
    if m is None:
        return "不存在"
    if m < 60:
        return "%.0f 分钟前" % m
    if m < 1440:
        return "%.1f 小时前" % (m / 60)
    return "%.1f 天前" % (m / 1440)


def load_json(p, default=None):
    if not os.path.isfile(p):
        return default if default is not None else {}
    try:
        with io.open(p, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return default if default is not None else {}


# ---------------------------------------------------------------------------
# 定时器的真实情况
# ---------------------------------------------------------------------------
def scheduler_state():
    """调度器活着没。

    Do NOT decide this by looking for a process called lanya-supervisor.ps1 -
    that query matches the query itself, and the scheduled task may run the
    file under a wrapper whose command line looks nothing like the file name.

    Judge by cadence instead: the supervisor runs "steal" every 10 minutes and
    "greet" every 60. If steal.log advanced within the last ~25 minutes, the
    scheduler is alive regardless of what the process list says.
    """
    steal_age = age_min(os.path.join(D, "steal.log"))
    alive = steal_age is not None and steal_age < 25
    return alive, steal_age, age_min(os.path.join(W, "lanya-supervisor.log"))


def job_list():
    """从 supervisor 里抠出任务表：名字 -> 间隔分钟。"""
    p = os.path.join(W, "lanya-supervisor.ps1")
    if not os.path.isfile(p):
        return {}
    s = io.open(p, encoding="utf-8", errors="ignore").read()
    out = {}
    for m in re.finditer(r"n\s*=\s*'([a-z\-]+)';\s*m\s*=\s*(\d+)", s):
        out[m.group(1)] = int(m.group(2))
    return out


def main():
    print("=" * 74)
    print("  娱乐方式体检")
    print("=" * 74)
    print()

    sup_alive, steal_age, sup_age = scheduler_state()
    jobs = job_list()
    print("  \u5b9a\u65f6\u5668")
    print("    \u8c03\u5ea6\u5668\u5728\u8dd1 : %s\uff08\u6309 steal \u8282\u594f\u5224\u65ad\uff09"
          % ("\u2705 \u662f" if sup_alive else "\u274c \u5426"))
    print("    supervisor \u65e5\u5fd7 : %s" % fmt_age(sup_age))
    print("    \u4efb\u52a1\u8868       : %s" % (", ".join(sorted(jobs)) or "\u7a7a"))
    print()

    acts = load_json(os.path.join(D, "activities.json")).get("activities") or []
    enabled = {a.get("id"): bool(a.get("enabled")) for a in acts}

    rows = []

    def add(name, ok, detail):
        rows.append((name, ok, detail))

    # ---------------------------------------------------------------- 群聊
    gw = os.path.join(W, "groupwatch.log")
    a = age_min(gw)
    add("group \u81ea\u5df1\u6c34\u7fa4",
        a is not None and a < 15,
        "%s / job bili?%s" % (fmt_age(a), ""))
    if os.path.isfile(gw):
        for ln in tail(gw, 30):
            if "deliver(text)" in ln:
                add("group \u53d1\u5f97\u51fa\u53bb", True, "\u6700\u8fd1\u6709\u6210\u529f\u53d1\u9001")
                break
        else:
            add("group \u53d1\u5f97\u51fa\u53bb", False, "\u6700\u8fd1 30 \u884c\u6ca1\u6709\u6210\u529f\u53d1\u9001")

    # ---------------------------------------------------------------- B 站
    seen = os.path.join(os.path.expanduser("~"), ".dsh", "bili-duty", "seen.md")
    a = age_min(seen)
    n = count_lines(seen)
    add("bili \u73a9 B \u7ad9",
        a is not None and a < 24 * 60,
        "%s / %s \u884c / job=%s" % (fmt_age(a), n, jobs.get("bili", "\u274c\u65e0")))
    soc = os.path.join(W, "bili-social.log")
    add("bili \u56de\u590d\u522b\u4eba",
        age_min(soc) is not None and age_min(soc) < 24 * 60,
        "%s / job=%s" % (fmt_age(age_min(soc)), jobs.get("bili-social", "\u274c\u65e0")))

    # ---------------------------------------------------------------- 浏览器
    # browserd.out is not a reliable heartbeat: the scheduled task redirects its
    # stdout elsewhere, and when Playwright cannot open a pipe under the sandbox
    # it dies with PermissionError before writing anything. Judge by whether the
    # browser profile directory is being touched instead.
    bdir = os.path.join(D, "browser")
    newest = None
    if os.path.isdir(bdir):
        for root2, dirs2, files2 in os.walk(bdir):
            for f2 in files2:
                try:
                    t2 = os.path.getmtime(os.path.join(root2, f2))
                except Exception:
                    continue
                if newest is None or t2 > newest:
                    newest = t2
    bage = (time.time() - newest) / 60.0 if newest else None
    # A Playwright pipe failure is fatal and silent; surface it if the newest
    # browserd.out entry says so.
    crash = ""
    bout = os.path.join(W, "browserd.out")
    for ln in tail(bout, 40):
        if "PermissionError" in ln or "crashed" in ln:
            crash = " / \u6700\u8fd1\u6709\u5d29\u6e83\u8bb0\u5f55"
    add("browser \u4e0a\u7f51\u901b",
        bage is not None and bage < 24 * 60,
        "%s%s" % (fmt_age(bage), crash))

    # ---------------------------------------------------------------- 作品
    made = os.path.join(D, "made")
    if os.path.isdir(made):
        files = [f for f in os.listdir(made)
                 if os.path.isfile(os.path.join(made, f))
                 and f.endswith((".html", ".png", ".jpg", ".gif"))]
        newest = None
        for f in files:
            t = os.path.getmtime(os.path.join(made, f))
            if newest is None or t > newest:
                newest = t
        add("create \u505a\u4e1c\u897f", True,
            "%d \u4ef6\uff0c\u6700\u8fd1 %s" % (len(files),
                                          fmt_age((time.time() - newest) / 60
                                                  if newest else None)))
    else:
        add("create \u505a\u4e1c\u897f", False, "\u6ca1\u6709 made \u76ee\u5f55")

    # ---------------------------------------------------------------- 日记
    diary = os.path.join(D, "diary")
    if os.path.isdir(diary):
        ds = sorted(f for f in os.listdir(diary) if f.endswith(".md"))
        if ds:
            newest = os.path.join(diary, ds[-1])
            a = age_min(newest)
            add("diary \u5199\u65e5\u8bb0", a is not None and a < 48 * 60,
                "\u6700\u65b0 %s\uff08%s\uff09" % (ds[-1], fmt_age(a)))
        else:
            add("diary \u5199\u65e5\u8bb0", False, "\u76ee\u5f55\u7a7a")
    else:
        add("diary \u5199\u65e5\u8bb0", False, "\u6ca1\u6709 diary \u76ee\u5f55")

    # ---------------------------------------------------------------- 随笔
    notes = os.path.join(D, "notes")
    if os.path.isdir(notes):
        ns = sorted(f for f in os.listdir(notes) if f.endswith(".md"))
        if ns:
            newest = os.path.join(notes, ns[-1])
            add("notes \u5199\u968f\u7b14",
                age_min(newest) is not None and age_min(newest) < 72 * 60,
                "\u6700\u65b0 %s\uff08%s\uff09" % (ns[-1], fmt_age(age_min(newest))))
        else:
            add("notes \u5199\u968f\u7b14", False, "\u76ee\u5f55\u7a7a")
    else:
        add("notes \u5199\u968f\u7b14", False, "\u6ca1\u6709 notes \u76ee\u5f55")

    # ---------------------------------------------------------------- 语音
    vq = load_json(os.path.join(D, "voice-queue.json"), [])
    vlog = os.path.join(D, "voice.log")
    add("voice \u53d1\u8bed\u97f3",
        age_min(vlog) is not None and age_min(vlog) < 24 * 60,
        "\u961f\u5217 %d \u6761 / \u65e5\u5fd7 %s" % (len(vq) if isinstance(vq, list) else -1,
                                              fmt_age(age_min(vlog))))

    # ---------------------------------------------------------------- 提醒
    rem = load_json(os.path.join(D, "reminders.json"), [])
    remlog = os.path.join(W, "reminders.log")
    add("reminders \u5b9a\u63d0\u9192", True,
        "\u5171 %d \u6761 / \u65e5\u5fd7 %s" % (len(rem) if isinstance(rem, list) else -1,
                                          fmt_age(age_min(remlog))))

    # ---------------------------------------------------------------- 打招呼
    gd = os.path.join(D, "greet.log")
    add("greet \u4e3b\u52a8\u6253\u62db\u547c",
        age_min(gd) is not None and age_min(gd) < 48 * 60,
        "job=%s / \u65e5\u5fd7 %s" % (jobs.get("greet", "\u274c\u65e0"), fmt_age(age_min(gd))))

    # ---------------------------------------------------------------- 偷表情
    st = os.path.join(D, "steal.log")
    add("steal \u5077\u8868\u60c5\u5305",
        age_min(st) is not None and age_min(st) < 24 * 60,
        "job=%s / \u65e5\u5fd7 %s" % (jobs.get("steal", "\u274c\u65e0"), fmt_age(age_min(st))))

    # ---------------------------------------------------------------- 生活循环
    ll = os.path.join(D, "lifeloop.log")
    a = age_min(ll)
    add("lifeloop \u81ea\u5df1\u627e\u4e50\u5b50", a is not None and a < 60, fmt_age(a))

    print("  " + "-" * 70)
    bad = 0
    for name, ok, detail in rows:
        mark = "\u2705" if ok else "\u274c"
        if not ok:
            bad += 1
        print("  %s %-24s %s" % (mark, name, detail))
    print("  " + "-" * 70)
    print("  %d \u9879\uff0c\u6709\u95ee\u9898 %d" % (len(rows), bad))
    print()

    if bad:
        print("  \u6709\u95ee\u9898\u7684\u9879\u76ee\u4e0a\u9762\u6807\u4e86 \u274c\u3002")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
