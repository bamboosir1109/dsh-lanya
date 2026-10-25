# Auto-update: pull the latest code from GitHub on a schedule.
#
# Only code. Config and her data are never touched. Uses --ff-only so a dirty
# working tree stops the pull instead of getting mangled.
#
# Driven by the DSH plugin scheduler (see AUTOUPDATE in plugin/lib/index.js).
# Can also be run by hand:
#   powershell -File auto-update.ps1            pull once
#   powershell -File auto-update.ps1 -DryRun    just report whether there is news
#
# ASCII-only source (Windows PowerShell 5.1 reads BOM-less .ps1 as ANSI).
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $PSScriptRoot 'auto-update.log'

function Note([string]$m) {
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    try { Add-Content -Path $log -Value $line -Encoding UTF8 } catch { }
    Write-Host $line
}

# The repo sits next to napcat/, not inside it.
$repo = Join-Path $root 'dsh-lanya'
if (-not (Test-Path (Join-Path $repo '.git'))) {
    Note "skip: $repo is not a git repo"
    exit 0
}

Push-Location $repo
$before = (git rev-parse HEAD 2>$null)
$null = (git fetch --quiet origin 2>$null)
$after = (git rev-parse origin/main 2>$null)
Pop-Location

if (-not $before -or -not $after) {
    Note 'skip: cannot resolve HEAD or origin/main'
    exit 0
}

if ($before -eq $after) {
    if ($DryRun) { Note 'up to date' }
    exit 0
}

if ($DryRun) {
    Note ("has updates: " + $before.Substring(0, 7) + " -> " + $after.Substring(0, 7))
    exit 0
}

Push-Location $repo
$out = git pull --ff-only 2>&1
$code = $LASTEXITCODE
Pop-Location

if ($code -ne 0) {
    $msg = ($out -join ' ')
    if ($msg.Length -gt 300) { $msg = $msg.Substring(0, 300) }
    Note ("pull refused (local changes?): " + $msg)
    exit 0
}

$newShort = (git -C $repo rev-parse --short HEAD)
Note ("updated " + $before.Substring(0, 7) + " -> " + $newShort)

# Refresh the snapshots the panel reads, so the page shows the new version.
$snap = Join-Path $PSScriptRoot 'make_snapshots.py'
if (Test-Path $snap) {
    try { & python $snap | Out-Null } catch { }
}
