# -*- coding: utf-8 -*-
"""集中配置的读取器。找不到 config/project.json 就用这里的默认值。

    import project_conf
    project_conf.get("panelPort", 19388)
    project_conf.get("daemon.pollSeconds", 60)

规则：
  * 查文件按 _candidates() 的顺序，找到第一个能解析成 dict 的就用；
  * 文件里没写的键由 DEFAULTS 补上，所以 get() 几乎总能拿到值；
  * 读一次就缓存，进程里改文件不会生效（脚本都是一次性的，够用）；
    需要重读就 load(reload=True)。

默认值与 config/project.json 里的值保持一致 —— 配置文件丢了也不会变行为。
本文件保持纯 ASCII，中文目录名用转义写。
"""

import json
import os

# 找不到配置文件时用的值。改这里等于改出货默认值，慎动。
DEFAULTS = {
    "panelPort": 19389,
    "panelStaticPort": 19388,
    "onebotPort": 8720,
    "ttsPort": 9880,
    "panelFile": "panel.html",
    "dataDirName": "\u5927\u80a5\u9c7c",  # "Dafeiyu"
    "daemon": {
        "pollSeconds": 60,
        "headlessTimeoutSec": 360,
        "maxConcurrentAgents": 3,
        "autoUpdateMinutes": 360,
    },
    "bili": {
        "commentMaxLen": 30,
        "watchCandidates": 4,
        "commentRetries": 3,
    },
    "budget": {
        "dailyLimit": 1.0,
    },
}

_CACHE = None


def _candidates():
    """配置文件可能在哪。按优先级排，先命中的赢。

    仓库布局是 scripts/ 和 server/ 各在一层，所以往上数两层才到仓库根；
    安装后的扁平布局则是脚本直接躺在 napcat/ 里，配置在上一层。
    两种都试一遍，路径推导不写死。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    up1 = os.path.dirname(here)
    up2 = os.path.dirname(up1)
    paths = []
    env = os.environ.get("LANYA_PROJECT_CONFIG")
    if env:
        paths.append(env)
    for base in (here, up1, up2):
        paths.append(os.path.join(base, "config", "project.json"))
    paths.append(os.path.join(os.getcwd(), "config", "project.json"))
    seen = []
    for p in paths:
        if p not in seen:
            seen.append(p)
    return seen


def config_path():
    """实际存在的配置文件路径；一个都没有就 None。"""
    for p in _candidates():
        if os.path.isfile(p):
            return p
    return None


def _merge(base, extra):
    """把 extra 合到 base 上（深一层）。不改入参。"""
    out = {}
    for k, v in (base or {}).items():
        out[k] = _merge(v, {}) if isinstance(v, dict) else v
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(reload=False):
    """读配置，返回 dict。带缓存 —— 一个进程只读一次磁盘。"""
    global _CACHE
    if _CACHE is not None and not reload:
        return _CACHE
    data = {}
    for p in _candidates():
        if not os.path.isfile(p):
            continue
        try:
            # utf-8-sig 而不是 utf-8：Windows 记事本、PowerShell 5.1 的
            # Set-Content -Encoding UTF8 都会给文件加 BOM，json.load 遇到 BOM
            # 会直接抛 "Unexpected UTF-8 BOM"，那就静悄悄地退回默认值了 ——
            # 而 .ps1 那边照样读得到，两边行为就不一致。加 sig 两种都能读。
            with open(p, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
        except Exception:
            # 文件坏了就当没有，别让一个手抖的逗号拖死整台机器。
            continue
        if isinstance(loaded, dict):
            data = loaded
            break
    _CACHE = _merge(DEFAULTS, data)
    return _CACHE


def get(key_path, default=None):
    """按点号路径取值：get("daemon.pollSeconds", 60)。取不到就给 default。"""
    node = load()
    for part in str(key_path).split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node


def section(name):
    """取一整块，例如 section("bili") 拿 dict；拿不到给空 dict。"""
    got = get(name, {})
    return got if isinstance(got, dict) else {}


if __name__ == "__main__":
    print("config:", config_path())
    print(json.dumps(load(), ensure_ascii=False, indent=2))
