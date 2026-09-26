"""Editing one place on a page, in both stores.

«я хочу чтобы бот мог править любое место на любой странице, включая добавление и удаление
картинок». The page is read once and shown to the model as numbered lines; the model answers
with a short list of operations against those numbers, and `app/llm/edits.check` is what turns
those numbers back into blocks the code actually read.

That gate is the whole safety of the feature, and it is what most of this file is about: a line
the model invented, a sub-page it tried to delete, or a script that would empty the page cannot
get through, however the request was phrased. A picture *can* now be removed — that is what was
asked for — so the tests below also pin that undo brings it back and that the reply says so.
"""

from __future__ import annotations

import anthropic
import pytest

from app import texts
from app.commands.executor import Executor, Refused, UndoRecord
from app.commands.models import RewritePage
from app.conversation.reply import format_execution
from app.llm.context import RECENT_PAGE
from app.llm.edits import Edit, EditError, Editor, EditPlan, check, parse
from app.llm.health import Health
from app.notion.to_markdown import numbered, outline
from app.vault import mdedit
from tests.fakes import FakeNotionProvider

PAGE = "pg-wall"


def _text(kind: str, value: str, bid: str, **extra) -> dict:
    return {"id": bid, "type": kind, kind: {"rich_text": [{"plain_text": value}]}, **extra}


def _image(bid: str = "img-1", caption: str = "") -> dict:
    return {"id": bid, "type": "image",
            "image": {"type": "external", "external": {"url": "https://x/photo.png"},
                      "caption": [{"plain_text": caption}] if caption else []}}


def _page() -> list[dict]:
    """A page shaped like the one this feature was asked for: two approaches, a picture, a
    sub-page and a nested list."""
    return [
        _text("heading_2", "Подход", "h-1"),
        _text("paragraph", "вариант А, 90 см", "p-1"),
        _text("paragraph", "вариант Б, 120 см", "p-2"),
        _image("img-1", "стенка в сборе"),
        {"id": "sub-1", "type": "child_page", "child_page": {"title": "Смета"}},
        _text("bulleted_list_item", "вложенный", "b-1", has_children=True),
    ]


class FakeAnthropic:
    """Answers each call with the next item: a dict becomes the model's JSON, an exception is
    raised instead."""

    def __init__(self, *answers: object) -> None:
        import json as _json

        self._json = _json
        self.answers = list(answers)
        self.seen: list[dict] = []
        self.messages = self

    async def create(self, **kwargs):
        self.seen.append(kwargs)
        answer = self.answers.pop(0) if self.answers else {}
        if isinstance(answer, Exception):
            raise answer
        return type("Resp", (), {
            "content": [type("Block", (), {"type": "text",
                                            "text": self._json.dumps(answer)})()],
            "stop_reason": "end_turn",
            "usage": type("U", (), {"input_tokens": 300, "output_tokens": 60})(),
        })()

    async def close(self) -> None:
        pass


def _executor(provider: FakeNotionProvider, *answers: object) -> Executor:
    return Executor(provider, editor=Editor("", "claude-sonnet-5",
                                            client=FakeAnthropic(*answers)))


def _cmd(instruction: str = "убери второй вариант") -> RewritePage:
    return RewritePage(page_id=PAGE, target_name="Идеи", page_title="Шведская стенка",
                       instruction=instruction)


def _edits(*ops: dict) -> dict:
    full = {"op": "replace", "at": 0, "to": 0, "text": "", "url": "", "caption": ""}
    return {"edits": [{**full, **op} for op in ops], "full": ""}


# ---- the gate: what the model is allowed to point at -------------------------------------


def test_the_model_can_only_point_at_lines_that_were_on_the_page():
    lines = outline(_page())
    plan, refused = check(parse(_edits({"op": "replace", "at": 99, "text": "нет такой"})), lines)
    assert plan.edits == []
    assert "no such line" in refused[0]


def test_a_sub_page_cannot_be_deleted_however_the_request_was_phrased():
    lines = outline(_page())
    plan, refused = check(parse(_edits({"op": "delete", "at": 5})), lines)
    assert plan.edits == []
    assert "child_page stays" in refused[0]


def test_a_block_whose_children_were_never_read_is_left_alone():
    lines = outline(_page())
    plan, refused = check(parse(_edits({"op": "delete", "at": 6},
                                        {"op": "replace", "at": 6, "text": "x"})), lines)
    assert plan.edits == []
    assert any("stays" in r for r in refused)
    assert any("not editable text" in r for r in refused)


def test_a_picture_can_be_removed_but_its_text_cannot_be_edited():
    lines = outline(_page())
    removed, _ = check(parse(_edits({"op": "delete", "at": 4})), lines)
    assert [e.op for e in removed.edits] == ["delete"] and removed.edits[0].at == 4
    edited, refused = check(parse(_edits({"op": "replace", "at": 4, "text": "текст"})), lines)
    assert edited.edits == [] and "not editable text" in refused[0]


def test_a_script_that_would_empty_the_page_is_refused_as_a_rewrite():
    """More than half the page. If that is really the ask, the model was to answer with `full`,
    which goes through the rewrite path and its own safeguards."""
    page = [_text("paragraph", f"строка {i}", f"p-{i}") for i in range(10)]
    plan, refused = check(parse(_edits(*[{"op": "delete", "at": n} for n in range(1, 9)])),
                          outline(page))
    assert plan.edits == []
    assert "that is a rewrite, not an edit" in refused[-1]


def test_a_delete_range_keeps_what_it_may_and_says_what_it_could_not():
    lines = outline(_page())
    plan, refused = check(parse(_edits({"op": "delete", "at": 2, "to": 5})), lines)
    # Lines 2, 3 and 4 (two paragraphs and the picture) go; the sub-page at 5 stays.
    assert [e.at for e in plan.edits] == [2, 3, 4]
    assert "child_page stays" in refused[0]


def test_an_insert_past_the_end_lands_at_the_end_rather_than_nowhere():
    lines = outline(_page())
    plan, _ = check(parse(_edits({"op": "insert", "at": 99, "text": "- новый пункт"})), lines)
    assert plan.edits[0].at == len(lines)


def test_an_image_needs_a_web_address_not_a_made_up_one():
    lines = outline(_page())
    plan, refused = check(parse(_edits({"op": "image", "at": 1, "url": "photo.png"})), lines)
    assert plan.edits == [] and "not a web address" in refused[0]


def test_a_whole_new_text_bypasses_the_edit_rules_and_uses_the_rewrite_path():
    plan, refused = check(EditPlan(full="# Новый текст"), outline(_page()))
    assert plan.full == "# Новый текст" and plan.edits == [] and refused == []


# ---- what the model is shown -----------------------------------------------------------------


def test_the_page_is_numbered_and_says_what_may_not_be_touched():
    shown = numbered(outline(_page()))
    assert "[2] вариант А, 90 см" in shown
    assert texts.OUTLINE_MEDIA.format(what="image: стенка в сборе") in shown
    assert texts.OUTLINE_KEPT.format(what="child_page: Смета") in shown


# ---- applying the edits ----------------------------------------------------------------------


async def test_replacing_a_line_changes_that_block_and_leaves_the_rest_alone():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = _page()
    await _executor(provider, _edits({"op": "replace", "at": 2,
                                       "text": "вариант А, 240 см"})).run(_cmd())

    [call] = [c for c in provider.calls if c[0] == "update_block"]
    assert call[1] == "p-1"
    assert call[2]["paragraph"]["rich_text"][0]["text"]["content"] == "вариант А, 240 см"
    assert not [c for c in provider.calls if c[0] == "delete_block"]


async def test_a_line_that_becomes_a_different_kind_of_block_is_swapped_not_patched():
    """Notion cannot change a block's type, so a paragraph becoming a heading is an insert and
    a delete — which also keeps it in the same place."""
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = _page()
    await _executor(provider, _edits({"op": "replace", "at": 2,
                                       "text": "## Размеры"})).run(_cmd())

    assert not [c for c in provider.calls if c[0] == "update_block"]
    [append] = [c for c in provider.calls if c[0] == "append_blocks"]
    assert append[2][0]["type"] == "heading_2" and append[3] == "p-1"
    assert [c[1] for c in provider.calls if c[0] == "delete_block"] == ["p-1"]


async def test_removing_a_picture_is_carried_out_and_named_in_the_reply():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = _page()
    result = await _executor(provider, _edits({"op": "delete", "at": 4})).run(
        _cmd("убери картинку"))

    assert [c[1] for c in provider.calls if c[0] == "delete_block"] == ["img-1"]
    text = format_execution(result, target_url=None)
    assert texts.EDIT_REMOVED_MEDIA.format(n=1, undo=texts.BTN_UNDO) in text


async def test_adding_a_picture_from_a_link_puts_it_where_it_was_asked_for():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = _page()
    await _executor(provider, _edits({"op": "image", "at": 2,
                                       "url": "https://example.com/p.png",
                                       "caption": "новая"})).run(
        _cmd("добавь картинку после первого варианта"))

    [append] = [c for c in provider.calls if c[0] == "append_blocks"]
    assert append[3] == "p-1"  # after line 2
    assert append[2][0]["type"] == "image"


async def test_several_edits_never_shift_each_others_targets():
    """Everything is resolved against the numbering the model was given: replaces happen in
    place, then insertions, then removals from the bottom up."""
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = _page()
    await _executor(provider, _edits(
        {"op": "replace", "at": 2, "text": "вариант А, 240 см"},
        {"op": "insert", "at": 2, "text": "- купить анкеры"},
        {"op": "delete", "at": 3},
    )).run(_cmd())

    assert [c[1] for c in provider.calls if c[0] == "update_block"] == ["p-1"]
    assert [c[3] for c in provider.calls if c[0] == "append_blocks"] == ["p-1"]
    assert [c[1] for c in provider.calls if c[0] == "delete_block"] == ["p-2"]


async def test_the_reply_counts_the_places_rather_than_the_page_size():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = _page()
    result = await _executor(provider, _edits(
        {"op": "replace", "at": 2, "text": "вариант А, 240 см"},
        {"op": "insert", "at": 2, "text": "- купить анкеры"},
        {"op": "delete", "at": 3},
    )).run(_cmd())
    text = format_execution(result, target_url=None)

    assert texts.EDIT_REPLACED.format(n=1) in text
    assert texts.EDIT_ADDED.format(n=1) in text
    assert texts.EDIT_REMOVED.format(n=1) in text


# ---- undo ---------------------------------------------------------------------------------


async def test_undo_puts_every_kind_of_change_back():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = _page()
    executor = _executor(provider, _edits(
        {"op": "replace", "at": 2, "text": "вариант А, 240 см"},
        {"op": "insert", "at": 2, "text": "- купить анкеры"},
        {"op": "delete", "at": 4},
    ))
    result = await executor.run(_cmd())
    assert result.undo is not None and result.undo.kind == "edits"

    provider.calls.clear()
    await executor.undo(result.undo)

    # The replaced paragraph gets its old text back...
    [patch] = [c for c in provider.calls if c[0] == "update_block"]
    assert patch[1] == "p-1"
    assert patch[2]["paragraph"]["rich_text"][0]["plain_text"] == "вариант А, 90 см"
    # ...the inserted line goes...
    assert [c for c in provider.calls if c[0] == "delete_block"]
    # ...and the picture comes back out of the trash.
    assert [c[1] for c in provider.calls if c[0] == "restore_block"] == ["img-1"]


async def test_undo_survives_a_block_that_is_already_gone():
    from app.notion.errors import NotionError

    provider = FakeNotionProvider()
    provider.fail_restore_block = NotionError(404, "not_found", "gone")
    executor = _executor(provider)
    rec = UndoRecord(kind="edits", page_id=PAGE, edits=[
        {"kind": "deleted", "block_ids": ["img-1"]},
        {"kind": "added", "block_ids": ["new-1"]},
    ])

    await executor.undo(rec)  # must not raise

    assert [c[0] for c in provider.calls] == ["delete_block", "restore_block"]


# ---- failures -------------------------------------------------------------------------------


async def test_a_page_with_nothing_editable_is_refused_before_any_model_call():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = [{"id": "sub-1", "type": "child_page",
                                   "child_page": {"title": "Смета"}}]
    client = FakeAnthropic(_edits({"op": "delete", "at": 1}))
    with pytest.raises(Refused) as exc:
        await Executor(provider, editor=Editor("", "m", client=client)).run(_cmd())
    assert exc.value.code == "REWRITE_EMPTY"
    assert client.seen == []  # nothing was even asked


async def test_a_claude_failure_leaves_the_page_untouched_and_names_the_cause():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = _page()
    error = anthropic.APIError("boom", request=None,  # type: ignore[arg-type]
                               body={"error": {"message": "Your credit balance is too low"}})
    error.status_code = 400  # type: ignore[attr-defined]
    health = Health()
    editor = Editor("", "m", client=FakeAnthropic(error), health=health)

    with pytest.raises(Refused):
        await Executor(provider, editor=editor).run(_cmd())

    assert not [c for c in provider.calls
                if c[0] in ("update_block", "delete_block", "append_blocks")]
    assert health.reason == "credit"


async def test_an_answer_with_no_usable_edits_changes_nothing():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = _page()
    with pytest.raises(Refused) as exc:
        await _executor(provider, _edits({"op": "delete", "at": 5})).run(_cmd())
    assert exc.value.code == "REWRITE_FAILED"
    assert not [c for c in provider.calls if c[0] == "delete_block"]


async def test_the_editor_refuses_an_empty_page_without_calling_claude():
    client = FakeAnthropic()
    with pytest.raises(EditError):
        await Editor("", "m", client=client).plan("   ", "убери лишнее")
    assert client.seen == []


# ---- the Obsidian side -----------------------------------------------------------------------


def test_edits_apply_to_a_notes_lines_without_shifting_each_other():
    lines = ["# Стенка", "", "вариант А", "вариант Б", "конец"]
    edits = [Edit(op="replace", at=3, text="вариант А, 240 см"),
             Edit(op="insert", at=3, text="- купить анкеры"),
             Edit(op="delete", at=4)]

    assert mdedit.apply_edits(lines, edits) == [
        "# Стенка", "", "вариант А, 240 см", "- купить анкеры", "конец"]


def test_an_edit_pointing_outside_the_note_is_ignored():
    lines = ["один", "два"]
    assert mdedit.apply_edits(lines, [Edit(op="replace", at=9, text="нет")]) == lines
    assert mdedit.apply_edits(lines, [Edit(op="delete", at=0)]) == lines


def test_an_image_edit_becomes_a_markdown_image_line():
    out = mdedit.apply_edits(["текст"], [Edit(op="image", at=1, url="https://x/p.png",
                                              caption="подпись")])
    assert out == ["текст", "![подпись](https://x/p.png)"]


def test_a_note_is_numbered_line_by_line():
    assert mdedit.numbered(["один", "два"]) == "[1] один\n[2] два"


async def test_a_note_is_edited_in_place_and_keeps_everything_else(tmp_path):
    from app.vault.filer import Filer
    from app.vault.index import VaultIndex
    from app.vault.pipeline import VaultPipeline
    from app.vault.writer import VaultWriter
    from tests.test_vault_filer import FakeAnthropic as FakeFiler

    (tmp_path / "Шведская стенка.md").write_text(
        "---\ntag: дом\n---\n\n## Подход\n\nвариант А\nвариант Б\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    writer = VaultWriter(index)
    pipeline = VaultPipeline(
        index, writer,
        Filer("", "haiku", client=FakeFiler({"actions": [{
            "action": "rewrite", "note": "Шведская стенка", "text": "убери вариант Б"}]})),
        # Body lines, without the frontmatter: 1 "## Подход", 2 "", 3 вариант А, 4 вариант Б.
        None, editor=Editor("", "sonnet", client=FakeAnthropic(
            _edits({"op": "delete", "at": 4}))))

    turn = await pipeline.handle("убери из стенки вариант Б")

    text = (tmp_path / "Шведская стенка.md").read_text(encoding="utf-8")
    assert "вариант А" in text and "вариант Б" not in text
    assert "## Подход" in text and "tag: дом" in text
    assert turn.writes and turn.writes[0].kind == "rewrite"


async def test_undoing_a_note_edit_is_exact_because_the_whole_file_is_kept(tmp_path):
    from app.vault.filer import Filer
    from app.vault.index import VaultIndex
    from app.vault.pipeline import VaultPipeline
    from app.vault.writer import VaultWriter
    from tests.test_vault_filer import FakeAnthropic as FakeFiler

    before = "---\ntag: дом\n---\n\n![[стенка.png]]\n\nвариант А\nвариант Б\n"
    (tmp_path / "Шведская стенка.md").write_text(before, encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    pipeline = VaultPipeline(
        index, VaultWriter(index),
        Filer("", "haiku", client=FakeFiler({"actions": [{
            "action": "rewrite", "note": "Шведская стенка", "text": "убери картинку"}]})),
        # Line 1 of the body is the embedded picture.
        None, editor=Editor("", "sonnet", client=FakeAnthropic(
            _edits({"op": "delete", "at": 1}))))

    turn = await pipeline.handle("убери из стенки картинку")
    assert "стенка.png" not in (tmp_path / "Шведская стенка.md").read_text(encoding="utf-8")

    await pipeline.undo(turn.undos)

    # Byte for byte, including the picture: the file's whole previous text is the undo record,
    # and the attachment itself was never deleted.
    assert (tmp_path / "Шведская стенка.md").read_text(encoding="utf-8") == before


# ---- the page a short follow-up means --------------------------------------------------------

async def test_the_page_this_chat_last_wrote_to_is_offered_to_the_interpreter(tmp_path, env):
    """«убери второй вариант» names no page. Without this the interpreter has nothing to
    resolve, so every edit would have to spell out where it applies."""
    from tests.test_orchestrator import CHAT, USER, make_bot

    bot = make_bot(tmp_path)
    page_id = next(t.id for t in bot.snapshot.targets if t.kind == "page")
    bot.store.update_event(bot.store.new_event(telegram_user_id=USER, chat_id=CHAT,
                                              kind="message"),
                           notion_page_id=page_id)

    assert bot.store.last_page(CHAT) == page_id
    name = next(t.name for t in bot.snapshot.targets if t.id == page_id)
    ctx = bot.orch._builder.build(bot.snapshot, recent=name)
    assert ctx.payload[RECENT_PAGE] == name


async def test_a_page_the_workspace_no_longer_has_is_simply_not_offered(tmp_path, env):
    from tests.test_orchestrator import CHAT, USER, make_bot

    bot = make_bot(tmp_path)
    bot.store.update_event(bot.store.new_event(telegram_user_id=USER, chat_id=CHAT,
                                              kind="message"),
                           notion_page_id="pg-gone")
    ctx = bot.orch._builder.build(bot.snapshot)
    assert RECENT_PAGE not in ctx.payload


def test_another_chats_page_is_not_offered(tmp_path, env):
    from tests.test_orchestrator import CHAT, USER, make_bot

    bot = make_bot(tmp_path)
    bot.store.update_event(bot.store.new_event(telegram_user_id=USER, chat_id=CHAT + 1,
                                              kind="message"),
                           notion_page_id="pg-theirs")
    assert bot.store.last_page(CHAT) is None


def test_a_page_from_long_ago_is_not_what_that_page_means(tmp_path, env):
    """Twenty messages later, "убери оттуда второй вариант" is not about the page from before
    them: resolving it there would edit somewhere the user has stopped thinking about."""
    from tests.test_orchestrator import CHAT, USER, make_bot

    bot = make_bot(tmp_path)
    bot.store.update_event(bot.store.new_event(telegram_user_id=USER, chat_id=CHAT,
                                              kind="message"),
                           notion_page_id="pg-old")
    for _ in range(25):
        bot.store.new_event(telegram_user_id=USER, chat_id=CHAT, kind="message")

    assert bot.store.last_page(CHAT) is None


# ---- a line added to a list is undone without taking its neighbours ---------------------------

async def test_undoing_an_added_line_does_not_delete_the_lines_after_it():
    """Found live, and it was already in production: Notion answers an insert *after* a block
    with the new block **and every sibling that follows it**. Those ids went straight into the
    undo record, so adding one line to a list and pressing Undo would have deleted the user's
    own lines below it."""
    from app.commands.models import AppendBlocks

    provider = FakeNotionProvider()
    # The list has to end its section for the executor to insert *into* it rather than at the
    # end of the page — which is exactly when Notion reports the following siblings.
    provider.page_blocks[PAGE] = [
        _text("heading_2", "Что купить", "h-1"),
        _text("bulleted_list_item", "анкеры", "b-1"),
        _text("bulleted_list_item", "маты", "b-2"),
        _text("heading_2", "Заметки", "h-2"),
        _text("paragraph", "и ещё абзац", "p-9"),
    ]
    executor = Executor(provider)
    result = await executor.run(AppendBlocks(
        page_id=PAGE, target_name="Идеи", page_title="Шведская стенка",
        paragraphs=["шурупы"], markdown=True, request="добавь шурупы"))

    # It really did insert into the list rather than at the end of the page: otherwise this
    # test would pass without exercising the bug at all.
    [call] = [c for c in provider.calls if c[0] == "append_blocks"]
    assert call[3] == "b-2"
    assert result.undo is not None
    # Exactly one block was created, whatever the API reported alongside it.
    assert len(result.undo.block_ids) == 1
    assert not ({"h-2", "p-9"} & set(result.undo.block_ids))

    provider.calls.clear()
    await executor.undo(result.undo)
    deleted = [c[1] for c in provider.calls if c[0] == "delete_block"]
    assert deleted == result.undo.block_ids


async def test_an_added_picture_is_counted_once_not_with_the_lines_below_it():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = _page()
    result = await _executor(provider, _edits({"op": "image", "at": 1,
                                                "url": "https://example.com/p.png"})).run(_cmd())

    assert result.added == 1
    assert result.undo is not None
    assert [len(e.block_ids) for e in result.undo.edits] == [1]
