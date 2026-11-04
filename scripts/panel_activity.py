"""Bounded read-only projections of native tool events for the local panel.

Only configured conversations' current mapping is read. Chat text, reasoning,
raw arguments, session IDs and private recipients never enter output. Public
video metadata, bounded actual subtitle excerpts and public comment attempts
are explicit allowlists; platform receipts do not establish public visibility.
Compatibility tool records omit replies; episode evidence may contain public
comments. Private-message text is never projected.
This is a local runtime record, not proof of playback or recipient visibility.
"""
import hashlib
import contextlib
import datetime as dt
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import tempfile

MAX_SOURCE = 32 * 1024 * 1024
MAX_NATIVE_SOURCE = 256 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
MAX_LINE = 4 * 1024 * 1024
MAX_ROWS = 200000
MAX_RESULT = 2 * 1024 * 1024
MAX_REPLY_TEXT = 2000
MAX_CONTENT_EXCERPT = 1800
MAX_EPISODE_EVIDENCE = 40
SAFE_INTEGER = 9007199254740991
BILI_TOOLS = {
    'bili_recommend': 'recommendation', 'bili_run': 'recommendation',
    'bili_video_info': 'details', 'bili_subtitle': 'subtitle',
    'bili_observe_video': 'observation', 'bili_social_inbox': 'social_inbox',
    'bili_thread': 'comment_thread',
    'bili_frames': 'frames', 'bili_cover': 'cover',
    'bili_reply': 'action', 'bili_send_message': 'action',
}
TOOL_LABELS = {
    'recommendation': '读取B站推荐', 'details': '读取视频资料', 'subtitle': '读取视频字幕',
    'frames': '提取视频画面', 'cover': '读取视频封面', 'action': '尝试平台操作',
    'observation': '读取视频片段内容', 'social_inbox': '查看B站回复与提及',
    'comment_thread': '读取B站评论线程',
}
NAMED_TOOL_LABELS = {
    'pwsh': '运行PowerShell工具', 'exec': '运行本地工具', 'bash': '运行本地工具',
    'read': '读取文件', 'view': '查看文件或画面', 'read_image': '查看图片或截图',
    'write': '写入文件请求', 'edit': '编辑文件请求', 'glob': '查找文件', 'grep': '搜索文件内容',
    'send_meme': '挑选表情图片', 'qq_send_image': 'QQ发图尝试',
    'qq_send_message': 'QQ消息发送尝试', 'qq_send_text': 'QQ消息发送尝试',
    'bili_comments': '读取B站公开评论', 'bili_search': '搜索B站内容',
    'browser_open': '打开浏览器页面', 'browser_screenshot': '获取浏览器截图',
    'browser_snapshot': '读取浏览器页面', 'browser_click': '点击浏览器页面',
    'android_screenshot': '获取手机截图', 'android_tap': '点击手机界面',
}
DECISION_VERDICTS = {'reply', 'idle', 'defer', 'tool_action'}
HOST_CONTEXT_KINDS = {'agent-instructions', 'runtime-context', 'repeat-tool-reminder', 'compact-checkpoint'}
CHINA = dt.timezone(dt.timedelta(hours=8))


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
    # These are public video metadata fields, never a UID or a private sender.
    author = value.get('author')
    if not isinstance(author, str):
        owner = value.get('owner')
        author = owner.get('name') if isinstance(owner, dict) else None
    if isinstance(author, str) and author.strip():
        result['author'] = ''.join(char for char in author[:120] if ord(char) >= 32 and ord(char) != 127)
    return result


def _excerpt(text):
    if not isinstance(text, str):
        return {}
    clean = ''.join(char for char in text if ord(char) >= 32 or char in '\n\t').strip()
    return {'content_excerpt': clean[:MAX_CONTENT_EXCERPT],
            'content_truncated': len(clean) > MAX_CONTENT_EXCERPT}


def _arguments(data):
    value = data.get('arguments')
    if isinstance(value, str) and len(value) <= 65536:
        try:
            value = _json(value)
        except ActivityUnavailable:
            return None
    return value if isinstance(value, dict) else None


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
    if type(args.get('mention_admin')) is bool:
        reply['mention_admin_requested'] = args['mention_admin']
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
        mention = value.get('mention')
        if (isinstance(mention, dict) and type(mention.get('requested')) is bool
                and type(mention.get('applied')) is bool
                and mention['requested'] == reply.get('mention_admin_requested', False)):
            reply['mention_applied'] = mention['requested'] and mention['applied']
    if record.get('video'):
        reply['video'] = dict(record['video'])


def _seconds(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= SAFE_INTEGER


def _content_observation(record, value):
    if (value.get('ok') is not True or value.get('schema') != 'dsh-bili-video-content/v1'
            or value.get('status') not in ('observed', 'partial')
            or not _integer(value.get('page')) or not 1 <= value['page'] <= 1000
            or not _seconds(value.get('duration'))):
        return
    subtitle = value.get('subtitle')
    if not isinstance(subtitle, dict) or subtitle.get('status') not in ('available', 'unavailable', 'failed'):
        return
    segments = subtitle.get('segments')
    if not isinstance(segments, list) or len(segments) > 80 or type(subtitle.get('truncated')) is not bool:
        return
    if any(not isinstance(row, dict) or not _seconds(row.get('from')) or not _seconds(row.get('to'))
           or not row['from'] < row['to'] <= value['duration'] or not isinstance(row.get('text'), str)
           or not row['text'].strip() for row in segments):
        return
    if (subtitle['status'] == 'available') != bool(segments) or sum(len(row['text']) for row in segments) > 12000:
        return
    record.update(status='succeeded', page=value['page'], subtitle_status=subtitle['status'],
                  segment_count=len(segments), content_partial=value['status'] == 'partial')
    if segments:
        text = '\n'.join('[%g–%g秒] %s' % (row['from'], row['to'], row['text']) for row in segments)
        record.update(_excerpt(text), subtitle_chars=sum(len(row['text']) for row in segments),
                      segment_range={'start_sec': min(row['from'] for row in segments),
                                     'end_sec': max(row['to'] for row in segments)})
        record['content_truncated'] |= subtitle['truncated']


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
            distinct = {}
            for item in valid:
                distinct.setdefault(item['bvid'], {}).update(item)
            record.update(status='succeeded', item_count=len(distinct), videos=list(distinct.values())[:8])
    elif kind == 'details':
        if video and isinstance(value.get('title'), str):
            record['status'] = 'succeeded'
    elif kind == 'subtitle':
        text, segments = value.get('text'), value.get('segments')
        count = len(segments) if isinstance(segments, list) else segments
        if video and isinstance(text, str) and text.strip() and _integer(count) and count > 0:
            record.update(status='succeeded', subtitle_chars=len(text), segment_count=count)
            record.update(_excerpt(text))
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
    elif kind == 'observation':
        if video:
            _content_observation(record, value)
    elif kind in ('social_inbox', 'comment_thread'):
        items = value.get('items' if kind == 'social_inbox' else 'comments')
        if value.get('ok') is True and isinstance(items, list) and len(items) <= 21:
            record.update(status='succeeded', item_count=len(items))
    elif kind == 'action':
        sent = (record['tool'] == 'bili_send_message' and value.get('status') == 'sent'
                and value.get('ok') is True and _receipt(value.get('receipt')))
        receipts = [_comment_id(value.get(key)) for key in ('rpid', 'rpid_str')]
        replied = (record['tool'] == 'bili_reply' and (value.get('success') is True or
                   value.get('ok') is True and value.get('status') == 'accepted' and
                   _comment_id(value.get('receipt')) in receipts)
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


@contextlib.contextmanager
def _native_snapshot(path):
    """Copy one stable bounded source, spilling past 1MiB to a temporary file.

    The live handle closes before JSON parsing, so later appends do not require
    an otherwise active conversation to stay idle throughout the projection.
    The temporary snapshot is private to this read and closes on every exit.
    """
    path = ordinary(path)
    before = path.stat()
    _require(stat.S_ISREG(before.st_mode) and before.st_size <= MAX_NATIVE_SOURCE, 'source_limit')
    signature = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns)
    with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode='w+b') as snapshot:
        with path.open('rb') as raw:
            _require(signature(os.fstat(raw.fileno())) == signature(before), 'source_changing')
            remaining = before.st_size
            while remaining:
                chunk = raw.read(min(64 * 1024, remaining))
                _require(chunk, 'source_changing')
                snapshot.write(chunk)
                remaining -= len(chunk)
            _require(signature(os.fstat(raw.fileno())) == signature(before)
                     and signature(ordinary(path).stat()) == signature(before), 'source_changing')
        snapshot.seek(0)
        yield snapshot


@contextlib.contextmanager
def _native_lines(path):
    """Stream lines of a captured source, including concatenated zstd frames."""
    with _native_snapshot(path) as snapshot, contextlib.ExitStack() as stack:
        stream = snapshot
        if path.suffix == '.zstd':
            try:
                import zstandard
            except ImportError:
                raise ActivityUnavailable('compression_unavailable') from None
            stream = stack.enter_context(io.BufferedReader(
                zstandard.ZstdDecompressor().stream_reader(snapshot, read_across_frames=True, closefd=False)))

        def lines():
            total, count = 0, 0
            while True:
                try:
                    line = stream.readline(MAX_LINE + 2)
                except Exception as error:
                    raise ActivityUnavailable('malformed_source') from error
                if not line:
                    break
                total += len(line); count += 1
                _require(total <= MAX_EXPANDED and count <= MAX_ROWS + 1, 'source_limit')
                _require(len(line) <= MAX_LINE + 2, 'source_limit')
                _require(line.endswith(b'\n'), 'source_limit' if len(line) > MAX_LINE else 'malformed_source')
                line = line[:-1]
                if line.endswith(b'\r'):
                    line = line[:-1]
                _require(len(line) <= MAX_LINE, 'source_limit')
                yield line

        yield iter(lines())


def _native(path, session, cwd, preset, role, cutoff, now, *, window_end=None, metadata=None):
    with _native_lines(path) as lines:
        return _native_events(lines, session, cwd, preset, role, cutoff, now,
                              window_end=window_end, metadata=metadata)


def _native_events(lines, session, cwd, preset, role, cutoff, now, *, window_end=None, metadata=None):
    first = next(lines, None)
    _require(first is not None)
    header = _json(first)
    _require(isinstance(header, dict) and header.get('type') == 'session' and header.get('version') == 4
             and type(header.get('isSeeded')) is bool)
    _require(header.get('id') == session and _workspace(header.get('cwd')) == _workspace(cwd), 'owner_mismatch')
    current = _preset(header.get('agentPreset', ''))
    calls, records, markers = {}, [], 0
    active_turn = None
    available_from = None
    for index, line in enumerate(lines):
        event = _json(line)
        _require(isinstance(event, dict) and isinstance(event.get('type'), str) and event.get('seq') == index and _integer(event.get('seq'))
                 and _integer(event.get('time')) and isinstance(event.get('data'), dict))
        kind, data, stamp = event.get('type'), event['data'], event['time']
        if available_from is None:
            available_from = stamp
        if kind == 'session/end-seed' and data.get('inherited') is True:
            _require(header['isSeeded'])
            markers += 1
            calls.clear(); records.clear(); current = _preset(header.get('agentPreset', ''))
            available_from = stamp
            active_turn = None
        elif kind == 'agent-preset/selected':
            current = _preset(data.get('agentPreset'))
            calls.clear()
            active_turn = None
        elif kind == 'turn/start' and current == _preset(preset):
            _require(_integer(data.get('turn')) and active_turn is None)
            active_turn = {'turn': data['turn'], 'inputs': [], 'records': [], 'at': stamp}
        elif kind == 'user/message' and active_turn is not None:
            # Origin is native transport metadata only. Never inspect content,
            # quoted text or a model's explanation to infer an autonomous turn.
            source = data.get('source')
            origin = source.get('kind', '') if isinstance(source, dict) else ''
            if origin not in HOST_CONTEXT_KINDS:
                active_turn['inputs'].append(origin)
        elif kind == 'turn/end' and current == _preset(preset):
            _require(active_turn is not None and _integer(data.get('turn')) and data['turn'] == active_turn['turn'] and stamp >= active_turn['at'])
            inputs = active_turn['inputs']
            origin = ({'plugin:dsh-lanya-observation': 'observation', 'plugin:dsh-onebot': 'live'}
                      .get(inputs[0], 'unknown') if len(inputs) == 1 else 'unknown')
            for record in active_turn['records']:
                record['trigger_origin'] = origin
            active_turn = None
        elif kind == 'tool/call' and current == _preset(preset):
            name, call_id = data.get('name'), data.get('callId')
            if not isinstance(name, str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_:-]{0,79}', name):
                continue
            _require(isinstance(call_id, str) and 0 < len(call_id) <= 512 and call_id not in calls)
            _require(_integer(data.get('turn')) and _integer(data.get('step')))
            record = {'id': hashlib.sha256((session + '\0' + call_id + '\0' + str(index)).encode()).hexdigest()[:32],
                      'at': stamp, 'tool': name, 'kind': BILI_TOOLS.get(name, 'tool'), 'status': 'pending', 'source': role,
                      'trigger_origin': 'unknown'}
            if active_turn is not None:
                _require(active_turn['turn'] == data['turn'])
                active_turn['records'].append(record)
            record['_episode_id'] = hashlib.sha256((session + '\0turn\0' + str(data['turn'])).encode()).hexdigest()[:32]
            args = None
            if name in BILI_TOOLS:
                args = _arguments(data)
                if name != 'bili_send_message':
                    video = _video({'bvid': args.get('bvid')}) if isinstance(args, dict) else None
                    if video: record['video'] = video
                reply = _reply_call(name, args)
                if reply is not None: record['reply'] = reply
            elif name == 'qq_decide':
                args = _arguments(data)
                if args and args.get('verdict') in DECISION_VERDICTS:
                    record['verdict'] = args['verdict']
            calls[call_id] = {'seq': index, 'turn': data['turn'], 'step': data['step'], 'record': record, 'done': False}
            if cutoff <= stamp <= now + 30000 and (window_end is None or stamp < window_end):
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
            record['completed_at'] = stamp
            error = message.get('isError') is True or data.get('error') is not None
            if record['tool'] in BILI_TOOLS:
                _bili_result(record, message, error)
                _settle_reply(record, message, error)
            else:
                record['status'] = 'failed' if error else 'returned'
                if record['tool'] == 'qq_decide' and not error:
                    value = _result_object(message)
                    if value and type(value.get('accepted')) is bool:
                        record['status'] = 'accepted' if value['accepted'] else 'rejected'
    _require(markers == (1 if header['isSeeded'] else 0))
    _require(current == _preset(preset), 'owner_mismatch')
    if metadata is not None:
        metadata['available_from'] = available_from
        metadata['binding_sha256'] = hashlib.sha256(json.dumps(
            [session, _workspace(cwd), _preset(preset)], ensure_ascii=True,
            separators=(',', ':')).encode('utf-8')).hexdigest()
    return records


def _recent_videos(records, limit):
    """Directed read attempts only; recommendations and action-only IDs are not browsing."""
    videos = {}
    reads = {'details', 'subtitle', 'frames', 'cover', 'observation'}
    for record in records:
        video = record.get('video')
        if not video or record['kind'] not in reads | {'action'}:
            continue
        row = videos.setdefault(video['bvid'], dict(video, at=record['at'], kinds=set(),
            statuses=set(), record_count=0, subtitle_count=0, reply_count=0))
        if not row.get('title') and video.get('title'):
            row['title'] = video['title']
        if not row.get('author') and video.get('author'):
            row['author'] = video['author']
        if (record.get('content_excerpt') and record['status'] == 'succeeded' and
                record['at'] >= row.get('_excerpt_at', -1)):
            row.update(content_excerpt=record['content_excerpt'], content_truncated=record['content_truncated'],
                       _excerpt_at=record['at'])
        row['at'] = max(row['at'], record['at'])
        row['kinds'].add(record['kind']); row['statuses'].add(record['status'])
        row['record_count'] += 1
        row['subtitle_count'] += (record['kind'] == 'subtitle' or
                                  record['kind'] == 'observation' and record.get('subtitle_status') == 'available') and record['status'] == 'succeeded'
        row['reply_count'] += record['tool'] == 'bili_reply'
    result = []
    for row in videos.values():
        if not row['kinds'] & reads:
            continue
        row.pop('_excerpt_at', None)
        row['kinds'] = sorted(row['kinds']); row['statuses'] = sorted(row['statuses'])
        result.append(row)
    return sorted(result, key=lambda row: (row['at'], row['bvid']), reverse=True)[:limit]


def _public_record(record, *, reply=True):
    return {key: value for key, value in record.items()
            if not key.startswith('_') and (reply or key != 'reply')}


def _evidence(record):
    item = _public_record(record)
    item['label'] = TOOL_LABELS.get(record['kind'], NAMED_TOOL_LABELS.get(record['tool'], '调用工具 ' + record['tool']))
    states = {'succeeded': '已取得工具确认的内容', 'returned': '工具已返回，具体结果未核验',
              'failed': '工具明确失败', 'pending': '等待工具回执', 'unknown': '结果未确认'}
    item['summary'] = states.get(record['status'], '结果未确认')
    if record['kind'] in ('details', 'subtitle', 'frames', 'cover', 'observation'):
        item['summary'] += '；不代表完整播放'
    if record['tool'] == 'send_meme':
        item['summary'] = '挑图工具' + ('已返回；不代表已发送图片' if record['status'] == 'returned' else '结果未确认')
    if record['tool'] in ('qq_send_image', 'qq_send_message', 'qq_send_text'):
        item['summary'] += '；未核验 QQ 客户端送达'
    return item


def _episodes(records, limit):
    groups, decisions = {}, []
    for record in records:
        if record['tool'] == 'qq_decide':
            decisions.append({key: record[key] for key in ('id', 'at', 'status', 'verdict', 'trigger_origin') if key in record})
            continue
        groups.setdefault(record['_episode_id'], []).append(record)
    episodes = []
    for key, rows in groups.items():
        rows.sort(key=lambda row: (row['at'], row['id']))
        bili = [row for row in rows if row['tool'] in BILI_TOOLS]
        successful = [row for row in bili if row['status'] == 'succeeded']
        videos = _recent_videos(bili, 20)
        states = {row['status'] for row in rows}
        status = ('partial' if len(states) > 1 and states & {'failed', 'pending', 'unknown'} else
                  'failed' if states == {'failed'} else 'observed' if successful else
                  'pending' if 'pending' in states else 'unknown' if 'unknown' in states else 'returned')
        title = '工具活动'
        if bili:
            title = 'B站活动'
            successful_reads = [row for row in successful if row['kind'] in {'subtitle', 'details', 'frames', 'cover', 'observation'} and row.get('video')]
            if successful_reads:
                selected = next((row for row in reversed(successful_reads) if row['kind'] == 'subtitle'), successful_reads[-1])
                video = next((item for item in videos if item['bvid'] == selected['video']['bvid']), selected['video'])
                title = ('读取B站：' if selected['kind'] == 'subtitle' else 'B站资料：') + (video.get('title') or video['bvid'])
            elif any(row['kind'] == 'recommendation' for row in bili):
                title = '读取B站推荐'
            elif any(row['kind'] == 'action' for row in bili):
                title = 'B站操作回执'
        else:
            names = {row['tool'] for row in rows}
            if names & {'send_meme', 'qq_send_image'}:
                title = '表情图片与QQ发送尝试'
            elif names <= {'read_image', 'view'}:
                title = '查看图片或截图'
            elif names & {'pwsh', 'exec', 'bash'}:
                title = '运行本地工具与查看结果'
            elif names & {'read', 'write', 'edit', 'glob', 'grep'}:
                title = '本地文件操作记录'
            else:
                title = ' · '.join(NAMED_TOOL_LABELS.get(name, '调用工具 ' + name) for name in sorted(names)[:2])
        counts = {}
        for row in rows:
            label = TOOL_LABELS.get(row['kind'], NAMED_TOOL_LABELS.get(row['tool'], '调用工具 ' + row['tool']))
            counts[label] = counts.get(label, 0) + 1
        start = min(row['at'] for row in rows)
        episodes.append({'id': key, 'day': dt.datetime.fromtimestamp(start / 1000, CHINA).date().isoformat(),
                         'started_at': start, 'updated_at': max(row.get('completed_at', row['at']) for row in rows),
                         'title': title, 'status': status, 'activity_kind': 'bili' if bili else 'tools',
                         'trigger_origin': rows[0]['trigger_origin'], 'conversation_role': rows[0]['source'],
                         'summary': ' · '.join('%s %d 次' % item for item in counts.items()),
                         'tool_names': sorted({row['tool'] for row in rows}), 'turn_count': 1,
                         'videos': videos, 'evidence': [_evidence(row) for row in rows[:MAX_EPISODE_EVIDENCE]],
                         'evidence_total': len(rows), 'evidence_truncated': len(rows) > MAX_EPISODE_EVIDENCE})
    episodes.sort(key=lambda row: (row['started_at'], row['id']), reverse=True)
    decisions.sort(key=lambda row: (row['at'], row['id']), reverse=True)
    return episodes[:limit], decisions[:30], len(episodes), len(decisions)


def _activity_projection(*, workspace, agent_preset, identity_file, dsh_home, mapping_file=None,
                         group_fallback='', now=None, limit=100, window_start=None, window_end=None,
                         collect_records=False):
    """Caller supplies trusted project/config paths; no HTTP or model inputs."""
    now = int(time.time() * 1000) if now is None else now
    _require(_integer(now) and type(limit) is int and 1 <= limit <= 2000, 'invalid_configuration')
    start = now - 7 * 86400000 if window_start is None else window_start
    _require(_integer(start) and (window_end is None or _integer(window_end) and window_end > start), 'invalid_configuration')
    coverage = {'configured_scopes': 0, 'mapped_scopes': 0, 'owner_verified_scopes': 0,
                'current_mapped_sessions_only': True, 'window_days': 7,
                'window_start': start, 'window_end': window_end or now, 'truncated': False, 'reasons': [],
                'source_windows': [], 'date_basis': 'tool_call_started_at',
                'scope': 'current_configured_admin_and_group_native_tools',
                'chat_text_included': False}
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
                source_meta = {}
                records.extend(_native(paths[0], session, cwd, agent_preset, role, start, now,
                                       window_end=window_end, metadata=source_meta))
                coverage['source_windows'].append(dict(source=role, **source_meta))
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
    if collect_records:
        return records, coverage, status, now
    counts = dict(recommendation=0, details=0, subtitle=0, frames=0, cover=0, actions=0,
                  observation=0, social_inbox=0, comment_thread=0,
                  failed=0, unknown=0, pending=0)
    for record in bili:
        if record['status'] == 'succeeded':
            counts['actions' if record['kind'] == 'action' else record['kind']] += 1
        elif record['status'] in counts: counts[record['status']] += 1
    base = {'status': status, 'generated_at': now}
    episodes, decisions, episode_total, decision_total = _episodes(records, limit)
    return {'mainAgent': dict(base, schema='panel-bili-native/v1', records=[_public_record(row) for row in bili[:limit]], counts=counts,
                              replies=replies[:limit], recent_videos=_recent_videos(bili, limit),
                              coverage=dict(coverage, truncated=len(bili) > limit)),
            'recentActivity': dict(base, schema='panel-native-tools/v1',
                                  records=[_public_record(row, reply=False) for row in records[:30]],
                                  episodes=episodes, decisions=decisions,
                                  counts=dict(episodes=episode_total, decisions=decision_total,
                                              tools=len(records) - decision_total),
                                  coverage=dict(coverage, truncated=episode_total > limit,
                                                records_truncated=len(records) > 30,
                                                decisions_truncated=decision_total > 30))}


def activity_snapshot(*, workspace, agent_preset, identity_file, dsh_home, mapping_file=None,
                      group_fallback='', now=None, limit=100):
    """Current mapped native activity during the most recent seven days."""
    _require(type(limit) is int and 1 <= limit <= 200, 'invalid_configuration')
    return _activity_projection(workspace=workspace, agent_preset=agent_preset, identity_file=identity_file,
                                dsh_home=dsh_home, mapping_file=mapping_file, group_fallback=group_fallback,
                                now=now, limit=limit)


def _daily_view(records, coverage, status, now, day, start, end, limit):
    rows = [row for row in records if start <= row['at'] < end]
    episodes, decisions, episode_total, decision_total = _episodes(rows, limit)
    coverage = dict(coverage, window_days=1, window_start=start, window_end=end,
                    day_closed=now >= end, all_account_history_complete=False,
                    truncated=episode_total > limit, decisions_truncated=decision_total > 30,
                    bili_records_total=sum(row['tool'] in BILI_TOOLS for row in rows))
    return {'schema': 'panel-activity-daily/v1', 'day': day, 'status': status, 'generated_at': now,
            'episodes': episodes, 'decisions': decisions,
            'counts': {'episodes': episode_total, 'decisions': decision_total, 'tools': len(rows) - decision_total},
            'coverage': coverage}


def daily_facts(*, day, now=None, limit=2000, **options):
    """Exact Beijing-day projection using the same bounded, trusted reader.

    Current mappings are the only source. A closed date is not evidence that
    replaced/missing sessions or an entire account's day were recovered.
    """
    now = int(time.time() * 1000) if now is None else now
    _require(_integer(now) and isinstance(day, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', day), 'invalid_configuration')
    try:
        date = dt.date.fromisoformat(day)
        start = int(dt.datetime.combine(date, dt.time(), CHINA).timestamp() * 1000)
    except (ValueError, OverflowError):
        raise ActivityUnavailable('invalid_configuration') from None
    _require(0 <= start <= now, 'invalid_configuration')
    end = start + 86400000
    records, coverage, status, now = _activity_projection(now=now, limit=limit, window_start=start,
                                                         window_end=end, collect_records=True, **options)
    return _daily_view(records, coverage, status, now, day, start, end, limit)


def daily_catalog(*, now=None, limit=2000, **options):
    """All dates present in current trusted native logs, parsed only once.

    ``facts`` has the same per-date contract as daily_facts. It intentionally
    does not discover archives, other mappings, arbitrary files or old owners.
    """
    records, coverage, status, now = _activity_projection(now=now, limit=limit, window_start=0,
                                                         collect_records=True, **options)
    buckets = {}
    today = dt.datetime.fromtimestamp(now / 1000, CHINA).date()
    future_count = 0
    for row in records:
        date = dt.datetime.fromtimestamp(row['at'] / 1000, CHINA).date()
        if date > today:
            future_count += 1
            continue
        buckets.setdefault(date, []).append(row)
    buckets.setdefault(today, [])
    facts = []
    for date, rows in sorted(buckets.items()):
        start = int(dt.datetime.combine(date, dt.time(), CHINA).timestamp() * 1000)
        facts.append(_daily_view(rows, coverage, status, now, date.isoformat(), start, start + 86400000, limit))
    return {'schema': 'panel-activity-catalog/v1', 'status': status, 'generated_at': now,
            'days': [row['day'] for row in facts], 'facts': facts,
            'coverage': dict(coverage, future_day_records_omitted=future_count)}
