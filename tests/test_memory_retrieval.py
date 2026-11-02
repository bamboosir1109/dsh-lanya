"""Offline retrieval-quality cases; synthetic owners/facts and temporary SQLite."""
import contextlib
import datetime
import hashlib
import json
import sqlite3
import time
import unittest

import test_memory_context as fixture_module


class MemoryRetrievalTests(unittest.TestCase):
    def setUp(self):
        fixture = fixture_module.MemoryContextTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        for key in ('index', 'context', 'metadata', 'plastic', 'db', 'cwd', 'preset', 'owner', 'insert', 'sql'):
            setattr(self, key, getattr(fixture, key))
        self.now = int(time.time() * 1000)

    def annotate(self, row, **metadata):
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute('BEGIN')
            return self.metadata.write_metadata(connection, row, metadata)

    def context_for(self, query, **extra):
        return self.context.build_context(self.cwd, self.preset, query, db_path=str(self.db), **extra)

    def test_query_repetition_does_not_inflate_score_and_stop_words_do_not_match(self):
        self.insert('topic', text='raincoat umbrella', created_at=self.now, updated_at=self.now)
        index = self.index.Index.for_model(self.cwd, self.preset)
        self.assertEqual(index.search('umbrella')[0]['score'], index.search('umbrella umbrella umbrella')[0]['score'])
        self.assertEqual(self.index.tokens('这个 什么 现在'), [])
        self.assertIn('不能', self.index.tokens('不能吃花生'))

    def test_explicit_local_synonyms_recall_a_promised_fact(self):
        self.insert('promise', text='答应周四归还蓝色雨伞。')
        self.insert('other', text='下午观看了新的交通影片。')
        packet = self.context_for('以前的约定是什么')
        self.assertEqual([row['id'] for row in packet['memories']], ['promise'])
        self.assertEqual(packet['memories'][0]['retrieval']['method'], 'local_lexical')

    def test_specific_old_fact_beats_recent_partial_activity_and_foreign_exact_match(self):
        self.insert('specific', text='蓝色雨伞约定在南门归还。', created_at=1, updated_at=1)
        for number in range(20):
            self.insert('noise-' + str(number), text='今天看了雨伞视频，活动编号 ' + str(number), created_at=self.now, updated_at=self.now)
        self.insert('foreign', text='蓝色雨伞南门归还', workspace_key='foreign-owner')
        packet = self.context_for('蓝色雨伞南门归还')
        self.assertEqual(packet['memories'][0]['id'], 'specific')
        self.assertNotIn('foreign', [row['id'] for row in packet['memories']])

    def test_repeated_video_observations_collapse_without_deleting_rows(self):
        for number in range(12):
            self.insert('watch-' + str(number), text='看了视频 BV1Ab2Cd3Ef4，摘要序号 ' + str(number))
        self.insert('different', text='看了视频 BV9Zy8Xw7Vu6，主题是风。')
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        packet = self.context_for('看过的视频')
        self.assertEqual(len(packet['memories']), 2)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM entries')[0][0], 13)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_title_metadata_can_recall_an_old_fact_outside_recent_candidate_window(self):
        old = self.insert('old-title', text='行程集合地点已经确定。', created_at=1, updated_at=1)
        self.annotate(old, title='深海旅行集合约定', kind='event')
        for number in range(530):
            self.insert('recent-' + str(number), text='日常记录 ' + str(number), created_at=self.now, updated_at=self.now)
        packet = self.context_for('深海旅行')
        self.assertEqual(packet['memories'][0]['id'], 'old-title')
        self.assertTrue(packet['retrieval']['truncated'])
        self.assertEqual(packet['retrieval']['coverage'], 'bounded_candidates')
        self.assertLessEqual(packet['retrieval']['candidates'], 512)
        self.assertEqual(packet['retrieval']['total_visible'], 531)

    def test_updated_fact_and_explicit_status_rank_without_fabricating_confirmation(self):
        old = self.insert('old-rule', text='南门集合安排，雨天也适用。', created_at=1, updated_at=1)
        fresh = self.insert('fresh-rule', text='南门集合安排，当前以遮雨棚为准。', created_at=1, updated_at=self.now)
        self.annotate(old, status='superseded', kind='rule')
        self.annotate(fresh, status='active', kind='rule', importance=90)
        packet = self.context_for('南门集合安排')
        self.assertEqual(packet['memories'][0]['id'], 'fresh-rule')
        self.assertIn('old-rule', [row['id'] for row in packet['memories']])
        self.assertEqual(packet['memories'][0]['created_at'], 1)
        self.assertEqual(packet['memories'][0]['retrieval']['annotation_authority'], 'agent_metadata_not_external_confirmation')
        self.assertIsNone(packet['memories'][0]['presentation']['confidence'])
        self.assertEqual(packet['authority'], 'historical_evidence_not_current_runtime_configuration')

    def test_expired_annotations_decay_but_remain_searchable(self):
        expired = self.insert('expired', text='temporary umbrella arrangement')
        active = self.insert('active', text='umbrella arrangement currently valid')
        self.annotate(expired, valid_until='2001-01-01', kind='event')
        self.annotate(active, valid_from='2001-01-01', kind='fact')
        packet = self.context_for('umbrella arrangement')
        self.assertEqual(packet['memories'][0]['id'], 'active')
        self.assertEqual(next(row['retrieval']['validity'] for row in packet['memories'] if row['id'] == 'expired'), 'expired')

    def test_capsule_container_creation_does_not_rejuvenate_original_fact_time(self):
        first = self.insert('one', text='答应周四归还蓝色雨伞。', created_at=1, updated_at=2)
        second = self.insert('two', text='蓝色雨伞在南门交还。', created_at=3, updated_at=4)
        self.plastic.apply_merges([self.plastic.merge_group([first, second])], dry=False)
        packet = self.context_for('蓝色雨伞')
        self.assertEqual(packet['memories'][0]['fact_time_range'], (1.0, 4.0))
        self.assertGreater(packet['memories'][0]['retrieval']['age_days'], 1000)
        self.assertEqual({row['id'] for row in packet['originals']}, {'one', 'two'})

    def test_short_followup_uses_only_same_chat_user_topic_and_labels_assistant_reports(self):
        self.insert('promise', text='答应周四归还蓝色雨伞。')
        owner = self.context.session_owner(self.cwd, self.preset)
        event = dict(id='current', source='native_user_surface', role='user', text='之前答应的蓝色雨伞',
                     created_at=self.now, conversation_key='private:fixture', **owner)
        assistant = dict(event, id='report', role='assistant', text='蓝色雨伞的安排我提过。')
        foreign = dict(event, id='foreign', conversation_key='private:other', text='foreign unique topic')
        packet = self.context_for('那后来呢', conversation_key='private:fixture', recent_events=[event, assistant, foreign])
        self.assertEqual(packet['retrieval']['query_mode'], 'same_conversation_topic_fallback')
        self.assertEqual([row['id'] for row in packet['memories']], ['promise'])
        self.assertNotIn('foreign', [row['id'] for row in packet['recent_events']])
        self.assertEqual(next(row['evidence'] for row in packet['recent_events'] if row['role'] == 'assistant'), 'assistant_report_not_delivery')
        repeated = self.context_for('蓝色雨伞', conversation_key='private:fixture', recent_events=[event, dict(assistant, text=event['text'])])
        self.assertEqual({row['role'] for row in repeated['recent_events']}, {'user', 'assistant'})
        unrelated = self.context_for('宇宙背景辐射', conversation_key='private:fixture', recent_events=[event])
        self.assertEqual(unrelated['memories'], [])

    def test_context_has_a_shared_text_budget_and_preserves_source_bytes(self):
        for number in range(8):
            self.insert('long-' + str(number), text='umbrella ' + ('完整事实。' * 1200) + str(number))
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        packet = self.context_for('umbrella')
        self.assertLessEqual(sum(len(row['text']) for key in ('memories', 'recent_events', 'originals') for row in packet[key]), 10000)
        self.assertTrue(packet['retrieval']['context_truncated'])
        self.assertTrue(all(len(row['text_sha256']) == 64 for row in packet['memories']))
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_exact_validated_reverse_correction_outranks_old_active_recall_loop(self):
        old = self.insert('old-place', text='蓝色雨伞在南门交还。', created_at=1, updated_at=1, recall_count=900)
        new = self.insert('new-place', text='地点改在西侧遮雨棚。', created_at=self.now, updated_at=self.now)
        self.annotate(old, kind='fact', status='active')
        self.annotate(new, kind='fact', status='active', links=[dict(entry_id=old['id'], version=old['version'], relation='corrects')])
        packet = self.context_for('蓝色雨伞')
        self.assertEqual(packet['memories'][0]['id'], 'new-place')
        self.assertEqual(packet['memories'][0]['retrieval']['method'], 'linked_revision')
        prior = next(row for row in packet['memories'] if row['id'] == 'old-place')
        self.assertEqual(prior['presentation']['status'], 'active')
        self.assertEqual(prior['retrieval']['revised_by'][0]['entry_id'], 'new-place')
        self.assertEqual(prior['retrieval']['relationship_coverage'], 'retrieved_candidates_only')
        # Advancing a target version means an exact older link cannot silently
        # revise the new fact. The raw old record is never deleted or edited.
        self.sql('UPDATE entries SET version=version+1 WHERE id=?', (old['id'],))
        packet = self.context_for('蓝色雨伞')
        self.assertEqual(packet['memories'][0]['id'], 'old-place')
        self.assertEqual(packet['memories'][0]['retrieval']['revised_by'], [])

    def test_date_only_valid_until_includes_its_entire_local_day(self):
        today = datetime.datetime.now().astimezone().date().isoformat()
        row = self.insert('same-day', text='umbrella meeting schedule', created_at=self.now, updated_at=self.now)
        self.annotate(row, valid_until=today, kind='event')
        packet = self.context_for('umbrella schedule')
        self.assertEqual(packet['memories'][0]['retrieval']['validity'], 'within_declared_range')

    def test_declared_dates_and_recorded_dates_are_searchable_without_inventing_occurrence(self):
        declared = self.insert('declared-date', text='集合位置已定。', created_at=1, updated_at=1)
        self.annotate(declared, occurred_at='2026-11-04', valid_until='2026-11-06')
        recorded_at = int(datetime.datetime(2025, 3, 4, 12).timestamp() * 1000)
        recorded = self.insert('recorded-date', text='这是当时写下的便笺。', created_at=recorded_at, updated_at=recorded_at)
        self.assertEqual([row['id'] for row in self.context_for('2026-11-04')['memories']], ['declared-date'])
        self.assertEqual([row['id'] for row in self.context_for('2026-11-06')['memories']], ['declared-date'])
        packet = self.context_for('2025-03-04')
        self.assertEqual([row['id'] for row in packet['memories']], ['recorded-date'])
        self.assertIsNone(packet['memories'][0]['presentation'])
        self.assertEqual(packet['memories'][0]['created_at'], recorded_at)


if __name__ == '__main__':
    unittest.main()
