#!/usr/bin/env python3
"""Read-only conversation index for the Claude Code pane that currently has focus.

Herdr exposes each pane's Claude session id through its socket API; Claude Code
stores every turn in ~/.claude/projects/<slug>/<session-id>.jsonl. This pane
joins the two and renders one line per real user turn, so scrolled-off turns
stay reachable without touching the agent pane's scrollback.

Clicking a turn (or pressing enter) opens it in full in a session-modal popup.
The agent pane itself is never scrolled: the herdr socket API reports scroll
position but has no command to set it.
"""

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import herdr_api as api  # noqa: E402
import transcript as tx  # noqa: E402
import tui  # noqa: E402

HERDR_BIN = os.environ.get("HERDR_BIN_PATH") or "herdr"
PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID") or "convo.index"
SELF_PANE = os.environ.get("HERDR_PANE_ID") or ""

# The event stream drives retargeting; these are only safety nets for a socket
# that died or a transcript the filesystem has not flushed yet.
FALLBACK_POLL_SEC = 8.0
POLL_SEC = 1.2  # used instead of FALLBACK_POLL_SEC while the stream is down
FILE_POLL_SEC = 0.6
TICK_SEC = 0.15
WHEEL_LINES = 3

HEADER_ROWS = 1
FOOTER_ROWS = 1


def _own_workspace():
    """Scope following to this pane's workspace so each space keeps its own index."""
    try:
        context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except ValueError:
        context = {}
    return context.get("workspace_id") or ""


SELF_WORKSPACE = _own_workspace()


# --- herdr ------------------------------------------------------------------


def pane_list():
    return api.request("pane.list")


def focused_agent_pane():
    """Focused pane that owns a Claude session, or None while focus is elsewhere."""
    result = pane_list()
    if not result:
        return None
    for pane in result.get("panes", []):
        if not pane.get("focused"):
            continue
        if pane.get("pane_id") == SELF_PANE:
            return None  # focus is on this index pane; keep the previous target
        if SELF_WORKSPACE and pane.get("workspace_id") != SELF_WORKSPACE:
            return None  # focus moved to another space; keep showing this one
        session = pane.get("agent_session") or {}
        if session.get("kind") != "id" or not session.get("value"):
            return None
        return {
            "pane_id": pane.get("pane_id", ""),
            "session_id": session["value"],
            "agent": pane.get("agent") or session.get("agent") or "agent",
            "cwd": pane.get("cwd") or "",
            "title": pane.get("terminal_title_stripped") or pane.get("tab_id") or "",
            "workspace": pane.get("workspace_id") or "",
        }
    return None


def open_turn_popup(path, ordinal, title):
    """Spawn the turn view as a modal popup; errors are non-fatal for the index."""
    try:
        subprocess.Popen(
            [
                HERDR_BIN, "plugin", "pane", "open",
                "--plugin", PLUGIN_ID,
                "--entrypoint", "turn",
                "--placement", "popup",
                "--width", "80%",
                "--height", "80%",
                "--focus",
                "--env", f"CONVO_TURN_FILE={path}",
                "--env", f"CONVO_TURN_INDEX={ordinal}",
                "--env", f"CONVO_TURN_TITLE={title}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        pass


# --- view state -------------------------------------------------------------


class View:
    def __init__(self):
        self.rows = 24
        self.cols = 40
        self.top = 0
        self.cursor = 0
        self.follow = True
        self.query = ""
        self.typing = False
        self.rows_shown = []  # entries currently listed, after filtering

    @property
    def body_rows(self):
        return max(1, self.rows - HEADER_ROWS - FOOTER_ROWS)

    def apply(self, entries):
        """Recompute the visible rows for the current query."""
        if not self.query:
            self.rows_shown = list(entries)
        else:
            needle = self.query.lower()
            self.rows_shown = [
                e for e in entries
                if e["kind"] == "turn" and needle in e["summary"].lower()
            ]
        return self.rows_shown

    def row_to_index(self, row):
        """Terminal row (1-based) to list index, or None outside the list body."""
        offset = row - 1 - HEADER_ROWS
        if 0 <= offset < self.body_rows:
            return self.top + offset
        return None

    def clamp(self):
        count = len(self.rows_shown)
        if count == 0:
            self.top = self.cursor = 0
            return
        self.cursor = max(0, min(self.cursor, count - 1))
        span = self.body_rows
        self.top = max(0, min(self.top, max(0, count - span)))
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + span:
            self.top = self.cursor - span + 1

    def scroll(self, delta):
        span = self.body_rows
        count = len(self.rows_shown)
        self.top = max(0, min(self.top + delta, max(0, count - span)))
        self.cursor = max(self.top, min(self.cursor, self.top + span - 1))
        self.follow = False

    def selected(self):
        if 0 <= self.cursor < len(self.rows_shown):
            entry = self.rows_shown[self.cursor]
            if entry["kind"] == "turn":
                return entry
        return None


def entry_line(entry, cols, width_no, selected):
    if entry["kind"] == "break":
        label = f" compacted {entry['stamp']} "
        rule = "─" * max(0, cols - tx.cell_width(label))
        return f"{tui.DIM}{tx.fit(rule[: len(rule) // 2] + label + rule[len(rule) // 2 :], cols)}{tui.RESET}"
    prefix = f"{str(entry['ordinal']).rjust(width_no)} {entry['stamp']} {tx.size_bar(entry['weight'])} "
    body = tx.fit(entry["summary"], max(0, cols - tx.cell_width(prefix)))
    if selected:
        return f"{tui.INVERT}{tx.pad(prefix + body, cols)}{tui.RESET}"
    tail = " " * max(0, cols - tx.cell_width(prefix + body))
    return f"{tui.DIM}{prefix}{tui.RESET}{body}{tail}"


def render(term, view, target, index, streaming):
    view.rows, cols = term.measure()
    view.cols = cols
    entries = index.entries if index else []
    shown = view.apply(entries)
    view.clamp()

    if target:
        head = f"{target['agent']} · {target['title']}" if target["title"] else target["agent"]
    else:
        head = "waiting for a Claude pane…"
    lines = [f"{tui.ACCENT}{tui.BOLD}{tx.pad(tx.fit(head, cols), cols)}{tui.RESET}"]

    width_no = len(str(max(1, index.count if index else 1)))
    for row in range(view.body_rows):
        i = view.top + row
        lines.append(
            entry_line(shown[i], cols, width_no, i == view.cursor)
            if i < len(shown) else " " * cols
        )

    if view.typing:
        status = f"/{view.query}▏"
    elif index and index.error:
        status = f"error: {index.error}"
    elif not target:
        status = "focus a Claude pane"
    elif index is None:
        status = "session file not found"
    elif view.query:
        status = f"/{view.query} · {len(shown)}/{index.count} · esc clears"
    else:
        state = "follow" if view.follow else "manual"
        if not streaming:
            state += " · polling"
        status = f"{index.count} turns · {state} · / q"
    lines.append(f"{tui.DIM}{tx.pad(tx.fit(status, cols), cols)}{tui.RESET}")
    term.draw(lines)


def handle(event, view, index, target):
    """Apply one input event; return False to quit."""
    span = view.body_rows
    count = len(view.rows_shown)

    def open_selected():
        entry = view.selected()
        if index and entry:
            open_turn_popup(index.path, entry["ordinal"], (target or {}).get("title", ""))

    if event[0] == "mouse":
        _, button, _col, row, pressed = event
        if button == tui.WHEEL_UP:
            view.scroll(-WHEEL_LINES)
        elif button == tui.WHEEL_DOWN:
            view.scroll(WHEEL_LINES)
        elif button == 0 and pressed:
            hit = view.row_to_index(row)
            if hit is not None and hit < count:
                view.cursor = hit
                view.follow = False
                view.typing = False
                open_selected()
        return True

    key = event[1]

    if view.typing:
        if key in ("esc", "\x03"):
            view.query = ""
            view.typing = False
        elif key in ("\r", "\n"):
            view.typing = False
        elif key in ("\x7f", "\b"):
            view.query = view.query[:-1]
        elif len(key) == 1 and key >= " ":
            view.query += key
        view.cursor = 0
        view.top = 0
        view.follow = False
        return True

    if key in ("q", "\x03", "\x04"):
        return False
    if key == "/":
        view.typing = True
        view.query = ""
    elif key == "esc":
        view.query = ""
    elif key in ("\r", "\n"):
        open_selected()
    elif key in ("j", "down"):
        view.cursor += 1
        view.follow = view.cursor >= count - 1
    elif key in ("k", "up"):
        view.cursor -= 1
        view.follow = False
    elif key in ("pgdn", "\x06"):
        view.cursor += span
        view.follow = view.cursor >= count - 1
    elif key in ("pgup", "\x02"):
        view.cursor -= span
        view.follow = False
    elif key in ("g", "home"):
        view.cursor = 0
        view.follow = False
    elif key in ("G", "end"):
        view.cursor = max(0, count - 1)
        view.follow = True
    elif key == "f":
        view.follow = not view.follow
    return True


def main():
    if not sys.stdin.isatty():
        sys.stderr.write("convo-index must run in a terminal pane\n")
        return 1

    view = View()
    reader = tui.InputReader()
    stream = api.EventStream()
    target = None
    index = None
    retarget_due = True
    last_retarget = 0.0
    last_file = 0.0

    def adopt(found):
        nonlocal target, index
        if not found:
            return
        if target is None or found["session_id"] != target["session_id"]:
            target = found
            path = tx.session_path(found["session_id"], found["cwd"])
            index = tx.SessionIndex(path) if path else None
            view.top = view.cursor = 0
            view.follow = True
            view.query = ""
            view.typing = False
            return
        target = found
        if index is None:  # session file may appear only after the first turn
            path = tx.session_path(found["session_id"], found["cwd"])
            index = tx.SessionIndex(path) if path else None

    with tui.Term() as term:
        try:
            while True:
                now = time.monotonic()
                idle_gap = FALLBACK_POLL_SEC if stream.connected else POLL_SEC
                if retarget_due or now - last_retarget >= idle_gap:
                    retarget_due = False
                    last_retarget = now
                    adopt(focused_agent_pane())
                    if not stream.connected:
                        stream.connect()

                if index is not None and now - last_file >= FILE_POLL_SEC:
                    last_file = now
                    if index.refresh() and view.follow and not view.query:
                        view.cursor = max(0, len(index.entries) - 1)

                render(term, view, target, index, stream.connected)

                watched = [sys.stdin]
                if stream.fileno() is not None:
                    watched.append(stream.sock)
                ready, _, _ = select.select(watched, [], [], TICK_SEC)

                events = reader.drain() if sys.stdin in ready else reader.idle()
                if stream.sock is not None and stream.sock in ready:
                    if stream.drain():
                        retarget_due = True
                for event in events:
                    if not handle(event, view, index, target):
                        return 0
        except KeyboardInterrupt:
            return 0
        finally:
            stream.close()


if __name__ == "__main__":
    sys.exit(main())
