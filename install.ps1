# Install / update: lay the repo's classified folders out into the runtime layout.
#
# The repo is organised by role (scripts/, server/, panel/, ...), but every
# script assumes it sits under <workdir>\napcat\ and derives the workdir from
# its own location, while her own files live in <workdir>\<dataDir>\.
# So this flattens them into place, under the names the code actually reads.
#
#   .\install.ps1                    install one level above this repo
#   .\install.ps1 -Target "D:\work"  install somewhere else
#   .\install.ps1 -DataDir "..."     her folder name (default: config/project.json)
#   .\install.ps1 -WhatIf            dry run
#   .\install.ps1 -Force             overwrite existing files
#
# This file is deliberately ASCII-only: Windows PowerShell 5.1 reads a BOM-less
# .ps1 in the ANSI codepage, which turns any Chinese literal into mojibake.
param(
    [string]$Target = "",
    [string]$DataDir = "",
    [switch]$WhatIf,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$Repo = $PSScriptRoot

if (-not $Target) {
    $Target = Split-Path -Parent $Repo
}

# Repo-level default name, kept as char codes so this file stays ASCII.
$GenericName = ([string][char]0x5927 + [char]0x80A5 + [char]0x9C7C)     # generic default
$PersonaSuffix = ([char]0x4EBA + [char]0x8BBE + [char]0x6B63 + [char]0x6587)  # "persona text"

# Her folder name comes from config/project.json so the repo can stay generic
# and a personalised install can rename it without touching code. An existing
# folder that already holds her files wins over the configured default: that is
# how a renamed install (大肥鱼 here) keeps being updated instead of getting a
# second, empty folder next to it.
if (-not $DataDir) {
    $confPath = Join-Path $Repo 'config\project.json'
    if (Test-Path $confPath) {
        try {
            $DataDir = [string](Get-Content $confPath -Raw -Encoding UTF8 | ConvertFrom-Json).dataDirName
        } catch { }
    }
    if (-not $DataDir) { $DataDir = $GenericName }

    $markers = 'lanya-config.json', 'budget.json', 'lanya-tunables.json', 'activities.json'
    $best = $DataDir
    $bestScore = -1
    $cands = @($DataDir, $GenericName)
    # Any renamed folder in the workdir counts too, so a personalised install is
    # recognised without having to name it here.
    if (Test-Path $Target) {
        foreach ($d in (Get-ChildItem $Target -Directory)) { $cands += $d.Name }
    }
    foreach ($cand in ($cands | Select-Object -Unique)) {
        if (-not $cand) { continue }
        $base = Join-Path $Target $cand
        if (-not (Test-Path $base)) { continue }
        $score = 0
        foreach ($mk in $markers) { if (Test-Path (Join-Path $base $mk)) { $score++ } }
        if ($score -gt $bestScore) { $best = $cand; $bestScore = $score }
    }
    if ($bestScore -gt 0) { $DataDir = $best }
}

$napcat = Join-Path $Target 'napcat'
$data   = Join-Path $Target $DataDir

Write-Host "repo   : $Repo"
Write-Host "target : $Target"
Write-Host "data   : $data"
Write-Host ""

# from (in repo) -> to (runtime), one row per file so the renames stay explicit.
$map = @(
    # The server is named after the panel it serves; the code refers to lanya-*.
    @{ From = 'server\panel-server.py';          To = (Join-Path $napcat 'lanya-panel-server.py') },
    # Everything else in scripts/ lands flat in napcat\ under its own name.
    @{ From = 'scripts';                         To = $napcat },
    @{ From = 'prompts';                         To = $napcat },
    @{ From = 'config\project.json';             To = (Join-Path $Target 'config\project.json') },
    # The panel is served as a static file out of her made\ folder.
    @{ From = 'panel\panel.html';                To = (Join-Path $data 'made\lanya-panel.html') },
    # Templates: the persona, and the identity config that never goes into git.
    @{ From = 'docs\persona.example.md';         To = (Join-Path $napcat ($DataDir + '-' + $PersonaSuffix + '.md')) },
    @{ From = 'docs\config.example.json';        To = (Join-Path $data 'lanya-config.json') },
    @{ From = 'docs\tunables.example.json';      To = (Join-Path $data 'lanya-tunables.json') },
    @{ From = 'docs\activities.example.json';    To = (Join-Path $data 'activities.json') }
)

$copied = 0
foreach ($m in $map) {
    $src = Join-Path $Repo $m.From
    if (-not (Test-Path $src)) { continue }
    $isContainer = (Get-Item $src).PSIsContainer
    $dstRoot = Split-Path -Parent $m.To
    if (-not $WhatIf -and -not (Test-Path $dstRoot)) {
        New-Item -ItemType Directory -Path $dstRoot -Force | Out-Null
    }
    # A directory source means "flatten every file in it", minus the local
    # droppings that accumulate in scripts/ while the thing is running.
    $files = if ($isContainer) {
        Get-ChildItem $src -File | Where-Object { $_.Extension -notin '.log', '.bak', '.tmp' -and $_.Name -notlike '*.bak-*' }
    } else { @(Get-Item $src) }
    foreach ($f in $files) {
        $to = if ($isContainer) { Join-Path $m.To $f.Name } else { $m.To }
        $label = if ($isContainer) { $m.From + '\' + $f.Name } else { $m.From }
        if ((Test-Path $to) -and -not $Force) {
            Write-Host "  skip   $label  (exists; use -Force to overwrite)"
            continue
        }
        if ($WhatIf) {
            Write-Host "  would  $label -> $to"
        } else {
            Copy-Item $f.FullName $to -Force
            Write-Host "  copy   $label -> $to"
        }
        $copied++
    }
}

# The folders her tools write into. A missing one makes the panel look empty,
# or makes a tool throw on its first write, so create them up front.
if (-not $WhatIf) {
    foreach ($sub in 'made', 'notes', 'diary', 'people', 'memes', 'chats') {
        $p = Join-Path $data $sub
        if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p -Force | Out-Null }
    }
}

$panelCmd = 'python "' + (Join-Path $napcat 'lanya-panel-server.py') + '" --port 19388'

Write-Host ""
Write-Host "$copied file(s)."
Write-Host ""
Write-Host "Next:"
Write-Host ('  1. edit ' + (Join-Path $data 'lanya-config.json') + ' and set adminQQ + fanGroup')
Write-Host ('  2. ' + $panelCmd)
Write-Host '  3. dsh plugin --profile desktop add https://github.com/bamboosir1109/dsh-lanya'
