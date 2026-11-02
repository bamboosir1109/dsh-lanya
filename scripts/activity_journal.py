"""Private, deterministic daily facts. No model, transport or queue mutations.

Only the current configured owner's mapped native sessions are represented.
Existing notes/diaries and foreign files are never adopted or overwritten.
"""
from contextlib import contextmanager
import ctypes
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from panel_autonomy import _record as _secure_record, _copy_security, _ordinary

SCHEMA = 'activity-journal/v1'
BEIJING = dt.timezone(dt.timedelta(hours=8))
MAX_MARKDOWN = 512 * 1024
MAX_INDEX = 32 * 1024 * 1024


class JournalError(ValueError):
    pass


def _need(value):
    if not value:
        raise JournalError('journal_evidence_unavailable')


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _need(key not in result)
            result[key] = value
        return result
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                      parse_constant=lambda _: _need(False))


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def _record(path, maximum=MAX_INDEX):
    value, raw = _secure_record(path, maximum)
    return json.loads(json.dumps(value)), raw


def _day(value):
    _need(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) is not None)
    _need(dt.date.fromisoformat(value).isoformat() == value)
    return value


def _stamp(value):
    return type(value) is int and 0 <= value <= 9007199254740991


def _text(value, maximum=1000):
    if not isinstance(value, str):
        return ''
    return ' '.join(''.join(c for c in value[:maximum] if ord(c) >= 32 and ord(c) != 127).split())


def _time(value):
    return dt.datetime.fromtimestamp(value / 1000, BEIJING).strftime('%H:%M:%S') if _stamp(value) else '时间未确认'


def _private_directory(path, create=False):
    """Create only new directories with a private DACL; never edit parents."""
    path = _ordinary(path)
    if os.name != 'nt':
        if create and not path.exists():
            path.mkdir(mode=0o700)
        info = path.lstat()
        _need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0)
        return
    from ctypes import wintypes
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    token, size = wintypes.HANDLE(), wintypes.DWORD()
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    _need(bool(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token))))
    try:
        advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        _need(0 < size.value < 65536)
        buffer = ctypes.create_string_buffer(size.value)
        _need(bool(advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size))))
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_text = wintypes.LPWSTR()
        advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        _need(bool(advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text))))
        try:
            expected = 'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;' + sid_text.value + ')'
        finally:
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
    finally:
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(token)
    descriptor, length = ctypes.c_void_p(), wintypes.DWORD()
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    _need(bool(advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(expected, 1, ctypes.byref(descriptor), ctypes.byref(length))))
    try:
        if create and not os.path.lexists(path):
            class Attributes(ctypes.Structure):
                _fields_ = [('length', wintypes.DWORD), ('descriptor', ctypes.c_void_p), ('inherit', wintypes.BOOL)]
            attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
            kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(Attributes)]
            _need(bool(kernel.CreateDirectoryW(str(path), ctypes.byref(attributes))))
        _need(path.is_dir())
        actual = ctypes.c_void_p()
        advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        _need(advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None, None, None, ctypes.byref(actual)) == 0)
        try:
            sddl = wintypes.LPWSTR()
            advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
            _need(bool(advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(actual, 1, 4, ctypes.byref(sddl), None)))
            try:
                _need(sddl.value == expected)
            finally:
                kernel.LocalFree(ctypes.cast(sddl, ctypes.c_void_p))
        finally:
            kernel.LocalFree(actual)
    finally:
        kernel.LocalFree(descriptor)


def _location(workspace, agent_preset, data_directory):
    _need(isinstance(workspace, str) and os.path.isabs(workspace) and isinstance(agent_preset, str) and bool(agent_preset.strip()))
    work, data = _ordinary(workspace), _ordinary(data_directory)
    _need(work.is_dir() and data.is_dir() and os.path.commonpath([str(work), str(data)]) == str(work) and data != work)
    key = os.path.normcase(str(work)) + '\0' + (agent_preset.strip().lower() if os.name == 'nt' else agent_preset.strip())
    owner = hashlib.sha256(key.encode()).hexdigest()
    return data / 'activity-journal' / owner[:32], owner


@contextmanager
def _lock(root, create=False):
    path = _ordinary(root / '.lock')
    if create and not os.path.lexists(path):
        with path.open('xb') as stream:
            stream.write(b'\0'); stream.flush(); os.fsync(stream.fileno())
    before = path.lstat()
    _need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size == 1)
    fd = os.open(path, os.O_RDWR if create else os.O_RDONLY)
    acquired = False
    try:
        _need((os.fstat(fd).st_dev, os.fstat(fd).st_ino) == (before.st_dev, before.st_ino))
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1); acquired = True
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); acquired = True
            except OSError:
                pass
        _need(acquired)
        yield
    finally:
        if acquired:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == 'nt': msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_index(root, owner):
    path = root / 'index.json'
    if not os.path.lexists(path):
        return {'schema': SCHEMA, 'owner': owner, 'days': {}}, None
    before, raw = _record(path)
    value = _json(raw)
    _validate_index(value, owner)
    return value, before


def _validate_index(value, owner):
    _need(set(value) == {'schema', 'owner', 'days'} and value['schema'] == SCHEMA and value['owner'] == owner and type(value['days']) is dict)
    _need(len(value['days']) <= 10000)
    for day, item in value['days'].items():
        _day(day)
        _need(type(item) is dict and set(item) == {'status', 'updated_at', 'entries', 'coverage', 'record', 'input_digest', 'counts'} and
              item['status'] in ('open', 'closed_snapshot', 'partial') and _stamp(item['updated_at']) and
              type(item['entries']) is int and 0 <= item['entries'] <= 1000000 and
              isinstance(item['input_digest'], str) and re.fullmatch('[0-9a-f]{64}', item['input_digest']) is not None and
              type(item['coverage']) is dict and type(item['counts']) is dict)
        _validate_record(item['record'], MAX_MARKDOWN)
        _validate_coverage(item['coverage'])
        _need(set(item['counts']) == {'episodes', 'tools', 'decisions'} and
              all(type(n) is int and 0 <= n <= 1000000 for n in item['counts'].values()))


def _validate_record(value, maximum=MAX_INDEX, missing=False):
    if value is None and missing:
        return
    _need(type(value) is dict and set(value) == {'signature', 'sha256', 'security_sha256'} and
          isinstance(value['signature'], list) and len(value['signature']) == 7 and
          all(type(n) is int and n >= 0 for n in value['signature']) and value['signature'][5] == 1 and
          value['signature'][2] <= maximum and
          all(isinstance(value[k], str) and re.fullmatch('[0-9a-f]{64}', value[k]) is not None for k in ('sha256', 'security_sha256')))


def _validate_coverage(value):
    integers = {'configured_scopes', 'mapped_scopes', 'owner_verified_scopes', 'window_start', 'window_end', 'future_day_records_omitted'}
    booleans = {'truncated', 'decisions_truncated', 'day_closed', 'journal_text_truncated'}
    _need(type(value) is dict and set(value).issubset(integers | booleans | {'current_mapped_sessions_only', 'all_account_history_complete', 'reasons', 'source_windows'}) and
          value.get('current_mapped_sessions_only') is True and value.get('all_account_history_complete') is False)
    _need(all(_stamp(value[k]) for k in set(value) & integers) and all(type(value[k]) is bool for k in set(value) & booleans))
    _need(type(value.get('reasons')) is list and len(value['reasons']) <= 20 and
          all(isinstance(reason, str) and re.fullmatch('[A-Za-z_]{1,80}', reason) for reason in value['reasons']))
    sources = value.get('source_windows')
    _need(type(sources) is list and len(sources) <= 2 and len({row.get('source') for row in sources if type(row) is dict}) == len(sources))
    for row in sources:
        _need(type(row) is dict and set(row).issubset({'source', 'available_from', 'binding_sha256'}) and
              row.get('source') in ('primary', 'social') and (row.get('available_from') is None or _stamp(row['available_from'])) and
              isinstance(row.get('binding_sha256'), str) and re.fullmatch('[0-9a-f]{64}', row['binding_sha256']))


def _current(path):
    return _record(path)[0] if os.path.lexists(path) else None


def _new(path, raw):
    _need(len(raw) <= MAX_INDEX)
    with path.open('xb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    return _record(path)[0]


def _recover(root, owner):
    pending = root / 'pending.json'
    if not os.path.lexists(pending):
        return
    pending_record, raw = _record(pending)
    value = _json(raw)
    _need(type(value) is dict and set(value) == {'schema', 'owner', 'day', 'backup', 'markdown', 'index'} and
          value['schema'] == SCHEMA and value['owner'] == owner)
    day = _day(value['day'])
    targets = [('markdown', root / (day + '.md')), ('index', root / 'index.json')]
    for kind, target in targets:
        row = value[kind]
        _need(type(row) is dict and set(row) == {'before', 'after', 'candidate'} and type(row['candidate']) is str and
              row['candidate'] == Path(row['candidate']).name)
        _validate_record(row['before'], MAX_MARKDOWN if kind == 'markdown' else MAX_INDEX, True)
        _validate_record(row['after'], MAX_MARKDOWN if kind == 'markdown' else MAX_INDEX)
        candidate = _ordinary(root / row['candidate'])
        _need(candidate.parent == root and re.fullmatch(r'\.stage-[0-9a-f]{32}\.' + ('md' if kind == 'markdown' else 'json'), candidate.name) is not None)
        _need(_current(target) in (row['before'], row['after']))
    after_index_path = root / value['index']['candidate'] if _current(root / 'index.json') == value['index']['before'] else root / 'index.json'
    after_index_record, after_index_raw = _record(after_index_path)
    _need(after_index_record == value['index']['after'])
    after_index = _json(after_index_raw)
    _validate_index(after_index, owner)
    _need(day in after_index['days'] and after_index['days'][day]['record'] == value['markdown']['after'])
    backup = value['backup']
    if backup is not None:
        _need(type(backup) is dict and set(backup) == {'name', 'record'} and type(backup['name']) is str and
              backup['name'] == Path(backup['name']).name)
        _validate_record(backup['record'], MAX_MARKDOWN)
        path = _ordinary(root / backup['name'])
        _need(path.parent == root and re.fullmatch(r'\.backup-[0-9a-f]{32}\.md', path.name) is not None)
        if os.path.lexists(path):
            _need(_current(path) == backup['record'] and backup['record']['sha256'] == value['markdown']['before']['sha256'])
        else:
            _need(all(_current(target) == value[kind]['after'] for kind, target in targets))
    for kind, target in targets:
        row = value[kind]
        candidate = root / row['candidate']
        now = _current(target)
        _need(now in (row['before'], row['after']))
        if now == row['before']:
            _need(_current(candidate) == row['after'] and _current(target) == now)
            os.replace(candidate, target)
        _need(_current(target) == row['after'])
    # Keep the exact previous content until both replacements are committed.
    if backup is not None:
        path = root / backup['name']
        if os.path.lexists(path):
            _need(_current(path) == backup['record'])
            path.unlink()
    _need(_current(pending) == pending_record)
    pending.unlink()


def _coverage(fact):
    original = fact.get('coverage', {})
    _need(type(original) is dict)
    result = {key: original[key] for key in ('configured_scopes', 'mapped_scopes', 'owner_verified_scopes', 'window_start', 'window_end',
              'truncated', 'decisions_truncated', 'day_closed', 'future_day_records_omitted') if key in original}
    result.update(current_mapped_sessions_only=True, all_account_history_complete=False)
    result['reasons'] = [item for item in original.get('reasons', []) if isinstance(item, str) and re.fullmatch('[A-Za-z_]{1,80}', item)]
    result['source_windows'] = [{k: row[k] for k in ('source', 'available_from', 'binding_sha256') if k in row}
                                for row in original.get('source_windows', []) if type(row) is dict and row.get('source') in ('primary', 'social')]
    _validate_coverage(result)
    return result


def _render(fact):
    day = _day(fact['day'])
    coverage = _coverage(fact)
    episodes = fact.get('episodes', [])
    _need(type(episodes) is list and type(fact.get('counts')) is dict)
    lines = ['# ' + day + ' · 事实日记', '', '这是一份本机运行事实记录，不是情绪自述，也不代表完整账号历史。', '', '## 记录到的事情', '']
    counts = {key: value for key, value in fact['counts'].items() if key in ('episodes', 'tools', 'decisions') and type(value) is int and value >= 0}
    _need(set(counts) == {'episodes', 'tools', 'decisions'})
    lines.append('可见记录汇总：%d 段活动，%d 次工具调用，%d 次决定。' % (counts.get('episodes', 0), counts.get('tools', 0), counts.get('decisions', 0)))
    if not episodes:
        lines.extend(['', '当前可信来源中没有这一天的可见工具活动；这不证明没有发生其他事情。'])
    statuses = {'observed': '已观察', 'returned': '工具已返回', 'failed': '失败', 'partial': '部分记录', 'pending': '待结果', 'unknown': '结果未确认'}
    incomplete = fact.get('status') != 'ok' or coverage.get('truncated') is True or coverage.get('decisions_truncated') is True
    for episode in episodes[:2000]:
        _need(type(episode) is dict)
        title, summary = _text(episode.get('title'), 240), _text(episode.get('summary'), 1200)
        role = '主管理员会话' if episode.get('conversation_role') == 'primary' else '群社交会话' if episode.get('conversation_role') == 'social' else '来源未确认'
        status = statuses.get(episode.get('status'), '结果未确认')
        incomplete = incomplete or episode.get('status') in ('pending', 'unknown', 'partial') or episode.get('evidence_truncated') is True
        origin = {'observation': '自主观察', 'live': '实时会话', 'unknown': '触发来源未确认'}.get(episode.get('trigger_origin'), '触发来源未确认')
        lines.extend(['', '### %s · %s · %s · %s' % (_time(episode.get('started_at')), role, origin, status), title, summary])
        for evidence in episode.get('evidence', [])[:40]:
            if type(evidence) is not dict:
                continue
            label = _text(evidence.get('label'), 200)
            text = _text(evidence.get('summary'), 1200)
            if label or text:
                lines.append('- ' + (label + '：' if label else '') + text)
            excerpt = _text(evidence.get('content_excerpt'), 600)
            if excerpt:
                lines.append('  日记摘录（不是全文）：' + excerpt)
            video = evidence.get('video')
            if isinstance(video, dict) and isinstance(video.get('bvid'), str) and re.fullmatch(r'BV[0-9A-Za-z]{10}', video['bvid']):
                title = _text(video.get('title'), 240)
                author = _text(video.get('author'), 120)
                lines.append('  视频：' + title + (' · 作者：' + author if author else '') + ' · ' + video['bvid'])
                lines.append('  https://www.bilibili.com/video/' + video['bvid'])
            reply = evidence.get('reply')
            if isinstance(reply, dict) and reply.get('type') in ('comment', 'comment_reply'):
                text = _text(reply.get('text'), 2000)
                if text:
                    lines.append('  公开评论尝试：' + text)
                    lines.append('  接口回执：' + ('已记录' if reply.get('receipt_present') is True else '未确认') + '；公开可见性未核实。')
                    if reply.get('text_truncated') is True: lines.append('  评论尝试文字已截断。')
        if episode.get('evidence_truncated') is True:
            lines.append('本段证据条目已截断；不能把未展示条目当作不存在。')
    lines.extend(['', '## 范围与未确认', '', '仅读取当前配置的可信映射会话；已更换会话、其他账号与未保留来源不在本页证明范围内。',
                  '工具返回不等于完成动作；推荐、详情、字幕、抽帧都不证明播放或看完，接口回执不证明公开可见或 QQ 客户端送达。'])
    for row in coverage['source_windows']:
        if _stamp(row.get('available_from')):
            stamp = dt.datetime.fromtimestamp(row['available_from'] / 1000, BEIJING).isoformat()
            lines.append(('主管理员' if row['source'] == 'primary' else '群社交') + '来源可见起点：' + stamp)
    if incomplete:
        lines.append('本页存在来源不足、截断或尚未确认的结果；完整计数与来源边界以所附覆盖信息为准。')
    lines.append('这是过去日期的日终事实快照，仍可能有迟到证据。' if coverage.get('day_closed') else '当天记录仍在自然更新。')
    body = '\n'.join(line for line in lines if line is not None) + '\n'
    raw = body.encode('utf-8')
    if len(raw) > MAX_MARKDOWN:
        body = raw[:MAX_MARKDOWN - 150].decode('utf-8', errors='ignore') + '\n\n正文达到展示上限，已截断；不能当作整日完整明细。\n'
        coverage['journal_text_truncated'] = True
        incomplete = True
    status = 'partial' if incomplete else 'closed_snapshot' if coverage.get('day_closed') else 'open'
    return body, coverage, status, counts.get('episodes', len(episodes))


def _commit(root, owner, index, index_before, fact, now, fault=None):
    day = _day(fact['day'])
    body, coverage, status, entries = _render(fact)
    stable = {'day': day, 'body': body, 'coverage': coverage, 'status': status, 'entries': entries}
    input_digest = hashlib.sha256(_encoded(stable)).hexdigest()
    previous = index['days'].get(day)
    target = root / (day + '.md')
    before = _current(target)
    _need(before == (previous['record'] if previous else None))
    if previous is not None:
        old_coverage = previous['coverage']
        regressed = (previous['status'] != 'partial' and status == 'partial' or
                     any(fact['counts'][k] < previous['counts'][k] for k in ('episodes', 'tools', 'decisions')) or
                     any(coverage.get(k, 0) < old_coverage.get(k, 0) for k in ('owner_verified_scopes', 'mapped_scopes')))
        old_sources = {row['source']: row for row in old_coverage.get('source_windows', [])}
        new_sources = {row['source']: row for row in coverage.get('source_windows', [])}
        for source, old in old_sources.items():
            new = new_sources.get(source)
            regressed = regressed or new is None
            if new is not None:
                regressed = regressed or (_stamp(old.get('available_from')) and
                    (not _stamp(new.get('available_from')) or new['available_from'] > old['available_from']))
                regressed = regressed or not old.get('binding_sha256') or new.get('binding_sha256') != old.get('binding_sha256')
        if regressed:
            return 'degraded'
    if previous and previous['input_digest'] == input_digest:
        return 'unchanged'
    markdown_stage = root / ('.stage-' + uuid.uuid4().hex + '.md')
    markdown_after = _new(markdown_stage, body.encode('utf-8'))
    if before is not None:
        _copy_security(target, markdown_stage)
        markdown_after = _record(markdown_stage)[0]
    metadata = {'status': status, 'updated_at': now, 'entries': entries, 'coverage': coverage,
                'record': markdown_after, 'input_digest': input_digest, 'counts': {k: fact['counts'][k] for k in ('episodes', 'tools', 'decisions')}}
    next_index = dict(index, days=dict(index['days'], **{day: metadata}))
    index_stage = root / ('.stage-' + uuid.uuid4().hex + '.json')
    index_after = _new(index_stage, _encoded(next_index))
    if index_before is not None:
        _copy_security(root / 'index.json', index_stage)
        index_after = _record(index_stage)[0]
    backup = None
    if before is not None:
        old_record, old_body = _record(target)
        _need(old_record == before)
        backup_path = root / ('.backup-' + uuid.uuid4().hex + '.md')
        backup = {'name': backup_path.name, 'record': _new(backup_path, old_body)}
    _need(_current(target) == before and _current(root / 'index.json') == index_before)
    intent = {'schema': SCHEMA, 'owner': owner, 'day': day, 'backup': backup,
              'markdown': {'before': before, 'after': markdown_after, 'candidate': markdown_stage.name},
              'index': {'before': index_before, 'after': index_after, 'candidate': index_stage.name}}
    _new(root / 'pending.json', _encoded(intent))
    if fault:
        fault('after_intent')
    _recover(root, owner)
    return 'written'


def _write_health(root, owner, value):
    path = root / 'scan.json'
    before = _current(path)
    if before is not None:
        old = _json(_record(path)[1])
        _need(old['schema'] == SCHEMA and old['owner'] == owner)
    value = dict(value, schema=SCHEMA, owner=owner)
    stage = root / ('.stage-' + uuid.uuid4().hex + '.json')
    after = _new(stage, _encoded(value))
    if before is not None:
        _copy_security(path, stage); after = _record(stage)[0]
    _need(_current(path) == before and _current(stage) == after)
    os.replace(stage, path)
    _need(_current(path) == after)


def update_journal(*, workspace, agent_preset, data_directory, identity_file, dsh_home,
                   mapping_file=None, group_fallback='', now=None, catalog_provider=None, fault=None):
    """Today first, then at most one unsealed/changed older date per cycle."""
    now = int(time.time() * 1000) if now is None else now
    _need(_stamp(now))
    root, owner = _location(workspace, agent_preset, data_directory)
    if catalog_provider is None:
        from panel_activity import daily_catalog
        catalog_provider = daily_catalog
    catalog = catalog_provider(workspace=workspace, agent_preset=agent_preset, identity_file=identity_file,
                               dsh_home=dsh_home, mapping_file=mapping_file, group_fallback=group_fallback, now=now, limit=2000)
    _need(catalog.get('schema') == 'panel-activity-catalog/v1')
    if catalog.get('status') not in ('ok', 'partial'):
        if os.path.lexists(root):
            _private_directory(root.parent); _private_directory(root)
            with _lock(root, True):
                _write_health(root, owner, {'status': 'degraded', 'at': now, 'blocked_dates': [], 'reasons': ['source_unavailable']})
        raise JournalError('journal_source_unavailable')
    today = dt.datetime.fromtimestamp(now / 1000, BEIJING).date().isoformat()
    facts = {_day(row['day']): row for row in catalog['facts'] if row.get('status') in ('ok', 'partial')}
    _need(len(facts) == sum(row.get('status') in ('ok', 'partial') for row in catalog['facts']))
    _need(set(facts).issubset({_day(day) for day in catalog['days']}) and all(day <= today for day in facts))
    _private_directory(root.parent, True); _private_directory(root, True)
    written = 0
    with _lock(root, True):
        _recover(root, owner)
        index, before = _read_index(root, owner)
        order = ([today] if today in facts else []) + sorted(day for day in facts if day != today)
        old_written, blocked = 0, []
        for day in order:
            if day != today and old_written:
                break
            changed = _commit(root, owner, index, before, facts[day], now, fault)
            if changed == 'degraded': blocked.append(day)
            written += int(changed == 'written')
            old_written += int(changed == 'written' and day != today)
            index, before = _read_index(root, owner)
        _write_health(root, owner, {'status': 'degraded' if blocked or catalog['status'] == 'partial' else 'ok',
                                   'at': now, 'blocked_dates': blocked, 'reasons': ['source_coverage_regressed'] if blocked else []})
    return {'schema': SCHEMA, 'status': 'degraded' if blocked or catalog['status'] == 'partial' else 'ok', 'written_count': written, 'read_only_sources': True,
            'model_or_send_requests': 0, 'current_mapped_sessions_only': True}


def journal_snapshot(*, workspace, agent_preset, data_directory, day=None, limit=90, now=None):
    """Pure local read. Never creates folders, locks, diaries or source events."""
    stamp = int(time.time() * 1000) if now is None else now
    result = {'schema': SCHEMA, 'status': 'error', 'generated_at': stamp, 'read_only': True,
              'private': True, 'days': [], 'selected': None}
    try:
        _need(type(limit) is int and 1 <= limit <= 90 and _stamp(stamp))
        if day is not None: _day(day)
        root, owner = _location(workspace, agent_preset, data_directory)
        if not os.path.lexists(root):
            result.update(status='empty', selected_status='not_generated')
            return result
        _private_directory(root.parent); _private_directory(root)
        with _lock(root):
            _need(not os.path.lexists(root / 'pending.json'))
            index, before = _read_index(root, owner)
            health = {'status': 'unknown', 'blocked_dates': [], 'reasons': []}
            if os.path.lexists(root / 'scan.json'):
                health = _json(_record(root / 'scan.json')[1])
                _need(health['schema'] == SCHEMA and health['owner'] == owner)
            result['freshness'] = {key: health[key] for key in ('status', 'blocked_dates', 'reasons')}
            for selected_day in sorted(index['days'], reverse=True)[:limit]:
                item = index['days'][selected_day]
                _need(_current(root / (selected_day + '.md')) == item['record'])
                result['days'].append(dict(date=selected_day, **{key: item[key] for key in ('status', 'updated_at', 'entries', 'coverage')}))
            if day in index['days']:
                item = index['days'][day]
                actual, raw = _record(root / (day + '.md'), MAX_MARKDOWN)
                _need(actual == item['record'])
                result['selected'] = dict(date=day, markdown=raw.decode('utf-8'),
                                          **{key: item[key] for key in ('status', 'updated_at', 'entries', 'coverage')})
                if health['status'] == 'degraded':
                    result['selected']['coverage'] = dict(item['coverage'], latest_scan_degraded=True)
            _need(_current(root / 'index.json') == before)
        result.update(status='ok' if index['days'] else 'empty', selected_status='available' if result['selected'] else 'not_generated')
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RuntimeError):
        result.update(status='error', days=[], selected=None, selected_status='unavailable')
    return result
