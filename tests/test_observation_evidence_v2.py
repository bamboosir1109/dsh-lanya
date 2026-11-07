"""v1/v2 completion originals: isolated raw bytes, no real configuration or account."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/decision_events.py"
spec = importlib.util.spec_from_file_location("evidence_v2_fixture", SOURCE)
events = importlib.util.module_from_spec(spec)
spec.loader.exec_module(events)


class OriginalEvidenceTests(unittest.TestCase):
    def setUp(self):
        disposable = tempfile.TemporaryDirectory(prefix="lanya-original-v2-")
        self.addCleanup(disposable.cleanup)
        self.root = Path(disposable.name)
        for name in ("pending", "done"):
            (self.root / name).mkdir()
        self.event = dict(version=1, event_id="one", scope="private:101", kind="activity.opportunity",
                          producer="mind", created_at=1000, payload={"text": "原始中文 🐺"},
                          workspace_key="fixture-workspace", agent_key="fixture-agent",
                          origin="observation", control_allowed=False)

    def write(self, event=None, *, status="decided", next_review=None, version=2):
        event = event or self.event
        # Match JS order/UTF8/LF rather than reserializing into Python canonical form.
        raw = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        key = events.event_key(event["scope"], event["event_id"])
        pending = self.root / "pending" / (key + ".json")
        done = self.root / "done" / (key + ".json")
        pending.write_bytes(raw)
        marker = dict(version=version, event_id=event["event_id"], scope=event["scope"],
                      event_sha256=hashlib.sha256(raw).hexdigest(), status=status,
                      decision_id="fixture-decision", completed_at=2000)
        if version == 2:
            marker["event_raw"] = raw.decode("utf-8")
        if next_review is not None:
            marker["next_review_at"] = next_review
        done.write_text(json.dumps(marker), encoding="utf-8")
        return pending, done, marker, raw

    def retry(self, **changes):
        keys = ("scope", "kind", "payload", "producer", "event_id", "workspace_key", "agent_key", "created_at")
        return events.submit_observation(directory=self.root, **dict({key: self.event[key] for key in keys}, **changes))

    def test_deleted_v2_original_returns_terminal_and_idempotent_submit_without_republish(self):
        pending, done, marker, raw = self.write()
        pending.unlink()
        before = done.read_bytes()
        self.assertEqual(events.observation_completion(self.event["scope"], "one", directory=self.root), marker)
        self.assertEqual(self.retry(created_at=5000)["state"], "done")
        self.assertFalse(pending.exists())
        self.assertEqual(done.read_bytes(), before)
        with self.assertRaises(events.ObservationError):
            self.retry(agent_key="foreign")

    def test_review_chain_follows_completed_v2_child_when_both_pending_copies_deleted(self):
        pending, _, marker, _ = self.write(next_review=3000)
        key = events.event_key(self.event["scope"], self.event["event_id"])
        child = dict(self.event, event_id="main-review:" + key + ":3000", producer="main-review", created_at=3000,
                     payload=dict(self.event["payload"], review_context=dict(parent_observation_key=key,
                                  parent_event_id="one", previous_decision_id=marker["decision_id"], scheduled_at=3000)))
        child_pending, _, _, _ = self.write(child)
        pending.unlink(); child_pending.unlink()
        chain = events.observation_review_chain(self.event["scope"], "one", directory=self.root)
        self.assertEqual(chain["event"], child)
        self.assertEqual(chain["completion"]["status"], "decided")
        self.assertFalse(chain["waiting_review"])

    def test_unknown_never_follows_or_publishes_a_review_after_original_deletion(self):
        pending, _, _, _ = self.write(status="unknown", next_review=3000)
        pending.unlink()
        result = events.observation_review_chain(self.event["scope"], "one", directory=self.root)
        self.assertEqual(result["completion"]["status"], "unknown")
        self.assertFalse(result["waiting_review"])
        self.assertEqual(self.retry()["state"], "done")
        self.assertFalse(list((self.root / "pending").iterdir()))

    def test_present_corrupt_pending_is_not_hidden_by_valid_done_copy(self):
        pending, _, _, _ = self.write()
        for raw in (b"{", b"\xff", b"null"):
            with self.subTest(raw=raw):
                pending.write_bytes(raw)
                with self.assertRaises((events.ObservationError, UnicodeError, ValueError)):
                    events.observation_completion(self.event["scope"], "one", directory=self.root)
                with self.assertRaises((events.ObservationError, UnicodeError, ValueError)):
                    self.retry()

    def test_invalid_v2_raw_hash_identity_and_null_marker_refused(self):
        pending, done, marker, _ = self.write()
        pending.unlink()
        for change in ({"event_sha256": "0" * 64}, {"event_raw": marker["event_raw"] + " "},
                       {"event_id": "foreign"}, {"scope": "group:101"}, {"status": "failed"},
                       {"event_raw": "\ud800"}, {"version": True}):
            with self.subTest(change=change):
                done.write_text(json.dumps(dict(marker, **change)), encoding="utf-8")
                with self.assertRaises((events.ObservationError, ValueError)):
                    self.retry()
        pending, done, _, _ = self.write()
        done.write_text("null", encoding="utf-8")
        with self.assertRaises(events.ObservationError):
            self.retry()

    def test_v1_requires_its_original_and_does_not_invent_a_v2_backup(self):
        pending, done, marker, _ = self.write(version=1)
        self.assertEqual(self.retry()["state"], "done")
        pending.unlink()
        with self.assertRaises(events.ObservationError):
            self.retry()
        self.assertEqual(json.loads(done.read_text()), marker)

    def test_physical_event_boundary_includes_newline(self):
        # Own Python producer uses canonical ASCII; exactly MAX bytes is readable.
        request = {key: self.event[key] for key in ("scope", "kind", "payload", "producer", "event_id", "workspace_key", "agent_key", "created_at")}
        empty = dict(self.event, payload={"text": ""})
        overhead = len((events.canonical_json(empty) + "\n").encode())
        text = "x" * (events.MAX_OBSERVATION_BYTES - overhead)
        result = events.submit_observation(directory=self.root, **dict(request, payload={"text": text}))
        path = self.root / "pending" / (result["key"] + ".json")
        self.assertEqual(path.stat().st_size, events.MAX_OBSERVATION_BYTES)
        self.assertEqual(events._read_evidence(self.root, result["key"])[0]["payload"]["text"], text)
        with self.assertRaises(events.ObservationError):
            events.submit_observation(directory=self.root, **dict(request, payload={"text": text + "x"}))


if __name__ == "__main__": unittest.main()
