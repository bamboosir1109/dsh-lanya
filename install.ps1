# Install code into <workdir>\napcat and <workdir>\<dataDir>\made.
# Existing local configuration, identity and persona are always protected.
# -Force updates code only; -WhatIf never creates files or directories.
# Keep this source ASCII-only for Windows PowerShell 5.1.
param(
    [string]$Target = '',
    [string]$DataDir = '',
    [switch]$WhatIf,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$Repo = $PSScriptRoot
if (-not $Target) { $Target = Split-Path -Parent $Repo }
$Target = [IO.Path]::GetFullPath($Target)
$napcat = Join-Path $Target 'napcat'
$genericName = ([string][char]0x5927 + [char]0x80A5 + [char]0x9C7C)
$personaSuffix = ([string][char]0x4EBA + [char]0x8BBE + [char]0x6B63 + [char]0x6587)

function Read-Project([string]$file) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { return $null }
    try {
        $doc = Get-Content -LiteralPath $file -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($doc -isnot [pscustomobject]) { throw 'expected a JSON object' }
        return $doc
    } catch {
        throw ('Invalid project configuration: ' + $file + '. Repair it before installing.')
    }
}

# Match runtime precedence. Never create another project.json over an existing
# installation's settings, including the older napcat\config location.
$localProject = $null
$projectTarget = Join-Path $Target 'config\project.json'
foreach ($candidate in @((Join-Path $napcat 'config\project.json'), $projectTarget)) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $localProject = Read-Project $candidate
        $projectTarget = $candidate
        break
    }
}
$repoProject = Read-Project (Join-Path $Repo 'config\project.json')
$configuredName = if ($localProject -and $localProject.dataDirName) {
    [string]$localProject.dataDirName
} elseif ($repoProject -and $repoProject.dataDirName) {
    [string]$repoProject.dataDirName
} else { $genericName }

if ($DataDir -and $localProject -and $DataDir -cne $configuredName) {
    throw 'DataDir differs from existing project.json. Update that local setting explicitly before installing.'
}
if (-not $DataDir) { $DataDir = $configuredName }

# A data directory is a single child name, never a path outside Target.
if ([string]::IsNullOrWhiteSpace($DataDir) -or $DataDir -in '.', '..', 'napcat', 'config' -or
    [IO.Path]::IsPathRooted($DataDir) -or $DataDir.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0 -or
    $DataDir.EndsWith('.') -or $DataDir.EndsWith(' ') -or
    $DataDir -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') {
    throw 'DataDir must be a single folder name under Target, distinct from napcat and config.'
}
$data = Join-Path $Target $DataDir

# Existing historical persona/identity filenames must not be shadowed by new
# templates. Only a fresh installation gets napcat\persona.md.
$personaTarget = Join-Path $napcat 'persona.md'
foreach ($candidate in @($personaTarget,
    (Join-Path $data ($DataDir + '-' + $personaSuffix + '.md')),
    (Join-Path $napcat ($DataDir + '-' + $personaSuffix + '.md')))) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $personaTarget = $candidate; break }
}

$map = @()
foreach ($file in (Get-ChildItem -LiteralPath (Join-Path $Repo 'server') -File -Filter '*.py')) {
    $name = if ($file.Name -eq 'panel-server.py') { 'lanya-panel-server.py' } else { $file.Name }
    $map += @{ From = $file.FullName; To = (Join-Path $napcat $name); Protected = $false }
}
foreach ($folder in 'scripts', 'prompts') {
    foreach ($file in (Get-ChildItem -LiteralPath (Join-Path $Repo $folder) -File)) {
        if ($file.Extension -notin '.ps1', '.py', '.cmd', '.txt') { continue }
        $map += @{ From = $file.FullName; To = (Join-Path $napcat $file.Name); Protected = $false }
    }
}
foreach ($file in (Get-ChildItem -LiteralPath (Join-Path $Repo 'panel') -File)) {
    if ($file.Extension -notin '.html', '.css', '.js') { continue }
    $name = if ($file.Name -eq 'panel.html') { 'lanya-panel.html' } else { $file.Name }
    $map += @{ From = $file.FullName; To = (Join-Path $data ('made\' + $name)); Protected = $false }
}
# Ship a real offline browser game, separate from historical solver scripts.
# The fixed source list cannot walk browser profiles or overwrite other works.
foreach ($name in 'index.html', 'game.js', 'app.js') {
    $map += @{ From = (Join-Path $Repo ('games\2048\' + $name)); To = (Join-Path $data ('made\2048\' + $name)); Protected = $false }
}
$map += @(
    @{ From = (Join-Path $Repo 'config\project.json'); To = $projectTarget; Protected = $true; Project = $true },
    @{ From = (Join-Path $Repo 'docs\persona.example.md'); To = $personaTarget; Protected = $true },
    @{ From = (Join-Path $Repo 'docs\config.example.json'); To = (Join-Path $data 'lanya-config.json'); Protected = $true; Legacy = (Join-Path $data 'dafeiyu-config.json') },
    @{ From = (Join-Path $Repo 'docs\tunables.example.json'); To = (Join-Path $data 'lanya-tunables.json'); Protected = $true; Legacy = (Join-Path $data 'dafeiyu-tunables.json') },
    @{ From = (Join-Path $Repo 'docs\activities.example.json'); To = (Join-Path $data 'activities.json'); Protected = $true }
)

Write-Host ('repo   : ' + $Repo)
Write-Host ('target : ' + $Target)
Write-Host ('data   : ' + $data)
$copied = 0
foreach ($entry in $map) {
    $exists = Test-Path -LiteralPath $entry.To
    $legacyExists = $entry.Legacy -and (Test-Path -LiteralPath $entry.Legacy)
    if ($entry.Protected -and ($exists -or $legacyExists)) {
        Write-Host ('  protect ' + $entry.To)
        continue
    }
    if ($exists -and -not $Force) {
        Write-Host ('  skip    ' + $entry.To + ' (use -Force to update code)')
        continue
    }
    if ($WhatIf) {
        Write-Host ('  would   ' + $entry.From + ' -> ' + $entry.To)
        continue
    }
    $parent = Split-Path -Parent $entry.To
    if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    if ($entry.Project) {
        $initialProject = Read-Project $entry.From
        $initialProject | Add-Member -NotePropertyName dataDirName -NotePropertyValue $DataDir -Force
        $json = $initialProject | ConvertTo-Json -Depth 20
        [IO.File]::WriteAllText($entry.To, $json, (New-Object Text.UTF8Encoding($false)))
    } else {
        Copy-Item -LiteralPath $entry.From -Destination $entry.To -Force
    }
    Write-Host ('  copy    ' + $entry.To)
    $copied++
}
if (-not $WhatIf) {
    foreach ($sub in 'made', 'notes', 'diary', 'people', 'memes', 'chats') {
        $folder = Join-Path $data $sub
        if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
    }
}
Write-Host ($copied.ToString() + ' file(s) copied. Existing local settings and persona are protected.')
Write-Host 'Next: read docs/INSTALL.md; configure identity and external dependencies before starting.'
Write-Host 'Code copying does not restart running processes or update an installed DSH plugin.'
