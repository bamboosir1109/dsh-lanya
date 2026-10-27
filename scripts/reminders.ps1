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

# When the plugin's built-in scheduler is alive it already fires this script's work
# (see the SCHEDULE table in dsh-lanya/lib/index.js). Bail out immediately so the
# scheduled task becomes a no-op instead of spawning a second powershell every
# minute just to call the tick.
$__marker = Join-Path $PSScriptRoot 'scheduler-in-plugin.flag'
if (Test-Path $__marker) {
  if (((Get-Date) - (Get-Item $__marker).LastWriteTime).TotalMinutes -lt 30) { exit 0 }
}

# Timestamp of the file we started from. The main loop compares against it and
# exits when the script is edited, so the scheduled task relaunches fresh code.
# Without this a long-running instance keeps executing its in-memory copy and a
# fixed bug keeps reproducing for hours.
$scriptStartWrite = (Get-Item $PSCommandPath).LastWriteTime

# Derived from this script's own location: <root>\napcat\<file>.
$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $root 'napcat'))) {
  $root = Split-Path -Parent $root
}
$notesDir  = Join-Path $root ([string][char]0x5C9A + [char]0x5A05)   # "Lanya" folder
$remPath   = Join-Path $notesDir 'reminders.json'
$statePath = Join-Path $notesDir 'reminders.state.json'
$logPath   = Join-Path $notesDir 'reminders.log'
$cfgPath = Join-Path $root 'napcat\balance-alert.config.json'
# The owner's QQ, read from config.
$adminQQ = ''
try {
  $cf = Join-Path (Split-Path -Parent $PSScriptRoot) (Join-Path ([string][char]0x5927 + [char]0x80A5 + [char]0x9C7C) 'lanya-config.json')
  if (Test-Path $cf) {
    $cj = Get-Content $cf -Raw -Encoding UTF8 | ConvertFrom-Json
    $a = $cj.adminQQ
    if ($a -is [array]) { if ($a.Count -gt 0) { $adminQQ = [string]$a[0] } }
    elseif ($a) { $adminQQ = [string]$a }
  }
} catch { }
$groupId = ''
try {
  $cfgFile = Join-Path (Split-Path -Parent $PSScriptRoot) ($dataDirName + '\lanya-config.json')
  if (Test-Path $cfgFile) {
    $cj = Get-Content $cfgFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($cj.fanGroupQQ) { $groupId = [string]$cj.fanGroupQQ }
    if (-not $groupId -and $cj.fanGroup) { $groupId = [string]$cj.fanGroup }
  }
} catch { }
if (-not $groupId) {
  try {
    if (-not (Get-Command Get-ProjectConfValue -ErrorAction SilentlyContinue)) {
      . (Join-Path $PSScriptRoot 'project_conf.ps1')
    }
    $groupId = [string](Get-ProjectConfValue 'fanGroup' '')
  } catch { }
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

$napcat = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json

function Send-QQ([string]$uin, [string]$text) {
  Add-Type -AssemblyName System.Net.Http
  $client = New-Object System.Net.Http.HttpClient
  $client.Timeout = [TimeSpan]::FromSeconds(30)
  $client.DefaultRequestHeaders.Authorization =
    New-Object System.Net.Http.Headers.AuthenticationHeaderValue('Bearer', [string]$napcat.napcatToken)
  $payload = @{ user_id = $uin; message = $text } | ConvertTo-Json -Compress
  $content = New-Object System.Net.Http.StringContent($payload, [System.Text.Encoding]::UTF8, 'application/json')
  $url = ([string]$napcat.napcatHttp).TrimEnd('/') + '/send_private_msg'
  $resp = $client.PostAsync($url, $content).Result
  return [System.Text.Encoding]::UTF8.GetString($resp.Content.ReadAsByteArrayAsync().Result)
}

function Send-Group([string]$text, [string]$mention, [string]$image) {
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
  return [System.Text.Encoding]::UTF8.GetString($resp.Content.ReadAsByteArrayAsync().Result)
}

# target "group" -> the fan group; anything else is a QQ number (private chat).
function Send-Any([string]$t, [string]$text, [string]$mention, [string]$image) {
  if ($t -eq 'group') { return (Send-Group $text $mention $image) }
  # Private chat: keep it simple, an image reminder to a person is rare.
  return (Send-QQ $t $text)
}

while ($true) {
  try {
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

      $state = @{ daily = @{} }
      if (Test-Path $statePath) {
        try { $state = Get-Content $statePath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
      }
      $dailyMap = @{}
      if ($state.daily) {
        foreach ($p in $state.daily.PSObject.Properties) { $dailyMap[$p.Name] = [string]$p.Value }
      }

      $idx = 0
      foreach ($r in $list) {
        $idx++
        $key = 'r' + $idx
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
            Send-Any $target ([string]$r.text) ([string]$r.mention) ([string]$r.image) | Out-Null
            $fired = $true
            continue   # drop it
          }
          $keep += $r
          continue
        }

        if ($r.daily) {
          $hm = ([string]$r.daily).Trim()
          $todayKey = $key + '|' + $today
          if ($now.ToString('HH:mm') -eq $hm -and $dailyMap[$todayKey] -ne 'yes') {
            Write-Log ('due(daily ' + $hm + '): ' + $r.text)
            Send-Any $target ([string]$r.text) ([string]$r.mention) ([string]$r.image) | Out-Null
            $dailyMap[$todayKey] = 'yes'
            $fired = $true
          }
          $keep += $r
          continue
        }

        $keep += $r   # unrecognised entry, keep as-is
      }

      if ($fired -or $keep.Count -ne $list.Count) {
        [System.IO.File]::WriteAllText($remPath, ($keep | ConvertTo-Json -Depth 5), (New-Object System.Text.UTF8Encoding($false)))
        $dailyObj = [pscustomobject]@{}
        foreach ($k in $dailyMap.Keys) {
          # prune daily marks that are not from today
          if ($k -like ('*|' + $today)) { $dailyObj | Add-Member -NotePropertyName $k -NotePropertyValue 'yes' -Force }
        }
        [System.IO.File]::WriteAllText($statePath, ([pscustomobject]@{ daily = $dailyObj } | ConvertTo-Json -Depth 5 -Compress), (New-Object System.Text.UTF8Encoding($false)))
      }
    }
  } catch {
    Write-Log ('loop error: ' + $_.Exception.Message)
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

