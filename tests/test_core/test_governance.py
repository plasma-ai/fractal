"""Spawn and rearm governance: the child registry and the cap gates.

Covers children-table bookkeeping (add/update/approve/pending), the
depth/width/descendant spawn gates enforced inside the worktree lock,
the re-arm (continue/unretire) re-checks, and the subtree budget
bounds.
"""

from __future__ import annotations

import json
import pathlib

import pytest

import fractal.core
import fractal.util
from fractal.core.node import Node
from tests._helpers import _git, _stub_run_script

from .conftest import (
    _active_run,
    _record_step_cost,
    _spawn_chain,
    _spawn_parent_child,
)

__all__ = [
    'test_child_lifecycle',
    'test_child_update_writes_config_before_registry',
    'test_init_registers_child',
    'test_spawn_event_recorded_on_parent',
    'test_child_pending_lists_direct_children_only',
    'test_child_pending_skips_stranded_gates',
    'test_max_depth_enforcement',
    'test_max_children_counts_only_unsettled',
    'test_max_depth_ancestor_enforcement',
    'test_max_descendants_counts_only_unsettled',
    'test_spawn_limit_enforced_inside_lock',
    'test_continue_re_checks_width_gate',
    'test_continue_max_cost_refuses_when_parent_is_orphaned',
    'test_continue_re_checks_descendant_gate',
    'test_spawn_gate_reconciles_crashed_active',
    'test_continue_gate_reconciles_crashed_active',
    'test_unretire_re_checks_width_gate',
    'test_unretire_re_checks_descendant_gate',
    'test_unretire_settled_restore_passes_at_cap',
    'test_unretire_gate_reconciles_crashed_active',
    'test_max_cost_enforcement',
    'test_max_cost_bounds_child_by_subtree_remaining',
    'test_max_cost_child_bound_re_arms_after_prior_run',
    'test_cost_caps_refuse_an_agent_that_records_no_cost',
]


# ------ children registry


def test_child_lifecycle(node_with_db: Node) -> None:
    """Child add and list (the registry is scoped to the caller's subtree)."""
    node = node_with_db

    # add children
    node.child_add('backend', max_cost=10.0, max_depth=2, max_children=3)
    node.child_add('frontend', max_cost=5.0)

    # list all children
    children = node.child_list()
    assert len(children) == 2
    names = {row['node'] for row in children}
    # children are stored as <parent_branch>.<name>
    assert any('backend' in n for n in names)
    assert any('frontend' in n for n in names)

    # verify max_cost stored
    backend = next(c for c in children if 'backend' in c['node'])
    assert backend['max_cost'] == 10.0
    assert backend['max_depth'] == 2
    assert backend['max_children'] == 3


def test_child_update_writes_config_before_registry(node_with_db: Node) -> None:
    """``child_update`` writes the child config first, so a failure can't desync.

    The registry row and the child's ``config.json`` must stay in agreement. The
    config write is the failure-prone step (a malformed/unwritable config raises
    in ``config_set``); doing it before the ``nodes`` update means such a failure
    leaves both the row and the file at their old values rather than updating the
    row while the file lags.
    """
    parent = node_with_db
    repo = parent.worktree
    branch = parent.branch
    child_branch = f'{branch}.svc'
    # register the child and give it a real worktree so find_worktree resolves
    parent.child_add('svc', max_cost=5.0)
    worktree = repo / '.worktrees' / child_branch
    _git(repo, 'worktree', 'add', '-b', child_branch, f'{worktree}', branch)
    child_dir = worktree / '.fractal' / child_branch
    child_dir.mkdir(parents=True)
    config_path = child_dir / 'config.json'

    # happy path: both the registry row and the child config.json update together
    config_path.write_text('{"root": "main", "max_cost": 5.0}\n', encoding='utf-8')
    parent.child_update('svc', max_cost=8.0, title='Service')
    row = parent.db.read('nodes', where={'node': child_branch})[0]
    assert row['max_cost'] == 8.0
    assert row['title'] == 'Service'
    written = json.loads(config_path.read_text(encoding='utf-8'))
    assert written['max_cost'] == 8.0
    assert written['title'] == 'Service'

    # failure path: a malformed child config makes config_set raise (a ValueError
    # naming the file); the registry row must keep its prior value rather than
    # racing ahead of the unwritten file
    config_path.write_text('{ not json', encoding='utf-8')
    with pytest.raises(ValueError, match=r'config\.json'):
        parent.child_update('svc', max_cost=99.0)
    row = parent.db.read('nodes', where={'node': child_branch})[0]
    assert row['max_cost'] == 8.0


def test_init_registers_child(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful init registers child in parent's nodes table."""
    node = node_with_db
    _stub_run_script(monkeypatch, node)
    node.init(
        'backend',
        max_depth=2,
        max_children=3,
        max_descendants=5,
        max_cost=10.0,
    )

    # verify child registered
    children = node.child_list()
    assert len(children) == 1
    child = children[0]
    assert 'backend' in child['node']
    assert child['max_depth'] == 2
    assert child['max_children'] == 3
    assert child['max_descendants'] == 5
    assert child['max_cost'] == 10.0


def test_spawn_event_recorded_on_parent(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spawning a child logs a ``spawn`` event on the parent naming the child.

    The event lives on the surviving parent (the child carries only its own
    ``init``), and its metadata surfaces through the ``activity`` view.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    where = {'node': parent.branch, 'event': 'spawn'}
    spawns = parent.db.read('events', where=where)
    assert [row['metadata'] for row in spawns] == [child.branch]
    # the child has no spawn of its own
    assert child.db.read('events', where={'node': child.branch, 'event': 'spawn'}) == []
    # the metadata surfaces through the activity view (it feeds the timeline)
    view = parent.db.read(
        query="SELECT metadata FROM activity WHERE node = ? AND event = 'spawn'",
        params=(parent.branch,),
    )
    assert [row['metadata'] for row in view] == [child.branch]


def test_child_pending_lists_direct_children_only(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``child_pending`` returns gated steps of direct children only.

    A parent approves its direct children's steps, not a grandchild's, so the
    lister includes the direct child's gated step and excludes the grandchild's.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # spawn a grandchild under the child
    monkeypatch.setenv('_NODE', f'{child.node_dir}')
    Node(git_repo).init(name='grandkid')
    monkeypatch.delenv('_NODE')
    grandchild = Node(git_repo / '.worktrees' / 'main.parent.kid.grandkid')
    grandchild.status_set('active')
    grandchild.record.run_start()

    # gate a step on both the direct child and the grandchild
    def gate(node: Node) -> None:
        run_id = _active_run(node)
        iter_id = node.record.iter_start(run_id=run_id, iter=1)
        step_id = node.record.step_start(
            iter_id=iter_id,
            run_id=run_id,
            step=1,
            step_name='REVIEW',
        )
        node.record.step_pending(step_id=step_id)

    gate(child)
    gate(grandchild)

    # the parent sees only its direct child's gated step
    pending = parent.child_pending()
    assert [row['branch'] for row in pending] == [child.branch]


def test_child_pending_skips_stranded_gates(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``child_pending`` lists only gates a live wait can still release.

    A step killed, crash-reconciled, or wound down in reserve mode closes
    terminal with ``approved=''`` still set -- nothing will ever read an
    approval aimed at it, so the lister skips it; a paused step stays
    listed (approving while parked is the resume flow) and an active one
    is the ordinary mid-wait gate.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    run_id = _active_run(child)
    iter_id = child.record.iter_start(run_id=run_id, iter=1)

    def gate(step: int, name: str) -> int:
        step_id = child.record.step_start(
            iter_id=iter_id,
            run_id=run_id,
            step=step,
            step_name=name,
        )
        child.record.step_pending(step_id=step_id)
        return step_id

    waiting = gate(1, 'REVIEW')
    parked = gate(2, 'AUDIT')
    child.record.step_end(step_id=parked, status='paused', exit_code=0)
    stranded = gate(3, 'SHIP')
    child.record.step_end(step_id=stranded, status='killed', exit_code=1)

    pending = {row['step_id'] for row in parent.child_pending()}
    assert pending == {waiting, parked}


# ------ spawn gates


@pytest.mark.parametrize(
    argnames=('parent_depth', 'should_raise'),
    argvalues=[
        (0, True),
        (1, False),
        (2, False),
    ],
    ids=[
        'depth-0-rejects',
        'depth-1-allows',
        'depth-2-allows',
    ],
)
def test_max_depth_enforcement(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    parent_depth: int,
    should_raise: bool,
) -> None:
    """Max depth is enforced by the child's actual depth vs ancestor config.

    No ceiling check -- the child can set any ``max_depth`` it wants; the
    ancestor walk rejects based on actual depth, not requested limits.
    """
    node_with_db.config.set('max_depth', parent_depth)
    if should_raise:
        with pytest.raises(ValueError, match='Max depth reached'):
            node_with_db.init('child')
    else:
        # child sets a larger max_depth than parent -- no ceiling check
        _stub_run_script(monkeypatch, node_with_db)
        node_with_db.init('child', max_depth=parent_depth + 5)


@pytest.mark.parametrize(
    argnames=('kid_status', 'should_raise'),
    argvalues=[
        ('active', True),
        ('idle', True),
        ('completed', False),
        ('stopped', False),
        ('exited', False),
        ('killed', False),
        ('retired', False),
    ],
    ids=[
        'active-holds',
        'idle-holds',
        'completed-frees',
        'stopped-frees',
        'exited-frees',
        'killed-frees',
        'retired-frees',
    ],
)
def test_max_children_counts_only_unsettled(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    kid_status: str,
    should_raise: bool,
) -> None:
    """Width slots are held by unsettled children and freed by settled ones.

    The gate binds on children still in play -- active, or idle
    awaiting start -- while a settled or retired child frees its slot
    automatically, so ``max_children`` bounds concurrency rather than
    lifetime spawn count. No ceiling check -- a child may set a larger
    ``max_children`` than its parent.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    parent.config.set('max_children', 1)
    # settle (or keep live) the only existing child, then spawn a sibling
    child.status_set(kid_status)
    parent_wt = parent.worktree
    node_dir = parent_wt / '.fractal' / 'main.parent'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    if should_raise:
        with pytest.raises(ValueError, match='Max children reached'):
            Node(git_repo).init(name='kid2')
    else:
        # the settled child freed its slot; a larger child cap is no ceiling
        Node(git_repo).init(name='kid2', max_children=5)
        assert fractal.util.git.find_worktree(git_repo, 'main.parent.kid2') is not None


def test_max_depth_ancestor_enforcement(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ancestor's ``max_depth`` blocks a deep spawn past unlimited intermediates.

    Only the grandparent ``p`` caps depth; ``c`` and ``g`` set no limit. The
    ancestor walk still rejects a spawn under ``g`` -- enforcement holds without
    the intermediate nodes cooperating.
    """
    p, _, g = _spawn_chain(git_repo, monkeypatch)
    # p allows descendants down to relative depth 2 -- g sits exactly at the edge
    p.config.set('max_depth', 2)
    # a child under g is relative depth 3 from p -- rejected on p's budget
    with pytest.raises(ValueError, match='Max depth reached') as excinfo:
        g.init('child')
    assert p.branch in str(excinfo.value)


def test_max_descendants_counts_only_unsettled(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settled descendants free subtree capacity; the ancestor cap still binds.

    With ``c`` completed and ``g`` active, ``p``'s two-node subtree holds one
    slot: a cap of 1 on ``p`` binds on the live ``g`` even with the immediate
    parent set far higher (the ancestor's stricter limit wins).
    """
    p, _, g = _spawn_chain(git_repo, monkeypatch)
    # spawn under g (_NODE makes it the resolved caller, the CLI shape)
    node_dir = g.worktree / '.fractal' / 'main.p.c.g'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    # p's subtree holds one unsettled node (g); cap it there
    p.config.set('max_descendants', 1)
    # a larger limit on the immediate parent must not override the ancestor's
    g.config.set('max_descendants', 100)
    with pytest.raises(ValueError, match='Max descendants reached') as excinfo:
        Node(git_repo).init(name='child')
    assert p.branch in str(excinfo.value)
    # a cap of 2 has a free slot -- the settled c no longer counts
    p.config.set('max_descendants', 2)
    Node(git_repo).init(name='child')
    assert fractal.util.git.find_worktree(git_repo, 'main.p.c.g.child') is not None


def test_spawn_limit_enforced_inside_lock(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An over-limit spawn is rejected by the in-lock cap, off a fresh re-read.

    The limit check runs inside the ``.worktrees`` flock (TOCTOU safety), so
    this drives the full ``init`` path -- the real flock + a live re-read of
    the registry, not a patched ``_live_descendants``.
    """
    parent, _ = _spawn_parent_child(git_repo, monkeypatch)
    # parent already has one live child (kid); cap it there
    parent.config.set('max_children', 1)
    parent_wt = parent.worktree
    node_dir = parent_wt / '.fractal' / 'main.parent'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    with pytest.raises(ValueError, match='Max children reached'):
        Node(git_repo).init(name='kid2')
    monkeypatch.delenv('_NODE')
    # the rejected spawn created nothing: no second worktree, no registry row
    assert fractal.util.git.find_worktree(git_repo, 'main.parent.kid2') is None
    branches = {row['node'] for row in parent.child_list()}
    assert 'main.parent.kid2' not in branches


# ------ rearm gates


def test_continue_re_checks_width_gate(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continue needs a free width slot: respawn-to-cap refuses the re-arm.

    Spawn-to-cap -> settle -> respawn hands the settled node's slot to its
    replacement, so ``--continue`` re-checks the parent's ``max_children``
    with the spawn gate's unsettled counting and refuses -- the spawn
    refusal, no override flag -- while the replacement holds the slot.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    parent.config.set('max_children', 1)
    # settle the child, then spawn its replacement into the freed slot
    child.status_set('exited')
    parent_wt = parent.worktree
    node_dir = parent_wt / '.fractal' / 'main.parent'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    Node(git_repo).init(name='kid2')
    monkeypatch.delenv('_NODE')
    # the idle replacement holds the only slot -- the continue must refuse
    # (clean acknowledges init's uncommitted .gitattributes edit, keeping the
    # dirty-tree guard out of this gate's way)
    run_scripts = _stub_run_script(monkeypatch, child)
    with pytest.raises(ValueError, match='Max children reached'):
        child.start(continue_run=True, clean=True)
    assert run_scripts == []
    # the refused node stays settled -- no half-armed state holds a slot
    assert child.status() == 'exited'
    # settling the replacement frees the slot; the continue re-arms to idle
    kid2 = Node(git_repo / '.worktrees' / 'main.parent.kid2')
    kid2.status_set('completed')
    child.start(continue_run=True, clean=True)
    assert child.status() == 'idle'


def test_continue_max_cost_refuses_when_parent_is_orphaned(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``--continue --max-cost`` re-arm refuses cleanly if the parent is gone.

    The launch-time retune records on the parent; if the parent's worktree was
    pruned out of band the parent node can't be constructed, so the re-arm must
    raise a clean error rather than dereference ``None`` (an AttributeError),
    and never reach the launch.
    """
    _, child = _spawn_parent_child(git_repo, monkeypatch)
    child.status_set('exited')
    # orphan the parent out of band -- its worktree is what child.parent needs
    parent_wt = git_repo / '.worktrees' / 'main.parent'
    _git(git_repo, 'worktree', 'remove', '--force', f'{parent_wt}')
    run_scripts = _stub_run_script(monkeypatch, child)
    with pytest.raises(RuntimeError, match='parent worktree is gone'):
        child.start(continue_run=True, max_cost=5.0, clean=True)
    assert run_scripts == []


def test_continue_re_checks_descendant_gate(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continue binds on every ancestor's ``max_descendants``, like a spawn.

    With the grandchild ``g`` settled, its slot in ``p``'s subtree goes to
    the re-armed intermediate ``c``; a cap of 1 on ``p`` refuses ``g``'s
    continue naming the ancestor, and raising it to 2 admits the same
    continue.
    """
    p, c, g = _spawn_chain(git_repo, monkeypatch)
    # settle the grandchild; the intermediate holds p's only subtree slot
    g.status_set('exited')
    c.status_set('idle')
    p.config.set('max_descendants', 1)
    # clean acknowledges init's uncommitted .gitattributes edit, keeping the
    # dirty-tree guard out of this gate's way
    run_scripts = _stub_run_script(monkeypatch, g)
    with pytest.raises(ValueError, match='Max descendants reached') as excinfo:
        g.start(continue_run=True, clean=True)
    assert p.branch in str(excinfo.value)
    assert run_scripts == []
    # a cap of 2 has a free slot for the re-arm
    p.config.set('max_descendants', 2)
    g.start(continue_run=True, clean=True)
    assert g.status() == 'idle'


def test_spawn_gate_reconciles_crashed_active(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed-but-active child stops holding a width slot at the spawn gate.

    A loop that dies out of band leaves ``active`` with no tmux session; the
    gate heals it (persisted, the same reconcile ``list`` applies) before
    counting, so the dead loop's slot is free and the spawn proceeds instead
    of bouncing off a phantom child.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    parent.config.set('max_children', 1)
    # the child's loop dies out of band: status active, session gone
    sessions = frozenset({parent.tmux_session})
    monkeypatch.setattr('fractal.util.tmux.probe', lambda: sessions)
    parent_wt = parent.worktree
    node_dir = parent_wt / '.fractal' / 'main.parent'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    Node(git_repo).init(name='kid2')
    monkeypatch.delenv('_NODE')
    # the spawn landed and the heal persisted the honest terminal
    assert fractal.util.git.find_worktree(git_repo, 'main.parent.kid2') is not None
    assert child.status() == 'exited'


def test_continue_gate_reconciles_crashed_active(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed sibling's phantom slot never blocks a continue.

    The re-arm counts its crashed-but-active sibling the same way a spawn
    does: healed first (persisted), so the dead loop frees the only width
    slot and the continue proceeds.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # spawn a sibling and settle it (the node the continue re-arms)
    parent_wt = parent.worktree
    node_dir = parent_wt / '.fractal' / 'main.parent'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    Node(git_repo).init(name='kid2')
    monkeypatch.delenv('_NODE')
    kid2 = Node(git_repo / '.worktrees' / 'main.parent.kid2')
    kid2.status_set('stopped')
    parent.config.set('max_children', 1)
    # the child's loop dies out of band: status active, session gone
    sessions = frozenset({parent.tmux_session})
    monkeypatch.setattr('fractal.util.tmux.probe', lambda: sessions)
    # clean acknowledges init's uncommitted .gitattributes edit, keeping the
    # dirty-tree guard out of this gate's way
    _stub_run_script(monkeypatch, kid2)
    kid2.start(continue_run=True, clean=True)
    # the continue landed and the heal persisted the honest terminal
    assert kid2.status() == 'idle'
    assert child.status() == 'exited'


def test_unretire_re_checks_width_gate(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle-restoring unretire needs a free width slot, like a continue.

    Retire-to-cap -> respawn hands the retired node's slot to its
    replacement, so an unretire that would land ``idle`` re-checks the
    parent's ``max_children`` with the spawn gate's unsettled counting and
    refuses -- the spawn refusal, no override flag -- while the replacement
    holds the slot.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    parent.config.set('max_children', 1)
    # retire the idle child, then spawn its replacement into the freed slot
    child.status_set('idle')
    _stub_run_script(monkeypatch, child)
    child.retire()
    parent_wt = parent.worktree
    node_dir = parent_wt / '.fractal' / 'main.parent'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    Node(git_repo).init(name='kid2')
    monkeypatch.delenv('_NODE')
    # the idle replacement holds the only slot -- the unretire must refuse
    # (a fresh recorder: the retire above already ran its script)
    run_scripts = _stub_run_script(monkeypatch, child)
    with pytest.raises(ValueError, match='Max children reached'):
        child.unretire()
    assert run_scripts == []
    # the refused node stays retired -- no half-restored state holds a slot
    assert child.status() == 'retired'
    # settling the replacement frees the slot; the unretire restores idle
    kid2 = Node(git_repo / '.worktrees' / 'main.parent.kid2')
    kid2.status_set('completed')
    child.unretire()
    assert child.status() == 'idle'


def test_unretire_re_checks_descendant_gate(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle-restoring unretire binds on every ancestor's ``max_descendants``.

    With the grandchild ``g`` retired, its slot in ``p``'s subtree goes to
    the re-armed intermediate ``c``; a cap of 1 on ``p`` refuses ``g``'s
    unretire naming the ancestor, and raising it to 2 admits the same
    unretire.
    """
    p, c, g = _spawn_chain(git_repo, monkeypatch)
    # retire the idle grandchild; the intermediate holds p's only subtree slot
    g.status_set('idle')
    _stub_run_script(monkeypatch, g)
    g.retire()
    c.status_set('idle')
    p.config.set('max_descendants', 1)
    # a fresh recorder: the retire above already ran its script
    run_scripts = _stub_run_script(monkeypatch, g)
    with pytest.raises(ValueError, match='Max descendants reached') as excinfo:
        g.unretire()
    assert p.branch in str(excinfo.value)
    assert run_scripts == []
    # a cap of 2 has a free slot for the restore
    p.config.set('max_descendants', 2)
    g.unretire()
    assert g.status() == 'idle'


def test_unretire_settled_restore_passes_at_cap(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settled restore is admitted at cap -- it returns no node to play.

    Unretiring a node whose pre-retire status was settled changes nothing
    the width/descendant gates count, so a full tree does not block it:
    the node lands back on its settled status (a later continue still pays
    the gate).
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    parent.config.set('max_children', 1)
    # retire the completed child, then spawn its replacement into the slot
    child.status_set('completed')
    _stub_run_script(monkeypatch, child)
    child.retire()
    parent_wt = parent.worktree
    node_dir = parent_wt / '.fractal' / 'main.parent'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    Node(git_repo).init(name='kid2')
    monkeypatch.delenv('_NODE')
    # the replacement holds the only slot, but a completed restore needs none
    child.unretire()
    assert child.status() == 'completed'


def test_unretire_gate_reconciles_crashed_active(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed sibling's phantom slot never blocks an idle restore.

    The restore counts its crashed-but-active sibling the same way a spawn
    does: healed first (persisted), so the dead loop frees the only width
    slot and the unretire proceeds.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # spawn a sibling and retire it while idle (the node the unretire restores)
    parent_wt = parent.worktree
    node_dir = parent_wt / '.fractal' / 'main.parent'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    Node(git_repo).init(name='kid2')
    monkeypatch.delenv('_NODE')
    kid2 = Node(git_repo / '.worktrees' / 'main.parent.kid2')
    _stub_run_script(monkeypatch, kid2)
    kid2.retire()
    parent.config.set('max_children', 1)
    # the child's loop dies out of band: status active, session gone
    sessions = frozenset({parent.tmux_session})
    monkeypatch.setattr('fractal.util.tmux.probe', lambda: sessions)
    kid2.unretire()
    # the unretire landed and the heal persisted the honest terminal
    assert kid2.status() == 'idle'
    assert child.status() == 'exited'


# ------ budget gates


def test_max_cost_enforcement(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Max cost validation: parent requires child, step <= iter <= total."""
    node = node_with_db

    # parent with max_cost requires child to have max_cost
    node.config.set('max_cost', 10.0)
    node.record.run_start()
    with pytest.raises(ValueError, match='must also set'):
        node.init('child')

    # child max_cost exceeding remaining is rejected
    with pytest.raises(ValueError, match='exceeds remaining'):
        node.init('child', max_cost=20.0)

    # max_iter_cost > max_cost is rejected
    with pytest.raises(ValueError, match='exceeds max_cost'):
        node.init('child', max_cost=5.0, max_iter_cost=8.0)

    # max_step_cost > max_iter_cost is rejected
    with pytest.raises(ValueError, match='exceeds max_iter_cost'):
        node.init('child', max_cost=5.0, max_iter_cost=2.0, max_step_cost=3.0)

    # max_step_cost > max_cost is rejected (no iter cap set)
    with pytest.raises(ValueError, match='exceeds max_cost'):
        node.init('child', max_cost=5.0, max_step_cost=8.0)

    # valid allocation passes (step <= iter <= total)
    _stub_run_script(monkeypatch, node)
    node.init('child', max_cost=5.0, max_iter_cost=2.0, max_step_cost=1.0)


def test_max_cost_bounds_child_by_subtree_remaining(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child's cap is bounded by the parent's remaining *subtree* budget.

    The parent's ``max_cost`` covers itself plus every descendant, so a child
    may claim only what is left after the parent's own spend. The check is
    per-child, not summed -- two children may each fit the remainder
    (oversubscription), with the runtime subtree ceiling as the real ceiling.
    """
    node = node_with_db
    node.config.set('max_cost', 10.0)
    run_id = node.record.run_start()
    # record $4 of the parent's own spend -> $6 of the subtree budget remains
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    step_id = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.record.step_cost(step_id=step_id, cost=4.0)
    node.record.step_end(step_id=step_id, status='completed', exit_code=0)

    # a child claiming more than the $6 remainder is rejected
    with pytest.raises(ValueError, match='exceeds remaining'):
        node.init('greedy', max_cost=7.0)

    # within the remainder is allowed -- and a second child may claim it too
    # (per-child check, oversubscription permitted)
    _stub_run_script(monkeypatch, node)
    node.init('first', max_cost=6.0)
    node.init('second', max_cost=6.0)


def test_max_cost_child_bound_re_arms_after_prior_run(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drained prior run never shrinks the spawn gate's budget bound.

    Runs are isolated by design: with no active run, the next run starts
    fresh, so a child may claim up to the parent's full ``max_cost`` --
    prior-run spend is invisible to the bound.
    """
    node = node_with_db
    node.config.set('max_cost', 10.0)
    # a prior run records $8, then ends -- no active run remains
    run_id = node.record.run_start()
    _record_step_cost(node, run_id=run_id, cost=8.0)
    node.record.run_end(run_id=run_id, status='exited', exit_code=1)
    # the next run starts fresh, so the full cap is claimable
    _stub_run_script(monkeypatch, node)
    node.init('fresh', max_cost=10.0)


def test_cost_caps_refuse_an_agent_that_records_no_cost(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every path that would cap a muse node's cost refuses, naming why.

    muse records no dollar spend, so a cap on it could never trip, and a
    capped parent requires every child to carry one: a capped init, a
    spawn under a capped parent, a retune, and a launch over a cap armed by
    hand all refuse before anything changes.
    """
    Node(git_repo).init(agent='muse', user=True)
    worktrees = git_repo / '.worktrees'
    # a muse node with its own cap
    with pytest.raises(ValueError, match='muse records no dollar cost'):
        Node(git_repo).init(name='capped', max_cost=5.0)
    assert not (worktrees / 'main.capped').exists()
    # a muse child under a capped parent
    Node(git_repo).init(name='parent', agent='claude', max_cost=10.0)
    parent_dir = worktrees / 'main.parent' / '.fractal' / 'main.parent'
    monkeypatch.setenv('_NODE', f'{parent_dir}')
    with pytest.raises(ValueError, match=r"cannot run under 'main\.parent'"):
        Node(git_repo).init(name='kid', agent='muse')
    monkeypatch.delenv('_NODE')
    assert not (worktrees / 'main.parent.kid').exists()
    # an uncapped muse node takes no cap on a retune
    Node(git_repo).init(name='svc')
    with pytest.raises(ValueError, match='muse records no dollar cost'):
        Node(git_repo).child_retune('svc', max_cost=5.0)
    svc = Node(worktrees / 'main.svc')
    assert svc.config.get('max_cost') is None
    # nor at launch, over a cap the steering path wrote into its config
    svc.config.set('max_cost', 5.0)
    with pytest.raises(ValueError, match='muse records no dollar cost'):
        svc.start()
