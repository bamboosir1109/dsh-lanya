"""Summarize local QQ phase metadata without models, messages or source text.

Transport acceptance is not QQ-client receipt. A quiet decision is not a lost
reply; missing phases and partial logs remain explicit. No automatic recovery.
"""
import argparse
import collections
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

LIMIT = 8 * 1024 * 1024 + 4096
PHASES = {'received', 'not_admitted', 'preparation_failed', 'admitted', 'admission_refused',
          'superseded_preparation', 'queued', 'claimed', 'step_ready', 'decision', 'turn_end', 'delivery', 'delivery_refused', 'blocked',
          'group_listening', 'group_buffer_wait', 'group_buffer_ready', 'reply_review_queued', 'friend_history_staged',
          'group_history_staged', 'account_publication'}
TRANSPORT_FAILURES = ('backend_rejected', 'connection_unavailable', 'ack_timeout', 'unclassified')


def latest_group_activity(log, scope, *, after, now):
    """Return only a timestamp from the bounded private bridge diagnostics."""
    if not re.fullmatch(r'group:[1-9][0-9]*', scope):
        return None
    key = hashlib.sha256(scope.encode()).hexdigest()[:16]
    latest = None
    for path in (Path(log), Path(str(log) + '.previous')):
        try:
            with open_log(path) as stream:
                offset = max(0, os.fstat(stream.fileno()).st_size - 512 * 1024)
                stream.seek(offset)
                if offset:
                    stream.readline(4096)
                raw = stream.read(512 * 1024)
        except (OSError, ValueError):
            continue
        for line in raw.splitlines():
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict) or row.get('schema') != 'qq-interaction/v1' or row.get('scope') != key:
                continue
            stamp = row.get('at')
            if (type(stamp) is int and after < stamp <= now and row.get('phase') in ('group_listening', 'admitted')
                    and isinstance(row.get('input'), str) and re.fullmatch('[a-f0-9]{24}', row['input'])):
                latest = max(latest or stamp, stamp)
    return latest


def _latency_stats(values):
    values = sorted(values)
    return dict(samples=len(values), p50_ms=values[len(values)//2] if values else None,
                p95_ms=values[min(len(values)-1, int(len(values)*.95))] if values else None)


def summarize(raw, now_ms, since_ms=None):
    if len(raw) > LIMIT:
        raise ValueError('diagnostic_log_too_large')
    inputs = {}; invalid = 0; pending = {}; claimed = set(); coalesced = set(); missing_parents = 0
    for line in raw.splitlines():
        try:
            row = json.loads(line)
            if (row.get('schema') != 'qq-interaction/v1' or row.get('phase') not in PHASES
                    or not re.fullmatch('[a-f0-9]{16}', row.get('scope', ''))
                    or not re.fullmatch('[a-f0-9]{24}', row.get('input', ''))
                    or type(row.get('at')) is not int or row['at'] <= 0):
                raise ValueError('invalid_record')
            key = (row['scope'], row['input'])
            inputs.setdefault(key, []).append(row)
            if row['phase'] == 'claimed':
                claimed.add(key); pending.pop(key, None)
            if row['phase'] == 'queued':
                if row.get('coalesced') is True:
                    parents = [candidate for candidate in pending if candidate[0] == row['scope'] and candidate != key]
                    if parents:
                        # The adapter emits this only after replacing the native
                        # pending tail. Correlation is diagnostic metadata, never
                        # authority to restore a message or a ticket.
                        parent = parents[-1]; coalesced.add(parent); pending.pop(parent)
                    else:
                        missing_parents += 1
                if key not in claimed:
                    pending[key] = True
        except (ValueError, TypeError, AttributeError):
            invalid += 1
    groups = {}
    for key, events in inputs.items():
        scope, _ = key
        phases = {e['phase'] for e in events}; last = max(e['at'] for e in events)
        if since_ms is not None and last < since_ms:
            continue
        delivery = [e for e in events if e['phase'] == 'delivery']
        publication = [e for e in events if e['phase'] == 'account_publication']
        decisions = [e for e in events if e['phase'] == 'decision']
        quiet = any(e.get('verdict') in ('idle', 'defer', 'silence') and not e.get('reason')
                    or e.get('verdict') == 'reply' and e.get('reason') == 'silent' for e in decisions)
        refused = 'delivery_refused' in phases or any(e.get('status') != 'sent' for e in delivery)
        superseded = (any(e['phase'] == 'blocked' and e.get('reason') == 'superseded_live_input' for e in events)
                      or any(e.get('verdict') == 'reply' and e.get('reason') == 'stale_revision' for e in decisions))
        status = ('delivery_uncertain' if any(e.get('status') == 'unknown' for e in delivery) else
                  'partial_transport' if refused and any(e.get('status') == 'sent' for e in delivery) else
                  'transport_accepted' if any(e.get('status') == 'sent' for e in delivery) else
                  'delivery_suppressed' if delivery else
                  'account_publication_uncertain' if any(e.get('status') == 'unknown' for e in publication) else
                  'account_publication_accepted' if any(e.get('status') == 'sent' for e in publication) else
                  'account_publication_suppressed' if publication else
                  'decided_quiet' if quiet else
                  'coalesced' if key in coalesced else
                  'superseded_turn' if superseded else
                  'blocked' if 'blocked' in phases else
                  'delivery_refused' if 'delivery_refused' in phases else
                  'preparation_failed' if 'preparation_failed' in phases else
                  'group_listening' if 'group_listening' in phases and not phases.intersection({'admitted', 'queued', 'claimed'}) else
                  'not_admitted' if 'not_admitted' in phases and not phases.intersection({'admitted', 'queued', 'claimed'}) else
                  'superseded_preparation' if 'superseded_preparation' in phases else
                  'ended_without_confirmed_transport' if 'turn_end' in phases else
                  'active' if 'claimed' in phases else 'pending' if 'queued' in phases else
                  'friend_history_staged' if 'friend_history_staged' in phases else
                  'group_history_staged' if 'group_history_staged' in phases else 'preparing')
        group = groups.setdefault(scope, {'counts': collections.Counter(), 'accepted_latency_ms': [],
                                          'backend_wait_ms': [], 'inbound_prepare_ms': [], 'long_open_inputs': 0,
                                          'transport_failures': collections.Counter()})
        group['counts'][status] += 1
        for attempt in delivery:
            failure = attempt.get('transport_failure')
            if attempt.get('status') == 'unknown' and failure in TRANSPORT_FAILURES:
                group['transport_failures'][failure] += 1
        if status in ('active', 'pending', 'preparing') and now_ms - last > 30000:
            group['long_open_inputs'] += 1
        received = [e['at'] for e in events if e['phase'] == 'received']
        accepted = [e['at'] for e in delivery if e.get('status') == 'sent']
        if received and accepted and min(accepted) >= min(received):
            group['accepted_latency_ms'].append(min(accepted) - min(received))
        admitted = next((e for e in events if e['phase'] == 'admitted'), None)
        if admitted and received and (since_ms is None or min(received) >= since_ms):
            for stage in ('backend_wait_ms', 'inbound_prepare_ms'):
                elapsed = admitted.get(stage)
                if type(elapsed) is int and 0 <= elapsed <= admitted['at'] - min(received):
                    group[stage].append(elapsed)
    result = []
    for scope, group in sorted(groups.items()):
        accepted = _latency_stats(group.pop('accepted_latency_ms'))
        stages = {stage: _latency_stats(group.pop(stage + '_ms')) for stage in ('backend_wait', 'inbound_prepare')}
        result.append(dict(scope=scope, **group, accepted_samples=accepted['samples'],
                           accepted_p50_ms=accepted['p50_ms'], accepted_p95_ms=accepted['p95_ms'],
                           intake_latency=stages))
    return dict(schema='qq-interaction-health/v1', read_only=True,
        status='partial' if invalid or missing_parents or len(raw) >= 8 * 1024 * 1024 else 'ok', invalid_records=invalid,
        missing_coalesced_parents=missing_parents, queue_correlation='ordered_phase_metadata_only',
        capacity_limited=len(raw) >= 8 * 1024 * 1024,
        input_count=sum(sum(group['counts'].values()) for group in result),
        since_ms=since_ms, scopes=result, receipt_boundary='transport_acceptance_only',
        history_replayed=False, models=0, messages=0)


def open_log(path):
    if not path.is_absolute(): raise ValueError('absolute_log_path_required')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_nlink != 1: raise ValueError('ordinary_log_required')
    f = path.open('rb')
    try:
        opened = os.fstat(f.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError('log_changed')
    except BaseException:
        f.close()
        raise
    return f


def read_log(path):
    with open_log(path) as f:
        return f.read(LIMIT+1)


def compare_friend_ingress(scopes, raw, *, account, since_ms, now_ms, history):
    """Compare bounded provider history with retained inbound diagnostics."""
    if not 0 <= now_ms - since_ms <= 24 * 60 * 60_000:
        raise ValueError('comparison_window_out_of_range')
    peers = sorted({scope for scope in scopes if re.fullmatch(r'private:[1-9][0-9]{0,15}', scope)
                    and scope[8:] != account and int(scope[8:]) <= 2**53 - 1})
    received, staged = set(), set()
    acknowledged_outputs = collections.defaultdict(set)
    partial_log = False
    for segment in raw:
        if len(segment) >= LIMIT:
            raise ValueError('diagnostic_log_too_large')
        for line in segment.splitlines():
            try:
                row = json.loads(line)
                if (not isinstance(row, dict) or row.get('schema') != 'qq-interaction/v1'
                        or row.get('phase') not in PHASES or type(row.get('at')) is not int
                        or not re.fullmatch(r'[a-f0-9]{16}', row.get('scope', ''))
                        or not re.fullmatch(r'[a-f0-9]{24}', row.get('input', ''))):
                    raise ValueError('invalid_record')
                if row.get('phase') == 'received':
                    received.add((row['scope'], row['input']))
                elif row.get('phase') == 'friend_history_staged':
                    staged.add((row['scope'], row['input']))
                elif row.get('phase') == 'delivery' and row.get('status') == 'sent' and 'output' in row:
                    output = row['output']
                    if not isinstance(output, str) or not re.fullmatch(r'[a-f0-9]{24}', output):
                        raise ValueError('invalid_output_reference')
                    acknowledged_outputs[row['scope'], row['input']].add(output)
            except (ValueError, TypeError, KeyError):
                partial_log = True

    def inspect(scope):
        peer = scope[8:]
        try:
            rows = history(peer)
        except RuntimeError:
            return {'history_unavailable': 1}
        if not isinstance(rows, list) or len(rows) > 50:
            return {'history_unavailable': 1}
        summary = collections.Counter(history_checked=1)
        seen = set()
        incoming = set()
        own_outputs = set()
        scope_key = hashlib.sha256(scope.encode()).hexdigest()[:16]
        oldest = now_ms
        for row in rows:
            if not isinstance(row, dict) or type(row.get('time')) is not int or row['time'] <= 0:
                summary['invalid_history_rows'] += 1
                continue
            at = row['time'] * 1000
            oldest = min(oldest, at)
            if at < since_ms or at > now_ms:
                continue
            sender = row.get('sender')
            if not isinstance(sender, dict):
                continue
            author = str(sender.get('user_id'))
            if author not in (peer, account):
                continue
            if not row.get('sub_type'):
                summary['unclassified_history_rows'] += 1
                continue
            if row['sub_type'] != 'friend':
                continue
            message_id = str(row.get('message_id'))
            if not re.fullmatch(r'-?[0-9]{1,16}', message_id) or abs(int(message_id)) > 2**53 - 1:
                summary['invalid_history_rows'] += 1
                continue
            input_key = hashlib.sha256((scope + '\0' + message_id).encode()).hexdigest()[:24]
            if author == account:
                if (row.get('message_type') == 'private'
                        and str(row.get('target_id', peer)) == peer and str(row.get('peer_id', peer)) == peer):
                    own_outputs.add(input_key)
                continue
            if message_id in seen:
                continue
            seen.add(message_id)
            summary['friend_inputs'] += 1
            key = (scope_key, input_key)
            incoming.add(key)
            outcome = ('matched_ingress' if key in received else 'staged_after_reconnect' if key in staged
                       else 'without_ingress_evidence')
            summary[outcome] += 1
        for key in incoming:
            outputs = acknowledged_outputs.get(key)
            if outputs:
                summary['friend_inputs_with_acknowledged_output'] += 1
                if outputs & own_outputs:
                    summary['friend_inputs_with_reply_in_history'] += 1
        if len(rows) == 50 and oldest >= since_ms:
            summary['history_window_limited'] += 1
        return summary

    totals = collections.Counter()
    with ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(inspect, peers[:32]):
            totals.update(result)
    partial = (partial_log or len(peers) > 32 or any(totals[k] for k in
               ('history_unavailable', 'invalid_history_rows', 'unclassified_history_rows', 'history_window_limited')))
    return {'schema': 'qq-friend-ingress-audit/v1', 'status': 'partial' if partial else 'ok',
            'since_ms': since_ms, 'scopes_eligible': len(peers), 'scopes_sampled': min(len(peers), 32),
            'partial_log': partial_log, 'counts': dict(totals), 'read_only': True,
            'reply_evidence': 'exact_outbound_id_in_friend_history'}


def audit_friend_ingress(path, mapping_path, since_ms, now_ms):
    from project_conf import data_dir, ConfigError
    from qq_backend import resolve, health, call, BackendError

    def config_json(file):
        with open_log(file) as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('configuration_too_large')
        return json.loads(raw.decode('utf-8-sig'))

    mapping = config_json(mapping_path)
    if not isinstance(mapping, dict) or not all(isinstance(scope, str) for scope in mapping):
        raise ValueError('mapping_invalid')
    try:
        config = config_json(Path(data_dir()) / 'lanya-config.json')
    except ConfigError:
        raise ValueError('configuration_unavailable') from None
    account = str(config.get('botQQ', '')) if isinstance(config, dict) else ''
    if not re.fullmatch('[1-9][0-9]{0,19}', account):
        raise ValueError('bot_account_unconfigured')
    try:
        connection = resolve(expected_account=account)
        status = health(connection, expected_account=account, timeout=3)
    except (BackendError, ConfigError):
        raise ValueError('backend_unavailable') from None
    if not status.get('online') or status.get('account') != account:
        raise ValueError('backend_unverified')
    previous = path.with_name(path.name + '.previous')
    try:
        segments = [read_log(previous)]
    except FileNotFoundError:
        segments = []
    segments.append(read_log(path))

    def history(peer):
        value = call('get_friend_msg_history', {'user_id': int(peer), 'count': 50},
                     connection=connection, timeout=4)['data']
        return value.get('messages') if isinstance(value, dict) else None

    return compare_friend_ingress(mapping, segments, account=account, since_ms=since_ms, now_ms=now_ms, history=history)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--log', required=True)
    p.add_argument('--previous', action='store_true', help='Read the retained previous segment')
    p.add_argument('--since-ms', type=int, help='Include inputs with a phase at or after this Unix timestamp in milliseconds')
    p.add_argument('--friend-mapping', help='Compare recent mapped friend history with both diagnostic segments')
    a = p.parse_args()
    if a.since_ms is not None and a.since_ms < 0:
        p.error('--since-ms must be nonnegative')
    path = Path(a.log)
    now_ms = int(time.time() * 1000)
    if a.friend_mapping:
        if a.previous or a.since_ms is None or not 0 <= now_ms - a.since_ms <= 24 * 60 * 60_000:
            p.error('--friend-mapping requires --since-ms within the past 24 hours')
        try:
            value = audit_friend_ingress(path, Path(a.friend_mapping), a.since_ms, now_ms)
        except (OSError, ValueError, TypeError):
            print(json.dumps({'status': 'unavailable', 'read_only': True}))
            return 1
        print(json.dumps(value))
        return 0
    if a.previous:
        path = path.with_name(path.name + '.previous')
    print(json.dumps(summarize(read_log(path), now_ms, a.since_ms), ensure_ascii=True))
    return 0


if __name__ == '__main__': raise SystemExit(main())
