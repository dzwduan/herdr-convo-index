"""Minimal terminal plumbing shared by the index pane and the turn popup.

Herdr forwards mouse events to pane apps that request tracking, so both views
enable SGR mouse reporting and parse clicks and wheel notches themselves.
"""

import os
import re
import select
import sys
import termios
import tty

RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
ITALIC = "\x1b[3m"
UNDERLINE = "\x1b[4m"
ACCENT = "\x1b[36m"
CODE = "\x1b[33m"
INVERT = "\x1b[7m"

ALT_SCREEN_ON = "\x1b[?1049h\x1b[?25l"
ALT_SCREEN_OFF = "\x1b[?25h\x1b[?1049l"
MOUSE_ON = "\x1b[?1000h\x1b[?1006h"
MOUSE_OFF = "\x1b[?1006l\x1b[?1000l"

SGR_MOUSE_RE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
CSI_KEYS = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end"}
TILDE_KEYS = {"1": "home", "4": "end", "5": "pgup", "6": "pgdn"}

WHEEL_UP = 64
WHEEL_DOWN = 65


class Term:
    """Alt-screen raw-mode terminal with mouse reporting."""

    def __init__(self, mouse=True):
        self.mouse = mouse
        self.fd = sys.stdin.fileno()
        self.saved = None
        self.rows = 24
        self.cols = 80

    def __enter__(self):
        self.saved = termios.tcgetattr(self.fd)
        sys.stdout.write(ALT_SCREEN_ON + (MOUSE_ON if self.mouse else ""))
        sys.stdout.flush()
        tty.setraw(self.fd)
        return self

    def __exit__(self, *_):
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        sys.stdout.write((MOUSE_OFF if self.mouse else "") + ALT_SCREEN_OFF)
        sys.stdout.flush()
        return False

    def measure(self):
        size = os.get_terminal_size()
        self.rows, self.cols = size.lines, size.columns
        # Leave the last column untouched: writing it triggers deferred line wrap
        # and the final glyph (usually a truncation ellipsis) gets dropped.
        return self.rows, max(1, self.cols - 1)

    def draw(self, lines):
        sys.stdout.write("\x1b[H" + "\x1b[K\r\n".join(lines[: self.rows]) + "\x1b[K")
        sys.stdout.flush()


class InputReader:
    """Buffers stdin and yields ('key', name) / ('mouse', button, col, row) events."""

    def __init__(self):
        self.buf = ""

    def poll(self, timeout):
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        return self.drain() if ready else self.idle()

    def drain(self):
        """Read whatever stdin has buffered and return the events it completes."""
        try:
            chunk = os.read(sys.stdin.fileno(), 4096)
        except OSError:
            chunk = b""
        if not chunk:
            return [("key", "q")]  # stdin closed; treat as quit
        return self.feed(chunk.decode("utf-8", "replace"))

    def idle(self):
        """Resolve input that only becomes unambiguous once nothing follows it."""
        if self.buf == "\x1b":
            self.buf = ""
            return [("key", "esc")]
        return []

    def feed(self, text):
        """Append raw input and drain the complete events it contains."""
        self.buf += text
        events = []
        while self.buf:
            event, consumed = self._parse()
            if consumed == 0:
                break  # incomplete sequence; wait for the rest
            self.buf = self.buf[consumed:]
            if event:
                events.append(event)
        return events

    def _parse(self):
        buf = self.buf
        if not buf.startswith("\x1b"):
            return ("key", buf[0]), 1
        if len(buf) == 1:
            return None, 0
        if buf[1] != "[":
            return ("key", "esc"), 1
        match = SGR_MOUSE_RE.match(buf)
        if match:
            button, col, row, kind = match.groups()
            event = ("mouse", int(button), int(col), int(row), kind == "M")
            return event, match.end()
        body = buf[2:]
        if body and body[0] in CSI_KEYS:
            return ("key", CSI_KEYS[body[0]]), 3
        tilde = body.find("~")
        if tilde >= 0:
            return ("key", TILDE_KEYS.get(body[:tilde], "esc")), tilde + 3
        if len(buf) > 32:
            return None, 1  # unrecognized noise; drop a byte and resync
        return None, 0
