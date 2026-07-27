"""Plugin settings, shaped after the `ui.sidebar_*` keys herdr reads for its own.

herdr hands every plugin a config directory, so the index takes its settings
from `config.toml` there rather than from the environment of whatever process
happened to launch the pane. The parser understands flat `key = value` lines
only: three settings do not justify a dependency, and tomllib would cost
python 3.8. Environment variables still win, for a one-off override.

    width           = 34         # ui.sidebar_width
    collapsed_mode  = "compact"  # ui.sidebar_collapsed_mode: compact
    start_collapsed = false      # ui.sidebar_start_collapsed
"""

import os
from pathlib import Path

DEFAULTS = {"width": 34, "collapsed_mode": "compact", "start_collapsed": False}
ENV_KEYS = {
    "width": "CONVO_INDEX_WIDTH",
    "collapsed_mode": "CONVO_INDEX_COLLAPSED_MODE",
    "start_collapsed": "CONVO_INDEX_START_COLLAPSED",
}
MODES = ("compact",)
MIN_WIDTH = 16


def config_values(directory=None):
    """The `key = value` pairs in the plugin's config.toml, as raw strings."""
    directory = directory or os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if not directory:
        return {}
    try:
        text = (Path(directory) / "config.toml").read_text()
    except OSError:
        return {}
    values = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        key, sep, raw = line.partition("=")
        if sep:
            values[key.strip()] = raw.strip().strip("\"'")
    return values


def load(directory=None):
    """Settings; environment beats config.toml, which beats the defaults."""
    values = config_values(directory)
    out = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        raw = os.environ.get(ENV_KEYS[key]) or values.get(key)
        if raw is None:
            continue
        if isinstance(default, bool):
            out[key] = raw.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(default, int):
            try:
                out[key] = int(raw)
            except ValueError:
                pass
        else:
            out[key] = raw
    out["width"] = max(MIN_WIDTH, out["width"])
    if out["collapsed_mode"] not in MODES:
        out["collapsed_mode"] = DEFAULTS["collapsed_mode"]
    return out
