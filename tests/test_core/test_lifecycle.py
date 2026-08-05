"""Node lifecycle verbs: start, finish/stop, kill, retire/unretire, merge.

Covers the full run lifecycle end to end, per-verb guards and
refusals, recursive signal fan-out across a subtree, and
merge-to-parent semantics.
"""

from __future__ import annotations

import fcntl
import os
import pathlib
import signal
import subprocess
import time
from typing import NoReturn, Optional

import pytest

from fractal.constants import PGID_FILE
from fractal.core.node import Node
from tests._helpers import _git, _stub_run_script

from .conftest import (
    _active_run,
    _parse_project_dir,
    _record_step_cost,
    _resolve_branch,
    _spawn_chain,
    _spawn_parent_child,
)

__all__ = [
    'test_full_run_lifecycle',
    'test_start_rejects_retired',
    'test_start_rejects_user',
    'test_start_rejects_non_positive_max_cost',
    'test_start_only_from_idle',
    'test_start_continue_from_terminal',
    'test_start_drain_reaches_the_launch_and_requires_continue',
    'test_start_continue_re_arms_after_drained_run',
    'test_start_continue_refuses_after_budget_end',
    'test_start_continue_rolls_back_retune_on_refusal_or_failed_launch',
    'test_start_without_max_cost_warns_and_runs',
    'test_start_continue_reconciles_crashed_active',
    'test_finish_rejects_non_active',
    'test_stop_rejects_non_active',
    'test_signal_rejects_active_node_without_run',
    'test_finish_accepts_reason',
    'test_kill_vets_recorded_group_before_script',
    'test_kill_sets_killed_status',
    'test_kill_reaps_idle_node_before_start',
    'test_kill_stamps_idle_killed_before_the_reap',
    'test_kill_marks_all_active',
    'test_kill_keeps_loop_terminal_status_when_raced',
    'test_retire_sets_status',
    'test_retire_rejects_active',
    'test_unretire_restores_pre_retire_status',
    'test_unretire_without_recorded_prior_falls_back_to_idle',
    'test_unretire_restores_the_latest_prior_when_raced',
    'test_retire_rejects_user',
    'test_signals_recurse_to_active_descendants',
    'test_recursive_signals_attribute_the_propagating_node',
    'test_recursion_skips_inactive_descendants',
    'test_signals_refuse_settled_target_before_the_sweep',
    'test_kill_recurses_to_descendants',
    'test_signals_reach_deep_through_inactive_intermediate',
    'test_kill_propagates_deep_status_and_keeps_worktrees',
    'test_kill_reaps_booting_descendant',
    'test_graceful_sweep_reaches_a_descendant_that_appears_mid_sweep',
    'test_merge_lifecycle',
    'test_merge_no_op_when_nothing_to_merge',
    'test_merge_surfaces_script_notices',
    'test_merge_excludes_merged_node_seed',
    'test_merge_excludes_subproject_node_seed',
    'test_merge_refreshes_parent_wiki_indexes',
    'test_merge_restores_parent_when_index_refresh_fails',
    'test_merge_refuses_when_parent_worktree_is_dirty',
    'test_merge_refuses_settled_child_into_a_running_target',
    'test_merge_event_survives_child_delete',
]


# ------ full run


def test_full_run_lifecycle(node_with_db: Node) -> None:
    """A full run -> iteration -> step lifecycle aggregates cost at every level."""
    node = node_with_db

    # start run
    run_id = node.record.run_start()
    assert isinstance(run_id, int)

    # start iteration
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    assert isinstance(iter_id, int)

    # start and end steps with cost
    step_1 = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.record.step_cost(step_id=step_1, cost=0.50)
    node.record.step_end(step_id=step_1, status='completed', exit_code=0)

    step_2 = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=2,
        step_name='EXECUTE',
    )
    node.record.step_cost(step_id=step_2, cost=1.25)
    node.record.step_end(step_id=step_2, status='completed', exit_code=0)

    # verify step rows
    steps = node.db.read('steps', where={'run_id': run_id})
    assert len(steps) == 2
    costs = {row['step_name']: row['cost'] for row in steps}
    assert costs['PLAN'] == 0.50
    assert costs['EXECUTE'] == 1.25

    # per-step cost is queryable by step_id (powers the --max-step-cost warning)
    assert node.cost.spent(step_id=step_1) == 0.50
    assert node.cost.spent(step_id=step_2) == 1.25

    # end iteration -- cost rolls up from steps (derived, not stored)
    node.record.iter_end(iter_id=iter_id, status='completed', exit_code=0)
    iters = node.db.read('iters', where={'iter_id': iter_id})
    assert node.cost.spent(iter_id=iter_id) == 1.75
    assert iters[0]['status'] == 'completed'
    assert iters[0]['ended_at'] is not None

    # end run -- cost rolls up from steps; duration derived from started/ended
    node.record.run_end(run_id=run_id, status='completed', exit_code=0)
    runs = node.db.read('runs', where={'run_id': run_id})
    assert node.cost.spent(run_id=run_id, max_depth=0) == 1.75
    assert runs[0]['status'] == 'completed'
    assert runs[0]['ended_at'] is not None


# ------ start


def test_start_rejects_retired(node_with_db: Node) -> None:
    """Start raises for retired nodes."""
    node = node_with_db
    # set status to retired
    node.status_set('retired')
    # verify start rejects
    with pytest.raises(RuntimeError):
        node.start()


def test_start_rejects_user(node_with_db: Node) -> None:
    """Start raises for user nodes."""
    node = node_with_db
    node.config.set('user', True)
    with pytest.raises(RuntimeError):
        node.start()


@pytest.mark.parametrize('max_cost', [0, 0.0, -1.0])
def test_start_rejects_non_positive_max_cost(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    max_cost: float,
) -> None:
    """Start refuses a non-positive budget instead of launching.

    A zero/negative ``max_cost`` (reachable through other write paths) would
    launch straight into an immediate degenerate $0 finish, so the guard rejects
    it before any tmux session is started. A *missing* ``max_cost`` is allowed --
    it runs uncapped (see ``test_start_without_max_cost_warns_and_runs``).
    """
    node = node_with_db
    node.config.set('max_cost', max_cost)
    # the node is idle (fixture default) -- only the budget should block start
    run_scripts = _stub_run_script(monkeypatch, node)
    with pytest.raises(RuntimeError, match='max_cost'):
        node.start()
    assert run_scripts == []


def test_start_only_from_idle(node_with_db: Node) -> None:
    """Start without continue raises from non-idle status."""
    node = node_with_db
    # set status to a terminal state
    node.status_set('completed')
    # verify start rejects without continue
    with pytest.raises(RuntimeError):
        node.start()


@pytest.mark.parametrize('status', ['completed', 'stopped', 'exited', 'killed'])
def test_start_continue_from_terminal(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """Start with continue succeeds from every continuable terminal status."""
    node = node_with_db
    # configure a cost budget (required to start)
    node.config.set('max_cost', 1.0)
    # set the node to a terminal status
    node.status_set(status)
    # verify continue launches (stub shell script)
    run_scripts = _stub_run_script(monkeypatch, node)
    node.start(continue_run=True)
    assert run_scripts


def test_start_drain_reaches_the_launch_and_requires_continue(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--drain`` rides the launch argv, and only alongside ``--continue``.

    The flag is the whole wind-down contract: if it never reaches
    ``start.sh`` (and through it the loop), a run the operator ordered to
    drain quietly keeps its spawn and re-arm doors open. A plain start
    carries no drain, and the CLI refuses the flag without ``--continue``
    rather than accepting a no-op.
    """
    node = node_with_db
    node.config.set('max_cost', 1.0)
    node.status_set('stopped')
    run_scripts = _stub_run_script(monkeypatch, node)
    node.start(continue_run=True, drain=True)
    launch = next(call for call in run_scripts if call[0] == 'start.sh')
    assert '--continue' in launch
    assert '--drain' in launch
    # a plain continue carries no drain
    run_scripts.clear()
    node.status_set('stopped')
    node.start(continue_run=True)
    launch = next(call for call in run_scripts if call[0] == 'start.sh')
    assert '--drain' not in launch


def test_start_continue_re_arms_after_drained_run(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-budget end continues bare: prior-run spend never blocks a launch.

    Runs are isolated by design: a launch after a drained ``exited``/1 run
    (a crash or timeout, never the ``exited``/0 budget landing) proceeds
    with the full ``max_cost`` re-armed -- the continue gate keys on the
    budget-landing discriminator, not on prior spend itself.
    """
    node = node_with_db
    node.config.set('max_cost', 0.15)
    # run 1 drains past the cap, then dies abnormally (exit 1, not a landing)
    run_1 = node.record.run_start()
    _record_step_cost(node, run_id=run_1, cost=0.20)
    node.record.run_end(run_id=run_1, status='exited', exit_code=1)
    node.status_set('exited')
    # the launch re-arms the full cap and proceeds
    run_scripts = _stub_run_script(monkeypatch, node)
    node.start(continue_run=True)
    assert run_scripts


@pytest.mark.parametrize(
    argnames=('stamped_cap', 'reason', 'refusal'),
    argvalues=[
        # a run that tripped its own budget refuses with the stamped figures
        pytest.param(
            0.15,
            'cost budget reserve reached (spent $0.2000 >= $0.15 max - $0.015 reserve)',
            r'ended on its cost budget \(spent \$0\.2000 of \$0\.15 armed\)',
            id='own-budget',
        ),
        # an uncapped run landed by an ancestor's cascade has no stamp and no
        # config cap -- the refusal names the ancestor and the missing arm
        pytest.param(
            None,
            'ancestor budget abort: subtree cost budget reached'
            ' (spent $9 >= $5 max) (via finish of main.parent);'
            ' this run spent $0.2000',
            r"cut by an ancestor's cost budget"
            r' \(spent \$0\.2000, no cap armed\)',
            id='cascaded-uncapped',
        ),
    ],
)
def test_start_continue_refuses_after_budget_end(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    stamped_cap: Optional[float],
    reason: str,
    refusal: str,
) -> None:
    """A budget-ended run refuses a bare continue; an explicit cap re-arms it.

    ``exited``/0 is the budget-landing discriminator: a bare ``--continue``
    would silently re-spend money the caller never re-authorized, so it
    refuses -- naming the stamped figures for a run that tripped its own
    cap, and the ancestor whose cascade landed an uncapped run. An
    explicit ``max_cost`` retunes the cap through the parent (reserve
    re-derivation included) and proceeds.
    """
    _, child = _spawn_parent_child(git_repo, monkeypatch)
    # land the child's run on its budget: exited/0 with the landing's reason
    # recorded, and the armed cap stamped on the run row when one was set
    run_id = _active_run(child)
    _record_step_cost(child, run_id=run_id, cost=0.20)
    if stamped_cap is not None:
        child.db.update({'max_cost': stamped_cap}, 'runs', where={'run_id': run_id})
    child.record.run_end(
        run_id=run_id,
        status='exited',
        exit_code=0,
        metadata=reason,
    )
    child.status_set('exited')
    # a bare continue refuses with the recorded figures
    run_scripts = _stub_run_script(monkeypatch, child)
    with pytest.raises(RuntimeError, match=refusal):
        child.start(continue_run=True)
    assert run_scripts == []
    # an explicit cap retunes through the parent and proceeds (--clean
    # sidesteps the dirty-tree guard, which has its own coverage); the
    # retune echo rides the returned confirmation
    output = child.start(continue_run=True, clean=True, max_cost=0.5)
    assert run_scripts
    assert child.config.get('max_cost') == 0.5
    assert 'max_cost: unset -> 0.5' in output


def test_start_continue_rolls_back_retune_on_refusal_or_failed_launch(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused or failed continue never persists a ``--max-cost`` retune.

    The retune lands before the launch so the config checks read the new
    cap, but a continue refused at the re-arm gate or rolled back by a
    failed ``start.sh`` must not leave the cap silently armed in the
    child's config or registry row -- the caller's authorization was for a
    launch that never happened.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # land the child's run on its budget so the continue requires --max-cost
    run_id = _active_run(child)
    _record_step_cost(child, run_id=run_id, cost=0.20)
    child.record.run_end(run_id=run_id, status='exited', exit_code=0)
    child.status_set('exited')
    # a closed width gate refuses the re-arm after the retune persisted
    parent.config.set('max_children', 0)
    with pytest.raises(ValueError, match='Max children'):
        child.start(continue_run=True, clean=True, max_cost=0.5)
    assert child.config.get('max_cost') is None
    assert child.db.read('nodes', where={'node': child.branch})[0]['max_cost'] is None
    assert child.status() == 'exited'
    # a failed launch rolls the retune back alongside the status
    parent.config.set('max_children', None)

    def boom(script: str, *args: str) -> NoReturn:
        raise RuntimeError('boom')

    monkeypatch.setattr(child, '_run_script', boom)
    with pytest.raises(RuntimeError, match='boom'):
        child.start(continue_run=True, clean=True, max_cost=0.5)
    assert child.config.get('max_cost') is None
    assert child.db.read('nodes', where={'node': child.branch})[0]['max_cost'] is None
    assert child.status() == 'exited'


def test_start_without_max_cost_warns_and_runs(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Start without a cost cap runs uncapped, warning instead of refusing.

    A token-priced agent with no priced model can only run uncapped, so a
    missing ``max_cost`` does not block start -- it proceeds with a loud
    warning that spend is untracked.
    """
    node = node_with_db
    # idle and non-user, with no max_cost configured (fixture default)
    node.status_set('idle')
    run_scripts = _stub_run_script(monkeypatch, node)
    node.start()
    assert run_scripts
    assert 'without a cost cap' in caplog.text


def test_start_continue_reconciles_crashed_active(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--continue`` recovers a crashed-but-active node whose session is gone.

    A loop that dies without ending leaves the status ``active`` with no tmux
    session, which would wedge ``--continue`` (it rejects an active status).
    With the session provably gone (one-loop-per-node), start reconciles the
    status to the honest ``exited`` terminal and proceeds -- re-arming to
    ``idle`` under the continue gate.
    """
    node = node_with_db
    # configure a cost budget (required to start)
    node.config.set('max_cost', 1.0)
    # crashed loop: status active but no tmux session
    node.status_set('active')
    # verify continue reconciles to exited and launches (stub session + shell)
    monkeypatch.setattr(node, '_tmux_session_exists', lambda: False)
    run_scripts = _stub_run_script(monkeypatch, node)
    node.start(continue_run=True)
    assert run_scripts
    # healed to exited mid-flight, then re-armed idle by the gate
    assert node.status() == 'idle'


# ------ finish / stop


def test_finish_rejects_non_active(node_with_db: Node) -> None:
    """Finish raises when node is not active."""
    node = node_with_db
    # set status to idle
    node.status_set('idle')
    # verify finish rejects
    with pytest.raises(RuntimeError, match='not active'):
        node.finish()


def test_stop_rejects_non_active(node_with_db: Node) -> None:
    """Stop raises when node is not active."""
    node = node_with_db
    # set status to idle
    node.status_set('idle')
    # verify stop rejects
    with pytest.raises(RuntimeError, match='not active'):
        node.stop()


@pytest.mark.parametrize('signal', ['finish', 'stop'])
def test_signal_rejects_active_node_without_run(
    node_with_db: Node,
    signal: str,
) -> None:
    """finish/stop reject an active node that has no run, rather than no-op.

    The loop starts a run before marking itself active, so an active node with
    zero runs only happens if the status was set directly. ``signal_set`` would
    silently drop the signal while the command reported success -- the guard must
    raise instead. ``kill`` is intentionally excluded: it tears the node down
    regardless of the audit signal.
    """
    node = node_with_db
    # active but with no run started (the point of the test)
    node.status_set('active')
    # the guard raises before any shell call, so no _run_script stub is needed
    with pytest.raises(RuntimeError, match='no run'):
        getattr(node, signal)()


def test_finish_accepts_reason(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finish sets the finish signal with a reason."""
    node = node_with_db
    # set status to active
    node.status_set('active')
    node.record.run_start()
    # call finish with reason (stub shell script)
    _stub_run_script(monkeypatch, node)
    node.finish(reason='task done')
    # verify signal was set
    assert node.record.signal_get('finish') is not None


# ------ kill


@pytest.mark.parametrize(
    argnames='recycled',
    argvalues=[True, False],
    ids=['recycled', 'recorded'],
)
def test_kill_vets_recorded_group_before_script(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    recycled: bool,
) -> None:
    """Kill drops a recycled ``.pgid`` record before the script's fallback reap.

    ``kill.sh`` falls back to the recorded process group when the pane
    lookup is empty, gating on liveness alone -- a pid the OS recycled to
    an unrelated same-user group answers that probe and would draw the
    TERM/KILL. The pre-vet arbitrates identity the way ``_reap_orphan``
    does (a leader younger than its record marks a recycled pid) and
    unlinks the stale record, so the script only ever sees vetted files;
    a genuine record survives for the fallback to use.
    """
    node = node_with_db
    node.status_set('active')
    node.record.run_start()
    # a live same-user group standing in for the recorded one; backdating
    # the record makes the leader postdate it, i.e. a recycled pid
    leader = subprocess.Popen(['sleep', '300'], start_new_session=True)
    pgid_file = node.node_dir / PGID_FILE
    try:
        pgid_file.write_text(f'{leader.pid}\n', encoding='utf-8')
        if recycled:
            stale = time.time() - 3600
            os.utime(pgid_file, (stale, stale))
        _stub_run_script(monkeypatch, node)
        node.kill()
        if recycled:
            # the stale record was dropped and the stranger spared
            assert not pgid_file.exists()
            assert leader.poll() is None
        else:
            # the genuine record survives for the script's fallback
            assert pgid_file.exists()
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        leader.wait()


def test_kill_sets_killed_status(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kill sets status to killed."""
    node = node_with_db
    # set status to active
    node.status_set('active')
    node.record.run_start()
    # kill (stub shell script)
    _stub_run_script(monkeypatch, node)
    node.kill()
    # verify status
    assert node.status() == 'killed'


def test_kill_reaps_idle_node_before_start(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Kill lands on an idle, never-started node, and start then refuses.

    An unwanted spawn is reapable the moment it registers -- without this,
    a kill only lands once the node activates, so a breach spawn gets a
    head start equal to whoever is watching. The kill needs no live
    session and no run row: it stamps ``killed``, and a later plain
    ``node start`` refuses, so the node can never activate. It is the
    designed happy path, so it is also quiet -- the run-scoped signal it
    has no run for is skipped, not warned about.
    """
    node = node_with_db
    # a never-started node: idle, no run rows, no tmux session
    monkeypatch.setattr(Node, '_tmux_session_exists', lambda self: False)
    _stub_run_script(monkeypatch, node)
    node.kill()
    assert node.status() == 'killed'
    assert 'no runs found' not in caplog.text
    # the attribution still lands: the kill event carries it alone
    events = node.db.read('events', where={'node': node.branch, 'event': 'kill'})
    assert [row['metadata'] for row in events] == ['killed by operator']
    # the killed stamp closes the start path -- the node never activates
    with pytest.raises(RuntimeError, match='Cannot start from status'):
        node.start()


def test_kill_stamps_idle_killed_before_the_reap(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle kill stamps ``killed`` under the flock, before ``kill.sh``.

    The boot window: a kill landing while a start is mid-validation finds
    no session to reap, so a stamp that trailed the reap would let the
    loop boot in between, flock-read ``idle``, stamp ``active``, and run
    forever under a ``killed`` census row -- kill.sh already no-op'd and
    the loop never polls the kill signal. The pre-reap stamp serializes
    the outcomes: the loop's flock'd boot check sees ``killed`` and
    stands down.
    """
    node = node_with_db
    # a never-started node: idle, no run rows, no tmux session
    monkeypatch.setattr(Node, '_tmux_session_exists', lambda self: False)
    stamped: list[str] = []

    def run_script(script: str, *args: str) -> subprocess.CompletedProcess[str]:
        stamped.append(node.status())
        return subprocess.CompletedProcess([script, *args], 0, '', '')

    monkeypatch.setattr(node, '_run_script', run_script)
    node.kill()
    # kill.sh fired exactly once, with the killed stamp already down
    assert stamped == ['killed']


def test_kill_marks_all_active(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kill closes every still-open lifecycle row and records the interrupted step.

    Closing is first-writer-wins (exit 1, stamped end). The kill event itself
    auto-resolves its lineage from the in-flight rows, so it names the step it
    interrupted -- while an event logged before any step ran carries none.
    """
    node = node_with_db

    # an event logged before any iteration/step has no step/iteration lineage
    run_id = node.record.run_start()
    node.record.event_start('merge')
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    step_id = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )

    # set active status (kill requires it)
    node.status_set('active')
    # kill (stub the shell script since we don't have tmux)
    _stub_run_script(monkeypatch, node)
    node.kill()

    # every open entity row is now killed, stamped with an end and exit 1
    for table in ('runs', 'iters', 'steps'):
        for row in node.db.read(table):
            assert row['status'] == 'killed'
            assert row['ended_at'] is not None
            assert row['exit_code'] == 1

    # the in-flight event is killed; the kill event itself completes
    events = {row['event']: row for row in node.db.read('events')}
    assert events['merge']['status'] == 'killed'
    assert events['kill']['status'] == 'completed'
    # the kill event auto-resolved the interrupted step; the pre-step merge did not
    assert events['kill']['step_id'] == step_id
    assert events['kill']['iter_id'] == iter_id
    assert events['merge']['step_id'] is None
    assert events['merge']['iter_id'] is None


def test_kill_keeps_loop_terminal_status_when_raced(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loop that lands its own clean terminal during the kill keeps it.

    The loop's finalize can stamp ``completed`` between the kill signal and
    the reap; the run row then closes ``completed``/0 (fenced against the
    kill's row marking), so the status stamp must not disagree by
    overwriting the terminal with ``killed``.
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.record.run_start()

    def finalize(script: str, *args: str) -> subprocess.CompletedProcess[str]:
        # the loop's finalize wins the race while kill.sh runs; the returned
        # clean result stands in for the reaped script
        node.record.run_end(run_id=run_id, status='completed', exit_code=0)
        node.status_set('completed')
        return subprocess.CompletedProcess([script, *args], 0, '', '')

    monkeypatch.setattr(node, '_run_script', finalize)
    node.kill()
    # both lifecycle surfaces agree on the loop's clean landing
    assert node.status() == 'completed'
    run = node.db.read('runs', where={'run_id': run_id})[0]
    assert run['status'] == 'completed'
    assert run['exit_code'] == 0


# ------ retire / unretire


def test_retire_sets_status(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retire sets status to retired."""
    node = node_with_db
    # set status to idle
    node.status_set('idle')
    node.record.run_start()
    # retire (stub shell script)
    _stub_run_script(monkeypatch, node)
    node.retire()
    # verify status
    assert node.status() == 'retired'


def test_retire_rejects_active(node_with_db: Node) -> None:
    """Retire raises when node is active."""
    node = node_with_db
    # set status to active
    node.status_set('active')
    # verify retire rejects
    with pytest.raises(RuntimeError):
        node.retire()


@pytest.mark.parametrize('reason', [None, 'archived: superseded by rework'])
def test_unretire_restores_pre_retire_status(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    reason: Optional[str],
) -> None:
    """Unretire restores the status the node held before it was retired.

    Retiring a completed node must not erase its completion marker: unretire
    lands back on ``completed`` (not a hard-coded ``idle``) in both stores --
    the ``.status`` file and the ``nodes`` registry row stay in lockstep. A
    retire reason rides the same event metadata after the prior status, and
    its own colons must not confuse the restore.
    """
    node = node_with_db
    # register the node so the registry row tracks the round-trip too
    node.db.write({'node': node.branch, 'status': 'completed'}, 'nodes')
    node.status_set('completed')
    node.record.run_start()
    # retire then unretire (stub shell scripts)
    _stub_run_script(monkeypatch, node)
    node.retire(reason=reason)
    node.unretire()
    # the reason rides the retire event metadata after the prior status
    retire_events = node.db.read('events', where={'event': 'retire'}, limit=1)
    expected = f'completed: {reason}' if reason else 'completed'
    assert retire_events[0]['metadata'] == expected
    # verify the pre-retire status is restored in both stores
    assert node.status() == 'completed'
    rows = node.db.read('nodes', where={'node': node.branch}, limit=1)
    assert rows[0]['status'] == 'completed'


def test_unretire_without_recorded_prior_falls_back_to_idle(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unretire falls back to idle when no retire event recorded a prior status.

    A retired node with no prior status recorded on its retire event (a
    ``.status`` file set by hand, or a retire event carrying no prior)
    has nothing to restore; unretire resets it to ``idle`` rather than
    guessing.
    """
    node = node_with_db
    # set status to retired directly -- no retire event, no recorded prior
    node.status_set('retired')
    node.record.run_start()
    # unretire (stub shell script)
    _stub_run_script(monkeypatch, node)
    node.unretire()
    # verify status
    assert node.status() == 'idle'


def test_unretire_restores_the_latest_prior_when_raced(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raced unretire restores the latest recorded prior, not a stale one.

    A rival cycle -- a winning unretire, a run to ``stopped``, a re-retire
    recording it -- lands between this caller's validation and its lock
    acquisition. The restore target is resolved under the flock, so the
    loser restores the fresh ``stopped``, never the ``completed`` the
    first retire recorded.
    """
    node = node_with_db
    node.db.write({'node': node.branch, 'status': 'completed'}, 'nodes')
    node.status_set('completed')
    node.record.run_start()
    real_flock = fcntl.flock
    raced = []

    def raced_flock(fd: object, op: int) -> None:
        # the rival's full cycle lands before this caller's acquisition,
        # re-retiring with 'stopped' recorded as the fresh prior; one-shot,
        # since the rival's own retire also acquires the flock
        if not raced:
            raced.append(True)
            node.status_set('stopped')
            node.retire()
        real_flock(fd, op)

    # retire records 'completed', then the raced unretire (stub shell scripts;
    # the flock stub lands after the retire so only the unretire races)
    _stub_run_script(monkeypatch, node)
    node.retire()
    monkeypatch.setattr('fractal.core.worktree.fcntl.flock', raced_flock)
    node.unretire()
    assert node.status() == 'stopped'


@pytest.mark.parametrize('op', ['retire', 'unretire'])
def test_retire_rejects_user(node_with_db: Node, op: str) -> None:
    """Retire/unretire raise for user nodes (the root is not retirable)."""
    node = node_with_db
    node.config.set('user', True)
    with pytest.raises(RuntimeError, match='user node'):
        getattr(node, op)()


# ------ recursive signals


@pytest.mark.parametrize('signal', ['stop', 'finish'])
def test_signals_recurse_to_active_descendants(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    signal: str,
) -> None:
    """stop/finish reach every active descendant, not just the target node."""
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # signal the parent -- the active child is signaled too (shell hooks stubbed)
    _stub_run_script(monkeypatch, Node)
    getattr(parent, signal)(reason='wrap up')
    assert parent.record.signal_get(signal) is not None
    assert child.record.signal_get(signal) is not None


@pytest.mark.parametrize('signal', ['stop', 'finish', 'kill'])
def test_recursive_signals_attribute_the_propagating_node(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    signal: str,
) -> None:
    """A propagated signal's row names the node it came from.

    A parent's budget wind-down finishes its whole subtree with the parent's
    reason; stamped verbatim on a descendant's signal row it reads as the
    descendant's OWN event -- a "cost budget reserve reached" landing far
    under the descendant's own cap is an ancestor's boundary firing
    correctly, yet files as a high-severity mis-fire. The descendant's row
    must carry the attribution; the target's own row keeps the bare reason.
    ``kill`` rows additionally lead with the killed-by attribution.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # signal the parent with a budget-style reason (shell hooks stubbed)
    _stub_run_script(monkeypatch, Node)
    getattr(parent, signal)(reason='cost budget reserve reached')
    prefix = 'killed by operator: ' if signal == 'kill' else ''
    assert parent.record.signal_get(signal) == f'{prefix}cost budget reserve reached'
    assert (
        child.record.signal_get(signal)
        == f'{prefix}cost budget reserve reached (via {signal} of main.parent)'
    )


def test_recursion_skips_inactive_descendants(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished descendant is left alone and no longer counts as active."""
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    assert parent.list(status='active', live=True)
    # the child has finished
    child.status_set('completed')
    assert not parent.list(status='active', live=True)
    # stopping the parent does not signal the finished child
    _stub_run_script(monkeypatch, Node)
    parent.stop()
    assert child.record.signal_get('stop') is None


@pytest.mark.parametrize('signal', ['stop', 'finish', 'kill'])
def test_signals_refuse_settled_target_before_the_sweep(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    signal: str,
) -> None:
    """A non-active target refuses up front, with no descendant signaled.

    The fan-out verbs sweep the subtree before acting on the target, so
    the target's own guard must run first -- otherwise a verb aimed at a
    settled node (or the idle user node) would signal or reap every
    active descendant and only then raise, reporting a refusal for what
    was in fact a tree-wide side effect.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    parent.status_set('completed')
    run_scripts = _stub_run_script(monkeypatch, Node)
    with pytest.raises(RuntimeError, match='not active'):
        getattr(parent, signal)()
    assert run_scripts == []
    # the active child was neither signaled nor reaped
    assert child.record.signal_get(signal) is None
    assert child.status() == 'active'


def test_kill_recurses_to_descendants(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kill reaps active descendants too, marking each killed."""
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    _stub_run_script(monkeypatch, Node)
    parent.kill()
    assert parent.status() == 'killed'
    assert child.status() == 'killed'


@pytest.mark.parametrize('signal', ['stop', 'finish'])
def test_signals_reach_deep_through_inactive_intermediate(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    signal: str,
) -> None:
    """stop/finish reach an active grandchild past a non-active child.

    The flat ``nodes`` registry is authoritative: a non-active intermediate
    must not hide the active grandchild below it. A parent->child (non-flat)
    walk would stop at the finished ``c`` and miss ``g`` entirely.
    """
    p, c, g = _spawn_chain(git_repo, monkeypatch)
    # signal p -- the deep active grandchild is signaled, the done child is not
    _stub_run_script(monkeypatch, Node)
    getattr(p, signal)(reason='wrap up')
    assert p.record.signal_get(signal) is not None
    assert g.record.signal_get(signal) is not None
    assert c.record.signal_get(signal) is None


def test_kill_propagates_deep_status_and_keeps_worktrees(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kill marks a deep descendant killed in an ancestor table, keeping trees."""
    p, c, g = _spawn_chain(git_repo, monkeypatch)
    _stub_run_script(monkeypatch, Node)
    p.kill()
    # the active grandchild is killed; the non-active intermediate is untouched
    assert g.status() == 'killed'
    assert c.status() == 'completed'
    # the grandchild's killed status reaches the root node's flat registry
    root = Node(git_repo)
    root_rows = {row['node']: row['status'] for row in root.db.read('nodes')}
    assert root_rows[g.branch] == 'killed'
    # kill never removes worktrees -- the whole chain stays on disk
    for node in (p, c, g):
        assert node.exists()


def test_kill_reaps_booting_descendant(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kill reaps a child whose loop is still booting, not just active ones.

    A spawn in flight when the kill lands has a tmux session (start.sh
    created it) but reads ``idle`` -- the loop stamps ``active`` only after
    its preflight. The sweep must reap it anyway: exiting the fixpoint over
    the booting child leaves it to stamp ``active`` seconds after the kill
    returns and run its whole budget in a subtree reported killed.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # rewind the child to its boot window: session live (the helper's probe
    # already presents it), run row open, 'active' not yet stamped
    child.status_set('idle')
    run_scripts = _stub_run_script(monkeypatch, Node)
    parent.kill()
    assert parent.status() == 'killed'
    # the booting child was reaped -- kill.sh ran against its worktree and
    # its open run row closed killed
    assert ('kill.sh', f'{child.worktree}') in run_scripts
    run = child.record.runs(limit=1)[0]
    assert run['status'] == 'killed'


@pytest.mark.parametrize('verb', ['stop', 'finish'])
def test_graceful_sweep_reaches_a_descendant_that_appears_mid_sweep(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
) -> None:
    """Stop and finish re-enumerate, so a start landing mid-sweep is signaled.

    A single pass covers only the descendants live when it began, so a
    child that stamped ``active`` while the sweep was signaling its
    sibling escaped with no signal row at all -- running on unattended
    under a stop, and blocking the parent's drain-wait under a finish.
    The sweep re-reads until no fresh live descendant appears, the way
    ``kill`` and ``pause`` already do.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # a second child in its boot window: registered with a run open, its
    # loop yet to stamp active
    node_dir = parent.worktree / '.fractal' / 'main.parent'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    Node(git_repo).init(name='late')
    monkeypatch.delenv('_NODE')
    late = Node(git_repo / '.worktrees' / 'main.parent.late')
    late.record.run_start()
    # present all three loops alive, so no enumeration reconciles one away
    sessions = frozenset({parent.tmux_session, child.tmux_session, late.tmux_session})
    monkeypatch.setattr('fractal.util.tmux.probe', lambda: sessions)
    # the late child wins its boot race while the sweep signals its sibling
    # -- the interleaving a single pass exited over
    signal_one = getattr(Node, f'_{verb}')

    def _signal_one(
        self: Node,
        reason: Optional[str] = None,
        *,
        fan_out: bool = False,
    ) -> None:
        if self.branch == child.branch:
            late.status_set('active')
        signal_one(self, reason, fan_out=fan_out)

    monkeypatch.setattr(Node, f'_{verb}', _signal_one)
    _stub_run_script(monkeypatch, Node)
    getattr(parent, verb)()
    # every descendant carries the signal, the late arrival included, and
    # each names the parent that ordered it
    for node in (child, late):
        row = node.record.signal_get(verb)
        assert row is not None, node.branch
    assert parent.record.signal_get(verb) is not None


# ------ merge


def test_merge_lifecycle(git_repo: pathlib.Path) -> None:
    """Init, commit, squash-merge, and verify parent has changes."""
    project_dir, branch = _init_and_commit(git_repo, 'feature')

    # squash-merge back to parent
    child_node = Node(project_dir)
    child_node.merge()

    # verify parent branch has the change
    _git(git_repo, 'checkout', 'main')
    assert (git_repo / 'feature.txt').is_file()

    # verify child branch and worktree still exist
    worktree_dir = git_repo / '.worktrees' / branch
    assert worktree_dir.exists()


def test_merge_no_op_when_nothing_to_merge(git_repo: pathlib.Path) -> None:
    """Re-merging a node with no new commits is a clean no-op, not a crash.

    A squash merge with no net change stages nothing, so ``git commit`` would
    abort with "nothing to commit"; ``merge.sh`` must report the no-op and
    exit 0 (like git's "Already up to date") instead of surfacing a RuntimeError.
    """
    project_dir, _ = _init_and_commit(git_repo, 'feature')
    child_node = Node(project_dir)

    # first merge lands the work on the parent
    child_node.merge()
    commits_after_first = _rev_count(git_repo, 'main')

    # second merge has nothing new -- a clean no-op, not a RuntimeError
    output, _ = child_node.merge()
    assert 'Nothing to merge' in output

    # no spurious empty commit landed on the parent
    assert _rev_count(git_repo, 'main') == commits_after_first


def test_merge_surfaces_script_notices(git_repo: pathlib.Path) -> None:
    """Merge returns merge.sh's success-path stderr warnings beside the output.

    A child worktree left dirty at merge time makes ``merge.sh`` skip
    advancing the branch's merge-base and warn on stderr while exiting 0;
    the pair return surfaces the warning (the CLI echoes it to stderr)
    instead of dropping it with the CompletedProcess -- the operator's
    only explanation for a later re-merge re-diffing from the fork point.
    """
    project_dir, _ = _init_and_commit(git_repo, 'feature')
    child_node = Node(project_dir)
    # dirty the child so the merge-base advance is skipped with a warning
    drift = project_dir / 'feature.txt'
    drift.write_text('uncommitted drift\n', encoding='utf-8')
    output, notices = child_node.merge()
    assert output
    assert 'skipped advancing' in notices


def test_merge_excludes_merged_node_seed(git_repo: pathlib.Path) -> None:
    """Squash-merge must not pull the merged node's own seed dir into the parent.

    ``merge --squash`` stages the child's entire ``.fractal/<branch>/`` seed
    (NODE.md, steps, scripts, memory -- ~30 files) alongside its real work.
    Committing that seed orphans it in the parent tree once the node is deleted,
    accumulating one dir per merge; ``merge.sh`` must strip the merged node's own
    seed so the parent gains only real work. Re-merging then re-stages only the
    seed, which must strip back to a clean no-op rather than an empty commit.
    """
    project_dir, branch = _init_and_commit(git_repo, 'feature')

    # the loop's COMMIT step tracks the node's own seed dir on its branch;
    # replicate that here so the squash has a committed seed to pull in
    _git(project_dir, 'add', f'.fractal/{branch}')
    _git(project_dir, 'commit', '-m', 'commit node seed')

    # squash-merge back into the parent
    child_node = Node(project_dir)
    child_node.merge()

    # the child's real work landed on the parent...
    assert (git_repo / 'feature.txt').is_file()

    # ...but its own seed dir did not -- neither in the parent's working tree...
    assert not (git_repo / '.fractal' / branch).exists()

    # ...nor tracked in the parent's merge commit
    tracked = _git(git_repo, 'ls-files', f'.fractal/{branch}')
    assert tracked.stdout.strip() == ''

    # re-merging re-stages the seed (the parent still lacks it), strips it, and
    # finds nothing new -- the strip degrades to the clean no-op path, not an
    # empty-commit crash
    output, _ = child_node.merge()
    assert 'Nothing to merge' in output


def test_merge_excludes_subproject_node_seed(git_repo: pathlib.Path) -> None:
    """Squash-merge strips a monorepo node's ``<project>/.fractal`` seed.

    The sub-project (``project != "."``) counterpart of
    ``test_merge_excludes_merged_node_seed``: such a node's seed lives at
    ``<project>/.fractal/<branch>``, so ``merge.sh`` must strip it rooted at the
    project dir, not just at the repo root, leaving the parent only real work.
    """
    # commit a sub-project wiki -- the base-ref precondition for child init
    app = git_repo / 'app'
    app.mkdir()
    (app / 'wiki').mkdir()
    (app / 'wiki' / '_index.md').write_text(
        '---\nname: app\n---\n# app\n\n***\n',
        encoding='utf-8',
    )
    _git(git_repo, 'add', 'app')
    _git(git_repo, 'commit', '-m', 'add app wiki')

    # a sub-project user node, then a child that inherits project 'app'
    Node(git_repo).init(path='app', agent='claude', user=True)
    Node(git_repo).init(name='feature')
    worktree = git_repo / '.worktrees' / 'main.feature'
    branch = 'main.feature'

    # commit the child's own seed (under app/.fractal/<branch>) plus real work,
    # mirroring what the loop's COMMIT step tracks on the branch
    (worktree / 'app' / 'feature.txt').write_text('real work\n', encoding='utf-8')
    _git(worktree, 'add', f'app/.fractal/{branch}', 'app/feature.txt')
    _git(worktree, 'commit', '-m', 'work + seed')

    # squash-merge into the parent (the app user node, branch main)
    Node(worktree).merge()

    # the child's real work landed under the parent's app/...
    assert (git_repo / 'app' / 'feature.txt').is_file()
    # ...but its own seed did not -- neither in the parent's working tree...
    assert not (git_repo / 'app' / '.fractal' / branch).exists()
    # ...nor tracked in the parent's merge commit
    tracked = _git(git_repo, 'ls-files', f'app/.fractal/{branch}')
    assert tracked.stdout.strip() == ''

    # re-merging re-stages only the seed, strips it, and degrades to a no-op
    output, _ = Node(worktree).merge()
    assert 'Nothing to merge' in output


def test_merge_refreshes_parent_wiki_indexes(git_repo: pathlib.Path) -> None:
    """The squash-merge folds regenerated wiki indexes into the merge commit.

    The ``_index.md`` merge driver keeps ours per link block, dropping the
    merged branch's link rows, so ``merge.sh`` runs ``wiki update`` on the
    parent's tracked wikis after the squash and stages the refreshed bytes --
    the merge commit carries current indexes and leaves the parent clean.
    The staging covers only what the refresh owns: an operator's untracked
    draft under the parent wiki never rides the merge commit.
    """
    project_dir, _ = _init_and_commit(git_repo, 'feature')
    # an untracked operator draft in the parent wiki (outside the refresh)
    (git_repo / 'wiki' / 'draft.txt').write_text('operator scratch\n', encoding='utf-8')
    # the child commits a wiki page, leaving the generated index stale
    (project_dir / 'wiki' / 'topic.md').write_text(
        '---\nname: topic\ndesc: A topic page.\n---\n\n# topic\n\n***\n',
        encoding='utf-8',
    )
    _git(project_dir, 'add', 'wiki/topic.md')
    _git(project_dir, 'commit', '-m', 'add topic page')

    # squash-merge into the parent (main)
    Node(project_dir).merge()

    # the merge commit carries the regenerated index row for the new page...
    index = _git(git_repo, 'show', 'main:wiki/_index.md').stdout
    assert '[[topic' in index
    # ...the untracked parent draft stayed out of the merge commit...
    tracked = _git(git_repo, 'ls-files', 'wiki/draft.txt').stdout
    assert tracked.strip() == ''
    # ...and the refresh left no residue beyond the draft
    status = _git(git_repo, 'status', '--porcelain').stdout
    assert status.strip() == '?? wiki/draft.txt'


def test_merge_restores_parent_when_index_refresh_fails(
    git_repo: pathlib.Path,
) -> None:
    """A refused index refresh restores the parent exactly like a conflict.

    ``wiki update`` refuses a wiki carrying merge conflict markers; landing
    the squash anyway would commit a broken wiki, so ``merge.sh`` resets the
    parent and fails with the refusal in the error.
    """
    project_dir, _ = _init_and_commit(git_repo, 'feature')
    # a settings-complete parent wiki (the shape `wiki init` builds), so the
    # refusal is the only thing update does -- no restored-file residue
    settings = git_repo / 'wiki' / '.wiki' / 'settings.json'
    settings.parent.mkdir()
    settings.write_text('{}\n', encoding='utf-8')
    _git(git_repo, 'add', 'wiki/.wiki/settings.json')
    _git(git_repo, 'commit', '-m', 'wiki settings')
    # the child commits a conflict-marked wiki page -- update refuses it
    (project_dir / 'wiki' / 'broken.md').write_text(
        '---\nname: broken\ndesc: A broken page.\n---\n\n# broken\n\n***\n'
        '<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> other\n',
        encoding='utf-8',
    )
    _git(project_dir, 'add', 'wiki/broken.md')
    _git(project_dir, 'commit', '-m', 'add broken page')
    pre_head = _git(git_repo, 'rev-parse', 'HEAD').stdout.strip()

    with pytest.raises(RuntimeError, match='Merge conflict markers'):
        Node(project_dir).merge()

    # the failed merge landed nothing on the parent...
    head = _git(git_repo, 'rev-parse', 'HEAD').stdout.strip()
    assert head == pre_head
    # ...and left no staged or working residue behind
    status = _git(git_repo, 'status', '--porcelain').stdout
    assert status == ''


def test_merge_refuses_when_parent_worktree_is_dirty(git_repo: pathlib.Path) -> None:
    """Merge refuses, preserving the parent's work, when the parent is dirty.

    The squash and the restore-on-failure ``reset --hard HEAD`` would otherwise
    absorb or destroy the parent's own uncommitted (tracked) changes, so merge.sh
    bails up front and leaves them intact.
    """
    project_dir, _ = _init_and_commit(git_repo, 'feature')
    # a tracked, uncommitted change in the parent (main) worktree
    parent_file = git_repo / 'parent_local.txt'
    parent_file.write_text('v1\n', encoding='utf-8')
    _git(git_repo, 'add', 'parent_local.txt')
    _git(git_repo, 'commit', '-m', 'parent file')
    parent_file.write_text('uncommitted edit\n', encoding='utf-8')

    with pytest.raises(RuntimeError, match='uncommitted changes'):
        Node(project_dir).merge()

    # the parent's uncommitted work survived (reset --hard never ran) and the
    # child's work did not land on the parent
    assert parent_file.read_text(encoding='utf-8') == 'uncommitted edit\n'
    assert not (git_repo / 'feature.txt').is_file()


def test_merge_refuses_settled_child_into_a_running_target(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Merging into a live (or paused) target refuses; the target's own loop may.

    ``merge.sh`` squashes, refreshes indexes, and commits inside the target
    worktree, and its failure paths ``reset --hard`` -- racing the target
    loop's own writes and commit backstop would absorb or destroy that loop's
    fresh work. An outside merge of a settled child therefore refuses while
    the target is active or paused, while the target's own loop (which merges
    its settled children as part of its normal iteration, single-actor) still
    merges freely.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    parent_wt = git_repo / '.worktrees' / 'main.parent'
    child_wt = git_repo / '.worktrees' / 'main.parent.kid'
    # the child settles with committed work; the parent's loop stays live
    (child_wt / 'work.txt').write_text('child work\n', encoding='utf-8')
    _git(child_wt, 'add', 'work.txt')
    _git(child_wt, 'commit', '-m', 'child work')
    child.status_set('completed')

    # an outside merge refuses while the target's loop is running...
    with pytest.raises(RuntimeError, match='active target'):
        child.merge()
    # ...and while the target is parked paused (its run frozen mid-flight)
    parent.status_set('paused')
    with pytest.raises(RuntimeError, match='paused target'):
        child.merge()
    # neither refusal touched the running target's worktree
    assert not (parent_wt / 'work.txt').exists()

    # the target's own loop merges the settled child while active: the loop
    # is blocked on the very agent step running the merge, so nothing races
    parent.status_set('active')
    node_dir = parent_wt / '.fractal' / 'main.parent'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    output, _ = child.merge()
    assert 'Squash-merged' in output
    assert (parent_wt / 'work.txt').is_file()


def test_merge_event_survives_child_delete(git_repo: pathlib.Path) -> None:
    """The parent-side ``merge`` event outlives the merged child's deletion.

    ``merge`` is logged on the *parent* (the surviving target), not the child.
    Were it logged on the child, the child's deletion would destroy the only
    record of it.
    """
    project_dir, branch = _init_and_commit(git_repo, 'feature')
    parent = Node(git_repo)

    # squash-merge the child into its parent -- the event lands on the parent
    Node(project_dir).merge()
    merges = parent.db.read('events', where={'event': 'merge'})
    assert [row['metadata'] for row in merges] == [f'{branch} -> main']

    # delete the merged child; its worktree (and its own DB) are torn down
    Node(project_dir).delete()
    assert not project_dir.exists()

    # the merge record survives on the parent (it was never on the child), and
    # the delete is recorded there too -- the whole trail lives on the survivor
    survived = parent.db.read('events', where={'event': 'merge'})
    assert [row['metadata'] for row in survived] == [f'{branch} -> main']
    deletes = parent.db.read('events', where={'event': 'delete'})
    assert [row['metadata'] for row in deletes] == [branch]


# ------ helpers


def _init_and_commit(
    git_repo: pathlib.Path,
    name: str,
) -> tuple[pathlib.Path, str]:
    """Init a node and make a commit in its worktree."""
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    output = node.init(name=name)
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    # configure git user in worktree
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    # make a change and commit
    test_file = project_dir / f'{name}.txt'
    test_file.write_text(f'hello from {name}\n', encoding='utf-8')
    _git(project_dir, 'add', f'{name}.txt')
    _git(project_dir, 'commit', '-m', 'test change')
    return project_dir, branch


def _rev_count(git_repo: pathlib.Path, branch: str) -> int:
    """Count the commits reachable from ``branch`` in ``git_repo``."""
    result = _git(git_repo, 'rev-list', '--count', branch)
    return int(result.stdout.strip())
