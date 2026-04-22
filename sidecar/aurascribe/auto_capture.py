"""Auto-capture — sustained-speech detection that auto-starts and
auto-stops a meeting without the user clicking anything.

Runs a second, lightweight mic stream whenever recording is idle:

    LISTENING  ──(≥ N sec of sustained speech)──→ ARMED
                                                   ↓
                                              start_meeting()
                                                   ↓
    RECORDING  ──(≥ M sec of sustained silence)──→ back to LISTENING
                                                   ↑
                                              stop_meeting()

Design choices:

  * Two mic streams on the same device are flaky across WASAPI /
    CoreAudio, so during RECORDING our stream is closed and we ride on
    the recording pipeline's `audio_level` callback for silence
    detection. The recording pipeline already streams RMS per 512-sample
    block, so reusing that signal avoids any double-capture headache.

  * Only recordings the monitor auto-started are subject to auto-stop.
    Manual `Start Recording` clicks stay until the user clicks `Stop` —
    otherwise the monitor would "steal" control of a meeting the user
    explicitly began.

  * Silero VAD is shared with the recording pipeline via
    `aurascribe.audio.vad_model` so we load torch + the model weights
    exactly once per sidecar process.

  * Mic-open failures (no device, OS permission denied, device in use)
    are retried on a capped exponential backoff so a grant-permission-
    later flow is picked up automatically within a minute.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Awaitable, Callable

import numpy as np

from aurascribe import config
from aurascribe.audio.vad_model import get_vad_model

log = logging.getLogger("aurascribe")

# 16 kHz / 512 samples = 32 ms per block → ~31 decisions/s.
_SAMPLE_RATE = 16_000
_BLOCK_SAMPLES = 512
_BLOCK_SECS = _BLOCK_SAMPLES / _SAMPLE_RATE  # ~0.032s

# Start detection — "is the recent window mostly speech?"
_START_SPEECH_RATIO = 0.6  # ≥60% of recent frames must be classified as speech

# RMS below this for `stop_silence_seconds` ends a meeting.
_STOP_RMS_THRESHOLD = 0.005

# Cooldown after an auto-stop before a new auto-start can fire — prevents
# a transient silence blip from stopping-then-immediately-restarting the
# same conversation as a second meeting.
_RESTART_COOLDOWN_SEC = 5.0

# Retry schedule for mic-open failures (seconds between attempts).
_RETRY_BACKOFF = [5, 10, 30, 60, 60]

# UI broadcast throttle — don't spam WS with 30 Hz updates.
_UI_BROADCAST_HZ = 5

States = str  # "disabled" | "listening" | "armed" | "recording" | "error"


class AutoCaptureMonitor:
    """Process-wide mic-activity monitor that auto-starts and auto-stops
    recordings. One instance lives on the sidecar's singletons; the
    FastAPI lifespan wires callbacks in during startup.
    """

    def __init__(
        self,
        manager: Any,  # MeetingManager (typed as Any to avoid import cycle)
        broadcast: Callable[[dict], Awaitable[None]],
    ) -> None:
        self._manager = manager
        self._broadcast = broadcast

        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()
        self._state: States = "disabled"
        self._enabled: bool = False

        # Mic stream + resampler. None while not listening.
        self._stream: Any = None
        self._resampler: Any = None
        self._accum: np.ndarray = np.zeros(0, dtype=np.float32)

        # Rolling window of recent VAD decisions for start detection.
        self._recent: deque[bool] = deque()
        self._recent_max: int = 0
        self._recent_threshold: int = 0

        # Silence accounting while RECORDING — counts consecutive silent
        # 512-sample blocks emitted by the recording pipeline.
        self._silent_blocks: int = 0
        self._silence_block_threshold: int = 0
        # True iff the active recording was auto-started by us. Manual
        # Start clicks leave this False, which suppresses auto-stop.
        self._auto_started_current: bool = False

        # Timestamps for cooldowns + UI throttle.
        self._last_auto_stop: float = 0.0
        self._last_ui_broadcast: float = 0.0
        self._ui_confidence: float = 0.0

        # Transitioning flag blocks re-entry during start/stop plumbing.
        self._transitioning: bool = False
        # Retry task for mic-open failures.
        self._retry_task: asyncio.Task | None = None
        self._retry_idx: int = 0
        # Serialize torch inference across callback-triggered tasks.
        self._vad_sem = asyncio.Semaphore(1)

    # ── Public properties ───────────────────────────────────────────────

    @property
    def state(self) -> States:
        return self._state

    @property
    def enabled(self) -> bool:
        return self._enabled

    def snapshot(self) -> dict:
        """Small dict suitable for /api/status + /api/auto-capture GETs."""
        return {
            "enabled": self._enabled,
            "state": self._state,
            "confidence": round(float(self._ui_confidence), 3),
        }

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def enable(self) -> None:
        """Turn the monitor on. Idempotent — calling twice is a no-op."""
        async with self._lock:
            if self._enabled:
                return
            self._enabled = True
            self._loop = asyncio.get_running_loop()
            self._rebuild_windows_locked()
            if self._manager.is_recording:
                # A recording is already running — our listening stream
                # stays closed; we just sit at RECORDING and wait for it.
                self._state = "recording"
                self._auto_started_current = False
                await self._publish_state()
                return
            await self._start_listening_locked()

    async def disable(self) -> None:
        async with self._lock:
            if not self._enabled:
                return
            self._enabled = False
            self._cancel_retry_locked()
            await self._stop_listening_locked()
            self._state = "disabled"
            await self._publish_state()

    async def reload_from_config(self) -> None:
        """Re-apply settings from `aurascribe.config`. Called after a
        successful PUT /api/settings/config so toggling auto-capture or
        tuning thresholds doesn't require a full sidecar restart."""
        desired = bool(config.AUTO_CAPTURE_ENABLED)
        async with self._lock:
            # Window sizes depend on config — always refresh them.
            self._rebuild_windows_locked()
        if desired and not self._enabled:
            await self.enable()
        elif not desired and self._enabled:
            await self.disable()

    def _rebuild_windows_locked(self) -> None:
        """Refresh rolling-window sizes from config. Caller holds lock."""
        start_sec = max(0.5, float(config.AUTO_CAPTURE_START_SPEECH_SEC))
        stop_sec = max(5.0, float(config.AUTO_CAPTURE_STOP_SILENCE_SEC))
        self._recent_max = max(8, int(start_sec / _BLOCK_SECS))
        self._recent_threshold = int(self._recent_max * _START_SPEECH_RATIO)
        self._silence_block_threshold = int(stop_sec / _BLOCK_SECS)
        # Preserve recent content on resize.
        old = list(self._recent)
        self._recent = deque(old[-self._recent_max:], maxlen=self._recent_max)
        self._silent_blocks = 0

    # ── Manager status callback — mic handoff ───────────────────────────

    async def on_manager_status(self, event: str, data: dict) -> None:
        """Register with `manager.on_status(...)` during lifespan.

        When a meeting starts (by any path — our auto-fire or a manual
        click), we close our listening stream so the recording pipeline
        owns the mic. When it ends, we re-open ours if still enabled.
        """
        if event == "recording":
            async with self._lock:
                await self._stop_listening_locked()
                self._state = "recording"
                # If `_fire_start` set the flag, it stays True. A manual
                # click leaves it False — which suppresses auto-stop.
                self._silent_blocks = 0
                await self._publish_state()
        elif event in ("done", "error"):
            async with self._lock:
                if self._auto_started_current:
                    # Reset the flag whether the stop was us or manual.
                    self._auto_started_current = False
                if self._enabled:
                    await self._start_listening_locked()

    # ── Manager level callback — silence detection while recording ──────

    async def on_manager_level(self, rms: float, peak: float) -> None:
        """Register with `manager.on_level(...)`. Fires ~30 Hz during
        recording. We only act when the current meeting was auto-started."""
        if self._state != "recording":
            return
        if not self._auto_started_current:
            return
        if self._transitioning:
            return
        if rms < _STOP_RMS_THRESHOLD:
            self._silent_blocks += 1
        else:
            self._silent_blocks = 0
        if self._silence_block_threshold and self._silent_blocks >= self._silence_block_threshold:
            await self._fire_stop()

    # ── Listening stream open/close ─────────────────────────────────────

    async def _start_listening_locked(self) -> None:
        assert self._enabled
        if self._stream is not None:
            return
        if self._manager.is_recording:
            self._state = "recording"
            await self._publish_state()
            return

        try:
            import sounddevice as sd
            import soxr
        except Exception as e:
            log.warning("auto-capture: sounddevice/soxr unavailable: %s", e)
            await self._enter_error_and_schedule_retry_locked()
            return

        try:
            info = sd.query_devices(kind="input")
            native_sr = int(info.get("default_samplerate") or _SAMPLE_RATE)
            if native_sr == _SAMPLE_RATE:
                self._resampler = None
                blocksize = _BLOCK_SAMPLES
            else:
                self._resampler = soxr.ResampleStream(
                    native_sr, _SAMPLE_RATE, num_channels=1,
                    dtype="float32", quality="HQ",
                )
                blocksize = max(1, int(round(_BLOCK_SAMPLES * native_sr / _SAMPLE_RATE)))
            self._accum = np.zeros(0, dtype=np.float32)
            self._recent.clear()
            self._ui_confidence = 0.0

            stream = sd.InputStream(
                samplerate=native_sr,
                channels=1,
                dtype="float32",
                blocksize=blocksize,
                callback=self._audio_callback,
            )
            stream.start()
            self._stream = stream
        except Exception as e:
            # Most common causes: OS mic permission denied, no default mic,
            # another app holds the device exclusively. All worth retrying.
            log.info("auto-capture: mic open failed, will retry: %s", e)
            self._stream = None
            self._resampler = None
            await self._enter_error_and_schedule_retry_locked()
            return

        self._retry_idx = 0
        self._cancel_retry_locked()
        self._state = "listening"
        await self._publish_state()

    async def _stop_listening_locked(self) -> None:
        if self._stream is None:
            return
        stream = self._stream
        self._stream = None
        self._resampler = None
        try:
            stream.stop()
            stream.close()
        except Exception as e:
            log.warning("auto-capture: stream close failed: %s", e)

    async def _enter_error_and_schedule_retry_locked(self) -> None:
        self._state = "error"
        await self._publish_state()
        self._cancel_retry_locked()
        delay = _RETRY_BACKOFF[min(self._retry_idx, len(_RETRY_BACKOFF) - 1)]
        self._retry_idx += 1
        self._retry_task = asyncio.create_task(self._retry_after(delay))

    def _cancel_retry_locked(self) -> None:
        if self._retry_task is not None:
            self._retry_task.cancel()
            self._retry_task = None

    async def _retry_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if not self._enabled:
                return
            if self._stream is not None:
                return
            if self._manager.is_recording:
                return
            await self._start_listening_locked()

    # ── Audio callback (sd thread) ──────────────────────────────────────

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        # indata is read-only on some hosts; copy before handoff.
        samples = indata[:, 0].astype(np.float32, copy=True)
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._schedule_process, samples)

    def _schedule_process(self, samples: np.ndarray) -> None:
        # Runs on the event loop — fan out VAD evaluation without blocking
        # the audio callback.
        asyncio.ensure_future(self._process_samples(samples))

    async def _process_samples(self, samples: np.ndarray) -> None:
        if self._resampler is not None:
            samples = self._resampler.resample_chunk(samples)
            if samples.size == 0:
                return
        if self._accum.size:
            samples = np.concatenate((self._accum, samples))
        n_full = (samples.size // _BLOCK_SAMPLES) * _BLOCK_SAMPLES
        if n_full:
            for i in range(0, n_full, _BLOCK_SAMPLES):
                await self._evaluate_block(samples[i : i + _BLOCK_SAMPLES])
        self._accum = (
            samples[n_full:].copy()
            if n_full < samples.size
            else np.zeros(0, dtype=np.float32)
        )

    async def _evaluate_block(self, block: np.ndarray) -> None:
        # Gate on state — a stream that's draining after stop_listening
        # can still deliver a couple of late blocks.
        if self._state not in ("listening", "armed"):
            return
        try:
            import torch
            async with self._vad_sem:
                model, _ = get_vad_model()
                with torch.no_grad():
                    tensor = torch.from_numpy(block).float()
                    confidence = float(model(tensor, _SAMPLE_RATE).item())
        except Exception as e:
            log.warning("auto-capture: VAD inference failed: %s", e)
            return

        threshold = float(config.AUTO_CAPTURE_VAD_THRESHOLD)
        is_speech = confidence >= threshold
        self._ui_confidence = 0.75 * self._ui_confidence + 0.25 * confidence
        self._recent.append(is_speech)

        now = time.time()
        if now - self._last_ui_broadcast > (1.0 / _UI_BROADCAST_HZ):
            self._last_ui_broadcast = now
            await self._publish_state()

        await self._maybe_fire_start(now)

    async def _maybe_fire_start(self, now: float) -> None:
        if self._state != "listening":
            return
        if self._transitioning:
            return
        if len(self._recent) < self._recent_max:
            return
        if sum(self._recent) < self._recent_threshold:
            return
        if now - self._last_auto_stop < _RESTART_COOLDOWN_SEC:
            return
        if not self._manager.is_ready:
            return
        if self._manager.is_recording:
            return
        await self._fire_start()

    # ── Triggers ────────────────────────────────────────────────────────

    async def _fire_start(self) -> None:
        self._transitioning = True
        try:
            log.info("auto-capture: sustained speech detected → starting meeting")
            async with self._lock:
                self._state = "armed"
                await self._publish_state()
                await self._stop_listening_locked()
                # Mark BEFORE awaiting start_meeting so the status callback
                # that fires on "recording" sees it as auto-started.
                self._auto_started_current = True
            title = f"Auto-captured {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            try:
                await self._manager.start_meeting(title=title)
            except Exception as e:
                log.warning("auto-capture: start_meeting failed: %s", e)
                async with self._lock:
                    self._auto_started_current = False
                    if self._enabled and not self._manager.is_recording:
                        await self._start_listening_locked()
        finally:
            self._transitioning = False

    async def _fire_stop(self) -> None:
        self._transitioning = True
        try:
            log.info("auto-capture: sustained silence → stopping meeting")
            try:
                await self._manager.stop_meeting(summarize=False)
            except Exception as e:
                log.warning("auto-capture: stop_meeting failed: %s", e)
            self._last_auto_stop = time.time()
        finally:
            self._transitioning = False

    # ── UI broadcast ────────────────────────────────────────────────────

    async def _publish_state(self) -> None:
        payload = {
            "type": "auto_capture",
            "enabled": self._enabled,
            "state": self._state,
            "confidence": round(float(self._ui_confidence), 3),
            "silent_seconds": (
                round(self._silent_blocks * _BLOCK_SECS, 1)
                if self._state == "recording" and self._auto_started_current
                else 0.0
            ),
        }
        try:
            await self._broadcast(payload)
        except Exception:
            # Broadcast failures are non-fatal — WS clients come and go.
            pass
