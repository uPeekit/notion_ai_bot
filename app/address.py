"""The bot's own name: recognising when it is being addressed, and taking the name back out.

Deterministic on purpose. A model deciding "was that name meant for me?" would sometimes write
the name into the shopping list, and sometimes drop a real person's name out of a note about
them. The rule here is narrow and explainable instead: the name counts as an address only at
the very start or the very end of a message, with nothing but punctuation around it. Anywhere
else in the sentence it is ordinary words and stays — a task that mentions the bot by name is
still a task about the bot."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app import texts

# Filler that may precede the name, and the punctuation that may surround it.
_OPENERS = "(?:" + "|".join(texts.ADDRESS_OPENERS) + r")\s+"
_EDGE = r"[\s,.!?:;—–\-]*"
# The name must be a whole word: a longer word that merely starts with it is not the bot.
_BOUND = r"(?![\w-])"
_BEFORE = r"(?<![\w-])"
# A question about the bot is short; a long message that merely contains the words is a note.
MAX_ABOUT = 40


@dataclass(frozen=True)
class Address:
    text: str  # the message without the name
    called: bool  # the name was used to address the bot
    only_name: bool  # the whole message was the name and nothing else


def names(raw: str) -> tuple[str, ...]:
    """The configured name and its other spellings, longest first so the longer one wins."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return tuple(sorted(parts, key=len, reverse=True))


def _pattern(spellings: tuple[str, ...]) -> re.Pattern[str] | None:
    if not spellings:
        return None
    alternatives = "|".join(re.escape(name) for name in spellings)
    return re.compile(
        rf"^{_EDGE}(?:{_OPENERS})?(?:{alternatives}){_BOUND}{_EDGE}"
        rf"|{_EDGE}{_BEFORE}(?:{alternatives}){_BOUND}{_EDGE}$",
        re.IGNORECASE,
    )


def strip(text: str, spellings: tuple[str, ...]) -> Address:
    """Take the name off the front or the back of a message, if it is there."""
    pattern = _pattern(spellings)
    if pattern is None:
        return Address(text, called=False, only_name=False)
    bare = text.strip()
    if any(bare.strip("".join(" ,.!?:;—–-")).casefold() == name.casefold()
           for name in spellings):
        return Address("", called=True, only_name=True)
    cleaned, count = pattern.subn(" ", text, count=2)
    cleaned = cleaned.strip()
    if not count or not cleaned:
        return Address(text.strip(), called=bool(count), only_name=False)
    return Address(cleaned, called=True, only_name=False)


def about_question(text: str) -> bool:
    """A question about the bot itself ("who are you", "what can you do"), answered from
    `texts.ABOUT_QUESTIONS`, never from a model."""
    asked = " ".join(text.lower().split()).strip("?!. ")
    if len(asked) > MAX_ABOUT:
        return False
    return any(phrase in asked for phrase in texts.ABOUT_QUESTIONS)
