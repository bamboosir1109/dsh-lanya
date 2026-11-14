"""Shanghai rest/expiry producer and cross-language evidence; synthetic data only."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import decision_events as events

MIDNIGHT = 1793721600 * 1000  # 2026-11-03 16:00 UTC, Shanghai midnight.
WAKE = MIDNIGHT + 8 * 60 * 60 * 1000


class AutonomyRestTests(unittest.TestCase):
    def load_mind(self):
        temporary = tempfile.TemporaryDirectory(prefix="rest-mind-fixture-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        project = root / "project.json"
        project.write_text(json.dumps({"dataDirName": "character", "decisions": {"mode": "unified"}}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"LANYA_WORKDIR": str(root), "LANYA_PROJECT_CONFIG": str(project)}):
            spec = importlib.util.spec_from_file_location("rest_mind_fixture", SCRIPTS / "mind.py")
            mind = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mind)
        return mind

    def test_real_js_and_python_clock_contract_agree_at_boundaries(self):
        samples = [MIDNIGHT - 1, MIDNIGHT, WAKE - 1, WAKE, WAKE + 1]
        module = (SCRIPTS.parent / "plugin/lib/decision-policy.js").as_uri()
        code = "import {autonomousRestState} from " + json.dumps(module) + ";console.log(JSON.stringify(" + json.dumps(samples) + ".map(autonomousRestState)))"
        run = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), [events.autonomous_rest_state(value) for value in samples])
        self.assertEqual([events.autonomous_rest_state(value)["resting"] for value in samples], [False, True, True, False, False])
        for bad in (True, -1, 1.5, float("nan"), 2**53):
            with self.assertRaises(events.ObservationError):
                events.autonomous_rest_state(bad)

    def test_night_force_cannot_submit_or_mutate_observer_state(self):
        mind = self.load_mind()
        value = {"plan": "synthetic plan", "decision_scopes": {"untrusted": "kept untouched"}}
        before = copy.deepcopy(value)
        with mock.patch.object(events, "project_scopes", return_value=["private:101"]), \
             mock.patch.object(mind, "get", return_value=2), \
             mock.patch.object(mind.time, "time", return_value=MIDNIGHT / 1000), \
             mock.patch.object(mind, "_observer_states", side_effect=AssertionError("state mutation forbidden")), \
             mock.patch.object(mind, "_read_observer_hits", side_effect=AssertionError("data read forbidden")), \
             mock.patch.object(events, "submit_project_observation", side_effect=AssertionError("submit forbidden")), \
             mock.patch.object(mind, "save", side_effect=AssertionError("write forbidden")), \
             mock.patch.object(mind, "log"):
            self.assertEqual(mind._observe_cycle(value, force=True), 0)
        self.assertEqual(value, before)

    def test_rest_local_completion_accepts_all_observation_kinds_but_not_forged_decisions(self):
        for reason in ("autonomy_rest", "observation_expired"):
            for kind in ("reminder.due", "social.notification", "entertainment.review", "activity.opportunity"):
                event = dict(version=1, event_id="synthetic-event", scope="private:101", kind=kind, producer="fixture",
                             created_at=MIDNIGHT - 1, payload={}, workspace_key="fixture-workspace", agent_key="fixture-agent",
                             origin="observation", control_allowed=False)
                raw = (events.canonical_json(event) + "\n").encode()
                outcome_id = hashlib.sha256("\0".join(["no-decision", event["scope"], event["event_id"], "1"]).encode()).hexdigest()
                marker = dict(version=3, event_raw=raw.decode(), event_id=event["event_id"], scope=event["scope"],
                              event_sha256=hashlib.sha256(raw).hexdigest(), completed_at=MIDNIGHT, status="no_decision",
                              reason=reason, local_outcome_id=outcome_id, observation_revision=1, native_turn_completed=False)
                self.assertEqual(events._validated_completion(marker, event, raw)["reason"], reason)
                for change in ({"native_turn_completed": True}, {"decision_id": "fabricated"}, {"next_review_at": WAKE}):
                    with self.assertRaises(events.ObservationError):
                        events._validated_completion({**marker, **change}, event, raw)
                with self.assertRaises(events.ObservationError):
                    events._validated_completion({**marker, "reason": "native_admission_refused"}, event, raw)

    def test_wake_creates_one_fresh_generation_not_a_batch_of_old_greetings(self):
        for reason, completed_at in [("autonomy_rest", MIDNIGHT), ("observation_expired", WAKE)]:
            with self.subTest(reason=reason):
                self.check_wake_generation(reason, completed_at)

    def check_wake_generation(self, reason, completed_at):
        mind = self.load_mind()
        old_event = dict(event_id="old-night-event", scope="private:101", created_at=MIDNIGHT - 1)
        value = {"decision_observers_version": 2, "decision_scopes": {"private:101": {
            "generation": 8, "pending": {"scope": "private:101", "event_id": old_event["event_id"],
                "signature": "a" * 64, "interrupts": []}}}}
        published = []
        def chain(_scope, event_id):
            if event_id == old_event["event_id"]:
                return {"event": old_event, "completion": {"status": "no_decision", "reason": reason,
                    "native_turn_completed": False, "completed_at": completed_at}, "waiting_review": False}
            return {"event": {"event_id": event_id}, "completion": None, "waiting_review": False}
        with mock.patch.object(events, "project_scopes", return_value=["private:101"]), \
             mock.patch.object(events, "observation_review_chain", side_effect=chain), \
             mock.patch.object(events, "submit_project_observation", side_effect=lambda **row: published.append(row)), \
             mock.patch.object(mind, "get", return_value=2), mock.patch.object(mind.time, "time", return_value=WAKE / 1000), \
             mock.patch.object(mind, "_read_observer_hits", return_value=[]), \
             mock.patch.object(mind, "_acknowledge_scope_hits"), mock.patch.object(mind, "_diary_hint", return_value=None), \
             mock.patch.object(mind, "save"), mock.patch.object(mind, "log"):
            self.assertEqual(mind._observe_cycle(value), 0)
            self.assertEqual(mind._observe_cycle(value), 0)
        self.assertEqual(len(published), 1)
        self.assertNotEqual(published[0]["event_id"], old_event["event_id"])
        self.assertEqual(value["decision_scopes"]["private:101"]["generation"], 9)
        self.assertEqual(published[0]["payload"]["prior_rest_lifecycle"]["previous_activity_status"], "unknown")
        self.assertTrue(published[0]["payload"]["prior_rest_lifecycle"]["old_actions_not_replayed"])


if __name__ == "__main__":
    unittest.main()
