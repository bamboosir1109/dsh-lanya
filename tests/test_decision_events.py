"""Immutable observation evidence; temporary files, no model/QQ/config reads."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "decision_events.py"
spec = importlib.util.spec_from_file_location("decision_events_fixture", SOURCE)
events = importlib.util.module_from_spec(spec)
spec.loader.exec_module(events)


class DecisionEventsTests(unittest.TestCase):
    def setUp(self):
        disposable = tempfile.TemporaryDirectory()
        self.addCleanup(disposable.cleanup)
        self.directory = Path(disposable.name) / "inbox"
        self.request = dict(scope="group:101", event_id="quiet:one", kind="quiet",
                            producer="fixture", payload={"text": "合适才说话"},
                            workspace_key="workspace-a", agent_key="preset-a",
                            directory=self.directory, created_at=1)

    def submit(self, **changes):
        return events.submit_observation(**dict(self.request, **changes))

    def test_immutable_retry_does_not_duplicate_or_replace_evidence(self):
        first = self.submit()
        target = self.directory / "pending" / (first["key"] + ".json")
        before = target.read_bytes()
        second = self.submit(created_at=2)
        self.assertTrue(first["submitted"])
        self.assertFalse(second["submitted"])
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(json.loads(before)["payload"], self.request["payload"])

    def test_changed_payload_with_same_identity_is_a_conflict(self):
        first = self.submit()
        target = self.directory / "pending" / (first["key"] + ".json")
        before = target.read_bytes()
        with self.assertRaises(events.ObservationError):
            self.submit(payload={"text": "different evidence"})
        self.assertEqual(target.read_bytes(), before)

    def test_completion_is_bound_to_physical_utf8_evidence_hash(self):
        first = self.submit()
        event = self.directory / "pending" / (first["key"] + ".json")
        done = self.directory / "done" / event.name
        marker = {"version": 1, "event_sha256": hashlib.sha256(event.read_bytes()).hexdigest(),
                  "status": "decided", "decision_id": "fixture-primary"}
        done.write_text(json.dumps(marker), encoding="utf-8")
        self.assertEqual(self.submit()["state"], "done")
        marker["event_sha256"] = "incorrect"
        done.write_text(json.dumps(marker), encoding="utf-8")
        with self.assertRaises(events.ObservationError):
            self.submit()

    def test_parallel_publish_has_one_immutable_winner(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.submit(), range(16)))
        self.assertEqual(sum(x["submitted"] for x in results), 1)
        self.assertEqual(len(list((self.directory / "pending").iterdir())), 1)

    def test_identity_is_hashed_and_never_used_as_a_path(self):
        first = self.submit(event_id="../../outside.json")
        files = list(self.directory.rglob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, first["key"] + ".json")
        self.assertTrue(files[0].resolve().is_relative_to(self.directory.resolve()))

    def test_invalid_control_owner_and_nonfinite_payload_refused_before_files(self):
        first = dict(self.request)
        first.pop("directory")
        for bad in ({"scope": "group:0101"}, {"scope": 101}, {"workspace_key": None},
                    {"agent_key": None}, {"created_at": True}, {"payload": {"n": float("nan")}},
                    {"kind": "invalid\nkind"}):
            with self.subTest(bad=bad), self.assertRaises((events.ObservationError, ValueError)):
                self.submit(**bad)
        event = dict(first, version=1, origin="live", control_allowed=True)
        with self.assertRaises(events.ObservationError):
            events.validate_event(event)
        self.assertFalse(self.directory.exists())

    def test_payload_size_is_bounded_and_file_parent_is_refused(self):
        with self.assertRaises(events.ObservationError):
            self.submit(payload={"text": "x" * (256 * 1024)})
        self.directory.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(events.ObservationError):
            self.submit()

    def test_interrupted_publish_keeps_no_partial_event_and_retry_succeeds(self):
        operation = "rename" if os.name == "nt" else "link"
        with mock.patch.object(events.os, operation, side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                self.submit()
        self.assertEqual(list((self.directory / "pending").iterdir()), [])
        self.assertTrue(self.submit()["submitted"])

    def test_shared_empty_keys_are_explicit_not_missing(self):
        self.assertTrue(self.submit(workspace_key="", agent_key="")["submitted"])


if __name__ == "__main__":
    unittest.main()
