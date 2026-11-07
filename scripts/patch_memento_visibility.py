"""Replay the reviewed dsh-memento 0.5.23 session visibility fix.

Preview by default. Only two public program files are backed up/patched; no
profile configuration or memory database is opened. Atomic replacement gives
hardlinked installation files private inodes, leaving shared package sources
unchanged. Symlink/junction ancestors are refused.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


PATCH_ID = "memento-session-visibility-v1"
VERSION = "0.5.23"
BASELINE_HASHES = {
    "lib/protocol.mjs": "797db785e5ce355dd7c54e1db8dfe1482f246666a143efc8a435933cc176281e",
    "lib/store.mjs": "d7e0b3b4974e4527aef7e2647fe1e5809a8eec36b6c7ef3222f7ce3e75807359",
}
QUERY_START = "  query(filter = {}, opts = {}) {\n    const { entries, total, truncated } = this.store.queryEntries({\n"
QUERY_PATCH = """  query(filter = {}, opts = {}) {
    // dsh-lanya: memento-session-visibility-v1
    // Only an explicit management call (no options) may see all workspaces.
    const scoped = Object.keys(opts).length > 0
    const header = opts.session?.header
    const hasSession = header !== null && typeof header === 'object' && typeof header.cwd === 'string'
    const visibleKeys = scoped ? {
      workspaceKey: hasSession ? workspaceKeyOf(header.cwd) : '',
      agentKey: hasSession ? agentKeyOf(header.agentPreset) : '',
    } : {}
    const { entries, total, truncated } = this.store.queryEntries({
"""
AGENT_FORWARD = "      ...(typeof opts.agentKey === 'string' ? { agentKey: opts.agentKey } : {}),\n"
STORE_ANCHOR = """        conditions.push("(agent_key = '' OR agent_key = ?)"); params.push(filter.agentKey)
      }
      const where"""
STORE_PATCH = """        conditions.push("(agent_key = '' OR agent_key = ?)"); params.push(filter.agentKey)
      }
      // dsh-lanya: memento-session-visibility-v1
      if (typeof filter.workspaceKey === 'string') {
        conditions.push("(scope = 'user-global' OR (scope = 'workspace' AND workspace_key = ?))")
        params.push(filter.workspaceKey)
      }
      const where"""
REPLACEMENTS = {
    "lib/protocol.mjs": [
        ("limit?: number, agentKey?: string}) => import('../types.js').MemoryQueryResult} queryEntries",
         "limit?: number, agentKey?: string, workspaceKey?: string}) => import('../types.js').MemoryQueryResult} queryEntries"),
        (QUERY_START, QUERY_PATCH),
        (AGENT_FORWARD, "      ...visibleKeys,\n"),
    ],
    "lib/store.mjs": [
        (" * @property {string} [agentKey]\n * @typedef {object} AuditInput",
         " * @property {string} [agentKey]\n * @property {string} [workspaceKey]\n * @typedef {object} AuditInput"),
        (STORE_ANCHOR, STORE_PATCH),
    ],
}


class PatchConflict(RuntimeError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def no_links(path):
    for part in [*reversed(path.parents), path]:
        if os.path.lexists(part):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise PatchConflict("Link/reparse paths are not supported")


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)


def read_regular(path):
    no_links(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise PatchConflict("Expected a regular file")
    data = path.read_bytes()
    if signature(path.lstat()) != signature(before):
        raise PatchConflict("File changed while reading")
    return data


def transform(relative, data, reverse=False):
    text = data.decode("utf-8")
    operations = REPLACEMENTS[relative]
    operations = [(after, before) for before, after in reversed(operations)] if reverse else operations
    for before, after in operations:
        if text.count(before) != 1:
            raise PatchConflict("Reviewed source anchor mismatch")
        text = text.replace(before, after, 1)
    return text.encode("utf-8")


def check_metadata(root):
    metadata = json.loads(read_regular(root / "package.json").decode("utf-8"))
    if metadata.get("name") != "dsh-memento" or metadata.get("version") != VERSION:
        raise PatchConflict("Unreviewed package name/version")


def inspect_package(package_root):
    root = Path(os.path.abspath(package_root))
    no_links(root)
    if not root.is_dir():
        raise PatchConflict("Package directory missing")
    check_metadata(root)
    entries = []
    for relative, baseline_hash in BASELINE_HASHES.items():
        path = root / relative
        current = read_regular(path)
        if digest(current) == baseline_hash:
            before, after, state = current, transform(relative, current), "original"
        else:
            before = transform(relative, current, reverse=True)
            if digest(before) != baseline_hash:
                raise PatchConflict("Unreviewed source hash")
            after, state = current, "patched"
        entries.append(dict(path=relative, before=before, after=after, current=current,
                            state=state, hard_links=path.lstat().st_nlink))
    return root, entries


def replace_private(path, data, expected_hash):
    current = read_regular(path)
    initial = path.lstat()
    if digest(current) != expected_hash:
        raise PatchConflict("Refusing to overwrite changed code")
    fd, temporary = tempfile.mkstemp(prefix=".memento-visibility-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(initial.st_mode))
        if signature(path.lstat()) != signature(initial) or digest(read_regular(path)) != expected_hash:
            raise PatchConflict("Code changed before replacement")
        os.replace(temporary, path)
        if read_regular(path) != data:
            raise PatchConflict("Replacement verification failed")
    finally:
        if temporary.exists():
            temporary.unlink()


def write_new(path, data):
    no_links(path)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def record_bytes(record):
    return (json.dumps(record, ensure_ascii=True, indent=2) + "\n").encode("utf-8")


def summary(entries, state):
    return dict(patch_id=PATCH_ID, version=VERSION, state=state,
                files=[dict(path=entry["path"], before_sha256=digest(entry["before"]),
                            after_sha256=digest(entry["after"]), hard_links=entry["hard_links"])
                       for entry in entries])


@contextmanager
def package_lock(package_root):
    root = Path(os.path.abspath(package_root))
    no_links(root)
    path = root / ".dsh-lanya-visibility.lock"
    with path.open("xb") as stream:
        stream.write(str(os.getpid()).encode("ascii"))
        identity = signature(os.fstat(stream.fileno()))[:2]
    try:
        yield
    finally:
        no_links(root)
        if os.path.lexists(path) and signature(path.lstat())[:2] == identity:
            path.unlink()


def patch_package(package_root, apply=False, backup_root=None):
    if not apply:
        return _patch_package(package_root, False, backup_root)
    with package_lock(package_root):
        return _patch_package(package_root, True, backup_root)


def _patch_package(package_root, apply=False, backup_root=None):
    root, entries = inspect_package(package_root)
    states = {entry["state"] for entry in entries}
    if states == {"patched"}:
        return summary(entries, "already_patched")
    if states != {"original"}:
        raise PatchConflict("Mixed patch state; inspect the saved restore manifest")
    if not apply:
        return summary(entries, "preview")
    if backup_root is None:
        raise PatchConflict("Apply requires an explicit private backup_root")
    backup = Path(os.path.abspath(backup_root))
    no_links(backup)
    if backup == root or root in backup.parents or backup in root.parents:
        raise PatchConflict("Backup and package paths must be separate")
    # Exclusive creation preserves the first original instead of replacing it.
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    (backup / "lib").mkdir()
    for entry in entries:
        write_new(backup / entry["path"], entry["before"])
    record = dict(summary(entries, "prepared"), package_root=str(root))
    manifest = backup / "manifest.json"
    write_new(manifest, record_bytes(record))
    try:
        # Recheck the entire batch before replacing either linked filename.
        check_metadata(root)
        for entry in entries:
            if read_regular(root / entry["path"]) != entry["before"]:
                raise PatchConflict("Code changed after preview")
        for entry in entries:
            replace_private(root / entry["path"], entry["after"], digest(entry["before"]))
        record["state"] = "patched"
    except Exception:
        record["state"] = "rolled_back"
        for entry in reversed(entries):
            try:
                current = read_regular(root / entry["path"])
                if current == entry["after"]:
                    replace_private(root / entry["path"], entry["before"], digest(entry["after"]))
                elif current != entry["before"]:
                    record["state"] = "rollback_conflict"
            except Exception:
                record["state"] = "rollback_conflict"
        replace_private(manifest, record_bytes(record), digest(read_regular(manifest)))
        raise
    replace_private(manifest, record_bytes(record), digest(read_regular(manifest)))
    return dict(summary(entries, "patched"), manifest=str(manifest))


def restore_package(package_root, manifest_path, apply=False):
    if not apply:
        return _restore_package(package_root, manifest_path, False)
    with package_lock(package_root):
        return _restore_package(package_root, manifest_path, True)


def _restore_package(package_root, manifest_path, apply=False):
    root, entries = inspect_package(package_root)
    manifest = Path(os.path.abspath(manifest_path))
    original_record = read_regular(manifest)
    record = json.loads(original_record.decode("utf-8"))
    if (record.get("patch_id") != PATCH_ID or record.get("version") != VERSION or
            record.get("package_root") != str(root) or
            [entry.get("path") for entry in record.get("files", [])] != list(BASELINE_HASHES)):
        raise PatchConflict("Restore manifest identity mismatch")
    for entry, saved in zip(entries, record["files"]):
        before = read_regular(manifest.parent / entry["path"])
        if (digest(before) != BASELINE_HASHES[entry["path"]] or before != entry["before"] or
                saved.get("before_sha256") != digest(before) or
                saved.get("after_sha256") != digest(entry["after"])):
            raise PatchConflict("Restore original/hash mismatch")
    if not apply:
        return summary(entries, "restore_preview")
    # The whole batch above must match before any reverse write is attempted.
    check_metadata(root)
    try:
        for entry in reversed(entries):
            if entry["state"] == "patched":
                replace_private(root / entry["path"], entry["before"], digest(entry["after"]))
    except Exception:
        record["state"] = "restore_interrupted"
        replace_private(manifest, record_bytes(record), digest(original_record))
        raise
    record["state"] = "restored"
    replace_private(manifest, record_bytes(record), digest(original_record))
    return summary(entries, "restored")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", required=True)
    parser.add_argument("--backup-root", help="New private directory; required for --apply patch")
    parser.add_argument("--restore", help="Saved manifest; restore previews unless --apply is given")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.restore:
            result = restore_package(args.package_root, args.restore, apply=args.apply)
        else:
            result = patch_package(args.package_root, apply=args.apply, backup_root=args.backup_root)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception as error:
        print(type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
