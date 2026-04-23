"""Writes meetings, people notes, and daily briefs into the Obsidian vault.

Generic layout — no taxonomy beyond date and person. Users add their own
tags / folders for customers, projects, teams in Obsidian if they want;
AuraScribe doesn't impose a hierarchy.

  Meetings/YYYY/YYYY-MM-DD/<HH-MM> - <title>.md
  People/<Display Name>.md       (voice_id in frontmatter is the real identity key)
  Daily/YYYY-MM-DD.md            (flat by date — generated daily briefs)

People-note identity is keyed by `voice_id` in the note's frontmatter,
NOT the filename. Users can rename a People note in Obsidian and we'll
still find it on the next write. On filename collision (two people with
the same display name) we disambiguate the file *on creation* with a
readable suffix drawn from email domain → org → short voice_id hash.

If `OBSIDIAN_VAULT` isn't configured, every writer function returns None
and the rest of the app carries on — transcripts still land in SQLite.
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
    VAULT_DAILY,
    VAULT_MEETINGS,
    VAULT_PEOPLE,
)
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
    path timed out (meeting-file vs. person-note vs. daily-brief).
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


def _ensure_path_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


# ── Meeting path resolution ─────────────────────────────────────────────────


def meeting_file_path(started_at: datetime, title: str) -> Path | None:
    """Canonical path for a meeting's vault file.

    `Meetings/YYYY/YYYY-MM-DD/<HH-MM> - <title>.md`. Year folder keeps
    the file browser usable past year one; day folder groups the day's
    standups / 1:1s / customer calls without forcing the user into any
    taxonomy beyond "what day was this".
    """
    if VAULT_MEETINGS is None:
        return None
    year = started_at.strftime("%Y")
    day = started_at.strftime("%Y-%m-%d")
    time_part = started_at.strftime("%H-%M")
    stem = f"{time_part} - {_safe_filename_part(title)}"
    return VAULT_MEETINGS / year / day / f"{stem}.md"


def meeting_vault_link(started_at: datetime, title: str) -> str | None:
    """Vault-relative wikilink target (no `.md`) — e.g.
    `Meetings/2026/2026-04-22/09-30 - Conviva kickoff`.
    """
    path = meeting_file_path(started_at, title)
    if path is None or OBSIDIAN_VAULT is None:
        return None
    try:
        rel = path.relative_to(OBSIDIAN_VAULT).with_suffix("")
    except ValueError:
        # Path isn't under the vault (shouldn't happen but stays defensive).
        return None
    return rel.as_posix()


def daily_brief_file_path(brief_date: str) -> Path | None:
    """Canonical path for a daily brief: `Daily/YYYY-MM/YYYY-MM-DD.md`.

    Month folders keep each directory to ~31 files — scanning "what did
    I work on in March?" stays usable, and Obsidian's file tree doesn't
    turn into a wall of 365+ entries after a year of use.
    """
    if VAULT_DAILY is None or len(brief_date) < 7:
        return None
    year_month = brief_date[:7]  # "YYYY-MM"
    return VAULT_DAILY / year_month / f"{brief_date}.md"


# ── People-note lookup (voice_id is the real identity key) ──────────────────
#
# Filename is cosmetic: `People/John Smith.md`. The source of truth for
# "which file is John Smith" is the `voice_id` frontmatter field. This
# means the user can rename a People note in Obsidian and AuraScribe will
# still find it on the next write — we look up by voice_id, never by name.
#
# Index cache rebuilds on People/ mtime change or on our own write (we
# invalidate immediately so back-to-back writes within the same mtime
# granularity stay consistent).

_VOICE_ID_RE = re.compile(r"^voice_id:\s*(\S+)\s*$", re.MULTILINE)

_people_index: dict[str, Path] = {}
_people_index_mtime: float | None = None


def _read_voice_id(path: Path) -> str | None:
    """Parse the `voice_id:` line from a People note's frontmatter.

    Deliberately small — reads only the first ~2KB (frontmatter is tiny,
    and we hit every People file on index rebuild). Returns None if the
    file is missing / not a People note / has no voice_id, which is the
    right behavior: the writer treats such files as untracked and will
    disambiguate around them if the display-name collides.
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            head = f.read(2048)
    except OSError:
        return None
    m = _VOICE_ID_RE.search(head)
    return m.group(1) if m else None


def _people_index_rebuild() -> None:
    """Scan `People/` and refresh the voice_id → path map."""
    global _people_index, _people_index_mtime
    _people_index = {}
    if VAULT_PEOPLE is None or not VAULT_PEOPLE.exists():
        _people_index_mtime = None
        return
    try:
        _people_index_mtime = VAULT_PEOPLE.stat().st_mtime
    except OSError:
        _people_index_mtime = None
    for path in sorted(VAULT_PEOPLE.glob("*.md")):
        voice_id = _read_voice_id(path)
        if voice_id:
            # First hit wins — sorted() gives a deterministic choice if
            # the user has accidentally duplicated a voice_id across two
            # files (which they shouldn't, but might).
            _people_index.setdefault(voice_id, path)


def _people_index_current() -> dict[str, Path]:
    """Return the current voice_id → path map, rebuilding if stale."""
    if VAULT_PEOPLE is None:
        return {}
    if not VAULT_PEOPLE.exists():
        if _people_index:
            _people_index.clear()
        return _people_index
    try:
        mtime = VAULT_PEOPLE.stat().st_mtime
    except OSError:
        mtime = None
    if mtime != _people_index_mtime or not _people_index:
        _people_index_rebuild()
    return _people_index


def _people_index_remember(voice_id: str, path: Path) -> None:
    """Record a just-written People note in the index so the next lookup
    doesn't waste a rescan on a still-fresh mtime."""
    global _people_index_mtime
    _people_index[voice_id] = path
    # Bump mtime so a subsequent _people_index_current() call trusts the
    # in-memory update instead of re-scanning.
    try:
        if VAULT_PEOPLE is not None:
            _people_index_mtime = VAULT_PEOPLE.stat().st_mtime
    except OSError:
        pass


# Free-mail domains that don't disambiguate anything — if a user's only
# distinguishing info is "@gmail.com" we skip to the next priority.
_FREEMAIL_DOMAINS = frozenset({
    "gmail", "googlemail", "yahoo", "outlook", "hotmail", "live", "msn",
    "icloud", "me", "mac", "proton", "protonmail", "pm", "aol",
})


def _email_disambiguator(email: str | None) -> str | None:
    """Second-level label from an email domain, lowercased.

    `jane@acme.com` → `acme`. `jane@mail.acme.co.uk` → `acme`.
    Freemail addresses return None — they don't disambiguate the person,
    so the caller falls through to org / hash.
    """
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[-1].strip().lower()
    if not domain:
        return None
    labels = [p for p in domain.split(".") if p]
    if len(labels) < 2:
        return None
    # Strip common TLDs + country ccTLDs (e.g. `co.uk`, `com.au`).
    country_ccs = {"uk", "au", "nz", "in", "ca", "jp", "sg", "za", "br"}
    while len(labels) > 1 and labels[-1] in country_ccs | {
        "com", "net", "org", "io", "ai", "co", "edu", "gov",
    }:
        labels.pop()
    sld = labels[-1] if labels else None
    if not sld or sld in _FREEMAIL_DOMAINS:
        return None
    return _safe_filename_part(sld)


_ORG_STOPWORDS = frozenset({
    "inc", "inc.", "llc", "llc.", "ltd", "ltd.", "corp", "corp.",
    "co", "co.", "gmbh", "pty", "s.a.", "sa", "ag", "plc", "kk",
})


def _org_disambiguator(org: str | None) -> str | None:
    """Condensed org name, lowercased — strips legal suffixes.

    `Acme Corporation Inc.` → `acme corporation`. Capped at two tokens
    so the filename doesn't blow up for a long org name.
    """
    if not org:
        return None
    tokens = [t for t in re.split(r"\s+", org.strip()) if t]
    tokens = [t for t in tokens if t.lower().rstrip(",") not in _ORG_STOPWORDS]
    if not tokens:
        return None
    short = " ".join(tokens[:2]).lower()
    return _safe_filename_part(short)


def _hash_disambiguator(voice_id: str) -> str:
    """First 6 hex chars of the voice_id, stripped of dashes.

    Deterministic, guaranteed unique across voices, ugly but unfailing.
    """
    return voice_id.replace("-", "").lower()[:6] or "xxxxxx"


def _pick_disambiguation_suffix(
    voice_id: str,
    email: str | None,
    org: str | None,
    *,
    taken: set[Path],
    base_stem: str,
) -> str:
    """Choose a readable suffix for a colliding People filename.

    Priority: email domain → org → short voice_id hash. If the chosen
    suffix still collides with another taken path (because a previous
    collision burned the same slot), fall through to the next source;
    if all three end up taken, append `-2`, `-3` to the hash suffix
    until the path is free.
    """
    candidates: list[str] = []
    e = _email_disambiguator(email)
    if e:
        candidates.append(e)
    o = _org_disambiguator(org)
    if o and o not in candidates:
        candidates.append(o)
    h = _hash_disambiguator(voice_id)
    if h not in candidates:
        candidates.append(h)

    for cand in candidates:
        path = VAULT_PEOPLE / f"{base_stem} ({cand}).md"  # type: ignore[operator]
        if path not in taken and not path.exists():
            return cand

    # Everything taken — append -2, -3, ... to the hash until unique.
    n = 2
    while True:
        cand = f"{h}-{n}"
        path = VAULT_PEOPLE / f"{base_stem} ({cand}).md"  # type: ignore[operator]
        if path not in taken and not path.exists():
            return cand
        n += 1


def _find_person_path_by_voice_id(voice_id: str) -> Path | None:
    """Current path of the People note for `voice_id`, or None.

    Honors user renames — if the user renamed the file in Obsidian,
    we find it through its frontmatter. Returns None for vault
    un-configured or voice never seen before.
    """
    if not voice_id or VAULT_PEOPLE is None:
        return None
    index = _people_index_current()
    path = index.get(voice_id)
    if path and path.exists():
        return path
    if path and not path.exists():
        # Stale — user deleted the file. Evict + force a rescan in case
        # they renamed it AND our mtime cache missed the change.
        _people_index.pop(voice_id, None)
        _people_index_rebuild()
        return _people_index.get(voice_id)
    return None


def _resolve_person_path(
    voice_id: str,
    display_name: str,
    email: str | None,
    org: str | None,
) -> Path | None:
    """Path to write for (voice_id, display_name) — existing or new.

    - If the voice already has a People note anywhere under `People/`,
      return that path (user may have renamed it; we don't move it).
    - Otherwise pick `People/<display>.md`, falling back to a
      disambiguation suffix if that filename is taken by another voice.
    """
    if VAULT_PEOPLE is None:
        return None
    existing = _find_person_path_by_voice_id(voice_id)
    if existing is not None:
        return existing

    VAULT_PEOPLE.mkdir(parents=True, exist_ok=True)
    base_stem = _safe_filename_part(display_name)
    base_path = VAULT_PEOPLE / f"{base_stem}.md"
    if not base_path.exists():
        return base_path

    # Base filename is taken by a different voice — disambiguate.
    existing_voice = _read_voice_id(base_path)
    if existing_voice == voice_id:
        # Race: voice_id index missed this file (e.g. freshly added
        # externally). Claim it.
        return base_path
    suffix = _pick_disambiguation_suffix(
        voice_id, email, org, taken=set(), base_stem=base_stem,
    )
    return VAULT_PEOPLE / f"{base_stem} ({suffix}).md"


def person_vault_link(voice_id: str, display_name: str) -> str | None:
    """Wikilink target for a speaker — `[[John Smith]]` when unambiguous,
    `[[John Smith (acme)|John Smith]]` when the People note was
    disambiguated. Returns None when we can't resolve a path at all."""
    if VAULT_PEOPLE is None:
        return None
    path = _find_person_path_by_voice_id(voice_id)
    if path is None:
        # Person hasn't been written yet — use the display name directly.
        # The link will resolve once the People note lands.
        return f"[[{_safe_filename_part(display_name)}]]"
    stem = path.stem
    if stem == _safe_filename_part(display_name):
        return f"[[{stem}]]"
    return f"[[{stem}|{_safe_filename_part(display_name)}]]"


# ── Vault bootstrap + maintenance ───────────────────────────────────────────


async def bootstrap_vault_layout() -> int:
    """Create the three top-level folders on first boot.

    Idempotent — existing folders are left alone. Returns the number of
    folders newly created. No template files are seeded; AuraScribe
    composes every file's content directly and a generic "template"
    would just be noise in a new vault.
    """
    if OBSIDIAN_VAULT is None:
        return 0
    created = 0
    for root in (VAULT_MEETINGS, VAULT_PEOPLE, VAULT_DAILY):
        if root is None:
            continue
        if not root.exists():
            try:
                root.mkdir(parents=True, exist_ok=True)
                created += 1
            except OSError as e:
                log.warning("Could not create %s: %s", root, e)
    if created:
        log.info("Created %d vault folder(s) on bootstrap", created)
    return created


# Back-compat alias — some callers still import `bootstrap_vault_templates`
# from the old layout. The function no longer seeds anything, just ensures
# the three folders exist.
bootstrap_vault_templates = bootstrap_vault_layout


def cleanup_vault_stragglers() -> int:
    """Delete zero-byte meeting / people / brief files left behind by
    crashed or aborted writes. No-op when the vault isn't configured."""
    total = 0
    for root in (VAULT_MEETINGS, VAULT_PEOPLE, VAULT_DAILY):
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


_PROVISIONAL_SPEAKER_RE = re.compile(r"^Speaker \d+$")


def _real_speakers(utterances: list[Utterance]) -> list[str]:
    """Distinct speaker names that represent real attendees.

    Excludes `Me`, `Unknown`, and provisional `Speaker N` placeholders
    — those don't belong in attendee lists or as People links.
    """
    seen: set[str] = set()
    out: list[str] = []
    for u in utterances:
        s = u.speaker
        if not s or s == "Me" or s == "Unknown":
            continue
        if _PROVISIONAL_SPEAKER_RE.match(s):
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


async def _attendee_voice_ids(speakers: list[str]) -> dict[str, str]:
    """Map speaker display-name → voice_id for each known speaker.

    Skips unknowns — lookups that miss the voices table don't appear in
    the result. Callers use the map to build alias wikilinks and the
    `attendee_voice_ids` frontmatter array.
    """
    if not speakers:
        return {}
    out: dict[str, str] = {}
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Parameterize explicitly — sqlite3 doesn't support IN (?) with a
        # list, so we build the placeholder list ourselves.
        placeholders = ",".join("?" * len(speakers))
        cursor = await db.execute(
            f"SELECT id, name FROM voices WHERE name IN ({placeholders})",
            speakers,
        )
        async for row in cursor:
            out[row["name"]] = row["id"]
    return out


async def write_meeting(
    meeting_id: str,
    title: str,
    started_at: datetime,
    utterances: list[Utterance],
    summary: str,
    action_items: list[str],
) -> Path | None:
    """Write/overwrite the vault file for `meeting_id`.

    Reads the prior `vault_path` from the DB; if the computed path
    differs (e.g. user renamed the meeting mid-recording) the old file
    is unlinked before the new one is written.

    Returns the final path, or None when Obsidian isn't configured.
    """
    if OBSIDIAN_VAULT is None:
        return None

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT vault_path FROM meetings WHERE id = ?", (meeting_id,),
        )
        row = await cursor.fetchone()
    old_vault_path = row["vault_path"] if row else None

    new_path = meeting_file_path(started_at, title)
    if new_path is None:
        log.warning("write_meeting: could not resolve path for %s", meeting_id)
        return None

    # Remove the old file if we're writing somewhere else now. Best-effort
    # — a failed unlink still lets us write to the new path.
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
    speakers = _real_speakers(utterances)
    voice_ids_by_name = await _attendee_voice_ids(speakers)

    # Build attendee wikilinks + parallel voice_id array.
    attendee_links: list[str] = []
    attendee_ids: list[str] = []
    for name in speakers:
        voice_id = voice_ids_by_name.get(name)
        if voice_id:
            link = person_vault_link(voice_id, name) or f"[[{_safe_filename_part(name)}]]"
        else:
            # Voice row doesn't exist yet (shouldn't happen once the
            # meeting has finalized — rename-speaker creates one — but
            # stays defensive). Fall back to a plain wikilink.
            link = f"[[{_safe_filename_part(name)}]]"
        attendee_links.append(link)
        if voice_id:
            attendee_ids.append(voice_id)

    attendees_json = "[" + ", ".join(f'"{a}"' for a in attendee_links) + "]"
    voice_ids_json = "[" + ", ".join(f'"{v}"' for v in attendee_ids) + "]"

    transcript_md = format_transcript(utterances)
    live_intel_md = await _render_live_intel_section(meeting_id)

    duration_sec: int | None = None
    if utterances:
        last = utterances[-1]
        duration_sec = int(max(0.0, last.end))

    # Frontmatter. Flat + agent-friendly — `type: meeting` distinguishes
    # from `type: daily-brief` / `type: person` elsewhere in the vault.
    frontmatter_lines = [
        "---",
        "type: meeting",
        f"meeting_id: {meeting_id}",
        f"date: {date_str}",
        f"time: {time_str}",
    ]
    if duration_sec is not None:
        frontmatter_lines.append(f"duration_sec: {duration_sec}")
    frontmatter_lines.extend(
        [
            f"attendees: {attendees_json}",
            f"attendee_voice_ids: {voice_ids_json}",
            "status: done" if summary else "status: in-progress",
            "tags: [aurascribe]",
            "---",
        ]
    )

    header_line = (
        f"> Recorded {date_str} at {time_str} · "
        f"{', '.join(attendee_links) if attendee_links else 'Solo'}"
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
    """Write a Daily Brief markdown file into `Daily/YYYY-MM-DD.md`.

    `brief` matches the schema returned by `llm.daily_brief.build_brief`.
    `meetings_meta` is a list of dicts with `title` + `started_at` keys
    used to produce wikilinks back to each meeting file.
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
            # Plain `[[Name]]` — Obsidian resolves to whichever People
            # note carries that filename stem. Daily briefs don't have
            # enough per-person context to use the disambiguated alias
            # form, and plain links resolve fine when names are unique.
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
    if not await _write_text_with_timeout(path, content, what="daily-brief"):
        return None
    return path


# ── Person note ─────────────────────────────────────────────────────────────


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _parse_frontmatter(raw: str) -> tuple[dict, str]:
    """Split a markdown file into (frontmatter-dict, body).

    Deliberately minimal YAML subset — we only need string scalars plus
    the couple of list fields we write ourselves. Nested mappings or
    block scalars fall back to raw strings, which is fine because we
    only read well-known keys and preserve everything else as-is.
    """
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return {}, raw
    front = m.group(1)
    body = raw[m.end():]
    data: dict = {}
    for line in front.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if key:
            data[key] = val
    return data, body


def _extract_meetings_section(body: str) -> tuple[str, str]:
    """Split existing People-note body into (pre-meetings, meetings-section).

    Meetings section starts at the first `## Meetings` line and runs to EOF
    (we don't expect anything after it). Pre-meetings is everything before.
    Either can be empty.
    """
    marker = "\n## Meetings"
    if marker in body:
        idx = body.index(marker)
        return body[:idx], body[idx + 1:]  # drop the leading \n
    # First-line variant.
    if body.startswith("## Meetings"):
        return "", body
    return body, ""


async def update_person_note(
    voice_id: str,
    person_name: str,
    updated_notes: str,
    meeting_title: str,
    meeting_started_at: datetime | None = None,
    *,
    email: str | None = None,
    org: str | None = None,
    role: str | None = None,
) -> Path | None:
    """Create or update the People note for `voice_id`.

    Filename is cosmetic: `People/<Display Name>.md` on first write,
    disambiguated with a readable suffix on collision with another
    voice. Once the file exists, identity is keyed by the `voice_id`
    frontmatter line, so a user rename in Obsidian is preserved across
    future writes.

    `updated_notes` becomes the Notes section; the existing `## Meetings`
    list is preserved and the new meeting appended as a wikilink.
    """
    if OBSIDIAN_VAULT is None or VAULT_PEOPLE is None:
        return None
    if not voice_id:
        log.warning("update_person_note: refusing to write without a voice_id (%s)", person_name)
        return None

    path = _resolve_person_path(voice_id, person_name, email, org)
    if path is None:
        return None
    _ensure_path_parent(path)

    existing = ""
    if path.exists():
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            existing = await f.read()

    _, body = _parse_frontmatter(existing)
    _, meetings_section = _extract_meetings_section(body)

    today = date.today().isoformat()
    link_target = (
        meeting_vault_link(meeting_started_at, meeting_title)
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
        meetings_section = f"## Meetings\n{meeting_link}\n"

    # Frontmatter — voice_id is the identity key, others are descriptive.
    frontmatter = [
        "---",
        "type: person",
        f"voice_id: {voice_id}",
        f"name: {person_name}",
    ]
    if email:
        frontmatter.append(f"email: {email}")
    if org:
        frontmatter.append(f"org: {org}")
    if role:
        frontmatter.append(f"role: {role}")
    frontmatter.extend(
        [
            "tags: [person, aurascribe]",
            f"last_seen: {today}",
            "---",
        ]
    )

    content = (
        "\n".join(frontmatter)
        + f"\n\n# {person_name}\n\n"
        + "## Notes\n"
        + (updated_notes.rstrip() + "\n\n" if updated_notes else "\n")
        + meetings_section
    )

    if not await _write_text_with_timeout(path, content, what="person-note"):
        return None
    _people_index_remember(voice_id, path)
    return path


async def get_person_note_body(voice_id: str) -> str:
    """Return the existing People-note contents for `voice_id`, or "" if none.

    Used by the LLM notes prompt to merge new insights with what's
    already written.
    """
    path = _find_person_path_by_voice_id(voice_id)
    if path is None:
        return ""
    try:
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            return await f.read()
    except Exception:
        return ""
