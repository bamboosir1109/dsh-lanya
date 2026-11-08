"""Temporary watch fixtures, real native identity/lock, no DSH/model/QQ start."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

FILE = Path(__file__).resolve().parents[1] / 'scripts/main_agent_watch.py'
spec = importlib.util.spec_from_file_location('watch_test', FILE)
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


def evidence(hosts=0, unknown=False, ports=None, fresh=False):
    return {'host_count': hosts, 'unverifiable_host': unknown,
            'ports': [False, False] if ports is None else ports, 'scheduler_fresh': fresh,
            'ports_confirmed_absent': [value is False for value in ([False, False] if ports is None else ports)]}


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exe = self.root / 'DeepSeek Harness.exe'
        self.exe.write_bytes(b'inert fixture bytes, never executable')
        bindings = [self.root / 'profile.fixture', self.root / 'selection.fixture']
        for item in bindings:
            item.write_bytes(b'inert startup setting')
        self.config = {'schema': watch.SCHEMA, 'enabled': True, 'host_executable': str(self.exe),
                       'host_sha256': hashlib.sha256(self.exe.read_bytes()).hexdigest(), 'arguments': [],
                       'startup_bindings': [{'path': str(item), 'sha256': hashlib.sha256(item.read_bytes()).hexdigest()} for item in bindings],
                       'scheduler_file': str(self.root / 'scheduler-in-plugin.flag'), 'core_ports': [19387, 8643]}
        self.clock = 1000
        self.launched = []

    def engine(self, probe):
        def launch(config):
            self.launched.append(config.copy())
            return SimpleNamespace(pid=12345)
        return watch.Watch(probe=probe, launch=launch, now=lambda: self.clock)

    def test_two_absence_checks_and_fresh_probe_start_only_once_with_backoff(self):
        engine = self.engine(lambda *_: evidence())
        self.assertEqual(engine.tick(self.config)['state'], 'waiting')
        self.assertEqual(engine.tick(self.config)['reason'], 'pinned_host_started_once')
        for _ in range(10):
            self.assertEqual(engine.tick(self.config)['state'], 'waiting')
        self.assertEqual(len(self.launched), 1)
        self.clock += 121
        self.assertEqual(engine.tick(self.config)['state'], 'starting')
        self.assertEqual(len(self.launched), 2)

    def test_existing_stale_host_is_not_killed_or_duplicated(self):
        engine = self.engine(lambda *_: evidence(hosts=1))
        for _ in range(4):
            self.assertEqual(engine.tick(self.config)['state'], 'waiting')
        self.assertEqual(self.launched, [])

    def test_unreadable_host_or_uncertain_or_occupied_port_prevents_start(self):
        for row in (evidence(unknown=True), evidence(ports=[None, False]), evidence(ports=[True, False])):
            engine = self.engine(lambda *_, r=row: r)
            for _ in range(3):
                engine.tick(self.config)
        self.assertEqual(self.launched, [])

    def test_owner_appearing_after_hashing_prevents_start(self):
        rows = iter([evidence(), evidence(), evidence(hosts=1)])
        engine = self.engine(lambda *_: next(rows))
        engine.tick(self.config)
        self.assertEqual(engine.tick(self.config)['reason'], 'owner_changed_before_start')
        self.assertEqual(self.launched, [])

    def test_changed_binary_hash_prevents_start(self):
        self.exe.write_bytes(b'unknown replacement')
        engine = self.engine(lambda *_: evidence())
        engine.tick(self.config)
        self.assertEqual(engine.tick(self.config)['reason'], 'host_binary_changed')
        self.assertEqual(self.launched, [])

    def test_changed_startup_profile_or_selection_prevents_wrong_profile_launch(self):
        Path(self.config['startup_bindings'][1]['path']).write_bytes(b'changed selection')
        engine = self.engine(lambda *_: evidence())
        engine.tick(self.config)
        self.assertEqual(engine.tick(self.config)['reason'], 'startup_binding_changed')
        self.assertEqual(self.launched, [])

    def test_paused_configuration_does_not_probe_or_launch(self):
        self.config['enabled'] = False
        def unexpected(*_):
            self.fail('paused watcher probed the host')
        self.assertEqual(self.engine(unexpected).tick(self.config)['state'], 'paused')
        self.assertEqual(self.launched, [])

    def test_healthy_requires_fresh_scheduler_and_both_ports(self):
        engine = self.engine(lambda *_: evidence(hosts=3, ports=[True, True], fresh=True))
        self.assertEqual(engine.tick(self.config)['state'], 'healthy')
        self.assertEqual(self.launched, [])

    def test_native_snapshot_requires_jobs_array_not_string_or_invalid_members(self):
        marker = Path(self.config['scheduler_file'])
        owner = {'pid': 100, 'birth': str(116_444_736_000_000_000 + 999_000 * 10_000), 'image': str(self.exe)}
        with mock.patch.object(watch, 'host_processes', return_value=([owner], False)), \
                mock.patch.object(watch, 'port_open', return_value=True), \
                mock.patch.object(watch, 'listener_table', return_value=set()):
            for jobs, expected in [('mind', False), (None, False), (['mind', 1], False), (['mind'], True)]:
                marker.write_text(json.dumps({'version': 2, 'pid': 100, 'at': 1000000, 'jobs': jobs}), encoding='utf-8')
                self.assertEqual(watch.snapshot(self.config, now=1000)['scheduler_fresh'], expected)

    def test_failed_launch_enters_backoff_without_burst(self):
        engine = self.engine(lambda *_: evidence())
        def fail(_):
            self.launched.append(True)
            raise OSError('fixture error')
        engine.launch = fail
        engine.tick(self.config)
        self.assertEqual(engine.tick(self.config)['reason'], 'host_start_failed_backoff')
        for _ in range(5):
            engine.tick(self.config)
        self.assertEqual(len(self.launched), 1)

    def test_configuration_rejects_non_default_arguments_and_corrupt_or_relative_input(self):
        path = self.root / 'watch.json'
        path.write_text(json.dumps(self.config), encoding='utf-8')
        self.assertTrue(watch.configuration(path)['enabled'])
        self.config['arguments'] = ['--other-profile']
        path.write_text(json.dumps(self.config), encoding='utf-8')
        with self.assertRaises(ValueError):
            watch.configuration(path)
        path.write_text('{bad', encoding='utf-8')
        with self.assertRaises(ValueError):
            watch.configuration(path)
        with self.assertRaises(ValueError):
            watch.ordinary(Path('relative.json'))

    def test_atomic_status_preserves_complete_metadata_and_cleans_candidates(self):
        path = self.root / 'status.json'
        watch.atomic_status(path, {'state': 'healthy', 'at': 1})
        watch.atomic_status(path, {'state': 'paused', 'at': 2})
        self.assertEqual(watch.document(path), {'state': 'paused', 'at': 2})
        self.assertEqual(list(self.root.glob('.*.tmp')), [])

    def test_public_projection_verifies_fresh_native_owner_and_never_exposes_paths_or_pid(self):
        config = self.root / 'main-agent-watch.config.json'
        config.write_text(json.dumps(self.config), encoding='utf-8')
        data = {'schema': watch.STATUS, 'state': 'healthy', 'reason': 'scheduler_and_core_verified',
                'pid': 12345, 'birth': '123456', 'at': 1000000,
                'source_sha256': hashlib.sha256(FILE.read_bytes()).hexdigest()}
        status = self.root / 'main-agent-watch.status.json'
        status.write_text(json.dumps(data), encoding='utf-8')
        probe = lambda _: {'birth': '123456', 'image': sys.executable}
        result = watch.public_status(self.root, now=1001, process_probe=probe)
        self.assertEqual(result['status'], 'healthy')
        self.assertNotIn('pid', result)
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertEqual(watch.public_status(self.root, now=1061, process_probe=probe)['status'], 'offline')
        changed_owner = lambda _: {'birth': str(123_457), 'image': sys.executable}
        self.assertEqual(watch.public_status(self.root, now=1001, process_probe=changed_owner)['reason'], 'watch_owner_unverified')
        self.assertEqual(watch.public_status(self.root, now=1001, process_probe=lambda _: None)['status'], 'unknown')

    def test_public_projection_missing_corrupt_paused_and_unreadable_owner_are_distinct(self):
        self.assertEqual(watch.public_status(self.root)['status'], 'not_configured')
        config = self.root / 'main-agent-watch.config.json'
        config.write_text('{invalid', encoding='utf-8')
        self.assertEqual(watch.public_status(self.root)['status'], 'unknown')
        self.config['enabled'] = False
        config.write_text(json.dumps(self.config), encoding='utf-8')
        self.assertEqual(watch.public_status(self.root)['status'], 'paused')

    def test_real_native_lock_rejects_second_process_then_releases(self):
        path = self.root / 'lock'
        code = ('import importlib.util,sys\n'
                's=importlib.util.spec_from_file_location("w",sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n'
                'try:\n with m.ownership(m.Path(sys.argv[2])): pass\nexcept OSError: sys.exit(12)\n')
        with watch.ownership(path):
            blocked = subprocess.run([sys.executable, '-B', '-c', code, str(FILE), str(path)], capture_output=True, timeout=15)
            self.assertEqual(blocked.returncode, 12, blocked.stderr)
        released = subprocess.run([sys.executable, '-B', '-c', code, str(FILE), str(path)], capture_output=True, timeout=15)
        self.assertEqual(released.returncode, 0, released.stderr)

    @unittest.skipUnless(os.name == 'nt', 'native Windows identity only')
    def test_real_current_process_native_birth_and_image_are_exact(self):
        first = watch.native_process(os.getpid())
        second = watch.native_process(os.getpid())
        self.assertEqual(first, second)
        self.assertEqual(first['pid'], os.getpid())
        # Windows venv's python.exe is a redirector; the running native image
        # is its base interpreter. Still require one exact expected image.
        executable = getattr(sys, '_base_executable', None) or sys.executable
        self.assertEqual(os.path.normcase(first['image']), os.path.normcase(executable))
        self.assertGreater(int(first['birth']), 0)


if __name__ == '__main__':
    unittest.main()
