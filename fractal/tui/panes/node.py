"""Implements ``NodePane`` -- the node card, runs explorer, and event log.

The right-hand pane (mode: ``node``) shows the focused node's headline state
(status, run/iter/step line, agent/model/session, the measures matrix, config
chips), the run -> iteration -> step explorer, and the unified activity
timeline. Three sub-zones share the mode: ``top`` (the card), ``mid`` (the
explorer, row-selected), and ``rows`` (the event log, row-selected).
"""

from __future__ import annotations

import json
import typing
from typing import Any, Optional, Union

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import Click, Enter, Key, Leave
from textual.widgets import Rule as Divider, Static

import fractal.tui.fmt
import fractal.tui.theme
from fractal.tui.data import leaf_of, user_tag
from fractal.tui.widgets import PaneScroll

if typing.TYPE_CHECKING:
    from textual.widget import Widget

    from fractal.tui.app import FractalApp
    from fractal.tui.snapshot import Snapshot

__all__ = ['NodePane']

# a pending signal shows in the head as its present participle
_SIGNAL_ING = {
    'finish': 'finishing',
    'stop': 'stopping',
    'kill': 'killing',
    'pause': 'pausing',
    'exit': 'exiting',
}

# config chips: config.json keys in schema order, minus the keys the card
# already shows as a denominator (max_iters in `iter n/m`, the six cap keys
# in the measures matrix); agent/model are NOT excluded -- the card shows the
# current step's agent/model, the chips show the node default (they can differ)
_CONFIG_ORDER = (
    'title',
    'user',
    'project',
    'scope',
    'clone_dirs',
    'base',
    'meta',
    'agent',
    'provider',
    'model',
    'effort',
    'max_iters',
    'max_depth',
    'max_children',
    'max_descendants',
    'timeout',
    'iter_timeout',
    'step_timeout',
    'step_retries',
    'step_retry_backoff',
    'interval',
    'sleep',
    'wait',
    'max_cost',
    'max_iter_cost',
    'max_step_cost',
    'reserve_budget',
    'sync',
    'local',
    'detached',
    'blind',
    'sealed',
)
_SPOKEN_CONFIG = frozenset(
    {
        'max_iters',
        'timeout',
        'iter_timeout',
        'step_timeout',
        'max_cost',
        'max_iter_cost',
        'max_step_cost',
    }
)


class LogScopeToggle(Static):
    """The node/descendants log-scope switch: brightens on mouse hover."""

    def on_enter(self: LogScopeToggle, event: Enter) -> None:
        """Brighten on hover."""
        self.app.node_pane.set_log_hover(True)

    def on_leave(self: LogScopeToggle, event: Leave) -> None:
        """Dim when the pointer leaves."""
        self.app.node_pane.set_log_hover(False)

    def on_click(self: LogScopeToggle, event: Click) -> None:
        """Flip the log scope."""
        self.app.node_pane.toggle_log_scope()


class NodePane:
    """The NODE pane: card (``top``) + runs explorer (``mid``) + event log (``rows``)."""

    def __init__(self: NodePane, app: FractalApp) -> None:
        """Initialize ``NodePane``.

        Args:
            app: The owning cockpit app (widget access + mode flips).

        """
        self.app = app
        self.zone = 'mid'
        self.ex_sel = 0
        self.ex_expanded: set[tuple] = set()
        self.ev_sel = 0
        # toggled choices stick per branch for the session; a branch never
        # toggled falls back to log_scope's default rule
        self.sub_log_by_branch: dict[str, bool] = {}
        # include descendants in the event log
        self.sub_log = self.log_scope(app.scope)
        self.log_hover = False

    def compose(self: NodePane) -> ComposeResult:
        """Compose the pane interior (content mounts on the first rebuild)."""
        yield Static('', id='nodehead')
        yield Divider()
        with Vertical(id='nodecard'):
            yield Static('', id='noderun')
            yield Static('', id='nodeident')
            yield Static('', id='nodemeasures')
            yield Static('', id='nodeconfig')
        yield Divider()
        yield Static('', id='nodeexcols')
        with Vertical(id='nodebody'):
            yield PaneScroll(id='nodeexplore')
            with Vertical(id='nodeactivity'):
                yield Divider(id='nodeevdiv')
                yield LogScopeToggle('', id='nodelogscope')
                yield Static('', id='nodeevcols')
                yield PaneScroll(id='nodeevents')
        with Vertical(classes='footwrap'):
            yield Divider()
            yield Static(self._foot(), id='nodefoot')

    def rebuild(self: NodePane, snap: Snapshot) -> None:
        """Re-render the whole pane (card + explorer + event log)."""
        self.set_card(snap)
        self.rebuild_body(snap)

    def set_card(self: NodePane, snap: Snapshot) -> None:
        """Update the card: head, run line, ident, measures, config chips.

        While the explorer drives a selection the card time-machines to the
        highlighted run/iter/step; otherwise it tracks the live context.
        """
        card, measures = self._context_view(snap)
        self.app.query_one('#nodehead', Static).update(self._head(card))
        self.app.query_one('#noderun', Static).update(self._run_line(measures))
        self.app.query_one('#nodeident', Static).update(self._ident(card))
        self.app.query_one('#nodemeasures', Static).update(self._measures(measures))
        config = self.app.query_one('#nodeconfig', Static)
        config.update(self._config_chips(snap))
        # hovering the chips shows the whole config file as pretty JSON
        config.tooltip = self._config_text(snap)

    def _context_view(
        self: NodePane,
        snap: Snapshot,
    ) -> tuple[Optional[dict], Optional[dict]]:
        """Resolve the (card, measures) pair the card renders.

        The explorer selection's context while the user drives it, the live
        context otherwise.
        """
        selected = self._selection(snap)
        if selected is None:
            return snap.card, self._live_measures(snap.measures)
        ref, run, it, step = selected
        card = dict(snap.card)
        if step is not None:
            card['agent'] = step['agent'] or card['agent']
            card['model'] = step['model'] or card['model']
            card['session'] = step['session']
        # the card reads as it would have at the END of the hovered row:
        # ancestors show to-date values (elapsed up to, spend as of); the
        # hovered row and its representatives show their own spans
        level = len(ref)
        hovered = step if level >= 3 else (it if level == 2 else run)
        t = self._end(hovered)
        if level >= 3:
            cost_run, cost_iter = step['run_spend'], step['iter_spend']
        elif level == 2:
            cost_run = it['run_spend']
            cost_iter = it['cost_raw']
        else:
            cost_run = run['spend']
            cost_iter = it['cost_raw'] if it else None
        measures = dict(snap.measures)
        iter_num = it['iter'] if it else None
        step_num = step['step'] if step else None
        step_name = step['name'] if step else None
        elapsed_iter = self._to_date(it, t) if level >= 3 else self._span(it)
        elapsed_run = self._to_date(run, t) if level >= 2 else self._span(run)
        cost_step = step['cost_raw'] if step else None
        measures.update(
            run=run['number'],
            iter=iter_num,
            step=step_num,
            step_name=step_name,
            elapsed_step=self._span(step),
            elapsed_iter=elapsed_iter,
            elapsed_run=elapsed_run,
            cost_step=cost_step,
            cost_iter=cost_iter,
            cost_run=cost_run,
        )
        return card, measures

    def _selection(self: NodePane, snap: Snapshot) -> Optional[tuple]:
        """Resolve the highlighted explorer row to (run, iter, step) rows.

        The iter/step fall back to the newest/last the way the live context
        does.
        """
        if self.app.mode != 'node' or self.zone != 'mid' or not snap.history:
            return None
        rows = self._ex_rows(snap)
        if not rows:
            return None
        ref = rows[min(self.ex_sel, len(rows) - 1)]
        run = snap.history[ref[0]]
        iters = run['iters']
        it = iters[ref[1]] if len(ref) >= 2 else (iters[0] if iters else None)
        steps = it['steps'] if it is not None else ()
        step = steps[ref[2]] if len(ref) >= 3 else (steps[-1] if steps else None)
        return ref, run, it, step

    def _span(self: NodePane, row: Optional[dict]) -> Optional[float]:
        """Compute a row's span: stored when settled, ticking while open."""
        if row is None:
            return None
        if row['duration'] is not None:
            return row['duration']
        started = row['started']
        if started is None:
            return None
        return max(0.0, self.app.now() - started.timestamp())

    def _end(self: NodePane, row: dict) -> float:
        """Compute a row's end instant (an open row reads as the clock)."""
        if row['started'] is None or row['duration'] is None:
            return self.app.now()
        return row['started'].timestamp() + row['duration']

    def _to_date(self: NodePane, row: Optional[dict], t: float) -> Optional[float]:
        """Compute the elapsed from a row's start to the hovered instant."""
        if row is None or row['started'] is None:
            return None
        return max(0.0, t - row['started'].timestamp())

    def _live_measures(self: NodePane, m: Optional[dict]) -> Optional[dict]:
        """Re-tick the open rows' elapsed figures against the render clock.

        The snapshot bakes elapsed at build time and a quiet tree reuses the
        same snapshot tick after tick, so the open rows' start instants are
        measured here -- the card's clocks and cap gauges stay live between
        disk writes.
        """
        if m is None:
            return None
        live = dict(m)
        for scope in ('step', 'iter', 'run'):
            started = m[f'started_{scope}']
            if started is not None:
                live[f'elapsed_{scope}'] = max(0.0, self.app.now() - started)
        return live

    def rebuild_body(self: NodePane, snap: Snapshot) -> None:
        """Re-mount the explorer and the event log."""
        self._ex_rebuild(snap)
        self._ev_rebuild(snap)

    def rescope(self: NodePane, snap: Snapshot) -> None:
        """Reset selection state and re-render for a new scope."""
        self.ex_sel = 0
        self.ex_expanded = set()
        self.ev_sel = 0
        self.zone = 'mid'
        self.rebuild(snap)

    def _content_w(self: NodePane) -> int:
        """Return the node pane's content width (inside border + padding)."""
        node_width = self.app.snapshot.geometry.node_width
        return node_width - 2 * (
            fractal.tui.theme.BORDER_W + fractal.tui.theme.PANE_PAD
        )

    def _head(self: NodePane, card: Optional[dict]) -> str:
        """Render card row 1.

        Status (glyph + word) hard left, branch centered in grey, the pending
        signal (present participle) hard right in grey.
        """
        if not card:
            return f'[{fractal.tui.theme.DIM}]no node[/]'
        glyph, color = fractal.tui.fmt.status_style(card['status'], card['signal'])
        status = card['status']
        label = f'{glyph} {status}'
        if card['signal']:
            signal = _SIGNAL_ING.get(card['signal'], card['signal'])
        else:
            signal = ''
        width = self._content_w()
        branch = card['branch'] + user_tag(card['branch'], self.app.data.root_branch)
        # truncate only on real collision: the branch may use everything
        # between the status (left) and the signal (right), one space clear
        room = width - len(label) - len(signal) - 2
        if len(branch) > room:
            branch = branch[: max(1, room - 1)] + fractal.tui.theme.ELLIPSIS
        start = (width - len(branch)) // 2
        if signal:
            start = min(start, width - len(signal) - 1 - len(branch))
        start = max(start, len(label) + 1)
        lead = start - len(label)
        gap = ' ' * lead
        result = f'[{color}]{label}[/]{gap}[{fractal.tui.theme.CHROME}]{branch}[/]'
        if signal:
            mid = max(1, width - len(signal) - start - len(branch))
            gap = ' ' * mid
            result += f'{gap}[{fractal.tui.theme.CHROME}]{signal}[/]'
        return result

    def _run_line(self: NodePane, m: Optional[dict]) -> str:
        """Render card row 2: run / iter / step, centered, dim separators.

        A selected SYNC pre-step reads as plain ``sync``, like the explorer.
        """
        if not m or m['run'] is None:
            return ''
        iter_num = m['iter']
        iter_max = m['iter_max']
        if iter_max:
            it = f'{iter_num}/{iter_max}'
        else:
            it = f'{iter_num}'
        run_num = m['run']
        parts = [f'run {run_num}', f'iter {it}']
        if m['step'] == 0:
            parts.append('sync')
        elif m['step'] is not None:
            step_num = m['step']
            step_total = m['step_total']
            step_name = fractal.tui.fmt.esc(m['step_name'])
            parts.append(f'step {step_num}/{step_total} ({step_name})')
        plain = f' {fractal.tui.theme.SEP} '.join(parts)
        pad = max(0, (self._content_w() - len(plain)) // 2)
        joiner = f' [{fractal.tui.theme.DIM}]{fractal.tui.theme.SEP}[/] '
        return ' ' * pad + joiner.join(parts)

    def _ident(self: NodePane, card: Optional[dict]) -> str:
        """Render the agent / model / full session id block.

        The session is the currently running one (the open iteration's
        newest); the chips show the node defaults -- they can differ.
        """
        if not card:
            return f'[{fractal.tui.theme.DIM}]no node[/]'
        # agent/model/session are agent-editable config values -- escape them
        # (like _config_chips/_ev_desc) so a stray '[/]' can't MarkupError the
        # pane rebuild; the surrounding style tags are added after escaping
        agent = fractal.tui.fmt.esc(card['agent'] or fractal.tui.theme.EMPTY)
        if card['detached']:
            agent += f' [{fractal.tui.theme.DIM}](detached)[/]'
        model = fractal.tui.fmt.esc(card['model'] or fractal.tui.theme.EMPTY)
        session = fractal.tui.fmt.esc(card['session'] or fractal.tui.theme.EMPTY)
        agent_col = fractal.tui.fmt.col('agent', fractal.tui.theme.IDENT_W)
        model_col = fractal.tui.fmt.col('model', fractal.tui.theme.IDENT_W)
        session_col = fractal.tui.fmt.col('session', fractal.tui.theme.IDENT_W)
        return (
            f'[{fractal.tui.theme.DIM}]{agent_col}[/]{agent}\n'
            f'[{fractal.tui.theme.DIM}]{model_col}[/]{model}\n'
            f'[{fractal.tui.theme.DIM}]{session_col}[/]{session}'
        )

    def _measures(self: NodePane, m: Optional[dict]) -> Union[Text, str]:
        """Render the scope x metric matrix (rows step/iter/run, time/cost).

        Each cell is ``<current>/<ceiling>`` plus a green->red gauge of the
        fraction used; ``-`` and an empty gauge when uncapped. The iter COUNT
        has no row -- it shows in row 2 (iter n/m).
        """
        if not m:
            return ''
        scopes = [
            (
                'step',
                m['elapsed_step'],
                m['cap_step_s'],
                m['cost_step'],
                m['cap_step_cost'],
            ),
            (
                'iter',
                m['elapsed_iter'],
                m['cap_iter_s'],
                m['cost_iter'],
                m['cap_iter_cost'],
            ),
            ('run', m['elapsed_run'], m['cap_run_s'], m['cost_run'], m['cap_run_cost']),
        ]
        # render the figures first: each gauge column starts three columns
        # past its column's longest figure, and the two gauges split the
        # leftover width; an over-long figure truncates (ellipsis) rather than
        # squeezing the gauges out
        figs = []
        for scope, elapsed, elapsed_cap, cost, cost_cap in scopes:
            fig = (
                scope,
                _figure(elapsed, elapsed_cap, fractal.tui.fmt.dur),
                (elapsed or 0) / elapsed_cap if elapsed_cap else 0.0,
                _figure(cost, cost_cap, fractal.tui.fmt.money),
                (cost or 0) / cost_cap if cost_cap else 0.0,
            )
            figs.append(fig)
        # both gauges share one fixed width and trail their text (the row
        # ends ragged-right rather than stretching to the pane edge)
        gap = fractal.tui.theme.GAUGE_GAP
        avail = self._content_w() - fractal.tui.theme.MEAS_W - 2 * fractal.tui.theme.GAP
        el_fig = max(len(fig[1][1]) for fig in figs)
        co_fig = max(len(fig[3][1]) for fig in figs)
        bar_w = fractal.tui.theme.BAR_W
        budget = max(8, avail - 2 * gap - 2 * bar_w)
        if el_fig + co_fig > budget:
            # over-long figures truncate (ellipsis) rather than
            # pushing the gauges out
            el_fig = min(el_fig, max(4, budget // 3))
            co_fig = max(4, budget - el_fig)
        # header: blank corner over the scope labels, then the metric columns
        rows = [
            fractal.tui.fmt.row(
                fractal.tui.fmt.cell('', fractal.tui.theme.MEAS_W),
                fractal.tui.fmt.cell(
                    markup=f'[{fractal.tui.theme.DIM}]time[/]',
                    width=el_fig + gap + bar_w,
                ),
                fractal.tui.fmt.cell(
                    markup=f'[{fractal.tui.theme.DIM}]cost[/]',
                    width=co_fig + gap + bar_w,
                ),
            )
        ]
        # the run-cost gauge warns once spend enters the configured reserve
        reserve = m.get('reserve_budget')
        for scope, (el_markup, _), el_frac, (co_markup, _), co_frac in figs:
            warn = bool(
                scope == 'run'
                and reserve
                and m['cap_run_cost']
                and m['cap_run_cost'] - (m['cost_run'] or 0) <= reserve
            )
            el_bar = fractal.tui.fmt.cap_bar(el_frac, width=bar_w)
            co_bar = fractal.tui.fmt.cap_bar(co_frac, width=bar_w, warn=warn)
            rows.append(
                fractal.tui.fmt.row(
                    fractal.tui.fmt.cell(
                        markup=f'[{fractal.tui.theme.DIM}]{scope}[/]',
                        width=fractal.tui.theme.MEAS_W,
                    ),
                    fractal.tui.fmt.row(
                        fractal.tui.fmt.cell(el_markup, el_fig),
                        fractal.tui.fmt.cell(markup=el_bar, width=bar_w),
                        gap=gap,
                    ),
                    fractal.tui.fmt.row(
                        fractal.tui.fmt.cell(co_markup, co_fig),
                        fractal.tui.fmt.cell(markup=co_bar, width=bar_w),
                        gap=gap,
                    ),
                )
            )
        return Text('\n').join(rows)

    def _config_chips(self: NodePane, snap: Snapshot) -> str:
        """Render the config chips (schema order, non-null json values).

        Items already shown above are skipped; chips pack into <= 3 rows,
        and a trailing unboxed ``...`` marks any that did not fit.
        """
        config = snap.config
        chips = [
            f'"{key}": {_json_val(config.get(key))}'
            for key in _CONFIG_ORDER
            if key not in _SPOKEN_CONFIG and config.get(key) is not None
        ]
        width = self._content_w()
        rows: list[list[str]] = [[]]
        widths = [0]
        leftover: list[str] = []
        for index, text in enumerate(chips):
            chip_w = len(text) + 2  # the box adds a space each side
            last = len(rows) - 1
            add = chip_w if not rows[last] else 1 + chip_w
            if widths[last] + add <= width:
                rows[last].append(text)
                widths[last] += add
            elif len(rows) < 3:
                rows.append([text])
                widths.append(chip_w)
            else:
                leftover = chips[index:]
                break
        result = []
        for index, row in enumerate(rows):
            # muted grey chips (a box like the unselected kind-toggle option);
            # the chip text embeds agent-writable config values, so escape it
            cells = [
                f'[{fractal.tui.theme.DIM} on {fractal.tui.theme.SURFACE}]'
                f' {fractal.tui.fmt.esc(text)} [/]'
                for text in row
            ]
            if leftover and index == len(rows) - 1:
                cells.append(f'[{fractal.tui.theme.DIM}]...[/]')
            result.append(' '.join(cells))
        return '\n'.join(result)

    def _config_text(self: NodePane, snap: Snapshot) -> Text:
        """Render the config file as pretty-printed JSON (the chips' tooltip)."""
        return Text(json.dumps(snap.config, indent=2), style=fractal.tui.theme.DIM)

    def _foot(self: NodePane) -> str:
        """Render the foot hints for the active zone."""
        if self.app.mode == 'node' and self.zone == 'top':
            return (
                f'[{fractal.tui.theme.DIM}]{fractal.tui.theme.RET} chat this session'
                f' {fractal.tui.theme.SEP} {fractal.tui.theme.DOWN} runs'
                f' {fractal.tui.theme.SEP} esc back[/]'
            )
        if self.app.mode == 'node' and self.zone == 'scope':
            return (
                f'[{fractal.tui.theme.DIM}]{fractal.tui.theme.LEFT}'
                f'{fractal.tui.theme.RIGHT}/{fractal.tui.theme.RET} flip scope'
                f' {fractal.tui.theme.SEP} {fractal.tui.theme.DOWN} log'
                f' {fractal.tui.theme.SEP} esc back[/]'
            )
        if self.app.mode == 'node' and self.zone == 'rows':
            return (
                f'[{fractal.tui.theme.DIM}]{fractal.tui.theme.UP}'
                f'{fractal.tui.theme.DOWN} select {fractal.tui.theme.SEP}'
                f' {fractal.tui.theme.RET} open in runs'
                f' {fractal.tui.theme.SEP} t subtree'
                f' {fractal.tui.theme.SEP} esc back[/]'
            )
        return (
            f'[{fractal.tui.theme.DIM}]{fractal.tui.theme.UP}'
            f'{fractal.tui.theme.DOWN} select {fractal.tui.theme.SEP}'
            f' {fractal.tui.theme.RIGHT}/{fractal.tui.theme.RET} expand'
            f' {fractal.tui.theme.SEP} {fractal.tui.theme.LEFT} collapse'
            f' {fractal.tui.theme.SEP} {fractal.tui.theme.DOWN} event log'
            f' {fractal.tui.theme.SEP} esc back[/]'
        )

    def _ex_rows(self: NodePane, snap: Snapshot) -> list[tuple]:
        """List the visible explorer refs (runs, then expanded levels)."""
        rows: list[tuple] = []
        for run_index, run in enumerate(snap.history):
            rows.append((run_index,))
            if (run_index,) not in self.ex_expanded:
                continue
            for iter_index, it in enumerate(run['iters']):
                rows.append((run_index, iter_index))
                if (run_index, iter_index) not in self.ex_expanded:
                    continue
                for step_index in range(len(it['steps'])):
                    rows.append((run_index, iter_index, step_index))
        return rows

    def _ex_entry(self: NodePane, snap: Snapshot, ref: tuple) -> dict:
        """Resolve an explorer ref to its history entry."""
        entry: dict = snap.history[ref[0]]
        if len(ref) >= 2:
            entry = entry['iters'][ref[1]]
        if len(ref) >= 3:
            entry = entry['steps'][ref[2]]
        return entry

    def _ex_row(self: NodePane, snap: Snapshot, ref: tuple) -> Static:
        """Render one explorer row (caret, status dot, label, columns)."""
        entry = self._ex_entry(snap, ref)
        level = len(ref) - 1
        if level < 2:
            if ref in self.ex_expanded:
                caret = fractal.tui.theme.CARET_OPEN
            else:
                caret = fractal.tui.theme.CARET_CLOSED
            marker = f'{caret} '
        else:
            marker = '  '
        # a status dot leads the label; session / dur / cost columns follow,
        # built markup-safe via fmt.cell/row so the dot never drifts alignment
        indent = '  ' * level
        dot = fractal.tui.fmt.dot(entry.get('status') or '')
        text = fractal.tui.fmt.esc(entry['label'])
        label = f'{indent}{marker}{dot} {text}'
        if entry.get('duration'):
            duration = fractal.tui.fmt.dur(entry['duration'])
        else:
            duration = ''
        session = fractal.tui.fmt.esc(entry.get('session') or '-')
        cost = entry.get('cost') or ''
        return Static(
            fractal.tui.fmt.row(
                fractal.tui.fmt.cell(label, snap.geometry.label_w),
                fractal.tui.fmt.cell(
                    markup=f'[{fractal.tui.theme.DIM}]{session}[/]',
                    width=fractal.tui.theme.SESS_W,
                ),
                fractal.tui.fmt.cell(
                    markup=f'[{fractal.tui.theme.DIM}]{duration}[/]',
                    width=snap.geometry.ev_dur_w,
                    justify='right',
                ),
                fractal.tui.fmt.cell(
                    markup=f'[{fractal.tui.theme.DIM}]{cost}[/]',
                    width=snap.geometry.ev_cost_w,
                    justify='right',
                ),
            ),
            classes='exrow',
        )

    def _ex_cols(self: NodePane, snap: Snapshot) -> Text:
        """Render the explorer column header (aligned with the rows)."""
        return fractal.tui.fmt.row(
            fractal.tui.fmt.cell('', snap.geometry.label_w),
            fractal.tui.fmt.cell(
                markup=f'[{fractal.tui.theme.DIM}]session[/]',
                width=fractal.tui.theme.SESS_W,
            ),
            fractal.tui.fmt.cell(
                markup=f'[{fractal.tui.theme.DIM}]elapsed[/]',
                width=snap.geometry.ev_dur_w,
                justify='right',
            ),
            fractal.tui.fmt.cell(
                markup=f'[{fractal.tui.theme.DIM}]cost[/]',
                width=snap.geometry.ev_cost_w,
                justify='right',
            ),
        )

    def _ev_cols(self: NodePane, snap: Snapshot) -> Text:
        """Render the activity column header (aligned with the rows)."""
        g = snap.geometry
        return fractal.tui.fmt.row(
            fractal.tui.fmt.cell(
                markup=f'[{fractal.tui.theme.DIM}]time[/]',
                width=fractal.tui.theme.TIME_W,
            ),
            fractal.tui.fmt.cell(f'[{fractal.tui.theme.DIM}]node[/]', g.ev_node_w),
            fractal.tui.fmt.cell(f'[{fractal.tui.theme.DIM}]run[/]', g.ev_run_w),
            fractal.tui.fmt.cell(f'[{fractal.tui.theme.DIM}]iter[/]', g.ev_iter_w),
            fractal.tui.fmt.cell(f'[{fractal.tui.theme.DIM}]step[/]', g.ev_step_w),
            fractal.tui.fmt.cell(f'[{fractal.tui.theme.DIM}]event[/]', g.desc_w),
            fractal.tui.fmt.cell(
                markup=f'[{fractal.tui.theme.DIM}]elapsed[/]',
                width=g.ev_dur_w,
                justify='right',
            ),
            fractal.tui.fmt.cell(
                markup=f'[{fractal.tui.theme.DIM}]cost[/]',
                width=g.ev_cost_w,
                justify='right',
            ),
        )

    def _ex_rebuild(self: NodePane, snap: Snapshot) -> None:
        """Re-mount the explorer rows."""
        self.app.query_one('#nodeexcols', Static).update(self._ex_cols(snap))
        box = self.app.query_one('#nodeexplore', PaneScroll)
        rows = self._ex_rows(snap)
        self.ex_sel = min(self.ex_sel, max(0, len(rows) - 1))
        box.remount(*[self._ex_row(snap, ref) for ref in rows])
        # runs collapsed (default) -> capped at half the body; any expansion ->
        # the runs view may grow to hide the event log before it scrolls
        box.set_class(bool(self.ex_expanded), 'expanded')
        self.app.call_after_refresh(self._ex_paint)

    def _ex_paint(self: NodePane) -> None:
        """Paint the explorer row highlight."""
        for index, widget in enumerate(self.app.query('#nodeexplore .exrow')):
            selected = (
                self.app.mode == 'node' and self.zone == 'mid' and index == self.ex_sel
            )
            widget.set_class(selected, 'rsel')
            if selected:
                widget.scroll_visible(animate=False)
        # the card follows the highlight (and snaps back to live on leave)
        self.set_card(self.app.snapshot)

    def _ev_verb(self: NodePane, event: dict) -> str:
        """Pick the event's past-tense word.

        Entities read ``started`` or their end status; node events read the
        past tense of the event name (spawn -> spawned). A NULL ``event_id``
        marks the view-synthesized entity-start rows -- a real ``start``
        event renders as an ordinary node event.
        """
        if event['event'] == 'start' and event['event_id'] is None:
            return 'started'
        if event['kind'] == 'node':
            return fractal.tui.fmt.NODE_VERB.get(event['event'], event['event'])
        return event['status']

    def _ev_color(self: NodePane, event: dict) -> str:
        """Pick the verb's color.

        One rule: a status word takes its status color, every other verb
        (starts, node events) is muted -- the node column carries the ink.
        Sync passes are routine chrome and stay muted throughout.
        """
        if event['event'] == 'start' or event['kind'] == 'node':
            return fractal.tui.theme.DIM
        if event['sync']:
            return fractal.tui.theme.DIM
        _, color = fractal.tui.fmt.status_style(event['status'])
        return color

    def _ev_desc(self: NodePane, event: dict) -> str:
        """Render the desc cell: step name + colored verb + metadata.

        The cell truncates to the desc width, so the metadata tail is what
        loses chars to the ellipsis.
        """
        verb = f'[{self._ev_color(event)}]{self._ev_verb(event)}[/]'
        if event['sync']:
            verb = f'[{fractal.tui.theme.DIM}]sync[/] {verb}'
        elif event['kind'] == 'step' and event['name']:
            name = fractal.tui.fmt.esc(event['name'])
            verb = f'{name} {verb}'
        meta = event.get('metadata') or ''
        # entity-start rows only (NULL event_id) -- a start *event* keeps its
        # metadata (the continue marker)
        if event['event'] == 'start' and event['event_id'] is None:
            meta = ''
        if meta:
            return f'{verb} [{fractal.tui.theme.DIM}]{fractal.tui.fmt.esc(meta)}[/]'
        return verb

    def _ev_line(
        self: NodePane,
        snap: Snapshot,
        event: dict,
        *,
        expanded: bool = False,
    ) -> Union[Text, Table]:
        """Render one activity row; ``expanded`` unfolds it to full text."""
        duration = fractal.tui.fmt.dur(event['duration']) if event['duration'] else ''
        cost = fractal.tui.fmt.money(event['cost']) if event['cost'] is not None else ''
        # time / node / run / iter / step / desc / dur / cost -- every column
        # sized to its longest value (the geometry), two columns of air
        # between; lineage cells carry bare ordinals (the header row names
        # the columns), absent lineage renders empty, and a sync pass leaves
        # the step cell empty
        clock = fractal.tui.fmt.clock(event['created_at'], self.app.tz)
        run = str(event['run_n']) if event['run_n'] else ''
        it = str(event['iter_n']) if event['iter_n'] else ''
        step = str(event['step_n']) if event['step_n'] else ''
        tag = user_tag(event['branch'], self.app.data.root_branch)
        node_name = leaf_of(event['branch']) + tag
        g = snap.geometry
        if expanded:
            # the selected row unfolds: the lineage columns drop out (the
            # full branch takes their span) and the full metadata folds
            span = (
                g.ev_node_w
                + g.ev_run_w
                + g.ev_iter_w
                + g.ev_step_w
                + 3 * fractal.tui.theme.GAP
            )
            desc_w = g.desc_w
            # fixed columns (no expand): the grid must end where the folded
            # rows do, not stretch into the pane's breathing column
            grid = Table.grid()
            grid.add_column(width=fractal.tui.theme.TIME_W)
            grid.add_column(width=fractal.tui.theme.GAP)
            grid.add_column(width=span, overflow='fold')
            grid.add_column(width=fractal.tui.theme.GAP)
            grid.add_column(width=desc_w, overflow='fold')
            grid.add_column(width=fractal.tui.theme.GAP)
            grid.add_column(justify='right', width=g.ev_dur_w)
            grid.add_column(width=fractal.tui.theme.GAP)
            grid.add_column(justify='right', width=g.ev_cost_w)
            branch_name = event['branch'] + tag
            grid.add_row(
                Text.from_markup(f'[{fractal.tui.theme.DIM}]{clock}[/]'),
                Text(),
                Text.from_markup(f'[{fractal.tui.theme.INK}]{branch_name}[/]'),
                Text(),
                _fill(Text.from_markup(self._ev_desc(event)), desc_w),
                Text(),
                Text.from_markup(f'[{fractal.tui.theme.DIM}]{duration}[/]'),
                Text(),
                Text.from_markup(f'[{fractal.tui.theme.DIM}]{cost}[/]'),
            )
            return grid
        return fractal.tui.fmt.row(
            fractal.tui.fmt.cell(
                markup=f'[{fractal.tui.theme.DIM}]{clock}[/]',
                width=fractal.tui.theme.TIME_W,
            ),
            fractal.tui.fmt.cell(
                markup=f'[{fractal.tui.theme.INK}]{node_name}[/]',
                width=g.ev_node_w,
            ),
            fractal.tui.fmt.cell(f'[{fractal.tui.theme.DIM}]{run}[/]', g.ev_run_w),
            fractal.tui.fmt.cell(f'[{fractal.tui.theme.DIM}]{it}[/]', g.ev_iter_w),
            fractal.tui.fmt.cell(f'[{fractal.tui.theme.DIM}]{step}[/]', g.ev_step_w),
            fractal.tui.fmt.cell(self._ev_desc(event), g.desc_w),
            fractal.tui.fmt.cell(
                markup=f'[{fractal.tui.theme.DIM}]{duration}[/]',
                width=g.ev_dur_w,
                justify='right',
            ),
            fractal.tui.fmt.cell(
                markup=f'[{fractal.tui.theme.DIM}]{cost}[/]',
                width=g.ev_cost_w,
                justify='right',
            ),
        )

    def set_log_hover(self: NodePane, hover: bool) -> None:
        """Track the log-scope toggle's mouse hover state."""
        self.log_hover = hover
        self.paint_log_scope()

    def log_scope(self: NodePane, branch: str) -> bool:
        """Return a branch's log scope (toggled choice, else the default rule).

        The user (root) node defaults to the descendants view, every other
        node to its own activity.
        """
        default = branch == self.app.data.root_branch
        return self.sub_log_by_branch.get(branch, default)

    def toggle_log_scope(self: NodePane) -> None:
        """Flip the activity log between the node and its descendants."""
        self.sub_log = not self.sub_log
        self.sub_log_by_branch[self.app.scope] = self.sub_log
        self.ev_sel = 0
        self.app.refresh_log()

    def paint_log_scope(self: NodePane) -> None:
        """Repaint the log-scope toggle (lit on hover or as the active zone)."""
        lit = self.log_hover or (self.app.mode == 'node' and self.zone == 'scope')
        active = 'descendants' if self.sub_log else 'node'
        markup = fractal.tui.fmt.toggle(('node', 'descendants'), active, lit)
        self.app.query_one('#nodelogscope', LogScopeToggle).update(markup)

    def _ev_rebuild(self: NodePane, snap: Snapshot) -> None:
        """Re-mount the activity rows.

        A centered date row closes each day's group (newest first, so
        everything above a date row happened on that date).
        """
        self.paint_log_scope()
        self.app.query_one('#nodeevcols', Static).update(self._ev_cols(snap))
        widgets: list[Static] = []
        for index, event in enumerate(snap.log):
            widgets.append(Static(self._ev_line(snap, event), classes='evrow'))
            stamp = fractal.tui.fmt.timestamp(event['created_at'], self.app.tz)
            date = stamp.split(' ')[0]
            following = snap.log[index + 1] if index + 1 < len(snap.log) else None
            next_date = None
            if following:
                stamp = fractal.tui.fmt.timestamp(following['created_at'], self.app.tz)
                next_date = stamp.split(' ')[0]
            if date != next_date:
                # a centered date flanked by rules, spanning the row
                inner = snap.geometry.node_width - fractal.tui.theme.NODE_CHROME
                side = max(0, inner - len(date) - 2)
                left = side // 2
                marker = (
                    f'[{fractal.tui.theme.RULE}]{fractal.tui.theme.LINE * left}[/]'
                    f'[{fractal.tui.theme.CHROME}] {date} [/]'
                    f'[{fractal.tui.theme.RULE}]'
                    f'{fractal.tui.theme.LINE * (side - left)}[/]'
                )
                widgets.append(Static(marker, classes='evdate'))
        box = self.app.query_one('#nodeevents', PaneScroll)
        box.remount(*widgets)
        self.ev_sel = min(self.ev_sel, max(0, len(snap.log) - 1))
        self.app.call_after_refresh(self._ev_paint)

    def _ev_paint(self: NodePane) -> None:
        """Paint the activity-log row highlight."""
        snap = self.app.snapshot
        for index, widget in enumerate(self.app.query('#nodeevents .evrow')):
            selected = (
                self.app.mode == 'node' and self.zone == 'rows' and index == self.ev_sel
            )
            # the selected row unfolds to its full text; restore on the way out
            if widget.has_class('expanded') != selected and index < len(snap.log):
                widget.update(self._ev_line(snap, snap.log[index], expanded=selected))
            widget.set_class(selected, 'expanded')
            widget.set_class(selected, 'rsel')
            if selected:
                # the unfold changes the row's height: scroll after layout
                # settles, or the view jumps while the highlight sits still
                self.app.call_after_refresh(widget.scroll_visible, animate=False)

    def click_row(self: NodePane, row: Widget) -> bool:
        """Move the zone cursor to the clicked row; a re-click activates.

        The first click lands the cursor, like arrowing there; a click on
        the already-highlighted row acts like ``enter`` (fold/fork an explorer row,
        reveal a log row's entity above).

        Returns:
            Whether ``row`` is a selectable explorer or event row.

        """
        exrows = list(self.app.query('#nodeexplore .exrow'))
        evrows = list(self.app.query('#nodeevents .evrow'))
        snap = self.app.snapshot
        if row in exrows:
            index = exrows.index(row)
            if self.app.mode == 'node' and self.zone == 'mid' and self.ex_sel == index:
                self._ex_activate(snap)
                return True
            self.zone = 'mid'
            self.ex_sel = index
        elif row in evrows:
            index = evrows.index(row)
            if self.app.mode == 'node' and self.zone == 'rows' and self.ev_sel == index:
                self._open_event(snap)
                return True
            self.zone = 'rows'
            self.ev_sel = index
        else:
            return False
        self.app.mode = 'node'
        self.paint_zone()
        return True

    def enter(self: NodePane) -> None:
        """Enter the pane: land on the runs tree."""
        self.app.mode = 'node'
        self.zone = 'mid'
        self.ex_sel = 0
        self._ex_rebuild(self.app.snapshot)
        self.paint_zone()

    def leave(self: NodePane) -> None:
        """Return to ring mode."""
        self.app.mode = 'ring'
        # repay any row rebuild a landing skipped while this mode drove
        self.app.refresh_stale()
        self.paint_zone()
        self.app.paint_ring()

    def paint_zone(self: NodePane) -> None:
        """Paint the card tint, the zone-aware foot, and the row highlights."""
        top = self.app.mode == 'node' and self.zone == 'top'
        self.app.query_one('#nodecard').set_class(top, 'zonefocus')
        self.app.query_one('#nodefoot', Static).update(self._foot())
        self.paint_log_scope()
        self._ex_paint()
        self._ev_paint()

    def _goto_scope(self: NodePane) -> None:
        """Land on the log-scope toggle between the runs and the log."""
        self.zone = 'scope'
        self.paint_zone()

    def _goto_rows(self: NodePane) -> None:
        """Leave the runs tree for the event log's row cursor."""
        self.zone = 'rows'
        self.ev_sel = 0
        self.paint_zone()

    def _goto_mid(self: NodePane, *, at_end: bool = False) -> None:
        """Return to the runs tree (``at_end`` lands on the last row)."""
        self.zone = 'mid'
        rows = self._ex_rows(self.app.snapshot)
        if rows:
            self.ex_sel = (len(rows) - 1) if at_end else min(self.ex_sel, len(rows) - 1)
        self.paint_zone()

    def key(self: NodePane, event: Key) -> None:
        """Handle node mode: card chat, explorer selection, log scroll/rows."""
        key = event.key
        snap = self.app.snapshot
        # top zone: the card; enter opens a chat against its session
        if self.zone == 'top':
            if key in ('escape', 'up'):
                self.leave()
            elif key == 'down':
                self.zone = 'mid'
                self.paint_zone()
            elif key == 'enter':
                card, _ = self._context_view(snap)
                self.app.fork_session({'session': (card or {}).get('session')})
                # the flow moved to the compose pane: drop the card highlight
                self.paint_zone()
            else:
                return
            event.stop()
            return
        # scope zone: the node/descendants toggle over the activity log
        if self.zone == 'scope':
            if key == 'escape':
                self.leave()
            elif key == 'up':
                self._goto_mid(at_end=True)
            elif key == 'down':
                if snap.log:
                    self._goto_rows()
            elif key in ('left', 'right', 'enter', 't'):
                self.toggle_log_scope()
            else:
                return
            event.stop()
            return
        # log rows: a selection cursor over the activity timeline (the view
        # scrolls only when the cursor crosses the viewport edge); enter jumps the
        # explorer (and the card) to the row's run/iter/step
        if self.zone == 'rows':
            if key == 'escape':
                self.leave()
            elif key == 'up':
                if self.ev_sel <= 0:
                    self._goto_scope()
                else:
                    self.ev_sel -= 1
                    self._ev_paint()
            elif key == 'down':
                self.ev_sel = min(len(snap.log) - 1, self.ev_sel + 1)
                self._ev_paint()
            elif key == 'enter':
                self._open_event(snap)
            elif key == 't':
                # toggle the subtree log (descendants merged into the timeline)
                self.toggle_log_scope()
            else:
                return
            event.stop()
            return
        # middle zone: the runs tree
        rows = self._ex_rows(snap)
        if not rows:
            if key == 'escape':
                self.leave()
            elif key == 'up':
                self.zone = 'top'
                self.paint_zone()
            elif key == 'down':
                self._goto_scope()
            else:
                return
            event.stop()
            return
        ref = rows[min(self.ex_sel, len(rows) - 1)]
        if key == 'escape':
            self.leave()
        elif key == 'up':
            if self.ex_sel <= 0:
                self.zone = 'top'
                self.paint_zone()
            else:
                self.ex_sel -= 1
                self._ex_paint()
        elif key == 'down':
            if self.ex_sel >= len(rows) - 1:
                self._goto_scope()  # past the last row -> the log-scope toggle
            else:
                self.ex_sel += 1
                self._ex_paint()
        elif key == 'right' and len(ref) < 3:
            self.ex_expanded.add(ref)
            self._ex_rebuild(snap)
        elif key == 'left' and ref in self.ex_expanded:
            self.ex_expanded.discard(ref)
            self._ex_rebuild(snap)
        elif key == 'enter':
            self._ex_activate(snap)
        else:
            return
        event.stop()

    def _ex_activate(self: NodePane, snap: Snapshot) -> None:
        """Activate the selected explorer row: fold a run/iter, fork a step."""
        rows = self._ex_rows(snap)
        if not rows:
            return
        ref = rows[min(self.ex_sel, len(rows) - 1)]
        if len(ref) < 3:
            self.ex_expanded.symmetric_difference_update({ref})
            self._ex_rebuild(snap)
        else:
            # enter on a step: fork-chat its session in the compose pane
            self.app.fork_session(self._ex_entry(snap, ref))
            self._ex_paint()

    def _open_event(self: NodePane, snap: Snapshot) -> None:
        """Jump the explorer (and the card) to the event's entity.

        Expands its lineage, selects it, and lands back in the runs zone.
        """
        if not snap.log:
            return
        row = snap.log[min(self.ev_sel, len(snap.log) - 1)]
        ref = _event_ref(snap.history, row)
        if ref is None:
            return
        for depth in range(1, len(ref)):
            self.ex_expanded.add(ref[:depth])
        self.zone = 'mid'
        self._ex_rebuild(snap)
        rows = self._ex_rows(snap)
        if ref in rows:
            self.ex_sel = rows.index(ref)
        self.paint_zone()


# ------ helper functions


def _figure(value: Any, cap: Any, fmt_fn: Any) -> tuple[str, str]:
    """Build a measures figure: its markup and its plain form (for sizing)."""
    ceiling = fmt_fn(cap) if cap else '-'
    markup = f'{fmt_fn(value)}[{fractal.tui.theme.DIM}]/{ceiling}[/]'
    plain = f'{fmt_fn(value)}/{ceiling}'
    return markup, plain


def _fill(text: Text, width: int) -> Text:
    """Wrap by character fill: every line packs to the cell's full width.

    Rich's ``fold`` moves a token that misses the remaining space onto its
    own line; the unfolded log row reads as a continuous stream instead -- a
    sha or branch starts beside its verb and breaks at the cell edge.
    """
    plain = text.plain
    offsets = list(range(width, len(plain), width))
    if width <= 0 or not offsets:
        return text
    return Text('\n').join(text.divide(offsets))


def _event_ref(history: tuple[dict, ...], event: dict) -> Optional[tuple]:
    """Resolve an activity row to its explorer ref (its deepest known entity)."""
    for run_index, run in enumerate(history):
        if run['run_id'] != event['run_id']:
            continue
        if event['iter_id'] is None:
            return (run_index,)
        for iter_index, it in enumerate(run['iters']):
            if it['iter_id'] != event['iter_id']:
                continue
            if event['step_id'] is not None:
                for step_index, step in enumerate(it['steps']):
                    if step['step_id'] == event['step_id']:
                        return (run_index, iter_index, step_index)
            return (run_index, iter_index)
        return (run_index,)
    return None


def _json_val(value: Any) -> str:
    """Render a config value as JSON (quoted string / true|false / number)."""
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, float):
        # float-arithmetic artifacts (0.7000000000000001) read as their short form
        return repr(round(value, 10))
    return str(value)
