param(
    [int]$Port = 0
)

# Native Uvicorn output and Ctrl+C are passed through to the one foreground worker.
$ErrorActionPreference = 'Continue'
if (-not $PSBoundParameters.ContainsKey('Port')) {
    if ($env:TREND_SCOUT_DEMO_PORT) {
        $environmentPort = 0
        if (-not [int]::TryParse($env:TREND_SCOUT_DEMO_PORT, [ref]$environmentPort)) {
            throw 'TREND_SCOUT_DEMO_PORT must be an integer from 1 to 65535.'
        }
        $Port = $environmentPort
    } else {
        $Port = 8000
    }
}
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Port must be between 1 and 65535.' }
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
    Write-Output "Starting the local Wikimedia LIVE application at http://127.0.0.1:$Port/dashboard/"
    Write-Output 'Loopback only, one worker. Provider requests start only when Fetch is selected.'
    & $venvPython -m scripts.run_wikimedia_live_local --port $Port
    if ($LASTEXITCODE -ne 0) { throw "TrendScout runtime exited with code $LASTEXITCODE." }
} finally {
    Pop-Location
}
