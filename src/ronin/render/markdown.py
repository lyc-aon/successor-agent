"""Markdown rendering — Pretext-shaped, theme-agnostic, pure stdlib.

This module turns CommonMark-subset source text into a list of laid-out
lines that the chat painter can render. It's the markdown analog of
`text.py`'s `PreparedText`: parse the source ONCE in `__init__`, then
`lines(width)` is cached per width.

Two-stage rendering:

  1. PARSE: source string → AST (list of Block subclasses)
       Done once in PreparedMarkdown.__init__. The AST holds blocks
       like Paragraph, Header, CodeBlock, BulletList, etc., each with
       inline spans where applicable.

  2. LAYOUT (cached per width): AST → list[LaidOutLine]
       Each LaidOutLine has a list of LaidOutSpan objects (text +
       attribute bitmask + semantic tag) and an optional line tag for
       full-row backgrounds (code blocks, blockquotes).

The chat painter then resolves semantic tags to theme-specific colors
at paint time, so the layout cache is theme-independent. Theme
transitions only re-style; they don't re-layout.

CommonMark subset supported:

  Block: paragraphs, # headers (1-6), ``` fenced code blocks,
         - / * / + bullet lists, 1. / 2. ordered lists, > blockquotes,
         --- horizontal rules

  Inline: **bold**, *italic*, ~~strikethrough~~, `inline code`,
          [link text](url), plain text

Not supported (deferred):

  Tables, footnotes, HTML embedding, reference links, setext headers,
  image syntax, deeply nested lists, definition lists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .cells import (
    ATTR_BOLD,
    ATTR_DIM,
    ATTR_ITALIC,
    ATTR_STRIKE,
    ATTR_UNDERLINE,
)
from .measure import char_width


# ─── AST types ───


@dataclass(slots=True)
class Span:
    """An inline span — a run of text with consistent style.

    attrs is a bitmask of ATTR_* flags.
    tag is a semantic identifier the painter resolves to theme colors:
        ""        — default body text
        "code"    — inline `code` (bg tint)
        "link"    — clickable link (extracted url)
    link is the URL for tag="link" spans.
    """
    text: str
    attrs: int = 0
    tag: str = ""
    link: str | None = None


# Block types — discriminated union via isinstance checks. Each block
# carries the data the renderer needs to lay it out.


@dataclass(slots=True)
class Paragraph:
    spans: list[Span]


@dataclass(slots=True)
class Header:
    level: int  # 1-6
    spans: list[Span]


@dataclass(slots=True)
class CodeBlock:
    language: str  # may be empty
    lines: list[str]  # raw lines, no inline parsing


@dataclass(slots=True)
class ListItem:
    spans: list[Span]
    # Future: nested blocks for multi-paragraph items. v0 keeps items
    # to single-line spans.


@dataclass(slots=True)
class BulletList:
    items: list[ListItem]


@dataclass(slots=True)
class OrderedList:
    items: list[ListItem]
    start: int = 1


@dataclass(slots=True)
class BlockQuote:
    blocks: list[Block]  # forward reference; resolved at runtime


@dataclass(slots=True)
class HorizontalRule:
    pass


# Forward-reference resolution: Block is a union of all the types above.
# We can't use `Block | ...` syntax at the type level inside the dataclass
# field annotation reliably without quoting, so we declare the alias here.
Block = (
    Paragraph
    | Header
    | CodeBlock
    | BulletList
    | OrderedList
    | BlockQuote
    | HorizontalRule
)


# ─── Laid-out output (after wrap) ───


@dataclass(slots=True)
class LaidOutSpan:
    """A span ready for rendering — text, attrs, semantic tag."""
    text: str
    attrs: int = 0
    tag: str = ""
    link: str | None = None


@dataclass(slots=True)
class LaidOutLine:
    """A single rendered row of markdown content.

    spans:    left-to-right styled segments
    line_tag: optional full-row treatment ("code_block", "blockquote",
              "header_rule", "hr"). The painter applies row-wide
              backgrounds or border characters based on this.
    indent:   how many cells of leading whitespace this line should
              get when painted (used for list items, blockquote depth)
    """
    spans: list[LaidOutSpan] = field(default_factory=list)
    line_tag: str = ""
    indent: int = 0


# ─── Block parser ───


_HR_RE = re.compile(r"^([-*_])(?:\s*\1){2,}\s*$")
_HEADER_RE = re.compile(r"^(#{1,6})(?:\s+(.*))?$")
_BULLET_RE = re.compile(r"^[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\d+)[.)]\s+(.*)$")
_FENCE_RE = re.compile(r"^```\s*(\S*)\s*$")
_BLOCKQUOTE_RE = re.compile(r"^>\s?(.*)$")


def parse_blocks(source: str) -> list[Block]:
    """Parse markdown source into a list of block-level elements.

    Walks the source line-by-line, identifying block starts and
    grouping consecutive lines that belong to the same block.
    """
    if not source:
        return []
    lines = source.split("\n")
    blocks: list[Block] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Empty line — paragraph separator, skip
        if not stripped:
            i += 1
            continue

        # Horizontal rule
        if _HR_RE.match(stripped):
            blocks.append(HorizontalRule())
            i += 1
            continue

        # Fenced code block
        fence_match = _FENCE_RE.match(stripped)
        if fence_match:
            language = fence_match.group(1)
            i += 1
            code_lines: list[str] = []
            while i < n:
                if lines[i].strip().startswith("```"):
                    i += 1  # consume closing fence
                    break
                code_lines.append(lines[i])
                i += 1
            blocks.append(CodeBlock(language=language, lines=code_lines))
            continue

        # Header
        header_match = _HEADER_RE.match(stripped)
        if header_match:
            level = len(header_match.group(1))
            content = (header_match.group(2) or "").strip()
            blocks.append(Header(level=level, spans=parse_inline(content)))
            i += 1
            continue

        # Blockquote (consume consecutive > lines)
        if _BLOCKQUOTE_RE.match(stripped):
            quote_source_lines: list[str] = []
            while i < n:
                m = _BLOCKQUOTE_RE.match(lines[i].strip())
                if not m:
                    break
                quote_source_lines.append(m.group(1))
                i += 1
            inner_source = "\n".join(quote_source_lines)
            blocks.append(BlockQuote(blocks=parse_blocks(inner_source)))
            continue

        # Bullet list
        if _BULLET_RE.match(stripped):
            items: list[ListItem] = []
            while i < n:
                m = _BULLET_RE.match(lines[i].strip())
                if not m:
                    break
                items.append(ListItem(spans=parse_inline(m.group(1))))
                i += 1
            blocks.append(BulletList(items=items))
            continue

        # Ordered list
        ordered_match = _ORDERED_RE.match(stripped)
        if ordered_match:
            start = int(ordered_match.group(1))
            items = []
            while i < n:
                m = _ORDERED_RE.match(lines[i].strip())
                if not m:
                    break
                items.append(ListItem(spans=parse_inline(m.group(2))))
                i += 1
            blocks.append(OrderedList(items=items, start=start))
            continue

        # Default: paragraph (consume consecutive non-empty non-block lines)
        para_lines: list[str] = []
        while i < n:
            cur = lines[i]
            cur_stripped = cur.strip()
            if not cur_stripped:
                break
            if (
                _HR_RE.match(cur_stripped)
                or _HEADER_RE.match(cur_stripped)
                or _FENCE_RE.match(cur_stripped)
                or _BULLET_RE.match(cur_stripped)
                or _ORDERED_RE.match(cur_stripped)
                or _BLOCKQUOTE_RE.match(cur_stripped)
            ):
                break
            para_lines.append(cur_stripped)
            i += 1
        text = " ".join(para_lines)
        blocks.append(Paragraph(spans=parse_inline(text)))

    return blocks


# ─── Inline parser ───
#
# A small state-machine parser. We walk the input character by character,
# tracking the current attribute bitmask and emitting spans when the
# style changes. The supported markers are:
#
#   `code`           inline code
#   **bold**         bold (also __bold__ — defer)
#   *italic*         italic (also _italic_ — defer)
#   ~~strike~~       strikethrough
#   [text](url)      link
#
# This is intentionally less strict than CommonMark's flanking-delimiter
# rules. For chat-shaped content the naive approach handles all real
# cases without overengineering.


def parse_inline(text: str) -> list[Span]:
    """Walk text producing styled spans."""
    spans: list[Span] = []
    if not text:
        return spans

    i = 0
    n = len(text)
    cur_text = ""
    cur_attrs = 0

    def flush() -> None:
        nonlocal cur_text
        if cur_text:
            spans.append(Span(text=cur_text, attrs=cur_attrs))
            cur_text = ""

    while i < n:
        ch = text[i]

        # Inline code: `text`. The text inside is literal — no other
        # markers are interpreted.
        if ch == "`":
            end = text.find("`", i + 1)
            if end != -1:
                flush()
                code_text = text[i + 1:end]
                spans.append(Span(text=code_text, tag="code"))
                i = end + 1
                continue

        # Link: [text](url)
        if ch == "[":
            close_bracket = text.find("]", i + 1)
            if (
                close_bracket != -1
                and close_bracket + 1 < n
                and text[close_bracket + 1] == "("
            ):
                close_paren = text.find(")", close_bracket + 2)
                if close_paren != -1:
                    flush()
                    link_text = text[i + 1:close_bracket]
                    link_url = text[close_bracket + 2:close_paren]
                    spans.append(
                        Span(
                            text=link_text,
                            attrs=cur_attrs | ATTR_UNDERLINE,
                            tag="link",
                            link=link_url,
                        )
                    )
                    i = close_paren + 1
                    continue

        # Bold: **text**
        if ch == "*" and i + 1 < n and text[i + 1] == "*":
            flush()
            cur_attrs ^= ATTR_BOLD
            i += 2
            continue

        # Italic: *text* (single asterisk)
        if ch == "*":
            flush()
            cur_attrs ^= ATTR_ITALIC
            i += 1
            continue

        # Strikethrough: ~~text~~
        if ch == "~" and i + 1 < n and text[i + 1] == "~":
            flush()
            cur_attrs ^= ATTR_STRIKE
            i += 2
            continue

        cur_text += ch
        i += 1

    flush()
    return spans


# ─── Layout / wrap ───


def _span_width(text: str) -> int:
    """Display width of text using char_width."""
    return sum(char_width(c) for c in text)


def _wrap_spans(spans: list[Span], width: int) -> list[list[LaidOutSpan]]:
    """Greedy word-wrap a list of inline spans into a list of lines.

    Each output line is a list of LaidOutSpan in left-to-right order.
    Spans are split at word boundaries; words longer than width get
    hard-broken. The original span's attrs/tag/link are preserved
    across the split.
    """
    if width <= 0:
        return [[]]
    if not spans:
        return [[]]

    lines: list[list[LaidOutSpan]] = []
    current: list[LaidOutSpan] = []
    current_w = 0

    for span in spans:
        # Tokenize the span text into words and whitespace runs.
        # We preserve the original span's attrs/tag/link for each
        # emitted LaidOutSpan.
        tokens = _tokenize_span_text(span.text)

        for tok_text, is_space in tokens:
            tok_w = _span_width(tok_text)

            # Wrap if this token would overflow
            if current_w + tok_w > width:
                if is_space and current_w > 0:
                    # Drop trailing space, commit, reset
                    lines.append(current)
                    current = []
                    current_w = 0
                    continue
                if current_w == 0:
                    # Token alone is wider than width — hard-break it
                    chunks = _hard_break_token(tok_text, width)
                    for chunk in chunks[:-1]:
                        lines.append(
                            [LaidOutSpan(
                                text=chunk,
                                attrs=span.attrs,
                                tag=span.tag,
                                link=span.link,
                            )]
                        )
                    last = chunks[-1]
                    if last:
                        current = [
                            LaidOutSpan(
                                text=last,
                                attrs=span.attrs,
                                tag=span.tag,
                                link=span.link,
                            )
                        ]
                        current_w = _span_width(last)
                    continue
                # Commit current line and start a new one with the token
                lines.append(current)
                current = []
                current_w = 0
                if is_space:
                    continue  # don't lead a wrapped line with whitespace

            # Add token to current line. Try to merge with the previous
            # span if it shares attrs/tag/link.
            if (
                current
                and current[-1].attrs == span.attrs
                and current[-1].tag == span.tag
                and current[-1].link == span.link
            ):
                current[-1].text += tok_text
            else:
                current.append(
                    LaidOutSpan(
                        text=tok_text,
                        attrs=span.attrs,
                        tag=span.tag,
                        link=span.link,
                    )
                )
            current_w += tok_w

    if current or not lines:
        lines.append(current)
    return lines


def _tokenize_span_text(text: str) -> list[tuple[str, bool]]:
    """Split text into (token, is_space) pairs.

    A "word" is a maximal run of non-whitespace; a "space" is a maximal
    run of whitespace. Newlines inside spans are treated as space (the
    block parser already split them out).
    """
    if not text:
        return []
    out: list[tuple[str, bool]] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            j = i
            while j < n and text[j].isspace():
                j += 1
            out.append((text[i:j], True))
            i = j
        else:
            j = i
            while j < n and not text[j].isspace():
                j += 1
            out.append((text[i:j], False))
            i = j
    return out


def _hard_break_token(text: str, width: int) -> list[str]:
    """Break a too-long token into chunks of at most `width` cells."""
    if width <= 0:
        return [text]
    chunks: list[str] = []
    current = ""
    current_w = 0
    for ch in text:
        cw = char_width(ch)
        if current_w + cw > width:
            chunks.append(current)
            current = ch
            current_w = cw
        else:
            current += ch
            current_w += cw
    if current:
        chunks.append(current)
    return chunks


# ─── Block-to-line rendering ───


def render_blocks(blocks: list[Block], width: int) -> list[LaidOutLine]:
    """Render a list of blocks into laid-out lines for the given width."""
    out: list[LaidOutLine] = []
    n = len(blocks)
    for i, block in enumerate(blocks):
        block_lines = _render_block(block, width)
        out.extend(block_lines)
        # Spacer between blocks (single blank line) — but not after
        # the last block, and not for the special case of two adjacent
        # list items / nothing-to-separate.
        if i < n - 1:
            out.append(LaidOutLine())
    return out


def _render_block(block: Block, width: int) -> list[LaidOutLine]:
    if isinstance(block, Paragraph):
        return _render_paragraph(block, width)
    if isinstance(block, Header):
        return _render_header(block, width)
    if isinstance(block, CodeBlock):
        return _render_code_block(block, width)
    if isinstance(block, BulletList):
        return _render_bullet_list(block, width)
    if isinstance(block, OrderedList):
        return _render_ordered_list(block, width)
    if isinstance(block, BlockQuote):
        return _render_block_quote(block, width)
    if isinstance(block, HorizontalRule):
        return _render_hr(block, width)
    return []


def _render_paragraph(block: Paragraph, width: int) -> list[LaidOutLine]:
    wrapped = _wrap_spans(block.spans, width)
    return [LaidOutLine(spans=row) for row in wrapped]


def _render_header(block: Header, width: int) -> list[LaidOutLine]:
    # Headers get bold + the "header" semantic tag. Below the header
    # we emit a thin horizontal rule (only for h1/h2 to keep noise down).
    boosted = [
        Span(text=s.text, attrs=s.attrs | ATTR_BOLD, tag="header", link=s.link)
        for s in block.spans
    ]
    wrapped = _wrap_spans(boosted, width)
    out = [LaidOutLine(spans=row) for row in wrapped]
    if block.level <= 2:
        out.append(LaidOutLine(line_tag="header_rule"))
    return out


def _render_code_block(block: CodeBlock, width: int) -> list[LaidOutLine]:
    out: list[LaidOutLine] = []
    # Optional language tag header row
    if block.language:
        tag_text = f" {block.language} "
        out.append(
            LaidOutLine(
                spans=[LaidOutSpan(text=tag_text, attrs=ATTR_DIM, tag="code_lang")],
                line_tag="code_lang",
            )
        )
    # Each code line — hard-wrapped at width-2 to leave a 2-cell indent
    inner_w = max(1, width - 2)
    for line in block.lines:
        if not line:
            out.append(LaidOutLine(spans=[], line_tag="code_block"))
            continue
        for chunk in _hard_break_token(line, inner_w):
            out.append(
                LaidOutLine(
                    spans=[LaidOutSpan(text=chunk, tag="code")],
                    line_tag="code_block",
                    indent=2,
                )
            )
    return out


def _render_bullet_list(block: BulletList, width: int) -> list[LaidOutLine]:
    out: list[LaidOutLine] = []
    bullet = "• "
    bullet_w = len(bullet)
    for item in block.items:
        wrapped = _wrap_spans(item.spans, max(1, width - bullet_w))
        for line_idx, row in enumerate(wrapped):
            line_spans: list[LaidOutSpan] = []
            if line_idx == 0:
                line_spans.append(
                    LaidOutSpan(text=bullet, attrs=ATTR_BOLD, tag="list_marker")
                )
            else:
                line_spans.append(LaidOutSpan(text=" " * bullet_w))
            line_spans.extend(row)
            out.append(LaidOutLine(spans=line_spans))
    return out


def _render_ordered_list(block: OrderedList, width: int) -> list[LaidOutLine]:
    out: list[LaidOutLine] = []
    n = len(block.items)
    end_num = block.start + n - 1
    num_w = len(str(end_num)) + 2  # "N. "
    for i, item in enumerate(block.items):
        marker = f"{block.start + i}. "
        marker = marker.rjust(num_w)
        wrapped = _wrap_spans(item.spans, max(1, width - num_w))
        for line_idx, row in enumerate(wrapped):
            line_spans: list[LaidOutSpan] = []
            if line_idx == 0:
                line_spans.append(
                    LaidOutSpan(text=marker, attrs=ATTR_BOLD, tag="list_marker")
                )
            else:
                line_spans.append(LaidOutSpan(text=" " * num_w))
            line_spans.extend(row)
            out.append(LaidOutLine(spans=line_spans))
    return out


def _render_block_quote(block: BlockQuote, width: int) -> list[LaidOutLine]:
    out: list[LaidOutLine] = []
    # Each inner block gets indented; we mark line_tag="blockquote" so the
    # painter draws the left border character.
    inner_w = max(1, width - 2)  # 2 cells for the border + space
    inner_lines = render_blocks(block.blocks, inner_w)
    for line in inner_lines:
        out.append(
            LaidOutLine(
                spans=line.spans,
                line_tag="blockquote",
                indent=2,
            )
        )
    return out


def _render_hr(block: HorizontalRule, width: int) -> list[LaidOutLine]:
    return [LaidOutLine(line_tag="hr")]


# ─── PreparedMarkdown — Pretext-shaped cache ───


class PreparedMarkdown:
    """A markdown source that can be laid out at any cell width.

    Pretext-shaped: parse the source ONCE in __init__, then lines(width)
    is cached for the most recent width. Re-rendering at a different
    width pays one wrap pass; re-rendering at the same width is free.

    The output is theme-agnostic — semantic tags ("code", "header",
    "list_marker", etc.) are resolved to actual colors at paint time
    by the chat. This keeps the cache theme-independent.
    """

    __slots__ = (
        "source",
        "_blocks",
        "_cache_w",
        "_cache_lines",
    )

    def __init__(self, source: str) -> None:
        self.source = source
        self._blocks: list[Block] = parse_blocks(source)
        self._cache_w: int = -1
        self._cache_lines: list[LaidOutLine] = []

    def lines(self, width: int) -> list[LaidOutLine]:
        if width <= 0:
            return []
        if width == self._cache_w:
            return self._cache_lines
        self._cache_lines = render_blocks(self._blocks, width)
        self._cache_w = width
        return self._cache_lines

    def height(self, width: int) -> int:
        return len(self.lines(width))
