"""Shared state + cross-router helpers for the HTTP layer.

Centralising these here avoids circular imports between the main `api`
module and the per-feature routers.

Contents:
  * `manager`          — MeetingManager singleton used by every router
  * `ws_clients`       — active WebSocket connections
  * `broadcast_lock`   — guards ws_clients mutation vs. broadcast iteration
  * `broadcast`        — push a JSON payload to every connected client
  * Vault helpers      — `rewrite_vault`, used after any meeting mutation
  * Analysis helpers   — `run_analysis`, `persist_summary`,
                         `rename_with_vault_move`, `fetch_meeting_row`
  * Deletion helpers   — `delete_audio_files`, `delete_vault_files`
  * Voice helpers      — palette + id lookup/create + recompute-flag bumping,
                         shared between meetings.py (rename-speaker/assign)
                         and voices.py (CRUD + merge)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path

import aiosqlite
from fastapi import HTTPException, WebSocket

from aurascribe.config import AUDIO_DIR, DB_PATH
from aurascribe.llm.analysis import (
    AnalysisEmptyError,
    AnalysisResult,
    analyze_meeting,
)
from aurascribe.llm.client import LLMUnavailableError
from aurascribe.llm.prompts import format_transcript
from aurascribe.meeting_manager import MeetingManager, extract_action_items
from aurascribe.obsidian.writer import rewrite_meeting_vault
from aurascribe.transcription import Utterance

log = logging.getLogger("aurascribe")

# ── Singletons ──────────────────────────────────────────────────────────────

manager = MeetingManager()
ws_clients: list[WebSocket] = []
# Guards concurrent ws_clients iteration. Without this, a WebSocket
# connecting or disconnecting mid-broadcast races the for-loop in
# `broadcast()` — payload drops, or "list changed size during iteration".
broadcast_lock = asyncio.Lock()

# Populated during FastAPI lifespan startup — see aurascribe.api. Lives
# here so every router (settings for hot-reload, auto_capture routes for
# toggle, status endpoint for snapshot) can reach it without reintroducing
# the `api → routes → api` import cycle.
auto_capture_monitor: "AutoCaptureMonitor | None" = None  # type: ignore[name-defined]


def set_auto_capture_monitor(monitor: object) -> None:
    """Install the process-wide AutoCaptureMonitor. Called once during
    lifespan startup."""
    global auto_capture_monitor
    auto_capture_monitor = monitor  # type: ignore[assignment]


async def broadcast(payload: dict) -> None:
    async with broadcast_lock:
        dead: list[WebSocket] = []
        for ws in ws_clients:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            try:
                ws_clients.remove(ws)
            except ValueError:
                pass  # already removed by the /ws endpoint's disconnect handler


# ── Provisional speaker label pattern ───────────────────────────────────────
#
# Shared between meetings.rename-speaker (detects provisional → Voice)
# and finalize paths (excludes provisional from people-note generation).
PROVISIONAL_LABEL_RE = re.compile(r"^Speaker \d+$")


# ── Voice palette + helpers ─────────────────────────────────────────────────
#
# Used by both the voices router (new-voice creation) and meetings router
# (speaker rename/assign, which can create a voice on the fly).

# Palette keys — ONE-TO-ONE with SPEAKER_PALETTE in src/lib/speakerColors.ts.
# Hues spread ~40° apart on the color wheel so no two slots read as similar
# at a glance. The key is stored on voices.color; the frontend looks it up
# in its own table to resolve Tailwind class names. Never reorder existing
# entries — doing so would re-assign colors on every stored voice. Append
# only if the frontend table is extended in the same order.
VOICE_PALETTE_KEYS: tuple[str, ...] = (
    "rose",
    "orange",
    "yellow",
    "lime",
    "emerald",
    "cyan",
    "blue",
    "violet",
    "fuchsia",
)


async def next_voice_color(db: aiosqlite.Connection) -> str:
    """Pick the palette slot with the fewest existing voices, tie-breaking
    by palette order. This guarantees:
      * distinct colors for every voice up to 9 (palette size)
      * stable assignment — a slot "freed" by a deletion is filled first
        before doubling up on any other slot
      * deterministic under concurrency-free access (the sidecar serializes
        writes through aiosqlite).
    """
    cursor = await db.execute(
        "SELECT color FROM voices WHERE color IS NOT NULL"
    )
    rows = await cursor.fetchall()
    counts: dict[str, int] = {k: 0 for k in VOICE_PALETTE_KEYS}
    for (c,) in rows:
        if c in counts:
            counts[c] += 1
    return min(VOICE_PALETTE_KEYS, key=lambda k: (counts[k], VOICE_PALETTE_KEYS.index(k)))


async def backfill_voice_colors(db: aiosqlite.Connection) -> None:
    """Migrate voices whose `color` is NULL or a legacy hex string
    (VOICE_COLORS from before the palette-key refactor) to the new key
    scheme. Runs once on startup — cheap no-op when all voices are
    already keyed correctly. Assignment honours created_at order so the
    oldest voice gets the first available slot; newer voices fill the
    remaining slots in order."""
    cursor = await db.execute(
        "SELECT id, color FROM voices ORDER BY created_at ASC"
    )
    rows = await cursor.fetchall()
    if not rows:
        return
    counts: dict[str, int] = {k: 0 for k in VOICE_PALETTE_KEYS}
    to_fix: list[str] = []
    for voice_id, color in rows:
        if color in counts:
            counts[color] += 1
        else:
            to_fix.append(voice_id)
    if not to_fix:
        return
    now = datetime.now().isoformat()
    for voice_id in to_fix:
        slot = min(
            VOICE_PALETTE_KEYS,
            key=lambda k: (counts[k], VOICE_PALETTE_KEYS.index(k)),
        )
        await db.execute(
            "UPDATE voices SET color = ?, updated_at = ? WHERE id = ?",
            (slot, now, voice_id),
        )
        counts[slot] += 1
    await db.commit()


async def get_or_create_voice(db: aiosqlite.Connection, name: str) -> str:
    """Return voice_id for `name`, creating the row with a fresh color if new.
    Works whether or not the caller has set a row_factory — positional [0]
    access is supported by both tuples and aiosqlite.Row."""
    cursor = await db.execute("SELECT id FROM voices WHERE name = ?", (name,))
    row = await cursor.fetchone()
    if row is not None:
        return str(row[0])
    voice_id = str(uuid.uuid4())
    color = await next_voice_color(db)
    now = datetime.now().isoformat()
    await db.execute(
        "INSERT INTO voices (id, name, color, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (voice_id, name, color, now, now),
    )
    return voice_id


async def bump_meeting_tag(db: aiosqlite.Connection, meeting_id: str) -> None:
    """Mark this meeting as having had a label change, so the UI can show
    a 'Tags pending — Recompute to apply' indicator."""
    await db.execute(
        "UPDATE meetings SET last_tagged_at = ? WHERE id = ?",
        (datetime.now().isoformat(), meeting_id),
    )


async def bump_meetings_for_voice(db: aiosqlite.Connection, voice_id: str) -> None:
    """Same idea, but for voice-level changes (rename / delete / merge)
    that can affect every meeting where this voice was tagged. Bumps each
    affected meeting in one statement."""
    await db.execute(
        "UPDATE meetings SET last_tagged_at = ? "
        "WHERE id IN (SELECT DISTINCT meeting_id FROM voice_embeddings "
        "             WHERE voice_id = ? AND meeting_id IS NOT NULL)",
        (datetime.now().isoformat(), voice_id),
    )


# ── Vault rewrite ───────────────────────────────────────────────────────────


async def rewrite_vault(meeting_id: str) -> None:
    """Wrapper kept so existing call sites read naturally — delegates to the
    shared helper in obsidian.writer, which also handles the live-intel
    section that the realtime loop accumulates."""
    await rewrite_meeting_vault(meeting_id)


# ── Deletion helpers ────────────────────────────────────────────────────────


def delete_audio_files(meeting_ids: list[str]) -> None:
    """Remove the .opus recording for each id. Best-effort — the meeting rows
    are already gone, so a lingering file would just waste disk."""
    for mid in meeting_ids:
        p = AUDIO_DIR / f"{mid}.opus"
        if p.exists():
            try:
                p.unlink()
            except Exception as e:
                log.warning("could not delete audio file %s: %s", p, e)


def delete_vault_files(vault_paths: list[str | None]) -> None:
    """Remove the Obsidian markdown files for the given meetings.

    Pass absolute paths as stored in `meetings.vault_path`. Entries that
    are None/empty or point at a non-existent file are silently skipped
    — meetings that pre-date vault configuration or were never written
    simply have nothing to remove. Best-effort, same as delete_audio_files."""
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


# ── Analysis helpers (title + summary) ──────────────────────────────────────


async def run_analysis(meeting_id: str) -> tuple[AnalysisResult, str | None, str | None]:
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


async def persist_summary(meeting_id: str, summary_md: str) -> None:
    """Write summary + extracted action_items to the meeting row."""
    action_items = extract_action_items(summary_md)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE meetings SET summary = ?, action_items = ? WHERE id = ?",
            (summary_md, json.dumps(action_items) if action_items else None, meeting_id),
        )
        await db.commit()


async def rename_with_vault_move(
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
                pass  # best-effort; rewrite_vault below creates the new one


# Fields we store as JSON-encoded TEXT in SQLite but want the client to
# receive as native arrays/objects. Parsing server-side means the frontend
# never JSON.parse()s on render, which also keeps the `Meeting` TypeScript
# type honest (arrays instead of "could-be-malformed" strings).
_JSON_FIELDS: tuple[str, ...] = (
    "action_items",
    "live_highlights",
    "live_action_items_self",
    "live_action_items_others",
)


def normalize_meeting_row(row: dict) -> dict:
    """Parse the TEXT-encoded JSON fields in a meeting row into native
    objects, tolerating legacy NULL / malformed values. Mutates + returns
    `row`.

    `live_support_intelligence` stays a plain string — it's markdown/text,
    not JSON. Everything else under `_JSON_FIELDS` becomes the decoded
    object (typically a list) or None when unset / unparseable.
    """
    for field in _JSON_FIELDS:
        raw = row.get(field)
        if raw is None or raw == "":
            row[field] = None
            continue
        try:
            row[field] = json.loads(raw)
        except (TypeError, ValueError):
            # Don't blow up an API response on one bad row; log + drop.
            log.warning("meeting row %s: malformed JSON in %s — dropping",
                        row.get("id"), field)
            row[field] = None
    return row


async def fetch_meeting_row(meeting_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
        row = await cursor.fetchone()
    return normalize_meeting_row(dict(row)) if row else {}
