"""Combined meeting analysis — titles + summary from a single LLM call.

Both "AI Summary" and "Suggest Title" used to make their own LLM call
with essentially the same transcript. That doubled cost and latency
for a shared piece of work. This module collapses the two into a
single request that returns both artefacts, and exposes small helpers
for the two endpoints that consume them.

The LLM is asked for just the parts only it can derive from the
transcript:

  {
    "entity": "Acme Corp",            # customer / person / project / "Internal"
    "topics": ["…", "…", "…"],         # 3 distinct topic phrases
    "summary_markdown": "## Summary\\n…"
  }

…and the server stitches the final titles as
`{YYYY-MM-DD HH-MM-SS} - {entity} - {topic}` using the meeting's known
`started_at`. That's more reliable than asking the model to echo the
date (they often hallucinate the wrong day, drop seconds, or mix
timezones) and uses fewer output tokens.

A legacy shape `{"titles": ["…", "…", "…"]}` is still tolerated so
user-edited prompts that predate this change keep working — the
cleaned list is used as-is when the new-shape fields are absent.

Output format contract for summary_markdown is unchanged
(## Summary / ## Key Decisions / ## Action Items / ## Key Topics /
## People Mentioned), so the Obsidian writer + action-items extractor
keep working untouched.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aurascribe.config import LLM_CONTEXT_TOKENS, PROMPTS_DIR
from aurascribe.llm.client import LLMUnavailableError, chat
from aurascribe.llm.prompts import meeting_analysis_user_prompt
from aurascribe.llm.sampling import prepare_transcript

log = logging.getLogger("aurascribe.llm.analysis")

# Match any "Transcription …" title (the MeetingManager default), the
# "Auto-captured …" title the auto-capture monitor uses, or the DB-level
# "Untitled Meeting" default. Endpoints use this to decide whether to
# auto-apply a suggested title — we never clobber a title the user has
# already typed.
_PLACEHOLDER_RE = re.compile(
    r"^(transcription(\s|$)|auto-captured(\s|$)|untitled\s+meeting\s*$)",
    re.IGNORECASE,
)

# Output token budget. This call produces both the summary (markdown
# with several sections) AND three titles — so it needs the roomier
# summary budget, not the title-only one. Sized off context window:
# daily_brief uses 8%, summary currently uses 10%. We match summary.
# Clamped so tiny models don't starve input and huge models don't
# over-reserve.
MAX_OUTPUT_TOKENS = max(2048, min(16384, int(LLM_CONTEXT_TOKENS * 0.10)))

# User-editable prompt file + bundled fallback. Same two-step loader
# pattern as realtime.py / daily_brief.py — the user copy is checked
# first, and the bundled copy under llm/ is the safety net if the user
# file is missing or unreadable. Inline defaults catch the case where
# both files are unreachable (shouldn't happen, but better to degrade
# than crash).
PROMPT_FILENAME = "meeting_analysis.md"
_USER_PROMPT = PROMPTS_DIR / PROMPT_FILENAME
_BUNDLED_DEFAULT = Path(__file__).resolve().parent / PROMPT_FILENAME


@dataclass(frozen=True)
class AnalysisResult:
    """Parsed output of one combined analysis call.

    `titles` is the list of fully-composed titles ready to apply
    (`{timestamp} - {entity} - {topic}`), so endpoints don't need to
    know the composition rules. `entity` + `topics` are kept around in
    case a future UI wants to show them separately.
    """
    titles: list[str]
    summary_markdown: str
    entity: str | None = None
    topics: tuple[str, ...] = ()


class AnalysisEmptyError(RuntimeError):
    """Raised when the LLM returned no content at all.

    Typical cause: reasoning model burned its output budget on internal
    thinking before emitting the JSON. Endpoints surface this with a
    specific actionable message ("raise `llm_context_tokens`…") so the
    user isn't left guessing.
    """


def is_placeholder_title(title: str | None) -> bool:
    """True if `title` looks like an auto-assigned placeholder, not user input."""
    if not title:
        return True
    return bool(_PLACEHOLDER_RE.match(title.strip()))


def _load_system_prompt() -> str:
    """Read the user-editable meeting_analysis.md, fall back to bundled."""
    try:
        return _USER_PROMPT.read_text(encoding="utf-8")
    except Exception as e:
        log.warning(
            "Could not read user meeting_analysis.md (%s): falling back to bundled default",
            e,
        )
        try:
            return _BUNDLED_DEFAULT.read_text(encoding="utf-8")
        except Exception as e2:
            log.error("Could not read bundled meeting_analysis.md: %s", e2)
            # Last-ditch inline fallback so the analysis endpoint at least
            # returns *something*. Keeps the shape the parser expects.
            return (
                "You are AuraScribe. Return a JSON object with fields "
                "`entity` (string), `topics` (array of 3 short Title Case "
                "phrases), and `summary_markdown` (string). No prose, no "
                "code fences."
            )


async def analyze_meeting(
    *,
    transcript: str,
    current_title: str | None,
    started_at: datetime | None = None,
) -> AnalysisResult:
    """Run the combined title-+-summary LLM call.

    `started_at` is the meeting's wall-clock start. When provided, the
    server composes final titles as `{YYYY-MM-DD HH-MM-SS} - {entity} -
    {topic}`. When omitted (e.g. tests), titles fall back to just
    `{entity} - {topic}`.

    Raises:
      LLMUnavailableError: provider unreachable (endpoint maps to 503).
      AnalysisEmptyError: model returned empty string (reasoning burn).
    """
    transcript = prepare_transcript(transcript, max_output_tokens=MAX_OUTPUT_TOKENS)

    system_prompt = _load_system_prompt()
    raw = await chat(
        meeting_analysis_user_prompt(transcript=transcript, current_title=current_title),
        system=system_prompt,
        max_tokens=MAX_OUTPUT_TOKENS,
        temperature=0.4,
    )
    if not raw.strip():
        raise AnalysisEmptyError(
            "LLM returned no content. The model likely burned its output "
            "budget on internal reasoning. Fix: raise `llm_context_tokens` "
            "in Settings, or switch `llm_model` to a non-reasoning model."
        )
    return _parse_analysis(raw, started_at=started_at)


def _parse_analysis(raw: str, *, started_at: datetime | None = None) -> AnalysisResult:
    """Extract entity + topics + summary_markdown from the LLM response.

    Tolerant of:
      - pure JSON
      - JSON wrapped in ```json … ``` code fences
      - prose preceding the JSON (scans forward to the first `{`)
      - legacy `{"titles": [...]}` shape from the previous prompt
        (used as-is without timestamp composition, so user-edited
        prompts that predate this change still produce usable output)

    Fields missing or of the wrong type degrade to empty — callers
    decide whether that constitutes a hard failure for their path.
    """
    text = (raw or "").strip()
    # Strip code fences if present.
    if text.startswith("```"):
        # Remove opening fence (``` or ```json) and trailing fence.
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
        text = text.strip()
    # Jump forward to the first `{` — handles models that prepend
    # "Here's the analysis:" or similar.
    start = text.find("{")
    if start > 0:
        text = text[start:]

    try:
        parsed, _ = json.JSONDecoder().raw_decode(text)
    except Exception:
        log.warning("Could not parse analysis JSON: %r", raw[:300])
        return AnalysisResult(titles=[], summary_markdown="")

    if not isinstance(parsed, dict):
        return AnalysisResult(titles=[], summary_markdown="")

    summary = parsed.get("summary_markdown")
    summary_str = summary.strip() if isinstance(summary, str) else ""

    # ── New shape: entity + topics ──────────────────────────────────
    raw_entity = parsed.get("entity")
    entity = _clean_entity(raw_entity) if isinstance(raw_entity, str) else None
    topics = _clean_phrases(parsed.get("topics"))

    if topics:
        composed = _compose_titles(started_at, entity, topics)
        return AnalysisResult(
            titles=composed,
            summary_markdown=summary_str,
            entity=entity,
            topics=tuple(topics),
        )

    # ── Legacy shape: titles ──────────────────────────────────────────
    # Older bundled prompts (pre-entity refactor) and any user-edited
    # copies still in that shape return `titles` directly. Use them
    # without composition — their format was "Title Case short phrase"
    # which is perfectly usable, just without the date + entity prefix.
    legacy_titles = _clean_phrases(parsed.get("titles"))
    return AnalysisResult(
        titles=legacy_titles,
        summary_markdown=summary_str,
        entity=None,
        topics=(),
    )


def _clean_phrases(raw: object) -> list[str]:
    """Validate/dedupe topic or title strings from the parsed JSON.

    Rejects empty, overly long (>100 char, likely the model wrote a
    paragraph), and case-insensitive duplicates. Caps at 3. Shared by
    `topics` (new shape) and `titles` (legacy shape) — same cleaning
    rules apply to both.
    """
    if not isinstance(raw, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for t in raw:
        if not isinstance(t, str):
            continue
        s = t.strip().strip('"').strip("'").rstrip(".").strip()
        if not s or len(s) > 100:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(s)
    return cleaned[:3]


# Characters that are invalid in filenames on Windows (colon, slash,
# backslash, etc.) or that break Obsidian link syntax. The composed
# title is used verbatim as the markdown filename, so we strip them
# rather than risk a filesystem error at write time. Replaced with a
# single space; consecutive spaces are collapsed afterwards.
_FILENAME_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _clean_entity(raw: str) -> str | None:
    """Normalise a raw entity string into something safe for a title.

    Strips quotes, trailing punctuation, and filesystem-unsafe
    characters. Rejects empty / all-generic values so the composition
    path falls back to `{timestamp} - {topic}` rather than emitting
    `… - Meeting - …` noise.
    """
    s = raw.strip().strip('"').strip("'").rstrip(".").strip()
    if not s:
        return None
    # Trim to a reasonable length; long entity names overflow the
    # filename budget on Windows.
    if len(s) > 40:
        s = s[:40].rstrip()
    s = _FILENAME_UNSAFE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return None
    # Generic placeholders the model sometimes emits when it can't
    # identify an entity — treat as "no entity" so we compose
    # `{timestamp} - {topic}` instead of redundant noise.
    if s.lower() in {"meeting", "call", "sync", "discussion", "n/a", "none", "unknown"}:
        return None
    return s


def _compose_titles(
    started_at: datetime | None,
    entity: str | None,
    topics: list[str],
) -> list[str]:
    """Stitch `{entity} - {topic}` (or just `{topic}`) from the parts.

    - If `entity` is None/falsy the entity slot is dropped.
    - Topics are sanitised for filesystem-unsafe characters — gives
      every title a clean filename.
    - `started_at` is retained for signature compatibility but unused:
      the timestamp is prepended to the *filename* only (see
      `obsidian.writer.meeting_file_path`), not the visible title.
    """
    del started_at  # unused; filename gets its own timestamp
    out: list[str] = []
    for topic in topics:
        clean_topic = _FILENAME_UNSAFE.sub(" ", topic).strip()
        clean_topic = re.sub(r"\s+", " ", clean_topic).strip()
        if not clean_topic:
            continue
        parts = [p for p in (entity, clean_topic) if p]
        out.append(" - ".join(parts))
    return out
