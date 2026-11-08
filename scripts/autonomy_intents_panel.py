"""Read-only, bounded projection of the configured conversations' own plans.

Plans are agent reports, never proof of an action. No model, queue, policy or
database is created here. HTTP callers cannot select a recipient or owner.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from panel_activity import ordinary, read_bytes, _json, _workspace, _preset

SCHEMA = 'dsh-autonomy-plans-panel/v1'
ACTIVE = ('planned', 'active', 'paused', 'blocked_unknown')
SAFE_INTEGER = 9007199254740991


def _need(value):
    if not value:
        raise ValueError('Invalid scoped intent source')


def _integer(value):
    return type(value) is int and 0 <= value <= SAFE_INTEGER


def _text(value, limit, empty=False):
    _need(isinstance(value, str) and len(value) <= limit and (empty or bool(value.strip())))
    return value


def _projection(raw, row, workspace, preset, scope, role, now):
    _need(isinstance(raw, str) and len(raw.encode('utf-8')) <= 16384)
    record = _json(raw)
    _need(isinstance(record, dict) and record.get('schema') == 'dsh-autonomy-intent/v1')
    _need(record.get('owner') == {'workspace_key': workspace, 'agent_key': preset})
    _need(record.get('scope') == scope and record.get('summary_is_agent_report') is True)
    revision, status, updated = row
    _need(type(revision) is int and revision >= 1 and status in ACTIVE and _integer(updated))
    _need(record.get('revision') == revision and record.get('status') == status and record.get('updated_at') == updated)
    _need(_integer(record.get('created_at')) and record['created_at'] <= updated <= now + 300000)
    due = record.get('next_review_at')
    _need(due is None or _integer(due))
    receipts = record.get('tool_receipts')
    _need(isinstance(receipts, list) and len(receipts) <= 8)
    outcomes = {}
    for receipt in receipts:
        _need(isinstance(receipt, dict) and isinstance(receipt.get('outcome'), str))
        # Only counts leave this local projection. Names, arguments, IDs,
        # recipient, paths and raw tool messages are deliberately omitted.
        outcome = receipt['outcome']
        _need(outcome in ('returned', 'success', 'failed', 'unknown', 'sent', 'accepted', 'rejected'))
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return {'role': role, 'revision': revision, 'status': status, 'updated_at': updated,
            'goal': _text(record.get('goal'), 180), 'reason': _text(record.get('reason'), 240, True),
            'next_step': _text(record.get('next_step'), 240, True),
            'agent_summary': _text(record.get('agent_summary'), 240, True),
            'next_review_at': due, 'summary_is_agent_report': True, 'receipt_outcomes': outcomes}


def intent_snapshot(*, workspace, agent_preset, identity_file, data_directory,
                    group_fallback='', now=None):
    """Only trusted local configuration supplies paths and the two scopes."""
    now = int(time.time() * 1000) if now is None else now
    result = {'schema': SCHEMA, 'status': 'unavailable', 'read_only': True, 'private': True,
              'generated_at': now, 'plans': [], 'coverage': 'configured_admin_and_group',
              'summary_is_agent_report': True}
    try:
        _need(_integer(now))
        workspace_key, agent_key = _workspace(workspace), _preset(agent_preset)
        identity = _json(read_bytes(identity_file, 1024 * 1024))
        _need(isinstance(identity, dict))
        admin = identity.get('adminQQ')
        if isinstance(admin, list):
            admin = admin[0] if admin else ''
        group = identity.get('fanGroupQQ')
        if group in (None, ''):
            group = identity.get('fanGroup')
        if group in (None, ''):
            group = group_fallback
        scopes = [('primary', 'private:', admin)] + ([('social', 'group:', group)] if group not in (None, '') else [])
        _need(all(type(peer) in (str, int) and re.fullmatch('[1-9][0-9]*', str(peer)) for _, _, peer in scopes))
        _workspace(os.fspath(data_directory))
        database = ordinary(Path(data_directory) / 'autonomy-intents.sqlite3')
        if not database.exists():
            result['status'] = 'not_started'
            return result
        before = database.stat()
        _need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= 16 * 1024 * 1024)
        wal_present = Path(str(database) + '-wal').exists()
        for suffix in ('-wal', '-shm'):
            sidecar = ordinary(str(database) + suffix)
            if sidecar.exists():
                meta = sidecar.stat()
                _need(stat.S_ISREG(meta.st_mode) and meta.st_nlink == 1 and meta.st_size <= 128 * 1024 * 1024)
        _need(not wal_present or Path(str(database) + '-shm').exists())
        # SQLite mode=ro may create empty WAL/SHM files for a checkpointed WAL
        # database. For that case use immutable reads and reject the whole
        # projection if a writer appears or the main file changes. Live WAL
        # reads use SQLite's existing shared-memory coordination instead.
        immutable = not wal_present
        connection = sqlite3.connect(database.as_uri() + '?mode=ro' + ('&immutable=1' if immutable else ''), uri=True, timeout=1)
        try:
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            _need(connection.execute('PRAGMA user_version').fetchone() == (1,))
            for role, prefix, peer in scopes:
                scope = prefix + str(peer)
                key = hashlib.sha256(('\0'.join((workspace_key, agent_key, scope))).encode()).hexdigest()
                rows = connection.execute('SELECT revision,status,updated_at,record_json FROM intent_heads '
                    'WHERE owner_scope_key=? AND status IN (?,?,?,?) ORDER BY updated_at DESC LIMIT 16', (key, *ACTIVE)).fetchall()
                for revision, status, updated, raw in rows:
                    result['plans'].append(_projection(raw, (revision, status, updated), workspace_key, agent_key, scope, role, now))
        finally:
            connection.close()
        after = database.stat()
        _need((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino) and after.st_nlink == 1)
        if immutable:
            _need((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns))
            _need(not Path(str(database) + '-wal').exists())
        result['plans'].sort(key=lambda row: row['updated_at'], reverse=True)
        result['status'] = 'ok' if result['plans'] else 'empty'
    except (OSError, ValueError, TypeError, sqlite3.Error, OverflowError):
        result['plans'] = []
        result['status'] = 'unavailable'
    return result
