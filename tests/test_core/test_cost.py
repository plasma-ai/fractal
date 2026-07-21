"""Test the ``fractal.core.cost`` module.

Cost accounting for a node (the ``Cost`` ledger).

Behavior pins for the cost readers over persisted rows: per-run budgets
and rollups, the unpriced/untracked disclosure taxonomy, per-run subtree
lineage (breakdown/lifetime), and the display-complete spend table
(``Cost.rows`` sums to ``Cost.spent``).

Uses the in-process ``node_with_db`` fixture (or the spawned parent/child
pair for lineage cases); deterministic, no sleeps.
"""

from __future__ import annotations

import pathlib
from typing import Optional

import pytest

from fractal.core import cost
from fractal.core.node import Node

from .conftest import _active_run, _record_step_cost, _spawn_parent_child

__all__ = [
    'test_cost_remaining_subtracts_step_costs',
    'test_cost_remaining_scopes_to_per_level_caps',
    'test_cost_spent_reads_current_run_after_continue',
    'test_run_cost_rollup_spans_iterations_and_sync_steps',
    'test_abnormal_end_marks_streamed_step_unpriced',
    'test_no_unpriced_marker_without_stream_or_flushed_cost',
    'test_late_flush_replaces_unpriced_marker_with_cost',
    'test_reconcile_marks_streamed_step_unpriced',
    'test_cost_unpriced_counts_ended_null_cost_steps',
    'test_cost_untracked_distinguishes_null_from_zero',
    'test_cost_untracked_subtree_flags_untracked_child',
    'test_parent_run_id_scopes_subtree_cost',
    'test_cost_spent_includes_deleted_child',
    'test_subtree_spent_walks_deleted_descendants',
    'test_cost_lifetime_sums_all_runs_across_subtree',
    'test_cost_rows_sum_to_spent_with_a_deleted_descendant',
]


# ------ cost readers


def test_cost_remaining_subtracts_step_costs(node_with_db: Node) -> None:
    """Cost remaining computes max_cost minus step costs."""
    node = node_with_db

    # no max_cost configured -- returns None
    assert node.cost.remaining() is None

    # set max_cost
    node.config.set('max_cost', 10.0)

    # no steps yet -- full budget remaining
    run_id = node.record.run_start()
    assert node.cost.remaining() == 10.0

    # record step costs
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    step_id = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.record.step_cost(step_id=step_id, cost=3.50)
    node.record.step_end(step_id=step_id, status='completed', exit_code=0)

    # remaining updated
    assert node.cost.remaining() == 6.50

    # spent returns total
    assert node.cost.spent(max_depth=0) == 3.50


def test_cost_remaining_scopes_to_per_level_caps(node_with_db: Node) -> None:
    """``Cost.remaining`` with ``iter_id``/``step_id`` uses the per-level caps.

    The run scope keys off ``max_cost``; an iteration scope off ``max_iter_cost``;
    a step scope off ``max_step_cost`` -- each minus that scope's recorded spend.
    """
    node = node_with_db
    node.config.set('max_cost', 10.0)
    node.config.set('max_iter_cost', 4.0)
    node.config.set('max_step_cost', 2.0)
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    step_id = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.record.step_cost(step_id=step_id, cost=1.5)
    node.record.step_end(step_id=step_id, status='completed', exit_code=0)

    # run scope -> max_cost - spend; iteration -> max_iter_cost; step -> max_step_cost
    assert node.cost.remaining() == pytest.approx(8.5)
    assert node.cost.remaining(iter_id=iter_id) == pytest.approx(2.5)
    assert node.cost.remaining(step_id=step_id) == pytest.approx(0.5)


def test_cost_spent_reads_current_run_after_continue(node_with_db: Node) -> None:
    """Bare cost views read the current run only; a prior run needs ``--run``.

    Runs are isolated by design: a continue opens a fresh run, so the bare
    reading forgets prior spend and ``Cost.remaining`` charges ``max_cost``
    with the current run alone. A prior run stays readable via its id.
    """
    node = node_with_db
    node.config.set('max_cost', 10.0)

    # run 1 spends, then exits (a continue never reuses a run)
    run_1 = node.record.run_start()
    _record_step_cost(node, run_id=run_1, cost=1.75)
    node.record.run_end(run_id=run_1, status='exited', exit_code=1)

    # run 2 (the continue) spends against a fresh per-run budget
    run_2 = node.record.run_start()
    _record_step_cost(node, run_id=run_2, cost=2.25)

    # bare calls read the current run; the cap charges it alone
    assert node.cost.spent(max_depth=0) == pytest.approx(2.25)
    assert node.cost.remaining() == pytest.approx(7.75)

    # an explicit run id still reads the drained prior run
    assert node.cost.spent(run_id=run_1, max_depth=0) == pytest.approx(1.75)


def test_run_cost_rollup_spans_iterations_and_sync_steps(node_with_db: Node) -> None:
    """Run cost sums every step across iterations, including SYNC (step 0).

    ``test_full_run_lifecycle`` covers a single iteration; the loop records a
    SYNC step (``step=0``) before each real step and runs many iterations per
    run. This checks the rollup over two iterations with a step-0 SYNC row: the
    run total equals the step-sum (``Cost.spent``) with no double-count, and
    ``Cost.remaining`` reflects it.
    """
    node = node_with_db
    node.config.set('max_cost', 10.0)
    run_id = node.record.run_start()

    # iteration 1: a SYNC step (step 0) then a real step
    iter_1 = node.record.iter_start(run_id=run_id, iter=1)
    sync_1 = node.record.step_start(
        iter_id=iter_1,
        run_id=run_id,
        step=0,
        step_name='SYNC',
    )
    node.record.step_cost(step_id=sync_1, cost=0.25)
    node.record.step_end(step_id=sync_1, status='completed', exit_code=0)
    plan_1 = node.record.step_start(
        iter_id=iter_1,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.record.step_cost(step_id=plan_1, cost=1.00)
    node.record.step_end(step_id=plan_1, status='completed', exit_code=0)
    node.record.iter_end(iter_id=iter_1, status='completed', exit_code=0)

    # iteration 2: a single real step
    iter_2 = node.record.iter_start(run_id=run_id, iter=2)
    exec_2 = node.record.step_start(
        iter_id=iter_2,
        run_id=run_id,
        step=1,
        step_name='EXECUTE',
    )
    node.record.step_cost(step_id=exec_2, cost=2.50)
    node.record.step_end(step_id=exec_2, status='completed', exit_code=0)
    node.record.iter_end(iter_id=iter_2, status='completed', exit_code=0)

    node.record.run_end(run_id=run_id, status='completed', exit_code=0)

    total = 0.25 + 1.00 + 2.50

    # per-iteration rollups (derived from steps) include the step-0 SYNC cost
    assert node.cost.spent(iter_id=iter_1) == 1.25
    assert node.cost.spent(iter_id=iter_2) == 2.50

    # run rollup equals the step-sum -- no double-count -- and drives remaining
    assert node.cost.spent(run_id=run_id, max_depth=0) == total
    assert node.cost.remaining() == 10.0 - total


# ------ unpriced / untracked taxonomy


@pytest.mark.parametrize(
    argnames=('status', 'reason', 'expected'),
    argvalues=[
        ('killed', 'timed out', 'timed out; unpriced'),
        ('failed', 'agent error (exit 1)', 'agent error (exit 1); unpriced'),
        ('stopped', None, 'unpriced'),
        ('exited', None, 'unpriced'),
    ],
)
def test_abnormal_end_marks_streamed_step_unpriced(
    node_with_db: Node,
    status: str,
    reason: Optional[str],
    expected: str,
) -> None:
    """A step killed before its first usage flush is marked unpriced.

    The stream opened but no usage frame ever flushed -- spend plausibly
    burned with no figure recorded. The end must stamp an explicit
    ``unpriced`` marker on the row's metadata (composing with the kill
    reason) so ledgers can tell "free step" from "unpriced step"; the cost
    column stays NULL -- SUM honesty is the disclosure count's job.
    """
    node = node_with_db
    _, _, step_id = _streamed_step(node)
    node.record.step_end(step_id=step_id, status=status, exit_code=1, metadata=reason)
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['metadata'] == expected
    assert row['cost'] is None


def test_no_unpriced_marker_without_stream_or_flushed_cost(
    node_with_db: Node,
) -> None:
    """The marker is scoped to burn-plausible rows only.

    A step whose agent never streamed (no session) has nothing to price --
    it stays a plain NULL row; a step whose usage already flushed carries a
    real figure; a clean completion is never marked even with a session (a
    token-priced codex step legitimately completes with NULL cost).
    """
    node = node_with_db
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    # never streamed: killed pre-launch, no burn -- no marker
    unstreamed = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='EXECUTE',
    )
    node.record.step_end(step_id=unstreamed, status='killed', exit_code=1)
    # flushed: the metered partial is on the row -- no marker
    flushed = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=2,
        step_name='EXECUTE',
    )
    node.record.step_session(
        'claude',
        step_id=flushed,
        model='claude-fable-5',
        session='session-119f',
    )
    node.record.step_cost(step_id=flushed, cost=0.5)
    node.record.step_end(
        step_id=flushed,
        status='killed',
        exit_code=1,
        metadata='timed out',
    )
    # clean end with a session and no cost (untracked agent shape) -- no marker
    untracked = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=3,
        step_name='EXECUTE',
    )
    node.record.step_session(
        'codex',
        step_id=untracked,
        model=None,
        session='session-119u',
    )
    node.record.step_end(step_id=untracked, status='completed', exit_code=0)
    rows = {
        row['step_id']: row for row in node.db.read('steps', where={'iter_id': iter_id})
    }
    assert rows[unstreamed]['metadata'] == ''
    assert rows[unstreamed]['cost'] is None
    assert rows[flushed]['metadata'] == 'timed out'
    assert rows[flushed]['cost'] == 0.5
    assert rows[untracked]['metadata'] == ''
    assert rows[untracked]['cost'] is None


def test_late_flush_replaces_unpriced_marker_with_cost(
    node_with_db: Node,
) -> None:
    """A flush landing after the kill prices the row and drops the marker.

    ``step_cost`` may run after ``step_end`` (the per-frame flush racing a
    kill): the real figure replaces the placeholder state, so the stale
    ``unpriced`` marker must not survive next to a recorded cost.
    """
    node = node_with_db
    _, _, step_id = _streamed_step(node)
    node.record.step_end(
        step_id=step_id,
        status='killed',
        exit_code=1,
        metadata='timed out',
    )
    node.record.step_cost(step_id=step_id, cost=0.25)
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == 0.25
    assert row['metadata'] == 'timed out'


def test_reconcile_marks_streamed_step_unpriced(node_with_db: Node) -> None:
    """The stranded-row reconcile marks a dead loop's streamed step.

    A loop killed outright never runs a step end; the next ``run_start``
    stamps the orphaned open rows ``exited`` -- the same pre-first-flush
    window, through the ``Record.close_open`` funnel, so the marker must
    land there too.
    """
    node = node_with_db
    _, _, step_id = _streamed_step(node)
    # a new run reconciles the stranded lifecycle (crashed-loop shape)
    node.record.run_start()
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['status'] == 'exited'
    assert row['metadata'] == 'unpriced'
    assert row['cost'] is None


def test_cost_unpriced_counts_ended_null_cost_steps(node_with_db: Node) -> None:
    """``Cost.unpriced`` counts ended NULL-cost steps per scope.

    The disclosure half of the unpriced-step remedy: SUM() skips NULL rows
    without a trace, so ledger-facing queries need the gap count -- ended
    rows only (an open step is merely not priced *yet*), across the same
    scopes ``Cost.spent`` answers for.
    """
    node = node_with_db
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    # a priced completed step: not a gap
    priced = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.record.step_cost(step_id=priced, cost=0.5)
    node.record.step_end(step_id=priced, status='completed', exit_code=0)
    # a killed streamed step with no flush: the gap
    killed = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=2,
        step_name='EXECUTE',
    )
    node.record.step_session(
        'claude',
        step_id=killed,
        model='claude-fable-5',
        session='session-119c',
    )
    node.record.step_end(step_id=killed, status='killed', exit_code=1)
    # a still-open step: NULL cost but not ended -- never counted
    node.record.step_start(iter_id=iter_id, run_id=run_id, step=3, step_name='REVIEW')
    assert node.cost.unpriced(step_id=priced) == 0
    assert node.cost.unpriced(step_id=killed) == 1
    assert node.cost.unpriced(iter_id=iter_id) == 1
    assert node.cost.unpriced(run_id=run_id, max_depth=0) == 1


def test_cost_untracked_distinguishes_null_from_zero(node_with_db: Node) -> None:
    """``Cost.untracked`` flags a scope whose steps recorded ``NULL`` cost.

    A token-priced agent with no priced model records ``NULL`` cost, so its spend
    sums to ``0`` yet is not genuinely ``$0``. ``Cost.untracked`` is ``True`` only
    when the scope has steps and none carries a cost.
    """
    node = node_with_db
    run_id = node.record.run_start()

    # no steps yet -> genuinely nothing, not untracked
    assert node.cost.spent() == 0.0
    assert node.cost.untracked() is False

    # a step that never recorded a cost -> spend sums to 0 but is untracked
    iter_1 = node.record.iter_start(run_id=run_id, iter=1)
    null_step = node.record.step_start(
        iter_id=iter_1,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.record.step_end(step_id=null_step, status='completed', exit_code=0)
    assert node.cost.spent() == 0.0
    assert node.cost.untracked() is True
    assert node.cost.untracked(step_id=null_step) is True

    # a priced step among them -> the run scope reads as tracked again
    iter_2 = node.record.iter_start(run_id=run_id, iter=2)
    priced_step = node.record.step_start(
        iter_id=iter_2,
        run_id=run_id,
        step=1,
        step_name='EXEC',
    )
    node.record.step_cost(step_id=priced_step, cost=1.25)
    node.record.step_end(step_id=priced_step, status='completed', exit_code=0)
    assert node.cost.untracked(step_id=priced_step) is False
    assert node.cost.untracked() is False
    assert node.cost.spent() == pytest.approx(1.25)


def test_cost_untracked_subtree_flags_untracked_child(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parent reads a fully-untracked child's spend as untracked, not $0.

    The codex-on-ChatGPT case: a manager (claude, tracked) monitors a child whose
    steps recorded ``NULL`` cost. At the parent's run scope, ``Cost.spent`` sums
    to 0 (the child's NULL costs add nothing) -- so ``Cost.untracked`` must walk
    the per-run subtree and report untracked, letting ``cost spent`` show ``null``
    rather than ``$0`` (which would hide the child's real, unpriced spend). A
    *mixed* subtree (any priced step) reads as tracked.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    p_run = _active_run(parent)
    child_run = _active_run(child)

    # the child does work but records NULL cost (untracked codex)
    child_iter = child.record.iter_start(run_id=child_run, iter=1)
    child_step = child.record.step_start(
        iter_id=child_iter,
        run_id=child_run,
        step=1,
        step_name='PLAN',
    )
    child.record.step_end(step_id=child_step, status='completed', exit_code=0)

    # parent has no own priced steps -> subtree spend sums to 0, but it is untracked
    assert parent.cost.spent(run_id=p_run) == 0.0
    assert parent.cost.untracked(run_id=p_run) is True  # subtree walk sees the child
    # own scope only: the parent itself ran nothing -> genuinely zero, not untracked
    assert parent.cost.untracked(run_id=p_run, max_depth=0) is False

    # a priced step anywhere in the subtree makes it tracked again (mixed case)
    _record_step_cost(parent, run_id=p_run, cost=0.50)
    assert parent.cost.spent(run_id=p_run) == pytest.approx(0.50)
    assert parent.cost.untracked(run_id=p_run) is False


# ------ subtree rollups


def test_parent_run_id_scopes_subtree_cost(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-run subtree cost links a child run to the parent run it spawned under.

    A child run started while the parent is active records the parent's active
    ``run_id``, so the parent's per-run ``Cost.spent`` and ``Cost.breakdown``
    include that child's in-run spend. A child run started while the parent is
    idle links to no parent run (``parent_run_id IS NULL``) and is excluded from
    the parent's per-run subtree.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    p_run = _active_run(parent)
    child_run = _active_run(child)
    # the child's run, started under the active parent, records the parent's run
    linked = child.db.read('runs', where={'run_id': child_run}, limit=1)[0]
    assert linked['parent_run_id'] == p_run

    # $1 of child spend in that in-parent-run child run rolls up to the parent run
    _record_step_cost(child, run_id=child_run, cost=1.0)
    assert parent.cost.spent(run_id=p_run) == pytest.approx(1.0)

    # end both runs, then start a second child run while the parent is idle: with
    # no active parent run, it links to none (parent_run_id NULL)
    child.record.run_end(run_id=child_run, status='completed', exit_code=0)
    parent.record.run_end(run_id=p_run, status='completed', exit_code=0)
    idle_run = child.record.run_start()
    idle_row = child.db.read('runs', where={'run_id': idle_run}, limit=1)[0]
    assert idle_row['parent_run_id'] is None
    _record_step_cost(child, run_id=idle_run, cost=2.0)

    # the parent run's subtree (and per-node breakdown) excludes the
    # idle-spawned child run -- only the in-run $1 is attributed
    assert parent.cost.spent(run_id=p_run) == pytest.approx(1.0)
    assert parent.cost.breakdown(run_id=p_run) == pytest.approx({child.branch: 1.0})


def test_cost_spent_includes_deleted_child(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deleted child's recorded spend still counts in the parent's subtree.

    A subtree walk reading each child's own database would let a deleted
    child erase its spend from the parent's ``Cost.spent`` -- a ``max_cost``
    budget silently regaining headroom it already burned. The central
    database keeps the lineage priced.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    child_branch = child.branch
    p_run = _active_run(parent)
    child_run = _active_run(child)
    _record_step_cost(child, run_id=child_run, cost=1.5)
    assert parent.cost.spent(run_id=p_run) == pytest.approx(1.5)

    # delete the child -- its spend must survive in the parent's rollup
    child.status_set('completed')
    Node(git_repo / '.worktrees' / 'main.parent.kid').delete()
    assert parent.cost.spent(run_id=p_run) == pytest.approx(1.5)
    assert parent.cost.breakdown(run_id=p_run) == pytest.approx({child_branch: 1.5})


def test_subtree_spent_walks_deleted_descendants(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cockpit's run-spend walk counts a deleted descendant, like Cost.spent.

    The card reads run-spend via :func:`cost.subtree_spent` -- the same
    ``parent_run_id`` DB walk budget enforcement uses -- so a deleted child's
    spend still shows and the displayed total never drifts below the figure
    the budget gate counts.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    p_run = _active_run(parent)
    child_run = _active_run(child)
    _record_step_cost(child, run_id=child_run, cost=1.5)
    # delete the child -- its spend must survive the run-spend walk
    child.status_set('completed')
    Node(git_repo / '.worktrees' / 'main.parent.kid').delete()
    connection = parent.db.connect(read_only=True)
    try:
        assert cost.subtree_spent(connection, p_run) == pytest.approx(1.5)
    finally:
        connection.close()


def test_cost_lifetime_sums_all_runs_across_subtree(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifetime cost spans every run per node, over the whole subtree at once.

    ``Cost.breakdown`` scopes to one run's spawn lineage; the lifetime view
    keys each registered branch (the node itself included) to its all-runs
    total, so one call covers a tree view's per-row spend.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    _record_step_cost(parent, run_id=_active_run(parent), cost=0.25)
    _record_step_cost(child, run_id=_active_run(child), cost=1.0)
    # a second child run: lifetime totals accumulate across runs
    second_run = child.record.run_start()
    _record_step_cost(child, run_id=second_run, cost=0.5)
    assert parent.cost.lifetime() == pytest.approx(
        {'main.parent': 0.25, 'main.parent.kid': 1.5}
    )
    # the user view narrows by depth and zero-fills spendless branches
    user = Node(git_repo)
    assert user.cost.lifetime(max_depth=1) == pytest.approx(
        {'main': 0.0, 'main.parent': 0.25}
    )


def test_cost_rows_sum_to_spent_with_a_deleted_descendant(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Cost.rows`` totals ``Cost.spent`` even after a descendant delete.

    The display-complete table leads with the target's own row, keeps each
    still-registered descendant, and appends a deleted-lineage row (no cap,
    ``deleted`` flagged) for spend that still chains via ``parent_run_id``
    -- so the rows always sum to the figure ``Cost.spent`` reports.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    child_branch = child.branch
    p_run = _active_run(parent)
    child_run = _active_run(child)
    _record_step_cost(parent, run_id=p_run, cost=0.25)
    _record_step_cost(child, run_id=child_run, cost=1.5)
    # live subtree: the parent's own row leads, the registered child follows
    rows = parent.cost.rows(run_id=p_run)
    assert [row['node'] for row in rows] == [parent.branch, child_branch]
    assert not any(row['deleted'] for row in rows)
    total = sum(row['spent'] for row in rows)
    assert total == pytest.approx(parent.cost.spent(run_id=p_run))

    # delete the child -- its spend re-appears as a deleted-lineage row
    child.status_set('completed')
    Node(git_repo / '.worktrees' / 'main.parent.kid').delete()
    rows = parent.cost.rows(run_id=p_run)
    deleted = [row for row in rows if row['deleted']]
    assert [row['node'] for row in deleted] == [child_branch]
    assert deleted[0]['max_cost'] is None
    total = sum(row['spent'] for row in rows)
    assert total == pytest.approx(parent.cost.spent(run_id=p_run))


# ------ helpers


def _streamed_step(node: Node, *, step: int = 1) -> tuple[int, int, int]:
    """Open a run/iter/step and capture a session (stream opened, no flush)."""
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    step_id = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=step,
        step_name='EXECUTE',
    )
    node.record.step_session(
        'claude',
        step_id=step_id,
        model='claude-fable-5',
        session='session-119',
    )
    return run_id, iter_id, step_id
