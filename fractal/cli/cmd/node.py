"""Implements ``fractal node`` sub-app commands."""

from __future__ import annotations

import pathlib
import sys
from typing import Optional

import typer

from fractal.cli.utils import (
    StreamRenderer,
    command,
    parse_reserve_budget,
    print_json,
    print_rows,
    require_non_negative,
    resolve_init_target,
    resolve_node,
    resolve_target,
)
from fractal.constants import STATUSES
from fractal.core.agent import seed_agents
from fractal.core.loop import Loop
from fractal.core.node import Node

__all__ = [
    'node_init',
    'node_start',
    'node_finish',
    'node_stop',
    'node_pause',
    'node_resume',
    'node_kill',
    'node_merge',
    'node_delete',
    'node_reconcile',
    'node_retire',
    'node_unretire',
    'node_attach',
    'node_status',
    'node_list',
    'node_activity',
    'node_approve',
    'node_pending',
    'node_chat',
    'node_update',
    'node_loop',
    'node_seed',
]

# consumers bind list columns by header name, never by position
_LIST_COLUMNS = [
    'status',
    'detail',
    'node',
    'title',
    'max_cost',
    'spend',
    'max_depth',
    'max_children',
    'max_descendants',
    'last',
]

# consumers bind activity columns by header name, never by position
_ACTIVITY_COLUMNS = [
    'timestamp',
    'node',
    'event_id',
    'step_id',
    'iter_id',
    'run_id',
    'event',
    'actor',
    'step_name',
    'step',
    'iter',
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
    scope_help = 'Subdirectory scope within the worktree (comma-separated; repeatable).'
    scope = typer.Option(None, '--scope', help=scope_help)
    # base option
    base_help = 'Branch to start from; also the squash-merge target when set.'
    base = typer.Option(None, '--base', help=base_help)
    # meta option
    meta_help = 'Target node branch for meta-configuration.'
    meta = typer.Option(None, '--meta', help=meta_help)
    # inherit option
    inherit_help = (
        'Seed surfaces from the parent node instead of the package seed'
        ' (comma-separated; repeatable): steps, scripts, skills, config,'
        ' or all.'
    )
    inherit = typer.Option(None, '--inherit', help=inherit_help)
    # steps option
    steps_help = (
        'Directory of NN- prefixed step files (*.md) to seed steps/ from'
        ' instead of the package seed; mutually exclusive with --inherit=steps.'
    )
    steps = typer.Option(None, '--steps', help=steps_help)
    # profile option
    profile_help = (
        'Named seed bundle under .fractal/profiles/<name>/: steps/ seeds the'
        ' step list, NODE.md a deployment-ready charter (fill-sheet validated'
        ' at init; mutex with --steps).'
    )
    profile = typer.Option(None, '--profile', help=profile_help)
    # pin option
    pin_help = (
        'Commission pin (a commit sha): must resolve, and every pin:'
        ' declaration in the profile charter must match it.'
    )
    pin = typer.Option(None, '--pin', help=pin_help)
    # agent option
    agent_help = 'Agent command (default: inherited from the nearest ancestor).'
    agent = typer.Option(None, '--agent', help=agent_help)
    # provider option
    provider_help = (
        'Provider route for the agent (e.g. openrouter; default: the'
        ' vendor-native endpoint, inherited from the nearest ancestor).'
    )
    provider = typer.Option(None, '--provider', help=provider_help)
    # model option
    model_help = 'Model override (passed to agent CLI via --model).'
    model = typer.Option(None, '--model', help=model_help)
    # effort option
    effort_help = 'Reasoning-effort override (passed to agent CLI).'
    effort = typer.Option(None, '--effort', help=effort_help)
    # max iters option
    max_iters_help = 'Per-run iteration cap (default: unlimited).'
    max_iters = typer.Option(None, '--max-iters', help=max_iters_help)
    # max depth option
    max_depth_help = 'Maximum child node nesting depth (default: unlimited).'
    max_depth = typer.Option(None, '--max-depth', help=max_depth_help)
    # max children option
    max_children_help = 'Maximum direct child nodes (default: unlimited).'
    max_children = typer.Option(None, '--max-children', help=max_children_help)
    # max descendants option
    max_descendants_help = 'Maximum total descendant nodes (default: unlimited).'
    max_descendants = typer.Option(None, '--max-descendants', help=max_descendants_help)
    # timeout option
    timeout_help = 'Whole-run time budget (e.g. 30s, 10m, 1.5h).'
    timeout = typer.Option(None, '--timeout', help=timeout_help)
    # iter timeout option
    iter_timeout_help = 'Per-iteration time budget (e.g. 30s, 10m, 1.5h).'
    iter_timeout = typer.Option(None, '--iter-timeout', help=iter_timeout_help)
    # step timeout option
    step_timeout_help = 'Per-step time budget (e.g. 30s, 10m); caps each step.'
    step_timeout = typer.Option(None, '--step-timeout', help=step_timeout_help)
    # step retries option
    step_retries_help = 'Retries per failed step (default: 1; 0 disables).'
    step_retries = typer.Option(None, '--step-retries', help=step_retries_help)
    # step retry backoff option
    step_retry_backoff_help = 'Delay before each step retry (default: 10s).'
    step_retry_backoff = typer.Option(
        None,
        '--step-retry-backoff',
        help=step_retry_backoff_help,
    )
    # interval option
    interval_help = 'Fixed iteration schedule (e.g. 30m, 1h).'
    interval = typer.Option(None, '--interval', help=interval_help)
    # sleep option
    sleep_help = 'Delay between iterations (e.g. 10s, 5m).'
    sleep = typer.Option(None, '--sleep', help=sleep_help)
    # wait option
    wait_help = 'Sleep between approval-wait sync invocations (default: 1m).'
    wait = typer.Option(None, '--wait', help=wait_help)
    # max cost option
    max_cost_help = (
        'Maximum cost in USD per run — runs are isolated, so each launch'
        ' arms the cap anew; after a budget-ended run, `node start'
        ' --continue` refuses without an explicit --max-cost.'
    )
    max_cost = typer.Option(None, '--max-cost', help=max_cost_help)
    # max iter cost option
    max_iter_cost_help = 'Maximum cost per iteration in USD.'
    max_iter_cost = typer.Option(None, '--max-iter-cost', help=max_iter_cost_help)
    # max step cost option
    max_step_cost_help = 'Maximum cost per step in USD (warn-only when unenforceable).'
    max_step_cost = typer.Option(None, '--max-step-cost', help=max_step_cost_help)
    # reserve budget option
    reserve_budget_help = (
        'Budget reserved for cleanup; USD or N% of --max-cost (default: 10%).'
    )
    reserve_budget = typer.Option(None, '--reserve-budget', help=reserve_budget_help)
    # sync flag
    sync_help = 'Run sync mode before each step (default: enabled).'
    sync = typer.Option(None, '--sync/--no-sync', help=sync_help)
    # detached flag
    detached_help = 'Run each step as a separate invocation (default: continuous).'
    detached = typer.Option(None, '--detached/--no-detached', help=detached_help)
    # local flag
    local_help = "Skip pushing to remote after each iteration's commit."
    local = typer.Option(None, '--local/--no-local', help=local_help)
    # blind flag
    blind_help = 'Subscribe to no channels (the parent still reads this node).'
    blind = typer.Option(False, '--blind', help=blind_help)
    # sealed flag
    sealed_help = (
        "Seal the node's mailbox: its own seat cannot read hosted messages"
        ' until an operator or the parent unseals it (config sealed=false).'
    )
    sealed = typer.Option(False, '--sealed', help=sealed_help)
    # reset flag
    reset_help = 'Delete node files and reinitialize.'
    reset = typer.Option(False, '--reset', help=reset_help)

    @command(app, 'init')
    def _init(
        name: str = name,
        path: str = path,
        title: Optional[str] = title,
        scope: Optional[list[str]] = scope,
        base: Optional[str] = base,
        meta: Optional[str] = meta,
        inherit: Optional[list[str]] = inherit,
        steps: Optional[str] = steps,
        profile: Optional[str] = profile,
        pin: Optional[str] = pin,
        agent: Optional[str] = agent,
        provider: Optional[str] = provider,
        model: Optional[str] = model,
        effort: Optional[str] = effort,
        max_iters: Optional[int] = max_iters,
        max_depth: Optional[int] = max_depth,
        max_children: Optional[int] = max_children,
        max_descendants: Optional[int] = max_descendants,
        timeout: Optional[str] = timeout,
        iter_timeout: Optional[str] = iter_timeout,
        step_timeout: Optional[str] = step_timeout,
        step_retries: Optional[int] = step_retries,
        step_retry_backoff: Optional[str] = step_retry_backoff,
        interval: Optional[str] = interval,
        sleep: Optional[str] = sleep,
        wait: Optional[str] = wait,
        max_cost: Optional[float] = max_cost,
        max_iter_cost: Optional[float] = max_iter_cost,
        max_step_cost: Optional[float] = max_step_cost,
        reserve_budget: Optional[str] = reserve_budget,
        sync: Optional[bool] = sync,
        detached: Optional[bool] = detached,
        local: Optional[bool] = local,
        blind: bool = blind,
        sealed: bool = sealed,
        reset: bool = reset,
    ) -> None:
        """Create an agent node.

        The node's task contract lives in ``<node_dir>/NODE.md`` --
        author its Instructions and Completion Requirements sections,
        then ``fractal node start <name>``.
        """
        # a per-iter/step cap with no run ceiling can't be enforced
        # (once the per-iter budget drains, later steps run unbounded)
        # -- reject at creation so the operator fixes it now, not at
        # runtime; no cost flags at all is allowed and runs uncapped
        if max_cost is None:
            if max_iter_cost is not None:
                raise typer.BadParameter('--max-iter-cost requires --max-cost.')
            if max_step_cost is not None:
                raise typer.BadParameter('--max-step-cost requires --max-cost.')
        # max_iters must be positive like the config setter enforces -- a
        # non-positive cap reads as unlimited in the loop, so 0 would build
        # a node that iterates without bound instead of never
        if max_iters is not None and max_iters <= 0:
            raise typer.BadParameter('--max-iters must be greater than 0.')
        require_non_negative(
            max_iters=max_iters,
            max_depth=max_depth,
            max_children=max_children,
            max_descendants=max_descendants,
            step_retries=step_retries,
            max_cost=max_cost,
            max_iter_cost=max_iter_cost,
            max_step_cost=max_step_cost,
        )
        reserve_budget = parse_reserve_budget(reserve_budget, max_cost)
        node, path = resolve_init_target(path)
        output = node.init(
            name=name,
            path=path,
            title=title,
            scope=scope,
            base=base,
            meta=meta,
            inherit=inherit,
            steps=steps,
            profile=profile,
            pin=pin,
            agent=agent,
            provider=provider,
            model=model,
            effort=effort,
            max_iters=max_iters,
            max_depth=max_depth,
            max_children=max_children,
            max_descendants=max_descendants,
            timeout=timeout,
            iter_timeout=iter_timeout,
            step_timeout=step_timeout,
            step_retries=step_retries,
            step_retry_backoff=step_retry_backoff,
            interval=interval,
            sleep=sleep,
            wait=wait,
            max_cost=max_cost,
            max_iter_cost=max_iter_cost,
            max_step_cost=max_step_cost,
            reserve_budget=reserve_budget,
            sync=sync,
            detached=detached,
            local=local,
            blind=blind,
            sealed=sealed,
            reset=reset,
        )
        if output:
            typer.echo(output)
        # an uncapped node runs and spends without bound -- warn on stderr
        # (advisory, never a block); an agent with no tracked spend to meter
        # (codex without a priced model) stays quiet
        if max_cost is None and max_iters is None:
            # resolve the effective agent the way init does: the flag, else the
            # nearest ancestor's default walked from the calling node (_NODE)
            # the child nests under -- not the repo-root target resolve_init
            # returned (they differ once an agent spawns its own children)
            parent = Node.resolve_caller()
            if parent is None or parent.repo_dir != node.repo_dir:
                parent = node
            effective_agent = agent or parent.agent_effective()
            tracked = True
            # the probe needs an initialized node to anchor the agent
            # registry's database: the resolve_init target falls back to the
            # repo root, which carries no config (and so no tree root to
            # resolve the database through) whenever the checkout sits off
            # the tree's root branch -- the operator's own branch, while
            # nodes run; an unprobed agent reads as tracked, exactly like the
            # unregistered backend below: unknown spend earns the warning,
            # and this advisory must never fail a spawn that already succeeded
            if effective_agent and parent.exists():
                # an unregistered backend reads as tracked -- unknown
                # spend earns the warning, never a block
                try:
                    tracked = parent.agent(effective_agent).tracks_cost(model)
                except ValueError:
                    tracked = True
            if tracked:
                typer.echo(
                    'Warning: no --max-cost/--max-iters — this node can run'
                    ' and spend without bound.',
                    err=True,
                )

    return app


def node_start(app: typer.Typer) -> typer.Typer:
    """Register the ``start`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # continue flag
    continue_help = (
        'Continue a stopped/exited node (further iterations): the launch'
        ' restores the worktree — uncommitted project files refuse without'
        ' --clean — and a budget-ended run refuses without an explicit'
        ' --max-cost.'
    )
    continue_ = typer.Option(False, '--continue', help=continue_help)
    # clean flag
    clean_help = 'With --continue: discard uncommitted project files.'
    clean = typer.Option(False, '--clean', help=clean_help)
    # drain flag
    drain_help = (
        'With --continue: run a drain — the harness forbids spawns and'
        ' re-arms from this run and tells every seat it is draining.'
    )
    drain = typer.Option(False, '--drain', help=drain_help)
    # max cost option
    max_cost_help = (
        'With --continue: retune the cost cap before relaunch, echoed'
        ' old -> new; required when the last run ended on its budget.'
    )
    max_cost = typer.Option(None, '--max-cost', help=max_cost_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'start')
    def _start(
        node: Optional[str] = node,
        continue_: bool = continue_,
        clean: bool = clean,
        drain: bool = drain,
        max_cost: Optional[float] = max_cost,
        path: str = path,
    ) -> None:
        """Launch a node in a tmux session.

        Run parameters come from ``config.json`` (set at init or
        edited before launch); only ``--continue``/``--clean``/
        ``--max-cost`` are set here. Runs are isolated -- each launch
        arms the cap anew -- but a run that ended on its cost budget
        refuses a bare ``--continue``: pass ``--max-cost`` to arm the
        next run explicitly.
        """
        require_non_negative(max_cost=max_cost)
        if clean and not continue_:
            raise typer.BadParameter('--clean requires --continue.')
        if drain and not continue_:
            raise typer.BadParameter('--drain requires --continue.')
        if max_cost is not None and not continue_:
            raise typer.BadParameter('--max-cost requires --continue.')
        node = resolve_target(path, node)
        output = node.start(
            continue_run=continue_,
            clean=clean,
            drain=drain,
            max_cost=max_cost,
        )
        if output:
            typer.echo(output)

    return app


def node_finish(app: typer.Typer) -> typer.Typer:
    """Register the ``finish`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # reason option
    reason_help = (
        'Optional reason for finishing. The loop-authored budget phrases'
        ' (`cost budget ... (spent $...)`) are reserved and classify the'
        ' finish as a budget abort.'
    )
    reason = typer.Option(None, '--reason', help=reason_help)
    # cancel flag
    cancel_help = 'Withdraw the pending finish signal instead of sending one.'
    cancel = typer.Option(False, '--cancel', help=cancel_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'finish')
    def _finish(
        node: Optional[str] = node,
        reason: Optional[str] = reason,
        cancel: bool = cancel,
        path: str = path,
    ) -> None:
        """Stop after the current iteration (``--cancel`` withdraws a pending finish)."""
        node = resolve_target(path, node)
        if cancel:
            result = node.finish_cancel(reason)
        else:
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


def node_pause(app: typer.Typer) -> typer.Typer:
    """Register the ``pause`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # reason option
    reason_help = 'Optional reason for pausing.'
    reason = typer.Option(None, '--reason', help=reason_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'pause')
    def _pause(
        node: Optional[str] = node,
        reason: Optional[str] = reason,
        path: str = path,
    ) -> None:
        """Pause the subtree: abort in-flight agents, park the loops."""
        node = resolve_target(path, node)
        result = node.pause(reason)
        typer.echo(result)

    return app


def node_resume(app: typer.Typer) -> typer.Typer:
    """Register the ``resume`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'resume')
    def _resume(
        node: Optional[str] = node,
        path: str = path,
    ) -> None:
        """Resume the paused subtree where it left off (leaf-first)."""
        node = resolve_target(path, node)
        result = node.resume()
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
    # continue flag
    continue_help = (
        'Finish a hand-resolved squash after a conflicted merge: strip the'
        ' seed, refresh indexes, commit, and advance the merge-base.'
    )
    continue_ = typer.Option(False, '--continue', help=continue_help)
    # delete flag
    delete_help = (
        'Delete the node (worktree, branch, and subtree) after a successful merge.'
    )
    delete = typer.Option(False, '--delete', help=delete_help)
    # force flag
    force_help = 'With --delete, skip its confirmation prompt.'
    force = typer.Option(False, '--force', '-f', help=force_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'merge')
    def _merge(
        node: Optional[str] = node,
        continue_: bool = continue_,
        delete: bool = delete,
        force: bool = force,
        path: str = path,
    ) -> None:
        """Squash-merge a node's branch into its parent."""
        node = resolve_target(path, node)
        # --delete tears down the subtree; pre-flight every one of the
        # teardown's refusals (the cwd inside a doomed worktree, a live or
        # locked descendant) here, so a refusal lands before the squash
        # rather than after a merge that already committed
        if delete:
            node.guard_delete()
            # the chained teardown is as destructive as `node delete`, so it
            # takes the same confirmation gate -- landed before the squash
            # for the same reason as the refusals above
            if not force:
                descendants = len(node.child_list())
                if descendants:
                    s = 's' if descendants != 1 else ''
                    prompt = (
                        f'Merge node {node.branch}, then delete it and its'
                        f' {descendants} descendant{s}?'
                    )
                else:
                    prompt = f'Merge node {node.branch}, then delete it?'
                # the node and each descendant hold one worktree and one branch
                s = 's' if descendants else ''
                es = 'es' if descendants else ''
                typer.echo(
                    f'Warning: This permanently removes the worktree{s} and'
                    f' deletes the branch{es} after the merge.\nConsider'
                    f' retiring the node to hide it while preserving its'
                    f' branch{es}.',
                    err=True,
                )
                typer.confirm(prompt, abort=True)
        output, notices = node.merge(continue_merge=continue_)
        if output:
            typer.echo(output)
        # success-path warnings ride stderr so piped stdout stays parseable
        if notices:
            typer.echo(notices, err=True)
        # chain the teardown only after a merge that landed (a failed merge
        # raised above); its gate already passed with the pre-flight
        if delete:
            output, notices = node.delete()
            if output:
                typer.echo(output)
            if notices:
                typer.echo(notices, err=True)

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
                output = caller.deregister(node)
                typer.echo(output)
                return
            raise
        if not force:
            descendants = len(node.child_list())
            if descendants:
                s = 's' if descendants != 1 else ''
                prompt = (
                    f'Delete node {node.branch} and its {descendants} descendant{s}?'
                )
            else:
                prompt = f'Delete node {node.branch}?'
            # the node and each descendant hold one worktree and one branch
            s = 's' if descendants else ''
            es = 'es' if descendants else ''
            typer.echo(
                f'Warning: This permanently removes the worktree{s} and deletes'
                f' the branch{es}.\nConsider retiring the node to hide it while'
                f' preserving its branch{es}.',
                err=True,
            )
            typer.confirm(prompt, abort=True)
        output, notices = node.delete()
        if output:
            typer.echo(output)
        # unmerged-work warnings ride stderr so piped stdout stays parseable
        if notices:
            typer.echo(notices, err=True)

    return app


def node_reconcile(app: typer.Typer) -> typer.Typer:
    """Register the ``reconcile`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'reconcile')
    def _reconcile(
        node: Optional[str] = node,
        path: str = path,
    ) -> None:
        """Record orphaned descendants in the events log; registry rows kept.

        The audit step after cleaning up a node's worktree/branch with plain
        git instead of ``delete``: each newly observed orphan gets one
        ``orphan`` event row (already-recorded branches are skipped).
        """
        node = resolve_target(path, node)
        recorded = node.reconcile()
        if not recorded:
            typer.echo('No orphaned nodes to record.')
            return
        for branch in recorded:
            typer.echo(f'Recorded orphaned node {branch}.')

    return app


def node_retire(app: typer.Typer) -> typer.Typer:
    """Register the ``retire`` command."""
    # node argument
    node_help = 'Target node branch (default: this node).'
    node = typer.Argument(None, help=node_help)
    # reason option
    reason_help = 'Optional reason for retiring.'
    reason = typer.Option(None, '--reason', help=reason_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'retire')
    def _retire(
        node: Optional[str] = node,
        reason: Optional[str] = reason,
        path: str = path,
    ) -> None:
        """Park a node: hidden from list, unstartable; branch and history kept."""
        node = resolve_target(path, node)
        result = node.retire(reason)
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
        """Restore a retired node to its pre-retire status.

        An idle restore returns the node to the unsettled pool, so the
        width/descendant caps are re-checked as at spawn (refused over
        cap, no override); a settled restore holds no slot.
        """
        node = resolve_target(path, node)
        result = node.unretire()
        typer.echo(result)

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
        # self-reconciling: a crashed-but-active node reads settled
        display = node.status_display()
        typer.echo(display)

    return app


def node_list(app: typer.Typer) -> typer.Typer:
    """Register the ``list`` command."""
    # node argument
    node_help = (
        "Tree root branch, or a target node branch (default: this node's descendants)."
    )
    node = typer.Argument(None, help=node_help)
    # all nodes flag
    all_nodes_help = 'Include retired nodes.'
    all_nodes = typer.Option(False, '--all', help=all_nodes_help)
    # retired flag
    retired_help = 'Show only retired nodes.'
    retired = typer.Option(False, '--retired', help=retired_help)
    # max depth option
    max_depth_help = 'Maximum child depth to include (1 = direct children only).'
    max_depth = typer.Option(None, '--max-depth', help=max_depth_help)
    # status option
    status_help = 'Filter to a status, or several comma-separated (e.g. active,paused).'
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
    # json flag
    json_help = 'Output a JSON array of row objects (mutex with --csv).'
    json = typer.Option(False, '--json', help=json_help)
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
        json: bool = json,
        path: str = path,
    ) -> None:
        """List a node's descendants with status (blank limit columns mean unlimited).

        Lists descendants only -- it never includes the target row; use
        ``fractal node status`` for the node's own status. A tree root
        lists its whole tree; with several trees and a checkout belonging
        to none of them, a bare ``list`` spans them all. ``status`` is
        always bare and ``detail`` carries any qualifier (a pending
        signal, an exited run's end reason, ``orphaned``, an unresolved
        ``model drop``, an ``iteration gap``). ``spend`` is the current run's subtree cost, the
        scope ``max_cost`` beside it is enforced at, and is blank for a node
        that has never run. ``last`` is the age of each node's newest activity;
        ``!`` flags an active node quiet past ``max(step_timeout, 5m)``.
        """
        # validate arguments
        require_non_negative(max_depth=max_depth)
        if json and csv:
            raise typer.BadParameter('--json is mutually exclusive with --csv.')
        # --count prints a bare number, so pairing it with a row format is a
        # contradiction: silently honoring one would hand a machine consumer
        # a shape it never asked for -- the rule covers both row formats
        if json and count:
            raise typer.BadParameter('--json is mutually exclusive with --count.')
        if csv and count:
            raise typer.BadParameter('--csv is mutually exclusive with --count.')
        if status == '':
            raise typer.BadParameter('--status cannot be empty.')
        # statuses are a closed set -- an unknown chunk would filter to an
        # empty listing indistinguishable from no matching nodes, so each
        # comma-separated chunk validates against the set ('orphan' is the
        # listing's own worktree-gone relabel, filterable like the rest)
        if status:
            valid = (*STATUSES, 'orphan')
            for chunk in status.split(','):
                chunk = chunk.strip()
                if chunk and chunk not in valid:
                    options = ', '.join(valid)
                    raise typer.BadParameter(
                        f'Unknown status: {chunk!r}. Valid statuses: {options}.'
                    )
        # a whole-tree listing (no explicit branch) of a path with no
        # fractal root is "no nodes", not an error; must report an empty
        # list (exit 0) on the uninitialized case rather than hard-failing
        if node is None:
            try:
                targets = [resolve_target(path, node)]
            except typer.BadParameter:
                # a non-init checkout (the user on their own branch while
                # nodes run) is a live tree, not "no nodes" -- anchor on the
                # user node by config, not the checkout (mirrors pause)
                try:
                    user = Node.resolve_user(path)
                    targets = [user] if user is not None else []
                except RuntimeError:
                    # several trees, and the checkout owns none of them: a
                    # read-only listing spans them all instead of taking the
                    # mutating verbs' refusal -- showing every node is no
                    # guess, and each row's branch names the tree it sits in
                    targets = Node.user_nodes(path)
                if not targets:
                    if count:
                        typer.echo(0)
                    elif json:
                        print_json([], columns=_LIST_COLUMNS)
                    else:
                        print_rows([], csv=csv, columns=_LIST_COLUMNS)
                    return
        else:
            # a tree root owns no worktree of its own, so it resolves by
            # config like the tree-scoped verbs -- keyed to the worktree map
            # a whole-tree listing would need its root branch checked out
            user = Node.resolve_user(path, name=node)
            targets = [user] if user is not None else [resolve_target(path, node)]
        # list nodes
        rows = []
        for target in targets:
            rows += target.list(
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
        rows = [{column: row.get(column) for column in _LIST_COLUMNS} for row in rows]
        if json:
            print_json(rows, columns=_LIST_COLUMNS)
            return
        # bracket the status for terminal display only
        # (machine output stays unbracketed for clean parsing)
        if rows and not csv and sys.stdout.isatty():
            for row in rows:
                status = row['status']
                row['status'] = f'[{status}]'
        print_rows(rows, csv=csv, columns=_LIST_COLUMNS)

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
    # json flag
    json_help = 'Output a JSON array of row objects (mutex with --csv).'
    json = typer.Option(False, '--json', help=json_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'activity')
    def _activity(
        node: Optional[str] = node,
        limit: Optional[int] = limit,
        csv: bool = csv,
        json: bool = json,
        path: str = path,
    ) -> None:
        """Show the node's lifecycle activity, most recent first.

        The ``cost`` column is the row's own-node step cost (end rows
        sum only this node's steps); subtree spend, children included,
        is ``fractal node cost spent``.
        """
        require_non_negative(limit=limit)
        if json and csv:
            raise typer.BadParameter('--json is mutually exclusive with --csv.')
        node = resolve_target(path, node)
        rows = node.record.activity(limit=limit)
        rows = [
            {column: row.get(column) for column in _ACTIVITY_COLUMNS} for row in rows
        ]
        if json:
            print_json(rows, columns=_ACTIVITY_COLUMNS)
        else:
            print_rows(rows, csv=csv, columns=_ACTIVITY_COLUMNS)

    return app


def node_approve(app: typer.Typer) -> typer.Typer:
    """Register the ``approve`` command."""
    # node argument
    node_help = 'Child node branch whose step to approve.'
    node = typer.Argument(..., help=node_help)
    # step id argument
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
        typer.echo(f'Step {approved} on {child.branch} approved.')

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
    current_help = "Fork the node's loop session (excludes --session/--resume)."
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
        # fork session and stream reply through the CLI renderer
        render = StreamRenderer()
        result = node.chat(
            prompt=prompt,
            session=session,
            current=current,
            resume=resume,
            model=model,
            render=render,
        )
        # a truncated stream (no result frame) ends mid-line with no summary
        render.close()
        # surface the resulting session id so the thread can be continued
        if result:
            typer.echo(f'session: {result}', err=True)

    return app


def node_update(app: typer.Typer) -> typer.Typer:
    """Register the ``update`` command."""
    # node argument (deliberately explicit)
    node_help = 'Target child node branch.'
    node = typer.Argument(..., help=node_help)
    # title option
    title_help = 'Child display name.'
    title = typer.Option(None, '--title', help=title_help)
    # max cost option
    max_cost_help = (
        'Child maximum cost in USD per run — runs are isolated, so each'
        ' launch arms the cap anew; after a budget-ended run, `node start'
        ' --continue` refuses without an explicit --max-cost.'
    )
    max_cost = typer.Option(None, '--max-cost', help=max_cost_help)
    # max iter cost option
    max_iter_cost_help = 'Child maximum cost per iteration in USD.'
    max_iter_cost = typer.Option(None, '--max-iter-cost', help=max_iter_cost_help)
    # max step cost option
    max_step_cost_help = (
        'Child maximum cost per step in USD (warn-only when unenforceable).'
    )
    max_step_cost = typer.Option(None, '--max-step-cost', help=max_step_cost_help)
    # reserve budget option
    reserve_budget_help = (
        'Budget reserved for cleanup; USD or N% of the effective --max-cost.'
    )
    reserve_budget = typer.Option(None, '--reserve-budget', help=reserve_budget_help)
    # step timeout option
    step_timeout_help = 'Child per-step time budget (e.g. 30s, 10m); caps each step.'
    step_timeout = typer.Option(None, '--step-timeout', help=step_timeout_help)
    # max depth option
    max_depth_help = 'Child maximum nesting depth.'
    max_depth = typer.Option(None, '--max-depth', help=max_depth_help)
    # max children option
    max_children_help = 'Child maximum direct child nodes.'
    max_children = typer.Option(None, '--max-children', help=max_children_help)
    # max descendants option
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
        max_iter_cost: Optional[float] = max_iter_cost,
        max_step_cost: Optional[float] = max_step_cost,
        reserve_budget: Optional[str] = reserve_budget,
        step_timeout: Optional[str] = step_timeout,
        max_depth: Optional[int] = max_depth,
        max_children: Optional[int] = max_children,
        max_descendants: Optional[int] = max_descendants,
        path: str = path,
    ) -> None:
        """Update a child node's configuration, confirming each change old -> new.

        The supported retune path: updates the registry row and the child's
        ``config.json`` together, and a running loop picks the new caps up at
        its next iteration. (A direct config-file edit is honored at the same
        boundary, back-filled to the registry with a warning.)
        """
        # validate arguments
        kwargs = {
            'max_cost': max_cost,
            'max_iter_cost': max_iter_cost,
            'max_step_cost': max_step_cost,
            'max_depth': max_depth,
            'max_children': max_children,
            'max_descendants': max_descendants,
        }
        require_non_negative(**kwargs)
        if title is None and reserve_budget is None and step_timeout is None:
            if all(v is None for v in kwargs.values()):
                raise typer.BadParameter(
                    'Specify at least one of --title/--max-cost/--max-iter-cost'
                    '/--max-step-cost/--reserve-budget/--step-timeout/--max-depth'
                    '/--max-children/--max-descendants.'
                )
        # resolve the target tree-wide (short names too) like every other node
        # command, then derive its parent -- only the parent can rewrite a child
        target = resolve_target(path, node)
        if '.' not in target.branch:
            raise typer.BadParameter(f'Cannot update the user node: {target.branch}.')
        # parent is None when its worktree is gone (pruned out of band)
        parent = target.parent
        if parent is None:
            raise typer.BadParameter(f'Parent worktree not found: {target.branch}.')
        # resolve an explicit reserve to USD against the effective cap -- the
        # N% grammar is CLI surface and never enters core; the default-mode
        # reserve retune is core's (child_retune)
        new_reserve = None
        if reserve_budget is not None:
            if max_cost is not None:
                effective_max_cost = max_cost
            else:
                effective_max_cost = target.config.get('max_cost')
            new_reserve = parse_reserve_budget(reserve_budget, effective_max_cost)
        # retune through core -- it owns the reserve retune, the effective-cap
        # guards, the merged validation, and the prior capture
        _, _, name = target.branch.rpartition('.')
        changes = parent.child_retune(
            name,
            title=title,
            max_cost=max_cost,
            max_iter_cost=max_iter_cost,
            max_step_cost=max_step_cost,
            reserve_budget=new_reserve,
            step_timeout=step_timeout,
            max_depth=max_depth,
            max_children=max_children,
            max_descendants=max_descendants,
        )
        # confirm each change, old -> new -- a silent mid-run retune is
        # indistinguishable from a dropped one
        for key, values in changes.items():
            prior, value = values
            prior = prior if prior is not None else 'unset'
            typer.echo(f'{key}: {prior} -> {value}')

    return app


def node_loop(app: typer.Typer) -> typer.Typer:
    """Register the ``_loop`` command."""
    # agent command argument
    agent_command_help = (
        'Agent invocation (e.g. "claude"); default: the configured agent.'
    )
    agent_command = typer.Argument(None, help=agent_command_help)
    # continue flag
    continue_help = (
        'Continue a stopped/exited node (further iterations); the worktree'
        ' restore discards uncommitted project files.'
    )
    continue_ = typer.Option(False, '--continue', help=continue_help)
    # resume flag
    resume_help = 'Resume a paused node (adopt its open run where the pause left it).'
    resume = typer.Option(False, '--resume', help=resume_help)
    # drain flag
    drain_help = 'With --continue: forbid spawns and re-arms for this run.'
    drain = typer.Option(False, '--drain', help=drain_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_loop')
    def _loop(
        agent_command: Optional[str] = agent_command,
        continue_: bool = continue_,
        resume: bool = resume,
        drain: bool = drain,
        path: str = path,
    ) -> None:
        """Run the node's iteration loop (invoked by start.sh inside tmux)."""
        node = resolve_node(path)
        loop = Loop(
            node,
            agent_command=agent_command,
            continue_=continue_,
            resume=resume,
            drain=drain,
            render=StreamRenderer(),
        )
        code = loop.run()
        if code:
            raise SystemExit(code)

    return app


def node_seed(app: typer.Typer) -> typer.Typer:
    """Register the ``node _seed`` command."""
    # node-dir argument (raw path, not a resolved node: init.sh
    # seeds the agent dirs before the node is registered)
    node_dir_help = 'Node data directory to seed under.'
    node_dir = typer.Argument(..., help=node_dir_help)
    # parent flag
    parent_help = "Parent node's data directory, when one exists."
    parent = typer.Option(None, '--parent', help=parent_help)
    # reset flag
    reset_help = 'Wipe each agent dir before seeding.'
    reset = typer.Option(False, '--reset', help=reset_help)

    @command(app, '_seed')
    def _seed(
        node_dir: str = node_dir,
        parent: Optional[str] = parent,
        reset: bool = reset,
    ) -> None:
        """Seed the node's agent config dirs (invoked by init.sh)."""
        parent_dir = pathlib.Path(parent) if parent else None
        seed_agents(pathlib.Path(node_dir), parent_dir=parent_dir, reset=reset)

    return app
