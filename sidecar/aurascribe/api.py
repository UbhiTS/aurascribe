"""FastAPI app — REST + WebSocket.

Full route surface ported from the legacy backend. The SPA-mount routes are
gone: the Tauri WebView loads the frontend directly, so the sidecar only
serves JSON + WebSocket.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import aiosqlite
import numpy as np
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from aurascribe import __version__
from aurascribe import config
from aurascribe.config import AUDIO_DIR, DB_PATH
from aurascribe.db.database import init_db
from aurascribe.llm import daily_brief as daily_brief_mod
from aurascribe.llm.client import LLMUnavailableError, get_available_models
from aurascribe.llm.analysis import (
    AnalysisEmptyError,
    AnalysisResult,
    analyze_meeting,
    is_placeholder_title,
)
from aurascribe.llm.prompts import format_transcript
from aurascribe.meeting_manager import MeetingManager
from aurascribe.obsidian.writer import cleanup_vault_stragglers, rewrite_meeting_vault
from aurascribe.transcription import Utterance

manager = MeetingManager()
ws_clients: list[WebSocket] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    cleanup_vault_stragglers()

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
                        "match_distance": u.match_distance,
                        "audio_start": u.audio_start,
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
        # Meeting finished → the Daily Brief for that meeting's date is now
        # stale. Mark + regenerate in the background so the user's next
        # visit to the Daily Briefs page is already fresh.
        if event == "done" and data.get("meeting_id"):
            asyncio.create_task(_regen_brief_for_meeting(data["meeting_id"]))

    manager.on_utterance(on_utterance)
    manager.on_partial(on_partial)
    manager.on_status(on_status)
    manager.intel.set_broadcast(_broadcast)
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


def _delete_audio_files(meeting_ids: list[str]) -> None:
    """Remove the .opus recording for each id. Best-effort — the meeting rows
    are already gone, so a lingering file would just waste disk."""
    for mid in meeting_ids:
        p = AUDIO_DIR / f"{mid}.opus"
        if p.exists():
            try:
                p.unlink()
            except Exception as e:
                import logging as _log
                _log.getLogger("aurascribe").warning(
                    "could not delete audio file %s: %s", p, e
                )


def _delete_vault_files(vault_paths: list[str | None]) -> None:
    """Remove the Obsidian markdown files for the given meetings.

    Pass absolute paths as stored in `meetings.vault_path`. Entries that
    are None/empty or point at a non-existent file are silently skipped
    — meetings that pre-date vault configuration or were never written
    simply have nothing to remove. Like `_delete_audio_files`, this is
    best-effort: the DB row is already gone, so the worst case is a
    stale file that the vault-straggler cleanup on next boot will not
    touch (it only prunes files for meeting IDs that still exist)."""
    import logging as _log
    log = _log.getLogger("aurascribe")
    for vp in vault_paths:
        if not vp:
            continue
        p = Path(vp)
        if not p.exists():
            continue
        try:
            p.unlink()
        except Exception as e:
            log.warning("could not delete vault file %s: %s", p, e)


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
            "SELECT id, title, started_at, ended_at, status, vault_path, "
            "       last_tagged_at, last_recomputed_at "
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
        # Collect vault_paths before the DELETE — same reason as the
        # single-delete path: the pointers die with the rows.
        cursor = await db.execute(
            f"SELECT vault_path FROM meetings WHERE id IN ({placeholders})", req.ids
        )
        vault_paths = [row[0] async for row in cursor]
        await db.execute(f"DELETE FROM utterances WHERE meeting_id IN ({placeholders})", req.ids)
        await db.execute(f"DELETE FROM meetings WHERE id IN ({placeholders})", req.ids)
        await db.commit()
    _delete_audio_files(req.ids)
    _delete_vault_files(vault_paths)
    return {"ok": True, "deleted": len(req.ids)}


@app.delete("/api/meetings/all")
async def clear_all_meetings(days: int = 2) -> dict:
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    vault_paths: list[str | None] = []
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, vault_path FROM meetings WHERE started_at >= ?", (cutoff,)
        )
        rows = [row async for row in cursor]
        ids = [r[0] for r in rows]
        vault_paths = [r[1] for r in rows]
        if ids:
            placeholders = ",".join("?" * len(ids))
            await db.execute(f"DELETE FROM utterances WHERE meeting_id IN ({placeholders})", ids)
            await db.execute(f"DELETE FROM meetings WHERE id IN ({placeholders})", ids)
            await db.commit()
    _delete_audio_files(ids)
    _delete_vault_files(vault_paths)
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
            "SELECT id, speaker, text, start_time, end_time, match_distance, audio_start FROM utterances "
            "WHERE meeting_id = ? ORDER BY start_time",
            (meeting_id,),
        )
        utterances = [dict(u) async for u in cursor]

    meeting["utterances"] = utterances
    return meeting


async def _rewrite_vault(meeting_id: str) -> None:
    """Wrapper kept for the existing call sites — delegates to the shared
    helper in obsidian.writer. The helper also handles the live-intel
    section that the realtime loop accumulates."""
    await rewrite_meeting_vault(meeting_id)


@app.delete("/api/meetings/{meeting_id}")
async def delete_meeting(meeting_id: str) -> dict:
    # Read vault_path before the DELETE so we can unlink the markdown
    # file after the DB row is gone. Must read first — once the row is
    # deleted, we've lost the pointer.
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT vault_path FROM meetings WHERE id = ?", (meeting_id,)
        )
        row = await cursor.fetchone()
        vault_path = row[0] if row else None
        await db.execute("DELETE FROM utterances WHERE meeting_id = ?", (meeting_id,))
        await db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        await db.commit()
    _delete_audio_files([meeting_id])
    _delete_vault_files([vault_path])
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


async def _run_analysis(meeting_id: str) -> tuple[AnalysisResult, str | None, str | None]:
    """Shared body for the two analysis-driven endpoints.

    Loads the meeting + utterances, runs the combined title+summary LLM
    call, and returns the parsed result alongside the fields the caller
    needs for its follow-up work: the current title (for placeholder
    detection) and the current vault_path (for auto-rename file moves).

    Raises HTTPException for the cases both endpoints share:
      404 — no such meeting
      400 — no transcript to analyze
      503 — LLM provider unreachable
      502 — LLM returned empty (reasoning burn) or non-JSON
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT title, vault_path FROM meetings WHERE id = ?", (meeting_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Meeting not found")
        current_title = row["title"]
        current_vault_path = row["vault_path"]
        cursor = await db.execute(
            "SELECT id, speaker, text, start_time, end_time FROM utterances "
            "WHERE meeting_id = ? ORDER BY start_time",
            (meeting_id,),
        )
        utt_rows = await cursor.fetchall()

    if not utt_rows:
        raise HTTPException(400, "No transcript available to analyze")

    utterances = [
        Utterance(speaker=r["speaker"], text=r["text"], start=r["start_time"], end=r["end_time"])
        for r in utt_rows
    ]
    transcript = format_transcript(utterances)

    try:
        result = await analyze_meeting(
            transcript=transcript,
            current_title=current_title,
        )
    except LLMUnavailableError as e:
        raise HTTPException(503, str(e))
    except AnalysisEmptyError:
        raise HTTPException(
            502,
            "The LLM returned no content. Most likely your model "
            "(reasoning model?) burned its whole output budget on internal "
            "thinking before producing JSON. Fix: in Settings, raise "
            "`llm_context_tokens` (try 16384+), or switch `llm_model` to a "
            "non-reasoning model. Then hit Try again.",
        )
    return result, current_title, current_vault_path


async def _persist_summary(meeting_id: str, summary_md: str) -> None:
    """Write summary + extracted action_items to the meeting row."""
    action_items = manager._extract_action_items(summary_md)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE meetings SET summary = ?, action_items = ? WHERE id = ?",
            (summary_md, json.dumps(action_items) if action_items else None, meeting_id),
        )
        await db.commit()


async def _rename_with_vault_move(
    meeting_id: str, new_title: str, old_vault_path: str | None
) -> None:
    """Apply a rename and move the Obsidian file to match — same machinery
    the /rename endpoint uses, factored out so the auto-rename path
    doesn't duplicate it."""
    new_title = new_title.strip()
    if not new_title:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE meetings SET title = ? WHERE id = ?", (new_title, meeting_id)
        )
        await db.commit()
    if old_vault_path:
        old_file = Path(old_vault_path)
        if old_file.exists():
            try:
                old_file.unlink()
            except OSError:
                pass  # best-effort; _rewrite_vault below creates the new one


async def _fetch_meeting_row(meeting_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
        row = await cursor.fetchone()
    return dict(row) if row else {}


@app.post("/api/meetings/{meeting_id}/summarize")
async def summarize_meeting(meeting_id: str) -> dict:
    """Generate/refresh the AI summary.

    Piggyback: since the combined LLM call also produces title
    suggestions, if the meeting still carries a placeholder title
    (e.g. "Transcription <timestamp>") we auto-apply the top suggestion
    as the new title, updating the Obsidian file to match. A user-
    supplied title is never overwritten.
    """
    result, current_title, current_vault_path = await _run_analysis(meeting_id)

    if not result.summary_markdown:
        raise HTTPException(
            502,
            "The LLM replied but the summary section was missing or malformed. "
            "Check the sidecar log for the raw output, then try again.",
        )
    await _persist_summary(meeting_id, result.summary_markdown)

    # Free auto-rename — only applies when the user hasn't typed their
    # own title yet, and only when we actually got a usable suggestion.
    if is_placeholder_title(current_title) and result.titles:
        await _rename_with_vault_move(meeting_id, result.titles[0], current_vault_path)

    await _rewrite_vault(meeting_id)
    return await _fetch_meeting_row(meeting_id)


@app.post("/api/meetings/{meeting_id}/suggest-title")
async def suggest_meeting_title(meeting_id: str) -> dict:
    """Return 3 AI-generated title candidates.

    Piggyback: the underlying call also yields a summary, so we persist
    it here too — one click in the UI refreshes both artefacts. The
    frontend applies the user's chosen title via PATCH so rename/vault
    sync stays in one code path.
    """
    result, _current_title, _old_vault = await _run_analysis(meeting_id)

    if not result.titles:
        raise HTTPException(
            502,
            "The LLM replied but no title candidates were parseable. "
            "Check the sidecar log for the raw output, then hit Try again.",
        )

    # Side-effect: the same response contains a fresh summary. Persist
    # it so the user doesn't have to click AI Summary separately for
    # basically-free output.
    if result.summary_markdown:
        await _persist_summary(meeting_id, result.summary_markdown)
        await _rewrite_vault(meeting_id)

    meeting = await _fetch_meeting_row(meeting_id)
    return {"suggestions": result.titles, "meeting": meeting}


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

    Creates a new meeting; moves utterances (and their voice_embeddings rows)
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
            "UPDATE voice_embeddings SET meeting_id = ? "
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
            "SELECT id, speaker, text, start_time, end_time, match_distance, audio_start FROM utterances "
            "WHERE meeting_id = ? ORDER BY start_time",
            (meeting_id,),
        )
        utterances = [dict(u) async for u in cursor]
    return {"meeting_id": meeting_id, "utterances": utterances}


@app.get("/api/meetings/{meeting_id}/audio")
async def get_meeting_audio(meeting_id: str) -> FileResponse:
    """Stream the meeting's Opus recording. Starlette's FileResponse
    handles HTTP Range natively, which the browser's <audio> element uses
    to seek — so `audio.currentTime = X` Just Works without any server
    awareness of the requested offset."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT audio_path FROM meetings WHERE id = ?", (meeting_id,)
        )
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Meeting not found")
    audio_path_s = row[0]
    # Fall back to the canonical location even if the DB row is missing
    # an audio_path (e.g. the UPDATE raced a crash). The file is the truth.
    candidates = [Path(audio_path_s)] if audio_path_s else []
    candidates.append(AUDIO_DIR / f"{meeting_id}.opus")
    for p in candidates:
        if p.exists():
            return FileResponse(
                str(p),
                media_type="audio/ogg",
                filename=f"{meeting_id}.opus",
            )
    raise HTTPException(404, "No audio recorded for this meeting")


# ── Settings ─────────────────────────────────────────────────────────────────


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


@app.get("/api/settings/data-dir")
async def get_settings_data_dir() -> dict:
    return _data_dir_response()


@app.put("/api/settings/data-dir")
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


# Declarative spec for every editable config field. Ordering here is purely
# for developer convenience — the UI picks its own groupings via the key
# names. Each tuple is (key, default_for_display).
_CONFIG_FIELDS: list[tuple[str, object]] = [
    ("hf_token",                       None),
    ("my_speaker_label",               "Me"),
    ("llm_base_url",                   "http://127.0.0.1:1234/v1"),
    ("llm_api_key",                    "lm-studio"),
    ("llm_model",                      "local-model"),
    ("llm_context_tokens",             4096),
    ("whisper_model",                  "large-v3-turbo"),
    ("whisper_language",               "en"),
    ("obsidian_vault",                 None),
    ("rt_highlights_debounce_sec",     20.0),
    ("rt_highlights_max_interval_sec", 60.0),
    ("rt_highlights_window_sec",       180.0),
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
        "whisper_language":               config.WHISPER_LANGUAGE,
        "obsidian_vault":                 str(config.OBSIDIAN_VAULT) if config.OBSIDIAN_VAULT else None,
        "rt_highlights_debounce_sec":     config.RT_HIGHLIGHTS_DEBOUNCE_SEC,
        "rt_highlights_max_interval_sec": config.RT_HIGHLIGHTS_MAX_INTERVAL_SEC,
        "rt_highlights_window_sec":       config.RT_HIGHLIGHTS_WINDOW_SEC,
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
    whisper_language: str | None = None
    obsidian_vault: str | None = None
    rt_highlights_debounce_sec: float | None = None
    rt_highlights_max_interval_sec: float | None = None
    rt_highlights_window_sec: float | None = None


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


@app.get("/api/settings/config")
async def get_settings_config() -> dict:
    return _config_response()


@app.put("/api/settings/config")
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
        "active_audio_device": manager.active_device_name,
        # True iff config.obsidian_vault is set. Lets the header show the
        # vault state without waiting for a vault_path to be stamped on a
        # meeting (which only happens after the first markdown write).
        "obsidian_configured": config.OBSIDIAN_VAULT is not None,
    }


@app.get("/api/models")
async def list_models() -> dict:
    return {"models": await get_available_models()}


@app.post("/api/meetings/{meeting_id}/intel/refresh")
async def refresh_intel(meeting_id: str) -> dict:
    """Force a realtime-intelligence run now, bypassing debounce. No-op if
    the meeting isn't currently active in the manager."""
    if manager.current_meeting_id != meeting_id:
        raise HTTPException(400, "Meeting is not currently recording")
    try:
        await manager.intel.trigger_now(meeting_id)
    except LLMUnavailableError as e:
        raise HTTPException(503, str(e))
    return {"ok": True}


@app.get("/api/intel/prompt-path")
async def intel_prompt_path() -> dict:
    """Return the absolute path of the user-editable realtime-intelligence
    prompt file. Lets the UI surface 'edit me' affordances."""
    from aurascribe.llm.realtime import _ensure_prompt_file

    return {"path": str(_ensure_prompt_file())}


# ── Daily Brief ──────────────────────────────────────────────────────────────


async def _regen_brief_for_meeting(meeting_id: str) -> None:
    """Find the date a meeting belongs to, mark that day's brief stale, and
    rebuild it. Broadcasts `daily_brief_updated` on completion so the UI
    refetches automatically. All failures are swallowed — this is a
    best-effort background task."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT started_at FROM meetings WHERE id = ?", (meeting_id,)
            )
            row = await cursor.fetchone()
        if row is None or not row["started_at"]:
            return
        brief_date = daily_brief_mod.date_of_iso(row["started_at"])
    except Exception as e:
        import logging as _log
        _log.getLogger("aurascribe").warning(
            "daily_brief: could not resolve date for meeting %s: %s", meeting_id, e
        )
        return

    await daily_brief_mod.mark_stale(brief_date)
    await _broadcast({
        "type": "daily_brief_updated",
        "date": brief_date,
        "status": "refreshing",
    })
    try:
        result = await daily_brief_mod.build_brief(brief_date)
    except LLMUnavailableError as e:
        import logging as _log
        _log.getLogger("aurascribe").info(
            "daily_brief: LLM unavailable while regenerating %s: %s", brief_date, e
        )
        await _broadcast({
            "type": "daily_brief_updated",
            "date": brief_date,
            "status": "stale",
        })
        return
    except Exception as e:
        import logging as _log
        _log.getLogger("aurascribe").warning(
            "daily_brief: regen failed for %s: %s", brief_date, e, exc_info=True
        )
        await _broadcast({
            "type": "daily_brief_updated",
            "date": brief_date,
            "status": "stale",
        })
        return
    await _broadcast({
        "type": "daily_brief_updated",
        "date": brief_date,
        "status": "ready",
        "generated_at": result.get("generated_at"),
    })


@app.get("/api/daily-brief")
async def get_daily_brief(date_param: str | None = Query(None, alias="date")) -> dict:
    """Return the cached brief for `date` (defaults to today). Fast — does
    NOT call the LLM. If the brief is missing or marked stale, the UI can
    trigger `/api/daily-brief/refresh` to rebuild it."""
    brief_date = date_param or daily_brief_mod.today_str()
    try:
        date.fromisoformat(brief_date)
    except ValueError:
        raise HTTPException(400, f"Invalid date (expected YYYY-MM-DD): {brief_date}")

    cached = await daily_brief_mod.get_cached(brief_date)
    # Still report the meeting count for the date even if no brief exists yet
    # — lets the UI say "2 meetings on this day, tap refresh to build brief".
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, title, started_at, ended_at, status FROM meetings "
            "WHERE started_at >= ? AND started_at < date(?, '+1 day') "
            "ORDER BY started_at",
            (brief_date, brief_date),
        )
        meetings = [dict(r) for r in await cursor.fetchall()]

    return {
        "date": brief_date,
        "brief": cached.get("brief") if cached else None,
        "meeting_count": len(meetings),
        "meeting_ids": cached.get("meeting_ids", []) if cached else [],
        "meetings": meetings,
        "generated_at": cached.get("generated_at") if cached else None,
        "is_stale": cached.get("is_stale", True) if cached else True,
        "exists": cached is not None and cached.get("brief") is not None,
    }


@app.post("/api/daily-brief/refresh")
async def refresh_daily_brief(date_param: str | None = Query(None, alias="date")) -> dict:
    """Force regeneration of the brief for `date`. Blocks until complete.
    Broadcasts `daily_brief_updated` on the way out so any other clients
    refresh too."""
    brief_date = date_param or daily_brief_mod.today_str()
    try:
        date.fromisoformat(brief_date)
    except ValueError:
        raise HTTPException(400, f"Invalid date (expected YYYY-MM-DD): {brief_date}")

    await _broadcast({
        "type": "daily_brief_updated",
        "date": brief_date,
        "status": "refreshing",
    })
    try:
        result = await daily_brief_mod.build_brief(brief_date)
    except LLMUnavailableError as e:
        await _broadcast({
            "type": "daily_brief_updated",
            "date": brief_date,
            "status": "stale",
        })
        raise HTTPException(503, str(e))

    await _broadcast({
        "type": "daily_brief_updated",
        "date": brief_date,
        "status": "ready",
        "generated_at": result.get("generated_at"),
    })
    return {
        "date": brief_date,
        "brief": result["brief"],
        "meeting_count": result["meeting_count"],
        "meeting_ids": result["meeting_ids"],
        "generated_at": result["generated_at"],
        "is_stale": False,
        "exists": True,
    }


# Friendly display names for the prompt files we know about. Anything else in
# PROMPTS_DIR is shown with its raw filename so the user can still find it.
_PROMPT_LABELS = {
    "live_intelligence.md": "Live Intelligence",
    "daily_brief.md": "Daily Brief",
}


@app.get("/api/intel/prompts")
async def list_prompts() -> dict:
    """Enumerate every .md file under the user's prompts dir (APP_DATA/prompts).
    Known prompts are seeded on first run; the user can also drop extra files
    here. Edits are picked up on the next LLM call — no restart needed."""
    items: list[dict] = []
    for path in sorted(config.PROMPTS_DIR.glob("*.md")):
        items.append({
            "name": _PROMPT_LABELS.get(path.name, path.name),
            "filename": path.name,
            "path": str(path),
        })
    return {"dir": str(config.PROMPTS_DIR), "prompts": items}


class OpenPromptRequest(BaseModel):
    filename: str


@app.post("/api/intel/open-prompt")
async def open_prompt(req: OpenPromptRequest) -> dict:
    """Open a prompt file in the user's default editor.

    Sidesteps tauri-plugin-shell's URL-only `open` scope. The filename is
    validated against the prompts dir (basename only — no path traversal) so
    this endpoint can't be coaxed into opening arbitrary files."""
    # Reject anything that isn't a bare basename — guards against ../ etc.
    safe_name = Path(req.filename).name
    if safe_name != req.filename or not safe_name:
        raise HTTPException(400, "Invalid filename")
    target = config.PROMPTS_DIR / safe_name
    if not target.exists() or not target.is_file():
        raise HTTPException(404, f"Prompt file not found: {safe_name}")

    abs_target = str(target.resolve())
    import logging as _log
    _log.getLogger("aurascribe").info("open-prompt: dispatching to OS shell: %r", abs_target)

    try:
        if sys.platform == "win32":
            # `cmd /c start "" "<path>"` is the canonical Windows pattern for
            # "open with default handler". The empty title arg ("") is
            # required because `start` interprets the first quoted argument
            # as a window title — without it, our path becomes the title and
            # the actual file arg is missing. More reliable than os.startfile,
            # which has had path-resolution quirks on certain Windows builds.
            subprocess.Popen(
                ["cmd", "/c", "start", "", abs_target],
                shell=False,
                close_fds=True,
            )
        elif sys.platform == "darwin":
            subprocess.Popen(["open", abs_target])
        else:
            subprocess.Popen(["xdg-open", abs_target])
    except Exception as e:
        raise HTTPException(500, f"Failed to open file: {e}")
    return {"ok": True, "path": abs_target}


# ── Voices ───────────────────────────────────────────────────────────────────
#
# Voices replace the old enroll-first model: speakers are identified over time
# by tagging utterances in meetings. Each tag folds that utterance's centroid
# embedding into the Voice's pool, so matching improves with every tag.
# Unknown clusters in a live meeting surface as provisional "Speaker N" —
# tagging bulk-assigns every pill in that cluster to the chosen Voice.

_PROVISIONAL_LABEL_RE = re.compile(r"^Speaker \d+$")

# Palette for auto-assigning a Voice color on creation. Cycles by creation
# order so the first N voices each get a distinct hue.
_VOICE_COLORS = [
    "#a78bfa",  # purple
    "#34d399",  # emerald
    "#22d3ee",  # cyan
    "#fbbf24",  # amber
    "#f472b6",  # pink
    "#fb7185",  # rose
    "#2dd4bf",  # teal
    "#818cf8",  # indigo
]


async def _next_voice_color(db: aiosqlite.Connection) -> str:
    cursor = await db.execute("SELECT COUNT(*) FROM voices")
    row = await cursor.fetchone()
    n = int(row[0]) if row and row[0] is not None else 0
    return _VOICE_COLORS[n % len(_VOICE_COLORS)]


async def _bump_meeting_tag(db: aiosqlite.Connection, meeting_id: str) -> None:
    """Mark this meeting as having had a label change, so the UI can show
    a 'Tags pending — Recompute to apply' indicator."""
    await db.execute(
        "UPDATE meetings SET last_tagged_at = ? WHERE id = ?",
        (datetime.now().isoformat(), meeting_id),
    )


async def _bump_meetings_for_voice(
    db: aiosqlite.Connection, voice_id: str
) -> None:
    """Same idea, but for voice-level changes (rename / delete / merge)
    that can affect every meeting where this voice was tagged. Bumps each
    affected meeting in one statement."""
    await db.execute(
        "UPDATE meetings SET last_tagged_at = ? "
        "WHERE id IN (SELECT DISTINCT meeting_id FROM voice_embeddings "
        "             WHERE voice_id = ? AND meeting_id IS NOT NULL)",
        (datetime.now().isoformat(), voice_id),
    )


async def _get_or_create_voice(
    db: aiosqlite.Connection, name: str
) -> str:
    """Return voice_id for `name`, creating the row with a fresh color if new.
    Works whether or not the caller has set a row_factory — positional [0]
    access is supported by both tuples and aiosqlite.Row."""
    cursor = await db.execute("SELECT id FROM voices WHERE name = ?", (name,))
    row = await cursor.fetchone()
    if row is not None:
        return str(row[0])
    voice_id = str(uuid.uuid4())
    color = await _next_voice_color(db)
    now = datetime.now().isoformat()
    await db.execute(
        "INSERT INTO voices (id, name, color, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (voice_id, name, color, now, now),
    )
    return voice_id


@app.get("/api/voices")
async def list_voices() -> list[dict]:
    """Every Voice, with aggregate stats. The frontend uses `snippet_count`
    to render the samples-gate indicator (≥3 = active in auto-match)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT v.id, v.name, v.color, v.created_at, v.updated_at,
                   COUNT(ve.id) AS snippet_count,
                   COALESCE(SUM(COALESCE(ve.end_time, 0) - COALESCE(ve.start_time, 0)), 0) AS total_seconds,
                   MAX(ve.created_at) AS last_tagged_at
              FROM voices v
              LEFT JOIN voice_embeddings ve ON ve.voice_id = v.id
             GROUP BY v.id
             ORDER BY v.name
            """
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@app.get("/api/voices/{voice_id}")
async def get_voice(voice_id: str) -> dict:
    """Voice detail + every tagged snippet with enough metadata for the UI
    to play each clip via the existing per-meeting audio endpoint."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, name, color, created_at, updated_at FROM voices WHERE id = ?",
            (voice_id,),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Voice not found")
        voice = dict(row)

        cursor = await db.execute(
            """
            SELECT ve.id, ve.meeting_id, ve.utterance_id, ve.start_time, ve.end_time,
                   ve.source, ve.created_at,
                   m.title AS meeting_title, m.started_at AS meeting_started_at,
                   u.text AS utterance_text, u.audio_start AS audio_start
              FROM voice_embeddings ve
              LEFT JOIN meetings m ON m.id = ve.meeting_id
              LEFT JOIN utterances u ON u.id = ve.utterance_id
             WHERE ve.voice_id = ?
             ORDER BY ve.created_at DESC
            """,
            (voice_id,),
        )
        snippets = [dict(r) for r in await cursor.fetchall()]
    voice["snippets"] = snippets
    voice["snippet_count"] = len(snippets)
    return voice


class VoicePatch(BaseModel):
    name: str | None = None
    color: str | None = None


@app.patch("/api/voices/{voice_id}")
async def update_voice(voice_id: str, req: VoicePatch) -> dict:
    """Rename and/or recolor. Rename cascades into utterances.speaker across
    every meeting so the pills update everywhere. Caller should follow up
    with a rewrite of affected vault files if that matters."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT name FROM voices WHERE id = ?", (voice_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Voice not found")
        old_name = row["name"]

        new_name = req.name.strip() if req.name else None
        if new_name and new_name != old_name:
            # Prevent collision with an existing voice.
            cursor = await db.execute(
                "SELECT id FROM voices WHERE name = ? AND id != ?", (new_name, voice_id)
            )
            if await cursor.fetchone() is not None:
                raise HTTPException(409, f"A voice named '{new_name}' already exists")
            await db.execute(
                "UPDATE voices SET name = ?, updated_at = ? WHERE id = ?",
                (new_name, datetime.now().isoformat(), voice_id),
            )
            await db.execute(
                "UPDATE utterances SET speaker = ? WHERE speaker = ?",
                (new_name, old_name),
            )
            # Renaming a voice changes pill text everywhere it appears.
            # Recompute won't change the labels (text is direct) but we still
            # bump so library cards can flag "labels changed since recompute".
            await _bump_meetings_for_voice(db, voice_id)

        if req.color is not None:
            await db.execute(
                "UPDATE voices SET color = ?, updated_at = ? WHERE id = ?",
                (req.color, datetime.now().isoformat(), voice_id),
            )
        await db.commit()

    await manager.engine.reload_voices()
    return {"ok": True}


@app.delete("/api/voices/{voice_id}")
async def delete_voice(voice_id: str) -> dict:
    """Delete a Voice + all its embeddings. Every utterance previously
    tagged with this voice's name reverts to 'Unknown'."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT name FROM voices WHERE id = ?", (voice_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Voice not found")
        name = row["name"]

        # Capture which meetings will be affected BEFORE the delete cascade
        # nukes voice_embeddings — we need the meeting list for the bump.
        await _bump_meetings_for_voice(db, voice_id)

        # voice_embeddings has ON DELETE CASCADE, but cascade only fires with
        # PRAGMA foreign_keys enabled for this connection — set it explicitly.
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("DELETE FROM voices WHERE id = ?", (voice_id,))
        await db.execute(
            "UPDATE utterances SET speaker = 'Unknown' WHERE speaker = ?",
            (name,),
        )
        await db.commit()

    await manager.engine.reload_voices()
    return {"ok": True}


@app.delete("/api/voices/{voice_id}/snippets/{snippet_id}")
async def delete_voice_snippet(voice_id: str, snippet_id: str) -> dict:
    """Remove one tagged snippet from a Voice's pool. The utterance itself
    keeps its speaker label — only the pool entry goes, so future matches
    rely on the remaining embeddings."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Capture the meeting before the row is gone so we can flag it for
        # recompute. Removing a sample changes the matcher's output for any
        # future recompute of meetings where this voice was tagged.
        cursor = await db.execute(
            "SELECT meeting_id FROM voice_embeddings WHERE id = ? AND voice_id = ?",
            (snippet_id, voice_id),
        )
        row = await cursor.fetchone()
        affected_meeting = row[0] if row else None

        cursor = await db.execute(
            "DELETE FROM voice_embeddings WHERE id = ? AND voice_id = ?",
            (snippet_id, voice_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(404, "Snippet not found")
        if affected_meeting:
            await _bump_meeting_tag(db, affected_meeting)
        await db.commit()

    await manager.engine.reload_voices()
    return {"ok": True}


class VoiceMergeRequest(BaseModel):
    from_id: str
    into_id: str


@app.post("/api/voices/merge")
async def merge_voices(req: VoiceMergeRequest) -> dict:
    """Fold Voice `from_id` into `into_id`: move every embedding over,
    rewrite every utterance.speaker from the old name to the new, then
    delete the source Voice. Can't undo in one click — use snippet-delete
    to back out individual embeddings if needed."""
    if req.from_id == req.into_id:
        raise HTTPException(400, "from_id and into_id must differ")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, name FROM voices WHERE id IN (?, ?)", (req.from_id, req.into_id)
        )
        rows = {r["id"]: r["name"] for r in await cursor.fetchall()}
        if req.from_id not in rows or req.into_id not in rows:
            raise HTTPException(404, "One or both voices not found")
        from_name = rows[req.from_id]
        into_name = rows[req.into_id]

        # Bump every meeting that referenced either side BEFORE the merge —
        # afterwards the from-side is gone and we can't enumerate it.
        await _bump_meetings_for_voice(db, req.from_id)
        await _bump_meetings_for_voice(db, req.into_id)

        await db.execute(
            "UPDATE voice_embeddings SET voice_id = ? WHERE voice_id = ?",
            (req.into_id, req.from_id),
        )
        await db.execute(
            "UPDATE utterances SET speaker = ? WHERE speaker = ?",
            (into_name, from_name),
        )
        await db.execute("DELETE FROM voices WHERE id = ?", (req.from_id,))
        await db.execute(
            "UPDATE voices SET updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(), req.into_id),
        )
        await db.commit()

    await manager.engine.reload_voices()
    return {"ok": True, "merged_into": req.into_id}


class RenameSpeakerRequest(BaseModel):
    meeting_id: str
    old_name: str
    new_name: str


@app.post("/api/meetings/{meeting_id}/rename-speaker")
async def rename_speaker(meeting_id: str, req: RenameSpeakerRequest) -> dict:
    """Bulk-tag every pill in a cluster/name within one meeting to a Voice.

    Two modes:
    - Provisional ("Speaker N") → Voice: create/find the Voice and fold
      every matching utterance's embedding into its pool, then drop the
      provisional label so new chunks match via the Voices matcher.
    - One Voice name → another (existing or new): cascades the rename
      through `utterances.speaker` and creates the target Voice if needed.
    """
    new_name = req.new_name.strip()
    if not new_name:
        raise HTTPException(400, "new_name cannot be empty")

    is_provisional = bool(_PROVISIONAL_LABEL_RE.match(req.old_name))

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        voice_id = await _get_or_create_voice(db, new_name)

        if is_provisional:
            cursor = await db.execute(
                "SELECT id, embedding, start_time, end_time FROM utterances "
                "WHERE meeting_id = ? AND speaker = ?",
                (meeting_id, req.old_name),
            )
            matching = await cursor.fetchall()

            now = datetime.now().isoformat()
            for row in matching:
                if row["embedding"] is None:
                    continue
                await db.execute(
                    "INSERT INTO voice_embeddings "
                    "(id, voice_id, meeting_id, utterance_id, embedding, "
                    " start_time, end_time, source, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'manual', ?)",
                    (
                        str(uuid.uuid4()), voice_id, meeting_id, row["id"],
                        row["embedding"], row["start_time"], row["end_time"], now,
                    ),
                )

        await db.execute(
            "UPDATE utterances SET speaker = ? WHERE meeting_id = ? AND speaker = ?",
            (new_name, meeting_id, req.old_name),
        )
        await db.execute(
            "UPDATE voices SET updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(), voice_id),
        )
        await _bump_meeting_tag(db, meeting_id)
        await db.commit()

    if is_provisional:
        manager.release_provisional_label(meeting_id, req.old_name)
    await manager.engine.reload_voices()
    await _rewrite_vault(meeting_id)
    return {"ok": True, "voice_id": voice_id}


class AssignUtteranceSpeakerRequest(BaseModel):
    speaker: str  # "" or "Unknown" clears the tag + removes learning
    create_if_new: bool = True


@app.post("/api/meetings/{meeting_id}/utterances/{utterance_id}/assign")
async def assign_utterance_speaker(
    meeting_id: str, utterance_id: str, req: AssignUtteranceSpeakerRequest
) -> dict:
    """Tag (or retag, or clear) one utterance.

    Side-effect: folds this utterance's embedding into the Voice's pool.
    Retagging first removes any prior learning tied to this utterance so
    mistakes are fully undoable.
    """
    new_speaker = req.speaker.strip()
    is_clear = not new_speaker or new_speaker.lower() == "unknown"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT embedding, start_time, end_time FROM utterances "
            "WHERE id = ? AND meeting_id = ?",
            (utterance_id, meeting_id),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Utterance not found")
        embedding = row["embedding"]

        # Undo any prior learning from this utterance before applying the
        # new one — guarantees re-tagging mistakes is lossless.
        await db.execute(
            "DELETE FROM voice_embeddings WHERE utterance_id = ?",
            (utterance_id,),
        )

        if is_clear:
            await db.execute(
                "UPDATE utterances SET speaker = 'Unknown' WHERE id = ?",
                (utterance_id,),
            )
        else:
            # Find or create the Voice (always, so a tag with no embedding
            # still registers the name and color).
            cursor = await db.execute(
                "SELECT id FROM voices WHERE name = ?", (new_speaker,)
            )
            voice_row = await cursor.fetchone()
            if voice_row is None:
                if not req.create_if_new:
                    raise HTTPException(
                        400, f"Voice '{new_speaker}' not found and create_if_new=false"
                    )
                voice_id = await _get_or_create_voice(db, new_speaker)
            else:
                voice_id = str(voice_row["id"])

            await db.execute(
                "UPDATE utterances SET speaker = ? WHERE id = ?",
                (new_speaker, utterance_id),
            )
            if embedding is not None:
                await db.execute(
                    "INSERT INTO voice_embeddings "
                    "(id, voice_id, meeting_id, utterance_id, embedding, "
                    " start_time, end_time, source, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'manual', ?)",
                    (
                        str(uuid.uuid4()), voice_id, meeting_id, utterance_id,
                        embedding, row["start_time"], row["end_time"],
                        datetime.now().isoformat(),
                    ),
                )
            await db.execute(
                "UPDATE voices SET updated_at = ? WHERE id = ?",
                (datetime.now().isoformat(), voice_id),
            )
        await _bump_meeting_tag(db, meeting_id)
        await db.commit()

    await manager.engine.reload_voices()
    await _rewrite_vault(meeting_id)
    return {"ok": True, "speaker": "Unknown" if is_clear else new_speaker}


# ── Recompute (apply current Voices DB to a past meeting) ────────────────────


def _load_meeting_audio_sync(path: Path) -> np.ndarray:
    """Decode an .opus file to 16kHz float32 mono for pyannote. Runs in an
    executor — soundfile decoding can take a second on long meetings."""
    import soundfile as sf

    data, sr = sf.read(str(path), dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != 16_000:
        # Meetings recorded by us are already 16kHz, but fall back cleanly
        # if someone drops in a file from elsewhere.
        try:
            from scipy.signal import resample_poly
            g = np.gcd(sr, 16_000)
            data = resample_poly(data, 16_000 // g, sr // g).astype(np.float32)
        except Exception:
            raise RuntimeError(f"Unexpected sample rate {sr} and scipy unavailable")
    return np.ascontiguousarray(data, dtype=np.float32)


class RecomputeSkipped(Exception):
    """Raised by _do_recompute when the meeting can't be recomputed — still
    recording, no audio file, diarization pipeline missing. Carries an HTTP
    status hint so the endpoint can map it; the debounce path just swallows."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def _do_recompute(meeting_id: str) -> dict:
    """Core recompute logic, shared by the explicit endpoint and the
    debounced auto-trigger. Raises RecomputeSkipped on precondition failures;
    successful runs return {turns, updated}."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT audio_path, status FROM meetings WHERE id = ?", (meeting_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise RecomputeSkipped(404, "Meeting not found")
        if row["status"] == "recording":
            raise RecomputeSkipped(400, "Cannot recompute a meeting that is still recording")
        audio_path_s = row["audio_path"]

    candidates = [Path(audio_path_s)] if audio_path_s else []
    candidates.append(AUDIO_DIR / f"{meeting_id}.opus")
    audio_path = next((p for p in candidates if p.exists()), None)
    if audio_path is None:
        raise RecomputeSkipped(400, "No audio recording found for this meeting")

    loop = asyncio.get_running_loop()
    try:
        audio = await loop.run_in_executor(None, _load_meeting_audio_sync, audio_path)
    except Exception as e:
        raise RecomputeSkipped(500, f"Could not decode audio: {e}")

    try:
        turns = await manager.engine.diarize_full_audio(audio)
    except RuntimeError as e:
        raise RecomputeSkipped(503, str(e))

    if not turns:
        return {"turns": 0, "updated": 0}

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, audio_start, start_time, end_time FROM utterances "
            "WHERE meeting_id = ? ORDER BY start_time",
            (meeting_id,),
        )
        utterances = await cursor.fetchall()

        updated = 0
        for u in utterances:
            anchor = u["audio_start"]
            if anchor is None:
                continue
            u_start = float(anchor)
            u_end = u_start + max(0.0, float(u["end_time"]) - float(u["start_time"]))
            best_speaker = "Unknown"
            best_distance: float | None = None
            best_overlap = 0.0
            for t_start, t_end, speaker, distance in turns:
                overlap = max(0.0, min(u_end, t_end) - max(u_start, t_start))
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_speaker = speaker
                    best_distance = distance
            if best_overlap <= 0.0:
                best_speaker = "Unknown"
                best_distance = None
            await db.execute(
                "UPDATE utterances SET speaker = ?, match_distance = ? WHERE id = ?",
                (best_speaker, best_distance, u["id"]),
            )
            updated += 1
        # Stamp the recompute completion so the UI can clear the
        # "Tags pending" badge.
        await db.execute(
            "UPDATE meetings SET last_recomputed_at = ? WHERE id = ?",
            (datetime.now().isoformat(), meeting_id),
        )
        await db.commit()

    await _rewrite_vault(meeting_id)
    return {"turns": len(turns), "updated": updated}


@app.post("/api/meetings/{meeting_id}/recompute")
async def recompute_meeting_speakers(meeting_id: str) -> dict:
    """Re-label a meeting's utterances using the current Voices DB.

    Runs full-meeting diarization (not per-chunk like the live path), matches
    each new turn's centroid against Voices, then rewrites every utterance's
    speaker by picking the turn that covers its audio_start. Text is never
    re-ASRed — only the speaker column changes. Finally rewrites the vault
    file so Obsidian reflects the new tags.
    """
    try:
        result = await _do_recompute(meeting_id)
    except RecomputeSkipped as e:
        raise HTTPException(e.status, e.detail)
    return {"ok": True, **result}


