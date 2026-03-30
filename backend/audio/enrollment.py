"""
Speaker enrollment — record a short voice sample from the user
so AuraScribe can always label them as 'Me'.
"""
import asyncio
import io
import numpy as np
import sounddevice as sd
import torch
import aiosqlite
import pickle
from datetime import datetime

from backend.config import SAMPLE_RATE, CHANNELS, DB_PATH, HF_TOKEN


async def record_enrollment_sample(duration: float = 10.0) -> np.ndarray:
    """Record `duration` seconds from the default mic and return as numpy array."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[np.ndarray] = loop.create_future()

    def _record():
        audio = sd.rec(
            int(duration * SAMPLE_RATE),
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
        )
        sd.wait()
        loop.call_soon_threadsafe(future.set_result, audio[:, 0])

    thread = asyncio.get_running_loop().run_in_executor(None, _record)
    return await future


async def save_enrollment(person_name: str, audio: np.ndarray):
    """Extract speaker embedding and save to DB."""
    from pyannote.audio import Model, Inference

    model = Model.from_pretrained("pyannote/embedding", token=HF_TOKEN)
    inference = Inference(model, window="whole")

    # pyannote expects a dict with waveform tensor + sample_rate
    waveform = torch.from_numpy(audio).unsqueeze(0)  # (1, samples)
    embedding = inference({"waveform": waveform, "sample_rate": SAMPLE_RATE})
    embedding_bytes = pickle.dumps(embedding)

    async with aiosqlite.connect(DB_PATH) as db:
        # Upsert person
        await db.execute(
            "INSERT OR IGNORE INTO people (name, created_at) VALUES (?, ?)",
            (person_name, datetime.now().isoformat()),
        )
        cursor = await db.execute("SELECT id FROM people WHERE name = ?", (person_name,))
        row = await cursor.fetchone()
        person_id = row[0]

        await db.execute(
            "INSERT INTO speaker_enrollment (person_id, embedding, created_at) VALUES (?, ?, ?)",
            (person_id, embedding_bytes, datetime.now().isoformat()),
        )
        await db.commit()

    return person_id


async def load_enrolled_speakers() -> dict[int, np.ndarray]:
    """Returns {person_id: embedding_array} for all enrolled speakers."""
    result = {}
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT se.person_id, se.embedding FROM speaker_enrollment se "
            "ORDER BY se.created_at DESC"
        )
        seen = set()
        async for person_id, emb_bytes in cursor:
            if person_id not in seen:
                result[person_id] = pickle.loads(emb_bytes)
                seen.add(person_id)
    return result
