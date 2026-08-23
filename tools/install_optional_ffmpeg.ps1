$ErrorActionPreference = "Stop"

if (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue) {
    Write-Host "FFmpeg is already installed."
    exit 0
}
if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
    throw "winget is unavailable. Install FFmpeg manually and add it to PATH."
}
Write-Host "Installing optional FFmpeg component..."
& winget.exe install --id Gyan.FFmpeg --exact --silent --accept-source-agreements --accept-package-agreements
if ($LASTEXITCODE -ne 0) {
    throw "FFmpeg installation failed with exit code $LASTEXITCODE"
}
Write-Host "FFmpeg installation completed. Reopen Streamer Caption before using it."
