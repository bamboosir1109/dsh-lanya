# Proactive greetings, restricted to people she ALREADY has a real memory of.
#
# Why the restriction: only greet people she already has a real memory of. So a candidate must have BOTH:
#   - a profile file  <dafeiyu>\people\<qq>.md  (this is what she writes after a real
#     exchange), AND
#   - an actual message from or to them somewhere in her readable history.
# Having a profile is not enough on its own: she could have created one from a
# single passing hello with nothing to say.
#
# Everything else - daytime only, a low daily cap, "do not repeat the same person
# on consecutive days" - exists to keep this from becoming spam.
#
# Registered as DSH-Dafeiyu-Greet (hourly; it decides for itself whether to act).

# --- 隐藏控制台窗口 ---------------------------------------------------------
# 由计划任务用 python.exe 启动时，Windows 会先弹一个黑框再跑脚本。用户看到的
# "窗口反复弹出"就是这个。脚本没法阻止窗口被创建，但可以在第一行就把它藏掉，
# 把可见时间从"整个运行时长"压到启动那一瞬。

# --- 插件调度器在跑时，立刻退出 ---------------------------------------------
# 这几个脚本都由计划任务用 python.exe 直接拉起，而 python 是控制台程序，任务一
# 触发就会闪一个黑框。插件里的调度器已经在做同样的事（而且没有窗口），所以这里
# 一看到它的标记文件就立刻退出——窗口存在的时间从"整个运行时长"缩到进程启动那
# 一瞬。删除任务能彻底解决，但那需要管理员权限。
import os as _os
def _find_dsh_root():
    """找 DSH 装在哪。

    别写死盘符。先看环境变量，再看自己这一级往上，再看各盘根目录。
    """
    rel = os.path.join("resources", "runtime", "cli", "bin", "dsh.cmd")
    here = os.path.dirname(os.path.abspath(__file__))
    cands = []
    for base in (here, os.path.dirname(here), os.path.dirname(os.path.dirname(here))):
        cands.append(os.path.join(base, "DSH"))
    for d in "CDEFG":
        cands.append(d + ":\\DSH")
    for c in cands:
        if os.path.isfile(os.path.join(c, rel)):
            return c
    return cands[0]


import time as _time
try:
    _mk = _os.path.join(r"<WORKDIR>\napcat", "scheduler-in-plugin.flag")
    if _os.path.exists(_mk) and (_time.time() - _os.path.getmtime(_mk)) < 1800:
        raise SystemExit(0)
except SystemExit:
    raise
except Exception:
    pass
# ---------------------------------------------------------------------------
import ctypes as _ct
try:
    _ct.windll.user32.ShowWindow(_ct.windll.kernel32.GetConsoleWindow(), 0)
except Exception:
    pass
# ---------------------------------------------------------------------------
import glob
import json
import os
import random
import re
import subprocess
import time
import urllib.request

NAPCAT = "http://127.0.0.1:8720"
TOKEN = "<ONEBOT_TOKEN>"
ADMIN = "YOUR_ADMIN_QQ"          # the owner; she should not "greet" him like a stranger

DAFEIYU = os.path.join(r"<WORKDIR>", "\u5927\u80a5\u9c7c")      # "Dafeiyu"
PEOPLE = os.path.join(DAFEIYU, "people")
CHATS = os.path.join(DAFEIYU, "chats")
PROMPT = os.path.join(r"<WORKDIR>\napcat", "greet.prompt.txt")
STATE = os.path.join(DAFEIYU, "greet.state.json")
LOG = os.path.join(DAFEIYU, "greet.log")
SENT = os.path.join(DAFEIYU, "sent-log.txt")

DSH = os.path.join(os.environ.get("DSH_ROOT") or _find_dsh_root(),
                   "resources", "runtime", "cli", "bin", "dsh.cmd")
MAX_PER_DAY = 2
DAY_START, DAY_END = 10, 22


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    print(line)


def api(action, payload):
    req = urllib.request.Request(
        NAPCAT + "/" + action,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save(path, obj):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
    except Exception:
        pass


def profiles():
    """QQ-number filenames only - a nickname filename never matches."""
    out = {}
    for p in glob.glob(os.path.join(PEOPLE, "*.md")):
        stem = os.path.splitext(os.path.basename(p))[0]
        if stem.isdigit():
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    out[stem] = fh.read().strip()
            except Exception:
                pass
    return out


def talked_before(qq, nickname):
    """True if we can find a real line from/to this person in the exported history."""
    if not os.path.isdir(CHATS):
        return False
    needles = [qq]
    if nickname:
        needles.append(nickname)
    for f in glob.glob(os.path.join(CHATS, "*.md")):
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except Exception:
            continue
        for n in needles:
            if n and n in body:
                return True
    return False


def ask_agent(task):
    try:
        p = subprocess.run([DSH, "--profile", "headless", "-"], input=task.encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=240)
        # headless stdout is UTF-16LE with a BOM, not UTF-8.
        raw = p.stdout
        if raw[:2] == b"\xff\xfe":
            return raw.decode("utf-16-le").lstrip("\ufeff").strip()
        try:
            return raw.decode("utf-8").strip()
        except Exception:
            return raw.decode("utf-16-le", "replace").lstrip("\ufeff").strip()
    except Exception as e:
        log("agent call failed: " + str(e)[:120])
        return ""


def main():
    os.makedirs(DAFEIYU, exist_ok=True)
    now = time.localtime()
    if not (DAY_START <= now.tm_hour < DAY_END):
        log("outside daytime window; exit")
        return

    st = load(STATE, {"days": {}})
    today = time.strftime("%Y-%m-%d")
    sent_today = st.get("days", {}).get(today, [])
    if len(sent_today) >= MAX_PER_DAY:
        log("daily cap reached (%d/%d); exit" % (len(sent_today), MAX_PER_DAY))
        return

    ppl = profiles()
    if not ppl:
        log("no profiles yet; nothing to greet")
        return

    # Last greets overall, so the same person does not get picked every time.
    recent = []
    for d in sorted(st.get("days", {}).keys())[-7:]:
        recent += st["days"][d]

    cands = []
    for qq, text in ppl.items():
        if qq == ADMIN or qq in sent_today or qq in recent:
            continue
        m = re.search(r"[\w\u4e00-\u9fff]{1,20}", text)
        nick = m.group(0) if m else ""
        if not talked_before(qq, nick):
            log("skip %s: profile exists but no real conversation found" % qq)
            continue
        cands.append((qq, text))

    if not cands:
        log("no eligible candidates (need profile + prior conversation)")
        return

    qq, text = random.choice(cands)

    if not os.path.isfile(PROMPT):
        log("missing prompt file; exit")
        return
    with open(PROMPT, "r", encoding="utf-8") as fh:
        tpl = fh.read()
    task = tpl.replace("{{QQ}}", qq).replace("{{PROFILE}}", text[:1500])

    ans = ask_agent(task)
    ans = (ans or "").strip()
    if ans == "" or ans.upper().startswith("SKIP"):
        log("agent chose not to greet %s" % qq)
        return

    lines = [l for l in ans.split("\n") if l.strip()]
    text_out = lines[-1].strip() if lines else ""
    if text_out == "":
        log("empty greeting; skip")
        return

    try:
        api("send_private_msg", {"user_id": int(qq), "message": text_out})
        log("greeted %s: %s" % (qq, text_out))
        st.setdefault("days", {}).setdefault(today, []).append(qq)
        save(STATE, st)
        with open(SENT, "a", encoding="utf-8") as fh:
            fh.write("[%s] -> %s: %s\r\n" % (time.strftime("%Y-%m-%d %H:%M"), qq, text_out))
    except Exception as e:
        log("send failed for %s: %s" % (qq, str(e)[:120]))


if __name__ == "__main__":
    main()


