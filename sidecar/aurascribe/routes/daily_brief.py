"""Daily Brief endpoints + the cross-meeting regeneration hook.

`regen_brief_for_meeting` is imported by the main app's lifespan so the
Daily Brief for a meeting's date auto-refreshes whenever a meeting
finishes. All failures are swallowed — this is a best-effort background
task, never user-blocking.
"""
from __future__ import annotations

import logging
from datetime import date

import aiosqlite
from fastapi import APIRouter, HTTPException, Query

from aurascribe.config import DB_PATH
from aurascribe.llm import daily_brief as daily_brief_mod
from aurascribe.llm.client import LLMTruncatedError, LLMUnavailableError
from aurascribe.routes._shared import broadcast

log = logging.getLogger("aurascribe")

router = APIRouter(prefix="/api/daily-brief")


async def regen_brief_for_meeting(meeting_id: str) -> None:
    """Find the date a meeting belongs to, mark that day's brief stale, and
    rebuild it. Broadcasts `daily_brief_updated` on completion so the UI
    refetches automatically. Called from api.py's meeting-finished hook."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT started_at FROM meetings WHERE id = ?", (meeting_id,)
            )
            row = await cursor.fetchone()
        if row is None or not row["started_at"]:
            return
        brief_date = daily_brief_mod.date_of_iso(row["started_at"])
    except Exception as e:
        log.warning(
            "daily_brief: could not resolve date for meeting %s: %s", meeting_id, e
        )
        return

    await daily_brief_mod.mark_stale(brief_date)
    await broadcast({
        "type": "daily_brief_updated",
        "date": brief_date,
        "status": "refreshing",
    })
    try:
        result = await daily_brief_mod.build_brief(brief_date)
    except LLMUnavailableError as e:
        log.info(
            "daily_brief: LLM unavailable while regenerating %s: %s", brief_date, e
        )
        await broadcast({
            "type": "daily_brief_updated",
            "date": brief_date,
            "status": "stale",
        })
        return
    except Exception as e:
        log.warning(
            "daily_brief: regen failed for %s: %s", brief_date, e, exc_info=True
        )
        await broadcast({
            "type": "daily_brief_updated",
            "date": brief_date,
            "status": "stale",
        })
        return
    await broadcast({
        "type": "daily_brief_updated",
        "date": brief_date,
        "status": "ready",
        "generated_at": result.get("generated_at"),
    })


@router.get("")
async def get_daily_brief(date_param: str | None = Query(None, alias="date")) -> dict:
    """Return the cached brief for `date` (defaults to today). Fast — does
    NOT call the LLM. If the brief is missing or marked stale, the UI can
    trigger `/api/daily-brief/refresh` to rebuild it."""
    brief_date = date_param or daily_brief_mod.today_str()
    try:
        date.fromisoformat(brief_date)
    except ValueError:
        raise HTTPException(400, f"Invalid date (expected YYYY-MM-DD): {brief_date}")

    cached = await daily_brief_mod.get_cached(brief_date)
    # Still report the meeting count for the date even if no brief exists yet
    # — lets the UI say "2 meetings on this day, tap refresh to build brief".
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, title, started_at, ended_at, status FROM meetings "
            "WHERE started_at >= ? AND started_at < date(?, '+1 day') "
            "ORDER BY started_at",
            (brief_date, brief_date),
        )
        meetings = [dict(r) for r in await cursor.fetchall()]

    return {
        "date": brief_date,
        "brief": cached.get("brief") if cached else None,
        "meeting_count": len(meetings),
        "meeting_ids": cached.get("meeting_ids", []) if cached else [],
        "meetings": meetings,
        "generated_at": cached.get("generated_at") if cached else None,
        "is_stale": cached.get("is_stale", True) if cached else True,
        "exists": cached is not None and cached.get("brief") is not None,
    }


@router.post("/refresh")
async def refresh_daily_brief(date_param: str | None = Query(None, alias="date")) -> dict:
    """Force regeneration of the brief for `date`. Blocks until complete.
    Broadcasts `daily_brief_updated` on the way out so any other clients
    refresh too."""
    brief_date = date_param or daily_brief_mod.today_str()
    try:
        date.fromisoformat(brief_date)
    except ValueError:
        raise HTTPException(400, f"Invalid date (expected YYYY-MM-DD): {brief_date}")

    await broadcast({
        "type": "daily_brief_updated",
        "date": brief_date,
        "status": "refreshing",
    })
    try:
        result = await daily_brief_mod.build_brief(brief_date)
    except LLMUnavailableError as e:
        await broadcast({
            "type": "daily_brief_updated",
            "date": brief_date,
            "status": "stale",
        })
        raise HTTPException(503, str(e))
    except LLMTruncatedError as e:
        await broadcast({
            "type": "daily_brief_updated",
            "date": brief_date,
            "status": "stale",
        })
        raise HTTPException(
            502,
            f"Daily brief was cut off mid-generation ({e}). "
            "Raise `llm_context_tokens` or use a larger model.",
        )

    await broadcast({
        "type": "daily_brief_updated",
        "date": brief_date,
        "status": "ready",
        "generated_at": result.get("generated_at"),
    })
    return {
        "date": brief_date,
        "brief": result["brief"],
        "meeting_count": result["meeting_count"],
        "meeting_ids": result["meeting_ids"],
        "generated_at": result["generated_at"],
        "is_stale": False,
        "exists": True,
    }
