"""Continuous mic capture with Silero VAD.

Yields audio chunks when speech is detected. Torch is a soft dep — imported
lazily in `start()` so the sidecar boots without the `[asr]` extra installed.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import AsyncGenerator

import numpy as np

from aurascribe.config import (
    CHANNELS,
    CHUNK_DURATION,
    SAMPLE_RATE,
    SILENCE_DURATION,
    VAD_THRESHOLD,
)

_STOP_SENTINEL: object = object()
_RING_MAXLEN = int(30 * SAMPLE_RATE / 512)  # 30s of 512-sample blocks


class AudioCapture:
    def __init__(self) -> None:
        self._vad_model = None
        self._vad_utils = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream = None  # sounddevice.InputStream when started
        self._ring: deque[np.ndarray] = deque(maxlen=_RING_MAXLEN)
        self._ring_mark: int = 0
        # Resampling state — only used when the device's native rate isn't 16kHz.
        self._resampler = None
        self._accum: np.ndarray = np.zeros(0, dtype=np.float32)

    def _audio_callback(self, indata: np.ndarray, frames: int, time, status) -> None:
        chunk = indata[:, 0].astype(np.float32, copy=True)
        if self._resampler is not None:
            chunk = self._resampler.resample_chunk(chunk)
            if chunk.size == 0:
                return

        # Silero VAD requires exactly 512-sample blocks at 16kHz. Accumulate
        # resampled output and emit full blocks downstream.
        if self._accum.size:
            chunk = np.concatenate((self._accum, chunk))
        # Emit as many full 512-sample blocks as we have.
        n_full = (chunk.size // 512) * 512
        if n_full:
            for i in range(0, n_full, 512):
                block = chunk[i:i + 512].copy()
                self._ring.append(block)
                if self._loop and not self._loop.is_closed():
                    self._loop.call_soon_threadsafe(self._queue.put_nowait, block)
        self._accum = chunk[n_full:].copy() if n_full < chunk.size else np.zeros(0, dtype=np.float32)

    def get_recent_audio(self, seconds: float = 4.0) -> np.ndarray | None:
        frames_needed = int(seconds * SAMPLE_RATE / 512)
        snapshot = list(self._ring)[-frames_needed:]
        if not snapshot:
            return None
        return np.concatenate(snapshot)

    def mark_position(self) -> None:
        self._ring_mark = len(self._ring)

    def get_audio_since_mark(self, max_seconds: float = 10.0) -> np.ndarray | None:
        snapshot = list(self._ring)
        new_blocks = snapshot[self._ring_mark:]
        if not new_blocks:
            return None
        max_frames = int(max_seconds * SAMPLE_RATE / 512)
        return np.concatenate(new_blocks[-max_frames:])

    def list_devices(self) -> list[dict]:
        """Return unique input-capable devices.

        Windows exposes each physical device under several host APIs (MME,
        DirectSound, WASAPI, WDM-KS). We dedupe by name and prefer the
        modern APIs so the UI only shows one row per physical mic.
        """
        try:
            import sounddevice as sd
        except Exception:
            return []

        # Lower rank wins when the same device name appears under multiple APIs.
        HOST_RANK = {
            "Windows WASAPI": 0,
            "Windows WDM-KS": 1,
            "Windows DirectSound": 2,
            "MME": 3,
        }
        # MME virtual/pseudo devices — noise in the dropdown, always skip.
        MME_META = {"Microsoft Sound Mapper - Input", "Primary Sound Capture Driver"}

        devices = sd.query_devices()
        hostapis = sd.query_hostapis()

        best: dict[str, dict] = {}
        for i, d in enumerate(devices):
            if d["max_input_channels"] <= 0:
                continue
            name = d["name"]
            if name in MME_META:
                continue
            hostapi_idx = d.get("hostapi", -1)
            hostapi_name = (
                hostapis[hostapi_idx]["name"]
                if 0 <= hostapi_idx < len(hostapis)
                else ""
            )
            rank = HOST_RANK.get(hostapi_name, 99)
            existing = best.get(name)
            if existing is None or rank < existing["_rank"]:
                best[name] = {
                    "index": i,
                    "name": name,
                    "channels": d["max_input_channels"],
                    "host_api": hostapi_name,
                    "_rank": rank,
                }

        result = []
        for entry in sorted(best.values(), key=lambda x: x["name"].lower()):
            entry.pop("_rank")
            result.append(entry)
        return result

    async def start(self, device: int | None = None) -> None:
        import sounddevice as sd
        import soxr
        import torch

        if self._vad_model is None:
            self._vad_model, self._vad_utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                trust_repo=True,
            )
            self._vad_model.eval()

        # Determine the device's native sample rate. WASAPI/WDM-KS won't
        # auto-convert from 16kHz; MME does. We always open at native rate
        # and resample to 16kHz ourselves — same pipeline regardless of API.
        info = sd.query_devices(device, kind="input") if device is not None else sd.query_devices(kind="input")
        native_sr = int(info["default_samplerate"]) or SAMPLE_RATE

        if native_sr == SAMPLE_RATE:
            self._resampler = None
            blocksize = 512
        else:
            self._resampler = soxr.ResampleStream(
                native_sr, SAMPLE_RATE, num_channels=1, dtype="float32", quality="HQ"
            )
            # Size the input block so each callback produces ~512 samples at 16kHz.
            blocksize = int(round(512 * native_sr / SAMPLE_RATE))

        while not self._queue.empty():
            self._queue.get_nowait()
        self._ring.clear()
        self._ring_mark = 0
        self._accum = np.zeros(0, dtype=np.float32)

        self._loop = asyncio.get_running_loop()
        self._stream = sd.InputStream(
            device=device,
            samplerate=native_sr,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
            callback=self._audio_callback,
        )
        self._stream.start()

    async def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._loop and not self._loop.is_closed():
            await self._queue.put(_STOP_SENTINEL)

    def _is_speech(self, audio: np.ndarray) -> bool:
        import torch

        tensor = torch.from_numpy(audio).float()
        with torch.no_grad():
            confidence = self._vad_model(tensor, SAMPLE_RATE).item()
        return confidence >= VAD_THRESHOLD

    async def stream_speech_chunks(self) -> AsyncGenerator[np.ndarray, None]:
        """Yield numpy arrays of speech audio.

        Accumulates audio while speech is detected; yields on silence, max
        duration, or stop sentinel (which also flushes).
        """
        buffer: list[np.ndarray] = []
        silence_frames = 0
        block_duration = 512 / SAMPLE_RATE
        silence_threshold_frames = int(SILENCE_DURATION / block_duration)
        max_frames = int(CHUNK_DURATION / block_duration)

        while True:
            chunk = await self._queue.get()

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
