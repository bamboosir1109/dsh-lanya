"""Precisely adapt reviewed dsh-meme selection to the bound OneBot channel.

Preview only by default. No images, index/settings, policy or business DB is
opened. Apply/restore require stopped host, fresh fingerprint and private
originals. Restore this layer before the historical OneBot layers.
"""
import argparse
from contextlib import contextmanager, ExitStack
import datetime
import json
import os
from pathlib import Path
import re
import sys
import uuid

import patch_autonomy_continuity as continuity
from patch_onebot_policy import PatchError, ordinary_path, read_regular, replace_once, sha256, write_new
from patch_onebot_replay import encoded, sealed, json_file, identity, package_lock, syntax

REPO = Path(__file__).resolve().parents[1]
PATCH_ID = 'lanya-meme-qq-picker-v1'
MARKER = '.lanya-qq-picker.patch.json'
MODULE = 'lanya-qq-meme.mjs'
SOURCE = REPO / 'plugin/lib/meme-qq-picker.mjs'
KNOWN = {'index.js': '38b766f3a8838fcfcc736e8874da2018febda3774ae5aa81ddd71a86f5430359',
         'memes.js': '2a036594131ad5667b6539a40f46f5d8a200ab0964ef16693e05b372f1cda1b4',
         'bridge.js': '6f692aef1d9aec06ca77a88c76e2cfe3183ad8c37ace7cd2d9dcc9f104399d3d'}
KEYS = ('meme:index.js', 'meme:memes.js', 'meme:' + MODULE, 'onebot:lib/bridge.js')

RESOLVER = """    resolveIndexed(stored) {
      if (typeof stored !== 'string' || !stored) throw new Error('Invalid indexed meme');
      const ids = enabledIds();
      const active = activePackId();
      let id = active;
      let local = stored;
      const slash = stored.indexOf('/');
      const first = slash > 0 ? stored.slice(0, slash) : '';
      const prefixed = first && packRows().find((p) => p.id === first && p.id !== active);
      if (prefixed) { id = first; local = stored.slice(slash + 1); }
      if (!ids.includes(id)) throw new Error('Indexed meme pack disabled');
      const pack = packRows().find((p) => p.id === id);
      if (!pack) throw new Error('Indexed meme pack unavailable');
      let store;
      try {
        store = new MemesStore(pack.path);
        if (!store.db.prepare('SELECT path FROM memes WHERE path = ?').get(local))
          throw new Error('Meme is not indexed');
        return { root: store.root, relativePath: local, file: resolve(store.root, local) };
      } finally { if (store) store.close(); }
    },
"""

PROVIDER = """        // Owner-bound indexed meme capability; no exposed ticket or destination.
        // A dsh agent scope is an event/fiber scope, not automatically a Cordis
        // service scope. Isolate this one service on the EXISTING agent context.
        const isolateKey = Symbol.for('cordis.isolate');
        const mediaScope = Object.create(agentCtx[isolateKey] ?? null);
        mediaScope.lanyaQqMemeMedia = Symbol('lanyaQqMemeMedia');
        Object.defineProperty(agentCtx, isolateKey, {
            value: mediaScope, writable: true, configurable: true, enumerable: true,
        });
        agentCtx.reflect.provide('lanyaQqMemeMedia', Object.freeze({
            forExecution: (exec) => {
                const record = this.lanyaPolicy.captureExec(exec);
                return Object.freeze({
                    format: 'lanya-qq-meme-media/v1',
                    selectionPolicy: this.lanyaPolicy.memeSelectionPolicy?.(exec),
                    sendCandidate: (candidate, textFollows) => this.lanyaPolicy.sendSelectedMeme(candidate, textFollows, exec),
                    mediaDir: this.deps.config.mediaDir,
                    maxBytes: this.deps.config.maxImageBytes,
                    assertCurrent: () => {
                        if (this.lanyaPolicy.captureExec(exec) !== record)
                            throw new Error('QQ meme selection is no longer current');
                    },
                });
            },
        }));
"""


def extend_tagged_provider(raw):
    newline = '\r\n' if b'\r\n' in raw else '\n'
    text = raw.decode('utf-8').replace('\r\n', '\n')
    anchor = '                    selectionPolicy: this.lanyaPolicy.memeSelectionPolicy?.(exec),\n'
    addition = '                    sendCandidate: (candidate, textFollows) => this.lanyaPolicy.sendSelectedMeme(candidate, textFollows, exec),\n'
    if text.count(anchor) != 1 or text.count("agentCtx.reflect.provide('lanyaQqMemeMedia'") != 1:
        raise PatchError('Unreviewed indexed meme provider')
    if addition in text:
        if text.count(addition) != 1 or anchor + addition not in text: raise PatchError('Changed indexed meme provider')
        return raw
    text = text.replace('// Selection capability only: no ticket, send method, or broad home root.',
                        '// Owner-bound indexed meme capability; no exposed ticket or destination.')
    return text.replace(anchor, anchor + addition, 1).replace('\n', newline).encode('utf-8')


def extend_tagged_registration(raw):
    newline = '\r\n' if b'\r\n' in raw else '\n'
    text = raw.decode('utf-8').replace('\r\n', '\n')
    old = "import { getQqCapability, selectQqCandidates } from './lanya-qq-meme.mjs'"
    new = "import { getQqCapability, selectQqCandidates, registerTaggedMemeTool } from './lanya-qq-meme.mjs'"
    anchor = 'export function registerSendMemeTool(ctx, memes, sendImage, urlPrefix = null) {\n'
    addition = '  registerTaggedMemeTool(ctx, memes, defineTool)\n'
    if text.count(anchor) != 1: raise PatchError('Unreviewed native meme registration')
    if new in text and anchor + addition in text: return raw
    if text.count(old) != 1 or addition in text: raise PatchError('Changed native meme registration')
    return text.replace(old, new, 1).replace(anchor, anchor + addition, 1).replace('\n', newline).encode('utf-8')


def extend_current_provider(raw):
    """Add the reviewed live selection fence to an already patched bridge."""
    newline = '\r\n' if b'\r\n' in raw else '\n'
    text = raw.decode('utf-8').replace('\r\n', '\n')
    marker = '// LANYA_BOUND_MEME_SELECTION_V1\n'
    anchor = "                    format: 'lanya-qq-meme-media/v1',\n"
    addition = '                    selectionPolicy: this.lanyaPolicy.memeSelectionPolicy?.(exec),\n'
    required = ["agentCtx.reflect.provide('lanyaQqMemeMedia'", 'const record = this.lanyaPolicy.captureExec(exec);',
                'if (this.lanyaPolicy.captureExec(exec) !== record)']
    if any(text.count(part) != 1 for part in required) or text.count(anchor) != 1:
        raise PatchError('Unreviewed meme capability provider')
    if text.startswith(marker):
        if text.count(addition) != 1 or anchor + addition not in text:
            raise PatchError('Changed live meme capability provider')
        return raw
    if addition in text:
        raise PatchError('Live selection provider already present without marker')
    return (marker + text.replace(anchor, anchor + addition, 1)).replace('\n', newline).encode('utf-8')


def transform(name, raw):
    if sha256(raw) != KNOWN[name]:
        raise PatchError('Unreviewed native program')
    newline = '\r\n' if b'\r\n' in raw else '\n'
    text = raw.decode('utf-8').replace('\r\n', '\n')
    if name == 'index.js':
        anchor = '    resolveStored(stored) {\n'
        text = replace_once(text, anchor, RESOLVER + anchor)
        text = replace_once(text,
            "'- 发图先 search 拿候选,[表情: 描述] 里的描述必须抄候选原文,自己编的描述前端配不上图;\\n'",
            "'- 发图先 search 拿候选。若返回 mode=qq_selection，按真实path用qq_send_image发送，绝不写占位文字；只有Web结果才抄候选占位描述;\\n'")
        text = replace_once(text,"'- 发完保持简短,让图自己说话,不啰嗦不复述。'",
                            "'- 正文长短按内容，需要就完整说，不设固定句数或总条数。'")
    elif name == 'memes.js':
        anchor = "import { defineTool } from '@deepseek-ai/dsh-tools'"
        text = replace_once(text, anchor, anchor + "\nimport { getQqCapability, selectQqCandidates } from './lanya-qq-meme.mjs'")
        text = replace_once(text, "    description: '发一张表情包。两种搜法:",
            "    description: 'QQ结果mode=qq_selection只选真实图片，selected不等于送达。空caption或requires_visual_review=true先read_image({file_path:候选path})；read只读文本。已有当前有效reply票就复用，不再次qq_decide；尚未决策则选图看图后qq_decide(reply)，再qq_send_image({sources:[候选path]})。已作非reply决定时不改票、不发图。禁止QQ占位文字。其它Web行为保持原样。两种搜法:")
        text = replace_once(text,"? '挑中后把 [表情: 描述] 原样写进回复",
                            "? '仅实际Web结果且mode不是qq_selection时，挑中后把 [表情: 描述] 原样写进回复")
        text = replace_once(text,"'气氛对了就主动发;发完短接,让图自己说话。'",
                            "'气氛对了可主动选图；正文长短按内容，需要就完整说，不设固定句数或总条数。'")
        text = replace_once(text, '    execute(args) {\n',
            "    execute(args, exec) {\n      const qqCap = getQqCapability(exec)\n      if (qqCap) return selectQqCandidates(memes, args, exec, qqCap)\n")
    else:
        text = replace_once(text, '    installChannelScope(agentCtx) {\n',
                            '    installChannelScope(agentCtx) {\n' + PROVIDER)
    result = text.replace('\n', newline).encode('utf-8')
    return extend_tagged_registration(result) if name == 'memes.js' else result


def roots(meme, onebot):
    result = {}
    for role, value, expected in [('meme', meme, ('dsh-meme', '0.1.47')),
                                   ('onebot', onebot, ('dsh-onebot-qq', '0.8.1'))]:
        if not Path(value).is_absolute():
            raise PatchError('Explicit absolute package roots required')
        path = ordinary_path(value)
        package, _, _ = json_file(path / 'package.json')
        if (package.get('name'), package.get('version')) != expected or package.get('type') != 'module':
            raise PatchError('Unsupported package/version')
        result[role] = path
    if result['meme'] == result['onebot'] or any(result[a] in result[b].parents for a,b in [('meme','onebot'),('onebot','meme')]):
        raise PatchError('Distinct package roots required')
    return result


def target(packages, key):
    role, name = key.split(':', 1)
    if key not in KEYS:
        raise PatchError('Unknown patch target')
    return ordinary_path(packages[role] / name)


@contextmanager
def historical_bridge(path, raw, before):
    """Only the owned bridge is substituted, with its original native identity.

    All old manifests/markers/dependencies are still read from their actual
    paths by the original historical inspector. Never virtualize a marker or
    accept current unknown bytes via this view.
    """
    saved = []
    for name, module in tuple(sys.modules.items()):
        if not (name.startswith('patch_onebot_') or name == 'patch_autonomy_continuity'):
            continue
        for attribute in ('read_regular', 'identity'):
            original = getattr(module, attribute, None)
            if not callable(original):
                continue
            def proxy(value, original=original, attribute=attribute):
                if Path(value) == path:
                    return (raw, tuple(before['signature'])) if attribute == 'read_regular' else before
                return original(value)
            saved.append((module, attribute, original))
            setattr(module, attribute, proxy)
    try:
        yield
    finally:
        for module, attribute, original in reversed(saved):
            setattr(module, attribute, original)


def parent_view(packages, original=None, before=None):
    if original is None:
        view = continuity.inspect(packages['onebot'])
    else:
        with historical_bridge(target(packages, 'onebot:lib/bridge.js'), original, before):
            view = continuity.inspect(packages['onebot'])
    if view[0]['status'] != 'already_patched':
        raise PatchError('Complete continuity parent required')
    return view[0]['fingerprint']


def manifest_path(value, packages):
    if not Path(value).is_absolute():
        raise PatchError('Explicit private manifest required')
    path = ordinary_path(value)
    if path.name != 'patch.json' or any(path == root or root in path.parents for root in [REPO, *packages.values()]):
        raise PatchError('Private manifest must remain outside source/packages')
    return path


def inspect(meme_dir, onebot_dir, manifest=None, *, canonical=True):
    packages = roots(meme_dir, onebot_dir)
    source, source_sig = read_regular(SOURCE) if canonical else (None,None)
    marker_path = packages['meme'] / MARKER
    marker = json_file(marker_path)[0] if os.path.lexists(marker_path) else None
    if marker is not None and manifest is None:
        manifest = marker.get('manifest', '')
    current = {key: identity(target(packages, key)) if os.path.lexists(target(packages, key)) else None for key in KEYS}
    originals, generated, record = {}, {}, None
    if manifest is None:
        if source is None:raise PatchError('Private restore manifest required')
        before = dict(current)
        if before['meme:' + MODULE] is not None or marker is not None:
            raise PatchError('Unowned installed helper/marker')
        for key in KEYS:
            if key == 'meme:' + MODULE:
                generated[key] = source
            else:
                name = key.split(':')[-1].split('/')[-1]
                originals[key] = read_regular(target(packages, key))[0]
                generated[key] = transform(name, originals[key])
        parent = parent_view(packages)
        record = dict(version=1, patch=PATCH_ID, state='prepared', roots={k:str(v) for k,v in packages.items()},
                      before=before, after={key:sha256(raw) for key,raw in generated.items()}, parent_fingerprint=parent,
                      package_hashes={key:identity(path/'package.json')['sha256'] for key,path in packages.items()},
                      source_sha256=sha256(source))
        status = 'patch_needed'
        private = None
    else:
        private = manifest_path(manifest, packages)
        record, _, _ = json_file(private)
        fields = {'version','patch','state','roots','before','after','parent_fingerprint','package_hashes','source_sha256','manifest','bridge_slot'}
        if (set(record) != fields or type(record.get('version')) is not int or record['version'] != 1 or
                record.get('patch') != PATCH_ID or record.get('state') != 'prepared' or record.get('manifest') != str(private) or
                record.get('roots') != {k:str(v) for k,v in packages.items()} or set(record.get('before',{})) != set(KEYS) or
                set(record.get('after',{})) != set(KEYS) or (canonical and record.get('source_sha256') != sha256(source)) or
                record.get('after',{}).get('meme:'+MODULE) != record.get('source_sha256') or
                record['before']['meme:' + MODULE] is not None):
            raise PatchError('Unknown picker manifest or newer source')
        before = record['before']
        bridge_key = 'onebot:lib/bridge.js'
        if (current[bridge_key] and current[bridge_key]['sha256'] == before[bridge_key]['sha256'] and
                sealed(current[bridge_key]) != sealed(before[bridge_key])):
            raise PatchError('Old bridge bytes with an unknown native identity')
        for key in KEYS:
            saved = private.parent / 'generated' / key.replace(':','/')
            generated[key] = read_regular(saved)[0]
            if sha256(generated[key]) != record['after'][key]:
                raise PatchError('Private generated copy changed')
            if before[key] is not None:
                originals[key] = read_regular(private.parent / 'original' / key.replace(':','/'))[0]
                name = key.split(':')[-1].split('/')[-1]
                if sha256(originals[key]) != before[key]['sha256'] or transform(name,originals[key]) != generated[key]:
                    raise PatchError('Private original/transform changed')
            elif canonical and generated[key] != source:
                raise PatchError('Private helper copy changed')
            allowed = [before[key]['sha256'] if before[key] else None, record['after'][key]]
            if (current[key]['sha256'] if current[key] else None) not in allowed:
                raise PatchError('Unknown later program change')
        if marker is not None and marker != dict(record,state='patched'):
            raise PatchError('Installed picker marker changed')
        slot_name = record.get('bridge_slot')
        if not isinstance(slot_name,str) or not re.fullmatch(r'\.lanya-meme-original-[0-9a-f]{32}\.js',slot_name):
            raise PatchError('Invalid physical bridge original slot')
        slot = ordinary_path(packages['onebot']/'lib'/slot_name)
        bridge = target(packages,'onebot:lib/bridge.js')
        if os.path.lexists(slot):
            if sealed(identity(slot)) != sealed(before['onebot:lib/bridge.js']):
                raise PatchError('Physical bridge original identity changed')
            if slot.lstat().st_nlink != (2 if sealed(current['onebot:lib/bridge.js'])==sealed(before['onebot:lib/bridge.js']) else 1):
                raise PatchError('Physical bridge original link count changed')
            parent = parent_view(packages,read_regular(slot)[0],identity(slot))
        else:
            if sealed(current['onebot:lib/bridge.js']) != sealed(before['onebot:lib/bridge.js']):
                raise PatchError('Physical bridge original missing before restoration')
            parent = parent_view(packages)
        if parent != record['parent_fingerprint']:
            raise PatchError('Historical parent evidence changed')
        status = 'already_patched' if marker is not None and all(current[k] and current[k]['sha256']==record['after'][k] for k in KEYS) else 'partial_layer'
    if record['package_hashes'] != {key:identity(path/'package.json')['sha256'] for key,path in packages.items()}:
        raise PatchError('Package metadata changed')
    snapshot = dict(record=record,current=current,marker=identity(marker_path) if marker is not None else None,
                    source=dict(sha256=sha256(source),signature=source_sig) if canonical else None)
    # Copies themselves are also fingerprinted: replacement with equal bytes is
    # a fresh input, never silently accepted between preview and apply.
    if private:
        snapshot['private'] = {str(p):identity(p) for p in [private, *sorted((private.parent/'generated').rglob('*')), *sorted((private.parent/'original').rglob('*'))] if p.is_file()}
        snapshot['bridge_slot'] = identity(slot) if os.path.lexists(slot) else None
    result = dict(patch=PATCH_ID,status=status,applied=False,requires_stopped_host=True,fingerprint=sealed(snapshot),
                  program_count=4,before={k:v['sha256'] if v else None for k,v in before.items()},after=record['after'])
    return result,packages,originals,generated,record,snapshot


def flags(apply, host_stopped):
    if type(apply) is not bool or type(host_stopped) is not bool:
        raise PatchError('Boolean apply/stopped-host assertions required')


def locks(packages):
    stack = ExitStack()
    try:
        for path in sorted(packages.values()):
            stack.enter_context(package_lock(path))
    except BaseException:
        stack.close(); raise
    return stack


def patch(meme_dir,onebot_dir,*,apply=False,host_stopped=False,expected_fingerprint=None,backup_dir=None):
    flags(apply,host_stopped)
    result,packages,_,generated,_,_ = inspect(meme_dir,onebot_dir)
    syntax({key.replace(':','/'):raw for key,raw in generated.items()})
    if not apply:
        if result['status']=='partial_layer': raise PatchError('Interrupted layer requires restore')
        return result
    if host_stopped is not True or not backup_dir: raise PatchError('Stopped host/private backup required')
    with locks(packages):
        result,packages,originals,generated,record,snapshot = inspect(meme_dir,onebot_dir)
        if result['fingerprint'] != expected_fingerprint or result['status']=='partial_layer': raise PatchError('Fresh picker fingerprint required')
        if result['status']=='already_patched': return result
        base = ordinary_path(backup_dir)
        if not Path(backup_dir).is_absolute() or any(base==r or r in base.parents for r in [REPO,*packages.values()]):
            raise PatchError('Private backup required outside source/packages')
        base.mkdir(parents=True,exist_ok=True)
        backup=base/(PATCH_ID+'-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex)
        backup.mkdir();manifest=backup/'patch.json'
        record=dict(record,manifest=str(manifest),bridge_slot='.lanya-meme-original-'+uuid.uuid4().hex+'.js')
        for category,rows in [('original',originals),('generated',generated)]:
            for key,raw in rows.items():
                dest=backup/category/key.replace(':','/');dest.parent.mkdir(parents=True,exist_ok=True);write_new(dest,raw)
        write_new(manifest,encoded(record))
        stages={};changed=[]
        try:
            for key,raw in generated.items():
                stage=target(packages,key).with_name('.lanya-picker-'+uuid.uuid4().hex+'.mjs');write_new(stage,raw);stages[key]=stage
            if inspect(meme_dir,onebot_dir)[0]['fingerprint'] != expected_fingerprint: raise PatchError('Inputs changed before replacement')
            bridge=target(packages,'onebot:lib/bridge.js')
            slot=ordinary_path(packages['onebot']/'lib'/record['bridge_slot'])
            if bridge.lstat().st_nlink!=1 or sealed(identity(bridge))!=sealed(record['before']['onebot:lib/bridge.js']):
                raise PatchError('Bridge original link/identity changed')
            write_new(backup/'bridge-slot-intent.json',encoded(dict(slot=record['bridge_slot'],identity=record['before']['onebot:lib/bridge.js'])))
            os.link(bridge,slot)
            if sealed(identity(slot))!=sealed(record['before']['onebot:lib/bridge.js']) or slot.lstat().st_nlink!=2:
                raise PatchError('Physical bridge original slot did not verify')
            for index,key in enumerate(KEYS):
                view=inspect(meme_dir,onebot_dir,manifest)
                actual=identity(target(packages,key)) if os.path.lexists(target(packages,key)) else None
                if sealed(actual)!=sealed(record['before'][key]) or read_regular(stages[key])[0]!=generated[key]:
                    raise PatchError('Replacement input changed')
                write_new(backup/('intent-%d.json'%index),encoded(dict(key=key,before=actual,after=record['after'][key])))
                os.replace(stages.pop(key),target(packages,key));changed.append(key)
                if identity(target(packages,key))['sha256'] != record['after'][key]: raise PatchError('Replacement readback failed')
            inspect(meme_dir,onebot_dir,manifest)
            write_new(packages['meme']/MARKER,encoded(dict(record,state='patched')))
            if inspect(meme_dir,onebot_dir)[0]['status']!='already_patched': raise PatchError('Final picker readback failed')
            return dict(result,status='patched',applied=True,manifest=str(manifest))
        except BaseException:
            write_new(backup/'interrupted.json',encoded(dict(patch=PATCH_ID,changed=changed)))
            raise
        finally:
            for stage in stages.values():
                if os.path.lexists(stage): ordinary_path(stage).unlink()


def restore(meme_dir,onebot_dir,manifest,*,apply=False,host_stopped=False,expected_fingerprint=None):
    flags(apply,host_stopped)
    def preview():
        view,packages,originals,_,record,snapshot=inspect(meme_dir,onebot_dir,manifest,canonical=False)
        change=[k for k in KEYS if (snapshot['current'][k]['sha256'] if snapshot['current'][k] else None) != view['before'][k]]
        remove=bool(snapshot['marker'])
        slot_present=snapshot['bridge_slot'] is not None
        return dict(view,status='restore_preview' if change or remove or slot_present else 'already_restored',restore=change,remove_marker=remove,remove_slot=slot_present),packages,originals,record,snapshot
    result,packages,_,_,_=preview()
    if not apply:return result
    if host_stopped is not True:raise PatchError('Stopped host required')
    with locks(packages):
        result,packages,originals,record,snapshot=preview()
        if result['fingerprint']!=expected_fingerprint:raise PatchError('Fresh restore fingerprint required')
        if result['status']=='already_restored':return result
        # Restoring the bridge first makes historical validation real again.
        for key in reversed(KEYS):
            if key not in result['restore']:continue
            fresh=preview()[4]['current'][key]
            if sealed(fresh)!=sealed(snapshot['current'][key]):raise PatchError('Restore target changed')
            dest=target(packages,key)
            stage=None
            try:
                if record['before'][key] is None:
                    if identity(dest)!=fresh:raise PatchError('Helper removal identity changed')
                    dest.unlink()
                else:
                    if key=='onebot:lib/bridge.js':
                        slot=ordinary_path(packages['onebot']/'lib'/record['bridge_slot'])
                        if (sealed(identity(slot))!=sealed(record['before'][key]) or slot.lstat().st_nlink!=1 or
                                identity(dest)!=fresh):raise PatchError('Physical bridge restoration input changed')
                        os.replace(slot,dest)
                    else:
                        stage=dest.with_name('.lanya-picker-restore-'+uuid.uuid4().hex+'.mjs');write_new(stage,originals[key])
                        if identity(dest)!=fresh or read_regular(stage)[0]!=originals[key]:raise PatchError('Restore input changed')
                        os.replace(stage,dest);stage=None
            finally:
                if stage is not None and os.path.lexists(stage):ordinary_path(stage).unlink()
        marker=packages['meme']/MARKER
        if result['remove_marker']:
            preview()
            if identity(marker)!=snapshot['marker']:raise PatchError('Marker removal identity changed')
            marker.unlink()
        slot=ordinary_path(packages['onebot']/'lib'/record['bridge_slot'])
        if os.path.lexists(slot):
            preview()
            if (sealed(identity(slot))!=sealed(record['before']['onebot:lib/bridge.js']) or
                    sealed(identity(target(packages,'onebot:lib/bridge.js')))!=sealed(record['before']['onebot:lib/bridge.js']) or
                    slot.lstat().st_nlink!=2):raise PatchError('Unused physical original slot changed')
            slot.unlink()
        final=preview()[0]
        if final['status']!='already_restored':raise PatchError('Final restore readback failed')
        if parent_view(packages)!=record['parent_fingerprint']:raise PatchError('Real parent identity readback failed')
        return dict(result,status='restored',applied=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--meme-dir',required=True);parser.add_argument('--onebot-dir',required=True)
    parser.add_argument('--restore-manifest');parser.add_argument('--apply',action='store_true')
    parser.add_argument('--host-stopped',action='store_true');parser.add_argument('--expected-fingerprint');parser.add_argument('--backup-dir')
    args=parser.parse_args(argv)
    try:
        opts=dict(apply=args.apply,host_stopped=args.host_stopped,expected_fingerprint=args.expected_fingerprint)
        result=restore(args.meme_dir,args.onebot_dir,args.restore_manifest,**opts) if args.restore_manifest else patch(args.meme_dir,args.onebot_dir,backup_dir=args.backup_dir,**opts)
        print(json.dumps(result,ensure_ascii=True));return 0
    except (OSError,ValueError,TypeError,RuntimeError,KeyError) as error:
        print(json.dumps(dict(patch=PATCH_ID,status='refused',error_class=type(error).__name__)));return 2


if __name__=='__main__':raise SystemExit(main())
