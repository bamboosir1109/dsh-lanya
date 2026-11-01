"""Real old layers and new updater on synthetic packages; never a live host."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import patch_meme_qq_picker as picker
import patch_autonomy_continuity as continuity
from patch_onebot_policy import PatchError
from patch_onebot_replay import identity, sealed
from patch_onebot_policy import sha256

spec=importlib.util.spec_from_file_location('meme_picker_parent_fixture',ROOT/'tests/test_patch_onebot_autonomy.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
NATIVE=ROOT/'tests/fixtures/meme_qq_picker'


class MemePickerPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AutonomyPatchTests.setUpClass()
        cls.addClassCleanup(fixture.AutonomyPatchTests.doClassCleanups)

    def setUp(self):
        self.parent=fixture.AutonomyPatchTests();self.parent.setUp();self.addCleanup(self.parent.doCleanups)
        self.parent.apply();self.onebot=self.parent.plugin
        backup=self.parent.root/'continuity';backup.mkdir()
        view=continuity.patch(self.onebot,ROOT)
        continuity.patch(self.onebot,ROOT,apply=True,host_stopped=True,
                         expected_fingerprint=view['fingerprint'],backup_dir=backup)
        self.meme=self.parent.root/'meme';self.meme.mkdir()
        for name in ('index.js','memes.js'):shutil.copyfile(NATIVE/name,self.meme/name)
        (self.meme/'package.json').write_text('{"name":"dsh-meme","version":"0.1.47","type":"module"}',encoding='utf-8')
        self.backup=self.parent.root/'picker-backups'
        self.before_bridge=identity(self.onebot/'lib/bridge.js')
        self.parent_fp=continuity.inspect(self.onebot)[0]['fingerprint']
        # Old public upstream bridge fixture differs from the installed reviewed
        # build. Only this synthetic chain's exact hash is substituted here;
        # production accepts solely its pinned actual 0.8.1 bridge (6f692a...).
        seam=mock.patch.dict(picker.KNOWN,{'bridge.js':sha256((self.onebot/'lib/bridge.js').read_bytes())})
        seam.start();self.addCleanup(seam.stop)

    def preview(self):return picker.patch(self.meme,self.onebot)
    def apply(self,fp=None):
        return picker.patch(self.meme,self.onebot,apply=True,host_stopped=True,
                            expected_fingerprint=fp or self.preview()['fingerprint'],backup_dir=self.backup)
    def restore(self,manifest):
        view=picker.restore(self.meme,self.onebot,manifest)
        return picker.restore(self.meme,self.onebot,manifest,apply=True,host_stopped=True,expected_fingerprint=view['fingerprint'])

    def test_real_parent_preview_apply_restore_preserves_native_original_inode(self):
        view=self.preview();self.assertEqual(view['status'],'patch_needed');self.assertFalse(self.backup.exists())
        result=self.apply(view['fingerprint']);record=json.loads(Path(result['manifest']).read_text())
        slot=self.onebot/'lib'/record['bridge_slot']
        self.assertEqual(sealed(identity(slot)),sealed(self.before_bridge));self.assertEqual(slot.stat().st_nlink,1)
        self.assertNotEqual(identity(self.onebot/'lib/bridge.js')['signature'][1],self.before_bridge['signature'][1])
        self.assertEqual(self.preview()['status'],'already_patched')
        self.assertFalse(self.apply()['applied'])
        self.assertEqual(self.restore(result['manifest'])['status'],'restored')
        self.assertEqual(identity(self.onebot/'lib/bridge.js'),self.before_bridge)
        self.assertEqual(continuity.inspect(self.onebot)[0]['fingerprint'],self.parent_fp)
        self.assertFalse(slot.exists());self.assertFalse((self.meme/picker.MODULE).exists())
        self.assertEqual(picker.restore(self.meme,self.onebot,result['manifest'])['status'],'already_restored')
        self.assertEqual(self.parent.sentinel.read_bytes(),b'{"synthetic_policy":"never-open-or-change"}\n')

    def test_stale_fingerprint_and_nonboolean_host_are_refused_before_write(self):
        for value in ('false',1,None):
            with self.subTest(value=value),self.assertRaises(PatchError):
                picker.patch(self.meme,self.onebot,apply=True,host_stopped=value,backup_dir=self.backup)
        with self.assertRaises(PatchError):self.apply('0'*64)
        self.assertFalse(self.backup.exists());self.assertEqual(identity(self.onebot/'lib/bridge.js'),self.before_bridge)

    def test_existing_helper_or_unknown_native_bytes_refused(self):
        helper=self.meme/picker.MODULE;helper.write_bytes(b'// unowned')
        with self.assertRaises(PatchError):self.preview()
        helper.unlink();(self.meme/'memes.js').write_bytes(b'// unknown')
        with self.assertRaises(PatchError):self.preview()

    def test_partial_replace_has_full_originals_and_resumes_explicit_restore(self):
        actual_replace=picker.os.replace
        calls=[]
        def broken(src,dst):
            calls.append(str(dst))
            if Path(dst)==self.onebot/'lib/bridge.js':raise OSError('synthetic interruption')
            return actual_replace(src,dst)
        with mock.patch.object(picker.os,'replace',side_effect=broken),self.assertRaises(OSError):self.apply()
        manifests=list(self.backup.glob('*/patch.json'));self.assertEqual(len(manifests),1)
        self.assertTrue((manifests[0].parent/'interrupted.json').exists())
        with self.assertRaises(PatchError):self.preview()
        self.restore(manifests[0])
        self.assertEqual(identity(self.onebot/'lib/bridge.js'),self.before_bridge)
        self.assertEqual(continuity.inspect(self.onebot)[0]['fingerprint'],self.parent_fp)

    def test_slot_missing_changed_and_unknown_later_program_are_refused_whole_batch(self):
        result=self.apply();manifest=Path(result['manifest']);record=json.loads(manifest.read_text())
        slot=self.onebot/'lib'/record['bridge_slot'];raw=slot.read_bytes()
        stage=slot.with_suffix('.new');stage.write_bytes(raw);os.replace(stage,slot)
        before=(self.meme/'index.js').read_bytes()
        with self.assertRaises(PatchError):picker.restore(self.meme,self.onebot,manifest)
        self.assertEqual((self.meme/'index.js').read_bytes(),before)

    def test_marker_or_private_generated_tamper_is_refused(self):
        result=self.apply();manifest=Path(result['manifest'])
        generated=manifest.parent/'generated/meme/memes.js'
        generated.write_bytes(generated.read_bytes()+b'\n// tamper')
        with self.assertRaises(PatchError):picker.restore(self.meme,self.onebot,manifest)
        self.assertTrue((self.meme/picker.MARKER).exists())

    def test_restore_interruption_after_physical_bridge_can_resume(self):
        result=self.apply();actual_replace=picker.os.replace
        def broken(src,dst):
            if Path(dst)==self.meme/'memes.js':raise OSError('synthetic restore interruption')
            return actual_replace(src,dst)
        with mock.patch.object(picker.os,'replace',side_effect=broken),self.assertRaises(OSError):self.restore(result['manifest'])
        self.assertEqual(identity(self.onebot/'lib/bridge.js'),self.before_bridge)
        self.restore(result['manifest'])
        self.assertEqual(continuity.inspect(self.onebot)[0]['fingerprint'],self.parent_fp)

    def test_missing_physical_slot_with_new_bridge_refuses_before_any_restore(self):
        result=self.apply();manifest=Path(result['manifest']);record=json.loads(manifest.read_text())
        (self.onebot/'lib'/record['bridge_slot']).unlink()
        before=(self.meme/'index.js').read_bytes()
        with self.assertRaises(PatchError):picker.restore(self.meme,self.onebot,manifest)
        self.assertEqual((self.meme/'index.js').read_bytes(),before)
        self.assertTrue((self.meme/picker.MARKER).exists())

    def test_private_restore_is_independent_of_future_or_missing_canonical_source(self):
        result=self.apply()
        with mock.patch.object(picker,'SOURCE',self.parent.root/'absent-future-helper.mjs'):
            with self.assertRaises(OSError):self.preview()
            self.assertEqual(self.restore(result['manifest'])['status'],'restored')
        self.assertEqual(identity(self.onebot/'lib/bridge.js'),self.before_bridge)
        self.assertEqual(continuity.inspect(self.onebot)[0]['fingerprint'],self.parent_fp)

    def test_before_bridge_bytes_on_new_inode_refuse_entire_restore_before_writes(self):
        result=self.apply();manifest=Path(result['manifest'])
        bridge=self.onebot/'lib/bridge.js'
        stage=bridge.with_suffix('.unowned')
        stage.write_bytes((manifest.parent/'original/onebot/lib/bridge.js').read_bytes())
        os.replace(stage,bridge)
        before={key:identity(picker.target({'meme':self.meme,'onebot':self.onebot},key)) for key in picker.KEYS}
        with self.assertRaises(PatchError):picker.restore(self.meme,self.onebot,manifest)
        self.assertEqual({key:identity(picker.target({'meme':self.meme,'onebot':self.onebot},key)) for key in picker.KEYS},before)
        self.assertTrue((self.meme/picker.MARKER).exists())


if __name__=='__main__':unittest.main()
