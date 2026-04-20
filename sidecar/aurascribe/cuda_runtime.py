"""Download + hydrate the CUDA runtime bundle on first launch.

The CUDA installer variant ships WITHOUT the big CUDA DLLs
(torch_cuda.dll, cudnn_*, cublas_*, nvidia-cu12 wheels, etc.) because
NSIS's 32-bit makensis crashes with "Internal compiler error #12345:
error mmapping datablock" on the >1 GB payload — and WiX's 32-bit
light.exe OOMs the same way.

CI's release workflow moves every file >= 50 MB plus everything under
`_internal/nvidia/` out of the PyInstaller bundle. Those files are
bin-packed into multiple < 2 GB zip parts (GitHub Release assets are
capped at 2 GB per file) alongside a small manifest listing the parts.
The sidecar fetches the manifest first, then each part, then extracts
every part into its own bundle dir — restoring the original layout
PyInstaller + `_wire_windows_cuda_dlls` already know how to find DLLs
in.

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
_BASE_NAME_FMT = "AuraScribe-CUDA-runtime-v{version}"
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
    the manifest + each zip part from the release and extract them into
    the bundle dir. Blocks until done. No-op for CPU variant or dev
    runs."""
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

    print(
        f"== AuraScribe: hydrating CUDA runtime v{want} "
        f"(one-time ~4 GB download split across multiple parts, "
        f"~10-20 min on typical broadband)",
        flush=True,
    )
    try:
        _download_and_extract(root, want)
    except Exception as e:
        # Don't leave a partial state pretending to be ready — let the
        # next launch retry. Re-raise so startup fails loudly instead
        # of crashing later with cryptic "DLL not found" errors.
        ready.unlink(missing_ok=True)
        raise RuntimeError(
            f"Failed to fetch AuraScribe CUDA runtime v{want}: {e}. "
            f"Check your internet connection and try launching again. "
            f"If the problem persists, the release asset may be missing."
        ) from e
    ready.write_text(want, encoding="utf-8")
    print("== AuraScribe: CUDA runtime ready", flush=True)


def _download_and_extract(root: Path, version: str) -> None:
    base = _BASE_NAME_FMT.format(version=version)
    release_url = f"https://github.com/{_REPO}/releases/download/v{version}"

    # 1. Pull the manifest first so we know exactly how many parts exist
    # and what their filenames are. Tiny file — fetched into memory.
    manifest_url = f"{release_url}/{base}.manifest.txt"
    manifest_body = _fetch_text(manifest_url)
    manifest = _parse_manifest(manifest_body)
    parts_count = int(manifest.get("parts", "0"))
    if parts_count <= 0:
        raise RuntimeError(f"Manifest reports 0 parts: {manifest_body!r}")
    if manifest.get("version") != version:
        raise RuntimeError(
            f"Manifest version {manifest.get('version')!r} doesn't match "
            f"marker version {version!r}"
        )

    # 2. Download + extract each part in turn. Each part is a valid zip
    # on its own — no concatenation required. We delete each part after
    # extract to keep peak disk usage to ~1 part's worth.
    for idx in range(1, parts_count + 1):
        key = f"part{idx}"
        part_name = manifest.get(key)
        if not part_name:
            raise RuntimeError(f"Manifest missing {key}: {manifest_body!r}")
        print(
            f"== AuraScribe: downloading CUDA runtime part {idx}/{parts_count}",
            flush=True,
        )
        part_url = f"{release_url}/{part_name}"
        tmp = root / f".{part_name}.part"
        try:
            _download(part_url, tmp)
            print(
                f"== AuraScribe: extracting part {idx}/{parts_count}",
                flush=True,
            )
            _extract(tmp, root)
        finally:
            tmp.unlink(missing_ok=True)


def _fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_manifest(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _download(url: str, dest: Path) -> None:
    # Follow redirects by default (GH release assets redirect to S3).
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
                # Log every 50 MB — frequent enough to show progress,
                # infrequent enough to avoid console spam.
                if read - last_logged >= 50 * 1024 * 1024:
                    if total:
                        pct = int(100 * read / total)
                        print(
                            f"  ...{read // (1024 * 1024)} / "
                            f"{total // (1024 * 1024)} MB ({pct}%)",
                            flush=True,
                        )
                    else:
                        print(
                            f"  ...{read // (1024 * 1024)} MB "
                            f"(total unknown)",
                            flush=True,
                        )
                    last_logged = read


def _extract(zip_path: Path, target: Path) -> None:
    with zipfile.ZipFile(zip_path) as z:
        # `extractall` writes relative paths as-is; CI stages each part
        # zip with paths relative to the bundle root, so _internal/...
        # lands exactly where PyInstaller would have put it.
        z.extractall(target)
