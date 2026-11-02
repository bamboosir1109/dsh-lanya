"""Native-completed/no-decision evidence: temporary files, no account or model."""
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
QUEUE = SCRIPTS.parent / "plugin/lib/observation-queue.js"
spec = importlib.util.spec_from_file_location("no_decision_events_fixture", SCRIPTS / "decision_events.py")
events = importlib.util.module_from_spec(spec)
spec.loader.exec_module(events)


def local_outcome(event, revision):
    text = "no-decision\0" + event["scope"] + "\0" + event["event_id"] + "\0" + str(revision)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def marker_for(event, raw, completed_at=2000, revision=1):
    return dict(version=3, event_raw=raw.decode("utf-8"), event_id=event["event_id"], scope=event["scope"],
                event_sha256=hashlib.sha256(raw).hexdigest(), completed_at=completed_at, status="no_decision",
                reason="model_completed_without_decision", local_outcome_id=local_outcome(event, revision),
                observation_revision=revision, native_turn_completed=True)


class NoDecisionEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="no-decision-evidence-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("pending", "done"):
            (self.root / name).mkdir()
        self.event = dict(version=1, event_id="opportunity:one", scope="private:101", kind="activity.opportunity",
                          producer="mind", created_at=1000, payload={"text": "synthetic opportunity"},
                          workspace_key="fixture-workspace", agent_key="fixture-agent", origin="observation",
                          control_allowed=False)

    def write(self, event=None, marker=None):
        event = self.event if event is None else event
        raw = (events.canonical_json(event) + "\n").encode("utf-8")
        key = events.event_key(event["scope"], event["event_id"])
        pending, done = (self.root / name / (key + ".json") for name in ("pending", "done"))
        pending.write_bytes(raw)
        marker = marker_for(event, raw) if marker is None else marker
        done.write_text(json.dumps(marker), encoding="utf-8")
        return pending, done, marker, raw

    def completion(self, event=None):
        event = self.event if event is None else event
        return events.observation_completion(event["scope"], event["event_id"], directory=self.root)

    def submit(self, **changes):
        keys = ("scope", "kind", "payload", "producer", "event_id", "workspace_key", "agent_key", "created_at")
        return events.submit_observation(directory=self.root,
                                         **dict({key: self.event[key] for key in keys}, **changes))

    def test_native_terminal_is_not_a_decision_or_review_and_retry_is_immutable(self):
        pending, done, marker, raw = self.write()
        before = done.read_bytes()
        self.assertEqual(self.completion(), marker)
        self.assertEqual(self.submit(created_at=9000)["state"], "done")
        chain = events.observation_review_chain(self.event["scope"], self.event["event_id"], directory=self.root)
        self.assertFalse(chain["waiting_review"])
        self.assertEqual(chain["completion"]["status"], "no_decision")
        self.assertNotIn("decision_id", chain["completion"])
        self.assertNotIn("next_review_at", chain["completion"])
        self.assertEqual(done.read_bytes(), before)
        self.assertEqual(pending.read_bytes(), raw)
        self.assertEqual(len(list((self.root / "pending").iterdir())), 1)

    def test_missing_pending_uses_exact_v3_original_without_republishing(self):
        pending, done, marker, _ = self.write()
        pending.unlink()
        before = done.read_bytes()
        self.assertEqual(self.completion(), marker)
        self.assertEqual(self.submit()["state"], "done")
        self.assertFalse(pending.exists())
        self.assertEqual(done.read_bytes(), before)
        with self.assertRaises(events.ObservationError):
            self.submit(agent_key="foreign-agent")

    def test_zero_step_admission_refusal_is_a_distinct_local_terminal_without_model_completion(self):
        pending, done, marker, raw = self.write()
        marker.update(reason="native_admission_refused", native_turn_completed=False)
        done.write_text(json.dumps(marker), encoding="utf-8")
        before = done.read_bytes()
        self.assertEqual(self.completion(), marker)
        self.assertEqual(self.submit(created_at=9000)["state"], "done")
        chain = events.observation_review_chain(self.event["scope"], self.event["event_id"], directory=self.root)
        self.assertFalse(chain["waiting_review"])
        self.assertFalse(chain["completion"]["native_turn_completed"])
        self.assertEqual(chain["completion"]["reason"], "native_admission_refused")
        self.assertNotIn("decision_id", chain["completion"])
        self.assertNotIn("next_review_at", chain["completion"])
        self.assertEqual(done.read_bytes(), before)
        self.assertEqual(pending.read_bytes(), raw)
        pending.unlink()
        self.assertEqual(self.completion(), marker)

    def test_admission_refusal_rejects_mismatched_reason_completion_and_additional_authority(self):
        _, done, marker, _ = self.write()
        marker.update(reason="native_admission_refused", native_turn_completed=False)
        for change in ({"native_turn_completed": True}, {"native_turn_completed": 0},
                       {"reason": "model_completed_without_decision"}, {"reason": "budget_exhausted"},
                       {"reason": "turn_in_flight"}, {"native_model_steps": 0},
                       {"native_admission_reason": "previous_turn_unresolved"},
                       {"verdict": "idle"}, {"decision_id": "invented"}, {"next_review_at": 3000}):
            with self.subTest(change=change):
                done.write_text(json.dumps(dict(marker, **change)), encoding="utf-8")
                with self.assertRaises(events.ObservationError):
                    self.completion()

    def test_present_corrupt_pending_cannot_be_hidden_by_completed_original(self):
        pending, _, _, _ = self.write()
        for raw in (b"{", b"null", b"\xff"):
            with self.subTest(raw=raw):
                pending.write_bytes(raw)
                with self.assertRaises((events.ObservationError, ValueError, UnicodeError)):
                    self.completion()
                with self.assertRaises((events.ObservationError, ValueError, UnicodeError)):
                    self.submit()

    def test_no_decision_requires_exact_fields_without_authority_or_llm_claims(self):
        _, done, marker, _ = self.write()
        for key in ("decision_id", "next_review_at", "verdict", "ticket", "send_permission"):
            with self.subTest(extra=key):
                done.write_text(json.dumps(dict(marker, **{key: "forged"})), encoding="utf-8")
                with self.assertRaises(events.ObservationError):
                    self.completion()
        for key in marker:
            with self.subTest(missing=key):
                value = dict(marker)
                del value[key]
                done.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(events.ObservationError):
                    self.completion()

    def test_invalid_types_ids_revisions_times_and_status_are_rejected(self):
        _, done, marker, _ = self.write()
        changes = ({"version": True}, {"version": 2}, {"observation_revision": True},
                   {"observation_revision": 0}, {"observation_revision": 2**53},
                   {"native_turn_completed": 1}, {"native_turn_completed": False},
                   {"completed_at": True}, {"completed_at": 999}, {"completed_at": 2**53},
                   {"status": "decided"}, {"reason": "cancelled"},
                   {"local_outcome_id": "0" * 64}, {"scope": "group:101"},
                   {"event_id": "foreign"}, {"event_sha256": "0" * 64},
                   {"event_raw": marker["event_raw"] + " "})
        for change in changes:
            with self.subTest(change=change):
                done.write_text(json.dumps(dict(marker, **change)), encoding="utf-8")
                with self.assertRaises(events.ObservationError):
                    self.completion()

    def test_only_mind_opportunities_and_main_reviews_accept_this_terminal(self):
        for change in ({"kind": "reminder.due"}, {"producer": "other-observer"},
                       {"kind": "activity_due"}, {"kind": "quiet"}):
            with self.subTest(change=change):
                event = dict(self.event, **change)
                self.write(event)
                with self.assertRaises(events.ObservationError):
                    self.completion(event)
        event = dict(self.event, producer="main-review")
        self.write(event)
        self.assertEqual(self.completion(event)["status"], "no_decision")

    def test_revision_and_completion_boundary_values_are_exact_safe_integers(self):
        pending, done, _, raw = self.write()
        for revision in (1, 2**53 - 1):
            marker = marker_for(self.event, raw, completed_at=self.event["created_at"], revision=revision)
            done.write_text(json.dumps(marker), encoding="utf-8")
            self.assertEqual(self.completion(), marker)
        self.assertTrue(pending.is_file())

    def test_v1_and_v2_decisions_remain_compatible_but_cannot_claim_no_decision(self):
        _, done, _, raw = self.write()
        for version in (1, 2):
            marker = dict(version=version, event_sha256=hashlib.sha256(raw).hexdigest(),
                          status="decided", decision_id="synthetic-llm-decision", completed_at=2000)
            if version == 2:
                marker.update(event_raw=raw.decode(), event_id=self.event["event_id"], scope=self.event["scope"])
            done.write_text(json.dumps(marker), encoding="utf-8")
            self.assertEqual(self.completion(), marker)
            marker["status"] = "no_decision"
            done.write_text(json.dumps(marker), encoding="utf-8")
            with self.assertRaises(events.ObservationError):
                self.completion()

    def test_existing_review_chain_stops_at_native_completed_child(self):
        pending, done, _, raw = self.write()
        parent = dict(version=2, event_raw=raw.decode(), event_id=self.event["event_id"], scope=self.event["scope"],
                      event_sha256=hashlib.sha256(raw).hexdigest(), status="decided", decision_id="parent-decision",
                      completed_at=2000, next_review_at=3000)
        done.write_text(json.dumps(parent), encoding="utf-8")
        key = events.event_key(self.event["scope"], self.event["event_id"])
        child = dict(self.event, event_id="main-review:" + key + ":3000", producer="main-review", created_at=3000,
                     payload=dict(self.event["payload"], review_context=dict(parent_observation_key=key,
                                  parent_event_id=self.event["event_id"], previous_decision_id="parent-decision",
                                  scheduled_at=3000)))
        child_pending, child_done, _, child_raw = self.write(child)
        child_done.write_text(json.dumps(marker_for(child, child_raw, completed_at=4000)), encoding="utf-8")
        pending.unlink()
        child_pending.unlink()
        chain = events.observation_review_chain(self.event["scope"], self.event["event_id"], directory=self.root)
        self.assertEqual(chain["event"], child)
        self.assertEqual(chain["completion"]["status"], "no_decision")
        self.assertFalse(chain["waiting_review"])
        self.assertFalse(list((self.root / "pending").iterdir()))

    def test_actual_node_pump_writes_a_python_readable_terminal_without_replay(self):
        self.event["payload"] = {"text": "synthetic 中文 🌙"}
        submitted = self.submit()
        pending = self.root / "pending" / (submitted["key"] + ".json")
        # Native JS producers preserve literal UTF8 and input key order.
        pending.write_bytes((json.dumps(self.event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        original = pending.read_bytes()
        script = r"""
import fs from 'node:fs'; import path from 'node:path'; import {createHash} from 'node:crypto';
const {createObservationPump} = await import(process.argv[1]);
const directory=process.argv[2]; let calls=0, acknowledgements=0;
const pump=createObservationPump({directory,now:()=>2000,dispatch:async event=>{
  calls++;
  return {completed:true,status:'no_decision',reason:'model_completed_without_decision',
    local_outcome_id:createHash('sha256').update(['no-decision',event.scope,event.event_id,1].join('\0')).digest('hex'),
    observation_revision:1,native_turn_completed:true,completed_at:2000};
},acknowledge:async()=>{acknowledgements++}});
try {
  if(!await pump.start({poll:false}))throw Error('fixture ownership unavailable');
  await pump.drain();
  for(const name of fs.readdirSync(path.join(directory,'pending')))fs.unlinkSync(path.join(directory,'pending',name));
  await pump.drain();
  console.log(JSON.stringify({calls,acknowledgements}));
} finally {pump.stop()}
"""
        run = subprocess.run(["node", "--input-type=module", "-e", script, QUEUE.as_uri(), str(self.root)],
                             capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        # The same pump caches an exact successful acknowledgement; evidence
        # fallback remains readable without repeating policy I/O or dispatch.
        self.assertEqual(json.loads(run.stdout), {"calls": 1, "acknowledgements": 1})
        marker = self.completion()
        self.assertEqual(marker["version"], 3)
        self.assertEqual(marker["event_raw"].encode("utf-8"), original)
        self.assertEqual(marker["local_outcome_id"], local_outcome(self.event, 1))
        self.assertEqual(self.submit()["state"], "done")
        self.assertFalse(pending.exists())


class MindNoDecisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mind-no-decision-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = self.root / "project.json"
        config.write_text(json.dumps({"dataDirName": "character", "decisions": {"mode": "unified",
                                          "agentPreset": "fixture-agent"}, "mind": {"opportunityMin": 3}}), encoding="utf-8")
        environment = mock.patch.dict(os.environ, {"LANYA_PROJECT_CONFIG": str(config), "LANYA_WORKDIR": str(self.root)})
        environment.start(); self.addCleanup(environment.stop)
        path = mock.patch.object(sys, "path", [str(SCRIPTS)] + sys.path)
        path.start(); self.addCleanup(path.stop)
        import project_conf
        project_conf.load(reload=True)
        self.addCleanup(setattr, project_conf, "_CACHE", None)
        spec = importlib.util.spec_from_file_location("mind_no_decision_fixture", SCRIPTS / "mind.py")
        self.mind = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mind)
        modules = mock.patch.dict(sys.modules, {"decision_events": events})
        modules.start(); self.addCleanup(modules.stop)
        self.scopes = ["private:101", "group:202"]
        for target, name, kwargs in ((events, "project_scopes", {"return_value": self.scopes}),
                                     (self.mind, "get", {"side_effect": lambda key, default=None: 3 if key == "mind.opportunityMin" else default}),
                                     (self.mind, "log", {}),
                                     (self.mind, "ask", {"side_effect": AssertionError("LLM forbidden")}),
                                     (self.mind, "deliver", {"side_effect": AssertionError("QQ forbidden")})):
            patch = mock.patch.object(target, name, **kwargs)
            patch.start(); self.addCleanup(patch.stop)
        clock = mock.patch.object(self.mind.time, "time", return_value=1000.0)
        self.clock = clock.start(); self.addCleanup(clock.stop)
        self.inbox = Path(self.mind.DATA) / "decision-inbox"
        submit = mock.patch.object(events, "submit_project_observation", side_effect=self.publish)
        self.submit = submit.start(); self.addCleanup(submit.stop)

    def publish(self, **kwargs):
        return events.submit_observation(**kwargs, directory=self.inbox,
                                         workspace_key="fixture-workspace", agent_key="fixture-agent")

    def state(self):
        return json.loads(Path(self.mind.MIND).read_text(encoding="utf-8"))

    def finish(self, scope, *, unknown=False, admission_refused=False, preserve_pending=False):
        state = self.state()["decision_scopes"][scope]
        identity = state["pending"]["event_id"]
        key = events.event_key(scope, identity)
        pending = self.inbox / "pending" / (key + ".json")
        raw = pending.read_bytes(); event = json.loads(raw)
        marker = marker_for(event, raw, completed_at=1001000)
        if admission_refused:
            marker.update(reason="native_admission_refused", native_turn_completed=False)
        if unknown:
            marker = dict(version=2, event_raw=raw.decode(), scope=scope, event_id=identity,
                          event_sha256=hashlib.sha256(raw).hexdigest(), completed_at=1001000,
                          status="unknown", decision_id="uncertain-send")
        done = self.inbox / "done" / (key + ".json")
        done.write_text(json.dumps(marker), encoding="utf-8")
        if not preserve_pending:
            pending.unlink()
        return identity, done.read_bytes()

    def test_native_completion_clears_once_then_offers_only_next_normal_generation(self):
        self.assertEqual(self.mind.one_cycle(), 0)
        primary, original = self.finish(self.scopes[0])
        self.finish(self.scopes[1], unknown=True)
        self.clock.return_value = 1002
        self.assertEqual(self.mind.one_cycle(), 0)
        state = self.state()["decision_scopes"][self.scopes[0]]
        self.assertEqual(state["last_status"], "no_decision")
        self.assertEqual(state["generation"], 1)
        self.assertNotIn("pending", state)
        self.assertIsNone(state["next_review_at"])
        self.assertEqual(state["next_opportunity_at"], 1181000)
        self.assertEqual(self.submit.call_count, 2)
        self.clock.return_value = 1180.999
        self.assertEqual(self.mind.one_cycle(), 0)
        self.assertEqual(self.submit.call_count, 2)
        self.clock.return_value = 1181
        self.assertEqual(self.mind.one_cycle(), 0)
        current = self.state()["decision_scopes"]
        self.assertEqual(current[self.scopes[0]]["generation"], 2)
        self.assertNotEqual(current[self.scopes[0]]["pending"]["event_id"], primary)
        self.assertEqual(current[self.scopes[1]]["last_status"], "unknown")
        self.assertIsNone(current[self.scopes[1]]["next_opportunity_at"])
        self.assertEqual(self.submit.call_count, 3)
        self.assertEqual(self.mind.one_cycle(), 0)
        self.assertEqual(self.submit.call_count, 3)
        key = events.event_key(self.scopes[0], primary)
        self.assertEqual((self.inbox / "done" / (key + ".json")).read_bytes(), original)
        self.assertFalse((self.inbox / "pending" / (key + ".json")).exists())

    def test_bad_native_completion_blocks_only_that_scope_without_replay(self):
        self.assertEqual(self.mind.one_cycle(), 0)
        identity, _ = self.finish(self.scopes[0])
        self.finish(self.scopes[1])
        marker = self.inbox / "done" / (events.event_key(self.scopes[0], identity) + ".json")
        broken = json.loads(marker.read_text())
        broken["decision_id"] = "cannot-promote-local-outcome"
        marker.write_text(json.dumps(broken), encoding="utf-8")
        self.clock.return_value = 1200
        self.assertEqual(self.mind.one_cycle(), 1)
        states = self.state()["decision_scopes"]
        self.assertEqual(states[self.scopes[0]]["pending"]["event_id"], identity)
        self.assertIn("observer_error", states[self.scopes[0]])
        self.assertEqual(states[self.scopes[0]]["generation"], 1)
        self.assertEqual(states[self.scopes[1]]["generation"], 2)
        self.assertEqual(self.submit.call_count, 3)

    def test_admission_refusal_preserves_old_evidence_then_offers_an_independent_generation_with_uncertainty(self):
        self.assertEqual(self.mind.one_cycle(), 0)
        primary, original = self.finish(self.scopes[0], admission_refused=True, preserve_pending=True)
        self.finish(self.scopes[1], unknown=True)
        key = events.event_key(self.scopes[0], primary)
        pending = self.inbox / "pending" / (key + ".json")
        original_pending = pending.read_bytes()
        self.clock.return_value = 1002
        self.assertEqual(self.mind.one_cycle(), 0)
        state = self.state()["decision_scopes"][self.scopes[0]]
        fact = dict(event_id=primary, completed_at=1001000, reason="previous_turn_unresolved",
                    previous_activity_status="unknown", budget_reservation_preserved=True)
        self.assertEqual(state["last_status"], "no_decision")
        self.assertEqual(state["last_admission_refusal"], fact)
        self.assertEqual(state["generation"], 1)
        self.assertNotIn("pending", state)
        self.assertIsNone(state["next_review_at"])
        self.assertEqual(state["next_opportunity_at"], 1181000)
        self.clock.return_value = 1180.999
        self.assertEqual(self.mind.one_cycle(), 0)
        self.assertEqual(self.submit.call_count, 2)
        self.clock.return_value = 1181
        self.assertEqual(self.mind.one_cycle(), 0)
        states = self.state()["decision_scopes"]
        fresh = states[self.scopes[0]]["pending"]["event_id"]
        self.assertNotEqual(fresh, primary)
        self.assertEqual(states[self.scopes[0]]["generation"], 2)
        self.assertEqual(states[self.scopes[0]]["last_admission_refusal"], fact)
        fresh_path = self.inbox / "pending" / (events.event_key(self.scopes[0], fresh) + ".json")
        payload = json.loads(fresh_path.read_text(encoding="utf-8"))["payload"]
        self.assertEqual(payload["prior_admission_refusal"], fact)
        self.assertIn("new independent review, not a retry", payload["instruction"])
        self.assertIn("repeat an uncertain external action", payload["instruction"])
        self.assertEqual(states[self.scopes[1]]["last_status"], "unknown")
        self.assertIsNone(states[self.scopes[1]]["next_opportunity_at"])
        self.assertEqual(self.submit.call_count, 3)
        self.assertEqual(self.mind.one_cycle(), 0)
        self.assertEqual(self.submit.call_count, 3)
        self.assertEqual((self.inbox / "done" / (key + ".json")).read_bytes(), original)
        self.assertEqual(pending.read_bytes(), original_pending)


if __name__ == "__main__":
    unittest.main()
