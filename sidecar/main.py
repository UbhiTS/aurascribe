"""AuraScribe Python sidecar entry point.

Binds to 127.0.0.1 by default — only the Tauri shell reaches it.

CUDA DLL wiring for Windows happens in `aurascribe/__init__.py` so it runs
the moment the package is first imported — before any submodule (in
particular `faster_whisper`) tries to resolve cuBLAS/cuDNN.
"""
from __future__ import annotations

import importlib.util
import logging
import logging.handlers
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

import uvicorn

from aurascribe.api import app
from aurascribe.config import LOGS_DIR


# Optional extras declared in pyproject.toml. Used at boot to log a clear
# warning when a packaged build is missing one — silent ImportErrors at
# the first /api/meetings/start call are much harder to diagnose.
_OPTIONAL_EXTRAS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("asr",         ("faster_whisper", "sounddevice", "scipy", "librosa")),
    ("diarization", ("torch", "torchaudio", "pyannote.audio")),
    ("llm",         ("openai",)),
)


def _check_extras(log: logging.Logger) -> None:
    """Report which optional extras are installed vs missing. Never fails —
    missing extras degrade specific features (ASR / diarization / LLM) but
    the sidecar still serves the API surface that doesn't need them."""
    for extra, mods in _OPTIONAL_EXTRAS:
        missing = [m for m in mods if importlib.util.find_spec(m) is None]
        if missing:
            log.warning(
                "extras: [%s] missing — features will be disabled (missing: %s). "
                "Fix with: pip install -e ./sidecar[%s]",
                extra, ", ".join(missing), extra,
            )
        else:
            log.info("extras: [%s] OK", extra)


def _install_excepthook(log: logging.Logger) -> None:
    """Catch-all for anything that escapes the normal FastAPI request lane.
    Writes a dated crash file next to the rolling log so a crashed sidecar
    still leaves a breadcrumb for the user to share."""
    def handle(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            # Let Ctrl-C exit cleanly without writing a crash file.
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        crash_path = LOGS_DIR / f"crash-{stamp}.log"
        try:
            with open(crash_path, "w", encoding="utf-8") as f:
                f.write(f"AuraScribe sidecar crash @ {datetime.now().isoformat()}\n\n")
                traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
        except Exception:
            pass  # last-ditch; we're already dying, don't hide the original
        log.critical(
            "unhandled exception — crash dumped to %s",
            crash_path,
            exc_info=(exc_type, exc_value, exc_tb),
        )

    sys.excepthook = handle


def _configure_logging() -> logging.Logger:
    """Set up dual-destination logging:

    * Stdout (inherited by Tauri's console) — immediate feedback when
      running under `tauri dev`.
    * Rotating file `APP_DATA/logs/sidecar.log` — survives app exits and
      is the thing to ask users for when something goes wrong in prod.
      Keeps 5× 5MB files before wrapping; plenty for a day or two of
      live diagnostics without growing unbounded.
    """
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Clear anything uvicorn/basicConfig might have left behind so we don't
    # get doubled lines.
    for h in list(root.handlers):
        root.removeHandler(h)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    file_handler = logging.handlers.RotatingFileHandler(
        LOGS_DIR / "sidecar.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    log = logging.getLogger("aurascribe")
    log.setLevel(logging.INFO)
    log.info("logging → stdout + %s", LOGS_DIR / "sidecar.log")
    return log


def main() -> None:
    log = _configure_logging()
    _install_excepthook(log)
    _check_extras(log)

    # One-line startup summary of the ASR device so `tauri dev` output +
    # users' sidecar.log make it obvious what's being used. Helpful when
    # debugging "why is transcription so slow?" — it's usually CPU mode.
    from aurascribe import config as _cfg
    probe = _cfg.HARDWARE_PROBE
    if probe["device"] == "cuda":
        log.info(
            "asr: device=cuda (%s%s) compute=%s model=%s",
            probe.get("device_name") or "unknown",
            f", {probe['vram_gb']} GB" if probe.get("vram_gb") else "",
            _cfg.WHISPER_COMPUTE_TYPE,
            _cfg.WHISPER_MODEL,
        )
    else:
        log.warning(
            "asr: running on CPU (no CUDA GPU detected) — transcription will "
            "be ~5-10\u00d7 slower than on a GPU. Override in Settings if you "
            "have CUDA installed. compute=%s model=%s",
            _cfg.WHISPER_COMPUTE_TYPE, _cfg.WHISPER_MODEL,
        )

    host = os.environ.get("SIDECAR_HOST", "127.0.0.1")
    port = _pick_free_port(log, host, int(os.environ.get("SIDECAR_PORT", "8765")))
    # `log_config=None` stops uvicorn from installing its own logging config
    # (which would clobber our handlers and double-log). Uvicorn's access
    # logger falls back to the root logger instead.
    uvicorn.run(app, host=host, port=port, log_level="info", log_config=None)


def _pick_free_port(log: logging.Logger, host: str, preferred: int) -> int:
    """Return `preferred` if it binds, otherwise scan 10 ports above it.

    Writes the winning port to ``APP_DATA/sidecar-port`` so the Tauri
    shell can read it and point the frontend at the right URL. Previously
    the port was hard-coded to 8765 everywhere; a user with a lingering
    TIME_WAIT socket from a previous run, or another process holding
    8765 (VNC relays, Synergy, AV proxies), hit a 30s hang followed by
    a silent sidecar crash. Now we log the fallback and persist it so
    the UI converges on the live port.
    """
    import socket

    for candidate in range(preferred, preferred + 10):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, candidate))
        except OSError:
            continue
        finally:
            s.close()
        if candidate != preferred:
            log.warning(
                "sidecar: port %d in use — falling back to %d",
                preferred, candidate,
            )
        # Write the chosen port for the Tauri shell to read. Best-effort:
        # a failure here doesn't block startup, just means the shell
        # stays on its default probe sequence.
        try:
            port_file = LOGS_DIR.parent / "sidecar-port"
            port_file.write_text(str(candidate), encoding="utf-8")
        except Exception as e:
            log.warning("sidecar: could not write port file: %s", e)
        return candidate

    raise RuntimeError(
        f"sidecar: no free port in {preferred}-{preferred + 9}. "
        "Another process is holding the range — close other AuraScribe "
        "instances or unusual services and try again."
    )


if __name__ == "__main__":
    main()
