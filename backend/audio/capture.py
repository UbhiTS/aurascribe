"""
Continuous microphone capture with Silero VAD.
Yields audio chunks when speech is detected.
"""
import asyncio
from collections import deque
import numpy as np
import sounddevice as sd
from typing import AsyncGenerator

from backend.config import SAMPLE_RATE, CHANNELS, VAD_THRESHOLD, SILENCE_DURATION, CHUNK_DURATION

_STOP_SENTINEL = object()

# Ring buffer: keep last 30s of raw audio for rolling-window transcription
_RING_MAXLEN = int(30 * SAMPLE_RATE / 512)


class AudioCapture:
    def __init__(self):
        self._vad_model = None
        self._vad_utils = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream: sd.InputStream | None = None
        self._ring: deque[np.ndarray] = deque(maxlen=_RING_MAXLEN)

    def _audio_callback(self, indata: np.ndarray, frames: int, time, status):
        chunk = indata[:, 0].copy()  # mono
        self._ring.append(chunk)     # always store in ring buffer
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._queue.put_nowait, chunk)

    def get_recent_audio(self, seconds: float = 4.0) -> np.ndarray | None:
        """Return a snapshot of the last `seconds` of raw audio (thread-safe)."""
        frames_needed = int(seconds * SAMPLE_RATE / 512)
        snapshot = list(self._ring)[-frames_needed:]
        if not snapshot:
            return None
        return np.concatenate(snapshot)

    def mark_position(self):
        """Record current ring buffer length — use get_audio_since_mark() to get only new audio."""
        self._ring_mark = len(self._ring)

    def get_audio_since_mark(self, max_seconds: float = 10.0) -> np.ndarray | None:
        """Return audio recorded after the last mark(), up to max_seconds."""
        mark = getattr(self, "_ring_mark", 0)
        snapshot = list(self._ring)
        new_blocks = snapshot[mark:]
        if not new_blocks:
            return None
        max_frames = int(max_seconds * SAMPLE_RATE / 512)
        return np.concatenate(new_blocks[-max_frames:])

    def list_devices(self) -> list[dict]:
        devices = sd.query_devices()
        result = []
        for i, d in enumerate(devices):
            if d["max_input_channels"] > 0:
                result.append({"index": i, "name": d["name"], "channels": d["max_input_channels"]})
        return result

    async def start(self, device: int | None = None):
        import torch
        if self._vad_model is None:
            self._vad_model, self._vad_utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                trust_repo=True,
            )
            (self._get_speech_timestamps, _, self._read_audio, *_) = self._vad_utils
            self._vad_model.eval()
        # Drain any leftover items from a previous session
        while not self._queue.empty():
            self._queue.get_nowait()
        self._ring.clear()
        self._ring_mark = 0
        self._loop = asyncio.get_running_loop()
        self._stream = sd.InputStream(
            device=device,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=512,  # Silero VAD requires exactly 512 samples at 16kHz
            callback=self._audio_callback,
        )
        self._stream.start()

    async def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        # Signal the generator to flush and exit
        if self._loop and not self._loop.is_closed():
            await self._queue.put(_STOP_SENTINEL)

    def _is_speech(self, audio: np.ndarray) -> bool:
        import torch
        tensor = torch.from_numpy(audio).float()
        with torch.no_grad():
            confidence = self._vad_model(tensor, SAMPLE_RATE).item()
        return confidence >= VAD_THRESHOLD

    async def stream_speech_chunks(self) -> AsyncGenerator[np.ndarray, None]:
        """
        Yields numpy arrays of speech audio.
        Accumulates audio when speech is detected, yields on silence, max duration,
        or when stop() is called (flushes remaining buffer).
        Each block is 512 samples = 32ms at 16kHz.
        """
        buffer: list[np.ndarray] = []
        silence_frames = 0
        block_duration = 512 / SAMPLE_RATE
        silence_threshold_frames = int(SILENCE_DURATION / block_duration)
        max_frames = int(CHUNK_DURATION / block_duration)

        while True:
            chunk = await self._queue.get()

            # Stop sentinel — flush buffer and exit
            if chunk is _STOP_SENTINEL:
                if buffer:
                    yield np.concatenate(buffer)
                return

            if self._is_speech(chunk):
                buffer.append(chunk)
                silence_frames = 0
            elif buffer:
                silence_frames += 1
                buffer.append(chunk)

                if silence_frames >= silence_threshold_frames or len(buffer) >= max_frames:
                    yield np.concatenate(buffer)
                    buffer = []
                    silence_frames = 0
