"""Synthetic native records only; no host, account, network or model."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import chat_export as export
import panel_activity as native

NOW = 1800000000000


class ChatExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='scoped export fixture ')
        self.addCleanup(temporary.cleanup); self.root = Path(temporary.name)
        self.workspace = self.root / 'workspace'; self.workspace.mkdir()
        self.role = self.workspace / 'character'; self.role.mkdir()
        self.home = self.root / 'native'; self.home.mkdir()
        self.mapping = self.home / 'media/onebot/chat-sessions.json'; self.mapping.parent.mkdir(parents=True)
        self.mapping.write_text('{"private:101":"mapped"}', encoding='utf8')
        self.header = dict(type='session', version=4, id='mapped', cwd=str(self.workspace), agentPreset='Fixture', isSeeded=False)
        self.rows = []
        self.source = self.home / 'sessions' / native._project(str(self.workspace)) / 'mapped' / 'session.v4.jsonl'
        self.source.parent.mkdir(parents=True)
        for name in ('subprocess.run', 'subprocess.Popen', 'urllib.request.urlopen'):
            patch = mock.patch(name, side_effect=AssertionError('external operation forbidden'))
            patch.start(); self.addCleanup(patch.stop)

    def event(self, typ, data, **extra):
        row = dict(type=typ, seq=len(self.rows), time=NOW + len(self.rows), data=data, **extra)
        self.rows.append(row); return row

    def incoming(self, text='human fixture'):
        return self.event('user/message', dict(source=dict(kind='plugin:dsh-onebot'), content=[dict(type='text', text=text)]))

    def write(self):
        self.source.write_bytes(b''.join((json.dumps(x, ensure_ascii=False)+'\n').encode() for x in [self.header, *self.rows]))

    def run_export(self):
        return export.export_current(workspace=str(self.workspace), preset='Fixture', role=self.role,
                                     dsh_home=self.home, mapping_file=self.mapping)

    def outputs(self):
        directory, _ = export.current_directory(str(self.workspace), 'Fixture', self.role)
        return sorted(directory.glob('qq-*.md'))

    def test_only_current_mapping_is_read_even_if_foreign_workspace_is_newest(self):
        self.incoming(); self.write()
        foreign = self.home / 'sessions' / 'newest-foreign-workspace'; foreign.mkdir()
        (foreign / 'session.v4.jsonl').write_text('FOREIGN_PRIVATE_FIXTURE', encoding='utf8')
        gui = self.source.parent.parent / 'session-unmapped-gui'; gui.mkdir()
        (gui / 'session.v4.jsonl').write_text('UNMAPPED_PRIVATE_FIXTURE', encoding='utf8')
        with mock.patch.object(native, '_native_lines', wraps=native._native_lines) as reader:
            result = self.run_export()
        self.assertEqual(result['written'], 1)
        self.assertEqual([call.args[0] for call in reader.call_args_list], [self.source])
        body = self.outputs()[0].read_text(encoding='utf8')
        self.assertIn('human fixture', body); self.assertNotIn('PRIVATE_FIXTURE', body)

    def test_single_real_message_is_kept_and_legacy_or_manual_files_are_untouched(self):
        self.incoming(); self.write()
        legacy = self.role / 'chats'; legacy.mkdir()
        manual = legacy / 'manual.md'; manual.write_bytes(b'manual original')
        old = legacy / 'session-legacy.md'; old.write_bytes(b'legacy original')
        self.assertEqual(self.run_export()['messages'], 1)
        self.assertEqual(manual.read_bytes(), b'manual original'); self.assertEqual(old.read_bytes(), b'legacy original')
        body = self.outputs()[0].read_text(encoding='utf8')
        self.assertIn('+08:00', body); self.assertIn('对方', body)

    def test_unchanged_native_source_does_not_parse_or_rewrite_again(self):
        self.incoming(); self.write(); self.run_export()
        file = self.outputs()[0]; before = file.stat()
        with mock.patch.object(native, '_native_lines', side_effect=AssertionError('unchanged source parsed')):
            result = self.run_export()
        after = file.stat()
        self.assertEqual(result['cached'], 1); self.assertEqual(result['written'], 0)
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_internal_only_append_updates_cache_without_rewriting_text(self):
        self.incoming(); self.write(); self.run_export()
        file = self.outputs()[0]; before = file.stat()
        self.event('request/header', {'private_internal': 'NOT_A_CHAT_MESSAGE'}); self.write()
        result = self.run_export(); after = file.stat()
        self.assertEqual(result['written'], 0)
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertNotIn('NOT_A_CHAT_MESSAGE', file.read_text(encoding='utf8'))

    def test_changed_generated_output_preserves_previous_bytes_and_descriptor(self):
        self.incoming('first fixture'); self.write(); self.run_export()
        file = self.outputs()[0]; original = file.read_bytes()
        self.incoming('second fixture'); self.write(); self.run_export()
        archived = list((file.parent / '.history').glob('*.md'))
        self.assertEqual(len(archived), 1); self.assertEqual(archived[0].read_bytes(), original)
        self.assertTrue(archived[0].with_suffix('.md.sd').is_file())
        self.assertIn('second fixture', file.read_text(encoding='utf8'))

    def test_manual_edit_of_generated_output_is_not_overwritten(self):
        self.incoming(); self.write(); self.run_export()
        file = self.outputs()[0]; file.write_bytes(b'manual changed original')
        self.incoming('new fixture'); self.write()
        self.assertEqual(self.run_export()['status'], 'partial')
        self.assertEqual(file.read_bytes(), b'manual changed original')

    def test_host_context_reasoning_and_other_presets_are_not_conversation_text(self):
        self.event('user/message', dict(source=dict(kind='agent-instructions'), content='HOST_PRIVATE_FIXTURE'))
        self.event('assistant/message', dict(content=[dict(type='reasoning', text='REASONING_PRIVATE_FIXTURE')]))
        self.event('agent-preset/selected', dict(agentPreset='Other'))
        self.incoming('OTHER_PRESET_PRIVATE_FIXTURE')
        self.event('agent-preset/selected', dict(agentPreset='Fixture')); self.incoming('visible fixture')
        self.write(); self.run_export()
        body = self.outputs()[0].read_text(encoding='utf8')
        self.assertIn('visible fixture', body); self.assertNotIn('PRIVATE_FIXTURE', body)

    def inline(self, *, good=True, **changes):
        self.event('turn/start', dict(turn=1)); self.incoming()
        call = self.event('tool/call', dict(name='qq_decide', callId='fixture-call', turn=1, step=2,
                                         arguments=json.dumps(dict(text='accepted inline fixture'))))
        event = self.event('tool/result', dict(turn=1, step=2, message=dict(source=dict(callId='fixture-call'),
            toolCallId='fixture-call', isError=False, content=[dict(type='text', text=json.dumps(dict(sent=good, inline_text=True)))])),
            surfaceOp='append', sourceEventSeqs=[call['seq']])
        for key, value in changes.items():event[key] = value
        self.event('assistant/message', dict(message=dict(content=[dict(type='text', text='already sent narration fixture')])))
        self.event('turn/end', dict(turn=1))
        return event

    def test_exact_inline_receipt_and_generated_narration_are_distinct(self):
        self.inline(); self.write(); self.run_export()
        body = self.outputs()[0].read_text(encoding='utf8')
        self.assertIn('accepted inline fixture', body); self.assertIn('QQ接口接受', body)
        self.assertNotIn('already sent narration fixture', body)
        self.assertIn('不代表客户端可见', body)

    def test_mismatched_unknown_or_rewritten_receipts_do_not_publish_drafts(self):
        for variation in ('unknown', 'seq', 'step', 'call', 'rewrite', 'error'):
            with self.subTest(variation=variation):
                self.rows = []; event = self.inline(good=variation != 'unknown')
                if variation == 'seq':event['sourceEventSeqs'] = [999]
                if variation == 'step':event['data']['step'] = 3
                if variation == 'call':event['data']['message']['toolCallId'] = 'other'
                if variation == 'rewrite':event['surfaceOp'] = 'replace'
                if variation == 'error':event['data']['message']['isError'] = True
                self.write(); self.run_export(); body = self.outputs()[0].read_text(encoding='utf8')
                self.assertNotIn('accepted inline fixture', body)
                self.assertIn('助手生成', body)

    def test_seeded_prefix_is_discarded_and_missing_boundary_is_refused(self):
        self.header['isSeeded'] = True; self.incoming('SEEDED_PRIVATE_FIXTURE')
        self.event('session/end-seed', dict(inherited=True)); self.incoming('own fixture'); self.write()
        self.run_export(); self.assertNotIn('SEEDED_PRIVATE_FIXTURE', self.outputs()[0].read_text(encoding='utf8'))
        old = self.outputs()[0].read_bytes()
        self.rows = []; self.incoming('UNBOUNDED_SEED_PRIVATE_FIXTURE'); self.write()
        self.assertEqual(self.run_export()['status'], 'partial'); self.assertEqual(self.outputs()[0].read_bytes(), old)

    def test_header_owner_mismatch_and_ambiguous_mapping_refuse_without_fallback(self):
        self.incoming(); self.header['cwd'] = str(self.root / 'foreign'); self.write()
        self.assertEqual(self.run_export()['errors'], 1); self.assertEqual(self.outputs(), [])
        self.header['cwd'] = str(self.workspace); self.write()
        self.mapping.write_text('{"private:101":"mapped","group:202":"mapped"}', encoding='utf8')
        self.assertEqual(self.run_export()['errors'], 2); self.assertEqual(self.outputs(), [])

    def test_source_append_after_snapshot_is_not_cached_as_latest(self):
        self.incoming('first fixture'); self.write()
        reader = native._native_lines
        @contextlib.contextmanager
        def append_after_capture(path):
            with reader(path) as lines:
                self.incoming('later fixture'); self.write()
                yield lines
        with mock.patch.object(native, '_native_lines', append_after_capture):self.run_export()
        self.assertNotIn('later fixture', self.outputs()[0].read_text(encoding='utf8'))
        self.assertEqual(self.run_export()['cached'], 0)
        self.assertIn('later fixture', self.outputs()[0].read_text(encoding='utf8'))

    def test_tail_is_bounded_and_real_group_sender_is_not_renamed_to_owner(self):
        self.mapping.write_text('{"group:202":"mapped"}', encoding='utf8')
        for index in range(export.MAX_MESSAGES + 10):self.incoming('MESSAGE-%04d' % index)
        self.write(); result = self.run_export(); body = self.outputs()[0].read_text(encoding='utf8')
        self.assertEqual(result['messages'], export.MAX_MESSAGES)
        self.assertNotIn('> MESSAGE-0000\n', body); self.assertIn('群消息', body)

    def test_compressed_frames_and_empty_sessions(self):
        self.write(); result = self.run_export(); self.assertEqual(result['messages'], 0); self.assertEqual(self.outputs(), [])
        import zstandard
        self.incoming(); self.write(); raw = self.source.read_bytes(); self.source.unlink()
        parts = raw.splitlines(keepends=True)
        compressed = self.source.with_suffix('.jsonl.zstd')
        compressed.write_bytes(b''.join(zstandard.ZstdCompressor().compress(part) for part in parts))
        self.assertEqual(self.run_export()['written'], 1)

    def test_source_limit_failure_does_not_replace_last_good_output(self):
        self.incoming(); self.write(); self.run_export(); previous = self.outputs()[0].read_bytes()
        self.incoming('second fixture'); self.write()
        with mock.patch.object(native, 'MAX_NATIVE_SOURCE', 1):
            self.assertEqual(self.run_export()['status'], 'partial')
        self.assertEqual(self.outputs()[0].read_bytes(), previous)

    def test_removed_mapping_archives_only_owned_output_and_keeps_manual_file(self):
        self.incoming(); self.write(); self.run_export()
        output = self.outputs()[0]; before = output.read_bytes()
        manual = output.parent / 'manual.md'; manual.write_bytes(b'manual retained')
        self.mapping.write_text('{}', encoding='utf8')
        result = self.run_export()
        self.assertEqual(result['archived'], 1); self.assertEqual(result['messages'], 0)
        self.assertEqual(self.outputs(), []); self.assertEqual(manual.read_bytes(), b'manual retained')
        self.assertEqual(next((output.parent / '.history').glob('*.md')).read_bytes(), before)

    def test_valid_empty_replacement_archives_previous_generation_without_deleting_it(self):
        self.incoming(); self.write(); self.run_export()
        output = self.outputs()[0]; before = output.read_bytes()
        self.rows = []; self.write(); result = self.run_export()
        self.assertEqual(result['archived'], 1); self.assertEqual(self.outputs(), [])
        self.assertEqual(next((output.parent / '.history').glob('*.md')).read_bytes(), before)

    def test_main_reports_no_private_exception_details(self):
        import project_conf
        for exception in (ValueError, project_conf.ConfigError):
            with self.subTest(exception=exception):
                stream = io.StringIO()
                with mock.patch.object(project_conf, 'load', side_effect=exception('PRIVATE_FIXTURE_DETAILS')), contextlib.redirect_stdout(stream):
                    self.assertEqual(export.main(), 2)
                self.assertNotIn('PRIVATE_FIXTURE_DETAILS', stream.getvalue())


if __name__ == '__main__':
    unittest.main()
