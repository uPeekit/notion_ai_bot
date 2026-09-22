"""The Obsidian side's interpreter and the pipeline around it: what the model is shown, what is
accepted from it, and what happens when it is wrong or down."""

from __future__ import annotations

import json
from datetime import datetime

import anthropic
import pytest

from app import texts
from app.vault.filer import Filer, FilerError, check, context
from app.vault.index import VaultIndex
from app.vault.linker import Linker, apply_links, obvious_links
from app.vault.pipeline import VaultPipeline
from app.vault.writer import VaultAction, VaultWriter

NOW = datetime(2026, 9, 22, 18, 30)


@pytest.fixture
def index(tmp_path):
    (tmp_path / texts.VAULT_BOOKS_DIR).mkdir()
    (tmp_path / texts.VAULT_AREAS_DIR).mkdir()
    (tmp_path / f"{texts.VAULT_BOOKS_DIR}/Чапаев и Пустота.md").write_text(
        "---\nstatus: To read\nauthor: Пелевин\n---\n\nо книге\n", encoding="utf-8")
    (tmp_path / f"{texts.VAULT_AREAS_DIR}/дом.md").write_text(
        "---\ntag: home\n---\n\n## Покупки\n\n- [ ] лампочки\n", encoding="utf-8")
    (tmp_path / f"{texts.VAULT_TASKS_NOTE}.md").write_text("## дом\n\n- [ ] счета #home\n",
                                                            encoding="utf-8")
    (tmp_path / "_bot.md").write_text("Задачи — в файл задач.\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    return index


class FakeAnthropic:
    """Enough of anthropic.AsyncAnthropic for the filer and the linker."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.seen: list[dict] = []
        self.messages = self

    async def create(self, **kwargs):
        self.seen.append(kwargs)
        answer = self.answers.pop(0) if self.answers else {}
        if isinstance(answer, Exception):
            raise answer
        return type("Resp", (), {
            "content": [type("Block", (), {"type": "text", "text": json.dumps(answer)})()],
            "stop_reason": "end_turn",
            "usage": type("U", (), {"input_tokens": 100, "output_tokens": 20})(),
        })()

    async def close(self) -> None:
        pass


# ---- context -----------------------------------------------------------------------------

def test_context_shows_names_folders_tags_and_the_guide_but_no_paths(index):
    ctx = context(index, "дочитал чапаева", NOW)
    assert "Чапаев и Пустота" in ctx.known_notes
    assert texts.VAULT_BOOKS_DIR in ctx.folders and "home" in ctx.tags
    assert ctx.guide.startswith("Задачи")
    payload = ctx.json()
    assert "2026-09-22" in payload and ".md" not in payload


def test_context_offers_the_open_tasks_a_message_could_be_about(index):
    """Without this the filer cannot tick anything: it sees note names, and a task is a line
    inside one. «посылку забрал» has to find the task that is already there."""
    assert context(index, "счета оплатил", NOW).open_tasks == ["счета #home"]
    assert context(index, "что-то другое", NOW).open_tasks == []


# ---- checking the model's answer ---------------------------------------------------------

def test_check_keeps_a_good_task(index):
    [action] = check([{"action": "task", "text": "купить лампочки", "heading": "дом",
                       "tags": ["#home"], "due": "2026-09-30", "repeat": "every month"}],
                     index, "купи лампочки")
    assert action.action == "task" and action.tags == ["home"]
    assert action.due == "2026-09-30" and action.repeat == "every month"


def test_check_drops_a_made_up_date_or_repeat_rule(index):
    [action] = check([{"action": "task", "text": "зубы", "due": "завтра",
                       "repeat": "каждый день"}], index, "зубы")
    assert action.due == "" and action.repeat == ""


def test_check_sends_an_unknown_note_to_the_inbox_with_the_users_words(index):
    [action] = check([{"action": "append", "note": "Такой заметки нет", "body": ["строка"]}],
                     index, "допиши строку в заметку")
    assert action.action == "inbox" and action.text == "строка"
    [action] = check([{"action": "update", "note": "нет", "props": []}], index, "исходное")
    assert action.action == "inbox" and action.text == "исходное"


def test_check_puts_a_note_with_an_unknown_folder_into_the_notes_folder(index):
    [action] = check([{"action": "note", "folder": "Выдуманная", "title": "Идея"}], index, "x")
    assert action.folder == texts.VAULT_NOTES_DIR
    [action] = check([{"action": "note", "folder": texts.VAULT_BOOKS_DIR, "title": "Книга",
                       "props": [{"name": "status", "value": "To read"}]}], index, "x")
    assert action.folder == texts.VAULT_BOOKS_DIR and action.props == {"status": "To read"}


def test_check_refuses_nonsense_and_caps_the_list(index):
    assert check([{"action": "task", "text": "   "}], index, "x") == []
    assert check([{"action": "выдумка", "text": "что-то"}], index, "x")[0].action == "inbox"
    assert len(check([{"action": "task", "text": f"t{i}"} for i in range(50)], index, "x")) == 10


# ---- the model call ----------------------------------------------------------------------

async def test_filer_asks_with_the_vault_and_returns_actions(index):
    client = FakeAnthropic({"actions": [{"action": "task", "text": "зубы"}]})
    filer = Filer("", "claude-haiku-4-5", client=client)
    actions, prompt_tokens, output_tokens = await filer.file("зубы", context(index, "зубы", NOW))
    assert actions == [{"action": "task", "text": "зубы"}]
    assert (prompt_tokens, output_tokens) == (100, 20)
    sent = client.seen[0]
    assert sent["model"] == "claude-haiku-4-5"
    assert "зубы" in sent["messages"][0]["content"]
    assert sent["output_config"]["format"]["type"] == "json_schema"


async def test_filer_reports_a_failed_call(index):
    error = anthropic.APIError("boom", request=None, body=None)  # type: ignore[arg-type]
    filer = Filer("", "m", client=FakeAnthropic(error))
    with pytest.raises(FilerError):
        await filer.file("x", context(index, "x", NOW))


# ---- the pipeline ------------------------------------------------------------------------

def pipeline(index, *answers, linker=None) -> VaultPipeline:
    writer = VaultWriter(index, now=lambda: NOW)
    filer = Filer("", "claude-haiku-4-5", client=FakeAnthropic(*answers))
    return VaultPipeline(index, writer, filer, linker, now=lambda: NOW)


async def test_pipeline_writes_and_says_what_it_did(index):
    turn = await pipeline(index, {"actions": [
        {"action": "task", "text": "купить лампочки", "heading": "дом", "tags": ["home"]},
        {"action": "update", "note": "Чапаев и Пустота", "props": [
            {"name": "status", "value": "Read"}]},
    ]}).handle("купи лампочки и отметь что дочитал чапаева")
    assert [w.kind for w in turn.writes] == ["task", "update"]
    assert "Obsidian:" in turn.reply_line()
    assert "- [ ] купить лампочки #home" in index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    assert "status: Read" in index.read(f"{texts.VAULT_BOOKS_DIR}/Чапаев и Пустота.md")
    assert len(turn.undos) == 2


async def test_pipeline_keeps_the_words_when_the_model_answers_nothing(index):
    turn = await pipeline(index, {"actions": []}).handle("какая-то мысль")
    assert [w.kind for w in turn.writes] == ["inbox"]
    assert "какая-то мысль" in index.read(f"{texts.VAULT_INBOX_NOTE}.md")


async def test_pipeline_survives_a_model_that_is_down(index):
    error = anthropic.APIError("down", request=None, body=None)  # type: ignore[arg-type]
    turn = await pipeline(index, error).handle("зубы")
    assert turn.writes == [] and turn.error
    assert texts.VAULT_FAILED.split("{")[0] in turn.reply_line()


async def test_undo_puts_every_file_of_the_turn_back(index):
    before = index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    pipe = pipeline(index, {"actions": [{"action": "task", "text": "зубы", "heading": "дом"}]})
    turn = await pipe.handle("зубы")
    assert "зубы" in index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    await pipe.undo(turn.undos)
    assert index.read(f"{texts.VAULT_TASKS_NOTE}.md") == before


# ---- the linker --------------------------------------------------------------------------

def test_obvious_links_find_existing_names_and_leave_markup_alone(index):
    body = "читаю Чапаев и Пустота, см. [[дом]] и `дом` и https://x/дом"
    pairs = obvious_links(body, index)
    assert ("Чапаев и Пустота", "Чапаев и Пустота") in pairs
    assert not any(note == "дом" for _, note in pairs)  # already linked, in code, in a URL


def test_apply_links_writes_each_link_once_longest_first():
    out = apply_links("Чапаев и Пустота — роман, Чапаев там главный",
                      [("Чапаев", "Чапаев"), ("Чапаев и Пустота", "Чапаев и Пустота")])
    assert out.startswith("[[Чапаев и Пустота]] — роман, [[Чапаев]] там главный")


async def test_linker_adds_model_links_only_for_real_notes_and_real_words(index, tmp_path):
    writer = VaultWriter(index, now=lambda: NOW)
    client = FakeAnthropic({"links": [
        {"phrase": "Пелевина", "note": "Чапаев и Пустота"},  # kept: both sides check out
        {"phrase": "Пелевина", "note": "Выдуманная заметка"},  # note does not exist
        {"phrase": "чего тут нет", "note": "Чапаев и Пустота"},  # words are not in the text
    ]})
    linker = Linker(index, writer, model="m", client=client)
    write = writer.run(VaultAction(action="note", folder=texts.VAULT_NOTES_DIR, title="Мысль",
                                   body=["перечитать Пелевина"]))
    assert await linker.link(write) == 1
    text = index.read(write.path)
    assert "[[Чапаев и Пустота|Пелевина]]" in text and "Выдуманная" not in text


async def test_linker_leaves_the_task_and_inbox_notes_alone(index):
    writer = VaultWriter(index, now=lambda: NOW)
    linker = Linker(index, writer, model="m", client=FakeAnthropic({"links": []}))
    write = writer.run(VaultAction(action="task", text="дочитать Чапаев и Пустота",
                                    heading="дом"))
    assert await linker.link(write) == 0
    assert "[[" not in index.read(write.path)
