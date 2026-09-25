"""The bot's name: when it is being addressed, and when the name is just a word in a sentence."""

from __future__ import annotations

import pytest

from app import address, texts
from app.conversation.orchestrator import Orchestrator
from tests.helpers import cand, make_interp, val
from tests.test_orchestrator import CHAT, USER, make_bot  # noqa: F401  (make_bot is a fixture)

NAMES = address.names("Джеф,Джефф,Jeff")


@pytest.mark.parametrize("message,expected", [
    ("Джеф, купи молоко", "купи молоко"),
    ("джеф купи молоко", "купи молоко"),
    ("Джефф! что у меня сегодня?", "что у меня сегодня?"),
    ("эй Джеф — напомни про зубы", "напомни про зубы"),
    ("Jeff, buy milk", "buy milk"),
    ("купи молоко, Джеф", "купи молоко"),
    ("Джеф, купи молоко, Джеф", "купи молоко"),
])
def test_the_name_is_taken_off_the_ends(message, expected):
    out = address.strip(message, NAMES)
    assert out.text == expected and out.called and not out.only_name


@pytest.mark.parametrize("message", [
    "Рассмотреть возможность использовать Джефф в Боте",
    "позвонить Джефу насчёт машины",
    "Джефферсон стрит 12",
    "джефгриль купить",
])
def test_the_name_inside_a_sentence_is_left_alone(message):
    out = address.strip(message, NAMES)
    assert out.text == message and not out.called


@pytest.mark.parametrize("message", ["Джеф", "  джеф?", "Джефф!!"])
def test_being_called_with_nothing_else(message):
    out = address.strip(message, NAMES)
    assert out.only_name and out.called and out.text == ""


def test_without_a_configured_name_nothing_changes():
    out = address.strip("Джеф, купи молоко", address.names(""))
    assert out.text == "Джеф, купи молоко" and not out.called


def test_questions_about_the_bot():
    assert address.about_question("кто ты?")
    assert address.about_question("а что ты умеешь")
    # a long message that merely contains the words is a note, not a question about the bot
    assert not address.about_question("запиши в заметку про бота что ты умеешь и чего нет")
    assert not address.about_question("купить молоко")


# ---- through the bot ----------------------------------------------------------------------

@pytest.fixture
def named(tmp_path, env, monkeypatch):
    bot = make_bot(tmp_path)
    bot.orch._names = NAMES
    return bot


async def test_a_message_addressed_by_name_is_written_without_it(named):
    named.llm.queue(make_interp("create", cand(named.ctx, "t2", 0.95,
                                                fields={"t2.f1": val("Молоко", 1.0)})))
    await named.orch.handle_text(CHAT, USER, "Джеф, купи молоко")
    text, _payload, _schema = named.llm.seen[0]
    assert text == "купи молоко"  # the interpreter never sees the name


async def test_calling_the_bot_by_name_alone_answers_without_a_model(named):
    reply = await named.orch.handle_text(CHAT, USER, "Джеф?")
    assert reply.text == texts.CALLED
    assert named.llm.calls == 0 and named.notion.calls == []


async def test_asking_what_it_is_answers_from_texts(named):
    reply = await named.orch.handle_text(CHAT, USER, "Джеф, кто ты?")
    assert "Джеф" in reply.text and named.llm.calls == 0


async def test_an_unnamed_bot_still_works_the_old_way(tmp_path, env):
    bot = make_bot(tmp_path)
    assert isinstance(bot.orch, Orchestrator) and bot.orch._names == ()
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95,
                                              fields={"t2.f1": val("Молоко", 1.0)})))
    await bot.orch.handle_text(CHAT, USER, "Джеф, купи молоко")
    text, _payload, _schema = bot.llm.seen[0]
    assert text == "Джеф, купи молоко"
    bot.store.close()
