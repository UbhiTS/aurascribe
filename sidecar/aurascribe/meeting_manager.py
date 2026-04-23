"""Meeting lifecycle orchestrator.

Pipeline: audio capture → transcription → (diarization) → LLM summary → Obsidian write.

Phase 2 wires the shape with a `StubEngine` (returns no utterances); Phase 3
swaps in `WhisperEngine`. Phase 4 adds real diarization.
"""
from __future__ import annotations

import asyncio
import json
import logging
import pickle
import re
import uuid
from datetime import datetime
from typing import Awaitable, Callable

import aiosqlite
import numpy as np

from aurascribe.audio.capture import AudioCapture
from aurascribe.config import (
    AUDIO_DIR,
    DB_PATH,
    OBSIDIAN_WRITE_CHUNKS,
    OBSIDIAN_WRITE_INTERVAL_SEC,
    PROVISIONAL_THRESHOLD,
    SAMPLE_RATE,
    SPECULATIVE_INTERVAL_SEC,
    SPECULATIVE_WINDOW_SEC,
)
from aurascribe.llm.client import LLMTruncatedError, LLMUnavailableError, chat
from aurascribe.llm.prompts import (
    MEETING_SUMMARY_SYSTEM,
    format_transcript,
    meeting_summary_prompt,
    people_notes_prompt,
)
from aurascribe.llm.realtime import RealtimeIntelligence
from aurascribe.tasks import safe_task
from aurascribe.obsidian.writer import (
    forget_meeting_throttle,
    get_person_note_body,
    note_chunk_arrived,
    time_since_write,
    update_person_note,
    write_meeting,
)
from aurascribe.transcription import TranscriptionEngine, Utterance, default_engine

log = logging.getLogger("aurascribe")

# Cosine-distance threshold for clustering "Unknown" embeddings into the same
# provisional speaker. Matching is centroid-based: we compare each new chunk
# against the running mean of each existing cluster, not the min-over-pool used
# for enrolled speakers — centroids stay stable as the meeting grows instead of
# drifting more permissive with every added embedding. Edit here if speakers
# merge too eagerly (lower) or split across many labels (raise).
_PROVISIONAL_THRESH = PROVISIONAL_THRESHOLD
# FIFO cap on per-label embedding lists. Each embedding is ~3KB (768
# float32s); without a cap, a 3-hour 20-speaker meeting could pin ~200MB
# of pyannote vectors. The centroid stays accurate long before we hit
# this cap — 100 embeddings is already a very stable mean — so eviction
# is essentially lossless for clustering quality.
_PROVISIONAL_MAX_EMBEDDINGS = 100
_PROVISIONAL_LABEL_RE = re.compile(r"^Speaker \d+$")

# Vault-write throttle for the live recording loop. We want the file to look
# alive, but writing on every ~10s chunk thrashes Obsidian sync watchers.
# Write only when EITHER the time gate OR the chunk gate trips — whichever
# fires first. The intel loop's writes naturally reset both via the writer
# module's shared throttle state. Both gates are user-tunable via
# Settings → Advanced → Obsidian Write Cadence.
_VAULT_WRITE_INTERVAL_SEC = OBSIDIAN_WRITE_INTERVAL_SEC
_VAULT_WRITE_CHUNKS = OBSIDIAN_WRITE_CHUNKS

UtteranceCallback = Callable[[str, list[Utterance]], Awaitable[None]]
PartialCallback = Callable[[str, str, str], Awaitable[None]]
StatusCallback = Callable[[str, dict], Awaitable[None]]
# Fires at ~30Hz while recording with (rms, peak) both in [0, 1],
# computed off the same 16kHz mono blocks that feed Whisper. Receivers
# (WS broadcaster today) are expected to be cheap; slow callbacks will
# back up the audio thread's event-loop scheduling.
LevelCallback = Callable[[float, float], Awaitable[None]]


def extract_action_items(summary_md: str) -> list[str]:
    """Parse the `## Action Items` section of an LLM-generated summary.

    Pure text extraction — no state, no side effects. Lifted out of
    MeetingManager so the API layer (which also needs to re-extract after
    /summarize-style endpoints) can call it without reaching into a
    private method on the manager instance.
    """
    items: list[str] = []
    in_section = False
    for line in summary_md.splitlines():
        if "## Action Items" in line:
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break
            stripped = line.strip()
            if stripped.startswith("- "):
                items.append(stripped[2:])
    return items


class MeetingManager:
    def __init__(self, engine: TranscriptionEngine | None = None) -> None:
        self.capture = AudioCapture()
        self.engine: TranscriptionEngine = engine or default_engine()
        self._current_meeting_id: str | None = None
        self._utterance_callbacks: list[UtteranceCallback] = []
        self._partial_callbacks: list[PartialCallback] = []
        self._status_callbacks: list[StatusCallback] = []
        self._level_callbacks: list[LevelCallback] = []
        self._running = False
        self._ready = False
        # Populated by initialize() if engine.load() fails (Whisper
        # download interrupted, HF token rejected, OOM, etc). Surfaced
        # via /api/status so the UI can show the user a useful message
        # with a Retry button instead of spinning on "Loading…" forever.
        self._load_error: str | None = None
        # Friendly name of the mic currently feeding AudioCapture. None when
        # idle. Surfaced via /api/status so the UI can show which device is
        # actually recording, not just what was picked in the dropdown.
        self._active_device_name: str | None = None
        self._task: asyncio.Task | None = None
        self._spec_task: asyncio.Task | None = None
        self._transcribe_sem = asyncio.Semaphore(1)
        # Capture wall-clock (seconds since recording started) of the end
        # of the last committed VAD chunk. Drives the speculative-partial
        # window: each partial pass transcribes audio SINCE this anchor
        # (capped at SPECULATIVE_WINDOW_SEC) so the partial bubble shows
        # everything the user's said since the last real utterance landed,
        # instead of a sliding fixed-width window that appears to forget
        # earlier words.
        self._partial_anchor_wall: float = 0.0
        # Captured at `initialize()` time (always called from the sidecar
        # event loop during FastAPI lifespan). Reused by the audio-thread
        # level shim to schedule async broadcasts without importing
        # capture internals.
        self._event_loop: asyncio.AbstractEventLoop | None = None
        # Monitor mode: same capture pipeline as a meeting, but no record
        # loop / no DB / no ASR. Used to drive the pre-recording VU meter
        # + waveform in the UI so the user sees the exact signal that would
        # be captured if they hit Start. Auto-torn-down when a real
        # meeting starts (capture is a singleton and can't be open twice).
        #
        # `_monitor_lock` serializes start/stop/start sequences — React
        # StrictMode's mount-cleanup-remount pattern (+ any rapid picker
        # change) can fire two /monitor/start calls back-to-back before
        # the first one has even created its stream. Without the lock,
        # both would call capture.start() concurrently and end up with
        # two mic InputStreams hammering the same soxr resampler, which
        # corrupts its internal state and crashes with MemoryError.
        self._monitoring = False
        self._monitor_lock = asyncio.Lock()
        # Per-meeting provisional speaker state. Unknown utterances are
        # clustered in-memory: embeddings that look like an existing provisional
        # speaker reuse that label; novel ones get the next "Speaker N".
        self._provisional_pools: dict[str, dict[str, list[np.ndarray]]] = {}
        self._provisional_next_n: dict[str, int] = {}
        # Real-time intelligence loop. The api layer wires its broadcast
        # callback in via `intel.set_broadcast(...)` during lifespan setup.
        # Live title refinement (entity + topic) is now part of the same
        # call — RealtimeIntelligence emits `title_updated` WS events when
        # title_locked == 0, no separate refiner instance needed.
        self.intel = RealtimeIntelligence()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        self._event_loop = asyncio.get_running_loop()
        self._load_error = None
        await self._emit_status("loading", {"message": "Loading transcription models..."})

        async def _on_stage(message: str) -> None:
            await self._emit_status("loading", {"message": message})

        # Engine.load calls back into _on_stage between phases so the UI can
        # show "Downloading Whisper …" / "Loading diarization …" rather than
        # a single opaque "Loading…" for 1–5 minutes on first run.
        try:
            try:
                await self.engine.load(on_stage=_on_stage)
            except TypeError:
                # Back-compat for engines that don't accept on_stage.
                await self.engine.load()
        except Exception as e:
            # Engine failed to load — record it so /api/status surfaces the
            # error, and emit an "error" event so the UI can show a Retry
            # button instead of spinning on "Loading…" forever. Common causes:
            # Whisper download interrupted, pyannote 401 (HF token rejected
            # or licence not accepted), GPU OOM, missing CUDA DLL.
            log.exception("Engine load failed")
            self._load_error = f"{type(e).__name__}: {e}"
            await self._emit_status(
                "error",
                {"message": f"Could not load transcription models: {self._load_error}"},
            )
            return
        self._ready = True
        await self._emit_status("ready", {"message": "Models loaded. Ready to record."})

    @property
    def load_error(self) -> str | None:
        """Last engine-load failure message, or None if the engine loaded
        cleanly. Cleared on the next `initialize()` call."""
        return self._load_error

    async def start_meeting(
        self,
        title: str = "",
        device: int | None = None,
        loopback_device: int | None = None,
        capture_mic: bool = True,
    ) -> str:
        # Readiness check is cheap and doesn't need a lock. The `_running`
        # check needs one — see below — because capture.start() can take
        # 1–2s on WASAPI and two concurrent start requests (double-click,
        # network retry, two UI tabs) would both pass a lockless pre-check
        # and then race on capture initialisation.
        if not self._ready:
            raise RuntimeError("Models still loading — try again in a moment")

        if not title:
            # Placeholder — replaced by live-intelligence refinement once
            # there's a transcript. The filename gets its own timestamp
            # via meeting_file_path, so no date needed here.
            title = "Untitled recording"

        # Capture transition + recording-state transition are serialized
        # by `_monitor_lock`:
        #   - the `_running` check AND set happen inside the lock, so a
        #     second concurrent caller sees `True` and bails with a clean
        #     RuntimeError instead of racing on capture.start();
        #   - any late /monitor/start can't sneak in between the monitor
        #     teardown and our capture.start();
        #   - on capture failure we roll back every piece of state AND
        #     delete the DB row before releasing the lock, so the next
        #     caller sees `_running = False` and a clean DB.
        meeting_id = str(uuid.uuid4())
        async with self._monitor_lock:
            if self._running:
                raise RuntimeError("Already recording")
            await self._stop_monitor_locked()

            # Commit the DB row inside the lock — rollback is easier when
            # the row's lifetime is entirely owned by this critical section.
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT INTO meetings (id, title, started_at, status) VALUES (?, ?, ?, 'recording')",
                    (meeting_id, title, datetime.now().isoformat()),
                )
                await db.commit()

            self._running = True
            self._current_meeting_id = meeting_id
            self._provisional_pools[meeting_id] = {}
            self._provisional_next_n[meeting_id] = 1
            try:
                await self.capture.start(
                    device=device,
                    loopback_device=loopback_device,
                    capture_mic=capture_mic,
                )
            except Exception:
                # Full rollback: state, in-memory pools, DB row. Without
                # this a transient capture failure (mic unplugged mid-start,
                # PortAudio hiccup) would leave the row stuck in 'recording'
                # forever and the next click would get "Already recording".
                self._running = False
                self._current_meeting_id = None
                self._provisional_pools.pop(meeting_id, None)
                self._provisional_next_n.pop(meeting_id, None)
                try:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
                        await db.commit()
                except Exception as cleanup_err:
                    log.warning(
                        "Could not clean up failed-start meeting row %s: %s",
                        meeting_id, cleanup_err,
                    )
                raise
        # Only surface a mic name when we actually opened the mic. In
        # system-only mode `_resolve_device_name` would still pick up a
        # default-mic label, which would mislead the header chip.
        self._active_device_name = self._resolve_device_name(device) if capture_mic else None

        audio_path = AUDIO_DIR / f"{meeting_id}.opus"
        try:
            self.capture.start_recording(audio_path)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE meetings SET audio_path = ? WHERE id = ?",
                    (str(audio_path), meeting_id),
                )
                await db.commit()
        except Exception as e:
            # Audio recording is a nice-to-have — a failure here shouldn't
            # kill the meeting itself. Log and carry on with transcript-only.
            log.warning("Opus recording unavailable for %s: %s", meeting_id, e)

        await self.intel.prepare_meeting(meeting_id)
        # Seed the partial anchor to the current capture wall-clock so the
        # first partial only covers audio captured FROM THIS POINT ON —
        # monitor-mode audio that preceded the Start button shouldn't bleed
        # into the partial bubble.
        self._partial_anchor_wall = self.capture.wall_clock_seconds()
        self._task = safe_task(self._record_loop(meeting_id), name=f"record_loop[{meeting_id}]")
        self._spec_task = safe_task(self._speculative_loop(meeting_id), name=f"spec_loop[{meeting_id}]")
        await self._emit_status("recording", {"meeting_id": meeting_id, "title": title})
        return meeting_id

    async def stop_meeting(self, summarize: bool = False) -> dict:
        if not self._running or self._current_meeting_id is None:
            raise RuntimeError("No active recording")

        # Capture transition is locked so an incoming /monitor/start
        # can't race with our capture.stop() and land on a half-open
        # InputStream. Once `_running` flips False + capture is stopped,
        # the next monitor/start can proceed cleanly.
        async with self._monitor_lock:
            self._running = False
            self._active_device_name = None
            await self.capture.stop()
            self.capture.stop_recording()
        if self._spec_task:
            self._spec_task.cancel()
            try:
                await self._spec_task
            except asyncio.CancelledError:
                pass
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        meeting_id = self._current_meeting_id
        self._current_meeting_id = None
        self._provisional_pools.pop(meeting_id, None)
        self._provisional_next_n.pop(meeting_id, None)
        await self.intel.flush_and_clear(meeting_id)
        # Drop throttle counters — finalize will do the last write itself.
        forget_meeting_throttle(meeting_id)

        status_msg = "Generating summary..." if summarize else "Saving transcript..."
        await self._emit_status("processing", {"meeting_id": meeting_id, "message": status_msg})
        result = await self._finalize_meeting(meeting_id, summarize=summarize)
        await self._emit_status("done", {"meeting_id": meeting_id, "vault_path": result.get("vault_path")})
        return result

    # ── Recording loop ────────────────────────────────────────────────────────

    async def _record_loop(self, meeting_id: str) -> None:
        elapsed = 0.0

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT title, started_at FROM meetings WHERE id = ?", (meeting_id,)
            )
            row = await cursor.fetchone()
        assert row is not None
        _initial_title, started_at_str = row
        started_at = datetime.fromisoformat(started_at_str)
        all_utterances: list[Utterance] = []

        # Outer error boundary — catches failures in capture.stream_speech_chunks
        # (mic unplugged, driver crash), the transcription pipeline, or any
        # other unhandled exception. Without this, the record task dies silently
        # and the meeting keeps "recording" from the user's POV but no transcript
        # is being produced. On fatal error we broadcast a status:error event so
        # the UI can show the user, and let the caller stop the meeting cleanly.
        try:
            async for audio_chunk in self.capture.stream_speech_chunks():
                if len(audio_chunk) < int(SAMPLE_RATE * 0.3):
                    continue
                chunk_duration = len(audio_chunk) / SAMPLE_RATE
                # Snapshot the wall-clock position of the chunk's start in the
                # Opus file — read *before* the transcribe await so a few more
                # audio blocks landing meanwhile don't drift our anchor. The
                # recorder advances on the audio thread; `- chunk_duration`
                # walks back to the first sample of this chunk.
                chunk_wall_end = self.capture.wall_clock_seconds()
                chunk_wall_start = max(0.0, chunk_wall_end - chunk_duration)
                # Advance the partial anchor so the speculative loop's next
                # pass looks at audio AFTER this chunk, not audio this chunk
                # already consumed. Set before the await so a slow transcribe
                # doesn't leave the partial window straddling the chunk.
                self._partial_anchor_wall = chunk_wall_end
                log.info(f"Transcribing chunk: {chunk_duration:.1f}s of audio")
                try:
                    async with self._transcribe_sem:
                        utterances = await self.engine.transcribe(audio_chunk)
                    for u in utterances:
                        # Preserve the pre-elapsed within-chunk offset so we can
                        # map into the wall-clock file. Speech-time (`u.start`)
                        # drives display; `u.audio_start` drives playback.
                        if chunk_wall_start > 0.0 or chunk_wall_end > 0.0:
                            u.audio_start = chunk_wall_start + u.start
                        u.start += elapsed
                        u.end += elapsed
                    elapsed += chunk_duration
                    self._relabel_unknowns(meeting_id, utterances)
                    log.info(f"Got {len(utterances)} utterances")
                    if utterances:
                        all_utterances.extend(utterances)
                        await self._save_utterances(meeting_id, utterances)
                        for cb in self._utterance_callbacks:
                            await cb(meeting_id, utterances)
                        # Realtime intelligence runs on a debounced timer — this
                        # call only schedules; it doesn't block the record loop.
                        # The same call also handles live title refinement
                        # (entity + topic in the JSON), gated on title_locked.
                        await self.intel.note_utterances(meeting_id, utterances)

                        # Throttled vault write: skip unless we've hit either
                        # gate. The intel loop's writes also reset these counters
                        # via the writer module's shared state, so a recent
                        # intel-driven write keeps the chunk loop quiet.
                        pending_chunks = note_chunk_arrived(meeting_id)
                        elapsed_since_write = time_since_write(meeting_id)
                        should_write = (
                            pending_chunks >= _VAULT_WRITE_CHUNKS
                            or elapsed_since_write >= _VAULT_WRITE_INTERVAL_SEC
                        )
                        if should_write:
                            try:
                                # Re-fetch the title on every write — user can
                                # rename mid-recording and we must write to the
                                # new filename. write_meeting reads the prior
                                # `vault_path` from the DB and unlinks it if
                                # the path changes (rename, or bucket reclassify),
                                # so no separate cleanup step is needed here.
                                async with aiosqlite.connect(DB_PATH) as db:
                                    cursor = await db.execute(
                                        "SELECT title FROM meetings WHERE id = ?", (meeting_id,)
                                    )
                                    title_row = await cursor.fetchone()
                                current_title = title_row[0] if title_row else _initial_title
                                await write_meeting(
                                    meeting_id=meeting_id,
                                    title=current_title,
                                    started_at=started_at,
                                    utterances=all_utterances,
                                    summary="",
                                    action_items=[],
                                )
                            except Exception as e:
                                log.warning(f"Live Obsidian write failed: {e}")
                except Exception as e:
                    log.error(f"Transcription error: {e}", exc_info=True)
                    await self._emit_status(
                        "error", {"message": str(e), "meeting_id": meeting_id},
                    )
        except asyncio.CancelledError:
            raise  # normal stop_meeting path — let it propagate
        except Exception as e:
            # Something below the per-chunk try/except failed (capture
            # stream error, callback misbehaving, DB dropped). Persist
            # what we have, broadcast so the UI can surface it, and
            # exit — stop_meeting() will finalize from here.
            log.exception("Record loop crashed — recording halted")
            await self._emit_status(
                "error",
                {
                    "message": (
                        f"Recording stopped unexpectedly: {e}. "
                        f"The transcript so far has been saved."
                    ),
                    "meeting_id": meeting_id,
                },
            )

    async def _speculative_loop(self, meeting_id: str) -> None:
        """Periodically re-transcribe everything since the last committed
        chunk so the partial bubble accumulates the user's current sentence
        rather than sliding a fixed-width tail window. Capped at
        SPECULATIVE_WINDOW_SEC to keep per-pass cost bounded (and below the
        capture ring's 30s maxlen). Interval + cap are user-tunable via
        Settings → Advanced → Live partial transcription."""
        interval = max(0.25, float(SPECULATIVE_INTERVAL_SEC))
        max_window = max(1.0, float(SPECULATIVE_WINDOW_SEC))
        await asyncio.sleep(interval)
        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self._running:
                    break
                if self._transcribe_sem.locked():
                    continue
                # Grow the partial window from the last committed chunk
                # boundary. Clamps: at least 1s (can't transcribe shorter
                # reliably); at most SPECULATIVE_WINDOW_SEC (stays below
                # the ring's 30s maxlen and bounds per-pass work).
                now_wall = self.capture.wall_clock_seconds()
                elapsed = max(0.0, now_wall - self._partial_anchor_wall)
                window = min(max_window, max(1.0, elapsed))
                audio = self.capture.get_recent_audio(seconds=window)
                if audio is None or len(audio) < int(SAMPLE_RATE * 1.0):
                    continue
                async with self._transcribe_sem:
                    # Skip diarization for the live partial — it's expensive
                    # and the real chunk that follows will re-do it properly.
                    utterances = await self.engine.transcribe(audio, diarize=False)
                if utterances and self._running:
                    first = utterances[0]
                    speaker = first.speaker
                    # Map "Unknown" to a matching provisional label read-only —
                    # never allocates, so the partial display doesn't burn a
                    # "Speaker N" that the real chunk would later re-allocate.
                    if speaker == "Unknown" and first.embedding is not None:
                        try:
                            emb = pickle.loads(first.embedding)
                            speaker = self._match_provisional(meeting_id, emb)
                        except Exception:
                            pass
                    # Concat all utterances from the window so the partial
                    # shows the entire current sentence, not just the first
                    # Whisper segment. Speaker attribution on the partial
                    # is best-effort — the real chunk will re-diarize.
                    text = " ".join(u.text.strip() for u in utterances if u.text.strip())
                    if text:
                        await self._emit_partial(meeting_id, speaker, text)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"Speculative transcription error: {e}", exc_info=True)

    # ── Provisional speaker clustering ────────────────────────────────────────
    #
    # Every chunk whose embedding doesn't match an enrolled speaker comes back
    # as "Unknown". We cluster those unknowns in-memory per meeting so the user
    # sees "Speaker 1", "Speaker 2"... instead of every line collapsing to one
    # "Unknown" bucket. Renaming a provisional label (via the rename-speaker
    # endpoint) both relabels the transcript and folds the pooled embeddings
    # into a real enrolled person — effectively "tag-as-you-go" enrollment.

    def _relabel_unknowns(self, meeting_id: str, utterances: list[Utterance]) -> None:
        """Mutate utterances: swap speaker='Unknown' for a provisional 'Speaker N'."""
        pool = self._provisional_pools.setdefault(meeting_id, {})
        for u in utterances:
            if u.speaker != "Unknown" or u.embedding is None:
                continue
            try:
                emb = pickle.loads(u.embedding)
            except Exception as e:
                log.warning("Could not unpickle embedding for provisional match: %s", e)
                continue
            speaker, distance = self._match_or_allocate_provisional(meeting_id, emb, add=True)
            u.speaker = speaker
            u.match_distance = distance

    def _match_provisional(self, meeting_id: str, emb: np.ndarray) -> str:
        """Best-matching provisional label for `emb`, or 'Unknown' if none fit.

        Read-only — does not grow any pool. Used for live-partial display so a
        mid-chunk partial doesn't burn a "Speaker N" the real chunk will re-use.
        """
        speaker, _distance = self._match_or_allocate_provisional(meeting_id, emb, add=False)
        return speaker

    def _match_or_allocate_provisional(
        self, meeting_id: str, emb: np.ndarray, *, add: bool
    ) -> tuple[str, float | None]:
        from scipy.spatial.distance import cosine

        pool = self._provisional_pools.setdefault(meeting_id, {})
        distances: list[tuple[str, float]] = []
        for label, embs in pool.items():
            if not embs:
                continue
            centroid = np.mean(np.stack(embs), axis=0)
            d = float(cosine(emb, centroid))
            distances.append((label, d))

        # Filter NaN/inf before picking best — Python's sort with NaN keys is
        # undefined and can return a NaN entry as "best", which then fails the
        # threshold check and spawns a spurious new cluster. NaNs come from
        # degenerate centroids on very short turns; once the pool no longer
        # sees new bad embeddings (whisper.py guards them out), these are
        # leftover zombie clusters that should just be ignored for matching.
        valid = [(l, d) for l, d in distances if np.isfinite(d)]
        valid.sort(key=lambda ld: ld[1])
        best_label, best_dist = (valid[0] if valid else (None, float("inf")))

        if best_label is not None and best_dist < _PROVISIONAL_THRESH:
            if add:
                pool[best_label].append(emb)
                # FIFO-evict oldest embeddings once the cap is hit so the
                # pool doesn't grow monotonically during a long meeting.
                # Centroid stability past 100 samples is a rounding error.
                if len(pool[best_label]) > _PROVISIONAL_MAX_EMBEDDINGS:
                    pool[best_label] = pool[best_label][-_PROVISIONAL_MAX_EMBEDDINGS:]
                log.info(
                    "provisional: matched %s dist=%.3f (all=%s) thresh=%.2f",
                    best_label, best_dist,
                    [(l, round(d, 3)) for l, d in distances], _PROVISIONAL_THRESH,
                )
            return best_label, best_dist

        if not add:
            # Read-only miss — caller gets "Unknown" to render until the real
            # chunk lands and allocates the number.
            return "Unknown", None

        n = self._provisional_next_n.get(meeting_id, 1)
        label = f"Speaker {n}"
        self._provisional_next_n[meeting_id] = n + 1
        pool[label] = [emb]
        log.info(
            "provisional: allocated %s (all=%s) thresh=%.2f",
            label,
            [(l, round(d, 3)) for l, d in distances], _PROVISIONAL_THRESH,
        )
        # Fresh cluster — this utterance is definitionally the centroid, so
        # treat it as high-confidence (distance=0.0) for downstream grouping.
        return label, 0.0

    def release_provisional_label(self, meeting_id: str, label: str) -> None:
        """Drop a provisional label from the in-memory pool — called after it
        has been renamed to a real enrolled speaker. Future chunks will match
        via the engine's enrolled pool instead."""
        pool = self._provisional_pools.get(meeting_id)
        if pool:
            pool.pop(label, None)

    async def _save_utterances(self, meeting_id: str, utterances: list[Utterance]) -> None:
        # Generate a uuid per utterance so the WS broadcast + the frontend's
        # assign flow can reference rows by stable id before the commit.
        if not utterances:
            return
        # Batched executemany — one round-trip covers every utterance in
        # the chunk instead of N sequential INSERTs. Saves ~5-10ms per
        # utterance at the DB layer, which adds up over a long meeting.
        created_at = datetime.now().isoformat()
        rows: list[tuple] = []
        for u in utterances:
            u.id = str(uuid.uuid4())
            rows.append(
                (u.id, meeting_id, u.speaker, u.text, u.start, u.end,
                 u.audio_start, u.embedding, u.match_distance, created_at),
            )
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executemany(
                "INSERT INTO utterances (id, meeting_id, speaker, text, start_time, "
                "end_time, audio_start, embedding, match_distance, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            await db.commit()

    # ── Finalization ──────────────────────────────────────────────────────────

    async def _finalize_meeting(self, meeting_id: str, summarize: bool = False) -> dict:
        utterances = await self._load_utterances(meeting_id)
        if not utterances:
            # No transcript pills = nothing worth keeping. Drop the meeting
            # row entirely (instead of leaving a `status=done` orphan the
            # user has to tidy up by hand) and unlink the audio file the
            # capture pipeline recorded in case silence was captured.
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM utterances WHERE meeting_id = ?", (meeting_id,))
                await db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
                await db.commit()
            audio_path = AUDIO_DIR / f"{meeting_id}.opus"
            if audio_path.exists():
                try:
                    audio_path.unlink()
                except Exception as e:
                    log.warning("could not delete empty-meeting audio %s: %s", audio_path, e)
            return {"meeting_id": meeting_id, "error": "No speech detected", "dropped": True}

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT title, started_at FROM meetings WHERE id = ?",
                (meeting_id,),
            )
            row = await cursor.fetchone()
            assert row is not None
            title = row["title"]
            started_at = datetime.fromisoformat(row["started_at"])

        summary_md = ""
        action_items: list[str] = []

        vault_path = await write_meeting(
            meeting_id=meeting_id,
            title=title,
            started_at=started_at,
            utterances=utterances,
            summary=summary_md,
            action_items=action_items,
        )

        transcript = format_transcript(utterances)
        if summarize:
            try:
                summary_md = await chat(
                    meeting_summary_prompt(transcript, title),
                    system=MEETING_SUMMARY_SYSTEM,
                )
                action_items = extract_action_items(summary_md)

                await write_meeting(
                    meeting_id=meeting_id,
                    title=title,
                    started_at=started_at,
                    utterances=utterances,
                    summary=summary_md,
                    action_items=action_items,
                )

                # Auto-write a People note for every real speaker the
                # meeting has a voice_id for. Unknowns, `Me`, and
                # provisional `Speaker N` placeholders are skipped — they
                # aren't identities we can pin a note to.
                speakers = list({
                    u.speaker for u in utterances
                    if u.speaker != "Me"
                    and u.speaker != "Unknown"
                    and not _PROVISIONAL_LABEL_RE.match(u.speaker)
                })
                if speakers:
                    voice_meta = await self._voice_meta_by_name(speakers)
                    for speaker in speakers:
                        meta = voice_meta.get(speaker)
                        if not meta:
                            # No voices row yet — can't key a People note
                            # to a voice_id, so skip this speaker. The
                            # next finalize after the user tags a pill
                            # (which creates the voice row) will pick
                            # them up.
                            continue
                        speaker_lines = "\n".join(
                            u.text for u in utterances if u.speaker == speaker
                        )
                        existing = await get_person_note_body(meta["id"])
                        updated = await chat(
                            people_notes_prompt(speaker, existing, speaker_lines)
                        )
                        await update_person_note(
                            voice_id=meta["id"],
                            person_name=speaker,
                            updated_notes=updated,
                            meeting_title=title,
                            meeting_started_at=started_at,
                            email=meta.get("email"),
                            org=meta.get("org"),
                            role=meta.get("role"),
                        )
            except LLMTruncatedError as e:
                # Model hit max_tokens before emitting the full summary —
                # actionable for the user (raise llm_context_tokens or use
                # a bigger model). Distinct from "LLM unavailable" so the
                # log points at the right fix.
                log.warning(
                    "Summary truncated at the model's output budget for %s — "
                    "saving transcript without summary. Raise `llm_context_tokens` "
                    "in Settings or switch to a model with a bigger budget. (%s)",
                    meeting_id, e,
                )
            except LLMUnavailableError as e:
                log.warning(
                    "LLM unreachable — transcript saved without summary for %s: %s",
                    meeting_id, e,
                )
            except Exception as e:
                log.exception(
                    "Unexpected error during summary for %s — transcript saved without summary: %s",
                    meeting_id, e,
                )
        else:
            log.info("LLM summarization disabled — saving transcript only")

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE meetings SET status='done', ended_at=?, summary=?, action_items=?, vault_path=? WHERE id=?",
                (
                    datetime.now().isoformat(),
                    summary_md or None,
                    json.dumps(action_items) if action_items else None,
                    str(vault_path) if vault_path else None,
                    meeting_id,
                ),
            )
            await db.commit()

        return {
            "meeting_id": meeting_id,
            "title": title,
            "summary": summary_md or None,
            "action_items": action_items,
            "vault_path": str(vault_path) if vault_path else None,
        }

    async def _load_utterances(self, meeting_id: str) -> list[Utterance]:
        utterances: list[Utterance] = []
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT id, speaker, text, start_time, end_time FROM utterances "
                "WHERE meeting_id = ? ORDER BY start_time",
                (meeting_id,),
            )
            async for row in cursor:
                utterances.append(
                    Utterance(id=row[0], speaker=row[1], text=row[2], start=row[3], end=row[4])
                )
        return utterances

    async def _voice_meta_by_name(self, speakers: list[str]) -> dict[str, dict]:
        """Look up voice_id + descriptive metadata for each speaker name.

        Returns {name → {id, email, org, role}} — names without a
        matching voices row are absent from the result. Used by the
        finalize path to build People notes keyed on voice_id.
        """
        if not speakers:
            return {}
        out: dict[str, dict] = {}
        placeholders = ",".join("?" * len(speakers))
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT id, name, email, org, role FROM voices "
                f"WHERE name IN ({placeholders})",
                speakers,
            )
            async for row in cursor:
                out[row["name"]] = {
                    "id": row["id"],
                    "email": row["email"],
                    "org": row["org"],
                    "role": row["role"],
                }
        return out

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def on_utterance(self, cb: UtteranceCallback) -> None:
        self._utterance_callbacks.append(cb)

    def on_partial(self, cb: PartialCallback) -> None:
        self._partial_callbacks.append(cb)

    def on_status(self, cb: StatusCallback) -> None:
        self._status_callbacks.append(cb)

    def on_level(self, cb: LevelCallback) -> None:
        """Register an async listener for per-block RMS/peak updates.
        Wires the sync `capture._on_level` callback the first time a
        listener is added; subsequent calls just append to the list."""
        self._level_callbacks.append(cb)
        if self.capture._on_level is None:
            self.capture._on_level = self._on_capture_level

    def _on_capture_level(self, rms: float, peak: float) -> None:
        """Thread-safe shim invoked on the audio thread. Schedules the
        async fan-out on the sidecar's event loop so broadcast() + the
        WebSocket send_json calls don't touch audio-thread state."""
        loop = self._event_loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._emit_level(rms, peak), loop)
        except RuntimeError:
            pass  # loop shutting down — drop the tick silently

    async def _emit_level(self, rms: float, peak: float) -> None:
        for cb in self._level_callbacks:
            try:
                await cb(rms, peak)
            except Exception:
                pass

    async def _emit_partial(self, meeting_id: str, speaker: str, text: str) -> None:
        for cb in self._partial_callbacks:
            try:
                await cb(meeting_id, speaker, text)
            except Exception:
                pass

    async def _emit_status(self, event: str, data: dict) -> None:
        for cb in self._status_callbacks:
            try:
                await cb(event, data)
            except Exception:
                pass

    # ── Monitor mode (idle-state visualizer source) ───────────────────────────
    #
    # Monitor opens the same capture pipeline as a real meeting but skips
    # ASR / diarization / DB — the only observable effect is the
    # ~30Hz `audio_level` WS broadcast the visualizers already consume.
    # Lets the UI animate the VU meter + waveform live for whatever
    # source the user has selected, *before* they hit Start Recording.
    #
    # Reentrancy rules:
    #   * If a meeting is recording, monitor requests are rejected (busy).
    #   * If an existing monitor is running, starting a new one with a
    #     different config transparently restarts — callers don't need
    #     to stop+start explicitly on every picker change.
    #   * start_meeting() tears down the monitor first.

    async def start_monitor(
        self,
        device: int | None = None,
        loopback_device: int | None = None,
        capture_mic: bool = True,
    ) -> None:
        # Serialized against stop_monitor and against concurrent calls to
        # itself — see _monitor_lock comment in __init__.
        async with self._monitor_lock:
            if self._running:
                raise RuntimeError("Can't monitor while recording")
            # Unlike start_meeting, monitor doesn't need Whisper / pyannote —
            # only capture + the VAD used to size blocks. VAD is loaded
            # lazily inside capture.start() on first call. So we can boot
            # the visualizer before the heavy models finish loading.
            if self._monitoring:
                await self._stop_monitor_locked()
            await self.capture.start(
                device=device,
                loopback_device=loopback_device,
                capture_mic=capture_mic,
            )
            self._monitoring = True
            # Background task drains the VAD queue so the ring doesn't
            # back up with un-consumed blocks while we're monitoring.
            # Blocks are discarded — monitor doesn't transcribe.
            self._monitor_drain_task = safe_task(
                self._monitor_drain_loop(),
                name="monitor_drain_loop",
            )

    async def stop_monitor(self) -> None:
        async with self._monitor_lock:
            await self._stop_monitor_locked()

    async def _stop_monitor_locked(self) -> None:
        """Teardown half of the monitor lifecycle. Assumes caller holds
        `_monitor_lock`. Never call from outside the lock — capture.stop()
        can race with a concurrent capture.start() and leak an
        InputStream whose callback keeps writing to a new resampler."""
        if not self._monitoring:
            return
        self._monitoring = False
        # Always cancel + null the drain task — even if capture.stop()
        # below raises. Previously a capture-stop exception left the
        # drain task running against a closed queue, pinning a coroutine
        # and leaking memory on repeated monitor toggles.
        drain = getattr(self, "_monitor_drain_task", None)
        self._monitor_drain_task = None
        try:
            if drain is not None:
                drain.cancel()
                try:
                    await drain
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            # capture.stop() must run even if drain cancellation errored.
            try:
                await self.capture.stop()
            except Exception as e:
                log.warning("Monitor capture.stop() failed: %s", e)

    async def _monitor_drain_loop(self) -> None:
        """Pull-and-drop loop that keeps the capture's internal queue from
        backing up during monitor mode. The audio thread pushes 512-block
        ndarrays; we just await + discard."""
        try:
            while self._monitoring:
                chunk = await self.capture._queue.get()
                # Capture's _STOP_SENTINEL is posted from stop(); exit
                # cleanly rather than treating it as audio.
                if not isinstance(chunk, np.ndarray):
                    break
        except asyncio.CancelledError:
            raise

    @property
    def is_monitoring(self) -> bool:
        return self._monitoring

    # ── Helpers ───────────────────────────────────────────────────────────────

    def list_audio_devices(self) -> list[dict]:
        return self.capture.list_devices()

    def list_audio_output_devices(self) -> list[dict]:
        """WASAPI-capable output devices for the loopback picker."""
        return self.capture.list_output_devices()

    def _resolve_device_name(self, device: int | None) -> str | None:
        """Friendly name for the sounddevice index that was just opened.

        device=None means "OS default" — sounddevice doesn't give a stable
        name for that synthetic slot, so we resolve it to the physical input
        it actually bound to. Failures fall back to None rather than killing
        the recording.
        """
        try:
            import sounddevice as sd
            if device is None:
                info = sd.query_devices(kind="input")
            else:
                info = sd.query_devices(device, kind="input")
            name = info.get("name") if isinstance(info, dict) else None
            return name if isinstance(name, str) and name else None
        except Exception:
            return None

    @property
    def active_device_name(self) -> str | None:
        return self._active_device_name

    @property
    def is_recording(self) -> bool:
        return self._running

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def current_meeting_id(self) -> str | None:
        return self._current_meeting_id
