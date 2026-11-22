# Builds dist\AddonManager.exe (one file, runs without Python installed).
# Steps: build venv (pinned PyInstaller) -> compile check + tests -> PyInstaller ->
# hidden self-test of the .exe against a fake WoW folder in a temp folder -> size + sha256.
# Never touches a real WoW folder or the real %APPDATA% settings.
#
#   powershell -ExecutionPolicy Bypass -File build.ps1            (full build)
#   powershell -ExecutionPolicy Bypass -File build.ps1 -TestOnly  (compile check + tests only)
#   ... build.ps1 -DistPath <folder> -WorkPath <folder>  (test build that leaves dist\ alone)
#   ... build.ps1 -ForceTests    (run the tests even if the same code already passed)
#   ... build.ps1 -Fingerprint   (only print the code fingerprint the stamp uses)
#
# Tests-passed stamp: when the tests pass, .venv-build\tests-passed.json records a fingerprint
# of the code tested (git tree ids of the inputs below, staged/unstaged/untracked included, via a
# temp index; plus the venv's Python and packages). A later run skips the tests only when the
# fingerprint is exactly the same and the stamp is under 24 h old. Any doubt: the tests run.
param(
    [switch]$TestOnly,
    [switch]$ForceTests,
    [switch]$Fingerprint,
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
# The app's own packages (requirements-app.txt, pinned with hashes); only installed when missing.
& $py -c "import cryptography, spake2, tkinterdnd2" 2>$null
if ($LASTEXITCODE) {
    Write-Host 'Installing the app packages (requirements-app.txt) ...'
    & $py -m pip install --disable-pip-version-check -q --only-binary=:all: --require-hashes -r requirements-app.txt
    if ($LASTEXITCODE) { throw 'Could not install the app packages.' }
}

# --- Tests-passed stamp (see the top). Every failure here means "run the tests". ---
# What the tests read, relative to the repo root. A test that reads another repo file must add it here.
$testInputs = @('AddonManager', 'addons.json')
$stampFile = Join-Path $here '.venv-build\tests-passed.json'
$stampMaxAge = [TimeSpan]::FromHours(24)

function Get-TestFingerprint([string]$tmpDir) {
    # Git tree ids of $testInputs as they are on disk (a temp index, never the real one) + the venv's
    # Python and packages. $null when anything is off.
    $ErrorActionPreference = 'Continue'   # git warnings on stderr must not throw (PowerShell 5.1)
    $oldIndex = $env:GIT_INDEX_FILE
    $tmpIndex = Join-Path $tmpDir ('fp-' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.index')
    try {
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) { return $null }
        $top = & git -C $here rev-parse --show-toplevel 2>$null
        if ($LASTEXITCODE -or -not $top) { return $null }
        $gitDir = & git -C $top rev-parse --absolute-git-dir 2>$null
        if ($LASTEXITCODE -or -not $gitDir) { return $null }
        $realIndex = Join-Path $gitDir 'index'
        if (Test-Path -LiteralPath $realIndex) { Copy-Item -LiteralPath $realIndex -Destination $tmpIndex -ErrorAction Stop }
        $env:GIT_INDEX_FILE = $tmpIndex
        & git -C $top add -A 2>$null | Out-Null
        if ($LASTEXITCODE) { return $null }
        $tree = & git -C $top write-tree 2>$null
        if ($LASTEXITCODE -or -not $tree) { return $null }
        $parts = @()
        foreach ($p in $testInputs) {
            $id = & git -C $top rev-parse "${tree}:$p" 2>$null
            if ($LASTEXITCODE -or -not $id) { return $null }
            $parts += "$p=$id"
        }
        $pyVer = & $py -VV 2>$null
        if ($LASTEXITCODE) { return $null }
        $freeze = & $py -m pip freeze --all --disable-pip-version-check 2>$null
        if ($LASTEXITCODE) { return $null }
        $bytes = [Text.Encoding]::UTF8.GetBytes((@($pyVer) + @($freeze) | Out-String))
        $sha = [Security.Cryptography.SHA256]::Create()
        $envHash = -join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') })
        $sha.Dispose()
        return (($parts -join ';') + ";env=$envHash")
    } catch {
        return $null
    } finally {
        if ($null -eq $oldIndex) { Remove-Item Env:\GIT_INDEX_FILE -ErrorAction SilentlyContinue }
        else { $env:GIT_INDEX_FILE = $oldIndex }
        Remove-Item -LiteralPath $tmpIndex, ($tmpIndex + '.lock') -Force -ErrorAction SilentlyContinue
    }
}

function Read-TestStamp {
    # The stamp as {fingerprint, passed_at, age}, or $null if missing, unreadable or too old.
    try {
        $s = Get-Content -LiteralPath $stampFile -Raw -ErrorAction Stop | ConvertFrom-Json
        if (-not $s.fingerprint -or -not $s.passed_utc_ticks) { return $null }
        $age = [DateTime]::UtcNow - [DateTime]::new([long]$s.passed_utc_ticks, [DateTimeKind]::Utc)
        if ($age -lt [TimeSpan]::Zero -or $age -ge $stampMaxAge) { return $null }
        return $s
    } catch { return $null }
}

function Write-TestStamp([string]$fp) {
    # $fp empty = "not passed" (written before a run, so a failed run never leaves an old pass behind).
    try {
        $obj = [ordered]@{ fingerprint = $fp; passed_at = (Get-Date).ToString('yyyy-MM-dd HH:mm');
                           passed_utc_ticks = [string][DateTime]::UtcNow.Ticks }
        $tmp = $stampFile + '.tmp'
        [IO.File]::WriteAllText($tmp, ($obj | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $tmp -Destination $stampFile -Force -ErrorAction Stop
        return $true
    } catch { Write-Host "Could not write the test stamp: $($_.Exception.Message)"; return $false }
}

$work = Join-Path $TempRoot ('AddonManager_build_' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $work | Out-Null
try {
    if ($Fingerprint) {
        $fp = Get-TestFingerprint $work
        if (-not $fp) { throw 'No fingerprint (git missing or failed).' }
        Write-Host $fp
        return
    }

    Write-Host 'Compile check ...'
    & $py -m compileall -q main.py am tests
    if ($LASTEXITCODE) { throw 'Compile check failed.' }

    $fp = Get-TestFingerprint $work
    $stamp = $null
    if ($fp -and -not $ForceTests) { $stamp = Read-TestStamp }
    if ($stamp -and ($stamp.fingerprint -ceq $fp)) {
        Write-Host "Tests skipped: same code passed at $($stamp.passed_at) (-ForceTests to run them)."
    } else {
        Write-Host 'Tests ...'
        if ($fp) { [void](Write-TestStamp '') }
        $env:ADDONMANAGER_TEST_TMP = Join-Path $work 'tests'
        & $py -m unittest discover -s tests -t .
        if ($LASTEXITCODE) { throw 'Tests failed.' }
        Remove-Item Env:\ADDONMANAGER_TEST_TMP
        if (-not $fp) {
            Write-Host 'No test stamp written (no fingerprint: git missing or failed).'
        } elseif ((Get-TestFingerprint $work) -cne $fp) {
            Write-Host 'Tests passed, but the code changed during the run: no stamp written.'
        } elseif (Write-TestStamp $fp) {
            Write-Host 'Test stamp written.'
        }
    }
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
