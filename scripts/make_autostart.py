"""Register a hidden launcher for the installed canonical startup script.

Never regenerates an obsolete duplicate start-all.ps1, installs an injector,
changes the scheduler mode or overwrites an unknown Windows Startup entry.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import stat
import uuid


class StartupError(ValueError):
    pass


def configure(launcher, startup, backups, *, what_if=False):
    import panel_autonomy as store
    launcher = store._ordinary(launcher)
    startup, backups = store._ordinary(startup), store._ordinary(backups)
    record, raw = store._record(launcher)
    if b'qq-backend-heal.ps1' not in raw or b"ValidateSet('External', 'Plugin')" not in raw:
        raise StartupError('Install the current canonical start-all.ps1 before registering startup')
    command = 'powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + str(launcher) + '"'
    body = ('Set shell = CreateObject("WScript.Shell")\r\n'
            'shell.Run "' + command.replace('"', '""') + '", 0, False\r\n').encode('utf-16')
    target = startup / 'lanya-start-all.vbs'
    previous = startup / 'lanYa-start-all.cmd'
    current_record = previous_record = None
    if target.exists():
        current_record, existing = store._record(target)
        if existing != body:
            raise StartupError('An existing Startup entry is not owned by this workspace; no changes made')
    if previous.exists():
        previous_record, previous_raw = store._record(previous)
        if os.name == 'nt' and previous_record['signature'][6] & 1:
            raise StartupError('The previous Startup command is readonly; no changes made')
        expected = '@echo off\nstart "" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + str(launcher) + '"'
        try:
            actual = previous_raw.decode('ascii').replace('\r\r\n', '\n').replace('\r\n', '\n').strip()
        except UnicodeError:
            actual = ''
        if actual != expected:
            raise StartupError('The previous Startup command is unknown; no changes made')
    result = dict(registered=current_record is not None, changed=current_record is None or previous_record is not None,
        what_if=what_if, automatic_run=False, script_rewritten=False, backup_retained=False)
    result['backup_planned'] = previous_record is not None
    if what_if:
        return result
    backups.mkdir(parents=True, exist_ok=True)
    with store._write_lock(backups / 'startup-management'):
        if store._record(launcher)[0] != record:
            raise StartupError('Canonical startup script changed; no entry installed')
        if previous_record is not None:
            if store._record(previous)[0] != previous_record:
                raise StartupError('Previous Startup command changed')
            original = backups / ('startup-' + uuid.uuid4().hex + '.cmd.original')
            with original.open('xb') as output:
                output.write(previous_raw); output.flush(); os.fsync(output.fileno())
            os.chmod(original, stat.S_IMODE(previous_record['signature'][4]))
            store._copy_security(previous, original)
            saved, saved_raw = store._record(original)
            if saved_raw != previous_raw or saved['security_sha256'] != previous_record['security_sha256']:
                raise StartupError('Startup original could not be preserved')
            result['backup_retained'] = True
        startup.mkdir(parents=True, exist_ok=True)
        if current_record is None:
            with target.open('xb') as output:
                output.write(body); output.flush(); os.fsync(output.fileno())
        if store._record(target)[1] != body:
            raise StartupError('Startup registration could not be verified')
        if previous_record is not None:
            if store._record(previous)[0] != previous_record:
                raise StartupError('Previous Startup command changed; original is preserved')
            # Exact owned entry only, after a complete byte/ACL-preserved backup
            # and verified replacement. Unknown entries were refused above.
            previous.unlink()
    result['registered'] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--what-if', action='store_true')
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    try:
        from project_conf import work_dir, data_dir
        launcher = Path(work_dir()) / 'napcat/start-all.ps1'
        startup = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/Startup'
        result = configure(launcher, startup, Path(data_dir()) / 'maintenance/autostart-originals', what_if=args.what_if)
        if args.run and not args.what_if:
            from hidden_task import powershell_executable
            process = subprocess.Popen([str(powershell_executable()), '-NoLogo', '-NoProfile', '-NonInteractive',
                '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', str(launcher)], cwd=str(launcher.parent),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            result.update(automatic_run=True, pid=process.pid)
        print(json.dumps(result, ensure_ascii=True)); return 0
    except StartupError as error:
        print(json.dumps(dict(ok=False, error=str(error), automatic_run=False))); return 1
    except Exception:
        print(json.dumps(dict(ok=False, status='startup_registration_unconfirmed', automatic_run=False)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
