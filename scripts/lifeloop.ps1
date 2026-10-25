# Lanya's "own life" loop.
#
# Gives her time to herself: periodically wakes a headless agent with an open
# brief ("go find something to do"), lets it actually work (it has tools: web
# search/fetch, file read/write, shell inside the workspace), and then delivers
# whatever it decided to share - to the fan group, to her owner, or to nobody.
#
# This is the mechanism behind "she does things on her own and tells people".
# A message-driven QQ session can only react; this loop is what lets her initiate.
#
# Output protocol from the agent (first line decides):
#   NOTHING                       -> nothing worth saying, stay quiet
#   SHARE group  <text>           -> post to the fan group
#   SHARE admin  <text>           -> private message to her owner
#   SHARE <qq>   <text>           -> private message to that friend
# Anything after the first line is ignored, so the brief stays short on QQ.
#
# Cost: one LLM run per tick, but it can use tools so it may be several calls.
# The daily budget file is checked first, so this stops when the pocket money
# runs out.
#
# ASCII-only source (PowerShell 5.1 reads BOM-less .ps1 as ANSI); Chinese lives in
# lifeloop.prompt.txt, read explicitly as UTF-8.

param(
  [switch]$DryRun,
  [switch]$Once,
  # How often she gets free time. The owner asked for much more of it, and for her
  # to actually use up her daily allowance, so this went from hourly to every 15
  # minutes. The budget check keeps that bounded: once the day's money is gone,
  # nothing more runs until tomorrow.
  [int]$IntervalMinutes = 15,
  [int]$WindowStart = 9,
  [int]$WindowEnd = 23
)

$ErrorActionPreference = 'Stop'

$root       = Join-Path (Split-Path -Parent $PSScriptRoot) 'napcat'
$workDir    = Split-Path -Parent $root
$lanyaDir   = Join-Path $workDir ([string][char]0x5C9A + [char]0x5A05)
$notesDir   = Join-Path $lanyaDir 'notes'
$madeDir    = Join-Path $lanyaDir 'made'
$promptPath = Join-Path $root 'lifeloop.prompt.txt'
$activitiesPath = Join-Path $lanyaDir 'activities.json'

$statePath  = Join-Path $lanyaDir 'lifeloop.state.json'
$logPath    = Join-Path $lanyaDir 'lifeloop.log'
$budgetPath = Join-Path $lanyaDir 'budget.json'
$cfgPath    = Join-Path $root 'balance-alert.config.json'
$dsh        = (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'DSH\resources\runtime\cli\bin\dsh.cmd')
$groupId    = 'YOUR_GROUP_ID'
$adminQQ    = 'YOUR_ADMIN_QQ'

function Write-Log([string]$m) {
  $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m
  Add-Content -Path $logPath -Value $line -Encoding UTF8
  Write-Output $line
}


# How long to sleep before the next wake-up.
#
# A fixed 15-minute tick made her rhythm feel like a punch clock. Real people
# are uneven: sometimes they come back to something within minutes because it is
# still interesting, sometimes they drift off for a long stretch. The levels also
# make the page able to show "she is keen right now" vs "she is dozing".
#
#   keen     half to 0.9x   - came back quickly, still hooked
#   normal   0.9x to 1.5x   - the usual uneven gap
#   doze     1.5x to 2.5x   - lost interest / quiet stretch
function Get-SleepPlan([int]$BaseMinutes) {
    $roll = Get-Random -Minimum 0 -Maximum 100
    if ($roll -lt 25) {
        $lo = 0.5; $hi = 0.9; $mood = 'keen'
    } elseif ($roll -lt 85) {
        $lo = 0.9; $hi = 1.5; $mood = 'normal'
    } else {
        $lo = 1.5; $hi = 2.5; $mood = 'doze'
    }
    $factor = $lo + ((Get-Random -Minimum 0 -Maximum 1000) / 1000.0) * ($hi - $lo)
    $secs = [int]($BaseMinutes * 60 * $factor)
    if ($secs -lt 60) { $secs = 60 }
    return @{ Seconds = $secs; Mood = $mood }
}

function Save-Mood([string]$Mood, [int]$Secs) {
    try {
        $obj = @{ mood = $Mood; nextSeconds = $Secs; at = (Get-Date).ToString('o') }
        $jp = Join-Path $lanyaDir 'lifeloop.mood.json'
        ($obj | ConvertTo-Json -Compress) | Set-Content -Path $jp -Encoding UTF8
    } catch { }
}

function Read-AnyText([string]$path) {
  $b = [System.IO.File]::ReadAllBytes($path)
  if ($b.Length -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF) {
    return [System.Text.Encoding]::UTF8.GetString($b, 3, $b.Length - 3)
  }
  return [System.Text.Encoding]::UTF8.GetString($b)
}

if (-not (Test-Path $notesDir)) { New-Item -ItemType Directory -Path $notesDir -Force | Out-Null }
$napcat = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json

# NOTE: the list of people she may contact lives in lifeloop.prompt.txt, not here.
# This file must stay pure ASCII - PowerShell 5.1 reads a BOM-less .ps1 with the
# ANSI codepage, so Chinese literals here become a parse error.
$friendsBlock = '(see the contact list inside the prompt above)'

function Invoke-NapCat([string]$action, $bodyObj) {
  Add-Type -AssemblyName System.Net.Http
  $client = New-Object System.Net.Http.HttpClient
  $client.Timeout = [TimeSpan]::FromSeconds(30)
  $client.DefaultRequestHeaders.Authorization =
    New-Object System.Net.Http.Headers.AuthenticationHeaderValue('Bearer', [string]$napcat.napcatToken)
  # -Depth matters. ConvertTo-Json defaults to 2 levels, but an image segment nests
  # four: object -> array -> {type,data} -> {file}. Without it the "file" key is
  # dropped and NapCat answers "file message is missing a parameter", so a picture fails
  # while plain text goes through. Same bug as groupwatch had.
  $json = $bodyObj | ConvertTo-Json -Compress -Depth 10
  $content = New-Object System.Net.Http.StringContent($json, [System.Text.Encoding]::UTF8, 'application/json')
  $url = ([string]$napcat.napcatHttp).TrimEnd('/') + '/' + $action
  $resp = $client.PostAsync($url, $content).Result
  return [System.Text.Encoding]::UTF8.GetString($resp.Content.ReadAsByteArrayAsync().Result)
}

# Same raw-byte stdout handling as groupwatch.ps1: decoding the child's stdout as
# text used the console codepage and produced mojibake.
function Ask-Agent([string]$task) {
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = 'cmd.exe'
  $psi.Arguments = '/c ""' + $dsh + '" --profile headless -"'
  $psi.UseShellExecute = $false
  $psi.RedirectStandardInput = $true
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.CreateNoWindow = $true
  $psi.WorkingDirectory = $workDir

  $p = [System.Diagnostics.Process]::Start($psi)
  $ms = New-Object System.IO.MemoryStream
  $copyTask = $p.StandardOutput.BaseStream.CopyToAsync($ms)
  $errTask = $p.StandardError.ReadToEndAsync()
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($task)
  $p.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
  $p.StandardInput.BaseStream.Flush()
  $p.StandardInput.Close()
  if (-not $p.WaitForExit(300000)) { try { $p.Kill() } catch { } }
  try { $copyTask.Wait(15000) | Out-Null } catch { }
  $errTask.Wait(3000) | Out-Null
  $raw = $ms.ToArray()
  if ($raw.Length -eq 0) { return '' }
  if ($raw.Length -ge 2 -and $raw[0] -eq 0xFF -and $raw[1] -eq 0xFE) {
    return [System.Text.Encoding]::Unicode.GetString($raw, 2, $raw.Length - 2)
  }
  if ($raw.Length -ge 3 -and $raw[0] -eq 0xEF -and $raw[1] -eq 0xBB -and $raw[2] -eq 0xBF) {
    return [System.Text.Encoding]::UTF8.GetString($raw, 3, $raw.Length - 3)
  }
  return [System.Text.Encoding]::UTF8.GetString($raw)
}

Write-Log ('lifeloop start: interval=' + $IntervalMinutes + 'min window=' + $WindowStart + '-' + $WindowEnd + ' dryRun=' + [bool]$DryRun)

while ($true) {
  $hour = (Get-Date).Hour
  if ($hour -lt $WindowStart -or $hour -ge $WindowEnd) {
    if ($Once) { Write-Log 'outside window, exit'; break }
    Start-Sleep -Seconds 300; continue
  }

  # Pocket money gate.
  $skip = $false
  if (Test-Path $budgetPath) {
    try {
      $b = Get-Content $budgetPath -Raw -Encoding UTF8 | ConvertFrom-Json
      if ($b.exhausted) { Write-Log ('budget exhausted (' + $b.spent + '/' + $b.limit + '), sleep'); $skip = $true }
    } catch { }
  }
  if ($skip) { if ($Once) { break } else { Start-Sleep -Seconds 900; continue } }

  try {
    # What she already has: her own notes, so she can continue earlier threads
    # instead of starting from nothing every time.
    $noteFiles = @(Get-ChildItem $notesDir -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 5)
    $noteList = if ($noteFiles.Count -gt 0) { ($noteFiles | ForEach-Object { $_.Name }) -join ', ' } else { '(none yet)' }

    $state = @{ lastShare = ''; lastAt = '' }
    if (Test-Path $statePath) { try { $state = Get-Content $statePath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { } }
    $sinceShare = if ($state.lastAt) { [math]::Round(((Get-Date) - [DateTime]::Parse([string]$state.lastAt)).TotalMinutes, 0) } else { 999 }

    # How long since anyone actually talked to her. The QQ session files are
    # rewritten on every message, so their newest mtime is a cheap, accurate
    # "last time someone spoke to me" clock - no API call needed.
    $idleMin = 999
    $sessDir = Join-Path $env:USERPROFILE '.dsh\storages\session_projcache\sessions'
    if (Test-Path $sessDir) {
      $newest = Get-ChildItem $sessDir -File -Filter 'onebot-*.json' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
      if ($newest) { $idleMin = [math]::Round(((Get-Date) - $newest.LastWriteTime).TotalMinutes, 0) }
    }

    $tpl = Read-AnyText $promptPath

# Append the "do not do this today" list from activities.json.
# The panel owns that file: turning an activity off there must actually stop it,
# otherwise the checkbox is a lie.
try {
    if (Test-Path $activitiesPath) {
        $acts = (Get-Content $activitiesPath -Raw -Encoding UTF8 | ConvertFrom-Json).activities
        $off = @($acts | Where-Object { $_.enabled -eq $false })
        if ($off.Count -gt 0) {
            $names = ($off | ForEach-Object { $_.name }) -join ', '
            $tpl = $tpl + "`n`n[panel] These are switched OFF today, do not do them: " + $names + "`n"
        }
    }
} catch { }
    $task = $tpl.Replace('{{NOW}}', (Get-Date).ToString('yyyy-MM-dd HH:mm')).
      Replace('{{NOTES}}', $noteList).
      Replace('{{LASTSHARE}}', [string]$state.lastShare).
      Replace('{{SINCESHARE}}', [string]$sinceShare).
      Replace('{{IDLE}}', [string]$idleMin).
      Replace('{{FRIENDS}}', $friendsBlock)

    Write-Log ('waking agent (notes=' + $noteFiles.Count + ' sinceLastShare=' + $sinceShare + 'min idle=' + $idleMin + 'min)')
    $answer = (Ask-Agent $task).Trim()
    if ($answer -eq '') { Write-Log 'empty answer'; if ($Once) { break } else { $plan = Get-SleepPlan $IntervalMinutes; Save-Mood $plan.Mood $plan.Seconds; Write-Log ('sleep ' + $plan.Seconds + 's (' + $plan.Mood + ')'); Start-Sleep -Seconds $plan.Seconds; continue } }

    $rows = @($answer -split "`r?`n" | Where-Object { $_.Trim() -ne '' })
    $head = $rows[0].Trim()
    Write-Log ('decision: ' + $head.Substring(0, [Math]::Min(80, $head.Length)))

    if ($head -notlike 'SHARE*') {
      Write-Log 'NOTHING - nothing to share'
      if ($Once) { break } else { $plan = Get-SleepPlan $IntervalMinutes; Save-Mood $plan.Mood $plan.Seconds; Write-Log ('sleep ' + $plan.Seconds + 's (' + $plan.Mood + ')'); Start-Sleep -Seconds $plan.Seconds; continue }
    }

    # SHARE <target> <text>
    $m = [regex]::Match($head, '^SHARE\s+(\S+)\s+(.+)$')
    if (-not $m.Success) { Write-Log 'malformed SHARE line'; if ($Once) { break } else { $plan = Get-SleepPlan $IntervalMinutes; Save-Mood $plan.Mood $plan.Seconds; Write-Log ('sleep ' + $plan.Seconds + 's (' + $plan.Mood + ')'); Start-Sleep -Seconds $plan.Seconds; continue } }
    $target = $m.Groups[1].Value.Trim().ToLower()
    $textToSend = $m.Groups[2].Value.Trim()

    # Optional second line "IMG <path>" attaches a picture. She downloads web
    # images into her own folder first, so we always send a local file (proven to
    # work); a bare URL is accepted too as a fallback.
    $imgPath = $null
    if ($rows.Count -ge 2 -and $rows[1] -match '^\s*IMG\s+(.+?)\s*$') {
      $candidate = $Matches[1].Trim()
      if ($candidate -match '^https?://') {
        $imgPath = $candidate
        Write-Log ('attaching remote image: ' + $candidate)
      } elseif (Test-Path $candidate) {
        $ext = [System.IO.Path]::GetExtension($candidate).ToLower()
        if ($ext -match '^\.(jpg|jpeg|png|gif|webp|bmp)$') {
          $imgPath = $candidate
          Write-Log ('attaching local image: ' + (Split-Path $candidate -Leaf))
        } else {
          Write-Log ('IMG target is not an image (' + $ext + '); sending text only')
        }
      } else {
        Write-Log ('IMG path not found: ' + $candidate)
      }
    }

    # Optional file payload: how she hands over something she MADE (a little game,
    # a page, a document). Accepts a local path only - no remote downloads here.
    $filePath = $null
    foreach ($row in $rows) {
      if ($row -match '^\s*FILE\s+(.+?)\s*$') {
        $candidate = $Matches[1].Trim()
        if (Test-Path $candidate) {
          $filePath = $candidate
          Write-Log ('attaching file: ' + (Split-Path $candidate -Leaf))
        } else {
          Write-Log ('FILE path not found: ' + $candidate)
        }
      }
    }

    if ($DryRun) {
      Write-Log ('DRY RUN - would send to ' + $target + ': ' + $textToSend + $(if ($imgPath) { ' [+image]' } else { '' }) + $(if ($filePath) { ' [+file ' + (Split-Path $filePath -Leaf) + ']' } else { '' }))
      if ($Once) { break } else { $plan = Get-SleepPlan $IntervalMinutes; Save-Mood $plan.Mood $plan.Seconds; Write-Log ('sleep ' + $plan.Seconds + 's (' + $plan.Mood + ')'); Start-Sleep -Seconds $plan.Seconds; continue }
    }

    $msgObj = if ($imgPath) {
      @(
        @{ type = 'text';  data = @{ text = $textToSend } },
        @{ type = 'image'; data = @{ file = $imgPath } }
      )
    } else { $textToSend }

    $res = if ($target -eq 'group') {
      Invoke-NapCat 'send_group_msg' @{ group_id = $groupId; message = $msgObj }
    } elseif ($target -eq 'admin') {
      Invoke-NapCat 'send_private_msg' @{ user_id = $adminQQ; message = $msgObj }
    } elseif ($target -match '^\d{5,12}$') {
      Invoke-NapCat 'send_private_msg' @{ user_id = $target; message = $msgObj }
    } else {
      Write-Log ('unknown target ' + $target + '; skipped')
      if ($Once) { break } else { $plan = Get-SleepPlan $IntervalMinutes; Save-Mood $plan.Mood $plan.Seconds; Write-Log ('sleep ' + $plan.Seconds + 's (' + $plan.Mood + ')'); Start-Sleep -Seconds $plan.Seconds; continue }
    }

    # Optional link payload: preferred way to hand over something she made. She
    # writes "LINK <filename>" and we expand it into the local works URL, since
    # the file is already served from made/ by made-server.ps1.
    $extraLine = $null
    foreach ($row in $rows) {
      if ($row -match '^\s*LINK\s+(.+?)\s*$') {
        $name = $Matches[1].Trim()
        if ($name -match '^https?://') {
          $extraLine = $name
        } else {
          # Accept a bare name, a relative path, or an absolute path inside made/.
          $leaf = Split-Path $name -Leaf
          if (Test-Path (Join-Path $madeDir $leaf)) {
            $extraLine = 'http://127.0.0.1:19388/' + [uri]::EscapeDataString($leaf)
          } else {
            Write-Log ('LINK target not found in made/: ' + $leaf)
          }
        }
        if ($extraLine) { Write-Log ('attaching link: ' + $extraLine) }
      }
    }
    # Safety net for LINKS: she kept telling the reader to click and play without
    # emitting the LINK line, so the reader had nothing to click. If her text names a file that
    # actually exists in made/, attach that link automatically. Matching on the
    # bare basename is precise enough: a caption naming the work contains its stem
    # and so matches the matching html file.
    if (-not $extraLine -and (Test-Path $madeDir)) {
      foreach ($mf in (Get-ChildItem $madeDir -File -ErrorAction SilentlyContinue)) {
        if ($mf.Name.StartsWith('_')) { continue }
        $stem = [System.IO.Path]::GetFileNameWithoutExtension($mf.Name)
        if ($stem.Length -ge 2 -and $textToSend -like ('*' + $stem + '*')) {
          $extraLine = 'http://127.0.0.1:19388/' + [uri]::EscapeDataString($mf.Name)
          Write-Log ('auto-linked by name match: ' + $mf.Name)
          break
        }
      }
    }

    if ($extraLine) { $textToSend = $textToSend.TrimEnd() + ' ' + $extraLine }

    # Hard rule: never push a FILE into a group. The owner asked for this after a
    # game landed in the 515-member fan group - a downloadable attachment in a busy
    # group is intrusive (and unclickable for most members). Links and images stay
    # fine in groups; files are private-chat only.
    if ($filePath -and $target -eq 'group') {
      Write-Log ('BLOCKED: file into group (' + (Split-Path $filePath -Leaf) + ') - files are private-only; sending text only')
      $filePath = $null
    }

    # Optional file payload: this is how she hands over something she MADE (a
    # little game, a page, a document). Sent after the text so the caption
    # arrives first. upload_private_file / upload_group_file are the OneBot
    # actions NapCat actually supports for this (send_private_file does not exist).
    if ($filePath) {
      $fileRes = if ($target -eq 'group') {
        Invoke-NapCat 'upload_group_file' @{ group_id = $groupId; file = $filePath; name = (Split-Path $filePath -Leaf) }
      } elseif ($target -eq 'admin') {
        Invoke-NapCat 'upload_private_file' @{ user_id = $adminQQ; file = $filePath; name = (Split-Path $filePath -Leaf) }
      } else {
        Invoke-NapCat 'upload_private_file' @{ user_id = $target; file = $filePath; name = (Split-Path $filePath -Leaf) }
      }
      Write-Log ('deliver(file): ' + $fileRes)
    }
    Write-Log ('deliver(' + $target + '): ' + $res)

    if ($res -match '"retcode":\s*0') {
      $obj = [pscustomobject]@{ lastShare = $textToSend; lastAt = (Get-Date).ToString('o'); lastTarget = $target }
      [System.IO.File]::WriteAllText($statePath, ($obj | ConvertTo-Json -Compress), (New-Object System.Text.UTF8Encoding($false)))

      # Append to a log she can READ.
      #
      # Why: the onebot plugin drops her own outbound messages (ignoreSelf=true,
      # to prevent a self-reply loop), so a message sent from here never enters
      # her conversation context. When the owner then replies "so what about
      # that thing you said", she has no idea what he means. This log is the
      # workaround: she can open it when the conversation references something
      # she does not remember saying.
      $sentLog = Join-Path $lanyaDir 'sent-log.txt'
      $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm')
      $entry = '[' + $stamp + '] -> ' + $target + ': ' + $textToSend + "`r`n"
      [System.IO.File]::AppendAllText($sentLog, $entry, (New-Object System.Text.UTF8Encoding($false)))

      # Also feed the cross-chat log, which the plugin injects into EVERY chat's
      # next turn. Without this she could send something here and then not know
      # about it when the same person replies in a private chat.
      $crossLog = Join-Path $lanyaDir 'cross-chat.md'
      $time = (Get-Date).ToString('HH:mm:ss')
      $who = if ($target -eq 'group') { 'group' } else { $target }
      $crossEntry = '[' + $time + '] ' + $who + ' | Lanya(sent): ' + $textToSend + "`n"
      [System.IO.File]::AppendAllText($crossLog, $crossEntry, (New-Object System.Text.UTF8Encoding($false)))
    }
  } catch {
    Write-Log ('loop error: ' + $_.Exception.Message)
  }

  if ($Once) { break }
  Start-Sleep -Seconds ($IntervalMinutes * 60)
}

Write-Log 'lifeloop exit'
