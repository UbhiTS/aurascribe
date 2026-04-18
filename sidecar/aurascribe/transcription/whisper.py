"""faster-whisper ASR + optional pyannote embedding speaker match.

Speaker ID is a soft dep: if pyannote (the [diarization] extra) isn't
installed or HF_TOKEN is missing, the engine still works — utterances
come back with speaker="Unknown" and no embedding attached.

Each chunk's pyannote embedding is attached to the produced utterances so
the caller can persist it. When the user later assigns an "Unknown"
utterance to a speaker, that stored embedding is folded into the speaker's
pool — the matcher improves online.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pickle

import aiosqlite
import numpy as np

from aurascribe.config import (
    DB_PATH,
    HF_TOKEN,
    MODELS_DIR,
    SAMPLE_RATE,
    WHISPER_COMPUTE_TYPE,
    WHISPER_DEVICE,
    WHISPER_LANGUAGE,
    WHISPER_MODEL,
)
from aurascribe.transcription.engine import PartialCallback, Utterance

log = logging.getLogger("aurascribe.whisper")

# Cosine-distance thresholds. Override via env for tuning.
_THRESH_MULTI = float(os.environ.get("SPEAKER_THRESH_MULTI", "0.55"))
_THRESH_SOLO = float(os.environ.get("SPEAKER_THRESH_SOLO", "0.70"))
# Ratio test: best speaker must beat second-best by this margin. Rejects
# ambiguous chunks where two enrolled speakers are near-tied.
_RATIO_MARGIN = float(os.environ.get("SPEAKER_RATIO_MARGIN", "0.80"))


class WhisperEngine:
    """ASR via faster-whisper; speaker via pyannote embedding (if available)."""

    def __init__(self, enable_speaker_id: bool = True) -> None:
        self._model = None
        self._embedding_inference = None
        self._enable_speaker_id = enable_speaker_id
        # {speaker_name: [embedding, embedding, ...]} — one name has many
        # embeddings as online learning grows their pool.
        self._enrolled_pools: dict[str, list[np.ndarray]] = {}
        self._ready = False

    async def load(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._load_sync)
        await self._load_enrolled()
        self._ready = True

    def _load_sync(self) -> None:
        from faster_whisper import WhisperModel

        log.info(
            "Loading faster-whisper: model=%s device=%s compute=%s",
            WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE,
        )
        self._model = WhisperModel(
            WHISPER_MODEL,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
            download_root=str(MODELS_DIR),
        )

        if self._enable_speaker_id:
            try:
                from pyannote.audio import Inference, Model

                log.info("Loading pyannote/embedding for speaker ID")
                embedding_model = Model.from_pretrained("pyannote/embedding", token=HF_TOKEN)
                self._embedding_inference = Inference(embedding_model, window="whole")
            except Exception as e:
                log.warning(
                    "Speaker ID disabled (pyannote unavailable or HF_TOKEN missing): %s", e
                )
                self._embedding_inference = None

    async def _load_enrolled(self) -> None:
        pools: dict[str, list[np.ndarray]] = {}
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT p.name, se.embedding FROM speaker_enrollment se "
                    "JOIN people p ON p.id = se.person_id"
                )
                async for name, emb_bytes in cursor:
                    if emb_bytes is None:
                        continue
                    pools.setdefault(name, []).append(pickle.loads(emb_bytes))
        except Exception as e:
            log.warning("Could not load enrolled speakers: %s", e)
        self._enrolled_pools = pools
        log.info(
            "Enrolled speakers: %s",
            {name: len(pool) for name, pool in pools.items()},
        )

    async def reload_enrolled(self) -> None:
        await self._load_enrolled()

    async def transcribe(
        self,
        audio: np.ndarray,
        on_partial: PartialCallback | None = None,
    ) -> list[Utterance]:
        if not self._ready:
            raise RuntimeError("Engine not loaded — call load() first")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._transcribe_sync, audio, on_partial)

    def _transcribe_sync(
        self, audio: np.ndarray, on_partial: PartialCallback | None = None
    ) -> list[Utterance]:
        # Compute the pyannote embedding once; reuse for match + persist.
        embedding = self._compute_embedding(audio)
        speaker = self._match_speaker(embedding) if embedding is not None else "Unknown"
        embedding_bytes = pickle.dumps(embedding) if embedding is not None else None

        segments, _info = self._model.transcribe(
            audio,
            beam_size=5,
            language=WHISPER_LANGUAGE,
            condition_on_previous_text=False,
            vad_filter=False,  # audio is already VAD-gated upstream
        )

        utterances: list[Utterance] = []
        accumulated = ""
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            utterances.append(
                Utterance(
                    speaker=speaker,
                    text=text,
                    start=seg.start,
                    end=seg.end,
                    embedding=embedding_bytes,
                )
            )
            accumulated = (accumulated + " " + text).strip()
            if on_partial:
                on_partial(speaker, accumulated)

        return utterances

    # ── Speaker identification helpers ────────────────────────────────────

    def _compute_embedding(self, audio: np.ndarray):
        if self._embedding_inference is None:
            return None
        try:
            import torch

            waveform = torch.from_numpy(audio).unsqueeze(0)
            return self._embedding_inference(
                {"waveform": waveform, "sample_rate": SAMPLE_RATE}
            )
        except Exception as e:
            log.warning("Embedding compute failed: %s", e)
            return None

    def _match_speaker(self, embedding) -> str:
        if not self._enrolled_pools:
            return "Unknown"

        from scipy.spatial.distance import cosine

        # For each enrolled speaker, take the MIN distance across their pool.
        per_speaker: list[tuple[str, float]] = []
        for name, pool in self._enrolled_pools.items():
            if not pool:
                continue
            best = min(float(cosine(embedding, emb)) for emb in pool)
            per_speaker.append((name, best))
        if not per_speaker:
            return "Unknown"
        per_speaker.sort(key=lambda nd: nd[1])

        only_one = len(per_speaker) == 1
        best_name, best_dist = per_speaker[0]
        second_dist = per_speaker[1][1] if len(per_speaker) > 1 else float("inf")
        threshold = _THRESH_SOLO if only_one else _THRESH_MULTI

        passes_abs = best_dist < threshold
        passes_ratio = only_one or (best_dist < _RATIO_MARGIN * second_dist)
        decision = best_name if (passes_abs and passes_ratio) else "Unknown"

        log.info(
            "speaker-id: per_speaker=%s best=%s dist=%.3f second=%.3f "
            "thresh=%.2f ratio_margin=%.2f abs=%s ratio=%s -> %s",
            [(n, round(d, 3)) for n, d in per_speaker],
            best_name, best_dist, second_dist, threshold, _RATIO_MARGIN,
            passes_abs, passes_ratio, decision,
        )
        return decision
