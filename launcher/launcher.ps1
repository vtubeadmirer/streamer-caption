param(
    [string]$InstallRoot = "",
    [switch]$NoUpdate,
    [switch]$Direct
)

# Starts the current app version from the side-by-side layout.
#   default : START_HERE.bat menu (installed / advanced use)
#   -Direct : start live captions immediately and open the browser on first run
#             (portable one-button mode, called by bootstrap.ps1)

$ErrorActionPreference = "Stop"
if (-not $InstallRoot) {
    $InstallRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
}
$StatePath = Join-Path $InstallRoot "state.json"
$RuntimePython = Join-Path $InstallRoot "runtime\.venv\Scripts\python.exe"
$Updater = Join-Path $PSScriptRoot "updater.py"
$UserData = Join-Path $InstallRoot "user-data"
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
    $Links = @{
        "logs" = (Join-Path $UserData "logs")
        "reports" = (Join-Path $UserData "reports")
        "samples" = (Join-Path $UserData "samples")
    }
    foreach ($Name in $Links.Keys) {
        New-Item -ItemType Directory -Force -Path $Links[$Name] | Out-Null
        $Link = Join-Path $VersionRoot $Name
        if (-not (Test-Path -LiteralPath $Link)) {
            & cmd.exe /d /c "mklink /J `"$Link`" `"$($Links[$Name])`"" | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Could not connect persistent data: $Name" }
        }
    }
}

function Get-AdminPort {
    $configPath = Join-Path $UserData "config.json"
    try {
        if (Test-Path -LiteralPath $configPath) {
            $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
            if ($config.server -and $config.server.admin_port) { return [int]$config.server.admin_port }
        }
    } catch { }
    return 8766
}

function Open-WhenReady([string]$Url, [int]$Port, [int]$TimeoutSec) {
    # Background job: open the page once the admin server answers, so the app keeps the foreground.
    $script = {
        param($Url, $Port, $TimeoutSec)
        $deadline = (Get-Date).AddSeconds($TimeoutSec)
        while ((Get-Date) -lt $deadline) {
            try {
                $client = New-Object Net.Sockets.TcpClient
                $client.Connect("127.0.0.1", $Port)
                $client.Close()
                Start-Process $Url
                return
            } catch { Start-Sleep -Milliseconds 500 }
        }
    }
    Start-Job -ScriptBlock $script -ArgumentList $Url, $Port, $TimeoutSec | Out-Null
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
                if ($Direct) {
                    # One-button mode: the admin page shows an update banner with a button; no console questions.
                    Write-Host "Open the admin page and press 'Update now' when convenient."
                } else {
                    $answer = Read-Host "Update now? (Y/N)"
                    if ($answer -match '^[Yy]') {
                        & $RuntimePython $Updater update --install-root $InstallRoot
                        if ($LASTEXITCODE -ne 0) { Write-Warning "Update failed. The current version was not changed." }
                    }
                }
            }
        }
    } catch {
        Write-Warning "Update check was skipped: $($_.Exception.Message)"
    }
}

# Exit code 76 means "start me again": the admin page asked for a restart after a
# settings change or an in-app update. state.json is re-read so a new version starts.
$RelaunchExitCode = 76
# 77 tells bootstrap.ps1 that an update staged a new runtime (runtime.json differs from the
# installed marker); bootstrap swaps it in while no Python is running, then starts us again.
$RuntimeSwapExitCode = 77

function Test-RuntimePending {
    $info = Join-Path $InstallRoot "runtime.json"
    $marker = Join-Path $InstallRoot "runtime\.venv\RUNTIME_VERSION"
    if (-not (Test-Path -LiteralPath $info) -or -not (Test-Path -LiteralPath $marker)) { return $false }
    try {
        $wanted = [string](Get-Content -LiteralPath $info -Raw | ConvertFrom-Json).runtime_version
        $current = (Get-Content -LiteralPath $marker -Raw).Trim()
        return ($wanted -and $wanted -ne $current)
    } catch { return $false }
}
$env:STREAMER_CAPTION_USER_DATA = $UserData
$env:STREAMER_CAPTION_SUPERVISED = "1"
do {
    $State = Read-State
    $VersionRoot = Join-Path (Join-Path $InstallRoot "versions") ([string]$State.current_version)
    if (-not (Test-Path -LiteralPath (Join-Path $VersionRoot "main.py"))) {
        throw "Current version is incomplete: $VersionRoot"
    }
    Ensure-RuntimeLink $VersionRoot
    Ensure-PersistentLinks $VersionRoot

    Push-Location $VersionRoot
    try {
        if ($Direct) {
            $port = Get-AdminPort
            $firstRunFlag = Join-Path $UserData "first-run.flag"
            if (Test-Path -LiteralPath $firstRunFlag) {
                Open-WhenReady "http://127.0.0.1:$port/setup" $port 120
                Remove-Item -LiteralPath $firstRunFlag -Force -ErrorAction SilentlyContinue
            }
            Write-Host "Live captions are starting (version $($State.current_version)). Closing this window stops them. Admin page: http://127.0.0.1:$port/admin" -ForegroundColor Green
            & $RuntimePython (Join-Path $VersionRoot "tools\run_supervised.py")
            $exitCode = $LASTEXITCODE
        } else {
            & (Join-Path $VersionRoot "START_HERE.bat")
            $exitCode = $LASTEXITCODE
        }
    } finally {
        Pop-Location
        Get-Job | Remove-Job -Force -ErrorAction SilentlyContinue
    }
    if ($exitCode -eq $RelaunchExitCode) {
        if (Test-RuntimePending) {
            Write-Host ""
            Write-Host "A new runtime was downloaded; installing it before restarting..." -ForegroundColor Cyan
            exit $RuntimeSwapExitCode
        }
        Write-Host ""
        Write-Host "Restarting Streamer Caption..." -ForegroundColor Cyan
        Start-Sleep -Seconds 1
    }
} while ($exitCode -eq $RelaunchExitCode)

if ($exitCode -and $State.previous_version) {
    Write-Warning "The application exited with code $exitCode. Previous version: $($State.previous_version)"
    $answer = Read-Host "Roll back to the previous version? (Y/N)"
    if ($answer -match '^[Yy]') {
        & $RuntimePython $Updater rollback --install-root $InstallRoot
    }
}
exit $exitCode
