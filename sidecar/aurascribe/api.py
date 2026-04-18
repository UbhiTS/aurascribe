"""FastAPI app — REST + WebSocket.

Full route surface ported from the legacy backend. The SPA-mount routes are
gone: the Tauri WebView loads the frontend directly, so the sidecar only
serves JSON + WebSocket.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import aiosqlite
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from aurascribe import __version__
from aurascribe.config import DB_PATH
from aurascribe.db.database import init_db
from aurascribe.llm.client import LLMUnavailableError, chat, get_available_models
from aurascribe.llm.prompts import MEETING_SUMMARY_SYSTEM, format_transcript, meeting_summary_prompt
from aurascribe.meeting_manager import MeetingManager
from aurascribe.obsidian.writer import write_meeting
from aurascribe.transcription import Utterance

manager = MeetingManager()
ws_clients: list[WebSocket] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    async def on_utterance(meeting_id: str, utterances: list[Utterance]) -> None:
        await _broadcast(
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
                    }
                    for u in utterances
                ],
            }
        )

    async def on_partial(meeting_id: str, speaker: str, text: str) -> None:
        await _broadcast(
            {"type": "partial_utterance", "meeting_id": meeting_id, "speaker": speaker, "text": text}
        )

    async def on_status(event: str, data: dict) -> None:
        await _broadcast({"type": "status", "event": event, **data})

    manager.on_utterance(on_utterance)
    manager.on_partial(on_partial)
    manager.on_status(on_status)
    asyncio.create_task(manager.initialize())
    yield


app = FastAPI(title="AuraScribe Sidecar", version=__version__, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)


async def _broadcast(payload: dict) -> None:
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.remove(ws)


# ── WebSocket ────────────────────────────────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    ws_clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_clients.remove(websocket)


# ── Meeting endpoints ────────────────────────────────────────────────────────


class StartMeetingRequest(BaseModel):
    title: str = ""
    device: int | None = None


@app.post("/api/meetings/start")
async def start_meeting(req: StartMeetingRequest) -> dict:
    try:
        meeting_id = await manager.start_meeting(title=req.title, device=req.device)
        return {"meeting_id": meeting_id, "status": "recording"}
    except RuntimeError as e:
        raise HTTPException(400, str(e))


class StopMeetingRequest(BaseModel):
    summarize: bool = False


@app.post("/api/meetings/stop")
async def stop_meeting(req: StopMeetingRequest = StopMeetingRequest()) -> dict:
    try:
        return await manager.stop_meeting(summarize=req.summarize)
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.get("/api/meetings")
async def list_meetings(limit: int = 20, offset: int = 0, days: int = 2) -> list[dict]:
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
    ids: list[str]


@app.post("/api/meetings/bulk-delete")
async def bulk_delete_meetings(req: BulkDeleteRequest) -> dict:
    if not req.ids:
        return {"ok": True, "deleted": 0}
    placeholders = ",".join("?" * len(req.ids))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"DELETE FROM utterances WHERE meeting_id IN ({placeholders})", req.ids)
        await db.execute(f"DELETE FROM meetings WHERE id IN ({placeholders})", req.ids)
        await db.commit()
    return {"ok": True, "deleted": len(req.ids)}


@app.delete("/api/meetings/all")
async def clear_all_meetings(days: int = 2) -> dict:
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id FROM meetings WHERE started_at >= ?", (cutoff,))
        ids = [row[0] async for row in cursor]
        if ids:
            placeholders = ",".join("?" * len(ids))
            await db.execute(f"DELETE FROM utterances WHERE meeting_id IN ({placeholders})", ids)
            await db.execute(f"DELETE FROM meetings WHERE id IN ({placeholders})", ids)
            await db.commit()
    return {"ok": True, "deleted": len(ids)}


@app.get("/api/meetings/{meeting_id}")
async def get_meeting(meeting_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Meeting not found")
        meeting = dict(row)

        cursor = await db.execute(
            "SELECT id, speaker, text, start_time, end_time FROM utterances "
            "WHERE meeting_id = ? ORDER BY start_time",
            (meeting_id,),
        )
        utterances = [dict(u) async for u in cursor]

    meeting["utterances"] = utterances
    return meeting


async def _rewrite_vault(meeting_id: str) -> None:
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
            "SELECT id, speaker, text, start_time, end_time FROM utterances "
            "WHERE meeting_id = ? ORDER BY start_time",
            (meeting_id,),
        )
        rows = await cursor.fetchall()
    utterances = [
        Utterance(speaker=r["speaker"], text=r["text"], start=r["start_time"], end=r["end_time"])
        for r in rows
    ]
    await write_meeting(
        meeting_id=meeting_id,
        title=title,
        started_at=started_at,
        utterances=utterances,
        summary=summary,
        action_items=action_items,
    )


@app.delete("/api/meetings/{meeting_id}")
async def delete_meeting(meeting_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM utterances WHERE meeting_id = ?", (meeting_id,))
        await db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        await db.commit()
    return {"ok": True}


class RenameMeetingRequest(BaseModel):
    title: str


@app.patch("/api/meetings/{meeting_id}")
async def rename_meeting(meeting_id: str, req: RenameMeetingRequest) -> dict:
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
    if old_vault_path:
        from pathlib import Path

        old_file = Path(old_vault_path)
        if old_file.exists():
            old_file.unlink()
    await _rewrite_vault(meeting_id)
    return {"ok": True}


@app.post("/api/meetings/{meeting_id}/summarize")
async def summarize_meeting(meeting_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT title FROM meetings WHERE id = ?", (meeting_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Meeting not found")
        title = row["title"]
        cursor = await db.execute(
            "SELECT id, speaker, text, start_time, end_time FROM utterances "
            "WHERE meeting_id = ? ORDER BY start_time",
            (meeting_id,),
        )
        utt_rows = await cursor.fetchall()

    if not utt_rows:
        raise HTTPException(400, "No transcript available to summarize")

    utterances = [
        Utterance(speaker=r["speaker"], text=r["text"], start=r["start_time"], end=r["end_time"])
        for r in utt_rows
    ]
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
    return dict(row) if row else {}


class TrimMeetingRequest(BaseModel):
    before: float | None = None  # delete utterances with start_time < before (then rebase to 0)
    after: float | None = None   # delete utterances with start_time > after


@app.post("/api/meetings/{meeting_id}/trim")
async def trim_meeting(meeting_id: str, req: TrimMeetingRequest) -> dict:
    """Crop the transcript. Deletes utterances outside [before, after].

    Semantics are list-positional: `before` and `after` are the clicked line's
    start_time, and that clicked line is always kept. Strict inequality on
    start_time on both sides means the clicked line itself stays.

    When `before` is given, remaining utterances are rebased so the first one
    starts at 0 — keeps displayed timestamps sensible. `started_at` on the
    meeting row is shifted forward by the same amount.

    Clears summary/action_items (stale post-trim) and re-writes the vault file.
    """
    if req.before is None and req.after is None:
        raise HTTPException(400, "Provide at least one of 'before' or 'after'")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT status, started_at FROM meetings WHERE id = ?", (meeting_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Meeting not found")
        if row["status"] == "recording":
            raise HTTPException(400, "Cannot trim a meeting that is still recording")
        started_at = datetime.fromisoformat(row["started_at"])

        if req.before is not None:
            await db.execute(
                "DELETE FROM utterances WHERE meeting_id = ? AND start_time < ?",
                (meeting_id, req.before),
            )
        if req.after is not None:
            await db.execute(
                "DELETE FROM utterances WHERE meeting_id = ? AND start_time > ?",
                (meeting_id, req.after),
            )

        cursor = await db.execute(
            "SELECT MIN(start_time) AS min_s, MAX(end_time) AS max_e "
            "FROM utterances WHERE meeting_id = ?",
            (meeting_id,),
        )
        bounds = await cursor.fetchone()
        min_start = bounds["min_s"] if bounds else None
        max_end = bounds["max_e"] if bounds else None

        shift = 0.0
        if req.before is not None and min_start is not None and min_start > 0:
            shift = float(min_start)
            await db.execute(
                "UPDATE utterances SET start_time = start_time - ?, end_time = end_time - ? "
                "WHERE meeting_id = ?",
                (shift, shift, meeting_id),
            )

        new_started_at = started_at + timedelta(seconds=shift) if shift else started_at
        new_ended_at = new_started_at + timedelta(seconds=float(max_end) - shift) if max_end is not None else None

        await db.execute(
            "UPDATE meetings SET started_at = ?, ended_at = ?, summary = NULL, action_items = NULL WHERE id = ?",
            (new_started_at.isoformat(), new_ended_at.isoformat() if new_ended_at else None, meeting_id),
        )
        await db.commit()

    await _rewrite_vault(meeting_id)
    return {"ok": True, "shifted_by": shift}


class SplitMeetingRequest(BaseModel):
    at: float  # seconds — utterances with start_time >= at move to the new meeting
    new_title: str | None = None


@app.post("/api/meetings/{meeting_id}/split")
async def split_meeting(meeting_id: str, req: SplitMeetingRequest) -> dict:
    """Split a meeting in two at timestamp `at`.

    Creates a new meeting; moves utterances (and their speaker_enrollment rows)
    with start_time >= at to it, rebasing their timestamps to 0. The original
    meeting keeps utterances before the cut. Both meetings get fresh vault files
    and their summaries cleared (stale post-split).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT title, started_at, status, vault_path FROM meetings WHERE id = ?",
            (meeting_id,),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Meeting not found")
        if row["status"] == "recording":
            raise HTTPException(400, "Cannot split a meeting that is still recording")

        original_title = row["title"]
        original_started_at = datetime.fromisoformat(row["started_at"])

        cursor = await db.execute(
            "SELECT MIN(start_time) AS min_s, MAX(end_time) AS max_e "
            "FROM utterances WHERE meeting_id = ? AND start_time >= ?",
            (meeting_id, req.at),
        )
        after_bounds = await cursor.fetchone()
        if after_bounds is None or after_bounds["min_s"] is None:
            raise HTTPException(400, "No utterances at or after the split point")

        cursor = await db.execute(
            "SELECT COUNT(*) AS n FROM utterances WHERE meeting_id = ? AND start_time < ?",
            (meeting_id, req.at),
        )
        before_count_row = await cursor.fetchone()
        if before_count_row is None or before_count_row["n"] == 0:
            raise HTTPException(400, "No utterances before the split point — nothing to split")

        shift = float(after_bounds["min_s"])
        new_started_at = original_started_at + timedelta(seconds=shift)
        new_ended_at = original_started_at + timedelta(seconds=float(after_bounds["max_e"]))
        new_title = (req.new_title or f"{original_title} (Part 2)").strip()

        new_meeting_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO meetings (id, title, started_at, ended_at, status) "
            "VALUES (?, ?, ?, ?, 'done')",
            (new_meeting_id, new_title, new_started_at.isoformat(), new_ended_at.isoformat()),
        )

        await db.execute(
            "UPDATE utterances SET meeting_id = ?, start_time = start_time - ?, end_time = end_time - ? "
            "WHERE meeting_id = ? AND start_time >= ?",
            (new_meeting_id, shift, shift, meeting_id, req.at),
        )
        await db.execute(
            "UPDATE speaker_enrollment SET meeting_id = ? "
            "WHERE meeting_id = ? AND utterance_id IN "
            "(SELECT id FROM utterances WHERE meeting_id = ?)",
            (new_meeting_id, meeting_id, new_meeting_id),
        )

        cursor = await db.execute(
            "SELECT MAX(end_time) AS max_e FROM utterances WHERE meeting_id = ?",
            (meeting_id,),
        )
        orig_bounds = await cursor.fetchone()
        orig_ended_at = (
            original_started_at + timedelta(seconds=float(orig_bounds["max_e"]))
            if orig_bounds and orig_bounds["max_e"] is not None
            else None
        )
        await db.execute(
            "UPDATE meetings SET ended_at = ?, summary = NULL, action_items = NULL WHERE id = ?",
            (orig_ended_at.isoformat() if orig_ended_at else None, meeting_id),
        )
        await db.commit()

    await _rewrite_vault(meeting_id)
    await _rewrite_vault(new_meeting_id)
    return {"ok": True, "new_meeting_id": new_meeting_id}


@app.get("/api/meetings/{meeting_id}/transcript")
async def get_transcript(meeting_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, speaker, text, start_time, end_time FROM utterances "
            "WHERE meeting_id = ? ORDER BY start_time",
            (meeting_id,),
        )
        utterances = [dict(u) async for u in cursor]
    return {"meeting_id": meeting_id, "utterances": utterances}


# ── Status ───────────────────────────────────────────────────────────────────


@app.get("/api/status")
async def get_status() -> dict:
    return {
        "ok": True,
        "version": __version__,
        "engine_ready": manager.is_ready,
        "is_recording": manager.is_recording,
        "current_meeting_id": manager.current_meeting_id,
        "audio_devices": manager.list_audio_devices(),
    }


@app.get("/api/models")
async def list_models() -> dict:
    return {"models": await get_available_models()}


# ── People + enrollment ──────────────────────────────────────────────────────


@app.get("/api/people")
async def list_people() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, name, vault_path, created_at FROM people ORDER BY name"
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


class EnrollRequest(BaseModel):
    name: str
    duration: float = 10.0


@app.post("/api/enroll/start")
async def enroll_start(req: EnrollRequest) -> dict:
    try:
        from aurascribe.audio.enrollment import record_enrollment_sample, save_enrollment
    except ImportError as e:
        raise HTTPException(
            503, f"Enrollment requires the [diarization] extra. Install with: pip install -e .\\sidecar[diarization]. ({e})"
        )
    await _broadcast(
        {"type": "status", "event": "enrolling", "message": f"Recording {req.duration}s sample for {req.name}..."}
    )
    try:
        audio = await record_enrollment_sample(req.duration)
        person_id = await save_enrollment(req.name, audio)
    except Exception as e:
        # Clear the "enrolling" header status on the way out.
        await _broadcast({"type": "status", "event": "ready", "message": ""})
        msg = str(e)
        if "401" in msg or "Unauthorized" in msg or "gated" in msg.lower() or "GatedRepo" in msg:
            raise HTTPException(
                503,
                "pyannote/embedding could not be downloaded. Set HF_TOKEN in .env to a real "
                "token and accept the license at https://hf.co/pyannote/embedding. Then restart the app.",
            )
        raise HTTPException(500, f"Enrollment failed: {msg}")
    await manager.engine.reload_enrolled()
    # Counterpart to the "enrolling" broadcast above — unsticks the header.
    await _broadcast({"type": "status", "event": "ready", "message": ""})
    return {"person_id": person_id, "name": req.name}


class RenameSpeakerRequest(BaseModel):
    meeting_id: str
    old_name: str
    new_name: str


@app.post("/api/meetings/{meeting_id}/rename-speaker")
async def rename_speaker(meeting_id: str, req: RenameSpeakerRequest) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE utterances SET speaker = ? WHERE meeting_id = ? AND speaker = ?",
            (req.new_name, meeting_id, req.old_name),
        )
        await db.execute(
            "UPDATE people SET name = ? WHERE name = ?",
            (req.new_name, req.old_name),
        )
        await db.commit()
    await manager.engine.reload_enrolled()
    await _rewrite_vault(meeting_id)
    return {"ok": True}


class AssignUtteranceSpeakerRequest(BaseModel):
    speaker: str  # "" or "Unknown" clears the tag + removes learning
    create_if_new: bool = True


@app.post("/api/meetings/{meeting_id}/utterances/{utterance_id}/assign")
async def assign_utterance_speaker(
    meeting_id: str, utterance_id: str, req: AssignUtteranceSpeakerRequest
) -> dict:
    """Assign (or re-assign, or clear) the speaker for one utterance.

    Side-effect: folds this utterance's embedding into the speaker's pool
    so the matcher improves online. Re-tagging first removes the prior
    learning tied to this utterance — mistakes are fully undoable.
    """
    new_speaker = req.speaker.strip()
    is_clear = not new_speaker or new_speaker.lower() == "unknown"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT embedding FROM utterances WHERE id = ? AND meeting_id = ?",
            (utterance_id, meeting_id),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Utterance not found")
        embedding = row["embedding"]

        # Undo any prior learning from this utterance before applying the
        # new one — guarantees re-tagging mistakes is lossless.
        await db.execute(
            "DELETE FROM speaker_enrollment WHERE utterance_id = ?",
            (utterance_id,),
        )

        if is_clear:
            await db.execute(
                "UPDATE utterances SET speaker = 'Unknown' WHERE id = ?",
                (utterance_id,),
            )
        else:
            cursor = await db.execute("SELECT id FROM people WHERE name = ?", (new_speaker,))
            person_row = await cursor.fetchone()
            if person_row is None:
                if not req.create_if_new:
                    raise HTTPException(
                        400, f"Speaker '{new_speaker}' not found and create_if_new=false"
                    )
                person_id = str(uuid.uuid4())
                await db.execute(
                    "INSERT INTO people (id, name, created_at) VALUES (?, ?, ?)",
                    (person_id, new_speaker, datetime.now().isoformat()),
                )
            else:
                person_id = str(person_row["id"])

            await db.execute(
                "UPDATE utterances SET speaker = ? WHERE id = ?",
                (new_speaker, utterance_id),
            )
            if embedding is not None:
                enrollment_id = str(uuid.uuid4())
                await db.execute(
                    "INSERT INTO speaker_enrollment (id, person_id, embedding, utterance_id, meeting_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (enrollment_id, person_id, embedding, utterance_id, meeting_id, datetime.now().isoformat()),
                )
        await db.commit()

    await manager.engine.reload_enrolled()
    await _rewrite_vault(meeting_id)
    return {"ok": True, "speaker": "Unknown" if is_clear else new_speaker}
