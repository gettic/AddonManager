# Builds dist\AddonManager.exe (one file, runs without Python installed).
# Steps: build venv (pinned PyInstaller) -> compile check + tests -> PyInstaller ->
# hidden self-test of the .exe against a fake WoW folder in a temp folder -> size + sha256.
# Never touches a real WoW folder or the real %APPDATA% settings.
#
#   powershell -ExecutionPolicy Bypass -File build.ps1            (full build)
#   powershell -ExecutionPolicy Bypass -File build.ps1 -TestOnly  (compile check + tests only)
param(
    [switch]$TestOnly,
    [string]$TempRoot = $env:TEMP
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
        --distpath dist --workpath build AddonManager.spec
    if ($LASTEXITCODE) { throw 'PyInstaller failed.' }
    $exe = Join-Path $here 'dist\AddonManager.exe'

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

    $item = Get-Item -LiteralPath $exe
    $hash = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLower()
    Write-Host ('BUILD OK: {0}  {1:N1} MB  sha256 {2}' -f $item.FullName, ($item.Length / 1MB), $hash)
}
finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
