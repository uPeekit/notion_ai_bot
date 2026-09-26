"""Set the grocery page up in an existing vault. Dry run unless asked to write.

Four things, all idempotent — running it twice changes nothing:

1. create `Продукты.md` and put the consumables on it, ticked (in stock);
2. take those lines out of the task file, leaving the one-off purchases alone;
3. add the grocery block to the home page;
4. add `filename does not include Продукты` to the home page's undated-tasks query, so a
   product waiting to be bought does not also show up there as a task.

Which lines are consumables is not guessed: `--products` names them, and everything that looks
like a purchase is printed either way so the list can be corrected. A tool that decides this with
a model would be a tool nobody can check.

    python tools/groceries_setup.py --vault C:\\data\\obsidian
    python tools/groceries_setup.py --vault C:\\data\\obsidian --apply
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import texts  # noqa: E402
from app.vault import groceries  # noqa: E402

# What moves, unless --products says otherwise: the consumables in this vault's task file.
DEFAULT_PRODUCTS = ("бальзам для волос", "шампунь", "яйца", "бекон", "мусорные пакеты", "тортик")
# Lines that look like a purchase, so the tool can show everything it did *not* move.
_PURCHASE = re.compile("|".join(texts.VAULT_BUY_WORDS), re.IGNORECASE)
_TASK = re.compile(r"^\s*[-*+]\s+\[[^\]]\]\s*(.*)$")

HOME_BLOCK = f"""## {texts.VAULT_GROCERIES_NOTE}

```tasks
not done
filename includes {texts.VAULT_GROCERIES_NOTE}
hide backlink
hide edit button
hide postpone button
```
"""
# The home page query that lists undated tasks; groceries have no dates, so without this line
# every product waiting to be bought appears there too.
EXCLUDE = f"filename does not include {texts.VAULT_GROCERIES_NOTE}"
GUIDE_LINE = (
    f"- Продукты и расходники для дома — на странице «{texts.VAULT_GROCERIES_NOTE}». "
    "Это реестр: у каждого продукта одна строка навсегда, галочка снята — надо купить. "
    "Ничего не создаётся и не архивируется. Техника, одежда, подарки и покупки со сроком — "
    "обычные задачи."
)


def _matches(text: str, products: tuple[str, ...]) -> str | None:
    """Which named product this task line is about, if any."""
    name = groceries.bare(text)
    return next((p for p in products if groceries.same(name, p)), None)


def split_tasks(tasks: str, products: tuple[str, ...]) -> tuple[str, list[str], list[str]]:
    """The task file without the consumables, the products taken out, and the purchases left."""
    kept: list[str] = []
    moved: list[str] = []
    left: list[str] = []
    for line in tasks.split("\n"):
        m = _TASK.match(line)
        body = m.group(1) if m else ""
        product = _matches(body, products) if body else None
        if product is not None:
            moved.append(groceries.bare(body))
            continue
        if body and _PURCHASE.search(body):
            left.append(body.strip())
        kept.append(line)
    return "\n".join(kept), moved, left


def home(text: str) -> str:
    """The home page with the grocery block added and the undated query narrowed."""
    out = text
    if f"filename includes {texts.VAULT_GROCERIES_NOTE}" not in out:
        # After the task queries and before the first embedded view — but at the *heading* that
        # owns that view, never between the heading and the embed, which would orphan it.
        embed = out.find("\n![[")
        at = out.rfind("\n## ", 0, embed) + 1 if embed != -1 else -1
        if at <= 0:
            at = len(out)
        out = f"{out[:at].rstrip()}\n\n{HOME_BLOCK}\n{out[at:].lstrip()}"
    if EXCLUDE not in out:
        out = re.sub(r"(\nno due date\n)", f"\\1{EXCLUDE}\n", out, count=1)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--products", default=",".join(DEFAULT_PRODUCTS),
                        help="comma-separated names to move out of the task file")
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()

    root: Path = args.vault
    products = tuple(p.strip() for p in args.products.split(",") if p.strip())
    page = root / f"{texts.VAULT_GROCERIES_NOTE}.md"
    tasks_file = root / f"{texts.VAULT_TASKS_NOTE}.md"
    home_file = root / f"{texts.VAULT_HOME_NOTE}.md"
    guide_file = root / "_bot.md"
    for needed in (tasks_file, home_file):
        if not needed.is_file():
            print(f"not found: {needed}")
            return 2

    tasks = tasks_file.read_text(encoding="utf-8")
    new_tasks, moved, left = split_tasks(tasks, products)
    current = page.read_text(encoding="utf-8") if page.is_file() else groceries.note()
    new_page, added = groceries.apply(current, moved, done=True)
    new_home = home(home_file.read_text(encoding="utf-8"))
    guide = guide_file.read_text(encoding="utf-8") if guide_file.is_file() else ""
    new_guide = guide if GUIDE_LINE in guide else _with_guide(guide)

    print(f"{page.name}: {len(groceries.read(new_page))} products "
          f"({len(added)} added from the task file)")
    for name in added:
        print(f"    + {name}")
    extra = len(moved) - len(added)
    print(f"{tasks_file.name}: {len(moved)} lines removed"
          + (f" ({extra} of them duplicates of a product already moved)" if extra > 0
             else ""))
    if left:
        print("  purchases left as tasks (move them by hand if any is a consumable):")
        for line in left:
            print(f"    · {line}")
    old_home = home_file.read_text(encoding="utf-8")
    print(f"{home_file.name}: "
          f"{'grocery block added' if new_home != old_home else 'unchanged'}")
    print(f"{guide_file.name}: "
          f"{'a line about groceries added' if new_guide != guide else 'unchanged'}")

    if not args.apply:
        print("\ndry run — nothing written. Add --apply.")
        return 0
    page.write_text(new_page, encoding="utf-8", newline="\n")
    tasks_file.write_text(new_tasks, encoding="utf-8", newline="\n")
    home_file.write_text(new_home, encoding="utf-8", newline="\n")
    if new_guide != guide:
        guide_file.write_text(new_guide, encoding="utf-8", newline="\n")
    print("\nwritten.")
    return 0


def _with_guide(guide: str) -> str:
    """The grocery rule added to the user's own guide note, next to the other filing rules."""
    if not guide.strip():
        return f"{GUIDE_LINE}\n"
    at = guide.find("\n## ")
    if at == -1:
        return f"{guide.rstrip()}\n{GUIDE_LINE}\n"
    return f"{guide[:at].rstrip()}\n{GUIDE_LINE}\n{guide[at:]}"


if __name__ == "__main__":
    raise SystemExit(main())
