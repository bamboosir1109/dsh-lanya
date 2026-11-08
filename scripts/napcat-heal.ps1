# Compatibility entry for existing scheduled tasks; use the selected provider.
& (Join-Path $PSScriptRoot 'qq-backend-heal.ps1')
exit $LASTEXITCODE
