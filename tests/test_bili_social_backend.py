"""Isolated actual backend code with inert library seams; no platform/model."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from bili_social_backend import BiliSocialBackend, normalize_notification

BVID = "BV1234567890"


class SocialBackendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "account.json"
        self.config.write_text('{"adminBiliUid":"707"}', encoding="utf-8")
        self.forms, self.writes = [], []
        self.result = {"rpid": 9001, "rpid_str": "9001"}
        self.info = {"bvid": BVID, "aid": 123}
        self.thread_value = {"root": self.row(101), "replies": [self.row(102, 101, 101)], "page": {"count": 1}}
        test = self
        class Api:
            def __init__(self, **kwargs):
                pass
            def update_data(self, **form):
                test.forms.append(form)
                return self
            @property
            async def result(self):
                test.writes.append(1)
                if isinstance(test.result, BaseException):
                    raise test.result
                return test.result
        video = types.SimpleNamespace(Video=lambda **kw: types.SimpleNamespace(get_info=mock.AsyncMock(side_effect=lambda: self.info)))
        comment = types.SimpleNamespace(Comment=lambda **kw: types.SimpleNamespace(get_sub_comments=mock.AsyncMock(side_effect=lambda **args: self.thread_value)),
            CommentResourceType=types.SimpleNamespace(VIDEO=1), Api=Api, API={"comment": {"send": {}}})
        self.ns = {"get_cred": lambda: object(), "video": video, "comment": comment,
                   "session": types.SimpleNamespace(get_replies=mock.AsyncMock(return_value={"items": []}), get_at=mock.AsyncMock(return_value={"items": []})),
                   "user": types.SimpleNamespace(User=lambda **kw: types.SimpleNamespace(get_user_info=mock.AsyncMock(return_value={"mid": 707, "name": "synthetic-admin"}))),
                   "_guard": lambda name: "", "_style_reason": mock.AsyncMock(return_value=""), "_ledger_append": mock.Mock()}
        self.backend = BiliSocialBackend(self.ns, account_config=self.config)

    def row(self, rpid, root=0, parent=0):
        return {"oid": 123, "rpid": rpid, "root": root, "parent": parent,
                "member": {"mid": 808, "uname": "fixture-author"}, "content": {"message": "actual fixture comment"}, "ctime": 1}

    def run_async(self, operation):
        return asyncio.run(operation)

    def test_thread_keeps_exact_video_root_parent_and_large_decimal_ids(self):
        value = self.run_async(self.backend.thread(BVID, "101"))
        self.assertEqual(value["aid"], "123")
        self.assertEqual(value["comments"][1]["rpid"], "102")
        self.assertEqual(value["comments"][1]["parent"], "101")
        self.assertFalse(value["truncated"])
        self.thread_value["root"]["oid"] = 999
        with self.assertRaises(ValueError):
            self.run_async(self.backend.thread(BVID, "101"))

    def test_thread_rejects_cross_video_children_and_wrong_root(self):
        for key, bad in (("oid", 999), ("root", 777)):
            with self.subTest(key=key):
                self.thread_value["replies"] = [self.row(102, 101, 101)]
                self.thread_value["replies"][0][key] = bad
                with self.assertRaises(ValueError):
                    self.run_async(self.backend.thread(BVID, "101"))

    def test_comment_and_actual_reply_submit_exact_root_parent(self):
        value = self.run_async(self.backend.reply(BVID, "fixture detail"))
        self.assertEqual(value["status"], "accepted")
        self.assertEqual(value["public_visibility"], "unverified")
        self.assertNotIn("root", self.forms[-1])
        value = self.run_async(self.backend.reply(BVID, "fixture reply", "102", "101"))
        self.assertEqual((self.forms[-1]["root"], self.forms[-1]["parent"]), (101, 102))
        self.assertEqual(value["root"], "101")

    def test_unknown_target_or_cross_video_never_reaches_submission(self):
        value = self.run_async(self.backend.reply(BVID, "fixture", "999", "101"))
        self.assertEqual(value["status"], "failed")
        self.assertEqual(self.writes, [])
        self.thread_value["root"]["oid"] = 456
        self.assertEqual(self.run_async(self.backend.reply(BVID, "fixture", "101"))["status"], "failed")
        self.assertEqual(self.writes, [])

    def test_mention_uses_only_trusted_uid_and_fresh_verified_name_mapping(self):
        value = self.run_async(self.backend.reply(BVID, "fixture detail", mention_admin=True))
        self.assertEqual(value["mention"], {"requested": True, "applied": True})
        self.assertEqual(json.loads(self.forms[0]["at_name_to_mid"]), {"synthetic-admin": 707})
        self.assertIn("@synthetic-admin ", self.forms[0]["message"])
        self.assertNotIn("at_name_to_mid", self.run_no_mention_form())

    def run_no_mention_form(self):
        self.run_async(self.backend.reply(BVID, "fixture detail"))
        return self.forms[-1]

    def test_missing_or_changed_account_does_not_fabricate_mention(self):
        self.config.write_text('{}', encoding="utf-8")
        self.assertEqual(self.run_async(self.backend.reply(BVID, "fixture", mention_admin=True))["status"], "failed")
        self.assertEqual(self.writes, [])
        self.config.write_text('{"adminBiliUid":"707"}', encoding="utf-8")
        self.ns["user"].User = lambda **kw: types.SimpleNamespace(get_user_info=mock.AsyncMock(return_value={"mid": 909, "name": "wrong"}))
        self.assertEqual(self.run_async(self.backend.reply(BVID, "fixture", mention_admin=True))["status"], "failed")
        self.assertEqual(self.writes, [])

    def test_style_and_original_whitelist_refusal_remain_preflight(self):
        self.ns["_style_reason"].return_value = "fixture style refusal"
        self.assertEqual(self.run_async(self.backend.reply(BVID, "fixture"))["reason"], "style_rejected")
        self.ns["_guard"] = lambda _: "disabled"
        self.assertEqual(self.run_async(self.backend.reply(BVID, "fixture"))["reason"], "tool_disabled")
        self.assertEqual(self.writes, [])

    def test_post_submission_errors_missing_and_conflicting_receipts_are_unknown(self):
        for result in (TimeoutError("fixture"), asyncio.CancelledError(), {}, {"rpid": 9001, "rpid_str": "9002"}):
            with self.subTest(result=type(result).__name__):
                self.result = result
                self.assertEqual(self.run_async(self.backend.reply(BVID, "fixture"))["status"], "unknown")
        self.assertEqual(len(self.writes), 4)
        self.ns["_ledger_append"].assert_not_called()

    def test_notification_source_is_reply_target_not_old_target_or_video_title(self):
        row = normalize_notification({"id": 44, "user": {"mid": 808}, "item": {
            "title": "video title", "source_content": "real reply", "subject_id": 123,
            "source_id": 102, "target_id": 101, "root_id": 101, "business_id": 1,
            "type": "reply", "uri": "https://www.bilibili.com/video/" + BVID}}, "reply")
        self.assertEqual((row["rpid"], row["parent"], row["target_id"], row["text"]), ("102", "102", "101", "real reply"))
        self.assertTrue(row["replyable"])
        row = normalize_notification({"id": 45, "item": {"title": "a dynamic", "business_id": 11,
            "subject_id": 999, "source_id": 102, "uri": "https://www.bilibili.com/video/" + BVID}}, "at")
        self.assertEqual(row["resource_type"], "unknown")
        self.assertFalse(row["replyable"])

    def test_inbox_returns_partial_errors_and_never_private_messages(self):
        self.ns["session"].get_replies.side_effect = TimeoutError()
        value = self.run_async(self.backend.inbox())
        self.assertFalse(value["ok"])
        self.assertTrue(value["partial"])
        self.assertEqual(value["errors"], [{"kind": "reply", "error_class": "TimeoutError"}])
        self.assertEqual(self.writes, [])

    def test_inbox_same_page_duplicates_and_incomplete_cursor_are_explicit(self):
        row = {"id": 44, "user": {"mid": 808}, "item": {"title": "fixture title", "source_content": "fixture reply", "source_id": 102}}
        self.ns["session"].get_replies.return_value = {"cursor": {"is_end": False}, "items": [row, row]}
        value = self.run_async(self.backend.inbox())
        self.assertEqual(len(value["items"]), 1)
        self.assertTrue(value["partial"])
        self.assertTrue(value["notifications_read_only"])
        self.assertEqual(self.writes, [])

    def test_style_history_failure_does_not_erase_a_real_comment_receipt(self):
        self.ns["_ledger_append"].side_effect = OSError("fixture history failure")
        value = self.run_async(self.backend.reply(BVID, "fixture detail"))
        self.assertEqual(value["status"], "accepted")
        self.assertEqual(value["rpid_str"], "9001")

    def test_explicit_video_subject_resolves_av_only_notice_without_guessing_a_bv(self):
        row = {"id": 44, "user": {"mid": 808}, "item": {"title": "fixture", "source_content": "fixture reply",
            "business_id": 1, "type": "reply", "subject_id": 123, "source_id": 102, "root_id": 101,
            "uri": "https://www.bilibili.com/video/av123"}}
        self.ns["session"].get_replies.return_value = {"items": [row]}
        original = self.ns["video"].Video
        with mock.patch.object(self.ns["video"], "Video", side_effect=original) as construct:
            value = self.run_async(self.backend.inbox())
        self.assertEqual(construct.call_count, 1)
        self.assertEqual(construct.call_args.kwargs["aid"], 123)
        self.assertEqual(value["items"][0]["bvid"], BVID)
        self.assertTrue(value["items"][0]["replyable"])
        self.assertEqual(value["items"][0]["video_resolution"], "subject_id_verified")
        self.assertEqual(self.writes, [])

    def test_subject_resolution_failure_and_cross_resource_identity_stay_unknown(self):
        row = {"id": 44, "item": {"title": "fixture", "source_content": "fixture reply",
            "business_id": 1, "type": "reply", "subject_id": 123, "source_id": 102}}
        self.ns["session"].get_replies.return_value = {"items": [row]}
        for info in ({"aid": 999, "bvid": BVID}, {"aid": 123, "bvid": "not-a-bv"}):
            with self.subTest(info=info):
                self.info = info
                value = self.run_async(self.backend.inbox())
                self.assertIsNone(value["items"][0]["bvid"])
                self.assertFalse(value["items"][0]["replyable"])
                self.assertTrue(value["partial"])
        self.ns["video"].Video = mock.Mock(side_effect=TimeoutError())
        self.assertEqual(self.run_async(self.backend.inbox())["items"][0]["video_resolution"], "unknown")
        self.assertEqual(self.writes, [])

    def test_subject_resolution_is_bounded_and_dynamic_or_unknown_is_never_video(self):
        base = {"title": "fixture", "source_content": "fixture reply", "subject_id": 123, "source_id": 102}
        rows = [{"id": 44 + index, "item": {**base, "business_id": business, "type": kind}}
                for index, (business, kind) in enumerate(((11, "reply"), (17, "dynamic"), (None, "reply"), (1, "reply"), (1, "reply")))]
        self.ns["session"].get_replies.return_value = {"items": rows}
        original = self.ns["video"].Video
        with mock.patch.object(self.ns["video"], "Video", side_effect=original) as construct:
            value = self.run_async(self.backend.inbox(limit=3))
            self.assertEqual(construct.call_count, 0)
            self.assertEqual(len(value["items"]), 3)
            value = self.run_async(self.backend.inbox(limit=5))
            self.assertEqual(construct.call_count, 1)  # Same explicit subject resolved once per batch.
            self.assertEqual(sum(row["replyable"] for row in value["items"]), 2)

    def test_mcp_error_content_is_not_a_successful_empty_notification_read(self):
        from bili_mcp import BiliMCP
        client = BiliMCP()
        with mock.patch.object(client, "_rpc", return_value={"isError": True, "content": [{"type": "text", "text": "fixture refusal"}]}):
            with self.assertRaises(RuntimeError):
                client.call_json("bili_received_replies")


if __name__ == "__main__":
    unittest.main()
