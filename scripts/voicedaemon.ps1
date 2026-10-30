# Voice queue daemon for Dafeiyu.
#
# The QQ session runs with sandbox=workspace-write and cannot initialise SAPI, so
# she cannot synthesise speech inside a turn. Instead she WRITES a request into
# voice-queue.json and this daemon (running outside the DSH sandbox) does the
# synthesis and sends the voice message for her.
#
# voice-queue.json (array, in her own folder):
#   [ { "text": "<what to say>", "target": "admin" } ]
#   target: "admin" (default) | "group" | a QQ number
# The daemon clears each entry once it has been sent.
#
# ASCII-only source; Chinese lives only in the data files it reads.

param(
  [switch]$Once,
  [int]$PollSeconds = 15,
  [string]$Voice = 'zh-CN-XiaoyiNeural',
  [int]$Rate = 0
)

$ErrorActionPreference = 'Continue'
$observationHelper = Join-Path $PSScriptRoot 'qq_observation.ps1'
if (-not (Test-Path -LiteralPath $observationHelper -PathType Leaf)) { throw 'Missing QQ observation guard' }
. $observationHelper
if (-not (Get-Command Test-QqUnifiedMode -ErrorAction SilentlyContinue)) { throw 'QQ observation guard did not load' }
if (Test-QqUnifiedMode) {
  # Do not even open/claim the old queue. The unified voice tool must migrate or
  # consume pending records explicitly; a pause is not successful task delivery.
  Write-Output 'paused: legacy voice queue preserved for unified owner; no delivery occurred'
  exit 3
}

$root      = $PSScriptRoot
$dafeiyuDir  = Join-Path (Split-Path -Parent $PSScriptRoot) ([string][char]0x5927 + [char]0x80A5 + [char]0x9C7C)   # "Dafeiyu"
$queuePath = Join-Path $dafeiyuDir 'voice-queue.json'
$logPath   = Join-Path $dafeiyuDir 'voice.log'
$sayScript = Join-Path $root 'say.ps1'
# Local GPT-SoVITS engine, preferred. say.ps1 (edge-tts) stays as the fallback so
# her voice never goes silent while the local model server is down.
$localScript = Join-Path $root 'say-local.ps1'
$localSpeed = 1.0
$cfgPath   = Join-Path $root 'balance-alert.config.json'
# Group id must come from config. It used to be the literal string
# "YOUR_GROUP_ID" - a placeholder left behind by a sanitising pass - which meant
# sharing a note or sending a voice message to a group that does not exist.
# In QQ that shows up as a phantom chat whose name is YOUR_GROUP_ID.
$groupId = ''
try {
  if (-not (Get-Command Get-ProjectConfValue -ErrorAction SilentlyContinue)) {
    . (Join-Path $PSScriptRoot 'project_conf.ps1')
  }
} catch { }
try {
  $cfgFile = Join-Path (Split-Path -Parent $PSScriptRoot) ($dataDirName + '\lanya-config.json')
  if (Test-Path $cfgFile) {
    $cj = Get-Content $cfgFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($cj.fanGroupQQ) { $groupId = [string]$cj.fanGroupQQ }
    if (-not $groupId -and $cj.fanGroup) { $groupId = [string]$cj.fanGroup }
  }
} catch { }
if (-not $groupId) {
  try { $groupId = [string](Get-ProjectConfValue 'fanGroup' '') } catch { }
}
# The owner's QQ. This was the literal string "YOUR_ADMIN_QQ" - a placeholder
# from a sanitising pass - so alerts, shared notes and voice messages were all
# addressed to a user that does not exist ("cannot get user info" from NapCat).
$adminQQ = ''
try {
  $cf = Join-Path (Split-Path -Parent $PSScriptRoot) ($dataDirName + '\lanya-config.json')
  if (Test-Path $cf) {
    $cj = Get-Content $cf -Raw -Encoding UTF8 | ConvertFrom-Json
    $a = $cj.adminQQ
    if ($a -is [array]) { if ($a.Count -gt 0) { $adminQQ = [string]$a[0] } }
    elseif ($a) { $adminQQ = [string]$a }
  }
} catch { }

function Write-Log([string]$m) {
  $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m
  Add-Content -Path $logPath -Value $line -Encoding UTF8
  Write-Output $line
}

if (-not (Test-Path $dafeiyuDir)) { New-Item -ItemType Directory -Path $dafeiyuDir -Force | Out-Null }
if (-not (Test-Path $queuePath)) {
  [System.IO.File]::WriteAllText($queuePath, '[]', (New-Object System.Text.UTF8Encoding($false)))
}
$napcat = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json

function Invoke-NapCat([string]$action, $bodyObj) {
  Assert-QqLegacyAction $action
  Add-Type -AssemblyName System.Net.Http
  $client = New-Object System.Net.Http.HttpClient
  $client.Timeout = [TimeSpan]::FromSeconds(60)
  $client.DefaultRequestHeaders.Authorization =
    New-Object System.Net.Http.Headers.AuthenticationHeaderValue('Bearer', [string]$napcat.napcatToken)
  $json = $bodyObj | ConvertTo-Json -Compress -Depth 6
  $content = New-Object System.Net.Http.StringContent($json, [System.Text.Encoding]::UTF8, 'application/json')
  $url = ([string]$napcat.napcatHttp).TrimEnd('/') + '/' + $action
  $resp = $client.PostAsync($url, $content).Result
  return [System.Text.Encoding]::UTF8.GetString($resp.Content.ReadAsByteArrayAsync().Result)
}

Write-Log ('voiced start: poll=' + $PollSeconds + 's voice=' + $Voice)
Write-Log ('queued now: ' + ([System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($queuePath))))

while ($true) {
  try {
    $raw = [System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($queuePath))
    if ($raw.Trim() -eq '') { $raw = '[]' }
    $list = @($raw | ConvertFrom-Json)
    # Claim the queue atomically RIGHT AFTER reading, so a second pass (or a
    # second daemon) can never deliver the same entry twice. This is the fix for
    # the duplicate-voice incident.
    if ($list.Count -gt 0) {
      [System.IO.File]::WriteAllText($queuePath, '[]', (New-Object System.Text.UTF8Encoding($false)))
    }

    if ($list.Count -gt 0) {
      Write-Log ('picked up ' + $list.Count + ' voice request(s)')
      foreach ($item in $list) {
        # Field names: she does not always use "text". Accept the obvious
        # synonyms, and NEVER drop an entry silently - an earlier version did
        # exactly that, so her requests vanished every 15 seconds with only
        # "queue cleared" in the log, which looked like the feature working.
        $text = ''
        foreach ($k in @('text', 'message', 'content', 'say', 'msg', 'speak')) {
          if ($item.$k) { $text = [string]$item.$k; break }
        }
        if ($text.Trim() -eq '') {
          Write-Log ('WARN: entry has no usable text field; keys=' + (($item.PSObject.Properties.Name) -join ',') + ' raw=' + ($item | ConvertTo-Json -Compress))
          continue
        }
        $target = if ($item.target) { [string]$item.target } else { 'admin' }
        # Optional mood: normal | excited | soft | pout. Anything else -> normal.
        $mood = if ($item.mood) { [string]$item.mood } else { 'normal' }
        if (@('normal', 'excited', 'soft', 'pout') -notcontains $mood) { $mood = 'normal' }

        # 1) synthesise.
        #
        # Preferred engine is the local GPT-SoVITS server: it gives her a real,
        # characterful voice instead of the flat official TTS, at ~1.5s per
        # sentence. It is only usable while the model server is up, so edge-tts
        # stays as a fallback - her voice must never go silent because I was
        # tinkering with the new engine.
        $out = $null
        if ($localScript -and (Test-Path $localScript)) {
          $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $localScript -Text $text -Speed $localSpeed 2>&1
        }
        $outStr = ($out | Out-String).Trim()
        $wav = ''
        if ($outStr -match '^OK\s+(.+?)\s+\(') { $wav = $Matches[1].Trim() }
        if ($wav -eq '' -or -not (Test-Path $wav)) {
          Write-Log ('local engine unavailable (' + $outStr + '); falling back to edge-tts')
          $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $sayScript -Text $text -Voice $Voice -Mood $mood 2>&1
          $outStr = ($out | Out-String).Trim()
          if ($outStr -match '^OK\s+(.+?)\s+\(') { $wav = $Matches[1].Trim() }
        }
        if ($wav -eq '' -or -not (Test-Path $wav)) {
          Write-Log ('synthesis FAILED, text was: ' + $text)
          Write-Log ('  detail: ' + $outStr)
          [System.IO.File]::AppendAllText((Join-Path $dafeiyuDir 'voice-failed.log'), ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm') + '] ' + $target + ' (' + $mood + '): ' + $text + "`r`n"), (New-Object System.Text.UTF8Encoding($false)))
          continue
        }
        Write-Log ('synthesised ' + (Split-Path $wav -Leaf) + ' mood=' + $mood + ' for ' + $target)

        # 2) send as a voice (record) segment
        $seg = @(@{ type = 'record'; data = @{ file = $wav } })
        $res = if ($target -eq 'group') {
          Invoke-NapCat 'send_group_msg' @{ group_id = $groupId; message = $seg }
        } elseif ($target -eq 'admin') {
          Invoke-NapCat 'send_private_msg' @{ user_id = $adminQQ; message = $seg }
        } elseif ($target -match '^\d{5,12}$') {
          Invoke-NapCat 'send_private_msg' @{ user_id = $target; message = $seg }
        } else {
          Write-Log ('unknown target ' + $target)
          continue
        }
        Write-Log ('sent voice to ' + $target + ': ' + $res)

        # Keep a readable record of what she said out loud.
        $sentLog = Join-Path $dafeiyuDir 'sent-log.txt'
        $entry = '[' + (Get-Date).ToString('yyyy-MM-dd HH:mm') + '] -> ' + $target + ' (voice): ' + $text + "`r`n"
        [System.IO.File]::AppendAllText($sentLog, $entry, (New-Object System.Text.UTF8Encoding($false)))
      }
      # clear the queue (already done right after reading; kept as a safety net)
      Write-Log 'queue cleared'
    }
  } catch {
    Write-Log ('loop error: ' + $_.Exception.Message)
  }

  if ($Once) { break }
  Start-Sleep -Seconds $PollSeconds
}

Write-Log 'voiced exit'
