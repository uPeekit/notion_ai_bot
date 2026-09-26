"""The agenda: the morning digest, "what is planned on X", "what should I do now" — and the
schedule that sends the first of those."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta

import pytest

from app import texts
from app.daily import DailyMessage, parse_at
from app.vault import agenda
from app.vault.filer import Filer, check
from app.vault.index import VaultIndex
from app.vault.pipeline import VaultPipeline
from app.vault.writer import VaultWriter
from tests.test_vault_filer import FakeAnthropic

TODAY = date(2026, 9, 25)
NOW = datetime(2026, 9, 25, 10, 0)


@pytest.fixture
def index(tmp_path):
    (tmp_path / texts.VAULT_CLUB_DIR).mkdir()
    (tmp_path / f"{texts.VAULT_TASKS_NOTE}.md").write_text(
        "## дом\n\n"
        "- [ ] платить счета #home 🔁 every month 📅 2026-09-15\n"       # overdue
        "- [ ] коробка разобрать #home 📅 2026-09-20\n"                   # overdue, older? no
        "- [ ] 9.20 ортопед марк #personal 📅 2026-09-25\n"               # today, with a time
        "- [ ] купить шампунь #personal 📅 2026-09-25\n"                  # today
        "- [ ] 13 тервисеконтроль #personal 📅 2026-09-26\n"              # tomorrow
        "- [ ] турник #home\n"                                             # no date
        "- [x] старое #home 📅 2026-09-01 ✅ 2026-09-02\n"                # done: never shown
        "- [ ] далёкое #home 📅 2026-11-19\n",                            # far future
        encoding="utf-8")
    (tmp_path / f"{texts.VAULT_CLUB_DIR}/2026-09-26 Чёрные кувшинки.md").write_text(
        "---\ndate: 2026-09-26\n---\n\nвстреча\n", encoding="utf-8")
    (tmp_path / f"{texts.VAULT_ARCHIVE_NOTE}.md").write_text(
        "- [ ] забытая в архиве #home 📅 2026-09-10\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    return index


def texts_of(items) -> list[str]:
    return [i.text for i in items]


# ---- reading the vault -------------------------------------------------------------------

def test_open_tasks_read_dates_times_and_skip_done_and_archive(index):
    items = agenda.open_tasks(index)
    assert "старое" not in " ".join(texts_of(items))
    assert "забытая в архиве" not in " ".join(texts_of(items))
    ortoped = next(i for i in items if "ортопед" in i.text)
    assert ortoped.due == date(2026, 9, 25) and ortoped.time == "09:20"
    assert ortoped.label.startswith("09:20 ")
    assert ortoped.tags == ("personal",)
    assert next(i for i in items if i.text == "турник").due is None  # tags are not in the text


def test_build_splits_overdue_today_tomorrow_and_events(index):
    a = agenda.build(index, TODAY)
    assert texts_of(a.overdue) == ["платить счета 🔁 every month", "коробка разобрать"]
    assert texts_of(a.today) == ["ортопед марк", "купить шампунь"]
    assert [i.time for i in a.today] == ["09:20", ""]  # timed first
    # a bare leading number is not read as a time ("2 билета" is not 02:00)
    assert texts_of(a.tomorrow) == ["13 тервисеконтроль"]
    assert texts_of(a.events) == ["2026-09-26 Чёрные кувшинки"]
    assert "далёкое" not in str(a)


def test_digest_is_empty_when_nothing_is_due(tmp_path):
    empty = VaultIndex(tmp_path)
    empty.refresh()
    assert agenda.digest(agenda.build(empty, TODAY), TODAY) == ""


def test_digest_reads_like_a_message(index):
    text = agenda.digest(agenda.build(index, TODAY), TODAY)
    assert text.startswith(texts.VAULT_AGENDA_HEADER.format(date="25.09"))
    assert texts.VAULT_AGENDA_OVERDUE.format(n=2) in text
    assert "• 09:20 ортопед марк" in text
    assert "Чёрные кувшинки" in text


def test_on_day_and_range(index):
    assert texts_of(agenda.on_day(index, date(2026, 9, 26))) == [
        "13 тервисеконтроль", "2026-09-26 Чёрные кувшинки"]
    week = agenda.on_day(index, TODAY, TODAY + timedelta(days=7))
    assert len(week) == 4 and "далёкое" not in " ".join(texts_of(week))
    assert agenda.on_day(index, date(2026, 12, 31)) == []


def test_suggest_prefers_missed_one_offs_over_slipped_repeats(index):
    picked = agenda.suggest(index, TODAY, limit=3)
    # "коробка разобрать" is a one-off that was missed; the monthly bill is late by nature
    assert texts_of(picked)[0] == "коробка разобрать"
    assert texts_of(picked)[1] == "платить счета 🔁 every month"
    assert len(picked) == 3
    # nothing overdue or due today left: it still offers something to do
    assert "турник" in " ".join(texts_of(agenda.suggest(index, date(2026, 1, 1))))


# ---- the question reaching the vault ------------------------------------------------------

def pipeline(index, *answers) -> VaultPipeline:
    return VaultPipeline(index, VaultWriter(index, now=lambda: NOW),
                         Filer("", "m", client=FakeAnthropic(*answers)), None, now=lambda: NOW)


async def test_a_question_about_a_day_is_answered_from_the_vault(index):
    turn = await pipeline(index, {"actions": [
        {"action": "agenda", "scope": "day", "due_from": "2026-09-26"}]}
    ).handle("что у меня завтра?")
    assert turn.writes == [] and turn.undos == []
    assert texts.VAULT_ON_DAY.format(date="26.09") in turn.reply_line()
    assert "тервисеконтроль" in turn.reply_line()


async def test_a_question_about_a_range(index):
    turn = await pipeline(index, {"actions": [
        {"action": "agenda", "scope": "day", "due_from": "2026-09-25", "due_to": "2026-09-26"}]}
    ).handle("что на этой неделе?")
    assert texts.VAULT_ON_RANGE.format(start="25.09", end="26.09") in turn.reply_line()


async def test_what_should_i_do_now(index):
    turn = await pipeline(index, {"actions": [{"action": "agenda", "scope": "now"}]}
                          ).handle("что мне сейчас делать?")
    line = turn.reply_line()
    assert line.startswith(texts.VAULT_NOW) and "платить счета" in line


def test_an_agenda_action_without_anything_defaults_to_now(index):
    [action] = check([{"action": "agenda"}], index, "ну что там")
    assert action.action == "agenda" and action.scope == "now"


async def test_what_have_i_not_done_lists_everything_overdue(index):
    """This is the bug the user reported. «Какие у меня задачи просрочены» answered "ничего не
    нашёл" while eleven tasks were overdue — see the two tests below for why."""
    turn = await pipeline(index, {"actions": [{"action": "agenda", "scope": "overdue"}]}
                          ).handle("какие задачи я не сделал?")
    line = turn.reply_line()
    assert line.startswith(texts.VAULT_OVERDUE)
    assert "коробка разобрать" in line


def test_the_scope_the_model_fills_in_by_default_means_nothing(index):
    """Root cause. The filer puts `scope: "any"` on almost every action it answers — it is
    filler, not a scope. Being truthy, it slipped past the "unspecified means what-now" guard and
    the question fell through to the date branch, which looks at *today* alone. Nothing was due
    exactly today, so the answer was "nothing found"."""
    [action] = check([{"action": "agenda", "scope": "any"}], index, "какие задачи просрочены")
    assert action.scope == "now"


async def test_a_question_the_vault_answered_is_not_buried_under_notions_empty_search(index):
    """The other half of what the user saw: Notion cannot express "overdue" at all, so its title
    search found nothing and said so — above the real answer."""
    turn = await pipeline(index, {"actions": [{"action": "agenda", "scope": "overdue"}]}
                          ).handle("какие задачи я не сделал?")
    assert turn.answer and turn.reply_line() == turn.answer


async def test_an_answered_question_is_visible_in_the_log(index, caplog):
    """A turn that answered a question used to log exactly what a turn that did nothing logged
    (`vault <model>: -`), which is how the bug above stayed hidden for a week."""
    import logging

    with caplog.at_level(logging.INFO, logger="app.vault.pipeline"):
        await pipeline(index, {"actions": [{"action": "agenda", "scope": "overdue"}]}
                       ).handle("что просрочено")
    assert any("answered:" in r.getMessage() for r in caplog.records)


def test_made_up_dates_and_scopes_are_dropped(index):
    [action] = check([{"action": "agenda", "scope": "вчера", "due_from": "завтра"}], index, "x")
    assert action.due_from == "" and action.scope == "now"


# ---- the schedule -------------------------------------------------------------------------

def test_parse_at():
    assert parse_at("09:00") == time(9, 0)
    assert parse_at(" 7:30 ") == time(7, 30)
    assert parse_at("") is None and parse_at("утром") is None


def test_next_run_is_tomorrow_once_today_has_passed():
    daily = DailyMessage(lambda: asyncio.sleep(0), time(9, 0), "Europe/Tallinn")
    from zoneinfo import ZoneInfo

    tallinn = ZoneInfo("Europe/Tallinn")
    before = datetime(2026, 9, 25, 8, 59, tzinfo=tallinn)
    after = datetime(2026, 9, 25, 9, 1, tzinfo=tallinn)
    assert daily.next_run(before) == datetime(2026, 9, 25, 9, 0, tzinfo=tallinn)
    assert daily.next_run(after) == datetime(2026, 9, 26, 9, 0, tzinfo=tallinn)


async def test_a_bot_started_just_after_the_hour_still_sends_today():
    from zoneinfo import ZoneInfo

    sent: list[int] = []
    tallinn = ZoneInfo("Europe/Tallinn")
    late = DailyMessage(lambda: _record(sent), time(9, 0), "Europe/Tallinn",
                        now=lambda: datetime(2026, 9, 25, 9, 5, tzinfo=tallinn))
    late.start()
    await asyncio.sleep(0.05)
    await late.stop()
    assert sent == [1]  # fired once on startup, then went back to waiting

    much_later = DailyMessage(lambda: _record(sent), time(9, 0), "Europe/Tallinn",
                              now=lambda: datetime(2026, 9, 25, 20, 0, tzinfo=tallinn))
    much_later.start()
    await asyncio.sleep(0.05)
    await much_later.stop()
    assert sent == [1]  # too late to be useful: it waits for tomorrow


async def test_a_failing_send_does_not_kill_the_schedule():
    async def boom() -> None:
        raise RuntimeError("no telegram")

    from zoneinfo import ZoneInfo

    daily = DailyMessage(boom, time(9, 0), "Europe/Tallinn",
                         now=lambda: datetime(2026, 9, 25, 9, 5,
                                              tzinfo=ZoneInfo("Europe/Tallinn")))
    daily.start()
    await asyncio.sleep(0.05)
    assert daily.running
    await daily.stop()


async def _record(sent: list[int]) -> None:
    sent.append(1)
