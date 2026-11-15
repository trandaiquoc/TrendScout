param(
    [int]$Port = 8000,
    [switch]$Cloud,
    [switch]$GoogleLive
)

# Native Uvicorn output and Ctrl+C are passed through to the one foreground worker.
$ErrorActionPreference = 'Continue'
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Port must be between 1 and 65535.' }
if ($Cloud -and $GoogleLive) { throw 'Choose either -Cloud or -GoogleLive.' }

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Join-Path $root 'Backend'
$venvPython = Join-Path $backend '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & (Join-Path $root 'setup.ps1')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
        throw 'Windows setup failed; see the setup.ps1 error above.'
    }
}

$pythonCheck = & $venvPython -c 'import sys, fastapi, httpx, uvicorn; assert sys.version_info[:2] == (3, 12)'
if ($LASTEXITCODE -ne 0) {
    throw 'Backend/.venv is missing required runtime packages or is not Python 3.12. Run .\setup.ps1.'
}

Push-Location $backend
try {
    if ($GoogleLive) {
        Write-Output "Starting the separately approved Google LIVE profile at http://127.0.0.1:$Port/dashboard/"
        Write-Output 'Local only, one worker. Google providers require separate approval flags; startup does not run a discovery Fetch.'
        & $venvPython -m scripts.run_google_live_local --port $Port
    } elseif ($Cloud) {
        if (-not $env:GOOGLE_CLOUD_PROJECT) { throw 'Set GOOGLE_CLOUD_PROJECT before selecting -Cloud.' }
        if (-not $env:TREND_SCOUT_OPERATOR_TOKEN -or $env:TREND_SCOUT_OPERATOR_TOKEN.Length -lt 24) {
            throw 'Set TREND_SCOUT_OPERATOR_TOKEN (at least 24 characters) before selecting -Cloud.'
        }
        Write-Output "Starting the explicit cloud-backed runtime at http://127.0.0.1:$Port/"
        Write-Output 'No provider Fetch runs at startup. One worker; press Ctrl+C to stop.'
        & $venvPython -m uvicorn app.main:app --host 127.0.0.1 --port $Port --workers 1
    } else {
        Write-Output "Starting normal Wikimedia LIVE at http://127.0.0.1:$Port/dashboard/"
        Write-Output 'Local only, one worker, no automatic Fetch. Press Ctrl+C to stop.'
        & $venvPython -m scripts.run_wikimedia_live_local --port $Port
    }
    if ($LASTEXITCODE -ne 0) { throw "TrendScout runtime exited with code $LASTEXITCODE." }
} finally {
    Pop-Location
}
