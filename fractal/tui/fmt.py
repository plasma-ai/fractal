"""Functions for pane rendering.

Timestamps, status glyphs, gauges, fixed-width grid primitives, and the
box-drawing tree renderer. Side-effect-free string/``Text`` builders consuming
``fractal.tui.theme`` tokens, so they are trivially testable and every
color/glyph stays single-sourced.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional, Union

from rich.markup import escape
from rich.text import Text

from fractal.constants import STATUSES

from . import theme

__all__ = [
    'NODE_VERB',
    'timestamp',
    'clock',
    'status_style',
    'dot',
    'cap_bar',
    'trunc',
    'esc',
    'col',
    'cell',
    'row',
    'toggle',
    'dur',
    'money',
    'chat_meta',
    'chat_summary',
    'tree_lines',
]

# event-log desc: the past-tense verb per node-event type (entity rows derive
# theirs from start/end + status); the widest verb also floors the node pane's
# desc column so metadata keeps room before the ellipsis
NODE_VERB = {
    'init': 'initialized',
    'spawn': 'spawned',
    'commit': 'committed',
    'approve': 'approved',
    'merge': 'merged',
    'delete': 'deleted',
    'start': 'started',
    'finish': 'finished',
    'stop': 'stopped',
    'kill': 'killed',
    'pause': 'paused',
    'resume': 'resumed',
    'retire': 'retired',
    'unretire': 'unretired',
}

# the (glyph, color-token name) per lifecycle status, keyed through the real
# vocabulary -- a status added to fractal.constants.STATUSES without a style
# row here fails loudly at import instead of quietly rendering as idle;
# colors are stored by token NAME and resolved per call: a palette selected
# after import (theme.select) must still color the dots
_STYLE_ROWS = {
    'active': (theme.DOT_ON, 'SUCCESS'),
    # paused is live-shaped (frozen mid-work, holding its slot), not
    # settled -- filled, unlike stopped
    'paused': (theme.DOT_ON, 'WARNING'),
    'idle': (theme.DOT_OFF, 'DIM'),
    'completed': (theme.DOT_OFF, 'SUCCESS'),
    'stopped': (theme.DOT_OFF, 'WARNING'),
    'exited': (theme.DOT_OFF, 'ERROR'),
    'killed': (theme.DOT_OFF, 'ERROR'),
    'failed': (theme.DOT_OFF, 'ERROR'),
    'retired': (theme.DOT_OFF, 'DIM'),
}
_STATUS_STYLE = {status: _STYLE_ROWS[status] for status in STATUSES}


def timestamp(at: dt.datetime, tz: dt.tzinfo) -> str:
    """Return the date and time in ``tz``, space-separated (no offset)."""
    return at.astimezone(tz).strftime('%Y-%m-%d %H:%M:%S')


def clock(at: dt.datetime, tz: dt.tzinfo) -> str:
    """Return the wall-clock ``HH:MM:SS`` in ``tz`` (the event-log time column)."""
    return at.astimezone(tz).strftime('%H:%M:%S')


def status_style(status: str, signal: str = '') -> tuple[str, str]:
    """Return the ``(glyph, color)`` for a lifecycle status.

    Filled ``DOT_ON`` means running (or carrying a pending signal, which
    overrides the status color); hollow ``DOT_OFF`` means settled. Statuses
    are the real lifecycle vocabulary (``fractal.constants.STATUSES``); an
    unknown string (a hand-edited ``.status`` file) degrades to the hollow
    dim default.
    """
    # a pending signal overrides the status (the loop honors it next boundary)
    override = {
        'finish': (theme.DOT_ON, theme.SUCCESS),
        'stop': (theme.DOT_ON, theme.WARNING),
        'kill': (theme.DOT_ON, theme.ERROR),
        'pause': (theme.DOT_ON, theme.WARNING),
        'exit': (theme.DOT_ON, theme.ERROR),
    }
    if signal in override:
        return override[signal]
    glyph, color = _STATUS_STYLE.get(status, (theme.DOT_OFF, 'DIM'))
    return glyph, getattr(theme, color)


def dot(status: str, signal: str = '') -> str:
    """Return the status glyph alone, wrapped in its status color."""
    glyph, color = status_style(status, signal)
    return f'[{color}]{glyph}[/]'


def cap_bar(frac: float, *, width: int = theme.BAR_W, warn: bool = False) -> str:
    """Return a gauge toward a cap: green fill, full red once the cap is hit.

    ``warn`` renders the fill yellow -- the run-cost gauge inside the
    configured reserve budget.
    """
    if frac >= 1.0:
        return f'[{theme.ERROR}]{theme.BAR * width}[/]'
    fill = round(max(0.0, frac) * width)
    color = theme.WARNING if warn else theme.SUCCESS
    return (
        f'[{color}]{theme.BAR * fill}[/][{theme.TRACK}]{theme.BAR * (width - fill)}[/]'
    )


def trunc(text: str, width: int) -> str:
    """Truncate to ``width`` with a trailing ellipsis; no padding."""
    if len(text) > width:
        return text[: width - 1] + theme.ELLIPSIS
    return text


def esc(text: str) -> str:
    """Escape Rich markup in untrusted text so it renders literally.

    Agent- and message-authored strings (node titles, radio subjects and
    bodies, event metadata) reach the cockpit through markup f-strings that
    ``Text.from_markup``/``Static`` parse. Without
    escaping, a stray ``[/]`` raises ``MarkupError`` and crashes the pane
    rebuild on the next poll, and ``[link=...]`` injects terminal escape
    sequences. Escape the interpolated value, never the composed string,
    so the surrounding style tags survive.
    """
    return escape(text)


def col(text: str, width: int) -> str:
    """Return a fixed-width column: truncate on overflow, else left-pad."""
    if len(text) > width:
        return trunc(text, width)
    return f'{text:<{width}}'


def cell(markup: str, width: int, justify: str = 'left') -> Text:
    """Return a markup-safe fixed-width cell.

    Pads/truncates by VISIBLE width, so colored dots and ``[dim]`` wrappers
    never break column alignment (the f-string ``{x:<8}`` trap).
    """
    result = Text.from_markup(markup)
    result.truncate(width, overflow='ellipsis')
    pad = max(0, width - result.cell_len)
    if justify == 'right':
        padded = Text(' ' * pad)
        padded.append_text(result)
        return padded
    result.pad_right(pad)
    return result


def row(*cells: Union[Text, str], gap: int = theme.GAP) -> Text:
    """Join ``cell`` Texts (or markup strings) into one aligned row."""
    result = Text()
    for index, item in enumerate(cells):
        if index:
            result.append(' ' * gap)
        result.append_text(
            item if isinstance(item, Text) else Text.from_markup(str(item))
        )
    return result


def toggle(labels: tuple[str, str], active: str, lit: bool) -> str:
    """Render a two-segment switch: each label boxed with a space per side.

    The active segment always sits on a brighter chip than the inactive
    (resting AND lit/hover), so selection reads from the chip.
    """
    result = []
    for label in labels:
        if label == active:
            if lit:
                style = f'{theme.INK_BRIGHT} on {theme.LIT_ACTIVE}'
            else:
                style = f'{theme.INK} on {theme.SEL}'
        else:
            if lit:
                style = f'{theme.INK} on {theme.LIT}'
            else:
                style = f'{theme.CHROME} on {theme.SURFACE}'
        result.append(f'[{style}] {label} [/]')
    return ''.join(result)


def dur(secs: Optional[float]) -> str:
    """Return a compact duration (``43s`` / ``18m`` / ``1h``); ellipsis for none."""
    if secs is None:
        return theme.ELLIPSIS
    if secs < 60:
        return f'{secs:.0f}s'
    if secs < 3_600:
        return f'{secs / 60:.0f}m'
    return f'{secs / 3_600:.0f}h'


def money(value: Optional[float]) -> str:
    """Return a dollar figure (``$1,083.42``); ellipsis for none (not yet recorded)."""
    if value is None:
        return theme.ELLIPSIS
    return f'${value:,.2f}'


def chat_meta(duration: float) -> str:
    """Return a chat turn's closing meta line (wall clock alone)."""
    return f'done {theme.SEP} {duration:.1f}s'


def chat_summary(
    turns: Optional[int],
    duration: float,
    cost: Optional[float],
) -> str:
    """Return a chat result's closing summary line.

    A turn-counting result (claude) closes on turns/duration/cost; a result
    without a turn count (codex) closes on duration and cost. An absent
    cost fact reads ``$?``, never ``$0`` -- the same placeholder the CLI's
    final close prints.
    """
    cost_str = f'${cost:.2f}' if cost is not None else '$?'
    if turns is None:
        return f'done {theme.SEP} {duration:.1f}s {theme.SEP} {cost_str}'
    return (
        f'done {theme.SEP} {turns} turns {theme.SEP}'
        f' {duration:.1f}s {theme.SEP} {cost_str}'
    )


# ------ tree


def tree_lines(
    rows: list[dict],
    collapsed: set[str],
) -> list[tuple[str, str, str, str]]:
    """Render the whole-tree rows as box-drawing lines.

    ``rows`` is the DFS-ordered list of tree-row dicts from the snapshot (each
    with ``branch``/``name``/``depth``/``status``/``signal``/``is_user``/
    ``is_focused``/``has_kids``). Descendants of a collapsed branch are hidden.
    The glyph head (box-drawing prefix, caret, dot) is kept apart from the
    label so a wrapping label hangs under its own start column; the hang line
    carries the still-continuing vertical glyphs under those wrapped lines so
    the tree's lines stay unbroken.

    Args:
        rows: DFS-ordered tree rows.
        collapsed: Branches whose subtrees are folded.

    Returns:
        ``(branch, head_markup, hang_markup, label_markup)`` tuples for the
        visible rows.

    """
    # visibility: skip descendants of a collapsed branch
    visible: list[dict] = []
    skip: Optional[int] = None
    for entry in rows:
        depth = entry['depth']
        if skip is not None and depth > skip:
            continue
        skip = None
        visible.append(entry)
        if entry['has_kids'] and entry['branch'] in collapsed:
            skip = depth
    # last-among-siblings flag per visible row
    count = len(visible)
    is_last = [True] * count
    for index, entry in enumerate(visible):
        for ahead in range(index + 1, count):
            if visible[ahead]['depth'] < entry['depth']:
                break
            if visible[ahead]['depth'] == entry['depth']:
                is_last[index] = False
                break
    # assemble the box-drawing prefix + marker + dot + name per row
    result = []
    cont: dict[int, bool] = {}
    for index, entry in enumerate(visible):
        depth = entry['depth']
        parts = [
            theme.PIPE if cont.get(level) else theme.INDENT for level in range(1, depth)
        ]
        if depth >= 1:
            parts.append(theme.ELBOW if is_last[index] else theme.TEE)
        prefix = ''.join(parts)
        if depth >= 1:
            cont[depth] = not is_last[index]
        # the hang line -- what continues beneath the glyphs when the label
        # wraps: ancestor columns carry through, a tee becomes a pipe, and an
        # elbow (its line ends at this row) becomes indent
        hang = ''.join(
            theme.PIPE if cont.get(level) else theme.INDENT
            for level in range(1, depth + 1)
        )
        hang = f'[{theme.DIM}]{hang}[/]' if hang else ''
        if entry['has_kids']:
            if entry['branch'] in collapsed:
                caret = theme.CARET_CLOSED
            else:
                caret = theme.CARET_OPEN
            marker = f'[{theme.DIM}]{caret}[/] '
        else:
            marker = '  '
        name = esc(entry['name'])
        if entry['is_focused']:
            name = f'[b]{name}[/]'
        else:
            name = f'[{theme.DIM}]{name}[/]'
        if entry['is_user']:
            # the user (root) node sits outside the agent lifecycle: a filled
            # circle in the row's text color instead of a status color
            if entry['is_focused']:
                mark = f'[{theme.INK}]{theme.DOT_ON}[/]'
            else:
                mark = f'[{theme.DIM}]{theme.DOT_ON}[/]'
        else:
            mark = dot(entry['status'], entry['signal'])
        head = f'[{theme.DIM}]{prefix}[/]{marker}{mark} '
        result.append((entry['branch'], head, hang, name))
    return result
