"""Real key parser — bytes from stdin → typed key events.

This is the foundational input layer that replaces the inline ESC
accumulator we had in `RoninChat`. It handles:

  - Printable ASCII (0x20-0x7E)
  - UTF-8 multi-byte sequences (accumulated and emitted as full chars)
  - Control codes (Ctrl+A through Ctrl+Z, plus Tab/Enter/Backspace)
  - CSI escape sequences (arrow keys, Page Up/Down, Home/End, F-keys)
  - SS3 / application cursor mode escape sequences
  - Bracketed paste (CSI 200~ ... 201~)
  - Bare ESC (with timeout, after a tick boundary)
  - Modifier-bearing CSI sequences (CSI 1;2A = Shift+Up, etc.)

Use:
    decoder = KeyDecoder()
    for byte in input_bytes:
        for event in decoder.feed(byte):
            handle(event)
    # On frame boundary, flush any pending bare-ESC etc:
    for event in decoder.flush():
        handle(event)

Each call to feed() emits 0 or more KeyEvents. The decoder maintains
internal state across calls, so a multi-byte sequence is reassembled
even if it arrives byte-by-byte.

The decoder is **pure** — it doesn't touch the terminal, doesn't block,
and has no I/O. It's a state machine over the input byte stream.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


# ─── Key types ───


class Key(Enum):
    """Symbolic key identifiers for non-character keys.

    Character input (printable + UTF-8) uses KeyEvent.char instead of
    a Key value, so this enum stays small and only covers things that
    don't have a meaningful Unicode codepoint.
    """

    # Editing
    ENTER = auto()
    BACKSPACE = auto()
    TAB = auto()
    SHIFT_TAB = auto()
    ESC = auto()       # bare ESC after timeout / flush

    # Navigation
    UP = auto()
    DOWN = auto()
    LEFT = auto()
    RIGHT = auto()
    HOME = auto()
    END = auto()
    PG_UP = auto()
    PG_DOWN = auto()
    INSERT = auto()
    DELETE = auto()

    # Function keys
    F1 = auto()
    F2 = auto()
    F3 = auto()
    F4 = auto()
    F5 = auto()
    F6 = auto()
    F7 = auto()
    F8 = auto()
    F9 = auto()
    F10 = auto()
    F11 = auto()
    F12 = auto()

    # Bracketed paste markers
    PASTE_START = auto()
    PASTE_END = auto()


# Modifier bitmask (matches xterm/CSI conventions where applicable).
MOD_NONE = 0
MOD_SHIFT = 1 << 0
MOD_ALT = 1 << 1
MOD_CTRL = 1 << 2


@dataclass(slots=True, frozen=True)
class KeyEvent:
    """A single decoded key press.

    Exactly one of `key` or `char` is set:
      - `key` is set for non-character keys (arrows, F-keys, etc.)
      - `char` is set for character input (printable ASCII or UTF-8)
      - For control codes (Ctrl+A etc.), `char` is the lowercase letter
        and `mods` has MOD_CTRL set
      - For PASTE_CHUNK events, `char` holds the pasted text chunk

    `mods` is a bitmask of MOD_SHIFT / MOD_ALT / MOD_CTRL.
    """

    key: Key | None = None
    char: str | None = None
    mods: int = MOD_NONE

    @property
    def is_char(self) -> bool:
        return self.key is None and self.char is not None and len(self.char) >= 1

    @property
    def is_ctrl(self) -> bool:
        return bool(self.mods & MOD_CTRL)

    @property
    def is_alt(self) -> bool:
        return bool(self.mods & MOD_ALT)

    @property
    def is_shift(self) -> bool:
        return bool(self.mods & MOD_SHIFT)


def key_name(event: KeyEvent) -> str:
    """Human-readable name for an event, useful for debugging."""
    parts: list[str] = []
    if event.is_ctrl:
        parts.append("Ctrl")
    if event.is_alt:
        parts.append("Alt")
    if event.is_shift:
        parts.append("Shift")
    if event.key is not None:
        parts.append(event.key.name)
    elif event.char is not None:
        if len(event.char) == 1 and event.char.isprintable():
            parts.append(event.char)
        else:
            parts.append(repr(event.char))
    return "+".join(parts) if parts else "?"


# ─── Lookup tables for ESC sequences ───
#
# CSI = ESC [ , SS3 = ESC O. Both are common ways terminals encode
# the same logical key depending on application/normal cursor mode.

# Pure CSI sequences (no parameters), keyed by the final byte after CSI.
_CSI_FINAL: dict[int, Key] = {
    ord("A"): Key.UP,
    ord("B"): Key.DOWN,
    ord("C"): Key.RIGHT,
    ord("D"): Key.LEFT,
    ord("H"): Key.HOME,
    ord("F"): Key.END,
    ord("Z"): Key.SHIFT_TAB,  # CSI Z = back-tab
}

# SS3 sequences (ESC O <final>) — application cursor mode.
_SS3_FINAL: dict[int, Key] = {
    ord("A"): Key.UP,
    ord("B"): Key.DOWN,
    ord("C"): Key.RIGHT,
    ord("D"): Key.LEFT,
    ord("H"): Key.HOME,
    ord("F"): Key.END,
    ord("P"): Key.F1,
    ord("Q"): Key.F2,
    ord("R"): Key.F3,
    ord("S"): Key.F4,
}

# CSI <number>~ sequences, keyed by the parameter number.
_CSI_TILDE: dict[int, Key] = {
    1: Key.HOME,
    2: Key.INSERT,
    3: Key.DELETE,
    4: Key.END,
    5: Key.PG_UP,
    6: Key.PG_DOWN,
    7: Key.HOME,
    8: Key.END,
    11: Key.F1,
    12: Key.F2,
    13: Key.F3,
    14: Key.F4,
    15: Key.F5,
    17: Key.F6,
    18: Key.F7,
    19: Key.F8,
    20: Key.F9,
    21: Key.F10,
    23: Key.F11,
    24: Key.F12,
}

# CSI 200 ~ starts a paste, CSI 201 ~ ends one. We treat these as
# special markers — see _handle_csi_tilde.
_PASTE_START_NUM = 200
_PASTE_END_NUM = 201


def _utf8_seq_len(first_byte: int) -> int:
    """Number of bytes in a UTF-8 sequence given the leading byte.

    Returns 1 for ASCII, 2-4 for multi-byte, 0 for an invalid lead.
    """
    if first_byte < 0x80:
        return 1
    if first_byte < 0xC0:
        return 0  # continuation byte appearing where a lead is expected
    if first_byte < 0xE0:
        return 2
    if first_byte < 0xF0:
        return 3
    if first_byte < 0xF8:
        return 4
    return 0


# ─── Decoder state machine ───


class _State:
    GROUND = "ground"           # waiting for next byte
    UTF8 = "utf8"               # accumulating UTF-8 continuation bytes
    ESC_SEEN = "esc_seen"       # saw ESC, waiting for [ or O or another byte
    CSI_PARAMS = "csi_params"   # in ESC [ , collecting parameter bytes
    SS3 = "ss3"                 # in ESC O , next byte is the final
    PASTE = "paste"             # inside CSI 200 ~ ... CSI 201 ~


class KeyDecoder:
    """Stateful byte → KeyEvent decoder.

    Call `.feed(byte)` for each input byte and iterate the returned
    list of events. Call `.flush()` at frame boundaries to drain any
    pending bare ESC.

    The decoder is **stateful** but **non-blocking** and **side-effect-
    free** — it never touches the terminal or any other I/O.
    """

    def __init__(self) -> None:
        self._state: str = _State.GROUND

        # UTF-8 accumulation
        self._utf8_buf: bytearray = bytearray()
        self._utf8_remaining: int = 0

        # ESC sequence accumulation
        self._csi_params: bytearray = bytearray()  # bytes between CSI and final
        self._esc_pending: bool = False             # True after a bare ESC

        # Paste accumulation
        self._paste_buf: bytearray = bytearray()
        # End-of-paste detection: scan _paste_buf for ESC[201~
        self._paste_end_match: int = 0  # current match length within b"\x1b[201~"

    # ─── public ───

    def feed(self, byte: int) -> list[KeyEvent]:
        """Feed a single byte. Returns 0 or more KeyEvents."""
        out: list[KeyEvent] = []
        self._step(byte, out)
        return out

    def feed_bytes(self, data: bytes | bytearray) -> list[KeyEvent]:
        """Feed multiple bytes. Returns 0 or more KeyEvents."""
        out: list[KeyEvent] = []
        for b in data:
            self._step(b, out)
        return out

    def flush(self) -> list[KeyEvent]:
        """Drain pending sequences at frame boundaries.

        A bare ESC press leaves the decoder in ESC_SEEN with no follow-
        up bytes. We treat that as Key.ESC after the next flush. UTF-8
        and CSI accumulators are also reset (anything incomplete is
        treated as garbage and dropped).
        """
        out: list[KeyEvent] = []
        if self._state == _State.ESC_SEEN and self._esc_pending:
            out.append(KeyEvent(key=Key.ESC))
            self._esc_pending = False
            self._state = _State.GROUND
        elif self._state in (_State.UTF8, _State.CSI_PARAMS, _State.SS3):
            # Drop the incomplete sequence.
            self._reset()
        return out

    # ─── core state machine ───

    def _step(self, byte: int, out: list[KeyEvent]) -> None:
        st = self._state

        if st == _State.GROUND:
            self._step_ground(byte, out)
            return
        if st == _State.UTF8:
            self._step_utf8(byte, out)
            return
        if st == _State.ESC_SEEN:
            self._step_esc_seen(byte, out)
            return
        if st == _State.CSI_PARAMS:
            self._step_csi_params(byte, out)
            return
        if st == _State.SS3:
            self._step_ss3(byte, out)
            return
        if st == _State.PASTE:
            self._step_paste(byte, out)
            return

    def _step_ground(self, byte: int, out: list[KeyEvent]) -> None:
        # ESC starts a sequence
        if byte == 0x1B:
            self._state = _State.ESC_SEEN
            self._esc_pending = True
            return

        # Tab / Enter / Backspace are common control codes
        if byte == 0x09:
            out.append(KeyEvent(key=Key.TAB))
            return
        if byte == 0x0D or byte == 0x0A:
            out.append(KeyEvent(key=Key.ENTER))
            return
        if byte == 0x7F or byte == 0x08:
            out.append(KeyEvent(key=Key.BACKSPACE))
            return

        # Ctrl+letter (0x01..0x1A → A..Z) — but skip 0x09 (tab),
        # 0x0a/0x0d (enter), 0x08 (backspace) which we just handled.
        if 0x01 <= byte <= 0x1A:
            letter = chr(byte + ord("a") - 1)
            out.append(KeyEvent(char=letter, mods=MOD_CTRL))
            return

        # Printable ASCII
        if 0x20 <= byte < 0x7F:
            out.append(KeyEvent(char=chr(byte)))
            return

        # UTF-8 lead byte (high bit set)
        if byte >= 0x80:
            seq_len = _utf8_seq_len(byte)
            if seq_len == 0:
                # Invalid lead — drop it.
                return
            if seq_len == 1:
                # Shouldn't happen for high-bit, but be safe.
                out.append(KeyEvent(char=chr(byte)))
                return
            self._utf8_buf = bytearray([byte])
            self._utf8_remaining = seq_len - 1
            self._state = _State.UTF8
            return

        # Other control codes (0x1C-0x1F): ignore for v0.

    def _step_utf8(self, byte: int, out: list[KeyEvent]) -> None:
        # Continuation bytes have the form 10xxxxxx
        if (byte & 0xC0) != 0x80:
            # Invalid — drop the partial sequence and re-process this
            # byte from ground state.
            self._reset()
            self._step_ground(byte, out)
            return
        self._utf8_buf.append(byte)
        self._utf8_remaining -= 1
        if self._utf8_remaining == 0:
            try:
                ch = self._utf8_buf.decode("utf-8")
            except UnicodeDecodeError:
                ch = None
            self._reset()
            if ch is not None and len(ch) >= 1:
                out.append(KeyEvent(char=ch))

    def _step_esc_seen(self, byte: int, out: list[KeyEvent]) -> None:
        self._esc_pending = False
        if byte == ord("["):
            self._state = _State.CSI_PARAMS
            self._csi_params = bytearray()
            return
        if byte == ord("O"):
            self._state = _State.SS3
            return
        # Alt+<char> — terminals encode this as ESC followed by the char.
        # We treat it as a key event with MOD_ALT.
        if 0x20 <= byte < 0x7F:
            self._state = _State.GROUND
            out.append(KeyEvent(char=chr(byte), mods=MOD_ALT))
            return
        if byte == 0x1B:
            # Two ESCs in a row — emit Key.ESC for the first, treat the
            # second as starting a new sequence.
            out.append(KeyEvent(key=Key.ESC))
            self._esc_pending = True
            return
        # Anything else: emit ESC and re-process from ground.
        out.append(KeyEvent(key=Key.ESC))
        self._state = _State.GROUND
        self._step_ground(byte, out)

    def _step_csi_params(self, byte: int, out: list[KeyEvent]) -> None:
        # Parameter bytes: 0x30-0x3F (digits, ;, :)
        # Intermediate bytes: 0x20-0x2F (space etc., rare)
        # Final byte: 0x40-0x7E
        if 0x30 <= byte <= 0x3F or 0x20 <= byte <= 0x2F:
            self._csi_params.append(byte)
            return
        if 0x40 <= byte <= 0x7E:
            # Final byte — dispatch.
            params = bytes(self._csi_params)
            self._csi_params = bytearray()
            self._state = _State.GROUND
            self._dispatch_csi(params, byte, out)
            return
        # Invalid byte in CSI — drop the sequence.
        self._reset()

    def _step_ss3(self, byte: int, out: list[KeyEvent]) -> None:
        self._state = _State.GROUND
        if byte in _SS3_FINAL:
            out.append(KeyEvent(key=_SS3_FINAL[byte]))

    def _step_paste(self, byte: int, out: list[KeyEvent]) -> None:
        """Accumulate paste bytes until we see CSI 201~."""
        end_marker = b"\x1b[201~"
        # Streaming match against the end marker
        if byte == end_marker[self._paste_end_match]:
            self._paste_end_match += 1
            if self._paste_end_match == len(end_marker):
                # Paste complete. Emit the chunk + the end marker.
                if self._paste_buf:
                    try:
                        text = bytes(self._paste_buf).decode("utf-8", errors="replace")
                    except Exception:
                        text = ""
                    if text:
                        out.append(KeyEvent(char=text))
                out.append(KeyEvent(key=Key.PASTE_END))
                self._paste_buf = bytearray()
                self._paste_end_match = 0
                self._state = _State.GROUND
            return
        # Not a match — flush the partial match into the paste buf and
        # re-attempt against the current byte from the start.
        if self._paste_end_match > 0:
            self._paste_buf.extend(end_marker[: self._paste_end_match])
            self._paste_end_match = 0
            # Re-process this byte: it might start a new partial match
            if byte == end_marker[0]:
                self._paste_end_match = 1
                return
        self._paste_buf.append(byte)

    # ─── CSI dispatch ───

    def _dispatch_csi(
        self,
        params: bytes,
        final: int,
        out: list[KeyEvent],
    ) -> None:
        """Map a parsed CSI sequence to a KeyEvent.

        Handles:
          CSI <final>            (no params)        → arrow keys, Home, End
          CSI <num>~             (number ; tilde)   → PgUp/PgDn, F-keys
          CSI <num1>;<num2><fin> (modifier-bearing) → e.g. Shift+Up
          CSI 200~ / CSI 201~    (bracketed paste)
        """
        # Parse parameters as integers separated by ';'
        nums: list[int] = []
        if params:
            for part in params.split(b";"):
                if part.isdigit():
                    nums.append(int(part))
                else:
                    nums.append(0)

        # Bracketed paste markers
        if final == ord("~"):
            if not nums:
                return
            n = nums[0]
            if n == _PASTE_START_NUM:
                self._state = _State.PASTE
                self._paste_buf = bytearray()
                self._paste_end_match = 0
                out.append(KeyEvent(key=Key.PASTE_START))
                return
            if n == _PASTE_END_NUM:
                # We shouldn't see this in CSI dispatch — paste end is
                # caught inside _step_paste. But if we somehow get it
                # here, just emit the marker.
                out.append(KeyEvent(key=Key.PASTE_END))
                return
            if n in _CSI_TILDE:
                key = _CSI_TILDE[n]
                mods = _decode_modifier(nums[1] if len(nums) > 1 else 1)
                out.append(KeyEvent(key=key, mods=mods))
                return
            return

        # CSI <final> — arrow keys, Home, End, etc.
        if final in _CSI_FINAL:
            key = _CSI_FINAL[final]
            # Modifier comes from second param when present (e.g. CSI 1;2A = Shift+Up)
            mods = MOD_NONE
            if len(nums) >= 2:
                mods = _decode_modifier(nums[1])
            out.append(KeyEvent(key=key, mods=mods))
            return

        # Unknown CSI — silently ignore.

    # ─── helpers ───

    def _reset(self) -> None:
        """Reset all transient state to GROUND."""
        self._state = _State.GROUND
        self._utf8_buf = bytearray()
        self._utf8_remaining = 0
        self._csi_params = bytearray()
        self._esc_pending = False
        self._paste_buf = bytearray()
        self._paste_end_match = 0


def _decode_modifier(n: int) -> int:
    """Translate the xterm modifier number into our MOD_ bitmask.

    xterm modifier values (with offset of 1):
      1 = none, 2 = Shift, 3 = Alt, 4 = Shift+Alt,
      5 = Ctrl,  6 = Shift+Ctrl, 7 = Alt+Ctrl, 8 = Shift+Alt+Ctrl
    """
    if n <= 1:
        return MOD_NONE
    bits = n - 1
    mods = MOD_NONE
    if bits & 1:
        mods |= MOD_SHIFT
    if bits & 2:
        mods |= MOD_ALT
    if bits & 4:
        mods |= MOD_CTRL
    return mods
