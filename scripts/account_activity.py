"""Own-account publication settings and read-only accepted/unknown history.

No QQ write, model, timer, retry or claim recovery is performed by this module.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid
from urllib.parse import quote

import panel_autonomy as store
import project_conf
import qq_backend

DEFAULTS = {'enabled': False, 'posts': True, 'signature': True, 'audience': 'friends',
            'postIntervalHours': 6, 'signatureIntervalHours': 4, 'dailyPosts': 2, 'dailySignatures': 2}


def settings(project, provider):
    options = project.get('qqBackend', {})
    store._need(type(options) is dict, 'invalid_account_settings', 400)
    value = options.get('accountActivity', {})
    store._need(type(value) is dict and not set(value) - set(DEFAULTS), 'invalid_account_settings', 400)
    out = dict(DEFAULTS, **value)
    store._need(all(type(out[key]) is bool for key in ('enabled', 'posts', 'signature')) and
                out['audience'] in ('friends', 'self'), 'invalid_account_settings', 400)
    for key, maximum in [('postIntervalHours', 168), ('signatureIntervalHours', 168), ('dailyPosts', 6), ('dailySignatures', 6)]:
        store._need(type(out[key]) is int and 1 <= out[key] <= maximum, 'invalid_account_settings', 400)
    capabilities = qq_backend.BACKENDS[provider].capabilities
    return dict(out, post_supported='account_post' in capabilities, signature_supported='account_signature' in capabilities)


def journal_path(project, provider):
    identity, _ = qq_backend._read(Path(project_conf.data_dir()) / 'lanya-config.json', optional=True)
    account = (identity or {}).get('botQQ')
    if type(account) not in (int, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', str(account)):
        raise qq_backend.BackendError('Configure the trusted QQ account before reading its activity')
    work = os.path.abspath(project_conf.work_dir())
    key = work.lower() if os.name == 'nt' else work
    binding = hashlib.sha256('\0'.join((key, provider, str(account))).encode()).hexdigest()
    return store._ordinary(Path(work) / 'napcat' / 'account-activity' / (binding + '.sqlite')), binding


def history(project, provider):
    file, binding = journal_path(project, provider)
    if not file.exists():
        return []
    info = file.stat()
    store._need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 32 * 1024 * 1024)
    connection = sqlite3.connect('file:' + quote(str(file).replace('\\', '/'), safe='/:') + '?mode=ro', uri=True, timeout=1)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        store._need(connection.execute('PRAGMA user_version').fetchone()[0] == 1)
        store._need(connection.execute('SELECT binding FROM account_binding').fetchone()[0] == binding)
        rows = [dict(row) for row in connection.execute('SELECT * FROM publications ORDER BY started_at DESC,request_key DESC LIMIT 20')]
        store._need(all(row['kind'] in ('post', 'signature') and row['status'] in ('reserved', 'accepted', 'rejected', 'unknown') and
                        type(row['started_at']) is int and type(row['content']) is str and len(row['content']) <= 1600 for row in rows))
        return [dict(kind=row['kind'], text=row['content'], state=row['status'], at=row['started_at'],
                     audience=row['audience'], receipt_id=row['receipt_id'], reason=row['reason']) for row in rows]
    finally:
        connection.close()


def snapshot():
    path, record, _, project = store._project()
    provider = qq_backend.describe(project=project)[0].key
    value = settings(project, provider)
    recent = history(project, provider)
    try:
        import budget_runtime
        enforcement = budget_runtime.get_snapshot()['enforcement']
        loaded = enforcement.get('runtime_gate_bound') is True and enforcement.get('account_activity_bound') is True
    except Exception:
        loaded = False
    return dict(schema='dsh-account-activity/v1', availability='available', provider=provider,
                revision=store._revision(path, record), settings=value, recent=recent, runtime_loaded=loaded,
                read_only=True, sends_messages=False,
                meaning='backend_accepted_is_not_client_visibility',
                rest=dict(timezone='Asia/Shanghai', start='00:00', end='08:00'))


def save_settings(body):
    if (type(body) is not dict or set(body) != {'revision', 'settings'} or
            type(body['revision']) is not str or re.fullmatch(r'[a-f0-9]{64}', body['revision']) is None or
            type(body['settings']) is not dict or set(body['settings']) != set(DEFAULTS)):
        raise store.SettingsError('invalid_account_settings', 'Expected the current revision and account activity settings')
    path, _, _, _ = store._project()
    store._need(path is not None, 'configuration_missing')
    with store._write_lock(path):
        current, before, raw, project = store._project()
        store._need(current == path and body['revision'] == store._revision(path, before), 'revision_conflict', 409)
        store._need(project.get('decisions', {}).get('mode') == 'unified', 'unified_settings_required', 409)
        provider = qq_backend.describe(project=project)[0].key
        validated = settings({'qqBackend': {'accountActivity': body['settings']}}, provider)
        # Account validation is local and never publishes a test post.
        journal_path(project, provider)
        value = store._json(raw)
        value.setdefault('qqBackend', {})['accountActivity'] = {key: validated[key] for key in DEFAULTS}
        encoded = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
        operation = uuid.uuid4().hex
        original = store._ordinary(path.with_name('.' + path.name + '.account-' + operation + '.original.json'))
        candidate = store._ordinary(path.with_name('.' + path.name + '.account-' + operation + '.candidate.json'))
        with original.open('xb') as output:
            output.write(raw); output.flush(); os.fsync(output.fileno())
        store._copy_security(path, original)
        store._need(store._record(original)[1] == raw and store._record(original)[0]['security_sha256'] == before['security_sha256'])
        with candidate.open('xb') as output:
            output.write(encoded); output.flush(); os.fsync(output.fileno())
        os.chmod(candidate, stat.S_IMODE(before['signature'][4]))
        store._copy_security(path, candidate)
        prepared, checked = store._record(candidate)
        store._need(checked == encoded and prepared['security_sha256'] == before['security_sha256'])
        store._need(store._record(path)[0] == before, 'revision_conflict', 409)
        os.replace(candidate, path)
        store._need(store._record(path)[0] == prepared, 'account_settings_readback_failed')
    return snapshot()
