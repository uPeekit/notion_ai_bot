"""app.speech.whisper_local against a fake faster-whisper model injected through the
model_factory seam. No test may import faster_whisper, load a real model, or touch the
network — the seam is the whole point."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.config import Settings
from app.speech.base import SpeechEmpty, SpeechError
from app.speech.whisper_local import WhisperLocal


def settings(device: str = "cpu") -> Settings:
    return Settings(
        _env_file=None,
        notion_token="ntn-test-token",
        whisper_model="tiny",
        whisper_device=device,
        whisper_compute_type="int8",
        whisper_language="ru",
    )


@dataclass
class FakeSegment:
    text: str


@dataclass
class FakeModel:
    """Stands in for faster_whisper.WhisperModel. `segments` may itself raise on iteration
    (a generator function) to simulate a failure that only surfaces mid-transcription,
    distinct from a failure raised by the factory at load time."""

    segments: object

    def transcribe(self, path, *, language=None, vad_filter=None):
        segments = self.segments() if callable(self.segments) else iter(self.segments)
        return segments, object()


class RecordingFactory:
    """The constructor-injected seam. Returns canned models/errors in order and records the
    device (and other kwargs) each call received, so tests can assert what WhisperLocal asked
    for without ever importing the real faster_whisper.WhisperModel."""

    def __init__(self, results: list) -> None:
        self._results = list(results)
        self.calls: list[dict] = []

    def __call__(self, model_size_or_path, *, device, compute_type):
        self.calls.append(
            {"model": model_size_or_path, "device": device, "compute_type": compute_type}
        )
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _raise(exc: Exception):
    def gen():
        raise exc
        yield  # pragma: no cover - unreachable, makes this a generator function

    return gen


# ---- joining and emptiness ---------------------------------------------------------------


async def test_transcribe_joins_segments_and_strips():
    factory = RecordingFactory([FakeModel([FakeSegment(" hello "), FakeSegment("world ")])])
    stt = WhisperLocal(settings(), model_factory=factory)

    result = await stt.transcribe(Path("a.ogg"))

    assert result == "hello world"


@pytest.mark.parametrize(
    "segments", [[], [FakeSegment("   ")], [FakeSegment(""), FakeSegment(" ")]]
)
async def test_no_segments_or_whitespace_only_raises_speech_empty(segments):
    factory = RecordingFactory([FakeModel(segments)])
    stt = WhisperLocal(settings(), model_factory=factory)

    with pytest.raises(SpeechEmpty):
        await stt.transcribe(Path("a.ogg"))


# ---- lazy load + caching ------------------------------------------------------------------


async def test_model_is_constructed_once_lazily_and_cached():
    factory = RecordingFactory([FakeModel([FakeSegment("hi")])])
    stt = WhisperLocal(settings(), model_factory=factory)
    assert factory.calls == []  # nothing constructed at __init__ time

    await stt.transcribe(Path("a.ogg"))
    await stt.transcribe(Path("b.ogg"))

    assert len(factory.calls) == 1


# ---- CUDA fallback -------------------------------------------------------------------------


async def test_cuda_load_error_falls_back_to_cpu_and_succeeds(caplog):
    factory = RecordingFactory(
        [RuntimeError("libcublas.so not found"), FakeModel([FakeSegment("ok")])]
    )
    stt = WhisperLocal(settings(device="auto"), model_factory=factory)

    with caplog.at_level(logging.WARNING):
        result = await stt.transcribe(Path("a.ogg"))

    assert result == "ok"
    assert [c["device"] for c in factory.calls] == ["auto", "cpu"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "libcublas.so not found" in warnings[0].getMessage()


async def test_mid_transcription_cuda_error_also_falls_back(caplog):
    """A CUDA failure that only surfaces while consuming segments (not at model-load time)
    must be caught too — faster-whisper returns a lazy generator, so the real failure can
    happen after transcribe() returns and only shows up during iteration."""
    failing_model = FakeModel(_raise(RuntimeError("CUDA error: device-side assert")))
    factory = RecordingFactory([failing_model, FakeModel([FakeSegment("ok")])])
    stt = WhisperLocal(settings(device="auto"), model_factory=factory)

    with caplog.at_level(logging.WARNING):
        result = await stt.transcribe(Path("a.ogg"))

    assert result == "ok"
    assert [c["device"] for c in factory.calls] == ["auto", "cpu"]
    assert sum(1 for r in caplog.records if r.levelno == logging.WARNING) == 1


async def test_fallback_is_not_reattempted_on_the_next_message():
    factory = RecordingFactory(
        [RuntimeError("boom"), FakeModel([FakeSegment("ok")])]
    )
    stt = WhisperLocal(settings(device="auto"), model_factory=factory)

    await stt.transcribe(Path("a.ogg"))
    await stt.transcribe(Path("b.ogg"))

    # only the two calls from the first message's fallback; the second message reused the
    # already-fallen-back cpu model instead of retrying cuda.
    assert [c["device"] for c in factory.calls] == ["auto", "cpu"]


async def test_second_cuda_then_cpu_failure_raises_speech_error():
    factory = RecordingFactory([RuntimeError("cuda broke"), RuntimeError("cpu broke too")])
    stt = WhisperLocal(settings(device="auto"), model_factory=factory)

    with pytest.raises(SpeechError):
        await stt.transcribe(Path("a.ogg"))


async def test_cpu_device_never_attempts_cuda():
    factory = RecordingFactory([RuntimeError("disk full")])
    stt = WhisperLocal(settings(device="cpu"), model_factory=factory)

    with pytest.raises(SpeechError):
        await stt.transcribe(Path("a.ogg"))

    assert [c["device"] for c in factory.calls] == ["cpu"]  # never tried cuda/auto at all


# ---- off the event loop --------------------------------------------------------------------


async def test_transcribe_runs_off_the_event_loop(monkeypatch):
    factory = RecordingFactory([FakeModel([FakeSegment("hi")])])
    stt = WhisperLocal(settings(), model_factory=factory)

    real_to_thread = asyncio.to_thread
    calls = []

    async def spy_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr("app.speech.whisper_local.asyncio.to_thread", spy_to_thread)

    result = await stt.transcribe(Path("a.ogg"))

    assert result == "hi"
    assert len(calls) == 1


# ---- concurrency: two voice notes arriving close together ----------------------------------


async def test_concurrent_transcriptions_construct_the_model_only_once():
    """_ensure_model's `if self._model is None: ...` is a check-then-set: without a lock around
    the whole of _transcribe_sync, two voice notes handled by two asyncio.to_thread workers can
    both see self._model as None and both build a model — doubling a GPU allocation that is
    already sharing 8GB with the LLM. The factory here blocks the first caller to reach it on a
    threading.Event, and only releases once a second, concurrently-dispatched transcribe() call
    has had a real (if generous) window to reach the same check — long enough that an unguarded
    check-then-set would have raced in and called the factory a second time."""
    entered = threading.Event()
    release = threading.Event()

    class SlowFactory:
        def __init__(self, model: FakeModel) -> None:
            self._model = model
            self.calls: list[dict] = []

        def __call__(self, model_size_or_path, *, device, compute_type):
            self.calls.append(
                {"model": model_size_or_path, "device": device, "compute_type": compute_type}
            )
            entered.set()
            assert release.wait(timeout=2), "test setup: release was never set"
            return self._model

    factory = SlowFactory(FakeModel([FakeSegment("hi")]))
    stt = WhisperLocal(settings(), model_factory=factory)

    async def releaser() -> None:
        await asyncio.to_thread(entered.wait, 2)
        await asyncio.sleep(0.05)  # generous window for an unguarded second call to race in
        release.set()

    first, second, _ = await asyncio.gather(
        stt.transcribe(Path("a.ogg")), stt.transcribe(Path("b.ogg")), releaser()
    )

    assert first == "hi"
    assert second == "hi"
    assert len(factory.calls) == 1
