"""Test landing a paused node's committed work while it stays paused."""

from __future__ import annotations

import pathlib

import pytest

from fractal.core.node import Node
from tests._helpers import _git

from .conftest import _parse_project_dir

__all__ = [
    'test_paused_merge_lands_twice_and_carries_the_frozen_step',
    'test_paused_merge_validation_refusal_changes_nothing',
    'test_paused_merge_refuses_an_advance_over_dirty_paths',
]

FROZEN_EDIT = 'frozen mid-step edit\n'
FROZEN_UNTRACKED = 'frozen scratch\n'


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


def _snapshot(repo: pathlib.Path, node: Node) -> tuple[str, ...]:
    """Capture both sides' HEAD, index, and worktree state."""
    return tuple(
        _git(worktree, *args).stdout
        for worktree in (repo, node.worktree)
        for args in (
            ('rev-parse', 'HEAD'),
            ('ls-files', '-s'),
            ('status', '--porcelain', '--untracked-files=all'),
        )
    )
