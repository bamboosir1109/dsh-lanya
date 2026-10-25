# Install / update: lay the repo's classified folders out into the runtime layout.
#
# The repo is organised by role (scripts/, server/, panel/, ...), but the scripts
# assume they sit under <workdir>\napcat\ and derive the workdir from
# dirname(dirname(__file__)). So this flattens them into place.
#
#   .\install.ps1                    install one level above this repo
#   .\install.ps1 -Target "D:\work"  install somewhere else
#   .\install.ps1 -WhatIf            dry run
#   .\install.ps1 -Force             overwrite existing files
param(
    [string]$Target = "",
    [switch]$WhatIf,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$Repo = $PSScriptRoot

if (-not $Target) {
    $Target = Split-Path -Parent $Repo
}

$napcat = Join-Path $Target 'napcat'
$LanyaName = [string][char]0x5C9A + [char]0x5A05
$lanya = Join-Path $Target $LanyaName

Write-Host "repo   : $Repo"
Write-Host "target : $Target"
Write-Host ""

$map = @(
    @{ From = 'server'; To = $napcat },
    @{ From = 'scripts'; To = $napcat },
    @{ From = 'prompts'; To = $napcat },
    @{ From = 'panel'; To = $lanya }
)

$copied = 0
foreach ($m in $map) {
    $src = Join-Path $Repo $m.From
    if (-not (Test-Path $src)) { continue }
    $dst = $m.To
    if (-not $WhatIf -and -not (Test-Path $dst)) {
        New-Item -ItemType Directory -Path $dst -Force | Out-Null
    }
    foreach ($f in (Get-ChildItem $src -File)) {
        $to = Join-Path $dst $f.Name
        if ((Test-Path $to) -and -not $Force) {
            Write-Host "  skip   $($m.From)/$($f.Name)  (exists; use -Force to overwrite)"
            continue
        }
        if ($WhatIf) {
            Write-Host "  would  $($m.From)/$($f.Name) -> $to"
        } else {
            Copy-Item $f.FullName $to -Force
            Write-Host "  copy   $($m.From)/$($f.Name) -> $to"
        }
        $copied++
    }
}

$docs = Join-Path $Repo 'docs'
if (Test-Path $docs) {
    if (-not $WhatIf -and -not (Test-Path $lanya)) {
        New-Item -ItemType Directory -Path $lanya -Force | Out-Null
    }
    $cfg = Join-Path $lanya 'lanya-config.json'
    if (-not (Test-Path $cfg)) {
        $src = Join-Path $docs 'config.example.json'
        if (Test-Path $src) {
            if (-not $WhatIf) { Copy-Item $src $cfg }
            Write-Host "  copy   docs/config.example.json -> $cfg  (edit adminQQ)"
            $copied++
        }
    }
}

Write-Host ""
Write-Host "$copied file(s)."
Write-Host ""
Write-Host "Next:"
Write-Host "  1. edit $(Join-Path $lanya 'lanya-config.json') and set adminQQ"
Write-Host "  2. python `"$(Join-Path $napcat 'lanya-panel-server.py')`" --port 19388"
