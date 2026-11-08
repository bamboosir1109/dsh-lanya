# A private pipe to the shared resolver. No token in command arguments or logs.
# Start once per worker/configuration load, not once per message or send slice.
function Invoke-QqBackendResolver {
    param([string]$Python = 'python', [switch]$Status)
    $helper = Join-Path $PSScriptRoot 'qq_backend.py'
    if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) {
        throw 'QQ backend resolver is not installed'
    }
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $Python
    $mode = if ($Status) { '--status' } else { '--connection-private' }
    $start.Arguments = '-B "' + $helper + '" ' + $mode
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw 'QQ backend resolver failed to start' }
        $output = $process.StandardOutput.ReadToEndAsync()
        $errorRead = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(10000)) {
            $process.Kill()
            throw 'QQ backend resolver timed out'
        }
        $raw = $output.GetAwaiter().GetResult()
        [void]($errorRead.GetAwaiter().GetResult())
        if ($process.ExitCode -ne 0 -or $raw.Length -gt 16384) {
            throw 'Selected QQ backend configuration is unavailable'
        }
        $value = $raw | ConvertFrom-Json -ErrorAction Stop
        if ($value.provider -isnot [string]) { throw 'Invalid QQ backend connection' }
        if (-not $Status -and ($value.napcatHttp -isnot [string] -or $value.napcatToken -isnot [string])) {
            throw 'Invalid QQ backend connection'
        }
        if ($Status -and $value.verified -isnot [bool]) { throw 'Invalid QQ backend status' }
        return $value
    } catch {
        # Never echo stdout, stderr, tokens, JSON decoder text or private paths.
        throw 'Selected QQ backend configuration is unavailable'
    } finally {
        $process.Dispose()
    }
}

function Get-QqBackendConnection {
    param([string]$Python = 'python')
    return Invoke-QqBackendResolver -Python $Python
}

function Get-QqBackendStatus {
    param([string]$Python = 'python')
    return Invoke-QqBackendResolver -Python $Python -Status
}
