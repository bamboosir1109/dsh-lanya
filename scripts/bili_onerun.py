# -*- coding: utf-8 -*-
"""真的刷一条 + 评论，全在面板里点一下完成。

链路
----
  1 定搜索词：她攒的偏好 > 群里最近提到的 > 记忆里的关键词
  2 带着词去搜，搜不着退回推荐/热门
  3 挨个试，挑一条真有内容的（字幕不是歌词）
  4 读完内容再写评论；写不出像样的就不发
  5 真发出去，记进判重表，写进长期记忆，更新偏好

为什么评论让 agent 写：没有它就只能用固定模板，那不像人说话。
agent 只写一句话，不调任何工具，很便宜。

⚠️ 提示词必须走 stdin。当命令行参数传会在第一个换行处被截断。
"""
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bili_mcp import BiliMCP

WORK = os.path.dirname(HERE)
SEEN = os.path.join(os.path.expanduser("~"), ".dsh", "bili-duty", "seen.md")
PERSONA_FILE = os.path.join(WORK, "napcat", "persona.md")
LANYA_DIR = os.path.join(WORK, "\u5c9a\u5a05")
INTEREST_FILE = os.path.join(LANYA_DIR, "bili-interests.json")
CHAT_DIR = os.path.join(LANYA_DIR, "chats")
CONFIG_FILE = os.path.join(LANYA_DIR, "lanya-config.json")
def _find_dsh():
    """找 dsh.cmd。

    别写死盘符 —— 每台机器装哪儿不一样。依次看：
    环境变量 DSH_ROOT > 和本脚本同级的 DSH 目录 > 几个常见位置。
    """
    rel = os.path.join("resources", "runtime", "cli", "bin", "dsh.cmd")
    cands = []
    if os.environ.get("DSH_ROOT"):
        cands.append(os.environ["DSH_ROOT"])
    # 和 napcat 同级、或者上一级
    for base in (WORK, os.path.dirname(WORK)):
        cands.append(os.path.join(base, "DSH"))
    for d in ("C:\\", "D:\\", "E:\\", "F:\\"):
        cands.append(os.path.join(d, "DSH"))
    for c in cands:
        p = os.path.join(c, rel)
        if os.path.isfile(p):
            return p
    return os.path.join(cands[-1], rel) if cands else "dsh.cmd"


DSH = _find_dsh()


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------------------
# 本地配置
# ---------------------------------------------------------------------------
def _cfg():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def admin_bili():
    """管理员的 B 站账号，用来在评论里 @ 他。

    以前写死在源码里 —— 那是别人的账号，不该出现在公开仓库。
    现在从 lanya-config.json 读，没配就返回空（也就不 @ 了）。
    """
    c = _cfg()
    return (str(c.get("adminBiliName") or "").strip(),
            str(c.get("adminBiliUid") or "").strip())


# ---------------------------------------------------------------------------
# 人设 / 记忆 / 例句
# ---------------------------------------------------------------------------
def _persona_snippet(max_chars=2500):
    try:
        if os.path.isfile(PERSONA_FILE):
            with open(PERSONA_FILE, "r", encoding="utf-8") as fh:
                return fh.read()[:max_chars]
    except Exception:
        pass
    return ""


def _memory_snippet(max_items=12):
    try:
        import sqlite3
        db = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-memento",
                          "memory.db")
        if not os.path.isfile(db):
            return ""
        c = sqlite3.connect(db)
        rows = c.execute(
            "SELECT text FROM entries ORDER BY created_at DESC LIMIT ?",
            (max_items,)).fetchall()
        c.close()
        return "\n".join("- " + r[0] for r in rows if r[0])
    except Exception:
        return ""


def _examples(persona, max_items=14):
    """从人设里抽出她实际会说的句子，当 few-shot 用。

    规则贴过去写出来是「通用 B 站评论」，例子贴过去才像她。
    关键是别把反例抽进来：人设里大量「❌（不接）」「❌要不要我接着」是
    "不许这么说"的示范，喂给模型等于教坏。
    """
    if not persona:
        return []
    out = []
    for line in persona.splitlines():
        ln = line.strip()
        if not ln:
            continue
        if any(m in ln for m in ("\u274c", "\u7edd\u4e0d\u8bf4", "\u4e0d\u8bb8",
                                 "\u4e0d\u8981", "\u522b\u603b", "\u7981\u6b62")):
            continue
        tick = ln.find("\u2705")
        seg = ln[tick + 1:] if tick >= 0 else ln
        for m in re.finditer(
                r'[\u300c\u201c]([^\u300d\u201d\n]{2,30})[\u300d\u201d]', seg):
            t = m.group(1).strip()
            if 2 <= len(t) <= 30:
                out.append(t)

    bad = set("\uff08\uff09()\u3010\u3011[]{}*")
    keep = [t for t in out if not any(ch in bad for ch in t) and len(t) >= 3]

    seen = set()
    uniq = []
    for t in keep:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq[:max_items]


# B 站评论区那套话。写着"别用"，模型还是会滑回去，所以生成完再查一遍。
_CLICHE = (
    "\u4e09\u8fde", "\u5148\u8d5e", "UP\u4e3b", "up\u4e3b",
    "DNA", "\u4e0d\u5f97\u4e86", "\u7edd\u4e86", "\u592a\u7968\u4eae\u4e86",
    "\u4e00\u952e", "\u5f88\u68d2", "\u52a0\u6cb9", "\u7231\u4e86\u7231\u4e86",
    "\u795e\u4f5c", "\u5341\u5206\u611f\u8c22", "\u611f\u8c22\u5206\u4eab",
    "\u6c38\u8fdc\u7684\u795e", "\u7237\u9752\u56de", "\u7b11\u6b7b",
)


def _is_cliche(text):
    if not text:
        return True
    t = text.lower()
    for w in _CLICHE:
        if w.lower() in t:
            return True
    if len(text) > 26 and text.count("\u4e86") >= 3:
        return True
    return False


# ---------------------------------------------------------------------------
# 生成评论
# ---------------------------------------------------------------------------
def watch_frames(bvid, every=0, max_frames=36):
    """Download the video and pull real frames out of it.

    Returns ([], reason) on failure - never raises, because not being able to
    see the picture is not a good reason to skip writing the comment.
    """
    import json as _j
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "bili_watch.py")
    if not os.path.isfile(script):
        return [], "bili_watch.py missing"
    args = [sys.executable, script, bvid, "--max=36"]
    if every:
        args.append("--every=%d" % every)
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900)
        data = _j.loads((p.stdout or "").strip() or "{}")
        # Prefer the contact sheets: nine frames per image, and all of the text
        # in them is still readable. Handing over thirty loose stills instead
        # just floods the model and it reads none of them properly.
        sheets = list(data.get("sheets") or [])
        if sheets:
            return sheets, str(data.get("error") or "")
        return list(data.get("frames") or []), str(data.get("error") or "")
    except Exception as e:
        return [], str(e)[:150]


def gen_comment(title, up, content="", mention=False, frames=None):
    """让 headless agent 写一句评论。

    顺序很重要：内容放最前面。之前塞在中间，模型当没看见。
    """
    persona = _persona_snippet()
    memory = _memory_snippet()
    ex = _examples(persona)

    # Frames first when we have them: seeing the picture beats reading a
    # transcript of it. The agent has read_image and can look at these files.
    prompt = ""
    if frames:
        prompt += ("我把这个视频真下载下来、按间隔抽帧、拼成了下面这几张图"
                   "（每张是 3x3 = 九帧，按时间顺序，从左上到右下）：\n")
        for _f in frames[:8]:
            prompt += "| " + _f + "\n"
        prompt += ("\n用 read_image 把这些图都看一遍——画面里的字能读清。"
                   "看完能说出它到底讲了什么，再写评论。\n\n")

    prompt += "下面是一条视频里的内容摘录：\n"
    prompt += "\n".join("| " + ln for ln in (content or "")[:1200].splitlines())
    prompt += "\n\n"
    prompt += (
        "写一条 B 站评论，只能针对上面这段内容。\n"
        "硬要求：\n"
        "- 必须提到内容里的某个具体东西（一个词、一件事、一句话）\n"
        "- 不许评价视频本身好不好，不许说三连/先赞为敬/用心/加油/神作/"
        "DNA动了/含金量/助眠/后劲这类话\n"
        "- 不超过 30 字，口语，一句话\n\n")
    if ex:
        prompt += ("这个人平时这么说话（学口气，别抄）：\n"
                   + "\u3001".join(ex[:10]) + "\n\n")
    if memory:
        prompt += "她记得的事：\n" + memory[:600] + "\n\n"
    prompt += "只输出这一句评论。不要引号，不要解释，不要问问题。\n"

    try:
        _env = dict(os.environ)
        _env["PYTHONUTF8"] = "1"
        _env["PYTHONIOENCODING"] = "utf-8"
        r = subprocess.run([DSH, "--profile", "headless", "-"],
                           input=prompt.encode("utf-8"),
                           capture_output=True, text=False, timeout=180,
                           env=_env)
        txt = (r.stdout or b"").decode("utf-8", "replace").strip()
        txt = txt.strip().strip('"\u201c\u201d\'')
        best = ""
        for ln in txt.splitlines():
            ln = ln.strip().strip('"\u201c\u201d\'')
            if not (4 <= len(ln) <= 60):
                continue
            if ln.startswith(("dsh:", "#", "```", "- ", "|")):
                continue
            for pre in ("\u8fd9\u53e5\u600e\u4e48\u6837\uff1a",
                        "\u8bc4\u8bba\uff1a", "\u8fd9\u53e5\uff1a",
                        "\u56de\u7b54\uff1a"):
                if ln.startswith(pre):
                    ln = ln[len(pre):].strip()
            if not _is_cliche(ln):
                return ln
            best = best or ln
        return best
    except Exception as e:
        log("gen_comment failed: %s" % str(e)[:120])
        return ""


# ---------------------------------------------------------------------------
# 判重表 / 记忆
# ---------------------------------------------------------------------------
def seen_bvs():
    out = set()
    try:
        with open(SEEN, "r", encoding="utf-8") as fh:
            for ln in fh:
                for m in re.finditer(r"BV[0-9A-Za-z]{10}", ln):
                    out.add(m.group(0))
    except Exception:
        pass
    return out


def _append_comment_row(bv, rpid, text):
    """往 seen.md 的「评论记录」表插一行（不是追加到文件末尾）。

    MCP 的 duty_seen_append 只会把「视频」格式的行追加到文件末尾，
    评论如果走它就写进视频表，于是"她发过的评论"永远是空的。
    """
    try:
        with open(SEEN, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except Exception:
        return
    row = "| %s | %s |  | %s | %s |\n" % (
        bv, rpid or "", time.strftime("%Y-%m-%d"), text.replace("|", "/")[:40])
    in_comment = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith("## \u8bc4\u8bba\u8bb0\u5f55"):
            in_comment = True
            continue
        if in_comment and ln.strip().startswith("| ---"):
            lines.insert(i + 1, row)
            with open(SEEN, "w", encoding="utf-8", newline="") as fh:
                fh.writelines(lines)
            return
    add = ("\n## \u8bc4\u8bba\u8bb0\u5f55\n\n"
           "| BV | rpid | root | \u65f6\u95f4 | \u6b63\u6587 |\n"
           "| --- | --- | --- | --- | --- |\n" + row)
    with open(SEEN, "a", encoding="utf-8") as fh:
        fh.write(add)


def record_memory(title, up, comment, bv, feeling=""):
    """把这一趟刷站写进长期记忆。像真人一样，刷过什么、说过什么能想起来。"""
    try:
        import sqlite3
        import uuid
        db = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-memento",
                          "memory.db")
        if not os.path.isfile(db):
            return
        text = "\u5728 B \u7ad9\u770b\u4e86\u300a%s\u300b\uff08%s\uff09" % (
            title[:40], up or "\u4e0d\u77e5\u9053\u8c01")
        if feeling:
            text += "\uff0c" + feeling[:24]
        if comment:
            text += "\uff0c\u7559\u4e86\u53e5\u300c%s\u300d" % comment[:30]
        text += "\u3002" + bv

        now = int(time.time() * 1000)
        eid = str(uuid.uuid4())[:8] + "-" + uuid.uuid4().hex[:8]
        c = sqlite3.connect(db)
        c.execute(
            "INSERT INTO entries (id, track, scope, workspace_key, text, source, "
            "created_at, updated_at, session_id, agent_key, tags, version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, "agent", "workspace", "", text, "bili", now, now, None, "",
             '["bili"]', 1))
        c.execute(
            "INSERT INTO audit (ts, action, track, scope, entry_id, text, "
            "outcome, source, session_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (now, "add-bili", "agent", "workspace", eid, text,
             "recorded after a bilibili run", "bili_onerun", None))
        c.commit()
        c.close()
        log("memory: " + text[:50])
    except Exception as e:
        log("record_memory failed: %s" % str(e)[:100])


# ---------------------------------------------------------------------------
# 兴趣
# ---------------------------------------------------------------------------
def load_interests():
    try:
        with open(INTEREST_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return (d.get("ups") or [])[:8], (d.get("topics") or [])[:12]
    except Exception:
        return [], []


def save_interests(up, topic):
    ups, topics = load_interests()
    if up:
        ups = [up] + [u for u in ups if u != up]
    if topic:
        topics = [topic] + [t for t in topics if t != topic]
    try:
        os.makedirs(LANYA_DIR, exist_ok=True)
        with open(INTEREST_FILE, "w", encoding="utf-8") as fh:
            json.dump({"ups": ups[:20], "topics": topics[:30]},
                      fh, ensure_ascii=False, indent=1)
    except Exception as e:
        log("save_interests failed: %s" % str(e)[:80])


def group_buzz(max_files=4, max_chars=2500):
    try:
        files = [f for f in os.listdir(CHAT_DIR)
                 if f.startswith("\u7fa4-")]
    except Exception:
        return ""
    if not files:
        return ""
    files.sort(key=lambda f: os.path.getmtime(os.path.join(CHAT_DIR, f)),
               reverse=True)
    out = []
    for f in files[:max_files]:
        try:
            with open(os.path.join(CHAT_DIR, f), "r", encoding="utf-8") as fh:
                out.append(fh.read()[-1200:])
        except Exception:
            pass
    return "\n".join(out)[-max_chars:]


def pick_search_terms(count=3):
    """定这一轮搜什么。

    优先级：她攒的偏好 > 群里最近提到的书名号内容 > 记忆里的关键词。
    抽出来的必须是短词——整条标题当搜索词，B 站搜出来是空的。
    """
    terms = []

    def push(t):
        t = (t or "").strip(" \u3000\u300a\u300b\u201c\u201d!\uff01?"
                            "\uff1f,.\u3002")
        if t and 2 <= len(t) <= 14 and t not in terms:
            terms.append(t)

    ups, topics = load_interests()
    for t in topics[:2]:
        push(t)

    for m in re.finditer(r'\u300a([^\u300b]{2,14})\u300b', group_buzz()):
        push(m.group(1))

    mem = _memory_snippet(10)
    seen = {}
    for m in re.finditer(r'[A-Za-z][A-Za-z0-9.\-]{2,18}', mem):
        w = m.group(0)
        if w.lower() in ("dsh", "bili", "http", "https", "bv"):
            continue
        seen[w] = seen.get(w, 0) + 1
    for w, _n in sorted(seen.items(), key=lambda kv: -kv[1])[:2]:
        push(w)

    return terms[:count]


# ---------------------------------------------------------------------------
# 内容
# ---------------------------------------------------------------------------
def _content_of(m, bv):
    """把一条视频「她能看到的东西」拼出来：简介 + 字幕开头。

    拿不到内容就返回 None —— 没看过就没资格评论。
    """
    parts = []
    d = {}
    try:
        info = m.call_json("bili_video_info", {"bvid": bv})
        d = info.get("data") or info
        desc = (d.get("desc") or "").strip()
        if desc and len(desc) > 12:
            parts.append("\u7b80\u4ecb\uff1a" + desc[:400])
    except Exception:
        pass

    sub = ""
    try:
        r = m.call_json("bili_subtitle", {"bvid": bv})
        sub = (r.get("text") or "").strip()
    except Exception:
        pass

    # 歌词字幕对她没用——一条 MV 的字幕就是歌词，她没法"对内容有反应"。
    all_lines = [ln.strip() for ln in sub.splitlines() if ln.strip()]
    song_lines = [ln for ln in all_lines if "\u266a" in ln or "\u266b" in ln]
    is_song = bool(all_lines) and len(song_lines) * 2 > len(all_lines)

    if sub and not is_song:
        body = "\n".join(ln for ln in all_lines
                         if ln != "\u97f3\u4e50" and "\u266a" not in ln)
        if body.strip():
            parts.append("\u5b57\u5e55\uff08\u5979\u8bf4\u7684\u8bdd / "
                         "\u65c1\u767d\uff09\uff1a" + body[:900])

    title = d.get("title") or ""
    up = ((d.get("owner") or {}).get("name")) or ""
    if not parts:
        return None, title, up, ""
    return "\n\n".join(parts), title, up, "\n".join(parts)[:900]


# ---------------------------------------------------------------------------
# 跑一轮
# ---------------------------------------------------------------------------
def run_one(do_comment=True, mention=False):
    result = {"ok": False, "step": "", "error": ""}
    already = seen_bvs()
    comment = ""
    pick = None

    with BiliMCP() as m:
        # 1 攒候选：先带着兴趣搜，搜不到才退回推荐
        result["step"] = "pick"
        cands = []

        def add_batch(vs):
            for v in vs or []:
                bv = v.get("bvid") or v.get("bv") or ""
                if bv and bv not in already:
                    cands.append({"bv": bv,
                                  "title": v.get("title") or "",
                                  "up": v.get("author") or v.get("up") or ""})

        terms = pick_search_terms()
        result["terms"] = terms
        for t in terms:
            if cands:
                break
            try:
                r = m.call_json("bili_search",
                                {"keyword": t, "num": 10, "order": "pubdate"})
                add_batch(r.get("videos"))
                if cands:
                    log("search %s -> %d" % (t, len(cands)))
            except Exception as e:
                log("search %s failed: %s" % (t, str(e)[:80]))

        if not cands:
            for src, args in (("bili_recommend", {"count": 12}),
                              ("bili_hot_videos", {"count": 12})):
                try:
                    r = m.call_json(src, args)
                except Exception as e:
                    log("%s failed: %s" % (src, str(e)[:100]))
                    continue
                add_batch(r.get("videos") or r.get("list"))
                if cands:
                    break

        if not cands:
            result["error"] = "\u6ca1\u6709\u6ca1\u5237\u8fc7\u7684\u89c6\u9891"
            return result

        # 2 挨个试，挑一条真有内容的
        result["step"] = "watch"
        content = ""
        tried = []
        for c in cands[:4]:
            body, title, up, sub = _content_of(m, c["bv"])
            tried.append(c["bv"])
            if body:
                pick = {"bv": c["bv"], "title": title or c["title"],
                        "up": up or c["up"]}
                content = body
                log("watched %s (%d chars)" % (c["bv"], len(body)))
                break
            log("skip %s: no content" % c["bv"])

        if not pick:
            result["error"] = ("\u8bd5\u4e86 %d \u6761\u90fd\u62ff\u4e0d\u5230"
                               "\u5185\u5bb9\uff0c\u8fd9\u8f6e\u4e0d\u53d1\u4e86"
                               % len(tried))
            result["tried"] = tried
            return result

        result["bv"] = pick["bv"]
        result["title"] = pick["title"]
        result["up"] = pick["up"]

        # 3 写评论
        if do_comment:
            result["step"] = "comment"
            for _try in range(3):
                if _try == 0:
                    _fr, _why = watch_frames(pick["bv"])
                    if _fr:
                        log("watched %d real frames" % len(_fr))
                    else:
                        log("could not extract frames: %s" % (_why or "?")[:80])
                cand = gen_comment(pick["title"], pick["up"], content=content,
                                   frames=_fr)
                if cand and not _is_cliche(cand):
                    comment = cand
                    break
            if not comment:
                result["error"] = ("\u4e09\u6b21\u90fd\u6ca1\u5199\u51fa\u50cf\u6837"
                                   "\u7684\u8bc4\u8bba\uff0c\u8fd9\u8f6e\u4e0d\u53d1")
                return result

            log("comment: %s" % comment)

            # @ 由代码追加，不指望模型记得
            if mention:
                nm, _uid = admin_bili()
                if nm and ("@" + nm) not in comment:
                    comment = (comment.rstrip("\uff0c\u3002\uff01\uff1f,.!? ")
                               + " @" + nm + " ")

            try:
                rep = m.call_json("bili_reply",
                                  {"bvid": pick["bv"], "text": comment})
                if not rep.get("success"):
                    result["error"] = ("\u8bc4\u8bba\u6ca1\u53d1\u51fa\u53bb\uff1a"
                                       + str(rep)[:150])
                    return result
                result["rpid"] = rep.get("rpid")
                log("replied rpid=%s" % rep.get("rpid"))
            except Exception as e:
                result["error"] = "\u8bc4\u8bba\u5931\u8d25\uff1a" + str(e)[:150]
                return result

        # 4 记录
        result["step"] = "record"
        try:
            m.call("duty_seen_append",
                   {"bvid": pick["bv"], "title": pick["title"],
                    "up": pick["up"],
                    "status": "\u8bc4\u8bba" if comment else "\u6311\u4e86"})
        except Exception as e:
            log("record video failed: %s" % str(e)[:100])

        if comment:
            try:
                _append_comment_row(pick["bv"], result.get("rpid", ""), comment)
            except Exception as e:
                log("record comment failed: %s" % str(e)[:100])

    save_interests(pick["up"], (result.get("terms") or [""])[0])
    record_memory(pick["title"], pick["up"], comment, pick["bv"],
                  feeling=("\u770b\u7684\u662f\u641c\u7d22\u8bcd\u300c%s\u300d"
                           "\u627e\u5230\u7684" % (result.get("terms") or [""])[0]
                           if result.get("terms") else ""))

    result["ok"] = True
    result["comment"] = comment
    result["step"] = "done"
    return result


# ---------------------------------------------------------------------------
# 核对：这条评论公开了没有
# ---------------------------------------------------------------------------
def verify_comment(bv, text):
    try:
        with BiliMCP() as m:
            r = m.call_json("bili_comments", {"bvid": bv, "num": 50})
    except Exception as e:
        return {"state": "unknown", "why": str(e)[:120]}

    cms = r.get("comments") or []
    if not cms and not r.get("count"):
        return {"state": "unknown", "why": "\u8bc4\u8bba\u533a\u8bfb\u4e0d\u5230"}

    key = (text or "").strip()[:20]
    for c in cms:
        body = (c.get("content") or "").strip()
        if key and key in body:
            return {"state": "public", "rpid": c.get("rpid"),
                    "user": c.get("user"), "like": c.get("like")}

    # 正文对不上时，按"自己的账号名"兜底
    upl = str(_cfg().get("biliUserName") or "").strip()
    if upl:
        for c in cms:
            if upl in (c.get("user") or ""):
                return {"state": "public", "rpid": c.get("rpid"),
                        "user": c.get("user"), "like": c.get("like"),
                        "note": "text differs"}
    return {"state": "pending",
            "why": "\u8bc4\u8bba\u533a\u524d 50 \u6761\u91cc\u6ca1\u6709"
                   "\uff08\u591a\u534a\u5728\u5ba1\u6838\uff09"}


if __name__ == "__main__":
    if "--verify" in sys.argv:
        i = sys.argv.index("--verify")
        bv = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
        txt = ""
        # 评论正文走 base64：中文当命令行参数传，Windows 上会被转成 GBK 乱码。
        if "--text-b64" in sys.argv:
            j = sys.argv.index("--text-b64")
            if len(sys.argv) > j + 1:
                try:
                    import base64
                    txt = base64.b64decode(sys.argv[j + 1]).decode("utf-8")
                except Exception:
                    txt = ""
        elif len(sys.argv) > i + 2:
            txt = sys.argv[i + 2]
        print(json.dumps(verify_comment(bv, txt), ensure_ascii=False))
        sys.exit(0)

    no_comment = "--no-comment" in sys.argv
    r = run_one(do_comment=not no_comment, mention="--mention" in sys.argv)
    print(json.dumps(r, ensure_ascii=False, indent=1))
