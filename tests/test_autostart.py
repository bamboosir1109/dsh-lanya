from pathlib import Path
import importlib.util
import sys
import tempfile
import unittest

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
spec=importlib.util.spec_from_file_location('autostart_candidate',SCRIPTS/'make_autostart.py')
candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)

class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='autostart-candidate-')
        self.root=Path(self.temp.name);self.addCleanup(self.clean)
        self.launcher=self.root/'workspace with spaces/napcat/start-all.ps1'
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_bytes(((SCRIPTS/'start-all.ps1')).read_bytes())
        self.startup=self.root/'Startup';self.backups=self.root/'workspace with spaces/originals'
    def clean(self):
        assert self.root.resolve().parent==Path(tempfile.gettempdir()).resolve()
        assert self.root.name.startswith('autostart-candidate-')
        self.temp.cleanup()
    def run_config(self,**options):return candidate.configure(self.launcher,self.startup,self.backups,**options)
    def test_whatif_writes_nothing_and_does_not_claim_registration(self):
        before=self.launcher.read_bytes();result=self.run_config(what_if=True)
        self.assertFalse(result['registered']);self.assertFalse(result['backup_retained'])
        self.assertFalse(self.startup.exists());self.assertFalse(self.backups.exists());self.assertEqual(self.launcher.read_bytes(),before)
    def test_unicode_hidden_entry_reuses_canonical_script_and_is_idempotent(self):
        raw=self.launcher.read_bytes()
        self.launcher=self.root/'workspace \u6d4b\u8bd5 with spaces/napcat/start-all.ps1'
        self.launcher.parent.mkdir(parents=True);self.launcher.write_bytes(raw)
        first=self.run_config();self.assertTrue(first['registered']);self.assertFalse(first['automatic_run'])
        text=(self.startup/'lanya-start-all.vbs').read_text(encoding='utf-16')
        self.assertIn('-WindowStyle Hidden',text);self.assertIn(', 0, False',text)
        self.assertIn(str(self.launcher),text);self.assertEqual(self.launcher.read_bytes(),raw)
        self.assertFalse(self.run_config()['changed'])
    def test_owned_legacy_entry_is_archived_exactly_before_removing_duplicate(self):
        self.startup.mkdir();legacy=self.startup/'lanYa-start-all.cmd'
        raw=('@echo off\r\nstart "" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "'+str(self.launcher)+'"\r\n').encode('ascii')
        legacy.write_bytes(raw);result=self.run_config()
        self.assertTrue(result['backup_retained']);self.assertFalse(legacy.exists())
        originals=list(self.backups.glob('*.cmd.original'));self.assertEqual(len(originals),1);self.assertEqual(originals[0].read_bytes(),raw)
    def test_unknown_entry_cannot_be_overwritten_or_add_a_second_startup(self):
        self.startup.mkdir();legacy=self.startup/'lanYa-start-all.cmd';legacy.write_bytes(b'unknown operator entry')
        with self.assertRaises(candidate.StartupError):self.run_config()
        self.assertEqual(legacy.read_bytes(),b'unknown operator entry')
        self.assertFalse((self.startup/'lanya-start-all.vbs').exists());self.assertFalse(self.backups.exists())

if __name__=='__main__':unittest.main()
