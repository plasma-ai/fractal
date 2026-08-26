"""Implements ``Record`` class."""

from __future__ import annotations

import contextlib
import logging
import typing
from typing import Any, Optional

import fractal.util
from fractal.constants import EVENTS, STATUSES
from fractal.typing import Row

if typing.TYPE_CHECKING:
    import sqlite3

    from .db import Database
    from .node import Node

__all__ = []


class Record:
    """Row-accounting surface over the tree's central database.

    The loop-facing persistence API: every lifecycle transition funnels
    through first-writer-wins fenced updates, events are point-in-time log
    entries, and signals are consumable control rows.
    """

    def __init__(self: Record, node: Node) -> None:
        """Initialize ``Record``.

        Args:
            node: The owning ``Node`` instance.

        """
        self._node = node

    @property
    def node(self: Record) -> Node:
        """Return the owning node."""
        return self._node

    @property
    def db(self: Record) -> Database:
        """Return the central database."""
        return self._node.db

    def resolve_context(
        self: Record,
        run_id: Optional[int] = None,
        iter_id: Optional[int] = None,
        step_id: Optional[int] = None,
        *,
        active: bool = False,
    ) -> tuple[Optional[int], Optional[int], Optional[int]]:
        """Resolve the ``(step_id, iter_id, run_id)`` context.

        Explicit ids win wholesale: they are returned verbatim (after the
        chain check) and the current context -- even when one exists -- is
        not consulted. Otherwise ``run_id`` is the latest active run, else
        the most recent run (the run container persists across iterations)
        -- unless ``active``, which returns ``None`` for ``run_id`` when no
        run is active (for events that do not belong to the node's own run).
        ``step_id``/``iter_id`` are the in-flight (active) step/iteration,
        or ``None`` when nothing is running -- a row only pins to something
        currently active. All ``None`` if the node is not initialized or has
        no runs.

        Args:
            run_id: Explicit run id, returned verbatim.
            iter_id: Explicit iteration id (requires ``run_id``).
            step_id: Explicit step id (requires ``iter_id`` and ``run_id``).
            active: Resolve ``run_id`` active-only (no most-recent fallback).

        Returns:
            ``(step_id, iter_id, run_id)``, each ``None`` where not
            applicable.

        Raises:
            ValueError: When the explicit lineage does not chain
                (``step_id`` without ``iter_id``/``run_id``, or ``iter_id``
                without ``run_id``).

        """
        # explicit lineage must chain: a step belongs to an iteration and an
        # iteration to a run, so a dangling child id is a caller bug
        if step_id is not None and (iter_id is None or run_id is None):
            raise ValueError('step_id requires iter_id and run_id.')
        if iter_id is not None and run_id is None:
            raise ValueError('iter_id requires run_id.')
        # explicit ids win wholesale -- a run-only prefix stays
        # partial (no per-field backfill)
        if run_id is not None:
            return step_id, iter_id, run_id
        # skip if the node is not initialized
        if not self._node.exists():
            return None, None, None
        # run: the active one, else the most recent (the container persists),
        # unless active -- then NULL when nothing is active
        branch = self._node.branch
        rows = self.db.read(
            'runs',
            where={'node': branch, 'status': 'active'},
            limit=1,
        )
        if rows:
            run_id = rows[0]['run_id']
        elif active:
            return None, None, None
        else:
            rows = self.db.read('runs', where={'node': branch}, limit=1)
            run_id = rows[0]['run_id'] if rows else None
            return None, None, run_id
        # step: only in-flight (active)
        rows = self.db.read(
            'steps',
            where={'node': branch, 'status': 'active'},
            limit=1,
        )
        step_id = rows[0]['step_id'] if rows else None
        # iteration: only in-flight (active)
        rows = self.db.read(
            'iters',
            where={'node': branch, 'status': 'active'},
            limit=1,
        )
        iter_id = rows[0]['iter_id'] if rows else None
        # return context
        return step_id, iter_id, run_id

    def _fenced(
        self: Record,
        data: dict[str, Any],
        table: str,
        *,
        where: dict[str, Any],
        connection: Optional[sqlite3.Connection] = None,
    ) -> int:
        """Write a lifecycle row transition (the single write path).

        Every ``runs``/``iters``/``steps`` transition write (status
        terminals, the approval gate) routes through here, keeping the
        transition guards uniform and greppable: a guard in ``where``
        (e.g. ``ended_at IS NULL``) makes the transition
        first-writer-wins, and the returned count is the observable
        verdict -- 0 means another writer already won. The step recorder
        columns (``session``/``model``/``cost`` and the ``unpriced``
        marker) deliberately write raw: additive audit figures, safe on
        closed rows.

        Args:
            data: Column values to set.
            table: Lifecycle table (``runs``/``iters``/``steps``).
            where: Column filters, including the transition guard.
            connection: Transaction to run inside (from
                :meth:`Database.transaction`).

        Returns:
            Number of rows transitioned.

        """
        return self.db.update(data, table, where=where, connection=connection)

    def close_open(
        self: Record,
        status: str,
        *,
        skip_event: Optional[int] = None,
        metadata: Optional[str] = None,
        connection: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Close every still-open run/iteration/step row with a terminal.

        Stamps ``status``/``exit_code``/``ended_at`` on the node's
        runs/iters/steps rows that are still open (``ended_at IS NULL``),
        first-writer-wins so a clean end's rows are left untouched. Shared
        by :meth:`run_start`, :meth:`Loop._on_exit`,
        :meth:`Node._reconcile_status`, and :meth:`Node._mark_active_killed`
        so the cascade has one definition and the DB can never diverge
        from the ``.status`` file. The row
        cascade -- the unpriced pre-read, the three closes, and the marker
        stamps -- commits as one transaction, so a crash mid-cascade can
        never leave e.g. a closed run over still-active iteration/step
        rows.

        Every close here is abnormal (exit 1), so an open step whose stream
        opened (session recorded) but whose cost never flushed gets the
        ``unpriced`` metadata marker, mirroring :meth:`step_end`.

        With ``skip_event`` -- the kill cascade -- any still-active event is
        closed by ``status`` too (events have no ``ended_at``), skipping the
        in-flight kill event itself (``Node._kill`` finalizes that one via
        :meth:`event_end`).

        Args:
            status: Terminal status for the open rows.
            skip_event: The in-flight kill event to leave untouched; when
                given, stray active events are swept as well.
            metadata: Stamped on the closing run rows only (the close's
                attribution); ``None`` leaves metadata untouched.
            connection: Transaction to run the row cascade inside (from
                :meth:`Database.transaction`); opens its own when omitted.

        """
        branch = self._node.branch
        now = fractal.util.time.utc_now()
        # the row cascade is one atomic unit: join the caller's transaction
        # when given (run_start pairs it with the successor's insert), else
        # open one here
        if connection is not None:
            transaction = contextlib.nullcontext(connection)
        else:
            transaction = self.db.transaction()
        with transaction as connection:
            # read the open streamed-but-unpriced steps before closing them --
            # they get the unpriced marker once stamped
            rows = self.db.read(
                'steps',
                where={'node': branch, 'ended_at': None},
                connection=connection,
            )
            unpriced = [
                row
                for row in rows
                if row['cost'] is None and row['session'] is not None
            ]
            for table in ('runs', 'iters', 'steps'):
                data = {
                    'status': status,
                    'exit_code': 1,
                    'ended_at': now,
                }
                # the run row carries the close's attribution; iteration and
                # step rows inherit the story through their run
                if metadata is not None and table == 'runs':
                    data['metadata'] = metadata
                self._fenced(
                    data=data,
                    table=table,
                    where={'node': branch, 'ended_at': None},
                    connection=connection,
                )
            # stamp the marker on the closed streamed rows, guarded on the
            # cost still being absent -- a flush serializes around the
            # transaction: landing before, it prices the row out of the
            # pre-read; landing after, step_cost strips the marker again
            for row in unpriced:
                meta = row['metadata']
                data = {'metadata': f'{meta}; unpriced' if meta else 'unpriced'}
                self.db.update(
                    data=data,
                    table='steps',
                    where={'step_id': row['step_id'], 'cost': None},
                    connection=connection,
                )
        # the kill cascade also closes any stray active event by status,
        # skipping the in-flight kill event; the transition guards on the
        # event still being active, so an event_end racing the sweep (a
        # parent approve, an operator finish) stays first-writer-wins
        if skip_event is None:
            return
        data = {'status': status, 'exit_code': 1}
        for row in self.db.read('events', where={'node': branch, 'status': 'active'}):
            if row['event_id'] == skip_event:
                continue
            self.db.update(
                data=data,
                table='events',
                where={'event_id': row['event_id'], 'status': 'active'},
            )

    def run_start(self: Record) -> int:
        """Create a run row with ``status='active'``.

        Reconciles a stranded lifecycle first: any run (and its still-open
        iteration/step) left active by a dead loop is stamped ``exited``,
        so run resolution stays unambiguous. This is safe because
        ``start.sh`` refuses to launch while the node's tmux session exists
        -- one loop per node -- so an open row here is provably orphaned.

        The row stamps the cost cap armed at launch (``max_cost``, NULL
        when uncapped), so the arm survives later config retunes. The
        reconcile and the insert commit as one transaction, so a crash
        between them can never close the stranded rows without their
        successor.

        Returns:
            Run ID.

        """
        # reconcile a crashed loop's stranded rows: stamp every still-open
        # row exited (first-writer-wins, so a clean end's rows are untouched);
        # the close and the insert share one transaction
        branch = self._node.branch
        with self.db.transaction() as connection:
            self.close_open('exited', connection=connection)
            # create the new active run with the node's default agent and
            # armed cost cap, linked to the parent's active run (NULL at root
            # / when the parent is idle); the start instant is captured after
            # the reconcile so the stranded rows' ended_at never postdates it
            now = fractal.util.time.utc_now()
            agent = self._node._default_agent()
            data = {
                'node': branch,
                'parent_run_id': self._parent_run_id(),
                'agent': agent,
                'max_cost': self._node.config.get('max_cost'),
                'status': 'active',
                'started_at': now,
            }
            return self.db.write(data, 'runs', connection=connection)

    def run_end(
        self: Record,
        *,
        run_id: int,
        status: str,
        exit_code: int,
        metadata: Optional[str] = None,
    ) -> int:
        """End a run.

        First-writer-wins: stamps ``status``, ``exit_code``, and
        ``ended_at`` only while the run is still open (``ended_at IS
        NULL``), so a racing kill and the loop's own end can't overwrite
        each other. Duration is derived from ``started_at``/``ended_at``;
        cost rolls up from ``steps`` -- neither is stored on the run.

        Args:
            run_id: Run to end.
            status: Final status.
            exit_code: Process exit code.
            metadata: Optional short reason the run ended (e.g. ``Reached max
                iterations``) for visibility in ``node activity``; the metadata
                column is left untouched when ``None``.

        Returns:
            Rows transitioned -- 0 when another writer already closed it.

        """
        # validate the row status against the known set
        if status not in STATUSES:
            raise _invalid_status(status)
        # stamp the terminal only if still open (first-writer-wins)
        now = fractal.util.time.utc_now()
        data = {
            'status': status,
            'exit_code': exit_code,
            'ended_at': now,
        }
        # record a reason only when given (don't clobber existing metadata)
        if metadata is not None:
            data['metadata'] = metadata
        return self._fenced(
            data=data,
            table='runs',
            where={'run_id': run_id, 'ended_at': None},
        )

    def run_open(self: Record) -> Optional[Row]:
        """Resolve the open run and re-entry context a resume boot adopts.

        Pause parks with the run row open; the resume boot reuses it
        instead of opening a fresh one (which would re-arm the budget).
        The run's newest iteration decides the re-entry: an open one is
        adopted, re-entering at the first step that never completed --
        derived from the completed step rows, since a checkpoint or drain
        park writes no paused row and rows from an earlier pause cycle are
        stale -- while a closed one (a boundary pause) only anchors the
        numbering. A step paused awaiting approval that was approved while
        parked is skipped past instead of re-run (``'; unpriced'`` may
        have been appended, so the marker matches as a prefix).

        Returns:
            ``{'run_id', 'iter', 'iter_id', 'resume_step'}`` -- ``iter_id``
            and ``resume_step`` are ``None`` at a boundary, ``iter`` is
            ``None`` when the run has no iterations -- or ``None`` when no
            run is open.

        """
        runs = self.db.read(
            'runs',
            where={'node': self._node.branch, 'ended_at': None},
            limit=1,
        )
        if not runs:
            return None
        run_id = runs[0]['run_id']
        result = {
            'run_id': run_id,
            'iter': None,
            'iter_id': None,
            'resume_step': None,
        }
        iters = self.db.read('iters', where={'run_id': run_id}, limit=1)
        if not iters:
            return result
        newest, *_ = iters
        result['iter'] = newest['iter']
        if newest['ended_at'] is not None:
            return result
        result['iter_id'] = newest['iter_id']
        # re-enter at the first step that never completed (SYNC rows carry
        # the number of the step they precede, not their own work)
        steps = self.db.read(
            'steps',
            where={'iter_id': newest['iter_id'], 'status': 'completed'},
        )
        completed_max = max(
            (row['step'] for row in steps if row['step_name'] != 'SYNC'),
            default=0,
        )
        resume_step = completed_max + 1
        # skip past approved awaiting-approval steps: they close paused,
        # never completed, so each holds the re-entry until its approval
        # lands -- and the lookup is scoped per step, so a newer paused row
        # from a later pause cycle can never shadow an earlier approval
        while True:
            where = {
                'iter_id': newest['iter_id'],
                'status': 'paused',
                'step': resume_step,
            }
            paused = self.db.read('steps', where=where, limit=1)
            if not paused:
                break
            if not paused[0]['metadata'].startswith('awaiting approval'):
                break
            if not self.step_approved(step_id=paused[0]['step_id']):
                break
            resume_step += 1
        result['resume_step'] = resume_step
        return result

    def run_latest(
        self: Record,
        *,
        branch: Optional[str] = None,
    ) -> Optional[int]:
        """Return the latest run ID for a branch -- active first, else most recent.

        Mirrors the current-run resolution the cost family uses, but keyed
        by an explicit ``branch`` so it also answers for a deleted node:
        registry rows die with the node while its runs persist (keyed by
        branch text), and this is the entry point for reading them.

        Args:
            branch: Branch to resolve; this node's own branch if omitted.

        Returns:
            Run ID, or ``None`` when the branch has no recorded runs.

        """
        # resolve the run: the active one, else the most recent (rowid DESC)
        branch = branch or self._node.branch
        rows = self.db.read(
            'runs',
            where={'node': branch, 'status': 'active'},
            limit=1,
        )
        if not rows:
            rows = self.db.read('runs', where={'node': branch}, limit=1)
        return rows[0]['run_id'] if rows else None

    def run_branches(self: Record) -> list[str]:
        """Return every branch with a recorded run, one entry per branch.

        The companion to :meth:`run_latest` for deleted nodes: registry
        rows die with the node while its runs persist keyed by branch
        text, so this is the name pool a short-name lookup expands
        against.

        Returns:
            Distinct branch names from the runs table.

        """
        rows = self.db.read(query='SELECT DISTINCT node FROM runs')
        return [row['node'] for row in rows]

    def iter_start(
        self: Record,
        *,
        run_id: int,
        iter: int,
    ) -> int:
        """Create an iteration row with ``status='active'``.

        Args:
            run_id: Parent run.
            iter: Iteration number within the run.

        Returns:
            Iteration ID.

        """
        agent = self._node._default_agent()
        model = self._node.config.get('model')
        now = fractal.util.time.utc_now()
        data = {
            'node': self._node.branch,
            'run_id': run_id,
            'iter': iter,
            'agent': agent,
            'model': model,
            'status': 'active',
            'started_at': now,
        }
        return self.db.write(data, 'iters')

    def iter_end(
        self: Record,
        *,
        iter_id: int,
        status: str,
        exit_code: int,
        metadata: Optional[str] = None,
    ) -> int:
        """End an iteration.

        First-writer-wins via the ``ended_at IS NULL`` guard. Duration is
        derived from ``started_at``/``ended_at`` and cost rolls up from
        ``steps`` -- neither is stored. Records the default agent's session
        (continuous mode) so the iteration stays resumable. An iteration
        whose configured model was unset inherits the steps' recorded
        (stream-reported) model when every step agrees, so a defaulted
        spawn's model stays recoverable from the row.

        Args:
            iter_id: Iteration to end.
            status: Final status.
            exit_code: Iteration exit code.
            metadata: Optional short failure reason (e.g. ``timed out``) for
                visibility in ``node activity``; the metadata column is left
                untouched when ``None``.

        Returns:
            Rows transitioned -- 0 when another writer already closed it.

        """
        # validate the row status against the known set
        if status not in STATUSES:
            raise _invalid_status(status)
        # record default agent's session for the iteration (continuous
        # mode only; None when detached or the default agent never ran)
        if agent := self._node._default_agent():
            session = self._node.sessions.get(agent)
        else:
            session = None
        # stamp the terminal only if still open (first-writer-wins)
        now = fractal.util.time.utc_now()
        data = {
            'status': status,
            'exit_code': exit_code,
            'ended_at': now,
        }
        if session is not None:
            data['session'] = session
        # record a reason only when given (don't clobber existing metadata)
        if metadata is not None:
            data['metadata'] = metadata
        # the iteration records the SERVED model: when every step's recorded
        # (stream-reported) model agrees, it wins over the config-seeded pin
        # -- divergence must be visible on the row, not laundered under the
        # pin -- while disagreeing or unreporting steps keep the seed
        iter_rows = self.db.read('iters', where={'iter_id': iter_id}, limit=1)
        if iter_rows:
            steps = self.db.read('steps', where={'iter_id': iter_id})
            models = {row['model'] for row in steps if row['model']}
            if len(models) == 1 and (model := models.pop()) != iter_rows[0]['model']:
                data['model'] = model
        return self._fenced(
            data=data,
            table='iters',
            where={'iter_id': iter_id, 'ended_at': None},
        )

    def step_start(
        self: Record,
        *,
        iter_id: int,
        run_id: int,
        step: int,
        step_name: str,
    ) -> int:
        """Create a step row with ``status='active'``.

        The agent and its real session are recorded later by
        :meth:`step_session`, captured from the agent's output stream (so
        they are set in detached mode too, enabling after-the-fact resume).

        Args:
            iter_id: Parent iteration.
            run_id: Parent run.
            step: Step number within the iteration.
            step_name: Step name (e.g. ``EXECUTE``).

        Returns:
            Step ID.

        """
        now = fractal.util.time.utc_now()
        data = {
            'node': self._node.branch,
            'iter_id': iter_id,
            'run_id': run_id,
            'step': step,
            'step_name': step_name,
            'status': 'active',
            'started_at': now,
        }
        return self.db.write(data, 'steps')

    def step_cost(
        self: Record,
        *,
        step_id: int,
        cost: float,
    ) -> None:
        """Record cost for a step.

        A figure landing on a row already marked ``unpriced`` (the per-frame
        flush racing a kill) strips the stale marker.

        Args:
            step_id: Step to update.
            cost: Cost in USD.

        """
        data = {'cost': cost}
        # drop a stale unpriced marker -- the row carries a real figure now;
        # the read and the write share one transaction, so a close_open stamp
        # landing between them can never strand the marker on a priced row
        with self.db.transaction() as connection:
            rows = self.db.read(
                'steps',
                where={'step_id': step_id},
                connection=connection,
            )
            if rows:
                meta = rows[0]['metadata']
                if meta == 'unpriced':
                    data['metadata'] = ''
                elif meta.endswith('; unpriced'):
                    data['metadata'] = meta[: -len('; unpriced')]
            self.db.update(
                data=data,
                table='steps',
                where={'step_id': step_id},
                connection=connection,
            )

    def step_session(
        self: Record,
        agent: str,
        *,
        step_id: int,
        model: Optional[str],
        session: str,
    ) -> None:
        """Record the agent, model, and real session for a step.

        Captured from the agent's output stream by the stream driver for
        both agents, so it is recorded even in detached mode (enabling
        after-the-fact resume and per-agent cost attribution).

        Args:
            agent: Agent that ran the step (e.g. ``claude`` or ``codex``).
            step_id: Step to update.
            model: The model that actually ran the step -- stream-reported
                where the agent names it, else the configured model
                (frontmatter or node default), or ``None`` when neither
                names one.
            session: Real, agent-specific session.

        """
        data = {'agent': agent, 'model': model, 'session': session}
        self.db.update(data, 'steps', where={'step_id': step_id})

    def step_end(
        self: Record,
        *,
        step_id: int,
        status: str,
        exit_code: int,
        metadata: Optional[str] = None,
    ) -> int:
        """End a step.

        First-writer-wins via the ``ended_at IS NULL`` guard, so a kill
        racing the loop's own end can't overwrite the outcome. Duration is
        derived from ``started_at``/``ended_at``; the ``cost`` column is
        left untouched (recorded separately by :meth:`step_cost`, possibly
        after the step has ended).

        An abnormal end (any terminal but ``completed``) whose row has a
        session but no cost appends ``unpriced`` to its metadata: the agent
        stream opened, so spend plausibly burned before the first usage
        flush -- the marker lets ledgers tell "free step" from "unpriced
        step". Cost stays NULL (``Cost.unpriced`` discloses the gap count);
        a flush that lands later strips the marker.

        Args:
            step_id: Step to end.
            status: Final status.
            exit_code: Agent exit code.
            metadata: Optional short, fractal-owned failure reason (e.g.
                ``timed out``/``agent error``) for visibility in ``node
                activity``; the metadata column is left untouched when ``None``.

        Returns:
            Rows transitioned -- 0 when another writer already closed it.

        """
        # validate the row status against the known set
        if status not in STATUSES:
            raise _invalid_status(status)
        # stamp the terminal only if still open (first-writer-wins)
        now = fractal.util.time.utc_now()
        data = {
            'status': status,
            'exit_code': exit_code,
            'ended_at': now,
        }
        # record a reason only when given (don't clobber existing metadata)
        if metadata is not None:
            data['metadata'] = metadata
        # mark an abnormal end unpriced when the stream opened but no usage
        # flushed -- burn happened with no recorded figure
        if status != 'completed':
            rows = self.db.read('steps', where={'step_id': step_id, 'ended_at': None})
            if rows and rows[0]['cost'] is None and rows[0]['session'] is not None:
                reason = metadata if metadata is not None else rows[0]['metadata']
                data['metadata'] = f'{reason}; unpriced' if reason else 'unpriced'
        return self._fenced(
            data=data,
            table='steps',
            where={'step_id': step_id, 'ended_at': None},
        )

    def step_pending(
        self: Record,
        *,
        step_id: int,
    ) -> None:
        """Mark a step as requiring approval.

        The ``approved`` column has three states: NULL (does
        not require approval), ``''`` (pending approval),
        and an ISO 8601 timestamp (approved). This method
        transitions from NULL to ``''`` -- the guard admits no other
        starting state, so a stray re-pend can never demote an
        approval back to pending.

        The fresh row supersedes any earlier still-pending row of the same
        iteration step (a re-run after an unapproved pause): the stale row
        would otherwise sit in ``pending`` forever, silently swallowing
        approvals aimed at it. Superseding is gated on this row winning
        the mark, and voiding guards on the twin still being pending --
        a stray re-pend can never void another row's live gate, and an
        approval landing between the read and the void survives.

        Args:
            step_id: Step to mark.

        """
        data = {'approved': ''}
        marked = self._fenced(
            data=data,
            table='steps',
            where={'step_id': step_id, 'approved': None},
        )
        if not marked:
            return
        # void superseded pending twins -- only this row's approval counts now
        if rows := self.db.read('steps', where={'step_id': step_id}):
            twins = self.db.read(
                'steps',
                where={
                    'iter_id': rows[0]['iter_id'],
                    'step': rows[0]['step'],
                    'approved': '',
                },
            )
            for twin in twins:
                if twin['step_id'] != step_id:
                    self._fenced(
                        data={'approved': None},
                        table='steps',
                        where={'step_id': twin['step_id'], 'approved': ''},
                    )

    def step_approve(
        self: Record,
        *,
        step_id: int,
    ) -> int:
        """Approve a step (set ``approved`` to the current UTC timestamp).

        The low-level write; the step is validated (exists and requires
        approval) by the sole caller ``Node.child_approve`` before the
        approve event is logged. The stamp is a compare-and-swap on the
        live gate (``approved = ''``): first-approval-wins, so a re-approve
        keeps the original instant and an approval aimed at a superseded
        (voided) gate writes nothing instead of resurrecting it.

        Args:
            step_id: Step to approve.

        Returns:
            Rows transitioned -- 0 when the gate was not pending.

        """
        now = fractal.util.time.utc_now()
        data = {'approved': now}
        return self._fenced(
            data=data,
            table='steps',
            where={'step_id': step_id, 'approved': ''},
        )

    def step_approved(
        self: Record,
        *,
        step_id: int,
    ) -> bool:
        """Check whether a step is approved.

        Returns ``True`` if the step does not require approval
        (``approved`` is NULL) or has been approved (``approved``
        is a timestamp). Returns ``False`` only when the step
        requires approval but has not been approved yet
        (``approved`` is an empty string).

        Args:
            step_id: Step to check.

        Returns:
            Whether the step is approved or requires no approval.

        """
        rows = self.db.read('steps', where={'step_id': step_id}, limit=1)
        if not rows:
            return True
        approved = rows[0].get('approved')
        return approved is None or bool(approved)

    def event_start(
        self: Record,
        event: str,
        *,
        metadata: str = '',
        run_id: Optional[int] = None,
        iter_id: Optional[int] = None,
        step_id: Optional[int] = None,
    ) -> Optional[int]:
        """Log an event.

        A caller that knows the event's lineage passes it explicitly (the
        loop's commit step, for example); otherwise the lineage resolves via
        :meth:`resolve_context` (active-only): ``run_id`` is the active run
        (NULL when none is active -- an event carries a run only if it fired
        inside one), and ``step_id``/``iter_id`` are the in-flight
        step/iteration, so a ``kill`` names the interrupted step. Every row
        stamps its ``actor``: the calling node's branch when the write comes
        from inside a node (loop-written events self-attribute), the
        operator otherwise. No-op if the node is not initialized.

        Args:
            event: Event type (one of ``fractal.constants.EVENTS``).
            metadata: Free-form context string (e.g. a child branch, a
                commit sha, or a reason).
            run_id: Run the event belongs to (skips resolution when any
                lineage id is passed).
            iter_id: Iteration the event belongs to.
            step_id: Step the event belongs to.

        Returns:
            Event ID, or ``None`` if the node is not
            initialized.

        Raises:
            ValueError: ``event`` is not a known type, or the explicit
                lineage does not chain (``step_id`` without ``iter_id``/
                ``run_id``, or ``iter_id`` without ``run_id``).

        """
        # validate the event against the known vocabulary (typo-catcher)
        if event not in EVENTS:
            raise ValueError(f'Invalid event: {event!r}')
        # pin the event to its lineage: explicit ids win wholesale (the
        # chain check rides resolve_context); otherwise resolve the current
        # one (active-only -- a run attaches only when one is active); a
        # kill names the interrupted step
        step_id, iter_id, run_id = self.resolve_context(
            run_id=run_id,
            iter_id=iter_id,
            step_id=step_id,
            active=True,
        )
        # skip if the node is not initialized
        if not self._node.exists():
            return None
        # attribute the write: the calling node's branch when inside a node,
        # the operator otherwise
        caller = self._node.resolve_caller()
        data = {
            'node': self._node.branch,
            'event': event,
            'status': 'active',
            'actor': caller.branch if caller else 'operator',
        }
        if step_id is not None:
            data['step_id'] = step_id
        if iter_id is not None:
            data['iter_id'] = iter_id
        if run_id is not None:
            data['run_id'] = run_id
        if metadata:
            data['metadata'] = metadata
        return self.db.write(data, 'events')

    def event_end(
        self: Record,
        *,
        event_id: int,
        status: str,
        exit_code: Optional[int] = None,
    ) -> None:
        """End an event.

        Events are point-in-time log entries (no ``ended_at``); this just
        records the action's final ``status`` and optional ``exit_code``.

        Args:
            event_id: Event to end.
            status: Final status.
            exit_code: Event exit code.

        """
        # validate the row status against the known set
        if status not in STATUSES:
            raise _invalid_status(status)
        # update event
        data = {'status': status}
        if exit_code is not None:
            data['exit_code'] = exit_code
        self.db.update(data, 'events', where={'event_id': event_id})

    def signal_get(
        self: Record,
        signal: str,
        *,
        run_id: Optional[int] = None,
    ) -> Optional[str]:
        """Return signal metadata, or ``None`` if not set.

        Auto-resolves ``run_id`` from the latest run if
        not provided.

        Args:
            signal: Signal identifier.
            run_id: Run to check. Auto-resolved if omitted.

        Returns:
            Signal metadata string, or ``None``.

        """
        if run_id is None:
            _, _, run_id = self.resolve_context()
        if run_id is None:
            return None
        rows = self.db.read(
            'signals',
            where={'signal': signal, 'run_id': run_id},
            limit=1,
        )
        if rows:
            return rows[0]['metadata']
        return None

    def signal_set(
        self: Record,
        signal: str,
        metadata: str = '',
    ) -> None:
        """Append a signal to the database.

        Resolves ``run_id`` from the latest active run,
        falls back to the most recent run regardless of
        status. No-op if no runs exist.

        Args:
            signal: Signal identifier (``finish``, ``stop``,
                ``kill``, ``pause``, ``exit``).
            metadata: Text payload.

        """
        _, _, run_id = self.resolve_context()
        if run_id is None:
            self._node.log(
                message='Warning: no runs found; signal not set',
                level=logging.WARNING,
            )
            return
        data = {
            'node': self._node.branch,
            'run_id': run_id,
            'signal': signal,
            'metadata': metadata,
        }
        self.db.write(data, 'signals')

    def signal_clear(
        self: Record,
        signal: str,
        *,
        run_id: Optional[int] = None,
    ) -> None:
        """Delete a run's rows for one signal.

        Signals are append-only except the deliberate withdrawals:
        ``finish_cancel``, and the resume boot clearing the ``pause``
        rows that parked the run it adopts (it would otherwise re-park
        on them at its first checkpoint).

        Args:
            signal: Signal identifier.
            run_id: Run to clear. Auto-resolved if omitted.

        """
        if run_id is None:
            _, _, run_id = self.resolve_context()
        if run_id is None:
            return
        self.db.delete(
            'signals',
            where={'node': self._node.branch, 'run_id': run_id, 'signal': signal},
        )

    def runs(
        self: Record,
        *,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[Row]:
        """List this node's run rows, newest first.

        Args:
            status: Include only runs with this status.
            limit: Maximum rows to return.

        Returns:
            Run rows.

        """
        where: dict[str, Any] = {'node': self._node.branch}
        if status is not None:
            where['status'] = status
        return self.db.read('runs', where=where, limit=limit)

    def iters(
        self: Record,
        *,
        run_id: Optional[int] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[Row]:
        """List this node's iteration rows, newest first.

        Args:
            run_id: Include only iterations of this run.
            status: Include only iterations with this status.
            limit: Maximum rows to return.

        Returns:
            Iteration rows.

        """
        where: dict[str, Any] = {'node': self._node.branch}
        if run_id is not None:
            where['run_id'] = run_id
        if status is not None:
            where['status'] = status
        return self.db.read('iters', where=where, limit=limit)

    def steps(
        self: Record,
        *,
        run_id: Optional[int] = None,
        iter_id: Optional[int] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[Row]:
        """List this node's step rows, newest first.

        Args:
            run_id: Include only steps of this run.
            iter_id: Include only steps of this iteration.
            status: Include only steps with this status.
            limit: Maximum rows to return.

        Returns:
            Step rows.

        """
        where: dict[str, Any] = {'node': self._node.branch}
        if run_id is not None:
            where['run_id'] = run_id
        if iter_id is not None:
            where['iter_id'] = iter_id
        if status is not None:
            where['status'] = status
        return self.db.read('steps', where=where, limit=limit)

    def events(
        self: Record,
        *,
        run_id: Optional[int] = None,
        event: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[Row]:
        """List this node's event rows, newest first.

        Args:
            run_id: Include only events of this run.
            event: Include only events of this type.
            status: Include only events with this status.
            limit: Maximum rows to return.

        Returns:
            Event rows.

        """
        where: dict[str, Any] = {'node': self._node.branch}
        if run_id is not None:
            where['run_id'] = run_id
        if event is not None:
            where['event'] = event
        if status is not None:
            where['status'] = status
        return self.db.read('events', where=where, limit=limit)

    def signals(
        self: Record,
        *,
        run_id: Optional[int] = None,
        signal: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[Row]:
        """List this node's signal rows, newest first.

        Args:
            run_id: Include only signals of this run.
            signal: Include only signals with this identifier.
            limit: Maximum rows to return.

        Returns:
            Signal rows.

        """
        where: dict[str, Any] = {'node': self._node.branch}
        if run_id is not None:
            where['run_id'] = run_id
        if signal is not None:
            where['signal'] = signal
        return self.db.read('signals', where=where, limit=limit)

    def activity(
        self: Record,
        *,
        limit: Optional[int] = None,
    ) -> list[Row]:
        """Return this node's activity-view rows, most recent first.

        The ``activity`` view unifies entity start/end rows (with derived
        ``duration`` and rolled-up ``cost`` on the end rows) and the node's
        point-in-time events, each carrying its run/iter/step lineage. The
        LEFT JOINs add the display numbers the surrogate ids stand for --
        the iteration-relative ``step`` number, ``step_name``, and the
        run-relative ``iter``.

        Args:
            limit: Maximum rows to return.

        Returns:
            Activity rows.

        """
        query = (
            'SELECT a.*, s.step AS step, s.step_name AS step_name,'
            ' i.iter AS iter'
            ' FROM activity a'
            ' LEFT JOIN steps s ON a.step_id = s.step_id'
            ' LEFT JOIN iters i ON a.iter_id = i.iter_id'
            ' WHERE a.node = ?'
            ' ORDER BY a.timestamp DESC, a.run_id DESC, a.iter_id DESC,'
            ' a.step_id DESC'
        )
        params: tuple[Any, ...] = (self._node.branch,)
        if limit is not None:
            query += ' LIMIT ?'
            params += (limit,)
        return self.db.read(query=query, params=params)

    def _parent_run_id(self: Record) -> Optional[int]:
        """Resolve the parent node's active run id, for ``run_start`` lineage.

        Reads the central DB -- the only durable channel, since ``RUN_ID`` is
        not in the child's env and the child's ``run_start`` runs in a fresh
        detached loop. Returns the parent's **active** run, or ``None`` when
        this is a root node or the parent is idle -- in which case this run
        belongs to no parent run and is excluded from the parent's per-run
        subtree cost.

        Returns:
            The parent's active run id, or ``None``.

        """
        # root node has no parent
        branch = self._node.branch
        if '.' not in branch:
            return None
        # the parent's active run, straight from the central DB
        parent_branch, *_ = branch.rsplit('.', 1)
        rows = self.db.read(
            'runs',
            where={'node': parent_branch, 'status': 'active'},
            limit=1,
        )
        return rows[0]['run_id'] if rows else None


# ------ helper functions


def _invalid_status(status: str) -> ValueError:
    """Build the unknown-status error (shared across every raise site)."""
    return ValueError(f'Invalid status: {status!r}')
