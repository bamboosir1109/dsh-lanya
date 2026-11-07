"""Start an existing, operator-installed QQ backend without parallel injectors.

No binary installation, blanket QQ termination or agreement/password bypass.
The vendor's own WebUI owns injection, login and legal onboarding.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.parse
import uuid
import re
import argparse
import itertools

import qq_backend as backend


def _listening(url):
    parsed = urllib.parse.urlsplit(url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)), timeout=0.5):
            return True
    except ConnectionRefusedError:
        return False
    except OSError:
        return None


def _native(pid):
    if type(pid) is not int or pid <= 0:
        return None
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
            stat = Path('/proc/' + str(pid) + '/stat').read_text().rsplit(')', 1)[1].split()
            if stat[0] == 'Z':
                return False
            return {'pid': pid, 'birth': stat[19], 'exe': os.readlink('/proc/' + str(pid) + '/exe')}
        except ProcessLookupError:
            return False
        except (OSError, ValueError):
            return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False if ctypes.get_last_error() == 87 else None
    try:
        size = wintypes.DWORD(32768); buffer = ctypes.create_unicode_buffer(size.value)
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        creation, exit_time, system, user = (wintypes.FILETIME() for _ in range(4))
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(system), ctypes.byref(user)):
            return None
        if exit_time.dwHighDateTime or exit_time.dwLowDateTime:
            return False
        return {'pid': pid, 'birth': str((creation.dwHighDateTime << 32) | creation.dwLowDateTime), 'exe': buffer.value}
    finally:
        kernel.CloseHandle(handle)


def _qq_client_state():
    """True=QQ exists, False=confirmed absent, None=enumeration unverified."""
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
            ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
            ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
            ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
            ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    for name in ('Process32FirstW', 'Process32NextW'):
        api = getattr(kernel, name); api.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]; api.restype = wintypes.BOOL
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = Entry(); entry.dwSize = ctypes.sizeof(entry)
        if not kernel.Process32FirstW(handle, ctypes.byref(entry)):
            return None
        for _ in range(32768):
            if entry.szExeFile.casefold() == 'qq.exe':
                return True
            if not kernel.Process32NextW(handle, ctypes.byref(entry)):
                return False if ctypes.get_last_error() == 18 else None
        return None
    finally:
        kernel.CloseHandle(handle)


class LaunchJournal:
    """Shared durable launch facts. Completion proves creation, never login."""
    def __init__(self, directory, store):
        self.store = store
        self.directory = store._ordinary(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def read(self, file):
        try:
            record, raw = self.store._record(file)
            return self.store._json(raw), record
        except (OSError, ValueError, RuntimeError):
            raise backend.BackendError('Backend launch record is unreadable; inspect before retrying') from None

    @staticmethod
    def valid_process(value, pid=None):
        return (isinstance(value, dict) and set(value) == {'pid', 'birth', 'exe'} and
            type(value.get('pid')) is int and value['pid'] > 0 and (pid is None or value['pid'] == pid) and
            isinstance(value.get('birth'), str) and 0 < len(value['birth']) <= 128 and
            isinstance(value.get('exe'), str) and Path(value['exe']).is_absolute())

    def current(self, provider, native):
        file = self.directory / (provider + '-process.json')
        if not file.exists():
            if os.path.lexists(file):
                raise backend.BackendError('Backend launch receipt is not an ordinary file')
            return None, False
        receipt, _ = self.read(file)
        identity = receipt.get('process')
        if receipt.get('provider') != provider or not self.valid_process(identity):
            raise backend.BackendError('Previous backend launch receipt is invalid; inspect before retrying')
        actual = native(identity['pid'])
        if actual != identity and actual is not False:
            raise backend.BackendError('Previous backend process identity is unconfirmed; do not start a duplicate')
        return receipt, actual == identity

    def write_new(self, file, value):
        file = self.store._ordinary(file)
        raw = (json.dumps(value, sort_keys=True) + '\n').encode()
        with file.open('xb') as output:
            output.write(raw); output.flush(); os.fsync(output.fileno())
        _, actual = self.store._record(file)
        if raw != actual:
            raise backend.BackendError('Backend launch record could not be verified')

    def settled(self, provider, receipt=None):
        files = list(itertools.islice(self.directory.glob(provider + '-*.intent.json'), 8193))
        if len(files) > 8192:
            raise backend.BackendError('Backend history needs inspection before another launch')
        pairs = []
        for intent in files:
            value, original = self.read(intent)
            operation = value.get('operation')
            if (not isinstance(operation, str) or not re.fullmatch(r'[a-f0-9]{32}', operation) or
                    intent.name != provider + '-' + operation + '.intent.json' or value.get('provider') != provider or
                    value.get('automatic_retry') is not False):
                raise backend.BackendError('Backend launch intent is invalid; inspect before retrying')
            result = intent.with_name(intent.name.replace('.intent.json', '.result.json'))
            # A receipt is committed only after a native process identity was
            # verified. It repairs exactly the crash gap before result writing;
            # an intent without that evidence is never replayed.
            if not result.exists() and receipt and receipt.get('operation') == operation:
                self.write_new(result, dict(operation=operation, started=True, process=receipt['process'], recovered_from_receipt=True))
            if not result.exists():
                raise backend.BackendError('A previous backend launch is unconfirmed; inspect before retrying')
            outcome, result_record = self.read(result)
            if outcome.get('operation') != operation or outcome.get('started') is not True or not self.valid_process(outcome.get('process')):
                raise backend.BackendError('Backend launch result is invalid; inspect before retrying')
            pairs.append((original['signature'][3], operation, intent, original, result, result_record))
        # Preserve completed history without a lifetime restart limit. Only
        # individually verified files move, within this same private directory.
        old = sorted(pairs)[:-32]
        if old:
            archive = self.store._ordinary(self.directory / 'archive'); archive.mkdir(mode=0o700, exist_ok=True)
            base = self.directory.resolve()
            for _, operation, intent, original, result, result_record in old:
                if receipt and receipt.get('operation') == operation:
                    continue
                for source, expected in ((intent, original), (result, result_record)):
                    target = self.store._ordinary(archive / source.name)
                    if (os.path.commonpath([str(base), str(source.resolve())]) != str(base) or
                            os.path.commonpath([str(base), str(target.resolve())]) != str(base) or target.exists() or
                            self.store._record(source)[0] != expected):
                        raise backend.BackendError('Backend launch history changed; archive verification required')
                    source.rename(target)
                    if self.store._record(target)[0] != expected:
                        raise backend.BackendError('Backend launch archive needs verification')

    def begin(self, provider):
        operation = uuid.uuid4().hex
        intent = self.directory / (provider + '-' + operation + '.intent.json')
        self.write_new(intent, dict(operation=operation, provider=provider, automatic_retry=False))
        return operation, intent

    def complete(self, provider, operation, intent, identity, **metadata):
        value = dict(provider=provider, process=identity, operation=operation, at=int(time.time() * 1000), **metadata)
        candidate = self.directory / ('.' + operation + '.json')
        self.write_new(candidate, value)
        receipt = self.directory / (provider + '-process.json')
        self.store._ordinary(receipt)
        os.replace(candidate, receipt)
        actual, _ = self.read(receipt)
        if actual != value:
            raise backend.BackendError('Backend launch receipt needs verification; do not retry')
        self.write_new(intent.with_name(intent.name.replace('.intent.json', '.result.json')),
            dict(operation=operation, started=True, process=identity))


def _other_backend_idle(project, selected, journal, native, listening):
    for key in backend.BACKENDS:
        if key == selected:
            continue
        spec, settings, root, config, _, _ = backend.describe(project=project, provider=key)
        receipt, alive = journal.current(key, native)
        if alive:
            raise backend.BackendError('Another owned backend process is active; stop its injection before switching')
        journal.settled(key, receipt)
        try:
            other = backend.resolve(project=project, provider=key)
        except backend.BackendConfigurationMissing:
            other = None
        except backend.BackendError:
            if os.path.lexists(config) or backend.installed(spec, root):
                raise backend.BackendError('Another backend configuration is unverified; inspect it before starting') from None
            continue
        if other is not None and listening(other.base_url) is not False:
            raise backend.BackendError('Another QQ backend endpoint is busy; stop its injection before starting this backend')
        if other is not None or backend.installed(spec, root):
            address = backend.webui_address(spec, settings, root)
            if listening(address) is not False:
                raise backend.BackendError('Another backend WebUI is active or unconfirmed; inspect it before starting')


def start(*, napcat_launcher=None, listening=_listening, launch=subprocess.Popen, native=_native, preflight=subprocess.run,
        expected_provider=None, expected_revision=None, qq_client_state=_qq_client_state):
    conf = backend._conf(); project = conf.load(reload=True)
    spec, settings, root, _, _, _ = backend.describe(project=project)
    if spec.key not in ('napcat', 'snowluma'):
        raise backend.BackendError('This backend has no registered lifecycle handler')
    import panel_autonomy as store

    def selected_unchanged():
        if conf.load(reload=True) != project or expected_provider not in (None, spec.key):
            raise backend.BackendError('Backend selection changed during startup')
        if expected_revision is not None:
            import qq_backend_selection as selection
            path, record, _, _ = selection._project()
            if store._revision(path, record) != expected_revision:
                raise backend.BackendError('Backend configuration changed; refresh before starting')

    selected_unchanged()
    # Process receipts and launch intents are operator state, outside the
    # character's writable home. Selecting a different QQ provider does not
    # move this common service directory into the vendor's installation.
    runtime = store._ordinary(Path(conf.work_dir()) / 'napcat')
    journal = LaunchJournal(runtime / 'backend-runtime', store)
    selected_config = Path(conf.config_path()) if conf.config_path() else runtime / 'backend-start'
    # Selection and both provider launchers share one cross-process lock.
    with store._write_lock(selected_config):
        selected_unchanged()
        webui = backend.webui_address(spec, settings, root)
        if spec.key == 'napcat':
            current = backend.resolve(project=project)
            state = listening(current.base_url)
            if state is None:
                raise backend.BackendError('Selected backend port state is unknown; verify it before starting')
            if state is True:
                version = backend.call('get_version_info', connection=current, timeout=3)['data']
                if not isinstance(version, dict) or not isinstance(version.get('app_name'), str) or not re.fullmatch(spec.app_pattern, version['app_name'], re.I):
                    raise backend.BackendError('Selected backend port belongs to an unverified service')
                selected_unchanged()
                return dict(ok=True, provider=spec.key, started=False, status='already_running',
                    webui_url=webui, connection_verified=False)
        receipt, alive = journal.current(spec.key, native)
        journal.settled(spec.key, receipt)
        if alive:
            return dict(ok=True, provider=spec.key, started=False, status='already_running',
                webui_url=webui, connection_verified=False)
        _other_backend_idle(project, spec.key, journal, native, listening)
        if listening(webui) is not False:
            raise backend.BackendError('Backend WebUI port is in use; verify the existing service before starting')
        if spec.key == 'napcat':
            # Existing local launchers may close QQ before injection. A failed
            # HTTP check does not authorize closing any live/unknown client.
            if qq_client_state() is not False:
                raise backend.BackendError('A QQ client is running or unverified; inspect it in QQ before requesting injection')
            if napcat_launcher is None:
                raise backend.BackendError('NapCat launcher is unavailable')
            selected_unchanged()
            operation, intent = journal.begin(spec.key)
            pid = napcat_launcher()
            identity = native(pid)
            if not journal.valid_process(identity, pid):
                raise backend.BackendError('NapCat launcher identity is unconfirmed; inspect before retrying')
            journal.complete(spec.key, operation, intent, identity)
            return dict(ok=True, provider=spec.key, started=True, status='starting', pid=pid,
                webui_url=webui, connection_verified=False)

        entry = root / 'index.mjs'; executable = root / ('node.exe' if os.name == 'nt' else 'node')
        checker = root / 'check-node-version.cjs'
        entry_record, _ = store._record(entry, 32 * 1024 * 1024)
        executable_record, _ = store._record(executable, 128 * 1024 * 1024)
        checker_record, _ = store._record(checker)
        # A project-side WebUI override cannot change the vendor's real port.
        runtime_path = root / 'config/runtime.json'
        runtime, runtime_pin = backend._read(runtime_path, optional=True)
        actual_port = (runtime or {}).get('webuiPort', spec.webui_port)
        if webui != 'http://127.0.0.1:' + str(backend._port(actual_port)):
            raise backend.BackendError('Project and vendor WebUI ports differ; configure the vendor before starting')
        environment = dict(os.environ)
        for key in ['SNOWLUMA_ACCEPT_EULA', 'SNOWLUMA_ACCEPT_PRIVACY', 'SNOWLUMA_DEV_MODE', 'SNOWLUMA_DEV_WEBUI']:
            environment.pop(key, None)
        try:
            probe = preflight([str(executable), str(checker)], cwd=str(root), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, env=environment,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.SubprocessError):
            raise backend.BackendError('Backend Node preflight could not be verified') from None
        if probe.returncode != 0:
            raise backend.BackendError('Backend Node version is unsupported by this release')
        if (store._record(entry, 32 * 1024 * 1024)[0] != entry_record or
                store._record(executable, 128 * 1024 * 1024)[0] != executable_record or
                store._record(checker)[0] != checker_record):
            raise backend.BackendError('Backend runtime changed during startup')
        selected_unchanged()
        if backend._read(runtime_path, optional=True)[1] != runtime_pin:
            raise backend.BackendError('Vendor runtime configuration changed during startup')
        _other_backend_idle(project, spec.key, journal, native, listening)
        operation, intent = journal.begin(spec.key)
        log = journal.directory / (spec.key + '-' + operation + '.log')
        with log.open('xb') as stream:
            process = launch([str(executable), str(entry)], cwd=str(root), stdin=subprocess.DEVNULL,
                stdout=stream, stderr=subprocess.STDOUT, env=environment,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        identity = native(process.pid)
        if (not journal.valid_process(identity, process.pid) or
                os.path.normcase(identity['exe']) != os.path.normcase(str(executable))):
            raise backend.BackendError('Started backend identity is unconfirmed; inspect it before retrying')
        journal.complete(spec.key, operation, intent, identity, entry_sha256=entry_record['sha256'],
            executable_sha256=executable_record['sha256'])
        return dict(ok=True, provider=spec.key, started=True, status='starting', webui_url=webui,
            login_in_vendor_webui=True, connection_verified=False)


def recover(*, inspect=backend.runtime_status, starter=start):
    value = inspect()
    if value.get('verified') is True:
        return dict(ok=True, provider=value['provider'], status='online', started=False)
    if value.get('provider') == 'snowluma':
        # start refuses busy ports and unconfirmed process identities. A live
        # logged-out runtime stays available for vendor WebUI recovery.
        result = starter()
        result['connection_verified'] = False
        result['action_required'] = result.get('status') == 'already_running'
        return result
    return dict(ok=False, provider=value.get('provider'), status='operator_check_required',
        started=False, action_required=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recover', action='store_true', required=True)
    parser.parse_args()
    try:
        result = recover()
        if result.get('status') != 'online':
            print(json.dumps(result, ensure_ascii=True))
        return 0 if result.get('ok') else 2
    except Exception:
        # Preserve private launch intents, but never print exception paths.
        print(json.dumps(dict(ok=False, status='backend_recovery_unconfirmed', automatic_retry=False)))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
