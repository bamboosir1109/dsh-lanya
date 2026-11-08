"""Preview native memento 0.5.23 transactional history after visibility patch.

Only program files are read. Never opens a memory database or starts a host.
Apply/restore require an unchanged fingerprint and stopped-host declaration.
Restore history first, then use the older visibility patch's own restore.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import patch_memento_visibility as visibility
from patch_onebot_policy import ordinary_path, read_regular, sha256, write_new, PatchError

REPO = Path(__file__).resolve().parents[1]
PATCH_ID = "lanya-memento-native-history-v1"
TARGETS = ("lib/store.mjs", "lib/protocol.mjs")
MODULE = "lib/lanya-memento-history.mjs"
MARKER = "lib/lanya-memento-history.patch.json"
DEPENDENCIES = {
    "lib/constants.mjs": "bd8f653c8db33c324c776e575fb9e980b0f833f53b87d69207d005949bf19df8",
    "lib/errors.mjs": "1f164dcebec32f1635f582a2614ee4302dadf447d39fe7ea199d109b023e5836",
    "lib/match.mjs": "eb021cce9f777868106ae9d98a1b71a79802e29820ca88eee56fe1b5597dbfdb",
    "lib/budget.mjs": "9b0c17c1b49fb062f23c08fef4f9a3e6b4d205d372e0d2c057342877f0bc13de",
    "lib/gate.mjs": "7174cfbbbd75c9f324e4ebf4066ec103b7cbca467b42a9c0fa512a0a4f8e02a3",
    "lib/workspace.mjs": "debd2a84dc8852d9ed3c16a96d6f3ccb0f39d1b7568be3c5cea538001ebc33ab",
}


def exact(text, before, after):
    if text.count(before) != 1:
        raise PatchError("Unknown/ambiguous native history anchor")
    return text.replace(before, after, 1)


def method(text, name, change):
    start = text.index("    " + name + "(")
    end = text.index("\n    },", start) + len("\n    },")
    return text[:start] + change(text[start:end]) + text[end:]


def transform(name, raw):
    text = raw.decode("utf-8")
    if name == "lib/store.mjs":
        text = exact(text, "import { DatabaseSync } from 'node:sqlite'", "import { withNativeHistory, requireNativeTarget } from './lanya-memento-history.mjs'\nimport { DatabaseSync } from 'node:sqlite'")
        text = method(text, "insertEntry", lambda block: exact(block, "      return insertOne(db, input)",
            "      return withNativeHistory(db, 'add', () => insertOne(db, input), input.sessionId ?? null)"))
        text = method(text, "seedEntries", lambda block: exact(block,
            "      return withTransaction(db, () => inputs.map((input) => insertOne(db, input)))",
            "      return withNativeHistory(db, 'seed', () => inputs.map((input) => insertOne(db, input)), inputs[0]?.sessionId ?? null)"))
        def mutation(block, operation):
            block = exact(block, "      return withTransaction(db, () => {", "      return withNativeHistory(db, '" + operation + "', () => {")
            block = exact(block, "      })\n    },", "      }, input.sessionId ?? null)\n    },")
            if operation in ("replace", "remove"):
                block = exact(block, "matchEntries(db, track, scope, input.match)", "matchEntries(db, track, scope, input.match, input.expectedEntry ?? input)")
                anchor = "        const tags = input.tags === undefined ? target.tags : input.tags" if operation == "replace" else "        db.prepare('DELETE FROM entries WHERE id = ?').run(target.id)"
                block = exact(block, anchor, "        requireNativeTarget(target, input.expectedEntry)\n" + anchor)
            else:
                block = exact(block, "        for (const match of input.matches) {", "        for (const [index, match] of input.matches.entries()) {\n          const expected = input.expectedEntries?.[index]")
                block = exact(block, "matchEntries(db, track, scope, match)", "matchEntries(db, track, scope, match, expected ?? input)")
                block = exact(block, "          db.prepare('DELETE FROM entries WHERE id = ?').run(target.id)", "          requireNativeTarget(target, expected)\n          db.prepare('DELETE FROM entries WHERE id = ?').run(target.id)")
            return block
        for name, operation in (("replaceEntry", "replace"), ("removeEntry", "remove"), ("consolidateEntries", "consolidate")):
            text = method(text, name, lambda block, operation=operation: mutation(block, operation))
    elif name == "lib/protocol.mjs":
        text = exact(text,
            "    // 审批等待期间目标条目可能已被并发写改动：以此刻重新定位的 previous 为权威重算净变化，\n    // 再用此刻用量复审。复审与 replaceEntry 之间无 await，判断与实际写入之间不存在窗口。",
            "    // current 只用于原协议的预算复审，不重新批准目标。\n    // initial 的完整批准行由 Provider 在 BEGIN IMMEDIATE 内重验；并发变化会拒绝本次修改。")
        text = exact(text,
            "    // 审批等待期间目标可能被并发写改动：重新定位并以此刻为权威重算净变化。",
            "    // current 仅复算预算；事务内必须仍是 initial 的完整批准行，不能趁审批等待更换条目。")
        text = exact(text, "      track, scope, match: input.match, text,", "      track, scope, match: input.match, text, expectedEntry: initial,")
        text = exact(text, "    const removed = this.store.removeEntry({ track: input.track, scope: input.scope, match: input.match })",
            "    const removed = this.store.removeEntry({ track: input.track, scope: input.scope, match: input.match, expectedEntry: target, sessionId: write.agent.session?.id ?? null })")
        text = exact(text, "      track, scope, matches: input.matches, text,", "      track, scope, matches: input.matches, text, expectedEntries: initial,")
    else:
        raise PatchError("Unknown native history target")
    return ("// LANYA_MEMENTO_NATIVE_HISTORY_V1\n" + text).encode("utf-8")


def snapshot(plugin, names):
    result = {}
    for name in names:
        path = ordinary_path(plugin / name)
        if os.path.lexists(path):
            raw, signature = read_regular(path)
            result[name] = dict(sha256=sha256(raw), signature=signature)
        else: result[name] = None
    return result


def metadata(plugin):
    raw, signature = read_regular(plugin / "package.json")
    value = json.loads(raw)
    if value.get("name") != "dsh-memento" or value.get("version") != "0.5.23":
        raise PatchError("Only reviewed dsh-memento 0.5.23 is supported")
    result = snapshot(plugin, DEPENDENCIES)
    if any(row is None or row["sha256"] != DEPENDENCIES[name] for name, row in result.items()):
        raise PatchError("Native protocol dependency changed")
    return {"package": dict(sha256=sha256(raw), signature=signature), "dependencies": result}


def seal(value):
    return sha256(json.dumps(value, sort_keys=True).encode())


def reviewed_before(plugin):
    _, entries = visibility.inspect_package(plugin)
    if any(entry["state"] != "patched" for entry in entries):
        raise PatchError("Apply the reviewed visibility patch first; then preview native history")
    return {entry["path"]: entry["current"] for entry in entries}


def preview(plugin_dir):
    plugin = ordinary_path(plugin_dir)
    meta = metadata(plugin)
    current = snapshot(plugin, (*TARGETS, MODULE, MARKER))
    module = read_regular(REPO / "plugin/lib/memento-history.mjs")[0]
    if current[MARKER]:
        record = json.loads(read_regular(plugin / MARKER)[0])
        if record.get("patch") != PATCH_ID or record.get("plugin") != str(plugin) or record.get("state") != "patched":
            raise PatchError("Unknown native history marker")
        if set(record.get("after", {})) != set((*TARGETS, MODULE)) or any(not current[name] or current[name]["sha256"] != record["after"][name] for name in (*TARGETS, MODULE)):
            raise PatchError("Installed native history changed")
        if sha256(module) != record["after"][MODULE]:
            raise PatchError("Native history helper changed; explicit restore/migration required")
        originals = None
        outputs = {name: read_regular(plugin / name)[0] for name in (*TARGETS, MODULE)}
        status = "already_patched"
    else:
        if current[MODULE]: raise PatchError("Native history helper exists without marker")
        originals = reviewed_before(plugin)
        outputs = {name: transform(name, raw) for name, raw in originals.items()}
        outputs[MODULE] = module
        status = "patch_needed"
    after = {name: sha256(raw) for name, raw in outputs.items()}
    state = dict(plugin=str(plugin), metadata=meta, current=current, after=after)
    result = dict(status=status, applied=False, fingerprint=seal(state), after=after, requires_stopped_host=True)
    return result, plugin, originals, outputs, state


def stages(plugin, outputs):
    result = {}
    try:
        node = shutil.which("node")
        if not node: raise PatchError("Node is required")
        for name, raw in outputs.items():
            target = plugin / (".native-history-" + uuid.uuid4().hex + ".mjs")
            write_new(target, raw); result[name] = target
            if subprocess.run([node, "--check", str(target)], capture_output=True, timeout=30).returncode:
                raise PatchError("Native history JavaScript syntax failed")
        return result
    except Exception:
        for path in result.values(): path.unlink()
        raise


def patch(plugin_dir, *, apply=False, host_stopped=False, expected_fingerprint=None, backup_dir=None):
    options = dict(apply=apply, host_stopped=host_stopped, expected_fingerprint=expected_fingerprint, backup_dir=backup_dir)
    if not apply: return _patch(plugin_dir, **options)
    # Share the existing visibility patch's OS lock, including cross-process
    # exclusion. A persistent lock file is not deleted/reclaimed by PID/age.
    with visibility.package_lock(plugin_dir): return _patch(plugin_dir, **options)


def _patch(plugin_dir, *, apply=False, host_stopped=False, expected_fingerprint=None, backup_dir=None):
    result, plugin, originals, outputs, state = preview(plugin_dir)
    if not apply: return result
    if not host_stopped or expected_fingerprint != result["fingerprint"] or not backup_dir:
        raise PatchError("Apply needs stopped host, fresh fingerprint and private backup directory")
    if result["status"] == "already_patched": return result
    base = ordinary_path(backup_dir)
    if base == plugin or plugin in base.parents or base == REPO or REPO in base.parents:
        raise PatchError("Private originals must stay outside source/install")
    base.mkdir(parents=True, exist_ok=True)
    backup = base / (PATCH_ID + "-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex)
    (backup / "lib").mkdir(parents=True)
    for name, raw in originals.items(): write_new(backup / name, raw)
    record = dict(patch=PATCH_ID, state="prepared", plugin=str(plugin), backup=str(backup), metadata=state["metadata"],
                  before={name: sha256(raw) for name, raw in originals.items()}, after=result["after"])
    write_new(backup / "patch.json", (json.dumps(record, indent=2) + "\n").encode())
    pending = stages(plugin, outputs)
    changed = []
    try:
        fresh, *_ = preview(plugin)
        if fresh["fingerprint"] != result["fingerprint"]: raise PatchError("Native history inputs changed")
        for name, raw in originals.items():
            if read_regular(backup / name)[0] != raw: raise PatchError("Native history backup changed")
        write_new(plugin / MODULE, outputs[MODULE]); changed.append(MODULE)
        for name in TARGETS:
            if metadata(plugin) != state["metadata"] or snapshot(plugin, [name])[name] != state["current"][name]: raise PatchError("Native history source changed during apply")
            os.replace(pending[name], plugin / name); pending.pop(name); changed.append(name)
        write_new(plugin / MARKER, (json.dumps(dict(record, state="patched"), indent=2) + "\n").encode())
        return dict(result, status="patched", applied=True, backup=str(backup))
    except Exception:
        write_new(backup / "interrupted.json", (json.dumps(dict(patch=PATCH_ID, changed=changed)) + "\n").encode())
        raise
    finally:
        for path in pending.values():
            if os.path.lexists(path): path.unlink()


def restore_preview(plugin_dir, manifest_file):
    plugin, manifest = ordinary_path(plugin_dir), ordinary_path(manifest_file)
    if manifest.name != "patch.json" or plugin in manifest.parents or REPO in manifest.parents:
        raise PatchError("Private history patch.json required outside source/install")
    raw, signature = read_regular(manifest)
    record = json.loads(raw)
    if (not isinstance(record, dict) or set(record) != {"patch", "state", "plugin", "backup", "metadata", "before", "after"}
        or record["patch"] != PATCH_ID or record["state"] != "prepared" or record["plugin"] != str(plugin)
        or record["backup"] != str(manifest.parent) or set(record["before"]) != set(TARGETS) or set(record["after"]) != set((*TARGETS, MODULE))):
        raise PatchError("Native history manifest identity/layout mismatch")
    meta = metadata(plugin)
    if seal({"package": meta["package"]["sha256"], "deps": {n: v["sha256"] for n, v in meta["dependencies"].items()}}) != seal({"package": record["metadata"]["package"]["sha256"], "deps": {n: v["sha256"] for n, v in record["metadata"]["dependencies"].items()}}):
        raise PatchError("Native history package/dependencies changed")
    originals = {}
    for name in TARGETS:
        before = read_regular(manifest.parent / name)[0]
        baseline = visibility.transform(name, before, reverse=True)
        if sha256(baseline) != visibility.BASELINE_HASHES[name] or sha256(before) != record["before"][name] or sha256(transform(name, before)) != record["after"][name]:
            raise PatchError("Native history private original/hash mismatch")
        originals[name] = before
    current = snapshot(plugin, (*TARGETS, MODULE, MARKER))
    for name in TARGETS:
        if not current[name] or current[name]["sha256"] not in {record["before"][name], record["after"][name]}:
            raise PatchError("Unknown/later native history source; restore refused")
    if current[MODULE] and current[MODULE]["sha256"] != record["after"][MODULE]: raise PatchError("History helper changed")
    if current[MARKER] and read_regular(plugin / MARKER)[0] != (json.dumps(dict(record, state="patched"), indent=2) + "\n").encode():
        raise PatchError("History marker changed")
    state = dict(plugin=str(plugin), metadata=meta, manifest=dict(sha256=sha256(raw), signature=signature),
                 originals=snapshot(manifest.parent, TARGETS), current=current)
    restore_names = [name for name in TARGETS if current[name]["sha256"] != record["before"][name]]
    remove = [name for name in (MODULE, MARKER) if current[name]]
    return dict(status="restore_preview" if restore_names or remove else "already_restored", applied=False,
                fingerprint=seal(state), restore=restore_names, remove=remove, requires_stopped_host=True), plugin, originals, state


def restore(plugin_dir, manifest_file, *, apply=False, host_stopped=False, expected_fingerprint=None):
    options = dict(apply=apply, host_stopped=host_stopped, expected_fingerprint=expected_fingerprint)
    if not apply: return _restore(plugin_dir, manifest_file, **options)
    with visibility.package_lock(plugin_dir): return _restore(plugin_dir, manifest_file, **options)


def _restore(plugin_dir, manifest_file, *, apply=False, host_stopped=False, expected_fingerprint=None):
    result, plugin, originals, state = restore_preview(plugin_dir, manifest_file)
    if not apply: return result
    if not host_stopped or result["fingerprint"] != expected_fingerprint: raise PatchError("Restore needs stopped host and fresh fingerprint")
    if result["status"] == "already_restored": return result
    pending = stages(plugin, {name: originals[name] for name in result["restore"]})
    try:
        fresh, *_ = restore_preview(plugin, manifest_file)
        if fresh["fingerprint"] != result["fingerprint"]: raise PatchError("History restore inputs changed")
        for name in result["restore"]:
            if metadata(plugin) != state["metadata"] or snapshot(plugin, [name])[name] != state["current"][name]: raise PatchError("History restore target changed")
            os.replace(pending[name], plugin / name); pending.pop(name)
        for name in result["remove"]:
            if snapshot(plugin, [name])[name] != state["current"][name]: raise PatchError("History generated target changed")
            (plugin / name).unlink()
        return dict(result, status="restored", applied=True)
    finally:
        for path in pending.values():
            if os.path.lexists(path): path.unlink()


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
