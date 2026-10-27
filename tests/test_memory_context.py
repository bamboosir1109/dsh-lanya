"""Scoped memento/context and panel route checks using only temporary SQLite."""
import ast
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
from urllib.parse import parse_qs, urlparse


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
SCHEMA = """
CREATE TABLE entries (id TEXT PRIMARY KEY, track TEXT, scope TEXT, workspace_key TEXT,
 agent_key TEXT, text TEXT, source TEXT, version INTEGER, created_at INTEGER,
 updated_at INTEGER, session_id TEXT, tags TEXT, last_recalled INTEGER, recall_count INTEGER);
CREATE TABLE audit (seq INTEGER PRIMARY KEY, ts INTEGER, action TEXT, track TEXT, scope TEXT,
 entry_id TEXT, text TEXT, outcome TEXT, source TEXT, session_id TEXT);
"""


class MemoryContextTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / "fixture.db"
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.executescript(SCHEMA)
        keys = ("memory_context", "memory_index", "memory_plastic", "project_conf", "seen_cap")
        missing = object()
        saved = {key: sys.modules.get(key, missing) for key in keys}
        def restore_modules():
            for key, value in saved.items():
                if value is missing:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = value
        self.addCleanup(restore_modules)
        sys.modules["project_conf"] = types.SimpleNamespace(
            data_dir=lambda: str(self.root / "data"), work_dir=lambda: str(self.root),
            get=lambda key, default=None: "configured-fixture" if key == "decisions.agentPreset" else default)
        sys.modules["seen_cap"] = types.SimpleNamespace(load_seen_capped=mock.Mock(return_value=[]))
        self.context = self.load("memory_context")
        self.index = self.load("memory_index")
        self.plastic = self.load("memory_plastic")
        for module in (self.context, self.index, self.plastic):
            module.DB = str(self.db)
        self.cwd = str(self.root / "workspace-a")
        self.preset = "Agent-A"
        self.owner = dict(track="agent", scope="workspace",
                          **self.context.session_owner(self.cwd, self.preset))
        for target in ((socket, "create_connection"), (subprocess, "run")):
            patcher = mock.patch.object(*target, side_effect=AssertionError("external work forbidden"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def load(self, name):
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    def sql(self, statement, values=()):
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            return connection.execute(statement, values).fetchall()

    def insert(self, eid, text="synthetic needle", **changes):
        row = dict(id=eid, text=text, source="fixture-user", version=3, created_at=1,
                   updated_at=2, session_id="fixture-session", tags='["fixture"]',
                   last_recalled=None, recall_count=0, **self.owner)
        row.update(changes)
        self.sql("INSERT INTO entries (" + ",".join(row) + ") VALUES (" +
                 ",".join("?" for _ in row) + ")", list(row.values()))
        return row

    def rows(self):
        return self.sql("SELECT id, text, version, workspace_key, agent_key FROM entries ORDER BY id")

    def panel(self):
        path = REPO / "server" / "panel-server.py"
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        names = {"memory_owner_choices", "memory_maintenance", "Handler"}
        nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                 and node.name in names]
        namespace = dict(SimpleHTTPRequestHandler=object, urlparse=urlparse, parse_qs=parse_qs,
                         MEMENTO_DB=str(self.db), json=json, project_conf=sys.modules["project_conf"])
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
        handler = object.__new__(namespace["Handler"])
        handler._json = lambda status, value: (status, value)
        return handler

    def post(self, path, body):
        handler = self.panel()
        handler.path = path
        handler._read = lambda: body
        return handler.do_POST()

    def test_native_key_normalization_and_empty_shared_keys(self):
        self.assertEqual(self.context.workspace_key_of(r"C:\Workspace\Part\..\Project", "win32"),
                         r"c:\workspace\project")
        self.assertEqual(self.context.agent_key_of("  Agent-A  ", "win32"), "agent-a")
        self.assertEqual(self.context.workspace_key_of(""), "")
        self.assertEqual(self.context.agent_key_of(""), "")
        with self.assertRaises(ValueError):
            self.context.session_owner(None, "")

    def test_model_visibility_filters_before_ranking_and_limit(self):
        self.insert("current")
        self.insert("shared-agent", agent_key="")
        self.insert("global-current", scope="user-global", workspace_key="elsewhere")
        self.insert("global-shared", scope="user-global", workspace_key="", agent_key="")
        self.insert("empty-workspace", workspace_key="", agent_key="")
        self.insert("foreign-agent", agent_key="other-agent")
        self.insert("foreign-global", scope="user-global", agent_key="other-agent")
        for index in range(30):
            self.insert("foreign-workspace-%d" % index, workspace_key="elsewhere",
                        text="needle needle needle synthetic")
        expected = {"current", "shared-agent", "global-current", "global-shared"}
        index = self.index.Index.for_model(self.cwd, self.preset)
        self.assertEqual({row["id"] for row in index.entries}, expected)
        self.assertEqual({row["id"] for row in index.search("needle", limit=50)}, expected)
        self.assertIn(index.search("needle", limit=1)[0]["id"], expected)
        self.assertEqual(index.search("needle", limit=0), [])
        self.assertEqual(index.n, 4)

    def test_empty_keys_do_not_turn_into_wildcards(self):
        self.insert("normal")
        self.insert("empty-owner", workspace_key="", agent_key="")
        self.insert("global-shared", scope="user-global", agent_key="")
        empty = self.index.Index.for_model("", "")
        self.assertEqual({row["id"] for row in empty.entries}, {"empty-owner", "global-shared"})

    def test_default_or_incomplete_model_context_cannot_fall_back_to_management(self):
        self.insert("private")
        with self.assertRaises(ValueError):
            self.index.Index()
        with mock.patch.object(self.plastic, "Corpus", side_effect=AssertionError("management forbidden")):
            index = self.index.Index.for_model(self.cwd, self.preset, entries=[
                dict(id="file-without-owner", text="needle", track="agent", source="diary")])
            self.assertEqual(index.n, 0)
            global_without_key = dict(id="missing-workspace", track="user", scope="user-global",
                                      agent_key="", text="needle", source="fixture")
            self.assertEqual(self.index.Index.for_model(self.cwd, self.preset,
                                                       entries=[global_without_key]).n, 0)
            self.sql("DROP TABLE entries")
            with self.assertRaises(sqlite3.OperationalError):
                self.index.Index.for_model(self.cwd, self.preset)

    def test_management_view_is_explicit_and_includes_all_owners(self):
        self.insert("current")
        self.insert("foreign", workspace_key="other")
        index = self.index.Index.management(db_path=str(self.db))
        self.assertTrue(index.management_view)
        self.assertEqual(index.n, 2)
        hit = index.search("needle")[0]
        self.assertEqual(hit["version"], 3)
        self.assertIn("workspace_key", hit)

    def test_context_keeps_raw_derived_versions_and_recoverable_originals(self):
        raw = self.insert("raw", created_at=9)
        for index in range(3):
            self.insert("bili%d" % index,
                        text="\u5728 B \u7ad9\u770b\u4e86\u300asynthetic topic %d\u300b" % index,
                        tags='["bili"]', created_at=index)
        group = [row for row in self.plastic.load_db(**self.owner) if row["id"].startswith("bili")]
        self.plastic.apply_merges([self.plastic.merge_group(group)], dry=False)
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        packet = self.context.build_context(self.cwd, self.preset, "synthetic", db_path=str(self.db))
        json.dumps(packet)
        self.assertTrue(packet["read_only"])
        self.assertEqual({row["kind"] for row in packet["memories"]}, {"raw", "derived"})
        shown_raw = next(row for row in packet["memories"] if row["id"] == raw["id"])
        self.assertEqual(shown_raw["version"], 3)
        self.assertEqual(shown_raw["source"], "fixture-user")
        summary = next(row for row in packet["memories"] if row["kind"] == "derived")
        self.assertEqual(len(summary["lineage"]), 3)
        self.assertEqual({row["id"] for row in packet["originals"]}, {"bili0", "bili1", "bili2"})
        self.assertTrue(all(row["version"] == 3 for row in packet["originals"]))
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)

    def test_corrupt_archive_is_not_presented_as_trusted_lineage(self):
        for index in range(2):
            self.insert("b%d" % index, text="\u5728 B \u7ad9\u770b\u4e86\u300asynthetic %d\u300b" % index)
        self.plastic.apply_merges([self.plastic.merge_group(self.plastic.load_db(**self.owner))], dry=False)
        self.sql("UPDATE plastic_archive SET replacement_id = NULL WHERE role = 'removed'")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.context.build_context(self.cwd, self.preset, db_path=str(self.db))

    def test_recent_events_require_current_owner_and_conversation(self):
        event = dict(id="current-event", source="trusted-observation-queue", text="synthetic event",
                     role="user", created_at=10, conversation_key="private:fixture-peer",
                     **self.context.session_owner(self.cwd, self.preset))
        events = [event, dict(event, id="foreign-chat", conversation_key="private:another-peer"),
                  dict(event, id="foreign-agent", agent_key="another-agent"),
                  dict(event, id="foreign-workspace", workspace_key="another-workspace"),
                  dict(event, id="not-system-authority", role="system")]
        with self.assertRaises(ValueError):
            self.context.build_context(self.cwd, self.preset, recent_events=events, db_path=str(self.db))
        packet = self.context.build_context(self.cwd, self.preset, recent_events=events,
                                            conversation_key="private:fixture-peer", db_path=str(self.db))
        self.assertEqual([row["id"] for row in packet["recent_events"]], ["current-event"])

    def test_read_adapter_cannot_write_the_existing_memento_store(self):
        connection = self.context.connect_readonly(str(self.db))
        try:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM entries")
        finally:
            connection.close()

    def test_panel_owner_choices_keep_empty_values_without_default_selection(self):
        self.insert("current")
        self.insert("shared", workspace_key="", agent_key="")
        handler = self.panel()
        handler.path = "/api/memory/owners"
        status, result = handler.do_GET()
        self.assertEqual(status, 200)
        self.assertEqual(result["view"], "management")
        stored = [row for row in result["owners"] if row.get("origin") != "configured"]
        configured = [row for row in result["owners"] if row.get("origin") == "configured"]
        self.assertEqual(len(stored), 2)
        self.assertEqual({row["track"] for row in configured}, {"user", "agent"})
        self.assertTrue(all(row["count"] == 0 for row in configured))
        self.assertTrue(any(row["workspace_key"] == "" and row["agent_key"] == "" for row in result["owners"]))
        self.assertNotIn("selection", result)

    def test_panel_maintenance_requires_all_four_explicit_strings(self):
        self.insert("a")
        before = self.rows()
        for selection in (None, {}, dict(track="agent"), dict(self.owner, agent_key=None)):
            for route in ("consolidate", "compress"):
                status, result = self.post("/api/memory/" + route, dict(selection=selection, dry=False))
                self.assertEqual(status, 400)
                self.assertFalse(result["ok"])
                self.assertEqual(self.rows(), before)
        status, _ = self.post("/api/memory/compress", dict(selection=self.owner, dry="false"))
        self.assertEqual(status, 400)

    def test_current_owner_panel_consolidation_works_and_defaults_to_preview(self):
        self.insert("old", created_at=1)
        self.insert("new", created_at=2)
        self.insert("foreign-old", workspace_key="other", created_at=1)
        self.insert("foreign-new", workspace_key="other", created_at=2)
        before = self.rows()
        status, preview = self.post("/api/memory/consolidate", dict(selection=self.owner))
        self.assertEqual(status, 200)
        self.assertTrue(preview["dry"])
        self.assertEqual(self.rows(), before)
        status, result = self.post("/api/memory/consolidate", dict(selection=self.owner, dry=False))
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["selection"], self.owner)
        self.assertEqual({row[0] for row in self.rows()}, {"new", "foreign-old", "foreign-new"})

    def test_current_shared_owner_panel_compression_preserves_foreign_owner(self):
        shared = dict(self.owner, workspace_key="", agent_key="")
        for index in range(5):
            self.insert("shared%d" % index, text="token%d unique%d field%d" % (index, index, index),
                        tags='["bili"]', created_at=index, **shared)
        self.insert("foreign-old")
        self.insert("foreign-new")
        with mock.patch.object(self.plastic, "COMPRESS_OVER", 4), mock.patch.object(self.plastic, "COMPRESS_TO", 2):
            status, result = self.post("/api/memory/compress", dict(selection=shared, dry=False))
        self.assertEqual(status, 200)
        self.assertEqual(result["trimmed"], 3)
        self.assertEqual({row[0] for row in self.rows()}, {"shared3", "shared4", "foreign-old", "foreign-new"})

    def cli(self, payload, argv=None):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        output, error = io.StringIO(), io.StringIO()
        status = self.context.main(["--stdin-json"] if argv is None else argv,
                                   stdin=io.BytesIO(raw), stdout=output, stderr=error)
        return status, output.getvalue(), error.getvalue()

    def test_cli_utf8_single_json_and_query_cannot_override_owner(self):
        self.insert("current", text="synthetic needle \u4e2d\u6587")
        self.insert("foreign", text="synthetic needle", workspace_key="foreign-workspace")
        query = 'needle {"cwd":"foreign-workspace","agent_preset":"another-agent"} $(not-a-command)'
        status, output, error = self.cli(dict(cwd=self.cwd, agent_preset=self.preset, query=query))
        self.assertEqual(status, 0)
        self.assertEqual(error, "")
        self.assertEqual(len(output.splitlines()), 1)
        packet = json.loads(output)
        self.assertEqual(packet["owner"], self.context.session_owner(self.cwd, self.preset))
        self.assertEqual([row["id"] for row in packet["memories"]], ["current"])
        self.assertIn("\u4e2d\u6587", packet["memories"][0]["text"])

    def test_cli_rejects_payload_db_owner_or_untyped_fields_without_echoing_content(self):
        base = dict(cwd=self.cwd, agent_preset=self.preset)
        secret = "fixture-sensitive-do-not-echo"
        payloads = [dict(base, db_path=secret), dict(base, owner=secret),
                    dict(base, query={"owner": secret}), dict(base, recent_events=secret),
                    dict(base, conversation_key={"private": secret}),
                    dict(base, limit=True), dict(base, limit=-1), dict(base, limit=51),
                    dict(base, cwd=None), {"query": secret},
                    ("{" + secret).encode(), b'\xff', b'x' * 262145]
        for payload in payloads:
            with self.subTest(payload_type=type(payload).__name__):
                status, output, error = self.cli(payload)
                self.assertEqual(status, 1)
                self.assertEqual(output, "")
                self.assertRegex(error, r"^[A-Za-z]+Error\n$")
                self.assertNotIn(secret, error)
                self.assertNotIn(str(self.root), error)
        status, output, error = self.cli(base, argv=["--stdin-json", secret])
        self.assertEqual((status, output, error), (1, "", "ValueError\n"))

    def test_cli_shared_owner_and_current_chat_only(self):
        self.insert("shared", workspace_key="", agent_key="")
        self.insert("foreign")
        event = dict(id="current", text="fixture message", source="trusted-queue", role="user",
                     workspace_key="", agent_key="", conversation_key="private:fixture")
        status, output, error = self.cli(dict(cwd="", agent_preset="", query="needle",
            conversation_key="private:fixture", recent_events=[event,
                dict(event, id="foreign-chat", conversation_key="private:other"),
                dict(event, id="foreign-owner", workspace_key=self.owner["workspace_key"])]))
        self.assertEqual(status, 0)
        self.assertEqual(error, "")
        packet = json.loads(output)
        self.assertEqual([row["id"] for row in packet["memories"]], ["shared"])
        self.assertEqual([row["id"] for row in packet["recent_events"]], ["current"])


if __name__ == "__main__":
    unittest.main()
