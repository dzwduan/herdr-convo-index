#!/usr/bin/env python3
"""Self-contained checks for the conversation index plugin.

Run: python3 tests/verify.py   (exit 0 = all checks passed)
Covers manifest validity, Claude and Codex turn extraction against fixtures,
incremental tailing, full-turn loading, focus scoping, session-file resolution,
text metrics, input parsing, click-row mapping, and markdown rendering.
No herdr server or real agent transcripts are required.
"""

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

os.environ.setdefault("HERDR_PLUGIN_CONTEXT_JSON", "{}")
os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)  # exercise toggle's fallback path
import herdr_api as api  # noqa: E402
import markdown as md  # noqa: E402
import transcript as tx  # noqa: E402
import tui  # noqa: E402
import convo_index as ci  # noqa: E402
import turn_view as tv  # noqa: E402
import toggle  # noqa: E402

FIXTURE = ROOT / "tests" / "fixture.jsonl"
CODEX_FIXTURE = ROOT / "tests" / "codex_fixture.jsonl"
MANIFEST_PLACEMENTS = ("overlay", "split", "tab", "zoomed")
FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def check_manifest():
    print("manifest")
    try:
        import tomllib
    except ModuleNotFoundError:  # python < 3.11
        print("  skip tomllib unavailable")
        return
    data = tomllib.loads((ROOT / "herdr-plugin.toml").read_bytes().decode())
    for field in ("id", "name", "version", "min_herdr_version"):
        check(f"has {field}", field in data)
    panes = {p["id"]: p for p in data.get("panes", [])}
    actions = {a["id"]: a for a in data.get("actions", [])}
    check("declares index pane", "index" in panes)
    check("declares turn pane", "turn" in panes)
    # herdr 0.7.x accepts "popup" for plugin.pane.open but not in the manifest,
    # and rejects the whole file on install if it appears here.
    check("manifest placements are installable",
          all(p.get("placement", "overlay") in MANIFEST_PLACEMENTS
              for p in panes.values()),
          [p.get("placement") for p in panes.values()])
    check("declares toggle action", "toggle" in actions)
    for entry in list(panes.values()) + list(actions.values()):
        script = ROOT / entry["command"][-1]
        check(f"{entry['id']} command exists", script.exists(), str(script))


def check_popup_launch():
    print("popup launch")
    captured = []

    class FakePopen:
        def __init__(self, argv, **_):
            captured.append(argv)

    saved = ci.subprocess.Popen
    try:
        ci.subprocess.Popen = FakePopen
        ci.open_turn_popup("/tmp/s.jsonl", 7, "session")
    finally:
        ci.subprocess.Popen = saved

    argv = captured[0] if captured else []
    def flag(name):
        return argv[argv.index(name) + 1] if name in argv else None

    check("the turn view is launched", bool(argv), argv)
    check("opened as a popup", flag("--placement") == "popup", argv)
    check("popup is sized at open time",
          flag("--width") == "80%" and flag("--height") == "80%", argv)
    check("entrypoint matches the manifest", flag("--entrypoint") == "turn", argv)
    check("transcript and ordinal are passed",
          "CONVO_TURN_FILE=/tmp/s.jsonl" in argv and "CONVO_TURN_INDEX=7" in argv, argv)


def check_extraction():
    print("turn extraction")
    index = tx.SessionIndex(FIXTURE)
    changed = index.refresh()
    summaries = [e["summary"] for e in index.turns]
    check("first refresh reports change", changed)
    check(
        "keeps only real user turns",
        summaries
        == [
            "讲解 L2 prefetch 和 TMA 的区别，必要的地方画 ascii",
            "second question with a reminder",
            "第三个问题",
        ],
        summaries,
    )
    check("second refresh is a no-op", index.refresh() is False)
    check("ordinals are dense", [e["ordinal"] for e in index.turns] == [1, 2, 3])
    check("turn count tracked", index.count == 3)
    check("timestamps parsed",
          all(len(e["stamp"]) == 5 and e["stamp"][2] == ":" for e in index.turns))
    breaks = [e for e in index.entries if e["kind"] == "break"]
    check("compaction rendered as a break", len(breaks) == 1, index.entries)
    check("break sits after the last turn",
          index.entries[-1]["kind"] == "break", [e["kind"] for e in index.entries])
    check("reply weight attributed to its turn",
          index.turns[0]["weight"] == len("ignored") and index.turns[1]["weight"] == 0,
          [e["weight"] for e in index.turns])


def check_codex_extraction():
    print("Codex turn extraction")
    index = tx.SessionIndex(CODEX_FIXTURE)
    changed = index.refresh()
    summaries = [e["summary"] for e in index.turns]
    check("first refresh reports change", changed)
    check(
        "keeps event messages without response-item duplicates",
        summaries
        == [
            "first Codex question",
            "second Codex question",
            "third Codex question",
        ],
        summaries,
    )
    check("ignores injected developer and environment messages",
          all("injected" not in summary for summary in summaries), summaries)
    check("ordinals are dense", [e["ordinal"] for e in index.turns] == [1, 2, 3])
    breaks = [e for e in index.entries if e["kind"] == "break"]
    check("Codex compaction rendered once", len(breaks) == 1, index.entries)
    check("compaction sits between the second and third turns",
          [e["kind"] for e in index.entries] == ["turn", "turn", "break", "turn"],
          [e["kind"] for e in index.entries])
    expected_weight = len("second answer") + len("continued after compaction")
    check("reply stays attached across mid-turn compaction",
          index.turns[1]["weight"] == expected_weight,
          index.turns[1]["weight"])

    first = tx.load_turn(CODEX_FIXTURE, 1)
    check("full Codex prompt loads", first["text"] == "first Codex question", first)
    check("commentary and final answer load once",
          first["blocks"] == [("text", "checking the code"), ("text", "first answer")],
          first["blocks"])
    second = tx.load_turn(CODEX_FIXTURE, 2)
    check("multiline Codex prompt stays intact",
          second["text"] == "second Codex question\nwith detail", second["text"])
    check("full reply crosses compaction",
          second["blocks"] == [
              ("text", "second answer"),
              ("text", "continued after compaction"),
          ],
          second["blocks"])


def check_size_bar():
    print("size bar")
    check("empty reply is the lowest bar", tx.size_bar(0) == tx.SIZE_BARS[0])
    check("huge reply is the highest bar", tx.size_bar(10 ** 7) == tx.SIZE_BARS[-1])
    levels = [tx.SIZE_BARS.index(tx.size_bar(w)) for w in (0, 300, 2000, 30000)]
    check("bars increase with size", levels == sorted(levels) and len(set(levels)) == 4, levels)
    check("tool calls carry no weight",
          tx.reply_weight({"type": "assistant", "message": {"content": [
              {"type": "tool_use", "name": "Bash"}]}}) == 0)
    check("weight matches what is rendered",
          tx.reply_weight({"type": "assistant", "message": {"content": [
              {"type": "text", "text": "abcde"},
              {"type": "tool_use", "name": "Read"},
              {"type": "thinking", "thinking": "xy"}]}}) == 7)


def check_tailing():
    print("incremental tailing")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "s.jsonl"
        path.write_text(
            json.dumps({"type": "user", "timestamp": "2026-07-27T00:00:00.000Z",
                        "message": {"role": "user", "content": "first"}}) + "\n"
        )
        index = tx.SessionIndex(path)
        index.refresh()
        check("initial turn", [e["summary"] for e in index.turns] == ["first"])

        with path.open("a") as handle:  # append a partial line, then complete it
            handle.write('{"type":"user","timestamp":"2026-07-27T00:01:00.000Z",')
        index.refresh()
        check("partial line not consumed", [e["summary"] for e in index.turns] == ["first"])

        with path.open("a") as handle:
            handle.write('"message":{"role":"user","content":"second"}}\n')
        index.refresh()
        check("completed line picked up",
              [e["summary"] for e in index.turns] == ["first", "second"])

        with path.open("a") as handle:  # a reply arriving later grows that turn
            handle.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "x" * 40}]}}) + "\n")
        grew = index.refresh()
        check("late reply reported as a change", grew)
        check("late reply lands on the open turn", index.turns[-1]["weight"] == 40)

        path.write_text("")  # truncation resets the tail
        index.refresh()
        check("truncation resets",
              index.entries == [] and index.offset == 0 and index.count == 0)


def check_codex_tailing():
    print("Codex incremental tailing")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rollout-session-id.jsonl"
        path.write_text(json.dumps({
            "type": "event_msg",
            "timestamp": "2026-07-27T00:00:00.000Z",
            "payload": {"type": "user_message", "message": "first"},
        }) + "\n")
        index = tx.SessionIndex(path)
        index.refresh()
        check("initial Codex turn", [e["summary"] for e in index.turns] == ["first"])

        with path.open("a") as handle:
            handle.write('{"type":"event_msg","timestamp":"2026-07-27T00:01:00.000Z",')
        index.refresh()
        check("partial Codex record waits", len(index.turns) == 1)

        with path.open("a") as handle:
            handle.write('"payload":{"type":"user_message","message":"second"}}\n')
            handle.write(json.dumps({
                "type": "event_msg",
                "timestamp": "2026-07-27T00:01:01.000Z",
                "payload": {"type": "agent_message", "message": "answer"},
            }) + "\n")
        changed = index.refresh()
        check("completed Codex records picked up", changed)
        check("new Codex turn indexed",
              [e["summary"] for e in index.turns] == ["first", "second"])
        check("Codex reply grows the open turn", index.turns[-1]["weight"] == len("answer"))


def check_load_turn():
    print("full turn loading")
    first = tx.load_turn(FIXTURE, 1)
    check("first turn found", first is not None)
    check("keeps the whole prompt",
          first["text"] == "讲解 L2 prefetch 和 TMA 的区别，必要的地方画 ascii", first["text"])
    check("collects the reply", first["blocks"] == [("text", "ignored")], first["blocks"])

    check("neighbouring ordinal loads", tx.load_turn(FIXTURE, 2)["text"].startswith("second"))
    third = tx.load_turn(FIXTURE, 3)
    check("reminder stripped from body", third["text"] == "第三个问题", third["text"])
    check("ordinal past the end", tx.load_turn(FIXTURE, 99) is None)

    index = tx.SessionIndex(FIXTURE)
    index.refresh()
    check("index ordinals address the same turns",
          all(tx.load_turn(FIXTURE, e["ordinal"])["text"].startswith(e["summary"][:8])
              for e in index.turns))

    blocks = tx.assistant_blocks({
        "type": "assistant",
        "message": {"content": [
            {"type": "thinking", "thinking": "weighing"},
            {"type": "text", "text": "answer"},
            {"type": "tool_use", "name": "Bash"},
            {"type": "text", "text": "   "},
        ]},
    })
    check("assistant blocks classified",
          blocks == [("thinking", "weighing"), ("text", "answer"), ("tool", "Bash")], blocks)
    check("sidechain replies skipped",
          tx.assistant_blocks({"type": "assistant", "isSidechain": True,
                               "message": {"content": [{"type": "text", "text": "x"}]}}) == [])


def check_focus_scoping():
    print("focus scoping")
    panes = {
        "panes": [
            {"pane_id": "w3:p1", "focused": False, "workspace_id": "w3", "agent": "claude",
             "cwd": "/x", "agent_session": {"kind": "id", "value": "S3"}},
            {"pane_id": "w3:p9", "focused": False, "workspace_id": "w3"},
            {"pane_id": "w2:p1", "focused": False, "workspace_id": "w2", "agent": "claude",
             "cwd": "/y", "agent_session": {"kind": "id", "value": "S2"}},
            {"pane_id": "w3:p2", "focused": False, "workspace_id": "w3", "agent": "shell"},
            {"pane_id": "w3:p3", "focused": False, "workspace_id": "w3", "agent": "codex",
             "cwd": "/z", "agent_session": {"kind": "id", "value": "C3"}},
        ]
    }

    def focus(pane_id):
        for pane in panes["panes"]:
            pane["focused"] = pane["pane_id"] == pane_id

    saved_call, saved_self, saved_ws = ci.pane_list, ci.SELF_PANE, ci.SELF_WORKSPACE
    try:
        ci.pane_list = lambda *_: panes
        ci.SELF_PANE, ci.SELF_WORKSPACE = "w3:p9", "w3"
        focus("w3:p1")
        check("follows claude pane in own space",
              (ci.focused_agent_pane() or {}).get("session_id") == "S3")
        focus("w3:p3")
        codex = ci.focused_agent_pane() or {}
        check("follows Codex pane in own space",
              codex.get("session_id") == "C3" and codex.get("agent") == "codex", codex)
        focus("w2:p1")
        check("ignores other space", ci.focused_agent_pane() is None)
        focus("w3:p9")
        check("ignores itself", ci.focused_agent_pane() is None)
        focus("w3:p2")
        check("ignores non-agent pane", ci.focused_agent_pane() is None)
        ci.pane_list = lambda *_: None
        check("survives api failure", ci.focused_agent_pane() is None)
    finally:
        ci.pane_list, ci.SELF_PANE, ci.SELF_WORKSPACE = saved_call, saved_self, saved_ws


def check_session_path():
    print("session path resolution")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        slug = root / "-home-dev-demo-project"
        slug.mkdir()
        (slug / "abc-123.jsonl").write_text("")
        check("slug derived from cwd",
              tx.session_path("abc-123", "/home/dev/demo/project", root) == slug / "abc-123.jsonl")
        check("falls back to scanning",
              tx.session_path("abc-123", "/unrelated/path", root) == slug / "abc-123.jsonl")
        check("unknown session",
              tx.session_path("nope", "/home/dev/demo/project", root) is None)

        codex_root = root / "codex-sessions"
        day = codex_root / "2026" / "07" / "27"
        day.mkdir(parents=True)
        codex_file = day / "rollout-2026-07-27T08-00-00-codex-123.jsonl"
        codex_file.write_text("")
        check("Codex nested session resolved by id",
              tx.session_path("codex-123", "/unrelated", codex_root, agent="codex")
              == codex_file)
        check("unknown Codex session",
              tx.session_path("nope", "/unrelated", codex_root, agent="codex") is None)
        check("path-valued session used directly",
              tx.session_path(str(codex_file), "", root, agent="codex") == codex_file)
        check("unsupported agent is rejected",
              tx.session_path("codex-123", "", codex_root, agent="other") is None)


def check_text_metrics():
    print("text metrics")
    check("wide glyphs count as 2", tx.cell_width("中a") == 3)
    check("no truncation when it fits", tx.fit("abc", 10) == "abc")
    fitted = tx.fit("讲解L2 prefetch和tma的区别", 20)
    check("truncated marks elision", fitted.endswith("…"), fitted)
    check("truncation respects budget", tx.cell_width(fitted) <= 20, tx.cell_width(fitted))
    check("zero budget", tx.fit("abc", 0) == "")
    check("pad reaches target width", tx.cell_width(tx.pad("中a", 10)) == 10)

    wrapped = tx.wrap("aaa bbb ccc ddd", 7)
    check("wrap respects width", all(tx.cell_width(line) <= 7 for line in wrapped), wrapped)
    check("wrap loses nothing", "".join(wrapped).replace(" ", "")
          == "aaabbbcccddd", wrapped)
    wide = tx.wrap("中文中文中文", 5)
    check("wrap handles wide glyphs", all(tx.cell_width(line) <= 5 for line in wide), wide)
    check("wrap keeps blank lines", tx.wrap("a\n\nb", 10) == ["a", "", "b"])


def check_input_parsing():
    print("input parsing")
    reader = tui.InputReader()
    check("plain key", reader.feed("j") == [("key", "j")])
    check("arrow key", reader.feed("\x1b[B") == [("key", "down")])
    check("page key", reader.feed("\x1b[5~") == [("key", "pgup")])
    check("left click", reader.feed("\x1b[<0;12;7M") == [("mouse", 0, 12, 7, True)])
    check("click release ignored later",
          reader.feed("\x1b[<0;12;7m") == [("mouse", 0, 12, 7, False)])
    check("wheel up", reader.feed("\x1b[<64;3;3M") == [("mouse", tui.WHEEL_UP, 3, 3, True)])
    check("wheel down", reader.feed("\x1b[<65;3;3M") == [("mouse", tui.WHEEL_DOWN, 3, 3, True)])
    check("split sequence waits", reader.feed("\x1b[<0;1") == [])
    check("split sequence completes", reader.feed("0;4M") == [("mouse", 0, 10, 4, True)])
    check("batched events", reader.feed("jk\x1b[A") ==
          [("key", "j"), ("key", "k"), ("key", "up")])


def check_click_mapping():
    print("click mapping")
    view = ci.View()
    view.rows, view.top = 10, 5  # header + 8 body rows + footer
    check("first body row", view.row_to_index(2) == 5)
    check("last body row", view.row_to_index(9) == 12)
    check("header row is not a turn", view.row_to_index(1) is None)
    check("footer row is not a turn", view.row_to_index(10) is None)

    entries = [{"kind": "turn", "ordinal": i + 1, "stamp": "00:00",
                "summary": f"t{i}", "weight": 0} for i in range(40)]
    view.top, view.cursor = 0, 0
    view.apply(entries)
    view.scroll(3)
    check("wheel scrolls the viewport", view.top == 3 and view.cursor == 3)
    view.scroll(-100)
    check("wheel clamps at the top", view.top == 0)
    view.scroll(1000)
    check("wheel clamps at the end", view.top == 40 - view.body_rows, view.top)


def check_collapse():
    print("collapse button")
    view = ci.View()
    view.rows, view.cols = 10, 34
    check("the status-line marker hits the button", view.hits_button(2, 10))
    check("the status text does not", not view.hits_button(4, 10))
    check("the same column in the body does not", not view.hits_button(2, 5))

    widths = []
    saved_narrow, saved_self = toggle.narrow, ci.SELF_PANE
    try:
        toggle.narrow = lambda pane_id, cols: widths.append(cols)
        ci.SELF_PANE = "w1:p1"
        ci.handle(("key", "z"), view, None, None)
        check("z folds the pane to a rail",
              view.collapsed and widths == [ci.COLLAPSED_COLS], widths)
        check("a folded row still addresses its turn", view.row_to_index(5) == 3)
        ci.handle(("mouse", 0, 1, 10, True), view, None, None)
        check("clicking the marker restores the previous width",
              not view.collapsed and widths[-1] == 35, widths)
    finally:
        toggle.narrow, ci.SELF_PANE = saved_narrow, saved_self

    entry = {"kind": "turn", "ordinal": 1, "stamp": "10:00", "summary": "x", "weight": 5000}
    check("a folded row carries the size bar",
          tx.size_bar(5000) in ci.rail_line(entry, 3, False))
    check("a folded break is a rule",
          "─" in ci.rail_line({"kind": "break", "stamp": "10:00", "weight": 0}, 3, False))


def check_filtering():
    print("filtering")
    entries = [
        {"kind": "turn", "ordinal": 1, "stamp": "10:00", "summary": "prefetch 与 TMA", "weight": 0},
        {"kind": "break", "ordinal": 0, "stamp": "10:30", "summary": "", "weight": 0},
        {"kind": "turn", "ordinal": 2, "stamp": "11:00", "summary": "TMEM layout", "weight": 0},
        {"kind": "turn", "ordinal": 3, "stamp": "12:00", "summary": "tma descriptor", "weight": 0},
    ]
    view = ci.View()
    view.rows = 12
    check("no query shows everything", len(view.apply(entries)) == 4)

    view.query = "tma"
    shown = view.apply(entries)
    check("filter is case-insensitive",
          [e["ordinal"] for e in shown] == [1, 3], [e["ordinal"] for e in shown])
    check("filter drops breaks", all(e["kind"] == "turn" for e in shown))

    view.cursor = 1
    check("selection keeps the original ordinal", view.selected()["ordinal"] == 3)

    view.query = "nothing matches this"
    view.apply(entries)
    view.clamp()
    check("empty result is safe", view.selected() is None and view.cursor == 0)

    view.query = ""
    view.apply(entries)
    view.cursor = 1  # the break row
    check("breaks are not openable", view.selected() is None)


def check_typing_mode():
    print("filter typing")
    view = ci.View()
    view.rows = 12
    check("slash enters typing", ci.handle(("key", "/"), view, None, None) and view.typing)
    for ch in "tm":
        ci.handle(("key", ch), view, None, None)
    check("keys build the query", view.query == "tm", view.query)
    ci.handle(("key", "\x7f"), view, None, None)
    check("backspace edits the query", view.query == "t", view.query)
    check("q is not a quit while typing",
          ci.handle(("key", "q"), view, None, None) and view.query == "tq")
    ci.handle(("key", "\r"), view, None, None)
    check("enter confirms and keeps the filter", not view.typing and view.query == "tq")
    ci.handle(("key", "esc"), view, None, None)
    check("escape clears the filter", view.query == "")
    check("q quits outside typing", ci.handle(("key", "q"), view, None, None) is False)


def check_turn_rendering():
    print("turn rendering")
    turn = {
        "text": "问题",
        "stamp": "10:00",
        "blocks": [("tool", "Bash"), ("text", "answer"), ("thinking", "musing"),
                   ("tool", "Read")],
    }
    body = "\n".join(tv.compose(turn, 40))
    check("tool names are not rendered",
          "Bash" not in body and "Read" not in body, body)
    check("reply text is rendered", "answer" in body)
    check("thinking is rendered", "musing" in body)
    check("prompt is rendered", "问题" in body)
    check("a tool-only reply renders just the prompt",
          len(tv.compose({"text": "q", "stamp": "10:00",
                          "blocks": [("tool", "Bash")]}, 40)) == 1)


def plain(lines):
    """Visible text of rendered lines, with escape codes removed."""
    return [tx.ANSI_RE.sub("", line) for line in lines]


def check_markdown():
    print("markdown rendering")
    heading = md.render("## Title", 20)
    check("heading loses its hashes", plain(heading)[0] == "Title")
    check("heading is emphasised", tui.BOLD in heading[0])
    check("heading is underlined by a rule", set(plain(heading)[1]) == {"─"})
    check("deep heading gets no rule", len(md.render("#### Deep", 20)) == 1)

    emphasis = md.render("a **bold** and *slanted* and `code` word", 60)
    text = plain(emphasis)[0]
    check("emphasis markers are consumed",
          "*" not in text and "`" not in text, text)
    check("emphasis words survive", text == "a bold and slanted and code word", text)
    check("bold is styled", tui.BOLD in emphasis[0])
    check("code span is styled", tui.CODE in emphasis[0])

    link = md.render("see [the docs](https://example.com/x) now", 60)
    check("link text is kept", plain(link)[0] == "see the docs now", plain(link)[0])
    check("link url is dropped", "example.com" not in link[0])

    fence = md.render("```sh\nherdr plugin list\n```\ntail", 40)
    check("fence markers are consumed", "```" not in "".join(fence), fence)
    check("fenced code is gutter-marked", plain(fence)[0].startswith(md.CODE_MARK))
    check("fenced code body survives", "herdr plugin list" in plain(fence)[0])
    check("text after the fence resumes", plain(fence)[-1] == "tail", fence)
    check("unclosed fence still terminates",
          "x" in "".join(plain(md.render("```\nx", 40))))

    items = md.render("- first\n  - nested\n1. numbered", 40)
    rows = plain(items)
    check("bullet marker is replaced", rows[0] == f"{md.BULLET} first", rows)
    check("nested item is indented", rows[1] == f"  {md.BULLET} nested", rows)
    check("ordered marker is kept", rows[2] == "1. numbered", rows)
    wrapped = plain(md.render("- " + "word " * 12, 20))
    check("list continuation hangs", wrapped[1].startswith("  "), wrapped)

    words = plain(md.render("alpha beta gamma delta", 12))
    check("latin words are not split", all(" " in w or len(w) <= 12 for w in words)
          and "alpha" in words[0] and "gamma" in "".join(words[1:]), words)
    dense = plain(md.render("中" * 30, 20))
    check("cjk fills the line", tx.cell_width(dense[0]) == 20, dense)
    tight = plain(md.render("中" * 9 + "。尾", 20))
    check("punctuation does not open a line", not tight[1].startswith("。"), tight)
    check("wrapping loses nothing",
          "".join(plain(md.render("中" * 9 + "。尾", 20))) == "中" * 9 + "。尾")

    quote = md.render("> quoted", 20)
    check("quote is marked", plain(quote)[0] == f"{md.QUOTE_MARK} quoted")

    rule = md.render("---", 12)
    check("rule spans the width", plain(rule)[0] == "─" * 12)
    check("blank lines are preserved", md.render("a\n\nb", 20)[1] == "")

    table = md.render("| a | bb |\n| --- | --- |\n| 1 | 2 |", 40)
    rows = plain(table)
    check("table keeps every row", len(rows) == 3, rows)
    check("table header is styled", tui.BOLD in table[0])
    check("table separator becomes a rule", "┼" in rows[1], rows)
    check("table cells are laid out in columns",
          rows[0].startswith("a") and "bb" in rows[0] and rows[2].startswith("1"), rows)
    long_cell = plain(md.render(
        "| f | note |\n| --- | --- |\n| x | " + "word " * 20 + "|", 30))
    check("long table cells wrap instead of vanishing",
          long_cell[-1].count("word") and sum(l.count("word") for l in long_cell) == 20,
          long_cell)

    sample = ("## 标题\n\n| 文件 | 作用 |\n| --- | --- |\n"
              "| `bin/tui.py` | 终端底层，负责 **鼠标上报** 与 alt-screen |\n\n"
              "- 一条比较长的中文条目，用来触发换行并检查悬挂缩进是否正确\n\n"
              "> 引用\n\n```sh\nherdr plugin action invoke convo.index.toggle\n```\n")
    for width in (24, 40, 64):
        widest = max(tx.cell_width(line) for line in plain(md.render(sample, width)))
        check(f"nothing exceeds {width} cells", widest <= width, f"got {widest}")
    check("degenerate width is safe", md.render("**x**", 0) == [""])


def check_socket_client():
    print("socket client")
    saved = api.SOCKET_PATH
    try:
        api.SOCKET_PATH = "/nonexistent/herdr.sock"
        check("missing socket reported", api.available() is False)
        check("request degrades to None", api.request("pane.list") is None)
        stream = api.EventStream()
        check("stream reports disconnection", stream.connected is False)
        check("dead stream drains empty", stream.drain() == [])
        check("dead stream has no fd", stream.fileno() is None)
    finally:
        api.SOCKET_PATH = saved


def check_state_dir():
    print("toggle state dir")
    check("fallback matches herdr's plugin state layout",
          toggle.STATE_DIR == Path.home() / ".local/state/herdr/plugins" / toggle.PLUGIN_ID,
          str(toggle.STATE_DIR))
    os.environ["HERDR_PLUGIN_STATE_DIR"] = "/tmp/convo-index-state"
    try:
        reloaded = importlib.reload(toggle)
        check("herdr-provided dir wins",
              reloaded.STATE_DIR == Path("/tmp/convo-index-state"), str(reloaded.STATE_DIR))
    finally:
        os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)
        importlib.reload(toggle)


def main():
    for step in (check_manifest, check_popup_launch, check_extraction, check_codex_extraction,
                 check_size_bar, check_tailing, check_codex_tailing, check_load_turn,
                 check_focus_scoping, check_session_path,
                 check_text_metrics, check_input_parsing, check_click_mapping,
                 check_collapse, check_filtering, check_typing_mode, check_turn_rendering,
                 check_markdown, check_socket_client, check_state_dir):
        step()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
