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
    'test_paused_merge_refuses_an_advance_over_dirty_paths',
    'test_paused_merge_refuses_an_unfinished_git_operation',
    'test_paused_merge_refuses_a_branch_that_moved_mid_merge',
    'test_resume_waits_for_a_running_merge',
    'test_paused_continue_rollback_keeps_the_staged_resolution',
    'test_paused_no_op_continue_refusal_keeps_the_squash_message',
]

FROZEN_EDIT = 'frozen mid-step edit\n'
FROZEN_UNTRACKED = 'frozen scratch\n'

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
    """Land A, resume, land B: each lands once and the frozen state survives."""
    _, node = _paused_child(git_repo)
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
    _assert_frozen(node)

    # resume, commit B over A's file, and park again
    node.status_set('active')
    (node.worktree / 'work.txt').write_text('A\nB\n', encoding='utf-8')
    _git(node.worktree, 'add', 'work.txt')
    _git(node.worktree, 'commit', '-m', 'work B')
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


def test_paused_merge_refuses_an_advance_over_dirty_paths(
    git_repo: pathlib.Path,
) -> None:
    """An advance that would rewrite a frozen dirty file refuses the whole merge."""
    _, node = _paused_child(git_repo)
    # the target changes the file the node holds dirty
    (git_repo / 'README.md').write_text('target edit\n', encoding='utf-8')
    _git(git_repo, 'add', 'README.md')
    _git(git_repo, 'commit', '-m', 'target edit')
    before = _snapshot(git_repo, node)

    with pytest.raises(RuntimeError, match=r'README\.md'):
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


def test_resume_waits_for_a_running_merge(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A resume relaunches a paused node only once a running merge lets go.

    A merge's carried advance rewrites the paused node's worktree, so the
    relaunch takes the repo's merge lock first: while another process holds
    it the relaunch waits, and it runs once the lock is released.
    """
    root, _ = _paused_child(git_repo)
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
        # the holder dies, the kernel releases the lock, and the relaunch runs
        holder.kill()
        holder.wait(timeout=5)
        resumer.join(timeout=60)
        assert not resumer.is_alive()
        assert [call[0] for call in run_scripts] == ['resume.sh']
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


# ------ helpers


def _paused_child(repo: pathlib.Path) -> tuple[Node, Node]:
    """Return a tree-wide paused root and a depth-1 child frozen mid-step.

    The child committed work A, then froze with an uncommitted edit to a
    tracked file and an untracked file -- the shape pause leaves behind.
    """
    root = Node(repo)
    root.init(agent='claude', user=True)
    node = Node(_parse_project_dir(root.init(name='feature')))
    (node.worktree / 'work.txt').write_text('A\n', encoding='utf-8')
    _git(node.worktree, 'add', 'work.txt')
    _git(node.worktree, 'commit', '-m', 'work A')
    (node.worktree / 'README.md').write_text(FROZEN_EDIT, encoding='utf-8')
    (node.worktree / 'scratch.txt').write_text(FROZEN_UNTRACKED, encoding='utf-8')
    # the tree-wide brake latches the root; the child parks paused
    root.pause()
    node.status_set('paused')
    return root, node


def _assert_frozen(node: Node) -> None:
    """Assert the child's frozen mid-step state is byte-identical."""
    assert (node.worktree / 'README.md').read_text(encoding='utf-8') == FROZEN_EDIT
    assert (node.worktree / 'scratch.txt').read_text(
        encoding='utf-8'
    ) == FROZEN_UNTRACKED


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
