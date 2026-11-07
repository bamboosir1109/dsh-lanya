"""Private first-person diary reports, written by the existing main agent.

No model, transport, native transcript, memory-fact or legacy diary writer.
Facts stay in activity_journal; feelings are explicitly agent self-reports.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

sys.dont_write_bytecode = True
import activity_journal as facts

SCHEMA = 'narrative-diary/v1'
MAXIMUM = 1024 * 1024
HASH = re.compile(r'[0-9a-f]{64}')


def need(value, reason='diary_unavailable'):
    if not value:
        raise facts.JournalError(reason)


def digest(value):
    return hashlib.sha256(facts._encoded(value)).hexdigest()


def short(value, maximum, required=False):
    need(isinstance(value, str) and len(value) <= maximum and
         not any(ord(c) < 32 and c not in '\n\t' for c in value), 'invalid_diary_text')
    value = value.strip()
    need(not required or bool(value), 'empty_diary_text')
    return value


def location(workspace, agent_preset, data_directory, conversation_key):
    need(isinstance(conversation_key, str) and re.fullmatch(r'private:[1-9][0-9]*', conversation_key), 'private_diary_only')
    source, owner = facts._location(workspace, agent_preset, data_directory)
    # Even two private recipients of one preset cannot share a diary file.
    return source / 'personal' / hashlib.sha256(conversation_key.encode('utf-8')).hexdigest()[:32], owner


def validate(value, owner, scope, day):
    need(type(value) is dict and value.get('schema') == SCHEMA and value.get('owner') == owner and
         value.get('scope') == scope and value.get('day') == day, 'foreign_diary_preserved')
    need(type(value.get('revision')) is int and 1 <= value['revision'] <= 10000 and
         facts._stamp(value.get('updated_at')) and HASH.fullmatch(value.get('fact_revision', '')) and
         isinstance(value.get('coverage'), dict) and value.get('feelings_are_agent_report') is True, 'invalid_diary_record')
    short(value.get('title'), 60, True); short(value.get('markdown'), 8000, True)
    operations = value.get('operations')
    need(type(operations) is dict and 1 <= len(operations) <= 256, 'invalid_diary_operations')
    for key, operation in operations.items():
        need(HASH.fullmatch(key) and type(operation) is dict and HASH.fullmatch(operation.get('request_digest', '')) and
             type(operation.get('revision')) is int and 1 <= operation['revision'] <= value['revision'], 'invalid_diary_operations')
    need(type(value.get('evidence_refs')) is list and 1 <= len(value['evidence_refs']) <= 24 and
         all(isinstance(ref, str) and HASH.fullmatch(ref) for ref in value['evidence_refs']), 'invalid_diary_references')
    evidence = value.get('source_evidence')
    need(value.get('narrative_is_agent_report') is True and type(evidence) is list and
         len(evidence) == len(value['evidence_refs']) and
         {item.get('ref') for item in evidence if type(item) is dict} == set(value['evidence_refs']), 'invalid_diary_source_snapshot')
    for item in evidence:
        need(type(item) is dict and set(item) == {'ref', 'summary', 'excerpt_truncated'} and
             type(item['excerpt_truncated']) is bool, 'invalid_diary_source_snapshot')
        short(item['summary'], 900, True)
    return value


def read_day(root, owner, scope, day):
    path = root / (facts._day(day) + '.json')
    if not os.path.lexists(path):
        return None, None
    record, raw = facts._record(path, MAXIMUM)
    return validate(facts._json(raw), owner, scope, day), record


def project(value):
    return {key: value[key] for key in ('day', 'title', 'revision', 'updated_at', 'markdown',
            'fact_revision', 'coverage', 'evidence_refs', 'source_evidence',
            'feelings_are_agent_report', 'narrative_is_agent_report')}


def _recover_same(root, owner, scope, operation_id, request_digest):
    path = root / 'pending.json'
    if not os.path.lexists(path):
        return
    pending_record, raw = facts._record(path, MAXIMUM)
    pending = facts._json(raw)
    need(type(pending) is dict and set(pending) == {'schema', 'owner', 'scope', 'day', 'operation_id', 'before', 'after', 'candidate', 'history'}, 'invalid_diary_pending')
    need(pending['schema'] == SCHEMA and pending['owner'] == owner and pending['scope'] == scope and
         pending['operation_id'] == operation_id, 'diary_submission_unknown_read_existing')
    day = facts._day(pending['day']); target = root / (day + '.json')
    candidate = pending['candidate']; history = pending['history']
    need(isinstance(candidate, str) and re.fullmatch(r'\.stage-[0-9a-f]{32}\.json', candidate), 'invalid_diary_candidate')
    facts._validate_record(pending['before'], MAXIMUM, True); facts._validate_record(pending['after'], MAXIMUM)
    current = facts._current(target)
    need(current in (pending['before'], pending['after']), 'diary_unknown_file_preserved')
    source = target if current == pending['after'] else root / candidate
    record, body = facts._record(source, MAXIMUM)
    need(record == pending['after'], 'diary_candidate_changed')
    value = validate(facts._json(body), owner, scope, day)
    need(operation_id in value['operations'], 'diary_operation_missing')
    need(value['operations'][operation_id]['request_digest'] == request_digest, 'diary_idempotency_conflict')
    if history is not None:
        need(type(history) is dict and set(history) == {'name', 'record'} and
             isinstance(history['name'], str) and re.fullmatch(r'\.history-[0-9a-f]{32}\.json', history['name']), 'invalid_diary_history')
        need(facts._record(root / history['name'], MAXIMUM)[0] == history['record'] and
             history['record']['sha256'] == pending['before']['sha256'], 'diary_history_changed')
    else:
        need(pending['before'] is None, 'diary_history_required')
    if current == pending['before']:
        need(facts._current(target) == current, 'diary_target_changed')
        os.replace(root / candidate, target)
    need(facts._current(target) == pending['after'] and facts._current(path) == pending_record, 'diary_commit_unconfirmed')
    path.unlink()


def narrative_snapshot(*, workspace, agent_preset, data_directory, conversation_key,
                       day=None, limit=90, now=None, facts_provider=None):
    """Read only. Neither page reads nor pending hints generate a diary/model."""
    now = int(time.time() * 1000) if now is None else now
    today = dt.datetime.fromtimestamp(now / 1000, facts.BEIJING).date().isoformat()
    chosen = facts._day(day or today)
    result = {'schema': SCHEMA, 'status': 'empty', 'read_only': True, 'private': True,
              'generated_at': now, 'days': [], 'selected': None, 'submission': 'none',
              'facts': {'status': 'unavailable', 'revision': None}, 'new_facts_available': False}
    need(type(limit) is int and 1 <= limit <= 90 and facts._stamp(now), 'invalid_diary_read')
    root, owner = location(workspace, agent_preset, data_directory, conversation_key)
    provider = facts_provider or facts.diary_facts
    source = provider(workspace=workspace, agent_preset=agent_preset, data_directory=data_directory, day=chosen, now=now)
    result['facts'] = source
    if not os.path.lexists(root):
        result['new_facts_available'] = source.get('status') == 'available' and bool(source.get('items'))
        return result
    facts._private_directory(root)
    with facts._lock(root):
        pending = os.path.lexists(root / 'pending.json')
        result['submission'] = 'unknown_read_existing' if pending else 'none'
        for path in sorted(root.iterdir(), key=lambda value: value.name, reverse=True):
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}\.json', path.name):
                continue
            entry, _ = read_day(root, owner, conversation_key, path.stem)
            result['days'].append({key: entry[key] for key in ('day', 'title', 'revision', 'updated_at', 'fact_revision')})
            if len(result['days']) == limit:
                break
        entry, _ = read_day(root, owner, conversation_key, chosen)
        if entry:
            result['selected'] = project(entry)
        result['new_facts_available'] = (not pending and source.get('status') == 'available' and bool(source.get('items')) and
                                         (not entry or entry['fact_revision'] != source.get('revision')))
    result['status'] = 'ok' if result['days'] else 'empty'
    return result


def write_diary(*, workspace, agent_preset, data_directory, conversation_key, day,
                expected_revision, fact_revision, title, experiences, feelings, unfinished,
                operation_id, now=None, facts_provider=None, fault=None):
    now = int(time.time() * 1000) if now is None else now
    date = dt.datetime.fromtimestamp(now / 1000, facts.BEIJING).date(); facts._day(day)
    need(day in (date.isoformat(), (date - dt.timedelta(days=1)).isoformat()), 'diary_day_not_current_or_yesterday')
    need(type(expected_revision) is int and 0 <= expected_revision < 10000 and isinstance(operation_id, str) and HASH.fullmatch(operation_id), 'invalid_diary_cas')
    need(isinstance(fact_revision, str) and HASH.fullmatch(fact_revision), 'invalid_diary_fact_revision')
    title = short(title, 60, True)
    need('\n' not in title and not re.fullmatch(r'[\d\W_]+', title), 'concrete_diary_title_required')
    feelings = short(feelings, 800); unfinished = short(unfinished, 800)
    need(type(experiences) is list and 1 <= len(experiences) <= 6, 'grounded_diary_experience_required')
    paragraphs, references = [], []
    for experience in experiences:
        need(type(experience) is dict and set(experience) == {'text', 'evidence_refs'}, 'invalid_diary_experience')
        text = short(experience['text'], 1200, True); refs = experience['evidence_refs']
        need(type(refs) is list and 1 <= len(refs) <= 4 and all(isinstance(ref, str) and HASH.fullmatch(ref) for ref in refs), 'invalid_diary_references')
        paragraphs.append(text); references.extend(refs)
    need(any('我' in paragraph or re.search(r'\bI\b', paragraph) for paragraph in paragraphs), 'first_person_diary_required')
    request_digest = digest(dict(day=day, expected_revision=expected_revision, fact_revision=fact_revision,
                                 title=title, experiences=experiences, feelings=feelings, unfinished=unfinished))
    root, owner = location(workspace, agent_preset, data_directory, conversation_key)
    # All new directories have private DACLs. Existing ordinary diary files,
    # unrelated owners and user-written DATA/diary files are never adopted.
    for directory in (root.parent.parent.parent, root.parent.parent, root.parent, root):
        facts._private_directory(directory, True)
    with facts._lock(root, True):
        _recover_same(root, owner, conversation_key, operation_id, request_digest)
        previous, before = read_day(root, owner, conversation_key, day)
        if previous and operation_id in previous['operations']:
            operation = previous['operations'][operation_id]
            need(operation['request_digest'] == request_digest, 'diary_idempotency_conflict')
            return {'schema': SCHEMA, 'status': 'already_committed', 'revision': operation['revision'], 'day': day, 'written': False}
        need((previous['revision'] if previous else 0) == expected_revision, 'diary_revision_conflict')
        need(not previous or len(previous['operations']) < 256, 'diary_revision_capacity_reached')
        source = (facts_provider or facts.diary_facts)(workspace=workspace, agent_preset=agent_preset,
                    data_directory=data_directory, day=day, now=now)
        need(source.get('status') == 'available' and source.get('revision') == fact_revision, 'diary_facts_changed_read_again')
        allowed = {item['ref'] for item in source['items']}
        need(set(references) <= allowed, 'diary_reference_not_in_current_facts')
        operations = dict(previous['operations']) if previous else {}
        operations[operation_id] = {'request_digest': request_digest, 'revision': expected_revision + 1}
        value = dict(schema=SCHEMA, owner=owner, scope=conversation_key, day=day, title=title,
                     revision=expected_revision + 1, updated_at=now, fact_revision=fact_revision,
                     coverage=source['coverage'], evidence_refs=sorted(set(references)), feelings_are_agent_report=True,
                     source_evidence=[dict(ref=item['ref'], summary=item['summary'], excerpt_truncated=item.get('excerpt_truncated') is True)
                                      for item in source['items'] if item['ref'] in set(references)], narrative_is_agent_report=True,
                     markdown='# ' + day + ' · ' + title + '\n\n' + '\n\n'.join(paragraphs + ([feelings] if feelings else []) + ([unfinished] if unfinished else [])) + '\n', operations=operations)
        validate(value, owner, conversation_key, day)
        stage = root / ('.stage-' + uuid.uuid4().hex + '.json')
        after = facts._new(stage, facts._encoded(value))
        target = root / (day + '.json'); history = None
        if before is not None:
            facts._copy_security(target, stage); after = facts._record(stage, MAXIMUM)[0]
            old_record, old_raw = facts._record(target, MAXIMUM); need(old_record == before, 'diary_changed_before_commit')
            name = '.history-' + uuid.uuid4().hex + '.json'
            history = {'name': name, 'record': facts._new(root / name, old_raw)}
        need(facts._current(target) == before, 'diary_changed_before_commit')
        pending = dict(schema=SCHEMA, owner=owner, scope=conversation_key, day=day,
                       operation_id=operation_id, before=before, after=after, candidate=stage.name, history=history)
        facts._new(root / 'pending.json', facts._encoded(pending))
        if fault:
            fault('after_intent')
        _recover_same(root, owner, conversation_key, operation_id, request_digest)
    return {'schema': SCHEMA, 'status': 'committed', 'revision': expected_revision + 1, 'day': day, 'written': True}


def pending_diary(**options):
    """Only today's new evidence and yesterday's grounded close-out are due."""
    now = options.get('now', int(time.time() * 1000))
    today = dt.datetime.fromtimestamp(now / 1000, facts.BEIJING).date()
    for day in (today.isoformat(), (today - dt.timedelta(days=1)).isoformat()):
        view = narrative_snapshot(day=day, **options)
        if view['submission'] != 'none':
            return {'schema': SCHEMA, 'status': 'unknown_read_existing', 'day': day, 'write_requested': False}
        if view['new_facts_available']:
            return {'schema': SCHEMA, 'status': 'due', 'day': day, 'fact_revision': view['facts']['revision'],
                    'narrative_revision': view['selected']['revision'] if view['selected'] else 0,
                    'write_requested': False, 'instruction': 'In this same primary main-agent turn, read qq_diary first. If the facts support a useful update, write a brief first-person diary with a specific theme title, confirmed experiences, your own reported feelings and an unfinished thread. Reference actual fact IDs. Do not invent experiences or treat your feelings as facts about the user. This is private self-reflection, never a reason to send QQ or share group/private transcripts.'}
    return {'schema': SCHEMA, 'status': 'current', 'write_requested': False}


def trusted_options(request):
    from project_conf import DEFAULTS
    from runtime_capabilities import _configuration, owner_of
    cwd, preset, scope = request['cwd'], request['agent_preset'], request['conversation_key']
    owner = owner_of(cwd, preset); work = Path(os.path.abspath(cwd))
    configuration = _configuration(work); name = configuration.get('dataDirName', DEFAULTS['dataDirName'])
    need(isinstance(name, str) and name not in ('', '.', '..') and '/' not in name and '\\' not in name, 'invalid_diary_data_directory')
    data = work / name
    _, raw = facts._record(data / 'lanya-config.json', 1024 * 1024)
    identity = json.loads(raw.decode('utf-8-sig')); admin = identity.get('adminQQ')
    if isinstance(admin, list):
        admin = admin[0] if admin else None
    need(not isinstance(admin, bool) and re.fullmatch(r'[1-9][0-9]*', str(admin or '')) and scope == 'private:' + str(admin), 'configured_primary_diary_only')
    return dict(workspace=cwd, agent_preset=preset, data_directory=str(data), conversation_key=scope), owner


def main():
    try:
        need(sys.argv[1:] == ['--stdin-json'], 'diary_stdin_request_required')
        raw = sys.stdin.buffer.read(64 * 1024 + 1); need(len(raw) <= 64 * 1024, 'diary_request_too_large')
        request = facts._json(raw); need(type(request) is dict, 'invalid_diary_request')
        options, owner = trusted_options(request)
        common = {'cwd', 'agent_preset', 'conversation_key', 'action', 'day'}
        if request.get('action') == 'read':
            need(set(request) <= common and common - {'day'} <= set(request), 'invalid_diary_read_request')
            value = narrative_snapshot(day=request.get('day'), **options)
        else:
            fields = {'expected_revision', 'fact_revision', 'title', 'experiences', 'feelings', 'unfinished', 'operation_id'}
            need(request.get('action') == 'write' and set(request) == common | fields, 'invalid_diary_write_request')
            value = write_diary(**options, **{key: request[key] for key in fields | {'day'}})
        value.update(owner=owner, conversation_key=request['conversation_key'])
        sys.stdout.buffer.write(facts._encoded(value))
    except Exception as error:
        sys.stderr.write(type(error).__name__ + '\n')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
