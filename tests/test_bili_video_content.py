"""Injected video/JSON fixtures only; no credentials, network, player or model."""
import asyncio
import copy
import importlib.util
from pathlib import Path
import unittest


PATH = Path(__file__).resolve().parents[1] / "scripts/bili_video_content.py"
spec = importlib.util.spec_from_file_location("bili_content_fixture", PATH)
content = importlib.util.module_from_spec(spec)
spec.loader.exec_module(content)
BV = "BV1xx411c7mD"
URL = "https://aisubtitle.hdslb.com/fixture.json"


class Video:
    def __init__(self):
        self.info = {"bvid": BV, "aid": 123, "title": "A fixture video", "duration": 300,
                     "pages": [{"page": 1, "cid": 101, "part": "Part one", "duration": 100},
                               {"page": 2, "cid": 202, "part": "Part two", "duration": 200}]}
        self.subtitle = {"subtitles": [{"lan": "ai-zh", "subtitle_url": URL}]}
        self.cids = []

    async def get_info(self):
        return copy.deepcopy(self.info)

    async def get_pages(self):
        return [{"page": 1, "cid": 303, "part": "Fallback pages", "duration": 40}]

    async def get_subtitle(self, *, cid):
        self.cids.append(cid)
        return copy.deepcopy(self.subtitle)


class VideoContentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.video = Video()
        self.requests = []
        self.body = {"body": [{"from": 0, "to": 5, "content": "First caption"},
                              {"from": 5, "to": 10, "content": "Second caption"}]}

    async def fetch(self, url):
        self.requests.append(url)
        return copy.deepcopy(self.body)

    async def observe(self, **kwargs):
        return await content.observe_video(BV, video_factory=lambda bvid: self.video,
                                           fetch_subtitle=self.fetch, **kwargs)

    async def test_selected_part_cid_and_timestamp_text_survive_without_playback_claim(self):
        result = await self.observe(page=2)
        self.assertEqual(self.video.cids, [202])
        self.assertEqual((result["page"], result["cid"], result["duration"]), (2, 202, 200))
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["subtitle"]["segments"][1],
                         {"from": 5, "to": 10, "text": "Second caption", "text_truncated": False})
        self.assertEqual(result["subtitle"]["range"], {"start_sec": 0, "end_sec": 10})
        self.assertEqual(result["evidence"], {"kind": "subtitles", "playback_verified": False, "completion_verified": False})
        self.assertEqual(result["frames_next"]["arguments"]["page"], 2)
        self.assertEqual(result["frames_next"]["execution"], "not_requested")
        self.assertFalse(result["frames_next"]["image_inspection_verified"])

    async def test_range_keeps_original_overlapping_caption_times(self):
        result = await self.observe(start_sec=3, end_sec=7)
        self.assertEqual(result["effective_range"], {"start_sec": 3, "end_sec": 7, "clipped_to_part": False})
        self.assertEqual(result["subtitle"]["range"], {"start_sec": 0, "end_sec": 10})
        self.assertEqual(len(result["subtitle"]["segments"]), 2)

    async def test_segment_and_character_limits_are_real_and_explicit(self):
        by_count = await self.observe(max_segments=1)
        self.assertEqual(len(by_count["subtitle"]["segments"]), 1)
        self.assertTrue(by_count["subtitle"]["truncated"])
        self.assertEqual(by_count["status"], "partial")
        by_chars = await self.observe(max_chars=4)
        self.assertEqual(sum(len(row["text"]) for row in by_chars["subtitle"]["segments"]), 4)
        self.assertTrue(by_chars["subtitle"]["segments"][0]["text_truncated"])
        self.assertTrue(by_chars["subtitle"]["truncated"])

    async def test_no_subtitle_still_returns_real_selected_details_and_frame_route(self):
        self.video.subtitle = {"subtitles": []}
        result = await self.observe(page=2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["title"], "A fixture video")
        self.assertEqual(result["subtitle"]["status"], "unavailable")
        self.assertEqual(result["evidence"]["kind"], "metadata")
        self.assertEqual(result["frames_next"]["arguments"], {"bvid": BV, "page": 2, "at": [50.0, 100.0, 150.0]})
        self.assertEqual(self.requests, [])

    async def test_get_pages_fallback_is_explicit_not_assuming_first_info_cid(self):
        self.video.info.pop("pages")
        self.video.info["cid"] = 999
        result = await self.observe()
        self.assertEqual((result["cid"], self.video.cids), (303, [303]))

    async def test_nonexistent_page_does_not_fall_back_to_first(self):
        result = await self.observe(page=3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "page_out_of_range")
        self.assertEqual(self.video.cids, [])
        self.assertEqual(self.requests, [])

    async def test_other_video_or_other_subtitle_cid_is_rejected(self):
        self.video.info["bvid"] = "BV2xx411c7mD"
        result = await self.observe()
        self.assertEqual(result["error_code"], "video_identity_mismatch")
        self.video.info["bvid"] = BV
        self.video.subtitle["cid"] = 999
        result = await self.observe(page=2)
        self.assertEqual(result["subtitle"]["reason_code"], "subtitle_identity_mismatch")
        self.assertEqual(self.requests, [])

    async def test_invalid_times_and_out_of_part_segments_are_never_evidence(self):
        self.body["body"] += [{"from": True, "to": 2, "content": "Boolean time"},
                              {"from": 99, "to": 101, "content": "Outside this part"},
                              {"from": float("nan"), "to": 20, "content": "Invalid time"}]
        result = await self.observe()
        self.assertEqual(len(result["subtitle"]["segments"]), 2)
        self.assertEqual(result["subtitle"]["skipped_invalid_count"], 3)
        self.assertEqual(result["status"], "partial")

    async def test_window_outside_part_fails_and_long_window_reports_clipping(self):
        outside = await self.observe(page=1, start_sec=150)
        self.assertEqual(outside["error_code"], "range_outside_part")
        self.assertEqual(self.requests, [])
        clipped = await self.observe(page=1, end_sec=150)
        self.assertEqual(clipped["effective_range"]["end_sec"], 100)
        self.assertTrue(clipped["effective_range"]["clipped_to_part"])
        self.assertEqual(clipped["status"], "partial")

    async def test_subtitle_url_is_official_https_and_never_exposed(self):
        for url in ("http://aisubtitle.hdslb.com/x", "https://127.0.0.1/x", "https://hdslb.com.attacker.test/x",
                    "https://fixture-user" + "@" + "aisubtitle.hdslb.com/x", "https://aisubtitle.hdslb.com:444/x"):
            with self.subTest(url=url):
                self.video.subtitle["subtitles"][0]["subtitle_url"] = url
                result = await self.observe()
                self.assertEqual(result["subtitle"]["reason_code"], "no_usable_subtitle")
        self.assertEqual(self.requests, [])
        self.video.subtitle["subtitles"][0]["subtitle_url"] = "//aisubtitle.hdslb.com/fixture.json?fixture_signature=secret"
        result = await self.observe()
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("fixture_signature", repr(result))

    async def test_subtitle_failure_keeps_metadata_and_does_not_leak_exception(self):
        async def fail(url):
            raise RuntimeError("private signed request must not escape")
        result = await content.observe_video(BV, video_factory=lambda bvid: self.video, fetch_subtitle=fail)
        self.assertTrue(result["ok"])
        self.assertEqual(result["subtitle"]["status"], "failed")
        self.assertEqual(result["subtitle"]["reason_code"], "subtitle_read_failed")
        self.assertNotIn("private signed", repr(result))

    async def test_bad_arguments_fail_before_any_primitive(self):
        calls = []
        for kwargs in ({"page": True}, {"page": 0}, {"start_sec": -1}, {"end_sec": float("inf")},
                       {"start_sec": 10, "end_sec": 5}, {"max_segments": 81}, {"max_chars": 12001},
                       {"max_chars": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await content.observe_video(BV, video_factory=lambda bvid: calls.append(bvid),
                                            fetch_subtitle=self.fetch, **kwargs)
        self.assertEqual(calls, [])

    async def test_invalid_body_or_pages_never_leave_unverified_subtitle_text(self):
        self.body = {"body": "not a segment list"}
        result = await self.observe()
        self.assertEqual(result["subtitle"]["status"], "failed")
        self.assertEqual(result["subtitle"]["segments"], [])
        self.video.info["pages"][1]["cid"] = 0
        result = await self.observe()
        self.assertEqual(result["error_code"], "invalid_part_metadata")

    async def test_boolean_part_index_and_extreme_numeric_arguments_are_invalid(self):
        self.video.info["pages"][0]["page"] = True
        result = await self.observe()
        self.assertEqual(result["error_code"], "invalid_part_metadata")
        with self.assertRaises(ValueError):
            await self.observe(start_sec=10 ** 1000)

    async def test_subtitle_url_controls_never_reach_the_injected_fetcher(self):
        self.video.subtitle["subtitles"][0]["subtitle_url"] = "https://aisubtitle.hdslb.com/fixture\n.json"
        result = await self.observe()
        self.assertEqual(result["subtitle"]["reason_code"], "no_usable_subtitle")
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
