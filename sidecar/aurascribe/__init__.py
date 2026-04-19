"""AuraScribe sidecar package."""
from __future__ import annotations

import warnings as _warnings

__version__ = "0.1.0"

# Silence the noisy UserWarning pyannote.audio 3.3+ emits when torchcodec's
# DLLs fail to load on Windows. torchcodec is a transitive dep pulled in by
# pyannote for audio decoding; it tries to load libtorchcodec_core{4-8}.dll
# at import time and spews ~40 lines of traceback as a *warning* when none
# of the probed ABI versions exist. pyannote falls back to a working
# decoder either way, so the warning is pure log noise.
_warnings.filterwarnings(
    "ignore",
    message=r".*libtorchcodec_core\d+\.dll.*",
    category=UserWarning,
)
_warnings.filterwarnings(
    "ignore",
    message=r".*torchcodec.*",
    category=UserWarning,
)


def _wire_windows_cuda_dlls() -> None:
    """Make CUDA 12 cuBLAS/cuDNN DLLs discoverable on Windows.

    CTranslate2 (faster-whisper's backend) is linked against CUDA 12.
    PyTorch nightly cu130 ships CUDA 13's cuBLAS, which doesn't satisfy it.
    We install `nvidia-cublas-cu12` + `nvidia-cudnn-cu12` (Windows wheels),
    but their `bin/` dirs aren't on PATH — we add them to Python's DLL search
    path here, before `faster_whisper` is imported anywhere.
    """
    import os
    import site
    import sys
    from pathlib import Path

    if sys.platform != "win32":
        return

    seen: set[str] = set()
    candidates = list(site.getsitepackages())
    user_site = site.getusersitepackages()
    if user_site:
        candidates.append(user_site)

    extra_paths: list[str] = []
    for sp in candidates:
        nvidia = Path(sp) / "nvidia"
        if not nvidia.is_dir():
            continue
        for pkg in nvidia.iterdir():
            bin_dir = pkg / "bin"
            if bin_dir.is_dir() and str(bin_dir) not in seen:
                seen.add(str(bin_dir))
                # Python 3.8+ DLL search: affects extensions loaded after this call.
                os.add_dll_directory(str(bin_dir))
                extra_paths.append(str(bin_dir))

    if extra_paths:
        # Also prepend to PATH — CTranslate2's runtime LoadLibrary calls for
        # cuBLAS/cuDNN resolve against PATH, not the Python DLL search path.
        os.environ["PATH"] = os.pathsep.join(extra_paths + [os.environ.get("PATH", "")])


_wire_windows_cuda_dlls()
