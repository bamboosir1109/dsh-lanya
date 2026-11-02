"""Real temporary daily files/ACL/locks; no production, model or transport."""
import ast
import copy
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import activity_journal as journal
from panel_autonomy import _security

TODAY = '2026-11-02'
NOW = int(dt.datetime(2026, 11, 2, 12, tzinfo=journal.BEIJING).timestamp() * 1000)
PRIVATE = 'PRIVATE_FIXTURE_NOT_IN_JOURNAL'


def facts(day=TODAY, episodes=1, status='ok', scopes=2, binding='a' * 64):
    start = int(dt.datetime.combine(dt.date.fromisoformat(day), dt.time(), journal.BEIJING).timestamp() * 1000)
    return {'schema': 'panel-activity-daily/v1', 'day': day, 'status': status, 'generated_at': NOW,
            'episodes': [{'id': str(i), 'started_at': start + i, 'updated_at': start + i,
                          'title': '读取公开视频资料', 'status': 'returned', 'summary': '读取了视频资料，未验证播放。',
                          'trigger_origin': 'observation', 'conversation_role': 'primary',
                          'reasoning': PRIVATE, 'raw_args': PRIVATE,
                          'evidence': [{'label': '字幕', 'summary': '读取字幕内容', 'content_excerpt': '一小段公开字幕',
                                        'video': {'bvid': 'BV1234567890', 'title': '公开演示', 'author': '公开作者'},
                                        'reply': {'type': 'comment', 'text': '公开评论尝试', 'receipt_present': True}}]} for i in range(episodes)],
            'decisions': [], 'counts': {'episodes': episodes, 'tools': episodes, 'decisions': 0},
            'coverage': {'configured_scopes': 2, 'mapped_scopes': scopes, 'owner_verified_scopes': scopes,
                         'current_mapped_sessions_only': True, 'all_account_history_complete': False,
                         'truncated': False, 'decisions_truncated': False, 'day_closed': day < TODAY,
                         'window_start': start, 'window_end': start + 86400000, 'reasons': [],
                         'source_windows': [{'source': role, 'available_from': start - 86400000, 'binding_sha256': binding}
                                            for role in ('primary', 'social')[:scopes]]}}


def catalog(*rows, status='ok'):
    return {'schema': 'panel-activity-catalog/v1', 'status': status, 'days': [row['day'] for row in rows],
            'facts': list(rows), 'generated_at': NOW, 'coverage': {}}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='journal-fixture-')
        self.addCleanup(self.cleanup_temporary)
        self.base = Path(self.temporary.name)
        self.work = self.base / 'workspace'; self.work.mkdir()
        self.data = self.work / 'character'; self.data.mkdir()
        self.options = dict(workspace=str(self.work), agent_preset='Fixture', data_directory=str(self.data),
                            identity_file=self.base / 'identity.json', dsh_home=self.base / 'home', now=NOW)
        self.root, self.owner = journal._location(str(self.work), 'Fixture', str(self.data))
        self.current = catalog(facts())
        self.provider = mock.Mock(side_effect=lambda **kw: copy.deepcopy(self.current))
        for name in ('subprocess.run', 'subprocess.Popen', 'urllib.request.urlopen'):
            patcher = mock.patch(name, side_effect=AssertionError('external execution forbidden'))
            patcher.start(); self.addCleanup(patcher.stop)

    def cleanup_temporary(self):
        root = Path(self.temporary.name).resolve()
        self.assertEqual(root.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(root.name.startswith('journal-fixture-'))
        self.temporary.cleanup()

    def update(self, **kw):
        return journal.update_journal(**dict(self.options, **kw), catalog_provider=self.provider)

    def read(self, day=TODAY):
        return journal.journal_snapshot(workspace=str(self.work), agent_preset='Fixture', data_directory=str(self.data), day=day, now=NOW)

    def test_today_backfill_past_seven_days_and_idempotent_owned_updates(self):
        legacy = self.data / 'diary'; legacy.mkdir()
        existing = legacy / (TODAY + '.md'); existing.write_text('existing personal diary')
        before_parent = _security(self.data)
        self.current = catalog(facts('2026-10-20'), facts('2026-11-01'), facts())
        self.assertEqual(self.update()['written_count'], 2)
        self.assertTrue((self.root / '2026-10-20.md').is_file())
        self.assertFalse((self.root / '2026-11-01.md').exists())
        self.assertEqual(self.update()['written_count'], 1)
        before = journal._record(self.root / (TODAY + '.md'))[0]
        self.assertEqual(self.update()['written_count'], 0)
        self.assertEqual(journal._record(self.root / (TODAY + '.md'))[0], before)
        self.assertEqual(self.provider.call_count, 3)
        result = self.read()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['selected']['status'], 'open')
        self.assertEqual(self.read('2026-10-20')['selected']['status'], 'closed_snapshot')
        body = result['selected']['markdown']
        for present in ('自主观察', '公开演示', 'BV1234567890', '公开作者', '公开评论尝试', '日记摘录（不是全文）', '不代表完整账号历史'):
            self.assertIn(present, body)
        self.assertNotIn(PRIVATE, body)
        self.assertEqual(existing.read_text(), 'existing personal diary')
        self.assertEqual(_security(self.data), before_parent)

    def test_source_degradation_and_same_count_new_binding_preserve_successful_diary(self):
        self.current = catalog(facts('2026-11-01', 3), facts())
        self.update()
        path = self.root / '2026-11-01.md'
        before = journal._record(path)
        for reduced in (facts('2026-11-01', 1, 'partial', 1), facts('2026-11-01', 3, binding='b' * 64),
                        facts('2026-11-01', 0), facts('2026-11-01', 3, 'partial')):
            self.current = catalog(reduced, facts(), status='partial' if reduced['status'] == 'partial' else 'ok')
            self.update()
            self.assertEqual(journal._record(path), before)
            view = self.read('2026-11-01')
            self.assertEqual(view['selected']['entries'], 3)
            self.assertTrue(view['selected']['coverage']['latest_scan_degraded'])
            self.assertEqual(view['freshness']['status'], 'degraded')

    def test_unavailable_scan_preserves_existing_and_readers_never_create_files(self):
        self.assertEqual(self.read()['status'], 'empty')
        self.assertFalse(self.root.exists())
        self.update()
        before = journal._record(self.root / (TODAY + '.md'))
        self.current = catalog(status='unavailable')
        with self.assertRaises(journal.JournalError): self.update()
        self.assertEqual(journal._record(self.root / (TODAY + '.md')), before)
        self.assertEqual(self.read()['freshness']['status'], 'degraded')

    def test_unknown_same_bytes_inode_and_foreign_existing_diary_are_never_adopted(self):
        self.update()
        path = self.root / (TODAY + '.md')
        foreign = self.root / 'user-edited.tmp'
        foreign.write_bytes(path.read_bytes()); os.replace(foreign, path)
        before = journal._record(path)
        with self.assertRaises(journal.JournalError): self.update()
        self.assertEqual(journal._record(path), before)
        self.assertEqual(self.read()['status'], 'error')

    def test_interruption_retains_original_backup_and_next_cycle_finishes_only_known_commit(self):
        self.update()
        old = (self.root / (TODAY + '.md')).read_bytes()
        self.current = catalog(facts(episodes=2))
        original = os.replace

        def interrupted(source, target):
            if Path(target) == self.root / 'index.json':
                raise OSError('controlled index publish interruption')
            return original(source, target)

        with mock.patch.object(journal.os, 'replace', side_effect=interrupted), self.assertRaises(OSError):
            self.update()
        self.assertTrue((self.root / 'pending.json').exists())
        self.assertTrue(any(path.read_bytes() == old for path in self.root.glob('.backup-*.md')))
        self.assertEqual(self.read()['status'], 'error')
        self.update()
        self.assertFalse((self.root / 'pending.json').exists())
        self.assertEqual(self.read()['selected']['entries'], 2)

    def test_pending_paths_cannot_replace_or_unlink_a_sibling_file(self):
        self.update()
        self.current = catalog(facts(episodes=2))
        with self.assertRaisesRegex(RuntimeError, 'controlled'):
            self.update(fault=lambda phase: (_ for _ in ()).throw(RuntimeError('controlled')))
        pending = self.root / 'pending.json'
        value = journal._json(pending.read_bytes())
        outside = self.data / ('.stage-' + 'c' * 32 + '.md')
        outside.write_text('outside file must remain')
        initial = journal._record(outside)
        for spelling in (str(outside), '../' + outside.name):
            bad = copy.deepcopy(value)
            bad['markdown']['candidate'] = spelling
            bad['markdown']['after'] = initial[0]
            pending.write_bytes(journal._encoded(bad))
            with self.assertRaises(journal.JournalError): journal._recover(self.root, self.owner)
            self.assertEqual(journal._record(outside), initial)
        bad = copy.deepcopy(value)
        backup = self.data / ('.backup-' + 'd' * 32 + '.md'); backup.write_text('outside backup')
        initial = journal._record(backup)
        bad['backup'] = {'name': str(backup), 'record': initial[0]}
        pending.write_bytes(journal._encoded(bad))
        with self.assertRaises(journal.JournalError): journal._recover(self.root, self.owner)
        self.assertEqual(journal._record(backup), initial)

    def test_lock_contention_and_wrong_owner_are_explicit_errors(self):
        self.update()
        with journal._lock(self.root, True):
            with self.assertRaises(journal.JournalError): self.update()
            self.assertEqual(self.read()['status'], 'error')
        index = self.root / 'index.json'
        value = journal._json(index.read_bytes()); value['owner'] = 'foreign'
        index.write_bytes(journal._encoded(value))
        self.assertEqual(self.read()['status'], 'error')
        with self.assertRaises(journal.JournalError): self.update()

    def test_pending_index_candidate_foreign_owner_is_refused_before_any_replacement(self):
        self.update()
        before = journal._record(self.root / (TODAY + '.md'))
        self.current = catalog(facts(episodes=2))
        with self.assertRaises(RuntimeError):
            self.update(fault=lambda phase: (_ for _ in ()).throw(RuntimeError('controlled')))
        path = self.root / 'pending.json'
        intent = journal._json(path.read_bytes())
        candidate = self.root / intent['index']['candidate']
        value = journal._json(candidate.read_bytes()); value['owner'] = 'foreign'
        candidate.write_bytes(journal._encoded(value))
        intent['index']['after'] = journal._record(candidate)[0]
        path.write_bytes(journal._encoded(intent))
        with self.assertRaises(journal.JournalError): journal._recover(self.root, self.owner)
        self.assertEqual(journal._record(self.root / (TODAY + '.md')), before)

    def test_scope_private_reply_text_is_not_rendered_or_merged_as_public(self):
        self.current['facts'][0]['episodes'][0]['evidence'].append({'label': '私信', 'summary': '仅有接口记录',
                    'reply': {'type': 'private_message', 'text': PRIVATE}, 'raw_args': PRIVATE})
        self.current['facts'][0]['episodes'][0]['evidence'][0]['reply']['text_truncated'] = True
        self.update()
        result = self.read()
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertIn('评论尝试文字已截断', result['selected']['markdown'])
        self.assertIsNone(self.read('2026-11-01')['selected'])

    def test_midnight_closes_owned_previous_day_and_preserves_new_today(self):
        self.update()
        old = facts(); old['coverage']['day_closed'] = True
        tomorrow = facts('2026-11-03'); tomorrow['coverage']['day_closed'] = False
        self.current = catalog(old, tomorrow)
        self.update(now=NOW + 86400000)
        self.assertEqual(self.read()['selected']['status'], 'closed_snapshot')
        self.assertEqual(self.read('2026-11-03')['selected']['status'], 'open')

    def test_foreign_first_diary_and_unknown_target_after_intent_are_preserved(self):
        journal._private_directory(self.root.parent, True); journal._private_directory(self.root, True)
        path = self.root / (TODAY + '.md'); path.write_text('existing user diary')
        before = journal._record(path)
        with self.assertRaises(journal.JournalError): self.update()
        self.assertEqual(journal._record(path), before)
        path.unlink()
        self.update()
        self.current = catalog(facts(episodes=2))
        unknown = []

        def changed(phase):
            foreign = self.root / 'foreign.tmp'; foreign.write_text('post-intent user file')
            os.replace(foreign, path); unknown.append(journal._record(path))

        with self.assertRaises(journal.JournalError): self.update(fault=changed)
        self.assertEqual(journal._record(path), unknown[0])
        self.assertTrue(list(self.root.glob('.backup-*.md')))

    def test_actual_server_reader_returns_exact_owned_text_without_generating(self):
        self.update()
        source = ast.parse((REPO / 'server/panel-server.py').read_text(encoding='utf-8-sig'))
        node = next(row for row in source.body if isinstance(row, ast.FunctionDef) and row.name == 'panel_activity_journal')
        namespace = {'DATA': str(self.data), 'time': __import__('time'),
                     'project_conf': types.SimpleNamespace(ConfigError=RuntimeError),
                     '_panel_native_record_options': lambda: {'workspace': str(self.work), 'agent_preset': 'Fixture'}}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<actual-journal-server-reader>', 'exec'), namespace)
        with mock.patch.object(journal, 'update_journal', side_effect=AssertionError('GET must not generate')):
            result = namespace['panel_activity_journal'](TODAY, 90)
        self.assertEqual(result['selected']['markdown'], (self.root / (TODAY + '.md')).read_text(encoding='utf-8'))
        self.assertEqual(result['selected']['date'], TODAY)


class JournalRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = ast.parse((REPO / 'server/panel-server.py').read_text(encoding='utf-8-sig'))
        handler = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == 'Handler')
        getter = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == 'do_GET')
        namespace = {'urlparse': urlparse, 'parse_qs': parse_qs, 'json': json, 're': __import__('re'),
                     'project_conf': types.SimpleNamespace(get=lambda key, default: default)}
        exec(compile(ast.Module(body=[getter], type_ignores=[]), '<actual-journal-route>', 'exec'), namespace)
        cls.namespace, cls.getter = namespace, namespace['do_GET']

    def request(self, path='/api/activity/journal', headers=None, peer='127.0.0.1'):
        response = {'headers': {}}
        handler = types.SimpleNamespace(path=path, headers={'Host': '127.0.0.1:19389', **(headers or {})},
                                        client_address=(peer, 1234), server=types.SimpleNamespace(server_port=19389), wfile=io.BytesIO())
        handler.send_response = lambda code: response.update(code=code)
        handler.send_header = lambda key, value: response['headers'].update({key.lower(): value})
        handler.end_headers = lambda: None
        self.namespace['panel_activity_journal'] = mock.Mock(return_value={'schema': journal.SCHEMA, 'status': 'empty', 'read_only': True, 'private': True, 'days': [], 'selected': None})
        self.namespace['do_GET'](handler)
        response['body'] = json.loads(handler.wfile.getvalue())
        return response, self.namespace['panel_activity_journal']

    def test_only_fixed_local_owner_route_and_validated_read_parameters(self):
        response, provider = self.request('/api/activity/journal?day=2026-11-02&limit=90', headers={'Origin': 'http://127.0.0.1:19388'})
        self.assertEqual(response['code'], 200)
        provider.assert_called_once_with('2026-11-02', 90)
        self.assertEqual(response['headers']['access-control-allow-origin'], 'http://127.0.0.1:19388')
        for path in ('?day=one', '?limit=0', '?limit=91', '?day=2026-11-02&day=2026-11-01', '?workspace=foreign'):
            response, provider = self.request('/api/activity/journal' + path)
            self.assertEqual(response['code'], 400)
            provider.assert_not_called()

    def test_remote_cross_site_and_rebinding_host_cannot_read_diary(self):
        for extra in ({'peer': '192.0.2.1'}, {'headers': {'Origin': 'https://example.invalid'}},
                      {'headers': {'Host': 'example.invalid:19389'}}, {'headers': {'Origin': 'null'}}):
            response, provider = self.request(**extra)
            self.assertEqual(response['code'], 403)
            self.assertNotIn('access-control-allow-origin', response['headers'])
            provider.assert_not_called()


import test_main_decision_observers as observer_fixture
import test_panel_activity as native_fixture


class MindJournalHookTests(unittest.TestCase):
    setUp = observer_fixture.MainDecisionObserverTests.setUp
    module = observer_fixture.MainDecisionObserverTests.module
    patch = observer_fixture.MainDecisionObserverTests.patch

    def test_journal_failure_does_not_change_observation_result_or_call_a_model(self):
        observed = self.patch(self.mind, '_observe_cycle', return_value=0)
        self.patch(journal, 'update_journal', side_effect=RuntimeError(PRIVATE))
        self.assertEqual(self.mind.one_cycle(), 0)
        observed.assert_called_once()
        self.ask.assert_not_called(); self.send.assert_not_called()
        self.assertFalse(any(PRIVATE in str(call) for call in self.mind.log.call_args_list))

    def test_dry_and_legacy_skip_journal_and_observer_failure_still_preserves_failure(self):
        observed = self.patch(self.mind, '_observe_cycle', return_value=0)
        callback = self.patch(self.mind, '_journal_cycle')
        self.assertEqual(self.mind.one_cycle(dry=True), 0); callback.assert_not_called()
        observed.side_effect = ValueError('controlled observer failure')
        self.assertEqual(self.mind.one_cycle(), 1); callback.assert_called_once()
        callback.reset_mock()
        self.conf._CACHE['decisions'] = {'mode': 'legacy'}
        self.mind.save(self.mind.MIND, {'next_check_ts': 2000})
        self.assertEqual(self.mind.one_cycle(), 0); callback.assert_not_called()
        self.ask.assert_not_called(); self.send.assert_not_called()


class NativeJournalIntegrationTests(unittest.TestCase):
    setUp = native_fixture.PanelActivityTests.setUp
    event = native_fixture.PanelActivityTests.event
    call = native_fixture.PanelActivityTests.call
    write = native_fixture.PanelActivityTests.write

    def test_real_native_projection_backfills_old_day_without_private_text(self):
        self.event('user/message', {'content': [{'type': 'text', 'text': PRIVATE}]})
        self.call('bili_video_info', {'bvid': native_fixture.BV, 'title': '真实合成公开资料'}, args={'bvid': native_fixture.BV})
        for row in self.rows: row['time'] = native_fixture.NOW - 10 * 86400000
        self.call('bili_subtitle', {'bvid': native_fixture.BV, 'title': '公开字幕视频', 'segments': 2,
                                   'text': '公开字幕片段'}, args={'bvid': native_fixture.BV}, turn=2)
        self.call('bili_send_message', {'ok': True, 'status': 'sent'}, args={'text': PRIVATE, 'receiver_uid': 808}, turn=3)
        self.write()
        data = self.work / 'character'; data.mkdir()
        result = journal.update_journal(workspace=str(self.work), agent_preset='Fixture', data_directory=str(data),
                                        identity_file=self.identity, dsh_home=self.home, now=native_fixture.NOW)
        self.assertEqual(result['written_count'], 2)
        day = dt.datetime.fromtimestamp(native_fixture.NOW / 1000, journal.BEIJING).date().isoformat()
        view = journal.journal_snapshot(workspace=str(self.work), agent_preset='Fixture', data_directory=str(data), day=day)
        self.assertEqual(len(view['days']), 2)
        self.assertIn('公开字幕片段', view['selected']['markdown'])
        self.assertNotIn(PRIVATE, json.dumps(view, ensure_ascii=False))
        self.assertFalse(view['selected']['coverage']['all_account_history_complete'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
