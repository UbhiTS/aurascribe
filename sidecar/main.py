"""AuraScribe Python sidecar entry point.

Binds to 127.0.0.1 by default — only the Tauri shell reaches it.
"""
from __future__ import annotations

import logging
import os

import uvicorn

from aurascribe.api import app


def main() -> None:
    # Route our own loggers (aurascribe.*) to stdout at INFO so speaker-id
    # diagnostics and pipeline traces show up in `tauri dev`'s console.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logging.getLogger("aurascribe").setLevel(logging.INFO)

    host = os.environ.get("SIDECAR_HOST", "127.0.0.1")
    port = int(os.environ.get("SIDECAR_PORT", "8765"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
