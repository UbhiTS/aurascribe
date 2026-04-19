# Build the Python sidecar into a standalone Windows distribution.
#
# Output:
#   sidecar/dist/aurascribe-sidecar/           (PyInstaller onedir)
#     aurascribe-sidecar.exe                   (entry binary)
#     _internal/                                (Python + deps + CUDA DLLs)
#
# The Tauri installer (via `bundle.resources` in tauri.conf.json) copies
# this entire folder into the installed app's `resources/aurascribe-sidecar/`,
# and src-tauri/src/lib.rs spawns the .exe directly.
#
# PREREQUISITES
#   * A Python 3.13 venv at ../.venv with `pip install -e ./sidecar[all]`
#     already done. CUDA 12 wheels (nvidia-cublas-cu12, nvidia-cudnn-cu12)
#     must be present so PyInstaller can collect their DLLs.
#   * PyInstaller installed in the venv. This script installs it if missing.
#
# RUN
#   powershell -NoProfile -File sidecar/build.ps1
#   (or: npm run build:sidecar)
#
# Works on Windows PowerShell 5.1 (shipped with every Windows install) and
# PowerShell 7+. No PS7-only features used.

param(
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$sidecarDir = Split-Path -Parent $PSCommandPath
$repoRoot   = Split-Path -Parent $sidecarDir
$venvPy     = Join-Path $repoRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPy)) {
    throw "Python venv not found at $venvPy.`n" +
          "First-time setup:`n" +
          "  py -3.13 -m venv .venv`n" +
          "  .venv\Scripts\pip install -e ./sidecar[all] pyinstaller"
}

Push-Location $sidecarDir
try {
    Write-Host "==> Ensuring PyInstaller is installed" -ForegroundColor Cyan
    & $venvPy -m pip install --quiet --upgrade pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller failed" }

    if ($Clean -or (Test-Path 'build')) {
        Write-Host "==> Cleaning prior build output" -ForegroundColor Cyan
        Remove-Item -Recurse -Force 'build', 'dist' -ErrorAction SilentlyContinue
    }

    Write-Host "==> Running PyInstaller (this takes several minutes)" -ForegroundColor Cyan
    & $venvPy -m PyInstaller aurascribe-sidecar.spec --clean --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller exited with code $LASTEXITCODE" }

    $exe = Join-Path $sidecarDir 'dist\aurascribe-sidecar\aurascribe-sidecar.exe'
    if (-not (Test-Path $exe)) {
        throw "Expected output at $exe but it was not produced."
    }

    $bundleDir = Split-Path $exe
    $sizeMB = [math]::Round(((Get-ChildItem -Recurse $bundleDir | Measure-Object Length -Sum).Sum / 1MB), 1)
    Write-Host "==> Sidecar built: $bundleDir ($sizeMB MB)" -ForegroundColor Green
}
finally {
    Pop-Location
}
