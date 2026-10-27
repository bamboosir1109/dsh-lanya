# -*- coding: utf-8 -*-
"""全文检查所有「发消息」的代码路径。

背景
----
这一晚反复出现同一类故障：消息发不出去、发到不存在的目标、发了没人回。
根因几乎都是同一个 —— **脱敏脚本把真值换成了占位符，而线上在跑的那份也被换了**。

    $selfId  = 'BOT_QQ'          @ 检测永远为假
    $groupId = 'YOUR_GROUP_ID'    发到一个不存在的群
    $adminQQ = 'YOUR_ADMIN_QQ'    发给一个不存在的用户

**它们全都不报错。** NapCat 那边返回 `retcode 0`（发给 0 号也返回成功），
或者返回一句「无法获取用户信息」，脚本照样往下走。

这个脚本做的事
--------------
1. 找出**每一处**发消息的调用点
2. 找出每个目标 id 的**来源**
3. 判断来源是不是「从配置读」还是「写死/占位符/空」
4. 对每个文件做语法检查
5. **实测**每个 id 在 NapCat 那边是不是真的存在

用法
----
    python audit_send.py
    python audit_send.py --live     # 额外做一次真实连通性检查（不发送）
"""
import io
import json
import os
import re
import sys

# Both derived from this file's location: tests/ lives inside the repo.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W = os.path.join(os.path.dirname(REPO), "napcat")

# 发消息的动作名
SEND_ACTIONS = (
    "send_group_msg", "send_private_msg", "send_msg",
    "send_group_forward_msg", "send_private_forward_msg",
    "send_group_poke", "send_private_poke",
)

# 目标变量名
TARGET_VARS = ("group_id", "user_id", "GroupId", "groupId", "adminQQ",
               "selfId", "TargetGroup", "target", "targets")

# 什么算「有问题」的值
BAD_LITERAL = re.compile(
    r"=\s*['\"](YOUR_[A-Z_]+|BOT_QQ|PLACEHOLDER|<[A-Z_]+>)['\"]")
# 什么算「可疑」：写死的长数字（可能是真 QQ 号）
HARD_NUMBER = re.compile(r"=\s*['\"](\d{6,12})['\"]")


def status_of(path, s, var):
    """判断某个变量在这个文件里是怎么被赋值的。"""
    # 有没有从配置读的痕迹
    reads_config = bool(re.search(
        r"(lanya-config\.json|Get-ProjectConfValue|onebot11\.json)", s))
    pat_assign = re.compile(
        r"^[ \t]*\$?" + re.escape(var) + r"\s*=\s*(.+)$", re.M)
    assigns = []
    for m in pat_assign.finditer(s):
        rhs = m.group(1).strip()
        ln = s[:m.start()].count("\n") + 1
        assigns.append((ln, rhs))

    if not assigns:
        # 可能是参数默认值  [string]$GroupId = '...'
        pat_param = re.compile(
            r"\[string\]\$" + re.escape(var) + r"\s*=\s*([^,\n]+)")
        for m in pat_param.finditer(s):
            rhs = m.group(1).strip()
            ln = s[:m.start()].count("\n") + 1
            assigns.append((ln, rhs))
    if not assigns:
        return "NOT-ASSIGNED", []

    bad = []
    for ln, rhs in assigns:
        if BAD_LITERAL.match("=" + rhs) or re.match(
                r"['\"](YOUR_[A-Z_]+|BOT_QQ|PLACEHOLDER|<[A-Z_]+>)['\"]", rhs):
            bad.append((ln, rhs, "占位符"))
        elif re.match(r"^['\"](\d{6,12})['\"]$", rhs):
            bad.append((ln, rhs, "写死的号码"))
        elif rhs in ("", "''", '""'):
            pass   # 空的，可能稍后赋值，单独看
    if bad:
        return "BAD", bad
    if reads_config or any(("lanya-config" in r) or ("Get-ProjectConfValue" in r)
                           or ("onebot11" in r) or ("Get-LoginInfo" in r)
                           or ("get_login_info" in r) for _, r in assigns):
        return "CONFIG", assigns
    return "OK", assigns


def main():
    live = "--live" in sys.argv
    print("=" * 74)
    print("  发送消息路径审计")
    print("=" * 74)
    print()

    files = []
    for base in (W, os.path.join(REPO, "scripts")):
        if not os.path.isdir(base):
            continue
        for f in sorted(os.listdir(base)):
            if f.endswith((".ps1", ".py")):
                files.append(os.path.join(base, f))

    problems = []

    for p in files:
        try:
            s = io.open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        if not any(a in s for a in SEND_ACTIONS):
            continue
        # Do not audit the auditor: its own pattern table contains the very
        # placeholder strings it looks for.
        if os.path.abspath(p) == os.path.abspath(__file__):
            continue

        rel = os.path.relpath(p, os.path.dirname(REPO))
        print("  " + rel)

        # ① 找出所有发送调用
        calls = []
        for a in SEND_ACTIONS:
            for m in re.finditer(re.escape(a), s):
                ln = s[:m.start()].count("\n") + 1
                calls.append((ln, a))
        calls.sort()
        print("    发送调用 %d 处" % len(calls))

        # ② 检查每个目标变量
        for var in TARGET_VARS:
            st, det = status_of(p, s, var)
            if st == "NOT-ASSIGNED":
                continue
            if st == "BAD":
                for ln, rhs, why in det:
                    print("      ❌ $%s L%d = %s   (%s)" % (var, ln, rhs, why))
                    problems.append((rel, var, ln, rhs, why))
            elif st == "CONFIG":
                print("      ✅ $%s  从配置读" % var)
            else:
                print("      ·  $%s  见 %s" % (var, det[0][1][:50]))

        # ③ 语法检查
        if p.endswith(".py"):
            r = os.system('python -c "import ast,sys; ast.parse(open(sys.argv[1],'
                          'encoding=\'utf-8\').read())" "%s" 2>nul' % p)
            if r != 0:
                print("      ❌ Python 语法错误")
                problems.append((rel, "-", 0, "", "语法"))
        else:
            if sum(1 for b in open(p, "rb").read() if b > 127):
                print("      ❌ .ps1 含非 ASCII 字节")
                problems.append((rel, "-", 0, "", "非 ASCII"))
        print()

    # ---------------------------------------------------------------- 汇总
    print("=" * 74)
    if problems:
        print("  ❌ 发现 %d 处问题：" % len(problems))
        for rel, var, ln, rhs, why in problems:
            print("     %-40s $%-12s L%-4d %s" % (rel, var, ln, why))
    else:
        print("  ✅ 所有发送目标都来自配置，没有占位符 / 写死的号码")
    print("=" * 74)

    if live:
        print()
        print("  实测：这些 id 在 NapCat 那边真的存在吗")
        print()
        check_live()

    return 1 if problems else 0


def check_live():
    import urllib.request
    tok = ""
    nb = os.path.join(W, "framework", "config", "onebot11.json")
    with io.open(nb, encoding="utf-8-sig") as fh:
        d = json.load(fh)
    hs = ((d.get("network") or {}).get("httpServers") or [])
    if hs:
        tok = hs[0].get("token") or ""

    def call(action, payload=None):
        req = urllib.request.Request(
            "http://127.0.0.1:8720/" + action,
            data=json.dumps(payload or {}).encode("utf-8"),
            headers={"content-type": "application/json",
                     "authorization": "Bearer " + tok},
            method="POST")
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())

    # 配置里声明的值
    # The data dir is one level ABOVE napcat, not inside it. Getting this wrong
    # is exactly the bug being audited, so look in both places.
    cfg = {}
    _parent = os.path.dirname(W)
    for _base in (_parent, W):
        for dn in ("\u5c9a\u5a05", "\u5927\u80a5\u9c7c"):
            cp = os.path.join(_base, dn, "lanya-config.json")
            if os.path.isfile(cp):
                with io.open(cp, encoding="utf-8-sig") as fh:
                    cfg = json.load(fh)
                break
        if cfg:
            break

    bot = str(cfg.get("botQQ") or "")
    grp = str(cfg.get("fanGroupQQ") or cfg.get("fanGroup") or "")
    admins = cfg.get("adminQQ") or []
    if not isinstance(admins, list):
        admins = [admins]

    print("  config: botQQ=%s fanGroupQQ=%s adminQQ=%s"
          % (bot, grp, ",".join(str(a) for a in admins)))
    print()

    # 登录账号真的是 botQQ 吗
    try:
        li = call("get_login_info")
        real = str((li.get("data") or {}).get("user_id") or "")
        ok = real == bot
        print("  %s 登录账号 %s 与 config.botQQ %s %s"
              % ("✅" if ok else "❌", real, bot,
                 "一致" if ok else "不一致 —— @ 检测会失效"))
    except Exception as e:
        print("  ❌ get_login_info 失败: " + str(e)[:60])

    # 群存在吗
    try:
        r = call("get_group_list")
        ids = [str(g.get("group_id")) for g in (r.get("data") or [])]
        ok = grp in ids
        print("  %s 群 %s %s" % ("✅" if ok else "❌", grp,
                                 "存在" if ok else "不在她的群列表里"))
    except Exception as e:
        print("  ❌ get_group_list 失败: " + str(e)[:60])

    # 管理员存在吗
    try:
        r = call("get_friend_list")
        ids = [str(g.get("user_id")) for g in (r.get("data") or [])]
        for a in admins:
            a = str(a)
            ok = a in ids
            print("  %s 管理员 %s %s" % ("✅" if ok else "❌", a,
                                         "是好友" if ok else "不是好友"))
    except Exception as e:
        print("  ❌ get_friend_list 失败: " + str(e)[:60])


if __name__ == "__main__":
    sys.exit(main())
