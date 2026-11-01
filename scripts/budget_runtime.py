"""Unified per-turn API-cost estimates. Never calls a model or a platform.

Only trusted callers classify a turn; session-name prefixes are never billing
evidence. An unresolved finite reservation is retained across process restarts.
"""
import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
import uuid

import project_conf
from panel_autonomy import _record as _secure_record, _copy_security

SCHEMA = 'unified-budget/v1'
CONFIG_SCHEMA = 'dsh-autonomy-budget-config/v1'
BUCKETS = ('autonomy', 'private', 'group')
DEFAULT = {'schema': CONFIG_SCHEMA, 'currency': 'USD', 'basis': 'meter_api_usd_estimate',
           'limits': {'autonomy': 1.0, 'private': 0.0, 'group': 0.0}, 'reserve_usd': 0.05}
DDL = """
CREATE TABLE budget_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE budget_turns (
 turn_key TEXT PRIMARY KEY, owner_hash TEXT NOT NULL, scope_hash TEXT NOT NULL,
 category TEXT NOT NULL, day TEXT NOT NULL, instance_id TEXT NOT NULL,
 status TEXT NOT NULL, reserve_micros INTEGER NOT NULL, spent_micros INTEGER,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, receipt_hash TEXT);
CREATE INDEX budget_turn_day_category ON budget_turns (day,category,owner_hash);
"""


class BudgetError(ValueError):
    def __init__(self, code='budget_unavailable', http_status=503):
        super().__init__(code)
        self.code, self.http_status = code, http_status


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _ordinary(path, missing=False):
    path = Path(os.path.abspath(path))
    for part in [*reversed(path.parents), path]:
        if not os.path.lexists(part):
            if missing:
                continue
            raise BudgetError()
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise BudgetError('unsafe_budget_path')
    if path.exists() and not path.is_file():
        raise BudgetError('unsafe_budget_path')
    return path


def _read(path, limit=131072):
    path = _ordinary(path)
    _, raw = _secure_record(path, limit)
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise BudgetError('duplicate_budget_key', 400)
            result[key] = item
        return result
    try:
        return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(BudgetError('invalid_budget_number', 400))), hashlib.sha256(raw).hexdigest()
    except (ValueError, UnicodeError):
        raise BudgetError('invalid_budget_json')


def _paths():
    root = Path(project_conf.data_dir())
    return root / 'autonomy-budget.json', root / 'autonomy-budget.sqlite'


def _micros(value, zero=True):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or value > 1000000 or (not zero and value <= 0):
        raise BudgetError('invalid_budget_amount', 400)
    number = round(value * 1000000)
    if number < (0 if zero else 1):
        raise BudgetError('invalid_budget_amount', 400)
    return number


def _settings(value):
    if not isinstance(value, dict) or set(value) != {'limits', 'reserve_usd'} or not isinstance(value['limits'], dict) or set(value['limits']) != set(BUCKETS):
        raise BudgetError('invalid_budget_settings', 400)
    for limit in value['limits'].values():
        _micros(limit)
    _micros(value['reserve_usd'], False)
    return value


def _config():
    path, _ = _paths()
    if not os.path.lexists(path):
        value = json.loads(_json(DEFAULT))
        return value, _hash('absent:' + _json(value))
    value, revision = _read(path)
    if not isinstance(value, dict) or set(value) != set(DEFAULT) or value.get('schema') != CONFIG_SCHEMA or value.get('currency') != 'USD' or value.get('basis') != DEFAULT['basis']:
        raise BudgetError('invalid_budget_configuration', 400)
    _settings({k: value[k] for k in ('limits', 'reserve_usd')})
    return value, revision


def _clock(now=None):
    now = int(time.time() * 1000) if now is None else now
    if type(now) is not int or not 0 <= now <= 2**53 - 1:
        raise BudgetError('invalid_budget_clock')
    return now, dt.datetime.fromtimestamp(now / 1000).astimezone().date().isoformat()


def _connect(write=False):
    _, path = _paths()
    _ordinary(path, missing=write)
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), timeout=5)
    else:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    if not write:
        connection.execute('PRAGMA query_only=ON')
    return connection


def _validate_db(connection, create=False):
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables and create:
        # executescript implicitly commits; initialize with individual statements.
        for statement in DDL.strip().split(';'):
            if statement.strip():
                connection.execute(statement)
        connection.execute("INSERT INTO budget_meta VALUES ('schema', ?)", (SCHEMA,))
    elif tables != {'budget_meta', 'budget_turns'}:
        raise BudgetError('invalid_budget_database')
    expected = ['turn_key', 'owner_hash', 'scope_hash', 'category', 'day', 'instance_id', 'status', 'reserve_micros', 'spent_micros', 'created_at', 'updated_at', 'receipt_hash']
    if [r[1] for r in connection.execute('PRAGMA table_info(budget_turns)')] != expected or connection.execute("SELECT value FROM budget_meta WHERE key='schema'").fetchone()[0] != SCHEMA:
        raise BudgetError('invalid_budget_database')


@contextmanager
def _transaction(write=False):
    connection = _connect(write)
    try:
        connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        _validate_db(connection, create=write)
        yield connection
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _summary(connection, config, now):
    _, day = _clock(now)
    buckets = {}
    coverage = None
    if connection is not None:
        row = connection.execute("SELECT value FROM budget_meta WHERE key='coverage_start'").fetchone()
        coverage = int(row[0]) if row else None
    for category in BUCKETS:
        spent = reserved = unknown = turns = 0
        if connection is not None:
            for row in connection.execute('SELECT status,reserve_micros,spent_micros FROM budget_turns WHERE day=? AND category=? AND owner_hash=?', (day, category, _configured_owner_hash())):
                if row['status'] not in ('reserved', 'settled', 'unknown') or type(row['reserve_micros']) is not int or row['reserve_micros'] < 0:
                    raise BudgetError('invalid_budget_database')
                turns += 1
                if row['status'] == 'settled':
                    if type(row['spent_micros']) is not int or row['spent_micros'] < 0:
                        raise BudgetError('invalid_budget_database')
                    spent += row['spent_micros']
                else:
                    reserved += row['reserve_micros']
                    unknown += 1
        limit = _micros(config['limits'][category])
        can_start = not limit or spent + reserved + _micros(config['reserve_usd'], False) <= limit
        buckets[category] = {'spent_usd': spent / 1e6, 'reserved_usd': reserved / 1e6,
                             'remaining_usd': max(0, limit - spent - reserved) / 1e6 if limit else None,
                             'exhausted': bool(limit and spent + reserved >= limit), 'unknown_count': unknown, 'turn_count': turns,
                             'can_start_new_turn': bool(can_start), 'blocked_reason': None if can_start else 'insufficient_reserve'}
    return {'date': day, 'coverage_start': coverage, 'status': 'tracking' if coverage is not None else 'not_started',
            'counts_complete': coverage is not None and all(item['unknown_count'] == 0 for item in buckets.values()), 'historical_backfill': False, 'buckets': buckets}


def get_snapshot(now=None):
    """Pure read: does not initialize a database or a configuration file."""
    config, revision = _config()
    _, database = _paths()
    gate = None
    if os.path.lexists(database):
        with _transaction() as connection:
            usage = _summary(connection, config, now)
            row = connection.execute("SELECT value FROM budget_meta WHERE key='gate'").fetchone()
            gate = json.loads(row[0]) if row else None
    else:
        usage = _summary(None, config, now)
    if _config()[1] != revision:
        raise BudgetError('budget_configuration_changed', 409)
    stamp, _ = _clock(now)
    fresh = (isinstance(gate, dict) and type(gate.get('at')) is int and 0 <= stamp - gate['at'] <= 90000
             and gate.get('owner_hash') == _configured_owner_hash())
    return {'schema': SCHEMA, 'availability': 'available' if usage['coverage_start'] is not None else 'not_started', 'revision': revision,
            'effective': {'currency': 'USD', 'basis': DEFAULT['basis'], 'limits': config['limits'], 'reserve_usd': config['reserve_usd']},
            'usage': usage, 'enforcement': {'before_turn': True, 'before_action': True,
            'provider_hard_cap': False, 'runtime_gate_bound': fresh, 'gate_last_seen': gate['at'] if fresh else None,
            'gate_pid': gate.get('pid') if fresh else None,
            'adapter_source_sha256': gate.get('adapter_source_sha256') if fresh else None,
            'helper_source_sha256': gate.get('helper_source_sha256') if fresh else None,
            'control_decisions_exempt': True, 'evidence': 'recent adapter heartbeat; not a provider credit balance',
            'scope': 'trusted bound main-agent turn; not entertainment-only or account-wide cost'}}


def gate_evidence(now=None):
    """Private safe metadata for server comparison to actual scheduler/consumer."""
    value = get_snapshot(now)['enforcement']
    return {'at': value['gate_last_seen'], 'pid': value['gate_pid'],
            'adapter_source_sha256': value['adapter_source_sha256'],
            'helper_source_sha256': value['helper_source_sha256']}


def save_config(settings, expected_revision):
    _settings(settings)
    if not isinstance(expected_revision, str) or not re.fullmatch('[a-f0-9]{64}', expected_revision):
        raise BudgetError('fresh_budget_revision_required', 409)
    config_path, _ = _paths()
    with _transaction(True):
        _, current = _config()
        if current != expected_revision:
            raise BudgetError('stale_budget_revision', 409)
        before = _secure_record(config_path)[0] if os.path.lexists(config_path) else None
        config = dict(DEFAULT, **settings)
        raw = (_json(config) + '\n').encode('utf-8')
        temp = config_path.with_name('.budget-' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('xb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            if before is not None:
                os.chmod(temp, stat.S_IMODE(before['signature'][4]))
                _copy_security(config_path, temp)
            candidate = _secure_record(temp)[0]
            _ordinary(config_path, missing=True)
            if _config()[1] != current:
                raise BudgetError('budget_configuration_changed', 409)
            if before is not None and _secure_record(config_path)[0] != before:
                raise BudgetError('budget_configuration_changed', 409)
            os.replace(str(temp), str(config_path))
            if _secure_record(config_path)[0] != candidate:
                raise BudgetError('budget_configuration_readback_failed')
        finally:
            if temp.exists():
                temp.unlink()
    return get_snapshot()


def _configured_owner_hash():
    cwd = os.path.abspath(project_conf.work_dir())
    preset = project_conf.get('decisions.agentPreset', '')
    if not isinstance(preset, str):
        raise BudgetError('budget_owner_unavailable')
    value = cwd + '\0' + preset.strip()
    return _hash(value.lower() if os.name == 'nt' else value)


def _owner(request):
    def normalized(value):
        return value.lower() if os.name == 'nt' else value
    cwd, preset = request.get('cwd'), request.get('agent_preset')
    configured = project_conf.get('decisions.agentPreset', '')
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not isinstance(preset, str) or not isinstance(configured, str) or normalized(os.path.abspath(cwd)) != normalized(os.path.abspath(project_conf.work_dir())) or normalized(preset.strip()) != normalized(configured.strip()):
        raise BudgetError('budget_owner_mismatch', 403)
    scope = request.get('conversation_key')
    if not isinstance(scope, str) or not re.fullmatch(r'(private|group):[1-9][0-9]*', scope):
        raise BudgetError('invalid_budget_scope', 400)
    origin = request.get('origin')
    if origin not in ('observation', 'live'):
        raise BudgetError('invalid_budget_origin', 400)
    return _hash(normalized(os.path.abspath(cwd)) + '\0' + normalized(preset.strip())), scope, 'autonomy' if origin == 'observation' else scope.split(':')[0]


def operate(request, now=None):
    if isinstance(request, dict) and request.get('operation') == 'heartbeat':
        if set(request) != {'operation','instance_id','pid','adapter_source_sha256'} or not isinstance(request['instance_id'], str) or not re.fullmatch('[a-f0-9]{64}', request['instance_id']) or type(request['pid']) is not int or request['pid'] <= 0 or not isinstance(request['adapter_source_sha256'],str) or not re.fullmatch('[a-f0-9]{64}',request['adapter_source_sha256']):
            raise BudgetError('invalid_budget_heartbeat', 400)
        stamp, _ = _clock(now)
        with _transaction(True) as connection:
            _config()
            connection.execute("INSERT OR REPLACE INTO budget_meta VALUES ('gate',?)", (_json({'at':stamp,'instance_id':request['instance_id'],'pid':request['pid'],
                'adapter_source_sha256':request['adapter_source_sha256'], 'helper_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'owner_hash':_configured_owner_hash()}),))
        return {'bound': True, 'read_only': False}
    if not isinstance(request, dict) or request.get('operation') not in ('reserve', 'check', 'settle', 'unknown'):
        raise BudgetError('invalid_budget_operation', 400)
    owner, scope, category = _owner(request)
    key, instance = request.get('turn_key'), request.get('instance_id')
    if not all(isinstance(x, str) and re.fullmatch('[a-f0-9]{64}', x) for x in (key, instance)):
        raise BudgetError('invalid_budget_turn_identity', 400)
    now, day = _clock(now)
    with _transaction(True) as connection:
        config, _ = _config()
        row = connection.execute('SELECT * FROM budget_turns WHERE turn_key=?', (key,)).fetchone()
        if row and (row['owner_hash'] != owner or row['scope_hash'] != _hash(scope) or row['category'] != category):
            raise BudgetError('budget_turn_owner_conflict', 409)
        limit = _micros(config['limits'][category])
        reserve = _micros(config['reserve_usd'], False) if limit else 0
        if request['operation'] == 'reserve':
            if row:
                allowed = row['status'] == 'reserved' and row['instance_id'] == instance
                return {'allowed': allowed, 'reason': 'active_reservation' if allowed else 'previous_turn_unresolved', 'category': category, 'limited': bool(limit), 'decision_only': bool(limit and row['reserve_micros'] == 0)}
            usage = _summary(connection, config, now)['buckets'][category]
            if limit and request.get('meter_available') is not True:
                if request['origin'] == 'observation':
                    return {'allowed': False, 'reason': 'cost_meter_unavailable', 'category': category, 'limited': True}
                reserve = 0
            if limit and _micros(usage['spent_usd']) + _micros(usage['reserved_usd']) + reserve > limit:
                if request['origin'] == 'observation':
                    return {'allowed': False, 'reason': 'budget_exhausted', 'category': category, 'limited': True}
                # A human's control decision must remain possible. This is an
                # explicit cost-bearing exception, not a hidden free model call.
                reserve = 0
            connection.execute('INSERT INTO budget_turns VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)',
                               (key, owner, _hash(scope), category, day, instance, 'reserved', reserve, None, now, now))
            connection.execute("INSERT OR IGNORE INTO budget_meta VALUES ('coverage_start',?)", (str(now),))
            return {'allowed': True, 'reason': 'decision_only' if limit and reserve == 0 else 'reserved', 'category': category, 'limited': bool(limit), 'decision_only': bool(limit and reserve == 0),
                    'state': _summary(connection, config, now)['buckets'][category]}
        if row is None or row['instance_id'] != instance:
            raise BudgetError('budget_reservation_unavailable', 409)
        if request['operation'] == 'check':
            # A reserved turn may use its own allocation even when it occupies
            # the last daily slot. Other reservations cannot borrow it.
            used = _summary(connection, config, now)['buckets'][category]
            allocated = row['reserve_micros'] if row['status'] == 'reserved' else 0
            allowed = (row['status'] in ('reserved', 'settled') and row['day'] == day and
                       (not limit or row['reserve_micros'] > 0 and
                        _micros(used['spent_usd']) + _micros(used['reserved_usd']) - allocated < limit))
            return {'allowed': allowed, 'reason': 'within_reservation' if allowed else 'reservation_day_changed' if row['day'] != day else 'budget_exhausted', 'category': category, 'limited': bool(limit)}
        if request['operation'] == 'unknown':
            if row['status'] == 'reserved':
                connection.execute("UPDATE budget_turns SET status='unknown',updated_at=? WHERE turn_key=?", (now, key))
            return {'settled': False, 'status': row['status'] if row['status'] == 'settled' else 'unknown'}
        receipt = request.get('receipt')
        if not isinstance(receipt, dict) or set(receipt) != {'api_cost_usd', 'start_seq', 'end_seq', 'calls', 'priced'} or receipt['priced'] is not True or any(type(receipt[k]) is not int or not 0 <= receipt[k] <= 2**53 - 1 for k in ('start_seq','end_seq','calls')) or receipt['end_seq'] <= receipt['start_seq'] or receipt['calls'] < 1:
            raise BudgetError('invalid_budget_cost_receipt', 400)
        spent = _micros(receipt['api_cost_usd'])
        receipt_hash = _hash(_json(receipt))
        if row['status'] == 'settled':
            if row['receipt_hash'] != receipt_hash:
                raise BudgetError('budget_cost_receipt_conflict', 409)
            return {'settled': True, 'status': 'settled', 'duplicate': True}
        connection.execute("UPDATE budget_turns SET status='settled',spent_micros=?,updated_at=?,receipt_hash=? WHERE turn_key=?", (spent, now, receipt_hash, key))
        return {'settled': True, 'status': 'settled', 'duplicate': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stdin-json', action='store_true')
    parser.add_argument('--snapshot', action='store_true')
    args = parser.parse_args()
    if args.snapshot:
        result = get_snapshot()
    elif args.stdin_json:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise BudgetError('budget_request_too_large', 400)
        result = operate(json.loads(raw.decode('utf-8')))
    else:
        parser.error('Choose --snapshot or --stdin-json')
    print(_json(result))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(_json({'ok': False, 'error_code': error.code if isinstance(error, BudgetError) else 'budget_unavailable', 'error_class': type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
