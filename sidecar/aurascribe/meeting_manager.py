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
from aurascribe.llm.client import chat
from aurascribe.llm.prompts import (
    MEETING_SUMMARY_SYSTEM,
    format_transcript,
    meeting_summary_prompt,
    people_notes_prompt,
)
from aurascribe.llm.realtime import RealtimeIntelligence
from aurascribe.llm.title_refinement import TitleRefinement
from aurascribe.obsidian.writer import (
    forget_meeting_throttle,
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
        self.intel = RealtimeIntelligence()
        # Live meeting-title refinement. Runs on the same debounce cadence
        # as `intel` so LLM activity is batched rather than scattered.
        # Broadcast is set via `title_refiner.set_broadcast(...)` in the
        # same lifespan block so it can fire `title_updated` WS events.
        self.title_refiner = TitleRefinement()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        self._event_loop = asyncio.get_running_loop()
        await self._emit_status("loading", {"message": "Loading transcription models..."})

        async def _on_stage(message: str) -> None:
            await self._emit_status("loading", {"message": message})

        # Engine.load calls back into _on_stage between phases so the UI can
        # show "Downloading Whisper …" / "Loading diarization …" rather than
        # a single opaque "Loading…" for 1–5 minutes on first run.
        try:
            await self.engine.load(on_stage=_on_stage)
        except TypeError:
            # Back-compat for engines that don't accept on_stage.
            await self.engine.load()
        self._ready = True
        await self._emit_status("ready", {"message": "Models loaded. Ready to record."})

    async def start_meeting(
        self,
        title: str = "",
        device: int | None = None,
        loopback_device: int | None = None,
        capture_mic: bool = True,
    ) -> str:
        if self._running:
            raise RuntimeError("Already recording")
        if not self._ready:
            raise RuntimeError("Models still loading — try again in a moment")

        if not title:
            title = f"Transcription {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        meeting_id = str(uuid.uuid4())
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO meetings (id, title, started_at, status) VALUES (?, ?, ?, 'recording')",
                (meeting_id, title, datetime.now().isoformat()),
            )
            await db.commit()

        self._current_meeting_id = meeting_id
        self._provisional_pools[meeting_id] = {}
        self._provisional_next_n[meeting_id] = 1
        # Capture transition is locked so a late /monitor/start can't
        # sneak in between the monitor teardown and our capture.start().
        # We also set `_running` inside the lock, so any monitor request
        # that waits for the lock sees the "recording" state when it
        # finally runs and bails early instead of opening a duplicate
        # stream.
        async with self._monitor_lock:
            await self._stop_monitor_locked()
            self._running = True
            try:
                await self.capture.start(
                    device=device,
                    loopback_device=loopback_device,
                    capture_mic=capture_mic,
                )
            except Exception:
                # Roll back state so the next click doesn't get "Already recording".
                self._running = False
                self._current_meeting_id = None
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
                    await db.commit()
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
        await self.title_refiner.prepare_meeting(meeting_id)
        # Seed the partial anchor to the current capture wall-clock so the
        # first partial only covers audio captured FROM THIS POINT ON —
        # monitor-mode audio that preceded the Start button shouldn't bleed
        # into the partial bubble.
        self._partial_anchor_wall = self.capture.wall_clock_seconds()
        self._task = asyncio.create_task(self._record_loop(meeting_id))
        self._spec_task = asyncio.create_task(self._speculative_loop(meeting_id))
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
        await self.title_refiner.flush_and_clear(meeting_id)
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
        # Track the filename we last wrote to so we can clean it up on rename.
        # The rename endpoint deletes the old file, but a chunk write racing
        # with the rename can recreate it; sweeping on next write keeps the
        # vault tidy.
        prev_title: str | None = None

        all_utterances: list[Utterance] = []

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
                    await self.intel.note_utterances(meeting_id, utterances)
                    # Fire-and-forget title refinement — same debounce
                    # window as intel, but a cheaper LLM call (only
                    # {entity, topic} returned). The module no-ops if
                    # title_locked is 1.
                    await self.title_refiner.note_utterances(meeting_id, utterances)

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
                            # new filename (otherwise every chunk recreates
                            # the old one).
                            async with aiosqlite.connect(DB_PATH) as db:
                                cursor = await db.execute(
                                    "SELECT title FROM meetings WHERE id = ?", (meeting_id,)
                                )
                                title_row = await cursor.fetchone()
                            current_title = title_row[0] if title_row else _initial_title
                            if prev_title and prev_title != current_title:
                                self._cleanup_vault_file(started_at, prev_title)
                            await write_meeting(
                                meeting_id=meeting_id,
                                title=current_title,
                                started_at=started_at,
                                utterances=all_utterances,
                                summary="",
                                action_items=[],
                            )
                            prev_title = current_title
                        except Exception as e:
                            log.warning(f"Live Obsidian write failed: {e}")
            except Exception as e:
                log.error(f"Transcription error: {e}", exc_info=True)
                await self._emit_status("error", {"message": str(e), "meeting_id": meeting_id})

    @staticmethod
    def _cleanup_vault_file(started_at: datetime, title: str) -> None:
        """Delete the vault file named after `(started_at, title)`, if any.

        Called when a mid-recording rename leaves a stale file behind. Safe
        even if the file doesn't exist. Uses the shared path helper so it
        agrees with the writer on where a given meeting lives.
        """
        from aurascribe.obsidian.writer import meeting_file_path

        path = meeting_file_path(started_at, title)
        if path is None or not path.exists():
            return
        try:
            path.unlink()
        except Exception as e:
            log.warning("Could not delete stale vault file %s: %s", path, e)

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
        async with aiosqlite.connect(DB_PATH) as db:
            for u in utterances:
                u.id = str(uuid.uuid4())
                await db.execute(
                    "INSERT INTO utterances (id, meeting_id, speaker, text, start_time, end_time, audio_start, embedding, match_distance, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (u.id, meeting_id, u.speaker, u.text, u.start, u.end, u.audio_start, u.embedding, u.match_distance, datetime.now().isoformat()),
                )
            await db.commit()

    # ── Finalization ──────────────────────────────────────────────────────────

    async def _finalize_meeting(self, meeting_id: str, summarize: bool = False) -> dict:
        utterances = await self._load_utterances(meeting_id)
        if not utterances:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE meetings SET status='done', ended_at=? WHERE id=?",
                    (datetime.now().isoformat(), meeting_id),
                )
                await db.commit()
            return {"meeting_id": meeting_id, "error": "No speech detected"}

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT title, started_at FROM meetings WHERE id = ?", (meeting_id,)
            )
            row = await cursor.fetchone()
            assert row is not None
            title, started_at_str = row
        started_at = datetime.fromisoformat(started_at_str)

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

                speakers = list({
                    u.speaker for u in utterances
                    if u.speaker != "Me"
                    and u.speaker != "Unknown"
                    and not _PROVISIONAL_LABEL_RE.match(u.speaker)
                })
                for speaker in speakers:
                    speaker_lines = "\n".join(u.text for u in utterances if u.speaker == speaker)
                    existing = await self._get_existing_person_note(speaker)
                    updated = await chat(people_notes_prompt(speaker, existing, speaker_lines))
                    await update_person_note(speaker, updated, title, started_at)
            except Exception as e:
                log.warning(f"LLM unavailable — transcript saved without summary: {e}")
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

    async def _get_existing_person_note(self, name: str) -> str:
        from aurascribe.config import VAULT_PEOPLE

        if VAULT_PEOPLE is None:
            return ""
        path = VAULT_PEOPLE / f"{name}.md"
        if not path.exists():
            return ""
        import aiofiles

        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            return await f.read()

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
            self._monitor_drain_task = asyncio.create_task(self._monitor_drain_loop())

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
        drain = getattr(self, "_monitor_drain_task", None)
        if drain is not None:
            drain.cancel()
            try:
                await drain
            except (asyncio.CancelledError, Exception):
                pass
            self._monitor_drain_task = None
        await self.capture.stop()

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
