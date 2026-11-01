import contextlib
import datetime
import importlib.abc
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]


class SnapshotPublishing(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.made = self.root / "made"
        self.made.mkdir()
        conf = types.ModuleType("project_conf")
        conf.work_dir = lambda: str(self.root)
        conf.data_dir = lambda: str(self.root)
        self.panel = types.ModuleType("lps")
        self.panel.BUDGET = str(self.root / "budget.json")
        self.panel.CONFIG = str(self.root / "config.json")
        self.panel.TUNABLES = str(self.root / "tunables.json")
        self.panel.MEMECFG = str(self.root / "memes-config.json")
        self.panel.FILE_DIRS = {"made": str(self.made)}
        self.panel.is_panel_artifact = lambda name: name.startswith("lanya-")
        self.panel.meme_groups_with_tags = lambda: []
        self.panel.tools_snapshot = lambda: []
        self.panel.reminders_snapshot = lambda: []
        self.panel.scheduled_tasks = lambda: []
        self.panel.memory_overview = lambda: {"entries": []}
        self.panel.bili_snapshot = lambda: {"records": []}
        self.panel.unified_budget_snapshot = lambda: {
            "schema": "unified-budget/v1", "availability": "not_started",
            "usage": {"coverage_start": None, "counts_complete": False},
            "effective": {"currency": "USD"}}
        self.panel.list_dir_files = lambda *args, **kwargs: []
        for path in (self.panel.BUDGET, self.panel.CONFIG, self.panel.TUNABLES, self.panel.MEMECFG):
            Path(path).write_text('{}', encoding='utf-8')
        panel = self.panel
        class Loader(importlib.abc.Loader):
            def create_module(self, spec): return panel
            def exec_module(self, module): pass
        spec = importlib.util.spec_from_loader("lps", Loader())
        self.module = types.ModuleType("isolated_snapshots")
        self.module.__dict__["__file__"] = str(REPO / "server/make_snapshots.py")
        self.module.__dict__["__name__"] = "isolated_snapshots"
        # Execute the actual module with a synthetic panel/config; never import
        # the installed server or issue any real platform/process request.
        with mock.patch.dict(sys.modules, {"project_conf": conf}), \
                mock.patch("importlib.util.spec_from_file_location", return_value=spec):
            exec(compile((REPO / "server/make_snapshots.py").read_bytes(), self.module.__file__, "exec"), self.module.__dict__)

    def test_schema_utc_timestamp_and_input_not_mutated(self):
        original = {"ok": True, "budget": {"spent": 0}}
        self.assertTrue(self.module.dump("budget", original))
        result = json.loads((self.made / "lanya-budget.json").read_bytes())
        self.assertEqual(result["_snapshot"]["schema"], 1)
        stamp = result["_snapshot"]["generated_at"]
        self.assertTrue(stamp.endswith("Z"))
        parsed = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        self.assertEqual(parsed.utcoffset(), datetime.timedelta(0))
        self.assertNotIn("_snapshot", original)

    def test_explicit_source_error_and_invalid_name_never_replace(self):
        dest = self.made / "lanya-budget.json"
        dest.write_bytes(b'{"old":true}')
        for value in ({"ok": False}, {"ok": 1}, [], None):
            with self.assertRaises(ValueError):
                self.module.dump("budget", value)
        for bad in ("../budget", "x/y", "", "x\\y"):
            with self.assertRaises(ValueError): self.module.dump(bad, {"ok": True})
        self.assertEqual(dest.read_bytes(), b'{"old":true}')

    def test_atomic_publication_keeps_old_complete_json_until_replace(self):
        dest = self.made / "lanya-budget.json"
        dest.write_bytes(b'{"old":true}')
        original = os.replace
        seen = []
        def checked(source, target):
            seen.append(True)
            self.assertEqual(Path(source).parent, self.made)
            self.assertEqual(dest.read_bytes(), b'{"old":true}')
            self.assertEqual(json.loads(Path(source).read_bytes())["budget"], {"spent": 3})
            original(source, target)
        with mock.patch.object(self.module.os, "replace", checked):
            self.assertTrue(self.module.dump("budget", {"ok": True, "budget": {"spent": 3}}))
        self.assertEqual(len(seen), 1)
        self.assertEqual(json.loads(dest.read_bytes())["budget"]["spent"], 3)
        self.assertEqual(list(self.made.glob(".lanya-snapshot-*")), [])

    def test_replace_failure_and_bad_serialization_preserve_old_timestamp(self):
        self.module.dump("budget", {"ok": True, "budget": {}})
        dest = self.made / "lanya-budget.json"
        before = dest.read_bytes()
        with mock.patch.object(self.module.os, "replace", side_effect=OSError("PRIVATE detail")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(self.module.dump("budget", {"ok": True, "budget": {"spent": 8}}))
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertEqual(dest.read_bytes(), before)
        with self.assertRaises(ValueError): self.module.dump("budget", {"ok": True, "nan": float("nan")})
        self.assertEqual(dest.read_bytes(), before)
        self.assertEqual(list(self.made.glob(".lanya-snapshot-*")), [])

    def test_failed_source_does_not_refresh_last_success_and_works_is_generated(self):
        self.module.dump("budget", {"ok": True, "budget": {"spent": 4}})
        dest = self.made / "lanya-budget.json"
        before = dest.read_bytes()
        self.panel.unified_budget_snapshot = lambda: {"availability": "error"}
        game = self.made / "games/fixture/index.html"
        game.parent.mkdir(parents=True)
        game.write_bytes(b"fixture")
        with contextlib.redirect_stdout(io.StringIO()):
            self.module.main()
        self.assertEqual(dest.read_bytes(), before)
        works = json.loads((self.made / "lanya-works.json").read_bytes())
        self.assertIn("games/fixture/index.html", [row["name"] for row in works["files"]])
        self.assertEqual(works["_snapshot"]["schema"], 1)

    def test_main_uses_unified_budget_and_never_relabels_legacy_zero_as_new_usage(self):
        Path(self.panel.BUDGET).write_text('{INVALID', encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()):
            self.module.main()
        result = json.loads((self.made / "lanya-budget.json").read_bytes())
        self.assertTrue(result["ok"])
        self.assertEqual(result["budget"]["schema"], "unified-budget/v1")
        self.assertEqual(result["budget"]["availability"], "not_started")
        self.assertIsNone(result["budget"]["usage"]["coverage_start"])
        self.assertFalse(result["budget"]["usage"]["counts_complete"])
        self.assertEqual(result["budget"]["effective"]["currency"], "USD")
        self.assertNotIn("spent", result["budget"])

    def test_memory_availability_error_keeps_last_success_bytes_and_time(self):
        self.module.dump("memory", {"ok": True, "memory": {
            "entries": [], "counts": {"active": 0},
            "availability": {"status": "available", "counts_complete": True}}})
        dest = self.made / "lanya-memory.json"
        before = dest.read_bytes()
        stamp = json.loads(before)["_snapshot"]["generated_at"]
        self.panel.memory_overview = lambda: {"entries": [], "counts": {"active": None},
            "availability": {"status": "error", "counts_complete": False}}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.module.main()
        self.assertEqual(dest.read_bytes(), before)
        self.assertEqual(json.loads(dest.read_bytes())["_snapshot"]["generated_at"], stamp)
        self.assertIn("snapshot_failed memory ValueError", output.getvalue())
        self.assertEqual(list(self.made.glob(".lanya-snapshot-*")), [])


if __name__ == "__main__":
    unittest.main()
