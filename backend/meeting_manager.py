"""
Orchestrates the full meeting lifecycle:
  audio capture → transcription → diarization → LLM summary → Obsidian write
"""
import asyncio
import json
import aiosqlite
from datetime import datetime
from typing import Callable, Awaitable

from backend.config import DB_PATH, SAMPLE_RATE
from backend.audio.capture import AudioCapture
from backend.transcription.engine import TranscriptionEngine, Utterance
from backend.llm.client import chat
from backend.llm.prompts import (
    meeting_summary_prompt, format_transcript,
    people_notes_prompt, MEETING_SUMMARY_SYSTEM,
)
from backend.obsidian.writer import (
    write_meeting, update_person_note,
)

# Callback type: receives new utterances as they arrive
UtteranceCallback = Callable[[int, list[Utterance]], Awaitable[None]]


class MeetingManager:
    def __init__(self):
        self.capture = AudioCapture()
        self.engine = TranscriptionEngine()
        self._current_meeting_id: int | None = None
        self._utterance_callbacks: list[UtteranceCallback] = []
        self._partial_callbacks: list[Callable[[int, str, str], Awaitable[None]]] = []
        self._status_callbacks: list[Callable[[str, dict], Awaitable[None]]] = []
        self._running = False
        self._task: asyncio.Task | None = None
        self._spec_task: asyncio.Task | None = None
        self._transcribe_sem = asyncio.Semaphore(1)  # one transcription at a time

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self):
        """Load models — call once at app startup."""
        await self._emit_status("loading", {"message": "Loading transcription models..."})
        await self.engine.load()
        await self._emit_status("ready", {"message": "Models loaded. Ready to record."})

    async def start_meeting(self, title: str = "", device: int | None = None) -> int:
        if self._running:
            raise RuntimeError("Already recording")

        if not title:
            title = f"Meeting {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "INSERT INTO meetings (title, started_at, status) VALUES (?, ?, 'recording')",
                (title, datetime.now().isoformat()),
            )
            await db.commit()
            meeting_id = cursor.lastrowid

        self._current_meeting_id = meeting_id
        self._running = True
        await self.capture.start(device=device)
        self._task = asyncio.create_task(self._record_loop(meeting_id))
        self._spec_task = asyncio.create_task(self._speculative_loop(meeting_id))
        await self._emit_status("recording", {"meeting_id": meeting_id, "title": title})
        return meeting_id

    async def stop_meeting(self, summarize: bool = False) -> dict:
        if not self._running or self._current_meeting_id is None:
            raise RuntimeError("No active recording")

        self._running = False
        # stop() sends a sentinel that flushes the buffer and lets the loop exit naturally
        await self.capture.stop()
        if self._spec_task:
            self._spec_task.cancel()
            try:
                await self._spec_task
            except asyncio.CancelledError:
                pass
        if self._task:
            try:
                await self._task  # wait for final chunk to be transcribed
            except asyncio.CancelledError:
                pass

        meeting_id = self._current_meeting_id
        self._current_meeting_id = None

        status_msg = "Generating summary..." if summarize else "Saving transcript..."
        await self._emit_status("processing", {"meeting_id": meeting_id, "message": status_msg})
        result = await self._finalize_meeting(meeting_id, summarize=summarize)
        await self._emit_status("done", {"meeting_id": meeting_id, "vault_path": result.get("vault_path")})
        return result

    # ── Recording loop ─────────────────────────────────────────────────────────

    async def _record_loop(self, meeting_id: int):
        import logging
        from backend.obsidian.writer import write_meeting
        log = logging.getLogger("aurascribe")
        elapsed = 0.0  # cumulative audio time — gives each utterance a unique timestamp

        # Cache meeting metadata so we don't hit the DB on every chunk
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT title, started_at FROM meetings WHERE id = ?", (meeting_id,)
            )
            row = await cursor.fetchone()
        title, started_at_str = row
        started_at = datetime.fromisoformat(started_at_str)

        all_utterances: list = []  # grows as chunks are finalized

        async for audio_chunk in self.capture.stream_speech_chunks():
            # Skip chunks shorter than 0.3 seconds — filters pure-noise blips
            if len(audio_chunk) < int(SAMPLE_RATE * 0.3):
                continue
            chunk_duration = len(audio_chunk) / SAMPLE_RATE
            log.info(f"Transcribing chunk: {chunk_duration:.1f}s of audio")
            try:
                async with self._transcribe_sem:
                    utterances = await self.engine.transcribe(audio_chunk)
                # Offset start/end by accumulated recording time so timestamps are unique
                for u in utterances:
                    u.start += elapsed
                    u.end += elapsed
                elapsed += chunk_duration
                log.info(f"Got {len(utterances)} utterances")
                if utterances:
                    all_utterances.extend(utterances)
                    await self._save_utterances(meeting_id, utterances)
                    for cb in self._utterance_callbacks:
                        await cb(meeting_id, utterances)
                    # Live-save transcript to Obsidian after every finalized chunk
                    try:
                        await write_meeting(
                            meeting_id=meeting_id,
                            title=title,
                            started_at=started_at,
                            utterances=all_utterances,
                            summary="",
                            action_items=[],
                        )
                    except Exception as e:
                        log.warning(f"Live Obsidian write failed: {e}")
            except Exception as e:
                log.error(f"Transcription error: {e}", exc_info=True)
                await self._emit_status("error", {"message": str(e), "meeting_id": meeting_id})

    async def _speculative_loop(self, meeting_id: int):
        """Every 1.5s, transcribe the last 4s of audio to show live partial results."""
        import logging
        log = logging.getLogger("aurascribe")
        await asyncio.sleep(1.5)
        while self._running:
            try:
                await asyncio.sleep(1.5)
                if not self._running:
                    break
                if self._transcribe_sem.locked():
                    continue  # final transcription in progress — skip this round
                audio = self.capture.get_recent_audio(seconds=4.0)
                if audio is None or len(audio) < int(SAMPLE_RATE * 1.0):
                    continue
                async with self._transcribe_sem:
                    utterances = await self.engine.transcribe(audio)
                if utterances and self._running:
                    await self._emit_partial(meeting_id, utterances[0].speaker, utterances[0].text)
            except asyncio.CancelledError:
                raise  # let cancellation propagate so stop_meeting() can clean up
            except Exception as e:
                log.warning(f"Speculative transcription error: {e}", exc_info=True)

    async def _save_utterances(self, meeting_id: int, utterances: list[Utterance]):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executemany(
                "INSERT INTO utterances (meeting_id, speaker, text, start_time, end_time, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (meeting_id, u.speaker, u.text, u.start, u.end, datetime.now().isoformat())
                    for u in utterances
                ],
            )
            await db.commit()

    # ── Finalization ──────────────────────────────────────────────────────────

    async def _finalize_meeting(self, meeting_id: int, summarize: bool = False) -> dict:
        import logging
        log = logging.getLogger("aurascribe")

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
            title, started_at_str = row

        started_at = datetime.fromisoformat(started_at_str)

        # Always write transcript to Obsidian first — this never depends on LLM
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

        # LLM summary — only if explicitly enabled and LM Studio is reachable
        transcript = format_transcript(utterances)
        if not summarize:
            log.info("LLM summarization disabled — saving transcript only")
        try:
            if not summarize:
                raise StopIteration  # skip LLM block cleanly
            summary_md = await chat(
                meeting_summary_prompt(transcript, title),
                system=MEETING_SUMMARY_SYSTEM,
            )
            action_items = self._extract_action_items(summary_md)

            # Update the vault file with the summary now that we have it
            await write_meeting(
                meeting_id=meeting_id,
                title=title,
                started_at=started_at,
                utterances=utterances,
                summary=summary_md,
                action_items=action_items,
            )

            # People notes
            speakers = list({u.speaker for u in utterances if u.speaker != "Me"})
            for speaker in speakers:
                speaker_lines = "\n".join(u.text for u in utterances if u.speaker == speaker)
                existing = await self._get_existing_person_note(speaker)
                updated = await chat(people_notes_prompt(speaker, existing, speaker_lines))
                await update_person_note(speaker, updated, title)

        except StopIteration:
            pass  # summarization intentionally skipped
        except Exception as e:
            log.warning(f"LLM unavailable — transcript saved without summary: {e}")

        # Save to DB
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE meetings SET status='done', ended_at=?, summary=?, action_items=?, vault_path=? WHERE id=?",
                (
                    datetime.now().isoformat(),
                    summary_md or None,
                    json.dumps(action_items) if action_items else None,
                    str(vault_path),
                    meeting_id,
                ),
            )
            await db.commit()

        return {
            "meeting_id": meeting_id,
            "title": title,
            "summary": summary_md or None,
            "action_items": action_items,
            "vault_path": str(vault_path),
        }

    async def _load_utterances(self, meeting_id: int) -> list[Utterance]:
        utterances = []
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT speaker, text, start_time, end_time FROM utterances "
                "WHERE meeting_id = ? ORDER BY start_time",
                (meeting_id,),
            )
            async for speaker, text, start, end in cursor:
                utterances.append(Utterance(speaker=speaker, text=text, start=start, end=end))
        return utterances

    async def _get_existing_person_note(self, name: str) -> str:
        from backend.config import VAULT_PEOPLE
        path = VAULT_PEOPLE / f"{name}.md"
        if path.exists():
            import aiofiles
            async with aiofiles.open(path, "r", encoding="utf-8") as f:
                return await f.read()
        return ""

    @staticmethod
    def _extract_action_items(summary_md: str) -> list[str]:
        items = []
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

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def on_utterance(self, cb: UtteranceCallback):
        self._utterance_callbacks.append(cb)

    def on_partial(self, cb: Callable[[int, str, str], Awaitable[None]]):
        self._partial_callbacks.append(cb)

    def on_status(self, cb: Callable[[str, dict], Awaitable[None]]):
        self._status_callbacks.append(cb)

    async def _emit_partial(self, meeting_id: int, speaker: str, text: str):
        for cb in self._partial_callbacks:
            try:
                await cb(meeting_id, speaker, text)
            except Exception:
                pass

    async def _emit_status(self, event: str, data: dict):
        for cb in self._status_callbacks:
            try:
                await cb(event, data)
            except Exception:
                pass

    # ── Helpers ───────────────────────────────────────────────────────────────

    def list_audio_devices(self) -> list[dict]:
        return self.capture.list_devices()

    @property
    def is_recording(self) -> bool:
        return self._running

    @property
    def current_meeting_id(self) -> int | None:
        return self._current_meeting_id
