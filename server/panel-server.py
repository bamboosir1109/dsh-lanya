# Lanya panel server - serves the control panel AND its API from ONE origin.
#
# Why this exists: the panel used to live on the works server (19388) and talk to
# the DSH host on 19387. Every write failed with "Failed to fetch":
#   - plain GET: the OPTIONS preflight hit a route that answers 400
#   - POST: the DSH web server does not accept cross-origin writes at all
# Fighting CORS was the wrong direction. Serving the page and the API from the same
# origin removes the whole class of problem - no preflight, no CORS, no proxy.
#
# Static files come from her made/ folder, so existing game links keep working.
#
#   python lanya-panel-server.py            run on 19389
#   python lanya-panel-server.py --port N
#
# ASCII-only source.

import json
import re
import os
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.join(REPO, "\u5c9a\u5a05", "made")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAPCAT = os.path.join(REPO, "napcat")
PERSONA = os.path.join(NAPCAT, "\u5c9a\u5a05\u4eba\u8bbe\u6b63\u6587.md".replace("\u5c9a\u5a05", "\u5c9a\u5a05-"))
# 上面这种拼法容易出错，直接用 os.path.join 拼真实文件名更稳
PERSONA = os.path.join(NAPCAT, "\u5c9a\u5a05" + "-\u4eba\u8bbe\u6b63\u6587.md")
CONFIG  = os.path.join(REPO, "\u5c9a\u5a05", "lanya-config.json")
TUNABLES = os.path.join(REPO, "\u5c9a\u5a05", "lanya-tunables.json")
MEMECFG  = os.path.join(REPO, "\u5c9a\u5a05", "lanya-memes-config.json")
MEME_TAGS = os.path.join(REPO, "\u5c9a\u5a05", "lanya-memes-tags.json")

PROFILE = os.path.join(os.path.expanduser("~"), ".dsh", "profiles", "desktop")
PROFILE_PKG = os.path.join(PROFILE, "package.json")
DSH_CLI = os.path.join(os.environ.get("DSH_ROOT", r"D:\DSH"),
                   "resources", "runtime", "cli", "bin", "dsh.cmd")

SCRIPTS = {
    "groupwatch": "groupwatch.ps1",
    "lifeloop": "lifeloop.ps1",
    "voicedaemon": "voicedaemon.ps1",
    "supervisor": "lanya-supervisor.ps1",
    "napcat-heal": "napcat-heal.ps1",
    "kill-strays": "kill-strays.ps1",
    "settings-guard": "settings-guard.py",
    "state": "lanya-state.ps1",
    "tick": "lanya-tick.ps1",
    # 面板上每个部件都应该有自己的启动按钮，不然显示"0 个"就不知道怎么办。
    "browserd": "browserd.py",
    # tts 不是普通脚本：它跑在单独的 conda 环境里、有自己的工作目录，\n    # 交给下面的 SPECIAL 分支处理。
    "works": "made-server.ps1",
}


def start_napcat_elevated():
    """Launch the NapCat injection with a UAC prompt.

    napimain refuses to do anything without elevation, and killing QQ without
    being able to inject would just leave the user offline. So this is the only
    path that actually works - and it means one UAC click, every time.
    """
    inner = (
        'taskkill /IM QQ.exe /F >nul 2>nul & timeout /t 4 /nobreak >nul & '
        'cd /d "' + os.path.join(NAPCAT, "framework") + '" && '
        'napimain.exe "C:\\software\\QQNT\\QQ.exe" '
        '"<WORKDIR>\\napcat\\framework\\napiloader.dll" '
        '"<WORKDIR>/napcat/framework/nativeLoader.cjs"'
    )
    ps = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-Command",
        "Start-Process cmd.exe -ArgumentList '/c','" + inner.replace("'", "''") + "' -Verb RunAs"
    ]
    p = subprocess.run(ps, capture_output=True, timeout=120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return p.returncode


# 有些部件不能当普通脚本起：解释器不同、工作目录不同、还要带参数。
SPECIAL = {
    "tts": {
        "exe": r"C:\code\anaconda\envs\gptsovits\python.exe",
        "cwd": os.path.join(REPO, "tts", "GPT-SoVITS"),
        "args": ["-X", "utf8", "api_v2.py", "-a", "127.0.0.1", "-p", "9880",
                 "-c", "GPT_SoVITS/configs/tts_infer.yaml"],
    },
}


def run_script(name):
    sp = SPECIAL.get(name)
    if sp:
        if not os.path.isfile(sp["exe"]) and not sp["exe"].lower().endswith(".exe"):
            pass
        p = subprocess.Popen(
            [sp["exe"]] + sp["args"], cwd=sp["cwd"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return p.pid

    script = SCRIPTS.get(name)
    if not script:
        raise ValueError("unknown target: " + name)
    full = os.path.join(NAPCAT, script)
    if not os.path.isfile(full):
        raise FileNotFoundError(script)
    if script.endswith(".py"):
        exe, args = "python", [full]
    else:
        exe = "powershell.exe"
        args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", full]
    child = subprocess.Popen(
        [exe] + args,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return child.pid


# 部件 -> 计划任务名。
#
# 直接起脚本是错的：groupwatch / lifeloop 这些内部会调 dsh.cmd（批处理），
# 每调一次就弹一个控制台窗口——外层隐藏了没用，内层管不到。而且面板起的是
# 第二个副本，跟任务里那个打架。
#
# 触发已有任务才是对的：任务本来就配了 -WindowStyle Hidden，由计划程序拉起，
# 不会有窗口；schtasks /run 也不需要管理员。
TASKS = {
    "groupwatch":  "DSH-GroupWatch",
    "lifeloop":    "DSH-Lanya-Life",
    "voicedaemon": "DSH-Lanya-Voice",
    "browserd":    "DSH-Lanya-Browser",
    "works":       "DSH-Lanya-Works",
    "tts":         "DSH-Lanya-TTS",
}



# ---------------------------------------------------------------------------
# 外部工具：NapCat / QQ / Node / Python / Git / DSH 本身。
# 面板上要能看到它们在不在、什么版本、跑没跑，用不着开命令行去查。
# ---------------------------------------------------------------------------
TOOLS = [
    {"key": "napcat", "name": "NapCat", "kind": "QQ 注入框架",
     "paths": [os.path.join(REPO, "napcat", "framework", "napimain.exe")],
     "port": 8720,
     "note": "注入进 QQ 的那一层。没有它，机器人连不上。"},
    {"key": "qq", "name": "QQ", "kind": "QQ 客户端",
     "paths": [r"C:\software\QQNT\QQ.exe"],
     "proc": "QQ.exe",
     "note": "机器人小号登录的地方。"},
    {"key": "dsh", "name": "DeepSeek Harness", "kind": "宿主程序",
     "paths": [os.path.join(os.environ.get("DSH_ROOT", r"D:\DSH"), "DeepSeek Harness.exe")],
     "port": 19387,
     "note": "整个系统跑在里面。插件都挂在它上面。"},
    {"key": "node", "name": "Node.js", "kind": "运行时",
     "paths": [os.path.join(os.environ.get("DSH_ROOT", r"D:\DSH"),
                           "resources", "runtime", "node", "node.exe"), r"C:\Program Files\nodejs\node.exe"],
     "note": "插件是 JS 写的，靠它跑。"},
    {"key": "python", "name": "Python", "kind": "运行时",
     "paths": [r"C:\Python314\python.exe", r"C:\code\anaconda\python.exe"],
     "note": "面板服务、表情包处理、刷 B 站都靠它。"},
    {"key": "python-tts", "name": "Python (语音环境)", "kind": "专用环境",
     "paths": [r"C:\code\anaconda\envs\gptsovits\python.exe"],
     "note": "语音合成单独一个环境，因为依赖版本和主环境冲突。"},
    {"key": "git", "name": "Git", "kind": "版本控制",
     "paths": [r"C:\code\Git\cmd\git.exe"],
     "note": "拉插件、更新插件用。"},
]


def port_open(port):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.35)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        s.close()


def tool_status(item):
    found = None
    for p in item.get("paths", []):
        if os.path.isfile(p):
            found = p
            break
    version = ""
    if found:
        try:
            # FileVersionInfo 只有 Windows 有；纯 Python 读不到的话就留空。
            version = ""
        except Exception:
            version = ""
    running = False
    if item.get("port"):
        running = port_open(item["port"])
    elif item.get("proc"):
        try:
            out = subprocess.run(["tasklist.exe", "/FI", "IMAGENAME eq " + item["proc"]],
                                 capture_output=True, timeout=10,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            running = item["proc"].lower() in (out.stdout or b"").decode("utf-8", "replace").lower()
        except Exception:
            running = False
    return {
        "key": item["key"],
        "name": item["name"],
        "kind": item.get("kind", ""),
        "note": item.get("note", ""),
        "path": found or "",
        "installed": bool(found),
        "running": running,
        "port": item.get("port"),
    }



# 每个工具的上游仓库，用来查最新版本。查不到不算错——离线时本来就不通。
UPSTREAM = {
    "napcat": ("NapNeko/NapCatQQ", "NapCat"),
    "dsh":    ("deepseek-ai/DeepSeek-Harness", "DeepSeek Harness"),
}


def check_upstream(key):
    """Return the latest release tag from GitHub, or None if unreachable."""
    repo = UPSTREAM.get(key)
    if not repo:
        return None
    url = "https://api.github.com/repos/" + repo[0] + "/releases/latest"
    try:
        import urllib.request, json as _json
        req = urllib.request.Request(url, headers={
            "User-Agent": "lanya-panel",
            "Accept": "application/vnd.github+json",
        })
        with urllib.request.urlopen(req, timeout=12) as r:
            data = _json.loads(r.read().decode("utf-8", "replace"))
        return data.get("tag_name") or data.get("name")
    except Exception:
        return None


def tools_snapshot():
    return [tool_status(x) for x in TOOLS]


def run_task(name):
    task = TASKS.get(name)
    if not task:
        return None
    p = subprocess.run(
        ["schtasks.exe", "/run", "/tn", task],
        capture_output=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    ok = p.returncode == 0
    return {"ok": ok, "task": task,
            "log": (p.stdout or b"").decode("utf-8", "replace").strip()
                   or (p.stderr or b"").decode("utf-8", "replace").strip()}



# ---------------------------------------------------------------------------
# Extra data the multi-page console needs: budget, memes, notes/diary, works.
# ---------------------------------------------------------------------------
LANYA_DIR = os.path.join(REPO, "\u5c9a\u5a05")
BUDGET    = os.path.join(LANYA_DIR, "budget.json")
MEME_ROOT = os.path.join(
    os.path.join(os.path.expanduser("~"), ".dsh", "profiles", "desktop",
                            "node_modules", "dsh-meme", "memes"),
    "dafeiyu-001", "memes")

# 允许页面上浏览的目录。白名单，不接受任意路径 - 这是给浏览器用的接口，
# 不能让它顺着 ../ 读到别处去。
ACTIVITIES = os.path.join(REPO, "\u5c9a\u5a05", "activities.json")
ARCHIVE_ROOT = os.path.join(REPO, "\u5c9a\u5a05-\u5f52\u6863")

FILE_DIRS = {
    "diary":  os.path.join(LANYA_DIR, "diary"),
    "notes":  os.path.join(LANYA_DIR, "notes"),
    "made":   os.path.join(LANYA_DIR, "made"),
    "people": os.path.join(LANYA_DIR, "people"),
    "archive": os.path.join(REPO, "\u5c9a\u5a05-\u5f52\u6863"),
}

IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def safe_name(name):
    """Reject anything that could escape its directory."""
    if not name:
        return None
    base = os.path.basename(str(name))
    if base != str(name) or base.startswith("."):
        return None
    return base


def list_memes(tag=None):
    out = []
    if not os.path.isdir(MEME_ROOT):
        return out
    tags = [tag] if tag else sorted(os.listdir(MEME_ROOT))
    for tg in tags:
        d = os.path.join(MEME_ROOT, tg)
        if not os.path.isdir(d):
            continue
        files = [f for f in sorted(os.listdir(d)) if f.lower().endswith(IMG_EXT)]
        if files:
            out.append({"tag": tg, "count": len(files), "files": files})
    return out


def list_dir_files(key, with_preview=False, limit=0):
    # 归档目录按批次分子文件夹，得递归；名字带上子目录才分得清。
    if key == "archive":
        out = []
        root = FILE_DIRS["archive"]
        for dirpath, _dn, filenames in os.walk(root):
            rel = os.path.relpath(dirpath, root)
            for f in filenames:
                # 归档里混着会话日志（session.v4.jsonl.zstd）——那是聊天
                # 隐私数据，不是作品，不能列在页面上给人翻。
                if f.endswith(".zstd") or "session.v4" in f or f.endswith(".lock.json"):
                    continue
                p = os.path.join(dirpath, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                nm = f if rel == "." else (rel.replace("\\", "/") + "/" + f)
                out.append({"name": nm, "size": st.st_size,
                            "mtime": int(st.st_mtime)})
        out.sort(key=lambda x: x["mtime"], reverse=True)
        return out[:800]

    """List files in one of the whitelisted folders.

    with_preview reads just the head of each file so the page can show a snippet
    instead of forcing a click on every entry. Reading all 8000 notes would be
    slow, so limit caps how many previews are produced.
    """
    root = FILE_DIRS.get(key)
    if not root or not os.path.isdir(root):
        return []
    items = []
    names = sorted(os.listdir(root), reverse=True)
    if limit and limit > 0:
        names = names[:limit]
    for f in names:
        p = os.path.join(root, f)
        if not os.path.isfile(p):
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        # 面板自己的快照（lanya-*.json / lanya-persona.txt）不是她的作品，
        # 列在作品页里只会让人更看不懂。
        if f.startswith("lanya-") or f.startswith("theme-"):
            continue
        row = {"name": f, "size": st.st_size, "mtime": int(st.st_mtime)}
        if with_preview:
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    head = fh.read(260)
                row["preview"] = " ".join(head.split())[:160]
            except Exception:
                row["preview"] = ""
        items.append(row)
    return items



# ---------------------------------------------------------------------------
# B 站插件（dsh-bili-scroll）的状态和 duty.md 编辑。
#
# 这个插件没有 HTTP 接口，它的「值得调」的东西只有两样：
#   duty.md   它要做什么 —— 改这个真的会影响行为
#   duty.log  它最近干了什么
#
# 所以页面不去伪造一堆看着能调其实没用的开关，只暴露这两个真的有效的。
# ---------------------------------------------------------------------------
BILI_HOME = os.path.join(os.path.expanduser("~"), ".dsh", "bili-duty")
BILI_DUTY = os.path.join(BILI_HOME, "duty.md")
BILI_LOG  = os.path.join(BILI_HOME, "duty.log")
BILI_SEEN = os.path.join(BILI_HOME, "seen.md")


def tail_file(path, lines=40, limit=20000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
        rows = data.splitlines()
        return "\n".join(rows[-lines:])[:limit]
    except Exception as e:
        return ""


# duty.log 里的启动噪音——这些每重启一次写一遍，跟刷了什么无关。
_BILI_NOISE = (
    "已挂载", "插件加载自", "私信监听", "值班轮用", "schemastery",
    "dsh-tools", "信任栅栏", "开始一轮", "轮次结束",
)


def bili_activity(log_text):
    """把 duty.log 分成「真活动」和「启动噪音」。"""
    real, noise = [], []
    for ln in (log_text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if any(n in ln for n in _BILI_NOISE):
            noise.append(ln)
        else:
            real.append(ln)
    return real, noise


def _md_rows(text):
    """把一段 markdown 表格拆成行（去掉表头和分隔线）。"""
    rows = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln.startswith("|"):
            continue
        if set(ln) <= set("|-: "):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        rows.append(cells)
    return rows


# ---------------------------------------------------------------------------
# B 站写操作：直接在这里发请求，不转子进程。
#
# 之前每删一条评论都 spawn 一个 venv python 跑 bili_account.py，光启动就
# 一两秒；再加上整页重载拉封面，删一条要等好几秒。这几件事都能就地做完。
# ---------------------------------------------------------------------------
_BILI_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_AID_CACHE_FILE = os.path.join(os.path.expanduser("~"), ".dsh", "bili-duty",
                               "aid-cache.json")


def _bili_cred():
    try:
        with open(os.path.join(os.path.expanduser("~"), ".dsh", "bili-duty",
                               "bili_credential.json"), "r",
                  encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _bili_cookie(c):
    parts = []
    for k, v in (("SESSDATA", "sessdata"), ("bili_jct", "bili_jct"),
                 ("buvid3", "buvid3"), ("DedeUserID", "dedeuserid")):
        if c.get(v):
            parts.append("%s=%s" % (k, c[v]))
    return "; ".join(parts)


def _bili_get(url, c):
    import urllib.request as _u
    req = _u.Request(url, headers={
        "user-agent": _BILI_UA,
        "referer": "https://www.bilibili.com/",
        "cookie": _bili_cookie(c)})
    with _u.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _bili_post(url, form, c):
    import urllib.parse as _up
    import urllib.request as _u
    body = _up.urlencode(form).encode("utf-8")
    req = _u.Request(url, data=body, headers={
        "user-agent": _BILI_UA,
        "referer": "https://www.bilibili.com/",
        "origin": "https://www.bilibili.com",
        "content-type": "application/x-www-form-urlencoded",
        "cookie": _bili_cookie(c)})
    with _u.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _aid_of(bvid):
    """BV -> aid，带本地缓存。同一个视频只查一次。"""
    try:
        with open(_AID_CACHE_FILE, "r", encoding="utf-8") as fh:
            cache = json.load(fh)
    except Exception:
        cache = {}
    if cache.get(bvid):
        return cache[bvid]
    try:
        d = _bili_get("https://api.bilibili.com/x/web-interface/view?bvid="
                      + bvid, _bili_cred())
        aid = (d.get("data") or {}).get("aid")
    except Exception:
        return None
    if aid:
        cache[bvid] = aid
        try:
            with open(_AID_CACHE_FILE, "w", encoding="utf-8") as fh:
                json.dump(cache, fh)
        except Exception:
            pass
    return aid


def bili_delete_comment(bvid, rpid):
    """删自己在某条视频下的评论。返回 (ok, 说明)。"""
    c = _bili_cred()
    if not c.get("bili_jct"):
        return False, "没有 bili_jct，重扫一次码"
    aid = _aid_of(bvid)
    if not aid:
        return False, "拿不到 aid"
    try:
        d = _bili_post("https://api.bilibili.com/x/v2/reply/del",
                       {"oid": aid, "rpid": rpid, "type": 1,
                        "csrf": c.get("bili_jct", "")}, c)
    except Exception as e:
        return False, str(e)[:120]
    if d.get("code") == 0:
        return True, "删掉了"
    return False, "code=%s %s" % (d.get("code"), d.get("message"))


def bili_follow(uid, act=1):
    c = _bili_cred()
    if not c.get("bili_jct"):
        return False, "没有 bili_jct"
    try:
        d = _bili_post("https://api.bilibili.com/x/relation/modify",
                       {"fid": uid, "act": act, "re_src": 11,
                        "csrf": c.get("bili_jct", "")}, c)
    except Exception as e:
        return False, str(e)[:120]
    if d.get("code") == 0:
        return True, "关注了" if act == 1 else "取关了"
    if d.get("code") == 22003:
        return True, "本来就关注着"
    return False, "code=%s %s" % (d.get("code"), d.get("message"))


def bili_cover_urls(bvids):
    """给一批 BV 号查封面图 URL，缓存在本地。

    只查没缓存的。B 站这个接口不需要登录。
    """
    cache = {}
    try:
        with open(COVER_CACHE_FILE, "r", encoding="utf-8") as fhc:
            cache = json.load(fhc)
    except Exception:
        cache = {}

    todo = [b for b in bvids if b and b not in cache]
    for bv in todo[:12]:           # 一次别查太多，页面会卡
        try:
            import urllib.request as _urc
            url = ("https://api.bilibili.com/x/web-interface/view?bvid=" + bv)
            req = _urc.Request(url, headers={
                "user-agent": "Mozilla/5.0",
                "referer": "https://www.bilibili.com/"})
            with _urc.urlopen(req, timeout=12) as rc:
                d = json.loads(rc.read().decode("utf-8"))
            pic = ((d.get("data") or {}).get("pic") or "")
            cache[bv] = pic
        except Exception:
            cache[bv] = ""

    try:
        os.makedirs(os.path.dirname(COVER_CACHE_FILE), exist_ok=True)
        with open(COVER_CACHE_FILE, "w", encoding="utf-8") as fhc2:
            json.dump(cache, fhc2, ensure_ascii=False)
    except Exception:
        pass
    return {b: cache.get(b, "") for b in bvids}


def bili_seen_detail(seen_text):
    """从判重表里拆出「评论记录」和「看过的视频」。

    真实格式（插件写的）：
      评论：| BV | rpid | root | 时间 | 正文前 20 字 |
      视频：| 日期 | BV | 标题 | UP | 状态 |
    表头行（第一行含 BV 字样）要跳过。
    """
    comments, videos = [], []
    if not seen_text:
        return comments, videos

    seg = seen_text.split("## 评论记录", 1)
    if len(seg) == 2:
        body = seg[1].split("## 看过的视频")[0]
        for cells in _md_rows(body):
            if not cells or cells[0].lower() == "bv" or "BV" == cells[0]:
                continue              # 表头
            if len(cells) < 5 or not cells[0].startswith("BV"):
                continue
            comments.append({"bv": cells[0],
                             "rpid": cells[1],
                             "time": cells[3],
                             "text": cells[4]})

    seg2 = seen_text.split("## 看过的视频", 1)
    if len(seg2) == 2:
        for cells in _md_rows(seg2[1]):
            if not cells:
                continue
            if cells[0] in ("日期", "date"):
                continue              # 表头
            if len(cells) >= 5:
                videos.append({"date": cells[0], "bv": cells[1],
                               "title": cells[2], "up": cells[3],
                               "status": cells[4]})
            elif len(cells) >= 2:
                videos.append({"date": cells[0], "bv": cells[1],
                               "title": "", "up": "", "status": ""})
            elif cells[0].startswith("BV"):
                videos.append({"date": "", "bv": cells[0],
                               "title": "", "up": "", "status": ""})
    return comments, videos


def bili_snapshot():
    loaded = os.path.isdir(BILI_HOME)
    duty = ""
    if os.path.isfile(BILI_DUTY):
        try:
            with open(BILI_DUTY, "r", encoding="utf-8", errors="replace") as fh:
                duty = fh.read()
        except Exception:
            duty = ""
    # 不能只读尾巴：评论表在文件开头，视频表在末尾。
    # 之前 tail_file(60) 把评论全截掉了，页面上「她发过的评论」一直是空的。
    try:
        with open(BILI_SEEN, "r", encoding="utf-8", errors="replace") as _fhs:
            seen = _fhs.read()
    except Exception:
        seen = ""
    log = tail_file(BILI_LOG, 30)
    # 判重表里「看过的视频」那段有内容 = 刷过
    watched = 0
    try:
        seg = seen.split("## 看过的视频", 1)
        if len(seg) == 2:
            watched = len([x for x in seg[1].splitlines() if x.strip()])
    except Exception:
        pass
    real, noise = bili_activity(log)
    comments, videos = bili_seen_detail(seen)
    return {
        "installed": loaded,
        "home": BILI_HOME,
        "dutyChars": len(duty),
        "hasDuty": bool(duty.strip()),
        "watched": len(videos),
        "log": "\n".join(real[-40:]),       # 只给真活动
        "noiseCount": len(noise),
        "noise": "\n".join(noise[-5:]),
        "seen": seen,
        "comments": comments,
        "videos": videos,
        "covers": bili_cover_urls([v.get("bv", "") for v in videos]),
    }



# ---------------------------------------------------------------------------
# 统一的记忆视图。
#
# 她的记忆现在散在几处：memento 的审计流水（谁在什么时候写了什么）、
# 人物档案、日记、随笔。分开看就是四套东西，合成一个视图才能管。
# 这里只读，不改——写记忆要走 memento 自己的流程，别绕过它。
# ---------------------------------------------------------------------------
MEMENTO_DB = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-memento", "memory.db")


def memento_rows(sql, args=(), limit=200):
    try:
        import sqlite3
        uri = "file:%s?mode=ro" % MEMENTO_DB.replace("\\", "/")
        con = sqlite3.connect(uri, uri=True, timeout=5)
        cur = con.cursor()
        cur.execute(sql, args)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()][:limit]
        con.close()
        return rows
    except Exception:
        return []


def memory_overview():
    # 过滤掉 snapshot：它们每轮都写一条、内容全是同一句话，341 条把真正
    # 有价值的记忆操作淹没了。只在实在没有其它记录时才退回显示它们。
    audit = memento_rows(
        "SELECT seq, ts, action, track, scope, text, outcome FROM audit "
        "WHERE action <> 'snapshot' ORDER BY seq DESC LIMIT 60")
    snapshot_count = 0
    if not audit:
        rows = memento_rows("SELECT COUNT(*) AS n FROM audit WHERE action = 'snapshot'")
        snapshot_count = rows[0]["n"] if rows else 0
        audit = memento_rows(
            "SELECT seq, ts, action, track, scope, text, outcome FROM audit "
            "ORDER BY seq DESC LIMIT 5")
    props = memento_rows(
        "SELECT id, kind, track, scope, text, status, created_at FROM proposals "
        "ORDER BY created_at DESC LIMIT 30")
    # 真正的记忆条目——之前页面只查 audit（流水），entries 一条都没查过，
    # 所以永远显示"还没有写进长期记忆"。
    try:
        entries = memento_rows(
            "SELECT id, track, scope, text, tags, created_at, recall_count "
            "FROM entries ORDER BY created_at DESC LIMIT 200")
    except Exception:
        entries = []
    active = memento_rows("SELECT COUNT(*) AS n FROM entries")
    counts = {
        "audit": len(memento_rows("SELECT seq FROM audit")),
        "active": (active[0]["n"] if active else 0),
        "proposals": len(memento_rows("SELECT id FROM proposals")),
        "people": len(list_dir_files("people")),
        "diary": len(list_dir_files("diary")),
        "notes": len(list_dir_files("notes")),
    }
    return {"counts": counts, "audit": audit, "proposals": props, "entries": entries, "snapshotOnly": snapshot_count > 0}


# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
# 表情包的多来源支持（v2：补上 meme-packs 里那两个包）
#
# 之前只扫了 dafeiyu-001 一个包，所以 815 张里只显示 107 张。
# 现在扫三个位置：
#   .dsh/meme-packs/<pack>/memes/<tag>/   插件市场装的包（official-001 / whale-girl-emoji）
#   dsh-meme/memes/dafeiyu-001/memes/<tag>/  大肥鱼那个包
#   岚娅/memes/...                        她自己的
#
# ASCII-only 的标识符；注释里用中文没问题，这个文件是 python 不是 ps1。

import os

IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

DSH_ROOT = os.path.join(os.path.expanduser("~"), ".dsh")
PACKS = os.path.join(DSH_ROOT, "meme-packs")
DSH_MEME = os.path.join(
    DSH_ROOT, "profiles", "desktop", "node_modules", "dsh-meme", "memes",
    "dafeiyu-001", "memes")
LANYA_MEMES = os.path.join(REPO, "\u5c9a\u5a05", "memes")

# 这些子目录是临时/回收区，不算分类
SKIP_DIRS = {"rejected", "_scratch", "_stage", "_conv", "_tmpcls", "_archive"}

# pack id -> 中文显示名（manifest 里有 name，但直接读文件更省事，读不到就用 id）
PACK_LABELS = {
    "official-001": "官方表情包",
    "whale-girl-emoji": "鲸鱼娘",
    "dafeiyu-001": "大肥鱼",
}


def _imgs(folder, recursive=False):
    out = []
    if not os.path.isdir(folder):
        return out
    if recursive:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if f.lower().endswith(IMG_EXT) and not f.startswith("."):
                    out.append(os.path.relpath(os.path.join(root, f), folder))
    else:
        for f in os.listdir(folder):
            p = os.path.join(folder, f)
            if os.path.isfile(p) and f.lower().endswith(IMG_EXT) and not f.startswith("."):
                out.append(f)
    return sorted(out)


def _pack_label(pack_id):
    label = PACK_LABELS.get(pack_id)
    if label:
        return label
    try:
        import json
        with open(os.path.join(PACKS, pack_id, "manifest.json"), "r", encoding="utf-8") as fh:
            return json.load(fh).get("name") or pack_id
    except Exception:
        return pack_id


def sources():
    """(key, 显示名, 根目录, 是否按标签分目录)"""
    out = []

    # 1. meme-packs 下的每个包
    if os.path.isdir(PACKS):
        for pid in sorted(os.listdir(PACKS)):
            memes_dir = os.path.join(PACKS, pid, "memes")
            if os.path.isdir(memes_dir) and _imgs_count(memes_dir) > 0:
                out.append((pid, _pack_label(pid), memes_dir, True))

    # 2. 大肥鱼那个包（在插件目录下，不在 meme-packs）
    if os.path.isdir(DSH_MEME):
        out.append(("plugin", "大肥鱼", DSH_MEME, True))

    # 3. 她自己的
    out.append(("stolen", "收回来的", os.path.join(LANYA_MEMES, "stolen"), True))
    out.append(("mine", "自己做的", LANYA_MEMES, False))
    out.append(("firefly", "firefly 那批", os.path.join(LANYA_MEMES, "firefly-2026-10-16"), False))
    return out


def _imgs_count(folder):
    n = 0
    for root, dirs, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(IMG_EXT) and not f.startswith("."):
                n += 1
    return n


def meme_groups():
    groups = []
    for key, label, root, tagged in sources():
        if not os.path.isdir(root):
            continue
        if tagged:
            for tag in sorted(os.listdir(root)):
                d = os.path.join(root, tag)
                if not os.path.isdir(d) or tag in SKIP_DIRS:
                    continue
                files = _imgs(d)
                if files:
                    groups.append({"source": key, "name": label, "tag": tag,
                                   "count": len(files), "files": files})
        else:
            files = _imgs(root)
            if files:
                groups.append({"source": key, "name": label, "tag": label,
                               "count": len(files), "files": files})
    return groups


_ACT_LABEL = {
    "groupwatch": ("\u81ea\u5df1\u6c34\u7fa4", "\u76ef\u7740\u7c89\u4e1d\u7fa4\uff0c\u89c9\u5f97\u8be5\u8bf4\u8bdd\u5c31\u63d2\u4e00\u53e5"),
    "lifeloop": ("\u81ea\u5df1\u8fc7\u65e5\u5b50", "\u6bcf 15 \u5206\u949f\u9192\u4e00\u6b21\uff0c\u81ea\u5df1\u627e\u70b9\u4e8b\u505a"),
    "voicedaemon": ("\u53d1\u8bed\u97f3", "\u628a\u8981\u8bf4\u7684\u8bdd\u5408\u6210\u8bed\u97f3\u53d1\u51fa\u53bb"),
    "tts": ("\u8bed\u97f3\u5f15\u64ce", "\u672c\u5730\u5408\u6210\u58f0\u97f3"),
    "browserd": ("\u4e0a\u7f51\u901b", "\u7528\u6d4f\u89c8\u5668\u770b\u7f51\u9875\u3001\u8bfb\u6587\u7ae0"),
    "works": ("\u521b\u4f5c", "\u5199\u968f\u7b14\u3001\u753b\u4e1c\u897f\u3001\u505a\u5c0f\u9875\u9762"),
    "napcat": ("\u63a5 QQ", "QQ \u90a3\u4e00\u5c42\uff0c\u6ca1\u5b83\u8fde\u4e0d\u4e0a"),
    "headless": ("\u5e72\u6d3b", "\u4e34\u65f6\u7b97\u4e1c\u897f\u7528"),
}


def activity_suggestions(components, acts):
    """已装但还没列进活动的部件，给个建议。"""
    have = set((a.get("bound") or "") for a in acts)
    out = []
    for c in components:
        tag = c.get("tag") or ""
        if tag in have or tag not in _ACT_LABEL:
            continue
        name, desc = _ACT_LABEL[tag]
        out.append({"tag": tag, "name": name, "desc": desc,
                    "running": int(c.get("count") or 0) > 0})
    return out

def _onebot_token():
    """From onebot11.json, return the HTTP server token."""
    try:
        cfgp = os.path.join(NAPCAT, "framework", "config", "onebot11.json")
        with open(cfgp, "r", encoding="utf-8") as fh9:
            cfg9 = json.load(fh9)
        for x in (cfg9.get("network", {}).get("httpServers") or []):
            if x.get("token"):
                return x["token"]
    except Exception:
        pass
    return ""


def _extra_tags():
    try:
        with open(MEME_TAGS, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def meme_groups_with_tags():
    """把基础分组按多标签表展开：一张图挂在几个标签下就算几次。"""
    base = meme_groups()
    extra = _extra_tags()
    if not extra:
        return base

    # 建立 "文件名 -> [额外标签]" 索引，key 用 标签/文件名 或裸文件名
    by_name = {}
    for k, tags in extra.items():
        nm = k.split("/")[-1]
        by_name.setdefault(nm, set()).update(tags or [])

    groups = {}
    for g in base:
        r = groups.setdefault((g["source"], g["tag"]),
                              {"source": g["source"], "name": g["name"],
                               "tag": g["tag"], "count": 0, "files": []})
        for f in g["files"]:
            r["files"].append(f)
            r["count"] += 1

    # 再按额外标签建一份分组
    for g in base:
        for f in g["files"]:
            for t in by_name.get(f, ()):
                if t == g["tag"]:
                    continue
                r = groups.setdefault((g["source"], t),
                                      {"source": g["source"], "name": g["name"],
                                       "tag": t, "count": 0, "files": []})
                r["files"].append(f)
                r["count"] += 1

    # 同名标签合并：官方包和收回来的都有 happy，分成两行看着乱，
    # 合成一行「happy 14」才像能用的标签。
    merged = {}
    for r in groups.values():
        key = r["tag"]
        m = merged.get(key)
        if m is None:
            m = merged[key] = {"source": "merged", "name": r["tag"], "tag": r["tag"],
                               "count": 0, "files": [], "refs": {},
                               "sources": [r["source"]]}
        for f in r["files"]:
            if f not in m["files"]:
                m["files"].append(f)
                m["refs"][f] = r["source"]
        if r["source"] not in m["sources"]:
            m["sources"].append(r["source"])
    out = []
    for m in merged.values():
        m["files"] = sorted(set(m["files"]))
        m["count"] = len(m["files"])
        out.append(m)
    # 常用情绪排前面，别的按字数
    order = ["happy","sad","angry","speechless","cute","shy","surprise",
             "confused","refuse","pray","cheer","thanks","sleepy","lazy",
             "hungry","work","money","scared","cool","other"]
    def k(r):
        try:
            return (0, order.index(r["tag"]))
        except ValueError:
            return (1, r["tag"])
    out.sort(key=k)
    return out


def meme_path(source, tag, name):
    """Resolve a meme to a real file path, or None. Used by both the image
    endpoint and delete - one resolver means they cannot disagree."""
    if not source or not name:
        return None
    base = os.path.basename(str(name))
    if base != str(name) or base.startswith("."):
        return None
    if source == "merged":
        # 合并同名标签后 source 是 "merged"，真实文件分散在各个源里。
        # 按文件名把所有源搜一遍。
        for s0 in sources():
            _k, _l, root, tagged0 = s0
            cand = os.path.join(root, tag, base) if tagged0 else os.path.join(root, base)
            if os.path.isfile(cand):
                return cand
        return None

    src = next((s for s in sources() if s[0] == source), None)
    if not src:
        return None
    _key, _label, root, tagged = src
    if tagged:
        tagbase = os.path.basename(str(tag or ""))
        if tagbase != str(tag or ""):
            return None
        full = os.path.join(root, tagbase, base)
    else:
        full = os.path.join(root, base)
    return full if os.path.isfile(full) else None


# -*- coding: utf-8 -*-
"""定时任务：提醒清单 + 系统计划任务，都在页面上看得到。"""
import io
import json
import os
import subprocess

LANYA_DIR_ = os.path.join(REPO, "\u5c9a\u5a05")
REMINDERS_ = os.path.join(LANYA_DIR_, "reminders.json")

# 插件调度器已经接管的那些，在页面上标出来，免得用户以为重复的也需要留
COVERED_BY_PLUGIN = {
    "DSH-Reminders": "插件每 30 秒查提醒",
    "DSH-Lanya-Budget": "插件每 5 分钟算花销",
    "DSH-Lanya-Chats": "插件每 10 分钟导记录",
    "DSH-Lanya-Steal": "插件每 10 分钟收表情",
    "DSH-Lanya-Greet": "插件每 60 分钟问好",
    "NapCat-DSH-Bridge": "插件每 10 分钟查 QQ",
}


def reminders_snapshot():
    """她自己的提醒清单（文本调用写进去的那种）。"""
    try:
        with io.open(REMINDERS_, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for i, r in enumerate(data if isinstance(data, list) else []):
        if not isinstance(r, dict):
            continue
        out.append({
            "i": i,
            "text": r.get("text", ""),
            "at": r.get("at", ""),
            "daily": r.get("daily", ""),
            "target": r.get("target", ""),
            "mention": r.get("mention", ""),
            "image": r.get("image", ""),
        })
    return out


def scheduled_tasks():
    """Windows 计划任务里和岚娅有关的那些。"""
    try:
        p = subprocess.run(
            ["schtasks.exe", "/query", "/fo", "CSV", "/v"],
            capture_output=True, timeout=40,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = (p.stdout or b"").decode("mbcs", "replace")
    except Exception:
        return []
    # 中文 Windows 下 schtasks 的表头是中文，DictReader 靠列名取不到，
    # 所以按位置取。CSV 顺序：TaskName, Next Run Time, Status, Last Run Time,
    # Last Result, ... Task To Run 在后面。
    import csv as _csv
    rows = []
    try:
        rows = list(_csv.reader(io.StringIO(text)))
    except Exception:
        return []
    if len(rows) < 2:
        return []
    seen = set()
    out = []
    for r in rows[1:]:
        if not r:
            continue
        # 列顺序（中文 Windows /fo CSV /v）:
        #   0 主机名  1 任务名  2 下次运行  3 模式  4 登录状态
        #   5 上次运行  6 上次结果  7 创建者  8 要运行的任务  11 计划任务状态
        # 之前按第 0 列取名字，拿到的是主机名，所以一个都匹配不上。
        name = (r[1] if len(r) > 1 else "").strip()
        if name.startswith("\\"):
            name = name[1:]
        if not name or name in seen:
            continue
        if not (name.startswith("DSH-") or name.startswith("NapCat")):
            continue
        seen.add(name)
        st = (r[11] if len(r) > 11 else "").strip() or (r[4] if len(r) > 4 else "").strip()
        out.append({
            "name": name,
            "next": (r[2] if len(r) > 2 else "").strip(),
            "status": st,
            "last": (r[5] if len(r) > 5 else "").strip(),
            "result": (r[6] if len(r) > 6 else "").strip(),
            "action": (r[8] if len(r) > 8 else "").strip()[:120],
            "plugin": COVERED_BY_PLUGIN.get(name, ""),
        })
    out.sort(key=lambda x: x["name"])
    return out


class Handler(SimpleHTTPRequestHandler):
    server_version = "LanyaPanel/1.0"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        pass   # 静默，别刷控制台

    def end_headers(self):
        # 页面和样式绝不能被缓存。
        # 之前改完代码用户刷新还是旧页面——DSH 内置视图的缓存很难清，
        # 与其让他到处找 "清缓存"，不如直接告诉浏览器别存。
        p = urlparse(self.path).path.lower()
        if p.endswith(".html") or p.endswith(".css") or p.endswith(".js"):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    # ---- helpers -------------------------------------------------------
    def _cors(self):
        # 页面可能来自 19388（作品站，计划任务常驻），接口在这里（19389）。
        # 跨端口就必须自己给 CORS 头，否则浏览器连 OPTIONS 预检都过不去。
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
        self.send_header("access-control-allow-headers", "content-type")
        self.send_header("access-control-max-age", "86400")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("content-length", "0")
        self.end_headers()

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        try:
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n) if n > 0 else b""
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    # ---- routes --------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/bili/verify":
            # Ask Bilibili whether a comment we recorded is actually public yet.
            try:
                import subprocess as _spv
                q = parse_qs(urlparse(self.path).query)
                bv = (q.get("bv") or [""])[0].strip()
                txt = (q.get("text") or [""])[0]
                if not bv:
                    return self._json(400, {"ok": False, "error": "missing bv"})
                script = os.path.join(NAPCAT, "bili_onerun.py")
                _venv = os.path.join(os.path.expanduser("~"), ".dsh", "bili-duty",
                                     ".venv", "Scripts", "python.exe")
                _py = _venv if os.path.isfile(_venv) else "python"
                _env = dict(os.environ)
                _env["PYTHONUTF8"] = "1"
                _env["PYTHONIOENCODING"] = "utf-8"
                import base64 as _b64v
                _tb = _b64v.b64encode(txt.encode("utf-8")).decode("ascii")
                # text=True 会用系统编码（中文 Windows 是 GBK）解码子进程输出，
                # 而子进程按 PYTHONUTF8 写的是 UTF-8 —— 解出来是空的。
                r = _spv.run([_py, script, "--verify", bv, "--text-b64", _tb],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=120,
                             cwd=NAPCAT, env=_env,
                             creationflags=getattr(_spv, "CREATE_NO_WINDOW", 0))
                out = (r.stdout or "").strip()
                st = out.find("{")
                data = json.loads(out[st:]) if st >= 0 else {"state": "unknown"}
                return self._json(200, {"ok": True, "result": data})
            except Exception as e:
                return self._json(200, {"ok": False, "error": str(e)[:200]})

        if path == "/api/bili/cover":
            # B站的图床要 Referer，浏览器从本地页面直接 img 会被挡（403）。
            # 走服务端代理一次，带上正确的头。
            q = parse_qs(urlparse(self.path).query)
            bv = (q.get("bv") or [""])[0].strip()
            if not bv:
                return self._json(400, {"ok": False, "error": "missing bv"})
            try:
                import urllib.request as _urv
                urls = bili_cover_urls([bv])
                pic = urls.get(bv) or ""
                if not pic:
                    return self._json(404, {"ok": False, "error": "no cover"})
                if pic.startswith("//"):
                    pic = "https:" + pic
                req = _urv.Request(pic, headers={
                    "user-agent": "Mozilla/5.0",
                    "referer": "https://www.bilibili.com/"})
                with _urv.urlopen(req, timeout=20) as rv:
                    data = rv.read()
                self.send_response(200)
                self.send_header("content-type", "image/jpeg")
                self.send_header("content-length", str(len(data)))
                self.send_header("cache-control", "max-age=3600")
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)[:200]})

        if path == "/api/persona":
            try:
                with open(PERSONA, "r", encoding="utf-8") as fh:
                    return self._json(200, {"ok": True, "text": fh.read()})
            except Exception as e:
                return self._json(200, {"ok": False, "text": "", "error": str(e)})
        if path == "/api/schedule":
            return self._json(200, {
                "ok": True,
                "reminders": reminders_snapshot(),
                "tasks": scheduled_tasks(),
            })

        if path == "/api/memory":
            return self._json(200, {"ok": True, "memory": memory_overview()})

        if path == "/api/bili/login/credential":
            # 凭证在就算登录过——bili-qr.json 只是"这一轮扫码"的临时状态，
            # 用户扫完以后它会一直停在 waiting，不能拿它判断登没登录。
            cred = os.path.join(os.path.expanduser("~"), ".dsh", "bili_credential.json")
            ok = False
            uid = ""
            if os.path.isfile(cred):
                try:
                    with open(cred, "r", encoding="utf-8") as fhc:
                        cc = json.load(fhc)
                    ok = bool(cc.get("sessdata")) and bool(cc.get("bili_jct"))
                    uid = str(cc.get("dedeuserid") or "")
                except Exception:
                    pass
            return self._json(200, {"ok": True, "loggedIn": ok, "uid": uid})

        if path == "/api/bili/login/status":
            qr = os.path.join(NAPCAT, "bili-qr.png")
            st = os.path.join(NAPCAT, "bili-qr.json")
            info = {}
            if os.path.isfile(st):
                try:
                    with open(st, "r", encoding="utf-8") as fh2:
                        info = json.load(fh2)
                except Exception:
                    info = {}
            return self._json(200, {"ok": True,
                                    "hasImage": os.path.isfile(qr),
                                    "mtime": int(os.path.getmtime(qr)) if os.path.isfile(qr) else 0,
                                    "state": info})

        if path == "/api/qq/friends":
            try:
                import urllib.request as _ur7
                req = _ur7.Request("http://127.0.0.1:8720/get_friend_list",
                                   data=b"{}",
                                   headers={"content-type": "application/json",
                                            "authorization": "Bearer " + _onebot_token()},
                                   method="POST")
                with _ur7.urlopen(req, timeout=30) as r7:
                    d7 = json.loads(r7.read().decode("utf-8"))
                return self._json(200, {"ok": True, "friends": d7.get("data") or []})
            except Exception as e:
                return self._json(200, {"ok": False, "error": str(e)[:200]})

        if path == "/api/qq/groups":
            try:
                import urllib.request as _ur8
                req = _ur8.Request("http://127.0.0.1:8720/get_group_list",
                                   data=b"{}",
                                   headers={"content-type": "application/json",
                                            "authorization": "Bearer " + _onebot_token()},
                                   method="POST")
                with _ur8.urlopen(req, timeout=30) as r8:
                    d8 = json.loads(r8.read().decode("utf-8"))
                return self._json(200, {"ok": True, "groups": d8.get("data") or []})
            except Exception as e:
                return self._json(200, {"ok": False, "error": str(e)[:200]})

        if path == "/api/napcat/profile":
            prof_file = os.path.join(NAPCAT, "qq-profile.json")
            saved = {}
            if os.path.isfile(prof_file):
                try:
                    with open(prof_file, "r", encoding="utf-8") as fhp:
                        saved = json.load(fhp)
                except Exception:
                    saved = {}
            try:
                import urllib.request as _ur5
                req = _ur5.Request(
                    "http://127.0.0.1:8720/get_login_info",
                    data=b"{}",
                    headers={"content-type": "application/json",
                             "authorization": "Bearer " + _onebot_token()},
                    method="POST")
                with _ur5.urlopen(req, timeout=20) as r5:
                    info = json.loads(r5.read().decode("utf-8"))
                d = info.get("data") or {}
                # OneBot 只能给昵称；签名和头像读不回来，用本地记的最后一次值
                d["signature"] = saved.get("signature", "")
                d["avatarFile"] = saved.get("avatarFile", "")
                d["hasAvatar"] = bool(saved.get("avatarFile"))
                return self._json(200, {"ok": True, "profile": d})
            except Exception as e:
                return self._json(200, {"ok": False, "error": str(e)[:200]})

        if path == "/api/napcat":
          try:
            # NapCat 的 onebot11 配置，只读展示（token 打码）
            cfgp = os.path.join(NAPCAT, "framework", "config", "onebot11.json")
            out = {"found": False, "path": cfgp}
            if os.path.isfile(cfgp):
                try:
                    with open(cfgp, "r", encoding="utf-8") as fh2:
                        cfg = json.load(fh2)
                    out["found"] = True
                    net = cfg.get("network") or {}
                    http_s = net.get("httpServers") or []
                    ws_c = net.get("websocketClients") or []
                    ws_s = net.get("websocketServers") or []
                    out["http"] = [{"host": x.get("host", ""), "port": x.get("port", "")}
                                   for x in http_s]
                    out["wsClient"] = [{"url": x.get("url", ""),
                                        "token": x.get("token", "")} for x in ws_c]
                    out["wsServer"] = [{"host": x.get("host", ""),
                                        "port": x.get("port", "")} for x in ws_s]
                    tok = ""
                    for x in ws_c:
                        if x.get("token"):
                            tok = x["token"]
                            break
                    out["tokenMasked"] = (tok[:4] + "\u2026" + tok[-4:]) if len(tok) > 8 else ("\u5df2\u8bbe\u7f6e" if tok else "\uff08\u7a7a\uff09")
                except Exception as e:
                    out["error"] = str(e)
            return self._json(200, {"ok": True, "napcat": out})
          except Exception as _e:
            return self._json(200, {"ok": True, "napcat": {"found": False, "error": str(_e)}})

        if path == "/api/activities":
            try:
                with open(ACTIVITIES, "r", encoding="utf-8") as fha:
                    data = json.load(fha)
            except Exception:
                data = {"activities": []}

            # 把每项活动绑到的部件状态查出来，页面才知道"能不能真干"
            st3 = {}
            try:
                snap = os.path.join(ROOT, "lanya-state.json")
                with open(snap, "r", encoding="utf-8") as fhs3:
                    st3 = dict((c.get("tag"), c) for c in
                               (json.load(fhs3).get("components") or []))
            except Exception:
                st3 = {}

            def comp_count(tag):
                c = st3.get(tag)
                return int(c.get("count") or 0) if c else 0

            for a in (data.get("activities") or []):
                b = a.get("bound")
                a["ready"] = None          # None = 不依赖部件
                a["why"] = ""
                if b == "bili-duty":
                    crit = os.path.join(os.path.expanduser("~"), ".dsh",
                                        "bili_credential.json")
                    okcred = False
                    if os.path.isfile(crit):
                        try:
                            with open(crit, "r", encoding="utf-8") as fhc2:
                                cc2 = json.load(fhc2)
                            okcred = bool(cc2.get("sessdata")) and bool(cc2.get("bili_jct"))
                        except Exception:
                            okcred = False
                    a["ready"] = okcred
                    a["why"] = "B 站已登录" if okcred else "还没登录 B 站（去刷 B 站页扫码）"
                elif b:
                    n = comp_count(b)
                    a["ready"] = n > 0
                    a["why"] = (a.get("noteOn") if n > 0 else a.get("noteOff")) or (
                        ("%s 在跑" % b) if n > 0 else ("%s 没起" % b))

            data["suggestions"] = activity_suggestions(
                list(st3.values()), data.get("activities") or [])
            return self._json(200, {"ok": True, "data": data})

        if path == "/api/live":
            # 她现在在做什么：lifeloop 的日志 + 最后一条分享
            out = {"log": [], "last": "", "lastAt": "", "nextIn": ""}
            try:
                lf = tail_file(os.path.join(LANYA_DIR_, "lifeloop.log"), 12)
                out["log"] = [x.strip() for x in (lf or "").splitlines() if x.strip()]
            except Exception:
                pass
            try:
                with open(os.path.join(LANYA_DIR_, "lifeloop.state.json"), "r",
                          encoding="utf-8") as fhs2:
                    st2 = json.load(fhs2)
                out["last"] = st2.get("lastShare", "")
                out["lastAt"] = st2.get("lastAt", "")
            except Exception:
                pass
            try:
                with open(os.path.join(LANYA_DIR_, "budget.json"), "r",
                          encoding="utf-8") as fhb:
                    b = json.load(fhb)
                out["money"] = {"spent": b.get("spent", 0), "limit": b.get("limit", 0),
                                "exhausted": b.get("exhausted", False)}
            except Exception:
                pass
            return self._json(200, {"ok": True, "live": out})

        if path == "/api/bili":
            return self._json(200, {"ok": True, "bili": bili_snapshot()})

        if path == "/api/reminders/delete":
            try:
                idx = int(body.get("i"))
            except Exception:
                return self._json(400, {"ok": False, "error": "bad index"})
            try:
                with io.open(REMINDERS_, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    data = [data]
                if not isinstance(data, list) or idx < 0 or idx >= len(data):
                    return self._json(404, {"ok": False, "error": "not found"})
                removed = data.pop(idx)
                with io.open(REMINDERS_, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                return self._json(200, {"ok": True, "removed": removed})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/bili/duty":
            try:
                with open(BILI_DUTY, "r", encoding="utf-8") as fh:
                    return self._json(200, {"ok": True, "text": fh.read()})
            except Exception as e:
                return self._json(200, {"ok": False, "text": "", "error": str(e)})

        if path == "/api/tools/check":
            q = parse_qs(urlparse(self.path).query)
            key = (q.get("key") or [""])[0]
            latest = check_upstream(key)
            return self._json(200, {"ok": True, "key": key, "latest": latest,
                                    "reachable": latest is not None})

        if path == "/api/tools":
            return self._json(200, {"ok": True, "tools": tools_snapshot()})

        if path == "/api/budget":
            try:
                with open(BUDGET, "r", encoding="utf-8") as fh:
                    return self._json(200, {"ok": True, "budget": json.load(fh)})
            except Exception as e:
                return self._json(200, {"ok": False, "budget": {}, "error": str(e)})

        if path == "/api/memes/config":
            try:
                with open(MEMECFG, "r", encoding="utf-8") as fh:
                    return self._json(200, {"ok": True, "config": json.load(fh)})
            except Exception as e:
                return self._json(200, {"ok": False, "config": {}, "error": str(e)})

        if path == "/api/memes":
            groups = meme_groups_with_tags()
            return self._json(200, {"ok": True,
                                    "tagMap": _extra_tags(),
                                    "total": sum(g["count"] for g in groups),
                                    "groups": groups})

        if path == "/api/files/raw":
            q = parse_qs(urlparse(self.path).query)
            key = (q.get("dir") or [""])[0]
            name = (q.get("name") or [""])[0]
            if key not in FILE_DIRS or not name:
                return self._json(400, {"ok": False, "error": "bad params"})
            # 归档里的 name 是 "子目录/文件"，basename 会把子目录砍掉，
            # 结果永远找不到文件。改成按相对路径拼，并挡掉 ..
            rel = "/".join(p for p in str(name).replace("\\", "/").split("/")
                           if p and p not in (".", ".."))
            full = os.path.join(FILE_DIRS[key], *rel.split("/")) if rel else ""
            if not full or not os.path.isfile(full):
                return self._json(404, {"ok": False, "error": "not found"})
            ext = os.path.splitext(full)[1].lower()
            ctype = {".png": "image/png", ".gif": "image/gif",
                     ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".webp": "image/webp", ".bmp": "image/bmp"}.get(ext, "application/octet-stream")
            try:
                with open(full, "rb") as fh2:
                    data = fh2.read()
                self.send_response(200)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(data)))
                self.send_header("cache-control", "max-age=600")
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/files":
            q = parse_qs(urlparse(self.path).query)
            key = (q.get("dir") or [""])[0]
            if key not in FILE_DIRS:
                return self._json(400, {"ok": False, "error": "unknown dir"})
            prev = (q.get("preview") or ["0"])[0] in ("1", "true", "yes")
            try:
                lim = int((q.get("limit") or ["0"])[0])
            except Exception:
                lim = 0
            return self._json(200, {"ok": True, "dir": key,
                                    "files": list_dir_files(key, prev, lim)})

        if path == "/api/files/search":
            q = parse_qs(urlparse(self.path).query)
            key = (q.get("dir") or [""])[0]
            kw = (q.get("q") or [""])[0].strip()
            if key not in FILE_DIRS or not kw:
                return self._json(400, {"ok": False, "error": "bad params"})
            root = FILE_DIRS[key]
            hits = []
            limit = 80
            if os.path.isdir(root):
                for f in sorted(os.listdir(root), reverse=True):
                    p = os.path.join(root, f)
                    if not os.path.isfile(p):
                        continue
                    try:
                        with open(p, "r", encoding="utf-8", errors="replace") as fh:
                            text = fh.read(400000)
                    except Exception:
                        continue
                    idx = text.find(kw)
                    if idx < 0 and kw.lower() not in f.lower():
                        continue
                    if idx < 0:
                        idx = 0
                    start = max(0, idx - 40)
                    snippet = text[start:idx + len(kw) + 80]
                    snippet = " ".join(snippet.split())
                    hits.append({"name": f, "snippet": snippet})
                    if len(hits) >= limit:
                        break
            return self._json(200, {"ok": True, "q": kw, "hits": hits})

        if path == "/api/files/read":
            q = parse_qs(urlparse(self.path).query)
            key = (q.get("dir") or [""])[0]
            name = safe_name((q.get("name") or [""])[0])
            if key not in FILE_DIRS or not name:
                return self._json(400, {"ok": False, "error": "bad params"})
            full = os.path.join(FILE_DIRS[key], name)
            if not os.path.isfile(full):
                return self._json(404, {"ok": False, "error": "not found"})
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    txt = fh.read(200000)
                return self._json(200, {"ok": True, "name": name, "text": txt})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/tunables":
            try:
                with open(TUNABLES, "r", encoding="utf-8") as fh:
                    return self._json(200, {"ok": True, "tunables": json.load(fh)})
            except Exception as e:
                return self._json(200, {"ok": False, "tunables": {}, "error": str(e)})
        if path == "/api/config":
            try:
                with open(CONFIG, "r", encoding="utf-8") as fh:
                    return self._json(200, {"ok": True, "config": json.load(fh)})
            except Exception as e:
                return self._json(200, {"ok": False, "config": {}, "error": str(e)})

        if path.startswith("/archive/"):
            # 归档里的图片/文件：给作品页的「归档」标签看图用
            rel = path[len("/archive/"):]
            from urllib.parse import unquote as _uq
            rel = _uq(rel)
            parts = [p for p in rel.split("/") if p and p not in ("..", ".")]
            if not parts:
                return self._json(400, {"ok": False, "error": "bad path"})
            full = os.path.join(ARCHIVE_ROOT, *parts)
            if os.path.isfile(full):
                ext = os.path.splitext(full)[1].lower()
                ctype = {".png": "image/png", ".gif": "image/gif",
                         ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                         ".webp": "image/webp", ".bmp": "image/bmp"}.get(
                             ext, "application/octet-stream")
                try:
                    with open(full, "rb") as fh:
                        data = fh.read()
                    self.send_response(200)
                    self.send_header("content-type", ctype)
                    self.send_header("content-length", str(len(data)))
                    self.send_header("cache-control", "max-age=600")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                except Exception:
                    pass
            return self._json(404, {"ok": False, "error": "no archive file"})

        if path == "/qq-avatar.png":
            ap = os.path.join(NAPCAT, "qq-avatar.png")
            if os.path.isfile(ap):
                try:
                    with open(ap, "rb") as fhav:
                        dav = fhav.read()
                    self.send_response(200)
                    self.send_header("content-type", "image/png")
                    self.send_header("content-length", str(len(dav)))
                    self.send_header("cache-control", "no-store")
                    self.end_headers()
                    self.wfile.write(dav)
                    return
                except Exception:
                    pass
            return self._json(404, {"ok": False, "error": "no avatar"})

        if path == "/bili-qr.png":
            qp = os.path.join(NAPCAT, "bili-qr.png")
            if os.path.isfile(qp):
                try:
                    with open(qp, "rb") as fh3:
                        data = fh3.read()
                    self.send_response(200)
                    self.send_header("content-type", "image/png")
                    self.send_header("content-length", str(len(data)))
                    self.send_header("cache-control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                except Exception:
                    pass
            return self._json(404, {"ok": False, "error": "no qr"})

        if path.startswith("/memes/"):
            parts = [p for p in path.split("/") if p]
            if len(parts) == 4:
                # BaseHTTPRequestHandler 给的是 URL 编码过的路径，中文文件名必须
                # 先解码，否则 os.path.isfile 永远 False——表现就是图全是 404。
                from urllib.parse import unquote as _unq
                src, tg, fn = _unq(parts[1]), _unq(parts[2]), _unq(parts[3])
                full = meme_path(src, tg, fn)
                if full:
                    ext = os.path.splitext(full)[1].lower()
                    ctype = {".png": "image/png", ".gif": "image/gif",
                             ".webp": "image/webp", ".bmp": "image/bmp"}.get(ext, "image/jpeg")
                    with open(full, "rb") as fh:
                        data = fh.read()
                    self.send_response(200)
                    self.send_header("content-type", ctype)
                    self.send_header("content-length", str(len(data)))
                    self.send_header("cache-control", "max-age=300")
                    self.end_headers()
                    self.wfile.write(data)
                    return
            return self._json(404, {"ok": False, "error": "no meme"})

        if path in ("/", ""):
            self.path = "/lanya-panel.html"
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read()

        if path == "/api/bili/reply/delete":
            # 直接发请求，不起子进程 —— 光 python 启动就要一两秒。
            bv = str(body.get("bv") or "").strip()
            rpid = str(body.get("rpid") or "").strip()
            if not bv or not rpid:
                return self._json(400, {"ok": False, "error": "missing bv or rpid"})
            try:
                okk, note = bili_delete_comment(bv, rpid)
            except Exception as e:
                okk, note = False, str(e)[:150]
            if okk:
                # 顺手把本地那行也去掉，页面才对得上
                try:
                    with open(BILI_SEEN, "r", encoding="utf-8") as fhs:
                        lines = fhs.readlines()
                    keep = []
                    for ln in lines:
                        cells = [x.strip() for x in ln.strip().strip("|").split("|")]
                        if len(cells) >= 2 and cells[0] == bv and cells[1] == rpid:
                            continue
                        keep.append(ln)
                    with open(BILI_SEEN, "w", encoding="utf-8", newline="") as fhs2:
                        fhs2.writelines(keep)
                except Exception:
                    pass
            return self._json(200, {"ok": okk, "note": note,
                                    "error": "" if okk else note})

        if path == "/api/bili/follow":
            uid = str(body.get("uid") or "").strip()
            act = 1 if body.get("act", 1) == 1 else 2
            if not uid:
                return self._json(400, {"ok": False, "error": "missing uid"})
            try:
                okk2, note2 = bili_follow(uid, act)
            except Exception as e:
                okk2, note2 = False, str(e)[:150]
            return self._json(200, {"ok": okk2, "note": note2})

        if path == "/api/bili/onerun":
            # Really do one round: pick a video, write a comment, post it, record it.
            # Talks to the bili MCP server over JSON-RPC directly, so it does not
            # need a DSH agent with tools loaded.
            try:
                import subprocess as _spo
                script = os.path.join(NAPCAT, "bili_onerun.py")
                if not os.path.isfile(script):
                    return self._json(404, {"ok": False, "error": "bili_onerun.py missing"})
                _venv = os.path.join(os.path.expanduser("~"), ".dsh", "bili-duty",
                                     ".venv", "Scripts", "python.exe")
                _py = _venv if os.path.isfile(_venv) else "python"
                _env = dict(os.environ)
                _env["PYTHONUTF8"] = "1"
                _env["PYTHONIOENCODING"] = "utf-8"
                want_comment = body.get("comment", True) is not False
                want_mention = body.get("mention", False) is True
                args = [_py, script]
                if not want_comment:
                    args.append("--no-comment")
                if want_mention:
                    args.append("--mention")
                p = _spo.Popen(args, stdout=_spo.PIPE, stderr=_spo.PIPE,
                               cwd=NAPCAT, env=_env,
                               creationflags=getattr(_spo, "CREATE_NO_WINDOW", 0))
                out, err = p.communicate(timeout=300)
                txt = (out or b"").decode("utf-8", "replace")
                # 最后一段 JSON 就是结果
                st = txt.find("{")
                data = {}
                if st >= 0:
                    try:
                        data = json.loads(txt[st:])
                    except Exception:
                        data = {}
                if not data:
                    data = {"ok": False,
                            "error": (txt or (err or b"").decode("utf-8", "replace"))[-300:]}
                return self._json(200, data)
            except Exception as e:
                return self._json(200, {"ok": False, "error": str(e)[:300]})

        if path == "/api/bili/seen/delete":
            kind = str(body.get("kind") or "")
            bv = str(body.get("bv") or "").strip()
            if not bv:
                return self._json(400, {"ok": False, "error": "missing bv"})
            try:
                with open(BILI_SEEN, "r", encoding="utf-8") as fhs:
                    lines = fhs.readlines()
                out = []
                removed = 0
                for ln in lines:
                    hit = False
                    if "|" in ln and bv in ln:
                        if kind == "comment":
                            # 评论表：| BV | rpid | ...
                            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
                            if cells and cells[0] == bv and len(cells) >= 2:
                                hit = True
                        elif kind == "video":
                            # 视频表：| 日期 | BV | ...
                            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
                            if len(cells) >= 2 and cells[1] == bv:
                                hit = True
                    if hit:
                        removed += 1
                        continue
                    out.append(ln)
                if not removed:
                    return self._json(404, {"ok": False, "error": "not found"})
                with open(BILI_SEEN, "w", encoding="utf-8", newline="") as fhs2:
                    fhs2.writelines(out)
                return self._json(200, {"ok": True, "removed": removed,
                                        "note": "只删了本地记录，B站上的没动"})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/bili/run":
            # Spawn a headless agent to actually run one Bilibili duty pass.
            # Nothing happens on its own: seen.md stays empty until asked.
            try:
                import subprocess as _spb
                duty = os.path.join(os.path.expanduser("~"), ".dsh",
                                    "bili-duty", "duty.md")
                if not os.path.isfile(duty):
                    return self._json(200, {"ok": False, "error": "duty.md not found"})
                dsh = os.path.join(os.environ.get("DSH_ROOT", r"D:\DSH"),
                   "resources", "runtime", "cli", "bin", "dsh.cmd")
                if not os.path.isfile(dsh):
                    return self._json(200, {"ok": False, "error": "dsh.cmd not found"})
                with open(duty, "r", encoding="utf-8", errors="replace") as fhd:
                    brief = ("Go do one Bilibili duty pass now. Use your bili tools.\n\n"
                             + fhd.read()[:20000])
                pr = _spb.Popen([dsh, "--profile", "headless", "-"],
                                stdin=_spb.PIPE, stdout=_spb.DEVNULL,
                                stderr=_spb.DEVNULL,
                                creationflags=getattr(_spb, "CREATE_NO_WINDOW", 0))
                try:
                    pr.stdin.write(brief.encode("utf-8"))
                    pr.stdin.close()
                except Exception:
                    pass
                return self._json(200, {"ok": True, "pid": pr.pid})
            except Exception as e:
                return self._json(200, {"ok": False, "error": str(e)[:200]})

        if path == "/api/action":
            target = str(body.get("target") or "")
            # NapCat 注入必须提权，否则 napimain 什么都不做。与其静默失败，
            # 不如直接弹 UAC 让用户点一下——这是唯一合法的路径。
            if target == "napcat-elevated":
                try:
                    pid = start_napcat_elevated()
                    return self._json(200, {"ok": True, "pid": pid, "elevated": True,
                                             "note": "UAC prompt opened - click YES"})
                except Exception as e:
                    return self._json(500, {"ok": False, "error": str(e)})
            try:
                # 能映射到计划任务的，走任务（无窗口）；其余才直接起脚本。
                tr = run_task(target)
                if tr is not None:
                    return self._json(200 if tr["ok"] else 500,
                                      {"ok": tr["ok"], "task": tr["task"], "log": tr["log"]})
                pid = run_script(target)
                return self._json(200, {"ok": True, "pid": pid})
            except Exception as e:
                return self._json(400, {"ok": False, "error": str(e)})

        if path == "/api/persona":
            text = body.get("text") or ""
            if not text.strip():
                return self._json(400, {"ok": False, "error": "empty"})
            try:
                with open(PERSONA, "w", encoding="utf-8") as fh:
                    fh.write(text)
                return self._json(200, {"ok": True, "chars": len(text)})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/schedule":
            return self._json(200, {
                "ok": True,
                "reminders": reminders_snapshot(),
                "tasks": scheduled_tasks(),
            })

        if path == "/api/memory":
            return self._json(200, {"ok": True, "memory": memory_overview()})

        if path == "/api/bili":
            return self._json(200, {"ok": True, "bili": bili_snapshot()})

        if path == "/api/bili/duty":
            try:
                with open(BILI_DUTY, "r", encoding="utf-8") as fh:
                    return self._json(200, {"ok": True, "text": fh.read()})
            except Exception as e:
                return self._json(200, {"ok": False, "text": "", "error": str(e)})

        if path == "/api/tools/check":
            q = parse_qs(urlparse(self.path).query)
            key = (q.get("key") or [""])[0]
            latest = check_upstream(key)
            return self._json(200, {"ok": True, "key": key, "latest": latest,
                                    "reachable": latest is not None})

        if path == "/api/tools":
            return self._json(200, {"ok": True, "tools": tools_snapshot()})

        if path == "/api/budget":
            try:
                with open(BUDGET, "r", encoding="utf-8") as fh:
                    return self._json(200, {"ok": True, "budget": json.load(fh)})
            except Exception as e:
                return self._json(200, {"ok": False, "budget": {}, "error": str(e)})

        if path == "/api/memes/config":
            try:
                with open(MEMECFG, "r", encoding="utf-8") as fh:
                    return self._json(200, {"ok": True, "config": json.load(fh)})
            except Exception as e:
                return self._json(200, {"ok": False, "config": {}, "error": str(e)})

        if path == "/api/memes":
            groups = meme_groups()
            return self._json(200, {"ok": True,
                                    "total": sum(g["count"] for g in groups),
                                    "groups": groups})

        if path == "/api/files":
            q = parse_qs(urlparse(self.path).query)
            key = (q.get("dir") or [""])[0]
            if key not in FILE_DIRS:
                return self._json(400, {"ok": False, "error": "unknown dir"})
            return self._json(200, {"ok": True, "dir": key, "files": list_dir_files(key)})

        if path == "/api/files/read":
            q = parse_qs(urlparse(self.path).query)
            key = (q.get("dir") or [""])[0]
            name = safe_name((q.get("name") or [""])[0])
            if key not in FILE_DIRS or not name:
                return self._json(400, {"ok": False, "error": "bad params"})
            full = os.path.join(FILE_DIRS[key], name)
            if not os.path.isfile(full):
                return self._json(404, {"ok": False, "error": "not found"})
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    txt = fh.read(200000)
                return self._json(200, {"ok": True, "name": name, "text": txt})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/schedule":
            return self._json(200, {
                "ok": True,
                "reminders": reminders_snapshot(),
                "tasks": scheduled_tasks(),
            })

        if path == "/api/memory":
            return self._json(200, {"ok": True, "memory": memory_overview()})

        if path == "/api/bili":
            return self._json(200, {"ok": True, "bili": bili_snapshot()})

        if path == "/api/bili/duty":
            try:
                with open(BILI_DUTY, "r", encoding="utf-8") as fh:
                    return self._json(200, {"ok": True, "text": fh.read()})
            except Exception as e:
                return self._json(200, {"ok": False, "text": "", "error": str(e)})

        if path == "/api/tools/check":
            q = parse_qs(urlparse(self.path).query)
            key = (q.get("key") or [""])[0]
            latest = check_upstream(key)
            return self._json(200, {"ok": True, "key": key, "latest": latest,
                                    "reachable": latest is not None})

        if path == "/api/tools":
            return self._json(200, {"ok": True, "tools": tools_snapshot()})

        if path == "/api/budget":
            b = body.get("budget")
            if not isinstance(b, dict):
                return self._json(400, {"ok": False, "error": "budget must be an object"})
            try:
                with open(BUDGET, "r", encoding="utf-8") as fh:
                    cur = json.load(fh)
            except Exception:
                cur = {}
            if isinstance(cur, dict):
                cur.update(b)
                b = cur
            try:
                tmp = BUDGET + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(b, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, BUDGET)
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/bili/duty":
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                return self._json(400, {"ok": False, "error": "空内容不保存"})
            try:
                bak = BILI_DUTY + ".bak"
                if os.path.isfile(BILI_DUTY):
                    import shutil
                    shutil.copy2(BILI_DUTY, bak)
                with open(BILI_DUTY, "w", encoding="utf-8") as fh:
                    fh.write(text)
                return self._json(200, {"ok": True, "chars": len(text)})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/memes/config":
            cfg = body.get("config")
            if not isinstance(cfg, dict):
                return self._json(400, {"ok": False, "error": "config must be an object"})
            try:
                with open(MEMECFG, "r", encoding="utf-8") as fh:
                    cur = json.load(fh)
            except Exception:
                cur = {}
            if isinstance(cur, dict):
                cur.update(cfg)
                cfg = cur
            try:
                tmp = MEMECFG + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(cfg, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, MEMECFG)
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/memes/classify":
            # 触发按情绪归类。跑起来要几分钟到几十分钟（每张都要过一遍读图），
            # 所以 detached 起一个后台进程，接口立刻返回。
            script = os.path.join(NAPCAT, "classify_emotion.py")
            if not os.path.isfile(script):
                return self._json(404, {"ok": False, "error": "classifier missing"})
            try:
                p = subprocess.Popen(
                    ["python", script],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return self._json(200, {"ok": True, "pid": p.pid,
                                        "note": "classifying in background"})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/memes/tag":
            src = str(body.get("source") or "")
            tag = str(body.get("tag") or "")
            fname = str(body.get("file") or "")
            want = body.get("tags")
            if not isinstance(want, list):
                return self._json(400, {"ok": False, "error": "tags must be a list"})
            if not fname:
                return self._json(400, {"ok": False, "error": "missing file"})
            try:
                cur = _extra_tags()
                key = str(tag) + "/" + os.path.basename(fname)
                cleaned = [str(t).strip()[:24] for t in want if str(t).strip()][:6]
                if cleaned:
                    cur[key] = cleaned
                else:
                    cur.pop(key, None)
                tmp = MEME_TAGS + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(cur, fh, ensure_ascii=False, indent=1)
                os.replace(tmp, MEME_TAGS)
                return self._json(200, {"ok": True, "tags": cleaned})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/memes/move":
            src = str(body.get("source") or "")
            tag = str(body.get("tag") or "")
            fname = str(body.get("file") or "")
            to_tag = str(body.get("toTag") or "").strip()
            if not to_tag:
                return self._json(400, {"ok": False, "error": "missing toTag"})
            # 只允许移动「收回来的」那一批——其它来源是包自带的，动了会乱
            if src != "stolen":
                return self._json(400, {"ok": False, "error": "only stolen can be moved"})
            full = meme_path(src, tag, fname)
            if not full:
                return self._json(404, {"ok": False, "error": "not found"})
            safe_tag = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", to_tag)[:32]
            if not safe_tag:
                return self._json(400, {"ok": False, "error": "bad tag"})
            dst_dir = os.path.join(LANYA_MEMES, "stolen", safe_tag)
            try:
                os.makedirs(dst_dir, exist_ok=True)
                os.replace(full, os.path.join(dst_dir, os.path.basename(full)))
                return self._json(200, {"ok": True, "toTag": safe_tag})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/memes/delete":
            full = meme_path(body.get("source"), body.get("tag"), body.get("file"))
            if not full:
                return self._json(404, {"ok": False, "error": "not found"})
            try:
                os.remove(full)
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/bili/login/start":
            # 扫码登录：起一个 python 子进程调 bilibili-api 的登录，把二维码图存下来
            try:
                import subprocess as _sp2
                script = os.path.join(NAPCAT, "bili_login3.py")
                if not os.path.isfile(script):
                    return self._json(404, {"ok": False, "error": "bili_login2.py missing"})
                # bilibili_api / qrcode 装在 bili-duty 的 venv 里，系统 python 没有
                _venv = os.path.join(os.path.expanduser("~"), ".dsh", "bili-duty",
                                     ".venv", "Scripts", "python.exe")
                _py = _venv if os.path.isfile(_venv) else "python"
                _sp2.Popen([_py, script],
                           stdout=_sp2.DEVNULL, stderr=_sp2.DEVNULL,
                           creationflags=getattr(_sp2, "CREATE_NO_WINDOW", 0))
                return self._json(200, {"ok": True, "note": "starting"})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/napcat/profile":
            try:
                import urllib.request as _ur6
                _tok6 = _onebot_token()

                def _ob(action, payload):
                    req = _ur6.Request(
                        "http://127.0.0.1:8720/" + action,
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"content-type": "application/json",
                                 "authorization": "Bearer " + _tok6},
                        method="POST")
                    with _ur6.urlopen(req, timeout=30) as r6:
                        return json.loads(r6.read().decode("utf-8"))

                did = []
                nick = str(body.get("nickname") or "").strip()
                if nick:
                    _ob("set_qq_profile", {"nickname": nick})
                    did.append("nickname")
                sig = str(body.get("signature") or "").strip()
                if sig:
                    _ob("set_self_longnick", {"longNick": sig})
                    did.append("signature")
                av = str(body.get("avatarBase64") or "").strip()
                if av:
                    if av.startswith("data:"):
                        av = av.split(",", 1)[-1]
                    # 同时把图存本地，页面才能显示出来（OneBot 读不回头像）
                    import base64 as _b64
                    ap = os.path.join(NAPCAT, "qq-avatar.png")
                    try:
                        with open(ap, "wb") as fha:
                            fha.write(_b64.b64decode(av))
                    except Exception:
                        ap = ""
                    _ob("set_qq_avatar", {"file": "base64://" + av})
                    did.append("avatar")
                    if ap:
                        saved3 = {}
                        pf3 = os.path.join(NAPCAT, "qq-profile.json")
                        if os.path.isfile(pf3):
                            try:
                                with open(pf3, "r", encoding="utf-8") as fhr:
                                    saved3 = json.load(fhr)
                            except Exception:
                                saved3 = {}
                        saved3["avatarFile"] = ap
                        with open(pf3, "w", encoding="utf-8") as fhr:
                            json.dump(saved3, fhr, ensure_ascii=False)
                return self._json(200, {"ok": True, "did": did})
            except Exception as e:
                return self._json(200, {"ok": False, "error": str(e)[:200]})

        if path == "/api/update":
            # 从 GitHub 拉最新代码。--ff-only：有本地改动就失败，不会把东西搅乱。
            import subprocess as _sp3
            repo = os.environ.get("LANYA_REPO") or os.path.join(os.path.dirname(NAPCAT), "dsh-lanya")
            try:
                before = _sp3.run(["git", "-C", repo, "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=20).stdout.strip()
                pull = _sp3.run(["git", "-C", repo, "pull", "--ff-only"],
                                capture_output=True, text=True, timeout=180)
                if pull.returncode != 0:
                    msg = (pull.stderr or pull.stdout or "").strip()[:400]
                    return self._json(200, {"ok": False,
                                            "error": msg or "git pull failed"})
                after = _sp3.run(["git", "-C", repo, "rev-parse", "HEAD"],
                                 capture_output=True, text=True, timeout=20).stdout.strip()
                if before == after:
                    return self._json(200, {"ok": True, "changed": [],
                                            "message": "已经是最新，没有新提交。"})
                log = _sp3.run(["git", "-C", repo, "log", "--oneline",
                                before + ".." + after],
                               capture_output=True, text=True, timeout=20).stdout
                changed = [ln.strip() for ln in log.splitlines() if ln.strip()][:20]
                return self._json(200, {"ok": True, "changed": changed,
                                        "message": "拉下来 %d 个新提交。" % len(changed)})
            except Exception as e:
                return self._json(200, {"ok": False, "error": str(e)[:300]})

        if path == "/api/memory/edit":
            eid = str(body.get("id") or "").strip()
            text = str(body.get("text") or "").strip()
            if not eid or not text:
                return self._json(400, {"ok": False, "error": "need id and text"})
            if len(text) > 400:
                return self._json(400, {"ok": False, "error": "too long (max 400)"})
            try:
                import sqlite3 as _sq4, time as _t4
                db = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-memento", "memory.db")
                c4 = _sq4.connect(db)
                row = c4.execute("SELECT text FROM entries WHERE id = ?", (eid,)).fetchone()
                if not row:
                    c4.close()
                    return self._json(404, {"ok": False, "error": "not found"})
                c4.execute("UPDATE entries SET text = ?, updated_at = ? WHERE id = ?",
                           (text, int(_t4.time() * 1000), eid))
                c4.execute(
                    "INSERT INTO audit (ts, action, track, scope, entry_id, text, outcome, source, session_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (int(_t4.time() * 1000), "edit-panel", None, None, eid,
                     text, "edited via panel", "panel", None))
                c4.commit()
                c4.close()
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/activities/add":
            try:
                with open(ACTIVITIES, "r", encoding="utf-8") as fha4:
                    data = json.load(fha4)
                acts = data.setdefault("activities", [])
                name = str(body.get("name") or "").strip()
                if not name:
                    return self._json(400, {"ok": False, "error": "need a name"})
                # 用名字生成一个稳定 id，重复就加序号
                base = "".join(c for c in name.lower() if c.isalnum()) or "act"
                aid = base
                n = 1
                while any(a.get("id") == aid for a in acts):
                    aid = base + str(n)
                    n += 1
                acts.append({
                    "id": aid,
                    "name": name[:24],
                    "desc": str(body.get("desc") or "")[:120],
                    "enabled": bool(body.get("enabled", True)),
                    "cost": bool(body.get("cost", True)),
                })
                tmp = ACTIVITIES + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fha5:
                    json.dump(data, fha5, ensure_ascii=False, indent=1)
                os.replace(tmp, ACTIVITIES)
                return self._json(200, {"ok": True, "id": aid})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/activities/delete":
            try:
                with open(ACTIVITIES, "r", encoding="utf-8") as fha6:
                    data = json.load(fha6)
                aid = str(body.get("id") or "")
                before = len(data.get("activities") or [])
                data["activities"] = [a for a in (data.get("activities") or [])
                                      if a.get("id") != aid]
                if len(data["activities"]) == before:
                    return self._json(404, {"ok": False, "error": "not found"})
                tmp = ACTIVITIES + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fha7:
                    json.dump(data, fha7, ensure_ascii=False, indent=1)
                os.replace(tmp, ACTIVITIES)
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/activities":
            try:
                with open(ACTIVITIES, "r", encoding="utf-8") as fha2:
                    data = json.load(fha2)
                acts = data.get("activities") or []
                want = body.get("enabled") or {}
                for a in acts:
                    aid = a.get("id")
                    if aid in want:
                        a["enabled"] = bool(want[aid])
                tmp = ACTIVITIES + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fha3:
                    json.dump(data, fha3, ensure_ascii=False, indent=1)
                os.replace(tmp, ACTIVITIES)
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/budget/topup":
            try:
                amt = float(body.get("amount") or 0)
                if amt == 0:
                    return self._json(400, {"ok": False, "error": "amount is 0"})
                bp = os.path.join(LANYA_DIR_, "budget.json")
                with open(bp, "r", encoding="utf-8") as fhb2:
                    b = json.load(fhb2)
                # amount > 0 加额度；< 0 直接记一笔花费
                if amt > 0:
                    b["limit"] = round(float(b.get("limit") or 0) + amt, 4)
                    b["exhausted"] = False
                else:
                    b["spent"] = round(float(b.get("spent") or 0) + abs(amt), 4)
                lim = float(b.get("limit") or 0)
                sp = float(b.get("spent") or 0)
                b["remaining"] = round(max(0.0, lim - sp), 4)
                b["exhausted"] = bool(lim > 0 and sp >= lim)
                import uuid as _u4
                tmp = bp + "." + _u4.uuid4().hex[:8] + ".tmp"
                _retry = 6
                with open(tmp, "w", encoding="utf-8") as fhb3:
                    json.dump(b, fhb3, ensure_ascii=False)
                # Windows 上目标被另一个进程读着时 os.replace 会抛 WinError 5/32。
                # 连点两下「+1」正好会撞上，重试几次就过去了。
                for _k in range(_retry):
                    try:
                        os.replace(tmp, bp)
                        break
                    except OSError:
                        if _k == _retry - 1:
                            raise
                        import time as _t5
                        _t5.sleep(0.05)
                return self._json(200, {"ok": True, "budget": b})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/memory/add":
            text = str(body.get("text") or "").strip()
            track = str(body.get("track") or "agent")
            if track not in ("user", "agent"):
                track = "agent"
            if not text:
                return self._json(400, {"ok": False, "error": "empty text"})
            if len(text) > 400:
                return self._json(400, {"ok": False, "error": "too long (max 400)"})
            try:
                import sqlite3 as _sq, uuid as _uuid, time as _t
                db = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-memento", "memory.db")
                c = _sq.connect(db)
                now = int(_t.time() * 1000)
                eid = str(_uuid.uuid4())[:8] + "-" + _uuid.uuid4().hex[:8]
                c.execute(
                    "INSERT INTO entries (id, track, scope, workspace_key, text, source, "
                    "created_at, updated_at, session_id, agent_key, tags, version) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, track, "workspace", "", text, "panel", now, now, None, "", "[]", 1))
                c.execute(
                    "INSERT INTO audit (ts, action, track, scope, entry_id, text, outcome, source, session_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (now, "add-panel", track, "workspace", eid, text, "added via panel", "panel", None))
                c.commit()
                c.close()
                return self._json(200, {"ok": True, "id": eid})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/memory/delete":
            eid = str(body.get("id") or "").strip()
            if not eid:
                return self._json(400, {"ok": False, "error": "missing id"})
            try:
                import sqlite3 as _sq, time as _t
                db = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-memento", "memory.db")
                c = _sq.connect(db)
                row = c.execute("SELECT text FROM entries WHERE id = ?", (eid,)).fetchone()
                if not row:
                    c.close()
                    return self._json(404, {"ok": False, "error": "not found"})
                c.execute("DELETE FROM entries WHERE id = ?", (eid,))
                c.execute(
                    "INSERT INTO audit (ts, action, track, scope, entry_id, text, outcome, source, session_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (int(_t.time() * 1000), "delete-panel", None, None, eid,
                     row[0], "deleted via panel", "panel", None))
                c.commit()
                c.close()
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/files/delete":
            key = str(body.get("dir") or "")
            name = safe_name(body.get("name"))
            if key not in FILE_DIRS or not name:
                return self._json(400, {"ok": False, "error": "bad params"})
            full = os.path.join(FILE_DIRS[key], name)
            if not os.path.isfile(full):
                return self._json(404, {"ok": False, "error": "not found"})
            try:
                os.remove(full)
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/tunables":
            tun = body.get("tunables")

        if path == "/api/tunables":
            tun = body.get("tunables")
            if not isinstance(tun, dict):
                return self._json(400, {"ok": False, "error": "tunables must be an object"})
            # 保留文件里的中文分组说明，只覆盖数值项
            try:
                with open(TUNABLES, "r", encoding="utf-8") as fh:
                    current = json.load(fh)
            except Exception:
                current = {}
            if isinstance(current, dict):
                current.update(tun)
                tun = current
            try:
                tmp = TUNABLES + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(tun, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, TUNABLES)
                return self._json(200, {"ok": True, "restart": ["groupwatch"]})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/config":
            cfg = body.get("config")
            if not isinstance(cfg, dict):
                return self._json(400, {"ok": False, "error": "config must be an object"})
            try:
                tmp = CONFIG + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(cfg, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, CONFIG)
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/plugin-toggle":
            name = str(body.get("name") or "").strip()
            want = body.get("enabled") is not False
            if not name:
                return self._json(400, {"ok": False, "error": "missing name"})
            try:
                with open(PROFILE_PKG, "r", encoding="utf-8") as fh:
                    doc = json.load(fh)
                bundles = doc.setdefault("dsh", {}).setdefault("profile", {}).setdefault("bundles", [])
                if want and name not in bundles:
                    bundles.append(name)
                if not want and name in bundles:
                    bundles.remove(name)
                tmp = PROFILE_PKG + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, PROFILE_PKG)
                return self._json(200, {"ok": True, "enabled": want, "restart": True})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if path == "/api/plugin":
            op = str(body.get("op") or "add")
            spec = str(body.get("spec") or "").strip()
            if not spec:
                return self._json(400, {"ok": False, "log": "missing spec"})
            try:
                p = subprocess.run(
                    [DSH_CLI, "plugin", "--profile", "desktop", op, spec],
                    capture_output=True, timeout=900,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
                return self._json(200, {"ok": p.returncode == 0, "log": out.strip() or "(no output)"})
            except Exception as e:
                return self._json(200, {"ok": False, "log": "failed: " + str(e)})

        return self._json(404, {"ok": False, "error": "no such route"})


def main():
    port = int(os.environ.get("LANYA_PANEL_PORT", "19389"))
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except Exception:
            pass
    os.makedirs(ROOT, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("lanya panel server on http://127.0.0.1:%d/lanya-panel.html" % port, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    while True:
        try:
            main()
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("crashed: %s - restarting in 5s" % e, flush=True)
            import time
            time.sleep(5)
