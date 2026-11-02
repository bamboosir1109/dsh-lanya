"""Actual temporary SQLite ledger fixtures; no private runtime or HTTP access."""
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('isolated_budget_reports', SCRIPTS / 'budget_reports.py')
reports = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reports)
budget = reports.budget


class DailySpendingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lanya-report-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        previous = budget.project_conf
        budget.project_conf = SimpleNamespace(data_dir=lambda: str(self.data), work_dir=lambda: str(self.root),
                                             get=lambda key, default=None: 'fixture' if key == 'decisions.agentPreset' else default)
        self.addCleanup(setattr, budget, 'project_conf', previous)
        self.now = self.stamp('2026-11-02T00:05:00+08:00')
        self.index = 0

    @staticmethod
    def stamp(value):
        return int(dt.datetime.fromisoformat(value).timestamp() * 1000)

    def record(self, at, category='autonomy', amount=0.01, status='settled', finish=None):
        self.index += 1
        request = {'budget_contract': budget.CONTRACT, 'cwd': str(self.root), 'agent_preset': 'fixture',
                   'conversation_key': 'group:101' if category == 'group' else 'private:101',
                   'origin': 'observation' if category == 'autonomy' else 'live',
                   'turn_key': hashlib.sha256(str(self.index).encode()).hexdigest(),
                   'instance_id': 'b' * 64, 'meter_available': True, 'operation': 'reserve'}
        self.assertTrue(budget.operate(request, at)['allowed'])
        if status == 'settled':
            receipt = {'budget_contract': budget.CONTRACT, 'api_cost_cny': amount, 'start_seq': 1, 'end_seq': 2, 'calls': 1, 'priced': True}
            budget.operate(dict(request, operation='settle', receipt=receipt), finish or at)
        elif status == 'unknown':
            budget.operate(dict(request, operation='unknown'), finish or at)
        return request

    def snapshot(self, **kwargs):
        return reports.daily_spending(self.now, **kwargs)

    def test_missing_does_not_create_or_claim_a_zero(self):
        value = self.snapshot()
        self.assertEqual(len(value['days']), 30)
        self.assertEqual(value['today'], '2026-11-02')
        self.assertTrue(all(row['total'] is None and row['status'] == 'unknown' for row in value['days']))
        self.assertFalse(value['coverage']['owner_verified'])
        self.assertFalse(self.data.exists())

    def test_shanghai_midnight_rolls_categories_without_cumulative_values(self):
        self.record(self.stamp('2026-11-01T23:59:59+08:00'), 'private', 0.988)
        self.record(self.stamp('2026-11-02T00:00:00+08:00'), 'group', 0.219)
        self.record(self.stamp('2026-11-02T00:01:00+08:00'), 'autonomy', 0.031)
        first, second = self.snapshot(days=2)['days']
        self.assertEqual((first['private'], first['group'], first['autonomy']), (0.988, 0, 0))
        self.assertEqual((second['private'], second['group'], second['autonomy']), (0, 0.219, 0.031))
        self.assertEqual(second['total'], 0.25)

    def test_settles_after_midnight_on_reservation_day(self):
        self.record(self.stamp('2026-11-01T23:59:59+08:00'), finish=self.now)
        first, second = self.snapshot(days=2)['days']
        self.assertEqual(first['total'], 0.01)
        self.assertEqual(second['total'], 0)

    def test_unknown_and_pending_keep_reservations_separate(self):
        self.record(self.now - 30000, status='unknown')
        self.record(self.now - 10000, status='reserved')
        value = self.snapshot(days=1)['days'][0]
        self.assertEqual(value['total'], 0)
        self.assertEqual((value['unknown_count'], value['pending_count'], value['reserved_cny']), (1, 1, 0.1))
        self.assertEqual(value['status'], 'partial')
        self.assertEqual(value['buckets']['autonomy']['turn_count'], 2)

    def test_history_before_tracking_is_null_and_later_empty_day_is_zero(self):
        self.record(self.stamp('2026-10-31T12:00:00+08:00'))
        values = self.snapshot(days=4)['days']
        self.assertIsNone(values[0]['total'])
        self.assertEqual(values[1]['status'], 'partial')
        self.assertEqual(values[2]['total'], 0)
        self.assertEqual(values[2]['status'], 'complete')
        self.assertEqual(values[3]['status'], 'partial')

    def test_day_is_not_taken_from_ambient_os_timezone_or_saved_day(self):
        self.record(self.now - 1000)
        with budget._transaction(True) as connection:
            connection.execute("UPDATE budget_turns SET day='1999-12-31'")
        self.assertEqual(self.snapshot(days=1)['days'][0]['total'], 0.01)

    def test_foreign_owner_not_counted_and_identifiers_not_exposed(self):
        self.record(self.now - 2000, 'private', 0.02)
        other = self.record(self.now - 1000, 'group', 0.03)
        with budget._transaction(True) as connection:
            connection.execute('UPDATE budget_turns SET owner_hash=? WHERE turn_key=?', ('f' * 64, other['turn_key']))
        value = self.snapshot(days=1)
        self.assertEqual(value['days'][0]['total'], 0.02)
        encoded = json.dumps(value)
        for secret in (str(self.root), other['turn_key'], 'private:101', 'group:101', 'f' * 64):
            self.assertNotIn(secret, encoded)

    def test_read_does_not_touch_existing_db_or_deprecated_files(self):
        self.record(self.now - 1000)
        former = self.data / 'budget.json'
        former.write_text('{"spent":999999}')
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.data.iterdir()}
        self.snapshot()
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.data.iterdir()}
        self.assertEqual(before, after)

    def test_other_owner_coverage_never_creates_a_confirmed_zero(self):
        self.record(self.now - 1000)
        with budget._transaction(True) as connection:
            connection.execute('UPDATE budget_turns SET owner_hash=?', ('f' * 64,))
        value = self.snapshot(days=1)
        self.assertIsNone(value['coverage']['start_at'])
        self.assertIsNone(value['days'][0]['total'])

    def test_owner_coverage_starts_at_its_first_turn(self):
        other = self.record(self.stamp('2026-10-30T12:00:00+08:00'))
        with budget._transaction(True) as connection:
            connection.execute('UPDATE budget_turns SET owner_hash=? WHERE turn_key=?', ('f' * 64, other['turn_key']))
        self.record(self.now - 1000)
        first, today = self.snapshot(days=2)['days']
        self.assertIsNone(first['total'])
        self.assertEqual(today['total'], 0.01)

    def test_repeated_receipt_stays_one_turn(self):
        request = self.record(self.now - 1000, amount=0.123456)
        receipt = {'budget_contract': budget.CONTRACT, 'api_cost_cny': 0.123456, 'start_seq': 1, 'end_seq': 2, 'calls': 1, 'priced': True}
        budget.operate(dict(request, operation='settle', receipt=receipt), self.now)
        row = self.snapshot(days=1)['days'][0]
        self.assertEqual((row['total'], row['settled_count']), (0.123456, 1))

    def test_malformed_receipt_metadata_refused_not_displayed_as_zero(self):
        self.record(self.now - 1000)
        with budget._transaction(True) as connection:
            connection.execute("UPDATE budget_turns SET receipt_hash='bad'")
        with self.assertRaises(budget.BudgetError):
            self.snapshot()

    def test_no_coverage_with_turns_refused(self):
        self.record(self.now - 1000)
        with budget._transaction(True) as connection:
            connection.execute("DELETE FROM budget_meta WHERE key='coverage_start'")
        with self.assertRaises(budget.BudgetError):
            self.snapshot()

    def test_invalid_window_and_clock_refused(self):
        for days in (0, 91, 1.5, True):
            with self.assertRaises(budget.BudgetError):
                self.snapshot(days=days)
        for now in (-1, True, 1.1):
            with self.assertRaises(budget.BudgetError):
                reports.daily_spending(now)


if __name__ == '__main__':
    unittest.main()
