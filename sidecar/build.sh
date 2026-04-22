#!/usr/bin/env bash
# Build the Python sidecar into a standalone macOS distribution.
#
# Output:
#   sidecar/dist/aurascribe-sidecar/        (PyInstaller onedir)
#     aurascribe-sidecar                    (entry binary, no .exe on macOS)
#     _internal/                            (Python + deps)
#
# The Tauri bundle (via bundle.resources in tauri.conf.json) copies this
# folder into the app's Resources/; src-tauri/src/lib.rs spawns the binary.
#
# PREREQUISITES
#   * A Python 3.13 venv at ../.venv with:
#       pip install -e ./sidecar[all]
#   * PyInstaller installed in the venv. This script installs it if missing.
#
# RUN
#   bash sidecar/build.sh          (or: npm run build:sidecar on macOS)
#   bash sidecar/build.sh --clean  (wipe prior build output first)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_PY="$REPO_ROOT/.venv/bin/python3"

# ── Argument parsing ──────────────────────────────────────────────────────────
CLEAN=0
for arg in "$@"; do
    case "$arg" in
        --clean|-c) CLEAN=1 ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

# ── Pre-flight checks ─────────────────────────────────────────────────────────
if [[ ! -f "$VENV_PY" ]]; then
    echo "ERROR: Python venv not found at $VENV_PY" >&2
    echo "First-time setup:" >&2
    echo "  python3.13 -m venv .venv" >&2
    echo "  .venv/bin/pip install -e ./sidecar[all] pyinstaller" >&2
    exit 1
fi

# ── Torch alignment (local dev only) ─────────────────────────────────────────
#
# On macOS, the regular PyPI torch wheel includes MPS (Metal) support for
# Apple Silicon. No special index is needed. In CI the matrix step pre-pins
# the wheel before calling this script, so we skip any reinstall there.
IN_CI="${CI:-false}"
if [[ "$IN_CI" != "true" && "${GITHUB_ACTIONS:-false}" != "true" ]]; then
    echo "==> Checking torch installation..."
    TORCH_VER=$("$VENV_PY" -c "import torch; print(torch.__version__)" 2>/dev/null || echo "missing")
    MPS_OK=$("$VENV_PY" -c \
        "import torch; print('ok' if torch.backends.mps.is_available() else 'cpu')" \
        2>/dev/null || echo "cpu")
    echo "    torch: $TORCH_VER  |  MPS: $MPS_OK"
    if [[ "$TORCH_VER" == "missing" ]]; then
        echo "==> Installing torch (MPS-capable, from PyPI)..."
        "$VENV_PY" -m pip install torch torchaudio
    fi
else
    echo "==> CI detected — skipping torch auto-alignment (matrix already pinned the wheel)"
fi

# ── PyInstaller ───────────────────────────────────────────────────────────────
echo "==> Ensuring PyInstaller is installed..."
"$VENV_PY" -m pip install --quiet --upgrade pyinstaller

cd "$SCRIPT_DIR"

if [[ "$CLEAN" -eq 1 ]] || [[ -d build ]]; then
    echo "==> Cleaning prior build output..."
    rm -rf build dist
fi

echo "==> Running PyInstaller (this takes several minutes)..."
"$VENV_PY" -m PyInstaller aurascribe-sidecar.spec --clean --noconfirm

BINARY="$SCRIPT_DIR/dist/aurascribe-sidecar/aurascribe-sidecar"
if [[ ! -f "$BINARY" ]]; then
    echo "ERROR: Expected output at $BINARY but it was not produced." >&2
    exit 1
fi

BUNDLE_DIR="$(dirname "$BINARY")"
SIZE_MB=$(du -sh "$BUNDLE_DIR" | cut -f1)
echo "==> Sidecar built: $BUNDLE_DIR ($SIZE_MB)"
