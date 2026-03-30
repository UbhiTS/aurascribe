"""
FastAPI application — REST endpoints + WebSocket for real-time transcript streaming.
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.config import DB_PATH, APP_DIR
from backend.db.database import init_db
from backend.meeting_manager import MeetingManager
from backend.llm.client import chat, get_available_models, LLMUnavailableError
from backend.llm.prompts import meeting_summary_prompt, format_transcript, MEETING_SUMMARY_SYSTEM
from backend.obsidian.writer import write_meeting

app = FastAPI(title="AuraScribe", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

manager = MeetingManager()
ws_clients: list[WebSocket] = []


# ── Startup ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    await init_db()
    asyncio.create_task(_initialize_manager())


async def _initialize_manager():
    async def on_utterance(meeting_id: int, utterances):
        payload = {
            "type": "utterances",
            "meeting_id": meeting_id,
            "data": [
                {
                    "speaker": u.speaker,
                    "text": u.text,
                    "start_time": u.start,
                    "end_time": u.end,
                }
                for u in utterances
            ],
        }
        await _broadcast(payload)

    async def on_partial(meeting_id: int, speaker: str, text: str):
        await _broadcast({
            "type": "partial_utterance",
            "meeting_id": meeting_id,
            "speaker": speaker,
            "text": text,
        })

    async def on_status(event: str, data: dict):
        await _broadcast({"type": "status", "event": event, **data})

    manager.on_utterance(on_utterance)
    manager.on_partial(on_partial)
    manager.on_status(on_status)
    await manager.initialize()


async def _broadcast(payload: dict):
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.remove(ws)


# ── WebSocket ──────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep alive
    except WebSocketDisconnect:
        ws_clients.remove(websocket)


# ── Meeting endpoints ──────────────────────────────────────────────────────────

class StartMeetingRequest(BaseModel):
    title: str = ""
    device: int | None = None


@app.post("/api/meetings/start")
async def start_meeting(req: StartMeetingRequest):
    try:
        meeting_id = await manager.start_meeting(title=req.title, device=req.device)
        return {"meeting_id": meeting_id, "status": "recording"}
    except RuntimeError as e:
        raise HTTPException(400, str(e))


class StopMeetingRequest(BaseModel):
    summarize: bool = False


@app.post("/api/meetings/stop")
async def stop_meeting(req: StopMeetingRequest = StopMeetingRequest()):
    try:
        result = await manager.stop_meeting(summarize=req.summarize)
        return result
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.get("/api/meetings")
async def list_meetings(limit: int = 20, offset: int = 0, days: int = 2):
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, title, started_at, ended_at, status, vault_path "
            "FROM meetings WHERE started_at >= ? ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (cutoff, limit, offset),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


class BulkDeleteRequest(BaseModel):
    ids: list[int]


@app.post("/api/meetings/bulk-delete")
async def bulk_delete_meetings(req: BulkDeleteRequest):
    if not req.ids:
        return {"ok": True, "deleted": 0}
    placeholders = ",".join("?" * len(req.ids))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"DELETE FROM utterances WHERE meeting_id IN ({placeholders})", req.ids)
        await db.execute(f"DELETE FROM meetings WHERE id IN ({placeholders})", req.ids)
        await db.commit()
    return {"ok": True, "deleted": len(req.ids)}


@app.delete("/api/meetings/all")
async def clear_all_meetings(days: int = 2):
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM meetings WHERE started_at >= ?", (cutoff,)
        )
        ids = [row[0] async for row in cursor]
        if ids:
            placeholders = ",".join("?" * len(ids))
            await db.execute(f"DELETE FROM utterances WHERE meeting_id IN ({placeholders})", ids)
            await db.execute(f"DELETE FROM meetings WHERE id IN ({placeholders})", ids)
            await db.commit()
    return {"ok": True, "deleted": len(ids)}


@app.get("/api/meetings/{meeting_id}")
async def get_meeting(meeting_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Meeting not found")
        meeting = dict(row)

        cursor = await db.execute(
            "SELECT speaker, text, start_time, end_time FROM utterances "
            "WHERE meeting_id = ? ORDER BY start_time",
            (meeting_id,),
        )
        utterances = [dict(u) async for u in cursor]

    meeting["utterances"] = utterances
    return meeting


async def _rewrite_vault(meeting_id: int):
    """Reload meeting + utterances from DB and overwrite the Obsidian vault file."""
    from backend.transcription.engine import Utterance as Utt
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT title, started_at, summary, action_items FROM meetings WHERE id = ?",
            (meeting_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return
        title = row["title"]
        started_at = datetime.fromisoformat(row["started_at"])
        summary = row["summary"] or ""
        action_items = json.loads(row["action_items"]) if row["action_items"] else []
        cursor = await db.execute(
            "SELECT speaker, text, start_time, end_time FROM utterances "
            "WHERE meeting_id = ? ORDER BY start_time",
            (meeting_id,),
        )
        rows = await cursor.fetchall()
    utterances = [Utt(speaker=r["speaker"], text=r["text"], start=r["start_time"], end=r["end_time"]) for r in rows]
    await write_meeting(
        meeting_id=meeting_id,
        title=title,
        started_at=started_at,
        utterances=utterances,
        summary=summary,
        action_items=action_items,
    )


@app.delete("/api/meetings/{meeting_id}")
async def delete_meeting(meeting_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM utterances WHERE meeting_id = ?", (meeting_id,))
        await db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        await db.commit()
    return {"ok": True}


class RenameMeetingRequest(BaseModel):
    title: str


@app.patch("/api/meetings/{meeting_id}")
async def rename_meeting(meeting_id: int, req: RenameMeetingRequest):
    if not req.title.strip():
        raise HTTPException(400, "Title cannot be empty")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT vault_path FROM meetings WHERE id = ?", (meeting_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Meeting not found")
        old_vault_path = row["vault_path"]
        await db.execute(
            "UPDATE meetings SET title = ? WHERE id = ?", (req.title.strip(), meeting_id)
        )
        await db.commit()
    # Delete old vault file before rewriting with new filename
    if old_vault_path:
        from pathlib import Path as _Path
        old_file = _Path(old_vault_path)
        if old_file.exists():
            old_file.unlink()
    await _rewrite_vault(meeting_id)
    return {"ok": True}


@app.post("/api/meetings/{meeting_id}/summarize")
async def summarize_meeting(meeting_id: int):
    from backend.transcription.engine import Utterance as Utt
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT title FROM meetings WHERE id = ?", (meeting_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Meeting not found")
        title = row["title"]
        cursor = await db.execute(
            "SELECT speaker, text, start_time, end_time FROM utterances "
            "WHERE meeting_id = ? ORDER BY start_time",
            (meeting_id,),
        )
        utt_rows = await cursor.fetchall()

    if not utt_rows:
        raise HTTPException(400, "No transcript available to summarize")

    utterances = [Utt(speaker=r["speaker"], text=r["text"], start=r["start_time"], end=r["end_time"]) for r in utt_rows]
    transcript = format_transcript(utterances)

    try:
        summary_md = await chat(meeting_summary_prompt(transcript, title), system=MEETING_SUMMARY_SYSTEM)
    except LLMUnavailableError as e:
        raise HTTPException(503, str(e))

    action_items = manager._extract_action_items(summary_md)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE meetings SET summary = ?, action_items = ? WHERE id = ?",
            (summary_md, json.dumps(action_items) if action_items else None, meeting_id),
        )
        await db.commit()

    await _rewrite_vault(meeting_id)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
        row = await cursor.fetchone()
    return dict(row)


@app.get("/api/meetings/{meeting_id}/transcript")
async def get_transcript(meeting_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT speaker, text, start_time, end_time FROM utterances "
            "WHERE meeting_id = ? ORDER BY start_time",
            (meeting_id,),
        )
        utterances = [dict(u) async for u in cursor]
    return {"meeting_id": meeting_id, "utterances": utterances}


# ── Status ─────────────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    return {
        "is_recording": manager.is_recording,
        "current_meeting_id": manager.current_meeting_id,
        "audio_devices": manager.list_audio_devices(),
    }


@app.get("/api/models")
async def list_models():
    models = await get_available_models()
    return {"models": models}


# ── People endpoints ───────────────────────────────────────────────────────────

@app.get("/api/people")
async def list_people():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, name, vault_path, created_at FROM people ORDER BY name"
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ── Speaker enrollment ─────────────────────────────────────────────────────────

class EnrollRequest(BaseModel):
    name: str
    duration: float = 10.0


@app.post("/api/enroll/start")
async def enroll_start(req: EnrollRequest):
    from backend.audio.enrollment import record_enrollment_sample, save_enrollment
    await _broadcast({"type": "status", "event": "enrolling", "message": f"Recording {req.duration}s sample for {req.name}..."})
    audio = await record_enrollment_sample(req.duration)
    person_id = await save_enrollment(req.name, audio)
    await manager.engine.reload_enrolled()
    return {"person_id": person_id, "name": req.name}


# ── Rename speaker ─────────────────────────────────────────────────────────────

class RenameSpeakerRequest(BaseModel):
    meeting_id: int
    old_name: str
    new_name: str


@app.post("/api/meetings/{meeting_id}/rename-speaker")
async def rename_speaker(meeting_id: int, req: RenameSpeakerRequest):
    async with aiosqlite.connect(DB_PATH) as db:
        # Update utterances in this meeting
        await db.execute(
            "UPDATE utterances SET speaker = ? WHERE meeting_id = ? AND speaker = ?",
            (req.new_name, meeting_id, req.old_name),
        )
        # If the old name matches an enrolled person, rename them globally
        # so future utterances from that voice use the new name
        await db.execute(
            "UPDATE people SET name = ? WHERE name = ?",
            (req.new_name, req.old_name),
        )
        await db.commit()
    # Reload enrolled speakers so the engine uses the new name immediately
    await manager.engine.reload_enrolled()
    # Rewrite the Obsidian vault file with updated speaker names
    await _rewrite_vault(meeting_id)
    return {"ok": True}


# ── Serve React frontend ───────────────────────────────────────────────────────

frontend_dist = APP_DIR / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/assets", StaticFiles(directory=str(frontend_dist / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        index = frontend_dist / "index.html"
        return FileResponse(str(index))
