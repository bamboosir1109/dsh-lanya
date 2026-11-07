"""Real parent manifests on temporary packages; no app, model or transport.

The legacy module is this repository's exact 03795a source, not a private
installed package. Upgrade/restore tests preserve every parent program/marker.
"""
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
sys.path.insert(0, str(ROOT / 'scripts'))
import patch_onebot_policy as policy
import patch_onebot_replay as replay
import patch_onebot_egress as egress
import patch_onebot_egress_images as images

LEGACY = ROOT / 'tests/fixtures/onebot_egress/qq-egress-v1.js'


class EgressImagesPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lanya-egress-images-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plugin = self.root / 'plugin'
        shutil.copytree(ROOT / 'tests/fixtures/onebot_policy', self.plugin)
        shutil.copyfile(ROOT / 'tests/fixtures/onebot_replay/lib/replay.js', self.plugin / 'lib/replay.js')
        self.state = self.root / 'synthetic-production-sentinel.json'
        self.state_bytes = b'{"synthetic_production_sentinel":true}\n'
        self.state.write_bytes(self.state_bytes)
        p = policy.patch(self.plugin, state_file=self.state)
        p = policy.patch(self.plugin, state_file=self.state, apply=True, host_stopped=True,
                         expected_fingerprint=p['fingerprint'], backup_dir=self.root / 'policy-backups')
        self.policy = Path(p['backup']) / 'patch.json'
        r = replay.patch(self.plugin, self.policy)
        r = replay.patch(self.plugin, self.policy, apply=True, host_stopped=True,
                         expected_fingerprint=r['fingerprint'], backup_dir=self.root / 'replay-backups')
        self.replay = Path(r['backup']) / 'patch.json'
        legacy = LEGACY.read_bytes()
        self.assertEqual(hashlib.sha256(legacy).hexdigest(), images.BEFORE_SHA256)
        self.fake_source = self.root / 'reviewed-old-source'
        (self.fake_source / 'plugin/lib').mkdir(parents=True)
        (self.fake_source / 'plugin/lib/qq-egress.js').write_bytes(legacy)
        with mock.patch.object(egress, 'REPO', self.fake_source):
            p = egress.patch(self.plugin, self.replay)
            p = egress.patch(self.plugin, self.replay, apply=True, host_stopped=True,
                             expected_fingerprint=p['fingerprint'], backup_dir=self.root / 'egress-backups')
        self.egress = Path(p['manifest'])
        self.parents = {path.relative_to(self.plugin).as_posix(): path.read_bytes()
                        for path in (self.plugin / 'lib').iterdir() if path.is_file() and path.name != Path(images.GENERATED).name}
        self.canonical = (ROOT / 'plugin/lib/qq-egress.js').read_bytes()
        self.assertNotEqual(hashlib.sha256(self.canonical).hexdigest(), images.BEFORE_SHA256)

    def preview(self):
        return images.patch(self.plugin, self.egress)

    def apply(self, preview=None):
        return images.patch(self.plugin, self.egress, apply=True, host_stopped=True,
                            expected_fingerprint=(preview or self.preview())['fingerprint'], backup_dir=self.root / 'images-backups')

    def restore(self, manifest):
        p = images.restore(self.plugin, manifest)
        return images.restore(self.plugin, manifest, apply=True, host_stopped=True, expected_fingerprint=p['fingerprint'])

    def assert_parents(self):
        for name, raw in self.parents.items():
            self.assertEqual((self.plugin / name).read_bytes(), raw, name)
        self.assertEqual(self.state.read_bytes(), self.state_bytes)

    def test_preview_upgrade_idempotency_and_ordered_restore(self):
        before = self.preview()
        self.assertEqual(before['status'], 'patch_needed')
        self.assertFalse((self.root / 'images-backups').exists())
        self.assertEqual((self.plugin / images.GENERATED).read_bytes(), LEGACY.read_bytes())
        self.assert_parents()
        applied = self.apply(before)
        self.assertEqual(applied['status'], 'patched')
        self.assertEqual((self.plugin / images.GENERATED).read_bytes(), self.canonical)
        self.assertEqual(set(applied['before']), {images.GENERATED})
        self.assertEqual(set(applied['after']), {images.GENERATED})
        self.assertEqual(self.preview()['status'], 'already_patched')
        self.assertFalse(self.apply()['applied'])
        with self.assertRaises(policy.PatchError):
            egress.restore(self.plugin, self.egress)
        self.assert_parents()
        self.restore(applied['manifest'])
        self.assertFalse((self.plugin / images.INSTALLED_MARKER).exists())
        self.assertEqual((self.plugin / images.GENERATED).read_bytes(), LEGACY.read_bytes())
        self.assertEqual(egress.patch(self.plugin, self.replay)['status'], 'already_patched')
        self.assert_parents()
        p = egress.restore(self.plugin, self.egress)
        egress.restore(self.plugin, self.egress, apply=True, host_stopped=True, expected_fingerprint=p['fingerprint'])
        p = replay.restore(self.plugin, self.replay)
        replay.restore(self.plugin, self.replay, apply=True, host_stopped=True, expected_fingerprint=p['fingerprint'])

    def test_new_v1_install_is_not_needed_without_fake_upgrade(self):
        p = egress.restore(self.plugin, self.egress)
        egress.restore(self.plugin, self.egress, apply=True, host_stopped=True, expected_fingerprint=p['fingerprint'])
        p = egress.patch(self.plugin, self.replay)
        p = egress.patch(self.plugin, self.replay, apply=True, host_stopped=True,
                         expected_fingerprint=p['fingerprint'], backup_dir=self.root / 'fresh-egress-backups')
        self.egress = Path(p['manifest'])
        preview = self.preview()
        self.assertEqual(preview['status'], 'not_needed')
        self.assertFalse(preview['applied'])
        self.assertEqual(self.apply(preview)['status'], 'not_needed')
        self.assertFalse((self.root / 'images-backups').exists())
        self.assertFalse((self.plugin / images.INSTALLED_MARKER).exists())
        self.assertEqual((self.plugin / images.GENERATED).read_bytes(), self.canonical)

    def test_v1_legacy_partial_missing_generated_restores_after_source_update(self):
        (self.plugin / images.GENERATED).unlink()
        with self.assertRaises(policy.PatchError):
            egress.patch(self.plugin, self.replay)
        p = egress.restore(self.plugin, self.egress)
        self.assertEqual(p['status'], 'restore_preview')
        egress.restore(self.plugin, self.egress, apply=True, host_stopped=True, expected_fingerprint=p['fingerprint'])
        self.assertFalse((self.plugin / egress.INSTALLED_MARKER).exists())
        self.assertFalse((self.plugin / images.GENERATED).exists())
        self.assertEqual(self.state.read_bytes(), self.state_bytes)

    def test_unknown_recorded_v1_module_digest_is_not_source_drift_allowance(self):
        record = json.loads(self.egress.read_bytes())
        record['after'][images.GENERATED] = '9' * 64
        self.egress.write_bytes(replay.encoded(record))
        (self.plugin / egress.INSTALLED_MARKER).write_bytes(replay.encoded(dict(record, state='patched')))
        with self.assertRaises(policy.PatchError):
            self.preview()
        with self.assertRaises(policy.PatchError):
            egress.restore(self.plugin, self.egress)
        self.assertFalse((self.root / 'images-backups').exists())

    def test_stop_and_fresh_fingerprint_and_boolean_assertions(self):
        preview = self.preview()
        with self.assertRaises(policy.PatchError):
            images.patch(self.plugin, self.egress, apply=True, backup_dir=self.root / 'bad', expected_fingerprint=preview['fingerprint'])
        with self.assertRaises(policy.PatchError):
            self.apply(dict(preview, fingerprint='0' * 64))
        for value in ('false', 1, None, []):
            with self.subTest(value=value), self.assertRaises(policy.PatchError):
                images.patch(self.plugin, self.egress, apply=True, host_stopped=value,
                             expected_fingerprint=preview['fingerprint'], backup_dir=self.root / 'bad')
            with self.subTest(apply=value), self.assertRaises(policy.PatchError):
                images.patch(self.plugin, self.egress, apply=value)
        self.assertFalse((self.root / 'images-backups').exists())
        self.assert_parents()

    def test_parent_marker_original_and_program_drift_each_refuse_before_write(self):
        paths = [self.plugin / 'lib/lanya-policy.patch.json', self.plugin / replay.INSTALLED_MARKER,
                 self.plugin / egress.INSTALLED_MARKER, self.egress.parent / 'lib/bridge.js',
                 self.plugin / 'lib/bridge.js', self.plugin / 'lib/lanya-decision-policy.js']
        for path in paths:
            raw = path.read_bytes()
            try:
                path.write_bytes(raw + b'\n// unknown drift\n')
                with self.subTest(path=path.name), self.assertRaises((policy.PatchError, ValueError)):
                    self.preview()
                self.assertFalse((self.root / 'images-backups').exists())
            finally:
                path.write_bytes(raw)
        self.assert_parents()

    def test_unknown_module_and_missing_module_refuse_without_manifest(self):
        module = self.plugin / images.GENERATED
        module.write_bytes(module.read_bytes() + b'\n// later\n')
        with self.assertRaises(policy.PatchError): self.preview()
        module.unlink()
        with self.assertRaises(policy.PatchError): self.preview()
        self.assertFalse((self.root / 'images-backups').exists())
        self.assert_parents()

    def test_same_bytes_new_inode_invalidates_preview_before_backup(self):
        preview = self.preview()
        module = self.plugin / images.GENERATED
        replacement = self.root / 'same-bytes.js'
        replacement.write_bytes(module.read_bytes())
        old_inode = module.stat().st_ino
        os.replace(replacement, module)
        self.assertNotEqual(module.stat().st_ino, old_inode)
        with self.assertRaises(policy.PatchError): self.apply(preview)
        self.assertFalse((self.root / 'images-backups').exists())
        self.assert_parents()

    def test_hardlink_is_severed_in_both_directions(self):
        module = self.plugin / images.GENERATED
        peer = self.root / 'original-peer.js'
        os.link(module, peer)
        applied = self.apply()
        self.assertEqual(peer.read_bytes(), LEGACY.read_bytes())
        self.assertNotEqual(module.stat().st_ino, peer.stat().st_ino)
        upgraded_peer = self.root / 'upgraded-peer.js'
        os.link(module, upgraded_peer)
        self.restore(applied['manifest'])
        self.assertEqual(upgraded_peer.read_bytes(), self.canonical)
        self.assertNotEqual(module.stat().st_ino, upgraded_peer.stat().st_ino)
        self.assert_parents()

    def test_shared_package_lock_blocks_upgrade(self):
        preview = self.preview()
        with replay.package_lock(self.plugin):
            with self.assertRaises(policy.PatchError): self.apply(preview)
        self.assertFalse((self.root / 'images-backups').exists())
        self.assert_parents()

    def test_interruption_after_replacement_has_explicit_restore_and_preserves_parent(self):
        write = images.write_new
        def fail_marker(path, raw):
            if Path(path) == self.plugin / images.INSTALLED_MARKER:
                raise OSError('synthetic marker publication interruption')
            return write(path, raw)
        with mock.patch.object(images, 'write_new', side_effect=fail_marker):
            with self.assertRaises(OSError): self.apply()
        self.assertEqual((self.plugin / images.GENERATED).read_bytes(), self.canonical)
        self.assertFalse((self.plugin / images.INSTALLED_MARKER).exists())
        manifest = next((self.root / 'images-backups').glob('*/patch.json'))
        self.assertTrue((manifest.parent / 'interrupted.json').exists())
        with self.assertRaises(policy.PatchError): self.preview()
        self.restore(manifest)
        self.assertEqual((self.plugin / images.GENERATED).read_bytes(), LEGACY.read_bytes())
        self.assert_parents()

    def test_private_original_generated_copy_marker_and_later_change_block_restore(self):
        applied = self.apply()
        manifest = Path(applied['manifest'])
        paths = [manifest.parent / images.GENERATED, manifest.parent / 'generated.js',
                 self.plugin / images.INSTALLED_MARKER, self.plugin / images.GENERATED]
        for path in paths:
            raw = path.read_bytes()
            before = (self.plugin / images.GENERATED).read_bytes()
            try:
                path.write_bytes(raw + b'\n// unknown drift\n')
                current = (self.plugin / images.GENERATED).read_bytes()
                with self.subTest(path=path.name), self.assertRaises((policy.PatchError, ValueError)):
                    images.restore(self.plugin, manifest)
                self.assertEqual((self.plugin / images.GENERATED).read_bytes(), current)
            finally:
                path.write_bytes(raw)
            self.assertEqual((self.plugin / images.GENERATED).read_bytes(), before)
        self.assert_parents()

    def test_restore_requires_stop_and_fresh_fingerprint(self):
        applied = self.apply()
        p = images.restore(self.plugin, applied['manifest'])
        with self.assertRaises(policy.PatchError):
            images.restore(self.plugin, applied['manifest'], apply=True, expected_fingerprint=p['fingerprint'])
        with self.assertRaises(policy.PatchError):
            images.restore(self.plugin, applied['manifest'], apply=True, host_stopped=True, expected_fingerprint='0' * 64)
        self.assertEqual((self.plugin / images.GENERATED).read_bytes(), self.canonical)
        self.assert_parents()

    def test_interrupted_restore_cannot_remove_parent_layer_until_marker_cleanup(self):
        applied = self.apply()
        original_unlink = Path.unlink
        def fail_marker(path, *args, **kwargs):
            if path == self.plugin / images.INSTALLED_MARKER:
                raise OSError('synthetic restore marker cleanup interruption')
            return original_unlink(path, *args, **kwargs)
        p = images.restore(self.plugin, applied['manifest'])
        with mock.patch.object(Path, 'unlink', autospec=True, side_effect=fail_marker):
            with self.assertRaises(OSError):
                images.restore(self.plugin, applied['manifest'], apply=True, host_stopped=True, expected_fingerprint=p['fingerprint'])
        self.assertEqual((self.plugin / images.GENERATED).read_bytes(), LEGACY.read_bytes())
        self.assertTrue((self.plugin / images.INSTALLED_MARKER).exists())
        with self.assertRaises(policy.PatchError): egress.restore(self.plugin, self.egress)
        remaining = images.restore(self.plugin, applied['manifest'])
        self.assertEqual(remaining['restore'], [])
        self.assertEqual(remaining['remove'], [images.INSTALLED_MARKER])
        self.restore(applied['manifest'])
        self.assert_parents()

    def test_missing_owned_program_has_explicit_whole_layer_restore(self):
        applied = self.apply()
        (self.plugin / images.GENERATED).unlink()
        with self.assertRaises(policy.PatchError): self.preview()
        self.restore(applied['manifest'])
        self.assertEqual((self.plugin / images.GENERATED).read_bytes(), LEGACY.read_bytes())
        self.assertFalse((self.plugin / images.INSTALLED_MARKER).exists())
        self.assert_parents()

    def test_final_readback_failure_never_reports_success_and_restore_is_idempotent(self):
        inspect = images.inspect
        def fail(*args, **kwargs):
            result = inspect(*args, **kwargs)
            if (self.plugin / images.INSTALLED_MARKER).exists() and len(args) == 2:
                raise policy.PatchError('synthetic final readback failure')
            return result
        with mock.patch.object(images, 'inspect', side_effect=fail):
            with self.assertRaises(policy.PatchError): self.apply()
        manifest = next((self.root / 'images-backups').glob('*/patch.json'))
        self.restore(manifest)
        p = images.restore(self.plugin, manifest)
        self.assertEqual(p['status'], 'already_restored')
        self.assertEqual(p['restore'], [])
        self.assertEqual(p['remove'], [])
        again = images.restore(self.plugin, manifest, apply=True, host_stopped=True, expected_fingerprint=p['fingerprint'])
        self.assertEqual(again['status'], 'already_restored')
        self.assertFalse(again['applied'])
        self.assert_parents()


if __name__ == '__main__':
    unittest.main()
