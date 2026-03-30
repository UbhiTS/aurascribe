import aiosqlite
from pathlib import Path
from backend.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL DEFAULT 'Untitled Meeting',
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    status      TEXT NOT NULL DEFAULT 'recording',  -- recording | processing | done
    summary     TEXT,
    action_items TEXT,   -- JSON array
    vault_path  TEXT
);

CREATE TABLE IF NOT EXISTS utterances (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id  INTEGER NOT NULL REFERENCES meetings(id),
    speaker     TEXT NOT NULL,
    text        TEXT NOT NULL,
    start_time  REAL NOT NULL,
    end_time    REAL NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    speaker_id  TEXT,       -- pyannote speaker label mapped to this person
    vault_path  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS speaker_enrollment (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER REFERENCES people(id),
    embedding   BLOB NOT NULL,  -- numpy array serialized
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        for statement in SCHEMA.split(";"):
            s = statement.strip()
            if s:
                await db.execute(s)
        await db.commit()


async def get_db():
    return aiosqlite.connect(DB_PATH)
