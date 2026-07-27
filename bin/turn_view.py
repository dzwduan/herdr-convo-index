#!/usr/bin/env python3
"""Modal popup that shows one conversation turn in full.

The index pane spawns this with CONVO_TURN_FILE / CONVO_TURN_INDEX pointing at a
Claude Code or Codex transcript and a 1-based turn ordinal. Reading the
transcript rather than the pane scrollback is what makes arbitrarily old turns
reachable: herdr's pane.read caps out around 1000 lines.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import markdown as md  # noqa: E402
import transcript as tx  # noqa: E402
import tui  # noqa: E402

WHEEL_LINES = 3
GUTTER = "  "

USER_MARK = "❯"
TEXT_MARK = "●"
THINK_MARK = "✻"


def compose(turn, width):
    """Turn record to ANSI display lines, one block per marker."""
    body = max(1, width - len(GUTTER))
    lines = []

    def block(mark, mark_style, rendered):
        for i, line in enumerate(rendered or [""]):
            lines.append(f"{mark_style}{mark}{tui.RESET} {line}" if i == 0
                         else f"{GUTTER}{line}")

    block(USER_MARK, tui.ACCENT + tui.BOLD, md.render(turn["text"], body))
    for kind, text in turn["blocks"]:
        if kind not in tx.RENDERED_KINDS:
            continue
        lines.append("")
        if kind == "text":
            block(TEXT_MARK, tui.ACCENT, md.render(text, body))
        else:
            # Thinking stays unstyled prose: nesting dim under inline markup
            # would need every reset to restore it.
            block(THINK_MARK, tui.DIM,
                  [f"{tui.DIM}{line}{tui.RESET}" for line in tx.wrap(text, body)])
    return lines


def render(term, lines, top, header):
    rows, cols = term.measure()
    body_rows = max(1, rows - 2)
    top = max(0, min(top, max(0, len(lines) - body_rows)))

    out = [f"{tui.ACCENT}{tui.BOLD}{tx.pad(tx.fit(header, cols), cols)}{tui.RESET}"]
    for i in range(body_rows):
        out.append(lines[top + i] if top + i < len(lines) else "")
    end = min(len(lines), top + body_rows)
    status = f"{top + 1}-{end}/{len(lines)} · j/k g/G scroll · n/p turn · q closes"
    out.append(f"{tui.DIM}{tx.pad(tx.fit(status, cols), cols)}{tui.RESET}")
    term.draw(out)
    return top, body_rows


def main():
    if not sys.stdin.isatty():
        sys.stderr.write("convo-index turn view must run in a terminal pane\n")
        return 1

    raw_path = os.environ.get("CONVO_TURN_FILE") or ""
    try:
        ordinal = int(os.environ.get("CONVO_TURN_INDEX") or "0")
    except ValueError:
        ordinal = 0
    title = os.environ.get("CONVO_TURN_TITLE") or ""

    path = Path(raw_path) if raw_path else None
    if path is not None and not path.exists():
        path = None

    def load(n):
        return tx.load_turn(path, n) if path is not None and n > 0 else None

    turn = load(ordinal)

    def header_for():
        if turn is None:
            return "conversation turn"
        return f"#{ordinal} · {turn['stamp']}" + (f" · {title}" if title else "")

    def step(delta):
        """Move to an adjacent turn; stays put at either end of the transcript."""
        nonlocal turn, ordinal
        neighbour = load(ordinal + delta)
        if neighbour is None:
            return False
        turn, ordinal = neighbour, ordinal + delta
        return True

    top = 0
    width = 0
    lines = None
    reader = tui.InputReader()
    with tui.Term() as term:
        try:
            while True:
                _, cols = term.measure()
                if lines is None or cols != width:
                    width = cols
                    lines = (compose(turn, cols) if turn is not None else
                             [f"{tui.DIM}turn {ordinal} not found in "
                              f"{raw_path or '(no file)'}{tui.RESET}"])
                top, span = render(term, lines, top, header_for())

                for event in reader.poll(0.15):
                    if event[0] == "mouse":
                        if event[1] == tui.WHEEL_UP:
                            top -= WHEEL_LINES
                        elif event[1] == tui.WHEEL_DOWN:
                            top += WHEEL_LINES
                        continue
                    key = event[1]
                    if key in ("q", "esc", "\x03", "\x04"):
                        return 0
                    if key in ("n", "right", "\r", "\n"):
                        if step(1):
                            top, lines = 0, None
                    elif key in ("p", "left"):
                        if step(-1):
                            top, lines = 0, None
                    elif key in ("j", "down"):
                        top += 1
                    elif key in ("k", "up"):
                        top -= 1
                    elif key in ("pgdn", "\x06", " "):
                        top += span
                    elif key in ("pgup", "\x02"):
                        top -= span
                    elif key in ("g", "home"):
                        top = 0
                    elif key in ("G", "end"):
                        top = len(lines)
                    top = max(0, top)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
