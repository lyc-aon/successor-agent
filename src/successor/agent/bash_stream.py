"""Streaming bash block detector — finds fenced ```bash blocks in
incrementally-arriving model output.

The model produces content one ContentChunk at a time. A fenced
bash block looks like:

    Let me check the file.

    ```bash
    cat README.md
    ```

    Done.

The triple-backtick fence marker can split across chunks in any
position — `"`` "` in chunk N, `"`bash\n"` in chunk N+1, `"cat REA"`
in chunk N+2, `"DME.md\n```"` in chunk N+3 — and the detector has
to recognize them all.

The detector is a state machine with three states:
  TEXT          we're outside a code block, scanning for ```
  IN_FENCE_OPEN we just saw ``` and are reading the language tag
                (or whitespace until newline)
  IN_BASH       inside a ```bash block, accumulating content until
                the closing ``` arrives
  IN_OTHER      inside a non-bash code block (```python, ```json) —
                we ignore these

Public surface:
  BashStreamDetector — the state machine
    .feed(text)        consume a chunk; returns list of newly-completed
                       bash command strings
    .completed()       all completed blocks since construction
    .partial_buffer    in-progress block content (for diagnostics)
    .reset()           clear state — used at end-of-stream
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


# ─── Constants ───

# Recognized bash language tags. We accept several aliases because
# different models normalize differently.
BASH_LANGUAGE_TAGS: frozenset[str] = frozenset({
    "bash", "sh", "shell", "zsh", "fish",  # standard shells
    "console", "terminal",                  # documentation conventions
})

# Fence marker — must be EXACTLY three backticks at the start of a line.
# We don't support tilde-fenced blocks (``` only — no ~~~).
FENCE_MARKER: str = "```"


# ─── States ───


class _State(Enum):
    TEXT = "text"
    IN_FENCE_OPEN = "in_fence_open"  # just saw ```, reading lang tag
    IN_BASH = "in_bash"
    IN_OTHER = "in_other"  # ```python or other non-bash code block


# ─── Detector ───


@dataclass
class BashStreamDetector:
    """Stateful streaming detector for ```bash code blocks.

    Feed it ContentChunk text incrementally. Each call returns a
    list of NEWLY-completed bash command strings. The detector
    maintains internal state across calls, so a fence marker that
    spans two chunks is correctly handled.

    Trip semantics:
      - Only fences at the START of a line trigger detection. A
        triple-backtick mid-line is treated as inline code (no trip).
      - The closing fence MUST also be at the start of a line, otherwise
        the model is just typing literal backticks inside the block.
      - Empty bash blocks (``` bash\n``` ) yield empty strings, which
        callers should filter.
      - Multiple bash blocks in one stream are detected independently.

    The detector is `feed()`-driven, NOT line-driven, so chunks of
    any size work. We keep a small `_carry` buffer to handle partial
    fence markers split across chunks.
    """

    _state: _State = _State.TEXT
    _carry: str = ""  # accumulated text waiting for line resolution
    _block_buffer: str = ""  # content of the in-progress block
    _completed: list[str] = field(default_factory=list)
    _at_line_start: bool = True  # are we at the beginning of a line?

    # ─── Public API ───

    def feed(self, text: str) -> list[str]:
        """Consume a chunk of streamed model content.

        Returns a list of NEW bash command strings completed by this
        chunk. May be empty if the chunk didn't close any block.

        The text is processed character-by-character so partial fence
        markers across chunk boundaries are handled correctly. Any
        residual partial fence at the end of the chunk is buffered
        in _carry and prepended to the next feed() call.
        """
        if not text and not self._carry:
            return []

        # Prepend any carry from the previous chunk
        if self._carry:
            text = self._carry + text
            self._carry = ""

        new_completed: list[str] = []
        i = 0
        n = len(text)

        while i < n:
            # ─── State: TEXT (outside any block) ───
            if self._state == _State.TEXT:
                # Look for a fence at the start of a line. We need to
                # buffer the line so we can recognize fences that span
                # the chunk boundary.
                ch = text[i]
                if self._at_line_start and self._could_be_fence(text, i):
                    # Try to consume the full fence + language tag
                    consumed, lang = self._try_consume_fence_open(text, i)
                    if consumed > 0:
                        i += consumed
                        # Transition based on the language tag
                        if lang in BASH_LANGUAGE_TAGS:
                            self._state = _State.IN_BASH
                            self._block_buffer = ""
                        else:
                            self._state = _State.IN_OTHER
                        # Open fence consumes through the newline →
                        # next char is start of a new line
                        self._at_line_start = True
                        continue
                    # Otherwise this might be a partial fence — buffer
                    # the rest of the chunk and try again next call
                    self._carry = text[i:]
                    return new_completed
                # Just a regular char — track newline state and skip
                self._at_line_start = (ch == "\n")
                i += 1
                continue

            # ─── State: IN_BASH or IN_OTHER ───
            # Look for a closing fence at the start of a line.
            ch = text[i]
            if self._at_line_start and self._could_be_fence(text, i):
                consumed = self._try_consume_fence_close(text, i)
                if consumed > 0:
                    # Block closed — yield the ENTIRE block content as
                    # a single bash command. We deliberately do NOT
                    # split on newlines: bash itself parses multi-line
                    # scripts perfectly, including heredocs, quoted
                    # multi-line strings, if/then/fi blocks, function
                    # defs, and case statements. A naive line-splitter
                    # cannot understand any of those — it would turn
                    # `cat > f.html <<EOF\n<html>...\nEOF` into seven
                    # separate commands, each failing with
                    # "command not found". One fenced block = one
                    # command passed straight to dispatch_bash.
                    if self._state == _State.IN_BASH:
                        block_text = self._block_buffer.strip()
                        if block_text:
                            new_completed.append(block_text)
                            self._completed.append(block_text)
                    self._state = _State.TEXT
                    self._block_buffer = ""
                    i += consumed
                    # Close fence consumes through newline OR end-of-text
                    # → next char is at the start of a new line
                    self._at_line_start = True
                    continue
                # Could be a partial fence — buffer
                self._carry = text[i:]
                return new_completed

            # Regular char inside a block
            if self._state == _State.IN_BASH:
                self._block_buffer += ch
            self._at_line_start = (ch == "\n")
            i += 1

        return new_completed

    def flush(self) -> list[str]:
        """Force-resolve any in-progress state at end-of-stream.

        Call this when the stream ends so any pending close fence
        (e.g., a final ``` with no trailing newline) is recognized
        as a block closure rather than left dangling.

        Returns any newly-completed bash commands.
        """
        if not self._carry and self._state == _State.TEXT:
            return []

        new_completed: list[str] = []

        # Process any carry as if EOS arrived after it
        carry = self._carry
        self._carry = ""

        if self._state in (_State.IN_BASH, _State.IN_OTHER) and carry:
            # The carry might contain a closing fence
            stripped = carry.rstrip("\r\n \t")
            if stripped == "```" or stripped.endswith("\n```"):
                if self._state == _State.IN_BASH:
                    block_text = (self._block_buffer + carry.split("```")[0]).strip()
                    if block_text:
                        new_completed.append(block_text)
                        self._completed.append(block_text)
                self._state = _State.TEXT
                self._block_buffer = ""
            else:
                # Genuine in-progress block — append the carry to the buffer
                if self._state == _State.IN_BASH:
                    self._block_buffer += carry
        elif self._state == _State.IN_BASH and carry == "":
            # Buffer ended right before the closing fence we never saw
            pass

        return new_completed

    def completed(self) -> list[str]:
        """All bash commands completed since construction (or last reset)."""
        return list(self._completed)

    def partial_buffer(self) -> str:
        """The in-progress block buffer (for diagnostics / UI hints)."""
        return self._block_buffer

    def reset(self) -> None:
        """Clear all state. Use at end-of-stream so the next stream
        starts clean."""
        self._state = _State.TEXT
        self._carry = ""
        self._block_buffer = ""
        self._completed = []
        self._at_line_start = True

    def is_inside_block(self) -> bool:
        return self._state in (_State.IN_BASH, _State.IN_OTHER)

    # ─── Internal: fence parsing ───

    @staticmethod
    def _could_be_fence(text: str, i: int) -> bool:
        """Cheap pre-check — does text[i:] start with at least one backtick?"""
        return i < len(text) and text[i] == "`"

    def _try_consume_fence_open(self, text: str, i: int) -> tuple[int, str]:
        """Try to parse an opening ``` + language tag from text[i:].

        Returns (chars_consumed, lang_tag). lang_tag is "" if no
        language tag was given. chars_consumed is 0 if we couldn't
        recognize a complete fence (e.g., text ends mid-marker).
        """
        n = len(text)
        # Need at least 3 backticks
        if i + 3 > n:
            return (0, "")
        if text[i:i+3] != FENCE_MARKER:
            return (0, "")
        # Read the language tag — chars after the fence up to whitespace/newline
        j = i + 3
        lang_chars: list[str] = []
        while j < n and text[j] not in (" ", "\t", "\n", "\r"):
            lang_chars.append(text[j])
            j += 1
        # We need to consume up to and including the next newline so
        # the body starts cleanly. If we hit end-of-text without a
        # newline, that's a partial fence and we can't commit.
        # Skip whitespace/CR after the lang tag
        while j < n and text[j] in (" ", "\t", "\r"):
            j += 1
        if j >= n:
            return (0, "")
        if text[j] != "\n":
            # Anything other than EOL after the fence open is malformed.
            # Treat as not-a-fence (probably inline backticks).
            return (0, "")
        j += 1  # consume the newline
        return (j - i, "".join(lang_chars).lower())

    def _try_consume_fence_close(self, text: str, i: int) -> int:
        """Try to parse a closing ``` from text[i:].

        Returns chars_consumed or 0 if no closing fence here. The
        closing fence MUST be alone on its line. Accepts:
          - ``` followed by newline (consumes both)
          - ``` followed by trailing whitespace + newline (consumes all)
          - ``` at end-of-text (consumes just the fence)

        Returns 0 (NOT a partial-fence sentinel) if there's text after
        the fence on the same line, e.g. "```bash" — that's not a close.
        """
        n = len(text)
        if i + 3 > n:
            return 0
        if text[i:i+3] != FENCE_MARKER:
            return 0
        # Walk past the fence + any trailing whitespace
        j = i + 3
        while j < n and text[j] in (" ", "\t", "\r"):
            j += 1
        # End-of-text: this is a closing fence with no trailing newline.
        # The model often emits ``` as the last token. Consume it.
        if j >= n:
            return j - i
        # Newline: standard close. Consume it.
        if text[j] == "\n":
            return j + 1 - i
        # Anything else (a letter, etc.) means this wasn't actually a
        # close fence — it was an open fence we shouldn't be in this
        # state for, OR ``` followed by inline content. Reject.
        return 0

    # The old _split_block_into_commands was removed after a bug
    # report: a heredoc HTML write
    #     ```bash
    #     cat > foo.html <<'EOF'
    #     <!DOCTYPE html>
    #     <html>...</html>
    #     EOF
    #     ```
    # got split into seven separate "commands" (one per line), each
    # dispatched independently, each failing with "command not found"
    # on the HTML tag lines. Root cause: a line-level splitter cannot
    # understand heredocs, quoted multi-line strings, if/then/fi,
    # functions, or case statements. bash itself handles all of those
    # natively via subprocess.run(shell=True, executable="/bin/bash"),
    # so we now yield the entire fenced block as a single command and
    # let bash parse it.
