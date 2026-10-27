# Start every runtime piece of Lanya. Idempotent: if something is already up it
# is left alone. Called at logon from the Startup folder, and safe to run by hand.
#
# Everything here runs hidden - no console windows popping up at login.

$ErrorActionPreference = 'SilentlyContinue'
$root = "<WORKDIR>\napcat"
$log = Join-Path $root 'start-all.log'

function Note($msg) {
  $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $msg
  try { Add-Content -Path $log -Value $line -Encoding UTF8 } catch { }
}

function Test-Port($p) {
  return ((netstat -ano | Select-String (':' + $p + ' .*LISTENING')).Count -gt 0)
}

function Test-Script($name) {
  $c = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -like ('*' + $name + '*') -and
    $_.CommandLine -notlike '*Get-CimInstance*'
  }
  return ($c | Measure-Object).Count -gt 0
}

# NOTE: never name a parameter $args - it is a PowerShell automatic
# variable and the caller's value is silently ignored.
function Spawn($exe, $argv, $label) {
  try {
    Start-Process -FilePath $exe -ArgumentList $argv -WindowStyle Hidden
    Note ('started ' + $label)
  } catch {
    Note ('FAILED to start ' + $label + ': ' + $_.Exception.Message)
  }
}

Note '--- start-all begin ---'

# 1. Panel API (19389) and the static one (19388). Both are the same script.
if (-not (Test-Port 19389)) {
  Spawn 'python' @((Join-Path $root 'lanya-panel-server.py')) 'panel-api:19389'
}
if (-not (Test-Port 19388)) {
  Spawn 'python' @((Join-Path $root 'lanya-panel-server.py'), '--port', '19388') 'panel-static:19388'
}

# 2. Supervisor - it is what drives budget, chats, steal, greet, memory, bili.
#    Without it nothing periodic happens at all.
if (-not (Test-Script 'lanya-supervisor.ps1')) {
  Spawn 'powershell.exe' @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden',
        '-File', (Join-Path $root 'lanya-supervisor.ps1')) 'supervisor'
}

# 3. The long-running daemons.
$daemons = @(
  @{ n = 'groupwatch.ps1';  f = 'groupwatch.ps1' },
  @{ n = 'lifeloop.ps1';    f = 'lifeloop.ps1' },
  @{ n = 'voicedaemon.ps1'; f = 'voicedaemon.ps1' },
  @{ n = 'browserd.py';     f = 'browserd.py'; py = $true },
  @{ n = 'reminders.ps1';   f = 'reminders.ps1' }
)
foreach ($d in $daemons) {
  if (Test-Script $d.f) { continue }
  $p = Join-Path $root $d.f
  if (-not (Test-Path $p)) { Note ('missing: ' + $d.f); continue }
  if ($d.py) {
    Spawn 'C:\Python314\python.exe' @($p) $d.f
  } else {
    Spawn 'powershell.exe' @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden',
          '-File', $p) $d.f
  }
}

Note '--- start-all done ---'
