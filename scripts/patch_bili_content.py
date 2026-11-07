"""Generate an incremental MCP candidate from an exact reviewed original.

Never starts/imports the installed server, loads credentials, or installs files.
The three candidate files are new exclusive outputs for separate maintenance.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path

from patch_onebot_policy import PatchError, ordinary_path, read_regular, replace_once

KNOWN = {"8fad6e5f976232a56861c7921e8dc112d0449528ee3fac72007eca304f077a77"}
MARKER = "# LANYA_BILI_CONTENT_V1\n"
ROOT = Path(__file__).resolve().parent


def transform(raw, *, account_config):
    if hashlib.sha256(raw).hexdigest() not in KNOWN:
        raise PatchError("Unknown reviewed Bilibili MCP source")
    account = ordinary_path(account_config)
    text = raw.decode("utf-8").replace("\r\n", "\n")
    if MARKER.strip() in text:
        raise PatchError("Bilibili content layer already present")
    tree = ast.parse(text)
    targets = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "bili_reply"]
    if len(targets) != 1:
        raise PatchError("Unknown comment function")
    node = targets[0]
    start = min([node.lineno] + [decorator.lineno for decorator in node.decorator_list])
    lines = text.splitlines(keepends=True)
    replacement = '''@tool()
async def bili_reply(bvid: str, text: str, rpid: str = "0", root: str = "0", mention_admin: bool = False) -> str:
    """Submit one real video comment/reply; IDs are decimal strings.

    rpid identifies the actual target comment; root is its actual thread root.
    mention_admin uses only the host's trusted administrator account mapping.
    Unknown delivery is not permission to retry; accepted is not public visibility.
    """
    return json.dumps(await _lanya_social.reply(bvid, text, rpid, root, mention_admin), ensure_ascii=False)
'''
    text = "".join(lines[:start - 1]) + replacement + "".join(lines[node.end_lineno:])
    text = replace_once(text, "async def bili_frames(bvid: str, num: int = 3, at: list | None = None) -> list:",
                        "async def bili_frames(bvid: str, num: int = 3, at: list | None = None, page: int = 1) -> list:")
    text = replace_once(text, '''    pages = info.get("pages") or []
    cid = info.get("cid") or ((pages or [{}])[0].get("cid") or 0)
    if not cid:
        return _text_only({"bvid": bvid, "ok": False, "error": "拿不到 cid"})

    # 故事板只覆盖第 1P：目标时间点按"实际覆盖的长度"算，别落到没采样的 P2 上
    covered = float(pages[0].get("duration") or duration) if pages else duration
    covered = covered or duration''', '''    pages = info.get("pages") or []
    if type(page) is not int or not 1 <= page <= len(pages):
        return _text_only({"bvid": bvid, "ok": False, "error": "目标分P不存在"})
    selected_page = pages[page - 1]
    cid = selected_page.get("cid")
    if type(cid) is not int or cid <= 0:
        return _text_only({"bvid": bvid, "ok": False, "error": "目标分P的cid不可用"})
    covered = float(selected_page.get("duration") or 0)
    if covered <= 0:
        return _text_only({"bvid": bvid, "ok": False, "error": "目标分P的时长不可用"})''')
    text = replace_once(text, '''            "pages": max(1, len(pages)),
            "tile":''', '''            "pages": len(pages), "page": page, "cid": str(cid), "source": "storyboard",
            "tile":''')
    text = replace_once(text, '''                    "多P 视频只覆盖第 1P，看 covered_sec",''',
                        '''                    "仅覆盖所选分P的实际故事板范围，看 page/cid/covered_sec；非播放或看完证明",''')
    text = text.replace('**多P 视频的故事板通常只覆盖第 1P**，返回里会用', '只读所选分P，无法取得其故事板时明确失败；返回里会用', 1)
    text = text.replace('        bvid: 视频BV号，如"BV1uNk1YxEJQ"\n        num: 取几帧，1-6，默认 3',
                        '        bvid: 视频BV号\n        page: 真实分P页号，从1开始；不回落到其他分P\n        num: 取几帧，1-6，默认 3', 1)
    hook = '''from bili_social_backend import BiliSocialBackend
from bili_video_content import observe_video as _lanya_observe_video
_lanya_social = BiliSocialBackend(globals(), account_config=ACCOUNT_CONFIG)


async def _lanya_subtitle_json(url):
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as client:
        async with client.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError("Subtitle HTTP failure")
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > 2 * 1024 * 1024:
                    raise ValueError("Subtitle response exceeds limit")
                chunks.append(chunk)
            return json.loads(b"".join(chunks).decode("utf-8"))


@tool()
async def bili_observe_video(bvid: str, page: int = 1, start_sec: float | None = None,
                             end_sec: float | None = None, max_segments: int = 80, max_chars: int = 12000) -> str:
    """Read selected-page subtitle timeline and real video metadata; not playback."""
    value = await _lanya_observe_video(bvid, page=page, start_sec=start_sec, end_sec=end_sec,
        max_segments=max_segments, max_chars=max_chars,
        video_factory=lambda bv: video.Video(bvid=bv, credential=get_cred()), fetch_subtitle=_lanya_subtitle_json)
    return json.dumps(value, ensure_ascii=False)


@tool()
async def bili_social_inbox(limit: int = 20) -> str:
    """Read actual reply/@ notifications and thread pointers; never private messages."""
    return json.dumps(await _lanya_social.inbox(limit), ensure_ascii=False)


@tool()
async def bili_thread(bvid: str, root: str, num: int = 20) -> str:
    """Read one actual same-video root and child comments before a threaded reply."""
    return json.dumps(await _lanya_social.thread(bvid, root, num), ensure_ascii=False)


'''.replace("ACCOUNT_CONFIG", json.dumps(str(account), ensure_ascii=True))
    text = replace_once(text, 'if __name__ == "__main__":\n    mcp.run(transport="stdio")', hook + 'if __name__ == "__main__":\n    mcp.run(transport="stdio")')
    output = (MARKER + text).encode("utf-8")
    compile(output, "mcp-candidate.py", "exec")
    return output


def candidates(source, *, account_config):
    outputs = {"mcp_server.py": transform(source, account_config=account_config),
               "bili_social_backend.py": read_regular(ROOT / "bili_social_backend.py")[0],
               "bili_video_content.py": read_regular(ROOT / "bili_video_content.py")[0]}
    for name, content in outputs.items():
        compile(content, name, "exec")
    return outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--account-config", required=True)
    parser.add_argument("--output-dir")
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()
    source = ordinary_path(args.input)
    raw, signature = read_regular(source)
    outputs = candidates(raw, account_config=args.account_config)
    before = hashlib.sha256(raw).hexdigest()
    if args.output_dir:
        if args.expected_sha256 != before:
            raise PatchError("Exact reviewed input SHA required")
        destination = ordinary_path(args.output_dir)
        if destination == source.parent or source.parent in destination.parents:
            raise PatchError("Installed source directory cannot receive candidates")
        if read_regular(source) != (raw, signature):
            raise PatchError("Reviewed source changed")
        destination.mkdir(exist_ok=False)
        for name, content in outputs.items():
            with open(destination / name, "xb") as stream:
                stream.write(content)
    print(json.dumps({"input_sha256": before, "outputs": {name: hashlib.sha256(content).hexdigest() for name, content in outputs.items()},
                      "installation_writes": 0, "candidate_files": len(outputs)}))


if __name__ == "__main__":
    main()
