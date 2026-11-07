"""Native record projection uses temporary synthetic sessions, no host/API."""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import tracemalloc
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import panel_activity as activity

NOW = 1800000000000
BV = 'BV1234567890'
SECRET = 'PRIVATE_FIXTURE_TEXT_DO_NOT_LEAK'


class PanelActivityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='panel-native-fixture-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / 'workspace'; self.work.mkdir()
        self.home = self.root / 'home'; self.home.mkdir()
        self.identity = self.root / 'identity.json'
        self.identity.write_text('{"adminQQ":["101"]}', encoding='utf-8')
        self.mapping = self.home / 'media/onebot/chat-sessions.json'
        self.mapping.parent.mkdir(parents=True)
        self.mapping.write_text('{"private:101":"fixture-session"}', encoding='utf-8')
        self.header = {'type': 'session', 'version': 4, 'id': 'fixture-session',
                       'cwd': str(self.work), 'agentPreset': 'Fixture', 'isSeeded': False}
        self.rows = []
        self.path = self.home / 'sessions' / activity._project(str(self.work)) / activity._segment(self.header['id']) / 'session.v4.jsonl'
        self.path.parent.mkdir(parents=True)
        for target in ('subprocess.run', 'subprocess.Popen', 'urllib.request.urlopen'):
            guard = mock.patch(target, side_effect=AssertionError('external execution forbidden'))
            guard.start(); self.addCleanup(guard.stop)

    def event(self, kind, data, **extra):
        if kind == 'tool/result': extra.setdefault('surfaceOp', 'append')
        row = dict(type=kind, seq=len(self.rows), time=NOW - 1000 + len(self.rows), data=data, **extra)
        self.rows.append(row)
        return row

    def call(self, name, output=None, *, args=None, error=False, pending=False, turn=1):
        call_id = 'call-' + str(len(self.rows))
        call = self.event('tool/call', {'name': name, 'callId': call_id, 'turn': turn,
                                      'step': len(self.rows), 'arguments': json.dumps(args or {})})
        if not pending:
            self.event('tool/result', {'turn': turn, 'step': call['data']['step'],
                'message': {'source': {'callId': call_id}, 'toolCallId': call_id,
                            'isError': error, 'content': [{'type': 'text', 'text': json.dumps(output)}]}},
                sourceEventSeqs=[call['seq']])

    def write(self):
        raw = b''.join((json.dumps(row, ensure_ascii=False) + '\n').encode()
                       for row in [self.header, *self.rows])
        self.path.write_bytes(raw)
        return raw

    def snapshot(self, **kwargs):
        self.write()
        return activity.activity_snapshot(workspace=str(self.work), agent_preset='Fixture',
            identity_file=self.identity, dsh_home=self.home, now=NOW, **kwargs)

    def test_host_interruption_preserves_tool_facts_and_new_turns_without_fabricating_completion(self):
        self.event('turn/start', {'turn': 1})
        self.event('user/message', {'source': {'kind': 'plugin:dsh-lanya-observation'}})
        self.call('bili_video_info', {'bvid': BV, 'title': 'Before interruption'}, args={'bvid': BV}, turn=1)
        self.event('turn/start', {'turn': 2})
        self.event('user/message', {'source': {'kind': 'plugin:dsh-onebot'}})
        self.call('bili_video_info', {'bvid': BV, 'title': 'After restart'}, args={'bvid': BV}, turn=2)
        self.event('turn/end', {'turn': 2})
        value=self.snapshot();main=value['mainAgent']
        self.assertEqual(main['status'],'partial')
        self.assertIn('native_turn_interrupted',main['coverage']['reasons'])
        self.assertEqual(len(main['records']),2)
        self.assertEqual(main['records'][0]['trigger_origin'],'live')
        self.assertEqual(main['records'][1]['trigger_origin'],'unknown')
        self.assertTrue(all(row['status']=='succeeded' for row in main['records']))
        # A repeated/lower turn is still a malformed lifecycle.
        self.rows[4]['data']['turn']=1
        self.assertEqual(self.snapshot()['mainAgent']['status'],'unavailable')

    def test_actual_native_shapes_remain_distinct_and_private_text_is_not_projected(self):
        self.event('user/message', {'content': [{'type': 'text', 'text': SECRET}]})
        self.call('bili_recommend', {'ok': True, 'videos': [{'bvid': BV, 'title': 'Public fixture'}, {'bvid': BV}]})
        self.call('bili_video_info', {'bvid': BV, 'title': 'Public fixture'}, args={'bvid': BV})
        self.call('bili_subtitle', {'bvid': BV, 'title': 'Public fixture', 'segments': 2, 'text': 'Public subtitle'}, args={'bvid': BV})
        self.call('bili_frames', {'ok': True, 'bvid': BV, 'frames': [1, 2], 'image_attached': False}, args={'bvid': BV})
        self.call('read', {'text': SECRET}, args={'path': SECRET})
        result = self.snapshot(); main = result['mainAgent']
        self.assertEqual(main['status'], 'ok')
        self.assertEqual(main['counts']['recommendation'], 1)
        self.assertEqual(main['counts']['subtitle'], 1)
        self.assertEqual(main['counts']['frames'], 1)
        recommendation = next(row for row in main['records'] if row['kind'] == 'recommendation')
        self.assertEqual(recommendation['item_count'], 1)
        frame = next(row for row in main['records'] if row['kind'] == 'frames')
        self.assertIs(frame['image_attached'], False)
        rendered = json.dumps(result)
        for forbidden in (SECRET, self.header['id'], 'private:101', str(self.home), 'watched', 'playback'):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual(result['recentActivity']['records'][0]['status'], 'returned')

    def test_timestamped_video_content_keeps_selected_part_and_only_public_excerpt(self):
        fixture = Path(__file__).parent / 'fixtures/bili_video_content/observed.json'
        value = json.loads(fixture.read_text(encoding='utf-8'))
        value['private_extra'] = SECRET
        self.call('bili_observe_video', value, args={'bvid': value['bvid'], 'page': value['page']})
        self.call('bili_social_inbox', {'ok': True, 'items': [{'text': SECRET, 'author_uid': SECRET}]})
        self.call('bili_thread', {'ok': True, 'bvid': value['bvid'], 'comments': [{'text': SECRET}]},
                  args={'bvid': value['bvid'], 'root': '123'})
        main = self.snapshot()['mainAgent']
        record = next(row for row in main['records'] if row['tool'] == 'bili_observe_video')
        self.assertEqual(record['status'], 'succeeded')
        self.assertEqual(record['page'], value['page'])
        self.assertEqual(record['segment_count'], len(value['subtitle']['segments']))
        self.assertEqual(record['segment_range'], value['subtitle']['range'])
        self.assertIn(value['subtitle']['segments'][0]['text'], record['content_excerpt'])
        self.assertEqual(main['recent_videos'][0]['subtitle_count'], 1)
        self.assertEqual(main['counts']['observation'], 1)
        self.assertEqual(main['counts']['social_inbox'], 1)
        rendered = json.dumps(main)
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn('playback_verified', rendered)
        self.assertNotIn('author_uid', rendered)

    def test_video_content_invalid_or_metadata_only_never_invents_a_subtitle(self):
        template = {'schema': 'dsh-bili-video-content/v1', 'ok': True, 'status': 'partial',
                    'bvid': BV, 'page': 2, 'duration': 30, 'subtitle': {
                        'status': 'unavailable', 'segments': [], 'truncated': False}}
        self.call('bili_observe_video', template, args={'bvid': BV, 'page': 2})
        self.call('bili_observe_video', {**template, 'schema': 'untrusted'}, args={'bvid': BV})
        self.call('bili_observe_video', {**template, 'bvid': 'BV0123456789'}, args={'bvid': BV})
        for segment in ({'from': True, 'to': 5, 'text': SECRET},
                        {'from': 5, 'to': 2, 'text': SECRET},
                        {'from': 1, 'to': 40, 'text': SECRET}):
            self.call('bili_observe_video', {**template, 'subtitle': {
                'status': 'available', 'segments': [segment], 'truncated': False}}, args={'bvid': BV})
        records = self.snapshot()['mainAgent']['records']
        self.assertEqual(sum(row['status'] == 'succeeded' for row in records), 1)
        successful = next(row for row in records if row['status'] == 'succeeded')
        self.assertEqual(successful['subtitle_status'], 'unavailable')
        self.assertNotIn('content_excerpt', successful)
        self.assertNotIn(SECRET, json.dumps(records))

    def test_main_agent_comment_receipt_and_explicit_mention_stay_separate_from_visibility(self):
        self.call('bili_reply', {'ok': True, 'status': 'accepted', 'receipt': '345', 'rpid': '345',
                   'mention': {'requested': True, 'applied': True}},
                  args={'bvid': BV, 'text': 'Public fixture opinion', 'mention_admin': True})
        self.call('bili_reply', {'ok': True, 'status': 'accepted', 'receipt': '345', 'rpid': '346'},
                  args={'bvid': BV, 'text': 'Unconfirmed fixture'})
        replies = self.snapshot()['mainAgent']['replies']
        accepted = next(row for row in replies if row['status'] == 'accepted')
        self.assertTrue(accepted['receipt_present'])
        self.assertTrue(accepted['mention_applied'])
        self.assertEqual(accepted['public_visibility'], 'unverified')
        self.assertEqual(next(row for row in replies if row['text'] == 'Unconfirmed fixture')['status'], 'unknown')

    def test_empty_subtitle_or_false_success_never_becomes_watched_or_sent(self):
        self.call('bili_subtitle', {'message': 'No subtitles'}, args={'bvid': BV})
        self.call('bili_subtitle', {'bvid': BV, 'segments': 3, 'text': ''}, args={'bvid': BV})
        self.call('bili_send_message', {'ok': True, 'status': 'sent', 'receipt': 'receipt-fixture'}, args={'receiver_uid': 808, 'text': SECRET})
        self.call('bili_reply', {'success': True})
        self.call('bili_reply', {'success': False, 'error': SECRET})
        self.call('bili_reply', {'error': SECRET}, error=True)
        self.call('bili_send_message', {'ok': False, 'status': 'unknown', 'reason': SECRET})
        main = self.snapshot()['mainAgent']
        self.assertEqual(main['counts']['subtitle'], 0)
        self.assertEqual(main['counts']['actions'], 1)
        self.assertEqual(main['counts']['unknown'], 5)
        self.assertEqual(main['counts']['failed'], 1)
        self.assertNotIn('receipt-fixture', json.dumps(main))
        self.assertNotIn(SECRET, json.dumps(main))

    def test_public_comment_attempt_and_interface_receipt_are_not_public_visibility(self):
        text = '这是公开评论正文。\n<script>只是正文，不是界面代码</script>'
        self.call('bili_reply', {'success': True, 'rpid': 202, 'rpid_str': '202',
                  'root': 99, 'parent': 99, 'message': SECRET},
                  args={'bvid': BV, 'text': text, 'rpid': 99, 'force': True, 'title': SECRET})
        result = self.snapshot(); reply = result['mainAgent']['replies'][0]
        self.assertEqual(reply['type'], 'comment_reply')
        self.assertEqual(reply['text'], text)
        self.assertIs(reply['text_truncated'], False)
        self.assertEqual(reply['target_comment_id'], '99')
        self.assertEqual(reply['rpid'], '202')
        self.assertEqual(reply['video']['bvid'], BV)
        self.assertNotIn('title', reply['video'])
        self.assertEqual(reply['status'], 'accepted')
        self.assertIs(reply['receipt_present'], True)
        self.assertEqual(reply['evidence'], 'platform_receipt')
        self.assertEqual(reply['public_visibility'], 'unverified')
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn('reply', result['recentActivity']['records'][0])
        self.assertEqual(result['recentActivity']['episodes'][0]['evidence'][0]['reply']['text'], text)

    def test_failed_unknown_and_pending_comments_keep_attempt_text_without_claiming_sent(self):
        for value, extra in [({'success': False, 'error': SECRET}, {}),
                             ({'success': True}, {}), ({'error': SECRET}, {'error': True})]:
            self.call('bili_reply', value, args={'bvid': BV, 'text': '尝试正文'}, **extra)
        self.call('bili_reply', args={'bvid': BV, 'text': '还没有回执'}, pending=True)
        replies = self.snapshot()['mainAgent']['replies']
        self.assertEqual([row['status'] for row in replies], ['pending', 'unknown', 'unknown', 'failed'])
        self.assertEqual([row['evidence'] for row in replies],
                         ['pending_call', 'tool_error', 'unconfirmed_result', 'platform_rejection'])
        self.assertTrue(all(row['receipt_present'] is False for row in replies))
        self.assertTrue(all(row['public_visibility'] == 'unverified' for row in replies))
        self.assertEqual(replies[-1]['text'], '尝试正文')
        self.assertNotIn(SECRET, json.dumps(replies))

    def test_private_messages_never_project_text_recipient_or_message_receipt(self):
        self.call('bili_send_message', {'ok': True, 'status': 'sent', 'receipt': 'PRIVATE_RECEIPT'},
                  args={'receiver_uid': 9876543210, 'text': SECRET, 'bvid': BV, 'rpid': 1})
        result = self.snapshot(); reply = result['mainAgent']['replies'][0]
        self.assertEqual(reply['type'], 'private_message')
        self.assertEqual(reply['status'], 'accepted')
        self.assertEqual(reply['public_visibility'], 'not_applicable')
        for field in ('text', 'text_truncated', 'video', 'target_comment_id', 'rpid'):
            self.assertNotIn(field, reply)
        for secret in (SECRET, '9876543210', 'PRIVATE_RECEIPT'):
            self.assertNotIn(secret, json.dumps(result))

    def test_reply_text_is_bounded_and_never_taken_from_tool_result_prose(self):
        text = '中🙂' * 1200
        self.call('bili_reply', {'success': True, 'rpid_str': '9007199254740993', 'text': SECRET},
                  args={'bvid': BV, 'text': text})
        self.call('bili_reply', {'success': True, 'rpid': 202, 'message': SECRET})
        replies = self.snapshot()['mainAgent']['replies']
        self.assertNotIn('text', replies[0])
        self.assertEqual(replies[1]['text'], text[:2000])
        self.assertIs(replies[1]['text_truncated'], True)
        self.assertEqual(replies[1]['rpid'], '9007199254740993')
        self.assertNotIn(SECRET, json.dumps(replies))

    def test_reply_receipts_require_tool_specific_shapes_and_unambiguous_comment_id(self):
        values = [({'success': True, 'rpid': 202, 'rpid_str': '203'}, 'bili_reply'),
                  ({'ok': True, 'status': 'sent', 'receipt': 'receipt'}, 'bili_reply'),
                  ({'success': True, 'rpid': 202}, 'bili_send_message'),
                  ({'success': True, 'rpid': True}, 'bili_reply'),
                  ({'success': True, 'rpid_str': 'not-a-comment-id'}, 'bili_reply')]
        for value, tool in values: self.call(tool, value, args={'bvid': BV, 'text': '尝试'})
        replies = self.snapshot()['mainAgent']['replies']
        self.assertTrue(all(row['status'] == 'unknown' for row in replies))
        self.assertTrue(all(row['receipt_present'] is False for row in replies))

    def test_recent_video_aggregate_excludes_recommendations_and_action_only_targets(self):
        other, action_only = 'BV0000000001', 'BV0000000002'
        self.call('bili_recommend', {'ok': True, 'videos': [{'bvid': other, 'title': '仅候选'}]})
        self.call('bili_reply', {'success': True, 'rpid': 202}, args={'bvid': action_only, 'text': '仅评论'})
        self.call('bili_video_info', {'bvid': BV, 'title': '真实资料标题'}, args={'bvid': BV})
        self.call('bili_subtitle', {'bvid': BV, 'segments': 1, 'text': '实际公开字幕'}, args={'bvid': BV})
        self.call('bili_reply', {'success': True, 'rpid': 203}, args={'bvid': BV, 'text': '公开评论'})
        self.call('bili_frames', {'error': SECRET}, args={'bvid': BV})
        self.call('bili_cover', args={'bvid': BV}, pending=True)
        result = self.snapshot(); videos = result['mainAgent']['recent_videos']
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]['bvid'], BV)
        self.assertEqual(videos[0]['title'], '真实资料标题')
        self.assertEqual(videos[0]['record_count'], 5)
        self.assertEqual(videos[0]['subtitle_count'], 1)
        self.assertEqual(videos[0]['reply_count'], 1)
        self.assertEqual(videos[0]['kinds'], ['action', 'cover', 'details', 'frames', 'subtitle'])
        self.assertEqual(videos[0]['statuses'], ['failed', 'pending', 'succeeded'])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn('watched', json.dumps(videos))

    def test_failed_or_pending_video_reads_stay_attempts_and_limit_does_not_create_success(self):
        self.call('bili_video_info', {'error': SECRET}, args={'bvid': BV, 'title': SECRET})
        self.call('bili_subtitle', args={'bvid': 'BV0000000001'}, pending=True)
        main = self.snapshot(limit=1)['mainAgent']
        self.assertEqual(main['recent_videos'][0]['statuses'], ['pending'])
        self.assertEqual(main['counts']['details'], 0)
        self.assertEqual(main['counts']['subtitle'], 0)
        self.assertNotIn(SECRET, json.dumps(main))

    def test_comment_rewrite_preserves_original_receipt_and_foreign_owner_hides_public_attempts(self):
        self.call('bili_reply', {'success': True, 'rpid': 202}, args={'bvid': BV, 'text': '原始尝试正文'})
        rewritten = json.loads(json.dumps(self.rows[-1]['data']))
        rewritten['message']['content'] = [{'type': 'text', 'text': '压缩后的摘要'}]
        self.event('tool/result', rewritten, sourceEventSeqs=[1],
            surfaceOp={'op': 'replace', 'startSeq': 1, 'endSeq': 1})
        main = self.snapshot()['mainAgent']
        self.assertEqual(len(main['replies']), 1)
        self.assertEqual(main['replies'][0]['status'], 'accepted')
        self.assertEqual(main['replies'][0]['text'], '原始尝试正文')
        self.header['agentPreset'] = 'Foreign'
        main = self.snapshot()['mainAgent']
        self.assertEqual(main['status'], 'unavailable')
        self.assertEqual(main['replies'], [])
        self.assertEqual(main['recent_videos'], [])

    def test_current_workspace_preset_and_exact_session_header_are_required(self):
        self.call('bili_recommend', {'ok': True, 'videos': []})
        original = dict(self.header)
        for key, wrong in [('id', 'foreign'), ('cwd', str(self.root)), ('agentPreset', 'Other')]:
            self.header = dict(original, **{key: wrong})
            with self.subTest(key=key):
                main = self.snapshot()['mainAgent']
                self.assertEqual(main['status'], 'unavailable'); self.assertEqual(main['records'], [])
        self.header = original
        self.mapping.write_text(json.dumps({'private:101': {'session': 'fixture-session', 'workspacePath': str(self.root)}}))
        self.assertEqual(self.snapshot()['mainAgent']['status'], 'unavailable')

    def test_seed_prefix_is_dropped_and_foreign_preset_tools_are_excluded(self):
        self.header['isSeeded'] = True
        self.call('bili_recommend', {'ok': True, 'videos': [{'bvid': BV}]})
        self.event('session/end-seed', {'inherited': True})
        self.event('agent-preset/selected', {'agentPreset': 'Other'})
        self.call('bili_recommend', {'ok': True, 'videos': [{'bvid': BV}]})
        self.event('agent-preset/selected', {'agentPreset': 'Fixture'})
        self.call('bili_subtitle', {'bvid': BV, 'segments': 1, 'text': 'Synthetic subtitle'})
        main = self.snapshot()['mainAgent']
        self.assertEqual(main['status'], 'ok'); self.assertEqual(len(main['records']), 1)
        self.assertEqual(main['records'][0]['kind'], 'subtitle')

    def test_result_pair_mismatch_and_missing_seed_boundary_fail_closed(self):
        self.call('bili_recommend', {'ok': True, 'videos': []})
        self.rows[-1]['sourceEventSeqs'] = [99]
        self.assertEqual(self.snapshot()['mainAgent']['status'], 'unavailable')
        self.rows[-1]['sourceEventSeqs'] = [0]; self.header['isSeeded'] = True
        self.assertEqual(self.snapshot()['mainAgent']['status'], 'unavailable')

    def test_native_content_rewrite_counts_the_original_execution_once(self):
        self.call('bili_recommend', {'ok': True, 'videos': [{'bvid': BV}]})
        original = json.loads(json.dumps(self.rows[-1]['data']))
        rewritten = json.loads(json.dumps(original))
        rewritten['message']['content'] = [{'type': 'text', 'text': 'Compacted native tool output'}]
        replacement = self.event('tool/result', rewritten, sourceEventSeqs=[1],
            surfaceOp={'op': 'replace', 'startSeq': 1, 'endSeq': 1})
        self.event('tool/result', rewritten, sourceEventSeqs=[replacement['seq']],
            surfaceOp={'op': 'replace', 'startSeq': replacement['seq'], 'endSeq': replacement['seq']})
        main = self.snapshot()['mainAgent']
        self.assertEqual(main['status'], 'ok')
        self.assertEqual(main['counts']['recommendation'], 1)
        self.assertEqual(main['records'][0]['item_count'], 1)
        self.assertEqual(main['records'][0]['at'], self.rows[0]['time'])

    def test_duplicate_append_and_noncontent_rewrite_changes_are_rejected(self):
        self.call('bili_recommend', {'ok': True, 'videos': []})
        original = json.loads(json.dumps(self.rows[-1]['data']))
        self.event('tool/result', original, sourceEventSeqs=[1])
        self.assertEqual(self.snapshot()['mainAgent']['status'], 'unavailable')
        self.rows.pop()
        altered = json.loads(json.dumps(original)); altered['message']['isError'] = True
        self.event('tool/result', altered, sourceEventSeqs=[1],
            surfaceOp={'op': 'replace', 'startSeq': 1, 'endSeq': 1})
        self.assertEqual(self.snapshot()['mainAgent']['status'], 'unavailable')
        self.rows[-1]['data'] = original
        self.rows[-1]['surfaceOp']['endSeq'] = 0
        self.assertEqual(self.snapshot()['mainAgent']['status'], 'unavailable')

    def test_pending_real_calls_and_record_limit_do_not_change_full_window_counts(self):
        for _ in range(3): self.call('bili_recommend', {'ok': True, 'videos': []})
        self.call('bili_video_info', pending=True, args={'bvid': BV})
        main = self.snapshot(limit=1)['mainAgent']
        self.assertEqual(main['counts']['recommendation'], 3)
        self.assertEqual(main['counts']['pending'], 1)
        self.assertEqual(main['records'][0]['status'], 'pending')
        self.assertTrue(main['coverage']['truncated'])

    def test_two_configured_scopes_cannot_alias_the_same_log(self):
        self.identity.write_text('{"adminQQ":["101"],"fanGroupQQ":"303"}')
        self.mapping.write_text('{"private:101":"fixture-session","group:303":"fixture-session"}')
        main = self.snapshot()['mainAgent']
        self.assertEqual(main['status'], 'unavailable')
        self.assertIn('mapping_ambiguous', main['coverage']['reasons'])

    def test_empty_configured_preset_and_duplicate_seed_boundaries_are_rejected(self):
        self.write()
        for preset in ('', '  ', False):
            with self.subTest(preset=preset):
                main = activity.activity_snapshot(workspace=str(self.work), agent_preset=preset,
                    identity_file=self.identity, dsh_home=self.home, now=NOW)['mainAgent']
                self.assertEqual(main['status'], 'unavailable')
                self.assertEqual(main['records'], [])
        self.header['isSeeded'] = True
        self.event('session/end-seed', {'inherited': True})
        self.event('session/end-seed', {'inherited': True})
        self.call('bili_recommend', {'ok': True, 'videos': []})
        self.assertEqual(self.snapshot()['mainAgent']['status'], 'unavailable')

    def test_malformed_missing_or_overlarge_source_is_unknown_not_successful_empty(self):
        self.write()
        self.path.write_text('{bad\n')
        def get(): return activity.activity_snapshot(workspace=str(self.work), agent_preset='Fixture',
            identity_file=self.identity, dsh_home=self.home, now=NOW)['mainAgent']
        self.assertEqual(get()['status'], 'unavailable')
        self.path.unlink(); self.assertEqual(get()['status'], 'unavailable')
        self.write()
        with mock.patch.object(activity, 'MAX_NATIVE_SOURCE', 2):
            self.assertEqual(get()['status'], 'unavailable')

    def test_zstd_native_log_uses_the_same_owner_and_result_pair_validation(self):
        try: import zstandard
        except ImportError: self.skipTest('optional zstandard not installed')
        self.call('bili_recommend', {'ok': True, 'videos': []})
        raw = self.write(); self.path.unlink()
        self.path.with_suffix('.jsonl.zstd').write_bytes(zstandard.ZstdCompressor().compress(raw))
        result = activity.activity_snapshot(workspace=str(self.work), agent_preset='Fixture',
            identity_file=self.identity, dsh_home=self.home, now=NOW)
        self.assertEqual(result['mainAgent']['counts']['recommendation'], 1)

    def large_native(self):
        """A real file larger than both former whole-log ceilings."""
        padding = SECRET * 2048
        with self.path.open('wb') as stream:
            stream.write((json.dumps(self.header) + '\n').encode())
            index = 0
            while stream.tell() < 68 * 1024 * 1024:
                stream.write((json.dumps({'type': 'assistant/message', 'seq': index,
                    'time': NOW - 2000, 'data': {'message': {'content': padding}}}) + '\n').encode())
                index += 1
            call = {'type': 'tool/call', 'seq': index, 'time': NOW - 1000,
                    'data': {'name': 'bili_video_info', 'callId': 'large-call', 'turn': 1, 'step': 1,
                             'arguments': json.dumps({'bvid': BV})}}
            result = {'type': 'tool/result', 'seq': index + 1, 'time': NOW - 900,
                'surfaceOp': 'append', 'sourceEventSeqs': [index],
                'data': {'turn': 1, 'step': 1, 'message': {'source': {'callId': 'large-call'},
                    'toolCallId': 'large-call', 'content': [{'type': 'text', 'text': json.dumps({'bvid': BV, 'title': 'Public large-source video'})}]}}}
            for row in (call, result): stream.write((json.dumps(row) + '\n').encode())

    def read_current(self):
        return activity.activity_snapshot(workspace=str(self.work), agent_preset='Fixture',
            identity_file=self.identity, dsh_home=self.home, now=NOW)['mainAgent']

    def test_large_plain_native_stream_does_not_materialize_whole_log(self):
        self.large_native()
        self.assertGreater(self.path.stat().st_size, 64 * 1024 * 1024)
        tracemalloc.start()
        try:
            result = self.read_current()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['counts']['details'], 1)
        self.assertLess(peak, 8 * 1024 * 1024)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_large_expanded_zstd_stream_retains_original_pair_and_final_owner(self):
        try: import zstandard
        except ImportError: self.skipTest('optional zstandard not installed')
        self.large_native()
        compressed = self.path.with_suffix('.jsonl.zstd')
        with self.path.open('rb') as source, compressed.open('wb') as target:
            with zstandard.ZstdCompressor().stream_writer(target, closefd=False) as writer:
                while chunk := source.read(64 * 1024): writer.write(chunk)
        self.path.unlink()
        self.assertLess(compressed.stat().st_size, 32 * 1024 * 1024)
        self.assertEqual(self.read_current()['counts']['details'], 1)
        # A later frame changing the actual owner invalidates the projection.
        with compressed.open('rb') as raw, zstandard.ZstdDecompressor().stream_reader(raw) as stream:
            count = sum(chunk.count(b'\n') for chunk in iter(lambda: stream.read(64 * 1024), b'')) - 1
        event = {'type': 'agent-preset/selected', 'seq': count, 'time': NOW,
                 'data': {'agentPreset': 'foreign'}}
        with compressed.open('ab') as target:
            target.write(zstandard.ZstdCompressor().compress((json.dumps(event) + '\n').encode()))
        result = self.read_current()
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('owner_mismatch', result['coverage']['reasons'])

    def test_stream_limits_and_incomplete_tail_remain_unknown(self):
        self.call('bili_video_info', {'bvid': BV}, args={'bvid': BV}); raw = self.write()
        for name, ceiling in [('MAX_EXPANDED', len(raw) - 1), ('MAX_LINE', 20), ('MAX_ROWS', 1)]:
            with self.subTest(name=name), mock.patch.object(activity, name, ceiling):
                result = self.read_current()
                self.assertEqual(result['status'], 'unavailable')
                self.assertIn('source_limit', result['coverage']['reasons'])
        self.path.write_bytes(raw[:-1])
        self.assertEqual(self.read_current()['status'], 'unavailable')

    def test_later_append_or_replacement_does_not_change_the_captured_projection(self):
        self.call('bili_video_info', {'bvid': BV, 'title': 'Public fixture'}, args={'bvid': BV}); raw = self.write()
        original = activity._native_events
        for replace in (False, True):
            self.path.write_bytes(raw)
            def changing(*args, **kwargs):
                result = original(*args, **kwargs)
                if replace:
                    self.path.unlink(); self.path.write_bytes(raw.replace(b'"Fixture"', b'"foreign"'))
                else:
                    with self.path.open('ab') as stream: stream.write(b'{}\n')
                return result
            with self.subTest(replace=replace), mock.patch.object(activity, '_native_events', side_effect=changing):
                result = self.read_current()
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['counts']['details'], 1)
            self.assertEqual(self.read_current()['status'], 'unavailable')

    def test_source_changing_during_capture_discards_the_projection(self):
        self.call('bili_video_info', {'bvid': BV}, args={'bvid': BV}); self.write()
        factory = activity.tempfile.SpooledTemporaryFile
        target = self.path
        class ChangingSnapshot:
            def __init__(self, *args, **kwargs): self.file = factory(*args, **kwargs); self.changed = False
            def __enter__(self): return self
            def __exit__(self, *args): self.file.close()
            def __getattr__(self, name): return getattr(self.file, name)
            def write(self, value):
                result = self.file.write(value)
                if not self.changed:
                    self.changed = True
                    with target.open('ab') as stream: stream.write(b'{}\n')
                return result
        with mock.patch.object(activity.tempfile, 'SpooledTemporaryFile', ChangingSnapshot):
            result = self.read_current()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['records'], [])
        self.assertIn('source_changing', result['coverage']['reasons'])

    def test_corrupt_compression_is_reported_without_raising_out_of_the_snapshot(self):
        try: import zstandard
        except ImportError: self.skipTest('optional zstandard not installed')
        self.path.with_suffix('.jsonl.zstd').write_bytes(b'invalid compressed fixture')
        result = self.read_current()
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('malformed_source', result['coverage']['reasons'])

    def test_linked_native_source_is_refused_without_following_it(self):
        raw = self.write(); target = self.root / 'foreign-log'; target.write_bytes(raw); self.path.unlink()
        try: self.path.symlink_to(target)
        except OSError: self.skipTest('symlink creation unavailable')
        main = activity.activity_snapshot(workspace=str(self.work), agent_preset='Fixture',
            identity_file=self.identity, dsh_home=self.home, now=NOW)['mainAgent']
        self.assertEqual(main['status'], 'unavailable')
        self.assertIn('unsafe_source', main['coverage']['reasons'])

    def trusted_options(self):
        self.write()
        return dict(workspace=str(self.work), agent_preset='Fixture',
                    identity_file=self.identity, dsh_home=self.home, now=NOW)

    def test_turn_episode_has_actual_public_content_and_no_private_or_reasoning_text(self):
        self.event('turn/start', {'turn': 1})
        self.event('user/message', {'source': {'kind': 'plugin:dsh-lanya-observation'}, 'content': SECRET})
        self.event('assistant/message', {'message': {'content': [{'type': 'reasoning', 'text': SECRET},
                                                                  {'type': 'text', 'text': SECRET}]}})
        self.call('bili_video_info', {'bvid': BV, 'title': '真正返回的标题',
                  'owner': {'name': '公开作者', 'mid': 9_999_999_999}, 'cookie': SECRET},
                  args={'bvid': BV, 'title': SECRET, 'author': SECRET})
        public = '这是一段实际返回的公开字幕。\n' * 200
        self.call('bili_subtitle', {'bvid': BV, 'segments': 2, 'text': public}, args={'bvid': BV})
        self.call('exec', {'stdout': SECRET}, args={'command': SECRET})
        self.call('qq_decide', {'accepted': True}, args={'verdict': 'idle', 'reasoning': SECRET})
        self.event('turn/end', {'turn': 1})
        result = self.snapshot(); recent = result['recentActivity']
        self.assertEqual(len(recent['episodes']), 1)
        episode = recent['episodes'][0]
        self.assertEqual(episode['trigger_origin'], 'observation')
        self.assertEqual(episode['turn_count'], 1)
        self.assertEqual(episode['status'], 'observed')
        self.assertEqual(episode['videos'][0]['author'], '公开作者')
        self.assertIn('真正返回的标题', episode['title'])
        subtitle = next(row for row in episode['evidence'] if row['tool'] == 'bili_subtitle')
        self.assertEqual(subtitle['content_excerpt'], public.strip()[:activity.MAX_CONTENT_EXCERPT])
        self.assertTrue(subtitle['content_truncated'])
        self.assertIn('不代表完整播放', subtitle['summary'])
        self.assertEqual(recent['decisions'][0]['verdict'], 'idle')
        self.assertEqual(recent['decisions'][0]['status'], 'accepted')
        self.assertNotIn('qq_decide', episode['tool_names'])
        text = json.dumps(result)
        for value in (SECRET, str(9_999_999_999), self.header['id'], '_episode_id', 'command', 'reasoning'):
            self.assertNotIn(value, text)

    def test_idle_decisions_do_not_push_real_activity_out_of_episode_limit(self):
        self.call('bili_video_info', {'bvid': BV, 'title': '实际活动'}, args={'bvid': BV}, turn=1)
        for turn in range(2, 52):
            self.call('qq_decide', {'accepted': True}, args={'verdict': 'idle'}, turn=turn)
        recent = self.snapshot(limit=1)['recentActivity']
        self.assertEqual(len(recent['episodes']), 1)
        self.assertIn('实际活动', recent['episodes'][0]['title'])
        self.assertEqual(recent['counts'], {'episodes': 1, 'decisions': 50, 'tools': 1})
        self.assertFalse(recent['coverage']['truncated'])
        self.assertTrue(recent['coverage']['records_truncated'])
        self.assertTrue(recent['coverage']['decisions_truncated'])

    def test_only_complete_native_single_input_turns_have_a_known_origin(self):
        cases = [(['plugin:dsh-onebot', 'runtime-context'], 'live'),
                 (['plugin:dsh-lanya-observation', 'agent-instructions'], 'observation'),
                 (['plugin:dsh-lanya-observation', 'plugin:dsh-onebot'], 'unknown'),
                 (['runtime-context'], 'unknown'), (['other'], 'unknown')]
        for turn, (kinds, expected) in enumerate(cases, 1):
            self.event('turn/start', {'turn': turn})
            for kind in kinds:
                self.event('user/message', {'source': {'kind': kind}, 'content': SECRET})
            self.call('bili_video_info', {'bvid': BV, 'title': expected}, args={'bvid': BV}, turn=turn)
            self.event('turn/end', {'turn': turn})
        self.event('turn/start', {'turn': 6})
        self.event('user/message', {'source': {'kind': 'plugin:dsh-onebot'}})
        self.call('read', {'text': SECRET}, turn=6)
        episodes = self.snapshot()['recentActivity']['episodes']
        self.assertEqual([row['trigger_origin'] for row in episodes],
                         ['unknown', *[expected for _, expected in reversed(cases)]])
        self.assertNotIn(SECRET, json.dumps(episodes))

    def test_failed_pending_and_unknown_tool_results_never_become_accomplishments(self):
        self.call('bili_video_info', {'error': SECRET}, args={'bvid': BV}, turn=1)
        self.call('bili_subtitle', args={'bvid': BV}, pending=True, turn=2)
        self.call('bili_subtitle', {'bvid': BV, 'text': SECRET, 'segments': 0}, args={'bvid': BV}, turn=3)
        self.call('exec', {'stdout': 'I watched everything ' + SECRET}, args={'command': SECRET}, turn=4)
        episodes = self.snapshot()['recentActivity']['episodes']
        self.assertEqual([row['status'] for row in episodes], ['returned', 'unknown', 'pending', 'failed'])
        self.assertTrue(all('content_excerpt' not in item for row in episodes for item in row['evidence']))
        self.assertNotIn(SECRET, json.dumps(episodes))

    def test_daily_facts_exact_beijing_day_reads_beyond_seven_days_and_counts_before_limits(self):
        target = activity.dt.datetime.fromtimestamp(NOW / 1000, activity.CHINA).date() - activity.dt.timedelta(days=10)
        start = int(activity.dt.datetime.combine(target, activity.dt.time(), activity.CHINA).timestamp() * 1000)
        stamps = [start - 1, start, start + 1, start + 86400000]
        for turn, stamp in enumerate(stamps, 1):
            begin = len(self.rows)
            self.call('bili_video_info', {'bvid': BV, 'title': '日边界事实'}, args={'bvid': BV}, turn=turn)
            for row in self.rows[begin:]: row['time'] = stamp
        options = self.trusted_options()
        view = activity.daily_facts(day=target.isoformat(), limit=1, **options)
        self.assertEqual(view['counts']['episodes'], 2)
        self.assertEqual(view['counts']['tools'], 2)
        self.assertTrue(view['coverage']['truncated'])
        self.assertTrue(view['coverage']['day_closed'])
        self.assertFalse(view['coverage']['all_account_history_complete'])
        self.assertEqual(view['coverage']['window_start'], start)
        self.assertEqual(view['coverage']['window_end'], start + 86400000)
        self.assertEqual(view['episodes'][0]['started_at'], start + 1)
        self.assertEqual(activity.activity_snapshot(**options)['recentActivity']['counts']['episodes'], 0)

    def test_daily_catalog_parses_once_and_matches_each_daily_view_without_tail_cutoff(self):
        for turn in range(1, 45):
            begin = len(self.rows)
            self.call('read', {'text': SECRET}, args={'path': SECRET}, turn=turn)
            for row in self.rows[begin:]: row['time'] = NOW - (turn % 3) * 86400000 - 10000
        options = self.trusted_options()
        with mock.patch.object(activity, '_native', wraps=activity._native) as parser:
            catalog = activity.daily_catalog(**options)
        self.assertEqual(parser.call_count, 1)
        self.assertEqual(sum(row['counts']['tools'] for row in catalog['facts']), 44)
        for fact in catalog['facts']:
            self.assertEqual(fact, activity.daily_facts(day=fact['day'], **options))
        self.assertNotIn(SECRET, json.dumps(catalog))

    def test_episode_evidence_is_bounded_without_losing_full_tool_count_or_old_inode_checks(self):
        for _ in range(activity.MAX_EPISODE_EVIDENCE + 5): self.call('read', {'text': SECRET})
        recent = self.snapshot()['recentActivity']
        episode = recent['episodes'][0]
        self.assertEqual(episode['evidence_total'], activity.MAX_EPISODE_EVIDENCE + 5)
        self.assertEqual(len(episode['evidence']), activity.MAX_EPISODE_EVIDENCE)
        self.assertTrue(episode['evidence_truncated'])
        self.assertEqual(recent['counts']['tools'], activity.MAX_EPISODE_EVIDENCE + 5)
        self.header['agentPreset'] = 'Foreign'
        unavailable = activity.daily_catalog(**self.trusted_options())
        self.assertEqual(unavailable['status'], 'unavailable')
        self.assertTrue(all(row['episodes'] == [] for row in unavailable['facts']))

    def test_exact_day_invalid_or_future_input_is_refused(self):
        for day in ('2026-99-99', '2026-1-1', '2039-01-01', '../private', None):
            with self.subTest(day=day), self.assertRaises(activity.ActivityUnavailable):
                activity.daily_facts(day=day, **self.trusted_options())

    def test_catalog_does_not_make_a_future_journal_date_from_clock_tolerance(self):
        today = activity.dt.datetime.fromtimestamp(NOW / 1000, activity.CHINA).date()
        next_start = int(activity.dt.datetime.combine(today + activity.dt.timedelta(days=1),
                         activity.dt.time(), activity.CHINA).timestamp() * 1000)
        self.call('read_image', {'text': SECRET})
        for row in self.rows: row['time'] = next_start + 10
        options = dict(self.trusted_options(), now=next_start - 10)
        self.assertEqual(activity.activity_snapshot(**options)['recentActivity']['counts']['tools'], 1)
        catalog = activity.daily_catalog(**options)
        self.assertEqual(catalog['days'], [today.isoformat()])
        self.assertEqual(catalog['facts'][0]['counts']['tools'], 0)
        self.assertEqual(catalog['coverage']['future_day_records_omitted'], 1)

    def test_actual_tool_families_get_readable_labels_without_raw_operations(self):
        for name in ('pwsh', 'read_image', 'glob', 'edit'):
            self.call(name, {'stdout': SECRET}, args={'command': SECRET, 'path': SECRET})
        episode = self.snapshot()['recentActivity']['episodes'][0]
        self.assertEqual(episode['title'], '运行本地工具与查看结果')
        self.assertIn('运行PowerShell工具 1 次', episode['summary'])
        self.assertIn('查看图片或截图 1 次', episode['summary'])
        self.assertIn('查找文件 1 次', episode['summary'])
        self.assertNotIn(SECRET, json.dumps(episode))

    def test_successful_subtitle_does_not_hide_unknown_action_or_label_a_different_failed_video(self):
        self.call('bili_video_info', {'bvid': BV, 'title': '确实读取的字幕视频'}, args={'bvid': BV})
        self.call('bili_subtitle', {'bvid': BV, 'text': '实际字幕', 'segments': 1}, args={'bvid': BV})
        other = 'BV0000000001'
        self.call('bili_video_info', {'error': SECRET}, args={'bvid': other})
        self.call('bili_reply', {'success': True}, args={'bvid': other, 'text': '尝试评论'})
        episode = self.snapshot()['recentActivity']['episodes'][0]
        self.assertEqual(episode['status'], 'partial')
        self.assertEqual(episode['title'], '读取B站：确实读取的字幕视频')
        self.assertEqual(episode['evidence'][-1]['reply']['status'], 'unknown')
        self.assertNotIn(SECRET, json.dumps(episode))

    def test_source_binding_changes_with_mapping_but_not_append_or_content_compaction(self):
        self.call('bili_video_info', {'bvid': BV, 'title': '公开资料'}, args={'bvid': BV})
        def binding():
            return self.snapshot()['recentActivity']['coverage']['source_windows'][0]['binding_sha256']
        first = binding()
        self.assertRegex(first, '^[0-9a-f]{64}$')
        rewritten = json.loads(json.dumps(self.rows[-1]['data']))
        rewritten['message']['content'] = [{'type': 'text', 'text': '压缩后的公开摘要'}]
        self.event('tool/result', rewritten, sourceEventSeqs=[1],
                   surfaceOp={'op': 'replace', 'startSeq': 1, 'endSeq': 1})
        self.assertEqual(binding(), first)
        self.call('read_image', {'text': SECRET}, turn=2)
        self.assertEqual(binding(), first)
        new_session = 'different-fixture-session'
        self.header['id'] = new_session
        self.mapping.write_text(json.dumps({'private:101': new_session}))
        self.path = self.path.parent.parent / activity._segment(new_session) / 'session.v4.jsonl'
        self.path.parent.mkdir()
        self.assertNotEqual(binding(), first)
        text = json.dumps(self.snapshot())
        self.assertNotIn(new_session, text)
        self.assertNotIn(str(self.work), text)


if __name__ == '__main__': unittest.main()
