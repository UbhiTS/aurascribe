"""Audio file naming + on-disk discovery for per-meeting recordings.

Filename layout: ``"<uuid> - <sanitized title>.opus"``. The UUID stays first
so the file is uniquely tied to its meeting row regardless of title edits;
the title suffix is purely for browsability in Explorer/Finder. All audio
resolution goes through :func:`find_meeting_audio_file` so callers don't
need to care whether the on-disk name still matches the current title.

Lives in ``aurascribe.audio`` (not ``routes._shared``) so both the meeting
manager and the HTTP routers can use it without a circular import — the
manager is what ``_shared.py`` itself depends on.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import aiosqlite

from aurascribe.config import AUDIO_DIR

log = logging.getLogger(__name__)

# Windows reserves ``\/:*?"<>|`` plus control chars in filenames. macOS/Linux
# only block ``/`` and NUL, but we sanitize for the strictest target so an
# .opus copied across machines stays portable.
_FS_UNSAFE_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WHITESPACE_RUN_RE = re.compile(r"\s+")
_AUDIO_TITLE_MAX = 120


def _sanitize_audio_title(title: str | None) -> str:
    """Filesystem-safe rendering of a meeting title for use in a filename.

    Strips path separators + reserved Windows chars (replaced with a space),
    collapses repeated whitespace, trims trailing dots/spaces (Windows
    refuses to create files with those), and caps at 120 chars so the full
    "<uuid> - <title>.opus" fits comfortably under the 260-char MAX_PATH
    limit without us having to know the parent dir's depth. Falls back to
    "Untitled Meeting" when the input is empty or sanitizes to nothing.
    """
    cleaned = _FS_UNSAFE_CHARS_RE.sub(" ", (title or "").strip())
    cleaned = _WHITESPACE_RUN_RE.sub(" ", cleaned).strip(" .")
    if len(cleaned) > _AUDIO_TITLE_MAX:
        cleaned = cleaned[:_AUDIO_TITLE_MAX].rstrip(" .")
    return cleaned or "Untitled Meeting"


def desired_audio_filename(meeting_id: str, title: str | None) -> str:
    """Return the canonical "<uuid> - <title>.opus" name for a meeting."""
    return f"{meeting_id} - {_sanitize_audio_title(title)}.opus"


def find_meeting_audio_file(meeting_id: str) -> Path | None:
    """Locate the .opus file for ``meeting_id`` regardless of title suffix.

    Tries the new "<uuid> - <title>.opus" layout via glob, falling back to
    the legacy "<uuid>.opus" so meetings recorded before this change stay
    playable. Returns None when nothing on disk matches."""
    matches = sorted(AUDIO_DIR.glob(f"{meeting_id}*.opus"))
    if matches:
        return matches[0]
    legacy = AUDIO_DIR / f"{meeting_id}.opus"
    return legacy if legacy.exists() else None


async def sync_meeting_audio_filename(
    db: aiosqlite.Connection, meeting_id: str
) -> Path | None:
    """Rename the meeting's audio file to match its current title.

    Resolves the on-disk file (handles legacy UUID-only and new layouts),
    computes the desired filename from the row's title, and renames if
    they differ. The new path is written back to ``meetings.audio_path``
    so subsequent reads skip the glob. No-op when:

    * no audio file exists yet (recording in progress / never recorded),
    * the desired name already matches the current name,
    * the rename fails (Windows file lock during recording, permission
      error, etc.) — logged and the existing path is returned as-is.

    Returns the resolved path, or None if no file exists.
    """
    cursor = await db.execute(
        "SELECT title FROM meetings WHERE id = ?", (meeting_id,)
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    title = row[0]

    current = find_meeting_audio_file(meeting_id)
    if current is None:
        return None

    desired_name = desired_audio_filename(meeting_id, title)
    if current.name == desired_name:
        return current

    target = AUDIO_DIR / desired_name
    if target.exists() and target.resolve() != current.resolve():
        # Should never happen — desired_name embeds meeting_id which is
        # unique. If it does (manual file shuffle, sync conflict), leave
        # the canonical UUID-keyed file in place rather than clobbering.
        log.warning(
            "sync_audio_filename collision for %s: %s already exists",
            meeting_id, target,
        )
        return current

    try:
        current.rename(target)
    except OSError as e:
        log.warning(
            "could not rename audio %s → %s: %s", current, target, e,
        )
        return current

    await db.execute(
        "UPDATE meetings SET audio_path = ? WHERE id = ?",
        (str(target), meeting_id),
    )
    return target
