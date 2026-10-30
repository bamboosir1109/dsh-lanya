# Steal memes from the group, classified by a vision model.
#
# Why not size heuristics: the first attempt guessed from dimensions and let news
# screenshots through - she re-posted one and a groupmate called it out
# ("你发图不对，没有关联"). Why not a local VLM: a multimodal model can already
# read images through the agent, so there is no reason to download several GB and
# burn VRAM on a second model.
#
# So this stages new images, asks the headless agent (which CAN see pictures) to
# judge each one, and files them accordingly:
#   memes/stolen/    real stickers and memes - usable when she replies
#   memes/rejected/  news, screenshots, photos - kept but never re-sent
#
# The images are batched into ONE agent call per run to keep the token cost low.
#
# Usage:
#   python stealmemes.py             normal scan
#   python stealmemes.py --recheck   re-judge everything already collected

# --- 隐藏控制台窗口 ---------------------------------------------------------
# 由计划任务用 python.exe 启动时，Windows 会先弹一个黑框再跑脚本。用户看到的
# "窗口反复弹出"就是这个。脚本没法阻止窗口被创建，但可以在第一行就把它藏掉，
# 把可见时间从"整个运行时长"压到启动那一瞬。

# --- 表情包行为开关（顶层，最先执行）----------------------------------------
# 面板上可以关掉「偷表情包」和「自动分类」。这一段必须在任何逻辑之前跑，
# 否则配置改了不生效。
from project_conf import data_dir, work_dir
import os as _mo
try:
    import json as _mj
    _mcfg = _mo.path.join(data_dir(), "lanya-memes-config.json")
    with open(_mcfg, "r", encoding="utf-8") as _mfh:
        _mdata = _mj.load(_mfh)
    if _mdata.get("stealEnabled") is False:
        raise SystemExit(0)
    _AUTO_CLASSIFY = _mdata.get("autoClassify") is not False
    _MAX_PER_RUN = int(_mdata.get("maxPerRun") or 12)
except SystemExit:
    raise
except Exception:
    _AUTO_CLASSIFY = True
    _MAX_PER_RUN = 12
# ---------------------------------------------------------------------------

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
import shutil
import subprocess
import sys
import time
import urllib.request
from decision_events import unified_decisions_enabled

NAPCAT = "http://127.0.0.1:8720"
# Neither the token nor the group id belongs in the source. The token was
# sitting here in plain text, which means every copy of this file carried a live
# credential; the group id was hardcoded so the script could only ever work
# against one group.
#
# Both are read from the install now:
#   token -> framework/config/onebot11.json
#   group -> <root>/<dataDir>/lanya-config.json
NAPCAT_TOKEN = ""
GROUP = ""
try:
    _nb = os.path.join(work_dir(), "napcat", "framework", "config", "onebot11.json")
    if os.path.isfile(_nb):
        with open(_nb, encoding="utf-8-sig") as _fh:
            _nd = json.load(_fh)
        _hs = ((_nd.get("network") or {}).get("httpServers") or [])
        if _hs:
            NAPCAT_TOKEN = str(_hs[0].get("token") or "")
except Exception:
    pass
try:
    _cp = os.path.join(data_dir(), "lanya-config.json")
    if os.path.isfile(_cp):
        with open(_cp, encoding="utf-8-sig") as _fh:
            _cd = json.load(_fh)
        GROUP = str(_cd.get("fanGroupQQ") or _cd.get("fanGroup") or "")
except Exception:
    pass
TOKEN = NAPCAT_TOKEN
LOOKBACK = 60

LANYA = data_dir()
MEMES = os.path.join(LANYA, "memes")
OUT = os.path.join(MEMES, "stolen")
REJ = os.path.join(MEMES, "rejected")
STAGE = os.path.join(MEMES, "_stage")
INDEX = os.path.join(MEMES, "stolen-index.md")
REJINDEX = os.path.join(MEMES, "rejected-index.md")
LOG = os.path.join(LANYA, "steal.log")
SEEN = os.path.join(MEMES, ".seen.json")
PROMPT = os.path.join(work_dir(), "napcat", "classify-meme.prompt.txt")

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
OK_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
# Images per classifier call. Handing a few hundred paths to one call overflows the
# model's context and it only answers for the first handful.
BATCH = 20


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


def ask_agent(task):
    if unified_decisions_enabled():
        raise RuntimeError('Independent meme classifier disabled in unified mode')
    """Run the headless profile. Its stdout is UTF-16LE with a BOM."""
    try:
        p = subprocess.run([DSH, "--profile", "headless", "-"], input=task.encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=600)
        raw = p.stdout
        if raw[:2] == b"\xff\xfe":
            return raw.decode("utf-16-le").lstrip("\ufeff").strip()
        try:
            return raw.decode("utf-8").strip()
        except Exception:
            return raw.decode("utf-16-le", "replace").lstrip("\ufeff").strip()
    except Exception as e:
        log("agent call failed: " + str(e)[:140])
        return ""


def _classify_batch(paths):
    """One agent call for up to BATCH images."""
    with open(PROMPT, "r", encoding="utf-8") as fh:
        tpl = fh.read()
    listing = "\n".join("%d. %s" % (i + 1, p) for i, p in enumerate(paths))
    task = tpl.replace("{{LIST}}", listing)
    ans = ask_agent(task)

    verdict = {}
    for line in ans.split("\n"):
        line = line.strip()
        if not line:
            continue
        # accept "3. MEME" / "3 MEME" / "3、MEME"
        head = line.replace("\u3001", " ").replace(".", " ").replace("\uff0e", " ")
        parts = head.split()
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        idx = int(parts[0]) - 1
        if 0 <= idx < len(paths):
            verdict[paths[idx]] = parts[1].upper().startswith("MEME")
    if not verdict:
        log("classifier returned nothing usable for a batch; answer was: " + ans[:200])
    return verdict


def classify(paths):
    """Ask the vision model which of these are memes. Returns {path: bool}.

    Batched: handing 278 paths to one call overflows the model's context and it
    answers for only the first handful. BATCH at a time keeps every answer aligned
    with what was actually shown.
    """
    if not paths:
        return {}
    out = {}
    total = len(paths)
    for start in range(0, total, BATCH):
        chunk = paths[start:start + BATCH]
        log("classifying %d-%d of %d" % (start + 1, start + len(chunk), total))
        out.update(_classify_batch(chunk))
    return out
    return verdict


def note(path, name, who, verdict):
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("- `%s`  (%s, %s)  %s\n" % (name, who, time.strftime("%m-%d %H:%M"), verdict))
    except Exception:
        pass


def place(src, name, who, is_meme, verdict):
    d = OUT if is_meme else REJ
    os.makedirs(d, exist_ok=True)
    stem, ext = os.path.splitext(name)
    if ext.lower() not in OK_EXT:
        ext = os.path.splitext(src)[1] or ".jpg"
    dst = os.path.join(d, stem + ext)
    if os.path.exists(dst):
        return False
    shutil.move(src, dst)
    note(INDEX if is_meme else REJINDEX, os.path.basename(dst), who, verdict)
    return True


def run_scan():
    if unified_decisions_enabled():
        from meme_observations import observe_existing_memes
        result = observe_existing_memes(limit=_MAX_PER_RUN)
        print(json.dumps(result, ensure_ascii=True))
        return result
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(REJ, exist_ok=True)
    os.makedirs(STAGE, exist_ok=True)
    seen = load(SEEN, [])
    seenset = set(seen)

    try:
        h = api("get_group_msg_history", {"group_id": GROUP, "count": LOOKBACK})
    except Exception as e:
        log("history failed: " + str(e)[:120])
        return
    msgs = (h.get("data") or {}).get("messages") or []

    staged = []          # (stage_path, original_name, who)
    for m in msgs:
        segs = m.get("message")
        if not isinstance(segs, list):
            continue
        who = ((m.get("sender") or {}).get("nickname") or "?")
        for s in segs:
            if not isinstance(s, dict) or s.get("type") != "image":
                continue
            data = s.get("data") or {}
            name = str(data.get("file") or "")
            if name == "" or name in seenset:
                continue
            seenset.add(name)
            try:
                r = api("get_image", {"file": name})
                src = ((r.get("data") or {}).get("file") or "")
                if src == "" or not os.path.isfile(src):
                    continue
                stem, ext = os.path.splitext(name)
                if ext.lower() not in OK_EXT:
                    ext = os.path.splitext(src)[1] or ".jpg"
                sp = os.path.join(STAGE, stem + ext)
                shutil.copy2(src, sp)
                staged.append((sp, name, who))
            except Exception as e:
                log("image failed " + name + ": " + str(e)[:90])

    if not staged:
        log("nothing new")
        save(SEEN, sorted(seenset))
        return

    log("staged %d new image(s); asking the vision model" % len(staged))
    verdict = classify([p for p, _n, _w in staged])

    kept = dropped = unknown = 0
    for sp, _name, who in staged:
        if sp in verdict:
            ok = verdict[sp]
            if place(sp, os.path.basename(sp), who, ok, "vision: " + ("meme" if ok else "not-meme")):
                if ok:
                    kept += 1
                else:
                    dropped += 1
        else:
            # No verdict: keep it out of the usable library rather than risk
            # re-posting a news screenshot.
            if place(sp, os.path.basename(sp), who, False, "vision: unjudged"):
                unknown += 1

    save(SEEN, sorted(seenset))
    log("done: +%d memes, +%d rejected (%d unjudged), %d usable total"
        % (kept, dropped, unknown, len([f for f in os.listdir(OUT) if not f.startswith(".")])))


def run_recheck():
    """Re-judge the existing library - used to clean up the size-based era."""
    if unified_decisions_enabled():
        from meme_observations import observe_existing_memes
        result = observe_existing_memes(limit=_MAX_PER_RUN, recheck=True)
        print(json.dumps(result, ensure_ascii=True))
        return result
    os.makedirs(STAGE, exist_ok=True)
    files = sorted(glob.glob(os.path.join(OUT, "*")))
    if not files:
        log("recheck: nothing to do")
        return
    moved = []
    for f in files:
        dst = os.path.join(STAGE, os.path.basename(f))
        shutil.move(f, dst)
        moved.append(dst)
    verdict = classify(moved)
    kept = dropped = 0
    for sp in moved:
        ok = verdict.get(sp, False)
        if place(sp, os.path.basename(sp), "(rechecked)", ok, "vision: " + ("meme" if ok else "not-meme")):
            if ok:
                kept += 1
            else:
                dropped += 1
    # refresh the index so it lists only what is actually usable
    try:
        live = set(os.listdir(OUT))
        if os.path.isfile(INDEX):
            with open(INDEX, "r", encoding="utf-8") as fh:
                keep = [l for l in fh if "`" in l and l.split("`")[1] in live]
            with open(INDEX, "w", encoding="utf-8") as fh:
                fh.writelines(keep)
    except Exception:
        pass
    log("recheck: %d kept as memes, %d moved to rejected" % (kept, dropped))


if __name__ == "__main__":
    if "--recheck" in sys.argv:
        run_recheck()
    else:
        run_scan()


