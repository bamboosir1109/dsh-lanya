"""Upgrade only the reviewed old egress module; never load policy or the app.

Preview is read-only. Apply/restore require a stopped host and fresh fingerprint.
Restore this independent layer before egress V1, replay isolation and policy V1.
"""
import argparse
import datetime
import os
from pathlib import Path
import uuid

import patch_onebot_egress as egress
from patch_onebot_policy import PatchError, ordinary_path, read_regular, sha256, write_new
from patch_onebot_replay import encoded, sealed, json_file, private_manifest, identity, package_lock, syntax

REPO = Path(__file__).resolve().parents[1]
PATCH_ID = 'lanya-onebot-egress-images-v1'
GENERATED = egress.GENERATED
INSTALLED_MARKER = 'lib/lanya-egress-images.patch.json'
BEFORE_SHA256 = egress.LEGACY_GENERATED_SHA256
SOURCE = REPO / 'plugin/lib/qq-egress.js'


def parent_layer(plugin, manifest, canonical_sha):
    """Validate every old layer while reserving only GENERATED for this owner.

No virtual filesystem and no weakening of V1's ordinary restore checks: its
three transformations are reconstructed from exact private original bytes.
"""
    path = private_manifest(manifest, plugin)
    record, meta, _ = json_file(path)
    replay_path = record.get('parent', {}).get('replay_manifest')
    if not isinstance(replay_path, str):
        raise PatchError('Explicit egress parent replay manifest required')
    base, expected, chain, watched, installed = egress.parents(plugin, replay_path)
    fields = {'version', 'patch', 'plugin', 'before', 'after', 'backup', 'state', 'package_sha256', 'parent'}
    if (set(record) != fields or record.get('version') != 1 or record.get('patch') != egress.PATCH_ID
            or record.get('state') != 'prepared' or ordinary_path(record.get('plugin', '')) != plugin
            or ordinary_path(record.get('backup', '')) != path.parent or record.get('parent') != chain
            or record.get('package_sha256') != base['package_sha256']
            or set(record.get('before', {})) != set(egress.TARGETS)
            or set(record.get('after', {})) != set((*egress.TARGETS, GENERATED))
            or record['after'][GENERATED] not in (BEFORE_SHA256, canonical_sha)):
        raise PatchError('Unknown egress parent manifest')
    watched[str(path)] = meta
    for name in egress.TARGETS:
        raw, signature = read_regular(path.parent / name)
        if (sha256(raw) != expected[name] or record['before'][name] != expected[name]
                or sha256(egress.transform(name, raw)) != record['after'][name]
                or sha256(installed[name][0]) != record['after'][name]):
            raise PatchError('Egress parent original/transform/installed program mismatch')
        watched[str(path.parent / name)] = dict(sha256=sha256(raw), signature=signature)
    _, marker_meta, marker_raw = json_file(plugin / egress.INSTALLED_MARKER)
    if marker_raw != encoded(dict(record, state='patched')):
        raise PatchError('Complete unchanged egress parent marker required')
    watched[str(plugin / egress.INSTALLED_MARKER)] = marker_meta
    parent = dict(egress_manifest=str(path), egress_manifest_sha256=meta['sha256'],
                  egress_marker_sha256=marker_meta['sha256'], chain=chain)
    return record, parent, watched


def inspect(plugin_dir, egress_manifest, restore_manifest=None):
    plugin = ordinary_path(plugin_dir)
    source, source_signature = read_regular(SOURCE)
    source_sha = sha256(source)
    old, parent, watched = parent_layer(plugin, egress_manifest, source_sha)
    watched[str(SOURCE)] = dict(sha256=source_sha, signature=source_signature)
    module = ordinary_path(plugin / GENERATED)
    current = identity(module) if os.path.lexists(module) else None
    watched[str(module)] = current
    marker = ordinary_path(plugin / INSTALLED_MARKER)
    marker_raw = None
    if os.path.lexists(marker):
        value, meta, marker_raw = json_file(marker)
        watched[str(marker)] = meta
        if restore_manifest is None:
            restore_manifest = ordinary_path(value.get('backup', '')) / 'patch.json'
    else:
        watched[str(marker)] = None
    record = None
    original = None
    if restore_manifest is not None:
        path = private_manifest(restore_manifest, plugin)
        record, meta, _ = json_file(path)
        fields = {'version', 'patch', 'plugin', 'before', 'after', 'backup', 'state', 'package_sha256', 'parent'}
        if (set(record) != fields or record.get('version') != 1 or record.get('patch') != PATCH_ID
                or record.get('state') != 'prepared' or ordinary_path(record.get('plugin', '')) != plugin
                or ordinary_path(record.get('backup', '')) != path.parent
                or record.get('package_sha256') != old['package_sha256'] or record.get('parent') != parent
                or record.get('before') != {GENERATED: BEFORE_SHA256}
                or record.get('after') != {GENERATED: source_sha}
                or old['after'][GENERATED] != BEFORE_SHA256 or source_sha == BEFORE_SHA256):
            raise PatchError('Unknown egress-images private manifest or source drift')
        watched[str(path)] = meta
        original, signature = read_regular(path.parent / GENERATED)
        generated, generated_signature = read_regular(path.parent / 'generated.js')
        if sha256(original) != BEFORE_SHA256 or generated != source:
            raise PatchError('Egress-images private original/generated copy changed')
        watched[str(path.parent / GENERATED)] = dict(sha256=sha256(original), signature=signature)
        watched[str(path.parent / 'generated.js')] = dict(sha256=sha256(generated), signature=generated_signature)
        if marker_raw is not None and marker_raw != encoded(dict(record, state='patched')):
            raise PatchError('Egress-images installed marker changed')
        if current is not None and current['sha256'] not in (BEFORE_SHA256, source_sha):
            raise PatchError('Later/unknown egress-images module change')
        complete = marker_raw is not None and current is not None and current['sha256'] == source_sha
        status = 'already_patched' if complete else 'partial_layer'
    else:
        if marker_raw is not None or current is None or current['sha256'] != old['after'][GENERATED]:
            raise PatchError('Unowned egress module drift; explicit images restore manifest required')
        if current['sha256'] == source_sha:
            status = 'not_needed'
        elif current['sha256'] == BEFORE_SHA256:
            original = read_regular(module)[0]
            status = 'patch_needed'
        else:
            raise PatchError('Only the exact reviewed legacy module may be upgraded')
    before = {GENERATED: old['after'][GENERATED]}
    after = {GENERATED: source_sha}
    snapshot = dict(plugin=str(plugin), parent=parent, watched=watched, before=before, after=after)
    result = dict(patch=PATCH_ID, status=status, applied=False, requires_stopped_host=True,
                  fingerprint=sealed(snapshot), before=before, after=after, parent=parent)
    return result, plugin, original, source, snapshot, record


def validate_flags(apply, host_stopped):
    if type(apply) is not bool or type(host_stopped) is not bool:
        raise PatchError('Apply and stopped-host assertions must be booleans')


def patch(plugin_dir, egress_manifest, *, apply=False, host_stopped=False, expected_fingerprint=None, backup_dir=None):
    validate_flags(apply, host_stopped)
    if not apply:
        result, _, _, source, _, _ = inspect(plugin_dir, egress_manifest)
        if result['status'] == 'partial_layer':
            raise PatchError('Interrupted egress-images layer requires explicit restore')
        syntax({GENERATED: source})
        return result
    if host_stopped is not True or not backup_dir:
        raise PatchError('Apply requires stopped host, private backup and fresh fingerprint')
    with package_lock(plugin_dir):
        result, plugin, original, source, snapshot, _ = inspect(plugin_dir, egress_manifest)
        if result['fingerprint'] != expected_fingerprint or result['status'] == 'partial_layer':
            raise PatchError('Unchanged egress-images preview fingerprint required')
        if result['status'] in ('not_needed', 'already_patched'):
            return result
        syntax({GENERATED: source})
        base = ordinary_path(backup_dir)
        if base == REPO or REPO in base.parents or base == plugin or plugin in base.parents:
            raise PatchError('Private backup must be outside source/installed package')
        base.mkdir(parents=True, exist_ok=True)
        backup = base / (PATCH_ID + '-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex)
        backup.mkdir(); (backup / 'lib').mkdir()
        write_new(backup / GENERATED, original)
        write_new(backup / 'generated.js', source)
        record = dict(version=1, patch=PATCH_ID, plugin=str(plugin), before=result['before'], after=result['after'],
                      backup=str(backup), state='prepared', package_sha256=snapshot['watched'][str(plugin / 'package.json')]['sha256'],
                      parent=result['parent'])
        manifest = backup / 'patch.json'
        write_new(manifest, encoded(record))
        stage = plugin / 'lib' / ('.lanya-egress-images-stage-' + uuid.uuid4().hex + '.js')
        changed = []
        try:
            write_new(stage, source)
            if inspect(plugin, egress_manifest)[0]['fingerprint'] != result['fingerprint']:
                raise PatchError('Egress-images inputs changed before replacement')
            inspect(plugin, egress_manifest, manifest)
            if identity(plugin / GENERATED) != snapshot['watched'][str(plugin / GENERATED)] or read_regular(stage)[0] != source:
                raise PatchError('Egress-images target/staging identity changed')
            os.replace(stage, plugin / GENERATED); changed.append(GENERATED)
            # Validate all parents, copies and owned program before publication.
            inspect(plugin, egress_manifest, manifest)
            write_new(plugin / INSTALLED_MARKER, encoded(dict(record, state='patched')))
            if inspect(plugin, egress_manifest)[0]['status'] != 'already_patched':
                raise PatchError('Egress-images final full readback failed')
            return dict(result, status='patched', state='patched', applied=True, backup=str(backup), manifest=str(manifest))
        except Exception:
            write_new(backup / 'interrupted.json', encoded(dict(patch=PATCH_ID, changed=changed)))
            raise
        finally:
            if os.path.lexists(stage):
                ordinary_path(stage).unlink()


def restore(plugin_dir, manifest, *, apply=False, host_stopped=False, expected_fingerprint=None):
    validate_flags(apply, host_stopped)
    plugin = ordinary_path(plugin_dir)
    manifest = private_manifest(manifest, plugin)
    parent = json_file(manifest)[0].get('parent', {}).get('egress_manifest')
    if not isinstance(parent, str):
        raise PatchError('Explicit egress parent manifest required')

    def preview():
        result, root, original, _, snapshot, layer = inspect(plugin, parent, manifest)
        current = snapshot['watched'][str(root / GENERATED)]
        names = [GENERATED] if current is None or current['sha256'] != layer['before'][GENERATED] else []
        remove = [INSTALLED_MARKER] if snapshot['watched'][str(root / INSTALLED_MARKER)] is not None else []
        return dict(result, status='restore_preview' if names or remove else 'already_restored',
                    restore=names, remove=remove), root, original, snapshot

    if not apply:
        return preview()[0]
    if host_stopped is not True:
        raise PatchError('Restore requires stopped host and fresh fingerprint')
    with package_lock(plugin):
        result, root, original, snapshot = preview()
        if result['fingerprint'] != expected_fingerprint:
            raise PatchError('Unchanged egress-images restore fingerprint required')
        if result['status'] == 'already_restored':
            return result
        stage = None
        try:
            if result['restore']:
                stage = root / 'lib' / ('.lanya-egress-images-restore-' + uuid.uuid4().hex + '.js')
                write_new(stage, original)
            if preview()[0]['fingerprint'] != result['fingerprint']:
                raise PatchError('Egress-images restore inputs changed')
            if stage is not None:
                inspect(root, parent, manifest)
                current = identity(root / GENERATED) if os.path.lexists(root / GENERATED) else None
                if current != snapshot['watched'][str(root / GENERATED)] or read_regular(stage)[0] != original:
                    raise PatchError('Egress-images restore target/staging changed')
                os.replace(stage, root / GENERATED); stage = None
            for name in result['remove']:
                inspect(root, parent, manifest)
                if identity(root / name) != snapshot['watched'][str(root / name)]:
                    raise PatchError('Egress-images marker removal identity changed')
                ordinary_path(root / name).unlink()
            final = preview()[0]
            if final['status'] != 'already_restored' or final['restore'] or final['remove']:
                raise PatchError('Egress-images restore final whole readback failed')
            return dict(result, status='restored', applied=True)
        finally:
            if stage is not None and os.path.lexists(stage):
                ordinary_path(stage).unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin-dir', required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--egress-manifest')
    mode.add_argument('--restore-manifest')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--host-stopped', action='store_true')
    parser.add_argument('--expected-fingerprint')
    parser.add_argument('--backup-dir')
    args = parser.parse_args(argv)
    try:
        options = dict(apply=args.apply, host_stopped=args.host_stopped, expected_fingerprint=args.expected_fingerprint)
        result = restore(args.plugin_dir, args.restore_manifest, **options) if args.restore_manifest else patch(
            args.plugin_dir, args.egress_manifest, backup_dir=args.backup_dir, **options)
        import json
        print(json.dumps(result, ensure_ascii=True)); return 0
    except (PatchError, OSError, ValueError, TypeError, KeyError) as error:
        import json, sys
        print(json.dumps({'error': str(error) if isinstance(error, PatchError) else type(error).__name__}), file=sys.stderr); return 2


if __name__ == '__main__':
    raise SystemExit(main())
