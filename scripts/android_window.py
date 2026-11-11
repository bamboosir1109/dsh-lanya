"""Keep one named Windows emulator window at its desktop position."""
import argparse
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import time


class EmulatorWindow:
    def __init__(self, executable, title):
        self.executable = executable
        self.title = title
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user32.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
        self.user32.SetProcessDpiAwarenessContext(wintypes.HANDLE(-4))
        self.user32.EnumWindows.argtypes = [self.callback_type, wintypes.LPARAM]
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        for name in ("IsWindowVisible", "IsIconic", "IsZoomed"):
            getattr(self.user32, name).argtypes = [wintypes.HWND]
        self.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT]
        self.user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                             ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = ctypes.c_short
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                            ctypes.POINTER(wintypes.DWORD)]
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def matches(self, hwnd):
        title = ctypes.create_unicode_buffer(len(self.title) + 2)
        self.user32.GetWindowTextW(hwnd, title, len(title))
        if title.value != self.title or not self.user32.IsWindowVisible(hwnd):
            return False
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = self.kernel32.OpenProcess(0x1000, False, pid.value)
        if not process:
            return False
        try:
            path = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(path))
            return bool(self.kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size))
                        and os.path.samefile(path.value, self.executable))
        except OSError:
            return False
        finally:
            self.kernel32.CloseHandle(process)

    def find(self):
        found = []

        @self.callback_type
        def visit(hwnd, _):
            if self.matches(hwnd):
                found.append(hwnd)
            return True

        self.user32.EnumWindows(visit, 0)
        return found[0] if len(found) == 1 else None

    def placement(self, hwnd, position):
        if self.user32.IsIconic(hwnd) or self.user32.IsZoomed(hwnd):
            return None
        window = wintypes.RECT()
        desktop = wintypes.RECT()
        if not (self.user32.GetWindowRect(hwnd, ctypes.byref(window))
                and self.user32.SystemParametersInfoW(48, 0, ctypes.byref(desktop), 0)):
            return None
        width, height = window.right - window.left, window.bottom - window.top
        right = max(desktop.left, desktop.right - width)
        bottom = max(desktop.top, desktop.bottom - height)
        x, y = position or (right - 96, desktop.top + 24)
        return (window.left, window.top), (max(desktop.left, min(x, right)), max(desktop.top, min(y, bottom)))

    def move(self, hwnd, position):
        if not self.matches(hwnd):
            return False
        # Preserve size, stacking order, focus and the emulator's owned toolbar.
        return bool(self.user32.SetWindowPos(hwnd, None, *position, 0, 0, 0x0215))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--position", nargs=2, type=int, metavar=("X", "Y"))
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Android window placement requires Windows")
    emulator = EmulatorWindow(args.exe.resolve(strict=True), args.title)
    last = None
    changed_at = time.monotonic()
    while True:
        hwnd = emulator.find()
        placement = emulator.placement(hwnd, args.position) if hwnd else None
        if not args.watch:
            if placement is None:
                return 1
            return 0 if placement[0] == placement[1] or emulator.move(hwnd, placement[1]) else 1
        now = time.monotonic()
        state = (hwnd, placement)
        if state != last or emulator.user32.GetAsyncKeyState(1) < 0:
            last, changed_at = state, now
        elif placement and placement[0] != placement[1] and now - changed_at >= 3:
            emulator.move(hwnd, placement[1])
        time.sleep(0.75)


if __name__ == "__main__":
    raise SystemExit(main())
