"""Read-only voice capability projection. Never synthesizes or sends audio.

Process observations describe infrastructure, not a successful synthesis.
This module does not read voice queues, request ledgers, reference audio,
transcripts, generated audio, credentials, or model/QQ messages.
"""
import datetime
import math
from pathlib import Path
import re
import stat
import time

SCHEMA = "dsh-voice-status/v1"


def _count(components, tag):
    if not isinstance(components, list):
        return None
    rows = [row for row in components if isinstance(row, dict) and row.get("tag") == tag]
    if len(rows) != 1:
        return None
    value = rows[0].get("count")
    return value if type(value) is int and 0 <= value <= 10000 else None


def _observed(value, now):
    try:
        if isinstance(value, str) and len(value) <= 64:
            # Windows round-trip timestamps have seven fractional digits;
            # Python 3.9 accepts only millisecond/microsecond precision here.
            normalized = re.sub(r"\.(\d{1,9})(?=[+-]\d{2}:\d{2}$)",
                                lambda match: "." + match[1][:6].ljust(6, "0"),
                                value.replace("Z", "+00:00"))
            parsed = datetime.datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                return None, "unavailable"
            seconds = parsed.timestamp()
        elif type(value) in (int, float) and math.isfinite(value):
            seconds = value / 1000
        else:
            return None, "unavailable"
        if not math.isfinite(seconds) or seconds <= 0 or seconds > now + 5:
            return None, "unavailable"
        return value, "fresh" if now - seconds < 90 else "stale"
    except (ValueError, OverflowError, OSError):
        return None, "unavailable"


def _installed(runtime_dir):
    try:
        directory = Path(runtime_dir)
        if not directory.is_absolute():
            return None
        path = directory / "voice_executor.py"
        for node in [path, *path.parents]:
            info = node.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return None
            if node == path and not stat.S_ISREG(info.st_mode):
                return None
        return True
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError):
        return None


def voice_status(*, runtime_dir, components, mode, port, observed_at, now=None):
    """Project trusted local state metadata without probing or invoking TTS."""
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError("A finite status timestamp is required")
    selected_mode = mode if mode in ("unified", "legacy") else "unknown"
    observed, freshness = _observed(observed_at, now)
    engine_count = _count(components, "tts")
    return {
        "schema": SCHEMA,
        "mode": selected_mode,
        "execution": "main_agent_on_demand" if selected_mode == "unified" else
                     "independent_queue" if selected_mode == "legacy" else "unknown",
        "daemon_required": False if selected_mode == "unified" else
                           True if selected_mode == "legacy" else None,
        "daemon_process_count": _count(components, "voicedaemon"),
        "executor": {"installed": _installed(runtime_dir)},
        "engine": {
            "type": "local_tts_process_observation",
            "process_count": engine_count,
            "observed_at": observed,
            "observation_state": freshness if engine_count is not None else "unavailable",
            "port": port if type(port) is int and 1 <= port <= 65535 else None,
        },
        "synthesis": {"state": "not_probed"},
        "delivery": {"state": "unverified"},
        "read_only": True,
    }


def unavailable_voice_status():
    """A missing projection helper/configuration never implies engine failure."""
    return {"schema": SCHEMA, "mode": "unknown", "execution": "unknown",
            "daemon_required": None, "daemon_process_count": None,
            "executor": {"installed": None},
            "engine": {"type": "local_tts_process_observation", "process_count": None,
                       "observed_at": None, "observation_state": "unavailable", "port": None},
            "synthesis": {"state": "not_probed"}, "delivery": {"state": "unverified"},
            "read_only": True}
