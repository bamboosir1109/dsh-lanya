"""Panel Bili routes: temporary configuration/evidence and mocked external I/O."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
PROPOSED = REPO / 'server' / 'panel-server.py'


class PanelBiliMainAgentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='panel-bili-isolated-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.napcat = self.root / 'napcat'
        self.napcat.mkdir()
        self.data = self.root / 'fixture'
        self.data.mkdir()
        self.home = self.root / 'fixture-home'
        self.home.mkdir()
        self.config = self.root / 'project.json'
        self.config.write_text(json.dumps({'dataDirName': 'fixture',
            'decisions': {'mode': 'unified', 'agentPreset': 'Fixture'}}), encoding='utf-8')
        (self.data / 'lanya-config.json').write_text(json.dumps({'adminQQ': ['101'], 'fanGroupQQ': '303'}), encoding='utf-8')
        env = mock.patch.dict(os.environ, {'LANYA_WORKDIR': str(self.root), 'LANYA_PROJECT_CONFIG': str(self.config)})
        env.start(); self.addCleanup(env.stop)
        old_path = sys.path[:]
        sys.path.insert(0, str(REPO / 'scripts'))
        self.addCleanup(sys.path.__setitem__, slice(None), old_path)
        import project_conf
        old_cache, old_config_path = project_conf._CACHE, project_conf._CONFIG_PATH
        project_conf._CACHE = None
        self.addCleanup(setattr, project_conf, '_CACHE', old_cache)
        self.addCleanup(setattr, project_conf, '_CONFIG_PATH', old_config_path)
        for target in ('subprocess.run', 'subprocess.Popen', 'urllib.request.urlopen'):
            guard = mock.patch(target, side_effect=AssertionError('real process/platform/model I/O forbidden'))
            guard.start(); self.addCleanup(guard.stop)
        original_expanduser = os.path.expanduser
        home = mock.patch('os.path.expanduser', side_effect=lambda value:
                          str(self.home) if value == '~' else original_expanduser(value))
        home.start(); self.addCleanup(home.stop)
        spec = importlib.util.spec_from_file_location('panel_bili_main_agent_fixture', PROPOSED)
        self.panel = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.panel)

    def route(self, path, body):
        handler = object.__new__(self.panel.Handler)
        handler.path = path
        raw = json.dumps(body).encode('utf-8')
        handler.headers = {'content-length': str(len(raw))}
        handler.rfile = io.BytesIO(raw)
        handler._json = lambda code, value: (code, value)
        return handler.do_POST()

    def events(self):
        pending = self.data / 'decision-inbox/pending'
        return [json.loads(path.read_text(encoding='utf-8')) for path in pending.glob('*.json')] if pending.exists() else []

    def test_unified_both_routes_share_real_daily_preferences_dedup_and_no_external_io(self):
        with mock.patch('subprocess.Popen', side_effect=AssertionError('unified cannot spawn')) as popen:
            first = self.route('/api/bili/run', {'comment': False, 'mention': True})
            second = self.route('/api/bili/onerun', {'comment': False, 'mention': True})
            third = self.route('/api/bili/run', {'comment': False, 'mention': True})
        self.assertTrue(first[1]['queued'])
        self.assertFalse(second[1]['queued'])
        self.assertFalse(third[1]['queued'])
        self.assertTrue(all(row[0] == 200 and row[1]['mode'] == 'unified' and row[1]['delivered'] is False
                            for row in (first, second, third)))
        self.assertEqual(len(self.events()), 1)
        event = self.events()[0]
        self.assertEqual(event['scope'], 'private:101')
        self.assertEqual(event['workspace_key'], str(self.root).lower() if os.name == 'nt' else str(self.root))
        self.assertEqual(event['agent_key'], 'fixture' if os.name == 'nt' else 'Fixture')
        self.assertEqual(event['kind'], 'entertainment.review')
        self.assertEqual(event['producer'], 'bili-opportunity')
        self.assertEqual(event['origin'], 'observation')
        self.assertIs(event['control_allowed'], False)
        self.assertEqual(event['payload']['preferences'], {'comments_allowed': False, 'mention_requested': True})
        self.assertNotIn('sent', first[1])
        self.assertNotIn('watched', first[1])
        popen.assert_not_called()

    def test_request_owner_mode_and_control_cannot_override_trusted_configuration(self):
        result = self.route('/api/bili/run', {'scope': 'group:999', 'workspace_key': 'untrusted',
            'agent_key': 'untrusted', 'mode': 'legacy', 'origin': 'live', 'control_allowed': True})
        self.assertEqual(result[0], 200)
        event = self.events()[0]
        self.assertEqual(event['scope'], 'private:101')
        self.assertFalse(event['control_allowed'])
        self.assertNotIn('untrusted', json.dumps(event))

    def test_different_preferences_keep_existing_separate_dedup_keys(self):
        self.route('/api/bili/run', {'comment': False})
        self.route('/api/bili/onerun', {'comment': True})
        self.assertEqual(len(self.events()), 2)

    def test_bad_or_missing_explicit_configuration_never_falls_back_to_legacy(self):
        for value in ('{bad-json', '[]', '{"decisions":{"mode":"invalid"}}', '{"decisions":null}'):
            self.config.write_text(value, encoding='utf-8')
            for path in ('/api/bili/run', '/api/bili/onerun'):
                with mock.patch('subprocess.Popen') as popen:
                    code, result = self.route(path, {})
                self.assertEqual(code, 503)
                self.assertFalse(result['ok'])
                self.assertFalse(result['queued'])
                self.assertFalse(result['delivered'])
                self.assertNotIn('bad-json', json.dumps(result))
                popen.assert_not_called()
        self.config.unlink()
        with mock.patch('subprocess.Popen') as popen:
            self.assertEqual(self.route('/api/bili/run', {})[0], 503)
        popen.assert_not_called()
        self.assertEqual(self.events(), [])

    def test_bad_identity_and_nonobject_preferences_refuse_without_process_or_events(self):
        (self.data / 'lanya-config.json').write_text('{"adminQQ":[]}', encoding='utf-8')
        for path in ('/api/bili/run', '/api/bili/onerun'):
            self.assertEqual(self.route(path, {})[0], 503)
            self.assertEqual(self.route(path, [1])[0], 503)
        self.assertEqual(self.events(), [])

    def test_explicit_legacy_preserves_both_existing_mocked_process_branches(self):
        self.config.write_text(json.dumps({'dataDirName': 'fixture', 'decisions': {'mode': 'legacy'}}), encoding='utf-8')
        duty = self.home / '.dsh/bili-duty/duty.md'
        duty.parent.mkdir(parents=True)
        duty.write_text('synthetic legacy duty', encoding='utf-8')
        cli = self.napcat / 'dsh.cmd'
        cli.write_text('synthetic, never execute', encoding='utf-8')
        self.panel.DSH_CLI = str(cli)
        (self.napcat / 'bili_onerun.py').write_text('# synthetic, never execute\n', encoding='utf-8')
        def fake_process(*_args, **_kwargs):
            return SimpleNamespace(pid=42, stdin=io.BytesIO(),
                communicate=lambda timeout: (b'{"ok":true,"legacy_fixture":true}', b''))
        with mock.patch('subprocess.Popen', side_effect=fake_process) as popen:
            run = self.route('/api/bili/run', {})
            onerun = self.route('/api/bili/onerun', {'comment': False, 'mention': True})
        self.assertEqual(run, (200, {'ok': True, 'pid': 42}))
        self.assertEqual(onerun, (200, {'ok': True, 'legacy_fixture': True}))
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(popen.call_args_list[0].args[0], [str(cli), '--profile', 'headless', '-'])
        self.assertEqual(popen.call_args_list[1].args[0][-2:], ['--no-comment', '--mention'])
        self.assertEqual(self.events(), [])


if __name__ == '__main__':
    unittest.main()
