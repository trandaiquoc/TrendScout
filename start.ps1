param(
    [int]$Port = 8000,
    [switch]$Synthetic
)

# Allow Uvicorn's native stderr stream through on Windows PowerShell 5.1; its
# exit code is checked after the server stops.
$ErrorActionPreference = 'Continue'
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Port must be between 1 and 65535.' }
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Join-Path $root 'Backend'
$venvPython = Join-Path $backend '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & (Join-Path $root 'setup.ps1')
    if (-not (Test-Path -LiteralPath $venvPython)) { throw 'Windows setup failed.' }
}

Push-Location $backend
try {
    if ($Synthetic) {
        Write-Output "Starting the local SYNTHETIC demo at http://127.0.0.1:$Port/"
        Write-Output 'Fixture data is temporary. Press Ctrl+C to stop Uvicorn.'
        & $venvPython -m uvicorn app.demo_local:create_demo_app --factory --host 127.0.0.1 --port $Port --workers 1
    } else {
        if (-not $env:GOOGLE_CLOUD_PROJECT) { throw 'Set GOOGLE_CLOUD_PROJECT for the standard cloud-backed runtime, or use .\start.ps1 -Synthetic for offline development.' }
        if (-not $env:TREND_SCOUT_OPERATOR_TOKEN -or $env:TREND_SCOUT_OPERATOR_TOKEN.Length -lt 24) {
            throw 'Set TREND_SCOUT_OPERATOR_TOKEN (at least 24 characters) for the standard runtime, or use .\start.ps1 -Synthetic.'
        }
        Write-Output "Starting the standard TrendScout runtime at http://127.0.0.1:$Port/"
        Write-Output 'No provider fetch runs at startup. Press Ctrl+C to stop Uvicorn.'
        & $venvPython -m uvicorn app.main:app --host 127.0.0.1 --port $Port --workers 1
    }
    if ($LASTEXITCODE -ne 0) { throw 'TrendScout exited with an error.' }
} finally {
    Pop-Location
}
