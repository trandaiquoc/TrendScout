# Windows PowerShell 5.1 reports native stderr (including pip progress) as
# NativeCommandError records. Native processes are checked by exit code below.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Join-Path $root 'Backend'
$venvPython = Join-Path $backend '.venv\Scripts\python.exe'

try {
    $launcher = Get-Command py -ErrorAction Stop
    & $launcher.Source -3.12 -c 'import sys; assert sys.version_info[:2] == (3, 12)'
    if ($LASTEXITCODE -ne 0) { throw 'Native Python 3.12 is required. Install it with: winget install --id Python.Python.3.12 --exact' }
} catch {
    throw 'Native Python 3.12 is required. Install it with: winget install --id Python.Python.3.12 --exact'
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    & $launcher.Source -3.12 -m venv (Join-Path $backend '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create Backend\.venv with Python 3.12.' }
}

Push-Location $backend
try {
    & $venvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Pinned project dependency installation failed.' }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'pip check found an inconsistent environment.' }
    & $venvPython -c 'import sys, sqlite3, httpx, fastapi; assert sys.version_info[:2] == (3, 12); sqlite3.connect('':memory:'').execute(''select 1'').fetchone(); print(''Python 3.12, httpx, FastAPI, SQLite: PASS'')'
    if ($LASTEXITCODE -ne 0) { throw 'Native runtime import verification failed.' }

    if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is unavailable; cannot establish the Windows private data directory.' }
    $localRoot = Join-Path $env:LOCALAPPDATA 'TrendScout'
    & $venvPython -c 'import sys; from app.storage.local_file_security import secure_private_directory; secure_private_directory(sys.argv[1])' $localRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not establish the private per-user TrendScout data/config directory.' }
} finally {
    Pop-Location
}

Write-Output 'TrendScout Windows environment is ready. No credentials or .env file were created.'
