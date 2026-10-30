"""Android panel fixtures: temporary files only, no ADB, devices or network."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"


def png(width=2, height=3):
    def chunk(kind, raw):
        return struct.pack(">I", len(raw)) + kind + raw + struct.pack(">I", zlib.crc32(kind + raw) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress((b"\0" + b"\x11\x22\x33\xff" * width) * height)) + chunk(b"IEND", b""))


class AndroidPanelTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="android panel fixtures ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        config = self.root / "project.json"
        config.write_text(json.dumps({"dataDirName": "IsolatedRole", "android": {
            "serial": "emulator-fixture", "maxShotAgeSeconds": 30}}), encoding="utf-8")
        env = mock.patch.dict(os.environ, {"LANYA_WORKDIR": str(self.root), "LANYA_PROJECT_CONFIG": str(config)})
        env.start(); self.addCleanup(env.stop)
        old_path = sys.path[:]
        sys.path.insert(0, str(SCRIPTS)); self.addCleanup(sys.path.__setitem__, slice(None), old_path)
        import project_conf
        old, old_config_path = project_conf._CACHE, project_conf._CONFIG_PATH
        project_conf._CACHE = None
        self.addCleanup(setattr, project_conf, "_CACHE", old)
        self.addCleanup(setattr, project_conf, "_CONFIG_PATH", old_config_path)
        spec = importlib.util.spec_from_file_location("android_panel_fixture", SCRIPTS / "android_panel.py")
        self.module = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.module)
        self.clock = 1000.0
        self.live = True
        self.adapter = self.module.AndroidPanel(now=lambda: self.clock, is_running=lambda _: self.live)
        self.directory = self.adapter.directory
        self.directory.mkdir(parents=True)
        self.image = png()
        self.view = {"serial": self.adapter.serial, "shot_id": "a" * 32, "width": 2, "height": 3,
                     "captured_at": self.clock, "sha256": hashlib.sha256(self.image).hexdigest()}
        self.write("shot.png", self.image)
        self.write("shot-meta.json", self.view)
        self.write("result.json", dict(self.view, seq=1, ok=True, status="ok", shot="ignored-device-path"))
        self.write("panel-request.json", {"seq": 1, "serial": self.adapter.serial, "submitted_at": self.clock})
        run = mock.patch("subprocess.run", side_effect=AssertionError("real process forbidden"))
        run.start(); self.addCleanup(run.stop)
        network = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden"))
        network.start(); self.addCleanup(network.stop)

    def write(self, name, value):
        path = self.directory / name
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode("utf-8"))

    def read(self, name):
        return json.loads((self.directory / name).read_text(encoding="utf-8"))

    def job(self, action="shot", **fields):
        body = dict(action=action, serial=self.adapter.serial, **fields)
        if action != "shot": body.setdefault("expected_shot_id", self.view["shot_id"])
        return body

    def assert_code(self, expected, function, *args):
        with self.assertRaises(self.module.PanelAndroidError) as caught: function(*args)
        self.assertEqual(caught.exception.code, expected)

    def test_custom_paths_and_verified_image_never_use_result_path(self):
        self.assertEqual(self.directory, self.root / "IsolatedRole" / "android")
        self.assertEqual(self.adapter.status()["status"], "observed")
        self.assertEqual(self.adapter.image(self.adapter.serial, self.view["shot_id"]), self.image)

    def test_worker_offline_or_leftover_boot_image_cannot_become_online(self):
        self.live = False
        self.assertEqual(self.adapter.status()["code"], "worker_offline")
        self.assert_code("worker_offline", self.adapter.image, self.adapter.serial, self.view["shot_id"])
        self.assert_code("worker_offline", self.adapter.submit, self.job())
        self.live = True
        (self.directory / "panel-request.json").unlink()
        self.assertEqual(self.adapter.status()["code"], "capture_required")
        self.assert_code("capture_required", self.adapter.image, self.adapter.serial, self.view["shot_id"])

    def test_explicit_serial_and_current_view_are_mandatory(self):
        self.assert_code("wrong_serial", self.adapter.submit, {"action": "shot"})
        self.assert_code("wrong_serial", self.adapter.submit, {"serial": "another-device", "action": "shot"})
        self.assert_code("wrong_serial", self.adapter.image, "another-device", self.view["shot_id"])
        self.assert_code("stale_view", self.adapter.submit, self.job("tap", x=1, y=2, expected_shot_id="b" * 32))
        self.assert_code("stale_view", self.adapter.submit, {"action": "key", "serial": self.adapter.serial, "key": "BACK"})
        self.assertFalse((self.directory / "cmd.json").exists())

    def test_atomic_queue_ack_is_pending_and_invalidates_old_image(self):
        response = self.adapter.submit(self.job("tap", x=1, y=2))
        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], "pending")
        queued = self.read("cmd.json")
        self.assertEqual(queued["serial"], self.adapter.serial)
        self.assertEqual(queued["expected_shot_id"], self.view["shot_id"])
        self.assertEqual(queued["seq"], response["seq"])
        original = (self.directory / "cmd.json").read_bytes()
        self.assert_code("busy", self.adapter.submit, self.job())
        self.assertEqual((self.directory / "cmd.json").read_bytes(), original)
        self.assert_code("pending", self.adapter.image, self.adapter.serial, self.view["shot_id"])

    def test_claimed_job_and_pending_result_gap_both_prevent_second_action(self):
        response = self.adapter.submit(self.job())
        claimed = self.directory / "cmd.claim-fixture.json"
        (self.directory / "cmd.json").rename(claimed)
        self.assert_code("busy", self.adapter.submit, self.job())
        claimed.unlink()
        self.assert_code("busy", self.adapter.submit, self.job())
        self.assertEqual(self.adapter.status()["seq"], response["seq"])

    def test_claim_wait_is_read_only_for_fresh_and_old_requests(self):
        claim = self.directory / "cmd.claim-fixture.json"
        for age in (3, 86400):
            with self.subTest(age=age):
                original = b'{"action":"tap","seq":7}'
                claim.write_bytes(original)
                os.utime(claim, (self.clock - age, self.clock - age))
                before = claim.stat()
                response = self.adapter.status()
                self.assertEqual(response["status"], "pending")
                self.assertEqual(response["claim_wait"], {
                    "present": True, "count": 1, "age_seconds": age,
                    "detail": "waiting", "result": "unknown"})
                self.assertEqual(response["shot_id"], "")
                self.assert_code("busy", self.adapter.submit, self.job())
                self.assert_code("busy", self.adapter.submit, self.job("tap", x=0, y=0))
                self.assert_code("pending", self.adapter.image, self.adapter.serial, self.view["shot_id"])
                self.assertEqual(claim.read_bytes(), original)
                self.assertEqual(claim.stat().st_mtime_ns, before.st_mtime_ns)
                self.assertFalse((self.directory / "cmd.json").exists())
        self.live = False
        response = self.adapter.status()
        self.assertEqual(response["code"], "worker_offline")
        self.assertEqual(response["claim_wait"]["age_seconds"], 86400)
        self.assertTrue(claim.exists())

    def test_malformed_claim_body_is_not_read_or_interpreted_as_completion(self):
        claim = self.directory / "cmd.claim-bad.json"
        claim.write_bytes(b'not JSON\x00')
        os.utime(claim, (self.clock - 8, self.clock - 8))
        response = self.adapter.status()
        self.assertEqual(response["claim_wait"]["age_seconds"], 8)
        self.assertEqual(response["claim_wait"]["result"], "unknown")
        self.assertEqual(response["code"], "pending")
        self.assert_code("busy", self.adapter.submit, self.job())
        self.assertEqual(claim.read_bytes(), b'not JSON\x00')

    def test_claim_wait_reports_clock_anomaly_without_negative_or_guessed_age(self):
        claim = self.directory / "cmd.claim-fixture.json"
        claim.write_bytes(b'{}')
        os.utime(claim, (self.clock + 30, self.clock + 30))
        for now in (self.clock, float("nan"), float("inf")):
            with self.subTest(now=now):
                with mock.patch.object(self.adapter, "now", return_value=now):
                    response = self.adapter.status()
                self.assertIsNone(response["claim_wait"]["age_seconds"])
                self.assertEqual(response["claim_wait"]["detail"], "clock_anomaly")
                self.assertEqual(response["code"], "pending")
        self.assert_code("busy", self.adapter.submit, self.job())

    def test_claim_link_and_unreadable_metadata_are_not_followed_or_given_age(self):
        # Deterministic symlink/reparse metadata seam: Windows may deny creating
        # actual symlinks. The real queue filename still blocks all submissions.
        claim = self.directory / "cmd.claim-fixture.json"
        claim.write_bytes(b'unchanged unknown request')
        variants = [
            (SimpleNamespace(st_mode=stat.S_IFLNK, st_mtime=0, st_file_attributes=0), "link_or_non_regular"),
            (SimpleNamespace(st_mode=stat.S_IFREG, st_mtime=0, st_file_attributes=0x400), "link_or_non_regular"),
            (PermissionError("fixture unreadable"), "metadata_unavailable"),
        ]
        for metadata, detail in variants:
            with self.subTest(detail=detail):
                entry = mock.Mock(name="claim entry")
                entry.name = claim.name
                if isinstance(metadata, Exception): entry.stat.side_effect = metadata
                else: entry.stat.return_value = metadata
                scan = mock.MagicMock()
                scan.__enter__.return_value = iter([entry])
                with mock.patch.object(self.module.os, "scandir", return_value=scan):
                    diagnostic = self.adapter._claim_wait()
                entry.stat.assert_called_once_with(follow_symlinks=False)
                self.assertEqual(diagnostic["detail"], detail)
                self.assertIsNone(diagnostic["age_seconds"])
                self.assertEqual(self.adapter.status()["code"], "pending")
                self.assert_code("busy", self.adapter.submit, self.job())
        self.assertEqual(claim.read_bytes(), b'unchanged unknown request')

    def test_multiple_claims_use_oldest_file_time_and_no_claim_hides_diagnostic(self):
        self.assertIsNone(self.adapter.status()["claim_wait"])
        for suffix, age in (("one", 4), ("two", 19)):
            claim = self.directory / ("cmd.claim-" + suffix + ".json")
            claim.write_bytes(b'{}')
            os.utime(claim, (self.clock - age, self.clock - age))
        response = self.adapter.status()
        self.assertEqual(response["claim_wait"]["count"], 2)
        self.assertEqual(response["claim_wait"]["age_seconds"], 19)
        self.assert_code("busy", self.adapter.submit, self.job())

    def test_real_worker_function_roundtrip_uses_only_inert_adb_fixture(self):
        from androidd import AndroidWorker, atomic_json
        def adb(argv, **kwargs):
            self.assertEqual(argv[1:3], ["-s", self.adapter.serial])
            args = argv[3:]
            output = b"device\n" if args == ["get-state"] else b"Physical size: 2x3\n" if args == ["shell", "wm size"] else self.image
            return SimpleNamespace(returncode=0, stdout=output)
        worker = AndroidWorker(run=adb, now=lambda: self.clock)
        response = self.adapter.submit(self.job())
        job = self.read("cmd.json"); (self.directory / "cmd.json").unlink()
        result = worker.handle(job); atomic_json(worker.result, result)
        self.assertTrue(result["ok"])
        state = self.adapter.status()
        self.assertEqual(state["seq"], response["seq"])
        self.assertEqual(state["shot_id"], result["shot_id"])
        self.assertEqual(self.adapter.image(self.adapter.serial, state["shot_id"]), self.image)

    def test_failed_action_keeps_error_and_never_serves_old_png(self):
        response = self.adapter.submit(self.job("key", key="HOME"))
        (self.directory / "cmd.json").unlink()
        self.write("result.json", {"seq": response["seq"], "serial": self.adapter.serial,
            "ok": False, "status": "offline", "code": "offline", "note": "fixture disconnected", "shot": "", "shot_id": ""})
        state = self.adapter.status()
        self.assertEqual(state["error"], "fixture disconnected")
        self.assertEqual(state["shot_id"], "")
        self.assertTrue((self.directory / "shot.png").exists())
        self.assert_code("offline", self.adapter.image, self.adapter.serial, self.view["shot_id"])
        self.assert_code("offline", self.adapter.submit, self.job("tap", x=0, y=0))

    def test_expired_or_corrupt_frames_are_not_actionable(self):
        self.clock += 31
        self.assertEqual(self.adapter.status()["code"], "stale_view")
        self.assert_code("stale_view", self.adapter.submit, self.job("tap", x=0, y=0))
        self.clock -= 31
        self.write("shot.png", self.image[:-1])
        self.assertEqual(self.adapter.status()["code"], "invalid_frame")
        self.write("shot.png", png(3, 2))
        self.assertEqual(self.adapter.status()["code"], "stale_view")

    def test_only_existing_worker_actions_and_valid_fields_are_accepted(self):
        invalid = [self.job("install", apk="x"), self.job("tap", x=True, y=0),
            self.job("tap", x=2, y=0), self.job("key", key="BACK; reboot"),
            self.job("shell", cmd="rm -rf /"), self.job("shell", cmd="settings put global x 1"),
            self.job("text", text="control\x00"), self.job("start", pkg="bad/pkg"),
            self.job("shot", expected_shot_id="a", unexpected="payload")]
        for body in invalid:
            with self.subTest(body=body): self.assert_code("bad_job", self.adapter.submit, body)
        self.assertFalse((self.directory / "cmd.json").exists())

    def test_publish_race_preserves_other_job_and_blocks_old_view_until_fresh_capture(self):
        def raced_publish(*_):
            self.write("cmd.json", {"serial": "external", "action": "shot", "seq": 7})
            raise FileExistsError("fixture race")
        with mock.patch.object(self.module.os, "link", side_effect=raced_publish):
            self.assert_code("busy", self.adapter.submit, self.job())
        self.assertEqual(self.read("cmd.json")["serial"], "external")
        self.assertEqual(self.adapter.status()["code"], "queue_failed")
        (self.directory / "cmd.json").unlink()
        self.assertEqual(self.adapter.submit(self.job())["status"], "pending")

    def test_timeout_never_serves_old_frame_but_allows_explicit_new_capture(self):
        self.adapter.submit(self.job()); (self.directory / "cmd.json").unlink()
        self.clock += 121
        self.assertEqual(self.adapter.status()["code"], "request_timeout")
        self.assertEqual(self.adapter.submit(self.job())["status"], "pending")

    def test_malformed_state_fails_closed(self):
        self.write("result.json", {"seq": "not-an-integer"})
        self.assertEqual(self.adapter.status()["code"], "invalid_state")
        self.write("panel-request.json", b"not json")
        self.assertEqual(self.adapter.status()["code"], "invalid_state")

    def test_real_os_lock_probe_ignores_leftover_files(self):
        lock = self.directory / "worker.lock"
        self.assertFalse(self.module.worker_running(lock))
        with self.module.worker_lock(lock):
            self.assertTrue(self.module.worker_running(lock))
        self.assertFalse(self.module.worker_running(lock))

    def handler(self, path, body=None, origin=None):
        spec = importlib.util.spec_from_file_location("android_panel_server_fixture", REPO / "server" / "panel-server.py")
        server = importlib.util.module_from_spec(spec); spec.loader.exec_module(server)
        handler = server.Handler.__new__(server.Handler)
        handler.path = path
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        handler.headers = {"content-length": str(len(raw))}
        if origin: handler.headers["Origin"] = origin
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        handler.server = SimpleNamespace(server_port=19389)
        handler.response_headers = {}
        handler.send_response = lambda code: setattr(handler, "response_code", code)
        handler.send_header = lambda key, value: handler.response_headers.update({key.lower(): value})
        handler.end_headers = lambda: None
        handler._json = lambda code, value: (code, value)
        return handler

    def test_http_routes_preserve_pending_errors_and_uncached_png(self):
        with mock.patch.dict(sys.modules, {"android_panel": self.module}), mock.patch.object(self.module, "AndroidPanel", return_value=self.adapter):
            handler = self.handler("/api/android/status?serial=" + self.adapter.serial)
            self.assertEqual(handler.do_GET()[1]["status"], "observed")
            handler = self.handler("/api/android/shot?serial=" + self.adapter.serial + "&shot_id=" + self.view["shot_id"])
            self.assertIsNone(handler.do_GET())
            self.assertEqual(handler.response_code, 200)
            self.assertIn("no-store", handler.response_headers["cache-control"])
            self.assertEqual(handler.wfile.getvalue(), self.image)
            handler = self.handler("/api/android/job", self.job(), "http://127.0.0.1:19389")
            code, body = handler.do_POST()
            self.assertEqual(code, 202); self.assertFalse(body["ok"])
            handler = self.handler("/api/android/shot?serial=" + self.adapter.serial + "&shot_id=" + self.view["shot_id"])
            self.assertEqual(handler.do_GET()[1]["code"], "pending")

    def test_http_rejects_foreign_origin_duplicate_query_and_unknown_serial(self):
        with mock.patch.dict(sys.modules, {"android_panel": self.module}), mock.patch.object(self.module, "AndroidPanel", return_value=self.adapter):
            handler = self.handler("/api/android/job", self.job(), "https://untrusted.invalid")
            self.assertEqual(handler.do_POST()[0], 403)
            self.assertFalse((self.directory / "cmd.json").exists())
            handler = self.handler("/api/android/status?serial=x&serial=y")
            self.assertEqual(handler.do_GET()[0], 400)
            handler = self.handler("/api/android/status?serial=other")
            self.assertEqual(handler.do_GET()[1]["code"], "wrong_serial")


if __name__ == "__main__": unittest.main()
