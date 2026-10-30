# Lock Dafeiyu's tunables so nobody can talk her into changing them.
#
# Why code and not a prompt rule: any "do not change the settings" instruction is
# still just words to the model, and she can be talked into raising her own limits. This makes the change fail
# regardless of what she agrees to.
#
# The baseline lives OUTSIDE her home directory, so a conversation with her cannot
# reach it. On a mismatch this restores the approved values and appends an alert
# the owner can read.
#
#   python settings-guard.py --init    record the current values as approved
#   python settings-guard.py           verify/restore (this is what the task runs)
#
# ASCII-only source.

from project_conf import data_dir, work_dir
import io
import os
import re
import sys
import time

NAPCAT = os.path.join(work_dir(), "napcat")
# Deliberately not under the data dir - she works in there.
LOCK_ROOT = data_dir() + "-\u5f52\u6863"
LOCK = os.path.join(LOCK_ROOT, "settings.lock.json")
ALERT = os.path.join(LOCK_ROOT, "settings-alerts.log")

TARGETS = {
    "groupwatch.ps1": [
        "QuietMinutes",
        "MinGapMinutes",
        "QuietGapMinMinutes",
        "QuietGapMaxMinutes",
        "MaxPerHour",
        "MaxPerHourAny",
        "MaxPer24Hours",
        "MaxQuietStartsPerDay",
        "MaxTotal",
    ],
}


def log(path, msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    print(line)


def read_text(path):
    with io.open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def extract(text, name):
    """Pull '$Name = 123' out of the script."""
    m = re.search(r"\$" + re.escape(name) + r"\s*=\s*(-?\d+)", text)
    return int(m.group(1)) if m else None


def set_value(text, name, value):
    return re.sub(
        r"(\$" + re.escape(name) + r"\s*=\s*)-?\d+",
        lambda m: m.group(1) + str(value),
        text,
        count=1,
    )


def snapshot():
    out = {}
    for fname, names in TARGETS.items():
        p = os.path.join(NAPCAT, fname)
        if not os.path.isfile(p):
            continue
        text = read_text(p)
        out[fname] = {n: extract(text, n) for n in names if extract(text, n) is not None}
    return out


def set_readonly(path, on):
    """Best-effort protection. The guard restores values anyway; read-only makes
    the common accidental write fail before it ever takes effect."""
    try:
        import stat
        mode = os.stat(path).st_mode
        if on:
            os.chmod(path, mode & ~stat.S_IWRITE)
        else:
            os.chmod(path, mode | stat.S_IWRITE)
    except Exception:
        pass


def main():
    init = "--init" in sys.argv
    current = snapshot()

    if init:
        os.makedirs(LOCK_ROOT, exist_ok=True)
        import json
        with io.open(LOCK, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(current, ensure_ascii=False, indent=2))
        log(ALERT, "baseline recorded: " + json.dumps(current, ensure_ascii=False))
        return

    if not os.path.isfile(LOCK):
        log(ALERT, "no baseline yet - run with --init once")
        return

    import json
    with io.open(LOCK, "r", encoding="utf-8") as fh:
        approved = json.load(fh)

    drifted = []
    for fname, names in approved.items():
        p = os.path.join(NAPCAT, fname)
        if not os.path.isfile(p):
            continue
        text = read_text(p)
        original = text
        changed = False
        for name, want in names.items():
            got = extract(text, name)
            if got is not None and got != want:
                drifted.append("%s.%s %s -> %s (restored)" % (fname, name, got, want))
                text = set_value(text, name, want)
                changed = True
        if changed:
            tmp = p + ".tmp"
            with io.open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, p)

    if drifted:
        for d in drifted:
            log(ALERT, d)
    else:
        log(ALERT, "ok - settings match the approved baseline")


if __name__ == "__main__":
    main()
