"""Writes meetings, people notes, and daily briefs into the Obsidian vault.

If `OBSIDIAN_VAULT` is not configured, the writer returns None and the rest of
the app continues — transcripts still land in SQLite.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path

import aiofiles
import aiosqlite

from aurascribe.config import DB_PATH, VAULT_MEETINGS, VAULT_PEOPLE
from aurascribe.llm.prompts import format_transcript
from aurascribe.transcription import Utterance

log = logging.getLogger("aurascribe.obsidian")


def _slug(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-").lower()


def _ensure_dirs() -> bool:
    if VAULT_MEETINGS is None or VAULT_PEOPLE is None:
        return False
    VAULT_MEETINGS.mkdir(parents=True, exist_ok=True)
    VAULT_PEOPLE.mkdir(parents=True, exist_ok=True)
    return True


def cleanup_vault_stragglers() -> int:
    """Delete zero-byte meeting files left behind by crashed/aborted writes.

    Returns the number of files removed. Safe to call on startup — if the
    vault isn't configured, this is a no-op.
    """
    if VAULT_MEETINGS is None or not VAULT_MEETINGS.exists():
        return 0
    removed = 0
    for path in VAULT_MEETINGS.glob("*.md"):
        try:
            if path.stat().st_size == 0:
                path.unlink()
                removed += 1
        except Exception as e:
            log.warning("Could not clean up %s: %s", path, e)
    if removed:
        log.info("Removed %d zero-byte straggler(s) from %s", removed, VAULT_MEETINGS)
    return removed


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
    filename = f"{date_str} {title}.md"
    path = VAULT_MEETINGS / filename

    speakers = list({u.speaker for u in utterances})
    # "Speaker N" is a provisional placeholder, not a real person — don't link.
    people_links = ", ".join(
        f"[[People/{s}]]" for s in speakers
        if s != "Me" and not re.match(r"^Speaker \d+$", s) and s != "Unknown"
    )
    transcript_md = format_transcript(utterances)

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

    return path


async def update_person_note(person_name: str, updated_notes: str, meeting_title: str) -> Path | None:
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
