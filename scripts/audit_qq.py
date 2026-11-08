# -*- coding: utf-8 -*-
"""Read-only QQ diagnostics for the selected OneBot backend.

Uses actual backend configuration and bounded HTTP without sending messages,
starting models or echoing account identifiers, tokens or conversation bodies.
HTTP and reverse-WebSocket authentication are independent.
"""
import io
import json
import os
import re
import sys
import time
import urllib.parse
import socket

import qq_backend
from project_conf import work_dir, data_dir

W = os.path.join(work_dir(), "napcat")
D = data_dir()
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


def port_up(port, host='127.0.0.1'):
    if type(port) is not int or not 1 <= port <= 65535:
        return False
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def main():
    print("=" * 76)
    print("  QQ 聊天链路体检")
    print("=" * 76)
    print()

    ROWS.clear()
    try:
        connection = qq_backend.resolve()
    except qq_backend.BackendError:
        add("所选 QQ 后端", False, "配置未确认，没有回退到其他后端")
        for name, ok, detail in ROWS:
            print(name, detail)
        return 1
    pc = plugin_cfg()
    cfg = load_json(os.path.join(D, "lanya-config.json"))
    add("所选 QQ 后端", True, connection.name)
    add("HTTP 访问凭据", bool(connection.token), "只检查是否已配置，不显示内容")
    wcs = [row for row in connection.network.get(connection.spec.ws_clients, []) if row.get('enabled', True)]
    tokens = []
    ports = set()
    ws_valid = True
    for row in wcs:
        try:
            url = row.get('url', '')
            if not isinstance(url, str) or not url or len(url) > 2048:
                raise ValueError()
            parsed = urllib.parse.urlsplit(url)
            if (parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or parsed.scheme not in ('ws', 'wss')
                    or parsed.username is not None or parsed.password is not None or parsed.fragment):
                raise ValueError()
            port = parsed.port or (443 if parsed.scheme == 'wss' else 80)
            if not 1 <= port <= 65535: raise ValueError()
            token = row.get(connection.spec.token_key) or (urllib.parse.parse_qs(parsed.query).get('access_token') or [''])[0]
            if not isinstance(token, str): raise ValueError()
            tokens.append(token); ports.add((parsed.hostname, port))
        except (ValueError, TypeError):
            ws_valid = False
    add("反向 WS 与插件凭据", ws_valid and bool(pc.get('accessToken')) and pc['accessToken'] in tokens,
        "只核对反向连接；不要求与 HTTP 令牌相同")
    for address, label in [(connection.base_url, "OneBot HTTP"), (connection.webui_url, "管理页")]:
        parsed = urllib.parse.urlsplit(address)
        add(label + "端口", port_up(parsed.port or (443 if parsed.scheme == 'https' else 80), parsed.hostname), "端口可达不代表账号已登录")
    add("反向 WS 端口", ws_valid and bool(ports) and all(port_up(port, host) for host, port in ports), "依据当前启用的回环适配器")
    groups = []
    try:
        status = qq_backend.health(connection, expected_account=cfg.get('botQQ'))
        add("后端版本与登录", status['online'] and status['good'] and status['logged_in'] and status['provider_matches'],
            "只读接口核验，实际消息另行确认")
        add("账号与角色配置", bool(cfg.get('botQQ')) and status['account_matches'], "只核对一致性，不显示账号")
        result = qq_backend.call('get_group_list', connection=connection)
        if not isinstance(result.get('data'), list) or any(not isinstance(group, dict) for group in result['data']):
            raise qq_backend.BackendError('Invalid group list')
        groups = [str(group.get('group_id')) for group in result['data']]
        add("核验期间配置一致", qq_backend.resolve().fingerprint == connection.fingerprint, "配置代次及端点保持一致")
    except qq_backend.BackendError:
        add("OneBot 可调用", False, "所选连接未确认，没有发送任何消息")

    # ---------------------------------------------------------------- 群号
    tun = load_json(os.path.join(D, "lanya-tunables.json"))
    tg = str(tun.get("GroupId") or "")
    fg = str(cfg.get("fanGroupQQ") or cfg.get("fanGroup") or "")
    add("群号: tunables vs config", bool(tg) and tg == fg,
        "只核对一致性，不显示群号")
    if groups:
        add("群号: 在真实群列表里", fg in groups, "只读核验")

    # ---------------------------------------------------------------- 私聊准入
    policy = pc.get("dmPolicy") or ""
    allow_all = bool(pc.get("allowAllUsers"))
    admins = pc.get("adminUsers") or []
    # 按源码：open -> allowAllUsers || adminUsers；allowlist -> 只看 allowFrom
    can_all = policy == "open" and allow_all
    add("私聊: 所有好友都能聊", can_all,
        "policy=%s allowAllUsers=%s" % (policy or "?", allow_all))

    # 管理员号是否都在 adminUsers 里
    admin_list = cfg.get("adminQQ") or []
    if not isinstance(admin_list, list):
        admin_list = [admin_list]
    missing = [str(a) for a in admin_list if a and str(a) not in [str(item) for item in admins]]
    add("私聊: 管理员都在 adminUsers", not missing,
        "缺 %d 个" % len(missing) if missing else "齐")

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
