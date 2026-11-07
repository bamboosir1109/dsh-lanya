"""Backend lifecycle with inert launch/native/port seams, never a real injector."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import qq_backend_control as control


class BackendControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='backend lifecycle ')
        self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.write('napcat/config/project.json', {'dataDirName': 'fixture', 'qqBackend': {'provider': 'snowluma'}})
        self.write('snowluma/config/runtime.json', {'webuiPort': 5097})
        self.entry = self.root / 'snowluma/index.mjs'; self.entry.write_text('// inert fixture', encoding='utf-8')
        (self.root / 'snowluma/check-node-version.cjs').write_text('// inert checker', encoding='utf-8')
        self.exe = self.root / 'snowluma' / ('node.exe' if os.name == 'nt' else 'node')
        self.exe.write_bytes(b'INERT; NEVER EXECUTE')
        environment = mock.patch.dict(os.environ, {'LANYA_WORKDIR': str(self.root),
            'LANYA_PROJECT_CONFIG': str(self.root / 'napcat/config/project.json'),
            'SNOWLUMA_ACCEPT_EULA': '1', 'SNOWLUMA_ACCEPT_PRIVACY': '1', 'SNOWLUMA_DEV_MODE': '1'})
        environment.start(); self.addCleanup(environment.stop)
        self.launch = mock.Mock(return_value=SimpleNamespace(pid=303))
        self.identity = {'pid': 303, 'birth': 'fixture-birth', 'exe': str(self.exe)}
        self.native = mock.Mock(return_value=self.identity)
        self.preflight = mock.Mock(return_value=SimpleNamespace(returncode=0))
        self.qq_state = mock.Mock(return_value=False)

    def write(self, name, value):
        path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8'); return path

    def start(self, **changes):
        return control.start(**{'listening': lambda url: False, 'launch': self.launch, 'native': self.native,
            'preflight': self.preflight, 'qq_client_state': self.qq_state, **changes})

    def test_existing_runtime_launches_once_hidden_without_consent_or_dev_bypasses(self):
        first = self.start(); self.assertTrue(first['started']); self.assertFalse(first['connection_verified'])
        second = self.start(); self.assertFalse(second['started']); self.assertEqual(second['status'], 'already_running')
        self.launch.assert_called_once(); args, values = self.launch.call_args
        self.assertEqual(args[0], [str(self.exe), str(self.entry)])
        self.assertEqual(values['cwd'], str(self.entry.parent))
        for key in ['SNOWLUMA_ACCEPT_EULA', 'SNOWLUMA_ACCEPT_PRIVACY', 'SNOWLUMA_DEV_MODE']:
            self.assertNotIn(key, values['env'])
        self.assertTrue((self.root / 'napcat/backend-runtime/snowluma-process.json').is_file())

    def test_busy_or_unknown_other_backend_blocks_launch_without_killing_qq(self):
        self.write('napcat/framework/config/onebot11.json', {'network': {'httpServers': [{'port': 8720, 'token': 'fixture'}]}})
        for state in [True, None]:
            with self.assertRaises(control.backend.BackendError): self.start(listening=lambda url: state)
        self.launch.assert_not_called()

    def test_busy_webui_without_owned_identity_does_not_launch_a_second_instance(self):
        with self.assertRaises(control.backend.BackendError): self.start(listening=lambda url: True)
        self.launch.assert_not_called()

    def test_unconfirmed_started_process_has_an_intent_and_cannot_be_blindly_retried(self):
        self.native.return_value = None
        with self.assertRaises(control.backend.BackendError): self.start()
        with self.assertRaises(control.backend.BackendError): self.start()
        self.launch.assert_called_once()
        self.assertEqual(len(list((self.root / 'napcat/backend-runtime').glob('*.intent.json'))), 1)

    def test_unreadable_existing_identity_is_not_treated_as_dead(self):
        self.start(); self.native.return_value = None
        with self.assertRaises(control.backend.BackendError): self.start()
        self.launch.assert_called_once()

    def test_napcat_uses_only_the_explicit_launcher_callback(self):
        self.write('napcat/config/project.json', {'dataDirName': 'fixture', 'qqBackend': {'provider': 'napcat'}})
        self.write('napcat/framework/config/onebot11.json', {'network': {'httpServers': [{'port': 8720}]}})
        launcher = mock.Mock(return_value=404)
        self.native.return_value = {'pid': 404, 'birth': 'fixture-birth', 'exe': str(self.root / 'inert-launcher.exe')}
        result = self.start(napcat_launcher=launcher)
        launcher.assert_called_once(); self.assertEqual(result['provider'], 'napcat')
        self.launch.assert_not_called()

    def test_vendor_preflight_failure_does_not_launch_or_leave_unknown_intent(self):
        self.preflight.return_value.returncode = 1
        with self.assertRaises(control.backend.BackendError): self.start()
        self.launch.assert_not_called()
        self.assertEqual(list((self.root / 'napcat/backend-runtime').glob('*.intent.json')), [])

    def test_selection_change_during_vendor_preflight_cannot_launch_the_previous_backend(self):
        def change(*args, **kwargs):
            self.write('napcat/config/project.json', {'qqBackend': {'provider': 'napcat'}})
            return SimpleNamespace(returncode=0)
        self.preflight.side_effect = change
        with self.assertRaises(control.backend.BackendError): self.start()
        self.launch.assert_not_called()

    def test_unknown_napcat_port_does_not_call_interactive_launcher(self):
        self.write('napcat/config/project.json', {'dataDirName': 'fixture', 'qqBackend': {'provider': 'napcat'}})
        self.write('napcat/framework/config/onebot11.json', {'network': {'httpServers': [{'port': 8720}]}})
        launcher = mock.Mock(return_value=404)
        with self.assertRaises(control.backend.BackendError): self.start(napcat_launcher=launcher, listening=lambda url: None)
        launcher.assert_not_called()

    def test_recovery_never_uses_an_interactive_napcat_launcher_or_kills_qq(self):
        starter = mock.Mock(side_effect=AssertionError('must not start'))
        result = control.recover(inspect=lambda: {'provider': 'napcat', 'verified': False}, starter=starter)
        self.assertFalse(result['started']); self.assertTrue(result['action_required']); starter.assert_not_called()

    def test_a_live_snowluma_runtime_requires_login_check_instead_of_a_second_launch(self):
        starter = mock.Mock(return_value={'ok': True, 'provider': 'snowluma', 'status': 'already_running', 'started': False})
        result = control.recover(inspect=lambda: {'provider': 'snowluma', 'verified': False}, starter=starter)
        self.assertFalse(result['started']); self.assertFalse(result['connection_verified']); self.assertTrue(result['action_required'])

    def napcat(self):
        self.write('napcat/config/project.json', {'dataDirName': 'fixture', 'qqBackend': {'provider': 'napcat'}})
        self.write('napcat/framework/config/onebot11.json', {'network': {'httpServers': [{'port': 8720}]}})

    def test_live_or_unverified_qq_cannot_be_closed_by_the_napcat_launcher(self):
        self.napcat(); launcher = mock.Mock(return_value=404)
        for value in (True, None):
            with self.subTest(state=value), self.assertRaises(control.backend.BackendError):
                self.start(napcat_launcher=launcher, qq_client_state=lambda: value)
        launcher.assert_not_called()
        self.assertEqual(list((self.root / 'napcat/backend-runtime').glob('*.intent.json')), [])

    def test_napcat_launch_is_recorded_once_even_while_its_http_port_is_still_starting(self):
        self.napcat(); launcher = mock.Mock(return_value=404)
        self.native.return_value = {'pid': 404, 'birth': 'fixture-birth', 'exe': str(self.root / 'inert-launcher.exe')}
        self.assertTrue(self.start(napcat_launcher=launcher)['started'])
        self.qq_state.return_value = True
        self.assertFalse(self.start(napcat_launcher=launcher)['started'])
        launcher.assert_called_once(); self.qq_state.assert_called_once()

    def test_unconfirmed_napcat_launch_cannot_be_repeated_after_a_client_timeout(self):
        self.napcat(); launcher = mock.Mock(return_value=404); self.native.return_value = None
        for _ in range(2):
            with self.assertRaises(control.backend.BackendError): self.start(napcat_launcher=launcher)
        launcher.assert_called_once()

    def test_other_backend_webui_blocks_start_even_when_its_http_port_is_closed(self):
        self.write('napcat/framework/config/onebot11.json', {'network': {'httpServers': [{'port': 8720}]}})
        with self.assertRaises(control.backend.BackendError): self.start(listening=lambda url: url.endswith(':6099'))
        self.launch.assert_not_called()

    def test_corrupt_alternate_backend_configuration_is_not_evidence_of_an_idle_backend(self):
        path = self.write('napcat/framework/config/onebot11.json', {})
        path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(control.backend.BackendError): self.start()
        self.launch.assert_not_called()

    def test_another_owned_backend_process_blocks_start_before_its_ports_are_ready(self):
        identity = {'pid': 404, 'birth': 'fixture-birth', 'exe': str(self.root / 'inert-launcher.exe')}
        self.write('napcat/backend-runtime/napcat-process.json', {'provider': 'napcat', 'process': identity})
        self.native.return_value = identity
        with self.assertRaises(control.backend.BackendError): self.start()
        self.launch.assert_not_called()

    def test_completed_launch_history_is_preserved_without_a_128_restart_lifetime_limit(self):
        import panel_autonomy as store
        folder = self.root / 'napcat/backend-runtime'
        journal = control.LaunchJournal(folder, store)
        first = None
        for number in range(150):
            operation = format(number, '032x')
            intent = folder / ('snowluma-' + operation + '.intent.json')
            result = folder / ('snowluma-' + operation + '.result.json')
            journal.write_new(intent, {'provider': 'snowluma', 'operation': operation, 'automatic_retry': False})
            journal.write_new(result, {'operation': operation, 'started': True, 'process': self.identity})
            if first is None: first = (intent.name, store._record(intent)[0])
        journal.settled('snowluma')
        self.assertEqual(len(list(folder.glob('*.intent.json'))), 32)
        self.assertEqual(len(list((folder / 'archive').glob('*.intent.json'))), 118)
        self.assertEqual(store._record(folder / 'archive' / first[0])[0], first[1])

    def test_a_fake_or_missing_result_is_not_a_completed_start(self):
        folder = 'napcat/backend-runtime/snowluma-' + 'a' * 32
        self.write(folder + '.intent.json', {'provider': 'snowluma', 'operation': 'a' * 32, 'automatic_retry': False})
        self.write(folder + '.result.json', {})
        with self.assertRaises(control.backend.BackendError): self.start()
        self.launch.assert_not_called()

    def test_exact_native_receipt_repairs_only_the_crash_gap_before_result_publication(self):
        self.start()
        folder = self.root / 'napcat/backend-runtime'
        file = next(folder.glob('*.result.json')); file.unlink()
        self.assertFalse(self.start()['started'])
        self.assertTrue(json.loads(file.read_bytes())['recovered_from_receipt'])
        self.launch.assert_called_once()

    def test_stale_start_request_and_conflicting_vendor_port_do_not_launch(self):
        with self.assertRaises(control.backend.BackendError): self.start(expected_provider='napcat')
        self.write('napcat/config/project.json', {'dataDirName': 'fixture', 'qqBackend': {
            'provider': 'snowluma', 'providers': {'snowluma': {'webuiPort': 5099}}}})
        with self.assertRaises(control.backend.BackendError): self.start()
        self.launch.assert_not_called()

    def test_runtime_settings_changed_during_node_preflight_cannot_launch_stale_configuration(self):
        def change(*args, **kwargs):
            self.write('snowluma/config/runtime.json', {'webuiPort': 5098})
            return SimpleNamespace(returncode=0)
        self.preflight.side_effect = change
        with self.assertRaises(control.backend.BackendError): self.start()
        self.launch.assert_not_called()


if __name__ == '__main__': unittest.main()
