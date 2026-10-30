"""Incremental replay isolation on temporary synthetic packages only."""
import hashlib
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
import patch_onebot_policy as base
import patch_onebot_replay as patcher


class ReplayPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lanya-replay-patch-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plugin = self.root / "plugin"
        shutil.copytree(ROOT / "tests/fixtures/onebot_policy", self.plugin)
        shutil.copyfile(ROOT / "tests/fixtures/onebot_replay/lib/replay.js", self.plugin / "lib/replay.js")
        self.state = self.root / "production-sentinel.json"
        self.state.write_bytes(b'{"synthetic_production_sentinel":true}\n')
        preview = base.patch(self.plugin, state_file=self.state)
        applied = base.patch(self.plugin, state_file=self.state, apply=True, host_stopped=True,
                             expected_fingerprint=preview["fingerprint"], backup_dir=self.root / "base-backups")
        self.parent = Path(applied["backup"]) / "patch.json"
        self.parent_marker = (self.plugin / "lib/lanya-policy.patch.json").read_bytes()
        self.before = {name: (self.plugin / name).read_bytes() for name in patcher.TARGETS}

    def preview(self):
        return patcher.patch(self.plugin, self.parent)

    def apply(self, preview=None, **kwargs):
        return patcher.patch(self.plugin, self.parent, apply=True, host_stopped=True,
                             expected_fingerprint=(preview or self.preview())["fingerprint"],
                             backup_dir=self.root / "replay-backups", **kwargs)

    def restore(self, manifest):
        preview = patcher.restore(self.plugin, manifest)
        return patcher.restore(self.plugin, manifest, apply=True, host_stopped=True,
                               expected_fingerprint=preview["fingerprint"])

    def assert_originals(self):
        for name, raw in self.before.items():
            self.assertEqual((self.plugin / name).read_bytes(), raw)
        self.assertEqual((self.plugin / "lib/lanya-policy.patch.json").read_bytes(), self.parent_marker)
        self.assertEqual(self.state.read_bytes(), b'{"synthetic_production_sentinel":true}\n')

    def test_preview_is_readonly_and_preserves_explicit_recording_api(self):
        result = self.preview()
        self.assertEqual(result["status"], "patch_needed")
        self.assertFalse(result["applied"])
        self.assertFalse((self.plugin / ".lanya-replay-isolation.lock").exists())
        self.assertFalse((self.root / "replay-backups").exists())
        transformed = patcher.transform("lib/replay.js", self.before["lib/replay.js"]).decode()
        self.assertIn("const sourceFile = options.inboxFile", transformed)
        self.assertIn("await writeFile(options.outFile", transformed)
        self.assertIn("root: diagnosticRoot", transformed)
        self.assert_originals()

    def test_apply_idempotency_and_restore_then_original_v1_restore(self):
        result = self.apply()
        self.assertTrue(result["applied"])
        self.assertEqual(result["patch"], "lanya-onebot-replay-isolation-v1")
        manifest = Path(result["manifest"])
        record = json.loads(manifest.read_text())
        self.assertEqual(set(record["after"]), set(patcher.TARGETS))
        self.assertEqual(record["parent"]["manifest_sha256"], hashlib.sha256(self.parent.read_bytes()).hexdigest())
        self.assertEqual((self.plugin / "lib/lanya-policy.patch.json").read_bytes(), self.parent_marker)
        again = self.preview()
        self.assertEqual(again["status"], "already_patched")
        self.assertFalse(self.apply(again)["applied"])
        self.assertTrue(self.restore(manifest)["applied"])
        self.assert_originals()
        self.assertFalse((self.plugin / patcher.INSTALLED_MARKER).exists())
        old = base.restore(self.plugin, self.parent)
        base.restore(self.plugin, self.parent, apply=True, host_stopped=True, expected_fingerprint=old["fingerprint"])
        self.assertFalse((self.plugin / "lib/lanya-policy.patch.json").exists())

    def test_existing_v1_private_original_and_marker_must_match(self):
        original = self.parent.parent / "bridge.js"
        original.write_bytes(original.read_bytes() + b"\n// later\n")
        with self.assertRaisesRegex(patcher.PatchError, "original/transform"):
            self.preview()
        self.assert_originals()

    def test_generated_module_drift_refuses_before_writes(self):
        target = self.plugin / "lib/lanya-policy-adapter.js"
        target.write_bytes(target.read_bytes() + b"\n// later\n")
        with self.assertRaisesRegex(patcher.PatchError, "V1 output"):
            self.preview()
        self.assert_originals()

    def test_reformatted_v1_marker_is_a_conflict(self):
        target = self.plugin / "lib/lanya-policy.patch.json"
        target.write_bytes(target.read_bytes() + b"\n")
        with self.assertRaisesRegex(patcher.PatchError, "V1 marker"):
            self.preview()

    def test_stopped_declaration_and_fresh_fingerprint_are_required(self):
        preview = self.preview()
        with self.assertRaises(patcher.PatchError):
            patcher.patch(self.plugin, self.parent, apply=True, expected_fingerprint=preview["fingerprint"], backup_dir=self.root / "bad")
        with self.assertRaisesRegex(patcher.PatchError, "unchanged"):
            self.apply(dict(preview, fingerprint="bad"))
        self.assert_originals()

    def test_new_inode_breaks_hardlinks_on_apply_and_restore(self):
        target = self.plugin / "lib/bridge.js"
        peer = self.root / "shared-bridge.js"
        os.link(target, peer)
        result = self.apply()
        self.assertEqual(peer.read_bytes(), self.before["lib/bridge.js"])
        self.assertNotEqual(target.stat().st_ino, peer.stat().st_ino)
        self.restore(Path(result["manifest"]))
        self.assert_originals()
        self.assertNotEqual(target.stat().st_ino, peer.stat().st_ino)

    def test_partial_apply_keeps_verified_restore_manifest(self):
        replace = os.replace
        calls = 0
        def fail_second(source, destination):
            nonlocal calls
            if Path(destination).name in ("bridge.js", "replay.js", "inbound.js"):
                calls += 1
                if calls == 2:
                    raise OSError("synthetic interruption")
            return replace(source, destination)
        with mock.patch.object(patcher.os, "replace", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.apply()
        manifests = list((self.root / "replay-backups").glob("*/patch.json"))
        self.assertEqual(len(manifests), 1)
        self.assertTrue((manifests[0].parent / "interrupted.json").exists())
        with self.assertRaises(patcher.PatchError):
            self.preview()
        self.restore(manifests[0])
        self.assert_originals()
        self.assertFalse(list((self.plugin / "lib").glob(".lanya-replay-stage-*")))

    def test_later_target_edit_refuses_entire_restore(self):
        result = self.apply()
        target = self.plugin / "lib/inbound.js"
        target.write_bytes(target.read_bytes() + b"\n// later operator edit\n")
        before = {name: (self.plugin / name).read_bytes() for name in patcher.TARGETS}
        with self.assertRaisesRegex(patcher.PatchError, "Later/unknown"):
            patcher.restore(self.plugin, result["manifest"])
        self.assertEqual(before, {name: (self.plugin / name).read_bytes() for name in patcher.TARGETS})

    def test_corrupt_private_backup_refuses_restore(self):
        result = self.apply()
        saved = Path(result["backup"]) / "lib/replay.js"
        saved.write_bytes(b"corrupt")
        with self.assertRaisesRegex(patcher.PatchError, "original hash"):
            patcher.restore(self.plugin, result["manifest"])

    def test_same_bytes_new_identity_invalidates_restore_preview(self):
        result = self.apply()
        preview = patcher.restore(self.plugin, result["manifest"])
        target = self.plugin / "lib/bridge.js"
        replacement = self.root / "same.js"
        replacement.write_bytes(target.read_bytes())
        os.replace(replacement, target)
        with self.assertRaisesRegex(patcher.PatchError, "unchanged"):
            patcher.restore(self.plugin, result["manifest"], apply=True, host_stopped=True, expected_fingerprint=preview["fingerprint"])

    def test_final_apply_readback_failure_cannot_report_success(self):
        inspect = patcher.inspect
        def failed(*args, **kwargs):
            value = inspect(*args, **kwargs)
            if (self.plugin / patcher.INSTALLED_MARKER).exists() and kwargs.get("restore_manifest") is None:
                raise patcher.PatchError("synthetic final readback failure")
            return value
        with mock.patch.object(patcher, "inspect", side_effect=failed):
            with self.assertRaisesRegex(patcher.PatchError, "readback"):
                self.apply()
        manifest = next((self.root / "replay-backups").glob("*/patch.json"))
        self.restore(manifest)
        self.assert_originals()

    def test_final_restore_readback_failure_cannot_report_success(self):
        result = self.apply()
        preview = patcher.restore(self.plugin, result["manifest"])
        inspect = patcher.inspect
        def failed(*args, **kwargs):
            value = inspect(*args, **kwargs)
            if not (self.plugin / patcher.INSTALLED_MARKER).exists() and all((self.plugin / name).read_bytes() == raw for name, raw in self.before.items()):
                raise patcher.PatchError("synthetic final restore readback failure")
            return value
        with mock.patch.object(patcher, "inspect", side_effect=failed):
            with self.assertRaisesRegex(patcher.PatchError, "readback"):
                patcher.restore(self.plugin, result["manifest"], apply=True, host_stopped=True, expected_fingerprint=preview["fingerprint"])
        self.assertEqual(patcher.restore(self.plugin, result["manifest"])["status"], "already_restored")
        self.assert_originals()

    def test_private_backups_cannot_enter_source_repository(self):
        with self.assertRaisesRegex(patcher.PatchError, "outside source"):
            patcher.patch(self.plugin, self.parent, apply=True, host_stopped=True,
                          expected_fingerprint=self.preview()["fingerprint"], backup_dir=ROOT / "forbidden-private-backup")
        self.assertFalse((ROOT / "forbidden-private-backup").exists())

    def test_os_lock_and_two_processes_do_not_both_apply_same_preview(self):
        with patcher.package_lock(self.plugin):
            with self.assertRaisesRegex(patcher.PatchError, "owns"):
                with patcher.package_lock(self.plugin):
                    self.fail("nested lock acquired")
        preview = self.preview()
        argv = [sys.executable, str(ROOT / "scripts/patch_onebot_replay.py"), "--plugin-dir", str(self.plugin),
                "--policy-manifest", str(self.parent), "--apply", "--host-stopped", "--expected-fingerprint", preview["fingerprint"],
                "--backup-dir", str(self.root / "replay-backups")]
        processes = [subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        statuses = []
        for process in processes:
            process.communicate(timeout=30)
            statuses.append(process.returncode)
        self.assertEqual(sorted(statuses), [0, 2])
        self.assertEqual(len(list((self.root / "replay-backups").glob("*/patch.json"))), 1)


if __name__ == "__main__":
    unittest.main()
