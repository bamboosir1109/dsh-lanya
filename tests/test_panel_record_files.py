import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("panel_record_files", REPO / "scripts/panel_record_files.py")
files = importlib.util.module_from_spec(spec)
spec.loader.exec_module(files)


class WorkEntries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "made"
        self.root.mkdir()

    def put(self, relative, body=b"real fixture"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def test_nested_game_entry_not_dependencies_and_flat_compatibility(self):
        self.put("games/2048/index.html")
        self.put("games/2048/app.js")
        self.put("games/2048/assets/tile.png")
        self.put("drawing.png")
        self.put("helper.js")
        self.put("lanya-state.json")
        result = files.works_snapshot(self.root, excluded=lambda name: name == "lanya-state.json")
        rows = {row["name"]: row for row in result["files"]}
        self.assertEqual(set(rows), {"games/2048/index.html", "drawing.png", "helper.js"})
        self.assertEqual(rows["games/2048/index.html"]["entry_type"], "directory_entry")
        self.assertEqual(rows["games/2048/index.html"]["url"], "./games/2048/index.html")
        self.assertFalse(result["truncated"])

    def test_unicode_spaces_and_delimiters_have_safe_relative_url(self):
        self.put("a #&%/index.html")
        row = files.works_snapshot(self.root)["files"][0]
        self.assertEqual(row["url"], "./a%20%23%26%25/index.html")
        self.assertFalse(row["url"].startswith("/"))

    def test_reparse_child_is_not_followed(self):
        inside = self.put("fake/index.html")
        original = Path.lstat
        def fake_stat(path):
            result = original(path)
            if path == inside.parent:
                values = {name: getattr(result, name) for name in dir(result) if name.startswith("st_")}
                values["st_file_attributes"] = 0x400
                return type("Reparse", (), values)()
            return result
        with mock.patch.object(Path, "lstat", fake_stat):
            result = files.works_snapshot(self.root)
        self.assertEqual(result["files"], [])
        self.assertEqual(result["skipped_unsafe_or_unreadable"], 1)

    def test_actual_hardlink_is_not_exposed(self):
        outside = Path(self.temp.name) / "outside.html"
        outside.write_bytes(b"outside")
        os.link(outside, self.root / "linked.html")
        result = files.works_snapshot(self.root)
        self.assertEqual(result["files"], [])
        self.assertEqual(result["skipped_unsafe_or_unreadable"], 1)

    def test_linked_root_and_non_directory_are_rejected(self):
        plain = self.put("plain.html")
        with self.assertRaises(ValueError):
            files.works_snapshot(plain)
        original = Path.lstat
        def fake_root(path):
            value = original(path)
            if path == self.root:
                values = {name: getattr(value, name) for name in dir(value) if name.startswith("st_")}
                values["st_file_attributes"] = 0x400
                return type("Reparse", (), values)()
            return value
        with mock.patch.object(Path, "lstat", fake_root), self.assertRaises(ValueError):
            files.works_snapshot(self.root)

    def test_depth_and_entry_limits_report_truncation(self):
        self.put("deep/more/index.html")
        self.assertTrue(files.works_snapshot(self.root, max_depth=1)["truncated"])
        self.put("a.html")
        self.put("b.html")
        result = files.works_snapshot(self.root, max_entries=1)
        self.assertEqual(len(result["files"]), 1)
        self.assertTrue(result["truncated"])
        for bad in (0, True, 1.0):
            with self.assertRaises(ValueError):
                files.works_snapshot(self.root, max_nodes=bad)

    def test_hidden_or_excluded_directories_are_not_browsed(self):
        self.put(".private/index.html")
        self.put("lanya-panel-assets/index.html")
        self.assertEqual(files.works_snapshot(self.root,
            excluded=lambda name: name.startswith("lanya-panel"))["files"], [])


if __name__ == "__main__":
    unittest.main()
