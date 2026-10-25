# -*- coding: utf-8 -*-
"""把所有 POST 接口挨个走一遍，看它到底写没写。

起因：零花钱「保存额度」点了没反应。查下去发现 do_POST 里那个
`/api/budget` 是个**只读**处理器（被我早前的"修路由"脚本从 do_GET 挪过来的），
面板拿到 ok:true 就以为存上了，其实一个字没写。

同一类错误可能还有。这里不看代码，看**行为**：
每个 POST 打一次，前后读状态比对，没变化的标出来。
"""
import json
import os
import sys
import urllib.request
import urllib.error

API = "http://127.0.0.1:19388"


def get(path):
    try:
        with urllib.request.urlopen(API + path, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"_err": str(e)[:100]}


def post(path, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(API + path, data=data, method="POST",
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "body": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:
        return {"_err": str(e)[:100]}


def main():
    rows = []

    def note(name, ok, detail):
        rows.append((name, ok, detail))
        print(("  OK   " if ok else "  FAIL ") + name + "   " + detail)

    print("=== 1. /api/budget 保存额度 ===")
    b0 = get("/api/budget").get("budget") or {}
    want = 3.5 if abs((b0.get("limit") or 0) - 3.5) > 0.01 else 4.5
    post("/api/budget", {"budget": {"limit": want,
                                    "dmLimit": b0.get("dmLimit", 0),
                                    "groupLimit": b0.get("groupLimit", 0),
                                    "askWhenExhausted": True}})
    b1 = get("/api/budget").get("budget") or {}
    note("/api/budget 写 limit", abs((b1.get("limit") or 0) - want) < 0.01,
         "%s -> %s (期望 %s)" % (b0.get("limit"), b1.get("limit"), want))

    print("=== 2. /api/budget/topup 记花费 ===")
    c0 = get("/api/budget").get("budget") or {}
    post("/api/budget/topup", {"amount": -0.01})
    c1 = get("/api/budget").get("budget") or {}
    note("/api/budget/topup 负数记账", (c1.get("spent") or 0) > (c0.get("spent") or 0),
         "%s -> %s" % (c0.get("spent"), c1.get("spent")))

    print("=== 3. /api/tunables 群发言参数 ===")
    t0 = (get("/api/tunables").get("tunables") or {})
    tw = 100 if t0.get("QuietMinutes") == 90 else 90
    post("/api/tunables", {"tunables": {"QuietMinutes": tw}})
    t1 = (get("/api/tunables").get("tunables") or {})
    note("/api/tunables 写 QuietMinutes", t1.get("QuietMinutes") == tw,
         "%s -> %s (期望 %s)" % (t0.get("QuietMinutes"), t1.get("QuietMinutes"), tw))

    print("=== 4. /api/config 身份 ===")
    g0 = get("/api/config").get("config") or {}
    probe = "probe-" + str(int(__import__("time").time()))
    g0["effectProbe"] = probe
    post("/api/config", {"config": g0})
    g1 = get("/api/config").get("config") or {}
    note("/api/config 写字段", g1.get("effectProbe") == probe,
         "effectProbe=%s" % g1.get("effectProbe"))
    # 清掉探针
    if g1.get("effectProbe"):
        g1.pop("effectProbe", None)
        post("/api/config", {"config": g1})

    print("=== 5. /api/memes/config 偷图开关 ===")
    m0 = get("/api/memes/config").get("config") or {}
    cur = bool(m0.get("stealEnabled", True))
    post("/api/memes/config", {"config": dict(m0, stealEnabled=(not cur))})
    m1 = get("/api/memes/config").get("config") or {}
    note("/api/memes/config 翻开关", bool(m1.get("stealEnabled")) != cur,
         "%s -> %s" % (cur, m1.get("stealEnabled")))
    post("/api/memes/config", {"config": dict(m1, stealEnabled=cur)})

    print("=== 6. /api/memory/add 记忆 ===")
    n0 = len((get("/api/memory").get("memory") or {}).get("entries") or [])
    probe2 = "effect-" + str(int(__import__("time").time()))
    post("/api/memory/add", {"text": probe2, "track": "agent"})
    ents = (get("/api/memory").get("memory") or {}).get("entries") or []
    note("/api/memory/add 真加进去", len(ents) == n0 + 1, "%d -> %d" % (n0, len(ents)))

    print("=== 7. /api/persona 人设 ===")
    p0 = get("/api/persona")
    ptxt = (p0.get("persona") or {}).get("text") or p0.get("text") or ""
    post("/api/persona", {"text": ptxt + "\n\n<!-- probe -->\n"})
    p1 = get("/api/persona")
    p1txt = (p1.get("persona") or {}).get("text") or p1.get("text") or ""
    note("/api/persona 写进去", len(p1txt) > len(ptxt), "%d -> %d" % (len(ptxt), len(p1txt)))
    if ptxt:
        post("/api/persona", {"text": ptxt})

    print("=== 8. /api/activities 开关 ===")
    a0 = ((get("/api/activities").get("data") or {}).get("activities") or [])
    if a0:
        was = a0[0].get("enabled")
        aid = a0[0].get("id")
        # 面板发的是 {"enabled": {id: bool}}，不是整个数组
        post("/api/activities", {"enabled": {aid: (not was)}})
        a1 = ((get("/api/activities").get("data") or {}).get("activities") or [])
        now = a1[0].get("enabled") if a1 else None
        note("/api/activities 存开关", now != was, "%s -> %s" % (was, now))
        post("/api/activities", {"enabled": {aid: was}})
    else:
        note("/api/activities", False, "没有活动可测")

    print("=== 9. /api/napcat/profile 资料 ===")
    np0 = get("/api/napcat/profile")
    if np0.get("ok"):
        sig = (np0.get("profile") or {}).get("signature") or ""
        new_sig = sig + "" if sig.endswith("\u2713") else (sig + "\u2713")
        r = post("/api/napcat/profile", {"signature": new_sig})
        np1 = get("/api/napcat/profile")
        note("/api/napcat/profile 写签名",
             ((np1.get("profile") or {}).get("signature") or "") == new_sig,
             "返回 %s，读回 %s" % (str(r)[:40], str((np1.get("profile") or {}).get("signature"))[:30]))
        if sig:
            post("/api/napcat/profile", {"signature": sig})
    else:
        note("/api/napcat/profile", False, str(np0)[:80])

    # ---- 汇总 ----
    bad = [r for r in rows if not r[1]]
    print("")
    print("=== 汇总 ===")
    print("  接口 %d 个，没生效 %d 个" % (len(rows), len(bad)))
    for n, _ok, d in bad:
        print("    FAIL  " + n + "   " + d)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
