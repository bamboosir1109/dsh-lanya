@echo off
REM One-click cleanup for the redundant scheduled tasks (self-elevating).
REM
REM Double-click this file. It asks for admin via UAC, then runs cleanup-tasks.ps1.
REM
REM Two things here are deliberate:
REM   1. ASCII-only name and contents. The previous launcher had a Chinese
REM      filename, which gets mangled when cmd re-launches itself through UAC.
REM   2. No YES prompt in the target script, so there is no step to miss.

setlocal
set "SCRIPT=%~dp0cleanup-tasks.ps1"

net session >nul 2>&1
if %errorlevel% equ 0 goto :run

echo.
echo   Administrator rights required.
echo   A UAC prompt will appear - click YES.
echo.

REM Re-launch as administrator. -FilePath cmd with the /c argument keeps the
REM quoting intact even though the path contains spaces.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','\"%~f0\"' -Verb RunAs"
exit /b

:run
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%"
echo.
echo   Done. Press any key to close.
pause >nul
