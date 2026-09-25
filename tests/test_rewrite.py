"""Rewriting text that is already there, in both stores.

The request this exists for: «у меня страница про шведскую стенку, там много всего из разных
источников — оставь один подход и одни размеры». The Notion side used to refuse it outright
(`SEM_UNSUPPORTED_OP`: a page supports appends, not updates) and the Obsidian side could only
replace a section whose heading the filer happened to name.

The one rule that is not negotiable, and is pinned first below: an image, a file, a sub-page or
an embedded database is never archived by a rewrite, whatever the instruction says. That rule
lives in the code and not in a prompt, so no misread message can delete someone's photographs.
"""

from __future__ import annotations

import anthropic
import pytest

from app import texts
from app.commands.executor import Executor, Refused, UndoRecord
from app.commands.models import RewritePage
from app.conversation.reply import format_execution
from app.llm.health import Health
from app.llm.rewrite import RewriteError, Rewriter, unfence
from app.notion.to_markdown import is_rewritable, page_markdown, section_of
from tests.fakes import FakeNotionProvider

PAGE = "pg-wall"


def _text(kind: str, value: str, bid: str, **extra) -> dict:
    return {"id": bid, "type": kind, kind: {"rich_text": [{"plain_text": value}]}, **extra}


def _image(bid: str = "img-1") -> dict:
    return {"id": bid, "type": "image",
            "image": {"type": "external", "external": {"url": "https://x/photo.png"}}}


class FakeAnthropic:
    """Answers each call with the next item: a string becomes the model's text, an exception is
    raised instead."""

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.seen: list[dict] = []
        self.messages = self

    async def create(self, **kwargs):
        self.seen.append(kwargs)
        answer = self.answers.pop(0) if self.answers else ""
        if isinstance(answer, Exception):
            raise answer
        return type("Resp", (), {
            "content": [type("Block", (), {"type": "text", "text": answer})()],
            "stop_reason": "end_turn",
            "usage": type("U", (), {"input_tokens": 120, "output_tokens": 40})(),
        })()

    async def close(self) -> None:
        pass


def _executor(provider: FakeNotionProvider, *answers: object) -> Executor:
    rewriter = Rewriter("", "claude-sonnet-5", client=FakeAnthropic(*answers))
    return Executor(provider, rewriter=rewriter)


def _cmd(instruction: str = "оставь один подход") -> RewritePage:
    return RewritePage(page_id=PAGE, target_name="Идеи", page_title="Шведская стенка",
                       instruction=instruction)


# ---- the rule that matters most ----------------------------------------------------------


async def test_an_image_is_never_archived_by_a_rewrite():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = [
        _image("img-1"),
        _text("paragraph", "вариант А, 90 см", "p-1"),
        _text("paragraph", "вариант Б, 120 см", "p-2"),
        {"id": "sub-1", "type": "child_page", "child_page": {"title": "Смета"}},
        {"id": "db-1", "type": "child_database", "child_database": {"title": "Покупки"}},
    ]
    await _executor(provider, "вариант А, 90 см").run(_cmd())

    archived = [c[1] for c in provider.calls if c[0] == "delete_block"]
    assert archived == ["p-1", "p-2"]
    assert "img-1" not in archived and "sub-1" not in archived and "db-1" not in archived


async def test_a_block_with_children_is_left_alone_because_its_children_were_never_read():
    """A nested list could hold a picture two levels down. Nothing read it, so nothing may
    delete it."""
    assert not is_rewritable({"type": "bulleted_list_item", "has_children": True})
    assert is_rewritable({"type": "bulleted_list_item"})
    assert not is_rewritable(_image())
    assert not is_rewritable({"type": "table"})


async def test_a_page_with_no_text_at_all_is_refused_rather_than_emptied():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = [_image(), {"id": "t-1", "type": "table"}]
    with pytest.raises(Refused) as exc:
        await _executor(provider, "что-нибудь").run(_cmd())
    assert exc.value.code == "REWRITE_EMPTY"
    assert not [c for c in provider.calls if c[0] in ("delete_block", "append_blocks")]


# ---- the write itself ------------------------------------------------------------------------


async def test_the_new_text_is_written_before_the_old_text_is_archived():
    """A failure halfway must leave the page with too much, never with too little."""
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = [_text("paragraph", "старое", "p-1")]
    await _executor(provider, "новое").run(_cmd())

    kinds = [c[0] for c in provider.calls if c[0] in ("append_blocks", "delete_block")]
    assert kinds == ["append_blocks", "delete_block"]


async def test_the_text_lands_after_a_picture_the_page_opens_with():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = [_image("img-1"), _text("paragraph", "старое", "p-1")]
    await _executor(provider, "новое").run(_cmd())

    [append] = [c for c in provider.calls if c[0] == "append_blocks"]
    assert append[3] == "img-1"  # `after`: the page still opens with its photograph


async def test_the_model_sees_the_page_as_markdown_and_the_user_s_own_words():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = [
        _text("heading_2", "Подход", "h-1"),
        _text("bulleted_list_item", "90 см", "b-1"),
    ]
    client = FakeAnthropic("готово")
    await Executor(provider, rewriter=Rewriter("", "m", client=client)).run(
        _cmd("оставь один подход"))

    sent = client.seen[0]["messages"][0]["content"]
    assert "## Подход" in sent and "- 90 см" in sent
    assert "оставь один подход" in sent


# ---- undo ---------------------------------------------------------------------------------


async def test_undo_takes_the_new_text_away_and_brings_the_old_text_back():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = [_text("paragraph", "старое", "p-1")]
    executor = _executor(provider, "новое")
    result = await executor.run(_cmd())

    assert result.undo is not None and result.undo.kind == "rewrite"
    assert result.undo.markdown == ["старое"]

    provider.calls.clear()
    await executor.undo(result.undo)

    assert [c[1] for c in provider.calls if c[0] == "delete_block"] == result.undo.block_ids
    [append] = [c for c in provider.calls if c[0] == "append_blocks"]
    assert "старое" in str(append[2])


async def test_undo_survives_a_block_that_is_already_gone():
    """Undo can be pressed twice; the second press must still restore the text."""
    provider = FakeNotionProvider()
    executor = _executor(provider)
    provider.page_blocks[PAGE] = []
    rec = UndoRecord(kind="rewrite", page_id=PAGE, block_ids=["gone"], markdown=["старое"])
    from app.notion.errors import NotionError
    provider.delete_block = _raises(NotionError(404, "not_found", "gone"))

    await executor.undo(rec)  # must not raise

    assert any(c[0] == "append_blocks" for c in provider.calls)


def _raises(error: Exception):
    async def _fail(block_id):
        raise error
    return _fail


# ---- what the user is told ----------------------------------------------------------------


async def test_the_reply_says_how_much_text_there_was_and_how_much_there_is_now():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = [_text("paragraph", f"строка {i}", f"p-{i}")
                                  for i in range(10)]
    result = await _executor(provider, "одна строка").run(_cmd())
    text = format_execution(result, target_url=None)

    assert texts.DONE_REWRITE.format(target_name="Идеи", item_title="Шведская стенка") in text
    assert "19" in text and "1" in text  # ten paragraphs with blank lines between them
    assert "одна строка" in text  # the preview


async def test_a_rewrite_that_all_but_empties_the_page_says_so():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = [_text("paragraph", f"строка {i}", f"p-{i}")
                                  for i in range(20)]
    result = await _executor(provider, "итог").run(_cmd())

    assert texts.REWRITE_SHRANK.format(undo=texts.BTN_UNDO) in format_execution(
        result, target_url=None)


async def test_the_blocks_left_alone_are_counted_in_the_reply():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = [_image("a"), _image("b"), _text("paragraph", "текст", "p-1")]
    result = await _executor(provider, "другой текст").run(_cmd())

    assert texts.REWRITE_KEPT.format(n=2) in format_execution(result, target_url=None)


# ---- the model's answer ----------------------------------------------------------------------


async def test_an_empty_answer_changes_nothing():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = [_text("paragraph", "старое", "p-1")]
    with pytest.raises(Refused) as exc:
        await _executor(provider, "   ").run(_cmd())
    assert exc.value.code == "REWRITE_FAILED"
    assert not [c for c in provider.calls if c[0] == "delete_block"]


async def test_a_claude_failure_leaves_the_page_untouched_and_names_the_cause():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = [_text("paragraph", "старое", "p-1")]
    error = anthropic.APIError("boom", request=None,  # type: ignore[arg-type]
                               body={"error": {"message": "Your credit balance is too low"}})
    error.status_code = 400  # type: ignore[attr-defined]
    health = Health()
    rewriter = Rewriter("", "m", client=FakeAnthropic(error), health=health)

    with pytest.raises(Refused):
        await Executor(provider, rewriter=rewriter).run(_cmd())

    assert not [c for c in provider.calls if c[0] == "delete_block"]
    assert health.reason == "credit"  # and the reply will say so, once


async def test_no_rewriter_is_a_plain_refusal_not_a_crash():
    with pytest.raises(Refused) as exc:
        await Executor(FakeNotionProvider()).run(_cmd())
    assert exc.value.code == "REWRITE_UNAVAILABLE"


@pytest.mark.parametrize(("answer", "expected"), [
    ("```markdown\n# A\n```", "# A"),
    ("```\n# A\n```", "# A"),
    ("# A", "# A"),
    # A page that really is one code block keeps its fence.
    ("```python\nx = 1\n```", "```python\nx = 1\n```"),
])
def test_a_fence_the_model_wrapped_everything_in_is_taken_off(answer, expected):
    assert unfence(answer) == expected


async def test_an_answer_cut_off_mid_page_is_refused():
    """Half a page is worse than none: the tail would simply disappear."""
    class Truncating(FakeAnthropic):
        async def create(self, **kwargs):
            resp = await super().create(**kwargs)
            resp.stop_reason = "max_tokens"
            return resp

    rewriter = Rewriter("", "m", client=Truncating("начало текста"))
    with pytest.raises(RewriteError):
        await rewriter.rewrite("что-то длинное", "сократи")


# ---- reading a page -------------------------------------------------------------------------


def test_a_section_is_the_heading_and_everything_under_it():
    blocks = [_text("heading_2", "Подход", "h1"), _text("paragraph", "текст", "p1"),
              _text("heading_2", "Размеры", "h2"), _text("paragraph", "90 см", "p2")]
    assert section_of(blocks, "Подход") == (0, 2)
    assert section_of(blocks, "Размеры") == (2, 4)
    assert section_of(blocks, "Чего нет") is None


def test_a_page_becomes_plain_markdown_with_no_vault_links():
    blocks = [_text("heading_1", "План", "h1"), _text("to_do", "купить доску", "t1"),
              _text("paragraph", "дальше", "p1")]
    assert page_markdown(blocks) == "# План\n\n- [ ] купить доску\n\nдальше"


# ---- the Obsidian side ------------------------------------------------------------------------

def _vault(tmp_path, text: str = ""):
    """A vault with one note, its index, and a writer over it."""
    from app.vault.index import VaultIndex
    from app.vault.writer import VaultWriter

    (tmp_path / "Шведская стенка.md").write_text(
        text or "---\ntag: дом\n---\n\n## Подход\n\nвариант А\n\n## Размеры\n\n90 см\n",
        encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    return index, VaultWriter(index)


def _pipeline(index, writer, filer_answer: dict, *rewrites: object):
    from app.vault.filer import Filer
    from app.vault.pipeline import VaultPipeline
    from tests.test_vault_filer import FakeAnthropic as FakeFiler

    return VaultPipeline(index, writer, Filer("", "haiku", client=FakeFiler(filer_answer)),
                         None, rewriter=Rewriter("", "sonnet", client=FakeAnthropic(*rewrites)))


async def test_a_note_is_rewritten_and_keeps_its_frontmatter(tmp_path):
    index, writer = _vault(tmp_path)
    pipeline = _pipeline(index, writer, {"actions": [{
        "action": "rewrite", "note": "Шведская стенка", "text": "оставь один подход"}]},
        "## Подход\n\nвариант А, 90 см\n")

    turn = await pipeline.handle("перепиши стенку, оставь один подход")

    text = (tmp_path / "Шведская стенка.md").read_text(encoding="utf-8")
    assert "tag: дом" in text  # the properties are the note's, not the text's
    assert "вариант А, 90 см" in text and "## Размеры" not in text
    assert "Obsidian —" in turn.reply_line()


async def test_only_the_named_section_is_rewritten(tmp_path):
    index, writer = _vault(tmp_path)
    pipeline = _pipeline(index, writer, {"actions": [{
        "action": "rewrite", "note": "Шведская стенка", "heading": "Размеры",
        "text": "в миллиметрах"}]}, "900 мм\n")

    await pipeline.handle("перепиши размеры в миллиметрах")

    text = (tmp_path / "Шведская стенка.md").read_text(encoding="utf-8")
    assert "900 мм" in text
    assert "## Подход" in text and "вариант А" in text  # the rest is untouched


async def test_the_model_is_shown_only_the_section_it_is_asked_to_rewrite(tmp_path):
    index, writer = _vault(tmp_path)
    client = FakeAnthropic("900 мм")
    from app.vault.filer import Filer
    from app.vault.pipeline import VaultPipeline
    from tests.test_vault_filer import FakeAnthropic as FakeFiler
    pipeline = VaultPipeline(
        index, writer, Filer("", "haiku", client=FakeFiler({"actions": [{
            "action": "rewrite", "note": "Шведская стенка", "heading": "Размеры",
            "text": "в миллиметрах"}]})),
        None, rewriter=Rewriter("", "sonnet", client=client))

    await pipeline.handle("перепиши размеры")

    sent = client.seen[0]["messages"][0]["content"]
    assert "90 см" in sent and "вариант А" not in sent


async def test_the_previous_version_is_kept_in_trash_beyond_the_undo_window(tmp_path):
    index, writer = _vault(tmp_path)
    pipeline = _pipeline(index, writer, {"actions": [{
        "action": "rewrite", "note": "Шведская стенка", "text": "сократи"}]}, "коротко\n")

    turn = await pipeline.handle("сократи стенку")
    await pipeline.undo(turn.undos)

    trashed = list((tmp_path / ".trash").glob("*.md"))
    assert trashed and "вариант А" in trashed[0].read_text(encoding="utf-8")
    # and undo itself put the note back as it was
    assert "вариант А" in (tmp_path / "Шведская стенка.md").read_text(encoding="utf-8")


async def test_a_rewrite_of_a_note_that_is_not_there_writes_nothing(tmp_path):
    index, writer = _vault(tmp_path)
    pipeline = _pipeline(index, writer, {"actions": [{
        "action": "rewrite", "note": "Которой нет", "text": "сократи"}]}, "коротко")

    turn = await pipeline.handle("сократи то, чего нет")

    # The check turns an unknown note into an inbox line: the words are kept, nothing is lost.
    assert [w.kind for w in turn.writes] == ["inbox"]


async def test_a_failed_rewrite_leaves_the_note_exactly_as_it_was(tmp_path):
    index, writer = _vault(tmp_path)
    before = (tmp_path / "Шведская стенка.md").read_text(encoding="utf-8")
    error = anthropic.APIError("boom", request=None,  # type: ignore[arg-type]
                               body={"error": {"message": "Your credit balance is too low"}})
    error.status_code = 400  # type: ignore[attr-defined]
    pipeline = _pipeline(index, writer, {"actions": [{
        "action": "rewrite", "note": "Шведская стенка", "text": "сократи"}]}, error)

    turn = await pipeline.handle("сократи стенку")

    assert (tmp_path / "Шведская стенка.md").read_text(encoding="utf-8") == before
    assert turn.writes == []
    assert texts.LLM_DOWN_SHORT["credit"] in turn.reply_line()
