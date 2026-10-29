"""Durable observations for the single chat decision owner; never runs an LLM.

The event stays immutable in pending/. A consumer writes a separate done/
marker, so retries cannot republish an event while it moves between folders.
Payloads are evidence, never user control instructions or send permissions.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid


class ObservationError(ValueError):
    pass


def canonical_json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def event_key(scope, event_id):
    return hashlib.sha256((scope + "\0" + event_id).encode("utf-8")).hexdigest()


def _plain_directory(path, create=False):
    path = Path(os.path.abspath(path))
    for part in [*reversed(path.parents), path]:
        if not os.path.lexists(part) and create:
            try:
                part.mkdir()
            except FileExistsError:
                pass
        if os.path.lexists(part):
            info = part.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or
                    getattr(info, "st_file_attributes", 0) & 0x400):
                raise ObservationError("Observation path must contain ordinary directories")
    return path


def validate_event(event):
    if not isinstance(event, dict) or event.get("version") != 1:
        raise ObservationError("Unsupported observation version")
    if not isinstance(event.get("scope"), str) or not re.fullmatch(r"(?:private|group):[1-9][0-9]*", event["scope"]):
        raise ObservationError("Expected a canonical string conversation scope")
    for key in ("event_id", "kind", "producer"):
        value = event.get(key)
        if not isinstance(value, str) or not value or len(value) > 256 or re.search(r"[\x00-\x1f]", value):
            raise ObservationError("Invalid observation identity")
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", event["kind"]):
        raise ObservationError("Invalid observation kind")
    if event.get("origin") != "observation" or event.get("control_allowed") is not False:
        raise ObservationError("An observation cannot impersonate a user or grant control")
    if not isinstance(event.get("payload"), dict):
        raise ObservationError("Observation payload must be an object")
    if any(not isinstance(event.get(key), str) for key in ("workspace_key", "agent_key")):
        raise ObservationError("Explicit memory ownership is required")
    created = event.get("created_at")
    if isinstance(created, bool) or not isinstance(created, int) or created < 0 or created > 2**53 - 1:
        raise ObservationError("Invalid observation creation time")
    if len(canonical_json(event).encode("utf-8")) > 256 * 1024:
        raise ObservationError("Observation exceeds the bounded event size")
    return event


def _read_regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or
            getattr(info, "st_file_attributes", 0) & 0x400):
        raise ObservationError("Observation entry is not an ordinary file")
    if info.st_size > 512 * 1024:
        raise ObservationError("Observation entry exceeds its size limit")
    return json.loads(path.read_text(encoding="utf-8"))


def _semantic(event):
    # A producer retry's wall clock is not a new event.
    return canonical_json({key: value for key, value in event.items() if key != "created_at"})


def _validated_completion(result, event, raw):
    """Only a complete, evidence-bound terminal decision may acknowledge work."""
    if (not isinstance(result, dict) or type(result.get("version")) is not int or result["version"] != 1 or
            result.get("event_sha256") != hashlib.sha256(raw).hexdigest() or
            result.get("status") not in ("decided", "sent", "unknown", "suppressed")):
        raise ObservationError("Invalid observation completion evidence")
    decision = result.get("decision_id")
    if not isinstance(decision, str) or not decision.strip() or len(decision) > 256 or re.search(r"[\x00-\x1f]", decision):
        raise ObservationError("Invalid observation completion decision")
    for key in ("scope", "event_id"):
        if key in result and result[key] != event[key]:
            raise ObservationError("Observation completion identity mismatch")
    for key in ("completed_at", "next_review_at"):
        if key in result:
            value = result[key]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**53 - 1:
                raise ObservationError("Invalid observation completion time")
            if key == "next_review_at" and value <= event["created_at"]:
                raise ObservationError("Invalid observation review time")
    return result


def submit_observation(*, scope, kind, payload, producer, event_id,
                       workspace_key, agent_key, directory=None, created_at=None):
    if directory is None:
        from project_conf import data_dir
        directory = Path(data_dir()) / "decision-inbox"
    event = validate_event({"version": 1, "event_id": event_id, "scope": scope,
                            "kind": kind, "producer": producer,
                            "created_at": int(time.time() * 1000) if created_at is None else created_at,
                            "payload": payload, "workspace_key": workspace_key, "agent_key": agent_key,
                            "origin": "observation", "control_allowed": False})
    base = _plain_directory(directory, create=True)
    pending = _plain_directory(base / "pending", create=True)
    done = _plain_directory(base / "done", create=True)
    key = event_key(scope, event_id)
    target = pending / (key + ".json")
    marker = done / (key + ".json")

    def existing():
        previous = validate_event(_read_regular(target))
        if _semantic(previous) != _semantic(event):
            raise ObservationError("Event identity already contains different evidence")
        state = "pending"
        if os.path.lexists(marker):
            _validated_completion(_read_regular(marker), previous, target.read_bytes())
            state = "done"
        return {"submitted": False, "state": state, "key": key, "event_id": event_id}

    if os.path.lexists(target):
        return existing()
    if os.path.lexists(marker):
        raise ObservationError("Completion marker exists without its immutable event")
    temporary = pending / (".publish-" + uuid.uuid4().hex + ".tmp")
    raw = (canonical_json(event) + "\n").encode("utf-8")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            if os.name == "nt":
                os.rename(temporary, target)  # Windows refuses to replace a winner.
            else:
                os.link(temporary, target, follow_symlinks=False)  # Atomic publish, no overwrite.
        except FileExistsError:
            return existing()
        return {"submitted": True, "state": "pending", "key": key, "event_id": event_id}
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


def submit_project_observation(*, scope, kind, payload, producer, event_id, directory=None):
    """Ownership is configuration, never inferred from payload or model output."""
    from project_conf import work_dir, get
    workspace = os.path.abspath(work_dir())
    preset = str(get("decisions.agentPreset", "") or "").strip()
    return submit_observation(scope=scope, kind=kind, payload=payload, producer=producer,
                              event_id=event_id, directory=directory,
                              workspace_key=workspace.lower() if os.name == "nt" else workspace,
                              agent_key=preset.lower() if os.name == "nt" else preset)


def unified_decisions_enabled():
    from project_conf import get
    mode = get("decisions.mode", "unified")
    if mode not in ("unified", "legacy"):
        raise ObservationError("Unknown decision mode; refusing independent activity")
    return mode == "unified"


def project_scope(target="admin"):
    """Routing identity comes from local configuration, never observation text."""
    from project_conf import data_dir, get
    config = Path(data_dir()) / "lanya-config.json"
    identity = json.loads(config.read_text(encoding="utf-8-sig")) if config.is_file() else {}
    if not isinstance(identity, dict):
        raise ObservationError("Invalid project identity configuration")
    if target == "group":
        value = identity.get("fanGroupQQ") or identity.get("fanGroup") or get("fanGroup", "")
        prefix = "group:"
    else:
        value = identity.get("adminQQ") if target == "admin" else target
        if isinstance(value, list):
            value = value[0] if value else ""
        prefix = "private:"
    value = str(value or "")
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise ObservationError("No configured observation conversation")
    return prefix + value


def project_scopes():
    """One private owner and, when configured, one separate group opportunity."""
    from project_conf import data_dir, get
    config = Path(data_dir()) / "lanya-config.json"
    identity = json.loads(config.read_text(encoding="utf-8-sig")) if config.is_file() else {}
    if not isinstance(identity, dict):
        raise ObservationError("Invalid project identity configuration")
    admin = identity.get("adminQQ")
    if isinstance(admin, list):
        admin = admin[0] if admin else ""
    group = identity.get("fanGroupQQ")
    if group in (None, ""):
        group = identity.get("fanGroup")
    if group in (None, ""):
        group = get("fanGroup", "")
    values = [("private:", admin)]
    if group not in (None, ""):
        values.append(("group:", group))
    scopes = []
    for prefix, value in values:
        if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", str(value or "")):
            raise ObservationError("Invalid configured observation conversation")
        scopes.append(prefix + str(value))
    return scopes


def observation_review_chain(scope, event_id, directory=None, limit=128):
    """Read immutable queue successors; never publish a second review owner.

    activity_due/reminder.due are legacy producer-owned. All other kinds use
    the queue's main-review identity. An existing successor is always verified
    before following it, including legacy events made by an earlier version.
    """
    if directory is None:
        from project_conf import data_dir
        directory = Path(data_dir()) / "decision-inbox"
    base = _plain_directory(directory)
    _plain_directory(base / "pending")
    _plain_directory(base / "done")
    for _ in range(limit):
        key = event_key(scope, event_id)
        target = base / "pending" / (key + ".json")
        event = validate_event(_read_regular(target))
        if event["scope"] != scope or event["event_id"] != event_id:
            raise ObservationError("Invalid review observation identity")
        completion = observation_completion(scope, event_id, directory=base)
        result = {"event": event, "completion": completion, "waiting_review": False}
        if completion is None or completion["status"] == "unknown" or "next_review_at" not in completion:
            return result
        due = completion["next_review_at"]
        successor_id = "main-review:" + key + ":" + str(due)
        child = base / "pending" / (event_key(scope, successor_id) + ".json")
        if not os.path.lexists(child):
            result["waiting_review"] = event["kind"] not in ("activity_due", "reminder.due")
            return result
        expected = dict(event, event_id=successor_id, producer="main-review", created_at=due,
                        payload=dict(event["payload"], review_context={
                            "parent_observation_key": key, "parent_event_id": event_id,
                            "previous_decision_id": completion["decision_id"], "scheduled_at": due}))
        actual = validate_event(_read_regular(child))
        if canonical_json(actual) != canonical_json(expected):
            raise ObservationError("Review successor conflicts with immutable evidence")
        event_id = successor_id
    raise ObservationError("Review chain exceeds its bounded length")


def observation_completion(scope, event_id, directory=None):
    if directory is None:
        from project_conf import data_dir
        directory = Path(data_dir()) / "decision-inbox"
    base = _plain_directory(directory)
    key = event_key(scope, event_id)
    target = base / "pending" / (key + ".json")
    marker = base / "done" / (key + ".json")
    if not os.path.lexists(marker):
        return None
    _plain_directory(marker.parent)
    _plain_directory(target.parent)
    result = _read_regular(marker)
    event = validate_event(_read_regular(target))
    if event["scope"] != scope or event["event_id"] != event_id:
        raise ObservationError("Invalid observation completion evidence")
    return _validated_completion(result, event, target.read_bytes())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ObservationError("Observation request is too large")
        event = validate_event(json.loads(raw.decode("utf-8-sig")))
        result = submit_observation(directory=args.directory,
                                    **{key: event[key] for key in ("scope", "kind", "payload", "producer",
                                                                 "event_id", "workspace_key", "agent_key", "created_at")})
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, TypeError) as error:
        print("observation refused: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
