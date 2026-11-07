"""Isolated buffers, fake Windows API and fake launcher; no native table calls."""
import ctypes
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

FILE = Path(__file__).resolve().parents[1] / 'scripts/main_agent_watch.py'
spec = importlib.util.spec_from_file_location('candidate_watch', FILE)
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


def table(family, ports, address_byte=0, state=2):
    layout, row_type = (watch.TcpTable, watch.TcpRow) if family == 2 else (watch.Tcp6Table, watch.Tcp6Row)
    raw = bytearray(layout.table.offset + len(ports) * ctypes.sizeof(row_type))
    raw[:4] = bytes(ctypes.c_uint32(len(ports)))
    for index, port in enumerate(ports):
        row = row_type()
        row.state, row.local_port, row.pid = state, socket.htons(port), 100
        if family == 2:
            row.local_address = address_byte
        else:
            row.local_address[0] = address_byte
        start = layout.table.offset + index * ctypes.sizeof(row_type)
        raw[start:start + ctypes.sizeof(row_type)] = bytes(row)
    return bytes(raw)


class FakeApi:
    def __init__(self, raw, growth=0, error=None, query_error=122):
        self.raw, self.growth, self.error, self.query_error = raw, growth, error, query_error
        self.calls = []

    def __call__(self, buffer, size_pointer, ordered, family, table_class, reserved):
        size = size_pointer._obj
        self.calls.append((buffer is None, ordered, family, table_class, reserved))
        if buffer is None:
            size.value = len(self.raw)
            return self.query_error
        if self.growth:
            self.growth -= 1
            size.value += 4
            return 122
        if self.error is not None:
            return self.error
        size.value = len(self.raw)
        ctypes.memmove(buffer, self.raw, len(self.raw))
        return 0


def evidence(hosts=0, unknown=False, ports=None, absent=None, fresh=False):
    return {'host_count': hosts, 'unverifiable_host': unknown,
            'ports': [None, None] if ports is None else ports,
            'ports_confirmed_absent': [True, True] if absent is None else absent,
            'scheduler_fresh': fresh}


class ListenerBuffers(unittest.TestCase):
    def test_ipv4_wildcard_and_specific_addresses_network_order_and_duplicates(self):
        for address in (0, 0x0100007f):
            self.assertEqual(watch.decode_listener_table(table(2, [19387, 8643, 19387], address), 2), {19387, 8643})
        self.assertEqual(ctypes.sizeof(watch.TcpRow), 24)

    def test_ipv6_address_scope_padding_and_network_order(self):
        for address in (0, 1, 255):
            self.assertEqual(watch.decode_listener_table(table(23, [8643, 65535], address), 23), {8643, 65535})
        self.assertEqual(ctypes.sizeof(watch.Tcp6Row), 56)
        self.assertEqual(watch.TcpTable.table.offset, 4)
        self.assertEqual(watch.Tcp6Table.table.offset, 4)

    def test_empty_tables_are_complete_not_failed_results(self):
        for family in (2, 23):
            self.assertEqual(watch.decode_listener_table(table(family, []), family), set())

    def test_incomplete_header_count_truncation_wrong_state_and_port_zero_rejected(self):
        malformed = [b'', b'\0' * 3, bytes(ctypes.c_uint32(0xffffffff)),
                     table(2, [19387])[:-1], table(2, [19387], state=5), table(2, [0])]
        for raw in malformed:
            with self.subTest(raw_length=len(raw)), self.assertRaises(ValueError):
                watch.decode_listener_table(raw, 2)
        with self.assertRaises(ValueError):
            watch.decode_listener_table(table(23, [8643])[:-1], 23)
        with self.assertRaises(ValueError):
            watch.decode_listener_table(table(2, []), 10)

    def test_decode_uses_structure_offset_and_stride_including_synthetic_padding(self):
        class Padded(ctypes.Structure):
            _fields_ = [('count', ctypes.c_uint32), ('padding', ctypes.c_uint32), ('table', watch.TcpRow * 1)]
        row = watch.TcpRow(); row.state = 2; row.local_port = socket.htons(19387)
        raw = bytes(ctypes.c_uint32(1)) + b'PAD!' + bytes(row)
        with mock.patch.object(watch, 'TcpTable', Padded):
            self.assertEqual(watch.decode_listener_table(raw, 2), {19387})

    def test_size_query_complete_fetch_class_and_reserved_are_exact(self):
        api = FakeApi(table(2, [19387]))
        self.assertEqual(watch.listener_table(2, api), {19387})
        self.assertEqual(api.calls, [(True, False, 2, 3, 0), (False, False, 2, 3, 0)])
        api = FakeApi(table(23, []), query_error=0)
        self.assertEqual(watch.listener_table(23, api), set())

    def test_native_api_factory_uses_exact_win32_types_without_calling_it(self):
        api = FakeApi(table(2, []))
        with mock.patch.object(watch.ctypes, 'WinDLL', return_value=SimpleNamespace(GetExtendedTcpTable=api)) as library:
            self.assertIs(watch._tcp_table_api(), api)
        library.assert_called_once_with('iphlpapi', use_last_error=True)
        self.assertEqual(api.argtypes, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int,
                                       ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32])
        self.assertIs(api.restype, ctypes.c_uint32)
        self.assertEqual(api.calls, [])

    def test_bounded_buffer_growth_and_exhaustion(self):
        api = FakeApi(table(23, [8643]), growth=2)
        self.assertEqual(watch.listener_table(23, api), {8643})
        self.assertEqual(len(api.calls), 4)
        api = FakeApi(table(23, [8643]), growth=3)
        self.assertIsNone(watch.listener_table(23, api))
        self.assertEqual(len(api.calls), 4)

    def test_fetch_error_unsupported_family_and_missing_api_stay_unknown(self):
        for api in (FakeApi(table(2, []), error=50), FakeApi(table(2, []), query_error=5)):
            self.assertIsNone(watch.listener_table(2, api))
        api = FakeApi(table(2, []))
        self.assertIsNone(watch.listener_table(10, api))
        self.assertEqual(api.calls, [])
        for error in (OSError('unavailable'), AttributeError('unsupported')):
            with mock.patch.object(watch, '_tcp_table_api', side_effect=error):
                self.assertIsNone(watch.listener_table(2))

    def test_invalid_size_short_growth_and_claimed_complete_overrun_stay_unknown(self):
        for declared in (0, 3, watch.TCP_TABLE_MAX_BYTES + 1):
            def oversized(buffer, pointer, *_):
                pointer._obj.value = declared
                return 122
            self.assertIsNone(watch.listener_table(2, oversized))
        def no_growth(buffer, pointer, *_):
            pointer._obj.value = 4
            return 122
        self.assertIsNone(watch.listener_table(2, no_growth))
        def overrun(buffer, pointer, *_):
            pointer._obj.value = 4 if buffer is None else 5
            return 122 if buffer is None else 0
        self.assertIsNone(watch.listener_table(2, overrun))


class PortEvidence(unittest.TestCase):
    def test_full_three_state_truth_table(self):
        for connect in (True, False, None):
            for before in (True, False, None):
                for after in (True, False, None):
                    expected = (False if any(value is True for value in (connect, before, after))
                                else True if before is False and after is False else None)
                    self.assertIs(watch.confirmed_port_absence(connect, before, after), expected)

    def test_either_family_listener_blocks_and_unknown_is_not_empty(self):
        cases = [([{19387}, set()], [True, False]),
                 ([set(), {8643}], [False, True]),
                 ([None, set()], [None, None]),
                 ([None, {8643}], [None, True])]
        for tables, expected in cases:
            with mock.patch.object(watch, 'listener_table', side_effect=tables) as probe:
                result = watch.listener_observation([19387, 8643])
            self.assertEqual(result['ports_present'], expected)
            self.assertEqual([call.args[0] for call in probe.call_args_list], [2, 23])
            self.assertEqual(set(result), {'ipv4_complete', 'ipv6_complete', 'ports_present'})

    def test_timeout_remains_unknown_but_complete_absence_is_separate(self):
        with mock.patch.object(watch.socket, 'create_connection', side_effect=socket.timeout('fixture')):
            self.assertIsNone(watch.port_open(19387))
        with mock.patch.object(watch.socket, 'create_connection', side_effect=ConnectionRefusedError()):
            self.assertIs(watch.port_open(19387), False)
        self.assertIs(watch.confirmed_port_absence(None, False, False), True)
        self.assertIsNone(watch.confirmed_port_absence(None, None, False))

    def test_snapshot_orders_both_native_families_before_and_after_connects(self):
        events = []
        def native(family):
            events.append(('table', family))
            return set()
        def connect(port):
            events.append(('connect', port))
            return None
        with mock.patch.object(watch, 'host_processes', return_value=([], False)), \
                mock.patch.object(watch, 'listener_table', side_effect=native), \
                mock.patch.object(watch, 'port_open', side_effect=connect), \
                mock.patch.object(watch, 'document', side_effect=OSError('no marker')):
            result = watch.snapshot({'host_executable': 'unused', 'scheduler_file': 'unused',
                                     'core_ports': [19387, 8643]}, now=1000)
        self.assertEqual(events, [('table', 2), ('table', 23), ('connect', 19387),
                                  ('connect', 8643), ('table', 2), ('table', 23)])
        self.assertEqual(result['ports'], [None, None])
        self.assertEqual(result['ports_confirmed_absent'], [True, True])
        self.assertTrue(result['listener_evidence']['after']['ipv6_complete'])

    def test_snapshot_rejects_late_listener_or_late_incomplete_table(self):
        for tables, expected in [([set(), set(), set(), {8643}], [True, False]),
                                 ([set(), set(), set(), None], [None, None])]:
            with mock.patch.object(watch, 'host_processes', return_value=([], False)), \
                    mock.patch.object(watch, 'listener_table', side_effect=tables), \
                    mock.patch.object(watch, 'port_open', return_value=None), \
                    mock.patch.object(watch, 'document', side_effect=OSError('no marker')):
                result = watch.snapshot({'host_executable': 'unused', 'scheduler_file': 'unused',
                                         'core_ports': [19387, 8643]}, now=1000)
            self.assertEqual(result['ports_confirmed_absent'], expected)

    def test_snapshot_scheduler_fresh_owner_and_jobs_array_requirements_remain(self):
        owner = {'pid': 100, 'birth': str(116_444_736_000_000_000 + 999_000 * 10_000), 'image': 'unused'}
        config = {'host_executable': 'unused', 'scheduler_file': 'unused', 'core_ports': [19387, 8643]}
        for jobs, expected in [('mind', False), (None, False), (['mind', 1], False), (['mind'], True)]:
            with mock.patch.object(watch, 'host_processes', return_value=([owner], False)), \
                    mock.patch.object(watch, 'listener_table', return_value=set()), \
                    mock.patch.object(watch, 'port_open', return_value=True), \
                    mock.patch.object(watch, 'document', return_value={'version': 2, 'pid': 100, 'at': 1000000, 'jobs': jobs}):
                self.assertIs(watch.snapshot(config, now=1000)['scheduler_fresh'], expected)


class RestartGuards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.exe = self.root / 'DeepSeek Harness.exe'
        self.exe.write_bytes(b'inert fixture, never executable')
        bindings = [self.root / name for name in ('profile.fixture', 'selection.fixture')]
        for item in bindings:
            item.write_bytes(b'inert startup')
        self.config = {'enabled': True, 'host_executable': str(self.exe),
                       'host_sha256': hashlib.sha256(self.exe.read_bytes()).hexdigest(),
                       'startup_bindings': [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                                            for path in bindings]}
        self.clock, self.calls = 1000, []

    def engine(self, probe):
        def fake_launch(config):
            self.calls.append(config)
            return SimpleNamespace(pid=12345)
        return watch.Watch(probe=probe, launch=fake_launch, now=lambda: self.clock)

    def test_timeout_with_double_native_absence_uses_two_ticks_fresh_probe_and_backoff(self):
        observed = []
        def probe(*_):
            observed.append(True)
            return evidence()
        engine = self.engine(probe)
        self.assertEqual(engine.tick(self.config)['state'], 'waiting')
        self.assertEqual(engine.tick(self.config)['state'], 'starting')
        self.assertEqual(len(observed), 3)
        for _ in range(3):
            self.assertEqual(engine.tick(self.config)['state'], 'waiting')
        self.assertEqual(len(self.calls), 1)
        self.clock += 121
        self.assertEqual(engine.tick(self.config)['state'], 'starting')
        self.assertEqual(len(self.calls), 2)

    def test_unknown_present_malformed_missing_and_conflicting_evidence_never_launch(self):
        missing = evidence(); missing.pop('ports_confirmed_absent')
        cases = [evidence(absent=[None, True]), evidence(absent=[False, True]),
                 evidence(ports=[True, None]), evidence(absent=[]),
                 evidence(absent=[1, True]), evidence(ports=[0, None]), missing,
                 evidence(hosts=1), evidence(unknown=True)]
        for row in cases:
            engine = self.engine(lambda *_, row=row: row)
            for _ in range(3):
                engine.tick(self.config)
        self.assertEqual(self.calls, [])

    def test_final_probe_listener_unknown_or_host_reappearing_prevents_start(self):
        for final in (evidence(absent=[True, False]), evidence(absent=[True, None]), evidence(hosts=1)):
            rows = iter([evidence(), evidence(), final])
            engine = self.engine(lambda *_: next(rows))
            engine.tick(self.config)
            self.assertEqual(engine.tick(self.config)['reason'], 'owner_changed_before_start')
        self.assertEqual(self.calls, [])

    def test_both_hash_guards_remain_before_and_after_fresh_probe(self):
        for target in (self.exe, Path(self.config['startup_bindings'][1]['path'])):
            for change_on in (2, 3):
                before, calls = target.read_bytes(), []
                def probe(*_):
                    calls.append(True)
                    if len(calls) == change_on:
                        target.write_bytes(b'foreign startup')
                    return evidence()
                engine = self.engine(probe)
                engine.tick(self.config)
                self.assertIn(engine.tick(self.config)['reason'], ('host_binary_changed', 'startup_binding_changed'))
                self.assertEqual(len(calls), change_on)
                target.write_bytes(before)
        self.assertEqual(self.calls, [])

    def test_healthy_still_needs_real_tcp_and_scheduler_not_native_presence(self):
        row = evidence(hosts=1, ports=[True, True], absent=[False, False], fresh=True)
        self.assertEqual(self.engine(lambda *_: row).tick(self.config)['state'], 'healthy')
        row = evidence(hosts=1, ports=[None, None], absent=[False, False], fresh=True)
        self.assertEqual(self.engine(lambda *_: row).tick(self.config)['state'], 'waiting')
        self.assertEqual(self.calls, [])

    def test_paused_never_probes_and_launch_failure_backoff_is_preserved(self):
        self.config['enabled'] = False
        engine = self.engine(lambda *_: self.fail('paused probe'))
        self.assertEqual(engine.tick(self.config)['state'], 'paused')
        self.config['enabled'] = True
        engine = self.engine(lambda *_: evidence())
        def fail(_):
            self.calls.append(True)
            raise OSError('fake launch failure')
        engine.launch = fail
        engine.tick(self.config)
        self.assertEqual(engine.tick(self.config)['reason'], 'host_start_failed_backoff')
        for _ in range(3):
            engine.tick(self.config)
        self.assertEqual(self.calls, [True])


if __name__ == '__main__':
    unittest.main()
