"""Tiny utility for fire-and-forget asyncio tasks.

`asyncio.create_task(coro)` swallows exceptions silently — the task dies,
the exception is logged only when the task is garbage-collected (often
much later), and the system carries on in an inconsistent state. That's
the source of half the "auto-capture stuck", "daily brief never refreshes"
bugs we've shipped.

`safe_task` does three things:

  * Always attaches a done-callback that logs the exception with a name
    so we can grep for it in `sidecar.log`.
  * Holds a strong reference until the task completes — without this,
    Python may GC a fire-and-forget task before it gets a chance to run
    (https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task).
  * Optional `on_error` hook so the caller can react (e.g. flip state to
    "error", broadcast to clients) instead of just logging.

Use it everywhere we'd otherwise write `asyncio.create_task(...)`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, TypeVar

log = logging.getLogger("aurascribe.tasks")

# Strong references to in-flight tasks so the GC doesn't collect them
# before they run. Cleaned in the done-callback.
_LIVE_TASKS: set[asyncio.Task[Any]] = set()


def safe_task(
    coro: Awaitable[Any],
    *,
    name: str,
    on_error: Callable[[BaseException], None] | None = None,
) -> asyncio.Task[Any]:
    """Spawn a fire-and-forget task that logs unhandled exceptions.

    `name` is what shows up in the log line and (where supported) in
    `asyncio` task introspection. Keep it short and identifying — e.g.
    `"auto_capture.enable"`, `"daily_brief.regen[2026-04-22]"`.
    """
    task = asyncio.create_task(coro, name=name)
    _LIVE_TASKS.add(task)

    def _done(t: asyncio.Task[Any]) -> None:
        _LIVE_TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is None:
            return
        log.exception("safe_task %r raised", name, exc_info=exc)
        if on_error is not None:
            try:
                on_error(exc)
            except Exception:
                # If the error hook itself fails, don't take the process
                # with it — but do log so we know the hook is broken.
                log.exception("safe_task %r on_error hook failed", name)

    task.add_done_callback(_done)
    return task


T = TypeVar("T")


class BlockingCallTimeout(Exception):
    """Raised when `run_sync_with_timeout` exceeds its deadline. The
    underlying thread is LEFT RUNNING — Python cannot force-kill threads
    safely, so the caller must assume the blocked work will finish
    eventually (or not) in the background. For sounddevice / PortAudio
    this is fine because the next call creates a fresh handle."""


async def run_sync_with_timeout(
    fn: Callable[..., T],
    *args: Any,
    timeout: float,
    name: str = "sync_call",
    **kwargs: Any,
) -> T:
    """Run a blocking function in the default executor with a timeout.

    Use this to wrap sync calls that can hang the event loop — especially
    audio-device enumeration / stream open on Windows WASAPI, which can
    stall 10+ seconds on a wedged driver. Without a timeout, the stall
    freezes the entire sidecar (including the UI's heartbeat poll).

    Raises `BlockingCallTimeout` if the call doesn't finish within
    `timeout` seconds.
    """
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, lambda: fn(*args, **kwargs)),
            timeout=timeout,
        )
    except asyncio.TimeoutError as e:
        log.warning("run_sync_with_timeout: %s exceeded %.1fs", name, timeout)
        raise BlockingCallTimeout(
            f"{name} did not complete within {timeout:.1f}s"
        ) from e
