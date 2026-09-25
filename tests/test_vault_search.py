"""Answering a question from the vault, and the on/off switches the admin page writes."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from app import texts
from app.switches import NAMES, Switches
from app.vault.filer import check, context
from app.vault.index import VaultIndex
from app.vault.pipeline import VaultPipeline
from app.vault.search import search
from app.vault.writer import VaultWriter
from tests.test_vault_filer import FakeAnthropic

NOW = datetime(2026, 9, 23, 10, 0)


@pytest.fixture
def index(tmp_path):
    (tmp_path / texts.VAULT_BOOKS_DIR).mkdir()
    (tmp_path / texts.VAULT_NOTES_DIR).mkdir()
    (tmp_path / f"{texts.VAULT_BOOKS_DIR}/Чапаев и Пустота.md").write_text(
        "---\nstatus: Reading\nauthor: Пелевин\n---\n\nо книге\n", encoding="utf-8")
    (tmp_path / f"{texts.VAULT_NOTES_DIR}/Рецепт борща.md").write_text(
        "---\ntags: [home]\n---\n\n## Ингредиенты\n\n- свёкла\n- копчёная курица\n",
        encoding="utf-8")
    (tmp_path / f"{texts.VAULT_TASKS_NOTE}.md").write_text(
        "## дом\n\n- [ ] купить лампочки #home\n- [x] старое дело #home\n\n## моя херня\n\n"
        "- [ ] забрать посылку #personal\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    return index


def names_of(hits) -> list[str]:
    return [h.name for h in hits]


def names(hits) -> list[str]:
    return [h.line if h.kind == "task" else h.name for h in hits]


def test_search_finds_notes_by_name_properties_and_text(index):
    assert "Чапаев и Пустота" in names(search(index, "чапаев"))
    assert "Чапаев и Пустота" in names(search(index, "пелевин"))  # a property value
    hits = search(index, "копчёная курица")
    assert hits[0].name == "Рецепт борща" and "куриц" in hits[0].line


def test_search_answers_with_open_tasks_not_finished_ones(index):
    found = names(search(index, "лампочки"))
    assert any("купить лампочки" in f for f in found)
    assert not any("старое дело" in f for f in names(search(index, "старое дело")))


def test_search_by_tag_alone_lists_what_carries_it(index):
    found = names(search(index, "", tags=("personal",)))
    assert any("забрать посылку" in f for f in found)
    assert not any("лампочки" in f for f in found)


def test_search_can_be_limited_to_a_folder(index):
    assert names(search(index, "борщ", folder=texts.VAULT_NOTES_DIR)) == ["Рецепт борща"]
    # The words match nothing in the folder that was named: the folder itself is the answer
    # ("когда следующая встреча" in the meetings folder), not silence.
    assert names(search(index, "борщ", folder=texts.VAULT_BOOKS_DIR)) == ["Чапаев и Пустота"]


def test_a_filter_with_no_words_lists_what_it_selected(index):
    assert names(search(index, "", props={"status": "Reading"})) == ["Чапаев и Пустота"]
    assert names(search(index, "", props={"status": "Read"})) == []  # exact, not by stem


def test_words_match_by_form_not_by_a_shared_start(index):
    """The bug this fixes: a four-letter stem made «шведскую» match «шведбанк» and «страницу»
    match «страха», so a question about one page answered with three unrelated ones."""
    from app.vault.index import related

    assert related("шведскую", "шведская") and related("книги", "книга")
    assert related("чапаева", "чапаев") and related("борщ", "борща")
    assert not related("шведскую", "шведбанк")
    assert not related("страницу", "страха")
    assert not related("дом", "домой")  # too short to judge


def test_command_words_do_not_drag_in_every_note(index):
    """«найди мою страницу про X» must answer about X, not about every note containing
    «страница»."""
    names = names_of(search(index, "найди мою страницу про борщ"))
    assert names == ["Рецепт борща"]


def test_one_shared_word_in_the_text_is_not_an_answer(index):
    # The recipe's text mentions "курица"; the book's does not mention "Пелевин" — a question
    # naming several things is not answered by a note that happens to carry one of them.
    hits = search(index, "курица пелевина чапаева")
    assert all("куриц" not in h.line for h in hits)
    # two of the words in one line is an answer, one is not
    assert any("куриц" in h.line for h in search(index, "копчёная курица"))


def test_a_search_finds_nothing_rather_than_guessing(index):
    assert search(index, "квантовая хромодинамика") == []
    assert search(index, "") == []


async def test_the_pipeline_answers_a_question_without_writing(index, tmp_path):
    claude = FakeAnthropic({"actions": [{"action": "search", "text": "лампочки"}]})
    from app.vault.filer import Filer

    pipe = VaultPipeline(index, VaultWriter(index, now=lambda: NOW),
                         Filer("", "m", client=claude), None, now=lambda: NOW)
    turn = await pipe.handle("что там по лампочкам?")

    assert turn.writes == [] and turn.asked and turn.undos == []
    line = turn.reply_line()
    assert texts.VAULT_SEARCH_HEADER in line and "купить лампочки" in line


async def test_a_note_hit_is_a_link_that_opens_it_in_obsidian(index):
    from app.vault.filer import Filer

    claude = FakeAnthropic({"actions": [{"action": "search", "text": "борщ"}]})
    pipe = VaultPipeline(index, VaultWriter(index, now=lambda: NOW),
                         Filer("", "m", client=claude), None, now=lambda: NOW)
    turn = await pipe.handle("что там про борщ?")
    assert f"obsidian://open?vault={turn.vault}&file=" in turn.reply_line()
    assert "%D0%97%D0%B0%D0%BC" in turn.reply_line()  # the folder, percent-encoded


async def test_a_question_with_no_answer_says_so(index):
    from app.vault.filer import Filer

    claude = FakeAnthropic({"actions": [{"action": "search", "text": "хромодинамика"}]})
    pipe = VaultPipeline(index, VaultWriter(index, now=lambda: NOW),
                         Filer("", "m", client=claude), None, now=lambda: NOW)
    turn = await pipe.handle("что у меня про хромодинамику?")
    assert turn.reply_line() == texts.VAULT_SEARCH_EMPTY


def test_a_search_action_without_anything_to_search_for_becomes_an_inbox_line(index):
    [action] = check([{"action": "search", "text": "", "tags": []}], index, "что там?")
    assert action.action == "inbox" and action.text == "что там?"


def test_the_filer_is_told_which_notes_and_tasks_could_answer(index):
    ctx = context(index, "что там по лампочкам", NOW)
    assert any("лампочки" in t for t in ctx.open_tasks)


# ---- switches ----------------------------------------------------------------------------

def test_switches_default_to_env_and_are_overridden_by_the_file(tmp_path):
    path = tmp_path / "switches.json"
    switches = Switches(path, {"notion": False})
    assert switches.all() == {"notion": False, "obsidian": True, "linker": True, "mail": True}

    assert switches.save({"notion": True, "obsidian": False, "linker": True, "mail": True}) == 2
    assert switches.save({"notion": True, "obsidian": False, "linker": True, "mail": True}) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["obsidian"] is False

    fresh = Switches(path, {"notion": False})  # the file wins over .env once it exists
    assert fresh.get("notion") is True and fresh.get("obsidian") is False


def test_switches_notice_a_change_made_while_the_bot_runs(tmp_path):
    path = tmp_path / "switches.json"
    switches = Switches(path)
    assert switches.get("obsidian") is True
    path.write_text(json.dumps({name: False for name in NAMES}), encoding="utf-8")
    assert switches.get("obsidian") is False


def test_a_broken_switches_file_leaves_everything_as_env_had_it(tmp_path, caplog):
    path = tmp_path / "switches.json"
    path.write_text("{not json", encoding="utf-8")
    switches = Switches(path, {"notion": True, "obsidian": True, "linker": False})
    assert switches.all() == {"notion": True, "obsidian": True, "linker": False, "mail": True}
