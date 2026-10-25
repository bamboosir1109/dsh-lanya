# Serve Dafeiyu's works over HTTP so she can hand over a clickable link.
#
# Why: she writes little games into her made\ folder, and a link is far nicer than an
# attachment - it opens in the browser, works from QQ, and cannot be mistaken for
# a suspicious file.
#
# Scope caveat, worth knowing: this binds to 127.0.0.1, so the link only resolves
# ON THIS MACHINE. It is meant for the owner, not for the fan group - a 127.0.0.1
# URL is unopenable for anyone else. If LAN access is ever wanted, change -Bind to
# 0.0.0.0 and use the machine's LAN IP instead, but that also exposes the folder to
# the local network, so it is deliberately not the default.
#
# ASCII-only source.

param(
  [int]$Port = 19388,
  [string]$Bind = '127.0.0.1'
)


# Repo root: <root>/napcat/xxx.ps1 -> <root>
$root = Split-Path -Parent $PSScriptRoot
$ErrorActionPreference = 'Continue'

# ---- shared project config ------------------------------------------------
# Reads <repo>\config\project.json. The helper carries the same defaults, so a
# missing or broken config file leaves behaviour exactly as it was.
$confHelper = Join-Path $PSScriptRoot 'project_conf.ps1'
if (Test-Path $confHelper) { . $confHelper }
if (-not (Get-Command Get-ProjectConfValue -ErrorAction SilentlyContinue)) {
  function Get-ProjectConfValue { param([string]$KeyPath, $Default = $null) return $Default }
}
# An explicit -Port still wins; the config only replaces the DEFAULT (19388).
if (-not $PSBoundParameters.ContainsKey('Port')) {
  $Port = [int](Get-ProjectConfValue 'panelStaticPort' 19388)
}
$dataDirName = [string](Get-ProjectConfValue 'dataDirName' ([string][char]0x5927 + [char]0x80A5 + [char]0x9C7C))

$dafeiyuDir = Join-Path $root $dataDirName   # "Dafeiyu"
$madeDir  = Join-Path $dafeiyuDir 'made'
$logPath  = Join-Path $dafeiyuDir 'made-server.log'

function Write-Log([string]$m) {
  $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m
  Add-Content -Path $logPath -Value $line -Encoding UTF8
  Write-Output $line
}

if (-not (Test-Path $madeDir)) { New-Item -ItemType Directory -Path $madeDir -Force | Out-Null }

# Already serving? Then there is nothing to do (the task repeats on a timer).
# ---------------------------------------------------------------------------
# Also keep the control panel server alive (port 19389).
#
# Same trick as above: this task already fires on a timer and exits when the port
# is busy, so piggy-backing the panel here means the panel gets resurrected within
# ten minutes of dying, with no new scheduled task and therefore no console window
# flashing. The panel server is what serves /api/* for the control page, because
# the DSH web server on 19387 refuses cross-origin writes.
# ---------------------------------------------------------------------------
$panelPort = [int](Get-ProjectConfValue 'panelPort' 19389)
# The file got renamed at some point; a tree installed before that still
# has the old name. Try both rather than silently falling back to plain
# http.server, which serves the page but none of the API.
$panelScript = Join-Path $PSScriptRoot 'dafeiyu-panel-server.py'
if (-not (Test-Path $panelScript)) {
  $panelScript = Join-Path $PSScriptRoot 'lanya-panel-server.py'
}
if (Test-Path $panelScript) {
  $panelBusy = (netstat -ano | Select-String (':' + $panelPort + ' .*LISTENING')).Count -gt 0
  if ($panelBusy) {
    Write-Log ('panel server already on ' + $panelPort + ', skip')
  }
  else {
    Write-Log ('starting panel server on ' + $panelPort)
    Start-Process -FilePath 'python' -ArgumentList ('"' + $panelScript + '"') -WindowStyle Hidden
    for ($j = 1; $j -le 8; $j++) {
      Start-Sleep -Seconds 1
      if ((netstat -ano | Select-String (':' + $panelPort + ' .*LISTENING')).Count -gt 0) {
        Write-Log ('panel up after ' + $j + 's')
        break
      }
    }
  }
}

$busy = (netstat -ano | Select-String (':' + $Port + ' .*LISTENING')).Count -gt 0
if ($busy) { Write-Log ('already listening on ' + $Port + ', skip'); exit 0 }

Write-Log ('serving ' + $madeDir + ' at http://' + $Bind + ':' + $Port + '/')

# python -m http.server keeps running in the foreground; started detached so this
# script can exit and the server survives.
  # (translated above)
  # (translated above)
  $panel = Join-Path $PSScriptRoot 'dafeiyu-panel-server.py'
if (-not (Test-Path $panel)) { $panel = Join-Path $PSScriptRoot 'lanya-panel-server.py' }
  if (Test-Path $panel) {
    Start-Process -FilePath 'python' -ArgumentList ('"' + $panel + '" --port ' + $Port) -WindowStyle Hidden
  }
  else {
    $args = '-m http.server ' + $Port + ' --bind ' + $Bind + ' --directory "' + $madeDir + '"'
    Start-Process -FilePath 'python' -ArgumentList $args -WindowStyle Hidden
  }

for ($i = 1; $i -le 10; $i++) {
  Start-Sleep -Seconds 1
  if ((netstat -ano | Select-String (':' + $Port + ' .*LISTENING')).Count -gt 0) {
    Write-Log ('up after ' + $i + 's')
    exit 0
  }
}

Write-Log ('failed to come up on ' + $Port)
exit 1