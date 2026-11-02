"""Synthetic SQLite only; no installed plugin, memory or model is accessed."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import memory_metadata as metadata
import memory_mutations as mutations

SCHEMA = """CREATE TABLE entries (id TEXT PRIMARY KEY,track TEXT,scope TEXT,
workspace_key TEXT,agent_key TEXT,text TEXT,source TEXT,tags TEXT DEFAULT '[]',
version INTEGER DEFAULT 1,created_at INTEGER,updated_at INTEGER,session_id TEXT,
last_recalled INTEGER,recall_count INTEGER DEFAULT 0);
CREATE TABLE audit (seq INTEGER PRIMARY KEY,ts INTEGER,action TEXT,track TEXT,scope TEXT,
entry_id TEXT,text TEXT,outcome TEXT,source TEXT,session_id TEXT);"""


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memory-metadata-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "synthetic.sqlite"
        self.connection = sqlite3.connect(self.db)
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.owner = dict(track="agent", scope="workspace", workspace_key="fixture-workspace", agent_key="fixture-agent")
        self.row = dict(id="fixture-fact", **self.owner, text="synthetic original fact", source="fixture",
            tags="[]", version=1, created_at=100, updated_at=200, session_id="fixture-session", last_recalled=None, recall_count=0)
        self.connection.execute("INSERT INTO entries (" + ",".join(self.row) + ") VALUES (" + ",".join("?" for _ in self.row) + ")", tuple(self.row.values()))
        self.connection.commit()

    def annotate(self, data=None):
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            return metadata.write_metadata(self.connection, self.row, data or dict(title="Synthetic topic",kind="event",occurred_at="2025-06-01",importance=60))

    def test_missing_table_reader_is_readonly_and_dates_remain_unknown(self):
        before = self.db.read_bytes()
        self.connection.execute("PRAGMA query_only=ON")
        self.assertIsNone(metadata.read_metadata(self.connection, self.row))
        self.assertEqual(self.db.read_bytes(), before)
        data = metadata.normalize_metadata({"title":"Concrete topic"})
        self.assertIsNone(data["occurred_at"])
        self.assertIsNone(data["confidence"])
        self.assertNotIn("recorded_at", data)
        for invalid in ({"recorded_at":1},{"occurred_at":"yesterday"},{"occurred_at":"2025-02-29"},
                        {"occurred_at":"2025-01-01T12:00:00"},{"importance":True},{"confidence":1.1},
                        {"valid_from":"2025-06-02","valid_until":"2025-06-01"}):
            with self.subTest(invalid=invalid), self.assertRaises(metadata.MemoryMetadataError):
                metadata.normalize_metadata(invalid)

    def test_exact_version_owner_and_fact_bound_but_recall_is_not_a_fact_write(self):
        expected = self.annotate()
        self.assertEqual(metadata.read_metadata(self.connection,self.row),expected)
        self.assertEqual(metadata.read_metadata(self.connection,dict(self.row,recall_count=99,last_recalled=999)),expected)
        for changed in (dict(version=2),dict(text="changed"),dict(agent_key="foreign"),dict(created_at=999)):
            self.assertIsNone(metadata.read_metadata(self.connection,dict(self.row,**changed)))
        self.assertEqual(self.connection.execute("SELECT created_at FROM entries").fetchone()[0],100)

    def test_append_only_guards_reject_replace_update_delete_and_schema_tamper(self):
        self.annotate()
        for sql in ("DELETE FROM memory_metadata_revisions", "UPDATE memory_metadata_revisions SET metadata_json='{}'",
                    "INSERT OR REPLACE INTO memory_metadata_revisions SELECT * FROM memory_metadata_revisions"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.connection.execute(sql)
        self.connection.execute("DROP TRIGGER memory_metadata_delete_refused")
        with self.assertRaises(metadata.MemoryMetadataError):
            metadata.read_metadata(self.connection,self.row)

    def test_cross_owner_or_stale_link_rolls_back_whole_operator_change(self):
        foreign = dict(self.row,id="foreign",agent_key="other-agent")
        self.connection.execute("INSERT INTO entries (" + ",".join(foreign) + ") VALUES (" + ",".join("?" for _ in foreign) + ")", tuple(foreign.values()))
        self.connection.commit()
        for target, version in (("foreign",1),(self.row["id"],99)):
            with self.assertRaises(mutations.MemoryMutationConflict):
                mutations.mutate("edit",dict(selection=self.owner,id=self.row["id"],expected_version=1,text="new",
                    metadata=dict(links=[dict(entry_id=target,version=version,relation="corrects")])),db_path=self.db,apply=True)
        self.assertEqual(dict(self.connection.execute("SELECT * FROM entries WHERE id=?",(self.row["id"],)).fetchone()),self.row)
        self.assertIsNone(metadata.read_metadata(self.connection,self.row))

    def test_operator_edit_preserves_event_time_and_history_restore_does_not_reuse_annotation(self):
        original = self.annotate()
        preview = mutations.mutate("edit",dict(selection=self.owner,id=self.row["id"],expected_version=1,text="corrected",
            metadata=dict(confidence=40,status="uncertain")),db_path=self.db,apply=False)
        self.assertEqual(preview["metadata"]["occurred_at"],original["occurred_at"])
        changed = mutations.mutate("edit",dict(selection=self.owner,id=self.row["id"],expected_version=1,text="corrected",
            metadata=dict(confidence=40,status="uncertain")),db_path=self.db,apply=True)
        new = changed["entry"]
        self.assertEqual(new["created_at"],100)
        current = metadata.read_metadata(self.connection,new)
        self.assertEqual(current["occurred_at"],"2025-06-01")
        self.assertIn(dict(entry_id=self.row["id"],version=1,relation="updates"),current["links"])
        self.assertEqual(metadata.read_metadata(self.connection,self.row),original)
        plan = mutations.restore_change(changed["change_id"],self.owner,db_path=self.db)
        restored = mutations.restore_change(changed["change_id"],self.owner,db_path=self.db,apply=True,expected_fingerprint=plan["fingerprint"])
        fact = dict(self.connection.execute("SELECT * FROM entries WHERE id=?",(self.row["id"],)).fetchone())
        self.assertEqual(fact["text"],self.row["text"])
        self.assertIsNone(metadata.read_metadata(self.connection,fact))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM memory_metadata_revisions").fetchone()[0],2)

    def test_clear_fields_unknown_is_explicit_and_metadata_input_is_copied(self):
        old = metadata.normalize_metadata(dict(title="Topic",occurred_at="2025-06-01",confidence=90))
        request = dict(clear_fields=["confidence","occurred_at"],kind="reflection")
        result = metadata.normalize_metadata(request,old)
        self.assertIsNone(result["confidence"])
        self.assertIsNone(result["occurred_at"])
        self.assertEqual(result["title"],"Topic")
        self.assertNotIn("clear_fields",result)
        with self.assertRaises(metadata.MemoryMetadataError):
            metadata.normalize_metadata(dict(clear_fields=["confidence"],confidence=80),old)


if __name__ == "__main__":
    unittest.main()
