"""
Cohere Transcribe + pyannote speaker embedding pipeline.
Returns utterances with speaker labels.

Pipeline (per audio chunk):
  1. Cohere Transcribe → text
  2. Compute embedding of the chunk
  3. Match against enrolled speaker embeddings → name
"""
import asyncio
import numpy as np
import aiosqlite
import pickle
from dataclasses import dataclass

from backend.config import (
    HF_TOKEN, SAMPLE_RATE, WHISPER_DEVICE,
    WHISPER_LANGUAGE, DB_PATH
)

COHERE_MODEL = "CohereLabs/cohere-transcribe-03-2026"


@dataclass
class Utterance:
    speaker: str
    text: str
    start: float
    end: float


class TranscriptionEngine:
    def __init__(self):
        self._processor = None
        self._cohere = None
        self._embedding_inference = None  # pyannote Inference for speaker matching
        self._enrolled: dict[str, np.ndarray] = {}
        self._ready = False

    async def load(self):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._load_models)
        await self._load_enrolled_speakers()
        self._ready = True

    def _load_models(self):
        import torch
        from transformers import AutoProcessor, CohereAsrForConditionalGeneration
        from pyannote.audio import Model, Inference

        # Cohere Transcribe 2B — #1 English ASR accuracy
        self._processor = AutoProcessor.from_pretrained(COHERE_MODEL)
        self._cohere = CohereAsrForConditionalGeneration.from_pretrained(
            COHERE_MODEL,
            torch_dtype=torch.float16,
            device_map=WHISPER_DEVICE,
        )
        self._cohere.eval()

        # Speaker embedding model for enrolled-speaker name matching
        embedding_model = Model.from_pretrained("pyannote/embedding", token=HF_TOKEN)
        self._embedding_inference = Inference(embedding_model, window="whole")

    async def _load_enrolled_speakers(self):
        new_enrolled: dict[str, np.ndarray] = {}
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT p.name, se.embedding FROM speaker_enrollment se "
                    "JOIN people p ON p.id = se.person_id "
                    "ORDER BY se.created_at DESC"
                )
                seen: set[str] = set()
                async for name, emb_bytes in cursor:
                    if name not in seen:
                        new_enrolled[name] = pickle.loads(emb_bytes)
                        seen.add(name)
        except Exception:
            pass
        self._enrolled = new_enrolled  # atomic replace — clears stale names

    async def reload_enrolled(self):
        await self._load_enrolled_speakers()

    async def transcribe(
        self,
        audio: np.ndarray,
        on_partial=None,   # callable(speaker, partial_text) — called from worker thread
    ) -> list[Utterance]:
        if not self._ready:
            raise RuntimeError("Engine not loaded — call load() first")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._transcribe_sync, audio, on_partial)

    def _transcribe_sync(self, audio: np.ndarray, on_partial=None) -> list[Utterance]:
        import torch
        import threading
        from transformers import TextIteratorStreamer

        # 1. Identify speaker first (embedding on full chunk — fast)
        speaker = self._resolve_speaker(audio)
        duration = len(audio) / SAMPLE_RATE

        # 2. Prepare inputs
        inputs = self._processor(
            audio,
            sampling_rate=SAMPLE_RATE,
            return_tensors="pt",
            language=WHISPER_LANGUAGE,
        )
        inputs.pop("audio_chunk_index", None)  # not needed with streamer
        inputs = inputs.to(self._cohere.device, dtype=self._cohere.dtype)

        # 3. Stream generation token-by-token
        streamer = TextIteratorStreamer(
            self._processor.tokenizer,
            skip_special_tokens=True,
            skip_prompt=True,
        )
        gen_kwargs = {**inputs, "max_new_tokens": 448, "streamer": streamer}
        thread = threading.Thread(target=self._cohere.generate, kwargs=gen_kwargs)
        thread.start()

        full_text = ""
        for token in streamer:
            full_text += token
            stripped = full_text.strip()
            if on_partial and stripped:
                on_partial(speaker, stripped)

        thread.join()

        text = full_text.strip()
        if not text:
            return []
        return [Utterance(speaker=speaker, text=text, start=0.0, end=duration)]

    def _resolve_speaker(self, audio: np.ndarray) -> str:
        if not self._enrolled:
            return "Unknown"

        only_one_enrolled = len(self._enrolled) == 1
        sole_name = next(iter(self._enrolled)) if only_one_enrolled else None

        try:
            import torch
            from scipy.spatial.distance import cosine

            waveform = torch.from_numpy(audio).unsqueeze(0)
            embedding = self._embedding_inference({"waveform": waveform, "sample_rate": SAMPLE_RATE})

            best_name, best_dist = None, float("inf")
            for name, enrolled_emb in self._enrolled.items():
                dist = cosine(embedding, enrolled_emb)
                if dist < best_dist:
                    best_dist = dist
                    best_name = name

            # Solo mode: very loose threshold — only one possible speaker
            # Multi-person mode: tighter to avoid misattribution
            threshold = 0.70 if only_one_enrolled else 0.45
            if best_dist < threshold and best_name:
                return best_name

        except Exception:
            pass

        if only_one_enrolled:
            return sole_name
        return "Unknown"
