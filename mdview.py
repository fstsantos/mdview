#!/usr/bin/env python3
"""MDVIEW: a Markdown viewer for the terminal that looks like a 90s DOS utility.

Standard library only. Examples:

    mdview README.md
    mdview docs/ --light
    mdview --help

How it works (three stages, each in its own section of this file):

1. `parse`      Markdown -> list of blocks (heading, paragraph, list, table...).
2. `Renderer`   blocks -> lines already wrapped to the screen width. Each line is a
                list of `(text, style, flags)` segments; no color is decided here.
3. `Pager`      paints the lines with the chosen theme (ANSI codes) and handles the keyboard.

Keeping "style" (stage 2) apart from "color" (stage 3) is what lets a single
key switch themes without rendering the document again. The one exception is
the family of ornaments: the ANSI-art themes draw headings, rules and code
blocks with shaded blocks instead of box-drawing lines, so switching between
the two families renders the document again.
"""

from __future__ import annotations

import argparse
import os
import re
import select
import shutil
import signal
import sys
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Union

VERSION = "1.0"
PROG = "mdview"

Seg = tuple[str, str, str]  # (text, style name, flags: b=bold i=italic u=underline s=strikethrough)
Line = list[Seg]

# =============================================================================
# Colors and themes
# =============================================================================

# The 16-color VGA palette, in RGB. Explicit RGB (instead of codes 30-37) gives
# the same colors on every terminal, whatever the user's color scheme.
VGA = [
    (0, 0, 0), (0, 0, 170), (0, 170, 0), (0, 170, 170),
    (170, 0, 0), (170, 0, 170), (170, 85, 0), (170, 170, 170),
    (85, 85, 85), (85, 85, 255), (85, 255, 85), (85, 255, 255),
    (255, 85, 85), (255, 85, 255), (255, 255, 85), (255, 255, 255),
]  # fmt: skip

# style -> (foreground, background), as VGA palette indexes
THEMES: dict[str, dict[str, tuple[int, int]]] = {
    # Dark: the classic blue of DOS editors and file managers.
    "dark": {
        "text": (7, 1), "strong": (15, 1), "em": (11, 1),
        "h1": (0, 3), "h1box": (0, 3), "shadow": (8, 0),
        "h2": (14, 1), "h2deco": (14, 1), "h3": (11, 1), "h4": (15, 1),
        "rule": (3, 1), "hrdeco": (11, 1),
        "code": (10, 0), "codeblk": (10, 0), "codebox": (7, 0), "codelbl": (14, 0),
        "link": (11, 1), "linkref": (10, 1),
        "quotebar": (13, 1), "bullet": (14, 1),
        "tborder": (3, 1), "th": (14, 1), "match": (0, 14),
        "frame": (15, 1), "sbar": (1, 3),
        "bar": (0, 7), "barhot": (4, 7),
        "dlg": (0, 7), "dlghot": (4, 7), "dlgsel": (15, 2), "dlgshadow": (8, 0),
        "logo0": (15, 1), "logo1": (11, 1), "logo2": (3, 1),
    },
    # Light: white paper with navy bars, in the style of Windows 3.x windows.
    "light": {
        "text": (0, 15), "strong": (0, 15), "em": (1, 15),
        "h1": (15, 1), "h1box": (15, 1), "shadow": (8, 7),
        "h2": (4, 15), "h2deco": (4, 15), "h3": (1, 15), "h4": (0, 15),
        "rule": (8, 15), "hrdeco": (1, 15),
        "code": (1, 7), "codeblk": (0, 7), "codebox": (8, 7), "codelbl": (1, 7),
        "link": (1, 15), "linkref": (8, 15),
        "quotebar": (5, 15), "bullet": (4, 15),
        "tborder": (8, 15), "th": (1, 15), "match": (0, 14),
        "frame": (1, 15), "sbar": (0, 7),
        "bar": (15, 1), "barhot": (14, 1),
        "dlg": (0, 7), "dlghot": (4, 7), "dlgsel": (15, 1), "dlgshadow": (7, 8),
        "logo0": (1, 15), "logo1": (9, 15), "logo2": (3, 15),
    },
}  # fmt: skip


def ansi_theme(
    ramp: tuple[int, int, int, int],
    title: tuple[int, int],
    hot: int,
    accent: int,
    second: int,
    code: tuple[int, int],
) -> dict[str, tuple[int, int]]:
    """Colors of an ANSI-art theme, in the manner of the 90s BBS art groups.

    The page is black and everything is lit by `ramp`, four palette indexes
    going from dark to bright; shaded blocks (░▒▓█) painted along it give the
    gradients. `title` is the (foreground, background) of banners, bars and
    dialogs, `hot` the highlight over that background, `accent` and `second`
    the two colors that stand out from the ramp, `code` the colors of code.
    """
    dark, mid, bright, peak = ramp
    title_bg, code_bg = title[1], code[1]
    return {
        "text": (7, 0), "strong": (15, 0), "em": (second, 0),
        "h1": title, "h1box": (title_bg, 0), "shadow": (8, 0),
        "h2": (accent, 0), "h2deco": (mid, 0), "h3": (bright, 0), "h4": (15, 0),
        "rule": (dark, 0), "hrdeco": (accent, 0),
        "code": code, "codeblk": code, "codebox": (code_bg, 0), "codelbl": (accent, code_bg),
        "link": (bright, 0), "linkref": (mid, 0),
        "quotebar": (second, 0), "bullet": (accent, 0),
        "tborder": (mid, 0), "th": (accent, 0), "match": (0, 15),
        "frame": (mid, 0), "sbar": (mid, 0),
        "bar": title, "barhot": (hot, title_bg),
        "dlg": title, "dlghot": (hot, title_bg), "dlgsel": (15, 0), "dlgshadow": (7, 8),
        "logo0": (peak, 0), "logo1": (bright, 0), "logo2": (mid, 0),
        # the ramp itself, used by the gradients (only the ANSI-art ornaments need it)
        "g0": (dark, 0), "g1": (mid, 0), "g2": (bright, 0), "g3": (peak, 0),
    }  # fmt: skip


ANSI_THEMES = {
    # Acid: blue fading into cyan and white, with yellow lettering.
    "acid": ansi_theme(ramp=(1, 9, 11, 15), title=(15, 1), hot=14, accent=14, second=13, code=(11, 1)),
    # Ice: cold grays and cyans, white highlights.
    "ice": ansi_theme(ramp=(8, 3, 11, 15), title=(0, 3), hot=15, accent=15, second=9, code=(11, 8)),
    # Fire: red, orange-red, yellow, white, like a flame.
    "fire": ansi_theme(ramp=(4, 12, 14, 15), title=(15, 4), hot=14, accent=14, second=12, code=(14, 8)),
    # Toxic: phosphor green going to yellow.
    "toxic": ansi_theme(ramp=(2, 10, 14, 15), title=(0, 2), hot=14, accent=14, second=10, code=(10, 8)),
    # Neon: magenta and cyan on black.
    "neon": ansi_theme(ramp=(5, 13, 11, 15), title=(15, 5), hot=11, accent=11, second=13, code=(13, 8)),
}
THEMES.update(ANSI_THEMES)
DEFAULT_THEME = "dark"

# theme -> (display name, one-line description). The key of THEMES is what
# --theme takes; the display name is what the screen shows.
THEME_INFO = {
    "dark": ("DOS Blue", "the classic blue of DOS editors and file managers"),
    "light": ("Paper White", "white paper with navy bars, like Windows 3.x"),
    "acid": ("Acid Wave", "ANSI art: blue fading into cyan and white, yellow lettering"),
    "ice": ("Ice Cold", "ANSI art: cold grays and cyans, white highlights"),
    "fire": ("Fire Storm", "ANSI art: red, orange, yellow and white, like a flame"),
    "toxic": ("Toxic Green", "ANSI art: phosphor green going to yellow"),
    "neon": ("Neon Night", "ANSI art: magenta and cyan"),
}


def theme_title(theme: str) -> str:
    return THEME_INFO.get(theme, (theme.capitalize(), ""))[0]


# --- saved default -----------------------------------------------------------
# The default theme lives in a one-setting `key = value` file, so that it can
# also be edited by hand.


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / PROG / "config"


def saved_theme() -> str | None:
    """The theme saved as the default, or None if there is none (or it is not a known theme)."""
    try:
        lines = config_path().read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip() == "theme" and value.strip() in THEMES:
            return value.strip()
    return None


def save_theme(theme: str) -> None:
    """Save `theme` as the default, keeping any other line of the file. Raises OSError."""
    path = config_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        lines = []
    lines = [line for line in lines if line.partition("=")[0].strip() != "theme"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([*lines, f"theme = {theme}"]) + "\n", encoding="utf-8")


def default_theme() -> str:
    """The theme used without --theme: MDVIEW_THEME, then the saved one, then DEFAULT_THEME."""
    env = os.environ.get("MDVIEW_THEME")
    if env in THEMES:
        return env
    return saved_theme() or DEFAULT_THEME


def save_theme_message(theme: str) -> str:
    """Save `theme` as the default and describe the outcome in one line."""
    try:
        save_theme(theme)
    except OSError as error:
        return f"Could not save the default theme: {error.strerror or error}"
    message = f"Default theme: {theme_title(theme)}"
    env = os.environ.get("MDVIEW_THEME")
    if env in THEMES and env != theme:
        message += " (but MDVIEW_THEME is set and wins)"
    return message

# flag -> SGR attribute code
SGR_ATTRS = (("b", "1"), ("i", "3"), ("u", "4"), ("s", "9"))

RESET = "\x1b[0m"
CURSOR_HOME = "\x1b[H"
CLEAR_SCREEN = "\x1b[2J"
# alternate screen, hidden cursor, mouse wheel sends arrow keys (?1007)
ENTER_FULLSCREEN = "\x1b[?1049h\x1b[?25l\x1b[?1007h"
LEAVE_FULLSCREEN = "\x1b[?1007l\x1b[0m\x1b[?25h\x1b[?1049l"


def move_to(row: int, col: int = 1) -> str:
    return f"\x1b[{row};{col}H"


@lru_cache(maxsize=None)
def sgr(theme: str, style: str, flags: str = "") -> str:
    """ANSI (SGR) sequence that turns on the colors and attributes of a style."""
    fg, bg = THEMES[theme][style]
    if style == "text":  # bold/italic in plain text also change the color
        if "b" in flags:
            fg = THEMES[theme]["strong"][0]
        elif "i" in flags:
            fg = THEMES[theme]["em"][0]
    attrs = "".join(";" + code for flag, code in SGR_ATTRS if flag in flags)
    (r, g, b), (R, G, B) = VGA[fg], VGA[bg]
    return f"\x1b[0{attrs};38;2;{r};{g};{b};48;2;{R};{G};{B}m"


# =============================================================================
# Text width in the terminal
# =============================================================================
# len() counts characters, but the terminal counts columns: emoji and ideographs
# take 2, combining accents take 0. All line wrapping goes through text_width().

EMOJI_SELECTOR = 0xFE0F
ZERO_WIDTH = (0x200B, 0x200D)  # zero width space and zero width joiner


def _char_width(ch: str) -> int:
    code = ord(ch)
    if code < 0x300:
        return 1
    if unicodedata.combining(ch) or code in ZERO_WIDTH:
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def _char_widths(s: str) -> Iterator[int]:
    """Width, in columns, of each character of `s`, in order."""
    last = 0  # width of the last visible character
    for ch in s:
        if ord(ch) == EMOJI_SELECTOR:
            # The "emoji presentation" selector takes no space itself, but widens
            # the previous character to 2 columns (e.g. ❤ + selector = ❤️).
            width = 1 if last == 1 else 0
            if width:
                last = 2
        else:
            width = _char_width(ch)
            last = width or last
        yield width


def text_width(s: str) -> int:
    """Width of `s` in terminal columns."""
    return len(s) if s.isascii() else sum(_char_widths(s))


def clip(s: str, max_width: int) -> str:
    """Longest prefix of `s` that fits in `max_width` columns."""
    if s.isascii():
        return s[: max(max_width, 0)]
    used = end = 0
    for i, width in enumerate(_char_widths(s)):
        if used + width > max_width:
            break
        used, end = used + width, i + 1
    return s[:end]


def plain(segs: Line) -> str:
    """Text of a line, without the styles."""
    return "".join(text for text, _, _ in segs)


def compose(segs: Line, width: int, theme: str, fill: str = "text") -> str:
    """Paint a line: apply the colors, cut at the limit and fill the background up to `width`."""
    out, used = [], 0
    for text, style, flags in segs:
        if used >= width:
            break
        if used + text_width(text) > width:
            text = clip(text, width - used)
        out.append(sgr(theme, style, flags) + text)
        used += text_width(text)
    if used < width:
        out.append(sgr(theme, fill) + " " * (width - used))
    return "".join(out)


RE_WRAP_TOKEN = re.compile(r"\n|[ \t]+|[^ \t\n]+")  # line break, whitespace or word


def wrap(segs: Line, width: int) -> list[Line]:
    """Wrap a sequence of segments into lines of at most `width` columns.

    Breaks at spaces; a "word" may span several segments (e.g. `**bold**,`),
    and words longer than the line are split.
    """
    width = max(width, 1)
    lines: list[Line] = [[]]
    line_width = 0
    # The word being built only goes onto the line once it ends (flush_word),
    # because only then do we know whether it fits. The space before it stays
    # pending: it is dropped if the word ends up on the next line.
    word: Line = []
    word_width = 0
    pending_space: Seg | None = None

    def new_line() -> None:
        nonlocal line_width, pending_space
        lines.append([])
        line_width, pending_space = 0, None

    def flush_word() -> None:
        nonlocal line_width, word, word_width, pending_space
        if not word:
            return
        if line_width and line_width + (1 if pending_space else 0) + word_width > width:
            new_line()
        if pending_space and line_width:
            lines[-1].append(pending_space)
            line_width += 1
        pending_space = None
        for text, style, flags in word:
            while line_width + text_width(text) > width:  # word longer than the line: split it
                head = clip(text, width - line_width)
                if not head and line_width == 0:
                    head = text[0]  # character wider than the line: emit it anyway
                if head:
                    lines[-1].append((head, style, flags))
                text = text[len(head) :]
                lines.append([])
                line_width = 0
            if text:
                lines[-1].append((text, style, flags))
                line_width += text_width(text)
        word, word_width = [], 0

    for text, style, flags in segs:
        for token in RE_WRAP_TOKEN.findall(text):
            if token == "\n":
                flush_word()
                new_line()
            elif token[0] in " \t":
                flush_word()
                pending_space = (" ", style, flags)
            else:
                word.append((token, style, flags))
                word_width += text_width(token)
    flush_word()
    return lines


# =============================================================================
# Stage 1: Markdown -> blocks
# =============================================================================


@dataclass
class Heading:
    level: int
    text: str


@dataclass
class Paragraph:
    text: str


@dataclass
class CodeBlock:
    lang: str
    lines: list[str]


@dataclass
class Quote:
    blocks: list[Block]


@dataclass
class Rule:
    pass


@dataclass
class ListBlock:
    ordered: bool
    start: int  # number of the first item (ordered lists)
    items: list[list[Block]]
    loose: bool  # items separated by blank lines


@dataclass
class Table:
    header: list[str]
    aligns: list[str]  # per column: "l", "c" or "r"
    rows: list[list[str]]


Block = Union[Heading, Paragraph, CodeBlock, Quote, Rule, ListBlock, Table]

RE_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([^`]*)$")
RE_HEAD = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
RE_HR = re.compile(r"^ {0,3}([-*_])(?:\s*\1){2,}\s*$")
RE_LIST = re.compile(r"^( *)([-*+]|\d{1,9}[.)])( +|$)(.*)$")  # (indent)(marker)(spaces)(text)
RE_QUOTE = re.compile(r"^ {0,3}> ?(.*)$")
RE_TABLE_SEP = re.compile(r":?-+:?")
RE_TASK = re.compile(r"\[([ xX])\]\s+")

# List markers that may interrupt a paragraph with no blank line before them.
# Ordered lists only interrupt when they start at 1 (CommonMark rule).
PARAGRAPH_BREAKING_MARKERS = ("-", "*", "+", "1.", "1)")


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


def _skip_blank(lines: list[str], i: int) -> int:
    """Index of the first non-blank line at or after `i`."""
    while i < len(lines) and not lines[i].strip():
        i += 1
    return i


def _starts_block(line: str) -> bool:
    """Does the line open a block that is neither a paragraph nor a table?"""
    return bool(RE_FENCE.match(line) or RE_HEAD.match(line) or RE_HR.match(line) or RE_QUOTE.match(line))


def split_row(s: str) -> list[str]:
    """Split a table row into cells (honors `\\|` and `|` inside code spans)."""
    s = s.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells, cell, in_code, k = [], "", False, 0
    while k < len(s):
        ch = s[k]
        if ch == "\\" and s[k + 1 : k + 2] == "|":
            cell += "|"
            k += 2
            continue
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append(cell.strip())
            cell = ""
        else:
            cell += ch
        k += 1
    cells.append(cell.strip())
    return cells


def _table_at(lines: list[str], i: int) -> bool:
    """Is there a table at `i`? (a header followed by a `|---|---|` line with the same columns)"""
    if "|" not in lines[i] or i + 1 >= len(lines) or "-" not in lines[i + 1]:
        return False
    separator = split_row(lines[i + 1])
    return len(separator) == len(split_row(lines[i])) and all(RE_TABLE_SEP.fullmatch(c) for c in separator)


def _parse_code(lines: list[str], i: int) -> tuple[CodeBlock, int]:
    opening = RE_FENCE.match(lines[i])
    mark, info = opening.group(1), opening.group(2).strip()
    indent = _indent(lines[i])
    # closes with the same character, repeated at least as many times as in the opening
    closing = re.compile(rf"^ {{0,3}}{re.escape(mark[0])}{{{len(mark)},}}\s*$")
    body = []
    i += 1
    while i < len(lines) and not closing.match(lines[i]):
        line = lines[i]
        # strip the fence's indentation (code block inside a list item)
        body.append(line[indent:] if not line[:indent].strip() else line)
        i += 1
    return CodeBlock(info.split()[0] if info else "", body), i + 1


def _parse_quote(lines: list[str], i: int) -> tuple[Quote, int]:
    body = []
    while i < len(lines) and lines[i].strip():
        m = RE_QUOTE.match(lines[i])
        body.append(m.group(1) if m else lines[i])  # no ">": lazy continuation
        i += 1
    return Quote(parse(body)), i


def _parse_table(lines: list[str], i: int) -> tuple[Table, int]:
    header = split_row(lines[i])
    aligns = []
    for cell in split_row(lines[i + 1]):
        aligns.append("c" if cell.startswith(":") and cell.endswith(":") else "r" if cell.endswith(":") else "l")
    rows = []
    i += 2
    while i < len(lines) and lines[i].strip() and "|" in lines[i]:
        rows.append(split_row(lines[i]))
        i += 1
    return Table(header, aligns, rows), i


def _list_item_at(line: str, base: int, ordered: bool) -> re.Match | None:
    """Match `line` if it is an item of the list with indentation `base` and of the same kind."""
    m = RE_LIST.match(line)
    if not m or RE_HR.match(line):
        return None
    if not (base <= len(m.group(1)) <= base + 1) or m.group(2)[0].isdigit() != ordered:
        return None
    return m


def _parse_item_body(lines: list[str], i: int, item: re.Match, base: int) -> tuple[list[str], int]:
    """Collect the lines of a list item, with the item's indentation removed."""
    indent, marker, spaces, text = item.groups()
    gap = len(spaces) if text and len(spaces) <= 4 else 1
    content_col = len(indent) + len(marker) + gap  # column where the item's content starts
    body = [text]
    i += 1
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            # blank line: the item goes on if what follows is indented
            after = _skip_blank(lines, i)
            if after < len(lines) and _indent(lines[after]) >= base + 2:
                body.extend([""] * (after - i))
                i = after
                continue
            break
        indent_here = _indent(line)
        if indent_here >= base + 2:
            body.append(line[min(indent_here, content_col) :])
        elif _starts_block(line) or RE_LIST.match(line):
            break
        else:
            body.append(line.strip())  # unindented paragraph continuation
        i += 1
    return body, i


def _parse_list(lines: list[str], i: int) -> tuple[ListBlock, int]:
    first = RE_LIST.match(lines[i])
    base = len(first.group(1))
    ordered = first.group(2)[0].isdigit()
    start = int(first.group(2)[:-1]) if ordered else 1
    items, loose = [], False
    while i < len(lines):
        item = _list_item_at(lines[i], base, ordered)
        if not item:
            break
        body, i = _parse_item_body(lines, i, item, base)
        items.append(parse(body))
        after = _skip_blank(lines, i)
        if after > i:  # there was a blank line after the item
            if after < len(lines) and _list_item_at(lines[after], base, ordered):
                loose = True  # ...and the list goes on: it is a "loose" list
                i = after
            else:
                break
    return ListBlock(ordered, start, items, loose), i


def _interrupts_paragraph(lines: list[str], i: int) -> bool:
    """Does line `i` start another block even with no blank line before it?"""
    line = lines[i]
    if _starts_block(line) or _table_at(lines, i):
        return True
    m = RE_LIST.match(line)
    return bool(m and m.group(4) and m.group(2) in PARAGRAPH_BREAKING_MARKERS)


def _parse_paragraph(lines: list[str], i: int) -> tuple[Paragraph, int]:
    parts = []
    start = i
    while i < len(lines) and lines[i].strip():
        line = lines[i]
        if i > start and _interrupts_paragraph(lines, i):
            break
        hard_break = line.endswith("  ") or line.endswith("\\")
        parts.append(line.strip().rstrip("\\") + ("\n" if hard_break else " "))
        i += 1
    return Paragraph("".join(parts).strip()), i


def parse(lines: list[str]) -> list[Block]:
    """Convert Markdown lines into a list of blocks."""
    blocks: list[Block] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        block: Block
        if RE_FENCE.match(line):
            block, i = _parse_code(lines, i)
        elif m := RE_HEAD.match(line):
            block, i = Heading(len(m.group(1)), m.group(2)), i + 1
        elif RE_HR.match(line):
            block, i = Rule(), i + 1
        elif RE_QUOTE.match(line):
            block, i = _parse_quote(lines, i)
        elif _table_at(lines, i):
            block, i = _parse_table(lines, i)
        elif (m := RE_LIST.match(line)) and len(m.group(1)) <= 3:
            block, i = _parse_list(lines, i)
        else:
            block, i = _parse_paragraph(lines, i)
        blocks.append(block)
    return blocks


def split_front_matter(lines: list[str]) -> tuple[CodeBlock | None, list[str]]:
    """Split off the YAML front matter (`---` ... `---` at the top), shown as a code block."""
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() in ("---", "..."):
                return CodeBlock("metadata", lines[1:j]), lines[j + 1 :]
    return None, lines


# Inline formatting. The order of the alternatives matters: the first one to
# match wins (escapes before everything, code before emphasis, `**` before `*`).
INLINE = re.compile(
    r"""
      (?P<esc>\\[\\`*_{}\[\]()#+\-.!|~<>])
    | (?P<code>(?P<cq>`+)(?P<codet>.+?)(?P=cq)(?!`))
    | (?P<img>!\[(?P<alt>[^\]]*)\]\((?P<isrc>[^)\s]+)[^)]*\))
    | (?P<link>\[(?P<lt>(?:[^\[\]]|\[[^\]]*\])+)\]\((?P<href>[^)\s]*)(?:\s+"[^"]*")?\))
    | (?P<auto><(?P<aurl>(?:https?://|mailto:)[^>\s]+)>)
    | (?P<bold>(?P<bq>\*\*|__)(?=\S)(?P<bt>.+?)(?<=\S)(?P=bq))
    | (?P<ital>(?<![\w*_])(?P<iq>[*_])(?=[^\s*_])(?P<it>.+?)(?<=[^\s*_])(?P=iq)(?![\w*_]))
    | (?P<strike>~~(?=\S)(?P<st>.+?)(?<=\S)~~)
    | (?P<br><br\s*/?>)
    """,
    re.X,
)

# =============================================================================
# Stage 2: blocks -> lines of segments
# =============================================================================


class Renderer:
    """Turns blocks into lines ready to be painted, decorated with box-drawing characters."""

    BULLETS = "■►•·"  # one per nesting level

    def __init__(self, ansi: bool = False) -> None:
        self.ansi = ansi  # ornaments made of shaded blocks (ANSI-art themes) instead of lines
        self.links: list[str] = []  # link targets, numbered in the footer (as in Lynx)
        self.list_depth = 0
        self._renderers = {
            Paragraph: self.paragraph,
            Heading: self.heading,
            CodeBlock: self.code_block,
            Quote: self.quote,
            Rule: self.rule,
            ListBlock: self.list_block,
            Table: self.table,
        }

    def render(self, text: str, width: int) -> list[Line]:
        """Render a whole Markdown document into lines of at most `width` columns."""
        width = max(width, 10)
        front_matter, lines = split_front_matter(text.expandtabs(4).splitlines())
        blocks: list[Block] = [front_matter] if front_matter else []
        blocks += parse(lines)
        out = self.blocks(blocks, width)
        if self.links:
            out += self.link_footer(width)
        return out

    def blocks(self, blocks: list[Block], width: int, tight: bool = False) -> list[Line]:
        """Render blocks one after another, separated by a blank line (unless `tight`)."""
        out: list[Line] = []
        for k, block in enumerate(blocks):
            if k and not tight:
                out.append([])
                if isinstance(block, Heading) and block.level <= 2:
                    out.append([])  # top-level headings get extra breathing room
            out += self._renderers[type(block)](block, width)
        return out

    def link_footer(self, width: int) -> list[Line]:
        label = "─── LINKS "
        if self.ansi:
            title: Line = [("▓▒░ LINKS ", "g1", ""), *self.fade("─", max(width - len(label), 0))]
        else:
            title = [(label + "─" * max(width - len(label), 0), "rule", "")]
        out: list[Line] = [[], title]
        for number, href in enumerate(self.links, 1):
            out += wrap([(f"[{number}] ", "linkref", ""), (href, "link", "")], width)
        return out

    # --- inline text ---------------------------------------------------------

    def inline(self, text: str, style: str = "text", flags: str = "") -> Line:
        """Convert inline formatting (bold, code, links...) into segments."""
        out: Line = []
        pos = 0
        for m in INLINE.finditer(text):
            if m.start() > pos:
                out.append((text[pos : m.start()], style, flags))
            pos = m.end()
            kind = m.lastgroup
            if kind == "esc":
                out.append((m.group()[1], style, flags))
            elif kind == "code":
                code = m.group("codet")
                if len(code) > 2 and code[0] == code[-1] == " ":
                    code = code[1:-1]
                out.append((code, "code", ""))
            elif kind == "img":
                out.append((f"[image: {m.group('alt') or m.group('isrc')}]", "linkref", ""))
            elif kind == "link":
                out += self.inline(m.group("lt"), "link", flags + "u")
                href = m.group("href")
                if href and not href.startswith("#"):
                    out.append((f"[{self.link_number(href)}]", "linkref", ""))
            elif kind == "auto":
                out.append((m.group("aurl"), "link", "u"))
            elif kind == "bold":
                out += self.inline(m.group("bt"), style, flags + "b")
            elif kind == "ital":
                out += self.inline(m.group("it"), style, flags + "i")
            elif kind == "strike":
                out += self.inline(m.group("st"), style, flags + "s")
            elif kind == "br":
                out.append(("\n", style, flags))
        if pos < len(text):
            out.append((text[pos:], style, flags))
        return out

    def link_number(self, href: str) -> int:
        """Number of the link in the footer (the same target reuses its number)."""
        if href not in self.links:
            self.links.append(href)
        return self.links.index(href) + 1

    @staticmethod
    def prefixed(lines: list[Line], first: Seg, rest: Seg | None = None) -> list[Line]:
        """Put `first` before the 1st line and `rest` (default: spaces) before the others."""
        rest = rest or (" " * text_width(first[0]), first[1], "")
        return [[first if k == 0 else rest, *line] for k, line in enumerate(lines)]

    @staticmethod
    def fade(char: str, width: int, reverse: bool = False) -> Line:
        """A rule made of `char` that fades from bright to dark along the theme ramp."""
        sizes = [width // 8, width // 4, width // 4]
        sizes.append(width - sum(sizes))
        segs: Line = [(char * n, f"g{3 - k}", "") for k, n in enumerate(sizes) if n]
        return segs[::-1] if reverse else segs

    # --- blocks --------------------------------------------------------------

    def paragraph(self, block: Paragraph, width: int) -> list[Line]:
        return wrap(self.inline(block.text), width)

    def heading(self, block: Heading, width: int) -> list[Line]:
        if block.level == 1:
            return self.heading_banner(block.text, width) if self.ansi else self.heading_box(block.text, width)
        if block.level == 2:
            lines = wrap(self.inline(block.text, "h2", "b"), width - 5)
            underline: Line = self.fade("▀", width) if self.ansi else [("═" * width, "rule", "")]
            return self.prefixed(lines, ("█▓▒░ ", "h2deco", "")) + [underline]
        if block.level == 3:
            lines = wrap(self.inline(block.text, "h3", "b"), width - 2)
            length = min(max(text_width(plain(line)) for line in lines) + 2, width)
            underline = self.fade("─", length) if self.ansi else [("─" * length, "rule", "")]
            return self.prefixed(lines, ("► ", "h3", "b")) + [underline]
        return self.prefixed(wrap(self.inline(block.text, "h4", "b"), width - 2), ("· ", "h4", "b"))

    def heading_box(self, text: str, width: int) -> list[Line]:
        """Level 1 heading: a double-line box with a shadow, like a window."""
        segs = [(t.upper(), style, flags) for t, style, flags in self.inline(text, "h1", "b")]
        box_width = width - 1  # the last column is left for the shadow
        inner = box_width - 4
        shadow = (" ", "shadow", "")
        out: list[Line] = [[("╔" + "═" * (box_width - 2) + "╗", "h1box", "")]]
        for line in wrap(segs, inner):
            pad = " " * (inner - text_width(plain(line)))
            out.append([("║ ", "h1box", ""), *line, (pad + " ║", "h1box", ""), shadow])
        out.append([("╚" + "═" * (box_width - 2) + "╝", "h1box", ""), shadow])
        out.append([(" ", "text", ""), (" " * box_width, "shadow", "")])
        return out

    def heading_banner(self, text: str, width: int) -> list[Line]:
        """Level 1 heading, ANSI-art style: a solid bar whose ends dissolve into the page."""
        segs = [(t.upper(), style, flags) for t, style, flags in self.inline(text, "h1", "b")]
        inner = width - 8  # on each side: three shaded blocks and a space
        indent = ("   ", "text", "")
        out: list[Line] = [[indent, ("▄" * (width - 6), "h1box", "")]]
        for line in wrap(segs, inner):
            pad = " " * (inner - text_width(plain(line)))
            out.append([("░▒▓", "h1box", ""), (" ", "h1", ""), *line, (pad + " ", "h1", ""), ("▓▒░", "h1box", "")])
        out.append([indent, ("▀" * (width - 6), "h1box", "")])
        out.append([])  # same height as heading_box(), so the scroll position survives a theme switch
        return out

    def code_block(self, block: CodeBlock, width: int) -> list[Line]:
        inner = width - 4
        lang = block.lang
        label = f"[ {lang} ]" if lang and text_width(lang) + 8 < width else ""
        # ANSI art: a slab in the code background, with half-block edges and an accent bar on the left
        ansi = self.ansi
        if ansi:
            left, right = ("▌ ", "codelbl", ""), ("  ", "codeblk", "")
        else:
            left, right = ("│ ", "codebox", ""), (" │", "codebox", "")
        top: Line = [("▄▄" if ansi else "┌─", "codebox", "")]
        if label:
            top.append((label, "codelbl", ""))
        rest_width = width - 3 - text_width(label)
        top.append(("▄" * (rest_width + 1) if ansi else "─" * rest_width + "┐", "codebox", ""))
        out = [top]
        for raw in block.lines or [""]:
            rest = raw.rstrip()
            while True:  # long code line: split it without reflowing
                chunk = clip(rest, inner) or rest[:1]
                rest = rest[len(chunk) :]
                pad = " " * (inner - text_width(chunk))
                out.append([left, (chunk + pad, "codeblk", ""), right])
                if not rest:
                    break
        out.append([("▀" * width if ansi else "└" + "─" * (width - 2) + "┘", "codebox", "")])
        return out

    def quote(self, block: Quote, width: int) -> list[Line]:
        bar = ("▒ " if self.ansi else "▌ ", "quotebar", "")
        return [[bar, *line] for line in self.blocks(block.blocks, width - 2)]

    def rule(self, block: Rule, width: int) -> list[Line]:
        deco = "░▒▓█▓▒░"
        left = max((width - len(deco) - 2) // 2, 0)
        right = max(width - left - len(deco) - 2, 0)
        if self.ansi:  # the lines fade away from the center
            return [[*self.fade("─", left, reverse=True), (f" {deco} ", "hrdeco", ""), *self.fade("─", right)]]
        return [[("─" * left + " ", "rule", ""), (deco, "hrdeco", ""), (" " + "─" * right, "rule", "")]]

    def list_block(self, block: ListBlock, width: int) -> list[Line]:
        number_width = len(str(block.start + len(block.items) - 1)) + 1  # aligns "9." with "10."
        out: list[Line] = []
        for k, item in enumerate(block.items):
            if block.loose and k:
                out.append([])
            if block.ordered:
                marker = f"{block.start + k}.".rjust(number_width) + " "
            else:
                marker = self.BULLETS[min(self.list_depth, len(self.BULLETS) - 1)] + " "
            if item and isinstance(item[0], Paragraph):  # task list: - [ ] / - [x]
                task = RE_TASK.match(item[0].text)
                if task:
                    marker = "[ ] " if task.group(1) == " " else "[√] "
                    item = [Paragraph(item[0].text[task.end() :]), *item[1:]]
            self.list_depth += 1
            body = self.blocks(item, width - len(marker), tight=not block.loose) or [[]]
            self.list_depth -= 1
            out += self.prefixed(body, (marker, "bullet", ""))
        return out

    def table(self, block: Table, width: int) -> list[Line]:
        ncol = len(block.header)
        rows = [(row + [""] * ncol)[:ncol] for row in block.rows]  # pad/trim to ncol cells
        cells = [[self.inline(c, "th", "b") for c in block.header]]
        cells += [[self.inline(c) for c in row] for row in rows]
        widths = self.fit_columns(cells, width)

        def border(left: str, fill: str, mid: str, right: str) -> Line:
            return [(left + mid.join(fill * (w + 2) for w in widths) + right, "tborder", "")]

        # each cell becomes a list of lines; a table row is as tall as its tallest cell
        wrapped = [[wrap(cell, widths[c]) for c, cell in enumerate(row)] for row in cells]
        multiline = any(len(cell) > 1 for row in wrapped[1:] for cell in row)
        out = [border("╔", "═", "╤", "╗")]
        for r, row in enumerate(wrapped):
            if r == 1:
                out.append(border("╠", "═", "╪", "╣"))
            elif r > 1 and multiline:  # only rule between rows when some cell wrapped
                out.append(border("╟", "─", "┼", "╢"))
            for k in range(max(len(cell) for cell in row)):
                line: Line = [("║ ", "tborder", "")]
                for c, cell in enumerate(row):
                    segs = cell[k] if k < len(cell) else []
                    free = widths[c] - text_width(plain(segs))
                    align = block.aligns[c]
                    lpad = free if align == "r" else free // 2 if align == "c" else 0
                    line += [(" " * lpad, "text", ""), *segs, (" " * (free - lpad), "text", "")]
                    line.append((" │ " if c < ncol - 1 else " ║", "tborder", ""))
                out.append(line)
        out.append(border("╚", "═", "╧", "╝"))
        return out

    @staticmethod
    def fit_columns(cells: list[list[Line]], width: int) -> list[int]:
        """Width of each column of a table that has to fit in `width`.

        Starts from the natural widths and takes 1 column off the widest one
        until it fits; cells wrap instead of being cut. While it can, it only
        shrinks columns that do not yet have to split words (each column's
        "floor" is its longest word).
        """
        ncol = len(cells[0])
        texts = [[plain(row[c]) for row in cells] for c in range(ncol)]
        widths = [max(max(text_width(part) for part in t.split("\n")) for t in col) or 1 for col in texts]
        floors = [max((text_width(word) for t in col for word in t.split()), default=1) for col in texts]
        available = width - (ncol + 1) - 2 * ncol  # minus the borders and the padding around the cells
        while sum(widths) > available:
            shrinkable = [c for c in range(ncol) if widths[c] > floors[c]]
            widest = max(shrinkable or range(ncol), key=widths.__getitem__)
            if widths[widest] <= 4:
                break
            widths[widest] -= 1
        return widths


# =============================================================================
# Stage 3: full screen (pager)
# =============================================================================

# Escape sequences sent by the terminal -> key name
KEYS = {
    b"\x1b[A": "up", b"\x1bOA": "up", b"\x1b[B": "down", b"\x1bOB": "down",
    b"\x1b[C": "right", b"\x1bOC": "right", b"\x1b[D": "left", b"\x1bOD": "left",
    b"\x1b[5~": "pgup", b"\x1b[6~": "pgdn",
    b"\x1b[H": "home", b"\x1b[1~": "home", b"\x1bOH": "home",
    b"\x1b[F": "end", b"\x1b[4~": "end", b"\x1bOF": "end",
    b"\x1bOP": "f1", b"\x1b[11~": "f1", b"\x1bOR": "f3", b"\x1b[13~": "f3",
    b"\x1b[17~": "f6", b"\x1b[21~": "f10", b"\x1b[Z": "btab", b"\x1b": "esc",
}  # fmt: skip
# Splits keyboard input into keys: an escape sequence (CSI or SS3), a lone Esc,
# an ASCII character or a multi-byte UTF-8 character.
RE_KEY = re.compile(rb"\x1b\[[0-9;]*[A-Za-z~]|\x1bO[A-Za-z]|\x1b|[\x00-\x7f]|[\xc0-\xff][\x80-\xbf]*")

ENTER = ("\n", "\r")
BACKSPACE = ("\x7f", "\x08")

HELP_KEYS = [
    ("↑ ↓  j k", "scroll one line"),
    ("PgUp PgDn  Space b", "scroll one page"),
    ("d  u", "scroll half a page"),
    ("Home End  g G", "start / end of the document"),
    ("/", "search text (Enter confirms, Esc cancels)"),
    ("n  N", "next / previous match"),
    ("T", "next theme"),
    ("S", "save the current theme as the default"),
    ("F3  o", "open the file list"),
    ("F6  Tab  →", "next file"),
    ("Shift+Tab  ←", "previous file"),
    ("F1  h  ?", "this help"),
    ("F10  q  Esc", "quit"),
]

STATUS_HINTS = [("F1", "Help"), ("F3", "Open"), ("F6", "Next"), ("T", "Theme"), ("/", "Search"), ("Q", "Quit")]

MIN_COLS, MIN_ROWS = 30, 8


def highlight(segs: Line, term: str) -> Line:
    """Mark the occurrences of `term` (already lowercase) with the `match` style."""
    low = plain(segs).lower()
    hit = [False] * len(low)  # hit[i]: is character i of the line part of an occurrence?
    at = low.find(term)
    while at >= 0:
        hit[at : at + len(term)] = [True] * len(term)
        at = low.find(term, at + len(term))

    def is_hit(i: int) -> bool:
        return i < len(hit) and hit[i]

    # Split each segment at the points where it enters or leaves an occurrence.
    out: Line = []
    offset = 0  # position of the current segment within the line
    for text, style, flags in segs:
        start = 0
        for end in range(1, len(text) + 1):
            if end == len(text) or is_hit(offset + end) != is_hit(offset + start):
                out.append((text[start:end], "match" if is_hit(offset + start) else style, flags))
                start = end
        offset += len(text)
    return out


def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


class Pager:
    """Full screen: shows one document at a time and reacts to the keyboard."""

    def __init__(self, files: list[Path], theme: str, max_width: int | None) -> None:
        self.files, self.theme, self.max_width = files, theme, max_width
        self.file_index = 0
        self.text = ""  # Markdown of the current file
        self.lines: list[Line] = []  # the same, rendered at the current width
        self.top = 0  # first visible line
        self.mode = "view"  # view | help | pick | search
        self.search_input = ""  # what is being typed in the search prompt
        self.search_term = ""  # confirmed search, lowercase
        self.matches: list[int] = []  # lines containing search_term
        self.message = ""  # notice shown in the status bar
        self.picked = 0  # file highlighted in the file list
        self.resized = False
        self.dirty = True  # the screen needs to be redrawn
        # geometry, computed in layout()
        self.cols = self.rows = 0  # terminal size
        self.view_height = 1  # visible document lines
        self.text_width = 10  # width of the rendered text
        self.margin = 0  # margin that centers the text when --width is set

    # --- state ---------------------------------------------------------------

    @property
    def current_file(self) -> Path:
        return self.files[self.file_index]

    @property
    def ansi(self) -> bool:
        """Is the current theme one of the ANSI-art ones?"""
        return self.theme in ANSI_THEMES

    @property
    def max_top(self) -> int:
        return max(len(self.lines) - self.view_height, 0)

    def layout(self) -> None:
        """Recompute the geometry and render the document again (on open or resize)."""
        self.cols, self.rows = shutil.get_terminal_size()
        self.view_height = max(self.rows - 4, 1)  # menu, top border, bottom border, status bar
        inner = max(self.cols - 4, 10)  # frame, scrollbar and one column of padding on each side
        self.text_width = min(inner, self.max_width) if self.max_width else inner
        self.margin = (inner - self.text_width) // 2
        self.lines = Renderer(self.ansi).render(self.text, self.text_width)
        self.find_matches()
        self.scroll(0)

    def next_theme(self) -> None:
        names = list(THEMES)
        was_ansi = self.ansi
        self.theme = names[(names.index(self.theme) + 1) % len(names)]
        if self.ansi != was_ansi:  # the ornaments changed, not only the colors
            self.layout()
        self.message = f"Theme: {theme_title(self.theme)} · S saves as default"

    def load(self, index: int) -> None:
        self.file_index = index % len(self.files)
        self.text = read_file(self.current_file)
        self.top = 0
        self.layout()

    def scroll(self, delta: int) -> None:
        self.top = max(0, min(self.top + delta, self.max_top))
        self.dirty = True

    def find_matches(self) -> None:
        term = self.search_term
        self.matches = [i for i, line in enumerate(self.lines) if term and term in plain(line).lower()]

    def jump_to_match(self, forward: bool, include_top: bool = False) -> None:
        """Go to the next (or previous) line matching the search, wrapping around.

        `include_top` also accepts the line already at the top of the screen.
        """
        if not self.matches:
            self.message = "Not found" if self.search_term else ""
            return
        if forward:
            after = self.top - 1 if include_top else self.top
            target = next((i for i in self.matches if i > after), self.matches[0])
        else:
            target = next((i for i in reversed(self.matches) if i < self.top), self.matches[-1])
        self.top = target
        self.scroll(0)
        self.message = f"{self.matches.index(target) + 1}/{len(self.matches)} lines with «{self.search_term}»"

    # --- keyboard ------------------------------------------------------------

    def read_keys(self, timeout: float) -> list[str]:
        """Keys pressed, waiting up to `timeout` seconds for the first one."""
        fd = sys.stdin.fileno()
        if not select.select([fd], [], [], timeout)[0]:
            return []
        data = os.read(fd, 1024)
        return [KEYS.get(token) or token.decode("utf-8", "replace") for token in RE_KEY.findall(data)]

    def handle(self, key: str) -> bool:
        """Handle a key; returns False to quit."""
        self.dirty = True
        if self.mode == "help":
            self.mode = "view"  # any key closes the help
        elif self.mode == "search":
            self.handle_search_key(key)
        elif self.mode == "pick":
            self.handle_pick_key(key)
        else:
            return self.handle_view_key(key)
        return True

    def handle_search_key(self, key: str) -> None:
        if key in ENTER:
            self.mode, self.search_term = "view", self.search_input.lower()
            self.find_matches()
            self.jump_to_match(forward=True, include_top=True)
        elif key == "esc":
            self.mode = "view"
        elif key in BACKSPACE:
            self.search_input = self.search_input[:-1]
        elif len(key) == 1 and key.isprintable():
            self.search_input += key

    def handle_pick_key(self, key: str) -> None:
        if key in ("up", "k"):
            self.picked = (self.picked - 1) % len(self.files)
        elif key in ("down", "j"):
            self.picked = (self.picked + 1) % len(self.files)
        elif key in ENTER:
            self.mode = "view"
            self.load(self.picked)
        elif key in ("esc", "q", "f3", "f10"):
            self.mode = "view"

    def handle_view_key(self, key: str) -> bool:
        self.message = ""
        page = max(self.view_height - 1, 1)
        if key in ("q", "Q", "esc", "f10"):
            return False
        elif key in ("down", "j", *ENTER):
            self.scroll(1)
        elif key in ("up", "k"):
            self.scroll(-1)
        elif key in ("pgdn", " ", "f"):
            self.scroll(page)
        elif key in ("pgup", "b"):
            self.scroll(-page)
        elif key == "d":
            self.scroll(page // 2)
        elif key == "u":
            self.scroll(-(page // 2))
        elif key in ("home", "g"):
            self.scroll(-len(self.lines))
        elif key in ("end", "G"):
            self.scroll(len(self.lines))
        elif key in ("t", "T"):
            self.next_theme()
        elif key in ("s", "S"):
            self.message = save_theme_message(self.theme)
        elif key in ("f1", "h", "?"):
            self.mode = "help"
        elif key in ("f3", "o"):
            self.mode, self.picked = "pick", self.file_index
        elif key in ("f6", "\t", "right"):
            self.load(self.file_index + 1)
        elif key in ("btab", "left"):
            self.load(self.file_index - 1)
        elif key == "/":
            self.mode, self.search_input = "search", ""
        elif key == "n":
            self.jump_to_match(forward=True)
        elif key == "N":
            self.jump_to_match(forward=False)
        return True

    # --- drawing -------------------------------------------------------------
    # Each part of the screen returns an already positioned string; draw() joins
    # them and writes everything at once, so the screen does not flicker.

    def draw(self) -> None:
        if self.cols < MIN_COLS or self.rows < MIN_ROWS:
            sys.stdout.write(RESET + CLEAR_SCREEN + CURSOR_HOME + "Terminal too small for MDVIEW.")
            sys.stdout.flush()
            return
        parts = [
            CURSOR_HOME + self.menu_bar(),
            move_to(2) + self.frame_top(),
            self.content(),
            move_to(self.rows - 1) + self.frame_bottom(),
            move_to(self.rows) + self.status_bar(),
        ]
        if self.mode == "help":
            parts.append(self.help_dialog())
        elif self.mode == "pick":
            parts.append(self.pick_dialog())
        sys.stdout.write("".join(parts) + RESET)
        sys.stdout.flush()

    def bar(self, left: Line, right: str) -> str:
        """A full-width bar, with `left` on the left and `right` flush to the right."""
        right = clip(right, max(self.cols - 1, 0))
        return compose(left, self.cols - text_width(right), self.theme, "bar") + sgr(self.theme, "bar") + right

    def menu_bar(self) -> str:
        left = [(" ≡ ", "bar", ""), ("MDVIEW", "barhot", "b"), (f" │ {self.current_file}", "bar", "")]
        return self.bar(left, f" {theme_title(self.theme)} │ [{self.file_index + 1}/{len(self.files)}] ")

    def frame_top(self) -> str:
        """Top border of the frame, with the file name centered."""
        frame = sgr(self.theme, "frame", "b")
        corner = "▄▄" if self.ansi else "╔═[■]"
        fade_in, fade_out = ("█▓▒░", "░▒▓█") if self.ansi else ("", "")
        room = self.cols - len(corner) - 1 - len(fade_in + fade_out)
        title = clip(f" {self.current_file.name} ", room - 2)
        left = (room - text_width(title)) // 2
        right = room - left - text_width(title)
        if self.ansi:
            title = sgr(self.theme, "strong", "b") + title + frame
            return f"{frame}{corner}{'▄' * left}{fade_in}{title}{fade_out}{'▄' * right}▄"
        return frame + corner + "═" * left + title + "═" * right + "╗"

    def frame_bottom(self) -> str:
        if self.ansi:
            return sgr(self.theme, "frame", "b") + "▀" * self.cols
        return sgr(self.theme, "frame", "b") + "╚" + "═" * (self.cols - 2) + "╝"

    def content(self) -> str:
        """Visible lines of the document, between the left border and the scrollbar."""
        theme, height = self.theme, self.view_height
        frame = sgr(theme, "frame", "b")
        scrollbar = sgr(theme, "sbar")
        left_gutter = sgr(theme, "text") + " " * (1 + self.margin)
        right_gutter = sgr(theme, "text") + " " * max(self.cols - 3 - self.margin - self.text_width, 0)
        matches = set(self.matches)
        side = "║"
        thumb_char, track = ("█", "░") if self.ansi else ("■", "▒")
        # scrollbar thumb position (the first and last rows are the arrows)
        thumb = 1 + (round((height - 3) * self.top / self.max_top) if self.max_top else 0)
        out = []
        for row in range(height):
            i = self.top + row
            segs = self.lines[i] if i < len(self.lines) else []
            if i in matches:
                segs = highlight(segs, self.search_term)
            if height < 3:
                scroll_char = "║"
            else:
                scroll_char = "▲" if row == 0 else "▼" if row == height - 1 else thumb_char if row == thumb else track
            if self.ansi:  # the border dissolves away from the corners
                edge = min(row, height - 1 - row)
                side = "█▓▒░"[edge] if edge < 4 else "│"
            out.append(
                f"{move_to(row + 3)}{frame}{side}{left_gutter}{compose(segs, self.text_width, theme)}{right_gutter}"
                f"\x1b[{self.cols}G{scrollbar}{scroll_char}"
            )
        return "".join(out)

    def status_bar(self) -> str:
        if self.mode == "search":
            return self.bar([(" Search: ", "barhot", "b"), (self.search_input + "█", "bar", "")], "")
        left: Line = [(" ", "bar", "")]
        for key, label in STATUS_HINTS:
            left += [(key, "barhot", "b"), (f" {label}  ", "bar", "")]
        total = len(self.lines)
        percent = round(100 * self.top / self.max_top) if self.max_top else 100
        first, last = min(self.top + 1, total), min(self.top + self.view_height, total)
        position = f"Ln {first}-{last}/{total} {percent:3d}% "
        return self.bar(left, f" {self.message} │ {position}" if self.message else f" {position}")

    def help_dialog(self) -> str:
        key_width = max(text_width(keys) for keys, _ in HELP_KEYS)
        rows: list[Line] = [[(keys.ljust(key_width + 2), "dlghot", "b"), (what, "dlg", "")] for keys, what in HELP_KEYS]
        rows += [[], [("Press any key to go back", "dlg", "i")]]
        return self.dialog(f"MDVIEW {VERSION} · Keys", rows)

    def pick_dialog(self) -> str:
        # sliding window over the list, keeping the highlighted file in the middle
        visible = max(self.rows - 8, 1)
        first = max(0, min(self.picked - visible // 2, len(self.files) - visible))
        rows: list[Line] = []
        for k in range(first, min(first + visible, len(self.files))):
            selected = k == self.picked
            rows.append([(f" {'►' if selected else ' '} {self.files[k]} ", "dlgsel" if selected else "dlg", "")])
        rows.append([])
        rows.append([
            ("↑↓", "dlghot", "b"), (" select  ", "dlg", ""),
            ("Enter", "dlghot", "b"), (" open  ", "dlg", ""),
            ("Esc", "dlghot", "b"), (" back", "dlg", ""),
        ])  # fmt: skip
        return self.dialog("Open file", rows)

    def dialog(self, title: str, rows: list[Line]) -> str:
        """Centered modal window, with a double border and a shadow."""
        theme = self.theme
        inner = min(max([text_width(plain(row)) for row in rows] + [text_width(title) + 4]), self.cols - 8)
        width, height = inner + 4, len(rows) + 2
        x, y = (self.cols - width) // 2 + 1, max((self.rows - height) // 2 + 1, 2)
        title = clip(f" {title} ", inner)
        left = (inner + 2 - text_width(title)) // 2
        right = inner + 2 - left - text_width(title)
        frame, shadow = sgr(theme, "dlg", "b"), sgr(theme, "dlgshadow")
        out = [f"{move_to(y, x)}{frame}╔{'═' * left}{title}{'═' * right}╗"]
        for k, row in enumerate(rows, 1):
            out.append(f"{move_to(y + k, x)}{frame}║ {compose(row, inner, theme, 'dlg')}{frame} ║{shadow}  ")
        out.append(f"{move_to(y + height - 1, x)}{frame}╚{'═' * (inner + 2)}╝{shadow}  ")
        out.append(f"{move_to(y + height, x + 2)}{shadow}{' ' * width}")
        return "".join(out)

    # --- main loop -----------------------------------------------------------

    def run(self) -> None:
        # Unix-only; imported here so that --no-pager mode works without them.
        import termios
        import tty

        fd = sys.stdin.fileno()
        saved_terminal = termios.tcgetattr(fd)
        signal.signal(signal.SIGWINCH, lambda *_: setattr(self, "resized", True))
        sys.stdout.write(ENTER_FULLSCREEN)
        try:
            tty.setcbreak(fd)  # keys arrive one at a time, with no echo and no waiting for Enter
            self.load(0)
            while True:
                if self.resized:
                    self.resized = False
                    self.layout()
                if self.dirty:
                    self.dirty = False
                    self.draw()
                # the short timeout lets the loop notice a resize without waiting for a key
                for key in self.read_keys(0.25):
                    if not self.handle(key):
                        return
        except KeyboardInterrupt:
            pass
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved_terminal)
            sys.stdout.write(LEAVE_FULLSCREEN)
            sys.stdout.flush()


# =============================================================================
# Command line
# =============================================================================

LOGO = [
    "███╗   ███╗██████╗ ██╗   ██╗██╗███████╗██╗    ██╗",
    "████╗ ████║██╔══██╗██║   ██║██║██╔════╝██║    ██║",
    "██╔████╔██║██║  ██║██║   ██║██║█████╗  ██║ █╗ ██║",
    "██║╚██╔╝██║██║  ██║╚██╗ ██╔╝██║██╔══╝  ██║███╗██║",
    "██║ ╚═╝ ██║██████╔╝ ╚████╔╝ ██║███████╗╚███╔███╔╝",
    "╚═╝     ╚═╝╚═════╝   ╚═══╝  ╚═╝╚══════╝ ╚══╝╚══╝ ",
]

# The help itself is written in Markdown and goes through the renderer.
HELP_MD = """
Reads `.md` files in the terminal in full screen: boxed headings, tables whose
cells wrap, framed code blocks and links numbered in a footer. Uses only the
Python standard library.

## Usage

```
%PROG% [options] FILE.md [FILE.md ...]
%PROG% [options] DIRECTORY
```

Given a **directory**, opens every `.md` file in it (switch files with `F3` or `F6`).

## Options

| Option | What it does |
|---|---|
| `-t`, `--theme NAME` | Color theme (see below). Without it: the `MDVIEW_THEME` variable, then the saved default, then `dark` |
| `--dark`, `--light` | Shortcuts for `--theme dark` and `--theme light` |
| `--save-theme` | Saves the chosen theme as the default (same as the `S` key) |
| `--list-themes` | Lists the themes |
| `-w`, `--width N` | Maximum text width, in columns. Default: the terminal width |
| `-n`, `--no-pager` | Prints the formatted document and exits, without going full screen |
| `--no-color` | No colors (also applies when the `NO_COLOR` variable is set) |
| `-V`, `--version` | Shows the version |
| `-h`, `--help` | Shows this help |

## Themes

| Name | Theme | Look |
|---|---|---|
%THEMES%

## Keys (full-screen mode)

| Key | Action |
|---|---|
%KEYS%

## Examples

```
%PROG% README.md
%PROG% docs/ --light
%PROG% README.md --theme acid
%PROG% --theme acid --save-theme
%PROG% docs/guide.md -w 100
%PROG% README.md --no-pager | less -R
```
"""
HELP_THEME_ROWS = [f"| `{name}` | {theme_title(name)} | {THEME_INFO[name][1].capitalize()} |" for name in THEMES]
HELP_MD = HELP_MD.replace("%PROG%", PROG).replace("%THEMES%", "\n".join(HELP_THEME_ROWS))
HELP_KEY_ROWS = [f"| `{keys}` | {what[0].upper()}{what[1:]} |" for keys, what in HELP_KEYS]
HELP_MD = HELP_MD.replace("%KEYS%", "\n".join(HELP_KEY_ROWS))

MIN_WIDTH_OPTION = 20


def emit(lines: list[Line], theme: str, color: bool, width: int) -> None:
    """Print rendered lines to standard output (non-full-screen mode)."""
    for line in lines:
        if color:
            sys.stdout.write(compose([(" ", "text", ""), *line], width, theme) + RESET + "\n")
        else:
            sys.stdout.write(plain(line).rstrip() + "\n")


def show_help(theme: str, color: bool, width: int) -> None:
    text_w = min(width - 2, 96)
    lines: list[Line] = [[]]
    if text_w >= len(LOGO[0]):
        lines += [[(row, f"logo{k // 2}", "")] for k, row in enumerate(LOGO)]  # gradient: a new shade every 2 rows
    else:
        lines.append([("MDVIEW", "logo0", "b")])
    subtitle = f"Markdown viewer · v{VERSION}"
    lines.append([("░▒▓█ ", "logo2", ""), (subtitle, "strong", "b"), (" █▓▒░", "logo2", "")])
    lines.append([])
    lines += Renderer(theme in ANSI_THEMES).render(HELP_MD, text_w)
    lines.append([])
    emit(lines, theme, color, width)


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # short error message, without argparse's "usage"
        sys.stderr.write(f"{PROG}: error: {message}\nRun '{PROG} --help' to see the options.\n")
        sys.exit(2)


def list_themes(current: str) -> None:
    width = max(len(name) for name in THEMES)
    title_width = max(len(theme_title(name)) for name in THEMES)
    for name in THEMES:
        title, look = THEME_INFO[name]
        mark = "*" if name == current else " "
        print(f"{mark} {name.ljust(width)}  {title.ljust(title_width)}  {look}")
    print(f"\n* = default. Change it with '{PROG} --theme NAME --save-theme' or the S key.")


def build_parser() -> ArgumentParser:
    # add_help=False: -h/--help show our own help (show_help), not argparse's
    parser = ArgumentParser(prog=PROG, add_help=False)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("-t", "--theme", choices=list(THEMES))  # default: default_theme()
    parser.add_argument("--dark", dest="theme", action="store_const", const="dark")
    parser.add_argument("--light", dest="theme", action="store_const", const="light")
    parser.add_argument("--save-theme", action="store_true")
    parser.add_argument("--list-themes", action="store_true")
    parser.add_argument("-w", "--width", type=int)
    parser.add_argument("-n", "--no-pager", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("-V", "--version", action="store_true")
    parser.add_argument("-h", "--help", action="store_true")
    return parser


def collect_files(paths: list[Path], parser: ArgumentParser) -> list[Path]:
    """Expand directories into their `.md` files; exits with an error on an unusable path."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            found = sorted(path.glob("*.md"))
            if not found:
                parser.error(f"no .md files in '{path}'")
            files += found
        elif path.is_file():
            files.append(path)
        else:
            parser.error(f"file not found: '{path}'")
    return files


def print_files(files: list[Path], theme: str, color: bool, cols: int, max_width: int | None) -> None:
    """Non-full-screen mode: prints the formatted documents, one after another."""
    text_w = min(cols - 2, max_width) if max_width else cols - 2
    for k, file in enumerate(files):
        if k:
            sys.stdout.write("\n")
        emit(Renderer(theme in ANSI_THEMES).render(read_file(file), text_w), theme, color, cols)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    interactive = sys.stdout.isatty() and sys.stdin.isatty()
    color = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
    cols = shutil.get_terminal_size().columns

    if args.version:
        print(f"{PROG} {VERSION}")
        return 0
    if args.list_themes:
        list_themes(default_theme())
        return 0
    if args.save_theme:
        if args.theme is None:
            parser.error("--save-theme needs a theme: use it with --theme NAME")
        print(save_theme_message(args.theme), file=sys.stderr if args.paths else sys.stdout)
        if not args.paths:
            return 0
    args.theme = args.theme or default_theme()
    if args.help or not args.paths:
        show_help(args.theme, color, cols)
        return 0
    if args.width is not None and args.width < MIN_WIDTH_OPTION:
        parser.error(f"--width must be at least {MIN_WIDTH_OPTION}")

    files = collect_files(args.paths, parser)
    if interactive and color and not args.no_pager:
        Pager(files, args.theme, args.width).run()
    else:
        print_files(files, args.theme, color, cols, args.width)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # e.g. `mdview file.md -n | head`
        sys.exit(0)
