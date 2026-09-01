"""Functions for node seed templates."""

from __future__ import annotations

import io
import pathlib
import subprocess
import tarfile
from typing import Any, Optional

import tomli_w

import fractal.util
from fractal.constants import CONFIG_FILE, FRACTAL_FOLDER, WORKTREES_FOLDER

__all__ = []

#: per-node template provenance file in the node's data directory
TEMPLATE_FILE = '_template.toml'


def locate(
    value: str,
    *,
    repo_dir: pathlib.Path,
) -> tuple[str, Optional[str], pathlib.Path]:
    """Resolve a ``--template`` flag value to its tracked folder.

    Splits the optional ``@<ref>`` suffix at the last ``@`` and resolves
    the path half -- absolute or cwd-relative -- to its containing
    worktree, which must be the main checkout or one of its
    ``.worktrees/`` entries. Tab-completion therefore works from the root
    and from inside a node worktree, and the recorded form is the same
    repo-relative POSIX path either way.

    Args:
        value: The flag value (``<path>[@<ref>]``).
        repo_dir: Main repo root.

    Returns:
        Tuple of the worktree-relative POSIX path, the ref (``None``
        when the value carries no suffix), and the containing worktree.

    Raises:
        ValueError: If the value does not parse or the path is refused.

    """
    # a folder name carrying '@' cannot be told from a ref suffix -- when
    # the whole value names a folder on disk, refuse it before the split
    # eats the '@' (the post-split component check covers the rest)
    if '@' in value and pathlib.Path(value).is_dir():
        raise ValueError(
            f'--template folder name contains "@": {value!r};'
            ' "@" separates the ref (<path>[@<ref>]).'
        )
    # split the ref suffix at the last '@' -- a folder name never carries
    # one, so the split is unambiguous
    head, sep, tail = value.rpartition('@')
    if sep and head and tail:
        given, ref = head, tail
    elif sep:
        raise ValueError(
            f'Invalid --template value: {value!r} (expected <path>[@<ref>]).'
        )
    else:
        given, ref = value, None
    # a '..' step is refused before resolution -- the recorded form is a
    # plain worktree-relative path
    if '..' in pathlib.Path(given).parts:
        raise ValueError(f'--template path may not contain ".." steps: {given!r}.')
    resolved = pathlib.Path(given).resolve()
    # locate the containing worktree through the nearest existing ancestor,
    # so a folder deleted from disk but tracked at the fork commit still
    # resolves
    probe = resolved
    while not probe.is_dir() and probe.parent != probe:
        probe = probe.parent
    found = fractal.util.git.toplevel(probe, check=False)
    if found is not None:
        found = found.resolve()
    inside = found is not None and (
        found == repo_dir or found.parent == repo_dir / WORKTREES_FOLDER
    )
    if not inside or found is None or not resolved.is_relative_to(found):
        raise ValueError(
            f'--template path is outside this repository: {given!r};'
            ' a template is a tracked folder in the repo (the main'
            ' checkout or a node worktree).'
        )
    rel = resolved.relative_to(found)
    if not rel.parts:
        raise ValueError(
            '--template names the worktree root; point it at a folder inside the repo.'
        )
    relpath = rel.as_posix()
    # machinery components are refused casefolded, so every rule written
    # for node data stays blind to templates
    parts = tuple(part.casefold() for part in rel.parts)
    if FRACTAL_FOLDER in parts or '.git' in parts:
        raise ValueError(
            f'--template path contains a fractal machinery component:'
            f' {relpath!r}; a template is ordinary project content, outside'
            f' {FRACTAL_FOLDER}/ and .git/.'
        )
    if parts[0] == WORKTREES_FOLDER:
        raise ValueError(
            f'--template path leads into {WORKTREES_FOLDER}/: {relpath!r};'
            ' worktree checkouts are tracked at no commit -- name the'
            " folder by the repo's own path."
        )
    if any('@' in part for part in rel.parts):
        raise ValueError(
            f'--template folder name contains "@": {relpath!r};'
            ' "@" separates the ref (<path>[@<ref>]).'
        )
    return relpath, ref, found


def materialize(
    *,
    worktree: pathlib.Path,
    path: str,
    commit: str,
    dest: pathlib.Path,
) -> pathlib.Path:
    """Extract a template folder at a commit into a directory.

    Reads the folder from git (``git archive``, run from the worktree
    root -- a pathspec is cwd-relative), so uncommitted edits never
    deploy. The archive keeps the path prefix, so the bundle lands at
    ``<dest>/<path>``.

    Args:
        worktree: Worktree root the path is recorded against.
        path: Worktree-relative template folder path (POSIX).
        commit: Commit to read the folder at.
        dest: Directory to extract into.

    Returns:
        The bundle root (``<dest>/<path>``).

    Raises:
        ValueError: If the folder is untracked at the commit, the commit
            is unknown, the path is not a template folder, or the folder
            carries a symlink.

    """
    archive = subprocess.run(
        ['git', 'archive', commit, '--', f':(literal){path}'],
        capture_output=True,
        cwd=worktree,
    )
    if archive.returncode != 0:
        stderr = archive.stderr.decode(errors='replace')
        # git exits 128 with a distinct message per missing half: the
        # pathspec miss (folder untracked at the commit) and the unknown rev
        if 'did not match any files' in stderr:
            on_disk = (
                ' (an uncommitted copy exists on disk)'
                if (worktree / path).exists()
                else ''
            )
            raise ValueError(
                f'Template folder {path!r} is not tracked at commit'
                f' {commit}{on_disk}; commit the folder on the branch the'
                ' child forks from, or pass @<ref> to read it at another'
                ' commit.'
            )
        if 'not a valid object name' in stderr:
            raise ValueError(f'Template ref does not resolve: {commit!r}.')
        raise RuntimeError(f'git archive failed: {stderr.strip()}')
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
        # a template is self-contained: init.sh dereferences skill links
        # and copies steps and scripts as regular files only, so a link
        # pointing outside the folder would dangle after extraction --
        # refuse by name instead
        for member in bundle.getmembers():
            if member.issym() or member.islnk():
                raise ValueError(
                    f'Template folder {path!r} carries a symlink:'
                    f' {member.name!r}; a template is self-contained --'
                    ' commit the target file in its place.'
                )
        bundle.extractall(dest, filter='data')
    root = dest / pathlib.PurePosixPath(path)
    if not root.is_dir():
        raise ValueError(f'--template points at a file, not a folder: {path!r}.')
    if not (root / CONFIG_FILE).is_file():
        raise ValueError(
            f'{path!r} is not a template folder: no {CONFIG_FILE} at'
            f' {commit}; the config preset (even empty) is what marks a'
            ' template folder.'
        )
    return root


def trim(
    bundle: pathlib.Path,
    *,
    include: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
) -> None:
    """Reduce a materialized bundle to its effective set.

    The effective set is every template file minus ``exclude``, or only
    ``include``; a directory entry covers its subtree. Files outside the
    set are deleted from the bundle -- downstream only ever sees the
    effective set -- and directories the deletions emptied are dropped,
    so a fully trimmed surface falls back to the inherit-or-package
    source.

    Args:
        bundle: The bundle root.
        include: Template-relative paths to keep (exclusive of ``exclude``).
        exclude: Template-relative paths to drop (exclusive of ``include``).

    Raises:
        ValueError: If an entry matches nothing in the template, or names
            ``config.json`` or ``_template.toml``.

    """
    if not include and not exclude:
        return
    flag = 'include' if include else 'exclude'
    files = sorted(
        entry.relative_to(bundle).as_posix()
        for entry in bundle.rglob('*')
        if entry.is_file()
    )
    covered: set[str] = set()
    for entry in include or exclude or []:
        normalized = pathlib.PurePosixPath(entry).as_posix()
        if normalized in (CONFIG_FILE, TEMPLATE_FILE):
            raise ValueError(
                f'--{flag} may not name {normalized!r}'
                ' (template machinery, not a seed surface).'
            )
        matched = [
            file
            for file in files
            if file == normalized or file.startswith(f'{normalized}/')
        ]
        if not matched:
            raise ValueError(
                f'--{flag} entry matches nothing in the template: {entry!r}.'
            )
        covered.update(matched)
    # delete outside the effective set -- the marker always stays: it is
    # the config preset, not a seed surface, and never a listing entry
    for file in files:
        if exclude:
            keep = file not in covered
        else:
            keep = file in covered or file == CONFIG_FILE
        if not keep:
            (bundle / file).unlink()
    # drop emptied directories, deepest first
    for folder in sorted(
        (entry for entry in bundle.rglob('*') if entry.is_dir()),
        reverse=True,
    ):
        if not any(folder.iterdir()):
            folder.rmdir()


def write_provenance(
    bundle: pathlib.Path,
    *,
    path: str,
    commit: str,
    values: dict[str, str],
    include: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
) -> None:
    """Write the ``_template.toml`` provenance file into a bundle.

    The file records what seeded the node: the repo-relative template
    ``path``, the ``commit`` actually read, the mutually exclusive
    ``include``/``exclude`` listing when one was given, and the
    ``[values]`` table of slot fills. The table goes last so the scalar
    keys stay at the top level.

    Args:
        bundle: The bundle root.
        path: Worktree-relative template folder path (POSIX).
        commit: The commit the folder was read at.
        values: Slot fills to record.
        include: Template-relative paths kept (exclusive of ``exclude``).
        exclude: Template-relative paths dropped (exclusive of ``include``).

    """
    data: dict[str, Any] = {'path': path, 'commit': commit}
    if include:
        data['include'] = list(include)
    if exclude:
        data['exclude'] = list(exclude)
    data['values'] = dict(values)
    text = tomli_w.dumps(data)
    (bundle / TEMPLATE_FILE).write_text(text, encoding='utf-8')
