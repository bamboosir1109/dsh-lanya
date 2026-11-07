"""Isolated filesystem regression tests; never touch the real workspace plan."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "workspace_archive.py"
SPEC = importlib.util.spec_from_file_location("workspace_archive", SCRIPT)
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


class WorkspaceArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lanya-archive-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = archive.workspace_path(self.temporary.name)

    def write(self, name, value=b"sample"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        return path

    def plan(self, *names):
        base = "audit/archive/batch"
        return {"archive_root": base, "manifest": base + "/manifest.json",
                "items": [{"source": name, "destination": base + "/entries/" + name,
                           "status": "approved"} for name in names]}

    def manifest_path(self, plan):
        return self.root / plan["manifest"]

    def test_default_cli_preview_has_no_filesystem_side_effects(self):
        self.write("sample space.txt", b"preview")
        plan = self.plan("sample space.txt")
        self.write("plan.json", json.dumps(plan).encode())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(archive.main(["--workspace", str(self.root), "--plan", "plan.json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "preview")
        self.assertTrue((self.root / "sample space.txt").exists())
        self.assertFalse((self.root / "audit").exists())

    def test_file_directory_unicode_and_empty_directory_round_trip(self):
        file_name = "\u6587\u4ef6.txt"
        self.write(file_name, b"file-data")
        self.write("old tree/nested/data.bin", bytes(range(255)))
        (self.root / "old tree/empty").mkdir()
        plan = self.plan(file_name, "old tree")
        result = archive.apply_plan(self.root, plan)
        self.assertEqual(result["state"], "archived")
        self.assertFalse((self.root / file_name).exists())
        self.assertFalse((self.root / "old tree").exists())
        for entry in result["items"]:
            files = [record for record in entry["snapshot"]["records"] if record["kind"] == "file"]
            self.assertTrue(all(len(record["sha256"]) == 64 for record in files))
        self.assertEqual(archive.restore(self.root, self.manifest_path(plan))["restore_count"], 2)
        self.assertFalse((self.root / file_name).exists())
        restored = archive.restore(self.root, self.manifest_path(plan), apply=True)
        self.assertEqual(restored["state"], "restored")
        self.assertEqual((self.root / file_name).read_bytes(), b"file-data")
        self.assertEqual((self.root / "old tree/nested/data.bin").read_bytes(), bytes(range(255)))
        self.assertTrue((self.root / "old tree/empty").is_dir())
        self.assertTrue(self.manifest_path(plan).exists())
        self.assertEqual(archive.restore(self.root, self.manifest_path(plan))["restore_count"], 0)

    def test_inactive_database_and_reviewed_root_qr_round_trip(self):
        for name in ("snapshot.db", "bili-qr.png"):
            self.write(name)
        plan = self.plan("snapshot.db", "bili-qr.png")
        with self.assertRaises(archive.ArchiveError):
            archive.prepare(self.root, plan)
        plan["items"][0]["database_inactive"] = True
        with self.assertRaises(archive.ArchiveError):
            archive.prepare(self.root, plan)
        plan["items"][1]["reviewed_root_copy"] = True
        result = archive.apply_plan(self.root, plan)
        self.assertTrue(result["items"][0]["database_inactive"])
        self.assertTrue(result["items"][1]["reviewed_root_copy"])
        self.assertEqual(archive.restore(self.root, self.manifest_path(plan), apply=True)["restore_count"], 2)

    def test_windows_executable_filename_stat_round_trip(self):
        self.write("old.cmd", b"@exit /b\r\n")
        plan = self.plan("old.cmd")
        archive.apply_plan(self.root, plan)
        archive.restore(self.root, self.manifest_path(plan), apply=True)
        self.assertEqual((self.root / "old.cmd").read_bytes(), b"@exit /b\r\n")

    def test_traversal_absolute_reserved_and_protected_paths_refused(self):
        for name in ("../escape", "/absolute", "C:/absolute", "CON.txt", "bad.",
                     "README.md", "AGENTS.md", ".git", "state.lock", "state.db-wal"):
            with self.subTest(name=name), self.assertRaises(archive.ArchiveError):
                archive.prepare(self.root, self.plan(name))
        self.write("live.txt")
        plan = self.plan("live.txt")
        plan["protected_roots"] = ["live.txt"]
        with self.assertRaises(archive.ArchiveError):
            archive.prepare(self.root, plan)

    def test_archive_manifest_and_destination_overlap_refused(self):
        self.write("old/data")
        for change in ({"archive_root": "old/archive", "manifest": "old/archive/manifest.json"},
                       {"manifest": "outside.json"}):
            plan = self.plan("old")
            plan.update(change)
            with self.assertRaises(archive.ArchiveError):
                archive.prepare(self.root, plan)
        plan = self.plan("old", "old")
        with self.assertRaises(archive.ArchiveError):
            archive.prepare(self.root, plan)
        plan = self.plan("old")
        plan["items"][0]["destination"] = plan["manifest"] + "/child"
        with self.assertRaises(archive.ArchiveError):
            archive.prepare(self.root, plan)

    def test_later_invalid_parent_refused_before_any_move(self):
        self.write("first")
        self.write("second")
        plan = self.plan("first", "second")
        self.write("audit/archive/batch/blocked")
        plan["items"][1]["destination"] = plan["archive_root"] + "/blocked/second"
        with mock.patch.object(archive, "rename_noreplace") as move:
            with self.assertRaises(archive.ArchiveError):
                archive.apply_plan(self.root, plan)
        move.assert_not_called()
        self.assertTrue((self.root / "first").exists())
        self.assertFalse(self.manifest_path(plan).exists())

    def test_existing_destination_or_manifest_refused(self):
        self.write("old")
        plan = self.plan("old")
        self.write(plan["items"][0]["destination"], b"occupied")
        with self.assertRaises(archive.ArchiveError):
            archive.apply_plan(self.root, plan)
        self.assertEqual((self.root / plan["items"][0]["destination"]).read_bytes(), b"occupied")
        other = self.plan("old")
        other["manifest"] = "audit/archive/batch/existing.json"
        self.write(other["manifest"], b"existing")
        with self.assertRaises(archive.ArchiveError):
            archive.prepare(self.root, other)

    def test_expected_hash_and_size_refused_before_writes(self):
        self.write("old")
        for fields in ({"sha256": "0" * 64}, {"size_bytes": 1234}):
            plan = self.plan("old")
            plan["items"][0].update(fields)
            with self.assertRaises(archive.ArchiveError):
                archive.apply_plan(self.root, plan)
            self.assertFalse((self.root / "audit").exists())

    def test_restore_conflict_checks_whole_batch_before_moving(self):
        self.write("one")
        self.write("two")
        plan = self.plan("one", "two")
        archive.apply_plan(self.root, plan)
        self.write("two", b"new-data")
        with mock.patch.object(archive, "rename_noreplace") as move:
            with self.assertRaises(archive.ArchiveError):
                archive.restore(self.root, self.manifest_path(plan), apply=True)
        move.assert_not_called()
        self.assertFalse((self.root / "one").exists())
        self.assertEqual((self.root / "two").read_bytes(), b"new-data")

    def test_modified_archive_refuses_restore(self):
        self.write("old")
        plan = self.plan("old")
        archive.apply_plan(self.root, plan)
        self.write(plan["items"][0]["destination"], b"tampered")
        with self.assertRaises(archive.ArchiveError):
            archive.restore(self.root, self.manifest_path(plan), apply=True)
        self.assertFalse((self.root / "old").exists())

    def test_interrupted_batch_restores_moved_and_unmoved_entries(self):
        self.write("one")
        self.write("two")
        plan = self.plan("one", "two")
        original = archive.rename_noreplace
        calls = []

        def fail_second(source, target):
            calls.append(source)
            if len(calls) == 2:
                raise OSError("simulated sharing violation")
            original(source, target)

        with mock.patch.object(archive, "rename_noreplace", side_effect=fail_second):
            with self.assertRaises(OSError):
                archive.apply_plan(self.root, plan)
        self.assertEqual(json.loads(self.manifest_path(plan).read_text())["state"], "interrupted")
        self.assertEqual(archive.restore(self.root, self.manifest_path(plan), apply=True)["restore_count"], 1)
        self.assertTrue((self.root / "one").exists())
        self.assertTrue((self.root / "two").exists())

    def test_destination_race_cannot_overwrite(self):
        self.write("old")
        plan = self.plan("old")
        original = archive.rename_noreplace

        def race(source, target):
            target.write_bytes(b"competing-file")
            original(source, target)

        with mock.patch.object(archive, "rename_noreplace", side_effect=race):
            with self.assertRaises(archive.ArchiveError):
                archive.apply_plan(self.root, plan)
        self.assertEqual((self.root / "old").read_bytes(), b"sample")
        self.assertEqual((self.root / plan["items"][0]["destination"]).read_bytes(), b"competing-file")

    def test_apply_rechecks_before_creating_manifest(self):
        self.write("old")
        plan = self.plan("old")
        original = archive.prepare

        def mutate(root, document):
            value = original(root, document)
            self.write("old", b"changed-after-preview")
            return value

        with mock.patch.object(archive, "prepare", side_effect=mutate):
            with self.assertRaises(archive.ArchiveError):
                archive.apply_plan(self.root, plan)
        self.assertFalse((self.root / "audit").exists())

    @unittest.skipUnless(shutil.which("git"), "Git unavailable")
    def test_whole_git_directory_preserves_ignored_untracked_and_dirty_metadata(self):
        repo = self.root / "old-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True, capture_output=True)
        self.write("old-repo/.gitignore", b"ignored.bin\n")
        self.write("old-repo/ignored.bin", b"ignored-data")
        self.write("old-repo/untracked.txt", b"untracked-data")
        plan = self.plan("old-repo")
        result = archive.apply_plan(self.root, plan)
        metadata = result["items"][0]["snapshot"]["git"][0]
        self.assertTrue(metadata["dirty"])
        self.assertIsNone(metadata["head"])
        self.assertTrue(metadata["branch"])
        archive.restore(self.root, self.manifest_path(plan), apply=True)
        self.assertTrue((repo / ".git/config").is_file())
        self.assertEqual((repo / "ignored.bin").read_bytes(), b"ignored-data")
        self.assertEqual((repo / "untracked.txt").read_bytes(), b"untracked-data")

    def create_symlink(self, link, target, directory=False):
        try:
            os.symlink(target, link, target_is_directory=directory)
        except (OSError, NotImplementedError) as error:
            self.skipTest("Symlink creation unavailable: " + type(error).__name__)

    def test_internal_symlink_inventoried_without_following_target(self):
        self.write("outside/data.bin", b"outside-data")
        self.write("old/data.bin", b"inside-data")
        self.create_symlink(self.root / "old/link", self.root / "outside", directory=True)
        plan = self.plan("old")
        result = archive.apply_plan(self.root, plan)
        records = result["items"][0]["snapshot"]["records"]
        self.assertEqual(result["items"][0]["snapshot"]["link_count"], 1)
        self.assertFalse(any(record["path"].startswith("link/") for record in records))
        archive.restore(self.root, self.manifest_path(plan), apply=True)
        self.assertEqual((self.root / "outside/data.bin").read_bytes(), b"outside-data")

    def test_source_link_and_destination_ancestor_link_refused(self):
        self.write("ordinary")
        self.create_symlink(self.root / "linked", self.root / "ordinary")
        with self.assertRaises(archive.ArchiveError):
            archive.prepare(self.root, self.plan("linked"))
        (self.root / "outside").mkdir()
        self.create_symlink(self.root / "audit", self.root / "outside", directory=True)
        with self.assertRaises(archive.ArchiveError):
            archive.apply_plan(self.root, self.plan("ordinary"))
        self.assertTrue((self.root / "ordinary").exists())

    def test_windows_reparse_flag_is_a_link(self):
        info = mock.Mock(st_mode=0o40755, st_file_attributes=0x400)
        self.assertTrue(archive.is_link(info))


if __name__ == "__main__":
    unittest.main()
