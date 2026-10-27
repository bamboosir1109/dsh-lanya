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

# The supervisor drives every other periodic job (budget, chats, steal,
# napcat-heal, greetd, memory, bili, bili-social). Without it registered,
# none of them run at all - which is why Bilibili sat untouched for a day.
if (-not (Get-ScheduledTask -TaskName 'DSH-Lanya-Supervisor' -ErrorAction SilentlyContinue)) {
  $act = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' +
               (Join-Path $napcat 'lanya-supervisor.ps1') + '"')
  $trg = New-ScheduledTaskTrigger -AtLogOn
  $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName 'DSH-Lanya-Supervisor' -Action $act -Trigger $trg `
    -Settings $set -Force | Out-Null
  Note 'DSH-Lanya-Supervisor  <-  lanya-supervisor.ps1'
}
else { Note 'DSH-Lanya-Supervisor  already exists, left alone' }


# This is the one that was missing. Without it budget.json never updates, so the
# allowance silently does nothing and every figure on the panel is stale.
RepeatTask 'DSH-Lanya-Budget' 'budget.ps1' 5

Write-Host ''
Write-Host '  === result ==='
Get-ScheduledTask -TaskName 'DSH-*' -ErrorAction SilentlyContinue |
  Sort-Object TaskName |
  ForEach-Object { Note ($_.TaskName.PadRight(24) + $_.State) }

# --- pieces that had NO task at all ---------------------------------------
#
# These three are why a reboot left the whole thing dark: the panel was 404,
# nothing periodic ran (no supervisor), and the Bilibili job silently stopped.
# Registering them here means one elevated run fixes all of it for good.
#
# Note: Register-ScheduledTask needs elevation. Running this script as a normal
# user fails with "Access is denied" for every entry - that is expected, not a
# bug in the script.

if (-not (Get-ScheduledTask -TaskName 'DSH-Lanya-Panel' -ErrorAction SilentlyContinue)) {
  $a1 = New-ScheduledTaskAction -Execute 'C:\Python314\python.exe' `
    -Argument ('"' + (Join-Path $napcat 'lanya-panel-server.py') + '"')
  $t1 = New-ScheduledTaskTrigger -AtLogOn
  $s1 = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
    -RestartInterval (New-TimeSpan -Minutes 2) -RestartCount 999
  Register-ScheduledTask -TaskName 'DSH-Lanya-Panel' -Action $a1 -Trigger $t1 `
    -Settings $s1 -Force | Out-Null
  Note 'DSH-Lanya-Panel  <-  19389'
}

if (-not (Get-ScheduledTask -TaskName 'DSH-Lanya-PanelStatic' -ErrorAction SilentlyContinue)) {
  $a2 = New-ScheduledTaskAction -Execute 'C:\Python314\python.exe' `
    -Argument ('"' + (Join-Path $napcat 'lanya-panel-server.py') + '" --port 19388')
  $t2 = New-ScheduledTaskTrigger -AtLogOn
  $s2 = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
    -RestartInterval (New-TimeSpan -Minutes 2) -RestartCount 999
  Register-ScheduledTask -TaskName 'DSH-Lanya-PanelStatic' -Action $a2 -Trigger $t2 `
    -Settings $s2 -Force | Out-Null
  Note 'DSH-Lanya-PanelStatic  <-  19388'
}


# ---------------------------------------------------------------------------
# The planning loop. lifeloop used to wake her on a random timer; mind.py
# replaced it with a plan that survives between rounds, and lifeloop now
# retires itself. That makes this task the only thing driving her autonomous
# activity - without it she just sits there between messages.
#
# It has to be a task, not a spawned process: anything started with
# Start-Process gets reaped, and mind.py dies within seconds of being launched
# that way.
if (-not (Get-ScheduledTask -TaskName 'DSH-Lanya-Mind' -ErrorAction SilentlyContinue)) {
  $am = New-ScheduledTaskAction -Execute 'C:\Python314\python.exe' `
    -Argument ('"' + (Join-Path $napcat 'mind.py') + '"')
  $tm1 = New-ScheduledTaskTrigger -AtLogOn
  $tm2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
  $sm = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
  Register-ScheduledTask -TaskName 'DSH-Lanya-Mind' -Action $am `
    -Trigger @($tm1, $tm2) -Settings $sm -Force | Out-Null
  Note 'DSH-Lanya-Mind  <-  mind.py'
}

Write-Host ''
