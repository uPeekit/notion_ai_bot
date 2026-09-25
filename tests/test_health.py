"""When Claude cannot be used, the bot says so in words the user can act on.

The failure this suite exists for really happened: the Anthropic account ran out of credit at
20:31, the interpreter quietly switched to the local model, the Obsidian side stopped writing
and answered `Obsidian: не записано (claude 400)`, and nothing anywhere said "top up the
account". Everything below pins the parts of the fix that a user would notice."""

from __future__ import annotations

import anthropic
import pytest

from app import texts
from app.llm import health as health_mod
from app.llm.base import LLMUnavailable
from app.llm.fallback import BILLING_COOLDOWN_S, FallbackLLM
from app.llm.health import Health
from app.vault.pipeline import VaultTurn


def _error(status: int | None, message: str = "") -> anthropic.APIError:
    """An API error shaped the way the SDK hands them to us: a status and a body message."""
    body = {"error": {"type": "invalid_request_error", "message": message}} if message else None
    error = anthropic.APIError("boom", request=None, body=body)  # type: ignore[arg-type]
    if status is not None:
        error.status_code = status  # type: ignore[attr-defined]
    return error


# ---- which failures the user can act on -------------------------------------------------------


@pytest.mark.parametrize(("status", "message", "expected"), [
    # The one 400 that is not our own mistake: an empty balance is refused as a bad request.
    (400, "Your credit balance is too low to access the Anthropic API", health_mod.CREDIT),
    (400, "insufficient_quota", health_mod.CREDIT),
    (402, "", health_mod.CREDIT),
    (401, "invalid x-api-key", health_mod.KEY),
    (403, "forbidden", health_mod.KEY),
    (429, "rate limit exceeded", health_mod.LIMIT),
    (500, "internal", health_mod.DOWN),
    (529, "overloaded", health_mod.DOWN),
    # Our own mistake: a schema the API would not compile. Nothing for the user to do.
    (400, "max_tokens: must be greater than 0", ""),
    (404, "model not found", ""),
])
def test_only_failures_with_something_to_do_about_them_are_reported(status, message, expected):
    assert health_mod.reason(_error(status, message)) == expected


def test_a_timeout_or_a_dead_connection_counts_as_an_outage():
    assert health_mod.reason(anthropic.APITimeoutError(request=None)) == health_mod.DOWN  # type: ignore[arg-type]


def test_the_description_carries_the_api_message_and_never_the_request():
    text = health_mod.describe(_error(400, "Your credit balance is too low"))
    assert text == "claude 400: Your credit balance is too low"


# ---- told once an hour, not once a message ----------------------------------------------------


def test_the_warning_is_shown_once_an_hour():
    clock = [1000.0]
    health = Health(remind_s=3600.0, clock=lambda: clock[0])
    health.record(_error(400, "Your credit balance is too low"))

    assert texts.LLM_DOWN_NOTE["credit"] in health.note()
    assert health.note() == ""  # the next twenty messages of the hour stay quiet
    clock[0] += 3599
    assert health.note() == ""
    clock[0] += 2
    assert texts.LLM_DOWN_NOTE["credit"] in health.note()


def test_nothing_is_said_while_claude_works():
    assert Health().note() == ""


def test_a_call_that_goes_through_ends_the_warning():
    health = Health()
    health.record(_error(402))
    assert health.note()
    health.ok()
    assert health.note() == "" and health.reason == ""


def test_a_different_reason_is_worth_saying_again_within_the_hour():
    """The account was topped up and the key then failed: the old sentence would be a lie."""
    health = Health()
    health.record(_error(402))
    assert texts.LLM_DOWN_NOTE["credit"] in health.note()
    health.record(_error(401, "invalid x-api-key"))
    assert texts.LLM_DOWN_NOTE["key"] in health.note()


def test_a_failure_we_caused_is_not_blamed_on_the_service():
    health = Health()
    assert health.record(_error(404, "model not found")) == ""
    assert health.note() == ""


# ---- what the user actually reads -------------------------------------------------------------


def test_the_obsidian_line_names_the_cause_instead_of_a_status_code():
    line = VaultTurn(error="claude 400: Your credit balance is too low",
                     reason=health_mod.CREDIT).reply_line()
    assert texts.LLM_DOWN_SHORT["credit"] in line
    assert "400" not in line  # the status code was the whole of what it used to say


def test_a_vault_failure_that_is_not_claude_still_shows_what_it_was():
    assert "PermissionError" in VaultTurn(error="PermissionError").reply_line()


def test_the_note_says_what_broke_what_it_costs_and_what_to_do():
    note = texts.LLM_DOWN_NOTE["credit"]
    assert "Anthropic" in note and "Obsidian" in note and "console.anthropic.com" in note


# ---- the fallback waits longer for an empty balance --------------------------------------------


class _Dead:
    model = "claude"

    def __init__(self, reason: str) -> None:
        self._reason = reason
        self.calls = 0

    async def interpret(self, text, context, schema):
        self.calls += 1
        raise LLMUnavailable("claude 400", self._reason)

    async def models(self):
        return []


class _Local:
    model = "local"

    def __init__(self) -> None:
        self.calls = 0

    async def interpret(self, text, context, schema):
        self.calls += 1
        return object(), object()

    async def models(self):
        return ["local"]


@pytest.mark.parametrize(("reason", "quiet_for"), [
    (health_mod.CREDIT, BILLING_COOLDOWN_S),
    (health_mod.LIMIT, 300.0),
])
async def test_an_empty_balance_is_retried_far_less_often_than_a_rate_limit(reason, quiet_for):
    """Topping up an account takes longer than five minutes; a rate limit does not."""
    clock = [0.0]
    primary, local = _Dead(reason), _Local()
    llm = FallbackLLM(primary, local, cooldown_s=300.0, clock=lambda: clock[0])

    await llm.interpret("x", None, {})
    clock[0] += quiet_for - 1
    await llm.interpret("x", None, {})
    assert primary.calls == 1  # still inside the cooldown: no second wasted round trip
    clock[0] += 2
    await llm.interpret("x", None, {})
    assert primary.calls == 2
    assert local.calls == 3  # every message was answered, all the way through
