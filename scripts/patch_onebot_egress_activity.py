"""One-module QQ activity-reference update over the complete meme-picker layer.

Default preview is read-only. Host-stopped apply/restore require a fresh whole
fingerprint and private originals. No host, policy, messages or assets are read.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import uuid

import patch_meme_qq_picker as picker
from patch_onebot_policy import PatchError, ordinary_path, read_regular, sha256, write_new
from patch_onebot_replay import encoded, sealed, json_file, identity, syntax

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / 'plugin/lib/qq-egress.js'
PATCH_ID = 'lanya-onebot-egress-activity-v1'
MODULE = 'lib/lanya-qq-egress.js'
MARKER = 'lib/lanya-egress-activity.patch.json'
BEFORE = 'ca1afbccfb44927b381e8f8f820802acc320f1c5079601d5e0a0df81267b32a2'


def parent_view(packages, manifest, original=None, before=None):
    def inspect():
        view = picker.inspect(packages['meme'], packages['onebot'], manifest, canonical=False)
        if view[0]['status'] != 'already_patched':
            raise PatchError('Complete reviewed picker parent required')
        return view[0]['fingerprint']
    if original is None:
        return inspect()
    # The sole reserved module has a real original, physical identity and slot.
    # Every unrelated program/marker is checked at its actual historical path.
    with picker.historical_bridge(packages['onebot'] / MODULE, original, before):
        return inspect()


def inspect(meme_dir, onebot_dir, picker_manifest=None, manifest=None, *, canonical=True):
    packages = picker.roots(meme_dir, onebot_dir)
    target = ordinary_path(packages['onebot'] / MODULE)
    marker_path = ordinary_path(packages['onebot'] / MARKER)
    marker = json_file(marker_path)[0] if os.path.lexists(marker_path) else None
    if marker is not None and manifest is None:
        manifest = marker.get('manifest', '')
    current = identity(target) if os.path.lexists(target) else None
    source, source_sig = read_regular(SOURCE) if canonical else (None, None)
    private, slot, original = None, None, None
    if manifest is None:
        if source is None or sha256(source) == BEFORE or marker is not None:
            raise PatchError('Explicit new source and unused layer required')
        if current is None or current['sha256'] != BEFORE or target.lstat().st_nlink != 1:
            raise PatchError('Exact reviewed images module required')
        parent_path = picker.manifest_path(picker_manifest, packages)
        fingerprint = parent_view(packages, parent_path)
        original = read_regular(target)[0]
        generated = source
        record = dict(version=1, patch=PATCH_ID, state='prepared',
            roots={key: str(path) for key, path in packages.items()}, before=current,
            after=sha256(source), parent_manifest=str(parent_path),
            parent_manifest_record=identity(parent_path), parent_fingerprint=fingerprint)
        status = 'patch_needed'
    else:
        private = picker.manifest_path(manifest, packages)
        record, _, _ = json_file(private)
        fields = {'version', 'patch', 'state', 'roots', 'before', 'after', 'parent_manifest',
                  'parent_manifest_record', 'parent_fingerprint', 'manifest', 'slot'}
        if (set(record) != fields or type(record.get('version')) is not int or record['version'] != 1
                or record.get('patch') != PATCH_ID or record.get('state') != 'prepared'
                or record.get('roots') != {key: str(path) for key, path in packages.items()}
                or record.get('manifest') != str(private) or record.get('before', {}).get('sha256') != BEFORE
                or not isinstance(record.get('after'), str) or not re.fullmatch('[0-9a-f]{64}', record['after'])
                or record['after'] == BEFORE):
            raise PatchError('Unknown activity-egress private manifest')
        parent_path = picker.manifest_path(record['parent_manifest'], packages)
        if picker_manifest is not None and picker.manifest_path(picker_manifest, packages) != parent_path:
            raise PatchError('Different parent manifest refused')
        if sealed(identity(parent_path)) != sealed(record['parent_manifest_record']):
            raise PatchError('Parent manifest identity changed')
        original = read_regular(private.parent / 'original.js')[0]
        generated = read_regular(private.parent / 'generated.js')[0]
        if sha256(original) != BEFORE or sha256(generated) != record['after'] or (canonical and generated != source):
            raise PatchError('Private original/generated or current source changed')
        if current is not None and current['sha256'] not in (BEFORE, record['after']):
            raise PatchError('Unknown later module change')
        if current is not None and current['sha256'] == BEFORE and sealed(current) != sealed(record['before']):
            raise PatchError('Before bytes with an unknown physical identity')
        if not isinstance(record['slot'], str) or not re.fullmatch(r'\.lanya-egress-activity-original-[0-9a-f]{32}\.js', record['slot']):
            raise PatchError('Invalid physical original slot')
        slot = ordinary_path(target.parent / record['slot'])
        if os.path.lexists(slot):
            if (sealed(identity(slot)) != sealed(record['before']) or
                    slot.lstat().st_nlink != (2 if sealed(current) == sealed(record['before']) else 1)):
                raise PatchError('Physical original identity/link count changed')
            fingerprint = parent_view(packages, parent_path, read_regular(slot)[0], identity(slot))
        else:
            if sealed(current) != sealed(record['before']):
                raise PatchError('Physical original missing before restore')
            fingerprint = parent_view(packages, parent_path)
        if fingerprint != record['parent_fingerprint']:
            raise PatchError('Historical picker parent evidence changed')
        if marker is not None and marker != dict(record, state='patched'):
            raise PatchError('Installed activity-egress marker changed')
        status = 'already_patched' if marker is not None and current and current['sha256'] == record['after'] else 'partial_layer'
    snapshot = dict(record=record, current=current, parent_fingerprint=fingerprint,
        marker=identity(marker_path) if marker is not None else None,
        source=dict(sha256=sha256(source), signature=source_sig) if canonical else None)
    if private:
        snapshot['private'] = {name: identity(path) for name, path in (
            ('manifest', private), ('original', private.parent / 'original.js'), ('generated', private.parent / 'generated.js'))}
        snapshot['slot'] = identity(slot) if os.path.lexists(slot) else None
    result = dict(patch=PATCH_ID, status=status, applied=False, requires_stopped_host=True,
        fingerprint=sealed(snapshot), program_count=1, before={MODULE: BEFORE}, after={MODULE: record['after']})
    return result, packages, original, generated, record, snapshot


def patch(meme_dir, onebot_dir, picker_manifest, *, apply=False, host_stopped=False,
          expected_fingerprint=None, backup_dir=None):
    picker.flags(apply, host_stopped)
    result, packages, _, generated, _, _ = inspect(meme_dir, onebot_dir, picker_manifest)
    syntax({MODULE: generated})
    if not apply:
        if result['status'] == 'partial_layer': raise PatchError('Interrupted layer requires explicit restore')
        return result
    if host_stopped is not True or not backup_dir:
        raise PatchError('Stopped host and private backup required')
    with picker.locks(packages):
        result, packages, original, generated, record, snapshot = inspect(meme_dir, onebot_dir, picker_manifest)
        if result['fingerprint'] != expected_fingerprint or result['status'] == 'partial_layer':
            raise PatchError('Fresh complete preview fingerprint required')
        if result['status'] == 'already_patched': return result
        base = ordinary_path(backup_dir)
        if not Path(backup_dir).is_absolute() or any(base == path or path in base.parents for path in [REPO, *packages.values()]):
            raise PatchError('Private backup must be outside packages/source')
        base.mkdir(parents=True, exist_ok=True)
        backup = base / (PATCH_ID + '-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex)
        backup.mkdir(); manifest = backup / 'patch.json'
        record = dict(record, manifest=str(manifest), slot='.lanya-egress-activity-original-' + uuid.uuid4().hex + '.js')
        write_new(backup / 'original.js', original); write_new(backup / 'generated.js', generated)
        write_new(manifest, encoded(record))
        target = packages['onebot'] / MODULE
        slot = ordinary_path(target.parent / record['slot'])
        stage = target.with_name('.lanya-egress-activity-stage-' + uuid.uuid4().hex + '.js')
        changed = False
        try:
            write_new(stage, generated)
            if inspect(meme_dir, onebot_dir, picker_manifest)[0]['fingerprint'] != expected_fingerprint:
                raise PatchError('Inputs changed before replacement')
            if target.lstat().st_nlink != 1 or sealed(identity(target)) != sealed(record['before']):
                raise PatchError('Original module identity changed')
            write_new(backup / 'slot-intent.json', encoded(dict(slot=record['slot'], before=record['before'])))
            os.link(target, slot)
            inspect(meme_dir, onebot_dir, picker_manifest, manifest)
            if identity(target) != snapshot['current'] or read_regular(stage)[0] != generated:
                raise PatchError('Replacement inputs changed')
            write_new(backup / 'replace-intent.json', encoded(dict(before=record['before'], after=record['after'])))
            os.replace(stage, target); changed = True
            inspect(meme_dir, onebot_dir, picker_manifest, manifest)
            write_new(packages['onebot'] / MARKER, encoded(dict(record, state='patched')))
            if inspect(meme_dir, onebot_dir, picker_manifest)[0]['status'] != 'already_patched':
                raise PatchError('Final whole-layer readback failed')
            return dict(result, status='patched', applied=True, manifest=str(manifest))
        except BaseException:
            write_new(backup / 'interrupted.json', encoded(dict(patch=PATCH_ID, module_replaced=changed)))
            raise
        finally:
            if os.path.lexists(stage): ordinary_path(stage).unlink()


def restore(meme_dir, onebot_dir, manifest, *, apply=False, host_stopped=False, expected_fingerprint=None):
    picker.flags(apply, host_stopped)
    def preview():
        result, packages, original, _, record, snapshot = inspect(meme_dir, onebot_dir, manifest=manifest, canonical=False)
        change = sealed(snapshot['current']) != sealed(record['before'])
        remove, slot = snapshot['marker'] is not None, snapshot['slot'] is not None
        return dict(result, status='restore_preview' if change or remove or slot else 'already_restored',
                    restore_module=change, remove_marker=remove, remove_slot=slot), packages, record, snapshot
    result, packages, _, _ = preview()
    if not apply: return result
    if host_stopped is not True: raise PatchError('Stopped host required')
    with picker.locks(packages):
        result, packages, record, snapshot = preview()
        if result['fingerprint'] != expected_fingerprint: raise PatchError('Fresh restore fingerprint required')
        if result['status'] == 'already_restored': return result
        target, marker = packages['onebot'] / MODULE, packages['onebot'] / MARKER
        slot = ordinary_path(target.parent / record['slot'])
        if result['restore_module']:
            if (preview()[3]['current'] != snapshot['current'] or slot.lstat().st_nlink != 1 or
                    sealed(identity(slot)) != sealed(record['before'])):
                raise PatchError('Physical restore inputs changed')
            os.replace(slot, target)
        if result['remove_marker']:
            preview()
            if identity(marker) != snapshot['marker']: raise PatchError('Marker removal identity changed')
            marker.unlink()
        if os.path.lexists(slot):
            preview()
            if (slot.lstat().st_nlink != 2 or sealed(identity(slot)) != sealed(record['before']) or
                    sealed(identity(target)) != sealed(record['before'])):
                raise PatchError('Unused original slot changed')
            slot.unlink()
        if preview()[0]['status'] != 'already_restored' or parent_view(packages, record['parent_manifest']) != record['parent_fingerprint']:
            raise PatchError('Real parent restore readback failed')
        return dict(result, status='restored', applied=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--meme-dir', required=True); parser.add_argument('--onebot-dir', required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--picker-manifest'); mode.add_argument('--restore-manifest')
    parser.add_argument('--apply', action='store_true'); parser.add_argument('--host-stopped', action='store_true')
    parser.add_argument('--expected-fingerprint'); parser.add_argument('--backup-dir')
    args = parser.parse_args(argv)
    try:
        options = dict(apply=args.apply, host_stopped=args.host_stopped, expected_fingerprint=args.expected_fingerprint)
        result = restore(args.meme_dir, args.onebot_dir, args.restore_manifest, **options) if args.restore_manifest else patch(
            args.meme_dir, args.onebot_dir, args.picker_manifest, backup_dir=args.backup_dir, **options)
        print(json.dumps(result, ensure_ascii=True)); return 0
    except (OSError, ValueError, TypeError, RuntimeError, KeyError) as error:
        print(json.dumps(dict(patch=PATCH_ID, status='refused', error_class=type(error).__name__))); return 2


if __name__ == '__main__': raise SystemExit(main())
