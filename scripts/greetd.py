# Proactive greetings, restricted to people she ALREADY has a real memory of.
#
# Why the restriction: the owner asked for "主动招呼，但是需要保证这个人是之前已经
# 聊过有记忆的人". So a candidate must have BOTH:
#   - a profile file  <lanya>\people\<qq>.md  (this is what she writes after a real
#     exchange), AND
#   - an actual message from or to them somewhere in her readable history.
# Having a profile is not enough on its own: she could have created one from a
# single passing hello with nothing to say.
#
# Everything else - daytime only, a low daily cap, "do not repeat the same person
# on consecutive days" - exists to keep this from becoming spam.
#
# Registered as DSH-Lanya-Greet (hourly; it decides for itself whether to act).

# --- 隐藏控制台窗口 ---------------------------------------------------------
# 由计划任务用 python.exe 启动时，Windows 会先弹一个黑框再跑脚本。用户看到的
# "窗口反复弹出"就是这个。脚本没法阻止窗口被创建，但可以在第一行就把它藏掉，
# 把可见时间从"整个运行时长"压到启动那一瞬。

# ---------------------------------------------------------------------------
from project_conf import data_dir, work_dir
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
# The OneBot token is a live credential; it does not belong in source, where
# every copy of the file carries it. Read it from NapCat's own config instead.
TOKEN = ""
try:
    _nb = os.path.join(work_dir(), "napcat", "framework", "config", "onebot11.json")
    if os.path.isfile(_nb):
        with open(_nb, encoding="utf-8-sig") as _fh:
            _nd = json.load(_fh)
        _hs = ((_nd.get("network") or {}).get("httpServers") or [])
        if _hs:
            TOKEN = str(_hs[0].get("token") or "")
except Exception:
    pass
ADMIN = ""          # the owner; she should not "greet" him like a stranger
# Read it from config - it is identity, not source.
try:
    _cp2 = os.path.join(data_dir(), "lanya-config.json")
    if os.path.isfile(_cp2):
        with open(_cp2, encoding="utf-8-sig") as _fh2:
            _cd2 = json.load(_fh2)
        _a = _cd2.get("adminQQ") or []
        if isinstance(_a, list):
            ADMIN = str(_a[0]) if _a else ""
        elif _a:
            ADMIN = str(_a)
except Exception:
    pass

LANYA = data_dir()
PEOPLE = os.path.join(LANYA, "people")
CHATS = os.path.join(LANYA, "chats")
PROMPT = os.path.join(work_dir(), "napcat", "greet.prompt.txt")
STATE = os.path.join(LANYA, "greet.state.json")
LOG = os.path.join(LANYA, "greet.log")
SENT = os.path.join(LANYA, "sent-log.txt")

# DSH install root - found, not assumed, so the repo carries no drive letter.
DSH = ""
try:
    _rel = os.path.join("resources", "runtime", "cli", "bin", "dsh.cmd")
    for _d in "CDEFG":
        _c = _d + ":\\DSH"
        if os.path.isfile(os.path.join(_c, _rel)):
            DSH = os.path.join(_c, _rel)
            break
except Exception:
    pass
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
    """QQ-number filenames only - she once saved one as admin.md and it never matched."""
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
    os.makedirs(LANYA, exist_ok=True)
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

    from decision_events import unified_decisions_enabled, submit_project_observation
    if unified_decisions_enabled():
        for candidate, _profile in cands:
            submit_project_observation(
                scope="private:" + candidate, kind="social_review", producer="greet",
                event_id="greet:" + today + ":" + candidate,
                payload={"date": today, "prior_conversation": True,
                         "instruction": "Consider whether a new reason to contact this known person exists. No greeting is required; idle is valid."})
        log("queued social observations; no independent model or direct send")
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


