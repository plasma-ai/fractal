"""Test landing a paused node's committed work while it stays paused."""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time

import pytest

from fractal.constants import MERGE_LOCK_FILE, WORKTREES_FOLDER
from fractal.core.node import Node
from tests._helpers import _git, _stub_run_script

from .conftest import _parse_project_dir

__all__ = [
    'test_paused_merge_lands_twice_and_carries_the_frozen_step',
    'test_paused_merge_validation_refusal_changes_nothing',
    'test_paused_merge_refuses_an_advance_over_the_frozen_state',
    'test_paused_merge_refuses_an_unfinished_git_operation',
    'test_paused_merge_refuses_a_branch_that_moved_mid_merge',
    'test_paused_merge_refuses_a_node_that_left_paused_while_it_waited',
    'test_resume_waits_for_a_running_merge',
    'test_paused_continue_rollback_keeps_the_staged_resolution',
    'test_paused_no_op_continue_refusal_keeps_the_squash_message',
    'test_paused_merge_rolls_back_both_sides_after_the_commit',
    'test_paused_merge_names_what_a_failed_rollback_left',
    'test_paused_merge_lands_when_interrupted_after_the_branch_moved',
    'test_paused_merge_refuses_when_a_carry_check_fails',
    'test_paused_no_op_merge_advances_past_a_restored_path',
]

# the frozen step's dirty edit to a tracked file, its staged new file, and
# its untracked scratch file
FROZEN_EDIT = 'frozen mid-step edit\n'
FROZEN_STAGED = 'frozen staged file\n'
FROZEN_UNTRACKED = 'frozen scratch\n'

# a file the target tracks at the node's fork, a sibling's later edit of it,
# and the new file that sibling lands
SHARED = 'shared at the fork\n'
SHARED_EDIT = 'shared, edited by a sibling\n'
SIBLING = 'landed by a sibling\n'

# a standalone holder of the real merge lock file: acquire an exclusive
# fcntl.flock on argv[1], signal readiness via argv[2], then block -- the same
# lock and mode Node.merge and Node._resume contend
_LOCK_HOLDER = (
    'import fcntl, sys, time; '
    "f = open(sys.argv[1], 'a'); "
    'fcntl.flock(f, fcntl.LOCK_EX); '
    "open(sys.argv[2], 'w').close(); "
    'time.sleep(120)'
)


def test_paused_merge_lands_twice_and_carries_the_frozen_step(
    git_repo: pathlib.Path,
) -> None:
    """Land A, resume, land B: each lands once and the frozen state survives.

    A sibling landed on the target first, so the first advance moves its new
    file and its edit of a shared file into the frozen worktree, beside the
    step's dirty edit, staged file, and scratch file.
    """
    _, node = _paused_child(git_repo)
    _land_sibling(git_repo)
    message = 'Land the first findings\n\nA descriptive body.'

    output, _ = node.merge(message=message)

    # A landed under the given message, and the node is still parked
    assert 'Squash-merged' in output
    assert _git(git_repo, 'log', '-1', '--format=%B', 'main').stdout.strip() == message
    assert _git(git_repo, 'show', 'main:work.txt').stdout == 'A\n'
    assert node.status() == 'paused'
    # the node's branch gained the post-squash commit, its dirt intact
    first = _git(git_repo, 'rev-parse', 'main').stdout.strip()
    _git(node.worktree, 'merge-base', '--is-ancestor', first, 'HEAD')
    # the advance moved the sibling's landing into the frozen worktree
    for name, content in (('sib.txt', SIBLING), ('shared.txt', SHARED_EDIT)):
        assert (node.worktree / name).read_text(encoding='utf-8') == content
        assert _git(node.worktree, 'show', f'HEAD:{name}').stdout == content
    _assert_frozen(node)

    # resume, commit B over A's file alone, and park again
    node.status_set('active')
    (node.worktree / 'work.txt').write_text('A\nB\n', encoding='utf-8')
    _git(node.worktree, 'commit', '-m', 'work B', '--', 'work.txt')
    node.status_set('paused')

    output, _ = node.merge()

    # the second squash holds only B's hunk, under the default message
    assert 'Squash-merged' in output
    assert _git(git_repo, 'log', '-1', '--format=%s', 'main').stdout.strip() == (
        f'merge {node.branch}'
    )
    changed = _git(git_repo, 'diff', '--name-only', 'main~1', 'main').stdout
    assert changed.split() == ['work.txt']
    diff = _git(git_repo, 'diff', '-U0', 'main~1', 'main', '--', 'work.txt').stdout
    assert [line for line in diff.splitlines() if line[:1] in '+-'] == [
        '--- a/work.txt',
        '+++ b/work.txt',
        '+B',
    ]
    # history shows A and B landed once each
    subjects = _git(git_repo, 'log', '--format=%s', f'{first}~1..main').stdout
    assert subjects.splitlines() == [f'merge {node.branch}', message.splitlines()[0]]
    assert node.status() == 'paused'
    _assert_frozen(node)


def test_paused_merge_validation_refusal_changes_nothing(
    git_repo: pathlib.Path,
) -> None:
    """A failed validation leaves the target, the node, and its pause as they were."""
    _, node = _paused_child(git_repo)
    (git_repo / 'check.sh').write_text('exit 23\n', encoding='utf-8')
    _git(git_repo, 'add', 'check.sh')
    _git(git_repo, 'commit', '-m', 'validator')
    before = _snapshot(git_repo, node)

    with pytest.raises(RuntimeError, match='validation'):
        node.merge(validation_script='check.sh')

    assert _snapshot(git_repo, node) == before
    assert node.status() == 'paused'
    _assert_frozen(node)


@pytest.mark.parametrize(
    argnames=('landed', 'content', 'ignored', 'named'),
    argvalues=[
        # an edit to the file the frozen step holds dirty
        ('README.md', 'landed\n', False, 'README.md'),
        # a file where the frozen step keeps an untracked one
        ('scratch.txt', 'landed\n', False, 'scratch.txt'),
        # a file where the node keeps an ignored one
        ('ignored.txt', 'landed\n', True, 'ignored.txt'),
        # a directory where the frozen step keeps an untracked file
        ('scratch.txt/note.md', 'landed\n', False, 'scratch.txt'),
        # the very file and bytes the frozen step staged
        ('staged.txt', FROZEN_STAGED, False, 'staged.txt'),
    ],
    ids=['dirty', 'untracked', 'ignored', 'file-for-dir', 'staged'],
)
def test_paused_merge_refuses_an_advance_over_the_frozen_state(
    git_repo: pathlib.Path,
    landed: str,
    content: str,
    ignored: bool,
    named: str,
) -> None:
    """An advance that would rewrite the frozen state refuses the whole merge.

    The target lands a path the frozen worktree holds dirty, untracked, or
    ignored, a directory where it keeps a file, or the file it staged with
    those same bytes, which the advance would absorb as committed: the merge
    refuses before its commit, naming the path, and leaves the target, the
    node, and its pause as they were.
    """
    _, node = _paused_child(git_repo)
    # the node keeps a private file the repo's exclude file ignores
    if ignored:
        resolved = _git(git_repo, 'rev-parse', '--git-path', 'info/exclude').stdout
        with (git_repo / resolved.strip()).open('a', encoding='utf-8') as exclude:
            exclude.write(f'{landed}\n')
        (node.worktree / landed).write_text('private\n', encoding='utf-8')
    # the target lands the path, force-added past the same ignore
    (git_repo / landed).parent.mkdir(parents=True, exist_ok=True)
    (git_repo / landed).write_text(content, encoding='utf-8')
    _git(git_repo, 'add', '-f', landed)
    _git(git_repo, 'commit', '-m', f'land {landed}')
    before = _snapshot(git_repo, node)

    with pytest.raises(RuntimeError, match=re.escape(named)):
        node.merge()

    assert _snapshot(git_repo, node) == before
    assert node.status() == 'paused'
    _assert_frozen(node)


@pytest.mark.parametrize(
    argnames=('marker', 'operation'),
    argvalues=[
        # a merge, squash, cherry-pick, or revert stopped on a conflict
        ('MERGE_HEAD', 'a merge in progress'),
        ('SQUASH_MSG', 'a squash merge in progress'),
        ('CHERRY_PICK_HEAD', 'a cherry-pick in progress'),
        ('REVERT_HEAD', 'a revert in progress'),
        # a rebase stopped part-way, by either backend
        ('rebase-merge', 'a rebase in progress'),
        ('rebase-apply', 'a rebase in progress'),
        # a git command still running, or killed holding its lock
        ('index.lock', 'a running git process or a stale index.lock'),
    ],
    ids=[
        'merge',
        'squash',
        'cherry-pick',
        'revert',
        'rebase-merge',
        'rebase-apply',
        'index-lock',
    ],
)
def test_paused_merge_refuses_an_unfinished_git_operation(
    git_repo: pathlib.Path,
    marker: str,
    operation: str,
) -> None:
    """A frozen step stopped inside a git operation refuses the merge untouched.

    The carried advance would move the node's branch under the operation, so
    the merge refuses before anything changes, naming the operation and its
    marker: the target, the node, and its pause stay as they were.
    """
    _, node = _paused_child(git_repo)
    # plant the marker where git keeps it for the node's worktree
    resolved = _git(node.worktree, 'rev-parse', '--git-path', marker).stdout.strip()
    path = node.worktree / resolved
    if marker.startswith('rebase-'):
        path.mkdir()
    elif marker == 'index.lock':
        path.touch()
    elif marker == 'SQUASH_MSG':
        path.write_text('Squashed commit of the following:\n', encoding='utf-8')
    else:
        head = _git(node.worktree, 'rev-parse', 'HEAD').stdout
        path.write_text(head, encoding='utf-8')
    before = _snapshot(git_repo, node)

    with pytest.raises(RuntimeError, match=re.escape(f'{operation} ({path})')):
        node.merge()

    assert _snapshot(git_repo, node) == before
    assert node.status() == 'paused'
    _assert_frozen(node)


def test_paused_merge_refuses_a_branch_that_moved_mid_merge(
    git_repo: pathlib.Path,
) -> None:
    """A commit the node makes during the merge refuses it and survives.

    The advance moves only the commit the squash took: from a later one it
    would delete that commit's files as adjudicated away, so the merge
    refuses and restores the target, and the late commit keeps its file on
    the node's branch and disk. A validator that commits in the node's
    worktree stands in for a writer racing the merge.
    """
    _, node = _paused_child(git_repo)
    late = node.worktree / 'late.txt'
    (git_repo / 'check.sh').write_text(
        f'printf "late\\n" > "{late}"\n'
        f'git -C "{node.worktree}" add late.txt\n'
        f'git -C "{node.worktree}" commit -q -m late -- late.txt\n',
        encoding='utf-8',
    )
    _git(git_repo, 'add', 'check.sh')
    _git(git_repo, 'commit', '-m', 'a validator that commits in the node')
    head = _git(git_repo, 'rev-parse', 'HEAD').stdout

    moved = re.escape(f'{node.branch} moved during the merge')
    with pytest.raises(RuntimeError, match=moved):
        node.merge(validation_script='check.sh')

    # the target is back where it was...
    assert _git(git_repo, 'rev-parse', 'HEAD').stdout == head
    assert _git(git_repo, 'status', '--porcelain').stdout == ''
    # ...and the late commit kept its file on disk and in the node's tree
    assert late.read_text(encoding='utf-8') == 'late\n'
    tracked = _git(node.worktree, 'ls-tree', '--name-only', 'HEAD', 'late.txt').stdout
    assert tracked.strip() == 'late.txt'
    assert node.status() == 'paused'
    _assert_frozen(node)


def test_paused_merge_refuses_a_node_that_left_paused_while_it_waited(
    git_repo: pathlib.Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A node that leaves ``paused`` while its merge waits for the lock refuses it.

    The merge builds its arguments from the status it first read, so once it
    holds the repo's merge lock it reads the status again: a node that went
    active meanwhile refuses the merge, naming both statuses, with nothing
    changed on either side.
    """
    _, node = _paused_child(git_repo)
    _land_sibling(git_repo)
    before = _snapshot(git_repo, node)
    lock_file = git_repo / WORKTREES_FOLDER / MERGE_LOCK_FILE
    ready = tmp_path_factory.mktemp('merge_lock') / 'holder_ready'
    holder = subprocess.Popen(
        [sys.executable, '-c', _LOCK_HOLDER, f'{lock_file}', f'{ready}']
    )
    errors: list[Exception] = []

    def _merge() -> None:
        try:
            node.merge()
        except Exception as error:
            errors.append(error)

    merger = threading.Thread(target=_merge)
    try:
        _await_ready(ready, proc=holder, deadline=time.monotonic() + 10)
        merger.start()
        # the merge read paused and now waits behind the held lock
        merger.join(timeout=3)
        assert merger.is_alive()
        # the node goes active, then the holder dies and releases the lock
        node.status_set('active')
        holder.kill()
        holder.wait(timeout=5)
        merger.join(timeout=60)
        assert not merger.is_alive()
    finally:
        # the holder is a single leaf process, so a pid kill reaps it fully
        if holder.poll() is None:
            holder.kill()
            holder.wait()
        if merger.is_alive():
            merger.join(timeout=60)

    assert [f'{error}' for error in errors] == [
        'Cannot merge: the node went from paused to active while the merge'
        ' waited. Merge again.'
    ]
    assert _snapshot(git_repo, node) == before


def test_resume_waits_for_a_running_merge(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A resume relaunches a paused node only once a running merge lets go.

    A merge's carried advance rewrites the paused node's worktree, so the
    relaunch takes the repo's merge lock first: while another process holds
    it the relaunch says once that it waits, and it runs once the lock is
    released.
    """
    root, node = _paused_child(git_repo)
    # only the resume's own notices count
    caplog.clear()
    run_scripts = _stub_run_script(monkeypatch, Node)
    lock_file = git_repo / WORKTREES_FOLDER / MERGE_LOCK_FILE
    ready = tmp_path_factory.mktemp('merge_lock') / 'holder_ready'
    holder = subprocess.Popen(
        [sys.executable, '-c', _LOCK_HOLDER, f'{lock_file}', f'{ready}']
    )
    resumer = threading.Thread(target=root.resume)
    try:
        _await_ready(ready, proc=holder, deadline=time.monotonic() + 10)
        resumer.start()
        # the relaunch waits behind the held lock
        resumer.join(timeout=3)
        assert resumer.is_alive()
        assert run_scripts == []
        notice = (
            'Waiting for a merge in progress to finish before resuming'
            f' {node.branch}...'
        )
        assert caplog.messages == [notice], caplog.messages
        # the holder dies, the kernel releases the lock, and the relaunch runs
        holder.kill()
        holder.wait(timeout=5)
        resumer.join(timeout=60)
        assert not resumer.is_alive()
        assert [call[0] for call in run_scripts] == ['resume.sh']
        assert caplog.messages == [notice], caplog.messages
    finally:
        # the holder is a single leaf process, so a pid kill reaps it fully
        if holder.poll() is None:
            holder.kill()
            holder.wait()
        if resumer.is_alive():
            resumer.join(timeout=60)


def test_paused_continue_rollback_keeps_the_staged_resolution(
    git_repo: pathlib.Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A ``--continue`` rolled back after its commit keeps the resolution staged.

    After a conflicted merge the operator redoes the squash by hand and
    resolves it; a failure after the commit moves the target's HEAD back but
    leaves the resolution staged and the squash message in place, the node's
    frozen step intact, so the re-run lands the resolution.
    """
    _, node = _paused_child(git_repo)
    # the target commits its own work.txt, so the squash conflicts
    (git_repo / 'work.txt').write_text('target\n', encoding='utf-8')
    _git(git_repo, 'add', 'work.txt')
    _git(git_repo, 'commit', '-m', 'target work')
    with pytest.raises(RuntimeError, match='produced conflicts'):
        node.merge()
    # the operator redoes the squash by hand and stages a resolution of both
    redo = subprocess.run(
        ['git', 'merge', '--squash', node.branch],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert redo.returncode != 0, (redo.stdout, redo.stderr)
    resolved = 'A\ntarget\n'
    (git_repo / 'work.txt').write_text(resolved, encoding='utf-8')
    _git(git_repo, 'add', 'work.txt')
    blob = _git(git_repo, 'rev-parse', ':work.txt').stdout
    head = _git(git_repo, 'rev-parse', 'HEAD').stdout
    marker = _git(git_repo, 'rev-parse', '--git-path', 'SQUASH_MSG').stdout.strip()
    squash_msg = git_repo / marker
    message = squash_msg.read_bytes()
    child = _state(node.worktree)

    # the node's branch refuses to move once the target has committed
    with pytest.MonkeyPatch().context() as mp:
        _shim(
            mp,
            tmp_path_factory,
            'git',
            f'[[ " $* " == *" update-ref "*" refs/heads/{node.branch} "* ]] && exit 1\n',
        )
        with pytest.raises(RuntimeError, match='staged squash is left in place'):
            node.merge(continue_merge=True)

    # the target's HEAD is back with the resolution still staged and the
    # squash message there for the re-run, and the node is as it was
    assert _git(git_repo, 'rev-parse', 'HEAD').stdout == head
    assert _git(git_repo, 'rev-parse', ':work.txt').stdout == blob
    assert squash_msg.read_bytes() == message
    assert _state(node.worktree) == child
    _assert_frozen(node)

    # the re-run lands the resolution, the node still paused
    output, _ = node.merge(continue_merge=True)

    assert 'Squash-merged' in output
    assert _git(git_repo, 'show', 'main:work.txt').stdout == resolved
    assert node.status() == 'paused'
    _assert_frozen(node)


def test_paused_no_op_continue_refusal_keeps_the_squash_message(
    git_repo: pathlib.Path,
) -> None:
    """A no-op ``--continue`` whose carried advance refuses can still be re-run.

    A resolution that keeps the target's content for every change commits
    nothing, and the no-op arm clears the squash markers before its advance
    runs; when the advance would rewrite a frozen dirty file, the refusal
    restores the squash message, so the operator can move the file aside
    and re-run.
    """
    _, node = _paused_child(git_repo)
    # the target commits its own work.txt, so the squash conflicts, and edits
    # the README the frozen step holds dirty, so the advance cannot carry it
    (git_repo / 'work.txt').write_text('target\n', encoding='utf-8')
    (git_repo / 'README.md').write_text('target edit\n', encoding='utf-8')
    _git(git_repo, 'add', 'work.txt', 'README.md')
    _git(git_repo, 'commit', '-m', 'target work')
    with pytest.raises(RuntimeError, match='produced conflicts'):
        node.merge()
    # the operator redoes the squash and keeps the target's own work.txt
    redo = subprocess.run(
        ['git', 'merge', '--squash', node.branch],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert redo.returncode != 0, (redo.stdout, redo.stderr)
    _git(git_repo, 'checkout', '--ours', '--', 'work.txt')
    _git(git_repo, 'add', 'work.txt')
    head = _git(git_repo, 'rev-parse', 'HEAD').stdout
    marker = _git(git_repo, 'rev-parse', '--git-path', 'SQUASH_MSG').stdout.strip()
    squash_msg = git_repo / marker
    message = squash_msg.read_bytes()
    child = _state(node.worktree)

    with pytest.raises(RuntimeError, match='staged squash is left in place'):
        node.merge(continue_merge=True)

    # nothing moved, and the squash message is back for the re-run
    assert _git(git_repo, 'rev-parse', 'HEAD').stdout == head
    assert squash_msg.read_bytes() == message
    assert _state(node.worktree) == child
    assert node.status() == 'paused'
    _assert_frozen(node)


@pytest.mark.parametrize(
    argnames=('failure', 'step'),
    argvalues=[
        # the node's branch refuses to move
        ('update-ref', 'moving main.feature to its post-squash commit failed'),
        # the forward worktree update fails, or is cut short after writing
        ('read-tree', "updating main.feature's worktree failed"),
        ('read-tree-cut-short', "updating main.feature's worktree failed"),
        # the post-squash commit cannot be recorded
        ('commit-tree', "recording main.feature's post-squash commit failed"),
        # a target pre-commit hook stages a file past the judged tree
        (
            'tree-check',
            "main's commit does not hold the tree the advance was judged against",
        ),
        # an interrupt lands during the forward update, before the branch moves
        ('interrupt', 'merge of main.feature was interrupted'),
    ],
    ids=[
        'update-ref',
        'read-tree',
        'read-tree-cut-short',
        'commit-tree',
        'tree-check',
        'interrupt',
    ],
)
def test_paused_merge_rolls_back_both_sides_after_the_commit(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    failure: str,
    step: str,
) -> None:
    """A failure after the squash commit rolls the target and the node back whole.

    A sibling's landing gives the advance files to move into the frozen
    worktree; when the target's commit holds another tree than the one the
    advance was judged against, the post-squash commit cannot be recorded,
    the node's branch move or its worktree update fails -- the update cut
    short too, after it wrote some of those files -- or an interrupt lands
    before the branch moves, the merge names the step, returns the node's
    paths, and resets the target, so both sides match their pre-merge state,
    the node stays paused, and the target's merge event closes failed.
    """
    _, node = _paused_child(git_repo)
    _land_sibling(git_repo)
    before = _snapshot(git_repo, node)
    # the forward update alone: the reverse that follows runs for real
    fired = tmp_path_factory.mktemp('fired') / 'forward'
    forward = f'[[ " $* " == *" read-tree -m -u "[0-9a-f]* && ! -e "{fired}" ]]'
    real_git = shutil.which('git')
    body = ''
    if failure == 'update-ref':
        body = (
            f'[[ " $* " == *" update-ref "*" refs/heads/{node.branch} "* ]] && exit 1\n'
        )
    elif failure == 'read-tree':
        body = f'if {forward}; then\n    touch "{fired}"\n    exit 1\nfi\n'
    elif failure == 'read-tree-cut-short':
        # cut short once the sibling's new file and its shared edit are written
        body = (
            f'if {forward}; then\n'
            f'    touch "{fired}"\n'
            f'    cp "{git_repo}/sib.txt" "{git_repo}/shared.txt" "{node.worktree}/"\n'
            '    exit 130\n'
            'fi\n'
        )
    elif failure == 'commit-tree':
        body = '[[ " $* " == *" commit-tree "* ]] && exit 1\n'
    elif failure == 'tree-check':
        # the hook stages a file into the very commit, after the advance's check
        hook = _git(git_repo, 'rev-parse', '--git-path', 'hooks/pre-commit').stdout
        hook_path = git_repo / hook.strip()
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        hook_path.write_text(
            '#!/usr/bin/env bash\nprintf "hook\\n" > extra.txt\ngit add extra.txt\n',
            encoding='utf-8',
        )
        hook_path.chmod(0o755)
    else:
        # the TERM reaches the script while the forward update runs; bash
        # takes it once git returns, the node's branch not yet moved
        body = (
            f'if {forward}; then\n'
            f'    touch "{fired}"\n'
            f'    "{real_git}" "$@"\n'
            '    STATUS=$?\n'
            '    kill -TERM "$PPID"\n'
            '    exit "$STATUS"\n'
            'fi\n'
        )
    _shim(monkeypatch, tmp_path_factory, 'git', body)

    with pytest.raises(
        RuntimeError, match='rolled back and both worktrees restored'
    ) as raised:
        node.merge()

    assert step in f'{raised.value}', raised.value
    assert _snapshot(git_repo, node) == before
    assert node.status() == 'paused'
    _assert_frozen(node)
    merges = node.db.read('events', where={'event': 'merge'})
    assert [event['status'] for event in merges] == ['failed']


def test_paused_merge_names_what_a_failed_rollback_left(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A rollback that cannot restore a path names it and the reset to run.

    The forward update is cut short after writing the sibling's files, and
    both the reverse update and the restore of the shared file fail: the
    target still goes back, but the error says the merge could not be fully
    rolled back, names the path still changed and the node's old commit, and
    gives the exact reset that restores the target.
    """
    _, node = _paused_child(git_repo)
    _land_sibling(git_repo)
    head = _git(git_repo, 'rev-parse', 'HEAD').stdout.strip()
    child_old = _git(node.worktree, 'rev-parse', 'HEAD').stdout.strip()
    fired = tmp_path_factory.mktemp('fired') / 'forward'
    _shim(
        monkeypatch,
        tmp_path_factory,
        'git',
        # the forward update writes the sibling's files and is cut short...
        f'if [[ " $* " == *" read-tree -m -u "[0-9a-f]* ]]; then\n'
        f'    [[ -e "{fired}" ]] && exit 1\n'
        f'    touch "{fired}"\n'
        f'    cp "{git_repo}/sib.txt" "{git_repo}/shared.txt" "{node.worktree}/"\n'
        '    exit 130\n'
        'fi\n'
        # ...and nothing can write the shared file back
        '[[ " $* " == *" checkout "*":(literal)shared.txt"* ]] && exit 1\n'
        '[[ " $* " == *" restore "*":(literal)shared.txt"* ]] && exit 1\n',
    )

    with pytest.raises(RuntimeError, match='could NOT be fully rolled back') as raised:
        node.merge()

    error = f'{raised.value}'
    assert f"'git -C {git_repo} reset --hard {head}'" in error, error
    assert f'check that {node.branch} is at {child_old}' in error, error
    assert '(still changed: shared.txt)' in error, error
    # the target is back, and the node keeps its frozen step and pause
    assert _git(git_repo, 'rev-parse', 'HEAD').stdout.strip() == head
    assert _git(node.worktree, 'rev-parse', 'HEAD').stdout.strip() == child_old
    assert node.status() == 'paused'
    _assert_frozen(node)
    merges = node.db.read('events', where={'event': 'merge'})
    assert [event['status'] for event in merges] == ['failed']


def test_paused_merge_lands_when_interrupted_after_the_branch_moved(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """An interrupt after the node's branch moved finishes the carried merge.

    The branch moves only once the worktree update succeeded, so the carry
    has landed whole: the merge completes instead of reversing the update
    under the moved branch, the node sits on its post-squash commit with the
    sibling's landing and its frozen state in place, and the target's merge
    event closes completed.
    """
    _, node = _paused_child(git_repo)
    _land_sibling(git_repo)
    real_git = shutil.which('git')
    # the TERM reaches the script while update-ref runs; bash takes it once
    # git returns, the branch already moved
    _shim(
        monkeypatch,
        tmp_path_factory,
        'git',
        f'if [[ " $* " == *" update-ref "*" refs/heads/{node.branch} "* ]]; then\n'
        f'    "{real_git}" "$@"\n'
        '    STATUS=$?\n'
        '    kill -TERM "$PPID"\n'
        '    exit "$STATUS"\n'
        'fi\n',
    )

    output, _ = node.merge()

    assert 'Squash-merged' in output
    subject = _git(node.worktree, 'log', '-1', '--format=%s').stdout.strip()
    assert subject == 'merge main (post-squash)'
    # the index holds the post-squash commit's tree plus the staged file alone
    staged = _git(node.worktree, 'diff', '--cached', '--name-only', 'HEAD').stdout
    assert staged.split() == ['staged.txt']
    assert (node.worktree / 'sib.txt').read_text(encoding='utf-8') == SIBLING
    assert node.status() == 'paused'
    _assert_frozen(node)
    merges = node.db.read('events', where={'event': 'merge'})
    assert [event['status'] for event in merges] == ['completed']


@pytest.mark.parametrize(
    argnames=('tool', 'body', 'step'),
    argvalues=[
        # the node's index path, read with its branch and tip
        (
            'git',
            '[[ " $* " == *" rev-parse --git-path index "* ]] && exit 1\n',
            "reading main.feature's worktree failed",
        ),
        # the post-squash tree, written from a private index
        (
            'git',
            '[[ "$GIT_INDEX_FILE" == */carry-advance-index && " $* " == *" write-tree "* ]]'
            ' && exit 1\n',
            "building main.feature's post-squash tree failed",
        ),
        # the copy of the node's index the dry run refreshes
        (
            'cp',
            '[[ "${!#}" == */carry-index ]] && exit 1\n',
            "copying main.feature's index failed",
        ),
        # the target's staged tree the advance is judged against
        (
            'git',
            '[[ -z "${GIT_INDEX_FILE:-}" && " $* " == *" write-tree "* ]] && exit 1\n',
            'reading the staged squash of main.feature failed',
        ),
    ],
    ids=['read-worktree', 'build-tree', 'copy-index', 'staged-tree'],
)
def test_paused_merge_refuses_when_a_carry_check_fails(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    tool: str,
    body: str,
    step: str,
) -> None:
    """A carry check that cannot run refuses the merge before its commit.

    Reading the node's worktree, building its post-squash tree, copying its
    index for the dry run, or reading the target's staged squash fails
    closed: the merge refuses, naming the step, and both sides and the
    node's pause stay as they were.
    """
    _, node = _paused_child(git_repo)
    before = _snapshot(git_repo, node)
    _shim(monkeypatch, tmp_path_factory, tool, body)

    with pytest.raises(RuntimeError, match=re.escape(step)):
        node.merge()

    assert _snapshot(git_repo, node) == before
    assert node.status() == 'paused'
    _assert_frozen(node)


def test_paused_no_op_merge_advances_past_a_restored_path(
    git_repo: pathlib.Path,
) -> None:
    """A paused merge whose squash only edits the target's ``.fractal/`` advances.

    The restore returns the path to the target's content, so nothing lands;
    the drop is an adjudication, so the carried advance still moves the node
    past it, its frozen state intact, and the next merge does not re-offer
    the edit.
    """
    _, node = _paused_child(git_repo)
    notes = '.fractal/notes.txt'
    (git_repo / notes).write_text('target notes\n', encoding='utf-8')
    _git(git_repo, 'add', notes)
    _git(git_repo, 'commit', '-m', 'target notes')
    # the first landing carries the target's notes into the node, which then
    # edits only those
    node.merge()
    node.status_set('active')
    (node.worktree / notes).write_text('node notes\n', encoding='utf-8')
    _git(node.worktree, 'commit', '-m', 'edit the notes', '--', notes)
    node.status_set('paused')
    head = _git(git_repo, 'rev-parse', 'HEAD').stdout

    output, notices = node.merge()

    # nothing landed, the restore named the path, and the node moved past it
    assert 'Nothing to merge' in output
    assert notes in notices, notices
    assert _git(git_repo, 'rev-parse', 'HEAD').stdout == head
    subject = _git(node.worktree, 'log', '-1', '--format=%s').stdout.strip()
    assert subject == 'merge main (post-squash)'
    assert (node.worktree / notes).read_text(encoding='utf-8') == 'target notes\n'
    assert node.status() == 'paused'
    _assert_frozen(node)


# ------ helpers


def _paused_child(repo: pathlib.Path) -> tuple[Node, Node]:
    """Return a tree-wide paused root and a depth-1 child frozen mid-step.

    The child forked with ``shared.txt`` tracked and committed work A, then
    froze with an uncommitted edit to a tracked file, a staged new file, and
    an untracked file -- the shape pause leaves behind.
    """
    (repo / 'shared.txt').write_text(SHARED, encoding='utf-8')
    _git(repo, 'add', 'shared.txt')
    _git(repo, 'commit', '-m', 'shared file')
    root = Node(repo)
    root.init(agent='claude', user=True)
    node = Node(_parse_project_dir(root.init(name='feature')))
    (node.worktree / 'work.txt').write_text('A\n', encoding='utf-8')
    _git(node.worktree, 'add', 'work.txt')
    _git(node.worktree, 'commit', '-m', 'work A')
    (node.worktree / 'README.md').write_text(FROZEN_EDIT, encoding='utf-8')
    (node.worktree / 'staged.txt').write_text(FROZEN_STAGED, encoding='utf-8')
    _git(node.worktree, 'add', 'staged.txt')
    (node.worktree / 'scratch.txt').write_text(FROZEN_UNTRACKED, encoding='utf-8')
    # the tree-wide brake latches the root; the child parks paused
    root.pause()
    node.status_set('paused')
    return root, node


def _land_sibling(repo: pathlib.Path) -> None:
    """Commit a sibling's landing on the target: a new file and a shared edit."""
    (repo / 'sib.txt').write_text(SIBLING, encoding='utf-8')
    (repo / 'shared.txt').write_text(SHARED_EDIT, encoding='utf-8')
    _git(repo, 'add', 'sib.txt', 'shared.txt')
    _git(repo, 'commit', '-m', 'sibling landing')


def _assert_frozen(node: Node) -> None:
    """Assert the child's frozen mid-step state is byte-identical and in place.

    The tracked edit stays unstaged, the new file staged, and the scratch
    file untracked, each with its frozen bytes.
    """
    for name, content in (
        ('README.md', FROZEN_EDIT),
        ('staged.txt', FROZEN_STAGED),
        ('scratch.txt', FROZEN_UNTRACKED),
    ):
        assert (node.worktree / name).read_text(encoding='utf-8') == content
    status = _git(node.worktree, 'status', '--porcelain').stdout.splitlines()
    for entry in (' M README.md', 'A  staged.txt', '?? scratch.txt'):
        assert entry in status, status


def _await_ready(
    marker: pathlib.Path,
    *,
    proc: subprocess.Popen,
    deadline: float,
) -> None:
    """Block (bounded) until ``marker`` appears, failing if ``proc`` dies first."""
    while not marker.exists():
        assert proc.poll() is None, 'holder exited before acquiring the lock'
        assert time.monotonic() < deadline, 'holder never signalled ready'
        time.sleep(0.02)


def _shim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    name: str,
    body: str,
) -> None:
    """Put a pass-through ``name`` that runs ``body`` first at the head of ``PATH``.

    ``body`` is a bash block over the call's arguments that fails a chosen
    call or acts at it; the shim then execs the real binary. The bindir lives
    outside the repo, so it never shows up as untracked residue on the target.
    """
    real = shutil.which(name)
    assert real is not None, f'{name} must be on PATH'
    bindir = tmp_path_factory.mktemp('shim')
    shim = bindir / name
    shim.write_text(
        f'#!/usr/bin/env bash\n{body}exec "{real}" "$@"\n',
        encoding='utf-8',
    )
    shim.chmod(0o755)
    path = os.environ['PATH']
    monkeypatch.setenv('PATH', f'{bindir}{os.pathsep}{path}')


def _snapshot(repo: pathlib.Path, node: Node) -> tuple[str, ...]:
    """Capture both sides' HEAD, index, and worktree state."""
    return _state(repo) + _state(node.worktree)


def _state(worktree: pathlib.Path) -> tuple[str, ...]:
    """Capture one worktree's HEAD, index, and worktree state."""
    return tuple(
        _git(worktree, *args).stdout
        for args in (
            ('rev-parse', 'HEAD'),
            ('ls-files', '-s'),
            ('status', '--porcelain', '--untracked-files=all'),
        )
    )
