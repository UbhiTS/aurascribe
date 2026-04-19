# PyInstaller spec — AuraScribe Python sidecar.
#
# Produces a self-contained onedir bundle at `dist/aurascribe-sidecar/`
# containing `aurascribe-sidecar.exe` plus every DLL + Python module it
# needs. The Tauri installer copies this whole folder into the app's
# resource directory; at runtime, src-tauri/src/lib.rs spawns the .exe
# directly — no Python interpreter required on the user's machine.
#
# Run with: .venv/Scripts/python -m PyInstaller aurascribe-sidecar.spec
#           --clean --noconfirm
# (build.ps1 wraps this.)
#
# NOTES
# -----
# * `collect_all` scrapes every submodule + data file + binary from a
#   package. Heavy for the ML stack but removes an entire class of
#   "ImportError at runtime" failures for packages that import their own
#   submodules lazily (torch, pyannote, librosa, numba).
# * CUDA DLLs: the `nvidia-*-cu12` wheels drop cuBLAS/cuDNN/cuda_nvrtc
#   into `site-packages/nvidia/*/bin`. We scoop those up as binaries so
#   ctranslate2 finds them at runtime. PyInstaller's bootloader puts the
#   bundle dir on the DLL search path automatically on Windows.
# * Bundled prompt files (aurascribe/llm/*.md) ride along under the same
#   package path so config.py's seeding logic still resolves them via
#   `Path(__file__).parent`.

from pathlib import Path
import site

from PyInstaller.utils.hooks import collect_all

block_cipher = None

hiddenimports: list[str] = []
datas: list[tuple] = []
binaries: list[tuple] = []

# ── Heavy / lazy-import packages ─────────────────────────────────────────────
for pkg in (
    # ASR + audio
    "faster_whisper", "ctranslate2", "sounddevice", "soundfile", "soxr",
    "scipy", "librosa", "numba", "numpy",
    # pyaec ships a bundled aec.dll next to its __init__.py; collect_all
    # picks it up so `os.path.join(os.path.dirname(__file__), "aec.dll")`
    # still resolves inside the PyInstaller bundle.
    "pyaec",
    # soundcard is a cffi-backed wrapper around WASAPI — needs its cffi
    # _soundcard.cdef and the cffi runtime collected so the pure-Python
    # calls into the OS audio APIs still work in the frozen bundle.
    "soundcard", "cffi",
    # Diarization
    "torch", "torchaudio", "pyannote", "pyannote.audio",
    "speechbrain", "omegaconf", "lightning_fabric", "pytorch_lightning",
    "transformers", "huggingface_hub", "safetensors", "tokenizers",
    # Server stack
    "uvicorn", "fastapi", "pydantic", "pydantic_core",
    "aiosqlite", "aiofiles", "websockets",
    # LLM
    "openai",
):
    try:
        tmp_datas, tmp_bins, tmp_hidden = collect_all(pkg)
        datas += tmp_datas
        binaries += tmp_bins
        hiddenimports += tmp_hidden
    except Exception as e:  # noqa: BLE001
        print(f"[spec] skipped {pkg}: {e}")

# ── Our package — bundle the prompt .md files explicitly ────────────────────
datas += [("aurascribe/llm/live_intelligence.md", "aurascribe/llm")]
datas += [("aurascribe/llm/daily_brief.md", "aurascribe/llm")]

# ── CUDA 12 DLLs from nvidia-* wheels ───────────────────────────────────────
# CTranslate2 (faster-whisper's backend) is linked against CUDA 12. The
# wheels drop their DLLs under `site-packages/nvidia/*/bin`, which isn't on
# any default search path — we explicitly ship those into the bundle root.
for sp in site.getsitepackages():
    nvidia_root = Path(sp) / "nvidia"
    if not nvidia_root.is_dir():
        continue
    for pkg_dir in nvidia_root.iterdir():
        bin_dir = pkg_dir / "bin"
        if not bin_dir.is_dir():
            continue
        for dll in bin_dir.glob("*.dll"):
            # Second tuple element = destination inside the bundle. Empty
            # string = bundle root, so the DLLs sit next to .exe where the
            # PyInstaller bootloader's DLL search finds them first.
            binaries.append((str(dll), "."))

# ── Uvicorn's reflection-loaded submodules (PyInstaller can't see these) ────
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan", "uvicorn.lifespan.on", "uvicorn.lifespan.off",
]

# ── Our own entry + package ─────────────────────────────────────────────────
hiddenimports += [
    "aurascribe",
    "aurascribe.api",
    "aurascribe.meeting_manager",
    "aurascribe.transcription",
    "aurascribe.transcription.engine",
    "aurascribe.transcription.whisper",
    "aurascribe.audio.capture",
    "aurascribe.db.database",
    "aurascribe.llm.client",
    "aurascribe.llm.analysis",
    "aurascribe.llm.prompts",
    "aurascribe.llm.realtime",
    "aurascribe.llm.daily_brief",
    "aurascribe.llm.sampling",
    "aurascribe.obsidian.writer",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Shrink the bundle by excluding GUI/notebook stacks we don't ship.
    excludes=[
        "matplotlib", "tkinter", "IPython", "jupyter", "notebook",
        "pytest", "pandas.tests", "PyQt5", "PyQt6", "PySide2", "PySide6",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="aurascribe-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX shrinks the binary but often breaks torch / ctranslate2 DLLs.
    upx=False,
    # Keep the console for now — sidecar logs are our only lifeline when
    # something goes wrong in production. Flip to False once logging
    # routes to a file + the pipeline is stable.
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="aurascribe-sidecar",
)
