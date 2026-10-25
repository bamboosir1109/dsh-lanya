# Read the shared project config.
#
#   . "$PSScriptRoot\project_conf.ps1"
#   $c = Get-ProjectConf                       # nested hashtable, defaults filled in
#   $p = Get-ProjectConfValue 'panelStaticPort' 19388
#   $s = Get-ProjectConfValue 'daemon.pollSeconds' 60
#
# If config/project.json is missing or unreadable the built-in defaults below are
# used, so a missing file never changes behaviour -- only an edited one does.
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
}

$script:ProjectConfCache = $null

function Get-ProjectConfPath {
    # Where project.json may live. Repo layout has scripts/ and server/ one level
    # below the root; the installed flat layout has the scripts in napcat/.
    # Try both, never hardcode a drive.
    $here = $PSScriptRoot
    if (-not $here) {
        try { $here = Split-Path -Parent $MyInvocation.MyCommand.Path } catch { $here = '' }
    }
    $cands = @()
    if ($env:LANYA_PROJECT_CONFIG) { $cands += $env:LANYA_PROJECT_CONFIG }
    if ($here) {
        $up1 = Split-Path -Parent $here
        $up2 = if ($up1) { Split-Path -Parent $up1 } else { '' }
        foreach ($b in @($here, $up1, $up2)) {
            if ($b) { $cands += (Join-Path $b 'config\project.json') }
        }
    }
    if ((Get-Location).Path) { $cands += (Join-Path (Get-Location).Path 'config\project.json') }
    foreach ($p in $cands) {
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return $null
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
    if ($null -ne $script:ProjectConfCache) { return $script:ProjectConfCache }
    $fileData = @{}
    $path = Get-ProjectConfPath
    if ($path) {
        try {
            $raw = Get-Content -LiteralPath $path -Raw -Encoding UTF8
            if ($raw -and $raw.Trim() -ne '') {
                $obj = $raw | ConvertFrom-Json
                $conv = ConvertTo-ProjectConfTable $obj
                if ($conv -is [System.Collections.IDictionary]) { $fileData = $conv }
            }
        }
        catch {
            # A broken file must not take the machine down - fall back to defaults.
            $fileData = @{}
        }
    }
    $script:ProjectConfCache = Merge-ProjectConfTable $script:ProjectConfDefaults $fileData
    return $script:ProjectConfCache
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
    # Run directly: print what is in effect, handy when checking a machine.
    $p = Get-ProjectConfPath
    Write-Output ('config: ' + $(if ($p) { $p } else { '(none - using defaults)' }))
    Get-ProjectConf | ConvertTo-Json -Depth 6
}
