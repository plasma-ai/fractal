"""Implements ``Cost`` class."""

from __future__ import annotations

import typing
from typing import Optional

if typing.TYPE_CHECKING:
    import sqlite3

    from fractal.typing import Row

    from .db import Database
    from .node import Node

__all__ = []


class Cost:
    """Cost ledger readers over config caps and step-cost rows."""

    def __init__(self: Cost, node: Node) -> None:
        """Initialize ``Cost``.

        Args:
            node: The owning ``Node`` instance.

        """
        self._node = node

    @property
    def node(self: Cost) -> Node:
        """Return the owning node."""
        return self._node

    @property
    def db(self: Cost) -> Database:
        """Return the central database."""
        return self._node.db

    def remaining(
        self: Cost,
        *,
        run_id: Optional[int] = None,
        iter_id: Optional[int] = None,
        step_id: Optional[int] = None,
    ) -> Optional[float]:
        """Compute remaining cost budget for a run, iteration, or step.

        ``--max-cost`` is a **per-run** ceiling: by default this returns
        ``max_cost`` minus the current run's subtree spend (own steps plus
        descendants chained by ``parent_run_id``; the active run, else the most
        recent), so a budget drained in one run is fresh again in the next. Pass
        ``run_id`` for a specific run. ``iter_id``/``step_id`` instead
        scope to the matching per-level cap (``max_iter_cost``/``max_step_cost``).
        Reflects recorded step costs only -- the active step counts whatever
        cost is already flushed to its row, not its unflushed in-progress
        accrual. The run budget is **subtree-shared
        with no reserved self-slice**: a manager that sizes children to its full
        remaining leaves itself none and can be starved out of its own merge-up
        iteration -- size children below the remaining if the manager must keep
        working after spawning.

        Args:
            run_id: A run to scope to; the current run if omitted.
            iter_id: Scope to an iteration's ``max_iter_cost`` headroom.
            step_id: Scope to a step's ``max_step_cost`` headroom.

        Returns:
            Remaining cost in USD, or ``None`` if the relevant cap is not
            configured (``max_cost``/``max_iter_cost``/``max_step_cost``).

        """
        # iteration/step scope -> the matching per-level cap minus its spend
        if step_id is not None:
            max_step_cost = self._node.config.get('max_step_cost')
            if max_step_cost is None:
                return None
            return float(max_step_cost) - self.spent(step_id=step_id)
        if iter_id is not None:
            max_iter_cost = self._node.config.get('max_iter_cost')
            if max_iter_cost is None:
                return None
            return float(max_iter_cost) - self.spent(iter_id=iter_id)
        # run scope -> max_cost minus the run's subtree spend (own steps plus
        # descendants, chained by parent_run_id) so remaining tracks the whole
        # subtree's headroom, not just this node's direct steps
        max_cost = self._node.config.get('max_cost')
        if max_cost is None:
            return None
        result = float(max_cost)
        # resolve the run: current (active, else most recent) unless given;
        # a never-run node has nothing to subtract -> the full budget
        if run_id is None:
            _, _, run_id = self._node.record.resolve_context()
        if run_id is None:
            return result
        result -= self.spent(run_id=run_id)
        return result

    def spent(
        self: Cost,
        *,
        run_id: Optional[int] = None,
        iter_id: Optional[int] = None,
        step_id: Optional[int] = None,
        max_depth: Optional[int] = None,
    ) -> float:
        """Return the total cost for a run, iteration, step, or per-run subtree.

        Sums the current run's direct step cost by default (the active run, else
        the most recent) -- the per-run reading behind ``--run`` scoping. Pass
        ``run_id`` for a specific run. Includes child node costs: a child counts
        only the runs it spawned under this run's lineage, chained via
        ``parent_run_id`` (the per-run subtree) -- a deleted child's recorded
        runs still count. Pass ``max_depth=0`` for this node only,
        ``max_depth=1`` to include children, etc.

        When ``iter_id`` (or ``step_id``) is given, returns cost
        for that iteration (or step) only (children are not included).

        Args:
            run_id: A run to scope to; the current run if omitted.
            iter_id: Scope to a specific iteration.
            step_id: Scope to a specific step.
            max_depth: Maximum child depth to include.
                ``None`` includes all descendants,
                ``0`` is direct cost only.

        Returns:
            Cost in USD (0.0 if no data).

        """
        # sum a single step's cost (children not applicable)
        if step_id is not None:
            query = (
                'SELECT COALESCE(SUM(cost), 0) AS total FROM steps WHERE step_id = ?'
            )
            rows = self.db.read(query=query, params=(step_id,))
            return rows[0]['total'] if rows else 0.0
        # sum iteration steps (children not applicable)
        if iter_id is not None:
            query = (
                'SELECT COALESCE(SUM(cost), 0) AS total FROM steps WHERE iter_id = ?'
            )
            rows = self.db.read(query=query, params=(iter_id,))
            return rows[0]['total'] if rows else 0.0
        # sum the per-run subtree's step cost (a never-run node has spent nothing)
        row = self._scoped(
            'COALESCE(SUM(s.cost), 0) AS total',
            run_id=run_id,
            max_depth=max_depth,
        )
        return row['total'] if row else 0.0

    def untracked(
        self: Cost,
        *,
        run_id: Optional[int] = None,
        iter_id: Optional[int] = None,
        step_id: Optional[int] = None,
        max_depth: Optional[int] = None,
    ) -> bool:
        """Return whether a scope's spend is untracked rather than genuinely zero.

        A token-priced agent with no priced model records ``NULL`` cost,
        so its spend sums to ``0`` yet is not actually ``$0``. Returns ``True``
        when the scope has unknowable (``NULL``-cost) steps and none recorded
        a real figure, letting the CLI show ``untracked`` instead of ``$0`` so
        a parent can tell "spent nothing" from "untrackable". Zero-cost rows
        (never-launched steps, and a flushed genuine $0) are neutral: they
        prove nothing spent, not that spend was tracked. The run scope mirrors
        ``spent``: it walks the per-run subtree (to ``max_depth``) so a
        fully-untracked child reads as untracked at the parent, not as ``$0``
        -- a mixed subtree (any real figure) is tracked. The iteration/step
        scope is own steps only (no children).

        Args:
            run_id: A run to scope to; the current run if omitted.
            iter_id: Scope to a specific iteration.
            step_id: Scope to a specific step.
            max_depth: Maximum child depth to include for the run scope
                (``None`` all descendants, ``0`` this node only).

        Returns:
            ``True`` if the scope has ``NULL``-cost steps and no real figure.

        """
        # iteration/step scope: own steps only (children not applicable,
        # mirroring spent) -- one COUNT over the matching steps
        if step_id is not None or iter_id is not None:
            if step_id is not None:
                where, param = 'step_id = ?', step_id
            else:
                where, param = 'iter_id = ?', iter_id
            query = (
                'SELECT COUNT(*) - COUNT(cost) AS unknown,'
                ' COUNT(CASE WHEN cost > 0 THEN 1 END) AS figured'
                f' FROM steps WHERE {where}'
            )
            if rows := self.db.read(query=query, params=(param,)):
                return rows[0]['unknown'] > 0 and rows[0]['figured'] == 0
            return False
        # run scope: count the per-run subtree's steps like spent so an
        # untracked child's NULL-cost steps aren't read as a genuine $0
        row = self._scoped(
            'COUNT(*) - COUNT(s.cost) AS unknown,'
            ' COUNT(CASE WHEN s.cost > 0 THEN 1 END) AS figured',
            run_id=run_id,
            max_depth=max_depth,
        )
        if row:
            return row['unknown'] > 0 and row['figured'] == 0
        return False

    def unpriced(
        self: Cost,
        *,
        run_id: Optional[int] = None,
        iter_id: Optional[int] = None,
        step_id: Optional[int] = None,
        max_depth: Optional[int] = None,
    ) -> int:
        """Count a scope's ended steps that recorded no cost.

        ``SUM()`` skips NULL-cost rows without a trace, so ledger-facing
        readings disclose this count when nonzero. NULL means the spend is
        unknowable -- a launched step whose usage never flushed: kills before
        the first usage flush (marked ``unpriced`` on their metadata),
        post-launch pre-stream deaths, and untracked-agent rows. A step the
        loop closes without reaching its launch records an explicit zero
        instead (out-of-band crash heals still land here: a healed
        pre-launch row is indistinguishable from a launched pre-stream
        death). Ended rows only -- an open step is merely not priced *yet*.
        Scopes mirror ``spent``.

        Args:
            run_id: A run to scope to; the current run if omitted.
            iter_id: Scope to a specific iteration.
            step_id: Scope to a specific step.
            max_depth: Maximum child depth to include for the run scope
                (``None`` all descendants, ``0`` this node only).

        Returns:
            Number of ended steps in the scope with ``NULL`` cost.

        """
        # iteration/step scope: own steps only (children
        # not applicable, mirroring spent)
        if step_id is not None or iter_id is not None:
            if step_id is not None:
                where, param = 'step_id = ?', step_id
            else:
                where, param = 'iter_id = ?', iter_id
            query = (
                'SELECT COUNT(*) AS unpriced FROM steps'
                f' WHERE {where} AND ended_at IS NOT NULL AND cost IS NULL'
            )
            rows = self.db.read(query=query, params=(param,))
            return rows[0]['unpriced'] if rows else 0
        # run scope: count the per-run subtree's rows like spent
        row = self._scoped(
            'COUNT(*) AS unpriced',
            run_id=run_id,
            max_depth=max_depth,
            where=' WHERE s.ended_at IS NOT NULL AND s.cost IS NULL',
        )
        return row['unpriced'] if row else 0

    def breakdown(
        self: Cost,
        *,
        run_id: Optional[int] = None,
        max_depth: Optional[int] = None,
    ) -> dict[str, float]:
        """Map each in-subtree descendant branch to its own spend in a run.

        Walks the ``parent_run_id`` chain from ``run_id`` (the current run --
        active, else most recent -- when omitted): a branch maps to its own
        step cost across the runs it spawned, directly or transitively, under
        that run -- a deleted descendant's recorded runs still count. Branches
        with no run in the lineage are absent (callers treat a missing branch
        as ``0``). Powers ``fractal node cost breakdown``.

        Args:
            run_id: Run to scope to; the current run if omitted.
            max_depth: Maximum descendant depth to include (``1`` = direct
                children only); all descendants if omitted.

        Returns:
            ``{branch: own cost in USD}`` for each in-subtree descendant.

        """
        if run_id is None:
            _, _, run_id = self._node.record.resolve_context()
        if run_id is None:
            return {}
        # group each lineage run's own step cost by its node; depth 0 is this
        # run itself, so the descendants are the depth > 0 rows
        cte, params = self._lineage(run_id, max_depth)
        query = (
            f'{cte}'
            ' SELECT lineage.node AS node, COALESCE(SUM(s.cost), 0) AS total'
            ' FROM lineage LEFT JOIN steps s ON s.run_id = lineage.run_id'
            ' WHERE lineage.depth > 0'
            ' GROUP BY lineage.node'
        )
        rows = self.db.read(query=query, params=params)
        return {row['node']: row['total'] for row in rows}

    def lifetime(
        self: Cost,
        *,
        max_depth: Optional[int] = None,
    ) -> dict[str, float]:
        """Map this node and each in-subtree descendant to its lifetime spend.

        Unlike :meth:`breakdown`, which scopes to a single run's
        ``parent_run_id`` lineage, this sums every step's cost across all of a
        node's runs -- idle-spawned runs no parent run links to included --
        and always covers the node itself, so one call replaces a per-node
        fan-out over a whole subtree. Totals are per branch name and span
        re-inits of the same name (history rows persist by design); a deleted
        descendant holds no registry row and so does not key.

        Args:
            max_depth: Maximum descendant depth to include (``1`` = direct
                children only); all descendants if omitted.

        Returns:
            ``{branch: lifetime own cost in USD}`` for this node and each
            in-subtree descendant.

        """
        # one grouped read over the tree-central steps table; the registry
        # projection scopes it to this subtree
        branches = {self._node.branch}
        for row in self._node.child_list(max_depth=max_depth) or []:
            branches.add(row['node'])
        query = 'SELECT node, COALESCE(SUM(cost), 0) AS total FROM steps GROUP BY node'
        totals = {row['node']: row['total'] for row in self.db.read(query=query)}
        return {branch: totals.get(branch, 0.0) for branch in branches}

    def rows(
        self: Cost,
        *,
        run_id: Optional[int] = None,
        max_depth: Optional[int] = None,
        deleted: Optional[str] = None,
    ) -> list[Row]:
        """Return the display-complete per-branch spend table for one run lineage.

        Rows are ``{'node', 'max_cost', 'spent', 'deleted'}``: the target's
        own row leads (its cap from config; the whole answer for a leaf and
        for ``max_depth=0``), each still-registered descendant follows with
        its cap (idle children spend ``0.0``), then any lineage descendant
        whose registry row is gone (``max_cost`` ``None``, ``deleted``
        ``True``) -- its spend would otherwise vanish from the table while
        still counting in :meth:`spent`, so the rows always sum to it. When
        ``deleted`` names a deleted target (the caller resolves for it), the
        lead row is that branch with no cap and no registered-children rows
        are emitted -- delete clears the whole subtree's registry rows, the
        lineage carries the rest.

        Args:
            run_id: Run to scope to; the current run if omitted.
            max_depth: Maximum descendant depth to include (``1`` = direct
                children only); all descendants if omitted.
            deleted: A deleted target branch to lead the table with.

        Returns:
            Spend rows summing to the scope's :meth:`spent`.

        """
        # delete clears the whole subtree's registry rows, so a deleted target
        # has no cap and no registered children -- the lineage carries the rest
        if deleted:
            branch, max_cost, children = deleted, None, []
            lead_deleted = True
        else:
            branch, max_cost = self._node.branch, self._node.config.get('max_cost')
            children = self._node.child_list(max_depth=max_depth)
            if children is None:
                raise RuntimeError('No database.')
            lead_deleted = False
        # per-descendant own spend in the run lineage; this is the same set
        # spent sums, so the rows below total to it
        breakdown = self.breakdown(run_id=run_id, max_depth=max_depth)
        # lead with the target's own depth-0 spend, which the descendant-only
        # breakdown drops -- the whole answer for a leaf, and what makes
        # max_depth=0 yield exactly this node
        row = {
            'node': branch,
            'max_cost': max_cost,
            'spent': self.spent(run_id=run_id, max_depth=0),
            'deleted': lead_deleted,
        }
        rows = [row]
        # each still-registered descendant, with its cap (idle children = 0.0)
        registered = set()
        for child in children:
            registered.add(child['node'])
            row = {
                'node': child['node'],
                'max_cost': child.get('max_cost'),
                'spent': breakdown.get(child['node'], 0.0),
                'deleted': False,
            }
            rows.append(row)
        # then any lineage descendant whose registry row is gone
        for lineage_branch, lineage_spent in breakdown.items():
            if lineage_branch not in registered:
                row = {
                    'node': lineage_branch,
                    'max_cost': None,
                    'spent': lineage_spent,
                    'deleted': True,
                }
                rows.append(row)
        return rows

    @staticmethod
    def _lineage(
        run_id: int,
        max_depth: Optional[int],
    ) -> tuple[str, tuple[int, ...]]:
        """Build the recursive per-run subtree CTE rooted at ``run_id``.

        ``lineage`` carries ``(run_id, node, depth)`` for the run and every
        descendant run chained to it via ``parent_run_id`` -- each hop is one
        node level -- bounded to ``max_depth`` hops when given.

        Returns:
            The ``WITH RECURSIVE`` prefix and its bound parameters.

        """
        if max_depth is not None:
            depth_guard = ' WHERE lineage.depth < ?'
            params = (run_id, max_depth)
        else:
            depth_guard = ''
            params = (run_id,)
        cte = (
            'WITH RECURSIVE lineage(run_id, node, depth) AS ('
            ' SELECT run_id, node, 0 FROM runs WHERE run_id = ?'
            ' UNION ALL'
            ' SELECT runs.run_id, runs.node, lineage.depth + 1'
            ' FROM runs JOIN lineage ON runs.parent_run_id = lineage.run_id'
            f'{depth_guard}'
            ')'
        )
        return cte, params

    def _scoped(
        self: Cost,
        select: str,
        *,
        run_id: Optional[int],
        max_depth: Optional[int],
        where: str = '',
    ) -> Optional[Row]:
        """Run one aggregate over the per-run subtree (the shared run-scope shell).

        Resolves the run scope -- current (active, else most recent) unless
        given -- and joins ``steps`` against the ``parent_run_id`` lineage
        CTE: the run plus every descendant run chained to it (each hop is one
        node level).

        Args:
            select: Aggregate SELECT list over the joined ``steps s`` rows.
            run_id: A run to scope to; the current run if omitted.
            max_depth: Maximum child depth to include (``None`` all
                descendants, ``0`` this node only).
            where: Optional ``WHERE`` tail over the joined rows.

        Returns:
            The single aggregate row, or ``None`` when no run resolves.

        """
        # resolve the run scope: current (active, else most recent) unless given
        if run_id is None:
            _, _, run_id = self._node.record.resolve_context()
        if run_id is None:
            return None
        # aggregate over the per-run subtree: the run plus every descendant
        # run chained to it by parent_run_id
        cte, params = self._lineage(run_id, max_depth)
        query = (
            f'{cte}'
            f' SELECT {select}'
            ' FROM steps s JOIN lineage ON s.run_id = lineage.run_id'
            f'{where}'
        )
        rows = self.db.read(query=query, params=params)
        return rows[0] if rows else None


def run_spend(
    costs: dict[int, tuple[Optional[int], float]],
    steps: dict[int, list[tuple[float, float]]],
    run_id: int,
    until: Optional[float] = None,
) -> float:
    """Return a run's subtree spend as of an instant, over prefetched plain rows.

    The in-memory twin of :meth:`Cost.spent`'s recursive SQL walk (kept
    adjacent so the pairing is one-file-visible): the run's own steps ended
    by the instant, plus every descendant run chained to it via
    ``parent_run_id`` -- each hop is one node level. Open rows read as
    all-time. Pure -- no database access.

    Args:
        costs: ``{run_id: (parent_run_id, own step-cost sum)}`` across the
            subtree (the chain substrate).
        steps: ``{run_id: [(ended_epoch, cost), ...]}`` for costed steps.
        run_id: The run to total.
        until: Epoch instant to total as of; all time if omitted.

    Returns:
        Subtree spend in USD as of ``until``.

    """
    # the run's own steps, ended by the instant
    own = steps.get(run_id, [])
    total = sum(cost for ended, cost in own if until is None or ended <= until)
    # plus every descendant run chained to this one via parent_run_id
    for child_run_id, chain in costs.items():
        parent_run_id, _ = chain
        if parent_run_id == run_id:
            total += run_spend(costs, steps, child_run_id, until)
    return total


def subtree_spent(connection: sqlite3.Connection, run_id: int) -> float:
    """Return a run's per-run-subtree step cost via the ``parent_run_id`` walk.

    The DB-authoritative all-time counterpart to :func:`run_spend`: it walks
    the persisted ``runs`` table, so a deleted or orphaned descendant's runs
    still count -- matching :meth:`Cost.spent`, the figure budget enforcement
    reads. Runs on any read-only connection.

    Args:
        connection: Open connection to read through.
        run_id: The run to total.

    Returns:
        Subtree spend in USD.

    """
    cte, params = Cost._lineage(run_id, None)
    query = (
        f'{cte}'
        ' SELECT COALESCE(SUM(s.cost), 0) AS total'
        ' FROM steps s JOIN lineage ON s.run_id = lineage.run_id'
    )
    row = connection.execute(query, params).fetchone()
    return row[0] if row else 0.0
