"""Shared Silero VAD loader.

The recording path (`AudioCapture`) and the auto-capture monitor both
want to run Silero VAD, and loading the model twice would double the
RAM hit + duplicate the one-shot `torch.hub.load()` network call on
first use. This module caches a single `(model, utils)` pair behind a
thread-safe lazy initializer — the first caller pays the ~1s load cost,
every caller after shares the cached tensor.

Torch is imported inside the loader so modules that only *declare* they
might use VAD don't pull torch in at import time.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Tuple

log = logging.getLogger("aurascribe")

_vad_model: Any = None
_vad_utils: Any = None
_vad_lock = threading.Lock()


def get_vad_model() -> Tuple[Any, Any]:
    """Return the shared `(silero_vad_model, utils)` pair.

    Loads on first call, cached thereafter. Thread-safe — multiple
    concurrent first-callers will serialize on the lock and share the
    resulting tensor (torch.hub.load isn't reentrant, so serialization
    is necessary anyway).
    """
    global _vad_model, _vad_utils
    if _vad_model is not None:
        return _vad_model, _vad_utils
    with _vad_lock:
        if _vad_model is not None:
            return _vad_model, _vad_utils
        import torch
        model, utils = torch.hub.load(
            repo_or_dir="snakers4/silero-vad",
            model="silero_vad",
            force_reload=False,
            trust_repo=True,
        )
        model.eval()
        _vad_model = model
        _vad_utils = utils
        log.info("Silero VAD loaded (shared instance)")
    return _vad_model, _vad_utils
