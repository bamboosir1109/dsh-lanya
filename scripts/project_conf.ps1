# Read the shared project config.
#
#   . "$PSScriptRoot\project_conf.ps1"
#   $c = Get-ProjectConf                       # nested hashtable, defaults filled in
#   $p = Get-ProjectConfValue 'panelStaticPort' 19388
#   $s = Get-ProjectConfValue 'daemon.pollSeconds' 60
#
# Defaults apply only when no candidate exists. An explicit missing override or
# an unreadable/invalid first existing file stops activity, never falls back to
# a lower-priority legacy sender configuration.
#
# ASCII-ONLY ON PURPOSE. Windows PowerShell 5.1 decodes a BOM-less .ps1 as ANSI,
# so any non-ASCII byte in here is a syntax error waiting to happen. The Chinese
# folder name is written as a char code instead.

$script:ProjectConfDefaults = @{
    panelPort       = 19389
    panelStaticPort = 19388
    onebotPort      = 8720
    ttsPort         = 9880
    panelFile       = 'panel.html'
    dataDirName     = ([string][char]0x5927 + [char]0x80A5 + [char]0x9C7C)   # "Dafeiyu"
    daemon          = @{
        pollSeconds        = 60
        headlessTimeoutSec = 360
        maxConcurrentAgents = 3
        autoUpdateMinutes  = 360
    }
    bili            = @{
        commentMaxLen   = 30
        watchCandidates = 4
        commentRetries  = 3
    }
    budget          = @{
        dailyLimit = 1.0
    }
    mind            = @{
        opportunityMin = 6
    }
    decisions       = @{
        mode = 'unified'
        agentPreset = ''
    }
}

$script:ProjectConfCache = $null
$script:ProjectConfSelectedPath = $null
$script:ProjectConfHere = $PSScriptRoot

function Get-ProjectWorkDir {
    if ($env:LANYA_WORKDIR) { return [IO.Path]::GetFullPath($env:LANYA_WORKDIR) }
    $project = $script:ProjectConfHere
    if ((Split-Path -Leaf $project) -in @('scripts', 'server')) {
        $project = Split-Path -Parent $project
    }
    return (Split-Path -Parent $project)
}

function Get-ProjectDataDir {
    return (Join-Path (Get-ProjectWorkDir) ([string](Get-ProjectConfValue 'dataDirName')))
}

function Get-ProjectConfCandidates {
    # Where project.json may live. Repo layout has scripts/ and server/ one level
    # below the root; the installed flat layout has the scripts in napcat/.
    # Try both, never hardcode a drive.
    $here = $script:ProjectConfHere
    $cands = @()
    if ($env:LANYA_PROJECT_CONFIG) { $cands += $env:LANYA_PROJECT_CONFIG }
    if ($here) {
        $workspace = Get-ProjectWorkDir
        $bases = @($here, $workspace)
        if ((Split-Path -Leaf $here) -in @('scripts', 'server')) {
            $bases = @((Split-Path -Parent $here), $workspace, $here)
        }
        foreach ($b in $bases) {
            if ($b) { $cands += (Join-Path $b 'config\project.json') }
        }
    }
    if ((Get-Location).Path) { $cands += (Join-Path (Get-Location).Path 'config\project.json') }
    return @($cands | Select-Object -Unique)
}

function Get-ProjectConfPath {
    $null = Get-ProjectConf
    return $script:ProjectConfSelectedPath
}

function ConvertTo-ProjectConfTable($Node) {
    # ConvertFrom-Json gives PSCustomObject; turn it into nested hashtables so
    # callers can write $c.daemon.pollSeconds and index with strings.
    if ($null -eq $Node) { return $null }
    if ($Node -is [System.Array]) { return , $Node }
    if ($Node -is [string] -or $Node -is [ValueType]) { return $Node }
    if ($Node -is [System.Collections.IDictionary]) {
        $o = @{}
        foreach ($k in @($Node.Keys)) { $o[[string]$k] = ConvertTo-ProjectConfTable $Node[$k] }
        return $o
    }
    if ($Node -is [psobject]) {
        $o = @{}
        foreach ($p in $Node.PSObject.Properties) { $o[$p.Name] = ConvertTo-ProjectConfTable $p.Value }
        return $o
    }
    return $Node
}

function Merge-ProjectConfTable($Base, $Over) {
    # Deep merge, one level of nesting is enough for this file.
    $out = @{}
    foreach ($k in @($Base.Keys)) {
        $v = $Base[$k]
        $out[$k] = if ($v -is [System.Collections.IDictionary]) { Merge-ProjectConfTable $v @{} } else { $v }
    }
    foreach ($k in @($Over.Keys)) {
        $ov = $Over[$k]
        if ($ov -is [System.Collections.IDictionary] -and $out[$k] -is [System.Collections.IDictionary]) {
            $out[$k] = Merge-ProjectConfTable $out[$k] $ov
        }
        else {
            $out[$k] = $ov
        }
    }
    return $out
}

function Get-ProjectConf {
    <#
        Returns the project config as a nested hashtable, with the built-in
        defaults filling any key the file does not set. Cached per runspace.
    #>
    param([switch]$Reload)
    if (-not $Reload -and $null -ne $script:ProjectConfCache) { return $script:ProjectConfCache }
    # A failed reload must revoke any old cached legacy authority.
    $script:ProjectConfCache = $null
    $fileData = @{}
    $script:ProjectConfSelectedPath = $null
    try {
        $explicit = if ($env:LANYA_PROJECT_CONFIG) { [IO.Path]::GetFullPath($env:LANYA_PROJECT_CONFIG) } else { $null }
        $candidates = Get-ProjectConfCandidates
    } catch { Throw-ProjectConfigError }
    foreach ($path in $candidates) {
        try {
            if (-not (Test-Path -LiteralPath $path -ErrorAction Stop)) {
                if ($explicit -and [IO.Path]::GetFullPath($path) -eq $explicit) { Throw-ProjectConfigError }
                continue
            }
            if (-not (Test-Path -LiteralPath $path -PathType Leaf -ErrorAction Stop)) { Throw-ProjectConfigError }
            # Get-Content can silently replace invalid UTF-8 bytes. Reject
            # encoding corruption rather than accepting a changed config.
            $raw = ([Text.UTF8Encoding]::new($false, $true)).GetString([IO.File]::ReadAllBytes($path)).TrimStart([char]0xFEFF)
            if (-not $raw -or -not $raw.TrimStart().StartsWith('{')) { Throw-ProjectConfigError }
            $obj = $raw | ConvertFrom-Json -ErrorAction Stop
            if ($null -eq $obj -or $obj.GetType().FullName -ne 'System.Management.Automation.PSCustomObject') { Throw-ProjectConfigError }
            $fileData = ConvertTo-ProjectConfTable $obj
            $script:ProjectConfSelectedPath = [IO.Path]::GetFullPath($path)
            break
        }
        catch { Throw-ProjectConfigError }
    }
    $script:ProjectConfCache = Merge-ProjectConfTable $script:ProjectConfDefaults $fileData
    return $script:ProjectConfCache
}

function Throw-ProjectConfigError {
    # No parser exception, private JSON excerpt or config pathname is exposed.
    $exception = [System.InvalidOperationException]::new('Project configuration invalid or unavailable; activity refused')
    throw [System.Management.Automation.ErrorRecord]::new(
        $exception, 'ConfigError', [System.Management.Automation.ErrorCategory]::InvalidData, $null)
}

function Get-ProjectConfValue {
    <#
        One value by dotted path, with a fallback:
            Get-ProjectConfValue 'daemon.pollSeconds' 60
    #>
    param(
        [string]$KeyPath,
        $Default = $null
    )
    $node = Get-ProjectConf
    foreach ($part in ($KeyPath -split '\.')) {
        if ($null -eq $node) { return $Default }
        if ($node -is [System.Collections.IDictionary] -and $node.Contains($part)) {
            $node = $node[$part]
        }
        else {
            return $Default
        }
    }
    if ($null -eq $node) { return $Default }
    return $node
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        $null = Get-ProjectConf
        Write-Output 'project_config_ok'
    } catch {
        Write-Error 'ConfigError: project configuration unavailable; activity refused' -ErrorAction Continue
        exit 1
    }
}
