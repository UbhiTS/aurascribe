"""Decide which vault bucket (and customer, if any) a meeting belongs in.

Two-stage classifier:

  1. Speaker lookup — for every named speaker in the transcript, check
     whether a `People/<Name>.md` file already exists somewhere in the
     vault. If yes, the path tells us the customer (or that they're
     internal). When ≥2 speakers point at the same customer, that's
     unambiguous — skip the LLM entirely. ~no cost, ~always right.

  2. LLM fallback — when speaker lookup is inconclusive (zero named
     speakers, conflicting customers, only Me + Unknowns), send the
     transcript + the list of known customer folder names + the
     speaker list to the LLM. It returns
     `{bucket, customer, confidence, reasoning}` per the contract in
     `meeting_bucket.md`. We only trust customer = "<NewName>" when
     confidence is high enough — low-confidence guesses fall back to
     inbox so the user can triage.

The resulting `(bucket, customer)` pair is persisted on the meetings
row by the caller; subsequent writer calls re-read it and route
accordingly.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from aurascribe.config import (
    LLM_CONTEXT_TOKENS,
    PROMPTS_DIR,
    VAULT_CUSTOMERS,
)
from aurascribe.llm.client import LLMUnavailableError, chat
from aurascribe.llm.sampling import prepare_transcript
from aurascribe.obsidian.writer import (
    BUCKET_CUSTOMER,
    BUCKET_INBOX,
    BUCKET_INTERNAL,
    BUCKET_INTERVIEW,
    BUCKET_PERSONAL,
    VALID_BUCKETS,
    find_person_customer,
    find_person_path,
)
from aurascribe.transcription import Utterance

log = logging.getLogger("aurascribe.llm.bucket_inference")

# Confidence below this gate falls back to inbox even when the LLM had a
# guess — better to leave the meeting in 00-Inbox/ for the user to triage
# than to scatter low-confidence choices across customer folders that
# would then need cleanup.
_LLM_CONFIDENCE_GATE = 0.5

# Provisional speaker labels never identify a real person. Match what
# meeting_manager uses for the same purpose.
_PROVISIONAL_LABEL_RE = re.compile(r"^Speaker \d+$")

PROMPT_FILENAME = "meeting_bucket.md"
_USER_PROMPT = PROMPTS_DIR / PROMPT_FILENAME
_BUNDLED_DEFAULT = Path(__file__).resolve().parent / PROMPT_FILENAME

# Bucket inference is a small JSON return — cap output tokens low so we
# don't waste budget on chatty models. Floor of 256 keeps headroom for
# verbose reasoning fields.
_MAX_OUTPUT_TOKENS = max(256, min(1024, int(LLM_CONTEXT_TOKENS * 0.05)))


@dataclass(frozen=True)
class InferredBucket:
    """Classifier output. `customer` is None for any bucket != customer."""

    bucket: str
    customer: str | None
    source: str  # "speaker-lookup" | "llm" | "fallback-inbox"
    confidence: float
    reasoning: str = ""


def _named_speakers(utterances: list[Utterance]) -> list[str]:
    """Distinct, real-person speakers from the transcript.

    Drops "Me" (the user themselves don't disambiguate the bucket),
    "Unknown" (no identity), and "Speaker N" (unresolved provisional
    cluster). Order is by first appearance so the LLM's NAMED_SPEAKERS
    block reads naturally.
    """
    seen: set[str] = set()
    out: list[str] = []
    for u in utterances:
        s = (u.speaker or "").strip()
        if not s or s == "Me" or s == "Unknown":
            continue
        if _PROVISIONAL_LABEL_RE.match(s):
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _list_known_customers() -> list[str]:
    """Folder names directly under 10-Customers/. Used to anchor the LLM
    on existing accounts so it reuses names instead of inventing variants."""
    if VAULT_CUSTOMERS is None or not VAULT_CUSTOMERS.exists():
        return []
    out: list[str] = []
    for child in VAULT_CUSTOMERS.iterdir():
        if child.is_dir() and not child.name.startswith("."):
            out.append(child.name)
    out.sort()
    return out


def _speaker_lookup(speakers: list[str]) -> InferredBucket | None:
    """Decide the bucket from the People/ folder layout alone.

    Returns None when the lookup is inconclusive (no known speakers, or
    a tie that needs disambiguation). When it returns a result, the
    confidence is high — we know exactly where these people live in
    the vault.
    """
    if not speakers:
        return None

    customers = Counter()
    internals = 0
    unknowns = 0
    for s in speakers:
        # find_person_customer returns the customer folder name when the
        # person lives under 10-Customers/<X>/People/, None when they're
        # in 20-Internal/People/ OR not found anywhere.
        cust = find_person_customer(s)
        if cust:
            customers[cust] += 1
            continue
        # Distinguish "internal colleague" (file exists under 20-Internal)
        # from "never seen this name" — the former pins the bucket, the
        # latter doesn't.
        if find_person_path(s) is not None:
            internals += 1
        else:
            unknowns += 1

    # Most-common customer wins. A single matching customer + zero
    # ambiguity = 1.0 confidence.
    if customers:
        top, top_count = customers.most_common(1)[0]
        # Penalise when speakers are split across multiple customers —
        # weird case (cross-account meeting?) but possible. We pick the
        # majority but signal the ambiguity via lower confidence so the
        # caller can route to inbox if it's strict about confidence.
        spread = len(customers)
        confidence = 1.0 if spread == 1 else max(0.5, top_count / sum(customers.values()))
        return InferredBucket(
            bucket=BUCKET_CUSTOMER,
            customer=top,
            source="speaker-lookup",
            confidence=confidence,
            reasoning=f"Speakers {dict(customers)} resolve under 10-Customers/.",
        )

    # No customer hits, but every named speaker is an internal colleague →
    # confidently internal. (We still allow the LLM to override interview /
    # personal in a later layer if desired.)
    if internals > 0 and unknowns == 0:
        return InferredBucket(
            bucket=BUCKET_INTERNAL,
            customer=None,
            source="speaker-lookup",
            confidence=0.9,
            reasoning="All named speakers live under 20-Internal/People/.",
        )

    # Mixed bag — let the LLM look at the transcript.
    return None


def _load_prompt() -> str:
    """User-editable prompt with bundled fallback. Same loader pattern as
    every other LLM prompt in the project."""
    try:
        return _USER_PROMPT.read_text(encoding="utf-8")
    except Exception as e:
        log.warning("Could not read user meeting_bucket.md (%s): falling back to bundled", e)
        try:
            return _BUNDLED_DEFAULT.read_text(encoding="utf-8")
        except Exception as e2:
            log.error("Could not read bundled meeting_bucket.md: %s", e2)
            return (
                "Classify the meeting into one of: customer, internal, "
                "interview, personal. Return JSON with bucket, customer, "
                "confidence, reasoning."
            )


def _build_user_prompt(
    transcript: str,
    speakers: list[str],
    known_customers: list[str],
) -> str:
    customers_block = "\n".join(known_customers) if known_customers else "(none yet)"
    speakers_block = "\n".join(speakers) if speakers else "(none)"
    return (
        f"<KNOWN_CUSTOMERS>\n{customers_block}\n</KNOWN_CUSTOMERS>\n\n"
        f"<NAMED_SPEAKERS>\n{speakers_block}\n</NAMED_SPEAKERS>\n\n"
        f"<TRANSCRIPT>\n{transcript}\n</TRANSCRIPT>"
    )


_FILENAME_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _clean_customer(raw: object) -> str | None:
    """Normalise the LLM's customer string into a folder-safe name. Strips
    suffixes like 'Inc.' / 'LLC', filesystem-unsafe chars, and runs of
    whitespace. Returns None for empty / generic placeholders so the caller
    falls back to inbox instead of creating a `Unknown/` folder."""
    if not isinstance(raw, str):
        return None
    s = raw.strip().strip('"').strip("'").rstrip(".").strip()
    if not s:
        return None
    s = re.sub(r"\b(Inc\.?|LLC|Corp\.?|Corporation|Ltd\.?|GmbH|Pty)\b", "", s, flags=re.I)
    s = _FILENAME_UNSAFE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(",")
    if not s:
        return None
    if s.lower() in {"unknown", "n/a", "none", "tbd", "internal", "customer"}:
        return None
    if len(s) > 60:
        s = s[:60].rstrip()
    return s


async def _llm_infer(
    utterances: list[Utterance],
    speakers: list[str],
) -> InferredBucket:
    """Run the meeting_bucket LLM and parse its JSON.

    Falls back to inbox (low confidence) on any failure mode — unreachable
    LLM, empty response, malformed JSON, or invalid bucket value. The
    caller then leaves the meeting in 00-Inbox/ for manual triage.
    """
    from aurascribe.llm.prompts import format_transcript

    system = _load_prompt()
    transcript_md = format_transcript(utterances)
    transcript_md = prepare_transcript(transcript_md, max_output_tokens=_MAX_OUTPUT_TOKENS)
    known_customers = _list_known_customers()
    user_msg = _build_user_prompt(transcript_md, speakers, known_customers)

    try:
        raw = await chat(
            user_msg,
            system=system,
            max_tokens=_MAX_OUTPUT_TOKENS,
            temperature=0.2,
        )
    except LLMUnavailableError as e:
        log.warning("Bucket inference: LLM unavailable, defaulting to inbox: %s", e)
        return InferredBucket(BUCKET_INBOX, None, "fallback-inbox", 0.0, str(e))

    text = (raw or "").strip()
    if not text:
        return InferredBucket(BUCKET_INBOX, None, "fallback-inbox", 0.0, "empty LLM reply")

    # Tolerate code fences / leading prose — same trick as analysis.py.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
        text = text.strip()
    start = text.find("{")
    if start > 0:
        text = text[start:]

    try:
        parsed, _ = json.JSONDecoder().raw_decode(text)
    except Exception as e:
        log.warning("Bucket inference: could not parse JSON (%s): %r", e, raw[:200])
        return InferredBucket(BUCKET_INBOX, None, "fallback-inbox", 0.0, "malformed JSON")

    if not isinstance(parsed, dict):
        return InferredBucket(BUCKET_INBOX, None, "fallback-inbox", 0.0, "non-object JSON")

    bucket_raw = parsed.get("bucket")
    if bucket_raw not in VALID_BUCKETS:
        log.warning("Bucket inference: invalid bucket %r, defaulting to inbox", bucket_raw)
        return InferredBucket(BUCKET_INBOX, None, "fallback-inbox", 0.0, f"unknown bucket {bucket_raw!r}")

    customer = _clean_customer(parsed.get("customer")) if bucket_raw == BUCKET_CUSTOMER else None
    if bucket_raw == BUCKET_CUSTOMER and customer is None:
        # Customer bucket needs a name — degrade to inbox so we don't
        # create the meeting under `10-Customers/None/` or similar.
        log.warning("Bucket inference: bucket=customer but no usable name, defaulting to inbox")
        return InferredBucket(BUCKET_INBOX, None, "fallback-inbox", 0.0, "customer name missing")

    confidence_raw = parsed.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    reasoning = parsed.get("reasoning")
    reasoning = reasoning.strip() if isinstance(reasoning, str) else ""

    if confidence < _LLM_CONFIDENCE_GATE:
        log.info(
            "Bucket inference: low-confidence LLM result (%.2f) → inbox: %s",
            confidence, reasoning or "(no reasoning)",
        )
        return InferredBucket(BUCKET_INBOX, None, "fallback-inbox", confidence, reasoning)

    return InferredBucket(bucket_raw, customer, "llm", confidence, reasoning)


async def infer_bucket(utterances: list[Utterance]) -> InferredBucket:
    """Public entry point. Speaker lookup first, LLM fallback if needed.

    Returns an `InferredBucket` regardless of outcome — caller never has
    to handle None. Stable result on empty transcript: bucket=inbox.
    """
    if not utterances:
        return InferredBucket(BUCKET_INBOX, None, "fallback-inbox", 0.0, "no utterances")

    speakers = _named_speakers(utterances)
    quick = _speaker_lookup(speakers)
    if quick is not None:
        log.info(
            "Bucket inference: speaker-lookup → %s/%s (conf=%.2f)",
            quick.bucket, quick.customer, quick.confidence,
        )
        return quick

    log.info("Bucket inference: speaker lookup inconclusive, asking LLM")
    return await _llm_infer(utterances, speakers)
