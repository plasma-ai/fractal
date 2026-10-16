"""Functions for local git operations via subprocess."""

from __future__ import annotations

import pathlib
import subprocess
import typing
from typing import Literal, Optional

__all__ = []

#: Git's markers of an operation left unfinished in a worktree, each mapped to
#: what it names: a merge, cherry-pick, or revert stopped on a conflict (or a
#: squash staged but never committed), a rebase stopped part-way, and the index
#: lock a running git command holds and a killed one leaves behind.
OPERATION_MARKERS = {
    'MERGE_HEAD': 'a merge in progress',
    'SQUASH_MSG': 'a squash merge in progress',
    'CHERRY_PICK_HEAD': 'a cherry-pick in progress',
    'REVERT_HEAD': 'a revert in progress',
    'rebase-merge': 'a rebase in progress',
    'rebase-apply': 'a rebase in progress',
    'index.lock': 'a running git process or a stale index.lock',
}


@typing.overload
def run(
    cmd: list[str],
    *,
    cwd: Optional[pathlib.Path] = None,
    check: Literal[True] = True,
) -> str: ...


@typing.overload
def run(
    cmd: list[str],
    *,
    cwd: Optional[pathlib.Path] = None,
    check: Literal[False],
) -> Optional[str]: ...


def run(
    cmd: list[str],
    *,
    cwd: Optional[pathlib.Path] = None,
    check: bool = True,
) -> Optional[str]:
    """Run a git command and return stripped stdout.

    Args:
        cmd: Git subcommand and arguments (without ``git`` prefix).
        cwd: Working directory for the command.
        check: Raise ``RuntimeError`` on non-zero exit.

    Returns:
        Stripped stdout string, or ``None`` on non-zero
        exit when ``check`` is ``False``.

    """
    full_cmd = ['git']
    if cwd:
        full_cmd.extend(['-C', f'{cwd}'])
    full_cmd.extend(cmd)
    # a missing git binary is treated like a failed command, so callers that
    # pass check=False (e.g. the leading rev-parse) degrade to a clean no-op
    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        if check:
            cmd_string = ' '.join(cmd)
            raise RuntimeError(f'git {cmd_string} failed: {e}') from e
        return None
    if result.returncode != 0:
        if check:
            cmd_string = ' '.join(cmd)
            error = result.stderr.strip()
            raise RuntimeError(
                f'git {cmd_string} failed (exit {result.returncode}): {error!r}'
            )
        return None
    return result.stdout.strip()


def run_bytes(
    cmd: list[str],
    *,
    cwd: Optional[pathlib.Path] = None,
) -> Optional[bytes]:
    """Run a git command and return raw stdout bytes (``None`` on failure).

    The binary-safe, unstripped, non-raising counterpart to :func:`run`, for
    reading a file blob (``git show <ref>:<path>``) or a NUL-separated
    listing where text decoding and whitespace stripping would corrupt
    content -- a non-zero exit (e.g. the path absent at that revision)
    degrades to ``None``.

    Args:
        cmd: Git subcommand and arguments (without ``git`` prefix).
        cwd: Working directory for the command.

    Returns:
        Raw stdout bytes, or ``None`` on non-zero exit.

    """
    full_cmd = ['git']
    if cwd:
        full_cmd.extend(['-C', f'{cwd}'])
    full_cmd.extend(cmd)
    # a missing git binary is treated like a failed command, matching run's
    # check=False branch, so blob reads degrade to a clean no-op
    try:
        result = subprocess.run(full_cmd, capture_output=True)
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def toplevel(path: pathlib.Path, *, check: bool = True) -> Optional[pathlib.Path]:
    """Return the worktree top-level directory for a path.

    Args:
        path: A directory inside the worktree.
        check: Raise ``RuntimeError`` on non-zero exit.

    Returns:
        The worktree top-level, or ``None`` on non-zero exit
        when ``check`` is ``False``.

    """
    cmd = ['rev-parse', '--show-toplevel']
    result = run(cmd, cwd=path, check=check)
    if result is None:
        return None
    return pathlib.Path(result)


def common_dir(path: pathlib.Path) -> pathlib.Path:
    """Return the main git repo root (resolves through worktrees).

    Args:
        path: A directory inside any of the repo's worktrees.

    Returns:
        The main repository root.

    """
    cmd = ['rev-parse', '--git-common-dir']
    result = run(cmd, cwd=path)
    common = (path / result).resolve()
    # the parent-of-.git derivation only fits the standard layout -- a gitfile
    # repo (submodule, --separate-git-dir) nests its git dir elsewhere and
    # records its checkout in core.worktree, which --show-toplevel resolves
    # from inside the git dir
    if common.name == '.git':
        return common.parent
    cmd = ['rev-parse', '--show-toplevel']
    result = run(cmd, cwd=common)
    return pathlib.Path(result)


def branch(path: pathlib.Path, *, check: bool = True) -> Optional[str]:
    """Return the checked-out branch name for a worktree.

    Args:
        path: A directory inside the worktree.
        check: Raise ``RuntimeError`` on non-zero exit.

    Returns:
        The branch name, or ``None`` on non-zero exit
        when ``check`` is ``False``.

    """
    cmd = ['rev-parse', '--abbrev-ref', 'HEAD']
    return run(cmd, cwd=path, check=check)


def worktree_map(repo_dir: pathlib.Path) -> dict[str, pathlib.Path]:
    """Map each branch to its on-disk worktree path from one ``git worktree list``.

    The batched form of :func:`find_worktree` -- callers resolving many
    branches (e.g. a listing decorating a whole subtree) build the map once
    instead of spawning a ``git worktree list`` per branch. A worktree whose
    directory no longer exists (``rm -rf``'d out of band, still listed by git as
    ``prunable``) is dropped, so both the listing and the resolver agree a
    hand-removed node is gone.

    Args:
        repo_dir: Main repo root.

    Returns:
        Mapping of branch name to worktree path.

    """
    cmd = ['worktree', 'list', '--porcelain']
    output = run(cmd, cwd=repo_dir, check=False)
    result = {}
    worktree = None
    for line in (output or '').splitlines():
        if line.startswith('worktree '):
            worktree = line[len('worktree ') :]
        elif line.startswith('branch ') and worktree is not None:
            name = line[len('branch ') :].removeprefix('refs/heads/')
            # a worktree counts only if its dir survives on disk -- git still
            # lists an rm-rf'd worktree (as prunable), but it is really gone
            worktree_path = pathlib.Path(worktree)
            if worktree_path.is_dir():
                result[name] = worktree_path
    return result


def find_worktree(repo_dir: pathlib.Path, branch: str) -> Optional[pathlib.Path]:
    """Find the on-disk worktree path for a branch.

    A thin lookup over :func:`worktree_map`, so it shares the same on-disk
    probe -- a worktree ``rm -rf``'d out of band still lists (as ``prunable``)
    in git's porcelain, but its directory is gone, so it resolves as absent
    here rather than handing back a dead path.

    Args:
        repo_dir: Main repo root.
        branch: Branch name.

    Returns:
        Worktree path, or ``None`` if not found.

    """
    return worktree_map(repo_dir).get(branch)


def prunable(repo_dir: pathlib.Path) -> bool:
    """Return whether git lists any prunable (dead-on-disk) worktree.

    A worktree ``rm -rf``'d out of band lingers in ``git worktree list`` as
    ``prunable`` until ``git worktree prune`` clears its metadata (and the
    lingering entry keeps its branch ref checked out, resisting deletion) --
    callers use this to point at that one-shot cleanup.

    Args:
        repo_dir: Main repo root.

    Returns:
        Whether at least one listed worktree is prunable.

    """
    cmd = ['worktree', 'list', '--porcelain']
    output = run(cmd, cwd=repo_dir, check=False)
    return any(line.startswith('prunable ') for line in (output or '').splitlines())


def in_progress(path: pathlib.Path) -> list[tuple[str, pathlib.Path]]:
    """Return the git operations left unfinished in a worktree.

    One ``git rev-parse --git-path`` probe resolves every marker in
    :data:`OPERATION_MARKERS` -- a linked worktree keeps them in its own git
    dir -- and each marker present on disk names its operation.

    Args:
        path: The worktree's top-level directory.

    Returns:
        Each unfinished operation's description and marker path, in
        :data:`OPERATION_MARKERS` order.

    Raises:
        RuntimeError: If git cannot resolve the markers, so a caller guarding
            on the answer fails closed.

    """
    cmd = ['rev-parse']
    for marker in OPERATION_MARKERS:
        cmd.extend(['--git-path', marker])
    output = run(cmd, cwd=path)
    result = []
    for marker, resolved in zip(OPERATION_MARKERS, output.splitlines(), strict=True):
        # git answers relative to the worktree it ran in, else absolute
        marker_path = path / resolved
        if marker_path.exists():
            result.append((OPERATION_MARKERS[marker], marker_path))
    return result
