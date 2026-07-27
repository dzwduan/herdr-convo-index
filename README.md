# herdr conversation index

A [herdr](https://herdr.dev) plugin that keeps a Claude Code or Codex session
navigable.

A long agent session scrolls its own history away: the pane shows the current
turn, and everything before it is somewhere up in the scrollback. This plugin
docks a narrow index beside the pane listing every turn you asked, and opens any
one of them — prompt and reply, in full — in a popup.

```
┌──────────────────────────────┬────────────────────────────────┐
│ codex                        │ codex · 讲解 L2 prefetch…      │
│                              │  1 11:13 ▃ 讲解 L2 prefetch…   │
│ (only the current turn is    │  2 11:36 ▆ 我理解是先 prefe…   │
│  still on screen)            │  3 11:40 ▂ 这里是我对 pref…    │
│                              │ ──── compacted 12:20 ────      │
│                              │  4 12:43 ▇ 这个改动的风险在…   │
│                              │ 12 turns · follow · / q        │
└──────────────────────────────┴────────────────────────────────┘
                                        │ click, or press enter
                                        ▼
        ┌────────────────────────────────────────────────┐
        │ #3 · 11:40 · 讲解 L2 prefetch…                 │
        │ ❯ 这里是我对 prefetch 的理解…                  │
        │                                                │
        │ ● 你的理解基本对，两处需要修正…                │
        │                                                │
        │   区别                                         │
        │   ────────────────────────────────────────     │
        │   触发方  │ 粒度                               │
        │   ────────┼────────────────────────────────    │
        │   硬件    │ cache line                         │
        │                                                │
        │ 1-42/318 · j/k g/G scroll · n/p turn · q       │
        └────────────────────────────────────────────────┘
```

## Install

```sh
herdr plugin install dzwduan/herdr-convo-index
```

Bind a key in `~/.config/herdr/config.toml`, then run `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+i"
type = "plugin_action"
command = "convo.index.toggle"
description = "toggle conversation index"
```

`prefix` is `ctrl+b` unless you have changed it. Without a keybinding, the same
action runs from a shell:

```sh
herdr plugin action invoke convo.index.toggle
```

Toggling splits the focused pane to the right and sizes the index to about 34
columns; set `CONVO_INDEX_WIDTH` to change that. The pane id is remembered per
space, so toggling again in the same space closes it. Folding the pane (`z`, or
the `>>` on the status line — where herdr keeps its own sidebar control) gives
that width back to the agent pane and leaves a strip showing the turn count over
a `<<`; unfolding restores whatever width the pane had.

Requirements: herdr >= 0.7.0 with its server running, `python3` on `PATH` (3.8+;
3.11+ only for the manifest check in the tests), macOS or Linux, and the herdr
integration for the agent you use:

```sh
herdr integration install claude  # for Claude Code
herdr integration install codex   # for Codex
```

Claude Code transcripts are read from `~/.claude/projects/`. Codex transcripts
are read from `$CODEX_HOME/sessions/`, which defaults to `~/.codex/sessions/`.
No third-party Python packages are needed.

## Using it

Index pane:

| key | action |
| --- | --- |
| left click | select that turn and open it |
| `enter` | open the selected turn |
| `j` / `k`, `↑` / `↓` | move the selection |
| wheel, `PgUp` / `PgDn`, `ctrl+b` / `ctrl+f` | scroll |
| `g` / `G` | first / last turn |
| `/` | filter by prompt text — `enter` keeps the filter, `esc` clears it |
| `f` | follow the latest turn, or stay put |
| `z`, click the `>>` on the status line | fold the pane to a narrow strip, or unfold it |
| `q` | close the pane |

Turn popup:

| key | action |
| --- | --- |
| `n` / `p`, `→` / `←`, `enter` | next / previous turn, without closing |
| `j` / `k`, `↑` / `↓`, wheel | scroll |
| `PgUp` / `PgDn`, `space`, `ctrl+b` / `ctrl+f` | page |
| `g` / `G` | top / bottom |
| `q`, `esc` | close |

Each index row is `NN HH:MM ▅ prompt`. The block glyph is a size bar for the
reply that followed, counted over exactly the text the popup will show, so a
long explanation is distinguishable from a one-line answer before you open it.
Compaction boundaries are drawn as `──── compacted HH:MM ────` rules; they do
not consume turn numbers, so ordinals stay stable across them.

## How it works

`pane.list` reports each pane's agent, session id and working directory. Claude
Code writes turns to `~/.claude/projects/<cwd-slug>/<session-id>.jsonl`; Codex
writes them below `$CODEX_HOME/sessions/YYYY/MM/DD/` in `rollout-*.jsonl` files.
The index resolves the session id against the appropriate store and tails that
file incrementally.

Codex writes both UI events and lower-level response items. The index consumes
the UI events, so injected context and the duplicate response-item copy of each
prompt or reply never become extra turns.

Focus is tracked over a persistent `events.subscribe` connection to the herdr
socket (`pane.focused`, `pane.agent_detected`, `pane.closed`), so retargeting is
immediate and an idle index does no periodic work at all. If the socket is
unavailable it falls back to polling `pane.list` and says `polling` in its
status line rather than going quiet. The index follows focus **within its own
space** only, so each space keeps a stable index.

Opening a turn reads the transcript, not the pane scrollback — which is what
makes arbitrarily old turns reachable, since `pane.read` caps out near 1000
lines. Replies are markdown, so the popup renders them as markdown: headings,
emphasis, code spans, fenced blocks, lists, quotes, rules and pipe tables.
Wrapping runs over styled characters rather than a finished string, so escape
codes never count toward the width, wide glyphs never straddle the right edge,
latin words are kept whole while CJK breaks between glyphs, and closing
punctuation never opens a line.

## What it deliberately does not do

- **It never scrolls the agent pane.** Opening a turn shows it in a popup and
  leaves the pane's viewport alone. The socket API reports scroll position
  (`pane.scroll_changed`) but exposes no command to set it, and page keys sent
  through `pane.send-keys` are not treated as scrollback keys.
- **Tool calls are not shown** — names, inputs and results alike. A list of tool
  names says nothing once the reply itself is in front of you. A turn that was
  only tool calls therefore shows just its prompt.
- **Noise is filtered out of the index**: tool results, Claude sidechains, Codex
  subagent activity, slash commands and their output, task notifications,
  injected system reminders, interrupt markers. Compaction notices are kept, as
  rules.
- **The size bar is an order-of-magnitude hint**, not a measurement. A turn that
  did heavy tool work and said little scores low.
- **Markdown support is partial**: source line breaks are kept rather than
  reflowed into paragraphs, link text is shown but its URL is dropped, nested
  block structures (a list inside a quote, a table inside a list) are not
  modelled, and thinking blocks stay plain prose. Table columns shrink
  proportionally and cells wrap inside their column, so no cell text is lost.
- **`/` filters prompts only**, not reply bodies.
- **Only Claude Code and Codex panes are indexed** — and only after herdr has
  detected a session id. Other agents are skipped.
- **The index cannot run as a popup.** Popups receive no `HERDR_PANE_ID`, so it
  could not exclude itself when working out which pane has focus.
- **Mouse support depends on herdr forwarding mouse events.** The plugin requests
  SGR tracking (`?1000h` / `?1006h`); every action also has a key, so a terminal
  that swallows the mouse costs nothing.

## Development

```sh
git clone https://github.com/dzwduan/herdr-convo-index
herdr plugin link ./herdr-convo-index
```

```sh
python3 tests/verify.py    # exit 0 = all checks passed
```

The checks run against Claude and Codex fixture transcripts plus stubbed socket
responses — no herdr server and no real transcript needed. They cover the
manifest, turn extraction, incremental tailing, full-turn loading, focus
scoping, session-file resolution, text metrics, input parsing, click mapping,
filtering, and markdown rendering.

Layout: `bin/transcript.py` parses the JSONL and measures text, `bin/tui.py` is
the terminal and input layer, `bin/herdr_api.py` speaks the socket protocol,
`bin/markdown.py` renders markdown to ANSI, `bin/convo_index.py` is the index
pane, `bin/turn_view.py` is the popup, and `bin/toggle.py` is the action.

## License

MIT — see [LICENSE](LICENSE).
