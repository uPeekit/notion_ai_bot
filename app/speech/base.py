"""The speech-to-text seam: a protocol plus its error hierarchy, mirroring app/llm/base.py.
Handlers (Task 2) catch SpeechEmpty/SpeechError and map them onto texts.ERRORS themselves —
this module holds no user-facing strings."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class SpeechError(Exception):
    pass


class SpeechEmpty(SpeechError):
    """The decoder ran and returned no segments, or only whitespace. A normal outcome (the
    user sent silence or noise), not something to log at ERROR level."""


class SpeechToText(Protocol):
    async def transcribe(self, path: Path) -> str: ...
