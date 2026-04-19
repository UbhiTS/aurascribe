"""HTTP route package — one submodule per feature area.

Each submodule exposes an `APIRouter` named `router` that the main app
(aurascribe.api) mounts with its own URL prefix. Shared state (manager
singleton, WebSocket clients, common helpers) lives in `_shared`.
"""
from aurascribe.routes.meetings import router as meetings_router
from aurascribe.routes.voices import router as voices_router
from aurascribe.routes.settings import router as settings_router
from aurascribe.routes.daily_brief import router as daily_brief_router
from aurascribe.routes.intel import router as intel_router

__all__ = [
    "meetings_router",
    "voices_router",
    "settings_router",
    "daily_brief_router",
    "intel_router",
]
