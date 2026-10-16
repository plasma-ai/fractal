"""Pause/resume semantics and the tree pause latch.

Covers the paused state's guard matrix (only resume, kill, merge, and
chat legal), the parent-first pause and leaf-first resume fan-out,
pausing-node withdrawal, and the user-node latch that blocks spawns,
starts, and targeted resumes tree-wide.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Optional

import pytest

from fractal.core.node import Node
from tests._helpers import _git, _stub_run_script

from .conftest import _spawn_parent_child

__all__ = [
    'test_pause_rejects_non_active',
    'test_pause_signals_and_decorates',
    'test_paused_rejects_all_but_resume_kill_merge_and_chat',
    'test_kill_reaps_a_paused_node',
    'test_reconcile_leaves_paused_untouched',
    'test_resume_requires_paused',
    'test_resume_withdraws_a_pausing_node',
    'test_pause_fans_out_top_down_and_resume_leaf_first',
    'test_abort_names_a_git_operation_left_in_progress',
    'test_pause_latch_blocks_spawn_and_start',
    'test_pause_latch_blocks_targeted_resume',
    'test_tree_pause_latches_depth_one',
]


# ------ pause / resume


def test_pause_rejects_non_active(node_with_db: Node) -> None:
    """Pause raises when node is not active."""
    node = node_with_db
    # set status to idle
    node.status_set('idle')
    # verify pause rejects
    with pytest.raises(RuntimeError, match='not active'):
        node.pause()


def test_pause_signals_and_decorates(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pause sets the pause signal, logs its event, and decorates the display."""
    node = node_with_db
    node.status_set('active')
    node.record.run_start()
    # pause (stub shell script: the abort is pause.sh's job)
    _stub_run_script(monkeypatch, node)
    result = node.pause(reason='cooling off')
    assert 'Pause signal sent to 1 node' in result
    # the signal carries the reason and the display shows the pending park
    assert node.record.signal_get('pause') == 'cooling off'
    assert node.status_display() == 'active (pausing)'
    # the pause event is bracketed completed
    events = node.db.read('events', where={'node': node.branch, 'event': 'pause'})
    assert [event['status'] for event in events] == ['completed']


def test_paused_rejects_all_but_resume_kill_merge_and_chat(
    node_with_db: Node,
) -> None:
    """A paused node admits only resume, kill, merge, and (fork-only) chat.

    Delete/retire would act on the frozen mid-step worktree, and start
    (fresh or ``--continue``) would git-clean it and re-arm the budget --
    every other path must refuse and name the way out.
    """
    node = node_with_db
    node.status_set('paused')
    with pytest.raises(RuntimeError, match='Resume or kill it first'):
        node.delete()
    with pytest.raises(RuntimeError, match='Resume or kill it first'):
        node.retire()
    with pytest.raises(RuntimeError, match='Resume it first'):
        node.start()
    with pytest.raises(RuntimeError, match='Resume it first'):
        node.start(continue_run=True)


def test_kill_reaps_a_paused_node(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kill accepts a paused node and closes its open rows ``killed``.

    The escape hatch: a parked subtree has no loop to signal, so kill is
    pure bookkeeping -- rows close, the status lands ``killed``, and the
    node becomes continue-eligible.
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.record.run_start()
    node.status_set('paused')
    # kill the parked node (stub shell script: nothing is alive to reap)
    _stub_run_script(monkeypatch, node)
    node.kill(reason='abandoning the experiment')
    assert node.status() == 'killed'
    # the open run row closed killed
    run = node.db.read('runs', where={'run_id': run_id})[0]
    assert run['status'] == 'killed'
    assert run['ended_at'] is not None


def test_reconcile_leaves_paused_untouched(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paused node with no tmux session is parked, not crashed.

    No session is paused's *normal* state (the loop exits at pause; resume
    relaunches it, on this host or after a transplant) -- the crashed-active
    heal must never relabel it ``exited`` or close its open rows.
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.record.run_start()
    node.status_set('paused')
    # the loop is gone -- exactly how a paused node looks
    monkeypatch.setattr(node, '_tmux_session_exists', lambda: False)
    # a reject-active op runs the reconcile first; it must land on the
    # paused guard, not relabel the node exited and delete it
    with pytest.raises(RuntimeError, match='paused'):
        node.delete()
    assert node.status() == 'paused'
    # the open run row survived for resume to adopt
    run = node.db.read('runs', where={'run_id': run_id})[0]
    assert run['ended_at'] is None


def test_resume_requires_paused(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume raises unless the node is paused, then relaunches it."""
    node = node_with_db
    node.status_set('idle')
    # verify resume rejects
    with pytest.raises(RuntimeError, match='not paused'):
        node.resume()
    # a paused node resumes (stub shell script: the relaunch is resume.sh's job)
    node.status_set('paused')
    _stub_run_script(monkeypatch, node)
    result = node.resume()
    assert 'Resumed 1 node' in result
    # the resume event is bracketed completed
    events = node.db.read('events', where={'node': node.branch, 'event': 'resume'})
    assert [event['status'] for event in events] == ['completed']
    # resume heals the exclude block -- the relaunch path skips start's
    # refresh, and this repo carried no info/exclude at all
    exclude = node.repo_dir / '.git' / 'info' / 'exclude'
    assert '**/skills/.system/' in exclude.read_text(encoding='utf-8')


def test_resume_withdraws_a_pausing_node(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume on a still-parking node withdraws its pause instead of failing.

    Between the pause command and the loop's park the node is ``active``
    with a pending pause signal; a resume in that window cannot relaunch a
    live loop, so it clears the signal -- the loop then never parks -- and
    closes the pause span for the deadline credit.
    """
    node = node_with_db
    node.status_set('active')
    node.record.run_start()
    _stub_run_script(monkeypatch, node)
    node.pause(reason='hold')
    assert node.record.signal_get('pause') is not None
    # resume in the parking window: withdrawal, never a relaunch
    # (a fresh recorder: the pause above already ran its script)
    run_scripts = _stub_run_script(monkeypatch, node)
    result = node.resume()
    assert 'Resumed 1 node' in result
    assert node.record.signal_get('pause') is None
    assert run_scripts == []
    # the pause span is closed for the credit walk
    events = node.db.read('events', where={'node': node.branch, 'event': 'resume'})
    assert [event['status'] for event in events] == ['completed']


def test_pause_fans_out_top_down_and_resume_leaf_first(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pause reaches the parent before the child; resume inverts the order.

    Top-down pause means a parent parks before its children and can never
    drain-complete over them mid-fan-out; leaf-first resume means every
    child is running again before its parent's drain-waits can look. The
    event rows record the actual order.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # pause the parent -- the active child is signaled too, with attribution
    _stub_run_script(monkeypatch, Node)
    result = parent.pause(reason='hold')
    assert 'Pause signal sent to 2 nodes' in result
    assert parent.record.signal_get('pause') == 'hold'
    assert child.record.signal_get('pause') == 'hold (via pause of main.parent)'
    # the fan-out ran parent first (shallowest first)
    pause_events = parent.db.read('events', where={'event': 'pause'})
    pause_events.sort(key=lambda event: event['event_id'])
    assert [event['node'] for event in pause_events] == [
        'main.parent',
        'main.parent.kid',
    ]
    # both loops park (simulated -- the loops are stubbed here)
    parent.status_set('paused')
    child.status_set('paused')
    # resume the parent -- the child relaunches first (deepest first)
    result = parent.resume()
    assert 'Resumed 2 nodes' in result
    resume_events = parent.db.read('events', where={'event': 'resume'})
    resume_events.sort(key=lambda event: event['event_id'])
    assert [event['node'] for event in resume_events] == [
        'main.parent.kid',
        'main.parent',
    ]


@pytest.mark.parametrize('verb', ['pause', 'kill'])
@pytest.mark.parametrize(
    argnames='marker',
    argvalues=[
        # a merge cut short leaves its head beside the staged result
        'MERGE_HEAD',
        # a rebase cut short leaves its state folder
        'rebase-merge',
        # a git command killed mid-write leaves the index locked
        'index.lock',
        # nothing left in progress
        None,
    ],
    ids=['merge', 'rebase', 'index-lock', 'clean'],
)
def test_abort_names_a_git_operation_left_in_progress(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    verb: str,
    marker: Optional[str],
) -> None:
    """Pause and kill name a git operation the abort left in the worktree.

    An agent killed mid-merge leaves the merge in its node's worktree, where
    a later merge of the node trips over it. Planted where git keeps it for
    the child's linked worktree, each kind of leftover draws exactly one
    warning naming it, whatever the abort found in flight; a clean
    worktree draws none.
    """
    _, child = _spawn_parent_child(git_repo, monkeypatch)
    _stub_run_script(monkeypatch, Node)
    if marker is not None:
        relpath = _git(child.worktree, 'rev-parse', '--git-path', marker).stdout
        path = child.worktree / relpath.strip()
        if marker == 'rebase-merge':
            path.mkdir()
        else:
            path.write_text('0' * 40 + '\n', encoding='utf-8')
    caplog.set_level(logging.WARNING)
    getattr(child, verb)(reason='brake')
    named = [
        record.getMessage()
        for record in caplog.records
        if 'git operation in progress' in record.getMessage()
    ]
    expected = [
        f'Warning: {child.worktree} has a git operation in progress ({marker});'
        ' finish or abort it before merging this node'
    ]
    assert named == (expected if marker is not None else [])


# ------ tree latch


def test_pause_latch_blocks_spawn_and_start(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paused (or pausing) ancestor refuses new spawns and starts.

    The latch closes the fan-out race: a node born or started into a
    pausing subtree would run unfrozen inside a "paused" tree, so
    ``init``/``start`` refuse until resume.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    parent_dir = parent.worktree / '.fractal' / 'main.parent'
    # a parked parent latches its subtree
    parent.status_set('paused')
    child.status_set('stopped')
    with pytest.raises(RuntimeError, match='under a paused node'):
        child.start(continue_run=True)
    monkeypatch.setenv('_NODE', f'{parent_dir}')
    with pytest.raises(RuntimeError, match='Cannot spawn under a paused node'):
        Node(git_repo).init(name='kid2')
    monkeypatch.delenv('_NODE')
    # a still-active parent with a pending pause signal latches too
    parent.status_set('active')
    parent.record.signal_set('pause', 'incoming')
    with pytest.raises(RuntimeError, match='under a paused node'):
        child.start(continue_run=True)


def test_pause_latch_blocks_targeted_resume(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paused ancestor refuses a targeted resume of a deeper node.

    The resume boot skips the ancestor walk (the leaf-first fan-out's
    exemption), so without the verb-side refusal a targeted resume would
    relaunch a child to run new work inside a subtree the pause froze --
    the equivalent start refuses the identical state. Resuming the
    latching ancestor relaunches the child with it.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    _stub_run_script(monkeypatch, Node)
    # both loops park (simulated -- the loops are stubbed here)
    parent.status_set('paused')
    child.status_set('paused')
    with pytest.raises(RuntimeError, match='Cannot resume under a paused node'):
        child.resume()
    # the withdrawal path is latched too: a child still parking under a
    # parked parent keeps its pending pause
    child.status_set('active')
    child.record.signal_set('pause', 'incoming')
    with pytest.raises(RuntimeError, match='Cannot resume under a paused node'):
        child.resume()
    assert child.record.signal_get('pause') is not None
    # resuming the latching parent relaunches the child with it (leaf-first)
    child.status_set('paused')
    result = parent.resume()
    assert 'Resumed 2 nodes' in result


def test_tree_pause_latches_depth_one(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user-node pause brakes the whole tree, depth-1 included.

    A depth-1 node's only ancestor is the statusless user root, so the
    ancestor walk alone cannot latch it -- the tree-wide brake writes the
    root marker that init/start consult, and the tree-wide release lifts
    it again.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # an idle depth-1 sibling, initialized before the brake
    Node(git_repo).init(name='newtop')
    newtop = Node(git_repo / '.worktrees' / 'main.newtop')
    user = Node(git_repo)
    # scope the fan-out stubs: the final depth-1 spawn below must run real
    with pytest.MonkeyPatch().context() as mp:
        _stub_run_script(mp, Node)
        result = user.pause(reason='brake')
    assert 'Pause signal sent to 2 nodes' in result
    # the latch refuses a depth-1 spawn and a depth-1 start
    with pytest.raises(RuntimeError, match='Cannot spawn under a paused node'):
        Node(git_repo).init(name='another')
    with pytest.raises(RuntimeError, match='Cannot start under a paused node'):
        newtop.start()
    # both loops park (simulated); the brake refuses a targeted depth-1
    # resume too -- only the tree-wide release lifts the latch
    parent.status_set('paused')
    child.status_set('paused')
    with pytest.MonkeyPatch().context() as mp:
        _stub_run_script(mp, Node)
        with pytest.raises(RuntimeError, match='Cannot resume under a paused node'):
            parent.resume()
        result = user.resume()
    assert 'Resumed 2 nodes' in result
    _stub_run_script(monkeypatch, newtop)
    newtop.start()
    Node(git_repo).init(name='another')
