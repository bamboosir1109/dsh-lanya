"""Keep one configured DSH host available; never dispatch models or messages.

Default is a read-only preview. --watch --apply holds one native file lock and
may start only the pinned executable after two empty, confirmed observations.
Living or unverifiable hosts and occupied ports are never killed or replaced.
"""
import argparse
import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import time
import uuid

SCHEMA = 'dsh-main-agent-watch/v1'
STATUS = 'dsh-main-agent-watch-status/v1'
STATES = {'healthy', 'waiting', 'starting', 'blocked', 'paused', 'error'}
REASONS = {'configuration_paused', 'scheduler_and_core_verified', 'existing_or_unverifiable_owner_no_duplicate',
           'confirming_absence_or_restart_backoff', 'host_binary_changed', 'owner_changed_before_start',
           'pinned_host_started_once', 'host_start_failed_backoff', 'configuration_or_probe_unavailable', 'startup_binding_changed'}


def ordinary(path, missing=False):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('absolute_path_required')
    for current in (path, *path.parents):
        try:
            info = current.lstat()
        except FileNotFoundError:
            if current == path and missing:
                continue
            raise
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 1024:
            raise ValueError('ordinary_path_required')
        if current == path and not stat.S_ISREG(info.st_mode):
            raise ValueError('ordinary_file_required')
    return path


def document(path):
    path = ordinary(path)
    if path.stat().st_size > 65536:
        raise ValueError('document_too_large')
    raw = path.read_bytes()
    value = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('object_required')
    return value


def descriptor_signature(stream):
    if os.name != 'nt':
        info = os.fstat(stream.fileno())
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode)
    # Python 3.9's Windows fstat and path stat do not expose identical identity
    # fields. Compare native handle records for both reads instead of dropping
    # the inode check or treating that interpreter difference as a file change.
    import msvcrt
    from ctypes import wintypes as w
    class Info(ctypes.Structure):
        _fields_ = [('attributes', w.DWORD), ('created', w.FILETIME), ('accessed', w.FILETIME),
                    ('written', w.FILETIME), ('volume', w.DWORD), ('size_hi', w.DWORD),
                    ('size_lo', w.DWORD), ('links', w.DWORD), ('index_hi', w.DWORD), ('index_lo', w.DWORD)]
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.GetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.POINTER(Info)]
    info = Info()
    if not k.GetFileInformationByHandle(msvcrt.get_osfhandle(stream.fileno()), ctypes.byref(info)):
        raise OSError('native_file_identity_unavailable')
    return (info.volume, info.index_hi, info.index_lo, info.size_hi, info.size_lo,
            info.written.dwHighDateTime, info.written.dwLowDateTime, info.attributes, info.links)


def file_hash(path):
    path = ordinary(path)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        before = descriptor_signature(stream)
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
        after = descriptor_signature(stream)
    with ordinary(path).open('rb') as current_stream:
        current = descriptor_signature(current_stream)
    if before != after or after != current:
        raise OSError('startup_file_changed_while_reading')
    return digest.hexdigest()


def configuration(path):
    value = document(path)
    if value.get('schema') != SCHEMA or type(value.get('enabled')) is not bool:
        raise ValueError('invalid_configuration')
    exe = ordinary(value.get('host_executable', ''))
    if exe.name.casefold() != 'deepseek harness.exe':
        raise ValueError('fixed_host_executable_required')
    digest = value.get('host_sha256')
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('pinned_host_hash_required')
    if value.get('arguments') != []:
        raise ValueError('default_host_arguments_required')
    bindings = value.get('startup_bindings')
    if not isinstance(bindings, list) or len(bindings) != 2:
        raise ValueError('profile_and_selection_bindings_required')
    for row in bindings:
        if not isinstance(row, dict) or set(row) != {'path', 'sha256'}:
            raise ValueError('startup_binding_invalid')
        ordinary(row['path'])
        digest = row['sha256']
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('startup_binding_invalid')
    if os.path.normcase(bindings[0]['path']) == os.path.normcase(bindings[1]['path']):
        raise ValueError('distinct_startup_bindings_required')
    marker = Path(value.get('scheduler_file', ''))
    if not marker.is_absolute() or marker.name != 'scheduler-in-plugin.flag':
        raise ValueError('fixed_scheduler_file_required')
    ordinary(marker, missing=True)
    ports = value.get('core_ports')
    if not isinstance(ports, list) or len(ports) != 2 or len(set(ports)) != 2 or any(type(p) is not int or not 1 <= p <= 65535 for p in ports):
        raise ValueError('two_core_ports_required')
    return value


def native_process(pid):
    """None means unreadable; False means absent. Birth prevents PID reuse."""
    if os.name != 'nt':
        return None
    from ctypes import wintypes as w
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.OpenProcess.restype = w.HANDLE
    k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    k.GetProcessTimes.argtypes = [w.HANDLE, ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64)]
    k.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    k.CloseHandle.argtypes = [w.HANDLE]
    handle = k.OpenProcess(0x1000, False, pid)
    if not handle:
        return False if ctypes.get_last_error() == 87 else None
    try:
        name = ctypes.create_unicode_buffer(32768)
        size, code = w.DWORD(32768), w.DWORD()
        born, end, kernel, user = (ctypes.c_uint64() for _ in range(4))
        if not (k.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size))
                and k.GetProcessTimes(handle, ctypes.byref(born), ctypes.byref(end), ctypes.byref(kernel), ctypes.byref(user))
                and k.GetExitCodeProcess(handle, ctypes.byref(code))):
            return None
        return {'pid': pid, 'image': name.value, 'birth': str(born.value)} if code.value == 259 else False
    finally:
        k.CloseHandle(handle)


def host_processes(executable):
    if os.name != 'nt':
        raise OSError('windows_required')
    from ctypes import wintypes as w
    class Entry(ctypes.Structure):
        _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD),
                    ('heap', ctypes.c_size_t), ('module', w.DWORD), ('threads', w.DWORD),
                    ('parent', w.DWORD), ('priority', w.LONG), ('flags', w.DWORD), ('name', w.WCHAR * 260)]
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    k.CreateToolhelp32Snapshot.restype = w.HANDLE
    k.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    k.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    k.CloseHandle.argtypes = [w.HANDLE]
    snapshot = k.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise OSError('process_inventory_unavailable')
    rows, unknown = [], False
    try:
        row = Entry(); row.size = ctypes.sizeof(row)
        more = k.Process32FirstW(snapshot, ctypes.byref(row))
        if not more:
            raise OSError('process_inventory_unavailable')
        while more:
            if row.name.casefold() == Path(executable).name.casefold():
                current = native_process(row.pid)
                if current is None:
                    unknown = True
                elif current and os.path.normcase(current['image']) == os.path.normcase(executable):
                    rows.append(current)
                elif current:
                    unknown = True
            more = k.Process32NextW(snapshot, ctypes.byref(row))
        if ctypes.get_last_error() not in (0, 18):
            raise OSError('process_inventory_incomplete')
    finally:
        k.CloseHandle(snapshot)
    return rows, unknown


def port_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.3):
            return True
    except ConnectionRefusedError:
        return False
    except OSError:
        return None


# These DWORD fields are always 32 bits, including a 64-bit Python process.
class TcpRow(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in
                ('state', 'local_address', 'local_port', 'remote_address', 'remote_port', 'pid')]


class Tcp6Row(ctypes.Structure):
    _fields_ = [('local_address', ctypes.c_ubyte * 16), ('local_scope', ctypes.c_uint32),
                ('local_port', ctypes.c_uint32), ('remote_address', ctypes.c_ubyte * 16),
                ('remote_scope', ctypes.c_uint32), ('remote_port', ctypes.c_uint32),
                ('state', ctypes.c_uint32), ('pid', ctypes.c_uint32)]


class TcpTable(ctypes.Structure):
    _fields_ = [('count', ctypes.c_uint32), ('table', TcpRow * 1)]


class Tcp6Table(ctypes.Structure):
    _fields_ = [('count', ctypes.c_uint32), ('table', Tcp6Row * 1)]


TCP_TABLE_MAX_BYTES = 16 * 1024 * 1024
TCP_TABLE_FETCH_ATTEMPTS = 3


def _tcp_table_api():
    if os.name != 'nt':
        raise OSError('windows_required')
    library = ctypes.WinDLL('iphlpapi', use_last_error=True)
    api = library.GetExtendedTcpTable
    api.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int,
                    ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    api.restype = ctypes.c_uint32
    return api


def decode_listener_table(raw, family):
    """Decode only complete owner-PID listener tables; other states are unknown."""
    layouts = {2: (TcpTable, TcpRow), 23: (Tcp6Table, Tcp6Row)}
    if family not in layouts or not 4 <= len(raw) <= TCP_TABLE_MAX_BYTES:
        raise ValueError('listener_table_invalid')
    table, row = layouts[family]
    count = ctypes.c_uint32.from_buffer_copy(raw[:4]).value
    offset, stride = table.table.offset, ctypes.sizeof(row)
    if offset + count * stride > len(raw):
        raise ValueError('listener_table_incomplete')
    ports = set()
    for index in range(count):
        start = offset + index * stride
        item = row.from_buffer_copy(raw[start:start + stride])
        if item.state != 2:
            raise ValueError('listener_table_not_listener')
        port = socket.ntohs(item.local_port & 0xffff)
        if not port:
            raise ValueError('listener_table_invalid_port')
        ports.add(port)
    return ports


def listener_table(family, api=None):
    """None means unknown, including unsupported families or incomplete fetches."""
    if family not in (2, 23):
        return None
    try:
        api = _tcp_table_api() if api is None else api
        size = ctypes.c_uint32()
        result = api(None, ctypes.byref(size), False, family, 3, 0)
        if result not in (0, 122):
            return None
        for _ in range(TCP_TABLE_FETCH_ATTEMPTS):
            capacity = size.value
            if not 4 <= capacity <= TCP_TABLE_MAX_BYTES:
                return None
            buffer = ctypes.create_string_buffer(capacity)
            result = api(buffer, ctypes.byref(size), False, family, 3, 0)
            if result == 122:
                if size.value <= capacity:
                    return None
                continue
            if result != 0 or not 4 <= size.value <= capacity:
                return None
            return decode_listener_table(buffer.raw[:size.value], family)
    except (OSError, ValueError, OverflowError, AttributeError):
        pass
    return None


def listener_observation(core_ports):
    tables = [listener_table(family) for family in (2, 23)]
    present = [True if any(table is not None and port in table for table in tables)
               else False if all(table is not None for table in tables) else None
               for port in core_ports]
    # Expose only the configured ports, not other local services or their owners.
    return {'ipv4_complete': tables[0] is not None, 'ipv6_complete': tables[1] is not None,
            'ports_present': present}


def confirmed_port_absence(connect, before, after):
    if connect is True or before is True or after is True:
        return False
    if (connect is False or connect is None) and before is False and after is False:
        return True
    return None


def restart_ports_absent(evidence):
    ports, absent = evidence.get('ports'), evidence.get('ports_confirmed_absent')
    return (isinstance(ports, list) and len(ports) == 2
            and all(port is False or port is None for port in ports)
            and isinstance(absent, list) and len(absent) == 2
            and all(value is True for value in absent))


def snapshot(config, now=None):
    now = time.time() if now is None else now
    rows, unknown = host_processes(config['host_executable'])
    before = listener_observation(config['core_ports'])
    ports = [port_open(port) for port in config['core_ports']]
    after = listener_observation(config['core_ports'])
    absent = [confirmed_port_absence(connect, first, last) for connect, first, last in
              zip(ports, before['ports_present'], after['ports_present'])]
    fresh, owner_present = False, False
    try:
        marker = document(config['scheduler_file'])
        pid, at = marker.get('pid'), marker.get('at')
        jobs = marker.get('jobs')
        valid = (type(marker.get('version')) is int and marker['version'] == 2
                 and type(pid) is int and 0 < pid <= 0xffffffff and type(at) is int and 0 <= at <= 9007199254740991
                 and isinstance(jobs, list) and 1 <= len(jobs) <= 64 and 'mind' in jobs
                 and all(isinstance(job, str) and 1 <= len(job) <= 128 for job in jobs))
        owner = next((r for r in rows if r['pid'] == pid), None)
        owner_present = bool(valid and owner)
        fresh = bool(owner_present and -5 <= now - at / 1000 <= 120
                     and (int(owner['birth']) - 116_444_736_000_000_000) // 10_000 <= at)
    except (OSError, ValueError, TypeError):
        pass
    return {'host_count': len(rows), 'unverifiable_host': unknown, 'ports': ports,
            'ports_confirmed_absent': absent, 'listener_evidence': {'before': before, 'after': after},
            'scheduler_fresh': fresh, 'scheduler_owner_present': owner_present,
            'host_identity_digest': hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()}


def atomic_status(path, value):
    ordinary(path, missing=True)
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write((json.dumps(value, ensure_ascii=True, sort_keys=True) + '\n').encode())
            stream.flush(); os.fsync(stream.fileno())
        os.replace(str(temp), str(path))
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def public_status(data_directory, now=None, process_probe=native_process):
    """Read-only, bounded UI projection; no PID, paths or configuration body."""
    base = {'configured': False, 'status': 'not_configured', 'reason': 'not_configured'}
    config_path = Path(data_directory) / 'main-agent-watch.config.json'
    if not os.path.lexists(config_path):
        return base
    try:
        config = configuration(config_path)
        if not config['enabled']:
            return dict(base, configured=True, status='paused', reason='configuration_paused')
        value = document(config_path.with_name('main-agent-watch.status.json'))
        pid, born, at = value.get('pid'), value.get('birth'), value.get('at')
        if (value.get('schema') != STATUS or value.get('state') not in STATES or value.get('reason') not in REASONS
                or type(pid) is not int or not 0 < pid <= 0xffffffff
                or not isinstance(born, str) or not born.isdigit() or not 1 <= len(born) <= 20
                or type(at) is not int or not 0 <= at <= 9007199254740991
                or value.get('source_sha256') != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()):
            raise ValueError('watch_evidence_invalid')
        age = (time.time() if now is None else now) - at / 1000
        if not -5 <= age <= 60:
            return dict(base, configured=True, status='offline', reason='watch_heartbeat_stale')
        owner = process_probe(pid)
        allowed = {os.path.normcase(sys.executable), os.path.normcase(str(Path(sys.executable).with_name('pythonw.exe')))}
        if not owner or owner['birth'] != born or os.path.normcase(owner['image']) not in allowed:
            return dict(base, configured=True, status='unknown' if owner is None else 'offline', reason='watch_owner_unverified')
        return dict(base, configured=True, status=value['state'], reason=value['reason'], last_seen_at=at)
    except (OSError, ValueError, TypeError, KeyError):
        return dict(base, configured=True, status='unknown', reason='watch_evidence_unavailable')


@contextlib.contextmanager
def ownership(path):
    """A living watcher keeps this native lock; no stale-file unlink takeover."""
    ordinary(path, missing=True)
    stream = path.open('a+b')
    acquired = False
    try:
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b'\0'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    finally:
        if acquired:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


class Watch:
    def __init__(self, probe=snapshot, launch=None, now=time.time):
        self.probe, self.now = probe, now
        self.launch = launch or self._launch
        self.empty_checks, self.attempts, self.next_start = 0, 0, 0

    @staticmethod
    def _launch(config):
        # Never invoke a shell or select another profile; the pinned default
        # application owns the already configured main conversation service.
        startup = None
        if os.name == 'nt':
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
        return subprocess.Popen([config['host_executable']], cwd=str(Path(config['host_executable']).parent),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                startupinfo=startup,
                                creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) | getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def tick(self, config):
        now = self.now()
        if not config['enabled']:
            self.empty_checks = 0
            return {'state': 'paused', 'reason': 'configuration_paused'}
        current = self.probe(config, now)
        if current['scheduler_fresh'] and all(p is True for p in current['ports']):
            self.empty_checks = 0; self.attempts = 0
            return {'state': 'healthy', 'reason': 'scheduler_and_core_verified'}
        if current['host_count'] or current['unverifiable_host'] or not restart_ports_absent(current):
            self.empty_checks = 0
            return {'state': 'blocked' if current['unverifiable_host'] else 'waiting',
                    'reason': 'existing_or_unverifiable_owner_no_duplicate'}
        self.empty_checks += 1
        if self.empty_checks < 2 or now < self.next_start:
            return {'state': 'waiting', 'reason': 'confirming_absence_or_restart_backoff'}
        if file_hash(config['host_executable']) != config['host_sha256']:
            return {'state': 'blocked', 'reason': 'host_binary_changed'}
        if any(file_hash(row['path']) != row['sha256'] for row in config['startup_bindings']):
            return {'state': 'blocked', 'reason': 'startup_binding_changed'}
        # Re-read ownership after hashing, immediately before starting. Never
        # restart a living owner just because a heartbeat is old.
        fresh = self.probe(config, self.now())
        if fresh['host_count'] or fresh['unverifiable_host'] or not restart_ports_absent(fresh):
            self.empty_checks = 0
            return {'state': 'blocked', 'reason': 'owner_changed_before_start'}
        if file_hash(config['host_executable']) != config['host_sha256'] or any(file_hash(row['path']) != row['sha256'] for row in config['startup_bindings']):
            return {'state': 'blocked', 'reason': 'startup_binding_changed'}
        self.attempts += 1
        self.next_start = now + min(900, 120 * 2 ** min(self.attempts - 1, 3))
        self.empty_checks = 0
        try:
            process = self.launch(config)
            return {'state': 'starting', 'reason': 'pinned_host_started_once', 'started_pid': process.pid}
        except OSError:
            return {'state': 'error', 'reason': 'host_start_failed_backoff'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    config_path = ordinary(Path(args.config))
    config = configuration(config_path)
    if not args.watch:
        print(json.dumps({'read_only': True, 'enabled': config['enabled'], 'snapshot': snapshot(config)}))
        return
    if not args.apply:
        raise ValueError('explicit_apply_required')
    lock = config_path.with_name('main-agent-watch.lock')
    output = config_path.with_name('main-agent-watch.status.json')
    watch = Watch()
    with ownership(lock):
        identity = native_process(os.getpid())
        if not identity:
            raise OSError('watcher_identity_unavailable')
        source_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        while True:
            try:
                config = configuration(config_path)
                result = watch.tick(config)
            except (OSError, ValueError, TypeError):
                result = {'state': 'error', 'reason': 'configuration_or_probe_unavailable'}
            try:
                atomic_status(output, dict(result, schema=STATUS, at=int(time.time() * 1000),
                                           pid=os.getpid(), birth=identity['birth'], source_sha256=source_sha,
                                           restart_attempts=watch.attempts, next_start_at=int(watch.next_start * 1000)))
            except OSError:
                # A transient status-file failure must not stop supervision.
                pass
            time.sleep(15)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'ok': False, 'error_class': type(error).__name__}))
        raise SystemExit(1)
