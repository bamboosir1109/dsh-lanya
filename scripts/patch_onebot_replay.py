"""Isolate offline replay after the reviewed OneBot policy V1 patch.

Preview is read-only. Explicit apply/restore require a stopped-host declaration
and a fresh fingerprint. This incremental layer keeps the older V1 manifest
unchanged; restore this layer before restoring V1. No policy/inbox/database is
opened and no application, model or transport is started by this patcher.
"""
import argparse
from contextlib import contextmanager
import datetime
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import uuid

import patch_onebot_policy as policy
from patch_onebot_policy import PatchError, ordinary_path, read_regular, replace_once, sha256, write_new

REPO = Path(__file__).resolve().parents[1]
PATCH_ID = "lanya-onebot-replay-isolation-v1"
MARKER = "// LANYA_REPLAY_ISOLATION_V1\n"
INSTALLED_MARKER = "lib/lanya-replay-isolation.patch.json"
TARGETS = ("lib/bridge.js", "lib/replay.js", "lib/inbound.js")
KNOWN_REPLAY = {"73f0c8f0241c5943b578e772a4814a0c9b257881c1400a6ec87d00960e2d7cef"}


def sealed(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode())


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, indent=2) + "\n").encode()


def json_file(path):
    raw, signature = read_regular(path)
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        raise PatchError("Invalid patch metadata JSON") from None
    if not isinstance(value, dict):
        raise PatchError("Patch metadata must be an object")
    return value, dict(sha256=sha256(raw), signature=signature), raw


def private_manifest(value, plugin):
    path = ordinary_path(value)
    if path.name != "patch.json" or path == REPO or REPO in path.parents or plugin in path.parents:
        raise PatchError("An explicit private backup patch.json outside source/package is required")
    return path


def identity(path):
    raw, signature = read_regular(path)
    return dict(sha256=sha256(raw), signature=signature)


@contextmanager
def package_lock(plugin):
    """Persistent ordinary lock file + OS lock. Never reclaim by PID or age."""
    root = ordinary_path(plugin)
    path = ordinary_path(root / ".lanya-replay-isolation.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    locked = False
    try:
        info = os.fstat(fd)
        ordinary_path(path)
        current = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            raise PatchError("Package lock must be one ordinary unlinked file")
        if info.st_size == 0:
            os.write(fd, b"\0")
            os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise PatchError("Another replay patch operation owns this package") from None
        locked = True
        yield
    finally:
        if locked:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def transform(name, raw):
    text = raw.decode("utf-8")
    if MARKER.strip() in text:
        raise PatchError("Already layered/partial files require the private restore manifest")
    if name == "lib/bridge.js":
        text = replace_once(text, "import { OneBotDecisionAdapter } from './lanya-policy-adapter.js';",
                            "import { OneBotDecisionAdapter } from './lanya-policy-adapter.js';\nimport { isAbsolute, join } from 'node:path';")
        text = replace_once(text, "    constructor(deps) {", "    constructor(deps, diagnosticOptions) {")
        text = replace_once(text,
            "        this.lanyaPolicy = new OneBotDecisionAdapter(this, deps, { configFile: new URL('./lanya-policy.config.json', import.meta.url) });",
            """        // Trusted replay assembly only: never taken from events or model arguments.
        if (diagnosticOptions !== undefined && (diagnosticOptions.offlineReplay !== true ||
            typeof diagnosticOptions.root !== 'string' || !isAbsolute(diagnosticOptions.root))) {
            throw new Error('An explicit absolute offline replay root is required');
        }
        this.diagnosticReplay = diagnosticOptions?.offlineReplay === true;
        this.lanyaPolicy = new OneBotDecisionAdapter(this, deps, this.diagnosticReplay ? {
            stateFile: join(diagnosticOptions.root, 'decision-policy.json'),
            pumpFactory: () => ({ start: async () => false, stop() {}, drain: async () => ({ processed: 0, status: 'offline-disabled' }) }),
            memoryContext: async () => null,
            runtimeCapabilities: async () => null,
        } : { configFile: new URL('./lanya-policy.config.json', import.meta.url) });""")
        text = replace_once(text, "        this.inbound = new InboundPipeline({",
                            "        this.inbound = new InboundPipeline({\n            diagnosticReplay: this.diagnosticReplay,")
    elif name == "lib/inbound.js":
        anchor = "                    appendFileSync(CROSS_CHAT_LOG,"
        if anchor in text:
            text = replace_once(text, anchor, "                    if (!this.ctx.diagnosticReplay) appendFileSync(CROSS_CHAT_LOG,")
        elif "CROSS_CHAT_LOG" in text:
            raise PatchError("Unknown local cross-log boundary")
    elif name == "lib/replay.js":
        text = replace_once(text, "import { readFile, stat, writeFile } from 'node:fs/promises';",
                            "import { readFile, stat, writeFile, mkdtemp, rm, mkdir, lstat } from 'node:fs/promises';\nimport { tmpdir } from 'node:os';")
        text = replace_once(text, "import { basename, join } from 'node:path';", "import { basename, join, resolve, sep } from 'node:path';")
        text = replace_once(text, "export async function replayInbox(options) {", """export async function replayInbox(options) {
    // Caller-selected recording input/report output keep their original API.
    // All implicit policy, inbox, mapping and media state belongs to this fresh root.
    const parent = resolve(tmpdir());
    const root = await mkdtemp(join(parent, 'dsh-onebot-replay-'));
    try {
        await mkdir(join(root, 'trace'));
        return await replayInboxIsolated({ ...options, traceDir: join(root, 'trace'), mediaDir: join(root, 'media') }, root);
    } finally {
        const absolute = resolve(root);
        const info = await lstat(absolute);
        if (!absolute.startsWith(parent + sep) || !basename(absolute).startsWith('dsh-onebot-replay-') || !info.isDirectory() || info.isSymbolicLink()) {
            throw new Error('Refusing cleanup outside the owned ordinary replay directory');
        }
        await rm(absolute, { recursive: true, force: false });
    }
}
async function replayInboxIsolated(options, diagnosticRoot) {""")
        text = replace_once(text, "session: { id: sessionId, seq: 0, header: { cwd: process.cwd() } },",
                            "session: { id: sessionId, seq: 0, header: { cwd: opts.meta.cwd, agentPreset: opts.meta.agentPreset } },")
        text = replace_once(text, "        media: new MediaStore(join(bridgeDir, 'media'), 6),", """        media: {
            cleanupExpired: async () => {},
            ensure: async () => { throw new Error('Offline replay media writes disabled'); },
            freshPath: () => { throw new Error('Offline replay media writes disabled'); },
            resolve: async () => { throw new Error('Offline replay media reads/downloads disabled'); },
            downloadUrl: async () => { throw new Error('Offline replay network disabled'); },
        },""")
        text = replace_once(text, "        agentPresets: undefined,", "        agentPresets: { defaultId: 'offline-replay', resolve: async () => ({ id: 'offline-replay' }) },")
        text = replace_once(text, "            agentPreset: 'standard',", "            agentPreset: 'offline-replay',")
        text = replace_once(text, "        log: (level, message) => log(level, message),\n    });\n    bridge.start();",
                            "        log: (level, message) => log(level, message),\n    }, { offlineReplay: true, root: diagnosticRoot });\n    bridge.start();")
        text = replace_once(text, "    try {\n        for (const { event } of batch) {", "    try {\n        await bridge.registry.mappingLoaded;\n        for (const { event } of batch) {")
        text = replace_once(text, "        interceptedOutbound: intercepted,", "        interceptedOutbound: intercepted,\n        recordingAgentFollowups: followups.length,\n        isolated: true,\n        modelCalled: false,")
    else:
        raise PatchError("Unknown replay isolation target")
    return (MARKER + text).encode("utf-8")


def canonical_modules():
    result = {}
    for target, source in (("lanya-decision-policy.js", "decision-policy.js"),
                           ("lanya-policy-adapter.js", "onebot-policy-adapter.js"),
                           ("lanya-observation-queue.js", "observation-queue.js")):
        raw, _ = read_regular(REPO / "plugin/lib" / source)
        if source == "onebot-policy-adapter.js":
            text = replace_once(raw.decode(), "from './decision-policy.js'", "from './lanya-decision-policy.js'")
            raw = replace_once(text, "from './observation-queue.js'", "from './lanya-observation-queue.js'").encode()
        result[target] = sha256(raw)
    return result


def read_base(plugin, manifest):
    manifest = private_manifest(manifest, plugin)
    record, manifest_meta, _ = json_file(manifest)
    required = {"patch", "plugin", "before", "after", "backup", "state", "package_sha256"}
    if (set(record) != required or record.get("patch") != policy.PATCH_ID or record.get("state") != "prepared" or
            ordinary_path(record.get("plugin", "")) != plugin or ordinary_path(record.get("backup", "")) != manifest.parent or
            not isinstance(record.get("before"), dict) or set(record["before"]) != set(policy.TARGETS) or
            not isinstance(record.get("after"), dict) or set(record["after"]) != set(policy.TARGETS + policy.GENERATED)):
        raise PatchError("Unknown/mismatched existing policy V1 manifest")
    package, package_meta, _ = json_file(plugin / "package.json")
    if (package.get("name"), package.get("version"), package.get("type")) != ("dsh-onebot-qq", "0.8.1", "module") or package_meta["sha256"] != record["package_sha256"]:
        raise PatchError("Only the unchanged reviewed OneBot 0.8.1 package is supported")
    watched = {str(plugin / "package.json"): package_meta, str(manifest): manifest_meta}
    for name in policy.TARGETS:
        original, signature = read_regular(manifest.parent / name)
        if sha256(original) != record["before"][name] or sha256(original) not in policy.KNOWN[name] or sha256(policy.transform(name, original)) != record["after"][name]:
            raise PatchError("Existing V1 original/transform hash mismatch: " + name)
        watched[str(manifest.parent / name)] = dict(sha256=sha256(original), signature=signature)
    marker_path = plugin / "lib/lanya-policy.patch.json"
    marker, marker_meta, marker_raw = json_file(marker_path)
    expected_marker = (json.dumps(dict(record, state="patched"), indent=2) + "\n").encode()
    if marker != dict(record, state="patched") or marker_raw != expected_marker:
        raise PatchError("Installed V1 marker no longer matches the private manifest")
    watched[str(marker_path)] = marker_meta
    installed = {}
    for name in policy.TARGETS + policy.GENERATED:
        raw, signature = read_regular(plugin / "lib" / name)
        installed["lib/" + name] = (raw, signature)
        if name not in ("bridge.js", "inbound.js") and sha256(raw) != record["after"][name]:
            raise PatchError("Existing V1 output has changed: " + name)
        watched[str(plugin / "lib" / name)] = dict(sha256=sha256(raw), signature=signature)
    canonical = canonical_modules()
    if any(record["after"][name] != digest for name, digest in canonical.items()):
        raise PatchError("Existing V1 adapter/core contract differs from this reviewed source")
    parent = dict(patch=policy.PATCH_ID, manifest=str(manifest), manifest_sha256=manifest_meta["sha256"],
                  installed_marker_sha256=sha256(marker_raw))
    return record, parent, watched, installed


def validate_record(plugin, path, base, parent):
    path = private_manifest(path, plugin)
    record, meta, _ = json_file(path)
    required = {"version", "patch", "plugin", "before", "after", "backup", "state", "package_sha256", "parent"}
    if (set(record) != required or record.get("version") != 1 or record.get("patch") != PATCH_ID or record.get("state") != "prepared" or
            ordinary_path(record.get("plugin", "")) != plugin or ordinary_path(record.get("backup", "")) != path.parent or
            record.get("package_sha256") != base["package_sha256"] or record.get("parent") != parent or
            not isinstance(record.get("before"), dict) or set(record["before"]) != set(TARGETS) or
            not isinstance(record.get("after"), dict) or set(record["after"]) != set(TARGETS)):
        raise PatchError("Unknown/mismatched replay layer manifest")
    originals, outputs, watched = {}, {}, {str(path): meta}
    for name in TARGETS:
        raw, signature = read_regular(path.parent / name)
        wanted = base["after"][Path(name).name] if name != "lib/replay.js" else None
        if sha256(raw) != record["before"][name] or (wanted is not None and sha256(raw) != wanted) or (wanted is None and sha256(raw) not in KNOWN_REPLAY):
            raise PatchError("Replay private original hash mismatch: " + name)
        output = transform(name, raw)
        if sha256(output) != record["after"][name]:
            raise PatchError("Replay output hash/anchor mismatch: " + name)
        originals[name], outputs[name] = (raw, signature), output
        watched[str(path.parent / name)] = dict(sha256=sha256(raw), signature=signature)
    return record, originals, outputs, watched


def inspect(plugin_dir, policy_manifest, *, restore_manifest=None):
    plugin = ordinary_path(plugin_dir)
    base, parent, watched, installed = read_base(plugin, policy_manifest)
    replay_raw, replay_sig = read_regular(plugin / "lib/replay.js")
    installed["lib/replay.js"] = replay_raw, replay_sig
    watched[str(plugin / "lib/replay.js")] = dict(sha256=sha256(replay_raw), signature=replay_sig)
    marker_path = ordinary_path(plugin / INSTALLED_MARKER)
    marker_raw = None
    if os.path.lexists(marker_path):
        marker, marker_meta, marker_raw = json_file(marker_path)
        watched[str(marker_path)] = marker_meta
        if restore_manifest is None:
            restore_manifest = ordinary_path(marker.get("backup", "")) / "patch.json"
    else:
        watched[str(marker_path)] = None
    record = None
    if restore_manifest is not None:
        record, originals, outputs, extra = validate_record(plugin, restore_manifest, base, parent)
        watched.update(extra)
        if marker_raw is not None and marker_raw != encoded(dict(record, state="patched")):
            raise PatchError("Installed replay marker differs from its private manifest")
        for name in TARGETS:
            if sha256(installed[name][0]) not in (record["before"][name], record["after"][name]):
                raise PatchError("Later/unknown file modification; refusing replay restore: " + name)
        status = "already_patched" if all(sha256(installed[name][0]) == record["after"][name] for name in TARGETS) and marker_raw is not None else "partial_layer"
    else:
        for name in ("lib/bridge.js", "lib/inbound.js"):
            if sha256(installed[name][0]) != base["after"][Path(name).name]:
                raise PatchError("Existing V1 target changed; use the replay private restore manifest if interrupted")
        if sha256(replay_raw) not in KNOWN_REPLAY:
            raise PatchError("Unknown OneBot replay source identity")
        originals = {name: installed[name] for name in TARGETS}
        outputs = {name: transform(name, raw) for name, (raw, _) in originals.items()}
        status = "patch_needed"
    before = {name: sha256(raw) for name, (raw, _) in originals.items()}
    after = {name: sha256(raw) for name, raw in outputs.items()}
    snapshot = dict(plugin=str(plugin), parent=parent, watched=watched, before=before, after=after)
    result = dict(patch=PATCH_ID, status=status, applied=False, requires_stopped_host=True,
                  fingerprint=sealed(snapshot), before=before, after=after, parent=parent)
    return result, plugin, originals, outputs, snapshot, record


def syntax(outputs):
    node = shutil.which("node")
    if not node:
        raise PatchError("Node is required for syntax checks")
    for name, raw in outputs.items():
        result = subprocess.run([node, "--input-type=module", "--check"], input=raw, capture_output=True, timeout=30)
        if result.returncode:
            raise PatchError("Replay JavaScript syntax check failed: " + name)


def patch(plugin_dir, policy_manifest, *, apply=False, host_stopped=False, expected_fingerprint=None, backup_dir=None):
    if not apply:
        result, _, _, outputs, _, _ = inspect(plugin_dir, policy_manifest)
        if result["status"] == "partial_layer":
            raise PatchError("Interrupted replay layer requires explicit restore preview")
        syntax(outputs)
        return result
    if not host_stopped or not backup_dir:
        raise PatchError("Apply requires --host-stopped, --backup-dir and a fresh --expected-fingerprint")
    with package_lock(plugin_dir):
        result, plugin, originals, outputs, snapshot, _ = inspect(plugin_dir, policy_manifest)
        if result["status"] == "partial_layer" or result["fingerprint"] != expected_fingerprint:
            raise PatchError("Apply requires the unchanged replay preview fingerprint")
        if result["status"] == "already_patched":
            return result
        syntax(outputs)
        base = ordinary_path(backup_dir)
        if base == REPO or REPO in base.parents or base == plugin or plugin in base.parents:
            raise PatchError("Private backups must be outside source and installed package")
        base.mkdir(parents=True, exist_ok=True)
        backup = ordinary_path(base / (PATCH_ID + "-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex))
        backup.mkdir()
        (backup / "lib").mkdir()
        for name, (raw, _) in originals.items():
            write_new(backup / name, raw)
        package_hash = snapshot["watched"][str(plugin / "package.json")]["sha256"]
        record = dict(version=1, patch=PATCH_ID, plugin=str(plugin), before=result["before"], after=result["after"],
                      backup=str(backup), state="prepared", package_sha256=package_hash, parent=result["parent"])
        manifest = backup / "patch.json"
        write_new(manifest, encoded(record))
        stages, replaced = {}, []
        try:
            for name, raw in outputs.items():
                stage = plugin / "lib" / (".lanya-replay-stage-" + uuid.uuid4().hex + ".js")
                write_new(stage, raw)
                stages[name] = stage
            fresh = inspect(plugin, policy_manifest)[0]
            if fresh["fingerprint"] != result["fingerprint"]:
                raise PatchError("Replay inputs changed before first replacement")
            for name in TARGETS:
                # Recheck all parent evidence, backups and partial states each time.
                inspect(plugin, policy_manifest, restore_manifest=manifest)
                if identity(plugin / name) != snapshot["watched"][str(plugin / name)]:
                    raise PatchError("Replay target identity changed before replacement: " + name)
                if read_regular(stages[name])[0] != outputs[name]:
                    raise PatchError("Replay staging hash changed")
                os.replace(stages[name], plugin / name)  # New inode severs HardLinks.
                stages.pop(name)
                replaced.append(name)
            write_new(plugin / INSTALLED_MARKER, encoded(dict(record, state="patched")))
            readback = inspect(plugin, policy_manifest)
            if readback[0]["status"] != "already_patched" or any(
                    readback[4]["watched"][str(plugin / name)]["sha256"] != result["after"][name] for name in TARGETS):
                raise PatchError("Final replay apply readback did not match the complete layer")
            return dict(result, status="patched", state="patched", applied=True, backup=str(backup), manifest=str(manifest))
        except Exception:
            write_new(backup / "interrupted.json", encoded(dict(patch=PATCH_ID, replaced=replaced)))
            raise
        finally:
            for stage in stages.values():
                if os.path.lexists(stage):
                    ordinary_path(stage).unlink()


def restore(plugin_dir, restore_manifest, *, apply=False, host_stopped=False, expected_fingerprint=None):
    plugin = ordinary_path(plugin_dir)
    manifest = private_manifest(restore_manifest, plugin)
    record, _, _ = json_file(manifest)
    parent = record.get("parent")
    if not isinstance(parent, dict) or not isinstance(parent.get("manifest"), str):
        raise PatchError("Replay restore is missing the exact policy V1 parent")

    def preview():
        result, root, originals, _, snapshot, layer = inspect(plugin, parent["manifest"], restore_manifest=manifest)
        if layer is None:
            raise PatchError("Missing replay layer")
        names = [name for name in TARGETS if snapshot["watched"][str(root / name)]["sha256"] != layer["before"][name]]
        marker = snapshot["watched"][str(root / INSTALLED_MARKER)]
        return dict(result, status="restore_preview" if names or marker else "already_restored", restore=names,
                    remove=[INSTALLED_MARKER] if marker else []), root, originals, snapshot

    if not apply:
        return preview()[0]
    if not host_stopped:
        raise PatchError("Restore apply requires --host-stopped and its fresh preview fingerprint")
    with package_lock(plugin):
        result, root, originals, snapshot = preview()
        if expected_fingerprint != result["fingerprint"]:
            raise PatchError("Restore requires the unchanged replay preview fingerprint")
        stages = {}
        try:
            for name in result["restore"]:
                stage = root / "lib" / (".lanya-replay-restore-" + uuid.uuid4().hex + ".js")
                write_new(stage, originals[name][0])
                stages[name] = stage
            if preview()[0]["fingerprint"] != result["fingerprint"]:
                raise PatchError("Replay restore inputs changed before replacement")
            for name in result["restore"]:
                inspect(root, parent["manifest"], restore_manifest=manifest)
                if identity(root / name) != snapshot["watched"][str(root / name)] or read_regular(stages[name])[0] != originals[name][0]:
                    raise PatchError("Replay restore target or staged original changed")
                os.replace(stages[name], root / name)
                stages.pop(name)
            if result["remove"]:
                target = root / INSTALLED_MARKER
                if identity(target) != snapshot["watched"][str(target)]:
                    raise PatchError("Installed replay marker changed during restore")
                ordinary_path(target).unlink()
            readback = preview()[0]
            if readback["status"] != "already_restored" or readback["restore"] or readback["remove"]:
                raise PatchError("Final replay restore readback did not exactly match all originals")
            return dict(result, status="restored", applied=True)
        finally:
            for stage in stages.values():
                if os.path.lexists(stage):
                    ordinary_path(stage).unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-dir", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--policy-manifest", help="Existing private policy V1 backup patch.json")
    mode.add_argument("--restore-manifest", help="This incremental layer's private patch.json")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--host-stopped", action="store_true")
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--backup-dir")
    args = parser.parse_args(argv)
    try:
        if args.restore_manifest:
            result = restore(args.plugin_dir, args.restore_manifest, apply=args.apply, host_stopped=args.host_stopped, expected_fingerprint=args.expected_fingerprint)
        else:
            result = patch(args.plugin_dir, args.policy_manifest, apply=args.apply, host_stopped=args.host_stopped,
                           expected_fingerprint=args.expected_fingerprint, backup_dir=args.backup_dir)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (PatchError, OSError, ValueError, KeyError, TypeError) as error:
        # Do not print original code, JSON configuration, credentials or DB data.
        print(json.dumps({"error": str(error) if isinstance(error, PatchError) else type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
