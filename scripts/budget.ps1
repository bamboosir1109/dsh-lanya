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
$observationHelper = Join-Path $PSScriptRoot 'qq_observation.ps1'
if (-not (Test-Path -LiteralPath $observationHelper -PathType Leaf)) { throw 'Missing QQ observation guard' }
. $observationHelper
if (-not (Get-Command Test-QqUnifiedMode -ErrorAction SilentlyContinue)) { throw 'QQ observation guard did not load' }
$null = Test-QqUnifiedMode

# ---- shared project config ------------------------------------------------
# Reads <repo>\config\project.json. The helper carries the same defaults, so a
# missing or broken config file leaves behaviour exactly as it was.
$confHelper = Join-Path $PSScriptRoot 'project_conf.ps1'
if (Test-Path $confHelper) { . $confHelper }
if (-not (Get-Command Get-ProjectConfValue -ErrorAction SilentlyContinue)) {
  function Get-ProjectConfValue { param([string]$KeyPath, $Default = $null) return $Default }
}
# An explicit -Limit still wins; the config only replaces the DEFAULT (1.0).
if (-not $PSBoundParameters.ContainsKey('Limit')) {
  
}
$dataDirName = [string](Get-ProjectConfValue 'dataDirName' ([string][char]0x5927 + [char]0x80A5 + [char]0x9C7C))

# $root is the napcat folder; her files live one level up. Same mistake as
# everywhere else in this script family.
$workDir = Get-ProjectWorkDir
$dafeiyuDir  = Join-Path $workDir $dataDirName
# %USERPROFILE% is cmd syntax; PowerShell does not expand it in a plain string,
# so this stayed literal and Test-Path always failed. The script then logged
# "ledger missing" and exited - the budget was never recalculated at all.
$ledger    = Join-Path (Join-Path $env:USERPROFILE '.dsh') 'storages\cost-meter\ledger.json'
$outPath   = Join-Path $dafeiyuDir 'budget.json'

# The panel writes its own limit into budget.json. Do not stomp it with the
# config default every run - that is exactly the "I set 2.5 and it changed
# itself back" complaint. Config is only the initial value.
$prevLimit = $null
if (Test-Path $outPath) {
  try {
    $pj0 = Get-Content $outPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -ne $pj0.limit) { $prevLimit = [double]$pj0.limit }
  } catch { }
}
if ($null -ne $prevLimit -and $prevLimit -gt 0) {
  $Limit = $prevLimit
} else {
  $Limit = [double](Get-ProjectConfValue 'budget.dailyLimit' 1.0)
}$logPath   = Join-Path $dafeiyuDir 'budget.log'
$cfgPath   = (Join-Path $PSScriptRoot 'balance-alert.config.json')
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
  # 0.0, not 0: [math]::Max(0, 2.3424) picks the INTEGER overload and
  # truncates to 2. That is how the remaining figure went wrong.
  remaining = [math]::Round([math]::Max(0.0, $Limit - $spent), 4)
  exhausted = $exhausted
  # Clear the "already told her" marker when the day changes, otherwise the
  # reminder never fires again after the first day.
  notifiedFor = if ($prev -and $prev.notifiedFor -eq $today) { [string]$prev.notifiedFor } else { '' }
  prevDate  = if ($prev -and $prev.date) { [string]$prev.date } else { $today }
  dmSpent    = [math]::Round($dmSpent, 4)
  groupSpent = [math]::Round($groupSpent, 4)
  # Total across every channel, for the panel to show next to the pocket-money
  # number. $spent above stays "her own play" on purpose.
  totalSpent = [math]::Round($spent + $dmSpent + $groupSpent, 4)
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
  if (Test-QqUnifiedMode) {
    try {
      $receipt = Submit-QqObservation -Scope (Get-QqObservationScope -Target admin) -Kind 'budget.exhausted' -Producer 'budget' -EventId ('budget:exhausted:' + $today) -Payload @{ date = $today; status_path = $outPath }
      Write-Log ('budget observation state=' + $receipt.state + '; no delivery occurred')
    } catch {
      Write-Log 'budget observation failed; notification receipt unchanged'
      exit 1
    }
    # Pending evidence is not a delivered QQ message. Keep notifiedFor intact.
    exit 0
  }
  Assert-QqLegacyAction 'send_private_msg'
  Add-Type -AssemblyName System.Net.Http
  $napcat = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $client = New-Object System.Net.Http.HttpClient
  $client.Timeout = [TimeSpan]::FromSeconds(30)
  $client.DefaultRequestHeaders.Authorization =
    New-Object System.Net.Http.Headers.AuthenticationHeaderValue('Bearer', [string]$napcat.napcatToken)
  # Message text comes from a JSON template: this script must stay pure ASCII,
  # because PowerShell 5.1 reads a BOM-less .ps1 with the ANSI codepage and would
  # mangle Chinese literals into a parse error (that happened once here already).
  $msgFile = (Join-Path $PSScriptRoot 'budget.messages.json')
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
