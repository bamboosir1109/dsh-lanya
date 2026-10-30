"""Real producer entry points, isolated files/inbox and inert model/network sinks."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import decision_events
import meme_observations as producer
import project_conf


class MemeObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='meme observation ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'character'
        self.memes = self.data / 'memes'
        self.stage = self.memes / '_stage'
        self.stage.mkdir(parents=True)
        self.settings = {}
        self.calls = []
        for patcher in [mock.patch.object(producer, 'data_dir', return_value=str(self.data)),
                        mock.patch.object(producer, 'get', side_effect=lambda key, default=None: self.settings.get(key, default)),
                        mock.patch.object(producer, 'project_scope', side_effect=lambda target: 'group:202' if target == 'group' else 'private:101'),
                        mock.patch.object(producer, 'unified_decisions_enabled', return_value=True),
                        mock.patch.object(producer, 'submit_project_observation', side_effect=self.submit)]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def submit(self, **kwargs):
        self.calls.append(kwargs)
        return decision_events.submit_observation(**kwargs, directory=self.data / 'decision-inbox',
                                                  workspace_key='fixture', agent_key='')

    def image(self, name='candidate.png', raw=b'unclassified image evidence'):
        target = self.stage / name
        target.write_bytes(raw)
        return target

    def events(self):
        return [json.loads(path.read_text(encoding='utf-8')) for path in
                (self.data / 'decision-inbox' / 'pending').glob('*.json')]

    def test_single_batch_and_cross_entry_dedup_do_not_classify(self):
        first, second = self.image(), self.image('another.gif')
        before = {path: path.read_bytes() for path in [first, second]}
        result = producer.observe_existing_memes()
        self.assertTrue(result['queued'])
        self.assertFalse(result['delivered'])
        self.assertEqual(result['classified'], 0)
        self.assertEqual(result['observed'], 2)
        self.assertFalse(producer.observe_existing_memes(recheck=True)['queued'])
        self.assertEqual(len(self.calls), 1)
        event, = self.events()
        self.assertEqual(event['kind'], 'meme.review')
        self.assertEqual(event['scope'], 'private:101')
        self.assertFalse(event['control_allowed'])
        self.assertEqual(len(event['payload']['candidates']), 2)
        for candidate in event['payload']['candidates']:
            self.assertEqual(len(candidate['sha256']), 64)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertFalse((self.memes / 'rejected').exists())
        self.assertFalse((self.memes / '.seen.json').exists())

    def test_only_new_file_or_new_content_opens_new_evidence(self):
        path = self.image()
        producer.observe_existing_memes()
        producer.observe_existing_memes()
        self.assertEqual(len(self.calls), 1)
        path.write_bytes(b'new evidence bytes')
        producer.observe_existing_memes()
        self.assertEqual(len(self.events()), 2)
        self.image('new.gif')
        result = producer.observe_existing_memes()
        self.assertEqual(result['observed'], 1)
        self.assertEqual(len(self.events()), 3)

    def test_twenty_candidates_maximum_without_starving_remaining_files(self):
        for index in range(25):
            self.image('candidate-%02d.png' % index)
        self.assertEqual(producer.observe_existing_memes(limit=999)['observed'], 20)
        self.assertEqual(producer.observe_existing_memes(limit=999)['observed'], 5)
        self.assertEqual(producer.observe_existing_memes()['state'], 'idle')
        self.assertEqual([len(call['payload']['candidates']) for call in self.calls], [20, 5])

    def test_failed_publish_retains_exact_pending_batch_when_new_file_arrives(self):
        path = self.image('z-last.png')
        with mock.patch.object(producer, 'submit_project_observation', side_effect=OSError('fixture refused')):
            with self.assertRaises(OSError):
                producer.observe_existing_memes()
        ledger_path = self.memes / '.review-observations.json'
        pending = json.loads(ledger_path.read_text())['pending']
        self.image('a-new.png')
        result = producer.observe_existing_memes()
        self.assertTrue(result['queued'])
        self.assertEqual(self.calls[-1]['event_id'], pending['event_id'])
        self.assertEqual(self.calls[-1]['payload']['candidates'][0]['path'], str(path))
        self.assertEqual(producer.observe_existing_memes()['observed'], 1)

    def test_commit_crash_retries_same_immutable_event_without_second_wakeup(self):
        self.image()
        atomic = producer._atomic_json
        writes = []
        def interrupted(path, value):
            writes.append(value['pending'])
            if len(writes) == 2:
                raise OSError('fixture commit interrupted')
            return atomic(path, value)
        with mock.patch.object(producer, '_atomic_json', side_effect=interrupted):
            with self.assertRaises(OSError):
                producer.observe_existing_memes()
        self.assertEqual(len(self.events()), 1)
        result = producer.observe_existing_memes()
        self.assertFalse(result['queued'])
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.calls[0]['event_id'], self.calls[1]['event_id'])

    def test_unknown_bytes_dry_and_corrupt_ledger_do_not_move_or_reject(self):
        path = self.image(raw=b'not a decoded picture')
        dry = producer.observe_existing_memes(dry=True)
        self.assertEqual(dry['state'], 'dry')
        self.assertFalse(self.events())
        self.assertFalse((self.memes / '.review-observations.json').exists())
        ledger = self.memes / '.review-observations.json'
        ledger.write_text('broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            producer.observe_existing_memes()
        self.assertEqual(ledger.read_text(), 'broken')
        self.assertEqual(path.read_bytes(), b'not a decoded picture')
        self.assertFalse((self.memes / 'rejected').exists())

    def test_configured_group_is_authority_and_unknown_target_refuses(self):
        self.image()
        self.settings['decisions.memeReviewTarget'] = 'group'
        producer.observe_existing_memes()
        self.assertEqual(self.events()[0]['scope'], 'group:202')
        self.settings['decisions.memeReviewTarget'] = 'text-from-image'
        with self.assertRaises(decision_events.ObservationError):
            producer.observe_existing_memes()

    def test_live_lock_prevents_second_producer(self):
        self.image()
        with producer._lock(self.memes / '.review-observations.lock'):
            with self.assertRaises(OSError):
                producer.observe_existing_memes()
        self.assertFalse(self.events())

    def test_activity_toggle_and_malformed_pending_never_publish(self):
        self.image()
        settings = self.data / 'lanya-memes-config.json'
        settings.write_text('{"autoClassify":false}', encoding='utf-8')
        self.assertEqual(producer.observe_existing_memes()['state'], 'paused')
        self.assertFalse((self.memes / '.review-observations.json').exists())
        settings.unlink()
        ledger = self.memes / '.review-observations.json'
        raw = json.dumps({'version': 1, 'submitted': {}, 'pending': {'scope': 'private:101',
                         'event_id': 'meme-review:invalid', 'payload': {'candidates': [{}]}}})
        ledger.write_text(raw, encoding='utf-8')
        with self.assertRaises(decision_events.ObservationError):
            producer.observe_existing_memes()
        self.assertEqual(ledger.read_text(), raw)
        self.assertFalse(self.events())

    def load_entry(self, name):
        # Patch paths before module top-level config discovery; never read this
        # machine's identity, OneBot auth or image collection.
        spec = importlib.util.spec_from_file_location('fixture_' + name, SCRIPTS / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        with mock.patch.object(project_conf, 'data_dir', return_value=str(self.data)), \
                mock.patch.object(project_conf, 'work_dir', return_value=str(self.root)), \
                mock.patch('ctypes.windll', create=True):
            spec.loader.exec_module(module)
        return module

    def test_real_unified_entries_never_call_agent_http_move_or_delete(self):
        self.image()
        for name in ['stealmemes', 'classify_emotion']:
            module = self.load_entry(name)
            with mock.patch.object(module, 'unified_decisions_enabled', return_value=True), \
                    mock.patch.object(module, 'ask_agent', side_effect=AssertionError('headless reached')), \
                    mock.patch.object(module.subprocess, 'run', side_effect=AssertionError('process reached')), \
                    mock.patch('shutil.move', side_effect=AssertionError('move reached')), \
                    mock.patch('sys.argv', [name]):
                if name == 'stealmemes':
                    with mock.patch.object(module, 'api', side_effect=AssertionError('QQ HTTP reached')), \
                            mock.patch.object(module, 'place', side_effect=AssertionError('classification reached')):
                        module.run_scan()
                        module.run_recheck()
                else:
                    with mock.patch.object(module, 'classify_batch', side_effect=AssertionError('classifier reached')), \
                            mock.patch.object(module, 'acquire_lock', side_effect=AssertionError('legacy lock reached')):
                        module.main()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.events()), 1)
        self.assertTrue((self.stage / 'candidate.png').is_file())

    def test_real_agent_functions_require_explicit_legacy(self):
        for name in ['stealmemes', 'classify_emotion']:
            module = self.load_entry(name)
            with mock.patch.object(module, 'unified_decisions_enabled', return_value=True), \
                    mock.patch.object(module.subprocess, 'run', side_effect=AssertionError('model reached')):
                with self.assertRaises(RuntimeError):
                    module.ask_agent('fixture')
            with mock.patch.object(module, 'unified_decisions_enabled', return_value=False), \
                    mock.patch.object(module.subprocess, 'run', return_value=mock.Mock(stdout=b'legacy fixture')) as run:
                self.assertEqual(module.ask_agent('fixture'), 'legacy fixture')
                self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
