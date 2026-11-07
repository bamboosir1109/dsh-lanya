# Export dsh session transcripts into readable Markdown for Dafeiyu.
#
# Why: the raw transcripts live in an inaccessible form - zstd-compressed JSONL
# under <dsh home>\sessions\<workspace>\<session>\session.v4.jsonl.zstd - so
# "she can read every conversation" was true only in theory. This turns them into
# plain Markdown she can actually open.
#
# What it writes: <dafeiyu>\chats\<session-name>.md for every session in the QQ
# workspace, newest last. Each line is "HH:MM 角色: 文本"; tool noise is dropped,
# because the point is what was SAID.
#
# Run by the DSH-Dafeiyu-Chats scheduled task every 10 minutes.

# --- 隐藏控制台窗口 ---------------------------------------------------------
# 由计划任务用 python.exe 启动时，Windows 会先弹一个黑框再跑脚本。用户看到的
# "窗口反复弹出"就是这个。脚本没法阻止窗口被创建，但可以在第一行就把它藏掉，
# 把可见时间从"整个运行时长"压到启动那一瞬。

def _config_path():
    return os.path.join(data_dir(), "lanya-config.json")


def _speaker_name():
    """导出时怎么标"用户"那一侧。

    以前写死成机主的笔名 —— 那是私事，而且这份文件是公开的。
    现在读配置里的 ownerLabel，没配就用中性的 user。
    """
    try:
        with open(_config_path(), "r", encoding="utf-8") as fh:
            return str(json.load(fh).get("ownerLabel") or "user")
    except Exception:
        return "user"


def _find_sessions_dir():
    """找 DSH 的会话目录。

    以前把工作目录编码进目录名里写死，
    等于把绝对路径藏进了文件夹名。现在扫一遍挑可以用的。
    """
    root = os.path.join(DSH_HOME, "sessions")
    try:
        cands = [os.path.join(root, d) for d in os.listdir(root)
                 if os.path.isdir(os.path.join(root, d))]
    except Exception:
        return root
    if not cands:
        return root
    cands.sort(key=lambda d: os.path.getmtime(d), reverse=True)
    return cands[0]


# ---------------------------------------------------------------------------
from project_conf import data_dir
import ctypes as _ct
try:
    _ct.windll.user32.ShowWindow(_ct.windll.kernel32.GetConsoleWindow(), 0)
except Exception:
    pass
# ---------------------------------------------------------------------------
import glob
import json
import os
import re
import time

import zstandard

DSH_HOME = os.path.join(os.path.expanduser("~"), ".dsh")
SESSIONS = _find_sessions_dir()
DAFEIYU = data_dir()
OUT_DIR = os.path.join(DAFEIYU, "chats")

# Session id -> friendly name. QQ sessions are the interesting ones; the others
# are the owner's own dsh conversations (including the one where she was built).
FRIENDLY = {
    "onebot-group-YOUR_GROUP_ID": "群-粉丝群(YOUR_GROUP_ID)",
    "onebot-private-YOUR_ADMIN_QQ": "私聊-<OWNER_NAME>(YOUR_ADMIN_QQ)",
    "onebot-private-FRIEND_QQ": "私聊-熟人(FRIEND_QQ)",
}

MAX_TEXT = 1200          # per message, keeps files skimmable
MAX_MSGS = 400           # newest N messages per session
MIN_MESSAGES = 4         # below this it is a daemon ping, not a conversation

# Plumbing that dsh records as if it were a user turn. None of it was typed by a
# person, so it must not appear in a transcript she reads as "what was said".
NOISE_MARKERS = (
    "<system-reminder>",
    "Current runtime context.",
    "MNEMON RUNTIME MEMORY SNAPSHOT",
    "<runtime-memory-file",
    "[MNEMON]",
    "You are an AI agent powered by DeepSeek Harness",
    "<available_skills>",
    "A skill is a reusable set of task-specific instructions",
    # Test traffic the owner asked to keep out of her readable history: probe
    # messages sent while wiring up the cross-chat log. Filtering here rather than
    # editing the transcripts means it stays clean on every re-export.
    "[\u8865\u4e01\u63a2\u9488]",
    "[\u53d1\u56fe\u81ea\u68c0]",
    "\u6062\u590d\u81ea\u68c0",
    "[\u97f3\u8272\u8bd5\u542c]",
    "[\u8c03\u97f3\u00b7",
)


def is_noise(text):
    head = text.lstrip()[:400]
    for m in NOISE_MARKERS:
        if m in head:
            return True
    return False


def text_of(msg):
    """Pull plain text out of a dsh message object."""
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
            parts.append(c["text"])
    return "\n".join(parts)


def strip_wrapper(t):
    """Inbound QQ messages arrive wrapped; keep the payload, drop the plumbing."""
    t = re.sub(r"\[(\u7fa4\u6d88\u606f|\u79c1\u804a)[^\]]*\]\s*", "", t)
    t = re.sub(r"\[\d{2}:\d{2}[^\]]*\]\s*", "", t)
    t = re.sub(r"<user_message[^>]*>", "", t)
    t = t.replace("</user_message>", "")
    return t.strip()


def hhmm(ms):
    try:
        return time.strftime("%H:%M", time.localtime(int(ms) / 1000))
    except Exception:
        return "--:--"


def export_one(zpath, name, session_id):
    d = zstandard.ZstdDecompressor()
    try:
        with open(zpath, "rb") as fh:
            raw = d.stream_reader(fh).read().decode("utf-8", "replace")
    except Exception as e:
        return 0, "read failed: %s" % str(e)[:80]

    out = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        typ = ev.get("type")
        if typ not in ("user/message", "assistant/message"):
            continue
        data = ev.get("data") or {}
        msg = data.get("message") or data
        body = text_of(msg)
        if not body.strip():
            continue
        if is_noise(body):
            continue
        body = strip_wrapper(body)[:MAX_TEXT]
        if not body:
            continue
        who = _speaker_name()
        out.append("[%s] %s: %s" % (hhmm(ev.get("time")), who, body.replace("\n", " / ")))

    if not out:
        return 0, "no messages"

    out = out[-MAX_MSGS:]
    header = [
        "# %s" % name,
        "",
        "- session: `%s`" % session_id,
        "- \u5bfc\u51fa\u65f6\u95f4: %s" % time.strftime("%Y-%m-%d %H:%M"),
        "- \u6700\u65b0 %d \u6761\u6d88\u606f\uff08\u5de5\u5177\u8c03\u7528\u5df2\u5254\u9664\uff09" % len(out),
        "",
        "---",
        "",
    ]
    os.makedirs(OUT_DIR, exist_ok=True)
    dst = os.path.join(OUT_DIR, name.replace("/", "_") + ".md")
    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(header + out))
    os.replace(tmp, dst)
    return len(out), "ok"


def main():
    if not os.path.isdir(SESSIONS):
        print("no sessions dir: " + SESSIONS)
        return
    os.makedirs(OUT_DIR, exist_ok=True)
    total = 0
    skipped = 0
    keep_names = set()
    for sub in sorted(os.listdir(SESSIONS)):
        full = os.path.join(SESSIONS, sub)
        if not os.path.isdir(full):
            continue
        # Skip one-shot daemon runs. A bare UUID session is a headless call from one
        # of her own timers (groupwatch, lifeloop, the meme collector...), not a
        # conversation. Exporting those produced hundreds of near-empty files that buried the real conversations. Real chats are onebot-*, and the owner's own
        # GUI sessions start with "session-".
        if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", sub):
            skipped += 1
            continue
        cands = glob.glob(os.path.join(full, "session.v4.jsonl.zstd"))
        if not cands:
            continue
        name = FRIENDLY.get(sub, sub)
        n, note = export_one(cands[0], name, sub)
        if n < MIN_MESSAGES:
            skipped += 1
            continue
        keep_names.add(name.replace("/", "_") + ".md")
        print("%-46s %s (%d)" % (name, note, n))
        total += n

    # Remove exports that the current filter no longer produces, so the folder
    # reflects the policy instead of accumulating history.
    for f in glob.glob(os.path.join(OUT_DIR, "*.md")):
        if os.path.basename(f) not in keep_names:
            try:
                os.remove(f)
            except Exception:
                pass

    print("total messages exported: %d -> %s (skipped %d non-conversations)"
          % (total, OUT_DIR, skipped))


if __name__ == "__main__":
    main()


