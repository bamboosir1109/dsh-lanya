# Auto-update: pull the latest code from GitHub on a schedule.
#
# Only the source checkout, using its current branch's configured upstream.
# Refuse local changes explicitly; --ff-only alone does not protect dirty trees.
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

function Read-Git([string[]]$Arguments) {
    $lines = @(& git -C $repo @Arguments 2>$null)
    $result = $LASTEXITCODE
    return @{ Ok = ($result -eq 0); Text = [string]::Join([Environment]::NewLine, $lines).Trim() }
}

$head = Read-Git @('rev-parse', '--verify', 'HEAD')
$branch = Read-Git @('symbolic-ref', '--quiet', '--short', 'HEAD')
$tracking = Read-Git @('rev-parse', '--symbolic-full-name', '@{upstream}')
$dirty = Read-Git @('status', '--porcelain=v1', '--untracked-files=normal')
if (-not $head.Ok -or $head.Text -notmatch '^[0-9a-f]{40,64}$' -or
    -not $branch.Ok -or -not $tracking.Ok -or -not $tracking.Text -or -not $dirty.Ok) {
    Note 'skip: cannot verify a branch, upstream and working tree'
    exit 0
}
if ($dirty.Text) {
    Note 'skip: working tree has local changes'
    exit 0
}
$remote = Read-Git @('config', '--get', ('branch.' + $branch.Text + '.remote'))
if (-not $remote.Ok -or -not $remote.Text -or $remote.Text -eq '.') {
    Note 'skip: no remote upstream configured'
    exit 0
}
$null = & git -C $repo fetch --quiet -- $remote.Text 2>$null
if ($LASTEXITCODE -ne 0) {
    Note 'skip: fetch failed; cached refs are not fresh update evidence'
    exit 0
}
$resolved = Read-Git @('rev-parse', '--verify', ($tracking.Text + '^{commit}'))
if (-not $resolved.Ok -or $resolved.Text -notmatch '^[0-9a-f]{40,64}$') {
    Note 'skip: cannot resolve the fetched upstream'
    exit 0
}
$before = $head.Text
$after = $resolved.Text

if ($before -eq $after) {
    if ($DryRun) { Note 'up to date' }
    exit 0
}

$null = & git -C $repo merge-base --is-ancestor $before $after 2>$null
if ($LASTEXITCODE -ne 0) {
    $null = & git -C $repo merge-base --is-ancestor $after $before 2>$null
    if ($LASTEXITCODE -eq 0) { Note 'up to date; local branch is ahead of upstream' }
    else { Note 'skip: local branch and upstream diverged or ancestry is unavailable' }
    exit 0
}

if ($DryRun) {
    Note ("has updates: " + $before.Substring(0, 7) + " -> " + $after.Substring(0, 7))
    exit 0
}

# Recheck after the fetch; do not merge into a branch or edits changed meanwhile.
$current = Read-Git @('rev-parse', '--verify', 'HEAD')
$currentBranch = Read-Git @('symbolic-ref', '--quiet', '--short', 'HEAD')
$currentTracking = Read-Git @('rev-parse', '--symbolic-full-name', '@{upstream}')
$currentDirty = Read-Git @('status', '--porcelain=v1', '--untracked-files=normal')
if (-not $current.Ok -or $current.Text -ne $before -or -not $currentBranch.Ok -or
    $currentBranch.Text -ne $branch.Text -or -not $currentTracking.Ok -or
    $currentTracking.Text -ne $tracking.Text -or -not $currentDirty.Ok -or $currentDirty.Text) {
    Note 'skip: branch or working tree changed during fetch'
    exit 0
}
$out = & git -C $repo merge --ff-only --no-edit --no-overwrite-ignore -- $after 2>&1
$code = $LASTEXITCODE

if ($code -ne 0) {
    $msg = ($out -join ' ')
    if ($msg.Length -gt 300) { $msg = $msg.Substring(0, 300) }
    Note ("fast-forward refused: " + $msg)
    exit 0
}

$confirmed = Read-Git @('rev-parse', '--verify', 'HEAD')
if (-not $confirmed.Ok -or $confirmed.Text -ne $after) {
    Note 'skip: updated source HEAD could not be verified'
    exit 0
}
Note ("source updated " + $before.Substring(0, 7) + " -> " + $after.Substring(0, 7) + '; deployment and process reload are separate')

# Refresh the snapshots the panel reads, so the page shows the new version.
$snap = Join-Path $PSScriptRoot 'make_snapshots.py'
if (Test-Path $snap) {
    try { & python $snap | Out-Null } catch { }
}
