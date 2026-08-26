"""Tests for fractal's git-ignore surfaces (``info/exclude`` + seed self-ignore)."""

from __future__ import annotations

import os
import pathlib
import subprocess

import fractal.core
from fractal.core.node import Node

__all__ = [
    'test_exclude_template_ships_as_package_data',
    'test_init_writes_git_excludes',
    'test_init_excludes_subproject_user_seed',
    'test_git_exclude_anchors_workspace_dirs',
    'test_second_init_keeps_the_first_tree_hidden',
    'test_track_untrack_toggle_survives_exclude_rewrites',
    'test_git_exclude_works_without_a_commit',
]


def test_exclude_template_ships_as_package_data() -> None:
    """The git-excludes template must ship inside the ``fractal`` package.

    ``_git_exclude`` writes it into ``.git/info/exclude``, resolving it
    beside the module (like ``schema.sql``) -- so a non-editable install, where
    the package lives in ``site-packages`` with no repo root above it, still
    finds it.
    """
    assets = pathlib.Path(fractal.core.__file__).parent.parent / '_assets'
    template = assets / 'git' / 'exclude'
    assert template.is_file(), (
        'git-excludes template missing from the fractal package; under a '
        'non-editable install _git_exclude would raise FileNotFoundError'
    )
    patterns = template.read_text(encoding='utf-8')
    for pattern in (
        '.worktrees/',
        '.db',
        '.status',
        '.headless',
        'headless.log',
        'claude.err',
        'codex.err',
        'grok.err',
        'opencode.err',
        'omp.err',
    ):
        assert pattern in patterns, f'excludes template must contain {pattern!r}'


def test_init_writes_git_excludes(tmp_path: pathlib.Path) -> None:
    """Init owns fractal's ignores via ``.git/info/exclude``, not ``.gitignore``.

    ``init`` writes a managed block into the repo-local exclude (shared across
    every worktree), so runtime artifacts are ignored in both the repo root and
    inside a linked worktree; the committed ``.gitignore`` is never created; and
    re-running the writer is idempotent. The user node's own seed dir ignores
    itself via its own self-ignore file, so child node branches still track
    their seeds and no per-tree state rides the shared block.
    """
    # fresh repo with NO committed .gitignore
    repo = tmp_path / 'repo'
    repo.mkdir()
    for cmd in (
        ['git', 'init', '-b', 'main'],
        ['git', 'config', 'user.email', 'test@test.com'],
        ['git', 'config', 'user.name', 'Test'],
        # neutralize any global excludesFile so check-ignore attributes the
        # match to fractal's own surfaces (no committed .gitignore exists here)
        ['git', 'config', 'core.excludesFile', os.devnull],
    ):
        subprocess.run(cmd, cwd=repo, capture_output=True, check=True)
    (repo / 'README.md').write_text('# test\n', encoding='utf-8')
    subprocess.run(
        ['git', 'add', 'README.md'],
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

    # init writes the managed block; the committed .gitignore is never created
    node = Node(repo)
    node.init(user=True)
    exclude = repo / '.git' / 'info' / 'exclude'
    body = exclude.read_text(encoding='utf-8')
    assert '# >>> fractal >>>' in body
    assert not (repo / '.gitignore').exists()

    # the user node's own seed dir hides itself (its own ignore file, not the
    # shared block); a child's differently-named seed dir carries no such
    # file, so node branches keep tracking their seeds
    assert '.gitignore' in _check_ignore(repo, '.fractal/main/config.json')
    assert not _check_ignore(repo, '.fractal/main.child')

    # the exclude is shared across worktrees: artifacts are ignored in the repo
    # root and inside a linked worktree, citing info/exclude (which outranks any
    # global excludes file)
    worktree = repo / '.worktrees' / 'feature'
    subprocess.run(
        ['git', 'worktree', 'add', '-q', f'{worktree}', '-b', 'feature'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    assert 'info/exclude' in _check_ignore(repo, '.worktrees')
    assert 'info/exclude' in _check_ignore(worktree, '.db')
    assert 'info/exclude' in _check_ignore(worktree, 'claude.err')

    # idempotent: re-running the writer leaves exactly one managed block
    node._git_exclude()
    assert exclude.read_text(encoding='utf-8').count('# >>> fractal >>>') == 1


def test_init_excludes_subproject_user_seed(tmp_path: pathlib.Path) -> None:
    """A monorepo sub-project user node ignores ``<project>/.fractal/<branch>/``.

    The self-ignore rides the node dir wherever it nests, so the sub-project
    user seed is hidden on the top-level branch while a child's
    differently-named seed dir is not -- the ``project != '.'`` mirror of the
    repo-root case.
    """
    repo = _committed_repo(tmp_path)
    (repo / 'app').mkdir()
    Node(repo).init(path='app', user=True)
    assert '.gitignore' in _check_ignore(repo, 'app/.fractal/main/config.json')
    assert not _check_ignore(repo, 'app/.fractal/main.child')


def test_git_exclude_anchors_workspace_dirs(tmp_path: pathlib.Path) -> None:
    """The managed block hides workspace dirs at the repo root only.

    Unanchored directory patterns match at every depth, so an unanchored
    ``.worktrees/`` exclude would also hide committable node-dir trees like
    ``.fractal/<node>/artifacts/``. Workspace dirs that only ever exist at
    the repo root are anchored; node-dir runtime markers -- including the
    ``tmp/`` scratch dir and ``setup.log`` -- stay unanchored on purpose:
    they live under tracked ``.fractal/<branch>/`` dirs at arbitrary project
    depth, and are scratch by convention wherever else they appear (ignore
    rules never touch already-tracked files, which caps the collateral).
    """
    repo = _committed_repo(tmp_path)
    Node(repo).init(user=True)
    # workspace dirs: hidden at the root, committable at depth
    assert 'info/exclude' in _check_ignore(repo, '.worktrees/wt')
    assert not _check_ignore(repo, 'sub/dir/.worktrees/wt')
    # node-dir committable trees are never swallowed by the block
    assert not _check_ignore(repo, '.fractal/main.child/artifacts/data.csv')
    assert not _check_ignore(repo, '.fractal/main.child/runs/checker.py')
    # node-dir runtime markers stay ignored at any depth
    assert 'info/exclude' in _check_ignore(repo, '.fractal/main.child/.status')
    assert 'info/exclude' in _check_ignore(repo, '.fractal/main.child/claude.err')
    # the scratch dir and setup log are ignored at any depth, node dir or not
    assert 'info/exclude' in _check_ignore(repo, '.fractal/main.child/tmp/cache.json')
    assert 'info/exclude' in _check_ignore(repo, 'app/.fractal/main.a/tmp/pages.txt')
    assert 'info/exclude' in _check_ignore(repo, 'src/tmp/scratch.txt')


def test_second_init_keeps_the_first_tree_hidden(tmp_path: pathlib.Path) -> None:
    """A second tree's init never exposes the first tree's runtime dir.

    Each user seed dir hides itself, so initializing tree after tree in one
    repo leaves every runtime dir invisible -- ``git status`` stays silent
    about ``.fractal/`` no matter which init ran last.
    """
    repo = _committed_repo(tmp_path)
    Node(repo).init(user=True)
    # commit the wiki baseline so the status silence below is attributable
    # to the seed dirs alone
    Node(repo).commit('baseline', init=True)
    subprocess.run(
        ['git', 'checkout', '-q', '-b', 'second'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    Node(repo).init(user=True)
    status = subprocess.run(
        ['git', 'status', '--porcelain'],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert '.fractal' not in status
    # both runtime dirs exist and each hides itself
    assert '.gitignore' in _check_ignore(repo, '.fractal/main/config.json')
    assert '.gitignore' in _check_ignore(repo, '.fractal/second/config.json')


def test_track_untrack_toggle_survives_exclude_rewrites(
    tmp_path: pathlib.Path,
) -> None:
    """``fractal track``/``untrack`` is the only toggle; block rewrites preserve it.

    Tracking truth is the seed dir's own self-ignore file, so the shared
    block writer (re-run on every child init and start) can never reset the
    choice. A fresh init is untracked; a re-init is an idempotent no-op.
    """
    repo = _committed_repo(tmp_path)
    node = Node(repo)
    node.init(user=True)
    probe = '.fractal/main/config.json'
    # a fresh init is untracked: the seed dir hides itself, re-init included
    assert _check_ignore(repo, probe)
    node.init(user=True)
    assert _check_ignore(repo, probe)
    # fractal track lifts the self-ignore; a block rewrite preserves it
    _toggle(repo, 'track')
    assert not _check_ignore(repo, probe)
    node._git_exclude()
    assert not _check_ignore(repo, probe)
    # fractal untrack restores the ignore, surviving a rewrite the same way
    _toggle(repo, 'untrack')
    assert _check_ignore(repo, probe)
    node._git_exclude()
    assert _check_ignore(repo, probe)


def test_git_exclude_works_without_a_commit(tmp_path: pathlib.Path) -> None:
    """On a commitless repo, ``_git_exclude`` still writes the managed block.

    The block is static -- no user node or branch resolution is involved --
    so spawn/start rewrites work from any repo state.
    """
    repo = _git_repo(tmp_path)
    Node(repo)._git_exclude()
    block = (repo / '.git' / 'info' / 'exclude').read_text(encoding='utf-8')
    assert '.worktrees/' in block


# ------ helpers


def _git_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a bare-bones git repo (enough for ``_git_exclude`` to resolve)."""
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(
        ['git', 'init', '-b', 'main'],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    return repo


def _committed_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A git repo with an initial commit and no global excludes file.

    ``init(user=True)`` needs a branch (a commit), and neutralizing
    ``core.excludesFile`` lets ``check-ignore`` attribute matches to
    fractal's own surfaces.
    """
    repo = _git_repo(tmp_path)
    for cmd in (
        ['git', 'config', 'user.email', 'test@test.com'],
        ['git', 'config', 'user.name', 'Test'],
        ['git', 'config', 'core.excludesFile', os.devnull],
    ):
        subprocess.run(cmd, cwd=repo, capture_output=True, check=True)
    (repo / 'README.md').write_text('# test\n', encoding='utf-8')
    subprocess.run(
        ['git', 'add', 'README.md'],
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
    return repo


def _check_ignore(cwd: pathlib.Path, path: str) -> str:
    """Return ``git check-ignore -v`` output (source, line, pattern, and path)."""
    result = subprocess.run(
        ['git', 'check-ignore', '-v', path],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _toggle(repo: pathlib.Path, verb: str) -> None:
    """Run ``fractal track``/``untrack`` against ``repo`` via the real CLI."""
    subprocess.run(
        ['fractal', verb],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
