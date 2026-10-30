# -*- coding: utf-8 -*-
"""集中配置读取器；完全无配置时才使用默认 unified 模式。

    import project_conf
    project_conf.get("panelPort", 19388)
    project_conf.get("daemon.pollSeconds", 60)

规则：
  * 查文件按 _candidates() 的顺序；首个现存候选必须是有效 object；
  * 显式 override 缺失、不可读、JSON 损坏或非 object 都立即拒绝活动；
  * 文件里没写的键由 DEFAULTS 补上，所以 get() 几乎总能拿到值；
  * 读一次就缓存，进程里改文件不会生效（脚本都是一次性的，够用）；
    需要重读就 load(reload=True)。

损坏配置不能降级到较低优先级的 legacy 发送配置。
本文件保持纯 ASCII，中文目录名用转义写。
"""

import json
import os
import sys


class ConfigError(RuntimeError):
    """Configuration is unavailable; callers must stop, never assume legacy."""

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
    "mind": {"opportunityMin": 6},
    "decisions": {"mode": "unified", "agentPreset": ""},
}

_CACHE = None
_CONFIG_PATH = None


def _layout():
    here = os.path.dirname(os.path.abspath(__file__))
    source = os.path.basename(here).lower() in ("scripts", "server")
    project = os.path.dirname(here) if source else here
    return here, project, os.path.dirname(project), source


def work_dir():
    """Workspace shared by napcat/ and the data folder, in either layout."""
    override = os.environ.get("LANYA_WORKDIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return _layout()[2]


def data_dir():
    """Configured character data directory; never guess from private files."""
    return os.path.join(work_dir(), str(get("dataDirName")))


def _candidates():
    """配置文件可能在哪。按优先级排，先命中的赢。

    源码布局优先仓库 config，避免误用相邻私人安装的配置；
    安装后的扁平布局优先 napcat/config，再兼容工作区 config。
    环境变量显式指定的文件永远优先。
    """
    here, project, workspace, source = _layout()
    workspace = work_dir()
    paths = []
    env = os.environ.get("LANYA_PROJECT_CONFIG")
    if env:
        paths.append(os.path.abspath(os.path.expanduser(env)))
    # A source checkout uses its own public defaults. An installed copy uses
    # napcat/config first, then the older workspace/config location. Do not
    # let a neighbouring private installation change a source checkout.
    bases = (project, workspace, here) if source else (here, workspace)
    for base in bases:
        paths.append(os.path.join(base, "config", "project.json"))
    paths.append(os.path.join(os.getcwd(), "config", "project.json"))
    seen = []
    for p in paths:
        p = os.path.abspath(p)
        if p not in seen:
            seen.append(p)
    return seen


def config_path():
    """Path of the valid dictionary actually loaded, or None for defaults."""
    load()
    return _CONFIG_PATH


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
    global _CACHE, _CONFIG_PATH
    if _CACHE is not None and not reload:
        return _CACHE
    # Clear before any read. A failed reload must never leave cached legacy
    # authority available to a later get() call in this process.
    _CACHE = None
    data = {}
    _CONFIG_PATH = None
    override = os.environ.get("LANYA_PROJECT_CONFIG")
    explicit = os.path.abspath(os.path.expanduser(override)) if override else None
    for p in _candidates():
        try:
            # utf-8-sig 而不是 utf-8：Windows 记事本、PowerShell 5.1 的
            # Set-Content -Encoding UTF8 都会给文件加 BOM，json.load 遇到 BOM
            # 会直接抛 "Unexpected UTF-8 BOM"。加 sig 两种都能读。
            with open(p, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
        except FileNotFoundError:
            if p == explicit or os.path.lexists(p):
                raise ConfigError("Project configuration unavailable; activity refused") from None
            continue
        except (OSError, ValueError, UnicodeError):
            # Do not propagate decoder text, filenames or private JSON excerpts.
            raise ConfigError("Project configuration invalid or unreadable; activity refused") from None
        if not isinstance(loaded, dict):
            raise ConfigError("Project configuration must be a JSON object; activity refused")
        data = loaded
        _CONFIG_PATH = p
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
    try:
        load()
        print("project_config_ok")
    except ConfigError:
        print("ConfigError: project configuration unavailable; activity refused", file=sys.stderr)
        sys.exit(1)
