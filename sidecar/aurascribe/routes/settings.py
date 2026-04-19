"""Settings endpoints — data-dir pointer and user config (LLM / ASR /
Obsidian / realtime-intel cadence). Both sets require a sidecar restart
to fully take effect, since module-level paths freeze at import time."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aurascribe import config

router = APIRouter(prefix="/api/settings")


# ── Data directory ──────────────────────────────────────────────────────────


class DataDirSettings(BaseModel):
    # Where AuraScribe stores ALL durable state (DB, audio, model cache,
    # config.json). Absolute path; `~` and env vars are expanded. An
    # explicit `""` or null clears the override → next startup falls back
    # to the default (%APPDATA%\AuraScribe).
    data_dir: str | None = None


def _data_dir_response() -> dict:
    """Snapshot of the current state-directory resolution. `effective` is
    what this running process picked up at import time; `override` is the
    bootstrap value the UI has persisted for next startup."""
    return {
        "effective": str(config.APP_DATA),
        "override": config.load_bootstrap_data_dir(),
        "default": str(config.DEFAULT_APP_DATA),
        "bootstrap_file": str(config.BOOTSTRAP_FILE),
    }


@router.get("/data-dir")
async def get_settings_data_dir() -> dict:
    return _data_dir_response()


@router.put("/data-dir")
async def put_settings_data_dir(req: DataDirSettings) -> dict:
    provided = req.model_fields_set if hasattr(req, "model_fields_set") else req.__fields_set__
    if "data_dir" not in provided:
        raise HTTPException(400, "data_dir is required")
    raw = req.data_dir
    if raw in (None, ""):
        config.save_bootstrap_data_dir(None)
    else:
        expanded = Path(os.path.expandvars(raw)).expanduser()
        if not expanded.is_absolute():
            raise HTTPException(400, "data_dir must be an absolute path")
        try:
            expanded.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(400, f"Cannot use data_dir {expanded}: {e}")
        config.save_bootstrap_data_dir(str(expanded))
    resp = _data_dir_response()
    # Saving doesn't hot-swap — all module-level paths were frozen at import.
    # A restart is needed iff what we just wrote differs from what's in use.
    resp["requires_restart"] = (
        resp["override"] is not None and resp["override"] != resp["effective"]
    )
    return resp


# ── User config (LLM / ASR / Obsidian / realtime) ───────────────────────────
#
# Declarative spec for every editable config field. Ordering here is purely
# for developer convenience — the UI picks its own groupings via the key
# names. Each tuple is (key, default_for_display).
# Defaults shown under each field in the Settings UI. `None` means "no
# preset default — fall back to auto-detect". The ASR trio (`whisper_model`,
# `whisper_device`, `whisper_compute_type`) compute their defaults from the
# hardware probe so the placeholder reflects what'd run if the user clears
# their override. See `_probe_hardware` in aurascribe.config.
_CONFIG_FIELDS: list[tuple[str, object]] = [
    ("hf_token",                       None),
    ("my_speaker_label",               "Me"),
    ("llm_base_url",                   "http://127.0.0.1:1234/v1"),
    ("llm_api_key",                    "lm-studio"),
    ("llm_model",                      "local-model"),
    ("llm_context_tokens",             4096),
    ("whisper_model",                  config._default_whisper_model()),
    ("whisper_device",                 config._default_whisper_device()),
    ("whisper_compute_type",           config._default_whisper_compute_type()),
    ("whisper_language",               "en"),
    ("obsidian_vault",                 None),
    ("rt_highlights_debounce_sec",     20.0),
    ("rt_highlights_max_interval_sec", 60.0),
    ("rt_highlights_window_sec",       180.0),
    # Advanced — chunking + VAD
    ("chunk_duration",                 10.0),
    ("silence_duration",               0.6),
    ("vad_threshold",                  0.5),
    # Advanced — echo cancellation
    ("aec_tail_ms",                    200),
    # Advanced — speaker identification
    ("voice_match_threshold_multi",    0.55),
    ("voice_match_threshold_solo",     0.70),
    ("voice_ratio_margin",             0.80),
    ("min_voice_samples",              3),
    ("provisional_threshold",          0.50),
    # Advanced — live partials
    ("speculative_interval_sec",       1.5),
    ("speculative_window_sec",         30.0),
    # Advanced — Obsidian write cadence
    ("obsidian_write_interval_sec",    15.0),
    ("obsidian_write_chunks",          5),
    # Advanced — Daily Brief
    ("daily_brief_auto_refresh",       False),
]


def _effective_for(key: str) -> object:
    """Resolve the actual in-process value of a config key by reading from
    the `config` module. Kept in a dict for O(1) lookup; centralized here
    so the field list and the effective readers stay in sync."""
    readers: dict = {
        "hf_token":                       config.HF_TOKEN,
        "my_speaker_label":               config.MY_SPEAKER_LABEL,
        "llm_base_url":                   config.LLM_BASE_URL,
        "llm_api_key":                    config.LLM_API_KEY,
        "llm_model":                      config.LLM_MODEL,
        "llm_context_tokens":             config.LLM_CONTEXT_TOKENS,
        "whisper_model":                  config.WHISPER_MODEL,
        "whisper_device":                 config.WHISPER_DEVICE,
        "whisper_compute_type":           config.WHISPER_COMPUTE_TYPE,
        "whisper_language":               config.WHISPER_LANGUAGE,
        "obsidian_vault":                 str(config.OBSIDIAN_VAULT) if config.OBSIDIAN_VAULT else None,
        "rt_highlights_debounce_sec":     config.RT_HIGHLIGHTS_DEBOUNCE_SEC,
        "rt_highlights_max_interval_sec": config.RT_HIGHLIGHTS_MAX_INTERVAL_SEC,
        "rt_highlights_window_sec":       config.RT_HIGHLIGHTS_WINDOW_SEC,
        "chunk_duration":                 config.CHUNK_DURATION,
        "silence_duration":               config.SILENCE_DURATION,
        "vad_threshold":                  config.VAD_THRESHOLD,
        "aec_tail_ms":                    config.AEC_TAIL_MS,
        "voice_match_threshold_multi":    config.VOICE_MATCH_THRESHOLD_MULTI,
        "voice_match_threshold_solo":     config.VOICE_MATCH_THRESHOLD_SOLO,
        "voice_ratio_margin":             config.VOICE_RATIO_MARGIN,
        "min_voice_samples":              config.MIN_VOICE_SAMPLES,
        "provisional_threshold":          config.PROVISIONAL_THRESHOLD,
        "speculative_interval_sec":       config.SPECULATIVE_INTERVAL_SEC,
        "speculative_window_sec":         config.SPECULATIVE_WINDOW_SEC,
        "obsidian_write_interval_sec":    config.OBSIDIAN_WRITE_INTERVAL_SEC,
        "obsidian_write_chunks":          config.OBSIDIAN_WRITE_CHUNKS,
        "daily_brief_auto_refresh":       config.DAILY_BRIEF_AUTO_REFRESH,
    }
    return readers[key]


class UserConfigUpdate(BaseModel):
    # Every field is optional so clients can PATCH-style send only what
    # they're changing. None or "" clears the override → default on restart.
    hf_token: str | None = None
    my_speaker_label: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_context_tokens: int | None = None
    whisper_model: str | None = None
    whisper_device: str | None = None
    whisper_compute_type: str | None = None
    whisper_language: str | None = None
    obsidian_vault: str | None = None
    rt_highlights_debounce_sec: float | None = None
    rt_highlights_max_interval_sec: float | None = None
    rt_highlights_window_sec: float | None = None
    # Advanced knobs.
    chunk_duration: float | None = None
    silence_duration: float | None = None
    vad_threshold: float | None = None
    aec_tail_ms: int | None = None
    voice_match_threshold_multi: float | None = None
    voice_match_threshold_solo: float | None = None
    voice_ratio_margin: float | None = None
    min_voice_samples: int | None = None
    provisional_threshold: float | None = None
    speculative_interval_sec: float | None = None
    speculative_window_sec: float | None = None
    obsidian_write_interval_sec: float | None = None
    obsidian_write_chunks: int | None = None
    daily_brief_auto_refresh: bool | None = None


def _config_response() -> dict:
    """Per-field snapshot: what's in use (effective, frozen at import), what
    the UI has saved (override), and the built-in default."""
    stored = config.load_user_config()
    settings: dict = {}
    for key, default in _CONFIG_FIELDS:
        settings[key] = {
            "effective": _effective_for(key),
            "override": stored.get(key),
            "default": default,
        }
    return {"settings": settings, "config_file": str(config.CONFIG_FILE)}


@router.get("/config")
async def get_settings_config() -> dict:
    return _config_response()


@router.put("/config")
async def put_settings_config(req: UserConfigUpdate) -> dict:
    provided = req.model_fields_set if hasattr(req, "model_fields_set") else req.__fields_set__
    updates: dict = {}
    for key in provided:
        val = getattr(req, key)
        # Path-shaped fields get absolute-path validation so we don't
        # silently accept relative paths that'd resolve unpredictably.
        if key == "obsidian_vault" and isinstance(val, str) and val:
            expanded = Path(os.path.expandvars(val)).expanduser()
            if not expanded.is_absolute():
                raise HTTPException(400, "obsidian_vault must be an absolute path")
            updates[key] = str(expanded)
        else:
            updates[key] = val
    config.save_user_config(updates)
    resp = _config_response()
    # Any field whose persisted value differs from what's live requires a
    # restart. Clearing an override (override=None) also counts if the
    # live value differs from the built-in default.
    resp["requires_restart"] = any(
        (f.get("override") if f.get("override") is not None else f.get("default"))
        != f.get("effective")
        for f in resp["settings"].values()
    )
    return resp
