"""Run one local PowerShell file from a pythonw scheduled-task action."""
import argparse
import ctypes
import os
from pathlib import Path
import stat
import subprocess
import sys


REPARSE_POINT = 0x400


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid arguments")


def local_file(raw, suffix):
    """Reject network, device, alternate-stream and redirected file paths."""
    if not isinstance(raw, str) or not raw or "\0" in raw or raw.startswith(("\\\\", "//")):
        raise ValueError("Local absolute file required")
    path = Path(raw)
    if not path.is_absolute() or path.suffix.lower() != suffix:
        raise ValueError("Local absolute file required")
    if os.name == "nt" and (len(path.drive) != 2 or path.drive[1] != ":"):
        raise ValueError("Local drive required")
    if ".." in path.parts or any(":" in part for part in path.parts[1:]):
        raise ValueError("Plain file path required")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & REPARSE_POINT:
            raise ValueError("Redirected path refused")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Regular file required")
    return path


def powershell_executable():
    """Use the Windows system directory, never PATH or a caller-supplied exe."""
    if os.name != "nt":
        raise OSError("Windows required")
    api = ctypes.WinDLL("kernel32", use_last_error=True).GetSystemDirectoryW
    api.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    api.restype = ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    length = api(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise OSError("System directory unavailable")
    return local_file(str(Path(buffer.value) / "WindowsPowerShell/v1.0/powershell.exe"), ".exe")


def run_script(raw):
    script = local_file(raw, ".ps1")
    executable = powershell_executable()
    result = subprocess.run(
        [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script)],
        cwd=str(script.parent), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return result.returncode


def main(argv=None):
    try:
        parser = PrivateArgumentParser(description=__doc__)
        parser.add_argument("--script", required=True)
        options = parser.parse_args(argv)
        return run_script(options.script)
    except Exception as error:
        # pythonw has no stderr. A console caller gets only the exception class,
        # without local paths, script output or configuration excerpts.
        if sys.stderr is not None:
            sys.stderr.write("hidden_task: " + type(error).__name__[:64] + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
