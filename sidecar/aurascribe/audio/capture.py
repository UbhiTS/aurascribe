"""Continuous mic capture with Silero VAD.

Yields audio chunks when speech is detected. Torch is a soft dep — imported
lazily in `start()` so the sidecar boots without the `[asr]` extra installed.

When `loopback_device` is supplied to `start()`, a second WASAPI-loopback
capture is opened on that output device via `soundcard` (the libportaudio
bundled with `sounddevice` is built without loopback support — missing
`PaWasapi_IsLoopback` symbol — so we can't use the same library for both
streams). A background worker pops aligned 10ms frames from both streams,
runs Speex-style AEC (pyaec) with the loopback as reference, sums the
cleaned mic with the loopback, and feeds the 16kHz mono result into the
existing 512-sample block pipeline. That lets us capture remote
participants (Zoom/Teams/Meet) + the local speaker over a
single-mic-on-speakers setup without getting doubled transcripts from
acoustic echo.
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import threading
import time as _time
from collections import deque
from pathlib import Path
from typing import AsyncGenerator

import numpy as np

from aurascribe.config import (
    AEC_TAIL_MS,
    CHANNELS,
    CHUNK_DURATION,
    SAMPLE_RATE,
    SILENCE_DURATION,
    VAD_THRESHOLD,
)

log = logging.getLogger("aurascribe")

_STOP_SENTINEL: object = object()
_RING_MAXLEN = int(30 * SAMPLE_RATE / 512)  # 30s of 512-sample blocks

# AEC frame size (10ms @ 16kHz); tail length is ms × 16 samples/ms.
# Tail covers the full speaker-DAC + air-travel + room-reflection path;
# 100ms wasn't enough for a speakers-in-room setup and users reported a
# "large hall" reverb because the linear filter ran out of length before
# room reflections had fully decayed. 200ms converges a bit slower but
# catches multi-bounce reverb tails that a typical desktop has. Tunable
# via the `aec_tail_ms` config key (Settings → Advanced → Echo
# cancellation).
_AEC_FRAME = 160
_AEC_TAIL = max(_AEC_FRAME, int(AEC_TAIL_MS * 16))
# Max allowed drift between mic and loopback FIFOs before we drop samples
# on the leading side. 80ms is well within pyaec's tail window so small
# skew is absorbed by the filter; anything larger means a clock glitch
# and we'd rather resync than feed mis-aligned pairs into AEC.
_MAX_SKEW_SAMPLES = 1280  # 80ms @ 16kHz


class MicUnavailableError(RuntimeError):
    """Microphone couldn't be opened — most commonly because Windows has
    denied mic access to the app, the device is in use by another process,
    or the selected device index has disappeared (Bluetooth headset gone).

    Carries a UI-friendly `kind` so the frontend can show the right
    affordance (e.g. "Open Windows mic settings" for permission denials).
    """

    def __init__(self, message: str, *, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind


class AudioCapture:
    def __init__(self) -> None:
        self._vad_model = None
        self._vad_utils = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream = None  # sounddevice.InputStream when started
        self._ring: deque[np.ndarray] = deque(maxlen=_RING_MAXLEN)
        # Resampling state — only used when the device's native rate isn't 16kHz.
        self._resampler = None
        self._accum: np.ndarray = np.zeros(0, dtype=np.float32)
        # Wall-clock Opus recorder. Tees every resampled 16kHz block to a
        # per-meeting .opus file so the UI can play back any transcript line.
        # Lock protects against close() racing with the audio-thread write.
        self._record_writer = None  # soundfile.SoundFile when recording
        self._record_samples: int = 0
        self._record_lock = threading.Lock()
        # Loopback / AEC state — all None when loopback is disabled (the
        # mic callback then emits blocks directly). When loopback is active,
        # `_lb_active=True`; the mic callback pushes into `_mic_fifo` and a
        # dedicated soundcard-loopback thread pushes into `_lb_fifo`. A
        # separate mixer thread drains both, aligns them, runs AEC, and
        # emits to the 512-block pipeline.
        self._lb_active = False
        self._lb_thread: threading.Thread | None = None
        self._lb_thread_stop = threading.Event()
        self._mic_fifo: deque[np.ndarray] = deque()
        self._lb_fifo: deque[np.ndarray] = deque()
        self._fifo_lock = threading.Lock()
        self._aec = None                  # pyaec.Aec instance
        self._aec_cancel = None           # direct lib.AecCancelEcho bound for ctypes fast-path
        self._aec_handle = None           # raw AecHandle pointer
        self._worker: threading.Thread | None = None
        self._worker_stop = threading.Event()
        # Sync callback fired once per 512-sample block with (rms, peak) of
        # the mixed output, both in [0, 1]. meeting_manager installs a
        # thread-safe shim that schedules a WS broadcast on the event loop,
        # giving the UI live visualizers for the signal that's actually
        # being transcribed — not just the raw mic. Stays None in code paths
        # that don't record (e.g. offline tests).
        self._on_level = None  # type: ignore[assignment]

    # ── Block emission ────────────────────────────────────────────────────────
    #
    # Shared path for both mic-only (emitted from the mic callback) and
    # dual-stream (emitted from the mixer thread). Appends resampled
    # 16kHz mono float32 to `self._accum`, carves off 512-sample blocks,
    # and dispatches them to the ring / Opus tee / VAD queue.

    def _emit_blocks(self, chunk: np.ndarray) -> None:
        if self._accum.size:
            chunk = np.concatenate((self._accum, chunk))
        n_full = (chunk.size // 512) * 512
        if n_full:
            last_block: np.ndarray | None = None
            for i in range(0, n_full, 512):
                block = chunk[i:i + 512].copy()
                self._ring.append(block)
                # Tee to Opus recorder BEFORE handing the block to the speech
                # queue, so wall_clock_seconds() read by the consumer (after
                # the await returns) already accounts for this block.
                if self._record_writer is not None:
                    with self._record_lock:
                        if self._record_writer is not None:
                            try:
                                self._record_writer.write(block)
                                self._record_samples += block.size
                            except Exception as e:
                                log.warning("opus record write failed: %s", e)
                if self._loop and not self._loop.is_closed():
                    self._loop.call_soon_threadsafe(self._queue.put_nowait, block)
                last_block = block
            # One level event per `_emit_blocks` call (~32ms at 16kHz for a
            # single-block call, longer for worker-thread batches). Using
            # only the last block keeps RAM touches minimal while still
            # giving visualizers a ~30Hz refresh, which is smoother than
            # the eye can distinguish.
            if self._on_level is not None and last_block is not None:
                try:
                    rms = float(np.sqrt(np.mean(last_block * last_block)))
                    peak = float(np.abs(last_block).max())
                    self._on_level(rms, peak)
                except Exception as e:
                    log.warning("level callback failed: %s", e)
        self._accum = chunk[n_full:].copy() if n_full < chunk.size else np.zeros(0, dtype=np.float32)

    def _audio_callback(self, indata: np.ndarray, frames: int, time, status) -> None:
        chunk = indata[:, 0].astype(np.float32, copy=True)
        if self._resampler is not None:
            chunk = self._resampler.resample_chunk(chunk)
            if chunk.size == 0:
                return

        # Dual-stream mode: hand off to the mixer worker via the mic FIFO.
        # The worker is responsible for AEC + mix + block emission.
        if self._lb_active:
            with self._fifo_lock:
                self._mic_fifo.append(chunk)
            return

        # Mic-only fast path — emit 512-sample blocks directly.
        self._emit_blocks(chunk)

    def _loopback_thread_main(self, speaker_id: str, emit_direct: bool) -> None:
        """Runs in a dedicated thread while the meeting is active.

        `soundcard`'s recorder is a synchronous `record(numframes=N)` call
        inside a `with` block — there's no callback model — so we poll
        it on a thread. Ask for 16kHz mono directly and let WASAPI's
        shared-mode resampler/mixer handle the conversion from the
        endpoint's native format; it's more than good enough for
        AEC-reference quality and cuts a soxr stream we'd otherwise have
        to run on this thread.

        Two destinations depending on mode:
          * `emit_direct=False` (mix mode): push into `_lb_fifo` for the
            mixer worker to align with mic + run AEC.
          * `emit_direct=True`  (system-only): no mic, no AEC — push
            straight into `_emit_blocks` and bypass the mixer entirely.

        COM init: WASAPI is a COM API; every thread that touches it must
        have COM initialized. In mix mode sounddevice's own audio thread
        already did this so soundcard piggybacks without issue — but in
        system-only mode this thread is the first to touch COM in the
        process, so we initialize explicitly. `CoInitializeEx` is a no-op
        if COM is already up, so it's safe to always call.
        """
        import soundcard as sc
        com_initialized = False
        try:
            ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
            # COINIT_MULTITHREADED = 0x0 — required for audio callback
            # threads (apartment-threaded STA would deadlock WASAPI).
            hr = ole32.CoInitializeEx(None, 0x0)
            # S_OK (0) or S_FALSE (1) both mean COM is usable from here.
            # RPC_E_CHANGED_MODE (0x80010106) means someone already inited
            # with a different apartment — benign, soundcard will still work.
            com_initialized = hr in (0, 1)
        except Exception as e:
            log.warning("CoInitializeEx failed: %s", e)

        try:
            # `get_microphone(..., include_loopback=True)` lets us treat
            # the speaker as a capture device.
            mic = sc.get_microphone(id=speaker_id, include_loopback=True)
        except Exception as e:
            log.warning("soundcard: could not open loopback %s: %s", speaker_id, e)
            if com_initialized:
                try:
                    ctypes.windll.ole32.CoUninitialize()  # type: ignore[attr-defined]
                except Exception:
                    pass
            return

        try:
            with mic.recorder(
                samplerate=SAMPLE_RATE,
                channels=1,
                blocksize=_AEC_FRAME,
            ) as rec:
                while not self._lb_thread_stop.is_set():
                    chunk = rec.record(numframes=_AEC_FRAME)
                    # `record()` returns float32 [-1, 1] shaped (N, channels).
                    if chunk.ndim == 2:
                        chunk = chunk[:, 0] if chunk.shape[1] == 1 else chunk.mean(axis=1)
                    chunk = chunk.astype(np.float32, copy=False)
                    if emit_direct:
                        self._emit_blocks(chunk)
                    else:
                        with self._fifo_lock:
                            self._lb_fifo.append(chunk)
        except Exception as e:
            log.warning("soundcard loopback recorder exited: %s", e)
        finally:
            if com_initialized:
                try:
                    ctypes.windll.ole32.CoUninitialize()  # type: ignore[attr-defined]
                except Exception:
                    pass

    def _run_aec_frame(self, mic_i16: np.ndarray, ref_i16: np.ndarray) -> np.ndarray:
        """Direct ctypes call into pyaec's bundled aec.dll — skips the list-
        packing overhead of the pure-Python wrapper (which is a per-sample
        hot path at 100 Hz / 10ms frames)."""
        out = np.zeros(mic_i16.size, dtype=np.int16)
        c_int16_p = ctypes.POINTER(ctypes.c_int16)
        self._aec_cancel(
            self._aec_handle,
            mic_i16.ctypes.data_as(c_int16_p),
            ref_i16.ctypes.data_as(c_int16_p),
            out.ctypes.data_as(c_int16_p),
            mic_i16.size,
        )
        return out

    def _mix_worker(self) -> None:
        """Drains the mic + loopback FIFOs, aligns them by sample count,
        runs AEC per-10ms-frame, mixes cleaned-mic + loopback, and pushes
        the result into the shared 512-block emitter. Exits when
        `_worker_stop` is set and both FIFOs are drained (or on shutdown
        if we need to bail early)."""
        mic_buf = np.zeros(0, dtype=np.float32)
        lb_buf = np.zeros(0, dtype=np.float32)

        while not self._worker_stop.is_set():
            with self._fifo_lock:
                while self._mic_fifo:
                    mic_buf = np.concatenate((mic_buf, self._mic_fifo.popleft()))
                while self._lb_fifo:
                    lb_buf = np.concatenate((lb_buf, self._lb_fifo.popleft()))

            # Clock drift / first-frame arrival order can leave one side
            # persistently ahead. Drop oldest samples on the leading side
            # when skew exceeds the AEC tail's tolerance; preserves
            # alignment at the cost of a tiny audible glitch.
            if mic_buf.size > lb_buf.size + _MAX_SKEW_SAMPLES:
                mic_buf = mic_buf[mic_buf.size - lb_buf.size - _MAX_SKEW_SAMPLES:]
            if lb_buf.size > mic_buf.size + _MAX_SKEW_SAMPLES:
                lb_buf = lb_buf[lb_buf.size - mic_buf.size - _MAX_SKEW_SAMPLES:]

            n_frames = min(mic_buf.size, lb_buf.size) // _AEC_FRAME
            if n_frames == 0:
                # Nothing aligned yet — don't spin. 5ms roughly matches
                # half an AEC frame so we wake up near the next arrival.
                _time.sleep(0.005)
                continue

            mixed_out = np.empty(n_frames * _AEC_FRAME, dtype=np.float32)
            for i in range(n_frames):
                mic_f = mic_buf[i * _AEC_FRAME:(i + 1) * _AEC_FRAME]
                lb_f = lb_buf[i * _AEC_FRAME:(i + 1) * _AEC_FRAME]
                mic_i16 = np.clip(mic_f * 32767.0, -32768, 32767).astype(np.int16)
                lb_i16 = np.clip(lb_f * 32767.0, -32768, 32767).astype(np.int16)
                cleaned_i16 = self._run_aec_frame(mic_i16, lb_i16)
                cleaned_f = cleaned_i16.astype(np.float32) * (1.0 / 32768.0)
                # No pre-attenuation: the user reported that dropping the
                # mic by 3 dB (plus AEC preprocess) made Mix mode sound
                # noticeably quieter than Mic only. Summing full-level
                # streams can hard-clip when both peak simultaneously,
                # but that's rare in conversation and Opus + Whisper
                # tolerate occasional clipping fine. We still clip to
                # [-1, 1] so Opus gets a bounded signal.
                mix = cleaned_f + lb_f
                np.clip(mix, -1.0, 1.0, out=mix)
                mixed_out[i * _AEC_FRAME:(i + 1) * _AEC_FRAME] = mix

            consumed = n_frames * _AEC_FRAME
            mic_buf = mic_buf[consumed:]
            lb_buf = lb_buf[consumed:]
            self._emit_blocks(mixed_out)

    def get_recent_audio(self, seconds: float = 4.0) -> np.ndarray | None:
        frames_needed = int(seconds * SAMPLE_RATE / 512)
        snapshot = list(self._ring)[-frames_needed:]
        if not snapshot:
            return None
        return np.concatenate(snapshot)

    def list_devices(self) -> list[dict]:
        """Return unique input-capable devices.

        Windows exposes each physical device under several host APIs (MME,
        DirectSound, WASAPI, WDM-KS). We dedupe by name and prefer the
        modern APIs so the UI only shows one row per physical mic.
        """
        return self._list_devices(output=False)

    def list_output_devices(self) -> list[dict]:
        """Return output endpoints usable as a WASAPI-loopback source.

        Enumerated via `soundcard` (not sounddevice) because sounddevice's
        bundled libportaudio DLL is built without loopback support. The
        returned `index` is this method's stable sort position — used as
        the round-trip handle the frontend passes back to `start()` and
        which we map to the speaker's soundcard `id` string via
        `_resolve_loopback_speaker`. Marked as "Default speaker" when it
        matches `sc.default_speaker()` so the UI can surface which
        endpoint Windows actually routes audio to right now.
        """
        try:
            import soundcard as sc
        except Exception:
            return []
        try:
            speakers = sc.all_speakers()
            default_id = sc.default_speaker().id
        except Exception:
            return []

        result: list[dict] = []
        for i, sp in enumerate(sorted(speakers, key=lambda s: s.name.lower())):
            result.append({
                "index": i,
                "name": sp.name + (" (default)" if sp.id == default_id else ""),
                "channels": int(getattr(sp, "channels", 2) or 2),
                "host_api": "Windows WASAPI",
            })
        return result

    def _resolve_loopback_speaker(self, loopback_index: int) -> str | None:
        """Reverse of `list_output_devices`: given the UI's index, return
        the soundcard `id` string for that endpoint. Re-enumerates every
        call because device insertion/removal can shuffle the ordering
        between the UI's last fetch and start-time."""
        try:
            import soundcard as sc
        except Exception:
            return None
        try:
            speakers = sc.all_speakers()
        except Exception:
            return None
        ordered = sorted(speakers, key=lambda s: s.name.lower())
        if 0 <= loopback_index < len(ordered):
            return str(ordered[loopback_index].id)
        return None

    def _list_devices(self, output: bool) -> list[dict]:
        # `output=True` is handled by `list_output_devices` via soundcard —
        # this stub stays for back-compat in case anyone calls it directly.
        if output:
            return self.list_output_devices()

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
        META = {"Microsoft Sound Mapper - Input", "Primary Sound Capture Driver"}

        devices = sd.query_devices()
        hostapis = sd.query_hostapis()

        best: dict[str, dict] = {}
        for i, d in enumerate(devices):
            if d["max_input_channels"] <= 0:
                continue
            name = d["name"]
            if name in META:
                continue
            # Disconnected Bluetooth Hands-Free / A2DP endpoints leak through
            # as raw MUI strings like "Headset (@System32\drivers\bthhfenum.sys,#2;…)".
            # Windows only resolves the friendly name when the device is paired
            # and active, so an unresolved "@...\\...sys" signature means the
            # endpoint isn't currently connected — hide it.
            if "@" in name and ".sys" in name:
                continue
            hostapi_idx = d.get("hostapi", -1)
            hostapi_name = (
                hostapis[hostapi_idx]["name"]
                if 0 <= hostapi_idx < len(hostapis)
                else ""
            )
            if hostapi_name not in HOST_RANK:
                continue
            rank = HOST_RANK[hostapi_name]
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

    async def start(
        self,
        device: int | None = None,
        loopback_device: int | None = None,
        capture_mic: bool = True,
    ) -> None:
        """Start recording. Mode is derived from the args:
          * mic-only      → capture_mic=True,  loopback_device=None
          * system-only   → capture_mic=False, loopback_device=<idx>
          * mic + system  → capture_mic=True,  loopback_device=<idx>

        `capture_mic=False` with `loopback_device=None` is caller error —
        nothing to record. Raises immediately.
        """
        import sounddevice as sd
        import soxr
        import torch

        if not capture_mic and loopback_device is None:
            raise RuntimeError(
                "At least one audio source is required — pick a microphone, "
                "a system-audio output, or both."
            )

        if self._vad_model is None:
            self._vad_model, self._vad_utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                trust_repo=True,
            )
            self._vad_model.eval()

        while not self._queue.empty():
            self._queue.get_nowait()
        self._ring.clear()
        self._accum = np.zeros(0, dtype=np.float32)
        with self._fifo_lock:
            self._mic_fifo.clear()
            self._lb_fifo.clear()

        self._loop = asyncio.get_running_loop()

        if capture_mic:
            # Determine the device's native sample rate. WASAPI/WDM-KS won't
            # auto-convert from 16kHz; MME does. We always open at native
            # rate and resample to 16kHz ourselves — same pipeline regardless
            # of API.
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

            # Opening + starting the InputStream is where Windows mic
            # permission denial manifests — sounddevice surfaces it as a
            # PortAudioError with an opaque "Unanticipated host error" /
            # error-code message. Translate it into a structured error the
            # API layer can return as 403 + kind.
            try:
                self._stream = sd.InputStream(
                    device=device,
                    samplerate=native_sr,
                    channels=CHANNELS,
                    dtype="float32",
                    blocksize=blocksize,
                    callback=self._audio_callback,
                )
                self._stream.start()
            except Exception as e:
                self._stream = None
                msg = str(e).lower()
                # Windows privacy denial + "device in use" both show up as
                # PortAudio host errors. We can't distinguish them reliably,
                # so we hand the user the most common fix.
                is_probable_permission = (
                    "unanticipated host error" in msg
                    or "invalid device" in msg
                    or "access is denied" in msg
                    or "0x80070005" in msg   # E_ACCESSDENIED
                )
                if is_probable_permission:
                    raise MicUnavailableError(
                        "Microphone could not be opened. Most commonly this is "
                        "Windows blocking mic access — check Settings → Privacy → "
                        "Microphone and ensure AuraScribe is allowed. If another "
                        "app (Teams, Zoom, Discord) is holding the mic, close it "
                        "and try again.",
                        kind="permission",
                    ) from e
                raise MicUnavailableError(
                    f"Microphone could not be opened: {e}",
                    kind="unknown",
                ) from e

        # Optional loopback capture. Failure semantics differ by mode:
        #   * mix mode (mic already opened): log, fall back to mic-only,
        #     the user's meeting still records.
        #   * system-only mode (no mic): failure leaves us with no audio
        #     source at all — rethrow so the caller tears down cleanly.
        if loopback_device is not None:
            try:
                self._start_loopback(loopback_device, mic_active=capture_mic)
            except Exception as e:
                if capture_mic:
                    log.warning(
                        "Loopback capture unavailable on device %s: %s — "
                        "continuing with mic-only.",
                        loopback_device, e,
                    )
                    self._teardown_loopback()
                else:
                    # Make sure the mic stream (if any partial init ran) is
                    # also cleaned up before rethrowing so stop() isn't
                    # required to reach a clean state.
                    self._teardown_loopback()
                    raise RuntimeError(
                        f"System audio capture failed on device {loopback_device}: {e}"
                    ) from e

    def _start_loopback(self, loopback_device: int, mic_active: bool) -> None:
        """Resolve the UI's output-device index to a soundcard speaker id,
        spin up the loopback-capture thread, and — when the mic is also
        active — start the mixer worker for AEC + mix. In system-only mode
        (mic inactive) the loopback thread emits blocks directly and no
        mixer/AEC is initialised.

        Raises on failure — caller decides whether to fall back.
        """
        speaker_id = self._resolve_loopback_speaker(loopback_device)
        if speaker_id is None:
            raise RuntimeError(f"output device index {loopback_device} not found")

        if mic_active:
            # AEC instance — frame=10ms, tail=200ms. Preprocess ON: the
            # Speex preprocessor bundles denoise + AGC + *residual echo
            # suppression*, and that last stage is what kills the
            # non-linear echo leakage the adaptive filter can't fully
            # cancel. Without it users hear a "large hall" reverb in
            # mix mode (two copies of remote audio: clean from loopback
            # and delayed from the mic path). The AGC will moderate mic
            # level a bit — an acceptable tradeoff for clean audio.
            from pyaec import Aec, lib as _aec_lib
            self._aec = Aec(_AEC_FRAME, _AEC_TAIL, SAMPLE_RATE, True)
            self._aec_cancel = _aec_lib.AecCancelEcho
            self._aec_handle = self._aec._aec

        # Flip the flag BEFORE starting the thread so any concurrent mic
        # callbacks route through the FIFO — otherwise the first few ms of
        # mic audio get emitted directly and skip AEC. `_lb_active` only
        # gates the mic callback's dispatch, so it's safe to set True even
        # in system-only mode (there's no mic callback in that case).
        self._lb_active = mic_active
        self._lb_thread_stop.clear()
        self._lb_thread = threading.Thread(
            target=self._loopback_thread_main,
            args=(speaker_id, not mic_active),  # emit_direct=True when mic off
            name="aurascribe-loopback-capture",
            daemon=True,
        )
        self._lb_thread.start()

        if mic_active:
            self._worker_stop.clear()
            self._worker = threading.Thread(
                target=self._mix_worker, name="aurascribe-aec-mixer", daemon=True,
            )
            self._worker.start()

    def _teardown_loopback(self) -> None:
        """Shut down the mixer worker and loopback capture thread.
        Idempotent — safe to call from the failure branch of
        `_start_loopback` or from `stop()` with no loopback active."""
        self._lb_active = False
        self._worker_stop.set()
        self._lb_thread_stop.set()
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None
        if self._lb_thread is not None:
            # Recorder context-manager exit can block on driver teardown;
            # give it a generous deadline but don't hang forever.
            self._lb_thread.join(timeout=3.0)
            self._lb_thread = None
        # Aec is a ctypes handle — dropping the reference calls AecDestroy
        # via __del__. Clear the cached entrypoints so a subsequent run
        # rebuilds them from the new instance.
        self._aec = None
        self._aec_cancel = None
        self._aec_handle = None
        with self._fifo_lock:
            self._mic_fifo.clear()
            self._lb_fifo.clear()

    async def stop(self) -> None:
        self._teardown_loopback()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._resampler = None
        if self._loop and not self._loop.is_closed():
            await self._queue.put(_STOP_SENTINEL)

    # ── Opus wall-clock recorder ──────────────────────────────────────────────

    def start_recording(self, path: Path) -> None:
        """Begin teeing the 16kHz stream to `path` as OGG Opus.

        The audio thread writes every block; `wall_clock_seconds()` reflects
        the total samples committed to the file. Idempotent-safe: if a prior
        recording is still open, it's closed first.
        """
        import soundfile as sf

        if self._record_writer is not None:
            self.stop_recording()
        path.parent.mkdir(parents=True, exist_ok=True)
        # libsndfile's Opus encoder picks a sensible default bitrate for
        # 16kHz mono (~24-32 kbps for speech). No public knob via soundfile
        # today — this is within the range we want anyway.
        writer = sf.SoundFile(
            str(path),
            mode="w",
            samplerate=SAMPLE_RATE,
            channels=1,
            format="OGG",
            subtype="OPUS",
        )
        with self._record_lock:
            self._record_writer = writer
            self._record_samples = 0

    def stop_recording(self) -> None:
        with self._record_lock:
            writer = self._record_writer
            self._record_writer = None
        if writer is not None:
            try:
                writer.close()
            except Exception as e:
                log.warning("opus record close failed: %s", e)

    def wall_clock_seconds(self) -> float:
        """Seconds of audio written to the active Opus file so far. 0.0 when
        not recording."""
        return self._record_samples / SAMPLE_RATE

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
