"""Markdown to ANSI rendering for the turn view.

Agent replies are markdown, and reading the raw source in a pane is what makes a
long turn hard to scan. This renders the subset that actually shows up in those
replies: headings, emphasis, code spans and fences, lists, quotes, rules and
pipe tables.

Line breaks in the source are preserved rather than reflowed into paragraphs —
the author's line structure carries meaning in agent output.

Wrapping works on (character, style) pairs so that escape codes never count
toward the terminal width and a wide glyph never straddles the right edge.
"""

import re

import transcript as tx
import tui

BULLET = "•"
QUOTE_MARK = "▏"
CODE_MARK = "│"
MIN_COL = 4
COL_GAP = " │ "

# Glyphs that may not open or close a wrapped line.
NO_START = "、，。．！？；：）〕〉》」』】”’%,.!?;:)]}…"
NO_END = "（〔〈《「『【“‘([{"

FENCE_RE = re.compile(r"^\s*(?:```+|~~~+)(.*)$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
RULE_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")

INLINE_RE = re.compile(
    r"(?P<tick>`+)(?P<code>.+?)(?P=tick)"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|__(?P<bold2>.+?)__"
    r"|~~(?P<strike>.+?)~~"
    r"|\[(?P<link>[^\]]*)\]\((?P<url>[^)]*)\)"
    r"|(?<![\w*])\*(?P<em>[^*\n]+?)\*(?![\w*])"
    r"|(?<![\w_])_(?P<em2>[^_\n]+?)_(?![\w_])",
    re.S,
)


# --- styled character streams -----------------------------------------------


def styled(text, style=""):
    return [(ch, style) for ch in text]


def inline(text, base=""):
    """Parse inline markup into a (character, style) stream."""
    out = []
    pos = 0
    for match in INLINE_RE.finditer(text):
        out += styled(text[pos : match.start()], base)
        pos = match.end()
        if match.group("code") is not None:
            out += styled(match.group("code"), tui.CODE)
        elif match.group("bold") is not None:
            out += inline(match.group("bold"), base + tui.BOLD)
        elif match.group("bold2") is not None:
            out += inline(match.group("bold2"), base + tui.BOLD)
        elif match.group("strike") is not None:
            out += inline(match.group("strike"), base + tui.DIM)
        elif match.group("link") is not None:
            out += inline(match.group("link"), base + tui.UNDERLINE)
        elif match.group("em") is not None:
            out += inline(match.group("em"), base + tui.ITALIC)
        else:
            out += inline(match.group("em2"), base + tui.ITALIC)
    out += styled(text[pos:], base)
    return out


def emit(chars, prefix=""):
    """Collapse a character stream into one ANSI string."""
    parts = [prefix]
    current = None
    for ch, style in chars:
        if style != current:
            parts.append(tui.RESET + style)
            current = style
        parts.append(ch)
    parts.append(tui.RESET)
    return "".join(parts)


def break_point(pending, nxt, width):
    """Where to split `pending` when `nxt` no longer fits: (line end, next start).

    CJK breaks between any two glyphs, so a mixed line should not be pulled back
    to the last space just because some latin token straddles the edge — that is
    what leaves Chinese paragraphs ragged. Only an actual word split is retreated
    from, and then only when the word is short enough to be worth moving.
    """
    end = len(pending)
    prev = pending[-1][0]
    if not (nxt == " " or prev == " "
            or tx.char_width(nxt) == 2 or tx.char_width(prev) == 2):
        space = max((i for i, (c, _) in enumerate(pending) if c == " "), default=-1)
        tail = sum(tx.char_width(c) for c, _ in pending[space + 1:])
        if space > 0 and tail < width // 2:
            return space, space + 1  # break between words, drop the space
        return end, end  # an unbreakable run: split it rather than overflow
    if end > 1 and (nxt in NO_START or prev in NO_END):
        return end - 1, end - 1  # keep punctuation attached to its neighbour
    return end, end


def wrap_styled(chars, width):
    """Break a character stream into lines of at most `width` cells."""
    if width <= 0:
        return [chars]
    lines = []
    current = []
    used = 0
    for ch, style in chars:
        w = tx.char_width(ch)
        if used + w > width and current:
            end, start = break_point(current, ch, width)
            lines.append(current[:end])
            current = current[start:]
            used = sum(tx.char_width(c) for c, _ in current)
            if not current and ch == " ":
                continue  # the wrap absorbs the space
        current.append((ch, style))
        used += w
    lines.append(current)
    return lines


def lay_out(chars, width, prefix="", hang=None):
    """Wrap a stream and prefix the first line with `prefix`, the rest with `hang`."""
    hang = " " * tx.cell_width(prefix) if hang is None else hang
    body = max(1, width - tx.cell_width(prefix))
    out = []
    for i, line in enumerate(wrap_styled(chars, body)):
        out.append(emit(line, prefix if i == 0 else hang))
    return out


# --- tables -----------------------------------------------------------------


def split_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def column_widths(rows, width):
    """Natural widths shrunk until the row fits, never below MIN_COL."""
    count = max(len(row) for row in rows)
    widths = [MIN_COL] * count
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], tx.cell_width(cell))
    budget = width - tx.cell_width(COL_GAP) * (count - 1)
    if budget < count * MIN_COL:
        return [max(1, budget // count)] * count  # too narrow to balance
    while sum(widths) > budget:
        widest = widths.index(max(widths))
        if widths[widest] <= MIN_COL:
            break
        widths[widest] -= 1
    return widths


def render_table(rows, width):
    """Header row, a rule, then body rows. Long cells wrap inside their column."""
    widths = column_widths(rows, width)
    out = []
    for index, row in enumerate(rows):
        base = tui.BOLD if index == 0 else ""
        columns = [wrap_styled(inline(row[i] if i < len(row) else "", base), limit)
                   for i, limit in enumerate(widths)]
        for line_no in range(max(len(column) for column in columns)):
            joined = []
            for i, column in enumerate(columns):
                if i:
                    joined += styled(COL_GAP, tui.DIM)
                cell = column[line_no] if line_no < len(column) else []
                joined += cell
                if i < len(columns) - 1:  # trailing pad would be dead space
                    used = sum(tx.char_width(c) for c, _ in cell)
                    joined += styled(" " * max(0, widths[i] - used))
            out.append(emit(joined))
        if index == 0:
            rule = ("─" * w for w in widths)
            out.append(emit(styled("─┼─".join(rule), tui.DIM)))
    return out


# --- blocks -----------------------------------------------------------------


def is_table_head(lines, i):
    return (
        "|" in lines[i]
        and i + 1 < len(lines)
        and "|" in lines[i + 1]
        and TABLE_SEP_RE.match(lines[i + 1])
    )


def render(text, width):
    """Markdown source to a list of ANSI lines no wider than `width` cells."""
    if width <= 0:
        return [""]
    src = text.split("\n")
    out = []
    i = 0
    while i < len(src):
        line = src[i]

        fence = FENCE_RE.match(line)
        if fence:
            i += 1
            while i < len(src) and not FENCE_RE.match(src[i]):
                for chunk in wrap_styled(styled(src[i], tui.CODE), width - 2):
                    out.append(emit(chunk, f"{tui.DIM}{CODE_MARK}{tui.RESET} "))
                i += 1
            i += 1  # closing fence, or end of input
            continue

        if is_table_head(src, i):
            rows = []
            while i < len(src) and "|" in src[i]:
                if not TABLE_SEP_RE.match(src[i]):
                    rows.append(split_row(src[i]))
                i += 1
            out += render_table(rows, width)
            continue

        i += 1

        if not line.strip():
            out.append("")
            continue

        if RULE_RE.match(line):
            out.append(emit(styled("─" * width, tui.DIM)))
            continue

        heading = HEADING_RE.match(line)
        if heading:
            level, body = len(heading.group(1)), heading.group(2)
            out += lay_out(inline(body, tui.BOLD + tui.ACCENT), width)
            if level <= 2:
                out.append(emit(styled("─" * width, tui.DIM)))
            continue

        quote = QUOTE_RE.match(line)
        if quote:
            out += lay_out(inline(quote.group(1), tui.DIM),
                           width, f"{tui.DIM}{QUOTE_MARK}{tui.RESET} ")
            continue

        item = LIST_RE.match(line)
        if item:
            pad, marker, body = item.groups()
            depth = min(len(pad) // 2, 3)
            label = BULLET if marker in "-*+" else marker
            prefix = " " * (depth * 2) + f"{tui.ACCENT}{label}{tui.RESET} "
            out += lay_out(inline(body), width, prefix,
                           " " * (depth * 2 + tx.cell_width(label) + 1))
            continue

        out += lay_out(inline(line), width)
    return out
