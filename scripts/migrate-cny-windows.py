"""Explicit Windows migration using a pinned runtime and exact security copies.

The operator must first stop Main and prevent automatic restart, for example
by pausing its watcher. This tool does not verify or control processes. It
never calls models or messaging platforms.
Default preview is read only. Applying requires a new archive/evidence folder,
a fresh fingerprint, and an explicit acknowledgement of the stopped Main.
"""
import argparse
import ast
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import traceback
import uuid

sys.dont_write_bytecode = True

RUNTIME_SHA = '9aaf91f59265cbc93862fc39fed168e9c429f0bef86f6045bd1208eba5b8efa0'
DEPENDENCIES = {
    'panel_autonomy': 'dd032a8b7fcbc095222a06c59d6bdab229b1757f15d2b4b5ea3223febb04f76f',
    'project_conf': '21c0cdf94c02ca24aa712c30d5718c38d0a62f51c8a0e6aa8002468c4dc09261'}
MAXIMUM = 64 * 1024 * 1024


def need(value, code):
    if not value:
        raise ValueError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2,
                       allow_nan=False) + '\n').encode('utf-8')


def ordinary(path, missing=False):
    path = Path(os.path.abspath(path))
    for item in (*reversed(path.parents), path):
        if os.path.lexists(item):
            value = item.lstat()
            need(not stat.S_ISLNK(value.st_mode) and
                 not getattr(value, 'st_file_attributes', 0) & 0x400, 'OrdinaryPathRequired')
        else:
            need(missing, 'ExistingPathRequired')
    resolved = path.resolve()
    need(resolved == path, 'CanonicalExplicitPathRequired')
    return path


def write_new(path, raw):
    path = ordinary(path, missing=True)
    need(type(raw) is bytes, 'RawBytesRequired')
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def save(path, value):
    raw = encoded(value)
    write_new(path, raw)
    return digest(raw)


@contextmanager
def runtime_environment(workdir, project):
    values = {'LANYA_WORKDIR': str(workdir), 'LANYA_PROJECT_CONFIG': str(project)}
    before = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def load_runtime(path):
    path = ordinary(path)
    need(path.is_file() and path.stat().st_nlink == 1 and digest(path.read_bytes()) == RUNTIME_SHA,
         'PinnedRuntimeRequired')
    for name, expected in DEPENDENCIES.items():
        dependency = ordinary(path.parent / (name + '.py'))
        need(dependency.stat().st_nlink == 1 and digest(dependency.read_bytes()) == expected,
             'PinnedRuntimeDependencyRequired')
        cached = sys.modules.get(name)
        need(cached is None or Path(cached.__file__).resolve() == dependency,
             'ForeignCachedRuntimeDependency')
    before = list(sys.path)
    try:
        sys.path.insert(0, str(path.parent))
        spec = importlib.util.spec_from_file_location('cny_windows_runtime_' + uuid.uuid4().hex, path)
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
    finally:
        sys.path[:] = before
    need(Path(api.__file__).resolve() == path and api.MIGRATION_SOURCE_SHA256 == RUNTIME_SHA,
         'RealRuntimeModuleIdentityRequired')
    return api


class SameParentCopier:
    """Only two API-created private files are eligible; sources are read only."""
    def __init__(self, api, configuration, database, archive, evidence, bindings, fault=None):
        self.api, self.original = api, api._copy_security
        self.configuration, self.database = ordinary(configuration), ordinary(database)
        self.archive, self.evidence = ordinary(archive, missing=True), ordinary(evidence)
        self.bindings, self.fault, self.completed = bindings, fault, {}
        self.allowed = {archive / 'database-before.sqlite': ('database', self.database),
                        archive / 'configuration-after.json': ('configuration', self.configuration)}

    def read(self, path):
        return self.api._secure_record(ordinary(path), MAXIMUM)

    def event(self, phase, role):
        if self.fault is not None:
            self.fault(phase, role)

    def __call__(self, source, target):
        source, target = ordinary(source), ordinary(target)
        need(target in self.allowed, 'OnlyApiCreatedPrivateCopyTargetsAllowed')
        role, expected = self.allowed[target]
        need(source == expected and role not in self.completed and target.parent == self.archive and
             target not in (self.configuration, self.database) and source != target,
             'KnownCopySourceAndSingleInvocationRequired')
        source_before, _ = self.read(source)
        target_before, raw = self.read(target)
        need(source_before['signature'][:2] != target_before['signature'][:2],
             'CanonicalSourceCannotBeCopyDestination')
        need(source.parent.stat().st_dev == self.archive.stat().st_dev == self.evidence.stat().st_dev,
             'SameVolumeSecurityStagingRequired')
        temporary = source.with_name('.lanya-cny-maintenance-' + uuid.uuid4().hex + '.tmp')
        retained = self.evidence / (role + '.original-private-file')
        need(temporary.parent == source.parent and temporary not in self.allowed and
             temporary not in (self.configuration, self.database) and
             not os.path.lexists(temporary) and not os.path.lexists(retained),
             'ExclusiveSecurityStagingPathsRequired')
        intent = {'format': 'cny-windows-security-stage-intent/v1', 'role': role,
            'source': str(source), 'target': str(target), 'temporary': str(temporary),
            'retained_private_target': str(retained), 'source_before': source_before,
            'target_before': target_before, 'bindings': self.bindings,
            'canonical_source_writes_by_copier': 0, 'automatic_retry': False}
        intent_sha = save(self.evidence / (role + '.intent.json'), intent)
        self.event('before_temp_create', role)
        write_new(temporary, raw)
        os.chmod(temporary, stat.S_IMODE(target_before['signature'][4]))
        # The original copier acts only on this fresh same-parent inode. Its
        # exact inherited-SD guard remains unchanged and is never relaxed.
        self.original(source, temporary)
        replacement, copied = self.read(temporary)
        need(copied == raw and replacement['sha256'] == target_before['sha256'] and
             replacement['signature'][4] == target_before['signature'][4] and
             replacement['security_sha256'] == source_before['security_sha256'],
             'ExactTemporaryBytesModeAndFullSecurityRequired')
        need(self.read(source)[0] == source_before and self.read(target)[0] == target_before,
             'SourceOrOwnedPrivateTargetChanged')
        self.event('before_private_target_move', role)
        # Final CAS after the hook point; Windows rename never overwrites an
        # existing destination. Preserve the initial API-created private inode.
        need(self.read(source)[0] == source_before and self.read(target)[0] == target_before and
             self.read(temporary)[0] == replacement and not os.path.lexists(retained),
             'FinalSecurityStagingCasRequired')
        os.rename(target, retained)
        need(self.read(retained)[0] == target_before and not os.path.lexists(target),
             'OriginalPrivateTargetNotRetained')
        self.event('before_final_rename', role)
        need(self.read(source)[0] == source_before and self.read(temporary)[0] == replacement and
             not os.path.lexists(target), 'FinalSecurityRenameOwnershipRequired')
        os.rename(temporary, target)
        need(self.read(target)[0] == replacement and self.read(retained)[0] == target_before and
             self.read(source)[0] == source_before, 'ExactRenamedPhysicalSecurityCopyRequired')
        receipt = dict(intent, format='cny-windows-security-stage/v1', success=True,
            intent_sha256=intent_sha, replacement_record=replacement,
            retained_private_target_record=target_before, source_unchanged_during_stage=True)
        receipt_sha = save(self.evidence / (role + '.result.json'), receipt)
        self.completed[role] = {'path': str(self.evidence / (role + '.result.json')),
                                'sha256': receipt_sha}


def run(args, fault=None):
    need(os.name == 'nt', 'WindowsOnlyMaintenanceTool')
    workdir, project = ordinary(args.workdir), ordinary(args.project_config)
    runtime = ordinary(args.runtime)
    need(workdir.is_dir() and project.is_file(), 'ExplicitExistingWorkdirAndProjectRequired')
    with runtime_environment(workdir, project):
        api = load_runtime(runtime)
        api.project_conf.load(reload=True)
        need(Path(api.project_conf.config_path()) == project, 'ExplicitProjectConfigurationRequired')
        configuration, database = [ordinary(path) for path in api._paths()]
        need(configuration.parent == database.parent and configuration.is_relative_to(workdir) and
             configuration.name == 'autonomy-budget.json' and database.name == 'autonomy-budget.sqlite',
             'ConfinedCanonicalBudgetPathsRequired')
        if not args.apply:
            preview = api.migration_preview()
            return {'success': True, 'applied': False, 'fingerprint': preview['fingerprint'],
                    'requires_stopped_main': True, 'settings_after': preview['settings_after'],
                    'turn_count': preview['turn_count'], 'turn_rows_sha256': preview['turn_rows_sha256']}
        need(args.confirm_main_stopped is True and re.fullmatch('[0-9a-f]{64}', args.fingerprint or ''),
             'ExplicitStoppedMainAcknowledgementAndFingerprintRequired')
        archive, evidence = ordinary(args.archive, missing=True), ordinary(args.evidence_dir, missing=True)
        need(archive != evidence and not archive.is_relative_to(evidence) and not evidence.is_relative_to(archive)
             and not os.path.lexists(archive) and not os.path.lexists(evidence) and
             archive.parent.is_dir() and evidence.parent.is_dir() and
             archive.parent.stat().st_dev == evidence.parent.stat().st_dev == configuration.stat().st_dev,
             'NewIndependentSameVolumeArchiveAndEvidenceRequired')
        caller = ordinary(args.caller_file)
        caller_record, _ = api._secure_record(caller, MAXIMUM)
        need(caller_record['sha256'] == args.expected_caller_sha256,
             'ExplicitPinnedCallerRequired')
        tool_record, tool_raw = api._secure_record(Path(__file__), MAXIMUM)
        runtime_record, _ = api._secure_record(runtime, MAXIMUM)
        tool_text = tool_raw.decode('utf-8')
        copier_node = next(node for node in ast.parse(tool_text).body
                           if isinstance(node, ast.ClassDef) and node.name == 'SameParentCopier')
        copier_sha = digest(ast.get_source_segment(tool_text, copier_node).encode('utf-8'))
        bindings = {'tool_sha256': tool_record['sha256'], 'copier_sha256': copier_sha,
                    'runtime_sha256': RUNTIME_SHA, 'caller_sha256': caller_record['sha256']}
        evidence.mkdir(mode=0o700)
        execution = {'format': 'cny-windows-migration-execution-intent/v1', 'bindings': bindings,
            'runtime': str(runtime), 'caller': str(caller), 'archive': str(archive),
            'fingerprint': args.fingerprint, 'requires_stopped_main': True,
            'operator_acknowledged_main_stopped': True, 'main_stop_verified_by_this_tool': False,
            'api_invocations': 1, 'automatic_retry': False}
        intent_sha = save(evidence / 'intent.json', execution)
        original = api._copy_security
        copier = SameParentCopier(api, configuration, database, archive, evidence, bindings, fault)
        try:
            try:
                api._copy_security = copier
                result = api.migrate_cny(args.fingerprint, archive)
            finally:
                api._copy_security = original
            need(api._copy_security is original and set(copier.completed) == {'database', 'configuration'} and
                 api._secure_record(runtime, MAXIMUM)[0] == runtime_record and
                 api._secure_record(caller, MAXIMUM)[0] == caller_record,
                 'OriginalCopierOrPinnedCodeChanged')
            result_record, raw = api._secure_record(archive / 'result.json', MAXIMUM)
            need(json.loads(raw) == json.loads(encoded(result)) and result['success'] is True and
                 result['migration_source_sha256'] == RUNTIME_SHA and
                 result['turn_row_mutations'] == result['source_ledger_mutations'] == 0,
                 'GenuineNumericPreservingMigrationResultRequired')
            receipt = dict(execution, format='cny-windows-migration-execution/v1', success=True,
                execution_intent_sha256=intent_sha, stages=copier.completed,
                original_module_copier_restored=True, migration_result_sha256=result_record['sha256'],
                canonical_source_writes_by_copier=0, turn_row_mutations=0, source_ledger_mutations=0)
            receipt_sha = save(evidence / 'result.json', receipt)
            return {'success': True, 'applied': True, 'migration_result_sha256': result_record['sha256'],
                    'execution_receipt_sha256': receipt_sha, 'execution_receipt': str(evidence / 'result.json'),
                    'turn_count': result['turn_count'], 'turn_rows_sha256': result['turn_rows_sha256'],
                    'automatic_retry': False}
        except BaseException as error:
            # Diagnostic failure never masks the original exception. Unknown
            # private stages and originals are retained for explicit inspection.
            try:
                save(evidence / 'failure.json', dict(execution, success=False,
                    error_class=type(error).__name__, private_message=str(error),
                    private_trace_without_locals=traceback.format_exc(), completed_stages=copier.completed,
                    original_module_copier_restored=api._copy_security is original))
            except BaseException:
                pass
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('runtime', 'workdir', 'project-config'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('archive', 'evidence-dir', 'caller-file'):
        parser.add_argument('--' + name, type=Path)
    parser.add_argument('--expected-caller-sha256')
    parser.add_argument('--fingerprint')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm-main-stopped', action='store_true')
    args = parser.parse_args()
    if args.apply:
        need(all(getattr(args, name) is not None for name in
                 ('archive', 'evidence_dir', 'caller_file', 'expected_caller_sha256')), 'ExplicitApplyPathsRequired')
    result = run(args)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'success': False, 'error_class': type(error).__name__,
                          'error_code': getattr(error, 'code', str(error)),
                          'automatic_retry': False}), file=sys.stderr)
        raise SystemExit(1)
