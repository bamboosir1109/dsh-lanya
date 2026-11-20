"""Owner-bound phone tool using the existing Android queue, not a second worker."""
import hashlib
import json
from pathlib import Path
import re
import stat
import sys
import time

from android_panel import AndroidPanel, PanelAndroidError
from androidd import AGENT_JOB_SECONDS, AndroidError, AndroidWorker, agent_apps, agent_emulator_target
from runtime_capabilities import DEFAULTS, _configuration, entertainment_guide, owner_of


SCHEMA = "dsh-android-result/v1"
ACTIONS = {
    "shot": set(), "tap": {"x", "y"}, "swipe": {"x1", "y1", "x2", "y2", "ms"},
    "back": set(), "home": set(), "open_game": set(), "open_app": {"app"}, "type_text": {"text"},
    "foreground": set(),
}
FRAME_NAME = re.compile(r"[a-f0-9]{32}\.png")
MAX_FRAME_BYTES = 16 * 1024 * 1024


def ordinary_directory(path):
    for part in (path, *path.parents):
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Android directories must be ordinary local directories")


def stage_frame(directory, shot_id, raw, captured_at):
    name = shot_id + ".png"
    if not FRAME_NAME.fullmatch(name) or not 0 < len(raw) <= MAX_FRAME_BYTES:
        raise ValueError("Invalid Android frame identifier or size")
    store = directory / "agent-frames"
    store.mkdir(exist_ok=True)
    ordinary_directory(store)
    # Keep only bounded tool-owned screenshots. A fresh frame must never replace
    # the bytes of another observation while the model is reading it.
    entries = []
    for child in store.iterdir():
        if not FRAME_NAME.fullmatch(child.name):
            continue
        try:
            info = child.lstat()
            if stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400:
                entries.append((info.st_mtime, child))
        except FileNotFoundError:
            continue
    for modified, child in sorted(entries)[:-63]:
        if modified < captured_at - 600:
            child.unlink(missing_ok=True)
    if sum(1 for child in store.iterdir() if FRAME_NAME.fullmatch(child.name)) >= 96:
        raise ValueError("Android frame cache is full")
    output = store / name
    with output.open("xb") as stream:
        stream.write(raw)
    return str(output)


def foreground_package(directory, serial):
    device = AndroidWorker()
    if device.serial != serial or device.work.resolve() != directory.resolve():
        raise AndroidError("configuration", "Android foreground target changed")
    report = device.shell("dumpsys", "activity", "activities", timeout=4)
    for field in ("topResumedActivity", "mResumedActivity"):
        packages = set(re.findall(r"(?m)^\s*" + field + r"\b[^\r\n]*?\s([a-zA-Z][\w.]*)/", report))
        if packages:
            return next(iter(packages)) if len(packages) == 1 else None
    return None


def execute(request, *, panel_factory=AndroidPanel, foreground_probe=foreground_package,
            monotonic=time.monotonic, sleep=time.sleep):
    if not isinstance(request, dict) or set(request) != {"cwd", "agent_preset", "conversation_key", "job"}:
        raise ValueError("Invalid Android tool request")
    owner = owner_of(request["cwd"], request["agent_preset"])
    scope = request["conversation_key"]
    base = {"schema": SCHEMA, "owner": owner, "conversation_key": scope}
    workspace = Path(request["cwd"]).resolve(strict=True)
    configuration = _configuration(workspace)
    options = configuration.get("android", {})
    serial = options.get("serial", "emulator-5556") if isinstance(options, dict) else None
    if not agent_emulator_target(options, serial):
        return dict(base, status="unavailable", code="dedicated_emulator_not_enabled")
    name = configuration.get("dataDirName", DEFAULTS["dataDirName"])
    if not isinstance(name, str) or name in ("", ".", "..") or re.search(r"[/\\:]", name):
        raise ValueError("Invalid configured character directory")
    data = workspace / name
    if not entertainment_guide(workspace, data, configuration, scope)["primary_internal_leisure"]:
        return dict(base, status="unavailable", code="primary_private_scope_required")

    job = request["job"]
    action = job.get("action") if isinstance(job, dict) else None
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValueError("Unknown Android tool action")
    allowed = ACTIONS[action] | {"action"} | (set() if action == "shot" else {"expected_shot_id"})
    screen_input = action in ("tap", "swipe", "type_text")
    if set(job) - allowed:
        raise ValueError("Unexpected Android tool parameters")
    job = dict(job, serial=serial)
    if action == "open_game":
        job.update(action="start", pkg="org.dshlanya.game2048")
    elif action == "open_app":
        app = job.pop("app", None)
        package = agent_apps(options).get(app) if isinstance(app, str) else None
        if not package:
            return dict(base, status="unavailable", code="android_app_not_enabled")
        job.update(action="start", pkg=package)
    elif action == "type_text":
        text = job.get("text")
        if not isinstance(text, str) or not text.strip() or not re.fullmatch(r"[ -~]{1,160}", text) or "%s" in text:
            return dict(base, status="error", code="android_printable_ascii_required")
        job.update(action="text")
    elif action in ("back", "home"):
        job.update(action="key", key=action.upper())
    directory = data / "android"
    submitted = False
    try:
        ordinary_directory(directory)
        panel = panel_factory(directory=directory, serial=serial, max_age=options.get("maxShotAgeSeconds", 30))
        apps = agent_apps(options)
        if action == "foreground" or (screen_input and "meituan" in apps):
            shot_id = job.get("expected_shot_id")
            first = panel.status(serial)
            if not shot_id or not first.get("ok") or first.get("shot_id") != shot_id:
                return dict(base, status="unavailable", code="stale_view")
            package = foreground_probe(directory, serial)
            last = panel.status(serial)
            if not last.get("ok") or last.get("shot_id") != shot_id or last.get("seq") != first.get("seq"):
                return dict(base, status="unavailable", code="stale_view")
            if action == "foreground":
                app = next((alias for alias, allowed in apps.items() if allowed == package), None)
                return dict(base, status="foreground", serial=serial, shot_id=shot_id, app=app)
            if package == apps["meituan"]:
                return dict(base, status="unavailable", code="android_meituan_human_input_required")
            if package not in {*apps.values(), "org.dshlanya.game2048"}:
                return dict(base, status="unavailable", code="android_foreground_required")
        deadline = monotonic() + AGENT_JOB_SECONDS + 8

        def observe_job(command):
            nonlocal submitted
            if monotonic() >= deadline:
                return dict(base, status="unknown", code="observation_timeout")
            # Publication may succeed even if its caller later loses the response.
            submitted = True
            try:
                seq = panel.submit(command, agent=True)["seq"]
            except PanelAndroidError as error:
                state = "unavailable" if error.code == "worker_offline" else "error"
                return dict(base, status=state, code=error.code)
            while monotonic() < deadline:
                status = panel.status(serial)
                if status.get("seq", 0) > seq:
                    return dict(base, status="unknown", code="superseded")
                if status.get("seq") == seq and status.get("status") != "pending":
                    if not status.get("ok"):
                        code = status.get("code", "capture_failed")
                        if code in {"android_operator_private_screen", "android_foreground_required"} and command["action"] == "shot":
                            return dict(base, status="unavailable", code=code)
                        return dict(base, status="error" if command["action"] == "shot" else "unknown", code=code)
                    return status
                sleep(0.2)
            return dict(base, status="unknown", code="observation_timeout")

        if action == "open_app" and "expected_shot_id" not in job:
            initial = observe_job({"action": "shot", "serial": serial})
            if not initial.get("ok"):
                return initial
            job["expected_shot_id"] = initial["shot_id"]
        status = observe_job(job)
        if not status.get("ok"):
            return status
        shot_id = status["shot_id"]
        raw = panel.image(serial, shot_id)
        frame = stage_frame(directory, shot_id, raw, status["captured_at"])
        return dict(base, status="observed", serial=serial, shot_id=shot_id, file=frame,
                    sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw),
                    width=status["width"], height=status["height"], captured_at=status["captured_at"])
    except (AndroidError, PanelAndroidError) as error:
        return dict(base, status="unknown" if submitted else "error", code=error.code)
    except FileNotFoundError:
        return dict(base, status="unknown" if submitted else "unavailable", code="worker_state_unavailable")
    except (OSError, ValueError):
        return dict(base, status="unknown" if submitted else "error", code="android_state_unavailable")


if __name__ == "__main__":
    try:
        if sys.argv[1:] != ["--stdin-json"]:
            raise ValueError("Expected --stdin-json")
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192:
            raise ValueError("Oversized Android request")
        print(json.dumps(execute(json.loads(raw)), ensure_ascii=True))
    except (OSError, ValueError, TypeError, KeyError):
        print("Invalid Android executor request", file=sys.stderr)
        sys.exit(1)
