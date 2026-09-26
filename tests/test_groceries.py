"""The grocery page: one permanent line per product, ticked when it is in stock.

The problem it solves is visible in the user's own vault: «Купить яйца», «Купить бекон» and
«Купить мусорные пакеты» were one-off tasks in the task file, and one recurring task had already
left three identical ticked copies in the archive note. A shopping list built that way grows
forever.

So: the page *is* the registry. Nothing is created and nothing is archived — a message only
changes which lines are ticked, which is exactly the gesture the user makes by hand in Obsidian.
The two rules that keep it that way are pinned first below: a grocery line never carries a date
or a repeat rule, and an ambiguous name adds a line rather than unticking a guess.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from app import texts
from app.vault import groceries
from app.vault.filer import Filer, check, context
from app.vault.index import VaultIndex
from app.vault.pipeline import VaultPipeline
from app.vault.writer import VaultAction, VaultWriter
from tests.test_vault_filer import FakeAnthropic

NOW = datetime(2026, 9, 30, 18, 0)

PAGE = """# Продукты

Отмеченные — есть дома.

- [x] бекон
- [x] молоко
- [x] мусорные пакеты
- [x] шампунь
- [x] яйца
"""


@pytest.fixture
def vault(tmp_path):
    (tmp_path / f"{texts.VAULT_GROCERIES_NOTE}.md").write_text(PAGE, encoding="utf-8")
    (tmp_path / f"{texts.VAULT_TASKS_NOTE}.md").write_text("## дом\n\n- [ ] счета #home\n",
                                                            encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    return index


def page(index: VaultIndex) -> str:
    return index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")


def pipeline(index: VaultIndex, *answers) -> VaultPipeline:
    return VaultPipeline(index, VaultWriter(index, now=lambda: NOW),
                         Filer("", "haiku", client=FakeAnthropic(*answers)), None,
                         now=lambda: NOW)


# ---- the two rules that keep the page from growing -------------------------------------------


def test_nothing_is_ever_created_or_archived_only_ticked(vault):
    """The whole point: the file has the same lines before and after a shopping trip."""
    before = page(vault)
    text, _ = groceries.apply(before, ["яйца", "молоко"], done=False)
    after, _ = groceries.apply(text, ["яйца", "молоко"], done=True)

    assert after == before
    assert len(groceries.read(after)) == len(groceries.read(before)) == 5


def test_a_grocery_line_never_carries_a_date_or_a_repeat(vault):
    """A repeating tick box copies itself on completion — that is what left three «зубы» in the
    archive note, and it is exactly what must not happen here."""
    actions = check([{"action": "grocery", "body": ["молоко"], "due": "2026-10-01",
                      "repeat": "every week"}], vault, "надо молоко")

    assert [a.action for a in actions] == ["grocery"]
    assert actions[0].due == "" and actions[0].repeat == ""


def test_an_ambiguous_name_adds_a_line_instead_of_unticking_a_guess():
    """Unticking the wrong product puts the wrong thing in the shop; a near-duplicate line is
    something the user can merge in two seconds."""
    lines = groceries.read("- [x] сок яблочный\n- [x] сок апельсиновый\n")
    assert groceries.match("сок", lines) is None

    text, changed = groceries.apply("- [x] сок яблочный\n- [x] сок апельсиновый\n",
                                    ["сок"], done=False)
    assert changed == ["сок"]
    assert "- [ ] сок" in text
    assert "- [x] сок яблочный" in text and "- [x] сок апельсиновый" in text


# ---- matching --------------------------------------------------------------------------------


@pytest.mark.parametrize(("asked", "expected"), [
    ("молоко", "молоко"),
    ("молока", "молоко"),          # any Russian ending
    ("2 л молока", "молоко"),      # an amount is not part of the name
    ("купить молоко", "молоко"),   # nor is the verb
    ("Молоко", "молоко"),          # nor is the case
    ("мусорные пакеты", "мусорные пакеты"),
    ("пакеты мусорные", "мусорные пакеты"),  # word order
])
def test_a_product_is_found_however_it_was_asked_for(asked, expected):
    found = groceries.match(asked, groceries.read(PAGE))
    assert found is not None and found.name == expected


def test_a_more_specific_product_is_not_mistaken_for_a_plainer_one():
    lines = groceries.read("- [x] молоко кокосовое\n")
    assert groceries.match("молоко", lines) is None


@pytest.mark.parametrize(("raw", "expected"), [
    ("Купить 2 л молока ✅ 2026-09-30", "молока"),
    ("яйца 🔁 every week 📅 2026-09-13", "яйца"),
    ("1 кг гречки", "гречки"),
    # The abbreviation for grams is also the first letter of this product: a unit is only
    # stripped straight after a number.
    ("гречка", "гречка"),
    ("пачка соли", "соли"),
    ("10 шт яиц", "яиц"),
])
def test_the_name_is_what_is_left_after_the_words_that_are_not_it(raw, expected):
    assert groceries.bare(raw) == expected


# ---- writing ---------------------------------------------------------------------------------


def test_needed_products_are_unticked_and_the_rest_left_alone(vault):
    writer = VaultWriter(vault, now=lambda: NOW)
    write = writer.run(VaultAction(action="grocery", body=["яйца", "2 л молока"]))

    assert write.kind == "grocery"
    assert groceries.needed(page(vault)) == ["молоко", "яйца"]
    assert "яйца, молоко" in write.what or "молоко, яйца" in write.what


def test_buying_them_ticks_them_back(vault):
    writer = VaultWriter(vault, now=lambda: NOW)
    writer.run(VaultAction(action="grocery", body=["яйца", "молоко"]))
    write = writer.run(VaultAction(action="grocery", body=["яйца", "молоко"], done=True))

    assert write.kind == "grocery_done"
    assert groceries.needed(page(vault)) == []
    assert page(vault) == PAGE  # byte for byte


def test_a_new_product_is_learned_in_alphabetical_order(vault):
    VaultWriter(vault, now=lambda: NOW).run(VaultAction(action="grocery", body=["кускус"]))

    names = [ln.name for ln in groceries.read(page(vault))]
    assert names == ["бекон", "кускус", "молоко", "мусорные пакеты", "шампунь", "яйца"]
    assert groceries.needed(page(vault)) == ["кускус"]


def test_buying_something_new_registers_it_without_putting_it_on_the_list(vault):
    VaultWriter(vault, now=lambda: NOW).run(
        VaultAction(action="grocery", body=["кускус"], done=True))

    assert "кускус" in [ln.name for ln in groceries.read(page(vault))]
    assert groceries.needed(page(vault)) == []


def test_asking_twice_changes_nothing_the_second_time(vault):
    writer = VaultWriter(vault, now=lambda: NOW)
    writer.run(VaultAction(action="grocery", body=["яйца"]))
    after_first = page(vault)
    write = writer.run(VaultAction(action="grocery", body=["яйца"]))

    assert write.undo is None  # nothing written, so nothing to undo
    assert page(vault) == after_first


def test_the_page_is_created_on_first_use(tmp_path):
    index = VaultIndex(tmp_path)
    index.refresh()
    VaultWriter(index, now=lambda: NOW).run(VaultAction(action="grocery", body=["молоко"]))

    text = (tmp_path / f"{texts.VAULT_GROCERIES_NOTE}.md").read_text(encoding="utf-8")
    assert texts.VAULT_GROCERIES_INTRO in text
    assert groceries.needed(text) == ["молоко"]


def test_undo_puts_the_page_back(vault):
    writer = VaultWriter(vault, now=lambda: NOW)
    before = page(vault)
    write = writer.run(VaultAction(action="grocery", body=["яйца", "кускус"]))
    assert write.undo is not None

    writer.undo(write.undo)
    assert page(vault) == before


# ---- the page decides what a grocery is ------------------------------------------------------


def test_a_product_already_on_the_page_is_never_misfiled(vault):
    """The rule the feature rests on: a lookup, not a judgement. The model answered "task" and
    the page overrules it."""
    actions = check([{"action": "task", "text": "Купить молоко", "heading": "дом"}],
                    vault, "купить молоко")

    assert [a.action for a in actions] == ["grocery"]
    assert actions[0].body == ["молоко"] and actions[0].done is False


def test_ticking_a_task_off_becomes_ticking_the_product_back(vault):
    actions = check([{"action": "update", "note": texts.VAULT_TASKS_NOTE,
                      "task": "Купить молоко", "done": True}], vault, "купил молоко")

    assert [a.action for a in actions] == ["grocery"]
    assert actions[0].body == ["молоко"] and actions[0].done is True


def test_a_dated_purchase_stays_a_task_even_for_food(vault):
    """«торт к субботе» is an errand with a deadline. A grocery line cannot hold a date, so
    turning this into one would lose the date."""
    actions = check([{"action": "task", "text": "Купить тортик", "due": "2026-10-03",
                      "heading": "дом"}], vault, "купить тортик к субботе")

    assert [a.action for a in actions] == ["task"]
    assert actions[0].due == "2026-10-03"


def test_something_the_page_never_heard_of_is_left_to_the_model(vault):
    actions = check([{"action": "task", "text": "Купить велосипед", "heading": "дом"}],
                    vault, "купить велосипед")

    assert [a.action for a in actions] == ["task"]


def test_the_model_is_shown_what_the_page_already_knows(vault):
    ctx = context(vault, "надо молока", NOW)
    payload = json.loads(ctx.json())

    assert "молоко" in payload["продукты_в_реестре"]
    assert payload["страница_продуктов"] == texts.VAULT_GROCERIES_NOTE


# ---- end to end through the pipeline ----------------------------------------------------------


async def test_a_message_unticks_and_the_reply_names_the_products(vault):
    turn = await pipeline(vault, {"actions": [
        {"action": "grocery", "body": ["яйца", "хлеб"]}]}).handle("надо яйца и хлеб")

    assert groceries.needed(page(vault)) == ["хлеб", "яйца"]
    line = turn.reply_line()
    assert "Obsidian —" in line and "яйца" in line and "хлеб" in line


async def test_groceries_are_not_things_to_do_today(vault):
    """They belong on the home page, where the check boxes work — not in "what should I start
    with" next to the dentist."""
    from app.vault import agenda

    VaultWriter(vault, now=lambda: NOW).run(VaultAction(action="grocery", body=["яйца"]))
    vault.refresh()

    assert not [i for i in agenda.open_tasks(vault) if "яйца" in i.text]
    assert "яйца" in agenda.groceries_line(vault)


def test_the_digest_line_appears_only_when_something_is_needed(vault):
    from app.vault import agenda

    assert agenda.groceries_line(vault) == ""
    VaultWriter(vault, now=lambda: NOW).run(VaultAction(action="grocery", body=["яйца"]))
    vault.refresh()
    assert agenda.groceries_line(vault) == texts.VAULT_GROCERIES_DIGEST.format(n=1, items="яйца")


# ---- the one-off setup of an existing vault ----------------------------------------------------

HOME = """## Без срока — по областям

```tasks
not done
no due date
group by tags
```

## Читаю

![[Книги.base#Читаю]]
"""

TASKS = """## дом

- [ ] Купить яйца #home
- [ ] Купить шампунь #home
- [ ] шампунь #personal
- [ ] Купить Качельки #personal
- [ ] платить счета дом #home 🔁 every month 📅 2026-09-15
"""


@pytest.fixture
def setup_vault(tmp_path):
    (tmp_path / f"{texts.VAULT_TASKS_NOTE}.md").write_text(TASKS, encoding="utf-8")
    (tmp_path / f"{texts.VAULT_HOME_NOTE}.md").write_text(HOME, encoding="utf-8")
    (tmp_path / "_bot.md").write_text("# Как бот раскладывает\n\n- Задачи — в файл задач.\n",
                                       encoding="utf-8")
    return tmp_path


def test_setup_moves_consumables_and_leaves_one_off_purchases(setup_vault):
    from tools import groceries_setup

    kept, moved, left = groceries_setup.split_tasks(TASKS, ("яйца", "шампунь"))

    assert sorted(moved) == ["шампунь", "шампунь", "яйца"]  # two shampoo lines, one product
    assert "Купить Качельки" in kept and "платить счета" in kept
    assert left == ["Купить Качельки #personal"]


def test_setup_collapses_two_lines_for_one_product(setup_vault):
    from tools import groceries_setup

    _, moved, _ = groceries_setup.split_tasks(TASKS, ("яйца", "шампунь"))
    text, added = groceries.apply(groceries.note(), moved, done=True)

    # Reported in the order they were asked for; the page itself is alphabetical.
    assert sorted(added) == ["шампунь", "яйца"]
    assert [ln.name for ln in groceries.read(text)] == ["шампунь", "яйца"]


def test_setup_keeps_an_embedded_view_with_its_own_heading(setup_vault):
    """The block goes after the task queries and before «## Читаю» — never between that heading
    and the embed underneath it, which would leave the heading orphaned."""
    from tools import groceries_setup

    out = groceries_setup.home(HOME)

    assert out.index("## Продукты") < out.index("## Читаю")
    assert out.index("## Читаю") < out.index("![[Книги.base")
    assert f"filename includes {texts.VAULT_GROCERIES_NOTE}" in out


def test_setup_narrows_the_undated_query_so_groceries_do_not_show_up_twice(setup_vault):
    from tools import groceries_setup

    out = groceries_setup.home(HOME)
    block = out[out.index("no due date"):]
    assert block.startswith(f"no due date\n{groceries_setup.EXCLUDE}")


def test_setup_is_idempotent(setup_vault):
    from tools import groceries_setup

    once = groceries_setup.home(HOME)
    assert groceries_setup.home(once) == once


# ---- model slips the gate has to absorb (all three seen on live runs) --------------------------

def test_a_grocery_action_that_names_no_product_takes_it_from_the_message(vault):
    """«купил яйца» came back with done=true and an empty body: no product named anywhere in the
    answer. The message names it, so that is where the name comes from."""
    actions = check([{"action": "grocery", "body": [], "text": "", "done": True}],
                    vault, "купил яйца")

    assert [(a.action, a.body, a.done) for a in actions] == [("grocery", ["яйца"], True)]


def test_saying_something_was_bought_is_never_a_request_for_the_list(vault):
    """The model kept putting scope=list on «купил яйца» and the bot answered with the whole
    shopping list instead of ticking the eggs off. Listing is answering a question; done=true
    says this is not one."""
    actions = check([{"action": "grocery", "body": [], "done": True, "scope": "list"}],
                    vault, "купил яйца")

    assert [(a.action, a.body, a.scope) for a in actions] == [("grocery", ["яйца"], "")]


def test_asking_what_to_buy_writes_nothing(vault):
    actions = check([{"action": "grocery", "body": [], "scope": "list"}],
                    vault, "что надо купить?")

    assert [(a.action, a.scope, a.body) for a in actions] == [("grocery", "list", [])]


async def test_the_answer_to_what_to_buy_is_the_list_and_nothing_is_written(vault):
    before = page(vault)
    turn = await pipeline(vault, {"actions": [
        {"action": "grocery", "body": [], "scope": "list"}]}).handle("что надо купить?")

    assert turn.writes == []
    assert page(vault) == before
    assert turn.reply_line() == texts.VAULT_GROCERIES_EMPTY

    VaultWriter(vault, now=lambda: NOW).run(VaultAction(action="grocery", body=["яйца"]))
    vault.refresh()
    turn = await pipeline(vault, {"actions": [
        {"action": "grocery", "body": [], "scope": "list"}]}).handle("что надо купить?")
    assert turn.reply_line() == texts.VAULT_GROCERIES_LIST.format(items="яйца")


def test_an_update_of_a_task_that_is_not_there_creates_it(vault):
    """«купить тортик к субботе» came back as an update of a task the file does not have — the
    cake had been moved to the grocery page. The writer had nothing to change and the message
    went to the inbox: a clear request with a deadline, lost."""
    actions = check([{"action": "update", "note": texts.VAULT_TASKS_NOTE,
                      "task": "Купить тортик к субботе #personal", "due": "2026-10-03",
                      "done": False}], vault, "купить тортик к субботе")

    assert [a.action for a in actions] == ["task"]
    assert actions[0].due == "2026-10-03" and actions[0].tags == ["personal"]
    assert "#" not in actions[0].text


def test_marking_a_task_done_when_it_is_not_there_never_creates_an_open_one(vault):
    """The other half of that rule: a «сделано» for something missing must not add work."""
    actions = check([{"action": "update", "note": texts.VAULT_TASKS_NOTE,
                      "task": "прибить полку", "done": True}], vault, "прибил полку")

    assert [a.action for a in actions] != ["task"]
