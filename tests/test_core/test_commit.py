"""Test the ``fractal.core.commit`` module."""

from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
from typing import Any, Optional

import pytest

import fractal.util
from fractal.core import commit
from fractal.core.node import Node
from tests._helpers import _files, _git

from .conftest import (
    _make_git_repo,
    _parse_project_dir,
    _resolve_branch,
    _spawn_parent_child,
)

__all__ = [
    'test_user_node_commit_init_commits_baseline',
    'test_commit_pushes_unless_local',
    'test_user_init_baseline_survives_a_hostile_external_ignore',
    'test_child_baseline_carries_only_its_seed',
    'test_child_baseline_refuses_a_file_moved_into_its_seed',
    'test_commit_event_records_sha_and_emits_once',
    'test_commit_excludes_write_atomic_temp_files',
    'test_commit_excludes_project_registry_sidecars',
    'test_commit_stages_node_records_past_external_excludes',
    'test_stage_records_tolerates_a_vanished_held_file',
    'test_stage_records_survives_an_unreadable_held_file',
    'test_skip_alarm_covers_foreign_info_exclude_lines',
    'test_record_force_add_refuses_non_record_files',
    'test_estate_add_refuses_non_record_files_with_no_ignore_layer',
    'test_estate_commits_its_own_tool_state',
    'test_estate_commits_the_template_provenance',
    'test_refused_estate_content_leaves_the_clean_check_quiet',
    'test_commit_stamps_iteration_from_args_or_open_row',
    'test_commit_rejects_prelabeled_agent_messages',
    'test_commit_refreshes_wiki_indexes',
    'test_commit_untracks_a_pretracked_wiki_cache',
    'test_commit_update_failure_blocks_commit_but_not_backstops',
    'test_force_commit_body_describes_the_sweep',
    'test_commit_warns_on_a_root_path_shadowing_the_node_folder',
    'test_a_failing_shadow_check_never_fails_the_commit',
    'test_committed_memory_index_links_no_left_out_file',
    'test_unchanged_memory_index_keeps_its_committed_bytes',
    'test_rebuilt_memory_index_is_the_refresh_less_left_out_rows',
    'test_rebuilt_memory_index_keeps_the_node_edits',
    'test_rebuilt_memory_index_follows_the_wiki_ignore_check',
    'test_memory_index_rebuild_waits_for_a_left_out_file',
    'test_a_failed_memory_index_rebuild_never_fails_the_commit',
    'test_commit_ignore_scope_bypasses_scope_but_not_lint',
    'test_multi_scope_commit_boundary',
    'test_dot_scope_root_bounds_the_whole_project',
    'test_scoped_commit_refuses_a_file_moved_into_scope',
    'test_sub_project_commit_boundary',
    'test_wiki_setting_moves_the_scope_exemption',
    'test_commit_refuses_an_invalid_stored_wiki',
    'test_scoped_commit_handles_non_ascii_and_whitespace_paths',
    'test_scoped_child_baseline_commits_init_gitattributes',
    'test_scoped_commit_refuses_a_foreign_line_beside_init_gitattributes',
    'test_commit_check_detects_untracked_work',
    'test_commit_surfaces_hook_aborted_commit',
    'test_commit_retries_after_reformat_hook',
    'test_commit_resolves_invoking_installation_cli',
    'test_commit_resolves_invoking_installation_wiki',
    'test_lint_runs_standalone_without_node_dir',
    'test_lint_lints_the_configured_wiki',
]

# the console script beside the running interpreter -- a bare name would
# resolve through a shim to whatever install the ambient PATH front-runs
_FRACTAL_BIN = pathlib.Path(sys.executable).parent / 'fractal'
# the wiki CLI beside the running interpreter, for the same reason
_WIKI_BIN = pathlib.Path(sys.executable).parent / 'wiki'

# the memory index of the node every memory index rebuild test spawns
_MEMORY_INDEX = '.fractal/main.task/memory/_index.md'

# an index stamp far older than any a test run writes
_AGED_STAMP = '2020-01-01T00:00:00Z'

# the commit warning's header over the root paths shadowing node folder entries
_SHADOW_WARNING = (
    "Warning: staged root path(s) share a name with an entry of the node's own"
    ' folder; if these are node files, move them under {node_folder}/; if they'
    ' are project files, ignore this warning:'
)


# ------ baselines and pushes


@pytest.mark.parametrize(
    argnames=('track', 'wiki'),
    argvalues=[
        pytest.param(False, 'wiki', id='untracked'),
        pytest.param(True, 'wiki', id='tracked'),
        pytest.param(False, 'docs', id='docs'),
    ],
)
def test_user_node_commit_init_commits_baseline(
    git_repo: pathlib.Path,
    track: bool,
    wiki: str,
) -> None:
    """``commit(init=True)`` baselines the project wiki -- plus node data when tracked.

    User nodes have no commit script, so the documented baseline step would
    otherwise fail; the ``--init`` path stages fractal's own artifacts (scoped, so
    other staged work is untouched) and commits them, while a non-init commit from
    a user node is rejected. The node's own ``.fractal/`` is git-ignored on the
    top-level branch by default, so it is committed only after ``fractal track``
    opts the tree in. A project naming another folder in its settings file
    baselines the wiki init created there, and the settings file itself.
    """
    if wiki != 'wiki':
        settings = git_repo / '.fractal' / '.settings.json'
        settings.parent.mkdir()
        settings.write_text(json.dumps({'wiki': wiki}) + '\n', encoding='utf-8')
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    if track:
        subprocess.run(
            [f'{_FRACTAL_BIN}', 'track'],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
    # a non-init commit from a user node is rejected
    with pytest.raises(RuntimeError, match='only --init is supported'):
        node.commit('x')

    # the baseline commits without error and always tracks the project wiki; the
    # node's own seed is committed only on a tracked tree
    before = _head(git_repo)
    # a stranded config write lock sits beside the config at commit time
    (git_repo / '.fractal' / 'main' / 'config.json.lock').touch()
    # engine-materialized system skills sit under the tracked skills dir
    system = git_repo / '.fractal' / 'main' / 'skills' / '.system' / 'imagegen'
    system.mkdir(parents=True)
    (system / 'SKILL.md').write_text('engine-materialized\n', encoding='utf-8')
    # the wiki tool's self-ignored derived cache sits inside the staged wiki
    cache = git_repo / wiki / '.wiki' / 'cache'
    cache.mkdir(parents=True, exist_ok=True)
    (cache / '.gitignore').write_text('*\n', encoding='utf-8')
    (cache / 'word_counts.json').write_text('{}\n', encoding='utf-8')
    # a fresh clone carries no info/exclude at all, so the baseline cannot
    # lean on a block written at init -- the runtime artifacts beside the
    # seed must stay out of the commit on their own
    (git_repo / '.git' / 'info' / 'exclude').unlink()
    node.commit('configure', init=True)
    # a tracked tree makes a real commit (the seed is new), and so does a
    # configured folder (init created its wiki); untracked, the default wiki
    # is already committed by the fixture, so the baseline is legitimately a
    # no-op
    if track or wiki != 'wiki':
        assert _head(git_repo) != before
    result = _git(git_repo, 'ls-files', '.fractal', wiki)
    tracked = result.stdout
    assert f'{wiki}/_index.md' in tracked
    assert ('.fractal/.settings.json' in tracked) == (wiki != 'wiki')
    assert ('.fractal/main/config.json' in tracked) == track
    # runtime artifacts never ride the baseline, tracked or not
    assert '.db' not in tracked
    assert 'config.json.lock' not in tracked
    assert 'skills/.system' not in tracked
    # the wiki's derived cache stays self-ignored past the force-add
    assert '.wiki/cache' not in tracked


def test_commit_pushes_unless_local(tmp_path: pathlib.Path) -> None:
    """``node.commit()`` pushes the branch unless the node was init'd ``--local``.

    Exercises ``node.commit`` -> the commit pipeline end to end and the
    config-driven push gate.
    """
    # bare remote wired as origin
    remote = tmp_path / 'remote.git'
    _git(tmp_path, 'init', '--bare', f'{remote}')
    repo = _make_git_repo(tmp_path / 'repo')
    _git(repo, 'remote', 'add', 'origin', f'{remote}')
    Node(repo).init(agent='claude', user=True)

    def _commit_node(name: str, *, local: bool) -> str:
        output = Node(repo).init(name=name, agent='claude', local=local)
        project_dir = _parse_project_dir(output)
        branch = _resolve_branch(project_dir)
        # configure git user in the worktree
        _git(project_dir, 'config', 'user.email', 'test@test.com')
        _git(project_dir, 'config', 'user.name', 'Test')
        # the seed's baseline commit through the real path pushes unless local
        Node(project_dir).commit(f'configure {name}', init=True)
        return branch

    pushed = _commit_node('pushed', local=False)
    held = _commit_node('held', local=True)

    # the non-local node's branch reaches the remote; the local one does not
    result = _git(tmp_path, 'ls-remote', '--heads', f'{remote}')
    refs = result.stdout
    assert pushed in refs
    assert held not in refs


def test_user_init_baseline_survives_a_hostile_external_ignore(
    tmp_path: pathlib.Path,
) -> None:
    """The baseline commits its seed and wiki past a broad external ignore.

    ``fractal init`` writes the user node's seed and the project wiki and
    must land them as the tree's baseline; a machine-local ``/.fractal/``
    line (or a host ``.gitignore``) would otherwise silently empty the
    pathspec and leave a tree whose baseline never happened.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    _git(repo, 'config', 'user.email', 'test@test.com')
    _git(repo, 'config', 'user.name', 'Test')
    # the hostile layer, in place BEFORE init writes anything
    exclude = repo / '.git' / 'info' / 'exclude'
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open('a', encoding='utf-8') as handle:
        handle.write('/.fractal/\nwiki/\n')
    Node(repo).init(agent='claude', user=True)
    # opt the tree in, so the seed is committable at all (the self-ignore
    # is fractal's own choice; the hostile layer above is the operator's)
    subprocess.run(
        [f'{_FRACTAL_BIN}', 'track'],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    node = Node(repo)
    node.commit('configure', init=True)

    tracked = _git(repo, 'ls-files').stdout
    # the seed and the wiki are on the branch despite both ignore rules
    assert '.fractal/main/config.json' in tracked
    assert 'wiki/_index.md' in tracked
    # runtime state still never rides the baseline
    assert '.status' not in tracked
    assert '.db' not in tracked


def test_child_baseline_carries_only_its_seed(tmp_path: pathlib.Path) -> None:
    """A child's ``--init`` baseline commits its seed alone.

    The baseline skips the wiki refresh and the lint, so it carries only
    fractal's setup: material copied into the child's tree refuses it by
    name, with nothing staged or committed. With the material set aside the
    seed commits as the baseline, and the material then lands through an
    ordinary commit, which the lint gates.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    material = project_dir / 'reference.txt'
    material.write_text('copied material\n', encoding='utf-8')
    head = _git(project_dir, 'rev-parse', 'HEAD').stdout

    with pytest.raises(RuntimeError, match='commits only the node seed') as refused:
        node.commit('configure', init=True)

    # the refusal names the copied file, and nothing was staged or committed
    assert 'reference.txt' in str(refused.value)
    assert _git(project_dir, 'rev-parse', 'HEAD').stdout == head
    assert _git(project_dir, 'diff', '--cached', '--name-only').stdout == ''
    # set aside, the material no longer blocks the seed's baseline...
    aside = tmp_path / 'reference.txt'
    material.rename(aside)
    node.commit('configure', init=True)
    subject = _git(project_dir, 'log', '-1', '--format=%s').stdout.strip()
    assert subject == 'main.task: init (configure)'
    tracked = _git(project_dir, 'ls-files').stdout
    assert '.fractal/main.task/NODE.md' in tracked
    assert 'reference.txt' not in tracked
    # ...and returned, it lands through an ordinary commit
    aside.rename(material)
    node.commit('add reference material')
    assert 'reference.txt' in _git(project_dir, 'ls-files').stdout
    node.commit(check=True)


def test_child_baseline_refuses_a_file_moved_into_its_seed(
    tmp_path: pathlib.Path,
) -> None:
    """A project file moved into the seed refuses the baseline by its source.

    git's rename detection names only a rename's destination, so a project
    file moved into the node's data dir would read as seed content alone,
    and the baseline would land the file's deletion without the lint. The
    refusal names the moved file's source, with nothing committed.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    head = _head(project_dir)
    _git(project_dir, 'mv', 'README.md', '.fractal/main.task/README.md')

    with pytest.raises(RuntimeError, match='commits only the node seed') as refused:
        node.commit('configure', init=True)

    # the refusal lists the source alone (the destination
    # is seed content), and nothing was committed
    listing = str(refused.value).splitlines()[1:]
    assert listing == ['README.md']
    assert _head(project_dir) == head


# ------ events, staging, and subjects


def test_commit_event_records_sha_and_emits_once(tmp_path: pathlib.Path) -> None:
    """A real commit logs one ``commit`` event keyed on the new sha.

    The pipeline emits from a single point gated on ``git commit``
    succeeding, so a reformat-hook abort-and-retry advances HEAD once and
    logs exactly one event; an ``--init`` baseline or a clean-tree no-op
    logs none -- the log counts commits, never command invocations.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)

    # an --init baseline of the seed lands but emits no commit event
    node.commit('baseline', init=True)
    assert node.db.read('events', where={'event': 'commit'}) == []
    # freshen both generated wikis -- the pipeline's index refresh is
    # byte-stable once run, so the no-op invocation below stays a no-op
    for wiki_dir in (
        project_dir / 'wiki',
        project_dir / '.fractal' / branch / 'memory',
    ):
        subprocess.run(
            ['wiki', 'update', f'--path={wiki_dir}'],
            capture_output=True,
            check=True,
        )
    # stub the lint gate (not under test) so the hook path alone decides
    lint = project_dir / '.fractal' / branch / 'scripts' / 'lint.sh'
    lint.write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')

    # a reformat-and-abort pre-commit hook: the first run rewrites the file and
    # fails, the retry succeeds -- the pipeline re-stages and commits once
    (project_dir / '.pre-commit-config.yaml').write_text(
        'repos: []\n',
        encoding='utf-8',
    )
    marker = project_dir / '.hook_ran'
    work = project_dir / 'work.txt'
    hooks_dir = project_dir / '.githooks'
    hooks_dir.mkdir()
    hook = hooks_dir / 'pre-commit'
    hook.write_text(
        '#!/bin/sh\n'
        f'if [ -f "{marker}" ]; then exit 0; fi\n'
        f'touch "{marker}"\n'
        f'printf "reformatted\\n" > "{work}"\n'
        'exit 1\n',
        encoding='utf-8',
    )
    hook.chmod(0o755)
    _git(project_dir, 'config', 'core.hooksPath', f'{hooks_dir}')

    # a real (non-init) commit through the abort-and-retry path -- the marker
    # proves the hook actually fired, so the retry was exercised
    work.write_text('work\n', encoding='utf-8')
    node.commit('do the work')
    assert marker.exists()

    # exactly one commit event, keyed on the new sha (no double-log on retry)
    events = node.db.read('events', where={'event': 'commit'})
    assert [row['metadata'] for row in events] == [_head(project_dir)]
    # a no-op invocation (clean tree, nothing staged) logs no event -- the
    # log counts commits, not command invocations
    node.commit('nothing to land')
    events = node.db.read('events', where={'event': 'commit'})
    assert [row['metadata'] for row in events] == [_head(project_dir)]
    # the subject carries no repo-name prefix
    result = _git(project_dir, 'log', '-1', '--format=%s')
    subject = result.stdout.strip()
    assert subject.startswith(f'{branch}: iteration ')
    assert subject.endswith('(do the work)')


def test_commit_excludes_write_atomic_temp_files(tmp_path: pathlib.Path) -> None:
    """A crash-stranded ``write_atomic`` temp never rides a work commit.

    write_atomic stages to ``.{name}-{rand}.tmp`` beside its target; a crash
    between mkstemp and os.replace can leave one in a committable tree. The
    stage excludes that shape so the residue is never committed, while an
    ordinary work file beside it still lands.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)

    # a real work file plus a write_atomic-shaped crash residue beside it
    (project_dir / 'work.txt').write_text('real work\n', encoding='utf-8')
    (project_dir / '.work.txt-a1b2c3.tmp').write_text('half\n', encoding='utf-8')
    node.commit('do the work', force=True)

    result = _git(project_dir, 'ls-files')
    tracked = result.stdout
    assert 'work.txt' in tracked
    assert '.work.txt-a1b2c3.tmp' not in tracked


@pytest.mark.parametrize(
    argnames='folder',
    argvalues=['.', '.fractal'],
    ids=['worktree-root', 'node-dir'],
)
def test_commit_excludes_project_registry_sidecars(
    tmp_path: pathlib.Path,
    folder: str,
) -> None:
    """A legacy ``registry.db``'s SQLite sidecars never ride a work commit.

    SQLite writes ``-wal``/``-shm`` (WAL mode) or ``-journal`` (rollback)
    beside a live DB, so a swept sidecar is a torn point-in-time byte
    capture of a database another process is mid-write on, plus perpetual
    churn commits as it mutates -- the exact reason the modern spelling is
    barred as a ``.db``/``.db-*`` pair. The legacy spelling gets the same
    pair, in the stage excludes and the exclude template alike, wherever
    the DB sits -- the worktree root or beside the node's seed dir.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)

    # a work file plus a legacy DB and a live writer's sidecars beside it
    (project_dir / 'work.txt').write_text('real work\n', encoding='utf-8')
    sidecars = ('registry.db-wal', 'registry.db-shm', 'registry.db-journal')
    for name in ('registry.db', *sidecars):
        (project_dir / folder / name).write_text('', encoding='utf-8')
    node.commit('do the work', force=True)

    result = _git(project_dir, 'ls-files')
    tracked = result.stdout
    assert 'work.txt' in tracked
    for name in ('registry.db', *sidecars):
        assert name not in tracked, name


def test_commit_stages_node_records_past_external_excludes(
    tmp_path: pathlib.Path,
) -> None:
    """Node records commit under a broad external ignore rule; runtime never.

    A single ``/.fractal/`` line in the shared ``.git/info/exclude`` would
    break every node's commit: the plain add honors the rule and either
    silently unstages the audit trail or hard-fails the whole add. The
    record pass owns the node dir with ``git add -f``, so record custody
    never depends on git ignore state -- while the runtime artifacts the
    force pass could now drag in (the status marker, ``tmp/`` scratch, a
    stray ``registry.db``) stay barred by the derived pathspec excludes.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)

    # a hostile exclude: everything under .fractal ignored, appended
    # OUTSIDE fractal's managed block (worktrees share info/exclude)
    exclude = repo / '.git' / 'info' / 'exclude'
    with exclude.open('a', encoding='utf-8') as handle:
        handle.write('/.fractal/\n')

    # a record write plus runtime residue beside it
    memory = node.node_dir / 'memory' / 'state.md'
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text('finding of record\n', encoding='utf-8')
    scratch = node.node_dir / 'tmp' / 'probe.txt'
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text('scratch\n', encoding='utf-8')
    registry = node.node_dir.parent / 'registry.db'
    registry.write_text('', encoding='utf-8')
    # a live legacy writer leaves SQLite sidecars beside the DB -- a hot WAL
    # is a torn mid-write byte capture, barred like its parent file
    sidecar = node.node_dir.parent / 'registry.db-wal'
    sidecar.write_text('', encoding='utf-8')
    node.commit('record custody', force=True)

    result = _git(project_dir, 'ls-files')
    tracked = result.stdout
    # the record landed despite the broad exclude ...
    assert 'memory/state.md' in tracked
    # ... and the runtime artifacts never ride, force pass or not
    assert 'tmp/probe.txt' not in tracked
    assert 'registry.db' not in tracked
    assert 'registry.db-wal' not in tracked
    assert '.status' not in tracked
    # ... and an estate-internal ignore file keeps its own hold (the memory
    # wiki's cache manages itself)
    assert '.wiki/cache' not in tracked


def test_stage_records_tolerates_a_vanished_held_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A held estate file deleted mid-stage never aborts the commit.

    The record pass force-adds externally-ignored estate files from an
    ``ls-files`` snapshot -- and ignored-and-untracked files are exactly
    what a user's ``git clean -X`` deletes, while estate contents churn
    under the node's own housekeeping. A path that vanishes between the
    snapshot and the ``git add -f`` must not fail the add and abort the
    whole commit after the scope sweep already staged the iteration's
    real work: the pass stages what still exists.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)

    # a hostile exclude: everything under .fractal externally ignored,
    # so the record pass owns both files below
    exclude = repo / '.git' / 'info' / 'exclude'
    with exclude.open('a', encoding='utf-8') as handle:
        handle.write('/.fractal/\n')
    memory = node.node_dir / 'memory' / 'state.md'
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text('finding of record\n', encoding='utf-8')
    doomed = node.node_dir / 'memory' / 'scratchpad.md'
    doomed.write_text('provisional\n', encoding='utf-8')

    # the race, held deterministic: the file vanishes (a git clean -X, the
    # estate's own housekeeping) right after the pass snapshots its listing
    real_run_bytes = fractal.util.git.run_bytes

    def racing_run_bytes(*args: Any, **kwargs: Any) -> Any:
        raw = real_run_bytes(*args, **kwargs)
        doomed.unlink(missing_ok=True)
        return raw

    monkeypatch.setattr('fractal.util.git.run_bytes', racing_run_bytes)
    node.commit('record custody', force=True)

    result = _git(project_dir, 'ls-files')
    tracked = result.stdout
    # the surviving record landed; the vanished one is simply absent
    assert 'memory/state.md' in tracked
    assert 'scratchpad.md' not in tracked


def test_stage_records_survives_an_unreadable_held_file(
    tmp_path: pathlib.Path,
) -> None:
    """One unreadable held record costs itself, never the whole backstop save.

    git stages nothing when any one path in a batched ``add -f`` cannot be
    indexed (exit 128), so a single permission-dead plan file would fail
    the force-add and abort the very ``--force`` save that exists to
    rescue work -- and break every ordinary commit beside it until a human
    clears the path. The pass stages every record it can and names the
    ones it could not.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    # a hostile exclude: everything under .fractal externally ignored,
    # so the record pass owns both files below
    exclude = repo / '.git' / 'info' / 'exclude'
    with exclude.open('a', encoding='utf-8') as handle:
        handle.write('/.fractal/\n')
    memory = node.node_dir / 'memory' / 'irreplaceable.md'
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text('finding of record\n', encoding='utf-8')
    doomed = node.node_dir / 'plans' / '0099-doomed.md'
    doomed.parent.mkdir(parents=True, exist_ok=True)
    doomed.write_text('# doomed\n', encoding='utf-8')
    doomed.chmod(0)
    result = node.commit('backstop save', force=True)

    tracked = _git(project_dir, 'ls-files').stdout
    # the healthy record landed; the unreadable one is named, never fatal
    assert 'memory/irreplaceable.md' in tracked
    assert 'plans/0099-doomed.md' not in tracked
    assert 'could not be staged' in result
    assert 'plans/0099-doomed.md' in result
    # that warning alone names it -- never an ignore-rule skip -- and the last
    # line counts it as an estate file
    lines = result.splitlines()
    skip_lines = [line for line in lines if 'skipped by ignore rules' in line]
    for line in skip_lines:
        assert 'plans/0099-doomed.md' not in line, line
    assert lines[-1].startswith('Held out: 1 estate file(s) not staged'), result


def test_skip_alarm_covers_foreign_info_exclude_lines(
    tmp_path: pathlib.Path,
) -> None:
    """A foreign ``info/exclude`` line alarms; fractal's own block stays silent.

    ``info/exclude`` is a user surface first -- fractal manages only its
    marker-delimited block -- so a user line there eats a deliverable
    exactly like a tracked ``.gitignore`` pattern and must count into the
    ignore-skip warning. The suppression keys on the block's line span,
    never the whole file. Rule sources outside the repo -- the shared
    exclude and the host's global ignore file -- are named without their
    machine paths, since a force commit folds the warning into history.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    # a path held only by fractal's own block (the tmp/ scratch pattern)
    # is an intentional runtime ignore -- no alarm
    (project_dir / 'tmp').mkdir()
    (project_dir / 'tmp' / 'probe.txt').write_text('scratch\n', encoding='utf-8')
    (project_dir / 'work.txt').write_text('work\n', encoding='utf-8')
    result = node.commit('quiet block hold')
    assert 'skipped by ignore rules' not in result
    # the identical pattern as a user line in the shared info/exclude eats
    # a deliverable -- the alarm must fire
    exclude = repo / '.git' / 'info' / 'exclude'
    with exclude.open('a', encoding='utf-8') as handle:
        handle.write('src/generated.txt\n')
    src = project_dir / 'src'
    src.mkdir()
    (src / 'generated.txt').write_text('deliverable\n', encoding='utf-8')
    (src / 'other.txt').write_text('more work\n', encoding='utf-8')
    result = node.commit('foreign line hold')
    assert 'skipped by ignore rules' in result
    # the rule is named by its repo-relative source, never a machine path
    assert 'src/generated.txt (.git/info/exclude:' in result
    assert f'{repo}' not in result
    # the host's global ignore file is the other rule source outside the
    # repo -- git names it by a machine path, so it reads as its config key,
    # in the warning and in the force commit body that folds the warning in
    excludes = tmp_path / 'global_ignore'
    excludes.write_text('# host\n*.swp\n', encoding='utf-8')
    _git(project_dir, 'config', 'core.excludesFile', f'{excludes}')
    (project_dir / 'notes.swp').write_text('swap\n', encoding='utf-8')
    (src / 'third.txt').write_text('still more work\n', encoding='utf-8')
    result = node.commit('host rule hold', force=True)
    assert 'notes.swp (core.excludesFile:2:*.swp)' in result
    body = _git(project_dir, 'log', '-1', '--format=%b').stdout
    assert 'notes.swp (core.excludesFile:2:*.swp)' in body
    assert f'{tmp_path}' not in body


def test_record_force_add_refuses_non_record_files(
    tmp_path: pathlib.Path,
) -> None:
    """The force pass stages records only, and says what it did.

    The layer this pass overrides -- a machine-local ignore -- is where a
    host fences its secrets, so a general "stage what is ignored" verb
    would silently commit a dotenv or a key a node parked in its estate.
    Only the canon-required record surfaces at text suffixes qualify; a
    non-record file is refused by name, and every force-add is reported
    on the commit output. A refused file is the refusal's to report: the
    ignore-rule warning, which names repository and host holds, leaves it
    out, and the held-out counts take it as an estate file.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    # a hostile exclude: everything under .fractal ignored
    exclude = repo / '.git' / 'info' / 'exclude'
    with exclude.open('a', encoding='utf-8') as handle:
        handle.write('/.fractal/\n')
    # a record, plus secret-shaped files a node parked beside it -- one in
    # a new directory, which the ignore listing collapses to one entry
    memory = node.node_dir / 'memory' / 'state.md'
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text('finding of record\n', encoding='utf-8')
    (node.node_dir / '.env').write_text('TOKEN=hunter2\n', encoding='utf-8')
    (node.node_dir / 'id_rsa').write_text('PRIVATE KEY\n', encoding='utf-8')
    (node.node_dir / 'creds.pem').write_text('CERT\n', encoding='utf-8')
    (node.node_dir / 'memory' / 'dump.tar').write_bytes(b'binary')
    (node.node_dir / 'data').mkdir()
    (node.node_dir / 'data' / 'blob.bin').write_bytes(b'binary')
    parked = ('.env', 'id_rsa', 'creds.pem', 'memory/dump.tar', 'data/blob.bin')
    result = node.commit('record custody', force=True)

    tracked = _git(project_dir, 'ls-files').stdout
    # the record landed; nothing secret-shaped or binary did
    assert 'memory/state.md' in tracked
    for path in parked:
        assert path not in tracked, path
    # the pass reports both halves rather than leaving them to be inferred
    assert 'Force-staged' in result
    assert 'memory/state.md' in result
    lines = result.splitlines()
    refused_line = next(line for line in lines if 'NOT force-staged' in line)
    assert 'not node records and were NOT force-staged' in refused_line
    for path in parked:
        assert f'.fractal/main.task/{path}' in refused_line, path
    # the ignore warning names neither a refused file nor the collapsed
    # directory holding one, and the last line counts them as estate files
    skip_lines = [line for line in lines if 'skipped by ignore rules' in line]
    for line in skip_lines:
        assert 'id_rsa' not in line, line
        assert '.fractal/main.task/data' not in line, line
    held = f'Held out: {len(parked)} estate file(s) not staged'
    assert lines[-1].startswith(held), result
    # both halves name worktree-relative paths -- a force commit folds these
    # bytes into git history, where machine-local paths do not belong
    forced_line = next(
        line for line in result.splitlines() if line.startswith('Force-staged')
    )
    assert '.fractal/main.task/memory/state.md' in forced_line
    assert f'{project_dir}' not in forced_line


def test_estate_add_refuses_non_record_files_with_no_ignore_layer(
    tmp_path: pathlib.Path,
) -> None:
    """The allowlist should bound the estate add, not just the ignore override.

    Containment is inverted against risk: the same dotenv the force pass
    refuses by name rides a commit silently the moment no host rule
    happens to fence it -- the default state of a fresh clone. One law
    should govern both paths, so a parked credential stays out of history
    and is named either way, while the estate's records still commit.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    # no hostile exclude this time: nothing fences the estate at all
    memory = node.node_dir / 'memory' / 'state.md'
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text('finding of record\n', encoding='utf-8')
    checker = node.node_dir / 'memory' / 'evidence' / 'main.py'
    checker.parent.mkdir(parents=True, exist_ok=True)
    checker.write_text('print("checked")\n', encoding='utf-8')
    (node.node_dir / '.env').write_text(
        'AWS_SECRET_ACCESS_KEY=hunter2\n',
        encoding='utf-8',
    )
    (node.node_dir / 'id_rsa').write_text('PRIVATE KEY\n', encoding='utf-8')
    (node.node_dir / 'creds.pem').write_text('CERT\n', encoding='utf-8')
    (node.node_dir / 'memory' / 'dump.tar').write_bytes(b'binary')
    # a plan's text patches are records; its screenshots and run output are not
    plans = node.node_dir / 'plans'
    plans.mkdir(parents=True, exist_ok=True)
    (plans / 'fix.diff').write_text('--- a\n+++ b\n', encoding='utf-8')
    (plans / 'fix.patch').write_text('--- a\n+++ b\n', encoding='utf-8')
    (plans / 'shot.png').write_bytes(b'\x89PNG')
    (plans / 'run.log').write_text('ran\n', encoding='utf-8')
    (plans / 'run.err').write_text('failed\n', encoding='utf-8')
    result = node.commit('plain add path')

    tracked = _git(project_dir, 'ls-files').stdout
    # the records landed; nothing secret-shaped, binary, or run output did
    assert 'memory/state.md' in tracked
    assert 'memory/evidence/main.py' in tracked
    assert 'plans/fix.diff' in tracked
    assert 'plans/fix.patch' in tracked
    for parked in ('.env', 'id_rsa', 'creds.pem', 'dump.tar'):
        assert parked not in tracked, parked
    # the refusal is named, not inferred from an absent file
    assert 'are not node records' in result
    for parked in ('id_rsa', 'creds.pem', 'dump.tar'):
        assert parked in result, parked
    for parked in ('shot.png', 'run.log', 'run.err'):
        assert parked not in tracked, parked
        assert parked in result, parked
    # a record edited after it is tracked still commits -- the allowlist
    # gates what the estate adds new, never the upkeep of its own history
    # (the memory wiki's index refresh owns the frontmatter around the body)
    memory.write_text('finding of record, revised\n', encoding='utf-8')
    node.commit('record upkeep')
    listing = _git(project_dir, 'show', 'HEAD:.fractal/main.task/memory/state.md')
    committed = listing.stdout
    assert 'finding of record, revised' in committed


def test_estate_commits_its_own_tool_state(tmp_path: pathlib.Path) -> None:
    """An ordinary node's estate tool state is content, and commits silently.

    The content law bounds what an estate adds, so it must describe
    everything an estate legitimately holds -- not only the records, but
    the tool state a fresh clone needs to check the estate out as the
    node left it: git's empty-directory placeholder under a bare record
    dir, and the memory wiki's settings, whose declared-root marker the
    wiki CLI reads the memory back through. Withholding those would
    leave a normal node permanently untracked against the pipeline's own
    clean check, with nothing able to clear it, so they must commit --
    and, being ordinary content, must draw no refusal notice.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    (project_dir / 'work.txt').write_text('work\n', encoding='utf-8')
    result = node.commit('ordinary work')

    tracked = _git(project_dir, 'ls-files').stdout
    # the estate's own tool state rides the commit beside the records
    estate = '.fractal/main.task'
    for state in (f'{estate}/plans/.gitkeep', f'{estate}/memory/.wiki/settings.json'):
        assert state in tracked, state
    assert f'{estate}/NODE.md' in tracked
    # nothing was withheld, so the pass says nothing about refusals
    assert 'NOT staged' not in result
    assert 'are not node records' not in result
    # and the tree reads back clean, so the loop's net never fires
    node.commit(check=True)


def test_estate_commits_the_template_provenance(tmp_path: pathlib.Path) -> None:
    """A template-seeded node's ``_template.toml`` is an estate record.

    The provenance file is the record of what seeded the node, so the
    node's own commits must fold it into history beside ``NODE.md`` and
    ``config.json`` -- staged by the ``--init`` baseline with no refusal
    notice, and clean against the pipeline's own check afterwards.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    # a committed template folder -- the seed reads from git, not the disk
    template = repo / 'templates' / 'crew'
    (template / 'steps').mkdir(parents=True)
    (template / 'config.json').write_text('{}\n', encoding='utf-8')
    (template / 'steps' / '01-GO.md').write_text('# go\n', encoding='utf-8')
    _git(repo, 'add', 'templates')
    _git(repo, 'commit', '-m', 'template templates/crew')
    output = Node(repo).init(
        name='task',
        agent='claude',
        local=True,
        template=f'{template}',
    )
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    result = node.commit('configure', init=True)

    # the provenance rides the baseline beside the other estate records
    tracked = _git(project_dir, 'ls-files').stdout
    assert '.fractal/main.task/_template.toml' in tracked
    # ordinary content draws no refusal notice
    assert 'NOT staged' not in result
    assert 'are not node records' not in result
    # and the tree reads back clean
    node.commit(check=True)


def test_refused_estate_content_leaves_the_clean_check_quiet(
    tmp_path: pathlib.Path,
) -> None:
    """A parked credential is withheld from history without reading as dirty.

    The clean check counts only work the stage could commit, which is
    why it already rides the stage's own excludes. Estate content the
    content law refuses is exactly that kind of dirt: no pass may ever
    stage it, so counting it would fire the loop's force-commit net
    every iteration over a file that can never clear. The refusal is
    reported through the commit output instead, where it names the file.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    (project_dir / 'work.txt').write_text('work\n', encoding='utf-8')
    node.commit('ordinary work')
    # a node parks credentials in its estate, with nothing fencing them --
    # one beside a tracked record, one in a directory of its own, which the
    # default status listing would collapse to a single untracked entry
    (node.node_dir / '.env').write_text('AWS_SECRET_ACCESS_KEY=x\n', encoding='utf-8')
    keys = node.node_dir / 'memory' / '.ssh'
    keys.mkdir(parents=True)
    (keys / 'id_ed25519').write_text('PRIVATE KEY\n', encoding='utf-8')
    result = node.commit('work beside parked credentials')

    tracked = _git(project_dir, 'ls-files').stdout
    for parked in ('.env', 'id_ed25519'):
        assert parked not in tracked, parked
        assert parked in result, parked
    # the refused files are dirt no pass may stage, so the net stays parked
    node.commit(check=True)


def test_commit_stamps_iteration_from_args_or_open_row(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commit subjects stamp the caller's run-qualified iteration, the open row's, or 0.

    The loop passes the live iteration and run/iter/step lineage as explicit
    arguments to the commit cluster -- so a backstop commit after the
    iteration row closes still stamps the run-qualified label it belongs to.
    An agent/operator commit stamps the open iteration's run and number,
    else the plain row-less ``iteration 0``, and its commit event pins the
    active lineage; ambient ``ITER``-style environment variables carry no
    weight.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    # configure git identity in the worktree
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    node.commit('baseline', init=True)

    def _subject() -> str:
        result = _git(project_dir, 'log', '-1', '--format=%s')
        return result.stdout.strip()

    # no open iteration and no exported loop env: the label falls back to 0
    (project_dir / 'a.txt').write_text('work\n', encoding='utf-8')
    node.commit('first', force=True)
    assert _subject() == f'{branch}: iteration 0 (first)'
    # an open iteration row: the commit stamps its run-qualified number and
    # the commit event pins the active lineage
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=3)
    (project_dir / 'b.txt').write_text('work\n', encoding='utf-8')
    node.commit('second', force=True)
    assert _subject() == f'{branch}: iteration {run_id}.3 (second)'
    events = node.db.read('events', where={'event': 'commit'})
    assert events[0]['run_id'] == run_id
    assert events[0]['iter_id'] == iter_id
    # a closed iteration row stops counting, and ambient env vars carry no
    # weight: the label falls back to 0
    node.record.iter_end(iter_id=iter_id, status='completed', exit_code=0)
    monkeypatch.setenv('ITER', '7')
    (project_dir / 'c.txt').write_text('work\n', encoding='utf-8')
    node.commit('final', force=True)
    assert _subject() == f'{branch}: iteration 0 (final)'
    monkeypatch.delenv('ITER')
    # a loop-invoked commit passes iteration and lineage explicitly -- the
    # backstop path that stamps the live number after the row closes
    (project_dir / 'd.txt').write_text('work\n', encoding='utf-8')
    commit.commit(
        node=node,
        message='explicit',
        force=True,
        iteration=9,
        run_id=run_id,
        iter_id=iter_id,
    )
    assert _subject() == f'{branch}: iteration {run_id}.9 (explicit)'
    events = node.db.read('events', where={'event': 'commit'})
    assert events[0]['run_id'] == run_id
    assert events[0]['iter_id'] == iter_id


def test_commit_rejects_prelabeled_agent_messages(tmp_path: pathlib.Path) -> None:
    """A message prelabeled with the branch or an ``iteration`` label is rejected.

    The pipeline composes the subject itself
    (``<branch>: iteration <run>.<iter> (<message>)``), so a message that
    repeats those labels would double-label history. The rejection anchors to
    the label shapes -- colons alone and mid-sentence mentions are fine --
    and the force/init save paths never block on the shape (a backstop must
    always land).
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    # configure git identity in the worktree
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    # baseline, then stub the lint gate (not under test)
    node.commit('baseline', init=True)
    lint = project_dir / '.fractal' / branch / 'scripts' / 'lint.sh'
    lint.write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')
    (project_dir / 'a.txt').write_text('work\n', encoding='utf-8')
    # branch-bearing and 'iteration'-bearing messages are rejected
    # (case-insensitively), before anything is staged or committed
    for message in (f'{branch}: add parser', 'Iteration 3 wrap-up'):
        with pytest.raises(RuntimeError, match='bare lowercase summary'):
            node.commit(message)
    # a colon alone is no label -- the bare summary lands
    node.commit('fix: parser edge case')
    # mentioning the labels mid-sentence is no prelabel either -- the guard
    # anchors to the label shapes, not bare substrings
    (project_dir / 'b.txt').write_text('work\n', encoding='utf-8')
    node.commit(f'fix off-by-one in the {branch} iteration counter')
    # the force backstop never blocks on the message shape
    (project_dir / 'c.txt').write_text('work\n', encoding='utf-8')
    node.commit(f'{branch}: rescued work', force=True)


# ------ wiki refresh and force backstops


@pytest.mark.parametrize('wiki', ['wiki', 'docs'])
def test_commit_refreshes_wiki_indexes(tmp_path: pathlib.Path, wiki: str) -> None:
    """A commit refreshes both wiki indexes so current bytes ride the commit.

    Agents write pages without touching the generated ``_index.md`` files;
    the pipeline runs ``wiki update`` on the project and memory wikis before
    the lint gate, so the refreshed indexes land in the same commit as the
    work -- no dedicated refresh commits, no agent turns spent on it. The
    project wiki is the configured folder, whatever its name: a corpus at
    ``wiki/`` beside a ``docs`` project wiki is ordinary content the
    refresh never touches, stale index and all.
    """
    repo = _make_git_repo(tmp_path / 'repo', wiki=wiki)
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    # configure git identity in the worktree
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    node.commit('baseline', init=True)
    corpus = _files(project_dir / 'wiki')
    # new pages in both wikis, with the generated indexes left stale
    (project_dir / wiki / 'topic.md').write_text(
        '---\nname: topic\ndesc: A topic page.\n---\n\n# topic\n\n***\n',
        encoding='utf-8',
    )
    (node.node_dir / 'memory' / 'finding.md').write_text(
        '---\nname: finding\ndesc: A finding.\n---\n\n# finding\n\n***\n',
        encoding='utf-8',
    )
    node.commit('add topic and finding pages')

    def _committed(path: str) -> str:
        result = _git(project_dir, 'show', f'HEAD:{path}')
        return result.stdout

    # both committed indexes carry the regenerated link rows...
    assert '[[topic' in _committed(f'{wiki}/_index.md')
    assert '[[finding' in _committed(f'.fractal/{branch}/memory/_index.md')
    # ...and the refresh left nothing behind for a later commit to sweep
    result = _git(project_dir, 'status', '--porcelain')
    status = result.stdout
    assert status == ''
    # a corpus beside the configured wiki kept its bytes
    if wiki != 'wiki':
        assert _files(project_dir / 'wiki') == corpus


def test_commit_untracks_a_pretracked_wiki_cache(tmp_path: pathlib.Path) -> None:
    """A work commit drops a tracked wiki cache and never re-tracks it.

    The wiki tool derives ``.wiki/cache/`` and self-ignores it, but a tree
    whose baseline once force-tracked it carries per-page mtimes that every
    ``wiki update`` rewrites -- churn riding every commit and defeating the
    byte-match parent and child copies need to merge cleanly. The stage
    drops a tracked cache from the index (the on-disk copy stays), so the
    next commit converges the tree and the cache's own ignore holds from
    there: the refreshed cache reads back clean, never dirty.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    # a legacy baseline that force-tracked the derived cache past its ignore
    wiki_dir = project_dir / 'wiki'
    subprocess.run(
        ['wiki', 'update', f'--path={wiki_dir}'],
        capture_output=True,
        check=True,
    )
    _git(project_dir, 'add', '-A')
    _git(project_dir, 'add', '-f', '--', 'wiki/.wiki/cache')
    _git(project_dir, 'commit', '-m', 'legacy baseline tracks the cache')
    # a work commit drops the cache from tracking and keeps the disk copy
    (project_dir / 'work.txt').write_text('work\n', encoding='utf-8')
    node.commit('do the work')

    tracked = _git(project_dir, 'ls-files', 'wiki').stdout
    assert '.wiki/cache' not in tracked
    assert (wiki_dir / '.wiki' / 'cache' / 'word_counts.json').is_file()
    # untracked again, the refresh's rewrites stay invisible: the tree reads
    # back clean and the loop's net never fires over the churn
    node.commit(check=True)


def test_commit_update_failure_blocks_commit_but_not_backstops(
    tmp_path: pathlib.Path,
) -> None:
    """A failed index refresh fails the commit; ``--force``/``--init`` still save.

    ``wiki update`` refuses a wiki carrying merge conflict markers, and a
    broken wiki must never land -- the commit fails with the update output in
    the error. The loop's backstop (``--force``) and the ``--init`` baseline
    skip the refresh, so the failure-save path is never blocked.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    # configure git identity in the worktree
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)

    # a conflict-marked wiki page in history -- `wiki update` refuses to write
    # over it (committed with raw git, past the pipeline's refresh)
    (project_dir / 'wiki' / 'broken.md').write_text(
        '---\nname: broken\ndesc: A broken page.\n---\n\n# broken\n\n***\n'
        '<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> other\n',
        encoding='utf-8',
    )
    _git(project_dir, 'add', 'wiki/broken.md')
    _git(project_dir, 'commit', '-m', 'broken page')
    # the --init baseline of the seed skips the refresh -- it lands despite
    # the markers
    node.commit('baseline', init=True)
    head_before = _head(project_dir)
    # a plain commit fails with the refusal named in the error, before landing
    (project_dir / 'work.txt').write_text('work\n', encoding='utf-8')
    with pytest.raises(RuntimeError, match='Merge conflict markers'):
        node.commit('add work')
    assert _head(project_dir) == head_before
    # the force backstop skips the refresh -- the rescue always saves
    node.commit('rescued work', force=True)
    assert _head(project_dir) != head_before


def test_force_commit_body_describes_the_sweep(tmp_path: pathlib.Path) -> None:
    """A force commit's body folds in the caller paragraph, warnings, and diffstat.

    The loop's backstop saves are force commits made when an iteration
    fails, so the commit must explain itself from git history alone: the
    caller's body paragraph (the failure reason and never-run tail), the
    ignore-skip, oversized-file, and root-shadow warnings, and the staged
    diffstat.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    # configure git identity in the worktree
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    node.commit('baseline', init=True)
    # a tracked host ignore rule eats a workspace file (the skip warning), an
    # oversized file trips the size warning, a root plan trips the shadow
    # warning, and real work rides the sweep
    gitignore = project_dir / '.gitignore'
    ignored = gitignore.read_text(encoding='utf-8') + 'eaten.log\n'
    gitignore.write_text(ignored, encoding='utf-8')
    (project_dir / 'eaten.log').write_text('eaten\n', encoding='utf-8')
    (project_dir / 'blob.bin').write_bytes(b'\0' * (11 * 1024 * 1024))
    (project_dir / 'plans').mkdir()
    (project_dir / 'plans' / 'stray.md').write_text('plan\n', encoding='utf-8')
    (project_dir / 'work.txt').write_text('work\n', encoding='utf-8')
    # a hostile external ignore holds the estate out, so the record pass
    # force-stages a fresh record and refuses the parked non-record --
    # both notices belong in the body beside the warnings
    exclude = repo / '.git' / 'info' / 'exclude'
    with exclude.open('a', encoding='utf-8') as handle:
        handle.write('/.fractal/\n')
    (node.node_dir / 'memory' / 'note.md').write_text('note\n', encoding='utf-8')
    (node.node_dir / '.env').write_text('TOKEN=hunter2\n', encoding='utf-8')
    run_id = node.record.run_start()
    commit.commit(
        node=node,
        message='failed on EXECUTE',
        force=True,
        body='agent error (exit 2)\nsteps not run: REVIEW, COMMIT',
        iteration=2,
        run_id=run_id,
    )

    def _log(fmt: str) -> str:
        result = _git(project_dir, 'log', '-1', f'--format={fmt}')
        return result.stdout.strip()

    # the subject is the run-qualified backstop label
    assert _log('%s') == f'{branch}: iteration {run_id}.2 (failed on EXECUTE)'
    # the body carries the caller's paragraph, the record pass's notices,
    # every warning, and a diffstat naming the swept files
    body = _log('%b')
    assert 'agent error (exit 2)' in body
    assert 'steps not run: REVIEW, COMMIT' in body
    assert 'Force-staged' in body
    assert 'memory/note.md' in body
    assert 'NOT force-staged' in body
    assert '.env' in body
    assert f'{project_dir}' not in body
    assert 'skipped by ignore rules' in body
    # the refused .env is the refusal's to name, never an ignore-rule skip
    assert '.fractal/main.task/.env (.git/info/exclude:' not in body
    assert f'{repo}' not in body
    assert 'blob.bin (11MB)' in body
    assert 'plans/stray.md (shadows .fractal/main.task/plans)' in body
    assert 'work.txt' in body


@pytest.mark.parametrize(
    argnames=('origin', 'path', 'warned'),
    argvalues=[
        # a node plan written at the worktree root by mistake
        ('written', 'plans/x.md', True),
        # a committed node plan moved to the worktree root
        ('moved', 'plans/x.md', True),
        # the project's own plans folder, there when the node started
        ('at-base', 'plans/x.md', False),
        # a new top-level path no node folder entry names
        ('unrelated', 'notes/x.md', False),
        # a merge bringing the path in, not the node writing it
        ('merged', 'plans/x.md', False),
    ],
    ids=['written', 'moved', 'at-base', 'unrelated', 'merged'],
)
def test_commit_warns_on_a_root_path_shadowing_the_node_folder(
    tmp_path: pathlib.Path,
    origin: str,
    path: str,
    warned: bool,
) -> None:
    """A new root path named like a node folder entry warns, and still commits.

    An agent that writes ``plans/x.md`` from the worktree root, or moves its
    own plan there, may have meant its own ``.fractal/<branch>/plans``; the
    commit names the path and the entry it shares a name with, and says what
    to do either way. A top-level name the project already had when the node
    started is the project's own, an unrelated new path is ordinary work, and
    a merge's additions are not the node's writes.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    if origin == 'at-base':
        (repo / 'plans').mkdir()
        (repo / 'plans' / 'README.md').write_text('roadmap\n', encoding='utf-8')
        _git(repo, 'add', 'plans')
        _git(repo, 'commit', '-m', 'add the project plans folder')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    # configure git identity in the worktree
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    node.commit('baseline', init=True)
    assert (node.node_dir / 'plans').is_dir()
    stray = project_dir / path
    stray.parent.mkdir(exist_ok=True)
    if origin == 'moved':
        plan = node.node_dir / 'plans' / 'x.md'
        plan.write_text('plan\n', encoding='utf-8')
        node.commit('add a plan')
        plan.rename(stray)
    elif origin == 'merged':
        _git(project_dir, 'checkout', '-q', '-b', 'side')
        stray.write_text('plan\n', encoding='utf-8')
        _git(project_dir, 'add', path)
        _git(project_dir, 'commit', '-q', '-m', 'side plan')
        _git(project_dir, 'checkout', '-q', branch)
        _git(project_dir, 'merge', '-q', '--no-commit', '--no-ff', 'side')
    else:
        stray.write_text('plan\n', encoding='utf-8')
    result = node.commit('add the path')
    lines = result.splitlines()
    warning = _SHADOW_WARNING.format(node_folder=f'.fractal/{branch}')
    assert (warning in lines) is warned, result
    assert (f'{path} (shadows .fractal/{branch}/plans)' in lines) is warned
    # the warning never refuses: the path is committed either way
    assert _git(project_dir, 'ls-files', path).stdout.strip() == path


@pytest.mark.parametrize('force', [False, True], ids=['plain', 'force'])
def test_a_failing_shadow_check_never_fails_the_commit(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    force: bool,
) -> None:
    """The shadow check is advisory: when it cannot run, the commit lands.

    The check reads the node's record for its base, and a locked central
    database must not fail a commit -- least of all the loop's backstop save.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    # configure git identity in the worktree
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    node.commit('baseline', init=True)
    (project_dir / 'plans').mkdir()
    (project_dir / 'plans' / 'x.md').write_text('plan\n', encoding='utf-8')

    def _locked(*args: Any, **kwargs: Any) -> str:
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr('fractal.core.files.Files._diff_anchor', _locked)
    result = commit.commit(node=node, message='add a plan', force=force)
    assert 'share a name' not in result
    assert _git(project_dir, 'ls-files', 'plans/x.md').stdout.strip() == 'plans/x.md'


# ------ the memory index rebuild


@pytest.mark.parametrize('force', [False, True], ids=['plain', 'force'])
def test_committed_memory_index_links_no_left_out_file(
    tmp_path: pathlib.Path,
    force: bool,
) -> None:
    """No committed memory index row links a file the commit leaves out.

    The memory wiki indexes every file on disk, so a binary or a log the
    estate law refuses gains a row -- from the commit's own refresh, or,
    before a backstop save that skips the refresh, from the node's own
    ``wiki update``. The committed indexes link neither the refused files
    nor a folder holding only them, the estate warning still names each
    file, and the working tree reads back clean.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    # records beside a refused archive, and a folder holding only a refused log
    memory = node.node_dir / 'memory'
    (memory / 'notes.md').write_text(
        '---\nname: notes\ndesc: Notes.\n---\n\n# notes\n\n***\n',
        encoding='utf-8',
    )
    (memory / 'data.json').write_text('{}\n', encoding='utf-8')
    (memory / 'dump.tar').write_bytes(b'binary')
    (memory / 'logs').mkdir()
    (memory / 'logs' / 'run.log').write_text('ran\n', encoding='utf-8')
    if force:
        subprocess.run(
            [_WIKI_BIN, 'update', f'--path={memory}'],
            capture_output=True,
            check=True,
        )
    head_before = _head(project_dir)
    result = node.commit('memory beside refused files', force=force)

    assert _head(project_dir) != head_before
    # the records keep their rows; the refused files and their folder have none
    index = _git(project_dir, 'show', f'HEAD:{_MEMORY_INDEX}').stdout
    assert '[[notes' in index
    assert '[[data.json' in index
    assert 'dump.tar' not in index
    assert 'logs/' not in index
    tracked = _git(project_dir, 'ls-files', '.fractal/main.task/memory').stdout
    assert 'memory/logs/' not in tracked
    # the estate warning still names each refused file, beside the rebuild
    refused = next(line for line in result.splitlines() if 'NOT staged' in line)
    for path in ('memory/dump.tar', 'memory/logs/run.log'):
        assert f'.fractal/main.task/{path}' in refused, path
    assert 'Memory index rebuilt without 2 file(s) the commit leaves out' in result
    node.commit(check=True)


def test_unchanged_memory_index_keeps_its_committed_bytes(
    tmp_path: pathlib.Path,
) -> None:
    """A memory index unchanged but for a refused file keeps HEAD's bytes.

    Every refresh adds the refused file's row back and re-stamps the index,
    so a rebuild without the row would move the ``updated:`` stamp on each
    commit while the rows stand still. An index whose rebuild matches HEAD's
    but for that stamp commits HEAD's bytes, old stamp included.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    memory = node.node_dir / 'memory'
    (memory / 'notes.md').write_text(
        '---\nname: notes\ndesc: Notes.\n---\n\n# notes\n\n***\n',
        encoding='utf-8',
    )
    (memory / 'dump.tar').write_bytes(b'binary')
    node.commit('notes beside a refused archive')
    # age the committed stamps, so a re-stamp within the same second shows
    index = memory / '_index.md'
    text = index.read_text(encoding='utf-8')
    aged = re.sub(r'(?m)^(created|updated): .*$', rf'\1: {_AGED_STAMP}', text)
    index.write_text(aged, encoding='utf-8')
    _git(project_dir, 'add', f'{index}')
    _git(project_dir, 'commit', '-m', 'age the memory index')
    # work elsewhere, with memory untouched and the archive still in it
    (project_dir / 'work.txt').write_text('work\n', encoding='utf-8')
    node.commit('more work')

    committed = _git(project_dir, 'show', f'HEAD:{_MEMORY_INDEX}').stdout
    assert committed == _git(project_dir, 'show', f'HEAD~1:{_MEMORY_INDEX}').stdout
    assert f'updated: {_AGED_STAMP}' in committed
    assert 'dump.tar' not in committed
    node.commit(check=True)


def test_rebuilt_memory_index_is_the_refresh_less_left_out_rows(
    tmp_path: pathlib.Path,
) -> None:
    """The rebuilt memory index is the refresh's, less the left-out rows.

    The rebuild drops what the commit leaves out and nothing else: a new
    page's row, every description, and the rest of the index keep the bytes
    the refresh wrote, and only the rows linking a refused file or a folder
    holding nothing else go.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    memory = node.node_dir / 'memory'
    (memory / 'notes.md').write_text(
        '---\nname: notes\ndesc: Notes.\n---\n\n# notes\n\n***\n',
        encoding='utf-8',
    )
    node.commit('notes')
    # a new page beside a refused archive and a folder holding only a log
    (memory / 'finding.md').write_text(
        '---\nname: finding\ndesc: A finding.\n---\n\n# finding\n\n***\n',
        encoding='utf-8',
    )
    (memory / 'dump.tar').write_bytes(b'binary')
    (memory / 'logs').mkdir()
    (memory / 'logs' / 'run.log').write_text('ran\n', encoding='utf-8')
    # the refresh the commit runs, taken ahead of it so its bytes are in hand
    # (the commit's own refresh then finds nothing left to do)
    subprocess.run(
        [_WIKI_BIN, 'update', f'--path={memory}'],
        capture_output=True,
        check=True,
    )
    refreshed = (memory / '_index.md').read_text(encoding='utf-8')
    node.commit('finding beside refused files')

    def _lines(text: str) -> list[str]:
        # an index's content lines, blank lines and the stamp aside
        return [
            line
            for line in text.splitlines()
            if line and not line.startswith('updated:')
        ]

    committed = _git(project_dir, 'show', f'HEAD:{_MEMORY_INDEX}').stdout
    assert '[[finding' in committed
    left_out = ('[[dump.tar', '[[logs/')
    expected = [line for line in _lines(refreshed) if not line.startswith(left_out)]
    assert _lines(committed) == expected


def test_rebuilt_memory_index_keeps_the_node_edits(tmp_path: pathlib.Path) -> None:
    """A node's own edits to a memory index survive the rebuild.

    The wiki regenerates an index's link rows but keeps its description and
    the text below its separator from the file it reads, and both are the
    node's to write. The rebuild starts from the bytes the commit stages, so
    the edits land in the commit and stay on disk while the refused row goes.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    memory = node.node_dir / 'memory'
    (memory / 'notes.md').write_text(
        '---\nname: notes\ndesc: Notes.\n---\n\n# notes\n\n***\n',
        encoding='utf-8',
    )
    node.commit('notes')
    # the node describes its memory and adds a line below the separator, with
    # a refused archive beside the pages
    index = memory / '_index.md'
    text = index.read_text(encoding='utf-8')
    text = text.replace('desc: ...', 'desc: What the node knows.')
    index.write_text(f'{text}\nRead the notes page first.\n', encoding='utf-8')
    (memory / 'dump.tar').write_bytes(b'binary')
    node.commit('describe memory beside a refused archive')

    committed = _git(project_dir, 'show', f'HEAD:{_MEMORY_INDEX}').stdout
    on_disk = index.read_text(encoding='utf-8')
    for text in (committed, on_disk):
        assert 'desc: What the node knows.' in text
        assert 'Read the notes page first.' in text
        assert 'dump.tar' not in text
    node.commit(check=True)


@pytest.mark.parametrize(
    argnames=('source', 'rule', 'indexed'),
    argvalues=[
        pytest.param('info/exclude', 'held.md', False, id='repository-rule'),
        pytest.param('core.excludesFile', 'held.md', True, id='host-rule'),
        pytest.param('info/exclude', '/.fractal/', True, id='ignored-root'),
    ],
)
def test_rebuilt_memory_index_follows_the_wiki_ignore_check(
    tmp_path: pathlib.Path,
    source: str,
    rule: str,
    indexed: bool,
) -> None:
    """A force-added memory record keeps exactly the row the refresh gives it.

    The record pass force-adds a record a machine-local ignore rule holds
    out, but the wiki's own ignore check decides whether the refresh indexes
    it: the repository's rules alone, read from the patterns, so a tracked
    file counts too. A repository rule keeps the record out of the index; a
    host rule does not, and neither does a rule that ignores the memory wiki
    itself, which turns the check off. The rebuild, which runs outside the
    repository, applies the same check, both at the commit that force-adds
    the record and at a later one that finds it tracked.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    # a machine-local rule holds the record out of the plain adds
    if source == 'info/exclude':
        exclude = repo / '.git' / 'info' / 'exclude'
        with exclude.open('a', encoding='utf-8') as handle:
            handle.write(f'{rule}\n')
    else:
        excludes = tmp_path / 'global_ignore'
        excludes.write_text(f'{rule}\n', encoding='utf-8')
        _git(project_dir, 'config', 'core.excludesFile', f'{excludes}')
    memory = node.node_dir / 'memory'
    (memory / 'held.md').write_text(
        '---\nname: held\ndesc: A held record.\n---\n\n# held\n\n***\n',
        encoding='utf-8',
    )
    (memory / 'dump.tar').write_bytes(b'binary')
    result = node.commit('record held by an ignore rule')
    assert '.fractal/main.task/memory/held.md' in result
    # a later commit finds the record tracked, the archive still refused
    (project_dir / 'work.txt').write_text('work\n', encoding='utf-8')
    node.commit('work beside the tracked record')

    for revision in ('HEAD~1', 'HEAD'):
        index = _git(project_dir, 'show', f'{revision}:{_MEMORY_INDEX}').stdout
        assert ('[[held' in index) == indexed, revision
        assert 'dump.tar' not in index, revision


def test_memory_index_rebuild_waits_for_a_left_out_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing left out, the commit carries the refresh's index untouched.

    The rebuild exists for the files a commit leaves out, so an ordinary
    commit never starts it: no wiki run outside the repository, no notice,
    and the committed memory index is byte for byte what the refresh wrote.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    memory = node.node_dir / 'memory'
    (memory / 'notes.md').write_text(
        '---\nname: notes\ndesc: Notes.\n---\n\n# notes\n\n***\n',
        encoding='utf-8',
    )
    (memory / 'data.json').write_text('{}\n', encoding='utf-8')
    rebuilds = _watch_rebuilds(monkeypatch, project_dir, fail=False)
    result = node.commit('notes and data')

    assert rebuilds == []
    assert 'Memory index rebuilt' not in result
    cmd = ['git', 'show', f'HEAD:{_MEMORY_INDEX}']
    committed = subprocess.run(cmd, cwd=project_dir, capture_output=True).stdout
    assert committed == (memory / '_index.md').read_bytes()
    assert b'[[data.json' in committed


@pytest.mark.parametrize('force', [False, True], ids=['plain', 'force'])
def test_a_failed_memory_index_rebuild_never_fails_the_commit(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    force: bool,
) -> None:
    """A failed rebuild warns in one line, and the commit lands without it.

    The rebuild is upkeep, so a failure inside it must never cost the save,
    least of all the loop's backstop. The commit carries the memory index
    as staged, refused row and all, and the warning says the rebuild did not
    happen.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    memory = node.node_dir / 'memory'
    (memory / 'notes.md').write_text(
        '---\nname: notes\ndesc: Notes.\n---\n\n# notes\n\n***\n',
        encoding='utf-8',
    )
    (memory / 'dump.tar').write_bytes(b'binary')
    # the node's own refresh, so both modes stage the refused row
    subprocess.run(
        [_WIKI_BIN, 'update', f'--path={memory}'],
        capture_output=True,
        check=True,
    )
    rebuilds = _watch_rebuilds(monkeypatch, project_dir, fail=True)
    head_before = _head(project_dir)
    result = node.commit('notes beside a refused archive', force=force)

    assert _head(project_dir) != head_before
    assert len(rebuilds) == 1
    warnings = [
        line
        for line in result.splitlines()
        if line.startswith('Warning: memory index not rebuilt')
    ]
    assert len(warnings) == 1, result
    assert 'update refused' in warnings[0]
    index = _git(project_dir, 'show', f'HEAD:{_MEMORY_INDEX}').stdout
    assert '[[dump.tar' in index


# ------ scope boundaries


def test_commit_ignore_scope_bypasses_scope_but_not_lint(
    tmp_path: pathlib.Path,
) -> None:
    """``commit(ignore_scope=True)`` commits out-of-scope work yet still lints.

    The scope check is soft -- an agent sometimes must touch files outside its
    scope. ``--ignore-scope`` commits them (the default refuses) while keeping the
    lint gate, unlike ``--force`` (which drops both).
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    # configure git identity in the worktree
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    # baseline (clean tree), then scope the node to a subdir that holds no changes
    node.commit('baseline', init=True)
    node.config.set('scope', 'inscope')
    # an out-of-scope change (not under inscope/, the node data dir, or wiki)
    (project_dir / 'outside.txt').write_text('out-of-scope work\n', encoding='utf-8')

    # default: the scope check refuses (script exits 1 -> RuntimeError)
    with pytest.raises(RuntimeError):
        node.commit('touch outside')

    # --ignore-scope still lints: a failing lint blocks the commit
    lint = project_dir / '.fractal' / branch / 'scripts' / 'lint.sh'
    lint.write_text('#!/usr/bin/env bash\nexit 1\n', encoding='utf-8')
    with pytest.raises(RuntimeError):
        node.commit('touch outside', ignore_scope=True)

    # --ignore-scope commits the out-of-scope change once lint passes
    lint.write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')
    node.commit('touch outside', ignore_scope=True)
    result = _git(project_dir, 'ls-files', 'outside.txt')
    tracked = result.stdout
    assert 'outside.txt' in tracked

    # --ignore-scope is the narrow escape hatch; combining it with --force (which
    # skips both scope AND lint) is rejected, like every other flag pair
    with pytest.raises(ValueError, match='--ignore-scope cannot be used with --force'):
        node.commit('again', ignore_scope=True, force=True)


def test_multi_scope_commit_boundary(tmp_path: pathlib.Path) -> None:
    """Multiple ``scope`` roots are all committable; outside them refuses.

    ``--scope`` is repeatable, so a node can own
    several roots. A commit touching any recorded root (plus the
    always-allowed node data dir) passes the boundary check; a change
    outside every root is refused with each root named in the error.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(
        name='task',
        agent='claude',
        local=True,
        scope=['inscope_a', 'inscope_b'],
    )
    project_dir = _parse_project_dir(output)
    # configure git identity in the worktree
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    # baseline cleans the tree (sweeping init's root .gitattributes); stub the
    # lint gate (not under test) so the boundary check alone decides
    node.commit('baseline', init=True)
    branch = _resolve_branch(project_dir)
    lint = project_dir / '.fractal' / branch / 'scripts' / 'lint.sh'
    lint.write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')
    # work under BOTH scoped roots
    for scope_root in ('inscope_a', 'inscope_b'):
        (project_dir / scope_root).mkdir()
        work = project_dir / scope_root / 'work.txt'
        work.write_text('in-scope work\n', encoding='utf-8')
    node.commit('touch both roots')
    result = _git(project_dir, 'ls-files', 'inscope_a', 'inscope_b')
    tracked = result.stdout
    assert 'inscope_a/work.txt' in tracked
    assert 'inscope_b/work.txt' in tracked
    # a change outside every root refuses, naming each root
    (project_dir / 'outside.txt').write_text('out-of-scope work\n', encoding='utf-8')
    with pytest.raises(RuntimeError) as excinfo:
        node.commit('touch outside')
    assert 'inscope_a' in str(excinfo.value)
    assert 'inscope_b' in str(excinfo.value)


def test_dot_scope_root_bounds_the_whole_project(tmp_path: pathlib.Path) -> None:
    """A ``.`` scope root names the project itself, so nothing is out of scope.

    ``.`` is the one legal scope root that is not a subdirectory. It collapses
    to the project boundary instead of joining into a literal ``./`` prefix --
    which matches no git path, so it would put every changed file out of scope
    and refuse every commit the node ever makes.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(
        name='task',
        agent='claude',
        local=True,
        scope=['.'],
    )
    project_dir = _parse_project_dir(output)
    # configure git identity in the worktree
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    # '.' is its own canonical form, so the setter's normalization keeps it
    assert node.config.get('scope') == ['.']
    # baseline cleans the tree (sweeping init's root .gitattributes); stub the
    # lint gate (not under test) so the boundary check alone decides
    node.commit('baseline', init=True)
    branch = _resolve_branch(project_dir)
    lint = project_dir / '.fractal' / branch / 'scripts' / 'lint.sh'
    lint.write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')
    # work anywhere in the project commits: a nested dir and the project root
    (project_dir / 'nested').mkdir()
    (project_dir / 'nested' / 'work.txt').write_text('nested work\n', encoding='utf-8')
    (project_dir / 'root.txt').write_text('root work\n', encoding='utf-8')
    node.commit('touch the whole project')
    result = _git(project_dir, 'ls-files')
    tracked = result.stdout
    assert 'nested/work.txt' in tracked
    assert 'root.txt' in tracked


def test_scoped_commit_refuses_a_file_moved_into_scope(
    tmp_path: pathlib.Path,
) -> None:
    """A file moved into a scope root from outside it refuses by its source.

    git's rename detection names only a rename's destination, so a tracked
    file moved into a scope root would read as in-scope work, and the commit
    would land the deletion outside the scope that the merge's footprint
    check refuses. The scope check names the moved file's source.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True, scope=['src'])
    project_dir = _parse_project_dir(output)
    # configure git identity in the worktree
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    # baseline cleans the tree (sweeping init's root .gitattributes); stub the
    # lint gate (not under test) so the boundary check alone decides
    node.commit('baseline', init=True)
    branch = _resolve_branch(project_dir)
    lint = project_dir / '.fractal' / branch / 'scripts' / 'lint.sh'
    lint.write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')
    head = _head(project_dir)
    (project_dir / 'src').mkdir()
    _git(project_dir, 'mv', 'README.md', 'src/README.md')

    with pytest.raises(RuntimeError, match='Changes outside node scope') as refused:
        node.commit('move readme')

    # the refusal lists the source alone, and nothing was committed
    listing = str(refused.value).splitlines()[1:]
    assert listing == ['README.md']
    assert _head(project_dir) == head


@pytest.mark.parametrize(
    argnames=('scope', 'inside', 'strays'),
    argvalues=[
        pytest.param(None, ['app/wiki/note.md'], ['wiki/note.md'], id='project-bound'),
        pytest.param(
            ['docs'],
            ['app/docs/a.md'],
            ['app/other.txt', 'root.txt'],
            id='scoped-in-project',
        ),
    ],
)
def test_sub_project_commit_boundary(
    tmp_path: pathlib.Path,
    scope: Optional[list[str]],
    inside: list[str],
    strays: list[str],
) -> None:
    """A sub-project node's boundaries sit inside its project dir.

    A node initialized with ``--path <project>`` owns that directory: with
    no scope roots it is bounded to ``<project>/``, and its always-committable
    wiki is the project's own, so the repo-root ``wiki/`` is as foreign as
    any other path outside the project. Scope roots nest under the project
    too -- a root of ``docs`` admits ``<project>/docs/`` alone, not the rest
    of the project nor the repo root -- and the refusal names exactly the
    paths outside them.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    # a committed sub-project wiki -- the base-ref precondition for the init
    app_wiki = repo / 'app' / 'wiki' / '_index.md'
    app_wiki.parent.mkdir(parents=True)
    app_wiki.write_text('---\nname: app\n---\n# app\n\n***\n', encoding='utf-8')
    _git(repo, 'add', 'app')
    _git(repo, 'commit', '-m', 'add app wiki')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(
        name='task',
        agent='claude',
        local=True,
        path='app',
        scope=scope,
    )
    project_dir = _parse_project_dir(output)
    # configure git identity in the worktree
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    worktree = node.worktree
    # baseline cleans the tree (sweeping init's root .gitattributes); stub the
    # lint gate (not under test) so the boundary check alone decides
    node.commit('baseline', init=True)
    lint = node.node_dir / 'scripts' / 'lint.sh'
    lint.write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')
    # work inside and outside the boundaries refuses, naming
    # exactly the paths outside
    for path in (*inside, *strays):
        (worktree / path).parent.mkdir(parents=True, exist_ok=True)
        (worktree / path).write_text(f'{path}\n', encoding='utf-8')
    with pytest.raises(RuntimeError) as excinfo:
        node.commit('work in and out of the project')
    listed = str(excinfo.value).split(':\n', 1)[1].splitlines()
    assert listed == strays
    # the inside work alone commits
    for path in strays:
        (worktree / path).unlink()
    node.commit('work in the project')
    result = _git(worktree, 'ls-files', *inside)
    tracked = result.stdout.split()
    assert tracked == inside


@pytest.mark.parametrize(
    argnames=('root_wiki', 'wiki', 'project', 'exempt', 'stray'),
    argvalues=[
        pytest.param(
            'wiki', 'wiki', None, 'wiki/note.md', 'docs/note.md', id='default'
        ),
        pytest.param('docs', 'docs', None, 'docs/note.md', 'wiki/note.md', id='docs'),
        pytest.param(
            'wiki',
            'docs',
            'app',
            'app/docs/note.md',
            'app/wiki/note.md',
            id='sub-project',
        ),
        pytest.param(
            'docs',
            'wiki',
            'app',
            'app/wiki/note.md',
            'app/docs/note.md',
            id='sub-project-default',
        ),
    ],
)
def test_wiki_setting_moves_the_scope_exemption(
    tmp_path: pathlib.Path,
    root_wiki: str,
    wiki: str,
    project: Optional[str],
    exempt: str,
    stray: str,
) -> None:
    """The configured project-wiki folder is the one a scope exempts.

    A project naming another folder as its shared wiki in
    ``.fractal/.settings.json`` moves the always-committable exemption
    there: a scoped node commits pages under it, while ``wiki/`` becomes
    ordinary content outside the scope. A child that selects a sub-project
    follows that project's own setting, whatever the tree's root project
    names.
    """
    repo = _make_git_repo(tmp_path / 'repo', wiki=root_wiki)
    # a committed sub-project wiki (the base-ref precondition for the init),
    # with a settings file when the sub-project names another folder
    if project is not None:
        index = repo / project / wiki / '_index.md'
        index.parent.mkdir(parents=True)
        index.write_text('---\nname: app\n---\n# app\n\n***\n', encoding='utf-8')
        if wiki != 'wiki':
            settings = repo / project / '.fractal' / '.settings.json'
            settings.parent.mkdir()
            settings.write_text(json.dumps({'wiki': wiki}), encoding='utf-8')
        _git(repo, 'add', project)
        _git(repo, 'commit', '-m', 'add app wiki')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(
        name='task',
        agent='claude',
        local=True,
        path=project,
        scope=['src'],
    )
    project_dir = _parse_project_dir(output)
    # configure git identity in the worktree
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    # baseline cleans the tree (sweeping init's root .gitattributes); stub the
    # lint gate (not under test) so the boundary check alone decides
    node.commit('baseline', init=True)
    lint = node.node_dir / 'scripts' / 'lint.sh'
    lint.write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')
    # a page under each folder: only the configured one is exempt
    for path in (exempt, stray):
        (project_dir / path).parent.mkdir(parents=True, exist_ok=True)
        (project_dir / path).write_text(f'{path}\n', encoding='utf-8')
    with pytest.raises(RuntimeError) as excinfo:
        node.commit('pages in both folders')
    listed = str(excinfo.value).split(':\n', 1)[1].splitlines()
    assert listed == [stray]
    # the exempt page alone commits past the scope
    (project_dir / stray).unlink()
    node.commit('page in the project wiki')
    result = _git(project_dir, 'ls-files', exempt)
    assert result.stdout.split() == [exempt]


@pytest.mark.parametrize('value', ['../outside', '.', 'src/.git'])
def test_commit_refuses_an_invalid_stored_wiki(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    """A hand-edited ``wiki`` key fails the commit instead of moving the boundary.

    The key is fixed at init, so only a raw edit of ``config.json`` lands a
    bad value; every commit re-validates it, so a folder outside the project
    or over its machinery can never widen what a scoped node may commit.
    """
    _, child = _spawn_parent_child(git_repo, monkeypatch)
    config = json.loads(child.config.path.read_text(encoding='utf-8'))
    config['wiki'] = value
    child.config.path.write_text(json.dumps(config, indent=2), encoding='utf-8')
    (child.worktree / 'probe.md').write_text('# probe\n', encoding='utf-8')
    with pytest.raises(ValueError, match='wiki must'):
        child.commit('add probe')


def test_scoped_commit_handles_non_ascii_and_whitespace_paths(
    tmp_path: pathlib.Path,
) -> None:
    """The scope boundary reads non-ASCII and whitespace paths intact.

    git C-quotes non-ASCII paths in its porcelain output by default
    (``core.quotepath``); a scope check parsing quoted lines would refuse
    an in-scope ``src/café.md`` as out of scope (the quoted string starts
    with ``"``) -- breaking every scoped commit that touches such a file
    -- and would list a mangled path when a real violation is reported.
    A path starting with whitespace is fragile the same way: a stripping
    read loses the first listed path's leading whitespace.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True, scope=['src'])
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    node.commit('baseline', init=True)
    branch = _resolve_branch(project_dir)
    lint = project_dir / '.fractal' / branch / 'scripts' / 'lint.sh'
    lint.write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')
    # an in-scope non-ASCII path commits cleanly
    (project_dir / 'src').mkdir()
    (project_dir / 'src' / 'café.md').write_text('# café\n', encoding='utf-8')
    node.commit('non-ascii in scope')
    result = _git(project_dir, 'ls-files', '-z', 'src')
    tracked = result.stdout
    assert 'src/café.md' in tracked
    # out-of-scope non-ASCII and leading-whitespace paths refuse, naming the
    # real paths -- the space-prefixed file sorts first in git's listing,
    # exactly where a stripping read would lose its leading whitespace
    (project_dir / 'buiten_één.txt').write_text('x\n', encoding='utf-8')
    (project_dir / ' leading.txt').write_text('x\n', encoding='utf-8')
    with pytest.raises(RuntimeError) as excinfo:
        node.commit('awkward paths out of scope')
    assert 'buiten_één.txt' in str(excinfo.value)
    assert '\n leading.txt' in str(excinfo.value)


@pytest.mark.parametrize(
    argnames='base',
    argvalues=[
        pytest.param(None, id='no-base-file'),
        pytest.param('\n*.md text\n', id='leading-blank-line'),
        pytest.param('*.md text  \n', id='trailing-spaces'),
    ],
)
def test_scoped_child_baseline_commits_init_gitattributes(
    tmp_path: pathlib.Path,
    base: Optional[str],
) -> None:
    """A scoped child's baseline sweeps the ``.gitattributes`` init wrote.

    Node init writes a worktree-root ``.gitattributes`` (the memory wiki's
    ``merge=wiki`` attribute) when the base lacks it -- an init artifact
    outside every scope root, which a scoped child's baseline would otherwise
    refuse as out-of-scope, leaving the tree dirty forever. The baseline must
    sweep init's own artifact, like the user-init commit does -- over a base
    with no file, and over one whose own lines the tool appends to, compared
    byte for byte: a stripped read of HEAD's copy would lose a leading blank
    line or trailing whitespace and miss init's edit.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    if base is not None:
        (repo / '.gitattributes').write_text(base, encoding='utf-8')
        _git(repo, 'add', '.gitattributes')
        _git(repo, 'commit', '-m', 'own attributes')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(
        name='task',
        agent='claude',
        local=True,
        scope=['inscope'],
    )
    project_dir = _parse_project_dir(output)
    # configure git identity in the worktree
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    # the baseline sweeps init's own artifact -- no manual add/commit first
    node.commit('baseline', init=True)
    # the artifact is committed: tracked on the branch and clean in the tree
    result = _git(project_dir, 'ls-files', '.gitattributes')
    tracked = result.stdout
    assert '.gitattributes' in tracked
    result = _git(project_dir, 'status', '--porcelain', '--', '.gitattributes')
    status = result.stdout
    assert status == ''
    # the committed file is the base's own bytes followed by init's lines
    result = _git(project_dir, 'show', 'HEAD:.gitattributes')
    committed = result.stdout
    assert committed.startswith(base or ''), committed
    lines = [line for line in committed.splitlines() if line]
    assert lines[-2:] == ['# Wiki index merge driver', '**/_index.md merge=wiki']


def test_scoped_commit_refuses_a_foreign_line_beside_init_gitattributes(
    tmp_path: pathlib.Path,
) -> None:
    """A foreign line in init's uncommitted ``.gitattributes`` puts it out of scope.

    Init's ``.gitattributes`` edit is admitted past the scope check as a
    whole: HEAD's content followed by exactly the two lines the wiki tool
    appends. Any other line riding beside them -- an attribute an agent adds
    while the init edit is still uncommitted -- makes the file an ordinary
    out-of-scope path, so a scoped node's commit refuses it naming the file;
    with the foreign line gone, the same baseline sweeps the edit in.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(
        name='task',
        agent='claude',
        local=True,
        scope=['inscope'],
    )
    project_dir = _parse_project_dir(output)
    # configure git identity in the worktree
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    attributes = project_dir / '.gitattributes'
    init_edit = attributes.read_text(encoding='utf-8')
    # a foreign attribute beside init's lines refuses, naming the file
    attributes.write_text(init_edit + '*.bin binary\n', encoding='utf-8')
    with pytest.raises(RuntimeError, match=r'\.gitattributes'):
        node.commit('baseline', init=True)
    # init's edit alone sweeps in
    attributes.write_text(init_edit, encoding='utf-8')
    node.commit('baseline', init=True)
    result = _git(project_dir, 'ls-files', '.gitattributes')
    tracked = result.stdout
    assert '.gitattributes' in tracked


# ------ the dirty-tree check


def test_commit_check_detects_untracked_work(tmp_path: pathlib.Path) -> None:
    """``commit(check=True)`` reports an untracked-only dirty tree as dirty.

    The loop's post-iteration safety net runs ``fractal commit --check`` and
    force-commits when it reports the tree dirty (the loop's backstops). The
    tracked-only query ``git diff --name-only HEAD`` never lists untracked
    files, so a step that leaves only new untracked work would be reported
    clean -- the force-commit skipped, and a later ``--continue``
    (``git clean -fd``) would discard the work. ``--check`` must use a query
    that sees untracked files, and it sees only dirt the stage could commit:
    runtime artifacts barred by the stage excludes stay invisible even when
    info/exclude is stale.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    # configure git identity in the worktree
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)
    # baseline commit -- everything committed, tree clean
    node.commit('baseline', init=True)
    # a clean tree passes --check (no raise)
    node.commit(check=True)
    # runtime artifacts the stage may never commit stay invisible to the
    # check, even in a worktree whose info/exclude predates their entry --
    # strip the line to simulate the stale block
    system = project_dir / '.fractal' / 'main.task' / 'skills' / '.system' / 'x'
    system.mkdir(parents=True)
    (system / 'SKILL.md').write_text('engine-materialized\n', encoding='utf-8')
    exclude = repo / '.git' / 'info' / 'exclude'
    stale = [
        line
        for line in exclude.read_text(encoding='utf-8').splitlines()
        if line != '**/skills/.system/'
    ]
    exclude.write_text('\n'.join(stale) + '\n', encoding='utf-8')
    node.commit(check=True)
    # a step leaves only an untracked file (no tracked changes)
    (project_dir / 'leftover.txt').write_text('uncommitted work\n', encoding='utf-8')
    # --check must report the dirty tree (script exits 1 -> RuntimeError)
    with pytest.raises(RuntimeError, match='Uncommitted changes'):
        node.commit(check=True)


# ------ pre-commit hooks


def test_commit_surfaces_hook_aborted_commit(tmp_path: pathlib.Path) -> None:
    """A pre-commit hook that aborts the commit must surface, not be masked.

    A bare ``git commit -m ... || true`` tolerates the benign "nothing to
    commit" no-op -- but it would also swallow a non-zero exit from a
    pre-commit hook (black/isort reformatting and aborting, or a check-only
    hook failing): the script would report success and push while ``HEAD``
    never advanced, leaving the iteration's work uncommitted and exposed to a
    later ``--continue`` (``git clean -fd``). The genuine no-op must still
    exit 0; a real hook/commit failure must propagate (non-zero -> RuntimeError).
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    # configure git identity in the worktree
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)

    # baseline commit -- tree clean
    node.commit('baseline', init=True)
    head_before = _head(project_dir)
    # a clean tree is a genuine no-op -- commit must not raise
    node.commit('noop', init=True)
    # stub the lint gate (not under test) so the hook path alone decides
    lint = node.node_dir / 'scripts' / 'lint.sh'
    lint.write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')
    # install a pre-commit hook that aborts the commit (a check-only hook
    # failing, or black reformatting staged files and exiting non-zero);
    # it prints its findings the way linters do -- to stdout
    hooks_dir = project_dir / '.githooks'
    hooks_dir.mkdir()
    hook = hooks_dir / 'pre-commit'
    hook.write_text(
        '#!/bin/sh\necho "lint finding: bad format"\nexit 1\n',
        encoding='utf-8',
    )
    hook.chmod(0o755)
    _git(project_dir, 'config', 'core.hooksPath', f'{hooks_dir}')
    # leave real work -- the script stages it, then the hook aborts the commit
    (project_dir / 'work.txt').write_text('iteration work\n', encoding='utf-8')
    # a masked abort would read as success: the aborted commit must surface
    # (script exits non-zero -> RuntimeError), carrying the hook's findings
    # so the agent's remediation loop has something to act on
    with pytest.raises(RuntimeError, match='lint finding: bad format'):
        node.commit('work')
    # HEAD did not advance -- the work is genuinely uncommitted, so a masked
    # "success" would have been a lie that a later --continue could discard
    assert _head(project_dir) == head_before


def test_commit_retries_after_reformat_hook(tmp_path: pathlib.Path) -> None:
    """A reformat-and-abort hook is recovered: re-stage and retry once.

    The common black/isort case -- the hook reformats staged files and exits
    non-zero. With a pre-commit config present, the pipeline re-stages the
    hook's changes and retries the commit once, so HEAD advances with the
    reformatted content. (A check-only hook that changes nothing still surfaces
    -- ``test_commit_surfaces_hook_aborted_commit`` covers that.)
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    # configure git identity in the worktree
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    node = Node(project_dir)

    node.commit('baseline', init=True)
    head_before = _head(project_dir)

    # stub the lint gate (not under test) so the hook path alone decides
    lint = node.node_dir / 'scripts' / 'lint.sh'
    lint.write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')
    # a pre-commit config makes the recovery path eligible
    (project_dir / '.pre-commit-config.yaml').write_text(
        'repos: []\n',
        encoding='utf-8',
    )
    # hook: first run reformats the work file and aborts; the retry run succeeds
    marker = project_dir / '.hook_ran'
    work = project_dir / 'work.txt'
    hooks_dir = project_dir / '.githooks'
    hooks_dir.mkdir()
    hook = hooks_dir / 'pre-commit'
    hook.write_text(
        '#!/bin/sh\n'
        f'if [ -f "{marker}" ]; then exit 0; fi\n'
        f'touch "{marker}"\n'
        f'printf "reformatted\\n" > "{work}"\n'
        'exit 1\n',
        encoding='utf-8',
    )
    hook.chmod(0o755)
    _git(project_dir, 'config', 'core.hooksPath', f'{hooks_dir}')

    work.write_text('original work\n', encoding='utf-8')
    # first hook run reformats + aborts; the pipeline re-stages and retries
    node.commit('work')

    # HEAD advanced and the committed work is the hook's reformatted version
    assert _head(project_dir) != head_before
    committed = _git(project_dir, 'show', 'HEAD:work.txt')
    assert committed.stdout.strip() == 'reformatted'


# ------ script resolution


def test_commit_resolves_invoking_installation_cli(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """``Node.commit`` subprocesses resolve the invoking installation's ``fractal``.

    ``Node.commit`` runs the seeded ``lint.sh`` via a raw subprocess that does
    not flow through ``_run_script``, so ``_run_script``'s PATH prepend cannot
    cover it. The invoking interpreter's own bin dir must win over anything
    fronted on PATH.
    """
    _, child = _spawn_parent_child(git_repo, monkeypatch)
    # front a decoy `fractal` on PATH that records any consultation -- its
    # exit 1 lands in lint.sh's `|| echo` fallback, so the commit itself
    # still completes on the local path
    decoy_dir = tmp_path / 'decoy_bin'
    decoy_dir.mkdir()
    marker = decoy_dir / 'consulted'
    decoy = decoy_dir / 'fractal'
    decoy.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n', encoding='utf-8')
    decoy.chmod(0o755)
    path = os.environ['PATH']
    monkeypatch.setenv('PATH', f'{decoy_dir}{os.pathsep}{path}')
    # drive a real commit -- lint.sh shells back into fractal for its config read
    (child.worktree / 'probe.md').write_text('# probe\n', encoding='utf-8')
    child.commit('add commit-path probe')
    assert not marker.exists()


def test_commit_resolves_invoking_installation_wiki(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """``lint.sh``'s wiki lints resolve the invoking installation's ``wiki``.

    Besides ``fractal``, the seeded ``lint.sh`` shells out to ``wiki`` for the
    memory- and project-wiki lints, so the same PATH prepend must also front
    the invoking installation's ``wiki`` -- a fronted foreign install must not
    answer the lint runs.
    """
    _, child = _spawn_parent_child(git_repo, monkeypatch)
    # the child's memory wiki exists, so lint.sh's wiki lints execute
    assert (child.node_dir / 'memory' / '_index.md').is_file()
    # front a decoy `wiki` on PATH that records any consultation -- its exit 1
    # would land in lint.sh's `|| echo` warning, so the marker (not a failed
    # commit) is what surfaces a consultation
    decoy_dir = tmp_path / 'decoy_bin'
    decoy_dir.mkdir()
    marker = decoy_dir / 'consulted'
    decoy = decoy_dir / 'wiki'
    decoy.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n', encoding='utf-8')
    decoy.chmod(0o755)
    path = os.environ['PATH']
    monkeypatch.setenv('PATH', f'{decoy_dir}{os.pathsep}{path}')
    # drive a real commit -- lint.sh runs `wiki lint` on the memory wiki
    (child.worktree / 'probe.md').write_text('# probe\n', encoding='utf-8')
    child.commit('add wiki-path probe')
    assert not marker.exists()


def test_lint_runs_standalone_without_node_dir(
    initialized_node: dict[str, Any],
) -> None:
    """``lint.sh`` resolves its own paths when run outside the loop.

    The loop's launches export ``NODE_DIR``, but ``fractal commit`` (hence
    ``lint.sh``) also runs standalone -- e.g. a human committing from a
    plain shell -- where ``NODE_DIR`` is unset. ``lint.sh`` must derive it
    from its own location rather than abort under ``set -u`` with an
    unbound-variable error.
    """
    worktree = initialized_node['project_dir']
    node_dir = initialized_node['node_dir']
    env = {k: v for k, v in os.environ.items() if k != 'NODE_DIR'}
    lint_sh = node_dir / 'scripts' / 'lint.sh'
    result = subprocess.run(
        ['bash', f'{lint_sh}'],
        cwd=worktree,
        capture_output=True,
        text=True,
        env=env,
    )
    assert 'unbound variable' not in result.stderr


@pytest.mark.parametrize('wiki', ['wiki', 'docs'])
def test_lint_lints_the_configured_wiki(
    tmp_path: pathlib.Path,
    wiki: str,
) -> None:
    """``lint.sh`` lints the project wiki at the node's configured folder.

    The seeded hook reads the folder from the node's config, so a project
    naming ``docs`` has ``docs/`` linted and ``wiki/`` left alone as
    ordinary content.
    """
    repo = _make_git_repo(tmp_path / 'repo', wiki=wiki)
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    node = Node(project_dir)
    # front a decoy `wiki` that records each invocation's arguments
    decoy_dir = tmp_path / 'decoy_bin'
    decoy_dir.mkdir()
    calls = decoy_dir / 'calls'
    decoy = decoy_dir / 'wiki'
    decoy.write_text(f'#!/bin/sh\necho "$@" >>"{calls}"\n', encoding='utf-8')
    decoy.chmod(0o755)
    path = os.pathsep.join(
        [f'{decoy_dir}', f'{_FRACTAL_BIN.parent}', os.environ['PATH']]
    )
    env = {**os.environ, 'PATH': path}
    lint_sh = node.node_dir / 'scripts' / 'lint.sh'
    subprocess.run(
        ['bash', f'{lint_sh}'],
        cwd=project_dir,
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    lines = calls.read_text(encoding='utf-8').splitlines()
    assert f'lint --path={project_dir / wiki}' in lines
    # a corpus at wiki/ beside the configured folder is never linted
    linted = f'lint --path={project_dir / "wiki"}' in lines
    assert linted is (wiki == 'wiki')


# ------ helpers


def _head(project_dir: pathlib.Path) -> str:
    """The checked-out worktree's HEAD sha."""
    result = _git(project_dir, 'rev-parse', 'HEAD')
    return result.stdout.strip()


def _watch_rebuilds(
    monkeypatch: pytest.MonkeyPatch,
    worktree: pathlib.Path,
    *,
    fail: bool,
) -> list[str]:
    """Record each ``wiki update`` run outside ``worktree``, failing it on ``fail``.

    The memory index rebuild is the only wiki run outside the repository, so
    the record lists its runs, and a refused run stands in for any failure
    inside the rebuild.
    """
    real_run = subprocess.run
    paths: list[str] = []

    def _run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        is_update = isinstance(cmd, list) and cmd[1:2] == ['update']
        if is_update and pathlib.Path(cmd[0]).name == 'wiki':
            path = cmd[2].removeprefix('--path=')
            if not pathlib.Path(path).is_relative_to(worktree):
                paths.append(path)
                if fail:
                    return subprocess.CompletedProcess(cmd, 1, '', 'update refused')
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', _run)
    return paths
