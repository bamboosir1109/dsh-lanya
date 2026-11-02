# One read-only terminal. No service control, message sending, model calls or
# helper processes. ASCII source also works in Windows PowerShell 5.1.
[CmdletBinding()]
param(
    [string]$Root,
    [switch]$Once
)

$ErrorActionPreference = 'Stop'
$script:MonitorTimeoutMs = 750

function Read-MonitorJson([string]$Path, [int]$Limit = 65536) {
    $stream = $null
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read,
            ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
        if ($stream.Length -gt $Limit) { return $null }
        $length = [int]$stream.Length
        $buffer = [byte[]]::new($length)
        $offset = 0
        while ($offset -lt $length) {
            $count = $stream.Read($buffer, $offset, $length - $offset)
            if ($count -eq 0) { return $null }
            $offset += $count
        }
        if ($stream.Length -ne $length) { return $null }
        $raw = ([Text.UTF8Encoding]::new($false, $true)).GetString($buffer).TrimStart([char]0xFEFF)
        if (-not $raw.TrimStart().StartsWith('{')) { return $null }
        $value = ConvertFrom-Json -InputObject $raw -ErrorAction Stop
        if ($value -isnot [pscustomobject]) { return $null }
        return $value
    } catch { return $null }
    finally { if ($null -ne $stream) { $stream.Dispose() } }
}

function Get-MonitorHttp([int]$Port, [string]$Endpoint) {
    # Fixed loopback host and fixed caller-owned routes. No proxy, credentials,
    # redirection or unbounded response; no requests overlap in this process.
    $response = $null
    $stream = $null
    $deadline = [Diagnostics.Stopwatch]::StartNew()
    try {
        $request = [Net.HttpWebRequest]::Create('http://127.0.0.1:' + $Port + $Endpoint)
        $request.Method = 'GET'
        $request.Proxy = $null
        $request.AllowAutoRedirect = $false
        $request.UseDefaultCredentials = $false
        $request.Timeout = $script:MonitorTimeoutMs
        $request.ReadWriteTimeout = $script:MonitorTimeoutMs
        $request.KeepAlive = $false
        $response = $request.GetResponse()
        if ([int]$response.StatusCode -ne 200 -or $response.ContentLength -gt 16384) { return $null }
        $stream = $response.GetResponseStream()
        $buffer = [byte[]]::new(16385)
        $offset = 0
        while ($offset -lt $buffer.Length) {
            $remaining = $script:MonitorTimeoutMs - $deadline.ElapsedMilliseconds
            if ($remaining -le 0) { return $null }
            if ($stream.CanTimeout) { $stream.ReadTimeout = [int]$remaining }
            $count = $stream.Read($buffer, $offset, $buffer.Length - $offset)
            if ($count -eq 0) { break }
            $offset += $count
        }
        if ($offset -gt 16384 -or $deadline.ElapsedMilliseconds -gt $script:MonitorTimeoutMs) { return $null }
        $raw = ([Text.UTF8Encoding]::new($false, $true)).GetString($buffer, 0, $offset)
        $value = ConvertFrom-Json -InputObject $raw -ErrorAction Stop
        if ($value -isnot [pscustomobject]) { return $null }
        return $value
    } catch { return $null }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
        if ($null -ne $response) { $response.Dispose() }
    }
}

function Test-MonitorPort([int]$Port) {
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $attempt = $client.ConnectAsync('127.0.0.1', $Port)
        if (-not $attempt.Wait($script:MonitorTimeoutMs)) { return $false }
        return $client.Connected
    } catch { return $false }
    finally { $client.Dispose() }
}

function Get-MonitorPort($Config, [string]$Name) {
    $number = $Config.$Name
    if (($number -is [int] -or $number -is [long]) -and $number -ge 1 -and $number -le 65535) {
        return [int]$number
    }
    return 0
}

function Get-MonitorContext([string]$WorkRoot) {
    $candidates = @()
    if ($env:LANYA_PROJECT_CONFIG) { $candidates += $env:LANYA_PROJECT_CONFIG }
    $candidates += (Join-Path $WorkRoot 'napcat\config\project.json')
    $candidates += (Join-Path $WorkRoot 'config\project.json')
    $selected = $null
    foreach ($candidate in $candidates) {
        if ([IO.File]::Exists($candidate)) { $selected = $candidate; break }
        if ($env:LANYA_PROJECT_CONFIG -and $candidate -eq $env:LANYA_PROJECT_CONFIG) { break }
    }
    $config = if ($selected) { Read-MonitorJson $selected } else { $null }
    # Never guess a character/data folder, and never follow a path supplied in
    # place of its configured single folder name.
    $name = $config.dataDirName
    $data = $null
    if ($name -is [string] -and $name.Length -gt 0 -and $name.Length -le 128 -and
        $name -notin @('.', '..') -and $name.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -lt 0 -and
        -not $name.Contains('\') -and -not $name.Contains('/')) {
        $data = Join-Path $WorkRoot $name
    }
    if (-not $data) { $config = $null }
    return [pscustomobject]@{
        Config = $config; Data = $data
        ApiPort = (Get-MonitorPort $config 'panelPort')
        StaticPort = (Get-MonitorPort $config 'panelStaticPort')
        QqPort = (Get-MonitorPort $config 'onebotPort')
    }
}

function Get-MonitorVersion([string]$WorkRoot) {
    $sourceVersion = 'unknown'
    $installedVersion = 'unknown'
    $installed = if ($env:USERPROFILE) {
        Join-Path $env:USERPROFILE '.dsh\profiles\desktop\node_modules\dsh-lanya\package.json'
    } else { $null }
    if ($installed -and [IO.File]::Exists($installed)) {
        $package = Read-MonitorJson $installed
        if ($package.name -ceq 'dsh-lanya' -and $package.version -is [string] -and
            $package.version -cmatch '^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$') {
            $installedVersion = $package.version
        }
    }
    foreach ($file in @((Join-Path $WorkRoot 'dsh-lanya\package.json'), (Join-Path $WorkRoot 'package.json'))) {
        if (-not [IO.File]::Exists($file)) { continue }
        $package = Read-MonitorJson $file
        if ($package.name -ceq 'dsh-lanya' -and $package.version -is [string] -and
            $package.version -cmatch '^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$') {
            $sourceVersion = $package.version
            break
        }
    }
    return [pscustomobject]@{ Source = $sourceVersion; Installed = $installedVersion }
}

function Get-MonitorAge($At) {
    if (($At -isnot [long] -and $At -isnot [int]) -or $At -lt 0 -or $At -gt 9007199254740991) { return $null }
    return ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() - $At) / 1000.0
}

function Get-MonitorWatch($Context) {
    if (-not $Context.Data) { return 'unknown (configuration unavailable)' }
    $watch = Read-MonitorJson (Join-Path $Context.Data 'main-agent-watch.status.json')
    $age = Get-MonitorAge $watch.at
    if ($watch.schema -cne 'dsh-main-agent-watch-status/v1' -or
        $watch.state -notin @('healthy', 'waiting', 'starting', 'blocked', 'paused', 'error') -or $null -eq $age) {
        return 'unknown (status unavailable)'
    }
    if ($age -lt -5 -or $age -gt 60) { return 'stale (last heartbeat outside 60 seconds)' }
    return 'reported ' + $watch.state + ' (owner unverified; heartbeat ' + [int][Math]::Max(0, $age) + 's ago)'
}

function Get-MonitorPanel([int]$Port, [string]$Role) {
    if (-not $Port) { return 'unknown (configuration unavailable)' }
    $health = Get-MonitorHttp $Port '/api/service-health'
    if ($health.ok -is [bool] -and $health.ok -and $health.service -ceq 'lanya-panel' -and
        $health.version -is [int] -and $health.version -eq 1 -and $health.port -is [int] -and
        $health.port -eq $Port -and $health.role -ceq $Role -and
        ($health.pid -is [int] -or $health.pid -is [long]) -and $health.pid -gt 0 -and
        $health.script_sha256 -is [string] -and $health.script_sha256 -cmatch '^[a-f0-9]{64}$') {
        return 'HTTP ready (identity not independently verified)'
    }
    return 'unknown (health response unavailable or invalid)'
}

function Get-MonitorBudgetTask {
    try {
        # Native ScheduledTasks cmdlet in this same PowerShell process. This
        # reads the existing task only; it never runs or re-registers it.
        $info = Get-ScheduledTaskInfo -TaskName 'DSH-Lanya-Budget' -ErrorAction Stop
        if ($info.LastRunTime -isnot [datetime] -or $info.LastRunTime.Year -lt 2000 -or
            ($info.LastTaskResult -isnot [int] -and $info.LastTaskResult -isnot [uint32] -and
             $info.LastTaskResult -isnot [long])) { return 'unknown' }
        return 'last run ' + $info.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') + '; result ' + $info.LastTaskResult
    } catch { return 'unknown (task information unavailable)' }
}

function Get-MonitorSnapshot([string]$WorkRoot) {
    $context = Get-MonitorContext $WorkRoot
    $main = 'unknown'
    $autonomy = 'unknown'
    $watch = Get-MonitorWatch $context
    $markerAge = $null
    $marker = Read-MonitorJson (Join-Path $WorkRoot 'napcat\scheduler-in-plugin.flag')
    if ($marker.version -ceq 2 -and $marker.jobs -is [array] -and $marker.jobs -contains 'mind') {
        $markerAge = Get-MonitorAge $marker.at
        if ($null -ne $markerAge -and $markerAge -ge -5 -and $markerAge -le 120) {
            $main = 'heartbeat fresh (owner unverified)'
        } elseif ($null -ne $markerAge) { $main = 'heartbeat stale' }
    }
    if ($context.ApiPort) {
        $value = Get-MonitorHttp $context.ApiPort '/api/autonomy'
        $runtime = $value.autonomy
        $allowedStates = @('unknown', 'offline', 'waiting', 'scheduled', 'processing', 'uncertain', 'legacy')
        if ($value.ok -is [bool] -and $value.ok -and $runtime.mode -cin @('unknown', 'unified', 'legacy') -and
            $runtime.status -cin $allowedStates) {
            $autonomy = $runtime.status
            if ($runtime.mode -ceq 'unified' -and $runtime.scheduler_verified -is [bool] -and $runtime.scheduler_verified -and
                $runtime.consumer_verified -is [bool] -and $runtime.consumer_verified -and
                $runtime.can_request_check -is [bool] -and $runtime.can_request_check -and
                $runtime.status -cin @('waiting', 'scheduled', 'processing', 'uncertain')) {
                $main = 'scheduler + consumer verified by panel API'
            } elseif ($runtime.status -ceq 'offline') { $main = 'offline (reported by panel API)' }
            if ($runtime.watch.configured -is [bool] -and $runtime.watch.configured -and $runtime.watch.status -cin
                @('not_configured', 'unknown', 'offline', 'healthy', 'waiting', 'starting', 'blocked', 'paused', 'error')) {
                $watch = $runtime.watch.status + ' (reported by panel API)'
            }
        }
    }
    $qq = if (-not $context.QqPort) { 'unknown (configuration unavailable)' }
        elseif (Test-MonitorPort $context.QqPort) { 'port reachable; message delivery unverified' }
        else { 'port unreachable; message delivery unverified' }
    $api = Get-MonitorPanel $context.ApiPort 'api'
    $works = Get-MonitorPanel $context.StaticPort 'works'
    $version = Get-MonitorVersion $WorkRoot
    return @(
        'DSH Monitor  |  read-only  |  refresh every 5 seconds',
        ('Updated : ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')),
        ('Source  : ' + $version.Source),
        ('Installed: ' + $version.Installed),
        'Loaded  : unverified (no process loading proof)',
        ('Main    : ' + $main),
        ('Watch   : ' + $watch),
        ('QQ      : ' + $qq),
        ('Autonomy: ' + $autonomy),
        ('Panel API: ' + $api),
        ('Panel UI : ' + $works),
        ('Budget task: ' + (Get-MonitorBudgetTask)),
        'Ctrl+C or closing this window stops only this monitor.'
    )
}

function Show-MonitorSnapshot([string[]]$Lines, [bool]$Initial) {
    if ($Once -or [Console]::IsOutputRedirected) { $Lines | Write-Output; return }
    try {
        if ($Initial) { [Console]::Clear() }
        [Console]::SetCursorPosition(0, 0)
        $width = [Math]::Max(20, [Console]::WindowWidth - 1)
        foreach ($line in $Lines) {
            # Pad old characters away without creating another console or
            # appending an unbounded stream of identical status messages.
            $visible = if ($line.Length -gt $width) { $line.Substring(0, $width) } else { $line }
            [Console]::WriteLine($visible.PadRight($width))
        }
    } catch { $Lines | Write-Output }
}

$mutex = $null
$owned = $false
$exitCode = 0
try {
    if (-not $Root) {
        if ($env:LANYA_WORKDIR) { $Root = $env:LANYA_WORKDIR }
        else {
            $layoutRoot = Split-Path -Parent $PSScriptRoot
            $Root = if ((Split-Path -Leaf $PSScriptRoot) -eq 'scripts') { Split-Path -Parent $layoutRoot } else { $layoutRoot }
        }
    }
    $workRoot = [IO.Path]::GetFullPath($Root)
    if (-not [IO.Directory]::Exists($workRoot)) { throw 'Unavailable root' }
    $canonicalRoot = $workRoot.TrimEnd([char[]]'\/').ToUpperInvariant()
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { $key = [BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($canonicalRoot))).Replace('-', '') }
    finally { $hasher.Dispose() }
    $mutex = [Threading.Mutex]::new($false, ('Local\DSH-Monitor-' + $key))
    try { $owned = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) {
        Write-Output 'DSH Monitor is already open for this workspace.'
        $exitCode = 2
    } else {
        try { $Host.UI.RawUI.WindowTitle = 'DSH Monitor' } catch { }
        $initial = $true
        do {
            $timer = [Diagnostics.Stopwatch]::StartNew()
            $lines = Get-MonitorSnapshot $workRoot
            Show-MonitorSnapshot $lines $initial
            $initial = $false
            if ($Once) { break }
            $remaining = [Math]::Max(0, 5000 - $timer.ElapsedMilliseconds)
            if ($remaining -gt 0) { Start-Sleep -Milliseconds $remaining }
        } while ($true)
    }
} catch [System.Management.Automation.PipelineStoppedException] {
    # Ctrl+C only leaves this read-only loop; finally releases the mutex.
} catch {
    Write-Output 'DSH Monitor unavailable. Check the workspace configuration.'
    $exitCode = 1
} finally {
    if ($owned -and $null -ne $mutex) { $mutex.ReleaseMutex() }
    if ($null -ne $mutex) { $mutex.Dispose() }
}
exit $exitCode
