"""Actual temporary settings/budget CAS; no production config or services."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("security_noop_settings", SCRIPTS / "panel_autonomy.py")
settings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(settings)


@unittest.skipUnless(os.name == "nt", "Windows security descriptors required")
class SecurityNoopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="panel-security-noop-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "project.json"
        self.value = {"dataDirName": "fixture", "decisions": {"mode": "unified"},
                      "mind": {"opportunityMin": 3}, "unrelated": "must remain"}
        self.config.write_text(json.dumps(self.value), encoding="utf-8")
        environment = mock.patch.dict(os.environ, {"LANYA_PROJECT_CONFIG": str(self.config),
                                                   "LANYA_WORKDIR": str(self.root)})
        environment.start(); self.addCleanup(environment.stop)
        project = settings.project_conf
        old = project._CACHE, project._CONFIG_PATH
        self.addCleanup(setattr, project, "_CACHE", old[0])
        self.addCleanup(setattr, project, "_CONFIG_PATH", old[1])
        project.load(reload=True)

    def test_full_same_parent_descriptor_noop_preserves_both_real_files(self):
        source, target = self.root / "source.tmp", self.root / "candidate.tmp"
        source.write_bytes(b"original-body")
        target.write_bytes(b"different-candidate-body")
        before = settings._record(source)[0], settings._record(target)[0]
        self.assertEqual(settings._security(source), settings._security(target))
        with mock.patch.object(settings, "_security", wraps=settings._security) as reads:
            settings._copy_security(source, target)
        self.assertEqual(reads.call_args_list, [mock.call(source), mock.call(target),
                                               mock.call(source), mock.call(target)])
        self.assertEqual((settings._record(source)[0], settings._record(target)[0]), before)

    def test_actual_autonomy_save_uses_new_inode_preserves_sd_and_rejects_stale_revision(self):
        before, _ = settings._record(self.config)
        security = settings._security(self.config)
        initial = settings.get_settings()
        result = settings.save_settings({"revision": initial["revision"], "settings": {"opportunity_minutes": 7.5}})
        after, _ = settings._record(self.config)
        self.assertEqual(result["effective"]["opportunity_minutes"], 7.5)
        self.assertNotEqual(before["signature"][1], after["signature"][1])
        self.assertEqual(settings._security(self.config), security)
        self.value["mind"]["opportunityMin"] = 7.5
        self.assertEqual(json.loads(self.config.read_text()), self.value)
        with self.assertRaises(settings.SettingsError) as error:
            settings.save_settings({"revision": initial["revision"], "settings": {"opportunity_minutes": 9}})
        self.assertEqual(error.exception.http_status, 409)
        self.assertEqual(settings._record(self.config)[0], after)

    def test_actual_budget_update_uses_the_same_copier_and_preserves_descriptor(self):
        with mock.patch.dict(sys.modules, {"panel_autonomy": settings}):
            spec = importlib.util.spec_from_file_location("security_noop_budget", SCRIPTS / "budget_runtime.py")
            budget = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(budget)
        first = budget.get_snapshot()
        desired = {"limits": {"autonomy": 1, "private": 0, "group": 0}, "reserve_usd": 0.05}
        first = budget.save_config(desired, first["revision"])
        path, _ = budget._paths()
        before, _ = settings._record(path)
        security = settings._security(path)
        desired["limits"]["autonomy"] = 2.5
        self.assertIs(budget._copy_security, settings._copy_security)
        result = budget.save_config(desired, first["revision"])
        self.assertEqual(result["effective"]["limits"]["autonomy"], 2.5)
        self.assertNotEqual(settings._record(path)[0]["signature"][1], before["signature"][1])
        self.assertEqual(settings._security(path), security)
        with self.assertRaises(budget.BudgetError) as error:
            budget.save_config(desired, first["revision"])
        self.assertEqual(error.exception.http_status, 409)

    def test_equal_first_read_then_source_or_target_sd_drift_is_refused(self):
        source, target = self.root / "source.tmp", self.root / "candidate.tmp"
        source.write_bytes(b"source"); target.write_bytes(b"target")
        descriptor = settings._security(source)
        for values in ([descriptor, descriptor, descriptor + b"changed"],
                       [descriptor, descriptor, descriptor, descriptor + b"changed"]):
            with self.subTest(reads=len(values)), mock.patch.object(settings, "_security", side_effect=values):
                with self.assertRaises(settings.SettingsError):
                    settings._copy_security(source, target)
        self.assertEqual(source.read_bytes(), b"source")
        self.assertEqual(target.read_bytes(), b"target")

    def test_different_full_descriptor_still_enters_strict_windows_copy(self):
        import ctypes
        source, target = self.root / "source.tmp", self.root / "candidate.tmp"
        source.write_bytes(b"source"); target.write_bytes(b"target")
        descriptor = settings._security(source)
        with mock.patch.object(settings, "_security", side_effect=[descriptor, descriptor + b"different"]), \
                mock.patch.object(ctypes, "WinDLL", side_effect=RuntimeError("strict copier reached")) as strict:
            with self.assertRaisesRegex(RuntimeError, "strict copier reached"):
                settings._copy_security(source, target)
        strict.assert_called_once_with("advapi32", use_last_error=True)
        self.assertEqual(source.read_bytes(), b"source")
        self.assertEqual(target.read_bytes(), b"target")


if __name__ == "__main__":
    unittest.main()
