# -*- coding: utf-8 -*-
"""真看视频 v2：高一点的分辨率 + 密集抽帧 + 拼图。

为什么改
--------
v1 是「480 宽、最多 24 帧、均匀撒在全片」。实测有两个问题：

**① 480 宽的字太小。** 改成 640 宽之后，屏幕录制里的评论正文
（"1261个包里只有143个有下载记录，这数字确实扎心"）**完全读得清**。

**② 均匀撒点等于什么都没看清。** 一条 6 分钟的视频撒 12 帧，
等于每 30 秒只看一眼——知道"它是个视频"，不知道"它讲了什么"。

真正有用的做法
--------------
**在同一位置密集取样，然后拼成一张图。**

实测 71 秒的视频，每 2 秒取一帧 = 35 帧。
把其中 9 帧拼成 3×3 的一张，字全都读得清：

    昨天我发了一条数据视频      播放 19 / 评论 13
    @画玖黎 1261个包里只有143个有下载记录，这数字确实扎心。
    这不是抄袭 / 是模板          把视频压成一条给主人看的简报

**九帧一张，三张就是二十七帧**，够她看懂一条短视频的起承转合了。

为什么不用场景切换检测
----------------------
试过 `select='gt(scene,0.15)'`，对这种屏幕录制**出 0 帧**——
画面变化太小，不算镜头切换。所以老老实实按时间撒点。

踩到的坑
--------
`-vf ... -q:v 3` 直接出 jpg 会报
`Non full-range YUV is non-standard`，必须补 `format=yuvj420p`。

用法
----
    python bili_watch.py BV1xx411c7mD
    python bili_watch.py BV1xx411c7mD --every=2          # 更密
    python bili_watch.py BV1xx411c7mD --from=30 --to=90  # 只看这一段
    python bili_watch.py BV1xx411c7mD --max=36           # 多取几帧

输出 JSON：`sheets` 是拼好的大图（**优先读这个**），`frames` 是原始单帧。
"""
import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile

from project_conf import data_dir, work_dir

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HOME = os.path.expanduser("~")
BILI_HOME = os.path.join(HOME, ".dsh", "bili-duty")
sys.path.insert(0, BILI_HOME)

WORK = work_dir()
DATA = data_dir()
FRAMEDIR = os.path.join(DATA, "made", "_videoframes")

# 640 是实测能读清屏幕文字的下限；再大只是白占带宽。
TILE_W = 640
GRID = 3          # 3x3 = 每张拼图九帧
MAX_FRAMES = 36   # 36 帧 = 4 张拼图

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
    """间隔：短视频每秒一帧太密，长视频太疏，按长度折中。"""
    if duration <= 0:
        return 5
    # 让总帧数尽量贴到 max_frames，但有下限，别取成每秒十帧
    step = duration / max(1, max_frames)
    if duration <= 90:
        return max(2, int(step))
    if duration <= 300:
        return max(3, int(step))
    return max(5, int(step))


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


async def fetch(bvid):
    """拿最低带宽直链并下载。返回 (本地文件, 标题, 时长, 字节数)。"""
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
    if vids:
        vids = sorted(vids, key=lambda x: x.get("bandwidth") or 0)
        url = vids[0].get("baseUrl") or vids[0].get("base_url")
    else:
        durl = data.get("durl") or []
        if not durl:
            raise RuntimeError("no playable stream")
        url = durl[0].get("url")

    hdr = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"),
           "Referer": "https://www.bilibili.com/video/" + bvid}
    os.makedirs(FRAMEDIR, exist_ok=True)
    async with aiohttp.ClientSession(headers=hdr) as sess:
        async with sess.get(url, timeout=aiohttp.ClientTimeout(total=600)) as r:
            if r.status != 200:
                raise RuntimeError("stream http %d" % r.status)
            body = await r.read()
    fd, local = tempfile.mkstemp(prefix="_src_", suffix=".m4s", dir=FRAMEDIR)
    with os.fdopen(fd, "wb") as fh:
        fh.write(body)
    return local, title, duration, len(body)


def extract(src, outdir, interval, max_frames, t_from=0, t_to=0):
    ff = find_ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg not found")
    os.makedirs(outdir, exist_ok=True)
    for f in os.listdir(outdir):
        try:
            os.remove(os.path.join(outdir, f))
        except Exception:
            pass

    vf = "fps=1/%d,scale=%d:-2,format=yuvj420p" % (max(1, interval), TILE_W)
    cmd = [ff, "-hide_banner", "-loglevel", "error"]
    if t_from and t_from > 0:
        cmd += ["-ss", str(t_from)]
    if t_to and t_to > t_from >= 0:
        cmd += ["-t", str(t_to - t_from)]
    cmd += ["-i", src, "-vf", vf, "-vsync", "vfr",
            "-frames:v", str(max_frames), "-q:v", "3", "-y",
            os.path.join(outdir, "f_%03d.jpg")]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg: " + (p.stderr or "")[:250])

    return [os.path.join(outdir, f) for f in sorted(os.listdir(outdir))
            if f.startswith("f_") and f.endswith(".jpg")]


def make_sheets(frames, grid=GRID):
    """把帧拼成网格图。九帧一张，读起来像连环画。"""
    try:
        from PIL import Image
    except Exception:
        return []
    if not frames:
        return []
    out = []
    per = grid * grid
    for si in range(0, len(frames), per):
        chunk = frames[si:si + per]
        imgs = []
        for f in chunk:
            try:
                imgs.append(Image.open(f).convert("RGB"))
            except Exception:
                continue
        if not imgs:
            continue
        w = max(i.width for i in imgs)
        h = max(i.height for i in imgs)
        sheet = Image.new("RGB", (w * grid, h * grid), (12, 12, 14))
        for i, im in enumerate(imgs):
            c, r = i % grid, i // grid
            sheet.paste(im, (c * w, r * h))
        # 文件名带上帧区间，方便对着时间点找
        a = si + 1
        b = si + len(chunk)
        path = os.path.join(os.path.dirname(frames[0]),
                            "sheet_%02d_%03d-%03d.jpg" % (si // per + 1, a, b))
        sheet.save(path, quality=88, optimize=True)
        out.append(path)
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(json.dumps({"ok": False, "error": "usage: bili_watch.py <bvid>"},
                         ensure_ascii=False))
        return 1
    bvid = args[0].strip()

    every = 0
    max_frames = MAX_FRAMES
    t_from = 0
    t_to = 0
    for a in sys.argv[1:]:
        if a.startswith("--every="):
            try:
                every = int(a.split("=", 1)[1])
            except Exception:
                pass
        elif a.startswith("--max="):
            try:
                max_frames = max(1, min(60, int(a.split("=", 1)[1])))
            except Exception:
                pass
        elif a.startswith("--from="):
            try:
                t_from = int(a.split("=", 1)[1])
            except Exception:
                pass
        elif a.startswith("--to="):
            try:
                t_to = int(a.split("=", 1)[1])
            except Exception:
                pass

    result = {"ok": False, "bvid": bvid}
    src = None
    try:
        src, title, duration, size = asyncio.run(fetch(bvid))
        result.update({"title": title, "duration": duration,
                       "download_mb": round(size / 1048576.0, 2)})
        step = every if every > 0 else pick_interval(duration, max_frames)
        result["interval_sec"] = step
        if t_from or t_to:
            result["window"] = [t_from, t_to]

        outdir = os.path.join(FRAMEDIR, bvid)
        frames = extract(src, outdir, step, max_frames, t_from, t_to)
        sheets = make_sheets(frames)

        result["ok"] = bool(frames)
        result["count"] = len(frames)
        result["sheets"] = sheets
        result["frames"] = frames
    except Exception as e:
        result["error"] = str(e)[:300]
    finally:
        if src and os.path.isfile(src):
            try:
                os.remove(src)
            except OSError:
                pass

    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
