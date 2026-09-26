"""Editing a note's markdown: where a new line goes, and how an existing line changes.

The same intent as the Notion executor's list matching, on text instead of blocks — a line
added to a note joins the list that is already under its heading, in that list's own style,
rather than landing at the end of the file as a stray paragraph."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.vault import frontmatter

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_ITEM = re.compile(r"^(\s*)(- \[[^\]]\]\s+|[-*+]\s+|\d+[.)]\s+)(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_TASK = re.compile(r"^(\s*)([-*+]\s+)\[([^\]])\]\s+(.*)$")
_DUE = re.compile(r"📅\s*\d{4}-\d{2}-\d{2}")
DONE = "x"
OPEN = " "


@dataclass(frozen=True)
class Section:
    heading: str | None
    start: int  # first line of the body under the heading
    end: int  # one past its last line


def _lines(text: str) -> list[str]:
    return text.split("\n")


def sections(lines: list[str]) -> list[Section]:
    """The note split at its headings. The text before the first heading is a section with no
    heading, so a note without headings has exactly one section."""
    out: list[Section] = []
    heading: str | None = None
    start = 0
    fenced = False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = _HEADING.match(line)
        if m:
            if i > start or heading is not None:
                out.append(Section(heading, start, i))
            heading, start = m.group(2), i + 1
    out.append(Section(heading, start, len(lines)))
    return out


def find_section(lines: list[str], heading: str | None) -> Section | None:
    if heading is None:
        return sections(lines)[0] if lines else None
    wanted = heading.strip().lstrip("#").strip().casefold()
    for s in sections(lines):
        if s.heading and s.heading.strip().casefold() == wanted:
            return s
    return None


def _last_item(lines: list[str], section: Section) -> tuple[int, re.Match] | None:
    """The last list item of the section's last list, and the run it belongs to."""
    found: tuple[int, re.Match] | None = None
    fenced = False
    for i in range(section.start, min(section.end, len(lines))):
        if _FENCE.match(lines[i]):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = _ITEM.match(lines[i])
        if m:
            found = (i, m)
        elif lines[i].strip() and found is not None:
            found = None  # the list ended; only a run that reaches the end counts
    return found


def _restyle(line: str, marker: str) -> str:
    """`line` as an item of a list whose items start with `marker`, whatever style it arrived
    in. A numbered list numbers the new item itself."""
    body = _ITEM.match(line)
    text = body.group(3) if body else line.strip()
    number = re.match(r"^(\d+)([.)]\s+)$", marker)
    if number:
        return f"{int(number.group(1)) + 1}{number.group(2)}{text}"
    return f"{marker}{text}"


def append_to(text: str, new_lines: list[str], heading: str | None = None,
              *, create_heading: bool = False) -> str | None:
    """`text` with `new_lines` added under `heading` (or at the end when it is None). Returns
    None when the heading is not there and may not be created."""
    props, body = frontmatter.split(text)
    lines = _lines(body)
    section = find_section(lines, heading)
    if section is None:
        if not create_heading or heading is None:
            return None
        while lines and not lines[-1].strip():
            lines.pop()
        block = ([""] if lines else []) + [f"## {heading}", ""] + new_lines
        return frontmatter.render(props, "\n".join(lines + block))

    last = _last_item(lines, section)
    if last is not None:
        at, m = last
        indent, marker = m.group(1), m.group(2)
        fitted = [indent + _restyle(line, marker) for line in new_lines]
        lines[at + 1:at + 1] = fitted
    else:
        end = section.end
        while end > section.start and not lines[end - 1].strip():
            end -= 1
        block = ([""] if end > section.start else []) + new_lines
        lines[end:end] = block
    return frontmatter.render(props, "\n".join(lines))


def set_props(text: str, changes: dict) -> str:
    """Set or clear properties, keeping the order of the ones already there. A value of None
    removes the property."""
    props, body = frontmatter.split(text)
    for key, value in changes.items():
        if value is None:
            props.pop(key, None)
        else:
            props[key] = value
    return frontmatter.render(props, body)


def replace_section(text: str, heading: str, new_body: list[str]) -> str | None:
    props, body = frontmatter.split(text)
    lines = _lines(body)
    section = find_section(lines, heading)
    if section is None or section.heading is None:
        return None
    lines[section.start:section.end] = ["", *new_body, ""]
    return frontmatter.render(props, "\n".join(lines))


def task_lines(text: str) -> list[tuple[int, str, str]]:
    """(line number, status character, text) for every tick box in the note."""
    _, body = frontmatter.split(text)
    out = []
    for i, line in enumerate(_lines(body)):
        m = _TASK.match(line)
        if m:
            out.append((i, m.group(3), m.group(4)))
    return out


def find_task(text: str, wanted: str) -> int | None:
    """The one tick box whose text matches `wanted` — exactly, or by containing every word of
    it. None when nothing matches, and None when several do: a guess would tick the wrong one."""
    from app.vault.index import words

    target = wanted.strip().casefold()
    hits = [i for i, _, line in task_lines(text) if line.strip().casefold() == target]
    if len(hits) != 1:
        wanted_words = words(wanted)
        hits = [i for i, _, line in task_lines(text)
                if wanted_words and wanted_words <= words(line)]
    return hits[0] if len(hits) == 1 else None


def set_task(text: str, line_no: int, *, checked: bool | None = None,
             due: str | None = None) -> str:
    """Tick, untick or re-date one tick box, leaving everything else on the line alone."""
    props, body = frontmatter.split(text)
    lines = _lines(body)
    m = _TASK.match(lines[line_no])
    if m is None:
        return text
    indent, marker, mark, rest = m.groups()
    if checked is not None:
        mark = DONE if checked else OPEN
    if due is not None:
        rest = _DUE.sub("", rest).rstrip()
        rest = f"{rest} 📅 {due}" if due else rest
    lines[line_no] = f"{indent}{marker}[{mark}] {rest.strip()}"
    return frontmatter.render(props, "\n".join(lines))


def numbered(lines: list[str]) -> str:
    """A note as the editing model reads it: `[7] the line`, one number per line.

    A note has no block ids, so a line number *is* the address — which is why the edits are
    applied to exactly the list of lines that was numbered, and never to a re-read file."""
    return "\n".join(f"[{i}] {line}" for i, line in enumerate(lines, start=1))


def apply_edits(lines: list[str], edits: list) -> list[str]:
    """The note's lines with an edit script applied. Pure: no file is read or written here.

    Every edit is resolved against the numbering it was given, so several edits never shift each
    other's targets — replacements happen in place, insertions are collected per line and
    written out afterwards, deletions only drop lines. An edit naming a line that is not there
    is ignored: the model can only point at what it was shown."""
    replaced: dict[int, str] = {}
    inserted: dict[int, list[str]] = {}
    dropped: set[int] = set()
    for edit in edits:
        op = getattr(edit, "op", "")
        at = getattr(edit, "at", 0)
        if op == "replace" and 1 <= at <= len(lines):
            replaced[at] = edit.text
        elif op == "delete":
            dropped.update(n for n in edit.span if 1 <= n <= len(lines))
        elif op == "insert" and edit.text.strip():
            inserted.setdefault(min(max(at, 1), len(lines)), []).append(edit.text)
        elif op == "image" and edit.url:
            caption = edit.caption or ""
            inserted.setdefault(min(max(at, 1), len(lines)), []).append(
                f"![{caption}]({edit.url})")
    out: list[str] = []
    for n, line in enumerate(lines, start=1):
        if n not in dropped:
            out.append(replaced.get(n, line))
        out += inserted.get(n, [])
    return out
