"""Effective main-agent settings; bounded local reads and explicit revision CAS.

This module never produces observations, changes queue state or starts a model.
Group observer startup settings are read-only and are not claimed to be loaded
by the currently running process. Only the actual project opportunity interval
may be edited here; silence and delivery policy are outside this API.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid

import project_conf


class SettingsError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.http_status = code, status


def _need(value, code="settings_unavailable", status=503):
    if not value:
        raise SettingsError(code, "Autonomy settings could not be verified", status)


def _ordinary(path):
    path = Path(os.path.abspath(path))
    for item in (*reversed(path.parents), path):
        if os.path.lexists(item):
            info = item.lstat()
            _need(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400)
    return path


def _security(path):
    if os.name != "nt":
        return b""
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("advapi32", use_last_error=True).GetFileSecurityW
    api.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    api.restype = wintypes.BOOL
    size = wintypes.DWORD()
    api(str(path), 7, None, 0, ctypes.byref(size))
    _need(ctypes.get_last_error() == 122 and 0 < size.value <= 1024 * 1024)
    buffer = ctypes.create_string_buffer(size.value)
    _need(bool(api(str(path), 7, buffer, size.value, ctypes.byref(size))))
    return buffer.raw[:size.value]


def _record(path, limit=2 * 1024 * 1024):
    path = _ordinary(path)
    before = path.lstat()
    _need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= limit)
    signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode,
                              info.st_nlink, getattr(info, "st_file_attributes", 0))
    acl = _security(path)
    with path.open("rb") as stream:
        _need(signature(os.fstat(stream.fileno())) == signature(before))
        raw = stream.read(limit + 1)
        _need(len(raw) == before.st_size and signature(os.fstat(stream.fileno())) == signature(before))
    with path.open("rb") as second:
        _need(signature(os.fstat(second.fileno())) == signature(before) and second.read(limit + 1) == raw and
              signature(os.fstat(second.fileno())) == signature(before))
    _need(signature(path.lstat()) == signature(before) and _security(path) == acl)
    return {"signature": signature(before), "sha256": hashlib.sha256(raw).hexdigest(),
            "security_sha256": hashlib.sha256(acl).hexdigest()}, raw


def _json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            _need(key not in value)
            value[key] = item
        return value
    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique)
    _need(type(value) is dict)
    return value


def _project():
    project_conf.load(reload=True)
    selected = project_conf.config_path()
    if selected is None:
        return None, None, None, project_conf.load()
    path = _ordinary(selected)
    before, raw = _record(path)
    loaded = _json(raw)
    project_conf.load(reload=True)
    _need(project_conf.config_path() == str(path) and _record(path)[0] == before)
    return path, before, raw, project_conf._merge(project_conf.DEFAULTS, loaded)


def _revision(path, record):
    if path is None:
        return None
    return hashlib.sha256(json.dumps([str(path), record], sort_keys=True).encode()).hexdigest()


def _cadence(config):
    mind = config.get("mind")
    _need(type(mind) is dict)
    value = mind.get("opportunityMin", project_conf.DEFAULTS["mind"]["opportunityMin"])
    _need(type(value) in (int, float) and math.isfinite(value) and 2 <= value <= 60)
    return value


def _group_observation(config):
    out = {"availability": "unavailable", "read_only": True, "meaning": "observation_only",
           "activation": "observer_startup_configuration", "running_values_verified": False,
           "source": None, "settings": {"quiet_minutes": None, "poll_seconds": None}}
    try:
        work = Path(project_conf.work_dir())
        script = work / "napcat/groupwatch.ps1"
        if not script.is_file():
            source = Path(__file__).resolve().with_name("groupwatch.ps1")
            _need(source.parent.name == "scripts")
            script = source
        _, raw = _record(script)
        header = raw.decode("utf-8-sig")
        match = re.search(r"\[int\]\s*\$QuietMinutes\s*=\s*([0-9]+)", header)
        _need(match is not None)
        quiet = int(match.group(1))
        daemon = config.get("daemon")
        _need(type(daemon) is dict)
        poll = daemon.get("pollSeconds", project_conf.DEFAULTS["daemon"]["pollSeconds"])
        data = _ordinary(project_conf.data_dir())
        _need(os.path.commonpath([str(work.resolve()), str(data.resolve())]) == str(work.resolve()))
        selected = None
        for name in ("dafeiyu-tunables.json", "lanya-tunables.json"):
            path = data / name
            if os.path.lexists(path):
                selected = path
                break
        if selected is not None:
            tunables = _json(_record(selected)[1])
            quiet, poll = tunables.get("QuietMinutes", quiet), tunables.get("PollSeconds", poll)
        # Match the observer's integer inputs, including numeric JSON strings.
        def integer(value):
            if type(value) is str and re.fullmatch(r"[0-9]{1,10}", value):
                value = int(value)
            _need(type(value) is int and 1 <= value <= 2147483647)
            return value
        out.update(availability="available", source=(selected.stem if selected else "project_and_script_defaults"),
                   settings={"quiet_minutes": integer(quiet), "poll_seconds": integer(poll)})
    except (OSError, ValueError, TypeError, UnicodeError, RuntimeError):
        out["availability"] = "error"
    return out


def get_settings():
    out = {"schema": "dsh-panel-autonomy-settings/v1", "availability": "error", "mode": "unknown",
           "managed_by": "main_agent", "effective": {"opportunity_minutes": None}, "revision": None,
           "writable_fields": [], "existing_reviews_unchanged": True,
           "applies_to": "future_opportunities_and_new_primary_reviews"}
    try:
        path, before, _, config = _project()
        decisions = config.get("decisions")
        _need(type(decisions) is dict and decisions.get("mode") in ("unified", "legacy"))
        mode = decisions["mode"]
        out.update(availability="available", mode=mode, managed_by="main_agent" if mode == "unified" else "legacy_task",
                   effective={"opportunity_minutes": _cadence(config)}, revision=_revision(path, before),
                   writable_fields=["opportunity_minutes"] if path is not None and mode == "unified" else [])
        out["group_observation"] = _group_observation(config)
    except (OSError, ValueError, TypeError, UnicodeError, RuntimeError):
        out["group_observation"] = {"availability": "unavailable", "read_only": True,
                                    "settings": {"quiet_minutes": None, "poll_seconds": None}}
    return out


@contextmanager
def _write_lock(config):
    path = _ordinary(config.with_name(".lanya-autonomy-settings.lock"))
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    locked = False
    try:
        info, current = os.fstat(fd), path.lstat()
        _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
              (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino))
        if info.st_size == 0:
            os.write(fd, b"\0"); os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise SettingsError("settings_busy", "Another settings update is in progress", 409) from None
        yield
    finally:
        if locked:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _copy_security(source, target):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        descriptor = ctypes.create_string_buffer(_security(source))
        owner, group, dacl = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        defaulted, present = wintypes.BOOL(), wintypes.BOOL()
        for name, pointer in (("GetSecurityDescriptorOwner", owner), ("GetSecurityDescriptorGroup", group)):
            api = getattr(advapi, name)
            api.argtypes = [wintypes.LPVOID, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
            api.restype = wintypes.BOOL
            _need(bool(api(descriptor, ctypes.byref(pointer), ctypes.byref(defaulted))))
        api = advapi.GetSecurityDescriptorDacl
        api.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
        api.restype = wintypes.BOOL
        _need(bool(api(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted))) and present.value)
        control, revision = wintypes.WORD(), wintypes.DWORD()
        api = advapi.GetSecurityDescriptorControl
        api.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
        api.restype = wintypes.BOOL
        _need(bool(api(descriptor, ctypes.byref(control), ctypes.byref(revision))))
        if not control.value & 0x0400:
            # SetNamedSecurityInfo re-inherits parent ACEs for a non-inherited
            # source DACL. Preserve that exact descriptor with SetFileSecurity;
            # the inherited case below needs SetNamed to retain its AI flag.
            api = advapi.SetFileSecurityW
            api.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
            api.restype = wintypes.BOOL
            protection = 0x80000000 if control.value & 0x1000 else 0
            _need(bool(api(str(target), 7 | protection, descriptor)))
            _need(_security(source) == _security(target))
            return
        api = advapi.SetNamedSecurityInfoW
        api.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                        wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID]
        api.restype = wintypes.DWORD
        protection = 0x80000000 if control.value & 0x1000 else 0x20000000
        _need(api(str(target), 1, 7 | protection, owner, group, dacl, None) == 0)
        _need(_security(source) == _security(target))


def save_settings(body):
    if (type(body) is not dict or set(body) != {"revision", "settings"} or
            type(body["revision"]) is not str or re.fullmatch(r"[0-9a-f]{64}", body["revision"]) is None or
            type(body["settings"]) is not dict or set(body["settings"]) != {"opportunity_minutes"}):
        raise SettingsError("invalid_settings", "Expected a fresh revision and opportunity_minutes")
    minutes = body["settings"]["opportunity_minutes"]
    if type(minutes) not in (int, float) or not math.isfinite(minutes) or not 2 <= minutes <= 60:
        raise SettingsError("invalid_interval", "Opportunity interval must be between 2 and 60 minutes")
    path, _, _, _ = _project()
    _need(path is not None, "configuration_missing")
    with _write_lock(path):
        current_path, before, raw, config = _project()
        _need(current_path == path)
        if body["revision"] != _revision(path, before):
            raise SettingsError("revision_conflict", "Settings changed; refresh before saving", 409)
        _need(config.get("decisions", {}).get("mode") == "unified", "unified_settings_required", 409)
        _cadence(config)
        value = _json(raw)
        mind = value.setdefault("mind", {})
        _need(type(mind) is dict)
        mind["opportunityMin"] = minutes
        encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        candidate = _ordinary(path.with_name("." + path.name + ".autonomy-" + uuid.uuid4().hex + ".tmp"))
        candidate_record = None
        try:
            with candidate.open("xb") as stream:
                stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
            os.chmod(candidate, stat.S_IMODE(before["signature"][4]))
            _copy_security(path, candidate)
            candidate_record, actual = _record(candidate)
            _need(actual == encoded and candidate_record["signature"][1] != before["signature"][1])
            _need(_record(path)[0] == before, "revision_conflict", 409)
            _need(project_conf.config_path() == str(path))
            os.replace(candidate, path)
            _need(_record(path)[0] == candidate_record, "settings_readback_failed")
        finally:
            # Only remove this operation's known unconsumed candidate.
            if candidate_record is not None and candidate.exists() and _record(candidate)[0] == candidate_record:
                candidate.unlink()
        result = get_settings()
        _need(result["availability"] == "available" and result["effective"]["opportunity_minutes"] == minutes)
        return result
