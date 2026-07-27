#!/usr/bin/env python3
"""Open the conversation index as a right-hand split, or close it if already open.

The opened pane id is remembered in the plugin state dir so a second invocation
closes the same pane instead of stacking another split.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

HERDR_BIN = os.environ.get("HERDR_BIN_PATH") or "herdr"
PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID") or "convo.index"
STATE_DIR = Path(
    os.environ.get("HERDR_PLUGIN_STATE_DIR") or Path.home() / ".local/state/herdr/convo.index"
)
STATE_FILE = STATE_DIR / "panes.json"

try:
    DESIRED_COLS = max(16, int(os.environ.get("CONVO_INDEX_WIDTH", "34")))
except ValueError:
    DESIRED_COLS = 34


def herdr(*args):
    try:
        proc = subprocess.run([HERDR_BIN, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout).get("result")
    except ValueError:
        return None


def live_pane_ids():
    result = herdr("pane", "list") or {}
    return {p.get("pane_id") for p in result.get("panes", [])}


def invoking_context():
    """Workspace and pane the index should split off, from the invocation context."""
    try:
        context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except ValueError:
        context = {}
    workspace = context.get("workspace_id") or ""
    target = context.get("focused_pane_id") or ""
    if workspace and target:
        return workspace, target
    result = herdr("pane", "list") or {}
    for pane in result.get("panes", []):
        if pane.get("focused"):
            return workspace or pane.get("workspace_id") or "", target or pane.get("pane_id") or ""
    return workspace, target


def load_state():
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def narrow(pane_id):
    """Shrink the index split toward DESIRED_COLS.

    `pane resize` moves the shared boundary by a ratio of the parent split, so
    the target ratio is approached iteratively instead of resolving which split
    node owns the pane.
    """
    for _ in range(4):
        layout = (herdr("pane", "layout", "--pane", pane_id) or {}).get("layout")
        if not layout:
            return
        area = layout.get("area") or {}
        total = area.get("width") or 0
        rect = next(
            (p.get("rect") for p in layout.get("panes", []) if p.get("pane_id") == pane_id), None
        )
        if not rect or total <= 0:
            return
        delta = rect.get("width", 0) - DESIRED_COLS
        if abs(delta) <= 2:
            return
        amount = round(abs(delta) / total, 4)
        if amount < 0.001:
            return
        direction = "right" if delta > 0 else "left"
        if not herdr("pane", "resize", "--direction", direction, "--amount", str(amount), "--pane", pane_id):
            return


def main():
    workspace, target = invoking_context()
    state = load_state()
    live = live_pane_ids()
    state = {ws: pid for ws, pid in state.items() if pid in live}

    existing = state.get(workspace)
    if existing:
        herdr("plugin", "pane", "close", existing)
        state.pop(workspace, None)
        save_state(state)
        return 0

    if not target:
        sys.stderr.write("no focused pane to split from\n")
        return 1
    result = herdr(
        "plugin", "pane", "open",
        "--plugin", PLUGIN_ID,
        "--entrypoint", "index",
        "--placement", "split",
        "--direction", "right",
        "--target-pane", target,
        "--no-focus",
    )
    pane_id = ((result or {}).get("plugin_pane") or {}).get("pane", {}).get("pane_id")
    if not pane_id:
        sys.stderr.write("failed to open conversation index pane\n")
        return 1

    narrow(pane_id)
    state[workspace] = pane_id
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
