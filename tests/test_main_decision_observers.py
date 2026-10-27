"""Actual autonomous entry points only queue evidence in unified mode."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
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

    def complete(self, *, next_review=None, status="decided"):
        event = self.pending()[-1]
        marker = {"version": 1, "event_sha256": hashlib.sha256(event.read_bytes()).hexdigest(),
                  "status": status, "decision_id": "fixture-primary"}
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

    def test_idle_completion_without_review_does_not_keep_waking_the_model(self):
        self.mind.save(self.mind.HITFILE, [{"who": "old", "text": "observed"}])
        self.mind.one_cycle()
        self.complete()
        self.assertEqual(self.mind.one_cycle(), 0)
        self.clock.return_value += 86400
        self.assertEqual(self.mind.one_cycle(), 0)
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.mind.load(self.mind.HITFILE, []), [])
        self.ask.assert_not_called()

    def test_only_main_llm_review_time_or_new_evidence_creates_another_turn(self):
        self.mind.one_cycle()
        self.complete(next_review=1_060_000)
        self.mind.one_cycle()
        self.assertEqual(len(self.pending()), 1)
        self.clock.return_value = 1060
        self.mind.one_cycle()
        self.assertEqual(len(self.pending()), 2)
        self.ask.assert_not_called()

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
