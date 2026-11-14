"""Owner-confirmed nominal CNY estimates. Never calls a model or platform.

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
from panel_autonomy import _record as _secure_record, _copy_security, _security

SCHEMA = 'unified-budget/v2'
CONFIG_SCHEMA = 'dsh-autonomy-budget-config/v2'
BASIS = 'owner_confirmed_numeric_cny_estimate'
CONTRACT = {'schema': SCHEMA, 'currency': 'CNY', 'basis': BASIS,
            'source_estimate_currency': 'USD',
            'source_estimate_basis': 'cost_meter_default_price_estimate',
            'numeric_scale': 1}
BUCKETS = ('autonomy', 'private', 'group')
TIMEZONE = dt.timezone(dt.timedelta(hours=8), 'Asia/Shanghai')
DEFAULT = dict(CONTRACT, schema=CONFIG_SCHEMA,
               limits={'autonomy': 5.0, 'private': 0.0, 'group': 0.0}, reserve_cny=0.05)
LEGACY_SCHEMA = 'unified-budget/v1'
LEGACY_CONFIG = {'schema': 'dsh-autonomy-budget-config/v1', 'currency': 'USD',
                 'basis': 'meter_api_usd_estimate',
                 'limits': {'autonomy': 0.694_444, 'private': 0.0, 'group': 0.0},
                 'reserve_usd': 0.05}
MIGRATION_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
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


def _is_contract(value):
    return (isinstance(value, dict) and set(value) == set(CONTRACT) and
            type(value.get('numeric_scale')) is int and value == CONTRACT)


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
    if not isinstance(value, dict) or set(value) != {'limits', 'reserve_cny'} or not isinstance(value['limits'], dict) or set(value['limits']) != set(BUCKETS):
        raise BudgetError('invalid_budget_settings', 400)
    for limit in value['limits'].values():
        # A positive cap must never round to the zero sentinel (unlimited).
        units = _micros(limit)
        if limit != 0 and (units == 0 or limit < 0.000001):
            raise BudgetError('invalid_budget_amount', 400)
    _micros(value['reserve_cny'], False)
    if value['reserve_cny'] < 0.000001:
        raise BudgetError('invalid_budget_amount', 400)
    return value


def _config():
    path, _ = _paths()
    if not os.path.lexists(path):
        value = json.loads(_json(DEFAULT))
        return value, _hash('absent:' + _json(value))
    value, revision = _read(path)
    if isinstance(value, dict) and value.get('schema') == 'dsh-autonomy-budget-config/v1':
        raise BudgetError('budget_currency_migration_required', 409)
    if not isinstance(value, dict) or set(value) != set(DEFAULT) or any(value.get(key) != item for key, item in DEFAULT.items() if key not in ('limits', 'reserve_cny')) or type(value.get('numeric_scale')) is not int:
        raise BudgetError('invalid_budget_configuration', 400)
    _settings({k: value[k] for k in ('limits', 'reserve_cny')})
    return value, revision


def _clock(now=None):
    now = int(time.time() * 1000) if now is None else now
    if type(now) is not int or not 0 <= now <= 2**53 - 1:
        raise BudgetError('invalid_budget_clock')
    return now, dt.datetime.fromtimestamp(now / 1000, TIMEZONE).date().isoformat()


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


def _validate_db(connection, create=False, schema=SCHEMA):
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables and create:
        # executescript implicitly commits; initialize with individual statements.
        for statement in DDL.strip().split(';'):
            if statement.strip():
                connection.execute(statement)
        connection.execute("INSERT INTO budget_meta VALUES ('schema', ?)", (SCHEMA,))
        connection.execute("INSERT INTO budget_meta VALUES ('currency_contract', ?)", (_json(CONTRACT),))
    elif tables != {'budget_meta', 'budget_turns'}:
        raise BudgetError('invalid_budget_database')
    expected = ['turn_key', 'owner_hash', 'scope_hash', 'category', 'day', 'instance_id', 'status', 'reserve_micros', 'spent_micros', 'created_at', 'updated_at', 'receipt_hash']
    current = connection.execute("SELECT value FROM budget_meta WHERE key='schema'").fetchone()
    if current and current[0] == 'unified-budget/v1' and schema == SCHEMA:
        raise BudgetError('budget_currency_migration_required', 409)
    if [r[1] for r in connection.execute('PRAGMA table_info(budget_turns)')] != expected or current is None or current[0] != schema:
        raise BudgetError('invalid_budget_database')
    if schema == SCHEMA:
        unit = connection.execute("SELECT value FROM budget_meta WHERE key='currency_contract'").fetchone()
        if unit is None or unit[0] != _json(CONTRACT):
            raise BudgetError('invalid_budget_currency_contract')


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


def _bucket_summary(connection, config, day, owner, category):
    spent = reserved = unknown = turns = 0
    if connection is not None:
        for row in connection.execute('SELECT status,reserve_micros,spent_micros FROM budget_turns WHERE day=? AND category=? AND owner_hash=?', (day, category, owner)):
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
    can_start = not limit or spent + reserved + _micros(config['reserve_cny'], False) <= limit
    return {'spent_cny': spent / 1e6, 'reserved_cny': reserved / 1e6,
            'remaining_cny': max(0, limit - spent - reserved) / 1e6 if limit else None,
            'exhausted': bool(limit and spent + reserved >= limit), 'unknown_count': unknown, 'turn_count': turns,
            'can_start_new_turn': bool(can_start), 'blocked_reason': None if can_start else 'insufficient_reserve'}


def _summary(connection, config, now):
    _, day = _clock(now)
    coverage = None
    owner = _configured_owner_hash()
    if connection is not None:
        row = connection.execute("SELECT value FROM budget_meta WHERE key='coverage_start'").fetchone()
        coverage = int(row[0]) if row else None
        first_owned = connection.execute('SELECT MIN(created_at) FROM budget_turns WHERE owner_hash=?', (owner,)).fetchone()[0]
        if first_owned is None:
            coverage = None
        elif type(first_owned) is not int or first_owned < 0 or coverage is None or first_owned < coverage:
            raise BudgetError('invalid_budget_database')
        else:
            coverage = first_owned
    buckets = {category: _bucket_summary(connection, config, day, owner, category) for category in BUCKETS}
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
             and gate.get('owner_hash') == _configured_owner_hash() and gate.get('budget_contract') == CONTRACT)
    return {'schema': SCHEMA, 'availability': 'available' if usage['coverage_start'] is not None else 'not_started', 'revision': revision,
            **{key: value for key, value in CONTRACT.items() if key != 'schema'},
            'effective': dict(CONTRACT, limits=config['limits'], reserve_cny=config['reserve_cny']),
            'usage': usage, 'enforcement': {'before_turn': True, 'before_action': True,
            'provider_hard_cap': False, 'runtime_gate_bound': fresh, 'gate_last_seen': gate['at'] if fresh else None,
            'gate_pid': gate.get('pid') if fresh else None,
            'adapter_source_sha256': gate.get('adapter_source_sha256') if fresh else None,
            'helper_source_sha256': gate.get('helper_source_sha256') if fresh else None,
            'backend_transport_bound': fresh and gate.get('backend_transport_bound') is True,
            'backend_quote_bound': fresh and gate.get('backend_quote_bound') is True,
            'account_activity_bound': fresh and gate.get('account_activity_bound') is True,
            'control_decisions_exempt': True, 'evidence': 'recent adapter heartbeat; not a provider credit balance',
            'scope': 'trusted bound main-agent turn; not entertainment-only or account-wide cost'}}


def gate_evidence(now=None):
    """Private safe metadata for server comparison to actual scheduler/consumer."""
    value = get_snapshot(now)['enforcement']
    return {'at': value['gate_last_seen'], 'pid': value['gate_pid'],
            'adapter_source_sha256': value['adapter_source_sha256'],
            'helper_source_sha256': value['helper_source_sha256'],
            'backend_transport_bound': value['backend_transport_bound'], 'backend_quote_bound': value['backend_quote_bound']}


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
    # A still-running v1 adapter cannot silently add USD-labelled receipts after
    # the one-time change of this project's nominal amount convention.
    if not isinstance(request, dict) or not _is_contract(request.get('budget_contract')):
        raise BudgetError('budget_currency_contract_required', 409)
    if request.get('operation') == 'autonomy_readiness':
        if set(request) != {'operation', 'budget_contract'}:
            raise BudgetError('invalid_budget_readiness', 400)
        bucket = get_snapshot(now)['usage']['buckets']['autonomy']
        return {'read_only': True, 'can_start_new_turn': bucket['can_start_new_turn']}
    if isinstance(request, dict) and request.get('operation') == 'heartbeat':
        fields = {'operation','instance_id','pid','adapter_source_sha256','budget_contract'}
        backend_fields = {'backend_transport_bound', 'backend_quote_bound'}
        optional_fields = backend_fields | {'account_activity_bound'}
        if (set(request) not in (fields, fields | backend_fields, fields | optional_fields) or
                not isinstance(request.get('instance_id'), str) or not re.fullmatch('[a-f0-9]{64}', request['instance_id']) or
                type(request.get('pid')) is not int or request['pid'] <= 0 or not isinstance(request.get('adapter_source_sha256'),str) or
                not re.fullmatch('[a-f0-9]{64}',request['adapter_source_sha256']) or
                any(type(request[key]) is not bool for key in optional_fields if key in request)):
            raise BudgetError('invalid_budget_heartbeat', 400)
        stamp, _ = _clock(now)
        with _transaction(True) as connection:
            _config()
            connection.execute("INSERT OR REPLACE INTO budget_meta VALUES ('gate',?)", (_json({'at':stamp,'instance_id':request['instance_id'],'pid':request['pid'],
                'adapter_source_sha256':request['adapter_source_sha256'], 'helper_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'backend_transport_bound':request.get('backend_transport_bound', False), 'backend_quote_bound':request.get('backend_quote_bound', False),
                'account_activity_bound':request.get('account_activity_bound', False),
                'owner_hash':_configured_owner_hash(), 'budget_contract':CONTRACT}),))
        return {'bound': True, 'read_only': False}
    if not isinstance(request, dict) or request.get('operation') not in ('reserve', 'check', 'settle', 'unknown'):
        raise BudgetError('invalid_budget_operation', 400)
    owner, scope, category = _owner(request)
    key, instance = request.get('turn_key'), request.get('instance_id')
    if not all(isinstance(x, str) and re.fullmatch('[a-f0-9]{64}', x) for x in (key, instance)):
        raise BudgetError('invalid_budget_turn_identity', 400)
    now, day = _clock(now)
    operation = request['operation']
    # A send check observes committed state. Taking the writer slot here
    # would queue every message part behind unrelated reservations and settlements.
    with _transaction(write=operation != 'check') as connection:
        config, revision = _config()
        row = connection.execute('SELECT * FROM budget_turns WHERE turn_key=?', (key,)).fetchone()
        if row and (row['owner_hash'] != owner or row['scope_hash'] != _hash(scope) or row['category'] != category):
            raise BudgetError('budget_turn_owner_conflict', 409)
        limit = _micros(config['limits'][category])
        reserve = _micros(config['reserve_cny'], False) if limit else 0
        if request['operation'] == 'reserve':
            if row:
                allowed = row['status'] == 'reserved' and row['instance_id'] == instance
                return {'allowed': allowed, 'reason': 'active_reservation' if allowed else 'previous_turn_unresolved', 'category': category, 'limited': bool(limit), 'decision_only': bool(limit and row['reserve_micros'] == 0)}
            usage = _bucket_summary(connection, config, day, owner, category)
            if limit and request.get('meter_available') is not True:
                if request['origin'] == 'observation':
                    return {'allowed': False, 'reason': 'cost_meter_unavailable', 'category': category, 'limited': True}
                reserve = 0
            if limit and _micros(usage['spent_cny']) + _micros(usage['reserved_cny']) + reserve > limit:
                if request['origin'] == 'observation':
                    return {'allowed': False, 'reason': 'budget_exhausted', 'category': category, 'limited': True}
                # A human's control decision must remain possible. This is an
                # explicit cost-bearing exception, not a hidden free model call.
                reserve = 0
            connection.execute('INSERT INTO budget_turns VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)',
                               (key, owner, _hash(scope), category, day, instance, 'reserved', reserve, None, now, now))
            connection.execute("INSERT OR IGNORE INTO budget_meta VALUES ('coverage_start',?)", (str(now),))
            return {'allowed': True, 'reason': 'decision_only' if limit and reserve == 0 else 'reserved', 'category': category, 'limited': bool(limit), 'decision_only': bool(limit and reserve == 0),
                    'state': _bucket_summary(connection, config, day, owner, category)}
        if row is None or row['instance_id'] != instance:
            raise BudgetError('budget_reservation_unavailable', 409)
        if operation == 'check':
            # A reserved turn may use its own allocation even when it occupies
            # the last daily slot. Other reservations cannot borrow it.
            used = _bucket_summary(connection, config, day, owner, category)
            allocated = row['reserve_micros'] if row['status'] == 'reserved' else 0
            allowed = (row['status'] in ('reserved', 'settled') and row['day'] == day and
                       (not limit or row['reserve_micros'] > 0 and
                        _micros(used['spent_cny']) + _micros(used['reserved_cny']) - allocated < limit))
            if _config()[1] != revision:
                raise BudgetError('budget_configuration_changed', 409)
            return {'allowed': allowed, 'reason': 'within_reservation' if allowed else 'reservation_day_changed' if row['day'] != day else 'budget_exhausted', 'category': category, 'limited': bool(limit)}
        if request['operation'] == 'unknown':
            if row['status'] == 'reserved':
                connection.execute("UPDATE budget_turns SET status='unknown',updated_at=? WHERE turn_key=?", (now, key))
            return {'settled': False, 'status': row['status'] if row['status'] == 'settled' else 'unknown'}
        receipt = request.get('receipt')
        if not isinstance(receipt, dict) or set(receipt) != {'api_cost_cny', 'start_seq', 'end_seq', 'calls', 'priced', 'budget_contract'} or not _is_contract(receipt.get('budget_contract')) or receipt['priced'] is not True or any(type(receipt[k]) is not int or not 0 <= receipt[k] <= 2**53 - 1 for k in ('start_seq','end_seq','calls')) or receipt['end_seq'] <= receipt['start_seq'] or receipt['calls'] < 1:
            raise BudgetError('invalid_budget_cost_receipt', 400)
        spent = _micros(receipt['api_cost_cny'])
        receipt_hash = _hash(_json(receipt))
        if row['status'] == 'settled':
            if row['receipt_hash'] != receipt_hash:
                raise BudgetError('budget_cost_receipt_conflict', 409)
            return {'settled': True, 'status': 'settled', 'duplicate': True}
        connection.execute("UPDATE budget_turns SET status='settled',spent_micros=?,updated_at=?,receipt_hash=? WHERE turn_key=?", (spent, now, receipt_hash, key))
        return {'settled': True, 'status': 'settled', 'duplicate': False}


def _turn_digest(connection):
    rows = [list(row) for row in connection.execute('SELECT * FROM budget_turns ORDER BY turn_key LIMIT 200001')]
    if len(rows) > 200000:
        raise BudgetError('budget_migration_source_limit')
    return len(rows), _hash(_json(rows))


def _metadata_digest(connection):
    rows = [list(row) for row in connection.execute('SELECT key,value FROM budget_meta ORDER BY key LIMIT 200001')]
    if len(rows) > 200000:
        raise BudgetError('budget_migration_source_limit')
    return _hash(_json(rows))


def _legacy_migration_state(connection):
    """Bound old files and a single SQLite snapshot; never reinterpret a v2 DB."""
    config_path, database = _paths()
    if str(connection.execute('PRAGMA journal_mode').fetchone()[0]).lower() != 'delete':
        raise BudgetError('budget_migration_journal_mode_unsupported', 409)
    value, revision = _read(config_path)
    if (not isinstance(value, dict) or set(value) != set(LEGACY_CONFIG) or value != LEGACY_CONFIG or
            type(value.get('reserve_usd')) not in (int, float) or
            any(type(item) not in (int, float) for item in value.get('limits', {}).values())):
        raise BudgetError('budget_migration_source_mismatch', 409)
    _validate_db(connection, schema=LEGACY_SCHEMA)
    metadata = [list(row) for row in connection.execute('SELECT key,value FROM budget_meta ORDER BY key LIMIT 200001')]
    if len(metadata) > 200000:
        raise BudgetError('budget_migration_source_limit')
    if any(row[0] in ('currency_contract', 'cny_migration') for row in metadata):
        raise BudgetError('budget_migration_source_mismatch', 409)
    count, digest = _turn_digest(connection)
    result = {'format': 'owner-confirmed-cny-migration-preview/v1',
              'from_schema': LEGACY_SCHEMA, 'to_schema': SCHEMA,
              'budget_contract': CONTRACT, 'numeric_scale': 1,
              'config_revision': revision, 'configuration_before': _secure_record(config_path)[0],
              'database_before': _secure_record(database, 64 * 1024 * 1024)[0],
              'turn_count': count, 'turn_rows_sha256': digest,
              'metadata_sha256': _metadata_digest(connection), 'settings_after': DEFAULT,
              'migration_source_sha256': MIGRATION_SOURCE_SHA256,
              'requires_stopped_main': True, 'automatic_retry': False,
              'turn_row_mutations': 0, 'source_ledger_mutations': 0}
    result['fingerprint'] = _hash(_json(result))
    return result


def migration_preview():
    """Read-only explicit v1→v2 preview. It never creates a missing database."""
    _migration_database_record(_paths()[1])
    connection = _connect()
    try:
        connection.execute('BEGIN')
        result = _legacy_migration_state(connection)
        if _secure_record(_paths()[1], 64 * 1024 * 1024)[0] != result['database_before']:
            raise BudgetError('budget_migration_source_changed', 409)
        return result
    finally:
        connection.rollback()
        connection.close()


def _migration_json(path, value):
    with path.open('xb') as stream:
        stream.write((_json(value) + '\n').encode('utf-8'))
        stream.flush()
        os.fsync(stream.fileno())


def _migration_database_record(path):
    record, raw = _secure_record(_ordinary(path), 64 * 1024 * 1024)
    # v1 used SQLite's default DELETE journal. Reject WAL before opening an
    # SQLite connection: a WAL reader can create sidecars, and the final close
    # can checkpoint after a physical receipt was captured. Other modes need a
    # separately reviewed migration rather than an implicit journal-mode write.
    if len(raw) < 100 or raw[:16] != b'SQLite format 3\0' or raw[18:20] != b'\x01\x01':
        raise BudgetError('budget_migration_journal_mode_unsupported', 409)
    return record


def _retained_config_record(path, expected, links):
    """Only the exact owned old inode may temporarily have two hard links."""
    path = _ordinary(path)
    info = path.stat()
    signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                 info.st_mode, info.st_nlink, getattr(info, 'st_file_attributes', 0))
    wanted = list(expected['signature']); wanted[5] = links
    if (list(signature) != wanted or not stat.S_ISREG(info.st_mode) or
            hashlib.sha256(path.read_bytes()).hexdigest() != expected['sha256'] or
            hashlib.sha256(_security(path)).hexdigest() != expected['security_sha256'] or
            (lambda current: (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns,
                              current.st_mode, current.st_nlink, getattr(current, 'st_file_attributes', 0)))(path.stat()) != signature):
        raise BudgetError('budget_migration_old_inode_changed', 409)
    return dict(expected, signature=signature)


def _same_database_identity(before, after):
    # SQLite changes content, size and mtime for the metadata transaction.
    # The file object, mode/link/attributes and full owner/group/DACL may not.
    return (all(before['signature'][index] == after['signature'][index] for index in (0, 1, 4, 5, 6)) and
            before['security_sha256'] == after['security_sha256'])


def migrate_cny(expected_fingerprint, archive_dir):
    """One explicit stopped-main maintenance call; never retries/repairs a phase.

    The caller proves the Main process is stopped outside this module. A new,
    same-volume private archive retains the old configuration inode/full SD and
    an SQLite backup made while a write reservation excludes other writers.
    A crash between config replace and metadata commit leaves both originals
    plus intent.json. Both ordinary runtime paths then refuse mixed versions;
    inspection/recovery is a separate controlled operator operation.
    """
    if not isinstance(expected_fingerprint, str) or not re.fullmatch('[a-f0-9]{64}', expected_fingerprint):
        raise BudgetError('fresh_budget_migration_fingerprint_required', 409)
    config_path, database = _paths()
    _migration_database_record(database)
    archive = Path(archive_dir)
    if not archive.is_absolute() or os.path.lexists(archive) or not archive.parent.is_dir():
        raise BudgetError('fresh_budget_migration_archive_required', 409)
    _ordinary(archive / 'intent.json', missing=True)
    if archive.parent.stat().st_dev != config_path.stat().st_dev:
        raise BudgetError('budget_migration_archive_not_same_volume', 409)
    # Existing-only URI: a missing/replaced DB is never initialized here.
    connection = sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=0)
    connection.row_factory = sqlite3.Row
    try:
        try:
            connection.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError:
            raise BudgetError('budget_migration_busy', 409) from None
        before = _legacy_migration_state(connection)
        if before['fingerprint'] != expected_fingerprint:
            raise BudgetError('stale_budget_migration_fingerprint', 409)
        archive.mkdir(mode=0o700, exist_ok=False)
        # A second read connection can copy the pre-migration snapshot while
        # BEGIN IMMEDIATE on the first connection prevents competing writers.
        source = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=0)
        backup_path = archive / 'database-before.sqlite'
        backup = sqlite3.connect(str(backup_path))
        try:
            source.backup(backup)
        finally:
            backup.close(); source.close()
        _copy_security(database, backup_path)
        backup_record = _secure_record(backup_path, 64 * 1024 * 1024)[0]
        check = sqlite3.connect(backup_path.as_uri() + '?mode=ro', uri=True)
        try:
            _validate_db(check, schema=LEGACY_SCHEMA)
            if (_turn_digest(check) != (before['turn_count'], before['turn_rows_sha256']) or
                    _metadata_digest(check) != before['metadata_sha256']):
                raise BudgetError('budget_migration_backup_failed')
        finally:
            check.close()
        candidate = archive / 'configuration-after.json'
        with candidate.open('xb') as stream:
            stream.write((_json(DEFAULT) + '\n').encode('utf-8'))
            stream.flush(); os.fsync(stream.fileno())
        os.chmod(candidate, stat.S_IMODE(before['configuration_before']['signature'][4]))
        _copy_security(config_path, candidate)
        candidate_record = _secure_record(candidate)[0]
        slot = archive / 'configuration-before.json'
        intent = {'format': 'owner-confirmed-cny-migration-intent/v1', 'preview': before,
                  'target': str(config_path), 'database': str(database), 'slot': str(slot),
                  'candidate': str(candidate), 'configuration_after': candidate_record,
                  'database_backup': str(backup_path), 'database_backup_record': backup_record,
                  'migration_source_sha256': MIGRATION_SOURCE_SHA256,
                  'automatic_retry': False}
        _migration_json(archive / 'intent.json', intent)
        if (_secure_record(config_path)[0] != before['configuration_before'] or
                _secure_record(database, 64 * 1024 * 1024)[0] != before['database_before'] or
                _secure_record(candidate)[0] != candidate_record):
            raise BudgetError('budget_migration_source_changed', 409)
        # Keep the original physical inode only at the final replacement edge;
        # the normal single-link configuration reader is never weakened.
        os.link(str(config_path), str(slot))
        _retained_config_record(config_path, before['configuration_before'], 2)
        _retained_config_record(slot, before['configuration_before'], 2)
        os.replace(str(candidate), str(config_path))
        if _secure_record(config_path)[0] != candidate_record:
            raise BudgetError('budget_migration_configuration_readback_failed')
        slot_record = _secure_record(slot)[0]
        if slot_record != before['configuration_before']:
            raise BudgetError('budget_migration_old_inode_changed', 409)
        if _secure_record(database, 64 * 1024 * 1024)[0] != before['database_before']:
            raise BudgetError('budget_migration_source_changed', 409)
        # No statement in this connection may alter turn rows, create/drop a
        # table, or change unrelated metadata. Old receipt hashes stay intact.
        denied = {getattr(sqlite3, name) for name in (
            'SQLITE_CREATE_TABLE', 'SQLITE_DROP_TABLE', 'SQLITE_ALTER_TABLE',
            'SQLITE_CREATE_INDEX', 'SQLITE_DROP_INDEX', 'SQLITE_DELETE')}
        def metadata_only(action, table, column, database_name, trigger):
            if action in denied or action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE) and table != 'budget_meta':
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        connection.set_authorizer(metadata_only)
        connection.execute("UPDATE budget_meta SET value=? WHERE key='schema' AND value=?", (SCHEMA, LEGACY_SCHEMA))
        connection.execute("INSERT INTO budget_meta VALUES ('currency_contract',?)", (_json(CONTRACT),))
        transition = {'from_schema': LEGACY_SCHEMA, 'to_schema': SCHEMA, 'numeric_scale': 1,
                      'fingerprint': expected_fingerprint, 'turn_rows_sha256': before['turn_rows_sha256']}
        connection.execute("INSERT INTO budget_meta VALUES ('cny_migration',?)", (_json(transition),))
        if _turn_digest(connection) != (before['turn_count'], before['turn_rows_sha256']):
            raise BudgetError('budget_migration_turn_rows_changed')
        connection.commit()
        _validate_db(connection)
        after_count, after_digest = _turn_digest(connection)
        if (after_count, after_digest) != (before['turn_count'], before['turn_rows_sha256']):
            raise BudgetError('budget_migration_turn_rows_changed')
        database_after = _secure_record(database, 64 * 1024 * 1024)[0]
        if (not _same_database_identity(before['database_before'], database_after) or
                _secure_record(config_path)[0] != candidate_record or
                _secure_record(slot)[0] != before['configuration_before']):
            raise BudgetError('budget_migration_final_identity_changed', 409)
        result = {'format': 'owner-confirmed-cny-migration-result/v1', 'success': True,
                  'fingerprint': expected_fingerprint, 'budget_contract': CONTRACT,
                  'migration_source_sha256': MIGRATION_SOURCE_SHA256,
                  'intent_sha256': _secure_record(archive / 'intent.json')[0]['sha256'],
                  'configuration_before': before['configuration_before'],
                  'configuration_after': _secure_record(config_path)[0],
                  'slot': str(slot), 'slot_record': slot_record,
                  'database_before': before['database_before'],
                  'database_after': database_after,
                  'database_backup': str(backup_path), 'database_backup_record': backup_record,
                  'turn_count': after_count, 'turn_rows_sha256': after_digest,
                  'turn_row_mutations': 0, 'source_ledger_mutations': 0,
                  'automatic_retry': False}
        _migration_json(archive / 'result.json', result)
        return result
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stdin-json', action='store_true')
    parser.add_argument('--snapshot', action='store_true')
    parser.add_argument('--migration-preview', action='store_true')
    parser.add_argument('--migrate-cny', metavar='EXPECTED_FINGERPRINT')
    parser.add_argument('--migration-archive', help='New private same-volume archive; used only by --migrate-cny')
    args = parser.parse_args()
    if sum(bool(value) for value in (args.snapshot, args.stdin_json, args.migration_preview, args.migrate_cny)) != 1:
        parser.error('Choose exactly one budget operation')
    if args.migration_preview:
        result = migration_preview()
    elif args.migrate_cny:
        if not args.migration_archive:
            parser.error('--migrate-cny requires --migration-archive')
        result = migrate_cny(args.migrate_cny, args.migration_archive)
    elif args.snapshot:
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
