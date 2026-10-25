# NapCat heal - replaces BOTH old QQ-side scheduled tasks.
#
# They were two tasks doing overlapping work (a "watchdog" every 20 min and a
# "bridge keeper" every 10 min), and the watchdog was worse than useless: it
# pointed at a launcher that had already been deleted, so every one of its runs
# did nothing at all while looking like it was watching.
#
# This single pass does what both were supposed to do:
#   1. is NapCat listening (8720 / 6099)?
#   2. is the account still logged in?
#   3. if not, try to bring it back
#   4. log one line, stay silent when healthy
#
# Elevation: napimain needs admin. When run from the supervisor (not elevated) a
# relaunch cannot work; that case is logged plainly instead of faking success.
#
# ASCII-only source.

$ErrorActionPreference = 'Continue'

$root = '<WORKDIR>\napcat'
$fw   = Join-Path $root 'framework'
$log  = Join-Path $root 'napcat-heal.log'

function Note([string]$m) {
  $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
  try { Add-Content -Path $log -Value $line -Encoding UTF8 } catch { }
  Write-Host $line
}

function PortUp([int]$p) {
  $hit = netstat -ano | Select-String (':' + $p + ' .*LISTENING') | Select-Object -First 1
  return ($null -ne $hit)
}

function LoggedIn() {
  try {
    Add-Type -AssemblyName System.Net.Http -ErrorAction SilentlyContinue
    $cfg = Get-Content (Join-Path $root 'balance-alert.config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $c = New-Object System.Net.Http.HttpClient
    $c.Timeout = [TimeSpan]::FromSeconds(8)
    $c.DefaultRequestHeaders.Authorization = New-Object System.Net.Http.Headers.AuthenticationHeaderValue('Bearer', [string]$cfg.napcatToken)
    $ct = New-Object System.Net.Http.StringContent('{}', [System.Text.Encoding]::UTF8, 'application/json')
    $url = ([string]$cfg.napcatHttp).TrimEnd('/') + '/get_login_info'
    $raw = $c.PostAsync($url, $ct).Result.Content.ReadAsStringAsync().Result
    $r = $raw | ConvertFrom-Json
    return ($r.status -eq 'ok' -and $r.data.user_id)
  }
  catch { return $false }
}

$up8720 = PortUp 8720
$up6099 = PortUp 6099

if ($up8720 -and $up6099) {
  if (LoggedIn) { exit 0 }
  Note 'ports up but account NOT logged in (security logout?) - restarting'
}
else {
  Note ('NapCat down (8720=' + $up8720 + ' 6099=' + $up6099 + ') - trying to restart')
}

# Elevation is checked BEFORE anything is killed. The first version killed QQ and
# only then discovered it could not inject - which took a working client down and
# left nothing in its place.
$isAdmin = $false
try {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $pr = New-Object Security.Principal.WindowsPrincipal($id)
  $isAdmin = $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
} catch { }

if (-not $isAdmin) {
  Note 'not elevated - leaving QQ alone (cannot inject anyway); run the launcher as administrator'
  exit 2
}

$napimain = Join-Path $fw 'napimain.exe'
$loader   = Join-Path $fw 'napiloader.dll'
$native   = Join-Path $fw 'nativeLoader.cjs'
$qqexe    = $env:QQNT_EXE
if (-not $qqexe) { $qqexe = 'C:\Program Files\Tencent\QQNT\QQ.exe' }

foreach ($f in @($napimain, $loader, $native, $qqexe)) {
  if (-not (Test-Path $f)) { Note ('cannot restart, missing: ' + $f); exit 1 }
}

try { & taskkill.exe /IM QQ.exe /F 2>&1 | Out-Null } catch { }
Start-Sleep -Seconds 4

try {
  $nativeArg = $native -replace '\\', '/'
  Start-Process -FilePath $napimain -ArgumentList @($qqexe, $loader, $nativeArg) -WorkingDirectory $fw -ErrorAction Stop | Out-Null
  Note 'relaunch requested'
}
catch {
  $m = $_.Exception.Message
  if ($m.Length -gt 140) { $m = $m.Substring(0, 140) }
  Note ('relaunch failed: ' + $m)
  exit 3
}
