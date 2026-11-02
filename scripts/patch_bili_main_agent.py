"""Reviewed dsh-bili-scroll 2.2.0 main-agent primitives; preview by default.

Never calls a platform/model or discovers a default installation. Private
originals stay outside source/install. Apply/restore require a stopped-host
declaration and a fresh fingerprint; this declaration does not stop the host.
Multi-file application is recoverable, not atomic. Unknown/later edits refuse
the entire restore preflight. Ledger/config data are never removed by restore.
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

from patch_onebot_policy import PatchError, ordinary_path, read_regular, sha256, write_new

REPO = Path(__file__).resolve().parents[1]
PATCH_ID = "lanya-bili-main-agent-v2"
LEGACY_ID = "lanya-bili-main-agent-v1"
TARGET = "index.js"
MODULE = "lib/lanya-bili-main-agent.js"
CONFIG = "lib/lanya-bili-main.config.json"
MARKER = "lib/lanya-bili-main.patch.json"
OUTPUTS = (TARGET, MODULE, CONFIG)
# Reviewed production and synthetic-fixture identities; no installed source is
# vendored. All external changes use exact anchors after this full hash check.
KNOWN = {"d8629438c95dadc7e70f57077a205f568f22488e89da3d30eacb1b68b873794a",
         "cdb94ea4a50b0e7044f7e9f9f75fc16c7d12c41da40d7b80e5b864eb22233cee"}
MCP_KNOWN = {"e2c843d6895845247fe2fb9eac072f854ec9a7560124d3c5db38714e164afc75",
             "f49272f532c5da37ad75cf922e6ba55bbb180accb23ca4161797c0d17e85c6cb"}


def replace(text, before, after):
    if text.count(before) != 1:
        raise PatchError("Unknown/ambiguous Bili patch anchor")
    return text.replace(before, after, 1)


def transform_v1(raw):
    text = raw.decode("utf-8").replace("\r\n", "\n")
    text = replace(text, "import { loadHostDeps } from './lib/hostdeps.mjs'",
                   "import { createBiliMainAgent } from './lib/lanya-bili-main-agent.js'\nimport { loadHostDeps } from './lib/hostdeps.mjs'")
    anchor = """  const tool = (spec) => ctx.tools.register(defineTool({
    ...spec,
    output: spec.output || textOutput,
    async execute(args, exec) { return safe(await spec.execute(args, exec)) },
  }))"""
    text = replace(text, anchor, anchor + "\n\n  const lanyaBiliMain = createBiliMainAgent({ call, configFile: new URL('./lib/lanya-bili-main.config.json', import.meta.url) })\n  for (const spec of lanyaBiliMain.tools()) tool(spec)")
    text = replace(text,
                   "    description: '**立刻跑一轮**（不等窗口）：起一个 headless 会话，把 duty.md 交给它去刷、写报告。'\n      + '适合\"我现在就想看一份\"的场合。',",
                   "    description: 'Read recommendations directly in this same agent, then use direct Bili tools or idle. A separate headless round is available only in explicit legacy mode.',")
    text = replace(text, "    async execute() { const r = runDuty('manual'); return { ok: true, ...r } },",
                   "    async execute(args, exec) { return lanyaBiliMain.run(args, exec, () => { const r = runDuty('manual'); return { ok: true, ...r } }) },")
    text = replace(text, "  const runDuty = (why = 'window') => {",
                   "  const runDuty = (why = 'window') => {\n    if (lanyaBiliMain.mode() !== 'legacy') return { started: false, mode: 'unified', reason: 'main_agent_only' }")
    text = replace(text, "  const tick = () => {\n    try {",
                   "  const tick = () => {\n    try {\n      if (lanyaBiliMain.mode() !== 'legacy') return")
    text = replace(text, "  function runDmReply(fromUid, text) {\n    try {",
                   "  function runDmReply(fromUid, text) {\n    try {\n      if (lanyaBiliMain.mode() !== 'legacy') return { ok: false, reason: 'main_agent_only' }")
    text = replace(text, "  async function dmTick() {\n    if (dmBusy) return",
                   "  async function dmTick() {\n    if (lanyaBiliMain.mode() !== 'legacy') return\n    if (dmBusy) return")
    return ("// LANYA_BILI_MAIN_AGENT_V1\n" + text).encode("utf-8")


def transform_registered(raw):
    """Replace only the reviewed V1 factory; retain unrelated installed layers."""
    text = raw.decode("utf-8").replace("\r\n", "\n")
    anchor = """  const tool = (spec) => ctx.tools.register(defineTool({
    ...spec,
    output: spec.output || textOutput,
    async execute(args, exec) { return safe(await spec.execute(args, exec)) },
  }))"""
    replacement = """  const lanyaBiliReplacements = new Set()
  const tool = (spec) => {
    const replacement = lanyaBiliMain.replacement(spec.name)
    if (replacement && lanyaBiliReplacements.has(spec.name)) return () => {}
    const selected = replacement || spec
    const disposer = ctx.tools.register(defineTool({
      ...selected,
      output: selected.output || textOutput,
      async execute(args, exec) { return safe(await selected.execute(args, exec)) },
    }))
    if (replacement) lanyaBiliReplacements.add(spec.name)
    return disposer
  }"""
    text = replace(text, anchor, replacement)
    # The real upstream host registers frames separately. Keep its actual image
    # attachment renderer, while forwarding/validating the selected part. The
    # reviewed synthetic V1 fixture intentionally has no image host services.
    if "    name: 'bili_frames'," in text:
        text = replace(text, """      bvid: { type: 'string', description: 'BV号' },
      num: { type: 'number', description: '取几帧，1-6，默认 3' },""", """      bvid: { type: 'string', description: 'BV号' },
      page: { type: 'number', description: '真实分P页号，从1开始，不回落到其他分P' },
      num: { type: 'number', description: '取几帧，1-6，默认 3' },""")
        text = replace(text, """      const at = Array.isArray(args.at) ? args.at.map(Number).filter(Number.isFinite) : undefined
      const { json, images } = await callRaw('bili_frames', {
        bvid: String(args.bvid ?? ''),""", """      const at = Array.isArray(args.at) ? args.at.map(Number).filter(Number.isFinite) : undefined
      const page = args.page ?? 1
      if (!Number.isSafeInteger(page) || page < 1 || page > 1000) throw new Error('Invalid video part')
      const { json, images } = await callRaw('bili_frames', {
        bvid: String(args.bvid ?? ''), page,""")
        text = replace(text, """      if (!images.length) return { ...meta, ok: false, error: 'MCP 没返回图片' }

      const a = await attachImages(exec, images)""", """      if (!images.length) return { ...meta, ok: false, error: 'MCP 没返回图片' }
      if (meta.bvid !== String(args.bvid ?? '') || meta.page !== page || !/^[1-9][0-9]*$/.test(String(meta.cid)) || meta.source !== 'storyboard') {
        return { ok: false, reason: 'frame_part_evidence_mismatch', image_attached: false }
      }

      const a = await attachImages(exec, images)""")
    text = replace(text, "// LANYA_BILI_MAIN_AGENT_V1", "// LANYA_BILI_MAIN_AGENT_V2")
    return text.encode("utf-8")


def transform(raw):
    return transform_registered(transform_v1(raw))


# V1 public helper and exact reviewed native/synthetic V1 index identities.
# A marker alone never authorizes replacing unknown local program edits.
V1_MODULE = "353297a8941d914051a9d2833bbd1985039c8eb802c082b5a7205c5b3b664d50"
V1_INDEX = {"7bb559221dfcf1b3d612e7c8477f73dc43ac542bdbb72cd0ea97d773564b17d4",
            "c5059afef0bcbf1b5a1f83f9fea2f3903b324aea91df03aef7085bee9f3d0aea"}


def package_snapshot(plugin):
    raw, signature = read_regular(plugin / "package.json")
    info = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(info, dict) or (info.get("name"), info.get("version"), info.get("type"), info.get("main")) != ("dsh-bili-scroll", "2.2.0", "module", "index.js"):
        raise PatchError("Only reviewed dsh-bili-scroll 2.2.0 ESM is supported")
    dependency, dep_signature = read_regular(plugin / "lib/mcp.mjs")
    if sha256(dependency) not in MCP_KNOWN:
        raise PatchError("Unknown reviewed direct MCP client")
    return {"package": {"sha256": sha256(raw), "signature": signature},
            "mcp": {"sha256": sha256(dependency), "signature": dep_signature}}


def private_path(value, plugin, suffix):
    if not os.path.isabs(os.fspath(value)):
        raise PatchError("An explicit absolute private path is required")
    result = ordinary_path(value)
    if result == plugin or plugin in result.parents or result == REPO or REPO in result.parents or result.suffix != suffix:
        raise PatchError("Private configuration/ledger must be outside source and installed packages")
    if result.exists() and not result.is_file():
        raise PatchError("Private configuration/ledger must be ordinary files")
    return result


def inventory(plugin, names):
    result = {}
    for name in names:
        target = ordinary_path(plugin / name)
        if os.path.lexists(target):
            raw, signature = read_regular(target)
            result[name] = {"sha256": sha256(raw), "signature": signature}
        else:
            result[name] = None
    return result


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True).encode())


def preview(plugin_dir, project_config, ledger_file):
    plugin = ordinary_path(plugin_dir)
    metadata = package_snapshot(plugin)
    project = private_path(project_config, plugin, ".json")
    ledger = private_path(ledger_file, plugin, ".sqlite")
    # The patch records paths only; it never copies configuration or opens a
    # real ledger. Runtime reads the shared project mode afresh for every call.
    raw, _ = read_regular(REPO / "plugin/lib/bili-main-agent.js")
    generated = {MODULE: raw, CONFIG: (json.dumps({"projectConfig": str(project), "ledgerFile": str(ledger)}, indent=2) + "\n").encode()}
    original, _ = read_regular(plugin / TARGET)
    current = inventory(plugin, (*OUTPUTS, MARKER))
    record = None
    if current[MARKER]:
        record = json.loads(read_regular(plugin / MARKER)[0])
        if (not isinstance(record, dict) or set(record) != {"patch", "state", "plugin", "backup", "metadata", "before", "after"}
                or record.get("patch") not in (PATCH_ID, LEGACY_ID) or record.get("plugin") != str(plugin) or record.get("state") != "patched"):
            raise PatchError("Unknown existing Bili patch marker")
        if set(record.get("after", {})) != set(OUTPUTS) or any(current[name] is None or current[name]["sha256"] != record["after"][name] for name in OUTPUTS):
            raise PatchError("Existing Bili module/config changed")
        if record["patch"] == LEGACY_ID:
            if sha256(original) not in V1_INDEX or current[MODULE]["sha256"] != V1_MODULE or read_regular(plugin / CONFIG)[0] != generated[CONFIG]:
                raise PatchError("Unknown V1 Bili upgrade input")
            wanted = {TARGET: transform_registered(original), **generated}
            status = "upgrade_needed"
        else:
            wanted = {TARGET: original, **generated}
            if record.get("after") != {name: sha256(data) for name, data in wanted.items()}:
                raise PatchError("Existing Bili patch changed; explicit restore/migration required")
            status = "already_patched"
    else:
        if sha256(original) not in KNOWN:
            raise PatchError("Unknown reviewed Bili index identity")
        if current[MODULE] or current[CONFIG]:
            raise PatchError("Owned Bili outputs exist without a matching marker")
        wanted = {TARGET: transform(original), **generated}
        status = "patch_needed"
    after = {name: sha256(data) for name, data in wanted.items()}
    snapshot = {"plugin": str(plugin), "metadata": metadata, "current": current, "after": after}
    before = {name: current[name]["sha256"] for name in (*OUTPUTS, MARKER)} if status == "upgrade_needed" else {TARGET: sha256(original)}
    result = {"status": status, "applied": False, "requires_stopped_host": True,
              "fingerprint": fingerprint(snapshot), "before": before, "after": after}
    return result, plugin, wanted, original, snapshot


def stage(plugin, outputs):
    stages = {}
    try:
        node = shutil.which("node")
        if not node:
            raise PatchError("Node is required for syntax validation")
        for name, raw in outputs.items():
            target = plugin / (".lanya-bili-stage-" + uuid.uuid4().hex + (".js" if name.endswith(".js") else ".json"))
            write_new(target, raw)
            stages[name] = target
            if name.endswith(".js") and subprocess.run([node, "--check", str(target)], capture_output=True, timeout=30).returncode:
                raise PatchError("Bili JavaScript syntax check failed")
        return stages
    except Exception:
        for target in stages.values(): target.unlink()
        raise


def patch(plugin_dir, *, project_config, ledger_file, apply=False, host_stopped=False, expected_fingerprint=None, backup_dir=None):
    result, plugin, outputs, original, snapshot = preview(plugin_dir, project_config, ledger_file)
    if not apply:
        return result
    if not host_stopped or not backup_dir or expected_fingerprint != result["fingerprint"]:
        raise PatchError("Apply needs --host-stopped, --backup-dir and unchanged --expected-fingerprint")
    if result["status"] == "already_patched":
        return result
    base = ordinary_path(backup_dir)
    if base == plugin or plugin in base.parents or base == REPO or REPO in base.parents:
        raise PatchError("Private originals must stay outside source/installed packages")
    base.mkdir(parents=True, exist_ok=True)
    backup = base / (PATCH_ID + "-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex)
    backup.mkdir()
    upgrading = result["status"] == "upgrade_needed"
    for name in result["before"]:
        (backup / name).parent.mkdir(parents=True, exist_ok=True)
        write_new(backup / name, read_regular(plugin / name)[0])
    record = {"patch": PATCH_ID, "state": "prepared", "plugin": str(plugin), "backup": str(backup),
              "metadata": snapshot["metadata"], "before": result["before"], "after": result["after"]}
    write_new(backup / "patch.json", (json.dumps(record, indent=2) + "\n").encode())
    stages = stage(plugin, outputs)
    changed = []
    try:
        fresh, *_ = preview(plugin, project_config, ledger_file)
        if fresh["fingerprint"] != result["fingerprint"] or any(sha256(read_regular(backup / name)[0]) != expected for name, expected in result["before"].items()):
            raise PatchError("Bili inputs changed before application")
        for name in (MODULE, CONFIG):
            if upgrading:
                if inventory(plugin, [name])[name] != snapshot["current"][name]:
                    raise PatchError("Bili upgrade output changed during application")
                os.replace(stages.pop(name), ordinary_path(plugin / name))
            else:
                write_new(ordinary_path(plugin / name), outputs[name])
            changed.append(name)
        if inventory(plugin, [TARGET])[TARGET] != snapshot["current"][TARGET] or package_snapshot(plugin) != snapshot["metadata"]:
            raise PatchError("Bili index/package changed during application")
        os.replace(stages[TARGET], plugin / TARGET)  # New inode severs hardlinks.
        stages.pop(TARGET)
        changed.append(TARGET)
        marker_bytes = (json.dumps(dict(record, state="patched"), indent=2) + "\n").encode()
        if upgrading:
            if inventory(plugin, [MARKER])[MARKER] != snapshot["current"][MARKER]:
                raise PatchError("Bili upgrade marker changed")
            marker_stage = stage(plugin, {MARKER: marker_bytes})[MARKER]
            stages[MARKER] = marker_stage
            os.replace(marker_stage, plugin / MARKER)
            stages.pop(MARKER)
        else:
            write_new(plugin / MARKER, marker_bytes)
        return dict(result, status="patched", applied=True, backup=str(backup))
    except Exception:
        write_new(backup / "interrupted.json", (json.dumps({"patch": PATCH_ID, "changed": changed}) + "\n").encode())
        raise
    finally:
        for target in stages.values():
            if os.path.lexists(target): target.unlink()


def restore_preview(plugin_dir, manifest_file):
    plugin = ordinary_path(plugin_dir)
    manifest = ordinary_path(manifest_file)
    if manifest.name != "patch.json" or plugin in manifest.parents or REPO in manifest.parents:
        raise PatchError("Restore requires private backup patch.json outside source/install")
    raw, signature = read_regular(manifest)
    record = json.loads(raw)
    if (not isinstance(record, dict) or set(record) != {"patch", "state", "plugin", "backup", "metadata", "before", "after"}
            or record["patch"] not in (PATCH_ID, LEGACY_ID) or record["state"] != "prepared" or record["plugin"] != str(plugin) or record["backup"] != str(manifest.parent)
            or set(record["before"]) not in ({TARGET}, {*OUTPUTS, MARKER}) or set(record["after"]) != set(OUTPUTS)):
        raise PatchError("Unknown Bili restore manifest layout/path")
    if set(record["before"]) == {*OUTPUTS, MARKER}:
        return upgrade_restore_preview(plugin, manifest, raw, signature, record)
    metadata = package_snapshot(plugin)
    if {key: value["sha256"] for key, value in metadata.items()} != {key: value["sha256"] for key, value in record["metadata"].items()}:
        raise PatchError("Bili package/direct client changed after patch")
    original, original_signature = read_regular(manifest.parent / TARGET)
    transformed = transform_v1(original) if record["patch"] == LEGACY_ID else transform(original)
    if sha256(original) not in KNOWN or sha256(original) != record["before"][TARGET] or sha256(transformed) != record["after"][TARGET]:
        raise PatchError("Private Bili original/hash is invalid")
    current = inventory(plugin, (*OUTPUTS, MARKER))
    if not current[TARGET] or current[TARGET]["sha256"] not in {record["before"][TARGET], record["after"][TARGET]}:
        raise PatchError("Unknown/later Bili index; restore refused")
    for name in (MODULE, CONFIG):
        if current[name] and current[name]["sha256"] != record["after"][name]:
            raise PatchError("Unknown/later generated Bili file; restore refused")
    if current[MARKER]:
        expected = (json.dumps(dict(record, state="patched"), indent=2) + "\n").encode()
        if read_regular(plugin / MARKER)[0] != expected:
            raise PatchError("Bili installed marker changed")
    snapshot = {"plugin": str(plugin), "metadata": metadata, "manifest": {"sha256": sha256(raw), "signature": signature},
                "original": {"sha256": sha256(original), "signature": original_signature}, "current": current}
    restore = [TARGET] if current[TARGET]["sha256"] != record["before"][TARGET] else []
    remove = [name for name in (MODULE, CONFIG, MARKER) if current[name]]
    return {"status": "restore_preview" if restore or remove else "already_restored", "applied": False,
            "requires_stopped_host": True, "fingerprint": fingerprint(snapshot), "restore": restore, "remove": remove}, plugin, original, snapshot


def restore(plugin_dir, manifest_file, *, apply=False, host_stopped=False, expected_fingerprint=None):
    result, plugin, original, snapshot = restore_preview(plugin_dir, manifest_file)
    if not apply:
        return result
    if not host_stopped or expected_fingerprint != result["fingerprint"]:
        raise PatchError("Restore needs --host-stopped and unchanged --expected-fingerprint")
    if result["status"] == "already_restored":
        return result
    all_originals = original if isinstance(original, dict) else {TARGET: original}
    stages = stage(plugin, {name: all_originals[name] for name in result["restore"]})
    try:
        fresh, *_ = restore_preview(plugin, manifest_file)
        if fresh["fingerprint"] != result["fingerprint"]:
            raise PatchError("Bili restore inputs changed")
        for name in result["restore"]:
            if inventory(plugin, [name])[name] != snapshot["current"][name]:
                raise PatchError("Bili index changed during restore")
            os.replace(stages[name], plugin / name)
            stages.pop(name)
        for name in result["remove"]:
            if inventory(plugin, [name])[name] != snapshot["current"][name]:
                raise PatchError("Bili output changed during restore")
            (plugin / name).unlink()  # Exact preflight inventory; no glob/recursion.
        return dict(result, status="restored", applied=True)
    finally:
        for target in stages.values():
            if os.path.lexists(target): target.unlink()


def upgrade_restore_preview(plugin, manifest, raw, signature, record):
    if record["patch"] != PATCH_ID:
        raise PatchError("Unknown Bili upgrade manifest")
    metadata = package_snapshot(plugin)
    if {key: value["sha256"] for key, value in metadata.items()} != {key: value["sha256"] for key, value in record["metadata"].items()}:
        raise PatchError("Bili package/direct client changed after upgrade")
    originals, signatures = {}, {}
    for name in (*OUTPUTS, MARKER):
        originals[name], sig = read_regular(manifest.parent / name)
        if sha256(originals[name]) != record["before"][name]:
            raise PatchError("Bili upgrade original changed")
        signatures[name] = {"sha256": sha256(originals[name]), "signature": sig}
    old_marker = json.loads(originals[MARKER])
    if (old_marker.get("patch") != LEGACY_ID or old_marker.get("plugin") != str(plugin) or old_marker.get("state") != "patched"
            or old_marker.get("after") != {name: record["before"][name] for name in OUTPUTS}
            or record["before"][MODULE] != V1_MODULE or record["before"][TARGET] not in V1_INDEX
            or sha256(transform_registered(originals[TARGET])) != record["after"][TARGET]):
        raise PatchError("Unknown Bili upgrade predecessor")
    current = inventory(plugin, (*OUTPUTS, MARKER))
    marker_after = sha256((json.dumps(dict(record, state="patched"), indent=2) + "\n").encode())
    for name in (*OUTPUTS, MARKER):
        after = marker_after if name == MARKER else record["after"][name]
        if not current[name] or current[name]["sha256"] not in {record["before"][name], after}:
            raise PatchError("Unknown/later Bili upgrade output; restore refused")
    snapshot = {"plugin": str(plugin), "metadata": metadata, "manifest": {"sha256": sha256(raw), "signature": signature},
                "originals": signatures, "current": current}
    changes = [name for name in (*OUTPUTS, MARKER) if current[name]["sha256"] != record["before"][name]]
    return {"status": "restore_preview" if changes else "already_restored", "applied": False,
            "requires_stopped_host": True, "fingerprint": fingerprint(snapshot), "restore": changes, "remove": []}, plugin, originals, snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-dir", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--project-config")
    mode.add_argument("--restore-manifest")
    parser.add_argument("--ledger-file")
    parser.add_argument("--backup-dir")
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--host-stopped", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        options = dict(apply=args.apply, host_stopped=args.host_stopped, expected_fingerprint=args.expected_fingerprint)
        if args.restore_manifest:
            result = restore(args.plugin_dir, args.restore_manifest, **options)
        else:
            if not args.ledger_file: raise PatchError("--ledger-file is required")
            result = patch(args.plugin_dir, project_config=args.project_config, ledger_file=args.ledger_file, backup_dir=args.backup_dir, **options)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (PatchError, OSError, ValueError, TypeError, KeyError):
        print("Bili patch rejected; review paths, known hashes and fresh preview", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
