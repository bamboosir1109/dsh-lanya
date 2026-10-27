# -*- coding: utf-8 -*-
"""QQ 聊天链路体检。

链路
----
    QQ 客户端
      -> NapCat 注入
      -> OneBot HTTP :8720（脚本发消息用）
      -> OneBot 反向 WS :8643（插件收消息用）
      -> dsh-onebot-qq 插件
      -> DSH headless agent
      -> 回复 -> NapCat -> QQ

任何一环错了，「她不理人」或者「发不出去」。这一晚踩过的都是这一类：
token 不一致、selfId 是占位符、群号是占位符、管理员是占位符。

查什么
------
1. token 三方一致（NapCat HTTP / NapCat WS client / 插件）
2. 账号一致（NapCat 登录号 / 配置 botQQ）
3. 三处群号一致（tunables / config / 实际群列表里有）
4. 私聊准入策略（能不能回所有人）
5. 端口都在听
6. 聊天记录在更新
7. 脚本里还有没有「处理了但没用」的死变量
8. 回复重复检测（同一句话连发）

用法
----
    python audit_qq.py
"""
import io
import json
import os
import re
import sys
import time
import urllib.request

W = r"<WORKDIR>\napcat"
D = os.path.join(r"<WORKDIR>", "\u5c9a\u5a05")
PROFILE = os.path.join(os.path.expanduser("~"), ".dsh", "profiles", "desktop")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROWS = []


def add(name, ok, detail):
    ROWS.append((name, ok, detail))


def load_json(p, default=None):
    if not os.path.isfile(p):
        return default if default is not None else {}
    try:
        with io.open(p, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return default if default is not None else {}


def napcat_cfg():
    return load_json(os.path.join(W, "framework", "config", "onebot11.json"), {})


def plugin_cfg():
    p = os.path.join(PROFILE, "cordis.patch.yml")
    if not os.path.isfile(p):
        return {}
    s = io.open(p, encoding="utf-8", errors="ignore").read()
    i = s.find("dsh-onebot")
    if i < 0:
        return {}
    seg = s[i:i + 3000]
    out = {}
    m = re.search(r'accessToken:\s*"([^"]+)"', seg)
    if m:
        out["accessToken"] = m.group(1)
    m = re.search(r'workspacePath:\s*"([^"]+)"', seg)
    if m:
        out["workspacePath"] = m.group(1)
    m = re.search(r'dmPolicy:\s*(\S+)', seg)
    if m:
        out["dmPolicy"] = m.group(1)
    m = re.search(r'allowAllUsers:\s*(\S+)', seg)
    if m:
        out["allowAllUsers"] = m.group(1).lower() == "true"
    m = re.search(r'interimMessages:\s*(\S+)', seg)
    if m:
        out["interimMessages"] = m.group(1).lower() == "true"
    for key in ("adminUsers", "allowFrom"):
        m = re.search(key + r":\s*\n((?:\s*-\s*\"?\d+\"?\s*\n)+)", seg)
        if m:
            out[key] = re.findall(r"\d{5,12}", m.group(1))
    return out


def call(action, payload=None, tok=""):
    req = urllib.request.Request(
        "http://127.0.0.1:8720/" + action,
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"content-type": "application/json",
                 "authorization": "Bearer " + tok},
        method="POST")
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def port_up(p):
    import subprocess
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(netstat -ano | Select-String ':%d .*LISTENING').Count" % p],
            capture_output=True, text=True, timeout=40).stdout
        return (out or "0").strip() not in ("", "0")
    except Exception:
        return False


def main():
    print("=" * 76)
    print("  QQ 聊天链路体检")
    print("=" * 76)
    print()

    nc = napcat_cfg()
    net = nc.get("network") or {}
    hs = (net.get("httpServers") or [{}])[0]
    wcs = net.get("websocketClients") or [{}]
    wc = wcs[0] if wcs else {}
    pc = plugin_cfg()

    http_tok = hs.get("token") or ""
    ws_tok = wc.get("token") or ""
    plug_tok = pc.get("accessToken") or ""

    # ---------------------------------------------------------------- token
    add("token: NapCat HTTP vs WS",
        bool(http_tok) and http_tok == ws_tok,
        "%s / %s" % (http_tok[:10] or "(空)", ws_tok[:10] or "(空)"))
    add("token: WS vs 插件",
        bool(ws_tok) and ws_tok == plug_tok,
        "%s / %s" % (ws_tok[:10] or "(空)", plug_tok[:10] or "(空)"))

    # ---------------------------------------------------------------- 端口
    for p, label in ((8720, "OneBot HTTP"), (8643, "反向 WS"), (6099, "WebUI")):
        add("端口 %d %s" % (p, label), port_up(p), "")

    # ---------------------------------------------------------------- 账号
    cfg = {}
    for dn in ("\u5c9a\u5a05", "\u5927\u80a5\u9c7c"):
        p = os.path.join(os.path.dirname(W), dn, "lanya-config.json")
        if os.path.isfile(p):
            cfg = load_json(p)
            break

    real_id = ""
    groups = []
    try:
        li = call("get_login_info", {}, http_tok)
        real_id = str((li.get("data") or {}).get("user_id") or "")
        gl = call("get_group_list", {}, http_tok)
        groups = [str(g.get("group_id")) for g in (gl.get("data") or [])]
    except Exception as e:
        add("OneBot 可调用", False, str(e)[:50])

    bot = str(cfg.get("botQQ") or "")
    add("账号: NapCat 登录 vs config.botQQ", bool(bot) and bot == real_id,
        "%s / %s" % (real_id or "(空)", bot or "(空)"))

    # ---------------------------------------------------------------- 群号
    tun = load_json(os.path.join(D, "lanya-tunables.json"))
    tg = str(tun.get("GroupId") or "")
    fg = str(cfg.get("fanGroupQQ") or cfg.get("fanGroup") or "")
    add("群号: tunables vs config", bool(tg) and tg == fg,
        "%s / %s" % (tg or "(空)", fg or "(空)"))
    if groups:
        add("群号: 在真实群列表里", fg in groups, fg or "(空)")

    # ---------------------------------------------------------------- 私聊准入
    policy = pc.get("dmPolicy") or ""
    allow_all = bool(pc.get("allowAllUsers"))
    admins = pc.get("adminUsers") or []
    # 按源码：open -> allowAllUsers || adminUsers；allowlist -> 只看 allowFrom
    can_all = (policy == "open" and allow_all) or (policy == "open" and admins)
    add("私聊: 所有好友都能聊", can_all,
        "policy=%s allowAllUsers=%s adminUsers=%s"
        % (policy or "?", allow_all, ",".join(admins) or "无"))

    # 管理员号是否都在 adminUsers 里
    admin_list = cfg.get("adminQQ") or []
    if not isinstance(admin_list, list):
        admin_list = [admin_list]
    missing = [a for a in admin_list if a and a not in admins]
    add("私聊: 管理员都在 adminUsers", not missing,
        "缺 %s" % ",".join(missing) if missing else "齐")

    # ---------------------------------------------------------------- 中间消息
    add("只发最终回复", pc.get("interimMessages") is False,
        "interimMessages=%s" % pc.get("interimMessages"))

    # ---------------------------------------------------------------- 聊天记录
    ch = os.path.join(D, "chats")
    newest = None
    if os.path.isdir(ch):
        for f in os.listdir(ch):
            fp = os.path.join(ch, f)
            if os.path.isfile(fp):
                t = os.path.getmtime(fp)
                if newest is None or t > newest:
                    newest = t
    age = (time.time() - newest) / 60.0 if newest else None
    add("聊天记录在更新", age is not None and age < 30,
        ("%.0f 分钟前" % age) if age is not None else "没有记录")

    # ---------------------------------------------------------------- 发送日志
    sl = os.path.join(D, "sent-log.txt")
    s_age = (time.time() - os.path.getmtime(sl)) / 60.0 if os.path.isfile(sl) else None
    add("发出记录在更新", s_age is not None and s_age < 24 * 60,
        ("%.0f 分钟前" % s_age) if s_age is not None else "没有")

    # ---------------------------------------------------------------- 死变量
    gw = os.path.join(W, "groupwatch.ps1")
    if os.path.isfile(gw):
        s = io.open(gw, encoding="utf-8", errors="ignore").read()
        dead = []
        for v in ("$newCount", "$repliedToMeHere", "$lastOtherId"):
            n_use = len(re.findall(re.escape(v), s))
            if n_use == 1:
                dead.append(v)
        add("groupwatch 没有只赋值不用的变量", not dead,
            ",".join(dead) if dead else "干净")

    # ---------------------------------------------------------------- 重复回复
    if os.path.isfile(gw):
        log = os.path.join(W, "groupwatch.log")
        if os.path.isfile(log):
            replies = []
            try:
                with io.open(log, encoding="utf-8", errors="ignore") as fh:
                    for ln in fh.readlines()[-400:]:
                        m = re.search(r"\] reply: (.+)$", ln)
                        if m:
                            replies.append(m.group(1).strip())
            except Exception:
                pass
            dup = 0
            for a, b in zip(replies, replies[1:]):
                if a and a == b:
                    dup += 1
            add("没有连发同一句", dup == 0,
                "%d 次相邻重复" % dup if dup else "干净")

    # ---------------------------------------------------------------- 输出
    print("  " + "-" * 72)
    bad = 0
    for name, ok, detail in ROWS:
        if not ok:
            bad += 1
        print("  %s %-34s %s" % ("\u2705" if ok else "\u274c", name, detail))
    print("  " + "-" * 72)
    print("  %d 项，有问题 %d" % (len(ROWS), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
