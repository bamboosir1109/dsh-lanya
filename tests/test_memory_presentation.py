"""Real read-only projections and panel routes against synthetic private data."""
import datetime
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import memory_presentation as presentation
import test_panel_autonomy as panel_fixture


class MemoryPresentationTests(unittest.TestCase):
    def test_recorded_instant_does_not_invent_event_time(self):
        row = dict(id='fixture', text='周末商量过登山计划。', created_at=1793520000000,
                   updated_at=1793606400000, last_recalled=1793606400000)
        view = presentation.present_entry(row)['presentation']
        self.assertIsNone(view['occurred_at'])
        self.assertEqual(view['recorded_at'], '2026-11-01T08:00:00Z')
        self.assertEqual(view['kind'], 'unspecified')
        for value in (None, '', 0, -1, True, 'yesterday', '2026-11-01T08:00:00'):
            self.assertIsNone(presentation.timestamp(value))
        self.assertEqual(presentation.timestamp('2026-11-01T16:00:00+08:00'), view['recorded_at'])

    def test_topic_title_and_metadata_remain_distinct_from_track(self):
        entry = dict(text='我自己的 在书架找到《星海航行手册》，还想继续读。', track='agent', tags='["reading"]')
        view = presentation.present_entry(entry)
        self.assertEqual(view['presentation']['title'], '星海航行手册')
        self.assertEqual(view['presentation']['title_origin'], 'content_excerpt')
        self.assertEqual(view['text'], entry['text'])
        self.assertEqual(view['tags'], ['reading'])
        explicit = presentation.present_entry(entry, dict(title='下次继续第三章', kind='reflection',
            occurred_at='2026-11-01', importance=60, confidence=None, status='uncertain'))['presentation']
        self.assertEqual((explicit['title'], explicit['title_origin'], explicit['kind']),
                         ('下次继续第三章', 'metadata', 'reflection'))
        self.assertEqual(explicit['occurred_at'], '2026-11-01')
        self.assertIsNone(explicit['confidence'])

    def test_date_filters_use_stored_time_in_configured_day_not_fact_words(self):
        start, end = presentation.parse_date_range('2026-11-02', '2026-11-02')
        self.assertTrue(presentation.in_recorded_range({'created_at':'2026-11-01T17:00:00Z'}, start, end))
        self.assertFalse(presentation.in_recorded_range({'created_at':None, 'text':'2026-11-02发生'}, start, end))
        self.assertFalse(presentation.in_recorded_range({'created_at':'2026-11-01T15:00:00Z'}, start, end))
        for pair in [('2026-02-30', None), ('2026-11-03', '2026-11-01'), ('today', None)]:
            with self.assertRaises(ValueError): presentation.parse_date_range(*pair)

    def test_file_heading_is_bounded_read_only_and_not_a_file_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '2026-11-02.md'
            original = '# 晚上想起的那本书\n\n我读了几页。\n'
            path.write_text(original, encoding='utf-8')
            before = path.stat()
            value = presentation.file_title(path)
            self.assertEqual(value['title'], '晚上想起的那本书')
            self.assertEqual(value['document_date'], '2026-11-02')
            self.assertEqual(path.read_text(encoding='utf-8'), original)
            self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
            linked = Path(directory) / 'linked.md'
            try:
                linked.hardlink_to(path)
            except (OSError, AttributeError):
                import os
                os.link(path, linked)
            self.assertEqual(presentation.file_title(linked), {})


class PanelMemoryProjectionTests(unittest.TestCase):
    setUp = panel_fixture.PanelAutonomyTests.setUp
    write = panel_fixture.PanelAutonomyTests.write
    route = panel_fixture.PanelAutonomyTests.route

    def database(self):
        path = self.root / 'synthetic-memory.db'
        with sqlite3.connect(path) as con:
            con.execute('CREATE TABLE entries (id TEXT, track TEXT, scope TEXT, workspace_key TEXT, agent_key TEXT, text TEXT, source TEXT, tags TEXT, version INTEGER, created_at INTEGER, updated_at INTEGER, session_id TEXT, recall_count INTEGER, last_recalled INTEGER)')
            for eid, text, at in [('first','喜欢登山和看地图。',1793520000000), ('second','周二去找观星手册。',1793606400000)]:
                con.execute('INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (eid,'agent','workspace','fixture-workspace','',''+text,'fixture','[]',1,at,at,'fixture-session',0,None))
        self.module.MEMENTO_DB = str(path)
        return path

    def test_search_filters_before_ranking_and_never_writes_or_falls_back_to_files(self):
        path = self.database(); before = path.read_bytes()
        with mock.patch('memory_plastic.Corpus', side_effect=AssertionError('filesystem fallback forbidden')):
            code, value = self.route('/api/memory/search?q=&start=2026-11-02&end=2026-11-02')
        self.assertEqual(code, 200)
        self.assertTrue(value['ok'])
        self.assertEqual([row['id'] for row in value['hits']], ['second'])
        self.assertEqual(value['time_basis'], 'recorded_at')
        self.assertEqual(value['hits'][0]['presentation']['title_origin'], 'content_excerpt')
        self.assertEqual(path.read_bytes(), before)

    def test_bad_range_and_database_errors_are_not_empty_success_or_raw_paths(self):
        self.database()
        code, value = self.route('/api/memory/search?q=map&start=2026-99-99')
        self.assertFalse(value['ok'])
        self.assertEqual(value['error'], 'MemorySearchUnavailable')
        self.module.MEMENTO_DB = str(self.root / 'private-missing.db')
        _, value = self.route('/api/memory/search?q=map')
        self.assertFalse(value['ok'])
        self.assertNotIn(str(self.root), repr(value))

    def test_diary_page_is_get_only_and_does_not_generate_a_diary(self):
        value = dict(schema='narrative-diary/v1', status='empty', read_only=True, private=True,
                     days=[], selected=None)
        with mock.patch.object(self.module, 'panel_narrative_diary', return_value=value) as reader:
            code, result = self.route('/api/memory/diary?day=2026-11-02')
            self.assertEqual((code,result['ok']), (200,True))
            reader.assert_called_once_with('2026-11-02')
        code, result = self.route('/api/memory/diary?day=../arbitrary')
        self.assertEqual(code,400)


if __name__ == '__main__':
    unittest.main()
