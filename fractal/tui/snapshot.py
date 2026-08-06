"""Implements the cockpit's per-tick view-model.

``SnapshotBuilder`` turns the poller's change reports into an immutable
``Snapshot`` -- the one object every pane renders from. The hard rule this
module exists for: **renderers never touch the database layer.** Each build
re-reads only the sections whose branch changed on disk (per-branch caches keyed
by the poller's tokens), shapes them once into the pane row contracts, and
returns the previous ``Snapshot`` object untouched when nothing changed -- so a
steady tick costs zero queries and panes can skip rebuilds by comparison.
"""

from __future__ import annotations

import contextlib
import dataclasses
import math
import pathlib
import sqlite3
import time
from collections.abc import Callable, Iterator
from typing import Any, Optional

from rich.text import Text

import fractal.core.cost
import fractal.util

from . import fmt, theme
from .data import TuiData, display_name_of, leaf_of, parse_ts, span, user_tag
from .poller import NodePoller

__all__ = [
    'Geometry',
    'Snapshot',
    'SnapshotBuilder',
]

# the built-in sync pass records under this name, but a user step file can
# carry the same name, so SYNC-named rows classify structurally (_sync_ids):
# passes are excluded from the displayed step numerator/denominator and
# render as plain `sync` in the explorer and event log, while a settled row
# alone on its number reads as the numbered step it is
_SYNC_NAME = 'SYNC'

# the widest node-event verb floors the event-log desc column (see Geometry)
_EV_LEAD_MAX = max(len(verb) for verb in fmt.NODE_VERB.values())


@dataclasses.dataclass(frozen=True)
class Geometry:
    """Pane dimensions derived once per focused-section refresh."""

    tree_width: int  # tree pane total width
    node_width: int  # node pane total width
    label_w: int  # explorer label cell width
    desc_w: int  # event-log desc cell width
    ev_node_w: int  # event-log node column (fits the longest leaf name)
    ev_run_w: int  # event-log run column (fits the longest value)
    ev_iter_w: int  # event-log iter column
    ev_step_w: int  # event-log step column
    ev_dur_w: int  # duration column (shared by the log and the explorer)
    ev_cost_w: int  # cost column (shared by the log and the explorer)

    @classmethod
    def empty(cls: type[Geometry]) -> Geometry:
        """Return a zero-width geometry (the worktree-less placeholder shape)."""
        return cls(
            tree_width=0,
            node_width=0,
            label_w=0,
            desc_w=0,
            ev_node_w=0,
            ev_run_w=0,
            ev_iter_w=0,
            ev_step_w=0,
            ev_dur_w=0,
            ev_cost_w=0,
        )


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """The immutable view-model one tick renders from."""

    repo: str  # repository name (header brand)
    scope: str  # focused branch
    tree: tuple[dict, ...]  # DFS tree rows (whole tree)
    counts: tuple[int, int]  # (total, active) for the tree foot
    card: Optional[dict]  # node-card head/ident state
    measures: Optional[dict]  # the 3x2 matrix values + caps
    config: dict  # scope's config.json (chips + tooltip)
    history: tuple[dict, ...]  # run -> iters -> steps (explorer)
    log: tuple[dict, ...]  # activity rows, newest first
    sessions: tuple[str, ...]  # distinct woven sessions, newest first
    channels: tuple[dict, ...]  # scope's channels (+ read/write flags)
    messages: tuple[dict, ...]  # scope's own top-level messages
    feed: tuple[dict, ...]  # subtree public+outbox fan-out (lazy)
    saved: tuple[dict, ...]  # scope's archive (lazy)
    geometry: Geometry

    @property
    def ticking(self: Snapshot) -> bool:
        """Return whether the card shows an open row measuring the live clock.

        A quiet tree reuses this object tick after tick; while this holds
        the app still repaints the card, so the open rows' elapsed cells
        and cap gauges keep ticking between disk writes.
        """
        measures = self.measures
        return measures is not None and (
            measures['started_step'] is not None
            or measures['started_iter'] is not None
            or measures['started_run'] is not None
        )


class SnapshotBuilder:
    """Snapshot builder over per-branch section caches keyed by poll tokens."""

    def __init__(
        self: SnapshotBuilder,
        data: TuiData,
        poller: NodePoller,
        *,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        """Initialize ``SnapshotBuilder``.

        Args:
            data: The read-only database surface.
            poller: The change-detection signal consulted on every build.
            now: Epoch-seconds clock for live-elapsed math; ``time.time``
                when omitted (injectable for deterministic tests).

        """
        self._data = data
        self._poller = poller
        self._now = now or time.time
        # per-branch section caches (dropped when the branch's token moves)
        self._topo: Optional[list[str]] = None
        self._children: dict[str, list[str]] = {}
        self._titles: dict[str, str] = {}
        self._brief: dict[str, dict] = {}
        self._shaped: dict[str, dict] = {}
        self._radio: dict[str, tuple] = {}
        self._archive: dict[str, tuple] = {}
        self._runcost: dict[str, dict] = {}
        self._runsteps: dict[str, dict] = {}
        self._logs: dict[str, tuple] = {}
        self._tree: Optional[tuple] = None
        self._tree_scope: Optional[str] = None
        self._stale: set[str] = set()
        self._failed: set[str] = set()
        self._feed_scope: Optional[str] = None
        self._sublog_scope: Optional[str] = None
        self._snapshot: Optional[Snapshot] = None
        # live tmux sessions, refreshed once per build; headless liveness uses
        # each node's process-group record (never persisted -- read-only)
        self._live_sessions: frozenset[str] = frozenset()

    def build(
        self: SnapshotBuilder,
        scope: str,
        *,
        want_feed: bool = False,
        want_archive: bool = False,
        want_subtree_log: bool = False,
    ) -> Snapshot:
        """Return the current snapshot, re-reading only what changed on disk.

        Args:
            scope: The focused branch.
            want_feed: Populate the cross-subtree feed section (lazy: the
                radio pane requests it only while showing Feed).
            want_archive: Populate the archive section (Archive view).
            want_subtree_log: Merge every descendant's activity into the log
                section (lazy: the node pane requests it while toggled on).

        Returns:
            The snapshot -- the **same object** as the previous build when
            nothing changed, so callers can skip rendering entirely.

        """
        # detect change; failed reads from the last build retry as moved
        moved = set(self._poller.changed(self._watched()))
        moved |= self._failed
        self._failed.clear()
        # refresh topology when the root registry changed (or on first build);
        # newly registered branches count as moved so their sections build, and
        # a second poll primes their tokens so the next build sees no movement
        if self._topo is None or self._data.root_branch in moved:
            moved |= self._refresh_topo()
            moved |= self._poller.changed(self._watched())
        for branch in moved:
            self._drop(branch)
        # reconcile crashed-but-active nodes for display only: a loop that died
        # leaves .status 'active' with no live runtime; fetch the live sessions
        # once and drop any brief whose session is gone so it rebuilds as the
        # honest 'exited'; a crash doesn't move the .status mtime, so the poller
        # can't catch it -- liveness is checked here EVERY build, ahead of the
        # short-circuit below, or a node that crashed since the last build would
        # stay 'active' in the reused snapshot forever
        self._live_sessions = self._data.live_sessions()
        reconciled = False
        for branch in self._topo or ():
            brief = self._brief.get(branch)
            if brief is not None and brief['status'] == 'active':
                if not self._data.loop_alive(branch, self._live_sessions):
                    self._drop(branch)
                    reconciled = True
        # short-circuit: nothing moved (a crash reconcile counts as movement)
        # and the previous snapshot still answers
        previous = self._snapshot
        root = self._data.root_branch
        if previous is not None:
            unchanged = not moved and not reconciled and previous.scope == scope
            feed_ready = not want_feed or self._feed_scope == scope
            archive_ready = not want_archive or root in self._archive
            sublog_ready = self._sublog_scope == (scope if want_subtree_log else None)
            if unchanged and feed_ready and archive_ready and sublog_ready:
                return previous
        # ensure the sections this snapshot needs
        for branch in self._topo or ():
            self._ensure_brief(branch)
        self._ensure_focus(scope)
        subtree = self._subtree(scope)
        if want_feed:
            for branch in subtree:
                self._ensure_radio(branch)
            self._feed_scope = scope
        if want_archive:
            # saved messages are the cockpit user's: always the root's archive
            self._ensure_archive(root)
        # assemble (cached sections keep identity across snapshots)
        shaped = self._shaped.get(scope) or _EMPTY_SHAPE
        # the subtree log merges every descendant's shaped rows; its geometry
        # re-derives so the node column fits the longest descendant leaf
        if want_subtree_log:
            log = self._subtree_log(subtree)
            geometry = self._geometry(shaped['card'], shaped['history'], scope, log)
        else:
            log = shaped['log']
            geometry = shaped['geometry']
        self._sublog_scope = scope if want_subtree_log else None
        tree = self._tree_rows(scope)
        total, active = self._counts()
        feed = self._feed(subtree) if want_feed else ()
        saved = self._archive.get(root, ()) if want_archive else ()
        snapshot = Snapshot(
            repo=self._data.repo_dir.name,
            scope=scope,
            tree=tree,
            counts=(total, active),
            card=shaped['card'],
            measures=shaped['measures'],
            config=shaped['config'],
            history=shaped['history'],
            log=log,
            sessions=shaped['sessions'],
            channels=shaped['channels'],
            messages=self._radio.get(scope, ()),
            feed=feed,
            saved=saved,
            geometry=geometry,
        )
        self._snapshot = snapshot
        return snapshot

    def _watched(self: SnapshotBuilder) -> dict[str, pathlib.Path]:
        """Build the branch -> node-dir map the poller stats.

        Before the first topology refresh only the root is known (its token
        triggers it).
        """
        branches = self._topo or [self._data.root_branch]
        result = {}
        for branch in branches:
            node_dir = self._data.node_dir(branch)
            if node_dir is not None:
                result[branch] = node_dir
        return result

    def _refresh_topo(self: SnapshotBuilder) -> set[str]:
        """Re-read the worktree map + root registry (creation order).

        Hides branches whose worktree is gone; returns newly seen branches.
        """
        self._data.refresh_worktrees()
        try:
            registry = self._data.registry_branches()
            titles = self._data.registry_titles()
        except (FileNotFoundError, sqlite3.Error):
            self._failed.add(self._data.root_branch)
            return set()
        self._titles = titles
        live = [self._data.root_branch]
        for branch in registry:
            if branch == self._data.root_branch:
                continue
            if self._data.node_dir(branch) is not None:
                live.append(branch)
        previous = set(self._topo or ())
        for branch in previous - set(live):
            self._data.evict(branch)
            self._drop(branch)
        self._topo = live
        # index direct children in creation order (one dotted segment deeper)
        self._children = {branch: [] for branch in live}
        for branch in live:
            parent, _, _ = branch.rpartition('.')
            if parent in self._children:
                self._children[parent].append(branch)
        self._tree = None
        return set(live) - previous

    def _drop(self: SnapshotBuilder, branch: str) -> None:
        """Invalidate every cached section the branch feeds.

        Its own sections, the tree, and any focused shape whose subtree
        contains it (the run-cost chase); the focused shape is marked stale
        rather than popped so a contended read (busy timeout) keeps showing
        last-known data until the retry.
        """
        self._brief.pop(branch, None)
        self._radio.pop(branch, None)
        self._archive.pop(branch, None)
        self._runcost.pop(branch, None)
        self._runsteps.pop(branch, None)
        self._logs.pop(branch, None)
        self._tree = None
        for scope in self._shaped:
            if branch == scope or branch.startswith(f'{scope}.'):
                self._stale.add(scope)

    def _subtree(self: SnapshotBuilder, scope: str) -> list[str]:
        """List the scope and every descendant branch (topology order)."""
        prefix = f'{scope}.'
        return [
            branch
            for branch in self._topo or ()
            if branch == scope or branch.startswith(prefix)
        ]

    @contextlib.contextmanager
    def _section(
        self: SnapshotBuilder,
        branch: str,
        *,
        placeholder: Optional[Callable[[], None]] = None,
    ) -> Iterator[sqlite3.Connection]:
        """Yield a read connection for one section load, absorbing lost reads.

        A lost read (busy timeout, missing database) marks the branch for
        retry, runs the loader's failure ``placeholder``, and suppresses the
        error. Only the connect/close/retry ceremony is centralized here --
        failure behavior is per-loader: it is NOT uniform, and it is not
        "leave last-good state untouched" (most loaders cache an empty
        section on loss so the ensure-guards do not re-run within the build).
        """
        # a failed connect yields a closed stand-in: the loader's first read
        # then raises into the one suppression path below (a contextmanager
        # cannot skip its body)
        try:
            connection = self._data.connect()
        except (FileNotFoundError, sqlite3.Error):
            connection = sqlite3.connect(':memory:')
            connection.close()
        try:
            yield connection
        except (FileNotFoundError, sqlite3.Error):
            self._failed.add(branch)
            if placeholder is not None:
                placeholder()
        finally:
            connection.close()

    def _ensure_brief(self: SnapshotBuilder, branch: str) -> None:
        """Cache the branch's status + pending signal.

        Status comes from the ``.status`` file, display-reconciled to ``exited``
        when it reads ``active`` but the node's loop runtime is gone (a crashed
        loop); the signal query runs only for an active node (a settled tree
        polls with zero queries).
        """
        if branch in self._brief:
            return
        status = self._data.status(branch)
        # display-reconcile a crashed-but-active node: active with no live loop
        # runtime means it died without ending; show the honest 'exited'
        # (display only -- never persisted; the cockpit is read-only)
        if status == 'active':
            if not self._data.loop_alive(branch, self._live_sessions):
                status = 'exited'
        signal = ''
        if status == 'active':
            # a lost read leaves the signal '' until the retry lands; the
            # brief row below is still written (unconditional cache write)
            with self._section(branch) as connection:
                signal = self._data.signal(connection, branch)
        self._brief[branch] = {'status': status, 'signal': signal}

    def _ensure_runcost(self: SnapshotBuilder, branch: str) -> None:
        """Cache the branch's run-cost and run-step maps (the spend chase)."""
        if branch in self._runcost:
            return

        def placeholder() -> None:
            self._runcost[branch] = {}
            self._runsteps[branch] = {}

        with self._section(branch, placeholder=placeholder) as connection:
            self._runcost[branch] = self._data.run_costs(connection, branch)
            self._runsteps[branch] = self._data.run_steps(connection, branch)

    def _ensure_radio(self: SnapshotBuilder, branch: str) -> None:
        """Cache the branch's radio rows (with react counts, newest first)."""
        if branch in self._radio:
            return

        def placeholder() -> None:
            self._radio[branch] = ()

        with self._section(branch, placeholder=placeholder) as connection:
            messages = self._data.message_rows(connection, branch)
            reacts = self._data.react_counts(connection, branch)
            self._radio[branch] = _shape_radio(messages, branch, reacts)

    def _ensure_archive(self: SnapshotBuilder, branch: str) -> None:
        """Cache the branch's archived messages (newest first)."""
        if branch in self._archive:
            return

        def placeholder() -> None:
            self._archive[branch] = ()

        with self._section(branch, placeholder=placeholder) as connection:
            rows = self._data.archive_rows(connection, branch)
            # archived copies are read by definition and carry no react counts;
            # each row keeps its source node (the `owner` column) for actions
            shaped = [_message(row, row['owner'], {}) for row in rows]
            shaped.sort(key=lambda row: row['created_at'], reverse=True)
            self._archive[branch] = tuple(shaped)

    def _ensure_focus(self: SnapshotBuilder, scope: str) -> None:
        """Build everything the scoped panes render.

        The run/iter/step tables shaped into card + measures + history +
        sessions, the event log, the scope's radio section, channels, config,
        and the geometry -- one connection for the scope plus one per
        uncached subtree branch (the run-cost chase).
        """
        if scope in self._shaped and scope not in self._stale:
            return
        self._ensure_brief(scope)
        brief = self._brief[scope]
        if self._data.node_dir(scope) is None:
            self._stale.discard(scope)
            self._shaped[scope] = dict(
                _EMPTY_SHAPE,
                geometry=self._geometry(None, (), scope, ()),
            )
            return
        config = self._data.config(scope)
        try:
            connection = self._data.connect()
            try:
                runs, iters, steps = self._data.tables(connection, scope)
                log_raw = self._data.log_rows(connection, (scope,))
                runcost = self._data.run_costs(connection, scope)
                runsteps = self._data.run_steps(connection, scope)
                messages = self._data.message_rows(connection, scope)
                reacts = self._data.react_counts(connection, scope)
                channels = self._data.channel_rows(connection, scope)
            finally:
                connection.close()
        except (FileNotFoundError, sqlite3.Error):
            # keep showing the stale shape (if any) until the retry lands --
            # the one stale-but-shown loader, so its except path stays
            # explicit here instead of riding a _section placeholder
            self._failed.add(scope)
            if scope not in self._shaped:
                self._shaped[scope] = dict(
                    _EMPTY_SHAPE,
                    geometry=self._geometry(None, (), scope, ()),
                )
            return
        self._runcost[scope] = runcost
        self._runsteps[scope] = runsteps
        self._radio[scope] = _shape_radio(messages, scope, reacts)
        # the run-cost chase spans the subtree; ensure each branch's map
        for branch in self._subtree(scope):
            self._ensure_runcost(branch)
        self._stale.discard(scope)
        run, it, step = _context(runs, iters, steps, config)
        # the card's session is the currently running one -- the open
        # iteration's newest stamped step -- never a prior step's leftover
        if it is not None:
            session = _iter_session(
                it=it,
                it_steps=[s for s in steps if s['iter_id'] == it['iter_id']],
            )
        else:
            session = None
        card = _card(scope, brief, step, config, session)
        # one guarded connection covers the whole spend pass: the card's
        # run-cost figure and every history run row's
        spends = self._run_spends(scope, [run['run_id'] for run in runs])
        measures = self._measures(
            runs=runs,
            steps=steps,
            config=config,
            run=run,
            it=it,
            step=step,
            spends=spends,
        )
        history = _history(runs, iters, steps, runcost, config)
        log = _log(log_raw, [run['run_id'] for run in runs], scope, config)
        self._logs[scope] = log
        # each row carries the run's subtree spend as of its own end -- the
        # card's run-cost figure when the explorer highlights it (open rows
        # read all-time: cost only accrues on step ends, which re-shape)
        for run_row in history:
            run_row['spend'] = spends[run_row['run_id']]
            for iter_row in run_row['iters']:
                iter_row['run_spend'] = self._run_spend_at(
                    branch=scope,
                    run_id=run_row['run_id'],
                    t=_end_epoch(iter_row),
                )
                for step_row in iter_row['steps']:
                    step_row['run_spend'] = self._run_spend_at(
                        branch=scope,
                        run_id=run_row['run_id'],
                        t=_end_epoch(step_row),
                    )
        geometry = self._geometry(card, history, scope, log)
        self._shaped[scope] = {
            'card': card,
            'measures': measures,
            'config': config,
            'history': history,
            'log': log,
            'sessions': _sessions(steps),
            'channels': tuple(channels),
            'geometry': geometry,
        }

    def _measures(
        self: SnapshotBuilder,
        *,
        runs: list[dict],
        steps: list[dict],
        config: dict,
        run: Optional[dict],
        it: Optional[dict],
        step: Optional[dict],
        spends: dict[int, float],
    ) -> Optional[dict]:
        """Shape the run/iter/step x time/cost matrix with every configured cap.

        All six cap axes exist (``timeout`` is the per-RUN wall clock,
        ``iter_timeout`` the per-iteration one); ``None`` means uncapped and
        renders ``-``.
        """
        if run is None:
            return None
        it_steps = [s for s in steps if it and s['iter_id'] == it['iter_id']]
        # max_iters is agent-editable like the caps below -- coerce, so a
        # mistyped value reads as no cap (the int sibling of _cost_or_none);
        # OverflowError covers a JSON Infinity, which int() rejects with
        # neither TypeError nor ValueError
        try:
            iter_max = int(config.get('max_iters'))  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            iter_max = None
        if iter_max is not None and iter_max < 0:
            iter_max = None
        return {
            'run': len(runs),
            'iter': it['iter'] if it else None,
            'iter_max': iter_max,
            'step': step['step'] if step else None,
            'step_total': _step_total(steps, run, _sync_ids(steps, config)),
            'step_name': step['step_name'] if step else None,
            'elapsed_step': self._elapsed(step) if step else None,
            'elapsed_iter': self._elapsed(it) if it else None,
            'elapsed_run': self._elapsed(run),
            # open rows also carry their start instant: a quiet tree reuses
            # this snapshot tick after tick, so the pane re-ticks their
            # elapsed against the render clock instead of the baked value
            'started_step': _open_epoch(step),
            'started_iter': _open_epoch(it),
            'started_run': _open_epoch(run),
            'cost_step': step['cost'] if step else None,
            'cost_iter': sum(s['cost'] or 0 for s in it_steps) if it else None,
            'cost_run': spends[run['run_id']],
            # config.json is agent-editable, so a duration may arrive as a
            # bare number (e.g. timeout: 3600) -- coerce to str before parsing
            # (matching Config.validate) so a mistyped value reads as no cap,
            # never an AttributeError that crashes the whole cockpit build
            'cap_step_s': fractal.util.parse_duration_seconds(
                str(config.get('step_timeout') or '')
            ),
            'cap_step_cost': _cost_or_none(config.get('max_step_cost')),
            'cap_iter_s': fractal.util.parse_duration_seconds(
                str(config.get('iter_timeout') or '')
            ),
            'cap_iter_cost': _cost_or_none(config.get('max_iter_cost')),
            'cap_run_s': fractal.util.parse_duration_seconds(
                str(config.get('timeout') or '')
            ),
            'cap_run_cost': _cost_or_none(config.get('max_cost')),
            'reserve_budget': _cost_or_none(config.get('reserve_budget')),
        }

    def _elapsed(
        self: SnapshotBuilder,
        row: Optional[dict],
    ) -> Optional[float]:
        """Compute a row's elapsed seconds.

        Open rows (no end instant yet) measure against the injected clock --
        an open iteration or run keeps ticking through SYNC steps and
        between-step windows; settled rows report their final wall time
        (``ended_at`` - ``started_at``).
        """
        if row is None:
            return None
        if row['ended_at'] is None:
            started = parse_ts(row['started_at'])
            if started is None:
                return None
            return max(0.0, self._now() - started.timestamp())
        return span(row['started_at'], row['ended_at'])

    def _run_spends(
        self: SnapshotBuilder,
        scope: str,
        run_ids: list[int],
    ) -> dict[int, float]:
        """Compute each run's all-time subtree spend via the DB walk.

        Walks the persisted ``parent_run_id`` lineage (like
        :meth:`fractal.core.cost.Cost.spent`, the figure budget enforcement
        reads), so a deleted or orphaned descendant's spend still counts and
        the card's total matches enforcement -- one connection for the whole
        pass. The per-instant history math (:meth:`_run_spend_at`) keeps the
        live-topo substrate. A lost read marks the scope for retry and
        degrades each figure to that cached in-memory math at all-time. Only
        runs on a rebuild -- a steady tick short-circuits before any shaping.
        """
        try:
            with self._data.reader() as connection:
                return {
                    run_id: fractal.core.cost.subtree_spent(connection, run_id)
                    for run_id in run_ids
                }
        except (FileNotFoundError, sqlite3.Error):
            self._failed.add(scope)
            return {
                run_id: self._run_spend_at(scope, run_id, math.inf)
                for run_id in run_ids
            }

    def _run_spend_at(
        self: SnapshotBuilder,
        branch: str,
        run_id: int,
        t: float,
    ) -> float:
        """Compute a run's subtree spend as of instant ``t``.

        Index-building over the cached per-branch maps plus one call into
        the pure core twin (:func:`fractal.core.cost.run_spend`), which owns
        the ``parent_run_id`` chain math beside its SQL pairing.
        """
        costs: dict[int, tuple[Optional[int], float]] = {}
        steps: dict[int, list[tuple[float, float]]] = {}
        for subtree_branch in self._subtree(branch):
            costs.update(self._runcost.get(subtree_branch, {}))
            steps.update(self._runsteps.get(subtree_branch, {}))
        return fractal.core.cost.run_spend(costs, steps, run_id, t)

    def _ensure_log(self: SnapshotBuilder, branch: str) -> None:
        """Cache the branch's shaped activity log (the subtree-log ingredient)."""
        if branch in self._logs:
            return

        def placeholder() -> None:
            self._logs[branch] = ()

        config = self._data.config(branch)
        with self._section(branch, placeholder=placeholder) as connection:
            log_raw = self._data.log_rows(connection, (branch,))
            run_ids = self._data.run_ids(connection, branch)
            self._logs[branch] = _log(log_raw, run_ids, branch, config)

    def _subtree_log(self: SnapshotBuilder, subtree: list[str]) -> tuple[dict, ...]:
        """Merge the subtree's activity logs, newest first (capped like one)."""
        rows: list[dict] = []
        for branch in subtree:
            self._ensure_log(branch)
            rows.extend(self._logs.get(branch, ()))
        rows.sort(
            key=lambda row: (
                row['created_at'],
                row['run_id'] or 0,
                row['iter_id'] or 0,
                row['step_id'] or 0,
            ),
            reverse=True,
        )
        return tuple(rows[:120])

    def _feed(self: SnapshotBuilder, subtree: list[str]) -> tuple[dict, ...]:
        """Merge the cross-subtree broadcast view, newest first.

        Every branch's public + outbox posts (read-only -- never
        ``Radio.feed``).
        """
        rows = []
        for branch in subtree:
            for row in self._radio.get(branch, ()):
                if row['channel'] in ('public', 'outbox'):
                    rows.append(row)
        rows.sort(key=lambda row: row['created_at'], reverse=True)
        return tuple(rows[:50])

    def _tree_rows(self: SnapshotBuilder, scope: str) -> tuple[dict, ...]:
        """Flatten the tree: DFS over the children index in creation order."""
        if self._tree is not None and self._tree_scope == scope:
            return self._tree
        rows: list[dict] = []

        def walk(branch: str, depth: int) -> None:
            """Append ``branch``'s row, then recurse into its children."""
            kids = self._children.get(branch, [])
            brief = self._brief.get(branch, {'status': 'idle', 'signal': ''})
            name = display_name_of(branch, self._titles.get(branch))
            row = {
                'branch': branch,
                'name': name + user_tag(branch, self._data.root_branch),
                'depth': depth,
                'status': brief['status'],
                'signal': brief['signal'],
                'is_user': branch == self._data.root_branch,
                'is_focused': branch == scope,
                'has_kids': bool(kids),
            }
            rows.append(row)
            for kid in kids:
                walk(kid, depth + 1)

        walk(self._data.root_branch, 0)
        self._tree = tuple(rows)
        self._tree_scope = scope
        return self._tree

    def _counts(self: SnapshotBuilder) -> tuple[int, int]:
        """Tally ``(total, active)`` agent nodes -- the user (root) node is not one."""
        total = max(0, len(self._topo or ()) - 1)
        active = sum(
            1
            for branch in self._topo or ()
            if self._brief.get(branch, {}).get('status') == 'active'
        )
        return total, active

    def _geometry(
        self: SnapshotBuilder,
        card: Optional[dict],
        history: tuple[dict, ...],
        scope: str,
        log: tuple[dict, ...],
    ) -> Geometry:
        """Compute every pane width the renderers consume.

        The node pane sizes to the widest of its ident block and an explorer
        row (label + the right-anchored session/dur/cost cluster), widened
        for air, floored by the event log's minimum useful desc column; the
        tree pane sizes to its widest line; computed once per focused-section
        refresh -- renderers never re-derive widths.
        """
        # every log column sizes to its longest value (desc flexes/truncates);
        # lineage columns carry bare ordinals floored to their header labels,
        # and the dur/cost columns share the explorer cluster's widths so the
        # two sections' trailing columns align
        root = self._data.root_branch
        ev_node_w = max(
            (
                len(leaf_of(row['branch']) + user_tag(row['branch'], root))
                for row in log
            ),
            default=0,
        )
        if ev_node_w:
            ev_node_w = max(ev_node_w, len('node'))
        ev_run_w = max(
            (len(str(row['run_n'])) for row in log if row['run_n']),
            default=0,
        )
        if ev_run_w:
            ev_run_w = max(ev_run_w, len('run'))
        ev_iter_w = max(
            (len(str(row['iter_n'])) for row in log if row['iter_n']),
            default=0,
        )
        if ev_iter_w:
            ev_iter_w = max(ev_iter_w, len('iter'))
        ev_step_w = max(
            (len(str(row['step_n'])) for row in log if row['step_n']),
            default=0,
        )
        if ev_step_w:
            ev_step_w = max(ev_step_w, len('step'))
        ev_dur_w = max(
            (len(fmt.dur(row['duration'])) for row in log if row['duration']),
            default=0,
        )
        ev_dur_w = max(ev_dur_w, len('elapsed'))
        ev_cost_w = max(
            (len(fmt.money(row['cost'])) for row in log if row['cost'] is not None),
            default=0,
        )
        ev_cost_w = max(ev_cost_w, theme.COST_W)
        cluster = theme.SESS_W + theme.GAP + ev_dur_w + theme.GAP + ev_cost_w
        if card is not None:
            agent = card['agent'] or theme.EMPTY
            if card['detached']:
                agent += ' (detached)'
            values = [
                agent,
                card['model'] or theme.EMPTY,
                card['session'] or theme.EMPTY,
            ]
            ident_w = theme.IDENT_W + max(len(value) for value in values)
        else:
            ident_w = theme.IDENT_W
        label_w = 0
        for run in history:
            label_w = max(label_w, 2 + len(run['label']))
            for it in run['iters']:
                label_w = max(label_w, 4 + len(it['label']))
                for step in it['steps']:
                    label_w = max(label_w, 6 + len(step['label']))
        fit = max(label_w + 2, theme.NODE_FIT_MIN)
        # the widen air goes to the label area only -- the session/dur/cost
        # cluster and the ident block are already exact-width content
        natural = max(ident_w, int(fit * theme.NODE_WIDEN) + theme.GAP + cluster)
        node_width = natural + theme.NODE_CHROME
        # floor: the event-log desc column must fit its widest node-event verb
        # plus META_MIN metadata chars before the ellipsis
        event_inner = (
            theme.TIME_W
            + theme.GAP
            + ev_node_w
            + theme.GAP
            + ev_run_w
            + theme.GAP
            + ev_iter_w
            + theme.GAP
            + ev_step_w
            + theme.GAP
            + (_EV_LEAD_MAX + 1 + theme.META_MIN + 1)
            + theme.GAP
            + ev_dur_w
            + theme.GAP
            + ev_cost_w
        )
        node_width = max(node_width, event_inner + theme.NODE_CHROME)
        # floor: the measures matrix row (scope label + both figure/gauge
        # columns) must fit without clipping its cost figures
        measures_inner = (
            theme.MEAS_W
            + theme.GAP
            + (theme.EL_FIG_W + theme.GAUGE_GAP + theme.BAR_W)
            + theme.GAP
            + (theme.CO_FIG_W + theme.GAUGE_GAP + theme.BAR_W)
        )
        node_width = max(node_width, measures_inner + theme.NODE_CHROME)
        inner = node_width - theme.NODE_CHROME
        # the tree pane fits its widest (uncollapsed) line and its foot
        lines = fmt.tree_lines(list(self._tree_rows(scope)), set())
        widest = max(
            (len(Text.from_markup(head + label).plain) for _, head, _, label in lines),
            default=20,
        )
        total, active = self._counts()
        foot = f'{active}/{total} nodes running'
        return Geometry(
            tree_width=max(widest, len(foot)) + theme.TREE_CHROME,
            node_width=node_width,
            label_w=inner - theme.GAP - cluster,
            desc_w=inner
            - theme.TIME_W
            - ev_node_w
            - ev_run_w
            - ev_iter_w
            - ev_step_w
            - ev_dur_w
            - ev_cost_w
            - 7 * theme.GAP,
            ev_node_w=ev_node_w,
            ev_run_w=ev_run_w,
            ev_iter_w=ev_iter_w,
            ev_step_w=ev_step_w,
            ev_dur_w=ev_dur_w,
            ev_cost_w=ev_cost_w,
        )


# ------ helper functions


# the focused shape when the scope's worktree is unavailable; the zero-width
# geometry keeps a first build renderable (the app resizes from it)
_EMPTY_SHAPE: dict[str, Any] = {
    'card': None,
    'measures': None,
    'config': {},
    'history': (),
    'log': (),
    'sessions': (),
    'channels': (),
    'geometry': Geometry.empty(),
}


def _sync_mode(config: dict) -> bool:
    """Return the node's sync mode, coerced exactly like the loop coerces it.

    Absent means on; any present value takes its truthiness (so even a
    string ``"false"`` reads as on, matching the running loop).
    """
    sync = config.get('sync')
    return True if sync is None else bool(sync)


def _sync_ids(steps: list[dict], config: dict) -> set[int]:
    """Return the step ids of the built-in sync passes.

    A SYNC-named row is a pass when it is a step-0 drain-wait row, when a
    differently-named step in the same iteration owns its number (the
    pre-step and approval-wait passes), or while it is still open under
    sync mode (a live pre-step pass precedes its own step's row; with sync
    off no pass can exist, so a live SYNC row is a user step mid-flight).
    A settled SYNC row alone on its number reads as a real step -- a user
    step file named SYNC -- so a fatally timed-out pass, whose step never
    launched, deliberately wears that step's number and its own failure.
    """
    live_pass = _sync_mode(config)
    owned: dict[int, set[int]] = {}
    for step in steps:
        if step['step_name'] != _SYNC_NAME:
            owned.setdefault(step['iter_id'], set()).add(step['step'])
    result = set()
    for step in steps:
        if step['step_name'] != _SYNC_NAME:
            continue
        numbers = owned.get(step['iter_id'], set())
        if (
            step['step'] == 0
            or (live_pass and step['ended_at'] is None)
            or step['step'] in numbers
        ):
            result.add(step['step_id'])
    return result


def _context(
    runs: list[dict],
    iters: list[dict],
    steps: list[dict],
    config: dict,
) -> tuple[Optional[dict], Optional[dict], Optional[dict]]:
    """Resolve the displayed (run, iter, step) from the tables.

    Run and iteration are the active else newest; the step is the iteration's
    active step else its last numbered one, sync passes excluded.
    """
    run = next((r for r in runs if r['status'] == 'active'), runs[0] if runs else None)
    if run is None:
        return None, None, None
    run_iters = [i for i in iters if i['run_id'] == run['run_id']]
    it = next(
        (i for i in run_iters if i['status'] == 'active'),
        run_iters[0] if run_iters else None,
    )
    step: Optional[dict] = None
    if it is not None:
        it_steps = [s for s in steps if s['iter_id'] == it['iter_id']]
        sync = _sync_ids(it_steps, config)
        active = next((s for s in it_steps if s['status'] == 'active'), None)
        if active is not None and active['step_id'] not in sync:
            step = active
        else:
            numbered = [s for s in it_steps if s['step_id'] not in sync]
            step = max(
                numbered,
                key=lambda s: (s['step'], s['step_id']),
                default=None,
            )
    return run, it, step


def _end_epoch(row: dict) -> float:
    """Return a history row's end instant (open rows read as "all time")."""
    if row['started'] is None or row['duration'] is None:
        return math.inf
    return row['started'].timestamp() + row['duration']


def _open_epoch(row: Optional[dict]) -> Optional[float]:
    """Return an open row's start instant; ``None`` once settled (no tick)."""
    if row is None or row['ended_at'] is not None:
        return None
    started = parse_ts(row['started_at'])
    if started is None:
        return None
    return started.timestamp()


def _step_total(steps: list[dict], run: dict, sync: set[int]) -> Optional[int]:
    """Return a run's pipeline length: ``MAX(step)``, sync passes excluded.

    Scoped to the given run so a pipeline trimmed between runs (5 -> 3)
    reports the new run's own N rather than the stale all-run max (which never
    shrinks), and a settled run keeps the honest max of what it actually ran.
    """
    scoped = [s for s in steps if s['run_id'] == run['run_id']]
    total = max(
        (s['step'] for s in scoped if s['step_id'] not in sync),
        default=0,
    )
    return total or None


def _cost_or_none(value: object) -> Optional[float]:
    """Coerce an agent-editable cost/budget config value to a float.

    ``config.json`` is edited directly on the steering path, so a cost cap or
    reserve may arrive as a string (the natural mistake when an agent
    self-tunes); the node pane divides and subtracts against it, so a mistyped
    value reads as no cap rather than a TypeError that crashes the whole
    cockpit build -- the cost sibling of the duration coercion in
    :meth:`SnapshotBuilder._measures`.
    """
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _card(
    branch: str,
    brief: dict,
    step: Optional[dict],
    config: dict,
    session: Optional[str],
) -> dict:
    """Shape the node card's head/ident state."""
    agent = (step['agent'] if step and step['agent'] else config.get('agent')) or ''
    model = (step['model'] if step and step['model'] else config.get('model')) or ''
    return {
        'branch': branch,
        'status': brief['status'],
        'signal': brief['signal'],
        'agent': agent,
        'model': model,
        'detached': bool(config.get('detached')),
        'session': session,
    }


def _iter_session(it: dict, it_steps: list[dict]) -> Optional[str]:
    """Return an iteration's display session.

    Stamped on the row at close; an open iteration reads its newest stamped
    step (the stream stamps it as soon as the step opens), so the currently
    running session shows while the iteration is live.
    """
    if it['session']:
        return it['session']
    stamped = [s for s in it_steps if s['session']]
    if not stamped:
        return None
    newest = max(stamped, key=lambda s: s['step_id'])
    return newest['session']


def _history(
    runs: list[dict],
    iters: list[dict],
    steps: list[dict],
    runcost: dict[int, tuple[Optional[int], float]],
    config: dict,
) -> tuple[dict, ...]:
    """Shape the explorer tree: runs (newest first) -> iters -> steps.

    Labels are final display strings (``run 2`` / ``iter 3`` / ``step 1:
    PREPARE`` / ``sync``); every row carries its database ids so the explorer
    and the event-log reveal address exact entities. A step's own pre-step
    sync folds into it (time spans both, costs sum) rather than listing
    separately; drain-wait and still-live syncs list as standalone ``sync``
    rows in chronological place, and a settled SYNC row alone on its number
    lists as the numbered step it is -- a user step named SYNC.
    """
    iters_by_run: dict[int, list[dict]] = {}
    for it in iters:
        iters_by_run.setdefault(it['run_id'], []).append(it)
    steps_by_iter: dict[int, list[dict]] = {}
    for step in steps:
        steps_by_iter.setdefault(step['iter_id'], []).append(step)
    sync = _sync_ids(steps, config)
    total = len(runs)
    result = []
    for index, run in enumerate(runs):
        iter_rows = []
        for it in iters_by_run.get(run['run_id'], []):
            it_steps = sorted(
                steps_by_iter.get(it['iter_id'], []),
                key=lambda s: (s['step'], s['step_id']),
            )
            # the iteration's representative step: its active one, else the last
            display = next(
                (s for s in it_steps if s['status'] == 'active'),
                it_steps[-1] if it_steps else None,
            )
            if display is not None:
                total_steps = _step_total(steps, run, sync)
                step_name = display['step_name']
                step_num = display['step']
                step_disp = f'{step_name} {step_num}/{total_steps}'
            else:
                step_disp = 'done'
            # each numbered step absorbs the sync passes that share its
            # recorded number (its pre-step sync); the step-0 drain-wait
            # and still-live passes precede no recorded step, so they keep
            # standalone `sync` rows owning their time and cost
            numbers = {s['step'] for s in it_steps if s['step_id'] not in sync}
            folded: dict[int, list[dict]] = {}
            standalone: set[int] = set()
            for s in it_steps:
                if s['step_id'] not in sync:
                    continue
                if s['step'] in numbers:
                    folded.setdefault(s['step'], []).append(s)
                else:
                    standalone.add(s['step_id'])
            step_rows = []
            for step in it_steps:
                is_sync = step['step_id'] in sync
                if is_sync and step['step_id'] not in standalone:
                    continue
                if is_sync:
                    # step 0 keeps the selected card reading plain `sync`
                    # (the run-line sentinel), whatever number a live
                    # pre-step pass recorded
                    label = 'sync'
                    step_num = 0
                    syncs = []
                else:
                    step_num = step['step']
                    step_name = step['step_name']
                    label = f'step {step_num}: {step_name}'
                    syncs = folded.get(step['step'], [])
                costs = [
                    cost
                    for cost in (step['cost'], *(s['cost'] for s in syncs))
                    if cost is not None
                ]
                started_at = min(
                    (step['started_at'], *(s['started_at'] for s in syncs))
                )
                step_row = {
                    'label': label,
                    'status': step['status'],
                    'step': step_num,
                    'name': step['step_name'],
                    'agent': step['agent'],
                    'model': step['model'],
                    'cost': fmt.money(sum(costs) if costs else None),
                    'cost_raw': sum(costs) if costs else None,
                    'session': step['session'],
                    'started': parse_ts(started_at),
                    'duration': span(started_at, step['ended_at']),
                    'run_id': step['run_id'],
                    'iter_id': step['iter_id'],
                    'step_id': step['step_id'],
                }
                step_rows.append(step_row)
            # chronological listing (a merged step sorts at its sync's start);
            # the running spend then reads "through this row, syncs included"
            step_rows.sort(key=lambda row: row['started'])
            iter_spend = 0.0
            for row in step_rows:
                iter_spend += row['cost_raw'] or 0
                row['iter_spend'] = iter_spend
            iter_cost = sum(s['cost'] or 0 for s in it_steps)
            iter_num = it['iter']
            iter_row = {
                'label': f'iter {iter_num}',
                'status': it['status'],
                'iter': it['iter'],
                'cost': fmt.money(iter_cost),
                'cost_raw': iter_cost,
                'session': _iter_session(it, it_steps),
                'step': step_disp,
                'started': parse_ts(it['started_at']),
                'duration': span(it['started_at'], it['ended_at']),
                'steps': tuple(step_rows),
                'run_id': it['run_id'],
                'iter_id': it['iter_id'],
            }
            iter_rows.append(iter_row)
        own_cost = runcost.get(run['run_id'], (None, 0.0))[1]
        # the run's display session is its newest iteration's
        session = next((row['session'] for row in iter_rows if row['session']), None)
        run_row = {
            'label': f'run {total - index}',
            'status': run['status'],
            'number': total - index,
            'cost': fmt.money(own_cost),
            'session': session,
            'started': parse_ts(run['started_at']),
            'duration': span(run['started_at'], run['ended_at']),
            'iters': tuple(iter_rows),
            'run_id': run['run_id'],
        }
        result.append(run_row)
    return tuple(result)


def _log(
    rows: list[dict],
    run_ids: list[int],
    branch: str,
    config: dict,
) -> tuple[dict, ...]:
    """Shape the activity view rows: derive each row's kind from its ids.

    Every row carries the owning ``branch`` and its lineage ordinals
    ``run_n``/``iter_n``/``step_n`` (the run ordinal is attached here -- the
    view only knows ids; ``run_ids`` lists the branch's runs newest first);
    a sync pass keeps ``step_n == 0``, which the log renders as an empty
    step cell. Step rows also carry ``sync`` -- the pass-vs-step verdict the
    log's muting keys on, :func:`_sync_ids`'s rule derived from the window's
    own rows.
    """
    total = len(run_ids)
    run_numbers = {run_id: total - index for index, run_id in enumerate(run_ids)}
    # classify the window's sync passes like _sync_ids does from the steps
    # table: a step settles when its end row lands, and rows are newest-first,
    # so any start row in the window has its end row here too
    live_pass = _sync_mode(config)
    owned: dict[int, set[int]] = {}
    ended: set[int] = set()
    for row in rows:
        if row['event_id'] is not None or row['step_id'] is None:
            continue
        if row['step_name'] != _SYNC_NAME:
            owned.setdefault(row['iter_id'], set()).add(row['step_n'])
        elif row['event'] == 'end':
            ended.add(row['step_id'])
    sync_ids: set[int] = set()
    for row in rows:
        if row['event_id'] is not None or row['step_id'] is None:
            continue
        if row['step_name'] == _SYNC_NAME and (
            row['step_n'] == 0
            or (live_pass and row['step_id'] not in ended)
            or row['step_n'] in owned.get(row['iter_id'], set())
        ):
            sync_ids.add(row['step_id'])
    result = []
    for row in rows:
        if row['event_id'] is not None:
            kind = 'node'
        elif row['step_id'] is not None:
            kind = 'step'
        elif row['iter_id'] is not None:
            kind = 'iter'
        else:
            kind = 'run'
        numbers = {
            'step': row['step_n'],
            'iter': row['iter_n'],
            'run': run_numbers.get(row['run_id']),
        }
        number = numbers.get(kind)
        log_row = {
            'kind': kind,
            'n': number,
            'run_n': run_numbers.get(row['run_id']),
            'iter_n': row['iter_n'],
            'step_n': row['step_n'],
            'branch': branch,
            'name': row['step_name'] if kind == 'step' else None,
            'sync': kind == 'step' and row['step_id'] in sync_ids,
            'event': row['event'],
            'status': row['status'],
            'exit_code': row['exit_code'],
            'duration': row['duration'],
            'cost': row['cost'],
            'metadata': row['metadata'] or '',
            'created_at': parse_ts(row['timestamp']),
            'run_id': row['run_id'],
            'iter_id': row['iter_id'],
            'step_id': row['step_id'],
            'event_id': row['event_id'],
        }
        result.append(log_row)
    return tuple(result)


def _sessions(steps: list[dict]) -> tuple[str, ...]:
    """Return distinct woven sessions, newest first (the compose combo).

    Sourced from the step rows -- stamped as soon as each invocation's
    stream opens -- so a live session is offered within seconds of launch,
    not only after its iteration ends.
    """
    seen: list[str] = []
    for step in steps:
        session = step['session']
        if session and session not in seen:
            seen.append(session)
    return tuple(seen)


def _shape_radio(
    messages: list[dict],
    owner: str,
    reacts: dict[int, tuple[int, int]],
) -> tuple[dict, ...]:
    """Shape raw message rows into the radio pane contract, newest first."""
    rows = [_message(row, owner, reacts) for row in messages]
    rows.sort(key=lambda row: row['created_at'], reverse=True)
    return tuple(rows)


def _message(
    row: dict,
    owner: str,
    reacts: dict[int, tuple[int, int]],
) -> dict:
    """Shape a raw message/archive row into the radio pane contract.

    ``read`` is the owning node's own state (its receipt in ``reads``,
    surfaced by the loader as ``is_read``): browsing another node's mailbox
    observes it without ever changing it, and only the user's own mailbox is
    interactive. Archive rows (no ``is_read`` key) read as read.
    """
    message_id = row['message_id']
    read = bool(row.get('is_read', True))
    pos, neg = reacts.get(message_id, (0, 0))
    return {
        'message_id': message_id,
        'message_uuid': row['message_uuid'],
        'channel': row['channel'],
        'sender': row['sender'],
        'session': row.get('session'),
        'node': owner,
        'priority': row['priority'],
        'subject': row['subject'],
        'data': row['data'],
        'created_at': parse_ts(row['created_at']),
        'read': read,
        'pos_reacts': pos,
        'neg_reacts': neg,
    }
