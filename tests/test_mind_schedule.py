"""Offline checks: all state is temporary; DSH and OneBot are mocked."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class MindScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = self.root / "project.json"
        config.write_text(json.dumps({"dataDirName": "test-character",
                                      "decisions": {"mode": "legacy"},
                                      "onebotPort": 18720,
                                      "daemon": {"headlessTimeoutSec": 17}}),
                          encoding="utf-8")
        env = mock.patch.dict(os.environ, {"LANYA_PROJECT_CONFIG": str(config),
                                           "LANYA_WORKDIR": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        path_patch = mock.patch.object(sys, "path", [str(SCRIPTS)] + sys.path)
        path_patch.start()
        self.addCleanup(path_patch.stop)
        import project_conf
        project_conf.load(reload=True)
        self.addCleanup(setattr, project_conf, "_CACHE", None)
        spec = importlib.util.spec_from_file_location("mind_under_test", SCRIPTS / "mind.py")
        self.mind = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mind)
        self.real_ask = self.mind.ask
        self.real_deliver = self.mind.deliver
        self.ask = self.patch("ask", return_value="THINK continue\nPLAN read\nNEXT page 2\nETA 12")
        self.deliver = self.patch("deliver", return_value=True)
        self.patch("build_prompt", return_value="offline test")
        self.patch("log")
        clock = mock.patch.object(self.mind.time, "time", return_value=1000.0)
        self.clock = clock.start()
        self.addCleanup(clock.stop)

    def patch(self, name, **kwargs):
        p = mock.patch.object(self.mind, name, **kwargs)
        result = p.start()
        self.addCleanup(p.stop)
        return result

    def state(self):
        return json.loads(Path(self.mind.MIND).read_text(encoding="utf-8"))

    def test_eta_gates_repeated_ticks_until_due(self):
        self.assertEqual(self.mind.one_cycle(), 0)
        self.assertEqual(self.state()["next_check_ts"], 1720)
        self.assertEqual(self.mind.one_cycle(), 0)
        self.ask.assert_called_once()
        self.clock.return_value = 1719
        self.assertEqual(self.mind.one_cycle(), 0)
        self.ask.assert_called_once()
        self.clock.return_value = 1720
        self.assertEqual(self.mind.one_cycle(), 0)
        self.assertEqual(self.ask.call_count, 2)

    def test_force_bypasses_eta_but_not_lock(self):
        self.mind.one_cycle()
        self.assertEqual(self.mind.one_cycle(force=True), 0)
        self.assertEqual(self.ask.call_count, 2)
        with self.mind.cycle_lock() as held:
            self.assertTrue(held)
            self.assertEqual(self.mind.one_cycle(force=True), 0)
        self.assertEqual(self.ask.call_count, 2)

    def test_eta_is_clamped_to_existing_bounds(self):
        self.ask.return_value = "PLAN read\nETA 120"
        self.assertEqual(self.mind.one_cycle(), 0)
        self.assertEqual(self.state()["next_check_ts"], 4600)
        self.ask.return_value = "PLAN read\nETA 1"
        self.assertEqual(self.mind.one_cycle(force=True), 0)
        self.assertEqual(self.state()["next_check_ts"], 1120)

    def test_missing_dsh_is_an_explicit_error(self):
        with mock.patch.object(self.mind, "find_dsh", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "dsh executable not found"):
                self.real_ask("offline")

    def test_lock_releases_after_exception(self):
        with self.assertRaisesRegex(ValueError, "test"):
            with self.mind.cycle_lock() as held:
                self.assertTrue(held)
                raise ValueError("test")
        with self.mind.cycle_lock() as held:
            self.assertTrue(held)

    def test_failure_preserves_plan_and_interrupts_and_sets_retry(self):
        old = self.mind.blank_mind()
        old["plan"] = {"goal": "existing plan"}
        hits = [{"who": "test", "text": "hello"}]
        self.mind.save(self.mind.MIND, old)
        self.mind.save(self.mind.HITFILE, hits)
        self.ask.side_effect = RuntimeError("agent timed out after 17 seconds")
        self.assertEqual(self.mind.one_cycle(), 1)
        state = self.state()
        self.assertEqual(state["plan"], old["plan"])
        self.assertEqual(state["next_check_ts"], 1360)
        self.assertIn("timed out", state["last_error"])
        self.assertEqual(self.mind.load(self.mind.HITFILE, []), hits)
        self.deliver.assert_not_called()

    def test_invalid_response_preserves_plan(self):
        self.mind.save(self.mind.MIND, {"plan": {"goal": "existing plan"}})
        self.ask.return_value = "unexpected response"
        self.assertEqual(self.mind.one_cycle(), 1)
        self.assertEqual(self.state()["plan"]["goal"], "existing plan")

    def test_success_only_consumes_handled_interrupts(self):
        old = [{"who": "old", "text": "hello"}]
        new = {"who": "new", "text": "later"}
        self.mind.save(self.mind.HITFILE, old)

        def answer(_prompt):
            self.mind.save(self.mind.HITFILE, old + [new])
            return "PLAN read\nETA 8"

        self.ask.side_effect = answer
        self.assertEqual(self.mind.one_cycle(), 0)
        self.assertEqual(self.mind.load(self.mind.HITFILE, []), [new])

    def test_dry_preview_never_calls_agent_or_consumes_state(self):
        self.mind.save(self.mind.MIND, {"plan": None, "next_check_ts": 9999})
        self.mind.save(self.mind.HITFILE, [{"text": "hello"}])
        before = Path(self.mind.MIND).read_bytes()
        self.assertEqual(self.mind.one_cycle(dry=True), 0)
        self.assertEqual(Path(self.mind.MIND).read_bytes(), before)
        self.assertEqual(self.mind.load(self.mind.HITFILE, []), [{"text": "hello"}])
        self.ask.assert_not_called()
        self.deliver.assert_not_called()

    def test_corrupt_state_is_not_overwritten(self):
        Path(self.mind.MIND).parent.mkdir(parents=True)
        Path(self.mind.MIND).write_text("{broken", encoding="utf-8")
        self.assertEqual(self.mind.one_cycle(), 1)
        self.assertEqual(Path(self.mind.MIND).read_text(encoding="utf-8"), "{broken")
        self.ask.assert_not_called()

    def test_agent_timeout_and_nonzero_exit_are_reported(self):
        with mock.patch.object(self.mind, "find_dsh", return_value="fake-dsh"), \
                mock.patch.object(self.mind.subprocess, "run") as run:
            run.side_effect = subprocess.TimeoutExpired("fake-dsh", 17)
            with self.assertRaisesRegex(RuntimeError, "timed out after 17"):
                self.real_ask("offline")
            self.assertEqual(run.call_args.kwargs["timeout"], 17)
            run.side_effect = None
            run.return_value = subprocess.CompletedProcess([], 3, b"", b"offline error")
            with self.assertRaisesRegex(RuntimeError, "exited with code 3"):
                self.real_ask("offline")

    def test_delivery_failure_returns_nonzero_and_retains_interrupts(self):
        self.mind.save(self.mind.HITFILE, [{"text": "hello"}])
        self.ask.return_value = "PLAN read\nSAY admin hello\nETA 8"
        self.deliver.return_value = False
        self.assertEqual(self.mind.one_cycle(), 1)
        self.assertIn("OneBot delivery failed", self.state()["last_error"])
        self.assertEqual(self.mind.load(self.mind.HITFILE, []), [{"text": "hello"}])

    def test_onebot_uses_configured_port_and_matching_local_token(self):
        self.mind.save(os.path.join(self.mind.DATA, "lanya-config.json"), {"adminQQ": [123]})
        self.mind.save(os.path.join(self.mind.RUNTIME, "framework", "config", "onebot11.json"),
                       {"network": {"httpServers": [{"port": 8720, "token": "wrong-test-token"},
                                                     {"port": 18720, "token": "test-token"}]}})
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"retcode": 0}'
        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            self.assertTrue(self.real_deliver("admin", "offline"))
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:18720/send_private_msg")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(json.loads(request.data)["user_id"], "123")


if __name__ == "__main__":
    unittest.main()
