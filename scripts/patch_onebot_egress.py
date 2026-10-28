"""Independent QQ egress layer after policy V1 + replay isolation V1.

Default preview never loads the app or policy state. Apply/restore require a
stopped-host assertion and fresh fingerprint. Restore egress before replay/V1.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import uuid

import patch_onebot_policy as policy
import patch_onebot_replay as replay
from patch_onebot_policy import PatchError, ordinary_path, read_regular, replace_once, sha256, write_new
from patch_onebot_replay import encoded, sealed, json_file, private_manifest, identity, package_lock, syntax

REPO = Path(__file__).resolve().parents[1]
PATCH_ID = 'lanya-onebot-egress-v1'
MARKER = '// LANYA_EGRESS_PATCH_V1\n'
INSTALLED_MARKER = 'lib/lanya-egress.patch.json'
TARGETS = ('lib/bridge.js', 'lib/outbound.js', 'lib/lanya-policy-adapter.js')
GENERATED = 'lib/lanya-qq-egress.js'


def transform(name, raw):
    text = raw.decode('utf-8')
    if MARKER.strip() in text:
        raise PatchError('Egress partial/already layered state requires its private manifest')
    if name == 'lib/lanya-policy-adapter.js':
        text = replace_once(text, "import { AsyncLocalStorage } from 'node:async_hooks'", """import { AsyncLocalStorage } from 'node:async_hooks'
import { NORMAL_STYLE, styleFromInbound, dispatchEgress, prepareAndSendReply,
  isPreparedReply, isVerbatimReply, isLiteralReply, rememberParameters, bindReplyOptions } from './lanya-qq-egress.js'""")
        text = replace_once(text, '    const observed = await this.policy.recordInbound({', """    // Copy trusted direct transport evidence before any storage/registry await.
    const egressStyle = styleFromInbound({ origin: 'live', text: inbound.text,
      replyId: inbound.replyId, quoted: !hasDirectEvidence, lanyaInjected: inbound.lanyaInjected })
    const observed = await this.policy.recordInbound({""")
        text = replace_once(text, "context.binding = Object.freeze({ ...observed, origin: 'live' })",
                            "context.binding = Object.freeze({ ...observed, origin: 'live', egressStyle })")
        text = replace_once(text, '  attachOutbound(outbound) {\n    const adapter = this', """  attachOutbound(outbound) {
    const adapter = this
    outbound.ctx.lanyaEgressIsPrepared = isPreparedReply
    outbound.ctx.lanyaEgressIsVerbatim = isVerbatimReply
    outbound.ctx.lanyaEgressIsLiteral = isLiteralReply
    outbound.ctx.lanyaEgressRemember = rememberParameters
    outbound.ctx.lanyaEgressPrepare = (scope, text, options) => {
      const envelope = adapter.context.getStore()?.envelope
      const pending = prepareAndSendReply(outbound, scope, text, options,
        envelope?.record?.binding?.egressStyle || NORMAL_STYLE)
      return pending.catch((error) => {
        if (error.partial) envelope?.record?.outcomes.push('partial')
        throw error
      })
    }""")
        text = replace_once(text, '  async guardConnectionCall(action, params, suppliedScope, transmit) {', """  async dispatchEgress(action, params, suppliedScope, transmit) {
    if (!communicates(action)) return this.guardConnectionCall(action, params, suppliedScope, () => transmit(action, params))
    const envelope = this.context.getStore()?.envelope
    try {
      return await dispatchEgress(action, params,
        envelope?.record?.binding?.egressStyle || NORMAL_STYLE,
        (nextAction, nextParams) => this.guardConnectionCall(nextAction, nextParams, suppliedScope,
          () => transmit(nextAction, nextParams)))
    } catch (error) {
      if (error.partial) envelope?.record?.outcomes.push('partial')
      throw error
    }
  }

  async guardConnectionCall(action, params, suppliedScope, transmit) {""")
        text = replace_once(text, 'original.call(this, scope, body, { ...options, lanyaEnvelope: envelope })',
                            'original.call(this, scope, body, bindReplyOptions(options, { lanyaEnvelope: envelope }))')
        text = replace_once(text, "const status = record.outcomes.includes('unknown') ? 'unknown'",
                            "const status = record.outcomes.some((item) => ['unknown', 'partial'].includes(item)) ? 'unknown'")
    elif name == 'lib/bridge.js':
        text = replace_once(text, 'return this.lanyaPolicy.guardConnectionCall(action, params, chatId, () => {',
                            'return this.lanyaPolicy.dispatchEgress(action, params, chatId, (action, params) => {')
    elif name == 'lib/outbound.js':
        text = replace_once(text, '    sendToChat(chatId, text, options = {}) {', """    sendToChat(chatId, text, options = {}) {
        if (this.ctx.lanyaEgressPrepare && !this.ctx.lanyaEgressIsPrepared(options)) {
            return this.ctx.lanyaEgressPrepare(chatId, text, options);
        }""")
        # Keep the reviewed local guard implementation as compatibility evidence;
        # the new shared normalizer owns prepared text, including code/RP data.
        if '// PATCHED (local): one gate' in text:
            text = replace_once(text, '        // PATCHED (local): one gate',
                                '        if (!this.ctx.lanyaEgressIsPrepared?.(options)) {\n        // PATCHED (local): one gate')
            text = replace_once(text, '        // Break a long reply into several short messages',
                                '        }\n        // Break a long reply into several short messages')
            text = replace_once(text, "if (typeof text === 'string' && options.split !== false) {",
                                "if (!this.ctx.lanyaEgressIsPrepared?.(options) && typeof text === 'string' && options.split !== false) {")
        if '                const plain = stripMarkdown(body);' in text:
            text = replace_once(text, '                const plain = stripMarkdown(body);',
                                '                const plain = this.ctx.lanyaEgressIsVerbatim?.(options) ? body : stripMarkdown(body);')
        if "            const { body, nodes } = extractForwardBlocks(text, '助手');" in text:
            text = replace_once(text, "            const { body, nodes } = extractForwardBlocks(text, '助手');",
                                "            const { body, nodes } = this.ctx.lanyaEgressIsLiteral?.(options)\n"
                                "                ? { body: text, nodes: [] } : extractForwardBlocks(text, '助手');")
        if '            let sentCard = false;' in text:
            text = replace_once(text, '            let sentCard = false;',
                                '            let sentCard = false;\n            let cardTransportStarted = false;')
            text = replace_once(text, "                        const id = await this.sendMsg(chatId, [{ type: 'image', data: { file: b64 } }], options);",
                                "                        cardTransportStarted = true;\n                        const id = await this.sendMsg(chatId, [{ type: 'image', data: { file: b64 } }], options);")
            text = replace_once(text, "                    this.ctx.log('warn', 't2i render failed, falling back to text: ' + describeError(error));",
                                "                    if (cardTransportStarted) throw error;\n                    this.ctx.log('warn', 't2i render failed, falling back to text: ' + describeError(error));")
        # Forward nodes are shaped at the guarded terminal; truncating upstream
        # would lose the tail before the shared normalizer can split it.
        if 'node.content.slice(0, 500)' in text:
            text = replace_once(text, 'node.content.slice(0, 500)', 'node.content')
        # The already-reviewed old policy fixture has the same terminal seam
        # without Markdown shaping. Its exact parent hash is checked separately.
        terminal = "        const data = await this.ctx.call('send_msg', params, chatId);"
        if terminal not in text:
            terminal = "        const result = await this.ctx.call('send_msg', params, chatId);"
        text = replace_once(text, terminal, '        this.ctx.lanyaEgressRemember?.(params, options);\n' + terminal)
    else:
        raise PatchError('Unknown egress target')
    return (MARKER + text).encode('utf-8')


def parents(plugin, replay_manifest):
    rpath = private_manifest(replay_manifest, plugin)
    rrecord, rmeta, _ = json_file(rpath)
    ppath = private_manifest(rrecord.get('parent', {}).get('manifest', ''), plugin)
    base, pmeta, _ = json_file(ppath)
    fields = {'patch', 'plugin', 'before', 'after', 'backup', 'state', 'package_sha256'}
    if (set(base) != fields or base.get('patch') != policy.PATCH_ID or base.get('state') != 'prepared' or
            ordinary_path(base.get('plugin', '')) != plugin or ordinary_path(base.get('backup', '')) != ppath.parent or
            set(base.get('before', {})) != set(policy.TARGETS) or set(base.get('after', {})) != set(policy.TARGETS + policy.GENERATED)):
        raise PatchError('Unknown policy V1 parent')
    package, pkgmeta, _ = json_file(plugin / 'package.json')
    if (package.get('name'), package.get('version'), package.get('type')) != ('dsh-onebot-qq', '0.8.1', 'module') or pkgmeta['sha256'] != base['package_sha256']:
        raise PatchError('Unchanged reviewed OneBot 0.8.1 package required')
    watched = {str(ppath): pmeta, str(rpath): rmeta, str(plugin / 'package.json'): pkgmeta}
    for name in policy.TARGETS:
        raw, sig = read_regular(ppath.parent / name)
        if sha256(raw) != base['before'][name] or sha256(raw) not in policy.KNOWN[name] or sha256(policy.transform(name, raw)) != base['after'][name]:
            raise PatchError('Policy V1 private original/transform mismatch')
        watched[str(ppath.parent / name)] = dict(sha256=sha256(raw), signature=sig)
    _, pm, praw = json_file(plugin / 'lib/lanya-policy.patch.json')
    if praw != (json.dumps(dict(base, state='patched'), indent=2) + '\n').encode():
        raise PatchError('Policy V1 marker changed')
    watched[str(plugin / 'lib/lanya-policy.patch.json')] = pm
    parent = dict(patch=policy.PATCH_ID, manifest=str(ppath), manifest_sha256=pmeta['sha256'], installed_marker_sha256=sha256(praw))
    replay_record, _, _, extra = replay.validate_record(plugin, rpath, base, parent)
    watched.update(extra)
    _, rm, rraw = json_file(plugin / replay.INSTALLED_MARKER)
    if rraw != encoded(dict(replay_record, state='patched')):
        raise PatchError('Replay isolation marker changed')
    watched[str(plugin / replay.INSTALLED_MARKER)] = rm
    if any(base['after'][name] != digest for name, digest in replay.canonical_modules().items()):
        raise PatchError('Policy canonical module contract changed')
    expected = {'lib/' + name: digest for name, digest in base['after'].items()}
    expected.update(replay_record['after'])
    installed = {}
    for name, digest in expected.items():
        raw, sig = read_regular(plugin / name)
        if name not in TARGETS and sha256(raw) != digest:
            raise PatchError('Unowned parent output changed: ' + name)
        installed[name] = (raw, sig)
        watched[str(plugin / name)] = dict(sha256=sha256(raw), signature=sig)
    chain = dict(policy_manifest=str(ppath), policy_manifest_sha256=pmeta['sha256'], policy_marker_sha256=sha256(praw),
                 replay_manifest=str(rpath), replay_manifest_sha256=rmeta['sha256'], replay_marker_sha256=sha256(rraw))
    return base, expected, chain, watched, installed


def inspect(plugin_dir, replay_manifest, restore_manifest=None):
    plugin = ordinary_path(plugin_dir)
    base, expected, parent, watched, installed = parents(plugin, replay_manifest)
    source, sig = read_regular(REPO / 'plugin/lib/qq-egress.js')
    watched[str(REPO / 'plugin/lib/qq-egress.js')] = dict(sha256=sha256(source), signature=sig)
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
    if restore_manifest is not None:
        path = private_manifest(restore_manifest, plugin)
        record, meta, _ = json_file(path)
        fields = {'version', 'patch', 'plugin', 'before', 'after', 'backup', 'state', 'package_sha256', 'parent'}
        if (set(record) != fields or record.get('version') != 1 or record.get('patch') != PATCH_ID or record.get('state') != 'prepared' or
                ordinary_path(record.get('plugin', '')) != plugin or ordinary_path(record.get('backup', '')) != path.parent or
                record.get('package_sha256') != base['package_sha256'] or record.get('parent') != parent or
                set(record.get('before', {})) != set(TARGETS) or set(record.get('after', {})) != set((*TARGETS, GENERATED))):
            raise PatchError('Unknown egress private manifest')
        watched[str(path)] = meta
        originals = {}
        for name in TARGETS:
            raw, sig = read_regular(path.parent / name)
            if sha256(raw) != expected[name] or sha256(raw) != record['before'][name]:
                raise PatchError('Egress original hash mismatch')
            originals[name] = raw, sig
            watched[str(path.parent / name)] = dict(sha256=sha256(raw), signature=sig)
        if marker_raw is not None and marker_raw != encoded(dict(record, state='patched')):
            raise PatchError('Egress installed marker differs from private manifest')
    else:
        originals = {name: installed[name] for name in TARGETS}
        if any(sha256(raw) != expected[name] for name, (raw, _) in originals.items()):
            raise PatchError('Owned target drift; interrupted layers require explicit restore manifest')
    outputs = {name: transform(name, raw) for name, (raw, _) in originals.items()}
    outputs[GENERATED] = source
    before = {name: sha256(raw) for name, (raw, _) in originals.items()}
    after = {name: sha256(raw) for name, raw in outputs.items()}
    if record and (record['before'] != before or record['after'] != after):
        raise PatchError('Egress source/transform differs from recorded layer')
    for name in TARGETS:
        if sha256(installed[name][0]) not in (before[name], after[name]):
            raise PatchError('Later/unknown egress target change')
    generated = ordinary_path(plugin / GENERATED)
    if os.path.lexists(generated):
        meta = identity(generated)
        if record is None or meta['sha256'] != after[GENERATED]:
            raise PatchError('Unknown/later generated egress module')
        watched[str(generated)] = meta
    else:
        watched[str(generated)] = None
    complete = marker_raw is not None and all(watched[str(plugin / name)] is not None and watched[str(plugin / name)]['sha256'] == digest for name, digest in after.items())
    status = 'already_patched' if complete else 'partial_layer' if record else 'patch_needed'
    snapshot = dict(plugin=str(plugin), parent=parent, watched=watched, before=before, after=after)
    result = dict(patch=PATCH_ID, status=status, applied=False, requires_stopped_host=True, fingerprint=sealed(snapshot), before=before, after=after, parent=parent)
    return result, plugin, originals, outputs, snapshot, record


def patch(plugin_dir, replay_manifest, *, apply=False, host_stopped=False, expected_fingerprint=None, backup_dir=None):
    if type(apply) is not bool or type(host_stopped) is not bool:
        raise PatchError('Apply and stopped-host assertions must be booleans')
    if not apply:
        result, _, _, outputs, _, _ = inspect(plugin_dir, replay_manifest)
        if result['status'] == 'partial_layer':
            raise PatchError('Use explicit egress restore after interruption')
        syntax(outputs)
        return result
    if host_stopped is not True or not backup_dir:
        raise PatchError('Apply requires stopped host, private backup and fresh fingerprint')
    with package_lock(plugin_dir):
        result, plugin, originals, outputs, snapshot, _ = inspect(plugin_dir, replay_manifest)
        if result['fingerprint'] != expected_fingerprint or result['status'] == 'partial_layer':
            raise PatchError('Unchanged egress preview fingerprint required')
        if result['status'] == 'already_patched':
            return result
        syntax(outputs)
        base = ordinary_path(backup_dir)
        if base == REPO or REPO in base.parents or base == plugin or plugin in base.parents:
            raise PatchError('Private backup must be outside public source/installed package')
        base.mkdir(parents=True, exist_ok=True)
        backup = base / (PATCH_ID + '-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex)
        backup.mkdir(); (backup / 'lib').mkdir()
        for name, (raw, _) in originals.items():
            write_new(backup / name, raw)
        record = dict(version=1, patch=PATCH_ID, plugin=str(plugin), before=result['before'], after=result['after'], backup=str(backup),
                      state='prepared', package_sha256=snapshot['watched'][str(plugin / 'package.json')]['sha256'], parent=result['parent'])
        manifest = backup / 'patch.json'
        write_new(manifest, encoded(record))
        stages, changed = {}, []
        try:
            for name, raw in outputs.items():
                stage = plugin / 'lib' / ('.lanya-egress-stage-' + uuid.uuid4().hex + '.js')
                write_new(stage, raw); stages[name] = stage
            if inspect(plugin, replay_manifest)[0]['fingerprint'] != result['fingerprint']:
                raise PatchError('Egress inputs changed before first replacement')
            for name in outputs:
                inspect(plugin, replay_manifest, manifest)
                current = identity(plugin / name) if os.path.lexists(plugin / name) else None
                if current != snapshot['watched'][str(plugin / name)] or read_regular(stages[name])[0] != outputs[name]:
                    raise PatchError('Egress target/staging identity changed')
                os.replace(stages[name], plugin / name); stages.pop(name); changed.append(name)
            write_new(plugin / INSTALLED_MARKER, encoded(dict(record, state='patched')))
            if inspect(plugin, replay_manifest)[0]['status'] != 'already_patched':
                raise PatchError('Egress final full readback failed')
            return dict(result, status='patched', state='patched', applied=True, backup=str(backup), manifest=str(manifest))
        except Exception:
            write_new(backup / 'interrupted.json', encoded(dict(patch=PATCH_ID, changed=changed)))
            raise
        finally:
            for stage in stages.values():
                if os.path.lexists(stage): ordinary_path(stage).unlink()


def restore(plugin_dir, manifest, *, apply=False, host_stopped=False, expected_fingerprint=None):
    if type(apply) is not bool or type(host_stopped) is not bool:
        raise PatchError('Apply and stopped-host assertions must be booleans')
    plugin = ordinary_path(plugin_dir)
    manifest = private_manifest(manifest, plugin)
    record = json_file(manifest)[0]
    parent = record.get('parent', {}).get('replay_manifest')
    if not isinstance(parent, str): raise PatchError('Explicit replay parent required')
    def preview():
        result, root, originals, _, snapshot, layer = inspect(plugin, parent, manifest)
        names = [name for name in TARGETS if snapshot['watched'][str(root / name)]['sha256'] != layer['before'][name]]
        remove = [name for name in (GENERATED, INSTALLED_MARKER) if snapshot['watched'][str(root / name)] is not None]
        return dict(result, status='restore_preview' if names or remove else 'already_restored', restore=names, remove=remove), root, originals, snapshot
    if not apply: return preview()[0]
    if host_stopped is not True: raise PatchError('Restore requires stopped host and fresh fingerprint')
    with package_lock(plugin):
        result, root, originals, snapshot = preview()
        if result['fingerprint'] != expected_fingerprint: raise PatchError('Unchanged restore preview fingerprint required')
        stages = {}
        try:
            for name in result['restore']:
                stage = root / 'lib' / ('.lanya-egress-restore-' + uuid.uuid4().hex + '.js')
                write_new(stage, originals[name][0]); stages[name] = stage
            if preview()[0]['fingerprint'] != result['fingerprint']: raise PatchError('Restore inputs changed')
            for name in result['restore']:
                inspect(root, parent, manifest)
                if identity(root / name) != snapshot['watched'][str(root / name)] or read_regular(stages[name])[0] != originals[name][0]:
                    raise PatchError('Restore target/staged original changed')
                os.replace(stages[name], root / name); stages.pop(name)
            for name in result['remove']:
                inspect(root, parent, manifest)
                if identity(root / name) != snapshot['watched'][str(root / name)]: raise PatchError('Generated/marker removal identity changed')
                ordinary_path(root / name).unlink()
            final = preview()[0]
            if final['status'] != 'already_restored' or final['restore'] or final['remove']:
                raise PatchError('Restore final whole readback failed')
            return dict(result, status='restored', applied=True)
        finally:
            for stage in stages.values():
                if os.path.lexists(stage): ordinary_path(stage).unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin-dir', required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--replay-manifest')
    mode.add_argument('--restore-manifest')
    parser.add_argument('--apply', action='store_true'); parser.add_argument('--host-stopped', action='store_true')
    parser.add_argument('--expected-fingerprint'); parser.add_argument('--backup-dir')
    args = parser.parse_args(argv)
    try:
        kwargs = dict(apply=args.apply, host_stopped=args.host_stopped, expected_fingerprint=args.expected_fingerprint)
        result = restore(args.plugin_dir, args.restore_manifest, **kwargs) if args.restore_manifest else patch(args.plugin_dir, args.replay_manifest, backup_dir=args.backup_dir, **kwargs)
        print(json.dumps(result, ensure_ascii=True)); return 0
    except (PatchError, OSError, ValueError, TypeError, KeyError) as error:
        import sys
        print(json.dumps({'error': str(error) if isinstance(error, PatchError) else type(error).__name__}), file=sys.stderr); return 2


if __name__ == '__main__':
    raise SystemExit(main())
