"""User-node ``init`` robustness and the ``commit --init`` baseline sweep.

Regressions here drive the real console script on real repos: ``init`` must
resolve its ``wiki`` step off its own interpreter, leave a repairable state
behind a failed wiki step, refuse to adopt a pre-existing docs directory,
refuse a slash branch with the rule named, record a sub-project target under
its subdir, flag an adopted wiki index that lacks the tool's frontmatter
stamps or a ``.gitattributes`` that lacks the wiki merge driver line without
rewriting either, and sweep everything init itself wrote -- never the user's
own pending edits -- into the baseline commit.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
from typing import Optional

import pytest

from fractal.core.node import Node
from tests._helpers import _git

from .conftest import _run

__all__ = [
    'test_init_prefers_the_environments_wiki_over_a_broken_shim',
    'test_init_names_the_missing_wiki_dependency',
    'test_init_failure_leaves_a_repairable_state',
    'test_init_refuses_to_adopt_a_preexisting_docs_directory',
    'test_init_refuses_a_slash_branch',
    'test_init_summarizes_creations_and_baseline_commit',
    'test_init_subproject_records_project',
    'test_init_warns_about_an_unstamped_wiki_index',
    'test_init_warns_about_a_missing_wiki_merge_driver_line',
    'test_commit_init_sweeps_the_gitattributes_edit',
    'test_commit_init_never_sweeps_user_gitattributes_edits',
]


# ------ tests


def test_init_prefers_the_environments_wiki_over_a_broken_shim(
    tmp_path: pathlib.Path,
) -> None:
    """``init`` succeeds when a broken pyenv-style shim shadows ``wiki`` on PATH.

    Orphaned pyenv shims resolve on PATH but exit 127, aborting init midway;
    the ``wiki`` beside fractal's own interpreter must win over the shim.
    """
    repo = _init_repo(tmp_path / 'repo')
    shims = _broken_wiki_shim(tmp_path)
    inherited = os.environ['PATH']
    path = f'{shims}{os.pathsep}{inherited}'
    result = _run(repo, 'init', PATH=path)
    assert result.returncode == 0, result.stderr
    assert (repo / 'wiki' / '_index.md').is_file()


def test_init_names_the_missing_wiki_dependency(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no ``wiki`` anywhere, ``init`` names plasma-wiki and the remedy.

    Runs in-process: emptying the interpreter's bin dir of a ``wiki`` sibling
    takes a ``sys.executable`` monkeypatch; the PATH holds only git.
    """
    repo = _init_repo(tmp_path / 'repo')
    # a bare interpreter dir (no wiki sibling) and a PATH holding only git
    bare = tmp_path / 'bare'
    bare.mkdir()
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    git = shutil.which('git')
    assert git is not None
    (bin_dir / 'git').symlink_to(git)
    monkeypatch.setattr(sys, 'executable', str(bare / 'python'))
    monkeypatch.setenv('PATH', str(bin_dir))
    with pytest.raises(RuntimeError, match='plasma-wiki'):
        Node(repo).init(user=True)


def test_init_failure_leaves_a_repairable_state(tmp_path: pathlib.Path) -> None:
    """A failed wiki step points at re-running, and the re-run repairs it.

    Config, db, and radio land before the wiki step, so a wiki failure
    strands a user node without a project wiki.
    """
    repo = _init_repo(tmp_path / 'repo')
    # sabotage the wiki step: a file squatting on the wiki directory path
    (repo / 'wiki').write_text('squatter\n', encoding='utf-8')
    result = _run(repo, 'init')
    assert result.returncode != 0
    assert 'wiki init failed' in result.stderr
    assert 're-run init' in result.stderr
    # the half-written state: user node data landed, the project wiki did not
    assert (repo / '.fractal' / 'main' / 'config.json').is_file()
    # clear the sabotage; a re-run completes the wiki instead of dying
    (repo / 'wiki').unlink()
    result = _run(repo, 'init')
    assert result.returncode == 0, result.stderr
    assert 'Re-created the missing project wiki' in result.stdout
    assert (repo / 'wiki' / '_index.md').is_file()


def test_init_refuses_to_adopt_a_preexisting_docs_directory(
    tmp_path: pathlib.Path,
) -> None:
    """A user's own ``wiki/`` docs directory fails init loudly, untouched.

    ``wiki init`` rewrites every page under its root (frontmatter,
    generated indexes), so a committed docs directory at the wiki path is
    refused with the remedy named -- never silently rewritten into the
    baseline.
    """
    repo = _init_repo(tmp_path / 'repo')
    docs = repo / 'wiki'
    docs.mkdir()
    (docs / 'Home.md').write_text('# Home\n\nWelcome.\n', encoding='utf-8')
    _git(repo, 'add', 'wiki')
    _git(repo, 'commit', '-m', 'docs')
    result = _run(repo, 'init')
    assert result.returncode != 0
    assert 'not a project wiki' in result.stderr
    assert 're-run init' in result.stderr
    # the docs are untouched: no rewrite, no wiki tool files, a clean tree
    assert (docs / 'Home.md').read_text(encoding='utf-8') == '# Home\n\nWelcome.\n'
    assert _git(repo, 'status', '--porcelain').stdout.strip() == ''


def test_init_refuses_a_slash_branch(tmp_path: pathlib.Path) -> None:
    """``init`` on a ``feat/x``-style branch names the rule, not a raw errno.

    Every per-branch artifact keys on the branch as a single path component,
    so a slash branch is refused up front with the remedy named -- and
    nothing partial (no ``.worktrees/``, no node data dir) is left behind.
    """
    repo = _init_repo(tmp_path / 'repo')
    _git(repo, 'checkout', '-b', 'feat/first')
    result = _run(repo, 'init')
    assert result.returncode != 0
    assert "'feat/first'" in result.stderr
    assert "containing '/' are not supported" in result.stderr
    # refused before any write: no cache dir, no node data dir
    assert not (repo / '.worktrees').exists()
    assert not (repo / '.fractal').exists()


def test_init_summarizes_creations_and_baseline_commit(
    tmp_path: pathlib.Path,
) -> None:
    """A fresh ``init`` says what it created and names the baseline commit.

    Every node spawn depends on the ``commit --init`` baseline (a worktree
    can only branch from a committed tree) -- the summary must surface it as
    the next step, not just report the branch.
    """
    repo = _init_repo(tmp_path / 'repo')
    result = _run(repo, 'init')
    assert result.returncode == 0, result.stderr
    assert 'Initialized user node on branch main' in result.stdout
    # what init created: the node data dir and the project wiki
    assert '.fractal/main' in result.stdout
    assert 'wiki' in result.stdout
    # the needed baseline commit, stated as the next step
    assert 'fractal commit' in result.stdout
    assert '--init' in result.stdout


def test_init_subproject_records_project(tmp_path: pathlib.Path) -> None:
    """``fractal init <subdir>`` creates a sub-project user node under it.

    A monorepo sub-project node nests its data under ``<subdir>/.fractal`` with
    the project recorded, and the prefix is applied exactly once (no doubling).
    """
    repo = _init_repo(tmp_path / 'repo')
    (repo / 'app').mkdir()
    # init the sub-project user node via the real CLI
    result = _run(repo, 'init', 'app')
    assert result.returncode == 0, result.stderr
    # data nests under app/, recorded as project 'app', not doubled
    config = repo / 'app' / '.fractal' / 'main' / 'config.json'
    assert config.is_file()
    assert json.loads(config.read_text(encoding='utf-8'))['project'] == 'app'
    cache = repo / '.worktrees' / '.project' / 'main'
    assert cache.read_text(encoding='utf-8').strip() == 'app'
    assert not (repo / 'app' / 'app').exists()


@pytest.mark.parametrize(
    argnames='stamped',
    argvalues=[
        pytest.param(False, id='hand-written'),
        pytest.param(True, id='stamped'),
    ],
)
def test_init_warns_about_an_unstamped_wiki_index(
    tmp_path: pathlib.Path,
    stamped: bool,
) -> None:
    """``init`` flags an adopted index without frontmatter stamps and rewrites nothing.

    Sibling nodes forking from an unstamped ``wiki/_index.md`` each stamp
    their own copy, and their merges then conflict on the ``created:`` line
    the merge driver cannot regenerate. Init leaves tracked files alone, so
    it warns with the ``wiki update`` remedy instead of rewriting the index,
    and the tree stays clean; an index already carrying ``created:`` draws
    no warning.
    """
    repo = _init_repo(tmp_path / 'repo')
    wiki = repo / 'wiki'
    wiki.mkdir()
    stamps = ''
    if stamped:
        stamps = (
            'desc: The project wiki.\n'
            'created: 2026-01-01T00:00:00Z\n'
            'updated: 2026-01-01T00:00:00Z\n'
        )
    (wiki / '_index.md').write_text(
        f'---\nname: wiki\n{stamps}---\n\n# wiki\n\n***\n', encoding='utf-8'
    )
    _git(repo, 'add', 'wiki')
    _git(repo, 'commit', '-m', 'adopted wiki')
    result = _run(repo, 'init')
    assert result.returncode == 0, result.stderr

    # the unstamped index is flagged with the remedy, the stamped one is not,
    # and neither is rewritten
    warning = (
        "Warning: wiki/_index.md carries no frontmatter stamps; run 'wiki update"
        " --path=wiki' and commit the result before initializing nodes, or sibling"
        ' nodes conflict on the index when they merge'
    )
    assert (warning in result.stderr) is (not stamped), result.stderr
    assert _git(repo, 'status', '--porcelain').stdout == ''


@pytest.mark.parametrize(
    argnames=('attributes', 'warned'),
    argvalues=[
        pytest.param(None, True, id='absent'),
        pytest.param('*.png binary\n', True, id='without-the-line'),
        pytest.param(
            '*.png binary\n**/_index.md merge=wiki\n', False, id='with-the-line'
        ),
    ],
)
def test_init_warns_about_a_missing_wiki_merge_driver_line(
    tmp_path: pathlib.Path,
    attributes: Optional[str],
    warned: bool,
) -> None:
    """``init`` flags an adopted wiki without the wiki merge driver attribute.

    ``wiki init`` writes ``**/_index.md merge=wiki`` to ``.gitattributes``
    for a fresh wiki, but an adopted wiki skips that step, and git reads the
    attribute from the target's own tree -- so wiki indexes that diverge on
    both sides conflict when they merge. Init leaves tracked files alone, so
    it warns with the line to append instead of writing it, whether the file
    is missing or merely lacks the line, and the tree stays clean; a
    ``.gitattributes`` already carrying the line draws no warning.
    """
    repo = _init_repo(tmp_path / 'repo')
    wiki = repo / 'wiki'
    wiki.mkdir()
    # a stamped index, so the attribute is the only thing init can flag
    (wiki / '_index.md').write_text(
        '---\nname: wiki\ndesc: The project wiki.\n'
        'created: 2026-01-01T00:00:00Z\nupdated: 2026-01-01T00:00:00Z\n'
        '---\n\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    if attributes is not None:
        (repo / '.gitattributes').write_text(attributes, encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'adopted wiki')
    result = _run(repo, 'init')
    assert result.returncode == 0, result.stderr

    # the missing line is flagged with the remedy, the present one is not,
    # and nothing is written either way
    warning = (
        'Warning: .gitattributes lacks the wiki index merge driver line; append'
        " '**/_index.md merge=wiki' to it and commit before initializing nodes,"
        ' or wiki indexes that diverge on both sides conflict when they merge'
    )
    assert (warning in result.stderr) is warned, result.stderr
    assert _git(repo, 'status', '--porcelain').stdout == ''


def test_commit_init_sweeps_the_gitattributes_edit(tmp_path: pathlib.Path) -> None:
    """``commit --init`` leaves a clean tree, ``.gitattributes`` included.

    ``wiki init`` writes the ``**/_index.md merge=wiki`` attribute and leaves
    committing it to the caller. The committed-line pin is exact so a future
    driver rename in the wiki package fails here loudly.
    """
    repo = _init_repo(tmp_path / 'repo')
    assert _run(repo, 'init').returncode == 0
    # precondition: init leaves the attribute uncommitted in the working tree
    status = _git(repo, 'status', '--porcelain').stdout
    assert '.gitattributes' in status
    result = _run(repo, 'commit', 'configure main', '--init')
    assert result.returncode == 0, result.stderr
    # the tree is clean and the attribute is part of the baseline
    assert _git(repo, 'status', '--porcelain').stdout.strip() == ''
    committed = _git(repo, 'show', 'HEAD:.gitattributes').stdout
    assert '**/_index.md merge=wiki' in committed.splitlines()


def test_commit_init_never_sweeps_user_gitattributes_edits(
    tmp_path: pathlib.Path,
) -> None:
    """A user's pending ``.gitattributes`` edits stay out of the baseline.

    A dirty ``.gitattributes`` at init time makes the wiki tool skip its
    write, so the pending content is purely the user's own work.
    """
    repo = _init_repo(tmp_path / 'repo')
    attributes = repo / '.gitattributes'
    attributes.write_text('*.png binary\n', encoding='utf-8')
    _git(repo, 'add', '.gitattributes')
    _git(repo, 'commit', '-m', 'user attributes')
    # a pending user edit, made before init -- the wiki tool skips its write
    attributes.write_text('*.png binary\n*.jpg binary\n', encoding='utf-8')
    assert _run(repo, 'init').returncode == 0
    result = _run(repo, 'commit', 'configure main', '--init')
    assert result.returncode == 0, result.stderr
    # the user's edit is still pending, uncommitted, and intact
    status = _git(repo, 'status', '--porcelain').stdout
    assert '.gitattributes' in status
    committed = _git(repo, 'show', 'HEAD:.gitattributes').stdout
    assert '*.jpg' not in committed
    assert attributes.read_text(encoding='utf-8') == '*.png binary\n*.jpg binary\n'


# ------ helpers


def _init_repo(path: pathlib.Path) -> pathlib.Path:
    """Create a git repo on ``main`` with one commit and a local identity."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, 'init', '-b', 'main')
    _git(path, 'config', 'user.email', 'test@test.com')
    _git(path, 'config', 'user.name', 'Test')
    (path / 'README.md').write_text('# repo\n', encoding='utf-8')
    _git(path, 'add', 'README.md')
    _git(path, 'commit', '-m', 'initial')
    return path


def _broken_wiki_shim(path: pathlib.Path) -> pathlib.Path:
    """Create a pyenv-style broken ``wiki`` shim; return its bin dir.

    The shim resolves on PATH but exits 127 with pyenv's "command not found"
    message, exactly like a shim whose backing environment is inactive.
    """
    shims = path / 'shims'
    shims.mkdir()
    shim = shims / 'wiki'
    shim.write_text(
        "#!/usr/bin/env bash\necho 'pyenv: wiki: command not found' >&2\nexit 127\n",
        encoding='utf-8',
    )
    shim.chmod(0o755)
    return shims
