"""Scope migration uses synthetic temporary SQLite only. No model or network."""
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "memory_scope_migration.py"
spec = importlib.util.spec_from_file_location("scope_migration_fixture", SOURCE)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class ScopeMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="scope migration fixture ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "memory.db"
        self.connection = sqlite3.connect(self.db)
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.execute("""CREATE TABLE entries(
            id TEXT PRIMARY KEY,track TEXT,scope TEXT,workspace_key TEXT,agent_key TEXT,
            text TEXT,source TEXT,version INTEGER,recall_count INTEGER,last_recalled_at INTEGER,
            created_at INTEGER,updated_at INTEGER,tags TEXT,session_id TEXT,weight REAL,extra_blob BLOB)""")
        for entry_id, track, workspace, agent in (
            ("fixture-agent-1", "agent", "", ""), ("fixture-agent-2", "agent", "", ""),
            ("fixture-user-1", "user", "", ""), ("fixture-other-work", "agent", "other-work", ""),
            ("fixture-other-agent", "agent", "", "other-agent"), ("fixture-existing-target", "agent", "current-work", ""),
        ):
            self.connection.execute("INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                entry_id, track, "workspace", workspace, agent, "original fixture fact " + entry_id, "fixture-source",
                1, 2, 80, 10, 20, '["fixture"]', "fixture-session", 0.5, b"\x00fixture\xff"))
        self.connection.execute("INSERT INTO entries SELECT 'fixture-global',track,'user-global',workspace_key,agent_key,text,source,version,recall_count,last_recalled_at,created_at,updated_at,tags,session_id,weight,extra_blob FROM entries WHERE id='fixture-agent-1'")
        self.connection.commit()
        self.mappings = [dict(source=self.owner(track), destination=self.owner(track, "current-work"), expected_count=count)
                         for track, count in (("agent", 2), ("user", 1))]

    @staticmethod
    def owner(track="agent", workspace="", agent=""):
        return dict(track=track, scope="workspace", workspace_key=workspace, agent_key=agent)

    def all_rows(self):
        return {row["id"]: dict(row) for row in self.connection.execute("SELECT * FROM entries")}

    def tables(self):
        return {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def apply(self):
        preview = migration.migrate(self.db, self.mappings)
        return migration.migrate(self.db, self.mappings, apply=True, expected_fingerprint=preview["fingerprint"])

    def test_preview_is_deterministic_readonly_and_order_independent(self):
        before = self.db.read_bytes()
        first = migration.migrate(self.db, self.mappings)
        with mock.patch.object(migration.time, "time", return_value=9999999):
            second = migration.migrate(self.db, list(reversed(self.mappings)))
        self.assertEqual(first, second)
        self.assertFalse(first["applied"])
        self.assertEqual(first["row_count"], 3)
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual(self.tables(), {"entries"})
        self.assertNotIn("fixture-agent-1", json.dumps(first))
        self.assertNotIn("original fixture fact", json.dumps(first))

    def test_apply_preserves_facts_ids_timestamps_and_unrelated_target_rows(self):
        before = self.all_rows()
        result = self.apply()
        self.assertTrue(result["applied"])
        after = self.all_rows()
        for entry_id, row in before.items():
            if entry_id in ("fixture-agent-1", "fixture-agent-2", "fixture-user-1"):
                expected = dict(row, workspace_key="current-work", version=2)
            else:
                expected = row
            self.assertEqual(after[entry_id], expected)
        header, mappings, rows = migration.verified_batch(self.connection, result["migration_id"])
        self.assertEqual(header["row_count"], 3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(mappings), 2)
        for row in rows:
            self.assertEqual(row["before"]["text"], row["after"]["text"])
            self.assertEqual(row["before"]["extra_blob"], {"sqlite_blob_base64": "AGZpeHR1cmX/"})

    def test_apply_requires_exact_fingerprint_and_expected_count_before_audit_creation(self):
        for fingerprint in (None, "0" * 64):
            with self.assertRaises(migration.MigrationConflict):
                migration.migrate(self.db, self.mappings, apply=True, expected_fingerprint=fingerprint)
            self.assertEqual(self.tables(), {"entries"})
        wrong = json.loads(json.dumps(self.mappings)); wrong[0]["expected_count"] = 3
        with self.assertRaises(migration.MigrationConflict): migration.migrate(self.db, wrong)
        self.assertEqual(self.tables(), {"entries"})

    def test_full_row_recall_source_owner_version_and_schema_changes_invalidate_preview(self):
        changes = ["text='changed fixture'", "source='changed-source'", "recall_count=99", "updated_at=99",
                   "version=2", "agent_key='other-agent'"]
        original = self.all_rows()["fixture-agent-1"]
        for change in changes:
            with self.subTest(change=change):
                preview = migration.migrate(self.db, self.mappings)
                self.connection.execute("UPDATE entries SET " + change + " WHERE id='fixture-agent-1'"); self.connection.commit()
                with self.assertRaises(migration.MigrationConflict):
                    migration.migrate(self.db, self.mappings, apply=True, expected_fingerprint=preview["fingerprint"])
                self.connection.execute("UPDATE entries SET " + ",".join(key+"=?" for key in original) + " WHERE id='fixture-agent-1'", tuple(original.values()))
                self.connection.commit()
        preview = migration.migrate(self.db, self.mappings)
        self.connection.execute("ALTER TABLE entries ADD COLUMN later_metadata TEXT"); self.connection.commit()
        with self.assertRaises(migration.MigrationConflict):
            migration.migrate(self.db, self.mappings, apply=True, expected_fingerprint=preview["fingerprint"])

    def test_all_mappings_and_audit_roll_back_on_a_later_row_failure(self):
        self.connection.execute("CREATE TRIGGER fail_fixture BEFORE UPDATE ON entries WHEN NEW.id='fixture-user-1' BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        self.connection.commit()
        before = self.all_rows(); preview = migration.migrate(self.db, self.mappings)
        with self.assertRaises(sqlite3.IntegrityError):
            migration.migrate(self.db, self.mappings, apply=True, expected_fingerprint=preview["fingerprint"])
        self.assertEqual(self.all_rows(), before)
        self.assertEqual(self.tables(), {"entries"})

    def test_whole_batch_recheck_catches_a_later_trigger_modifying_an_earlier_row(self):
        self.connection.execute("CREATE TRIGGER drift_fixture AFTER UPDATE ON entries WHEN NEW.id='fixture-user-1' BEGIN UPDATE entries SET recall_count=99 WHERE id='fixture-agent-1'; END")
        self.connection.commit()
        before = self.all_rows(); preview = migration.migrate(self.db, self.mappings)
        with self.assertRaises(migration.MigrationConflict):
            migration.migrate(self.db, self.mappings, apply=True, expected_fingerprint=preview["fingerprint"])
        self.assertEqual(self.all_rows(), before)
        self.assertEqual(self.tables(), {"entries"})

    def test_explicit_scope_track_duplicate_and_source_destination_chains_are_rejected(self):
        invalid = []
        for field, value in (("track", "user"), ("scope", "user-global")):
            bad = json.loads(json.dumps(self.mappings)); bad[0]["destination"][field] = value; invalid.append(bad)
        bad = json.loads(json.dumps(self.mappings)); del bad[0]["source"]["agent_key"]; invalid.append(bad)
        bad = json.loads(json.dumps(self.mappings)); bad[0]["expected_count"] = True; invalid.append(bad)
        invalid.append([self.mappings[0], self.mappings[0]])
        invalid.append([self.mappings[0], dict(source=self.owner(workspace="current-work"), destination=self.owner(workspace="third-work"), expected_count=1)])
        for mappings in invalid:
            with self.subTest(mappings=mappings), self.assertRaises(migration.MigrationError):
                migration.migrate(self.db, mappings)
        self.assertEqual(self.tables(), {"entries"})

    def test_distinct_explicit_sources_can_coexist_in_the_same_nonempty_destination(self):
        mappings = [self.mappings[0], dict(source=self.owner(workspace="other-work"), destination=self.owner(workspace="current-work"), expected_count=1)]
        preview = migration.migrate(self.db, mappings)
        result = migration.migrate(self.db, mappings, apply=True, expected_fingerprint=preview["fingerprint"])
        self.assertEqual(result["row_count"], 3)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM entries WHERE track='agent' AND workspace_key='current-work'").fetchone()[0], 4)

    def test_destination_workspace_cannot_be_empty_or_whitespace_and_is_not_silently_normalized(self):
        before = self.all_rows()
        for value in ("", " ", "\t\r\n"):
            mappings = json.loads(json.dumps(self.mappings))
            mappings[0]["destination"]["workspace_key"] = value
            with self.subTest(value=value), self.assertRaises(migration.MigrationError):
                migration.migrate(self.db, mappings)
        self.assertEqual(self.all_rows(), before)
        self.assertEqual(self.tables(), {"entries"})
        mappings = json.loads(json.dumps(self.mappings))
        mappings[0]["destination"]["workspace_key"] = "explicit-Key-kept-exactly"
        plan, _ = migration.migration_plan(self.connection, self.db, mappings)
        selected = next(item for item in plan["mappings"] if item["source"]["track"] == "agent")
        self.assertEqual(selected["source"]["workspace_key"], "")
        self.assertEqual(selected["destination"]["workspace_key"], "explicit-Key-kept-exactly")

    def test_restore_default_preview_is_readonly_and_apply_requires_its_own_fingerprint(self):
        original = self.all_rows(); applied = self.apply(); migration_id = applied["migration_id"]
        before = self.db.read_bytes()
        preview = migration.restore(self.db, migration_id)
        self.assertFalse(preview["applied"])
        self.assertEqual(before, self.db.read_bytes())
        with self.assertRaises(migration.MigrationConflict): migration.restore(self.db, migration_id, apply=True)
        with self.assertRaises(migration.MigrationConflict):
            migration.restore(self.db, migration_id, apply=True, expected_fingerprint=applied["fingerprint"])
        restored = migration.restore(self.db, migration_id, apply=True, expected_fingerprint=preview["fingerprint"])
        self.assertTrue(restored["applied"])
        for entry_id, row in original.items():
            expected = dict(row, version=3) if entry_id in ("fixture-agent-1", "fixture-agent-2", "fixture-user-1") else row
            self.assertEqual(self.all_rows()[entry_id], expected)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM scope_migration_rows").fetchone()[0], 3)
        audits = self.connection.execute("SELECT operation,details_json FROM scope_migration_audit ORDER BY operation").fetchall()
        self.assertEqual([row["operation"] for row in audits], ["migrate", "restore"])
        self.assertEqual(len(json.loads(audits[1]["details_json"])["rows"]), 3)
        with self.assertRaises(migration.MigrationConflict): migration.restore(self.db, migration_id)

    def test_restore_refuses_any_later_content_owner_recall_version_or_id_conflict_without_partial_restore(self):
        result = self.apply(); migration_id = result["migration_id"]
        original = self.all_rows()["fixture-user-1"]
        for change in ("text='new fixture fact'", "recall_count=3", "last_recalled_at=81", "version=3", "workspace_key='other-work'"):
            with self.subTest(change=change):
                self.connection.execute("UPDATE entries SET " + change + " WHERE id='fixture-user-1'"); self.connection.commit()
                before = self.all_rows()
                with self.assertRaises(migration.MigrationConflict): migration.restore(self.db, migration_id)
                self.assertEqual(before, self.all_rows())
                self.connection.execute("UPDATE entries SET " + ",".join(key+"=?" for key in original) + " WHERE id='fixture-user-1'", tuple(original.values()))
                self.connection.commit()
        self.connection.execute("DELETE FROM entries WHERE id='fixture-user-1'"); self.connection.commit()
        with self.assertRaises(migration.MigrationConflict): migration.restore(self.db, migration_id)
        replacement = dict(original, text="a different fixture now occupies this ID", version=1)
        self.connection.execute("INSERT INTO entries VALUES (" + ",".join("?" for _ in replacement) + ")", tuple(replacement.values())); self.connection.commit()
        with self.assertRaises(migration.MigrationConflict): migration.restore(self.db, migration_id)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM scope_migration_audit").fetchone()[0], 1)

    def test_restore_rechecks_the_entire_batch_inside_the_apply_transaction(self):
        result = self.apply(); migration_id = result["migration_id"]
        preview = migration.restore(self.db, migration_id)
        self.connection.execute("UPDATE entries SET recall_count=recall_count+1 WHERE id='fixture-user-1'"); self.connection.commit()
        before = self.all_rows()
        with self.assertRaises(migration.MigrationConflict):
            migration.restore(self.db, migration_id, apply=True, expected_fingerprint=preview["fingerprint"])
        self.assertEqual(self.all_rows(), before)
        self.assertEqual(self.connection.execute("SELECT state FROM scope_migration_batches").fetchone()[0], "applied")

    def test_restore_audit_and_row_writes_roll_back_together_on_failure(self):
        result = self.apply(); migration_id = result["migration_id"]
        preview = migration.restore(self.db, migration_id); before = self.all_rows()
        original_update = migration.update_entry
        calls = []
        def failing(connection, old, new):
            calls.append(old["id"])
            if len(calls) == 2: raise sqlite3.OperationalError("fixture restore fault")
            return original_update(connection, old, new)
        with mock.patch.object(migration, "update_entry", side_effect=failing), self.assertRaises(sqlite3.OperationalError):
            migration.restore(self.db, migration_id, apply=True, expected_fingerprint=preview["fingerprint"])
        self.assertEqual(self.all_rows(), before)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM scope_migration_audit").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT state FROM scope_migration_batches").fetchone()[0], "applied")

    def test_archive_tampering_missing_rows_and_audit_tampering_are_refused(self):
        result = self.apply(); migration_id = result["migration_id"]
        for sql in ("UPDATE scope_migration_rows SET before_json='{}' WHERE ordinal=0",
                    "UPDATE scope_migration_batches SET mappings_json='[]'",
                    "UPDATE scope_migration_audit SET details_json='{}'",
                    "DELETE FROM scope_migration_rows WHERE ordinal=0"):
            with self.subTest(sql=sql):
                self.connection.execute("SAVEPOINT fixture_damage")
                self.connection.execute(sql)
                with self.assertRaises(migration.MigrationConflict): migration.verified_batch(self.connection, migration_id)
                self.connection.execute("ROLLBACK TO fixture_damage"); self.connection.execute("RELEASE fixture_damage")

    def test_noop_batches_bad_paths_and_invalid_requests_do_not_create_databases(self):
        missing = self.root / "missing.db"
        with self.assertRaises(OSError): migration.migrate(missing, self.mappings)
        self.assertFalse(missing.exists())
        path = self.root / "request.json"
        path.write_text('{"mappings":[],"mappings":[]}', encoding="utf-8")
        with self.assertRaises(migration.MigrationError): migration.read_mappings(path)
        empty = [dict(source=self.owner(workspace="unused"), destination=self.owner(workspace="current-work"), expected_count=0)]
        preview = migration.migrate(self.db, empty)
        self.assertEqual(preview["row_count"], 0)
        with self.assertRaises(migration.MigrationError):
            migration.migrate(self.db, empty, apply=True, expected_fingerprint=preview["fingerprint"])

    def test_preview_reserves_enough_sqlite_version_capacity_for_restoration(self):
        self.connection.execute("UPDATE entries SET version=? WHERE id='fixture-agent-1'", (migration.MAX_VERSION - 1,))
        self.connection.commit()
        with self.assertRaises(migration.MigrationError): migration.migrate(self.db, self.mappings)
        self.assertEqual(self.tables(), {"entries"})

    def test_cli_only_emits_counts_fingerprints_batch_id_and_error_classes(self):
        request = self.root / "private-request.json"
        request.write_text(json.dumps({"mappings": self.mappings}), encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = migration.main(["--db", str(self.db), "--mappings", str(request)])
        self.assertEqual(code, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["row_count"], 3)
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("fixture-agent", stdout.getvalue())
        self.assertNotIn("original fixture fact", stdout.getvalue())
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = migration.main(["--db", str(self.db), "--mappings", str(request), "--apply"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue().strip(), "MigrationConflict")


if __name__ == "__main__": unittest.main()
