"""Runtime configuration — Windows-native paths.

All user settings live in `APP_DATA/config.json`. `.env` is not consulted:
if a value isn't in config.json, it falls back to the built-in default.
The Settings UI is the only supported way to change these.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _expand(p: str | None) -> Path | None:
    if not p:
        return None
    return Path(os.path.expandvars(p)).expanduser()


# ── App data (durable state) ─────────────────────────────────────────────────
#
# Everything AuraScribe owns — SQLite DB, per-meeting Opus recordings,
# cached Whisper model files, user settings — lives under a single root:
# APP_DATA. Keeping the entire state tree under one directory means the
# user can move to a new machine or reinstall the app just by copying that
# folder (e.g. E:\AuraScribe) and pointing the fresh install at it.
#
# APP_DATA is `data_dir` in bootstrap.json (what the Settings UI writes),
# or the default `%APPDATA%\AuraScribe` on a fresh install.
#
# The bootstrap file itself lives at a FIXED OS location — it can't move
# with APP_DATA because we need to read it *before* we know where APP_DATA
# is. After that, no other state touches this location; everything else
# follows APP_DATA.
#
# Note: `%APPDATA%` below reads the Windows system env var that points at
# the per-user roaming folder — not a user-settable override. It's how
# Windows tells us where to put user data.

DEFAULT_APP_DATA: Path = Path(
    os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
) / "AuraScribe"

# Bootstrap pointer — anchors the state tree. Never moves.
BOOTSTRAP_FILE: Path = DEFAULT_APP_DATA / "bootstrap.json"


def _read_bootstrap() -> dict:
    if not BOOTSTRAP_FILE.exists():
        return {}
    try:
        data = json.loads(BOOTSTRAP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_bootstrap_data_dir() -> str | None:
    """The `data_dir` the Settings UI has persisted, or None."""
    v = _read_bootstrap().get("data_dir")
    return v if isinstance(v, str) and v else None


def save_bootstrap_data_dir(value: str | None) -> None:
    """Write (or clear, when value is None/empty) the data_dir override.
    The file stays at BOOTSTRAP_FILE; only its contents change. Callers
    must restart the app for the new location to take effect — the
    module-level APP_DATA/DB_PATH/etc. are frozen at import time."""
    current = _read_bootstrap()
    if value in (None, ""):
        current.pop("data_dir", None)
    else:
        current["data_dir"] = value
    BOOTSTRAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    BOOTSTRAP_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")


APP_DATA: Path = _expand(load_bootstrap_data_dir()) or DEFAULT_APP_DATA
APP_DATA.mkdir(parents=True, exist_ok=True)

DB_PATH: Path = APP_DATA / "aurascribe.db"
MODELS_DIR: Path = APP_DATA / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)
# Per-meeting raw-audio recordings (OGG Opus, 24 kbps mono). One file per
# meeting, named <meeting_id>.opus. Deleted alongside the meeting row.
AUDIO_DIR: Path = APP_DATA / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
# Logs + crash dumps. Survives uninstall (it's under APP_DATA, which the
# user owns), which is exactly what we want — if the sidecar dies on
# startup the log is still there for diagnosis.
LOGS_DIR: Path = APP_DATA / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# User-editable LLM prompt templates. Seeded from the package-bundled copies
# on first run (or whenever the user deletes a file — the default returns).
# Existing files are NEVER overwritten, so edits are sticky.
PROMPTS_DIR: Path = APP_DATA / "prompts"
PROMPTS_DIR.mkdir(parents=True, exist_ok=True)

_BUNDLED_PROMPTS_DIR: Path = Path(__file__).resolve().parent / "llm"
# Known prompts we own. New prompts can be dropped in PROMPTS_DIR at any
# time — nothing seeds or gates them, they just need to live there.
_SEEDED_PROMPTS: tuple[str, ...] = ("live_intelligence.md", "daily_brief.md")

for _name in _SEEDED_PROMPTS:
    _target = PROMPTS_DIR / _name
    if _target.exists():
        continue
    _src = _BUNDLED_PROMPTS_DIR / _name
    if not _src.is_file():
        continue
    try:
        _target.write_text(_src.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        # Seeding is best-effort — if it fails (permissions, odd FS), the
        # runtime read paths fall back to the bundled copy anyway.
        pass

# ── User config (editable via Settings UI) ───────────────────────────────────
#
# All user-tunable knobs — LLM endpoint, Whisper model, Obsidian vault,
# realtime-intel cadence — persist in config.json inside APP_DATA. Moving the
# data dir to a new machine carries these over automatically.
#
# Resolution: config.json → built-in default. Nothing else. No env-var
# fallback; if it's not in config.json, it's the default.
#
# Keys that aren't in _CONFIG_KEYS are ignored (defensive — older/newer
# config.json files from a different install won't crash us).

CONFIG_FILE: Path = APP_DATA / "config.json"

_CONFIG_KEYS = {
    "hf_token",
    "my_speaker_label",
    "llm_base_url",
    "llm_api_key",
    "llm_model",
    "llm_context_tokens",
    "whisper_model",
    "whisper_language",
    "obsidian_vault",
    "rt_highlights_debounce_sec",
    "rt_highlights_max_interval_sec",
    "rt_highlights_window_sec",
}

# One-shot rename of the old LM-Studio-specific keys to provider-agnostic
# names. Runs every load; once the file is rewritten, the legacy keys are
# gone and this is a no-op on subsequent loads.
_LEGACY_KEY_MAP = {
    "lm_studio_url":            "llm_base_url",
    "lm_studio_api_key":        "llm_api_key",
    "lm_studio_model":          "llm_model",
    "lm_studio_context_tokens": "llm_context_tokens",
}


def _migrate_legacy_keys(data: dict) -> tuple[dict, bool]:
    changed = False
    out = dict(data)
    for old, new in _LEGACY_KEY_MAP.items():
        if old not in out:
            continue
        if new not in out:
            out[new] = out[old]
        out.pop(old, None)
        changed = True
    return out, changed


def load_user_config() -> dict:
    """Read config.json from APP_DATA. Missing/corrupt file = empty dict —
    startup must never fail on a bad config file."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    migrated, changed = _migrate_legacy_keys(data)
    if changed:
        try:
            CONFIG_FILE.write_text(json.dumps(migrated, indent=2), encoding="utf-8")
        except Exception:
            # Can't rewrite the file — in-memory migration still wins so the
            # running process uses the new keys. Next successful write sticks.
            pass
    return {k: v for k, v in migrated.items() if k in _CONFIG_KEYS}


def save_user_config(updates: dict) -> dict:
    """Merge `updates` into config.json. Keys with None/"" values are
    removed (fall back to default on next restart). Unknown keys are
    silently dropped. Returns the full persisted dict."""
    current: dict = {}
    if CONFIG_FILE.exists():
        try:
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except Exception:
            current = {}
    for k, v in updates.items():
        if k not in _CONFIG_KEYS:
            continue
        if v is None or (isinstance(v, str) and v == ""):
            current.pop(k, None)
        else:
            current[k] = v
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


_user_config: dict = load_user_config()


def _cfg_str(cfg_key: str, default: str) -> str:
    """config.json value, or `default`."""
    v = _user_config.get(cfg_key)
    return v if isinstance(v, str) and v else default


def _cfg_optional_str(cfg_key: str) -> str | None:
    """config.json value, or None — for fields where absent is meaningful
    (HF_TOKEN, OBSIDIAN_VAULT)."""
    v = _user_config.get(cfg_key)
    return v if isinstance(v, str) and v else None


def _cfg_int(cfg_key: str, default: int) -> int:
    v = _user_config.get(cfg_key)
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            pass
    return default


def _cfg_float(cfg_key: str, default: float) -> float:
    v = _user_config.get(cfg_key)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            pass
    return default


# External services --
HF_TOKEN: str | None = _cfg_optional_str("hf_token")
# OpenAI-compatible LLM endpoint. Works with LM Studio, Ollama's OpenAI
# shim, OpenAI, OpenRouter, Gemini's OpenAI-compat endpoint, Anthropic via
# a compatible proxy — anything that speaks /v1/chat/completions.
LLM_BASE_URL: str = _cfg_str("llm_base_url", "http://127.0.0.1:1234/v1")
LLM_API_KEY: str = _cfg_str("llm_api_key", "lm-studio")
# Model id sent in every chat-completions call. Must be what the provider
# expects (e.g. "gpt-4o", "gemini-2.0-flash", or the id your local server
# reports). Overridable per-call.
LLM_MODEL: str = _cfg_str("llm_model", "local-model")
# Total context window (in tokens) of the configured model. Drives the
# input-size budgeting for long-context calls like the Daily Brief. Bump
# this when you wire up a long-context model (e.g. a 200k+ variant);
# shrink it for smaller models.
LLM_CONTEXT_TOKENS: int = _cfg_int("llm_context_tokens", 4096)

# Obsidian vault root. None = integration disabled (transcripts still saved to DB).
OBSIDIAN_VAULT: Path | None = _expand(_cfg_optional_str("obsidian_vault"))

if OBSIDIAN_VAULT:
    VAULT_MEETINGS: Path | None = OBSIDIAN_VAULT / "AuraScribe" / "Meetings"
    VAULT_PEOPLE: Path | None = OBSIDIAN_VAULT / "AuraScribe" / "People"
    VAULT_DAILY: Path | None = OBSIDIAN_VAULT / "AuraScribe" / "Daily"
else:
    VAULT_MEETINGS = VAULT_PEOPLE = VAULT_DAILY = None

# ── ASR (faster-whisper) — Phase 3 consumes these ────────────────────────────

WHISPER_MODEL: str = _cfg_str("whisper_model", "large-v3-turbo")
# Device / compute-type are deliberately fixed — swapping them at runtime
# can brick the pipeline (wrong CUDA/CPU path, unsupported compute type).
# Edit these constants in-source if you need to retune.
WHISPER_DEVICE: str = "cuda"
WHISPER_COMPUTE_TYPE: str = "float16"
WHISPER_LANGUAGE: str = _cfg_str("whisper_language", "en")

# ── Diarization (pyannote) — Phase 4 consumes these ──────────────────────────

# Pipeline choice is fixed — swapping diarization pipelines is an expert-mode
# change that requires matching pyannote version + auth. Edit in-source.
DIARIZATION_MODEL: str = "pyannote/speaker-diarization-3.1"
MY_SPEAKER_LABEL: str = _cfg_str("my_speaker_label", "Me")

# ── Audio pipeline ───────────────────────────────────────────────────────────

SAMPLE_RATE: int = 16_000
CHANNELS: int = 1
CHUNK_DURATION: float = 10.0      # seconds per transcription chunk
SILENCE_DURATION: float = 0.6     # seconds of silence before ending an utterance
VAD_THRESHOLD: float = 0.5

# ── Realtime intelligence (live highlights / action items / talking points) ──

# Debounce: fire this many seconds after the last new utterance lands. Shorter
# = snappier panel updates, more LLM load. Local LMStudio handles ~1 call/15s
# comfortably with a 7-8B model.
RT_HIGHLIGHTS_DEBOUNCE_SEC: float = _cfg_float("rt_highlights_debounce_sec", 20.0)
# Hard cap: even during nonstop speech, never wait longer than this between
# refreshes. Keeps the support-intelligence card feeling alive.
RT_HIGHLIGHTS_MAX_INTERVAL_SEC: float = _cfg_float("rt_highlights_max_interval_sec", 60.0)
# Recent transcript window the LLM sees. Older context lives in the
# already-extracted highlights/action items, which we also send back.
RT_HIGHLIGHTS_WINDOW_SEC: float = _cfg_float("rt_highlights_window_sec", 180.0)
