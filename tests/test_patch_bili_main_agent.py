"""Private temporary plugins only; no real Bilibili/host/LLM calls."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("patch_bili_main_agent", ROOT / "scripts/patch_bili_main_agent.py")
patcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(patcher)


class BiliPatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lanya-bili-patch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.plugin = self.root / "plugin"
        shutil.copytree(ROOT / "tests/fixtures/bili_main", self.plugin)
        self.project = self.root / "private/project.json"
        self.ledger = self.root / "private/bili-actions.sqlite"
        self.project.parent.mkdir()
        self.project.write_text('{"decisions":{"mode":"unified"}}', encoding="utf-8")

    def tree(self):
        return {file.relative_to(self.plugin).as_posix(): file.read_bytes()
                for file in self.plugin.rglob("*") if file.is_file()}

    def preview(self):
        return patcher.patch(self.plugin, project_config=self.project, ledger_file=self.ledger)

    def apply(self, **options):
        return patcher.patch(self.plugin, project_config=self.project, ledger_file=self.ledger, apply=True,
                             host_stopped=True, expected_fingerprint=self.preview()["fingerprint"],
                             backup_dir=self.root / "backups", **options)

    def restored(self, result, **options):
        manifest = Path(result["backup"]) / "patch.json"
        preview = patcher.restore(self.plugin, manifest)
        return patcher.restore(self.plugin, manifest, apply=True, host_stopped=True,
                               expected_fingerprint=preview["fingerprint"], **options)

    def test_preview_never_creates_ledger_or_backup(self):
        before = self.tree()
        result = self.preview()
        self.assertEqual(result["status"], "patch_needed")
        self.assertFalse(result["applied"])
        self.assertEqual(before, self.tree())
        self.assertFalse(self.ledger.exists())
        self.assertFalse((self.root / "backups").exists())

    def test_replay_preserves_original_and_restore_is_exact(self):
        before = self.tree()
        result = self.apply()
        backup = Path(result["backup"])
        self.assertEqual((backup / "index.js").read_bytes(), before["index.js"])
        self.assertEqual(self.preview()["status"], "already_patched")
        self.assertFalse(self.apply()["applied"])
        config = json.loads((self.plugin / patcher.CONFIG).read_text())
        self.assertEqual(config, {"projectConfig": str(self.project), "ledgerFile": str(self.ledger)})
        self.assertIn(b"Fixture local customization", (self.plugin / "index.js").read_bytes())
        self.assertTrue(self.restored(result)["applied"])
        self.assertEqual(before, self.tree())
        self.assertEqual(patcher.restore(self.plugin, backup / "patch.json")["status"], "already_restored")
        self.assertFalse(self.ledger.exists())

    def test_apply_needs_stopped_host_fresh_preview_and_private_backup(self):
        before = self.tree()
        args = dict(project_config=self.project, ledger_file=self.ledger, apply=True)
        for extra in [{}, {"host_stopped": True}, {"host_stopped": True, "backup_dir": self.root / "backups", "expected_fingerprint": "stale"}]:
            with self.subTest(extra=list(extra)), self.assertRaises(patcher.PatchError):
                patcher.patch(self.plugin, **args, **extra)
        with self.assertRaises(patcher.PatchError):
            patcher.patch(self.plugin, **args, host_stopped=True, backup_dir=ROOT / "private-should-not-create",
                          expected_fingerprint=self.preview()["fingerprint"])
        self.assertEqual(before, self.tree())

    def test_unknown_index_client_version_and_owned_paths_refuse(self):
        for name in ["index.js", "lib/mcp.mjs", "package.json"]:
            file = self.plugin / name
            before = file.read_bytes()
            if name == "package.json":
                value = json.loads(before); value["version"] = "2.2.1"
                file.write_text(json.dumps(value))
            else: file.write_bytes(before + b"\n// unreviewed edit\n")
            with self.subTest(name=name), self.assertRaises(patcher.PatchError): self.preview()
            file.write_bytes(before)
        (self.plugin / patcher.CONFIG).write_text('{"local":true}')
        with self.assertRaises(patcher.PatchError): self.preview()

    def test_config_and_ledger_are_explicit_private_independent_paths(self):
        for project, ledger in [(Path("relative.json"), self.ledger), (self.project, self.plugin / "ledger.sqlite"),
                                (ROOT / "config/project.json", self.ledger), (self.project, self.root / "ledger.json")]:
            with self.subTest(project=str(project), ledger=str(ledger)), self.assertRaises(patcher.PatchError):
                patcher.patch(self.plugin, project_config=project, ledger_file=ledger)

    def test_preview_binds_inode_and_dependency_not_only_bytes(self):
        preview = self.preview()
        target = self.plugin / "index.js"
        replacement = self.plugin / "same.js"
        replacement.write_bytes(target.read_bytes())
        os.replace(replacement, target)
        with self.assertRaises(patcher.PatchError):
            patcher.patch(self.plugin, project_config=self.project, ledger_file=self.ledger, apply=True,
                          host_stopped=True, backup_dir=self.root / "backups", expected_fingerprint=preview["fingerprint"])

    def test_partial_apply_can_restore_generated_files_and_original_without_loss(self):
        before = self.tree()
        original_replace = patcher.os.replace
        def interrupt(source, target):
            if Path(target) == self.plugin / "index.js": raise OSError("fixture interruption")
            return original_replace(source, target)
        with mock.patch.object(patcher.os, "replace", side_effect=interrupt), self.assertRaises(OSError): self.apply()
        manifests = list((self.root / "backups").glob("*/patch.json"))
        self.assertEqual(len(manifests), 1)
        self.assertTrue((self.plugin / patcher.MODULE).exists())
        preview = patcher.restore(self.plugin, manifests[0])
        self.assertEqual(preview["restore"], [])
        patcher.restore(self.plugin, manifests[0], apply=True, host_stopped=True, expected_fingerprint=preview["fingerprint"])
        self.assertEqual(before, self.tree())

    def test_interruption_after_index_replace_before_marker_is_recoverable(self):
        before = self.tree()
        original_write = patcher.write_new
        def interrupt(file, raw):
            if Path(file) == self.plugin / patcher.MARKER: raise OSError("fixture marker publication interrupted")
            return original_write(file, raw)
        with mock.patch.object(patcher, "write_new", side_effect=interrupt), self.assertRaises(OSError): self.apply()
        manifest = next((self.root / "backups").glob("*/patch.json"))
        preview = patcher.restore(self.plugin, manifest)
        self.assertEqual(preview["restore"], ["index.js"])
        patcher.restore(self.plugin, manifest, apply=True, host_stopped=True, expected_fingerprint=preview["fingerprint"])
        self.assertEqual(before, self.tree())

    def test_restore_rejects_later_changes_as_a_whole_before_mutation(self):
        result = self.apply()
        manifest = Path(result["backup"]) / "patch.json"
        for name in patcher.OUTPUTS:
            file = self.plugin / name
            saved = file.read_bytes()
            file.write_bytes(saved + b"\n// later private edit\n")
            before = self.tree()
            with self.subTest(name=name), self.assertRaises(patcher.PatchError): patcher.restore(self.plugin, manifest)
            self.assertEqual(before, self.tree())
            file.write_bytes(saved)

    def test_an_edit_during_generated_publication_is_not_overwritten(self):
        original_write = patcher.write_new
        later = (self.plugin / "index.js").read_bytes() + b"\n// later operator edit\n"
        def mutate(file, raw):
            original_write(file, raw)
            if Path(file) == self.plugin / patcher.CONFIG:
                (self.plugin / "index.js").write_bytes(later)
        with mock.patch.object(patcher, "write_new", side_effect=mutate), self.assertRaises(patcher.PatchError): self.apply()
        self.assertEqual((self.plugin / "index.js").read_bytes(), later)
        self.assertTrue(list((self.root / "backups").glob("*/patch.json")))

    def test_restore_rechecks_manifest_original_package_and_preview(self):
        result = self.apply()
        manifest = Path(result["backup"]) / "patch.json"
        for file in [manifest, manifest.parent / "index.js", self.plugin / "package.json"]:
            saved = file.read_bytes()
            file.write_bytes(saved + b"\ninvalid")
            with self.subTest(file=file.name), self.assertRaises((patcher.PatchError, ValueError)): patcher.restore(self.plugin, manifest)
            file.write_bytes(saved)
        preview = patcher.restore(self.plugin, manifest)
        target = self.plugin / patcher.MODULE
        replacement = self.plugin / "replacement.js"
        replacement.write_bytes(target.read_bytes()); os.replace(replacement, target)
        before = self.tree()
        with self.assertRaises(patcher.PatchError):
            patcher.restore(self.plugin, manifest, apply=True, host_stopped=True, expected_fingerprint=preview["fingerprint"])
        self.assertEqual(before, self.tree())

    def test_restore_keeps_private_ledger_and_config_and_severs_hardlinks(self):
        peer = self.root / "hardlink-peer.js"
        original = (self.plugin / "index.js").read_bytes()
        os.link(self.plugin / "index.js", peer)
        result = self.apply()
        self.assertEqual(peer.read_bytes(), original)
        patched_peer = self.root / "patched-hardlink-peer.js"
        os.link(self.plugin / "index.js", patched_peer)
        patched = patched_peer.read_bytes()
        self.ledger.write_bytes(b"fixture private ledger untouched")
        self.restored(result)
        self.assertEqual(patched_peer.read_bytes(), patched)
        self.assertEqual(self.ledger.read_bytes(), b"fixture private ledger untouched")
        self.assertEqual(self.project.read_text(), '{"decisions":{"mode":"unified"}}')

    def test_cli_preview_and_restore_do_not_discover_installation(self):
        command = [sys.executable, "-B", str(ROOT / "scripts/patch_bili_main_agent.py"), "--plugin-dir", str(self.plugin),
                   "--project-config", str(self.project), "--ledger-file", str(self.ledger)]
        before = self.tree()
        run = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse(json.loads(run.stdout)["applied"])
        self.assertEqual(before, self.tree())


if __name__ == "__main__":
    unittest.main()
