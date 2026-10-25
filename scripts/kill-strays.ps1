# Kill stray headless agent processes before they pile up.
#
# The runaway this prevents: groupwatch and lifeloop each wake up and spawn a
# headless agent (`dsh --profile headless -`). Each one is 200-400 MB. Normally it
# answers and exits. But when one hangs - model call stalls, a tool waits forever -
# nothing reaps it, and the next tick spawns another. Fourteen of them arrived that
# way and the machine ended up holding 20+ GB.
#
# This looks for headless agents that have clearly outlived any sane single task
# and kills them. It only ever touches `dsh --profile headless`; the GUI, the bridge
# and the daemons are never candidates.
#
#   powershell ... -File kill-strays.ps1            report + kill
#   powershell ... -File kill-strays.ps1 -DryRun    report only
#
# ASCII-only source.

param(
  [int]$MaxAgeMinutes = 6,
  [switch]$DryRun
)


# Repo root: <root>/napcat/xxx.ps1 -> <root>
$root = Split-Path -Parent $PSScriptRoot
$ErrorActionPreference = 'Continue'

# ---- shared project config ------------------------------------------------
# Reads <repo>\config\project.json. The helper carries the same defaults, so a
# missing or broken config file leaves behaviour exactly as it was.
$confHelper = Join-Path $PSScriptRoot 'project_conf.ps1'
if (Test-Path $confHelper) { . $confHelper }
if (-not (Get-Command Get-ProjectConfValue -ErrorAction SilentlyContinue)) {
  function Get-ProjectConfValue { param([string]$KeyPath, $Default = $null) return $Default }
}

$root = $PSScriptRoot
$log  = Join-Path $root 'strays.log'

function Note([string]$m) {
  $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
  try { Add-Content -Path $log -Value $line -Encoding UTF8 } catch { }
  Write-Host $line
}

$now = Get-Date

# Hard concurrency cap. Age alone is too slow a defence: a burst of scheduled work
# can start six agents inside a minute, which measured 991 MB in one sample. Whatever
# their age, only a couple should ever be in flight - everything else is queued work
# that can just be dropped and retried on the next tick.
# 2 -> 3: the meme classifier holds one agent per batch (30-60s each).
# At 2 it kept getting trimmed alongside regular scheduled work and those
  # batches were wasted. 3 is still far below anything that strains memory.
  $MaxConcurrent = [int](Get-ProjectConfValue 'daemon.maxConcurrentAgents' 3)
$all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue

$strays = @()
foreach ($p in $all) {
  $cl = [string]$p.CommandLine
  if ($cl -eq '') { continue }
  # Only headless profile runs. Check both spellings people end up with.
  if ($cl -notmatch '--profile\s+headless') { continue }
  # Skip the current shell chain so we never kill ourselves.
  if ($p.ProcessId -eq $PID) { continue }
  $age = ($now - $p.CreationDate).TotalMinutes
  if ($age -ge $MaxAgeMinutes) {
    $strays += [pscustomobject]@{ PID = $p.ProcessId; Age = [math]::Round($age,1); MB = [math]::Round($p.WorkingSetSize/1MB) }
  }
}

# Concurrency check runs FIRST: even young agents count against the cap.
$allHeadless = @($all | Where-Object {
  $c = [string]$_.CommandLine
  $c -ne '' -and $c -match '--profile\s+headless' -and $_.ProcessId -ne $PID
})

$overCap = @()
if ($allHeadless.Count -gt $MaxConcurrent) {
  $sorted = $allHeadless | Sort-Object CreationDate
  $excess = $allHeadless.Count - $MaxConcurrent
  $overCap = @($sorted | Select-Object -First $excess)
  Note ('concurrency cap: ' + $allHeadless.Count + ' headless agents running, trimming ' + $excess + ' oldest')
  foreach ($p in $overCap) {
    $mb = [math]::Round($p.WorkingSetSize/1MB)
    if ($DryRun) { Note ('  would trim PID ' + $p.ProcessId + ' ' + $mb + 'MB'); continue }
    try {
      & taskkill.exe /PID $p.ProcessId /F 2>&1 | Out-Null
      Note ('  trimmed PID ' + $p.ProcessId + ' ' + $mb + 'MB')
    }
    catch { }
  }
  if ($DryRun) { }
  else { $strays = @($strays | Where-Object { $p = $_; -not ($overCap | Where-Object { $_.ProcessId -eq $p.PID }) }) }
}

if ($strays.Count -eq 0) {
  # Stay silent on a clean pass so the log stays readable.
  exit 0
}

Note ('found ' + $strays.Count + ' stray headless agent(s) older than ' + $MaxAgeMinutes + ' min')

foreach ($s in $strays) {
  if ($DryRun) {
    Note ('  would kill PID ' + $s.PID + ' age=' + $s.Age + 'min ' + $s.MB + 'MB')
    continue
  }
  try {
    & taskkill.exe /PID $s.PID /F 2>&1 | Out-Null
    Note ('  killed PID ' + $s.PID + ' age=' + $s.Age + 'min ' + $s.MB + 'MB')
  }
  catch {
    Note ('  failed to kill PID ' + $s.PID + ': ' + $_.Exception.Message.Substring(0, [Math]::Min(80, $_.Exception.Message.Length)))
  }
}
