"""Synthetic project settings and actual JS journal interoperability; no QQ."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import account_activity as activity
import budget_runtime
import panel_autonomy as store
import project_conf


class AccountActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='account settings ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'napcat/config/project.json'
        self.project.parent.mkdir(parents=True)
        self.original = {'dataDirName': 'fixture', 'decisions': {'mode': 'unified'},
                         'mind': {'opportunityMin': 6}, 'qqBackend': {'provider': 'snowluma', 'revision': 4},
                         'unrelated': {'keep': ['these', 'values']}}
        self.project.write_text(json.dumps(self.original), encoding='utf8')
        (self.root / 'fixture').mkdir()
        (self.root / 'fixture/lanya-config.json').write_text('{"botQQ":"303","adminQQ":"101"}', encoding='utf8')
        environment = mock.patch.dict(os.environ, {'LANYA_WORKDIR': str(self.root), 'LANYA_PROJECT_CONFIG': str(self.project)})
        environment.start(); self.addCleanup(environment.stop)
        heartbeat = mock.patch.object(budget_runtime, 'get_snapshot', return_value={'enforcement': {'runtime_gate_bound': True, 'account_activity_bound': True}})
        heartbeat.start(); self.addCleanup(heartbeat.stop)
        project_conf.load(reload=True)

    def test_unstarted_snapshot_is_read_only_and_uses_provider_capabilities(self):
        before = self.project.read_bytes()
        value = activity.snapshot()
        self.assertTrue(value['read_only']); self.assertTrue(value['runtime_loaded'])
        self.assertEqual(value['recent'], []); self.assertFalse(value['settings']['enabled'])
        self.assertTrue(value['settings']['post_supported'])
        self.assertFalse((self.root / 'napcat/account-activity').exists())
        self.assertEqual(self.project.read_bytes(), before)
        self.assertFalse(activity.settings({}, 'napcat')['post_supported'])

    def test_cas_setting_change_retains_full_original_security_and_unrelated_fields(self):
        before, raw = store._record(self.project)
        current = activity.snapshot()
        settings = dict(activity.DEFAULTS, enabled=True, audience='self')
        saved = activity.save_settings({'revision': current['revision'], 'settings': settings})
        self.assertTrue(saved['settings']['enabled']); self.assertNotEqual(saved['revision'], current['revision'])
        after = json.loads(self.project.read_bytes())
        without = json.loads(self.project.read_bytes()); del without['qqBackend']['accountActivity']
        self.assertEqual(without, self.original)
        self.assertEqual(after['qqBackend']['accountActivity'], settings)
        originals = list(self.project.parent.glob('.*.account-*.original.json'))
        self.assertEqual(len(originals), 1); self.assertEqual(originals[0].read_bytes(), raw)
        self.assertEqual(store._record(originals[0])[0]['security_sha256'], before['security_sha256'])
        with self.assertRaises(store.SettingsError): activity.save_settings({'revision': current['revision'], 'settings': settings})
        self.assertEqual(len(list(self.project.parent.glob('.*.account-*.original.json'))), 1)

    def test_invalid_settings_and_wrong_revision_cannot_mutate_config(self):
        before = self.project.read_bytes(); revision = activity.snapshot()['revision']
        for values in ({'enabled': 'yes'}, {'audience': 'public'}, {'dailyPosts': 0}, {'postIntervalHours': True}, {'unknown': True}):
            with self.subTest(values=values), self.assertRaises(store.SettingsError):
                activity.save_settings({'revision': revision, 'settings': dict(activity.DEFAULTS, **values)})
        self.assertEqual(self.project.read_bytes(), before)
        self.assertEqual(list(self.project.parent.glob('.*.account-*')), [])

    def test_real_javascript_journal_is_read_by_python_without_changing_source_rows(self):
        file, binding = activity.journal_path(project_conf.load(reload=True), 'snowluma')
        script = """import {createHash} from 'node:crypto';
const {AccountActivityStore,accountActivitySettings}=await import(process.argv[3]);
const store=new AccountActivityStore(process.argv[1],process.argv[2],()=>Date.parse('2026-11-08T01:00:00Z'));
await store.run({key:createHash('sha256').update('fixture request').digest('hex'),kind:'signature',text:'阳光正好。',audience:'profile'},
 accountActivitySettings({qqBackend:{accountActivity:{enabled:true}}},['account_signature']),()=>{},()=>({}));"""
        done = subprocess.run(['node', '--input-type=module', '-e', script, str(file), binding,
                               (ROOT / 'plugin/lib/account-activity.js').as_uri()],
                              capture_output=True, timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(done.returncode, 0, done.stderr)
        before = hashlib.sha256(file.read_bytes()).hexdigest()
        value = activity.snapshot()
        self.assertEqual(value['recent'][0]['text'], '阳光正好。')
        self.assertEqual(value['recent'][0]['state'], 'accepted')
        self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(), before)
        connection = sqlite3.connect(file)
        try:
            connection.execute("UPDATE account_binding SET binding='wrong account'")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(store.SettingsError): activity.snapshot()


if __name__ == '__main__': unittest.main()
