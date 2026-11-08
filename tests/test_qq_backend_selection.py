"""CAS/backup/ACL selection tests; no host process, model or QQ is contacted."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import os
import hashlib
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import qq_backend_selection as selection


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='backend selection ')
        self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.config = self.root / 'napcat/config/project.json'
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({'dataDirName': 'fixture', 'onebotPort': 8721,
            'budget': {'privateMarker': 'fixture-private-preserved'}, 'decisions': {'mode': 'unified'},
            'qqBackend': {'provider': 'napcat', 'providers': {}}}), encoding='utf-8')
        for file, value in [('napcat/framework/config/onebot11.json', {'network': {'httpServers': [{'port': 8721, 'token': 'fixture-nap'}]}}),
                ('snowluma/config/onebot.json', {'networks': {'httpServers': [{'name': 'http-default', 'port': 8730, 'accessToken': 'fixture-snow'}]}})]:
            path = self.root / file; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(value), encoding='utf-8')
        patch = mock.patch.dict(os.environ, {'LANYA_WORKDIR': str(self.root), 'LANYA_PROJECT_CONFIG': str(self.config)})
        patch.start(); self.addCleanup(patch.stop)
        self.ready = lambda: {'ready': True, 'state': 'fixture'}

    def revision(self):
        return selection.snapshot(guard=self.ready)['revision']

    def test_runtime_guard_requires_loaded_transport_quote_fresh_gate_and_actual_alias_hash(self):
        file = self.root / '.dsh/profiles/desktop/node_modules/dsh-onebot-qq/lib/lanya-policy-adapter.js'
        raw = b'// LANYA_BACKEND_BUNDLE_V1 fixture\nthis.backendGuard.edge();this.backendGuard.verify();'
        file.parent.mkdir(parents=True); file.write_bytes(raw)
        ready = dict(runtime_gate_bound=True, gate_pid=123, backend_transport_bound=True,
                     backend_quote_bound=True, adapter_source_sha256=hashlib.sha256(raw).hexdigest())
        view = dict(ready)
        runtime = SimpleNamespace(get_snapshot=lambda: {'enforcement': view})
        with mock.patch.object(Path, 'home', return_value=self.root), mock.patch.dict(sys.modules, budget_runtime=runtime):
            self.assertTrue(selection.runtime_guard()['ready'])
            for change in ({'runtime_gate_bound': False}, {'backend_transport_bound': False},
                           {'backend_quote_bound': False}, {'gate_pid': None}, {'adapter_source_sha256': '0' * 64}):
                view.clear(); view.update(ready); view.update(change)
                self.assertFalse(selection.runtime_guard()['ready'])
            view.clear(); view.update(ready); file.write_bytes(raw + b'changed')
            self.assertFalse(selection.runtime_guard()['ready'])

    def test_selection_preserves_unrelated_configuration_and_exact_original_backup_security(self):
        store = selection._store(); before, raw = store._record(self.config)
        result = selection.select({'provider': 'snowluma', 'revision': self.revision()}, guard=self.ready)
        self.assertEqual(result['selected'], 'snowluma'); self.assertFalse(result['started'])
        self.assertEqual(result['connection_state'], 'awaiting_verification')
        actual = json.loads(self.config.read_bytes()); self.assertEqual(actual['budget']['privateMarker'], 'fixture-private-preserved')
        self.assertEqual(actual['qqBackend']['revision'], 1)
        backups = list(self.config.parent.glob('*.original.json'))
        self.assertEqual(len(backups), 1); self.assertEqual(backups[0].read_bytes(), raw)
        self.assertEqual(store._record(backups[0])[0]['security_sha256'], before['security_sha256'])
        self.assertEqual(store._record(self.config)[0]['security_sha256'], before['security_sha256'])
        self.assertEqual(result['messages_sent'], 0)

    def test_stale_revision_rejects_before_backup_or_mutation(self):
        revision = self.revision(); original = self.config.read_bytes()
        self.config.write_bytes(original + b'\n')
        with self.assertRaises(selection.SelectionError) as error:
            selection.select({'provider': 'snowluma', 'revision': revision}, guard=self.ready)
        self.assertEqual(error.exception.code, 'revision_conflict')
        self.assertEqual(list(self.config.parent.glob('*.original.json')), [])

    def test_missing_target_and_unloaded_guard_cannot_change_selection(self):
        raw = self.config.read_bytes()
        with self.assertRaises(selection.SelectionError):
            selection.select({'provider': 'snowluma', 'revision': self.revision()}, guard=lambda: {'ready': False})
        self.assertEqual(self.config.read_bytes(), raw)
        (self.root / 'snowluma/config/onebot.json').unlink()
        with self.assertRaises(selection.backend.BackendError):
            selection.select({'provider': 'snowluma', 'revision': self.revision()}, guard=self.ready)
        self.assertEqual(self.config.read_bytes(), raw)

    def test_switching_back_increments_generation_instead_of_reusing_old_authority(self):
        for number, provider in enumerate(['snowluma', 'napcat', 'snowluma'], 1):
            selection.select({'provider': provider, 'revision': self.revision()}, guard=self.ready)
            self.assertEqual(json.loads(self.config.read_bytes())['qqBackend']['revision'], number)
        self.assertEqual(len(list(self.config.parent.glob('*.original.json'))), 3)

    def test_unknown_body_fields_and_unhashable_provider_are_rejected(self):
        for body in [{}, {'provider': [], 'revision': 'x' * 64},
                {'provider': 'snowluma', 'revision': 'x' * 64, 'token': 'fixture'}, []]:
            with self.assertRaises(selection.SelectionError): selection.select(body, guard=self.ready)

    def test_target_drift_during_guard_recheck_retains_original_configuration(self):
        raw = self.config.read_bytes(); calls = 0
        def guard():
            nonlocal calls
            calls += 1
            if calls == 2:
                file = self.root / 'snowluma/config/onebot.json'
                value = json.loads(file.read_bytes()); value['networks']['httpServers'][0]['port'] = 8731
                file.write_text(json.dumps(value), encoding='utf-8')
            return {'ready': True}
        with self.assertRaises(selection.SelectionError):
            selection.select({'provider': 'snowluma', 'revision': self.revision()}, guard=guard)
        self.assertEqual(self.config.read_bytes(), raw)

    def test_first_setup_can_select_an_installed_runtime_without_fabricating_connection_config(self):
        (self.root / 'snowluma/config/onebot.json').unlink()
        for name in selection.backend.BACKENDS['snowluma'].entry_points:
            (self.root / 'snowluma' / name).write_bytes(b'inert fixture; never execute')
        result = selection.select({'provider': 'snowluma', 'revision': self.revision()}, guard=self.ready)
        row = next(item for item in result['providers'] if item['selected'])
        self.assertTrue(row['installed']); self.assertFalse(row['configured'])
        self.assertFalse(result['started']); self.assertFalse((self.root / 'snowluma/config/onebot.json').exists())

    def test_legacy_workers_cannot_be_hot_switched_by_a_loaded_native_guard(self):
        original = json.loads(self.config.read_bytes()); original['decisions']['mode'] = 'legacy'
        self.config.write_text(json.dumps(original), encoding='utf-8'); before = self.config.read_bytes()
        with self.assertRaises(selection.SelectionError) as error:
            selection.select({'provider': 'snowluma', 'revision': self.revision()}, guard=self.ready)
        self.assertEqual(error.exception.code, 'offline_configuration_required')
        self.assertEqual(self.config.read_bytes(), before)


if __name__ == '__main__': unittest.main()
