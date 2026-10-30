"""Update one reviewed OneBot adapter after policy/replay/egress/images.

Default preview is read-only. Never import the app, open policy state, or run
models/transports. Restore autonomy before images, egress, replay and policy.
Existing parent markers and all other package programs remain byte-identical.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import uuid

import patch_onebot_policy as policy
import patch_onebot_replay as replay
import patch_onebot_egress as egress
import patch_onebot_egress_images as images
from patch_onebot_policy import PatchError, ordinary_path, read_regular, sha256, write_new, replace_once
from patch_onebot_replay import encoded, sealed, json_file, identity, package_lock, syntax

REPO = Path(__file__).resolve().parents[1]
PATCH_ID = 'lanya-onebot-autonomy-v1'
INSTALLED_MARKER = 'lib/lanya-autonomy.patch.json'
TARGET = 'lib/lanya-policy-adapter.js'
LEGACY_BASE_SHA256 = 'd6528707aea0b98ad7ef219928918f16f39a082b27c20508bbeea1a9a1b63545'
DEPENDENCIES = {
    'decision-policy.js': '1ad012e68a70ad0e6c69c8780d4fa98992d28c080f91512fa61c6b30b903907c',
    'observation-queue.js': '2e35c703c7b1edc2bb3d0f107a9c65976d1a8f8d601b470340557fe8d14e1dce',
    'qq-egress.js': 'ca1afbccfb44927b381e8f8f820802acc320f1c5079601d5e0a0df81267b32a2',
}
FIELDS = {'version', 'patch', 'plugin', 'before', 'after', 'backup', 'state', 'package_sha256', 'parent'}


def private_path(value, plugin, source=None):
    if not Path(value).is_absolute():
        raise PatchError('Explicit absolute private manifest required')
    path = ordinary_path(value)
    roots = (plugin, REPO) + ((source,) if source is not None else ())
    if path.name != 'patch.json' or any(path == root or root in path.parents for root in roots):
        raise PatchError('Explicit private patch.json outside installed/source roots required')
    return path


def base_adapter(raw):
    text = replace_once(raw.decode('utf-8'), "from './decision-policy.js'", "from './lanya-decision-policy.js'")
    return replace_once(text, "from './observation-queue.js'", "from './lanya-observation-queue.js'").encode('utf-8')


def compile_adapter(raw):
    return egress.transform(TARGET, base_adapter(raw))


def installed_root(value):
    plugin = ordinary_path(value)
    if not Path(value).is_absolute() or not plugin.is_dir():
        raise PatchError('Explicit absolute installed package root required')
    return plugin


def read_source(value):
    source = ordinary_path(value)
    if not Path(value).is_absolute() or not source.is_dir():
        raise PatchError('Explicit absolute source repository required')
    package, meta, _ = json_file(source / 'package.json')
    if package.get('name') != 'dsh-lanya' or package.get('type') != 'module':
        raise PatchError('Expected the explicit dsh-lanya source repository')
    watched = {str(source / 'package.json'): meta}
    for name, wanted in DEPENDENCIES.items():
        raw, sig = read_regular(source / 'plugin/lib' / name)
        if sha256(raw) != wanted:
            raise PatchError('Unsupported source dependency drift: ' + name)
        watched[str(source / 'plugin/lib' / name)] = dict(sha256=wanted, signature=sig)
    raw, sig = read_regular(source / 'plugin/lib/onebot-policy-adapter.js')
    watched[str(source / 'plugin/lib/onebot-policy-adapter.js')] = dict(sha256=sha256(raw), signature=sig)
    return source, raw, compile_adapter(raw), watched


def parents(plugin, images_manifest, allowed_adapter, source_root=None):
    """Reconstruct real private parent transforms without current-source globals.

    The old adapter is pinned to the public c684991 contract; the only alternate
    base is the explicitly reviewed source used for this update. No prior
    installer is rerun and no virtual filesystem masks installed changes.
    """
    watched = {}

    def record(path):
        path = private_path(path, plugin, source_root)
        value, meta, _ = json_file(path)
        watched[str(path)] = meta
        return path, value, meta

    def verify_record(path, value, patch, parent, before_keys, after_keys, package_sha):
        if (set(value) != FIELDS or type(value.get('version')) is not int or value['version'] != 1
                or value.get('patch') != patch or value.get('state') != 'prepared'
                or ordinary_path(value.get('plugin', '')) != plugin
                or ordinary_path(value.get('backup', '')) != path.parent
                or value.get('parent') != parent or value.get('package_sha256') != package_sha
                or not isinstance(value.get('before'), dict) or set(value['before']) != set(before_keys)
                or not isinstance(value.get('after'), dict) or set(value['after']) != set(after_keys)):
            raise PatchError('Unknown or foreign parent manifest: ' + patch)

    def saved(path, name, wanted):
        raw, sig = read_regular(path.parent / name)
        if sha256(raw) != wanted:
            raise PatchError('Parent original hash mismatch: ' + name)
        watched[str(path.parent / name)] = dict(sha256=wanted, signature=sig)
        return raw

    def marker(name, wanted):
        _, meta, raw = json_file(plugin / name)
        if raw != wanted:
            raise PatchError('Parent marker changed: ' + name)
        watched[str(plugin / name)] = meta
        return meta['sha256']

    ipath, image, imeta = record(images_manifest)
    epath, edge, emeta = record(image.get('parent', {}).get('egress_manifest', ''))
    rpath, review, rmeta = record(edge.get('parent', {}).get('replay_manifest', ''))
    ppath, base, pmeta = record(review.get('parent', {}).get('manifest', ''))
    package, pkgmeta, _ = json_file(plugin / 'package.json')
    watched[str(plugin / 'package.json')] = pkgmeta
    if (package.get('name'), package.get('version'), package.get('type')) != ('dsh-onebot-qq', '0.8.1', 'module'):
        raise PatchError('Only reviewed OneBot 0.8.1 is supported')
    pfields = {'patch', 'plugin', 'before', 'after', 'backup', 'state', 'package_sha256'}
    if (set(base) != pfields or base.get('patch') != policy.PATCH_ID or base.get('state') != 'prepared'
            or ordinary_path(base.get('plugin', '')) != plugin or ordinary_path(base.get('backup', '')) != ppath.parent
            or base.get('package_sha256') != pkgmeta['sha256']
            or set(base.get('before', {})) != set(policy.TARGETS)
            or set(base.get('after', {})) != set(policy.TARGETS + policy.GENERATED)):
        raise PatchError('Unknown or foreign policy V1 parent')
    for name in policy.TARGETS:
        raw = saved(ppath, name, base['before'][name])
        if sha256(raw) not in policy.KNOWN[name] or sha256(policy.transform(name, raw)) != base['after'][name]:
            raise PatchError('Unrecognized policy original/transform')
    for name, source in [('lanya-decision-policy.js', 'decision-policy.js'), ('lanya-observation-queue.js', 'observation-queue.js')]:
        if base['after'][name] != DEPENDENCIES[source]:
            raise PatchError('Unsupported policy dependency contract')
    if base['after']['lanya-policy-adapter.js'] not in (LEGACY_BASE_SHA256, allowed_adapter):
        raise PatchError('Unrecognized adapter base contract')
    pm = marker('lib/lanya-policy.patch.json', (json.dumps(dict(base, state='patched'), indent=2) + '\n').encode())
    replay_parent = dict(patch=policy.PATCH_ID, manifest=str(ppath), manifest_sha256=pmeta['sha256'], installed_marker_sha256=pm)
    # validate_record is a pure transform/metadata reader with no canonical-module lookup.
    replay_record, _, outputs, extra = replay.validate_record(plugin, rpath, base, replay_parent)
    watched.update(extra)
    rm = marker(replay.INSTALLED_MARKER, encoded(dict(replay_record, state='patched')))
    chain = dict(policy_manifest=str(ppath), policy_manifest_sha256=pmeta['sha256'], policy_marker_sha256=pm,
                 replay_manifest=str(rpath), replay_manifest_sha256=rmeta['sha256'], replay_marker_sha256=rm)
    expected = {'lib/' + name: digest for name, digest in base['after'].items()}
    expected.update({name: sha256(raw) for name, raw in outputs.items()})
    verify_record(epath, edge, egress.PATCH_ID, chain, egress.TARGETS,
                  (*egress.TARGETS, egress.GENERATED), pkgmeta['sha256'])
    for name in egress.TARGETS:
        raw = saved(epath, name, expected[name])
        if edge['before'][name] != expected[name] or sha256(egress.transform(name, raw)) != edge['after'][name]:
            raise PatchError('Egress original/transform mismatch')
    if edge['after'][egress.GENERATED] != images.BEFORE_SHA256:
        raise PatchError('This incremental updater requires the reviewed images parent layer')
    em = marker(egress.INSTALLED_MARKER, encoded(dict(edge, state='patched')))
    image_parent = dict(egress_manifest=str(epath), egress_manifest_sha256=emeta['sha256'], egress_marker_sha256=em, chain=chain)
    verify_record(ipath, image, images.PATCH_ID, image_parent, [images.GENERATED], [images.GENERATED], pkgmeta['sha256'])
    if (image['before'] != {images.GENERATED: images.BEFORE_SHA256}
            or image['after'] != {images.GENERATED: DEPENDENCIES['qq-egress.js']}):
        raise PatchError('Unrecognized images module contract')
    saved(ipath, images.GENERATED, images.BEFORE_SHA256)
    saved(ipath, 'generated.js', DEPENDENCIES['qq-egress.js'])
    im = marker(images.INSTALLED_MARKER, encoded(dict(image, state='patched')))
    expected.update(edge['after']); expected.update(image['after'])
    for name, wanted in expected.items():
        raw, sig = read_regular(plugin / name)
        if name != TARGET and sha256(raw) != wanted:
            raise PatchError('Unowned parent program changed: ' + name)
        watched[str(plugin / name)] = dict(sha256=sha256(raw), signature=sig)
    parent = dict(images_manifest=str(ipath), images_manifest_sha256=imeta['sha256'], images_marker_sha256=im,
                  egress=image_parent, chain=chain)
    return expected[TARGET], parent, watched, pkgmeta['sha256']


def inspect(plugin_dir, source_repo=None, images_manifest=None, restore_manifest=None):
    plugin = installed_root(plugin_dir)
    source_root = ordinary_path(source_repo) if source_repo is not None else None
    source_watched, record, original, path = {}, None, None, None
    marker = ordinary_path(plugin / INSTALLED_MARKER)
    marker_raw = None
    if os.path.lexists(marker):
        value, marker_meta, marker_raw = json_file(marker)
        if restore_manifest is None:
            restore_manifest = ordinary_path(value.get('backup', '')) / 'patch.json'
    if restore_manifest is not None:
        path = private_path(restore_manifest, plugin, source_root)
        record, record_meta, _ = json_file(path)
        fields = FIELDS | {'source'}
        if (set(record) != fields or type(record.get('version')) is not int or record['version'] != 1
                or record.get('patch') != PATCH_ID or record.get('state') != 'prepared'
                or ordinary_path(record.get('plugin', '')) != plugin
                or ordinary_path(record.get('backup', '')) != path.parent
                or set(record.get('before', {})) != {TARGET} or set(record.get('after', {})) != {TARGET}):
            raise PatchError('Unknown or foreign autonomy manifest')
        raw, sig = read_regular(path.parent / 'source-adapter.js')
        generated = compile_adapter(raw)
        if record.get('source', {}).get('adapter_sha256') != sha256(raw) or record['after'] != {TARGET: sha256(generated)}:
            raise PatchError('Autonomy source copy/compiled contract changed')
        source_watched[str(path)] = record_meta
        source_watched[str(path.parent / 'source-adapter.js')] = dict(sha256=sha256(raw), signature=sig)
        saved, saved_sig = read_regular(path.parent / 'generated.js')
        if saved != generated:
            raise PatchError('Autonomy generated private copy changed')
        source_watched[str(path.parent / 'generated.js')] = dict(sha256=sha256(saved), signature=saved_sig)
        original, old_sig = read_regular(path.parent / TARGET)
        if sha256(original) != record['before'][TARGET]:
            raise PatchError('Autonomy original backup changed')
        source_watched[str(path.parent / TARGET)] = dict(sha256=sha256(original), signature=old_sig)
        images_manifest = record.get('parent', {}).get('images_manifest')
        if source_repo is not None:
            source, current_source, compiled, current_watched = read_source(source_repo)
            if current_source != raw or compiled != generated or record['source'].get('repository') != str(source):
                raise PatchError('New source drift requires restoring the existing autonomy layer first')
            source_watched.update(current_watched)
    else:
        source, raw, generated, source_watched = read_source(source_repo)
    if images_manifest is None:
        raise PatchError('Explicit images parent manifest required')
    before, parent, watched, package_sha = parents(plugin, images_manifest, sha256(base_adapter(raw)), source_root)
    watched.update(source_watched)
    current = watched[str(plugin / TARGET)]
    if record is not None:
        if (record['parent'] != parent or record.get('package_sha256') != package_sha
                or record['before'] != {TARGET: before}
                or marker_raw is not None and marker_raw != encoded(dict(record, state='patched'))):
            raise PatchError('Autonomy parent/marker binding changed')
        if current['sha256'] not in (before, sha256(generated)):
            raise PatchError('Later or unknown adapter modification; refusing overwrite')
        status = 'already_patched' if marker_raw is not None and current['sha256'] == sha256(generated) else 'partial_layer'
    else:
        if marker_raw is not None or current['sha256'] != before:
            raise PatchError('Unowned adapter change; private restore manifest required')
        original = read_regular(plugin / TARGET)[0]
        status = 'not_needed' if current['sha256'] == sha256(generated) else 'patch_needed'
    watched[str(marker)] = marker_meta if marker_raw is not None else None
    snapshot = dict(plugin=str(plugin), before={TARGET: before}, after={TARGET: sha256(generated)},
                    parent=parent, package_sha256=package_sha, watched=watched)
    result = dict(patch=PATCH_ID, status=status, applied=False, before=snapshot['before'], after=snapshot['after'],
                  parent=parent, fingerprint=sealed(snapshot), requires_stopped_host=True)
    return result, plugin, raw, original, generated, snapshot, record


def flags(apply, host_stopped):
    if type(apply) is not bool or type(host_stopped) is not bool:
        raise PatchError('Apply and stopped-host assertions must be actual booleans')


def patch(plugin_dir, source_repo, images_manifest, *, apply=False, host_stopped=False,
          expected_fingerprint=None, backup_dir=None):
    flags(apply, host_stopped)
    if not apply:
        result, _, _, _, generated, _, _ = inspect(plugin_dir, source_repo, images_manifest)
        if result['status'] == 'partial_layer':
            raise PatchError('Interrupted autonomy update requires explicit restore')
        syntax({TARGET: generated})
        return result
    if host_stopped is not True or backup_dir is None:
        raise PatchError('Apply requires stopped host, private backup and fresh preview fingerprint')
    with package_lock(installed_root(plugin_dir)):
        result, plugin, raw, original, generated, snapshot, _ = inspect(plugin_dir, source_repo, images_manifest)
        if result['fingerprint'] != expected_fingerprint or result['status'] == 'partial_layer':
            raise PatchError('Exact fresh autonomy preview fingerprint required')
        if result['status'] in ('already_patched', 'not_needed'):
            return result
        syntax({TARGET: generated})
        source = ordinary_path(source_repo); base = ordinary_path(backup_dir)
        if not Path(backup_dir).is_absolute() or any(base == root or root in base.parents for root in (plugin, REPO, source)):
            raise PatchError('Explicit private backup outside source and installed roots required')
        base.mkdir(parents=True, exist_ok=True)
        backup = base / (PATCH_ID + '-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex)
        backup.mkdir(); (backup / 'lib').mkdir()
        write_new(backup / TARGET, original); write_new(backup / 'source-adapter.js', raw); write_new(backup / 'generated.js', generated)
        record = dict(version=1, patch=PATCH_ID, plugin=str(plugin), before=result['before'], after=result['after'],
                      backup=str(backup), state='prepared', package_sha256=snapshot['package_sha256'], parent=result['parent'],
                      source=dict(repository=str(source), adapter_sha256=sha256(raw)))
        manifest = backup / 'patch.json'; write_new(manifest, encoded(record))
        stage = plugin / 'lib' / ('.lanya-autonomy-stage-' + uuid.uuid4().hex + '.js')
        changed = []
        try:
            write_new(stage, generated)
            if inspect(plugin, source, images_manifest)[0]['fingerprint'] != result['fingerprint']:
                raise PatchError('Autonomy inputs changed before replacement')
            inspect(plugin, source, images_manifest, manifest)
            if identity(plugin / TARGET) != snapshot['watched'][str(plugin / TARGET)] or read_regular(stage)[0] != generated:
                raise PatchError('Autonomy target/staging identity changed')
            os.replace(stage, plugin / TARGET); changed.append(TARGET)
            inspect(plugin, source, images_manifest, manifest)
            write_new(plugin / INSTALLED_MARKER, encoded(dict(record, state='patched')))
            if inspect(plugin, source, images_manifest)[0]['status'] != 'already_patched':
                raise PatchError('Autonomy final whole readback failed')
            return dict(result, status='patched', state='patched', applied=True, manifest=str(manifest), backup=str(backup))
        except Exception:
            write_new(backup / 'interrupted.json', encoded(dict(patch=PATCH_ID, changed=changed)))
            raise
        finally:
            if os.path.lexists(stage): ordinary_path(stage).unlink()


def restore(plugin_dir, manifest, *, apply=False, host_stopped=False, expected_fingerprint=None):
    flags(apply, host_stopped)

    def preview():
        result, plugin, _, original, _, snapshot, record = inspect(plugin_dir, restore_manifest=manifest)
        names = [TARGET] if snapshot['watched'][str(plugin / TARGET)]['sha256'] != record['before'][TARGET] else []
        remove = [INSTALLED_MARKER] if snapshot['watched'][str(plugin / INSTALLED_MARKER)] is not None else []
        return dict(result, status='restore_preview' if names or remove else 'already_restored', restore=names, remove=remove), plugin, original, snapshot

    if not apply: return preview()[0]
    if host_stopped is not True: raise PatchError('Restore requires stopped host and fresh preview fingerprint')
    with package_lock(installed_root(plugin_dir)):
        result, plugin, original, snapshot = preview()
        if result['fingerprint'] != expected_fingerprint: raise PatchError('Exact fresh autonomy restore fingerprint required')
        if result['status'] == 'already_restored': return result
        stage = None
        try:
            if result['restore']:
                stage = plugin / 'lib' / ('.lanya-autonomy-restore-' + uuid.uuid4().hex + '.js'); write_new(stage, original)
            if preview()[0]['fingerprint'] != result['fingerprint']: raise PatchError('Autonomy restore inputs changed')
            if stage is not None:
                if identity(plugin / TARGET) != snapshot['watched'][str(plugin / TARGET)] or read_regular(stage)[0] != original:
                    raise PatchError('Autonomy restore target/staging changed')
                os.replace(stage, plugin / TARGET); stage = None
            if result['remove']:
                preview()
                if identity(plugin / INSTALLED_MARKER) != snapshot['watched'][str(plugin / INSTALLED_MARKER)]:
                    raise PatchError('Autonomy marker changed before removal')
                ordinary_path(plugin / INSTALLED_MARKER).unlink()
            final = preview()[0]
            if final['status'] != 'already_restored' or final['restore'] or final['remove']:
                raise PatchError('Autonomy restore final whole readback failed')
            return dict(result, status='restored', applied=True)
        finally:
            if stage is not None and os.path.lexists(stage): ordinary_path(stage).unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin-dir', required=True)
    parser.add_argument('--source-repo')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--images-manifest'); mode.add_argument('--restore-manifest')
    parser.add_argument('--apply', action='store_true'); parser.add_argument('--host-stopped', action='store_true')
    parser.add_argument('--expected-fingerprint'); parser.add_argument('--backup-dir')
    args = parser.parse_args(argv)
    try:
        options = dict(apply=args.apply, host_stopped=args.host_stopped, expected_fingerprint=args.expected_fingerprint)
        if args.restore_manifest:
            if args.source_repo: raise PatchError('Restore uses private source copies, not a current repository')
            result = restore(args.plugin_dir, args.restore_manifest, **options)
        else:
            if not args.source_repo: raise PatchError('Explicit --source-repo required for preview/apply')
            result = patch(args.plugin_dir, args.source_repo, args.images_manifest, backup_dir=args.backup_dir, **options)
        print(json.dumps(result, ensure_ascii=True)); return 0
    except (PatchError, OSError, ValueError, TypeError, KeyError) as error:
        import sys
        print(json.dumps({'error': str(error) if isinstance(error, PatchError) else type(error).__name__}), file=sys.stderr); return 2


if __name__ == '__main__':
    raise SystemExit(main())
