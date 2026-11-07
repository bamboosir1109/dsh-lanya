# Reminder daemon for Lanya.
#
# Why this exists: QQ sessions run with sandbox=workspace-write and approval=ask.
# Inside <WORKDIR> she can read/write freely, but anything needing elevation has
# no approval answerer in QQ and fails closed. Scheduling tasks from inside a QQ
# turn therefore does not work. So the scheduling lives HERE (one fixed task that
# runs forever), and she only has to write a plain JSON file inside her own folder.
#
# reminders.json entries (array):
#   { "text": "...", "at": "2026-10-11T18:00:00", "target": "YOUR_ADMIN_QQ" }
#       fires once at that local time, then is removed
#   { "text": "...", "daily": "09:00", "target": "YOUR_ADMIN_QQ" }
#       fires every day at that time, stays in the file
#   "target" is optional and defaults to the admin QQ below.
#
# ASCII-only source (PowerShell 5.1 reads BOM-less .ps1 as ANSI); all Chinese
# lives in reminders.json, read explicitly as UTF-8.

param(
  [switch]$Once
)

$ErrorActionPreference = 'Continue'
$observationHelper = Join-Path $PSScriptRoot 'qq_observation.ps1'
if (-not (Test-Path -LiteralPath $observationHelper -PathType Leaf)) { throw 'Missing QQ observation guard' }
. $observationHelper
if (-not (Get-Command Test-QqUnifiedMode -ErrorAction SilentlyContinue)) { throw 'QQ observation guard did not load' }
if (Test-QqUnifiedMode) {
  Write-Output 'paused: unified scheduler owns reminders; reminder records and receipts preserved'
  exit 3
}

# Resolve the same project and identity config as the panel and planning loop.
. (Join-Path $PSScriptRoot 'project_conf.ps1')
$root = Get-ProjectWorkDir
$runtimeDir = Join-Path $root 'napcat'
$notesDir = Get-ProjectDataDir
$remPath = Join-Path $notesDir 'reminders.json'
$statePath = Join-Path $notesDir 'reminders.state.json'
$logPath = Join-Path $notesDir 'reminders.log'
$scriptStartWrite = (Get-Item $PSCommandPath).LastWriteTime
$leasePath = Join-Path $runtimeDir 'scheduler-in-plugin.flag'
try {
  $lease = Get-Content -LiteralPath $leasePath -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
  if ($lease.version -eq 2 -and $lease.jobs -contains 'reminders' -and
      ((Get-Date) - (Get-Item -LiteralPath $leasePath).LastWriteTime).TotalSeconds -lt 120 -and
      (Get-Process -Id $lease.pid -ErrorAction SilentlyContinue)) { exit 0 }
} catch { }
$adminQQ = ''
$groupId = ''
try {
  $identity = Get-Content -LiteralPath (Join-Path $notesDir 'lanya-config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  if ($identity.adminQQ -is [array]) { $adminQQ = [string]$identity.adminQQ[0] }
  else { $adminQQ = [string]$identity.adminQQ }
  $groupId = [string]$identity.fanGroupQQ
  if (-not $groupId) { $groupId = [string]$identity.fanGroup }
} catch { }
$napcat = @{ napcatHttp = ('http://127.0.0.1:' + (Get-ProjectConfValue 'onebotPort' 8720)); napcatToken = '' }
$cfgPath = Join-Path $runtimeDir 'balance-alert.config.json'
try {
  $localConf = Get-Content -LiteralPath $cfgPath -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
  if ($localConf.napcatHttp) { $napcat.napcatHttp = [string]$localConf.napcatHttp }
  if ($localConf.napcatToken) { $napcat.napcatToken = [string]$localConf.napcatToken }
} catch { }
if (-not $napcat.napcatToken) {
  try {
    $onebot = Get-Content -LiteralPath (Join-Path $runtimeDir 'framework\config\onebot11.json') -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
    $napcat.napcatToken = [string]$onebot.network.httpServers[0].token
  } catch { }
}

function Confirm-Delivery($Response, $Client) {
  try {
    $null = $Response.EnsureSuccessStatusCode()
    $body = $Response.Content.ReadAsStringAsync().Result
    $ack = $body | ConvertFrom-Json
    if ($ack.status -ne 'ok' -or $ack.retcode -ne 0) { throw 'OneBot rejected delivery' }
    return $body
  } finally {
    if ($Response) { $Response.Dispose() }
    $Client.Dispose()
  }
}

function Get-ReminderKey($Reminder) {
  $fields = @('at', 'daily', 'target', 'text', 'mention', 'image')
  $identityValues = @($fields | ForEach-Object { [string]$Reminder.$_ })
  $bytes = [Text.Encoding]::UTF8.GetBytes(($identityValues -join [char]0))
  $hasher = [Security.Cryptography.SHA256]::Create()
  try { return ([BitConverter]::ToString($hasher.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
  finally { $hasher.Dispose() }
}

function Write-Log([string]$m) {
  $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m
  Add-Content -Path $logPath -Value $line -Encoding UTF8
  Write-Output $line
}

if (-not (Test-Path $notesDir)) { New-Item -ItemType Directory -Path $notesDir -Force | Out-Null }
if (-not (Test-Path $remPath)) {
  [System.IO.File]::WriteAllText($remPath, "[]", (New-Object System.Text.UTF8Encoding($false)))
}


function Send-QQ([string]$uin, [string]$text) {
  Assert-QqLegacyAction 'send_private_msg'
  Add-Type -AssemblyName System.Net.Http
  $client = New-Object System.Net.Http.HttpClient
  $client.Timeout = [TimeSpan]::FromSeconds(30)
  $client.DefaultRequestHeaders.Authorization =
    New-Object System.Net.Http.Headers.AuthenticationHeaderValue('Bearer', [string]$napcat.napcatToken)
  $payload = @{ user_id = $uin; message = $text } | ConvertTo-Json -Compress
  $content = New-Object System.Net.Http.StringContent($payload, [System.Text.Encoding]::UTF8, 'application/json')
  $url = ([string]$napcat.napcatHttp).TrimEnd('/') + '/send_private_msg'
  $resp = $client.PostAsync($url, $content).Result
  return (Confirm-Delivery $resp $client)
}

function Send-Group([string]$text, [string]$mention, [string]$image) {
  Assert-QqLegacyAction 'send_group_msg'
  Add-Type -AssemblyName System.Net.Http
  $client = New-Object System.Net.Http.HttpClient
  $client.Timeout = [TimeSpan]::FromSeconds(30)
  $client.DefaultRequestHeaders.Authorization =
    New-Object System.Net.Http.Headers.AuthenticationHeaderValue('Bearer', [string]$napcat.napcatToken)
  # Optional image. Sent as a segment array because a bare URL would post as text.
  # This is what lets a reminder actually send a picture instead of describing one -
  # the 00:00 one used to go out as its own instruction text.
  $withImg = ($image -and $image.Trim() -ne '')
  $msg = if ($mention -or $withImg) {
    $seg = @()
    if ($mention) { $seg += @{ type = 'at'; data = @{ qq = $mention } } }
    if ($withImg) { $seg += @{ type = 'image'; data = @{ file = $image } } }
    if ($text -and $text.Trim() -ne '') {
      $seg += @{ type = 'text'; data = @{ text = $(if ($mention) { ' ' + $text } else { $text }) } }
    }
    $seg
  } else { $text }
  $payload = @{ group_id = $groupId; message = $msg } | ConvertTo-Json -Compress -Depth 6
  $content = New-Object System.Net.Http.StringContent($payload, [System.Text.Encoding]::UTF8, 'application/json')
  $url = ([string]$napcat.napcatHttp).TrimEnd('/') + '/send_group_msg'
  $resp = $client.PostAsync($url, $content).Result
  return (Confirm-Delivery $resp $client)
}

# target "group" -> the fan group; anything else is a QQ number (private chat).
function Send-Any([string]$t, [string]$text, [string]$mention, [string]$image) {
  if ($t -eq 'group') {
    if ($groupId -notmatch '^\d+$') { throw 'No configured reminder group' }
    return (Send-Group $text $mention $image)
  }
  if ($t -eq 'admin') { $t = $adminQQ }
  if ($t -notmatch '^\d+$') { throw 'No configured reminder recipient' }
  # Private chat: keep it simple, an image reminder to a person is rare.
  return (Send-QQ $t $text)
}

while ($true) {
  $sendLock = $null
  try {
    $lockPath = Join-Path $notesDir 'reminders-send.lock'
    try {
      $sendLock = [IO.File]::Open($lockPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::ReadWrite, [IO.FileShare]::Read)
      $ownerBytes = [Text.Encoding]::UTF8.GetBytes([string]$PID)
      $sendLock.Write($ownerBytes, 0, $ownerBytes.Length)
      $sendLock.Flush()
    } catch {
      try {
        $ownerId = [int]([IO.File]::ReadAllText($lockPath))
        if ($ownerId -gt 0 -and -not (Get-Process -Id $ownerId -ErrorAction SilentlyContinue)) {
          Remove-Item -LiteralPath $lockPath -Force
        }
      } catch { }
      if ($Once) { break }
      Start-Sleep -Seconds 30
      continue
    }
    # Recheck ownership every round: the plugin may have started meanwhile.
    try {
      $lease = Get-Content -LiteralPath $leasePath -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
      if ($lease.version -eq 2 -and $lease.jobs -contains 'reminders' -and
          ((Get-Date) - (Get-Item -LiteralPath $leasePath).LastWriteTime).TotalSeconds -lt 120 -and
          (Get-Process -Id $lease.pid -ErrorAction SilentlyContinue)) { break }
    } catch { }
    $raw = [System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($remPath))
    if ($raw.Trim() -eq '') { $raw = '[]' }
    # NOT "@($raw | ConvertFrom-Json)". That form can hand back a single element
    # which IS the array, so foreach($r in $list) iterates once with $r = the whole
    # array - and PowerShell's member enumeration then makes $r.at return every
    # entry's "at" joined by a space, which is unparseable. Split it explicitly.
    $parsedJson = $raw | ConvertFrom-Json
    $list = @()
    if ($null -ne $parsedJson) {
      if ($parsedJson -is [array]) { $list = @($parsedJson) } else { $list = @($parsedJson) }
    }

    if ($list.Count -gt 0) {
      $now = Get-Date
      $today = $now.ToString('yyyy-MM-dd')
      $keep = @()
      $fired = $false

      $state = [pscustomobject]@{ daily = [pscustomobject]@{} }
      if (Test-Path $statePath) {
        try { $state = Get-Content $statePath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
      }
      $dailyMap = @{}
      $sentOnce = @{}
      if ($state.once) { foreach ($p in $state.once.PSObject.Properties) { $sentOnce[$p.Name] = [string]$p.Value } }
      if ($state.daily) {
        foreach ($p in $state.daily.PSObject.Properties) { $dailyMap[$p.Name] = [string]$p.Value }
      }

      $idx = 0
      foreach ($r in $list) {
        $idx++
        $key = Get-ReminderKey $r
        $target = if ($r.target) { [string]$r.target } else { $adminQQ }

        if ($r.at) {
          # ConvertFrom-Json already converts an ISO-8601 string to a DateTime.
          # Round-tripping it back through [string] and [DateTime]::Parse threw
          # "not a valid DateTime" on every loop - which is exactly why a reminder
          # she promised never fired. Take the value as-is when it is already a
          # DateTime, and only parse when it really is a string.
          $due = $null
          if ($r.at -is [datetime]) {
            $due = [datetime]$r.at
          }
          else {
            $asText = [string]$r.at
            $asDate = [datetime]::MinValue
            if ([datetime]::TryParse($asText, [ref]$asDate)) { $due = $asDate }
            else {
              try {
                $due = [datetime]::Parse($asText, [System.Globalization.CultureInfo]::InvariantCulture)
              } catch { $due = $null }
            }
          }
          if ($null -eq $due) {
            Write-Log ('WARN: unreadable time "' + [string]$r.at + '" - entry kept, fix the format')
            $keep += $r
            continue
          }
          if ($now -ge $due) {
            Write-Log ('due(once): ' + $r.text)
            try {
              if (-not $sentOnce[$key]) {
                Send-Any $target ([string]$r.text) ([string]$r.mention) ([string]$r.image) | Out-Null
                $sentOnce[$key] = $now.ToString('o')
              }
              $fired = $true
              continue
            } catch {
              Write-Log ('delivery failed; entry kept: ' + $_.Exception.Message)
              $keep += $r
              continue
            }
          }
          $keep += $r
          continue
        }

        if ($r.daily) {
          $hm = ([string]$r.daily).Trim()
          $todayKey = $key + '|' + $today
          $legacyKey = 'r' + $idx + '|' + $today
          $minutesLate = -1
          if ($hm -match '^([01]\d|2[0-3]):([0-5]\d)$') {
            $minutesLate = $now.Hour * 60 + $now.Minute - [int]$Matches[1] * 60 - [int]$Matches[2]
          }
          $grace = 5
          if ($null -ne $r.dailyGraceMinutes) {
            $parsedGrace = 5
            if ([int]::TryParse([string]$r.dailyGraceMinutes, [ref]$parsedGrace)) { $grace = [math]::Max(0, [math]::Min(60, $parsedGrace)) }
          }
          if ($minutesLate -ge 0 -and $minutesLate -le $grace -and $dailyMap[$todayKey] -ne 'yes' -and $dailyMap[$legacyKey] -ne 'yes') {
            Write-Log ('due(daily ' + $hm + '): ' + $r.text)
            try {
              Send-Any $target ([string]$r.text) ([string]$r.mention) ([string]$r.image) | Out-Null
              $dailyMap[$todayKey] = 'yes'
              $fired = $true
            } catch { Write-Log ('daily delivery failed; entry kept: ' + $_.Exception.Message) }
          }
          $keep += $r
          continue
        }

        $keep += $r   # unrecognised entry, keep as-is
      }

      if ($fired -or $keep.Count -ne $list.Count) {
        $dailyObj = [pscustomobject]@{}
        foreach ($entry in $dailyMap.GetEnumerator()) {
          # prune daily marks that are not from today
          $k = [string]$entry.Key
          if ($k -like ('*|' + $today)) { $dailyObj | Add-Member -NotePropertyName $k -NotePropertyValue 'yes' -Force }
        }
        [System.IO.File]::WriteAllText($statePath, ([pscustomobject]@{ daily = $dailyObj; once = $sentOnce } | ConvertTo-Json -Depth 5 -Compress), (New-Object System.Text.UTF8Encoding($false)))
        $encodedKeep = ConvertTo-Json -InputObject @($keep) -Depth 5
        if ([IO.File]::ReadAllText($remPath).TrimStart([char]0xFEFF) -eq $raw.TrimStart([char]0xFEFF)) {
          [System.IO.File]::WriteAllText($remPath, $encodedKeep, (New-Object System.Text.UTF8Encoding($false)))
        }
      }
    }
  } catch {
    Write-Log ('loop error: ' + $_.Exception.Message)
  } finally {
    if ($sendLock) {
      $sendLock.Dispose()
      Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
    }
  }

  if ($Once) { break }

  # Self-restart on edit. This is a long-running loop, so it holds its OWN copy of
  # the script in memory: changing the file on disk had no effect and a fixed bug
  # kept reproducing for hours while the log still showed the old error. Exit so
  # the scheduled task relaunches us with the current file.
  try {
    $nowWrite = (Get-Item $PSCommandPath -ErrorAction Stop).LastWriteTime
    if ($nowWrite -gt $scriptStartWrite) {
      Write-Log ('script changed on disk (' + $nowWrite + ' > ' + $scriptStartWrite + '); exiting so the task reloads it')
      break
    }
  } catch { }

  Start-Sleep -Seconds 30
}

