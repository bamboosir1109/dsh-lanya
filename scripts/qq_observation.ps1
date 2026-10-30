# ASCII-only bridge to the durable observation inbox. Never calls an LLM or QQ.
if (-not (Get-Command Get-ProjectConfValue -ErrorAction SilentlyContinue)) {
  . (Join-Path $PSScriptRoot 'project_conf.ps1')
}
$script:QqObservationHere = $PSScriptRoot

function Test-QqUnifiedMode {
  $mode = [string](Get-ProjectConfValue 'decisions.mode' 'unified')
  if ($mode -ceq 'legacy') { return $false }
  if ($mode -ceq 'unified') { return $true }
  throw 'Unknown decision mode; independent QQ activity refused'
}

function Assert-QqLegacyAction([string]$Action) {
  if ($Action -match '^(send_|upload_)' -and (Test-QqUnifiedMode)) {
    throw 'Direct QQ transport is disabled in unified mode; no delivery occurred'
  }
}

function Get-QqObservationScope {
  param([string]$Target = 'admin', [string]$GroupId = '')
  $identity = $null
  $identityPath = Join-Path (Get-ProjectDataDir) 'lanya-config.json'
  if (Test-Path -LiteralPath $identityPath -PathType Leaf) {
    $identity = Get-Content -LiteralPath $identityPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
  }
  $prefix = 'private:'
  $value = $Target
  if ($Target -ceq 'group') {
    $prefix = 'group:'
    $value = $GroupId
    if (-not $value) { $value = [string]$identity.fanGroupQQ }
    if (-not $value) { $value = [string]$identity.fanGroup }
    if (-not $value) { $value = [string](Get-ProjectConfValue 'fanGroup' '') }
  } elseif ($Target -ceq 'admin') {
    $value = $identity.adminQQ
    if ($value -is [array]) { $value = if ($value.Count) { $value[0] } else { '' } }
    $value = [string]$value
  }
  if ([string]$value -cnotmatch '^[1-9][0-9]*$') { throw 'No trusted observation conversation configured' }
  return ($prefix + $value)
}

function ConvertTo-QqProcessArgument([string]$Value) {
  # Windows CreateProcess quoting; no command interpreter is involved.
  return ('"' + [regex]::Replace([regex]::Replace($Value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"')
}

function Submit-QqObservation {
  param(
    [Parameter(Mandatory = $true)][string]$Scope,
    [Parameter(Mandatory = $true)][string]$Kind,
    [Parameter(Mandatory = $true)][string]$EventId,
    [Parameter(Mandatory = $true)][string]$Producer,
    [Parameter(Mandatory = $true)][System.Collections.IDictionary]$Payload
  )
  if (-not (Test-QqUnifiedMode)) { throw 'Observation submission requires unified mode' }
  if ($Scope -cnotmatch '^(private|group):[1-9][0-9]*$') { throw 'Invalid trusted observation scope' }
  $workspace = [IO.Path]::GetFullPath((Get-ProjectWorkDir))
  $preset = ([string](Get-ProjectConfValue 'decisions.agentPreset' '')).Trim()
  if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
    $workspace = $workspace.ToLowerInvariant()
    $preset = $preset.ToLowerInvariant()
  }
  $event = @{
    version = 1; scope = $Scope; kind = $Kind; event_id = $EventId; producer = $Producer
    created_at = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    workspace_key = $workspace; agent_key = $preset
    origin = 'observation'; control_allowed = $false; payload = $Payload
  }
  $json = $event | ConvertTo-Json -Compress -Depth 20
  $bytes = [Text.Encoding]::UTF8.GetBytes($json)
  if ($bytes.Length -gt 262144) { throw 'Observation exceeds size limit' }
  $python = if ($env:LANYA_PYTHON) { $env:LANYA_PYTHON } else { 'python' }
  $helper = Join-Path $script:QqObservationHere 'decision_events.py'
  if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) { throw 'Missing decision observation writer' }
  $psi = New-Object Diagnostics.ProcessStartInfo
  $psi.FileName = $python
  $psi.Arguments = ((@($helper, '--directory', (Join-Path (Get-ProjectDataDir) 'decision-inbox')) | ForEach-Object { ConvertTo-QqProcessArgument $_ }) -join ' ')
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $psi.RedirectStandardInput = $true
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
  $psi.StandardErrorEncoding = [Text.Encoding]::UTF8
  $process = $null
  try {
    $process = [Diagnostics.Process]::Start($psi)
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    $process.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
    $process.StandardInput.Close()
    if (-not $process.WaitForExit(30000)) {
      try { $process.Kill() } catch { }
      throw 'Observation writer timed out; no delivery occurred'
    }
    # Drain diagnostics without echoing child stderr, which could contain input
    # evidence or local configuration. Only a generic class/exit code is public.
    $null = $stderr.Result
    if ($process.ExitCode -ne 0) {
      throw ('Observation writer refused evidence; class=ObservationWriterError exit=' + $process.ExitCode + '; no delivery occurred')
    }
    $ack = $stdout.Result | ConvertFrom-Json -ErrorAction Stop
    if ($ack.state -notin @('pending', 'done') -or $ack.event_id -cne $EventId -or $ack.key -cnotmatch '^[a-f0-9]{64}$') {
      throw 'Invalid observation writer receipt'
    }
    return $ack
  } finally {
    if ($process) { $process.Dispose() }
  }
}

function Submit-QqQuietObservation {
  param([string]$GroupId, [long]$LastMessageAt, [int]$QuietMinutes, [DateTimeOffset]$Now = [DateTimeOffset]::Now)
  if ($QuietMinutes -lt 1 -or $LastMessageAt -le 0) { return $null }
  if (($Now.ToUnixTimeSeconds() - $LastMessageAt) -lt ($QuietMinutes * 60)) { return $null }
  $scope = Get-QqObservationScope -Target group -GroupId $GroupId
  # One immutable candidate per continuous quiet period. Passing wall-clock
  # time is not new evidence; only a real newer message starts another period.
  return (Submit-QqObservation -Scope $scope -Kind 'group.quiet' -Producer 'groupwatch' -EventId ('groupwatch:quiet:' + $LastMessageAt + ':' + $QuietMinutes) -Payload @{
    last_message_at = $LastMessageAt; quiet_threshold_minutes = $QuietMinutes
  })
}
