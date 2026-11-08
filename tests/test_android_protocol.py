"""Pinned-device vision protocol tests; all ADB calls are inert local fixtures."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zlib


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png(width=2, height=2, *, raster=None):
    raster = raster if raster is not None else (b"\0" + b"\x11\x22\x33\xff" * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raster)) + chunk(b"IEND", b""))


class AndroidProtocolTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="android protocol isolated ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        config = self.root / "project.json"
        config.write_text(json.dumps({"dataDirName": "TestCharacter", "android": {
            "serial": "emulator-5556", "maxShotAgeSeconds": 30}}), encoding="utf-8")
        env = mock.patch.dict(os.environ, {"LANYA_WORKDIR": str(self.root), "LANYA_PROJECT_CONFIG": str(config)})
        env.start(); self.addCleanup(env.stop)
        sys.path.insert(0, str(SCRIPTS)); self.addCleanup(sys.path.remove, str(SCRIPTS))
        import project_conf
        old = project_conf._CACHE
        project_conf._CACHE = None
        self.addCleanup(setattr, project_conf, "_CACHE", old)
        spec = importlib.util.spec_from_file_location("androidd_under_test", SCRIPTS / "androidd.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.time = 1000.0
        self.calls = []
        self.image = png()
        self.offline = False
        self.break_after_action = False
        self.worker = self.module.AndroidWorker(run=self.run_adb, now=lambda: self.time)

    def run_adb(self, argv, **kwargs):
        self.calls.append(argv)
        self.assertEqual(argv[1:3], ["-s", "emulator-5556"])
        self.assertEqual(kwargs["env"]["TEMP"], str(self.root / "android" / "temp"))
        args = argv[3:]
        stdout = b""
        if self.offline:
            return subprocess.CompletedProcess(argv, 1, b"", b"offline")
        if args == ["get-state"]:
            stdout = b"device\n"
        elif args == ["shell", "wm size"]:
            stdout = b"Physical size: 2x2\n"
        elif args == ["exec-out", "screencap", "-p"]:
            stdout = self.image
        elif args[:1] == ["shell"] and self.break_after_action:
            self.image = b"garbage" * 200
        return subprocess.CompletedProcess(argv, 0, stdout, b"")

    def shot(self):
        result = self.worker.handle({"seq": 1, "action": "shot"})
        self.assertTrue(result["ok"], result)
        return result

    def writes(self):
        return [args for args in self.calls if args[3:4] == ["shell"] and args[4] != "wm size"]

    def test_paths_and_all_device_commands_are_pinned_without_device_autoselection(self):
        view = self.shot()
        result = self.worker.handle({"seq": 2, "action": "tap", "x": 1, "y": 1,
                                     "expected_shot_id": view["shot_id"]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.worker.work, self.root / "TestCharacter" / "android")
        self.assertNotEqual(result["shot_id"], view["shot_id"])
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.writes()[0][3:], ["shell", "input tap 1 1"])
        self.assertEqual(sum(c[3:4] == ["exec-out"] for c in self.calls), 2)
        self.assertFalse(any("devices" in c or "connect" in c for c in self.calls))

    def test_missing_stale_wrong_frame_and_wrong_serial_never_act(self):
        result = self.worker.handle({"action": "tap", "x": 0, "y": 0})
        self.assertEqual(result["code"], "stale_view")
        view = self.shot()
        for extra in ({}, {"expected_shot_id": "invented"},
                      {"expected_shot_id": view["shot_id"], "serial": "emulator-5558"}):
            with self.subTest(extra=extra):
                result = self.worker.handle({"action": "tap", "x": 0, "y": 0, **extra})
                self.assertFalse(result["ok"])
                self.assertEqual(result["shot"], "")
        view = self.shot()
        self.time += 31
        result = self.worker.handle({"action": "tap", "x": 0, "y": 0, "expected_shot_id": view["shot_id"]})
        self.assertEqual(result["code"], "stale_view")
        self.assertEqual(self.writes(), [])

    def test_mutated_snapshot_and_invalid_coordinates_fail_before_input(self):
        view = self.shot()
        self.worker.shot.write_bytes(png(raster=(b"\0" + b"\xff\0\0\xff" * 2) * 2))
        result = self.worker.handle({"action": "tap", "x": 0, "y": 0, "expected_shot_id": view["shot_id"]})
        self.assertEqual(result["code"], "stale_view")
        for x in (-1, 2, True, "1"):
            view = self.shot()
            result = self.worker.handle({"action": "tap", "x": x, "y": 0, "expected_shot_id": view["shot_id"]})
            self.assertEqual(result["code"], "bad_job")
        self.assertEqual(self.writes(), [])

    def test_offline_and_post_action_capture_failure_never_publish_an_old_image(self):
        view = self.shot()
        self.offline = True
        result = self.worker.handle({"action": "tap", "x": 0, "y": 0, "expected_shot_id": view["shot_id"]})
        self.assertEqual(result["status"], "offline")
        self.assertEqual(result["shot"], "")
        self.offline = False
        view = self.shot()
        self.break_after_action = True
        job = {"action": "tap", "x": 0, "y": 0, "expected_shot_id": view["shot_id"]}
        result = self.worker.handle(job)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "bad_png")
        self.assertEqual(result["shot"], "")
        self.assertEqual(result["shot_id"], "")
        self.assertIsNone(self.worker.current_shot)
        self.assertEqual(self.worker.handle(job)["code"], "stale_view")
        self.assertEqual(len(self.writes()), 1)

    def test_complete_png_crc_zlib_scanlines_dimensions_and_iend_are_required(self):
        valid = png()
        invalid = [b"not PNG" * 200, valid[:-1], valid + b"trailing", valid[:30] + b"broken" + valid[36:],
                   png(raster=b"too short"), png(raster=(b"\x05" + b"\0" * 8) * 2),
                   valid.replace(chunk(b"IDAT", zlib.compress((b"\0" + b"\x11\x22\x33\xff" * 2) * 2)),
                                 chunk(b"IDAT", b"not zlib"))]
        for raw in invalid:
            with self.subTest(length=len(raw)):
                with self.assertRaises(self.module.AndroidError):
                    self.module.verify_png(raw)
        self.assertEqual(self.module.verify_png(valid), (2, 2))
        self.image = png(3, 2)
        result = self.worker.handle({"action": "shot"})
        self.assertEqual(result["code"], "bad_dimensions")
        self.assertEqual(result["shot"], "")

    def test_remote_text_is_quoted_and_legacy_shell_writes_are_refused(self):
        view = self.shot()
        text = "hello; echo unsafe & $HOME"
        result = self.worker.handle({"action": "text", "text": text, "expected_shot_id": view["shot_id"]})
        self.assertTrue(result["ok"])
        self.assertEqual(shlex.split(self.writes()[0][4]), ["input", "text", text.replace(" ", "%s")])
        writes = len(self.writes())
        for command in ("settings put global airplane_mode_on 1", "dumpsys battery set level 2", "wm size 100x100"):
            view = self.shot()
            result = self.worker.handle({"action": "shell", "cmd": command, "expected_shot_id": view["shot_id"]})
            self.assertEqual(result["code"], "bad_job")
        self.assertEqual(len(self.writes()), writes)

    def test_read_only_inspection_still_works_with_a_current_view(self):
        view = self.shot()
        result = self.worker.handle({"action": "shell", "cmd": "settings get system screen_brightness",
                                     "shot_id": view["shot_id"]})
        self.assertTrue(result["ok"])
        self.assertEqual(self.writes()[0][4], "settings get system screen_brightness")

    def test_old_frame_cannot_authorize_a_second_action(self):
        view = self.shot()
        job = {"action": "key", "key": "BACK", "expected_shot_id": view["shot_id"]}
        self.assertTrue(self.worker.handle(job)["ok"])
        self.assertEqual(self.worker.handle(job)["code"], "stale_view")
        self.assertEqual(len(self.writes()), 1)

    def test_json_queue_consumes_invalid_input_and_only_publishes_valid_shots(self):
        self.worker.cmd.write_text('{broken', encoding="utf-8")
        with mock.patch.object(self.module, "AndroidWorker", return_value=self.worker):
            self.assertEqual(self.module.main(["--once"]), 0)
        result = json.loads(self.worker.result.read_text(encoding="utf-8"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["shot"], "")
        self.assertFalse(self.worker.cmd.exists())
        self.assertEqual(list(self.worker.work.glob("cmd.claim-*")), [])
        self.worker.cmd.write_text(json.dumps({"seq": 2, "action": "shot"}), encoding="utf-8")
        with mock.patch.object(self.module, "AndroidWorker", return_value=self.worker):
            self.assertEqual(self.module.main(["--once"]), 0)
        result = json.loads(self.worker.result.read_text(encoding="utf-8"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["serial"], "emulator-5556")
        self.assertEqual(self.module.verify_png(Path(result["shot"]).read_bytes()), (2, 2))

    def test_setup_offline_has_no_image_and_second_worker_cannot_consume(self):
        self.offline = True
        with mock.patch.object(self.module, "AndroidWorker", return_value=self.worker), mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(self.module.main(["--setup"]), 1)
        self.assertEqual(json.loads(self.worker.result.read_text())["status"], "offline")
        self.assertEqual(json.loads(self.worker.result.read_text())["shot"], "")
        calls = len(self.calls)
        with self.module.worker_lock(self.worker.work / "worker.lock"):
            with mock.patch.object(self.module, "AndroidWorker", return_value=self.worker), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(self.module.main(["--once"]), 1)
            self.assertEqual(json.loads(output.getvalue())["code"], "busy")
        self.assertEqual(len(self.calls), calls)


if __name__ == "__main__":
    unittest.main()
