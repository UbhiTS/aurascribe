"""Combined meeting analysis — titles + summary from a single LLM call.

Both "AI Summary" and "Suggest Title" used to make their own LLM call
with essentially the same transcript. That doubled cost and latency
for a shared piece of work. This module collapses the two into a
single request that returns both artefacts, and exposes small helpers
for the two endpoints that consume them.

Output JSON shape (enforced in the system prompt):
  {
    "titles": ["...", "...", "..."],
    "summary_markdown": "## Summary\n...\n## Key Decisions\n..."
  }

The two shapes live in one response body; each endpoint uses the
subset it needs. The summary_markdown preserves the exact section
contract the Obsidian writer + the action-items extractor rely on
(## Summary / ## Key Decisions / ## Action Items / ## Key Topics
/ ## People Mentioned).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from aurascribe.config import LLM_CONTEXT_TOKENS
from aurascribe.llm.client import LLMUnavailableError, chat
from aurascribe.llm.prompts import MEETING_ANALYSIS_SYSTEM, meeting_analysis_prompt
from aurascribe.llm.sampling import prepare_transcript

log = logging.getLogger("aurascribe.llm.analysis")

# Match any "Transcription …" title (the MeetingManager default) or the
# DB-level "Untitled Meeting" default. Endpoints use this to decide
# whether to auto-apply a suggested title — we never clobber a title
# the user has already typed.
_PLACEHOLDER_RE = re.compile(r"^(transcription(\s|$)|untitled\s+meeting\s*$)", re.IGNORECASE)

# Output token budget. This call produces both the summary (markdown
# with several sections) AND three titles — so it needs the roomier
# summary budget, not the title-only one. Sized off context window:
# daily_brief uses 8%, summary currently uses 10%. We match summary.
# Clamped so tiny models don't starve input and huge models don't
# over-reserve.
MAX_OUTPUT_TOKENS = max(2048, min(16384, int(LLM_CONTEXT_TOKENS * 0.10)))


@dataclass(frozen=True)
class AnalysisResult:
    """Parsed output of one combined analysis call."""
    titles: list[str]
    summary_markdown: str


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


async def analyze_meeting(
    *,
    transcript: str,
    current_title: str | None,
) -> AnalysisResult:
    """Run the combined title-+-summary LLM call.

    Raises:
      LLMUnavailableError: provider unreachable (endpoint maps to 503).
      AnalysisEmptyError: model returned empty string (reasoning burn).
    """
    transcript = prepare_transcript(transcript, max_output_tokens=MAX_OUTPUT_TOKENS)

    raw = await chat(
        meeting_analysis_prompt(transcript=transcript, current_title=current_title),
        system=MEETING_ANALYSIS_SYSTEM,
        max_tokens=MAX_OUTPUT_TOKENS,
        temperature=0.4,
    )
    if not raw.strip():
        raise AnalysisEmptyError(
            "LLM returned no content. The model likely burned its output "
            "budget on internal reasoning. Fix: raise `llm_context_tokens` "
            "in Settings, or switch `llm_model` to a non-reasoning model."
        )
    return _parse_analysis(raw)


def _parse_analysis(raw: str) -> AnalysisResult:
    """Extract titles + summary_markdown from the LLM response.

    Tolerant of:
      - pure JSON
      - JSON wrapped in ```json … ``` code fences
      - prose preceding the JSON (scans forward to the first `{`)
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

    titles = _clean_titles(parsed.get("titles"))
    summary = parsed.get("summary_markdown")
    summary_str = summary.strip() if isinstance(summary, str) else ""
    return AnalysisResult(titles=titles, summary_markdown=summary_str)


def _clean_titles(raw_titles: object) -> list[str]:
    """Validate/dedupe titles from the parsed JSON.

    Rejects empty, overly long (>100 char, likely the model wrote a
    paragraph), and case-insensitive duplicates. Caps at 3.
    """
    if not isinstance(raw_titles, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for t in raw_titles:
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
