"""Memory maintenance fixtures: temporary SQLite only, no model/network/data reads."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memory_plastic.py"
SCHEMA = """
CREATE TABLE entries (
 id TEXT PRIMARY KEY, track TEXT NOT NULL, scope TEXT NOT NULL,
 workspace_key TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, source TEXT NOT NULL,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, session_id TEXT,
 agent_key TEXT NOT NULL DEFAULT '', last_recalled INTEGER,
 recall_count INTEGER NOT NULL DEFAULT 0, tags TEXT NOT NULL DEFAULT '[]',
 version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE audit (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, action TEXT NOT NULL,
 track TEXT, scope TEXT, entry_id TEXT, text TEXT, outcome TEXT, source TEXT, session_id TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('schemaVersion', '4');
"""


@contextlib.contextmanager
def stub_modules(values):
    """Restore only our keys; do not roll back unrelated module imports."""
    missing = object()
    saved = {key: sys.modules.get(key, missing) for key in values}
    sys.modules.update(values)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is missing:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


class MemoryPlasticTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "fixture.db"
        with sqlite3.connect(self.db) as connection:
            connection.executescript(SCHEMA)
        self.seen = types.SimpleNamespace(
            load_seen_capped=mock.Mock(side_effect=AssertionError("file corpus must stay offline")),
            trim_seen=mock.Mock(side_effect=AssertionError("seen rotation is a separate operation")))
        conf = types.SimpleNamespace(work_dir=lambda: str(self.root),
                                     data_dir=lambda: str(self.root / "custom-data"))
        with stub_modules({"project_conf": conf, "seen_cap": self.seen}):
            spec = importlib.util.spec_from_file_location("memory_plastic_fixture", SCRIPT)
            self.mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.mod)
        self.mod.DB = str(self.db)
        self.owner = dict(track="agent", scope="workspace", workspace_key="workspace-a",
                          agent_key="agent-a")
        for target in ("create_connection",):
            patcher = mock.patch.object(socket, target, side_effect=AssertionError("network forbidden"))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(subprocess, "run", side_effect=AssertionError("subprocess forbidden"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def execute(self, sql, values=()):
        with sqlite3.connect(self.db) as connection:
            return connection.execute(sql, values).fetchall()

    def insert(self, eid, text="steady synthetic memory", created=1, **changes):
        row = dict(id=eid, text=text, source="fixture-user", created_at=created,
                   updated_at=created, session_id="fixture-session", last_recalled=None,
                   recall_count=0, tags="[]", version=1, **self.owner)
        row.update(changes)
        self.execute("INSERT INTO entries (" + ",".join(row) + ") VALUES (" +
                     ",".join("?" for _ in row) + ")", list(row.values()))
        return row

    def entries(self):
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM entries ORDER BY id")]

    def archive(self, batch_id):
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(
                "SELECT * FROM plastic_archive WHERE batch_id = ? ORDER BY entry_id", (batch_id,))]

    def batch_id(self):
        return self.execute("SELECT batch_id FROM plastic_batches")[0][0]

    def assert_no_archive(self):
        self.assertEqual(self.execute("SELECT name FROM sqlite_master WHERE name LIKE 'plastic_%'"), [])

    def bili_group(self, prefix="bili", count=3):
        for index in range(count):
            self.insert(prefix + str(index),
                        text="\u5728 B \u7ad9\u770b\u4e86\u300asynthetic topic %s %d\u300b" % (prefix, index),
                        created=index + 1, tags='["bili","fixture"]')
        rows = [row for row in self.mod.load_db(**self.owner) if row["id"].startswith(prefix)]
        return self.mod.merge_group(rows)

    def test_configured_data_path(self):
        self.assertEqual(Path(self.mod.WORK), self.root)
        self.assertEqual(Path(self.mod.DIARY), self.root / "custom-data" / "diary")

    def test_unsorted_group_keeps_newest_row_complete_and_unchanged(self):
        self.insert("old", created=1)
        latest = self.insert("newest", text="steady synthetic memory " * 20, created=30,
                             updated_at=35, version=7, recall_count=9, last_recalled=32)
        self.insert("middle", created=20)
        merge = self.mod.merge_group(self.mod.load_db(**self.owner))
        self.assertEqual(merge["keep_id"], "newest")
        self.assertEqual(set(merge["merged_ids"]), {"old", "middle"})
        self.assertEqual(self.mod.apply_merges([merge], dry=False), 1)
        self.assertEqual(self.entries(), [latest])
        originals = self.archive(self.batch_id())
        self.assertEqual(len(originals), 3)
        self.assertEqual({row["entry_id"] for row in originals if row["role"] == "kept"}, {"newest"})
        self.assertEqual(json.loads(next(row["row_json"] for row in originals
                                         if row["entry_id"] == "newest")), latest)

    def test_owner_boundaries_and_explicit_selection(self):
        owners = [self.owner, dict(self.owner, track="user"),
                  dict(self.owner, scope="user-global"),
                  dict(self.owner, workspace_key="workspace-b"),
                  dict(self.owner, agent_key="agent-b")]
        for index, owner in enumerate(owners):
            for suffix in ("old", "new"):
                self.insert(str(index) + suffix, created=1 if suffix == "old" else 2, **owner)
        before = self.entries()
        self.assertEqual(len(self.mod.cluster(self.mod.load_db())), 5)
        with self.assertRaises(self.mod.MemoryConflict):
            self.mod.merge_group(self.mod.load_db())
        result = self.mod.consolidate(dry=False, **self.owner)
        self.assertEqual(result["applied"], 1)
        untouched = [row for row in self.entries() if row["id"][0] != "0"]
        self.assertEqual(untouched, [row for row in before if row["id"][0] != "0"])

    def test_default_writes_refuse_to_guess_owner(self):
        with mock.patch.object(self.mod, "_connect") as connect:
            for operation in (self.mod.consolidate, self.mod.compress):
                with self.assertRaisesRegex(self.mod.MemoryConflict, "explicit"):
                    operation(dry=False)
                with self.assertRaises(self.mod.MemoryConflict):
                    operation(dry=False, workspace_key="workspace-a")
            connect.assert_not_called()
        self.assert_no_archive()

    def test_explicit_empty_owner_keys_remain_valid_and_separate(self):
        for owner_key in ("", "workspace-a"):
            self.insert(owner_key + "1", workspace_key=owner_key, agent_key="", created=1)
            self.insert(owner_key + "2", workspace_key=owner_key, agent_key="", created=2)
        owner = dict(self.owner, workspace_key="", agent_key="")
        self.mod.consolidate(dry=False, **owner)
        self.assertEqual({row["id"] for row in self.entries()}, {"2", "workspace-a1", "workspace-a2"})

    def test_stale_version_and_same_version_content_are_rejected(self):
        self.insert("old", created=1)
        self.insert("new", created=2)
        merge = self.mod.merge_group(self.mod.load_db(**self.owner))
        for sql in ("UPDATE entries SET text = 'new fact', version = 2 WHERE id = 'old'",
                    "UPDATE entries SET text = 'another fact', version = 1 WHERE id = 'old'"):
            self.execute(sql)
            before = self.entries()
            with self.assertRaises(self.mod.MemoryConflict):
                self.mod.apply_merges([merge], dry=False)
            self.assertEqual(self.entries(), before)
            self.assert_no_archive()

    def test_delete_failure_rolls_back_originals_archive_and_audit(self):
        self.insert("old", created=1)
        self.insert("newest", created=3)
        self.insert("middle", created=2)
        before = self.entries()
        self.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON entries WHEN OLD.id = 'middle' "
                     "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.mod.consolidate(dry=False, **self.owner)
        self.assertEqual(self.entries(), before)
        self.assertEqual(self.execute("SELECT COUNT(*) FROM audit"), [(0,)])
        self.assert_no_archive()

    def test_audit_failure_rolls_back_entry_changes(self):
        self.insert("old", created=1)
        self.insert("new", created=2)
        before = self.entries()
        self.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON audit "
                     "BEGIN SELECT RAISE(ABORT, 'fixture audit failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.mod.consolidate(dry=False, **self.owner)
        self.assertEqual(self.entries(), before)
        self.assert_no_archive()

    def test_derived_summary_has_lineage_and_exact_restore(self):
        merge = self.bili_group()
        before = self.entries()
        self.mod.apply_merges([merge], dry=False)
        batch_id = self.batch_id()
        live = self.entries()
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["source"], "memory_plastic:derived")
        self.assertEqual(live[0]["version"], 1)
        self.assertIsNone(live[0]["session_id"])
        self.assertTrue({"bili", "fixture", "merged", "derived"} <= set(json.loads(live[0]["tags"])))
        records = self.archive(batch_id)
        originals = [record for record in records if record["role"] == "removed"]
        self.assertEqual([json.loads(record["row_json"]) for record in originals], before)
        self.assertEqual({record["replacement_id"] for record in originals}, {live[0]["id"]})
        for record in records:
            self.assertEqual(record["row_sha256"], hashlib.sha256(record["row_json"].encode()).hexdigest())
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        preview = self.mod.restore_batch(batch_id)
        self.assertTrue(preview["dry"])
        self.assertEqual(preview["restored"], 3)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)
        self.mod.restore_batch(batch_id, dry=False)
        self.assertEqual(self.entries(), before)
        self.assertEqual(self.mod.restore_batch(batch_id, dry=False)["restored"], 0)
        self.assertEqual(self.execute("SELECT value FROM meta WHERE key = 'schemaVersion'"), [('4',)])

    def test_restore_preflights_whole_batch_before_removing_any_summary(self):
        first = self.bili_group("first")
        second = self.bili_group("second")
        self.mod.apply_merges([first, second], dry=False)
        summary_id = self.entries()[-1]["id"]
        self.execute("UPDATE entries SET recall_count = 1, last_recalled = 100 WHERE id = ?", (summary_id,))
        before = self.entries()
        audit_before = self.execute("SELECT COUNT(*) FROM audit")
        for dry in (True, False):
            with self.assertRaises(self.mod.MemoryConflict):
                self.mod.restore_batch(self.batch_id(), dry=dry)
            self.assertEqual(self.entries(), before)
            self.assertEqual(self.execute("SELECT COUNT(*) FROM audit"), audit_before)
            self.assertEqual(self.execute("SELECT state FROM plastic_batches"), [('applied',)])

    def test_restore_failure_rolls_back_summary_originals_and_batch_state(self):
        self.mod.apply_merges([self.bili_group()], dry=False)
        before = self.entries()
        audit_before = self.execute("SELECT COUNT(*) FROM audit")
        self.execute("CREATE TRIGGER fail_restore BEFORE INSERT ON audit "
                     "WHEN NEW.action = 'plastic-restore' AND NEW.entry_id = 'bili1' "
                     "BEGIN SELECT RAISE(ABORT, 'fixture restore failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.mod.restore_batch(self.batch_id(), dry=False)
        self.assertEqual(self.entries(), before)
        self.assertEqual(self.execute("SELECT COUNT(*) FROM audit"), audit_before)
        self.assertEqual(self.execute("SELECT state FROM plastic_batches"), [('applied',)])

    def test_restore_refuses_occupied_original_id(self):
        merge = self.bili_group()
        self.mod.apply_merges([merge], dry=False)
        self.insert("bili0", text="new fact after maintenance", version=5)
        before = self.entries()
        with self.assertRaisesRegex(self.mod.MemoryConflict, "occupied"):
            self.mod.restore_batch(self.batch_id(), dry=False)
        self.assertEqual(self.entries(), before)

    def test_restore_refuses_edited_kept_row_or_changed_scope(self):
        self.insert("old", created=1)
        self.insert("new", created=2)
        self.mod.consolidate(dry=False, **self.owner)
        self.execute("UPDATE entries SET text = 'new fact', version = 2, workspace_key = 'workspace-b'")
        before = self.entries()
        with self.assertRaises(self.mod.MemoryConflict):
            self.mod.restore_batch(self.batch_id(), dry=False)
        self.assertEqual(self.entries(), before)

    def test_corrupt_archived_original_cannot_be_restored(self):
        self.mod.apply_merges([self.bili_group()], dry=False)
        self.execute("UPDATE plastic_archive SET row_json = '{}' WHERE role = 'removed'")
        before = self.entries()
        with self.assertRaisesRegex(self.mod.MemoryConflict, "checksum"):
            self.mod.restore_batch(self.batch_id(), dry=False)
        self.assertEqual(self.entries(), before)

    def test_changed_lineage_refuses_restore(self):
        self.mod.apply_merges([self.bili_group()], dry=False)
        before = self.entries()
        self.execute("UPDATE plastic_archive SET replacement_id = NULL WHERE entry_id = 'bili0'")
        with self.assertRaisesRegex(self.mod.MemoryConflict, "checksum"):
            self.mod.restore_batch(self.batch_id(), dry=False)
        self.assertEqual(self.entries(), before)

    def test_missing_archived_row_refuses_incomplete_restore(self):
        self.mod.apply_merges([self.bili_group()], dry=False)
        self.execute("DELETE FROM plastic_archive WHERE entry_id = 'bili0'")
        before = self.entries()
        with self.assertRaisesRegex(self.mod.MemoryConflict, "checksum"):
            self.mod.restore_batch(self.batch_id(), dry=False)
        self.assertEqual(self.entries(), before)

    def test_compress_archives_only_selected_overfull_owner(self):
        for index in range(5):
            self.insert("a%d" % index, text="unique%d token%d word%d" % (index, index, index),
                        created=index, tags='["bili"]')
        for index in range(3):
            self.insert("b%d" % index, text="foreign%d fixture%d" % (index, index),
                        workspace_key="workspace-b", created=index, tags='["bili"]')
        before = self.entries()
        with mock.patch.object(self.mod, "COMPRESS_OVER", 4), mock.patch.object(self.mod, "COMPRESS_TO", 2):
            result = self.mod.compress(dry=False, **self.owner)
        self.assertEqual(result["trimmed"], 3)
        self.assertEqual(result["left"], 2)
        self.assertEqual([row for row in self.entries() if row["id"].startswith("b")],
                         [row for row in before if row["id"].startswith("b")])
        archived = self.archive(result["batch_id"])
        self.assertEqual({row["entry_id"] for row in archived}, {"a0", "a1", "a2"})
        self.mod.restore_batch(result["batch_id"], dry=False)
        self.assertEqual(self.entries(), before)
        self.seen.trim_seen.assert_not_called()
        self.seen.load_seen_capped.assert_not_called()

    def test_global_preview_does_not_use_total_count_for_small_owners(self):
        for workspace in ("a", "b", "c"):
            for index in range(2):
                self.insert(workspace + str(index), workspace_key=workspace, tags='["bili"]')
        before = self.entries()
        with mock.patch.object(self.mod, "COMPRESS_OVER", 2), mock.patch.object(self.mod, "COMPRESS_TO", 1):
            result = self.mod.compress()
        self.assertEqual(result["merged"], 0)
        self.assertEqual(result["trimmed"], 0)
        self.assertEqual(self.entries(), before)
        self.assert_no_archive()

    def test_compression_merge_and_trim_failure_is_one_transaction(self):
        for index in range(3):
            self.insert("g%d" % index, created=index)
            self.insert("v%d" % index, text="distinct%d category%d keyword%d" % (index, index, index),
                        created=10 + index, tags='["bili"]')
        self.execute("CREATE TRIGGER fail_trim BEFORE DELETE ON entries WHEN OLD.id = 'v1' "
                     "BEGIN SELECT RAISE(ABORT, 'fixture trim failure'); END")
        before = self.entries()
        with mock.patch.object(self.mod, "COMPRESS_OVER", 4), mock.patch.object(self.mod, "COMPRESS_TO", 2):
            with self.assertRaises(sqlite3.IntegrityError):
                self.mod.compress(dry=False, **self.owner)
        self.assertEqual(self.entries(), before)
        self.assertEqual(self.execute("SELECT COUNT(*) FROM audit"), [(0,)])
        self.assert_no_archive()

    def test_compression_preview_matches_atomic_merge_and_trim(self):
        for index in range(3):
            self.insert("g%d" % index, created=index)
            self.insert("v%d" % index, text="distinct%d category%d keyword%d" % (index, index, index),
                        created=10 + index, tags='["bili"]')
        before = self.entries()
        with mock.patch.object(self.mod, "COMPRESS_OVER", 4), mock.patch.object(self.mod, "COMPRESS_TO", 2):
            preview = self.mod.compress(**self.owner)
            self.assert_no_archive()
            self.assertEqual(self.entries(), before)
            applied = self.mod.compress(dry=False, **self.owner)
        for key in ("merged", "trimmed", "left"):
            self.assertEqual(preview[key], applied[key])
        self.assertEqual(applied["left"], len(self.entries()))
        self.assertEqual({row["id"] for row in self.entries()}, {"g2", "v2"})
        self.assertEqual(self.execute("SELECT COUNT(*) FROM plastic_batches"), [(1,)])
        self.mod.restore_batch(applied["batch_id"], dry=False)
        self.assertEqual(self.entries(), before)

    def test_cli_restore_defaults_to_preview_and_unguarded_writes_refuse(self):
        self.mod.apply_merges([self.bili_group()], dry=False)
        before = self.entries()
        with mock.patch.object(sys, "argv", [str(SCRIPT), "--restore-batch", self.batch_id()]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self.mod._cli(), 0)
        self.assertTrue(json.loads(output.getvalue())["dry"])
        self.assertEqual(self.entries(), before)
        with mock.patch.object(sys, "argv", [str(SCRIPT), "--consolidate"]), \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(self.mod._cli(), 1)
        self.assertIn("explicit", error.getvalue())
        self.assertEqual(self.entries(), before)

    def test_repeat_consolidation_does_not_accumulate_duplicate_entries_or_batches(self):
        self.mod.apply_merges([self.bili_group()], dry=False)
        before = self.entries()
        batches = self.execute("SELECT COUNT(*) FROM plastic_batches")
        archived = self.execute("SELECT COUNT(*) FROM plastic_archive")
        result = self.mod.consolidate(dry=False, **self.owner)
        self.assertEqual(result["applied"], 0)
        self.assertIsNone(result["batch_id"])
        self.assertEqual(self.entries(), before)
        self.assertEqual(self.execute("SELECT COUNT(*) FROM plastic_batches"), batches)
        self.assertEqual(self.execute("SELECT COUNT(*) FROM plastic_archive"), archived)


if __name__ == "__main__":
    unittest.main()
