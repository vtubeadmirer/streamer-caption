param(
    [string]$InstallRoot = "",
    [switch]$NoUpdate
)

$ErrorActionPreference = "Stop"
if (-not $InstallRoot) {
    $InstallRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
}
$StatePath = Join-Path $InstallRoot "state.json"
$RuntimePython = Join-Path $InstallRoot "runtime\.venv\Scripts\python.exe"
$Updater = Join-Path $PSScriptRoot "updater.py"
if (-not (Test-Path -LiteralPath $StatePath)) { throw "Installation state is missing: $StatePath" }
if (-not (Test-Path -LiteralPath $RuntimePython)) { throw "Python runtime is missing: $RuntimePython" }

function Read-State {
    return Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
}

function Ensure-RuntimeLink([string]$VersionRoot) {
    $junction = Join-Path $VersionRoot ".venv"
    if (-not (Test-Path -LiteralPath $junction)) {
        & cmd.exe /d /c "mklink /J `"$junction`" `"$(Join-Path $InstallRoot 'runtime\.venv')`"" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not connect the shared runtime." }
    }
}

function Ensure-PersistentLinks([string]$VersionRoot) {
    $UserData = Join-Path $InstallRoot "user-data"
    $Links = @{
        "logs" = (Join-Path $UserData "logs")
        "reports" = (Join-Path $UserData "reports")
        "samples" = (Join-Path $UserData "samples")
    }
    foreach ($Name in $Links.Keys) {
        $Link = Join-Path $VersionRoot $Name
        if (-not (Test-Path -LiteralPath $Link)) {
            & cmd.exe /d /c "mklink /J `"$Link`" `"$($Links[$Name])`"" | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Could not connect persistent data: $Name" }
        }
    }
}

if (-not $NoUpdate -and (Test-Path -LiteralPath (Join-Path $InstallRoot "updater.json"))) {
    try {
        $updateConfig = Get-Content -LiteralPath (Join-Path $InstallRoot "updater.json") -Raw | ConvertFrom-Json
        if ($updateConfig.check_on_start) {
            $raw = & $RuntimePython $Updater check --install-root $InstallRoot
            $status = ($raw -join "`n") | ConvertFrom-Json
            if ($status.ok -and $status.available) {
                Write-Host ""
                Write-Host "A new Streamer Caption version is available: $($status.latest_version)" -ForegroundColor Cyan
                if ($status.release_notes) { Write-Host $status.release_notes }
                $answer = Read-Host "Update now? (Y/N)"
                if ($answer -match '^[Yy]') {
                    & $RuntimePython $Updater update --install-root $InstallRoot
                    if ($LASTEXITCODE -ne 0) { Write-Warning "Update failed. The current version was not changed." }
                }
            }
        }
    } catch {
        Write-Warning "Update check was skipped: $($_.Exception.Message)"
    }
}

$State = Read-State
$VersionRoot = Join-Path (Join-Path $InstallRoot "versions") ([string]$State.current_version)
if (-not (Test-Path -LiteralPath (Join-Path $VersionRoot "START_HERE.bat"))) {
    throw "Current version is incomplete: $VersionRoot"
}
Ensure-RuntimeLink $VersionRoot
Ensure-PersistentLinks $VersionRoot
$env:STREAMER_CAPTION_USER_DATA = (Join-Path $InstallRoot "user-data")
Push-Location $VersionRoot
try {
    & (Join-Path $VersionRoot "START_HERE.bat")
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($exitCode -and $State.previous_version) {
    Write-Warning "The application exited with code $exitCode. Previous version: $($State.previous_version)"
    $answer = Read-Host "Roll back to the previous version? (Y/N)"
    if ($answer -match '^[Yy]') {
        & $RuntimePython $Updater rollback --install-root $InstallRoot
    }
}
exit $exitCode
