"""Download + hydrate the CUDA runtime bundle on first launch.

The CUDA installer variant ships WITHOUT the big CUDA DLLs
(torch_cuda.dll, cudnn_*, cublas_*, nvidia-cu12 wheels, etc.) because
NSIS's 32-bit makensis crashes with "Internal compiler error #12345:
error mmapping datablock" on the >1 GB payload — and WiX's 32-bit
light.exe OOMs the same way.

CI's release workflow moves every file >= 50 MB plus everything under
`_internal/nvidia/` out of the PyInstaller bundle into a sibling zip,
uploaded as a release asset (AuraScribe-CUDA-runtime-v<version>.zip).
The sidecar detects a marker file planted alongside it during that
split and, on first launch, streams the zip from the GitHub Release
and extracts it back into its own bundle dir — restoring the original
structure PyInstaller + `_wire_windows_cuda_dlls` already know how
to find DLLs in.

CPU variant has no marker and this module is a no-op.
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# Intentionally minimal imports and no log dependency: this module runs
# before anything else in `aurascribe/__init__.py` so it can prep DLLs
# before torch/ctranslate2 get imported. Print directly to stdout so the
# parent Tauri process still surfaces progress in its attached console.

_REPO = "UbhiTS/aurascribe"
_ZIP_NAME_FMT = "AuraScribe-CUDA-runtime-v{version}.zip"
_MARKER_REL = Path("_internal") / "aurascribe" / "requires_cuda_runtime.txt"
_READY_REL = Path(".cuda_runtime_ready")


def _bundle_root() -> Path | None:
    """Root of the PyInstaller onedir bundle, or None when running from source."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return None


def ensure() -> None:
    """If the CUDA-variant marker is present and the runtime hasn't been
    hydrated yet (or is from a different version than this build), pull
    the zip from the release asset and extract it into the bundle dir.
    Blocks until done. No-op for CPU variant or dev runs."""
    root = _bundle_root()
    if root is None:
        return
    marker = root / _MARKER_REL
    if not marker.is_file():
        return
    want = marker.read_text(encoding="utf-8", errors="ignore").strip()
    if not want:
        return
    ready = root / _READY_REL
    if ready.is_file() and ready.read_text(encoding="utf-8", errors="ignore").strip() == want:
        return

    # Print early so the splash-visible console shows a clear "one-time
    # setup" message rather than a 5-minute "Loading..." silence.
    print(
        f"== AuraScribe: hydrating CUDA runtime v{want} "
        f"(one-time ~1 GB download, ~5-10 min on typical broadband)",
        flush=True,
    )
    try:
        _download_and_extract(root, want)
    except Exception as e:
        # Don't leave a partial state pretending to be ready — let the
        # next launch retry. Re-raise so startup fails loudly instead of
        # crashing later with cryptic "DLL not found" errors.
        ready.unlink(missing_ok=True)
        raise RuntimeError(
            f"Failed to fetch AuraScribe CUDA runtime v{want}: {e}. "
            f"Check your internet connection and try launching again. "
            f"If the problem persists, the release asset may be missing."
        ) from e
    ready.write_text(want, encoding="utf-8")
    print("== AuraScribe: CUDA runtime ready", flush=True)


def _download_and_extract(root: Path, version: str) -> None:
    zip_name = _ZIP_NAME_FMT.format(version=version)
    url = f"https://github.com/{_REPO}/releases/download/v{version}/{zip_name}"
    tmp = root / f".{zip_name}.part"
    try:
        _download(url, tmp)
        _extract(tmp, root)
    finally:
        tmp.unlink(missing_ok=True)


def _download(url: str, dest: Path) -> None:
    # Follow redirects by default (GH release assets redirect to S3).
    # `urlopen` raises URLError subclasses on network failure; we let
    # those bubble up to `ensure`'s except block.
    with urllib.request.urlopen(url, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        with open(dest, "wb") as out:
            chunk = 1024 * 1024  # 1 MB reads
            read = 0
            last_logged = 0
            while True:
                data = resp.read(chunk)
                if not data:
                    break
                out.write(data)
                read += len(data)
                # Log every 25 MB — roughly every 1-2 s on typical
                # broadband. More frequent would spam; less frequent
                # looks hung.
                if read - last_logged >= 25 * 1024 * 1024:
                    if total:
                        pct = int(100 * read / total)
                        print(
                            f"  CUDA runtime: {read // (1024 * 1024)} / "
                            f"{total // (1024 * 1024)} MB ({pct}%)",
                            flush=True,
                        )
                    else:
                        print(
                            f"  CUDA runtime: {read // (1024 * 1024)} MB "
                            f"(server didn't report total)",
                            flush=True,
                        )
                    last_logged = read


def _extract(zip_path: Path, target: Path) -> None:
    with zipfile.ZipFile(zip_path) as z:
        # `extractall` writes relative paths as-is; we rely on the CI
        # script having built the zip with paths relative to the bundle
        # root so _internal/nvidia/... lands exactly where PyInstaller
        # would have put it had it been bundled in the first place.
        z.extractall(target)
