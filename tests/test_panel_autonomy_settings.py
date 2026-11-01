"""Real temporary config CAS and inert panel APIs; no model/platform requests."""
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("panel_settings_fixture_base", Path(__file__).with_name("test_panel_autonomy.py"))
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)


class AutonomySettingsTests(unittest.TestCase):
    def setUp(self):
        self.base = fixture.PanelAutonomyTests(); self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.panel, self.root = self.base.module, self.base.root
        import panel_autonomy
        self.helper = panel_autonomy
        self.value = {"dataDirName": "fixture", "decisions": {"mode": "unified"},
                      "mind": {"opportunityMin": 3, "unrelated": "must remain"},
                      "daemon": {"pollSeconds": 60}, "private_marker": "DO NOT EXPORT"}
        self.base.write(self.base.config, self.value)
        self.budget = {"schema": "unified-budget/v1", "availability": "available", "revision": "a"*64,
                       "effective": {"limits": {"autonomy": 1, "private": 0, "group": 0}, "reserve_usd": 0.05},
                       "usage": {"date": "synthetic", "buckets": {}},
                       "enforcement": {"runtime_gate_bound": False, "runtime_owner_verified": False}}
        self.save_budget = mock.Mock(return_value=self.budget)
        self.get_budget = mock.Mock(return_value=self.budget)
        patch = mock.patch.dict(sys.modules, {"budget_runtime": SimpleNamespace(get_snapshot=self.get_budget, save_config=self.save_budget)})
        patch.start(); self.addCleanup(patch.stop)

    def settings(self): return self.base.route("/api/autonomy/settings")[1]["settings"]

    def test_get_is_current_sanitized_and_does_not_create_files(self):
        before = sorted((str(p.relative_to(self.root)), hashlib.sha256(p.read_bytes()).hexdigest())
                        for p in self.root.rglob("*") if p.is_file())
        settings = self.settings()
        self.assertEqual(settings["effective"], {"opportunity_minutes": 3})
        self.assertEqual(settings["writable_fields"], ["opportunity_minutes"])
        self.assertEqual(settings["availability"], "available")
        self.assertTrue(settings["existing_reviews_unchanged"])
        self.assertNotIn("DO NOT EXPORT", json.dumps(settings))
        self.assertNotIn(str(self.base.config), json.dumps(settings))
        self.assertEqual(before, sorted((str(p.relative_to(self.root)), hashlib.sha256(p.read_bytes()).hexdigest())
                         for p in self.root.rglob("*") if p.is_file()))
        self.value["mind"]["opportunityMin"] = 4
        self.base.write(self.base.config, self.value)
        self.assertEqual(self.settings()["effective"]["opportunity_minutes"], 4)

    def test_revision_cas_changes_only_actual_project_interval(self):
        old = self.base.config.stat()
        before = self.settings()
        code, result = self.base.route("/api/autonomy/settings", {"revision": before["revision"], "settings": {"opportunity_minutes": 7.5}})
        self.assertEqual(code, 200, result)
        self.assertEqual(result["settings"]["effective"]["opportunity_minutes"], 7.5)
        self.value["mind"]["opportunityMin"] = 7.5
        self.assertEqual(json.loads(self.base.config.read_text()), self.value)
        self.assertNotEqual(self.base.config.stat().st_ino, old.st_ino)
        self.assertNotEqual(result["settings"]["revision"], before["revision"])
        self.assertEqual(self.base.route("/api/autonomy/settings", {"revision": before["revision"], "settings": {"opportunity_minutes": 8}})[0], 409)

    def test_same_bytes_new_inode_invalidates_revision(self):
        revision = self.settings()["revision"]
        candidate = self.root / "new-config.json"; candidate.write_bytes(self.base.config.read_bytes())
        os.replace(candidate, self.base.config)
        code, result = self.base.route("/api/autonomy/settings", {"revision": revision, "settings": {"opportunity_minutes": 9}})
        self.assertEqual((code, result["code"]), (409, "revision_conflict"))

    def test_exact_fields_types_and_boundaries_reject_without_changes(self):
        original = self.base.config.read_bytes()
        for value in (True, False, "3", 1.9, 60.1, None):
            code, _ = self.base.route("/api/autonomy/settings", {"revision": self.settings()["revision"], "settings": {"opportunity_minutes": value}})
            self.assertEqual(code, 400, value)
        for raw in (b'{"revision":"x","revision":"y"}', b'{"settings":NaN}', b'[]', b'{'):
            self.assertEqual(self.base.route("/api/autonomy/settings", raw=raw)[0], 400)
        code, _ = self.base.route("/api/autonomy/settings", {"revision": self.settings()["revision"], "settings": {"opportunity_minutes": 3, "silence": False}})
        self.assertEqual(code, 400)
        self.assertEqual(self.base.config.read_bytes(), original)

    def test_shared_os_lock_refuses_concurrent_writer(self):
        revision = self.settings()["revision"]
        with self.helper._write_lock(self.base.config):
            code, result = self.base.route("/api/autonomy/settings", {"revision": revision, "settings": {"opportunity_minutes": 5}})
        self.assertEqual((code, result["code"]), (409, "settings_busy"))
        self.assertEqual(json.loads(self.base.config.read_text()), self.value)

    def test_legacy_unknown_and_broken_configuration_never_become_writable(self):
        self.value["decisions"]["mode"] = "legacy"; self.base.write(self.base.config, self.value)
        snapshot = self.settings(); self.assertEqual(snapshot["writable_fields"], [])
        self.assertEqual(self.base.route("/api/autonomy/settings", {"revision": snapshot["revision"], "settings": {"opportunity_minutes": 5}})[0], 409)
        self.base.config.write_text("broken PRIVATE BODY", encoding="utf-8")
        snapshot = self.settings()
        self.assertEqual(snapshot["availability"], "error")
        self.assertIsNone(snapshot["effective"]["opportunity_minutes"])
        self.assertNotIn("PRIVATE BODY", json.dumps(snapshot))

    def test_group_observer_reads_real_priority_and_marks_startup_not_running(self):
        old = self.base.data / "dafeiyu-tunables.json"; modern = self.base.data / "lanya-tunables.json"
        self.base.write(modern, {"QuietMinutes": 7, "PollSeconds": 20})
        self.base.write(old, {"QuietMinutes": 120, "PollSeconds": "45", "GroupId": "MUST NOT EXPORT"})
        group = self.settings()["group_observation"]
        self.assertEqual(group["settings"], {"quiet_minutes": 120, "poll_seconds": 45})
        self.assertEqual(group["source"], "dafeiyu-tunables")
        self.assertFalse(group["running_values_verified"]); self.assertTrue(group["read_only"])
        self.assertNotIn("MUST NOT EXPORT", json.dumps(group))
        old.write_text("broken", encoding="utf-8")
        self.assertEqual(self.settings()["group_observation"]["availability"], "error")
        old.unlink()
        self.assertEqual(self.settings()["group_observation"]["settings"]["quiet_minutes"], 7)

    def test_hardlink_config_and_reparse_components_are_refused(self):
        link = self.root / "config-link.json"
        os.link(self.base.config, link)
        try:
            self.assertEqual(self.settings()["availability"], "error")
        finally:
            link.unlink()
        with mock.patch.object(self.helper.Path, "lstat", return_value=SimpleNamespace(st_mode=stat_mode(), st_file_attributes=0x400)):
            self.assertEqual(self.settings()["availability"], "error")

    def test_live_and_budget_routes_share_actual_runtime_module(self):
        self.assertEqual(self.base.route("/api/budget")[1]["budget"], self.budget)
        live = self.base.route("/api/live")[1]["live"]
        self.assertEqual(live["budget"], self.budget); self.assertNotIn("money", live)
        request = {"revision": "a"*64, "settings": {"limits": {"autonomy": 2, "private": 0, "group": 0}, "reserve_usd": 0.05}}
        code, result = self.base.route("/api/budget", request)
        self.assertEqual(code, 200); self.assertEqual(result["budget"], self.budget)
        self.save_budget.assert_called_once_with(request["settings"], request["revision"])
        self.get_budget.side_effect = OSError("PRIVATE PATH SQL BODY")
        out = self.base.route("/api/budget")[1]
        self.assertFalse(out["ok"]); self.assertEqual(out["budget"]["availability"], "error")
        self.assertIsNone(out["budget"]["usage"]); self.assertNotIn("PRIVATE", json.dumps(out))
        import sqlite3
        self.get_budget.side_effect = sqlite3.DatabaseError("PRIVATE SQL BODY")
        self.assertEqual(self.base.route("/api/budget")[1]["budget"]["availability"], "error")

    def test_gate_bound_requires_current_verified_owner_and_loaded_file_hashes(self):
        profile = self.root / "profile"
        package = profile / "node_modules/dsh-onebot-qq/lib"; package.mkdir(parents=True)
        adapter = package / "lanya-policy-adapter.js"; adapter.write_text("// fixture adapter")
        helper = self.root / "budget-runtime-fixture.py"; helper.write_text("# fixture helper")
        config = package / "lanya-policy.config.json"
        self.base.write(config, {"budgetRuntimeScript": str(helper)})
        self.panel.PROFILE = str(profile)
        gate = {"at": 1000000, "pid": 42, "adapter_source_sha256": hashlib.sha256(adapter.read_bytes()).hexdigest(),
                "helper_source_sha256": hashlib.sha256(helper.read_bytes()).hexdigest()}
        backend = sys.modules["budget_runtime"]
        backend.__file__ = str(helper); backend.gate_evidence = lambda: dict(gate)
        self.budget["enforcement"].update(runtime_gate_bound=True, gate_pid=42, gate_last_seen=1000000)
        out = self.base.route("/api/budget")[1]["budget"]
        self.assertTrue(out["enforcement"]["runtime_gate_bound"])
        self.assertTrue(out["enforcement"]["runtime_owner_verified"])
        self.assertNotIn("gate_pid", out["enforcement"])
        previous = os.getcwd()
        try:
            os.chdir(helper.parent)
            self.base.write(config, {"budgetRuntimeScript": helper.name})
            self.assertFalse(self.base.route("/api/budget")[1]["budget"]["enforcement"]["runtime_gate_bound"])
        finally:
            os.chdir(previous)
            self.base.write(config, {"budgetRuntimeScript": str(helper)})
        for change in ({"pid": 43}, {"at": 999999}, {"adapter_source_sha256": "0"*64}, {"helper_source_sha256": "0"*64}):
            original = dict(gate); gate.update(change)
            self.assertFalse(self.base.route("/api/budget")[1]["budget"]["enforcement"]["runtime_gate_bound"])
            gate.clear(); gate.update(original)
        self.base.owner_mock.return_value = False
        self.assertFalse(self.base.route("/api/budget")[1]["budget"]["enforcement"]["runtime_gate_bound"])
        self.base.owner_mock.return_value = True
        import sqlite3
        with sqlite3.connect(str(self.base.lease)) as db:
            db.execute("UPDATE consumer SET pid=43")
        self.assertFalse(self.base.route("/api/budget")[1]["budget"]["enforcement"]["runtime_gate_bound"])

    def test_budget_route_real_module_pure_read_and_config_cas(self):
        source = Path(__file__).resolve().parents[1] / "scripts/budget_runtime.py"
        spec = importlib.util.spec_from_file_location("panel_budget_actual_fixture", source)
        actual = importlib.util.module_from_spec(spec); spec.loader.exec_module(actual)
        with mock.patch.dict(sys.modules, {"budget_runtime": actual}), \
                mock.patch.object(actual.time, "time", return_value=1793523600):
            path = self.base.data / "autonomy-budget.sqlite"
            config = self.base.data / "autonomy-budget.json"
            snapshot = self.base.route("/api/budget")[1]["budget"]
            self.assertFalse(path.exists()); self.assertFalse(config.exists())
            self.assertFalse(snapshot["enforcement"]["runtime_gate_bound"])
            settings = {"limits": {"autonomy": 2.5, "private": 0, "group": 0}, "reserve_usd": 0.03}
            code, result = self.base.route("/api/budget", {"revision": snapshot["revision"], "settings": settings})
            self.assertEqual(code, 200, result)
            self.assertEqual(result["budget"]["effective"]["limits"], settings["limits"])
            self.assertTrue(path.is_file()); self.assertTrue(config.is_file())
            self.assertEqual(self.base.route("/api/budget", {"revision": snapshot["revision"], "settings": settings})[0], 409)

    def test_retired_controls_cannot_write_legacy_files(self):
        paths = [self.base.data / name for name in ("activities.json", "lanya-tunables.json", "budget.json")]
        for path in paths: path.write_text('{"preserve":true}')
        before = [path.read_bytes() for path in paths]
        for route in ("/api/activities", "/api/activities/add", "/api/activities/delete", "/api/tunables", "/api/budget/topup"):
            self.assertEqual(self.base.route(route, {"amount": 1, "enabled": {"fixture": True}})[0], 410)
        self.assertEqual(self.base.route("/api/budget", {"budget": {"limit": 9}})[0], 410)
        self.assertEqual(before, [path.read_bytes() for path in paths])
        self.assertTrue(self.base.route("/api/activities")[1]["data"]["retired_controls"])
        self.assertTrue(self.base.route("/api/tunables")[1]["retired"])
        self.save_budget.assert_not_called()

    def test_cross_origin_and_duplicate_budget_fields_are_rejected_before_backend(self):
        handler = object.__new__(self.panel.Handler)
        handler.headers = {"Origin": "https://foreign.invalid", "content-length": "2"}
        with self.assertRaises(self.helper.SettingsError) as caught:
            handler._settings_body()
        self.assertEqual(caught.exception.http_status, 403)
        self.assertEqual(self.base.route("/api/budget", raw=b'{"revision":"x","revision":"x","settings":{}}')[0], 400)
        self.save_budget.assert_not_called()


def stat_mode():
    import stat
    return stat.S_IFREG | 0o600


if __name__ == "__main__": unittest.main()
