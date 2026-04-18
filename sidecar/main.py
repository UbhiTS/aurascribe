"""AuraScribe Python sidecar entry point.

Binds to 127.0.0.1 by default — only the Tauri shell reaches it.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def _register_cuda_dll_dirs() -> None:
    """Windows Python 3.8+ ignores PATH for DLL search. Register the nvidia-*
    pip packages' bin dirs so ctranslate2 (faster-whisper) finds cuBLAS/cuDNN.
    No-op on non-Windows or if the DLLs aren't installed."""
    if sys.platform != "win32":
        return
    try:
        import nvidia  # type: ignore
    except ImportError:
        return
    # `nvidia` is a PEP 420 namespace package — no __file__. Use __path__.
    for root_str in getattr(nvidia, "__path__", []) or []:
        nvidia_root = Path(root_str)
        for sub in ("cublas/bin", "cudnn/bin", "cuda_nvrtc/bin"):
            p = nvidia_root / sub
            if p.is_dir():
                os.add_dll_directory(str(p))


_register_cuda_dll_dirs()

import uvicorn

from aurascribe.api import app


def main() -> None:
    # Route our own loggers (aurascribe.*) to stdout at INFO so speaker-id
    # diagnostics and pipeline traces show up in `tauri dev`'s console.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logging.getLogger("aurascribe").setLevel(logging.INFO)

    host = os.environ.get("SIDECAR_HOST", "127.0.0.1")
    port = int(os.environ.get("SIDECAR_PORT", "8765"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
