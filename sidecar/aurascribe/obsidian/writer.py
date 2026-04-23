"""Writes meetings, people notes, and daily briefs into the Obsidian vault.

Layout contract (customer-isolated) — see `project_vault_structure.md`
in memory for the authoritative spec:

  00-Inbox/YYYY/MM/                       unclassified (recording / low-conf)
  10-Customers/<Customer>/
      <Customer>.md                       MOC (seeded on first meeting)
      People/<Name>.md
      Projects/<Project>.md
      Meetings/YYYY/MM/<filename>.md
      Notes/{Architecture,Stakeholders,Open-Risks,Commercials,Notes}.md
  20-Internal/
      People/<Name>.md
      Meetings/YYYY/MM/<filename>.md
      Notes/
  30-Interviews/YYYY/MM/<filename>.md
  40-Personal/YYYY/MM/<filename>.md
  50-Daily/YYYY/MM/YYYY-MM-DD.md
  90-Templates/ (seeded on boot — not written by this module at runtime)
  99-Archive/   (user moves closed customers here by hand)

Every meeting row carries `vault_bucket` + `vault_customer` in the DB.
Writer fetches them fresh on every call so a mid-recording reclassify
(auto-inference OR user override) moves the file to its new home on
the next write — no separate plumbing needed.

If `OBSIDIAN_VAULT` isn't configured, every writer function returns
None and the rest of the app carries on — transcripts still land in
SQLite.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path

import aiofiles
import aiosqlite

from aurascribe.config import (
    DB_PATH,
    OBSIDIAN_VAULT,
    VAULT_CUSTOMERS,
    VAULT_DAILY,
    VAULT_INBOX,
    VAULT_INTERNAL,
    VAULT_INTERVIEWS,
    VAULT_PERSONAL,
    VAULT_TEMPLATES,
)
from aurascribe.llm.prompts import format_transcript
from aurascribe.transcription import Utterance

log = logging.getLogger("aurascribe.obsidian")

# ── Bucket enum ─────────────────────────────────────────────────────────────

BUCKET_INBOX = "inbox"
BUCKET_CUSTOMER = "customer"
BUCKET_INTERNAL = "internal"
BUCKET_INTERVIEW = "interview"
BUCKET_PERSONAL = "personal"

VALID_BUCKETS = frozenset(
    {BUCKET_INBOX, BUCKET_CUSTOMER, BUCKET_INTERNAL, BUCKET_INTERVIEW, BUCKET_PERSONAL}
)

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


# Timeout for vault file writes. Local disk writes land in milliseconds;
# this cap catches vaults on slow / disconnected network drives (OneDrive,
# iCloud, SMB share) that would otherwise stall the record loop or intel
# loop for seconds at a time. 10s is way past any reasonable local write
# and still tight enough that the user notices a broken vault quickly.
_VAULT_WRITE_TIMEOUT = 10.0


async def _write_text_with_timeout(
    path: Path,
    content: str,
    *,
    what: str,
) -> bool:
    """Write `content` to `path` via aiofiles with a hard timeout.

    Returns True on success, False on timeout / failure. Never raises —
    vault writes are best-effort augmentation; a failed write must never
    take down the record loop or intel loop that called it.

    `what` is a short label for the log line so operators can tell which
    path timed out (meeting-file vs. person-note vs. MOC).
    """
    async def _do_write() -> None:
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(content)

    try:
        await asyncio.wait_for(_do_write(), timeout=_VAULT_WRITE_TIMEOUT)
        return True
    except asyncio.TimeoutError:
        log.warning(
            "Vault write timed out (%s, >%s s): %s. Vault may be on a "
            "slow/disconnected network drive — check the vault path in Settings.",
            what, _VAULT_WRITE_TIMEOUT, path,
        )
        return False
    except Exception as e:
        log.warning("Vault write failed (%s): %s — %s", what, path, e)
        return False


def forget_meeting_throttle(meeting_id: str) -> None:
    """Drop throttle state for a meeting. Call on finalize/stop."""
    _last_write_ts.pop(meeting_id, None)
    _chunks_since_write.pop(meeting_id, None)


def _note_write(meeting_id: str) -> None:
    _last_write_ts[meeting_id] = time.monotonic()
    _chunks_since_write[meeting_id] = 0


# ── Filename sanitization ───────────────────────────────────────────────────

_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename_part(s: str) -> str:
    cleaned = _ILLEGAL_FILENAME_CHARS.sub("-", s).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "untitled"


def _slug(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-").lower()


# ── Path resolution ─────────────────────────────────────────────────────────


def _normalize_bucket(bucket: str | None) -> str:
    """Coerce a DB-loaded bucket value to one of VALID_BUCKETS; unknown → inbox."""
    if bucket in VALID_BUCKETS:
        return bucket  # type: ignore[return-value]
    return BUCKET_INBOX


def _bucket_root(bucket: str, customer: str | None) -> Path | None:
    """Root folder a meeting's .md lives under for this bucket. None if
    Obsidian isn't configured. Falls back to Inbox for a 'customer'
    bucket with no customer name — the inference layer shouldn't emit
    that combo, but the writer is defensive."""
    if OBSIDIAN_VAULT is None:
        return None
    if bucket == BUCKET_CUSTOMER:
        if not customer:
            return VAULT_INBOX
        return VAULT_CUSTOMERS / _safe_filename_part(customer) if VAULT_CUSTOMERS else None
    if bucket == BUCKET_INTERNAL:
        return VAULT_INTERNAL
    if bucket == BUCKET_INTERVIEW:
        return VAULT_INTERVIEWS
    if bucket == BUCKET_PERSONAL:
        return VAULT_PERSONAL
    return VAULT_INBOX


def _meetings_dir(bucket: str, customer: str | None) -> Path | None:
    """Folder the YYYY/MM/ tree sits under.

    Customer and Internal both have a `Meetings/` subfolder so that
    sibling content (People/, Notes/, Projects/) can share the same
    parent without colliding with a flat list of meeting files. Inbox /
    Interviews / Personal skip the `Meetings/` wrapper because they
    don't have sibling subfolders to disambiguate from.
    """
    root = _bucket_root(bucket, customer)
    if root is None:
        return None
    if bucket in (BUCKET_CUSTOMER, BUCKET_INTERNAL):
        return root / "Meetings"
    return root


def meeting_file_path(
    started_at: datetime,
    title: str,
    bucket: str = BUCKET_INBOX,
    customer: str | None = None,
) -> Path | None:
    """Canonical path for a meeting's vault file, given its bucket/customer.

    `YYYY-MM-DD HH-MM-SS <title>.md` nested under `YYYY/MM/`. The title
    retains the customer name — it's redundant with the path but helps
    when files leave the vault standalone.
    """
    base = _meetings_dir(bucket, customer)
    if base is None:
        return None
    year = started_at.strftime("%Y")
    month = started_at.strftime("%m")
    stem = (
        f"{started_at.strftime('%Y-%m-%d %H-%M-%S')} "
        f"{_safe_filename_part(title)}"
    )
    return base / year / month / f"{stem}.md"


def meeting_vault_link(
    started_at: datetime,
    title: str,
    bucket: str = BUCKET_INBOX,
    customer: str | None = None,
) -> str | None:
    """Vault-relative wikilink target (no `.md`) — e.g.
    `10-Customers/Conviva/Meetings/2026/04/2026-04-22 09-30-00 - Conviva - Kickoff`.
    """
    path = meeting_file_path(started_at, title, bucket, customer)
    if path is None or OBSIDIAN_VAULT is None:
        return None
    try:
        rel = path.relative_to(OBSIDIAN_VAULT).with_suffix("")
    except ValueError:
        # Path isn't under the vault (shouldn't happen but stays defensive).
        return None
    return rel.as_posix()


def daily_brief_file_path(brief_date: str) -> Path | None:
    """Canonical path for a daily brief: `50-Daily/YYYY/MM/YYYY-MM-DD.md`."""
    if VAULT_DAILY is None:
        return None
    year, month = brief_date[:4], brief_date[5:7]
    return VAULT_DAILY / year / month / f"{brief_date}.md"


# ── People-note lookup across the vault ────────────────────────────────────


def _people_search_roots() -> list[Path]:
    """All folders where a Person note could live.

    Returns a defensive (possibly empty) list so callers can iterate without
    None-checking every entry. Order matters for tie-breaking: the first hit
    wins. Customer folders come before Internal because customer-scoped
    people are more specific — if the same name exists in both places, prefer
    the customer attribution.
    """
    roots: list[Path] = []
    if VAULT_CUSTOMERS and VAULT_CUSTOMERS.exists():
        # Sort so the person-attribution result is deterministic when
        # the same name exists in two customer folders. `iterdir()`
        # order is filesystem-dependent (varies across runs on some
        # platforms) and the audit flagged it as a reproducibility hole.
        for customer_dir in sorted(
            VAULT_CUSTOMERS.iterdir(), key=lambda p: p.name.lower(),
        ):
            if customer_dir.is_dir():
                people = customer_dir / "People"
                if people.exists():
                    roots.append(people)
    if VAULT_INTERNAL:
        internal_people = VAULT_INTERNAL / "People"
        if internal_people.exists():
            roots.append(internal_people)
    return roots


def find_person_path(name: str) -> Path | None:
    """First match for `<name>.md` across customer + internal people folders.

    Returns None if the vault isn't configured or the person hasn't been
    seen before. Used by the bucket-inference layer — a speaker known to
    live under `10-Customers/Conviva/People/` pins the meeting to Conviva.
    """
    target = f"{_safe_filename_part(name)}.md"
    for root in _people_search_roots():
        candidate = root / target
        if candidate.exists():
            return candidate
    return None


def find_person_customer(name: str) -> str | None:
    """Customer folder name the given person sits under, or None.

    None can mean any of: vault off, person not found, person is under
    `20-Internal/People/` (i.e. a colleague, not a customer contact).
    Callers use this result + their own logic to choose a bucket.
    """
    path = find_person_path(name)
    if path is None or VAULT_CUSTOMERS is None:
        return None
    try:
        rel = path.relative_to(VAULT_CUSTOMERS)
    except ValueError:
        return None
    # rel = <Customer>/People/<Name>.md — first component is the customer.
    if len(rel.parts) >= 1:
        return rel.parts[0]
    return None


def person_file_path(
    name: str,
    bucket: str = BUCKET_INTERNAL,
    customer: str | None = None,
) -> Path | None:
    """Where a NEW person note should live given the meeting it came from.

    Auto-written person stubs go into the customer folder when the
    meeting is customer-scoped, and into Internal otherwise. Interview /
    personal buckets don't get auto-people notes — caller checks bucket
    before calling (returns None for those buckets so the caller can't
    accidentally scatter files).
    """
    if OBSIDIAN_VAULT is None:
        return None
    safe = _safe_filename_part(name)
    if bucket == BUCKET_CUSTOMER and customer and VAULT_CUSTOMERS:
        return VAULT_CUSTOMERS / _safe_filename_part(customer) / "People" / f"{safe}.md"
    if bucket == BUCKET_INTERNAL and VAULT_INTERNAL:
        return VAULT_INTERNAL / "People" / f"{safe}.md"
    return None


# ── Vault bootstrap + maintenance ───────────────────────────────────────────


def _ensure_path_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _customer_root(customer: str) -> Path | None:
    """Path of `10-Customers/<Customer>/`, or None if vault unconfigured."""
    if VAULT_CUSTOMERS is None:
        return None
    return VAULT_CUSTOMERS / _safe_filename_part(customer)


# Canonical Notes/ filenames AuraScribe seeds on customer-folder
# bootstrap. Free-form scratch lives in `Notes.md`; the others give
# agents known locations to look for specific content.
_CANONICAL_NOTES: tuple[tuple[str, str], ...] = (
    (
        "Architecture.md",
        "Current state, target state, integration points. "
        "Architecture decisions and the tradeoffs behind them.",
    ),
    (
        "Stakeholders.md",
        "Decision makers, technical evaluators, economic buyers. "
        "Who can say yes, who can say no, who influences either.",
    ),
    (
        "Open-Risks.md",
        "Things that could derail the engagement — technical, "
        "commercial, organizational. Each with mitigation status.",
    ),
    (
        "Commercials.md",
        "Pricing discussions, contract structure, procurement timeline. "
        "Anything financial or contractual.",
    ),
    (
        "Notes.md",
        "Free-form scratch space. Anything that doesn't fit the other "
        "canonical files lands here.",
    ),
)


async def bootstrap_customer(customer: str) -> Path | None:
    """Create the per-customer folder skeleton on first contact.

    Idempotent — running on an existing customer folder leaves existing
    files untouched and only fills in any missing canonical pieces.
    Safe to call from any code path that has just decided this meeting
    belongs to a (possibly new) customer.

    Creates:
      <Customer>/<Customer>.md   — MOC with Dataview queries
      <Customer>/People/         — empty dir (people files written elsewhere)
      <Customer>/Projects/       — empty dir
      <Customer>/Meetings/       — empty dir (writer also creates as needed)
      <Customer>/Notes/<canonical>.md  — five seeded files (see _CANONICAL_NOTES)

    Returns the customer root path, or None when the vault isn't configured.
    """
    root = _customer_root(customer)
    if root is None:
        return None
    root.mkdir(parents=True, exist_ok=True)
    (root / "People").mkdir(exist_ok=True)
    (root / "Projects").mkdir(exist_ok=True)
    (root / "Meetings").mkdir(exist_ok=True)
    notes_dir = root / "Notes"
    notes_dir.mkdir(exist_ok=True)

    safe = _safe_filename_part(customer)
    moc_path = root / f"{safe}.md"
    if not moc_path.exists():
        moc_content = _customer_moc_content(customer)
        if await _write_text_with_timeout(moc_path, moc_content, what="customer-moc"):
            log.info("Bootstrapped customer MOC at %s", moc_path)

    for filename, blurb in _CANONICAL_NOTES:
        note_path = notes_dir / filename
        if note_path.exists():
            continue
        topic = filename[:-3]  # strip ".md"
        content = (
            "---\n"
            "type: customer-note\n"
            f"customer: {customer}\n"
            f"topic: {topic.lower()}\n"
            "tags: [aurascribe]\n"
            "---\n\n"
            f"# {topic}\n\n"
            f"> {blurb}\n"
        )
        await _write_text_with_timeout(note_path, content, what="customer-note")

    return root


def _customer_moc_content(customer: str) -> str:
    """Initial Map-of-Content for a customer.

    Frontmatter is minimal-but-extendable so the user can fill in
    `segment`, `stage`, `account_exec` etc. as the engagement matures —
    we don't pre-fill those because we'd usually be wrong. The Dataview
    queries are scoped to the customer's own folder so cross-customer
    bleed is impossible.
    """
    return f"""---
type: customer
name: {customer}
stage: discovery
tags: [customer, aurascribe]
---

# {customer}

> Map of Content for everything related to this customer.
> Edit the frontmatter as the engagement matures (`stage`, `segment`,
> `account_exec`, `gcp_products`).

## Active projects

```dataview
LIST FROM "10-Customers/{customer}/Projects"
```

## Recent meetings

```dataview
TABLE date, status FROM "10-Customers/{customer}/Meetings"
SORT date DESC LIMIT 20
```

## Key contacts

```dataview
LIST FROM "10-Customers/{customer}/People"
```

## Open action items (mine)

```dataview
TASK FROM "10-Customers/{customer}/Meetings"
WHERE !completed
```

## Notes

- [[{customer}/Notes/Architecture|Architecture]]
- [[{customer}/Notes/Stakeholders|Stakeholders]]
- [[{customer}/Notes/Open-Risks|Open Risks]]
- [[{customer}/Notes/Commercials|Commercials]]
- [[{customer}/Notes/Notes|Free-form notes]]
"""


# Template files seeded into 90-Templates/ on first boot. These are
# reference shapes for the user — they're not consumed by AuraScribe at
# runtime (the writer composes meeting/person/MOC content directly).
# Drop them in the vault so the user can clone them when authoring a
# stub manually.
_VAULT_TEMPLATES: tuple[tuple[str, str], ...] = (
    (
        "customer-meeting.md",
        """---
type: meeting
date: YYYY-MM-DD
time: HH:MM
bucket: customer
customer: <Customer>
meeting_id: <uuid>
attendees: ["[[Name]]"]
status: done
tags: [aurascribe]
---

# YYYY-MM-DD HH-MM-SS - <Customer> - <Topic>

> Recorded YYYY-MM-DD at HH:MM · [[Name]]

## Summary

## Action Items

## Transcript
""",
    ),
    (
        "customer-MOC.md",
        """---
type: customer
name: <Customer>
stage: discovery
segment: <industry>
account_exec: "[[<AE>]]"
gcp_products: []
tags: [customer, aurascribe]
---

# <Customer>

## Active projects
## Recent meetings
## Key contacts
## Open action items
""",
    ),
    (
        "person.md",
        """---
type: person
name: <Full Name>
role: <Title>
email: <email>
tags: [person, aurascribe]
---

# <Full Name>

## Notes

## Meetings
""",
    ),
    (
        "project.md",
        """---
type: project
name: <Project Name>
customer: <Customer>
status: active
gcp_products: []
tags: [project, aurascribe]
---

# <Project Name>

## Scope
## Success criteria
## Timeline
## Open risks
""",
    ),
)


async def bootstrap_vault_templates() -> int:
    """Seed `90-Templates/` with reference shapes on first boot.

    Idempotent — existing template files are NEVER overwritten so the
    user's customizations stick. Returns the count of files newly
    written. No-op when the vault isn't configured.
    """
    if VAULT_TEMPLATES is None:
        return 0
    VAULT_TEMPLATES.mkdir(parents=True, exist_ok=True)
    written = 0
    for filename, body in _VAULT_TEMPLATES:
        path = VAULT_TEMPLATES / filename
        if path.exists():
            continue
        if await _write_text_with_timeout(path, body, what="vault-template"):
            written += 1
    if written:
        log.info("Seeded %d vault template(s)", written)
    return written


def cleanup_vault_stragglers() -> int:
    """Delete zero-byte meeting files left behind by crashed/aborted writes.

    Walks every bucket root recursively — meetings can live under any of
    Inbox / Customers / Internal / Interviews / Personal / Daily. Safe
    no-op when the vault isn't configured or any root is absent.
    """
    total = 0
    roots: list[Path | None] = [
        VAULT_INBOX,
        VAULT_CUSTOMERS,
        VAULT_INTERNAL,
        VAULT_INTERVIEWS,
        VAULT_PERSONAL,
        VAULT_DAILY,
    ]
    for root in roots:
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


# ── Meeting write ───────────────────────────────────────────────────────────


async def _fetch_bucket_info(meeting_id: str) -> tuple[str, str | None, str | None]:
    """Load (bucket, customer, old_vault_path) for a meeting.

    Any of these can be NULL on a brand-new row — callers interpret
    `bucket=inbox` + `customer=None` + `old_vault_path=None` as "first
    write, nothing to clean up, land in inbox".
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT vault_bucket, vault_customer, vault_path FROM meetings WHERE id = ?",
            (meeting_id,),
        )
        row = await cursor.fetchone()
    if not row:
        return BUCKET_INBOX, None, None
    return (
        _normalize_bucket(row["vault_bucket"]),
        row["vault_customer"],
        row["vault_path"],
    )


def _attendee_links(speakers: list[str]) -> list[str]:
    """Wikilinks for real speakers — skip `Me`, `Unknown`, and `Speaker N`
    provisional placeholders. Unqualified `[[Name]]` links let Obsidian
    resolve to whichever `<Name>.md` exists under any People/ folder,
    which means the link keeps working if the person moves between
    customers later."""
    links: list[str] = []
    for s in speakers:
        if not s or s == "Me" or s == "Unknown":
            continue
        if re.match(r"^Speaker \d+$", s):
            continue
        links.append(f"[[{s}]]")
    return links


async def write_meeting(
    meeting_id: str,
    title: str,
    started_at: datetime,
    utterances: list[Utterance],
    summary: str,
    action_items: list[str],
) -> Path | None:
    """Write/overwrite the vault file for `meeting_id`.

    Pulls bucket + customer + prior vault_path from the DB so callers
    don't have to plumb them through. If the computed path differs from
    the stored `vault_path`, the old file is unlinked before the new
    one is written — that's how a finalize-time reclassify (inbox →
    customer) or a user rename (customer A → customer B) moves the file
    to its new home.

    Returns the final path, or None when Obsidian isn't configured.
    """
    if OBSIDIAN_VAULT is None:
        return None

    bucket, customer, old_vault_path = await _fetch_bucket_info(meeting_id)
    new_path = meeting_file_path(started_at, title, bucket, customer)
    if new_path is None:
        log.warning(
            "write_meeting: could not resolve path (bucket=%s customer=%s) for %s",
            bucket, customer, meeting_id,
        )
        return None

    # Remove the old file if we're writing somewhere else now. Best-effort
    # — if the unlink fails we still proceed, so Obsidian at least has a
    # fresh copy at the new path. A stray zero-byte file is cleaner up on
    # the next startup sweep.
    if old_vault_path:
        old_path = Path(old_vault_path)
        if old_path.exists() and old_path.resolve() != new_path.resolve():
            try:
                old_path.unlink()
            except Exception as e:
                log.warning("Could not delete stale vault file %s: %s", old_path, e)

    _ensure_path_parent(new_path)

    date_str = started_at.strftime("%Y-%m-%d")
    time_str = started_at.strftime("%H:%M")
    speakers = list({u.speaker for u in utterances})
    attendees = _attendee_links(speakers)
    attendees_json = "[" + ", ".join(f'"{a}"' for a in attendees) + "]"

    transcript_md = format_transcript(utterances)
    live_intel_md = await _render_live_intel_section(meeting_id)

    # Frontmatter. Keep it flat and agent-friendly — no nested structures.
    # `type: meeting` distinguishes from `type: daily-brief` / customer MOC.
    frontmatter_lines = [
        "---",
        "type: meeting",
        f"date: {date_str}",
        f"time: {time_str}",
        f"bucket: {bucket}",
    ]
    if bucket == BUCKET_CUSTOMER and customer:
        # Plain string (not a wikilink) — the folder path IS the link.
        # Keeps frontmatter queryable without double-resolving on render.
        frontmatter_lines.append(f"customer: {customer}")
    frontmatter_lines.extend(
        [
            f"meeting_id: {meeting_id}",
            f"attendees: {attendees_json}",
            "status: done" if summary else "status: in-progress",
            "tags: [aurascribe]",
            "---",
        ]
    )

    header_line = (
        f"> Recorded {date_str} at {time_str} · "
        f"{', '.join(attendees) if attendees else 'Solo'}"
    )

    content = (
        "\n".join(frontmatter_lines)
        + "\n\n"
        + f"# {title}\n\n"
        + header_line
        + "\n\n"
        + (summary.rstrip() + "\n" if summary else "")
        + live_intel_md
        + "\n---\n\n"
        + "## Transcript\n\n"
        + transcript_md
    )

    ok = await _write_text_with_timeout(new_path, content, what="meeting")
    if not ok:
        # Don't stamp vault_path or bump the throttle counters — keep
        # the next call to this function as a retry, not a repeat of
        # the previous skip. Transcripts still live in SQLite.
        return None

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE meetings SET vault_path = ? WHERE id = ?",
            (str(new_path), meeting_id),
        )
        await db.commit()

    _note_write(meeting_id)
    return new_path


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
    file. Used by the realtime intel loop, post-edit endpoints, and any
    caller that mutated the meeting row + wants the file updated."""
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


# ── Daily brief ─────────────────────────────────────────────────────────────


async def write_daily_brief(
    brief_date: str,
    brief: dict,
    meetings_meta: list[dict],
    generated_at: str,
) -> Path | None:
    """Write a Daily Brief markdown file into `50-Daily/YYYY/MM/YYYY-MM-DD.md`.

    `brief` matches the schema returned by `llm.daily_brief.build_brief` —
    tldr, highlights, decisions, action_items_self, action_items_others,
    open_threads, people, themes, tomorrow_focus, coaching. `meetings_meta`
    is a list of dicts with keys `title`, `started_at` (ISO), `bucket`,
    `customer` — used to produce wikilinks back to each meeting file.
    Callers built before bucket/customer existed can omit those keys; the
    link falls back to a plain title in that case.
    """
    path = daily_brief_file_path(brief_date)
    if path is None:
        return None
    _ensure_path_parent(path)

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
        "type: daily-brief",
        f"date: {brief_date}",
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
            # Plain `[[Name]]` — Obsidian resolves to whichever People/
            # folder holds the file, regardless of which customer owns it.
            name_link = f"[[{name}]]"
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
            bucket = _normalize_bucket(m.get("bucket"))
            customer = m.get("customer")
            try:
                started_dt = datetime.fromisoformat(started_raw)
            except Exception:
                started_dt = None
            link = (
                meeting_vault_link(started_dt, title, bucket, customer)
                if started_dt
                else None
            )
            if link:
                parts.append(f"- [[{link}|{title}]]")
            else:
                parts.append(f"- {title}")
        parts.append("")

    content = "\n".join(parts).rstrip() + "\n"
    if not await _write_text_with_timeout(path, content, what="daily-brief"):
        return None
    return path


# ── Person note stub ────────────────────────────────────────────────────────


async def update_person_note(
    person_name: str,
    updated_notes: str,
    meeting_title: str,
    meeting_started_at: datetime | None = None,
    *,
    bucket: str = BUCKET_INTERNAL,
    customer: str | None = None,
) -> Path | None:
    """Create or update a Person note in the appropriate People/ folder.

    Routing: customer meeting → `10-Customers/<Customer>/People/<Name>.md`,
    internal → `20-Internal/People/<Name>.md`. Other buckets don't get
    auto-people notes (inbox because we haven't classified yet; interview
    / personal because people there aren't worth indexing as contacts).

    `updated_notes` becomes the Notes section; the existing `## Meetings`
    list is preserved and the new meeting is appended as a wikilink.
    """
    if OBSIDIAN_VAULT is None:
        return None
    path = person_file_path(person_name, bucket=bucket, customer=customer)
    if path is None:
        # Bucket isn't one that hosts people notes — skip silently.
        return None

    _ensure_path_parent(path)

    existing = ""
    if path.exists():
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            existing = await f.read()

    meetings_section = ""
    if "## Meetings" in existing:
        after = existing.split("## Meetings", 1)[1]
        meetings_section = "## Meetings" + after

    today = date.today().isoformat()
    link_target = (
        meeting_vault_link(meeting_started_at, meeting_title, bucket, customer)
        if meeting_started_at
        else None
    )
    if link_target:
        meeting_link = f"- [[{link_target}|{meeting_title}]]"
    else:
        meeting_link = f"- {meeting_title}"

    if meetings_section:
        meetings_section = meetings_section.rstrip() + f"\n{meeting_link}\n"
    else:
        meetings_section = f"\n## Meetings\n{meeting_link}\n"

    frontmatter = [
        "---",
        "type: person",
        f"name: {person_name}",
    ]
    if bucket == BUCKET_CUSTOMER and customer:
        # The folder path already implies the customer, but a parallel
        # field keeps Dataview queries one-liner-friendly.
        frontmatter.append(f"customer: {customer}")
    frontmatter.extend(
        [
            "tags: [person, aurascribe]",
            f"last_updated: {today}",
            "---",
        ]
    )

    content = (
        "\n".join(frontmatter)
        + f"\n\n# {person_name}\n\n"
        + "## Notes\n"
        + (updated_notes.rstrip() + "\n" if updated_notes else "")
        + meetings_section
    )

    if not await _write_text_with_timeout(path, content, what="person-note"):
        return None
    return path
