"""Temporary intent DBs only; projection never starts agents or creates DBs."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import autonomy_intents_panel as panel

NOW = 1800000000000


class IntentPanelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='intent-panel-fixture-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identity = self.root / 'identity.json'
        self.identity.write_text('{"adminQQ":["101"],"fanGroupQQ":"202"}')
        self.database = self.root / 'autonomy-intents.sqlite3'
        self.workspace = panel._workspace(str(self.root))
        self.preset = panel._preset('Fixture')

    def snapshot(self):
        return panel.intent_snapshot(workspace=str(self.root), agent_preset='Fixture',
            identity_file=self.identity, data_directory=self.root, now=NOW)

    def insert(self, *, scope='private:101', workspace=None, stored_owner=None, status='active', mutate=None):
        workspace = workspace or self.workspace
        record = {'schema': 'dsh-autonomy-intent/v1', 'owner': stored_owner or
                  {'workspace_key': workspace, 'agent_key': self.preset}, 'scope': scope,
                  'intent_id': 'fixture-intent', 'revision': 1, 'goal': '<b>继续看视频</b>',
                  'reason': '想了解这个话题', 'next_step': '读取一条推荐的资料',
                  'agent_summary': '这是我保存的打算', 'status': status,
                  'next_review_at': NOW + 10000, 'created_at': NOW - 2000,
                  'updated_at': NOW - 1000, 'summary_is_agent_report': True,
                  'tool_receipts': [{'tool_name': 'read_image', 'outcome': 'returned',
                                     'time': NOW - 1000, 'receipt_key': 'PRIVATE_RECEIPT'}]}
        if mutate:
            mutate(record)
        key = hashlib.sha256(('\0'.join((workspace, self.preset, scope))).encode()).hexdigest()
        with sqlite3.connect(self.database) as connection:
            connection.execute('PRAGMA user_version=1')
            connection.execute('CREATE TABLE IF NOT EXISTS intent_heads(owner_scope_key TEXT,intent_id TEXT,revision INTEGER,status TEXT,updated_at INTEGER,record_json TEXT,PRIMARY KEY(owner_scope_key,intent_id))')
            connection.execute('INSERT INTO intent_heads VALUES(?,?,?,?,?,?)',
                (key, 'fixture-intent', 1, status, NOW - 1000, json.dumps(record)))

    def test_absent_does_not_create_database_or_sidecars(self):
        before = set(self.root.iterdir())
        self.assertEqual(self.snapshot()['status'], 'not_started')
        self.assertEqual(set(self.root.iterdir()), before)

    def test_scoped_reports_and_receipts_are_distinct_and_read_only(self):
        self.insert()
        raw = self.database.read_bytes()
        result = self.snapshot()
        self.assertEqual(result['status'], 'ok')
        plan = result['plans'][0]
        self.assertEqual(plan['receipt_outcomes'], {'returned': 1})
        self.assertIs(plan['summary_is_agent_report'], True)
        self.assertEqual(plan['goal'], '<b>继续看视频</b>')
        output = json.dumps(result)
        for private in ('private:101', 'PRIVATE_RECEIPT', self.workspace, 'fixture-intent', 'read_image'):
            self.assertNotIn(private, output)
        self.assertEqual(self.database.read_bytes(), raw)

    def test_other_owner_other_recipient_and_completed_plans_are_excluded(self):
        self.insert(workspace=self.workspace + '-foreign')
        self.insert(scope='private:303')
        self.insert(scope='group:202', status='completed')
        result = self.snapshot()
        self.assertEqual(result['status'], 'empty')
        self.assertEqual(result['plans'], [])

    def test_stored_owner_mismatch_never_exposes_report(self):
        self.insert(stored_owner={'workspace_key': 'foreign', 'agent_key': self.preset})
        self.assertEqual(self.snapshot()['status'], 'unavailable')
        self.assertEqual(self.snapshot()['plans'], [])

    def test_malformed_row_does_not_leave_partial_reports(self):
        self.insert()
        self.insert(scope='group:202', mutate=lambda record: record.update(revision=2))
        self.assertEqual(self.snapshot()['status'], 'unavailable')
        self.assertEqual(self.snapshot()['plans'], [])

    def test_unknown_schema_and_database_version_are_unavailable(self):
        self.insert()
        with sqlite3.connect(self.database) as connection:
            connection.execute('PRAGMA user_version=2')
        self.assertEqual(self.snapshot()['status'], 'unavailable')

    def test_live_wal_reads_the_committed_plan_via_existing_sqlite_coordination(self):
        self.insert()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('UPDATE intent_heads SET record_json=record_json')
            connection.commit()
            self.assertTrue(Path(str(self.database) + '-wal').exists())
            result = self.snapshot()
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['plans'][0]['status'], 'active')
        finally:
            connection.close()

    def test_database_hardlink_is_not_read(self):
        self.insert()
        os.link(self.database, self.root / 'other.sqlite3')
        self.assertEqual(self.snapshot()['status'], 'unavailable')

    def test_bad_config_cannot_select_a_different_owner(self):
        self.insert()
        self.identity.write_text('{"adminQQ":true}')
        self.assertEqual(self.snapshot()['status'], 'unavailable')

    def test_invalid_falsy_group_never_borrows_a_valid_fallback_group(self):
        self.insert(scope='group:202')
        for group in (False, [], {}, 0):
            with self.subTest(group=group):
                self.identity.write_text(json.dumps({'adminQQ':'101','fanGroupQQ':group,'fanGroup':'202'}))
                result = panel.intent_snapshot(workspace=str(self.root), agent_preset='Fixture',
                    identity_file=self.identity, data_directory=self.root, group_fallback='202', now=NOW)
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(result['plans'], [])

    def test_real_javascript_store_is_readable_without_changing_its_database(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node is required for the intent store contract')
        module = (Path(__file__).resolve().parents[1] / 'plugin/lib/autonomy-intents.js').as_uri()
        source = '''import {createIntentStore} from %s;
const store=createIntentStore({file:process.argv[1],botBinding:'inert-fixture',now:()=>1800000000000});
store.update({owner:JSON.parse(process.argv[2]),scope:'private:101',operationKey:'a'.repeat(64),action:'create',
 fields:{goal:'继续这个话题',reason:'想了解',next_step:'读取真实资料',agent_summary:'尚未完成',status:'active'},receipts:[]});
''' % json.dumps(module)
        done = subprocess.run([node, '--input-type=module', '-e', source, str(self.database),
            json.dumps({'workspace_key': self.workspace, 'agent_key': self.preset})],
            capture_output=True, timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(done.returncode, 0, done.stderr.decode(errors='replace'))
        raw = self.database.read_bytes()
        before_files = set(self.root.iterdir())
        result = self.snapshot()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['plans'][0]['goal'], '继续这个话题')
        self.assertEqual(result['plans'][0]['receipt_outcomes'], {})
        self.assertEqual(self.database.read_bytes(), raw)
        self.assertEqual(set(self.root.iterdir()), before_files)


if __name__ == '__main__':
    unittest.main()
