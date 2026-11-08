"""Read-only daily projections of trusted native turns and the budget ledger.

The report never repairs, reclassifies or settles the budget gate. Its amounts
are API estimates within each source's coverage, not provider invoices. Native
daily reports and gate usage remain separate; neither is added to the other.
An activity count, a session total and a reservation are never spending.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

import budget_runtime as budget

SCHEMA = 'daily-spending/v2'
TIMEZONE = dt.timezone(dt.timedelta(hours=8), 'Asia/Shanghai')
SAFE_INTEGER = 9007199254740991
_NATIVE_CACHE = {}
_NATIVE_LOCK = threading.Lock()


def _require(value):
    if not value:
        raise budget.BudgetError('invalid_spending_report')


def _integer(value):
    return type(value) is int and 0 <= value <= SAFE_INTEGER


def _date(stamp):
    return dt.datetime.fromtimestamp(stamp / 1000, TIMEZONE).date()


def _start(day):
    return int(dt.datetime.combine(day, dt.time(), TIMEZONE).timestamp() * 1000)


def _empty_bucket():
    return {'spent_cny': None, 'reserved_cny': 0, 'turn_count': 0,
            'settled_count': 0, 'pending_count': 0, 'unknown_count': 0}


def daily_spending(now=None, days=30):
    """Read one owner-scoped SQLite snapshot; no source/config/DB writes.

    A turn belongs to its reservation/start day in Asia/Shanghai, even when it
    settles after midnight. Historical days preceding tracking remain null.
    'complete' means complete within this ledger's stated scope, never all
    account traffic. Today's amount remains a partial day.
    """
    now = int(time.time() * 1000) if now is None else now
    _require(_integer(now) and type(days) is int and 1 <= days <= 90)
    today = _date(now)
    first = today - dt.timedelta(days=days - 1)
    result = []
    for offset in range(days):
        day = first + dt.timedelta(days=offset)
        result.append({'date': day.isoformat(), 'autonomy': None, 'private': None,
                       'group': None, 'total': None, 'status': 'unknown',
                       'turn_count': 0, 'settled_count': 0, 'pending_count': 0,
                       'unknown_count': 0, 'reserved_cny': 0,
                       'buckets': {key: _empty_bucket() for key in budget.BUCKETS}})
    by_day = {row['date']: row for row in result}
    coverage_start = None
    owner_verified = False
    _, path = budget._paths()
    if os.path.lexists(path):
        with budget._transaction() as connection:
            owner = budget._configured_owner_hash()
            record = connection.execute("SELECT value FROM budget_meta WHERE key='coverage_start'").fetchone()
            if record is not None:
                _require(isinstance(record[0], str) and re.fullmatch('[0-9]{1,16}', record[0]))
                coverage_start = int(record[0])
                _require(_integer(coverage_start) and coverage_start <= now + 30000)
            first_owned = connection.execute(
                'SELECT MIN(created_at) FROM budget_turns WHERE owner_hash=?', (owner,)).fetchone()[0]
            if first_owned is None:
                coverage_start = None
            else:
                _require(_integer(first_owned) and coverage_start is not None and first_owned >= coverage_start)
                coverage_start = first_owned
            owner_verified = True
            for item in connection.execute(
                    'SELECT category,status,reserve_micros,spent_micros,created_at,updated_at,receipt_hash '
                    'FROM budget_turns WHERE owner_hash=? AND created_at>=? AND created_at<?',
                    (owner, _start(first), _start(today + dt.timedelta(days=1)))):
                _require(item['category'] in budget.BUCKETS and
                         item['status'] in ('reserved', 'settled', 'unknown') and
                         _integer(item['created_at']) and _integer(item['updated_at']) and
                         item['updated_at'] >= item['created_at'] and
                         _integer(item['reserve_micros']) and coverage_start is not None and
                         item['created_at'] >= coverage_start)
                if item['created_at'] > now:
                    continue
                row = by_day[_date(item['created_at']).isoformat()]
                bucket = row['buckets'][item['category']]
                bucket['turn_count'] += 1
                if item['status'] == 'settled':
                    _require(_integer(item['spent_micros']) and isinstance(item['receipt_hash'], str)
                             and re.fullmatch('[a-f0-9]{64}', item['receipt_hash']))
                    bucket['settled_count'] += 1
                    bucket['_micros'] = bucket.get('_micros', 0) + item['spent_micros']
                else:
                    _require(item['spent_micros'] is None and item['receipt_hash'] is None)
                    bucket['pending_count' if item['status'] == 'reserved' else 'unknown_count'] += 1
                    bucket['_reserved'] = bucket.get('_reserved', 0) + item['reserve_micros']
    for row in result:
        day = dt.date.fromisoformat(row['date'])
        covered = coverage_start is not None and coverage_start < _start(day + dt.timedelta(days=1))
        for key, bucket in row['buckets'].items():
            micros = bucket.pop('_micros', 0)
            bucket['reserved_cny'] = bucket.pop('_reserved', 0) / 1000000
            if covered:
                bucket['spent_cny'] = micros / 1000000
                row[key] = bucket['spent_cny']
            for field in ('turn_count', 'settled_count', 'pending_count', 'unknown_count'):
                row[field] += bucket[field]
            row['reserved_cny'] += bucket['reserved_cny']
        if covered:
            row['total'] = round(sum(row[key] for key in budget.BUCKETS), 6)
            row['status'] = ('complete' if coverage_start <= _start(day) and day < today
                             and not row['pending_count'] and not row['unknown_count'] else 'partial')
        row['reserved_cny'] = round(row['reserved_cny'], 6)
    return {'schema': SCHEMA, **{k: v for k, v in budget.CONTRACT.items() if k != 'schema'}, 'timezone': 'Asia/Shanghai',
            'today': today.isoformat(), 'generated_at': now, 'days': result,
            'coverage': {'start_at': coverage_start, 'scope': 'trusted_main_agent_turns',
                         'owner_verified': owner_verified, 'historical_backfill': False,
                         'complete_days': False,
                         'basis': budget.BASIS,
                         'reasons': (['tracking_not_started'] if coverage_start is None else
                                     ['tracking_started_within_window'] if coverage_start > _start(first) else [])}}


def _native_rows(path, session, workspace, preset):
    import panel_activity as activity
    # Share the activity reader's bounded snapshot and streaming decompressor.
    # Only pricing/routing metadata below survives each decoded source line.
    with activity._native_lines(path) as lines:
        return _native_metadata(lines, session, workspace, preset)


def _native_metadata(lines, session, workspace, preset):
    import panel_activity as activity
    header = activity._json(next(lines, b''))
    activity._require(isinstance(header, dict) and header.get('type') == 'session' and
                      header.get('version') == 4 and type(header.get('isSeeded')) is bool and
                      header.get('id') == session and
                      activity._workspace(header.get('cwd')) == activity._workspace(workspace) and
                      activity._preset(header.get('agentPreset')) == activity._preset(preset), 'owner_mismatch')
    records, origins, markers, own_start, source_start = [], [], 0, 0, None
    for seq, line in enumerate(lines):
        event = activity._json(line)
        activity._require(isinstance(event, dict) and event.get('seq') == seq and
                          activity._integer(event.get('seq')) and activity._integer(event.get('time')) and event['time'] > 0 and
                          isinstance(event.get('type'), str) and isinstance(event.get('data'), dict))
        kind, data = event['type'], event['data']
        if kind == 'agent-preset/selected':
            activity._require(activity._preset(data.get('agentPreset')) == activity._preset(preset), 'owner_mismatch')
        if kind == 'session/end-seed' and data.get('inherited') is True:
            activity._require(header['isSeeded']); markers += 1; own_start = seq + 1
            # Only preserve the last pricing header across the seed boundary.
            records = [row for row in records if row['type'] == 'request/header'][-1:]
            origins.clear(); source_start = None
            continue
        source_start = event['time'] if source_start is None else min(source_start, event['time'])
        item = {'type': kind, 'seq': seq, 'time': event['time'], 'data': {}}
        if kind == 'request/header':
            config = data.get('header', {}).get('config', {})
            activity._require(isinstance(config, dict))
            item['data'] = {'header': {'config': {key: config.get(key) for key in ('provider', 'model')}}}
        elif kind in ('turn/start', 'turn/end'):
            item['data'] = {'turn': data.get('turn')}
        elif kind in ('assistant/message', 'compaction/summary'):
            message = data.get('message') or {}
            activity._require(isinstance(message, dict) and isinstance(message.get('source', {}), dict))
            source = message.get('source', {})
            item['data'] = {key: data.get(key) for key in ('turn', 'step', 'usage', 'provider', 'model')}
            item['data']['message'] = {'source': {key: source.get(key) for key in ('provider', 'model')}}
        elif kind == 'assistant/chunk' and isinstance(data.get('chunk'), dict) and data['chunk'].get('type') == 'usage':
            item['data'] = {key: data.get(key) for key in ('turn', 'step')}
            item['data']['chunk'] = {'type': 'usage', 'usage': data['chunk'].get('usage')}
        else:
            if kind == 'user/message':
                source = data.get('source')
                origins.append({'seq': seq, 'kind': source.get('kind', '') if isinstance(source, dict) else ''})
            continue
        records.append(item)
    activity._require(markers == (1 if header['isSeeded'] else 0) and source_start is not None)
    return records, origins, own_start, source_start


def _source_signature(paths):
    import panel_activity as activity
    result = []
    for path in paths:
        path = activity.ordinary(path)
        row = path.stat()
        result.append((str(path), row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns))
    return tuple(result)


def native_daily_spending(*, workspace, agent_preset, identity_file, dsh_home,
                          mapping_file=None, group_fallback='', cost_meter_root=None,
                          node_executable=None, now=None, days=30):
    """Bounded native projection; failures stay unavailable and never use a zero fallback.

    Only trusted configured paths may be supplied by the panel, never HTTP
    parameters. The subprocess receives usage/routing metadata, not messages,
    credentials, recipient IDs, raw arguments or session identifiers.
    """
    import panel_activity as activity
    sampled = int(time.time() * 1000) if now is None else now
    _require(_integer(sampled) and type(days) is int and 1 <= days <= 90)
    today = _date(sampled)
    dates = [(today - dt.timedelta(days=days - 1 - index)).isoformat() for index in range(days)]
    base = {'schema': SCHEMA, 'availability': 'unavailable',
            **{k: v for k, v in budget.CONTRACT.items() if k != 'schema'}, 'timezone': 'Asia/Shanghai',
            'today': today.isoformat(), 'generated_at': sampled,
            'days': [dict(date=date, autonomy=None, private=None, group=None, total=None, status='unknown',
                          turn_count=0, settled_count=0, pending_count=0, unknown_count=0, reserved_cny=0,
                          buckets={key: _empty_bucket() for key in budget.BUCKETS}) for date in dates],
            'coverage': {'scope': 'current_mapped_main_agent_native_turns',
                         'basis': 'owner_confirmed_numeric_cny_estimate', 'owner_verified': False,
                         'owner_verified_scopes': 0, 'configured_scopes': 0,
                         'current_mapped_sessions_only': True, 'historical_backfill': False,
                         'reconciled_days': 0, 'reasons': []}}
    try:
        activity._workspace(workspace); activity._preset(agent_preset)
        home = activity.ordinary(dsh_home)
        identity_path = activity.ordinary(identity_file)
        mapping_path = activity.ordinary(mapping_file or home / 'media/onebot/chat-sessions.json')
        meter = activity.ordinary(cost_meter_root or home / 'profiles/desktop/node_modules/dsh-cost-meter')
        ledger_path = home / 'storages/cost-meter/ledger.json'
        bridge = Path(__file__).with_name('budget-native-report.mjs')
        node = activity.ordinary(node_executable or shutil.which('node') or '')
        activity._require(node.is_file(), 'node_runtime_unavailable')
        identity = activity._json(activity.read_bytes(identity_path, 1024 * 1024))
        mapping = activity._json(activity.read_bytes(mapping_path, 1024 * 1024))
        activity._require(isinstance(identity, dict) and isinstance(mapping, dict), 'invalid_configuration')
        admin = identity.get('adminQQ'); admin = admin[0] if isinstance(admin, list) and admin else admin
        group = identity.get('fanGroupQQ') or identity.get('fanGroup') or group_fallback
        scopes = [('private', admin)] + ([('group', group)] if group not in (None, '') else [])
        base['coverage']['configured_scopes'] = len(scopes)
        activity._require(all(type(peer) in (str, int) and re.fullmatch('[1-9][0-9]*', str(peer))
                              for _, peer in scopes), 'invalid_configuration')
        selected, source_paths, selected_ids = [], [], set()
        for role, peer in scopes:
            entry = mapping.get(role + ':' + str(peer))
            if isinstance(entry, str): session, cwd = entry, workspace
            elif isinstance(entry, dict): session, cwd = entry.get('session'), entry.get('workspacePath') or workspace
            else: raise activity.ActivityUnavailable('mapping_missing')
            activity._require(isinstance(session, str) and session not in selected_ids and
                              activity._workspace(cwd) == activity._workspace(workspace), 'owner_mismatch')
            selected_ids.add(session)
            directory = home / 'sessions' / activity._project(cwd) / activity._segment(session)
            paths = [directory / name for name in ('session.v4.jsonl', 'session.v4.jsonl.zstd')
                     if os.path.lexists(directory / name)]
            activity._require(len(paths) == 1, 'source_missing' if not paths else 'source_ambiguous')
            source_paths.append(paths[0]); selected.append((role, session, paths[0]))
        # Include all installed pricing/aggregation sources in the read/cache
        # signature; a changed dependency cannot reuse an earlier projection.
        meter_sources = sorted((meter / 'lib').rglob('*.js'))
        activity._require(1 <= len(meter_sources) <= 200 and (meter / 'lib/turn-cost.js').is_file(), 'cost_meter_unavailable')
        paths = [identity_path, mapping_path, ledger_path, bridge, node, meter / 'package.json', *source_paths, *meter_sources]
        before = _source_signature(paths)
        key = (activity._workspace(workspace), activity._preset(agent_preset), today.isoformat(), days, str(node), before)
        with _NATIVE_LOCK:
            cached = _NATIVE_CACHE.get('value')
            if now is None and cached and cached[0] == key and 0 <= sampled - cached[1] <= 5000:
                return json.loads(json.dumps(cached[2]))
            ledger = activity._json(activity.read_bytes(ledger_path, 8 * 1024 * 1024))
            activity._require(isinstance(ledger, dict) and ledger.get('version') == 1 and
                              isinstance(ledger.get('days'), dict) and isinstance(ledger.get('config'), dict), 'cost_meter_unavailable')
            configuration = ledger['config']
            pricing = {key: configuration.get(key) for key in ('prices', 'priceMatch', 'priceOverrides',
                        'peakEnabled', 'peakEffectiveAt', 'peakWindows', 'peakHolidays', 'exchangeRate', 'planBilling')}
            pricing['codingPlans'] = {key: {'enabled': value.get('enabled') is True} for key, value in
                                      configuration.get('codingPlans', {}).items() if isinstance(value, dict)}
            pricing['goQuota'] = {'enabled': configuration.get('goQuota', {}).get('enabled') is True}
            sessions = []
            for role, session, source in selected:
                records, origins, own_start, source_start = _native_rows(source, session, workspace, agent_preset)
                expected = {}
                for date in dates:
                    day = ledger['days'].get(date)
                    if day is None: expected[date] = None; continue
                    activity._require(isinstance(day, dict) and day.get('date') == date and isinstance(day.get('sessions'), list))
                    matches = [row for row in day['sessions'] if isinstance(row, dict) and row.get('id') == session]
                    activity._require(len(matches) <= 1, 'source_ambiguous')
                    expected[date] = matches[0].get('apiCost') if matches else 0
                sessions.append(dict(role=role, records=records, origins=origins, own_start=own_start,
                                     source_start=source_start, expected_days=expected))
            payload = json.dumps(dict(now=sampled, dates=dates, config=pricing, sessions=sessions),
                                 ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('utf-8')
            activity._require(len(payload) <= 16 * 1024 * 1024, 'source_limit')
            process = subprocess.run([str(node), str(bridge), str(meter)], input=payload,
                                     capture_output=True, timeout=15, shell=False,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            activity._require(len(process.stdout) <= 1024 * 1024, 'source_limit')
            response = activity._json(process.stdout)
            if process.returncode != 0 or not isinstance(response, dict) or response.get('ok') is not True:
                code = response.get('code') if isinstance(response, dict) else None
                raise activity.ActivityUnavailable(code if code in ('node_runtime_unsupported', 'native_dependency_used',
                    'native_dependency_unavailable', 'invalid_native_cost_source', 'native_cost_unavailable') else 'native_cost_unavailable')
            report = response.get('report')
            activity._require(isinstance(report, dict) and report.get('schema') == SCHEMA and
                              type(report.get('numeric_scale')) is int and
                              all(report.get(k) == v for k, v in budget.CONTRACT.items() if k != 'schema') and
                              report.get('today') == today.isoformat()
                              and report.get('generated_at') == sampled and isinstance(report.get('days'), list)
                              and [row.get('date') for row in report['days']] == dates, 'malformed_source')
            # Live logs and ledger usage can advance after their bounded reads.
            # The captured native lines retain owner/sequence validation, and
            # the bridge reconciles them against the captured ledger per day.
            # New appends belong to the next cache key, not an unavailable UI.
            # Routing, pricing and code changes must still invalidate this read.
            fixed_paths = [path for path in paths if path not in [ledger_path, *source_paths]]
            fixed_before = tuple(row for row in before if row[0] in {str(path) for path in fixed_paths})
            activity._require(_source_signature(fixed_paths) == fixed_before, 'source_changing')
            latest_ledger = activity._json(activity.read_bytes(ledger_path, 8 * 1024 * 1024))
            activity._require(isinstance(latest_ledger, dict) and latest_ledger.get('version') == 1 and
                              latest_ledger.get('config') == configuration, 'source_changing')
            # The bridge's public output is aggregate-only by construction.
            _NATIVE_CACHE['value'] = (key, sampled, report)
            return json.loads(json.dumps(report))
    except subprocess.TimeoutExpired:
        base['coverage']['reasons'] = ['native_projection_timeout']
    except activity.ActivityUnavailable as error:
        base['coverage']['reasons'] = [str(error)]
    except (OSError, ValueError, TypeError, ImportError, OverflowError, AttributeError):
        base['coverage']['reasons'] = ['native_cost_unavailable']
    return base
