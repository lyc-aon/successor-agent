"""PreparedToolOutput — Pretext-shaped output pipeline for tool cards.

The renderer used to call `_wrap_output(card, width=...)` every paint
which does a fresh string split + hard-wrap pass per frame. That's
fine for a handful of short cards but wasteful at 30fps with long
command output. Worse, the old function had no way to carry span-level
metadata (match highlights in grep output, filetype chrome in ls
output), so any richer visual treatment required a second parse pass
at paint time.

PreparedToolOutput is the Pretext analog for tool card output:

    __init__  — parse the card's output ONCE, using a verb-class-
                specific parser that extracts structure (grep ->
                file:line:content tuples, ls -l -> perms+size+name
                tuples, etc). The result is a list of "prepared lines"
                with semantic spans.
    layout(w) — hard-wrap the prepared lines to width `w` and return
                a list of OutputLine(spans, kind) rows the renderer
                can paint directly. Width-keyed single-entry cache:
                repeat calls at the same width are essentially free.

Verb classes drive the parser choice:

    SEARCH  — grep/rg style. Parse `file:line:content` per line;
              produce (filename, lineno, content) tuples. At layout
              time, wrap content and mark match spans so the painter
              can background-highlight them.
    LIST    — ls -l style. Parse perms/size/date/name columns;
              detect `d`/`-`/`l` + `x` exec bit to classify entries
              as dir/file/link/exec. Each row has its name span
              classified so the painter colors them.
    READ    — cat/head/tail. Lines are content as-is; no structural
              parse, but the line kind is "content" so the painter
              can apply a code-block tint without the chrome guesses
              a generic card would make.
    INSPECT — pwd / git-status / git-log. Pass-through with a small
              syntax hint for git output (branch name highlighting,
              untracked/modified chrome).
    default — any other class falls back to plain stdout/stderr
              lines, same as the old _wrap_output behavior.

The output kind enum ("plain", "match", "chrome", "dim", "warn") is
deliberately tiny: five kinds cover every highlighting the cards
currently need. New kinds can be added later, but adding kinds also
means teaching the painter how to color them — kept small on purpose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .cards import ToolCard
from .diff_artifact import ChangeArtifact, ChangedFile, parse_unified_diff
from .verbclass import VerbClass, verb_class_for


# ─── Span + line primitives ───


@dataclass(frozen=True, slots=True)
class OutputSpan:
    """A run of text within an output line, with a semantic kind tag.

    The painter maps kinds to styles (fg/bg/attrs). A single line can
    have multiple spans of different kinds — e.g. a grep match line
    is `chrome + dim + plain + match + plain`.
    """

    text: str
    kind: str = "plain"  # "plain" | "match" | "chrome" | "dim" | "warn"


@dataclass(frozen=True, slots=True)
class OutputLine:
    """One visible output row. `spans` is the actual text content;
    `kind` is a coarse row-level tag the painter can use for the
    background tint of the entire row."""

    spans: tuple[OutputSpan, ...]
    kind: str = "stdout"  # stdout | stderr | match | truncated | header | diff_*

    @property
    def plain(self) -> str:
        """The concatenated text of all spans — used for tests and
        for measuring the total row width before wrapping."""
        return "".join(s.text for s in self.spans)

    @property
    def width(self) -> int:
        return sum(len(s.text) for s in self.spans)


# ─── Prepared-line intermediate form ───
#
# PreparedLine is the PARSED-but-NOT-WRAPPED form. A single PreparedLine
# may become multiple OutputLines after wrapping (e.g., a very long
# grep match with a narrow terminal).

@dataclass(frozen=True, slots=True)
class _PreparedLine:
    spans: tuple[OutputSpan, ...]
    kind: str


# ─── Parsers per verb class ───


# grep/rg output pattern: "file:line:content" or "file:line-content".
# `rg` uses `file:line:content` by default; GNU grep uses the same
# with -n. We accept both colon and dash separators before content.
_GREP_LINE_RE = re.compile(
    r"^(?P<file>[^:]+):(?P<lineno>\d+)[:\-](?P<content>.*)$"
)


def _parse_grep_line(line: str, query: str | None) -> _PreparedLine:
    """Parse one grep output line into spans.

    When the filename/lineno pattern matches, we split the line into
    three spans: file (chrome), `:lineno:` (dim), and the content with
    optional match highlighting. Non-matching lines fall through as
    plain stdout.
    """
    m = _GREP_LINE_RE.match(line)
    if not m:
        return _PreparedLine(
            spans=(OutputSpan(text=line, kind="plain"),),
            kind="stdout",
        )

    file_text = m.group("file")
    lineno_text = m.group("lineno")
    content = m.group("content")

    spans: list[OutputSpan] = [
        OutputSpan(text=file_text, kind="chrome"),
        OutputSpan(text=f":{lineno_text}:", kind="dim"),
    ]

    # If we know the query and it appears in content, break content
    # into alternating plain/match spans so the painter can background-
    # highlight the hits.
    if query:
        spans.extend(_split_match_spans(content, query))
    else:
        spans.append(OutputSpan(text=content, kind="plain"))

    return _PreparedLine(spans=tuple(spans), kind="match")


def _split_match_spans(content: str, query: str) -> list[OutputSpan]:
    """Split `content` into alternating plain and match spans on
    `query`. Case-insensitive exact-substring match — grep's regex
    is more powerful but matching the exact regex requires knowing
    the flags, so we fall back to substring matching which handles
    the common cases (grep TODO, grep -i fooBar, etc.)."""
    if not query:
        return [OutputSpan(text=content, kind="plain")]

    out: list[OutputSpan] = []
    haystack = content
    needle = query
    lower_hay = haystack.lower()
    lower_needle = needle.lower()
    cursor = 0
    while cursor < len(haystack):
        idx = lower_hay.find(lower_needle, cursor)
        if idx == -1:
            if cursor < len(haystack):
                out.append(OutputSpan(text=haystack[cursor:], kind="plain"))
            break
        if idx > cursor:
            out.append(OutputSpan(text=haystack[cursor:idx], kind="plain"))
        out.append(
            OutputSpan(text=haystack[idx:idx + len(needle)], kind="match"),
        )
        cursor = idx + len(needle)
    return out or [OutputSpan(text=content, kind="plain")]


# ls -l parser. We accept both `ls -l` and `ls -la` layouts.
# Example line: "drwxr-xr-x  2 user user  4096 Apr 10 12:00 src"
_LS_LINE_RE = re.compile(
    r"^(?P<perms>[dlcbps\-][rwxXsStT\-]{9}[+.@]?)\s+"
    r"(?P<nlinks>\d+)\s+"
    r"(?P<owner>\S+)\s+"
    r"(?P<group>\S+)\s+"
    r"(?P<size>\d+)\s+"
    r"(?P<date>[A-Za-z]{3}\s+\d+\s+(?:\d{2}:\d{2}|\d{4}))\s+"
    r"(?P<name>.+)$"
)


def _parse_ls_line(line: str) -> _PreparedLine:
    """Parse one line of `ls -l` output into spans.

    Classifies the entry by first-char perms bit (d/-/l/...) and
    decorates the name with a marker: `/` for dirs, `*` for exec,
    `@` for symlinks. Non-matching lines (the "total N" header,
    short-form `ls` output) pass through as plain chrome lines.
    """
    m = _LS_LINE_RE.match(line)
    if not m:
        # Might be the "total 42" header that ls -l prints
        if line.startswith("total "):
            return _PreparedLine(
                spans=(OutputSpan(text=line, kind="dim"),),
                kind="header",
            )
        return _PreparedLine(
            spans=(OutputSpan(text=line, kind="plain"),),
            kind="stdout",
        )

    perms = m.group("perms")
    name = m.group("name")

    # Classify entry kind
    first = perms[0]
    is_exec = "x" in perms[1:4]  # owner exec bit
    if first == "d":
        marker = "▸ "
        name_kind = "match"  # reuse match kind for accent_warn; painter
                             # maps classes; we'll add a dir kind cleanly
    elif first == "l":
        marker = "↗ "
        name_kind = "chrome"
    elif is_exec:
        marker = "★ "
        name_kind = "chrome"
    else:
        marker = "· "
        name_kind = "plain"

    # Rebuild the line: perms/links/owner/group/size/date stay as dim,
    # then the marker + name in its class-tinted span.
    chrome = f"{perms}  {m.group('nlinks'):>2} {m.group('owner'):<6} {m.group('size'):>8}  {m.group('date')}  "
    spans = (
        OutputSpan(text=chrome, kind="dim"),
        OutputSpan(text=marker, kind="chrome"),
        OutputSpan(text=name, kind=name_kind),
    )
    return _PreparedLine(spans=spans, kind="stdout")


# git status porcelain parser. Only triggers on git-status output
# which is already spaced as " M file", "?? file", "A  file" etc.
_GIT_STATUS_RE = re.compile(r"^([ MADRCU\?!]{1,2})\s+(.+)$")


def _parse_git_status_line(line: str) -> _PreparedLine:
    m = _GIT_STATUS_RE.match(line)
    if not m:
        return _PreparedLine(
            spans=(OutputSpan(text=line, kind="plain"),),
            kind="stdout",
        )
    flag = m.group(1).strip() or " "
    path = m.group(2)
    # M/A/?/D become warn chrome; D/R become match chrome
    if "M" in flag or "A" in flag:
        flag_kind = "chrome"
    elif "?" in flag:
        flag_kind = "dim"
    elif "D" in flag or "R" in flag:
        flag_kind = "warn"
    else:
        flag_kind = "plain"
    spans = (
        OutputSpan(text=f"{m.group(1):<3}", kind=flag_kind),
        OutputSpan(text=path, kind="plain"),
    )
    return _PreparedLine(spans=spans, kind="stdout")


# ─── Helpers used by multiple parsers ───


def _split_plain_lines(text: str, kind: str) -> list[_PreparedLine]:
    """Fallback parser: every raw line becomes one plain prepared line."""
    out: list[_PreparedLine] = []
    for raw in text.split("\n"):
        raw = raw.rstrip("\r")
        if not raw:
            out.append(_PreparedLine(spans=(OutputSpan(text="", kind="plain"),), kind=kind))
            continue
        out.append(_PreparedLine(
            spans=(OutputSpan(text=raw, kind="plain"),),
            kind=kind,
        ))
    # Drop trailing blanks
    while out and not out[-1].spans[0].text:
        out.pop()
    return out


def _query_from_search_params(card: ToolCard) -> str | None:
    """Extract the search query from a SEARCH card's params.

    The grep parser stores it under `pattern`; the find parser uses
    `name`. Returns None if neither is set.
    """
    for key, value in card.params:
        if key in ("pattern", "query", "name"):
            return value
    return None


# ─── The main class ───


DIFF_MAX_OUTPUT_LINES = 12


class PreparedToolOutput:
    """A card's output, parsed once and reusable at any width.

    Constructed from a ToolCard. Internally holds a list of
    _PreparedLine (semantic spans without wrapping). `layout(width)`
    hard-wraps each prepared line to the target width and returns
    a list of OutputLine objects ready for painting.

    Single-entry width-keyed cache: calling `layout(80)` twice in a
    row is free. Changing the width (resize) invalidates the cache
    and does one wrap pass.
    """

    __slots__ = ("_prepared", "_cache_key", "_cache_lines", "_has_semantic_diff")

    def __init__(self, card: ToolCard) -> None:
        self._prepared, self._has_semantic_diff = _prepare_for_card(card)
        # Cache keyed by (width, max_lines) so changes to either
        # invalidate a single entry. Both axes can change at render
        # time (resize → new width, max_lines stays constant in
        # practice but we key on it anyway for correctness).
        self._cache_key: tuple[int, int] = (-1, -1)
        self._cache_lines: list[OutputLine] = []

    @property
    def preferred_max_lines(self) -> int:
        return DIFF_MAX_OUTPUT_LINES if self._has_semantic_diff else 5

    def layout(
        self, width: int, *, max_lines: int | None = None,
    ) -> list[OutputLine]:
        """Return wrapped OutputLine rows for the target width.

        When `max_lines` is set, the result is clipped to a head of
        that many visible rows with one trailing "⋯ +N more lines ⋯"
        marker appended — a compact "streaming window" view that
        keeps long read/grep/ls output from flooding the chat with
        the full file/match dump. The settled card can still convey
        the gist without showing every line, while the full untrimmed
        output (up to `MAX_OUTPUT_BYTES`) still reaches the model via
        `_tool_card_content_for_api` so the next turn can reason
        about it.

        `max_lines=None` returns the full wrapped list (used by tests
        and by callers that want the raw body).
        """
        if width <= 0:
            return []
        effective_max = -1 if max_lines is None else int(max_lines)
        cache_key = (width, effective_max)
        if cache_key == self._cache_key:
            return self._cache_lines

        wrapped: list[OutputLine] = []
        for p in self._prepared:
            wrapped.extend(_wrap_prepared_line(p, width))

        if not wrapped:
            wrapped = [OutputLine(
                spans=(OutputSpan(text="(no output)", kind="dim"),),
                kind="truncated",
            )]
        elif max_lines is not None and len(wrapped) > max_lines:
            # Clip to a head window + one overflow marker. The
            # marker steals a row, so show (max_lines - 1) content
            # rows plus the marker (total = max_lines).
            head_rows = max(1, max_lines - 1)
            hidden = len(wrapped) - head_rows
            clipped: list[OutputLine] = wrapped[:head_rows]
            clipped.append(OutputLine(
                spans=(OutputSpan(
                    text=f"⋯ +{hidden} more line{'s' if hidden != 1 else ''} ⋯",
                    kind="dim",
                ),),
                kind="truncated",
            ))
            wrapped = clipped

        self._cache_key = cache_key
        self._cache_lines = wrapped
        return wrapped


# ─── Per-card preparation dispatcher ───


def _prepare_for_card(card: ToolCard) -> tuple[list[_PreparedLine], bool]:
    """Parse `card.output` + `card.stderr` into a list of prepared
    lines, using a verb-class-specific parser where available.

    Stderr always appends after stdout, as "stderr"-kind lines
    (warn-tinted at paint time).
    """
    cls = verb_class_for(card.verb, card.risk)
    prepared: list[_PreparedLine] = []
    has_semantic_diff = False

    stdout_raw = card.output or ""
    stderr_raw = card.stderr or ""

    change_artifact = card.change_artifact
    if change_artifact is None and _should_parse_unified_diff(card, stdout_raw):
        change_artifact = parse_unified_diff(stdout_raw)

    if change_artifact is not None and change_artifact.has_diff_rows:
        prepared.extend(_prepare_change_artifact(change_artifact))
        has_semantic_diff = True

    elif cls == VerbClass.SEARCH and card.verb == "search-content" and stdout_raw:
        query = _query_from_search_params(card)
        for raw in stdout_raw.split("\n"):
            raw = raw.rstrip("\r")
            if not raw:
                continue
            prepared.append(_parse_grep_line(raw, query))

    elif cls == VerbClass.LIST and stdout_raw:
        for raw in stdout_raw.split("\n"):
            raw = raw.rstrip("\r")
            if not raw:
                continue
            prepared.append(_parse_ls_line(raw))

    elif (
        cls == VerbClass.INSPECT
        and card.verb == "git-status"
        and stdout_raw
    ):
        for raw in stdout_raw.split("\n"):
            raw = raw.rstrip("\r")
            if not raw:
                continue
            prepared.append(_parse_git_status_line(raw))

    else:
        # Fallback: plain pass-through for all other classes
        prepared.extend(_split_plain_lines(stdout_raw, kind="stdout"))

    # Stderr is always appended as warn-colored lines regardless of class
    if stderr_raw:
        prepared.extend(_split_plain_lines(stderr_raw, kind="stderr"))

    return prepared, has_semantic_diff


def _should_parse_unified_diff(card: ToolCard, stdout_raw: str) -> bool:
    if not stdout_raw:
        return False
    if card.verb in ("git-diff", "git-show"):
        return True
    lines = stdout_raw.splitlines()
    return (
        any(line.startswith("diff --git ") for line in lines[:8])
        or (
            any(line.startswith("--- ") for line in lines[:8])
            and any(line.startswith("+++ ") for line in lines[:12])
            and any(line.startswith("@@ ") for line in lines[:20])
        )
    )


def _prepare_change_artifact(artifact: ChangeArtifact) -> list[_PreparedLine]:
    prepared: list[_PreparedLine] = []
    for line in artifact.prelude:
        prepared.append(_PreparedLine(
            spans=(OutputSpan(text=line, kind="dim"),),
            kind="diff_note",
        ))
    for file_change in artifact.files:
        prepared.extend(_prepare_changed_file(file_change))
    return prepared


def _prepare_changed_file(file_change: ChangedFile) -> list[_PreparedLine]:
    out: list[_PreparedLine] = []
    status_text = {
        "added": "added",
        "deleted": "deleted",
        "renamed": "renamed",
        "binary": "binary",
        "modified": "modified",
        "note": "changed",
    }.get(file_change.status, file_change.status)

    if file_change.old_path and file_change.old_path != file_change.path:
        header = (
            OutputSpan(text=f"{file_change.old_path} ", kind="dim"),
            OutputSpan(text="→", kind="chrome"),
            OutputSpan(text=f" {file_change.path}", kind="chrome"),
            OutputSpan(text=f"  [{status_text}]", kind="dim"),
        )
    else:
        header = (
            OutputSpan(text=file_change.path, kind="chrome"),
            OutputSpan(text=f"  [{status_text}]", kind="dim"),
        )
    out.append(_PreparedLine(spans=header, kind="diff_file"))

    for note in file_change.notes:
        out.append(_PreparedLine(
            spans=(OutputSpan(text=note, kind="dim"),),
            kind="diff_note",
        ))

    for hunk in file_change.hunks:
        if hunk.lines:
            out.append(_PreparedLine(
                spans=(OutputSpan(text=hunk.lines[0], kind="warn"),),
                kind="diff_hunk",
            ))
        for body_line in hunk.lines[1:]:
            if body_line.startswith("+"):
                kind = "diff_add"
            elif body_line.startswith("-"):
                kind = "diff_remove"
            elif body_line == r"\ No newline at end of file":
                kind = "diff_note"
            else:
                kind = "diff_context"
            out.append(_PreparedLine(
                spans=(OutputSpan(text=body_line, kind="plain"),),
                kind=kind,
            ))
    return out


# ─── Wrapping ───


def _wrap_prepared_line(p: _PreparedLine, width: int) -> list[OutputLine]:
    """Hard-wrap a prepared line to `width`. Preserves span kinds
    across wraps: if a long match span straddles a wrap boundary,
    both halves retain the `match` kind.

    Simple char-level greedy wrap (not word-aware) because command
    output frequently has no wrap-friendly whitespace (paths,
    JSON, code). The alternative would be a word-wrapper and a
    special case for long tokens; char-level is simpler and the
    result reads well for monospace output.
    """
    if not p.spans:
        return [OutputLine(spans=(), kind=p.kind)]

    # Fast path: total width fits
    total = sum(len(s.text) for s in p.spans)
    if total <= width:
        return [OutputLine(spans=p.spans, kind=p.kind)]

    # Slow path: slice across spans
    lines: list[OutputLine] = []
    current: list[OutputSpan] = []
    current_w = 0

    for span in p.spans:
        remaining = span.text
        while remaining:
            room = width - current_w
            if room <= 0:
                lines.append(OutputLine(spans=tuple(current), kind=p.kind))
                current = []
                current_w = 0
                room = width
            take = remaining[:room]
            current.append(OutputSpan(text=take, kind=span.kind))
            current_w += len(take)
            remaining = remaining[len(take):]

    if current:
        lines.append(OutputLine(spans=tuple(current), kind=p.kind))

    return lines
