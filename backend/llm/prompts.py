"""
All LLM prompt templates for AuraScribe.
"""
from backend.transcription.engine import Utterance


def format_transcript(utterances: list[Utterance]) -> str:
    lines = []
    for u in utterances:
        timestamp = f"[{_fmt_time(u.start)} → {_fmt_time(u.end)}]"
        lines.append(f"{timestamp} **{u.speaker}**: {u.text}")
    return "\n".join(lines)


def _fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


MEETING_SUMMARY_SYSTEM = """You are AuraScribe, an expert meeting analyst.
You extract structured information from meeting transcripts.
Be concise, factual, and actionable. Use markdown formatting."""


def meeting_summary_prompt(transcript: str, meeting_title: str = "") -> str:
    title_line = f"Meeting: {meeting_title}\n\n" if meeting_title else ""
    return f"""{title_line}Transcript:
{transcript}

---
Provide a structured meeting summary with these exact sections:

## Summary
2-3 sentence overview of what was discussed and decided.

## Key Decisions
Bullet list of decisions made. If none, write "None."

## Action Items
Bullet list in format: "- [ ] [Person] — [action] (by [date if mentioned])"
If no actions, write "None."

## Key Topics
Comma-separated list of main topics discussed.

## People Mentioned
List each person mentioned with a one-line description of their role/relevance in this meeting."""


def people_notes_prompt(person_name: str, existing_notes: str, new_transcript_excerpt: str) -> str:
    return f"""Update the notes for {person_name} based on this new meeting excerpt.

Existing notes:
{existing_notes or "(none yet)"}

New meeting excerpt mentioning {person_name}:
{new_transcript_excerpt}

Return updated notes covering:
- Role/title (if known)
- Key opinions or positions expressed
- Commitments or action items they took on
- Important context for future interactions

Be concise. Keep existing accurate information. Return only the updated notes content."""


