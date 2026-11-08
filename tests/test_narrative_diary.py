"""Real temporary private files, interrupted commits and CAS; no live data."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import activity_journal as journal
import narrative_diary as diary

DAY = '2026-11-03'
NOW = int(dt.datetime(2026, 11, 3, 20, tzinfo=journal.BEIJING).timestamp() * 1000)


class NarrativeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='narrative-diary-fixture-')
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name) / 'work'; self.work.mkdir()
        self.data = self.work / 'character'; self.data.mkdir()
        self.options = dict(workspace=str(self.work), agent_preset='Fixture', data_directory=str(self.data),
                            conversation_key='private:42', now=NOW)
        self.source = dict(schema='diary-facts/v1', day=DAY, status='available', revision='a' * 64,
                           coverage={'current_mapped_sessions_only': True, 'all_account_history_complete': False},
                           items=[{'ref': 'b' * 64, 'summary': '20:00:00 · 自主观察 · 工具已返回\n读到公开字幕，未证明播放或看完。'}], read_only=True)
        self.provider = lambda **kw: copy.deepcopy(self.source) if kw['day'] == self.source['day'] else dict(status='unavailable', revision=None, items=[])
        self.root, self.owner = diary.location(str(self.work), 'Fixture', str(self.data), 'private:42')
        for name in ('subprocess.run', 'subprocess.Popen', 'urllib.request.urlopen'):
            patcher = mock.patch(name, side_effect=AssertionError('No model, process or transport'))
            patcher.start(); self.addCleanup(patcher.stop)

    def write(self, **changes):
        args = dict(self.options, day=DAY, expected_revision=0, fact_revision='a' * 64,
                    title='从字幕里捡到一个想法', experiences=[{'text': '我读了一段公开视频字幕，还没确认播放。', 'evidence_refs': ['b' * 64]}],
                    feelings='我觉得这个角度挺有趣。', unfinished='明天有新线索再接着看。', operation_id='c' * 64,
                    facts_provider=self.provider)
        args.update(changes)
        return diary.write_diary(**args)

    def read(self, **changes):
        args = dict(self.options, day=DAY, facts_provider=self.provider); args.update(changes)
        return diary.narrative_snapshot(**args)

    def test_read_and_pending_never_create_diary_or_invoke_a_model(self):
        self.assertEqual(self.read()['status'], 'empty')
        self.assertTrue(self.read()['new_facts_available'])
        self.assertFalse(self.root.exists())
        pending = diary.pending_diary(**self.options, facts_provider=self.provider)
        self.assertEqual((pending['day'], pending['status'], pending['narrative_revision']), (DAY, 'due', 0))
        self.assertFalse(pending['write_requested'])
        self.assertFalse(self.root.exists())

    def test_first_person_topic_and_grounded_refs_leave_legacy_diary_untouched(self):
        legacy = self.data / 'diary'; legacy.mkdir()
        old = legacy / (DAY + '.md'); old.write_text('手写原件', encoding='utf-8')
        self.assertEqual(self.write()['revision'], 1)
        view = self.read(); item = view['selected']
        self.assertEqual(item['title'], '从字幕里捡到一个想法')
        self.assertIn('# ' + DAY + ' · 从字幕里捡到一个想法', item['markdown'])
        self.assertIn('我读了一段', item['markdown'])
        self.assertTrue(item['feelings_are_agent_report'])
        self.assertTrue(item['narrative_is_agent_report'])
        self.assertEqual(item['source_evidence'][0]['ref'], 'b' * 64)
        self.assertEqual(item['evidence_refs'], ['b' * 64])
        self.assertFalse(view['new_facts_available'])
        self.assertEqual(old.read_text(encoding='utf-8'), '手写原件')
        self.assertEqual(diary.pending_diary(**self.options, facts_provider=self.provider)['status'], 'current')

    def test_same_submission_is_idempotent_and_new_fact_cas_preserves_history(self):
        self.write(); target = self.root / (DAY + '.json'); before = journal._record(target)
        self.assertEqual(self.write()['status'], 'already_committed')
        self.assertEqual(journal._record(target), before)
        with self.assertRaisesRegex(journal.JournalError, 'idempotency_conflict'):
            self.write(feelings='另一份文字不能冒用同一个请求。')
        self.source['revision'] = 'd' * 64
        self.assertTrue(self.read()['new_facts_available'])
        with self.assertRaisesRegex(journal.JournalError, 'revision_conflict'):
            self.write(operation_id='e' * 64, fact_revision='d' * 64)
        updated = self.write(operation_id='e' * 64, expected_revision=1, fact_revision='d' * 64)
        self.assertEqual(updated['revision'], 2)
        history = list(self.root.glob('.history-*.json'))
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].read_bytes(), before[1])
        self.assertFalse(self.read()['new_facts_available'])

    def test_uncertain_submission_blocks_new_write_and_exact_same_can_reconcile_once(self):
        def fault(_): raise RuntimeError('simulated interruption')
        with self.assertRaises(RuntimeError): self.write(fault=fault)
        view = self.read()
        self.assertEqual(view['submission'], 'unknown_read_existing')
        self.assertFalse(view['new_facts_available'])
        self.assertEqual(diary.pending_diary(**self.options, facts_provider=self.provider)['status'], 'unknown_read_existing')
        with self.assertRaisesRegex(journal.JournalError, 'submission_unknown'):
            self.write(operation_id='d' * 64)
        with self.assertRaisesRegex(journal.JournalError, 'idempotency_conflict'):
            self.write(feelings='改过的文字不能借旧请求完成发布。')
        self.assertFalse((self.root / (DAY + '.json')).exists())
        self.assertEqual(self.write()['status'], 'already_committed')
        self.assertEqual(self.read()['selected']['revision'], 1)
        self.assertFalse((self.root / 'pending.json').exists())

    def test_changed_facts_and_foreign_reference_do_not_replace_a_successful_day(self):
        self.write(); target = self.root / (DAY + '.json'); before = journal._record(target)
        with self.assertRaisesRegex(journal.JournalError, 'facts_changed'):
            self.write(operation_id='e' * 64, expected_revision=1, fact_revision='f' * 64)
        with self.assertRaisesRegex(journal.JournalError, 'reference_not_in'):
            self.write(operation_id='e' * 64, expected_revision=1,
                       experiences=[{'text': '我不能用另一个日期的凭据。', 'evidence_refs': ['f' * 64]}])
        self.assertEqual(journal._record(target), before)

    def test_foreign_json_or_unknown_target_is_preserved(self):
        self.write(); target = self.root / (DAY + '.json')
        target.write_text('手写内容不能被当作系统日记覆盖', encoding='utf-8'); before = target.read_bytes()
        with self.assertRaises((ValueError, journal.JournalError)):
            self.write(expected_revision=1, operation_id='e' * 64)
        self.assertEqual(target.read_bytes(), before)

    def test_current_scope_only_and_old_empty_days_have_no_fabricated_closeout(self):
        for scope in ('group:42', 'private:0', 'private:42/../../other'):
            with self.assertRaises(journal.JournalError): self.read(conversation_key=scope)
        self.source['day'] = '2026-10-30'
        self.assertEqual(diary.pending_diary(**self.options, facts_provider=self.provider)['status'], 'current')
        with self.assertRaisesRegex(journal.JournalError, 'day_not_current'):
            self.write(day='2026-10-30')

    def test_traversal_candidate_in_unknown_does_not_touch_other_files(self):
        with self.assertRaises(RuntimeError): self.write(fault=lambda _: (_ for _ in ()).throw(RuntimeError()))
        pending = self.root / 'pending.json'; value = json.loads(pending.read_text(encoding='utf-8'))
        outside = self.data / 'keep.json'; outside.write_text('original', encoding='utf-8')
        value['candidate'] = '../../keep.json'; pending.write_bytes(journal._encoded(value))
        with self.assertRaises(journal.JournalError): self.write()
        self.assertEqual(outside.read_text(encoding='utf-8'), 'original')

    def test_cli_rechecks_configured_primary_not_merely_any_private_scope(self):
        identity = self.data / 'lanya-config.json'; identity.write_text(json.dumps({'adminQQ': '42'}), encoding='utf-8')
        with mock.patch('runtime_capabilities._configuration', return_value={'dataDirName': 'character'}):
            request = {'cwd': str(self.work), 'agent_preset': 'Fixture', 'conversation_key': 'private:42'}
            options, _ = diary.trusted_options(request)
            self.assertEqual(options['conversation_key'], 'private:42')
            with self.assertRaisesRegex(journal.JournalError, 'configured_primary'):
                diary.trusted_options(dict(request, conversation_key='private:43'))


import test_activity_journal as journal_fixture


class DiaryFactsIntegrationTests(unittest.TestCase):
    setUp = journal_fixture.JournalTests.setUp
    cleanup_temporary = journal_fixture.JournalTests.cleanup_temporary

    def source(self):
        return journal.diary_facts(workspace=str(self.work), agent_preset='Fixture',
                    data_directory=str(self.data), day=journal_fixture.TODAY, now=journal_fixture.NOW)

    def update(self):
        journal.update_journal(**self.options, catalog_provider=self.provider)

    def test_real_owned_facts_provide_refs_and_preserve_uncertain_playback_and_comments(self):
        self.update(); view = self.source()
        self.assertEqual(view['status'], 'available')
        self.assertEqual(len(view['items']), 1)
        self.assertIn('公开评论尝试', view['items'][0]['summary'])
        self.assertIn('未核实', view['items'][0]['summary'])
        self.assertNotIn(journal_fixture.PRIVATE, str(view))
        self.assertIn('not proof', view['instruction'])

    def test_diary_and_control_tool_activity_never_make_their_own_facts_revision_new(self):
        self.update(); before = self.source()
        episode = self.current['facts'][0]['episodes'][0]
        for name in ('qq_diary', 'memory', 'memory_recall'):
            episode['evidence'].append({'label': '调用工具 ' + name, 'summary': '工具已返回'})
        self.current['facts'][0]['counts']['tools'] += 3
        self.current['facts'][0]['counts']['decisions'] += 1
        self.update(); after = self.source()
        self.assertNotEqual(before['journal_revision'], after['journal_revision'])
        self.assertEqual(before['revision'], after['revision'])
        self.assertEqual(before['items'], after['items'])
        self.assertNotIn('qq_diary', str(after['items']))


if __name__ == '__main__': unittest.main()
