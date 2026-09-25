"""Settings the admin page owns: they fall back to .env and the code, they win once saved, and
they take effect without a restart."""

from __future__ import annotations

import json
from datetime import time

import pytest

from app.daily import DailyMessage, parse_times
from app.tuning import Defaults, Tuning

DEFAULTS = Defaults(
    bot_name="Джеф", agenda_at="09:00", mail_at="12:00,19:00",
    web_words=("найди", "погугли"), countdown_tag="отсчёт",
    date_props=("date", "дата"),
)


@pytest.fixture
def tuning(tmp_path) -> Tuning:
    return Tuning(tmp_path / "tuning.json", DEFAULTS)


def test_without_a_file_everything_comes_from_env_and_the_code(tuning):
    assert tuning.bot_name == "Джеф" and tuning.agenda_at == "09:00"
    assert tuning.web_words == ("найди", "погугли")
    assert tuning.countdown_tag == "отсчёт" and tuning.date_props == ("date", "дата")
    assert tuning.research_note == "" and tuning.linker_note == ""


def test_saved_values_win_and_an_empty_one_resets(tuning):
    assert tuning.save({"bot_name": "Жора", "web_words": "поищи, найди"}) == 2
    assert tuning.bot_name == "Жора" and tuning.web_words == ("поищи", "найди")
    assert tuning.save({"bot_name": "Жора"}) == 0  # unchanged
    assert tuning.save({"bot_name": ""}) == 1
    assert tuning.bot_name == "Джеф"  # back to what .env said


def test_lists_accept_commas_newlines_and_stray_hashes(tuning):
    tuning.save({"date_props": "date,\nдата\n when ", "countdown_tag": "#дедлайн"})
    assert tuning.date_props == ("date", "дата", "when")
    assert tuning.countdown_tag == "дедлайн"


def test_a_change_on_disk_is_noticed_without_a_restart(tuning, tmp_path):
    assert tuning.bot_name == "Джеф"
    (tmp_path / "tuning.json").write_text(json.dumps({"bot_name": "Жора"}), encoding="utf-8")
    assert tuning.bot_name == "Жора"


def test_a_broken_file_falls_back_instead_of_breaking_the_bot(tmp_path, caplog):
    (tmp_path / "tuning.json").write_text("{not json", encoding="utf-8")
    assert Tuning(tmp_path / "tuning.json", DEFAULTS).bot_name == "Джеф"


def test_only_strings_are_accepted(tuning):
    with pytest.raises(ValueError):
        tuning.save({"bot_name": 5})


def test_the_prompts_themselves_are_not_editable(tuning):
    """Deliberate: the extra-instruction fields add to a prompt, they never replace it."""
    assert set(tuning.all()) == {"bot_name", "agenda_at", "mail_at", "web_words",
                                 "countdown_tag", "date_props", "research_note", "linker_note"}


async def test_the_schedule_re_reads_its_times(tuning):
    times: list[tuple[time, ...]] = [parse_times("09:00")]
    daily = DailyMessage(lambda: None, lambda: times[0], "Europe/Tallinn")
    assert daily._times == (time(9, 0),)
    times[0] = parse_times("12:00,19:00")
    assert daily._times == (time(12, 0), time(19, 0))  # picked up with no restart


def test_extra_instructions_are_appended_never_substituted():
    from app.llm.research import _with_extra

    assert _with_extra("ПРАВИЛА", "") == "ПРАВИЛА"
    assert _with_extra("ПРАВИЛА", "  и ещё  ") == "ПРАВИЛА\n\nи ещё"
