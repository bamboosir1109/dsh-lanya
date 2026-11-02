"""Native-report fixtures only; no real account, session, model or HTTP access."""
import copy
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('native_budget_fixture', SCRIPTS / 'budget_reports.py')
reports = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reports)
import panel_activity as activity


class NativeDailyReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lanya-native-report-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'; self.work = self.root / 'workspace'
        self.work.mkdir(); self.home.mkdir()
        self.identity = self.root / 'identity.json'
        self.write(self.identity, {'adminQQ': '101', 'fanGroupQQ': '202'})
        self.mapping = self.home / 'media/onebot/chat-sessions.json'
        self.write(self.mapping, {'private:101': {'session': 'fixture-private', 'workspacePath': str(self.work)},
                                  'group:202': {'session': 'fixture-group', 'workspacePath': str(self.work)}})
        self.now = reports._start(reports.dt.date(2026, 11, 2)) + 300000
        self.day = '2026-11-02'
        self.meter = self.home / 'profiles/desktop/node_modules/dsh-cost-meter'
        self.write(self.meter / 'package.json', {'type': 'module'})
        engine = self.meter / 'lib/turn-cost.js'; engine.parent.mkdir(parents=True)
        engine.write_text("""export function turnCostFromRecords(records, config, start, end) {
          const calls = records.filter(e => e.seq > start && e.seq < end && e.type === 'assistant/message' && e.data.usage)
            .map(e => ({atMs:e.time, priced:e.data.usage.outputTokens !== 999,
              apiCost:e.data.usage.inputTokens / 1000000}));
          return {found:calls.length > 0,calls};
        }""", encoding='utf-8')
        self.node = shutil.which('node')
        if not self.node: self.skipTest('Node required for the native fixture subprocess')
        self.rows = {}
        self.add_source('private', 'plugin:dsh-lanya-observation', 10000)
        self.add_source('group', 'plugin:dsh-onebot', 20000)
        self.ledger = self.home / 'storages/cost-meter/ledger.json'
        self.write(self.ledger, {'version': 1, 'config': {'codingPlans': {}, 'goQuota': {}}, 'days': {
            self.day: {'date': self.day, 'sessions': [{'id': 'fixture-private', 'apiCost': 0.01},
                                                    {'id': 'fixture-group', 'apiCost': 0.02}]}}})
        reports._NATIVE_CACHE.clear()

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def add_source(self, role, origin, amount):
        session = 'fixture-' + role
        path = self.home / 'sessions' / activity._project(str(self.work)) / activity._segment(session) / 'session.v4.jsonl'
        rows = [{'type': 'session', 'version': 4, 'id': session, 'cwd': str(self.work),
                 'agentPreset': 'fixture', 'isSeeded': False},
                {'seq': 0, 'time': self.now-40000, 'type': 'turn/start', 'data': {'turn': 0}},
                {'seq': 1, 'time': self.now-39000, 'type': 'user/message', 'data': {
                    'source': {'kind': origin}, 'content': [{'type': 'text', 'text': 'PRIVATE CONVERSATION'}]}},
                {'seq': 2, 'time': self.now-30000, 'type': 'assistant/message', 'data': {
                    'turn': 0, 'step': 0, 'usage': {'inputTokens': amount, 'outputTokens': 1},
                    'message': {'content': [{'type': 'text', 'text': 'PRIVATE REPLY'}],
                                'source': {'provider': 'fixture', 'model': 'fixture', 'replayState': {'secret': 'TOKEN'}}}}},
                {'seq': 3, 'time': self.now-20000, 'type': 'turn/end', 'data': {'turn': 0}}]
        self.rows[role] = (path, rows)
        self.save(role)

    def save(self, role):
        path, rows = self.rows[role]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps(x) for x in rows) + '\n', encoding='utf-8')

    def report(self, **kwargs):
        options = dict(workspace=str(self.work), agent_preset='fixture', identity_file=str(self.identity),
                       dsh_home=str(self.home), node_executable=self.node, now=self.now, days=2)
        options.update(kwargs)
        return reports.native_daily_spending(**options)

    def test_real_subprocess_rebuilds_three_categories_not_session_names(self):
        result = self.report()
        self.assertEqual(result['availability'], 'available')
        self.assertEqual(result['schema'], 'daily-spending/v2')
        self.assertEqual(result['currency'], 'CNY')
        self.assertEqual(result['basis'], reports.budget.BASIS)
        self.assertEqual(result['numeric_scale'], 1)
        today = result['days'][-1]
        self.assertEqual((today['autonomy'], today['private'], today['group'], today['total']), (0.01, 0, 0.02, 0.03))
        self.assertEqual(today['buckets']['autonomy']['settled_count'], 1)
        self.assertEqual(result['coverage']['reconciled_days'], 1)
        self.assertIsNone(result['days'][0]['total'])

    def test_stdio_contains_no_conversation_recipients_or_session_ids(self):
        original = reports.subprocess.run; payloads = []
        def capture(*args, **kwargs):
            payloads.append(kwargs['input'].decode())
            return original(*args, **kwargs)
        with mock.patch.object(reports.subprocess, 'run', side_effect=capture): result = self.report()
        for secret in ('PRIVATE CONVERSATION', 'PRIVATE REPLY', 'TOKEN', 'fixture-private', 'fixture-group',
                       str(self.work), 'private:101', 'group:202'):
            self.assertNotIn(secret, payloads[0]); self.assertNotIn(secret, json.dumps(result))

    def test_missing_origin_is_not_guessed_from_session_name(self):
        self.rows['private'][1][2]['data']['source']['kind'] = 'other-plugin'; self.save('private')
        result = self.report(); today = result['days'][-1]
        self.assertIsNone(today['total']); self.assertEqual(today['unclassified_count'], 1)

    def test_duplicate_origin_is_ambiguous(self):
        rows = self.rows['private'][1]
        duplicate = copy.deepcopy(rows[2]); duplicate['seq'] = 2
        rows.insert(3, duplicate)
        for seq, row in enumerate(rows[1:]): row['seq'] = seq
        self.save('private')
        self.assertIsNone(self.report()['days'][-1]['total'])

    def test_mismatched_ledger_is_null_not_silently_repriced(self):
        value = json.loads(self.ledger.read_text()); value['days'][self.day]['sessions'][0]['apiCost'] = 99
        self.write(self.ledger, value)
        result = self.report(); self.assertIsNone(result['days'][-1]['total'])
        self.assertIn('native_ledger_snapshot_unreconciled', result['coverage']['reasons'])

    def test_unpriced_call_stays_unknown(self):
        self.rows['private'][1][3]['data']['usage']['outputTokens'] = 999; self.save('private')
        result = self.report(); today = result['days'][-1]
        self.assertIsNone(today['total']); self.assertEqual(today['unknown_count'], 1)
        self.assertEqual(result['coverage']['unpriced_calls'], 1)

    def test_missing_usage_is_an_unknown_turn(self):
        del self.rows['private'][1][3]['data']['usage']; self.save('private')
        value = json.loads(self.ledger.read_text()); value['days'][self.day]['sessions'][0]['apiCost'] = 0
        self.write(self.ledger, value)
        result = self.report(); today = result['days'][-1]
        self.assertEqual(today['unknown_count'], 1); self.assertEqual(today['status'], 'partial')
        self.assertIsNone(today['autonomy']); self.assertIsNone(today['total'])
        self.assertEqual(result['coverage']['no_cost_turns'], 1)

    def test_bad_sequence_and_wrong_owner_are_unavailable(self):
        self.rows['private'][1][3]['seq'] = 1; self.save('private')
        self.assertEqual(self.report()['availability'], 'unavailable')
        self.rows['private'][1][3]['seq'] = 2
        self.rows['private'][1][0]['cwd'] = str(self.root / 'other'); self.save('private')
        self.assertEqual(self.report()['availability'], 'unavailable')

    def test_duplicate_session_mapping_refused(self):
        value = json.loads(self.mapping.read_text()); value['group:202']['session'] = 'fixture-private'; self.write(self.mapping, value)
        self.assertEqual(self.report()['availability'], 'unavailable')

    def test_timeout_is_bounded_and_does_not_expose_error_input(self):
        with mock.patch.object(reports.subprocess, 'run', side_effect=subprocess.TimeoutExpired('PRIVATE PATH', 15)):
            result = self.report()
        self.assertEqual(result['coverage']['reasons'], ['native_projection_timeout'])
        self.assertTrue(all(row['total'] is None for row in result['days']))

    def test_unsupported_node_is_explicitly_unavailable(self):
        response = subprocess.CompletedProcess([], 1, b'{"ok":false,"code":"node_runtime_unsupported"}', b'PRIVATE PATH')
        with mock.patch.object(reports.subprocess, 'run', return_value=response): result = self.report()
        self.assertEqual(result['coverage']['reasons'], ['node_runtime_unsupported'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_source_change_during_projection_refused(self):
        original = reports.subprocess.run
        def change(*args, **kwargs):
            answer = original(*args, **kwargs)
            path = self.rows['private'][0]; path.write_bytes(path.read_bytes() + b'\n')
            return answer
        with mock.patch.object(reports.subprocess, 'run', side_effect=change): result = self.report()
        self.assertEqual(result['availability'], 'unavailable')
        self.assertEqual(result['coverage']['reasons'], ['source_changing'])

    def test_cache_reuses_only_same_date_owner_and_stable_files(self):
        original = reports.subprocess.run
        with mock.patch.object(reports.time, 'time', return_value=self.now / 1000), \
             mock.patch.object(reports.subprocess, 'run', wraps=original) as runner:
            first = self.report(now=None); second = self.report(now=None)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(first['generated_at'], second['generated_at'])

    def test_seeded_inherited_costs_are_excluded(self):
        path, old = self.rows['private']; own = copy.deepcopy(old[1:])
        inherited = copy.deepcopy(old[1:])
        for event in inherited: event['time'] -= 2 * 86400000
        old[0]['isSeeded'] = True
        rows = [old[0], *inherited, {'type': 'session/end-seed', 'time': self.now-45000,
                                   'data': {'inherited': True}}, *own]
        for seq, row in enumerate(rows[1:]): row['seq'] = seq
        self.rows['private'] = (path, rows); self.save('private')
        result = self.report(); self.assertEqual(result['days'][-1]['total'], 0.03)

    def test_native_read_preserves_every_fixture_file(self):
        before = {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(self.report()['availability'], 'available')
        after = {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(before, after)

    def test_bridge_response_must_preserve_the_complete_nominal_currency_contract(self):
        original = reports.subprocess.run
        for patch in ({'currency': 'USD'}, {'basis': 'unknown'}, {'numeric_scale': True},
                      {'source_estimate_currency': 'CNY'}, {'source_estimate_basis': 'actual_supplier_charge'}):
            reports._NATIVE_CACHE.clear()
            def corrupt(*args, **kwargs):
                answer = original(*args, **kwargs)
                payload = json.loads(answer.stdout)
                payload['report'].update(patch)
                return subprocess.CompletedProcess(answer.args, answer.returncode,
                                                  json.dumps(payload).encode('utf-8'), answer.stderr)
            with mock.patch.object(reports.subprocess, 'run', side_effect=corrupt):
                value = self.report()
            self.assertEqual(value['availability'], 'unavailable')
            self.assertTrue(all(row['total'] is None for row in value['days']))

    def test_untracked_external_dependency_is_refused(self):
        (self.meter / 'outside.js').write_text('export const outside = 1;', encoding='utf-8')
        source = self.meter / 'lib/turn-cost.js'
        source.write_text("import '../outside.js';\n" + source.read_text(), encoding='utf-8')
        result = self.report()
        self.assertEqual(result['availability'], 'unavailable')
        self.assertEqual(result['coverage']['reasons'], ['native_dependency_unavailable'])

    def test_parallel_reads_share_one_stable_projection(self):
        original = reports.subprocess.run
        with mock.patch.object(reports.time, 'time', return_value=self.now / 1000), \
             mock.patch.object(reports.subprocess, 'run', wraps=original) as runner:
            with ThreadPoolExecutor(max_workers=3) as pool:
                values = list(pool.map(lambda _: self.report(now=None), range(3)))
        self.assertEqual(runner.call_count, 1)
        self.assertTrue(all(value['availability'] == 'available' for value in values))

    def test_cache_invalidates_changed_pricing_source(self):
        original = reports.subprocess.run
        with mock.patch.object(reports.time, 'time', return_value=self.now / 1000), \
             mock.patch.object(reports.subprocess, 'run', wraps=original) as runner:
            self.report(now=None)
            source = self.meter / 'lib/turn-cost.js'
            source.write_text(source.read_text() + '\n// pricing source changed\n', encoding='utf-8')
            self.report(now=None)
        self.assertEqual(runner.call_count, 2)


if __name__ == '__main__': unittest.main()
