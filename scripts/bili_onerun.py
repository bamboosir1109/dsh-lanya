# -*- coding: utf-8 -*-
"""真的刷一条 + 评论，全在面板里点一下完成。

链路：
  1. MCP 拿推荐 / 热门 → 挑一个没刷过的
  2. 拿视频信息（标题、UP、分区）
  3. 让 headless agent 写一句像人话的评论（它不需要工具，只是写文案）
  4. MCP 的 bili_reply 真发出去
  5. MCP 的 duty_seen_append 记进判重表

为什么评论让 agent 写：没有它就只能用固定模板，那不像她说话。
agent 只写一句话，不调任何工具，很便宜。
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

LANYA = os.path.join(os.path.dirname(HERE), "\u5c9a\u5a05")
SEEN = os.path.join(os.path.expanduser("~"), ".dsh", "bili-duty", "seen.md")
# 管理员的 B 站账号（二两酥肉），可以在评论里 @ 他
ADMIN_BILI_UID = ADMIN_BILI_UID_PLACEHOLDER
ADMIN_BILI_NAME = "\u4e8c\u4e24\u9165\u8089"
DSH = os.path.join(os.environ.get("DSH_ROOT", os.environ.get("DSH_ROOT", r"D:\DSH")),
                   "resources", "runtime", "cli", "bin", "dsh.cmd")


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


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


PERSONA_FILE = os.path.join(os.path.dirname(HERE), "napcat", "persona.md")


def _persona_snippet(max_chars=2500):
    """读人设正文的开头一段——够她说话像自己就行，别把整篇塞进去。"""
    for f in (PERSONA_FILE,
              os.path.join(os.path.dirname(HERE), "napcat", "persona.md")):
        try:
            if os.path.isfile(f):
                with open(f, "r", encoding="utf-8") as fh:
                    return fh.read()[:max_chars]
        except Exception:
            pass
    return ""


def _memory_snippet(max_items=12):
    """从长期记忆里挑几条，让她说话带自己的立场和经历。"""
    try:
        import sqlite3
        db = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-memento", "memory.db")
        if not os.path.isfile(db):
            return ""
        c = sqlite3.connect(db)
        rows = c.execute("SELECT text FROM entries ORDER BY created_at DESC LIMIT ?",
                         (max_items,)).fetchall()
        c.close()
        return "\n".join("- " + r[0] for r in rows if r[0])
    except Exception:
        return ""


def _examples(persona, max_items=14):
    """从人设里抽出她实际会说的句子，当 few-shot 用。

    规则贴过去写出来是「通用 B 站评论」，例子贴过去才像她。
    关键是别把反例也抽进来：人设里大量「❌（不接）」「❌要不要我接着」是
    "不许这么说"的示范，喂给模型等于教坏。

    只从 ① ✅ 开头的行 ② 不含 ❌ 的行 里取。
    """
    if not persona:
        return []
    out = []
    for line in persona.splitlines():
        ln = line.strip()
        if not ln:
            continue
        # 整行是反例就跳过。除了 ❌，人设里还有「**绝不说**：」「不许」「别」
        # 这类行，里面同样裹着例子——那些是反例，不能喂。
        if any(m in ln for m in ("\u274c", "\u7edd\u4e0d\u8bf4", "\u4e0d\u8bb8",
                                 "\u4e0d\u8981", "\u522b\u603b", "\u7981\u6b62")):
            continue
        # ✅ 后面的示范优先
        tick = ln.find("\u2705")
        if tick >= 0:
            seg = ln[tick + 1:]
        else:
            seg = ln
        # 取「...」或 "..." 里的句子
        for m in re.finditer(r'[\u300c\u201c]([^\u300d\u201d\n]{2,30})[\u300d\u201d]', seg):
            t = m.group(1).strip()
            if 2 <= len(t) <= 30:
                out.append(t)

    # 洗掉残留：含括号/星号的是舞台说明，不是人话
    bad = set("\uff08\uff09()\u3010\u3011[]{}*")
    keep = [t for t in out
            if not any(ch in bad for ch in t) and len(t) >= 3]

    seen = set()
    uniq = []
    for t in keep:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq[:max_items]


def _style_snippet(persona, max_chars=1200):
    """从人设正文里挑「怎么说话」的段落，丢掉「你是谁」那种指令。

    整篇贴给一个只会写一句话的 agent，它会以为自己在扮演，然后拒绝写评论。
    只要风格样本就够了。
    """
    if not persona:
        return "一个二十岁左右的女生，说话短、口语、有点贫。"
    keep = []
    # 优先这些标题下面的内容
    want = ("短", "不匀", "语气", "口语", "别", "说话", "例句", "\u201c")
    for para in persona.split("\n\n"):
        head = para.strip()[:40]
        if any(w in head for w in want):
            keep.append(para.strip())
        if sum(len(x) for x in keep) > max_chars:
            break
    if not keep:
        keep = [persona[:max_chars]]
    return "\n\n".join(keep)[:max_chars]


# B 站评论区那套话。写着"别用"，模型还是会滑回去，所以生成完再查一遍。
_CLICHE = (
    "\u4e09\u8fde", "\u5148\u8d5e", "UP\u4e3b", "up\u4e3b",
    "DNA", "\u4e0d\u5f97\u4e86", "\u7edd\u4e86", "\u592a\u7968\u4eae\u4e86",
    "\u4e00\u952e", "\u5f88\u68d2", "\u52a0\u6cb9", "\u7231\u4e86\u7231\u4e86",
    "\u795e\u4f5c", "\u5341\u5206\u611f\u8c22", "\u611f\u8c22\u5206\u4eab",
    "\u6c38\u8fdc\u7684\u795e", "\u7237\u9752\u56de", "\u7b11\u6b7b",
)


def _is_cliche(text):
    """像不像那种谁都能发的 B 站套话。像就扔掉重写。"""
    if not text:
        return True
    t = text.lower()
    for w in _CLICHE:
        if w.lower() in t:
            return True
    # 一个具体细节都没有、全是大词的，也算
    if len(text) > 26 and text.count("\u4e86") >= 3:
        return True
    return False


def gen_comment(title, up, content="", mention=False):
    """让 headless agent 写一句评论。带着人设和记忆，才像她说的。

    顺序很重要：**内容放最前面**。之前把内容塞在提示词中间，
    模型当没看见——生成出来跟视频毫无关系。
    """
    persona = _persona_snippet()
    memory = _memory_snippet()
    ex = _examples(persona)

    # 1. 内容。这是她唯一"看到"的东西，必须最先看到
    prompt = "下面是一条视频里的内容摘录：\n"
    prompt += "\n".join("| " + ln for ln in (content or "")[:1200].splitlines())
    prompt += "\n\n"

    # 2. 任务：针对内容说话，不是针对视频质量
    prompt += (
        "写一条 B 站评论，只能针对上面这段内容。\n"
        "硬要求：\n"
        "- 必须提到内容里的某个具体东西（一个词、一件事、一句话）\n"
        "- 不许评价视频本身好不好，不许说三连/先赞为敬/用心/加油/神作/"
        "DNA动了/含金量/助眠/后劲这类话\n"
        "- 不超过 30 字，口语，一句话\n\n")

    # 3. 她的口气，放后面当参考
    if ex:
        prompt += ("她平时这么说话（学口气，别抄）：\n"
                   + "、".join(ex[:10]) + "\n\n")
    if memory:
        prompt += "她记得的事：\n" + memory[:600] + "\n\n"

    prompt += "只输出这一句评论。不要引号，不要解释，不要问问题。\n"

    try:
        # ⚠️ 提示词必须走 stdin。
        # 当命令行参数传的话，会在第一个换行处被截断 —— 内容摘录根本传不进去，
        # agent 只看到"下面是一条视频里的内容摘录："然后回"摘录在哪"。
        # 用 "-" 让它从 stdin 读整段。
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
            for pre in ("\u8fd9\u53e5\u600e\u4e48\u6837\uff1a", "\u8bc4\u8bba\uff1a",
                        "\u8fd9\u53e5\uff1a", "\u56de\u7b54\uff1a"):
                if ln.startswith(pre):
                    ln = ln[len(pre):].strip()
            if not _is_cliche(ln):
                return ln
            best = best or ln
        return best
    except Exception as e:
        log("gen_comment failed: %s" % str(e)[:120])
        return ""



def _append_comment_row(bv, rpid, text):
    """往 seen.md 的「评论记录」表插一行（不是追加到文件末尾）。"""
    try:
        with open(SEEN, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except Exception:
        return
    row = "| %s | %s |  | %s | %s |\n" % (
        bv, rpid or "", time.strftime("%Y-%m-%d"), text.replace("|", "/")[:40])
    # 找「评论记录」那张表的分隔线，插在它后面
    in_comment = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith("## 评论记录"):
            in_comment = True
            continue
        if in_comment and ln.strip().startswith("| ---"):
            lines.insert(i + 1, row)
            with open(SEEN, "w", encoding="utf-8", newline="") as fh:
                fh.writelines(lines)
            return
    # 没有就补一张表
    add = "\n## 评论记录\n\n| BV | rpid | root | 时间 | 正文 |\n| --- | --- | --- | --- | --- |\n" + row
    with open(SEEN, "a", encoding="utf-8") as fh:
        fh.write(add)


def record_memory(title, up, comment, bv, feeling=""):
    """把这一趟刷站写进长期记忆。像真人一样，刷过什么、说过什么能想起来。"""
    try:
        import sqlite3
        import uuid
        db = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-memento", "memory.db")
        if not os.path.isfile(db):
            return
        text = "在 B 站看了《%s》（%s）" % (title[:40], up or "不知道谁")
        if feeling:
            text += "，" + feeling[:24]
        if comment:
            text += "，留了句「%s」" % comment[:30]
        text += "。" + bv
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
            "INSERT INTO audit (ts, action, track, scope, entry_id, text, outcome, source, session_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (now, "add-bili", "agent", "workspace", eid, text,
             "recorded after a bilibili run", "bili_onerun", None))
        c.commit()
        c.close()
        log("memory: " + text[:50])
    except Exception as e:
        log("record_memory failed: %s" % str(e)[:100])


def verify_comment(bv, text):
    """去 B 站看这条评论公开了没有。

    返回 {"state": "public"|"pending"|"unknown", ...}
      public  在评论区列表里找到了
      pending 没找到（多半在审核，或者被拦了）
      unknown 查不到（视频评论关着 / 接口失败）
    """
    try:
        with BiliMCP() as m:
            r = m.call_json("bili_comments", {"bvid": bv, "num": 50})
    except Exception as e:
        return {"state": "unknown", "why": str(e)[:120]}

    cms = r.get("comments") or []
    if not cms and not r.get("count"):
        return {"state": "unknown", "why": "评论区读不到"}

    # 先按正文找（最准），再按用户名兜底
    key = (text or "").strip()[:20]
    for c in cms:
        body = (c.get("content") or "").strip()
        if key and key in body:
            return {"state": "public", "rpid": c.get("rpid"),
                    "user": c.get("user"), "like": c.get("like")}
    for c in cms:
        if "\u5c9a\u5a05" in (c.get("user") or ""):
            return {"state": "public", "rpid": c.get("rpid"),
                    "user": c.get("user"), "like": c.get("like"),
                    "note": "text differs"}
    return {"state": "pending",
            "why": "评论区前 50 条里没有（多半在审核，或没通过）"}


LANYA_DIR = os.path.join(os.path.dirname(HERE), "\u5c9a\u5a05")
INTEREST_FILE = os.path.join(LANYA_DIR, "bili-interests.json")
CHAT_DIR = os.path.join(LANYA_DIR, "chats")


def load_interests():
    """她攒下来的偏好：常看的 UP、常搜的词。"""
    try:
        with open(INTEREST_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return (d.get("ups") or [])[:8], (d.get("topics") or [])[:12]
    except Exception:
        return [], []


def save_interests(up, topic):
    """看完一条就记一笔。同一项重复出现，看着像真的感兴趣的。"""
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
    """群聊天记录里最近在聊什么。用来定搜索词。"""
    try:
        files = [f for f in os.listdir(CHAT_DIR) if f.startswith("\u7fa4-")]
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
                tail = fh.read()[-1200:]
            out.append(tail)
        except Exception:
            pass
    return "\n".join(out)[-max_chars:]


def pick_search_terms(count=3):
    """定这一轮搜什么。

    优先级：她攒的偏好 > 群里最近提到的书名号内容 > 记忆里的关键词。
    抽出来的必须是**短词**——整条标题当搜索词，B 站搜出来是空的。
    """
    terms = []

    def push(t):
        t = (t or "").strip(" \u3000\u300a\u300b\u201c\u201d!\uff01?\uff1f,.\u3002")
        if t and 2 <= len(t) <= 14 and t not in terms:
            terms.append(t)

    # 1. 攒下来的偏好
    ups, topics = load_interests()
    for t in topics[:2]:
        push(t)

    # 2. 群里最近提到的
    buzz = group_buzz()
    for m in re.finditer(r'\u300a([^\u300b]{2,14})\u300b', buzz):
        push(m.group(1))

    # 3. 记忆里反复出现的短词。整条标题当搜索词搜不出东西，
    #    所以只取里面的英文词和大肥鱼 / DeepSeek 这类专有名词。
    mem = _memory_snippet(10)
    seen = {}
    for m in re.finditer(r'[A-Za-z][A-Za-z0-9.\-]{2,18}', mem):
        w = m.group(0)
        if w.lower() in ("dsh", "bili", "http", "https", "bv"):
            continue
        seen[w] = seen.get(w, 0) + 1
    for m in re.finditer(r'\u5927\u80a5\u9c7c|\u5c9a\u5a05|\u72fc\u4e0e\u9999\u8f9b\u6599', mem):
        w = m.group(0)
        seen[w] = seen.get(w, 0) + 1
    for w, _n in sorted(seen.items(), key=lambda kv: -kv[1])[:2]:
        push(w)

    return terms[:count]


def _content_of(m, bv):
    """把一条视频「她能看到的东西」拼出来：简介 + 字幕开头。

    拿不到字幕就返回 None —— 没看过就没资格评论。
    """
    parts = []

    # 简介
    try:
        info = m.call_json("bili_video_info", {"bvid": bv})
        d = info.get("data") or info
        desc = (d.get("desc") or "").strip()
        if desc and len(desc) > 12:
            parts.append("简介：" + desc[:400])
    except Exception:
        d = {}

    # 字幕
    sub = ""
    try:
        r = m.call_json("bili_subtitle", {"bvid": bv})
        sub = (r.get("text") or "").strip()
    except Exception:
        sub = ""

    # 歌词字幕对她没用——一条 MV 的字幕就是歌词，她没法"对内容有反应"。
    # 判断办法：看带 ♪ 的行占比。太高就只当没字幕，退回简介。
    all_lines = [ln.strip() for ln in sub.splitlines() if ln.strip()]
    song_lines = [ln for ln in all_lines if "\u266a" in ln or "\u266b" in ln]
    is_song = bool(all_lines) and len(song_lines) * 2 > len(all_lines)

    if sub and not is_song:
        body = "\n".join(ln for ln in all_lines
                         if ln != "\u97f3\u4e50" and "\u266a" not in ln)
        if body.strip():
            parts.append("字幕（她说的话 / 旁白）：" + body[:900])

    # 只有简介、而且简介太短 —— 那她还是没东西可说
    desc_len = len((d.get("desc") or "").strip())
    if not parts or (len(parts) == 1 and is_song):
        return None, (d.get("title") or ""), ((d.get("owner") or {}).get("name") or ""), ""
    if desc_len < 20 and len(parts) == 0:
        return None, (d.get("title") or ""), ((d.get("owner") or {}).get("name") or ""), ""

    body = "\n".join(parts)

    title = d.get("title") or ""
    up = ((d.get("owner") or {}).get("name")) or ""
    return "\n\n".join(parts), title, up, body[:900]


def run_one(do_comment=True, mention=False):
    result = {"ok": False, "step": "", "error": ""}
    already = seen_bvs()

    with BiliMCP() as m:
        # 1. 攒候选。先带着兴趣去搜，搜不到才退回推荐。
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
                    log("search \u201c%s\u201d -> %d" % (t, len(cands)))
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
            result["error"] = "推荐和热门里没有没刷过的视频"
            return result

        # 2. 挨个试，挑一条真有字幕的。最多试 4 条，别把时间耗光
        result["step"] = "watch"
        pick = None
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
            log("skip %s: no subtitle or description" % c["bv"])

        if not pick:
            result["error"] = "试了 %d 条都拿不到字幕或简介，这轮不发了" % len(tried)
            result["tried"] = tried
            return result

        result["bv"] = pick["bv"]
        result["title"] = pick["title"]
        result["up"] = pick["up"]

        # 3. 写评论
        comment = ""
        if do_comment:
            result["step"] = "comment"
            for _try in range(3):
                cand = gen_comment(pick["title"], pick["up"],
                                   content=content, mention=mention)
                if cand and not _is_cliche(cand):
                    comment = cand
                    break
            if not comment:
                # 三次都写不出人话，就别发了。往人家评论区扔套话最难看
                result["error"] = "三次都没写出像样的评论，这轮不发"
                result["bv"] = pick["bv"]
                result["title"] = pick["title"]
                return result

            log("comment: %s" % comment)

            # @ 由代码追加，不指望模型自己记得加。
            if mention and ("@" + ADMIN_BILI_NAME) not in comment:
                comment = comment.rstrip("，。！？,.!? ") + " @" + ADMIN_BILI_NAME + " "

            try:
                rep = m.call_json("bili_reply",
                                  {"bvid": pick["bv"], "text": comment})
                if not rep.get("success"):
                    result["error"] = "评论没发出去：" + str(rep)[:150]
                    return result
                result["rpid"] = rep.get("rpid")
                log("replied rpid=%s" % rep.get("rpid"))
            except Exception as e:
                result["error"] = "评论失败：" + str(e)[:150]
                return result

        # 4. 记录
        result["step"] = "record"
        try:
            m.call("duty_seen_append",
                   {"bvid": pick["bv"], "title": pick["title"],
                    "up": pick["up"],
                    "status": "评论" if comment else "挑了"})
        except Exception as e:
            log("record video failed: %s" % str(e)[:100])

        if comment:
            try:
                _append_comment_row(pick["bv"], result.get("rpid", ""), comment)
            except Exception as e:
                log("record comment failed: %s" % str(e)[:100])

    # 记一笔偏好：UP 和这次搜的词。下次优先从这儿找
    save_interests(pick["up"], (result.get("terms") or [""])[0])

    record_memory(pick["title"], pick["up"], comment, pick["bv"],
                  feeling=("看的是搜索词「%s」找到的" % (result.get("terms") or [""])[0]
                           if result.get("terms") else ""))

    result["ok"] = True
    result["comment"] = comment
    result["step"] = "done"
    return result


def _append_comment_row(bv, rpid, text):
    """往 seen.md 的「评论记录」表插一行（不是追加到文件末尾）。"""
    try:
        with open(SEEN, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except Exception:
        return
    row = "| %s | %s |  | %s | %s |\n" % (
        bv, rpid or "", time.strftime("%Y-%m-%d"), text.replace("|", "/")[:40])
    # 找「评论记录」那张表的分隔线，插在它后面
    in_comment = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith("## 评论记录"):
            in_comment = True
            continue
        if in_comment and ln.strip().startswith("| ---"):
            lines.insert(i + 1, row)
            with open(SEEN, "w", encoding="utf-8", newline="") as fh:
                fh.writelines(lines)
            return
    # 没有就补一张表
    add = "\n## 评论记录\n\n| BV | rpid | root | 时间 | 正文 |\n| --- | --- | --- | --- | --- |\n" + row
    with open(SEEN, "a", encoding="utf-8") as fh:
        fh.write(add)


def record_memory(title, up, comment, bv):
    """把这一趟刷站写进长期记忆。像真人一样，刷过什么、说过什么能想起来。"""
    try:
        import sqlite3
        import uuid
        db = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-memento", "memory.db")
        if not os.path.isfile(db):
            return
        text = "在 B 站看了《%s》（%s）" % (title[:40], up or "不知道谁")
        if comment:
            text += "，留了句「%s」" % comment[:30]
        text += "。" + bv
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
            "INSERT INTO audit (ts, action, track, scope, entry_id, text, outcome, source, session_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (now, "add-bili", "agent", "workspace", eid, text,
             "recorded after a bilibili run", "bili_onerun", None))
        c.commit()
        c.close()
        log("memory: " + text[:50])
    except Exception as e:
        log("record_memory failed: %s" % str(e)[:100])


def verify_comment(bv, text):
    """去 B 站看这条评论公开了没有。

    返回 {"state": "public"|"pending"|"unknown", ...}
      public  在评论区列表里找到了
      pending 没找到（多半在审核，或者被拦了）
      unknown 查不到（视频评论关着 / 接口失败）
    """
    try:
        with BiliMCP() as m:
            r = m.call_json("bili_comments", {"bvid": bv, "num": 50})
    except Exception as e:
        return {"state": "unknown", "why": str(e)[:120]}

    cms = r.get("comments") or []
    if not cms and not r.get("count"):
        return {"state": "unknown", "why": "评论区读不到"}

    # 先按正文找（最准），再按用户名兜底
    key = (text or "").strip()[:20]
    for c in cms:
        body = (c.get("content") or "").strip()
        if key and key in body:
            return {"state": "public", "rpid": c.get("rpid"),
                    "user": c.get("user"), "like": c.get("like")}
    for c in cms:
        if "\u5c9a\u5a05" in (c.get("user") or ""):
            return {"state": "public", "rpid": c.get("rpid"),
                    "user": c.get("user"), "like": c.get("like"),
                    "note": "text differs"}
    return {"state": "pending",
            "why": "评论区前 50 条里没有（多半在审核，或没通过）"}


def run_one(do_comment=True, mention=False):
    result = {"ok": False, "step": "", "error": ""}
    already = seen_bvs()

    with BiliMCP() as m:
        # 1. 挑视频
        result["step"] = "pick"
        cands = []
        for src, args in (("bili_recommend", {"count": 8}),
                          ("bili_hot_videos", {"count": 8})):
            try:
                r = m.call_json(src, args)
            except Exception as e:
                log("%s failed: %s" % (src, str(e)[:100]))
                continue
            for v in (r.get("videos") or r.get("list") or []):
                bv = v.get("bvid") or v.get("bv") or ""
                if bv and bv not in already:
                    cands.append({"bv": bv,
                                  "title": v.get("title") or "",
                                  "up": v.get("author") or v.get("up") or ""})
            if cands:
                break

        if not cands:
            result["error"] = "推荐和热门里没有没刷过的视频"
            return result

        pick = cands[0]
        result["bv"] = pick["bv"]
        result["title"] = pick["title"]
        log("pick %s %s" % (pick["bv"], pick["title"][:30]))

        # 2. 补全信息
        try:
            info = m.call_json("bili_video_info", {"bvid": pick["bv"]})
            d = info.get("data") or info
            if not pick["title"]:
                pick["title"] = d.get("title") or ""
            if not pick["up"]:
                pick["up"] = ((d.get("owner") or {}).get("name")
                              or d.get("author") or "")
        except Exception as e:
            log("info failed: %s" % str(e)[:100])

        result["up"] = pick["up"]

        # 3. 写评论（最多试两次，套话就重写）
        comment = ""
        if do_comment:
            result["step"] = "comment"
            for _try in range(3):
                cand = gen_comment(pick["title"], pick["up"], mention=mention)
                if cand and not _is_cliche(cand):
                    comment = cand
                    break
                # 两次都套话就放弃，宁可不发也不往人家评论区扔一句套话。
            if not comment:
                result["error"] = "评论没生成出来"
                return result
            log("comment: %s" % comment)

            # @ 由代码追加，不指望模型自己记得加。
            if mention and ("@" + ADMIN_BILI_NAME) not in comment:
                comment = comment.rstrip("，。！？,.!? ") + " @" + ADMIN_BILI_NAME + " "

            try:
                rep = m.call_json("bili_reply",
                                  {"bvid": pick["bv"], "text": comment})
                if not rep.get("success"):
                    result["error"] = "评论没发出去：" + str(rep)[:150]
                    return result
                result["rpid"] = rep.get("rpid")
                log("replied rpid=%s" % rep.get("rpid"))
            except Exception as e:
                result["error"] = "评论失败：" + str(e)[:150]
                return result

        # 4. 记录
        result["step"] = "record"
        # 记两处，因为它们在文件里是两张不同的表：
        #   看过的视频  | 日期 | BV | 标题 | UP | 状态 |
        #   评论记录    | BV | rpid | root | 时间 | 正文 |
        # MCP 的 duty_seen_append 只会往后追加「视频」那种行，评论会写到
        # 视频表里去，于是「她发过的评论」永远是空的。评论那行自己插。
        try:
            m.call("duty_seen_append",
                   {"bvid": pick["bv"], "title": pick["title"],
                    "up": pick["up"],
                    "status": "评论" if comment else "挑了"})
        except Exception as e:
            log("record video failed: %s" % str(e)[:100])

        if comment:
            try:
                _append_comment_row(pick["bv"], result.get("rpid", ""), comment)
            except Exception as e:
                log("record comment failed: %s" % str(e)[:100])

    record_memory(pick["title"], pick["up"], comment, pick["bv"])

    result["ok"] = True
    result["comment"] = comment
    result["step"] = "done"
    return result


if __name__ == "__main__":
    if "--verify" in sys.argv:
        i = sys.argv.index("--verify")
        bv = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
        txt = ""
        # 评论正文走 base64：中文当命令行参数传，Windows 上会被转成 GBK 乱码，
        # 于是"按正文找"永远匹配不上。
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
