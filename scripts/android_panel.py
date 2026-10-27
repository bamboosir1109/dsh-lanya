"""Local panel adapter for androidd's single-slot queue. Never invokes ADB."""
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import time
import uuid

from androidd import AndroidError, atomic_json, verify_png, worker_lock
from project_conf import data_dir, get


class PanelAndroidError(ValueError):
    def __init__(self, code, message, http_status=409):
        super().__init__(message)
        self.code, self.http_status = code, http_status


def worker_running(path):
    """Probe the OS lock, never mistake a leftover lock file for a worker."""
    try:
        stream = Path(path).open("r+b")
    except FileNotFoundError:
        return False
    with stream:
        locked = False
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            return False
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return True
            raise
        finally:
            if locked:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)


class AndroidPanel:
    ACTION_FIELDS = {
        "shot": set(), "tap": {"x", "y"}, "longtap": {"x", "y", "ms"},
        "swipe": {"x1", "y1", "x2", "y2", "ms"}, "text": {"text"},
        "key": {"key"}, "start": {"pkg"}, "shell": {"cmd"},
    }

    def __init__(self, *, directory=None, serial=None, max_age=None, now=time.time,
                 is_running=worker_running):
        self.directory = Path(directory) if directory is not None else Path(data_dir()) / "android"
        self.serial = serial if serial is not None else str(get("android.serial", "emulator-5556")).strip()
        self.max_age = float(max_age if max_age is not None else get("android.maxShotAgeSeconds", 30))
        if (not isinstance(self.serial, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", self.serial)
                or not math.isfinite(self.max_age) or not 0 < self.max_age <= 300):
            raise PanelAndroidError("configuration", "Invalid configured Android target", 503)
        self.now, self.is_running = now, is_running

    def _target(self, serial):
        if serial != self.serial:
            raise PanelAndroidError("wrong_serial", "Explicit serial must match the configured device", 400)

    def _json(self, name, *, optional=False):
        try:
            path = self.directory / name
            if path.stat().st_size > 256 * 1024:
                raise ValueError("Oversized state")
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict):
                raise ValueError("Invalid state")
            return value
        except FileNotFoundError:
            if optional:
                return {}
            raise PanelAndroidError("no_frame", "No completed Android screenshot") from None
        except (OSError, ValueError):
            raise PanelAndroidError("invalid_state", "Android queue state is unreadable") from None

    def _base(self, status, code, error, **extra):
        return dict(ok=False, status=status, serial=self.serial, code=code, error=error,
                    shot_id="", max_age=self.max_age, **extra)

    @staticmethod
    def _sequence(state):
        seq = state.get("seq", 0)
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise PanelAndroidError("invalid_state", "Invalid Android request sequence")
        return seq

    def _snapshot(self):
        """Validate one complete result; old PNGs never act as a fallback."""
        if not self.is_running(self.directory / "worker.lock"):
            raise PanelAndroidError("worker_offline", "Android worker is offline", 503)
        result = self._json("result.json")
        request = self._json("panel-request.json", optional=True)
        result_seq, request_seq = self._sequence(result), self._sequence(request)
        if request.get("publish_failed"):
            raise PanelAndroidError("queue_failed", "Queue publication failed; capture again")
        if ((self.directory / "cmd.json").exists() or any(self.directory.glob("cmd.claim-*.json"))):
            raise PanelAndroidError("pending", "Android worker is processing a request")
        if request and result_seq != request_seq:
            timed_out = self.now() - request.get("submitted_at", self.now()) > 120
            code = "request_timeout" if timed_out else "pending" if result_seq < request_seq else "view_changed"
            message = ("Waiting for the requested screenshot" if code == "pending" else
                       "Android request timed out; capture again" if code == "request_timeout" else
                       "Another producer changed the Android view; capture again")
            raise PanelAndroidError(code, message)
        if result.get("serial") != self.serial:
            raise PanelAndroidError("wrong_serial", "Android result belongs to another serial")
        if result.get("ok") is not True or result.get("status") != "ok" or not result.get("shot_id"):
            raise PanelAndroidError(str(result.get("code") or "capture_failed"),
                                    str(result.get("note") or "Android screenshot failed")[:400],
                                    503 if result.get("status") == "offline" else 409)
        # A disk image from before opening the panel is not a new observation.
        if not request or request.get("serial") != self.serial:
            raise PanelAndroidError("capture_required", "Capture a new screenshot to begin")
        meta = self._json("shot-meta.json")
        fields = ("serial", "shot_id", "width", "height", "captured_at", "sha256")
        if (any(result.get(key) != meta.get(key) for key in fields) or
                not re.fullmatch(r"[0-9a-f]{32}", str(meta.get("shot_id", "")))):
            raise PanelAndroidError("stale_view", "Screenshot metadata changed; capture again")
        captured = meta.get("captured_at")
        if isinstance(captured, bool) or not isinstance(captured, (int, float)) or not math.isfinite(captured):
            raise PanelAndroidError("stale_view", "Invalid screenshot time")
        age = self.now() - captured
        if not 0 <= age <= self.max_age:
            raise PanelAndroidError("stale_view", "Screenshot expired; capture again")
        try:
            path = self.directory / "shot.png"
            if path.stat().st_size > 64 * 1024 * 1024:
                raise ValueError("Oversized screenshot")
            raw = path.read_bytes()
            width, height = verify_png(raw)
        except (OSError, ValueError, AndroidError):
            raise PanelAndroidError("invalid_frame", "Screenshot PNG cannot be verified") from None
        if (hashlib.sha256(raw).hexdigest() != meta.get("sha256") or
                (width, height) != (meta.get("width"), meta.get("height"))):
            raise PanelAndroidError("stale_view", "Screenshot bytes changed; capture again")
        # The worker writes PNG, metadata and result separately. Detect concurrent updates.
        if self._json("result.json") != result or self._json("shot-meta.json") != meta:
            raise PanelAndroidError("stale_view", "Screenshot changed during validation")
        return result, raw, age

    def status(self, serial=None):
        if serial is not None:
            self._target(serial)
        try:
            result, _, age = self._snapshot()
            return dict(ok=True, status="observed", serial=self.serial, seq=result["seq"],
                        shot_id=result["shot_id"], width=result["width"], height=result["height"],
                        captured_at=result["captured_at"], age_seconds=age, max_age=self.max_age,
                        note=str(result.get("note") or "")[:400])
        except PanelAndroidError as error:
            state = "pending" if error.code == "pending" else "offline" if error.http_status == 503 else "error"
            try:
                seq = self._sequence(self._json("panel-request.json", optional=True))
            except PanelAndroidError:
                seq = 0
            return self._base(state, error.code, str(error), seq=seq)

    def image(self, serial, shot_id):
        self._target(serial)
        result, raw, _ = self._snapshot()
        if not isinstance(shot_id, str) or shot_id != result["shot_id"]:
            raise PanelAndroidError("stale_view", "Requested screenshot is no longer current")
        return raw

    def _job(self, body):
        if not isinstance(body, dict):
            raise PanelAndroidError("bad_job", "Android job must be an object", 400)
        self._target(body.get("serial"))
        action = body.get("action")
        if not isinstance(action, str) or action not in self.ACTION_FIELDS:
            raise PanelAndroidError("bad_job", "Unknown Android worker action", 400)
        allowed = self.ACTION_FIELDS[action] | {"serial", "action", "expected_shot_id"}
        if set(body) - allowed:
            raise PanelAndroidError("bad_job", "Unexpected Android job fields", 400)
        job = dict(body)
        if action == "shot":
            return job
        result, _, _ = self._snapshot()
        if body.get("expected_shot_id") != result["shot_id"]:
            raise PanelAndroidError("stale_view", "Action requires the displayed screenshot ID")
        for name in self.ACTION_FIELDS[action] & {"x", "x1", "x2", "y", "y1", "y2"}:
            value = body.get(name)
            maximum = result["width"] if name.startswith("x") else result["height"]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < maximum:
                raise PanelAndroidError("bad_job", "Coordinates must be inside the displayed screenshot", 400)
        if action in ("longtap", "swipe"):
            ms = body.get("ms", 800 if action == "longtap" else 300)
            if isinstance(ms, bool) or not isinstance(ms, int) or not 1 <= ms <= 60000:
                raise PanelAndroidError("bad_job", "Invalid gesture duration", 400)
        if action == "text":
            value = body.get("text", "")
            if not isinstance(value, str) or len(value) > 2000 or re.search(r"[\x00-\x1f]", value):
                raise PanelAndroidError("bad_job", "Invalid input text", 400)
        if action == "key" and not (isinstance(body.get("key"), str) and
                re.fullmatch(r"[A-Z0-9_]{1,40}", body["key"])):
            raise PanelAndroidError("bad_job", "Invalid Android key", 400)
        if action == "start" and not (isinstance(body.get("pkg"), str) and
                re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", body["pkg"])):
            raise PanelAndroidError("bad_job", "Invalid Android package", 400)
        if action == "shell":
            try:
                if not isinstance(body.get("cmd"), str):
                    raise ValueError()
                words = shlex.split(body["cmd"])
            except ValueError:
                raise PanelAndroidError("bad_job", "Invalid inspection command", 400) from None
            allowed = ((words[:1] in (["getprop"], ["dumpsys"]) and len(words) <= 2) or
                       words in (["wm", "size"], ["wm", "density"]) or
                       (words[:2] == ["settings", "get"] and len(words) == 4 and words[2] in ("system", "secure", "global")) or
                       (words[:2] == ["settings", "list"] and len(words) == 3 and words[2] in ("system", "secure", "global")) or
                       (words[:2] == ["pm", "list"] and len(words) >= 3))
            if not allowed:
                raise PanelAndroidError("bad_job", "Only worker read-only inspection commands are allowed", 400)
        return job

    def submit(self, body):
        # No directory/lock creation while the worker is offline.
        self._target(body.get("serial") if isinstance(body, dict) else None)
        if not self.is_running(self.directory / "worker.lock"):
            raise PanelAndroidError("worker_offline", "Android worker is offline", 503)
        try:
            with worker_lock(self.directory / "panel-producer.lock"):
                if (self.directory / "cmd.json").exists() or any(self.directory.glob("cmd.claim-*.json")):
                    raise PanelAndroidError("busy", "Another Android request is still queued")
                previous = self._json("panel-request.json", optional=True)
                result = self._json("result.json", optional=True)
                result_seq, previous_seq = self._sequence(result), self._sequence(previous)
                expired = self.now() - previous.get("submitted_at", self.now()) > 120
                if (previous and not previous.get("publish_failed") and not expired and result_seq < previous_seq):
                    raise PanelAndroidError("busy", "Previous Android request has not completed")
                job = self._job(body)
                seq = max(int(self.now() * 1000), result_seq + 1, previous_seq + 1)
                job["seq"] = seq
                # Record invalidation before publishing the request. Even a crash must not
                # leave an old image available for a second action.
                request = {"seq": seq, "serial": self.serial, "submitted_at": self.now()}
                atomic_json(self.directory / "panel-request.json", request)
                temporary = self.directory / ("panel-cmd-" + uuid.uuid4().hex + ".tmp")
                try:
                    atomic_json(temporary, job)
                    # A hard-link publish is atomic and refuses an occupied single slot
                    # on Windows and Unix; unlike replace(), it cannot discard another job.
                    os.link(temporary, self.directory / "cmd.json")
                except OSError:
                    atomic_json(self.directory / "panel-request.json", dict(request, publish_failed=True))
                    raise
                finally:
                    temporary.unlink(missing_ok=True)
                return self._base("pending", "pending", "Android request queued", seq=seq)
        except AndroidError as error:
            raise PanelAndroidError("busy", "Another panel producer owns the queue") from error
        except FileExistsError:
            raise PanelAndroidError("busy", "Another Android producer occupied the queue") from None
