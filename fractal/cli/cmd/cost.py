"""Implements ``fractal node cost`` sub-app commands."""

from __future__ import annotations

from typing import Optional

import typer

from fractal.cli.utils import (
    command,
    print_rows,
    require_non_negative,
    require_run_owner,
    resolve_ledger_target,
)

__all__ = [
    'cost_remaining',
    'cost_spent',
    'cost_breakdown',
]

_DECIMAL_PRECISION = 4

_BREAKDOWN_COLUMNS = [
    'node',
    'max_cost',
    'spent',
]


def cost_remaining(app: typer.Typer) -> typer.Typer:
    """Register the ``remaining`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # run id option
    run_id_help = (
        'Scope to a run ID (default: the current run; budgets and spend are per-run).'
    )
    run_id = typer.Option(None, '--run', help=run_id_help)
    # iteration id option
    iter_id_help = "Scope to an iteration ID's max-iter-cost headroom."
    iter_id = typer.Option(None, '--iter', help=iter_id_help)
    # step id option
    step_id_help = "Scope to a step ID's max-step-cost headroom."
    step_id = typer.Option(None, '--step', help=step_id_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'remaining')
    def _remaining(
        node: Optional[str] = node,
        run_id: Optional[int] = run_id,
        iter_id: Optional[int] = iter_id,
        step_id: Optional[int] = step_id,
        path: str = path,
    ) -> None:
        """Print remaining cost budget (current run by default; --run for one run).

        Budgets are per-run, so a drained prior run never shrinks the bare
        figure (scope to it via ``--run``; a budget-ended run re-arms only
        through ``node start --continue --max-cost``). A deleted target
        reports ``no budget``: its history persists, but every cap store
        dies with the node.
        """
        if sum(scope is not None for scope in (run_id, iter_id, step_id)) > 1:
            raise typer.BadParameter('Use at most one of --run/--iter/--step.')
        # a deleted target reports the cap-less 'no budget' -- its history
        # persists, but remaining never answers through the caller
        node, deleted, _ = resolve_ledger_target(path, node)
        require_run_owner(node, run_id=run_id, branch=deleted or node.branch)
        if deleted is not None:
            typer.echo('no budget')
            return
        remaining = node.cost.remaining(
            run_id=run_id,
            iter_id=iter_id,
            step_id=step_id,
        )
        if remaining is None:
            typer.echo('no budget')
        else:
            remaining = max(0.0, remaining)
            typer.echo(f'${remaining:.{_DECIMAL_PRECISION}f}')

    return app


def cost_spent(app: typer.Typer) -> typer.Typer:
    """Register the ``spent`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # run id option
    run_id_help = (
        'Scope to a run ID (default: the current run; budgets and spend are per-run).'
    )
    run_id = typer.Option(None, '--run', help=run_id_help)
    # iteration id option
    iter_id_help = 'Scope to a specific iteration ID.'
    iter_id = typer.Option(None, '--iter', help=iter_id_help)
    # step id option
    step_id_help = 'Scope to a specific step ID.'
    step_id = typer.Option(None, '--step', help=step_id_help)
    # max depth option
    max_depth_help = 'Maximum child depth to include (0 = this node only).'
    max_depth = typer.Option(None, '--max-depth', help=max_depth_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'spent')
    def _spent(
        node: Optional[str] = node,
        run_id: Optional[int] = run_id,
        iter_id: Optional[int] = iter_id,
        step_id: Optional[int] = step_id,
        max_depth: Optional[int] = max_depth,
        path: str = path,
    ) -> None:
        """Print total cost (current run by default; --run for one run). Includes children.

        Budgets are per-run, so the bare reading never carries prior-run
        spend (scope to a prior run via ``--run``). A deleted target
        answers from its persisted history -- its latest recorded run by
        default.
        """
        require_non_negative(max_depth=max_depth)
        if sum(scope is not None for scope in (run_id, iter_id, step_id)) > 1:
            raise typer.BadParameter('Use at most one of --run/--iter/--step.')
        # a deleted target answers through the caller (shared db), its latest
        # recorded run standing in for the current one -- core gives run_id
        # precedence over the caller's own scope
        node, deleted, latest = resolve_ledger_target(path, node)
        require_run_owner(node, run_id=run_id, branch=deleted or node.branch)
        if deleted is not None:
            if run_id is None and iter_id is None and step_id is None:
                run_id = latest
        kwargs = {
            'run_id': run_id,
            'iter_id': iter_id,
            'step_id': step_id,
        }
        spent = node.cost.spent(**kwargs, max_depth=max_depth)
        if spent == 0.0 and node.cost.untracked(**kwargs, max_depth=max_depth):
            typer.echo('untracked')
        else:
            typer.echo(f'${spent:.{_DECIMAL_PRECISION}f}')
            # disclose the SUM's silent gap (ended steps with NULL cost:
            # launched steps whose usage never flushed, e.g. kills before
            # the first flush) on stderr; stdout stays parseable
            unpriced = node.cost.unpriced(**kwargs, max_depth=max_depth)
            if unpriced:
                s = 's' if unpriced != 1 else ''
                typer.echo(
                    f'{unpriced} unpriced step{s} (NULL cost) excluded',
                    err=True,
                )

    return app


def cost_breakdown(app: typer.Typer) -> typer.Typer:
    """Register the ``breakdown`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # run id option
    run_id_help = (
        'Scope to a run ID (default: the current run; budgets and spend are per-run).'
    )
    run_id = typer.Option(None, '--run', help=run_id_help)
    # max depth option
    max_depth_help = 'Maximum child depth to include (0 = this node only).'
    max_depth = typer.Option(None, '--max-depth', help=max_depth_help)
    # csv flag
    csv_help = 'Force CSV output (already the default when piped / non-TTY).'
    csv = typer.Option(False, '--csv', help=csv_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'breakdown')
    def _breakdown(
        node: Optional[str] = node,
        run_id: Optional[int] = run_id,
        max_depth: Optional[int] = max_depth,
        csv: bool = csv,
        path: str = path,
    ) -> None:
        """Print per-node cost breakdown (current run by default; --run for one run).

        Budgets are per-run, so the bare table covers the current run only
        (scope to a prior run via ``--run``).

        The target's own row leads (so a leaf node -- and ``--max-depth 0`` --
        attributes this node's own spend), followed by each in-subtree descendant.
        A descendant still registered shows its budget (idle children read 0.00);
        a descendant whose registry row is gone (deleted/reparented) but whose
        spend is still recorded (chained via ``parent_run_id``) is appended as a
        `` (deleted)`` row, so the rows always sum to ``cost spent``. A deleted
        *target* answers from its persisted history the same way: its own
        `` (deleted)`` row leads (no cap survives the delete) over its latest
        recorded run.
        """
        require_non_negative(max_depth=max_depth)
        # a deleted target drives the table through the caller (shared db),
        # its latest recorded run standing in for the current one -- core
        # gives run_id precedence over the caller's own scope
        node, deleted, latest = resolve_ledger_target(path, node)
        require_run_owner(node, run_id=run_id, branch=deleted or node.branch)
        if deleted is not None and run_id is None:
            run_id = latest
        # the display-complete spend table (leads with the target, sums to
        # cost spent) comes from core; rendering stays here
        rows = node.cost.rows(run_id=run_id, max_depth=max_depth, deleted=deleted)
        table = []
        for row in rows:
            branch = row['node']
            if row['deleted']:
                branch = f'{branch} (deleted)'
            display = {
                'node': branch,
                'max_cost': row['max_cost'],
                'spent': round(row['spent'], _DECIMAL_PRECISION),
            }
            table.append(display)
        print_rows(table, csv=csv, columns=_BREAKDOWN_COLUMNS)
        # disclose the SUM's silent gap across the same scope the table
        # sums (ended NULL-cost steps) -- stderr keeps the table parseable
        unpriced = node.cost.unpriced(
            run_id=run_id,
            max_depth=max_depth,
        )
        if unpriced:
            s = 's' if unpriced != 1 else ''
            typer.echo(
                f'{unpriced} unpriced step{s} (NULL cost) excluded',
                err=True,
            )

    return app
