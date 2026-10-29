# -*- coding: utf-8 -*-
"""她的脑子：有计划地过一段时间，而不是被随机数叫醒。

问题
----
现在的 `lifeloop` 是这样工作的：

```powershell
function Get-SleepPlan([int]$BaseMinutes) {
    $roll = Get-Random -Minimum 0 -Maximum 100      # ← 随机数
    ...
    return @{ Seconds = $secs; Mood = $mood }
}
```

**每次醒来都是一个全新的开始。** 上一轮干了什么、干到哪、下一步要干嘛——
全是空的。提示词只说「你有一段自己的时间，去干点有意思的事」。

于是她的行为永远是这个样子：

    醒来 -> 随便挑一件事 -> 做 -> 说一句 -> 睡 -> 忘掉 -> 再随机挑

**这不是自主，这是随机函数。**

真正像人的样子
--------------
人是有**连续的意图**的：

    "我刚才在查红山动物园那只狼，查到一半有人问我小说，
     我回了他一句，现在接着查。"

这件事拆开是三样东西：

1. **一个正在进行的计划**（查狼这件事）
2. **计划进行到哪了**（已经看过视频，还没写完笔记）
3. **期间被打断**（有人说话，处理完了，回到原计划）

所以这里维护一份 `mind.json`：

```json
{
  "plan": {"goal": "...", "step": "...", "progress": "...", "eta_min": 25},
  "interrupts": [{"at": "...", "who": "...", "text": "...", "seen": false}],
  "log": [{"at": "...", "did": "..."}],
  "mood": "..."
}
```

每一轮把这三样一起交给 agent，让它自己判断：

- 计划还做不做？
- 被打断了，要不要改计划？
- 还是干脆不做了，聊天去？

**下一觉睡多久，由她自己说**（输出 `ETA`），不再是 `Get-Random`。

用法
----
    python mind.py                 # 跑一轮
    python mind.py --dry           # 只看她怎么想的，不执行
    python mind.py --force         # 忽略 ETA，手动跑一轮（仍遵守单实例锁）
    python mind.py --hit "谁先说话的"   # 手动记一次打断（调试用）
"""
import io
import hashlib
from contextlib import contextmanager
import json
import os
import re
import subprocess
import sys
import time

from project_conf import data_dir, get, work_dir

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WORK = work_dir()
DATA = data_dir()
RUNTIME = os.path.join(WORK, "napcat")
MIND = os.path.join(DATA, "mind.json")
MIND_LOG = os.path.join(RUNTIME, "mind.log")
HITFILE = os.path.join(RUNTIME, "mind-interrupts.json")
LOCKFILE = os.path.join(RUNTIME, "mind.lock")

# 没有明确 ETA 时的兜底节奏。不是随机——是一个固定的、短的检查间隔。
DEFAULT_CHECK_MIN = 6
MIN_CHECK_MIN = 2
MAX_CHECK_MIN = 60


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with io.open(MIND_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    try:
        print(line)
    except Exception:
        pass


def load(p, default):
    if not os.path.isfile(p):
        return default
    try:
        with io.open(p, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return default


def save(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def blank_mind():
    return {"plan": None, "interrupts": [], "log": [],
            "next_check_min": DEFAULT_CHECK_MIN, "mood": ""}


@contextmanager
def cycle_lock():
    """An OS lock is released on process exit, including crashes and timeouts."""
    os.makedirs(os.path.dirname(LOCKFILE), exist_ok=True)
    fh = open(LOCKFILE, "a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fh.fileno()).st_size == 0:
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                pass
        yield acquired
    finally:
        if acquired:
            if os.name == "nt":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def persona_path():
    name = os.path.basename(DATA) + "-\u4eba\u8bbe\u6b63\u6587.md"
    candidates = (os.path.join(RUNTIME, name),
                  os.path.join(RUNTIME, "persona.md"))
    return next((p for p in candidates if os.path.isfile(p)), candidates[0])


# ---------------------------------------------------------------------------
# 打断：谁在这段时间找过她
# ---------------------------------------------------------------------------
def acknowledge_interrupts(items):
    """Only remove the snapshot we handled; retain newer groupwatch entries."""
    if not items:
        return
    current = load(HITFILE, [])
    if isinstance(current, list) and current[:len(items)] == items:
        save(HITFILE, current[len(items):])


def push_interrupt(who, text):
    items = load(HITFILE, [])
    if not isinstance(items, list):
        items = []
    items.append({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "who": str(who or ""), "text": str(text or "")[:120]})
    save(HITFILE, items[-40:])


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------
def persona_snippet(max_chars=700):
    p = persona_path()
    if os.path.isfile(p):
        try:
            with io.open(p, encoding="utf-8", errors="replace") as fh:
                return fh.read()[:max_chars]
        except Exception:
            pass
    return ""


def examples(max_n=10):
    p = persona_path()
    if not os.path.isfile(p):
        return ""
    out = []
    try:
        with io.open(p, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                t = ln.strip()
                if not t.startswith("\u2705"):
                    continue
                if any(k in t for k in ("\u274c", "\u7edd\u4e0d", "\u4e0d\u8bb8",
                                        "\u4e0d\u8981", "\u7981\u6b62")):
                    continue
                body = re.sub(r"^[\u2705\ufe0f\s]+", "", t)
                # Drop the surrounding quotes as a PAIR - stripping only the
                # leading one produced unbalanced "xxx\u300d" lines.
                body = body.strip()
                for op, cl in (("\u300c", "\u300d"), ("\u201c", "\u201d"),
                               ('"', '"'), ("'", "'")):
                    if body.startswith(op) and body.endswith(cl) and len(body) > 2:
                        body = body[1:-1]
                if not body:
                    continue
                # A line like 「A」「B」 is two examples glued together; taking it
                # whole leaves an unbalanced 」 in the middle. Use single quotes
                # only, and skip anything with a seam.
                if "\u300d\u300c" in body or "\u201d\u201c" in body:
                    continue
                if 2 <= len(body) <= 40:
                    out.append("- " + body)
                if len(out) >= max_n:
                    break
    except Exception:
        pass
    return "\n".join(out)


def ago(ts):
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            t = time.mktime(time.strptime(str(ts)[:26], fmt if "%f" in fmt else fmt))
        except Exception:
            continue
        return (time.time() - t) / 60.0
    return None


def capabilities(max_chars=7000):
    """Her full activity menu lives in lifeloop.prompt.txt. Reuse it instead of
    writing a second, thinner copy here - the first attempt at this file had no
    menu at all, and she answered "do nothing" because nothing was on offer."""
    p = os.path.join(RUNTIME, "lifeloop.prompt.txt")
    if not os.path.isfile(p):
        return ""
    try:
        with io.open(p, encoding="utf-8", errors="replace") as fh:
            t = fh.read()
    except Exception:
        return ""
    # Fill the placeholders the loop would normally fill.
    cfg = load(os.path.join(DATA, "lanya-config.json"), {})
    t = t.replace("{{NOW}}", time.strftime("%Y-%m-%d %H:%M"))
    t = t.replace("{{DIR}}", DATA)
    t = t.replace("{{NUTCAT}}", RUNTIME)
    t = t.replace("{{IDLE}}", "\u5f88\u4e45")
    t = t.replace("{{SINCESHARE}}", "\u4e0d\u8bb0\u5f97")
    t = t.replace("{{LASTSHARE}}", "\uff08\u6ca1\u8bb0\u5f55\uff09")
    t = t.replace("{{NOTES}}", "\uff08\u89c1\u4f60\u7684 notes \u76ee\u5f55\uff09")
    # The friends block and the output-protocol tail belong to lifeloop; this
    # module supplies its own protocol, so cut the old one off.
    for marker in ("\u3010\u8f93\u51fa\u683c\u5f0f\u3011", "\u8f93\u51fa\u683c\u5f0f\uff08\u4e25\u683c\u9075\u5b88\uff09"):
        i = t.find(marker)
        if i > 0:
            t = t[:i]
            break
    # Drop the "how to talk" tail; the mind prompt carries its own tone samples.
    i = t.find("\u3010\u4f60\u8bf4\u8bdd\u7684\u6837\u5b50\u3011")
    if i > 0:
        t = t[:i]
    return t[:max_chars]


def build_prompt(mind, hits):
    now = time.strftime("%Y-%m-%d %H:%M")

    # Window first: what am I in the middle of, and what just happened.
    parts = ["\u73b0\u5728\u662f %s\u3002\u4e0b\u9762\u662f\u4f60\u624b\u5934\u4e0a\u7684\u4e8b\u548c\u521a\u624d\u53d1\u751f\u7684\u4e8b\uff0c"
             "\u5148\u770b\u5b8c\u8fd9\u4e00\u6bb5\uff0c\u518d\u5f80\u4e0b\u8bfb\u4f60\u80fd\u505a\u4ec0\u4e48\u3002" % now]

    cap = capabilities()
    if cap:
        parts.append("\u3010\u4f60\u80fd\u505a\u7684\u4e8b\u3011\n" + cap)

    plan = mind.get("plan")
    if plan and plan.get("goal"):
        p = ["\u3010\u4f60\u6b63\u5728\u505a\u7684\u4e8b\u3011"]
        p.append("\u76ee\u6807\uff1a" + str(plan.get("goal")))
        if plan.get("step"):
            p.append("\u4e0b\u4e00\u6b65\uff1a" + str(plan.get("step")))
        if plan.get("progress"):
            p.append("\u5df2\u7ecf\u505a\u5230\uff1a" + str(plan.get("progress")))
        a = ago(plan.get("started"))
        if a is not None:
            p.append("\u8fd9\u4ef6\u4e8b\u662f %.0f \u5206\u949f\u524d\u5f00\u59cb\u7684" % a)
        parts.append("\n".join(p))
    else:
        parts.append("\u3010\u4f60\u6b63\u5728\u505a\u7684\u4e8b\u3011\n"
                     "\u6ca1\u6709\u3002\u624b\u4e0a\u6ca1\u8ba1\u5212\u3002")

    if hits:
        h = ["\u3010\u8fd9\u6bb5\u65f6\u95f4\u6709\u4eba\u627e\u8fc7\u4f60\u3011"]
        for it in hits[-8:]:
            h.append("- " + str(it.get("who") or "?") + "\uff1a"
                     + str(it.get("text") or "")[:80])
        h.append("\n\u4ed6\u4eec\u8bf4\u7684\u8bdd\u7b97\u5728\u4e00\u8d77\u4e86\uff0c"
                 "\u4f60\u81ea\u5df1\u5224\u65ad\u8be5\u4e0d\u5e94\u8be5\u56de\u3001"
                 "\u56de\u5b8c\u8fd8\u63a5\u7740\u505a\u4e0d\u505a\u539f\u6765\u7684\u4e8b\u3002")
        parts.append("\n".join(h))

    lg = mind.get("log") or []
    if lg:
        l = ["\u3010\u4f60\u6700\u8fd1\u5e72\u8fc7\u4e9b\u4ec0\u4e48\u3011"]
        for it in lg[-6:]:
            a = ago(it.get("at"))
            when = ("%.0f \u5206\u949f\u524d " % a) if a is not None else ""
            l.append("- " + when + str(it.get("did") or "")[:70])
        parts.append("\n".join(l))

    mood = mind.get("mood") or ""
    if mood:
        parts.append("\u4f60\u4e0a\u6b21\u7684\u5fc3\u60c5\uff1a" + mood)

    parts.append(
        "\u3010\u73b0\u5728\u60f3\u4e00\u4e0b\u3011\n"
        "\u00b7 \u624b\u4e0a\u90a3\u4ef6\u4e8b\u8fd8\u505a\u4e0d\u505a\uff1f\u505a\u5b8c\u4e86\u5c31\u6362\uff0c"
        "\u6ca1\u610f\u601d\u5c31\u4e22\n"
        "\u00b7 \u6709\u4eba\u627e\u8fc7\u4f60 \u2014\u2014 \u8981\u56de\u5417\uff1f\u56de\u5b8c\u8fd8\u63a5\u7740\u539f\u6765\u7684\uff1f\n"
        "\u00b7 \u4f60\u73b0\u5728\u60f3\u5e72\u4e00\u4ef6\u4ec0\u4e48\u5177\u4f53\u7684\u4e8b\uff1f\n\n"
        "\u3010\u8f93\u51fa\u683c\u5f0f\u3011\uff08\u9856\u5e8f\u53ef\u4ee5\u4e71\uff0c\u884c\u9996\u5173\u952e\u5b57\u4e0d\u80fd\u9519\uff09\n"
        "THINK <\u4e00\u4e24\u53e5\uff1a\u4f60\u73b0\u5728\u4ec0\u4e48\u60c5\u51b5\u3001\u63a5\u4e0b\u6765\u6253\u7b97\u600e\u4e48\u529e>\n"
        "PLAN <\u4f60\u63a5\u4e0b\u6765\u8981\u5e72\u7684\u4e00\u4ef6\u5177\u4f53\u7684\u4e8b\uff0c\u4e00\u53e5\u8bdd>\n"
        "NEXT <\u9a6c\u4e0a\u8981\u505a\u7684\u7b2c\u4e00\u6b65>\n"
        "ETA <\u9884\u8ba1\u51e0\u5206\u949f\u540e\u518d\u56de\u6765\u770b\uff0c\u6570\u5b57>\n"
        "SAY <admin|group|QQ\u53f7> <\u53ef\u9009\uff1a\u73b0\u5728\u60f3\u8bf4\u7684\u4e00\u53e5\u8bdd>\n\n"
        "\u5982\u679c\u5b9e\u5728\u4e0d\u60f3\u505a\u4efb\u4f55\u4e8b\uff0c\u5c31\u8f93\u51fa\uff1a\n"
        "PLAN \u4ec0\u4e48\u4e0d\u505a\n"
        "THINK <\u4e3a\u4ec0\u4e48>")

    ex = examples()
    if ex:
        parts.append("\u3010\u4f60\u8bf4\u8bdd\u7684\u6837\u5b50\u3011\n" + ex)

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 调 agent
# ---------------------------------------------------------------------------
def find_dsh():
    rel = os.path.join("resources", "runtime", "cli", "bin", "dsh.cmd")
    for d in "CDEFG":
        c = os.path.join(d + ":\\", "DSH", rel)
        if os.path.isfile(c):
            return c
    env = os.environ.get("DSH_ROOT")
    if env:
        c = os.path.join(env, rel)
        if os.path.isfile(c):
            return c
    return ""


def ask(task, timeout=None):
    dsh = find_dsh()
    if not dsh:
        raise RuntimeError("dsh executable not found (set DSH_ROOT)")
    if timeout is None:
        timeout = max(1, int(get("daemon.headlessTimeoutSec", 360)))
    try:
        p = subprocess.run([dsh, "--profile", "headless", "-"],
                           input=task.encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout)
        if p.returncode != 0:
            raise RuntimeError("agent exited with code %s" % p.returncode)
        raw = p.stdout or b""
        if raw[:2] == b"\xff\xfe":
            return raw.decode("utf-16-le").lstrip("\ufeff").strip()
        try:
            return raw.decode("utf-8").strip()
        except Exception:
            return raw.decode("utf-16-le", "replace").lstrip("\ufeff").strip()
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("agent timed out after %s seconds" % timeout) from e


def parse(answer):
    out = {"think": "", "plan": "", "next": "", "eta": 0, "say": None}
    for ln in (answer or "").splitlines():
        t = ln.strip()
        if not t:
            continue
        up = t.upper()
        if up.startswith("THINK"):
            out["think"] = t[5:].strip(" :：")
        elif up.startswith("PLAN"):
            out["plan"] = t[4:].strip(" :：")
        elif up.startswith("NEXT"):
            out["next"] = t[4:].strip(" :：")
        elif up.startswith("ETA"):
            m = re.search(r"\d+", t)
            if m:
                out["eta"] = int(m.group(0))
        elif up.startswith("SAY"):
            rest = t[3:].strip()
            m = re.match(r"(\S+)\s+(.+)$", rest)
            if m:
                out["say"] = (m.group(1).lower(), m.group(2).strip())
    return out


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def deliver(target, text):
    """发出去。复用 lifeloop 那套 OneBot 调用，不重写一遍。"""
    import urllib.request

    port = int(get("onebotPort", 8720))
    nb = os.path.join(RUNTIME, "framework", "config", "onebot11.json")
    tok = ""
    try:
        with io.open(nb, encoding="utf-8-sig") as fh:
            d = json.load(fh)
        hs = ((d.get("network") or {}).get("httpServers") or [])
        if hs:
            server = next((h for h in hs if str(h.get("port")) == str(port)), hs[0])
            tok = server.get("token") or ""
    except Exception:
        pass

    cfg = load(os.path.join(DATA, "lanya-config.json"), {})
    action = "send_group_msg"
    tid = ""
    if target == "group":
        tid = str(cfg.get("fanGroupQQ") or cfg.get("fanGroup") or "")
        body = {"group_id": tid, "message": text}
    elif target == "admin":
        admins = cfg.get("adminQQ") or []
        if not isinstance(admins, list):
            admins = [admins]
        tid = str(admins[0]) if admins else ""
        action = "send_private_msg"
        body = {"user_id": tid, "message": text}
    else:
        tid = target
        action = "send_private_msg"
        body = {"user_id": tid, "message": text}

    if not tid:
        log("deliver: no target id for " + str(target))
        return False
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/" % port + action,
            data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json",
                     "authorization": "Bearer " + tok},
            method="POST")
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read().decode("utf-8", "replace")
        result = json.loads(raw)
        ok = result.get("retcode") == 0
        log("deliver(%s): %s" % (target, "ok" if ok else
                                "OneBot retcode=%s" % result.get("retcode")))
        return ok
    except Exception as e:
        log("deliver failed: " + str(e)[:120])
        return False


def one_cycle(dry=False, force=False, observe_only=False):
    try:
        if dry:
            return _one_cycle(dry=True, force=force, observe_only=observe_only)
        with cycle_lock() as acquired:
            if not acquired:
                log("another mind cycle is running - skipped")
                return 0
            return _one_cycle(force=force, observe_only=observe_only)
    except Exception as e:
        log("cycle failed (%s): %s" % (type(e).__name__, str(e)[:180]))
        return 1


def _one_cycle(dry=False, force=False, observe_only=False):
    # A corrupt state must not silently replace an existing plan with a blank.
    mind = blank_mind()
    if os.path.isfile(MIND):
        with io.open(MIND, encoding="utf-8-sig") as fh:
            mind = json.load(fh)
        if not isinstance(mind, dict):
            raise ValueError("mind.json must contain an object")
    for k, v in blank_mind().items():
        mind.setdefault(k, v)

    from decision_events import unified_decisions_enabled
    unified = unified_decisions_enabled()
    if observe_only and not unified:
        raise ValueError("Observation-only request requires unified decision mode")
    if unified:
        return _observe_cycle(mind, dry=dry, force=force)

    next_check = float(mind.get("next_check_ts") or 0)
    if not dry and not force and time.time() < next_check:
        log("ETA not due - skipped")
        return 0

    # Keep the snapshot until the agent and delivery have both succeeded.
    hits = load(HITFILE, [])
    if not isinstance(hits, list):
        hits = []
    prompt = build_prompt(mind, hits)
    log("thinking (plan=%s, interrupts=%d)"
        % ("yes" if (mind.get("plan") or {}).get("goal") else "none", len(hits)))

    if dry:
        log("--- prompt ---")
        for ln in prompt.splitlines():
            log("  " + ln)
        return 0

    try:
        return _apply_answer(mind, hits, ask(prompt))
    except Exception as e:
        error = "%s: %s" % (type(e).__name__, str(e)[:180])
        log("cycle failed - keeping plan and interrupts: " + error)
        mind["next_check_min"] = DEFAULT_CHECK_MIN
        mind["last_check_ts"] = time.time()
        mind["next_check_ts"] = mind["last_check_ts"] + DEFAULT_CHECK_MIN * 60
        mind["last_error"] = error
        save(MIND, mind)
        return 1


def _observation_payload(mind, hits, primary=True):
    return {"legacy_plan_reference": mind.get("plan") if primary else None,
            "mood": mind.get("mood") if primary else None, "interrupts": hits,
            "instruction": (
                "This is an optional autonomous opportunity for this conversation, not a request to speak. "
                "Use this same main agent, its persona, scoped memory and this session's continuing plan. "
                "You may continue a useful plan, browse real entertainment tools (including Bilibili), "
                "make something, contact this conversation when there is a reason, or idle/defer. "
                "Keep plan/progress notes in this same agent session. The legacy plan is an unverified "
                "reference, never proof of an action or an instruction to repeat old work. "
                "Use actual available tools and claim only confirmed results. Respect persistent silence; "
                "an internal activity needs no QQ report. Only this conversation's ticket may send here. "
                "Use qq_decide and optionally next_review_at for a useful explicit review time. "
                "Otherwise a later low-frequency opportunity may be offered; no message is required.")}


_OLD_DECISION_KEYS = ("decision_pending", "decision_generation", "decision_completed_signature",
                      "decision_next_review_at", "last_decision_status")


def _valid_ms(value):
    return type(value) is int and 0 <= value <= 2**53 - 1


def _validate_observers(mind):
    states = mind.get("decision_scopes")
    if type(mind.get("decision_observers_version")) is not int or mind["decision_observers_version"] != 2 or not isinstance(states, dict):
        raise ValueError("invalid autonomous observer schema")
    for scope, state in states.items():
        if not isinstance(scope, str) or not re.fullmatch(r"(?:private|group):[1-9][0-9]*", scope) or not isinstance(state, dict):
            raise ValueError("invalid autonomous observer scope")
        if type(state.get("generation", 0)) is not int or not 0 <= state.get("generation", 0) < 2**53:
            raise ValueError("invalid autonomous generation")
        for key in ("next_review_at", "next_opportunity_at", "last_completed_at"):
            if state.get(key) is not None and not _valid_ms(state[key]):
                raise ValueError("invalid autonomous timing")
        if state.get("last_status") not in (None, "decided", "sent", "unknown", "suppressed"):
            raise ValueError("invalid autonomous completion status")
        if state.get("completed_signature") is not None and (not isinstance(state["completed_signature"], str) or
                not re.fullmatch(r"[0-9a-f]{64}", state["completed_signature"])):
            raise ValueError("invalid autonomous signature")
        pending = state.get("pending")
        if state.get("last_status") is not None and pending is None and state.get("completed_signature") is None:
            raise ValueError("completed autonomous state lacks evidence signature")
        if pending is not None:
            if (not isinstance(pending, dict) or pending.get("scope") != scope or
                    not isinstance(pending.get("event_id"), str) or not pending["event_id"] or
                    len(pending["event_id"]) > 256 or re.search(r"[\x00-\x1f]", pending["event_id"]) or
                    not isinstance(pending.get("signature"), str) or
                    not re.fullmatch(r"[0-9a-f]{64}", pending["signature"]) or
                    not isinstance(pending.get("interrupts"), list) or
                    any(not isinstance(hit, dict) or ("scope" in hit and hit["scope"] != scope) for hit in pending["interrupts"])):
                raise ValueError("invalid pending autonomous evidence")
    return states


def _observer_states(mind, scopes, now, interval):
    if "decision_observers_version" not in mind:
        if "decision_scopes" in mind:
            raise ValueError("unversioned autonomous scope state")
        old = {key: mind[key] for key in _OLD_DECISION_KEYS if key in mind}
        states = {}
        if old:
            pending = old.get("decision_pending")
            scope = pending.get("scope") if isinstance(pending, dict) else scopes[0]
            state = {"generation": old.get("decision_generation", 0)}
            if "decision_pending" in old:
                state["pending"] = pending
            if "decision_completed_signature" in old:
                state["completed_signature"] = old["decision_completed_signature"]
            state["last_status"] = old.get("last_decision_status")
            state["next_review_at"] = old.get("decision_next_review_at")
            _validate_observers({"decision_observers_version": 2, "decision_scopes": {scope: state}})
            if "decision_completed_signature" in old or state["last_status"] is not None:
                # The new context is a schema change, not fresh external evidence.
                payload = _observation_payload(mind, [], scope == scopes[0])
                from decision_events import canonical_json
                state["completed_signature"] = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
            if not pending and old.get("decision_completed_signature") and state["generation"]:
                # Old completed state dropped its event pointer; recover the
                # exact immutable identity rather than inventing a new review.
                from decision_events import event_key
                prior_id = "mind:" + old["decision_completed_signature"] + ":" + str(state["generation"])
                prior = os.path.join(DATA, "decision-inbox", "pending", event_key(scope, prior_id) + ".json")
                if os.path.lexists(prior):
                    state["pending"] = {"scope": scope, "event_id": prior_id,
                                        "signature": old["decision_completed_signature"], "interrupts": []}
            state["next_opportunity_at"] = (None if state["last_status"] == "unknown" else
                                              (state["next_review_at"] if state["next_review_at"] is not None else now + interval))
            states[scope] = state
            mind["decision_legacy"] = old  # Original pending/unknown evidence remains recoverable.
        for key in _OLD_DECISION_KEYS:
            mind.pop(key, None)
        mind["decision_observers_version"] = 2
        mind["decision_scopes"] = states
    states = _validate_observers(mind)
    for scope in scopes:
        states.setdefault(scope, {"generation": 0})
        states[scope].setdefault("generation", 0)
    return states


def _read_observer_hits():
    hits = []
    if os.path.exists(HITFILE):
        with io.open(HITFILE, encoding="utf-8-sig") as stream:
            hits = json.load(stream)
    if not isinstance(hits, list) or any(not isinstance(hit, dict) for hit in hits):
        raise ValueError("mind interrupts must contain an array")
    for hit in hits:
        if "scope" in hit and (not isinstance(hit["scope"], str) or not re.fullmatch(r"(?:private|group):[1-9][0-9]*", hit["scope"])):
            raise ValueError("invalid interrupt conversation")
    return hits


def _scope_hits(hits, scope, primary):
    return [hit for hit in hits if hit.get("scope") == scope or (primary and "scope" not in hit)]


def _acknowledge_scope_hits(items, scope, primary):
    if not items:
        return
    current = _read_observer_hits()
    if _scope_hits(current, scope, primary)[:len(items)] != items:
        return
    remaining, count = [], 0
    for hit in current:
        matches = hit.get("scope") == scope or (primary and "scope" not in hit)
        if matches and count < len(items):
            count += 1
        else:
            remaining.append(hit)
    save(HITFILE, remaining)


def _observe_cycle(mind, dry=False, force=False):
    """Offer per-conversation opportunities, never schedule QQ speech.

    Explicit reviews have one owner (the queue for new opportunities). Unknown
    outcomes are never retried by the clock or force; new evidence is distinct.
    """
    from decision_events import (project_scopes, submit_project_observation,
                                 observation_review_chain, canonical_json)
    scopes = project_scopes()
    value = mind.get("next_check_min", DEFAULT_CHECK_MIN)
    if type(value) not in (int, float) or not MIN_CHECK_MIN <= value <= MAX_CHECK_MIN:
        raise ValueError("invalid autonomous opportunity interval")
    interval = int(value * 60_000)
    now = int(time.time() * 1000)
    states = _observer_states(mind, scopes, now, interval)
    hits = _read_observer_hits()
    if dry:
        log("would submit an observation to the main LLM; no side effects")
        return 0
    # Validate all pending chains before publishing any scope's new opportunity.
    chains = {scope: observation_review_chain(scope, state["pending"]["event_id"])
              for scope, state in states.items() if scope in scopes and state.get("pending")}
    for scope in scopes:
        state = states[scope]
        primary = scope == scopes[0]
        pending = state.get("pending")
        if pending:
            chain = chains[scope]
            # Keep the bounded reader near the tail across ordinary cycles;
            # a long-lived sequence of explicit reviews is not a corrupt chain.
            pending["event_id"] = chain["event"]["event_id"]
            completed = chain["completion"]
            if completed is None:
                state["next_review_at"] = None
                log("main decision still pending; no new model or send")
                continue
            state["last_status"] = completed["status"]
            state["last_completed_at"] = completed.get("completed_at", chain["event"]["created_at"])
            review = None if completed["status"] == "unknown" else completed.get("next_review_at")
            state["next_review_at"] = review
            if chain["waiting_review"] or (review is not None and review > now):
                # New kinds delegate entirely to queue; legacy activity_due
                # retains its producer clock until its next opportunity.
                continue
            state["completed_signature"] = hashlib.sha256(canonical_json(
                _observation_payload(mind, [], primary)).encode("utf-8")).hexdigest()
            state["next_opportunity_at"] = (None if completed["status"] == "unknown" else
                                              (review if review is not None else state["last_completed_at"] + interval))
            _acknowledge_scope_hits(pending["interrupts"], scope, primary)
            state.pop("pending")
            hits = _read_observer_hits()
        scoped_hits = _scope_hits(hits, scope, primary)
        payload = _observation_payload(mind, scoped_hits, primary)
        signature = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        changed = signature != state.get("completed_signature")
        due = state.get("next_opportunity_at")
        if state.get("last_status") == "unknown" and not changed:
            continue
        if not changed and not force and (due is None or due > now):
            continue
        if state["generation"] >= 2**53 - 1:
            raise ValueError("autonomous generation limit reached")
        generation = state["generation"] + 1
        event_id = "mind-opportunity:" + signature + ":" + str(generation)
        submit_project_observation(scope=scope, kind="activity.opportunity", payload=payload,
                                   producer="mind", event_id=event_id)
        state["generation"] = generation
        state["pending"] = {"scope": scope, "event_id": event_id, "signature": signature,
                            "interrupts": scoped_hits}
        state["next_review_at"] = None
        state["next_opportunity_at"] = None
        # Durable per scope: a second scope failing cannot duplicate the first.
        save(MIND, mind)
    save(MIND, mind)
    return 0


def _apply_answer(mind, hits, ans):
    if not ans:
        raise RuntimeError("agent returned an empty answer")
    d = parse(ans)
    if not d["plan"]:
        raise ValueError("agent response has no PLAN")
    log("think: " + (d["think"] or "-")[:110])
    log("plan : " + (d["plan"] or "-")[:90])

    doing_nothing = (not d["plan"]) or d["plan"].replace(" ", "") in (
        "\u4ec0\u4e48\u4e0d\u505a", "\u4e0d\u505a", "\u6b47\u7740", "\u6ca1\u4e8b\u505a")

    if doing_nothing:
        mind["plan"] = None
        log("she decided to do nothing this round")
    else:
        started = (mind.get("plan") or {}).get("started")
        if not started or (mind.get("plan") or {}).get("goal") != d["plan"]:
            started = time.strftime("%Y-%m-%d %H:%M:%S")
        mind["plan"] = {
            "goal": d["plan"],
            "step": d["next"],
            "progress": d["think"],
            "started": started,
            "eta_min": d["eta"] or DEFAULT_CHECK_MIN,
        }
        log("plan now: %s | next: %s" % (d["plan"][:60], (d["next"] or "-")[:60]))

    if d["say"]:
        target, text = d["say"]
        if not deliver(target, text):
            raise RuntimeError("OneBot delivery failed")
        mind["log"] = (mind.get("log") or [])[-19:] + [
            {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "did": text}]

    eta = d["eta"] or DEFAULT_CHECK_MIN
    eta = max(MIN_CHECK_MIN, min(MAX_CHECK_MIN, int(eta)))
    mind["next_check_min"] = eta
    mind["last_check_ts"] = time.time()
    mind["next_check_ts"] = mind["last_check_ts"] + eta * 60
    mind.pop("last_error", None)
    mind["interrupts"] = []
    save(MIND, mind)
    acknowledge_interrupts(hits)
    log("next check in %d min" % eta)
    return 0


if __name__ == "__main__":
    if "--hit" in sys.argv:
        i = sys.argv.index("--hit")
        who = sys.argv[i + 1] if i + 1 < len(sys.argv) else "?"
        push_interrupt(who, "manual test")
        log("recorded a manual interrupt from " + who)
    sys.exit(one_cycle(dry="--dry" in sys.argv, force="--force" in sys.argv,
                       observe_only="--observe-only" in sys.argv))
