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
    vault_path  TEXT
);

CREATE TABLE IF NOT EXISTS utterances (
    id          TEXT PRIMARY KEY,
    meeting_id  TEXT NOT NULL REFERENCES meetings(id),
    speaker     TEXT NOT NULL,
    text        TEXT NOT NULL,
    start_time  REAL NOT NULL,
    end_time    REAL NOT NULL,
    embedding   BLOB,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
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

CREATE INDEX IF NOT EXISTS idx_utterances_meeting ON utterances(meeting_id);
CREATE INDEX IF NOT EXISTS idx_speaker_enrollment_utterance ON speaker_enrollment(utterance_id);
CREATE INDEX IF NOT EXISTS idx_speaker_enrollment_person ON speaker_enrollment(person_id);
"""


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
        await db.commit()
