"""Bounded video metadata and timestamped subtitles from injected read primitives.

No credentials, filesystem, HTTP client, playback or model lives in this module.
The caller supplies a video factory and a bounded subtitle-JSON fetcher.
"""
import inspect
import math
import re
from urllib.parse import urlsplit

SCHEMA = "dsh-bili-video-content/v1"
MAX_PAGES = 1000
MAX_SOURCE_SEGMENTS = 20000


def _number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _positive_integer(value):
    return type(value) is int and 0 < value <= 9007199254740991


def _text(value, limit):
    return value[:limit] if isinstance(value, str) else ""


async def _result(value):
    return await value if inspect.isawaitable(value) else value


def _subtitle_url(value):
    if not isinstance(value, str) or not value or len(value) > 8192 or re.search(r"[\x00-\x20\x7f]", value):
        return None
    value = "https:" + value if value.startswith("//") else value
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        valid_host = any(host == root or host.endswith("." + root) for root in ("hdslb.com", "bilibili.com"))
        if parsed.scheme != "https" or not valid_host or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None, 443):
            return None
    except ValueError:
        return None
    return value


def _unavailable(bvid, page, code):
    return {"schema": SCHEMA, "ok": False, "status": "unavailable", "bvid": bvid,
            "page": page, "cid": None, "error_code": code, "pages": [],
            "subtitle": {"status": "unavailable", "language": None, "segments": [],
                         "truncated": False, "range": {"start_sec": None, "end_sec": None}},
            "evidence": {"kind": "metadata", "playback_verified": False, "completion_verified": False},
            "frames_next": None}


async def observe_video(bvid, *, page=1, start_sec=None, end_sec=None,
                        max_segments=80, max_chars=12000,
                        video_factory, fetch_subtitle):
    """Read one selected part; return subtitles as untrusted source material.

    video_factory(bvid) returns a Video-like object exposing get_info,
    optional get_pages, and get_subtitle(cid=...). fetch_subtitle(https_url)
    returns decoded JSON and must enforce its own response-size/timeout bounds.
    Neither callback may start a model or claim playback.
    """
    if (not isinstance(bvid, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid)
            or type(page) is not int or not 1 <= page <= MAX_PAGES
            or type(max_segments) is not int or not 1 <= max_segments <= 80
            or type(max_chars) is not int or not 1 <= max_chars <= 12000
            or start_sec is not None and not _number(start_sec)
            or end_sec is not None and not _number(end_sec)
            or end_sec is not None and end_sec <= (start_sec or 0)
            or not callable(video_factory) or not callable(fetch_subtitle)):
        raise ValueError("Invalid bounded video observation arguments")
    try:
        video = await _result(video_factory(bvid))
        info = await _result(video.get_info())
        if not isinstance(info, dict) or info.get("bvid", bvid) != bvid:
            return _unavailable(bvid, page, "video_identity_mismatch")
        raw_pages = info.get("pages")
        if raw_pages is None and callable(getattr(video, "get_pages", None)):
            raw_pages = await _result(video.get_pages())
        if not isinstance(raw_pages, list) or not 1 <= len(raw_pages) <= MAX_PAGES:
            return _unavailable(bvid, page, "part_metadata_unavailable")
        pages = []
        for index, part in enumerate(raw_pages, 1):
            if (not isinstance(part, dict) or not _positive_integer(part.get("cid"))
                    or not _number(part.get("duration")) or part["duration"] <= 0
                    or type(part.get("page", index)) is not int or part.get("page", index) != index):
                return _unavailable(bvid, page, "invalid_part_metadata")
            pages.append({"page": index, "cid": part["cid"], "part": _text(part.get("part"), 180),
                          "duration": part["duration"]})
        if page > len(pages):
            return _unavailable(bvid, page, "page_out_of_range")
    except Exception:
        return _unavailable(bvid, page, "video_read_failed")
    selected = pages[page - 1]
    duration, cid = selected["duration"], selected["cid"]
    start = start_sec if start_sec is not None else 0
    if start >= duration:
        return _unavailable(bvid, page, "range_outside_part")
    end = min(end_sec, duration) if end_sec is not None else duration
    clipped = end_sec is not None and end_sec > duration
    subtitle = {"status": "unavailable", "language": None, "segments": [], "truncated": False,
                "range": {"start_sec": None, "end_sec": None}, "source_segment_count": 0,
                "matching_segment_count": 0, "skipped_invalid_count": 0}
    result = {"schema": SCHEMA, "ok": True, "status": "partial", "bvid": bvid,
              "page": page, "cid": cid, "title": _text(info.get("title"), 300),
              "duration": duration, "page_count": len(pages), "pages": pages,
              "requested_range": {"start_sec": start, "end_sec": end_sec},
              "effective_range": {"start_sec": start, "end_sec": end, "clipped_to_part": clipped},
              "subtitle": subtitle,
              "evidence": {"kind": "metadata", "playback_verified": False, "completion_verified": False},
              "frames_next": {"tool": "bili_frames", "arguments": {"bvid": bvid, "page": page,
                              "at": [round(start + (end - start) * fraction, 3) for fraction in (0.25, 0.5, 0.75)]},
                              "evidence_kind": "storyboard_stills", "execution": "not_requested",
                              "image_inspection_verified": False}}
    if _positive_integer(info.get("aid")):
        result["aid"] = info["aid"]
    try:
        listing = await _result(video.get_subtitle(cid=cid))
        if not isinstance(listing, dict) or listing.get("cid", cid) != cid:
            subtitle["reason_code"] = "subtitle_identity_mismatch"
            return result
        choices = listing.get("subtitles")
        if not isinstance(choices, list) or len(choices) > 256:
            subtitle["reason_code"] = "subtitle_list_unavailable"
            return result
        candidates = [(choice, _subtitle_url(choice.get("subtitle_url")))
                      for choice in choices if isinstance(choice, dict)]
        candidates = [(choice, url) for choice, url in candidates if url]
        if not candidates:
            subtitle["reason_code"] = "no_usable_subtitle"
            return result
        priority = {"ai-zh": 0, "zh-CN": 1, "zh-Hans": 2, "zh": 3}
        choice, url = min(candidates, key=lambda pair: priority.get(pair[0].get("lan"), 10)
                          if isinstance(pair[0].get("lan"), str) else 10)
        body = await _result(fetch_subtitle(url))
        rows = body.get("body") if isinstance(body, dict) else None
        if not isinstance(rows, list) or len(rows) > MAX_SOURCE_SEGMENTS:
            subtitle.update(status="failed", reason_code="invalid_subtitle_body")
            return result
        subtitle["source_segment_count"] = len(rows)
        language = choice.get("lan")
        subtitle["language"] = language if isinstance(language, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", language) else None
        matches = []
        for row in rows:
            if (not isinstance(row, dict) or not _number(row.get("from")) or not _number(row.get("to"))
                    or row["to"] <= row["from"] or row["to"] > duration
                    or not isinstance(row.get("content"), str) or not row["content"].strip()):
                subtitle["skipped_invalid_count"] += 1
                continue
            if row["to"] > start and row["from"] < end:
                matches.append({"from": row["from"], "to": row["to"], "text": row["content"]})
        matches.sort(key=lambda row: (row["from"], row["to"]))
        subtitle["matching_segment_count"] = len(matches)
        used = 0
        next_row = None
        for row in matches:
            if len(subtitle["segments"]) >= max_segments or used >= max_chars:
                subtitle["truncated"] = True
                next_row = row
                break
            text = row["text"][:max_chars - used]
            truncated = len(text) < len(row["text"])
            subtitle["segments"].append({**row, "text": text, "text_truncated": truncated})
            used += len(text)
            subtitle["truncated"] |= truncated
            if truncated:
                next_row = row
                break
        if subtitle["segments"]:
            subtitle["status"] = "available"
            subtitle["range"] = {"start_sec": min(row["from"] for row in subtitle["segments"]),
                                 "end_sec": max(row["to"] for row in subtitle["segments"])}
            result["evidence"]["kind"] = "subtitles"
            result["status"] = "partial" if clipped or subtitle["truncated"] or subtitle["skipped_invalid_count"] else "observed"
            if next_row is not None:
                # Anchor at the first incomplete caption. Overlapping captions
                # may repeat and a dense interval can require narrower bounds.
                resume = max(start, next_row["from"])
                subtitle["resume"] = {"start_sec": resume, "end_sec": end,
                                      "advances": resume > start}
        else:
            subtitle["reason_code"] = "no_segments_in_requested_range"
    except Exception:
        subtitle.update(status="failed", segments=[], truncated=False,
                        range={"start_sec": None, "end_sec": None}, reason_code="subtitle_read_failed")
    return result
