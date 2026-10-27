"""Synthesize local audio for the existing main agent. Never sends QQ messages.

UTF-8 stdin JSON is bound by the trusted OneBot adapter. A request ID has one
durable outcome; an interrupted or uncertain request is not silently replayed.
"""
from contextlib import contextmanager
import hashlib
import http.client
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid
import wave


SCHEMA = "dsh-voice-result/v1"
MAX_AUDIO = 20 * 1024 * 1024
MAX_INPUT = 32 * 1024


class VoiceError(ValueError):
    pass


class VoiceBusy(VoiceError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def unique_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise VoiceError("Duplicate JSON fields")
        result[key] = value
    return result


def plain_path(path, *, required=False):
    path = Path(os.path.abspath(path))
    for component in [*reversed(path.parents), path]:
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise VoiceError("Linked paths are not supported")
    if required and not path.exists():
        raise VoiceError("Required local voice resource is missing")
    return path


def bounded_json(path):
    path = plain_path(path, required=True)
    if not path.is_file() or path.stat().st_size > 256 * 1024:
        raise VoiceError("Invalid local voice document")
    value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_pairs)
    if not isinstance(value, dict):
        raise VoiceError("Invalid local voice document")
    return value


def resource_bytes(path, limit=MAX_AUDIO):
    path = plain_path(path, required=True)
    if not path.is_file() or not 1 <= path.stat().st_size <= limit:
        raise VoiceError("Invalid bounded local voice resource")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if not 1 <= len(raw) <= limit:
        raise VoiceError("Oversized local voice resource")
    return raw


def atomic_json(path, value):
    path = plain_path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write((canonical(value) + "\n").encode("utf-8"))
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def request_lock(path):
    path = plain_path(path)
    with path.open("a+b") as stream:
        if not path.stat().st_size:
            stream.write(b"\0"); stream.flush()
        stream.seek(0)
        acquired = False
        try:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                raise VoiceBusy("Voice request is already being processed") from None
            yield
        finally:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_UN)


def context(request):
    allowed = {"cwd", "agent_preset", "conversation_key", "request_id", "text", "speed"}
    if not isinstance(request, dict) or set(request) - allowed or not allowed - {"speed"} <= set(request):
        raise VoiceError("Invalid voice request fields")
    if (not isinstance(request["cwd"], str) or not os.path.isabs(request["cwd"]) or
            not isinstance(request["agent_preset"], str) or
            not isinstance(request["conversation_key"], str) or
            not re.fullmatch(r"(?:private|group):[1-9][0-9]*", request["conversation_key"]) or
            not isinstance(request["request_id"], str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", request["request_id"]) or
            not isinstance(request["text"], str) or not request["text"].strip() or len(request["text"]) > 600):
        raise VoiceError("Invalid voice request identity or text")
    speed = request.get("speed", 1.0)
    if isinstance(speed, bool) or not isinstance(speed, (float, int)) or not math.isfinite(speed) or not 0.5 <= speed <= 2:
        raise VoiceError("Invalid voice speed")
    root = plain_path(request["cwd"], required=True)
    if not root.is_dir():
        raise VoiceError("Existing trusted workspace required")
    explicit = os.environ.get("LANYA_PROJECT_CONFIG")
    if explicit:
        config = plain_path(os.path.expanduser(explicit), required=True)
        if not config.is_relative_to(root):
            raise VoiceError("Configuration belongs to another workspace")
    else:
        candidates = [root / "napcat/config/project.json", root / "config/project.json"]
        config = next((path for path in candidates if os.path.lexists(path)), None)
        if config is None:
            raise VoiceError("Explicit workspace project configuration required")
    project = bounded_json(config)
    name = project.get("dataDirName")
    if (not isinstance(name, str) or not name.strip() or name in (".", "..") or
            any(c in name for c in '/\\:\0') or name.rstrip(". ") != name):
        raise VoiceError("Invalid configured data directory")
    tts = project.get("tts", {})
    if not isinstance(tts, dict):
        raise VoiceError("Invalid local TTS configuration")
    port = project.get("ttsPort", 9880)
    if type(port) is not int or not 1 <= port <= 65535:
        raise VoiceError("Invalid local TTS port")
    reference = tts.get("refAudioPath", "tts/ref/ref-main.wav")
    if not isinstance(reference, str) or not reference:
        raise VoiceError("Invalid local reference audio")
    reference = plain_path(root / reference, required=True)
    if not reference.is_relative_to(root) or not reference.is_file() or not 1 <= reference.stat().st_size <= MAX_AUDIO:
        raise VoiceError("Reference audio must remain in the trusted workspace")
    prompt = tts.get("promptText", "")
    if not isinstance(prompt, str) or len(prompt) > 2000:
        raise VoiceError("Invalid reference prompt")
    owner = {"workspace_key": str(root), "agent_key": request["agent_preset"].strip()}
    if os.name == "nt":
        owner = {key: value.lower() for key, value in owner.items()}
    directory = plain_path(root / name / "voice")
    payload = dict(text=request["text"], text_lang="zh", ref_audio_path=str(reference), prompt_text=prompt,
                   prompt_lang="zh", top_k=15, top_p=1.0, temperature=1.0, text_split_method="cut5", batch_size=1,
                   speed_factor=float(speed), seed=0, parallel_infer=True, repetition_penalty=1.35, ref_free=not prompt)
    input_sha = digest(dict(owner=owner, conversation_key=request["conversation_key"], request_id=request["request_id"],
                            port=port, payload=payload, reference_sha256=hashlib.sha256(resource_bytes(reference)).hexdigest()))
    key = digest(dict(owner=owner, conversation_key=request["conversation_key"], request_id=request["request_id"]))
    return dict(owner=owner, directory=directory, port=port, payload=payload, input_sha256=input_sha, key=key)


def validate_audio(raw):
    if (not isinstance(raw, bytes) or not 44 <= len(raw) <= MAX_AUDIO or raw[:4] != b"RIFF" or
            raw[8:12] != b"WAVE" or int.from_bytes(raw[4:8], "little") != len(raw) - 8):
        raise VoiceError("Local TTS did not return a complete WAV")
    # wave validates sample decoding but does not cross-check these PCM header
    # fields. Reject malformed alignment/rate metadata before a file is ready.
    offset, formats = 12, 0
    while offset < len(raw):
        if offset + 8 > len(raw):
            raise VoiceError("Truncated WAV chunk header")
        name, size = raw[offset:offset + 4], int.from_bytes(raw[offset + 4:offset + 8], "little")
        end = offset + 8 + size
        if end > len(raw):
            raise VoiceError("Truncated WAV chunk")
        if name == b"fmt ":
            formats += 1
            fmt = raw[offset + 8:end]
            if formats != 1 or len(fmt) < 16 or int.from_bytes(fmt[:2], "little") != 1:
                raise VoiceError("A single standard PCM format chunk is required")
            channels = int.from_bytes(fmt[2:4], "little")
            rate = int.from_bytes(fmt[4:8], "little")
            byte_rate = int.from_bytes(fmt[8:12], "little")
            alignment = int.from_bytes(fmt[12:14], "little")
            bits = int.from_bytes(fmt[14:16], "little")
            if (bits not in (8, 16, 24, 32) or channels not in (1, 2) or
                    alignment != channels * (bits // 8) or byte_rate != rate * alignment):
                raise VoiceError("Inconsistent PCM alignment or byte rate")
        offset = end if end == len(raw) else end + (size & 1)
    if formats != 1:
        raise VoiceError("PCM format chunk is missing")
    try:
        with wave.open(io.BytesIO(raw), "rb") as audio:
            channels, width, rate, frames = audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()
            if audio.getcomptype() != "NONE" or channels not in (1, 2) or width not in (1, 2, 3, 4) or not 8000 <= rate <= 96000:
                raise VoiceError("Unsupported local WAV format")
            if not 0 < frames / rate <= 120 or len(audio.readframes(frames + 1)) != frames * width * channels:
                raise VoiceError("Truncated or oversized local WAV")
            return frames / rate
    except (wave.Error, EOFError):
        raise VoiceError("Invalid local WAV") from None


def fetch_audio(port, payload):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=45)
    try:
        connection.request("POST", "/tts", canonical(payload).encode("utf-8"), {"Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise VoiceError("Local TTS returned an error")
        raw = response.read(MAX_AUDIO + 1)
        if len(raw) > MAX_AUDIO:
            raise VoiceError("Oversized local TTS output")
        return raw
    finally:
        connection.close()


def synthesize(request, *, fetch=fetch_audio):
    info = context(request)
    directory = info["directory"]
    directory.mkdir(parents=True, exist_ok=True)
    ledger = plain_path(directory / ".requests")
    ledger.mkdir(exist_ok=True)
    key = info["key"]
    metadata = ledger / (key + ".json")
    output = directory / ("voice-" + key + ".wav")
    base = dict(schema=SCHEMA, owner=info["owner"], conversation_key=request["conversation_key"],
                request_id=request["request_id"], delivered=False)
    with request_lock(ledger / (key + ".lock")):
        if metadata.exists():
            saved = bounded_json(metadata)
            if (saved.get("version") != 1 or saved.get("input_sha256") != info["input_sha256"] or saved.get("base") != base or
                    saved.get("state") not in ("reserved", "ready", "error", "unknown")):
                raise VoiceError("Voice request identity changed or ledger is corrupt")
            if saved["state"] == "ready":
                audio = resource_bytes(output)
                duration = validate_audio(audio)
                result = dict(base, status="ready", file=str(output), sha256=hashlib.sha256(audio).hexdigest(),
                              bytes=len(audio), duration_seconds=duration)
                if result != saved.get("result"):
                    raise VoiceError("Cached local audio changed")
                return result
            return dict(base, status="unknown" if saved["state"] == "reserved" else saved["state"],
                        code="previous_request_not_replayed", file=None)
        if os.path.lexists(output):
            raise VoiceError("Unclaimed audio file exists; manual review required")
        saved = dict(version=1, input_sha256=info["input_sha256"], base=base, state="reserved", created_at=int(time.time() * 1000))
        atomic_json(metadata, saved)
        try:
            raw = fetch(info["port"], info["payload"])
            duration = validate_audio(raw)
            temporary = output.with_name(output.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                plain_path(output)
                if os.path.lexists(output):
                    raise VoiceError("Voice output appeared during synthesis")
                # Publish without replacement, including a file created by
                # another writer between the check and this operation.
                if os.name == "nt":
                    os.rename(temporary, output)
                else:
                    os.link(temporary, output)
                    temporary.unlink()
            finally:
                if temporary.exists(): temporary.unlink()
            result = dict(base, status="ready", file=str(output), sha256=hashlib.sha256(raw).hexdigest(),
                          bytes=len(raw), duration_seconds=duration)
            saved.update(state="ready", result=result)
            atomic_json(metadata, saved)
            return result
        except VoiceError:
            saved.update(state="error", code="local_synthesis_failed")
            atomic_json(metadata, saved)
            return dict(base, status="error", code="local_synthesis_failed", file=None)
        except (OSError, http.client.HTTPException):
            saved.update(state="unknown", code="uncertain_local_synthesis")
            atomic_json(metadata, saved)
            return dict(base, status="unknown", code="uncertain_local_synthesis", file=None)


def main(argv=None, stdin=None, stdout=None, stderr=None):
    argv = sys.argv[1:] if argv is None else argv
    stdin = getattr(sys.stdin, "buffer", sys.stdin) if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        if argv != ["--stdin-json"]:
            raise VoiceError("Expected --stdin-json")
        raw = stdin.read(MAX_INPUT + 1)
        if isinstance(raw, str): raw = raw.encode("utf-8")
        if len(raw) > MAX_INPUT:
            raise VoiceError("Oversized voice request")
        request = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
        stdout.write(canonical(synthesize(request)) + "\n")
        return 0
    except (VoiceError, OSError, ValueError, TypeError, KeyError) as error:
        stderr.write(type(error).__name__ + "\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
