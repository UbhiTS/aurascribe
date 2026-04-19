"""LLM prompt templates."""
from __future__ import annotations

from aurascribe.transcription import Utterance


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


MEETING_ANALYSIS_SYSTEM = """You are AuraScribe, an expert meeting analyst.
You read a meeting transcript and return TWO things in ONE response:

1. Three distinct title candidates for the meeting.
2. A structured markdown summary.

Output ONLY a single JSON object with this exact shape:
{
  "titles": ["…", "…", "…"],
  "summary_markdown": "## Summary\\n…\\n## Key Decisions\\n…"
}

Title rules:
- Exactly 3 distinct titles. At most 60 characters each. No trailing period.
- No quotes, no emoji, no filler like "Meeting about…" or "Discussion of…".
- Use Title Case.
- Prefer concrete nouns over vague verbs. Include the one or two most
  distinctive topics, projects, or decisions.
- Do NOT include dates, times, or speaker names.

summary_markdown MUST contain these exact sections in this order:

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
List each person mentioned with a one-line description of their role/relevance in this meeting.

Be concise, factual, and actionable. Escape newlines as \\n inside the
JSON string. Output ONLY the JSON object. No prose, no code fences."""


def meeting_analysis_prompt(*, transcript: str, current_title: str | None) -> str:
    parts: list[str] = []
    if current_title:
        parts.append(
            f"Current title (may be a placeholder like 'Untitled Meeting' "
            f"or 'Transcription <timestamp>'): {current_title}"
        )
    parts.append(f"Transcript:\n{transcript}")
    body = "\n\n".join(parts)
    return f"""{body}

---
Analyze this meeting. Return the JSON object with both `titles` and `summary_markdown`."""


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
