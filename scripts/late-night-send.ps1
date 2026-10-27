# One-shot "good night" sender for Lanya.
#
# Why: the reminder daemon may not be running (its scheduled task is registered
# by an elevated script), so this script arms itself, sleeps until the moment,
# and then sends the group message itself. It first checks the recent group
# history so the reminder daemon firing at the same minute does not cause a
# duplicate message.
#
# ASCII-only source (PowerShell 5.1 reads BOM-less .ps1 as ANSI); the Chinese
# message text is read from reminders.json as UTF-8.

$ErrorActionPreference = 'Continue'

$root      = '<WORKDIR>'
$lanyaDir  = Join-Path $root ([string][char]0x5C9A + [char]0x5A05)
$logPath   = Join-Path $lanyaDir 'late-night.log'
$remPath   = Join-Path $lanyaDir 'reminders.json'
$sentLog   = Join-Path $lanyaDir 'sent-log.txt'
$cfgPath   = '<WORKDIR>\napcat\balance-alert.config.json'
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
# Her own QQ, read from config (was a hardcoded number).
$selfId = ''
try {
  $cf = Join-Path (Split-Path -Parent $PSScriptRoot) (Join-Path ([string][char]0x5927 + [char]0x80A5 + [char]0x9C7C) 'lanya-config.json')
  if (Test-Path $cf) {
    $cj = Get-Content $cf -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($cj.botQQ) { $selfId = [string]$cj.botQQ }
  }
} catch { }
$hour      = 23
$minute    = 30

function Write-Log([string]$m) {
  $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m
  Add-Content -Path $logPath -Value $line -Encoding UTF8
}

$napcat = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
$hdr = @{ Authorization = 'Bearer ' + [string]$napcat.napcatToken }
$base = ([string]$napcat.napcatHttp).TrimEnd('/')

function Invoke-Api([string]$action, $obj) {
  $json = $obj | ConvertTo-Json -Compress -Depth 6
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
  return Invoke-RestMethod -Uri ($base + '/' + $action) -Method Post -Headers $hdr -ContentType 'application/json; charset=utf-8' -Body $bytes
}

$target = (Get-Date).Date.AddHours($hour).AddMinutes($minute)
if ($target -lt (Get-Date)) { $target = $target.AddDays(1) }
Write-Log ('armed for ' + $target.ToString('yyyy-MM-dd HH:mm'))

while ((Get-Date) -lt $target.AddSeconds(25)) { Start-Sleep -Seconds 20 }
Write-Log 'woke up'

$text = ''
$mention = ''
try {
  $raw = [System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($remPath))
  $list = @($raw | ConvertFrom-Json)
  foreach ($r in $list) {
    if ([string]$r.target -eq 'group' -and $r.text) {
      $text = [string]$r.text
      $mention = [string]$r.mention
      break
    }
  }
} catch { Write-Log ('reminders parse error: ' + $_.Exception.Message) }
if ($text -eq '') { Write-Log 'no message text found, abort'; exit 0 }

$already = $false
try {
  $hist = Invoke-Api 'get_group_msg_history' @{ group_id = [int64]$groupId; count = 8 }
  foreach ($m in $hist.data.messages) {
    $t = [datetimeoffset]::FromUnixTimeSeconds([int64]$m.time).ToLocalTime().DateTime
    if ([string]$m.user_id -eq $selfId -and $t -ge $target.AddMinutes(-3)) { $already = $true }
  }
} catch { Write-Log ('history error: ' + $_.Exception.Message) }

if ($already) { Write-Log 'already sent through another path, skip'; exit 0 }

try {
  $msg = if ($mention) {
    @(@{ type = 'at'; data = @{ qq = $mention } }, @{ type = 'text'; data = @{ text = ' ' + $text } })
  } else { $text }
  $r = Invoke-Api 'send_group_msg' @{ group_id = [int64]$groupId; message = $msg }
  Write-Log ('sent: ' + ($r | ConvertTo-Json -Compress))
  $entry = '[' + (Get-Date).ToString('yyyy-MM-dd HH:mm') + '] -> group: ' + $text + "`r`n"
  [System.IO.File]::AppendAllText($sentLog, $entry, (New-Object System.Text.UTF8Encoding($false)))
} catch { Write-Log ('send error: ' + $_.Exception.Message) }
