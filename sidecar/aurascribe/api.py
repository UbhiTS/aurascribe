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

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from aurascribe import __version__
from aurascribe import config
from aurascribe.db.database import init_db
from aurascribe.llm.client import get_available_models
from aurascribe.obsidian.writer import cleanup_vault_stragglers
from aurascribe.routes import (
    daily_brief_router,
    intel_router,
    meetings_router,
    settings_router,
    voices_router,
)
from aurascribe.routes._shared import (
    broadcast,
    broadcast_lock,
    manager,
    ws_clients,
)
from aurascribe.routes.daily_brief import regen_brief_for_meeting
from aurascribe.transcription import Utterance

log = logging.getLogger("aurascribe")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    cleanup_vault_stragglers()

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

    async def on_status(event: str, data: dict) -> None:
        await broadcast({"type": "status", "event": event, **data})
        # Meeting finished → the Daily Brief for that meeting's date is now
        # stale. Mark + regenerate in the background so the user's next
        # visit to the Daily Briefs page is already fresh.
        if event == "done" and data.get("meeting_id"):
            asyncio.create_task(regen_brief_for_meeting(data["meeting_id"]))

    manager.on_utterance(on_utterance)
    manager.on_partial(on_partial)
    manager.on_status(on_status)
    manager.intel.set_broadcast(broadcast)
    asyncio.create_task(manager.initialize())
    yield


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
    return {
        "ok": True,
        "version": __version__,
        "engine_ready": manager.is_ready,
        "is_recording": manager.is_recording,
        "current_meeting_id": manager.current_meeting_id,
        "audio_devices": manager.list_audio_devices(),
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
    }


@app.get("/api/models")
async def list_models() -> dict:
    return {"models": await get_available_models()}


@app.post("/api/system/open-mic-settings")
async def open_mic_settings() -> dict:
    """Launch the OS microphone-privacy settings pane.

    Called from the frontend's "permission denied" dialog so the user
    gets a one-click path to the fix rather than having to hunt through
    Windows Settings manually. No-op on non-Windows (returns a hint).
    """
    if sys.platform != "win32":
        return {"ok": False, "reason": "only-windows"}
    try:
        # `ms-settings:` is a Windows shell URI scheme. `cmd /c start` is the
        # canonical dispatcher — same pattern as intel.open-prompt.
        subprocess.Popen(
            ["cmd", "/c", "start", "", "ms-settings:privacy-microphone"],
            shell=False,
            close_fds=True,
        )
    except Exception as e:
        raise HTTPException(500, f"Could not open mic settings: {e}")
    return {"ok": True}
