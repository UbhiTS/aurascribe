"""Writes meetings, people notes, and daily briefs into the Obsidian vault.

If `OBSIDIAN_VAULT` is not configured, the writer returns None and the rest of
the app continues — transcripts still land in SQLite.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path

import aiofiles
import aiosqlite

from aurascribe.config import DB_PATH, VAULT_DAILY, VAULT_MEETINGS, VAULT_PEOPLE
from aurascribe.llm.prompts import format_transcript
from aurascribe.transcription import Utterance

log = logging.getLogger("aurascribe.obsidian")

# ── Per-meeting write throttle ──────────────────────────────────────────────
#
# Both the record loop (chunk-driven) and the realtime intel loop
# (LLM-driven) ultimately call write_meeting → which means we can put the
# throttle counters here once and have both code paths share them. The
# manager checks `time_since_write` and `chunks_since_write` to decide
# whether to skip a write; intel rewrites always go through and reset both.
_last_write_ts: dict[str, float] = {}
_chunks_since_write: dict[str, int] = {}


def note_chunk_arrived(meeting_id: str) -> int:
    """Bump the unwritten-chunk counter; return the new value."""
    _chunks_since_write[meeting_id] = _chunks_since_write.get(meeting_id, 0) + 1
    return _chunks_since_write[meeting_id]


def time_since_write(meeting_id: str) -> float:
    """Seconds since the last successful write to this meeting's vault file.
    Returns infinity if we haven't written yet — first chunk always writes."""
    last = _last_write_ts.get(meeting_id)
    if last is None:
        return float("inf")
    return time.monotonic() - last


def chunks_since_write(meeting_id: str) -> int:
    return _chunks_since_write.get(meeting_id, 0)


def forget_meeting_throttle(meeting_id: str) -> None:
    """Drop throttle state for a meeting. Call on finalize/stop."""
    _last_write_ts.pop(meeting_id, None)
    _chunks_since_write.pop(meeting_id, None)


def _note_write(meeting_id: str) -> None:
    _last_write_ts[meeting_id] = time.monotonic()
    _chunks_since_write[meeting_id] = 0


def _slug(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-").lower()


# Characters forbidden in Windows filenames (plus control chars). Stripped
# from titles before they land on disk so defaults like
# `"Transcription 2026-04-18 14:30"` (which contains a colon) don't silently
# create NTFS alternate-data-stream oddities.
_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename_part(s: str) -> str:
    cleaned = _ILLEGAL_FILENAME_CHARS.sub("-", s).strip().rstrip(".")
    # Collapse runs of dashes/whitespace that the substitution can leave.
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "untitled"


def meeting_file_path(started_at: datetime, title: str) -> Path | None:
    """Canonical path for a meeting's vault file:
    `Meetings/YYYY/MM/YYYY-MM-DD HH-MM-SS <Title>.md`.

    Returns None when Obsidian isn't configured. Shared by the writer and
    by meeting_manager's mid-recording rename cleanup so both agree on
    where a given meeting lives."""
    if VAULT_MEETINGS is None:
        return None
    year = started_at.strftime("%Y")
    month = started_at.strftime("%m")
    stem = (
        f"{started_at.strftime('%Y-%m-%d %H-%M-%S')} "
        f"{_safe_filename_part(title)}"
    )
    return VAULT_MEETINGS / year / month / f"{stem}.md"


def meeting_vault_link(started_at: datetime, title: str) -> str | None:
    """Obsidian wikilink target for a meeting (no .md extension, vault-relative).
    Example: `Meetings/2026/04/2026-04-18 14-30-15 Kickoff`.
    Returns None when the vault isn't configured."""
    path = meeting_file_path(started_at, title)
    if path is None or VAULT_MEETINGS is None:
        return None
    rel = path.relative_to(VAULT_MEETINGS.parent).with_suffix("")
    return rel.as_posix()


def daily_brief_file_path(brief_date: str) -> Path | None:
    """Canonical path for a daily brief: `Daily/YYYY/MM/YYYY-MM-DD.md`.
    Returns None when Obsidian isn't configured."""
    if VAULT_DAILY is None:
        return None
    year, month = brief_date[:4], brief_date[5:7]
    return VAULT_DAILY / year / month / f"{brief_date}.md"


def _ensure_dirs() -> bool:
    if VAULT_MEETINGS is None or VAULT_PEOPLE is None:
        return False
    VAULT_MEETINGS.mkdir(parents=True, exist_ok=True)
    VAULT_PEOPLE.mkdir(parents=True, exist_ok=True)
    return True


def cleanup_vault_stragglers() -> int:
    """Delete zero-byte meeting files left behind by crashed/aborted writes.

    Returns the number of files removed. Safe to call on startup — if the
    vault isn't configured, this is a no-op. Walks recursively now that
    meetings are nested under YYYY/MM/.
    """
    total = 0
    for root in (VAULT_MEETINGS, VAULT_DAILY):
        if root is None or not root.exists():
            continue
        for path in root.rglob("*.md"):
            try:
                if path.stat().st_size == 0:
                    path.unlink()
                    total += 1
            except Exception as e:
                log.warning("Could not clean up %s: %s", path, e)
    if total:
        log.info("Removed %d zero-byte straggler(s) from vault", total)
    return total


async def write_meeting(
    meeting_id: int,
    title: str,
    started_at: datetime,
    utterances: list[Utterance],
    summary: str,
    action_items: list[str],
) -> Path | None:
    if not _ensure_dirs():
        return None
    assert VAULT_MEETINGS is not None

    date_str = started_at.strftime("%Y-%m-%d")
    time_str = started_at.strftime("%H:%M")
    path = meeting_file_path(started_at, title)
    assert path is not None  # _ensure_dirs guarantees VAULT_MEETINGS
    path.parent.mkdir(parents=True, exist_ok=True)

    speakers = list({u.speaker for u in utterances})
    # "Speaker N" is a provisional placeholder, not a real person — don't link.
    people_links = ", ".join(
        f"[[People/{s}]]" for s in speakers
        if s != "Me" and not re.match(r"^Speaker \d+$", s) and s != "Unknown"
    )
    transcript_md = format_transcript(utterances)

    # Live intelligence (highlights, action items, support intelligence
    # history) is sourced from DB rather than function args so every call site
    # — record-loop chunk writes, finalize, intel-loop refreshes — picks it up
    # automatically without plumbing the data through.
    live_intel_md = await _render_live_intel_section(meeting_id)

    content = f"""---
date: {date_str}
time: {time_str}
meeting_id: {meeting_id}
tags: [meeting, aurascribe]
people: [{", ".join(speakers)}]
---

# {title}

> Recorded {date_str} at {time_str}
> Participants: {people_links or "Solo"}

{summary}
{live_intel_md}
---

## Transcript

{transcript_md}
"""

    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(content)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE meetings SET vault_path = ? WHERE id = ?",
            (str(path), meeting_id),
        )
        await db.commit()

    # Mark this meeting as freshly written — both the chunk loop and the
    # intel loop check this to decide whether to skip a redundant write.
    _note_write(meeting_id)
    return path


async def _render_live_intel_section(meeting_id: str) -> str:
    """Pull the four live-intel columns from the meetings row and render them
    as markdown. Returns "" if nothing has been captured yet, so the meeting
    file stays tidy on solo recordings or when LMStudio isn't running."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT live_highlights, live_action_items_self, "
            "live_action_items_others, live_support_intelligence_history "
            "FROM meetings WHERE id = ?",
            (meeting_id,),
        )
        row = await cursor.fetchone()
    if not row:
        return ""

    highlights = _safe_json_list(row["live_highlights"])
    action_self = _safe_json_list(row["live_action_items_self"])
    action_others = _safe_json_list(row["live_action_items_others"])
    history = _safe_json_list(row["live_support_intelligence_history"])

    if not (highlights or action_self or action_others or history):
        return ""

    parts: list[str] = ["", "---", "", "## Live Intelligence", ""]

    if highlights:
        parts.append("### Real-Time Highlights")
        parts.append("")
        parts.extend(f"- {h}" for h in highlights)
        parts.append("")

    if action_self or action_others:
        parts.append("### Action Items (Live)")
        parts.append("")
        if action_self:
            parts.append("**You:**")
            parts.extend(f"- [ ] {item}" for item in action_self)
            parts.append("")
        if action_others:
            parts.append("**Others:**")
            for entry in action_others:
                if isinstance(entry, dict):
                    speaker = entry.get("speaker", "Unknown")
                    item = entry.get("item", "")
                    parts.append(f"- [ ] **{speaker}:** {item}")
            parts.append("")

    if history:
        parts.append("### Support Intelligence Suggestions")
        parts.append("")
        parts.append("> Chronological — each block was suggested at the time shown.")
        parts.append("")
        for entry in history:
            if not isinstance(entry, dict):
                continue
            ts = entry.get("ts", "")
            text = (entry.get("text") or "").strip()
            if not text:
                continue
            # Try to parse to HH:MM:SS for display; fall back to whatever we stored.
            display_ts = ts
            try:
                display_ts = datetime.fromisoformat(ts).strftime("%H:%M:%S")
            except Exception:
                pass
            parts.append(f"#### {display_ts}")
            parts.append("")
            parts.append(text)
            parts.append("")

    return "\n".join(parts)


def _safe_json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except Exception:
        return []


async def rewrite_meeting_vault(meeting_id: str) -> Path | None:
    """Reload everything for `meeting_id` from the DB and rewrite its vault
    file. Used by the realtime intel loop (refresh markdown immediately after
    a successful run) and by post-edit endpoints in api.py.

    Returns None if Obsidian isn't configured or the meeting doesn't exist.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT title, started_at, summary, action_items "
            "FROM meetings WHERE id = ?",
            (meeting_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        title = row["title"]
        started_at = datetime.fromisoformat(row["started_at"])
        summary = row["summary"] or ""
        try:
            action_items = json.loads(row["action_items"]) if row["action_items"] else []
        except Exception:
            action_items = []
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
    return await write_meeting(
        meeting_id=meeting_id,
        title=title,
        started_at=started_at,
        utterances=utterances,
        summary=summary,
        action_items=action_items,
    )


async def write_daily_brief(
    brief_date: str,
    brief: dict,
    meetings_meta: list[dict],
    generated_at: str,
) -> Path | None:
    """Write a Daily Brief markdown file into `Daily/YYYY/MM/YYYY-MM-DD.md`.

    `brief` matches the schema returned by `llm.daily_brief.build_brief` —
    tldr, highlights, decisions, action_items_self, action_items_others,
    open_threads, people, themes, tomorrow_focus, coaching. `meetings_meta`
    is a list of dicts with keys `title` + `started_at` (ISO) for the
    meetings this brief covers — used to produce wikilinks back to each
    meeting file under `Meetings/YYYY/MM/...`.

    No-op when Obsidian isn't configured."""
    path = daily_brief_file_path(brief_date)
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        d = datetime.strptime(brief_date, "%Y-%m-%d")
        human_date = d.strftime("%A, %B %d, %Y")
    except Exception:
        human_date = brief_date

    meeting_count = len(meetings_meta)
    tldr = (brief.get("tldr") or "").strip()
    highlights = brief.get("highlights") or []
    decisions = brief.get("decisions") or []
    ai_self = brief.get("action_items_self") or []
    ai_others = brief.get("action_items_others") or []
    open_threads = brief.get("open_threads") or []
    people = brief.get("people") or []
    themes = brief.get("themes") or []
    tomorrow = brief.get("tomorrow_focus") or []
    coaching = brief.get("coaching") or []

    parts: list[str] = [
        "---",
        f"date: {brief_date}",
        "type: daily-brief",
        f"meetings: {meeting_count}",
        f"generated_at: {generated_at}",
        "tags: [daily-brief, aurascribe]",
        "---",
        "",
        f"# Daily Brief — {brief_date}",
        "",
        f"> {human_date} · "
        f"{meeting_count} meeting{'s' if meeting_count != 1 else ''}",
        "",
    ]

    if tldr:
        parts += ["## TL;DR", "", tldr, ""]

    if tomorrow:
        parts += ["## Tomorrow's Focus", ""]
        parts += [f"{i + 1}. {t}" for i, t in enumerate(tomorrow)]
        parts += [""]

    if ai_self:
        parts += ["## Action Items — You", ""]
        for a in ai_self:
            if not isinstance(a, dict):
                continue
            item = (a.get("item") or "").strip()
            if not item:
                continue
            priority = (a.get("priority") or "medium").lower()
            due = (a.get("due") or "").strip()
            source = (a.get("source") or "").strip()
            meta_bits = []
            if due:
                meta_bits.append(f"Due {due}")
            if source:
                meta_bits.append(f"_{source}_")
            meta = f" — {' — '.join(meta_bits)}" if meta_bits else ""
            parts.append(f"- [ ] **[{priority}]** {item}{meta}")
        parts.append("")

    if ai_others:
        parts += ["## Owed to You", ""]
        for a in ai_others:
            if not isinstance(a, dict):
                continue
            speaker = (a.get("speaker") or "Unknown").strip() or "Unknown"
            item = (a.get("item") or "").strip()
            if not item:
                continue
            due = (a.get("due") or "").strip()
            source = (a.get("source") or "").strip()
            meta_bits = []
            if due:
                meta_bits.append(f"Due {due}")
            if source:
                meta_bits.append(f"_{source}_")
            meta = f" — {' — '.join(meta_bits)}" if meta_bits else ""
            parts.append(f"- [ ] **{speaker}** — {item}{meta}")
        parts.append("")

    if highlights:
        parts += ["## Highlights", ""]
        parts += [f"- {h}" for h in highlights if isinstance(h, str) and h.strip()]
        parts.append("")

    if decisions:
        parts += ["## Decisions", ""]
        for dec in decisions:
            if not isinstance(dec, dict):
                continue
            d_text = (dec.get("decision") or "").strip()
            c_text = (dec.get("context") or "").strip()
            if not d_text:
                continue
            if c_text:
                parts.append(f"- **{d_text}** — {c_text}")
            else:
                parts.append(f"- **{d_text}**")
        parts.append("")

    if open_threads:
        parts += ["## Open Threads", ""]
        parts += [f"- {t}" for t in open_threads if isinstance(t, str) and t.strip()]
        parts.append("")

    if people:
        parts += ["## People", ""]
        for p in people:
            if not isinstance(p, dict):
                continue
            name = (p.get("name") or "").strip()
            takeaway = (p.get("takeaway") or "").strip()
            if not name:
                continue
            # Link to the People note if the vault is configured.
            name_link = f"[[People/{name}|{name}]]" if VAULT_PEOPLE else name
            parts.append(f"- **{name_link}** — {takeaway}" if takeaway else f"- **{name_link}**")
        parts.append("")

    if coaching:
        parts += ["## Coaching", ""]
        parts += [f"- {c}" for c in coaching if isinstance(c, str) and c.strip()]
        parts.append("")

    if themes:
        parts += ["## Themes", ""]
        tag_line = " ".join(
            "#" + re.sub(r"\s+", "-", t.strip().lower())
            for t in themes
            if isinstance(t, str) and t.strip()
        )
        parts += [tag_line, ""]

    # Link back to each meeting file for easy navigation.
    if meetings_meta:
        parts += ["## Meetings", ""]
        for m in meetings_meta:
            title = (m.get("title") or "Untitled").strip()
            started_raw = m.get("started_at") or ""
            try:
                started_dt = datetime.fromisoformat(started_raw)
            except Exception:
                started_dt = None
            link = meeting_vault_link(started_dt, title) if started_dt else None
            if link:
                parts.append(f"- [[{link}|{title}]]")
            else:
                parts.append(f"- {title}")
        parts.append("")

    content = "\n".join(parts).rstrip() + "\n"
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(content)
    return path


async def update_person_note(
    person_name: str,
    updated_notes: str,
    meeting_title: str,
    meeting_started_at: datetime | None = None,
) -> Path | None:
    if not _ensure_dirs():
        return None
    assert VAULT_PEOPLE is not None

    path = VAULT_PEOPLE / f"{person_name}.md"

    existing = ""
    if path.exists():
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            existing = await f.read()

    meetings_section = ""
    if "## Meetings" in existing:
        parts = existing.split("## Meetings")
        meetings_section = "## Meetings" + parts[1] if len(parts) > 1 else ""

    today = date.today().isoformat()
    link_target = (
        meeting_vault_link(meeting_started_at, meeting_title)
        if meeting_started_at
        else None
    )
    if link_target:
        meeting_link = f"- [[{link_target}|{meeting_title}]]"
    else:
        # Fallback for callers that didn't pass a started_at — preserves the
        # pre-restructure link shape so existing notes keep resolving.
        meeting_link = f"- [[Meetings/{today} {meeting_title}]]"

    if meetings_section:
        meetings_section = meetings_section.rstrip() + f"\n{meeting_link}\n"
    else:
        meetings_section = f"\n## Meetings\n{meeting_link}\n"

    content = f"""---
name: {person_name}
tags: [person, aurascribe]
last_updated: {today}
---

# {person_name}

## Notes
{updated_notes}
{meetings_section}"""

    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(content)

    return path
