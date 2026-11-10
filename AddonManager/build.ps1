# Builds dist\AddonManager.exe (one file, runs without Python installed).
# Steps: build venv (pinned PyInstaller) -> compile check + tests -> PyInstaller ->
# hidden self-test of the .exe against a fake WoW folder in a temp folder -> size + sha256.
# Never touches a real WoW folder or the real %APPDATA% settings.
#
#   powershell -ExecutionPolicy Bypass -File build.ps1            (full build)
#   powershell -ExecutionPolicy Bypass -File build.ps1 -TestOnly  (compile check + tests only)
#   ... build.ps1 -DistPath <folder> -WorkPath <folder>  (test build that leaves dist\ alone)
param(
    [switch]$TestOnly,
    [string]$TempRoot = $env:TEMP,
    [string]$DistPath = 'dist',     # where the .exe goes (e.g. a plan folder for a test build)
    [string]$WorkPath = 'build'
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
Set-Location -LiteralPath $here

$py = Join-Path $here '.venv-build\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) {
    Write-Host 'Creating build venv (.venv-build) ...'
    & python -m venv .venv-build
    if ($LASTEXITCODE) { throw 'Could not create the build venv.' }
    & $py -m pip install --disable-pip-version-check -q -r requirements-build.txt
    if ($LASTEXITCODE) { throw 'Could not install the build requirements.' }
}

$work = Join-Path $TempRoot ('AddonManager_build_' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $work | Out-Null
try {
    Write-Host 'Compile check ...'
    & $py -m compileall -q main.py am tests
    if ($LASTEXITCODE) { throw 'Compile check failed.' }

    Write-Host 'Tests ...'
    $env:ADDONMANAGER_TEST_TMP = Join-Path $work 'tests'
    & $py -m unittest discover -s tests -t .
    if ($LASTEXITCODE) { throw 'Tests failed.' }
    Remove-Item Env:\ADDONMANAGER_TEST_TMP
    if ($TestOnly) { Write-Host 'TESTS OK'; return }

    Write-Host 'PyInstaller ...'
    & (Join-Path $here '.venv-build\Scripts\pyinstaller.exe') --noconfirm --clean --log-level WARN `
        --distpath $DistPath --workpath $WorkPath AddonManager.spec
    if ($LASTEXITCODE) { throw 'PyInstaller failed.' }
    $exe = Join-Path (Resolve-Path -LiteralPath $DistPath) 'AddonManager.exe'

    Write-Host 'Self-test of the .exe (hidden, fake WoW folder) ...'
    $data = & $py -m tests.make_selftest_data (Join-Path $work 'selftest')
    if ($LASTEXITCODE -or $data.Count -lt 3) { throw 'Could not make self-test data.' }
    $report = Join-Path $work 'selftest-report.json'
    $argList = @('--selftest', '--manifest', ('"' + $data[0] + '"'), '--wow', ('"' + $data[1] + '"'),
                 '--github-api', ('"' + $data[2] + '"'),
                 '--home', ('"' + (Join-Path $work 'home') + '"'), '--selftest-out', ('"' + $report + '"'))
    $proc = Start-Process -FilePath $exe -ArgumentList $argList -Wait -PassThru -WindowStyle Hidden
    if (Test-Path -LiteralPath $report) { Get-Content -LiteralPath $report | Write-Host }
    if ($proc.ExitCode -ne 0) { throw "Self-test failed (exit code $($proc.ExitCode))." }

    # Self-update on a COPY of the .exe in the temp folder (dist\AddonManager.exe is never swapped).
    Write-Host 'Self-update test (hidden, copy of the .exe, fake release) ...'
    $upd = & $py -m tests.make_selftest_data (Join-Path $work 'selfupdate') --app-update $exe
    if ($LASTEXITCODE -or $upd.Count -lt 2) { throw 'Could not make self-update test data.' }
    $updReport = Join-Path $work 'selfupdate-report.json'
    $updArgs = @('--selftest-update', '--github-api', ('"' + $upd[1] + '"'),
                 '--home', ('"' + (Join-Path $work 'home-update') + '"'), '--selftest-out', ('"' + $updReport + '"'))
    $proc = Start-Process -FilePath $upd[0] -ArgumentList $updArgs -Wait -PassThru -WindowStyle Hidden
    if (Test-Path -LiteralPath $updReport) { Get-Content -LiteralPath $updReport | Write-Host }
    if ($proc.ExitCode -ne 0) { throw "Self-update test failed (exit code $($proc.ExitCode))." }

    $item = Get-Item -LiteralPath $exe
    $hash = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLower()
    Write-Host ('BUILD OK: {0}  {1:N1} MB  sha256 {2}' -f $item.FullName, ($item.Length / 1MB), $hash)
}
finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
