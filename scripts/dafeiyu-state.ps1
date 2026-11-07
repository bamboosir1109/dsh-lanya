# Write the control panel state as a plain JSON file.
#
# Why a file: the DSH web app is a SPA and swallows any path it does not own, so
# /dsh-lanya/ renders its own empty shell in a browser even though the server
# returns correct HTML. The works server (19388) serves real files fine, so the
# panel lives there - and reading a sibling file keeps everything same-origin,
# which is why no CORS handshake is involved.
#
# ASCII-only source.

$ErrorActionPreference = 'Continue'

# ---- shared project config ------------------------------------------------
# Reads <repo>\config\project.json. The helper carries the same defaults, so a
# missing or broken config file leaves behaviour exactly as it was.
$confHelper = Join-Path $PSScriptRoot 'project_conf.ps1'
if (Test-Path $confHelper) { . $confHelper }
if (-not (Get-Command Get-ProjectConfValue -ErrorAction SilentlyContinue)) {
  function Get-ProjectConfValue { param([string]$KeyPath, $Default = $null) return $Default }
}
$worksPort      = [int](Get-ProjectConfValue 'panelStaticPort' 19388)
$napcatPort     = [int](Get-ProjectConfValue 'onebotPort' 8720)
$ttsPort        = [int](Get-ProjectConfValue 'ttsPort' 9880)
$autoUpdateMins = [int](Get-ProjectConfValue 'daemon.autoUpdateMinutes' 360)
$dataDirName    = [string](Get-ProjectConfValue 'dataDirName' ([string][char]0x5927 + [char]0x80A5 + [char]0x9C7C))

$root     = $PSScriptRoot
$dafeiyuDir = Join-Path (Split-Path -Parent $PSScriptRoot) $dataDirName
$outDir   = Join-Path $dafeiyuDir 'made'
$outFile  = Join-Path $outDir 'lanya-state.json'
$legacyOutFile = Join-Path $outDir 'dafeiyu-state.json'

$profDir = (Join-Path $env:USERPROFILE '.dsh\profiles\desktop')
$nmDir   = Join-Path $profDir 'node_modules'
$pkgFile = Join-Path $profDir 'package.json'

$limitMb = 3000
$strayAgeMin = 6

$components = @(
  @{ tag = 'groupwatch';  match = 'groupwatch\.ps1';    desc = 'group watcher' },
  @{ tag = 'lifeloop';    match = 'lifeloop\.ps1';      desc = 'free time loop' },
  @{ tag = 'voicedaemon'; match = 'voicedaemon\.ps1';   desc = 'voice queue' },
  @{ tag = 'browserd';    match = 'browserd\.py';       desc = 'browser' },
  @{ tag = 'works';       match = '__WORKS__'; desc = ('works server ' + $worksPort) },
  @{ tag = 'tts';         match = 'api_v2\.py';         desc = ('local voice ' + $ttsPort) },
  @{ tag = 'napcat';      match = ('__PORT__:' + $napcatPort);      desc = 'napcat injection' },
  @{ tag = 'headless';    match = '--profile headless'; desc = 'headless agent' }
)

function Get-WorksServiceRow($Processes, $Listeners, $Health, [string]$ExpectedScript, [int]$Port, [string]$ExpectedHash) {
  $escaped = [regex]::Escape($ExpectedScript)
  $pattern = '^(?:"[^"]+"|[^\s"]+)(?:\s+(?<pythonflag>-B))?\s+(?:"' + $escaped + '"|' + $escaped + ')\s+--port\s+' + $Port + '\s*$'
  $hits = @($Processes | Where-Object {
    if($_.Name -notmatch '^pythonw?(\.exe)?$') { return $false }
    $matched = [regex]::Match([string]$_.CommandLine, $pattern, 'IgnoreCase')
    return ($matched.Success -and (-not $matched.Groups['pythonflag'].Success -or $matched.Groups['pythonflag'].Value -ceq '-B'))
  })
  $mb = 0.0; foreach($hit in $hits) { $mb += $hit.WorkingSetSize / 1MB }
  $owners = @($Listeners | Where-Object { $_.LocalPort -eq $Port } | Select-Object -ExpandProperty OwningProcess -Unique)
  $status = if($null -eq $Listeners){'unknown'}elseif($owners.Count){'blocked'}elseif($hits.Count){'starting'}else{'offline'}
  $healthy = $false
  if($hits.Count -eq 1 -and $owners.Count -eq 1 -and $owners[0] -eq $hits[0].ProcessId) {
    $status = 'unhealthy'
    if($Health -and $Health.ok -is [bool] -and $Health.ok -and $Health.version -is [int] -and $Health.version -eq 1 -and
       $Health.service -ceq 'lanya-panel' -and $Health.role -ceq 'works' -and $Health.port -eq $Port -and
       $Health.pid -is [int] -and $Health.port -is [int] -and $Health.pid -eq $hits[0].ProcessId -and
       $ExpectedHash -cmatch '^[a-f0-9]{64}$' -and $Health.script_sha256 -ceq $ExpectedHash) { $healthy=$true; $status='healthy' }
  }
  return [pscustomobject]@{tag='works';desc=('works server '+$Port);count=$hits.Count;mb=[math]::Round($mb,1);aging=0;
    service=[pscustomobject]@{role='works';port=$Port;count=$hits.Count;mb=[math]::Round($mb,1);healthy=$healthy;status=$status;listening=($owners.Count -gt 0)}}
}

$me = $PID
$procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
  $_.ProcessId -ne $me -and $_.CommandLine
})

$rows = @()
$total = 0.0
$cutoff = (Get-Date).AddMinutes(-$strayAgeMin)

foreach ($c in $components) {
  if($c.tag -eq 'works') {
    $worksScript = Join-Path $PSScriptRoot 'lanya-panel-server.py'
    $listeners = $null; $health = $null; $expectedHash = ''
    try { $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalPort -eq $worksPort }) } catch { }
    try { $expectedHash = (Get-FileHash -LiteralPath $worksScript -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant() } catch { }
    $row = Get-WorksServiceRow $procs $listeners $null $worksScript $worksPort $expectedHash
    if($row.service.status -eq 'unhealthy') {
      try {
        $response = Invoke-WebRequest -Uri ('http://127.0.0.1:'+$worksPort+'/api/service-health') -UseBasicParsing -TimeoutSec 2 -MaximumRedirection 0 -ErrorAction Stop
        if($response.StatusCode -eq 200 -and $response.Content.Length -le 4096) { $health = $response.Content | ConvertFrom-Json -ErrorAction Stop }
      } catch { }
      $row = Get-WorksServiceRow $procs $listeners $health $worksScript $worksPort $expectedHash
    }
    $rows += $row; $total += $row.mb
    continue
  }
  # NapCat is injected into the QQ process, so there is no process name to match.
  # Looking for napimain always yielded 0 and made the panel look offline. Probe
  # the port instead - that reflects what is actually reachable.
  if ($c.match -like '__PORT__:*') {
    $port = $c.match.Split(':')[1]
    $alive = (netstat -ano | Select-String (':' + $port + ' .*LISTENING')).Count -gt 0
    $rows += [pscustomobject]@{
      tag   = $c.tag
      desc  = $c.desc
      count = if ($alive) { 1 } else { 0 }
      mb    = 0
      aging = 0
      note  = if ($alive) { 'listen ' + $port } else { 'offline' }
    }
    continue
  }
  $re = [regex]::new($c.match, 'IgnoreCase')
  $hits = @($procs | Where-Object { $re.IsMatch([string]$_.CommandLine) })
  $mb = 0.0
  foreach ($h in $hits) { $mb += ($h.WorkingSetSize / 1MB) }
  $total += $mb

  $aging = 0
  if ($c.tag -eq 'headless') {
    $aging = @($hits | Where-Object { $_.CreationDate -lt $cutoff }).Count
  }

  $rows += [pscustomobject]@{
    tag   = $c.tag
    desc  = $c.desc
    count = $hits.Count
    mb    = [math]::Round($mb, 1)
    aging = $aging
  }
}

# ---- plugins -------------------------------------------------------------
# Read from the profile's bundle list and each package's own package.json, so the
# panel shows real versions and real upstream URLs rather than a hand-kept table.
$knownSources = @{
  'dsh-bili-whale' = 'https://github.com/Alterssss/dsh-bili-whale'
  'dsh-lanya'      = 'https://github.com/upstream-author/dsh-lanya'
}

$plugins = @()
if (Test-Path $pkgFile) {
  try {
    $prof = Get-Content $pkgFile -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($b in @($prof.dsh.profile.bundles)) {
      $pj = Join-Path (Join-Path $nmDir ($b -replace '/', '\')) 'package.json'
      $ver = '?'
      $repo = ''
      $desc = ''
      if (Test-Path $pj) {
        try {
          $p = Get-Content $pj -Raw -Encoding UTF8 | ConvertFrom-Json
          if ($p.version) { $ver = [string]$p.version }
          if ($p.description) { $desc = [string]$p.description }
          if ($p.repository) {
            $repo = if ($p.repository -is [string]) { [string]$p.repository } else { [string]$p.repository.url }
          }
        } catch { }
      }
      $url = ($repo -replace '^git\+', '') -replace '\.git$', ''
      if ((-not $url) -and $knownSources.ContainsKey($b)) { $url = $knownSources[$b] }
      $plugins += [pscustomobject]@{
        name    = $b
        version = $ver
        source  = $url
        desc    = $desc
      }
    }
  } catch { }
}

# ---- scheduled tasks -------------------------------------------------------
# The plugin scheduler already does some of these jobs, but the old tasks are
# still registered and doing them a second time. Count them here so the panel can
# say "N left to remove" instead of making the user go look.
$redundantTasks = @(
  'DSH-Reminders', 'DSH-Dafeiyu-Budget', 'DSH-Dafeiyu-Chats',
  'DSH-Dafeiyu-Steal', 'DSH-Dafeiyu-Greet', 'NapCat-DSH-Bridge'
)
$existing = @()
try {
  $csv = & schtasks.exe /query /fo CSV 2>&1 | ConvertFrom-Csv -ErrorAction SilentlyContinue
  $existing = @($csv | Where-Object { $_.TaskName } | Select-Object -ExpandProperty TaskName -Unique)
} catch { }
$stillThere = @()
foreach ($rt in $redundantTasks) {
  $hit = $existing | Where-Object { $_ -match [regex]::Escape($rt) }
  if ($hit) { $stillThere += $rt }
}

$os = Get-CimInstance Win32_OperatingSystem

$state = [pscustomobject]@{
  at     = (Get-Date).ToString('o')
  memory = [pscustomobject]@{
    dafeiyuMb = [math]::Round($total, 1)
    lanyaMb = [math]::Round($total, 1)
    limitMb = $limitMb
    over    = ($total -gt $limitMb)
    freeGb  = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
    totalGb = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
  }
  components = $rows
  plugins    = $plugins
  tasks      = [pscustomobject]@{ redundant = $stillThere.Count; names = $stillThere }
}

# Also drop a copy of the persona next to the panel. The panel reads it with a
# same-origin relative path: reading it over the API from the 19388 origin fails,
# because the browser sends an OPTIONS preflight and that route answers 400.
try {
  $personaPath = (Join-Path $PSScriptRoot ($dataDirName + '-' + [char]0x4EBA + [char]0x8BBE + [char]0x6B63 + [char]0x6587 + '.md'))
} catch { $personaPath = '' }
try {
  New-Item -ItemType Directory -Path $outDir -Force | Out-Null
  if ($personaPath -and (Test-Path $personaPath)) {
    Copy-Item $personaPath (Join-Path $outDir 'dafeiyu-persona.txt') -Force
  }
} catch { }

try {
  New-Item -ItemType Directory -Path $outDir -Force | Out-Null
  $json = $state | ConvertTo-Json -Depth 6 -Compress
  [System.IO.File]::WriteAllText($outFile, $json, (New-Object System.Text.UTF8Encoding($false)))
  [System.IO.File]::WriteAllText($legacyOutFile, $json, (New-Object System.Text.UTF8Encoding($false)))
}
catch {
  # Never break whatever called this.
}

# Auto-update nudge: try a GitHub pull every 6 hours.
#
# Throttled with a stamp file so the 30s tick does not hammer git.
try {
  $auScript = Join-Path $PSScriptRoot 'auto-update.ps1'
  $auStamp  = Join-Path $PSScriptRoot 'auto-update.lastrun'
  if (Test-Path $auScript) {
    $auDue = $true
    if (Test-Path $auStamp) {
      $auMins = ((Get-Date) - (Get-Item $auStamp).LastWriteTime).TotalMinutes
      if ($auMins -lt $autoUpdateMins) { $auDue = $false }
    }
    if ($auDue) {
      Set-Content -Path $auStamp -Value (Get-Date -Format 'o') -Encoding UTF8
      Start-Process -FilePath 'powershell' -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + $auScript + '"') -WindowStyle Hidden
    }
  }
} catch { }

# Also nudge the meme classifier along: one small batch at a time, then exit.
#
# A resident classifier would hold a headless agent for minutes at a stretch and
# fight the stray killer's concurrency cap. Eight images per nudge is small enough
# to look like any other scheduled job, and because classified files leave the flat
# folder, progress needs no checkpoint file.
#
# Throttled to once every 3 minutes so it does not run on every 30s tick.
try {
  $cScript = Join-Path $PSScriptRoot 'classify_emotion.py'
  $cStamp  = Join-Path $PSScriptRoot 'classify.lastrun'
  if (Test-Path $cScript) {
    $due = $true
    if (Test-Path $cStamp) {
      $mins = ((Get-Date) - (Get-Item $cStamp).LastWriteTime).TotalMinutes
      if ($mins -lt 2) { $due = $false }
    }
    if ($due) {
      $dafeiyuMemes = Join-Path (Join-Path (Split-Path -Parent $PSScriptRoot) $dataDirName) 'memes'
      $flat = @(Get-ChildItem (Join-Path $dafeiyuMemes 'stolen') -File -ErrorAction SilentlyContinue)
      if ($flat.Count -gt 0) {
        Set-Content -Path $cStamp -Value (Get-Date -Format 'o') -Encoding UTF8
        Start-Process -FilePath 'python' -ArgumentList ('"' + $cScript + '" --limit 20') -WindowStyle Hidden
      }
    }
  }
} catch { }

# Also refresh the static snapshots the panel reads. They are same-origin files,
# which is what makes the panel work inside an embedded view that blocks
# cross-port fetches. Cheap: it just re-serialises what is already in memory.
try {
  $snap = Join-Path $PSScriptRoot 'make_snapshots.py'
  if (Test-Path $snap) {
    Start-Process -FilePath 'python' -ArgumentList ('"' + $snap + '"') -WindowStyle Hidden -Wait
  }
} catch { }
