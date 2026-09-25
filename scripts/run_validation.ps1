param(
    [Parameter(Mandatory = $true)]
    [string]$IarPath,

    [string]$LaunchJson = "",

    [switch]$SkipLive
)

# Unit tests, then the live tests against a managed backend started from
# $IarPath (the IAR installation, i.e. the directory with common\bin under it).

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath (Join-Path $IarPath "common\bin"))) {
    throw "No common\bin under it, so not an IAR installation: $IarPath"
}
if ($LaunchJson -and -not (Test-Path -LiteralPath $LaunchJson)) {
    throw "Launch JSON not found: $LaunchJson"
}

Write-Host "Running unit tests..." -ForegroundColor Cyan
pytest -q -m "not live"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

if ($SkipLive) {
    Write-Host "Skipping live tests (-SkipLive)." -ForegroundColor Yellow
    exit 0
}

$liveArgs = @("-q", "-m", "live", "--cspy-iar-path=$IarPath")
if ($LaunchJson) {
    $liveArgs += "--cspy-launch=$LaunchJson"
}

Write-Host "Running live tests..." -ForegroundColor Cyan
pytest @liveArgs
exit $LASTEXITCODE
