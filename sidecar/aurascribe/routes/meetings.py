"""Meeting endpoints — recording lifecycle, CRUD, edit operations,
speaker tagging, and full-meeting recompute.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from aurascribe.audio.capture import MicUnavailableError
from aurascribe.audio.ffmpeg import (
    FFmpegFailedError,
    FFmpegMissingError,
    transcode_to_opus,
)
from aurascribe.config import AUDIO_DIR, DB_PATH
from aurascribe.llm.client import LLMTruncatedError, LLMUnavailableError
from aurascribe.routes._shared import (
    PROVISIONAL_LABEL_RE,
    bump_meeting_tag,
    delete_audio_files,
    delete_vault_files,
    fetch_meeting_row,
    find_meeting_audio_file,
    get_or_create_voice,
    manager,
    normalize_meeting_row,
    persist_summary,
    rename_with_vault_move,
    rewrite_vault,
    run_analysis,
    sync_meeting_audio_filename,
)

router = APIRouter(prefix="/api/meetings")


# ── Recording lifecycle ─────────────────────────────────────────────────────


class StartMeetingRequest(BaseModel):
    title: str = ""
    # Mic device to open. Ignored when `capture_mic` is False.
    device: int | None = None
    # Optional WASAPI output-device index — when set, the sidecar opens a
    # second loopback stream on that device. Combined with `capture_mic`
    # this drives the three UX modes:
    #   * mic only      → capture_mic=True,  loopback_device=None
    #   * system only   → capture_mic=False, loopback_device=<idx>
    #   * mic + system  → capture_mic=True,  loopback_device=<idx>
    # In mix mode the sidecar runs AEC (mic near-end / loopback reference)
    # and sums the cleaned mic with the system audio; in system-only mode
    # AEC is skipped since there's no near-end signal to cancel against.
    loopback_device: int | None = None
    # Whether to open the microphone stream at all. Default True keeps
    # back-compat for any client that hasn't been updated.
    capture_mic: bool = True


@router.post("/start")
async def start_meeting(req: StartMeetingRequest) -> dict:
    try:
        meeting_id = await manager.start_meeting(
            title=req.title,
            device=req.device,
            loopback_device=req.loopback_device,
            capture_mic=req.capture_mic,
        )
        return {"meeting_id": meeting_id, "status": "recording"}
    except MicUnavailableError as e:
        # 403 + structured detail so the frontend can show the "Open
        # Windows mic settings" affordance for permission denials, and a
        # generic try-again dialog for everything else.
        raise HTTPException(403, detail={"message": str(e), "kind": e.kind})
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


# ── Monitor mode ────────────────────────────────────────────────────────────
#
# Feeds the idle-state VU meter + waveform from the same capture pipeline a
# real meeting uses (minus ASR / DB). The frontend starts a monitor whenever
# the user lands on a non-mic-only source config; real meeting start tears
# the monitor down automatically, so callers don't have to sequence that.


class MonitorStartRequest(BaseModel):
    device: int | None = None
    loopback_device: int | None = None
    capture_mic: bool = True


@router.post("/monitor/start")
async def monitor_start(req: MonitorStartRequest) -> dict:
    try:
        await manager.start_monitor(
            device=req.device,
            loopback_device=req.loopback_device,
            capture_mic=req.capture_mic,
        )
        return {"ok": True}
    except MicUnavailableError as e:
        raise HTTPException(403, detail={"message": str(e), "kind": e.kind})
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@router.post("/monitor/stop")
async def monitor_stop() -> dict:
    await manager.stop_monitor()
    return {"ok": True}


# ── Audio file import ──────────────────────────────────────────────────────


# Generous upper bound — a 4-hour 24 kbps Opus is ~40 MB, but users may
# import lossless WAV (~700 MB/h). Anything bigger is almost certainly an
# accident; rejecting early avoids streaming gigabytes to disk first.
_IMPORT_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

# All formats we'll accept. ffmpeg can decode far more than this; the
# allow-list just guards against the user dropping random files (PDFs,
# zips, jpegs) into the import dialog.
_IMPORT_EXTENSIONS = (
    ".opus", ".ogg", ".wav", ".flac", ".mp3", ".m4a", ".aac",
    ".wma", ".webm", ".mp4", ".mkv", ".mov",
)


@router.post("/import")
async def import_audio_file(
    file: UploadFile = File(...),
    last_modified_ms: int | None = Form(None),
) -> dict:
    """Import an audio (or video) file as a new completed meeting.

    Pipeline: ffmpeg → 16 kHz mono Opus → meeting_manager.import_audio_file
    → engine.transcribe → finalize. The finalize step writes the Obsidian
    note, runs the AI summary (which may auto-rename the meeting), and
    syncs the on-disk filename to "<uuid> - <title>.opus".

    `last_modified_ms` (epoch milliseconds, taken from the browser's
    File.lastModified on upload) becomes the meeting's `started_at` so
    week-old recordings show up at their natural date in the library +
    Daily Brief instead of clustering on today.
    """
    import tempfile

    if not file.filename:
        raise HTTPException(400, "Missing filename")
    suffix = Path(file.filename).suffix.lower()
    if suffix and suffix not in _IMPORT_EXTENSIONS:
        raise HTTPException(
            400,
            f"Unsupported file extension '{suffix}'. Supported: "
            + ", ".join(_IMPORT_EXTENSIONS),
        )

    # Title defaults to the filename stem; AI summary may refine it later.
    title = (Path(file.filename).stem or "Imported Audio").strip() or "Imported Audio"

    # started_at: prefer the upload's lastModified (so a recording made
    # last Tuesday lands on Tuesday in the library); otherwise "now".
    if last_modified_ms is not None and last_modified_ms > 0:
        try:
            started_at = datetime.fromtimestamp(
                last_modified_ms / 1000.0
            ).isoformat()
        except (OverflowError, OSError, ValueError):
            started_at = datetime.now().isoformat()
    else:
        started_at = datetime.now().isoformat()

    tmp_dir = Path(tempfile.gettempdir())
    upload_token = uuid.uuid4().hex
    tmp_in = tmp_dir / f"aurascribe-import-{upload_token}{suffix or '.bin'}"
    tmp_out = tmp_dir / f"aurascribe-import-{upload_token}.opus"

    bytes_received = 0
    try:
        # Stream upload to disk in 1 MB chunks so we don't buffer the whole
        # file in RAM. Reject early once we exceed the size limit.
        with open(tmp_in, "wb") as out:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                bytes_received += len(chunk)
                if bytes_received > _IMPORT_MAX_BYTES:
                    raise HTTPException(
                        413,
                        f"File exceeds {_IMPORT_MAX_BYTES // (1024 * 1024)} MB limit",
                    )
                out.write(chunk)

        if bytes_received == 0:
            raise HTTPException(400, "Uploaded file is empty")

        # Transcode to canonical Opus. Raises if ffmpeg isn't installed
        # or the input is unreadable.
        try:
            await transcode_to_opus(tmp_in, tmp_out)
        except FFmpegMissingError as e:
            raise HTTPException(503, str(e))
        except FFmpegFailedError as e:
            detail = str(e)
            if e.stderr_tail:
                detail += f" — ffmpeg said: {e.stderr_tail}"
            raise HTTPException(400, detail)

        # Hand off to the manager — it owns the meeting row, transcription,
        # and finalize. import_audio_file moves tmp_out into AUDIO_DIR.
        result = await manager.import_audio_file(
            opus_src=tmp_out,
            title=title,
            started_at=started_at,
            summarize=True,
        )
        return {**result, "started_at": started_at}

    except HTTPException:
        raise
    except RuntimeError as e:
        # Most likely "Models still loading" — surfaced to the UI as a
        # retry-soon message.
        raise HTTPException(503, str(e))
    except Exception as e:
        logging.getLogger("aurascribe").exception(
            "Import failed for %s: %s", file.filename, e,
        )
        raise HTTPException(500, f"Import failed: {e}")
    finally:
        # tmp_in is always ours to delete; tmp_out is gone once
        # import_audio_file succeeds (it gets moved into AUDIO_DIR).
        for p in (tmp_in, tmp_out):
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass


# ── List / bulk delete / clear ──────────────────────────────────────────────


@router.get("")
async def list_meetings(
    limit: int = 20,
    offset: int = 0,
    days: int = 2,
    date: str | None = None,
) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if date:
            cursor = await db.execute(
                "SELECT id, title, started_at, ended_at, status, vault_path, "
                "       last_tagged_at, last_recomputed_at "
                "FROM meetings WHERE started_at LIKE ? "
                "ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (f"{date}%", limit, offset),
            )
        else:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            cursor = await db.execute(
                "SELECT id, title, started_at, ended_at, status, vault_path, "
                "       last_tagged_at, last_recomputed_at "
                "FROM meetings WHERE started_at >= ? "
                "ORDER BY started_at DESC LIMIT ? OFFSET ?",
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
    """Apply a user-typed title. Implicitly sets title_locked = 1 so the
    live-refinement loop and AI Summary don't overwrite what the user
    just typed. The lock can be cleared later via PATCH /title-lock."""
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
            "UPDATE meetings SET title = ?, title_locked = 1 WHERE id = ?",
            (req.title.strip(), meeting_id),
        )
        # Keep the .opus filename in lock-step with the title — only fires
        # when the meeting is no longer recording (the file is held open
        # during capture, so the rename would either fail or just no-op).
        await sync_meeting_audio_filename(db, meeting_id)
        await db.commit()
    if old_vault_path:
        old_file = Path(old_vault_path)
        if old_file.exists():
            old_file.unlink()
    await rewrite_vault(meeting_id)
    return {"ok": True}


class TitleLockRequest(BaseModel):
    locked: bool


@router.patch("/{meeting_id}/title-lock")
async def set_meeting_title_lock(meeting_id: str, req: TitleLockRequest) -> dict:
    """Toggle the title-frozen flag WITHOUT changing the title.

    Flipping to `locked=false` re-enables the live-refinement loop and
    the AI Summary auto-rename; flipping to `locked=true` freezes the
    current title against both. The frontend uses this for the lock
    icon next to the title — typing a custom title via PATCH already
    locks automatically, so this endpoint is for the explicit toggle
    path (unlock to get AI help; re-lock after the AI did its thing).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM meetings WHERE id = ?", (meeting_id,)
        )
        if await cursor.fetchone() is None:
            raise HTTPException(404, "Meeting not found")
        await db.execute(
            "UPDATE meetings SET title_locked = ? WHERE id = ?",
            (1 if req.locked else 0, meeting_id),
        )
        await db.commit()
    return {"ok": True, "locked": req.locked}


# ── Summary / title suggestion ──────────────────────────────────────────────


@router.post("/{meeting_id}/summarize")
async def summarize_meeting(meeting_id: str) -> dict:
    """Generate/refresh the AI summary.

    Piggyback: the same LLM call produces title candidates, so if the
    title is still unlocked (the user hasn't typed their own) we
    auto-apply the top suggestion + rename the Obsidian file. A locked
    title is never overwritten — the user owns it.
    """
    result, _current_title, current_vault_path, title_locked = await run_analysis(meeting_id)

    if not result.summary_markdown:
        raise HTTPException(
            502,
            "The LLM replied but the summary section was missing or malformed. "
            "Check the sidecar log for the raw output, then try again.",
        )
    await persist_summary(meeting_id, result.summary_markdown)

    # Auto-rename only when the title is unlocked. The user can unlock
    # a frozen title at any time via PATCH /title-lock and re-run
    # Summary to get a fresh suggestion applied.
    if not title_locked and result.titles:
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
    result, _current_title, _old_vault, _title_locked = await run_analysis(meeting_id)

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

    # Prefer the stored path; fall back to glob so renamed files (legacy
    # "<uuid>.opus" or new "<uuid> - <title>.opus" if audio_path went
    # stale after a rename / crash) still resolve.
    p: Path | None = None
    if audio_path_s:
        candidate = Path(audio_path_s)
        if candidate.exists():
            p = candidate
    if p is None:
        p = find_meeting_audio_file(meeting_id)
    if p is None:
        raise HTTPException(404, "No audio recorded for this meeting")
    # Download filename mirrors the on-disk name so the title shows up
    # if the user saves the file from the audio element's context menu.
    return FileResponse(
        str(p),
        media_type="audio/ogg",
        filename=p.name,
    )


# ── Speaker rename / per-utterance assign ───────────────────────────────────
#
# These endpoints are meeting-scoped but manipulate Voices state (creating a
# voice on the fly, folding embeddings into its pool). That's why the voice
# helpers live in _shared rather than inside voices.py.


def _load_audio_slice_sync(
    audio_path: Path, start_seconds: float, end_seconds: float
) -> np.ndarray | None:
    """Decode a slice of an .opus file to 16kHz float32 mono for pyannote.

    Returns None if the file is missing or the slice is empty/invalid. Used
    by the assign + rename endpoints to recover an embedding when the
    utterance's stored embedding is None (pyannote attaches a chunk's
    centroid to one turn per label, so follower turns have no sample).
    """
    import soundfile as sf

    if not audio_path.exists():
        return None
    try:
        info = sf.info(str(audio_path))
    except Exception:
        return None
    sr = info.samplerate
    # Pad slightly so pyannote's segmentation has enough context to land
    # a valid embedding — its powerset segmentation can refuse to commit
    # on a bare 1-2s clip with no surrounding silence.
    pad = 0.5
    s = max(0.0, start_seconds - pad)
    e = min(float(info.frames) / sr, end_seconds + pad)
    if e <= s:
        return None
    start_frame = int(s * sr)
    frames = int((e - s) * sr)
    if frames <= 0:
        return None
    try:
        data, file_sr = sf.read(
            str(audio_path), start=start_frame, frames=frames,
            dtype="float32", always_2d=False,
        )
    except Exception:
        return None
    if data.size == 0:
        return None
    if data.ndim > 1:
        data = data.mean(axis=1)
    if file_sr != 16_000:
        try:
            from scipy.signal import resample_poly
            g = np.gcd(file_sr, 16_000)
            data = resample_poly(data, 16_000 // g, file_sr // g).astype(np.float32)
        except Exception:
            return None
    return np.ascontiguousarray(data, dtype=np.float32)


def _resolve_meeting_audio_path(meeting_id: str, stored_audio_path: str | None) -> Path | None:
    """Resolve a meeting's on-disk recording. Tries the row's stored path
    first, then falls back to a glob via `find_meeting_audio_file` so
    renamed files (legacy UUID-only or current "<uuid> - <title>.opus")
    still resolve. Returns None when nothing on disk matches."""
    if stored_audio_path:
        candidate = Path(stored_audio_path)
        if candidate.exists():
            return candidate
    return find_meeting_audio_file(meeting_id)


async def _recover_segment_embedding(
    audio_path: Path | None,
    audio_start: float | None,
    duration: float,
) -> bytes | None:
    """Re-extract a fresh pyannote centroid for a single utterance from the
    meeting's recording — used when utterances.embedding is None (pyannote
    only attaches the chunk centroid to one turn per label, leaving follower
    turns sample-less). Returns pickled bytes ready for voice_embeddings,
    or None when the audio is missing / the slice is empty."""
    if audio_path is None or audio_start is None or duration <= 0:
        return None
    loop = asyncio.get_running_loop()
    audio = await loop.run_in_executor(
        None, _load_audio_slice_sync, audio_path,
        float(audio_start), float(audio_start) + float(duration),
    )
    if audio is None:
        return None
    return await manager.engine.extract_segment_embedding(audio)


class RenameSpeakerRequest(BaseModel):
    meeting_id: str
    old_name: str
    new_name: str
    # When provided AND `old_name` is a provisional cluster, only this single
    # utterance's embedding is enrolled into the target voice's pool. Other
    # utterances in the cluster are still relabeled for transcript display
    # but contribute no samples — so "1 click = 1 sample" holds in the
    # Voices page. None disables enrollment entirely (used for voice→voice
    # renames and explicit display-only relabels).
    enroll_utterance_id: str | None = None


@router.post("/{meeting_id}/rename-speaker")
async def rename_speaker(meeting_id: str, req: RenameSpeakerRequest) -> dict:
    """Bulk-tag every pill in a cluster/name within one meeting to a Voice.

    Two modes:
    - Provisional ("Speaker N") → Voice: create/find the Voice and relabel
      every utterance with that label, then drop the provisional cluster.
      Enrollment is scoped to a single anchor utterance (`enroll_utterance_id`)
      so the user's one click produces one sample, not N. The remaining
      members of the cluster ride along with the rename for display only.
    - One Voice name → another (existing or new): cascades the rename
      through `utterances.speaker` and creates the target Voice if needed.
      No enrollment — voice→voice renames don't manufacture new samples.
    """
    new_name = req.new_name.strip()
    if not new_name:
        raise HTTPException(400, "new_name cannot be empty")

    is_provisional = bool(PROVISIONAL_LABEL_RE.match(req.old_name))

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Case-insensitive: typing "me" or "ME" resolves to the existing "Me"
        # voice. `canonical_new_name` is what we tag utterances with so the
        # transcript pill reflects the voice's stored display name.
        voice_id, canonical_new_name = await get_or_create_voice(db, new_name)

        # Resolve the meeting's audio path up front — needed when we have to
        # fall back to slice-based embedding extraction. Done inside the same
        # connection so we avoid nesting another aiosqlite.connect mid-tx.
        cursor = await db.execute(
            "SELECT audio_path FROM meetings WHERE id = ?", (meeting_id,)
        )
        meeting_row = await cursor.fetchone()
        audio_path = _resolve_meeting_audio_path(
            meeting_id, meeting_row["audio_path"] if meeting_row else None,
        )

        if is_provisional and req.enroll_utterance_id:
            # Enroll ONLY the user-clicked anchor — never the whole cluster.
            # Re-tag-safe: drop any prior learning tied to this same utterance
            # so retagging a mistake is lossless (matches the per-utterance
            # /assign flow's behavior).
            cursor = await db.execute(
                "SELECT id, embedding, audio_start, start_time, end_time "
                "FROM utterances "
                "WHERE id = ? AND meeting_id = ? AND speaker = ?",
                (req.enroll_utterance_id, meeting_id, req.old_name),
            )
            anchor = await cursor.fetchone()
            if anchor:
                anchor_emb = anchor["embedding"]
                # Stored embedding is None for "follower" turns within the
                # same chunk-label — pyannote attached the centroid to the
                # first turn only. Recover by re-extracting from audio.
                if anchor_emb is None:
                    duration = float(anchor["end_time"]) - float(anchor["start_time"])
                    anchor_emb = await _recover_segment_embedding(
                        audio_path, anchor["audio_start"], duration,
                    )
                if anchor_emb is not None:
                    await db.execute(
                        "DELETE FROM voice_embeddings WHERE utterance_id = ?",
                        (req.enroll_utterance_id,),
                    )
                    await db.execute(
                        "INSERT INTO voice_embeddings "
                        "(id, voice_id, meeting_id, utterance_id, embedding, "
                        " start_time, end_time, source, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'manual', ?)",
                        (
                            str(uuid.uuid4()), voice_id, meeting_id, anchor["id"],
                            anchor_emb, anchor["start_time"], anchor["end_time"],
                            datetime.now().isoformat(),
                        ),
                    )

        # Cluster fold + voice→voice rename are both user-asserted: every
        # affected utterance is now considered verified, so a future recompute
        # won't silently flip it back. Tagging is the user's explicit ground
        # truth — only auto-assigned labels remain unverified.
        await db.execute(
            "UPDATE utterances SET speaker = ?, verified = 1 "
            "WHERE meeting_id = ? AND speaker = ?",
            (canonical_new_name, meeting_id, req.old_name),
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
    return {"ok": True, "voice_id": voice_id, "speaker": canonical_new_name}


class AssignUtteranceSpeakerRequest(BaseModel):
    speaker: str  # "" or "Unknown" clears the tag + removes learning
    create_if_new: bool = True
    # When False, only update the speaker label — do NOT add this utterance's
    # embedding to the voice's pool. Used for the trailing utterances of a
    # merged display-bubble where one user click should produce one sample.
    enroll: bool = True


@router.post("/{meeting_id}/utterances/{utterance_id}/assign")
async def assign_utterance_speaker(
    meeting_id: str, utterance_id: str, req: AssignUtteranceSpeakerRequest
) -> dict:
    """Tag (or retag, or clear) one utterance.

    Side-effect (when `enroll=True`): folds this utterance's embedding into
    the Voice's pool. Retagging first removes any prior learning tied to
    this utterance so mistakes are fully undoable.
    """
    new_speaker = req.speaker.strip()
    is_clear = not new_speaker or new_speaker.lower() == "unknown"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT embedding, audio_start, start_time, end_time FROM utterances "
            "WHERE id = ? AND meeting_id = ?",
            (utterance_id, meeting_id),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Utterance not found")
        embedding = row["embedding"]

        # Resolve audio path inside this same connection so we don't have to
        # nest another aiosqlite.connect when the slice-based fallback fires.
        cursor = await db.execute(
            "SELECT audio_path FROM meetings WHERE id = ?", (meeting_id,)
        )
        meeting_row = await cursor.fetchone()
        audio_path = _resolve_meeting_audio_path(
            meeting_id, meeting_row["audio_path"] if meeting_row else None,
        )

        # Undo any prior learning from this utterance before applying the
        # new one — guarantees re-tagging mistakes is lossless.
        await db.execute(
            "DELETE FROM voice_embeddings WHERE utterance_id = ?",
            (utterance_id,),
        )

        # Re-extract from audio when the live path didn't attach an embedding
        # to this turn (pyannote stores the chunk centroid on one turn per
        # label, so "follower" turns store None). Skipped when the request
        # won't enroll anyway. Held inside the connection because we want
        # the rest of this transaction to atomically include the new row.
        if embedding is None and not is_clear and req.enroll:
            duration = float(row["end_time"]) - float(row["start_time"])
            embedding = await _recover_segment_embedding(
                audio_path, row["audio_start"], duration,
            )

        if is_clear:
            # Even clearing-to-Unknown is a user-asserted choice; mark
            # verified so recompute doesn't immediately re-tag it from a
            # voice's pool the user already rejected.
            await db.execute(
                "UPDATE utterances SET speaker = 'Unknown', verified = 1 "
                "WHERE id = ?",
                (utterance_id,),
            )
            canonical_speaker = "Unknown"
        else:
            # Case-insensitive existence probe — typing "me" or "ME" must
            # not bypass the create_if_new=false guard when "Me" already
            # exists. We then call get_or_create_voice to either reuse the
            # existing row (returning its canonical display name) or create
            # a new one with the user-typed casing.
            cursor = await db.execute(
                "SELECT id FROM voices WHERE LOWER(name) = LOWER(?)", (new_speaker,)
            )
            voice_row = await cursor.fetchone()
            if voice_row is None and not req.create_if_new:
                raise HTTPException(
                    400, f"Voice '{new_speaker}' not found and create_if_new=false"
                )
            voice_id, canonical_speaker = await get_or_create_voice(db, new_speaker)

            await db.execute(
                "UPDATE utterances SET speaker = ?, verified = 1 WHERE id = ?",
                (canonical_speaker, utterance_id),
            )
            if embedding is not None and req.enroll:
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
    return {"ok": True, "speaker": canonical_speaker}


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

    audio_path: Path | None = None
    if audio_path_s:
        candidate = Path(audio_path_s)
        if candidate.exists():
            audio_path = candidate
    if audio_path is None:
        audio_path = find_meeting_audio_file(meeting_id)
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
        # Only consider unverified utterances. User-asserted tags (verified=1
        # set by the assign / rename-speaker / clear paths) are immutable
        # ground truth — pyannote may disagree with them, but the user is
        # the source of truth for the labels they've explicitly chosen.
        cursor = await db.execute(
            "SELECT id, audio_start, start_time, end_time FROM utterances "
            "WHERE meeting_id = ? AND verified = 0 ORDER BY start_time",
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
            # Defense-in-depth: re-check verified=0 in the UPDATE. Cheap, and
            # protects against any future code path that might race with a
            # concurrent tag landing during this loop.
            await db.execute(
                "UPDATE utterances SET speaker = ?, match_distance = ? "
                "WHERE id = ? AND verified = 0",
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
