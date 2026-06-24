"""Implements ``fractal node`` sub-app commands."""

from __future__ import annotations

import os
import pathlib
import sys
from typing import Optional

import typer

from fractal.cli.utils import (
    command,
    parse_reserve_budget,
    print_rows,
    require_non_negative,
    resolve_init_target,
    resolve_node,
    resolve_target,
    validate_config_values,
)
from fractal.core.node import Node

__all__ = [
    'node_init',
    'node_start',
    'node_finish',
    'node_stop',
    'node_kill',
    'node_merge',
    'node_delete',
    'node_retire',
    'node_unretire',
    'node_reset',
    'node_attach',
    'node_status',
    'node_list',
    'node_activity',
    'node_approve',
    'node_pending',
    'node_chat',
    'node_update',
    'node_render',
]

_ACTIVITY_COLUMNS = [
    'timestamp',
    'node',
    'event_id',
    'step_id',
    'iter_id',
    'run_id',
    'event',
    'status',
    'exit_code',
    'metadata',
    'duration',
    'cost',
]

_PENDING_COLUMNS = [
    'branch',
    'step_id',
    'step',
    'step_name',
]


def node_init(app: typer.Typer) -> typer.Typer:
    """Register the ``init`` command."""
    # name argument
    name_help = 'Node name.'
    name = typer.Argument(..., help=name_help)
    # path option
    path_help = 'Project root: repo root or monorepo sub-project.'
    path = typer.Option('.', '--path', help=path_help)
    # title option
    title_help = 'Human-readable display name (default: de-slugged node name).'
    title = typer.Option(None, '--title', help=title_help)
    # scope option
    scope_help = 'Subdirectory scope within the worktree.'
    scope = typer.Option(None, '--scope', help=scope_help)
    # base option
    base_help = 'Branch to start from; also the squash-merge target when set.'
    base = typer.Option(None, '--base', help=base_help)
    # meta option
    meta_help = 'Target node branch for meta-configuration.'
    meta = typer.Option(None, '--meta', help=meta_help)
    # agent option
    agent_help = 'Agent command (default: inherited from the nearest ancestor).'
    agent = typer.Option(None, '--agent', help=agent_help)
    # model option
    model_help = 'Model override (passed to agent CLI via --model).'
    model = typer.Option(None, '--model', help=model_help)
    # max-iters option
    max_iters_help = 'Per-run iteration cap (default: unlimited).'
    max_iters = typer.Option(None, '--max-iters', help=max_iters_help)
    # max-depth option
    max_depth_help = 'Maximum child node nesting depth (default: unlimited).'
    max_depth = typer.Option(None, '--max-depth', help=max_depth_help)
    # max-children option
    max_children_help = 'Maximum direct child nodes (default: unlimited).'
    max_children = typer.Option(None, '--max-children', help=max_children_help)
    # max-descendants option
    max_descendants_help = 'Maximum total descendant nodes (default: unlimited).'
    max_descendants = typer.Option(None, '--max-descendants', help=max_descendants_help)
    # timeout option
    timeout_help = 'Whole-run time budget (e.g. 30s, 10m, 1.5h).'
    timeout = typer.Option(None, '--timeout', help=timeout_help)
    # iter-timeout option
    iter_timeout_help = 'Per-iteration time budget (e.g. 30s, 10m, 1.5h).'
    iter_timeout = typer.Option(None, '--iter-timeout', help=iter_timeout_help)
    # step-timeout option
    step_timeout_help = 'Per-step time budget (e.g. 30s, 10m); caps each step.'
    step_timeout = typer.Option(None, '--step-timeout', help=step_timeout_help)
    # interval option
    interval_help = 'Fixed iteration schedule (e.g. 30m, 1h).'
    interval = typer.Option(None, '--interval', help=interval_help)
    # sleep option
    sleep_help = 'Delay between iterations (e.g. 10s, 5m).'
    sleep = typer.Option(None, '--sleep', help=sleep_help)
    # wait option
    wait_help = 'Sleep between approval-wait sync invocations (default: 1s).'
    wait = typer.Option(None, '--wait', help=wait_help)
    # max-cost option
    max_cost_help = 'Maximum cost per run in USD.'
    max_cost = typer.Option(None, '--max-cost', help=max_cost_help)
    # max-iter-cost option
    max_iter_cost_help = 'Maximum cost per iteration in USD.'
    max_iter_cost = typer.Option(None, '--max-iter-cost', help=max_iter_cost_help)
    # max-step-cost option
    max_step_cost_help = 'Maximum cost per step in USD (warn-only when unenforceable).'
    max_step_cost = typer.Option(None, '--max-step-cost', help=max_step_cost_help)
    # reserve-budget option
    reserve_budget_help = (
        'Budget reserved for cleanup; USD or N% of --max-cost (default: 10%).'
    )
    reserve_budget = typer.Option(None, '--reserve-budget', help=reserve_budget_help)
    # sync flag
    sync_help = 'Run sync mode before each step (default: enabled).'
    sync = typer.Option(None, '--sync/--no-sync', help=sync_help)
    # local flag
    local_help = "Skip pushing to remote after each iteration's commit."
    local = typer.Option(None, '--local/--no-local', help=local_help)
    # detached flag
    detached_help = 'Run each step as a separate invocation (default: continuous).'
    detached = typer.Option(False, '--detached', help=detached_help)
    # reset flag
    reset_help = 'Delete node files and reinitialize.'
    reset = typer.Option(False, '--reset', help=reset_help)

    @command(app, 'init')
    def _init(
        name: str = name,
        path: str = path,
        title: Optional[str] = title,
        scope: Optional[str] = scope,
        base: Optional[str] = base,
        meta: Optional[str] = meta,
        agent: Optional[str] = agent,
        model: Optional[str] = model,
        max_iters: Optional[int] = max_iters,
        max_depth: Optional[int] = max_depth,
        max_children: Optional[int] = max_children,
        max_descendants: Optional[int] = max_descendants,
        timeout: Optional[str] = timeout,
        iter_timeout: Optional[str] = iter_timeout,
        step_timeout: Optional[str] = step_timeout,
        interval: Optional[str] = interval,
        sleep: Optional[str] = sleep,
        wait: Optional[str] = wait,
        max_cost: Optional[float] = max_cost,
        max_iter_cost: Optional[float] = max_iter_cost,
        max_step_cost: Optional[float] = max_step_cost,
        reserve_budget: Optional[str] = reserve_budget,
        sync: Optional[bool] = sync,
        local: Optional[bool] = local,
        detached: bool = detached,
        reset: bool = reset,
    ) -> None:
        """Create an agent node."""
        # a per-iter/step cap with no run ceiling can't be enforced
        # (once the per-iter budget drains, later steps run unbounded)
        # -- reject at creation so the operator fixes it now, not at
        # runtime; no cost flags at all is allowed and runs uncapped
        if max_cost is None:
            if max_iter_cost is not None:
                raise typer.BadParameter('--max-iter-cost requires --max-cost.')
            if max_step_cost is not None:
                raise typer.BadParameter('--max-step-cost requires --max-cost.')
        require_non_negative(
            max_iters=max_iters,
            max_depth=max_depth,
            max_children=max_children,
            max_descendants=max_descendants,
            max_cost=max_cost,
            max_iter_cost=max_iter_cost,
            max_step_cost=max_step_cost,
        )
        validate_config_values(
            {
                'max_cost': max_cost,
                'max_iter_cost': max_iter_cost,
                'max_step_cost': max_step_cost,
                'timeout': timeout,
                'iter_timeout': iter_timeout,
                'step_timeout': step_timeout,
                'interval': interval,
                'sleep': sleep,
                'wait': wait,
            }
        )
        reserve_budget = parse_reserve_budget(reserve_budget, max_cost)
        node, _ = resolve_init_target(path)
        output = node.init(
            name=name,
            title=title,
            scope=scope,
            base=base,
            meta=meta,
            agent=agent,
            model=model,
            max_iters=max_iters,
            max_depth=max_depth,
            max_children=max_children,
            max_descendants=max_descendants,
            timeout=timeout,
            iter_timeout=iter_timeout,
            step_timeout=step_timeout,
            interval=interval,
            sleep=sleep,
            wait=wait,
            max_cost=max_cost,
            max_iter_cost=max_iter_cost,
            max_step_cost=max_step_cost,
            reserve_budget=reserve_budget,
            sync=sync,
            local=local,
            detached=detached,
            reset=reset,
        )
        if output:
            typer.echo(output)
        # a manual init from inside a worktree (no _NODE caller context) nests the
        # new node under the root user node, not the worktree's node -- surface it
        # so it is not a silent surprise (an agent loop always sets _NODE)
        worktrees = (node._repo_dir / '.worktrees').resolve()
        in_worktree = worktrees in pathlib.Path.cwd().resolve().parents
        if in_worktree and not os.environ.get('_NODE'):
            typer.echo(
                f"Note: nested '{name}' at the top level (under the root node) --"
                " manual init does not nest under the current worktree's node.",
                err=True,
            )

    return app


def node_start(app: typer.Typer) -> typer.Typer:
    """Register the ``start`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # resume flag
    resume_help = 'Resume a stopped/exited node (continue iterations).'
    resume = typer.Option(False, '--resume', help=resume_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'start')
    def _start(
        node: Optional[str] = node,
        resume: bool = resume,
        path: str = path,
    ) -> None:
        """Launch a node in a tmux session.

        Run parameters come from ``config.json`` (set at init or
        edited before launch); only ``--resume`` is set here.
        """
        node = resolve_target(path, node)
        output = node.start(resume=resume)
        if output:
            typer.echo(output)

    return app


def node_finish(app: typer.Typer) -> typer.Typer:
    """Register the ``finish`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # reason option
    reason_help = 'Optional reason for finishing.'
    reason = typer.Option(None, '--reason', help=reason_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'finish')
    def _finish(
        node: Optional[str] = node,
        reason: Optional[str] = reason,
        path: str = path,
    ) -> None:
        """Stop after the current iteration."""
        node = resolve_target(path, node)
        result = node.finish(reason)
        typer.echo(result)

    return app


def node_stop(app: typer.Typer) -> typer.Typer:
    """Register the ``stop`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # reason option
    reason_help = 'Optional reason for stopping.'
    reason = typer.Option(None, '--reason', help=reason_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'stop')
    def _stop(
        node: Optional[str] = node,
        reason: Optional[str] = reason,
        path: str = path,
    ) -> None:
        """Stop after the current step."""
        node = resolve_target(path, node)
        result = node.stop(reason)
        typer.echo(result)

    return app


def node_kill(app: typer.Typer) -> typer.Typer:
    """Register the ``kill`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # reason option
    reason_help = 'Optional reason for killing.'
    reason = typer.Option(None, '--reason', help=reason_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'kill')
    def _kill(
        node: Optional[str] = node,
        reason: Optional[str] = reason,
        path: str = path,
    ) -> None:
        """Kill a node immediately."""
        node = resolve_target(path, node)
        result = node.kill(reason)
        typer.echo(result)

    return app


def node_merge(app: typer.Typer) -> typer.Typer:
    """Register the ``merge`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'merge')
    def _merge(
        node: Optional[str] = node,
        path: str = path,
    ) -> None:
        """Squash-merge a node's branch into its parent."""
        node = resolve_target(path, node)
        output = node.merge()
        if output:
            typer.echo(output)

    return app


def node_delete(app: typer.Typer) -> typer.Typer:
    """Register the ``delete`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # force flag
    force_help = 'Skip confirmation prompt.'
    force = typer.Option(False, '--force', '-f', help=force_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'delete')
    def _delete(
        node: Optional[str] = node,
        force: bool = force,
        path: str = path,
    ) -> None:
        """Recursively remove a node's subtree and delete its branches."""
        # resolve target node
        try:
            node = resolve_target(path, node)
        except typer.BadParameter:
            # an orphan (worktree removed out of band) resolves as no live node;
            # node is still the branch string here (the resolve above raised), so
            # with --force, deregister it from the registry by branch or bare name
            if force and node:
                caller = resolve_node(path)
                matches = []
                if rows := caller.child_list():
                    for row in rows:
                        *_, name = row['node'].rsplit('.', 1)
                        if row['node'] == node or name == node:
                            matches.append(row['node'])
                if len(matches) == 1:
                    typer.echo(caller.deregister(matches[0]))
                    return
            raise
        if not force:
            descendants = len(node.child_list())
            if descendants:
                s = 's' if descendants != 1 else ''
                prompt = (
                    f'Delete node {node._branch} and its {descendants} descendant{s}?'
                )
            else:
                prompt = f'Delete node {node._branch}?'
            typer.echo(
                'Warning: This permanently removes the worktree(s) and deletes'
                ' the branch(es).\nConsider retiring the node to hide it while'
                ' preserving its branch(es).',
                err=True,
            )
            typer.confirm(prompt, abort=True)
        output = node.delete()
        if output:
            typer.echo(output)

    return app


def node_retire(app: typer.Typer) -> typer.Typer:
    """Register the ``retire`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'retire')
    def _retire(
        node: Optional[str] = node,
        path: str = path,
    ) -> None:
        """Mark a node as retired."""
        node = resolve_target(path, node)
        result = node.retire()
        typer.echo(result)

    return app


def node_unretire(app: typer.Typer) -> typer.Typer:
    """Register the ``unretire`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'unretire')
    def _unretire(
        node: Optional[str] = node,
        path: str = path,
    ) -> None:
        """Remove retired flag from a node."""
        node = resolve_target(path, node)
        result = node.unretire()
        typer.echo(result)

    return app


def node_reset(app: typer.Typer) -> typer.Typer:
    """Register the ``reset`` command."""
    # force flag
    force_help = 'Delete remaining worktrees before resetting.'
    force = typer.Option(False, '--force', '-f', help=force_help)
    # path option
    path_help = 'Worktree directory or repo root.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'reset')
    def _reset(
        force: bool = force,
        path: str = path,
    ) -> None:
        """Remove all worktrees and clean up .worktrees/."""
        # reset is a repo-wide teardown -- resolve to the repo root from any
        # cwd inside it (the agent's NODE_DIR, a worktree, or the repo root)
        repo_root = Node(path)._repo_dir
        output = Node.reset(repo_root, force=force)
        if output:
            typer.echo(output)

    return app


def node_attach(app: typer.Typer) -> typer.Typer:
    """Register the ``attach`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'attach')
    def _attach(
        node: Optional[str] = node,
        path: str = path,
    ) -> None:
        """Attach to a node's tmux session."""
        node = resolve_target(path, node)
        node.attach()

    return app


def node_status(app: typer.Typer) -> typer.Typer:
    """Register the ``status`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'status')
    def _status(
        node: Optional[str] = node,
        path: str = path,
    ) -> None:
        """Show a node's current status."""
        node = resolve_target(path, node)
        typer.echo(node.status_display())

    return app


def node_list(app: typer.Typer) -> typer.Typer:
    """Register the ``list`` command."""
    # node argument
    node_help = "Target node branch (default: this node's descendants)."
    node = typer.Argument(None, help=node_help)
    # all flag
    all_help = 'Include retired nodes.'
    all_nodes = typer.Option(False, '--all', help=all_help)
    # retired flag
    retired_help = 'Show only retired nodes.'
    retired = typer.Option(False, '--retired', help=retired_help)
    # max-depth option
    max_depth_help = 'Maximum child depth to include (1 = direct children only).'
    max_depth = typer.Option(None, '--max-depth', help=max_depth_help)
    # status option
    status_help = 'Filter to a single status (e.g. active).'
    status = typer.Option(None, '--status', help=status_help)
    # live flag
    live_help = (
        "Trust each child's real status: relabel a crashed active node"
        ' (no tmux session) as exited, and drop nodes whose worktree is gone.'
    )
    live = typer.Option(False, '--live', help=live_help)
    # count flag
    count_help = 'Print only the number of matching nodes.'
    count = typer.Option(False, '--count', help=count_help)
    # csv flag
    csv_help = 'Force CSV output (already the default when piped / non-TTY).'
    csv = typer.Option(False, '--csv', help=csv_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'list')
    def _list(
        node: Optional[str] = node,
        all_nodes: bool = all_nodes,
        retired: bool = retired,
        max_depth: Optional[int] = max_depth,
        status: Optional[str] = status,
        live: bool = live,
        count: bool = count,
        csv: bool = csv,
        path: str = path,
    ) -> None:
        """List a node's descendants with status (blank limit columns mean unlimited).

        Lists descendants only -- it never includes the target row; use
        ``fractal node status`` for the node's own status.
        """
        # validate arguments
        require_non_negative(max_depth=max_depth)
        if status == '':
            raise typer.BadParameter('--status cannot be empty.')
        # stable user-facing column set
        columns = [
            'status',
            'node',
            'title',
            'max_cost',
            'max_depth',
            'max_children',
            'max_descendants',
        ]
        # a whole-tree listing (no explicit branch) of a path with no
        # fractal root is "no nodes", not an error; must report an empty
        # list (exit 0) on the uninitialized case rather than hard-failing
        if node is None:
            try:
                node = resolve_target(path, node)
            except typer.BadParameter:
                if count:
                    typer.echo(0)
                else:
                    print_rows([], csv=csv, columns=columns)
                return
        else:
            node = resolve_target(path, node)
        # list nodes
        rows = node.list(
            all_nodes=all_nodes,
            retired_only=retired,
            max_depth=max_depth,
            status=status,
            live=live,
            decorated=not count,
        )
        # count short-circuits formatting -- emit just the number
        if count:
            typer.echo(len(rows))
            return
        rows = [{column: row.get(column) for column in columns} for row in rows]
        # bracket the status for terminal display only
        # (machine output stays unbracketed for clean parsing)
        if rows and not csv and sys.stdout.isatty():
            for row in rows:
                status = row['status']
                row['status'] = f'[{status}]'
        print_rows(rows, csv=csv, columns=columns)

    return app


def node_activity(app: typer.Typer) -> typer.Typer:
    """Register the ``activity`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # limit option
    limit_help = 'Maximum rows to return.'
    limit = typer.Option(None, '--limit', help=limit_help)
    # csv flag
    csv_help = 'Force CSV output (already the default when piped / non-TTY).'
    csv = typer.Option(False, '--csv', help=csv_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'activity')
    def _activity(
        node: Optional[str] = node,
        limit: Optional[int] = limit,
        csv: bool = csv,
        path: str = path,
    ) -> None:
        """Show the node's lifecycle activity, most recent first."""
        require_non_negative(limit=limit)
        node = resolve_target(path, node)
        query = (
            'SELECT * FROM activity WHERE node = ?'
            ' ORDER BY timestamp DESC, run_id DESC, iter_id DESC, step_id DESC'
        )
        if limit is not None:
            query += f' LIMIT {limit}'
        rows = node.db.read(query=query, params=(node._branch,))
        print_rows(rows, csv=csv, columns=_ACTIVITY_COLUMNS)

    return app


def node_approve(app: typer.Typer) -> typer.Typer:
    """Register the ``approve`` command."""
    # node argument (required -- approval is always about a child)
    node_help = 'Child node branch whose step to approve.'
    node = typer.Argument(..., help=node_help)
    # step id argument (optional -- defaults to the child's active step)
    step_id_help = "Step ID to approve (default: the child's active step)."
    step_id = typer.Argument(None, help=step_id_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'approve')
    def _approve(
        node: str = node,
        step_id: Optional[int] = step_id,
        path: str = path,
    ) -> None:
        """Approve a child's gated step (run from the parent)."""
        parent = resolve_node(path)
        child = resolve_target(path, node)
        approved = parent.child_approve(child, step_id=step_id)
        typer.echo(f'Step {approved} on {child._branch} approved.')

    return app


def node_pending(app: typer.Typer) -> typer.Typer:
    """Register the ``pending`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # csv flag
    csv_help = 'Force CSV output (already the default when piped / non-TTY).'
    csv = typer.Option(False, '--csv', help=csv_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'pending')
    def _pending(
        node: Optional[str] = node,
        csv: bool = csv,
        path: str = path,
    ) -> None:
        """List direct children's steps awaiting this node's approval."""
        node = resolve_target(path, node)
        rows = node.child_pending()
        print_rows(rows, csv=csv, columns=_PENDING_COLUMNS)

    return app


def node_chat(app: typer.Typer) -> typer.Typer:
    """Register the ``chat`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # prompt argument
    prompt_help = 'Prompt to send.'
    prompt = typer.Argument(None, help=prompt_help)
    # session option
    session_help = 'Session to fork (default: a fresh session).'
    session = typer.Option(None, '--session', help=session_help)
    # current flag
    current_help = "Fork the node's live loop session (excludes --session/--resume)."
    current = typer.Option(False, '--current', help=current_help)
    # resume flag
    resume_help = 'Continue --session in place instead of forking it.'
    resume = typer.Option(False, '--resume', help=resume_help)
    # model option
    model_help = "Model override (default: the node's configured model)."
    model = typer.Option(None, '--model', help=model_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'chat')
    def _chat(
        node: Optional[str] = node,
        prompt: Optional[str] = prompt,
        session: Optional[str] = session,
        current: bool = current,
        resume: bool = resume,
        model: Optional[str] = model,
        path: str = path,
    ) -> None:
        """Send one prompt to a node's agent and stream the reply."""
        # require a prompt argument
        if not prompt or not prompt.strip():
            raise typer.BadParameter(
                'A prompt is required as the second argument:'
                ' fractal node chat [<branch>] "<prompt>".'
            )
        # resolve target node
        node = resolve_target(path, node)
        # fork session and stream reply
        result = node.chat(
            prompt=prompt,
            session=session,
            current=current,
            resume=resume,
            model=model,
        )
        # surface the resulting session id so the thread can be continued
        if result:
            typer.echo(f'session: {result}', err=True)

    return app


def node_update(app: typer.Typer) -> typer.Typer:
    """Register the ``update`` command."""
    # node argument
    node_help = 'Target child node branch.'
    node = typer.Argument(..., help=node_help)
    # title option
    title_help = 'Child display name.'
    title = typer.Option(None, '--title', help=title_help)
    # max-cost option
    max_cost_help = 'Child maximum cost per run in USD.'
    max_cost = typer.Option(None, '--max-cost', help=max_cost_help)
    # max-depth option
    max_depth_help = 'Child maximum nesting depth.'
    max_depth = typer.Option(None, '--max-depth', help=max_depth_help)
    # max-children option
    max_children_help = 'Child maximum direct child nodes.'
    max_children = typer.Option(None, '--max-children', help=max_children_help)
    # max-descendants option
    max_descendants_help = 'Child maximum total descendant nodes.'
    max_descendants = typer.Option(None, '--max-descendants', help=max_descendants_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'update')
    def _update(
        node: str = node,
        title: Optional[str] = title,
        max_cost: Optional[float] = max_cost,
        max_depth: Optional[int] = max_depth,
        max_children: Optional[int] = max_children,
        max_descendants: Optional[int] = max_descendants,
        path: str = path,
    ) -> None:
        """Update a child node's configuration."""
        # validate arguments
        kwargs = {
            'max_cost': max_cost,
            'max_depth': max_depth,
            'max_children': max_children,
            'max_descendants': max_descendants,
        }
        require_non_negative(**kwargs)
        if title is None and all(v is None for v in kwargs.values()):
            raise typer.BadParameter(
                'Specify at least one of'
                ' --title/--max-cost/--max-depth/--max-children/--max-descendants.'
            )
        # resolve the target tree-wide (short names too) like every other node
        # command, then derive its parent -- only the parent can rewrite a child
        target = resolve_target(path, node)
        parent_branch, _, name = target._branch.rpartition('.')
        if parent_branch:
            parent = resolve_target(path, parent_branch)
        else:
            raise typer.BadParameter(f'Cannot update the user node: {target._branch}.')
        # validate the merged config the way init/config _set do -- a bare
        # require_non_negative still admits max_cost==0 and a max_cost lowered
        # below the child's stored max_iter_cost/max_step_cost (broken ordering)
        merged = {
            'max_cost': max_cost,
            'max_iter_cost': target.config_get('max_iter_cost'),
            'max_step_cost': target.config_get('max_step_cost'),
            'reserve_budget': target.config_get('reserve_budget'),
        }
        if max_cost is None:
            merged['max_cost'] = target.config_get('max_cost')
        validate_config_values(merged)
        # update node configuration
        parent.child_update(
            name=name,
            title=title,
            max_cost=max_cost,
            max_depth=max_depth,
            max_children=max_children,
            max_descendants=max_descendants,
        )

    return app


def node_render(app: typer.Typer) -> typer.Typer:
    """Register the ``_render`` command."""
    # template argument
    template_help = 'Template file to render (default: read from stdin).'
    template = typer.Argument(None, help=template_help)
    # var option
    vars_help = 'Override a template variable as KEY=VALUE (repeatable).'
    vars = typer.Option([], '--var', help=vars_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_render')
    def _render(
        template: Optional[str] = template,
        vars: list[str] = vars,
        path: str = path,
    ) -> None:
        """Render a node's ``$VAR`` template variables (the loop's substitutor).

        Reads the template from the file argument or stdin, substitutes the
        node's variables (overridden by any ``--var KEY=VALUE``), and prints the
        result verbatim. ``_run.sh`` pipes the assembled step prompt through this.
        """
        # read the template from the file argument, else stdin
        if template is not None:
            text = pathlib.Path(template).read_text(encoding='utf-8')
        else:
            text = sys.stdin.read()
        # parse KEY=VALUE overrides (split on the first '=' so a value may hold '=')
        overrides = {}
        for item in vars:
            key, _, value = item.partition('=')
            overrides[key] = value
        # render and print verbatim (no added newline -- byte-faithful like envsubst)
        node = resolve_node(path)
        rendered = node.render_template(text, overrides=overrides)
        typer.echo(rendered, nl=False)

    return app
