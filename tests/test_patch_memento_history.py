"""Real generic 0.5.23 programs in temp; never read installation or memory DB."""
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
import patch_memento_history as patcher
import patch_memento_visibility as visibility


class NativeHistoryPatchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="memento-native-patch-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.plugin = self.root / "plugin"
        shutil.copytree(ROOT / "tests/fixtures/memento_native_0_5_23", self.plugin)

    def visible(self):
        visibility.patch_package(self.plugin, apply=True, backup_root=self.root / "visibility-backup")

    def tree(self):
        return {p.relative_to(self.plugin).as_posix():p.read_bytes() for p in self.plugin.rglob('*') if p.is_file()}

    def apply(self):
        return patcher.patch(self.plugin, apply=True, host_stopped=True, expected_fingerprint=patcher.patch(self.plugin)['fingerprint'], backup_dir=self.root/'history-backup')

    def restore(self, manifest):
        preview = patcher.restore(self.plugin, manifest)
        return patcher.restore(self.plugin, manifest, apply=True, host_stopped=True, expected_fingerprint=preview['fingerprint'])

    def test_requires_visibility_first_and_preview_is_read_only(self):
        before = self.tree()
        with self.assertRaises(patcher.PatchError): patcher.patch(self.plugin)
        self.assertEqual(before,self.tree())
        self.visible();before=self.tree()
        result=patcher.patch(self.plugin)
        self.assertEqual(result['status'],'patch_needed')
        self.assertEqual(before,self.tree())
        self.assertFalse((self.root/'history-backup').exists())

    def test_apply_replay_and_reverse_order_restore_preserve_originals(self):
        original=self.tree();self.visible();visible=self.tree()
        result=self.apply();manifest=Path(result['backup'])/'patch.json'
        self.assertEqual(patcher.patch(self.plugin)['status'],'already_patched')
        self.assertFalse(self.apply()['applied'])
        for name in patcher.TARGETS:self.assertEqual((manifest.parent/name).read_bytes(),visible[name])
        with self.assertRaises(visibility.PatchConflict): visibility.restore_package(self.plugin,self.root/'visibility-backup/manifest.json')
        self.restore(manifest)
        self.assertEqual(self.tree(),visible)
        visibility.restore_package(self.plugin,self.root/'visibility-backup/manifest.json',apply=True)
        # The shared persistent OS lock is deliberately retained, never unlinked.
        for name,raw in original.items():self.assertEqual((self.plugin/name).read_bytes(),raw)

    def test_apply_needs_stopped_host_fresh_fingerprint_private_backup(self):
        self.visible();before=self.tree();preview=patcher.patch(self.plugin)
        for options in [{},{'host_stopped':True},{'host_stopped':True,'expected_fingerprint':'stale','backup_dir':self.root/'backup'}]:
            with self.subTest(options=options),self.assertRaises(patcher.PatchError):patcher.patch(self.plugin,apply=True,**options)
        with self.assertRaises(patcher.PatchError):patcher.patch(self.plugin,apply=True,host_stopped=True,expected_fingerprint=preview['fingerprint'],backup_dir=ROOT/'private-backup-refused')
        self.assertEqual(before,self.tree())

    def test_modified_target_dependency_and_marker_refuse(self):
        self.visible()
        for name in [*patcher.TARGETS,'lib/constants.mjs']:
            path=self.plugin/name;raw=path.read_bytes();path.write_bytes(raw+b'\n// later edit\n')
            with self.subTest(name=name),self.assertRaises(Exception):patcher.patch(self.plugin)
            path.write_bytes(raw)
        (self.plugin/patcher.MODULE).write_text('unknown owned module')
        with self.assertRaises(patcher.PatchError):patcher.patch(self.plugin)

    def test_inode_change_invalidates_apply_preview(self):
        self.visible();preview=patcher.patch(self.plugin)
        target=self.plugin/'lib/store.mjs';fresh=self.root/'fresh.mjs';fresh.write_bytes(target.read_bytes());os.replace(fresh,target)
        with self.assertRaises(patcher.PatchError):patcher.patch(self.plugin,apply=True,host_stopped=True,expected_fingerprint=preview['fingerprint'],backup_dir=self.root/'backup')

    def test_os_lock_excludes_another_operator_without_private_backup(self):
        self.visible();preview=patcher.patch(self.plugin)
        with visibility.package_lock(self.plugin):
            run=subprocess.run([sys.executable,'-B',str(ROOT/'scripts/patch_memento_history.py'),'--package-root',str(self.plugin),'--apply','--host-stopped','--expected-fingerprint',preview['fingerprint'],'--backup-dir',str(self.root/'blocked-backup')],capture_output=True)
        self.assertNotEqual(run.returncode,0)
        self.assertFalse((self.root/'blocked-backup').exists())

    def test_partial_application_is_recoverable_after_each_target(self):
        self.visible();before=self.tree();replace=patcher.os.replace;count=0
        def interrupted(source,target):
            nonlocal count
            if Path(target)==self.plugin/'lib/protocol.mjs':raise OSError('fixture interruption')
            count+=1;return replace(source,target)
        with mock.patch.object(patcher.os,'replace',side_effect=interrupted),self.assertRaises(OSError):self.apply()
        self.assertEqual(count,1)
        manifest=next((self.root/'history-backup').glob('*/patch.json'))
        self.restore(manifest)
        self.assertEqual(before,self.tree())

    def test_later_edit_prevents_whole_restore_and_does_not_touch_memory(self):
        self.visible();result=self.apply();manifest=Path(result['backup'])/'patch.json'
        (self.plugin/'private.fixture').write_bytes(b'no memory or config read by patcher')
        for name in [*patcher.TARGETS,patcher.MODULE]:
            target=self.plugin/name;raw=target.read_bytes();target.write_bytes(raw+b'\n// later edit\n');before=self.tree()
            with self.subTest(name=name),self.assertRaises(patcher.PatchError):patcher.restore(self.plugin,manifest)
            self.assertEqual(before,self.tree());target.write_bytes(raw)
        self.restore(manifest)
        self.assertEqual((self.plugin/'private.fixture').read_bytes(),b'no memory or config read by patcher')

    def test_private_original_and_manifest_tampering_or_stale_restore_refuse(self):
        self.visible();result=self.apply();manifest=Path(result['backup'])/'patch.json'
        for target in [manifest,manifest.parent/'lib/store.mjs']:
            raw=target.read_bytes();target.write_bytes(raw+b'\ninvalid')
            with self.assertRaises(Exception):patcher.restore(self.plugin,manifest)
            target.write_bytes(raw)
        preview=patcher.restore(self.plugin,manifest)
        target=self.plugin/patcher.MODULE;fresh=self.root/'replacement.mjs';fresh.write_bytes(target.read_bytes());os.replace(fresh,target)
        with self.assertRaises(patcher.PatchError):patcher.restore(self.plugin,manifest,apply=True,host_stopped=True,expected_fingerprint=preview['fingerprint'])

    def test_apply_and_restore_break_hardlinks_without_changing_peers(self):
        self.visible();original=(self.plugin/'lib/store.mjs').read_bytes();peer=self.root/'original-peer.mjs';os.link(self.plugin/'lib/store.mjs',peer)
        result=self.apply();self.assertEqual(peer.read_bytes(),original)
        patched=(self.plugin/'lib/store.mjs').read_bytes();peer2=self.root/'patched-peer.mjs';os.link(self.plugin/'lib/store.mjs',peer2)
        self.restore(Path(result['backup'])/'patch.json')
        self.assertEqual(peer2.read_bytes(),patched)


if __name__=='__main__':unittest.main()
