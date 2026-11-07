"""Explicit nominal-CNY migration: real temporary files/SQLite, no runtime I/O."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import shutil
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('migration_budget_runtime', SCRIPTS / 'budget_runtime.py')
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)


class BudgetMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='lanya-cny-migration-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'; self.data.mkdir()
        self.config, self.database = self.data / 'autonomy-budget.json', self.data / 'autonomy-budget.sqlite'
        self.archive = self.root / 'migration-unique'
        previous = budget.project_conf
        budget.project_conf = SimpleNamespace(data_dir=lambda: str(self.data), work_dir=lambda: str(self.root),
                                             get=lambda key, default=None: 'fixture' if key == 'decisions.agentPreset' else default)
        self.addCleanup(setattr, budget, 'project_conf', previous)
        self.now = budget._clock()[0]
        self.config.write_text(budget._json(budget.LEGACY_CONFIG) + '\n', encoding='utf-8')
        owner = budget._configured_owner_hash()
        self.rows = [
            ('a'*64, owner, 's'*64, 'autonomy', budget._clock(self.now)[1], 'b'*64,
             'settled', 50000, 900000, self.now, self.now, 'd'*64),
            ('c'*64, owner, 's'*64, 'autonomy', budget._clock(self.now)[1], 'b'*64,
             'reserved', 50000, None, self.now, self.now, None),
            ('e'*64, owner, 's'*64, 'autonomy', budget._clock(self.now)[1], 'b'*64,
             'unknown', 50000, None, self.now, self.now, None),
            ('f'*64, 'other-owner', 'other-scope', 'private', '2026-10-30', 'foreign-instance',
             'settled', 0, 1234, self.now-1, self.now, '7'*64)]
        connection = sqlite3.connect(str(self.database))
        try:
            connection.executescript(budget.DDL)
            connection.executemany('INSERT INTO budget_turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', self.rows)
            connection.executemany('INSERT INTO budget_meta VALUES (?,?)', [
                ('schema', budget.LEGACY_SCHEMA), ('coverage_start', str(self.now-1000)),
                ('gate', '{"old_evidence":true}'), ('unknown-metadata', 'keep-exactly')])
            connection.commit()
        finally:
            connection.close()
        self.before_rows = self.read_rows()
        self.before_meta = self.read_meta()

    def read_rows(self, path=None):
        connection = sqlite3.connect((path or self.database).as_uri() + '?mode=ro', uri=True)
        try:
            return connection.execute('SELECT * FROM budget_turns ORDER BY turn_key').fetchall()
        finally:
            connection.close()

    def read_meta(self, path=None):
        connection = sqlite3.connect((path or self.database).as_uri() + '?mode=ro', uri=True)
        try:
            return dict(connection.execute('SELECT key,value FROM budget_meta'))
        finally:
            connection.close()

    def test_preview_is_read_only_and_binds_entire_source(self):
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.data.iterdir()}
        value = budget.migration_preview()
        self.assertEqual(value['turn_count'], 4)
        self.assertEqual(value['numeric_scale'], 1)
        self.assertEqual(value['settings_after']['limits']['autonomy'], 5)
        self.assertTrue(value['requires_stopped_main'])
        self.assertFalse(value['automatic_retry'])
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.data.iterdir()})
        self.assertFalse(self.archive.exists())
        with self.assertRaisesRegex(budget.BudgetError, 'migration_required'):
            budget.get_snapshot(self.now)

    def test_migration_preserves_every_turn_and_original_config_inode_and_security(self):
        original_record = budget._secure_record(self.config)[0]
        original_acl = budget._security(self.config)
        preview = budget.migration_preview()
        result = budget.migrate_cny(preview['fingerprint'], str(self.archive))
        self.assertTrue(result['success'])
        self.assertEqual(self.read_rows(), self.before_rows)
        self.assertEqual(self.read_rows(self.archive / 'database-before.sqlite'), self.before_rows)
        self.assertEqual(self.read_meta(self.archive / 'database-before.sqlite'), self.before_meta)
        self.assertEqual(result['slot_record'], original_record)
        self.assertEqual(budget._secure_record(self.archive / 'configuration-before.json')[0], original_record)
        self.assertEqual(budget._security(self.config), original_acl)
        self.assertEqual(budget._security(self.archive / 'database-before.sqlite'), budget._security(self.database))
        self.assertEqual((self.config.stat().st_nlink, (self.archive / 'configuration-before.json').stat().st_nlink), (1, 1))
        self.assertEqual(result['turn_row_mutations'], 0)
        metadata = self.read_meta()
        self.assertEqual(metadata['gate'], self.before_meta['gate'])
        self.assertEqual(metadata['unknown-metadata'], self.before_meta['unknown-metadata'])
        self.assertEqual(set(metadata), set(self.before_meta) | {'currency_contract', 'cny_migration'})
        current = budget.get_snapshot(self.now)
        self.assertEqual(current['effective']['limits']['autonomy'], 5)
        self.assertEqual(current['effective']['reserve_cny'], 0.05)
        self.assertEqual(current['numeric_scale'], 1)
        state = current['usage']['buckets']['autonomy']
        self.assertEqual(state['spent_cny'], 0.9)
        self.assertEqual(state['reserved_cny'], 0.1)
        self.assertEqual(state['unknown_count'], 2)
        self.assertFalse(current['enforcement']['runtime_gate_bound'])

    def test_duplicate_migration_and_existing_archive_cannot_overwrite_evidence(self):
        preview = budget.migration_preview()
        budget.migrate_cny(preview['fingerprint'], str(self.archive))
        before = {p.name: p.read_bytes() for p in self.archive.iterdir()}
        for destination in (self.archive, self.root / 'second-attempt'):
            with self.assertRaises(budget.BudgetError):
                budget.migrate_cny(preview['fingerprint'], str(destination))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.archive.iterdir()})
        self.assertFalse((self.root / 'second-attempt').exists())

    def test_changed_turn_or_config_after_preview_is_refused_before_archive(self):
        preview = budget.migration_preview()
        connection = sqlite3.connect(str(self.database))
        try:
            connection.execute("UPDATE budget_turns SET spent_micros=900001 WHERE status='settled'")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(budget.BudgetError, 'stale_budget_migration'):
            budget.migrate_cny(preview['fingerprint'], str(self.archive))
        self.assertFalse(self.archive.exists())
        preview = budget.migration_preview()
        self.config.write_text(self.config.read_text() + ' ', encoding='utf-8')
        with self.assertRaisesRegex(budget.BudgetError, 'stale_budget_migration'):
            budget.migrate_cny(preview['fingerprint'], str(self.archive))
        self.assertFalse(self.archive.exists())

    def test_concurrent_writer_refused_without_migrating_or_creating_archive(self):
        preview = budget.migration_preview()
        connection = sqlite3.connect(str(self.database))
        try:
            connection.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(budget.BudgetError, 'budget_migration_busy'):
                budget.migrate_cny(preview['fingerprint'], str(self.archive))
        finally:
            connection.rollback(); connection.close()
        self.assertFalse(self.archive.exists())
        self.assertEqual(self.read_meta(), self.before_meta)

    def test_unknown_versions_and_missing_database_never_initialize(self):
        connection = sqlite3.connect(str(self.database))
        try:
            connection.execute("UPDATE budget_meta SET value='unknown/v99' WHERE key='schema'")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(budget.BudgetError):
            budget.migration_preview()
        self.database.unlink()
        with self.assertRaises(Exception):
            budget.migration_preview()
        with self.assertRaises(Exception):
            budget.migrate_cny('a'*64, str(self.archive))
        self.assertFalse(self.database.exists())
        self.assertFalse(self.archive.exists())

    def test_replace_failure_preserves_intent_old_inode_and_database_backup(self):
        preview = budget.migration_preview()
        with mock.patch.object(budget.os, 'replace', side_effect=OSError('synthetic replace failure')):
            with self.assertRaises(OSError):
                budget.migrate_cny(preview['fingerprint'], str(self.archive))
        self.assertTrue((self.archive / 'intent.json').is_file())
        self.assertFalse((self.archive / 'result.json').exists())
        self.assertEqual(self.config.stat().st_ino, (self.archive / 'configuration-before.json').stat().st_ino)
        self.assertEqual(self.config.stat().st_nlink, 2)
        self.assertEqual(self.read_rows(), self.before_rows)
        self.assertEqual(self.read_meta(), self.before_meta)
        with self.assertRaises(Exception):
            budget.get_snapshot(self.now)
        with self.assertRaises(Exception):
            budget.migrate_cny(preview['fingerprint'], str(self.root / 'retry'))
        self.assertFalse((self.root / 'retry').exists())

    def test_failure_after_replace_rolls_back_only_metadata_and_retains_recovery_evidence(self):
        preview = budget.migration_preview()
        real_digest = budget._turn_digest
        calls = []
        def fail_after_metadata(connection):
            calls.append(1)
            if len(calls) == 3:
                raise OSError('synthetic post-replace failure')
            return real_digest(connection)
        with mock.patch.object(budget, '_turn_digest', side_effect=fail_after_metadata):
            with self.assertRaises(OSError):
                budget.migrate_cny(preview['fingerprint'], str(self.archive))
        self.assertEqual(json.loads(self.config.read_text())['schema'], budget.CONFIG_SCHEMA)
        self.assertEqual(self.read_rows(), self.before_rows)
        self.assertEqual(self.read_meta(), self.before_meta)
        self.assertTrue((self.archive / 'intent.json').exists())
        self.assertFalse((self.archive / 'result.json').exists())
        self.assertEqual(self.read_meta(self.archive / 'database-before.sqlite'), self.before_meta)
        with self.assertRaisesRegex(budget.BudgetError, 'migration_required'):
            budget.get_snapshot(self.now)
        with self.assertRaises(budget.BudgetError):
            budget.migration_preview()

    def test_backup_metadata_drift_is_refused_before_intent_or_source_write(self):
        preview = budget.migration_preview()
        copier = budget._copy_security
        def drift_backup(source, target):
            if Path(target).name == 'database-before.sqlite':
                connection = sqlite3.connect(str(target))
                try:
                    connection.execute("UPDATE budget_meta SET value='drift' WHERE key='unknown-metadata'")
                    connection.commit()
                finally:
                    connection.close()
            copier(source, target)
        with mock.patch.object(budget, '_copy_security', side_effect=drift_backup):
            with self.assertRaisesRegex(budget.BudgetError, 'budget_migration_backup_failed'):
                budget.migrate_cny(preview['fingerprint'], str(self.archive))
        self.assertFalse((self.archive / 'intent.json').exists())
        self.assertEqual(self.read_rows(), self.before_rows)
        self.assertEqual(self.read_meta(), self.before_meta)
        self.assertEqual(json.loads(self.config.read_text()), budget.LEGACY_CONFIG)

    def test_same_bytes_database_on_another_inode_is_not_an_owned_database(self):
        foreign = self.root / 'same-bytes.sqlite'
        shutil.copyfile(self.database, foreign)
        budget._copy_security(self.database, foreign)
        before = budget._secure_record(self.database)[0]
        copied = budget._secure_record(foreign)[0]
        self.assertEqual(before['sha256'], copied['sha256'])
        self.assertNotEqual(before['signature'][1], copied['signature'][1])
        self.assertFalse(budget._same_database_identity(before, copied))
        preview = budget.migration_preview()
        with mock.patch.object(budget, '_same_database_identity', return_value=False):
            with self.assertRaisesRegex(budget.BudgetError, 'budget_migration_final_identity_changed'):
                budget.migrate_cny(preview['fingerprint'], str(self.archive))
        self.assertFalse((self.archive / 'result.json').exists())
        self.assertTrue((self.archive / 'intent.json').exists())
        self.assertEqual(self.read_rows(), self.before_rows)
        with self.assertRaises(budget.BudgetError):
            budget.migrate_cny(preview['fingerprint'], str(self.root / 'retry'))

    def test_wal_source_is_refused_before_sqlite_open_or_sidecar_creation(self):
        connection = sqlite3.connect(str(self.database))
        try:
            self.assertEqual(connection.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        finally:
            connection.close()
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.data.iterdir()}
        for action in (budget.migration_preview, lambda: budget.migrate_cny('a'*64, str(self.archive))):
            with self.assertRaisesRegex(budget.BudgetError, 'budget_migration_journal_mode_unsupported'):
                action()
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.data.iterdir()})
        self.assertFalse(self.archive.exists())


if __name__ == '__main__':
    unittest.main()
