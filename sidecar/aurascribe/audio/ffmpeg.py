"""Thin async ffmpeg wrapper for the import-audio path.

The live-record path writes opus directly via libsndfile; only imports go
through here. Resolution order for the binary:

1. ``imageio_ffmpeg.get_ffmpeg_exe()`` — a static ffmpeg shipped inside
   the ``imageio-ffmpeg`` Python wheel. PyInstaller's ``collect_all``
   includes it in the frozen sidecar, so packaged builds always have a
   working ffmpeg with no system install required.
2. ``ffmpeg`` on ``$PATH`` — lets a user override the bundled binary
   with their own (e.g. an ffmpeg with a particular codec licence) and
   keeps dev installs working when ``imageio-ffmpeg`` isn't installed
   in the venv yet.

Encoding params mirror what the live record loop produces: 16 kHz mono
Opus at 24 kbps. Keeping the format identical means downstream tooling
(soundfile decode, pyannote, the audio download endpoint) doesn't need
import-vs-record branches.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

# Match the live record path: 24 kbps mono Opus @ 16 kHz. Anything higher
# is wasted on speech, anything lower starts to hurt whisper accuracy.
_OPUS_BITRATE = "24k"
_OPUS_SAMPLE_RATE = "16000"


def _resolve_ffmpeg() -> str | None:
    """Return the path to a usable ffmpeg binary, or None if neither the
    bundled imageio-ffmpeg nor a system install is available."""
    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe:
            return str(exe)
    except Exception as e:
        # imageio-ffmpeg not installed (dev venv without `[asr]` extras)
        # or the bundled binary couldn't be located. Fall through to PATH.
        log.debug("imageio_ffmpeg not usable, falling back to PATH: %s", e)
    return shutil.which("ffmpeg")


def ffmpeg_available() -> bool:
    """True when a usable ffmpeg binary can be found (bundled or system)."""
    return _resolve_ffmpeg() is not None


class FFmpegMissingError(RuntimeError):
    """Raised by `transcode_to_opus` when no ffmpeg binary is on PATH.
    Surfaces as a 503 from the import endpoint with install instructions."""


class FFmpegFailedError(RuntimeError):
    """Raised when ffmpeg runs but the conversion fails — typically because
    the input file is corrupt, an unsupported codec, or a permissions issue
    on the output path. Carries ffmpeg's stderr tail for diagnosis."""

    def __init__(self, message: str, stderr_tail: str = "") -> None:
        super().__init__(message)
        self.stderr_tail = stderr_tail


async def transcode_to_opus(src: Path, dst: Path) -> None:
    """Re-encode any audio/video file to 16 kHz mono Opus at the canonical
    bitrate. Overwrites `dst`. Raises FFmpegMissingError when neither the
    bundled binary nor a system install can be found, and FFmpegFailedError
    on any non-zero exit code (with the last few lines of stderr included
    so the route can surface a useful error to the user)."""
    exe = _resolve_ffmpeg()
    if exe is None:
        raise FFmpegMissingError(
            "ffmpeg binary not found. The packaged build ships one via "
            "imageio-ffmpeg; if you're on a dev install run "
            "`pip install -e ./sidecar[asr]` to pull it in, or install "
            "ffmpeg system-wide (https://ffmpeg.org/download.html)."
        )

    # `-y` overwrites the output without prompting. `-vn` drops any video
    # stream (mp4 podcasts, screen recordings). `-ac 1` downmixes to mono;
    # `-ar` resamples to 16 kHz; `-c:a libopus -b:a 24k` is the actual
    # encode. `-loglevel error` keeps stderr quiet on success so the
    # tail-on-failure stays focused.
    proc = await asyncio.create_subprocess_exec(
        exe,
        "-loglevel", "error",
        "-y",
        "-i", str(src),
        "-vn",
        "-ac", "1",
        "-ar", _OPUS_SAMPLE_RATE,
        "-c:a", "libopus",
        "-b:a", _OPUS_BITRATE,
        str(dst),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", errors="replace").strip()
        # Keep the last ~600 chars — enough to see the actual error line
        # without flooding logs / response payloads.
        if len(tail) > 600:
            tail = "…" + tail[-600:]
        log.warning("ffmpeg failed (rc=%s) on %s: %s", proc.returncode, src, tail)
        raise FFmpegFailedError(
            f"ffmpeg failed to convert {src.name} to opus", tail,
        )
