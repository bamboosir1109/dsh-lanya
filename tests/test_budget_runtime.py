"""Temporary budgets only; no account, model, QQ, or session-log access."""
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
spec = importlib.util.spec_from_file_location('isolated_budget_runtime', REPO / 'scripts/budget_runtime.py')
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)


class BudgetRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lanya-budget-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'character'
        self.original = budget.project_conf
        budget.project_conf = SimpleNamespace(data_dir=lambda: str(self.data), work_dir=lambda: str(self.root),
                                             get=lambda key, default=None: 'fixture' if key == 'decisions.agentPreset' else default)
        self.addCleanup(setattr, budget, 'project_conf', self.original)
        self.now = int(dt.datetime(2026, 11, 1, 12).astimezone().timestamp() * 1000)
        self.request = {'budget_contract': budget.CONTRACT, 'cwd': str(self.root), 'agent_preset': 'fixture', 'conversation_key': 'private:101',
                        'origin': 'observation', 'turn_key': 'a' * 64, 'instance_id': 'b' * 64,
                        'meter_available': True, 'operation': 'reserve'}

    def run_op(self, **change):
        return budget.operate(dict(self.request, **change), self.now)

    def configure(self, autonomy=1, private=0, group=0, reserve=0.05):
        return budget.save_config({'limits': {'autonomy': autonomy, 'private': private, 'group': group}, 'reserve_cny': reserve},
                                  budget.get_snapshot(self.now)['revision'])

    def test_snapshot_missing_is_read_only_and_not_historical_zero(self):
        value = budget.get_snapshot(self.now)
        self.assertEqual(value['schema'], 'unified-budget/v2')
        self.assertEqual(value['availability'], 'not_started')
        self.assertIsNone(value['usage']['coverage_start'])
        self.assertFalse(value['usage']['counts_complete'])
        self.assertFalse(value['enforcement']['runtime_gate_bound'])
        self.assertFalse(self.data.exists())

    def test_nominal_yuan_is_scale_one_and_source_currency_is_only_provenance(self):
        self.run_op()
        self.run_op(operation='settle', receipt={'budget_contract': budget.CONTRACT, 'api_cost_cny': 0.9,
                    'start_seq': 1, 'end_seq': 2, 'calls': 1, 'priced': True})
        value = budget.get_snapshot(self.now)
        self.assertEqual(value['currency'], 'CNY')
        self.assertEqual(value['basis'], 'owner_confirmed_numeric_cny_estimate')
        self.assertEqual(value['source_estimate_currency'], 'USD')
        self.assertEqual(value['numeric_scale'], 1)
        self.assertEqual(value['usage']['buckets']['autonomy']['spent_cny'], 0.9)
        self.assertEqual(value['usage']['buckets']['autonomy']['remaining_cny'], 4.1)

    def test_positive_sub_micro_cap_is_not_silently_unlimited_but_tiny_cost_can_round(self):
        for amount in (0.00000001, 0.00000049, 0.0000005, 0.00000051, 0.00000099):
            with self.assertRaises(budget.BudgetError):
                self.configure(autonomy=amount)
        self.assertFalse(self.data.exists())
        with self.assertRaises(budget.BudgetError):
            self.configure(reserve=0.00000099)
        current = self.configure(autonomy=0.000001)
        self.assertFalse(current['usage']['buckets']['autonomy']['can_start_new_turn'])
        self.configure(autonomy=5)
        self.run_op()
        self.run_op(operation='settle', receipt={'budget_contract': budget.CONTRACT, 'api_cost_cny': 0.0000001,
                    'start_seq': 1, 'end_seq': 2, 'calls': 1, 'priced': True})
        self.assertEqual(budget.get_snapshot(self.now)['usage']['buckets']['autonomy']['spent_cny'], 0)

    def test_old_adapter_contract_and_old_usd_receipt_are_refused_without_mutation(self):
        old = dict(self.request); old.pop('budget_contract')
        with self.assertRaisesRegex(budget.BudgetError, 'contract_required'):
            budget.operate(old, self.now)
        self.assertFalse(self.data.exists())
        self.run_op()
        old_receipt = {'api_cost_usd': 0.9, 'start_seq': 1, 'end_seq': 2, 'calls': 1, 'priced': True}
        before = budget.get_snapshot(self.now)['usage']
        with self.assertRaisesRegex(budget.BudgetError, 'invalid_budget_cost_receipt'):
            self.run_op(operation='settle', receipt=old_receipt)
        with self.assertRaisesRegex(budget.BudgetError, 'contract_required'):
            self.run_op(budget_contract=dict(budget.CONTRACT, numeric_scale=True))
        self.assertEqual(budget.get_snapshot(self.now)['usage'], before)

    def test_cny_default_never_reads_legacy_or_guesses_session_prefix(self):
        self.data.mkdir()
        old = self.data / 'budget.json'; old.write_text('{"limit":2.5,"spent":900,"groupMuted":true}')
        before = old.read_bytes()
        self.assertTrue(self.run_op()['allowed'])
        value = budget.get_snapshot(self.now)
        self.assertEqual(value['effective']['currency'], 'CNY')
        self.assertEqual(value['effective']['limits']['autonomy'], 5)
        self.assertEqual(value['usage']['buckets']['autonomy']['reserved_cny'], 0.05)
        self.assertEqual(value['usage']['buckets']['private']['reserved_cny'], 0)
        self.assertEqual(old.read_bytes(), before)

    def test_concurrent_reservations_cannot_borrow_one_allocation(self):
        self.configure(autonomy=0.1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            answers = list(pool.map(lambda n: self.run_op(turn_key=hashlib.sha256(str(n).encode()).hexdigest()), range(4)))
        self.assertEqual(sum(row['allowed'] for row in answers), 2)
        state = budget.get_snapshot(self.now)['usage']['buckets']['autonomy']
        self.assertEqual(state['reserved_cny'], 0.1)
        self.assertTrue(state['exhausted'])

    def test_actual_turn_receipt_settles_once_and_conflicting_receipt_refused(self):
        self.run_op()
        receipt = {'budget_contract': budget.CONTRACT, 'api_cost_cny': 0.031, 'start_seq': 100, 'end_seq': 131, 'calls': 2, 'priced': True}
        self.assertTrue(self.run_op(operation='settle', receipt=receipt)['settled'])
        self.assertTrue(self.run_op(operation='settle', receipt=receipt)['duplicate'])
        with self.assertRaises(budget.BudgetError):
            self.run_op(operation='settle', receipt=dict(receipt, api_cost_cny=0.04))
        state = budget.get_snapshot(self.now)['usage']['buckets']['autonomy']
        self.assertEqual(state['spent_cny'], 0.031)
        self.assertEqual(state['reserved_cny'], 0)
        self.assertTrue(self.run_op(operation='check')['allowed'])

    def test_remaining_smaller_than_reserve_is_explicitly_not_startable(self):
        self.configure(autonomy=0.05)
        self.run_op()
        self.run_op(operation='settle', receipt={'budget_contract': budget.CONTRACT, 'api_cost_cny':0.02,'start_seq':1,'end_seq':2,'calls':1,'priced':True})
        state = budget.get_snapshot(self.now)['usage']['buckets']['autonomy']
        self.assertEqual(state['remaining_cny'], 0.03)
        self.assertFalse(state['exhausted'])
        self.assertFalse(state['can_start_new_turn'])
        self.assertEqual(state['blocked_reason'], 'insufficient_reserve')
        self.assertFalse(self.run_op(turn_key='e' * 64)['allowed'])

    def test_unknown_keeps_reservation_and_restart_does_not_replay(self):
        self.run_op()
        self.run_op(operation='unknown')
        self.assertFalse(self.run_op(instance_id='c' * 64)['allowed'])
        self.assertFalse(self.run_op()['allowed'])
        state = budget.get_snapshot(self.now)['usage']['buckets']['autonomy']
        self.assertEqual(state['reserved_cny'], 0.05)
        self.assertEqual(state['unknown_count'], 1)

    def test_origin_category_and_owner_are_not_taken_from_names(self):
        self.run_op()
        self.run_op(turn_key='c' * 64, origin='live')
        self.run_op(turn_key='d' * 64, origin='live', conversation_key='group:202')
        value = budget.get_snapshot(self.now)['usage']['buckets']
        self.assertEqual([value[k]['turn_count'] for k in ('autonomy','private','group')], [1,1,1])
        for change in ({'agent_preset': 'other'}, {'cwd': str(self.root / 'foreign')}, {'origin': 'onebot-admin'}, {'conversation_key': 'private:bad'}):
            with self.assertRaises(budget.BudgetError): self.run_op(turn_key='e' * 64, **change)
        with self.assertRaises(budget.BudgetError): self.run_op(origin='live')

    def test_foreign_owner_coverage_cannot_claim_current_owner_tracking_or_rewrite_old_reservations(self):
        self.run_op()
        with budget._transaction() as connection:
            original = tuple(connection.execute('SELECT * FROM budget_turns WHERE turn_key=?', ('a' * 64,)).fetchone())
        with patch.object(budget, 'project_conf', SimpleNamespace(data_dir=lambda: str(self.data),
                work_dir=lambda: str(self.root), get=lambda key, default=None: 'other' if key == 'decisions.agentPreset' else default)):
            value = budget.get_snapshot(self.now)
            self.assertEqual(value['availability'], 'not_started')
            self.assertIsNone(value['usage']['coverage_start'])
            self.assertFalse(value['usage']['counts_complete'])
            later = self.now + 1000
            self.assertTrue(budget.operate(dict(self.request, agent_preset='other', turn_key='e' * 64), later)['allowed'])
            self.assertEqual(budget.get_snapshot(later)['usage']['coverage_start'], later)
            self.assertEqual(budget.get_snapshot(later)['usage']['buckets']['autonomy']['reserved_cny'], 0.05)
        with budget._transaction() as connection:
            retained = tuple(connection.execute('SELECT * FROM budget_turns WHERE turn_key=?', ('a' * 64,)).fetchone())
        self.assertEqual(retained, original)

    def test_meter_missing_blocks_finite_autonomy_but_human_control_still_possible(self):
        self.assertFalse(self.run_op(meter_available=False)['allowed'])
        self.configure(private=0.01)
        decision = self.run_op(origin='live', meter_available=False)
        self.assertTrue(decision['allowed']); self.assertTrue(decision['decision_only'])
        self.assertFalse(self.run_op(origin='live', operation='check')['allowed'])
        self.run_op(origin='live', operation='settle', receipt={'budget_contract': budget.CONTRACT, 'api_cost_cny':0.001,'start_seq':1,'end_seq':2,'calls':1,'priced':True})
        self.assertLess(budget.get_snapshot(self.now)['usage']['buckets']['private']['spent_cny'], 0.01)
        self.assertFalse(self.run_op(origin='live', operation='check')['allowed'])

    def test_own_last_slot_is_usable_but_midnight_cannot_reuse_old_day_slot(self):
        self.configure(autonomy=0.05)
        self.assertTrue(self.run_op()['allowed'])
        self.assertTrue(self.run_op(operation='check')['allowed'])
        tomorrow = self.now + 86400000
        result = budget.operate(dict(self.request, operation='check'), tomorrow)
        self.assertFalse(result['allowed']); self.assertEqual(result['reason'], 'reservation_day_changed')
        self.assertTrue(budget.operate(dict(self.request, turn_key='d' * 64), tomorrow)['allowed'])

    def test_daily_cap_rolls_over_at_shanghai_midnight_on_a_utc_host(self):
        class UtcHostDateTime(dt.datetime):
            @classmethod
            def fromtimestamp(cls, stamp, tz=None):
                return super().fromtimestamp(stamp, tz or dt.timezone.utc)

        # The simulated host remains on Nov 1 while the configured reporting
        # day changes to Nov 2. Do not alter the machine's timezone or old rows.
        before = int(dt.datetime(2026, 11, 1, 15, 59, 59, tzinfo=dt.timezone.utc).timestamp() * 1000)
        after = before + 2000
        with patch.object(budget, 'dt', SimpleNamespace(datetime=UtcHostDateTime)):
            self.now = before
            self.configure(autonomy=0.05)
            self.assertTrue(self.run_op()['allowed'])
            self.assertEqual(budget.get_snapshot(before)['usage']['date'], '2026-11-01')
            self.assertEqual(budget.get_snapshot(after)['usage']['date'], '2026-11-02')
            expired = budget.operate(dict(self.request, operation='check'), after)
            self.assertFalse(expired['allowed'])
            self.assertEqual(expired['reason'], 'reservation_day_changed')
            self.assertTrue(budget.operate(dict(self.request, turn_key='d' * 64), after)['allowed'])
        with budget._transaction() as connection:
            rows = list(connection.execute('SELECT day,status,reserve_micros FROM budget_turns ORDER BY created_at'))
        self.assertEqual([row['day'] for row in rows], ['2026-11-01', '2026-11-02'])
        self.assertTrue(all(row['status'] == 'reserved' and row['reserve_micros'] == 50000 for row in rows))

    def test_revision_cas_preserves_acl_and_rejects_arbitrary_spending_edits(self):
        first = self.configure()
        config = self.data / 'autonomy-budget.json'
        from panel_autonomy import _security
        acl = _security(config)
        current = self.configure(autonomy=2)
        self.assertEqual(_security(config), acl)
        with self.assertRaises(budget.BudgetError): budget.save_config({'limits': {'autonomy': 3, 'private': 0, 'group': 0}, 'reserve_cny': 0.05}, first['revision'])
        with self.assertRaises(budget.BudgetError): budget.save_config({'spent_cny': 0}, current['revision'])
        self.assertEqual(budget.get_snapshot(self.now)['effective']['limits']['autonomy'], 2)

    def test_invalid_json_amounts_and_database_fail_closed(self):
        self.data.mkdir()
        config = self.data / 'autonomy-budget.json'
        for raw in ('{"schema":1,"schema":2}', '{"reserve_cny":NaN}'):
            config.write_text(raw)
            with self.assertRaises(budget.BudgetError): budget.get_snapshot(self.now)
        config.unlink()
        for bad in (True, False, -1, float('inf'), '1'):
            with self.assertRaises(budget.BudgetError): self.configure(autonomy=bad)
        (self.data / 'autonomy-budget.sqlite').write_bytes(b'not a SQLite budget')
        with self.assertRaises(Exception): budget.get_snapshot(self.now)

    def test_heartbeat_is_real_fresh_metadata_not_a_balance(self):
        budget.operate({'budget_contract': budget.CONTRACT, 'operation': 'heartbeat', 'instance_id': 'b' * 64, 'pid': os.getpid(), 'adapter_source_sha256':'c'*64}, self.now)
        self.assertTrue(budget.get_snapshot(self.now)['enforcement']['runtime_gate_bound'])
        self.assertFalse(budget.get_snapshot(self.now + 90001)['enforcement']['runtime_gate_bound'])
        self.assertFalse(budget.get_snapshot(self.now - 1)['enforcement']['runtime_gate_bound'])
        self.assertFalse(budget.get_snapshot(self.now)['enforcement']['provider_hard_cap'])
        self.assertEqual(budget.get_snapshot(self.now)['availability'], 'not_started')
        self.assertEqual(budget.gate_evidence(self.now)['pid'], os.getpid())
        self.assertEqual(budget.gate_evidence(self.now)['adapter_source_sha256'], 'c' * 64)

    def test_paired_backend_heartbeat_is_typed_fresh_and_does_not_change_limits(self):
        request = {'budget_contract': budget.CONTRACT, 'operation': 'heartbeat', 'instance_id': 'b' * 64,
                   'pid': os.getpid(), 'adapter_source_sha256': 'c' * 64}
        limits = budget.get_snapshot(self.now)['effective']
        budget.operate(request, self.now)
        self.assertFalse(budget.gate_evidence(self.now)['backend_transport_bound'])
        self.assertFalse(budget.gate_evidence(self.now)['backend_quote_bound'])
        ready = dict(request, backend_transport_bound=True, backend_quote_bound=True)
        budget.operate(ready, self.now)
        evidence = budget.gate_evidence(self.now)
        self.assertTrue(evidence['backend_transport_bound']); self.assertTrue(evidence['backend_quote_bound'])
        self.assertEqual(budget.get_snapshot(self.now)['effective'], limits)
        for at in (self.now - 1, self.now + 90001):
            self.assertFalse(budget.gate_evidence(at)['backend_transport_bound'])
            self.assertFalse(budget.gate_evidence(at)['backend_quote_bound'])
        for invalid in (dict(request, backend_transport_bound=True),
                        dict(ready, backend_transport_bound=1), dict(ready, backend_quote_bound='true')):
            with self.assertRaisesRegex(budget.BudgetError, 'invalid_budget_heartbeat'):
                budget.operate(invalid, self.now)
        budget.operate(request, self.now)
        self.assertFalse(budget.gate_evidence(self.now)['backend_transport_bound'])
        self.assertFalse(budget.gate_evidence(self.now)['backend_quote_bound'])

    @unittest.skipUnless(shutil.which('powershell.exe'), 'Windows PowerShell required')
    def test_actual_unified_legacy_script_gates_use_only_snapshot(self):
        scripts = self.root / 'napcat'; scripts.mkdir()
        for name in ('budget.ps1','budget_runtime.py','panel_autonomy.py','qq_observation.ps1','project_conf.ps1','project_conf.py','decision_events.py'):
            shutil.copyfile(REPO / 'scripts' / name, scripts / name)
        self.data.mkdir()
        old = self.data / 'budget.json'; old.write_text('{"groupMuted":true,"spent":900}')
        project = self.root / 'project.json'; project.write_text(json.dumps({'dataDirName':'character','decisions':{'mode':'unified','agentPreset':'fixture'}}))
        env = dict(os.environ, LANYA_WORKDIR=str(self.root), LANYA_PROJECT_CONFIG=str(project), LANYA_PYTHON=sys.executable)
        upper = {key.upper(): value for key, value in env.items()}
        env['PSModulePath'] = os.pathsep.join([str(Path(upper['USERPROFILE'])/'Documents/WindowsPowerShell/Modules'),str(Path(upper['PROGRAMFILES'])/'WindowsPowerShell/Modules'),str(Path(upper['SYSTEMROOT'])/'System32/WindowsPowerShell/v1.0/Modules')])
        before = old.read_bytes()
        result = subprocess.run([shutil.which('powershell.exe'),'-NoProfile','-File',str(scripts/'budget.ps1'),'-Notify'], env=env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        self.assertEqual(json.loads(result.stdout.decode('utf-8-sig'))['schema'], 'unified-budget/v2')
        self.assertEqual(old.read_bytes(), before)
        self.assertFalse((self.data/'decision-inbox').exists())
        self.assertFalse((self.data/'autonomy-budget.sqlite').exists())
        source = str(REPO/'scripts/groupwatch.ps1').replace("'", "''")
        harness = self.root/'branch.ps1'
        harness.write_text("$ErrorActionPreference='Stop'\nfunction Test-QqUnifiedMode {$true}\nfunction Test-Path {$true}\nfunction Get-Content {throw 'legacy budget read reached'}\n$budgetPath='fixture'\n$t=$null;$e=$null;$a=[Management.Automation.Language.Parser]::ParseFile('"+source+"',[ref]$t,[ref]$e)\n$b=$a.Find({param($n) $n -is [Management.Automation.Language.IfStatementAst] -and $n.Clauses[0].Item1.Extent.Text -eq '-not (Test-QqUnifiedMode) -and (Test-Path $budgetPath)'},$true)\nif(-not $b){throw 'missing gate'}\nInvoke-Expression $b.Extent.Text\nWrite-Output 'continued'", encoding='utf-8-sig')
        result = subprocess.run([shutil.which('powershell.exe'),'-NoProfile','-File',str(harness)], env=env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        self.assertEqual(result.stdout.strip(), b'continued')


if __name__ == '__main__': unittest.main()
