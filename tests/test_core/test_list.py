"""The list overlay pipeline.

Covers default vs ``--all`` filtering, the live view (trusting real
state and relabeling crashed-active rows), config-cap overlays over
stale registry rows, orphan flagging, and the ``last`` activity-age
column with its staleness flag, plus the spend reading and the split of
the status qualifier into its own column.
"""

from __future__ import annotations

import pathlib
from typing import Optional

import pytest

from fractal.constants import SOCKET_FILE
from fractal.core.node import Node
from tests._helpers import _past_timestamp

from .conftest import _active_run, _record_step_cost, _spawn_parent_child

__all__ = [
    'test_list_returns_nodes',
    'test_list_hides_retired',
    'test_list_all_shows_retired',
    'test_list_live_trusts_real_state',
    'test_list_live_relabels_crashed_active',
    'test_list_live_reads_booting_idle_as_active',
    'test_list_live_confirms_relabels_on_recorded_socket',
    'test_list_renders_config_caps_over_stale_registry',
    'test_list_decorates_exited_with_run_reason',
    'test_list_reports_run_scoped_subtree_spend',
    'test_list_flags_orphan_rows',
    'test_list_renders_last_activity_age',
    'test_list_flags_stale_active_rows',
    'test_list_flags_iteration_gaps',
    'test_list_flags_billing_backoff',
]


# ------ listing


def test_list_returns_nodes(node_with_db: Node) -> None:
    """List returns child node records."""
    node = node_with_db
    # register a child
    node.child_add('backend', max_cost=10.0)
    # list nodes
    nodes = node.list()
    assert len(nodes) >= 1
    for row in nodes:
        assert 'status' in row
        assert 'node' in row


def test_list_hides_retired(node_with_db: Node) -> None:
    """List excludes retired nodes by default."""
    node = node_with_db
    # register a child and set it to retired
    node.child_add('hidden')
    branch = f'{node.branch}.hidden'
    node.db.update({'status': 'retired'}, 'nodes', where={'node': branch})
    # verify retired node is hidden
    nodes = node.list()
    branches = {row['node'] for row in nodes}
    assert branch not in branches


def test_list_all_shows_retired(node_with_db: Node) -> None:
    """List with all_nodes includes retired nodes."""
    node = node_with_db
    # register a child and set it to retired
    node.child_add('archived')
    branch = f'{node.branch}.archived'
    node.db.update({'status': 'retired'}, 'nodes', where={'node': branch})
    # verify retired node is included with all_nodes
    nodes = node.list(all_nodes=True)
    branches = {row['node'] for row in nodes}
    assert branch in branches


# ------ live view and overlays


def test_list_live_trusts_real_state(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``live`` reflects each child's real status and drops gone worktrees."""
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # the live reconcile relabels a crashed active node (no tmux session) to
    # exited, so present this child's session as alive to test the active case
    monkeypatch.setattr(
        'fractal.util.tmux.probe',
        lambda: frozenset({child.tmux_session}),
    )
    # corrupt the parent's cached registry: stale status for the real child,
    # plus a phantom descendant that has no worktree
    parent.db.update({'status': 'completed'}, 'nodes', where={'node': child.branch})
    parent.db.merge({'node': 'main.parent.ghost', 'status': 'active'}, 'nodes')

    # the cached listing believes the registry verbatim (the phantom, worktree
    # gone, is flagged orphan rather than dropped)
    cached = {row['node']: row['status'] for row in parent.list()}
    assert cached[child.branch] == 'completed'
    assert cached['main.parent.ghost'] == 'orphan'

    # the live listing trusts the child's real status and drops the phantom
    live = {row['node']: row['status'] for row in parent.list(live=True)}
    assert live[child.branch] == 'active'
    assert 'main.parent.ghost' not in live


def test_list_live_relabels_crashed_active(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--live`` reads an active node with no tmux session as exited.

    A loop that crashed leaves ``.status`` 'active' with no live session;
    ``--live`` is the authoritative view, so it relabels that to 'exited' (a
    settled-vs-crashed check can trust it) -- without persisting the change,
    even through the decoration pass (the CLI's default), which skips the
    relabeled row rather than reach ``status_display``'s reconcile.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # no live tmux sessions -> the active child reads as crashed
    monkeypatch.setattr(
        'fractal.util.tmux.probe',
        frozenset,
    )
    rows = parent.list(live=True, decorated=True)
    live = {row['node']: row['status'] for row in rows}
    assert live[child.branch] == 'exited'
    # display-only: the child's own .status file is untouched
    assert child.status() == 'active'


def test_list_live_reads_booting_idle_as_active(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--live`` reads an idle node with a live tmux session as active.

    A started child holds 'idle' from start.sh's launch until its loop
    stamps 'active' after preflight, but its tmux session is already
    live -- the boot window. The live view reads it as active: a
    finishing parent's drain probes ``status='active,paused'`` live, and
    an 'idle' reading there would let the parent complete over a child
    started seconds earlier. Display-only, and a sessionless idle node
    (spawned, never started) stays idle -- it must never block a drain.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # the boot window: .status still 'idle', the tmux session already live
    child.status_set('idle')
    live = {row['node']: row['status'] for row in parent.list(live=True)}
    assert live[child.branch] == 'active'
    # the finish drain's exact probe must see the booting child
    draining = parent.list(status='active,paused', live=True, decorated=False)
    assert child.branch in {row['node'] for row in draining}
    # display-only: the child's own .status file is untouched
    assert child.status() == 'idle'
    # no live session (a spawned child never started) stays idle
    monkeypatch.setattr('fractal.util.tmux.probe', frozenset)
    live = {row['node']: row['status'] for row in parent.list(live=True)}
    assert live[child.branch] == 'idle'


def test_list_live_confirms_relabels_on_recorded_socket(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--live`` confirms each relabel on the node's recorded socket.

    The batched ambient probe only nominates candidates: a loop alive on
    the tmux server recorded at boot (``.socket``) is invisible to a
    shell resolving a different server, and relabeling from that ambient
    answer alone would echo a live loop as ``exited`` -- and let a
    finishing ancestor's drain miss a child booting on the recorded
    server.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    socket_file = child.node_dir / SOCKET_FILE
    socket_file.write_text('other-sock\n', encoding='utf-8')

    def probe(socket: Optional[str] = None) -> frozenset[str]:
        if socket == 'other-sock':
            return frozenset({child.tmux_session})
        return frozenset()

    monkeypatch.setattr('fractal.util.tmux.probe', probe)
    # the active child stays active -- the recorded socket confirms it lives
    live = {row['node']: row['status'] for row in parent.list(live=True)}
    assert live[child.branch] == 'active'
    # the boot window on the recorded socket reads active the same way
    child.status_set('idle')
    live = {row['node']: row['status'] for row in parent.list(live=True)}
    assert live[child.branch] == 'active'


def test_list_renders_config_caps_over_stale_registry(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listings render a present child's config caps, not the stale row.

    A rescue top-up edits the child's config directly (no ``node update``),
    so the registry row keeps the pre-rescue cap and ``node list`` lies to
    the parent verifying the top-up landed. Config is enforcement truth, so
    both listing flavors must render it -- display-only, the row itself
    stays a cache (it heals at ``node update`` and exit).
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # seed the registry cap via the blessed path, then top up config only --
    # the rescue move (config edit + continue, no node update)
    parent.child_update('kid', max_cost=12.0)
    child.config.set('max_cost', 15.0)
    # both listing flavors render the config cap
    cached = {row['node']: row['max_cost'] for row in parent.list()}
    assert cached[child.branch] == 15.0
    monkeypatch.setattr(
        'fractal.util.tmux.probe',
        lambda: frozenset({child.tmux_session}),
    )
    live = {row['node']: row['max_cost'] for row in parent.list(live=True)}
    assert live[child.branch] == 15.0
    # display-only: the registry row keeps its cache until update/exit heals
    row = child.db.read('nodes', where={'node': child.branch}, limit=1)[0]
    assert row['max_cost'] == 12.0


def test_list_decorates_exited_with_run_reason(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decorated listing carries an exited child's end reason in ``detail``.

    The reason rides its own column, never a suffix on ``status``: end
    reasons contain parentheses of their own (a budget landing quotes the
    figures), so a consumer splitting a composed ``exited (<reason>)`` back
    apart cannot do it reliably. ``status`` stays bare either way, and the
    undecorated listing stays cheap by leaving ``detail`` empty.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # land the child's run on its budget with the boundary's reason recorded
    reason = 'subtree cost budget reached (spent $6.0000 >= $5.0 max)'
    run_id = _active_run(child)
    child.record.run_end(run_id=run_id, status='exited', exit_code=0, metadata=reason)
    child.status_set('exited')
    # the decorated listing carries the reason whole, beside a bare status
    decorated = {row['node']: row for row in parent.list(decorated=True)}
    assert decorated[child.branch]['status'] == 'exited'
    assert decorated[child.branch]['detail'] == reason
    plain = {row['node']: row for row in parent.list()}
    assert plain[child.branch]['status'] == 'exited'
    assert not plain[child.branch]['detail']
    # the filter selects on the status column itself
    filtered = parent.list(status='exited', decorated=True)
    assert [row['node'] for row in filtered] == [child.branch]


def test_list_reports_run_scoped_subtree_spend(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``spend`` reads at the scope its ``max_cost`` neighbour is enforced at.

    The cap is a per-**run** ceiling over the run's whole subtree, so the
    column beside it must be the same reading -- the current run's spend
    including descendant runs chained under it -- or an operator comparing
    the two columns misjudges the headroom. A node that has never run has
    no reading to give and stays blank rather than claiming a spend of $0.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    _record_step_cost(parent, run_id=_active_run(parent), cost=2.5)
    _record_step_cost(child, run_id=_active_run(child), cost=1.25)

    # from the root: the parent's row carries its own spend plus the child
    # run chained under it, the child's row carries its own
    rows = {row['node']: row for row in Node(git_repo).list()}
    assert rows[parent.branch]['spend'] == pytest.approx(3.75)
    assert rows[child.branch]['spend'] == pytest.approx(1.25)

    # a spawned-but-never-started sibling has no run to read
    node_dir = parent.node_dir
    monkeypatch.setenv('_NODE', f'{node_dir}')
    Node(git_repo).init(name='fresh')
    monkeypatch.delenv('_NODE')
    rows = {row['node']: row for row in Node(git_repo).list()}
    assert rows[f'{parent.branch}.fresh']['spend'] is None


def test_list_flags_orphan_rows(node_with_db: Node) -> None:
    """Plain ``list`` flags a registry row whose worktree is gone as orphan.

    A phantom node (worktree removed out of band) would otherwise render as a
    healthy 'idle'; plain list stays a pure reader but marks it 'orphan'.
    """
    node = node_with_db
    # a registry-only child (child_add registers a row but builds no worktree)
    node.child_add('phantom')
    branch = f'{node.branch}.phantom'
    rows = {row['node']: row['status'] for row in node.list()}
    assert rows[branch] == 'orphan'


# ------ last-activity ages


def test_list_renders_last_activity_age(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``last`` renders the age of each node's newest activity instant.

    The newest instant wins -- an aged event must not shadow a fresh run
    start -- and a registry-only row with no recorded activity stays blank.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # age the child's events; its run start stays fresh
    child.db.update(
        data={'created_at': _past_timestamp(30 * 60)},
        table='events',
        where={'node': child.branch},
    )
    parent.child_add('phantom')
    rows = {row['node']: row['last'] for row in parent.list()}
    # the fresh run start wins over the aged events (seconds, not '30m')
    assert rows[child.branch].endswith('s')
    assert '!' not in rows[child.branch]
    assert rows[f'{parent.branch}.phantom'] is None


def test_list_flags_stale_active_rows(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active node quiet past ``max(step_timeout, 5m)`` is flagged stale.

    The ``!`` suffix marks an active loop that has written nothing for
    longer than a step should take: past the 5m floor with no
    ``step_timeout``, lifted by a live ``step_timeout`` above the age.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # push every activity instant on the child past the 5m floor
    stamp = _past_timestamp(20 * 60)
    child.db.update({'created_at': stamp}, 'events', where={'node': child.branch})
    child.db.update({'started_at': stamp}, 'runs', where={'node': child.branch})
    rows = {row['node']: row['last'] for row in parent.list()}
    assert rows[child.branch] == '20m!'
    # a step_timeout above the age lifts the flag (the 5m floor is a max)
    child.config.set('step_timeout', '1h')
    rows = {row['node']: row['last'] for row in parent.list()}
    assert rows[child.branch] == '20m'
    # a settled node is never flagged, however old its last activity
    child.config.set('step_timeout', '1m')
    child.status_set('completed')
    rows = {row['node']: row['last'] for row in parent.list()}
    assert rows[child.branch] == '20m'


def test_list_flags_iteration_gaps(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Iteration numbers that jump with no recorded row flag the listing.

    Recorded iteration rows are the execution trace: a run whose numbers
    jump (2 recorded, then 5) consumed iterations that never executed --
    the class a fleet transient once produced fleet-wide with zero trace.
    Contiguous rows never flag; the gap names the missing span.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    run_id = child.record.runs(limit=1)[0]['run_id']
    for number in (1, 2):
        iter_id = child.record.iter_start(run_id=run_id, iter=number)
        child.record.iter_end(iter_id=iter_id, status='completed', exit_code=0)
    # contiguous iterations: no gap flag
    rows = {row['node']: row['detail'] for row in parent.list(decorated=True)}
    assert 'iteration gap' not in (rows[child.branch] or '')
    # a jump to 5 leaves 3-4 unexecuted -- the detail names the span
    iter_id = child.record.iter_start(run_id=run_id, iter=5)
    child.record.iter_end(iter_id=iter_id, status='completed', exit_code=0)
    rows = {row['node']: row['detail'] for row in parent.list(decorated=True)}
    assert f'iteration gap {run_id}.3-{run_id}.4' in rows[child.branch]


def test_list_flags_billing_backoff(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three instant zero-cost failures flag the census PAUSED: billing.

    The credit-crash signature: consecutive newest launches failed
    instantly at $0, so the loop is backing off dead credits and the
    listing must say so loudly. A launch that spent real money breaks the
    streak -- an expensive genuine failure never reads as an outage --
    and so does a cannot-exec launch, the class the loop's breaker
    excludes (a broken agent install is hot-retrying, not backing off).
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    run_id = child.record.runs(limit=1)[0]['run_id']
    iter_id = child.record.iter_start(run_id=run_id, iter=1)

    def _failed_step(number: int, cost: Optional[float], metadata: str = '') -> None:
        step_id = child.record.step_start(
            iter_id=iter_id,
            run_id=run_id,
            step=number,
            step_name='EXECUTE',
        )
        child.record.step_end(
            step_id=step_id, status='failed', exit_code=1, metadata=metadata
        )
        if cost is not None:
            child.record.step_cost(step_id=step_id, cost=cost)

    for number in (1, 2, 3):
        _failed_step(number, 0.0)
    rows = {row['node']: row['detail'] for row in parent.list(decorated=True)}
    assert 'PAUSED: billing' in rows[child.branch]
    # a paid failure on top breaks the streak -- not an outage
    _failed_step(4, 0.75)
    rows = {row['node']: row['detail'] for row in parent.list(decorated=True)}
    assert 'PAUSED: billing' not in (rows[child.branch] or '')
    # cannot-exec launches book failed/instant with no cost, but the loop's
    # breaker excludes their class -- the census must too, retry marker and
    # all, or a broken agent install reads as a credit outage
    reason = 'agent launch failed: [Errno 2] No such file or directory'
    for number, metadata in ((5, reason), (6, reason), (7, f'{reason}; retry')):
        _failed_step(number, None, metadata=metadata)
    rows = {row['node']: row['detail'] for row in parent.list(decorated=True)}
    assert 'PAUSED: billing' not in (rows[child.branch] or '')
