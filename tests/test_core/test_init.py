"""Node init and provisioning.

Covers worktree/branch/registry creation, user-node bootstrap, agent
and sub-project inheritance, template seeding, ambient-node
resolution, and the init-time refusals (existing node, missing wiki,
nested worktrees, bad durations).
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import tomllib
from typing import Any

import pytest
import typer

import fractal.core
from fractal.cli.utils import init_node, resolve_init_target, resolve_node
from fractal.core.node import Node
from tests._helpers import _git

from .conftest import (
    _make_git_repo,
    _parse_project_dir,
    _resolve_branch,
    _spawn_parent_child,
)

__all__ = [
    'test_init_creates_node_structure',
    'test_init_scope_places_node_dir_at_project_root',
    'test_init_sets_the_worktree_commit_identity',
    'test_init_populates_skills_and_supports_reset',
    'test_reset_reseeds_scripts',
    'test_reset_clears_stale_registry_caps',
    'test_reset_re_anchors_base_at_the_reset_point',
    'test_init_materializes_title_in_registry',
    'test_init_on_existing_node_refuses_loudly',
    'test_init_refuses_when_an_active_fractal_shares_the_repo_name',
    'test_init_allows_a_second_tree_while_a_sibling_node_runs',
    'test_start_refuses_a_foreign_session_name_collision',
    'test_reset_refuses_a_running_or_frozen_node',
    'test_reset_heals_a_crashed_node_under_inits_flock',
    'test_init_refuses_case_variant_of_existing_sibling',
    'test_root_anchors_central_db',
    'test_user_init_on_a_dotted_branch',
    'test_user_init_rejects_a_dot_nested_second_root',
    'test_user_init_rejects_detached_head',
    'test_user_init_stores_and_updates_agent',
    'test_child_inherits_agent_from_ancestor',
    'test_child_inherits_provider_when_the_agent_routes',
    'test_child_inherits_agent_config_from_parent',
    'test_init_stores_unset_booleans_as_null',
    'test_child_inherits_steps_and_scripts_from_parent',
    'test_init_template_seeds_steps_and_validates_the_contract',
    'test_init_template_deploys_every_surface_from_the_commit',
    'test_init_template_reads_the_fork_commit',
    'test_init_template_reads_a_folder_deleted_from_disk',
    'test_init_template_include_exclude_recorded_and_honored',
    'test_init_template_listing_trims_before_the_contract_checks',
    'test_init_template_preset_fills_unset_flags',
    'test_init_template_preset_reserve_resolves_against_the_merged_ceiling',
    'test_init_template_refusals_leave_nothing_behind',
    'test_init_template_refuses_a_boundary_path',
    'test_init_template_refuses_agent_credentials',
    'test_init_template_preset_refuses_disallowed_keys',
    'test_init_template_preset_caps_refuse_like_flags',
    'test_init_template_resolves_the_same_path_from_any_cwd',
    'test_reset_without_template_drops_the_provenance',
    'test_pin_without_a_template_still_validates',
    'test_init_template_seeds_and_validates_the_fill_sheet',
    'test_init_template_fills_slots_from_values_set_and_pin',
    'test_init_template_slot_refusals_fire_before_any_worktree',
    'test_init_template_docket_anchor_defaults_to_the_fork_commit',
    'test_child_inherits_skills_only_on_request',
    'test_child_inherits_config_preferences_not_caps',
    'test_init_requires_resolvable_agent',
    'test_init_refuses_unsupported_agent',
    'test_init_refuses_unsupported_provider',
    'test_init_refuses_quoted_agent_command',
    'test_init_requires_project_wiki',
    'test_init_refuses_a_base_without_a_worktree',
    'test_init_rejects_inside_worktrees',
    'test_user_init_repairs_stranded_database',
    'test_user_init_rejects_second_project_on_same_branch',
    'test_init_rejects_subsecond_duration',
    'test_init_accepts_fractional_duration_under_comma_locale',
    'test_init_rejects_incoherent_pacing',
    'test_init_rejects_absolute_or_traversal_scope',
    'test_init_normalizes_scope_before_validating',
    'test_child_inherits_subproject_from_parent',
    'test_init_ignores_cross_repo_ambient_node',
    'test_init_node_default_path_ignores_cross_repo_ambient',
    'test_resolve_node_targets_subproject_user_node',
    'test_resolve_init_target_anchors_subproject_at_git_root',
    'test_resolve_init_target_refuses_linked_worktree',
]


# ------ helpers


def _commit_template(
    repo: pathlib.Path,
    path: str,
    files: dict[str, str],
) -> str:
    """Write ``files`` under ``repo/<path>``, commit the folder, return the sha.

    Template bytes deploy from git, never from the working copy, so every
    template fixture must be committed before the init that reads it.
    """
    for name, content in files.items():
        target = repo / path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
    _git(repo, 'add', path)
    _git(repo, 'commit', '-m', f'template {path}')
    return _git(repo, 'rev-parse', 'HEAD').stdout.strip()


# ------ provisioning


def test_init_creates_node_structure(initialized_node: dict[str, Any]) -> None:
    """Node init creates worktree with complete node structure."""
    project_dir = initialized_node['project_dir']
    node_dir = initialized_node['node_dir']
    branch = initialized_node['branch']
    repo = initialized_node['repo']

    # worktree exists at .worktrees/<branch>/
    worktree = repo / '.worktrees' / branch
    assert worktree.is_dir()

    # project wiki inherited into the worktree
    assert (worktree / 'wiki' / '_index.md').is_file()

    # node data directory exists
    assert node_dir.is_dir()

    # core files copied (AGENTS.md merged into NODE.md; no CLAUDE.md symlink)
    assert (node_dir / 'NODE.md').is_file()
    assert not (node_dir / 'AGENTS.md').exists()
    assert not (node_dir / 'CLAUDE.md').exists()

    # steps directory populated
    steps = list((node_dir / 'steps').glob('*.md'))
    assert len(steps) >= 3

    # scripts holds only the mutable, per-node scripts; the immutable machinery
    # (the loop and modes/) runs from the package, not here
    assert (node_dir / 'scripts' / 'setup.sh').is_file()
    assert (node_dir / 'scripts' / 'lint.sh').is_file()
    assert not (node_dir / 'modes').exists()

    # skills directory populated, each with a SKILL.md
    for skill in ('fractal', 'wiki', 'memory'):
        assert (node_dir / 'skills' / skill / 'SKILL.md').is_file()

    # skills symlinked into agent discovery dirs
    for agent_dir in ('.claude', '.codex', '.grok', '.opencode', '.omp', '.agents'):
        link = node_dir / agent_dir / 'skills'
        assert link.is_symlink()
        assert (link / 'fractal' / 'SKILL.md').is_file()

    # memory wiki initialized
    assert (node_dir / 'memory' / '_index.md').is_file()

    # no per-node database -- the central DB lives at the root user node
    assert not (node_dir / '.db').exists()
    assert (repo / '.fractal' / 'main' / '.db').is_file()

    # radio seeded with default channels (worker nodes, not just user nodes)
    node = Node(project_dir)
    channels = {channel['channel'] for channel in node.radio.channels()}
    assert channels == {'public', 'private', 'inbox', 'outbox'}

    # agent config copied (claude)
    assert (node_dir / '.claude').is_dir()

    # codex credentials are symlinked to the global codex home, never copied per
    # node (codex writes auth.json in-place through the link, so it stays current)
    codex_auth = node_dir / '.codex' / 'auth.json'
    assert codex_auth.is_symlink()
    codex_home = os.environ.get('CODEX_HOME') or os.path.expanduser('~/.codex')
    assert str(codex_auth.readlink()) == os.path.join(codex_home, 'auth.json')

    # grok credentials follow the same write-through contract
    grok_auth = node_dir / '.grok' / 'auth.json'
    assert grok_auth.is_symlink()
    grok_home = os.environ.get('GROK_HOME') or os.path.expanduser('~/.grok')
    assert str(grok_auth.readlink()) == os.path.join(grok_home, 'auth.json')

    # branch name is prefixed by the user node (top-level child of the root)
    assert branch == 'main.task'


def test_init_scope_places_node_dir_at_project_root(git_repo: pathlib.Path) -> None:
    """Init with ``--scope`` places node dir at project root."""
    # create subdirectory
    subdir = git_repo / 'packages' / 'core'
    subdir.mkdir(parents=True)

    node = Node(git_repo)
    node.init(agent='claude', user=True)
    output = node.init(name='scoped', scope=['packages/core'])
    project_dir = _parse_project_dir(output)

    # .fractal/ is at project root, not inside scope
    branch = _resolve_branch(project_dir)
    node_dir = project_dir / '.fractal' / branch
    assert node_dir.is_dir()

    # config records scope as a list of roots
    scoped_node = Node(project_dir)
    assert scoped_node.config.get('scope') == ['packages/core']


def test_init_sets_the_worktree_commit_identity(git_repo: pathlib.Path) -> None:
    """A node's commits are authored under its dotted name, the user's email.

    Init writes a worktree-scoped ``user.name`` so every commit the node
    makes attributes to the node itself (author-based membership for changed
    listings); the unset email inherits the user's own.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    output = node.init(name='task')
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    # the worktree-scoped identity is the dotted node name
    name = _git(project_dir, 'config', '--worktree', 'user.name')
    assert name.stdout.strip() == branch
    # a commit in the worktree carries the node's name and the user's email
    (project_dir / 'work.txt').write_text('output\n', encoding='utf-8')
    _git(project_dir, 'add', 'work.txt')
    _git(project_dir, 'commit', '-m', 'work')
    author = _git(project_dir, 'log', '-1', '--format=%an %ae')
    assert author.stdout.strip() == f'{branch} test@test.com'
    # the main checkout keeps the user's own identity
    author = _git(git_repo, 'log', '-1', '--format=%an')
    assert author.stdout.strip() == 'Test'


def test_init_populates_skills_and_supports_reset(git_repo: pathlib.Path) -> None:
    """Init populates the standard skills and supports reset."""
    node = Node(git_repo)
    node.init(agent='claude', user=True)

    # init creates the standard skills
    output = node.init(name='task')
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    node_dir = project_dir / '.fractal' / branch
    skill_dirs = [d.name for d in (node_dir / 'skills').iterdir() if d.is_dir()]
    assert {'fractal', 'wiki', 'memory'} <= set(skill_dirs)

    # reset recreates node files
    output = node.init(name='task', reset=True)
    assert 'Initialized' in output


def test_reset_reseeds_scripts(git_repo: pathlib.Path) -> None:
    """``--reset`` returns ``scripts/`` to its seed source.

    NODE.md, steps, plans, tmp, memory, skills, agent dirs, and config
    all pre-clear on reset; scripts surviving it would carry a node's
    tuned ``test.sh`` through what is documented as a full
    re-initialization.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    output = node.init(name='task')
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    test_sh = project_dir / '.fractal' / branch / 'scripts' / 'test.sh'
    stock = test_sh.read_text(encoding='utf-8')
    test_sh.write_text('# tuned\n', encoding='utf-8')
    node.init(name='task', reset=True)
    assert test_sh.read_text(encoding='utf-8') == stock


def test_reset_clears_stale_registry_caps(git_repo: pathlib.Path) -> None:
    """``--reset`` clears registry caps the re-init omits.

    Reset returns a stock node whose ``config.json`` carries only the new
    caps, and ``node list`` reads limits from the central ``nodes`` row
    (blank means unlimited) -- so an omitted cap must clear on the row too.
    Reconcile cannot heal it later (config-absent keys are left alone), so
    a surviving spawn-time cap would forever report a bound the node no
    longer enforces.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    node.init(name='task', max_cost=5.0, max_children=3)
    # the re-init sets max_depth only -- the old cost/width caps must go
    node.init(name='task', reset=True, max_depth=2)
    rows = {row['node']: row for row in node.db.read('nodes')}
    row = rows['main.task']
    assert row['max_cost'] is None
    assert row['max_children'] is None
    assert row['max_depth'] == 2


def test_reset_re_anchors_base_at_the_reset_point(git_repo: pathlib.Path) -> None:
    """``--reset`` stamps the reused tip as the new incarnation's fork point.

    History rows persist across reset, so a re-init whose init event carries
    no fork sha would fall back to the original fork -- reporting the dead
    incarnation's whole contribution as ``base`` until the first post-reset
    commit event lands, then silently flipping to post-reset scope.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    output = node.init(name='task')
    project_dir = _parse_project_dir(output)
    worker = Node(project_dir)
    # the dead incarnation's work: one committed file, its commit event logged
    (project_dir / 'old_work.txt').write_text('old\n', encoding='utf-8')
    _git(project_dir, 'add', 'old_work.txt')
    _git(project_dir, 'commit', '-m', 'old work')
    sha = _git(project_dir, 'rev-parse', 'HEAD').stdout.strip()
    worker.record.event_start('commit', metadata=sha)
    node.init(name='task', reset=True)
    # base starts empty: no scope reaches past the re-init
    assert not worker.files.list(since='base')
    # and covers exactly the new incarnation's work once it commits
    (project_dir / 'new_work.txt').write_text('new\n', encoding='utf-8')
    _git(project_dir, 'add', 'new_work.txt')
    _git(project_dir, 'commit', '-m', 'new work')
    sha = _git(project_dir, 'rev-parse', 'HEAD').stdout.strip()
    worker.record.event_start('commit', metadata=sha)
    changed = {entry['path'] for entry in worker.files.list(since='base')}
    assert changed == {'new_work.txt'}


def test_init_materializes_title_in_registry(initialized_node: dict) -> None:
    """A real init stamps the de-slugged title onto the central registry row.

    The GUI reads display names straight from the ``nodes`` table, so the title
    materialized at init (node name ``task`` -> ``Task``) must land on the row,
    not only in the worker's ``config.json``.
    """
    root = Node(initialized_node['repo'])
    rows = {row['node']: row for row in root.db.read('nodes')}
    assert rows[initialized_node['branch']]['title'] == 'Task'


def test_init_on_existing_node_refuses_loudly(
    git_repo: pathlib.Path,
) -> None:
    """Re-init of an existing node fails loudly and leaves config untouched.

    Were ``node init`` against an already-initialized node to exit 0 with
    the old node fully in place, the requested caps would silently never
    land while the operator believed they applied. Reuse is explicit in
    this CLI (``node start --continue``, ``--reset``), so an implicit adopt
    is refused by name.
    """
    Node(git_repo).init(agent='claude', user=True)
    Node(git_repo).init(name='retune', max_cost=0.10)
    node = Node(git_repo / '.worktrees' / 'main.retune')
    # re-init with different caps: refused, and the node is untouched
    with pytest.raises(ValueError, match=r"'main\.retune' already exists"):
        Node(git_repo).init(name='retune', max_cost=100.0)
    assert node.config.get('max_cost') == 0.10


def test_init_refuses_when_an_active_fractal_shares_the_repo_name(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`fractal init` refuses if another live fractal shares this basename.

    Two repositories with the same directory basename produce the same tmux
    session namespace (``<basename> (<branch>)``), so their node sessions --
    and ``node kill``, which resolves by that global name -- would collide.
    tmux names carry no repo path, so a session counts as ours only when its
    name derives from a branch this repo has checked out; one that no
    checkout accounts for is a different repo's, and the init is refused
    rather than creating a fractal that cannot be operated safely.
    """
    repo_name = git_repo.name.replace('.', '-').replace(':', '-')
    sessions = frozenset({f'{repo_name} (main.other)'})
    monkeypatch.setattr('fractal.util.tmux.probe', lambda *, socket=None: sessions)
    with pytest.raises(RuntimeError, match='Another active fractal'):
        Node(git_repo).init(agent='claude', user=True)


def test_init_allows_a_second_tree_while_a_sibling_node_runs(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running sibling tree never reads as a foreign fractal at init.

    One repository carries several trees, so the namespace guard must place
    a live session before refusing: a name derived from a branch this repo
    has checked out is our own tree's node, not a stranger sharing the
    basename, and initializing the second tree beside it has to work.
    """
    Node(git_repo).init(agent='claude', user=True)
    Node(git_repo).init(name='task', local=True)
    task = Node(git_repo / '.worktrees' / 'main.task')
    sessions = frozenset({task.tmux_session})
    monkeypatch.setattr('fractal.util.tmux.probe', lambda *, socket=None: sessions)
    subprocess.run(
        ['git', 'checkout', '-b', 'second'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )
    assert Node(git_repo).init(agent='claude', user=True)
    assert [user.branch for user in Node.user_nodes(git_repo)] == ['main', 'second']


def test_start_refuses_a_foreign_session_name_collision(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`node start` refuses when its session name is already live elsewhere.

    A different repository sharing this one's basename and node name yields
    an identical tmux session name; starting anyway would make ``node
    kill``/attach resolve ambiguously across both trees. The node is idle,
    so a live session under its name is necessarily foreign.
    """
    Node(git_repo).init(agent='claude', user=True)
    Node(git_repo).init(name='worker', local=True)
    node = Node(git_repo / '.worktrees' / 'main.worker')
    sessions = frozenset({node.tmux_session})
    monkeypatch.setattr('fractal.util.tmux.probe', lambda *, socket=None: sessions)
    with pytest.raises(RuntimeError, match='already active for another fractal'):
        node.start()


@pytest.mark.parametrize(
    argnames=('status', 'remedy'),
    argvalues=[
        ('active', 'Stop or kill it first'),
        ('paused', 'Resume or kill it first'),
    ],
)
def test_reset_refuses_a_running_or_frozen_node(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    remedy: str,
) -> None:
    """``init --reset`` refuses a running or frozen node instead of wiping it.

    Reset ``rm -rf``s the node dir, so a live loop's step files or a paused
    node's frozen run context would be destroyed -- it refuses exactly as
    delete, merge, and retire do, naming the same wind-down remedy.
    """
    Node(git_repo).init(agent='claude', user=True)
    Node(git_repo).init(name='task')
    node = Node(git_repo / '.worktrees' / 'main.task')
    node.status_set(status)
    # an active node reads live only behind a session (else the reconcile
    # heals it exited and reset would rightly proceed); paused is never healed
    monkeypatch.setattr(Node, '_tmux_session_exists', lambda self: True)
    with pytest.raises(RuntimeError, match=remedy):
        Node(git_repo).init(name='task', reset=True)
    # the node survived: reset never ran
    assert node.exists()


def test_reset_heals_a_crashed_node_under_inits_flock(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``init --reset`` heals a crashed-but-active target instead of refusing.

    Init holds the ``.worktrees`` flock across the reset, so the pre-reset
    heal's terminal writes must reuse that flock rather than re-acquire it:
    the crashed node stamps ``exited`` under init's lock and the reset
    proceeds instead of refusing over a loop that no longer runs.
    """
    Node(git_repo).init(agent='claude', user=True)
    Node(git_repo).init(name='task')
    node = Node(git_repo / '.worktrees' / 'main.task')
    node.status_set('active')
    # the loop died out of band: no session backs the active status
    monkeypatch.setattr('fractal.util.tmux.probe', lambda *, socket=None: frozenset())
    output = Node(git_repo).init(name='task', reset=True)
    assert 'Initialized' in output
    assert node.status() == 'idle'


def test_init_refuses_case_variant_of_existing_sibling(
    git_repo: pathlib.Path,
) -> None:
    """A name differing from a sibling only by case is refused, not aliased.

    On a case-insensitive filesystem the case-variant's worktree path
    resolves onto the sibling's dir, so init.sh would stamp a spurious init
    event on the sibling (re-flooring its base diff anchor), register a
    phantom registry row whose deletion prunes the sibling's branch, and a
    ``--reset`` would wipe the sibling's node files through the alias.
    """
    Node(git_repo).init(agent='claude', user=True)
    Node(git_repo).init(name='task')
    # the alias exists only where the filesystem is case-insensitive; on a
    # case-sensitive one the case-variant is a legitimately distinct node
    if not (git_repo / '.worktrees' / 'main.TASK').is_dir():
        pytest.skip('requires a case-insensitive filesystem')
    with pytest.raises(ValueError, match='case-insensitive'):
        Node(git_repo).init(name='TASK')
    # --reset hits the same refusal -- it would otherwise rm -rf the
    # sibling's node dir through the alias
    with pytest.raises(ValueError, match='case-insensitive'):
        Node(git_repo).init(name='TASK', reset=True)
    # the sibling is untouched: no phantom row, no second init event
    root = Node(git_repo)
    rows = {row['node'] for row in root.db.read('nodes')}
    assert 'main.TASK' not in rows
    events = root.db.read('events', where={'node': 'main.task', 'event': 'init'})
    assert len(events) == 1


def test_root_anchors_central_db(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The root user node anchors the one central database via its own ``root`` key.

    ``_init_user`` is the sole writer of the root's ``root`` config (init.sh
    plumbs ``--root`` for children only), so it must name the root's own branch
    -- otherwise ``Node.db`` joins on ``None``. Every node then resolves the
    same ``.db`` file from that key.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    root = Node(git_repo)
    # the root names itself, anchoring the tree's database
    assert root.config.get('root') == root.branch
    # parent and child resolve the one central database in the root's data dir
    assert parent.db.path == root.db.path
    assert child.db.path == root.db.path


# ------ agent configuration


def test_user_init_on_a_dotted_branch(tmp_path: pathlib.Path) -> None:
    """A root git branch containing dots inits (and re-inits) cleanly.

    The root's git branch is arbitrary user input (``v1.0``,
    ``stable-2.1``) -- only child name segments are dot-validated.
    Parenthood derives only below the tree root, so a dotted root must
    not read as the child of a phantom node (which would leave init
    half-complete and every re-run failing in the repair path). A
    ``--parent`` send from the root refuses cleanly the same way.
    """
    repo = _make_git_repo(tmp_path / 'dotted')
    subprocess.run(
        ['git', 'checkout', '-b', 'v1.0'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    node = Node(repo)
    assert node.init(agent='claude', user=True)
    # the re-init repair path must survive the dotted branch too
    assert node.init(agent='claude', user=True)
    # a parent-send from the tree root refuses cleanly, never routing to
    # a phantom 'v1'
    with pytest.raises(ValueError, match='No parent node'):
        node.radio.send(parent=True, subject='s', data='d', priority=3)


@pytest.mark.parametrize(
    argnames=('first', 'second'),
    argvalues=[
        ('v1', 'v1.0'),
        ('v1.0', 'v1'),
    ],
    ids=['plain_first', 'dotted_first'],
)
def test_user_init_rejects_a_dot_nested_second_root(
    tmp_path: pathlib.Path,
    first: str,
    second: str,
) -> None:
    """A second tree may not dot-nest under an existing one, in either order.

    A dotted root is fine alone, but ``.`` is the node hierarchy separator:
    a tree rooted at ``v1.0`` reads as a node inside one rooted at ``v1``, so
    every ``<root>.*`` scope (destroy, reset, the ancestor walk) would cross
    between them. The second init is refused whichever root came first, and
    the existing tree is left intact.
    """
    # the repo dir name feeds the wiki project name, so keep it dot-free
    repo = _make_git_repo(tmp_path / 'repo')
    for branch in (first, second):
        subprocess.run(
            ['git', 'checkout', '-b', branch],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        if branch == first:
            assert Node(repo).init(agent='claude', user=True)
    with pytest.raises(ValueError, match='collides with the tree rooted at'):
        Node(repo).init(agent='claude', user=True)
    # the established tree is untouched and still the only one
    roots = [user.branch for user in Node.user_nodes(repo)]
    assert roots == [first]


def test_user_init_rejects_detached_head(tmp_path: pathlib.Path) -> None:
    """A detached checkout is refused up front, leaving no init state behind.

    Git resolves a detached HEAD (a tag clone, CI checkout, mid-bisect) to
    the literal branch name ``HEAD`` -- a pseudo-ref that re-resolves to
    whatever is checked out later, so a tree anchored on it would orphan its
    registry and strand the baseline as dangling commits at the next
    checkout. Init must name the remedy and write nothing.
    """
    repo = _make_git_repo(tmp_path / 'detached')
    subprocess.run(
        ['git', 'checkout', '--detach'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    with pytest.raises(ValueError, match='detached HEAD'):
        Node(repo).init(agent='claude', user=True)
    # nothing landed: no node data dir, no project cache
    assert not (repo / '.fractal').exists()
    assert not (repo / '.worktrees').exists()


def test_user_init_stores_and_updates_agent(git_repo: pathlib.Path) -> None:
    """``init --agent`` records the user node's default agent and updates it.

    The user node carries the default that child spawns inherit; a re-run with a
    new ``--agent`` updates it (init is idempotent).
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    assert node.config.get('agent') == 'claude'
    # a re-run updates the stored default
    node.init(agent='codex', user=True)
    assert node.config.get('agent') == 'codex'


def test_child_inherits_agent_from_ancestor(git_repo: pathlib.Path) -> None:
    """A child spawned without ``--agent`` inherits the nearest ancestor's agent.

    The user node's default propagates to children; an explicit ``--agent``
    overrides it.
    """
    Node(git_repo).init(agent='claude', user=True)
    # no --agent: inherit the user node's default
    Node(git_repo).init(name='task')
    inherited = Node(git_repo / '.worktrees' / 'main.task')
    assert inherited.config.get('agent') == 'claude'
    # explicit --agent overrides inheritance
    Node(git_repo).init(name='other', agent='codex')
    overridden = Node(git_repo / '.worktrees' / 'main.other')
    assert overridden.config.get('agent') == 'codex'


def test_child_inherits_provider_when_the_agent_routes(
    git_repo: pathlib.Path,
) -> None:
    """The provider route inherits like the agent, but only onto routed agents.

    An openrouter-defaulting ancestor materializes the route into a claude
    child's config; a route-less agent (grok) skips materialization, so the
    key never pins an agent that cannot take it. Steering-time readback
    walks the ancestors for nodes spawned before the default existed.
    """
    Node(git_repo).init(agent='claude', user=True)
    # a child spawned before any provider default exists stores none
    Node(git_repo).init(name='early')
    early = Node(git_repo / '.worktrees' / 'main.early')
    assert early.config.get('provider') is None
    # the user node gains the default; a routed child materializes it
    Node(git_repo).init(user=True, provider='openrouter')
    Node(git_repo).init(name='routed')
    routed = Node(git_repo / '.worktrees' / 'main.routed')
    assert routed.config.get('provider') == 'openrouter'
    # a route-less agent skips materialization entirely
    Node(git_repo).init(name='leaf', agent='grok')
    leaf = Node(git_repo / '.worktrees' / 'main.leaf')
    assert leaf.config.get('provider') is None
    # an explicit --provider is stored as given (validated at the seam)
    Node(git_repo).init(name='pinned', provider='openrouter')
    pinned = Node(git_repo / '.worktrees' / 'main.pinned')
    assert pinned.config.get('provider') == 'openrouter'
    # the early child reads the route back through the ancestor walk
    assert early.provider_effective() == 'openrouter'
    assert early.config.get('provider') is None


def test_child_inherits_agent_config_from_parent(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child copies the parent node's agent config files, not the package seed.

    A top-level node has no parent agent config (the user node carries none), so
    it falls back to the package seed; a deeper child then inherits the parent's
    edited config, propagating settings down the tree. Siblings do not inherit
    from each other, and codex's auth.json stays a symlink (never copied).
    """
    seed_dir = pathlib.Path(fractal.core.__file__).parent.parent / '_node' / 'agents'
    # the single config file each agent reads from its (dot-prefixed) dir
    configs = {'claude': 'settings.json', 'codex': 'config.toml'}

    def node_dir(branch: str) -> pathlib.Path:
        return git_repo / '.worktrees' / branch / '.fractal' / branch

    Node(git_repo).init(agent='claude', user=True)

    # top-level node: the user node carries no agent config -> seed fallback
    Node(git_repo).init(name='task')
    for agent, cfg in configs.items():
        seeded = node_dir('main.task') / f'.{agent}' / cfg
        assert seeded.read_text() == (seed_dir / agent / cfg).read_text()

    # edit the parent's config, then spawn a child as the parent node (_NODE)
    for agent, cfg in configs.items():
        (node_dir('main.task') / f'.{agent}' / cfg).write_text(
            f'edited-by-parent: {cfg}\n',
            encoding='utf-8',
        )
    monkeypatch.setenv('_NODE', str(node_dir('main.task')))
    Node(git_repo).init(name='sub')
    for agent, cfg in configs.items():
        inherited = node_dir('main.task.sub') / f'.{agent}' / cfg
        parent_cfg = node_dir('main.task') / f'.{agent}' / cfg
        assert inherited.read_text() == parent_cfg.read_text()
    # codex credentials stay a symlink to the global home, never copied per node
    assert (node_dir('main.task.sub') / '.codex' / 'auth.json').is_symlink()

    # a second top-level node still seeds from the package, not the sibling
    monkeypatch.delenv('_NODE')
    Node(git_repo).init(name='other')
    for agent, cfg in configs.items():
        seeded = node_dir('main.other') / f'.{agent}' / cfg
        assert seeded.read_text() == (seed_dir / agent / cfg).read_text()


def test_init_stores_unset_booleans_as_null(git_repo: pathlib.Path) -> None:
    """Flagless ``sync``/``detached`` store null, not baked defaults.

    The loop applies the defaults (sync on, continuous) at read time, so
    ``config.json`` records only what the spawner chose -- the unset/set
    distinction config inheritance relies on. Explicit flags store their
    concrete boolean either way.
    """
    Node(git_repo).init(agent='claude', user=True)
    Node(git_repo).init(name='bare')
    bare = Node(git_repo / '.worktrees' / 'main.bare')
    assert bare.config.get('sync') is None
    assert bare.config.get('detached') is None
    Node(git_repo).init(name='flagged', sync=False, detached=True)
    flagged = Node(git_repo / '.worktrees' / 'main.flagged')
    assert flagged.config.get('sync') is False
    assert flagged.config.get('detached') is True
    Node(git_repo).init(name='off', detached=False)
    off = Node(git_repo / '.worktrees' / 'main.off')
    assert off.config.get('detached') is False


def test_child_inherits_steps_and_scripts_from_parent(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--inherit=steps,scripts`` copies the parent's live files.

    The parent's trimmed step profile and tuned scripts reach the child
    verbatim (scripts stay executable); a sibling spawned without the flag
    still seeds from the package. The user node carries no steps, so a
    top-level ``--inherit=steps`` fails loudly instead of falling back,
    and an unknown surface is rejected before the script runs.
    """

    def node_dir(branch: str) -> pathlib.Path:
        return git_repo / '.worktrees' / branch / '.fractal' / branch

    Node(git_repo).init(agent='claude', user=True)
    # unknown surfaces are rejected up front
    with pytest.raises(ValueError, match='Unknown inherit surface'):
        Node(git_repo).init(name='early', inherit=['stepz'])
    # the user node carries no steps -> a top-level inherit fails loudly
    with pytest.raises(RuntimeError, match='inherit=steps'):
        Node(git_repo).init(name='early', inherit=['steps'])
    # configure a parent: trim to a leaf profile and tune test.sh
    Node(git_repo).init(name='task')
    parent_steps = node_dir('main.task') / 'steps'
    (parent_steps / '00-PREPARE.md').unlink()
    (parent_steps / '03-REVIEW.md').unlink()
    tuned = '# tuned by parent\n'
    (node_dir('main.task') / 'scripts' / 'test.sh').write_text(tuned, encoding='utf-8')
    # spawn as the parent node (_NODE): the child copies the live files
    monkeypatch.setenv('_NODE', str(node_dir('main.task')))
    Node(git_repo).init(name='sub', inherit=['steps', 'scripts'])
    child_steps = node_dir('main.task.sub') / 'steps'
    assert sorted(f.name for f in child_steps.glob('*.md')) == sorted(
        f.name for f in parent_steps.glob('*.md')
    )
    child_test = node_dir('main.task.sub') / 'scripts' / 'test.sh'
    assert child_test.read_text() == tuned
    assert os.access(child_test, os.X_OK)
    # a flagless sibling still seeds the full profile from the package
    Node(git_repo).init(name='stock')
    stock_steps = node_dir('main.task.stock') / 'steps'
    assert (stock_steps / '00-PREPARE.md').is_file()


def test_init_template_seeds_steps_and_validates_the_contract(
    git_repo: pathlib.Path,
) -> None:
    """A steps-only template seeds the node's steps from its committed bytes.

    A tracked folder holding ``config.json`` and ``steps/`` is the
    steps-only template: its step files reach the node byte-for-byte from
    the fork commit, so an uncommitted working-copy edit never deploys. A
    ``steps/`` the loop could not discover (no step files, a missing
    ``NN-`` prefix, mixed prefix widths) and combining with
    ``--inherit=steps`` -- named or reached through ``all``, two rival
    step sources -- all refuse before any worktree is created.
    """

    def node_dir(branch: str) -> pathlib.Path:
        return git_repo / '.worktrees' / branch / '.fractal' / branch

    Node(git_repo).init(agent='claude', user=True)
    # a steps/ the loop could not discover refuses here, not at the node's
    # first iteration -- no step files, an unprefixed file, mixed widths
    _commit_template(
        git_repo,
        'templates/empty',
        {'config.json': '{}\n', 'steps/notes.txt': 'no steps here\n'},
    )
    with pytest.raises(ValueError, match='no step files'):
        Node(git_repo).init(name='task', template=f'{git_repo}/templates/empty')
    _commit_template(
        git_repo,
        'templates/unprefixed',
        {'config.json': '{}\n', 'steps/scout.md': '# scout the terrain\n'},
    )
    with pytest.raises(ValueError, match='without an NN- prefix'):
        Node(git_repo).init(name='task', template=f'{git_repo}/templates/unprefixed')
    _commit_template(
        git_repo,
        'templates/mixed',
        {
            'config.json': '{}\n',
            'steps/0-SCOUT.md': '# scout the terrain\n',
            'steps/01-STRIKE.md': '# strike the target\n',
        },
    )
    with pytest.raises(ValueError, match='mixes digit prefix widths'):
        Node(git_repo).init(name='task', template=f'{git_repo}/templates/mixed')
    # a bundled steps/ and --inherit=steps are rival step sources, whether
    # the surface is named or reached through the 'all' alias
    sha = _commit_template(
        git_repo,
        'templates/crew',
        {
            'config.json': '{}\n',
            'steps/00-SCOUT.md': '# scout the terrain\n',
            'steps/01-STRIKE.md': '# strike the target\n',
        },
    )
    crew = f'{git_repo}/templates/crew'
    with pytest.raises(ValueError, match='inherit=steps'):
        Node(git_repo).init(name='task', template=crew, inherit=['steps'])
    with pytest.raises(ValueError, match='inherit=steps'):
        Node(git_repo).init(name='task', template=crew, inherit=['all'])
    assert not (git_repo / '.worktrees' / 'main.task').exists()
    # an uncommitted step beside the tracked ones never deploys -- the
    # read is the commit, not the working copy
    rogue = git_repo / 'templates' / 'crew' / 'steps' / '99-ROGUE.md'
    rogue.write_text('# never committed\n', encoding='utf-8')
    Node(git_repo).init(name='task', template=crew)
    seeded = node_dir('main.task') / 'steps'
    assert sorted(f.name for f in seeded.glob('*.md')) == [
        '00-SCOUT.md',
        '01-STRIKE.md',
    ]
    for step in seeded.glob('*.md'):
        committed = _git(git_repo, 'show', f'{sha}:templates/crew/steps/{step.name}')
        assert step.read_text(encoding='utf-8') == committed.stdout
    # a flagless sibling still seeds the stock set from the package
    Node(git_repo).init(name='stock')
    stock_steps = node_dir('main.stock') / 'steps'
    assert (stock_steps / '00-PREPARE.md').is_file()


def test_init_template_deploys_every_surface_from_the_commit(
    git_repo: pathlib.Path,
) -> None:
    """Every template surface deploys byte-for-byte from the fork commit.

    One committed folder seeds the charter, steps, skills, and per-agent
    files (which beat the package seed), records its provenance in
    ``_template.toml`` with the fork sha, and leaves the init event's
    metadata as that same fork sha. A surface the folder lacks (scripts
    here) falls back to the package seed, the skill copy is wholesale, and
    the agent dir still gets its skills link and credential symlink.
    """
    charter = (
        '# crew\n\n## Instructions\n\nTEMPLATE CHARTER\n\n'
        '## Completion Requirements\n\nDone.\n'
    )
    Node(git_repo).init(agent='claude', user=True)
    sha = _commit_template(
        git_repo,
        'templates/crew',
        {
            'config.json': '{}\n',
            'NODE.md': charter,
            'steps/01-PREPARE.md': '# prepare the ground\n',
            'steps/02-EXECUTE.md': '# execute the task\n',
            'skills/workflow/SKILL.md': '# workflow\n',
            'agents/claude/settings.json': '{"seeded": "from the template"}\n',
            'agents/claude/math.md': 'think in lemmas\n',
        },
    )
    # an uncommitted agent file beside the tracked ones never deploys --
    # the read is the commit, not the working copy
    rogue = git_repo / 'templates' / 'crew' / 'agents' / 'claude' / 'rogue.md'
    rogue.write_text('never committed\n', encoding='utf-8')
    output = Node(git_repo).init(name='task', template=f'{git_repo}/templates/crew')
    node_dir = git_repo / '.worktrees' / 'main.task' / '.fractal' / 'main.task'
    assert not (node_dir / '.claude' / 'rogue.md').exists()
    # every surface matches the committed bytes
    deployed = {
        'NODE.md': 'NODE.md',
        'steps/01-PREPARE.md': 'steps/01-PREPARE.md',
        'steps/02-EXECUTE.md': 'steps/02-EXECUTE.md',
        'skills/workflow/SKILL.md': 'skills/workflow/SKILL.md',
        'agents/claude/settings.json': '.claude/settings.json',
        'agents/claude/math.md': '.claude/math.md',
    }
    for source, target in deployed.items():
        committed = _git(git_repo, 'show', f'{sha}:templates/crew/{source}')
        assert (node_dir / target).read_text(encoding='utf-8') == committed.stdout
    # the skill copy is wholesale -- a package skill absent from the folder
    # is never unioned in
    skills = {d.name for d in (node_dir / 'skills').iterdir() if d.is_dir()}
    assert skills == {'workflow'}
    # a surface the folder lacks falls back to the package seed
    seed_dir = pathlib.Path(fractal.core.__file__).parent.parent / '_node'
    setup = node_dir / 'scripts' / 'setup.sh'
    assert setup.read_text(encoding='utf-8') == (
        seed_dir / 'scripts' / 'setup.sh'
    ).read_text(encoding='utf-8')
    # the skills link and the credential symlink still land beside the
    # bundle's agent files
    assert (node_dir / '.claude' / 'skills').is_symlink()
    assert (node_dir / '.codex' / 'auth.json').is_symlink()
    # the provenance records the repo-relative path and the fork commit
    data = tomllib.loads((node_dir / '_template.toml').read_text(encoding='utf-8'))
    assert data['path'] == 'templates/crew'
    assert data['commit'] == sha
    assert sha == _git(git_repo, 'rev-parse', 'main').stdout.strip()
    assert data['values'] == {}
    assert 'include' not in data
    assert 'exclude' not in data
    # the init event's metadata is still the fork sha, untouched by the read
    events = Node(git_repo).db.read(
        'events',
        where={'node': 'main.task', 'event': 'init'},
    )
    assert [event['metadata'] for event in events] == [sha]
    # the fork copy is the root's copy, so no notice prints
    assert 'Notice: template' not in output


def test_init_template_reads_the_fork_commit(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The template deploys at the child's fork commit; ``@<ref>`` re-points it.

    The read is versioned: a top-level spawn reads the root tip, so an
    edit-and-recommit reaches the next init; a deep spawn reads the
    parent's lagging tip and prints one notice naming the ``@<ref>``
    remedy; ``@<root>`` reads a commit the parent has never merged, with
    no notice; and a folder the root does not track draws none. On the
    deep spawn the bundled agent file beats the parent's live copy, a
    surface the bundle lacks still inherits the parent's, and the init
    event's metadata stays the fork sha.
    """

    def node_dir(branch: str) -> pathlib.Path:
        return git_repo / '.worktrees' / branch / '.fractal' / branch

    Node(git_repo).init(agent='claude', user=True)
    _commit_template(
        git_repo,
        'templates/crew',
        {
            'config.json': '{}\n',
            'steps/01-GO.md': '# go v1\n',
            'agents/claude/settings.json': '{"crew": "v1"}\n',
        },
    )
    crew = f'{git_repo}/templates/crew'
    # the parent forks here, then the root's copy moves on
    Node(git_repo).init(name='task')
    fork = _git(git_repo, 'rev-parse', 'main.task').stdout.strip()
    tuned = '# tuned by parent\n'
    (node_dir('main.task') / 'scripts' / 'test.sh').write_text(tuned, encoding='utf-8')
    (node_dir('main.task') / '.claude' / 'settings.json').write_text(
        '{"edited": "by the parent"}\n',
        encoding='utf-8',
    )
    newer = _commit_template(
        git_repo,
        'templates/crew',
        {
            'steps/01-GO.md': '# go v2\n',
            'agents/claude/settings.json': '{"crew": "v2"}\n',
        },
    )
    # an edit-and-recommit reaches the next top-level init, and the fork
    # copy matches the root's, so no notice prints
    output = Node(git_repo).init(name='fresh', template=crew)
    step = node_dir('main.fresh') / 'steps' / '01-GO.md'
    assert step.read_text(encoding='utf-8') == '# go v2\n'
    data = tomllib.loads(
        (node_dir('main.fresh') / '_template.toml').read_text(encoding='utf-8')
    )
    assert data['commit'] == newer
    assert 'Notice: template' not in output
    # an edited working copy never deploys either: tamper a tracked step
    # (a deep spawn needs no clean root) -- the fork commit's bytes land
    (git_repo / 'templates' / 'crew' / 'steps' / '01-GO.md').write_text(
        '# tampered working copy\n',
        encoding='utf-8',
    )
    # a deep spawn reads the parent's lagging tip and prints the notice
    monkeypatch.setenv('_NODE', str(node_dir('main.task')))
    output = Node(git_repo).init(name='sub', template=crew, inherit=['scripts'])
    assert (
        "Notice: template 'templates/crew' differs on the root branch;"
        " pass --template=templates/crew@main to read the root's copy."
    ) in output
    sub_dir = node_dir('main.task.sub')
    assert (sub_dir / 'steps' / '01-GO.md').read_text(encoding='utf-8') == '# go v1\n'
    data = tomllib.loads((sub_dir / '_template.toml').read_text(encoding='utf-8'))
    assert data['commit'] == fork
    # the init event's metadata is the fork sha, matching the recorded read
    events = Node(git_repo).db.read(
        'events',
        where={'node': 'main.task.sub', 'event': 'init'},
    )
    assert [event['metadata'] for event in events] == [fork]
    # the bundled agent file beats the parent's live copy; the scripts the
    # bundle lacks inherit the parent's
    settings = (sub_dir / '.claude' / 'settings.json').read_text(encoding='utf-8')
    assert settings == '{"crew": "v1"}\n'
    assert (sub_dir / 'scripts' / 'test.sh').read_text(encoding='utf-8') == tuned
    # @<root> reads the root's current copy -- a commit the parent has
    # never merged -- with no notice
    output = Node(git_repo).init(name='ref', template=f'{crew}@main')
    ref_dir = node_dir('main.task.ref')
    assert (ref_dir / 'steps' / '01-GO.md').read_text(encoding='utf-8') == '# go v2\n'
    data = tomllib.loads((ref_dir / '_template.toml').read_text(encoding='utf-8'))
    assert data['commit'] == newer
    assert 'Notice: template' not in output
    # a folder the root does not track draws no notice either
    worktree = git_repo / '.worktrees' / 'main.task'
    _commit_template(
        worktree,
        'templates/local',
        {'config.json': '{}\n', 'steps/01-OWN.md': '# the parent authored this\n'},
    )
    output = Node(git_repo).init(name='loc', template=f'{worktree}/templates/local')
    loc_dir = node_dir('main.task.loc')
    assert (loc_dir / 'steps' / '01-OWN.md').is_file()
    assert 'Notice: template' not in output


def test_init_template_reads_a_folder_deleted_from_disk(
    git_repo: pathlib.Path,
) -> None:
    """A retired template still seeds from the commit that tracks it.

    Template bytes deploy from git, never from the working copy, so a
    folder ``git rm``ed from the branch resolves through its nearest
    existing ancestor and deploys at an explicit ``@<sha>`` -- and the
    provenance records that commit for later verbs to read.
    """
    Node(git_repo).init(agent='claude', user=True)
    sha = _commit_template(
        git_repo,
        'templates/retired',
        {'config.json': '{}\n', 'steps/01-GO.md': '# go\n'},
    )
    _git(git_repo, 'rm', '-r', '-q', 'templates/retired')
    _git(git_repo, 'commit', '-m', 'retire the template')
    assert not (git_repo / 'templates' / 'retired').exists()
    Node(git_repo).init(
        name='task',
        template=f'{git_repo}/templates/retired@{sha}',
    )
    node_dir = git_repo / '.worktrees' / 'main.task' / '.fractal' / 'main.task'
    step = (node_dir / 'steps' / '01-GO.md').read_text(encoding='utf-8')
    assert step == '# go\n'
    data = tomllib.loads((node_dir / '_template.toml').read_text(encoding='utf-8'))
    assert data['path'] == 'templates/retired'
    assert data['commit'] == sha


def test_init_template_include_exclude_recorded_and_honored(
    git_repo: pathlib.Path,
) -> None:
    """``--include``/``--exclude`` trim the deploy and land in the provenance.

    The effective set is every template file minus ``exclude``, or only
    ``include`` (a directory entry covers its subtree): an excluded step
    never deploys, an include-only node keeps the package seed for every
    trimmed surface, and the listing is recorded in ``_template.toml`` for
    later verbs to judge by.
    """

    def node_dir(branch: str) -> pathlib.Path:
        return git_repo / '.worktrees' / branch / '.fractal' / branch

    charter = (
        '# crew\n\n## Instructions\n\nTEMPLATE CHARTER\n\n'
        '## Completion Requirements\n\nDone.\n'
    )
    Node(git_repo).init(agent='claude', user=True)
    _commit_template(
        git_repo,
        'templates/crew',
        {
            'config.json': '{}\n',
            'NODE.md': charter,
            'steps/01-PREPARE.md': '# prepare the ground\n',
            'steps/02-EXECUTE.md': '# execute the task\n',
            'scripts/test.sh': '# template test script\n',
        },
    )
    crew = f'{git_repo}/templates/crew'
    # exclude drops the one step; every other surface still deploys
    Node(git_repo).init(name='lean', template=crew, exclude=['steps/02-EXECUTE.md'])
    lean = node_dir('main.lean')
    assert sorted(f.name for f in (lean / 'steps').glob('*.md')) == ['01-PREPARE.md']
    assert 'TEMPLATE CHARTER' in (lean / 'NODE.md').read_text(encoding='utf-8')
    test_sh = (lean / 'scripts' / 'test.sh').read_text(encoding='utf-8')
    assert test_sh == '# template test script\n'
    data = tomllib.loads((lean / '_template.toml').read_text(encoding='utf-8'))
    assert data['exclude'] == ['steps/02-EXECUTE.md']
    assert 'include' not in data
    # include keeps only the listed subtree; the trimmed surfaces fall back
    # to the package seed
    Node(git_repo).init(name='steponly', template=crew, include=['steps'])
    steponly = node_dir('main.steponly')
    assert sorted(f.name for f in (steponly / 'steps').glob('*.md')) == [
        '01-PREPARE.md',
        '02-EXECUTE.md',
    ]
    node_md = (steponly / 'NODE.md').read_text(encoding='utf-8')
    assert 'TEMPLATE CHARTER' not in node_md
    seed_dir = pathlib.Path(fractal.core.__file__).parent.parent / '_node'
    test_sh = (steponly / 'scripts' / 'test.sh').read_text(encoding='utf-8')
    assert test_sh == (seed_dir / 'scripts' / 'test.sh').read_text(encoding='utf-8')
    data = tomllib.loads((steponly / '_template.toml').read_text(encoding='utf-8'))
    assert data['include'] == ['steps']
    assert 'exclude' not in data


def test_init_template_listing_trims_before_the_contract_checks(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contract checks judge the effective set, not the whole folder.

    The listing trims the bundle first, so an excluded offender cannot
    refuse the init: dropping the width-conflicting step makes a mixed
    ``steps/`` legal, and excluding ``steps/`` outright clears the rival
    source ``--inherit=steps`` refuses. The checks still judge what
    deploys: a ``steps/`` whose survivors carry no step files refuses
    rather than deploying an empty surface.
    """

    def node_dir(branch: str) -> pathlib.Path:
        return git_repo / '.worktrees' / branch / '.fractal' / branch

    Node(git_repo).init(agent='claude', user=True)
    _commit_template(
        git_repo,
        'templates/mixed',
        {
            'config.json': '{}\n',
            'steps/0-SCOUT.md': '# scout the terrain\n',
            'steps/01-STRIKE.md': '# strike the target\n',
            'steps/notes.txt': 'not a step\n',
        },
    )
    mixed = f'{git_repo}/templates/mixed'
    # excluding the width-conflicting step makes the mixed profile legal
    Node(git_repo).init(name='lean', template=mixed, exclude=['steps/0-SCOUT.md'])
    lean_steps = node_dir('main.lean') / 'steps'
    assert sorted(f.name for f in lean_steps.glob('*.md')) == ['01-STRIKE.md']
    # a steps/ whose survivors carry no step files still refuses
    with pytest.raises(ValueError, match='no step files'):
        Node(git_repo).init(
            name='task',
            template=mixed,
            exclude=['steps/0-SCOUT.md', 'steps/01-STRIKE.md'],
        )
    assert not (git_repo / '.worktrees' / 'main.task').exists()
    # excluding steps/ outright clears the rival source: the child
    # inherits the parent's live steps instead
    monkeypatch.setenv('_NODE', str(node_dir('main.lean')))
    Node(git_repo).init(
        name='sub',
        template=mixed,
        exclude=['steps'],
        inherit=['steps'],
    )
    sub_steps = node_dir('main.lean.sub') / 'steps'
    assert sorted(f.name for f in sub_steps.glob('*.md')) == ['01-STRIKE.md']


def test_init_template_preset_fills_unset_flags(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The config preset fills unset flags: flag > preset > inherited > default.

    A template's ``config.json`` presets run config for every spawn it
    seeds: an explicit flag still wins, the preset beats the value
    ``--inherit=config`` would copy from the parent, and a key nobody sets
    keeps the package default. The flag checks run on the merged values, so a
    flag ``--max-iter-cost`` is legal against a preset ``max_cost``, and
    the registry row records the preset cap the parent's budget law must
    see.
    """

    def node_dir(branch: str) -> pathlib.Path:
        return git_repo / '.worktrees' / branch / '.fractal' / branch

    Node(git_repo).init(agent='claude', user=True)
    preset = {'model': 'sonnet', 'max_cost': 10, 'sync': False}
    _commit_template(
        git_repo,
        'templates/crew',
        {
            'config.json': json.dumps(preset) + '\n',
            'steps/01-GO.md': '# go\n',
        },
    )
    crew = f'{git_repo}/templates/crew'
    # a parent whose preferences rival the preset's
    Node(git_repo).init(name='mgr', model='opus', step_timeout='10m')
    monkeypatch.setenv('_NODE', str(node_dir('main.mgr')))
    # a flag --max-iter-cost is accepted against the preset ceiling
    Node(git_repo).init(
        name='sub',
        template=crew,
        inherit=['config'],
        max_iter_cost=2.0,
    )
    sub = Node(git_repo / '.worktrees' / 'main.mgr.sub')
    # the preset beats the parent's inherited model; the parent still
    # fills the keys the preset lacks; nobody set detached, so it stays
    # at the package default (null)
    assert sub.config.get('model') == 'sonnet'
    assert sub.config.get('step_timeout') == '10m'
    assert sub.config.get('max_cost') == 10
    assert sub.config.get('max_iter_cost') == 2.0
    assert sub.config.get('sync') is False
    assert sub.config.get('detached') is None
    # the registry row carries the preset cap, not a blank
    rows = Node(git_repo).db.read('nodes', where={'node': 'main.mgr.sub'})
    assert [row['max_cost'] for row in rows] == [10]
    # an explicit flag beats the preset
    Node(git_repo).init(name='pinned', template=crew, model='haiku')
    pinned = Node(git_repo / '.worktrees' / 'main.mgr.pinned')
    assert pinned.config.get('model') == 'haiku'
    assert pinned.config.get('max_cost') == 10


def test_init_template_preset_reserve_resolves_against_the_merged_ceiling(
    git_repo: pathlib.Path,
) -> None:
    """``--reserve-budget`` resolves against the merged ``max_cost``.

    The reserve resolves after the preset merge, so a percent flag is a
    fraction of the preset ceiling, an absent flag takes the 10% default
    of that same ceiling (a null preset key reads as unset, exactly like
    config.json's own nulls), a preset ``reserve_budget`` (already USD)
    lands as written, and the flag still beats it.
    """

    def reserve_of(branch: str) -> float:
        node = Node(git_repo / '.worktrees' / branch)
        return node.config.get('reserve_budget')

    Node(git_repo).init(agent='claude', user=True)
    _commit_template(
        git_repo,
        'templates/capped',
        {'config.json': json.dumps({'max_cost': 8, 'reserve_budget': None}) + '\n'},
    )
    capped = f'{git_repo}/templates/capped'
    _commit_template(
        git_repo,
        'templates/funded',
        {'config.json': json.dumps({'max_cost': 8, 'reserve_budget': 1.5}) + '\n'},
    )
    funded = f'{git_repo}/templates/funded'
    # a percent flag resolves against the preset ceiling
    Node(git_repo).init(name='pct', template=capped, reserve_budget='25%')
    assert reserve_of('main.pct') == 2.0
    # no flag: the default 10% of the preset ceiling
    Node(git_repo).init(name='dflt', template=capped)
    assert reserve_of('main.dflt') == 0.8
    # a preset reserve is already USD and lands as written
    Node(git_repo).init(name='preset', template=funded)
    assert reserve_of('main.preset') == 1.5
    # the flag beats the preset reserve
    Node(git_repo).init(name='flag', template=funded, reserve_budget='25%')
    assert reserve_of('main.flag') == 2.0


def test_init_template_refusals_leave_nothing_behind(
    git_repo: pathlib.Path,
) -> None:
    """Every template refusal fires before any worktree or registry row exists.

    Each refusal names what broke: an untracked folder (with the
    uncommitted-copy hint and both remedies), an unknown ``@<ref>``, a
    machinery component, a file path, a folder without the ``config.json``
    marker, a symlink entry, ``--include`` beside ``--exclude``, a
    listing entry matching nothing, a listing naming the template
    machinery, the listing flags without ``--template``, and an
    ``agents/`` entry naming no registered agent.
    """
    Node(git_repo).init(agent='claude', user=True)
    _commit_template(
        git_repo,
        'templates/crew',
        {'config.json': '{}\n', 'steps/01-GO.md': '# go\n'},
    )
    crew = f'{git_repo}/templates/crew'
    # a folder git does not track at the fork commit, with a copy on disk
    fresh = git_repo / 'templates' / 'fresh'
    fresh.mkdir()
    (fresh / 'config.json').write_text('{}\n', encoding='utf-8')
    with pytest.raises(
        ValueError,
        match=r'not tracked at commit .*\(an uncommitted copy exists on disk\);'
        r' commit the folder on the branch the child forks from,'
        r' or pass @<ref>',
    ):
        Node(git_repo).init(name='task', template=f'{fresh}')
    # an @<ref> that resolves to no commit
    with pytest.raises(
        ValueError,
        match="ref does not resolve to a commit: 'nosuchref'",
    ):
        Node(git_repo).init(name='task', template=f'{crew}@nosuchref')
    # a fractal machinery component
    with pytest.raises(ValueError, match='fractal machinery component'):
        Node(git_repo).init(name='task', template=f'{git_repo}/.fractal/main')
    # a tracked file, not a folder
    with pytest.raises(ValueError, match='points at a file, not a folder'):
        Node(git_repo).init(name='task', template=f'{crew}/steps/01-GO.md')
    # a tracked folder without the config.json marker
    _commit_template(git_repo, 'templates/bare', {'steps/01-GO.md': '# go\n'})
    with pytest.raises(ValueError, match=r'not a template folder: no config\.json'):
        Node(git_repo).init(name='task', template=f'{git_repo}/templates/bare')
    # a folder carrying a symlink is not self-contained
    linky = git_repo / 'templates' / 'linky'
    linky.mkdir()
    (linky / 'config.json').write_text('{}\n', encoding='utf-8')
    (linky / 'NODE.md').symlink_to('config.json')
    _git(git_repo, 'add', 'templates/linky')
    _git(git_repo, 'commit', '-m', 'template templates/linky')
    with pytest.raises(
        ValueError,
        match=r"carries a symlink: 'templates/linky/NODE\.md'",
    ):
        Node(git_repo).init(name='task', template=f'{linky}')
    # the mutually exclusive listing flags, and an entry matching nothing
    with pytest.raises(
        ValueError,
        match='--include cannot be combined with --exclude',
    ):
        Node(git_repo).init(
            name='task',
            template=crew,
            include=['steps'],
            exclude=['steps/01-GO.md'],
        )
    with pytest.raises(
        ValueError,
        match=r"matches nothing in the template: 'steps/99-NOPE\.md'",
    ):
        Node(git_repo).init(name='task', template=crew, exclude=['steps/99-NOPE.md'])
    # the listing may not name the template machinery
    with pytest.raises(ValueError, match=r"--exclude may not name 'config\.json'"):
        Node(git_repo).init(name='task', template=crew, exclude=['config.json'])
    with pytest.raises(
        ValueError,
        match=r"--include may not name '_template\.toml'",
    ):
        Node(git_repo).init(name='task', template=crew, include=['_template.toml'])
    # the listing flags ride the template
    with pytest.raises(
        ValueError,
        match='--include and --exclude require --template',
    ):
        Node(git_repo).init(name='task', include=['steps'])
    # an agents/ entry naming no registered agent is a typo that would
    # deploy nothing -- refused naming the supported set
    _commit_template(
        git_repo,
        'templates/typo',
        {'config.json': '{}\n', 'agents/clauded/settings.json': '{}\n'},
    )
    with pytest.raises(
        ValueError,
        match=r"agents/ names an unknown agent: 'clauded' \(supported: claude,",
    ):
        Node(git_repo).init(name='task', template=f'{git_repo}/templates/typo')
    # nothing landed -- no worktree, no registry row
    assert not (git_repo / '.worktrees' / 'main.task').exists()
    assert not Node(git_repo).db.exists('nodes', where={'node': 'main.task'})


@pytest.mark.parametrize(
    argnames=('value', 'match'),
    argvalues=[
        ('/etc', 'outside this repository'),
        ('{repo}', 'names the worktree root'),
        ('{repo}/templates/../templates/crew', r'may not contain "\.\." steps'),
        ('{repo}/templates/demo@2x', 'folder name contains "@"'),
        ('{repo}/templates/demo@2x@main', 'folder name contains "@"'),
        ('{repo}/.worktrees/ghost', r'leads into \.worktrees/'),
    ],
    ids=[
        'outside_repo',
        'worktree_root',
        'traversal_step',
        'at_in_folder',
        'at_in_part',
        'worktrees_path',
    ],
)
def test_init_template_refuses_a_boundary_path(
    git_repo: pathlib.Path,
    value: str,
    match: str,
) -> None:
    """Every ``--template`` boundary path refuses before any worktree exists.

    A template is a tracked folder inside the repo: a path outside every
    worktree, the worktree root itself, a ``..`` step, a folder name
    carrying ``@`` (on disk, where the shape cannot be told from a ref
    suffix, and in any path component), and an entry under ``.worktrees/``
    (a checkout tracked at no commit) each refuse by name, and nothing
    lands.
    """
    Node(git_repo).init(agent='claude', user=True)
    # a real folder whose name carries '@' -- indistinguishable from a
    # ref suffix, so the shape refuses whether or not a ref follows
    (git_repo / 'templates' / 'demo@2x').mkdir(parents=True)
    with pytest.raises(ValueError, match=match):
        Node(git_repo).init(name='task', template=value.format(repo=git_repo))
    # nothing landed -- no worktree, no registry row
    assert not (git_repo / '.worktrees' / 'main.task').exists()
    assert not Node(git_repo).db.exists('nodes', where={'node': 'main.task'})


def test_init_template_refuses_agent_credentials(
    git_repo: pathlib.Path,
) -> None:
    """A credential under a template's ``agents/`` refuses at init by name.

    ``agents/`` deploys into the node's live agent dirs, so a committed
    OAuth store (``auth.json``), a dot-file (claude's
    ``.credentials.json``, an ``.env``), and a generic key shape
    (``*.pem``) each refuse before any worktree exists, naming the file
    -- nothing deploys, whatever commit carried the leak.
    """
    Node(git_repo).init(agent='claude', user=True)
    offending = [
        ('oauth', 'agents/codex/auth.json', 'credential-named file'),
        ('dotcred', 'agents/claude/.credentials.json', 'dot-file'),
        ('dotenv', 'agents/claude/.env', 'dot-file'),
        ('keyfile', 'agents/deploy/signing.pem', 'credential-named file'),
    ]
    for folder, name, kind in offending:
        path = f'templates/{folder}'
        _commit_template(
            git_repo,
            path,
            {'config.json': '{}\n', name: 'hands off\n'},
        )
        with pytest.raises(
            ValueError,
            match=rf"carries a {kind} under agents/: '{re.escape(f'{path}/{name}')}'",
        ):
            Node(git_repo).init(name='task', template=f'{git_repo}/{path}')
    # nothing landed -- no worktree, no registry row
    assert not (git_repo / '.worktrees' / 'main.task').exists()
    assert not Node(git_repo).db.exists('nodes', where={'node': 'main.task'})


def test_init_template_preset_refuses_disallowed_keys(
    git_repo: pathlib.Path,
) -> None:
    """A preset key outside the allowlist refuses at init by name.

    A preset may carry only budget, limit, duration, model, and mode keys:
    an identity or immutable key (``scope``, ``title``, ``root``) belongs
    to the spawn, and an unknown key is a typo that would otherwise
    silently preset nothing -- each refusal names the key, and nothing is
    created.
    """
    Node(git_repo).init(agent='claude', user=True)
    disallowed = [
        ({'scope': ['src']}, "may not set 'scope'"),
        ({'title': 'Crew'}, "may not set 'title'"),
        ({'root': 'main'}, "may not set 'root'"),
        ({'max_costt': 5}, "Unknown template preset key: 'max_costt'"),
    ]
    for preset, match in disallowed:
        _commit_template(
            git_repo,
            'templates/crew',
            {'config.json': json.dumps(preset) + '\n'},
        )
        with pytest.raises(ValueError, match=match):
            Node(git_repo).init(name='task', template=f'{git_repo}/templates/crew')
    assert not (git_repo / '.worktrees' / 'main.task').exists()
    assert not Node(git_repo).db.exists('nodes', where={'node': 'main.task'})


def test_init_template_preset_caps_refuse_like_flags(
    git_repo: pathlib.Path,
) -> None:
    """A degenerate preset cap refuses exactly as the matching flag does.

    The cap checks run on the merged values, so a preset ``max_iters`` of
    0 (unlimited in the loop, never never-run), a negative preset cap, a
    preset per-iteration cap with no run ceiling anywhere, and a preset
    reserve with no ceiling anywhere (a reserve without a ceiling is
    inert) all refuse before any worktree exists, with the flag checks'
    own wording.
    """
    Node(git_repo).init(agent='claude', user=True)
    degenerate = [
        ({'max_iters': 0}, 'max_iters must be greater than 0'),
        ({'max_depth': -1}, 'max_depth must be >= 0'),
        ({'max_iter_cost': 1.0}, 'max_iter_cost requires max_cost'),
        ({'reserve_budget': 1.5}, 'reserve_budget requires max_cost'),
    ]
    for preset, match in degenerate:
        _commit_template(
            git_repo,
            'templates/crew',
            {'config.json': json.dumps(preset) + '\n'},
        )
        with pytest.raises(ValueError, match=match):
            Node(git_repo).init(name='task', template=f'{git_repo}/templates/crew')
    assert not (git_repo / '.worktrees' / 'main.task').exists()
    assert not Node(git_repo).db.exists('nodes', where={'node': 'main.task'})


def test_init_template_resolves_the_same_path_from_any_cwd(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every calling directory records the same repo-relative template path.

    ``--template`` is a filesystem path resolved against the caller's
    directory: a sub-project node seeded from a repo-root folder names it
    absolutely from inside its project dir, a spawn from inside a node
    worktree names it through the worktree's own checkout, and both record
    the identical repo-relative POSIX form -- one folder names one
    template from every project in the repo.
    """
    # commit a sub-project wiki -- the base-ref precondition for child init
    app = git_repo / 'app'
    (app / 'wiki').mkdir(parents=True)
    (app / 'wiki' / '_index.md').write_text(
        '---\nname: app\n---\n# app\n\n***\n',
        encoding='utf-8',
    )
    _git(git_repo, 'add', 'app')
    _git(git_repo, 'commit', '-m', 'add app wiki')
    sha = _commit_template(
        git_repo,
        'templates/crew',
        {'config.json': '{}\n', 'steps/01-GO.md': '# go\n'},
    )
    Node(git_repo).init(path='app', agent='claude', user=True)
    # a sub-project node seeded from the repo-root folder, called from
    # inside the sub-project dir
    monkeypatch.chdir(app)
    Node(git_repo).init(name='seeded', template=f'{git_repo}/templates/crew')
    seeded = (
        git_repo / '.worktrees' / 'main.seeded' / 'app' / '.fractal' / 'main.seeded'
    )
    assert (seeded / 'steps' / '01-GO.md').is_file()
    data = tomllib.loads((seeded / '_template.toml').read_text(encoding='utf-8'))
    assert data['path'] == 'templates/crew'
    assert data['commit'] == sha
    # a spawn from inside a node worktree resolves the relative path
    # through the worktree's checkout to the same repo-relative form
    monkeypatch.chdir(git_repo / '.worktrees' / 'main.seeded')
    Node(git_repo).init(name='fromwt', template='templates/crew')
    fromwt = (
        git_repo / '.worktrees' / 'main.fromwt' / 'app' / '.fractal' / 'main.fromwt'
    )
    data = tomllib.loads((fromwt / '_template.toml').read_text(encoding='utf-8'))
    assert data['path'] == 'templates/crew'


def test_reset_without_template_drops_the_provenance(
    git_repo: pathlib.Path,
) -> None:
    """``--reset`` without ``--template`` forgets the template seed whole.

    The provenance file follows the forget-unless-repassed rule config
    flags have: a re-init that names no template reseeds the surfaces from
    the package and drops ``_template.toml``, so no later verb judges the
    node by a template it does not carry.
    """
    Node(git_repo).init(agent='claude', user=True)
    _commit_template(
        git_repo,
        'templates/crew',
        {'config.json': '{}\n', 'steps/01-STRIKE.md': '# strike the target\n'},
    )
    Node(git_repo).init(name='task', template=f'{git_repo}/templates/crew')
    node_dir = git_repo / '.worktrees' / 'main.task' / '.fractal' / 'main.task'
    assert (node_dir / '_template.toml').is_file()
    assert (node_dir / 'steps' / '01-STRIKE.md').is_file()
    Node(git_repo).init(name='task', reset=True)
    assert not (node_dir / '_template.toml').exists()
    assert (node_dir / 'steps' / '00-PREPARE.md').is_file()
    assert not (node_dir / 'steps' / '01-STRIKE.md').exists()


def test_child_inherits_skills_only_on_request(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--inherit=skills`` snapshots the parent's live skill set.

    A flagless child seeds skills from the package even when the parent's
    set is customized; ``--inherit=skills`` (and ``all``) copies the
    parent's set wholesale (a skill absent at the parent is never
    copied). The user node carries no skills, so a top-level
    ``--inherit=skills`` fails loudly instead of falling back.
    """

    def node_dir(branch: str) -> pathlib.Path:
        return git_repo / '.worktrees' / branch / '.fractal' / branch

    def skill_names(branch: str) -> set[str]:
        skills_dir = node_dir(branch) / 'skills'
        return {d.name for d in skills_dir.iterdir() if d.is_dir()}

    Node(git_repo).init(agent='claude', user=True)
    # the user node carries no skills -> a top-level inherit fails loudly
    with pytest.raises(RuntimeError, match='inherit=skills'):
        Node(git_repo).init(name='early', inherit=['skills'])
    # configure a parent: drop a standard skill and add a custom one
    Node(git_repo).init(name='task')
    parent_skills = node_dir('main.task') / 'skills'
    shutil.rmtree(parent_skills / 'memory')
    custom = parent_skills / 'custom' / 'SKILL.md'
    custom.parent.mkdir()
    custom.write_text('# custom\n', encoding='utf-8')
    # spawn as the parent node (_NODE): the child snapshots the parent's set
    monkeypatch.setenv('_NODE', str(node_dir('main.task')))
    Node(git_repo).init(name='sub', inherit=['skills'])
    assert 'custom' in skill_names('main.task.sub')
    assert 'memory' not in skill_names('main.task.sub')
    # 'all' includes the skills surface
    Node(git_repo).init(name='full', inherit=['all'])
    assert 'custom' in skill_names('main.task.full')
    # a flagless sibling seeds the standard set from the package
    Node(git_repo).init(name='stock')
    assert 'memory' in skill_names('main.task.stock')
    assert 'custom' not in skill_names('main.task.stock')


def test_child_inherits_config_preferences_not_caps(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--inherit=config`` copies preference keys, never budget caps.

    Preferences (model, effort, booleans, step timing) snapshot from the
    parent's config; explicit flags win over inherited values; budget-class
    keys stay unset. ``sleep``/``interval`` are rival pacing keys, so an
    explicit ``--interval`` also blocks inheriting the parent's ``sleep``.
    """

    def node_dir(branch: str) -> pathlib.Path:
        return git_repo / '.worktrees' / branch / '.fractal' / branch

    Node(git_repo).init(agent='claude', user=True)
    Node(git_repo).init(
        name='mgr',
        model='opus',
        effort='high',
        sleep='5s',
        step_timeout='10m',
        sync=False,
        detached=True,
        max_iters=7,
        max_children=3,
    )
    monkeypatch.setenv('_NODE', str(node_dir('main.mgr')))
    # preferences snapshot; caps stay unset
    Node(git_repo).init(name='sub', inherit=['config'])
    sub = Node(git_repo / '.worktrees' / 'main.mgr.sub')
    assert sub.config.get('model') == 'opus'
    assert sub.config.get('effort') == 'high'
    assert sub.config.get('sleep') == '5s'
    assert sub.config.get('step_timeout') == '10m'
    assert sub.config.get('sync') is False
    assert sub.config.get('detached') is True
    assert sub.config.get('max_iters') is None
    assert sub.config.get('max_children') is None
    # explicit flags win, and --interval blocks the rival sleep key
    Node(git_repo).init(
        name='pinned',
        inherit=['config'],
        model='sonnet',
        interval='1h',
    )
    pinned = Node(git_repo / '.worktrees' / 'main.mgr.pinned')
    assert pinned.config.get('model') == 'sonnet'
    assert pinned.config.get('interval') == '1h'
    assert pinned.config.get('sleep') is None
    assert pinned.config.get('step_timeout') == '10m'


def test_init_requires_resolvable_agent(git_repo: pathlib.Path) -> None:
    """Spawning without ``--agent`` and no ancestor default is refused."""
    Node(git_repo).init(user=True)  # user node carries no agent
    with pytest.raises(ValueError, match='No --agent'):
        Node(git_repo).init(name='task')


def test_init_refuses_unsupported_agent(git_repo: pathlib.Path) -> None:
    """A typo'd ``--agent`` refuses at init, naming the supported backends.

    A junk name would store fine and kill the loop at boot inside the tmux
    pane -- after ``start`` already printed its success -- stranding the node
    idle with the diagnosis lost when the pane closes. The user init (the
    README's first command, where every spawn inherits the default), explicit
    spawn names, and inherited defaults all refuse the same way, and the
    refusal leaves nothing behind.
    """
    # the front door: a typo'd tree-wide default refuses before storing
    with pytest.raises(ValueError, match='Unsupported agent'):
        Node(git_repo).init(agent='cluade', user=True)
    Node(git_repo).init(agent='claude', user=True)
    with pytest.raises(ValueError, match='Unsupported agent'):
        Node(git_repo).init(name='task', agent='notreal')
    # nothing landed -- no worktree, no registry row
    assert not (git_repo / '.worktrees' / 'main.task').exists()
    assert not Node(git_repo).db.exists('nodes', where={'node': 'main.task'})
    # a junk inherited default (the user config is a steering surface, so a
    # typo can land there directly) refuses at the spawn that inherits it
    Node(git_repo).config.set('agent', 'notreal')
    with pytest.raises(ValueError, match='Unsupported agent'):
        Node(git_repo).init(name='task')


def test_init_refuses_unsupported_provider(git_repo: pathlib.Path) -> None:
    """An explicit ``--provider`` the agent cannot route refuses at init.

    An unsupported route would store fine and the node would silently spend
    vendor-native -- refused like an agent typo, naming the supported set,
    with nothing left behind. Only the explicit flag refuses: an inherited
    default keeps its silent drop onto agents that cannot take it.
    """
    Node(git_repo).init(agent='claude', user=True)
    # a route-less agent (grok) declares no providers, so any route refuses
    with pytest.raises(ValueError, match="Unsupported provider: 'openrouter'"):
        Node(git_repo).init(name='task', agent='grok', provider='openrouter')
    # a routed agent still refuses a route it does not declare
    with pytest.raises(ValueError, match='supported: openrouter'):
        Node(git_repo).init(name='task', provider='bogus')
    # nothing landed -- no worktree, no registry row
    assert not (git_repo / '.worktrees' / 'main.task').exists()
    assert not Node(git_repo).db.exists('nodes', where={'node': 'main.task'})


def test_init_refuses_quoted_agent_command(git_repo: pathlib.Path) -> None:
    """Shell quoting in an ``--agent`` command refuses at the accept gates.

    Agent commands split on whitespace with no shell interpretation, so a
    quoted argument would mangle into garbage argv words at loop boot,
    deep inside the tmux pane -- the same vanishing-pane failure class as
    a typo'd name, refused at the same gates with nothing stored.
    """
    quoted = 'claude --append-system-prompt "be terse"'
    with pytest.raises(ValueError, match='commands split on whitespace'):
        Node(git_repo).init(agent=quoted, user=True)
    Node(git_repo).init(agent='claude', user=True)
    with pytest.raises(ValueError, match='commands split on whitespace'):
        Node(git_repo).init(name='task', agent=quoted)
    # nothing landed -- no worktree, no registry row
    assert not (git_repo / '.worktrees' / 'main.task').exists()
    assert not Node(git_repo).db.exists('nodes', where={'node': 'main.task'})


# ------ preconditions


def test_init_requires_project_wiki(tmp_path: pathlib.Path) -> None:
    """Init errors if the base branch has no project wiki."""
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(
        ['git', 'init', '-b', 'main'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'config', 'user.email', 'test@test.com'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'config', 'user.name', 'Test'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    (repo / 'README.md').write_text(
        '# test\n',
        encoding='utf-8',
    )
    (repo / '.gitignore').write_text(
        '.venv\n.worktrees/\n.db\n.db-*\n.status\n',
        encoding='utf-8',
    )
    subprocess.run(
        ['git', 'add', '.'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'commit', '-m', 'init'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    node = Node(repo)
    node.init(agent='claude', user=True)
    with pytest.raises(RuntimeError, match='project wiki'):
        node.init(name='bad')


def test_init_refuses_a_base_without_a_worktree(git_repo: pathlib.Path) -> None:
    """A ``--base`` with no checked-out worktree refuses at init, naming both.

    The base is also the squash-merge target: ``merge.sh`` squashes inside
    the base's worktree, so a worktree-less base (a typo, or a branch
    nothing has checked out) would only fail at merge time, long after init
    printed its success. The refusal names the branch and the worktree
    requirement -- never a missing wiki (the wiki precondition reads through
    the base ref, so it would misdiagnose a typo) -- and leaves nothing
    behind; a worktree-backed base (another node's branch) still passes.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    # a typo'd base and a real-but-unattended branch refuse the same way
    _git(git_repo, 'branch', 'unattended')
    for base in ('does_not_exist', 'unattended'):
        with pytest.raises(ValueError, match='no checked-out worktree') as err:
            node.init(name='task', base=base)
        assert base in str(err.value)
        assert 'wiki' not in str(err.value)
    # nothing landed -- no worktree, no registry row
    assert not (git_repo / '.worktrees' / 'main.task').exists()
    assert not node.db.exists('nodes', where={'node': 'main.task'})
    # a base with a checked-out worktree still passes
    node.init(name='anchor')
    node.init(name='task', base='main.anchor')
    assert (git_repo / '.worktrees' / 'main.task').exists()


def test_init_rejects_inside_worktrees(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Init rejects creating a node inside the ``.worktrees`` directory."""
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    worktree_path = repo / '.worktrees' / 'task'
    worktree_path.mkdir(parents=True)
    # simulate a running node so the parent resolves and init.sh runs
    node_dir = repo / '.fractal' / 'main'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    node = Node(worktree_path)
    with pytest.raises(RuntimeError, match=r'inside .*\.worktrees'):
        node.init(name='bad')


def test_user_init_repairs_stranded_database(git_repo: pathlib.Path) -> None:
    """Re-running user init reseeds a DB/radio stranded by a partial prior init.

    ``config.json`` marks the node a user before ``db.init``/``radio.init`` run,
    so a crash between them leaves a valid-looking config over an unseeded tree
    -- the idempotent re-entry path must repair the DB, not only the wiki. A
    re-run must reseed the schema and default channels (both idempotent),
    so the whole tree is recoverable without manual deletion.
    """
    # a complete user node, then simulate the strand: drop the seeded database
    Node(git_repo).init(user=True)
    branch = _resolve_branch(git_repo)
    db_path = git_repo / '.fractal' / branch / '.db'
    assert db_path.exists()
    db_path.unlink()

    # re-running init hits the idempotent (is_user) branch and repairs the DB
    message = Node(git_repo).init(user=True)
    assert 'already initialized' in message
    node = Node(git_repo)
    tables = node.db.read(
        query="SELECT name FROM sqlite_master WHERE type='table'",
    )
    assert 'channels' in {row['name'] for row in tables}
    # radio is reseeded too: the default channels are back
    channels = {channel['channel'] for channel in node.radio.channels()}
    assert channels == {'public', 'private', 'inbox', 'outbox'}


def test_user_init_rejects_second_project_on_same_branch(
    git_repo: pathlib.Path,
) -> None:
    """One git branch maps to a single project."""
    (git_repo / 'app').mkdir()
    Node(git_repo).init(path='app', agent='claude', user=True)
    # a different project on the same branch is rejected with a clear error
    with pytest.raises(ValueError, match='one branch maps to a single project'):
        Node(git_repo).init(path='lib', user=True)
    # re-initializing the same project is idempotent
    message = Node(git_repo).init(path='app', user=True)
    assert 'already initialized' in message


@pytest.mark.parametrize(
    argnames='kwargs',
    argvalues=[
        {'timeout': '0s'},
        {'iter_timeout': '0.5s'},
        {'sleep': '0.01m'},
    ],
)
def test_init_rejects_subsecond_duration(
    git_repo: pathlib.Path,
    kwargs: dict[str, Any],
) -> None:
    """Init rejects a sub-1s duration up front instead of aborting at launch.

    The loop's duration parse rejects anything under 1 second -- so a stored
    ``0s``/``0.5s``/``0.01m`` would otherwise fail only when started (or, on
    a retunable key, crash a running loop's re-read). The merged config
    validator rejects it at the init boundary with a clear message.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    with pytest.raises(ValueError, match='at least 1 second'):
        node.init(name='task', agent='claude', **kwargs)


def test_init_accepts_fractional_duration_under_comma_locale(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A comma-decimal locale never rejects a valid fractional duration.

    init.sh re-checks the sub-second gate with awk, whose string-to-number
    conversion follows the process locale on macOS -- under a comma-decimal
    locale a dot-decimal ``0.5h`` (30 minutes) would truncate to ``0`` and
    be rejected as sub-second. The gate pins awk to the C locale, so the
    caller's locale never changes what parses.
    """
    monkeypatch.setenv('LC_ALL', 'de_DE.UTF-8')
    Node(git_repo).init(agent='claude', user=True)
    Node(git_repo).init(name='task', agent='claude', iter_timeout='0.5h')
    task = Node(git_repo / '.worktrees' / 'main.task')
    assert task.config.get('iter_timeout') == '0.5h'


@pytest.mark.parametrize(
    argnames=('kwargs', 'match'),
    argvalues=[
        ({'interval': '30m', 'sleep': '10s'}, 'mutually exclusive'),
        ({'interval': '30m', 'iter_timeout': '2h'}, 'exceeds'),
    ],
    ids=['interval_sleep', 'iter_timeout_over_interval'],
)
def test_init_rejects_incoherent_pacing(
    git_repo: pathlib.Path,
    kwargs: dict[str, Any],
    match: str,
) -> None:
    """Init refuses incoherent pacing up front, never launching into a wedge.

    The interval/sleep mutex and the iter-timeout-within-interval cap are
    launch invariants the loop depends on, so the launch validator rejects
    a violating combination at the init boundary with a clear message --
    not at loop construction, where the failure would strand the node
    ``idle`` inside the tmux pane with no run row.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    with pytest.raises(ValueError, match=match):
        node.init(name='task', agent='claude', **kwargs)


@pytest.mark.parametrize('root', ['/etc', '../../outside', 'a/../../b'])
def test_init_rejects_absolute_or_traversal_scope(
    git_repo: pathlib.Path,
    root: str,
) -> None:
    """Init refuses an absolute or ``..`` scope root up front.

    A scope root is a repo-relative subdirectory; an absolute or traversal
    path would persist into config.json (against the no-absolute-paths
    rule) and then never match commit.py's repo-relative prefix check --
    bricking every scoped commit from iteration one, far from the cause.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    with pytest.raises(ValueError, match='repo-relative subdirectory'):
        node.init(name='scoped', scope=[root])


def test_init_normalizes_scope_before_validating(git_repo: pathlib.Path) -> None:
    """Core init splits scope on the canonical separators, then validates.

    Whitespace is the stored form's list separator (``config _set`` splits
    on it), so a space-carrying entry is several roots, not one -- and
    validating the pre-split string would let a traversal root ride into
    config.json inside a space form, where the canonical split later frees
    it. Init must land the same list ``config _set`` would, with every
    split root individually validated.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    # the mixed comma+space form lands as the canonical list
    node.init(name='scoped', scope=['roots/a,roots/b roots/c'])
    child = Node(git_repo / '.worktrees' / 'main.scoped')
    assert child.config.get('scope') == ['roots/a', 'roots/b', 'roots/c']
    # a traversal root hiding inside a space form cannot bypass validation
    with pytest.raises(ValueError, match='repo-relative subdirectory'):
        node.init(name='sneaky', scope=['roots/a ../escape'])


# ------ sub-projects and ambient resolution


def test_child_inherits_subproject_from_parent(git_repo: pathlib.Path) -> None:
    """A child inherits its parent's project across the whole subtree."""
    app = git_repo / 'app'
    app.mkdir()
    # commit a sub-project wiki -- the base-ref precondition for child init
    app_wiki = app / 'wiki'
    app_wiki.mkdir()
    (app_wiki / '_index.md').write_text(
        '---\nname: app\n---\n# app\n\n***\n',
        encoding='utf-8',
    )
    subprocess.run(
        ['git', 'add', 'app'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'commit', '-m', 'add app wiki'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )
    # user node for the sub-project, then a child under it
    Node(git_repo).init(path='app', agent='claude', user=True)
    Node(git_repo).init(name='task')
    # the child inherits project 'app' and nests its data under app/, once
    child_wt = git_repo / '.worktrees' / 'main.task'
    child = Node(child_wt)
    assert child.config.get('project') == 'app'
    assert (child_wt / 'app' / '.fractal' / 'main.task').is_dir()
    assert not (child_wt / '.fractal' / 'main.task').exists()


def test_init_ignores_cross_repo_ambient_node(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``_NODE`` pointing at another repo is not adopted as parent.

    The ambient caller is adopted only when it lives in the *target* repo;
    otherwise the child would register in the wrong repo's DB (split-brain). A
    foreign ``_NODE`` falls back to the target repo's own user node.
    """
    repo_a = _make_git_repo(tmp_path / 'a')
    Node(repo_a).init(agent='claude', user=True)
    repo_b = _make_git_repo(tmp_path / 'b')
    Node(repo_b).init(agent='claude', user=True)

    # _NODE points into repo A, but we init in repo B
    monkeypatch.setenv('_NODE', f'{repo_a}')
    Node(repo_b).init(name='child')
    monkeypatch.delenv('_NODE')

    # the child registered under repo B's user node, and repo A is untouched
    assert (repo_b / '.worktrees' / 'main.child').is_dir()
    assert 'main.child' in {row['node'] for row in Node(repo_b).child_list()}
    assert 'main.child' not in {row['node'] for row in Node(repo_a).child_list()}


def test_init_node_default_path_ignores_cross_repo_ambient(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``init_node('.')`` does not redirect to a foreign ``_NODE`` repo.

    Redirecting to the ``_NODE`` repo *before* ``init``'s same-repo guard runs
    would let a stale ``_NODE`` pointing at another repo register the node
    there (split-brain). ``init_node`` must honor ``_NODE`` only when it lives
    in the cwd's repo -- the gap a Node-API-only test cannot catch.
    """
    repo_a = _make_git_repo(tmp_path / 'a')
    Node(repo_a).init(agent='claude', user=True)
    repo_b = _make_git_repo(tmp_path / 'b')
    Node(repo_b).init(agent='claude', user=True)
    # _NODE points into repo A, but the cwd (and default path) is repo B
    monkeypatch.setenv('_NODE', f'{repo_a}')
    monkeypatch.chdir(repo_b)
    resolved = init_node('.')
    monkeypatch.delenv('_NODE')
    # resolved to repo B's root, not redirected into repo A
    assert resolved.repo_dir == repo_b


def test_resolve_node_targets_subproject_user_node(git_repo: pathlib.Path) -> None:
    """``resolve_node`` targets the sub-project user node, not a lone child.

    A sub-project user node nests at ``<project>/.fractal/<branch>``; resolve_node
    must apply the project prefix from the ``.project`` cache, or it falls through
    to the single child worktree -- silently mis-targeting ``node list``/``status``
    /``cost`` and the ``commit --init`` baseline.
    """
    # commit a sub-project wiki -- the base-ref precondition for child init
    app = git_repo / 'app'
    app.mkdir()
    (app / 'wiki').mkdir()
    (app / 'wiki' / '_index.md').write_text(
        '---\nname: app\n---\n# app\n\n***\n',
        encoding='utf-8',
    )
    subprocess.run(['git', 'add', 'app'], cwd=git_repo, capture_output=True, check=True)
    subprocess.run(
        ['git', 'commit', '-m', 'add app wiki'],
        cwd=git_repo,
        capture_output=True,
        check=True,
    )
    # a sub-project user node, then one child (the single-child mis-target trigger)
    Node(git_repo).init(path='app', agent='claude', user=True)
    Node(git_repo).init(name='w')
    # resolve_node from the repo root targets the user node, not the child
    resolved = resolve_node(f'{git_repo}')
    assert resolved.is_user
    assert resolved.config.get('project') == 'app'


def test_resolve_init_target_anchors_subproject_at_git_root(
    git_repo: pathlib.Path,
) -> None:
    """A sub-project init target anchors at the git root (no doubled prefix).

    ``node init --path=<subproject>`` must anchor at the git root -- ``node_dir``
    derives the ``<project>/`` prefix from the ``.project`` cache, so anchoring at
    the sub-project folder would double it, breaking the documented monorepo
    ``node init`` with a ``FileNotFoundError``.
    """
    (git_repo / 'app').mkdir()
    app_dir = git_repo / 'app'
    node, project = resolve_init_target(f'{app_dir}')
    assert node.worktree == git_repo
    assert str(project) == 'app'


def test_resolve_init_target_refuses_linked_worktree(
    tmp_path: pathlib.Path,
) -> None:
    """Init from a linked git worktree fails with the main-checkout remedy.

    A linked worktree (``git worktree add ../feature``) lives outside the main
    repo root, so it can never be a sub-project path -- the resolver must
    refuse with the remedy (run init from the main checkout), not leak
    ``relative_to``'s raw ValueError.
    """
    repo = _make_git_repo(tmp_path / 'main')
    subprocess.run(
        ['git', 'worktree', 'add', '-b', 'feature', '../feature'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    with pytest.raises(typer.BadParameter, match='main checkout'):
        resolve_init_target(f'{tmp_path / "feature"}')


def test_pin_without_a_template_still_validates(
    git_repo: pathlib.Path,
) -> None:
    """``--pin`` alone runs the validation gate, not just alongside a template.

    A commission can pin a revision without carrying a template charter,
    and a gate that only ran under ``--template`` would wave those through
    -- a stale pin then dies at the node's first seat instead of at init.
    A resolvable pin initializes normally.
    """
    Node(git_repo).init(agent='claude', user=True)
    head = subprocess.run(
        ['git', 'rev-parse', 'HEAD'],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # a pin that resolves to no commit refuses before any worktree exists
    with pytest.raises(ValueError, match='--pin does not resolve'):
        Node(git_repo).init(name='v1', pin='0' * 40)
    assert not (git_repo / '.worktrees' / 'main.v1').exists()
    # a real pin passes the gate and the node initializes
    Node(git_repo).init(name='v1', pin=head)
    assert (git_repo / '.worktrees' / 'main.v1' / '.fractal' / 'main.v1').is_dir()


def test_init_template_seeds_and_validates_the_fill_sheet(
    git_repo: pathlib.Path,
) -> None:
    """A template charter deploys ready-made; stale seeds die at init.

    The charter is read at the fork commit and its fill-sheet gate runs
    pre-worktree: a truncated charter (a lost tail section), a ``pin:``
    that resolves to no commit or disagrees with ``--pin``, and a
    ``docket:`` row absent at the pin each refuse -- the stale-seed class
    dies at init instead of at the commission's first seat. A coherent
    seed deploys verbatim from the commit.
    """
    Node(git_repo).init(agent='claude', user=True)
    head = _git(git_repo, 'rev-parse', 'HEAD').stdout.strip()
    _commit_template(
        git_repo,
        'templates/steward',
        {'config.json': '{}\n', 'steps/00-VERIFY.md': '# verify the docket\n'},
    )
    steward = f'{git_repo}/templates/steward'

    def _commit_charter(pin_line: str, docket: str) -> str:
        charter = (
            f'## Instructions\n\nVerify the docket.\n{pin_line}\n'
            f'docket: {docket}\n\n## Completion Requirements\n\nVerdict filed.\n'
        )
        return _commit_template(git_repo, 'templates/steward', {'NODE.md': charter})

    # a stale pin (no such commit) refuses
    _commit_charter('pin: deadbeefdeadbeefdeadbeefdeadbeefdeadbeef', 'README.md')
    with pytest.raises(ValueError, match='pin does not resolve'):
        Node(git_repo).init(name='v1', template=steward)
    # a pin disagreeing with the commission's --pin refuses
    newer = _commit_charter(f'pin: {head}', 'README.md')
    with pytest.raises(ValueError, match='does not match --pin'):
        Node(git_repo).init(name='v1', template=steward, pin=newer)
    # a docket row absent at the pin refuses
    _commit_charter(f'pin: {head}', 'no/such/surface.md')
    with pytest.raises(ValueError, match='Docket row does not resolve'):
        Node(git_repo).init(name='v1', template=steward)
    # a truncated charter (lost tail section) refuses
    _commit_template(
        git_repo,
        'templates/steward',
        {'NODE.md': '## Instructions\n\nVerify.\n'},
    )
    with pytest.raises(ValueError, match='Completion Requirements'):
        Node(git_repo).init(name='v1', template=steward)
    # a pin spelling git itself accepts is validated, never skipped:
    # uppercase hex resolves (or refuses) like lowercase ...
    _commit_charter('pin: DEADBEEFDEADBEEFDEADBEEFDEADBEEFDEADBEEF', 'README.md')
    with pytest.raises(ValueError, match='pin does not resolve'):
        Node(git_repo).init(name='v1', template=steward)
    # ... a short abbreviation still gates against --pin ...
    _commit_charter(f'pin: {head[:5]}', 'README.md')
    with pytest.raises(ValueError, match='does not match --pin'):
        Node(git_repo).init(name='v1', template=steward, pin=newer)
    # ... and a spelling the gate cannot read as a sha refuses outright
    _commit_charter('pin: nosuchbranch', 'README.md')
    with pytest.raises(ValueError, match='not a commit sha'):
        Node(git_repo).init(name='v1', template=steward)
    assert not (git_repo / '.worktrees' / 'main.v1').exists()
    # a coherent seed deploys: charter verbatim from the commit (case-blind
    # pin match), template steps in place, and the next-steps banner says
    # the seeded task is ready to start rather than asking to author blank
    # sections
    sha = _commit_charter(f'pin: {head.upper()}', 'README.md')
    output = Node(git_repo).init(name='v1', template=steward, pin=head)
    node_dir = git_repo / '.worktrees' / 'main.v1' / '.fractal' / 'main.v1'
    committed = _git(git_repo, 'show', f'{sha}:templates/steward/NODE.md')
    assert (node_dir / 'NODE.md').read_text(encoding='utf-8') == committed.stdout
    assert [f.name for f in sorted((node_dir / 'steps').glob('*.md'))] == [
        '00-VERIFY.md'
    ]
    assert 'start the loop' in output
    assert 'sections start blank' not in output
    # a charterless init keeps the author-your-task banner
    output = Node(git_repo).init(name='v2')
    assert 'author the node' in output
    assert 'sections start blank' in output


def test_init_template_fills_slots_from_values_set_and_pin(
    git_repo: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """The slot pass renders the seed once, from merged values (later win).

    ``--set`` beats the ``--values`` sheet, ``--pin`` supplies ``pin``
    last -- beating a rival ``--set pin=`` -- and the charter gate reads
    the rendered text: a stale pin fill dies at init, a coherent one
    deploys with every ``{{slot}}`` filled and ``$VAR``/shell text
    intact, and the merged map -- an unused sheet key included (one
    sheet may cover several templates) -- is recorded in
    ``_template.toml``.
    """
    Node(git_repo).init(agent='claude', user=True)
    charter = (
        '## Instructions\n\npin: {{pin}}\nmission: {{ mission }}\n\n'
        '## Completion Requirements\n\nDone.\n'
    )
    _commit_template(
        git_repo,
        'templates/steward',
        {
            'config.json': '{}\n',
            'NODE.md': charter,
            'steps/01-GO.md': '# {{mission}} on $WORKTREE_DIR (${CURRENT_BRANCH%.*})\n',
        },
    )
    steward = f'{git_repo}/templates/steward'
    head = _git(git_repo, 'rev-parse', 'HEAD').stdout.strip()
    # the gate reads the rendered charter, so a stale pin fill refuses
    stale = 'deadbeef' * 5
    with pytest.raises(ValueError, match='pin does not resolve'):
        Node(git_repo).init(
            name='v1',
            template=steward,
            sets=[f'pin={stale}', 'mission=x'],
        )
    assert not (git_repo / '.worktrees' / 'main.v1').exists()
    # a coherent fill deploys: the sheet fills, --set overrides it, and
    # --pin supplies pin -- beating a rival --set pin= (a resolvable but
    # wrong revision must reach neither the charter nor the record)
    rival = _git(git_repo, 'rev-parse', 'HEAD~1').stdout.strip()
    sheet = tmp_path / 'fills.toml'
    sheet.write_text(
        'mission = "from the sheet"\nextra = "unused"\n',
        encoding='utf-8',
    )
    Node(git_repo).init(
        name='v1',
        template=steward,
        values=sheet,
        sets=['mission=prove it', f'pin={rival}'],
        pin=head,
    )
    node_dir = git_repo / '.worktrees' / 'main.v1' / '.fractal' / 'main.v1'
    node_md = (node_dir / 'NODE.md').read_text(encoding='utf-8')
    assert f'pin: {head}\n' in node_md
    assert rival not in node_md
    assert 'mission: prove it\n' in node_md
    step = (node_dir / 'steps' / '01-GO.md').read_text(encoding='utf-8')
    assert step == '# prove it on $WORKTREE_DIR (${CURRENT_BRANCH%.*})\n'
    data = tomllib.loads((node_dir / '_template.toml').read_text(encoding='utf-8'))
    assert data['values'] == {
        'mission': 'prove it',
        'extra': 'unused',
        'pin': head,
    }


def test_init_template_slot_refusals_fire_before_any_worktree(
    git_repo: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """Every slot-pass refusal names what broke, before any worktree exists.

    An unfilled slot and a stray ``{{`` (an uppercase name included) name
    the file and the token, a file that does not decode refuses by name
    (templates are text), a shapeless ``--set`` pair, a key breaking the
    slot grammar, and a malformed ``--values`` sheet refuse outright, and
    the fill flags require ``--template``.
    """
    Node(git_repo).init(agent='claude', user=True)
    _commit_template(
        git_repo,
        'templates/crew',
        {'config.json': '{}\n', 'steps/01-GO.md': '# go for {{mission}}\n'},
    )
    crew = f'{git_repo}/templates/crew'
    # an unfilled slot names the file and the token
    with pytest.raises(
        ValueError,
        match=r'templates/crew/steps/01-GO\.md has no value for slot'
        r' \{\{mission\}\}',
    ):
        Node(git_repo).init(name='task', template=crew)
    # an uppercase {{PIN}} is not a slot; the refusal names the residue
    _commit_template(
        git_repo,
        'templates/crew',
        {'steps/01-GO.md': '# go for {{PIN}}\n'},
    )
    with pytest.raises(
        ValueError,
        match=r"steps/01-GO\.md carries '\{\{PIN\}\}', which is not a slot",
    ):
        Node(git_repo).init(name='task', template=crew)
    # a file that does not decode refuses by name -- templates are text
    step = git_repo / 'templates' / 'crew' / 'steps' / '01-GO.md'
    step.write_bytes(b'\x80\x81 not utf-8\n')
    _git(git_repo, 'add', 'templates/crew')
    _git(git_repo, 'commit', '-m', 'binary step')
    with pytest.raises(ValueError, match=r'steps/01-GO\.md is not UTF-8 text'):
        Node(git_repo).init(name='task', template=crew)
    # the value sources validate before any read: a pair without '=', a
    # key outside the slot grammar (either source), a non-string sheet
    # value, a sheet that does not parse, and a sheet that does not exist
    with pytest.raises(ValueError, match=r"Invalid --set value: 'mission'"):
        Node(git_repo).init(name='task', template=crew, sets=['mission'])
    with pytest.raises(ValueError, match=r"--set key is not a slot name: 'MISSION'"):
        Node(git_repo).init(name='task', template=crew, sets=['MISSION=x'])
    sheet = tmp_path / 'fills.toml'
    sheet.write_text('Mission = "x"\n', encoding='utf-8')
    with pytest.raises(
        ValueError,
        match=r"--values key is not a slot name: 'Mission'",
    ):
        Node(git_repo).init(name='task', template=crew, values=sheet)
    sheet.write_text('count = 3\n', encoding='utf-8')
    with pytest.raises(
        ValueError,
        match=r"--values key 'count' does not hold a string",
    ):
        Node(git_repo).init(name='task', template=crew, values=sheet)
    sheet.write_text('= broken\n', encoding='utf-8')
    with pytest.raises(ValueError, match='not valid TOML'):
        Node(git_repo).init(name='task', template=crew, values=sheet)
    with pytest.raises(ValueError, match='--values file does not exist'):
        Node(git_repo).init(name='task', template=crew, values=tmp_path / 'no.toml')
    # the fill flags ride the template
    with pytest.raises(ValueError, match='--values and --set require --template'):
        Node(git_repo).init(name='task', sets=['mission=x'])
    # nothing landed -- no worktree, no registry row
    assert not (git_repo / '.worktrees' / 'main.task').exists()
    assert not Node(git_repo).db.exists('nodes', where={'node': 'main.task'})


def test_init_template_docket_anchor_defaults_to_the_fork_commit(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pinless charter's docket anchors at the fork commit, not ``HEAD``.

    A deep spawn forks from its parent's tip, so a docket surface
    committed there -- and absent from the main checkout's ``HEAD`` --
    resolves without ``--pin``; a row absent at the fork still refuses,
    naming the fork ref.
    """
    Node(git_repo).init(agent='claude', user=True)
    Node(git_repo).init(name='task')
    worktree = git_repo / '.worktrees' / 'main.task'
    # the docket surface and the template exist on the parent's tip only
    _commit_template(worktree, 'audited', {'target.md': '# audit me\n'})
    charter = (
        '## Instructions\n\nAudit the docket.\ndocket: audited/target.md\n\n'
        '## Completion Requirements\n\nVerdict filed.\n'
    )
    _commit_template(
        worktree,
        'templates/steward',
        {'config.json': '{}\n', 'NODE.md': charter},
    )
    listed = _git(git_repo, 'ls-tree', '-r', '--name-only', 'HEAD').stdout
    assert 'audited/target.md' not in listed
    # the pinless docket resolves at the fork (the parent's tip)
    monkeypatch.setenv('_NODE', str(worktree / '.fractal' / 'main.task'))
    Node(git_repo).init(name='steward', template=f'{worktree}/templates/steward')
    assert (git_repo / '.worktrees' / 'main.task.steward').is_dir()
    # a row absent at the fork still refuses, naming the fork ref
    stale = charter.replace('audited/target.md', 'no/such/surface.md')
    _commit_template(worktree, 'templates/steward', {'NODE.md': stale})
    with pytest.raises(
        ValueError,
        match=r"Docket row does not resolve at main\.task: 'no/such/surface\.md'",
    ):
        Node(git_repo).init(name='v2', template=f'{worktree}/templates/steward')
