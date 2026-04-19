"""Dynamic transcript sampling sized against the configured context window.

Both title suggestion and meeting summary send transcripts into the LLM.
Previously they used fixed char caps (24k for title) or no cap at all
(summary), which either wasted context on tiny models or overflowed it on
bigger ones. This module reads `LLM_CONTEXT_TOKENS` from settings and
returns a transcript slice that maximises the input budget without
tripping the model's context limit.

When the full transcript fits, we pass it through untouched. When it
doesn't, we return a head + middle + tail composite so the model sees
coverage across the whole meeting instead of just the first N minutes.
"""
from __future__ import annotations

from aurascribe.config import LLM_CONTEXT_TOKENS

# Slight overestimate of tokens-per-char keeps us under the model's real
# context budget with some headroom. Matches the value used by
# daily_brief.py for consistency.
_CHARS_PER_TOKEN = 3.5

# Reserve for the system prompt, user-prompt scaffolding (instructions,
# headers, "Return only JSON", etc), and the output budget's pre-alloc.
# 800 tokens is generous for our prompt templates — update if you add
# large boilerplate to any prompt.
_PROMPT_OVERHEAD_TOKENS = 800

# Minimum characters we'll bother sending. Below this the transcript is
# too clipped to carry meaning — caller should consider falling back to
# summary-only input.
_MIN_TRANSCRIPT_CHARS = 600

# Separator inserted between sampled sections so the model knows the
# transcript is non-contiguous. Kept short but unambiguous.
_SECTION_SEP = "\n\n…[transcript section omitted]…\n\n"


def compute_transcript_budget_chars(max_output_tokens: int) -> int:
    """Return the char budget available for transcript in one request.

    Formula:
        available_tokens = CONTEXT - output - prompt_overhead
        budget_chars     = available_tokens * CHARS_PER_TOKEN

    If the model has a tiny context, we floor at `_MIN_TRANSCRIPT_CHARS`
    so the caller still sends *something* rather than nothing.
    """
    available_tokens = max(
        0, LLM_CONTEXT_TOKENS - max_output_tokens - _PROMPT_OVERHEAD_TOKENS
    )
    budget_chars = int(available_tokens * _CHARS_PER_TOKEN)
    return max(_MIN_TRANSCRIPT_CHARS, budget_chars)


def sample_transcript(transcript: str, budget_chars: int) -> str:
    """Return at most `budget_chars` of transcript, sampled for coverage.

    Strategy:
      - transcript ≤ budget          → return as-is
      - transcript > budget          → head + middle + tail composite
        - each section = (budget - 2·separator_len) / 3
        - cut on utterance boundaries (\n) so we don't emit a half-line

    The middle slice is drawn around the transcript's midpoint, so the
    model sees the topical centre of the meeting (often where decisions
    land) and not just the opening.
    """
    transcript = transcript or ""
    if len(transcript) <= budget_chars:
        return transcript
    if budget_chars < len(_SECTION_SEP) * 2 + 300:
        # Budget too small to justify the head/middle/tail layout —
        # just return the head, cut to a clean line boundary.
        return _cut_head(transcript, budget_chars)

    per_section = (budget_chars - 2 * len(_SECTION_SEP)) // 3
    head = _cut_head(transcript, per_section)
    tail = _cut_tail(transcript, per_section)
    middle = _cut_middle(transcript, per_section)
    return f"{head}{_SECTION_SEP}{middle}{_SECTION_SEP}{tail}"


def prepare_transcript(transcript: str, *, max_output_tokens: int) -> str:
    """One-shot helper: compute budget + sample.

    Use this from prompt builders that don't care about the intermediate
    budget value (most callers).
    """
    budget = compute_transcript_budget_chars(max_output_tokens)
    return sample_transcript(transcript, budget)


# ── Internal ───────────────────────────────────────────────────────────────


def _cut_head(transcript: str, budget: int) -> str:
    """Take up to `budget` chars from the start, ending at a line boundary."""
    if len(transcript) <= budget:
        return transcript
    slice_ = transcript[:budget]
    # Prefer to end on a newline so we don't split an utterance mid-text.
    last_nl = slice_.rfind("\n")
    if last_nl > budget // 2:
        slice_ = slice_[:last_nl]
    return slice_


def _cut_tail(transcript: str, budget: int) -> str:
    """Take up to `budget` chars from the end, starting at a line boundary."""
    if len(transcript) <= budget:
        return transcript
    slice_ = transcript[-budget:]
    first_nl = slice_.find("\n")
    # Only trim the leading partial line if doing so doesn't cost more
    # than half the slice (i.e., the partial is short).
    if 0 <= first_nl < budget // 2:
        slice_ = slice_[first_nl + 1:]
    return slice_


def _cut_middle(transcript: str, budget: int) -> str:
    """Take up to `budget` chars centred on the transcript midpoint."""
    if len(transcript) <= budget:
        return transcript
    mid = len(transcript) // 2
    start = max(0, mid - budget // 2)
    end = min(len(transcript), start + budget)
    slice_ = transcript[start:end]
    # Trim partial first/last lines so we start/end on utterance boundaries.
    first_nl = slice_.find("\n")
    last_nl = slice_.rfind("\n")
    if 0 <= first_nl < len(slice_) // 4:
        slice_ = slice_[first_nl + 1:]
        last_nl = slice_.rfind("\n")  # recompute after head trim
    if last_nl > len(slice_) * 3 // 4:
        slice_ = slice_[:last_nl]
    return slice_
