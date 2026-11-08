# Selected-backend recovery. No broad QQ termination or automatic UAC.
# The supervisor runs this hidden; healthy passes produce no output.
$ErrorActionPreference = 'Stop'
$helper = Join-Path $PSScriptRoot 'qq_backend_control.py'
if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) { throw 'QQ backend control is not installed' }
& python -B $helper --recover
exit $LASTEXITCODE
