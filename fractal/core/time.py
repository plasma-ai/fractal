"""Implements ``Time`` class."""

from __future__ import annotations

import os
import typing
from typing import Optional

import fractal.util

if typing.TYPE_CHECKING:
    from .db import Database
    from .node import Node

__all__ = []


class Time:
    """Deadline accounting over config timeouts and row instants."""

    def __init__(self: Time, node: Node) -> None:
        """Initialize ``Time``.

        Args:
            node: The owning ``Node`` instance.

        """
        self._node = node

    @property
    def node(self: Time) -> Node:
        """Return the owning node."""
        return self._node

    @property
    def db(self: Time) -> Database:
        """Return the central database."""
        return self._node.db

    def remaining(
        self: Time,
        *,
        scope: Optional[str] = None,
        run_id: Optional[int] = None,
    ) -> Optional[float]:
        """Compute seconds left before a timeout fires.

        Derives each deadline from the configured timeout and the
        relevant ``started_at`` (mirroring how ``Cost.remaining`` reads
        persisted state), so it works for any node, not only the running
        one. The ``run`` scope (``--timeout``) is anchored on the active
        run's ``started_at``; the ``iter`` scope (``--iter-timeout``) on
        its active iteration's; the ``step`` scope on its active step's,
        against that step's effective ceiling (see :meth:`limit`), with a
        steered relaunch also charged its stuck launch's span, as the loop
        charges it. With no ``scope`` the soonest of the configured
        deadlines is returned -- the time until the next timeout fires.

        Args:
            scope: ``'run'``, ``'iter'``, or ``'step'`` to query one
                level; the soonest of all if omitted.
            run_id: Run to query. Auto-resolved if omitted.

        Returns:
            Remaining seconds (clamped at ``0``), or ``None`` if no
            timeout is configured for the scope or no run/iteration
            is active.

        """
        # resolve run
        if run_id is None:
            _, _, run_id = self._node.record.resolve_context()
        if run_id is None:
            return None
        # no scope -> soonest across the configured run/iter/step deadlines
        if scope is None:
            run = self.remaining(scope='run', run_id=run_id)
            iter = self.remaining(scope='iter', run_id=run_id)
            step = self.remaining(scope='step', run_id=run_id)
            candidates = [
                remaining for remaining in (run, iter, step) if remaining is not None
            ]
            return min(candidates) if candidates else None
        # read the scope's configured limit
        seconds = self.limit(scope)
        if seconds is None:
            return None
        # anchor on the scope's active row: the run itself, or
        # the run's active iteration/step
        if scope == 'run':
            rows = self.db.read(
                'runs',
                where={'run_id': run_id, 'ended_at': None},
                limit=1,
            )
        elif scope == 'iter':
            rows = self.db.read(
                'iters',
                where={'status': 'active', 'run_id': run_id},
                limit=1,
            )
        else:
            rows = self.db.read(
                'steps',
                where={'status': 'active', 'run_id': run_id},
                limit=1,
            )
        if not rows:
            return None
        # subtract elapsed time from the budget, crediting paused spans -- a
        # pause parks the loop with the run/iter rows left open, so the raw
        # wall clock would charge the frozen time against the deadline (a
        # step never spans a pause: the interrupted step row closes paused
        # and resume opens a fresh one); a steered step also owes its stuck
        # launch's span
        elapsed = fractal.util.time.elapsed(rows[0]['started_at'])
        if scope in ('run', 'iter'):
            elapsed -= self._pause_credit(run_id=run_id, since=rows[0]['started_at'])
        else:
            elapsed += self._steered_span(rows[0])
        remaining = seconds - elapsed
        return remaining if remaining > 0 else 0.0

    def limit(self: Time, scope: Optional[str] = None) -> Optional[float]:
        """Return the configured limit seconds for a scope (``run`` is ``timeout``).

        Answers for an idle node too: the ``run`` scope reads the
        whole-run ``timeout`` key, ``iter`` the ``iter_timeout`` key, with
        an unset one falling back to ``interval`` (the loop bounds an
        interval iteration by its slot). The ``step`` scope is the active
        step's effective ceiling -- its step file's ``timeout:`` override,
        else ``step_timeout`` (see :meth:`_step_limit`). With no ``scope``
        the soonest applicable limit (the smallest configured) is
        returned.

        Args:
            scope: ``'run'``, ``'iter'``, or ``'step'`` to query one
                level; the soonest of all if omitted.

        Returns:
            Limit in seconds, or ``None`` when nothing applicable is
            configured (or a stored duration does not parse).

        """
        # no scope -> soonest across the configured run/iter/step limits
        if scope is None:
            limits = (self.limit('run'), self.limit('iter'), self.limit('step'))
            candidates = [limit for limit in limits if limit is not None]
            return min(candidates) if candidates else None
        # read the scope's timeout from config
        if scope == 'run':
            timeout = self._node.config.get('timeout')
        elif scope == 'iter':
            timeout = self._node.config.get('iter_timeout')
            # interval bounds the iteration to its slot when no explicit
            # iter_timeout is set (mirrors the loop's per-iteration default)
            if not timeout:
                timeout = self._node.config.get('interval')
        elif scope == 'step':
            return self._step_limit()
        else:
            raise ValueError(f"scope must be 'run', 'iter', or 'step', got {scope!r}.")
        if not timeout:
            return None
        return fractal.util.parse_duration_seconds(timeout)

    def _step_limit(self: Time) -> Optional[float]:
        """Return the active step's effective ceiling seconds.

        Follows the loop's launch rule (``Step.limit``): the active step
        file's ``timeout:`` override when it parses, else the node's
        ``step_timeout``, which also answers when no step is active. The
        node's own seat -- its exported ``STEP_ID`` naming the active step
        -- reads its exported ``STEP_TIMEOUT_SECONDS``, the exact ceiling
        its launch enforces. Every other caller reads the step file, which
        the loop reads once per iteration: an edit landing mid-iteration
        shows here before the loop enforces it at the next iteration.
        """
        from .loop import Step

        # the node default
        timeout = self._node.config.get('step_timeout')
        default = fractal.util.parse_duration_seconds(timeout) if timeout else None
        step_id, _, _ = self._node.record.resolve_context(active=True)
        if step_id is None:
            return default
        # the node's own seat: its launch exported the enforced ceiling
        # (``-1`` when none); seat-editable, so garbage reads the file
        if os.environ.get('STEP_ID') == f'{step_id}':
            try:
                seconds = int(os.environ.get('STEP_TIMEOUT_SECONDS', ''))
            except ValueError:
                pass
            else:
                return float(seconds) if seconds > 0 else None
        # the row's step file; the SYNC pseudo-step runs from the package's
        # mode doc, and a vanished, unreadable, or malformed file falls back
        # to the node value, as at launch
        row = self.db.read('steps', where={'step_id': step_id}, limit=1)[0]
        try:
            if row['step_name'] == 'SYNC':
                sync = self._node._package_dir / '_node' / 'modes' / 'SYNC.md'
                step = Step.load(sync)
            else:
                step = Step.find(
                    self._node.node_dir / 'steps',
                    number=row['step'],
                    name=row['step_name'],
                )
            seconds = default if step is None else step.limit(default)
        except (OSError, ValueError):
            seconds = default
        return float(seconds) if seconds is not None and seconds > 0 else None

    def _steered_span(self: Time, row: dict) -> float:
        """Return the seconds a steered step's stuck launch already ran.

        A steer relaunches a stuck step on a row of its own, and the loop
        charges the stuck launch's span against the steered launch's
        ceiling, so the step deadline runs from the attempt's first
        launch. The stuck row is the step's previous row in the iteration,
        closed with a ``steered:`` reason; an attempt buys at most one
        steer, so the chain is one row long. A retry or a model-drop
        re-dispatch arms a fresh deadline in the loop, and its row never
        follows a ``steered:`` one.

        Args:
            row: The active step row.

        Returns:
            The stuck launch's seconds, ``0.0`` when the row is no steer.

        """
        rows = self.db.read(
            'steps',
            where={
                'iter_id': row['iter_id'],
                'step': row['step'],
                'step_name': row['step_name'],
            },
        )
        prior = [other for other in rows if other['step_id'] < row['step_id']]
        if not prior or not prior[0]['metadata'].startswith('steered: '):
            return 0.0
        stuck = prior[0]
        started = fractal.util.time.elapsed(stuck['started_at'])
        return started - fractal.util.time.elapsed(stuck['ended_at'])

    def _pause_credit(
        self: Time,
        *,
        run_id: int,
        since: Optional[str] = None,
    ) -> float:
        """Return seconds the run spent paused, from its pause/resume instants.

        Sums the pause->resume spans recorded on the run's events, clipping
        each to ``since`` (an iteration deadline credits only the span inside
        the iteration); an unmatched trailing pause accrues to now (the node
        is still parked). The instants are the substrate for pause-aware cost
        attribution as well.

        Args:
            run_id: Run whose events are scanned.
            since: ISO instant spans are clipped to (the scope's anchor).

        Returns:
            Credited seconds (``0.0`` when the run never paused).

        """
        rows = self.db.read(
            'events',
            where={'node': self._node.branch, 'run_id': run_id},
        )
        # a failed resume never relaunched the loop (the node is still
        # parked), so only a completed one closes a span; a failed pause
        # still opens one -- its signal is durable and the loop parks anyway
        spans = [
            row
            for row in rows
            if row['event'] == 'pause'
            or (row['event'] == 'resume' and row['status'] == 'completed')
        ]
        spans.sort(key=lambda row: row['created_at'])
        # walk the interleaved instants; duplicate pauses collapse onto the
        # first, duplicate resumes are inert, and the fixed-width UTC format
        # makes string comparison safe
        credit = 0.0
        pause_at = None
        for row in spans:
            if row['event'] == 'pause':
                if pause_at is None:
                    pause_at = row['created_at']
            elif pause_at is not None:
                start = max(pause_at, since) if since else pause_at
                end = row['created_at']
                if end > start:
                    credit += fractal.util.time.elapsed(start)
                    credit -= fractal.util.time.elapsed(end)
                pause_at = None
        if pause_at is not None:
            start = max(pause_at, since) if since else pause_at
            credit += max(fractal.util.time.elapsed(start), 0.0)
        return credit
