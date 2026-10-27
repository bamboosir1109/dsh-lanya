"""Terminal observation boundaries; isolated fixture paths, models and sends inert."""
import hashlib
import json
from pathlib import Path
import unittest

import test_main_decision_observers as observer_fixture


class ObserverCompletionTests(unittest.TestCase):
    setUp = observer_fixture.MainDecisionObserverTests.setUp
    patch = observer_fixture.MainDecisionObserverTests.patch
    module = observer_fixture.MainDecisionObserverTests.module
    pending = observer_fixture.MainDecisionObserverTests.pending
    complete = observer_fixture.MainDecisionObserverTests.complete

    def test_unknown_delivery_cannot_use_an_old_review_time_to_retry_unchanged_evidence(self):
        self.mind.one_cycle()
        self.complete(status="unknown", next_review=1_060_000)
        self.assertEqual(self.mind.one_cycle(), 0)
        saved = self.mind.load(self.mind.MIND, {})
        self.assertEqual(saved["last_decision_status"], "unknown")
        self.assertIsNone(saved["decision_next_review_at"])
        self.clock.return_value = 1060
        self.mind.one_cycle()
        self.clock.return_value += 86400
        self.mind.one_cycle()
        self.assertEqual(len(self.pending()), 1)
        # A genuine new inbound fact can still be reviewed; this is not replay.
        self.mind.save(self.mind.HITFILE, [{"who": "fixture", "text": "new external evidence"}])
        self.mind.one_cycle()
        self.assertEqual(len(self.pending()), 2)
        self.ask.assert_not_called(); self.send.assert_not_called()

    def test_invalid_completion_metadata_never_clears_pending_or_interrupts(self):
        self.mind.save(self.mind.HITFILE, [{"who": "fixture", "text": "unacknowledged evidence"}])
        self.mind.one_cycle()
        event = self.pending()[0]
        marker_path = self.data / "decision-inbox" / "done" / event.name
        marker = {"version": 1, "event_sha256": hashlib.sha256(event.read_bytes()).hexdigest(),
                  "status": "decided", "decision_id": "fixture-main"}
        before_mind, before_hits = Path(self.mind.MIND).read_bytes(), Path(self.mind.HITFILE).read_bytes()
        changes = [{"status": "failed"}, {"status": None}, {"decision_id": ""}, {"decision_id": None},
                   {"decision_id": "  "}, {"next_review_at": True}, {"next_review_at": "tomorrow"},
                   {"next_review_at": -1}, {"next_review_at": 1.5}, {"next_review_at": 2**53},
                   {"completed_at": True}, {"completed_at": -1}, {"scope": "private:999"},
                   {"event_id": "wrong-event"}, {"version": True},
                   {"next_review_at": json.loads(event.read_text(encoding="utf-8"))["created_at"]}]
        for change in changes:
            with self.subTest(change=change):
                marker_path.write_text(json.dumps(dict(marker, **change)), encoding="utf-8")
                self.assertEqual(self.mind.one_cycle(), 1)
                self.assertEqual(Path(self.mind.MIND).read_bytes(), before_mind)
                self.assertEqual(Path(self.mind.HITFILE).read_bytes(), before_hits)
                self.assertEqual(len(self.pending()), 1)
        self.ask.assert_not_called(); self.send.assert_not_called()

    def test_missing_decision_or_status_is_not_a_terminal_hash_only_marker(self):
        self.mind.one_cycle()
        event = self.pending()[0]
        marker_path = self.data / "decision-inbox" / "done" / event.name
        marker_path.write_text(json.dumps({"version": 1, "event_sha256": hashlib.sha256(event.read_bytes()).hexdigest()}), encoding="utf-8")
        self.assertEqual(self.mind.one_cycle(), 1)
        self.assertIn("decision_pending", self.mind.load(self.mind.MIND, {}))
        self.assertEqual(len(self.pending()), 1)

    def test_idempotent_publication_rejects_an_invalid_done_marker(self):
        self.mind.one_cycle()
        event = self.pending()[0]
        marker_path = self.data / "decision-inbox" / "done" / event.name
        value = json.loads(event.read_text(encoding="utf-8"))
        marker_path.write_text(json.dumps({"version": 1, "event_sha256": hashlib.sha256(event.read_bytes()).hexdigest(),
                                          "status": "failed", "decision_id": "fixture-main"}), encoding="utf-8")
        import decision_events
        with self.assertRaises(decision_events.ObservationError):
            decision_events.submit_observation(directory=self.data / "decision-inbox", **{
                key: value[key] for key in ("scope", "kind", "payload", "producer", "event_id", "workspace_key", "agent_key")})
        self.assertEqual(len(self.pending()), 1)


if __name__ == "__main__": unittest.main()
