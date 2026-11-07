"""Bounded readable QQ exports from explicitly mapped workspace/role sessions.

No workspace discovery, network/model call, or deletion of unrelated files.
Generated text and accepted inline QQ text are distinct. Interface acceptance
does not establish visibility in the recipient's client.
"""
from collections import Counter, deque
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

import panel_activity as native
import panel_autonomy as store

SCHEMA = 'scoped-qq-export/v1'
MAX_MESSAGES = 400
MAX_TEXT = 1200
MAX_SCOPES = 256
MAX_CALLS = 256
CHINA = dt.timezone(dt.timedelta(hours=8))


class ExportUnavailable(ValueError):
    pass


def require(value, reason):
    if not value:
        raise ExportUnavailable(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')) + '\n').encode()


def current_directory(workspace, preset, role):
    workspace = native._workspace(str(workspace))
    preset = native._preset(preset)
    role = store._ordinary(Path(role))
    require(role.parent == Path(workspace) and role.is_dir(), 'role_owner_mismatch')
    owner = digest(encode([workspace, preset]))
    return role / 'chats' / 'current' / owner[:32], owner


def text_of(message):
    if not isinstance(message, dict):
        return ''
    content = message.get('content')
    if isinstance(content, str):
        return content[:MAX_TEXT]
    if not isinstance(content, list):
        return ''
    return '\n'.join(block['text'] for block in content if isinstance(block, dict)
                     and block.get('type') == 'text' and isinstance(block.get('text'), str))[:MAX_TEXT]


def read_conversation(source, session, workspace, preset, kind):
    """Stream the shared stable snapshot and retain only a bounded text tail."""
    rows = deque(maxlen=MAX_MESSAGES)
    calls, accepted, markers = {}, set(), 0
    active_turn = None
    with native._native_lines(source) as lines:
        header = native._json(next(lines, b''))
        require(isinstance(header, dict) and header.get('type') == 'session' and header.get('version') == 4
                and type(header.get('isSeeded')) is bool, 'invalid_native_header')
        require(header.get('id') == session and native._workspace(header.get('cwd')) == native._workspace(workspace),
                'native_owner_mismatch')
        current = native._preset(header.get('agentPreset', ''))
        expected = native._preset(preset)
        for index, line in enumerate(lines):
            event = native._json(line)
            require(isinstance(event, dict) and type(event.get('seq')) is int and event['seq'] == index
                    and native._integer(event.get('time')) and isinstance(event.get('data'), dict), 'invalid_native_event')
            typ, data, stamp = event.get('type'), event['data'], event['time']
            if typ == 'session/end-seed' and data.get('inherited') is True:
                require(header['isSeeded'] and markers == 0, 'invalid_seed_boundary')
                rows.clear(); calls.clear(); accepted.clear(); active_turn = None; markers += 1
                current = native._preset(header.get('agentPreset', ''))
            elif typ == 'agent-preset/selected':
                current = native._preset(data.get('agentPreset', '')); active_turn = None; calls.clear()
            elif typ == 'turn/start':
                active_turn = data.get('turn'); calls.clear()
            elif typ == 'turn/end':
                active_turn = None; calls.clear()
            elif current == expected:
                if typ == 'user/message':
                    message = data.get('message', data)
                    source_meta = data.get('source', message.get('source', {}) if isinstance(message, dict) else {})
                    if isinstance(source_meta, dict) and source_meta.get('kind') == 'plugin:dsh-onebot':
                        text = text_of(message)
                        if text.strip():
                            rows.append(dict(at=stamp, turn=active_turn, kind='incoming', text=text,
                                             speaker='群消息' if kind == 'group' else '对方'))
                elif typ == 'assistant/message':
                    text = text_of(data.get('message', data))
                    if text.strip():
                        rows.append(dict(at=stamp, turn=active_turn, kind='generated', text=text, speaker='助手生成'))
                elif (typ == 'tool/call' and data.get('name') == 'qq_decide' and native._integer(active_turn)
                      and data.get('turn') == active_turn and native._integer(data.get('step'))):
                    args = native._arguments(data)
                    if isinstance(args, dict) and isinstance(args.get('text'), str) and args['text'].strip():
                        require(len(calls) < MAX_CALLS and isinstance(data.get('callId'), str), 'native_call_limit')
                        calls[data['callId']] = dict(seq=event['seq'], turn=active_turn, step=data['step'], text=args['text'][:MAX_TEXT])
                elif typ == 'tool/result':
                    message = data.get('message', {})
                    call_id = message.get('source', {}).get('callId') if isinstance(message, dict) and isinstance(message.get('source'), dict) else None
                    call = calls.pop(call_id, None)
                    if call is None:
                        continue
                    if not (event.get('surfaceOp') == 'append' and event.get('sourceEventSeqs') == [call['seq']]
                            and message.get('toolCallId') == call_id
                            and (message.get('isError') is None or message.get('isError') is False)
                            and data.get('turn') == call['turn'] and data.get('step') == call['step']):
                        continue
                    value = native._result_object(message)
                    if value and value.get('sent') is True and value.get('inline_text') is True:
                        accepted.add(call['turn'])
                        rows.append(dict(at=stamp, turn=call['turn'], kind='accepted', text=call['text'], speaker='助手 · QQ接口接受'))
        require(not header['isSeeded'] or markers == 1, 'seed_boundary_missing')
    return [row for row in rows if not (row['kind'] == 'generated' and row['turn'] in accepted)]


def render(rows, kind):
    title = '群聊记录' if kind == 'group' else '私聊记录'
    parts = ['# ' + title, '', '<!-- ' + SCHEMA + ' -->', '',
             '仅包含当前映射与已核验工作区、角色的最近文本。',
             '“助手生成”不等于已发送；QQ接口接受也不代表客户端可见。', '']
    for row in rows:
        stamp = dt.datetime.fromtimestamp(row['at'] / 1000, CHINA).isoformat(timespec='seconds')
        parts.extend(['## ' + stamp + ' · ' + row['speaker'], ''])
        parts.extend('> ' + line for line in row['text'].splitlines())
        parts.append('')
    return ('\n'.join(parts) + '\n').encode('utf8')


def _new(path, raw):
    with path.open('xb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    require(path.read_bytes() == raw, 'export_write_unconfirmed')


def _directory(path):
    store._ordinary(path); path.mkdir(exist_ok=True)
    require(path.is_dir(), 'export_directory_unavailable')


def _put(path, raw, expected=None, *, archive=True):
    """Replace only a known generated object, preserving its old bytes/SD."""
    store._ordinary(path)
    before = None
    if os.path.lexists(path):
        before, old = store._record(path)
        require(expected is not None and before['sha256'] == expected, 'managed_export_changed')
        if old == raw:
            return False
        if archive:
            history = path.parent / '.history'; _directory(history)
            original = history / (path.stem + '-' + uuid.uuid4().hex + path.suffix)
            _new(original, old); _new(original.with_suffix(original.suffix + '.sd'), store._security(path))
    stage = path.with_name('.export-' + uuid.uuid4().hex + path.suffix)
    _new(stage, raw)
    if before is not None:
        os.chmod(stage, stat.S_IMODE(before['signature'][4])); store._copy_security(path, stage)
        require(store._record(path)[0] == before, 'managed_export_changed')
        os.replace(stage, path)
    else:
        require(not os.path.lexists(path), 'managed_export_appeared')
        os.link(stage, path); stage.unlink()
    require(store._record(path)[1] == raw, 'export_write_unconfirmed')
    return True


def _archive_output(path, expected):
    """Hide only a verified managed export; preserve its physical object."""
    store._ordinary(path)
    if not os.path.lexists(path):
        return False
    before, raw = store._record(path)
    require(isinstance(expected, str) and before['sha256'] == expected, 'managed_export_changed')
    history = path.parent / '.history'; _directory(history)
    saved = history / (path.stem + '-' + uuid.uuid4().hex + path.suffix)
    require(not os.path.lexists(saved), 'export_archive_conflict')
    _new(saved.with_suffix(saved.suffix + '.sd'), store._security(path))
    require(store._record(path)[0] == before, 'managed_export_changed')
    os.replace(path, saved)
    require(store._record(saved) == (before, raw), 'export_archive_unconfirmed')
    return True


def export_current(*, workspace, preset, role, dsh_home, mapping_file=None):
    directory, owner = current_directory(workspace, preset, role)
    workspace = native._workspace(str(workspace)); preset = native._preset(preset)
    home = native.ordinary(Path(dsh_home))
    mapping_path = native.ordinary(Path(mapping_file) if mapping_file is not None else home / 'media/onebot/chat-sessions.json')
    mapping_raw = native.read_bytes(mapping_path, 1024 * 1024); mapping = native._json(mapping_raw)
    require(isinstance(mapping, dict) and len(mapping) <= 4096, 'invalid_mapping')
    selected = []
    for scope, entry in sorted(mapping.items()):
        match = re.fullmatch(r'(private|group):[1-9][0-9]{0,19}', scope)
        if not match:
            continue
        if isinstance(entry, str):
            session, cwd = entry, workspace
        elif isinstance(entry, dict):
            session, cwd = entry.get('session'), entry.get('workspacePath') or workspace
        else:
            continue
        if native._workspace(cwd) != workspace:
            continue
        require(isinstance(session, str) and 0 < len(session) <= 256, 'invalid_mapping')
        selected.append((scope, session, cwd, match[1]))
    require(len(selected) <= MAX_SCOPES, 'scope_limit')
    duplicates = Counter(item[1] for item in selected)
    if not selected and not directory.exists():
        return dict(status='ok', scopes=0, written=0, archived=0, cached=0, messages=0, errors=0)
    for path in (Path(role) / 'chats', Path(role) / 'chats/current', directory):
        _directory(path)
    index_path = directory / 'index.json'
    with store._write_lock(index_path):
        cache_record = None
        cache = dict(schema=SCHEMA, owner=owner, entries={})
        if index_path.exists():
            cache_record, body = store._record(index_path)
            cache = native._json(body)
            require(isinstance(cache, dict) and cache.get('schema') == SCHEMA and cache.get('owner') == owner
                    and isinstance(cache.get('entries'), dict), 'invalid_export_cache')
            require(len(cache['entries']) <= MAX_SCOPES and all(
                re.fullmatch('[0-9a-f]{24}', key) and isinstance(row, dict)
                and row.get('filename') == 'qq-' + key + '.md'
                and type(row.get('messages')) is int and 0 <= row['messages'] <= MAX_MESSAGES
                and isinstance(row.get('source_key'), str) and re.fullmatch('[0-9a-f]{64}', row['source_key'])
                and (row['messages'] == 0 or isinstance(row.get('output_sha256'), str)
                     and re.fullmatch('[0-9a-f]{64}', row['output_sha256']))
                for key, row in cache['entries'].items()), 'invalid_export_cache')
        entries, plans, errors, cached = {}, [], 0, 0
        selected_keys = {digest(encode([owner, scope]))[:24] for scope, _, _, _ in selected}
        for scope, session, cwd, kind in selected:
            key = digest(encode([owner, scope]))[:24]
            filename = 'qq-' + key + '.md'; output = directory / filename
            previous = cache['entries'].get(key, {})
            try:
                require(isinstance(previous, dict) and duplicates[session] == 1, 'ambiguous_mapping')
                folder = home / 'sessions' / native._project(cwd) / native._segment(session)
                sources = [folder / name for name in ('session.v4.jsonl', 'session.v4.jsonl.zstd') if os.path.lexists(folder / name)]
                require(len(sources) == 1, 'native_source_missing_or_ambiguous')
                source = sources[0]
                key_before = digest(encode(native.native_source_key(source, owner, scope, session)))
                if previous.get('source_key') == key_before and previous.get('filename') == filename:
                    if previous.get('messages') == 0:
                        entries[key] = previous; cached += 1; continue
                    if output.exists() and store._record(output)[0]['sha256'] == previous.get('output_sha256'):
                        entries[key] = previous; cached += 1; continue
                rows = read_conversation(source, session, workspace, preset, kind)
                entry = dict(source_key=key_before, filename=filename, messages=len(rows))
                if rows:
                    body = render(rows, kind); entry['output_sha256'] = digest(body)
                    plans.append((output, body, previous.get('output_sha256')))
                elif previous.get('output_sha256'):
                    plans.append((output, None, previous['output_sha256']))
                entries[key] = entry
            except (OSError, ValueError, TypeError, UnicodeError, OverflowError):
                errors += 1
                if previous:
                    entries[key] = previous
        require(native.read_bytes(mapping_path, 1024 * 1024) == mapping_raw, 'mapping_changed')
        for key, previous in cache['entries'].items():
            if key not in selected_keys and previous.get('output_sha256'):
                plans.append((directory / previous['filename'], None, previous['output_sha256']))
        written, archived = 0, 0
        for output, body, expected in plans:
            try:
                if body is None:
                    archived += int(_archive_output(output, expected))
                else:
                    written += int(_put(output, body, expected))
            except (OSError, ValueError, TypeError):
                errors += 1
                key = output.stem.removeprefix('qq-')
                if key in selected_keys and key in cache['entries']:
                    entries[key] = cache['entries'][key]
                else:
                    entries.pop(key, None)
        result_cache = dict(schema=SCHEMA, owner=owner, status='partial' if errors else 'ok', entries=entries)
        _put(index_path, encode(result_cache), cache_record['sha256'] if cache_record else None, archive=False)
    return dict(status='partial' if errors else 'ok', scopes=len(selected), written=written,
                archived=archived, cached=cached, messages=sum(row.get('messages', 0) for row in entries.values()), errors=errors)


def main():
    import project_conf
    try:
        config = project_conf.load(reload=True)
        decisions = config.get('decisions', {}); options = config.get('panelRecords', {})
        require(isinstance(decisions, dict) and isinstance(options, dict), 'invalid_configuration')
        workspace = project_conf.work_dir()
        home = options.get('dshHome', str(Path.home() / '.dsh')); mapping = options.get('onebotMappingFile')
        require(isinstance(home, str) and os.path.isabs(home) and
                (mapping is None or isinstance(mapping, str) and os.path.isabs(mapping)), 'invalid_record_paths')
        result = export_current(workspace=workspace, preset=decisions.get('agentPreset', ''),
            role=project_conf.data_dir(), dsh_home=home, mapping_file=mapping)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError, project_conf.ConfigError):
        print(json.dumps(dict(status='unavailable', reason='scoped_export_unavailable', source_records_unchanged=True)))
        return 2
