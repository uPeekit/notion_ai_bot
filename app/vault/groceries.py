"""The grocery page: a registry, not a to-do list.

Every product the user buys has one permanent line. Ticked means it is in stock; unticked means
buy it. So the page never grows except when a genuinely new product appears, and there is no
"completed" pile to clean up — which is what the task file does instead, where one recurring
tick left three identical corpses in the archive note.

The bot's whole job here is to untick a line that already exists, or tick it back when the thing
is bought. That is the same gesture the user makes by hand in Obsidian, on the same lines, so
there is no state anywhere else to keep in sync — and no tag either: the file *is* the identity,
which is what the home page's `filename includes` query keys off.

Two rules live in this module and nowhere else, because getting them wrong is how the page would
start growing again:

* a grocery line never carries a due date or a repeat rule — a recurring tick spawns a copy;
* matching is by name, and an ambiguous name adds a line instead of unticking a guess.

Pure and synchronous: it takes the note's text and returns new text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app import texts
from app.vault.index import related, words

# A tick box, with whatever the Tasks plugin has stamped on it.
_LINE = re.compile(r"^(\s*[-*+]\s+)\[([^\]])\]\s*(.*)$")
# Everything the Tasks plugin may stamp on a line \u2014 a done date, a due date, a repeat
# rule \u2014 cut from the first of those symbols to the end of it. A repeat rule runs to several
# words ("every week on Monday, Friday"), so taking the tail is the only reliable cut, and a
# product name never contains one of these symbols.
_STAMPS = re.compile(r"\s*[\u2705\u2795\U0001F6EB\u23F3\U0001F4C5\U0001F501\u274C].*$")
# Tags a task line carries (#home, #personal). Never part of a product name, and leaving them
# in stops a line taken from the task file from matching the same product on the page.
_TAGS = re.compile(r"(?:(?<=\s)|^)#[^\W\d_][\w/-]*", re.UNICODE)
_UNITS = "|".join(re.escape(u) for u in texts.VAULT_GROCERY_UNITS)
_PACKS = "|".join(re.escape(u) + r"\w*" for u in texts.VAULT_GROCERY_PACKS)
# A leading amount: a number with or without a unit, or a container word. A unit only
# counts straight after a number, because on its own it would swallow the product itself —
# the abbreviation for grams is also the first letter of several groceries.
_AMOUNT = re.compile(rf"^\s*(?:\d+[.,]?\d*\s*(?:{_UNITS})?|(?:{_PACKS}))\s+", re.IGNORECASE)
_BUY = re.compile(rf"^\s*(?:{'|'.join(re.escape(w) for w in texts.VAULT_BUY_WORDS)})\s+",
                  re.IGNORECASE)
DONE = "x"
OPEN = " "
MAX_NAME = 80


@dataclass(frozen=True)
class Line:
    """One product on the page."""

    at: int  # index into the note's lines
    prefix: str  # the bullet and indent, kept exactly as it was
    name: str
    done: bool


def bare(name: str) -> str:
    """A product's name without the words that are not part of it: the buying verb, an amount,
    and anything the Tasks plugin stamped on the line."""
    out = _STAMPS.sub("", name)
    out = _TAGS.sub("", out)
    out = _BUY.sub("", out)
    out = _AMOUNT.sub("", out)
    return re.sub(r"\s+", " ", out).strip(" .,;:")[:MAX_NAME]


def read(text: str) -> list[Line]:
    """Every product line on the page, in file order."""
    out: list[Line] = []
    for i, raw in enumerate(text.split("\n")):
        m = _LINE.match(raw)
        if m is None:
            continue
        name = bare(m.group(3))
        if name:
            out.append(Line(at=i, prefix=m.group(1), name=name,
                            done=m.group(2).strip().casefold() == DONE))
    return out


def same(one: str, two: str) -> bool:
    """Are these the same product? Exact first, then word by word in both directions.

    Both directions on purpose: plain milk must not match coconut milk, or asking for the
    first would untick the second whenever the page happens not to have the first."""
    a, b = bare(one).casefold(), bare(two).casefold()
    if a == b:
        return True
    aw, bw = words(a), words(b)
    if not aw or not bw:
        return False
    return (all(any(related(x, y) for y in bw) for x in aw)
            and all(any(related(y, x) for x in aw) for y in bw))


def match(name: str, lines: list[Line]) -> Line | None:
    """The one line this name means, or None.

    None when nothing matches *and* when several do: unticking a guess puts the wrong thing on
    the shopping list, while adding a line only risks a near-duplicate the user can merge."""
    wanted = bare(name).casefold()
    exact = [ln for ln in lines if ln.name.casefold() == wanted]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    close = [ln for ln in lines if same(name, ln.name)]
    return close[0] if len(close) == 1 else None


def _sort_key(name: str) -> str:
    # Cyrillic sorts by code point, which is alphabetical except for one letter that sits
    # past the end of the alphabet (see texts.VAULT_SORT_FOLD).
    folded = bare(name).casefold()
    for letter, instead in texts.VAULT_SORT_FOLD.items():
        folded = folded.replace(letter, instead)
    return folded


def needed(text: str) -> list[str]:
    """What still has to be bought."""
    return [ln.name for ln in read(text) if not ln.done]


def _written(line: Line, *, done: bool) -> str:
    return f"{line.prefix}[{DONE if done else OPEN}] {line.name}"


def apply(text: str, names: list[str], *, done: bool) -> tuple[str, list[str]]:
    """The page with those products ticked or unticked, and which of them actually changed.

    Unticking something the page has never heard of adds it — that is how the registry learns.
    Ticking something unknown adds it already ticked, so buying a new product registers it
    without putting it on the list."""
    lines = text.split("\n")
    known = read(text)
    changed: list[str] = []
    added: list[tuple[str, str]] = []  # (name, prefix) for products the page did not have
    for raw in names:
        name = bare(raw)
        if not name:
            continue
        found = match(name, known)
        if found is not None:
            if found.done == done:
                continue  # already in the state asked for: say nothing, write nothing
            lines[found.at] = _written(found, done=done)
            known = [ln if ln.at != found.at else Line(found.at, found.prefix, found.name, done)
                     for ln in known]
            changed.append(found.name)
            continue
        if any(same(name, other) for other, _ in added):
            continue  # the same product named twice in one message
        added.append((name, known[0].prefix if known else "- "))
        changed.append(name)
    for name, prefix in added:
        lines = _insert(lines, f"{prefix}[{DONE if done else OPEN}] {name}", name)
    return "\n".join(lines), changed


def _insert(lines: list[str], written: str, name: str) -> list[str]:
    """A new product, in alphabetical order among the ones already there — a hundred-line
    registry is only usable by hand if it is sorted."""
    key = _sort_key(name)
    at = None
    for line in read("\n".join(lines)):
        if _sort_key(line.name) > key:
            at = line.at
            break
        at = line.at + 1
    if at is None:  # no products yet: after the last non-empty line
        at = next((i + 1 for i in range(len(lines) - 1, -1, -1) if lines[i].strip()),
                  len(lines))
        if at and lines[at - 1].strip():
            # A blank line between the page's own text and the first product, or the
            # explanation at the top runs straight into the list.
            return [*lines[:at], "", written, *lines[at:]]
    return [*lines[:at], written, *lines[at:]]


def note() -> str:
    """A fresh page, with the one sentence that explains how it is used."""
    return f"# {texts.VAULT_GROCERIES_NOTE}\n\n{texts.VAULT_GROCERIES_INTRO}\n"
