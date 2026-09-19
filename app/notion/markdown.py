"""Markdown → Notion blocks, for text the model wrote (and the web research it asked for).

A deliberately small dialect, one block per line: `#`/`##`/`###` headings, `-`/`*` bullets,
`1.` numbered items, `- [ ]`/`- [x]` to-dos, `>` quotes, fenced code, `---` dividers and
`![caption](url)` images; inline **bold**, *italic*, ~~strike~~, `code` and [links](url).
Anything else is a paragraph, character for character. Nesting is flattened: a Telegram
message rarely has any, and a flat list is never wrong, only plainer."""

from __future__ import annotations

import re

RICH_TEXT_LIMIT = 2000
# One rich-text array may hold at most 100 elements; a paragraph with more formatting runs
# than that keeps its text and loses the excess formatting.
MAX_RICH_ELEMENTS = 100

_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
# The check mark may be a Cyrillic "x" (U+0445/U+0425) typed on a Russian layout.
_TODO = re.compile("^[-*+]\\s+\\[([ xX\u0445\u0425])\\]\\s+(.*)$")
_BULLET = re.compile(r"^[-*+•]\s+(.*)$")
_NUMBERED = re.compile(r"^\d{1,3}[.)]\s+(.*)$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_DIVIDER = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
_IMAGE = re.compile(r"^!\[([^\]]*)\]\((https?://[^\s)]+)\)$")
_FENCE = re.compile(r"^```\s*([\w+-]*)\s*$")
# Inline runs, in priority order: code first (nothing inside it is markup), then links, then
# the emphasis markers.
_INLINE = re.compile(
    r"`(?P<code>[^`]+)`"
    r"|\[(?P<ltext>[^\]]+)\]\((?P<lurl>https?://[^\s)]+)\)"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|__(?P<bold2>.+?)__"
    r"|~~(?P<strike>.+?)~~"
    r"|(?<![\w*])\*(?P<italic>[^*\s][^*]*?)\*(?![\w*])"
    r"|(?<![\w_])_(?P<italic2>[^_\s][^_]*?)_(?![\w_])"
)
_URL = re.compile(r"https?://[^\s)>\]]+")

# Notion's code-block language list is long and strict; anything unknown is "plain text".
_CODE_LANGUAGES = frozenset({
    "bash", "c", "c#", "c++", "css", "go", "html", "java", "javascript", "json", "kotlin",
    "markdown", "php", "python", "ruby", "rust", "shell", "sql", "swift", "typescript", "xml",
    "yaml",
})
_LANGUAGE_ALIASES = {"py": "python", "js": "javascript", "ts": "typescript", "sh": "shell",
                     "yml": "yaml", "cs": "c#", "cpp": "c++", "md": "markdown"}


def _text(content: str, *, url: str | None = None, **annotations: bool) -> list[dict]:
    out = []
    for i in range(0, len(content), RICH_TEXT_LIMIT):
        run: dict = {"type": "text", "text": {"content": content[i:i + RICH_TEXT_LIMIT]}}
        if url:
            run["text"]["link"] = {"url": url}
        if annotations:
            run["annotations"] = annotations
        out.append(run)
    return out


def _plain_with_links(text: str, **annotations: bool) -> list[dict]:
    """Plain text in which bare URLs become links."""
    out: list[dict] = []
    pos = 0
    for m in _URL.finditer(text):
        url = m.group(0).rstrip(".,;:!?")  # sentence punctuation after a URL is not part of it
        if m.start() > pos:
            out += _text(text[pos:m.start()], **annotations)
        out += _text(url, url=url, **annotations)
        pos = m.start() + len(url)
    if pos < len(text):
        out += _text(text[pos:], **annotations)
    return out


def rich_text(line: str) -> list[dict]:
    """Inline Markdown → a Notion rich-text array."""
    out: list[dict] = []
    pos = 0
    for m in _INLINE.finditer(line):
        if m.start() > pos:
            out += _plain_with_links(line[pos:m.start()])
        g = m.groupdict()
        if g["code"] is not None:
            out += _text(g["code"], code=True)
        elif g["ltext"] is not None:
            out += _text(g["ltext"], url=g["lurl"])
        elif g["bold"] is not None or g["bold2"] is not None:
            out += _plain_with_links(g["bold"] or g["bold2"], bold=True)
        elif g["strike"] is not None:
            out += _plain_with_links(g["strike"], strikethrough=True)
        else:
            out += _plain_with_links(g["italic"] or g["italic2"], italic=True)
        pos = m.end()
    if pos < len(line):
        out += _plain_with_links(line[pos:])
    if len(out) > MAX_RICH_ELEMENTS:
        return _text(line)
    return out


def _block(kind: str, text: str, **extra: object) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": rich_text(text), **extra}}


def image_block(url: str, caption: str = "") -> dict:
    image: dict = {"type": "external", "external": {"url": url}}
    if caption:
        image["caption"] = _text(caption)
    return {"object": "block", "type": "image", "image": image}


def _code_block(lines: list[str], language: str) -> dict:
    lang = _LANGUAGE_ALIASES.get(language.lower(), language.lower())
    return {"object": "block", "type": "code", "code": {
        "rich_text": _text("\n".join(lines)),
        "language": lang if lang in _CODE_LANGUAGES else "plain text",
    }}


def markdown_blocks(text: str) -> list[dict]:
    blocks: list[dict] = []
    code: list[str] | None = None
    language = ""
    for raw in text.splitlines():
        if code is not None:
            if _FENCE.match(raw.strip()):
                blocks.append(_code_block(code, language))
                code = None
            else:
                code.append(raw)
            continue
        line = raw.strip()
        if not line:
            continue
        if m := _FENCE.match(line):
            code, language = [], m.group(1)
        elif m := _HEADING.match(line):
            blocks.append(_block(f"heading_{len(m.group(1))}", m.group(2)))
        elif _DIVIDER.match(line):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        elif m := _IMAGE.match(line):
            blocks.append(image_block(m.group(2), m.group(1)))
        elif m := _TODO.match(line):
            blocks.append(_block("to_do", m.group(2), checked=m.group(1).strip() != ""))
        elif m := _BULLET.match(line):
            blocks.append(_block("bulleted_list_item", m.group(1)))
        elif m := _NUMBERED.match(line):
            blocks.append(_block("numbered_list_item", m.group(1)))
        elif m := _QUOTE.match(line):
            blocks.append(_block("quote", m.group(1)))
        else:
            blocks.append(_block("paragraph", line))
    if code is not None:  # an unclosed fence still keeps its text
        blocks.append(_code_block(code, language))
    return blocks
