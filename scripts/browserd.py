# Persistent browser for Lanya - the "look at the screen, then act" loop.
#
# Why a long-lived process: a game needs ONE browser session that keeps its state.
# Launching a fresh browser per command would reload the page every time and lose
# the game. So this stays alive, watches a command file, and answers with a fresh
# screenshot.
#
# Protocol (files live in 岚娅\browser\):
#   cmd.json    <- she writes one action:  {"action": "goto", "url": "..."}
#                                          {"action": "click", "x": 400, "y": 300}
#                                          {"action": "key", "key": "ArrowLeft"}
#                                          {"action": "type", "text": "hello"}
#                                          {"action": "wait", "ms": 800}
#                                          {"action": "shot"}
#                                          {"action": "scroll", "dy": 400}
#   shot.png    -> always refreshed after every action, so she can look again
#   result.json -> {"seq": n, "ok": true/false, "note": "...", "url": "...",
#                   "shot": "<path>"}
#
# She reads shot.png with her image tool, decides, and writes the next cmd.json.
# This file is only ASCII outside strings; the Chinese folder name is resolved
# relative to this script's own location's parent.

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
import asyncio
import json
import os
import sys
import time

from playwright.async_api import async_playwright

BASE = r"<WORKDIR>"
LANYA = os.path.join(BASE, "\u5c9a\u5a05")          # "Lanya"
BROWSER_DIR = os.path.join(LANYA, "browser")
CMD = os.path.join(BROWSER_DIR, "cmd.json")
SHOT = os.path.join(BROWSER_DIR, "shot.png")
RESULT = os.path.join(BROWSER_DIR, "result.json")
LOG = os.path.join(BROWSER_DIR, "browser.log")
SESSION = os.path.join(BROWSER_DIR, "session.json")

VIEWPORT = {"width": 1280, "height": 800}

# This machine reaches the outside world only through a local proxy. Chromium does
# NOT pick up the shell's HTTPS_PROXY when the daemon is started by a scheduled
# task, so the proxy must be handed to it explicitly. Without this every page came
# back as the browser's own "404 - page not found" error page: verified by loading
# 4399 successfully with the proxy and failing on the same URL without it.
PROXY = "http://127.0.0.1:17891"


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
        return json.loads(raw) if raw else default
    except Exception:
        return default


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    os.replace(tmp, path)


async def do_action(page, cmd):
    a = (cmd.get("action") or "").strip().lower()
    if a == "goto":
        await page.goto(cmd["url"], timeout=60000, wait_until="domcontentloaded")
        return "goto " + str(cmd["url"])
    if a == "click":
        await page.mouse.click(float(cmd.get("x", 0)), float(cmd.get("y", 0)))
        return "click %s,%s" % (cmd.get("x"), cmd.get("y"))
    if a == "key":
        await page.keyboard.press(str(cmd.get("key")))
        return "key " + str(cmd.get("key"))
    if a == "keydown":
        await page.keyboard.down(str(cmd.get("key")))
        return "keydown " + str(cmd.get("key"))
    if a == "keyup":
        await page.keyboard.up(str(cmd.get("key")))
        return "keyup " + str(cmd.get("key"))
    if a == "type":
        await page.keyboard.type(str(cmd.get("text", "")))
        return "type"
    if a == "wait":
        await asyncio.sleep(max(0, float(cmd.get("ms", 500))) / 1000.0)
        return "wait"
    if a == "scroll":
        await page.mouse.wheel(0, float(cmd.get("dy", 300)))
        return "scroll"
    if a == "back":
        await page.go_back()
        return "back"
    if a == "shot":
        return "shot"
    return "unknown action: " + a


async def main():
    os.makedirs(BROWSER_DIR, exist_ok=True)
    log("browserd start")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, proxy={"server": PROXY})
        ctx = await browser.new_context(viewport=VIEWPORT, locale="zh-CN")
        page = await ctx.new_page()

        last_seq = 0
        if os.path.exists(CMD):
            try:
                os.remove(CMD)
            except Exception:
                pass

        while True:
            cmd = read_json(CMD, None)
            if not cmd:
                await asyncio.sleep(1.5)
                continue

            seq = int(cmd.get("seq", 0) or 0)
            try:
                os.remove(CMD)
            except Exception:
                pass
            if seq and seq <= last_seq:
                continue
            last_seq = seq or (last_seq + 1)

            note, ok = "", True
            try:
                note = await do_action(page, cmd)
            except Exception as e:
                ok = False
                note = "%s: %s" % (type(e).__name__, str(e)[:200])
            log(("ok " if ok else "FAIL ") + note)

            # Always hand back a fresh screenshot so the next decision is grounded.
            shot_ok = True
            try:
                await page.screenshot(path=SHOT, timeout=20000)
            except Exception as e:
                shot_ok = False
                log("screenshot failed: " + str(e)[:120])

            try:
                url = page.url
            except Exception:
                url = ""
            write_json(RESULT, {
                "seq": last_seq,
                "ok": ok,
                "note": note,
                "url": url,
                "shot": SHOT if shot_ok else "",
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })

            # Keep the page title too - cheap and tells her where she is.
            try:
                write_json(SESSION, {"url": url, "title": await page.title()})
            except Exception:
                pass


if __name__ == "__main__":
    # Never die quietly. The daemon used to run main() once: any exception that
    # escaped the command loop (a failed write of result.json, for instance) killed
    # the process with nothing in the log, and the scheduler silently started a
    # fresh one ~10 minutes later - which is why she kept reporting "the browser
    # died" while the log only ever showed clean "browserd start" lines.
    while True:
        try:
            asyncio.run(main())
            log("browserd main loop returned; restarting")
        except KeyboardInterrupt:
            log("browserd interrupted")
            break
        except Exception as e:
            log("browserd crashed (%s): %s - restarting in 5s"
                % (type(e).__name__, str(e)[:200]))
            time.sleep(5)


