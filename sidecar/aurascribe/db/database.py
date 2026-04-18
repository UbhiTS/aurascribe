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
    live_highlights                      TEXT,
    live_action_items_self               TEXT,
    live_action_items_others             TEXT,
    live_support_intelligence            TEXT,
    live_support_intelligence_history    TEXT
);

CREATE TABLE IF NOT EXISTS utterances (
    id             TEXT PRIMARY KEY,
    meeting_id     TEXT NOT NULL REFERENCES meetings(id),
    speaker        TEXT NOT NULL,
    text           TEXT NOT NULL,
    start_time     REAL NOT NULL,
    end_time       REAL NOT NULL,
    embedding      BLOB,
    match_distance REAL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS people (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    speaker_id  TEXT,
    vault_path  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS speaker_enrollment (
    id          TEXT PRIMARY KEY,
    person_id   TEXT REFERENCES people(id),
    embedding   BLOB NOT NULL,
    utterance_id TEXT REFERENCES utterances(id),
    meeting_id  TEXT REFERENCES meetings(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
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
CREATE INDEX IF NOT EXISTS idx_speaker_enrollment_utterance ON speaker_enrollment(utterance_id);
CREATE INDEX IF NOT EXISTS idx_speaker_enrollment_person ON speaker_enrollment(person_id);
"""

# Current embedding dimension produced by the speaker pipeline. When the DB's
# stored value doesn't match, we drop all embedding data (enrollments + stored
# per-utterance embeddings) since cross-dim comparison isn't meaningful.
_CURRENT_EMBEDDING_DIM = "256"


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        for statement in SCHEMA.split(";"):
            s = statement.strip()
            if s:
                await db.execute(s)

        # Crash-recovery reconciliation. If the sidecar was killed mid-meeting
        # (taskkill, crash, power loss), the row is still status='recording'.
        # The utterances that made it to disk are the truth — finalize the
        # row with ended_at = last utterance's timestamp.
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

        # Idempotent column adds for forward-only schema changes on existing
        # DBs (CREATE TABLE IF NOT EXISTS doesn't touch existing tables).
        cursor = await db.execute("PRAGMA table_info(utterances)")
        utterance_cols = {row[1] async for row in cursor}
        if "match_distance" not in utterance_cols:
            await db.execute("ALTER TABLE utterances ADD COLUMN match_distance REAL")

        cursor = await db.execute("PRAGMA table_info(meetings)")
        meeting_cols = {row[1] async for row in cursor}
        for col in (
            "live_highlights",
            "live_action_items_self",
            "live_action_items_others",
            "live_support_intelligence",
            "live_support_intelligence_history",
        ):
            if col not in meeting_cols:
                await db.execute(f"ALTER TABLE meetings ADD COLUMN {col} TEXT")

        # Embedding-dimension migration. If the stored dimension doesn't match
        # what the current pipeline produces, wipe the old-dim data — mixing
        # dimensions makes cosine-distance comparisons crash.
        cursor = await db.execute(
            "SELECT value FROM schema_meta WHERE key = 'embedding_dim'"
        )
        row = await cursor.fetchone()
        stored_dim = row[0] if row else None
        if stored_dim != _CURRENT_EMBEDDING_DIM:
            await db.execute("DELETE FROM speaker_enrollment")
            await db.execute("UPDATE utterances SET embedding = NULL")
            await db.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('embedding_dim', ?)",
                (_CURRENT_EMBEDDING_DIM,),
            )

        await db.commit()
