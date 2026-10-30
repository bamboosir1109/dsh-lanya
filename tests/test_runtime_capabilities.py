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


if __name__ == "__main__":
    unittest.main()
