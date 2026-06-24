"""``Node.files_*`` -- the project-files surface behind the ``/web/files`` API.

The set is git-tracked files minus fractal's own dirs (``.fractal/``, ``wiki/``);
reads are allowlist-validated against that set so machinery and traversal are
unreachable; the archive zips a copy. ``changed=True`` returns the node's
diff-from-base instead. These drive a real git worktree (the ``node_with_db``
repo) so the git plumbing is exercised, not mocked.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from fractal.core.node import Node
from tests._helpers import _git

__all__ = [
    'test_files_list_returns_project_files_and_excludes_machinery',
    'test_files_read_caps_content_and_enforces_the_allowlist',
    'test_files_archive_zips_the_set_without_machinery',
    'test_files_list_changed_is_the_diff_from_base',
    'test_files_list_changed_without_a_base_is_empty',
    'test_files_list_changed_since_narrows_by_commit_iteration_run',
    'test_files_read_version_serves_both_sides_of_the_diff',
    'test_files_list_changed_survives_a_merge_into_the_base',
    'test_files_write_lands_in_worktree_and_rejects_escapes',
    'test_files_commit_files_commits_only_the_named_paths',
]

# a representative spread: a deliverable, a data table, code outside output/, and
# a binary -- written under the worktree and committed so git tracks them
_REPORT = '# Report\nalpha\nbeta\ngamma\n'


def _seed(node: Node) -> None:
    """Write + commit project files (plus the node's own .fractal machinery)."""
    root = node._root
    (root / 'output' / 'data').mkdir(parents=True)
    (root / 'output' / 'REPORT.md').write_text(_REPORT, encoding='utf-8')
    (root / 'output' / 'data' / 'results.tsv').write_text(
        'x\ty\n1\t2\n', encoding='utf-8'
    )
    (root / 'output' / 'logo.png').write_bytes(b'\x89PNG\r\n\x1a\n\x00\xff')
    (root / 'src').mkdir()
    (root / 'src' / 'main.py').write_text('print("hi")\n', encoding='utf-8')
    # commit everything: the fixture's tracked wiki/ + the node's .fractal/ ride
    # along, so the exclusion of fractal-owned dirs is genuinely exercised
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'seed')


def test_files_list_returns_project_files_and_excludes_machinery(
    node_with_db: Node,
) -> None:
    """The listing is tracked files minus .fractal/ and wiki/, kind by extension."""
    node = node_with_db
    _seed(node)
    by_path = {entry['path']: entry for entry in node.files_list()}
    # project files appear with their size (the render kind is the FE's call)
    assert {
        'output/REPORT.md',
        'output/data/results.tsv',
        'src/main.py',
        'output/logo.png',
    } <= set(by_path)
    assert by_path['output/REPORT.md']['size'] > 0
    # the default listing carries no line stats (there is no diff)
    assert 'additions' not in by_path['output/REPORT.md']
    # fractal's own dirs are excluded (both the fixture wiki/ and the node .fractal/)
    assert not any(p.startswith(('.fractal/', 'wiki/')) for p in by_path)
    # a subtree scope narrows the set
    scoped = {entry['path'] for entry in node.files_list(path='output')}
    assert scoped == {
        'output/REPORT.md',
        'output/data/results.tsv',
        'output/logo.png',
    }


def test_files_read_caps_content_and_enforces_the_allowlist(node_with_db: Node) -> None:
    """Reads return capped content; non-project paths are rejected."""
    node = node_with_db
    _seed(node)
    # full read returns the file with line accounting
    full = node.files_read('output/REPORT.md')
    assert full['content'] == _REPORT
    assert full['total_lines'] == 4
    assert full['truncated'] is False
    assert full['binary'] is False
    # a cap truncates to whole lines and reports the real total
    capped = node.files_read('output/REPORT.md', max_lines=2)
    assert capped['content'] == '# Report\nalpha'
    assert capped['truncated'] is True
    assert capped['total_lines'] == 4
    # binary content is flagged for download rather than rendered
    binary = node.files_read('output/logo.png')
    assert binary['binary'] is True
    assert binary['content'] == ''
    # allowlist: machinery, traversal, and unknown paths are all rejected
    for bad in (
        f'.fractal/{node._branch}/config.json',
        'wiki/_index.md',
        '../escape',
        'does_not_exist.md',
    ):
        with pytest.raises(ValueError):
            node.files_read(bad)


def test_files_archive_zips_the_set_without_machinery(node_with_db: Node) -> None:
    """The archive contains the project file set and nothing fractal-owned."""
    node = node_with_db
    _seed(node)
    with zipfile.ZipFile(io.BytesIO(node.files_archive())) as archive:
        names = set(archive.namelist())
        # a round-tripped file matches the worktree
        assert archive.read('output/REPORT.md').decode('utf-8') == _REPORT
    assert {'output/REPORT.md', 'src/main.py'} <= names
    assert not any(n.startswith(('.fractal/', 'wiki/')) for n in names)


def test_files_list_changed_is_the_diff_from_base(node_with_db: Node) -> None:
    """``changed`` lists this node's contribution (base...HEAD), minus machinery."""
    node = node_with_db
    base = _git(node._root, 'rev-parse', 'HEAD').stdout.strip()
    _seed(node)
    node.config_set(base=base)
    changed = {entry['path']: entry for entry in node.files_list(changed=True)}
    assert {'output/REPORT.md', 'src/main.py'} <= set(changed)
    assert not any(p.startswith(('.fractal/', 'wiki/')) for p in changed)
    # numstat line counts ride along; a binary file has none
    assert (
        changed['output/REPORT.md']['additions'],
        changed['output/REPORT.md']['deletions'],
    ) == (4, 0)
    assert changed['output/logo.png']['additions'] is None


def test_files_list_changed_without_a_base_is_empty(node_with_db: Node) -> None:
    """With no base (top-level branch, no config), there is nothing to diff."""
    node = node_with_db
    _seed(node)
    assert node.files_list(changed=True) == []


def test_files_list_changed_since_narrows_by_commit_iteration_run(
    node_with_db: Node,
) -> None:
    """``since`` scopes the diff: commit < iteration < run < base.

    Builds a real history -- run 1 (iters 1-2), run 2 (iters 3-4, the last with
    two commits) -- recording each commit as a ``commit`` event the way the
    commit script does, then checks each scope resolves to the right boundary.
    """
    node = node_with_db
    root = node._root
    base = _git(root, 'rev-parse', 'HEAD').stdout.strip()
    node.config_set(base=base)

    # commit a file as one iteration, logging the commit event _diff_base reads
    def commit_iter(run_id: int, iter_id: int, name: str) -> None:
        (root / name).write_text(name, encoding='utf-8')
        _git(root, 'add', '-A')
        _git(root, 'commit', '-m', name)
        sha = _git(root, 'rev-parse', 'HEAD').stdout.strip()
        node.event_start('commit', metadata=sha, run_id=run_id, iter_id=iter_id)

    r1 = node.run_start()
    commit_iter(r1, node.iter_start(run_id=r1, iter=1), 'a.txt')
    commit_iter(r1, node.iter_start(run_id=r1, iter=2), 'b.txt')
    r2 = node.run_start()
    commit_iter(r2, node.iter_start(run_id=r2, iter=1), 'c.txt')
    i4 = node.iter_start(run_id=r2, iter=2)
    commit_iter(r2, i4, 'd.txt')
    commit_iter(r2, i4, 'e.txt')  # second commit of the last iteration

    def names(**kwargs: str) -> set[str]:
        return {entry['path'] for entry in node.files_list(changed=True, **kwargs)}

    # base (default): the whole contribution since the branch point
    assert names() == {'a.txt', 'b.txt', 'c.txt', 'd.txt', 'e.txt'}
    # run: only the most recent run (run 2 -> iters 3-4)
    assert names(since='run') == {'c.txt', 'd.txt', 'e.txt'}
    # iteration: only the last iteration (iter 4 -> its two commits)
    assert names(since='iteration') == {'d.txt', 'e.txt'}
    # commit: only the last commit
    assert names(since='commit') == {'e.txt'}
    # an unknown scope is rejected
    with pytest.raises(ValueError):
        node.files_list(changed=True, since='bogus')


def test_files_read_version_serves_both_sides_of_the_diff(node_with_db: Node) -> None:
    """``version=base|current`` reads each side; ``exists`` flags adds/deletes.

    Builds a base state, then the node modifies one file, adds another, and
    deletes a third -- the three change kinds a before/after view must render.
    """
    node = node_with_db
    root = node._root
    # base state: a file we will modify, and one we will delete
    (root / 'mod.md').write_text('before\n', encoding='utf-8')
    (root / 'del.md').write_text('bye\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'base')
    node.config_set(base=_git(root, 'rev-parse', 'HEAD').stdout.strip())
    # the node's work: modify, add, delete
    (root / 'mod.md').write_text('after\n', encoding='utf-8')
    (root / 'new.md').write_text('fresh\n', encoding='utf-8')
    (root / 'del.md').unlink()
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'work')

    # the changed listing now surfaces the deletion alongside the modify/add
    assert {'mod.md', 'new.md', 'del.md'} <= {
        entry['path'] for entry in node.files_list(changed=True)
    }
    # modified: both sides present
    assert node.files_read('mod.md', version='base') == {
        'path': 'mod.md',
        'content': 'before\n',
        'truncated': False,
        'total_lines': 1,
        'size': 7,
        'binary': False,
        'exists': True,
    }
    assert node.files_read('mod.md')['content'] == 'after\n'  # current is default
    # added: no base side, present now
    assert node.files_read('new.md', version='base')['exists'] is False
    assert node.files_read('new.md', version='current')['content'] == 'fresh\n'
    # deleted: base preserved, no current side
    assert node.files_read('del.md', version='base')['content'] == 'bye\n'
    assert node.files_read('del.md', version='current')['exists'] is False
    # an unknown version is rejected
    with pytest.raises(ValueError):
        node.files_read('mod.md', version='bogus')


def test_files_list_changed_survives_a_merge_into_the_base(node_with_db: Node) -> None:
    """A node whose work was absorbed into its base still shows its diff.

    Reproduces the merged-child case: ``base`` points at a ref that already
    contains the node's HEAD, so a base-anchored diff is empty -- but every
    scope anchors on the node's own ``commit`` events, which the merge doesn't
    move, so the work still shows.
    """
    node = node_with_db
    root = node._root
    (root / 'work.txt').write_text('node output\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'work')
    sha = _git(root, 'rev-parse', 'HEAD').stdout.strip()
    run = node.run_start()
    node.event_start(
        'commit', metadata=sha, run_id=run, iter_id=node.iter_start(run_id=run, iter=1)
    )
    # the parent absorbed the node: base now contains the node's own HEAD, so a
    # base-vs-parent diff collapses to empty
    node.config_set(base=sha)
    for since in ('base', 'run', 'iteration', 'commit'):
        changed = {
            entry['path'] for entry in node.files_list(changed=True, since=since)
        }
        assert 'work.txt' in changed, f'{since} lost the merged node diff'


def test_files_write_lands_in_worktree_and_rejects_escapes(node_with_db: Node) -> None:
    """``files_write`` puts bytes in the worktree; committed files read back.

    Parent dirs are created; traversal and machinery paths are rejected.
    """
    node = node_with_db
    # a new file in a fresh subdir -- parents are created, bytes land on disk
    result = node.files_write('inputs/data.csv', b'a,b\n1,2\n')
    assert result == {'path': 'inputs/data.csv', 'size': 8}
    assert (node._root / 'inputs' / 'data.csv').read_bytes() == b'a,b\n1,2\n'
    # uncommitted it's absent from the tracked listing; committing surfaces it
    assert 'inputs/data.csv' not in {e['path'] for e in node.files_list()}
    _git(node._root, 'add', '-A')
    _git(node._root, 'commit', '-m', 'add input')
    assert 'inputs/data.csv' in {e['path'] for e in node.files_list()}
    assert node.files_read('inputs/data.csv')['content'] == 'a,b\n1,2\n'
    # binary content round-trips through the bytes path
    node.files_write('inputs/logo.png', b'\x89PNG\r\n\x1a\n\x00\xff')
    assert (
        node._root / 'inputs' / 'logo.png'
    ).read_bytes() == b'\x89PNG\r\n\x1a\n\x00\xff'
    # escapes (traversal, absolute, empty) and machinery are all rejected
    for bad in ('../escape', '/abs/path', '', '.fractal/x', 'wiki/x', '.git/x'):
        with pytest.raises(ValueError):
            node.files_write(bad, b'x')


def test_files_commit_files_commits_only_the_named_paths(node_with_db: Node) -> None:
    """``commit_files`` stages and commits just the named paths (pathspec).

    No lint/scope/push; blank input and unsafe paths are rejected.
    """
    node = node_with_db
    # write two files, then commit only one
    node.files_write('inputs/keep.txt', b'keep\n')
    node.files_write('inputs/other.txt', b'other\n')
    assert node.commit_files(['inputs/keep.txt'], 'add keep') == {
        'committed': True,
        'paths': ['inputs/keep.txt'],
    }
    # the committed path is now tracked; the un-named one stays uncommitted
    listed = {entry['path'] for entry in node.files_list()}
    assert 'inputs/keep.txt' in listed
    assert 'inputs/other.txt' not in listed
    assert node.files_read('inputs/keep.txt')['content'] == 'keep\n'
    # re-committing an unchanged path is a benign no-op
    assert node.commit_files(['inputs/keep.txt'], 'again') == {
        'committed': False,
        'paths': ['inputs/keep.txt'],
    }
    # empty paths, a blank message, and unsafe paths are all rejected
    with pytest.raises(ValueError):
        node.commit_files([], 'msg')
    with pytest.raises(ValueError):
        node.commit_files(['inputs/keep.txt'], '')
    for bad in ('../escape', '.fractal/x', '.git/x'):
        with pytest.raises(ValueError):
            node.commit_files([bad], 'msg')
