"""Offline path/extraction regression checks; no Bilibili or model requests."""
import contextlib
import asyncio
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class BiliPathsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        config = self.root / "project.json"
        config.write_text(json.dumps({"dataDirName": "custom-character"}), encoding="utf-8")
        env = mock.patch.dict(os.environ, {"LANYA_PROJECT_CONFIG": str(config),
                                           "LANYA_WORKDIR": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        paths = mock.patch.object(sys, "path", [str(SCRIPTS)] + sys.path)
        paths.start()
        self.addCleanup(paths.stop)
        import project_conf
        project_conf.load(reload=True)
        self.addCleanup(setattr, project_conf, "_CACHE", None)

    def load(self, name):
        spec = importlib.util.spec_from_file_location(name + "_under_test", SCRIPTS / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_all_bili_data_paths_follow_project_configuration(self):
        watch = self.load("bili_watch")
        onerun = self.load("bili_onerun")
        social = self.load("bili_social")
        data = self.root / "custom-character"
        self.assertEqual(Path(watch.WORK), self.root)
        self.assertEqual(Path(watch.DATA), data)
        self.assertEqual(Path(watch.FRAMEDIR), data / "made" / "_videoframes")
        self.assertEqual(Path(onerun.LANYA_DIR), data)
        self.assertEqual(Path(onerun.CONFIG_FILE), data / "lanya-config.json")
        self.assertEqual(Path(onerun.CHAT_DIR), data / "chats")
        self.assertEqual(Path(social.DATA), data)
        self.assertEqual(Path(social.STATE), self.root / "napcat" / "bili-social-seen.json")

    def test_social_reads_config_and_persona_from_configured_paths(self):
        social = self.load("bili_social")
        data = self.root / "custom-character"
        data.mkdir()
        (data / "lanya-config.json").write_text('{"adminBiliName":"offline-test"}', encoding="utf-8")
        runtime = self.root / "napcat"
        runtime.mkdir()
        persona = runtime / "custom-character-\u4eba\u8bbe\u6b63\u6587.md"
        persona.write_text("\u2705 \u300csynthetic example\u300d", encoding="utf-8")
        self.assertEqual(social.cfg()["adminBiliName"], "offline-test")
        self.assertEqual(Path(social.persona_path()), persona)
        self.assertIn("synthetic example", social.persona_snippet())
        self.assertIn("synthetic example", social.examples())

    def test_credentials_keep_the_shared_bili_duty_location(self):
        watch = self.load("bili_watch")
        home = Path(watch.BILI_HOME)
        self.assertEqual(home, Path(os.path.expanduser("~")) / ".dsh" / "bili-duty")
        fake_home = self.root / "bili-duty"
        fake_home.mkdir()
        (fake_home / "bili_credential.json").write_text(
            json.dumps({"SESSDATA": "test-session", "BILI_JCT": "test-csrf"}), encoding="utf-8")
        credential = mock.Mock(return_value="fake-credential")
        fake_module = types.SimpleNamespace(Credential=credential)
        with mock.patch.object(watch, "BILI_HOME", str(fake_home)), \
                mock.patch.dict(sys.modules, {"bilibili_api": fake_module}):
            self.assertEqual(watch.load_cred(), "fake-credential")
        credential.assert_called_once_with(sessdata="test-session", bili_jct="test-csrf",
                                           buvid3=None, dedeuserid=None)

    def test_clip_ending_at_n_seconds_works_from_zero(self):
        watch = self.load("bili_watch")
        out = self.root / "frames"
        with mock.patch.object(watch, "find_ffmpeg", return_value="fake-ffmpeg"), \
                mock.patch.object(watch.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertEqual(watch.extract("fake-source", str(out), 2, 12, 0, 30), [])
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-t") + 1], "30")
        self.assertEqual(command[command.index("-frames:v") + 1], "12")
        self.assertEqual(run.call_args.kwargs["creationflags"],
                         getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_downloads_have_distinct_temporary_files(self):
        watch = self.load("bili_watch")
        video = mock.Mock()
        video.get_info = mock.AsyncMock(return_value={"title": "offline", "duration": 30})
        video.get_download_url = mock.AsyncMock(return_value={"dash": {"video": [
            {"bandwidth": 1, "baseUrl": "https://offline.invalid/video"}]}})
        response = mock.MagicMock()
        response.status = 200
        response.read = mock.AsyncMock(return_value=b"offline-video")
        response.__aenter__.return_value = response
        session = mock.MagicMock()
        session.get.return_value = response
        session.__aenter__.return_value = session
        aiohttp = types.SimpleNamespace(ClientSession=mock.Mock(return_value=session),
                                        ClientTimeout=mock.Mock())
        bilibili = types.SimpleNamespace(video=types.SimpleNamespace(Video=mock.Mock(return_value=video)))
        with mock.patch.object(watch, "load_cred", return_value=None), \
                mock.patch.dict(sys.modules, {"aiohttp": aiohttp, "bilibili_api": bilibili}):
            first = asyncio.run(watch.fetch("BVfake"))[0]
            second = asyncio.run(watch.fetch("BVfake"))[0]
        self.assertNotEqual(first, second)
        self.assertEqual(Path(first).read_bytes(), b"offline-video")
        self.assertEqual(Path(second).read_bytes(), b"offline-video")

    def test_watch_frames_respects_callers_frame_limit(self):
        onerun = self.load("bili_onerun")
        output = json.dumps({"sheets": ["fake-sheet.jpg"]})
        with mock.patch.object(onerun.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, output, "")) as run:
            self.assertEqual(onerun.watch_frames("BVfake", max_frames=9), (["fake-sheet.jpg"], ""))
        self.assertIn("--max=9", run.call_args.args[0])

    def test_watch_frames_uses_bili_virtual_environment_when_available(self):
        onerun = self.load("bili_onerun")
        with mock.patch.object(onerun.os.path, "isfile", return_value=True), \
                mock.patch.object(onerun.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0, "{}", "")) as run:
            onerun.watch_frames("BVfake")
        self.assertEqual(run.call_args.args[0][0], onerun.VENV_PY)

    def test_failed_extraction_cleans_download_without_network(self):
        watch = self.load("bili_watch")
        source = self.root / "fake-source.m4s"
        source.write_bytes(b"offline test")

        async def fake_fetch(_bvid):
            return str(source), "fake title", 30, 12

        with mock.patch.object(watch, "fetch", side_effect=fake_fetch), \
                mock.patch.object(watch, "extract", side_effect=RuntimeError("fake ffmpeg failure")), \
                mock.patch.object(sys, "argv", ["bili_watch.py", "BVfake"]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(watch.main(), 1)
        self.assertFalse(source.exists())
        result = json.loads(output.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "fake ffmpeg failure")

    def test_comment_verification_requires_an_exact_receipt_before_any_platform_read(self):
        onerun = self.load("bili_onerun")
        with mock.patch.object(onerun, "BiliMCP") as client:
            for rpid in (None, "", "0", "01", "bad", True, 1.5):
                with self.subTest(rpid=rpid):
                    self.assertEqual(onerun.verify_comment("BV1234567890", "fixture", rpid=rpid)["state"], "unknown")
            self.assertEqual(onerun.verify_comment("not-a-video", "fixture", rpid="101")["state"], "unknown")
        client.assert_not_called()

    def test_comment_visibility_uses_the_full_text_and_exact_large_receipt_not_a_prefix_or_nickname(self):
        onerun = self.load("bili_onerun")
        target = "18446744073709551611"
        message = "the same first twenty characters, expected ending"
        client = mock.MagicMock(); client.__enter__.return_value = client
        with mock.patch.object(onerun, "BiliMCP", return_value=client), \
                mock.patch.object(onerun, "_cfg", return_value={"biliUserName": "fixture-self"}):
            client.call_json.return_value = {"comments": [{"rpid": "102", "content": message[:20] + " different", "user": "fixture-self"}]}
            self.assertEqual(onerun.verify_comment("BV1234567890", message, rpid=target)["state"], "unknown")
            client.call_json.return_value = {"comments": [{"rpid": target, "content": message[:20] + " different", "user": "fixture-self"}]}
            self.assertEqual(onerun.verify_comment("BV1234567890", message, rpid=target)["state"], "unknown")
            client.call_json.return_value = {"bvid": "BV1234567890", "comments": [{"rpid_str": target, "content": message, "user": "fixture-self"}]}
            value = onerun.verify_comment("BV1234567890", message, rpid=target)
            self.assertEqual(value["state"], "public")
            self.assertEqual(value["rpid"], target)
            self.assertEqual(value["evidence"], "sampled_public_comments")
            client.call_json.assert_called_with("bili_comments", {"bvid": "BV1234567890", "num": 50})

    def test_comment_visibility_errors_and_unseen_targets_stay_unknown_without_private_error_text(self):
        onerun = self.load("bili_onerun")
        client = mock.MagicMock(); client.__enter__.return_value = client
        with mock.patch.object(onerun, "BiliMCP", return_value=client):
            for value in ({"comments": []}, {"comments": "malformed"}, {"ok": False, "comments": []},
                          {"bvid": "BV0000000000", "comments": [{"rpid": "101", "content": "fixture"}]},
                          {"comments": [{"rpid": "101", "rpid_str": "102", "content": "fixture"}]}):
                client.call_json.return_value = value
                self.assertEqual(onerun.verify_comment("BV1234567890", "fixture", rpid="101")["state"], "unknown")
            client.call_json.side_effect = RuntimeError("private-read-error-must-not-escape")
            value = onerun.verify_comment("BV1234567890", "fixture", rpid="101")
            self.assertEqual(value["state"], "unknown")
            self.assertNotIn("private-read-error", json.dumps(value))

    def test_comment_verification_cli_preserves_large_ids_and_never_treats_flags_as_text(self):
        import base64
        onerun = self.load("bili_onerun")
        target = "18446744073709551611"
        with mock.patch.object(onerun, "verify_comment", return_value={"state": "unknown"}) as verify:
            onerun.verify_cli(["--verify", "BV1234567890", "--rpid", target])
            verify.assert_called_once_with("BV1234567890", "", rpid=target)
            verify.reset_mock()
            text = "完整的离线合成正文"
            encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
            onerun.verify_cli(["--verify", "BV1234567890", "--text-b64", encoded, "--rpid", target])
            verify.assert_called_once_with("BV1234567890", text, rpid=target)
            verify.reset_mock()
            self.assertEqual(onerun.verify_cli(["--verify", "BV1234567890", "--text-b64", "bad-base64", "--rpid", target])["state"], "unknown")
            verify.assert_not_called()

    def test_comment_not_seen_in_the_bounded_sample_is_not_reported_as_moderation_or_public(self):
        onerun = self.load("bili_onerun")
        client = mock.MagicMock(); client.__enter__.return_value = client
        client.call_json.return_value = {"comments": [{"rpid": "102", "content": "fixture"}] * 50 + [{"rpid": "101", "content": "fixture"}]}
        with mock.patch.object(onerun, "BiliMCP", return_value=client):
            value = onerun.verify_comment("BV1234567890", "fixture", rpid="101")
        self.assertEqual(value["state"], "unknown")
        self.assertEqual(value["code"], "comment_not_in_sample")
        self.assertEqual(value["sample_limit"], 50)


if __name__ == "__main__":
    unittest.main()
