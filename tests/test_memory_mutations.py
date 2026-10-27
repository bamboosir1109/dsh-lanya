"""Operator memory mutations: synthetic temporary SQLite, no live store access."""
import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SCHEMA = """
CREATE TABLE entries (
 id TEXT PRIMARY KEY, track TEXT NOT NULL, scope TEXT NOT NULL,
 workspace_key TEXT NOT NULL, agent_key TEXT NOT NULL, text TEXT NOT NULL,
 source TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 session_id TEXT, last_recalled INTEGER, recall_count INTEGER NOT NULL DEFAULT 0,
 tags TEXT NOT NULL DEFAULT '[]', version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE audit (
 seq INTEGER PRIMARY KEY, ts INTEGER, action TEXT, track TEXT, scope TEXT,
 entry_id TEXT, text TEXT, outcome TEXT, source TEXT, session_id TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('schemaVersion','4');
"""


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MemoryMutationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="lanya-memory-mutations-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "fixture.db"
        with sqlite3.connect(self.db) as connection:
            connection.executescript(SCHEMA)
        self.context = load("memory_context")
        with mock.patch.dict(sys.modules, {"memory_context": self.context}):
            self.mod = load("memory_mutations")
        self.owner = dict(track="agent", scope="workspace", workspace_key="fixture-workspace", agent_key="fixture-agent")

    def sql(self, statement, args=()):
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(statement, args).fetchall()]

    def insert(self, **changes):
        row = dict(id="fixture-entry", **self.owner, text="synthetic original", source="fixture",
                   created_at=10, updated_at=20, session_id="fixture-session", last_recalled=30,
                   recall_count=8, tags='["fixture"]', version=7)
        row.update(changes)
        self.sql("INSERT INTO entries (" + ",".join(row) + ") VALUES (" +
                 ",".join("?" for _ in row) + ")", tuple(row.values()))
        return row

    def mutate(self, operation, *, apply=True, **changes):
        body = dict(selection=self.owner, id="fixture-entry", expected_version=7,
                    text="synthetic changed")
        body.update(changes)
        return self.mod.mutate(operation, body, db_path=self.db, apply=apply)

    def restore(self, change, *, apply=False, fingerprint=None, selection=None):
        return self.mod.restore_change(change["change_id"], selection or self.owner,
            db_path=self.db, apply=apply, expected_fingerprint=fingerprint)

    def test_preview_preserves_database_bytes_schema_and_rows(self):
        self.insert()
        before = self.db.read_bytes()
        for operation in ("add", "edit", "delete"):
            result = self.mutate(operation, apply=False)
            self.assertFalse(result["applied"])
            self.assertIsNone(result["change_id"])
            self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(self.sql("SELECT name FROM sqlite_master WHERE name=?", (self.mod.ARCHIVE,)))

    def test_add_requires_explicit_complete_owner_and_does_not_default_to_shared(self):
        for selected in (None, {}, dict(track="agent"), dict(self.owner, agent_key=None),
                         dict(self.owner, workspace_key="")):
            with self.subTest(selection=selected), self.assertRaises(self.mod.MemoryMutationError):
                self.mutate("add", selection=selected)
        result = self.mutate("add")
        self.assertEqual(result["entry"]["workspace_key"], self.owner["workspace_key"])
        self.assertEqual(result["entry"]["agent_key"], self.owner["agent_key"])
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["entry"]["recall_count"], 0)
        self.assertEqual(self.sql("SELECT value FROM meta")[0]["value"], "4")

    def test_edit_increments_version_and_seals_complete_before_after_and_audit(self):
        original = self.insert()
        result = self.mutate("edit")
        self.assertEqual(result["version"], 8)
        self.assertEqual(result["entry"]["recall_count"], 8)
        archived = self.sql("SELECT * FROM " + self.mod.ARCHIVE)[0]
        self.assertEqual(json.loads(archived["before_json"]), original)
        self.assertEqual(json.loads(archived["after_json"]), result["entry"])
        for side in ("before", "after"):
            self.assertEqual(archived[side + "_sha256"],
                hashlib.sha256(archived[side + "_json"].encode()).hexdigest())
        self.assertEqual(self.sql("SELECT action FROM audit"), [{"action": "edit-maintenance"}])

    def test_owner_and_version_conflict_cannot_modify_any_row(self):
        self.insert()
        before = self.db.read_bytes()
        for operation in ("edit", "delete"):
            for owner in [dict(self.owner, track="user"), dict(self.owner, scope="user-global"),
                          dict(self.owner, workspace_key="elsewhere"), dict(self.owner, agent_key="another")]:
                with self.assertRaises(self.mod.MemoryMutationConflict):
                    self.mutate(operation, selection=owner)
            for version in (None, True, 0, "7", 6):
                with self.assertRaises(self.mod.MemoryMutationError):
                    self.mutate(operation, expected_version=version)
        self.assertEqual(self.db.read_bytes(), before)

    def test_legacy_shared_owner_is_exact_and_remains_editable(self):
        shared = dict(self.owner, workspace_key="", agent_key="")
        self.insert(**shared)
        result = self.mutate("edit", selection=shared)
        self.assertEqual(result["entry"]["workspace_key"], "")
        self.assertEqual(result["entry"]["agent_key"], "")

    def test_audit_failure_rolls_back_archive_and_entry_together(self):
        self.insert()
        self.sql("CREATE TRIGGER fixture_audit_failure BEFORE INSERT ON audit "
                 "BEGIN SELECT RAISE(ABORT,'fixture audit failure'); END")
        before = self.db.read_bytes()
        for operation in ("add", "edit", "delete"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.mutate(operation)
            self.assertEqual(self.db.read_bytes(), before)

    def test_delete_preserves_full_row_and_restore_advances_version(self):
        original = self.insert()
        deleted = self.mutate("delete")
        self.assertFalse(self.sql("SELECT * FROM entries"))
        preview = self.restore(deleted)
        self.assertFalse(preview["applied"])
        self.assertEqual(preview["entry"]["version"], 9)
        restored = self.restore(deleted, apply=True, fingerprint=preview["fingerprint"])
        row = self.sql("SELECT * FROM entries")[0]
        self.assertEqual(row["version"], 9)
        for key in original:
            if key not in ("version", "updated_at"):
                self.assertEqual(row[key], original[key])
        self.assertTrue(restored["change_id"])
        self.assertEqual(len(self.sql("SELECT * FROM audit")), 2)
        self.assertEqual(len(self.sql("SELECT * FROM " + self.mod.ARCHIVE)), 2)
        with self.assertRaises(self.mod.MemoryMutationConflict):
            self.restore(deleted)

    def test_edit_restore_requires_unchanged_full_after_including_recall_counters(self):
        self.insert()
        edited = self.mutate("edit")
        preview = self.restore(edited)
        self.sql("UPDATE entries SET recall_count=recall_count+1")
        with self.assertRaises(self.mod.MemoryMutationConflict):
            self.restore(edited, apply=True, fingerprint=preview["fingerprint"])
        self.assertEqual(self.sql("SELECT text FROM entries")[0]["text"], "synthetic changed")

    def test_restore_edit_keeps_higher_version_and_restores_original_text(self):
        self.insert()
        edited = self.mutate("edit")
        preview = self.restore(edited)
        result = self.restore(edited, apply=True, fingerprint=preview["fingerprint"])
        self.assertEqual(result["entry"]["text"], "synthetic original")
        self.assertEqual(result["entry"]["version"], 9)
        with self.assertRaises(self.mod.MemoryMutationConflict):
            self.mutate("edit", expected_version=8)

    def test_restore_add_removes_only_unchanged_owned_new_row(self):
        added = self.mutate("add")
        preview = self.restore(added)
        self.restore(added, apply=True, fingerprint=preview["fingerprint"])
        self.assertFalse(self.sql("SELECT * FROM entries"))
        restored = self.sql("SELECT * FROM " + self.mod.ARCHIVE + " WHERE operation='restore'")[0]
        self.assertEqual(json.loads(restored["before_json"])["id"], added["id"])
        self.assertIsNone(json.loads(restored["after_json"]))

    def test_restore_refuses_missing_fingerprint_owner_mismatch_and_occupied_id(self):
        original = self.insert()
        deleted = self.mutate("delete")
        with self.assertRaises(self.mod.MemoryMutationConflict):
            self.restore(deleted, apply=True)
        with self.assertRaises(self.mod.MemoryMutationConflict):
            self.restore(deleted, selection=dict(self.owner, agent_key="foreign"))
        preview = self.restore(deleted)
        self.insert(text="new fact", version=50)
        with self.assertRaises(self.mod.MemoryMutationConflict):
            self.restore(deleted, apply=True, fingerprint=preview["fingerprint"])
        self.assertEqual(self.sql("SELECT text FROM entries")[0]["text"], "new fact")

    def test_tampered_archive_payload_or_metadata_cannot_restore(self):
        self.insert()
        changed = self.mutate("edit")
        for field, value in (("before_json", "{}"), ("operation", "forged"), ("result_version", 100)):
            with self.subTest(field=field):
                row = self.sql("SELECT * FROM " + self.mod.ARCHIVE)[0]
                self.sql("UPDATE " + self.mod.ARCHIVE + " SET " + field + "=?", (value,))
                with self.assertRaises(self.mod.MemoryMutationConflict):
                    self.restore(changed)
                self.sql("UPDATE " + self.mod.ARCHIVE + " SET " + field + "=?", (row[field],))

    def test_later_change_prevents_reusing_an_old_delete_tombstone(self):
        self.insert()
        first = self.mutate("delete")
        self.insert(version=20, text="newer lifetime")
        self.mutate("delete", expected_version=20)
        with self.assertRaisesRegex(self.mod.MemoryMutationConflict, "later"):
            self.restore(first)

    def test_restore_audit_failure_rolls_back_insert_and_restored_marker(self):
        self.insert()
        deleted = self.mutate("delete")
        preview = self.restore(deleted)
        self.sql("CREATE TRIGGER fixture_restore_failure BEFORE INSERT ON audit "
                 "BEGIN SELECT RAISE(ABORT,'fixture restore failure'); END")
        before = self.db.read_bytes()
        with self.assertRaises(sqlite3.IntegrityError):
            self.restore(deleted, apply=True, fingerprint=preview["fingerprint"])
        self.assertEqual(self.db.read_bytes(), before)
        self.assertIsNone(self.sql("SELECT restored_by FROM " + self.mod.ARCHIVE)[0]["restored_by"])

    def test_two_concurrent_editors_cannot_both_commit_the_same_version(self):
        self.insert()
        def edit(name):
            try:
                return self.mutate("edit", text=name)["version"]
            except self.mod.MemoryMutationConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(edit, ("writer one", "writer two")))
        self.assertEqual(sorted(outcomes, key=str), [8, "conflict"])
        self.assertEqual(len(self.sql("SELECT * FROM audit")), 1)

    def test_missing_db_never_creates_a_store(self):
        missing = self.root / "missing.db"
        for apply in (False, True):
            with self.assertRaises(sqlite3.OperationalError):
                self.mod.mutate("add", dict(selection=self.owner, text="fixture"), db_path=missing, apply=apply)
            self.assertFalse(missing.exists())

    def test_panel_routes_use_the_shared_api_and_return_conflict(self):
        self.insert()
        source = ROOT / "server" / "panel-server.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        nodes = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))
                 and node.name in ("Handler", "memory_mutation", "memory_owner_choices")]
        conf = types.SimpleNamespace(work_dir=lambda: str(self.root / "configured"),
                                     get=lambda key, default="": "Fixture-Preset" if key == "decisions.agentPreset" else default)
        namespace = dict(SimpleHTTPRequestHandler=object, urlparse=urlparse, json=json,
                         project_conf=conf, MEMENTO_DB=str(self.db))
        with mock.patch.dict(sys.modules, {"memory_mutations": self.mod, "memory_context": self.context}):
            exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
            handler = object.__new__(namespace["Handler"])
            handler._json = lambda status, body: (status, body)
            for operation in ("edit", "delete"):
                handler.path = "/api/memory/" + operation
                handler._read = lambda: dict(id="fixture-entry", expected_version=6,
                                             selection=self.owner, text="stale fixture")
                status, body = handler.do_POST()
                self.assertEqual(status, 409)
                self.assertEqual(body["code"], "conflict")
            handler.path = "/api/memory/add"
            handler._read = lambda: dict(text="fixture new", selection=self.owner)
            status, body = handler.do_POST()
            self.assertEqual(status, 200)
            self.assertTrue(body["applied"])
            owners = namespace["memory_owner_choices"]()
            choices = [row for row in owners if row.get("origin") == "configured"]
            self.assertEqual({row["track"] for row in choices}, {"user", "agent"})
            self.assertTrue(all(row["count"] == 0 for row in choices))

    def test_cli_previews_and_legacy_seed_never_opens_a_database(self):
        request = self.root / "request.json"
        request.write_text(json.dumps(dict(selection=self.owner, text="fixture add")), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            status = self.mod.main(["add", "--db", str(self.db), "--request", str(request)])
        self.assertEqual(status, 0)
        self.assertFalse(json.loads(output.getvalue())["applied"])
        self.assertFalse(self.sql("SELECT * FROM entries"))
        seed = load("seed_memory")
        for argv in (["--seed"], ["--add", "placeholder"], ["--from-json", "never-read.json"], ["--list"]):
            with mock.patch.object(sqlite3, "connect", side_effect=AssertionError("DB forbidden")), redirect_stderr(io.StringIO()):
                self.assertEqual(seed.main(argv), 2)
        with self.assertRaises(RuntimeError):
            seed.add("unscoped placeholder")


if __name__ == "__main__":
    unittest.main()
