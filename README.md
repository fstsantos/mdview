# MDVIEW

A Markdown viewer for the terminal that looks like a 90s DOS utility: a blue
full-screen view, box-drawing frames, windows with drop shadows and the 16-color
VGA palette.

It is a single Python file with no dependencies beyond the standard library.

```
 ≡ MDVIEW │ docs/guide.md                                                 [1/3]
╔═[■]═══════════════════════════ guide.md ═════════════════════════════════════╗
║ ╔══════════════════════════════════════════════════════════════════════╗     ▲
║ ║ USER GUIDE                                                           ║     ■
║ ╚══════════════════════════════════════════════════════════════════════╝     ▒
║                                                                              ▒
║ █▓▒░ Installation                                                            ▒
║ ════════════════════════════════════════════════════════════════════════     ▼
╚══════════════════════════════════════════════════════════════════════════════╝
 F1 Help  F3 Open  F6 Next  T Theme  / Search  Q Quit             Ln 1-6/42   0%
```

## Requirements

- Python 3.9 or newer.
- For full-screen mode: Linux, macOS or WSL, in a terminal with 24-bit color
  (nearly all current ones). `--no-pager` mode does not depend on this.

## Installation

The whole program is the single file `mdview.py`. `make install` copies it to
your `PATH` as the `mdview` command:

```sh
sudo make install                # system-wide: /usr/local/bin/mdview
make install PREFIX=~/.local     # current user only: ~/.local/bin/mdview
```

Other targets:

```sh
make install-link PREFIX=~/.local   # symlink to this checkout instead of copying
sudo make uninstall                 # pass the same PREFIX used on install
```

If the destination directory is not on your `PATH`, the install prints the line
to add to your shell profile.

You can also run it without installing: `./mdview.py FILE.md` or
`python3 mdview.py FILE.md`.

## Usage

```sh
mdview [options] FILE.md [FILE.md ...]
mdview [options] DIRECTORY
```

Given a directory, it opens every `.md` file in it (without descending into
subdirectories), in alphabetical order. With no arguments, it shows the help.

```sh
mdview README.md
mdview docs/ --light
mdview docs/guide.md -w 100
mdview README.md --no-pager | less -R
```

### Options

| Option | What it does |
|---|---|
| `-t`, `--theme dark\|light` | Color theme. Default: `dark`, or the value of `MDVIEW_THEME` |
| `--dark`, `--light` | Shortcuts for `--theme dark` and `--theme light` |
| `-w`, `--width N` | Maximum text width, in columns (minimum 20). Default: the terminal width |
| `-n`, `--no-pager` | Prints the formatted document and exits, without going full screen |
| `--no-color` | No colors |
| `-V`, `--version` | Shows the version |
| `-h`, `--help` | Shows the help |

Full-screen mode is only used when both input and output are a terminal and
colors are on. Otherwise (redirected output, `--no-pager`, `--no-color`) the
document is printed and the program exits.

### Environment variables

| Variable | Effect |
|---|---|
| `MDVIEW_THEME` | Default theme (`dark` or `light`) |
| `NO_COLOR` | If set, turns colors off, like `--no-color` |

### Keys (full-screen mode)

| Key | Action |
|---|---|
| `↑` `↓` `j` `k` | Scroll one line |
| `PgUp` `PgDn` `Space` `b` | Scroll one page |
| `d` `u` | Scroll half a page |
| `Home` `End` `g` `G` | Start / end of the document |
| `/` | Search text (`Enter` confirms, `Esc` cancels) |
| `n` `N` | Next / previous match |
| `T` | Toggle dark / light theme |
| `F3` `o` | Open the file list |
| `F6` `Tab` `→` | Next file |
| `Shift+Tab` `←` | Previous file |
| `F1` `h` `?` | Help |
| `F10` `q` `Esc` | Quit |

The mouse wheel also scrolls the document. Search is case-insensitive.

## Supported Markdown

- Headings (`#` to `######`), paragraphs and hard line breaks
- **Bold**, *italic*, ~~strikethrough~~ and `code`
- Links (numbered, with the targets listed in a footer), autolinks and images
  (shown as `[image: description]`)
- Bulleted, numbered, nested and task lists (`- [ ]`, `- [x]`)
- Block quotes (`>`), including other blocks inside them
- Code blocks fenced with ```` ``` ```` or `~~~`, with the language name
- Tables with per-column alignment; long cells wrap
- Horizontal rules
- YAML front matter at the top of the file, shown as a "metadata" block

It is not a full CommonMark parser. Left out, among others: syntax
highlighting, headings underlined with `===`/`---`, code indented by 4 spaces,
reference links (`[text][ref]`), footnotes and HTML (except `<br>`).

## How the code is organized

Everything lives in the `mdview.py` file, split into sections that form a
three-stage pipeline:

| Stage | Where | What it does |
|---|---|---|
| 1 | `parse()` | Markdown → list of blocks (`Heading`, `Paragraph`, `ListBlock`, `Table`...) |
| 2 | `Renderer` | Blocks → lines already wrapped to the screen width |
| 3 | `Pager` | Paints the lines with the theme (ANSI codes) and handles the keyboard |

Each line produced by stage 2 is a list of `(text, style, flags)` segments,
where `style` is a name such as `h1` or `link`. Color is only decided in stage
3, by looking the style up in the `THEMES` table. That separation is what lets
a single key switch themes without rendering the document again.

The other sections of the file support this pipeline:

- **Colors and themes**: the VGA palette, the two themes and `sgr()`, which
  builds the ANSI sequences.
- **Text width**: `text_width()`, `clip()` and `wrap()` measure and wrap text in
  terminal columns (emoji and ideographs take 2, combining accents take 0).
- **Command line**: `main()` and the help screen, which is itself written in
  Markdown and goes through the renderer.

To create a theme, add an entry to `THEMES` with the same style names as the
existing ones; it becomes available to `--theme` automatically (the `T` key
only toggles between `dark` and `light`).
