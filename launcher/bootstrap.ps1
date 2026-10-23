param(
    [string]$InstallRoot = "",
    [switch]$NoUpdate,
    [switch]$Menu
)

# Portable "one button" entry point.
# 1. Make sure the shared Python runtime exists (download + verify once, or use a
#    runtime ZIP the user placed next to StreamerCaption.bat for offline setups).
# 2. Prepare user-data on first run and pick a GPU profile.
# 3. Hand over to launcher.ps1 (update check, junctions, start captions).

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
if (-not $InstallRoot) { $InstallRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..")) }
$RuntimeRoot = Join-Path $InstallRoot "runtime\.venv"
$RuntimePython = Join-Path $RuntimeRoot "Scripts\python.exe"
$RuntimeInfoPath = Join-Path $InstallRoot "runtime.json"
$StatePath = Join-Path $InstallRoot "state.json"
$UserData = Join-Path $InstallRoot "user-data"
$Downloads = Join-Path $InstallRoot "downloads"

function Say([string]$Text) { Write-Host $Text -ForegroundColor Cyan }

function Get-FileSha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Download-WithProgress([string]$Url, [string]$Target, [long]$ExpectedSize) {
    # Windows PowerShell 5.1 (the default on every Windows 10/11 PC) does not load
    # System.Net.Http on its own, and may still default to TLS 1.0 for GitHub.
    Add-Type -AssemblyName System.Net.Http
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Target) | Out-Null
    $partial = "$Target.partial"
    if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
    $client = [Net.Http.HttpClient]::new()
    $client.DefaultRequestHeaders.UserAgent.ParseAdd("streamer-caption-bootstrap")
    try {
        $response = $client.GetAsync($Url, [Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) { throw "download failed: HTTP $([int]$response.StatusCode) $Url" }
        $total = $response.Content.Headers.ContentLength
        if (-not $total) { $total = $ExpectedSize }
        $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $file = [IO.File]::Create($partial)
        try {
            $buffer = New-Object byte[] (1024 * 1024)
            $done = 0L; $lastPercent = -1
            while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $file.Write($buffer, 0, $read)
                $done += $read
                if ($total -gt 0) {
                    $percent = [int](($done * 100) / $total)
                    if ($percent -ne $lastPercent) {
                        Write-Progress -Activity "실행 환경 받는 중 (한 번만)" -Status ("{0} MB / {1} MB" -f [int]($done / 1MB), [int]($total / 1MB)) -PercentComplete $percent
                        $lastPercent = $percent
                    }
                }
            }
        } finally {
            $file.Dispose(); $stream.Dispose()
            Write-Progress -Activity "실행 환경 받는 중 (한 번만)" -Completed
        }
    } finally {
        $client.Dispose()
    }
    Move-Item -LiteralPath $partial -Destination $Target -Force
}

function Install-RuntimeArchive([string]$Zip, [string]$ExpectedSha, [string]$Version) {
    $actual = Get-FileSha256 $Zip
    if ($ExpectedSha -and $actual -ne $ExpectedSha.ToLowerInvariant()) {
        Remove-Item -LiteralPath $Zip -Force
        throw "실행 환경 파일 검증에 실패했습니다 (SHA-256 불일치). 다시 실행하면 새로 받습니다."
    }
    $staging = Join-Path $InstallRoot ("runtime\.staging-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($Zip, $staging)
        $extracted = Join-Path $staging ".venv"
        if (-not (Test-Path -LiteralPath (Join-Path $extracted "Scripts\python.exe"))) { throw "runtime archive has no .venv\Scripts\python.exe" }
        if (Test-Path -LiteralPath $RuntimeRoot) {
            $old = Join-Path $InstallRoot ("runtime\.old-" + [guid]::NewGuid().ToString("N"))
            Move-Item -LiteralPath $RuntimeRoot -Destination $old
            Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction SilentlyContinue
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $RuntimeRoot) | Out-Null
        Move-Item -LiteralPath $extracted -Destination $RuntimeRoot
    } finally {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Ensure-Runtime {
    if (-not (Test-Path -LiteralPath $RuntimeInfoPath)) {
        if (Test-Path -LiteralPath $RuntimePython) { return }
        throw "runtime.json이 없고 실행 환경도 없습니다. 배포 ZIP을 다시 풀어 주세요."
    }
    $info = Get-Content -LiteralPath $RuntimeInfoPath -Raw | ConvertFrom-Json
    $wanted = [string]$info.runtime_version
    $current = ""
    $marker = Join-Path $RuntimeRoot "RUNTIME_VERSION"
    if ((Test-Path -LiteralPath $RuntimePython) -and (Test-Path -LiteralPath $marker)) {
        $current = (Get-Content -LiteralPath $marker -Raw).Trim()
    }
    if ($current -eq $wanted) { return }

    $assetName = [string]$info.asset_name
    $local = Join-Path $InstallRoot $assetName
    $cached = Join-Path $Downloads $assetName
    if (Test-Path -LiteralPath $local) {
        Say "옆에 있는 실행 환경 파일을 사용합니다: $assetName"
        Install-RuntimeArchive $local ([string]$info.sha256) $wanted
        return
    }
    if (-not (Test-Path -LiteralPath $cached)) {
        Say "처음 실행이라 실행 환경(약 $([int]([long]$info.size / 1MB)) MB)을 받습니다. 한 번만 받고 다음부터는 바로 시작합니다."
        Download-WithProgress ([string]$info.url) $cached ([long]$info.size)
    }
    Install-RuntimeArchive $cached ([string]$info.sha256) $wanted
    Remove-Item -LiteralPath $cached -Force -ErrorAction SilentlyContinue
}

function Ensure-State {
    $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    $version = [string]$state.current_version
    $versionRoot = Join-Path (Join-Path $InstallRoot "versions") $version
    if (-not (Test-Path -LiteralPath (Join-Path $versionRoot "main.py"))) { throw "앱 파일이 없습니다: $versionRoot" }
    return $versionRoot
}

function Ensure-UserData([string]$VersionRoot) {
    $firstRun = -not (Test-Path -LiteralPath (Join-Path $UserData "config.json"))
    New-Item -ItemType Directory -Force -Path $UserData | Out-Null
    $junction = Join-Path $VersionRoot ".venv"
    if (-not (Test-Path -LiteralPath $junction)) {
        & cmd.exe /d /c "mklink /J `"$junction`" `"$RuntimeRoot`"" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "실행 환경 연결(junction)에 실패했습니다." }
    }
    if ($firstRun) {
        Say "처음 실행 준비 중: 설정 파일을 만들고 그래픽카드에 맞는 프로필을 고릅니다."
        & $RuntimePython (Join-Path $VersionRoot "tools\bootstrap_user_data.py") --user-data $UserData | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "설정 초기화에 실패했습니다." }
        $env:STREAMER_CAPTION_USER_DATA = $UserData
        & $RuntimePython (Join-Path $VersionRoot "tools\apply_auto_gpu_preset.py")
        if ($LASTEXITCODE -ne 0) { Write-Warning "GPU 프로필을 자동으로 고르지 못했습니다. 기본값으로 시작합니다." }
        Set-Content -LiteralPath (Join-Path $UserData "first-run.flag") -Value (Get-Date).ToString("o") -Encoding utf8
    }
    if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot "updater.json"))) {
        Copy-Item -LiteralPath (Join-Path $VersionRoot "updater.example.json") -Destination (Join-Path $InstallRoot "updater.json")
    }
}

try {
    if (-not (Test-Path -LiteralPath $StatePath)) { throw "state.json이 없습니다. 배포 ZIP을 통째로 풀어 주세요." }
    Ensure-Runtime
    $versionRoot = Ensure-State
    Ensure-UserData $versionRoot
} catch {
    Write-Host ""
    Write-Host "시작하지 못했습니다: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "같은 오류가 반복되면 downloads 폴더를 지우고 다시 실행하거나, GitHub 이슈로 알려 주세요."
    exit 1
}

# Hashtable splatting binds by parameter name; an array would be passed positionally.
$launcherArgs = @{ InstallRoot = $InstallRoot }
if ($NoUpdate) { $launcherArgs.NoUpdate = $true }
if (-not $Menu) { $launcherArgs.Direct = $true }
& (Join-Path $PSScriptRoot "launcher.ps1") @launcherArgs
exit $LASTEXITCODE
