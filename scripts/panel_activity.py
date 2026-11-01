"""Bounded read-only projections of native tool events for the local panel.

Only configured conversations' current mapping is read. Conversation text,
reasoning, raw arguments, session IDs and private recipients never enter output.
Public comment attempts are projected explicitly; platform receipts do not
establish public visibility. Private-message text is never projected.
This is a local runtime record, not proof of playback or recipient visibility.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import time

MAX_SOURCE = 32 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_LINE = 4 * 1024 * 1024
MAX_ROWS = 200000
MAX_RESULT = 2 * 1024 * 1024
MAX_REPLY_TEXT = 2000
SAFE_INTEGER = 9007199254740991
BILI_TOOLS = {
    'bili_recommend': 'recommendation', 'bili_run': 'recommendation',
    'bili_video_info': 'details', 'bili_subtitle': 'subtitle',
    'bili_frames': 'frames', 'bili_cover': 'cover',
    'bili_reply': 'action', 'bili_send_message': 'action',
}


class ActivityUnavailable(ValueError):
    pass


def _require(value, reason='malformed_source'):
    if not value:
        raise ActivityUnavailable(reason)


def _integer(value):
    return type(value) is int and 0 <= value <= SAFE_INTEGER


def _workspace(value):
    _require(isinstance(value, str) and bool(value.strip()) and os.path.isabs(value), 'owner_mismatch')
    full = os.path.normpath(value)
    return os.path.normcase(full) if os.name == 'nt' else full


def _preset(value):
    _require(isinstance(value, str) and bool(value.strip()), 'owner_mismatch')
    return value.strip().lower() if os.name == 'nt' else value.strip()


def ordinary(path):
    path = Path(os.path.abspath(os.fspath(path)))
    for current in [*reversed(path.parents), path]:
        if os.path.lexists(current):
            info = current.lstat()
            _require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400,
                     'unsafe_source')
    return path


def read_bytes(path, maximum=MAX_SOURCE):
    """Stable bounded read; no mode=write, index refresh, lock or DB access."""
    path = ordinary(path)
    before = path.stat()
    _require(stat.S_ISREG(before.st_mode) and before.st_size <= maximum, 'source_limit')
    signature = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns)
    with path.open('rb') as stream:
        _require(signature(os.fstat(stream.fileno())) == signature(before), 'source_changing')
        raw = stream.read(maximum + 1)
        _require(signature(os.fstat(stream.fileno())) == signature(before), 'source_changing')
    _require(len(raw) == before.st_size and signature(path.stat()) == signature(before), 'source_changing')
    return raw


def _json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            _require(key not in result)
            result[key] = value
        return result
    try:
        return json.loads(raw.decode('utf-8-sig') if isinstance(raw, bytes) else raw,
                          object_pairs_hook=pairs, parse_constant=lambda _: _require(False))
    except (ValueError, UnicodeError, RecursionError):
        raise ActivityUnavailable('malformed_source') from None


def _units(value):
    raw = value.encode('utf-16-le', 'surrogatepass')
    return [int.from_bytes(raw[index:index + 2], 'little') for index in range(0, len(raw), 2)]


def _segment(value):
    _require(isinstance(value, str) and value and value == value.strip(), 'mapping_invalid')
    if value in ('.', '..'):
        return '~002E' * len(value)
    return ''.join(chr(unit) if re.fullmatch('[A-Za-z0-9._-]', chr(unit)) else '~%04X' % unit
                   for unit in _units(value))


def _project(value):
    result, separator = '', False
    for unit in _units(value):
        char = chr(unit)
        if char in '/\\:':
            if not separator:
                result += '-'
            separator = True
        else:
            result += char if re.fullmatch('[A-Za-z0-9._-]', char) else '~%04X' % unit
            separator = False
    return '--' + (result.lstrip('-') or 'root')[:251] + '--'


def _video(value):
    if not isinstance(value, dict) or not isinstance(value.get('bvid'), str) or not re.fullmatch(r'BV[0-9A-Za-z]{10}', value['bvid']):
        return None
    result = {'bvid': value['bvid'], 'url': 'https://www.bilibili.com/video/' + value['bvid']}
    title = value.get('title')
    if isinstance(title, str):
        result['title'] = ''.join(char for char in title[:240] if ord(char) >= 32 and ord(char) != 127)
    return result


def _result_object(message):
    content = message.get('content', [])
    _require(isinstance(content, list))
    text = '\n'.join(block['text'] for block in content if isinstance(block, dict)
                     and block.get('type') == 'text' and isinstance(block.get('text'), str))
    if len(text.encode('utf-8')) > MAX_RESULT:
        return None
    try:
        value = _json(text)
    except ActivityUnavailable:
        return None
    if not isinstance(value, dict):
        return None
    # Native bili_run wraps its actual recommendation in data; direct tools do
    # not. Only an explicit successful envelope may be unwrapped.
    for _ in range(3):
        if isinstance(value.get('data'), dict) and (value.get('ok') is True or value.get('success') is True):
            value = value['data']
        else:
            break
    return value


def _receipt(value):
    return ((type(value) is int and 0 < value <= SAFE_INTEGER) or
            (isinstance(value, str) and bool(re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,127}', value))))


def _comment_id(value):
    if type(value) is int and 0 < value <= SAFE_INTEGER:
        return str(value)
    if isinstance(value, str) and re.fullmatch('[1-9][0-9]{0,31}', value):
        return value
    return None


def _reply_call(name, args):
    if name not in ('bili_reply', 'bili_send_message'):
        return None
    reply = {'type': 'private_message' if name == 'bili_send_message' else 'comment',
             'status': 'pending', 'receipt_present': False, 'evidence': 'pending_call',
             'public_visibility': 'not_applicable' if name == 'bili_send_message' else 'unverified'}
    # A private message remains metadata-only even when its native arguments
    # contain a recipient, text, or fields resembling a public video.
    if name == 'bili_send_message' or not isinstance(args, dict):
        return reply
    video = _video({'bvid': args.get('bvid')})
    if video:
        reply['video'] = video
    target = _comment_id(args.get('rpid'))
    if target:
        reply.update(type='comment_reply', target_comment_id=target)
    text = args.get('text')
    if isinstance(text, str):
        # This is the explicit public comment argument, not a model explanation,
        # result message, quoted conversation, or private-message argument.
        text.encode('utf-8')
        reply.update(text=text[:MAX_REPLY_TEXT], text_truncated=len(text) > MAX_REPLY_TEXT)
    return reply


def _settle_reply(record, message, error):
    reply = record.get('reply')
    if reply is None:
        return
    reply.update(status={'succeeded': 'accepted', 'failed': 'failed',
                         'pending': 'pending'}.get(record['status'], 'unknown'),
                 receipt_present=record.get('has_receipt') is True)
    reply['evidence'] = ('tool_error' if error else 'platform_receipt' if reply['receipt_present']
                         else 'platform_rejection' if reply['status'] == 'failed'
                         else 'unconfirmed_result')
    if reply['type'] == 'private_message':
        return
    value = _result_object(message)
    if reply['receipt_present'] and value is not None:
        receipt = _comment_id(value.get('rpid_str')) or _comment_id(value.get('rpid'))
        if receipt:
            reply['rpid'] = receipt
    if record.get('video'):
        reply['video'] = dict(record['video'])


def _bili_result(record, message, error):
    kind = record['kind']
    if error:
        record['status'] = 'unknown' if kind == 'action' else 'failed'
        return
    value = _result_object(message)
    if value is None:
        record['status'] = 'unknown'
        return
    if kind == 'action' and value.get('status') in ('unknown', 'reserved', 'duplicate'):
        # ok:false on a delivery with an uncertain/previous outcome is not a
        # platform rejection and never becomes a new successful action.
        record['status'] = 'unknown'
        return
    if value.get('ok') is False or value.get('success') is False or value.get('error') or value.get('blocked') is True:
        record['status'] = 'failed'
        return
    video = _video(value)
    if video:
        if record.get('video') and video['bvid'] != record['video']['bvid']:
            record['status'] = 'unknown'
            return
        record['video'] = video
    record['status'] = 'unknown'
    if kind == 'recommendation':
        items = value.get('videos')
        if value.get('ok') is True and isinstance(items, list):
            valid = [item for item in (_video(item) for item in items) if item]
            # A malformed nonempty list is not an authoritative empty success.
            if items and not valid:
                return
            distinct = {item['bvid']: item for item in valid}
            record.update(status='succeeded', item_count=len(distinct), videos=list(distinct.values())[:8])
    elif kind == 'details':
        if video and isinstance(value.get('title'), str):
            record['status'] = 'succeeded'
    elif kind == 'subtitle':
        text, segments = value.get('text'), value.get('segments')
        count = len(segments) if isinstance(segments, list) else segments
        if video and isinstance(text, str) and text.strip() and _integer(count) and count > 0:
            record.update(status='succeeded', subtitle_chars=len(text), segment_count=count)
    elif kind == 'frames':
        frames = value.get('frames')
        count = len(frames) if isinstance(frames, list) else frames
        if value.get('ok') is True and _integer(count) and count > 0:
            record.update(status='succeeded', frame_count=count)
            if type(value.get('image_attached')) is bool:
                record['image_attached'] = value['image_attached']
    elif kind == 'cover':
        if value.get('ok') is True:
            record['status'] = 'succeeded'
            if type(value.get('image_attached')) is bool:
                record['image_attached'] = value['image_attached']
    elif kind == 'action':
        sent = (record['tool'] == 'bili_send_message' and value.get('status') == 'sent'
                and value.get('ok') is True and _receipt(value.get('receipt')))
        receipts = [_comment_id(value.get(key)) for key in ('rpid', 'rpid_str')]
        replied = (record['tool'] == 'bili_reply' and value.get('success') is True
                   and any(receipts) and len(set(filter(None, receipts))) == 1)
        if sent or replied:
            record.update(status='succeeded', has_receipt=True)
        elif value.get('status') in ('failed', 'blocked'):
            record['status'] = 'failed'


def _result_header(data):
    """Native content-only rewrites do not represent another execution."""
    rest = dict(data)
    rest['message'] = dict(data['message'], content=None)
    return json.dumps(rest, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def _native(path, session, cwd, preset, role, cutoff, now):
    raw = read_bytes(path)
    if path.suffix == '.zstd':
        try:
            import zstandard
        except ImportError:
            raise ActivityUnavailable('compression_unavailable') from None
        try:
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw), read_across_frames=True) as stream:
                raw = stream.read(MAX_EXPANDED + 1)
        except Exception:
            raise ActivityUnavailable('malformed_source') from None
    _require(len(raw) <= MAX_EXPANDED, 'source_limit')
    _require(raw.endswith(b'\n'))
    lines = raw.splitlines()
    _require(1 <= len(lines) <= MAX_ROWS + 1, 'source_limit')
    _require(all(len(line) <= MAX_LINE for line in lines), 'source_limit')
    header = _json(lines[0])
    _require(isinstance(header, dict) and header.get('type') == 'session' and header.get('version') == 4
             and type(header.get('isSeeded')) is bool)
    _require(header.get('id') == session and _workspace(header.get('cwd')) == _workspace(cwd), 'owner_mismatch')
    current = _preset(header.get('agentPreset', ''))
    calls, records, markers = {}, [], 0
    for index, line in enumerate(lines[1:]):
        event = _json(line)
        _require(isinstance(event, dict) and isinstance(event.get('type'), str) and event.get('seq') == index and _integer(event.get('seq'))
                 and _integer(event.get('time')) and isinstance(event.get('data'), dict))
        kind, data, stamp = event.get('type'), event['data'], event['time']
        if kind == 'session/end-seed' and data.get('inherited') is True:
            _require(header['isSeeded'])
            markers += 1
            calls.clear(); records.clear(); current = _preset(header.get('agentPreset', ''))
        elif kind == 'agent-preset/selected':
            current = _preset(data.get('agentPreset'))
            calls.clear()
        elif kind == 'tool/call' and current == _preset(preset):
            name, call_id = data.get('name'), data.get('callId')
            if not isinstance(name, str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_:-]{0,79}', name):
                continue
            _require(isinstance(call_id, str) and 0 < len(call_id) <= 512 and call_id not in calls)
            _require(_integer(data.get('turn')) and _integer(data.get('step')))
            record = {'id': hashlib.sha256((session + '\0' + call_id + '\0' + str(index)).encode()).hexdigest()[:32],
                      'at': stamp, 'tool': name, 'kind': BILI_TOOLS.get(name, 'tool'), 'status': 'pending', 'source': role}
            args = None
            if name in BILI_TOOLS:
                args = data.get('arguments')
                if isinstance(args, str) and len(args) <= 65536:
                    try: args = _json(args)
                    except ActivityUnavailable: args = None
                if name != 'bili_send_message':
                    video = _video({'bvid': args.get('bvid')}) if isinstance(args, dict) else None
                    if video: record['video'] = video
                reply = _reply_call(name, args)
                if reply is not None: record['reply'] = reply
            calls[call_id] = {'seq': index, 'turn': data['turn'], 'step': data['step'], 'record': record, 'done': False}
            if cutoff <= stamp <= now + 30000:
                records.append(record)
        elif kind == 'tool/result':
            message = data.get('message')
            if not isinstance(message, dict):
                continue
            source = message.get('source')
            call_id = source.get('callId') if isinstance(source, dict) else None
            call = calls.get(call_id) if isinstance(call_id, str) else None
            if call is None:
                continue
            _require(message.get('toolCallId') == call_id and data.get('step') == call['step']
                     and data.get('turn') == call['turn'])
            if call['done']:
                # dsh-session assertToolResultRewrite permits only a replacement
                # of this call's current result, with all non-content data equal.
                # The durable original still attests the actual execution;
                # context compaction neither adds an action nor erases it.
                op, sources = event.get('surfaceOp'), event.get('sourceEventSeqs')
                _require(isinstance(op, dict) and set(op) == {'op', 'startSeq', 'endSeq'}
                         and op['op'] == 'replace' and _integer(op['startSeq']) and _integer(op['endSeq'])
                         and op['startSeq'] == op['endSeq'] == call['result_seq']
                         and isinstance(sources, list) and all(_integer(seq) and seq < index for seq in sources)
                         and len(sources) == len(set(sources)) and call['result_seq'] in sources
                         and _result_header(data) == call['result_header'])
                call['result_seq'] = index
                continue
            _require(event.get('surfaceOp') == 'append' and event.get('sourceEventSeqs') == [call['seq']])
            call['done'] = True
            call['result_seq'], call['result_header'] = index, _result_header(data)
            record = call['record']
            error = message.get('isError') is True or data.get('error') is not None
            if record['tool'] in BILI_TOOLS:
                _bili_result(record, message, error)
                _settle_reply(record, message, error)
            else:
                record['status'] = 'failed' if error else 'returned'
    _require(markers == (1 if header['isSeeded'] else 0))
    _require(current == _preset(preset), 'owner_mismatch')
    return records


def _recent_videos(records, limit):
    """Directed read attempts only; recommendations and action-only IDs are not browsing."""
    videos = {}
    reads = {'details', 'subtitle', 'frames', 'cover'}
    for record in records:
        video = record.get('video')
        if not video or record['kind'] not in reads | {'action'}:
            continue
        row = videos.setdefault(video['bvid'], dict(video, at=record['at'], kinds=set(),
            statuses=set(), record_count=0, subtitle_count=0, reply_count=0))
        if not row.get('title') and video.get('title'):
            row['title'] = video['title']
        row['at'] = max(row['at'], record['at'])
        row['kinds'].add(record['kind']); row['statuses'].add(record['status'])
        row['record_count'] += 1
        row['subtitle_count'] += record['kind'] == 'subtitle' and record['status'] == 'succeeded'
        row['reply_count'] += record['tool'] == 'bili_reply'
    result = []
    for row in videos.values():
        if not row['kinds'] & reads:
            continue
        row['kinds'] = sorted(row['kinds']); row['statuses'] = sorted(row['statuses'])
        result.append(row)
    return sorted(result, key=lambda row: (row['at'], row['bvid']), reverse=True)[:limit]


def activity_snapshot(*, workspace, agent_preset, identity_file, dsh_home, mapping_file=None,
                      group_fallback='', now=None, limit=100):
    """Caller supplies trusted project/config paths; no HTTP or model inputs."""
    now = int(time.time() * 1000) if now is None else now
    _require(_integer(now) and type(limit) is int and 1 <= limit <= 200, 'invalid_configuration')
    coverage = {'configured_scopes': 0, 'mapped_scopes': 0, 'owner_verified_scopes': 0,
                'current_mapped_sessions_only': True, 'window_days': 7,
                'window_start': now - 7 * 86400000, 'truncated': False, 'reasons': []}
    records, reasons = [], []
    try:
        _workspace(workspace); _preset(agent_preset)
        home = ordinary(dsh_home)
        mapping_path = ordinary(mapping_file or home / 'media/onebot/chat-sessions.json')
        identity = _json(read_bytes(identity_file, 1024 * 1024))
        _require(isinstance(identity, dict), 'invalid_configuration')
        admin = identity.get('adminQQ')
        if isinstance(admin, list): admin = admin[0] if admin else ''
        group = identity.get('fanGroupQQ')
        if group in (None, ''): group = identity.get('fanGroup')
        if group in (None, ''): group = group_fallback
        scopes = [('primary', 'private:', admin)] + ([('social', 'group:', group)] if group not in (None, '') else [])
        _require(all(type(peer) in (str, int) and re.fullmatch('[1-9][0-9]*', str(peer)) for _, _, peer in scopes),
                 'invalid_configuration')
        coverage['configured_scopes'] = len(scopes)
        mapping = _json(read_bytes(mapping_path, 1024 * 1024))
        _require(isinstance(mapping, dict), 'mapping_invalid')
        selected = []
        for role, prefix, peer in scopes:
            entry = mapping.get(prefix + str(peer))
            if entry is None:
                reasons.append('mapping_missing'); continue
            if isinstance(entry, str): session, cwd = entry, workspace
            elif isinstance(entry, dict):
                session, cwd = entry.get('session'), entry.get('workspacePath')
                if cwd in (None, ''): cwd = workspace
            else: reasons.append('mapping_invalid'); continue
            selected.append((role, session, cwd))
        coverage['mapped_scopes'] = len(selected)
        for role, session, cwd in selected:
            try:
                _require(sum(other[1] == session for other in selected) == 1, 'mapping_ambiguous')
                _require(_workspace(cwd) == _workspace(workspace), 'owner_mismatch')
                directory = home / 'sessions' / _project(cwd) / _segment(session)
                paths = [directory / name for name in ('session.v4.jsonl', 'session.v4.jsonl.zstd')
                         if os.path.lexists(directory / name)]
                _require(len(paths) == 1, 'source_missing' if not paths else 'source_ambiguous')
                records.extend(_native(paths[0], session, cwd, agent_preset, role, now - 7 * 86400000, now))
                coverage['owner_verified_scopes'] += 1
            except ActivityUnavailable as error: reasons.append(str(error))
            except (OSError, ValueError, TypeError, UnicodeError, OverflowError): reasons.append('source_unavailable')
        # The mapping/identity must not switch while a source is being projected.
        _require(_json(read_bytes(mapping_path, 1024 * 1024)) == mapping, 'source_changing')
        _require(_json(read_bytes(identity_file, 1024 * 1024)) == identity, 'source_changing')
    except ActivityUnavailable as error:
        records.clear(); coverage['owner_verified_scopes'] = 0; reasons.append(str(error))
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError):
        records.clear(); coverage['owner_verified_scopes'] = 0; reasons.append('source_unavailable')
    dedup = {record['id']: record for record in records}
    records = sorted(dedup.values(), key=lambda item: (item['at'], item['id']), reverse=True)
    bili = [record for record in records if record['tool'] in BILI_TOOLS]
    replies = [dict(record['reply'], id=record['id'], at=record['at'], tool=record['tool'])
               for record in bili if 'reply' in record]
    coverage['bili_records_total'] = len(bili)
    coverage['reasons'] = sorted(set(reasons))[:10]
    status = ('ok' if coverage['owner_verified_scopes'] and not reasons else
              'partial' if coverage['owner_verified_scopes'] else 'unavailable')
    counts = dict(recommendation=0, details=0, subtitle=0, frames=0, cover=0, actions=0,
                  failed=0, unknown=0, pending=0)
    for record in bili:
        if record['status'] == 'succeeded':
            counts['actions' if record['kind'] == 'action' else record['kind']] += 1
        elif record['status'] in counts: counts[record['status']] += 1
    base = {'status': status, 'generated_at': now}
    return {'mainAgent': dict(base, schema='panel-bili-native/v1', records=bili[:limit], counts=counts,
                              replies=replies[:limit], recent_videos=_recent_videos(bili, limit),
                              coverage=dict(coverage, truncated=len(bili) > limit)),
            'recentActivity': dict(base, schema='panel-native-tools/v1',
                                  records=[{key: value for key, value in record.items() if key != 'reply'}
                                           for record in records[:30]],
                                  coverage=dict(coverage, truncated=len(records) > 30))}
