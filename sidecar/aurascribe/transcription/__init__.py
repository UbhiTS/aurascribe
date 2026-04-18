"""Transcription engine package.

- `engine.Utterance` — result dataclass
- `engine.TranscriptionEngine` — protocol
- `engine.StubEngine` — no-op (returns [])
- `whisper.WhisperEngine` — faster-whisper + optional pyannote speaker ID
"""
from aurascribe.transcription.engine import Utterance, TranscriptionEngine, StubEngine

__all__ = ["Utterance", "TranscriptionEngine", "StubEngine", "default_engine"]


def default_engine() -> TranscriptionEngine:
    """Instantiate the default engine.

    Importing `WhisperEngine` is deferred so the package is importable even when
    the [asr] extra isn't installed.
    """
    from aurascribe.transcription.whisper import WhisperEngine
    return WhisperEngine()
