"""SQLite schema + initialization.

Primary keys are TEXT UUIDs (uuid4). Chosen over autoincrement so the DB
can be merged across machines later without id collisions, and so external
references (vault paths, URLs) don't leak sequential meeting counts.

────────────────────────────────────────────────────────────────────────
Schema-change policy — IMPORTANT.

AuraScribe is a single-user personal app. We do NOT write migration
code for the DB — no `ALTER TABLE ADD COLUMN`, no `PRAGMA table_info`
back-fill loops, no data preservation. When the schema needs to change:

  1. Edit the relevant `CREATE TABLE` block inside `SCHEMA`.
  2. Bump `_CURRENT_SCHEMA_VERSION`.

`init_db` detects the mismatch on next startup, drops every table, and
recreates from scratch. The user's previous meetings, voices, and
transcripts get wiped. That's acceptable for this app — migrating across
partially-applied schema changes produces a category of subtle bugs we
don't want to own.

The drop-and-recreate gate IS NOT migration code — it's the mechanism
that enforces the "always fresh" rule. Leave it alone.
────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import re

import aiosqlite

from aurascribe.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT 'Untitled Meeting',
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    status      TEXT NOT NULL DEFAULT 'recording',
    summary     TEXT,
    action_items TEXT,
    vault_path  TEXT,
    audio_path  TEXT,
    live_highlights                      TEXT,
    live_action_items_self               TEXT,
    live_action_items_others             TEXT,
    live_support_intelligence            TEXT,
    live_support_intelligence_history    TEXT,
    -- Bumped whenever a pill/voice change touches this meeting's labels.
    -- Compared against last_recomputed_at to surface a "Tags pending" badge
    -- so the user knows when an explicit Recompute would yield new info.
    last_tagged_at      TEXT,
    last_recomputed_at  TEXT,
    -- 0 = AI may update the title (live refinement + AI Summary).
    -- 1 = frozen, user owns the title. Flips to 1 automatically when
    -- the user types a custom title or picks a Sparkles suggestion,
    -- and can be toggled manually via PATCH /title-lock.
    title_locked        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS utterances (
    id             TEXT PRIMARY KEY,
    meeting_id     TEXT NOT NULL REFERENCES meetings(id),
    speaker        TEXT NOT NULL,
    text           TEXT NOT NULL,
    start_time     REAL NOT NULL,
    end_time       REAL NOT NULL,
    audio_start    REAL,
    embedding      BLOB,
    match_distance REAL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS voices (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    color       TEXT,
    -- File extension of an uploaded voice avatar (e.g. "png", "jpg").
    -- NULL = use the generated initials circle.
    avatar_ext  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS voice_embeddings (
    id           TEXT PRIMARY KEY,
    voice_id     TEXT NOT NULL REFERENCES voices(id) ON DELETE CASCADE,
    meeting_id   TEXT REFERENCES meetings(id) ON DELETE CASCADE,
    utterance_id TEXT REFERENCES utterances(id) ON DELETE SET NULL,
    embedding    BLOB NOT NULL,
    start_time   REAL,
    end_time     REAL,
    source       TEXT NOT NULL DEFAULT 'manual',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_briefs (
    date          TEXT PRIMARY KEY,
    brief_json    TEXT,
    meeting_ids   TEXT NOT NULL DEFAULT '[]',
    meeting_count INTEGER NOT NULL DEFAULT 0,
    generated_at  TEXT,
    is_stale      INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_utterances_meeting ON utterances(meeting_id);
CREATE INDEX IF NOT EXISTS idx_voice_embeddings_voice ON voice_embeddings(voice_id);
CREATE INDEX IF NOT EXISTS idx_voice_embeddings_meeting ON voice_embeddings(meeting_id);
CREATE INDEX IF NOT EXISTS idx_voice_embeddings_utterance ON voice_embeddings(utterance_id);
"""

# Bump this any time the SCHEMA block above changes shape. On mismatch,
# `init_db` drops every table and recreates from scratch — see the
# policy note at the top of this module.
_CURRENT_SCHEMA_VERSION = "title-locked-1"


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        # Foreign keys off by default in SQLite — turn them on so the
        # ON DELETE CASCADE/SET NULL clauses on voice_embeddings actually
        # fire when a meeting or voice is deleted.
        await db.execute("PRAGMA foreign_keys = ON")

        # Schema-version gate. On a version bump (or a fresh install
        # with no schema_meta at all), drop every table we own so the
        # CREATE TABLE block below recreates them cleanly. Only
        # `schema_meta` survives across resets.
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        )
        has_meta = await cursor.fetchone() is not None
        stored_version: str | None = None
        if has_meta:
            cursor = await db.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            )
            row = await cursor.fetchone()
            stored_version = row[0] if row else None

        if stored_version != _CURRENT_SCHEMA_VERSION:
            # Drop in child-before-parent order so FK constraints don't bite.
            for table in (
                "voice_embeddings",
                "utterances",
                "voices",
                "meetings",
                "daily_briefs",
            ):
                await db.execute(f"DROP TABLE IF EXISTS {table}")

        # Strip SQL line comments before splitting — aiosqlite/sqlite3
        # would happily accept `;` inside a `-- comment`, but our naive
        # split-by-semicolon doesn't. Stripping the comments first lets
        # contributors write natural-language comments without worrying
        # about whether punctuation will break the statement splitter.
        _schema_no_comments = re.sub(r"--[^\n]*", "", SCHEMA)
        for statement in _schema_no_comments.split(";"):
            s = statement.strip()
            if s:
                await db.execute(s)

        await db.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (_CURRENT_SCHEMA_VERSION,),
        )

        # Crash-recovery reconciliation. If the sidecar was killed mid-meeting
        # (taskkill, crash, power loss), the row is still status='recording'.
        # Finalize with ended_at = last utterance's timestamp. This is
        # runtime recovery, NOT a schema migration — it lands harmlessly on
        # a fresh DB (no rows) and matters only on subsequent restarts.
        await db.execute(
            """
            UPDATE meetings
               SET status = 'done',
                   ended_at = COALESCE(
                       ended_at,
                       (SELECT MAX(created_at) FROM utterances
                        WHERE utterances.meeting_id = meetings.id),
                       started_at
                   )
             WHERE status = 'recording'
            """
        )

        await db.commit()
