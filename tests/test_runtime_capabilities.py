"""Owner-bound capability fixtures; no real devices, models or service calls."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class RuntimeCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="runtime-capabilities-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / "napcat"
        self.runtime.mkdir()
        self.config = self.runtime / "config" / "project.json"
        self.config.parent.mkdir()
        self.config.write_text(json.dumps({"dataDirName": "FixtureRole", "android": {
            "serial": "emulator-5556"}}), encoding="utf-8")
        self.identity = self.root / "FixtureRole" / "lanya-config.json"
        self.identity.parent.mkdir()
        self.identity.write_text(json.dumps({"adminQQ": ["101", "303"]}), encoding="utf-8")
        env = mock.patch.dict(os.environ)
        env.start(); self.addCleanup(env.stop)
        os.environ.pop("LANYA_PROJECT_CONFIG", None)
        os.environ["LANYA_WORKDIR"] = str(self.root / "ignored-foreign-owner")
        sys.path.insert(0, str(SCRIPTS)); self.addCleanup(sys.path.remove, str(SCRIPTS))
        spec = importlib.util.spec_from_file_location("capability_fixture", SCRIPTS / "runtime_capabilities.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def context(self, **kwargs):
        return self.module.build_context(str(self.root), " Fixture-Preset ", "private:101",
                                         process_probe=lambda paths: {key: False for key in paths},
                                         now=lambda: 1234.0, **kwargs)

    def install(self, *names):
        for name in names:
            (self.runtime / name).write_text("# inert installed fixture\n", encoding="ascii")

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def test_missing_installation_is_explicit_and_read_only(self):
        before = self.snapshot()
        result = self.context()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(result["schema"], "dsh-runtime-capabilities/v1")
        self.assertTrue(result["read_only"])
        self.assertEqual(result["conversation_key"], "private:101")
        for key in ("browser", "android", "voice"):
            self.assertEqual(result["tools"][key]["deployment"], "not_installed")
        self.assertNotIn("ignored-foreign-owner", json.dumps(result))
        self.assertFalse(result["tools"]["android"]["cached_screenshot_is_current"])

    def test_installed_files_and_cached_images_never_prove_online(self):
        self.install("browserd.py", "androidd.py", "voice_executor.py")
        android = self.root / "FixtureRole" / "android"
        android.mkdir(parents=True)
        (android / "shot.png").write_bytes(b"cached private image is not a live view")
        (android / "result.json").write_text(json.dumps({"status": "ok", "shot_id": "old-view"}), encoding="utf-8")
        (android / "worker.lock").write_bytes(b"\0")
        before = self.snapshot()
        result = self.context()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(result["tools"]["browser"]["worker"], "offline")
        self.assertEqual(result["tools"]["android"]["worker"], "offline")
        self.assertEqual(result["tools"]["voice"]["reference_audio"], "missing")
        self.assertEqual(result["tools"]["voice"]["synthesis_service"], "unverified_not_probed")
        self.assertNotIn("old-view", json.dumps(result))

    def test_voice_is_an_optional_current_turn_expression_not_an_offline_assumption_or_send_permission(self):
        self.install("voice_executor.py")
        reference = self.root / "tts/ref/ref-main.wav"
        reference.parent.mkdir(parents=True)
        reference.write_bytes(b"asset presence is not a synthesis receipt")
        before = self.snapshot()
        for scope in ("private:101", "group:202"):
            with self.subTest(scope=scope):
                voice = self.module.build_context(str(self.root), "fixture", scope,
                    process_probe=lambda paths: {}, now=lambda: 1234.0)["tools"]["voice"]
                self.assertEqual(voice["synthesis_service"], "unverified_not_probed")
                self.assertEqual(voice["reference_audio"], "present")
                self.assertFalse(voice["delivered"])
                self.assertIn("need not wait for an explicit request", voice["expression"]["choice"])
                self.assertIn("does not mean offline", voice["expression"]["readiness"])
                self.assertIn("not a speech permit", voice["expression"]["authority"])
                self.assertIn("same content twice", voice["expression"]["text_alternative"])
        self.assertEqual(self.snapshot(), before)

    def test_qq_expression_guides_complete_content_and_real_image_route_without_authority(self):
        before = self.snapshot()
        for scope in ("private:101", "group:202"):
            with self.subTest(scope=scope):
                result = self.module.build_context(str(self.root), "fixture", scope,
                    process_probe=lambda paths: {}, now=lambda: 1234.0)
                guide = result["tools"]["qq_expression"]
                self.assertIn("no fixed sentence count or total message count", guide["reply_length"])
                self.assertIn("Default to concise, direct, natural chat", guide["reply_length"])
                self.assertIn("Expand when useful detail", guide["reply_length"])
                self.assertIn("preserving the full content", guide["reply_length"])
                self.assertIn("unverified", guide["registration"])
                self.assertIn("Selection is not delivery", guide["meme_selection"])
                self.assertIn("empty/missing image", guide["meme_selection"])
                self.assertIn("requires_visual_review", guide["meme_selection"])
                self.assertIn("read_image({file_path: chosen_path})", guide["meme_selection"])
                self.assertNotIn("read/view", guide["meme_selection"])
                self.assertIn("explicitly choose expression=text, meme or text_meme", guide["expression_decision"])
                self.assertIn("not a quota or an automatic sender", guide["expression_decision"])
                self.assertIn("qq_decide(reply)", guide["meme_delivery"])
                self.assertIn("qq_send_image({sources:", guide["meme_delivery"])
                self.assertIn("tool_action", guide["meme_delivery"])
                self.assertIn("does not grant QQ", guide["meme_delivery"])
                for boundary in ("silence", "scope", "revision", "budget", "deduplication", "unknown"):
                    self.assertIn(boundary, guide["limits"])
                self.assertIn("at most one real meme per turn", guide["limits"])
                self.assertIn("separate QQ image message", guide["limits"])
                self.assertNotIn("No fixed meme quota", guide["limits"])
        self.assertEqual(self.snapshot(), before)

    def test_delivery_state_boundary_is_internal_terminal_and_read_only_in_each_scope(self):
        before = self.snapshot()
        guides = []
        for scope in ("private:101", "group:202"):
            with self.subTest(scope=scope):
                result = self.module.build_context(str(self.root), "fixture", scope,
                    process_probe=lambda paths: {}, now=lambda: 1234.0)
                guide = result["tools"]["qq_expression"]["delivery_state_boundary"]
                guides.append(guide)
                self.assertIn("unknown or partial", guide["uncertain_send"])
                self.assertIn("stop all further QQ output in this turn", guide["uncertain_send"])
                self.assertIn("including final text, alternative content or media", guide["uncertain_send"])
                self.assertIn("read-only work may continue", guide["uncertain_send"])
                self.assertIn("Host send guards remain authoritative", guide["uncertain_send"])
                self.assertIn("Do not call qq_decide(idle)", guide["decision"])
                self.assertIn("internal control evidence, not chat or reminder content", guide["visible_content"])
                self.assertIn("fresh user question", guide["later_question"])
                self.assertIn("own valid ticket", guide["later_question"])
                self.assertIn("Ordinary parentheses and requested technical diagnostics are still allowed", guide["later_question"])
        self.assertEqual(guides[0], guides[1])
        guides[0]["decision"] = "isolated fixture mutation"
        self.assertNotEqual(guides[0], guides[1])
        self.assertEqual(self.snapshot(), before)

    def test_meme_alone_can_finish_without_receipt_quota_or_filler_text_in_each_scope(self):
        before = self.snapshot()
        for scope in ("private:101", "group:202", "private:404"):
            with self.subTest(scope=scope):
                tools = self.module.build_context(str(self.root), "fixture", scope,
                    process_probe=lambda paths: {}, now=lambda: 1234.0)["tools"]
                guide = tools["qq_expression"]
                self.assertIn("choose expression=meme when a fitting real image says enough", guide["expression_decision"])
                self.assertIn("expression=text_meme only for genuinely useful additional content", guide["expression_decision"])
                self.assertIn("not because an image requires accompanying text", guide["expression_decision"])
                completion = guide["meme_completion"]
                self.assertIn("real image alone", completion)
                self.assertIn("do not append text in this turn, whether the image succeeds, is unavailable or fails", completion)
                self.assertIn("qq_send_image confirms successful delivery", completion)
                self.assertIn("end immediately without a text bubble", completion)
                self.assertIn("choose expression=text_meme before the first QQ output", completion)
                self.assertIn("accompanying text is never required", completion)
                self.assertIn("Selection or staging is not delivery", completion)
                self.assertIn("Unknown or partial results still require stopping all further QQ output", completion)
                visible = guide["delivery_state_boundary"]["visible_content"]
                for internal in ("Successful tool receipts", "message_id", "raw tool JSON", "internal quota",
                                 "remaining allowance", "usage counts"):
                    self.assertIn(internal, visible)
                self.assertIn("not an extra chat bubble", visible)
                self.assertIn("fresh user question", guide["delivery_state_boundary"]["later_question"])
                self.assertEqual(tools["entertainment"]["rest_window"]["timezone"], "Asia/Shanghai")
                self.assertEqual(tools["entertainment"]["proactive_sharing"]["private_contact"]["minimum_interval_minutes"], 60)
                self.assertEqual(tools["entertainment"]["proactive_sharing"]["private_contact"]["wait_for_live_reply"], "use_trusted_autonomy_rhythm")
        self.assertEqual(self.snapshot(), before)

    def test_intent_protocol_preserves_real_candidates_and_recipient_authority_without_io(self):
        before = self.snapshot()
        primary = self.context()["tools"]["qq_autonomy"]
        group = self.module.build_context(str(self.root), "fixture", "group:202",
            process_probe=lambda paths: {}, now=lambda: 1234.0)["tools"]["qq_autonomy"]
        self.assertEqual(primary["social_planning_scope"], "configured_primary_only")
        self.assertEqual(group["social_planning_scope"], "unavailable_in_this_conversation")
        self.assertIn("actual_tool_list", primary["registration"])
        for field in ("intent_id", "expected_revision", "agent_summary", "blocked_unknown"):
            self.assertIn(field, primary["intent"])
        for required in ("qq_social_candidates", "can_propose=true", "live appearance", "1 to 12", "group_ref"):
            self.assertIn(required, primary["candidates"])
        for boundary in ("qq_social_propose", "sent=false", "scope-bound qq_decide", "silence", "budget", "private notes"):
            self.assertIn(boundary, primary["proposal"])
        for boundary in ("private_user_only", "no group_id", "unverified", "unknown", "without automatic retry"):
            self.assertIn(boundary, primary["transport"])
        self.assertIn("unsupported", primary["group_discovery"])
        self.assertIn("not an application", primary["group_discovery"])
        self.assertEqual(self.snapshot(), before)

    def test_malformed_primary_identity_does_not_advertise_social_planner_authority(self):
        self.identity.write_text(json.dumps({"adminQQ": True}), encoding="utf-8")
        self.assertEqual(self.context()["tools"]["qq_autonomy"]["social_planning_scope"],
                         "unavailable_in_this_conversation")

    def test_prompt_defaults_to_concise_but_keeps_complete_detail_and_no_total_cap(self):
        prompt = (SCRIPTS.parent / "prompts" / "lifeloop.prompt.txt").read_text(encoding="utf-8")
        style = next(line for line in prompt.splitlines() if line.startswith("聊天默认"))
        self.assertIn("简短直接", style)
        self.assertIn("长短按当前内容决定", style)
        self.assertIn("不设固定句数或总条数", style)
        self.assertIn("直接说完整", style)
        self.assertIn("不用等追问", style)
        self.assertNotRegex(style, r"(?:最多|限于|固定)[一二两三123]+(?:句|行|条)")

    def test_live_reaction_uses_one_candidate_without_restricting_user_image_understanding(self):
        before = self.snapshot()
        guide = self.context(context_mode='live_chat')['tools']['qq_expression']['meme_delivery']
        for boundary in ('limit: 1', 'one picker call per live turn', 'do not repeat',
                         'requires_visual_review=true', 'use concise text',
                         'necessary inspection of images supplied by the user', 'current reply ticket'):
            self.assertIn(boundary, guide)
        self.assertEqual(self.snapshot(), before)

    def test_entertainment_catalog_requires_real_entry_and_never_reads_private_bodies(self):
        made = self.root / "FixtureRole" / "made"
        game = made / "2048"
        game.mkdir(parents=True)
        (game / "board.png").write_bytes(b"old board is not a browser game")
        (game / "ai2048.py").write_text("# offline solver is not a web entry", encoding="ascii")
        self.assertEqual(self.context()["tools"]["entertainment"]["local_games"]["entries"], [])
        (game / "index.html").write_text("private body must not enter trusted guide", encoding="utf-8")
        hidden = made / "_old-profile"
        hidden.mkdir()
        (hidden / "index.html").write_text("profile data", encoding="utf-8")
        self.config.write_text(json.dumps({"dataDirName": "FixtureRole", "panelStaticPort": 19001}), encoding="utf-8")
        before = self.snapshot()
        guide = self.context()["tools"]["entertainment"]
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(guide["local_games"]["entries"], [{"name": "2048", "url": "http://127.0.0.1:19001/2048/"}])
        self.assertIn("unverified", guide["local_games"]["catalog_status"])
        self.assertNotIn("private body", json.dumps(guide))
        self.assertIn("not proof of watching", guide["bilibili"]["verify"])
        self.assertIn("no comment", guide["bilibili"]["social"])
        self.assertIn("decision-inbox", guide["limits"])
        self.assertTrue(guide["activity_notes"]["path"].startswith(str(self.root / "FixtureRole" / "notes")))
        other = self.module.build_context(str(self.root), "fixture", "group:202", process_probe=lambda paths: {})
        self.assertNotEqual(guide["activity_notes"]["path"], other["tools"]["entertainment"]["activity_notes"]["path"])

    def test_game_catalog_cannot_advertise_linked_or_reparse_paths(self):
        made = self.root / "FixtureRole" / "made"
        game = made / "linked-game"
        game.mkdir(parents=True)
        (game / "index.html").write_text("fixture", encoding="ascii")
        real_lstat = Path.lstat
        def reparse_parent(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400) if path == game else info
        with mock.patch.object(Path, "lstat", reparse_parent):
            self.assertEqual(self.context()["tools"]["entertainment"]["local_games"]["entries"], [])

    def test_proactive_sharing_keeps_internal_steps_local_without_banning_initiative(self):
        before = self.snapshot()
        for scope in ("private:101", "group:202"):
            with self.subTest(scope=scope):
                context = self.module.build_context(str(self.root), "fixture", scope,
                    process_probe=lambda paths: {}, now=lambda: 1234.0)
                sharing = context["tools"]["entertainment"]["proactive_sharing"]
                self.assertEqual(sharing["authority"], "preference_only_no_send_or_silence_control_permission")
                self.assertIn("unsolicited", sharing["routine_progress"])
                self.assertIn("activity note and panel", sharing["routine_progress"])
                self.assertIn("private activity-note paths/links", sharing["routine_progress"])
                self.assertIn("personal view", sharing["meaningful_sharing"])
                self.assertIn("not a ban on initiative", sharing["meaningful_sharing"])
                self.assertIn("accurately and completely", sharing["requested_detail"])
                self.assertIn("Do not suppress a useful plugin opinion", sharing["requested_detail"])
                for boundary in ("silence", "scope", "revision", "budget", "deduplication"):
                    self.assertIn(boundary, sharing["permission"])
                self.assertIn("no fixed sentence count or total message count", sharing["length"])
                self.assertIn("Default to concise, direct, natural sharing", sharing["length"])
                self.assertFalse(context["tools"]["voice"]["delivered"])
        self.assertEqual(self.snapshot(), before)

    def test_video_evidence_distinguishes_subtitles_frames_playback_and_completion(self):
        before = self.snapshot()
        bili = self.context()["tools"]["entertainment"]["bilibili"]
        claims = bili["claim_boundaries"]
        self.assertIn("not playback", bili["verify"])
        self.assertEqual(set(claims), {"recommendations_and_details", "subtitles", "frames", "playback", "completion"})
        self.assertIn("rather than claiming you watched or finished", claims["subtitles"])
        self.assertIn("do not prove continuous playback", claims["frames"])
        self.assertIn("playback progressing through that segment", claims["playback"])
        self.assertIn("a seek to the end", claims["completion"])
        self.assertIn("reading every subtitle is insufficient", claims["completion"])
        self.assertEqual(self.snapshot(), before)

    def test_bilibili_preferences_are_fresh_strict_and_do_not_expose_account_configuration(self):
        initial = self.context()["tools"]["entertainment"]["bilibili"]
        self.assertEqual(initial["preferences"], {"valid": True, "comment": False,
                                                "reply": False, "mentionAdmin": False})
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["bilibili"] = {"social": {"comment": True, "reply": True, "mentionAdmin": True},
                              "private_account_marker": "PRIVATE_BILI_ACCOUNT_NOT_FOR_GUIDE"}
        self.config.write_text(json.dumps(config), encoding="utf-8")
        before = self.snapshot()
        guide = self.context()["tools"]["entertainment"]["bilibili"]
        self.assertEqual(guide["preferences"], {"valid": True, "comment": True,
                                              "reply": True, "mentionAdmin": True})
        self.assertNotIn("PRIVATE_BILI_ACCOUNT_NOT_FOR_GUIDE", json.dumps(guide))
        self.assertIn("mention_admin=true or false", guide["mention"])
        self.assertIn("mention_admin: your_explicit_boolean_choice", guide["delivery"])
        self.assertIn("No quota", guide["mention"])
        self.assertEqual(self.snapshot(), before)
        for invalid in (None, [], True, {"comment": "true"}, {"reply": 1}, {"mentionAdmin": None}):
            with self.subTest(invalid=invalid):
                config["bilibili"]["social"] = invalid
                self.config.write_text(json.dumps(config), encoding="utf-8")
                before = self.snapshot()
                preferences = self.context()["tools"]["entertainment"]["bilibili"]["preferences"]
                self.assertEqual(preferences, {"valid": False, "comment": False,
                                               "reply": False, "mentionAdmin": False})
                self.assertEqual(self.snapshot(), before)

    def test_primary_social_choice_does_not_require_a_major_result_or_an_early_internal_verdict(self):
        before = self.snapshot()
        primary = self.context()["tools"]
        sharing = primary["entertainment"]["proactive_sharing"]
        self.assertIn("small amusing observation or a suitable real meme", sharing["meaningful_sharing"])
        self.assertIn("does not require a major achievement", sharing["meaningful_sharing"])
        self.assertIn("unanswered messages and timing", sharing["meaningful_sharing"])
        self.assertIn("consider reaching out in this same conversation", sharing["primary_choice"])
        self.assertIn("make one qq_decide decision before the first QQ output", sharing["permission"])
        self.assertIn("Reuse an existing valid reply ticket", sharing["permission"])
        self.assertIn("cannot later be upgraded", sharing["permission"])
        self.assertNotIn("then use qq_decide(tool_action or idle)", primary["entertainment"]["continuity"])
        self.assertIn("not as a mandatory ending", primary["entertainment"]["continuity"])
        self.assertIn("do not call qq_decide again", primary["qq_expression"]["meme_delivery"])
        group = self.module.build_context(str(self.root), "fixture", "group:202",
            process_probe=lambda paths: {}, now=lambda: 1234.0)["tools"]["entertainment"]
        self.assertIn("does not authorize contacting the primary private conversation", group["proactive_sharing"]["primary_choice"])
        self.assertEqual(self.snapshot(), before)

    def test_unsolicited_private_rhythm_waits_an_hour_and_for_a_real_reply_without_limiting_live_chat(self):
        before = self.snapshot()
        for scope in ("private:101", "private:303", "group:202"):
            with self.subTest(scope=scope):
                tools = self.module.build_context(str(self.root), "fixture", scope,
                    process_probe=lambda paths: {}, now=lambda: 1234.0)["tools"]
                sharing = tools["entertainment"]["proactive_sharing"]
                rhythm = sharing["private_contact"]
                self.assertEqual(rhythm["scope"], "unsolicited_private_contact_per_recipient")
                self.assertEqual(rhythm["minimum_interval_minutes"], 60)
                self.assertEqual(rhythm["wait_for_live_reply"], "use_trusted_autonomy_rhythm")
                self.assertIn("do not start another, even after an hour", rhythm["choice"])
                self.assertIn("a first suitable contact remains possible", rhythm["choice"])
                self.assertIn("a minimum, not a quota", rhythm["choice"])
                self.assertIn("text_delivery=not_measured cannot establish last actual contact", rhythm["evidence"])
                self.assertIn("Missing evidence is not permission to nudge", rhythm["evidence"])
                self.assertIn("does not impose a one-hour gap on replies", rhythm["boundary"])
                self.assertIn("never switch media or scope", rhythm["boundary"])
                self.assertIn("do not block responses to actual incoming messages", tools["qq_expression"]["direct_contact"])
                self.assertIn("unanswered proactive contact", tools["qq_expression"]["meme_preference"])
                self.assertIn("no follow-up until an actual live reply", tools["qq_autonomy"]["proposal"])
        primary = self.context()["tools"]["entertainment"]
        self.assertIn("rather than a fixed daily message target", primary["proactive_sharing"]["primary_choice"])
        self.assertNotIn("prefer a concrete social choice", primary["proactive_sharing"]["primary_choice"])
        self.assertNotIn("Do not wait for a person to speak first", primary["continuity"])
        self.assertIn("not a reason to skip daytime internal leisure", primary["cadence"]["preference"])
        self.assertEqual(self.snapshot(), before)

    def test_daily_beijing_rest_covers_autonomous_work_without_claiming_inflight_cancellation(self):
        before = self.snapshot()
        for scope in ("private:101", "group:202"):
            with self.subTest(scope=scope):
                guide = self.module.build_context(str(self.root), "fixture", scope,
                    process_probe=lambda paths: {}, now=lambda: 1234.0)["tools"]["entertainment"]
                rest = guide["rest_window"]
                self.assertEqual((rest["timezone"], rest["start_inclusive"], rest["end_exclusive"]),
                                 ("Asia/Shanghai", "00:00", "08:00"))
                self.assertIn("All observation sources, including activity, reminders, Bilibili and social opportunities", rest["autonomous_activity"])
                self.assertIn("stop starting new steps, tools and QQ sends", rest["autonomous_activity"])
                self.assertIn("Do not claim that already-started HTTP requests or external actions were cancelled", rest["autonomous_activity"])
                self.assertIn("do not replay skipped overnight opportunities", rest["autonomous_activity"])
                self.assertIn("not proof of the current time", rest["authority"])
                self.assertIn("may still receive a normal useful response", rest["live_input"])
                self.assertIn("Persistent silence, scope, revision, budget and delivery guards", rest["live_input"])
                self.assertIn("rest takes precedence", guide["cadence"]["explicit_review"])
                self.assertIn("night rest separately pauses autonomous internal work", guide["cadence"]["communication"])
        self.assertEqual(self.snapshot(), before)

    def test_cadence_uses_current_trusted_configuration_and_preserves_read_only_scope(self):
        for minutes in (2, 3, 3.5, 60):
            with self.subTest(minutes=minutes):
                self.config.write_text(json.dumps({"dataDirName": "FixtureRole", "mind": {
                    "opportunityMin": minutes}}), encoding="utf-8")
                before = self.snapshot()
                context = self.context()
                self.assertEqual(self.snapshot(), before)
                cadence = context["tools"]["entertainment"]["cadence"]
                self.assertTrue(cadence["valid"])
                self.assertEqual(cadence["configured_opportunity_min"], minutes)
                self.assertIn("new trusted primary", cadence["explicit_review"])
                self.assertIn("Already persisted decisions are not changed", cadence["explicit_review"])
                self.assertIn("not a requirement for a new action", cadence["preference"])
                self.assertIn("do not lock all future leisure", cadence["preference"])
                self.assertIn("not evidence", cadence["longer_interval"])
                self.assertIn("does not extend the next autonomous review", cadence["longer_interval"])
                self.assertIn("may remain idle, continue resting or keep watching", cadence["longer_interval"])
                self.assertIn("no sending authority", cadence["communication"])
                group = self.module.build_context(str(self.root), "fixture", "group:202",
                    process_probe=lambda paths: {}, now=lambda: 1234.0)
                group_guide = group["tools"]["entertainment"]
                self.assertEqual(group_guide["cadence"]["configured_opportunity_min"], minutes)
                self.assertIn("Group opportunities remain social only", group_guide["cadence"]["communication"])
                self.assertNotEqual(group_guide["activity_notes"]["path"], context["tools"]["entertainment"]["activity_notes"]["path"])
                self.assertTrue(context["tools"]["entertainment"]["primary_identity_valid"])
                self.assertTrue(context["tools"]["entertainment"]["primary_internal_leisure"])
                self.assertFalse(group_guide["primary_internal_leisure"])
                self.assertFalse(group_guide["group_social"])

    def test_group_cadence_uses_only_configured_group_and_same_visible_interval(self):
        self.config.write_text(json.dumps({"dataDirName": "FixtureRole", "mind": {
            "opportunityMin": 2}, "fanGroup": "404"}), encoding="utf-8")
        for identity, configured in (({"adminQQ": ["101"], "fanGroupQQ": "202", "fanGroup": "303"}, "202"),
                                     ({"adminQQ": ["101"], "fanGroupQQ": "", "fanGroup": 303}, "303"),
                                     ({"adminQQ": ["101"]}, "404")):
            with self.subTest(identity=identity):
                self.identity.write_text(json.dumps(identity), encoding="utf-8")
                before = self.snapshot()
                for scope, expected in (("group:" + configured, True), ("group:999", False), ("private:101", False)):
                    guide = self.module.build_context(str(self.root), "fixture", scope,
                        process_probe=lambda paths: {}, now=lambda: 1234.0)["tools"]["entertainment"]
                    self.assertTrue(guide["group_identity_valid"])
                    self.assertIs(guide["group_social"], expected)
                    self.assertEqual(guide["cadence"]["configured_opportunity_min"], 2)
                    self.assertIn("configured group social", guide["cadence"]["explicit_review"])
                    self.assertIn("ordinary live turns retain", guide["cadence"]["explicit_review"])
                    if expected:
                        self.assertIn("need not wait for an @ mention", guide["proactive_sharing"]["primary_choice"])
                        self.assertIn("who is speaking to whom", guide["proactive_sharing"]["primary_choice"])
                        self.assertIn("does not require speech", guide["proactive_sharing"]["primary_choice"])
                self.assertEqual(self.snapshot(), before)

    def test_malformed_group_identity_cannot_borrow_fallback_or_private_leisure(self):
        self.config.write_text(json.dumps({"dataDirName": "FixtureRole", "fanGroup": "202"}), encoding="utf-8")
        for group in (True, False, [], {}, "0202", "not-a-group"):
            with self.subTest(group=group):
                self.identity.write_text(json.dumps({"adminQQ": ["101"], "fanGroupQQ": group,
                                                     "fanGroup": "202"}), encoding="utf-8")
                before = self.snapshot()
                guide = self.module.build_context(str(self.root), "fixture", "group:202",
                    process_probe=lambda paths: {})["tools"]["entertainment"]
                self.assertFalse(guide["group_identity_valid"])
                self.assertFalse(guide["group_social"])
                self.assertFalse(guide["primary_internal_leisure"])
                self.assertIn("no configured group review cadence", guide["proactive_sharing"]["primary_choice"])
                self.assertEqual(self.snapshot(), before)

    def test_primary_identity_comes_from_owner_data_not_administrator_allowlist_order(self):
        for admins in (["303", "101"], "303", 303):
            with self.subTest(admins=admins):
                self.identity.write_text(json.dumps({"adminQQ": admins}), encoding="utf-8")
                guide = self.context()["tools"]["entertainment"]
                self.assertTrue(guide["primary_identity_valid"])
                self.assertFalse(guide["primary_internal_leisure"])
                primary = self.module.build_context(str(self.root), "fixture", "private:303",
                    process_probe=lambda paths: {})["tools"]["entertainment"]
                self.assertTrue(primary["primary_internal_leisure"])

    def test_missing_or_malformed_identity_cannot_claim_primary_autonomy(self):
        for body in (None, "{damaged", "[]", json.dumps({"adminQQ": []}),
                     json.dumps({"adminQQ": True}), json.dumps({"adminQQ": ["0101"]})):
            with self.subTest(body=body):
                if body is None:
                    self.identity.unlink()
                else:
                    self.identity.write_text(body, encoding="utf-8")
                guide = self.context()["tools"]["entertainment"]
                self.assertFalse(guide["primary_identity_valid"])
                self.assertFalse(guide["primary_internal_leisure"])
                self.assertEqual(guide["cadence"]["configured_opportunity_min"], 6)

    def test_missing_cadence_matches_mind_dotted_lookup_default(self):
        for extra in ({}, {"mind": {}}, {"mind": None}, {"mind": "fixture legacy scalar"}):
            with self.subTest(extra=extra):
                self.config.write_text(json.dumps(dict(dataDirName="FixtureRole", **extra)), encoding="utf-8")
                self.assertEqual(self.context()["tools"]["entertainment"]["cadence"]["configured_opportunity_min"], 6)

    def test_invalid_cadence_cannot_advertise_a_different_schedule(self):
        for value in (True, False, None, "3", [], {}, 0, 1.99, 60.01, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.config.write_text(json.dumps({"dataDirName": "FixtureRole", "mind": {
                    "opportunityMin": value}}), encoding="utf-8")
                before = self.snapshot()
                context = self.context()
                cadence = context["tools"]["entertainment"]["cadence"]
                self.assertFalse(cadence["valid"])
                self.assertIsNone(cadence["configured_opportunity_min"])
                self.assertIn("no trustworthy cadence", cadence["configuration"])
                self.assertEqual(context["tools"]["browser"]["deployment"], "not_installed")
                self.assertEqual(self.snapshot(), before)

    def test_invalid_works_port_is_not_advertised_as_a_game_service(self):
        for port in (True, 0, 65536, "19388"):
            with self.subTest(port=port):
                self.config.write_text(json.dumps({"dataDirName": "FixtureRole", "panelStaticPort": port}), encoding="utf-8")
                with self.assertRaises(self.module.CapabilityError):
                    self.context()

    def test_active_android_lock_only_proves_worker_not_live_device(self):
        self.install("androidd.py")
        from androidd import worker_lock
        lock = self.root / "FixtureRole" / "android" / "worker.lock"
        lock.parent.mkdir(parents=True)
        with worker_lock(lock):
            result = self.context()["tools"]["android"]
            self.assertEqual(result["worker"], "lock_held_device_unverified")
            self.assertEqual(result["live_device_or_page"], "unverified")
            self.assertFalse(result["cached_screenshot_is_current"])
        self.assertFalse(self.module.lock_held(lock))

    def test_installed_configuration_precedence_and_protocol_are_actionable(self):
        self.install("browserd.py", "androidd.py")
        lower = self.root / "config" / "project.json"
        lower.parent.mkdir()
        lower.write_text(json.dumps({"dataDirName": "WrongRole"}), encoding="utf-8")
        result = self.context()
        android = result["tools"]["android"]
        self.assertEqual(android["queue_directory"], str(self.root / "FixtureRole" / "android"))
        self.assertEqual(android["protocol"]["observe"]["serial"], "emulator-5556")
        self.assertIn("expected_shot_id", android["protocol"]["action_example"])
        self.assertIn("exact seq", android["protocol"]["verify"])
        self.assertIn("newly verified PNG", android["protocol"]["after_action"])
        self.assertIn("no shot_id", result["tools"]["browser"]["protocol"]["limits"])
        self.assertNotIn("WrongRole", json.dumps(result))

    def test_process_probe_is_bound_to_exact_trusted_deployed_paths(self):
        self.install("browserd.py")
        observed = []
        def probe(paths):
            observed.append(paths)
            return {"browser": True}
        result = self.module.build_context(str(self.root), "fixture", "group:202", process_probe=probe)
        self.assertEqual(observed, [{"browser": str(self.runtime / "browserd.py")}])
        self.assertEqual(result["tools"]["browser"]["worker"], "running_process_queue_unverified")
        self.assertEqual(result["tools"]["browser"]["live_device_or_page"], "unverified")

    def test_foreign_environment_or_damaged_preferred_config_fails_closed(self):
        os.environ["LANYA_PROJECT_CONFIG"] = str(self.root.parent / "foreign-project.json")
        with self.assertRaises(self.module.CapabilityError):
            self.context()
        os.environ.pop("LANYA_PROJECT_CONFIG")
        self.config.write_text("{damaged", encoding="utf-8")
        with self.assertRaises(self.module.CapabilityError):
            self.context()

    def test_reference_audio_must_be_an_ordinary_path_inside_the_trusted_workspace(self):
        with tempfile.TemporaryDirectory(prefix="foreign-reference-fixture-") as foreign:
            reference = Path(foreign) / "reference.wav"
            reference.write_bytes(b"not this role's material")
            for value in (str(reference), os.path.relpath(reference, self.root)):
                with self.subTest(reference=value):
                    self.config.write_text(json.dumps({"dataDirName": "FixtureRole", "tts": {
                        "refAudioPath": value}}), encoding="utf-8")
                    with self.assertRaises(self.module.CapabilityError):
                        self.context()
        local = self.root / "tts" / "ref" / "ref-main.wav"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"ordinary local reference fixture")
        self.config.write_text(json.dumps({"dataDirName": "FixtureRole"}), encoding="utf-8")
        self.assertEqual(self.context()["tools"]["voice"]["reference_audio"], "present")
        real_lstat = Path.lstat
        def reparse_parent(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400) if path == local.parent else info
        with mock.patch.object(Path, "lstat", reparse_parent):
            with self.assertRaises(self.module.CapabilityError):
                self.context()

    def test_owner_scope_and_directory_cannot_be_guessed(self):
        for cwd, preset, scope in [("", "fixture", "private:101"), ("relative", "fixture", "private:101"),
                                   (str(self.root), None, "private:101"), (str(self.root), "fixture", "group:0101")]:
            with self.subTest(cwd=cwd, preset=preset, scope=scope):
                with self.assertRaises(self.module.CapabilityError):
                    self.module.build_context(cwd, preset, scope)
        self.config.write_text(json.dumps({"dataDirName": "../foreign"}), encoding="utf-8")
        with self.assertRaises(self.module.CapabilityError):
            self.context()

    def test_cli_accepts_only_trusted_metadata_and_rejects_payload_without_echo(self):
        script = SCRIPTS / "runtime_capabilities.py"
        request = dict(cwd=str(self.root), agent_preset="fixture", conversation_key="private:101")
        first = subprocess.run([sys.executable, "-B", str(script), "--stdin-json"], input=json.dumps(request),
                               text=True, capture_output=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["conversation_key"], "private:101")
        second = subprocess.run([sys.executable, "-B", str(script), "--stdin-json"],
                                input=json.dumps(dict(request, payload="untrusted fixture must never echo")),
                                text=True, capture_output=True, check=False)
        self.assertEqual(second.returncode, 1)
        self.assertNotIn("untrusted fixture", second.stdout + second.stderr)

    def test_live_chat_skips_process_and_lock_probes_and_keeps_fresh_owner_and_media_guards(self):
        self.install('browserd.py', 'androidd.py', 'voice_executor.py')
        def forbidden(*_):
            self.fail('Incoming chat must not wait for a process or lock probe')
        before = self.snapshot()
        value = self.module.build_context(str(self.root), 'fixture', 'private:101',
            context_mode='live_chat', process_probe=forbidden, lock_probe=forbidden)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(value['context_mode'], 'live_chat')
        self.assertEqual(value['conversation_key'], 'private:101')
        self.assertEqual(value['tools']['browser']['worker'], 'unverified')
        self.assertEqual(value['tools']['android']['worker'], 'unverified')
        self.assertFalse(value['tools']['browser']['cached_screenshot_is_current'])
        self.assertIn('exact submitted seq', value['tools']['browser']['protocol']['verify'])
        self.assertIn('expected_shot_id', value['tools']['android']['protocol']['action_example'])
        self.assertIn('qq_send_voice', value['tools']['voice']['protocol']['send'])
        self.assertIn('qq_send_image', value['tools']['qq_expression']['meme_delivery'])
        self.assertIn('current reply ticket', value['tools']['qq_expression']['meme_delivery'])
        full = self.context()
        self.assertLess(len(json.dumps(value)), len(json.dumps(full)) * .5)
        # New configuration is still checked on every request, without caching.
        self.config.write_text('{damaged', encoding='utf-8')
        with self.assertRaises(self.module.CapabilityError):
            self.module.build_context(str(self.root), 'fixture', 'private:101', context_mode='live_chat')

    def test_live_mode_never_enables_primary_tools_for_another_chat_or_accepts_unknown_modes(self):
        value = self.module.build_context(str(self.root), 'fixture', 'group:202', context_mode='live_chat')
        self.assertFalse(value['tools']['qq_autonomy']['primary'])
        self.assertFalse(value['tools']['entertainment']['primary_internal_leisure'])
        for mode in ('fast_unchecked', None, True):
            with self.assertRaises(self.module.CapabilityError):
                self.module.build_context(str(self.root), 'fixture', 'private:101', context_mode=mode)

    def test_enabled_native_phone_is_discoverable_in_primary_live_and_autonomous_contexts(self):
        self.install('androidd.py', 'android_executor.py')
        self.config.write_text(json.dumps({'dataDirName': 'FixtureRole', 'android': {
            'serial': '127.0.0.1:16384', 'agentTransport': 'desktop_loopback', 'adbServerPort': 15043,
            'agentEnabled': True, 'agentApps': ['amap', 'unknown']}}), encoding='utf-8')
        full = self.context(lock_probe=lambda _: True)['tools']
        self.assertEqual(full['entertainment']['android_game']['name'], '2048')
        self.assertNotIn('agent_apps', full['android'])
        self.assertNotIn('android_game', self.context(lock_probe=lambda _: False)['tools']['entertainment'])

        def forbidden(*_):
            self.fail('Live phone guidance must not probe the worker')

        for scope in ('private:101', 'private:303', 'group:202'):
            with self.subTest(scope=scope):
                live = self.module.build_context(str(self.root), 'fixture', scope,
                    context_mode='live_chat', process_probe=forbidden, lock_probe=forbidden)['tools']
                self.assertNotIn('android_game', live['entertainment'])
                if scope == 'private:101':
                    self.assertEqual(live['android']['agent_tool'], 'android_phone')
                    self.assertIn('read_image', live['android']['agent_play'])
                    self.assertEqual(live['android']['agent_apps'], ['amap'])
                    self.assertEqual(live['android']['worker'], 'unverified')
                else:
                    self.assertNotIn('agent_tool', live['android'])
                    self.assertNotIn('agent_apps', live['android'])

    def test_warm_tone_reaches_live_and_full_context_without_widening_autonomy(self):
        before = self.snapshot()
        for scope in ('private:101', 'group:202'):
            full = self.module.build_context(str(self.root), 'fixture', scope, process_probe=lambda _: {})
            live = self.module.build_context(str(self.root), 'fixture', scope, context_mode='live_chat')
            self.assertEqual(full['tools']['qq_expression']['tone'], live['tools']['qq_expression']['tone'])
            self.assertIn('温和', live['tools']['qq_expression']['tone']['voice'])
            self.assertIn('Expression preference only', live['tools']['qq_expression']['tone']['authority'])
            initiative = full['tools']['entertainment']['self_direction']
            self.assertIn('confirmed progress', initiative['initiative'])
            self.assertIn('one-hour', initiative['boundaries'])
            self.assertIn('wait-for-live-reply', initiative['boundaries'])
            self.assertIn('00:00-08:00', initiative['boundaries'])
            self.assertIn('No additional agent', initiative['boundaries'])
            self.assertLess(len(json.dumps(live)), 20000)
            self.assertNotIn('account_activity', live['tools'])
            self.assertIs(type(live['tools']['qq_autonomy']['account_updates']), bool)
        self.assertEqual(self.snapshot(), before)

    def test_account_guide_uses_current_settings_and_only_the_primary_private_agent(self):
        config = {'qqBackend': {'provider': 'snowluma', 'accountActivity': {'enabled': True}}}
        guide = self.module.account_activity_guide(config, True)
        self.assertTrue(guide['available']); self.assertTrue(guide['settings']['post_supported'])
        self.assertIn('same main LLM', guide['choice'])
        self.assertIn('unknown', guide['protocol'])
        self.assertFalse(self.module.account_activity_guide(config, False)['available'])
        config['qqBackend']['provider'] = 'napcat'
        self.assertFalse(self.module.account_activity_guide(config, True)['settings']['post_supported'])
        config['qqBackend']['accountActivity'] = None
        self.assertFalse(self.module.account_activity_guide(config, True)['available'])


if __name__ == "__main__":
    unittest.main()
