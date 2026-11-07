# Runtime control. status is read-only: no probes, patch checks or log writes.
param(
  [Parameter(Position = 0)]
  [ValidateSet('status', 'start', 'stop', 'restart', 'logs')]
  [string]$Action = 'status'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'project_conf.ps1')
$workDir = Get-ProjectWorkDir
$runtimeDir = Join-Path $workDir 'napcat'
$dataDir = Get-ProjectDataDir
$taskNames = @('DSH-GroupWatch', 'DSH-Lanya-Voice', 'DSH-Lanya-Browser',
  'DSH-Lanya-Works', 'DSH-Lanya-TTS', 'DSH-Lanya-Budget',
  'DSH-Lanya-Supervisor', 'DSH-Lanya-Panel', 'DSH-Lanya-PanelStatic', 'DSH-Lanya-Mind')

function Show-Status {
  Write-Output ('Work directory: ' + $workDir)
  Write-Output ('Data directory: ' + $dataDir)
  $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue)
  $ports = [ordered]@{
    OneBot = (Get-ProjectConfValue 'onebotPort' 8720)
    NapCatWeb = 6099
    Bridge = 8643
    Panel = (Get-ProjectConfValue 'panelStaticPort' 19388)
    PanelAPI = (Get-ProjectConfValue 'panelPort' 19389)
    TTS = (Get-ProjectConfValue 'ttsPort' 9880)
  }
  foreach ($entry in $ports.GetEnumerator()) {
    $hit = @($listeners | Where-Object { $_.LocalPort -eq [int]$entry.Value })
    Write-Output ('{0,-12} {1,-6} {2}' -f $entry.Key, $entry.Value, $(if ($hit.Count) { 'LISTENING' } else { 'DOWN' }))
  }
  foreach ($name in $taskNames) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    $state = if ($task) { [string]$task.State } else { 'MISSING' }
    Write-Output ('{0,-28} {1}' -f $name, $state)
  }
  # mind.py is a single cycle: zero running processes is not a health failure.
  $leasePath = Join-Path $runtimeDir 'scheduler-in-plugin.flag'
  if (Test-Path -LiteralPath $leasePath) {
    $age = [int]((Get-Date) - (Get-Item -LiteralPath $leasePath).LastWriteTime).TotalSeconds
    Write-Output ('Plugin scheduler marker age: ' + $age + ' seconds (legacy markers alone do not prove liveness)')
  }
}

function Change-Tasks([string]$Operation) {
  foreach ($name in $taskNames) {
    if ($name -eq 'DSH-Lanya-Mind' -and $Operation -eq 'start') { continue } # Legacy duplicate; not a default launch target.
    if (-not (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)) { continue }
    if ($Operation -eq 'start') { Start-ScheduledTask -TaskName $name }
    else { Stop-ScheduledTask -TaskName $name }
    Write-Output ($Operation + ' ' + $name)
  }
}

switch ($Action) {
  'status' { Show-Status }
  'logs' {
    foreach ($name in @('groupwatch.log', 'mind.log', 'lanya-supervisor.log', 'start-all.log')) {
      Write-Output $name
      $logFile = Join-Path $runtimeDir $name
      if (Test-Path -LiteralPath $logFile) { Get-Content -LiteralPath $logFile -Tail 5 -Encoding UTF8 }
    }
  }
  'start' { Change-Tasks 'start'; Show-Status }
  'stop' { Change-Tasks 'stop' }
  'restart' { Change-Tasks 'stop'; Start-Sleep -Seconds 2; Change-Tasks 'start'; Show-Status }
}
