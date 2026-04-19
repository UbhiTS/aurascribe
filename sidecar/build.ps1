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

function Test-GPUPresent {
    # nvidia-smi is installed with every NVIDIA driver and on PATH by default.
    # Exit 0 iff it can enumerate at least one GPU. Wrapping stderr/stdout so
    # machines without the driver don't spam the console on this check.
    try {
        $null = & nvidia-smi --query-gpu=name --format=csv,noheader 2>&1
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Get-TorchVariant($py) {
    # Returns 'cuda', 'cpu', 'missing', or 'unknown' based on the installed
    # wheel. PyTorch encodes the variant in __version__ (e.g. '2.5.1+cu128'
    # or '2.5.1+cpu'). Wheels with no suffix are rare — usually a dev build
    # that we'd want to leave alone.
    $ver = & $py -c "import torch; print(torch.__version__)" 2>&1
    if ($LASTEXITCODE -ne 0 -or -not $ver) { return 'missing' }
    if ($ver -match '\+cu\d+') { return 'cuda' }
    if ($ver -match '\+cpu')   { return 'cpu' }
    return 'unknown'
}

Push-Location $sidecarDir
try {
    # ── Align PyTorch with host hardware (local dev only) ──────────────────
    #
    # faster-whisper uses ctranslate2 (its own CUDA bindings, independent of
    # torch). Pyannote diarization uses torch directly — and only moves to
    # GPU when torch.cuda.is_available() returns True, which requires a
    # CUDA-enabled torch wheel. If the user has an NVIDIA GPU but their
    # venv is on torch+cpu, diarization silently stays on CPU.
    #
    # Auto-swap the wheel so that bringing up a fresh dev environment (or
    # an existing one installed before CUDA support was needed) Just Works
    # without the user having to know about pip's `--index-url` dance.
    #
    # In CI the matrix step pre-installs the variant-correct wheel before
    # calling this script — we detect that and skip the swap so we don't
    # clobber the intended release configuration.
    $inCI = $env:CI -eq 'true' -or $env:GITHUB_ACTIONS -eq 'true'
    if (-not $inCI) {
        $gpu = Test-GPUPresent
        $torchVariant = Get-TorchVariant $venvPy
        $desired = if ($gpu) { 'cuda' } else { 'cpu' }
        $gpuLabel = if ($gpu) { 'present' } else { 'none' }
        Write-Host "==> Host GPU: $gpuLabel  |  torch: $torchVariant  |  desired: $desired" -ForegroundColor Cyan

        if ($torchVariant -ne $desired -and $torchVariant -ne 'unknown') {
            $index = if ($desired -eq 'cuda') { 'cu128' } else { 'cpu' }
            $url = "https://download.pytorch.org/whl/$index"
            Write-Host "==> Reinstalling torch ($torchVariant -> $desired) from $url" -ForegroundColor Yellow
            & $venvPy -m pip install --upgrade --force-reinstall --index-url $url torch torchaudio
            if ($LASTEXITCODE -ne 0) { throw "Failed to install torch/$desired wheel" }
        }
        else {
            Write-Host "==> torch already aligned with host" -ForegroundColor Green
        }
    }
    else {
        Write-Host "==> CI detected; skipping torch auto-alignment (matrix already pinned the wheel)" -ForegroundColor Cyan
    }

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
