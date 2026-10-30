"""Actual autonomous entry points only queue evidence in unified mode."""
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


class MainDecisionObserverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "fixture-character"
        self.data.mkdir()
        (self.data / "lanya-config.json").write_text(json.dumps({"adminQQ": ["101"]}), encoding="utf-8")
        config = self.root / "project.json"
        config.write_text(json.dumps({"dataDirName": "fixture-character", "decisions": {"mode": "unified"}}), encoding="utf-8")
        patcher = mock.patch.dict(os.environ, LANYA_WORKDIR=str(self.root), LANYA_PROJECT_CONFIG=str(config))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(sys, "path", [str(SCRIPTS)] + sys.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        import project_conf
        project_conf.load(reload=True)
        self.addCleanup(setattr, project_conf, "_CACHE", None)
        self.conf = project_conf
        self.mind = self.module("mind.py")
        self.clock = self.patch(self.mind.time, "time", return_value=1000.0)
        self.ask = self.patch(self.mind, "ask", side_effect=AssertionError("second LLM is forbidden"))
        self.send = self.patch(self.mind, "deliver", side_effect=AssertionError("direct QQ is forbidden"))
        self.patch(self.mind, "log")

    def patch(self, obj, name, **values):
        patcher = mock.patch.object(obj, name, **values)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def module(self, name):
        spec = importlib.util.spec_from_file_location("observer_fixture_" + name[:-3], SCRIPTS / name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def pending(self):
        return list((self.data / "decision-inbox" / "pending").glob("*.json"))

    def complete(self, *, next_review=None, status="decided", event=None):
        event = event or self.pending()[-1]
        marker = {"version": 1, "event_sha256": hashlib.sha256(event.read_bytes()).hexdigest(),
                  "status": status, "decision_id": "fixture-primary", "completed_at": int(self.clock.return_value * 1000)}
        if next_review is not None:
            marker["next_review_at"] = next_review
        (self.data / "decision-inbox" / "done" / event.name).write_text(json.dumps(marker), encoding="utf-8")

    def test_mind_queues_once_while_main_decision_is_pending(self):
        self.mind.save(self.mind.HITFILE, [{"who": "fixture", "text": "new evidence"}])
        self.assertEqual(self.mind.one_cycle(), 0)
        self.assertEqual(self.mind.one_cycle(), 0)
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(len(self.mind.load(self.mind.HITFILE, [])), 1)
        self.ask.assert_not_called()
        self.send.assert_not_called()

    def test_completed_turn_offers_later_opportunity_but_never_fixed_speech(self):
        self.mind.save(self.mind.HITFILE, [{"who": "old", "text": "observed"}])
        self.mind.one_cycle()
        self.complete()
        self.assertEqual(self.mind.one_cycle(), 0)
        self.clock.return_value += 359
        self.assertEqual(self.mind.one_cycle(), 0)
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.mind.load(self.mind.HITFILE, []), [])
        self.clock.return_value += 1
        self.assertEqual(self.mind.one_cycle(), 0)
        self.assertEqual(len(self.pending()), 2)
        self.ask.assert_not_called()
        self.send.assert_not_called()

    def test_only_main_llm_review_time_or_new_evidence_creates_another_turn(self):
        self.mind.one_cycle()
        self.complete(next_review=1_060_000)
        self.mind.one_cycle()
        self.assertEqual(len(self.pending()), 1)
        self.clock.return_value = 1060
        self.mind.one_cycle()
        # The queue, rather than Python, owns the explicit main-review successor.
        self.assertEqual(len(self.pending()), 1)
        self.ask.assert_not_called()

    def test_sent_without_review_uses_existing_interval_and_does_not_run_models(self):
        self.conf._CACHE["mind"] = {"opportunityMin": 12}
        self.mind.save(self.mind.MIND, {"next_check_min": 12})
        self.mind.one_cycle()
        self.complete(status="sent")
        self.mind.one_cycle()
        self.clock.return_value += 719
        self.mind.one_cycle()
        self.assertEqual(len(self.pending()), 1)
        self.clock.return_value += 1
        self.mind.one_cycle()
        self.assertEqual(len(self.pending()), 2)
        self.mind.one_cycle(force=True)
        self.assertEqual(len(self.pending()), 2)
        self.ask.assert_not_called(); self.send.assert_not_called()

    def test_force_is_a_new_opportunity_but_never_overrides_unknown(self):
        self.mind.one_cycle()
        self.complete()
        self.mind.one_cycle()
        self.mind.one_cycle(force=True)
        self.assertEqual(len(self.pending()), 2)
        # Equal fixture timestamps: choose the event whose generation is two.
        newest = next(p for p in self.pending() if json.loads(p.read_text())["event_id"].endswith(":2"))
        self.complete(status="unknown", event=newest, next_review=1_060_000)
        self.mind.one_cycle(force=True)
        self.clock.return_value += 86400
        self.mind.one_cycle(force=True)
        self.assertEqual(len(self.pending()), 2)
        self.mind.save(self.mind.HITFILE, [{"scope": "private:101", "text": "new evidence"}])
        self.mind.one_cycle()
        self.assertEqual(len(self.pending()), 3)

    def test_admin_group_opportunities_are_independent_without_private_context_leak(self):
        (self.data / "lanya-config.json").write_text(json.dumps({"adminQQ": ["101"], "fanGroupQQ": "202"}), encoding="utf-8")
        self.mind.save(self.mind.MIND, {"plan": {"goal": "private legacy reference"}, "mood": "private mood reference"})
        self.mind.save(self.mind.HITFILE, [{"text": "private interrupt"}, {"scope": "group:202", "text": "group interrupt"}])
        self.mind.one_cycle()
        events = {json.loads(p.read_text())["scope"]: p for p in self.pending()}
        admin = json.loads(events["private:101"].read_text())["payload"]
        group = json.loads(events["group:202"].read_text())["payload"]
        self.assertEqual(admin["legacy_plan_reference"]["goal"], "private legacy reference")
        self.assertEqual(admin["activity_role"], "internal_leisure")
        self.assertIn("trusted_runtime_capabilities.tools.entertainment", admin["instruction"])
        self.assertIn("Do not edit mind.json", admin["instruction"])
        self.assertIsNone(group["legacy_plan_reference"])
        self.assertIsNone(group["mood"])
        self.assertEqual(group["activity_role"], "conversation_social")
        self.assertIn("do not start a second copy", group["instruction"])
        self.assertEqual([h["text"] for h in admin["interrupts"]], ["private interrupt"])
        self.assertEqual([h["text"] for h in group["interrupts"]], ["group interrupt"])
        self.complete(event=events["private:101"])
        self.mind.one_cycle()
        self.clock.return_value += 360
        self.mind.one_cycle()
        self.assertEqual(len(self.pending()), 3)
        state = self.mind.load(self.mind.MIND, {})["decision_scopes"]
        self.assertEqual(state["private:101"]["generation"], 2)
        self.assertEqual(state["group:202"]["generation"], 1)

    def make_review(self, parent):
        import decision_events
        value = json.loads(parent.read_text())
        marker = json.loads((parent.parent.parent / "done" / parent.name).read_text())
        key = decision_events.event_key(value["scope"], value["event_id"])
        child = dict(value, event_id="main-review:" + key + ":" + str(marker["next_review_at"]),
                     producer="main-review", created_at=marker["next_review_at"],
                     payload=dict(value["payload"], review_context={"parent_observation_key": key,
                         "parent_event_id": value["event_id"], "previous_decision_id": marker["decision_id"],
                         "scheduled_at": marker["next_review_at"]}))
        decision_events.submit_observation(directory=parent.parent.parent, **{
            k: child[k] for k in ("scope", "event_id", "producer", "created_at", "payload", "workspace_key", "agent_key", "kind")})
        return parent.parent / (decision_events.event_key(child["scope"], child["event_id"]) + ".json")

    def test_explicit_queue_review_is_followed_to_completion_without_double_publication(self):
        self.mind.one_cycle()
        original = self.pending()[0]
        self.complete(event=original, next_review=1_060_000)
        self.clock.return_value = 1060
        self.mind.one_cycle(force=True)
        self.assertEqual(len(self.pending()), 1)
        child = self.make_review(original)
        self.mind.one_cycle()
        self.assertEqual(len(self.pending()), 2)
        self.complete(event=child)
        self.mind.one_cycle()
        self.clock.return_value += 359
        self.mind.one_cycle()
        self.assertEqual(len(self.pending()), 2)
        self.clock.return_value += 1
        self.mind.one_cycle()
        self.assertEqual(len(self.pending()), 3)

    def test_legacy_pending_migrates_without_duplicate_or_losing_original(self):
        import decision_events
        signature = "a" * 64
        identity = "mind:" + signature + ":7"
        decision_events.submit_project_observation(scope="private:101", kind="activity_due", producer="mind",
                                                   payload={"legacy": True}, event_id=identity)
        old = {"scope": "private:101", "event_id": identity, "signature": signature, "interrupts": []}
        self.mind.save(self.mind.MIND, {"decision_pending": old, "decision_generation": 7})
        self.mind.one_cycle(force=True)
        state = self.mind.load(self.mind.MIND, {})
        self.assertEqual(state["decision_legacy"]["decision_pending"], old)
        self.assertEqual(state["decision_scopes"]["private:101"]["pending"], old)
        self.assertNotIn("decision_pending", state)
        self.assertEqual(len(self.pending()), 1)
        self.complete(next_review=1_060_000)
        self.mind.one_cycle()
        self.clock.return_value = 1060
        self.mind.one_cycle()
        self.assertEqual(len(self.pending()), 2)

    def test_review_anchor_advances_across_more_than_128_lifetime_successors(self):
        self.mind.one_cycle()
        current = self.pending()[0]
        for _ in range(3):
            for _ in range(50):
                self.complete(event=current, next_review=int((self.clock.return_value + 60) * 1000))
                current = self.make_review(current)
                self.clock.return_value += 60
            self.mind.one_cycle()
            state = self.mind.load(self.mind.MIND, {})["decision_scopes"]["private:101"]
            self.assertEqual(state["pending"]["event_id"], json.loads(current.read_text())["event_id"])
        self.assertEqual(len(self.pending()), 151)
        self.complete(event=current)
        self.assertEqual(self.mind.one_cycle(), 0)
        self.assertNotIn("pending", self.mind.load(self.mind.MIND, {})["decision_scopes"]["private:101"])

    def test_forged_review_owner_or_payload_keeps_pending_and_records_only_safe_error(self):
        self.mind.one_cycle()
        original = self.pending()[0]
        self.complete(event=original, next_review=1_060_000)
        child = self.make_review(original)
        value = json.loads(child.read_text())
        for change in ({"agent_key": "other-agent"}, {"workspace_key": "other-workspace"},
                       {"payload": {"forged": "owner is not payload text"}}):
            child.write_text(json.dumps(dict(value, **change)), encoding="utf-8")
            before = Path(self.mind.MIND).read_bytes()
            self.assertEqual(self.mind.one_cycle(), 1)
            prior = json.loads(before)["decision_scopes"]["private:101"]
            after = self.mind.load(self.mind.MIND, {})["decision_scopes"]["private:101"]
            self.assertEqual(after["pending"], prior["pending"])
            self.assertEqual(after["generation"], prior["generation"])
            self.assertEqual(after["observer_error"]["error_class"], "ObservationError")
            self.assertEqual(set(after["observer_error"]), {"error_class", "first_seen_at", "last_seen_at"})
            self.assertEqual(len(self.pending()), 2)

    def test_completed_legacy_event_recovers_pointer_and_unknown_stays_terminal(self):
        import decision_events
        signature = "b" * 64
        identity = "mind:" + signature + ":1"
        decision_events.submit_project_observation(scope="private:101", kind="activity_due", producer="mind",
                                                   payload={"legacy": True}, event_id=identity)
        self.complete(status="unknown", next_review=1_060_000)
        self.mind.save(self.mind.MIND, {"decision_generation": 1, "decision_completed_signature": signature,
                                      "last_decision_status": "unknown", "decision_next_review_at": None})
        self.mind.one_cycle(force=True)
        self.clock.return_value += 86400
        self.mind.one_cycle(force=True)
        state = self.mind.load(self.mind.MIND, {})
        self.assertEqual(state["decision_legacy"]["last_decision_status"], "unknown")
        self.assertEqual(state["decision_scopes"]["private:101"]["last_status"], "unknown")
        self.assertEqual(len(self.pending()), 1)

    def test_malformed_scope_state_and_interrupts_do_not_publish_or_replace(self):
        for content in ({"decision_observers_version": True, "decision_scopes": {}},
                        {"decision_observers_version": 2, "decision_scopes": {"private:101": {"generation": True}}},
                        {"decision_pending": {"scope": "private:101", "event_id": "x", "signature": "bad", "interrupts": []}},
                        {"decision_completed_signature": "bad", "last_decision_status": "unknown"}):
            self.mind.save(self.mind.MIND, content)
            before = Path(self.mind.MIND).read_bytes()
            self.assertEqual(self.mind.one_cycle(), 1)
            self.assertEqual(Path(self.mind.MIND).read_bytes(), before)
            self.assertFalse(self.pending())
        self.mind.save(self.mind.MIND, {})
        Path(self.mind.HITFILE).parent.mkdir(exist_ok=True)
        Path(self.mind.HITFILE).write_text("{broken", encoding="utf-8")
        self.assertEqual(self.mind.one_cycle(), 1)
        self.assertFalse(self.pending())

    def test_missing_group_evidence_does_not_freeze_due_private_activity_or_replay_group(self):
        (self.data / "lanya-config.json").write_text(json.dumps({"adminQQ": ["101"], "fanGroupQQ": "202"}), encoding="utf-8")
        self.mind.one_cycle()
        events = {json.loads(p.read_text())["scope"]: p for p in self.pending()}
        group_raw = events["group:202"].read_bytes()
        before = self.mind.load(self.mind.MIND, {})["decision_scopes"]["group:202"]
        events["group:202"].unlink()
        self.complete(event=events["private:101"])
        self.assertEqual(self.mind.one_cycle(force=True), 1)
        self.clock.return_value += 360
        self.assertEqual(self.mind.one_cycle(), 1)
        state = self.mind.load(self.mind.MIND, {})["decision_scopes"]
        self.assertEqual(state["group:202"]["pending"], before["pending"])
        self.assertEqual(state["group:202"]["generation"], before["generation"])
        self.assertEqual(state["group:202"]["observer_error"], {
            "error_class": "FileNotFoundError", "first_seen_at": 1_000_000, "last_seen_at": 1_360_000})
        self.assertEqual(state["private:101"]["generation"], 2)
        self.assertEqual(len(self.pending()), 2)
        self.assertFalse(events["group:202"].exists())
        # Repair is explicit fixture restoration, never the producer inventing
        # a replacement event. The original pending identity remains in use.
        events["group:202"].write_bytes(group_raw)
        self.assertEqual(self.mind.one_cycle(force=True), 0)
        state = self.mind.load(self.mind.MIND, {})["decision_scopes"]["group:202"]
        self.assertNotIn("observer_error", state)
        self.assertEqual(state["pending"], before["pending"])
        self.assertEqual(len(self.pending()), 3)
        self.ask.assert_not_called(); self.send.assert_not_called()

    def test_missing_private_evidence_does_not_freeze_group_social_opportunity(self):
        (self.data / "lanya-config.json").write_text(json.dumps({"adminQQ": ["101"], "fanGroupQQ": "202"}), encoding="utf-8")
        self.mind.one_cycle()
        events = {json.loads(p.read_text())["scope"]: p for p in self.pending()}
        events["private:101"].unlink()
        self.complete(event=events["group:202"])
        self.assertEqual(self.mind.one_cycle(), 1)
        self.clock.return_value += 360
        self.assertEqual(self.mind.one_cycle(force=True), 1)
        state = self.mind.load(self.mind.MIND, {})["decision_scopes"]
        self.assertEqual(state["private:101"]["generation"], 1)
        self.assertEqual(state["group:202"]["generation"], 2)
        self.assertEqual(len(self.pending()), 2)

    def test_current_configuration_controls_interval_instead_of_legacy_eta(self):
        self.conf._CACHE["mind"] = {"opportunityMin": 3}
        self.mind.save(self.mind.MIND, {"next_check_min": 60})
        self.mind.one_cycle()
        self.complete()
        self.mind.one_cycle()
        self.clock.return_value += 179
        self.mind.one_cycle()
        self.assertEqual(len(self.pending()), 1)
        self.clock.return_value += 1
        self.mind.one_cycle()
        self.assertEqual(len(self.pending()), 2)

    def test_invalid_trusted_interval_refuses_without_events_or_state_replacement(self):
        for value in (True, None, "3", 1, 61, float("nan"), float("inf")):
            self.conf._CACHE["mind"] = {"opportunityMin": value}
            self.mind.save(self.mind.MIND, {"next_check_min": 3})
            before = Path(self.mind.MIND).read_bytes()
            self.assertEqual(self.mind.one_cycle(force=True), 1)
            self.assertEqual(Path(self.mind.MIND).read_bytes(), before)
            self.assertFalse(self.pending())

    def test_more_frequent_interval_never_overrides_explicit_future_review(self):
        self.conf._CACHE["mind"] = {"opportunityMin": 3}
        self.mind.one_cycle()
        self.complete(next_review=86_400_000)
        self.clock.return_value += 3600
        self.mind.one_cycle(force=True)
        self.assertEqual(len(self.pending()), 1)

    def test_instruction_update_is_not_fresh_evidence_for_existing_unknown(self):
        self.mind.save(self.mind.MIND, {"decision_observers_version": 2,
            "decision_scopes": {"private:101": {"generation": 9, "last_status": "unknown",
                "completed_signature": "f" * 64}}})
        self.assertEqual(self.mind.one_cycle(force=True), 0)
        self.assertFalse(self.pending())
        state = self.mind.load(self.mind.MIND, {})["decision_scopes"]["private:101"]
        self.assertEqual(state["evidence_version"], 1)
        original = self.mind._observation_payload
        with mock.patch.object(self.mind, "_observation_payload", side_effect=lambda *a, **k: dict(original(*a, **k), instruction="different new instruction")):
            self.clock.return_value += 86400
            self.assertEqual(self.mind.one_cycle(force=True), 0)
            self.assertFalse(self.pending())
            self.mind.save(self.mind.HITFILE, [{"scope": "private:101", "text": "new external evidence"}])
            self.assertEqual(self.mind.one_cycle(), 0)
            self.assertEqual(len(self.pending()), 1)

    def test_malformed_configured_group_refuses_before_any_scope_publication(self):
        for group in (False, 0, "not-a-conversation", ["202"]):
            (self.data / "lanya-config.json").write_text(json.dumps({"adminQQ": ["101"], "fanGroupQQ": group}), encoding="utf-8")
            self.assertEqual(self.mind.one_cycle(force=True), 1)
            self.assertFalse(self.pending())

    def test_observation_only_refuses_a_mode_change_before_running_legacy(self):
        self.conf._CACHE["decisions"]["mode"] = "legacy"
        self.assertEqual(self.mind.one_cycle(force=True, observe_only=True), 1)
        self.ask.assert_not_called(); self.send.assert_not_called()
        self.assertFalse(self.pending())
        self.assertFalse(Path(self.mind.MIND).exists())

    def test_actual_cli_observation_only_flag_stops_changed_config_before_model_or_transport(self):
        config = self.root / "project.json"
        value = json.loads(config.read_text(encoding="utf-8"))
        value["decisions"]["mode"] = "legacy"
        config.write_text(json.dumps(value), encoding="utf-8")
        harness = """
import runpy,sys
from unittest import mock
sys.path.insert(0,sys.argv[1])
script=sys.argv[2];sys.argv=[script,'--force','--observe-only']
with mock.patch('subprocess.run',side_effect=AssertionError('model forbidden')) as model, mock.patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')) as transport:
 try:runpy.run_path(script,run_name='__main__')
 except SystemExit as error:code=error.code
 else:code=0
 assert model.call_count==transport.call_count==0
 raise SystemExit(code)
"""
        result = subprocess.run([sys.executable, "-B", "-c", harness, str(SCRIPTS), str(SCRIPTS / "mind.py")],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(self.pending())
        self.assertFalse(Path(self.mind.MIND).exists())

    def test_new_interrupt_arriving_during_a_decision_is_not_consumed(self):
        old = {"who": "old", "text": "first"}
        new = {"who": "new", "text": "next"}
        self.mind.save(self.mind.HITFILE, [old])
        self.mind.one_cycle()
        self.mind.save(self.mind.HITFILE, [old, new])
        self.complete()
        self.mind.one_cycle()
        self.assertEqual(self.mind.load(self.mind.HITFILE, []), [new])
        self.assertEqual(len(self.pending()), 2)

    def test_unified_dry_preview_never_queues_or_changes_plan(self):
        self.mind.save(self.mind.MIND, {"plan": {"goal": "fixture"}})
        before = Path(self.mind.MIND).read_bytes()
        self.assertEqual(self.mind.one_cycle(dry=True), 0)
        self.assertEqual(Path(self.mind.MIND).read_bytes(), before)
        self.assertFalse((self.data / "decision-inbox").exists())

    def test_unknown_mode_or_missing_route_never_falls_back_to_legacy_model(self):
        self.conf._CACHE["decisions"]["mode"] = "broken"
        self.assertEqual(self.mind.one_cycle(), 1)
        self.conf._CACHE["decisions"]["mode"] = "unified"
        (self.data / "lanya-config.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.mind.one_cycle(), 1)
        self.ask.assert_not_called()
        self.send.assert_not_called()

    def test_greetings_queue_known_people_without_choosing_text_or_random_recipient(self):
        greeting = self.module("greetd.py")
        self.patch(greeting, "profiles", return_value={"202": "fixture profile"})
        self.patch(greeting, "talked_before", return_value=True)
        self.patch(greeting, "log")
        self.patch(greeting.time, "localtime", return_value=__import__('time').struct_time((2026, 1, 1, 12, 0, 0, 3, 1, -1)))
        ask = self.patch(greeting, "ask_agent", side_effect=AssertionError("second LLM"))
        send = self.patch(greeting, "api", side_effect=AssertionError("direct QQ"))
        choose = self.patch(greeting.random, "choice", side_effect=AssertionError("random autonomous choice"))
        greeting.main()
        greeting.main()
        self.assertEqual(len(self.pending()), 1)
        value = json.loads(self.pending()[0].read_text(encoding="utf-8"))
        self.assertEqual(value["scope"], "private:202")
        self.assertNotIn("profile", value["payload"])
        ask.assert_not_called()
        send.assert_not_called()
        choose.assert_not_called()

    def test_bili_entertainment_queues_once_and_never_runs_the_old_model_or_post_chain(self):
        runner = self.module("bili_onerun.py")
        connect = self.patch(runner, "BiliMCP", side_effect=AssertionError("old autonomous tool chain"))
        runner.run_one()
        runner.run_one()
        self.assertEqual(len(self.pending()), 1)
        evidence = json.loads(self.pending()[0].read_text(encoding="utf-8"))
        self.assertEqual(evidence["kind"], "entertainment.review")
        connect.assert_not_called()

    def test_social_notifications_only_read_platform_and_deduplicate_immutable_evidence(self):
        observer = self.module("autonomy_observers.py")
        mcp = mock.Mock()
        mcp.call_json.side_effect = lambda action, args: ({"replies": [{"id": "fixture", "user": {"mid": "303"}, "item": {"title": "hi", "subject_id": "topic"}}]}
                                                       if action == "bili_received_replies" else {"at": {"items": []}})
        first = observer.social_notifications(mcp)
        second = observer.social_notifications(mcp)
        self.assertEqual(first["sent"], 0)
        self.assertEqual(first["queued"], 1)
        self.assertEqual(second["queued"], 0)
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual({call.args[0] for call in mcp.call_json.call_args_list},
                         {"bili_received_replies", "bili_received_at_and_likes"})
        self.assertFalse((self.root / "napcat" / "bili-social-seen.json").exists())

    def test_social_dry_read_never_consumes_notifications_or_queues_them(self):
        observer = self.module("autonomy_observers.py")
        mcp = mock.Mock()
        mcp.call_json.return_value = {"items": [{"id": "fixture", "content": "hi"}]}
        result = observer.social_notifications(mcp, dry=True)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["queued"], 0)
        self.assertFalse((self.data / "decision-inbox").exists())

    def test_memory_timer_preserves_live_logs_and_only_requests_one_scoped_review(self):
        maintenance = self.module("memory-maintenance.py")
        raw = b"raw log remains exactly intact\n"
        Path(maintenance.LOG).write_bytes(raw)
        self.patch(maintenance, "log")
        import memory_context
        read = self.patch(memory_context, "load_visible_entries", return_value=[])
        maintenance.main()
        maintenance.main()
        self.assertEqual(read.call_args.args, (str(self.root), ""))
        self.assertEqual(Path(maintenance.LOG).read_bytes(), raw)
        self.assertEqual(len(self.pending()), 1)
        event = json.loads(self.pending()[0].read_text(encoding="utf-8"))
        self.assertEqual(event["kind"], "memory.review")
        self.assertNotIn("raw", event["payload"])


if __name__ == "__main__":
    unittest.main()
