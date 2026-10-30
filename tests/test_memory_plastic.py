"""Memory maintenance fixtures: temporary SQLite only, no model/network/data reads."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
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
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
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
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
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
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM entries ORDER BY id")]

    def archive(self, batch_id):
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
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

    def capsule_facts(self, row):
        stream = io.BytesIO(row["text"].encode("utf-8"))
        self.assertEqual(stream.readline(), b"dsh-memory-capsule/v1\n")
        facts = []
        while line := stream.readline():
            header = json.loads(line)
            raw = stream.read(header["text_bytes"])
            self.assertEqual(len(raw), header["text_bytes"])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), header["text_sha256"])
            self.assertEqual(stream.read(1), b"\n")
            facts.append(dict(text=raw.decode("utf-8"), entries=header["entries"]))
        return facts

    def capsule_originals(self, row):
        return sorted([dict(entry, text=fact["text"]) for fact in self.capsule_facts(row)
                       for entry in fact["entries"]], key=lambda item: item["id"])

    def model_packet(self, query=""):
        def module(name, filename):
            spec = importlib.util.spec_from_file_location(name, SCRIPT.parent / filename)
            value = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(value)
            return value
        context = module("memory_context_fixture", "memory_context.py")
        with stub_modules({"memory_context": context}):
            index = module("memory_index_fixture", "memory_index.py")
            with stub_modules({"memory_index": index}):
                return context.build_context(str(self.root), "fixture-agent", query=query,
                                             db_path=str(self.db))

    def use_model_owner(self):
        self.owner.update(workspace_key=os.path.abspath(self.root).lower(), agent_key="fixture-agent")

    def test_unsorted_group_preserves_every_complete_original_in_capsule(self):
        self.insert("old", created=1)
        self.insert("newest", text="steady synthetic memory " * 20, created=30,
                    updated_at=35, version=7, recall_count=9, last_recalled=32)
        self.insert("middle", created=20)
        before = self.entries()
        merge = self.mod.merge_group(self.mod.load_db(**self.owner))
        self.assertNotIn("keep_id", merge)
        self.assertEqual(set(merge["merged_ids"]), {"old", "middle", "newest"})
        self.assertEqual(self.mod.apply_merges([merge], dry=False), 1)
        self.assertEqual(self.capsule_originals(self.entries()[0]), before)
        originals = self.archive(self.batch_id())
        self.assertEqual(len(originals), 4)
        self.assertEqual({row["role"] for row in originals}, {"removed", "derived"})
        self.assertEqual([json.loads(row["row_json"]) for row in originals if row["role"] == "removed"], before)

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
        foreign = lambda row: any(row[key] != value for key, value in self.owner.items())
        untouched = [row for row in self.entries() if foreign(row)]
        self.assertEqual(untouched, [row for row in before if foreign(row)])

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
        self.assertEqual({row["id"] for row in self.entries() if row["workspace_key"] == "workspace-a"},
                         {"workspace-a1", "workspace-a2"})
        capsule = next(row for row in self.entries() if row["workspace_key"] == "")
        self.assertEqual({row["id"] for row in self.capsule_originals(capsule)}, {"1", "2"})

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

    def test_restore_refuses_edited_capsule_or_changed_scope(self):
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

    def test_compress_never_trims_distinct_tagged_facts_to_soft_target(self):
        for index in range(5):
            self.insert("a%d" % index, text="unique%d token%d word%d" % (index, index, index),
                        created=index, tags='["bili"]')
        for index in range(3):
            self.insert("b%d" % index, text="foreign%d fixture%d" % (index, index),
                        workspace_key="workspace-b", created=index, tags='["bili"]')
        before = self.entries()
        with mock.patch.object(self.mod, "COMPRESS_OVER", 4), mock.patch.object(self.mod, "COMPRESS_TO", 2):
            result = self.mod.compress(dry=False, **self.owner)
        self.assertEqual(result["trimmed"], 0)
        self.assertEqual(result["left"], 5)
        self.assertFalse(result["soft_target_met"])
        self.assertIsNone(result["batch_id"])
        self.assertEqual(self.entries(), before)
        self.assert_no_archive()
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

    def test_compression_capsule_failure_is_one_transaction(self):
        for index in range(3):
            self.insert("g%d" % index, created=index)
            self.insert("v%d" % index, text="distinct%d category%d keyword%d" % (index, index, index),
                        created=10 + index, tags='["bili"]')
        self.execute("CREATE TRIGGER fail_merge BEFORE DELETE ON entries WHEN OLD.id = 'g1' "
                     "BEGIN SELECT RAISE(ABORT, 'fixture capsule failure'); END")
        before = self.entries()
        with mock.patch.object(self.mod, "COMPRESS_OVER", 4), mock.patch.object(self.mod, "COMPRESS_TO", 2):
            with self.assertRaises(sqlite3.IntegrityError):
                self.mod.compress(dry=False, **self.owner)
        self.assertEqual(self.entries(), before)
        self.assertEqual(self.execute("SELECT COUNT(*) FROM audit"), [(0,)])
        self.assert_no_archive()

    def test_compression_preview_matches_atomic_capsules_with_soft_target(self):
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
        self.assertEqual(applied["trimmed"], 0)
        self.assertEqual(applied["left"], 4)
        self.assertFalse(applied["soft_target_met"])
        self.assertEqual([row for row in self.entries() if row["id"].startswith("v")],
                         [row for row in before if row["id"].startswith("v")])
        capsule = next(row for row in self.entries() if row["source"] == "memory_plastic:derived")
        self.assertEqual({row["id"] for row in self.capsule_originals(capsule)}, {"g0", "g1", "g2"})
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

    def test_distinct_name_negation_date_promise_and_todo_remain_queryable(self):
        self.use_model_owner()
        common = "甲的每周出行安排已经核对，所有安排依照同一份行程表逐项执行，相关交通和住宿事项还在持续确认，暂时没有新的总体变动；"
        details = ["丙明确不吃花生。", "承诺五月三日归还护照。", "周四取票未完成。", "入口改为东门。"]
        for index, detail in enumerate(details):
            self.insert("fact-" + str(index), text=common + detail, created=index + 1)
        before = self.entries()
        needles = ["丙明确", "不吃花生", "五月三日", "承诺", "取票未完成"]
        for needle in needles:
            self.assertTrue(any(needle in row["text"] for row in self.model_packet(needle)["memories"]))
        result = self.mod.consolidate(dry=False, **self.owner)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(self.capsule_originals(self.entries()[0]), before)
        for needle in needles:
            packet = self.model_packet(needle)
            self.assertEqual(len(packet["memories"]), 1)
            self.assertTrue(any(needle in row["text"] for row in packet["originals"]))
            self.assertEqual(len(packet["memories"][0]["lineage"]), 4)
            self.assertEqual({row["id"]: (row["created_at"], row["updated_at"], row["version"])
                              for row in packet["originals"]},
                             {row["id"]: (row["created_at"], row["updated_at"], row["version"])
                              for row in before})
        self.mod.restore_batch(result["batch_id"], dry=False)
        self.assertEqual(self.entries(), before)

    def test_bili_tail_titles_and_more_than_eight_rows_split_without_loss(self):
        self.use_model_owner()
        title = "合成资料用于离线测试所有名称日期承诺地点均为虚构内容包括航班交通酒店接送餐饮过敏用药行李保管责任"
        for index, suffix in enumerate(list("甲乙丙丁戊己庚辛壬癸子") + ["五月三日归还护照且不得迟到"]):
            self.insert("video-" + str(index).zfill(2), text="B 站看了《" + title + suffix + "》", created=index + 1)
        before = self.entries()
        result = self.mod.consolidate(dry=False, **self.owner)
        self.assertEqual(result["applied"], 2)
        restored_rows = []
        for row in self.entries():
            originals = self.capsule_originals(row)
            self.assertLessEqual(len(originals), 8)
            self.assertLessEqual(len(row["text"].encode("utf-8")), 8192)
            restored_rows.extend(originals)
        self.assertEqual(sorted(restored_rows, key=lambda row: row["id"]), before)
        packet = self.model_packet("归还护照")
        self.assertTrue(any("五月三日归还护照且不得迟到" in row["text"] for row in packet["originals"]))
        self.mod.restore_batch(result["batch_id"], dry=False)
        self.assertEqual(self.entries(), before)

    def test_mixed_group_preserves_non_bili_fact_and_exact_duplicate_metadata(self):
        self.use_model_owner()
        self.bili_group()
        self.insert("ordinary", text='丙不吃花生；五月三日承诺仍有效。\n保留"引号"与换行。', created=5)
        self.insert("duplicate", text='丙不吃花生；五月三日承诺仍有效。\n保留"引号"与换行。', created=6,
                    source="another-fixture-source", version=3)
        before = self.entries()
        merge = self.mod.merge_group(self.mod.load_db(**self.owner))
        self.mod.apply_merges([merge], dry=False)
        self.assertEqual(self.capsule_originals(self.entries()[0]), before)
        repeated = [fact for fact in self.capsule_facts(self.entries()[0]) if "花生" in fact["text"]]
        self.assertEqual(len(repeated), 1)
        self.assertEqual(len(repeated[0]["entries"]), 2)
        packet = self.model_packet("花生")
        self.assertEqual({row["id"]: row["text"] for row in packet["originals"]},
                         {row["id"]: row["text"] for row in before})

    def test_native_substring_queries_keep_raw_quotes_newlines_and_fake_headers(self):
        raw = '丙不吃"花生"，五月三日归还护照。\\路径\n' + \
              'dsh-memory-capsule/v1\n{"text_bytes":0,"entries":[]}\n承诺仍有效。'
        for index in range(2):
            self.insert("quoted-" + str(index), text=raw, created=index + 1)
        before = self.entries()
        self.assertEqual(self.execute("SELECT count(*) FROM entries WHERE instr(text,?)>0", (raw,)), [(2,)])
        result = self.mod.consolidate(dry=False, **self.owner)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(self.execute("SELECT count(*) FROM entries WHERE instr(text,?)>0", (raw,)), [(1,)])
        self.assertEqual(self.execute("SELECT count(*) FROM entries WHERE text LIKE ?", ("%" + raw + "%",)), [(1,)])
        self.assertEqual(self.capsule_originals(self.entries()[0]), before)

    def test_capsule_byte_bound_keeps_oversized_originals_and_never_truncates(self):
        self.insert("one", text="长事实甲" * 1600)
        self.insert("two", text="长事实乙" * 1600)
        before = self.entries()
        result = self.mod.consolidate(dry=False, **self.owner)
        self.assertEqual(result["applied"], 0)
        self.assertEqual(self.entries(), before)
        self.assert_no_archive()
        rows = [dict(row, text="合成短事实") for row in self.mod.load_db(**self.owner)]
        expected = len(self.mod.merge_group(rows)["text"].encode("utf-8"))
        with mock.patch.object(self.mod, "CAPSULE_MAX_BYTES", expected):
            self.assertIsNotNone(self.mod.merge_group(rows))
        with mock.patch.object(self.mod, "CAPSULE_MAX_BYTES", expected - 1):
            self.assertIsNone(self.mod.merge_group(rows))

    def test_bad_or_lossy_reviewed_capsules_and_victims_are_refused(self):
        self.insert("old", text="丙不吃花生")
        self.insert("new", text="五月三日承诺有效")
        rows = self.mod.load_db(**self.owner)
        for change in ({"text": None}, {"id": []}, {"workspace_key": []}, {"version": 0}):
            with self.assertRaises(self.mod.MemoryConflict):
                self.mod.merge_group([rows[0], dict(rows[1], **change)])
        for change in ({"text": "only the newest fact"}, {"keep_id": "new"}):
            merge = self.mod.merge_group(rows)
            merge.update(change)
            with self.assertRaises(self.mod.MemoryConflict):
                self.mod.apply_merges([merge], dry=False)
            self.assert_no_archive()
        with mock.patch.object(self.mod, "_connect") as connect:
            with self.assertRaises(self.mod.MemoryConflict):
                self.mod._apply_plan([], [rows[0]], dry=False)
            connect.assert_not_called()

    def test_121_distinct_facts_leave_soft_target_unmet_without_any_deletion(self):
        for index in range(121):
            self.insert("independent-" + str(index), text="unique%d token%d field%d" %
                        (index, index, index), created=index, tags='["bili","merged"]')
        before = self.entries()
        with mock.patch.object(self.mod, "_delete_archived") as delete:
            result = self.mod.compress(dry=False, **self.owner)
            delete.assert_not_called()
        self.assertEqual(result["left"], 121)
        self.assertEqual(result["trimmed"], 0)
        self.assertFalse(result["soft_target_met"])
        self.assertIsNone(result["batch_id"])
        self.assertEqual(self.entries(), before)
        self.assert_no_archive()

    def test_nul_original_stays_standalone_without_hiding_other_facts(self):
        common = "甲的每周出行安排已经核对，所有安排依照同一份行程表逐项执行，相关交通和住宿事项还在持续确认，暂时没有新的总体变动；"
        unsafe = self.insert("nul-first", text=common + "甲的原始内容\0后续内容仍保留。", created=1)
        normal = self.insert("normal-second", text=common + "乙在东门等候丙，承诺不取消。", created=2)
        self.insert("normal-third", text=common + "丙于五月三日带药。", created=3)
        before = self.entries()
        rows = self.mod.load_db(**self.owner)
        self.assertIsNone(self.mod.merge_group(rows))
        forged = dict(text="forced unsafe capsule", merged_ids=[row["id"] for row in rows],
                      merged_n=len(rows), _rows=rows)
        with self.assertRaises(self.mod.MemoryConflict):
            self.mod.apply_merges([forged], dry=False)
        self.assert_no_archive()
        query = "%" + normal["text"] + "%"
        self.assertEqual(self.execute("SELECT count(*) FROM entries WHERE text LIKE ?", (query,)), [(1,)])
        result = self.mod.consolidate(dry=False, **self.owner)
        self.assertEqual(result["applied"], 1)
        self.assertIn(unsafe, self.entries())
        capsule = next(row for row in self.entries() if row["source"] == "memory_plastic:derived")
        self.assertNotIn("\0", capsule["text"])
        self.assertIn(normal["text"], capsule["text"])
        self.assertEqual(self.execute("SELECT count(*) FROM entries WHERE text LIKE ?", (query,)), [(1,)])
        self.mod.restore_batch(result["batch_id"], dry=False)
        self.assertEqual(self.entries(), before)


if __name__ == "__main__":
    unittest.main()
