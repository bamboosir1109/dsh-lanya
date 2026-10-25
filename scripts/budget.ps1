# Daily token budget for Dafeiyu.
#
# Sums TODAY's cost of her own QQ sessions (ids starting with "onebot-") from the
# dsh-cost-meter ledger, then writes a small status file she and the daemons can
# read. Costs nothing to run - it only reads a JSON file.
#
# Why not just watch the total: the ledger's daily cost also includes the owner's
# own work (development sessions), which would eat her budget instantly. Only the
# onebot-* sessions are hers.
#
# Output: <dafeiyuDir>\budget.json (her own folder next to reminders.json)
#   { "date": "...", "limit": 1.0, "spent": 0.06, "remaining": 0.94,
#     "exhausted": false, "notifiedFor": "" }
#
# ASCII-only source (PowerShell 5.1 reads BOM-less .ps1 as ANSI); the Chinese
# folder name is built from code points.

param(
  [double]$Limit = 1.0,
  [switch]$Notify
)


# Repo root: <root>/napcat/xxx.ps1 -> <root>
$root = Split-Path -Parent $PSScriptRoot
$ErrorActionPreference = 'Continue'

$dafeiyuDir  = Join-Path $root ([string][char]0x5927 + [char]0x80A5 + [char]0x9C7C)   # "Dafeiyu"
$ledger    = '%USERPROFILE%\.dsh\storages\cost-meter\ledger.json'
$outPath   = Join-Path $dafeiyuDir 'budget.json'
$logPath   = Join-Path $dafeiyuDir 'budget.log'
$cfgPath   = '$root\napcat\balance-alert.config.json'
$adminQQ   = 'YOUR_ADMIN_QQ'

function Write-Log([string]$m) {
  $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m
  Add-Content -Path $logPath -Value $line -Encoding UTF8
  Write-Output $line
}

if (-not (Test-Path $dafeiyuDir)) { New-Item -ItemType Directory -Path $dafeiyuDir -Force | Out-Null }
if (-not (Test-Path $ledger)) { Write-Log 'ledger missing; nothing to do'; exit 0 }

$today = (Get-Date).ToString('yyyy-MM-dd')
$spent = 0.0
try {
  $j = [System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($ledger)) | ConvertFrom-Json
  $day = $j.days.$today
  if ($day -and $day.sessions) {
    foreach ($s in $day.sessions) {
      $id = [string]$s.id
      # What counts as her pocket money: the things SHE chooses to do - going
      # online, playing a game, writing notes, maintaining her own memory.
      # The owner's rule: pocket money covers only what SHE does for fun -
      # going online, games, her own projects. Talking to people is social, not
      #
      # Session ids identify the kind of run reliably enough:
      #   onebot-*    an actual QQ chat (private or group)      -> NOT hers to pay
      #   session-*   a session started from the desktop GUI     -> the owner's work
      #   <bare uuid> a headless run launched by one of her own daemons -> HERS
      # Split QQ sessions into private and group: each has its own daily cap.
      if ($id -like 'onebot-*') {
        if ($id -match 'group' -or $id -match 'YOUR_GROUP_ID') {
          $groupSpent += [double]$s.apiCost
        } else {
          $dmSpent += [double]$s.apiCost
        }
        continue
      }
      if ($id -like 'session-*') { continue }
      $spent += [double]$s.apiCost
    }
  }
} catch {
  Write-Log ('ledger read failed: ' + $_.Exception.Message)
  exit 1
}

$exhausted = $spent -ge $Limit
$prev = $null
if (Test-Path $outPath) { try { $prev = Get-Content $outPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { } }

# Carry the panel-set caps and switches through untouched. Otherwise every run
$dmLimit    = if ($prev -and $null -ne $prev.dmLimit)   { [double]$prev.dmLimit }   else { 0.0 }
$groupLimit = if ($prev -and $null -ne $prev.groupLimit){ [double]$prev.groupLimit } else { 0.0 }
$ask        = if ($prev -and $null -ne $prev.askWhenExhausted) { [bool]$prev.askWhenExhausted } else { $true }

$status = [pscustomobject]@{
  date      = $today
  limit     = $Limit
  spent     = [math]::Round($spent, 4)
  remaining = [math]::Round([math]::Max(0, $Limit - $spent), 4)
  exhausted = $exhausted
  notifiedFor = if ($prev -and $prev.notifiedFor) { [string]$prev.notifiedFor } else { '' }
  dmSpent    = [math]::Round($dmSpent, 4)
  groupSpent = [math]::Round($groupSpent, 4)
  dmLimit    = $dmLimit
  groupLimit = $groupLimit
  askWhenExhausted = $ask
  # Mute this channel once the cap is hit. 0 means unlimited, not "cap is zero".
  groupMuted = ($groupLimit -gt 0 -and $groupSpent -ge $groupLimit)
  dmMuted    = ($dmLimit    -gt 0 -and $dmSpent    -ge $dmLimit)
}
[System.IO.File]::WriteAllText($outPath, ($status | ConvertTo-Json -Compress), (New-Object System.Text.UTF8Encoding($false)))

Write-Log ('today=' + $today + ' spent=' + $status.spent + ' limit=' + $Limit + ' exhausted=' + $exhausted)

# Tell her once per day when the budget runs out.
if ($Notify -and $exhausted -and $status.notifiedFor -ne $today) {
  Add-Type -AssemblyName System.Net.Http
  $napcat = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $client = New-Object System.Net.Http.HttpClient
  $client.Timeout = [TimeSpan]::FromSeconds(30)
  $client.DefaultRequestHeaders.Authorization =
    New-Object System.Net.Http.Headers.AuthenticationHeaderValue('Bearer', [string]$napcat.napcatToken)
  # Message text comes from a JSON template: this script must stay pure ASCII,
  # because PowerShell 5.1 reads a BOM-less .ps1 with the ANSI codepage and would
  # mangle Chinese literals into a parse error (that happened once here already).
  $msgFile = '$root\napcat\budget.messages.json'
  $tpl = [System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($msgFile)) | ConvertFrom-Json
  $msg = $tpl.exhausted -f $Limit
  $payload = @{ user_id = $adminQQ; message = $msg } | ConvertTo-Json -Compress
  $content = New-Object System.Net.Http.StringContent($payload, [System.Text.Encoding]::UTF8, 'application/json')
  $url = ([string]$napcat.napcatHttp).TrimEnd('/') + '/send_private_msg'
  try {
    $res = $client.PostAsync($url, $content).Result
    $body = [System.Text.Encoding]::UTF8.GetString($res.Content.ReadAsByteArrayAsync().Result)
    Write-Log ('budget notice: ' + $body)
    $status.notifiedFor = $today
    [System.IO.File]::WriteAllText($outPath, ($status | ConvertTo-Json -Compress), (New-Object System.Text.UTF8Encoding($false)))
  } catch { Write-Log ('budget notice failed: ' + $_.Exception.Message) }
}
