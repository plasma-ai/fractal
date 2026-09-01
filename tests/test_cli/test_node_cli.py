"""End-to-end tests for the ``fractal node`` lifecycle CLI.

Drives the real ``fractal`` console script as a subprocess against a
throwaway git repo built by the CLI itself -- a user (root) node plus two
worker nodes. The tests exercise the node lifecycle surface end to end:
``init`` and its run-config flags, ``list`` and its filters, ``status``,
``diff`` against a node's recorded template,
the ``finish``/``stop``/``kill``/``retire``/``unretire``/``attach`` status
guards (including their ``RuntimeError`` messages and exit codes), ``update``
of a child's configuration, ``merge``'s ``--ignore-scope`` override reaching
the script, ``delete``'s unmerged-work warning ahead of its prompt (and behind
its refusals, which describe no deletion), orphan coherence after out-of-band
git cleanup (stored-status display plus ``reconcile``'s event-log audit), and
the hidden ``_scope`` footprint check ``merge.sh`` shells out to.

Behavior that is observable through the CLI is asserted directly, including
machine-output guarantees (piped status is unbracketed for clean parsing).
"""

from __future__ import annotations

import csv
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import tomllib

import pytest
import tomli_w

import fractal
from fractal.constants import STATUSES
from fractal.core.node import Node
from tests._helpers import _git

from .conftest import _await_settled, _cli_env, _fractal_bin, _require_tmux, _run

__all__ = [
    'test_init_persists_run_config',
    'test_init_ignores_ambient_provider_and_effort',
    'test_scope_flags_flatten_to_recorded_roots',
    'test_space_joined_scope_normalizes_on_read',
    'test_init_reserve_budget_defaults_to_ten_percent',
    'test_init_title_override_is_stored_verbatim',
    'test_node_init_requires_agent',
    'test_init_uses_central_db',
    'test_init_reset_reinitializes_node',
    'test_init_rejects_negative_limits',
    'test_init_rejects_iter_cost_without_max_cost',
    'test_init_rejects_non_positive_max_iters',
    'test_init_from_worktree_nests_under_that_node',
    'test_child_spawn_nests_under_parent',
    'test_child_without_base_branches_from_parent_tip',
    'test_node_init_path_records_subproject',
    'test_node_init_path_below_a_worktree_root_is_refused',
    'test_init_prints_node_md_next_steps',
    'test_init_scaffolds_ignored_tmp_scratch_dir',
    'test_engine_system_skills_ignored',
    'test_init_uncapped_priced_agent_warns',
    'test_init_uncapped_unpriced_agent_stays_quiet',
    'test_init_uncapped_warning_reads_the_spawning_parents_agent',
    'test_init_uncapped_warning_reads_a_template_preset_cap',
    'test_init_blind_seeds_no_subs_and_start_sweeps',
    'test_merge_delete_reaps_the_merged_child',
    'test_merge_ignore_scope_flag_lands_an_out_of_scope_squash',
    'test_delete_warns_of_unmerged_work_before_the_point_of_no_return',
    'test_delete_warns_of_a_descendants_unmerged_work_before_the_prompt',
    'test_delete_refuses_a_locked_worktree_before_any_unmerged_warning',
    'test_scope_lists_the_out_of_scope_paths',
    'test_scope_refuses_a_non_node_path',
    'test_list_filters_by_retired_and_depth',
    'test_list_json_mirrors_csv_shape',
    'test_list_status_count_and_live',
    'test_list_rejects_invalid_filters',
    'test_list_rejects_unknown_status',
    'test_list_whole_tree_from_a_non_init_checkout',
    'test_status_reports_idle_from_anywhere',
    'test_rm_rf_worktree_lists_orphan_then_force_deletes',
    'test_list_shows_stored_status_for_orphaned_terminal_node',
    'test_reconcile_records_orphan_event_once',
    'test_diff_reports_drift_per_surface',
    'test_diff_applies_the_recorded_listing_and_warns_on_a_stale_entry',
    'test_diff_requires_a_recorded_template_and_a_full_sha',
    'test_diff_stays_clean_after_the_template_folder_moves',
    'test_lifecycle_guard_rejects_idle_node',
    'test_start_drain_requires_continue',
    'test_retire_unretire_round_trips_through_list',
    'test_retire_refuses_a_retired_node_and_unretire_restores_its_status',
    'test_update_rewrites_child_config',
    'test_update_changes_title',
    'test_update_rejects_unknown_child_and_negatives',
    'test_update_max_cost_retunes_default_reserve',
    'test_update_resolves_short_name',
    'test_update_validates_config_like_init',
    'test_update_retunes_iter_and_step_cost',
    'test_update_rejects_iter_cost_on_uncapped_child',
    'test_cost_breakdown_rows_sum_to_spent_with_a_deleted_descendant',
    'test_cost_family_answers_for_a_deleted_target',
    'test_cost_family_refuses_a_live_ambiguous_short_name',
    'test_version_flag_reports_a_version',
    'test_table_commands_document_piped_csv_default',
    'test_commit_help_states_message_requirement',
    'test_list_pipe_status_has_no_brackets',
    'test_list_csv_columns_stable_empty_vs_populated',
    'test_activity_json_mirrors_csv_shape',
    'test_activity_names_attribution_and_lineage_columns',
    'test_chat_requires_a_prompt',
    'test_chat_rejects_codex_fork',
    'test_chat_current_requires_a_live_session',
]

# minimal claude stand-in: emits the init + result frames the stream driver
# expects and exits 0, so a launched loop completes an iteration hermetically
_AGENT_STUB = """#!/usr/bin/env bash
SID=$(uuidgen | tr '[:upper:]' '[:lower:]')
printf '{"type":"system","subtype":"init","session_id":"%s"}\\n' "$SID"
printf '{"type":"result","session_id":"%s","total_cost_usd":0.001,"num_turns":1,"duration_ms":1}\\n' "$SID"
"""


@pytest.fixture(scope='module')
def repo(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Return a repo with a user node and two worker nodes (task, docs).

    Built once via the real CLI so the tests exercise ``init``, the
    bootstrapped project wiki, and cross-node configuration. ``task`` is
    created with the full set of run-config flags; ``docs`` is a plain
    detached worker. Mutating tests either init (and delete) their own
    uniquely-named workers, round-trip the state they touch (retire then
    unretire), or confirm rewrites against prior values they read first,
    so no test depends on another's writes.
    """
    root = tmp_path_factory.mktemp('fractal_node')
    _init_tree(root, 'node')
    task = _run(
        root,
        'node',
        'init',
        'task',
        '--scope',
        'src',
        '--base',
        'main',
        '--agent',
        'claude',
        '--model',
        'sonnet',
        '--max-iters',
        '5',
        '--max-depth',
        '2',
        '--max-children',
        '3',
        '--max-descendants',
        '4',
        '--timeout',
        '30s',
        '--iter-timeout',
        '20s',
        '--max-cost',
        '1.5',
        '--max-step-cost',
        '0.25',
        '--reserve-budget',
        '0.5',
        '--local',
    )
    assert task.returncode == 0, task.stderr
    docs = _run(root, 'node', 'init', 'docs', '--agent', 'codex', '--detached')
    assert docs.returncode == 0, docs.stderr
    return {
        'root': root,
        'task': root / '.worktrees' / 'main.task',
        'docs': root / '.worktrees' / 'main.docs',
    }


# ------ init


@pytest.mark.parametrize(
    argnames=('key', 'expected'),
    argvalues=[
        ('scope', 'src'),
        ('base', 'main'),
        ('agent', 'claude'),
        ('model', 'sonnet'),
        ('title', 'Task'),
        ('max_iters', '5'),
        ('max_depth', '2'),
        ('max_children', '3'),
        ('max_descendants', '4'),
        ('timeout', '30s'),
        ('iter_timeout', '20s'),
        ('max_cost', '1.5'),
        ('max_step_cost', '0.25'),
        ('reserve_budget', '0.5'),
        ('local', 'true'),
    ],
)
def test_init_persists_run_config(repo: dict, key: str, expected: str) -> None:
    """Every init flag lands in the worker's ``config.json``.

    Drives the persisted value back out through ``config _get`` rather
    than reading the file directly, so the test tracks observable
    behavior and survives storage refactors.
    """
    assert _config(repo['task'], key) == expected


def test_init_ignores_ambient_provider_and_effort(repo: dict) -> None:
    """Exported ``PROVIDER``/``EFFORT`` shell variables never reach the config.

    Only the explicit ``--provider``/``--effort`` flags set these keys; an
    operator's ambient environment must not silently reroute the agent.
    """
    root = repo['root']
    ambient = _run(
        root,
        'node',
        'init',
        'ambient',
        '--agent',
        'claude',
        '--local',
        PROVIDER='openrouter',
        EFFORT='max',
    )
    assert ambient.returncode == 0, ambient.stderr
    worktree = root / '.worktrees' / 'main.ambient'
    assert _config(worktree, 'provider') == ''
    assert _config(worktree, 'effort') == ''


@pytest.mark.parametrize(
    argnames=('name', 'scope_flags', 'expected'),
    argvalues=[
        ('comma', ['--scope', 'src,docs'], ['src', 'docs']),
        ('repeat', ['--scope', 'src', '--scope', 'docs'], ['src', 'docs']),
        (
            'mixed',
            ['--scope', 'src,docs', '--scope', 'extra'],
            ['src', 'docs', 'extra'],
        ),
    ],
)
def test_scope_flags_flatten_to_recorded_roots(
    repo: dict,
    name: str,
    scope_flags: list[str],
    expected: list[str],
) -> None:
    """Comma, repeated, and mixed ``--scope`` forms flatten to one root list.

    The commit boundary consumes the recorded roots, so every form must
    persist the full list: ``config.json`` stores a JSON list (the pinned
    storage format shell consumers rely on) and ``config _get`` prints one
    root per line.
    """
    root = repo['root']
    multi = _run(
        root,
        'node',
        'init',
        f'multi_{name}',
        *scope_flags,
        '--agent',
        'claude',
        '--local',
    )
    assert multi.returncode == 0, multi.stderr
    worktree = root / '.worktrees' / f'main.multi_{name}'
    assert _config(worktree, 'scope') == '\n'.join(expected)
    config_path = worktree / '.fractal' / f'main.multi_{name}' / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    assert config['scope'] == expected


def test_space_joined_scope_normalizes_on_read(repo: dict) -> None:
    """A space-joined ``scope`` string reads as the split root list.

    A config may hold scope as one space-joined string; the read path
    normalizes it so the node keeps its multi-root boundary without a
    rewrite -- the stored file stays untouched until the next write.
    """
    root = repo['root']
    edited = _run(
        root,
        'node',
        'init',
        'edited',
        '--scope',
        'src',
        '--agent',
        'claude',
        '--local',
    )
    assert edited.returncode == 0, edited.stderr
    worktree = root / '.worktrees' / 'main.edited'
    config_path = worktree / '.fractal' / 'main.edited' / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config['scope'] = 'src docs'
    config_path.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
    assert _config(worktree, 'scope') == 'src\ndocs'
    # normalization happens on read: the stored string form is not rewritten
    stored = json.loads(config_path.read_text(encoding='utf-8'))
    assert stored['scope'] == 'src docs'


def test_init_reserve_budget_defaults_to_ten_percent(repo: dict) -> None:
    """With ``--max-cost`` and no ``--reserve-budget``, the reserve defaults to 10%."""
    root = repo['root']
    spawn = _run(
        root,
        'node',
        'init',
        'budgeted',
        '--agent',
        'claude',
        '--max-cost',
        '10',
    )
    assert spawn.returncode == 0, spawn.stderr
    node = root / '.worktrees' / 'main.budgeted'
    assert _config(node, 'reserve_budget') == '1.0'
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.budgeted', '--force').returncode == 0


def test_init_title_override_is_stored_verbatim(repo: dict) -> None:
    """``--title`` overrides the de-slugged default and is stored verbatim."""
    root = repo['root']
    spawn = _run(
        root,
        'node',
        'init',
        'pipeline',
        '--agent',
        'claude',
        '--title',
        'Data Pipeline v2',
    )
    assert spawn.returncode == 0, spawn.stderr
    node = root / '.worktrees' / 'main.pipeline'
    assert _config(node, 'title') == 'Data Pipeline v2'
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.pipeline', '--force').returncode == 0


def test_node_init_requires_agent(repo: dict) -> None:
    """``node init`` needs an agent -- from ``--agent`` or an ancestor default.

    The repo's user node carries no agent, so a bare ``node init`` is refused
    with guidance before any node is created; passing ``--agent`` succeeds and
    seeds the agent config.
    """
    root = repo['root']
    # no --agent and no inheritable default: refused before a node is created
    result = _run(root, 'node', 'init', 'auto')
    assert result.returncode != 0
    assert 'agent' in result.stderr.lower()
    assert not (root / '.worktrees' / 'main.auto').exists()
    # with --agent: succeeds and seeds the agent config
    assert _run(root, 'node', 'init', 'auto', '--agent', 'claude').returncode == 0
    node_dir = root / '.worktrees' / 'main.auto' / '.fractal' / 'main.auto'
    assert (node_dir / '.claude' / 'settings.json').is_file()
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.auto', '--force').returncode == 0


def test_init_uses_central_db(repo: dict) -> None:
    """Node init writes no per-node database -- the tree shares the root's.

    A worker's data directory holds config and seeds only; every database
    row lands in the central ``.db`` at the user node, which ``db _query``
    answers from regardless of the ``--path`` it is invoked with.
    """
    root, task = repo['root'], repo['task']
    assert not (task / '.fractal' / 'main.task' / '.db').exists()
    assert (root / '.fractal' / 'main' / '.db').is_file()
    # the same central registry answers from the root and from a worker
    registry = "SELECT node FROM nodes WHERE node = 'main.task'"
    for path in (root, task):
        result = _run(path, 'db', '_query', registry, '--csv')
        assert result.returncode == 0
        assert 'main.task' in result.stdout


def test_init_reset_reinitializes_node(repo: dict) -> None:
    """``--reset`` re-inits config in place -- and spares the central database."""
    root, docs = repo['root'], repo['docs']
    before = _config(docs, 'max_iters')
    reset = _run(
        root,
        'node',
        'init',
        'docs',
        '--reset',
        '--max-iters',
        '9',
        '--agent',
        'codex',
    )
    assert reset.returncode == 0
    assert _config(docs, 'max_iters') == '9'
    assert before != '9'
    # a node-level reset must never wipe the tree's shared history
    assert (root / '.fractal' / 'main' / '.db').is_file()
    registry = _run(root, 'db', '_query', 'SELECT node FROM nodes', '--csv')
    assert 'main.docs' in registry.stdout


@pytest.mark.parametrize(
    argnames=('flag', 'error'),
    argvalues=[
        ('--max-iters', 'max_iters must be greater than 0'),
        ('--max-depth', 'max_depth must be >= 0'),
        ('--max-children', 'max_children must be >= 0'),
        ('--max-cost', 'max_cost must be greater than 0'),
        ('--max-iter-cost', 'max_iter_cost must be greater than 0'),
    ],
)
def test_init_rejects_negative_limits(repo: dict, flag: str, error: str) -> None:
    """``init`` rejects every negative numeric cap (exit 2, naming the key).

    The refusal runs in core on the merged config -- a negative cap from a
    template preset refuses exactly like the flag -- so the message names
    the config key. Unbounded is expressed by omitting the flag, never a
    negative sentinel, and rejection happens before any node is created.
    """
    result = _run(repo['root'], 'node', 'init', 'neg', '--agent', 'claude', flag, '-1')
    assert result.returncode == 2, result.stderr
    assert error in (result.stdout + result.stderr)


@pytest.mark.parametrize('flag', ['--max-iter-cost', '--max-step-cost'])
def test_init_rejects_iter_cost_without_max_cost(repo: dict, flag: str) -> None:
    """A per-iteration/step cap with no ``--max-cost`` is rejected at init.

    A node always carries its own ``max_cost`` (``start`` refuses without a
    positive one), so an iter/step cap alone would build a node that can never
    start. The merged-config validation rejects it up front (exit 2, naming
    ``max_cost``) and creates no node, rather than letting it wedge at launch;
    pairing the cap with a ``--max-cost`` succeeds.
    """
    root = repo['root']
    rejected = _run(root, 'node', 'init', 'capless', '--agent', 'claude', flag, '5')
    assert rejected.returncode == 2, rejected.stderr
    assert 'max_cost' in (rejected.stdout + rejected.stderr)
    # the rejected init created nothing -- no worktree for the would-be node
    assert not (root / '.worktrees' / 'main.capless').exists()
    # the same cap with a run ceiling is accepted
    ok = _run(
        root,
        'node',
        'init',
        'capped2',
        '--agent',
        'claude',
        flag,
        '5',
        '--max-cost',
        '10',
    )
    assert ok.returncode == 0, ok.stderr
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.capped2', '--force').returncode == 0


@pytest.mark.parametrize(argnames='value', argvalues=['0', '-3'])
def test_init_rejects_non_positive_max_iters(repo: dict, value: str) -> None:
    """``init`` rejects a non-positive ``--max-iters`` (exit 2).

    A non-positive cap reads as unlimited in the loop, so 0 would build a
    node that iterates without bound instead of never -- the merged-config
    validation refuses it before any node is created.
    """
    root = repo['root']
    result = _run(
        root,
        'node',
        'init',
        'noiter',
        '--agent',
        'claude',
        '--max-iters',
        value,
    )
    assert result.returncode == 2, result.stderr
    assert 'greater than 0' in (result.stdout + result.stderr)
    assert not (root / '.worktrees' / 'main.noiter').exists()


def test_init_from_worktree_nests_under_that_node(repo: dict) -> None:
    """A manual ``node init`` from inside a worktree nests under that node.

    The agent loop sets ``_NODE`` so a child nests under its caller; a human
    running ``node init`` by hand has no ``_NODE``, so init derives the
    parent from the worktree the caller occupies rather than silently
    falling back to the root user node. The cwd-derived parent enforces its
    spawn constraints like any other (task is capped, so the child must set
    ``--max-cost``).
    """
    root = repo['root']
    # manual init from inside the task worktree (no _NODE): nests under task,
    # with no top-level fallback notice (the default matches intent)
    stray = _run(
        repo['task'],
        'node',
        'init',
        'stray',
        '--agent',
        'claude',
        '--max-cost',
        '0.5',
    )
    assert stray.returncode == 0, stray.stderr
    assert (root / '.worktrees' / 'main.task.stray').exists()
    assert not (root / '.worktrees' / 'main.stray').exists()
    assert 'top level' not in stray.stderr
    # the same call with a _NODE caller context still nests under task
    nested = _run(
        repo['task'],
        'node',
        'init',
        'nested',
        '--agent',
        'claude',
        '--max-cost',
        '0.5',
        _NODE=str(repo['task']),
    )
    assert nested.returncode == 0, nested.stderr
    assert (root / '.worktrees' / 'main.task.nested').exists()
    # an explicit repo-root --path from the same cwd keeps the root default
    top = _run(
        repo['task'],
        'node',
        'init',
        'explicit_top',
        '--agent',
        'claude',
        '--path',
        str(root),
    )
    assert top.returncode == 0, top.stderr
    assert (root / '.worktrees' / 'main.explicit_top').exists()
    # clean up all three so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.task.stray', '--force').returncode == 0
    assert _run(root, 'node', 'delete', 'main.task.nested', '--force').returncode == 0
    assert _run(root, 'node', 'delete', 'main.explicit_top', '--force').returncode == 0


def test_child_spawn_nests_under_parent(repo: dict) -> None:
    """A child spawned for ``task`` should be ``main.task.c1``."""
    root, task = repo['root'], repo['task']
    node_dir = task / '.fractal' / 'main.task'
    # run from inside the worktree with no --path: the caller (_NODE) drives
    # both the repo-root resolution and the parent nesting; task is capped,
    # so the child must set --max-cost
    spawn = _run(
        task,
        'node',
        'init',
        'c1',
        '--agent',
        'claude',
        '--max-cost',
        '0.5',
        _NODE=str(node_dir),
    )
    assert spawn.returncode == 0, spawn.stderr
    assert (root / '.worktrees' / 'main.task.c1').exists()
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.task.c1', '--force').returncode == 0


def test_child_without_base_branches_from_parent_tip(
    tmp_path: pathlib.Path,
) -> None:
    """A child spawned without ``--base`` branches from its parent's tip.

    Creating the child worktree with no start ref would branch it from the
    main repo's HEAD rather than the spawning node -- starting divergent
    (missing the parent's commits) until the first parent merge. Uses its own
    repo (not the shared fixture) so the parent can be advanced one commit
    past ``main``; the child must inherit that commit at creation.
    """
    root = tmp_path / 'repo'
    root.mkdir()
    _init_tree(root, 'base')
    assert _run(root, 'node', 'init', 'task', '--agent', 'claude').returncode == 0

    # advance the parent one commit past main, in its own worktree
    task = root / '.worktrees' / 'main.task'
    (task / 'parent_work.txt').write_text('only on the parent\n', encoding='utf-8')
    _git(task, 'add', 'parent_work.txt')
    _git(task, 'commit', '-m', 'parent work')

    # delegate a child from inside the parent with no --base: nests as main.task.c1
    node_dir = task / '.fractal' / 'main.task'
    spawn = _run(task, 'node', 'init', 'c1', '--agent', 'claude', _NODE=str(node_dir))
    assert spawn.returncode == 0, spawn.stderr

    # the child branched from the parent tip, so the parent's commit is present;
    # it would be ABSENT had the child branched off main HEAD
    child = root / '.worktrees' / 'main.task.c1'
    assert (child / 'parent_work.txt').exists(), spawn.stdout
    # main never saw that file -- proves the parent was genuinely ahead
    assert not (root / 'parent_work.txt').exists()


def test_node_init_path_records_subproject(tmp_path: pathlib.Path) -> None:
    """``node init --path=<subdir>`` records the sub-project, not ``'.'``.

    A monorepo child pointed at a sub-project via ``--path`` must record it
    like the ``fractal init <subdir>`` user flow: the resolved sub-path
    reaches init.sh instead of being dropped for the parent's project.
    """
    root = tmp_path
    # a root user node, then a child pointed at the sub-project
    _init_tree(root, 'mono', projects=('app',))
    result = _run(root, 'node', 'init', 'sub', '--path', 'app', '--agent', 'claude')
    assert result.returncode == 0, result.stderr
    # the child records project 'app' and nests its data under it
    cache = root / '.worktrees' / '.project' / 'main.sub'
    assert cache.read_text(encoding='utf-8').strip() == 'app'
    worktree = root / '.worktrees' / 'main.sub'
    config = worktree / 'app' / '.fractal' / 'main.sub' / 'config.json'
    assert config.is_file()
    assert json.loads(config.read_text(encoding='utf-8'))['project'] == 'app'


def test_node_init_path_below_a_worktree_root_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """``node init --path app`` from inside a parent's worktree is refused.

    A running node spawns with its own worktree as cwd, so a relative
    ``--path app`` there resolves below ``.worktrees/<parent>/`` -- a path
    that stands for the parent node and cannot carry a sub-project.
    Anchoring it at the parent silently would record project ``.`` and seed
    the child at the worktree root, so init refuses before any write and
    names the repo-root form; that form, from the same cwd, records the
    sub-project and nests the child's seed under it.
    """
    root = tmp_path
    _init_tree(root, 'mono', projects=('app',))
    init = _run(root, 'node', 'init', 'a', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    parent = root / '.worktrees' / 'main.a'
    _git(parent, 'add', '-A')
    _git(parent, 'commit', '-m', 'seed a')
    # the spawn route: cwd is the parent's worktree and _NODE names its seed
    node_dir = parent / '.fractal' / 'main.a'
    refused = _run(
        parent,
        'node',
        'init',
        'c',
        '--path',
        'app',
        '--agent',
        'claude',
        '--local',
        _NODE=str(node_dir),
    )
    assert refused.returncode != 0, refused.stdout
    # typer boxes and hard-wraps a bad-parameter message, so read it collapsed
    rendered = ' '.join(refused.stderr.replace('│', ' ').split())
    assert (
        'lies inside a node worktree; a sub-project child is initialized'
        ' against the repo root: --path'
    ) in rendered, refused.stderr
    # the panel folds a long path mid-word, so read the remedy with no spaces
    remedy = root / 'app'
    assert f'--path{remedy}' in ''.join(rendered.split()), refused.stderr
    # refused before any write: no worktree, no branch, no registry row
    assert not (root / '.worktrees' / 'main.a.c').exists()
    assert _git(root, 'branch', '--list', 'main.a.c').stdout.strip() == ''
    assert 'main.a.c' not in _run(root, 'node', 'list', '--all', '--csv').stdout
    # the repo-root form from the same cwd records the sub-project
    result = _run(
        parent,
        'node',
        'init',
        'c',
        '--path',
        str(root / 'app'),
        '--agent',
        'claude',
        '--local',
        _NODE=str(node_dir),
    )
    assert result.returncode == 0, result.stderr
    cache = root / '.worktrees' / '.project' / 'main.a.c'
    assert cache.read_text(encoding='utf-8').strip() == 'app'
    child = root / '.worktrees' / 'main.a.c'
    config = child / 'app' / '.fractal' / 'main.a.c' / 'config.json'
    assert config.is_file()
    assert json.loads(config.read_text(encoding='utf-8'))['project'] == 'app'
    assert not (child / '.fractal' / 'main.a.c').exists()


def test_init_prints_node_md_next_steps(repo: dict) -> None:
    """``node init`` ends with the task contract and the start command.

    The next-steps block prints after the ``Initialized ...`` line so the
    actionable step is the last thing on the terminal.
    """
    root = repo['root']
    spawn = _run(root, 'node', 'init', 'guided', '--agent', 'claude', '--max-cost', '1')
    assert spawn.returncode == 0, spawn.stderr
    # the next-steps block follows the "Initialized ..." line: the node-dir
    # NODE.md path, the (blank) contract sections, and the start command
    tail = spawn.stdout[spawn.stdout.index('Initialized ') :]
    assert '/.fractal/main.guided/NODE.md' in tail
    assert 'Instructions' in tail
    assert 'Completion Requirements' in tail
    assert 'fractal node start main.guided' in tail
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.guided', '--force').returncode == 0


def test_init_scaffolds_ignored_tmp_scratch_dir(repo: dict) -> None:
    """``node init`` scaffolds a ``tmp/`` scratch dir that git never commits.

    Without a sanctioned scratch dir, throwaway artifacts like page caches
    would land in commits; the managed ``info/exclude`` block keeps its
    contents out.
    """
    task = repo['task']
    assert (task / '.fractal' / 'main.task' / 'tmp').is_dir()
    # the exclude machinery ignores scratch content inside the node worktree
    probe = subprocess.run(
        ['git', '-C', f'{task}', 'check-ignore', '-q', '.fractal/main.task/tmp/x'],
        capture_output=True,
    )
    assert probe.returncode == 0


def test_engine_system_skills_ignored(repo: dict) -> None:
    """Engine-materialized system skills under ``skills/.system/`` stay untracked.

    Codex materializes its bundled system skills into ``$CODEX_HOME/skills/``
    at launch, and the agent seed symlinks that path at the node's tracked
    ``skills/`` dir -- so without the exclude, every node branch would sweep
    ~1,100 engine files into its next work commit.
    """
    task = repo['task']
    # the managed info/exclude block keeps the engine tree out of git
    cmd = [
        'git',
        '-C',
        f'{task}',
        'check-ignore',
        '-q',
        '.fractal/main.task/skills/.system/imagegen/SKILL.md',
    ]
    probe = subprocess.run(cmd, capture_output=True)
    assert probe.returncode == 0


@pytest.mark.parametrize(
    argnames=('flags', 'warns'),
    argvalues=[
        ([], True),
        (['--max-cost', '1'], False),
        (['--max-iters', '3'], False),
    ],
    ids=['uncapped', 'max-cost', 'max-iters'],
)
def test_init_uncapped_priced_agent_warns(
    repo: dict,
    flags: list[str],
    warns: bool,
) -> None:
    """Uncapped ``node init`` on a priced agent warns once on stderr.

    With neither ``--max-cost`` nor ``--max-iters`` the node can spend
    without bound, so init says so -- one advisory line naming both flags,
    never a block (the command still succeeds).
    """
    root = repo['root']
    spawn = _run(root, 'node', 'init', 'capchk', '--agent', 'claude', *flags)
    assert spawn.returncode == 0, spawn.stderr
    warnings = [
        line
        for line in spawn.stderr.splitlines()
        if '--max-cost' in line and '--max-iters' in line
    ]
    assert len(warnings) == (1 if warns else 0), spawn.stderr
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.capchk', '--force').returncode == 0


def test_init_uncapped_unpriced_agent_stays_quiet(repo: dict) -> None:
    """An agent fractal cannot price skips the uncapped warning.

    ``codex`` usage is priced through the pricing cache keyed by model; with
    no ``--model`` there is no rate to meter spend against, so the uncapped
    warning would name a spend fractal never tracks -- init stays quiet.
    """
    root = repo['root']
    spawn = _run(root, 'node', 'init', 'quietchk', '--agent', 'codex')
    assert spawn.returncode == 0, spawn.stderr
    assert '--max-cost' not in spawn.stderr
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.quietchk', '--force').returncode == 0


def test_init_uncapped_warning_reads_the_spawning_parents_agent(repo: dict) -> None:
    """The uncapped warning meters the calling node's agent, not the root's.

    An agent spawns children under itself (``_NODE``), so init inherits that
    node's agent -- the warning must read the same chain. A codex (unpriced)
    parent spawning an uncapped child stays quiet even though the root default
    is a priced claude.
    """
    root = repo['root']
    assert _run(root, 'node', 'init', 'coparent', '--agent', 'codex').returncode == 0
    parent_dir = root / '.worktrees' / 'main.coparent' / '.fractal' / 'main.coparent'
    spawn = _run(root, 'node', 'init', 'gchild', _NODE=str(parent_dir))
    assert spawn.returncode == 0, spawn.stderr
    # the effective agent is the codex parent's -- unpriced, so no warning
    assert '--max-cost' not in spawn.stderr
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.coparent', '--force').returncode == 0


def test_init_uncapped_warning_reads_a_template_preset_cap(repo: dict) -> None:
    """A template preset cap silences the uncapped-spend warning.

    The warning's flag-only view would call a preset-capped node unbounded,
    so the CLI reads the merged caps back from the child's stored config: a
    node capped by its template draws no warning (and carries the preset
    cap), while a template with no caps still warns.
    """
    root = repo['root']
    # commit a capped and a capless template -- template bytes deploy from
    # the fork commit, never the working copy
    for folder, preset in (('capped', '{"max_cost": 5}'), ('bare', '{}')):
        config = root / 'templates' / folder / 'config.json'
        config.parent.mkdir(parents=True)
        config.write_text(preset + '\n', encoding='utf-8')
    _git(root, 'add', 'templates')
    _git(root, 'commit', '-m', 'add templates')
    try:
        spawn = _run(
            root,
            'node',
            'init',
            'presetcap',
            '--agent',
            'claude',
            '--template',
            'templates/capped',
        )
        assert spawn.returncode == 0, spawn.stderr
        assert '--max-cost' not in spawn.stderr
        node = root / '.worktrees' / 'main.presetcap'
        assert _config(node, 'max_cost') == '5'
        bare = _run(
            root,
            'node',
            'init',
            'barecap',
            '--agent',
            'claude',
            '--template',
            'templates/bare',
        )
        assert bare.returncode == 0, bare.stderr
        warnings = [
            line
            for line in bare.stderr.splitlines()
            if '--max-cost' in line and '--max-iters' in line
        ]
        assert len(warnings) == 1, bare.stderr
    finally:
        # clean up so the shared module fixture is left as other tests expect
        for worker in ('main.presetcap', 'main.barecap'):
            _run(root, 'node', 'delete', worker, '--force')


def test_init_blind_seeds_no_subs_and_start_sweeps(
    repo: dict,
    tmp_path: pathlib.Path,
) -> None:
    """``--blind`` persists, seeds no subs, and ``start`` sweeps raced rows.

    A blind worker's config carries ``blind``; its radio seeds channels but
    no subscriptions, while the parent's own watch of it is untouched. A
    subscription planted between init and launch -- the window the start-time
    sweep closes -- is gone once ``node start`` boots the run.
    """
    _require_tmux()
    root = repo['root']
    spawn = _run(
        root,
        'node',
        'init',
        'blindkid',
        '--agent',
        'claude',
        '--blind',
        '--max-iters',
        '1',
        '--no-sync',
        '--local',
    )
    assert spawn.returncode == 0, spawn.stderr
    worktree = root / '.worktrees' / 'main.blindkid'
    assert _config(worktree, 'blind') == 'true'
    # the blind child holds no subscriptions of its own (header-only csv)
    fresh = _run(worktree, 'radio', 'subs', '--csv').stdout
    assert len(fresh.strip().splitlines()) == 1, fresh
    # the parent's own watch of the blind child is untouched
    watching = _run(root, 'radio', 'subs', '--csv').stdout
    assert 'main.blindkid' in watching
    # plant a raced sub -- the init-to-start window the sweep closes
    assert _run(worktree, 'radio', 'sub', '--node', 'main').returncode == 0
    planted = _run(worktree, 'radio', 'subs', '--csv').stdout
    assert len(planted.strip().splitlines()) > 1, planted
    # one trivial committed step so the stubbed launch settles quickly
    steps_dir = worktree / '.fractal' / 'main.blindkid' / 'steps'
    for step in steps_dir.glob('*.md'):
        step.unlink()
    (steps_dir / '01-work.md').write_text('# Work\n\nOne step.\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'setup blindkid')
    # launch with the stub agent on PATH (start.sh propagates PATH into tmux)
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    agent = bindir / 'claude'
    agent.write_text(_AGENT_STUB, encoding='utf-8')
    agent.chmod(0o755)
    env = _cli_env()
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    session = f'{root.name} (main-blindkid)'
    try:
        started = subprocess.run(
            [_fractal_bin(), 'node', 'start'],
            cwd=worktree,
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )
        assert started.returncode == 0, started.stderr
        # the sweep runs before the launch, so the raced row is already gone
        swept = _run(worktree, 'radio', 'subs', '--csv').stdout
        assert len(swept.strip().splitlines()) == 1, swept
        # let the one-iteration run settle so the cleanup delete is legal
        assert _await_settled(worktree), _run(worktree, 'node', 'status').stdout
    finally:
        # `=` prefix forces an exact target match (no prefix resolution)
        subprocess.run(
            ['tmux', 'kill-session', '-t', f'={session}'],
            capture_output=True,
        )
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.blindkid', '--force').returncode == 0


# ------ merge


def test_merge_delete_reaps_the_merged_child(tmp_path: pathlib.Path) -> None:
    """``node merge --delete`` chains the teardown onto a landed merge.

    The lifecycle's happy path ends with the child's work on the parent and
    no residue: worktree, branch, and registry row all go with the one
    command, and the removal echoes the worktree's size. The teardown's own
    refusals -- and its confirmation gate, which ``--force`` skips --
    pre-flight the merge, so a chain that cannot finish never starts -- the
    squash it would leave behind is irreversible.
    """
    root = tmp_path
    _init_tree(root, 'reap')
    assert _run(root, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    # the child commits real work on its branch
    task = root / '.worktrees' / 'main.task'
    _git(task, 'config', 'user.email', 'reap@test.local')
    _git(task, 'config', 'user.name', 'reap')
    (task / 'feature.txt').write_text('the work\n', encoding='utf-8')
    _git(task, 'add', '-A')
    _git(task, 'commit', '-m', 'main.task: feature')
    # from inside the doomed worktree the chain refuses up front: git cannot
    # remove a worktree the caller occupies, and that refusal must land
    # before the squash rather than after a merge that already committed
    inside = _run(task, 'node', 'merge', '--delete')
    assert inside.returncode != 0
    rendered = ' '.join(inside.stderr.replace('│', ' ').split())
    assert 'Cannot delete the current worktree from inside it.' in rendered
    assert not (root / 'feature.txt').exists()
    assert task.exists()
    # a paused descendant refuses the chain the same way -- its frozen work
    # would block the teardown, so the refusal must also land before the squash
    assert _run(task, 'node', 'init', 'sub', '--agent', 'claude').returncode == 0
    sub = Node(root / '.worktrees' / 'main.task.sub')
    sub.status_set('paused')
    frozen = _run(root, 'node', 'merge', 'main.task', '--delete')
    assert frozen.returncode != 0
    rendered = ' '.join(frozen.stderr.replace('│', ' ').split())
    assert 'active or paused descendant' in rendered
    assert not (root / 'feature.txt').exists()
    # settled again, the chain still gates on the delete's confirmation:
    # declining aborts before the squash, leaving everything in place
    sub.status_set('idle')
    declined = _run(root, 'node', 'merge', 'main.task', '--delete', stdin='n\n')
    assert declined.returncode != 0
    assert not (root / 'feature.txt').exists()
    assert task.exists()
    # one forced command merges the work and reaps the whole subtree
    result = _run(root, 'node', 'merge', 'main.task', '--delete', '--force')
    assert result.returncode == 0, result.stderr
    # the work landed on the parent...
    assert (root / 'feature.txt').is_file()
    # ...and the child left no residue: worktree, branch, and registry row
    assert not task.exists()
    assert _git(root, 'branch', '--list', 'main.task').stdout.strip() == ''
    registry = _run(root, 'db', '_query', 'SELECT node FROM nodes', '--csv')
    assert registry.returncode == 0, registry.stderr
    assert 'main.task' not in registry.stdout
    # the removal echo carries the reaped worktree's size: a real du reading
    # (digits and a unit), never the '?' fallback of a failed measurement
    removed = next(
        line
        for line in result.stdout.splitlines()
        if line.startswith('Removed worktree:')
    )
    size = removed.rsplit('(', 1)[-1].removesuffix(')')
    assert re.fullmatch(r'[\d.]+[A-Za-z]?', size), removed


def test_merge_ignore_scope_flag_lands_an_out_of_scope_squash(
    tmp_path: pathlib.Path,
) -> None:
    """``node merge --ignore-scope`` lands a squash the footprint check refuses.

    The squash is the one point that sees a scoped node's whole offering
    (commit-time scope is bypassable), so a path outside the node's scope
    refuses the merge by default and restores the target. The flag reaches
    ``merge.sh`` as its override and lands the offering as it is, both files
    tracked on the target.
    """
    root = tmp_path
    _init_tree(root, 'scope')
    init = _run(
        root,
        'node',
        'init',
        'task',
        '--scope',
        'docs',
        '--agent',
        'claude',
        '--local',
    )
    assert init.returncode == 0, init.stderr
    # work in and out of scope, committed with raw git (fractal commit would
    # refuse the out-of-scope path itself)
    task = root / '.worktrees' / 'main.task'
    (task / 'docs').mkdir()
    (task / 'docs' / 'a.md').write_text('# a\n', encoding='utf-8')
    (task / 'outside.txt').write_text('outside the scope\n', encoding='utf-8')
    _git(task, 'add', '-A')
    _git(task, 'commit', '-m', 'main.task: work in and out of scope')
    main_head = _git(root, 'rev-parse', 'HEAD').stdout.strip()

    # refused by default, naming the stray path, with the target restored
    refused = _run(root, 'node', 'merge', f'--path={task}')
    assert refused.returncode != 0, refused.stdout
    assert 'outside its scope' in refused.stderr, refused.stderr
    assert 'outside.txt' in refused.stderr, refused.stderr
    assert _git(root, 'rev-parse', 'HEAD').stdout.strip() == main_head
    assert not (root / 'outside.txt').exists()
    assert not (root / 'docs').exists()

    # the override lands the offering as it is
    landed = _run(root, 'node', 'merge', '--ignore-scope', f'--path={task}')
    assert landed.returncode == 0, landed.stderr
    assert 'Squash-merged main.task into main' in landed.stdout, landed.stdout
    tracked = _git(root, 'ls-files', 'docs/a.md', 'outside.txt').stdout.split()
    assert tracked == ['docs/a.md', 'outside.txt']
    assert (root / 'outside.txt').read_text(encoding='utf-8') == 'outside the scope\n'


# ------ delete


@pytest.mark.parametrize(
    argnames='force',
    argvalues=[
        pytest.param(False, id='prompted'),
        pytest.param(True, id='forced'),
    ],
)
def test_delete_warns_of_unmerged_work_before_the_point_of_no_return(
    tmp_path: pathlib.Path,
    force: bool,
) -> None:
    """``node delete`` warns of unmerged work ahead of the prompt, and only once.

    ``delete.sh`` judges unmerged work on its own path, just before the
    teardown -- too late for the operator at the confirmation prompt, who
    decides while the branch still exists to merge. The CLI makes the same
    judgment first and prints it before the prompt (before the deletion,
    under ``--force``), dropping the script's repeat so the warning reads
    once; a declined prompt leaves the node standing, and the forced
    teardown names the deleted tip.
    """
    root = tmp_path
    _init_tree(root, 'warn')
    init = _run(root, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    # the child commits work main never absorbs
    task = root / '.worktrees' / 'main.task'
    (task / 'feature.txt').write_text('the work\n', encoding='utf-8')
    _git(task, 'add', '-A')
    _git(task, 'commit', '-m', 'main.task: feature')
    unmerged = (
        'Warning: main.task has commits not merged into main; deleting discards'
        ' them (merge first to keep them)'
    )
    if not force:
        # declined at the prompt: the warning came first, and
        # nothing was torn down
        declined = _run(root, 'node', 'delete', f'--path={task}', stdin='n\n')
        assert declined.returncode != 0, declined.stdout
        assert declined.stderr.count(unmerged) == 1, declined.stderr
        prompted = declined.stderr.index('This permanently removes')
        assert declined.stderr.index(unmerged) < prompted, declined.stderr
        assert task.exists()
        assert _git(root, 'branch', '--list', 'main.task').stdout.strip() != ''
        return
    result = _run(root, 'node', 'delete', f'--path={task}', '--force')
    assert result.returncode == 0, result.stderr

    # the warning reads once, and the teardown names the deleted tip
    assert result.stderr.count(unmerged) == 1, result.stderr
    pattern = r'Deleted branch: main\.task \(was [0-9a-f]{7,}\)'
    deleted = re.search(pattern, result.stdout)
    assert deleted, result.stdout
    assert not task.exists()
    assert _git(root, 'branch', '--list', 'main.task').stdout.strip() == ''


@pytest.mark.parametrize(
    argnames='force',
    argvalues=[
        pytest.param(False, id='prompted'),
        pytest.param(True, id='forced'),
    ],
)
def test_delete_warns_of_a_descendants_unmerged_work_before_the_prompt(
    tmp_path: pathlib.Path,
    force: bool,
) -> None:
    """``node delete`` warns of a descendant's unmerged work ahead of the prompt.

    A subtree teardown discards every descendant's unmerged work too, and
    ``delete.sh`` warns of each only as it tears that node down -- after its
    branch is gone, and never at the prompt. The CLI judges the whole
    subtree first, each descendant against the deleted node's surviving
    target (the one its ``delete.sh`` is threaded), so the operator decides
    with every warning in view and each reads once, before any removal.
    """
    root = tmp_path
    _init_tree(root, 'warn')
    init = _run(root, 'node', 'init', 'alpha', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    alpha = root / '.worktrees' / 'main.alpha'
    _git(alpha, 'add', '-A')
    _git(alpha, 'commit', '-m', 'seed alpha')
    init = _run(
        root,
        'node',
        'init',
        'gamma',
        '--agent',
        'claude',
        '--local',
        f'--path={alpha}',
    )
    assert init.returncode == 0, init.stderr
    # the grandchild commits work nobody merges; alpha itself has none
    gamma = root / '.worktrees' / 'main.alpha.gamma'
    (gamma / 'feature.txt').write_text('the work\n', encoding='utf-8')
    _git(gamma, 'add', '-A')
    _git(gamma, 'commit', '-m', 'main.alpha.gamma: feature')
    unmerged = (
        'Warning: main.alpha.gamma has commits not merged into main; deleting'
        ' discards them (merge first to keep them)'
    )
    if not force:
        # declined at the prompt: the grandchild's warning came
        # first, and the whole subtree still stands
        declined = _run(root, 'node', 'delete', 'main.alpha', stdin='n\n')
        assert declined.returncode != 0, declined.stdout
        assert declined.stderr.count(unmerged) == 1, declined.stderr
        prompted = declined.stderr.index('This permanently removes')
        assert declined.stderr.index(unmerged) < prompted, declined.stderr
        assert alpha.exists()
        assert gamma.exists()
        assert _git(root, 'branch', '--list', 'main.alpha*').stdout.count('\n') == 2
        return
    # one merged stream (every echo flushes) so the warning's place among the
    # removal lines is observable: once, and before any node is torn down
    result = subprocess.run(
        [_fractal_bin(), 'node', 'delete', 'main.alpha', '--force'],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=_cli_env(),
        timeout=180,
    )
    merged = result.stdout
    assert result.returncode == 0, merged
    assert merged.count(unmerged) == 1, merged
    assert merged.index(unmerged) < merged.index('Removed worktree:'), merged
    assert not alpha.exists()
    assert not gamma.exists()
    assert _git(root, 'branch', '--list', 'main.alpha*').stdout.strip() == ''


def test_delete_refuses_a_locked_worktree_before_any_unmerged_warning(
    tmp_path: pathlib.Path,
) -> None:
    """``node delete``'s refusals land before its unmerged-work warning.

    The warning describes work a deletion discards, so it is only true of a
    deletion that happens: a teardown git would refuse -- here a locked
    worktree -- exits with its own error alone, never after a warning about
    commits the refused deletion leaves exactly where they are.
    """
    root = tmp_path
    _init_tree(root, 'lock')
    init = _run(root, 'node', 'init', 'task', '--agent', 'claude', '--local')
    assert init.returncode == 0, init.stderr
    # the child commits work main never absorbs, and its worktree is locked
    task = root / '.worktrees' / 'main.task'
    (task / 'feature.txt').write_text('the work\n', encoding='utf-8')
    _git(task, 'add', '-A')
    _git(task, 'commit', '-m', 'main.task: feature')
    _git(root, 'worktree', 'lock', f'{task}')

    result = _run(root, 'node', 'delete', f'--path={task}', '--force')

    # the refusal alone, and nothing torn down
    assert result.returncode != 0, result.stdout
    rendered = ' '.join(result.stderr.replace('│', ' ').split())
    assert 'Cannot delete: worktree is locked' in rendered, result.stderr
    assert 'has commits not merged' not in result.stderr, result.stderr
    assert task.exists()
    assert _git(root, 'branch', '--list', 'main.task').stdout.strip() != ''


# ------ _scope


@pytest.mark.parametrize(
    argnames=('stdin', 'flags', 'returncode', 'stdout'),
    argvalues=[
        pytest.param(b'src/a.py\0src/pkg/b.py\0', [], 0, b'', id='in-scope'),
        pytest.param(
            b'src/a.py\0outside.txt\0lib/c.py\0',
            [],
            1,
            b'lib/c.py\noutside.txt\n',
            id='mixed',
        ),
        pytest.param(
            b'src/a.py\0.gitattributes\0',
            ['--attributes-ok'],
            0,
            b'',
            id='attributes-admitted',
        ),
        pytest.param(
            b'src/a.py\0.gitattributes\0',
            [],
            1,
            b'.gitattributes\n',
            id='attributes-refused',
        ),
        pytest.param(b'', [], 0, b'', id='empty'),
        pytest.param(
            b'bad\xff.txt\0src/a.py\0',
            [],
            1,
            b'bad\xff.txt\n',
            id='undecodable-name',
        ),
    ],
)
def test_scope_lists_the_out_of_scope_paths(
    repo: dict,
    stdin: bytes,
    flags: list[str],
    returncode: int,
    stdout: bytes,
) -> None:
    """``node _scope`` answers ``merge.sh``'s footprint check by exit code and listing.

    The check reads NUL-separated repo-relative paths and judges them by the
    node's commit boundaries (``task`` is scoped to ``src``): nothing out of
    scope exits 0 with an empty listing, otherwise exit 1 with exactly the
    offending paths, sorted one per line. The worktree-root ``.gitattributes``
    is admitted only under ``--attributes-ok``, which ``merge.sh`` passes when the
    target's staged copy is init's own edit. The listing is raw bytes, so a
    name that is not valid UTF-8 round-trips to git's own bytes instead of
    failing the check.
    """
    task = repo['task']
    result = subprocess.run(
        [_fractal_bin(), 'node', '_scope', f'--path={task}', *flags],
        input=stdin,
        capture_output=True,
        env=_cli_env(),
        timeout=180,
    )
    assert result.returncode == returncode, result.stderr
    assert result.stdout == stdout


def test_scope_refuses_a_non_node_path(tmp_path: pathlib.Path) -> None:
    """``node _scope --path`` at a non-node is a usage error, never an answer.

    ``merge.sh`` reads exit 1 as the check's own answer (paths out of scope) and
    any other non-zero exit as an error, so a path with no node behind it
    must exit 2 -- typer's usage error -- rather than pass or refuse the
    squash.
    """
    result = subprocess.run(
        [_fractal_bin(), 'node', '_scope', f'--path={tmp_path}'],
        input=b'src/a.py\0',
        capture_output=True,
        env=_cli_env(),
        timeout=180,
    )
    assert result.returncode == 2, result.stderr
    assert result.stdout == b''


# ------ list / status


def test_list_filters_by_retired_and_depth(repo: dict) -> None:
    """``list`` honours ``--all``, ``--retired``, ``--max-depth`` and ``--csv``.

    Retires ``docs`` to exercise the filters, then restores it so the
    shared fixture is left as other tests expect.
    """
    root, docs = repo['root'], repo['docs']
    # retire docs so the filters have a retired node to include or exclude
    assert _run(docs, 'node', 'retire').returncode == 0
    # default view hides retired nodes
    default = _run(root, 'node', 'list', '--csv').stdout
    assert 'main.task' in default
    assert 'main.docs' not in default
    # --all includes retired nodes
    all_view = _run(root, 'node', 'list', '--all', '--csv').stdout
    assert 'main.docs' in all_view
    assert 'retired' in all_view
    # --retired shows only retired nodes
    retired_view = _run(root, 'node', 'list', '--retired', '--csv').stdout
    assert 'main.docs' in retired_view
    assert 'main.task' not in retired_view
    # --max-depth=0 lists no descendants (list never includes the node itself;
    # descendants start at relative depth 1, so --max-depth=1 is direct children)
    shallow = _run(root, 'node', 'list', '--max-depth', '0', '--csv').stdout
    assert 'main.task' not in shallow
    # restore the fixture
    assert _run(docs, 'node', 'unretire').returncode == 0


def test_list_json_mirrors_csv_shape(repo: dict) -> None:
    """``list --json`` emits typed row objects with the CSV's column set.

    One object per node, keys in the CSV header's order, so a scripted
    consumer never rebuilds accounting from comma-split text (a comma in
    one node's title would shift every column of a comma-split census).
    ``--json`` and ``--csv`` are mutually exclusive.
    """
    root = repo['root']
    header = _run(root, 'node', 'list', '--csv').stdout.splitlines()[0]
    result = _run(root, 'node', 'list', '--json')
    assert result.returncode == 0
    rows = json.loads(result.stdout)
    assert rows, 'fixture lists at least the two worker nodes'
    assert list(rows[0].keys()) == header.split(',')
    branches = {row['node'] for row in rows}
    assert {'main.task', 'main.docs'} <= branches
    # fields are typed, never re-stringified: numeric caps stay numbers (or
    # null), so a comma or quote in a text field can never shift a column
    for row in rows:
        assert isinstance(row['node'], str)
        for cap in ('max_cost', 'max_depth', 'max_children', 'max_descendants'):
            assert row[cap] is None or isinstance(row[cap], (int, float))
        # end_reason is a closed vocabulary or null, never composed prose
        assert row['end_reason'] in {
            None,
            'goal_met',
            'run_exhausted',
            'final_iteration_failed',
            'cost_budget',
            'timeout',
            'setup_abort',
            'other',
        }
    # the two machine formats cannot be combined ...
    clash = _run(root, 'node', 'list', '--json', '--csv')
    assert clash.returncode != 0
    # ... and neither can a row format and the bare count: honoring one
    # silently would hand a machine consumer a shape it never asked for --
    # the rule covers both row formats, not just JSON
    for row_format in ('--json', '--csv'):
        counted = _run(root, 'node', 'list', row_format, '--count')
        assert counted.returncode != 0, row_format
        assert 'mutually exclusive' in counted.stderr


def test_list_status_count_and_live(repo: dict) -> None:
    """``list`` honours ``--status``, ``--count``, and ``--live``.

    ``--count`` prints just the match count (the loop's child-drain predicate),
    ``--status`` filters to one status, and ``--live`` reports each node's real
    status. Activates ``task`` then restores it to ``idle`` so the shared
    fixture is left as other tests expect.
    """
    _require_tmux()
    root, task = repo['root'], repo['task']
    # --count matches the csv cardinality; the fixture's two workers are a
    # floor, not a total (shared-fixture tests may have added nodes)
    listing = _run(root, 'node', 'list', '--csv').stdout
    count = _run(root, 'node', 'list', '--count').stdout.strip()
    assert 'main.task' in listing
    assert 'main.docs' in listing
    assert int(count) == len(listing.splitlines()) - 1
    assert int(count) >= 2
    # task is not active yet, so the active filter must not list it
    pre_active = _run(root, 'node', 'list', '--status', 'active', '--csv')
    assert 'main.task' not in pre_active.stdout
    # activate task with a real tmux session -- --live is authoritative and
    # relabels an active node with no live session to exited, so the session
    # must exist for --live to report it active (the session name start.sh
    # derives: <repo dirname> (<branch, dots dashed>))
    Node(task).status_set('active')
    session = f'{root.name} (main-task)'
    subprocess.run(['tmux', 'new-session', '-d', '-s', session], check=True)
    try:
        listed = _run(
            root,
            'node',
            'list',
            '--status',
            'active',
            '--live',
            '--csv',
        )
        active = listed.stdout
        assert 'main.task' in active
        assert 'main.docs' not in active
        active_count = _run(
            root,
            'node',
            'list',
            '--status',
            'active',
            '--live',
            '--count',
        )
        assert int(active_count.stdout.strip()) == len(active.splitlines()) - 1
    finally:
        # `=` prefix forces an exact target match (no prefix resolution)
        subprocess.run(
            ['tmux', 'kill-session', '-t', f'={session}'],
            capture_output=True,
        )
    # restore the fixture
    Node(task).status_set('idle')


def test_list_rejects_invalid_filters(repo: dict) -> None:
    """``list`` rejects an empty ``--status`` and a negative ``--max-depth``.

    Both are CLI-layer parameter rejections (typer ``BadParameter``, exit 2):
    an empty status filter matches nothing (a likely typo), and a negative depth
    is invalid -- unbounded is expressed by omitting the flag, not ``-1``,
    consistent with ``node update``'s cap validation.
    """
    root = repo['root']
    # an empty status filter is rejected before it silently matches nothing
    empty_status = _run(root, 'node', 'list', '--status', '')
    assert empty_status.returncode == 2
    assert 'status' in (empty_status.stdout + empty_status.stderr)
    # negative depths are rejected (including -1 -- omit the flag for unbounded)
    for depth in ('-1', '-2'):
        bad_depth = _run(root, 'node', 'list', '--max-depth', depth)
        assert bad_depth.returncode == 2, depth
        assert 'max-depth' in (bad_depth.stdout + bad_depth.stderr)


def test_list_rejects_unknown_status(repo: dict) -> None:
    """``list`` refuses an unknown ``--status`` chunk, naming the valid set.

    Statuses are a closed set, and an unknown filter would return an empty
    listing indistinguishable from no matching nodes (a typo silently reads
    as "nothing to steer"). Each comma-separated chunk validates
    (``BadParameter``, exit 2); every real status -- and the listing's own
    ``orphan`` relabel -- still passes.
    """
    root = repo['root']
    # a bare typo and a typo'd chunk inside a multi-status filter both refuse
    for status in ('bogus', 'active,bogus'):
        rejected = _run(root, 'node', 'list', '--status', status)
        assert rejected.returncode == 2, status
        assert 'bogus' in (rejected.stdout + rejected.stderr)
    # every known status still filters cleanly
    for status in (*STATUSES, 'orphan'):
        listed = _run(root, 'node', 'list', '--status', status, '--csv')
        assert listed.returncode == 0, status


def test_list_whole_tree_from_a_non_init_checkout(repo: dict) -> None:
    """A bare ``list`` anchors on the user node from a non-init checkout.

    On a non-init branch (the user on their own branch while nodes run),
    branch-keyed resolution finds no node and dies on the multi-worktree
    ambiguity -- the whole-tree listing must anchor on the user node by
    config instead of reporting the live fleet as empty (count 0, exit 0).
    Checks out a side branch and restores ``main`` so the shared fixture
    is left as other tests expect.
    """
    root = repo['root']
    # the user checks the repo root out to their own branch while nodes exist
    _git(root, 'checkout', '-b', 'sidework')
    try:
        listing = _run(root, 'node', 'list', '--csv')
        count = _run(root, 'node', 'list', '--count')
    finally:
        # restore the fixture
        _git(root, 'checkout', 'main')
        _git(root, 'branch', '-D', 'sidework')
    # the whole tree lists (never empty while the fleet is registered)
    assert listing.returncode == 0
    assert 'main.task' in listing.stdout
    assert 'main.docs' in listing.stdout
    # --count agrees with the csv cardinality
    assert count.returncode == 0
    assert int(count.stdout.strip()) == len(listing.stdout.splitlines()) - 1


def test_status_reports_idle_from_anywhere(repo: dict) -> None:
    """``status`` reports ``idle`` for a fresh node, by cwd or by branch."""
    from_worktree = _run(repo['task'], 'node', 'status')
    assert from_worktree.returncode == 0
    assert from_worktree.stdout.strip() == 'idle'
    by_branch = _run(repo['root'], 'node', 'status', 'main.task')
    assert by_branch.returncode == 0
    assert by_branch.stdout.strip() == 'idle'


def test_rm_rf_worktree_lists_orphan_then_force_deletes(repo: dict) -> None:
    """A hand-``rm -rf``'d node lists ``orphan`` and ``delete --force`` unwedges it.

    ``rm -rf .worktrees/<node>`` leaves git still listing the worktree (prunable)
    while its directory is gone. Plain ``list`` must flag the node ``orphan``
    rather than a healthy ``idle``, and ``node delete <n> --force`` must succeed
    (its deregister fallback must not trip on the dead worktree path) --
    otherwise the node wedges in a catch-22 where ``--force`` errors "has a
    worktree" while the plain delete exits 2 "No fractal node at". Creates and
    removes its own node, so the shared fixture is left as found.
    """
    root = repo['root']
    spawn = _run(root, 'node', 'init', 'lost', '--agent', 'claude')
    assert spawn.returncode == 0, spawn.stderr
    # rm -rf the worktree dir out of band (git still lists it as prunable)
    shutil.rmtree(root / '.worktrees' / 'main.lost')

    # plain list flags the rm-rf'd node orphan, not a healthy idle
    listing = _run(root, 'node', 'list', '--csv').stdout
    orphan_row = next(line for line in listing.splitlines() if 'main.lost' in line)
    assert 'orphan' in orphan_row

    # --force deregisters the orphan instead of wedging on the dead worktree
    deleted = _run(root, 'node', 'delete', 'main.lost', '--force')
    assert deleted.returncode == 0, deleted.stdout + deleted.stderr
    assert 'main.lost' not in _run(root, 'node', 'list', '--all', '--csv').stdout


def test_list_shows_stored_status_for_orphaned_terminal_node(repo: dict) -> None:
    """Out-of-band git cleanup keeps a terminal node's stored status listable.

    Plain ``list`` must keep the stored terminal status visible and flag the
    orphaning in ``detail``, with the bare ``orphan`` status reserved for
    live-ish rows (active/idle) whose artifacts vanished. Creates and
    removes its own node, so the shared fixture is left as found.
    """
    root = repo['root']
    spawn = _run(root, 'node', 'init', 'done', '--agent', 'claude')
    assert spawn.returncode == 0, spawn.stderr
    # a settled node: mark completed, then clean its artifacts with plain git
    Node(root / '.worktrees' / 'main.done').status_set('completed')
    _git(root, 'worktree', 'remove', '--force', str(root / '.worktrees' / 'main.done'))
    _git(root, 'branch', '-D', 'main.done')

    # the stored terminal status survives in the listing, orphaning marked
    listing = _run(root, 'node', 'list', '--csv').stdout
    row = next(
        entry
        for entry in csv.DictReader(io.StringIO(listing))
        if entry['node'] == 'main.done'
    )
    assert row['status'] == 'completed'
    assert row['detail'] == 'orphaned'

    # cleanup: deregister the orphan row (branch pruning is best-effort)
    deleted = _run(root, 'node', 'delete', 'main.done', '--force')
    assert deleted.returncode == 0, deleted.stdout + deleted.stderr


def test_reconcile_records_orphan_event_once(repo: dict) -> None:
    """``node reconcile`` audits out-of-band removals in the events log, once.

    Plain-git cleanup writes no event row; reconcile records one ``orphan``
    event per removal, keeps the registry row, and is idempotent. Creates
    and removes its own node, so the shared fixture is left as found.
    """
    root = repo['root']
    spawn = _run(root, 'node', 'init', 'ghost', '--agent', 'claude')
    assert spawn.returncode == 0, spawn.stderr
    Node(root / '.worktrees' / 'main.ghost').status_set('completed')
    _git(root, 'worktree', 'remove', '--force', str(root / '.worktrees' / 'main.ghost'))
    _git(root, 'branch', '-D', 'main.ghost')

    # the lesion: the removal left no trace in the events log
    activity = _run(root, 'node', 'activity', '--csv').stdout
    assert not _orphan_activity_rows(activity, 'main.ghost')

    # reconcile records the orphaning and echoes what it recorded
    reconciled = _run(root, 'node', 'reconcile')
    assert reconciled.returncode == 0, reconciled.stdout + reconciled.stderr
    assert 'main.ghost' in reconciled.stdout
    activity = _run(root, 'node', 'activity', '--csv').stdout
    assert len(_orphan_activity_rows(activity, 'main.ghost')) == 1
    # recording is not removal: the row still lists with its stored status
    assert 'main.ghost' in _run(root, 'node', 'list', '--csv').stdout
    # idempotent: a second run finds nothing new to record
    again = _run(root, 'node', 'reconcile')
    assert 'main.ghost' not in again.stdout
    activity = _run(root, 'node', 'activity', '--csv').stdout
    assert len(_orphan_activity_rows(activity, 'main.ghost')) == 1

    # cleanup: deregister the orphan row (branch pruning is best-effort)
    deleted = _run(root, 'node', 'delete', 'main.ghost', '--force')
    assert deleted.returncode == 0, deleted.stdout + deleted.stderr


# ------ diff


def test_diff_reports_drift_per_surface(repo: dict) -> None:
    """``diff`` re-renders the recorded template and reports drift per file.

    A freshly seeded node is clean (exit 0, by branch and from its own
    worktree), with the codex ``auth.json`` symlink in place. An edit to
    each surface -- a step, a script, a skill file, an agent
    ``settings.json``, and the charter -- exits 1 with a unified diff
    naming the node-relative path; a deleted step reports as missing and
    ``{{`` residue is its own finding, while a node-added step, a live
    symlink, and the untouched ``auth.json`` are never reported.
    """
    root = repo['root']
    charter = (
        '# diffcrew\n\n## Instructions\n\nmission: {{mission}}\n\n'
        '## Completion Requirements\n\nDone.\n'
    )
    _commit_template(
        root,
        'templates/diffcrew',
        {
            'config.json': '{}\n',
            'NODE.md': charter,
            'steps/01-PLAN.md': '# plan the work\n',
            'steps/02-EXECUTE.md': '# execute the plan\n',
            'scripts/probe.sh': 'echo probe\n',
            'skills/workflow/SKILL.md': '# workflow\n',
            'agents/claude/settings.json': '{"seeded": true}\n',
            'agents/claude/math.md': 'think in lemmas\n',
        },
    )
    spawn = _run(
        root,
        'node',
        'init',
        'diffcrew',
        '--agent',
        'claude',
        '--template',
        'templates/diffcrew',
        '--set',
        'mission=probe the surfaces',
    )
    assert spawn.returncode == 0, spawn.stderr
    try:
        worktree = root / '.worktrees' / 'main.diffcrew'
        node_dir = worktree / '.fractal' / 'main.diffcrew'
        # a fresh seed is clean, by branch and from the node's own worktree
        clean = _run(root, 'node', 'diff', 'main.diffcrew')
        assert clean.returncode == 0, clean.stdout + clean.stderr
        assert clean.stdout.strip() == 'No drift from the recorded template.'
        assert _run(worktree, 'node', 'diff').returncode == 0
        assert (node_dir / '.codex' / 'auth.json').is_symlink()
        # drift every surface: edit a step, drop a step, add a node-own
        # step, edit a script, a skill file, and the agent settings, park
        # unrendered residue in the charter, and turn a live agent file
        # into a symlink
        step = node_dir / 'steps' / '01-PLAN.md'
        step.write_text('# plan the work\nan added line\n', encoding='utf-8')
        (node_dir / 'steps' / '02-EXECUTE.md').unlink()
        added = node_dir / 'steps' / '03-NEW.md'
        added.write_text('# node-added\n', encoding='utf-8')
        script = node_dir / 'scripts' / 'probe.sh'
        script.write_text('echo probed\n', encoding='utf-8')
        skill = node_dir / 'skills' / 'workflow' / 'SKILL.md'
        skill.write_text('# workflow, reworked\n', encoding='utf-8')
        settings = node_dir / '.claude' / 'settings.json'
        settings.write_text('{"seeded": false}\n', encoding='utf-8')
        math = node_dir / '.claude' / 'math.md'
        math.unlink()
        math.symlink_to(node_dir / 'NODE.md')
        (node_dir / 'NODE.md').write_text(charter, encoding='utf-8')
        drifted = _run(root, 'node', 'diff', 'main.diffcrew')
        assert drifted.returncode == 1, drifted.stdout + drifted.stderr
        # one unified diff per drifted file, headed by the node-relative path
        for header in (
            'NODE.md',
            'steps/01-PLAN.md',
            'scripts/probe.sh',
            'skills/workflow/SKILL.md',
            '.claude/settings.json',
        ):
            assert f'--- template/{header}' in drifted.stdout
            assert f'+++ node/{header}' in drifted.stdout
        assert '+an added line' in drifted.stdout
        assert 'steps/02-EXECUTE.md: missing from the node.' in drifted.stdout
        assert 'NODE.md: unrendered {{ residue in the live copy.' in drifted.stdout
        # never reported: the node's own additions and the live symlinks
        assert '03-NEW.md' not in drifted.stdout
        assert 'math.md' not in drifted.stdout
        assert 'auth.json' not in drifted.stdout
    finally:
        _run(root, 'node', 'delete', 'main.diffcrew', '--force')


def test_diff_applies_the_recorded_listing_and_warns_on_a_stale_entry(
    repo: dict,
) -> None:
    """The recorded ``exclude`` shapes the diff; a stale entry only warns.

    An excluded step is absent from the node by design, so it never
    reports as missing; an entry matching nothing in the template draws
    a warning on stderr, not a refusal, and the node still diffs clean.
    """
    root = repo['root']
    _commit_template(
        root,
        'templates/leandiff',
        {
            'config.json': '{}\n',
            'steps/01-GO.md': '# go\n',
            'steps/02-SKIP.md': '# never deployed\n',
        },
    )
    spawn = _run(
        root,
        'node',
        'init',
        'leandiff',
        '--agent',
        'claude',
        '--template',
        'templates/leandiff',
        '--exclude',
        'steps/02-SKIP.md',
    )
    assert spawn.returncode == 0, spawn.stderr
    try:
        node_dir = root / '.worktrees' / 'main.leandiff' / '.fractal' / 'main.leandiff'
        assert not (node_dir / 'steps' / '02-SKIP.md').exists()
        clean = _run(root, 'node', 'diff', 'main.leandiff')
        assert clean.returncode == 0, clean.stdout + clean.stderr
        # a listing entry the template no longer carries warns, not refuses
        record = node_dir / '_template.toml'
        data = tomllib.loads(record.read_text(encoding='utf-8'))
        data['exclude'].append('steps/99-GONE.md')
        record.write_text(tomli_w.dumps(data), encoding='utf-8')
        warned = _run(root, 'node', 'diff', 'main.leandiff')
        assert warned.returncode == 0, warned.stdout + warned.stderr
        assert 'No drift from the recorded template.' in warned.stdout
        expected = (
            'Warning: exclude entry matches nothing in the template:'
            " 'steps/99-GONE.md' (the template may have dropped it)."
        )
        assert expected in warned.stderr
    finally:
        _run(root, 'node', 'delete', 'main.leandiff', '--force')


def test_diff_requires_a_recorded_template_and_a_full_sha(repo: dict) -> None:
    """A recordless node and a hand-mangled commit are command errors.

    A node seeded without ``--template`` has nothing to diff against --
    exit 2, naming the missing record -- and a ``_template.toml`` whose
    commit is not a full 40-hex sha refuses the same way.
    """
    root = repo['root']
    # the long-lived task worker was seeded without a template
    recordless = _run(root, 'node', 'diff', 'main.task')
    assert recordless.returncode == 2, recordless.stdout + recordless.stderr
    assert 'No template recorded' in recordless.stderr
    _commit_template(
        root,
        'templates/shadiff',
        {'config.json': '{}\n', 'steps/01-GO.md': '# go\n'},
    )
    spawn = _run(
        root,
        'node',
        'init',
        'shadiff',
        '--agent',
        'claude',
        '--template',
        'templates/shadiff',
    )
    assert spawn.returncode == 0, spawn.stderr
    try:
        node_dir = root / '.worktrees' / 'main.shadiff' / '.fractal' / 'main.shadiff'
        record = node_dir / '_template.toml'
        data = tomllib.loads(record.read_text(encoding='utf-8'))
        data['commit'] = 'deadbeef'
        record.write_text(tomli_w.dumps(data), encoding='utf-8')
        mangled = _run(root, 'node', 'diff', 'main.shadiff')
        assert mangled.returncode == 2, mangled.stdout + mangled.stderr
        assert "commit is not a full 40-hex sha: 'deadbeef'" in mangled.stderr
    finally:
        _run(root, 'node', 'delete', 'main.shadiff', '--force')


def test_diff_stays_clean_after_the_template_folder_moves(repo: dict) -> None:
    """A template moved on a later commit still diffs clean at its recorded sha.

    The recorded commit keeps the folder's objects reachable, so the
    read is untouched by the rename and the node reports no drift.
    """
    root = repo['root']
    _commit_template(
        root,
        'templates/movediff',
        {'config.json': '{}\n', 'steps/01-GO.md': '# go\n'},
    )
    spawn = _run(
        root,
        'node',
        'init',
        'movediff',
        '--agent',
        'claude',
        '--template',
        'templates/movediff',
    )
    assert spawn.returncode == 0, spawn.stderr
    try:
        _git(root, 'mv', 'templates/movediff', 'templates/movediff2')
        _git(root, 'commit', '-m', 'move the template folder')
        moved = _run(root, 'node', 'diff', 'main.movediff')
        assert moved.returncode == 0, moved.stdout + moved.stderr
        assert 'No drift from the recorded template.' in moved.stdout
    finally:
        _run(root, 'node', 'delete', 'main.movediff', '--force')


# ------ lifecycle guards (idle node)


@pytest.mark.parametrize(
    argnames=('command', 'message'),
    argvalues=[
        # kill is absent by design: an idle node is killable (a spawn is
        # reapable before it activates), covered in test_core/test_lifecycle
        ('finish', 'Cannot finish: node is not active.'),
        ('stop', 'Cannot stop: node is not active.'),
        ('attach', 'Cannot attach: node is not active.'),
        ('unretire', 'Cannot unretire: node is not retired.'),
    ],
)
def test_lifecycle_guard_rejects_idle_node(
    repo: dict,
    command: str,
    message: str,
) -> None:
    """Signal/attach/unretire on an idle node fail with a clear error.

    Each guard must reject the wrong-state call (a ``RuntimeError`` from
    the core), exit non-zero, and report the reason on stderr so a script
    can surface it.
    """
    result = _run(repo['task'], 'node', command)
    assert result.returncode == 2
    assert message in result.stderr
    assert result.stdout.strip() == ''


def test_start_drain_requires_continue(repo: dict) -> None:
    """The CLI refuses ``--drain`` without ``--continue`` rather than no-op.

    ``--drain`` only means anything on a continued run; accepting it on a
    fresh start would let an operator believe a wind-down was armed while
    the node spawns freely.
    """
    task = repo['task']
    refused = _run(task, 'node', 'start', '--drain')
    assert refused.returncode != 0
    assert '--drain requires --continue' in refused.stderr


def test_retire_unretire_round_trips_through_list(repo: dict) -> None:
    """An idle node can retire and unretire, toggling its list visibility."""
    root, docs = repo['root'], repo['docs']
    # retire is allowed from idle and confirms
    retired = _run(docs, 'node', 'retire', '--reason', 'superseded by rework')
    assert retired.returncode == 0
    assert 'retire' in retired.stdout.lower()
    assert 'main.docs' not in _run(root, 'node', 'list', '--csv').stdout
    # the reason rides the retire event metadata after the prior status
    activity = _run(docs, 'node', 'activity', '--csv').stdout
    assert 'idle: superseded by rework' in activity
    # unretire restores visibility and confirms
    restored = _run(docs, 'node', 'unretire')
    assert restored.returncode == 0
    assert 'unretire' in restored.stdout.lower()
    assert 'main.docs' in _run(root, 'node', 'list', '--csv').stdout


def test_retire_refuses_a_retired_node_and_unretire_restores_its_status(
    repo: dict,
) -> None:
    """A second ``retire`` is refused, so ``unretire`` restores the settled status.

    Retired accepts only ``unretire`` and ``delete``: a retire that went
    through on a retired node would record ``retired`` as the pre-retire
    status, and the next ``unretire`` would fall back to ``idle`` instead of
    the ``completed`` the node held. The refusal exits non-zero with the
    reason, and the first retire's record stays the one ``unretire`` reads.
    """
    root = repo['root']
    init = _run(root, 'node', 'init', 'settled', '--agent', 'claude')
    assert init.returncode == 0, init.stderr
    settled = root / '.worktrees' / 'main.settled'
    # a settled node, as the loop leaves it
    Node(settled).status_set('completed')
    assert _run(settled, 'node', 'retire').returncode == 0
    again = _run(settled, 'node', 'retire')
    assert again.returncode == 2
    assert 'Cannot retire: node is already retired.' in again.stderr
    assert again.stdout.strip() == ''
    # unretire lands back on the settled status, in the file and the listing
    restored = _run(settled, 'node', 'unretire')
    assert restored.returncode == 0, restored.stderr
    assert _run(settled, 'node', 'status').stdout.strip() == 'completed'
    rows = json.loads(_run(root, 'node', 'list', '--all', '--json').stdout)
    statuses = {row['node']: row['status'] for row in rows}
    assert statuses['main.settled'] == 'completed'
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.settled', '--force').returncode == 0


# ------ update (child config)


def test_update_rewrites_child_config(repo: dict) -> None:
    """``update`` rewrites a child's caps and confirms each change old -> new.

    The user node is the parent of each worker, so ``update main.task`` from
    the repo root edits the ``task`` worker's configuration. A mid-run retune
    is confirmed per changed key -- a silent success is indistinguishable from
    a dropped one.
    """
    root, task = repo['root'], repo['task']
    # the stored caps before the rewrite anchor the confirmation echo
    prior_cost = _config(task, 'max_cost')
    prior_depth = _config(task, 'max_depth')
    prior_children = _config(task, 'max_children')
    # parent rewrites the task worker's caps
    updated = _run(
        root,
        'node',
        'update',
        'main.task',
        '--max-cost',
        '9.0',
        '--max-depth',
        '4',
        '--max-children',
        '7',
    )
    assert updated.returncode == 0, updated.stderr
    # each provided key is confirmed old -> new; untouched keys are not echoed
    assert f'max_cost: {prior_cost} -> 9.0' in updated.stdout
    assert f'max_depth: {prior_depth} -> 4' in updated.stdout
    assert f'max_children: {prior_children} -> 7' in updated.stdout
    assert 'max_descendants' not in updated.stdout
    # child config.json is rewritten
    assert _config(task, 'max_cost') == '9.0'
    assert _config(task, 'max_depth') == '4'
    assert _config(task, 'max_children') == '7'
    # parent's nodes table reflects the new caps
    listing = _run(root, 'node', 'list', '--csv').stdout
    assert '9.0' in listing


def test_update_changes_title(repo: dict) -> None:
    """``update --title`` rewrites a child's display name in config and the table."""
    root, docs = repo['root'], repo['docs']
    prior_title = _config(docs, 'title') or 'unset'
    updated = _run(root, 'node', 'update', 'main.docs', '--title', 'Documentation')
    assert updated.returncode == 0, updated.stderr
    # the rename is confirmed old -> new like the cap updates
    assert f'title: {prior_title} -> Documentation' in updated.stdout
    assert _config(docs, 'title') == 'Documentation'
    listing = _run(root, 'node', 'list', '--csv').stdout
    assert 'Documentation' in listing


def test_update_rejects_unknown_child_and_negatives(repo: dict) -> None:
    """``update`` errors on an unknown target and on negative caps.

    An unknown target is rejected by ``resolve_target`` (typer ``BadParameter``,
    exit 2) like every other node command; a negative cap is a parameter
    rejection from the CLI layer (typer ``BadParameter``, exit 2).
    """
    root = repo['root']
    unknown = _run(root, 'node', 'update', 'main.nope', '--max-cost', '1.0')
    assert unknown.returncode == 2
    assert 'no node found' in unknown.stderr.lower()
    negative = _run(root, 'node', 'update', 'main.task', '--max-cost', '-1')
    assert negative.returncode == 2
    assert 'max-cost' in (negative.stdout + negative.stderr)


def test_update_max_cost_retunes_default_reserve(repo: dict) -> None:
    """``update --max-cost`` keeps a default-mode reserve at 10% of the cap.

    Init materializes the default reserve once (10% of the initial cap); an
    ``update --max-cost`` that ignored it would leave a reserve sized for the
    old cap, with lowering the cap under a stale reserve rejected outright.
    """
    root = repo['root']
    spawn = _run(
        root,
        'node',
        'init',
        'retuned',
        '--agent',
        'claude',
        '--max-cost',
        '4',
    )
    assert spawn.returncode == 0, spawn.stderr
    node = root / '.worktrees' / 'main.retuned'
    assert _config(node, 'reserve_budget') == '0.4'
    try:
        # a default-mode reserve tracks the retuned cap
        updated = _run(root, 'node', 'update', 'main.retuned', '--max-cost', '8')
        assert updated.returncode == 0, updated.stderr
        assert _config(node, 'reserve_budget') == '0.8'
        # an explicit reserve is settable directly
        explicit = _run(
            root,
            'node',
            'update',
            'main.retuned',
            '--reserve-budget',
            '1.2',
        )
        assert explicit.returncode == 0, explicit.stderr
        assert _config(node, 'reserve_budget') == '1.2'
    finally:
        # clean up so the shared module fixture is left as other tests expect
        assert _run(root, 'node', 'delete', 'main.retuned', '--force').returncode == 0


def test_update_resolves_short_name(repo: dict) -> None:
    """``update`` resolves a unique short name tree-wide, like every other command.

    The parent is derived from the resolved full branch, so a grandchild's bare
    leaf edits the right node from the root, not only a direct child of the
    caller.
    """
    root, task = repo['root'], repo['task']
    node_dir = task / '.fractal' / 'main.task'
    # spawn a grandchild so the leaf's parent is not the caller (the user node);
    # task carries a max_cost, so the child must set one within the parent's cap
    spawn = _run(
        task,
        'node',
        'init',
        'sub',
        '--agent',
        'claude',
        '--max-cost',
        '1.0',
        _NODE=str(node_dir),
    )
    assert spawn.returncode == 0, spawn.stderr
    sub = root / '.worktrees' / 'main.task.sub'
    # from the root, the bare leaf 'sub' resolves tree-wide to main.task.sub
    updated = _run(root, 'node', 'update', 'sub', '--title', 'Grandchild')
    assert updated.returncode == 0, updated.stderr
    assert _config(sub, 'title') == 'Grandchild'
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.task.sub', '--force').returncode == 0


def test_update_validates_config_like_init(repo: dict) -> None:
    """``update`` rejects caps ``init``/``config _set`` would reject.

    ``max_cost`` must be positive, and an update may not break the
    step<=iter<=run ordering from either side -- all caught before any
    write, mirroring the init-time invariants.
    """
    root = repo['root']
    # a throwaway worker with a known max_iter_cost so the ordering check has teeth
    spawn = _run(
        root,
        'node',
        'init',
        'capped',
        '--agent',
        'claude',
        '--max-cost',
        '10',
        '--max-iter-cost',
        '8',
    )
    assert spawn.returncode == 0, spawn.stderr
    capped = root / '.worktrees' / 'main.capped'
    # max_cost=0 is rejected (a $0 ceiling degenerates the subtree check);
    # the invariant layer lives in core (exit 2), not the CLI boundary
    zero = _run(root, 'node', 'update', 'main.capped', '--max-cost', '0')
    assert zero.returncode == 2
    assert 'max_cost' in (zero.stdout + zero.stderr)
    # lowering max_cost below the stored max_iter_cost inverts the ordering
    inverted = _run(root, 'node', 'update', 'main.capped', '--max-cost', '5')
    assert inverted.returncode == 2
    assert 'max_iter_cost' in (inverted.stdout + inverted.stderr)
    # raising a per-iter cap above the effective max_cost is the same
    # inversion from the other side
    iter_over = _run(root, 'node', 'update', 'main.capped', '--max-iter-cost', '15')
    assert iter_over.returncode == 2
    assert 'max_iter_cost' in (iter_over.stdout + iter_over.stderr)
    # a step cap above the stored max_iter_cost breaks step <= iter
    step_over = _run(root, 'node', 'update', 'main.capped', '--max-step-cost', '9')
    assert step_over.returncode == 2
    assert 'max_step_cost' in (step_over.stdout + step_over.stderr)
    # the rejected updates never touched the stored config
    assert _config(capped, 'max_cost') == '10.0'
    assert _config(capped, 'max_iter_cost') == '8.0'
    # a valid update still lands
    ok = _run(root, 'node', 'update', 'main.capped', '--max-cost', '12')
    assert ok.returncode == 0, ok.stderr
    assert _config(capped, 'max_cost') == '12.0'
    # clean up so the shared module fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.capped', '--force').returncode == 0


def test_update_retunes_iter_and_step_cost(repo: dict) -> None:
    """``update`` retunes the per-iteration and per-step cost caps.

    The loop re-reads the full cost-key surface at each iteration boundary,
    so the caps are retunable mid-run. Both are config-only keys like the
    reserve -- the ``nodes`` table has no columns for them.
    """
    root = repo['root']
    # a throwaway worker so the shared fixture workers keep their caps
    spawn = _run(
        root,
        'node',
        'init',
        'tuned',
        '--agent',
        'claude',
        '--max-cost',
        '10',
    )
    assert spawn.returncode == 0, spawn.stderr
    tuned = root / '.worktrees' / 'main.tuned'
    try:
        # both caps land together, each confirmed unset -> new
        updated = _run(
            root,
            'node',
            'update',
            'main.tuned',
            '--max-iter-cost',
            '3',
            '--max-step-cost',
            '1',
        )
        assert updated.returncode == 0, updated.stderr
        assert 'max_iter_cost: unset -> 3.0' in updated.stdout
        assert 'max_step_cost: unset -> 1.0' in updated.stdout
        assert _config(tuned, 'max_iter_cost') == '3.0'
        assert _config(tuned, 'max_step_cost') == '1.0'
        # a later single-flag retune is confirmed against the stored cap
        lowered = _run(root, 'node', 'update', 'main.tuned', '--max-iter-cost', '2')
        assert lowered.returncode == 0, lowered.stderr
        assert 'max_iter_cost: 3.0 -> 2.0' in lowered.stdout
        assert _config(tuned, 'max_iter_cost') == '2.0'
    finally:
        # clean up so the shared module fixture is left as other tests expect
        assert _run(root, 'node', 'delete', 'main.tuned', '--force').returncode == 0


@pytest.mark.parametrize(
    argnames=('flag', 'key'),
    argvalues=[
        ('--max-iter-cost', 'max_iter_cost'),
        ('--max-step-cost', 'max_step_cost'),
    ],
)
def test_update_rejects_iter_cost_on_uncapped_child(
    repo: dict,
    flag: str,
    key: str,
) -> None:
    """A per-iteration/step cap on an uncapped child is rejected at update.

    Mirrors init's guard: ``docs`` carries no ``max_cost``, so granting it a
    per-iter/step cap alone would be unenforceable once the per-iter budget
    drains. The rejection comes from core's retune policy (exit 2), names
    ``--max-cost``, and writes nothing.
    """
    root, docs = repo['root'], repo['docs']
    rejected = _run(root, 'node', 'update', 'main.docs', flag, '5')
    assert rejected.returncode == 2, rejected.stderr
    assert '--max-cost' in (rejected.stdout + rejected.stderr)
    assert _config(docs, key) == ''


# ------ cost


def test_cost_breakdown_rows_sum_to_spent_with_a_deleted_descendant(
    repo: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cost breakdown`` rows total ``cost spent`` even after a descendant delete.

    A descendant whose registry row is gone but whose spend still chains via
    ``parent_run_id`` counts in ``cost spent`` (the lineage). Driving the table
    from the registry alone would drop its line item and under-sum the rows; the
    lineage-driven table appends it as a `` (deleted)`` row instead. Seeds the
    linked parent/child runs through the core API (the loop is what links a child
    run to the active parent run, impractical to stage over the bare CLI), then
    asserts the observable CLI output. Cleans up its own parent node.
    """
    root = repo['root']
    # build a parent and a child under it, each an active node with a started run
    parent_node = Node(root)
    parent_node.init(name='bp', agent='claude')
    parent_wt = root / '.worktrees' / 'main.bp'
    node_dir = parent_wt / '.fractal' / 'main.bp'
    monkeypatch.setenv('_NODE', f'{node_dir}')
    Node(root).init(name='kid', agent='claude')
    monkeypatch.delenv('_NODE')
    child_wt = parent_wt.parent / 'main.bp.kid'
    parent, child = Node(parent_wt), Node(child_wt)
    parent.status_set('active')
    p_run = parent.record.run_start()
    child.status_set('active')
    # the child run links to the parent's active run via the central DB
    child_run = child.record.run_start()
    # record spend on both, then delete the child (its spend must survive)
    _record_step_cost(parent, run_id=p_run, cost=0.5)
    _record_step_cost(child, run_id=child_run, cost=1.5)
    child.status_set('completed')
    child.delete()

    # the breakdown rows (self + the deleted descendant) total cost spent
    breakdown = _run(
        parent_wt,
        'node',
        'cost',
        'breakdown',
        '--run',
        str(p_run),
        '--csv',
    )
    assert breakdown.returncode == 0, breakdown.stderr
    # drop the header
    rows = breakdown.stdout.strip().splitlines()[1:]
    spends = [float(row.rsplit(',', 1)[1]) for row in rows]
    spent = _run(parent_wt, 'node', 'cost', 'spent', '--run', str(p_run))
    total = float(spent.stdout.strip().removeprefix('$'))
    assert sum(spends) == pytest.approx(total)
    # the gone descendant is line-itemed, marked deleted
    assert any('main.bp.kid (deleted)' in row for row in rows)
    # clean up the parent so the shared fixture is left as other tests expect
    assert _run(root, 'node', 'delete', 'main.bp', '--force').returncode == 0


def test_cost_family_answers_for_a_deleted_target(repo: dict) -> None:
    """The ``cost`` family reads history for a deleted node instead of erroring.

    Deleting a node clears its registry rows, but its runs/steps history
    persists -- and grading reads costs after pruning (metrics.md), so
    ``cost spent``/``breakdown``/``remaining <branch>`` must answer from
    history rather than dying at worktree resolution, for the full branch
    and for the same unique short names live resolution accepts.
    ``remaining`` reports ``no budget`` because both cap stores (config
    file and registry row) die with the node. Cleans up its own worker
    node.
    """
    root = repo['root']
    # build a worker with a cap, record one run's spend, then delete it
    Node(root).init(name='gone', agent='claude')
    capped = _run(root, 'node', 'update', 'main.gone', '--max-cost', '3')
    assert capped.returncode == 0, capped.stderr
    gone = Node(root / '.worktrees' / 'main.gone')
    gone.status_set('active')
    run_id = gone.record.run_start()
    _record_step_cost(gone, run_id=run_id, cost=2.5)
    gone.status_set('completed')
    assert _run(root, 'node', 'delete', 'main.gone', '--force').returncode == 0
    # spent answers from history -- the latest recorded run by default
    spent = _run(root, 'node', 'cost', 'spent', 'main.gone')
    assert spent.returncode == 0, spent.stderr
    assert spent.stdout.strip() == '$2.5000'
    # an explicit --run scopes through the same history
    scoped = _run(root, 'node', 'cost', 'spent', 'main.gone', '--run', str(run_id))
    assert scoped.stdout.strip() == '$2.5000'
    # breakdown leads with the deleted target's own row and sums to spent
    breakdown = _run(root, 'node', 'cost', 'breakdown', 'main.gone', '--csv')
    assert breakdown.returncode == 0, breakdown.stderr
    # drop the header
    rows = breakdown.stdout.strip().splitlines()[1:]
    assert rows[0].startswith('main.gone (deleted),')
    spends = [float(row.rsplit(',', 1)[1]) for row in rows]
    assert sum(spends) == pytest.approx(2.5)
    # remaining reports no budget -- the cap died with the node's config
    remaining = _run(root, 'node', 'cost', 'remaining', 'main.gone')
    assert remaining.returncode == 0, remaining.stderr
    assert remaining.stdout.strip() == 'no budget'
    # a short name answers through the same history: the registry-side
    # expansion dies with the registry row, so the trailing segment
    # resolves against the recorded runs instead
    short = _run(root, 'node', 'cost', 'spent', 'gone')
    assert short.returncode == 0, short.stderr
    assert short.stdout.strip() == '$2.5000'
    short_remaining = _run(root, 'node', 'cost', 'remaining', 'gone')
    assert short_remaining.returncode == 0, short_remaining.stderr
    assert short_remaining.stdout.strip() == 'no budget'
    # a branch with no recorded run keeps the not-found error (typo
    # guard), full name or short
    for name in ('main.nonesuch', 'nonesuch'):
        missing = _run(root, 'node', 'cost', 'spent', name)
        assert missing.returncode == 2, name
        assert 'No node found' in (missing.stdout + missing.stderr)


def test_cost_family_refuses_a_live_ambiguous_short_name(
    repo: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short name matching two live nodes refuses -- history never answers.

    The deleted-branch fallback answers when the registry rows died with
    the node; two LIVE same-leaf twins are the registry's own ambiguity
    refusal, and one-sided run history must not silently pick a twin --
    the ledger would attribute the wrong node's spend. Cleans up its own
    parent nodes.
    """
    root = repo['root']
    # two live parents, each with a same-leaf child; only one child runs
    for parent in ('t1', 't2'):
        Node(root).init(name=parent, agent='claude')
        parent_wt = root / '.worktrees' / f'main.{parent}'
        node_dir = parent_wt / '.fractal' / f'main.{parent}'
        monkeypatch.setenv('_NODE', f'{node_dir}')
        Node(root).init(name='same', agent='claude')
        monkeypatch.delenv('_NODE')
    twin = Node(root / '.worktrees' / 'main.t1.same')
    run_id = twin.record.run_start()
    _record_step_cost(twin, run_id=run_id, cost=1.25)
    # the short name refuses as ambiguous despite the one-sided history
    for verb in ('spent', 'remaining'):
        result = _run(root, 'node', 'cost', verb, 'same')
        assert result.returncode == 2, verb
        assert 'Ambiguous node name' in (result.stdout + result.stderr)
    # the full branch still answers
    full = _run(root, 'node', 'cost', 'spent', 'main.t1.same')
    assert full.returncode == 0, full.stderr
    assert full.stdout.strip() == '$1.2500'
    # clean up so the shared fixture is left as other tests expect
    for parent in ('main.t1', 'main.t2'):
        assert _run(root, 'node', 'delete', parent, '--force').returncode == 0


# ------ top-level


def test_version_flag_reports_a_version(repo: dict) -> None:
    """``fractal --version`` prints the package's own version and exits 0.

    The first-install smoke test for a distributed CLI: an eager root option,
    so it resolves before any command and works with no node present. The
    subprocess imports this worktree's package, so the output must equal its
    ``__version__`` -- the code that runs, not install-time dist-info.
    """
    result = _run(repo['root'], '--version')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == fractal.__version__


@pytest.mark.parametrize(
    argnames='argv',
    argvalues=[
        ('node', 'list', '--help'),
        ('node', 'activity', '--help'),
        ('node', 'cost', 'breakdown', '--help'),
    ],
)
def test_table_commands_document_piped_csv_default(repo: dict, argv: tuple) -> None:
    """The table commands' ``--csv`` help notes CSV is the piped default.

    ``print_rows`` already emits CSV on a non-TTY, so ``--csv`` only forces it
    when attached to a terminal; the help must say so (matching the radio
    commands) rather than imply ``--csv`` is required to get CSV when piped.
    """
    # typer wraps help across lines, so join before matching the phrase
    helptext = ' '.join(_run(repo['root'], *argv).stdout.split())
    assert 'piped' in helptext


def test_commit_help_states_message_requirement(repo: dict) -> None:
    """``fractal commit --help`` documents MESSAGE as required unless --check.

    Every committing path errors without a MESSAGE -- it is optional only
    for ``--check`` -- so help rendering it as a plain optional argument
    would misstate the contract.
    """
    # typer wraps help across lines inside panel borders, so strip the borders
    # and join before matching the phrase
    stdout = _run(repo['root'], 'commit', '--help').stdout
    helptext = ' '.join(stdout.replace('│', ' ').split())
    assert 'required unless --check' in helptext
    # the runtime side of the documented contract: no message, no --check
    result = _run(repo['task'], 'commit')
    assert result.returncode != 0
    assert 'Message is required unless --check' in result.stderr


# ------ machine output


def test_list_pipe_status_has_no_brackets(repo: dict) -> None:
    """Piped (non-csv) status should not be bracketed for parsing."""
    result = _run(repo['root'], 'node', 'list')
    assert '[idle]' not in result.stdout


def test_list_csv_columns_stable_empty_vs_populated(repo: dict) -> None:
    """``list --csv`` emits a stable, curated header whether or not rows match.

    The listing is projected to a fixed column set, so a script parsing the CSV
    sees the same header for a populated and an empty result (no header drift),
    the run-config caps it needs are present, and internal storage columns never
    leak.
    """
    root = repo['root']
    # populated (the fixture's worker nodes) and empty (a valid filter no
    # node ever matches -- 'failed' is entity-row only, never a node status)
    populated = _run(root, 'node', 'list', '--all', '--csv').stdout
    empty = _run(root, 'node', 'list', '--status', 'failed', '--csv').stdout
    # the header does not drift between a populated and an empty result
    header = populated.splitlines()[0]
    assert header == empty.splitlines()[0]
    # caps a script needs are shown; internal columns stay hidden
    assert 'max_cost' in header
    assert 'title' in header
    assert 'node_id' not in header


def test_activity_json_mirrors_csv_shape(repo: dict) -> None:
    """``activity --json`` emits an array of CSV-shaped row objects.

    The JSON surface is additive -- CSV stays the piped default -- and
    mirrors the CSV projection: one object per row, keys in the CSV
    header's column order. ``--json`` and ``--csv`` contradict and are
    refused.
    """
    root = repo['root']
    header = _run(root, 'node', 'activity', '--csv').stdout.splitlines()[0].split(',')
    listed = _run(root, 'node', 'activity', '--json')
    assert listed.returncode == 0, listed.stderr
    rows = json.loads(listed.stdout)
    assert rows, 'the fixture spawns recorded activity'
    assert all(list(row) == header for row in rows)
    # values round-trip: the fixture's worker inits recorded spawn events
    assert any(row['event'] == 'spawn' for row in rows)
    # --json contradicts --csv
    clash = _run(root, 'node', 'activity', '--json', '--csv')
    assert clash.returncode != 0
    assert 'mutually exclusive' in clash.stderr.lower()


def test_activity_names_attribution_and_lineage_columns(repo: dict) -> None:
    """``activity --csv`` renders ``actor`` and the display numbers.

    Consumers bind by header name, so the header names every projected
    column; an event row carries its writer and a step row its name, the
    iteration-relative step number, and the run-relative iteration number
    the surrogate lineage ids stand for.
    """
    root = repo['root']
    # seed one settled lineage on the root node so a step row exists to
    # read (closed at every level, so no active run leaks to other tests)
    node = Node(root)
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=3)
    step_id = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=2,
        step_name='PLAN',
    )
    node.record.step_end(step_id=step_id, status='completed', exit_code=0)
    node.record.iter_end(iter_id=iter_id, status='completed', exit_code=0)
    node.record.run_end(run_id=run_id, status='completed', exit_code=0)

    activity = _run(root, 'node', 'activity', '--csv').stdout
    # the header names the full projection, in its documented order
    header = activity.splitlines()[0].split(',')
    assert header == [
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
    rows = list(csv.DictReader(io.StringIO(activity)))
    # an event row names its writer -- the fixture's spawns are operator-made
    spawn = next(row for row in rows if row['event'] == 'spawn')
    assert spawn['actor'] == 'operator'
    # a step row renders its name and the run-relative numbers
    step = next(row for row in rows if row['step_id'])
    assert (step['step_name'], step['step'], step['iter']) == ('PLAN', '2', '3')


# ------ chat


def test_chat_requires_a_prompt(repo: dict) -> None:
    """``node chat`` with no prompt argument is refused (no spawn)."""
    result = _run(repo['root'], 'node', 'chat')
    assert result.returncode != 0
    assert 'prompt' in result.stderr.lower()


def test_chat_rejects_codex_fork(repo: dict) -> None:
    """Forking a codex session is refused -- codex ``exec`` cannot fork.

    ``docs`` is a codex node; ``--session`` without ``--resume`` requests a fork,
    which errors before any agent is spawned.
    """
    result = _run(repo['root'], 'node', 'chat', 'docs', 'hello', '--session', 'x')
    assert result.returncode != 0
    assert 'codex' in result.stderr.lower()


def test_chat_current_requires_a_live_session(repo: dict) -> None:
    """``--current`` is refused when the node has no live loop session (no spawn)."""
    # task is an idle node -- no running loop, so no session to fork
    result = _run(repo['root'], 'node', 'chat', 'task', 'hello', '--current')
    assert result.returncode != 0
    assert 'live session' in result.stderr.lower()


# ------ helpers


def _config(cwd: pathlib.Path, key: str) -> str:
    """Return a node's persisted config value via ``config _get``."""
    return _run(cwd, 'config', '_get', key).stdout.strip()


def _orphan_activity_rows(activity: str, branch: str) -> list[str]:
    """Activity CSV lines recording ``branch``'s orphan event."""
    lines = activity.splitlines()
    return [line for line in lines if 'orphan' in line and branch in line]


def _commit_template(
    root: pathlib.Path,
    path: str,
    files: dict[str, str],
) -> None:
    """Write ``files`` under ``root/<path>`` and commit the folder.

    Template bytes deploy from git, never from the working copy, so every
    template fixture must be committed before the init that reads it.
    """
    for name, content in files.items():
        target = root / path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
    _git(root, 'add', path)
    _git(root, 'commit', '-m', f'template {path}')


def _init_tree(
    root: pathlib.Path,
    identity: str,
    *,
    projects: tuple[str, ...] = (),
) -> None:
    """Build a repo with committed project wikis and run ``fractal init`` in it."""
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', f'{identity}@test.local')
    _git(root, 'config', 'user.name', identity)
    (root / 'README.md').write_text(f'# {identity}\n', encoding='utf-8')
    # a project wiki is required for scoped/based node init; a sub-project's
    # wiki backs its child's precondition lookup
    for wiki in (root / 'wiki', *(root / project / 'wiki' for project in projects)):
        wiki.mkdir(parents=True)
        (wiki / '_index.md').write_text(
            '---\nname: wiki\n---\n# wiki\n\n***\n',
            encoding='utf-8',
        )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    # fractal init creates the user node, so worker init then passes
    assert _run(root, 'init').returncode == 0


def _record_step_cost(node: Node, *, run_id: int, cost: float) -> None:
    """Record one completed step of ``cost`` USD in ``run_id`` (for cost rollups)."""
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    step_id = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.record.step_cost(step_id=step_id, cost=cost)
    node.record.step_end(step_id=step_id, status='completed', exit_code=0)
