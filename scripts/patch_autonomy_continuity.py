"""Incremental continuity update for the reviewed OneBot autonomy V1 layer.

Preview is read-only. Apply/restore require an explicitly stopped host, a
private backup and a fresh fingerprint. Old layer manifests remain unchanged.
Only the adapter and observation queue are replaced; policy state is never read.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import uuid

import patch_onebot_autonomy as autonomy
from patch_onebot_policy import PatchError, ordinary_path, read_regular, sha256, write_new
from patch_onebot_replay import encoded, sealed, json_file, identity, package_lock, syntax

REPO = Path(__file__).resolve().parents[1]
PATCH_ID = "lanya-autonomy-continuity-v1"
MARKER = "lib/lanya-continuity.patch.json"
TARGETS = ("lib/lanya-policy-adapter.js", "lib/lanya-observation-queue.js")


def root(value):
    if not Path(value).is_absolute():
        raise PatchError("An explicit absolute package path is required")
    result = ordinary_path(value)
    if not result.is_dir():
        raise PatchError("Installed package directory is unavailable")
    return result


def outputs(source):
    source = root(source)
    package, _, _ = json_file(source / "package.json")
    if (package.get("name"), package.get("type")) != ("dsh-lanya", "module"):
        raise PatchError("Explicit dsh-lanya source is required")
    adapter, _ = read_regular(source / "plugin/lib/onebot-policy-adapter.js")
    queue, _ = read_regular(source / "plugin/lib/observation-queue.js")
    helper = (read_regular(source / "plugin/lib/autonomy-intents.js")[0]
              if b"from './autonomy-intents.js'" in adapter else None)
    diary = (read_regular(source / "plugin/lib/narrative-diary.js")[0]
             if b"from './narrative-diary.js'" in adapter else None)
    return {TARGETS[0]: autonomy.compile_adapter(adapter, helper, diary), TARGETS[1]: queue}


def private_manifest(value, plugin, source=None):
    if not Path(value).is_absolute():
        raise PatchError("An explicit private manifest is required")
    result = ordinary_path(value)
    roots = (plugin, REPO) + ((source,) if source else ())
    if result.name != "patch.json" or any(result == item or item in result.parents for item in roots):
        raise PatchError("Private originals must remain outside source and package")
    return result


def inspect(plugin_dir, source_repo=None, manifest=None):
    plugin = root(plugin_dir)
    marker_path = ordinary_path(plugin / MARKER)
    marker = None
    if os.path.lexists(marker_path):
        marker, _, _ = json_file(marker_path)
        if manifest is None:
            manifest = Path(marker.get("backup", "")) / "patch.json"
    if manifest is None:
        if source_repo is None:
            raise PatchError("Explicit source is required for a new layer")
        source = root(source_repo)
        # This verifies the actual old private parent chain before any writes.
        # Do not pass newer source through the historical V1 dependency pins.
        parent_view = autonomy.inspect(plugin)
        if parent_view[0]["status"] != "already_patched":
            raise PatchError("A complete reviewed autonomy V1 parent is required")
        generated = outputs(source)
        watched = parent_view[5]["watched"]
        before = {name: identity(plugin / name) for name in TARGETS}
        record = {"version": 1, "patch": PATCH_ID, "plugin": str(plugin),
                  "source": str(source), "state": "prepared", "before": before,
                  "after": {name: sha256(raw) for name, raw in generated.items()},
                  "parent_fingerprint": parent_view[0]["fingerprint"],
                  "parent_files": watched}
        status = "patch_needed"
    else:
        source = root(source_repo) if source_repo is not None else None
        manifest = private_manifest(manifest, plugin, source)
        record, _, _ = json_file(manifest)
        if (record.get("version") != 1 or record.get("patch") != PATCH_ID or
                record.get("plugin") != str(plugin) or record.get("state") != "prepared" or
                record.get("backup") != str(manifest.parent) or
                set(record.get("before", {})) != set(TARGETS) or
                set(record.get("after", {})) != set(TARGETS) or
                not isinstance(record.get("parent_files"), dict)):
            raise PatchError("Unknown continuity manifest")
        generated = {}
        for name in TARGETS:
            original, _ = read_regular(manifest.parent / "original" / name)
            staged, _ = read_regular(manifest.parent / "generated" / name)
            if sha256(original) != record["before"][name]["sha256"] or sha256(staged) != record["after"][name]:
                raise PatchError("Continuity private original/generated copy changed")
            generated[name] = staged
        if source is not None and (str(source) != record["source"] or outputs(source) != generated):
            raise PatchError("Newer source requires an explicit new update")
        # Each original was verified by the historical parent reader before
        # application. Preserve its full evidence set, excluding only our two
        # owned programs, which now have their own exact before/after contract.
        for filename, expected in record["parent_files"].items():
            if filename in {str(plugin / name) for name in TARGETS}:
                continue
            actual = identity(Path(filename)) if os.path.lexists(filename) else None
            if sealed(actual) != sealed(expected):
                raise PatchError("Historical parent evidence changed")
        current = {name: identity(plugin / name)["sha256"] for name in TARGETS}
        old = {name: entry["sha256"] for name, entry in record["before"].items()}
        if any(current[name] not in (old[name], record["after"][name]) for name in TARGETS):
            raise PatchError("Unknown later continuity program change")
        if marker is not None and marker != dict(record, state="patched"):
            raise PatchError("Continuity installed marker changed")
        status = "already_patched" if current == record["after"] and marker is not None else "partial_layer"
    state = {name: identity(plugin / name) for name in TARGETS}
    snapshot = {"record": record, "current": state,
                "marker": identity(marker_path) if os.path.lexists(marker_path) else None}
    result = {"patch": PATCH_ID, "status": status, "applied": False,
              "requires_stopped_host": True, "fingerprint": sealed(snapshot),
              "program_count": len(TARGETS), "before": {n: x["sha256"] for n, x in record["before"].items()},
              "after": record["after"]}
    return result, plugin, generated, record


def patch(plugin_dir, source_repo, *, apply=False, host_stopped=False,
          expected_fingerprint=None, backup_dir=None):
    view, plugin, generated, record = inspect(plugin_dir, source_repo)
    syntax(generated)
    if not apply:
        if view["status"] == "partial_layer":
            raise PatchError("Interrupted continuity update needs explicit restore")
        return view
    if type(apply) is not bool or host_stopped is not True or backup_dir is None:
        raise PatchError("Apply requires a stopped host and private backup")
    with package_lock(plugin):
        fresh, _, generated, record = inspect(plugin, source_repo)
        if fresh["fingerprint"] != expected_fingerprint or fresh["status"] == "partial_layer":
            raise PatchError("Fresh continuity preview fingerprint required")
        if fresh["status"] == "already_patched":
            return fresh
        base = root(backup_dir)
        for forbidden in (plugin, root(source_repo), REPO):
            if base == forbidden or forbidden in base.parents:
                raise PatchError("Private backup must be outside source and installed package")
        backup = base / (PATCH_ID + "-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex)
        backup.mkdir()
        record = dict(record, backup=str(backup))
        for name in TARGETS:
            for category in ("original", "generated"):
                (backup / category / name).parent.mkdir(parents=True, exist_ok=True)
            original, _ = read_regular(plugin / name)
            write_new(backup / "original" / name, original)
            write_new(backup / "generated" / name, generated[name])
        manifest = backup / "patch.json"
        write_new(manifest, encoded(record))
        stages = {}
        changed = []
        try:
            for name, raw in generated.items():
                temporary = plugin / "lib" / (".lanya-continuity-" + uuid.uuid4().hex + ".js")
                write_new(temporary, raw)
                stages[name] = temporary
            if inspect(plugin, source_repo)[0]["fingerprint"] != expected_fingerprint:
                raise PatchError("Continuity inputs changed before apply")
            for name in TARGETS:
                if identity(plugin / name) != record["before"][name] or read_regular(stages[name])[0] != generated[name]:
                    raise PatchError("Continuity replacement input changed")
                os.replace(stages.pop(name), plugin / name)
                changed.append(name)
            write_new(plugin / MARKER, encoded(dict(record, state="patched")))
            if inspect(plugin, source_repo, manifest)[0]["status"] != "already_patched":
                raise PatchError("Continuity readback did not verify")
            return dict(fresh, status="patched", applied=True, manifest=str(manifest))
        except Exception:
            write_new(backup / "interrupted.json", encoded({"patch": PATCH_ID, "changed": changed}))
            raise
        finally:
            for temporary in stages.values():
                if os.path.lexists(temporary):
                    ordinary_path(temporary).unlink()


def restore(plugin_dir, manifest, *, apply=False, host_stopped=False, expected_fingerprint=None):
    view, plugin, _, record = inspect(plugin_dir, manifest=manifest)
    if not apply:
        return dict(view, status="restore_preview")
    if type(apply) is not bool or host_stopped is not True:
        raise PatchError("Restore requires an explicitly stopped host")
    with package_lock(plugin):
        if inspect(plugin, manifest=manifest)[0]["fingerprint"] != expected_fingerprint:
            raise PatchError("Fresh restore preview fingerprint required")
        for name in reversed(TARGETS):
            old = record["before"][name]["sha256"]
            current = identity(plugin / name)
            if current["sha256"] == old:
                continue
            raw, _ = read_regular(Path(manifest).parent / "original" / name)
            temporary = plugin / "lib" / (".lanya-continuity-restore-" + uuid.uuid4().hex + ".js")
            write_new(temporary, raw)
            if identity(plugin / name) != current or current["sha256"] != record["after"][name]:
                raise PatchError("Restore target changed")
            os.replace(temporary, plugin / name)
        marker = plugin / MARKER
        if os.path.lexists(marker):
            if json_file(marker)[0] != dict(record, state="patched"):
                raise PatchError("Restore marker changed")
            marker.unlink()
        autonomy.inspect(plugin)  # Historical V1 must again verify in place.
        return dict(view, status="restored", applied=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-dir", required=True)
    parser.add_argument("--source-repo")
    parser.add_argument("--restore-manifest")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--host-stopped", action="store_true")
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--backup-dir")
    args = parser.parse_args(argv)
    try:
        common = dict(apply=args.apply, host_stopped=args.host_stopped, expected_fingerprint=args.expected_fingerprint)
        result = (restore(args.plugin_dir, args.restore_manifest, **common) if args.restore_manifest else
                  patch(args.plugin_dir, args.source_repo, backup_dir=args.backup_dir, **common))
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, TypeError) as error:
        print(json.dumps({"status": "refused", "error_class": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
