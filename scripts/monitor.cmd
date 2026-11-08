@echo off
setlocal
rem One persistent PowerShell in this console; no service control or autorun.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0monitor.ps1" %*
exit /b %errorlevel%
