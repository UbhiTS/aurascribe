"""Runtime configuration — cross-platform paths.

All user settings live in `APP_DATA/config.json`. `.env` is not consulted:
if a value isn't in config.json, it falls back to the built-in default.
The Settings UI is the only supported way to change these.
"""
from __future__ import annotations

import json
import os
import sys
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
# Platform-appropriate user data directory:
#   Windows  → %APPDATA%\AuraScribe          (e.g. C:\Users\<user>\AppData\Roaming\AuraScribe)
#   macOS    → ~/Library/Application Support/AuraScribe
#   Linux    → $XDG_DATA_HOME/AuraScribe     (falls back to ~/.local/share/AuraScribe)

if sys.platform == "win32":
    DEFAULT_APP_DATA: Path = (
        Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        / "AuraScribe"
    )
elif sys.platform == "darwin":
    DEFAULT_APP_DATA = Path.home() / "Library" / "Application Support" / "AuraScribe"
else:
    DEFAULT_APP_DATA = (
        Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
        / "AuraScribe"
    )

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


def _resolve_app_data() -> Path:
    """Pick APP_DATA: the user's bootstrap override if it's usable,
    otherwise DEFAULT_APP_DATA. Falls back to a temp dir as a last
    resort if both are inaccessible (e.g. bootstrap points at an
    offline network share AND %APPDATA% is locked down — rare but
    fatal on startup if we don't recover).

    Writes a loud stderr breadcrumb on fallback; the sidecar's log
    handler isn't set up yet at import time so we can't use logging.
    """
    import sys as _sys
    import tempfile as _tempfile

    candidates: list[Path] = []
    override = _expand(load_bootstrap_data_dir())
    if override is not None:
        candidates.append(override)
    candidates.append(DEFAULT_APP_DATA)
    # Last-resort: a temp dir. If this fails too, we let the exception
    # propagate — there's genuinely nowhere to write.
    candidates.append(Path(_tempfile.gettempdir()) / "AuraScribe-fallback")

    for i, path in enumerate(candidates):
        try:
            path.mkdir(parents=True, exist_ok=True)
            # Probe write access — mkdir can succeed on a read-only
            # network mount but a subsequent file open fails. Better
            # to discover this now.
            _probe = path / ".write_probe"
            _probe.write_text("", encoding="utf-8")
            _probe.unlink()
            if i > 0:
                print(
                    f"[AuraScribe] data dir {candidates[i - 1]!s} is unusable; "
                    f"falling back to {path!s}. Check your bootstrap.json / "
                    "disk permissions / network connectivity.",
                    file=_sys.stderr,
                )
            return path
        except Exception as e:
            print(
                f"[AuraScribe] data dir {path!s} is unusable ({e!r}); "
                "trying next fallback.",
                file=_sys.stderr,
            )
            continue
    # All candidates failed — surface the original exception by retrying
    # the default, which will raise and leave a real traceback in stderr.
    DEFAULT_APP_DATA.mkdir(parents=True, exist_ok=True)
    return DEFAULT_APP_DATA


APP_DATA: Path = _resolve_app_data()


def _ensure_dir(path: Path, label: str) -> Path:
    """mkdir with a stderr breadcrumb on failure. Never raises — callers
    get a best-effort path and downstream code handles the miss."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        import sys as _sys
        print(
            f"[AuraScribe] could not create {label} dir {path!s}: {e!r}. "
            "Feature will be disabled until the directory is writable.",
            file=_sys.stderr,
        )
    return path


DB_PATH: Path = APP_DATA / "aurascribe.db"
MODELS_DIR: Path = _ensure_dir(APP_DATA / "models", "models")
# Per-meeting raw-audio recordings (OGG Opus, 24 kbps mono). One file per
# meeting, named <meeting_id>.opus. Deleted alongside the meeting row.
AUDIO_DIR: Path = _ensure_dir(APP_DATA / "audio", "audio")
# Logs + crash dumps. Survives uninstall (it's under APP_DATA, which the
# user owns), which is exactly what we want — if the sidecar dies on
# startup the log is still there for diagnosis.
LOGS_DIR: Path = _ensure_dir(APP_DATA / "logs", "logs")
# Per-voice avatar images. File name is `<voice_id>.<ext>` where `ext` is
# mirrored on voices.avatar_ext so we can serve the right MIME without
# re-probing the file. Images are scrubbed from disk on voice delete.
AVATARS_DIR: Path = _ensure_dir(APP_DATA / "avatars", "avatars")

# User-editable LLM prompt templates. Seeded from the package-bundled copies
# on first run (or whenever the user deletes a file — the default returns).
# Existing files are NEVER overwritten, so edits are sticky.
PROMPTS_DIR: Path = _ensure_dir(APP_DATA / "prompts", "prompts")

_BUNDLED_PROMPTS_DIR: Path = Path(__file__).resolve().parent / "llm"
# Known prompts we own. New prompts can be dropped in PROMPTS_DIR at any
# time — nothing seeds or gates them, they just need to live there.
_SEEDED_PROMPTS: tuple[str, ...] = (
    "live_intelligence.md",
    "daily_brief.md",
    "meeting_analysis.md",
)

# One-shot cleanup of prompt files we used to ship but no longer support.
# Live title refinement was folded into live_intelligence.md (one LLM call
# now returns highlights + entity + topic), so the standalone prompt is
# dead weight that would otherwise confuse a user editing it expecting
# it to do something. `meeting_bucket.md` drove the customer-isolated
# vault layout; the generic layout doesn't need bucket inference.
_RETIRED_PROMPTS: tuple[str, ...] = (
    "meeting_title_refinement.md",
    "meeting_bucket.md",
)

# Prompts whose APP_DATA copy gets nuked AND re-seeded from the bundled
# default on boot — used when we shipped a structural change to the
# prompt's input/output contract that the user's previous edits could
# never satisfy. Once the contract stabilises post-GA, this list goes
# back to empty (and prompt edits become sticky again).
_FORCE_RESEED_PROMPTS: tuple[str, ...] = (
    # live_intelligence now also returns `entity` + `topic` in JSON for
    # the merged title-refinement path. Old user copies don't ask for
    # those fields so the response would lack them.
    "live_intelligence.md",
)

for _name in _RETIRED_PROMPTS:
    _stale = PROMPTS_DIR / _name
    if _stale.exists():
        try:
            _stale.unlink()
        except Exception:
            pass

for _name in _FORCE_RESEED_PROMPTS:
    _stale = PROMPTS_DIR / _name
    if _stale.exists():
        try:
            _stale.unlink()
        except Exception:
            pass

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
    "whisper_device",
    "whisper_compute_type",
    "whisper_language",
    "obsidian_vault",
    "rt_highlights_debounce_sec",
    "rt_highlights_max_interval_sec",
    "rt_highlights_window_sec",
    # Advanced knobs — see the "Advanced Settings" section in the UI.
    "chunk_duration",
    "silence_duration",
    "vad_threshold",
    "aec_tail_ms",
    "voice_match_threshold_multi",
    "voice_match_threshold_solo",
    "voice_ratio_margin",
    "min_voice_samples",
    "provisional_threshold",
    "speculative_interval_sec",
    "speculative_window_sec",
    "obsidian_write_interval_sec",
    "obsidian_write_chunks",
    "daily_brief_auto_refresh",
    # Auto-capture — sustained-speech-based auto-start/stop.
    "auto_capture_enabled",
    "auto_capture_start_speech_sec",
    "auto_capture_stop_silence_sec",
    "auto_capture_vad_threshold",
    "auto_capture_countdown_after_silence_sec",
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


def _cfg_bool(cfg_key: str, default: bool) -> bool:
    """config.json value as a boolean. Accepts real JSON bools and common
    string forms ('true', '1', 'yes', 'on') so the Settings UI can PATCH
    either shape without the server caring."""
    v = _user_config.get(cfg_key)
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "on")
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

# Generic vault layout — three top-level folders, no taxonomy beyond date +
# person. Users add their own tags / folders for customers / projects /
# teams in Obsidian if they want; AuraScribe doesn't impose a hierarchy.
#
#   Meetings/YYYY/YYYY-MM-DD/<HH-MM> - <title>.md
#   People/<Display Name>.md         (voice_id in frontmatter is the real key)
#   Daily/YYYY-MM-DD.md              (generated daily briefs, flat by date)
if OBSIDIAN_VAULT:
    VAULT_MEETINGS: Path | None = OBSIDIAN_VAULT / "Meetings"
    VAULT_PEOPLE: Path | None = OBSIDIAN_VAULT / "People"
    VAULT_DAILY: Path | None = OBSIDIAN_VAULT / "Daily"
else:
    VAULT_MEETINGS = VAULT_PEOPLE = VAULT_DAILY = None

# ── ASR (faster-whisper) — device-adaptive defaults ──────────────────────────
#
# We probe the machine once at import to pick a safe set of defaults:
#   * No CUDA GPU              → cpu + int8 + small model (fits on any laptop)
#   * CUDA GPU with ≥8 GB VRAM → cuda + float16 + large-v3-turbo (fastest)
#   * CUDA GPU with 4-8 GB     → cuda + int8_float16 + large-v3-turbo
#   * CUDA GPU with <4 GB      → cuda + int8 + medium
#
# All three values are user-overridable via config.json / Settings. We keep
# the probe result around as module-level state so Settings can surface
# "Detected: RTX 4090, 24 GB" without re-probing.


def _probe_hardware() -> dict:
    """One-shot probe of the local accelerator, run at config import.

    We deliberately ask ctranslate2 first (faster-whisper's backend) —
    it has its own CUDA bindings via the `nvidia-*-cu12` wheels and is
    the actual thing that benefits from a GPU. PyTorch may be a
    CPU-only wheel (our CPU-variant build ships `torch+cpu`) while
    ctranslate2 can still run Whisper on CUDA fine.

    Torch is a secondary probe for device-name / VRAM metadata. If torch
    can't see the GPU (CPU-only wheel), we still return `device=cuda`
    with no display name — enough for the auto-detect to pick the right
    defaults, and the Settings UI handles the null name gracefully.

    Never raises — a failure at any layer just falls through to the
    next / stays on CPU defaults.
    """
    info: dict = {"device": "cpu", "device_name": None, "vram_gb": None}

    # Primary: ctranslate2 — authoritative for whisper performance.
    try:
        import ctranslate2  # type: ignore
        if ctranslate2.get_cuda_device_count() > 0:
            info["device"] = "cuda"
    except Exception:
        pass

    # Secondary: torch — for device name + VRAM, and as a fallback
    # detection path in case ctranslate2 isn't installed yet.
    try:
        import torch  # type: ignore
        torch_has_cuda = torch.cuda.is_available()
        if torch_has_cuda:
            info["device"] = "cuda"
        if info["device"] == "cuda" and torch_has_cuda:
            try:
                info["device_name"] = str(torch.cuda.get_device_name(0))
            except Exception:
                pass
            try:
                props = torch.cuda.get_device_properties(0)
                info["vram_gb"] = round(props.total_memory / (1024**3), 1)
            except Exception:
                pass
        # Apple Silicon MPS — only probe when no CUDA was found (mutually
        # exclusive on the same machine). MPS uses unified memory so there's
        # no discrete VRAM figure; we leave vram_gb as None.
        if info["device"] == "cpu":
            try:
                if torch.backends.mps.is_available():
                    info["device"] = "mps"
                    info["device_name"] = "Apple Silicon GPU"
            except Exception:
                pass
    except Exception:
        pass

    return info


def _probe_hardware_with_timeout(timeout_sec: float = 5.0) -> dict:
    """Run `_probe_hardware` on a worker thread with a hard timeout.

    On a wedged NVIDIA driver, `torch.cuda.is_available()` can block
    inside the driver for tens of seconds — which would freeze the
    splash before any logging is set up. We can't interrupt C-level
    driver calls, but we CAN time-bound the probe and fall back to
    safe CPU defaults if it doesn't return. The probe thread is left
    running (daemon=True) and its eventual return value is ignored;
    a subsequent process restart picks up the working config once
    the driver is healed.
    """
    import threading

    result: dict = {"device": "cpu", "device_name": None, "vram_gb": None}
    done = threading.Event()

    def _worker() -> None:
        nonlocal result
        try:
            result = _probe_hardware()
        except Exception:
            # Already handled internally, but belt-and-braces.
            pass
        finally:
            done.set()

    t = threading.Thread(target=_worker, name="hardware-probe", daemon=True)
    t.start()
    finished = done.wait(timeout=timeout_sec)
    if not finished:
        # Stderr because logging isn't configured yet at module-import
        # time. The user at least gets a breadcrumb in the attached console.
        import sys as _sys
        print(
            f"[AuraScribe] Hardware probe exceeded {timeout_sec:.0f}s — "
            "falling back to CPU defaults. Your GPU driver may be hung; "
            "try restarting the app or updating the NVIDIA driver.",
            file=_sys.stderr,
        )
    return result


HARDWARE_PROBE: dict = _probe_hardware_with_timeout()


def _default_whisper_device() -> str:
    return str(HARDWARE_PROBE["device"])


def _default_whisper_compute_type() -> str:
    device = _default_whisper_device()
    if device == "cuda":
        vram = HARDWARE_PROBE.get("vram_gb")
        if vram is None or vram >= 8:
            return "float16"
        if vram >= 4:
            return "int8_float16"
        return "int8"
    if device == "mps":
        # CTranslate2 uses Metal on MPS; float16 is the correct precision.
        return "float16"
    return "int8"


def _default_whisper_model() -> str:
    """Pick a model that'll actually run well on the detected hardware.

    `large-v3-turbo` is terrific on GPU but excruciating on CPU (~8x realtime
    on an i7). On CPU we start with `small` (~1x realtime, still usable
    quality), which the user can upgrade once they see how it performs.
    MPS (Apple Silicon) has excellent unified-memory bandwidth — treat it like
    a mid-range GPU and default to `large-v3-turbo`.
    """
    device = _default_whisper_device()
    if device == "cpu":
        return "small"
    if device == "cuda":
        vram = HARDWARE_PROBE.get("vram_gb")
        if vram is not None and vram < 4:
            return "medium"
    return "large-v3-turbo"


WHISPER_MODEL: str = _cfg_str("whisper_model", _default_whisper_model())
WHISPER_DEVICE: str = _cfg_str("whisper_device", _default_whisper_device())
WHISPER_COMPUTE_TYPE: str = _cfg_str("whisper_compute_type", _default_whisper_compute_type())

# Safety net: float16 is a no-op / error on CPU. If the user has crossed
# streams (e.g. device=cpu, compute_type=float16 from an old config) clamp
# to a safe combo and log. MPS is GPU-backed so float16 is fine there.
if WHISPER_DEVICE == "cpu" and WHISPER_COMPUTE_TYPE == "float16":
    import logging as _cfg_log
    _cfg_log.getLogger("aurascribe").warning(
        "config: whisper_compute_type=float16 is GPU-only; coercing to int8 "
        "because whisper_device=cpu. Override in Settings if needed."
    )
    WHISPER_COMPUTE_TYPE = "int8"

WHISPER_LANGUAGE: str = _cfg_str("whisper_language", "en")

# ── Diarization (pyannote) — Phase 4 consumes these ──────────────────────────

# Pipeline choice is fixed — swapping diarization pipelines is an expert-mode
# change that requires matching pyannote version + auth. Edit in-source.
DIARIZATION_MODEL: str = "pyannote/speaker-diarization-3.1"
MY_SPEAKER_LABEL: str = _cfg_str("my_speaker_label", "Me")

# ── Audio pipeline ───────────────────────────────────────────────────────────

SAMPLE_RATE: int = 16_000
CHANNELS: int = 1
# Seconds of audio per transcription chunk. Shorter = snappier partials,
# more Whisper invocations; longer = fewer but more complete chunks.
CHUNK_DURATION: float = _cfg_float("chunk_duration", 10.0)
# Silence (seconds) needed before VAD considers an utterance finished.
SILENCE_DURATION: float = _cfg_float("silence_duration", 0.6)
# Silero VAD confidence threshold in [0.0, 1.0]. Raise for noisy rooms;
# lower for quiet mics or whisperers.
VAD_THRESHOLD: float = _cfg_float("vad_threshold", 0.5)

# ── AEC (Speex acoustic echo canceller, pyaec) ───────────────────────────────
#
# Length of the linear filter tail the AEC keeps — effectively how far
# back in time it can "remember" the loopback reference when cancelling
# it out of the mic. Rooms with hard walls need a longer tail; headphone
# users can go shorter. See capture.py for why 200ms was the sweet spot
# on a typical desktop.
AEC_TAIL_MS: int = _cfg_int("aec_tail_ms", 200)

# ── Speaker identification thresholds ───────────────────────────────────────
#
# Cosine-distance gates for voice matching. Lower = stricter (fewer false
# positives, more "Unknown"); higher = more permissive.
VOICE_MATCH_THRESHOLD_MULTI: float = _cfg_float("voice_match_threshold_multi", 0.55)
VOICE_MATCH_THRESHOLD_SOLO: float = _cfg_float("voice_match_threshold_solo", 0.70)
# Ratio test — best candidate must beat runner-up by this factor.
VOICE_RATIO_MARGIN: float = _cfg_float("voice_ratio_margin", 0.80)
# Samples a Voice needs before it joins auto-matching. Below this, the
# Voice only applies when the user tags a line directly.
MIN_VOICE_SAMPLES: int = _cfg_int("min_voice_samples", 3)
# Provisional-clustering threshold for in-meeting unknowns ("Speaker 1/2…").
PROVISIONAL_THRESHOLD: float = _cfg_float("provisional_threshold", 0.50)

# ── Live partials (speculative transcription loop) ──────────────────────────
#
# The partial bubble re-transcribes audio since the last committed chunk
# boundary, capped at SPECULATIVE_WINDOW_SEC. That means the bubble
# naturally accumulates your current sentence as you speak, instead of
# sliding a fixed 4s window and appearing to "forget" the earlier words
# before the real chunk lands. Cap is 30s by default; shorter values make
# each partial pass cheaper at the cost of the bubble "forgetting" words
# spoken more than N seconds ago.
SPECULATIVE_INTERVAL_SEC: float = _cfg_float("speculative_interval_sec", 1.5)
SPECULATIVE_WINDOW_SEC: float = _cfg_float("speculative_window_sec", 30.0)

# ── Obsidian live-write throttle ────────────────────────────────────────────
#
# Write the live vault file when EITHER the interval OR the chunk count
# gate trips — whichever fires first. Tuned to keep Obsidian Sync watchers
# from thrashing during a long meeting.
OBSIDIAN_WRITE_INTERVAL_SEC: float = _cfg_float("obsidian_write_interval_sec", 15.0)
OBSIDIAN_WRITE_CHUNKS: int = _cfg_int("obsidian_write_chunks", 5)

# ── Daily Brief auto-refresh ────────────────────────────────────────────────
#
# When True, finishing a meeting kicks off a background Daily Brief regen
# for that meeting's date. Default is off — a regen is a long LLM call,
# and most users would rather kick it off manually from the Daily Brief
# page when they want it fresh.
DAILY_BRIEF_AUTO_REFRESH: bool = _cfg_bool("daily_brief_auto_refresh", False)

# ── Auto-capture (sustained-speech auto-start/stop) ─────────────────────────
#
# When True, the sidecar keeps a lightweight mic stream open whenever a
# meeting isn't already recording, runs Silero VAD on it, and auto-fires
# `start_meeting()` once it hears `auto_capture_start_speech_sec` of
# sustained speech. While the recording is active (and only when it was
# the monitor that started it — not a manual click), the pipeline's RMS
# stream is watched for `auto_capture_stop_silence_sec` of quiet before
# firing `stop_meeting()`. See `aurascribe.auto_capture` for the state
# machine. Default-on so a freshly installed app "just works" without the
# user needing to remember to hit Record — flip off in Settings to get
# the manual-only behavior back.
AUTO_CAPTURE_ENABLED: bool = _cfg_bool("auto_capture_enabled", True)
# Seconds of sustained speech before we auto-fire start_meeting. Lower =
# snappier (but may fire on a cough); higher = slower + more conservative.
AUTO_CAPTURE_START_SPEECH_SEC: float = _cfg_float("auto_capture_start_speech_sec", 1.5)
# Seconds of sustained silence during an auto-started recording before we
# auto-fire stop_meeting. Manually-started recordings ignore this — they
# always run until the user clicks Stop. Default 30s — short enough that
# the meeting wraps up promptly when the conversation actually ends, long
# enough to ride out a normal "let me look that up" pause.
AUTO_CAPTURE_STOP_SILENCE_SEC: float = _cfg_float("auto_capture_stop_silence_sec", 30.0)
# Seconds of continuous silence before the Stop button morphs into a
# live countdown. Gives the user early warning that auto-stop is
# counting against them — before the warning, the bar stays quiet.
AUTO_CAPTURE_COUNTDOWN_AFTER_SILENCE_SEC: float = _cfg_float(
    "auto_capture_countdown_after_silence_sec", 5.0,
)
# Silero VAD confidence threshold used by the monitor. Defaults to the
# shared `vad_threshold` (the same gate the recording pipeline uses); set
# explicitly to tune listening sensitivity independently — e.g. raise it
# in a noisy open office if the monitor keeps triggering on background
# chatter.
AUTO_CAPTURE_VAD_THRESHOLD: float = _cfg_float("auto_capture_vad_threshold", VAD_THRESHOLD)


def reload_auto_capture_from_file() -> None:
    """Re-read auto-capture keys from config.json into the module-level
    vars. Called after a successful PUT /api/settings/config so the
    running monitor can pick up toggle + threshold changes without a
    sidecar restart. Every other setting still requires a restart — these
    four are singled out because the monitor is the one piece of state
    that can safely hot-swap, and a user toggling auto-capture wants to
    feel an immediate effect."""
    global AUTO_CAPTURE_ENABLED, AUTO_CAPTURE_START_SPEECH_SEC
    global AUTO_CAPTURE_STOP_SILENCE_SEC, AUTO_CAPTURE_VAD_THRESHOLD
    global AUTO_CAPTURE_COUNTDOWN_AFTER_SILENCE_SEC
    fresh = load_user_config()
    _user_config.clear()
    _user_config.update(fresh)
    AUTO_CAPTURE_ENABLED = _cfg_bool("auto_capture_enabled", True)
    AUTO_CAPTURE_START_SPEECH_SEC = _cfg_float("auto_capture_start_speech_sec", 1.5)
    AUTO_CAPTURE_STOP_SILENCE_SEC = _cfg_float("auto_capture_stop_silence_sec", 30.0)
    AUTO_CAPTURE_VAD_THRESHOLD = _cfg_float("auto_capture_vad_threshold", VAD_THRESHOLD)
    AUTO_CAPTURE_COUNTDOWN_AFTER_SILENCE_SEC = _cfg_float(
        "auto_capture_countdown_after_silence_sec", 5.0,
    )

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
