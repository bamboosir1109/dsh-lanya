"""Real native JS facts -> Python restore; all databases are temporary fixtures."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import memory_native_history as history
import patch_memento_visibility as visibility
import patch_memento_history as native_patch

NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node with node:sqlite is required")
class NativeHistoryRestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory(prefix="native-history-restore-template-")
        cls.addClassCleanup(cls.shared.cleanup)
        root = Path(cls.shared.name)
        cls.plugin = root / "plugin"
        shutil.copytree(REPO / "tests/fixtures/memento_native_0_5_23", cls.plugin)
        visibility.patch_package(cls.plugin, apply=True, backup_root=root / "visibility-backup")
        preview = native_patch.patch(cls.plugin)
        native_patch.patch(cls.plugin, apply=True, host_stopped=True,
                           expected_fingerprint=preview["fingerprint"], backup_dir=root / "history-backup")
        cls.base = root / "base.sqlite"
        cls.meta = cls.node(cls.base, """
await native({action:'add',text:'原始中文事实 🐺',tags:['中文','🐺']});
await native({action:'replace',match:'原始中文',text:'修正后的事实 🐺'});
console.log(JSON.stringify({batches:store.db.prepare('SELECT batch_id FROM native_history_batches ORDER BY seq').all().map(x=>x.batch_id)}));
""")

    @classmethod
    def node(cls, database, body):
        code = f"""
import {{openMemoryStore}} from {json.dumps((cls.plugin / 'lib/store.mjs').as_uri())};
import {{MemoryProtocolCore}} from {json.dumps((cls.plugin / 'lib/protocol.mjs').as_uri())};
import {{makeMemoryTool}} from {json.dumps((cls.plugin / 'native-tool.mjs').as_uri())};
const store=openMemoryStore(process.argv[1]);
const core=new MemoryProtocolCore({{store,budgets:{{user:{{userGlobal:9000,workspace:9000}},agent:{{userGlobal:9000,workspace:9000}}}},writePolicy:'ask',gate:async()=> 'allowed-once'}});
const exec={{agent:{{session:{{id:'synthetic-session',header:{{cwd:{json.dumps(str(cls.plugin))},agentPreset:'fixture-preset'}}}}}},callId:'fixture-call',signal:new AbortController().signal}};
const native=args=>makeMemoryTool(core).execute(args,exec);
try {{ {body} }} finally {{store.close()}}
"""
        result = subprocess.run([NODE, "--input-type=module", "-e", code, str(database)],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        if result.returncode:
            raise AssertionError("Synthetic Node pipeline failed: " + result.stderr)
        return json.loads(result.stdout)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="native-history-restore-case-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = self.root / "facts.sqlite"
        shutil.copy2(self.base, self.db)
        self.batch = self.meta["batches"][-1]

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def facts(self):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM entries ORDER BY id")]

    def snapshot(self):
        with self.connection() as conn:
            return {name: [dict(row) for row in conn.execute("SELECT * FROM " + name + " ORDER BY 1")]
                    for name in ("entries", *history.TABLES)}

    def apply(self, batch=None, **kwargs):
        selected = batch or self.batch
        preview = history.restore(self.db, selected)
        return history.restore(self.db, selected, apply=True, writers_stopped=True,
                               expected_fingerprint=preview["fingerprint"], **kwargs)

    def archived(self):
        with self.connection() as conn:
            return dict(conn.execute("SELECT * FROM native_history_rows WHERE batch_id=?", (self.batch,)).fetchone())

    def mutate_archive(self, table, column, value, where="", params=()):
        with self.connection() as conn:
            trigger = table + "_update_refused"
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (trigger,)).fetchone()[0]
            conn.execute("DROP TRIGGER " + trigger)
            conn.execute("UPDATE " + table + " SET " + column + "=? " + where, (value, *params))
            conn.execute(sql)

    def test_actual_native_unicode_batch_roundtrip_and_node_continues_versions(self):
        row = self.archived()
        original, changed = json.loads(row["before_json"]), self.facts()[0]
        self.assertIn("原始中文", original["text"])
        self.assertIn("🐺", row["before_json"])
        self.assertNotIn("\\u539f", row["before_json"])
        self.assertEqual(history.hash_value(row["before_json"]), row["before_sha256"])
        restored = self.apply()
        expected = dict(original, version=changed["version"] + 1)
        self.assertEqual(self.facts(), [expected])
        self.assertEqual(history.list_batches(self.db)["batches"][-1]["restores_batch_id"], self.batch)
        output = self.node(self.db, """
const result=await native({action:'replace',match:'原始中文',text:'真实原生后续修改'});
const version=store.db.prepare('SELECT version FROM entries WHERE id=?').get(result.entry.id).version;
await native({action:'remove',match:'真实原生后续修改'});
console.log(JSON.stringify({version,tombstone:store.db.prepare('SELECT result_version FROM native_history_rows ORDER BY rowid DESC LIMIT 1').get().result_version}));
""")
        self.assertEqual(output, {"version": expected["version"] + 1, "tombstone": expected["version"] + 2})
        self.assertEqual(self.facts(), [])
        self.assertEqual(len(history.list_batches(self.db)["batches"]), 5)
        self.assertTrue(restored["applied"])

    def test_preview_and_list_are_read_only_and_do_not_expose_fact_text(self):
        before = self.snapshot()
        listed = history.list_batches(self.db)
        preview = history.restore(self.db, self.batch)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(preview["row_count"], 1)
        self.assertFalse(preview["applied"])
        self.assertNotIn("text", history.canonical(listed))
        self.assertNotIn("原始中文", history.canonical(preview))

    def test_apply_requires_writers_stopped_and_exact_fingerprint(self):
        before = self.snapshot()
        fingerprint = history.restore(self.db, self.batch)["fingerprint"]
        for options in ({}, {"expected_fingerprint": fingerprint},
                        {"writers_stopped": "false", "expected_fingerprint": fingerprint},
                        {"writers_stopped": 1, "expected_fingerprint": fingerprint},
                        {"writers_stopped": True, "expected_fingerprint": "0" * 64}):
            with self.subTest(options=options), self.assertRaises(history.HistoryConflict):
                history.restore(self.db, self.batch, apply=True, **options)
        self.assertEqual(self.snapshot(), before)

    def test_apply_api_refuses_non_boolean_write_mode(self):
        fingerprint = history.restore(self.db, self.batch)["fingerprint"]
        before = self.snapshot()
        for value in ("false", 1, None):
            with self.subTest(value=value), self.assertRaises(history.HistoryError):
                history.restore(self.db, self.batch, apply=value, writers_stopped=True, expected_fingerprint=fingerprint)
        self.assertEqual(self.snapshot(), before)

    def test_actual_native_escaped_surrogate_tags_cli_restore_and_node_followup(self):
        result = self.node(self.db, """
await native({action:'add',text:'surrogate tag original',tags:[String.fromCharCode(0xD800)]});
await native({action:'replace',match:'surrogate tag original',text:'surrogate tag changed'});
console.log(JSON.stringify(store.db.prepare('SELECT batch_id FROM native_history_batches ORDER BY seq DESC LIMIT 1').get()));
""")
        command = [sys.executable, "-B", str(SCRIPTS / "memory_native_history.py"), "--db", str(self.db)]
        listed = subprocess.run([*command, "--list"], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        preview = subprocess.run([*command, "--restore", result["batch_id"]], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        fingerprint = json.loads(preview.stdout)["fingerprint"]
        applied = subprocess.run([*command, "--restore", result["batch_id"], "--apply", "--writers-stopped", "--fingerprint", fingerprint],
                                 capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        restored = next(row for row in self.facts() if row["text"] == "surrogate tag original")
        self.assertEqual(restored["tags"], '["\\ud800"]')
        self.assertEqual(restored["version"], 3)
        followup = self.node(self.db, """
const changed=await native({action:'replace',match:'surrogate tag original',text:'surrogate tag later'});
const row=store.db.prepare('SELECT * FROM entries WHERE id=?').get(changed.entry.id);
await native({action:'remove',match:'surrogate tag later'});
console.log(JSON.stringify({tagCode:JSON.parse(row.tags)[0].charCodeAt(0),version:row.version,tombstone:store.db.prepare('SELECT result_version FROM native_history_rows ORDER BY rowid DESC LIMIT 1').get().result_version}));
""")
        self.assertEqual(followup, {"tagCode": 0xD800, "version": 4, "tombstone": 5})
        self.assertEqual(len(history.list_batches(self.db)["batches"]), 7)

    def test_inverse_of_inverse_preserves_facts_times_and_advances_version(self):
        after_original = self.facts()[0]
        inverse = self.apply()["inverse_batch_id"]
        self.apply(inverse)
        self.assertEqual(self.facts(), [dict(after_original, version=after_original["version"] + 2)])
        with self.assertRaises(history.HistoryConflict):
            history.restore(self.db, self.batch)
        self.assertEqual(len(history.list_batches(self.db)["batches"]), 4)

    def test_remove_tombstone_restores_at_tombstone_plus_one(self):
        before = self.facts()[0]
        result = self.node(self.db, """
await native({action:'remove',match:'修正后的事实'});
const b=store.db.prepare('SELECT batch_id FROM native_history_batches ORDER BY seq DESC LIMIT 1').get();
console.log(JSON.stringify(b));
""")
        self.assertEqual(self.facts(), [])
        self.apply(result["batch_id"])
        self.assertEqual(self.facts(), [dict(before, version=before["version"] + 2)])

    def test_undo_add_deletes_only_full_matching_row_and_archives_tombstone(self):
        new = self.node(self.db, """
await native({action:'add',text:'新增测试事实'});
console.log(JSON.stringify(store.db.prepare('SELECT batch_id FROM native_history_batches ORDER BY seq DESC LIMIT 1').get()));
""")
        old = next(row for row in self.facts() if row["text"] != "新增测试事实")
        applied = self.apply(new["batch_id"])
        self.assertEqual(self.facts(), [old])
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM native_history_rows WHERE batch_id=?", (applied["inverse_batch_id"],)).fetchone()
            self.assertEqual(row["after_json"], "null")
            self.assertEqual(row["result_version"], json.loads(row["before_json"])["version"] + 1)

    def test_consolidated_global_mixed_workspaces_restore_whole_batch_and_leave_foreign_row(self):
        meta = self.node(self.db, """
const a=store.insertEntry({track:'user',scope:'user-global',workspaceKey:'workspace-a',agentKey:'shared-agent',text:'mixed first fixture'});
const b=store.insertEntry({track:'user',scope:'user-global',workspaceKey:'workspace-b',agentKey:'shared-agent',text:'mixed second fixture'});
const before=store.db.prepare('SELECT * FROM entries WHERE id IN (?,?) ORDER BY id').all(a.id,b.id);
store.consolidateEntries({track:'user',scope:'user-global',agentKey:'shared-agent',matches:['mixed first','mixed second'],text:'mixed derived summary'});
const batch=store.db.prepare('SELECT batch_id FROM native_history_batches ORDER BY seq DESC LIMIT 1').get().batch_id;
store.insertEntry({track:'agent',scope:'workspace',workspaceKey:'foreign-workspace',agentKey:'foreign-agent',text:'unrelated foreign fixture'});
console.log(JSON.stringify({batch,before}));
""")
        foreign = next(row for row in self.facts() if row["text"] == "unrelated foreign fixture")
        result = self.apply(meta["batch"])
        self.assertEqual(result["row_count"], 3)
        facts = {row["id"]: row for row in self.facts()}
        for row in meta["before"]:
            self.assertEqual(facts[row["id"]], dict(row, version=row["version"] + 2))
        self.assertEqual(facts[foreign["id"]], foreign)
        self.assertNotIn("mixed derived summary", [row["text"] for row in facts.values()])

    def test_every_full_sql_field_change_including_recall_and_owner_blocks_restore(self):
        original = self.facts()[0]
        changes = {"id": "reused-fixture-id", "track": "agent", "scope": "user-global",
                   "workspace_key": "different-workspace", "agent_key": "different-agent", "text": "later fact",
                   "source": "later-source", "tags": '[ "中文", "🐺" ]', "version": original["version"] + 1,
                   "created_at": original["created_at"] + 1, "updated_at": original["updated_at"] + 1,
                   "last_recalled": 42, "recall_count": original["recall_count"] + 1, "session_id": "later-session"}
        for field, value in changes.items():
            with self.subTest(field=field):
                with self.connection() as conn:
                    conn.execute('UPDATE entries SET "' + field + '"=?', (value,))
                before = self.snapshot()
                with self.assertRaises(history.HistoryConflict):
                    self.apply()
                self.assertEqual(self.snapshot(), before)
                with self.connection() as conn:
                    conn.execute("DELETE FROM entries")
                    columns = list(original)
                    conn.execute("INSERT INTO entries (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")", tuple(original.values()))

    def test_later_same_id_history_blocks_even_when_current_values_are_reset_exactly(self):
        original = self.facts()[0]
        self.node(self.db, """
await native({action:'replace',match:'修正后的事实',text:'later native fixture'});
console.log('{}');
""")
        with self.connection() as conn:
            conn.execute("UPDATE entries SET " + ",".join(name + "=?" for name in original), tuple(original.values()))
        before = self.snapshot()
        with self.assertRaises(history.HistoryConflict):
            self.apply()
        self.assertEqual(self.snapshot(), before)

    def test_deleted_id_reuse_is_not_overwritten(self):
        original = self.facts()[0]
        result = self.node(self.db, """
await native({action:'remove',match:'修正后的事实'});
console.log(JSON.stringify(store.db.prepare('SELECT batch_id FROM native_history_batches ORDER BY seq DESC LIMIT 1').get()));
""")
        with self.connection() as conn:
            replacement = dict(original, text="reused ID different fact")
            conn.execute("INSERT INTO entries (" + ",".join(replacement) + ") VALUES (" + ",".join("?" for _ in replacement) + ")", tuple(replacement.values()))
        before = self.snapshot()
        with self.assertRaises(history.HistoryConflict):
            self.apply(result["batch_id"])
        self.assertEqual(self.snapshot(), before)

    def test_safe_integer_tombstone_exhaustion_refuses_restore_without_mutation(self):
        result = self.node(self.db, """
store.db.prepare('UPDATE entries SET version=?').run(Number.MAX_SAFE_INTEGER-2);
await native({action:'replace',match:'修正后的事实',text:'revision limit fixture'});
await native({action:'remove',match:'revision limit fixture'});
console.log(JSON.stringify(store.db.prepare('SELECT batch_id FROM native_history_batches ORDER BY seq DESC LIMIT 1').get()));
""")
        before = self.snapshot()
        with self.assertRaises(history.HistoryError):
            history.restore(self.db, result["batch_id"])
        self.assertEqual(self.snapshot(), before)

    def test_correctly_sealed_but_reused_id_or_regressed_revision_is_rejected(self):
        current = self.facts()[0]
        batch_id = "synthetic-reused-batch"
        row = {"batch_id": batch_id, "ordinal": 0, "entry_id": current["id"],
               "before_json": "null", "after_json": history.canonical(current),
               "before_sha256": history.hash_value(None), "after_sha256": history.hash_value(current),
               "result_version": current["version"]}
        with self.connection() as conn:
            header = {"seq": 3, "batch_id": batch_id, "format": history.FORMAT, "operation": "add",
                      "created_at": 42, "session_id": None, "schema_json": history.canonical(history.entry_schema(conn)),
                      "row_count": 1, "restores_batch_id": None}
            batch_hash = history.hash_value({"header": header, "rows": [row]})
            audit = {"audit_id": "synthetic-reused-audit", "batch_id": batch_id, "operation": "add", "created_at": 42, "batch_hash": batch_hash}
            conn.execute("INSERT INTO native_history_batches VALUES (?,?,?,?,?,?,?,?,?,?)", (*header.values(), batch_hash))
            conn.execute("INSERT INTO native_history_rows VALUES (?,?,?,?,?,?,?,?)", tuple(row.values()))
            conn.execute("INSERT INTO native_history_audit VALUES (?,?,?,?,?,?)", (*audit.values(), history.hash_value(audit)))
        before = self.snapshot()
        with self.assertRaises(history.HistoryError):
            history.restore(self.db, batch_id)
        self.assertEqual(self.snapshot(), before)

    def test_transaction_failure_at_each_boundary_rolls_back_facts_history_and_audit(self):
        before = self.snapshot()
        for boundary in ("after_history", "after_row", "before_commit"):
            def failure(point, connection):
                if point == boundary:
                    raise OSError("synthetic disk failure")
            with self.subTest(boundary=boundary), self.assertRaises(OSError):
                self.apply(_fault=failure)
            self.assertEqual(self.snapshot(), before)

    def test_sqlite_failure_is_redacted_and_full_batch_rolls_back(self):
        secret = "PRIVATE_SYNTHETIC_SQLITE_PAYLOAD"
        with self.connection() as conn:
            conn.execute("CREATE TRIGGER fail_restore BEFORE UPDATE ON entries BEGIN SELECT RAISE(ABORT,'" + secret + "'); END")
        before = self.snapshot()
        preview = history.restore(self.db, self.batch)
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "memory_native_history.py"), "--db", str(self.db),
                                 "--restore", self.batch, "--apply", "--writers-stopped", "--fingerprint", preview["fingerprint"]],
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {"error": "IntegrityError"})
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_entry_trigger_cannot_silently_mutate_an_unselected_owner(self):
        self.node(self.db, """
store.insertEntry({track:'agent',scope:'workspace',workspaceKey:'foreign',agentKey:'foreign',text:'foreign untouched'});
console.log('{}');
""")
        target = next(row for row in self.facts() if row["text"] != "foreign untouched")
        with self.connection() as conn:
            conn.execute("CREATE TRIGGER hidden_edit AFTER UPDATE ON entries WHEN NEW.id='" + target["id"] + "' BEGIN UPDATE entries SET text='unexpected side effect' WHERE text='foreign untouched'; END")
        before = self.snapshot()
        with self.assertRaises(history.HistoryConflict):
            self.apply()
        self.assertEqual(self.snapshot(), before)

    def test_canonical_utf8_duplicate_json_owner_and_safe_integer_rules(self):
        before = json.loads(self.archived()["before_json"])
        variants = [json.dumps(before, ensure_ascii=True, sort_keys=True),
                    '{"id":"duplicate",' + history.canonical(before)[1:],
                    history.canonical(dict(before, track="unknown")),
                    history.canonical(dict(before, tags="[null]")),
                    json.dumps(dict(before, version=history.MAX_INTEGER + 1), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    json.dumps(dict(before, version=True), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    history.canonical(before).replace('"text":', '"text":  ', 1)]
        original = self.archived()["before_json"]
        for raw in variants:
            with self.subTest(raw=raw[:20]):
                self.mutate_archive("native_history_rows", "before_json", raw, "WHERE batch_id=?", (self.batch,))
                with self.assertRaises(history.HistoryError):
                    history.restore(self.db, self.batch)
                self.mutate_archive("native_history_rows", "before_json", original, "WHERE batch_id=?", (self.batch,))
        with self.assertRaises(history.HistoryError):
            history.parse_json('"\\ud800"')
        with self.assertRaises(history.HistoryError):
            history.parse_json("NaN")

    def test_row_batch_and_audit_seal_tamper_is_refused(self):
        for table, field, where, params in (("native_history_rows", "before_sha256", "WHERE batch_id=?", (self.batch,)),
                                            ("native_history_batches", "batch_hash", "WHERE batch_id=?", (self.batch,)),
                                            ("native_history_audit", "audit_hash", "WHERE batch_id=?", (self.batch,))):
            with self.connection() as conn:
                original = conn.execute("SELECT " + field + " FROM " + table + " " + where, params).fetchone()[0]
            self.mutate_archive(table, field, "0" * 64, where, params)
            with self.subTest(table=table), self.assertRaises(history.HistoryError):
                history.restore(self.db, self.batch)
            self.mutate_archive(table, field, original, where, params)

    def test_fake_guard_partial_schema_and_reserved_prefix_are_refused_without_repair(self):
        with self.connection() as conn:
            original = conn.execute("SELECT sql FROM sqlite_master WHERE name='native_history_rows_update_refused'").fetchone()[0]
            conn.execute("DROP TRIGGER native_history_rows_update_refused")
            conn.execute("CREATE TRIGGER native_history_rows_update_refused BEFORE UPDATE ON native_history_rows BEGIN SELECT 1; END")
        with self.assertRaises(history.HistoryError):
            history.restore(self.db, self.batch)
        with self.connection() as conn:
            conn.execute("DROP TRIGGER native_history_rows_update_refused")
            conn.execute(original)
            conn.execute("CREATE TRIGGER native_history_foreign_guard AFTER UPDATE ON entries BEGIN SELECT 1; END")
        with self.assertRaises(history.HistoryError):
            history.restore(self.db, self.batch)
        with self.connection() as conn:
            conn.execute("DROP TRIGGER native_history_foreign_guard")
            conn.execute("DROP TABLE native_history_audit")
        with self.assertRaises(history.HistoryError):
            history.restore(self.db, self.batch)
        with self.connection() as conn:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='native_history_audit'").fetchone())

    def test_sql_replace_cannot_overwrite_any_append_only_identity(self):
        with self.connection() as conn:
            for table in history.TABLES:
                row = dict(conn.execute("SELECT * FROM " + table + " LIMIT 1").fetchone())
                with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("INSERT OR REPLACE INTO " + table + " VALUES (" + ",".join("?" for _ in row) + ")", tuple(row.values()))
        self.assertEqual(len(history.list_batches(self.db)["batches"]), 2)

    def test_entries_schema_drift_is_refused(self):
        with self.connection() as conn:
            conn.execute("ALTER TABLE entries ADD COLUMN unrelated_extra TEXT")
        with self.assertRaises(history.HistoryError):
            history.restore(self.db, self.batch)

    def test_child_crash_before_commit_leaves_no_inverse_or_partial_facts(self):
        before = self.snapshot()
        fingerprint = history.restore(self.db, self.batch)["fingerprint"]
        code = """import os,sys
sys.path.insert(0,sys.argv[1]); import memory_native_history as h
def crash(point,conn):
 if point==sys.argv[5]:os._exit(47)
h.restore(sys.argv[2],sys.argv[3],apply=True,writers_stopped=True,expected_fingerprint=sys.argv[4],_fault=crash)
"""
        for boundary in ("after_history", "after_row", "before_commit"):
            result = subprocess.run([sys.executable, "-B", "-c", code, str(SCRIPTS), str(self.db), self.batch, fingerprint, boundary], capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 47, result.stderr)
            self.assertEqual(self.snapshot(), before)
            with self.connection() as conn:
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_two_restore_processes_serialize_and_only_one_inverse_commits(self):
        fingerprint = history.restore(self.db, self.batch)["fingerprint"]
        gate = self.root / "go"
        code = """import pathlib,sys,time,json
sys.path.insert(0,sys.argv[1]); import memory_native_history as h
pathlib.Path(sys.argv[6]).write_text('ready')
while not pathlib.Path(sys.argv[5]).exists():time.sleep(.01)
try:
 print(json.dumps(h.restore(sys.argv[2],sys.argv[3],apply=True,writers_stopped=True,expected_fingerprint=sys.argv[4])))
except Exception as e:
 print(json.dumps({'error':type(e).__name__}));sys.exit(1)
"""
        children = []
        try:
            ready = [self.root / ("ready-" + str(i)) for i in range(2)]
            for marker in ready:
                children.append(subprocess.Popen([sys.executable, "-B", "-c", code, str(SCRIPTS), str(self.db), self.batch, fingerprint, str(gate), str(marker)], stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            deadline = time.monotonic() + 10
            while not all(path.exists() for path in ready) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(all(path.exists() for path in ready))
            gate.write_text("go")
            outputs = [child.communicate(timeout=20) for child in children]
            self.assertEqual(sorted(child.returncode for child in children), [0, 1], outputs)
            self.assertEqual(len(history.list_batches(self.db)["batches"]), 3)
            self.assertEqual(self.facts()[0]["version"], 3)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill(); child.communicate(timeout=10)  # Only this synthetic fixture child.

    def test_empty_native_history_list_does_not_create_tables(self):
        empty = self.root / "empty.sqlite"
        self.node(empty, "console.log('{}');")
        self.assertEqual(history.list_batches(empty), {"operation": "list", "batches": []})
        conn = sqlite3.connect(empty)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name GLOB 'native_history_*'").fetchone()[0], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
