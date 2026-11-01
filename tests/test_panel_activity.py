"""Native record projection uses temporary synthetic sessions, no host/API."""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
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

    def call(self, name, output=None, *, args=None, error=False, pending=False):
        call_id = 'call-' + str(len(self.rows))
        call = self.event('tool/call', {'name': name, 'callId': call_id, 'turn': 1,
                                      'step': len(self.rows), 'arguments': json.dumps(args or {})})
        if not pending:
            self.event('tool/result', {'turn': 1, 'step': call['data']['step'],
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

    def test_actual_native_shapes_remain_distinct_and_private_text_is_not_projected(self):
        self.event('user/message', {'content': [{'type': 'text', 'text': SECRET}]})
        self.call('bili_recommend', {'ok': True, 'videos': [{'bvid': BV, 'title': 'Public fixture'}, {'bvid': BV}]})
        self.call('bili_video_info', {'bvid': BV, 'title': 'Public fixture'}, args={'bvid': BV})
        self.call('bili_subtitle', {'bvid': BV, 'title': 'Public fixture', 'segments': 2, 'text': SECRET}, args={'bvid': BV})
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
        self.assertNotIn(text, json.dumps(result['recentActivity'], ensure_ascii=False))

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
        self.call('bili_subtitle', {'bvid': BV, 'segments': 1, 'text': SECRET}, args={'bvid': BV})
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
        with mock.patch.object(activity, 'MAX_SOURCE', 2):
            # read_bytes receives its explicit native bound below; cap that path.
            original = activity.read_bytes
            with mock.patch.object(activity, 'read_bytes', side_effect=lambda path, maximum=activity.MAX_SOURCE:
                    original(path, 2 if Path(path) == self.path else maximum)):
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

    def test_linked_native_source_is_refused_without_following_it(self):
        raw = self.write(); target = self.root / 'foreign-log'; target.write_bytes(raw); self.path.unlink()
        try: self.path.symlink_to(target)
        except OSError: self.skipTest('symlink creation unavailable')
        main = activity.activity_snapshot(workspace=str(self.work), agent_preset='Fixture',
            identity_file=self.identity, dsh_home=self.home, now=NOW)['mainAgent']
        self.assertEqual(main['status'], 'unavailable')
        self.assertIn('unsafe_source', main['coverage']['reasons'])


if __name__ == '__main__': unittest.main()
