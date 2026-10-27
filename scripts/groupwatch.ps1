# Realtime fan-group watcher.
#
# Instead of polling "should I post now?" on a fixed schedule, this runs as a
# long-lived daemon that watches the group and asks a headless agent to DECIDE:
# join the conversation when there is something to add, stay silent otherwise,
# and start a topic only when the room has gone quiet.
#
# Decision loop (every PollSeconds):
#   1. quiet-hours gate (default 9:00-23:00) - never post at night
#   2. read the last N group messages
#   3. hard rate limits: min gap since her own last message, max per hour
#   4. build a prompt and ask the headless profile for "SPEAK <line>" or "SILENT"
#   5. deliver via NapCat HTTP when the answer is SPEAK
#
# Triggers handed to the model:
#   "new-messages"  someone is chatting -> may join if it fits
#   "quiet"         nothing new for QuietMinutes -> may start a topic
#
# ASCII-only source (Windows PowerShell 5.1 reads BOM-less .ps1 as ANSI), so all
# Chinese lives in groupwatch.prompt.txt, read explicitly as UTF-8.

param(
  [switch]$DryRun,
  [switch]$Once,
  [string]$GroupId = '',
  [int]$PollSeconds = 60,
  [int]$QuietMinutes = 180,
  # Raised a lot on the owner's request (owner: make her much more active than this)

  # 4-minute gap and 6-per-2h ceiling made her a bystander rather than a member of
  # the group. MinGap is now 1 minute so she can answer back-to-back in a live
  # exchange, and the ceiling is high enough that hitting it is rare.
  # The counter slides over 2 hours, so 40 means roughly 20/hour.
  # Two different budgets, because they are two different behaviours.
  #
  # The owner's rule: joining a live conversation should not be throttled at all -
  # that is just being part of the group. What must not happen is her starting
  # topics over and over while nothing is going on. So the tight limits apply only
  # to self-started ('quiet') messages.
  [int]$MinGapMinutes = 1,           # when someone else is already talking
  # Self-started topics are NOT on a fixed interval. After she starts one, the next
  # allowed time is drawn at random from this range so the rhythm is not a metronome.
  # 
  # from 90 minutes to 180 (3 hours).
  [int]$QuietGapMinMinutes = 180,
  [int]$QuietGapMaxMinutes = 300,
  # HARD daily ceiling on messages SHE starts (not replies to someone addressing
  # her). The 2h window cap alone still allowed ~12 cold starts a day, which is why
  # she kept looking like she lives in the group. Half of that is plenty.
  [int]$MaxQuietStartsPerDay = 6,
  # Self-started topics per 2-hour window. With a ~3h cadence this is a backstop,
  # not the thing that normally stops her.
  # Soft 24h target . Not a hard cap: above it the chance of
  # speaking is lowered, below it the chance of starting something is raised.
  # This anchors the daily volume without freezing live conversation.
  [int]$MaxPer24Hours = 40,
  # Self-started topics per 2-hour window (hard stop for cold starts only).
  [int]$MaxPerHour = 1,
  # Hard ceiling on ANY send within one clock hour . When the hour is
  # full she stays silent until the hour rolls over - no catching up later.
  [int]$MaxPerHourAny = 6,
  # Runaway guard across ALL sends in the 2h window. Deliberately high: joining a
  # live conversation must not be the thing that silences her.
  [int]$MaxTotal = 200,
  [int]$WindowStart = 9,
  [int]$WindowEnd = 23
)

# Repo root: <root>/napcat/xxx.ps1 -> <root>
$root = Split-Path -Parent $PSScriptRoot

# ---- shared project config ------------------------------------------------
# Reads <repo>\config\project.json. The helper carries the same defaults, so a
# missing or broken config file leaves behaviour exactly as it was.
$confHelper = Join-Path $PSScriptRoot 'project_conf.ps1'
if (Test-Path $confHelper) { . $confHelper }
if (-not (Get-Command Get-ProjectConfValue -ErrorAction SilentlyContinue)) {
  function Get-ProjectConfValue { param([string]$KeyPath, $Default = $null) return $Default }
}
# An explicit -PollSeconds on the command line still wins; the config only
# replaces the DEFAULT, which is the same 60 seconds either way.
if (-not $PSBoundParameters.ContainsKey('PollSeconds')) {
  $PollSeconds = [int](Get-ProjectConfValue 'daemon.pollSeconds' 60)
}
$dataDirName = [string](Get-ProjectConfValue 'dataDirName' ([string][char]0x5927 + [char]0x80A5 + [char]0x9C7C))

# $root is the napcat folder; everything of hers lives one level up.
# Join-Path $root $dataDirName silently produced <napcat>\<dataDir>, which
# does not exist - that is why budget, tunables, affinity and lanya-config
# were all being read as empty.
# Use $PSScriptRoot, not $root: at this point $root still holds the PARENT
# of the script folder, so Split-Path -Parent $root would give the drive root.
$workDir = Split-Path -Parent $PSScriptRoot


# ---------------------------------------------------------------------------
# Respect the group spending cap.
#
# budget.ps1 recomputes how much she has spent in group chats today and sets
# groupMuted once the cap is hit. Without this check the cap would only ever be
# a number on a web page - it has to actually stop her from talking.
#
# Write-Log is defined further down, so this uses Write-Output directly.
# ---------------------------------------------------------------------------
# How many speeches may pass without a picture before one is attached
# automatically. The model is supposed to ask with an IMG line but it
# almost never does, so this is the real mechanism.
$MemeEvery = 1

# Being @-ed is a direct question, not ambient chatter, so it must not compete
# with the hourly conversation budget. Fresh means "within this many minutes,
# and after my own last line" - otherwise the same old mention would keep
# waking her up after she already answered it.
$mentionFreshMin = 3
# Separate ceiling so a spam wave cannot machine-gun her. Well above anything a
# normal group produces.
$mentionCapPerHour = 12
# How long an unanswered @ stays pending. Long enough that a missed poll does not
# lose it, short enough that a day-old mention does not resurrect.
$pendingMaxMin = 30
# Last-resort lines when the model refuses to answer a direct @ twice in a row.
# These must sound like her: short, no pleasantries, slightly cheeky. Built from
# code points because this file has to stay pure ASCII.
$mentionFallback = @(
  ([string][char]0x6B38),                                            # ei
  ([string][char]0x554A + [char]0xFF1F),                             # a?
  ([string][char]0x5728),                                            # zai
  ([string][char]0x548B + [char]0x4E86),                             # zenme le
  ([string][char]0x5E72 + [char]0x55EF + [char]0xFF1F),              # gan ma?
  ([string][char]0x53EB + [char]0x6211 + [char]0x5E72 + [char]0x55EF) # jiao wo gan ma
)

$budgetPath = Join-Path (Join-Path $workDir $dataDirName) 'budget.json'
if (Test-Path $budgetPath) {
  try {
    $bud = Get-Content $budgetPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($bud.groupMuted -eq $true) {
      $stamp = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
      Write-Output ('[' + $stamp + '] group spending cap reached (' + $bud.groupSpent + '/' + $bud.groupLimit + ') - silent')
      exit 0
    }
  } catch { }
}

# ---------------------------------------------------------------------------
# Load tunables from dafeiyu-tunables.json so the control panel can change them.
#
# They used to be param() defaults only, which meant editing this file to change
# how often she speaks. Now the JSON wins when it has a value; param() stays as
# the fallback so the script still works on its own.
#
# ASCII-only source.
# ---------------------------------------------------------------------------
$tunablesPath = Join-Path (Join-Path $workDir $dataDirName) 'dafeiyu-tunables.json'
    if (-not (Test-Path $tunablesPath)) {
      $tunablesPath = Join-Path (Join-Path $workDir $dataDirName) 'lanya-tunables.json'
    }
if (Test-Path $tunablesPath) {
  try {
    $tun = Get-Content $tunablesPath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($k in @('QuietMinutes','MinGapMinutes','QuietGapMinMinutes','QuietGapMaxMinutes',
                     'MaxPerHour','MaxPerHourAny','MaxPer24Hours','MaxQuietStartsPerDay',
                     'MaxTotal','WindowStart','WindowEnd','PollSeconds')) {
      if ($null -ne $tun.$k) { Set-Variable -Name $k -Value ([int]$tun.$k) -Scope 0 }
    }
    if ($tun.GroupId) { $GroupId = [string]$tun.GroupId }
  } catch { }
}



$root       = $PSScriptRoot
$promptPath = Join-Path $root 'groupwatch.prompt.txt'
$statePath  = Join-Path $root 'groupwatch.state.json'
# Speeches since the last picture, persisted so a restart does not
# reset the count and delay the first meme again.

# Per-QQ affinity. level "avoid" means she does not answer that person at all.
# The folder name is built from code points so this file stays ASCII-only.
$affinityPath = Join-Path (Join-Path $workDir $dataDirName) 'affinity.json'
$logPath    = Join-Path $root 'groupwatch.log'
$cfgPath    = Join-Path $root 'balance-alert.config.json'
$dsh        = (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'DSH\resources\runtime\cli\bin\dsh.cmd')
$selfId     = 'BOT_QQ'

function Get-MemeIfDue {
  # Pick a neutral, low-risk mood. Deliberately not angry or sad: attaching
  # those to an ordinary line can land badly.
  $moods = @('happy', 'cute', 'confused', 'cheer')
  $script:autoMood = $moods | Get-Random
  $base = Join-Path (Join-Path (Join-Path $workDir $dataDirName) 'memes') 'stolen'
  $d = Join-Path $base $script:autoMood
  if (-not (Test-Path $d)) { $d = Join-Path $base 'other' }
  if (-not (Test-Path $d)) { $d = $base }
  if (-not (Test-Path $d)) { return $null }
  $recent = @($state.recentMemes | Where-Object { $_ })
  $c = @(Get-ChildItem $d -File -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -match '^\.(jpg|jpeg|png|gif|webp)$' } |
    ForEach-Object { $_.FullName })
  $fresh = @($c | Where-Object { $recent -notcontains $_ })
  if ($fresh.Count -gt 0) { $c = $fresh }
  if (-not $c.Count) { return $null }
  return ($c | Get-Random)
}

function Write-Log([string]$m) {
  $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m
  Add-Content -Path $logPath -Value $line -Encoding UTF8
  Write-Output $line
}


# Nothing above may have set it (missing or renamed tunables file). Fall back
# to the shared config so the group id is never the empty string - NapCat
# rejects that with "Schema compilation error: Expected string" and the whole
# history read fails, which looks like "she stopped posting memes" from the
# outside.
if (-not $GroupId) {
  $cfgPath = Join-Path (Join-Path $workDir $dataDirName) 'lanya-config.json'
  if (Test-Path $cfgPath) {
    try {
      $cfg0 = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
      if ($cfg0.fanGroup) { $GroupId = [string]$cfg0.fanGroup }
      if (-not $GroupId -and $cfg0.fanGroupQQ) { $GroupId = [string]$cfg0.fanGroupQQ }
    } catch { }
  }
}
if (-not $GroupId) {
  $GroupId = [string](Get-ProjectConfValue 'fanGroup' '')
}
if (-not $GroupId) {
  Write-Log 'no group id configured; refusing to poll'
  return
}

function Read-AnyText([string]$path) {
  $b = [System.IO.File]::ReadAllBytes($path)
  if ($b.Length -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF) {
    return [System.Text.Encoding]::UTF8.GetString($b, 3, $b.Length - 3)
  }
  return [System.Text.Encoding]::UTF8.GetString($b)
}

$napcat = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json

# Meme library roots. Images are grouped by mood folder under each pack, so the
# model only has to name a mood and we pick a file from it.
#
# The first root is HER OWN collection: stickers she has taken from the group
# (stealmemes.py drops them in <dafeiyu>\memes\stolen). That is what makes mood
# "stolen" work, so she can throw a groupmate's own joke back at them.
$memeRoots = @(
  (Join-Path $workDir $dataDirName),
  '%USERPROFILE%\.dsh\profiles\desktop\node_modules\dsh-meme\memes\dafeiyu-001',
  '%USERPROFILE%\.dsh\meme-packs\official-001',
  '%USERPROFILE%\.dsh\meme-packs\whale-girl-emoji'
)

function Invoke-NapCat([string]$action, $bodyObj) {
  Add-Type -AssemblyName System.Net.Http
  $client = New-Object System.Net.Http.HttpClient
  $client.Timeout = [TimeSpan]::FromSeconds(30)
  $client.DefaultRequestHeaders.Authorization =
    New-Object System.Net.Http.Headers.AuthenticationHeaderValue('Bearer', [string]$napcat.napcatToken)
  # -Depth matters here. ConvertTo-Json defaults to 2 levels, but an image segment
  # nests four: object -> array -> {type,data} -> {file}. Without -Depth the "file"
  # key was silently dropped and NapCat answered "file message is missing a parameter", so every meme
  # she tried to send failed while her text went through fine.
  $json = $bodyObj | ConvertTo-Json -Compress -Depth 10
  $content = New-Object System.Net.Http.StringContent($json, [System.Text.Encoding]::UTF8, 'application/json')
  $url = ([string]$napcat.napcatHttp).TrimEnd('/') + '/' + $action
  # When NapCat is dead (QQ closed, injection lost) PostAsync never yields a
  # response, and .Result hands back $null instead of throwing. The old code
  # then called $resp.Content.* and the loop died with
  # "cannot call a method on a null-valued expression" - once a minute, with
  # nothing in the log to say the bridge was down. Say it plainly instead.
  $resp = $client.PostAsync($url, $content).Result
  if ($null -eq $resp -or $null -eq $resp.Content) {
    throw ('napcat-unreachable: ' + $action)
  }
  return [System.Text.Encoding]::UTF8.GetString($resp.Content.ReadAsByteArrayAsync().Result)
}

# Ask the headless profile one question; returns its stdout text.
#
# Read the child's stdout as RAW BYTES and decode them ourselves. Reading it as
# text (ReadToEndAsync) used the console codepage and turned a UTF-8 reply into
# mojibake on its way to QQ, so the group saw garbage instead of Chinese.
function Ask-Agent([string]$task) {
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = 'cmd.exe'
  $psi.Arguments = '/c ""' + $dsh + '" --profile headless -"'
  $psi.UseShellExecute = $false
  $psi.RedirectStandardInput = $true
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.CreateNoWindow = $true
  $psi.WorkingDirectory = $root

  $p = [System.Diagnostics.Process]::Start($psi)
  $ms = New-Object System.IO.MemoryStream
  $copyTask = $p.StandardOutput.BaseStream.CopyToAsync($ms)
  $errTask = $p.StandardError.ReadToEndAsync()
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($task)
  $p.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
  $p.StandardInput.BaseStream.Flush()
  $p.StandardInput.Close()
  if (-not $p.WaitForExit(180000)) { try { $p.Kill() } catch { } }
  try { $copyTask.Wait(10000) | Out-Null } catch { }
  $errTask.Wait(3000) | Out-Null
  $raw = $ms.ToArray()
  if ($raw.Length -eq 0) { return '' }
  # UTF-16LE with BOM (this is what the headless profile emits)
  if ($raw.Length -ge 2 -and $raw[0] -eq 0xFF -and $raw[1] -eq 0xFE) {
    return [System.Text.Encoding]::Unicode.GetString($raw, 2, $raw.Length - 2)
  }
  # UTF-8 with BOM
  if ($raw.Length -ge 3 -and $raw[0] -eq 0xEF -and $raw[1] -eq 0xBB -and $raw[2] -eq 0xBF) {
    return [System.Text.Encoding]::UTF8.GetString($raw, 3, $raw.Length - 3)
  }
  # Plain UTF-8
  return [System.Text.Encoding]::UTF8.GetString($raw)
}

$state = @{ lastSendAt = $null; sendTimes = @(); sendTimesQuiet = @(); lastMsgTime = 0; nextQuietAt = $null; quietDay = $null; quietDayCount = 0 }
if (Test-Path $statePath) {
  try { $state = Get-Content $statePath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
}
# Speeches since the last picture. Persisted so a restart does not
# reset it and delay the next meme.
if ($null -eq $state.speakSinceMeme) {
  $state | Add-Member -NotePropertyName speakSinceMeme -NotePropertyValue 0 -Force
}
$speakSinceMeme = [int]$state.speakSinceMeme
if ($null -eq $state.sendTimes) { $state | Add-Member -NotePropertyName sendTimes -NotePropertyValue @() -Force }
if ($null -eq $state.sendTimesQuiet) { $state | Add-Member -NotePropertyName sendTimesQuiet -NotePropertyValue @() -Force }

function Save-State() {
  # Both lists are pruned to the same 2-hour window. sendTimes = everything she
  # sent (runaway guard); sendTimesQuiet = only the messages where SHE started a
  # topic, which is the budget that actually needs limiting.
  $keep = @()
  if ($state.sendTimes) {
    foreach ($t in $state.sendTimes) {
      if (((Get-Date) - [DateTime]::Parse([string]$t)).TotalHours -lt 2) { $keep += $t }
    }
  }
  $keepQuiet = @()
  if ($state.sendTimesQuiet) {
    foreach ($t in $state.sendTimesQuiet) {
      if (((Get-Date) - [DateTime]::Parse([string]$t)).TotalHours -lt 2) { $keepQuiet += $t }
    }
  }
  $obj = [pscustomobject]@{
    lastSendAt     = $state.lastSendAt
    sendTimes      = $keep
    sendTimesQuiet = $keepQuiet
    lastMsgTime    = $state.lastMsgTime
    recentMemes    = @($state.recentMemes | Where-Object { $_ })
    speakSinceMeme = [int]($state.speakSinceMeme)
    nextQuietAt    = $state.nextQuietAt
    # Daily self-start counter must persist, otherwise a restart resets it and the
    # ceiling it exists to enforce silently disappears.
    quietDay       = $state.quietDay
    quietDayCount  = $state.quietDayCount
  }
  [System.IO.File]::WriteAllText($statePath, ($obj | ConvertTo-Json -Depth 5 -Compress), (New-Object System.Text.UTF8Encoding($false)))
  $state.sendTimes = $keep
  $state.sendTimesQuiet = $keepQuiet
}

Write-Log ('groupwatch start: group=' + $GroupId + ' poll=' + $PollSeconds + 's quietAfter=' + $QuietMinutes + 'min minGap=' + $MinGapMinutes + 'min quietGap=' + $QuietGapMinMinutes + '-' + $QuietGapMaxMinutes + 'min/day=' + $MaxQuietStartsPerDay + ' maxPerHour=' + $MaxPerHour + ' window=' + $WindowStart + '-' + $WindowEnd + ' dryRun=' + [bool]$DryRun)

# Single instance, enforced by an exclusive file handle. The DSH-GroupWatch task
# re-launches this script on a schedule and cannot tell a live watcher from a
# dead one, so two of them end up posting the same line twice - and only one
# process ever updates the state file, so the rate limits get bypassed.
# The lock dies with the process; a stale lock costs nothing.
$lockPath = Join-Path $root 'groupwatch.lock'
try {
  $script:lockStream = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
} catch {
  Write-Log 'another groupwatch already holds the lock - exiting'
  return
}

$stopFlag = Join-Path $root 'GROUPWATCH_OFF.flag'
# Throttle for the "NapCat is down" line. Without this the loop writes the same
# sentence every poll and the real cause scrolls away.
$script:napcatDownLoggedAt = [DateTime]::MinValue
while ($true) {
  if (Test-Path $stopFlag) { Write-Log 'stop flag present - exiting'; break }
  $now = Get-Date
  $hour = $now.Hour

  # Quiet hours are a soft flag now, not an early exit.
  #
  # The window used to be checked before the history was fetched, so at
  # night she could not see an @-mention at all and stayed silent. The rule
  # is narrower: do not START conversations at night, but do answer someone
  # who calls her by name. So keep polling and decide once we know.
  $outsideWindow = ($hour -lt $WindowStart -or $hour -ge $WindowEnd)

  try {
    $hj = (Invoke-NapCat 'get_group_msg_history' @{ group_id = $GroupId; count = 30 }) | ConvertFrom-Json
    if ($hj.retcode -ne 0) { Write-Log ('history failed: ' + $hj.message); Start-Sleep -Seconds $PollSeconds; continue }

    $lines = @()
    $lastTime = [DateTime]::MinValue
    $lastOtherTime = [DateTime]::MinValue
    $lastOtherId = ''
    $newCount = 0
    # For the affinity gate: who spoke recently, and whether anyone other than the
    # current speaker @-ed her.
    $recentSpeakers = New-Object System.Collections.ArrayList
    # message_id -> sender QQ. Needed because a reply segment only carries the id
    # of the message being answered, and "did they answer ME" is the difference
    # between a question and background chatter.
    $msgOwner = @{}
    $msgNick = @{}
    foreach ($m0 in $hj.data.messages) {
      $mid0 = [string]$m0.message_id
      if ($mid0) {
        $msgOwner[$mid0] = [string]$m0.sender.user_id
        $disp = if ($m0.sender.card) { [string]$m0.sender.card } else { [string]$m0.sender.nickname }
        $msgNick[$mid0] = $disp
      }
    }
    # (note omitted: this file must stay ASCII-only)
    $selfNames = @()
    if ($dataDirName) { $selfNames += [string]$dataDirName }
    try {
      $qp = Join-Path $root 'qq-profile.json'
      if (Test-Path $qp) {
        $qj = Get-Content $qp -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($qj.nickname) { $selfNames += [string]$qj.nickname }
      }
    } catch { }
    $repliedToMeHere = $false
    $addressedByOther = $false
    # An @ she has not answered yet is remembered across polls. Without this the
    # mention "expires" three minutes after it was sent and she can never come
    # back to it - which is exactly how an unanswered @ looks from the outside.
    $pendingQQ = ''
    $pendingAt = [DateTime]::MinValue
    if ($state.pendingMention) {
      try {
        $pendingQQ = [string]$state.pendingMention.qq
        $pendingAt = [DateTime]::Parse([string]$state.pendingMention.at)
      } catch { $pendingQQ = ''; $pendingAt = [DateTime]::MinValue }
    }
    # Newest time anyone @-ed her, so the trigger decision can tell a fresh
    # mention from one buried in the 30-message window.
    $lastMentionTime = [DateTime]::MinValue
    $lastMentionQQ = ''
    foreach ($m in $hj.data.messages) {
      $t = ''
      $mentionsMe = $false
      $replyToMe = $false
      $replyHint = ''
      $rawText = ''
      if ($m.message -is [array]) {
        $parts = @()
        foreach ($seg in $m.message) {
          if ($seg.type -eq 'text') {
            $parts += $seg.data.text
            $rawText += [string]$seg.data.text
          }
          elseif ($seg.type -eq 'image') { $parts += '[' + [char]0x56FE + [char]0x7247 + ']' }
          elseif ($seg.type -eq 'at') {
            $aq = [string]$seg.data.qq
            if ($aq -eq $selfId) {
              $parts += '@' + [char]0x4F60
              $mentionsMe = $true
            }
            elseif ($aq -eq 'all') { $parts += '@' + [char]0x5168 + [char]0x4F53 }
            else {
              $an = if ($msgNick.ContainsKey([string]$m.message_id)) { '' } else { '' }
              $parts += '@' + $aq
            }
          }
          elseif ($seg.type -eq 'reply') {
            # A reply segment carries only the id of the quoted message. Resolve it
            # through the map built above; if it is one of HER messages, whoever
            # sent this is answering her directly.
            $rid = [string]$seg.data.id
            if ($msgOwner.ContainsKey($rid) -and $msgOwner[$rid] -eq $selfId) {
              $replyToMe = $true
              $parts += '[' + [char]0x56DE + [char]0x4F60 + ']'
            }
            elseif ($msgOwner.ContainsKey($rid)) {
              $who2 = $msgNick[$rid]
              $parts += '[' + [char]0x56DE + $who2 + ']'
            }
            else {
              $parts += '[' + [char]0x5F15 + [char]0x7528 + ']'
            }
          }
        }
        $t = ($parts -join '')
      } else {
        $t = [string]$m.message
        $rawText = $t
      }
      # Name dropped in plain text counts as talking to her.
      if (-not $mentionsMe -and -not $replyToMe -and $rawText -and $selfNames.Count -gt 0) {
        foreach ($nm in $selfNames) {
          if ($nm -and $rawText.Contains($nm)) {
            $mentionsMe = $true
            break
          }
        }
      }
      $t = $t.Trim()
      if ($t -eq '') { continue }
      $mt = [DateTimeOffset]::FromUnixTimeSeconds([long]$m.time).LocalDateTime
      if ($mt -gt $lastTime) { $lastTime = $mt }
      $who = [string]$m.sender.user_id
      if ($who -ne $selfId) {
        $lastOtherTime = $mt
        $lastOtherId = $who
        # Keep only the tail so an old message from someone else does not keep the
        # gate open forever.
        [void]$recentSpeakers.Add($who)
        while ($recentSpeakers.Count -gt 6) { $recentSpeakers.RemoveAt(0) }
      }
      if (($mentionsMe -or $replyToMe) -and $who -ne $selfId) {
        $addressedByOther = $true
        if ($mt -gt $lastMentionTime) {
          $lastMentionTime = $mt
          $lastMentionQQ = $who
        }
      }
      if ($who -eq $selfId) {
        # Her own line. Use the real directory name, not a hardcoded one.
        $me = [string]$dataDirName
        $lines += ([string][char]0x6211 + '(' + $me + ')' + [char]0x4E4B + [char]0x524D + [char]0x8BF4 + ': ' + $t)
      }
      else {
        # QQ number included so two people sharing a nickname stay distinct, and
        # so the model can keep its own per-person memory straight.
        $disp = if ($m.sender.card) { [string]$m.sender.card } else { [string]$m.sender.nickname }
        $role = [string]$m.sender.role
        $tag = ''
        if ($role -eq 'admin' -or $role -eq 'owner') { $tag = '[' + [char]0x7FA4 + [char]0x4E3B + ']' }
        $lines += ($disp + '(' + $who + ')' + $tag + ': ' + $t)
      }
    }
    if ($lines.Count -eq 0) { Start-Sleep -Seconds $PollSeconds; continue }

    # Per-person dossiers. Without this she treats a regular like a stranger and
    # a newcomer like an old friend - the dossiers existed but were never fed
    # into the group prompt.
    #
    # Only the last few distinct speakers, and only the head of each file: the
    # point is orientation, not a data dump.
    $peopleText = ''
    try {
      $peopleDir = Join-Path (Join-Path $workDir $dataDirName) 'people'
      if (Test-Path $peopleDir) {
        $uniq = @($recentSpeakers | Select-Object -Unique)
        $take = @($uniq | Select-Object -Last 6)
        $blocks = @()
        foreach ($q in $take) {
          $fp = Join-Path $peopleDir ([string]$q + '.md')
          $hasFile = Test-Path $fp
          $body = ''
          if ($hasFile) { $body = Get-Content $fp -Raw -Encoding UTF8 }
          # Name first: needed both for a dossier block and for the stranger note.
          $nm = ''
          foreach ($mid in $msgNick.Keys) {
            if ($msgOwner[$mid] -eq [string]$q) { $nm = $msgNick[$mid]; break }
          }
          if (-not $nm) { $nm = [string]$q }
          if (-not $body) {
            # No dossier yet. Say so, otherwise she guesses and either treats a
            # stranger like an old friend or a regular like a stranger.
            $blocks += ('- ' + $nm + ' (' + $q + ')' + "`n" +
              '  ' + [char]0x6CA1 + [char]0x6253 + [char]0x8FC7 + [char]0x4EA4 + [char]0x9053 + ',' + [char]0x4E0D + [char]0x8BA4 + [char]0x8BC6 + '.' +
              '  ' + [char]0x5BF9 + [char]0x4ED6 + [char]0x5C31 + [char]0x50CF + [char]0x5BF9 + [char]0x7FA4 + [char]0x91CC + [char]0x5176 + [char]0x4ED6 + [char]0x4EBA + [char]0x4E00 + [char]0x6837 + [char]0xFF0C + [char]0x5C11 + [char]0x70B9 + [char]0x719F + [char]0x7EDC + '.')
            continue
          }
          $keep = @($body -split "`r?`n" |
            Where-Object { $_.Trim() } |
            Select-Object -First 12)
          $excerpt = ($keep -join "`n").Trim()
          if ($excerpt.Length -gt 600) { $excerpt = $excerpt.Substring(0, 600) + '...' }
          # The display name as it appears in the transcript, so the two line up.
          $nm = ''
          foreach ($mid in $msgNick.Keys) {
            if ($msgOwner[$mid] -eq [string]$q) { $nm = $msgNick[$mid]; break }
          }
          if (-not $nm) { $nm = [string]$q }
          $blocks += ('- ' + $nm + ' (' + $q + ')' + "`n" + $excerpt)
        }
        if ($blocks.Count -gt 0) { $peopleText = ($blocks -join "`n`n") }
      }
    }
    catch { }

    # Affinity gate .
    #
    # She should not engage with people on the avoid list - but ONLY when that
    # person is the whole conversation. The first version skipped whenever the most
    # recent message happened to be theirs, so one stray message at the end of a
    # lively exchange muted her for everyone else too. The owner's rule: if other
    # people are talking, or someone else has @-ed her, that person does not matter.
    #
    # So: skip only when every recent message came from the avoid list AND nobody
    # else has @-ed her.
    $avoidSet = @{}
    try {
      if (Test-Path $affinityPath) {
        $affAll = Get-Content $affinityPath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($prop in $affAll.PSObject.Properties) {
          if ($prop.Value -ne $null -and [string]$prop.Value.level -eq 'avoid') {
            $avoidSet[$prop.Name] = $true
          }
        }
      }
    } catch { }

    if ($avoidSet.Count -gt 0 -and $recentSpeakers.Count -gt 0) {
      $othersPresent = @($recentSpeakers | Where-Object { -not $avoidSet.ContainsKey($_) }).Count -gt 0
      if (-not $othersPresent -and -not $addressedByOther) {
        Write-Log ('affinity: only avoided people are talking (' + (($recentSpeakers) -join ',') + '), skip')
        if (-not $Once) { Start-Sleep -Seconds $PollSeconds; continue } else { break }
      }
      elseif ($othersPresent) {
        Write-Log ('affinity: avoided person present but others are talking too, proceeding')
      }
    }

    $idleMin = if ($lastTime -gt [DateTime]::MinValue) { ($now - $lastTime).TotalMinutes } else { 999 }
    $sinceMine = 999
    if ($state.lastSendAt) { $sinceMine = ($now - [DateTime]::Parse([string]$state.lastSendAt)).TotalMinutes }

    # Determine WHY she is being woken BEFORE applying any cap: the two cases
    # deserve completely different budgets.
    $triggerKey = ''
    # A fresh @-mention outranks everything: someone is asking her directly.
    $freshMention = $false
    if ($lastMentionTime -ne [DateTime]::MinValue) {
      $mentionAge = ($now - $lastMentionTime).TotalMinutes
      $lastSelf = $null
      if ($state.lastSendAt) {
        try { $lastSelf = [DateTime]::Parse([string]$state.lastSendAt) } catch { }
      }
      # Fresh = recent, and not already answered (the mention came after her last line).
      if ($mentionAge -le $mentionFreshMin -and (-not $lastSelf -or $lastMentionTime -gt $lastSelf)) {
        $freshMention = $true
      }
    }
    # A still-unanswered @ from an earlier poll keeps the mention trigger alive.
    if (-not $freshMention -and $pendingQQ -and $pendingAt -ne [DateTime]::MinValue) {
      $pendingAge = ($now - $pendingAt).TotalMinutes
      if ($pendingAge -le $pendingMaxMin) {
        $freshMention = $true
        if ($lastMentionTime -eq [DateTime]::MinValue) { $lastMentionTime = $pendingAt }
        if (-not $lastMentionQQ) { $lastMentionQQ = $pendingQQ }
        Write-Log ('pending mention from ' + [math]::Round($pendingAge,1) + 'min ago still unanswered - retrying')
      }
      else {
        # Give up after a while so one stale @ does not mute the trigger forever.
        $state.pendingMention = $null
        Write-Log ('pending mention expired after ' + [math]::Round($pendingAge,0) + 'min')
      }
    }
    if ($freshMention) { $triggerKey = 'mention' }
    elseif ($idleMin -ge $QuietMinutes) { $triggerKey = 'quiet' }
    else { $triggerKey = 'active' }

    # Night: only a direct @ gets through. Everything else waits for the window.
    if ($outsideWindow -and $triggerKey -ne 'mention') {
      if ($Once) { Write-Log ('outside window (' + $hour + 'h) and not addressed - exit'); break }
      Write-Log ('outside window (' + $hour + 'h) and not addressed - quiet for 5min')
      Start-Sleep -Seconds 300
      continue
    }
    if ($outsideWindow -and $triggerKey -eq 'mention') {
      Write-Log ('outside window (' + $hour + 'h) but addressed - answering anyway')
    }

    # Remember the unanswered @ so a failed attempt does not lose it.
    if ($triggerKey -eq 'mention') {
      $pendQQ = if ($lastMentionQQ) { $lastMentionQQ } else { $pendingQQ }
      $pendAt = if ($lastMentionTime -ne [DateTime]::MinValue) { $lastMentionTime } else { $now }
      $state | Add-Member -NotePropertyName pendingMention -NotePropertyValue @{
        qq = [string]$pendQQ
        at = $pendAt.ToString('o')
      } -Force
    }

    # Rate limits, counted separately per trigger type.
    #
    # The old design had ONE counter for everything, so joining a live
    # conversation burned the same budget as cold-starting a topic. She would
    # chat normally, hit 40/40, and then be muted for the rest of the window
    # even while people were actively talking to her - the log filled up with
    # "rate cap reached (40/40 per 2h window)". Starting topics unprompted DOES
    # need a ceiling (that is what stops spam in a quiet group); answering
    # people who are already talking should not consume it.
    $quietCount = @($state.sendTimesQuiet | Where-Object { $_ }).Count
    $totalCount = @($state.sendTimes | Where-Object { $_ }).Count

    # Hard daily ceiling on SELF-STARTED topics . Replies to someone addressing her are governed
    # separately and do not consume this - the owner's complaint was specifically
    # about her opening conversations on her own, not about answering people.
    if ($triggerKey -eq 'quiet') {
      $todayKey2 = (Get-Date).ToString('yyyy-MM-dd')
      $startedToday = 0
      if ($state.quietDay -eq $todayKey2) { $startedToday = [int]$state.quietDayCount }
      if ($startedToday -ge $MaxQuietStartsPerDay) {
        Write-Log ('daily self-start cap reached (' + $startedToday + '/' + $MaxQuietStartsPerDay + ' today) - still may reply if addressed')
        if (-not $Once) { Start-Sleep -Seconds $PollSeconds; continue } else { break }
      }
    }

    if ($triggerKey -eq 'quiet' -and $quietCount -ge $MaxPerHour) {
      Write-Log ('quiet cap reached (' + $quietCount + '/' + $MaxPerHour + ' self-started per 2h) - but she may still join ongoing chat')
      if (-not $Once) { Start-Sleep -Seconds $PollSeconds; continue } else { break }
    }
    # Even-spread gate : the daily 30 must not land in one
    # stretch. Hard ceiling on any single clock hour; when it is full she goes
    # quiet until the hour rolls over and never catches up afterwards.
    $hourAgo = (Get-Date).AddHours(-1).ToString('o')
    $hourCount = @($state.sendTimes | Where-Object { $_ -and ([string]$_ -ge $hourAgo) }).Count
    if ($freshMention) {
      # Named directly: the conversation budget does not apply. Only the
      # anti-spam ceiling does.
      if ($hourCount -ge $mentionCapPerHour) {
        Write-Log ('mention but spam ceiling reached (' + $hourCount + '/' + $mentionCapPerHour + ' per hour) - skip')
        if (-not $Once) { Start-Sleep -Seconds $PollSeconds; continue } else { break }
      }
      else {
        Write-Log ('mention detected (' + [math]::Round(($now - $lastMentionTime).TotalMinutes,1) + 'min ago) - bypassing hourly cap (' + $hourCount + '/' + $MaxPerHourAny + ')')
      }
    }
    elseif ($hourCount -ge $MaxPerHourAny) {
      Write-Log ('hourly cap reached (' + $hourCount + '/' + $MaxPerHourAny + ' per hour) - silent until next hour')
      if (-not $Once) { Start-Sleep -Seconds $PollSeconds; continue } else { break }
    }

    # Soft 24h target : not a hard cap. Above target the chance
    # of speaking drops smoothly; below target she is a bit more willing to start
    # something. Keeps the daily volume near MaxPer24Hours without freezing a
    # live exchange.
    $dayAgo = (Get-Date).AddHours(-24).ToString('o')
    $dayCount = @($state.sendTimes | Where-Object { $_ -and ([string]$_ -ge $dayAgo) }).Count
    if ($dayCount -ge $MaxPer24Hours -and -not $freshMention) {
      # 
      # speaking drops by 3 percentage points per extra message, and never
      # drops below a 10% chance of still speaking (skip is capped at 90%).
      $over = $dayCount - $MaxPer24Hours
      $skipPct = [math]::Min(90, $over * 3)
      if ((Get-Random -Minimum 1 -Maximum 101) -le $skipPct) {
        Write-Log ('24h over target (' + $dayCount + '/' + $MaxPer24Hours + '), skip this turn (' + $skipPct + '%)')
        if (-not $Once) { Start-Sleep -Seconds $PollSeconds; continue } else { break }
      }
      else {
        Write-Log ('24h over target (' + $dayCount + '/' + $MaxPer24Hours + '), passed the ' + $skipPct + '% roll')
      }
    }
    elseif ($triggerKey -eq 'quiet' -and $dayCount -lt ($MaxPer24Hours / 2)) {
      Write-Log ('24h well under target (' + $dayCount + '/' + $MaxPer24Hours + ') - more willing to start')
    }

    # Runaway guard only; deliberately far above what a human would send.
    # A direct @ still gets through: it is a question, not a volume problem.
    if ($totalCount -ge $MaxTotal -and $triggerKey -ne 'mention') {
      Write-Log ('hard stop (' + $totalCount + '/' + $MaxTotal + ' total per 2h), skip')
      if (-not $Once) { Start-Sleep -Seconds $PollSeconds; continue } else { break }
    }

    # Spacing depends on what she would be doing.
    #   joining an ongoing conversation -> a short breath so she does not talk over
    #                                      people, but no real throttle
    #   starting a topic on her own     -> the time drawn after her previous
    #                                      self-start (nextQuietAt, ~1h on average)
    if ($triggerKey -eq 'quiet') {
      # Owner rule (2026-10-17): if her last self-started topic got no reply,
      # drop that topic instead of going on about it. One repeat, then silence
      # on it - push the next attempt out by another full gap.
      if ($state.lastSendAt -and $lastOtherTime -ne [DateTime]::MinValue) {
        $lastSelf = $null
        try { $lastSelf = [datetime]([string]$state.lastSendAt) } catch { $lastSelf = $null }
        if ($lastSelf -and $lastOtherTime -lt $lastSelf) {
          Write-Log 'previous self-start got no reply - dropping that topic, next attempt pushed out'
          $state.nextQuietAt = (Get-Date).AddMinutes($QuietGapMaxMinutes).ToString('o')
          if (-not $Once) { Start-Sleep -Seconds $PollSeconds; continue } else { break }
        }
      }
      $nextQuiet = $null
      if ($state.nextQuietAt) {
        try { $nextQuiet = [datetime]$state.nextQuietAt } catch { $nextQuiet = $null }
      }
      # Fallback for state written before nextQuietAt existed.
      if ($null -eq $nextQuiet) {
        $nextQuiet = (Get-Date).AddMinutes($QuietGapMinMinutes)
      }
      if ((Get-Date) -lt $nextQuiet) {
        Write-Log ('quiet cooldown: next self-start at ' + $nextQuiet.ToString('HH:mm') + ', skip')
        if (-not $Once) { Start-Sleep -Seconds $PollSeconds; continue } else { break }
      }
    }
    elseif ($triggerKey -ne 'mention' -and $sinceMine -lt $MinGapMinutes) {
      Write-Log ('min gap not met (' + [math]::Round($sinceMine,1) + 'min), skip')
      if (-not $Once) { Start-Sleep -Seconds $PollSeconds; continue } else { break }
    }

    $triggerText = if ($triggerKey -eq 'mention') {
      'SOMEONE @-ED YOU ' + [math]::Round(($now - $lastMentionTime).TotalMinutes,1) +
      ' minutes ago and is waiting for your answer. Reply to them. Do not go SILENT, ' +
      'do not change the subject, do not answer someone else - answer the person who @-ed you.'
    }
    elseif ($triggerKey -eq 'quiet') {
      'group has been silent for ' + [math]::Round($idleMin,0) + ' minutes - you may start a topic'
    }
    else {
      'someone is chatting right now - you may join if it fits'
    }

    # Her own recent lines, fed back into the prompt. This decision session is
    # started fresh every poll, so without this she has no idea what she already
    # said and repeats herself - she made the same "nine lives" joke twice,
    # six minutes apart.
    $myRecent = ''
    try {
      $gwLog = Join-Path $root 'groupwatch.log'
      if (Test-Path $gwLog) {
        $mine = Get-Content $gwLog -Encoding UTF8 |
          Select-String -Pattern '\] reply: ' |
          Select-Object -Last 6 |
          ForEach-Object { '  ' + ($_.Line -replace '^.*\] reply: ', '') }
        if ($mine) { $myRecent = ($mine -join "`n") }
      }
    } catch { }

    # Regenerate the mood list from what is actually on disk. The prompt used to
  # carry a hardcoded list and it drifted: it offered tags like "sigh", "see",
  # "baka" that no folder had, so half the IMG requests resolved to nothing.
  $tagLines = @()
  $tagDesc = @{
    'happy' = '[char]0x5f00 + [char]0x5fc3 + [char]0x3001 + [char]0x5356 + [char]0x840c'; 'sad' = '[char]0x96be + [char]0x8fc7 + [char]0x3001 + [char]0x65e0 + [char]0x8bed';
    'angry' = '[char]0x751f + [char]0x6c14 + [char]0x3001 + [char]0x5acc + [char]0x5f03'; 'speechless' = '[char]0x65e0 + [char]0x8bed';
    'cute' = '[char]0x53ef + [char]0x7231'; 'shy' = '[char]0x5bb3 + [char]0x7f9e';
    'surprise' = '[char]0x9707 + [char]0x60ca'; 'confused' = '[char]0x56f0 + [char]0x60d1';
    'refuse' = '[char]0x62d2 + [char]0x7edd'; 'cheer' = '[char]0x52a0 + [char]0x6cb9';
    'sleepy' = '[char]0x56f0'; 'hungry' = '[char]0x997f';
    'money' = '[char]0x8981 + [char]0x94b1'; 'other' = '[char]0x5176 + [char]0x5b83';
    'work' = '[char]0x52a0 + [char]0x73ed'; 'lazy' = '[char]0x6446 + [char]0x70c2';
    'pray' = '[char]0x7948 + [char]0x7977'; 'cool' = '[char]0x9177'
  }
  $allTags = @('happy','sad','angry','speechless','cute','shy','surprise',
               'confused','refuse','cheer','sleepy','hungry','money','other',
               'work','lazy','pray','cool')
  foreach ($tg in $allTags) {
    $cnt = 0
    foreach ($pr in $memeRoots) {
      $dd = Join-Path (Join-Path $pr 'memes') $tg
      if (Test-Path $dd) {
        $cnt += @(Get-ChildItem $dd -File -Recurse -ErrorAction SilentlyContinue |
          Where-Object { $_.Extension -match '^\.(jpg|jpeg|png|gif|webp)$' }).Count
      }
    }
    if ($cnt -gt 0) {
      $tagLines += ('- ' + $tg + ' ' + $tagDesc[$tg] + '[char]0xff08' + $cnt + ' [char]0x5f20 + [char]0xff09')
    }
  }
  $stCount = 0
  $stDir = Join-Path (Join-Path (Join-Path (Split-Path -Parent $root) $dataDirName) 'memes') 'stolen'
  if (Test-Path $stDir) {
    $stCount = @(Get-ChildItem $stDir -File -Recurse -ErrorAction SilentlyContinue |
      Where-Object { $_.Extension -match '^\.(jpg|jpeg|png|gif|webp)$' }).Count
  }
  $memeTags = ($tagLines -join "`n")
  if ($stCount -gt 0) {
    $memeTags += ("`n`n**[char]0x8fd8 + [char]0x6709 + [char]0x4e00 + [char]0x4e2a + [char]0x4f60 + [char]0x81ea + [char]0x5df1 + [char]0x7684 + [char]0x6536 + [char]0x85cf + [char]0x5939**[char]0xff1a`n`n- **stolen [char]0x7fa4 + [char]0x53cb + [char]0x8868 + [char]0x60c5 + [char]0x5305**[char]0xff08" + $stCount + " [char]0x5f20 + [char]0xff09")
  }

  $tpl = Read-AnyText $promptPath
    $task = $tpl.Replace('{{CONTEXT}}', ($lines -join "`n")).
      Replace('{{IDLE}}', [string][math]::Round($idleMin,1)).
      Replace('{{SINCE}}', [string][math]::Round($sinceMine,1)).
      Replace('{{MINE}}', $myRecent).
      Replace('{{TRIGGER}}', $triggerText).
  Replace('{{MEME_TAGS}}', $memeTags).
  Replace('{{PEOPLE}}', $peopleText).
  Replace('{{CHAR_NAME}}', $dataDirName)

    Write-Log ('asking agent: trigger=' + $triggerKey + ' idle=' + [math]::Round($idleMin,1) + 'min sinceMine=' + [math]::Round($sinceMine,1) + 'min sent2h=' + $perHour)
    $answer = (Ask-Agent $task).Trim()

    # NOTE: wrap in @() - a single-line answer makes -split return a bare string,
    # and then $rows[0] is a [char] (no .Trim()), which threw here before.
    $rows = @($answer -split "`r?`n" | Where-Object { $_.Trim() -ne '' })
    $verdict = if ($rows.Count -gt 0) { $rows[0].Trim().ToUpper() } else { '' }
    Write-Log ('verdict=' + $verdict)

    # A direct @ is a question. Silence is not an acceptable answer to it, so ask
    # the model once more with the instruction spelled out, and if it still
    # refuses, reply anyway with something minimal.
    #
    # The prompt already forbade this (checked: {{TRIGGER}} says "Do not go
    # SILENT"), and the model still returned SILENT - which is why people were
    # being ignored after being @-ed.
    if ($triggerKey -eq 'mention' -and $verdict -notlike 'SPEAK*') {
      Write-Log ('mention but agent said [' + $verdict + '] - retrying with an explicit order')
      $retryTask = $task + "`n`n" +
        'REMINDER: you were @-ed directly. You must answer. Output SPEAK and one short line. SILENT is not allowed here.'
      $answer = (Ask-Agent $retryTask).Trim()
      $rows = @($answer -split "`r?`n" | Where-Object { $_.Trim() -ne '' })
      $verdict = if ($rows.Count -gt 0) { $rows[0].Trim().ToUpper() } else { '' }
      Write-Log ('retry verdict=' + $verdict)
      if ($verdict -notlike 'SPEAK*') {
        # Still nothing usable. Say something short rather than leaving them on read.
        $rows = @('SPEAK', ($mentionFallback | Get-Random))
        $verdict = 'SPEAK'
        Write-Log 'retry failed - using a fallback line'
      }
    }

    if ($verdict -notlike 'SPEAK*') {
      Write-Log 'SILENT - staying quiet'
      if (-not $Once) { Start-Sleep -Seconds $PollSeconds; continue } else { break }
    }

    $reply = if ($rows.Count -ge 2) { $rows[1].Trim() } else { ($rows[0] -replace '^SPEAK[:\s]*', '').Trim() }
    if ($reply -eq '' -and $triggerKey -eq 'mention') {
      $reply = ($mentionFallback | Get-Random)
      Write-Log ('SPEAK with an empty line on a mention - using a fallback line')
    }
    if ($reply -eq '') { Write-Log 'SPEAK but empty line; skip'; if (-not $Once) { Start-Sleep -Seconds $PollSeconds; continue } else { break } }

    # Guard: never post a stage direction.
    #
    # She started emitting parentheticals instead of just not replying.
    # as her way of saying she would not join in. That is a note to herself, not
    # something a person says out loud, and it reads bizarrely in a group. The
    # prompt now forbids it; this is the belt-and-braces check.
    #
    # The full-width brackets are built from code points on purpose: PowerShell 5.1
    # reads this file as ANSI/GBK, so literal U+FF08/U+FF09/U+3010/U+3011 bytes
    # decode to the wrong characters and the pattern would silently never match.
    $open  = '[' + ([char]0xFF08) + '(' + '\[' + ([char]0x3010) + ']'
    $close = '[' + ([char]0xFF09) + ')' + '\]' + ([char]0x3011) + ']'
    $stageRe = '^\s*' + $open + '.*' + $close + '\s*$'
    if ($reply -match $stageRe) {
      Write-Log ('suppressed stage-direction reply: ' + $reply)
      if ($triggerKey -eq 'mention') {
        $reply = ($mentionFallback | Get-Random)
        Write-Log 'mention + stage direction - using a fallback line'
      }
      else {
        if (-not $Once) { Start-Sleep -Seconds $PollSeconds; continue } else { break }
      }
    }

    # Optional third line "IMG <mood>" asks for a meme picked at random from that
    # mood folder. The model only chooses the mood; the file choice is random here
    # so the same shot does not repeat.
    $imgPath = $null
    if ($rows.Count -ge 3 -and $rows[2].Trim() -and
        $rows[2] -notmatch '^\s*IMG\s+([a-z0-9_-]+)\s*$') {
      Write-Log ('third line present but not a valid IMG directive: ' + $rows[2].Trim().Substring(0, [Math]::Min(40, $rows[2].Trim().Length)))
    }
    if ($rows.Count -ge 3 -and $rows[2] -match '^\s*IMG\s+([a-z0-9_-]+)\s*$') {
      $mood = $Matches[1].ToLower()
      # The prompt lists the real folder names, but the model invents its own
      # sometimes ("see", "baka", "sigh") and then nothing matches and she sends
      # no picture at all. Map the near misses onto a folder that exists.
      $moodAlias = @{
        'see' = 'surprise'; 'look' = 'surprise'; 'wow' = 'surprise';
        'sigh' = 'speechless'; 'speechlessness' = 'speechless';
        'meh' = 'speechless'; 'whatever' = 'speechless';
        'baka' = 'confused'; 'huh' = 'confused'; 'confusion' = 'confused';
        'what' = 'confused'; 'idiot' = 'confused';
        'laugh' = 'happy'; 'lol' = 'happy'; 'haha' = 'happy';
        'joy' = 'happy'; 'smile' = 'happy';
        'cry' = 'sad'; 'sob' = 'sad'; 'depressed' = 'sad';
        'mad' = 'angry'; 'rage' = 'angry'; 'annoyed' = 'angry';
        'shyness' = 'shy'; 'blush' = 'shy';
        'thanks' = 'cheer'; 'yes' = 'cheer'; 'ok' = 'cheer';
        'no' = 'refuse'; 'nope' = 'refuse'; 'reject' = 'refuse';
        'tired' = 'sleepy'; 'sleep' = 'sleepy'; 'zzz' = 'sleepy';
        'eat' = 'hungry'; 'food' = 'hungry';
        'rich' = 'money'; 'coin' = 'money';
        'cute' = 'cute'; 'love' = 'cute'; 'adore' = 'cute';
      }
      if ($moodAlias.ContainsKey($mood)) {
        Write-Log ('meme mood alias ' + $mood + ' -> ' + $moodAlias[$mood])
        $mood = $moodAlias[$mood]
      }
      $cands = @()
      foreach ($packRoot in $memeRoots) {
        $d = Join-Path (Join-Path $packRoot 'memes') $mood
        if (Test-Path $d) {
          $cands += @(Get-ChildItem $d -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Extension -match '^\.(jpg|jpeg|png|gif|webp)$' } |
            ForEach-Object { $_.FullName })
        }
      }

      # Her OWN collection comes first. The owner's point: with one folder per
      # mood, a mood request only ever reached the stolen pile when she happened to
      # ask for "stolen", so in practice she kept sending the same prepackaged
      # dafeiyu shots. Now any request prefers what she took from the group.
      # Her own collection lives one level up from this script:
      #   <workdir>\<dataDir>\memes\stolen
      # $root is the napcat folder, so Join-Path $root $dataDirName pointed at
      # <napcat>\<dataDir> which does not exist - her stolen stickers were never
      # reachable. The clue was the log saying "mood=stolen has no images" while
      # the folder held 87 files.
      $workDir = Split-Path -Parent $root
      $stolenDir = Join-Path (Join-Path (Join-Path $workDir $dataDirName) 'memes') 'stolen'
      # The three pre-packaged meme packs are empty shells - their folders still
      # carry the old tag names (sigh, see, baka, cpu ...) but hold zero images.
      # Everything she actually has lives here, sorted into stolen\<mood>\. So a
      # mood request has to look inside that subfolder too, otherwise the only
      # way to get a picture is IMG stolen and every named mood resolves to none.
      if (Test-Path (Join-Path $stolenDir $mood)) {
        $moodStolen = Join-Path $stolenDir $mood
        $cands += @(Get-ChildItem $moodStolen -File -Recurse -ErrorAction SilentlyContinue |
          Where-Object { $_.Extension -match '^\.(jpg|jpeg|png|gif|webp)$' } |
          ForEach-Object { $_.FullName })
      }
      $stolen = @()
      if (Test-Path $stolenDir) {
        # -Recurse: the classifier sorts them into stolen\<mood>\, so the top
        # level only ever held the handful that failed classification.
        $stolen = @(Get-ChildItem $stolenDir -File -Recurse -ErrorAction SilentlyContinue |
          Where-Object { $_.Extension -match '^\.(jpg|jpeg|png|gif|webp)$' } |
          ForEach-Object { $_.FullName })
      }

      # Rotate over the WHOLE pool, not just her stolen stickers. Filtering only
      # the stolen ones meant a mood-matched image could be sent twice in four
      # minutes - the owner caught her posting the identical picture at 11:46 and
      # again at 11:50.
      $recent = @($state.recentMemes | Where-Object { $_ })
      $freshStolen = @($stolen | Where-Object { $recent -notcontains $_ })
      if ($freshStolen.Count -gt 0) { $stolen = $freshStolen }
      $freshCands = @($cands | Where-Object { $recent -notcontains $_ })
      if ($freshCands.Count -gt 0) { $cands = $freshCands }

      if ($stolen.Count -gt 0) {
        # Mix in the mood-matched shots too, but keep her own collection as the
        # majority so it actually gets used.
        $pool = @($stolen) + @($stolen) + @($cands)
        $imgPath = $pool | Get-Random
        Write-Log ('meme mood=' + $mood + ' stolen=' + $stolen.Count + ' moodpool=' + $cands.Count + ' picked=' + (Split-Path $imgPath -Leaf))
      }
      elseif ($cands.Count -gt 0) {
        $imgPath = $cands | Get-Random
        Write-Log ('meme mood=' + $mood + ' candidates=' + $cands.Count + ' picked=' + (Split-Path $imgPath -Leaf))
      }
      else {
        # Last resort: any picture beats no picture. "other" exists for exactly
        # this, and if that is empty too, fall back to the whole stolen pile.
        $fallback = @()
        foreach ($packRoot in $memeRoots) {
          $od = Join-Path (Join-Path $packRoot 'memes') 'other'
          if (Test-Path $od) {
            $fallback += @(Get-ChildItem $od -File -Recurse -ErrorAction SilentlyContinue |
              Where-Object { $_.Extension -match '^\.(jpg|jpeg|png|gif|webp)$' } |
              ForEach-Object { $_.FullName })
          }
        }
        if ($stolen.Count -gt 0) { $fallback += $stolen }
        if ($fallback.Count -gt 0) {
          $imgPath = $fallback | Get-Random
          Write-Log ('meme mood=' + $mood + ' fell back to other/stolen; picked=' + (Split-Path $imgPath -Leaf))
        }
        else {
          Write-Log ('meme mood=' + $mood + ' has no images anywhere; sending text only')
        }
      }


      if ($imgPath) {
        # Remember recent picks, newest last. A long window on purpose: the point
        # is that the same picture must not come round again quickly.
        $state.recentMemes = @(@($state.recentMemes | Where-Object { $_ }) + @($imgPath))
        while (@($state.recentMemes).Count -gt 25) {
          $state.recentMemes = @($state.recentMemes)[1..(@($state.recentMemes).Count - 1)]
        }
        Save-State
      }
    }

  # Deterministic fallback. The model almost never writes the IMG line, so in
  # practice she answered text-only forever - the owner's complaint. Attaching a
  # picture should not depend on it remembering a third line: after $MemeEvery
  # speeches without one, pick a neutral mood here instead.
  if (-not $imgPath -and [int]$state.speakSinceMeme -ge $MemeEvery) {
      $seriousWords = @('[char]0x751f + [char]0x75c5','[char]0x533b + [char]0x9662','[char]0x96be + [char]0x53d7','[char]0x60f3 + [char]0x54ed','[char]0x5d29 + [char]0x6e83','[char]0x53bb + [char]0x4e16','[char]0x5206 + [char]0x624b','[char]0x5435 + [char]0x67b6','[char]0x5931 + [char]0x4e1a','[char]0x5de5 + [char]0x8d44')
      $serious = $false
      foreach ($w in $seriousWords) {
        if ($reply -and $reply.Contains([string]([regex]::Unescape($w)))) { $serious = $true }
      }
    if ($serious) {
      Write-Log 'meme skipped: serious topic'
    }
    else {
      $imgPath = Get-MemeIfDue
      if ($imgPath) {
        Write-Log ('meme auto-attached mood=' + $script:autoMood + ' picked=' + (Split-Path $imgPath -Leaf))
      }
      else {
        Write-Log 'meme auto-attach found nothing'
      }
    }
  }

    Write-Log ('reply: ' + $reply)
    if ($DryRun) { Write-Log ('DRY RUN - not sending' + $(if ($imgPath) { ' (would attach ' + (Split-Path $imgPath -Leaf) + ')' } else { '' })); if (-not $Once) { Start-Sleep -Seconds $PollSeconds; continue } else { break } }

    # Text and picture go out as TWO messages, not one bubble with both.
    # A person types the line, then drops the sticker in - the combined payload
    # reads like a UI that attached a file.
    $res = Invoke-NapCat 'send_group_msg' @{ group_id = $GroupId; message = $reply }
    Write-Log ('deliver(text): ' + $res)
    if (-not $imgPath) { $state.speakSinceMeme = [int]$state.speakSinceMeme + 1 }
    if ($imgPath) {
      Start-Sleep -Milliseconds (500 + (Get-Random -Minimum 0 -Maximum 900))
      $imgSeg = @(@{ type = 'image'; data = @{ file = $imgPath } })
      $resImg = Invoke-NapCat 'send_group_msg' @{ group_id = $GroupId; message = $imgSeg }
      Write-Log ('deliver(image): ' + $resImg)
      $state.speakSinceMeme = 0
      if ($resImg -notmatch '"retcode":\s*0') {
        Write-Log ('image send failed, text already went out')
      }
    }
    if ($res -match '"retcode":\s*0') {
      # She answered, so the pending @ is satisfied.
      if ($state.pendingMention) {
        $state | Add-Member -NotePropertyName pendingMention -NotePropertyValue $null -Force
        if ($triggerKey -eq 'mention') { Write-Log 'pending mention cleared - she answered' }
      }
      $state.lastSendAt = (Get-Date).ToString('o')
      $state.sendTimes = @($state.sendTimes) + @($state.lastSendAt)
      # Only SHE-STARTED messages consume the limited budget.
      if ($triggerKey -eq 'quiet') {
        $state.sendTimesQuiet = @($state.sendTimesQuiet) + @($state.lastSendAt)
        # Count against today's self-start ceiling.
        $dayKey = (Get-Date).ToString('yyyy-MM-dd')
        if ($state.quietDay -ne $dayKey) {
          $state.quietDay = $dayKey
          $state.quietDayCount = 0
        }
        $state.quietDayCount = [int]$state.quietDayCount + 1
        # Draw the next self-start from the (now much wider) jitter range.
        $wait = Get-Random -Minimum $QuietGapMinMinutes -Maximum ($QuietGapMaxMinutes + 1)
        $state.nextQuietAt = (Get-Date).AddMinutes($wait).ToString('o')
        Write-Log ('self-start #' + $state.quietDayCount + '/' + $MaxQuietStartsPerDay + ' today; next in ' + $wait + 'min (at ' + ([datetime]$state.nextQuietAt).ToString('HH:mm') + ')')
      }
      Save-State
    }
  } catch {
    $msg = $_.Exception.Message
    if ($msg -like 'napcat-unreachable*') {
      # Not a groupwatch fault: the bridge is gone (QQ closed, injection lost).
      # Keep polling, but say it once every ten minutes rather than once a minute.
      if (([DateTime]::Now - $script:napcatDownLoggedAt).TotalMinutes -ge 10) {
        $script:napcatDownLoggedAt = [DateTime]::Now
        Write-Log ('napcat down (' + $msg + ') - waiting; QQ is probably closed')
      }
    }
    else {
      Write-Log ('loop error: ' + $msg + ' @ ' + $_.InvocationInfo.PositionMessage)
    }
  }

  if ($Once) { break }
  Start-Sleep -Seconds $PollSeconds
}

Write-Log 'groupwatch exit'

# test