"""Preview the independent current-session raw-history fix for memento 0.5.23.

Program files only: never opens a memory/session store or starts the host.
Apply/restore need an explicit stopped-host boolean and fresh fingerprint.
Restore this layer before native-history, then the older visibility layer.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import uuid

import patch_memento_history as native
import patch_memento_visibility as visibility
from patch_onebot_policy import ordinary_path, read_regular, sha256, write_new, PatchError

REPO = Path(__file__).resolve().parents[1]
PATCH_ID = "lanya-memento-recall-scope-v1"
INDEX = "index.mjs"
MODULE = "lib/lanya-memento-recall-scope.mjs"
MARKER = "lib/lanya-memento-recall-scope.patch.json"
INPUT_SHA256 = "a05b8a2b29c0164f6524226b1dda45e47a3a58b1f574856c7f3c225b3f96f1a5"
EXTRACT_SHA256 = "b5ef107a9de5d1fecb98988ae78ffc864b978c1849764cd08a1cb1060001494f"
PRIOR_PROGRAM_HASHES = {
    "lib/store.mjs": "8f17878db218ebedb599ad32fc44082b333197ea053039397b4af1ea4f1984fc",
    "lib/protocol.mjs": "30b113587c1457fe775bdca3e8895b778df217fb1a9d15a6267a39c38cab5913",
    "lib/lanya-memento-history.mjs": "912258adc3a3814708cd7eba4d85c53354a36d396989399dba41b3532ed8873a",
}
SOURCE = REPO / "plugin/lib/memento-recall-scope.mjs"
PREFIX = "// LANYA_MEMENTO_RECALL_SCOPE_V1\n"


def exact(text, before, after):
    if text.count(before) != 1:
        raise PatchError("Unknown or ambiguous recall-scope anchor")
    return text.replace(before, after, 1)


def transform(raw):
    if sha256(raw) != INPUT_SHA256:
        raise PatchError("Unknown memento native index")
    text = raw.decode("utf-8")
    text = exact(text, "import { extractEventText } from './lib/extract.mjs'",
        "import { extractEventText } from './lib/extract.mjs'\n"
        "import { snapshotRecallOwner, assertRecallNotAborted, recallSessionHistory } from './lib/lanya-memento-recall-scope.mjs'")
    start = text.index("export function makeMemoryRecallTool(")
    end = text.index("\n/**\n * memory_recall 结果渲染", start)
    block = text[start:end]
    block = exact(block, "      exec.signal.throwIfAborted()", "      assertRecallNotAborted(exec.signal)")
    anchor = "      const session = /** @type {MemorySessionLike | null | undefined} */ (exec.agent?.session ?? null)"
    block = exact(block, anchor, anchor + "\n      const historyOwner = snapshotRecallOwner(session)")
    block = exact(block, "        /** @type {string | undefined} */ (exec.agent?.session?.header?.cwd),", "        historyOwner,")
    old_start = block.index("async function recallHistory(")
    old_end = block.index("\n}", old_start) + 2
    block = block[:old_start] + """async function recallHistory(ctx, query, limit, snippetCap, snippetChars, signal, owner, windowDays) {
  return recallSessionHistory({ getSessionQuery: () => ctx.get('sessionQuery'),
    owner, query, limit, snippetCap, snippetChars, signal, windowDays, extractEventText })
}""" + block[old_end:]
    return (PREFIX + text[:start] + block + text[end:]).encode("utf-8")


def seal(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def snapshot(root, names):
    return native.snapshot(root, names)


def metadata(plugin):
    reviewed, *_ = native.preview(plugin)
    if reviewed["status"] != "already_patched":
        raise PatchError("Reviewed visibility and native-history layers are required first")
    prior = snapshot(plugin, PRIOR_PROGRAM_HASHES)
    if any(value is None or value["sha256"] != PRIOR_PROGRAM_HASHES[name] for name, value in prior.items()):
        raise PatchError("Reviewed prior native program changed")
    extract, signature = read_regular(plugin / "lib/extract.mjs")
    if sha256(extract) != EXTRACT_SHA256:
        raise PatchError("Native event text extractor changed")
    return dict(native_fingerprint=reviewed["fingerprint"], extractor=dict(sha256=sha256(extract), signature=list(signature)))


def private_manifest(plugin, path):
    path = ordinary_path(path)
    if path.name != "patch.json" or path == plugin or plugin in path.parents or path == REPO or REPO in path.parents:
        raise PatchError("Private recall-scope manifest required outside source/install")
    raw, signature = read_regular(path)
    record = json.loads(raw)
    required = {"patch", "state", "plugin", "backup", "metadata", "before", "after"}
    if (not isinstance(record, dict) or set(record) != required or record["patch"] != PATCH_ID
        or record["state"] != "prepared" or record["plugin"] != str(plugin)
        or record["backup"] != str(path.parent) or record["before"] != {INDEX: INPUT_SHA256}
        or set(record["after"]) != {INDEX, MODULE}):
        raise PatchError("Recall-scope manifest identity mismatch")
    original, original_signature = read_regular(path.parent / INDEX)
    helper, helper_signature = read_regular(path.parent / "generated.mjs")
    if (sha256(original) != INPUT_SHA256 or sha256(transform(original)) != record["after"][INDEX]
        or sha256(helper) != record["after"][MODULE] or helper != read_regular(SOURCE)[0]):
        raise PatchError("Recall-scope original or module changed")
    return record, original, dict(sha256=sha256(raw), signature=signature,
        original_signature=original_signature, helper_signature=helper_signature)


def preview(plugin_dir):
    plugin = ordinary_path(plugin_dir)
    meta = metadata(plugin)
    current = snapshot(plugin, (INDEX, MODULE, MARKER))
    helper, helper_signature = read_regular(SOURCE)
    if current[MARKER]:
        installed = json.loads(read_regular(plugin / MARKER)[0])
        record, original, manifest = private_manifest(plugin, Path(installed["backup"]) / "patch.json")
        if read_regular(plugin / MARKER)[0] != encoded(dict(record, state="patched")) or record["metadata"] != meta:
            raise PatchError("Recall-scope marker or dependency changed")
        outputs = {INDEX: transform(original), MODULE: helper}
        if any(not current[name] or current[name]["sha256"] != sha256(raw) for name, raw in outputs.items()):
            raise PatchError("Installed recall-scope program changed")
        status = "already_patched"
    else:
        if current[MODULE]:
            raise PatchError("Unowned recall-scope module exists")
        original = read_regular(plugin / INDEX)[0]
        outputs = {INDEX: transform(original), MODULE: helper}
        manifest = None
        status = "patch_needed"
    state = dict(plugin=str(plugin), metadata=meta, current=current, source=dict(sha256=sha256(helper), signature=helper_signature),
                 manifest=manifest, after={name: sha256(raw) for name, raw in outputs.items()})
    return dict(status=status, applied=False, fingerprint=seal(state), after=state["after"], requires_stopped_host=True), plugin, original, outputs, state


def stages(plugin, outputs):
    result = {}
    try:
        node = shutil.which("node")
        if not node:
            raise PatchError("Node syntax checker required")
        for name, raw in outputs.items():
            path = plugin / (".recall-scope-" + uuid.uuid4().hex + ".mjs")
            write_new(path, raw)
            result[name] = path
            checked = subprocess.run([node, "--check", str(path)], capture_output=True, timeout=30)
            if checked.returncode:
                raise PatchError("Recall-scope JavaScript syntax failed")
        return result
    except Exception:
        for path in result.values():
            path.unlink()
        raise


def journal(backup, value):
    target = ordinary_path(backup / "journal.jsonl")
    before = target.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise PatchError("Journal must be an unlinked ordinary file")
    fd = os.open(target, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
    current = os.fstat(fd)
    if (current.st_dev, current.st_ino, current.st_nlink) != (before.st_dev, before.st_ino, 1):
        os.close(fd)
        raise PatchError("Journal identity changed")
    with os.fdopen(fd, "ab") as stream:
        stream.write(json.dumps(value, sort_keys=True).encode() + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def require_apply(apply, host_stopped, expected, actual):
    if apply is not True or host_stopped is not True or not isinstance(expected, str) or expected != actual:
        raise PatchError("Apply requires literal stopped-host true and fresh fingerprint")


def compensate(plugin, backup, initial, outputs, changed, marker_raw, expected_metadata):
    errors = []
    # A later/unknown change blocks the entire compensation batch before any
    # deletion; never remove a helper still referenced by an unowned index.
    try:
        if metadata(plugin) != expected_metadata:
            raise PatchError("Rollback dependency changed")
        for name in changed:
            expected = marker_raw if name == MARKER else outputs[name]
            if read_regular(ordinary_path(plugin / name))[0] != expected:
                raise PatchError("Changed rollback target refused")
    except Exception as error:
        write_new(backup / "failure.json", encoded(dict(patch=PATCH_ID, changed=changed,
            rollback_errors=[dict(error_class=type(error).__name__)])))
        return
    for name in reversed(changed):
        try:
            target = ordinary_path(plugin / name)
            expected = marker_raw if name == MARKER else outputs[name]
            if read_regular(target)[0] != expected:
                raise PatchError("Changed rollback target refused")
            journal(backup, dict(action="compensate_before", path=name))
            if name == INDEX:
                temporary = stages(plugin, {INDEX: initial})[INDEX]
                try:
                    if read_regular(target)[0] != expected:
                        raise PatchError("Rollback target changed")
                    os.replace(temporary, target)
                finally:
                    if temporary.exists(): temporary.unlink()
            else:
                target.unlink()
            journal(backup, dict(action="compensate_after", path=name))
        except Exception as error:
            errors.append(dict(path=name, error_class=type(error).__name__))
            break
    write_new(backup / "failure.json", encoded(dict(patch=PATCH_ID, changed=changed, rollback_errors=errors)))


def patch(plugin_dir, *, apply=False, host_stopped=False, expected_fingerprint=None, backup_dir=None):
    if type(apply) is not bool: raise PatchError("Boolean apply required")
    if not apply: return preview(plugin_dir)[0]
    with visibility.package_lock(plugin_dir):
        result, plugin, original, outputs, state = preview(plugin_dir)
        require_apply(apply, host_stopped, expected_fingerprint, result["fingerprint"])
        if result["status"] == "already_patched": return result
        if not backup_dir: raise PatchError("Private backup directory required")
        base = ordinary_path(backup_dir)
        if base == plugin or plugin in base.parents or base == REPO or REPO in base.parents:
            raise PatchError("Private backup must stay outside source/install")
        base.mkdir(parents=True, exist_ok=True)
        backup = base / (PATCH_ID + "-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex)
        backup.mkdir()
        write_new(backup / INDEX, original)
        write_new(backup / "generated.mjs", outputs[MODULE])
        record = dict(patch=PATCH_ID, state="prepared", plugin=str(plugin), backup=str(backup), metadata=state["metadata"],
            before={INDEX: INPUT_SHA256}, after=result["after"])
        write_new(backup / "patch.json", encoded(record))
        write_new(backup / "journal.jsonl", b"")
        pending = stages(plugin, outputs)
        changed = []
        marker_raw = encoded(dict(record, state="patched"))
        try:
            if preview(plugin)[0]["fingerprint"] != result["fingerprint"]:
                raise PatchError("Recall-scope inputs changed")
            private_manifest(plugin, backup / "patch.json")
            journal(backup, dict(action="write_before", path=MODULE))
            write_new(plugin / MODULE, outputs[MODULE]); changed.append(MODULE)
            if metadata(plugin) != state["metadata"] or snapshot(plugin, (INDEX,))[INDEX] != state["current"][INDEX]:
                raise PatchError("Native index changed before replacement")
            journal(backup, dict(action="write_before", path=INDEX))
            os.replace(pending[INDEX], plugin / INDEX); pending.pop(INDEX); changed.append(INDEX)
            if any(read_regular(plugin / name)[0] != raw for name, raw in outputs.items()):
                raise PatchError("Recall-scope output readback failed")
            journal(backup, dict(action="write_before", path=MARKER))
            write_new(plugin / MARKER, marker_raw); changed.append(MARKER)
            final = preview(plugin)[0]
            if final["status"] != "already_patched": raise PatchError("Recall-scope final verification failed")
            journal(backup, dict(action="verified", after=result["after"]))
            return dict(result, status="patched", applied=True, backup=str(backup))
        except Exception:
            compensate(plugin, backup, original, outputs, changed, marker_raw, state["metadata"])
            raise
        finally:
            for path in pending.values():
                if path.exists(): path.unlink()


def restore_preview(plugin_dir, manifest_file):
    plugin = ordinary_path(plugin_dir)
    record, original, manifest = private_manifest(plugin, manifest_file)
    meta = metadata(plugin)
    if meta != record["metadata"]:
        raise PatchError("Earlier patch dependency changed")
    current = snapshot(plugin, (INDEX, MODULE, MARKER))
    if not current[INDEX] or current[INDEX]["sha256"] not in (INPUT_SHA256, record["after"][INDEX]):
        raise PatchError("Later index edit prevents restore")
    if current[MODULE] and current[MODULE]["sha256"] != record["after"][MODULE]:
        raise PatchError("Later module edit prevents restore")
    if current[MARKER] and read_regular(plugin / MARKER)[0] != encoded(dict(record, state="patched")):
        raise PatchError("Later marker edit prevents restore")
    state = dict(plugin=str(plugin), metadata=meta, manifest=manifest, current=current)
    names = [INDEX] if current[INDEX]["sha256"] != INPUT_SHA256 else []
    remove = [name for name in (MODULE, MARKER) if current[name]]
    return dict(status="restore_preview" if names or remove else "already_restored", applied=False, fingerprint=seal(state),
        restore=names, remove=remove, requires_stopped_host=True), plugin, original, state, record


def restore(plugin_dir, manifest_file, *, apply=False, host_stopped=False, expected_fingerprint=None):
    if type(apply) is not bool: raise PatchError("Boolean apply required")
    if not apply: return restore_preview(plugin_dir, manifest_file)[0]
    with visibility.package_lock(plugin_dir):
        result, plugin, original, state, record = restore_preview(plugin_dir, manifest_file)
        require_apply(apply, host_stopped, expected_fingerprint, result["fingerprint"])
        if result["status"] == "already_restored": return result
        backup = ordinary_path(manifest_file).parent
        pending = stages(plugin, {INDEX: original}) if result["restore"] else {}
        try:
            if restore_preview(plugin, manifest_file)[0]["fingerprint"] != result["fingerprint"]:
                raise PatchError("Restore inputs changed")
            for name in [*result["restore"], *result["remove"]]:
                if metadata(plugin) != state["metadata"] or snapshot(plugin, (name,))[name] != state["current"][name]:
                    raise PatchError("Restore target changed")
                journal(backup, dict(action="restore_before", path=name))
                if name == INDEX:
                    os.replace(pending[name], plugin / name); pending.pop(name)
                else:
                    ordinary_path(plugin / name).unlink()
                journal(backup, dict(action="restore_after", path=name))
            if restore_preview(plugin, manifest_file)[0]["status"] != "already_restored":
                raise PatchError("Restore final verification failed")
            return dict(result, status="restored", applied=True)
        finally:
            for path in pending.values():
                if path.exists(): path.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", required=True)
    parser.add_argument("--restore-manifest")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--host-stopped", action="store_true")
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--backup-dir")
    args = parser.parse_args(argv)
    try:
        options = dict(apply=args.apply, host_stopped=args.host_stopped, expected_fingerprint=args.expected_fingerprint)
        result = restore(args.package_root, args.restore_manifest, **options) if args.restore_manifest else patch(args.package_root, backup_dir=args.backup_dir, **options)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception as error:
        print(type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__": sys.exit(main())
