"""Preview, archive and restore explicitly reviewed workspace entries.

Only top-level ordinary files/directories may be sources. Directory moves use a
same-volume rename and preserve everything, including ignored Git files. Links
inside a directory are inventoried but never traversed. No recursive delete or
cross-volume copy fallback exists. All audit artifacts remain in the workspace.
"""
import argparse
import ctypes
import datetime
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import uuid


class ArchiveError(RuntimeError):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def digest(value):
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def signature(info):
    return {"mode": info.st_mode, "device": info.st_dev, "inode": info.st_ino,
            "size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "attributes": getattr(info, "st_file_attributes", 0),
            "reparse_tag": getattr(info, "st_reparse_tag", 0), "hard_links": info.st_nlink}


def absolute(path):
    return pathlib.Path(os.path.abspath(os.fspath(path)))


def below(path, parent):
    try:
        return os.path.commonpath([str(path), str(parent)]) == str(parent)
    except ValueError:
        return False


def no_link_ancestors(path):
    path = absolute(path)
    for component in [*reversed(path.parents), path]:
        if os.path.lexists(component) and is_link(component.lstat()):
            raise ArchiveError("Link/reparse path component refused: " + str(component))


def workspace_path(path):
    root = absolute(path)
    no_link_ancestors(root)
    if not root.is_dir():
        raise ArchiveError("Workspace must be an existing ordinary directory")
    return root


def child(root, relative):
    if not isinstance(relative, str) or not relative:
        raise ArchiveError("Expected a nonempty workspace-relative path")
    windows = pathlib.PureWindowsPath(relative)
    if windows.drive or windows.root or relative.startswith("/"):
        raise ArchiveError("Absolute paths are not permitted in a plan")
    parts = relative.replace("\\", "/").split("/")
    for part in parts:
        if (not part or part in (".", "..") or part[-1:] in (".", " ") or
                any(char in part for char in '\x00<>:"|?*') or
                re.match(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", part)):
            raise ArchiveError("Unsafe relative path: " + relative)
    result = absolute(root.joinpath(*parts))
    if result == root or not below(result, root):
        raise ArchiveError("Path escapes workspace")
    no_link_ancestors(result)
    return result


def relative(root, path):
    return path.relative_to(root).as_posix()


def ensure_directories(root, target):
    if not below(target, root):
        raise ArchiveError("Directory escapes workspace")
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if not os.path.lexists(current):
            os.mkdir(current)
        info = current.lstat()
        if is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise ArchiveError("Destination parent is not an ordinary directory")


def validate_parent(root, target):
    """Check every existing parent before a batch starts changing anything."""
    no_link_ancestors(target)
    current = root
    device = root.stat().st_dev
    for part in target.relative_to(root).parts:
        current = current / part
        if not os.path.lexists(current):
            continue
        info = current.lstat()
        if is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise ArchiveError("Destination parent is not an ordinary directory: " + str(current))
        if info.st_dev != device:
            raise ArchiveError("Cross-volume destination parent refused")


def file_hash(path, initial):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        def matches_open_file():
            opened, expected = signature(os.fstat(stream.fileno())), signature(initial)
            if os.name == "nt":
                # Windows adds execute bits by filename (.cmd/.exe) in path stat,
                # while descriptor stat has no filename. Attributes still retain
                # read-only flags; the final path comparison checks every field.
                opened["mode"] = stat.S_IFMT(opened["mode"])
                expected["mode"] = stat.S_IFMT(expected["mode"])
            return opened == expected

        if not matches_open_file():
            raise ArchiveError("File changed before hashing: " + str(path))
        result = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
        if not matches_open_file():
            raise ArchiveError("File changed while hashing: " + str(path))
    if signature(path.lstat()) != signature(initial):
        raise ArchiveError("File replaced while hashing: " + str(path))
    return result.hexdigest()


def git_metadata(repo):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    prefix = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(repo)]
    try:
        head_result = subprocess.run(prefix + ["rev-parse", "--verify", "HEAD"], env=env,
                                     capture_output=True, timeout=30)
        head = head_result.stdout.decode().strip() if head_result.returncode == 0 else None
        branch_result = subprocess.run(prefix + ["symbolic-ref", "--quiet", "--short", "HEAD"], env=env,
                                       capture_output=True, timeout=30)
        branch = branch_result.stdout.decode("utf-8", "replace").strip() if branch_result.returncode == 0 else None
        status_output = subprocess.run(prefix + ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
                                       env=env, check=True, capture_output=True, timeout=30).stdout
        return {"head": head, "branch": branch, "dirty": bool(status_output),
                "status_entries": [entry for entry in status_output.decode("utf-8", "replace").split("\0") if entry]}
    except (OSError, subprocess.SubprocessError):
        raise ArchiveError("Could not inventory Git metadata: " + str(repo))


def snapshot(root, source):
    no_link_ancestors(source)
    records = []
    repos = []
    device = root.stat().st_dev

    def visit(path, name):
        info = path.lstat()
        if info.st_dev != device:
            raise ArchiveError("Cross-volume subtree refused: " + str(path))
        record = {"path": name, **signature(info)}
        if is_link(info):
            record["kind"] = "link"
            try:
                record["target"] = os.readlink(path)
            except OSError:
                record["target"] = None
            records.append(record)
            return
        if stat.S_ISREG(info.st_mode):
            record.update(kind="file", sha256=file_hash(path, info))
        elif stat.S_ISDIR(info.st_mode):
            record["kind"] = "directory"
        else:
            raise ArchiveError("Special filesystem entry refused: " + str(path))
        records.append(record)
        if record["kind"] == "directory":
            with os.scandir(path) as scanner:
                entries = sorted(scanner, key=lambda entry: entry.name)
            for entry in entries:
                entry_path = path / entry.name
                if entry.name == ".git":
                    marker_info = entry_path.lstat()
                    if is_link(marker_info) or not stat.S_ISDIR(marker_info.st_mode):
                        raise ArchiveError("Linked Git worktrees require a separate migration")
                    repos.append({"path": name, **git_metadata(path)})
                visit(entry_path, name + "/" + entry.name if name else entry.name)
            if signature(path.lstat()) != signature(info):
                raise ArchiveError("Directory changed while inventorying: " + str(path))

    visit(source, "")
    if records[0]["kind"] == "link":
        raise ArchiveError("A source cannot itself be a link/reparse point")
    return {"records": records, "sha256": digest(records), "git": repos,
            "file_count": sum(record["kind"] == "file" for record in records),
            "link_count": sum(record["kind"] == "link" for record in records),
            "bytes": sum(record["size"] for record in records if record["kind"] == "file")}


def read_json(root, path):
    no_link_ancestors(path)
    if not below(path, root) or not stat.S_ISREG(path.lstat().st_mode):
        raise ArchiveError("Plan/manifest must be a regular workspace file")
    with path.open(encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ArchiveError("Plan/manifest must be a JSON object")
    return value


def write_manifest(root, path, value, first=False):
    no_link_ancestors(path)
    if first:
        target = path
    else:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ArchiveError("Manifest was replaced")
        target = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    no_link_ancestors(target)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if not first:
        no_link_ancestors(path)
        os.replace(target, path)


def rename_noreplace(source, target):
    """Atomic, same-volume rename without a clobber/copy fallback."""
    if os.path.lexists(target):
        raise ArchiveError("Destination already exists: " + str(target))
    if os.name == "nt":
        os.rename(source, target)  # Windows rename refuses an existing destination.
    elif sys.platform.startswith("linux"):
        library = ctypes.CDLL(None, use_errno=True)
        rename = getattr(library, "renameat2", None)
        if rename is None:
            raise ArchiveError("This platform has no safe directory rename primitive")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
    else:
        raise ArchiveError("Safe no-replace rename is unavailable on this platform")


def validate_layout(root, document):
    archive = child(root, document.get("archive_root", "_archive"))
    manifest = child(root, document.get("manifest", ""))
    if manifest == archive or not below(manifest, archive):
        raise ArchiveError("Manifest must be below archive_root")
    entries = []
    skipped = []
    for item in document.get("items", []):
        if not isinstance(item, dict):
            raise ArchiveError("Plan items must be objects")
        if item.get("status") != "approved":
            skipped.append({"source": item.get("source"), "status": item.get("status", "review_required")})
            continue
        source = child(root, item.get("source", ""))
        target = child(root, item.get("destination", ""))
        if source.parent != root:
            raise ArchiveError("Archive sources must be top-level workspace entries")
        if source.name.casefold() in ("agents.md", "readme.md", ".git"):
            raise ArchiveError("Protected root entry refused: " + source.name)
        if source.name.casefold() == "bili-qr.png" and item.get("reviewed_root_copy") is not True:
            raise ArchiveError("Root QR copy needs an explicit reviewed_root_copy approval")
        if source.suffix.lower() in (".db", ".sqlite", ".sqlite3") and item.get("database_inactive") is not True:
            raise ArchiveError("Database needs an explicit inactive-snapshot review")
        if source.name.lower().endswith((".lock", "-wal", "-shm")):
            raise ArchiveError("Lock/active database sidecar refused")
        for protected in document.get("protected_roots", []):
            protected_path = child(root, protected)
            if below(source, protected_path) or below(protected_path, source):
                raise ArchiveError("Protected source refused: " + source.name)
        if below(archive, source) or below(source, archive):
            raise ArchiveError("Archive root and sources must not overlap")
        if target == archive or not below(target, archive):
            raise ArchiveError("Every destination must be below archive_root")
        if below(manifest, target) or below(target, manifest):
            raise ArchiveError("Manifest and destination must not overlap")
        entries.append({"source": relative(root, source), "destination": relative(root, target),
                        "expected_sha256": item.get("sha256"), "expected_size": item.get("size_bytes"),
                        "database_inactive": item.get("database_inactive") is True,
                        "reviewed_root_copy": item.get("reviewed_root_copy") is True})
    paths = [child(root, entry["source"]) for entry in entries]
    targets = [child(root, entry["destination"]) for entry in entries]
    for values in (paths, targets):
        for index, path in enumerate(values):
            for other in values[:index]:
                if below(path, other) or below(other, path) or str(path).casefold() == str(other).casefold():
                    raise ArchiveError("Duplicate/overlapping sources or destinations")
    return archive, manifest, entries, skipped


def prepare(root, document):
    archive, manifest, entries, skipped = validate_layout(root, document)
    if os.path.lexists(manifest):
        raise ArchiveError("Manifest already exists; choose a new batch")
    validate_parent(root, manifest.parent)
    for entry in entries:
        source = child(root, entry["source"])
        target = child(root, entry["destination"])
        if not os.path.lexists(source) or os.path.lexists(target):
            raise ArchiveError("Source missing or destination occupied: " + entry["source"])
        validate_parent(root, target.parent)
        entry["snapshot"] = snapshot(root, source)
        if entry["expected_sha256"]:
            records = entry["snapshot"]["records"]
            if records[0]["kind"] != "file" or records[0]["sha256"] != entry["expected_sha256"]:
                raise ArchiveError("Reviewed file hash changed: " + entry["source"])
        if entry["expected_size"] is not None and source.is_file() and source.stat().st_size != entry["expected_size"]:
            raise ArchiveError("Reviewed file size changed: " + entry["source"])
    return {"version": 1, "workspace": str(root), "archive_root": relative(root, archive),
            "manifest": relative(root, manifest), "plan_sha256": digest(document), "created": now(),
            "protected_roots": list(document.get("protected_roots", [])),
            "state": "preview", "items": entries, "skipped": skipped}


def assert_snapshot(root, path, expected):
    actual = snapshot(root, path)
    if actual["sha256"] != expected["sha256"]:
        raise ArchiveError("Filesystem changed since review: " + str(path))


def summary(manifest):
    return {"state": manifest["state"], "manifest": manifest["manifest"],
            "count": len(manifest["items"]), "bytes": sum(entry["snapshot"]["bytes"] for entry in manifest["items"]),
            "files": sum(entry["snapshot"]["file_count"] for entry in manifest["items"]),
            "links": sum(entry["snapshot"]["link_count"] for entry in manifest["items"]),
            "skipped": len(manifest.get("skipped", [])),
            "moves": [{"source": entry["source"], "destination": entry["destination"],
                       "files": entry["snapshot"]["file_count"], "bytes": entry["snapshot"]["bytes"],
                       "git": entry["snapshot"]["git"]} for entry in manifest["items"]]}


def apply_plan(root, document):
    manifest = prepare(root, document)
    if not manifest["items"]:
        raise ArchiveError("No reviewed entries to archive")
    # Check the entire batch again before creating any directory or moving anything.
    for entry in manifest["items"]:
        assert_snapshot(root, child(root, entry["source"]), entry["snapshot"])
    path = child(root, manifest["manifest"])
    ensure_directories(root, path.parent)
    manifest["state"] = "prepared"
    write_manifest(root, path, manifest, first=True)
    try:
        for entry in manifest["items"]:
            source, target = child(root, entry["source"]), child(root, entry["destination"])
            assert_snapshot(root, source, entry["snapshot"])
            ensure_directories(root, target.parent)
            if target.parent.stat().st_dev != source.lstat().st_dev:
                raise ArchiveError("Cross-volume rename refused")
            no_link_ancestors(source)
            no_link_ancestors(target)
            rename_noreplace(source, target)
            entry["state"] = "moved"
            assert_snapshot(root, target, entry["snapshot"])
            write_manifest(root, path, manifest)
        manifest["state"] = "archived"
        manifest["completed"] = now()
        write_manifest(root, path, manifest)
    except Exception as error:
        manifest["state"] = "interrupted"
        manifest["error_type"] = type(error).__name__
        write_manifest(root, path, manifest)
        raise
    return manifest


def restore(root, path, apply=False):
    manifest = read_json(root, path)
    if manifest.get("version") != 1 or manifest.get("workspace") != str(root):
        raise ArchiveError("Manifest version/workspace mismatch")
    # Reuse path/overlap checks, including the chosen archive root.
    layout = dict(manifest, items=[dict(entry, status="approved") for entry in manifest["items"]])
    _, checked_manifest, _, _ = validate_layout(root, layout)
    if checked_manifest != path:
        raise ArchiveError("Manifest path mismatch")
    to_restore = []
    for entry in manifest["items"]:
        source, target = child(root, entry["source"]), child(root, entry["destination"])
        source_exists, target_exists = os.path.lexists(source), os.path.lexists(target)
        if source_exists and target_exists:
            raise ArchiveError("Restore conflict; original path exists: " + entry["source"])
        if not source_exists and not target_exists:
            raise ArchiveError("Both original and archived entry are missing")
        assert_snapshot(root, target if target_exists else source, entry["snapshot"])
        if target_exists:
            to_restore.append(entry)
    if not apply:
        result = summary(manifest)
        result.update(state="restore_preview", restore_count=len(to_restore))
        return result
    try:
        for entry in to_restore:
            source, target = child(root, entry["source"]), child(root, entry["destination"])
            assert_snapshot(root, target, entry["snapshot"])
            rename_noreplace(target, source)
            entry["state"] = "restored"
            assert_snapshot(root, source, entry["snapshot"])
            write_manifest(root, path, manifest)
        manifest["state"] = "restored"
        manifest["restored"] = now()
        write_manifest(root, path, manifest)
    except Exception as error:
        manifest["state"] = "restore_interrupted"
        manifest["error_type"] = type(error).__name__
        write_manifest(root, path, manifest)
        raise
    result = summary(manifest)
    result["restore_count"] = len(to_restore)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--plan", help="Workspace-relative reviewed JSON plan")
    inputs.add_argument("--restore", help="Workspace-relative archive manifest")
    parser.add_argument("--apply", action="store_true", help="Explicitly perform moves; otherwise preview only")
    args = parser.parse_args(argv)
    try:
        root = workspace_path(args.workspace)
        if args.restore:
            result = restore(root, child(root, args.restore), apply=args.apply)
        else:
            plan = read_json(root, child(root, args.plan))
            manifest = apply_plan(root, plan) if args.apply else prepare(root, plan)
            result = summary(manifest)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (ArchiveError, OSError, ValueError, KeyError, TypeError) as error:
        print("archive refused: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
