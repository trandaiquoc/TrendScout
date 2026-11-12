# Windows PowerShell 5.1 turns native stderr (unittest's normal output stream)
# into NativeCommandError records. Keep running long enough to check each native
# process exit code explicitly below.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Join-Path $root 'Backend'
$python = Join-Path $backend '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Backend\.venv is missing. Run .\setup.ps1 first.' }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw 'Node.js is required for JavaScript syntax checks.' }

Push-Location $backend
try {
    Write-Output '[1/8] Focused D6.1B Windows tests, including native ACL and locking checks'
    & $python -m unittest discover -s tests -p 'test_d6_*.py' -v 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Focused D6.1B tests failed.' }

    Write-Output '[2/8] Complete portable unittest regression'
    & $python -m unittest discover -s tests -v 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Complete unittest regression failed.' }

    Write-Output '[3/8] Golden tests with PYTHONHASHSEED=1'
    $env:PYTHONHASHSEED = '1'
    & $python -m unittest discover -s tests -p 'test_golden_contract.py' -v 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Golden tests failed with seed 1.' }

    Write-Output '[4/8] Golden tests with PYTHONHASHSEED=983'
    $env:PYTHONHASHSEED = '983'
    & $python -m unittest discover -s tests -p 'test_golden_contract.py' -v 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Golden tests failed with seed 983.' }
    Remove-Item Env:PYTHONHASHSEED -ErrorAction SilentlyContinue

    Write-Output '[5/8] Dependency and JavaScript syntax checks'
    & $python -m pip check 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'pip check failed.' }
    Get-ChildItem (Join-Path $root 'Frontend\public') -Recurse -File -Filter '*.js' | ForEach-Object {
        & node --check $_.FullName 2>&1
        if ($LASTEXITCODE -ne 0) { throw "JavaScript syntax check failed: $($_.FullName)" }
    }

    Write-Output '[6/8] Offline Chrome DOM, keyboard, and API/dashboard browser E2E'
    foreach ($script in @(
        'tests\browser_workspace_offline.py',
        'tests\browser_keyboard_offline.py',
        'tests\browser_pipeline_e2e_offline.py',
        'tests\browser_public_fetch_e2e_offline.py',
        'tests\browser_distributed_fetch_e2e_offline.py'
    )) {
        & $python $script 2>&1
        if ($LASTEXITCODE -ne 0) { throw "Browser test failed: $script" }
    }

    Write-Output '[7/8] Bounded synthetic storage benchmark'
    $env:PYTHONPATH = $backend
    & $python scripts\benchmark_storage_offline.py 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Storage benchmark failed.' }
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

    Write-Output '[8/8] Windows-specific checks are complete; Linux seccomp remains a separate gate.'
} finally {
    Remove-Item Env:PYTHONHASHSEED -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    Pop-Location
}
