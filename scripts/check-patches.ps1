# Verify Dafeiyu's local patches are still present in dsh-onebot-qq.
#
# Why this exists: seven behaviours she depends on are local edits to the plugin's
# inbound.js / outbound.js. A routine plugin upgrade replaces those files and every
# one of them disappears - silently. The symptoms look like personality-looking symptoms rather than a broken install,
# so the real cause is easy to miss.
#
# Run this after ANY plugin update, or on a timer:
#   powershell -NoProfile -ExecutionPolicy Bypass -File check-patches.ps1
#
# Exit 0 = all present. Exit 1 = something is missing (prints what).
#
# ASCII-only source.

$ErrorActionPreference = 'Continue'

$lib = (Join-Path $env:USERPROFILE '.dsh\profiles\desktop\node_modules\dsh-onebot-qq\lib')
$inbound = Join-Path $lib 'inbound.js'
$outbound = Join-Path $lib 'outbound.js'
$log = (Join-Path $PSScriptRoot 'patch-check.log')

function Note([string]$m) {
  $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m
  Add-Content -Path $log -Value $line -Encoding UTF8
  Write-Output $line
}

if (-not (Test-Path $inbound) -or -not (Test-Path $outbound)) {
  Note 'FAIL plugin files not found - is dsh-onebot-qq installed?'
  exit 1
}

$inText = [System.IO.File]::ReadAllText($inbound, [System.Text.Encoding]::UTF8)
$outText = [System.IO.File]::ReadAllText($outbound, [System.Text.Encoding]::UTF8)

# Each entry: label, which file, a string that only our patch introduces.
$checks = @(
  @{ name = '01 self outbox write';      text = $inText;  marker = 'selfOutbox' },
  @{ name = '02 self outbox inject';     text = $inText;  marker = 'PATCHED (local): surface the bot' },
  @{ name = '03 voice transcription';    text = $inText;  marker = 'fetch_ptt_text' },
  @{ name = '04 cross-chat log write';   text = $inText;  marker = 'CROSS_CHAT_LOG' },
  @{ name = '05 cross-chat inject';      text = $inText;  marker = 'crossPrefix' },
  @{ name = '06 person profile inject';  text = $inText;  marker = 'CROSS_CHAT_PEOPLE_DIR' },
  @{ name = '07 qq face to text';        text = $inText;  marker = 'faceText' },
  @{ name = '08 outbound guard';         text = $outText; marker = 'PATCHED (local): one gate' },
  @{ name = '09 meme placeholder strip'; text = $outText; marker = 'stripped meme placeholder' }
)

$missing = @()
foreach ($c in $checks) {
  $ok = $c.text.Contains($c.marker)
  if (-not $ok) { $missing += $c.name }
  Write-Output ('  ' + $(if ($ok) { 'OK  ' } else { 'GONE' }) + '  ' + $c.name)
}

if ($missing.Count -eq 0) {
  Note ('PASS all ' + $checks.Count + ' patches present')
  exit 0
}

Note ('FAIL ' + $missing.Count + ' patch(es) missing after an upgrade: ' + ($missing -join '; '))
Note ('Rebuild them from ' + (Join-Path $PSScriptRoot 'plugin-patch-backup\inbound.js.orig') + ' (see the merge-plan doc)')
exit 1
