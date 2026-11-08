"""Preview/apply the single reviewed dsh-onebot-qq 0.8.1 newline burst fix.

No default installation path, package replacement, host restart or QQ calls.
Only the known splitter tail may change; local customizations before it survive.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import uuid


ORIGINAL_SHAPE = "b6d0ddc5e6ba1d7b420d7d49d91f9721107ebe9fb4d76335de7bf45abbb26ece"
PATCHED_SHAPE = "a8336d616f496a762220da9a8e6d7f590f3f00f66a7c1b9458a5ad33ffaa6307"
MARKER = b"const MAX_PIECES = 3;"
OLD = b"            return flat;"
NEW = b"            return mergeOverflow(flat);"


class PatchError(RuntimeError):
    pass


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def ordinary_path(value):
    path = Path(os.path.abspath(os.fspath(value)))
    for part in [*reversed(path.parents), path]:
        if os.path.lexists(part):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise PatchError("Link/reparse path refused: " + str(part))
    return path


def read_regular(path):
    ordinary_path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise PatchError("Expected an ordinary file: " + str(path))
    raw = path.read_bytes()
    after = path.lstat()
    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_mode)
    if signature(before) != signature(after):
        raise PatchError("File changed while reading: " + str(path))
    return raw, signature(after)


def probe(plugin_dir):
    plugin = ordinary_path(plugin_dir)
    package_raw, _ = read_regular(plugin / "package.json")
    package = json.loads(package_raw.decode("utf-8-sig"))
    if not isinstance(package, dict) or (package.get("name"), package.get("version"), package.get("type")) != (
            "dsh-onebot-qq", "0.8.1", "module"):
        raise PatchError("Only the reviewed dsh-onebot-qq 0.8.1 module layout is supported")
    target = ordinary_path(plugin / "lib" / "outbound.js")
    raw, signature = read_regular(target)
    if raw.count(MARKER) != 1:
        raise PatchError("Unknown splitter shape: expected one MAX_PIECES declaration")
    start = raw.index(MARKER)
    tail = raw[start:]
    normalized = tail.decode("utf-8").replace("\r\n", "\n").strip().encode("utf-8")
    shape = sha256(normalized)
    if shape == ORIGINAL_SHAPE and tail.count(OLD) == 1:
        patched = raw[:start] + tail.replace(OLD, NEW, 1)
        status = "patch_needed"
    elif shape == PATCHED_SHAPE:
        patched, status = raw, "already_patched"
    else:
        raise PatchError("Unknown splitter shape; no file was changed")
    return target, raw, patched, signature, status


def write_new(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def backup_original(backup_dir, target, original, patched):
    base = ordinary_path(backup_dir)
    base.mkdir(parents=True, exist_ok=True)
    ordinary_path(base)
    if not base.is_dir():
        raise PatchError("Backup destination must be a directory")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = base / ("onebot-burst-" + stamp + "-" + uuid.uuid4().hex)
    backup.mkdir()
    write_new(backup / "outbound.js", original)
    write_new(backup / "outbound.js.sha256", (sha256(original) + "  outbound.js\n").encode("ascii"))
    metadata = {"patch": "onebot-0.8.1-newline-burst", "target": str(target),
                "original_sha256": sha256(original), "patched_sha256": sha256(patched),
                "replacement": "return flat; -> return mergeOverflow(flat);"}
    write_new(backup / "patch.json", (json.dumps(metadata, indent=2) + "\n").encode("utf-8"))
    if (backup / "outbound.js").read_bytes() != original:
        raise PatchError("Backup verification failed; original file was not replaced")
    return backup


def patch(plugin_dir, *, apply=False, expected_sha256=None, backup_dir=None):
    if apply and (not expected_sha256 or backup_dir is None):
        raise PatchError("--apply requires --expected-sha256 from preview and an explicit --backup-dir")
    target, original, patched, signature, status = probe(plugin_dir)
    original_hash = sha256(original)
    result = {"status": status, "target": str(target), "original_sha256": original_hash,
              "patched_sha256": sha256(patched), "applied": False}
    if not apply:
        return result
    if expected_sha256 != original_hash:
        raise PatchError("Preview SHA mismatch; preview the current file again")
    if status == "already_patched":
        return result
    backup = backup_original(backup_dir, target, original, patched)
    result["backup"] = str(backup)
    temporary = target.with_name(".outbound-burst-" + uuid.uuid4().hex + ".js")
    try:
        write_new(temporary, patched)
        os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
        node = shutil.which("node")
        if not node:
            raise PatchError("Node is required for the syntax check; original file was not replaced")
        check = subprocess.run([node, "--check", str(temporary)], capture_output=True, timeout=30)
        if check.returncode:
            raise PatchError("JavaScript syntax check failed; original file was not replaced")
        current, current_signature = read_regular(target)
        if current_signature != signature or current != original:
            raise PatchError("Target changed after preview/backup; original file was not replaced")
        ordinary_path(temporary)
        os.replace(temporary, target)
        result.update(status="patched", applied=True)
        return result
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-dir", required=True, help="Explicit dsh-onebot-qq package directory")
    parser.add_argument("--apply", action="store_true", help="Apply only after an unchanged-SHA preview")
    parser.add_argument("--expected-sha256", help="original_sha256 printed by preview")
    parser.add_argument("--backup-dir", help="Explicit private backup directory (apply only)")
    args = parser.parse_args(argv)
    try:
        result = patch(args.plugin_dir, apply=args.apply, expected_sha256=args.expected_sha256,
                       backup_dir=args.backup_dir)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (PatchError, OSError, ValueError, subprocess.SubprocessError) as error:
        print("patch refused: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
