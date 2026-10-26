# Ensure every scheduled task this project needs actually exists.
#
# Why this file exists: the tasks were created by hand at some point and one of
# them - DSH-Lanya-Budget - was simply never made. Nothing noticed, because the
# failure mode is silence: budget.json keeps its yesterday numbers, the panel
# keeps showing them, and the daily allowance never fires. The panel even listed
# (note removed: source must stay ASCII-only)
#
# Safe to run repeatedly: every task is registered with -Force and a fixed
# definition, so this doubles as a repair tool.
#
# ASCII-only source.

$ErrorActionPreference = 'Stop'
# Find the work directory from this script's own location: it lives in
# <root>\napcat\, so the parent of the parent is <root>.
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path (Join-Path $root 'napcat'))) {
  # Fallback for a copy sitting directly under <root>.
  $root = Split-Path -Parent $PSScriptRoot
}
$napcat = Join-Path $root 'napcat'

function Note([string]$m) { Write-Host ('  ' + $m) }

function PS1Task([string]$name, [string]$script, [string]$extra = '') {
  $arg = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' +
         (Join-Path $napcat $script) + '"' + $extra
  $act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
  $trg = New-ScheduledTaskTrigger -AtLogOn
  $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
  if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Note ($name + '  already exists, left alone')
  }
  else {
    Register-ScheduledTask -TaskName $name -Action $act -Trigger $trg `
      -Settings $set -Force | Out-Null
    Note ($name + '  <-  ' + $script)
  }
}

function RepeatTask([string]$name, [string]$script, [int]$minutes) {
  $act = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -File "' +
               (Join-Path $napcat $script) + '"')
  $trg = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes $minutes)
  $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes ([math]::Max(3, $minutes - 2)))
  if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Note ($name + '  already exists, left alone')
  }
  else {
    Register-ScheduledTask -TaskName $name -Action $act -Trigger $trg `
      -Settings $set -Force | Out-Null
    Note ($name + '  <-  ' + $script + ' every ' + $minutes + 'min')
  }
}

Write-Host ''
Write-Host '  === long-running (start at logon) ==='

# The things that must be alive whenever the machine is on.
PS1Task 'DSH-GroupWatch'   'groupwatch.ps1'
PS1Task 'DSH-Lanya-Life'   'lifeloop.ps1'
PS1Task 'DSH-Lanya-Voice'  'voicedaemon.ps1'
PS1Task 'DSH-Lanya-Works'  'made-server.ps1'

# TTS lives outside napcat\ and has its own conda environment.
$tts = Join-Path $root 'tts\start-tts-server.ps1'
if (Test-Path $tts) {
  $act = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $tts + '"')
  $trg = New-ScheduledTaskTrigger -AtLogOn
  $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
  if (-not (Get-ScheduledTask -TaskName 'DSH-Lanya-TTS' -ErrorAction SilentlyContinue)) {
    Register-ScheduledTask -TaskName 'DSH-Lanya-TTS' -Action $act -Trigger $trg `
      -Settings $set -Force | Out-Null
    Note 'DSH-Lanya-TTS  <-  tts\start-tts-server.ps1'
  }
  else { Note 'DSH-Lanya-TTS  already exists, left alone' }
}

# The browser daemon is plain Python, no PowerShell wrapper.
$bd = Join-Path $napcat 'browserd.py'
if (Test-Path $bd) {
  $act = New-ScheduledTaskAction -Execute 'python' -Argument ('"' + $bd + '"')
  $trg = New-ScheduledTaskTrigger -AtLogOn
  $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
  if (-not (Get-ScheduledTask -TaskName 'DSH-Lanya-Browser' -ErrorAction SilentlyContinue)) {
    Register-ScheduledTask -TaskName 'DSH-Lanya-Browser' -Action $act -Trigger $trg `
      -Settings $set -Force | Out-Null
    Note 'DSH-Lanya-Browser  <-  browserd.py'
  }
  else { Note 'DSH-Lanya-Browser  already exists, left alone' }
}

Write-Host ''
Write-Host '  === periodic ==='

# This is the one that was missing. Without it budget.json never updates, so the
# allowance silently does nothing and every figure on the panel is stale.
RepeatTask 'DSH-Lanya-Budget' 'budget.ps1' 5

Write-Host ''
Write-Host '  === result ==='
Get-ScheduledTask -TaskName 'DSH-*' -ErrorAction SilentlyContinue |
  Sort-Object TaskName |
  ForEach-Object { Note ($_.TaskName.PadRight(24) + $_.State) }
Write-Host ''
