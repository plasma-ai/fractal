"""Node teardown tiers: delete, deregister, destroy, reset.

Covers history retention (rows outlive the node), the cwd and
active-node refusals, worktree-lock pre-flights, phantom-worktree
pruning, and the full destroy/reset lifecycles.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
from typing import Optional

import pytest

from fractal.constants import SOCKET_FILE
from fractal.core.node import Node
from tests._helpers import _stub_run_script

from .conftest import (
    _make_git_repo,
    _parse_project_dir,
    _resolve_branch,
    _spawn_parent_child,
)

__all__ = [
    'test_delete_rejects_active',
    'test_delete_rejects_paused',
    'test_delete_rejects_from_inside_worktree',
    'test_delete_recursively_removes_subtree',
    'test_delete_rejects_active_descendant',
    'test_delete_reconciles_crashed_self',
    'test_delete_reconciles_crashed_descendant',
    'test_delete_clears_registry_and_subs_but_keeps_history',
    'test_run_script_resolves_invoking_installation_cli',
    'test_delete_keeps_read_receipts',
    'test_delete_cleans_registry_when_parent_missing',
    'test_delete_not_blocked_by_pruned_child_worktree',
    'test_delete_clears_descendant_rows_from_parent',
    'test_deregister_removes_orphaned_node',
    'test_deregister_refuses_a_live_descendant',
    'test_rm_rf_worktree_lists_orphan_and_deregisters_keeping_history',
    'test_delete_aborts_cleanly_when_remote_delete_fails',
    'test_delete_locked_worktree_aborts_before_remote',
    'test_teardown_running_preflight_precedes_paused_settle',
    'test_teardown_refuses_on_inconclusive_tmux_probe',
    'test_teardown_refuses_session_alive_on_recorded_socket',
    'test_destroy_rejects_from_inside_worktree',
    'test_teardown_locked_preflight_precedes_paused_settle',
    'test_destroy_lifecycle',
    'test_destroy_rejects_an_unknown_tree',
    'test_teardown_guards_travel_with_the_scope',
    'test_destroy_prunes_phantom_node_branches',
    'test_reset_lifecycle',
]


# ------ delete / deregister


def test_delete_rejects_active(node_with_db: Node) -> None:
    """Delete raises when node is active."""
    node = node_with_db
    # set status to active
    node.status_set('active')
    # verify delete rejects
    with pytest.raises(RuntimeError, match='active'):
        node.delete()


def test_delete_rejects_paused(node_with_db: Node) -> None:
    """Delete raises when the node is paused.

    Unlike the operator-confirmed ``reset``/``destroy`` (which settle paused
    nodes), ``delete`` is agent-reachable -- it must never discard a hold a
    human deliberately placed.
    """
    node = node_with_db
    node.status_set('paused')
    with pytest.raises(RuntimeError, match='paused'):
        node.delete()


@pytest.mark.parametrize('delete_parent', [False, True])
def test_delete_rejects_from_inside_worktree(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    delete_parent: bool,
) -> None:
    """Delete refuses when cwd is inside the node's own or a descendant worktree.

    Git cannot remove a worktree the caller occupies. The descendant case
    requires resolving ``find_worktree``'s path before comparing.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='parent')
    parent_wt = git_repo / '.worktrees' / 'main.parent'
    # spawn a child under the parent -- _NODE makes the parent the resolved caller
    node_dir = parent_wt / '.fractal' / 'main.parent'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    Node(git_repo).init(name='kid')
    monkeypatch.delenv('_NODE')
    kid_wt = git_repo / '.worktrees' / 'main.parent.kid'
    # stand inside the kid worktree, then delete the kid (own) or the parent
    # (the kid is then a descendant) -- both must be refused
    monkeypatch.chdir(kid_wt)
    target = Node(parent_wt) if delete_parent else Node(kid_wt)
    with pytest.raises(RuntimeError, match='current worktree from inside it'):
        target.delete()


def test_delete_recursively_removes_subtree(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting a node tears down its whole subtree, deepest first.

    A live (non-active) child does not block the parent -- its worktree,
    branch, and registry rows go with the subtree.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='parent')
    parent_wt = git_repo / '.worktrees' / 'main.parent'

    # spawn a child under the parent -- _NODE makes the parent the resolved caller
    node_dir = parent_wt / '.fractal' / 'main.parent'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    Node(git_repo).init(name='kid')
    monkeypatch.delenv('_NODE')
    kid_wt = git_repo / '.worktrees' / 'main.parent.kid'
    assert kid_wt.is_dir()

    # deleting the parent recursively removes the child too
    Node(parent_wt).delete()

    # both worktrees are gone and neither lingers in the root registry
    assert not parent_wt.exists()
    assert not kid_wt.exists()
    after = {row['node'] for row in Node(git_repo).child_list()}
    assert 'main.parent' not in after
    assert 'main.parent.kid' not in after


def test_delete_rejects_active_descendant(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recursive delete refuses while any descendant is active.

    Tearing a running node's worktree out mid-execution would be unsafe, so the
    delete refuses (leaving the subtree intact) until it is stopped or killed.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='parent')
    parent_wt = git_repo / '.worktrees' / 'main.parent'

    # spawn a child and mark it active
    node_dir = parent_wt / '.fractal' / 'main.parent'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    Node(git_repo).init(name='kid')
    monkeypatch.delenv('_NODE')
    kid_wt = git_repo / '.worktrees' / 'main.parent.kid'
    Node(kid_wt).status_set('active')

    # the kid is genuinely running (live session), so delete
    # must refuse rather than reconcile it away
    monkeypatch.setattr(Node, '_tmux_session_exists', lambda self: True)
    with pytest.raises(RuntimeError, match='active or paused descendant'):
        Node(parent_wt).delete()

    # nothing was torn down
    assert parent_wt.is_dir()
    assert kid_wt.is_dir()


def test_delete_reconciles_crashed_self(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed-but-active node can be deleted, not wedged.

    Its status reads ``active`` but the tmux session is gone, so delete
    reconciles it to ``exited`` and tears the worktree down -- no hand-edited
    status file or loop restart needed.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='parent')
    parent_wt = git_repo / '.worktrees' / 'main.parent'
    Node(parent_wt).status_set('active')

    # the loop crashed: no live tmux session anywhere
    monkeypatch.setattr(Node, '_tmux_session_exists', lambda self: False)
    Node(parent_wt).delete()

    assert not parent_wt.exists()


def test_delete_reconciles_crashed_descendant(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed-but-active descendant does not wedge an ancestor's delete."""
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='parent')
    parent_wt = git_repo / '.worktrees' / 'main.parent'

    # spawn a child and mark it active, then crash it (session gone)
    node_dir = parent_wt / '.fractal' / 'main.parent'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    Node(git_repo).init(name='kid')
    monkeypatch.delenv('_NODE')
    kid_wt = git_repo / '.worktrees' / 'main.parent.kid'
    Node(kid_wt).status_set('active')

    # the kid's loop crashed: no live session, so it must not block the delete
    monkeypatch.setattr(Node, '_tmux_session_exists', lambda self: False)
    Node(parent_wt).delete()

    assert not parent_wt.exists()
    assert not kid_wt.exists()


def test_delete_clears_registry_and_subs_but_keeps_history(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delete sweeps registry rows + subscriptions; history rows persist.

    The central database outlives the branch: a deleted subtree's runs and
    messages stay readable (and costed), while its ``nodes`` rows and
    subscriptions (both directions) are swept so feeds and listings stop
    fanning into it.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    child_branch = child.branch
    # the child messages its parent, then the subtree settles
    child.radio.send(parent=True, subject='done', data='summary', priority=5)
    for node in (child, parent):
        node.status_set('completed')
    db = parent.db
    Node(git_repo / '.worktrees' / 'main.parent').delete()

    # registry and subs swept (both directions)
    assert db.read('nodes', where={'node': 'main.parent'}) == []
    assert db.read('nodes', where={'node': child_branch}) == []
    assert db.read('subs', where={'node': child_branch}) == []
    assert db.read('subs', where={'target': child_branch}) == []
    # history persists: the child's run rows and its message to the parent
    assert db.read('runs', where={'node': child_branch})
    assert db.read('messages', where={'sender': child_branch})


def test_run_script_resolves_invoking_installation_cli(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Script subprocesses resolve the invoking installation's ``fractal``.

    The lifecycle scripts shell back into ``fractal`` (config/event calls on
    the init/delete/merge paths), and resolving that off ambient PATH lets a
    foreign install answer -- a root venv speaking another branch's
    dialect can flip a suite verdict on byte-identical source. The invoking
    interpreter's own bin dir must win over anything fronted on PATH.
    """
    _, child = _spawn_parent_child(git_repo, monkeypatch)
    # front a decoy `fractal` on PATH that records any consultation -- its exit 1
    # lands in the scripts' `|| echo true` fallbacks, so the flow stays local
    decoy_dir = tmp_path / 'decoy_bin'
    decoy_dir.mkdir()
    marker = decoy_dir / 'consulted'
    decoy = decoy_dir / 'fractal'
    decoy.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n', encoding='utf-8')
    decoy.chmod(0o755)
    path = os.environ['PATH']
    monkeypatch.setenv('PATH', f'{decoy_dir}{os.pathsep}{path}')
    # drive a script that shells back into fractal (delete.sh reads config)
    child.status_set('completed')
    child.delete()
    assert not marker.exists()


def test_delete_keeps_read_receipts(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deleted node's read receipts persist (history is never swept).

    Read state is a per-(message, node) row in the shared ``reads`` table;
    deletion removes only ``nodes`` and ``subs``, so a deleted node's receipts
    survive rather than resurfacing as unread in a sibling's feed.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    child_branch = child.branch
    # the child reads a message in its own inbox, writing a read receipt
    uuid, _, _ = parent.radio.send(child_branch, subject='ack', data='d', priority=5)
    child.radio.read(uuid)
    db = parent.db
    assert db.read('reads', where={'node': child_branch})
    # tear the subtree down, then confirm the receipt outlived the node
    for node in (child, parent):
        node.status_set('completed')
    Node(git_repo / '.worktrees' / 'main.parent').delete()
    assert db.read('nodes', where={'node': child_branch}) == []
    assert db.read('reads', where={'node': child_branch})


def test_delete_cleans_registry_when_parent_missing(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With the immediate parent gone, delete still clears the central registry.

    The ``delete`` audit event lands on the parent only when it is still
    reachable; a hand-removed parent costs just that event -- the registry
    sweep still happens, and the anomaly is warned about rather than crashing
    mid-teardown.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='a')
    a_wt = git_repo / '.worktrees' / 'main.a'
    # grandchild main.a.b under main.a (a is the resolved caller via _NODE)
    node_dir = a_wt / '.fractal' / 'main.a'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    Node(git_repo).init(name='b')
    monkeypatch.delenv('_NODE')
    b_wt = git_repo / '.worktrees' / 'main.a.b'
    # the root (grandparent) tracks main.a.b too (flat registry)
    assert 'main.a.b' in {row['node'] for row in Node(git_repo).child_list()}
    # remove the immediate parent's registry so it's unreachable
    shutil.rmtree(a_wt / '.fractal' / 'main.a')

    Node(b_wt).delete()

    # main.a.b torn down, its row cleared from the reachable root, and warned
    assert not b_wt.exists()
    assert 'main.a.b' not in {row['node'] for row in Node(git_repo).child_list()}
    assert 'missing' in caplog.text.lower()


def test_delete_not_blocked_by_pruned_child_worktree(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered child whose worktree is gone is deregistered, not a wedge.

    Recursive delete tears down each descendant's worktree, but a phantom child
    (registry row present, worktree already removed) has nothing to tear down --
    it must be deregistered rather than crash or block the parent's deletion.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='parent')
    parent_wt = git_repo / '.worktrees' / 'main.parent'

    # spawn a child under the parent -- _NODE makes the parent the resolved caller
    node_dir = parent_wt / '.fractal' / 'main.parent'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    Node(git_repo).init(name='kid')
    monkeypatch.delenv('_NODE')
    kid_wt = git_repo / '.worktrees' / 'main.parent.kid'
    assert kid_wt.is_dir()

    # prune the kid's worktree dir -- git still lists it, but the dir is gone
    shutil.rmtree(kid_wt)

    # the phantom child does not block the parent's delete (real delete.sh
    # stubbed so only the Python guard/deregister logic runs)
    _stub_run_script(monkeypatch, Node)
    Node(parent_wt).delete()


def test_delete_clears_descendant_rows_from_parent(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting a node clears its descendants from the direct parent's registry.

    The parent's ``nodes`` table is a flat registry of every descendant, so a
    deleted node's grandchild rows would linger there if only the direct-child
    row were removed -- a stale-registry leak.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='x')
    x_wt = git_repo / '.worktrees' / 'main.x'

    # grandchild: init 'y' under x (x is the resolved caller via _NODE)
    node_dir = x_wt / '.fractal' / 'main.x'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    Node(git_repo).init(name='y')
    monkeypatch.delenv('_NODE')
    y_wt = git_repo / '.worktrees' / 'main.x.y'
    assert y_wt.is_dir()

    # the user (root) node registry now tracks both descendants
    root = Node(git_repo)
    before = {row['node'] for row in root.child_list()}
    assert {'main.x', 'main.x.y'} <= before

    # prune the grandchild's worktree dir -- git still lists it, but the dir is gone
    shutil.rmtree(y_wt)
    _stub_run_script(monkeypatch, Node)
    Node(x_wt).delete()

    # neither x nor its grandchild lingers in the parent registry
    after = {row['node'] for row in root.child_list()}
    assert 'main.x' not in after
    assert 'main.x.y' not in after


def test_deregister_removes_orphaned_node(git_repo: pathlib.Path) -> None:
    """``deregister`` tears a worktree-less orphan out of the registry + branch.

    A child whose worktree is removed out of band lingers in the registry (and
    consumes the children budget) and cannot be ``delete``d -- ``deregister``
    (which ``delete <branch> --force`` falls back to) prunes the row, branch, and
    ``.project`` entry without needing the worktree.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='orphan')
    orphan_wt = git_repo / '.worktrees' / 'main.orphan'
    # remove the worktree out of band -> an orphan (registry row, no worktree)
    subprocess.run(
        ['git', 'worktree', 'remove', '--force', f'{orphan_wt}'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )
    assert 'main.orphan' in {row['node'] for row in Node(git_repo).child_list()}
    # deregister clears the registry row and deletes the branch
    Node(git_repo).deregister('main.orphan')
    after = {row['node'] for row in Node(git_repo).child_list()}
    assert 'main.orphan' not in after
    branches = subprocess.run(
        ['git', 'branch', '--list', 'main.orphan'],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert branches.stdout.strip() == ''


def test_deregister_refuses_a_live_descendant(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``deregister`` refuses when a descendant still has a live worktree.

    The prune walks the whole subtree, so an orphan parent whose child is still
    live must not be deregistered -- doing so would clear the running child's
    registry row and project-cache entry out from under it. The guard names the
    offending descendant and leaves it (row and worktree) untouched.
    """
    _, child = _spawn_parent_child(git_repo, monkeypatch)
    child_branch = child.branch
    parent_wt = git_repo / '.worktrees' / 'main.parent'
    # orphan the parent out of band, leaving the child's worktree live
    subprocess.run(
        ['git', 'worktree', 'remove', '--force', f'{parent_wt}'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )
    # the live child sits in the orphan's subtree, so the prune must refuse
    with pytest.raises(RuntimeError, match=child_branch):
        Node(git_repo).deregister('main.parent')
    # the child survives untouched -- still registered, worktree still present
    assert child_branch in {row['node'] for row in Node(git_repo).child_list()}
    assert (git_repo / '.worktrees' / child_branch).exists()


def test_rm_rf_worktree_lists_orphan_and_deregisters_keeping_history(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``rm -rf``'d worktree reads as gone, so list flags it and delete works.

    ``git worktree list`` still lists a hand-``rm -rf``'d worktree (as
    ``prunable``), so the on-disk probe -- not git's stale porcelain -- is
    what decides a node is gone; ``deregister`` (``delete --force``'s
    fallback) must not be wedged by the dead path.
    """
    _, child = _spawn_parent_child(git_repo, monkeypatch)
    child_branch = child.branch
    # rm -rf the child's worktree dir out of band -- git still lists it prunable
    shutil.rmtree(git_repo / '.worktrees' / child_branch)

    # plain list flags the rm-rf'd node orphan rather than reporting it healthy
    rows = {row['node']: row['status'] for row in Node(git_repo).list()}
    assert rows[child_branch] == 'orphan'

    # deregister is not wedged by the dead worktree path: it clears the
    # registry row, keeps the run history, and hints the one-shot git cleanup
    message = Node(git_repo).deregister(child_branch)
    assert child_branch not in {row['node'] for row in Node(git_repo).child_list()}
    assert child.db.read('runs', where={'node': child_branch})
    assert 'git worktree prune' in message


def test_delete_aborts_cleanly_when_remote_delete_fails(
    tmp_path: pathlib.Path,
) -> None:
    """A failed remote-branch delete leaves the node intact and retryable.

    Were the worktree removed before the networked, failure-prone
    ``git push origin --delete``, a failed push (a protected branch,
    ``receive.denyDeletes``, an unreachable remote) would abort under ``set -e``
    with the worktree already gone but the local branch and ``.project`` cache
    still present -- a half-deleted node ``Node.delete`` cannot even retry (its
    ``exists()`` guard fails once the worktree is gone). The remote delete must
    run first, so a push failure aborts with nothing removed.
    """
    # bare remote that rejects branch deletions -- a deterministic push failure
    remote = tmp_path / 'remote.git'
    subprocess.run(
        ['git', 'init', '--bare', f'{remote}'],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', '-C', f'{remote}', 'config', 'receive.denyDeletes', 'true'],
        capture_output=True,
        check=True,
    )
    repo = _make_git_repo(tmp_path / 'repo')
    subprocess.run(
        ['git', 'remote', 'add', 'origin', f'{remote}'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    Node(repo).init(agent='claude', user=True)

    # a non-local node whose branch is pushed to the remote, so delete.sh's
    # ls-remote check finds it and attempts the (rejected) push --delete
    output = Node(repo).init(name='feature', agent='claude', local=False)
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    subprocess.run(
        ['git', '-C', f'{project_dir}', 'push', '-u', 'origin', branch],
        capture_output=True,
        check=True,
    )
    result = subprocess.run(
        ['git', 'ls-remote', '--heads', f'{remote}'],
        capture_output=True,
        text=True,
        check=True,
    )
    refs = result.stdout
    assert branch in refs

    # delete fails on the rejected remote delete...
    with pytest.raises(RuntimeError):
        Node(project_dir).delete()

    # ...but nothing local was removed -- the worktree and branch survive, so the
    # node stays consistent and the delete is safe to retry
    assert project_dir.is_dir()
    result = subprocess.run(
        ['git', 'branch', '--format=%(refname:short)'],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    local_branches = result.stdout.split()
    assert branch in local_branches


def test_delete_locked_worktree_aborts_before_remote(tmp_path: pathlib.Path) -> None:
    """A locked worktree aborts the delete before the remote branch is touched.

    With remote-first ordering, a locked (unremovable) worktree would otherwise
    let ``delete.sh`` delete the remote branch and then fail removing the worktree
    -- destroying the only copy while the node lingers, unretryable. A removability
    pre-check bails first, so the remote branch survives.
    """
    # bare remote with the node branch pushed, so delete.sh would attempt a push
    # --delete were it not aborted first
    remote = tmp_path / 'remote.git'
    subprocess.run(
        ['git', 'init', '--bare', f'{remote}'],
        capture_output=True,
        check=True,
    )
    repo = _make_git_repo(tmp_path / 'repo')
    subprocess.run(
        ['git', 'remote', 'add', 'origin', f'{remote}'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='feature', agent='claude', local=False)
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    subprocess.run(
        ['git', '-C', f'{project_dir}', 'push', '-u', 'origin', branch],
        capture_output=True,
        check=True,
    )
    # lock the worktree so removal is impossible
    subprocess.run(
        ['git', '-C', f'{repo}', 'worktree', 'lock', f'{project_dir}'],
        capture_output=True,
        check=True,
    )

    # delete aborts on the locked worktree...
    with pytest.raises(RuntimeError):
        Node(project_dir).delete()

    # ...before the remote was touched -- the remote branch (the only copy) survives
    result = subprocess.run(
        ['git', 'ls-remote', '--heads', f'{remote}'],
        capture_output=True,
        text=True,
        check=True,
    )
    refs = result.stdout
    assert branch in refs
    # the local node is intact too, so the delete is retriable after unlocking
    assert project_dir.is_dir()


# ------ destroy / reset


def test_teardown_running_preflight_precedes_paused_settle(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A teardown refused on a live session leaves paused nodes untouched.

    The paused settle is irreversible -- the kill closes the parked run and
    ends its resumability -- so the running-node refusal must pre-flight
    before it: a destroy/reset that refuses must change nothing.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='runner')
    node.init(name='frozen')
    runner = Node(git_repo / '.worktrees' / 'main.runner')
    frozen = Node(git_repo / '.worktrees' / 'main.frozen')
    runner.status_set('active')
    run_id = frozen.record.run_start()
    frozen.status_set('paused')
    # the runner's loop is alive in tmux; the teardown must refuse up front
    monkeypatch.setattr(
        'fractal.util.tmux.probe',
        lambda: frozenset({runner.tmux_session}),
    )
    with pytest.raises(RuntimeError, match='still running in tmux'):
        Node.destroy(git_repo)
    with pytest.raises(RuntimeError, match='still running in tmux'):
        Node.reset(git_repo)
    # the paused node's frozen state survived both refused teardowns
    assert frozen.status() == 'paused'
    run = frozen.db.read('runs', where={'run_id': run_id})[0]
    assert run['ended_at'] is None


def test_teardown_refuses_on_inconclusive_tmux_probe(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A teardown never proceeds blind: a failed tmux probe refuses up front.

    With no answer from tmux (binary absent, ``list-sessions`` erroring) any
    node may still be running, so destroy/reset refuse -- naming the probe --
    rather than tearing live loops down on ignorance.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='runner')
    runner = Node(git_repo / '.worktrees' / 'main.runner')
    runner.status_set('active')
    # tmux gives no answer (e.g. no binary on this shell's PATH)
    monkeypatch.setattr('fractal.util.tmux.probe', lambda: None)
    with pytest.raises(RuntimeError, match='probe gave no answer'):
        Node.destroy(git_repo)
    with pytest.raises(RuntimeError, match='probe gave no answer'):
        Node.reset(git_repo)
    # nothing was torn down
    assert runner.worktree.is_dir()
    assert runner.status() == 'active'


def test_teardown_refuses_session_alive_on_recorded_socket(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The teardown pre-flight probes each node's recorded socket.

    A loop's session lives on the tmux server recorded at boot
    (``.socket``); a shell resolving a different server reads a
    definitive-empty ambient answer there, and a teardown trusting it
    would remove worktrees, branches, and (destroy) the central DB out
    from under a live, spending loop.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='runner')
    runner = Node(git_repo / '.worktrees' / 'main.runner')
    runner.status_set('active')
    # the loop's session is alive on its recorded socket only -- the
    # ambient server answers definitively empty
    socket_file = runner.node_dir / SOCKET_FILE
    socket_file.write_text('other-sock\n', encoding='utf-8')

    def probe(socket: Optional[str] = None) -> frozenset[str]:
        if socket == 'other-sock':
            return frozenset({runner.tmux_session})
        return frozenset()

    monkeypatch.setattr('fractal.util.tmux.probe', probe)
    with pytest.raises(RuntimeError, match='still running in tmux'):
        Node.destroy(git_repo)
    with pytest.raises(RuntimeError, match='still running in tmux'):
        Node.reset(git_repo)
    # nothing was torn down
    assert runner.worktree.is_dir()
    assert runner.status() == 'active'


def test_destroy_rejects_from_inside_worktree(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Destroy refuses when cwd is inside a node worktree.

    Git cannot remove a worktree the caller occupies -- the same
    pre-flight delete and reset carry, so a destroy run from inside a
    node worktree refuses with nothing settled or torn down.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='task')
    task_wt = git_repo / '.worktrees' / 'main.task'
    monkeypatch.chdir(task_wt)
    with pytest.raises(RuntimeError, match='current worktree from inside it'):
        Node.destroy(git_repo)
    assert task_wt.is_dir()


def test_teardown_locked_preflight_precedes_paused_settle(
    git_repo: pathlib.Path,
) -> None:
    """A teardown refused on a locked worktree leaves paused nodes untouched."""
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='pinned')
    node.init(name='frozen')
    pinned_worktree = git_repo / '.worktrees' / 'main.pinned'
    frozen = Node(git_repo / '.worktrees' / 'main.frozen')
    run_id = frozen.record.run_start()
    frozen.status_set('paused')
    subprocess.run(
        ['git', 'worktree', 'lock', f'{pinned_worktree}'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )
    with pytest.raises(RuntimeError, match='locked'):
        Node.destroy(git_repo)
    with pytest.raises(RuntimeError, match='locked'):
        Node.reset(git_repo)
    # the paused node's frozen state survived both refused teardowns
    assert frozen.status() == 'paused'
    run = frozen.db.read('runs', where={'run_id': run_id})[0]
    assert run['ended_at'] is None


def test_destroy_lifecycle(git_repo: pathlib.Path) -> None:
    """Destroy removes worktrees, branches, node data, and the exclude block.

    A paused child rides into the teardown: destroy settles it (kill
    bookkeeping) rather than refusing -- the confirmation authorized
    discarding its frozen work.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='task')
    task = Node(git_repo / '.worktrees' / 'main.task')
    task.record.run_start()
    task.status_set('paused')

    output = Node.destroy(git_repo)
    assert 'Destroyed fractal' in output
    # children, the registry, and the user node's data are all gone
    assert not (git_repo / '.worktrees').exists()
    assert not (git_repo / '.fractal').exists()
    branches = subprocess.run(
        ['git', 'branch', '--list', 'main.task'],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert branches.stdout.strip() == ''
    # the exclude block is stripped; the committed wiki survives
    exclude = git_repo / '.git' / 'info' / 'exclude'
    assert '>>> fractal >>>' not in exclude.read_text(encoding='utf-8')
    assert (git_repo / 'wiki').is_dir()

    # destroying again is a clean no-op
    second = Node.destroy(git_repo)
    assert 'Nothing to destroy' in second


def test_destroy_rejects_an_unknown_tree(git_repo: pathlib.Path) -> None:
    """A tree-scoped destroy anchors the named tree's user node or refuses.

    A wrong or mid-tree branch name must error before any teardown -- keyed
    to the wrong tree it would guard and prune a healthy sibling's state.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='task')
    with pytest.raises(RuntimeError, match='No tree found'):
        Node.destroy(git_repo, name='ghost')
    # a mid-tree (non-user) branch is refused the same way
    with pytest.raises(RuntimeError, match='No tree found'):
        Node.destroy(git_repo, name='main.task')
    assert (git_repo / '.worktrees' / 'main.task').exists()


def test_teardown_guards_travel_with_the_scope(git_repo: pathlib.Path) -> None:
    """Each teardown pre-flights exactly the trees it will tear down.

    A repo-wide sweep guards every tree before settling any, so one tree's
    locked worktree refuses the whole run with a sibling's paused work still
    frozen -- guarding one tree while tearing down another would discard
    work the caller was never warned about. A tree-scoped teardown is the
    mirror: it pre-flights only its own tree, so a locked sibling cannot
    block it, and it settles only its own paused nodes.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='frozen')
    frozen = Node(git_repo / '.worktrees' / 'main.frozen')
    run_id = frozen.record.run_start()
    frozen.status_set('paused')
    # a second tree beside it, holding the locked worktree
    subprocess.run(
        ['git', 'checkout', '-b', 'second'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )
    Node(git_repo).init(agent='claude', user=True)
    Node(git_repo).init(name='pinned')
    pinned_worktree = git_repo / '.worktrees' / 'second.pinned'
    subprocess.run(
        ['git', 'worktree', 'lock', f'{pinned_worktree}'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )
    # the repo-wide sweep refuses on the sibling's lock, and the first tree's
    # paused work is still frozen -- nothing was settled behind the refusal
    with pytest.raises(RuntimeError, match='locked'):
        Node.destroy(git_repo)
    assert frozen.status() == 'paused'
    run = frozen.db.read('runs', where={'run_id': run_id})[0]
    assert run['ended_at'] is None
    # scoped to its own tree, the same teardown runs: the lock is out of
    # scope, the paused node settles, and the locked sibling stands
    user = Node.resolve_user(git_repo, name='main')
    Node.reset(git_repo, name='main')
    assert not (git_repo / '.worktrees' / 'main.frozen').exists()
    assert pinned_worktree.is_dir()
    run = user.db.read('runs', where={'run_id': run_id})[0]
    assert run['ended_at'] is not None


def test_destroy_prunes_phantom_node_branches(git_repo: pathlib.Path) -> None:
    """Destroy deletes the branch of a node whose worktree vanished out of band.

    A phantom node (registry row present, worktree ``rm -rf``'d out of band)
    gives ``destroy.sh`` no worktree to enumerate, so its branch would outlive
    the teardown -- and with the central DB (the last record of the branch)
    gone, a later re-init of the name would silently resurrect the old
    history instead of forking fresh from the parent.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='phantom')
    # rm -rf the worktree out of band -- the branch and registry row survive
    shutil.rmtree(git_repo / '.worktrees' / 'main.phantom')

    output = Node.destroy(git_repo)
    assert 'Destroyed fractal' in output
    # the phantom's branch went with the teardown
    branches = subprocess.run(
        ['git', 'branch', '--list', 'main.phantom'],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert branches.stdout.strip() == ''


def test_reset_lifecycle(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reset tears down every worktree but keeps the project and its history.

    One narrative: the refusal guards first (caller inside a worktree, a
    locked worktree), then the teardown settles a paused child (its open run
    closes ``killed`` -- the confirmed teardown discards frozen work, no
    manual kill sweep) and reconciles a crashed-active child (open run
    closes ``exited``) under a stale tree-wide pause latch, the survivor
    checks, and the converged state -- a fresh child inits immediately and a
    second reset is clean.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='task')
    node.init(name='other')
    task_wt = git_repo / '.worktrees' / 'main.task'
    task = Node(task_wt)
    other = Node(git_repo / '.worktrees' / 'main.other')

    # refuse from inside a node worktree (git cannot remove the caller's cwd)
    monkeypatch.chdir(task_wt)
    with pytest.raises(RuntimeError, match='inside'):
        Node.reset(git_repo)
    monkeypatch.chdir(git_repo)

    # refuse while a worktree is locked, before touching anything
    subprocess.run(
        ['git', 'worktree', 'lock', f'{task_wt}'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )
    with pytest.raises(RuntimeError, match='locked'):
        Node.reset(git_repo)
    assert task_wt.exists()
    subprocess.run(
        ['git', 'worktree', 'unlock', f'{task_wt}'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )

    # a paused child with a parked run, a crashed-active child (open run, no
    # tmux session), and a stale tree-wide pause latch all ride into the
    # teardown
    task.record.run_start()
    task.status_set('paused')
    other.status_set('active')
    other.record.run_start()
    latch = node._tree_latch_file
    latch.write_text('paused\n', encoding='utf-8')

    output = Node.reset(git_repo)
    assert 'Reset tree: main' in output
    # every worktree and branch is gone; .worktrees/ itself survives
    assert not task_wt.exists()
    assert not (git_repo / '.worktrees' / 'main.other').exists()
    assert (git_repo / '.worktrees').is_dir()
    branches = subprocess.run(
        ['git', 'branch', '--list', 'main.*'],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert branches.stdout.strip() == ''
    # the user node's data and the wiki survive; the registry is cleared the
    # right way around -- node/sub rows gone, history rows kept and closed
    assert (git_repo / '.fractal' / 'main' / '.db').is_file()
    assert (git_repo / 'wiki').is_dir()
    assert node.db.read('nodes') == []
    assert node.db.read('subs') == []
    # the paused child was settled, not refused: its parked run closed
    # killed, with the kill event attributing the teardown
    runs = node.db.read('runs', where={'node': 'main.task'})
    assert runs
    assert all(run['status'] == 'killed' for run in runs)
    kills = node.db.read('events', where={'node': 'main.task', 'event': 'kill'})
    assert any('reset teardown' in kill['metadata'] for kill in kills)
    # the crashed-active child was reconciled: its open run closed exited
    runs = node.db.read('runs', where={'node': 'main.other'})
    assert runs
    assert all(run['status'] == 'exited' for run in runs)
    assert node.db.read('events', where={'event': 'delete'})
    # the stale latch went with the tree it froze
    assert not latch.exists()

    # a fresh child inits immediately (resolution and the latch are clear)
    node.init(name='again')
    assert (git_repo / '.worktrees' / 'main.again').is_dir()
    # resetting again cleanly tears down the new node too
    Node.reset(git_repo)
    assert node.db.read('nodes') == []
