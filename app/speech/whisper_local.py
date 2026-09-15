"""faster-whisper-backed SpeechToText. See documentation/ARCHITECTURE.md section 10.

The model is loaded lazily, off the event loop, on the first call to transcribe() and then
kept for the process lifetime. faster_whisper.WhisperModel.transcribe() returns a lazy
segment generator, so it must be fully consumed inside the worker thread — never after
control returns to the event loop.

A CUDA failure (whether raised while loading the model or only while decoding the first
segments) switches the instance to CPU once, logs a single WARNING naming the original
error, and retries. A second failure raises SpeechError. The CPU fallback, once taken, is
permanent for the life of the instance: later calls go straight to CPU and never touch CUDA
again."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from faster_whisper import WhisperModel

from app.config import Settings
from app.speech.base import SpeechEmpty, SpeechError

log = logging.getLogger(__name__)

ModelFactory = Callable[..., Any]


class WhisperLocal:
    def __init__(self, settings: Settings, *, model_factory: ModelFactory = WhisperModel) -> None:
        self._settings = settings
        self._model_factory = model_factory
        self._model: Any | None = None
        self._device = settings.whisper_device

    async def transcribe(self, path: Path) -> str:
        text = await asyncio.to_thread(self._transcribe_sync, path)
        if not text.strip():
            raise SpeechEmpty()
        return text

    # ---- worker-thread body: never call this directly from the event loop -----------------

    def _transcribe_sync(self, path: Path) -> str:
        try:
            return self._attempt(path)
        except Exception as exc:
            if self._device == "cpu":
                raise SpeechError(f"whisper transcription failed: {exc}") from exc
            log.warning(
                "whisper failed on device %r (%s); falling back to cpu for the rest of the "
                "process",
                self._device,
                exc,
            )
            self._device = "cpu"
            self._model = None
            try:
                return self._attempt(path)
            except Exception as exc2:
                raise SpeechError(f"whisper transcription failed on cpu: {exc2}") from exc2

    def _attempt(self, path: Path) -> str:
        model = self._ensure_model()
        segments, _info = model.transcribe(
            str(path),
            language=self._settings.whisper_language,
            vad_filter=True,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    def _ensure_model(self) -> Any:
        if self._model is None:
            self._model = self._model_factory(
                self._settings.whisper_model,
                device=self._device,
                compute_type=self._settings.whisper_compute_type,
            )
        return self._model
