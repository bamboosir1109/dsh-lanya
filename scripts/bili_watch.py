# -*- coding: utf-8 -*-
"""真看视频：下载 -> 抽帧 -> 拼图，而不是靠故事板假装看过。

为什么不用 B 站自带的故事板
--------------------------
`bili_frames` 用的是 videoshot 接口。实测一个 71 秒的视频：

    拼图 4800x2700，只有 51 KB
    每格亮度均值 1.2（满分 255），第 11 格以后纯黑 0.0

**故事板是给进度条悬停预览用的**，压缩到几乎没有信息量，裁出来的帧基本全黑。
拿它当"我看了视频"的证据，就是假装。

真办法
------
B 站给的是 DASH 流，`get_download_url()` 能拿到 m4s 直链。
按最低清晰度下一份（71 秒的 360p 只有 0.2 MB），再用 ffmpeg 按间隔抽帧。
抽出来的帧是**能读字的真实画面**。

间隔怎么定
----------
不能一个间隔走天下：

    时长 <= 60s     每 5 秒
    <= 3 分钟       每 10 秒
    <= 10 分钟      每 20 秒
    > 10 分钟       每 40 秒

上限 24 帧，超了就自动拉大间隔。太多帧既吃上下文又没意义。

用法
----
    python bili_watch.py BV1xx411c7mD
    python bili_watch.py BV1xx411c7mD --every 8
    python bili_watch.py BV1xx411c7mD --max 12

输出 JSON，`frames` 里是抽好的图片路径（已按时间顺序命名）。
让 agent 直接读这些图就能真看懂。
"""
import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HOME = os.path.expanduser("~")
BILI_HOME = os.path.join(HOME, ".dsh", "bili-duty")
sys.path.insert(0, BILI_HOME)

WORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(WORK, "\u5c9a\u5a05")
FRAMEDIR = os.path.join(DATA, "made", "_videoframes")

# ffmpeg next to the TTS install; also try PATH.
FFMPEG_CANDIDATES = [
    os.path.join(WORK, "tts", "bin", "ffmpeg.exe"),
    r"C:\ffmpeg\bin\ffmpeg.exe",
]


def find_ffmpeg():
    for p in FFMPEG_CANDIDATES:
        if os.path.isfile(p):
            return p
    from shutil import which
    return which("ffmpeg")


def pick_interval(duration, max_frames):
    """帧间隔：既要有代表性，又别把上下文撑爆。"""
    for limit, step in ((60, 5), (180, 10), (600, 20)):
        if duration <= limit:
            n = max(1, int(duration // step))
            if n <= max_frames:
                return step
    # 长视频：按 max_frames 均分
    if duration <= 0:
        return 20
    return max(5, int(duration / max(1, max_frames)))


def load_cred():
    from bilibili_api import Credential
    p = os.path.join(BILI_HOME, "bili_credential.json")
    try:
        with io.open(p, encoding="utf-8-sig") as fh:
            d = json.load(fh)
    except Exception:
        return None
    return Credential(sessdata=d.get("sessdata") or d.get("SESSDATA"),
                      bili_jct=d.get("bili_jct") or d.get("BILI_JCT"),
                      buvid3=d.get("buvid3") or d.get("BUVID3"),
                      dedeuserid=d.get("dedeuserid") or d.get("DedeUserID"))


async def fetch(bvid, max_frames):
    """拿直链并下载。返回 (本地文件, 标题, 时长, 实际用到的清晰度)。"""
    import aiohttp
    from bilibili_api import video

    cred = load_cred()
    v = video.Video(bvid=bvid, credential=cred)
    info = await v.get_info()
    title = info.get("title") or ""
    duration = float(info.get("duration") or 0)

    data = await v.get_download_url(0)
    dash = data.get("dash") or {}
    vids = dash.get("video") or []
    if not vids:
        durl = data.get("durl") or []
        if not durl:
            raise RuntimeError("no playable stream")
        url = durl[0].get("url")
        quality = "durl"
    else:
        # 最低带宽那一路：文件小、下得快，480 宽足够读字
        vids = sorted(vids, key=lambda x: x.get("bandwidth") or 0)
        url = vids[0].get("baseUrl") or vids[0].get("base_url")
        quality = str(vids[0].get("id"))

    hdr = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"),
        "Referer": "https://www.bilibili.com/video/" + bvid,
    }
    tmpdir = tempfile.mkdtemp(prefix="biliw_")
    # ffmpeg 对 8.3 短路径会 I/O 失败，所以落在看得见的绝对路径下
    local = os.path.join(FRAMEDIR, "_src.m4s")
    os.makedirs(FRAMEDIR, exist_ok=True)

    async with aiohttp.ClientSession(headers=hdr) as sess:
        async with sess.get(url, timeout=aiohttp.ClientTimeout(total=300)) as r:
            if r.status != 200:
                raise RuntimeError("stream http %d" % r.status)
            body = await r.read()
    with open(local, "wb") as fh:
        fh.write(body)

    return local, title, duration, quality, len(body)


def extract(src, outdir, interval, max_frames):
    ff = find_ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg not found")
    if os.path.isdir(outdir):
        for f in os.listdir(outdir):
            try:
                os.remove(os.path.join(outdir, f))
            except Exception:
                pass
    os.makedirs(outdir, exist_ok=True)

    pattern = os.path.join(outdir, "frame_%03d.jpg")
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-i", src,
           "-vf", "fps=1/%d,scale=480:-2" % interval,
           "-frames:v", str(max_frames), "-q:v", "3", "-y", pattern]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg: " + (p.stderr or "")[:200])

    out = []
    for f in sorted(os.listdir(outdir)):
        if f.startswith("frame_") and f.endswith(".jpg"):
            out.append(os.path.join(outdir, f))
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(json.dumps({"ok": False, "error": "usage: bili_watch.py <bvid>"},
                         ensure_ascii=False))
        return 1
    bvid = args[0].strip()
    every = 0
    max_frames = 24
    for a in sys.argv[1:]:
        if a.startswith("--every="):
            try:
                every = int(a.split("=", 1)[1])
            except Exception:
                pass
        if a.startswith("--max="):
            try:
                max_frames = max(1, min(40, int(a.split("=", 1)[1])))
            except Exception:
                pass

    result = {"ok": False, "bvid": bvid}
    try:
        src, title, duration, quality, size = asyncio.run(
            fetch(bvid, max_frames))
        result.update({"title": title, "duration": duration,
                       "quality": quality,
                       "download_mb": round(size / 1048576.0, 2)})
        step = every if every > 0 else pick_interval(duration, max_frames)
        result["interval_sec"] = step

        outdir = os.path.join(FRAMEDIR, bvid)
        frames = extract(src, outdir, step, max_frames)
        result["ok"] = bool(frames)
        result["count"] = len(frames)
        result["frames"] = frames
        # 抽完就把源删掉：一次性的，留着重看没意义还占地方
        try:
            os.remove(src)
        except Exception:
            pass
    except Exception as e:
        result["error"] = str(e)[:300]

    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
