# Agent-tier proactive chat.
#
# Difference from autochat.ps1 (the script tier):
#   script tier = picks a canned line, zero tokens.
#   agent tier  = feeds the real recent conversation to a HEADLESS DSH agent and
#                 sends whatever it composes, so the message is context-aware.
#
# Why a headless profile instead of the running desktop host:
#   The QQ bridge (reverse WebSocket to DSH on 8643) is exclusive - only one host
#   may own it. Spinning up a second desktop-profile host would fight the live one.
#   The headless profile has NO onebot plugin, so it never touches 8643; it just
#   prints a reply to stdout and the script delivers it through NapCat's HTTP API.
#
# Cost: this tier spends tokens - one small LLM call per message. Keep the
# cadence low (see the scheduled task interval).
#
# ASCII-only source: Windows PowerShell 5.1 decodes BOM-less .ps1 using the system
# ANSI codepage, so all Chinese lives in the JSON/prompt files.

param(
  [switch]$DryRun,
  [string]$TargetGroup = '',
  [string]$TargetQQ = ''
)

$ErrorActionPreference = 'Stop'

# Derived from this script's own location: <root>\napcat\<file>.
$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $root 'napcat'))) {
  $root = Split-Path -Parent $root
}
$relPath = Join-Path $root 'autochat.relations.json'
$statePath = Join-Path $root 'agentchat.state.json'
$logPath   = Join-Path $root 'agentchat.log'
$promptPath = Join-Path $root 'agentchat.prompt.txt'
# DSH install root - found, not assumed.
$dsh = ''
foreach ($d in 'CDEFG') {
  $cand = Join-Path ($d + ':\') ('DSH\resources\runtime\cli\bin\dsh.cmd')
  if (Test-Path $cand) { $dsh = $cand; break }
}
$napcat  = Get-Content (Join-Path $root 'balance-alert.config.json') -Raw -Encoding UTF8 | ConvertFrom-Json

function Write-Log([string]$m) {
  $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m
  Add-Content -Path $logPath -Value $line -Encoding UTF8
  Write-Output $line
}

# Chinese literals are built from code points on purpose. This script must stay
# pure ASCII: Windows PowerShell 5.1 decodes a BOM-less .ps1 using the system ANSI
# codepage, so embedded Chinese would be read as garbage (this bit me once here).
$LBL_IMAGE = [string][char]0x56FE + [char]0x7247              # image label
$LBL_VOICE = [string][char]0x8BED + [char]0x97F3              # voice label
$SELF_PREFIX = [string][char]0x6211 + '(' + [char]0x5C9A + [char]0x5A05 + ')' + [char]0x4E4B + [char]0x524D + [char]0x8BF4 + ': '  # "I said earlier: "

# ---- helpers ---------------------------------------------------------------
function Invoke-NapCat([string]$action, $bodyObj) {
  Add-Type -AssemblyName System.Net.Http
  $client = New-Object System.Net.Http.HttpClient
  $client.Timeout = [TimeSpan]::FromSeconds(30)
  $client.DefaultRequestHeaders.Authorization =
    New-Object System.Net.Http.Headers.AuthenticationHeaderValue('Bearer', [string]$napcat.napcatToken)
  $json = $bodyObj | ConvertTo-Json -Compress
  $content = New-Object System.Net.Http.StringContent($json, [System.Text.Encoding]::UTF8, 'application/json')
  $url = ([string]$napcat.napcatHttp).TrimEnd('/') + '/' + $action
  $resp = $client.PostAsync($url, $content).Result
  return [System.Text.Encoding]::UTF8.GetString($resp.Content.ReadAsByteArrayAsync().Result)
}

# Read a text file that may be UTF-16LE (headless stdout) or UTF-8.
function Read-AnyText([string]$path) {
  $bytes = [System.IO.File]::ReadAllBytes($path)
  if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) {
    return [System.Text.Encoding]::Unicode.GetString($bytes, 2, $bytes.Length - 2)
  }
  if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
    return [System.Text.Encoding]::UTF8.GetString($bytes, 3, $bytes.Length - 3)
  }
  return [System.Text.Encoding]::UTF8.GetString($bytes)
}

# ---- cooldown --------------------------------------------------------------
$state = @{ lastSentAt = $null }
if (Test-Path $statePath) {
  try { $state = Get-Content $statePath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
}
if ($state.lastSentAt) {
  $since = ((Get-Date) - [DateTime]::Parse([string]$state.lastSentAt)).TotalHours
  if ($since -lt 2) { Write-Log ('cooldown active (' + [math]::Round($since,1) + 'h < 2h)'); exit 0 }
}

# ---- gather recent conversation as context --------------------------------
$history = Invoke-NapCat 'get_group_msg_history' @{ group_id = $TargetGroup; count = 15 }
$hj = $history | ConvertFrom-Json
if ($hj.retcode -ne 0) { Write-Log ('history fetch failed: ' + $history); exit 1 }

$lines = @()
# Her own QQ, read from config.
$selfId = ''
try {
  $cf = Join-Path (Split-Path -Parent $PSScriptRoot) (Join-Path ([string][char]0x5927 + [char]0x80A5 + [char]0x9C7C) 'lanya-config.json')
  if (Test-Path $cf) {
    $cj = Get-Content $cf -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($cj.botQQ) { $selfId = [string]$cj.botQQ }
  }
} catch { }
foreach ($m in $hj.data.messages) {
  $t = ''
  if ($m.message -is [array]) {
    # Represent non-text segments so the model knows a picture/voice was there.
    $parts = @()
    foreach ($seg in $m.message) {
      if ($seg.type -eq 'text') { $parts += $seg.data.text }
      elseif ($seg.type -eq 'image') { $parts += ('[' + $LBL_IMAGE + ']') }
      elseif ($seg.type -eq 'record') { $parts += ('[' + $LBL_VOICE + ']') }
      elseif ($seg.type -eq 'at') { $parts += ('@' + $seg.data.qq) }
    }
    $t = ($parts -join '')
  } else { $t = [string]$m.message }
  $t = $t.Trim()
  if ($t -eq '') { continue }

  # NapCat reports other people's messages to the bridge, but the bot's OWN
  # messages are sent straight through the HTTP API. With reportSelfMessage off
  # the bridge never sees them, so the agent would not know what it already said
  # and could not follow up when someone answers it. Mark them explicitly here so
  # the reply is coherent without needing a NapCat restart.
  $who = [string]$m.sender.user_id
  if ($who -eq $selfId) {
    $lines += ($SELF_PREFIX + $t)
  } else {
    $lines += ($m.sender.nickname + ': ' + $t)
  }
}
if ($lines.Count -eq 0) { Write-Log 'no usable history; skip'; exit 0 }

$contextPath = Join-Path $root 'agentchat.context.txt'
[System.IO.File]::WriteAllText($contextPath, ($lines -join "`r`n"), (New-Object System.Text.UTF8Encoding($false)))

$template = Read-AnyText $promptPath
$task = $template.Replace('{{TARGET}}', $TargetGroup).Replace('{{CONTEXT}}', ($lines -join "`n"))

if ($DryRun) {
  Write-Log ('DRY RUN - task is ' + $task.Length + ' chars; context lines:')
  $lines | ForEach-Object { Write-Log ('   ' + $_) }
  exit 0
}

# ---- ask the headless agent for one in-character line ---------------------
# The task goes in via STDIN (headless accepts `-` to read the task from stdin).
# Passing it on the command line would break on newlines and the ~8k cmd limit.
$taskPath = Join-Path $root 'agentchat.task.txt'
[System.IO.File]::WriteAllText($taskPath, $task, (New-Object System.Text.UTF8Encoding($false)))

$outFile = Join-Path $root 'agentchat.out.txt'
$errFile = Join-Path $root 'agentchat.err.txt'
Remove-Item $outFile, $errFile -Force -ErrorAction SilentlyContinue

# Use .NET redirects instead of cmd.exe metacharacters. Going through
# `cmd /c "... < in > out"` broke on nested quoting (the process exited in 0.1s
# having done nothing), so feed stdin and capture stdout via the process object.
$sw = [Diagnostics.Stopwatch]::StartNew()
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = 'cmd.exe'
$psi.Arguments = '/c ""' + $dsh + '" --profile headless -"'
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.CreateNoWindow = $true
$psi.WorkingDirectory = $root

$proc = [System.Diagnostics.Process]::Start($psi)
$stdoutTask = $proc.StandardOutput.ReadToEndAsync()
$stderrTask = $proc.StandardError.ReadToEndAsync()
# UTF-8 (no BOM) on stdin: the CLI reads the task as UTF-8.
$stdinBytes = [System.Text.Encoding]::UTF8.GetBytes($task)
$proc.StandardInput.BaseStream.Write($stdinBytes, 0, $stdinBytes.Length)
$proc.StandardInput.BaseStream.Flush()
$proc.StandardInput.Close()

if (-not $proc.WaitForExit(180000)) { try { $proc.Kill() } catch { } }
$sw.Stop()

$stdout = $stdoutTask.Result
$stderr = $stderrTask.Result
[System.IO.File]::WriteAllText($outFile, $stdout, (New-Object System.Text.UTF8Encoding($false)))
[System.IO.File]::WriteAllText($errFile, $stderr, (New-Object System.Text.UTF8Encoding($false)))
Write-Log ('headless exit=' + $proc.ExitCode + ' took=' + [math]::Round($sw.Elapsed.TotalSeconds,1) + 's outLen=' + $stdout.Length + ' errLen=' + $stderr.Length)

if ($stdout -eq '') {
  Write-Log ('empty reply; stderr tail: ' + $stderr.Substring(0, [Math]::Min(400, $stderr.Length)))
  exit 1
}
$reply = $stdout.Trim()
# Only the first line is sent - a chat message, not an essay.
$reply = ($reply -split "`r?`n" | Where-Object { $_.Trim() -ne '' } | Select-Object -First 1).Trim()
Write-Log ('reply: ' + $reply)

# ---- deliver --------------------------------------------------------------
$send = if ($TargetQQ -ne '') {
  Invoke-NapCat 'send_private_msg' @{ user_id = [string]$TargetQQ; message = $reply }
} else {
  Invoke-NapCat 'send_group_msg' @{ group_id = [string]$TargetGroup; message = $reply }
}
Write-Log ('deliver: ' + $send)

if ($send -match '"retcode":\s*0') {
  [pscustomobject]@{ lastSentAt = (Get-Date).ToString('o'); lastReply = $reply } |
    ConvertTo-Json -Compress | Set-Content -Path $statePath -Encoding UTF8
} else {
  exit 1
}
