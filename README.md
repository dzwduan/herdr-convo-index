# herdr conversation index

A herdr plugin that docks a narrow turn index next to a Claude Code pane. Turns
that scrolled out of the pane stay listed, and clicking one opens its full text
in a popup — so a long session stays navigable without touching the agent pane's
scrollback.

```
┌─────────────────────────────┬──────────────────────────────┐
│ claude pane                 │ claude · 讲解L2 prefetch…    │
│                             │  1 11:13 ▃ 阅读专利分析，以… │
│ (current turn only)         │  2 11:36 ▆ 我理解是先prefe…  │
│                             │  3 11:40 ▂ 这里是我对pref…   │
│                             │ ──── compacted 12:20 ──────  │
│                             │  4 12:43 ▇ 这个问题是什么？… │
│                             │ 12 turns · follow · / q      │
└─────────────────────────────┴──────────────────────────────┘
                                        │ click
                                        ▼
        ┌──────────────────────────────────────────────┐
        │ #3 · 11:40 · 讲解L2 prefetch…                │
        │ ❯ 这里是我对prefetch的理解…                  │
        │                                              │
        │ ● 你的理解基本对，两处需要修正…              │
        │ 1-42/318 · j/k g/G scroll · n/p turn · q     │
        └──────────────────────────────────────────────┘
```

## How it works

`pane.list` reports each pane's Claude session id and cwd. Claude Code writes
every turn to `~/.claude/projects/<cwd-slug>/<session-id>.jsonl`. The index pane
joins the two, tails the transcript incrementally, and renders one line per real
user turn: `NN HH:MM ▅ summary`.

The block glyph is a rough size bar for that turn's reply, counted over exactly
the text the popup shows, so a long explanation is distinguishable from a
one-line answer at a glance. Compaction boundaries are
kept and drawn as `──── compacted HH:MM ────` rules; they do not consume turn
numbers, so ordinals stay stable across them.

The index follows focus **within its own space**: focusing another Claude pane
in the same space retargets it; focusing a different space leaves it unchanged,
so each space keeps a stable index.

Retargeting is driven by a persistent `events.subscribe` stream on the herdr
socket (`pane.focused`, `pane.agent_detected`, `pane.closed`), so focus changes
land immediately and an idle index pane does no periodic work. If the socket is
unavailable the pane falls back to polling `pane.list` every 1.2 s and says
`polling` in its status line.

Clicking a turn (or pressing `enter`) opens a session-modal popup at 80%×80%
that renders that turn's full prompt plus the reply that followed it, read
straight from the transcript. That path has no length limit — `pane.read` caps
out near 1000 lines of scrollback, the JSONL does not.

The reply is markdown, so the popup renders it as markdown rather than showing
the source: headings, emphasis, code spans, fenced blocks, lists, quotes, rules
and pipe tables. Wrapping is done on styled characters, so escape codes never
count toward the width, wide glyphs never straddle the right edge, latin words
are kept whole while CJK breaks between glyphs, and closing punctuation never
opens a line.

## Requirements

- herdr >= 0.7.0 with its server running
- Python 3.8+ on `PATH` as `python3` (3.11+ for the manifest check in the tests)
- Claude Code writing transcripts under `~/.claude/projects/`
- macOS or Linux

## Install

```sh
herdr plugin install dzwduan/herdr-convo-index
```

Or, to work on it locally:

```sh
git clone https://github.com/dzwduan/herdr-convo-index
herdr plugin link ./herdr-convo-index
```

Bind a key in `~/.config/herdr/config.toml`, then `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+i"
type = "plugin_action"
command = "convo.index.toggle"
description = "toggle conversation index"
```

Or invoke it directly:

```sh
herdr plugin action invoke convo.index.toggle
```

The action splits the focused pane to the right, sizes the index to ~34 columns
(`CONVO_INDEX_WIDTH` overrides), and records the pane id per space in
`$HERDR_PLUGIN_STATE_DIR/panes.json`. Invoking it again in the same space closes
that pane.

## Keys

Index pane:

| key | action |
| --- | --- |
| left click | select that turn and open it |
| wheel | scroll the list |
| `enter` | open the selected turn |
| `/` | filter by prompt text; `enter` keeps it, `esc` clears |
| `j` / `k`, arrows | move selection |
| `PgUp` / `PgDn`, `ctrl+b` / `ctrl+f` | page |
| `g` / `G` | first / last turn |
| `f` | toggle follow-latest |
| `q` | close the pane |

Turn popup:

| key | action |
| --- | --- |
| wheel, `j` / `k`, `↑` / `↓` | scroll |
| `PgUp` / `PgDn`, `space`, `ctrl+b` / `ctrl+f` | page |
| `g` / `G` | top / bottom |
| `n` / `p`, `→` / `←`, `enter` | previous / next turn, without closing |
| `q`, `esc` | close |

## Boundaries

- **The agent pane is never scrolled.** Opening a turn shows it in a popup; it
  does not move the agent pane's viewport. The herdr socket API exposes scroll
  position (`pane.scroll_changed`) but has no command to set it, and page keys
  injected through `pane.send-keys` are not treated as scrollback keys.
- Filtered out of the index: tool results, subagent (`isSidechain`) turns, slash
  commands and their output, task notifications, injected system reminders and
  interrupt markers. Compaction notices are kept, but as rules rather than turns.
- The size bar is an order-of-magnitude hint, not a measurement: it counts
  rendered characters only, so a turn that did heavy tool work and said little
  scores low.
- `/` filters on the prompt line only, not on reply bodies.
- Only panes herdr has detected as Claude with a session id are indexed. Other
  agents report sessions differently and are skipped.
- The popup renders the prompt (`❯`), the reply prose (`●`) and thinking (`✻`).
  Tool calls are dropped entirely — names, inputs and results alike — since a
  list of tool names says nothing once the reply itself is in front of you. A
  turn that was only tool calls therefore shows just its prompt.
- Markdown support is deliberately partial: source line breaks are kept rather
  than reflowed into paragraphs, link text is shown but its URL is dropped,
  nested block structures (a list inside a quote, a table inside a list) are not
  modelled, and thinking blocks stay plain dim prose. Table columns shrink
  proportionally and cells wrap inside their column, so no cell text is lost.
- The index itself cannot run as a popup: popups get no `HERDR_PANE_ID`, so it
  could not exclude itself when computing which pane has focus.
- Mouse input depends on herdr forwarding mouse events to the pane app. The
  plugin requests SGR tracking (`?1000h`/`?1006h`); keyboard keys cover every
  action if a terminal swallows the mouse.

## Verify

```sh
python3 tests/verify.py    # exit 0 = all checks passed
```

Runs against `tests/fixture.jsonl` and stubbed API responses; no herdr server or
real transcript needed.

## License

MIT — see [LICENSE](LICENSE).
