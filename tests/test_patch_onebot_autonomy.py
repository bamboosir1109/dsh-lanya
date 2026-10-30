"""Real old patchers + public fixed source on temporary packages only.

No installed package, policy database, service, model or network is used.
The new updater never changes old patcher globals; fixtures replay an older
release by temporarily pointing those old installers at a synthetic source tree.
"""
from contextlib import ExitStack
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
import patch_onebot_autonomy as autonomy

FIXTURE = ROOT / 'tests/fixtures/onebot_autonomy/adapter-c684991.js'


class AutonomyPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = tempfile.TemporaryDirectory(prefix='lanya-autonomy-template-')
        cls.addClassCleanup(cls.template.cleanup)
        cls.fixture_root = Path(cls.template.name)
        cls.old = cls.fixture_root / 'old-source'
        (cls.old / 'plugin/lib').mkdir(parents=True)
        for name in ('decision-policy.js', 'observation-queue.js'):
            shutil.copyfile(ROOT / 'plugin/lib' / name, cls.old / 'plugin/lib' / name)
        shutil.copyfile(FIXTURE, cls.old / 'plugin/lib/onebot-policy-adapter.js')
        shutil.copyfile(ROOT / 'tests/fixtures/onebot_egress/qq-egress-v1.js', cls.old / 'plugin/lib/qq-egress.js')
        assert hashlib.sha256(autonomy.base_adapter(FIXTURE.read_bytes())).hexdigest() == autonomy.LEGACY_BASE_SHA256

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lanya-autonomy-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plugin = self.root / 'plugin'
        shutil.copytree(ROOT / 'tests/fixtures/onebot_policy', self.plugin)
        shutil.copyfile(ROOT / 'tests/fixtures/onebot_replay/lib/replay.js', self.plugin / 'lib/replay.js')
        self.sentinel = self.root / 'synthetic-private-policy.json'
        self.sentinel.write_bytes(b'{"synthetic_policy":"never-open-or-change"}\n')
        self.sentinel_before = self.sentinel.stat()
        self.source = self.root / 'source'
        (self.source / 'plugin/lib').mkdir(parents=True)
        (self.source / 'package.json').write_text('{"name":"dsh-lanya","type":"module"}\n', encoding='utf-8')
        for name in autonomy.DEPENDENCIES:
            shutil.copyfile(ROOT / 'plugin/lib' / name, self.source / 'plugin/lib' / name)
        # The updater accepts explicitly reviewed source, not mutable git HEAD.
        self.updated_source = b'// Synthetic reviewed update; no app execution.\n' + FIXTURE.read_bytes()
        (self.source / 'plugin/lib/onebot-policy-adapter.js').write_bytes(self.updated_source)
        with ExitStack() as stack:
            for module in (policy, replay, egress): stack.enter_context(mock.patch.object(module, 'REPO', self.old))
            p = policy.patch(self.plugin, state_file=self.sentinel)
            p = policy.patch(self.plugin, state_file=self.sentinel, apply=True, host_stopped=True,
                             expected_fingerprint=p['fingerprint'], backup_dir=self.root / 'policy')
            self.policy = Path(p['backup']) / 'patch.json'
            r = replay.patch(self.plugin, self.policy)
            r = replay.patch(self.plugin, self.policy, apply=True, host_stopped=True,
                             expected_fingerprint=r['fingerprint'], backup_dir=self.root / 'replay')
            self.replay = Path(r['backup']) / 'patch.json'
            e = egress.patch(self.plugin, self.replay)
            e = egress.patch(self.plugin, self.replay, apply=True, host_stopped=True,
                             expected_fingerprint=e['fingerprint'], backup_dir=self.root / 'egress')
            self.egress = Path(e['manifest'])
            stack.enter_context(mock.patch.object(images, 'SOURCE', ROOT / 'plugin/lib/qq-egress.js'))
            p = images.patch(self.plugin, self.egress)
            p = images.patch(self.plugin, self.egress, apply=True, host_stopped=True,
                             expected_fingerprint=p['fingerprint'], backup_dir=self.root / 'images')
            self.images = Path(p['manifest'])
        self.parents = {p.relative_to(self.plugin).as_posix(): p.read_bytes()
                        for p in self.plugin.rglob('*') if p.is_file() and p.name != '.lanya-replay-isolation.lock'}

    def preview(self):
        return autonomy.patch(self.plugin, self.source, self.images)

    def apply(self, before=None):
        return autonomy.patch(self.plugin, self.source, self.images, apply=True, host_stopped=True,
                              expected_fingerprint=(before or self.preview())['fingerprint'], backup_dir=self.root / 'autonomy')

    def assert_unowned(self):
        for name, raw in self.parents.items():
            if name != autonomy.TARGET: self.assertEqual((self.plugin / name).read_bytes(), raw, name)
        self.assertEqual(self.sentinel.read_bytes(), b'{"synthetic_policy":"never-open-or-change"}\n')
        now = self.sentinel.stat()
        self.assertEqual((now.st_mtime_ns, now.st_size), (self.sentinel_before.st_mtime_ns, self.sentinel_before.st_size))

    def test_preview_apply_noop_and_restore_preserve_all_parent_programs(self):
        p = self.preview()
        self.assertEqual(p['status'], 'patch_needed')
        self.assertFalse(p['applied']); self.assertFalse((self.root / 'autonomy').exists())
        self.assertEqual((self.plugin / autonomy.TARGET).read_bytes(), self.parents[autonomy.TARGET])
        result = self.apply(p)
        self.assertEqual(result['status'], 'patched')
        self.assertEqual((self.plugin / autonomy.TARGET).read_bytes(), autonomy.compile_adapter(self.updated_source))
        self.assertEqual(self.preview()['status'], 'already_patched')
        self.assertFalse(self.apply()['applied'])
        self.assert_unowned()
        manifest = result['manifest']
        # Future source changes must not prevent restoring the saved release.
        (self.source / 'plugin/lib/onebot-policy-adapter.js').write_text('future source drift', encoding='utf-8')
        p = autonomy.restore(self.plugin, manifest)
        done = autonomy.restore(self.plugin, manifest, apply=True, host_stopped=True, expected_fingerprint=p['fingerprint'])
        self.assertTrue(done['applied'])
        self.assertEqual((self.plugin / autonomy.TARGET).read_bytes(), self.parents[autonomy.TARGET])
        self.assertFalse((self.plugin / autonomy.INSTALLED_MARKER).exists())
        self.assertEqual(autonomy.restore(self.plugin, manifest)['status'], 'already_restored')
        self.assert_unowned()

    def test_strict_boolean_stopped_host_and_stale_fingerprint(self):
        for value in ('false', 1, None, []):
            with self.subTest(value=value), self.assertRaises(policy.PatchError):
                autonomy.patch(self.plugin, self.source, self.images, apply=True, host_stopped=value)
            with self.subTest(apply=value), self.assertRaises(policy.PatchError):
                autonomy.patch(self.plugin, self.source, self.images, apply=value)
        p = self.preview()
        with self.assertRaises(policy.PatchError):
            autonomy.patch(self.plugin, self.source, self.images, apply=True, expected_fingerprint=p['fingerprint'], backup_dir=self.root / 'bad')
        with self.assertRaises(policy.PatchError): self.apply(dict(p, fingerprint='0' * 64))
        self.assertFalse((self.root / 'autonomy').exists())
        self.assert_unowned()

    def test_source_or_installed_identity_drift_refuses_before_replacement(self):
        p = self.preview()
        target = self.plugin / autonomy.TARGET
        raw = target.read_bytes(); temp = target.with_suffix('.new'); temp.write_bytes(raw); os.replace(temp, target)
        with self.assertRaises(policy.PatchError): self.apply(p)
        p = self.preview()
        source = self.source / 'plugin/lib/onebot-policy-adapter.js'
        source.write_bytes(b'// later change\n' + source.read_bytes())
        with self.assertRaises(policy.PatchError): self.apply(p)
        self.assertEqual(target.read_bytes(), raw)

    def test_foreign_package_parent_manifest_and_source_dependency_are_rejected(self):
        record = json.loads(self.images.read_bytes()); record['plugin'] = str(self.root / 'foreign-plugin')
        self.images.write_bytes(replay.encoded(record))
        with self.assertRaises(policy.PatchError): self.preview()
        self.assertFalse((self.root / 'autonomy').exists())

    def test_unowned_program_marker_or_backup_tampering_refuses(self):
        paths = [self.plugin / 'lib/lanya-decision-policy.js', self.plugin / images.INSTALLED_MARKER,
                 self.egress.parent / 'lib/bridge.js']
        for path in paths:
            original = path.read_bytes(); path.write_bytes(original + b'\n')
            with self.subTest(path=path.name), self.assertRaises(policy.PatchError): self.preview()
            path.write_bytes(original)
        (self.source / 'plugin/lib/decision-policy.js').write_bytes(b'unsupported core')
        with self.assertRaises(policy.PatchError): self.preview()

    def test_hardlinked_old_adapter_gets_new_inode_without_changing_shared_source(self):
        target = self.plugin / autonomy.TARGET; twin = self.root / 'shared-old-adapter.js'
        os.link(target, twin); old = twin.read_bytes(); inode = twin.stat().st_ino
        result = self.apply()
        self.assertEqual(twin.read_bytes(), old); self.assertEqual(twin.stat().st_ino, inode)
        self.assertNotEqual(target.stat().st_ino, inode)
        p = autonomy.restore(self.plugin, result['manifest'])
        autonomy.restore(self.plugin, result['manifest'], apply=True, host_stopped=True, expected_fingerprint=p['fingerprint'])
        self.assertEqual(twin.read_bytes(), old); self.assertNotEqual(target.stat().st_ino, inode)

    def test_interruption_after_replace_is_explicitly_restorable(self):
        write = autonomy.write_new
        def fail_marker(path, raw):
            if Path(path) == self.plugin / autonomy.INSTALLED_MARKER: raise OSError('synthetic marker failure')
            return write(path, raw)
        with mock.patch.object(autonomy, 'write_new', side_effect=fail_marker), self.assertRaises(OSError): self.apply()
        manifests = list((self.root / 'autonomy').glob('*/patch.json')); self.assertEqual(len(manifests), 1)
        with self.assertRaises(policy.PatchError): self.preview()
        p = autonomy.restore(self.plugin, manifests[0])
        self.assertEqual(p['restore'], [autonomy.TARGET])
        autonomy.restore(self.plugin, manifests[0], apply=True, host_stopped=True, expected_fingerprint=p['fingerprint'])
        self.assertEqual((self.plugin / autonomy.TARGET).read_bytes(), self.parents[autonomy.TARGET]); self.assert_unowned()

    def test_restore_rejects_later_adapter_or_saved_original_changes(self):
        result = self.apply(); target = self.plugin / autonomy.TARGET; good = target.read_bytes()
        target.write_bytes(good + b'\n// later independent change\n')
        with self.assertRaises(policy.PatchError): autonomy.restore(self.plugin, result['manifest'])
        target.write_bytes(good)
        original = Path(result['backup']) / autonomy.TARGET; original.write_bytes(original.read_bytes() + b'\n')
        with self.assertRaises(policy.PatchError): autonomy.restore(self.plugin, result['manifest'])

    def test_shared_package_lock_refuses_a_second_operator(self):
        p = self.preview()
        cmd = [sys.executable, '-B', str(ROOT / 'scripts/patch_onebot_autonomy.py'), '--plugin-dir', str(self.plugin),
               '--source-repo', str(self.source), '--images-manifest', str(self.images), '--apply', '--host-stopped',
               '--expected-fingerprint', p['fingerprint'], '--backup-dir', str(self.root / 'blocked-backup')]
        with replay.package_lock(self.plugin):
            result = subprocess.run(cmd, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn('owns this package', result.stderr.decode())
        self.assertFalse((self.root / 'blocked-backup').exists())
        self.assert_unowned()

    def test_identical_explicit_source_is_noop_without_marker_or_backup(self):
        (self.source / 'plugin/lib/onebot-policy-adapter.js').write_bytes(FIXTURE.read_bytes())
        p = self.preview()
        self.assertEqual(p['status'], 'not_needed')
        self.assertFalse(self.apply(p)['applied'])
        self.assertFalse((self.plugin / autonomy.INSTALLED_MARKER).exists())
        self.assertFalse((self.root / 'autonomy').exists())
        self.assert_unowned()

    def test_final_whole_readback_failure_never_claims_success(self):
        inspect = autonomy.inspect
        def fail_final(*args, **kwargs):
            result = inspect(*args, **kwargs)
            if (self.plugin / autonomy.INSTALLED_MARKER).exists():
                raise policy.PatchError('synthetic final whole readback failure')
            return result
        with mock.patch.object(autonomy, 'inspect', side_effect=fail_final), self.assertRaisesRegex(policy.PatchError, 'final whole'):
            self.apply()
        manifests = list((self.root / 'autonomy').glob('*/patch.json'))
        self.assertEqual(len(manifests), 1)
        self.assertTrue((manifests[0].parent / 'interrupted.json').is_file())
        self.assertEqual(self.preview()['status'], 'already_patched')
        p = autonomy.restore(self.plugin, manifests[0])
        autonomy.restore(self.plugin, manifests[0], apply=True, host_stopped=True, expected_fingerprint=p['fingerprint'])
        self.assert_unowned()

    def test_relative_package_and_backup_inside_source_are_refused(self):
        with self.assertRaises(policy.PatchError):
            autonomy.patch('.', self.source, self.images, apply=True, host_stopped=True, backup_dir=self.root / 'bad')
        p = self.preview()
        with self.assertRaises(policy.PatchError):
            autonomy.patch(self.plugin, self.source, self.images, apply=True, host_stopped=True,
                           expected_fingerprint=p['fingerprint'], backup_dir=self.source / 'private-backup')
        self.assertFalse((self.source / 'private-backup').exists())
        self.assert_unowned()

    def test_cli_preview_is_machine_readable_and_requires_explicit_source(self):
        cmd = [sys.executable, '-B', str(ROOT / 'scripts/patch_onebot_autonomy.py'), '--plugin-dir', str(self.plugin), '--images-manifest', str(self.images)]
        no_source = subprocess.run(cmd, capture_output=True, timeout=30)
        self.assertEqual(no_source.returncode, 2)
        p = subprocess.run([*cmd, '--source-repo', str(self.source)], capture_output=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(json.loads(p.stdout)['status'], 'patch_needed')
        self.assertFalse((self.root / 'autonomy').exists())


if __name__ == '__main__': unittest.main()
