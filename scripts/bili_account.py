# -*- coding: utf-8 -*-
"""B 站账号操作：删自己的评论、关注人、发带 @ 的评论。

插件那 32 个工具里没有这些，但 B 站有接口，用凭证直接调。

删除评论   POST /x/v2/reply/del        需要 oid(aid) + rpid + csrf
关注/取关  POST /x/relation/modify     需要 fid + act(1关注/2取关) + csrf
发带 @ 评论 走 bili_reply，正文里写 "@名字 "

csrf = bili_jct。所有 POST 都要 Referer 和 UA，否则风控。
"""
import json
import os
import time
import urllib.parse
import urllib.request

CRED = os.path.join(os.path.expanduser("~"), ".dsh", "bili-duty",
                    "bili_credential.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _cred():
    with open(CRED, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _cookie(c):
    parts = []
    if c.get("sessdata"):
        parts.append("SESSDATA=" + c["sessdata"])
    if c.get("bili_jct"):
        parts.append("bili_jct=" + c["bili_jct"])
    if c.get("buvid3"):
        parts.append("buvid3=" + c["buvid3"])
    if c.get("dedeuserid"):
        parts.append("DedeUserID=" + str(c["dedeuserid"]))
    return "; ".join(parts)


def _post(url, form, c):
    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "user-agent": UA,
        "referer": "https://www.bilibili.com/",
        "origin": "https://www.bilibili.com",
        "content-type": "application/x-www-form-urlencoded",
        "cookie": _cookie(c),
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(url, c):
    req = urllib.request.Request(url, headers={
        "user-agent": UA,
        "referer": "https://www.bilibili.com/",
        "cookie": _cookie(c),
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def aid_of(bvid):
    """BV 号 -> aid。删评论要 aid，不是 BV。"""
    c = _cred()
    d = _get("https://api.bilibili.com/x/web-interface/view?bvid=" + bvid, c)
    if d.get("code") != 0:
        return None
    return (d.get("data") or {}).get("aid")


def delete_comment(bvid, rpid):
    """删自己在某条视频下的评论。返回 (ok, 说明)。"""
    c = _cred()
    aid = aid_of(bvid)
    if not aid:
        return False, "拿不到 aid（视频不存在或接口失败）"
    try:
        d = _post("https://api.bilibili.com/x/v2/reply/del",
                  {"oid": aid, "rpid": rpid, "type": 1,
                   "csrf": c.get("bili_jct", "")}, c)
    except Exception as e:
        return False, str(e)[:150]
    if d.get("code") == 0:
        return True, "删掉了"
    return False, "code=%s %s" % (d.get("code"), d.get("message"))


def follow(uid, act=1):
    """act=1 关注，act=2 取关。"""
    c = _cred()
    try:
        d = _post("https://api.bilibili.com/x/relation/modify",
                  {"fid": uid, "act": act, "re_src": 11,
                   "csrf": c.get("bili_jct", "")}, c)
    except Exception as e:
        return False, str(e)[:150]
    if d.get("code") == 0:
        return True, "关注了" if act == 1 else "取关了"
    # 22003 = 已经在关注
    if d.get("code") == 22003:
        return True, "本来就关注着"
    return False, "code=%s %s" % (d.get("code"), d.get("message"))


def following_list(uid, ps=50):
    """看某人关注了谁，用来判断关注成没成。"""
    c = _cred()
    try:
        d = _get("https://api.bilibili.com/x/relation/followings?vmid=%s&ps=%s&pn=1"
                 % (uid, ps), c)
        if d.get("code") != 0:
            return []
        return [x.get("mid") for x in ((d.get("data") or {}).get("list") or [])]
    except Exception:
        return []


def my_uid():
    c = _cred()
    return c.get("dedeuserid") or ""


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "who":
        print(json.dumps({"uid": my_uid()}, ensure_ascii=False))
    elif cmd == "follow":
        uid = int(sys.argv[2])
        print(json.dumps(follow(uid, 1), ensure_ascii=False))
    elif cmd == "unfollow":
        uid = int(sys.argv[2])
        print(json.dumps(follow(uid, 2), ensure_ascii=False))
    elif cmd == "del":
        bv, rpid = sys.argv[2], sys.argv[3]
        print(json.dumps(delete_comment(bv, rpid), ensure_ascii=False))
    elif cmd == "following":
        print(json.dumps(following_list(my_uid()), ensure_ascii=False))
    else:
        print("usage: who | follow <uid> | unfollow <uid> | del <bv> <rpid> | following")
