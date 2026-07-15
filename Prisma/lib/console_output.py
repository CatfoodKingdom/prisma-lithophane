"""Make launcher console output safe for arbitrary portable-install paths."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import Any


def configure_console_streams(streams: Iterable[Any] | None = None) -> None:
    """Prevent an unencodable status path from terminating a launcher.

    Windows console streams normally support Unicode, but redirected streams
    may inherit a legacy encoding such as cp1252. Preserve the stream's chosen
    encoding while escaping only characters it cannot represent.
    """

    selected = (sys.stdout, sys.stderr) if streams is None else tuple(streams)
    for stream in selected:
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(errors="backslashreplace")
        except Exception:
            # Closed, detached, or host-provided streams may not be mutable.
            # Their owner remains responsible for output behavior.
            continue


__all__ = ["configure_console_streams"]
