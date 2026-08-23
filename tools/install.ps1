param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "StreamerCaption"),
    [string]$MigrateFrom = "",
    [string]$PythonPath = "",
    [switch]$SkipDependencies
)

$ErrorActionPreference = "Stop"
$SourceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$Version = (Get-Content -LiteralPath (Join-Path $SourceRoot "VERSION") -Raw).Trim()
if ($Version -notmatch '^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$') {
    throw "Invalid VERSION: $Version"
}

function Write-Step([int]$Number, [string]$Text) {
    Write-Host "[$Number/7] $Text" -ForegroundColor Cyan
}

function Find-Python312 {
    $known = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:ProgramFiles "Python312\python.exe")
    )
    foreach ($candidate in $known) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        $path = (& py.exe -3.12 -c "import sys; print(sys.executable)" 2>$null)
        if ($LASTEXITCODE -eq 0 -and $path -and (Test-Path -LiteralPath $path.Trim())) {
            return $path.Trim()
        }
    }
    return $null
}

Write-Step 1 "Checking Python 3.12"
$Python = $null
if ($PythonPath) {
    $Python = [IO.Path]::GetFullPath($PythonPath)
    if (-not (Test-Path -LiteralPath $Python)) { throw "PythonPath does not exist: $Python" }
}
if (-not $Python) { $Python = Find-Python312 }
if (-not $Python) {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw "Python 3.12 is missing and winget is unavailable. Install Python 3.12, then run INSTALL.bat again."
    }
    Write-Host "Python 3.12 is missing. Installing it automatically..."
    & winget.exe install --id Python.Python.3.12 --exact --silent --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) { throw "Python installation failed with exit code $LASTEXITCODE" }
    $Python = Find-Python312
    if (-not $Python) { throw "Python was installed but could not be located. Restart Windows and run INSTALL.bat again." }
}
Write-Host "Python: $Python"

Write-Step 2 "Creating side-by-side install folders"
$VersionsRoot = Join-Path $InstallRoot "versions"
$VersionRoot = Join-Path $VersionsRoot $Version
$RuntimeRoot = Join-Path $InstallRoot "runtime\.venv"
$UserData = Join-Path $InstallRoot "user-data"
$LauncherRoot = Join-Path $InstallRoot "launcher"
New-Item -ItemType Directory -Force -Path $VersionsRoot,$VersionRoot,$UserData,$LauncherRoot,(Join-Path $InstallRoot "downloads") | Out-Null

Write-Step 3 "Copying application version $Version"
$excludeDirectories = @('.git','.venv','venv','runtime','user-data','logs','reports','samples','dist','downloads','__pycache__','.pytest_cache','simulstreaming-runtime')
$excludeFiles = @('config.json','slang_dict.json','whitelist.txt','blocklist.txt','*.pyc','*.pyo','*.wav','*.sqlite3','*.partial')
$arguments = @($SourceRoot,$VersionRoot,'/E','/R:1','/W:1','/NFL','/NDL','/NJH','/NJS','/NP','/XD') + $excludeDirectories + @('/XF') + $excludeFiles
& robocopy.exe @arguments | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Application copy failed with robocopy exit code $LASTEXITCODE" }

Write-Step 4 "Preparing shared Python runtime"
if (-not (Test-Path -LiteralPath (Join-Path $RuntimeRoot "Scripts\python.exe"))) {
    & $Python -m venv $RuntimeRoot
    if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed" }
}
$RuntimePython = Join-Path $RuntimeRoot "Scripts\python.exe"
if (-not $SkipDependencies) {
    & $RuntimePython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
    & $RuntimePython -m pip install -r (Join-Path $VersionRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }
} else {
    Write-Host "Dependency installation skipped for validation."
}

Write-Step 5 "Connecting runtime and persistent user data"
$Junction = Join-Path $VersionRoot ".venv"
if (-not (Test-Path -LiteralPath $Junction)) {
    & cmd.exe /d /c "mklink /J `"$Junction`" `"$RuntimeRoot`"" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Runtime junction creation failed" }
}
$bootstrapArgs = @((Join-Path $VersionRoot "tools\bootstrap_user_data.py"),'--user-data',$UserData)
if ($MigrateFrom) { $bootstrapArgs += @('--migrate-from',[IO.Path]::GetFullPath($MigrateFrom)) }
& $RuntimePython @bootstrapArgs
if ($LASTEXITCODE -ne 0) { throw "User data initialization failed" }
$PersistentLinks = @{
    "logs" = (Join-Path $UserData "logs")
    "reports" = (Join-Path $UserData "reports")
    "samples" = (Join-Path $UserData "samples")
}
foreach ($Name in $PersistentLinks.Keys) {
    $Link = Join-Path $VersionRoot $Name
    if (-not (Test-Path -LiteralPath $Link)) {
        & cmd.exe /d /c "mklink /J `"$Link`" `"$($PersistentLinks[$Name])`"" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Persistent data junction failed: $Name" }
    }
}

Write-Step 6 "Installing launcher and GPU recommendation"
Copy-Item -LiteralPath (Join-Path $VersionRoot "launcher\launcher.ps1") -Destination (Join-Path $LauncherRoot "launcher.ps1") -Force
Copy-Item -LiteralPath (Join-Path $VersionRoot "launcher\updater.py") -Destination (Join-Path $LauncherRoot "updater.py") -Force
Copy-Item -LiteralPath (Join-Path $VersionRoot "launcher\START_STREAMER_CAPTION.bat") -Destination (Join-Path $InstallRoot "START_STREAMER_CAPTION.bat") -Force
if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot "updater.json"))) {
    Copy-Item -LiteralPath (Join-Path $VersionRoot "updater.example.json") -Destination (Join-Path $InstallRoot "updater.json")
}
$env:STREAMER_CAPTION_USER_DATA = $UserData
& $RuntimePython (Join-Path $VersionRoot "tools\apply_auto_gpu_preset.py")
if ($LASTEXITCODE -ne 0) { Write-Warning "GPU preset could not be applied; defaults remain active." }

Write-Step 7 "Writing version state"
$StatePath = Join-Path $InstallRoot "state.json"
$Previous = $null
if (Test-Path -LiteralPath $StatePath) {
    $OldState = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    if ($OldState.current_version -and $OldState.current_version -ne $Version) { $Previous = $OldState.current_version }
}
$State = [ordered]@{
    current_version = $Version
    previous_version = $Previous
    installed_at = (Get-Date).ToString('o')
    install_root = $InstallRoot
}
$TemporaryState = "$StatePath.tmp"
$State | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $TemporaryState -Encoding utf8
Move-Item -LiteralPath $TemporaryState -Destination $StatePath -Force
Write-Host ""
Write-Host "Ready: $(Join-Path $InstallRoot 'START_STREAMER_CAPTION.bat')" -ForegroundColor Green
Write-Host "FFmpeg is optional and is not bundled. Run INSTALL_OPTIONAL_FFMPEG.bat only when an experimental feature requests it."
