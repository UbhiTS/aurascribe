"""Transcription engine protocol + stub implementation.

Phase 3 adds `whisper.WhisperEngine` (faster-whisper). Phase 4 adds real
pyannote 3.1 diarization, which layers on top of the engine output.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

import numpy as np


@dataclass
class Utterance:
    speaker: str
    text: str
    start: float
    end: float
    # Pickled pyannote embedding for this chunk. None when pyannote isn't
    # loaded; otherwise stored on the utterance row so the user can later
    # re-assign the speaker and fold the embedding into that person's pool.
    embedding: bytes | None = None
    # DB primary key (uuid4). Populated by `_save_utterances()`.
    id: str | None = None
    # Cosine distance from the matched speaker's centroid. Lower = more
    # confident. None for "Unknown" (no match), or for pre-migration rows.
    # Consumers use this to visually group consecutive high-confidence
    # same-speaker utterances.
    match_distance: float | None = None
    # Wall-clock offset (seconds) of this utterance's start in the meeting's
    # Opus recording file. Distinct from `start` (which is speech-time, i.e.
    # silence-skipped). Used by the UI to seek audio on click. None when the
    # meeting predates the audio-recording feature.
    audio_start: float | None = None


PartialCallback = Callable[[str, str], None]  # (speaker, partial_text)
StageCallback = Callable[[str], Awaitable[None]]  # (human-readable stage)


class TranscriptionEngine(Protocol):
    # `on_stage` is optional — StubEngine ignores it; WhisperEngine uses
    # it to broadcast "Downloading …" / "Loading diarization …" between
    # its heavy load phases so the splash stays informative on first run.
    async def load(self, on_stage: StageCallback | None = None) -> None: ...
    async def reload_voices(self) -> None: ...
    async def transcribe(
        self,
        audio: np.ndarray,
        on_partial: PartialCallback | None = None,
        *,
        diarize: bool = True,
    ) -> list[Utterance]: ...


class StubEngine:
    """No-op engine. `transcribe()` returns []. Used until Phase 3 lands."""

    def __init__(self) -> None:
        self._ready = False

    async def load(self, on_stage: StageCallback | None = None) -> None:
        if on_stage:
            await on_stage("StubEngine loaded")
        self._ready = True

    async def reload_voices(self) -> None:
        pass

    async def transcribe(
        self,
        audio: np.ndarray,
        on_partial: PartialCallback | None = None,
        *,
        diarize: bool = True,
    ) -> list[Utterance]:
        return []
