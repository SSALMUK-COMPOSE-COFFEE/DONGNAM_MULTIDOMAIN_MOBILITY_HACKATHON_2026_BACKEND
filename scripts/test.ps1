$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$originalPath = $env:PATH
$originalOfflineMode = $env:OFFLINE_MODE
$originalPythonUtf8 = $env:PYTHONUTF8

Push-Location $projectRoot
try {
    # Tests exercise mocked live APIs even when local development uses snapshots.
    $env:OFFLINE_MODE = 'false'
    $env:PYTHONUTF8 = '1'
    $gitBashDirectory = Join-Path $env:ProgramFiles 'Git\bin'
    if (Test-Path -LiteralPath (Join-Path $gitBashDirectory 'bash.exe')) {
        $env:PATH = $gitBashDirectory + ';' + $env:PATH
    }
    & py -3.12 -m uv run --frozen pytest @args
    $testExitCode = $LASTEXITCODE
}
finally {
    $env:PATH = $originalPath
    $env:OFFLINE_MODE = $originalOfflineMode
    $env:PYTHONUTF8 = $originalPythonUtf8
    Pop-Location
}
exit $testExitCode
