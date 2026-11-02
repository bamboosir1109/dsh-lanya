"""Voice status is metadata only; fixtures forbid synthesis and service I/O."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import test_panel_autonomy as panel_fixture

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("voice_status_fixture", REPO / "scripts/voice_status.py")
voice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voice)


class VoiceStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="voice status fixture ")
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name)
        (self.runtime / "voice_executor.py").write_text("# metadata fixture only\n")
        self.components = [{"tag": "tts", "count": 1, "private": "DO NOT PROJECT"},
                           {"tag": "voicedaemon", "count": 0}]

    def snapshot(self, **changes):
        options = dict(runtime_dir=self.runtime, components=self.components, mode="unified",
                       port=9880, observed_at=999000, now=1000.0)
        options.update(changes)
        return voice.voice_status(**options)

    def test_process_present_never_claims_synthesis_or_delivery_and_reads_no_audio(self):
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("content read forbidden")):
            value = self.snapshot()
        self.assertEqual(value["execution"], "main_agent_on_demand")
        self.assertIs(value["daemon_required"], False)
        self.assertIs(value["executor"]["installed"], True)
        self.assertEqual(value["engine"]["process_count"], 1)
        self.assertEqual(value["engine"]["observation_state"], "fresh")
        self.assertEqual(value["synthesis"], {"state": "not_probed"})
        self.assertEqual(value["delivery"], {"state": "unverified"})
        self.assertTrue(value["read_only"])
        self.assertNotIn("healthy", value["engine"])
        self.assertNotIn("DO NOT PROJECT", json.dumps(value))
        self.assertNotIn(str(self.runtime), json.dumps(value))

    def test_absent_process_is_observation_not_a_failed_synthesis(self):
        value = self.snapshot(components=[{"tag": "tts", "count": 0}])
        self.assertEqual(value["engine"]["process_count"], 0)
        self.assertEqual(value["synthesis"]["state"], "not_probed")
        (self.runtime / "voice_executor.py").unlink()
        self.assertIs(self.snapshot()["executor"]["installed"], False)

    def test_stale_and_malformed_observations_are_distinct_from_zero(self):
        self.assertEqual(self.snapshot(observed_at=800000)["engine"]["observation_state"], "stale")
        self.assertEqual(self.snapshot(observed_at="PRIVATE BAD TIMESTAMP")["engine"]["observation_state"], "unavailable")
        for rows in ([], [{"tag": "tts", "count": True}], [{"tag": "tts", "count": -1}],
                     [{"tag": "tts", "count": 1}, {"tag": "tts", "count": 1}]):
            self.assertIsNone(self.snapshot(components=rows)["engine"]["process_count"])
        self.assertIsNone(self.snapshot(port=True)["engine"]["port"])

    def test_legacy_and_unknown_modes_are_explicit(self):
        legacy = self.snapshot(mode="legacy")
        self.assertEqual(legacy["execution"], "independent_queue")
        self.assertIs(legacy["daemon_required"], True)
        unknown = self.snapshot(mode="bad-mode")
        self.assertEqual(unknown["execution"], "unknown")
        self.assertIsNone(unknown["daemon_required"])

    def test_powershell_seven_fractional_digit_timestamp_is_fresh_on_python39(self):
        stamp = "2026-11-03T02:00:00.1234567+08:00"
        value = self.snapshot(observed_at=stamp, now=1793642401.0)
        self.assertEqual(value["engine"]["observed_at"], stamp)
        self.assertEqual(value["engine"]["observation_state"], "fresh")

    def test_non_file_or_relative_executor_is_not_marked_installed(self):
        path = self.runtime / "voice_executor.py"
        path.unlink(); path.mkdir()
        self.assertIsNone(self.snapshot()["executor"]["installed"])
        self.assertIsNone(self.snapshot(runtime_dir="relative-path")["executor"]["installed"])


class PanelVoiceStatusTests(unittest.TestCase):
    setUp = panel_fixture.PanelAutonomyTests.setUp
    write = panel_fixture.PanelAutonomyTests.write
    route = panel_fixture.PanelAutonomyTests.route

    def services(self):
        return {role: {"role": role, "port": port, "count": 1, "mb": 1,
                       "healthy": True, "listening": True, "status": "healthy"}
                for role, port in (("works", 19388), ("api", 19389))}

    def state(self):
        return {"at": 999000, "memory": {}, "components": [
            {"tag": "groupwatch", "count": 1}, {"tag": "voicedaemon", "count": 0},
            {"tag": "tts", "count": 1}, {"tag": "browserd", "count": 1}]}

    def test_unified_state_and_route_share_projection_without_retired_control(self):
        self.write(self.data / "made/lanya-state.json", self.state())
        (self.napcat / "voice_executor.py").write_text("# inert executor\n")
        with mock.patch.object(self.module, "panel_services", return_value=self.services()):
            state = self.route("/api/state")[1]
            code, payload = self.route("/api/voice-status")
        self.assertEqual(code, 200)
        self.assertEqual(payload["voice"], state["voice"])
        self.assertEqual([row["tag"] for row in state["components"]],
                         ["groupwatch", "tts", "browserd", "works", "panel-api"])
        self.assertEqual(state["voice"]["engine"]["process_count"], 1)
        self.assertEqual(state["voice"]["synthesis"]["state"], "not_probed")
        self.assertNotIn(str(self.root), json.dumps(state["voice"]))

    def test_legacy_component_row_and_controls_are_preserved(self):
        self.write(self.data / "made/lanya-state.json", self.state())
        with mock.patch.object(self.module, "decision_mode", return_value="legacy"), \
                mock.patch.object(self.module, "autonomy_snapshot", return_value={"mode": "legacy"}), \
                mock.patch.object(self.module, "panel_services", return_value=self.services()):
            state = self.module.panel_state_snapshot()
        self.assertEqual(state["voice"]["execution"], "independent_queue")
        row = next(row for row in state["components"] if row["tag"] == "voicedaemon")
        self.assertNotIn("control", row)

    def test_dedicated_voice_route_does_not_read_autonomy_queues_or_probe_services(self):
        self.write(self.data / "made/lanya-state.json", self.state())
        with mock.patch.object(self.module, "panel_state_snapshot", side_effect=AssertionError("full state forbidden")), \
                mock.patch.object(self.module, "autonomy_snapshot", side_effect=AssertionError("queue read forbidden")), \
                mock.patch.object(self.module, "panel_services", side_effect=AssertionError("service I/O forbidden")):
            code, value = self.route("/api/voice-status")
            direct = self.module.voice_status_snapshot()
        self.assertEqual(code, 200)
        self.assertEqual(value["voice"], direct)
        self.assertEqual(direct["engine"]["process_count"], 1)
        self.assertEqual(direct["synthesis"]["state"], "not_probed")

    def test_projection_failure_is_unknown_and_never_changes_controls(self):
        self.write(self.data / "made/lanya-state.json", self.state())
        with mock.patch("voice_status.voice_status", side_effect=ValueError("PRIVATE INTERNAL DETAIL")), \
                mock.patch.object(self.module, "panel_services", return_value=self.services()):
            state = self.module.panel_state_snapshot()
        self.assertEqual(state["voice"]["mode"], "unknown")
        self.assertIsNone(state["voice"]["engine"]["process_count"])
        self.assertNotIn("voicedaemon", [row["tag"] for row in state["components"]])
        self.assertNotIn("PRIVATE INTERNAL DETAIL", json.dumps(state["voice"]))


if __name__ == "__main__":
    unittest.main()
