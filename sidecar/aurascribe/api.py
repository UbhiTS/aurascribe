"""FastAPI app wiring — lifespan + CORS + WebSocket + top-level status.

The sidecar only serves JSON + WebSocket (the Tauri WebView loads the
frontend directly); feature endpoints live in `aurascribe.routes.*` and
are mounted below.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from contextlib import asynccontextmanager

import aiosqlite
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from aurascribe import __version__
from aurascribe import config
from aurascribe.auto_capture import AutoCaptureMonitor
from aurascribe.db.database import init_db
from aurascribe.llm.client import get_available_models
from aurascribe.obsidian.writer import bootstrap_vault_templates, cleanup_vault_stragglers
from aurascribe.routes import (
    daily_brief_router,
    intel_router,
    meetings_router,
    settings_router,
    voices_router,
)
from aurascribe.routes import _shared as _shared_mod
from aurascribe.routes._shared import (
    backfill_voice_colors,
    broadcast,
    broadcast_lock,
    manager,
    set_auto_capture_monitor,
    ws_clients,
)
from aurascribe.routes.daily_brief import regen_brief_for_meeting
from aurascribe.tasks import BlockingCallTimeout, run_sync_with_timeout, safe_task
from aurascribe.transcription import Utterance

log = logging.getLogger("aurascribe")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # One-time migration: legacy voices stored hex colors ("#a78bfa"); the
    # new scheme stores palette keys ("rose") so the frontend can map to
    # its Tailwind class table. Cheap no-op once every voice is keyed.
    async with aiosqlite.connect(config.DB_PATH) as _db:
        await backfill_voice_colors(_db)
    cleanup_vault_stragglers()
    # Seed reference templates into 90-Templates/ on first boot. Idempotent —
    # existing user edits are never overwritten.
    try:
        await bootstrap_vault_templates()
    except Exception as e:
        log.warning("Could not seed vault templates: %s", e)

    async def on_utterance(meeting_id: str, utterances: list[Utterance]) -> None:
        await broadcast(
            {
                "type": "utterances",
                "meeting_id": meeting_id,
                "data": [
                    {
                        "id": u.id,
                        "speaker": u.speaker,
                        "text": u.text,
                        "start_time": u.start,
                        "end_time": u.end,
                        "match_distance": u.match_distance,
                        "audio_start": u.audio_start,
                    }
                    for u in utterances
                ],
            }
        )

    async def on_partial(meeting_id: str, speaker: str, text: str) -> None:
        await broadcast(
            {"type": "partial_utterance", "meeting_id": meeting_id, "speaker": speaker, "text": text}
        )

    # Built before on_status so the handler can forward events to it.
    auto_capture = AutoCaptureMonitor(manager=manager, broadcast=broadcast)
    set_auto_capture_monitor(auto_capture)

    async def on_status(event: str, data: dict) -> None:
        await broadcast({"type": "status", "event": event, **data})
        # Hand the event to the auto-capture monitor so it can close its
        # listening stream on "recording" and re-open on "done". Fire-and-
        # forget — the monitor's own lock serialises the resulting mic
        # transitions.
        safe_task(
            auto_capture.on_manager_status(event, data),
            name=f"auto_capture.on_manager_status[{event}]",
        )
        # Auto-enable the monitor the first time the engine reports ready.
        # We don't enable during "loading" because start_meeting() would
        # reject any trigger with "Models still loading" anyway.
        if event == "ready" and config.AUTO_CAPTURE_ENABLED and not auto_capture.enabled:
            safe_task(auto_capture.enable(), name="auto_capture.enable")
        # Meeting finished → the Daily Brief for that meeting's date is now
        # stale. Mark + regenerate in the background so the user's next
        # visit to the Daily Briefs page is already fresh. Skipped when the
        # user has turned off auto-refresh (Settings → Advanced → Daily
        # Brief) — the page can still be regenerated manually.
        if (
            event == "done"
            and data.get("meeting_id")
            and config.DAILY_BRIEF_AUTO_REFRESH
        ):
            safe_task(
                regen_brief_for_meeting(data["meeting_id"]),
                name=f"daily_brief.regen[{data['meeting_id']}]",
            )

    async def on_level(rms: float, peak: float) -> None:
        # ~30Hz during recording. Small-payload broadcast, no coalescing —
        # the WS lock is cheap and Waveform sub-samples to 20Hz anyway.
        await broadcast({"type": "audio_level", "rms": rms, "peak": peak})
        # Feed the same signal into auto-capture's silence detector so it
        # can auto-stop meetings it started. The monitor guards its own
        # state transitions; manually-started meetings are a no-op here.
        await auto_capture.on_manager_level(rms, peak)

    manager.on_utterance(on_utterance)
    manager.on_partial(on_partial)
    manager.on_status(on_status)
    manager.on_level(on_level)
    manager.intel.set_broadcast(broadcast)
    # Engine load runs in the background so the FastAPI lifespan can
    # return and the UI can reach /api/status (which surfaces loading
    # progress and any fatal load_error). Exceptions inside initialize()
    # are already caught there and converted to a "status: error" event;
    # safe_task is belt-and-braces in case the catch itself breaks.
    safe_task(manager.initialize(), name="manager.initialize")
    yield
    # Shutdown — release the monitor's mic stream cleanly so a restart
    # doesn't trip over a lingering PortAudio handle on Windows.
    try:
        await auto_capture.disable()
    except Exception:
        pass


app = FastAPI(title="AuraScribe Sidecar", version=__version__, lifespan=lifespan)

# The sidecar binds to 127.0.0.1 only and is reached by two clients:
# the Tauri WebView (production) and the Vite dev server (dev). Locking
# CORS to those origins instead of "*" removes a wide-open surface area
# without breaking anything — fetch() from any other origin simply fails.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:1420",
        "http://localhost:1420",
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

# ── Mount per-feature routers ───────────────────────────────────────────────
#
# Each router carries its own URL prefix (e.g. /api/meetings, /api/voices),
# so include_router() stays prefix-free at this level.
app.include_router(meetings_router)
app.include_router(voices_router)
app.include_router(settings_router)
app.include_router(daily_brief_router)
app.include_router(intel_router)


# ── WebSocket ───────────────────────────────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    async with broadcast_lock:
        ws_clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        async with broadcast_lock:
            try:
                ws_clients.remove(websocket)
            except ValueError:
                pass  # already pruned by a concurrent broadcast failure


# ── Status + model list ─────────────────────────────────────────────────────


@app.get("/api/status")
async def get_status() -> dict:
    # ASR / diarization runtime facts — `engine_ready` gates diarization_device
    # because pyannote may not have finished loading yet (or failed to load
    # entirely, in which case diarization is disabled). Whisper config comes
    # from config.py directly so the header can show the model + device even
    # during the loading phase — those values are decided at import time.
    engine = manager.engine
    diar_device = getattr(engine, "diarization_device", None) if manager.is_ready else None
    diar_enabled = manager.is_ready and diar_device is not None

    # Device enumeration hits WASAPI / soundcard and can stall the event
    # loop on a wedged audio driver (seen on Windows with a disconnected
    # USB endpoint). Run it in a worker thread with a hard timeout —
    # empty list on timeout, so the picker falls back to "Default mic"
    # rather than freezing the whole /api/status poll.
    async def _safe_list(fn, name: str) -> list[dict]:
        try:
            return await run_sync_with_timeout(fn, timeout=3.0, name=name)
        except BlockingCallTimeout:
            log.warning("%s timed out — returning empty device list", name)
            return []
        except Exception:
            log.exception("%s failed", name)
            return []
    audio_devices = await _safe_list(manager.list_audio_devices, "list_audio_devices")
    audio_output_devices = await _safe_list(
        manager.list_audio_output_devices, "list_audio_output_devices",
    )
    return {
        "ok": True,
        "version": __version__,
        "platform": sys.platform,   # "win32" | "darwin" | "linux" — lets the frontend adapt UI text
        "engine_ready": manager.is_ready,
        "engine_load_error": manager.load_error,
        "is_recording": manager.is_recording,
        "current_meeting_id": manager.current_meeting_id,
        "audio_devices": audio_devices,
        "audio_output_devices": audio_output_devices,
        "active_audio_device": manager.active_device_name,
        "obsidian_configured": config.OBSIDIAN_VAULT is not None,
        # Hardware the sidecar probed at import. Frozen for the lifetime of
        # the process; survives in the UI as the "Detected:" chip.
        "hardware": {
            "device": config.HARDWARE_PROBE["device"],
            "device_name": config.HARDWARE_PROBE.get("device_name"),
            "vram_gb": config.HARDWARE_PROBE.get("vram_gb"),
        },
        # What the running pipelines are actually doing. Values here let the
        # header show "Whisper large-v3-turbo · GPU" and "Diarization · CPU"
        # chips so the user always knows where the compute is happening.
        "asr": {
            "model": config.WHISPER_MODEL,
            "device": config.WHISPER_DEVICE,
            "compute_type": config.WHISPER_COMPUTE_TYPE,
        },
        "diarization": {
            "enabled": diar_enabled,
            "device": diar_device,  # "cuda" | "cpu" | None
        },
        # Snapshot of the auto-capture monitor so the UI can render the
        # correct toggle state on first load, before the first WS
        # `auto_capture` event arrives. `state` is one of:
        #   "disabled" | "listening" | "armed" | "recording" | "error"
        "auto_capture": (
            _shared_mod.auto_capture_monitor.snapshot()
            if _shared_mod.auto_capture_monitor is not None
            else {"enabled": False, "state": "disabled", "confidence": 0.0}
        ),
    }


# ── Auto-capture toggle ─────────────────────────────────────────────────────
#
# Persisted to config.json (same place Settings uses) AND applied live to
# the running monitor. Kept as a dedicated endpoint rather than making the
# user navigate to Settings — the toggle lives front-and-center on the
# Recording bar so flipping it mid-session shouldn't need two API calls.


class AutoCaptureToggle(BaseModel):
    enabled: bool


@app.get("/api/auto-capture")
async def get_auto_capture() -> dict:
    monitor = _shared_mod.auto_capture_monitor
    if monitor is None:
        return {"enabled": False, "state": "disabled", "confidence": 0.0}
    return monitor.snapshot()


@app.put("/api/auto-capture")
async def put_auto_capture(req: AutoCaptureToggle) -> dict:
    config.save_user_config({"auto_capture_enabled": req.enabled})
    config.reload_auto_capture_from_file()
    monitor = _shared_mod.auto_capture_monitor
    if monitor is not None:
        try:
            await monitor.reload_from_config()
        except Exception as e:
            raise HTTPException(500, f"Could not apply auto-capture toggle: {e}")
    return monitor.snapshot() if monitor is not None else {
        "enabled": req.enabled, "state": "disabled", "confidence": 0.0,
    }


@app.get("/api/models")
async def list_models() -> dict:
    return {"models": await get_available_models()}


@app.post("/api/system/retry-init")
async def retry_engine_init() -> dict:
    """Retry a failed engine load. Called from the splash's error card
    after the user has fixed the underlying problem (re-pasted HF token,
    reconnected network, freed VRAM).

    Rejected if the engine is already ready or currently loading (status
    transitions are surfaced via WS; the UI can tell).
    """
    if manager.is_ready:
        return {"ok": True, "message": "Engine already ready"}
    if manager.load_error is None:
        # No prior error to retry from — either still loading or never tried.
        raise HTTPException(409, "Engine load is not in an error state")
    safe_task(manager.initialize(), name="manager.initialize[retry]")
    return {"ok": True, "message": "Engine reload started"}


@app.post("/api/system/open-mic-settings")
async def open_mic_settings() -> dict:
    """Launch the OS microphone-privacy settings pane.

    Called from the frontend's "permission denied" dialog so the user gets a
    one-click path to the fix rather than having to hunt through system settings
    manually.

    Windows — opens ms-settings:privacy-microphone via cmd /c start.
    macOS   — opens System Settings → Privacy & Security → Microphone via open.
    Other   — returns {ok: false, reason: "unsupported-platform"}.
    """
    if sys.platform == "win32":
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", "ms-settings:privacy-microphone"],
                shell=False,
                close_fds=True,
            )
        except Exception as e:
            raise HTTPException(500, f"Could not open mic settings: {e}")
        return {"ok": True}

    if sys.platform == "darwin":
        try:
            # The x-apple.systempreferences: URL scheme opens the specified
            # pane in System Settings (macOS 13+) or System Preferences (12).
            subprocess.Popen(
                [
                    "open",
                    "x-apple.systempreferences:com.apple.preference.security"
                    "?Privacy_Microphone",
                ],
                close_fds=True,
            )
        except Exception as e:
            raise HTTPException(500, f"Could not open mic settings: {e}")
        return {"ok": True}

    return {"ok": False, "reason": "unsupported-platform"}
