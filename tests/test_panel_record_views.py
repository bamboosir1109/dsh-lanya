"""Panel GET integration with isolated config/files/SQLite and no external I/O."""
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('panel_records_isolated_base', Path(__file__).with_name('test_panel_bili_main_agent.py'))
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)


class PanelRecordViewTests(unittest.TestCase):
    def setUp(self):
        self.base = fixture.PanelBiliMainAgentTests()
        self.base.setUp(); self.addCleanup(self.base.doCleanups)
        self.panel = self.base.panel

    def get(self, path):
        handler = object.__new__(self.panel.Handler)
        handler.path = path
        handler._json = lambda code, value: (code, value)
        return handler.do_GET()

    def test_bili_get_uses_cached_covers_without_network_or_cache_write(self):
        home = Path(self.panel.BILI_HOME); home.mkdir(parents=True)
        seen = '## 看过的视频\n| 日期 | BV | 标题 | UP | 状态 |\n|---|---|---|---|---|\n| fixture | BV1234567890 | Public fixture | Public author | old record |\n'
        Path(self.panel.BILI_SEEN).write_text(seen, encoding='utf-8')
        cache = Path(self.panel.COVER_CACHE_FILE)
        cache.write_text('{"BV1234567890":"https://cache.invalid/image.jpg"}', encoding='utf-8')
        before = {path.name: path.read_bytes() for path in home.iterdir()}
        with mock.patch.object(self.panel, 'bili_cover_urls', side_effect=AssertionError('GET cannot fetch covers')):
            code, result = self.get('/api/bili?workspace=foreign&session=foreign')
        self.assertEqual(code, 200)
        self.assertEqual(result['bili']['covers'], {'BV1234567890': 'https://cache.invalid/image.jpg'})
        self.assertEqual(result['bili']['mainAgent']['status'], 'unavailable')
        self.assertEqual(before, {path.name: path.read_bytes() for path in home.iterdir()})

    def test_live_unified_exposes_only_sanitized_current_record_projection(self):
        expected = {'status': 'partial', 'generated_at': 1, 'records': [{'tool': 'bili_recommend', 'at': 1, 'status': 'succeeded'}]}
        with mock.patch.object(self.panel, 'autonomy_snapshot', return_value={'mode': 'unified'}), \
                mock.patch.object(self.panel, 'autonomy_label', return_value='fixture'), \
                mock.patch.object(self.panel, 'panel_main_activity', return_value={'recentActivity': expected}):
            code, result = self.get('/api/live')
        self.assertEqual(code, 200)
        self.assertEqual(result['live']['recentActivity'], expected)

    def test_explicit_broken_record_configuration_is_unavailable_without_fallback(self):
        with mock.patch.object(self.panel.project_conf, 'load',
                side_effect=self.panel.project_conf.ConfigError('fixture unavailable')):
            value = self.panel.panel_main_activity()
        self.assertEqual(value['mainAgent']['status'], 'unavailable')
        self.assertEqual(value['mainAgent']['records'], [])

    def test_plan_route_uses_local_configuration_and_ignores_url_owner_selection(self):
        import autonomy_intents_panel
        expected = {'schema':'dsh-autonomy-plans-panel/v1','status':'empty','plans':[],
                    'read_only':True,'private':True,'summary_is_agent_report':True}
        options = {'workspace':str(self.base.root),'agent_preset':'Fixture',
                   'identity_file':str(self.base.root / 'identity.json'),'group_fallback':'',
                   'dsh_home':str(self.base.root / 'home'),'mapping_file':None}
        with mock.patch.object(self.panel, '_panel_native_record_options', return_value=options), \
                mock.patch.object(autonomy_intents_panel, 'intent_snapshot', return_value=expected) as read, \
                mock.patch.object(self.panel, 'autonomy_snapshot', return_value={'mode':'unified'}):
            code, result = self.get('/api/live?workspace=foreign&recipient=303&database=foreign')
        self.assertEqual(code, 200)
        self.assertEqual(result['live']['plans'], expected)
        read.assert_called_once_with(workspace=options['workspace'], agent_preset='Fixture',
            identity_file=options['identity_file'], data_directory=self.panel.DATA, group_fallback='')

    def test_notes_limit_selects_newest_metadata_before_reading_preview(self):
        notes = Path(self.panel.FILE_DIRS['notes']); notes.mkdir()
        for index in range(65):
            path = notes / ('zzz-old-%03d.md' % index); path.write_text('old fixture')
            os.utime(path, ns=(1000000000, 1000000000))
        current = notes / 'activity-current.md'; current.write_text('current fixture')
        os.utime(current, ns=(5000000000, 5000000000))
        import builtins
        original = builtins.open
        reads = []
        def track(path, *args, **kwargs):
            if str(path).startswith(str(notes)): reads.append(Path(path).name)
            return original(path, *args, **kwargs)
        with mock.patch('builtins.open', side_effect=track):
            result = self.panel.list_dir_files('notes', True, 2)
        self.assertEqual(result[0]['name'], current.name)
        self.assertEqual(len(result), 2); self.assertEqual(len(reads), 2)

    def test_works_route_lists_nested_entry_and_excludes_its_own_snapshot(self):
        made = Path(self.panel.FILE_DIRS['made']); (made / 'fixture-game').mkdir(parents=True)
        (made / 'fixture-game/index.html').write_text('synthetic')
        (made / 'fixture-game/engine.js').write_text('synthetic')
        (made / 'lanya-works.json').write_text('{}')
        code, result = self.get('/api/works')
        self.assertEqual(code, 200); self.assertTrue(result['ok'])
        self.assertEqual([row['name'] for row in result['files']], ['fixture-game/index.html'])

    def database(self):
        file = self.base.root / 'memory-fixture.db'
        with contextlib.closing(sqlite3.connect(str(file))) as con:
            con.executescript('''
CREATE TABLE entries(id TEXT,track TEXT,scope TEXT,workspace_key TEXT,agent_key TEXT,text TEXT,source TEXT,tags TEXT,version INTEGER,created_at INTEGER,updated_at INTEGER,recall_count INTEGER);
CREATE TABLE proposals(id TEXT,kind TEXT,track TEXT,scope TEXT,text TEXT,status TEXT,created_at INTEGER);
CREATE TABLE audit(seq INTEGER,ts INTEGER,action TEXT,track TEXT,scope TEXT,text TEXT,outcome TEXT);
''')
            con.executemany('INSERT INTO audit VALUES(?,?,?,?,?,?,?)', [(i, i, 'fixture', 'agent', 'workspace', 'synthetic', 'ok') for i in range(237)])
            con.executemany('INSERT INTO proposals VALUES(?,?,?,?,?,?,?)', [(str(i), 'fixture', 'agent', 'workspace', 'synthetic', 'pending', i) for i in range(251)])
            con.commit()
        self.panel.MEMENTO_DB = str(file)
        return file

    def test_memory_totals_are_sql_counts_beyond_display_cap_and_database_stays_readonly(self):
        file = self.database(); before = hashlib.sha256(file.read_bytes()).hexdigest()
        result = self.panel.memory_overview()
        self.assertEqual(result['counts']['audit'], 237)
        self.assertEqual(result['counts']['proposals'], 251)
        self.assertEqual(len(result['audit']), 60)
        self.assertEqual(len(result['proposals']), 30)
        self.assertEqual(result['availability'], {'status': 'available', 'counts_complete': True})
        self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(), before)

    def test_unreadable_memory_is_unknown_instead_of_successful_zero(self):
        self.panel.MEMENTO_DB = str(self.base.root / 'does-not-exist.db')
        result = self.panel.memory_overview()
        self.assertEqual(result['availability']['status'], 'error')
        self.assertFalse(result['availability']['counts_complete'])
        self.assertTrue(all(result['counts'][name] is None for name in ('active', 'audit', 'proposals')))
        self.assertFalse(Path(self.panel.MEMENTO_DB).exists())


if __name__ == '__main__': unittest.main()
