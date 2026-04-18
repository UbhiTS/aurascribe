"""SQLite schema + initialization.

Primary keys are TEXT UUIDs (uuid4). Chosen over autoincrement so the DB
can be merged across machines later without id collisions, and so external
references (vault paths, URLs) don't leak sequential meeting counts.
"""
from __future__ import annotations

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
    last_recomputed_at  TEXT
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

# Current embedding dimension produced by the speaker pipeline. When the DB's
# stored value doesn't match, we drop all embedding data since cross-dim
# comparison isn't meaningful.
_CURRENT_EMBEDDING_DIM = "256"

# Schema generation. Bump when the table shape changes in a way that prior
# data can't be carried across. On mismatch we DROP the legacy tables so a
# fresh schema can be created from scratch — meetings, utterances, voices
# all get wiped. This is a single-user personal app; migrations aren't
# worth the engineering cost.
_CURRENT_SCHEMA_VERSION = "voices-1"


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        # Foreign keys off by default in SQLite — turn them on so the
        # ON DELETE CASCADE/SET NULL clauses on voice_embeddings actually
        # fire when a meeting or voice is deleted.
        await db.execute("PRAGMA foreign_keys = ON")

        # Schema-version gate. On a version bump, drop every table that
        # might be shaped differently and let the schema block below
        # recreate them cleanly. Only `schema_meta` survives.
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
                "speaker_enrollment",  # legacy
                "utterances",
                "voices",
                "people",              # legacy
                "meetings",
                "daily_briefs",
            ):
                await db.execute(f"DROP TABLE IF EXISTS {table}")

        for statement in SCHEMA.split(";"):
            s = statement.strip()
            if s:
                await db.execute(s)

        await db.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (_CURRENT_SCHEMA_VERSION,),
        )
        await db.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('embedding_dim', ?)",
            (_CURRENT_EMBEDDING_DIM,),
        )

        # Idempotent column adds for forward-only schema changes on existing
        # DBs. CREATE TABLE IF NOT EXISTS doesn't touch existing tables, so
        # any column added after voices-1 schipped needs to be ALTERed in.
        cursor = await db.execute("PRAGMA table_info(meetings)")
        meeting_cols = {row[1] async for row in cursor}
        for col in ("last_tagged_at", "last_recomputed_at"):
            if col not in meeting_cols:
                await db.execute(f"ALTER TABLE meetings ADD COLUMN {col} TEXT")

        # Crash-recovery reconciliation. If the sidecar was killed mid-meeting
        # (taskkill, crash, power loss), the row is still status='recording'.
        # Finalize with ended_at = last utterance's timestamp.
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
