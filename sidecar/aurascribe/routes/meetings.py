"""Meeting endpoints — recording lifecycle, CRUD, edit operations,
speaker tagging, and full-meeting recompute.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from aurascribe.config import AUDIO_DIR, DB_PATH
from aurascribe.llm.analysis import is_placeholder_title
from aurascribe.llm.client import LLMUnavailableError
from aurascribe.routes._shared import (
    PROVISIONAL_LABEL_RE,
    bump_meeting_tag,
    delete_audio_files,
    delete_vault_files,
    fetch_meeting_row,
    get_or_create_voice,
    manager,
    normalize_meeting_row,
    persist_summary,
    rename_with_vault_move,
    rewrite_vault,
    run_analysis,
)

router = APIRouter(prefix="/api/meetings")


# ── Recording lifecycle ─────────────────────────────────────────────────────


class StartMeetingRequest(BaseModel):
    title: str = ""
    device: int | None = None


@router.post("/start")
async def start_meeting(req: StartMeetingRequest) -> dict:
    try:
        meeting_id = await manager.start_meeting(title=req.title, device=req.device)
        return {"meeting_id": meeting_id, "status": "recording"}
    except RuntimeError as e:
        raise HTTPException(400, str(e))


class StopMeetingRequest(BaseModel):
    summarize: bool = False


@router.post("/stop")
async def stop_meeting(req: StopMeetingRequest = StopMeetingRequest()) -> dict:
    try:
        return await manager.stop_meeting(summarize=req.summarize)
    except RuntimeError as e:
        raise HTTPException(400, str(e))


# ── List / bulk delete / clear ──────────────────────────────────────────────


@router.get("")
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


@router.post("/bulk-delete")
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
    delete_audio_files(req.ids)
    delete_vault_files(vault_paths)
    return {"ok": True, "deleted": len(req.ids)}


@router.delete("/all")
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
    delete_audio_files(ids)
    delete_vault_files(vault_paths)
    return {"ok": True, "deleted": len(ids)}


# ── Single-meeting CRUD ─────────────────────────────────────────────────────


@router.get("/{meeting_id}")
async def get_meeting(meeting_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Meeting not found")
        meeting = normalize_meeting_row(dict(row))

        cursor = await db.execute(
            "SELECT id, speaker, text, start_time, end_time, match_distance, audio_start FROM utterances "
            "WHERE meeting_id = ? ORDER BY start_time",
            (meeting_id,),
        )
        utterances = [dict(u) async for u in cursor]

    meeting["utterances"] = utterances
    return meeting


@router.delete("/{meeting_id}")
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
    delete_audio_files([meeting_id])
    delete_vault_files([vault_path])
    return {"ok": True}


class RenameMeetingRequest(BaseModel):
    title: str


@router.patch("/{meeting_id}")
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
        old_file = Path(old_vault_path)
        if old_file.exists():
            old_file.unlink()
    await rewrite_vault(meeting_id)
    return {"ok": True}


# ── Summary / title suggestion ──────────────────────────────────────────────


@router.post("/{meeting_id}/summarize")
async def summarize_meeting(meeting_id: str) -> dict:
    """Generate/refresh the AI summary.

    Piggyback: since the combined LLM call also produces title
    suggestions, if the meeting still carries a placeholder title
    (e.g. "Transcription <timestamp>") we auto-apply the top suggestion
    as the new title, updating the Obsidian file to match. A user-
    supplied title is never overwritten.
    """
    result, current_title, current_vault_path = await run_analysis(meeting_id)

    if not result.summary_markdown:
        raise HTTPException(
            502,
            "The LLM replied but the summary section was missing or malformed. "
            "Check the sidecar log for the raw output, then try again.",
        )
    await persist_summary(meeting_id, result.summary_markdown)

    # Free auto-rename — only applies when the user hasn't typed their
    # own title yet, and only when we actually got a usable suggestion.
    if is_placeholder_title(current_title) and result.titles:
        await rename_with_vault_move(meeting_id, result.titles[0], current_vault_path)

    await rewrite_vault(meeting_id)
    return await fetch_meeting_row(meeting_id)


@router.post("/{meeting_id}/suggest-title")
async def suggest_meeting_title(meeting_id: str) -> dict:
    """Return 3 AI-generated title candidates.

    Piggyback: the underlying call also yields a summary, so we persist
    it here too — one click in the UI refreshes both artefacts. The
    frontend applies the user's chosen title via PATCH so rename/vault
    sync stays in one code path.
    """
    result, _current_title, _old_vault = await run_analysis(meeting_id)

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
        await persist_summary(meeting_id, result.summary_markdown)
        await rewrite_vault(meeting_id)

    meeting = await fetch_meeting_row(meeting_id)
    return {"suggestions": result.titles, "meeting": meeting}


# ── Trim / split ────────────────────────────────────────────────────────────


class TrimMeetingRequest(BaseModel):
    before: float | None = None  # delete utterances with start_time < before (then rebase to 0)
    after: float | None = None   # delete utterances with start_time > after


@router.post("/{meeting_id}/trim")
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

    await rewrite_vault(meeting_id)
    return {"ok": True, "shifted_by": shift}


class SplitMeetingRequest(BaseModel):
    at: float  # seconds — utterances with start_time >= at move to the new meeting
    new_title: str | None = None


@router.post("/{meeting_id}/split")
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

    await rewrite_vault(meeting_id)
    await rewrite_vault(new_meeting_id)
    return {"ok": True, "new_meeting_id": new_meeting_id}


# ── Transcript / audio ──────────────────────────────────────────────────────


@router.get("/{meeting_id}/transcript")
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


@router.get("/{meeting_id}/audio")
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


# ── Speaker rename / per-utterance assign ───────────────────────────────────
#
# These endpoints are meeting-scoped but manipulate Voices state (creating a
# voice on the fly, folding embeddings into its pool). That's why the voice
# helpers live in _shared rather than inside voices.py.


class RenameSpeakerRequest(BaseModel):
    meeting_id: str
    old_name: str
    new_name: str


@router.post("/{meeting_id}/rename-speaker")
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

    is_provisional = bool(PROVISIONAL_LABEL_RE.match(req.old_name))

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        voice_id = await get_or_create_voice(db, new_name)

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
        await bump_meeting_tag(db, meeting_id)
        await db.commit()

    if is_provisional:
        manager.release_provisional_label(meeting_id, req.old_name)
    await manager.engine.reload_voices()
    await rewrite_vault(meeting_id)
    return {"ok": True, "voice_id": voice_id}


class AssignUtteranceSpeakerRequest(BaseModel):
    speaker: str  # "" or "Unknown" clears the tag + removes learning
    create_if_new: bool = True


@router.post("/{meeting_id}/utterances/{utterance_id}/assign")
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
                voice_id = await get_or_create_voice(db, new_speaker)
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
        await bump_meeting_tag(db, meeting_id)
        await db.commit()

    await manager.engine.reload_voices()
    await rewrite_vault(meeting_id)
    return {"ok": True, "speaker": "Unknown" if is_clear else new_speaker}


# ── Real-time intelligence refresh ──────────────────────────────────────────


@router.post("/{meeting_id}/intel/refresh")
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


# ── Recompute (apply current Voices DB to a past meeting) ───────────────────


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


class _RecomputeSkipped(Exception):
    """Raised by _do_recompute when the meeting can't be recomputed — still
    recording, no audio file, diarization pipeline missing. Carries an HTTP
    status hint so the endpoint can map it."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def _do_recompute(meeting_id: str) -> dict:
    """Core recompute logic. Raises _RecomputeSkipped on precondition
    failures; successful runs return {turns, updated}."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT audio_path, status FROM meetings WHERE id = ?", (meeting_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise _RecomputeSkipped(404, "Meeting not found")
        if row["status"] == "recording":
            raise _RecomputeSkipped(400, "Cannot recompute a meeting that is still recording")
        audio_path_s = row["audio_path"]

    candidates = [Path(audio_path_s)] if audio_path_s else []
    candidates.append(AUDIO_DIR / f"{meeting_id}.opus")
    audio_path = next((p for p in candidates if p.exists()), None)
    if audio_path is None:
        raise _RecomputeSkipped(400, "No audio recording found for this meeting")

    loop = asyncio.get_running_loop()
    try:
        audio = await loop.run_in_executor(None, _load_meeting_audio_sync, audio_path)
    except Exception as e:
        raise _RecomputeSkipped(500, f"Could not decode audio: {e}")

    try:
        turns = await manager.engine.diarize_full_audio(audio)
    except RuntimeError as e:
        raise _RecomputeSkipped(503, str(e))

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

    await rewrite_vault(meeting_id)
    return {"turns": len(turns), "updated": updated}


@router.post("/{meeting_id}/recompute")
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
    except _RecomputeSkipped as e:
        raise HTTPException(e.status, e.detail)
    return {"ok": True, **result}
