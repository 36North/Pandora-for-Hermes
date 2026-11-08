"""Append-only diagnostics for the cast/audio paths.

Hermes' file logging can be unavailable (``concurrent-log-handler`` needs
pywin32, which the managed runtime may not carry; the gateway then falls back to
a handler that writes nothing). These events are too useful to lose to that, so
they also go to a small JSON-lines file under the plugin's data directory:

    <launch home>/plugin-data/pandora/diag.log

Values are already redacted by the caller (audio URLs lose their query string).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
MAX_BYTES = 256_000


def path() -> Path:
    """The diagnostic file beside the plugin's downloaded mpv."""
    from .pandora_mpv import bin_dir

    return bin_dir().parent / "diag.log"


def write(event: str, **fields: Any) -> None:
    """Append one JSON line; never raise into a playback path."""
    try:
        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **fields},
                          default=str)
        with _LOCK:
            if target.is_file() and target.stat().st_size > MAX_BYTES:
                target.replace(target.with_name(target.name + ".1"))
            with target.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:
        pass
