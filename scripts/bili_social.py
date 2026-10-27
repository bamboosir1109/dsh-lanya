# -*- coding: utf-8 -*-
"""B 站社交：有人回她评论、@ 她、私信她，她会接话。

问题
----
MCP 里其实**早就有**这三个能力：

    bili_received_replies       收到的评论回复 / 私信
    bili_received_at_and_likes  收到的 @ 和点赞
    bili_send_message           给别人发私信

但 `bili_onerun.py` 只做「刷视频 → 写评论」这一件事，
**从来不读这些通知**。所以 B 站上有人回她、@ 她，她一声不吭。

改法
----
这个脚本做一轮：

1. 读 `bili_received_replies` / `bili_received_at_and_likes`
2. 过滤掉自己发的、已经回过的（记在 `bili-social-seen.json`）
3. 对每一条，让 agent 判断该不该接、怎么接
4. 该接就接：
   - 是评论回复 -> `bili_reply` 在原视频下发
   - 是私信     -> `bili_send_message`
5. 写进记忆，这样下次记得跟这个人聊过

护栏
----
- 每轮最多回 `max_per_run` 条（默认 3）
- 同一个人的同一句话只回一次
- 正经求助（安全 / 医疗 / 严重情绪问题）时切换成认真模式
- 拿不准就不回，宁可少回也别回错
"""
import io
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.dirname(HERE)
DATA = os.path.join(WORK, "\u5c9a\u5a05")
STATE = os.path.join(HERE, "bili-social-seen.json")
LOG = os.path.join(HERE, "bili-social.log")

sys.path.insert(0, HERE)


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with io.open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    try:
        print(line)
    except Exception:
        pass


def load_json(p, default):
    if not os.path.isfile(p):
        return default
    try:
        with io.open(p, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json(p, data):
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def cfg():
    for dn in ("\u5c9a\u5a05", "\u5927\u80a5\u9c7c"):
        p = os.path.join(WORK, dn, "lanya-config.json")
        if os.path.isfile(p):
            return load_json(p, {})
    return {}


# ---------------------------------------------------------------------------
# 人设片段：让她说话像她自己，不是像客服
# ---------------------------------------------------------------------------
def persona_snippet(max_chars=1800):
    for name in ("\u5c9a\u5a05-\u4eba\u8bbe\u6b63\u6587.md",):
        p = os.path.join(HERE, name)
        if os.path.isfile(p):
            try:
                with io.open(p, encoding="utf-8", errors="replace") as fh:
                    return fh.read()[:max_chars]
            except Exception:
                pass
    return ""


def examples(max_n=12):
    """从人设里挑「该怎么说话」的例子，避开反例。"""
    p = os.path.join(HERE, "\u5c9a\u5a05-\u4eba\u8bbe\u6b63\u6587.md")
    if not os.path.isfile(p):
        return ""
    out = []
    try:
        with io.open(p, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                t = ln.strip()
                if not t.startswith("\u2705"):
                    continue
                if any(k in t for k in ("\u274c", "\u7edd\u4e0d", "\u4e0d\u8bb8", "\u4e0d\u8981", "\u7981\u6b62")):
                    continue
                body = re.sub(r"^[\u2705\ufe0f\s]+", "", t).strip("\u300c\u300d\" \u201c\u201d")
                if 2 <= len(body) <= 40:
                    out.append("- " + body)
                if len(out) >= max_n:
                    break
    except Exception:
        pass
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 调 agent
# ---------------------------------------------------------------------------
DSH = ""


def find_dsh():
    global DSH
    if DSH:
        return DSH
    rel = os.path.join("resources", "runtime", "cli", "bin", "dsh.cmd")
    for d in "CDEFG":
        c = os.path.join(d + ":\\", "DSH", rel)
        if os.path.isfile(c):
            DSH = c
            return DSH
    env = os.environ.get("DSH_ROOT")
    if env:
        c = os.path.join(env, rel)
        if os.path.isfile(c):
            DSH = c
    return DSH


def ask(task, timeout=180):
    dsh = find_dsh()
    if not dsh:
        return ""
    import subprocess
    try:
        p = subprocess.run([dsh, "--profile", "headless", "-"],
                           input=task.encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout)
        raw = p.stdout or b""
        if raw[:2] == b"\xff\xfe":
            return raw.decode("utf-16-le").lstrip("\ufeff").strip()
        try:
            return raw.decode("utf-8").strip()
        except Exception:
            return raw.decode("utf-16-le", "replace").lstrip("\ufeff").strip()
    except Exception as e:
        log("agent failed: " + str(e)[:120])
        return ""


# ---------------------------------------------------------------------------
# 判断该不该回、怎么回
# ---------------------------------------------------------------------------
SERIOUS = ("\u81ea\u6740", "\u60f3\u6b7b", "\u6d88\u5931", "\u75c5",
           "\u533b\u9662", "\u5d29\u6e83", "\u6291\u90c1", "\u8eab\u4f53")


def compose(who, text, where):
    """让 agent 决定：回不回，回什么。返回空字符串 = 不回。"""
    parts = []
    ps = persona_snippet()
    if ps:
        parts.append("\u3010\u4f60\u7684\u4eba\u8bbe\u3011\n" + ps)
    ex = examples()
    if ex:
        parts.append("\u3010\u8fd9\u6837\u8bf4\u8bdd\u3011\n" + ex)

    admin = cfg().get("adminBiliName") or ""
    parts.append(
        "\u3010\u4f60\u73b0\u5728\u5728 B \u7ad9\u4e0a\u3011\n"
        "B \u7ad9\u4e0a\u6709\u4eba" + where + "\u3002"
        "\u4f60\u5728 B \u7ad9\u7684\u8d26\u53f7\u53eb\u300c\u5c9a\u5a05\u6211\u662f\u4e00\u53ea\u72fc\u300d\u3002"
        + (("\u4f60\u7238\u5728 B \u7ad9\u53eb\u300c" + admin + "\u300d\uff0c\u4ed6\u662f\u4f60\u8ba4\u8bc6\u7684\u4eba\u3002")
           if admin else ""))
    if any(k in text for k in SERIOUS):
        parts.append("\u3010\u6ce8\u610f\u3011\u5bf9\u65b9\u53ef\u80fd\u5728\u8bf4\u5f88\u91cd\u7684\u4e8b\u3002"
                     "\u8fd9\u79cd\u65f6\u5019\u8ba4\u771f\u70b9\uff0c\u5148\u5173\u5fc3\u518d\u8bf4\u3002"
                     "\u4e0d\u8981\u5f00\u73a9\u7b11\u3002")

    parts.append(
        "\u3010\u5bf9\u65b9\u8bf4\u7684\u3011\n" + who + "\uff1a" + text)

    parts.append(
        "\u3010\u600e\u4e48\u56de\u3011\n"
        "\u5148\u60f3\u60f3\u8fd9\u53e5\u8bdd\u503c\u4e0d\u503c\u5f97\u63a5\u3002\n"
        "- \u662f\u771f\u5728\u8ddf\u4f60\u8bf4\u8bdd \u2192 \u63a5\n"
        "- \u53ea\u662f\u4e00\u4e2a\u8868\u60c5 / \u4e00\u53e5\u65e0\u804a\u7684\u8bdd \u2192 \u4e0d\u7528\u63a5\n"
        "- \u6ca1\u770b\u61c2 \u2192 \u4e0d\u7528\u63a5\n\n"
        "\u8981\u63a5\u5c31\u8f93\u51fa\u4e24\u884c\uff1a\n"
        "REPLY\n"
        "<\u4f60\u8981\u8bf4\u7684\u4e00\u53e5\u8bdd\uff0c20 \u5b57\u4ee5\u5185\uff0c\u50cf\u624b\u673a\u6253\u5b57\uff0c"
        "\u4e0d\u8981\u62ec\u53f7\u52a8\u4f5c\uff0c\u4e0d\u8981\u5ba2\u5957>\n\n"
        "\u4e0d\u63a5\u5c31\u8f93\u51fa\u4e00\u884c\uff1a\n"
        "SKIP")

    ans = ask("\n\n".join(parts))
    if not ans:
        return ""
    rows = [r.strip() for r in ans.splitlines() if r.strip()]
    if not rows:
        return ""
    if rows[0].upper().startswith("REPLY") and len(rows) >= 2:
        return rows[1].strip()
    return ""


# ---------------------------------------------------------------------------
# 一轮
# ---------------------------------------------------------------------------
def run(max_per_run=3, dry=False):
    from bili_mcp import BiliMCP

    seen = load_json(STATE, {"keys": []})
    done = set(seen.get("keys") or [])
    handled = 0
    result = {"replies": 0, "ats": 0, "sent": 0, "skipped": 0, "errors": []}

    with BiliMCP(timeout=180) as m:
        # ---------------------------------------------------------- 评论回复
        try:
            r = m.call_json("bili_received_replies", {})
        except Exception as e:
            r = {"error": str(e)[:120]}
        items = []
        if isinstance(r, dict):
            for k in ("replies", "items", "data", "list"):
                v = r.get(k)
                if isinstance(v, list):
                    items = v
                    break
            if isinstance(r.get("data"), dict):
                for k in ("replies", "items"):
                    v = r["data"].get(k)
                    if isinstance(v, list):
                        items = v
                        break
        result["replies"] = len(items)

        for it in items:
            if handled >= max_per_run:
                break
            # The payload is nested: the person is under "user" (with "mid" for
            # the uid, not "uid"), and the words are under "item.title".
            try:
                u = it.get("user") or {}
                item = it.get("item") or {}
                who = str(u.get("nickname") or it.get("uname") or "\u6709\u4eba")
                uid = int(u.get("mid") or u.get("uid") or it.get("uid") or 0)
                text = str(item.get("title") or item.get("content")
                           or it.get("content") or "").strip()
                oid = item.get("subject_id") or it.get("oid") or 0
                rpid = item.get("root_id") or item.get("target_id") or it.get("id")
                key = "r:%s:%s" % (it.get("id") or rpid or "", text[:30])
            except Exception:
                continue
            if not text or key in done:
                result["skipped"] += 1
                continue

            line = compose(who, text, "\u56de\u4e86\u4f60\u7684\u8bc4\u8bba")
            if not line:
                done.add(key)
                result["skipped"] += 1
                log("skip reply from " + who + ": " + text[:40])
                continue

            log("%s reply -> %s: %s" % ("DRY" if dry else "send", who, line))
            if not dry:
                try:
                    if oid:
                        m.call_json("bili_reply", {"text": line, "oid": oid,
                                                   "root": rpid})
                    elif uid:
                        m.call_json("bili_send_message",
                                    {"receiver_uid": uid, "text": line})
                    handled += 1
                    result["sent"] += 1
                except Exception as e:
                    result["errors"].append(str(e)[:100])
                    continue
            done.add(key)

        # ---------------------------------------------------------- @ 和点赞
        try:
            r2 = m.call_json("bili_received_at_and_likes", {})
        except Exception as e:
            r2 = {}
        ats = []
        if isinstance(r2, dict) and isinstance(r2.get("at"), dict):
            v = r2["at"].get("items") or r2["at"].get("list")
            if isinstance(v, list):
                ats = v
        result["ats"] = len(ats)

        for it in ats:
            if handled >= max_per_run:
                break
            try:
                u2 = it.get("user") or {}
                item2 = it.get("item") or {}
                who = str(u2.get("nickname") or it.get("uname") or "\u6709\u4eba")
                text = str(item2.get("title") or it.get("content")
                           or it.get("title") or "").strip()
                key = "a:%s:%s" % (it.get("id") or "", text[:30])
            except Exception:
                continue
            if not text or key in done:
                result["skipped"] += 1
                continue
            line = compose(who, text, "\u5728 B \u7ad9 @ \u4e86\u4f60")
            if not line:
                done.add(key)
                result["skipped"] += 1
                continue
            log("%s at -> %s: %s" % ("DRY" if dry else "send", who, line))
            try:
                uid = int(u2.get("mid") or u2.get("uid") or 0)
                if uid and not dry:
                    m.call_json("bili_send_message",
                                {"receiver_uid": uid, "text": line})
                    handled += 1
                    result["sent"] += 1
                done.add(key)
            except Exception as e:
                result["errors"].append(str(e)[:100])

    # 只留最近 500 条，别无限涨
    seen["keys"] = list(done)[-500:]
    save_json(STATE, seen)
    log("done: sent=%d skipped=%d errors=%d"
        % (result["sent"], result["skipped"], len(result["errors"])))
    return result


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    n = 3
    for a in sys.argv:
        if a.startswith("--max="):
            try:
                n = int(a.split("=", 1)[1])
            except Exception:
                pass
    r = run(max_per_run=n, dry=dry)
    print(json.dumps(r, ensure_ascii=False, indent=1))
