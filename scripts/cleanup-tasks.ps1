# Repo root: <root>/napcat/xxx.ps1 -> <root>
$root = Split-Path -Parent $PSScriptRoot
# Remove the scheduled tasks that the plugin now does itself. Run as administrator.
#
# Right click -> Run with PowerShell, or from an elevated prompt:
#   powershell -NoProfile -ExecutionPolicy Bypass -File "$root\napcat\cleanup-tasks.ps1"
#
# BEFORE running this: restart DSH once and confirm dsh-lanya is alive, i.e.
#   Test-Path "$root\napcat\scheduler-in-plugin.flag"
# should be True. That flag means the plugin's own scheduler is running and is
# already doing these jobs. Removing the tasks before that would stop the work.
#
# ASCII-only source.

$ErrorActionPreference = 'Continue'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host ''
  Write-Host '  NOT ELEVATED. Right click this file -> Run with PowerShell.' -ForegroundColor Red
  Write-Host ''
  Start-Sleep -Seconds 3
  exit 1
}

$marker = Join-Path '$root\napcat' 'scheduler-in-plugin.flag'

Write-Host ''
if (-not (Test-Path $marker)) {
  Write-Host '  The plugin scheduler is NOT running yet (no marker file).' -ForegroundColor Yellow
  Write-Host '  Restart DSH first, confirm the marker exists, then run this again.' -ForegroundColor Yellow
  Write-Host ''
  # No YES prompt: when this is double-clicked, an extra confirmation step only makes it easier to miss. The action is reversible (re-register the tasks).
}
else {
  $age = ((Get-Date) - (Get-Item $marker).LastWriteTime).TotalMinutes
  Write-Host ('  plugin scheduler alive, marker age ' + [int]$age + ' min') -ForegroundColor Green
}

# These are the ones the plugin's own scheduler covers now. Servers and the three
# long-lived daemons are deliberately NOT in this list:
#   DSH-Lanya-Works / -TTS / -Browser  are servers, not timers
#   DSH-GroupWatch / -Life / -Voice    are long-lived daemons
$redundant = @(
  'DSH-Reminders',
  'DSH-Lanya-Budget',
  'DSH-Lanya-Chats',
  'DSH-Lanya-Steal',
  'DSH-Lanya-Greet',
  'NapCat-DSH-Bridge'
)

Write-Host ''
Write-Host 'removing:' -ForegroundColor Cyan
foreach ($n in $redundant) {
  & schtasks.exe /delete /tn ('\' + $n) /f 2>&1 | Out-Null
  if ($LASTEXITCODE -eq 0) { Write-Host ('  removed  ' + $n) -ForegroundColor Green }
  else { Write-Host ('  absent   ' + $n) -ForegroundColor DarkGray }
}

Write-Host ''
Write-Host 'still scheduled (these are meant to stay):' -ForegroundColor Cyan
$csv = & schtasks.exe /query /fo CSV 2>&1 | ConvertFrom-Csv -ErrorAction SilentlyContinue
$csv | Where-Object { $_.TaskName -match 'DSH|NapCat' } | Select-Object -ExpandProperty TaskName -Unique | ForEach-Object {
  Write-Host ('  ' + $_) -ForegroundColor Gray
}

Write-Host ''
Write-Host 'after this, nothing should spawn once a minute. Watch Task Manager for a few minutes.' -ForegroundColor Cyan
Write-Host ''
Start-Sleep -Seconds 3
