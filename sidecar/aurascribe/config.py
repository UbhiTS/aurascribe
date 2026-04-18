"""Runtime configuration — Windows-native paths, loaded from .env at repo root."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# aurascribe/config.py -> aurascribe/ -> sidecar/ -> <repo root>/.env
_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")


def _expand(p: str | None) -> Path | None:
    if not p:
        return None
    return Path(os.path.expandvars(p)).expanduser()


# ── External services ────────────────────────────────────────────────────────

HF_TOKEN: str | None = os.environ.get("HF_TOKEN")
LM_STUDIO_URL: str = os.environ.get("LM_STUDIO_URL", "http://127.0.0.1:1234/v1")
LM_STUDIO_API_KEY: str = os.environ.get("LM_STUDIO_API_KEY", "lm-studio")
# Which model to ask LM Studio to run for summaries/people-notes. Must be
# loaded (or auto-loadable) in LM Studio. Overridable per-call.
LM_STUDIO_MODEL: str = os.environ.get("LM_STUDIO_MODEL", "local-model")

# Obsidian vault root. None = integration disabled (transcripts still saved to DB).
OBSIDIAN_VAULT: Path | None = _expand(os.environ.get("OBSIDIAN_VAULT"))

# ── App data (durable state) ─────────────────────────────────────────────────

APP_DATA: Path = _expand(os.environ.get("AURASCRIBE_DATA")) or Path(
    os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
) / "AuraScribe"
APP_DATA.mkdir(parents=True, exist_ok=True)

DB_PATH: Path = APP_DATA / "aurascribe.db"
MODELS_DIR: Path = APP_DATA / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

if OBSIDIAN_VAULT:
    VAULT_MEETINGS: Path | None = OBSIDIAN_VAULT / "AuraScribe" / "Meetings"
    VAULT_PEOPLE: Path | None = OBSIDIAN_VAULT / "AuraScribe" / "People"
    VAULT_DAILY: Path | None = OBSIDIAN_VAULT / "AuraScribe" / "Daily"
else:
    VAULT_MEETINGS = VAULT_PEOPLE = VAULT_DAILY = None

# ── ASR (faster-whisper) — Phase 3 consumes these ────────────────────────────

WHISPER_MODEL: str = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
WHISPER_DEVICE: str = os.environ.get("WHISPER_DEVICE", "cuda")
WHISPER_COMPUTE_TYPE: str = os.environ.get("WHISPER_COMPUTE_TYPE", "float16")
WHISPER_LANGUAGE: str = os.environ.get("WHISPER_LANGUAGE", "en")

# ── Diarization (pyannote) — Phase 4 consumes these ──────────────────────────

DIARIZATION_MODEL: str = os.environ.get(
    "DIARIZATION_MODEL", "pyannote/speaker-diarization-3.1"
)
MY_SPEAKER_LABEL: str = os.environ.get("MY_SPEAKER_LABEL", "Me")

# ── Audio pipeline ───────────────────────────────────────────────────────────

SAMPLE_RATE: int = 16_000
CHANNELS: int = 1
CHUNK_DURATION: float = 30.0      # seconds per transcription chunk
SILENCE_DURATION: float = 1.0     # seconds of silence before ending an utterance
VAD_THRESHOLD: float = 0.5
