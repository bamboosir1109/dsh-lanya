"""Panel control fixtures: temporary metadata, inert subprocesses, no models/network."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]


class PanelAutonomyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lanya panel autonomy ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.napcat = self.root / "napcat"
        self.napcat.mkdir()
        self.config = self.root / "project.json"
        self.write(self.config, {"dataDirName": "fixture", "decisions": {"mode": "unified"}})
        env = mock.patch.dict(os.environ, {"LANYA_WORKDIR": str(self.root), "LANYA_PROJECT_CONFIG": str(self.config)})
        env.start(); self.addCleanup(env.stop)
        old_path = sys.path[:]
        sys.path.insert(0, str(REPO / "scripts")); self.addCleanup(sys.path.__setitem__, slice(None), old_path)
        import project_conf
        old, old_config_path = project_conf._CACHE, project_conf._CONFIG_PATH
        project_conf._CACHE = None
        self.addCleanup(setattr, project_conf, "_CACHE", old)
        self.addCleanup(setattr, project_conf, "_CONFIG_PATH", old_config_path)
        for target in ("subprocess.run", "subprocess.Popen", "urllib.request.urlopen"):
            guard = mock.patch(target, side_effect=AssertionError("real I/O forbidden"))
            guard.start(); self.addCleanup(guard.stop)
        spec = importlib.util.spec_from_file_location("panel_autonomy_fixture", REPO / "server" / "panel-server.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.data = Path(self.module.DATA)
        self.data.mkdir()
        (self.data / "made").mkdir()
        self.clock = mock.patch.object(self.module.time, "time", return_value=1000.0)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.owner = mock.patch.object(self.module, "_scheduler_owner_verified", return_value=True)
        self.owner_mock = self.owner.start(); self.addCleanup(self.owner.stop)
        self.write(self.napcat / "scheduler-in-plugin.flag", {"version": 2, "pid": 42, "at": 1000000, "jobs": ["mind"]})
        (self.napcat / "mind.py").write_text("# inert fixture\n", encoding="utf-8")
        inbox = self.data / "decision-inbox"
        inbox.mkdir()
        self.lease = inbox / "consumer.sqlite"
        with sqlite3.connect(str(self.lease)) as db:
            db.executescript("CREATE TABLE consumer(id INTEGER PRIMARY KEY, pid INTEGER, nonce TEXT); PRAGMA user_version=1;")
            db.execute("INSERT INTO consumer VALUES(1,42,'fixture-only')")
        self.mind = {"decision_observers_version": 2, "decision_scopes": {
            "private:101": {"generation": 1, "last_status": "decided", "last_completed_at": 999000,
                            "next_opportunity_at": 1010000}}, "plan": "PRIVATE PLAN MUST NOT LEAK"}
        self.write(self.data / "mind.json", self.mind)
        self.write(self.data / "made" / "lanya-state.json", {"at": "fixture", "memory": {},
            "components": [{"tag": "lifeloop", "count": 0, "mb": 0}, {"tag": "mind", "count": 0, "mb": 0}]})
        self.write(self.data / "activities.json", {"activities": [{"id": "fixture", "bound": "lifeloop"}]})

    def write(self, file, value):
        file.write_text(json.dumps(value), encoding="utf-8")

    def route(self, path, body=None, raw=None):
        handler = object.__new__(self.module.Handler)
        handler.path = path
        data = raw if raw is not None else json.dumps(body or {}).encode("utf-8")
        handler.headers = {"content-length": str(len(data))}
        handler.rfile = io.BytesIO(data)
        handler._json = lambda code, value: (code, value)
        return handler.do_GET() if body is None and raw is None else handler.do_POST()

    def test_zero_processes_are_managed_consistently_without_private_identifiers(self):
        before = hashlib.sha256(self.lease.read_bytes()).hexdigest()
        state = self.route("/api/state")[1]
        live = self.route("/api/live")[1]["live"]
        activity = self.route("/api/activities")[1]["data"]
        self.assertEqual(state["autonomy"], live["autonomy"])
        self.assertEqual(state["autonomy"], activity["autonomy"])
        self.assertEqual(state["autonomy"]["status"], "waiting")
        self.assertTrue(activity["activities"][0]["ready"])
        self.assertTrue(all(row["count"] == 0 and row["control"] == "request_check" for row in state["components"] if row["tag"] in ("lifeloop", "mind")))
        safe = json.dumps(state) + json.dumps(live) + json.dumps(activity)
        for private in ("PRIVATE PLAN", "private:101", "fixture-only", '"pid"', '"nonce"'):
            self.assertNotIn(private, safe)
        self.assertEqual(before, hashlib.sha256(self.lease.read_bytes()).hexdigest())
        self.assertEqual(list(self.lease.parent.iterdir()), [self.lease])

    def test_stale_owner_mismatch_and_unverifiable_owner_refuse_checks(self):
        self.write(self.napcat / "scheduler-in-plugin.flag", {"version": 2, "pid": 42, "at": 700000, "jobs": ["mind"]})
        self.assertEqual(self.module.autonomy_snapshot()["status"], "offline")
        self.write(self.napcat / "scheduler-in-plugin.flag", {"version": 2, "pid": 42, "at": 1000000, "jobs": ["mind"]})
        for owner, expected in ((False, "offline"), (None, "unknown")):
            self.owner_mock.return_value = owner
            self.assertEqual(self.module.autonomy_snapshot()["status"], expected)
            self.assertFalse(self.module.request_autonomy_check()["ok"])
        self.owner_mock.return_value = True
        with sqlite3.connect(str(self.lease)) as db: db.execute("UPDATE consumer SET pid=43")
        self.assertEqual(self.module.autonomy_snapshot()["reason"], "consumer_owner_unverified")
        self.assertFalse(self.module.request_autonomy_check()["ok"])

    def test_scheduled_review_precedes_pending_and_unknown_is_explicit(self):
        row = self.mind["decision_scopes"]["private:101"]
        row.update(pending={"event_id": "synthetic-never-output"}, next_review_at=1020000, last_status="unknown")
        self.write(self.data / "mind.json", self.mind)
        self.assertEqual(self.module.autonomy_snapshot()["status"], "scheduled")
        row["next_review_at"] = None
        self.write(self.data / "mind.json", self.mind)
        self.assertEqual(self.module.autonomy_snapshot()["status"], "processing")
        row.pop("pending")
        self.write(self.data / "mind.json", self.mind)
        self.assertEqual(self.module.autonomy_snapshot()["status"], "uncertain")

    def test_missing_observer_evidence_is_aggregated_ahead_of_stale_scheduled_review(self):
        row = self.mind["decision_scopes"]["private:101"]
        row.update(next_review_at=999000, observer_error={"error_class": "FileNotFoundError", "first_seen_at": 990000, "last_seen_at": 999000})
        self.write(self.data / "mind.json", self.mind)
        status = self.module.autonomy_snapshot()
        self.assertEqual(status["status"], "uncertain")
        self.assertEqual(status["reason"], "observer_evidence_missing")
        self.assertEqual(status["observer"]["error_count"], 1)
        self.assertNotIn("FileNotFoundError", json.dumps(status))
        self.assertNotIn("private:101", json.dumps(status))

    def test_invalid_or_changed_mode_never_reuses_cached_legacy_authority(self):
        self.write(self.config, {"decisions": {"mode": "legacy"}})
        self.assertEqual(self.module.decision_mode(), "legacy")
        self.config.write_text("broken config PRIVATE SENTINEL", encoding="utf-8")
        self.assertEqual(self.module.autonomy_snapshot()["status"], "unknown")
        self.assertFalse(self.module.request_autonomy_check()["ok"])
        with self.assertRaises(self.module.project_conf.ConfigError): self.module.run_task("lifeloop")

    def test_bad_metadata_fails_closed_including_boolean_timestamp_and_links(self):
        row = self.mind["decision_scopes"]["private:101"]
        row["next_opportunity_at"] = True
        self.write(self.data / "mind.json", self.mind)
        self.assertEqual(self.module.autonomy_snapshot()["status"], "unknown")
        with mock.patch.object(self.module.os, "lstat", return_value=SimpleNamespace(st_mode=0, st_file_attributes=0x400)):
            self.assertFalse(self.module.request_autonomy_check()["ok"])

    def test_old_life_start_restart_and_direct_helpers_cannot_reach_tasks(self):
        for target in ("lifeloop", "voicedaemon", "supervisor", "tick"):
            with self.assertRaises(ValueError): self.module.run_task(target)
            with self.assertRaises(ValueError): self.module.run_script(target)
        for op in ("start", "restart", "check"):
            with mock.patch.object(self.module, "request_autonomy_check", return_value={"ok": True, "observation_only": True}) as request:
                self.assertTrue(self.module.control_action({"target": "lifeloop", "op": op})["observation_only"])
                request.assert_called_once_with()
        self.assertFalse(self.module.control_action({"target": "mind", "op": "stop"})["ok"])

    def test_click_produces_only_mind_force_and_repeated_click_is_deduplicated(self):
        process = SimpleNamespace(poll=lambda: None)
        with mock.patch.object(self.module.subprocess, "Popen", return_value=process) as popen:
            first = self.route("/api/action", {"target": "lifeloop"})[1]
            second = self.route("/api/autonomy/check", {})[1]
            self.assertTrue(first["producer_started"])
            self.assertFalse(first["decision_completed"])
            self.assertTrue(first["observation_only"])
            self.assertEqual(second["status"], "already_requested")
            popen.assert_called_once()
            self.assertEqual(popen.call_args.args[0][-3:], [str(self.napcat / "mind.py"), "--force", "--observe-only"])
            self.assertEqual(popen.call_args.kwargs["cwd"], str(self.napcat))
            self.assertEqual(json.loads((self.data / "mind.json").read_text()), self.mind)

    def test_completed_producer_still_obeys_shared_cooldown(self):
        process = SimpleNamespace(poll=lambda: 0)
        with mock.patch.object(self.module.subprocess, "Popen", return_value=process) as popen, \
                mock.patch.object(self.module, "autonomy_snapshot", return_value={"mode": "unified", "can_request_check": True}), \
                mock.patch.object(self.module.time, "monotonic", side_effect=[100, 110, 116]):
            self.assertEqual(self.module.request_autonomy_check()["status"], "requested")
            self.assertEqual(self.module.request_autonomy_check()["status"], "already_requested")
            self.assertEqual(self.module.request_autonomy_check()["status"], "requested")
            self.assertEqual(popen.call_count, 2)

    def test_check_rejects_caller_scopes_and_invalid_json_before_producing(self):
        for raw in (b'{"scope":"private:101"}', b'{', b'[]'):
            self.assertEqual(self.route("/api/autonomy/check", raw=raw)[0], 400)

    def test_legacy_task_behavior_and_oem_errors_remain_readable(self):
        self.write(self.config, {"decisions": {"mode": "legacy"}})
        result = SimpleNamespace(returncode=1, stdout=b"", stderr="错误: 任务已禁用。".encode("cp936"))
        with mock.patch.object(self.module.subprocess, "run", return_value=result) as run, \
                mock.patch.object(self.module, "decode_task_output", wraps=lambda raw: raw.decode("cp936").strip()):
            response = self.module.control_action({"target": "lifeloop"})
            self.assertFalse(response["ok"])
            self.assertEqual(response["log"], "错误: 任务已禁用。")
            self.assertEqual(run.call_args.args[0], ["schtasks.exe", "/run", "/tn", "DSH-Lanya-Life"])
        self.assertEqual(self.module.decode_task_output(result.stderr, "cp936"), "错误: 任务已禁用。")
        self.assertEqual(self.module.decode_task_output("错误".encode("utf-16")), "错误")
        self.assertEqual(self.module.decode_task_output(b"\xef\xbb\xbf" + "错误".encode("utf-8")), "错误")
        self.assertNotIn("\ufffd", self.module.decode_task_output(b"\xff", "utf-8"))


if __name__ == "__main__":
    unittest.main()
