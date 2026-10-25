"""Test how fractal's git calls share a worktree's index lock.

git guards each worktree's index with an ``index.lock`` file, and a write
that finds the file present fails outright. fractal's reads never take the
lock -- a read that did would fail a concurrent ``git add`` -- and the commit
pipeline waits out a lock another process holds briefly. Every test drives
real git in repositories built under pytest tmp dirs.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

import pytest

import fractal.util.git
from fractal.core.node import Node
from tests._helpers import _await_ready, _git

from .conftest import _make_git_repo, _parse_project_dir

__all__ = [
    'test_engine_reads_leave_the_index_untouched',
    'test_commit_waits_out_a_briefly_held_index_lock',
    'test_commit_fails_with_git_message_on_a_held_index_lock',
]

# the console script beside the running interpreter -- a bare name would
# resolve through a shim to whatever install the ambient PATH front-runs
_FRACTAL_BIN = pathlib.Path(sys.executable).parent / 'fractal'

# an mtime no checkout records, so the index's stat data for a tracked file
# set to it is stale and a refreshing read would rewrite the index
_AGED_NS = 1_000_000_000 * 10**9

# creates the index lock as git does (an exclusive create), holds it until
# the release marker argv[2] appears, then removes it; the lock file is its
# own ready marker, and a holder never released exits 1 within a minute
_INDEX_LOCK_HOLDER = (
    'import os, pathlib, sys, time\n'
    'lock, release = (pathlib.Path(arg) for arg in sys.argv[1:])\n'
    'os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))\n'
    'deadline = time.monotonic() + 60\n'
    'while not release.exists():\n'
    '    if time.monotonic() > deadline:\n'
    '        sys.exit(1)\n'
    '    time.sleep(0.02)\n'
    'lock.unlink()\n'
)


# ------ reads


def _check(repo: pathlib.Path, node: Node) -> tuple[pathlib.Path, Callable]:
    """The loop's dirty check over a clean worktree."""
    node.commit('baseline', init=True)
    return node.worktree, lambda: node.commit(check=True)


def _baseline_refusal(repo: pathlib.Path, node: Node) -> tuple[pathlib.Path, Callable]:
    """A ``--init`` baseline refused over copied material, before any staging."""
    (node.worktree / 'reference.txt').write_text('material\n', encoding='utf-8')

    def _read() -> None:
        with pytest.raises(RuntimeError, match='commits only the node seed'):
            node.commit('configure', init=True)

    return node.worktree, _read


def _files_listing(repo: pathlib.Path, node: Node) -> tuple[pathlib.Path, Callable]:
    """The project-files listing a poller reads."""
    return node.worktree, lambda: node.files.list()


def _continue_refusal(repo: pathlib.Path, node: Node) -> tuple[pathlib.Path, Callable]:
    """A continue refused over uncommitted project files."""
    node.status_set('stopped')
    (node.worktree / 'notes.txt').write_text('uncommitted work\n', encoding='utf-8')

    def _read() -> None:
        with pytest.raises(RuntimeError, match='restore would discard'):
            node.start(continue_run=True)

    return node.worktree, _read


def _merge_refusal(repo: pathlib.Path, node: Node) -> tuple[pathlib.Path, Callable]:
    """A merge refused over the target's uncommitted changes."""
    (repo / 'README.md').write_text('# edited\n', encoding='utf-8')

    def _read() -> None:
        with pytest.raises(RuntimeError, match='has uncommitted changes'):
            node.merge()

    return repo, _read


def _merge_continue_refusal(
    repo: pathlib.Path,
    node: Node,
) -> tuple[pathlib.Path, Callable]:
    """A merge continue refused over an unstaged edit beside the squash."""
    node.commit('configure', init=True)
    _git(repo, 'merge', '--squash', node.branch)
    (repo / 'README.md').write_text('# edited\n', encoding='utf-8')

    def _read() -> None:
        with pytest.raises(RuntimeError, match='unstaged changes remain'):
            node.merge(continue_merge=True)

    return repo, _read


def _init_refusal(repo: pathlib.Path, node: Node) -> tuple[pathlib.Path, Callable]:
    """A top-level spawn refused over the root's uncommitted changes."""
    (repo / 'README.md').write_text('# edited\n', encoding='utf-8')

    def _read() -> None:
        with pytest.raises(RuntimeError, match='has uncommitted changes'):
            Node(repo).init(name='other', agent='claude', local=True)

    return repo, _read


@pytest.mark.parametrize(
    argnames='case',
    argvalues=[
        pytest.param(_check, id='commit-check'),
        pytest.param(_baseline_refusal, id='baseline-refusal'),
        pytest.param(_files_listing, id='files-listing'),
        pytest.param(_continue_refusal, id='continue-refusal'),
        pytest.param(_merge_refusal, id='merge-refusal'),
        pytest.param(_merge_continue_refusal, id='merge-continue-refusal'),
        pytest.param(_init_refusal, id='init-refusal'),
    ],
)
def test_engine_reads_leave_the_index_untouched(
    tmp_path: pathlib.Path,
    case: Callable[[pathlib.Path, Node], tuple[pathlib.Path, Callable]],
) -> None:
    """A read that touches the working tree never takes or rewrites the index.

    A tracked file's stat data goes stale first, so a read that refreshed
    the index would rewrite it under ``index.lock`` -- the lock a concurrent
    ``git add`` or ``git commit`` in the same worktree would then fail on.
    Each read leaves the index's bytes, mtime, and inode as they were, and
    no lock file is ever created and removed beside it.
    """
    repo, node = _child(tmp_path)
    worktree, read = case(repo, node)
    os.utime(worktree / '.gitignore', ns=(_AGED_NS, _AGED_NS))
    index = _git_path(worktree, 'index')
    before = _index_state(index)

    read()

    assert _index_state(index) == before
    assert not _git_path(worktree, 'index.lock').exists()


# ------ held locks


def _child_baseline(tmp_path: pathlib.Path) -> tuple[pathlib.Path, Callable]:
    """A child's ``--init`` baseline, staged through the shared git runner."""
    _, node = _child(tmp_path)
    return node.worktree, lambda: node.commit('configure', init=True)


def _user_baseline(tmp_path: pathlib.Path) -> tuple[pathlib.Path, Callable]:
    """A tracked user node's baseline, its seed staged by its own add."""
    repo = _make_git_repo(tmp_path / 'repo')
    node = Node(repo)
    node.init(agent='claude', user=True)
    subprocess.run(
        [f'{_FRACTAL_BIN}', 'track'],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    record = repo / '.fractal' / 'main' / 'memory' / 'notes.md'
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text('root notes\n', encoding='utf-8')
    return repo, lambda: node.commit('configure', init=True)


_PIPELINES = [
    pytest.param(_child_baseline, id='child-baseline'),
    pytest.param(_user_baseline, id='user-baseline'),
]

# the pipeline call the lock meets: the first staging write (held before the
# commit starts), or the commit itself (taken as the pipeline's git commit runs)
_MOMENTS = [
    pytest.param('stage', id='at-stage'),
    pytest.param('commit', id='at-commit'),
]


@pytest.mark.parametrize('pipeline', _PIPELINES)
@pytest.mark.parametrize('moment', _MOMENTS)
def test_commit_waits_out_a_briefly_held_index_lock(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    pipeline: Callable[[pathlib.Path], tuple[pathlib.Path, Callable]],
    moment: str,
) -> None:
    """A commit meeting another process's brief index lock waits and lands.

    The holder takes the lock as git does and lets it go while the pipeline
    waits; the pipeline call that meets it fails on the held lock, waits,
    and retries once, so the commit lands with nothing removed by fractal.
    """
    worktree, commit = pipeline(tmp_path)
    lock = _git_path(worktree, 'index.lock')
    release = tmp_path / 'gate_release'
    head = _git(worktree, 'rev-parse', 'HEAD').stdout
    holders: list[subprocess.Popen] = []
    met: list[subprocess.CompletedProcess] = []

    def _hold() -> None:
        holder = subprocess.Popen(
            [sys.executable, '-c', _INDEX_LOCK_HOLDER, f'{lock}', f'{release}']
        )
        holders.append(holder)
        _await_ready(lock, proc=holder, deadline=time.monotonic() + 10)

    real_index_locked = fractal.util.git.index_locked

    def releasing_index_locked(result: subprocess.CompletedProcess) -> bool:
        # a write that met the held lock releases the holder and outlasts it,
        # so the lock is gone by the one retry
        locked = real_index_locked(result)
        if locked:
            met.append(result)
            release.touch()
            for holder in holders:
                holder.wait(timeout=10)
        return locked

    # the shim pins the window between the failed write and its retry at the
    # lock check every retry site makes, so the wait itself is zeroed
    monkeypatch.setattr(fractal.util.git, 'index_locked', releasing_index_locked)
    monkeypatch.setattr(fractal.util.git, 'INDEX_LOCK_WAIT', 0)
    try:
        if moment == 'stage':
            _hold()
        else:
            _before_git_commit(monkeypatch, _hold)
        commit()
    finally:
        # the holder is a single leaf process, so a pid kill reaps it fully
        for holder in holders:
            if holder.poll() is None:
                holder.kill()
            holder.wait()

    # one write met the lock, the holder released it, and the commit landed
    assert len(met) == 1
    assert [holder.returncode for holder in holders] == [0]
    assert _git(worktree, 'rev-parse', 'HEAD').stdout != head
    subject = _git(worktree, 'log', '-1', '--format=%s').stdout.strip()
    assert subject.endswith(': init (configure)')


@pytest.mark.parametrize('pipeline', _PIPELINES)
@pytest.mark.parametrize('moment', _MOMENTS)
def test_commit_fails_with_git_message_on_a_held_index_lock(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    pipeline: Callable[[pathlib.Path], tuple[pathlib.Path, Callable]],
    moment: str,
) -> None:
    """A lock that outlasts the wait fails the commit with git's own message.

    A lock still held after the wait may belong to a live process (an
    editor's ``git commit``) as easily as to a killed one, so the pipeline
    never removes it: the commit fails with git's message naming the lock,
    not as a hook failure, and the lock file is left exactly as found.
    """
    worktree, commit = pipeline(tmp_path)
    lock = _git_path(worktree, 'index.lock')
    head = _git(worktree, 'rev-parse', 'HEAD').stdout
    found: list[tuple[bytes, int, int]] = []

    def _hold() -> None:
        lock.write_bytes(b'held\n')
        found.append(_index_state(lock)[:3])

    # the lock outlasts any wait, so the wait is zeroed
    monkeypatch.setattr(fractal.util.git, 'INDEX_LOCK_WAIT', 0)
    if moment == 'stage':
        _hold()
    else:
        _before_git_commit(monkeypatch, _hold)
    with pytest.raises(RuntimeError) as failed:
        commit()

    # git's message, in the shape the pipeline reports any git failure
    pattern = r"^git .*failed \(exit 128\): .*index\.lock': File exists"
    assert re.match(pattern, f'{failed.value}'), f'{failed.value}'
    # the lock is left as found, and nothing was committed
    assert [_index_state(lock)[:3]] == found
    assert _git(worktree, 'rev-parse', 'HEAD').stdout == head


# ------ helpers


def _child(tmp_path: pathlib.Path) -> tuple[pathlib.Path, Node]:
    """Return a repo and its top-level child node, the child's identity pinned."""
    repo = _make_git_repo(tmp_path / 'repo')
    Node(repo).init(agent='claude', user=True)
    output = Node(repo).init(name='task', agent='claude', local=True)
    project_dir = _parse_project_dir(output)
    _git(project_dir, 'config', 'user.email', 'test@test.com')
    _git(project_dir, 'config', 'user.name', 'Test')
    return repo, Node(project_dir)


def _git_path(worktree: pathlib.Path, name: str) -> pathlib.Path:
    """The absolute path of ``name`` in the worktree's own git dir."""
    result = _git(worktree, 'rev-parse', '--path-format=absolute', '--git-path', name)
    return pathlib.Path(result.stdout.strip())


def _index_state(path: pathlib.Path) -> tuple[bytes, int, int, int]:
    """A file's bytes, mtime, and inode, and its directory's mtime.

    The directory's mtime moves whenever a file is created or removed in
    it, so a lock taken and released beside the file shows there.
    """
    stat = path.stat()
    return (
        path.read_bytes(),
        stat.st_mtime_ns,
        stat.st_ino,
        path.parent.stat().st_mtime_ns,
    )


def _before_git_commit(
    monkeypatch: pytest.MonkeyPatch, hook: Callable[[], None]
) -> None:
    """Run ``hook`` once, just before the pipeline's first ``git commit`` runs."""
    real_run = subprocess.run
    pending = [hook]

    def _run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        is_commit = (
            isinstance(cmd, list)
            and cmd[:2] == ['git', '-C']
            and cmd[3:4] == ['commit']
        )
        if is_commit and pending:
            pending.pop()()
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', _run)
