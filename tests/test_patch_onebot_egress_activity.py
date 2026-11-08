"""Real complete parent chain, private originals and physical-inode restores."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import patch_onebot_egress_activity as activity
import patch_meme_qq_picker as picker
from patch_onebot_policy import PatchError
from patch_onebot_replay import identity, sealed

spec = importlib.util.spec_from_file_location('activity_real_picker_fixture', ROOT / 'tests/test_patch_meme_qq_picker.py')
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)


class EgressActivityPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.MemePickerPatchTests.setUpClass()
        cls.addClassCleanup(fixture.MemePickerPatchTests.doClassCleanups)

    def setUp(self):
        self.base = fixture.MemePickerPatchTests(); self.base.setUp(); self.addCleanup(self.base.doCleanups)
        self.meme, self.onebot = self.base.meme, self.base.onebot
        self.parent = Path(self.base.apply()['manifest'])
        self.root = self.base.parent.root
        self.backups = self.root / 'activity-backups'
        self.module = self.onebot / activity.MODULE
        self.before = identity(self.module)
        self.parent_fp = picker.inspect(self.meme, self.onebot, self.parent, canonical=False)[0]['fingerprint']
        self.original_parent = {path: identity(path) for directory in (self.meme, self.onebot)
                                for path in directory.rglob('*') if path.is_file() and path != self.module
                                and path.name != '.lanya-replay-isolation.lock'}

    def preview(self):
        return activity.patch(self.meme, self.onebot, self.parent)

    def apply(self, fingerprint=None):
        return activity.patch(self.meme, self.onebot, self.parent, apply=True, host_stopped=True,
            expected_fingerprint=fingerprint or self.preview()['fingerprint'], backup_dir=self.backups)

    def restore(self, manifest):
        view = activity.restore(self.meme, self.onebot, manifest)
        return activity.restore(self.meme, self.onebot, manifest, apply=True, host_stopped=True,
                                expected_fingerprint=view['fingerprint'])

    def unchanged(self):
        for path, before in self.original_parent.items(): self.assertEqual(identity(path), before, str(path))
        self.assertEqual(self.base.parent.sentinel.read_bytes(), b'{"synthetic_policy":"never-open-or-change"}\n')

    def test_complete_apply_restore_preserves_old_inode_and_real_picker_parent(self):
        view = self.preview(); self.assertFalse(self.backups.exists())
        self.assertEqual(view['status'], 'patch_needed')
        applied = self.apply(view['fingerprint'])
        record = json.loads(Path(applied['manifest']).read_bytes())
        slot = self.module.parent / record['slot']
        self.assertEqual(sealed(identity(slot)), sealed(self.before))
        self.assertEqual(slot.stat().st_nlink, 1)
        self.assertNotEqual(identity(self.module)['signature'][1], self.before['signature'][1])
        self.assertEqual(self.module.read_bytes(), activity.SOURCE.read_bytes())
        self.assertEqual(self.preview()['status'], 'already_patched')
        self.assertFalse(self.apply()['applied']); self.unchanged()
        self.restore(applied['manifest'])
        self.assertEqual(identity(self.module), self.before)
        self.assertFalse(slot.exists())
        self.assertEqual(picker.inspect(self.meme, self.onebot, self.parent, canonical=False)[0]['fingerprint'], self.parent_fp)
        self.assertEqual(activity.restore(self.meme, self.onebot, applied['manifest'])['status'], 'already_restored')
        self.unchanged()

    def test_stale_nonboolean_and_unknown_inputs_refuse_before_apply(self):
        for value in ('false', 1, None):
            with self.assertRaises(PatchError):
                activity.patch(self.meme, self.onebot, self.parent, apply=True, host_stopped=value,
                    expected_fingerprint=self.preview()['fingerprint'], backup_dir=self.backups)
        with self.assertRaises(PatchError): self.apply('0' * 64)
        self.assertFalse(self.backups.exists()); self.assertEqual(identity(self.module), self.before)
        self.module.write_bytes(b'// unknown later file\n')
        with self.assertRaises(PatchError): self.preview()

    def test_after_without_marker_has_explicit_restore_and_needs_no_current_source(self):
        write = activity.write_new
        def interrupt(path, raw):
            if Path(path) == self.onebot / activity.MARKER: raise OSError('Synthetic marker interruption')
            return write(path, raw)
        with mock.patch.object(activity, 'write_new', side_effect=interrupt), self.assertRaises(OSError): self.apply()
        manifest = next(self.backups.glob('*/patch.json'))
        self.assertFalse((self.onebot / activity.MARKER).exists())
        with self.assertRaises(PatchError): self.preview()
        with mock.patch.object(activity, 'SOURCE', self.root / 'absent-source.js'):
            self.restore(manifest)
        self.assertEqual(identity(self.module), self.before); self.unchanged()

    def test_slot_loss_or_before_bytes_new_inode_refuses_whole_restore(self):
        applied = self.apply(); manifest = Path(applied['manifest'])
        record = json.loads(manifest.read_bytes()); slot = self.module.parent / record['slot']
        saved = slot.read_bytes(); slot.unlink()
        with self.assertRaises(PatchError): activity.restore(self.meme, self.onebot, manifest)
        replacement = self.module.with_name('unknown-before.js'); replacement.write_bytes(saved)
        activity.os.replace(replacement, self.module)
        with self.assertRaises(PatchError): activity.restore(self.meme, self.onebot, manifest)

    def test_unused_slot_and_consumed_slot_interruption_restore_safely(self):
        replace = activity.os.replace
        def stop_before(src, dst):
            if Path(dst) == self.module: raise OSError('Synthetic pre-replace interruption')
            return replace(src, dst)
        with mock.patch.object(activity.os, 'replace', side_effect=stop_before), self.assertRaises(OSError): self.apply()
        first = next(self.backups.glob('*/patch.json'))
        self.assertEqual(identity(self.module), self.before)
        self.assertTrue(activity.restore(self.meme, self.onebot, first)['remove_slot'])
        self.restore(first); self.assertEqual(identity(self.module), self.before)
        second = self.apply(); manifest = second['manifest']
        unlink = Path.unlink
        def stop_marker(path, *args, **kwargs):
            if path == self.onebot / activity.MARKER: raise OSError('Synthetic marker-removal interruption')
            return unlink(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', autospec=True, side_effect=stop_marker), self.assertRaises(OSError): self.restore(manifest)
        self.assertEqual(identity(self.module), self.before)
        self.assertTrue((self.onebot / activity.MARKER).exists())
        self.restore(manifest); self.unchanged()


if __name__ == '__main__': unittest.main()
