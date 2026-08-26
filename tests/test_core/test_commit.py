"""Test the ``fractal.core.commit`` module."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from typing import Any

import pytest

import fractal.util
from fractal.core import commit
from fractal.core.node import Node

from .conftest import (
    _make_git_repo,
    _parse_project_dir,
    _resolve_branch,
    _spawn_parent_child,
)

#: the console script beside the running interpreter -- a bare name would
#: resolve through a shim to whatever install the ambient PATH front-runs
_FRACTAL_BIN = pathlib.Path(sys.executable).parent / 'fractal'

__all__ = [
    'test_user_node_commit_init_commits_baseline',
    'test_commit_pushes_unless_local',
    'test_commit_event_records_sha_and_emits_once',
    'test_commit_excludes_write_atomic_temp_files',
    'test_commit_excludes_registry_sidecars',
    'test_user_init_baseline_survives_a_hostile_external_ignore',
    'test_commit_stages_node_records_past_external_excludes',
    'test_stage_records_tolerates_a_vanished_held_file',
    'test_stage_records_survives_an_unreadable_held_file',
    'test_skip_alarm_covers_foreign_info_exclude_lines',
    'test_commit_excludes_registry_sidecars',
    'test_user_init_baseline_survives_a_hostile_external_ignore',
    'test_record_force_add_refuses_non_record_files',
    'test_estate_add_refuses_non_record_files_with_no_ignore_layer',
    'test_estate_commits_its_own_tool_state',
    'test_refused_estate_content_leaves_the_clean_check_quiet',
    'test_commit_stamps_iteration_from_args_or_open_row',
    'test_commit_rejects_prelabeled_agent_messages',
    'test_commit_refreshes_wiki_indexes',
    'test_commit_untracks_a_pretracked_wiki_cache',
    'test_commit_update_failure_blocks_commit_but_not_backstops',
    'test_force_commit_body_describes_the_sweep',
    'test_commit_ignore_scope_bypasses_scope_but_not_lint',
    'test_multi_scope_commit_boundary',
    'test_dot_scope_root_bounds_the_whole_project',
    'test_scoped_commit_handles_non_ascii_and_whitespace_paths',
    'test_scoped_child_baseline_commits_init_gitattributes',
    'test_commit_check_detects_untracked_work',
    'test_commit_surfaces_hook_aborted_commit',
    'test_commit_retries_after_reformat_hook',
    'test_commit_resolves_invoking_installation_cli',
    'test_commit_resolves_invoking_installation_wiki',
    'test_lint_runs_standalone_without_node_dir',
]


# ------ baselines and pushes


@pytest.mark.parametrize('track', [False, True])
def test_user_node_commit_init_commits_baseline(
    git_repo: pathlib.Path,
    track: bool,
) -> None:
    """``commit(init=True)`` baselines the project wiki -- plus node data when tracked.

    User nodes have no commit script, so the documented baseline step would
    otherwise fail; the ``--init`` path stages fractal's own artifacts (scoped, so
    other staged work is untouched) and commits them, while a non-init commit from
    a user node is rejected. The node's own ``.fractal/`` is git-ignored on the
    top-level branch by default, so it is committed only after ``fractal track``
    opts the tree in.
    """
    node = Node(git_repo)
    node.init(agent='claude', user=True)
    if track:
        subprocess.run(
            ['fractal', 'track'],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
    # a non-init commit from a user node is rejected
    with pytest.raises(RuntimeError, match='only --init is supported'):
        node.commit('x')

    def _head() -> str:
        result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    # the baseline commits without error and always tracks the project wiki; the
    # node's own seed is committed only on a tracked tree
    before = _head()
    # a stranded config write lock sits beside the config at commit time
    (git_repo / '.fractal' / 'main' / 'config.json.lock').touch()
    # engine-materialized system skills sit under the tracked skills dir
    system = git_repo / '.fractal' / 'main' / 'skills' / '.system' / 'imagegen'
    system.mkdir(parents=True)
    (system / 'SKILL.md').write_text('engine-materialized\n', encoding='utf-8')
    # the wiki tool's self-ignored derived cache sits inside the staged wiki
    cache = git_repo / 'wiki' / '.wiki' / 'cache'
    cache.mkdir(parents=True, exist_ok=True)
    (cache / '.gitignore').write_text('*\n', encoding='utf-8')
    (cache / 'word_counts.json').write_text('{}\n', encoding='utf-8')
    # a fresh clone carries no info/exclude at all, so the baseline cannot
    # lean on a block written at init -- the runtime artifacts beside the
    # seed must stay out of the commit on their own
    (git_repo / '.git' / 'info' / 'exclude').unlink()
    node.commit('configure', init=True)
    # a tracked tree makes a real commit (the seed is new); untracked, the wiki
    # is already committed by the fixture, so the baseline is legitimately a no-op
    if track:
        assert _head() != before
    result = subprocess.run(
        ['git', 'ls-files', '.fractal', 'wiki'],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = result.stdout
    assert 'wiki/_index.md' in tracked
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

    def _commit_node(name: str, *, local: bool) -> str:
        output = Node(repo).init(name=name, agent='claude', local=local)
        project_dir = _parse_project_dir(output)
        branch = _resolve_branch(project_dir)
        # configure git user in the worktree
        for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
            subprocess.run(
                ['git', 'config', key, val],
                cwd=project_dir,
                capture_output=True,
                check=True,
            )
        # baseline commit through the real path (--init skips the empty-memory
        # lint stub but still pushes unless local)
        (project_dir / f'{name}.txt').write_text('work\n', encoding='utf-8')
        Node(project_dir).commit(f'add {name}', init=True)
        return branch

    pushed = _commit_node('pushed', local=False)
    held = _commit_node('held', local=True)

    # the non-local node's branch reaches the remote; the local one does not
    result = subprocess.run(
        ['git', 'ls-remote', '--heads', f'{remote}'],
        capture_output=True,
        text=True,
        check=True,
    )
    refs = result.stdout
    assert pushed in refs
    assert held not in refs


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
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)

    def _head() -> str:
        result = subprocess.run(
            ['git', '-C', f'{project_dir}', 'rev-parse', 'HEAD'],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    # freshen both generated wikis up front -- the pipeline's index refresh
    # is byte-stable once run, so the no-op invocation below stays a no-op
    for wiki_dir in (
        project_dir / 'wiki',
        project_dir / '.fractal' / branch / 'memory',
    ):
        subprocess.run(
            ['wiki', 'update', f'--path={wiki_dir}'],
            capture_output=True,
            check=True,
        )
    # an --init baseline commit lands but emits no commit event
    (project_dir / 'seed.txt').write_text('seed\n', encoding='utf-8')
    node.commit('baseline', init=True)
    assert node.db.read('events', where={'event': 'commit'}) == []
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
    subprocess.run(
        ['git', 'config', 'core.hooksPath', f'{hooks_dir}'],
        cwd=project_dir,
        capture_output=True,
        check=True,
    )

    # a real (non-init) commit through the abort-and-retry path -- the marker
    # proves the hook actually fired, so the retry was exercised
    work.write_text('work\n', encoding='utf-8')
    node.commit('do the work')
    assert marker.exists()

    # exactly one commit event, keyed on the new sha (no double-log on retry)
    events = node.db.read('events', where={'event': 'commit'})
    assert [row['metadata'] for row in events] == [_head()]
    # a no-op invocation (clean tree, nothing staged) logs no event -- the
    # log counts commits, not command invocations
    node.commit('nothing to land')
    events = node.db.read('events', where={'event': 'commit'})
    assert [row['metadata'] for row in events] == [_head()]
    # the subject carries no repo-name prefix
    result = subprocess.run(
        ['git', '-C', f'{project_dir}', 'log', '-1', '--format=%s'],
        capture_output=True,
        text=True,
        check=True,
    )
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
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)

    # a real work file plus a write_atomic-shaped crash residue beside it
    (project_dir / 'work.txt').write_text('real work\n', encoding='utf-8')
    (project_dir / '.work.txt-a1b2c3.tmp').write_text('half\n', encoding='utf-8')
    node.commit('do the work', force=True)

    result = subprocess.run(
        ['git', '-C', f'{project_dir}', 'ls-files'],
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = result.stdout
    assert 'work.txt' in tracked
    assert '.work.txt-a1b2c3.tmp' not in tracked


def test_commit_excludes_registry_sidecars(tmp_path: pathlib.Path) -> None:
    """A legacy ``registry.db``'s SQLite sidecars never ride a work commit.

    SQLite writes ``-wal``/``-shm`` (WAL mode) or ``-journal`` (rollback)
    beside a live DB, so a swept sidecar is a torn point-in-time byte
    capture of a database another process is mid-write on, plus perpetual
    churn commits as it mutates -- the exact reason the modern spelling is
    barred as a ``.db``/``.db-*`` pair. The legacy spelling gets the same
    pair, in the stage excludes and the exclude template alike.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)

    # a work file plus a legacy DB and a live writer's sidecars beside it
    (project_dir / 'work.txt').write_text('real work\n', encoding='utf-8')
    sidecars = ('registry.db-wal', 'registry.db-shm', 'registry.db-journal')
    for name in ('registry.db', *sidecars):
        (project_dir / name).write_text('', encoding='utf-8')
    node.commit('do the work', force=True)

    result = subprocess.run(
        ['git', '-C', f'{project_dir}', 'ls-files'],
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = result.stdout
    assert 'work.txt' in tracked
    assert 'registry.db' not in tracked


def test_commit_stages_node_records_past_external_excludes(
    tmp_path: pathlib.Path,
) -> None:
    """Node records commit under a broad external ignore rule; runtime never.

    One ``/.fractal/`` line in the shared ``.git/info/exclude`` broke every
    node's commit fleet-wide: the plain add honors the rule and either
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
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)

    # the incident's exclude: everything under .fractal ignored, appended
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

    result = subprocess.run(
        ['git', '-C', f'{project_dir}', 'ls-files'],
        capture_output=True,
        text=True,
        check=True,
    )
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
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)

    # the incident's exclude: everything under .fractal externally ignored,
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

    result = subprocess.run(
        ['git', '-C', f'{project_dir}', 'ls-files'],
        capture_output=True,
        text=True,
        check=True,
    )
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
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)
    # the incident's exclude: everything under .fractal externally ignored,
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

    tracked = subprocess.run(
        ['git', '-C', f'{project_dir}', 'ls-files'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # the healthy record landed; the unreadable one is named, never fatal
    assert 'memory/irreplaceable.md' in tracked
    assert 'plans/0099-doomed.md' not in tracked
    assert 'could not be staged' in result
    assert 'plans/0099-doomed.md' in result


def test_skip_alarm_covers_foreign_info_exclude_lines(
    tmp_path: pathlib.Path,
) -> None:
    """A foreign ``info/exclude`` line alarms; fractal's own block stays silent.

    ``info/exclude`` is a user surface first -- fractal manages only its
    marker-delimited block -- so a user line there eats a deliverable
    exactly like a tracked ``.gitignore`` pattern and must count into the
    ignore-skip warning. The suppression keys on the block's line span,
    never the whole file.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
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


def test_commit_excludes_registry_sidecars(tmp_path: pathlib.Path) -> None:
    """A registry database's SQLite sidecars never ride a commit.

    ``registry.db-wal``/``-shm``/``-journal`` are mid-write runtime state:
    committing one stages a torn snapshot of a database whose main file is
    excluded, and the pair can never be read back consistently. The main
    file was fenced already; the sidecars share its fate.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)
    folder = node.node_dir.parent
    for name in ('registry.db', 'registry.db-wal', 'registry.db-shm'):
        (folder / name).write_bytes(b'sqlite')
    (project_dir / 'work.txt').write_text('real work\n', encoding='utf-8')
    node.commit('sidecar sweep', force=True)

    tracked = subprocess.run(
        ['git', '-C', f'{project_dir}', 'ls-files'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert 'work.txt' in tracked
    for name in ('registry.db', 'registry.db-wal', 'registry.db-shm'):
        assert name not in tracked, name


def test_record_force_add_refuses_non_record_files(
    tmp_path: pathlib.Path,
) -> None:
    """The force pass stages records only, and says what it did.

    The layer this pass overrides -- a machine-local ignore -- is where a
    host fences its secrets, so a general "stage what is ignored" verb
    would silently commit a dotenv or a key a node parked in its estate.
    Only the canon-required record surfaces at text suffixes qualify; a
    non-record file is refused by name, and every force-add is reported
    on the commit output.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)
    # the incident's exclude: everything under .fractal ignored
    exclude = repo / '.git' / 'info' / 'exclude'
    with exclude.open('a', encoding='utf-8') as handle:
        handle.write('/.fractal/\n')
    # a record, plus secret-shaped files a node parked beside it
    memory = node.node_dir / 'memory' / 'state.md'
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text('finding of record\n', encoding='utf-8')
    (node.node_dir / '.env').write_text('TOKEN=hunter2\n', encoding='utf-8')
    (node.node_dir / 'id_rsa').write_text('PRIVATE KEY\n', encoding='utf-8')
    (node.node_dir / 'creds.pem').write_text('CERT\n', encoding='utf-8')
    (node.node_dir / 'memory' / 'dump.tar').write_bytes(b'binary')
    result = node.commit('record custody', force=True)

    tracked = subprocess.run(
        ['git', '-C', f'{project_dir}', 'ls-files'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # the record landed; nothing secret-shaped or binary did
    assert 'memory/state.md' in tracked
    for parked in ('.env', 'id_rsa', 'creds.pem', 'dump.tar'):
        assert parked not in tracked, parked
    # the pass reports both halves rather than leaving them to be inferred
    assert 'Force-staged' in result
    assert 'memory/state.md' in result
    assert 'not node records and were NOT force-staged' in result
    assert 'id_rsa' in result
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
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)
    # no hostile exclude this time: nothing fences the estate at all
    memory = node.node_dir / 'memory' / 'state.md'
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text('finding of record\n', encoding='utf-8')
    (node.node_dir / '.env').write_text(
        'AWS_SECRET_ACCESS_KEY=hunter2\n', encoding='utf-8'
    )
    (node.node_dir / 'id_rsa').write_text('PRIVATE KEY\n', encoding='utf-8')
    (node.node_dir / 'creds.pem').write_text('CERT\n', encoding='utf-8')
    (node.node_dir / 'memory' / 'dump.tar').write_bytes(b'binary')
    result = node.commit('plain add path')

    tracked = subprocess.run(
        ['git', '-C', f'{project_dir}', 'ls-files'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # the record landed; nothing secret-shaped or binary did
    assert 'memory/state.md' in tracked
    for parked in ('.env', 'id_rsa', 'creds.pem', 'dump.tar'):
        assert parked not in tracked, parked
    # the refusal is named, not inferred from an absent file
    assert 'are not node records' in result
    for parked in ('id_rsa', 'creds.pem', 'dump.tar'):
        assert parked in result, parked
    # a record edited after it is tracked still commits -- the allowlist
    # gates what the estate adds new, never the upkeep of its own history
    # (the memory wiki's index refresh owns the frontmatter around the body)
    memory.write_text('finding of record, revised\n', encoding='utf-8')
    node.commit('record upkeep')
    committed = subprocess.run(
        [
            'git',
            '-C',
            f'{project_dir}',
            'show',
            'HEAD:.fractal/main.task/memory/state.md',
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
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
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)
    (project_dir / 'work.txt').write_text('work\n', encoding='utf-8')
    result = node.commit('ordinary work')

    tracked = subprocess.run(
        ['git', '-C', f'{project_dir}', 'ls-files'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
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
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
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

    tracked = subprocess.run(
        ['git', '-C', f'{project_dir}', 'ls-files'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
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
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)
    node.commit('baseline', init=True)

    def _subject() -> str:
        result = subprocess.run(
            ['git', '-C', f'{project_dir}', 'log', '-1', '--format=%s'],
            capture_output=True,
            text=True,
            check=True,
        )
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
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
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


def test_commit_refreshes_wiki_indexes(tmp_path: pathlib.Path) -> None:
    """A commit refreshes both wiki indexes so current bytes ride the commit.

    Agents write pages without touching the generated ``_index.md`` files;
    the pipeline runs ``wiki update`` on the project and memory wikis before
    the lint gate, so the refreshed indexes land in the same commit as the
    work -- no dedicated refresh commits, no agent turns spent on it.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    # configure git identity in the worktree
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)
    node.commit('baseline', init=True)
    # new pages in both wikis, with the generated indexes left stale
    (project_dir / 'wiki' / 'topic.md').write_text(
        '---\nname: topic\ndesc: A topic page.\n---\n\n# topic\n\n***\n',
        encoding='utf-8',
    )
    (node.node_dir / 'memory' / 'finding.md').write_text(
        '---\nname: finding\ndesc: A finding.\n---\n\n# finding\n\n***\n',
        encoding='utf-8',
    )
    node.commit('add topic and finding pages')

    def _committed(path: str) -> str:
        result = subprocess.run(
            ['git', '-C', f'{project_dir}', 'show', f'HEAD:{path}'],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    # both committed indexes carry the regenerated link rows...
    assert '[[topic' in _committed('wiki/_index.md')
    assert '[[finding' in _committed(f'.fractal/{branch}/memory/_index.md')
    # ...and the refresh left nothing behind for a later commit to sweep
    result = subprocess.run(
        ['git', '-C', f'{project_dir}', 'status', '--porcelain'],
        capture_output=True,
        text=True,
        check=True,
    )
    status = result.stdout
    assert status == ''


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
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)
    # a legacy baseline that force-tracked the derived cache past its ignore
    wiki_dir = project_dir / 'wiki'
    subprocess.run(
        ['wiki', 'update', f'--path={wiki_dir}'],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'add', '-A'],
        cwd=project_dir,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'add', '-f', '--', 'wiki/.wiki/cache'],
        cwd=project_dir,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ['git', 'commit', '-m', 'legacy baseline tracks the cache'],
        cwd=project_dir,
        capture_output=True,
        check=True,
    )
    # a work commit drops the cache from tracking and keeps the disk copy
    (project_dir / 'work.txt').write_text('work\n', encoding='utf-8')
    node.commit('do the work')

    tracked = subprocess.run(
        ['git', '-C', f'{project_dir}', 'ls-files', 'wiki'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
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
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)

    def _head() -> str:
        result = subprocess.run(
            ['git', '-C', f'{project_dir}', 'rev-parse', 'HEAD'],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    # a conflict-marked wiki page -- `wiki update` refuses to write over it
    (project_dir / 'wiki' / 'broken.md').write_text(
        '---\nname: broken\ndesc: A broken page.\n---\n\n# broken\n\n***\n'
        '<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> other\n',
        encoding='utf-8',
    )
    # the --init baseline skips the refresh -- it lands despite the markers
    node.commit('baseline', init=True)
    head_before = _head()
    # a plain commit fails with the refusal named in the error, before landing
    (project_dir / 'work.txt').write_text('work\n', encoding='utf-8')
    with pytest.raises(RuntimeError, match='Merge conflict markers'):
        node.commit('add work')
    assert _head() == head_before
    # the force backstop skips the refresh -- the rescue always saves
    node.commit('rescued work', force=True)
    assert _head() != head_before


def test_force_commit_body_describes_the_sweep(tmp_path: pathlib.Path) -> None:
    """A force commit's body folds in the caller paragraph, warnings, and diffstat.

    The loop's backstop saves are force commits made when an iteration
    fails, so the commit must explain itself from git history alone: the
    caller's body paragraph (the failure reason and never-run tail), the
    ignore-skip and oversized-file warnings, and the staged diffstat.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    branch = _resolve_branch(project_dir)
    # configure git identity in the worktree
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)
    node.commit('baseline', init=True)
    # a tracked host ignore rule eats a workspace file (the skip warning), an
    # oversized file trips the size warning, and real work rides the sweep
    gitignore = project_dir / '.gitignore'
    gitignore.write_text(
        gitignore.read_text(encoding='utf-8') + 'eaten.log\n', encoding='utf-8'
    )
    (project_dir / 'eaten.log').write_text('eaten\n', encoding='utf-8')
    (project_dir / 'blob.bin').write_bytes(b'\0' * (11 * 1024 * 1024))
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
        result = subprocess.run(
            ['git', '-C', f'{project_dir}', 'log', '-1', f'--format={fmt}'],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    # the subject is the run-qualified backstop label
    assert _log('%s') == f'{branch}: iteration {run_id}.2 (failed on EXECUTE)'
    # the body carries the caller's paragraph, the record pass's notices,
    # both warnings, and a diffstat naming the swept files
    body = _log('%b')
    assert 'agent error (exit 2)' in body
    assert 'steps not run: REVIEW, COMMIT' in body
    assert 'Force-staged' in body
    assert 'memory/note.md' in body
    assert 'NOT force-staged' in body
    assert '.env' in body
    assert f'{project_dir}' not in body
    assert 'skipped by ignore rules' in body
    assert 'blob.bin (11MB)' in body
    assert 'work.txt' in body


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
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
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
    result = subprocess.run(
        ['git', '-C', f'{project_dir}', 'ls-files', 'outside.txt'],
        capture_output=True,
        text=True,
        check=True,
    )
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
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
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
    result = subprocess.run(
        ['git', '-C', f'{project_dir}', 'ls-files', 'inscope_a', 'inscope_b'],
        capture_output=True,
        text=True,
        check=True,
    )
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
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
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
    result = subprocess.run(
        ['git', '-C', f'{project_dir}', 'ls-files'],
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = result.stdout
    assert 'nested/work.txt' in tracked
    assert 'root.txt' in tracked


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
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)
    node.commit('baseline', init=True)
    branch = _resolve_branch(project_dir)
    lint = project_dir / '.fractal' / branch / 'scripts' / 'lint.sh'
    lint.write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')
    # an in-scope non-ASCII path commits cleanly
    (project_dir / 'src').mkdir()
    (project_dir / 'src' / 'café.md').write_text('# café\n', encoding='utf-8')
    node.commit('non-ascii in scope')
    result = subprocess.run(
        ['git', '-C', f'{project_dir}', 'ls-files', '-z', 'src'],
        capture_output=True,
        text=True,
        check=True,
    )
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


def test_scoped_child_baseline_commits_init_gitattributes(
    tmp_path: pathlib.Path,
) -> None:
    """A scoped child's baseline sweeps the ``.gitattributes`` init wrote.

    Node init writes a worktree-root ``.gitattributes`` (the
    memory wiki's ``merge=wiki`` attribute) when the base lacks it --
    an init artifact outside every scope root, which a scoped child's baseline
    would otherwise refuse as out-of-scope, leaving the tree dirty forever. The
    baseline must sweep init's own artifact, like the user-init commit does.
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
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)
    # the baseline sweeps init's own artifact -- no manual add/commit first
    node.commit('baseline', init=True)
    # the artifact is committed: tracked on the branch and clean in the tree
    result = subprocess.run(
        ['git', '-C', f'{project_dir}', 'ls-files', '.gitattributes'],
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = result.stdout
    assert '.gitattributes' in tracked
    result = subprocess.run(
        [
            'git',
            '-C',
            f'{project_dir}',
            'status',
            '--porcelain',
            '--',
            '.gitattributes',
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    status = result.stdout
    assert status == ''


# ------ the dirty-tree check


def test_commit_check_detects_untracked_work(tmp_path: pathlib.Path) -> None:
    """``commit(check=True)`` reports an untracked-only dirty tree as dirty.

    The loop's post-iteration safety net runs ``fractal commit --check`` and
    force-commits when it reports the tree dirty (the loop's backstops). The tracked-only
    query ``git diff --name-only HEAD`` never lists untracked files, so a step
    that leaves only new untracked work would be reported clean -- the
    force-commit skipped, and a later ``--continue`` (``git clean -fd``) would
    discard the work. ``--check`` must use a query that sees untracked files,
    and it sees only dirt the stage could commit: runtime artifacts barred by
    the stage excludes stay invisible even when info/exclude is stale.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    # configure git identity in the worktree
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
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
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)

    def _head() -> str:
        result = subprocess.run(
            ['git', '-C', f'{project_dir}', 'rev-parse', 'HEAD'],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    # baseline commit -- tree clean
    node.commit('baseline', init=True)
    head_before = _head()
    # a clean tree is a genuine no-op -- commit must not raise
    node.commit('noop', init=True)
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
    subprocess.run(
        ['git', 'config', 'core.hooksPath', f'{hooks_dir}'],
        cwd=project_dir,
        capture_output=True,
        check=True,
    )
    # leave real work -- the script stages it, then the hook aborts the commit
    (project_dir / 'work.txt').write_text('iteration work\n', encoding='utf-8')
    # a masked abort would read as success: the aborted commit must surface
    # (script exits non-zero -> RuntimeError), carrying the hook's findings
    # so the agent's remediation loop has something to act on
    with pytest.raises(RuntimeError, match='lint finding: bad format'):
        node.commit('work', init=True)
    # HEAD did not advance -- the work is genuinely uncommitted, so a masked
    # "success" would have been a lie that a later --continue could discard
    assert _head() == head_before


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
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
    node = Node(project_dir)

    def _head() -> str:
        result = subprocess.run(
            ['git', '-C', f'{project_dir}', 'rev-parse', 'HEAD'],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    node.commit('baseline', init=True)
    head_before = _head()

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
    subprocess.run(
        ['git', 'config', 'core.hooksPath', f'{hooks_dir}'],
        cwd=project_dir,
        capture_output=True,
        check=True,
    )

    work.write_text('original work\n', encoding='utf-8')
    # first hook run reformats + aborts; the pipeline re-stages and retries
    node.commit('work', init=True)

    # HEAD advanced and the committed work is the hook's reformatted version
    assert _head() != head_before
    committed = subprocess.run(
        ['git', '-C', f'{project_dir}', 'show', 'HEAD:work.txt'],
        capture_output=True,
        text=True,
        check=True,
    )
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
    # front a decoy `fractal` on PATH that records any consultation -- its exit 1
    # lands in lint.sh's `|| echo` fallback, so the commit itself still
    # completes on the local path
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
    ``lint.sh``) also runs standalone -- e.g. a human committing from a plain shell -- where
    ``NODE_DIR`` is unset. ``lint.sh`` must derive it from its own location rather
    than abort under ``set -u`` with an unbound-variable error.
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


def test_user_init_baseline_survives_a_hostile_external_ignore(
    tmp_path: pathlib.Path,
) -> None:
    """The baseline commits its seed and wiki past a broad external ignore.

    ``fractal init`` writes the user node's seed and the project wiki and
    must land them as the tree's baseline; a machine-local ``/.fractal/``
    line (or a host ``.gitignore``) would otherwise silently empty the
    pathspec and leave a tree whose baseline never happened -- the
    fleet-wide breakage this staging work exists to end.
    """
    repo = _make_git_repo(tmp_path / 'repo')
    for key, val in (('user.email', 'test@test.com'), ('user.name', 'Test')):
        subprocess.run(
            ['git', 'config', key, val],
            cwd=repo,
            capture_output=True,
            check=True,
        )
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

    tracked = subprocess.run(
        ['git', '-C', f'{repo}', 'ls-files'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # the seed and the wiki are on the branch despite both ignore rules
    assert '.fractal/main/config.json' in tracked
    assert 'wiki/_index.md' in tracked
    # runtime state still never rides the baseline
    assert '.status' not in tracked
    assert '.db' not in tracked
