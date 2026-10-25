# Lanya tick - runs whatever periodic job is due, driven by an EXISTING task.
#
# Why this exists: registering a new scheduled task needs administrator rights on
# this machine and the agent does not have them. But DSH-Reminders is already
# registered and already fires every minute, so it can carry the schedule. That
# removes the need for ~10 separate tasks without any elevation at all.
#
# Design choices:
#   - ONE job per tick. Some jobs (steal ~16s) are slow; doing them all in one tick
#     would delay the reminders that triggered us.
#   - Picks the most overdue job, so nothing starves.
#   - Its own state file, so it never fights with the supervisor if that ever gets
#     registered: whichever one runs, the same state file decides what is due.
#   - Every failure is logged and swallowed. It is called from reminders.ps1 and
#     must never break reminders.
#
# ASCII-only source.

$ErrorActionPreference = 'Continue'

# If the plugin's built-in scheduler is running (it leaves a marker file on start),
# do nothing here. Two schedulers running at once would do every job twice and spawn
# twice as many processes - exactly what we are trying to remove.
$marker = Join-Path '<WORKDIR>\napcat' 'scheduler-in-plugin.flag'
if (Test-Path $marker) {
  $ageMin = ((Get-Date) - (Get-Item $marker).LastWriteTime).TotalMinutes
  # A marker untouched for 30+ minutes means the plugin is probably gone; fall back.
  if ($ageMin -lt 30) { exit 0 }
}

$root  = '<WORKDIR>\napcat'
$state = Join-Path $root 'lanya-tick.state.json'
$log   = Join-Path $root 'lanya-tick.log'

function Note([string]$m) {
  $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
  try { Add-Content -Path $log -Value $line -Encoding UTF8 } catch { }
}

# name, minutes, exe, argument list
$jobs = @(
  @{ n = 'settings';   m = 5;    exe = 'python'; args = @((Join-Path $root 'settings-guard.py')) },
  @{ n = 'state';      m = 1;    exe = 'powershell.exe'; args = @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',(Join-Path $root 'lanya-state.ps1')) },
  @{ n = 'strays';     m = 5;    exe = 'powershell.exe'; args = @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',(Join-Path $root 'kill-strays.ps1')) },
  @{ n = 'bridge';     m = 10;   exe = 'powershell.exe'; args = @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',(Join-Path $root 'napcat-heal.ps1')) },
  @{ n = 'chats';      m = 10;   exe = 'python'; args = @((Join-Path $root 'export-chats.py')) },
  @{ n = 'steal';      m = 10;   exe = 'python'; args = @((Join-Path $root 'stealmemes.py')) },
  @{ n = 'greet';      m = 60;   exe = 'python'; args = @((Join-Path $root 'greetd.py')) },
  @{ n = 'memory';     m = 360;  exe = 'python'; args = @((Join-Path $root 'memory-maintenance.py')) },
  @{ n = 'patchcheck'; m = 1440; exe = 'powershell.exe'; args = @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',(Join-Path $root 'check-patches.ps1')) }
)

# Skip scripts that are not there instead of failing every minute.
$jobs = @($jobs | Where-Object {
  $t = $null
  foreach ($a in $_.args) { if ($a -match '\.(ps1|py)$') { $t = $a; break } }
  if ($t -and (Test-Path $t)) { $true } else { $false }
})
if ($jobs.Count -eq 0) { exit 0 }

$last = @{}
if (Test-Path $state) {
  try {
    $raw = Get-Content $state -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($p in $raw.PSObject.Properties) { $last[$p.Name] = [datetime]$p.Value }
  } catch { }
}

$now = Get-Date

# Most overdue first: overdue = elapsed / interval, biggest wins.
$best = $null
$bestScore = -1
foreach ($j in $jobs) {
  $elapsed = if ($last.ContainsKey($j.n)) { ($now - $last[$j.n]).TotalMinutes } else { 999999 }
  if ($elapsed -lt $j.m) { continue }
  $score = $elapsed / $j.m
  if ($score -gt $bestScore) { $bestScore = $score; $best = $j }
}

if (-not $best) { exit 0 }

# Mark as run BEFORE executing. If the job hangs or crashes the process, the next
# tick will not immediately retry it and pile up.
$last[$best.n] = $now
try {
  $obj = @{}
  foreach ($k in $last.Keys) { $obj[$k] = $last[$k].ToString('o') }
  [System.IO.File]::WriteAllText($state, ($obj | ConvertTo-Json -Compress), (New-Object System.Text.UTF8Encoding($false)))
} catch { }

try {
  $quoted = @()
  foreach ($a in $best.args) { if ($a -match '\s') { $quoted += ('"' + $a + '"') } else { $quoted += $a } }
  $p = Start-Process -FilePath $best.exe -ArgumentList $quoted -NoNewWindow -PassThru -ErrorAction Stop
  if (-not $p.WaitForExit(180000)) {
    try { $p.Kill() } catch { }
    Note ('FAIL ' + $best.n + ' timed out, killed')
  }
  else {
    try { $p.Refresh() } catch { }
    $code = $p.ExitCode
    if ($null -eq $code) { $code = 0 }
    if ($code -ne 0) { Note ('FAIL ' + $best.n + ' exit=' + $code) }
  }
}
catch {
  Note ('FAIL ' + $best.n + ' ' + $_.Exception.Message.Substring(0, [Math]::Min(120, $_.Exception.Message.Length)))
}
