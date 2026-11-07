"""Only generic program copies under temporary roots; no live store or host."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import patch_memento_recall_scope as patcher
import patch_memento_history as native
import patch_memento_visibility as visibility


class RecallScopePatchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="memento-recall-scope-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.plugin = self.root / "plugin"
        shutil.copytree(ROOT / "tests/fixtures/memento_native_0_5_23", self.plugin)
        visibility.patch_package(self.plugin, apply=True, backup_root=self.root / "visibility")
        p = native.patch(self.plugin)
        native.patch(self.plugin, apply=True, host_stopped=True, expected_fingerprint=p["fingerprint"], backup_dir=self.root / "history")

    def tree(self):
        return {p.relative_to(self.plugin).as_posix(): p.read_bytes() for p in self.plugin.rglob("*") if p.is_file()}

    def apply(self, **changes):
        options = dict(apply=True, host_stopped=True, expected_fingerprint=patcher.patch(self.plugin)["fingerprint"], backup_dir=self.root / "scope")
        options.update(changes)
        return patcher.patch(self.plugin, **options)

    def reverse(self, manifest, **changes):
        options = dict(apply=True, host_stopped=True, expected_fingerprint=patcher.restore(self.plugin, manifest)["fingerprint"])
        options.update(changes)
        return patcher.restore(self.plugin, manifest, **options)

    def test_default_preview_is_read_only(self):
        before = self.tree()
        result = patcher.patch(self.plugin)
        self.assertEqual(result["status"], "patch_needed")
        self.assertFalse(result["applied"])
        self.assertEqual(before, self.tree())
        self.assertFalse((self.root / "scope").exists())

    def test_apply_and_reverse_only_new_layer(self):
        before = self.tree()
        result = self.apply()
        manifest = Path(result["backup"]) / "patch.json"
        self.assertEqual((manifest.parent / patcher.INDEX).read_bytes(), before[patcher.INDEX])
        self.assertEqual(patcher.patch(self.plugin)["status"], "already_patched")
        self.assertFalse(self.apply()["applied"])
        self.assertFalse(patcher.restore(self.plugin, manifest)["applied"])
        self.reverse(manifest)
        self.assertEqual(before, self.tree())

    def test_literal_stopped_host_and_fresh_fingerprint_required(self):
        before = self.tree()
        for value in (False, "false", 1, None):
            with self.subTest(value=value), self.assertRaises(patcher.PatchError): self.apply(host_stopped=value)
        with self.assertRaises(patcher.PatchError): self.apply(expected_fingerprint="stale")
        with self.assertRaises(patcher.PatchError): self.apply(backup_dir=ROOT / "refused-scope-backup")
        self.assertEqual(before, self.tree())

    def test_source_and_dependencies_and_unowned_module_refuse(self):
        for name in [patcher.INDEX, "lib/extract.mjs", "lib/store.mjs", "lib/protocol.mjs", "lib/lanya-memento-history.mjs", "package.json"]:
            target = self.plugin / name
            raw = target.read_bytes()
            target.write_bytes(raw + b"\n// changed\n")
            with self.subTest(name=name), self.assertRaises(Exception): patcher.patch(self.plugin)
            target.write_bytes(raw)
        (self.plugin / patcher.MODULE).write_bytes(b"unowned")
        with self.assertRaises(patcher.PatchError): patcher.patch(self.plugin)

    def test_before_inode_change_invalidates_fingerprint(self):
        preview = patcher.patch(self.plugin)
        fresh = self.root / "new-index.mjs"
        fresh.write_bytes((self.plugin / patcher.INDEX).read_bytes())
        os.replace(fresh, self.plugin / patcher.INDEX)
        with self.assertRaises(patcher.PatchError): self.apply(expected_fingerprint=preview["fingerprint"])

    def test_shared_lock_excludes_apply(self):
        preview = patcher.patch(self.plugin)
        with visibility.package_lock(self.plugin):
            result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/patch_memento_recall_scope.py"),
                "--package-root", str(self.plugin), "--apply", "--host-stopped", "--expected-fingerprint", preview["fingerprint"],
                "--backup-dir", str(self.root / "blocked")], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "blocked").exists())

    def test_failure_after_module_rolls_back_exact_changes(self):
        before = self.tree()
        replace = patcher.os.replace
        def fail(source, target):
            if Path(target) == self.plugin / patcher.INDEX: raise OSError("synthetic interruption")
            return replace(source, target)
        with mock.patch.object(patcher.os, "replace", side_effect=fail), self.assertRaises(OSError): self.apply()
        self.assertEqual(before, self.tree())
        failure = next((self.root / "scope").glob("*/failure.json"))
        self.assertEqual(json.loads(failure.read_text())["rollback_errors"], [])

    def test_failure_after_index_compensates_without_touching_hardlink_peer(self):
        before = self.tree()
        peer = self.root / "peer-index.mjs"
        os.link(self.plugin / patcher.INDEX, peer)
        write = patcher.write_new
        def fail(path, raw):
            if Path(path) == self.plugin / patcher.MARKER: raise OSError("synthetic marker failure")
            return write(path, raw)
        with mock.patch.object(patcher, "write_new", side_effect=fail), self.assertRaises(OSError): self.apply()
        self.assertEqual(before, self.tree())
        self.assertEqual(peer.read_bytes(), before[patcher.INDEX])

    def test_full_apply_and_restore_break_hardlinks(self):
        original = (self.plugin / patcher.INDEX).read_bytes()
        peer = self.root / "original-peer.mjs"; os.link(self.plugin / patcher.INDEX, peer)
        result = self.apply(); manifest = Path(result["backup"]) / "patch.json"
        patched = (self.plugin / patcher.INDEX).read_bytes()
        after_peer = self.root / "patched-peer.mjs"; os.link(self.plugin / patcher.INDEX, after_peer)
        self.reverse(manifest)
        self.assertEqual(peer.read_bytes(), original)
        self.assertEqual(after_peer.read_bytes(), patched)

    def test_later_edits_prevent_any_restore(self):
        result = self.apply(); manifest = Path(result["backup"]) / "patch.json"
        for name in [patcher.INDEX, patcher.MODULE, patcher.MARKER]:
            target = self.plugin / name; raw = target.read_bytes(); target.write_bytes(raw + b"\nlater\n")
            before = self.tree()
            with self.subTest(name=name), self.assertRaises(Exception): patcher.restore(self.plugin, manifest)
            self.assertEqual(before, self.tree()); target.write_bytes(raw)

    def test_original_or_manifest_tamper_and_stale_restore_refuse(self):
        result = self.apply(); manifest = Path(result["backup"]) / "patch.json"
        for target in [manifest, manifest.parent / patcher.INDEX, manifest.parent / "generated.mjs"]:
            raw = target.read_bytes(); target.write_bytes(raw + b"tamper")
            with self.assertRaises(Exception): patcher.restore(self.plugin, manifest)
            target.write_bytes(raw)
        preview = patcher.restore(self.plugin, manifest)
        fresh = self.root / "replacement.mjs"; fresh.write_bytes((self.plugin / patcher.INDEX).read_bytes()); os.replace(fresh, self.plugin / patcher.INDEX)
        with self.assertRaises(patcher.PatchError): self.reverse(manifest, expected_fingerprint=preview["fingerprint"])
        for value in (False, "false", 1):
            with self.subTest(value=value), self.assertRaises(patcher.PatchError): self.reverse(manifest, host_stopped=value)

    def test_interrupted_restore_resumes_only_exact_before_after(self):
        before = self.tree(); result = self.apply(); manifest = Path(result["backup"]) / "patch.json"
        unlink = Path.unlink
        def fail(path, *args, **kwargs):
            if path == self.plugin / patcher.MODULE: raise OSError("synthetic unlink failure")
            return unlink(path, *args, **kwargs)
        with mock.patch.object(Path, "unlink", fail), self.assertRaises(OSError): self.reverse(manifest)
        self.assertEqual((self.plugin / patcher.INDEX).read_bytes(), before[patcher.INDEX])
        self.reverse(manifest)
        self.assertEqual(before, self.tree())

    def test_hardlinked_journal_cannot_append_to_another_file(self):
        result = self.apply(); manifest = Path(result["backup"]) / "patch.json"
        target = manifest.parent / "journal.jsonl"
        target.unlink()
        peer = self.root / "unrelated-file.txt"; peer.write_bytes(b"synthetic keep unchanged")
        os.link(peer, target)
        before = self.tree()
        with self.assertRaises(patcher.PatchError): self.reverse(manifest)
        self.assertEqual(before, self.tree())
        self.assertEqual(peer.read_bytes(), b"synthetic keep unchanged")

    def test_prior_native_marker_cannot_bless_modified_dependency(self):
        target = self.plugin / "lib/store.mjs"
        target.write_bytes(target.read_bytes() + b"\n// synthetic altered code\n")
        marker = self.plugin / native.MARKER
        record = json.loads(marker.read_text())
        record["after"]["lib/store.mjs"] = patcher.sha256(target.read_bytes())
        marker.write_text(json.dumps(record, indent=2) + "\n")
        # This layer pins actual reviewed program bytes independently of the
        # older marker's declarations, without changing that older protocol.
        with self.assertRaises(patcher.PatchError): patcher.patch(self.plugin)

    def test_link_or_reparse_metadata_is_refused_without_program_changes(self):
        target = self.plugin / patcher.INDEX
        before = self.tree(); lstat = Path.lstat
        for flags, mode in [(0x400, target.lstat().st_mode), (0, 0o120777)]:
            def link_info(path, *args, **kwargs):
                if path == target: return types.SimpleNamespace(st_mode=mode, st_file_attributes=flags)
                return lstat(path, *args, **kwargs)
            with self.subTest(flags=flags), mock.patch.object(Path, "lstat", link_info), self.assertRaises(patcher.PatchError):
                patcher.patch(self.plugin)
            self.assertEqual(before, self.tree())

    def test_later_index_change_blocks_whole_compensation_without_deleting_helper(self):
        write = patcher.write_new
        def fail(path, raw):
            if Path(path) == self.plugin / patcher.MARKER:
                target = self.plugin / patcher.INDEX
                target.write_bytes(target.read_bytes() + b"\n// synthetic later operator edit\n")
                raise OSError("synthetic failure after a later edit")
            return write(path, raw)
        with mock.patch.object(patcher, "write_new", side_effect=fail), self.assertRaises(OSError): self.apply()
        self.assertTrue((self.plugin / patcher.MODULE).exists())
        self.assertTrue((self.plugin / patcher.INDEX).read_bytes().endswith(b"synthetic later operator edit\n"))
        manifest = next((self.root / "scope").glob("*/patch.json"))
        with self.assertRaises(patcher.PatchError): patcher.restore(self.plugin, manifest)
        self.assertTrue(json.loads((manifest.parent / "failure.json").read_text())["rollback_errors"])


if __name__ == "__main__": unittest.main()
