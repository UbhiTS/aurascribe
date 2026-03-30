"""
Writes meeting transcripts, summaries, people notes, and daily briefs
directly into the Obsidian vault as markdown files.
"""
import aiofiles
import aiosqlite
import re
from datetime import datetime, date
from pathlib import Path

from backend.config import (
    VAULT_MEETINGS, VAULT_PEOPLE, DB_PATH
)
from backend.transcription.engine import Utterance
from backend.llm.prompts import format_transcript


def _slug(text: str) -> str:
    """Convert text to a safe filename."""
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-").lower()


def _ensure_dirs():
    VAULT_MEETINGS.mkdir(parents=True, exist_ok=True)
    VAULT_PEOPLE.mkdir(parents=True, exist_ok=True)


async def write_meeting(
    meeting_id: int,
    title: str,
    started_at: datetime,
    utterances: list[Utterance],
    summary: str,
    action_items: list[str],
) -> Path:
    _ensure_dirs()
    date_str = started_at.strftime("%Y-%m-%d")
    time_str = started_at.strftime("%H:%M")
    safe_title = _slug(title)
    filename = f"{date_str} {title}.md"
    path = VAULT_MEETINGS / filename

    # Build people wikilinks from speakers
    speakers = list({u.speaker for u in utterances})
    people_links = ", ".join(f"[[People/{s}]]" for s in speakers if s != "Me")

    # Format transcript
    transcript_md = format_transcript(utterances)

    content = f"""---
date: {date_str}
time: {time_str}
meeting_id: {meeting_id}
tags: [meeting, aurascribe]
people: [{', '.join(speakers)}]
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

    # Update DB with vault path
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE meetings SET vault_path = ? WHERE id = ?",
            (str(path), meeting_id),
        )
        await db.commit()

    return path


async def update_person_note(person_name: str, updated_notes: str, meeting_title: str) -> Path:
    """Create or update a person's note in the vault."""
    _ensure_dirs()
    path = VAULT_PEOPLE / f"{person_name}.md"

    # Read existing file if it exists
    existing = ""
    if path.exists():
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            existing = await f.read()

    # Extract existing meetings list from frontmatter if present
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


