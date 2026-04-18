"""Speaker enrollment — record a short voice sample and store an embedding.

The embedding comes from the diarization pipeline (`WhisperEngine.embed_for_enrollment`)
so enrolled speakers live in the same vector space as the per-turn centroids
the live loop emits. No separate embedder is loaded.
"""
from __future__ import annotations

import asyncio
import pickle
import uuid
from datetime import datetime

import aiosqlite
import numpy as np

from aurascribe.config import CHANNELS, DB_PATH, SAMPLE_RATE


async def record_enrollment_sample(duration: float = 10.0) -> np.ndarray:
    """Record `duration` seconds from the default mic."""
    import sounddevice as sd

    loop = asyncio.get_running_loop()
    future: asyncio.Future[np.ndarray] = loop.create_future()

    def _record() -> None:
        audio = sd.rec(
            int(duration * SAMPLE_RATE),
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
        )
        sd.wait()
        loop.call_soon_threadsafe(future.set_result, audio[:, 0])

    loop.run_in_executor(None, _record)
    return await future


async def save_enrollment(engine, person_name: str, audio: np.ndarray) -> str:
    """Embed via `engine` (the WhisperEngine) and persist. Returns person_id."""
    embedding = await engine.embed_for_enrollment(audio)
    embedding_bytes = pickle.dumps(embedding)

    async with aiosqlite.connect(DB_PATH) as db:
        # Upsert person by name.
        cursor = await db.execute("SELECT id FROM people WHERE name = ?", (person_name,))
        row = await cursor.fetchone()
        if row is not None:
            person_id = str(row[0])
        else:
            person_id = str(uuid.uuid4())
            await db.execute(
                "INSERT INTO people (id, name, created_at) VALUES (?, ?, ?)",
                (person_id, person_name, datetime.now().isoformat()),
            )

        enrollment_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO speaker_enrollment (id, person_id, embedding, created_at) VALUES (?, ?, ?, ?)",
            (enrollment_id, person_id, embedding_bytes, datetime.now().isoformat()),
        )
        await db.commit()

    return person_id


async def load_enrolled_speakers() -> dict[str, np.ndarray]:
    """Return {person_id: embedding} for all enrolled speakers (most recent per person)."""
    result: dict[str, np.ndarray] = {}
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT se.person_id, se.embedding FROM speaker_enrollment se "
            "ORDER BY se.created_at DESC"
        )
        seen: set[str] = set()
        async for person_id, emb_bytes in cursor:
            pid = str(person_id)
            if pid not in seen:
                result[pid] = pickle.loads(emb_bytes)
                seen.add(pid)
    return result
