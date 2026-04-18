"""Meeting lifecycle orchestrator.

Pipeline: audio capture → transcription → (diarization) → LLM summary → Obsidian write.

Phase 2 wires the shape with a `StubEngine` (returns no utterances); Phase 3
swaps in `WhisperEngine`. Phase 4 adds real diarization.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Awaitable, Callable

import aiosqlite

from aurascribe.audio.capture import AudioCapture
from aurascribe.config import DB_PATH, SAMPLE_RATE
from aurascribe.llm.client import chat
from aurascribe.llm.prompts import (
    MEETING_SUMMARY_SYSTEM,
    format_transcript,
    meeting_summary_prompt,
    people_notes_prompt,
)
from aurascribe.obsidian.writer import update_person_note, write_meeting
from aurascribe.transcription import TranscriptionEngine, Utterance, default_engine

log = logging.getLogger("aurascribe")

UtteranceCallback = Callable[[str, list[Utterance]], Awaitable[None]]
PartialCallback = Callable[[str, str, str], Awaitable[None]]
StatusCallback = Callable[[str, dict], Awaitable[None]]


class MeetingManager:
    def __init__(self, engine: TranscriptionEngine | None = None) -> None:
        self.capture = AudioCapture()
        self.engine: TranscriptionEngine = engine or default_engine()
        self._current_meeting_id: str | None = None
        self._utterance_callbacks: list[UtteranceCallback] = []
        self._partial_callbacks: list[PartialCallback] = []
        self._status_callbacks: list[StatusCallback] = []
        self._running = False
        self._ready = False
        self._task: asyncio.Task | None = None
        self._spec_task: asyncio.Task | None = None
        self._transcribe_sem = asyncio.Semaphore(1)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        await self._emit_status("loading", {"message": "Loading transcription models..."})
        await self.engine.load()
        self._ready = True
        await self._emit_status("ready", {"message": "Models loaded. Ready to record."})

    async def start_meeting(self, title: str = "", device: int | None = None) -> str:
        if self._running:
            raise RuntimeError("Already recording")
        if not self._ready:
            raise RuntimeError("Models still loading — try again in a moment")

        if not title:
            title = f"Meeting {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        meeting_id = str(uuid.uuid4())
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO meetings (id, title, started_at, status) VALUES (?, ?, ?, 'recording')",
                (meeting_id, title, datetime.now().isoformat()),
            )
            await db.commit()

        self._current_meeting_id = meeting_id
        self._running = True
        try:
            await self.capture.start(device=device)
        except Exception:
            # Roll back state so the next click doesn't get "Already recording".
            self._running = False
            self._current_meeting_id = None
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
                await db.commit()
            raise
        self._task = asyncio.create_task(self._record_loop(meeting_id))
        self._spec_task = asyncio.create_task(self._speculative_loop(meeting_id))
        await self._emit_status("recording", {"meeting_id": meeting_id, "title": title})
        return meeting_id

    async def stop_meeting(self, summarize: bool = False) -> dict:
        if not self._running or self._current_meeting_id is None:
            raise RuntimeError("No active recording")

        self._running = False
        await self.capture.stop()
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
        title, started_at_str = row
        started_at = datetime.fromisoformat(started_at_str)

        all_utterances: list[Utterance] = []

        async for audio_chunk in self.capture.stream_speech_chunks():
            if len(audio_chunk) < int(SAMPLE_RATE * 0.3):
                continue
            chunk_duration = len(audio_chunk) / SAMPLE_RATE
            log.info(f"Transcribing chunk: {chunk_duration:.1f}s of audio")
            try:
                async with self._transcribe_sem:
                    utterances = await self.engine.transcribe(audio_chunk)
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

    async def _speculative_loop(self, meeting_id: str) -> None:
        """Every 1.5s, transcribe the last 4s of audio for live partial display."""
        await asyncio.sleep(1.5)
        while self._running:
            try:
                await asyncio.sleep(1.5)
                if not self._running:
                    break
                if self._transcribe_sem.locked():
                    continue
                audio = self.capture.get_recent_audio(seconds=4.0)
                if audio is None or len(audio) < int(SAMPLE_RATE * 1.0):
                    continue
                async with self._transcribe_sem:
                    utterances = await self.engine.transcribe(audio)
                if utterances and self._running:
                    await self._emit_partial(meeting_id, utterances[0].speaker, utterances[0].text)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"Speculative transcription error: {e}", exc_info=True)

    async def _save_utterances(self, meeting_id: str, utterances: list[Utterance]) -> None:
        # Generate a uuid per utterance so the WS broadcast + the frontend's
        # assign flow can reference rows by stable id before the commit.
        async with aiosqlite.connect(DB_PATH) as db:
            for u in utterances:
                u.id = str(uuid.uuid4())
                await db.execute(
                    "INSERT INTO utterances (id, meeting_id, speaker, text, start_time, end_time, embedding, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (u.id, meeting_id, u.speaker, u.text, u.start, u.end, u.embedding, datetime.now().isoformat()),
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
                action_items = self._extract_action_items(summary_md)

                await write_meeting(
                    meeting_id=meeting_id,
                    title=title,
                    started_at=started_at,
                    utterances=utterances,
                    summary=summary_md,
                    action_items=action_items,
                )

                speakers = list({u.speaker for u in utterances if u.speaker != "Me"})
                for speaker in speakers:
                    speaker_lines = "\n".join(u.text for u in utterances if u.speaker == speaker)
                    existing = await self._get_existing_person_note(speaker)
                    updated = await chat(people_notes_prompt(speaker, existing, speaker_lines))
                    await update_person_note(speaker, updated, title)
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

    @staticmethod
    def _extract_action_items(summary_md: str) -> list[str]:
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

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def on_utterance(self, cb: UtteranceCallback) -> None:
        self._utterance_callbacks.append(cb)

    def on_partial(self, cb: PartialCallback) -> None:
        self._partial_callbacks.append(cb)

    def on_status(self, cb: StatusCallback) -> None:
        self._status_callbacks.append(cb)

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

    # ── Helpers ───────────────────────────────────────────────────────────────

    def list_audio_devices(self) -> list[dict]:
        return self.capture.list_devices()

    @property
    def is_recording(self) -> bool:
        return self._running

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def current_meeting_id(self) -> str | None:
        return self._current_meeting_id
