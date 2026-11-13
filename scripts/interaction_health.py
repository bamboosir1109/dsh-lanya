"""Summarize local QQ phase metadata without models, messages or source text.

Transport acceptance is not QQ-client receipt. A quiet decision is not a lost
reply; missing phases and partial logs remain explicit. No automatic recovery.
"""
import argparse
import collections
import json
import os
from pathlib import Path
import re
import stat
import time

LIMIT = 8 * 1024 * 1024 + 4096
PHASES = {'received', 'not_admitted', 'preparation_failed', 'admitted', 'admission_refused',
          'superseded_preparation', 'queued', 'claimed', 'step_ready', 'decision', 'turn_end', 'delivery', 'delivery_refused', 'blocked',
          'group_listening', 'group_buffer_wait', 'group_buffer_ready', 'reply_review_queued'}


def summarize(raw, now_ms):
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
        delivery = [e for e in events if e['phase'] == 'delivery']
        decisions = [e for e in events if e['phase'] == 'decision']
        quiet = any(e.get('verdict') in ('idle', 'defer', 'silence') and not e.get('reason')
                    or e.get('verdict') == 'reply' and e.get('reason') == 'silent' for e in decisions)
        refused = 'delivery_refused' in phases or any(e.get('status') != 'sent' for e in delivery)
        superseded = any(e['phase'] == 'blocked' and e.get('reason') == 'superseded_live_input' for e in events)
        status = ('delivery_uncertain' if any(e.get('status') == 'unknown' for e in delivery) else
                  'partial_transport' if refused and any(e.get('status') == 'sent' for e in delivery) else
                  'transport_accepted' if any(e.get('status') == 'sent' for e in delivery) else
                  'delivery_suppressed' if delivery else
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
                  'active' if 'claimed' in phases else 'pending' if 'queued' in phases else 'preparing')
        group = groups.setdefault(scope, {'counts': collections.Counter(), 'accepted_latency_ms': [], 'long_open_inputs': 0})
        group['counts'][status] += 1
        if status in ('active', 'pending', 'preparing') and now_ms - last > 30000:
            group['long_open_inputs'] += 1
        received = [e['at'] for e in events if e['phase'] == 'received']
        accepted = [e['at'] for e in delivery if e.get('status') == 'sent']
        if received and accepted and min(accepted) >= min(received):
            group['accepted_latency_ms'].append(min(accepted) - min(received))
    result = []
    for scope, group in sorted(groups.items()):
        values = sorted(group.pop('accepted_latency_ms'))
        result.append(dict(scope=scope, **group, accepted_samples=len(values),
            accepted_p50_ms=values[len(values)//2] if values else None,
            accepted_p95_ms=values[min(len(values)-1, int(len(values)*.95))] if values else None))
    return dict(schema='qq-interaction-health/v1', read_only=True,
        status='partial' if invalid or missing_parents or len(raw) >= 8 * 1024 * 1024 else 'ok', invalid_records=invalid,
        missing_coalesced_parents=missing_parents, queue_correlation='ordered_phase_metadata_only',
        capacity_limited=len(raw) >= 8 * 1024 * 1024,
        input_count=len(inputs), scopes=result, receipt_boundary='transport_acceptance_only',
        history_replayed=False, models=0, messages=0)


def read_log(path):
    if not path.is_absolute(): raise ValueError('absolute_log_path_required')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_nlink != 1: raise ValueError('ordinary_log_required')
    with path.open('rb') as f:
        opened = os.fstat(f.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino): raise ValueError('log_changed')
        return f.read(LIMIT+1)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--log', required=True)
    p.add_argument('--previous', action='store_true', help='Read the retained previous segment')
    a = p.parse_args()
    path = Path(a.log)
    if a.previous:
        path = path.with_name(path.name + '.previous')
    print(json.dumps(summarize(read_log(path), int(time.time()*1000)), ensure_ascii=True))


if __name__ == '__main__': main()
