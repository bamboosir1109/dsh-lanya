"""Reviewed V1/replay manifest fixtures; all installation writes are temporary.

Full upstream/installed outbound consumers run in test_onebot_egress_native.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import patch_onebot_policy as policy
import patch_onebot_replay as replay
import patch_onebot_egress as egress


class EgressPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lanya-egress-patch-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plugin = self.root / 'plugin'
        shutil.copytree(ROOT / 'tests/fixtures/onebot_policy', self.plugin)
        shutil.copyfile(ROOT / 'tests/fixtures/onebot_replay/lib/replay.js', self.plugin / 'lib/replay.js')
        self.state = self.root / 'synthetic-policy.json'
        p = policy.patch(self.plugin, state_file=self.state)
        p = policy.patch(self.plugin, state_file=self.state, apply=True, host_stopped=True,
                         expected_fingerprint=p['fingerprint'], backup_dir=self.root / 'policy-backups')
        self.policy = Path(p['backup']) / 'patch.json'
        r = replay.patch(self.plugin, self.policy)
        r = replay.patch(self.plugin, self.policy, apply=True, host_stopped=True,
                         expected_fingerprint=r['fingerprint'], backup_dir=self.root / 'replay-backups')
        self.replay = Path(r['backup']) / 'patch.json'
        self.originals = {name: (self.plugin / name).read_bytes() for name in egress.TARGETS}
        self.parent_markers = {name: (self.plugin / name).read_bytes() for name in
            ('lib/lanya-policy.patch.json', replay.INSTALLED_MARKER)}

    def preview(self):
        return egress.patch(self.plugin, self.replay)

    def apply(self, preview=None):
        return egress.patch(self.plugin, self.replay, apply=True, host_stopped=True,
                            expected_fingerprint=(preview or self.preview())['fingerprint'], backup_dir=self.root / 'egress-backups')

    def restore(self, manifest):
        p = egress.restore(self.plugin, manifest)
        return egress.restore(self.plugin, manifest, apply=True, host_stopped=True, expected_fingerprint=p['fingerprint'])

    def assert_originals(self):
        for name, raw in self.originals.items():
            self.assertEqual((self.plugin / name).read_bytes(), raw)
        for name, raw in self.parent_markers.items():
            self.assertEqual((self.plugin / name).read_bytes(), raw)
        self.assertFalse(self.state.exists())

    def test_preview_apply_and_ordered_restore_preserve_old_layers(self):
        p = self.preview()
        self.assertEqual(p['status'], 'patch_needed')
        self.assertFalse((self.root / 'egress-backups').exists())
        self.assert_originals()
        result = self.apply(p)
        self.assertEqual(self.preview()['status'], 'already_patched')
        self.assertFalse(self.apply(self.preview())['applied'])
        self.assertEqual(set(result['before']), set(egress.TARGETS))
        self.assertEqual(set(result['after']), set((*egress.TARGETS, egress.GENERATED)))
        with self.assertRaises(policy.PatchError):
            replay.restore(self.plugin, self.replay)
        self.restore(result['manifest'])
        self.assert_originals()
        r = replay.restore(self.plugin, self.replay)
        replay.restore(self.plugin, self.replay, apply=True, host_stopped=True, expected_fingerprint=r['fingerprint'])
        p = policy.restore(self.plugin, self.policy)
        policy.restore(self.plugin, self.policy, apply=True, host_stopped=True, expected_fingerprint=p['fingerprint'])

    def test_parent_original_tampering_refuses_whole_preview(self):
        original = self.replay.parent / 'lib/bridge.js'
        original.write_bytes(original.read_bytes() + b'\n// later\n')
        with self.assertRaises(policy.PatchError): self.preview()
        self.assert_originals()

    def test_owned_target_drift_refuses_before_backup(self):
        target = self.plugin / 'lib/outbound.js'
        target.write_bytes(target.read_bytes() + b'\n// later\n')
        with self.assertRaises(policy.PatchError): self.preview()
        self.assertFalse((self.root / 'egress-backups').exists())

    def test_fresh_fingerprint_and_stop_assertion(self):
        p = self.preview()
        with self.assertRaises(policy.PatchError):
            egress.patch(self.plugin, self.replay, apply=True, expected_fingerprint=p['fingerprint'], backup_dir=self.root / 'bad')
        with self.assertRaises(policy.PatchError): self.apply(dict(p, fingerprint='0' * 64))
        self.assert_originals()

    def test_api_rejects_truthy_non_boolean_stop_and_apply(self):
        p = self.preview()
        for value in ('false', 1, [], None):
            with self.subTest(value=value), self.assertRaises(policy.PatchError):
                egress.patch(self.plugin, self.replay, apply=True, host_stopped=value,
                             expected_fingerprint=p['fingerprint'], backup_dir=self.root / 'bad')
            with self.subTest(apply=value), self.assertRaises(policy.PatchError):
                egress.patch(self.plugin, self.replay, apply=value)
        self.assert_originals()
        result = self.apply(p)
        current = {name: (self.plugin / name).read_bytes() for name in egress.TARGETS}
        for value in ('false', 1, [], None):
            with self.subTest(restore=value), self.assertRaises(policy.PatchError):
                egress.restore(self.plugin, result['manifest'], apply=True, host_stopped=value)
            with self.subTest(restore_apply=value), self.assertRaises(policy.PatchError):
                egress.restore(self.plugin, result['manifest'], apply=value)
        self.assertEqual(current, {name: (self.plugin / name).read_bytes() for name in egress.TARGETS})

    def test_hardlink_is_severed_on_apply_and_restore(self):
        target = self.plugin / 'lib/outbound.js'
        peer = self.root / 'shared.js'
        os.link(target, peer)
        applied = self.apply()
        self.assertEqual(peer.read_bytes(), self.originals['lib/outbound.js'])
        self.assertNotEqual(target.stat().st_ino, peer.stat().st_ino)
        self.restore(applied['manifest'])
        self.assert_originals()
        self.assertNotEqual(target.stat().st_ino, peer.stat().st_ino)

    def test_partial_apply_has_verified_explicit_restore(self):
        original_replace = os.replace
        count = 0
        def fail_second(source, target):
            nonlocal count
            if Path(target).name in ('bridge.js', 'outbound.js', 'lanya-policy-adapter.js'):
                count += 1
                if count == 2: raise OSError('synthetic interruption')
            return original_replace(source, target)
        with mock.patch.object(egress.os, 'replace', side_effect=fail_second):
            with self.assertRaises(OSError): self.apply()
        manifest = next((self.root / 'egress-backups').glob('*/patch.json'))
        self.assertTrue((manifest.parent / 'interrupted.json').exists())
        with self.assertRaises(policy.PatchError): self.preview()
        self.restore(manifest)
        self.assert_originals()

    def test_later_generated_change_refuses_restore_without_writes(self):
        result = self.apply()
        module = self.plugin / egress.GENERATED
        module.write_bytes(module.read_bytes() + b'\n// later\n')
        before = {name: (self.plugin / name).read_bytes() for name in egress.TARGETS}
        with self.assertRaises(policy.PatchError): egress.restore(self.plugin, result['manifest'])
        self.assertEqual(before, {name: (self.plugin / name).read_bytes() for name in egress.TARGETS})

    def test_final_apply_readback_failure_is_not_success(self):
        original = egress.inspect
        def fail(*args, **kwargs):
            result = original(*args, **kwargs)
            if (self.plugin / egress.INSTALLED_MARKER).exists() and len(args) == 2:
                raise policy.PatchError('synthetic final readback failure')
            return result
        with mock.patch.object(egress, 'inspect', side_effect=fail):
            with self.assertRaises(policy.PatchError): self.apply()
        manifest = next((self.root / 'egress-backups').glob('*/patch.json'))
        self.restore(manifest)
        self.assert_originals()


if __name__ == '__main__':
    unittest.main()
