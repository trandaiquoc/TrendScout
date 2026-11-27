param(
    [int]$Port = 0,
    [switch]$YoutubeOwnerManualLive,
    [switch]$FirestoreSharedLive
)

# Native Uvicorn output and Ctrl+C are passed through to the one foreground worker.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Join-Path $root 'Backend'
$venvPython = Join-Path $backend '.venv\Scripts\python.exe'
$explicitPort = $PSBoundParameters.ContainsKey('Port')
if ($explicitPort -and ($Port -lt 1 -or $Port -gt 65535)) {
    throw 'Port must be between 1 and 65535.'
}
if (-not $explicitPort -and $env:TREND_SCOUT_DEMO_PORT) {
    $environmentPort = 0
    if (-not [int]::TryParse($env:TREND_SCOUT_DEMO_PORT, [ref]$environmentPort) -or
        $environmentPort -lt 1 -or $environmentPort -gt 65535) {
        throw 'TREND_SCOUT_DEMO_PORT must be an integer from 1 to 65535.'
    }
    $Port = $environmentPort
    $explicitPort = $true
}
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

$youtubeHost = (& $venvPython -c 'import socket; print(socket.gethostname())').Trim()
if ($LASTEXITCODE -ne 0 -or -not $youtubeHost) {
    throw 'Could not identify this Windows machine for the local YouTube LIVE runtime.'
}
$launcherArguments = @('--root-env-file', (Join-Path $root '.env'))
if ($explicitPort) { $launcherArguments += @('--port', [string]$Port) }
if ($YoutubeOwnerManualLive) {
    if ($env:VERCEL) {
        throw 'Owner manual YouTube LIVE is restricted to local Windows 2 development.'
    }
    if ($youtubeHost -ine 'DESKTOP-ICECOET') {
        throw 'Owner manual YouTube LIVE is authorized only on DESKTOP-ICECOET.'
    }
    $launcherArguments += '--youtube-owner-manual-live'
}
if ($FirestoreSharedLive) { $launcherArguments += '--firestore-shared-live' }
$previousYoutubeApproval = $env:TREND_SCOUT_YOUTUBE_LIVE_APPROVED
$previousYoutubeHost = $env:TREND_SCOUT_YOUTUBE_LIVE_HOST
$env:TREND_SCOUT_YOUTUBE_LIVE_APPROVED = 'true'
$env:TREND_SCOUT_YOUTUBE_LIVE_HOST = $youtubeHost

try {
    Push-Location $backend
    try {
        Write-Output "Starting the local multi-source LIVE application at http://127.0.0.1:$Port/dashboard/"
        Write-Output 'Loopback only, one worker. Provider requests start only when Fetch is selected.'
        & $venvPython -m scripts.run_google_live_local @launcherArguments
        if ($LASTEXITCODE -ne 0) { throw "TrendScout runtime exited with code $LASTEXITCODE." }
    } finally {
        Pop-Location
    }
} finally {
    if ($null -eq $previousYoutubeApproval) {
        Remove-Item Env:TREND_SCOUT_YOUTUBE_LIVE_APPROVED -ErrorAction SilentlyContinue
    } else {
        $env:TREND_SCOUT_YOUTUBE_LIVE_APPROVED = $previousYoutubeApproval
    }
    if ($null -eq $previousYoutubeHost) {
        Remove-Item Env:TREND_SCOUT_YOUTUBE_LIVE_HOST -ErrorAction SilentlyContinue
    } else {
        $env:TREND_SCOUT_YOUTUBE_LIVE_HOST = $previousYoutubeHost
    }
}
