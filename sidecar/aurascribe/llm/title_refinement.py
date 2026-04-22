"""Live meeting-title refinement.

Runs a lightweight LLM call in the same debounce window as
`RealtimeIntelligence`. The LLM returns only `{entity, topic}`; the
server stitches the final title as
`{YYYY-MM-DD HH-MM-SS} - {entity} - {topic}` using the known
`started_at` so the date is always authoritative.

Updates are gated by `meetings.title_locked`:
  * locked=0 → apply, persist, broadcast `title_updated` WS event
  * locked=1 → skip silently (the user owns the title)

This module is narrowly scoped: no summary, no highlights, no action
items — those live in `realtime.py`. Keeping them separate means
user-edited `live_intelligence.md` prompts keep their existing output
contract unchanged, and the title call stays cheap (~300 output tokens
vs ~2000 for live intel).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

import aiosqlite

from aurascribe.config import (
    DB_PATH,
    PROMPTS_DIR,
    RT_HIGHLIGHTS_DEBOUNCE_SEC,
    RT_HIGHLIGHTS_MAX_INTERVAL_SEC,
    RT_HIGHLIGHTS_WINDOW_SEC,
)
from aurascribe.llm.client import LLMUnavailableError, chat
from aurascribe.transcription import Utterance

log = logging.getLogger("aurascribe.title_refinement")

PROMPT_FILENAME = "meeting_title_refinement.md"
_USER_PROMPT = PROMPTS_DIR / PROMPT_FILENAME
_BUNDLED_DEFAULT = Path(__file__).resolve().parent / PROMPT_FILENAME

# JSON code-fence stripper — same tolerance as realtime.py since local
# LLMs like to wrap output even when told not to.
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)

# Characters that are invalid in filenames on Windows. Scrub them out of
# the composed title before we save, since titles feed the Obsidian
# vault filename. Shared rules with llm/analysis.py.
_FILENAME_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Generic entities the model sometimes emits when it can't identify a
# real one. Dropped server-side so we compose `{ts} - {topic}` without
# redundant `Meeting - ` noise.
_GENERIC_ENTITIES = {"meeting", "call", "sync", "discussion", "n/a", "none", "unknown"}

# Cap the input transcript so we don't blow past small models' context
# budgets with a long-running meeting. Titles don't need the full
# history — the most recent window is where the topic usually lives.
_MAX_PROMPT_CHARS = 8000

BroadcastFn = Callable[[dict], Awaitable[None]]


def _ensure_prompt_file() -> Path:
    """Copy the bundled default into APP_DATA/prompts if missing.
    Same healing pattern realtime.py uses — lets the user delete the
    file mid-run and have it regenerate instead of crashing."""
    if not _USER_PROMPT.exists() and _BUNDLED_DEFAULT.is_file():
        try:
            _USER_PROMPT.write_text(
                _BUNDLED_DEFAULT.read_text(encoding="utf-8"), encoding="utf-8",
            )
        except Exception as e:
            log.warning("Could not reseed %s: %s", _USER_PROMPT, e)
    return _USER_PROMPT


class _MeetingState:
    """Per-meeting debounce state. One RefineTask at a time — cancelled
    + replaced whenever a fresh utterance arrives inside the debounce
    window. `last_run_ts` + `last_title` are what let us skip LLM calls
    that would produce the same title."""

    __slots__ = ("pending_task", "last_run_ts", "last_title", "lock", "consecutive_failures")

    def __init__(self) -> None:
        self.pending_task: asyncio.Task | None = None
        self.last_run_ts: float = 0.0
        self.last_title: str = ""
        self.lock = asyncio.Lock()
        self.consecutive_failures: int = 0


class TitleRefinement:
    """Live title-refinement coordinator. Mirrors the
    `RealtimeIntelligence` API so `MeetingManager` can wire both into
    the same utterance stream with minimal fuss."""

    def __init__(self, broadcast: BroadcastFn | None = None) -> None:
        self._broadcast = broadcast
        self._states: dict[str, _MeetingState] = {}

    def set_broadcast(self, broadcast: BroadcastFn) -> None:
        self._broadcast = broadcast

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def prepare_meeting(self, meeting_id: str) -> None:
        _ensure_prompt_file()
        self._states[meeting_id] = _MeetingState()

    async def flush_and_clear(self, meeting_id: str) -> None:
        state = self._states.pop(meeting_id, None)
        if state and state.pending_task and not state.pending_task.done():
            state.pending_task.cancel()
            try:
                await state.pending_task
            except (asyncio.CancelledError, Exception):
                pass

    # ── Debounce trigger ─────────────────────────────────────────────────

    async def note_utterances(
        self, meeting_id: str, utterances: list[Utterance]
    ) -> None:
        """Reschedule a refinement run on every new utterance batch.

        Same debounce math as `RealtimeIntelligence.note_utterances`:
        cancel + re-arm unless we're already past the max-interval
        ceiling, in which case let the queued run fire.
        """
        if not utterances:
            return
        state = self._states.get(meeting_id)
        if state is None:
            await self.prepare_meeting(meeting_id)
            state = self._states[meeting_id]

        now = time.monotonic()
        time_since_last = now - state.last_run_ts if state.last_run_ts else float("inf")
        if state.pending_task and not state.pending_task.done():
            if time_since_last < RT_HIGHLIGHTS_MAX_INTERVAL_SEC:
                state.pending_task.cancel()
                try:
                    await state.pending_task
                except (asyncio.CancelledError, Exception):
                    pass
            else:
                return

        delay = RT_HIGHLIGHTS_DEBOUNCE_SEC
        if state.last_run_ts:
            remaining = RT_HIGHLIGHTS_MAX_INTERVAL_SEC - time_since_last
            if remaining > 0:
                delay = min(delay, remaining)
            else:
                delay = 0.0

        state.pending_task = asyncio.create_task(
            self._delayed_run(meeting_id, delay)
        )

    # ── Internal ─────────────────────────────────────────────────────────

    async def _delayed_run(self, meeting_id: str, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self._run(meeting_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Title refinement run crashed: %s", e, exc_info=True)

    async def _run(self, meeting_id: str) -> None:
        state = self._states.get(meeting_id)
        if state is None:
            return
        async with state.lock:
            try:
                await self._run_locked(meeting_id, state)
            except LLMUnavailableError as e:
                state.consecutive_failures += 1
                log.info(
                    "Title refinement skipped (LLM unavailable, attempt %d): %s",
                    state.consecutive_failures, e,
                )
            except Exception as e:
                state.consecutive_failures += 1
                log.warning("Title refinement run failed: %s", e, exc_info=True)

    async def _run_locked(self, meeting_id: str, state: _MeetingState) -> None:
        # Fetch current title, lock state, started_at, AND the recent
        # transcript in one DB pass. `title_locked == 1` short-circuits
        # the whole thing — no LLM call, no cost.
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT title, title_locked, started_at FROM meetings WHERE id = ?",
                (meeting_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return
            if bool(row["title_locked"]):
                log.debug("Title refinement: meeting %s is locked; skipping", meeting_id)
                return
            current_title = row["title"] or ""
            started_at_raw = row["started_at"]

            cursor = await db.execute(
                "SELECT MAX(end_time) AS max_e FROM utterances WHERE meeting_id = ?",
                (meeting_id,),
            )
            tail = await cursor.fetchone()
            max_end = float(tail["max_e"]) if tail and tail["max_e"] is not None else 0.0
            window_start = max(0.0, max_end - RT_HIGHLIGHTS_WINDOW_SEC)
            cursor = await db.execute(
                "SELECT speaker, text, start_time FROM utterances "
                "WHERE meeting_id = ? AND end_time >= ? "
                "ORDER BY start_time",
                (meeting_id, window_start),
            )
            utt_rows = await cursor.fetchall()
        if not utt_rows:
            return

        try:
            started_at = (
                datetime.fromisoformat(started_at_raw) if started_at_raw else None
            )
        except Exception:
            started_at = None

        recent = "\n".join(
            f"[{_fmt_t(r['start_time'])}] {r['speaker']}: {r['text']}"
            for r in utt_rows
        )
        # Clip from the front (most recent lines matter most) if the
        # transcript is very long — keeps small-context models happy.
        if len(recent) > _MAX_PROMPT_CHARS:
            recent = recent[-_MAX_PROMPT_CHARS:]

        prompt = self._render_prompt(
            recent_transcript=recent, current_title=current_title,
        )

        log.info(
            "title refinement: %d utterances in window, calling LLM",
            len(utt_rows),
        )
        raw = await chat(prompt, max_tokens=300)
        state.last_run_ts = time.monotonic()
        state.consecutive_failures = 0

        parsed = _parse_json(raw)
        if not parsed:
            log.warning(
                "Title refinement: unparseable JSON. First 200 chars: %r",
                raw[:200],
            )
            return

        entity = _clean_entity(parsed.get("entity"))
        topic = _clean_topic(parsed.get("topic"))
        if not topic:
            log.debug("Title refinement: no topic returned; skipping")
            return

        new_title = _compose_title(started_at, entity, topic)
        if not new_title:
            return
        if new_title == state.last_title or new_title == current_title:
            # Nothing changed — skip persist + broadcast to keep the UI
            # quiet. The model often converges on a stable suggestion
            # after the first few runs.
            state.last_title = new_title
            return

        # Re-check lock RIGHT before writing — the user may have hit
        # "freeze" while the LLM call was in flight, and we don't want
        # to clobber their choice.
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT title_locked, vault_path FROM meetings WHERE id = ?",
                (meeting_id,),
            )
            row = await cursor.fetchone()
            if not row or bool(row["title_locked"]):
                return
            old_vault_path = row["vault_path"]
            await db.execute(
                "UPDATE meetings SET title = ? WHERE id = ?",
                (new_title, meeting_id),
            )
            await db.commit()

        # Best-effort vault rename — same pattern as the pencil-rename
        # endpoint. Old file gets unlinked; the next intel flush
        # rewrites the vault so the new filename lands automatically.
        if old_vault_path:
            try:
                old_file = Path(old_vault_path)
                if old_file.exists():
                    old_file.unlink()
            except Exception as e:
                log.warning("Title refinement: could not unlink old vault file %s: %s",
                            old_vault_path, e)

        state.last_title = new_title

        if self._broadcast is not None:
            try:
                await self._broadcast({
                    "type": "title_updated",
                    "meeting_id": meeting_id,
                    "title": new_title,
                    "source": "live_refinement",
                })
            except Exception:
                # Broadcast failure isn't fatal; the DB write already landed.
                pass

    def _render_prompt(self, *, recent_transcript: str, current_title: str) -> str:
        prompt_path = _ensure_prompt_file()
        try:
            template = prompt_path.read_text(encoding="utf-8")
        except Exception as e:
            log.warning(
                "Could not read user prompt %s: %s — using bundled default",
                prompt_path, e,
            )
            try:
                template = _BUNDLED_DEFAULT.read_text(encoding="utf-8")
            except Exception as e2:
                log.error("Could not read bundled title refinement prompt: %s", e2)
                # Last-ditch inline fallback — keeps the feature alive if
                # both prompt files vanish.
                template = (
                    "Return JSON {\"entity\": string, \"topic\": string}. "
                    "No prose, no code fences.\n\n{recent_transcript}\n\n"
                    "Previously: {current_title}"
                )
        # str.replace to avoid tripping str.format on JSON-schema braces
        # in the prompt body.
        return (
            template
            .replace("{recent_transcript}", recent_transcript)
            .replace("{current_title}", current_title)
        )


# ── Helpers (shared style with llm/analysis.py) ──────────────────────────


def _fmt_t(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _parse_json(raw: str) -> dict | None:
    if not raw:
        return None
    candidate = _FENCE_RE.sub("", raw).strip()
    try:
        v = json.loads(candidate)
        return v if isinstance(v, dict) else None
    except Exception:
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        try:
            v = json.loads(candidate[start : end + 1])
            return v if isinstance(v, dict) else None
        except Exception:
            return None
    return None


def _clean_entity(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    s = raw.strip().strip('"').strip("'").rstrip(".").strip()
    if not s:
        return None
    if len(s) > 40:
        s = s[:40].rstrip()
    s = _FILENAME_UNSAFE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return None
    if s.lower() in _GENERIC_ENTITIES:
        return None
    return s


def _clean_topic(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    s = raw.strip().strip('"').strip("'").rstrip(".").strip()
    if not s or len(s) > 100:
        return None
    s = _FILENAME_UNSAFE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _compose_title(
    started_at: datetime | None,
    entity: str | None,
    topic: str,
) -> str:
    """Stitch `{YYYY-MM-DD HH-MM-SS} - {entity} - {topic}`.

    Drops the timestamp slot when `started_at` is None and the entity
    slot when `entity` is None — same rules as llm/analysis.py's
    `_compose_titles` so the two code paths stay visually consistent.
    """
    ts = started_at.strftime("%Y-%m-%d %H-%M-%S") if started_at else None
    parts = [p for p in (ts, entity, topic) if p]
    return " - ".join(parts)
