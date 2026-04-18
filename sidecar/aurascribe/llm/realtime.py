"""Real-time meeting intelligence — debounced LLM calls for live highlights,
action items, and forward-looking "support intelligence" talking points.

Usage:
    intel = RealtimeIntelligence(broadcast=ws_broadcast_callback)
    await intel.prepare_meeting(meeting_id)              # on start
    await intel.note_utterances(meeting_id, utterances)  # on each chunk
    await intel.flush_and_clear(meeting_id)              # on stop

State is held per-meeting in memory AND mirrored to the `meetings` table
columns (`live_highlights`, `live_action_items_self`,
`live_action_items_others`, `live_support_intelligence`) so a UI refresh
mid-meeting still shows the panel populated.

Cadence: a new utterance schedules a run for `RT_HIGHLIGHTS_DEBOUNCE_SEC`
later. Subsequent utterances within that window push the run further out
(classic debounce), but the run is force-fired if the time since the last
successful run exceeds `RT_HIGHLIGHTS_MAX_INTERVAL_SEC` — keeps the support
panel feeling alive during nonstop speech.
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
    MY_SPEAKER_LABEL,
    RT_HIGHLIGHTS_DEBOUNCE_SEC,
    RT_HIGHLIGHTS_MAX_INTERVAL_SEC,
    RT_HIGHLIGHTS_WINDOW_SEC,
)
from aurascribe.llm.client import LLMUnavailableError, chat
from aurascribe.transcription import Utterance

log = logging.getLogger("aurascribe.realtime")

PROMPT_FILENAME = "realtime_highlights.md"
# Live edit target. Lives in the repo so the dev workflow is "edit in your
# IDE, save, next LLM call sees it". For end-user packaging we'd want to
# fall back to a writable copy in PROMPTS_DIR, but that's deferred.
PROMPTS_DIR_REPO = Path(__file__).resolve().parent
_BUNDLED_DEFAULT = PROMPTS_DIR_REPO / PROMPT_FILENAME

# JSON code-fence stripper — local LLMs frequently wrap output in ```json...```
# even when the prompt forbids it. We tolerate both fenced and naked output.
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


BroadcastFn = Callable[[dict], Awaitable[None]]


def _ensure_prompt_file() -> Path:
    """Return the live prompt file path. Edits go straight into the repo
    file — no APPDATA shadow copy. Kept as a function (rather than inlining
    the constant) so swapping in a writable-fallback later is one place."""
    return _BUNDLED_DEFAULT


def _norm(s: str) -> str:
    """Loose-equality key for dedup. Lowercase, collapse whitespace, strip
    trailing punctuation. Catches the common case where the LLM rewrites
    "Send the diagram" as "send diagram." across two calls."""
    return re.sub(r"\s+", " ", s.lower()).strip(" .,;:!?-")


def _norm_action_other(item: dict) -> str:
    return f"{(item.get('speaker') or '').lower().strip()}::{_norm(item.get('item') or '')}"


class _MeetingState:
    """Per-meeting live intelligence accumulator."""

    __slots__ = (
        "highlights",
        "action_items_self",
        "action_items_others",
        "support_intelligence",
        "support_intelligence_history",
        "_highlight_keys",
        "_self_keys",
        "_other_keys",
        "lock",
        "pending_task",
        "last_run_ts",
        "consecutive_failures",
    )

    def __init__(self) -> None:
        self.highlights: list[str] = []
        self.action_items_self: list[str] = []
        # List of {"speaker": str, "item": str}
        self.action_items_others: list[dict] = []
        self.support_intelligence: str = ""
        # Append-only chronicle of every non-empty support intelligence push.
        # Entries: {"ts": ISO-8601 wall-clock, "text": str}. Used by the
        # Obsidian writer to render the full history of suggestions (the live
        # UI only shows the latest, but the markdown captures all of them).
        self.support_intelligence_history: list[dict] = []
        self._highlight_keys: set[str] = set()
        self._self_keys: set[str] = set()
        self._other_keys: set[str] = set()
        self.lock = asyncio.Lock()
        self.pending_task: asyncio.Task | None = None
        self.last_run_ts: float = 0.0
        self.consecutive_failures: int = 0


class RealtimeIntelligence:
    def __init__(self, broadcast: BroadcastFn | None = None) -> None:
        self._broadcast = broadcast
        self._states: dict[str, _MeetingState] = {}

    def set_broadcast(self, broadcast: BroadcastFn) -> None:
        self._broadcast = broadcast

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def prepare_meeting(self, meeting_id: str) -> None:
        """Initialize empty state for a new meeting and ensure the prompt file
        exists (seeded on first run)."""
        _ensure_prompt_file()
        self._states[meeting_id] = _MeetingState()

    async def hydrate(self, meeting_id: str) -> None:
        """Reload accumulated state from the DB — used when re-adopting an
        in-flight meeting after a sidecar restart."""
        state = self._states.setdefault(meeting_id, _MeetingState())
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT live_highlights, live_action_items_self, "
                "live_action_items_others, live_support_intelligence, "
                "live_support_intelligence_history "
                "FROM meetings WHERE id = ?",
                (meeting_id,),
            )
            row = await cursor.fetchone()
        if not row:
            return
        state.highlights = _safe_json_list(row["live_highlights"])
        state.action_items_self = _safe_json_list(row["live_action_items_self"])
        state.action_items_others = _safe_json_list(row["live_action_items_others"])
        state.support_intelligence = row["live_support_intelligence"] or ""
        state.support_intelligence_history = _safe_json_list(row["live_support_intelligence_history"])
        state._highlight_keys = {_norm(h) for h in state.highlights}
        state._self_keys = {_norm(s) for s in state.action_items_self}
        state._other_keys = {_norm_action_other(i) for i in state.action_items_others}

    async def flush_and_clear(self, meeting_id: str) -> None:
        """Cancel any pending debounced run and drop the in-memory state. The
        DB rows persist (already mirrored) so the panel survives reload."""
        state = self._states.pop(meeting_id, None)
        if state and state.pending_task and not state.pending_task.done():
            state.pending_task.cancel()
            try:
                await state.pending_task
            except (asyncio.CancelledError, Exception):
                pass

    # ── Debounce trigger ─────────────────────────────────────────────────────

    async def note_utterances(
        self, meeting_id: str, utterances: list[Utterance]
    ) -> None:
        """Called every time a new chunk of utterances lands. Schedules a
        debounced LLM run (or extends the existing one)."""
        if not utterances:
            return
        state = self._states.get(meeting_id)
        if state is None:
            # Recording started before prepare_meeting was called — heal lazily.
            await self.prepare_meeting(meeting_id)
            state = self._states[meeting_id]

        # If there's already a pending run, cancel & re-arm — UNLESS we're
        # already past the max-interval ceiling, in which case let the
        # existing one fire as scheduled.
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
                # Let the queued run go; a new one will be scheduled after it.
                return

        delay = RT_HIGHLIGHTS_DEBOUNCE_SEC
        # Cap delay so total wait never exceeds the max interval.
        if state.last_run_ts:
            remaining = RT_HIGHLIGHTS_MAX_INTERVAL_SEC - time_since_last
            if remaining > 0:
                delay = min(delay, remaining)
            else:
                delay = 0.0

        state.pending_task = asyncio.create_task(
            self._delayed_run(meeting_id, delay)
        )

    async def trigger_now(self, meeting_id: str) -> None:
        """Manual refresh — bypasses debounce. Used by 'Refresh' button or
        on stop to capture the very last segment."""
        state = self._states.get(meeting_id)
        if state is None:
            return
        if state.pending_task and not state.pending_task.done():
            state.pending_task.cancel()
            try:
                await state.pending_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._run(meeting_id)

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _delayed_run(self, meeting_id: str, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self._run(meeting_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Realtime intelligence run crashed: %s", e, exc_info=True)

    async def _run(self, meeting_id: str) -> None:
        state = self._states.get(meeting_id)
        if state is None:
            return
        # Serialize per-meeting — concurrent runs would race on dedup state.
        async with state.lock:
            try:
                await self._run_locked(meeting_id, state)
            except LLMUnavailableError as e:
                state.consecutive_failures += 1
                log.info("Realtime intel skipped (LLM unavailable, attempt %d): %s",
                         state.consecutive_failures, e)
            except Exception as e:
                state.consecutive_failures += 1
                log.warning("Realtime intel run failed: %s", e, exc_info=True)

    async def _run_locked(self, meeting_id: str, state: _MeetingState) -> None:
        # Pull recent transcript window from the DB. The manager has already
        # written it before invoking us, so this is the source of truth and
        # also the easy way to handle utterances that arrived between calls.
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT MAX(end_time) AS max_e FROM utterances WHERE meeting_id = ?",
                (meeting_id,),
            )
            row = await cursor.fetchone()
            max_end = float(row["max_e"]) if row and row["max_e"] is not None else 0.0
            window_start = max(0.0, max_end - RT_HIGHLIGHTS_WINDOW_SEC)
            cursor = await db.execute(
                "SELECT speaker, text, start_time, end_time FROM utterances "
                "WHERE meeting_id = ? AND end_time >= ? "
                "ORDER BY start_time",
                (meeting_id, window_start),
            )
            rows = await cursor.fetchall()
        if not rows:
            return
        recent = "\n".join(f"[{_fmt_t(r['start_time'])}] {r['speaker']}: {r['text']}" for r in rows)

        prompt = self._render_prompt(
            self_speaker=MY_SPEAKER_LABEL,
            existing_highlights=state.highlights,
            existing_action_items_self=state.action_items_self,
            existing_action_items_others=state.action_items_others,
            recent_transcript=recent,
        )

        log.info("realtime intel: %d utterances in window, calling LLM", len(rows))
        raw = await chat(prompt)
        state.last_run_ts = time.monotonic()
        state.consecutive_failures = 0

        parsed = _parse_json(raw)
        if parsed is None:
            log.warning("Realtime intel: LLM returned unparseable JSON; first 200 chars: %r", raw[:200])
            return

        new_highlights = _coerce_str_list(parsed.get("new_highlights"))
        new_self = _coerce_str_list(parsed.get("new_action_items_self"))
        new_others = _coerce_other_list(parsed.get("new_action_items_others"))
        support = _coerce_str(parsed.get("support_intelligence"))

        added_highlights, added_self, added_others = self._merge(
            state, new_highlights, new_self, new_others
        )
        # Support intelligence: live UI shows only the latest, but every
        # non-empty push is appended to the history (persisted to DB and
        # rendered into the Obsidian markdown). Skip verbatim-identical
        # back-to-back entries — that's just LLM thrashing, not signal.
        support_appended = False
        if support:
            state.support_intelligence = support
            last = state.support_intelligence_history[-1] if state.support_intelligence_history else None
            if not last or last.get("text", "").strip() != support.strip():
                state.support_intelligence_history.append({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "text": support,
                })
                support_appended = True

        await self._persist(meeting_id, state)
        # Refresh the Obsidian file immediately so the markdown reflects the
        # new intel without waiting for the next utterance chunk write.
        await self._refresh_vault(meeting_id)
        await self._broadcast_state(meeting_id, state, {
            "added_highlights": added_highlights,
            "added_action_items_self": added_self,
            "added_action_items_others": added_others,
            "support_intelligence_changed": bool(support),
            "support_intelligence_appended": support_appended,
        })

    def _render_prompt(
        self,
        *,
        self_speaker: str,
        existing_highlights: list[str],
        existing_action_items_self: list[str],
        existing_action_items_others: list[dict],
        recent_transcript: str,
    ) -> str:
        prompt_path = _ensure_prompt_file()
        try:
            template = prompt_path.read_text(encoding="utf-8")
        except Exception as e:
            log.warning("Could not read user prompt file %s: %s — using bundled default",
                        prompt_path, e)
            template = _BUNDLED_DEFAULT.read_text(encoding="utf-8")

        def fmt_list(items: list) -> str:
            if not items:
                return "(none yet)"
            if items and isinstance(items[0], dict):
                return "\n".join(f"- {i.get('speaker', '?')}: {i.get('item', '')}" for i in items)
            return "\n".join(f"- {i}" for i in items)

        # Use str.replace for {placeholders} so JSON-schema braces in the
        # prompt body don't trip str.format.
        return (
            template
            .replace("{self_speaker}", self_speaker)
            .replace("{existing_highlights}", fmt_list(existing_highlights))
            .replace("{existing_action_items_self}", fmt_list(existing_action_items_self))
            .replace("{existing_action_items_others}", fmt_list(existing_action_items_others))
            .replace("{recent_transcript}", recent_transcript)
        )

    def _merge(
        self,
        state: _MeetingState,
        new_highlights: list[str],
        new_self: list[str],
        new_others: list[dict],
    ) -> tuple[list[str], list[str], list[dict]]:
        added_h: list[str] = []
        for h in new_highlights:
            key = _norm(h)
            if key and key not in state._highlight_keys:
                state._highlight_keys.add(key)
                state.highlights.append(h)
                added_h.append(h)

        added_s: list[str] = []
        for s in new_self:
            key = _norm(s)
            if key and key not in state._self_keys:
                state._self_keys.add(key)
                state.action_items_self.append(s)
                added_s.append(s)

        added_o: list[dict] = []
        for item in new_others:
            key = _norm_action_other(item)
            if key and key not in state._other_keys:
                state._other_keys.add(key)
                state.action_items_others.append(item)
                added_o.append(item)

        return added_h, added_s, added_o

    async def _persist(self, meeting_id: str, state: _MeetingState) -> None:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE meetings SET "
                "live_highlights = ?, "
                "live_action_items_self = ?, "
                "live_action_items_others = ?, "
                "live_support_intelligence = ?, "
                "live_support_intelligence_history = ? "
                "WHERE id = ?",
                (
                    json.dumps(state.highlights),
                    json.dumps(state.action_items_self),
                    json.dumps(state.action_items_others),
                    state.support_intelligence or None,
                    json.dumps(state.support_intelligence_history)
                    if state.support_intelligence_history else None,
                    meeting_id,
                ),
            )
            await db.commit()

    async def _refresh_vault(self, meeting_id: str) -> None:
        """Best-effort Obsidian rewrite after a successful intel run. Imported
        lazily because the writer module pulls in vault paths that may not be
        configured (Obsidian disabled = no-op)."""
        try:
            from aurascribe.obsidian.writer import rewrite_meeting_vault

            await rewrite_meeting_vault(meeting_id)
        except Exception as e:
            log.warning("Vault refresh from realtime intel failed: %s", e)

    async def _broadcast_state(
        self, meeting_id: str, state: _MeetingState, deltas: dict
    ) -> None:
        if self._broadcast is None:
            return
        await self._broadcast({
            "type": "realtime_intelligence",
            "meeting_id": meeting_id,
            "highlights": state.highlights,
            "action_items_self": state.action_items_self,
            "action_items_others": state.action_items_others,
            "support_intelligence": state.support_intelligence,
            "deltas": deltas,
        })


# ── Helpers ──────────────────────────────────────────────────────────────────


def _fmt_t(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _safe_json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _parse_json(raw: str) -> dict | None:
    """Tolerate fenced/naked JSON; pull the outermost {...} if both fail."""
    if not raw:
        return None
    candidate = _FENCE_RE.sub("", raw).strip()
    try:
        v = json.loads(candidate)
        return v if isinstance(v, dict) else None
    except Exception:
        pass
    # Last-ditch: grab the first {...} block.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        try:
            v = json.loads(candidate[start : end + 1])
            return v if isinstance(v, dict) else None
        except Exception:
            return None
    return None


def _coerce_str_list(v) -> list[str]:
    if not isinstance(v, list):
        return []
    out: list[str] = []
    for item in v:
        if isinstance(item, str):
            s = item.strip()
            if s:
                out.append(s)
        elif isinstance(item, dict) and "item" in item:
            s = str(item["item"]).strip()
            if s:
                out.append(s)
    return out


def _coerce_other_list(v) -> list[dict]:
    if not isinstance(v, list):
        return []
    out: list[dict] = []
    for item in v:
        if isinstance(item, dict):
            speaker = str(item.get("speaker") or "").strip() or "Unknown"
            text = str(item.get("item") or "").strip()
            if text:
                out.append({"speaker": speaker, "item": text})
        elif isinstance(item, str) and item.strip():
            out.append({"speaker": "Unknown", "item": item.strip()})
    return out


def _coerce_str(v) -> str:
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, list):
        return "\n".join(f"- {s}" for s in v if isinstance(s, str) and s.strip())
    return ""
