# Lanya supervisor - one long-lived process that drives every periodic job.
#
# Why: nine scheduled tasks fired at 1/5/10 minute intervals. Task Scheduler
# launching a task costs far more than the script itself (taskeng + a fresh
# powershell + a fresh python each time), and the result looked like constant
# process churn. This process holds its own timers instead: one process, one log,
# one place to see what ran and what failed.
#
# What it does NOT absorb - deliberately:
#   groupwatch / lifeloop / voicedaemon  already long-lived; folding them in would
#                                        mean one crash taking everything down
#   made-server / TTS server / browserd  servers, not timers
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File lanya-supervisor.ps1
#   ... -Once          run every job once and exit (for testing)
#   ... -List          print the schedule and exit
#
# ASCII-only source.

param(
  [switch]$Once,
  [switch]$List
)

$ErrorActionPreference = 'Continue'

$root = $PSScriptRoot
if ($env:LANYA_WORKDIR) { $root = Join-Path $env:LANYA_WORKDIR 'napcat' }
$logPath = Join-Path $root 'lanya-supervisor.log'
$py = 'python'
$ps = 'powershell.exe'

function Write-Sup([string]$msg) {
  $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
  try { Add-Content -Path $logPath -Value $line -Encoding UTF8 } catch { }
  Write-Host $line
}

# name, minutes between runs, exe, argument string
$jobs = @(
  @{ n = 'reminders';  m = 1;      exe = $ps; args = @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File', (Join-Path $root 'reminders.ps1'), '-Once') },
  @{ n = 'budget';     m = 5;      exe = $ps; args = @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File', (Join-Path $root 'budget.ps1')) },
  @{ n = 'settings';   m = 5;      exe = $py; args = @((Join-Path $root 'settings-guard.py')) },
  @{ n = 'chats';      m = 10;     exe = $py; args = @((Join-Path $root 'export-chats.py')) },
  @{ n = 'steal';      m = 10;     exe = $py; args = @((Join-Path $root 'stealmemes.py')) },
  @{ n = 'bridge';     m = 10;     exe = $ps; args = @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File', (Join-Path $root 'napcat-heal.ps1')) },
  @{ n = 'strays';     m = 5;      exe = $ps; args = @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File', (Join-Path $root 'kill-strays.ps1')) },
  @{ n = 'greet';      m = 60;     exe = $py; args = @((Join-Path $root 'greetd.py')) },
  @{ n = 'memory';     m = 360;    exe = $py; args = @((Join-Path $root 'memory-maintenance.py')) },
  # Bilibili had NO schedule at all - the video run only happened when
  # somebody pressed the button on the panel, which is why some days she
  # simply did not watch anything. bili-social answers people who replied
  # to her comments or @-ed her there.
  # Her planning loop. This is what replaced lifeloop's random wake-up timer:
  # she now keeps a plan across rounds and revises it when somebody talks to
  # her. Runs every few minutes; the actual spacing comes from her own ETA.
  @{ n = 'mind';        m = 1;     exe = $py; args = @((Join-Path $root 'mind.py')) },
  @{ n = 'bili';        m = 180;   exe = $py; args = @((Join-Path $root 'bili_onerun.py'), '--mention') },
  @{ n = 'bili-social'; m = 90;    exe = $py; args = @((Join-Path $root 'bili_social.py'), '--max=3') },
  @{ n = 'patchcheck'; m = 1440;   exe = $ps; args = @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File', (Join-Path $root 'check-patches.ps1')) }
)

# Skip a job silently if its script is missing - do not spam the log every tick.
$jobs = @($jobs | Where-Object {
  # For powershell jobs the script path is not the last arg (-Once follows it),
  # so look for the first argument that ends in .ps1 / .py instead.
  $target = $null
  foreach ($a in $_.args) {
    if ($a -match '\.(ps1|py)$') { $target = $a; break }
  }
  if (-not $target) { $target = $_.args[0] }
  if (-not (Test-Path $target)) { Write-Sup ('skip ' + $_.n + ' (missing ' + (Split-Path $target -Leaf) + ')'); $false } else { $true }
})

if ($List) {
  foreach ($j in $jobs) { Write-Host ('  {0,-12} every {1} min  ->  {2}' -f $j.n, $j.m, (Split-Path $j.args[$j.args.Count-1] -Leaf)) }
  exit 0
}

if ($jobs.Count -eq 0) { Write-Sup 'no jobs available'; exit 1 }

# A second supervisor must not start a second set of timers.
try {
  $supervisorLock = [IO.File]::Open((Join-Path $root 'lanya-supervisor.lock'),
    [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
  if ($supervisorLock.SafeFileHandle.IsInvalid) { throw 'Cannot hold supervisor lock' }
} catch {
  Write-Output 'supervisor already running'
  exit 0
}

function Test-PluginOwnsJob([string]$jobName) {
  $marker = Join-Path $root 'scheduler-in-plugin.flag'
  try {
    if (-not (Test-Path -LiteralPath $marker)) { return $false }
    if (((Get-Date) - (Get-Item -LiteralPath $marker).LastWriteTime).TotalSeconds -gt 120) { return $false }
    $lease = Get-Content -LiteralPath $marker -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($lease.version -ne 2 -or -not $lease.pid -or -not ($lease.jobs -contains $jobName)) { return $false }
    return ($null -ne (Get-Process -Id $lease.pid -ErrorAction SilentlyContinue))
  } catch { return $false }
}

Write-Sup ('supervisor start: ' + $jobs.Count + ' jobs')

$last = @{}
foreach ($j in $jobs) { $last[$j.n] = [datetime]::MinValue }

function Invoke-Job($j) {
  if (Test-PluginOwnsJob $j.n) { return }
  if ($j.n -eq 'budget') {
    $standalone = Get-ScheduledTask -TaskName 'DSH-Lanya-Budget' -ErrorAction SilentlyContinue
    if ($standalone -and $standalone.State -ne 'Disabled') {
      $budgetScript = Join-Path $root 'budget.ps1'
      if (@($standalone.Actions | Where-Object { $_.Arguments -like ('*' + $budgetScript + '*') }).Count -gt 0) { return }
    }
  }
  $sw = [Diagnostics.Stopwatch]::StartNew()
  try {
    # Quote every argument that contains a space. Passing the array straight to
    # -ArgumentList let Windows split "<WORKDIR>\napcat\reminders.ps1" at the
    # space, so powershell got the flag split at the space and every job failed instantly.
    $quoted = @()
    foreach ($a in $j.args) {
      if ($a -match '\s') { $quoted += ('"' + $a + '"') } else { $quoted += $a }
    }
    $p = Start-Process -FilePath $j.exe -ArgumentList $quoted -NoNewWindow -PassThru -ErrorAction Stop
    # Do not wait forever: a stuck child must not freeze every other timer.
    $limitMs = 240000
    if (-not $p.WaitForExit($limitMs)) {
      try { $p.Kill() } catch { }
      Write-Sup ('FAIL ' + $j.n + ' timed out after ' + [int]($limitMs/1000) + 's, killed')
      return
    }
    $sw.Stop()
    # Start-Process -PassThru leaves ExitCode empty until the object is refreshed;
    # reading it directly made every successful job look like a failure.
    try { $p.Refresh() } catch { }
    $code = $p.ExitCode
    if ($null -eq $code) { $code = 0 }
    if ($code -ne 0) {
      Write-Sup ('FAIL ' + $j.n + ' exit=' + $code + ' (' + [int]$sw.Elapsed.TotalSeconds + 's)')
    } elseif ($sw.Elapsed.TotalSeconds -ge 20) {
      Write-Sup ('ok   ' + $j.n + ' (' + [int]$sw.Elapsed.TotalSeconds + 's)')
    }
  }
  catch {
    Write-Sup ('FAIL ' + $j.n + ' ' + $_.Exception.Message.Substring(0, [Math]::Min(120, $_.Exception.Message.Length)))
  }
}

if ($Once) {
  foreach ($j in $jobs) { Invoke-Job $j }
  Write-Sup 'single pass done'
  exit 0
}

while ($true) {
  try {
    $now = Get-Date
    foreach ($j in $jobs) {
      if (($now - $last[$j.n]).TotalMinutes -ge $j.m) {
        $last[$j.n] = $now
        Invoke-Job $j
      }
    }
  }
  catch {
    Write-Sup ('loop error (kept alive): ' + $_.Exception.Message.Substring(0, [Math]::Min(140, $_.Exception.Message.Length)))
  }
  Start-Sleep -Seconds 20
}
