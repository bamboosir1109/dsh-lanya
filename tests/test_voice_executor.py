"""Local fixtures only: no real TTS, QQ, model, or private data."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import wave


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/voice_executor.py"
spec = importlib.util.spec_from_file_location("voice_executor_fixture", SCRIPT)
voice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voice)


def wav_bytes():
    target = io.BytesIO()
    with wave.open(target, "wb") as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 1600)
    return target.getvalue()


class VoiceExecutorTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="voice executor fixture ")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.config = self.root / "napcat/config/project.json"
        self.config.parent.mkdir(parents=True)
        self.reference = self.root / "tts/ref/ref-main.wav"
        self.reference.parent.mkdir(parents=True)
        self.reference.write_bytes(wav_bytes())
        self.config.write_text(json.dumps({"dataDirName": "role", "ttsPort": 9880}), encoding="utf-8")
        self.environment = mock.patch.dict(os.environ)
        self.environment.start(); self.addCleanup(self.environment.stop)
        os.environ.pop("LANYA_PROJECT_CONFIG", None)
        self.request = dict(cwd=str(self.root), agent_preset="  Fixture-Preset  ",
                            conversation_key="private:101", request_id="fixture-call-1", text="synthetic spoken fixture")
        self.calls = []

    def fetch(self, port, payload):
        self.calls.append((port, payload))
        return wav_bytes()

    def test_ready_is_only_local_audio_and_same_request_reuses_verified_bytes(self):
        first = voice.synthesize(self.request, fetch=self.fetch)
        second = voice.synthesize(self.request, fetch=self.fetch)
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(first["status"], "ready")
        self.assertFalse(first["delivered"])
        self.assertEqual(Path(first["file"]).read_bytes(), wav_bytes())
        self.assertEqual(first["sha256"], hashlib.sha256(wav_bytes()).hexdigest())
        self.assertEqual(first["duration_seconds"], 0.1)
        self.assertEqual(first["owner"]["agent_key"], "fixture-preset" if os.name == "nt" else "Fixture-Preset")
        metadata = next((self.root / "role/voice/.requests").glob("*.json"))
        self.assertNotIn(self.request["text"], metadata.read_text(encoding="utf-8"))

    def test_same_request_id_cannot_change_text_reference_speed_or_config(self):
        voice.synthesize(self.request, fetch=self.fetch)
        for change in ({"text": "a different synthetic fact"}, {"speed": 1.5}):
            with self.subTest(change=change), self.assertRaises(voice.VoiceError):
                voice.synthesize(dict(self.request, **change), fetch=self.fetch)
        self.reference.write_bytes(wav_bytes() + b"different reference evidence")
        with self.assertRaises(voice.VoiceError): voice.synthesize(self.request, fetch=self.fetch)
        self.assertEqual(len(self.calls), 1)

    def test_same_id_in_different_conversation_is_a_distinct_local_request(self):
        first = voice.synthesize(self.request, fetch=self.fetch)
        second = voice.synthesize(dict(self.request, conversation_key="group:202"), fetch=self.fetch)
        self.assertNotEqual(first["file"], second["file"])
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(second["delivered"])

    def test_input_authority_paths_and_nonfinite_speed_are_rejected_before_network(self):
        changes = [{"scope": "group:202"}, {"endpoint": "https://fixture.invalid"}, {"cwd": "relative"},
                   {"conversation_key": "private:01"}, {"request_id": "../escape"}, {"text": " "},
                   {"text": "x" * 601}, {"speed": True}, {"speed": float("nan")}, {"speed": 0.4}, {"speed": 2.1}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(voice.VoiceError):
                voice.synthesize(dict(self.request, **change), fetch=self.fetch)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / "role/voice").exists())

    def test_invalid_preferred_configuration_never_falls_back_to_legacy(self):
        fallback = self.root / "config/project.json"
        fallback.parent.mkdir(); fallback.write_text('{"dataDirName":"other"}', encoding="utf-8")
        for raw in ("{broken", "[]", "null", '{"dataDirName":"role","dataDirName":"other"}'):
            self.config.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw), self.assertRaises((ValueError, voice.VoiceError)):
                voice.synthesize(self.request, fetch=self.fetch)
        self.assertEqual(self.calls, [])

    def test_foreign_explicit_configuration_and_reference_paths_are_rejected(self):
        other = tempfile.TemporaryDirectory(prefix="foreign voice fixture ")
        self.addCleanup(other.cleanup)
        outside = Path(other.name) / "project.json"
        outside.write_text('{"dataDirName":"foreign"}', encoding="utf-8")
        os.environ["LANYA_PROJECT_CONFIG"] = str(outside)
        with self.assertRaises(voice.VoiceError): voice.synthesize(self.request, fetch=self.fetch)
        os.environ.pop("LANYA_PROJECT_CONFIG")
        self.config.write_text(json.dumps({"dataDirName": "role", "tts": {"refAudioPath": str(outside)}}), encoding="utf-8")
        with self.assertRaises(voice.VoiceError): voice.synthesize(self.request, fetch=self.fetch)
        self.assertEqual(self.calls, [])

    def test_missing_reference_and_invalid_data_directory_cannot_create_output(self):
        self.reference.unlink()
        with self.assertRaises(voice.VoiceError): voice.synthesize(self.request, fetch=self.fetch)
        self.reference.write_bytes(wav_bytes())
        for name in ("..", "../other", "x\\other", "role. ", ""):
            self.config.write_text(json.dumps({"dataDirName": name}), encoding="utf-8")
            with self.subTest(name=name), self.assertRaises(voice.VoiceError):
                voice.synthesize(self.request, fetch=self.fetch)
        self.assertFalse((self.root / "role/voice").exists())

    def test_uncertain_transport_is_durable_and_never_replayed(self):
        calls = []
        def uncertain(port, payload):
            calls.append(1)
            raise ConnectionResetError("fixture private service error")
        first = voice.synthesize(self.request, fetch=uncertain)
        second = voice.synthesize(self.request, fetch=uncertain)
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(second["status"], "unknown")
        self.assertEqual(calls, [1])
        self.assertNotIn("fixture private", json.dumps(first))

    def test_interrupted_reservation_stays_unknown_without_new_synthesis(self):
        def interrupted(port, payload): raise RuntimeError("simulate abrupt worker death")
        with self.assertRaises(RuntimeError): voice.synthesize(self.request, fetch=interrupted)
        result = voice.synthesize(self.request, fetch=self.fetch)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.calls, [])

    def test_known_service_error_is_not_a_fallback_or_automatic_retry(self):
        calls = []
        def rejected(port, payload): calls.append(1); raise voice.VoiceError("fixture rejected")
        self.assertEqual(voice.synthesize(self.request, fetch=rejected)["status"], "error")
        self.assertEqual(voice.synthesize(self.request, fetch=rejected)["status"], "error")
        self.assertEqual(calls, [1])
        self.assertFalse(list((self.root / "role/voice").glob("*.wav")))

    def test_non_audio_truncated_and_wrong_riff_lengths_are_rejected(self):
        samples = [b"not audio", wav_bytes()[:-2], wav_bytes() + b"extra", b"RIFF" + b"\0" * 40]
        for index, raw in enumerate(samples):
            request = dict(self.request, request_id=f"invalid-audio-{index}")
            result = voice.synthesize(request, fetch=lambda *_: raw)
            self.assertEqual(result["status"], "error")
            self.assertFalse(result["delivered"])

    def test_pcm_alignment_and_byte_rate_must_match_decoded_samples(self):
        self.assertEqual(voice.validate_audio(wav_bytes()), 0.1)
        for index, (offset, width, value) in enumerate(((32, 2, 0), (32, 2, 1), (28, 4, 1))):
            raw = bytearray(wav_bytes())
            raw[offset:offset + width] = value.to_bytes(width, "little")
            with self.subTest(offset=offset, value=value), self.assertRaises(voice.VoiceError):
                voice.validate_audio(bytes(raw))
            result = voice.synthesize(dict(self.request, request_id=f"bad-pcm-{index}"), fetch=lambda *_: bytes(raw))
            self.assertEqual(result["status"], "error")
            self.assertFalse(result["delivered"])

    def test_modified_audio_or_ledger_fails_closed_without_regeneration(self):
        result = voice.synthesize(self.request, fetch=self.fetch)
        Path(result["file"]).write_bytes(b"tampered")
        with self.assertRaises(voice.VoiceError): voice.synthesize(self.request, fetch=self.fetch)
        self.assertEqual(len(self.calls), 1)
        metadata = next((self.root / "role/voice/.requests").glob("*.json"))
        metadata.write_text("{}", encoding="utf-8")
        with self.assertRaises(voice.VoiceError): voice.synthesize(self.request, fetch=self.fetch)
        self.assertEqual(len(self.calls), 1)

    def test_live_os_lock_refuses_a_second_synthesizer(self):
        info = voice.context(self.request)
        directory = info["directory"] / ".requests"
        directory.mkdir(parents=True)
        with voice.request_lock(directory / (info["key"] + ".lock")):
            with self.assertRaises(voice.VoiceBusy): voice.synthesize(self.request, fetch=self.fetch)
        self.assertEqual(self.calls, [])

    def test_actual_local_http_contract_returns_a_real_wav_and_no_send(self):
        observed = []
        class FixtureHandler(BaseHTTPRequestHandler):
            def do_POST(handler):
                observed.append((handler.path, json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))))
                audio = wav_bytes()
                handler.send_response(200); handler.send_header("Content-Length", str(len(audio)))
                handler.end_headers(); handler.wfile.write(audio)
            def log_message(*_): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            self.config.write_text(json.dumps({"dataDirName": "role", "ttsPort": server.server_port}), encoding="utf-8")
            result = voice.synthesize(self.request)
            self.assertEqual(result["status"], "ready")
            self.assertFalse(result["delivered"])
            self.assertEqual(observed[0][0], "/tts")
            self.assertEqual(observed[0][1]["text"], self.request["text"])
            self.assertTrue(observed[0][1]["ref_free"])
        finally:
            server.shutdown(); server.server_close(); worker.join(timeout=5)

    def test_cli_never_echoes_invalid_input_or_service_error_text(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = voice.main(["--stdin-json"], stdin=io.BytesIO(b'{"private-voice-text":'), stdout=out, stderr=err)
        self.assertEqual(status, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertNotIn("private-voice-text", err.getvalue())

    def test_duplicate_json_cannot_shadow_trusted_request_fields(self):
        out, err = io.StringIO(), io.StringIO()
        raw = b'{"cwd":"foreign-workspace","cwd":"current-workspace"}'
        with mock.patch.object(voice, "synthesize") as called:
            status = voice.main(["--stdin-json"], stdin=io.BytesIO(raw), stdout=out, stderr=err)
        self.assertEqual(status, 1)
        called.assert_not_called()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "VoiceError\n")


if __name__ == "__main__": unittest.main()
