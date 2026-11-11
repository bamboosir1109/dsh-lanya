"""Drive the real panel queue and worker with inert ADB results."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_android_protocol import SCRIPTS, png


class AndroidExecutorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="android agent fixture ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.role = self.root / "FixtureRole"
        self.role.mkdir()
        (self.role / "lanya-config.json").write_text('{"adminQQ":"101"}', encoding="utf-8")
        self.config = self.root / "project.json"
        self.options = {"dataDirName": "FixtureRole", "android": {
            "agentEnabled": True, "serial": "emulator-5556", "adbServerPort": 5039}}
        self.config.write_text(json.dumps(self.options), encoding="utf-8")
        env = mock.patch.dict(os.environ, {"LANYA_WORKDIR": str(self.root),
            "LANYA_PROJECT_CONFIG": str(self.config), "ADB_SERVER_SOCKET": "tcp:remote.invalid:5037"})
        env.start()
        self.addCleanup(env.stop)
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(sys.path.remove, str(SCRIPTS))
        import android_executor
        import androidd
        import project_conf
        for key in ("_CACHE", "_CONFIG_PATH"):
            self.addCleanup(setattr, project_conf, key, getattr(project_conf, key))
        project_conf._CACHE = None
        self.module = android_executor
        self.androidd = androidd
        self.image = png()
        self.actions = []
        self.fail_capture = False
        self.worker = androidd.AndroidWorker(run=self.adb)

    def adb(self, args, **kwargs):
        self.assertEqual(args[1:3], ["-s", "emulator-5556"])
        self.assertEqual(kwargs["env"]["ADB_SERVER_SOCKET"], "tcp:5039")
        request = args[3:]
        if request == ["get-state"]:
            data = b"device\n"
        elif request == ["shell", "wm size"]:
            data = b"Physical size: 2x2\n"
        elif request == ["exec-out", "screencap", "-p"]:
            data = self.image
        else:
            self.actions.append(request)
            data = b""
            self.image = (b"invalid after input" if self.fail_capture else
                          png(raster=(b"\0" + b"\x22\x33\x44\xff" * 2) * 2))
        return subprocess.CompletedProcess(args, 0, data, b"")

    def consume(self, _delay):
        command = self.worker.cmd
        job = json.loads(command.read_text(encoding="utf-8"))
        result = self.worker.handle(job)
        command.unlink()
        self.androidd.atomic_json(self.worker.result, result)

    def call(self, scope="private:101", **job):
        request = {"cwd": str(self.root), "agent_preset": "fixture", "conversation_key": scope, "job": job}
        return self.module.execute(request, sleep=self.consume)

    def test_owner_can_open_game_and_swipe_with_verified_distinct_frames(self):
        # Run consecutive jobs under one worker, preserving its last view.
        with self.androidd.worker_lock(self.worker.work / "worker.lock"):
            self.androidd.atomic_json(self.worker.result, self.worker.handle({"seq": 0, "action": "shot"}))

            first = self.call(action="shot")
            self.assertEqual(first["status"], "observed")
            game = self.call(action="open_game", expected_shot_id=first["shot_id"])
            self.assertEqual(game["status"], "observed")
            self.assertEqual(self.actions[0], ["shell", "monkey -p org.dshlanya.game2048 -c android.intent.category.LAUNCHER 1"])
            stale = self.call(action="tap", x=1, y=1, expected_shot_id=first["shot_id"])
            self.assertEqual(stale["code"], "stale_view")
            self.assertEqual(len(self.actions), 1)
            move = self.call(action="swipe", x1=0, y1=0, x2=1, y2=1, expected_shot_id=game["shot_id"])
            self.assertEqual(move["status"], "observed")
            self.assertEqual(len({first["shot_id"], game["shot_id"], move["shot_id"]}), 3)
            self.assertEqual(Path(first["file"]).read_bytes(), png())
            self.assertNotEqual(first["sha256"], move["sha256"])
            self.fail_capture = True
            failed = self.call(action="tap", x=1, y=1, expected_shot_id=move["shot_id"])
            self.assertEqual(failed["status"], "unknown")
            self.assertNotIn("file", failed)
            self.assertEqual(len(self.actions), 3)

    def test_other_scopes_and_opt_out_do_not_queue(self):
        for scope in ("private:102", "group:101"):
            receipt = self.call(scope, action="shot")
            self.assertEqual(receipt["code"], "primary_private_scope_required")
        self.options["android"]["agentEnabled"] = False
        self.config.write_text(json.dumps(self.options), encoding="utf-8")
        receipt = self.call(action="shot")
        self.assertEqual(receipt["code"], "dedicated_emulator_not_enabled")
        self.assertFalse(self.worker.cmd.exists())
        self.assertEqual(self.actions, [])

    def test_forwarded_emulator_must_be_loopback_with_its_own_adb_server(self):
        validate = self.androidd.agent_emulator_target
        options = {"agentEnabled": True, "agentTransport": "qemu_loopback", "adbServerPort": 5039}
        self.assertTrue(validate(options, "127.0.0.1:15555"))
        for serial in ("192.0.2.1:15555", "localhost:15555", "emulator-5557", "127.0.0.1:70000"):
            self.assertFalse(validate(options, serial))
        self.assertFalse(validate(dict(options, adbServerPort=5037), "127.0.0.1:15555"))
        self.assertFalse(validate(dict(options, agentTransport=None), "127.0.0.1:15555"))
        desktop = dict(options, agentTransport="desktop_loopback", adbServerPort=15043)
        self.assertTrue(validate(desktop, "127.0.0.1:16384"))
        self.assertFalse(validate(desktop, "127.0.0.1:15043"))
        self.assertFalse(validate(desktop, "192.0.2.1:16384"))

    def test_named_app_launch_is_enabled_explicitly_and_uses_the_current_frame(self):
        self.options["android"]["agentApps"] = ["amap"]
        self.config.write_text(json.dumps(self.options), encoding="utf-8")
        with self.androidd.worker_lock(self.worker.work / "worker.lock"):
            self.androidd.atomic_json(self.worker.result, self.worker.handle({"seq": 0, "action": "shot"}))
            first = self.call(action="shot")
            frame = {"action": "open_app", "expected_shot_id": first["shot_id"]}
            for alias in ("qq", "com.autonavi.minimap", []):
                self.assertEqual(self.call(app=alias, **frame)["code"], "android_app_not_enabled")
            self.assertEqual(self.actions, [])
            opened = self.call(app="amap", **frame)
            self.assertEqual(opened["status"], "observed")
            self.assertNotEqual(opened["shot_id"], first["shot_id"])
            self.assertEqual(self.actions, [["shell", "monkey -p com.autonavi.minimap -c android.intent.category.LAUNCHER 1"]])
            self.assertEqual(self.call(app="amap", **frame)["code"], "stale_view")
