import logging
from types import SimpleNamespace

import pytest

from app.llm.base import LLMInvalidOutput, LLMTrace, LLMUnavailable
from app.llm.fallback import FallbackLLM

CTX = SimpleNamespace(local_only=frozenset())


class Answer(SimpleNamespace):
    def __eq__(self, other):  # compare by who answered, so tests read `== "claude"`
        return self.by == other


class Stub:
    def __init__(self, name, *, fail=None, models=None, targets=("t1",)):
        self.name, self.fail, self._models, self.calls = name, fail, models or [name], 0
        self.targets = targets

    async def interpret(self, text, context, schema):
        self.calls += 1
        if self.fail:
            raise self.fail
        answer = Answer(by=self.name, candidates=[SimpleNamespace(target=t) for t in self.targets])
        return answer, LLMTrace(self.name, [], "", 1, 1)

    async def models(self):
        if isinstance(self.fail, LLMUnavailable):
            raise self.fail
        return self._models


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


async def test_primary_answers_when_it_can():
    primary, local = Stub("claude"), Stub("local")
    interp, trace = await FallbackLLM(primary, local).interpret("x", CTX, {})
    assert interp == "claude" and trace.model == "claude" and local.calls == 0


async def test_unavailable_primary_falls_back_and_is_skipped_for_the_cooldown(caplog):
    clock = Clock()
    primary, local = Stub("claude", fail=LLMUnavailable("claude 429: rate limited")), Stub("local")
    llm = FallbackLLM(primary, local, cooldown_s=300, clock=clock)
    with caplog.at_level(logging.WARNING):
        assert (await llm.interpret("x", CTX, {}))[0] == "local"
    assert "429" in caplog.text
    clock.now += 299
    await llm.interpret("x", CTX, {})
    assert primary.calls == 1 and local.calls == 2  # still cooling down: straight to local
    clock.now += 2
    primary.fail = None
    assert (await llm.interpret("x", CTX, {}))[0] == "claude"


async def test_an_invalid_answer_falls_back_for_that_message_only():
    primary = Stub("claude", fail=LLMInvalidOutput("declined"))
    local = Stub("local")
    llm = FallbackLLM(primary, local, clock=Clock())
    assert (await llm.interpret("x", CTX, {}))[0] == "local"
    await llm.interpret("x", CTX, {})
    assert primary.calls == 2  # no cooldown: the next message tries Claude again


async def test_when_both_fail_the_local_error_propagates():
    llm = FallbackLLM(Stub("claude", fail=LLMUnavailable("down")),
                      Stub("local", fail=LLMUnavailable("ollama unreachable")))
    with pytest.raises(LLMUnavailable, match="ollama"):
        await llm.interpret("x", CTX, {})


async def test_models_lists_both_and_survives_an_unreachable_primary(caplog):
    ok = FallbackLLM(Stub("claude"), Stub("local", models=["mistral-nemo:12b"]))
    assert await ok.models() == ["claude", "mistral-nemo:12b"]
    down = FallbackLLM(Stub("claude", fail=LLMUnavailable("claude 401: invalid x-api-key")),
                       Stub("local", models=["mistral-nemo:12b"]))
    with caplog.at_level(logging.WARNING):
        assert await down.models() == ["mistral-nemo:12b"]
    assert "401" in caplog.text


async def test_an_answer_naming_a_local_only_target_is_re_read_locally():
    ctx = SimpleNamespace(local_only=frozenset({"t3"}))
    primary, local = Stub("claude", targets=("t1", "t3")), Stub("local")
    llm = FallbackLLM(primary, local, clock=Clock())
    assert (await llm.interpret("x", ctx, {}))[0] == "local"
    assert primary.calls == 1 and local.calls == 1
    primary.targets = ("t1",)
    assert (await llm.interpret("x", ctx, {}))[0] == "claude"  # no cooldown was set
