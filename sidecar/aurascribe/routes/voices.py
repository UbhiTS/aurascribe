"""Voice endpoints — CRUD, merge, snippet-delete.

Voices replace the old enroll-first model: speakers are identified over
time by tagging utterances in meetings. Each tag folds that utterance's
centroid embedding into the Voice's pool, so matching improves with
every tag. The tagging itself happens via meetings.py's rename-speaker /
assign endpoints — this module handles the Voice objects themselves.
"""
from __future__ import annotations

from datetime import datetime

import aiosqlite
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aurascribe.config import DB_PATH
from aurascribe.routes._shared import (
    bump_meeting_tag,
    bump_meetings_for_voice,
    manager,
)

router = APIRouter(prefix="/api/voices")


@router.get("")
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


@router.get("/{voice_id}")
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


@router.patch("/{voice_id}")
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
            await bump_meetings_for_voice(db, voice_id)

        if req.color is not None:
            await db.execute(
                "UPDATE voices SET color = ?, updated_at = ? WHERE id = ?",
                (req.color, datetime.now().isoformat(), voice_id),
            )
        await db.commit()

    await manager.engine.reload_voices()
    return {"ok": True}


@router.delete("/{voice_id}")
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
        await bump_meetings_for_voice(db, voice_id)

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


@router.delete("/{voice_id}/snippets/{snippet_id}")
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
            await bump_meeting_tag(db, affected_meeting)
        await db.commit()

    await manager.engine.reload_voices()
    return {"ok": True}


class VoiceMergeRequest(BaseModel):
    from_id: str
    into_id: str


@router.post("/merge")
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
        await bump_meetings_for_voice(db, req.from_id)
        await bump_meetings_for_voice(db, req.into_id)

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
