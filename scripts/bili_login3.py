# -*- coding: utf-8 -*-
"""B 站扫码登录（自己走官方接口，不用 bilibili_api 的 QrCodeLogin）。

为什么不用库：它的 check_state() 拿到成功响应后是这么解析 cookie 的——
    cookies_list = cred_url.split("?")[1].split("&")
    for cookie in cookies_list:
        if cookie[:8] == "SESSDATA": sessdata = cookie[9:]
B 站返回的 url 现在带了 URL 编码、字段顺序也变了，这样切出来是空的，
于是 get_credential() 给出一个全空的 Credential —— 扫码明明成功了却存不下来。

自己走接口：
  1. GET  qrcode/generate        -> qrcode_key + url
  2. 把 url 画成二维码
  3. GET  qrcode/poll?qrcode_key -> code 86101 未扫 / 86090 已扫待确认 /
                                    86038 过期 / 0 成功
  4. 成功时响应里的 url 用 urllib.parse 正经解析出 cookie
"""
import io
import json
import os
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
QR_PNG = os.path.join(HERE, "bili-qr.png")
QR_JSON = os.path.join(HERE, "bili-qr.json")
CRED = os.path.join(os.path.expanduser("~"), ".dsh", "bili-duty", "bili_credential.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

MSG = {
    86101: ("waiting", "等你扫码", "SCAN"),
    86090: ("waiting", "扫到了，在手机上点一下确认", "CONF"),
    86038: ("expired", "二维码过期了，重新点一次", "TIMEOUT"),
}


def note(state, msg, step=""):
    try:
        with io.open(QR_JSON, "w", encoding="utf-8") as fh:
            json.dump({"state": state, "msg": msg, "step": step,
                       "ts": int(time.time())}, fh, ensure_ascii=False)
    except Exception:
        pass


def get_json(url):
    req = urllib.request.Request(url, headers={"user-agent": UA,
                                               "referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(req, timeout=25) as r:
        body = json.loads(r.read().decode("utf-8"))
        # 新版 B 站把 cookie 放在 Set-Cookie 响应头里，不在 data.url 上
        try:
            body["__set_cookie"] = r.headers.get_all("Set-Cookie") or []
        except Exception:
            body["__set_cookie"] = []
        return body


def main():
    # 1. 申请二维码
    try:
        gen = get_json("https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
    except Exception as e:
        note("failed", "申请二维码失败：%s" % str(e)[:120], "GENERR")
        return

    if gen.get("code") != 0:
        note("failed", "B 站返回错误：%s" % gen.get("message"), "GENFAIL")
        return

    key = gen["data"]["qrcode_key"]
    url = gen["data"]["url"]

    # 2. 画二维码
    try:
        import qrcode
        img = qrcode.make(url)
        img.save(QR_PNG)
    except Exception as e:
        note("failed", "画二维码失败：%s" % str(e)[:120], "QRFAIL")
        return

    note("waiting", MSG[86101][1], "SCAN")

    # 3. 轮询
    poll = ("https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
            "?qrcode_key=" + urllib.parse.quote(key))
    last = ""
    for _ in range(180):
        try:
            res = get_json(poll)
        except Exception as e:
            note("failed", "查状态失败：%s" % str(e)[:120], "POLLERR")
            return

        # B 站的 poll 接口：外层 code 是「请求成功」，真正的扫码状态在
        # data.code 里（86101 未扫 / 86090 已扫待确认 / 86038 过期 / 0 成功）。
        # 之前读外层 code，一调用就是 0，所以每次都误判成"扫码成功"。
        outer = res.get("code", -1)
        if outer != 0:
            note("failed", "接口错误 %s：%s" % (outer, res.get("message")), "APPERR")
            return
        d = res.get("data") or {}
        code = d.get("code", -1)

        if code == 0:
            got = {}

            # ① Set-Cookie 响应头（新版 B 站走这条）
            for raw in (res.get("__set_cookie") or []):
                pair = raw.split(";", 1)[0]
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    got[k.strip()] = v.strip()

            # ② data.url 里的 query（老版走这条）
            cred_url = (res.get("data") or {}).get("url") or ""
            if cred_url:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(cred_url).query)
                for k in ("SESSDATA", "bili_jct", "DedeUserID", "buvid3"):
                    if k in qs and k not in got:
                        got[k] = qs[k][0]

            sess = got.get("SESSDATA", "")
            jct = got.get("bili_jct", "")
            uid = got.get("DedeUserID", "")
            if not (sess and jct):
                note("failed",
                     "扫码成功但没拿到 cookie（data.url=%s，Set-Cookie=%d 条）"
                     % (cred_url[:40], len(res.get("__set_cookie") or [])), "EMPTY")
                return
            data = {"sessdata": sess, "bili_jct": jct,
                    "dedeuserid": uid, "buvid3": got.get("buvid3", "")}
            os.makedirs(os.path.dirname(CRED), exist_ok=True)
            with io.open(CRED, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            note("done", "登录成功，凭证已保存", "DONE")
            return

        st = MSG.get(code)
        if st is None:
            note("failed", "未知状态码 %s：%s" % (code, res.get("message")), "UNK")
            return
        if st[2] != last:
            note(st[0], st[1], st[2])
            last = st[2]
        if code == 86038:
            return

        time.sleep(2)

    note("expired", "等太久了，重新点一次", "LONG")


if __name__ == "__main__":
    main()
