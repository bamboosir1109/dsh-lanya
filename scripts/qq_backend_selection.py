"""Operator backend selection with a fresh revision, full backup and guard gate.

Selection is configured state, not a claim that QQ or the selected software
has started. Neither this module nor the panel installs third-party binaries.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

import qq_backend as backend


class SelectionError(ValueError):
    def __init__(self, code, message, status=409):
        super().__init__(message); self.code = code; self.status = status


def _store():
    # Share the existing settings lock and native owner/group/DACL-preserving
    # primitives. Domain policy and writable fields stay in this module.
    import panel_autonomy
    return panel_autonomy


def _project():
    conf = backend._conf(); conf.load(reload=True); value = conf.config_path()
    if value is None:
        raise SelectionError('configuration_missing', 'Create project configuration before selecting a QQ backend')
    store = _store(); path = store._ordinary(value); before, raw = store._record(path)
    document = store._json(raw)
    return path, before, raw, document


def runtime_guard():
    """Verify the actually loaded adapter before changing a live selection."""
    try:
        import budget_runtime
        view = budget_runtime.get_snapshot(); gate = view['enforcement']
        file = Path.home() / '.dsh/profiles/desktop/node_modules/dsh-onebot-qq/lib/lanya-policy-adapter.js'
        _, raw = _store()._record(file, 2 * 1024 * 1024)
        ready = (gate.get('runtime_gate_bound') is True and type(gate.get('gate_pid')) is int and
            gate.get('backend_transport_bound') is True and gate.get('backend_quote_bound') is True and
            gate.get('adapter_source_sha256') == hashlib.sha256(raw).hexdigest() and
            b'// LANYA_BACKEND_BUNDLE_V1 ' in raw and b'this.backendGuard.edge(' in raw and
            b'this.backendGuard.verify(' in raw)
        return dict(ready=ready, state='ready' if ready else 'reload_required')
    except Exception:
        return dict(ready=False, state='unverified')


def snapshot(*, guard=None):
    guard = guard or runtime_guard
    path, before, _, document = _project()
    result = backend.catalog(project=document)
    result.update(schema='qq-backend-selection/v1', revision=_store()._revision(path, before),
        runtime_guard=guard(), configuration_only=True)
    return result


def _target(document, provider):
    try:
        return backend.resolve(project=document, provider=provider).fingerprint
    except backend.BackendConfigurationMissing:
        spec, settings, root, _, _, _ = backend.describe(project=document, provider=provider)
        if not backend.installed(spec, root):
            raise
        address = backend.webui_address(spec, settings, root)
        return hashlib.sha256(json.dumps([provider, str(root), address, 'first_setup']).encode()).hexdigest()


def select(body, *, guard=None):
    guard = guard or runtime_guard
    if (not isinstance(body, dict) or set(body) != {'provider', 'revision'} or
            not isinstance(body.get('provider'), str) or body['provider'] not in backend.BACKENDS or
            not isinstance(body.get('revision'), str) or len(body['revision']) != 64):
        raise SelectionError('invalid_selection', 'Expected one supported backend and a fresh revision', 400)
    store = _store(); path, _, _, _ = _project()
    with store._write_lock(path):
        actual_path, before, raw, document = _project()
        if actual_path != path or body['revision'] != store._revision(path, before):
            raise SelectionError('revision_conflict', 'Configuration changed; refresh before selecting a backend')
        selected, _, _, _, _, _ = backend.describe(project=document)
        decisions = document.get('decisions', {})
        if not isinstance(decisions, dict) or decisions.get('mode', 'unified') != 'unified':
            raise SelectionError('offline_configuration_required', 'Stop legacy workers before an offline deployment selects another backend')
        target = _target(document, body['provider'])
        if guard().get('ready') is not True:
            raise SelectionError('runtime_reload_required', 'Load the backend-aware QQ adapter before changing the live backend')
        options = document.setdefault('qqBackend', {})
        if not isinstance(options, dict):
            raise SelectionError('configuration_invalid', 'Invalid backend selection configuration')
        sequence = options.get('revision', 0)
        if type(sequence) is not int or not 0 <= sequence < 9007199254740991:
            raise SelectionError('configuration_invalid', 'Invalid backend selection revision')
        options['provider'] = body['provider']; options['revision'] = sequence + 1
        encoded = (json.dumps(document, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        change_id = uuid.uuid4().hex
        original = store._ordinary(path.with_name('.' + path.name + '.qq-backend-' + change_id + '.original.json'))
        candidate = store._ordinary(path.with_name('.' + path.name + '.qq-backend-' + change_id + '.candidate.json'))
        original_record = candidate_record = None
        try:
            for file, content in ((original, raw), (candidate, encoded)):
                with file.open('xb') as stream:
                    stream.write(content); stream.flush(); os.fsync(stream.fileno())
                os.chmod(file, stat.S_IMODE(before['signature'][4])); store._copy_security(path, file)
                record, actual = store._record(file)
                if actual != content or record['security_sha256'] != before['security_sha256']:
                    raise SelectionError('backup_failed', 'Backend selection backup could not be verified', 503)
                if file == original:
                    original_record = record
                else:
                    candidate_record = record
            if store._record(path)[0] != before or store._record(original)[0] != original_record:
                raise SelectionError('revision_conflict', 'Configuration changed during backend selection')
            # Re-read the target configuration and loaded gate immediately
            # before the single replacement; a stale preview cannot activate.
            gate = guard()
            fresh = _target(store._json(raw), body['provider'])
            if fresh != target or gate.get('ready') is not True:
                raise SelectionError('backend_changed', 'Backend or loaded adapter changed; refresh before selecting')
            os.replace(candidate, path)
            if store._record(path)[0] != candidate_record:
                raise SelectionError('readback_failed', 'Backend selection needs recovery; do not retry automatically', 503)
        finally:
            if candidate_record is not None and candidate.exists() and store._record(candidate)[0] == candidate_record:
                candidate.unlink()
        result = snapshot(guard=guard)
        result.update(change_id=change_id, previous_provider=selected.key,
            connection_state='awaiting_verification', backup_retained=True,
            started=False, messages_sent=0)
        return result
