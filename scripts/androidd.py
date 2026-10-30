"""Pinned-device Android vision worker. Importing never starts ADB or a daemon.

JSON jobs retain seq/action and the existing action names. Visual actions must
name the last successful shot_id; an error never advertises an old screenshot.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import struct
import subprocess
import time
import uuid
import zlib

from project_conf import data_dir, get, work_dir


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PNG_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 16_000_000


class AndroidError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def verify_png(raw):
    """Validate complete non-interlaced PNG, CRCs, zlib stream and scanline sizes."""
    if not isinstance(raw, bytes) or not raw.startswith(PNG_SIGNATURE) or len(raw) > MAX_PNG_BYTES:
        raise AndroidError("bad_png", "Screenshot is not a bounded PNG")
    offset, chunks, pixels, ended, idat_ended = 8, [], None, False, False
    width = height = stride = None
    while offset < len(raw):
        if offset + 12 > len(raw):
            raise AndroidError("bad_png", "Truncated PNG chunk")
        length, = struct.unpack_from(">I", raw, offset)
        kind = raw[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(raw):
            raise AndroidError("bad_png", "Truncated PNG data")
        payload = raw[offset + 8:end - 4]
        expected, = struct.unpack_from(">I", raw, end - 4)
        if not re.fullmatch(rb"[A-Za-z]{4}", kind) or zlib.crc32(kind + payload) & 0xFFFFFFFF != expected:
            raise AndroidError("bad_png", "PNG CRC mismatch")
        if not chunks and kind != b"IHDR":
            raise AndroidError("bad_png", "PNG must start with IHDR")
        if kind == b"IHDR":
            if chunks or length != 13:
                raise AndroidError("bad_png", "Invalid PNG header")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            legal = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if (not width or not height or width * height > MAX_PIXELS or depth not in legal.get(color, ()) or
                    compression or filtering or interlace):
                raise AndroidError("bad_png", "Unsupported PNG dimensions or encoding")
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
            stride = (width * channels * depth + 7) // 8
        elif kind == b"IDAT":
            if idat_ended:
                raise AndroidError("bad_png", "Nonconsecutive PNG data")
            if pixels is None:
                pixels = bytearray()
            pixels.extend(payload)
        elif kind == b"PLTE":
            if b"PLTE" in chunks or pixels is not None or color in (0, 4) or not length or length % 3 or length > 768:
                raise AndroidError("bad_png", "Invalid PNG palette")
        elif kind == b"IEND":
            if length or pixels is None or end != len(raw):
                raise AndroidError("bad_png", "Invalid PNG end")
            ended = True
        elif kind[0] & 32 == 0 and kind != b"PLTE":
            raise AndroidError("bad_png", "Unsupported critical PNG chunk")
        if pixels is not None and kind not in (b"IDAT", b"IEND"):
            idat_ended = True
        chunks.append(kind)
        offset = end
        if ended:
            break
    if not ended or (color == 3 and b"PLTE" not in chunks):
        raise AndroidError("bad_png", "Incomplete PNG")
    expected_size = height * (stride + 1)
    decoder = zlib.decompressobj()
    try:
        decoded = decoder.decompress(pixels, expected_size + 1)
    except zlib.error as error:
        raise AndroidError("bad_png", "Invalid PNG compressed data") from error
    if (len(decoded) != expected_size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail or
            any(decoded[pos] > 4 for pos in range(0, len(decoded), stride + 1))):
        raise AndroidError("bad_png", "Invalid PNG scanline data")
    return width, height


def atomic_bytes(path, raw):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))


@contextmanager
def worker_lock(path):
    """Hold an OS lock; a leftover file never counts as a living owner."""
    stream = Path(path).open("a+b")
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b"\0"); stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as error:
            raise AndroidError("busy", "Another Android worker owns this queue") from error
        yield
    finally:
        if locked:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


class AndroidWorker:
    def __init__(self, *, run=subprocess.run, now=time.time):
        self.serial = str(get("android.serial", "emulator-5556")).strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", self.serial):
            raise AndroidError("configuration", "android.serial must identify one fixed device")
        self.adb_exe = str(get("android.adbPath", "") or
                           Path(work_dir()) / "android" / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb"))
        if not Path(self.adb_exe).is_file():
            self.adb_exe = shutil.which(self.adb_exe) or self.adb_exe
        self.max_age = float(get("android.maxShotAgeSeconds", 30))
        if not 0 < self.max_age <= 300:
            raise AndroidError("configuration", "maxShotAgeSeconds must be between 0 and 300")
        self.work = Path(data_dir()) / "android"
        self.work.mkdir(parents=True, exist_ok=True)
        self.shot = self.work / "shot.png"
        self.meta = self.work / "shot-meta.json"
        self.cmd = self.work / "cmd.json"
        self.result = self.work / "result.json"
        self.temp = Path(work_dir()) / "android" / "temp"
        self.temp.mkdir(parents=True, exist_ok=True)
        self.env = {**os.environ, "TEMP": str(self.temp), "TMP": str(self.temp)}
        self.run, self.now, self.current_shot = run, now, None

    def adb(self, *args, timeout=30):
        command = [self.adb_exe, "-s", self.serial, *args]
        options = {"capture_output": True, "timeout": timeout, "env": self.env}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            response = self.run(command, **options)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as error:
            raise AndroidError("offline", "ADB unavailable or timed out") from error
        if response.returncode:
            raise AndroidError("offline", "ADB command failed for the configured serial")
        return response.stdout

    def shell(self, *words):
        # ADB's remote shell needs its own quoting even though shell=False here.
        return self.adb("shell", shlex.join(str(word) for word in words)).decode("utf-8", "replace").strip()

    def online(self):
        if self.adb("get-state").strip() != b"device":
            raise AndroidError("offline", "Configured Android serial is not online")

    def capture(self):
        self.current_shot = None
        self.online()
        sizes = re.findall(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", self.shell("wm", "size"))
        if not sizes:
            raise AndroidError("bad_dimensions", "Cannot verify Android display dimensions")
        expected = tuple(map(int, sizes[-1]))
        raw = self.adb("exec-out", "screencap", "-p", timeout=45)
        width, height = verify_png(raw)
        if sorted((width, height)) != sorted(expected):
            raise AndroidError("bad_dimensions", "Screenshot dimensions differ from the configured display")
        shot = {"serial": self.serial, "shot_id": uuid.uuid4().hex, "width": width, "height": height,
                "captured_at": self.now(), "sha256": hashlib.sha256(raw).hexdigest()}
        atomic_bytes(self.shot, raw)
        atomic_json(self.meta, shot)
        self.current_shot = shot
        return shot

    def require_view(self, job):
        view = self.current_shot
        expected = job.get("expected_shot_id", job.get("shot_id"))
        if (not view or view["serial"] != self.serial or expected != view["shot_id"] or
                not 0 <= self.now() - view["captured_at"] <= self.max_age):
            raise AndroidError("stale_view", "Action requires the recent screenshot ID for this serial")
        try:
            on_disk = json.loads(self.meta.read_text(encoding="utf-8"))
            raw = self.shot.read_bytes()
            if on_disk != view or hashlib.sha256(raw).hexdigest() != view["sha256"]:
                raise ValueError("Snapshot changed")
            verify_png(raw)
        except (OSError, ValueError, AndroidError) as error:
            self.current_shot = None
            raise AndroidError("stale_view", "Observed screenshot is no longer current") from error
        return view

    def coordinate(self, value, maximum):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < maximum:
            raise AndroidError("bad_job", "Coordinates must be integers inside the observed screen")
        return str(value)

    def action(self, job):
        action = str(job.get("action", "")).lower()
        view = self.require_view(job)
        self.online()
        width, height = view["width"], view["height"]
        xy = lambda x, y: (self.coordinate(job.get(x), width), self.coordinate(job.get(y), height))
        if action == "tap":
            return self.shell("input", "tap", *xy("x", "y"))
        if action in ("longtap", "swipe"):
            duration = job.get("ms", 800 if action == "longtap" else 300)
            if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 60_000:
                raise AndroidError("bad_job", "Invalid gesture duration")
            first = xy("x", "y") if action == "longtap" else xy("x1", "y1")
            last = first if action == "longtap" else xy("x2", "y2")
            return self.shell("input", "swipe", *first, *last, str(duration))
        if action == "text":
            text = job.get("text", "")
            if not isinstance(text, str) or len(text) > 2000 or re.search(r"[\x00-\x1f]", text):
                raise AndroidError("bad_job", "Invalid input text")
            return self.shell("input", "text", text.replace(" ", "%s"))
        if action == "key":
            key = str(job.get("key", "BACK")).upper()
            if not re.fullmatch(r"[A-Z0-9_]{1,40}", key):
                raise AndroidError("bad_job", "Invalid key event")
            return self.shell("input", "keyevent", key)
        if action == "start":
            package = job.get("pkg", "")
            if not isinstance(package, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", package):
                raise AndroidError("bad_job", "Invalid Android package")
            output = self.shell("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
            if "no activities found" in output.lower():
                raise AndroidError("action_failed", "No launcher activity found")
            return output
        if action == "shell":
            words = shlex.split(str(job.get("cmd", "")))
            allowed = (
                (words[:1] == ["getprop"] and len(words) <= 2) or
                (words[:1] == ["dumpsys"] and len(words) <= 2) or
                (words in (["wm", "size"], ["wm", "density"])) or
                (words[:2] == ["settings", "get"] and len(words) == 4 and words[2] in ("system", "secure", "global")) or
                (words[:2] == ["settings", "list"] and len(words) == 3 and words[2] in ("system", "secure", "global")) or
                (words[:2] == ["pm", "list"] and len(words) >= 3)
            )
            if not allowed:
                raise AndroidError("bad_job", "shell supports read-only Android inspection commands")
            return self.shell(*words)
        raise AndroidError("bad_job", "Unknown Android action")

    def handle(self, job):
        result = {"seq": job.get("seq", 0) if isinstance(job, dict) else 0,
                  "serial": self.serial, "ok": False, "status": "error", "shot": "", "shot_id": ""}
        try:
            if not isinstance(job, dict) or job.get("serial", self.serial) != self.serial:
                raise AndroidError("bad_job", "Job serial does not match the configured target")
            if str(job.get("action", "")).lower() != "shot":
                detail = self.action(job)
                # The old view must never authorize a second action, even on failure.
                self.current_shot = None
            else:
                detail = ""
            shot = self.capture()
            result.update(ok=True, status="ok", note=detail[:400], shot=str(self.shot), **shot)
        except (AndroidError, KeyError, TypeError, ValueError, OSError) as error:
            self.current_shot = None
            code = error.code if isinstance(error, AndroidError) else "bad_job"
            result.update(status="offline" if code == "offline" else "error", code=code, note=str(error))
        result["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return result


def consume(worker, args):
    initial = worker.handle({"seq": 0, "action": "shot"})
    atomic_json(worker.result, initial)
    if args.setup:
        print(json.dumps(initial, ensure_ascii=True))
        return 0 if initial["ok"] else 1
    last_seq = 0
    while True:
        if worker.cmd.exists():
            claimed = worker.cmd.with_name("cmd.claim-" + uuid.uuid4().hex + ".json")
            try:
                # Claim one complete producer file atomically; never unlink a new job.
                worker.cmd.rename(claimed)
                job = json.loads(claimed.read_text(encoding="utf-8-sig"))
                seq = job.get("seq", 0) if isinstance(job, dict) else 0
                if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                    raise AndroidError("bad_job", "seq must be a nonnegative integer")
                if seq and seq <= last_seq:
                    raise AndroidError("duplicate", "Job sequence was already consumed")
                last_seq = seq or last_seq + 1
                result = worker.handle(job)
            except (ValueError, OSError, AndroidError) as error:
                result = {"seq": last_seq, "serial": worker.serial, "ok": False, "status": "error",
                          "code": getattr(error, "code", "bad_job"), "note": str(error), "shot": "", "shot_id": ""}
            finally:
                if claimed.exists():
                    claimed.unlink()
            atomic_json(worker.result, result)
        if args.once:
            return 0
        time.sleep(1.5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true", help="Capture one verified screenshot without an action")
    parser.add_argument("--once", action="store_true", help="Consume at most one JSON job")
    args = parser.parse_args(argv)
    try:
        worker = AndroidWorker()
        with worker_lock(worker.work / "worker.lock"):
            return consume(worker, args)
    except (AndroidError, ValueError, OSError) as error:
        print(json.dumps({"ok": False, "status": "error", "code": getattr(error, "code", "configuration"),
                          "note": str(error), "shot": "", "shot_id": ""}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
