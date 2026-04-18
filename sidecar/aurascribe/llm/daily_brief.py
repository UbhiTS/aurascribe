"""Daily Brief — aggregate a full day of meetings into a single briefing.

The brief is persisted in the `daily_briefs` table keyed by `YYYY-MM-DD`,
so opening the Daily Brief page is instant (no LLM call on every visit).
Regeneration happens:
  - automatically after a meeting ends (the date it belongs to is marked
    stale and rebuilt in the background)
  - on user demand via POST /api/daily-brief/refresh

Input shape to the LLM: one block per meeting with title, time, duration,
participants, and either (summary + live highlights) or a truncated raw
transcript. We prefer the already-distilled fields when present — a day
with five 90-minute meetings would blow local context otherwise.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import aiosqlite

from aurascribe.config import (
    DB_PATH,
    LM_STUDIO_CONTEXT_TOKENS,
    MY_SPEAKER_LABEL,
    PROMPTS_DIR,
)
from aurascribe.llm.client import LLMUnavailableError, chat

log = logging.getLogger("aurascribe.daily_brief")

PROMPT_FILENAME = "daily_brief.md"
# User-editable copy lives in APP_DATA/prompts (seeded by config.py).
# Bundled package copy is the read-only fallback.
_USER_PROMPT = PROMPTS_DIR / PROMPT_FILENAME
_BUNDLED_DEFAULT = Path(__file__).resolve().parent / PROMPT_FILENAME

# Rough chars-per-token for English text. Slight overestimate is safer than
# underestimate since we size transcript budgets off this.
_CHARS_PER_TOKEN = 3.5

# Output cap for the daily brief LLM call. A large brief (decisions,
# per-meeting action items, people takeaways, open threads, tomorrow's
# focus) across many meetings needs generous room — the default `chat()`
# cap of 2048 truncates mid-JSON. We reserve ~8% of the context window
# for output, capped sensibly so we don't starve the input on tiny models.
_DAILY_BRIEF_MAX_TOKENS = max(4096, min(16384, int(LM_STUDIO_CONTEXT_TOKENS * 0.08)))

# Reserve for the prompt template, per-meeting headers, summaries, live
# highlights, action items, and the model's own thinking/output. What's
# left of the context budget goes to raw transcript excerpts.
_PROMPT_OVERHEAD_TOKENS = 4000
_INPUT_BUDGET_TOKENS = max(
    2000,
    LM_STUDIO_CONTEXT_TOKENS - _DAILY_BRIEF_MAX_TOKENS - _PROMPT_OVERHEAD_TOKENS,
)
_TOTAL_TRANSCRIPT_BUDGET = int(_INPUT_BUDGET_TOKENS * _CHARS_PER_TOKEN)
_MIN_PER_MEETING_TRANSCRIPT = 2000
# Below this per-meeting budget, transcripts get too clipped to be useful —
# drop them entirely and rely on the already-distilled summary + live
# highlights + action items instead.
_DROP_TRANSCRIPTS_WHEN_BUDGET_BELOW = 1500

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)

# Default empty structure — used when the day has zero meetings. Keeps the
# frontend render path uniform (no special-case).
EMPTY_BRIEF: dict = {
    "tldr": "",
    "highlights": [],
    "decisions": [],
    "action_items_self": [],
    "action_items_others": [],
    "open_threads": [],
    "people": [],
    "themes": [],
    "tomorrow_focus": [],
    "coaching": [],
}


# ── Public API ──────────────────────────────────────────────────────────────


async def get_cached(brief_date: str) -> dict | None:
    """Return the persisted row for `brief_date` as a dict, or None if absent.

    Shape: {date, brief, meeting_ids, meeting_count, generated_at, is_stale}.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT date, brief_json, meeting_ids, meeting_count, generated_at, is_stale "
            "FROM daily_briefs WHERE date = ?",
            (brief_date,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    try:
        brief = json.loads(row["brief_json"]) if row["brief_json"] else None
    except Exception:
        brief = None
    try:
        meeting_ids = json.loads(row["meeting_ids"]) if row["meeting_ids"] else []
    except Exception:
        meeting_ids = []
    return {
        "date": row["date"],
        "brief": brief,
        "meeting_ids": meeting_ids,
        "meeting_count": row["meeting_count"],
        "generated_at": row["generated_at"],
        "is_stale": bool(row["is_stale"]),
    }


async def mark_stale(brief_date: str) -> None:
    """Flag the brief for `brief_date` as stale. Creates a placeholder row if
    none exists — lets the UI show 'stale, refresh pending' without a cold
    empty state. Idempotent."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO daily_briefs (date, meeting_ids, meeting_count, is_stale) "
            "VALUES (?, '[]', 0, 1) "
            "ON CONFLICT(date) DO UPDATE SET is_stale = 1",
            (brief_date,),
        )
        await db.commit()


async def build_brief(brief_date: str) -> dict:
    """Generate and persist the brief for `brief_date`. Returns the same
    shape as `get_cached`. Raises LLMUnavailableError if the LLM can't be
    reached (caller decides whether to expose as 503 or swallow)."""
    meetings = await _load_meetings_for_date(brief_date)
    meeting_ids = [m["id"] for m in meetings]
    generated_at = datetime.now().isoformat(timespec="seconds")

    if not meetings:
        # Empty-day shortcut — skip the LLM round-trip entirely.
        brief = dict(EMPTY_BRIEF)
        await _persist(brief_date, brief, meeting_ids, generated_at)
        # Don't write a markdown file for truly empty days — keeps the vault
        # tidy. The DB row is enough to remember "we checked, nothing there."
        return {
            "date": brief_date,
            "brief": brief,
            "meeting_ids": meeting_ids,
            "meeting_count": 0,
            "generated_at": generated_at,
            "is_stale": False,
        }

    prompt = _render_prompt(brief_date, meetings)
    raw = await chat(prompt, max_tokens=_DAILY_BRIEF_MAX_TOKENS)
    parsed = _parse_json(raw)
    if parsed is None:
        log.warning(
            "Daily brief LLM returned unparseable JSON for %s; first 200 chars: %r",
            brief_date, raw[:200],
        )
        # Persist a stub so the UI doesn't think generation never ran. The
        # user can hit Refresh to try again.
        brief = dict(EMPTY_BRIEF)
        brief["tldr"] = "(Brief generation failed to parse LLM output. Hit refresh to retry.)"
    else:
        brief = _normalize_brief(parsed)

    await _persist(brief_date, brief, meeting_ids, generated_at)

    # Mirror the brief into Obsidian. Best-effort — a vault write failure
    # must not poison the DB-persisted brief (the UI depends on that path).
    try:
        from aurascribe.obsidian.writer import write_daily_brief

        meetings_meta = [
            {"title": m.get("title"), "started_at": m.get("started_at")}
            for m in meetings
        ]
        await write_daily_brief(brief_date, brief, meetings_meta, generated_at)
    except Exception as e:
        log.warning("Daily brief vault write failed for %s: %s", brief_date, e)

    return {
        "date": brief_date,
        "brief": brief,
        "meeting_ids": meeting_ids,
        "meeting_count": len(meetings),
        "generated_at": generated_at,
        "is_stale": False,
    }


def date_of_iso(iso_ts: str) -> str:
    """Extract YYYY-MM-DD from a meeting's `started_at` ISO timestamp.

    Tolerant of fractional seconds and timezone suffixes — sidecar writes
    `datetime.now().isoformat()` which is naive local time; slicing is
    therefore correct for the local-day bucketing the user expects."""
    return iso_ts[:10]


def today_str() -> str:
    return date.today().isoformat()


# ── Internals ───────────────────────────────────────────────────────────────


async def _load_meetings_for_date(brief_date: str) -> list[dict]:
    """Return all meetings whose `started_at` local-date matches `brief_date`,
    with their utterances, sorted by start time.

    Uses a half-open date range on the ISO prefix. `started_at` is stored as
    naive-local ISO, so string comparison against `YYYY-MM-DD` boundaries is
    the correct bucketing here."""
    try:
        d = date.fromisoformat(brief_date)
    except ValueError:
        raise ValueError(f"Invalid date (expected YYYY-MM-DD): {brief_date}")
    next_day = (d + timedelta(days=1)).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, title, started_at, ended_at, status, summary, action_items, "
            "live_highlights, live_action_items_self, live_action_items_others, "
            "live_support_intelligence_history "
            "FROM meetings "
            "WHERE started_at >= ? AND started_at < ? AND status = 'done' "
            "ORDER BY started_at",
            (brief_date, next_day),
        )
        rows = await cursor.fetchall()

        meetings: list[dict] = []
        for row in rows:
            meeting = dict(row)
            cursor2 = await db.execute(
                "SELECT speaker, text, start_time, end_time FROM utterances "
                "WHERE meeting_id = ? ORDER BY start_time",
                (meeting["id"],),
            )
            meeting["utterances"] = [dict(u) async for u in cursor2]
            meetings.append(meeting)
    return meetings


def _render_prompt(brief_date: str, meetings: list[dict]) -> str:
    # Prefer the user-editable copy; fall back to the bundled default so a
    # deleted or momentarily-unreadable user file doesn't crash the brief.
    try:
        template = _USER_PROMPT.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("Could not read user daily_brief.md (%s): falling back to bundled default", e)
        try:
            template = _BUNDLED_DEFAULT.read_text(encoding="utf-8")
        except Exception as e2:
            log.error("Could not read bundled daily_brief.md: %s", e2)
            raise

    # Divide the total transcript budget evenly across meetings, with a floor.
    # If the per-meeting slice would fall below the drop threshold, skip raw
    # transcripts entirely — the already-distilled fields (summary, live
    # highlights, per-meeting action items) are high-signal enough on their
    # own, and the LLM does better with tight input than with heavily clipped
    # transcripts that lose context.
    n = max(1, len(meetings))
    per_meeting = _TOTAL_TRANSCRIPT_BUDGET // n
    include_transcripts = per_meeting >= _DROP_TRANSCRIPTS_WHEN_BUDGET_BELOW
    if include_transcripts:
        per_meeting = max(per_meeting, _MIN_PER_MEETING_TRANSCRIPT)
    log.info(
        "daily_brief: %d meetings → transcripts=%s, per_meeting_chars=%d",
        len(meetings), include_transcripts, per_meeting if include_transcripts else 0,
    )

    blocks = [
        _format_meeting_block(i + 1, m, per_meeting, include_transcripts)
        for i, m in enumerate(meetings)
    ]
    meetings_block = "\n\n---\n\n".join(blocks)

    return (
        template
        .replace("{self_speaker}", MY_SPEAKER_LABEL)
        .replace("{brief_date}", brief_date)
        .replace("{meeting_count}", str(len(meetings)))
        .replace("{meetings_block}", meetings_block)
    )


def _format_meeting_block(
    index: int, m: dict, per_meeting_chars: int, include_transcripts: bool
) -> str:
    started = m.get("started_at") or ""
    ended = m.get("ended_at") or ""
    duration_txt = _duration_phrase(started, ended)
    title = m.get("title") or "Untitled"
    start_hhmm = _hhmm(started)

    participants = sorted({
        u["speaker"] for u in m.get("utterances") or []
        if u.get("speaker") and u["speaker"] != "Unknown"
    })
    participants_line = ", ".join(participants) if participants else "(no speakers identified)"

    parts: list[str] = [
        f"### Meeting {index}: {title}",
        f"Start: {start_hhmm} · Duration: {duration_txt}",
        f"Participants: {participants_line}",
        "",
    ]

    summary = (m.get("summary") or "").strip()
    live_highlights = _safe_list(m.get("live_highlights"))
    live_ai_self = _safe_list(m.get("live_action_items_self"))
    live_ai_others = _safe_list(m.get("live_action_items_others"))

    # Prefer distilled fields when present — they're already high signal and
    # dramatically shrink the input over raw transcripts.
    used_distilled = False
    if summary:
        parts.append("**Summary (from this meeting):**")
        parts.append(summary)
        parts.append("")
        used_distilled = True
    if live_highlights:
        parts.append("**Live highlights captured during the meeting:**")
        parts.extend(f"- {h}" for h in live_highlights)
        parts.append("")
        used_distilled = True
    if live_ai_self:
        parts.append(f"**Action items already flagged for {MY_SPEAKER_LABEL}:**")
        parts.extend(f"- {it}" for it in live_ai_self)
        parts.append("")
        used_distilled = True
    if live_ai_others:
        parts.append("**Action items already flagged for others:**")
        for it in live_ai_others:
            if isinstance(it, dict):
                parts.append(f"- {it.get('speaker', '?')}: {it.get('item', '')}")
        parts.append("")
        used_distilled = True

    # Include a transcript excerpt only when budget allows. On high-volume
    # days we rely entirely on the distilled fields — an over-clipped
    # transcript loses context anyway, and if the meeting wasn't summarized
    # we at least still surface speakers + timing in the header.
    if include_transcripts:
        transcript_excerpt = _transcript_excerpt(m.get("utterances") or [], per_meeting_chars)
        if transcript_excerpt:
            label = "Transcript excerpt" if used_distilled else "Transcript"
            parts.append(f"**{label}:**")
            parts.append(transcript_excerpt)
    elif not used_distilled:
        # Edge case: no summary/highlights AND no transcript budget. Emit a
        # stub so the LLM at least knows this meeting happened.
        parts.append("_(meeting had no summary and transcript was elided for budget)_")

    return "\n".join(parts)


def _transcript_excerpt(utterances: list[dict], budget: int) -> str:
    """Build a budget-capped plain-text transcript. Uses head + tail sampling
    when over-budget so the LLM still sees how the meeting opened and closed
    (where commitments and decisions usually land)."""
    lines = [f"{_fmt_t(u['start_time'])} {u['speaker']}: {u['text']}" for u in utterances]
    full = "\n".join(lines)
    if len(full) <= budget:
        return full

    # Over budget — split between head and tail, keeping both landings.
    half = budget // 2
    head = full[: half - 50]
    tail = full[-(half - 50) :]
    # Snap to line boundaries so we don't show a half-speaker line.
    head = head[: head.rfind("\n")] if "\n" in head else head
    tail = tail[tail.find("\n") + 1 :] if "\n" in tail else tail
    return f"{head}\n\n[… {len(full) - half * 2} chars elided …]\n\n{tail}"


async def _persist(
    brief_date: str, brief: dict, meeting_ids: list[str], generated_at: str
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO daily_briefs (date, brief_json, meeting_ids, meeting_count, "
            "generated_at, is_stale) VALUES (?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(date) DO UPDATE SET "
            "brief_json = excluded.brief_json, "
            "meeting_ids = excluded.meeting_ids, "
            "meeting_count = excluded.meeting_count, "
            "generated_at = excluded.generated_at, "
            "is_stale = 0",
            (
                brief_date,
                json.dumps(brief),
                json.dumps(meeting_ids),
                len(meeting_ids),
                generated_at,
            ),
        )
        await db.commit()


# ── Parsing / normalization helpers ─────────────────────────────────────────


def _parse_json(raw: str) -> dict | None:
    """Best-effort JSON parse. Tolerates code fences, preamble/postamble,
    and JSON that was truncated mid-response (common when the LLM hits its
    max_tokens cap on a big daily brief)."""
    if not raw:
        return None
    candidate = _FENCE_RE.sub("", raw).strip()
    try:
        v = json.loads(candidate)
        return v if isinstance(v, dict) else None
    except Exception:
        pass
    # Grab the outermost {...} if there's a balanced pair.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        try:
            v = json.loads(candidate[start : end + 1])
            return v if isinstance(v, dict) else None
        except Exception:
            pass

    # Truncated-response recovery: the model ran out of tokens mid-value.
    # We've got a valid prefix up to some character — try closing any open
    # strings/arrays/objects and see if we can salvage *something* rather
    # than showing a failure card. Better half a brief than no brief.
    if start >= 0:
        repaired = _repair_truncated_json(candidate[start:])
        if repaired is not None:
            try:
                v = json.loads(repaired)
                return v if isinstance(v, dict) else None
            except Exception:
                return None
    return None


def _repair_truncated_json(text: str) -> str | None:
    """Attempt to close truncated JSON by tracking unclosed strings and
    container depth. Trims back to the last complete value before closing.
    Returns None when repair isn't feasible (e.g. the prefix isn't valid)."""
    # Walk the text tracking structural context. Trim the tail back to the
    # last complete key:value pair or element, then append closing brackets.
    in_string = False
    escape = False
    stack: list[str] = []   # "{" or "["
    # Last position at which the document was in a "between values" state —
    # i.e. safe to truncate here and then close containers.
    safe_cut = -1
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{" or ch == "[":
            stack.append(ch)
            safe_cut = i + 1  # after an opener, we can close immediately
        elif ch == "}" or ch == "]":
            if not stack:
                return None
            stack.pop()
            safe_cut = i + 1
        elif ch == "," and stack:
            # After a comma we're between elements — safe to chop and close,
            # but we need to drop the trailing comma before closing.
            safe_cut = i  # cut position excludes the comma
    if safe_cut <= 0 or not stack:
        return None
    head = text[:safe_cut].rstrip().rstrip(",")
    closer = "".join("}" if c == "{" else "]" for c in reversed(stack))
    return head + closer


def _normalize_brief(raw: dict) -> dict:
    """Coerce the LLM's JSON into the exact shape the frontend expects.

    Defensive because local LLMs occasionally drop fields, emit strings where
    arrays should be, or nest under an extra key. We favor "show what we got"
    over "show nothing" — fields missing end up as their empty default."""
    out: dict = dict(EMPTY_BRIEF)

    tldr = raw.get("tldr")
    if isinstance(tldr, str):
        out["tldr"] = tldr.strip()

    out["highlights"] = _str_list(raw.get("highlights"))

    decisions: list[dict] = []
    for item in _as_list(raw.get("decisions")):
        if isinstance(item, dict):
            d = str(item.get("decision") or "").strip()
            c = str(item.get("context") or "").strip()
            if d:
                decisions.append({"decision": d, "context": c})
        elif isinstance(item, str) and item.strip():
            decisions.append({"decision": item.strip(), "context": ""})
    out["decisions"] = decisions

    ai_self: list[dict] = []
    for item in _as_list(raw.get("action_items_self")):
        norm = _norm_action_item_self(item)
        if norm:
            ai_self.append(norm)
    out["action_items_self"] = ai_self

    ai_others: list[dict] = []
    for item in _as_list(raw.get("action_items_others")):
        norm = _norm_action_item_other(item)
        if norm:
            ai_others.append(norm)
    out["action_items_others"] = ai_others

    out["open_threads"] = _str_list(raw.get("open_threads"))

    people: list[dict] = []
    for item in _as_list(raw.get("people")):
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            takeaway = str(item.get("takeaway") or "").strip()
            if name:
                people.append({"name": name, "takeaway": takeaway})
    out["people"] = people

    out["themes"] = _str_list(raw.get("themes"))
    out["tomorrow_focus"] = _str_list(raw.get("tomorrow_focus"))
    out["coaching"] = _str_list(raw.get("coaching"))

    return out


_PRIORITIES = {"high", "medium", "low"}


def _norm_action_item_self(item) -> dict | None:
    if isinstance(item, str):
        s = item.strip()
        return {"item": s, "due": "", "source": "", "priority": "medium"} if s else None
    if not isinstance(item, dict):
        return None
    text = str(item.get("item") or "").strip()
    if not text:
        return None
    priority = str(item.get("priority") or "medium").lower().strip()
    if priority not in _PRIORITIES:
        priority = "medium"
    return {
        "item": text,
        "due": str(item.get("due") or "").strip(),
        "source": str(item.get("source") or "").strip(),
        "priority": priority,
    }


def _norm_action_item_other(item) -> dict | None:
    if isinstance(item, str):
        s = item.strip()
        return {"speaker": "Unknown", "item": s, "due": "", "source": ""} if s else None
    if not isinstance(item, dict):
        return None
    text = str(item.get("item") or "").strip()
    if not text:
        return None
    return {
        "speaker": str(item.get("speaker") or "Unknown").strip() or "Unknown",
        "item": text,
        "due": str(item.get("due") or "").strip(),
        "source": str(item.get("source") or "").strip(),
    }


def _as_list(v) -> list:
    return v if isinstance(v, list) else []


def _str_list(v) -> list[str]:
    out: list[str] = []
    for item in _as_list(v):
        if isinstance(item, str):
            s = item.strip()
            if s:
                out.append(s)
    return out


def _safe_list(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, list) else []
        except Exception:
            return []
    return []


def _fmt_t(seconds: float) -> str:
    m, s = divmod(int(seconds or 0), 60)
    h, m = divmod(m, 60)
    if h:
        return f"[{h:02d}:{m:02d}:{s:02d}]"
    return f"[{m:02d}:{s:02d}]"


def _hhmm(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except Exception:
        return "?"


def _duration_phrase(started: str, ended: str) -> str:
    try:
        s = datetime.fromisoformat(started)
        e = datetime.fromisoformat(ended)
        secs = max(0, int((e - s).total_seconds()))
    except Exception:
        return "?"
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m"
