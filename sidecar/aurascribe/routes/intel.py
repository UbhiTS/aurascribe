"""Live intelligence prompt-file endpoints.

The realtime-intelligence loop (and daily-brief generator) both read prompts
from `APP_DATA/prompts/` — user-editable copies seeded from package
defaults. These endpoints let the Settings UI list those files and open
them in the OS default editor.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aurascribe import config

log = logging.getLogger("aurascribe")

router = APIRouter(prefix="/api/intel")


# Friendly display names for the prompt files we know about. Anything else in
# PROMPTS_DIR is shown with its raw filename so the user can still find it.
_PROMPT_LABELS = {
    "live_intelligence.md": "Live Intelligence (highlights + live title)",
    "daily_brief.md": "Daily Brief",
    "meeting_analysis.md": "Meeting Analysis (title + summary)",
    "meeting_bucket.md": "Meeting Bucket Classifier",
}


@router.get("/prompt-path")
async def intel_prompt_path() -> dict:
    """Return the absolute path of the user-editable realtime-intelligence
    prompt file. Lets the UI surface 'edit me' affordances."""
    from aurascribe.llm.realtime import _ensure_prompt_file

    return {"path": str(_ensure_prompt_file())}


@router.get("/prompts")
async def list_prompts() -> dict:
    """Enumerate every .md file under the user's prompts dir (APP_DATA/prompts).
    Known prompts are seeded on first run; the user can also drop extra files
    here. Edits are picked up on the next LLM call — no restart needed."""
    items: list[dict] = []
    for path in sorted(config.PROMPTS_DIR.glob("*.md")):
        items.append({
            "name": _PROMPT_LABELS.get(path.name, path.name),
            "filename": path.name,
            "path": str(path),
        })
    return {"dir": str(config.PROMPTS_DIR), "prompts": items}


class OpenPromptRequest(BaseModel):
    filename: str


@router.post("/open-prompt")
async def open_prompt(req: OpenPromptRequest) -> dict:
    """Open a prompt file in the user's default editor.

    Sidesteps tauri-plugin-shell's URL-only `open` scope. The filename is
    validated against the prompts dir (basename only — no path traversal) so
    this endpoint can't be coaxed into opening arbitrary files."""
    # Reject anything that isn't a bare basename — guards against ../ etc.
    safe_name = Path(req.filename).name
    if safe_name != req.filename or not safe_name:
        raise HTTPException(400, "Invalid filename")
    target = config.PROMPTS_DIR / safe_name
    if not target.exists() or not target.is_file():
        raise HTTPException(404, f"Prompt file not found: {safe_name}")

    abs_target = str(target.resolve())
    log.info("open-prompt: dispatching to OS shell: %r", abs_target)

    try:
        if sys.platform == "win32":
            # `cmd /c start "" "<path>"` is the canonical Windows pattern for
            # "open with default handler". The empty title arg ("") is
            # required because `start` interprets the first quoted argument
            # as a window title — without it, our path becomes the title and
            # the actual file arg is missing. More reliable than os.startfile,
            # which has had path-resolution quirks on certain Windows builds.
            subprocess.Popen(
                ["cmd", "/c", "start", "", abs_target],
                shell=False,
                close_fds=True,
            )
        elif sys.platform == "darwin":
            subprocess.Popen(["open", abs_target])
        else:
            subprocess.Popen(["xdg-open", abs_target])
    except Exception as e:
        raise HTTPException(500, f"Failed to open file: {e}")
    return {"ok": True, "path": abs_target}
