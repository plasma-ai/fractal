"""Test the ``fractal.core.files`` module.

``Node.files`` -- the project-files surface.

The set is git-tracked files, machinery included (consumers filter);
``since`` switches a listing to the node's own contribution -- files its own
first-parent commits touched, with net diff counts -- anchored on the node's
event log with a time-window fallback for eventless commits. Reads pass the
structural boundary; uploads additionally refuse ``.fractal``. These drive a
real git worktree (the ``node_with_db`` repo) so the git plumbing is
exercised, not mocked.
"""

from __future__ import annotations

import io
import json
import pathlib
import subprocess
import zipfile

import pytest

from fractal.core.node import Node
from tests._helpers import _git

__all__ = [
    'test_files_list_returns_all_tracked_files',
    'test_files_read_caps_content_and_enforces_the_boundary',
    'test_files_read_truncation_preserves_line_terminators',
    'test_files_list_round_trips_non_ascii_paths',
    'test_files_glob_metachar_paths_round_trip',
    'test_files_archive_zips_the_listed_set',
    'test_files_symlinks_serve_in_tree_targets_only',
    'test_files_list_changed_is_the_diff_from_base',
    'test_files_list_changed_excludes_merged_in_content',
    'test_files_list_changed_keeps_squash_integrated_content',
    'test_files_list_changed_drops_zero_net_changes',
    'test_files_list_changed_includes_wiki_and_machinery_edits',
    'test_files_list_changed_without_an_anchor_is_empty',
    'test_files_list_changed_since_narrows_by_commit_iteration_run',
    'test_files_list_changed_since_ignores_other_nodes_history',
    'test_files_list_changed_since_run_covers_an_eventless_run',
    'test_files_list_base_covers_uploads_before_the_first_loop_commit',
    'test_diff_anchors_pin_to_the_current_incarnation',
    'test_files_read_before_serves_both_sides_of_the_diff',
    'test_files_read_at_serves_own_history_commits',
    'test_files_list_changed_survives_a_merge_into_the_base',
    'test_files_history_traces_own_commits_per_file',
    'test_files_write_lands_in_worktree_and_rejects_escapes',
    'test_files_write_through_a_symlink_updates_the_target',
    'test_files_uploads_accept_wiki_and_refuse_fractal',
    'test_files_commit_commits_only_the_named_paths',
    'test_files_writes_refuse_on_a_paused_node',
]

# a representative spread: a deliverable, a data table, code outside output/,
# and a binary -- written under the worktree and committed so git tracks them
_REPORT = '# Report\nalpha\nbeta\ngamma\n'


# ------ listing and reading


def test_files_list_returns_all_tracked_files(node_with_db: Node) -> None:
    """The listing is every tracked file -- machinery included -- scopable."""
    node = node_with_db
    _seed(node)
    by_path = {entry['path']: entry for entry in node.files.list()}
    # project files appear with their on-disk size
    assert {
        'output/REPORT.md',
        'output/data/results.tsv',
        'output/logo.png',
        'src/main.py',
    } <= set(by_path)
    assert by_path['output/REPORT.md']['size'] == len(_REPORT)
    # the full listing carries no change stats (there is no diff)
    assert 'change' not in by_path['output/REPORT.md']
    assert 'additions' not in by_path['output/REPORT.md']
    # wiki/ and .fractal/ list like any other tracked content (consumers
    # filter or collapse machinery)
    assert 'wiki/_index.md' in by_path
    assert f'.fractal/{node.branch}/config.json' in by_path
    # a subtree scope narrows the set
    scoped = {entry['path'] for entry in node.files.list(path='output')}
    assert scoped == {
        'output/REPORT.md',
        'output/data/results.tsv',
        'output/logo.png',
    }


def test_files_read_caps_content_and_enforces_the_boundary(
    node_with_db: Node,
) -> None:
    """Reads return capped content; structurally unsafe paths are rejected."""
    node = node_with_db
    _seed(node)
    # full read returns the file with line accounting
    full = node.files.read('output/REPORT.md')
    assert full['content'] == _REPORT
    assert full['total_lines'] == 4
    assert full['truncated'] is False
    assert full['binary'] is False
    # a cap truncates to whole lines and reports the real total
    capped = node.files.read('output/REPORT.md', max_lines=2)
    assert capped['content'] == '# Report\nalpha\n'
    assert capped['truncated'] is True
    assert capped['total_lines'] == 4
    # binary content is flagged for download rather than rendered
    binary = node.files.read('output/logo.png')
    assert binary['binary'] is True
    assert binary['content'] == ''
    assert binary['size'] > 0
    # the download path serves the same file straight from disk
    assert node.files.path('output/logo.png').read_bytes().startswith(b'\x89PNG')
    # tracked machinery is readable like any other content
    assert node.files.read('wiki/_index.md')['exists'] is True
    config = node.files.read(f'.fractal/{node.branch}/config.json')
    assert json.loads(config['content'])['root'] == node.branch
    # traversal, .git (case variants included -- APFS matches names
    # case-insensitively), sibling worktrees, leading pathspec magic, and
    # unknown paths (glob chars taken literally) are all rejected -- by both
    # the read and the download path
    for bad in (
        '.git',
        '.GIT/config',
        '.worktrees/x',
        '../escape',
        '/abs/path',
        '',
        '*',
        'src/*.py',
        'x[1].txt',
        ':(top)README.md',
        'does_not_exist.md',
    ):
        with pytest.raises(ValueError):
            node.files.read(bad)
        with pytest.raises(ValueError):
            node.files.path(bad)


def test_files_read_truncation_preserves_line_terminators(
    node_with_db: Node,
) -> None:
    """A capped read round-trips byte-identical against the raw file."""
    node = node_with_db
    (node.worktree / 'crlf.txt').write_bytes(b'# a\r\nbeta\r\n')
    _git(node.worktree, 'add', '-A')
    _git(node.worktree, 'commit', '-m', 'crlf')
    capped = node.files.read('crlf.txt', max_lines=1)
    # keepends truncation: the included portion is the file's exact bytes
    assert capped['content'] == '# a\r\n'
    assert capped['truncated'] is True
    assert capped['total_lines'] == 2


def test_files_list_round_trips_non_ascii_paths(node_with_db: Node) -> None:
    """A non-ASCII filename survives list -> read -> path un-mangled."""
    node = node_with_db
    name = 'déjà.md'
    (node.worktree / name).write_text('encore\n', encoding='utf-8')
    _git(node.worktree, 'add', '-A')
    _git(node.worktree, 'commit', '-m', 'non-ascii')
    # NUL-parsed listings return the path verbatim, never C-quoted
    assert name in {entry['path'] for entry in node.files.list()}
    assert node.files.read(name)['content'] == 'encore\n'
    assert node.files.path(name).name == name


def test_files_glob_metachar_paths_round_trip(node_with_db: Node) -> None:
    """A tracked name with glob chars (a bracketed route) stays servable.

    Framework-conventional names like Next.js ``app/[id]/page.tsx`` are glob
    metacharacters to git -- every surface must take them literally, so the
    single-char sibling the bracket expression would match never answers in
    the route's place.
    """
    node = node_with_db
    root = node.worktree
    # a dynamic route, plus the sibling its bracket expression globs to
    (root / 'app' / '[id]').mkdir(parents=True)
    (root / 'app' / '[id]' / 'page.tsx').write_text('dynamic\n', encoding='utf-8')
    (root / 'app' / 'i').mkdir()
    (root / 'app' / 'i' / 'page.tsx').write_text('sibling\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'routes')
    # the bracketed path lists, reads, and downloads -- as itself
    assert 'app/[id]/page.tsx' in {entry['path'] for entry in node.files.list()}
    assert node.files.read('app/[id]/page.tsx')['content'] == 'dynamic\n'
    assert node.files.path('app/[id]/page.tsx').read_text() == 'dynamic\n'
    # the before side resolves through the same literal plumbing
    (root / 'app' / '[id]' / 'page.tsx').write_text('rewritten\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'rewrite')
    before = node.files.read('app/[id]/page.tsx', since='commit', before=True)
    assert before['content'] == 'dynamic\n'
    # an upload with a bracketed name commits as that one literal path
    node.files.write('app/[slug]/page.tsx', b'slug\n')
    node.files.commit(['app/[slug]/page.tsx'], 'add slug route')
    committed = _git(root, 'show', '--name-only', '--format=', 'HEAD')
    assert committed.stdout.split() == ['app/[slug]/page.tsx']


def test_files_archive_zips_the_listed_set(node_with_db: Node) -> None:
    """The archive holds the listing's set, machinery included."""
    node = node_with_db
    _seed(node)
    with zipfile.ZipFile(io.BytesIO(node.files.archive())) as archive:
        names = set(archive.namelist())
        # a round-tripped file matches the worktree
        assert archive.read('output/REPORT.md').decode('utf-8') == _REPORT
    assert {'output/REPORT.md', 'src/main.py', 'wiki/_index.md'} <= names


def test_files_symlinks_serve_in_tree_targets_only(node_with_db: Node) -> None:
    """An in-tree symlink serves its target; an escaping one is unreachable."""
    node = node_with_db
    root = node.worktree
    (root / 'target.txt').write_text('inside\n', encoding='utf-8')
    (root / 'inside.link').symlink_to('target.txt')
    # a tracked link to a file outside the worktree (the exfiltration case)
    secret = root.parent / 'secret.txt'
    secret.write_text('outside\n', encoding='utf-8')
    (root / 'escape.link').symlink_to(secret)
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'links')
    # the in-tree link lists and reads its target content
    listed = {entry['path'] for entry in node.files.list()}
    assert 'inside.link' in listed
    assert node.files.read('inside.link')['content'] == 'inside\n'
    # the escaping link is dropped from listings and archives, and neither
    # readable nor downloadable
    assert 'escape.link' not in listed
    with zipfile.ZipFile(io.BytesIO(node.files.archive())) as archive:
        assert 'escape.link' not in set(archive.namelist())
    with pytest.raises(ValueError):
        node.files.read('escape.link')
    with pytest.raises(ValueError):
        node.files.path('escape.link')


# ------ changed listings and anchors


def test_files_list_changed_is_the_diff_from_base(node_with_db: Node) -> None:
    """``since='base'`` lists the node's contribution with net counts."""
    node = node_with_db
    base = _git(node.worktree, 'rev-parse', 'HEAD').stdout.strip()
    _seed(node)
    node.config.set('base', base)
    changed = {entry['path']: entry for entry in node.files.list(since='base')}
    assert {'output/REPORT.md', 'src/main.py'} <= set(changed)
    # the node's own committed machinery counts as its contribution too
    assert f'.fractal/{node.branch}/config.json' in changed
    # numstat line counts and the change kind ride along; binaries have none
    assert changed['output/REPORT.md']['change'] == 'added'
    assert (
        changed['output/REPORT.md']['additions'],
        changed['output/REPORT.md']['deletions'],
    ) == (4, 0)
    assert changed['output/logo.png']['additions'] is None


def test_files_list_changed_excludes_merged_in_content(node_with_db: Node) -> None:
    """Content synced in by a merge never reads as the node's output.

    A tree at HEAD contains everything ever merged in, so without the
    own-commit membership walk a sync merge would attribute the side
    branch's files (and their line counts) to this node.
    """
    node = node_with_db
    root = node.worktree
    node.config.set('base', _git(root, 'rev-parse', 'HEAD').stdout.strip())
    # the node's own work, then a side branch's file arriving via a merge
    _commit_file(root, 'own.md', 'mine\n', 'own work')
    _merge_side_branch(root, 'synced.md')
    changed = {entry['path']: entry for entry in node.files.list(since='base')}
    # the merged-in file is in the net diff but not the node's contribution
    assert 'own.md' in changed
    assert 'synced.md' not in changed
    assert changed['own.md']['additions'] == 1


def test_files_list_changed_keeps_squash_integrated_content(
    node_with_db: Node,
) -> None:
    """A squash-merged child lands as the node's own contribution.

    A squash integration is an ordinary commit on the node's first-parent
    line -- absorbed child work rightly reads as the absorbing node's output,
    unlike a sync merge's side history.
    """
    node = node_with_db
    root = node.worktree
    branch = _git(root, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
    node.config.set('base', _git(root, 'rev-parse', 'HEAD').stdout.strip())
    # a child branch does work; the node squash-merges it (the merge recipe)
    _git(root, 'checkout', '-q', '-b', 'child')
    _commit_file(root, 'child.md', 'child work\n', 'child: work')
    _git(root, 'checkout', '-q', branch)
    _git(root, 'merge', '--squash', 'child')
    _git(root, 'commit', '-m', 'absorb child')
    _git(root, 'branch', '-D', 'child')
    assert 'child.md' in _changed_names(node, 'base')


def test_files_list_changed_drops_zero_net_changes(node_with_db: Node) -> None:
    """A touched file with no net change drops out (self-corrections cancel)."""
    node = node_with_db
    root = node.worktree
    node.config.set('base', _git(root, 'rev-parse', 'HEAD').stdout.strip())
    _commit_file(root, 'keep.md', 'kept\n', 'add keep')
    _commit_file(root, 'scratch.py', 'temp\n', 'add scratch')
    (root / 'scratch.py').unlink()
    _git(root, 'add', 'scratch.py')
    _git(root, 'commit', '-m', 'drop scratch')
    # the add-then-delete file was touched but contributes nothing to render
    assert _changed_names(node, 'base') == {'keep.md'}


def test_files_list_changed_includes_wiki_and_machinery_edits(
    node_with_db: Node,
) -> None:
    """Wiki gardening and meta-node output list as the node's contribution.

    A node whose mission is the wiki, or configuring another node (its
    deliverable under ``.fractal/<target>/``), must not show an empty
    contribution -- consumers filter machinery, core does not.
    """
    node = node_with_db
    root = node.worktree
    node.config.set('base', _git(root, 'rev-parse', 'HEAD').stdout.strip())
    # wiki gardening plus a meta node's target seed, committed as own work
    original = (root / 'wiki' / '_index.md').read_text(encoding='utf-8')
    _commit_file(root, 'wiki/_index.md', f'{original}gardened\n', 'garden wiki')
    (root / '.fractal' / f'{node.branch}.target').mkdir(parents=True)
    _commit_file(
        root,
        f'.fractal/{node.branch}.target/NODE.md',
        'configured\n',
        'configure target',
    )
    changed = {entry['path']: entry for entry in node.files.list(since='base')}
    assert 'wiki/_index.md' in changed
    assert f'.fractal/{node.branch}.target/NODE.md' in changed
    # both sides read normally through the changed set
    before = node.files.read('wiki/_index.md', since='base', before=True)
    assert before['content'] == original
    target = node.files.read(f'.fractal/{node.branch}.target/NODE.md')
    assert target['content'] == 'configured\n'


def test_files_list_changed_without_an_anchor_is_empty(node_with_db: Node) -> None:
    """No anchor -- no base config, no logged commits -- reads as no changes."""
    node = node_with_db
    _seed(node)
    # a top-level branch with no base config has no fork point
    assert node.files.list(since='base') == []
    # a node that never committed through a run has no iteration/run anchor,
    # even once a base is configured
    node.config.set('base', _git(node.worktree, 'rev-parse', 'HEAD~1').stdout.strip())
    assert node.files.list(since='iteration') == []
    assert node.files.list(since='run') == []
    # an unknown scope is rejected
    with pytest.raises(ValueError):
        node.files.list(since='bogus')


def test_files_list_changed_since_narrows_by_commit_iteration_run(
    node_with_db: Node,
) -> None:
    """``since`` scopes the diff: commit < iteration < run < base.

    Builds a real history -- run 1 (iters 1-2), run 2 (iters 3-4, the last
    with two commits) -- recording each commit event the way the commit
    pipeline does, then checks each scope resolves to the right boundary.
    """
    node = node_with_db
    base = _git(node.worktree, 'rev-parse', 'HEAD').stdout.strip()
    node.config.set('base', base)

    r1 = node.record.run_start()
    _commit_iter(node, r1, node.record.iter_start(run_id=r1, iter=1), 'a.txt')
    _commit_iter(node, r1, node.record.iter_start(run_id=r1, iter=2), 'b.txt')
    r2 = node.record.run_start()
    _commit_iter(node, r2, node.record.iter_start(run_id=r2, iter=1), 'c.txt')
    i4 = node.record.iter_start(run_id=r2, iter=2)
    _commit_iter(node, r2, i4, 'd.txt')
    _commit_iter(node, r2, i4, 'e.txt')  # second commit of the last iteration

    # base: the whole contribution since the branch point
    assert _changed_names(node, 'base') == {'a.txt', 'b.txt', 'c.txt', 'd.txt', 'e.txt'}
    # run: only the most recent run (run 2 -> iters 3-4)
    assert _changed_names(node, 'run') == {'c.txt', 'd.txt', 'e.txt'}
    # iteration: only the last iteration (iter 4 -> its two commits)
    assert _changed_names(node, 'iteration') == {'d.txt', 'e.txt'}
    # commit: only the last commit
    assert _changed_names(node, 'commit') == {'e.txt'}


def test_files_list_changed_since_ignores_other_nodes_history(
    node_with_db: Node,
) -> None:
    """``since`` anchors on this node's own commits, not the tree's newest.

    The DB is tree-central: every node's ``commit`` events share one table
    with global run/iter ids, so a sibling that runs later holds the tree's
    MAX run/iter. An unscoped anchor query would pick that sibling's sha -- a
    commit on the sibling's branch -- collapsing this node's iteration/run
    diffs to the branch point (the whole contribution as pure adds).
    """
    node = node_with_db
    root = node.worktree
    node.config.set('base', _git(root, 'rev-parse', 'HEAD').stdout.strip())

    # this node's history: one run, two committed iterations
    r1 = node.record.run_start()
    _commit_iter(node, r1, node.record.iter_start(run_id=r1, iter=1), 'a.txt')
    _commit_iter(node, r1, node.record.iter_start(run_id=r1, iter=2), 'b.txt')

    # a sibling over the same central DB (the radio_pair recipe: real
    # worktree, hand-built node dir) that runs and commits AFTER this node --
    # its run/iter take the tree-wide MAX ids, on its own branch
    branch = node.branch
    peer_branch = f'{branch}.peer'
    worktree = root / '.worktrees' / peer_branch
    subprocess.run(
        ['git', 'worktree', 'add', '-b', peer_branch, f'{worktree}', branch],
        cwd=root,
        capture_output=True,
        check=True,
    )
    node_dir = worktree / '.fractal' / peer_branch
    node_dir.mkdir(parents=True)
    config = {
        'project': '.',
        'root': branch,
        'scope': '',
        'agent': 'claude',
        'local': False,
        'detached': False,
    }
    (node_dir / 'config.json').write_text(
        json.dumps(config, indent=2),
        encoding='utf-8',
    )
    (node_dir / '.status').write_text('idle\n', encoding='utf-8')
    peer = Node(worktree)
    peer_base = _git(worktree, 'rev-parse', 'HEAD').stdout.strip()
    r2 = peer.record.run_start()
    _commit_iter(peer, r2, peer.record.iter_start(run_id=r2, iter=1), 'peer.txt')

    # every scope reads this node's own history: the sibling's newer run/iter
    # (the tree-wide MAX) must not move the anchors
    assert _changed_names(node, 'base') == {'a.txt', 'b.txt'}
    assert _changed_names(node, 'run') == {'a.txt', 'b.txt'}
    assert _changed_names(node, 'iteration') == {'b.txt'}
    # and the sibling's view is its own work only
    peer.config.set('base', peer_base)
    assert _changed_names(peer, 'iteration') == {'peer.txt'}


def test_files_list_changed_since_run_covers_an_eventless_run(
    node_with_db: Node,
) -> None:
    """``since='run'`` covers a run whose commits never logged events.

    Agents commit raw mid-step sometimes, bypassing the pipeline's event
    emit. The newest run must still anchor on its own commits -- matched by
    author time against the run's window -- not silently blend into the last
    evented run.
    """
    node = node_with_db
    root = node.worktree
    node.config.set('base', _git(root, 'rev-parse', 'HEAD').stdout.strip())
    # run 1 commits through the pipeline (evented) and ends
    r1 = node.record.run_start()
    _commit_iter(node, r1, node.record.iter_start(run_id=r1, iter=1), 'a.txt')
    node.record.run_end(run_id=r1, status='completed', exit_code=0)
    # run 2's agent commits raw -- no commit events anywhere in the run
    node.record.run_start()
    _commit_file(root, 'b.txt', 'b\n', 'raw agent commit')
    _commit_file(root, 'c.txt', 'c\n', 'another raw commit')
    # the newest run resolves by time window, not the older run's events
    assert _changed_names(node, 'run') == {'b.txt', 'c.txt'}
    assert _changed_names(node, 'base') == {'a.txt', 'b.txt', 'c.txt'}


def test_files_list_base_covers_uploads_before_the_first_loop_commit(
    node_with_db: Node,
) -> None:
    """The init-event fork sha anchors ``base`` before any upload commit."""
    node = node_with_db
    fork = _git(node.worktree, 'rev-parse', 'HEAD').stdout.strip()
    # the fork sha init.sh stamps at branch creation
    node.record.event_start('init', metadata=fork)
    # an upload committed before the loop ever ran (no commit event -- a
    # first-commit-event anchor would silently exclude it); author time
    # resolves run membership at whole-second granularity, so the upload is
    # backdated the way a real pre-run upload predates the run
    node.files.write('inputs/data.csv', b'a,b\n1,2\n')
    node.files.commit(['inputs/data.csv'], 'seed inputs')
    _git(
        node.worktree,
        'commit',
        '--amend',
        '--no-edit',
        '--date=2020-01-01T00:00:00Z',
    )
    r1 = node.record.run_start()
    _commit_iter(node, r1, node.record.iter_start(run_id=r1, iter=1), 'a.txt')
    assert _changed_names(node, 'base') == {'inputs/data.csv', 'a.txt'}
    # the run scope excludes the upload: it predates the run's window
    assert _changed_names(node, 'run') == {'a.txt'}


def test_diff_anchors_pin_to_the_current_incarnation(node_with_db: Node) -> None:
    """A re-init of a deleted branch name never reads dead events.

    History rows persist across delete and reset by design, keyed only by the
    node name -- so after a re-init, anchor resolution must floor at the
    newest ``init`` event or it reaches the dead incarnation's commits.
    """
    node = node_with_db
    # the dead incarnation: an init event, one committed iteration
    fork = _git(node.worktree, 'rev-parse', 'HEAD').stdout.strip()
    node.record.event_start('init', metadata=fork)
    r1 = node.record.run_start()
    sha = _commit_iter(node, r1, node.record.iter_start(run_id=r1, iter=1), 'a.txt')
    # the re-init: a fresh init event stamps the new fork point (the tip)
    node.record.event_start('init', metadata=sha)
    # no scope may reach past the re-init into the dead incarnation
    assert _changed_names(node, 'base') == set()
    assert _changed_names(node, 'run') == set()
    assert _changed_names(node, 'iteration') == set()


def test_files_read_before_serves_both_sides_of_the_diff(node_with_db: Node) -> None:
    """``before`` reads the anchor side; ``exists`` flags adds and deletes.

    Builds a base state, then the node modifies one file, adds another, and
    deletes a third -- the three change kinds a before/after view must render.
    """
    node = node_with_db
    root = node.worktree
    # base state: a file we will modify, and one we will delete
    (root / 'mod.md').write_text('before\n', encoding='utf-8')
    (root / 'del.md').write_text('bye\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'base')
    node.config.set('base', _git(root, 'rev-parse', 'HEAD').stdout.strip())
    # the node's work: modify, add, delete
    (root / 'mod.md').write_text('after\n', encoding='utf-8')
    (root / 'new.md').write_text('fresh\n', encoding='utf-8')
    (root / 'del.md').unlink()
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'work')

    # the changed listing surfaces the deletion alongside the modify/add
    changed = {entry['path']: entry for entry in node.files.list(since='base')}
    assert {'mod.md', 'new.md', 'del.md'} <= set(changed)
    assert changed['del.md']['change'] == 'deleted'
    assert changed['del.md']['size'] == 0
    # modified: both sides present
    assert node.files.read('mod.md', since='base', before=True) == {
        'path': 'mod.md',
        'content': 'before\n',
        'truncated': False,
        'total_lines': 1,
        'size': 7,
        'binary': False,
        'exists': True,
    }
    assert node.files.read('mod.md')['content'] == 'after\n'
    # added: no before side, present now
    assert node.files.read('new.md', since='base', before=True)['exists'] is False
    assert node.files.read('new.md')['content'] == 'fresh\n'
    # deleted: before preserved, no current side
    assert node.files.read('del.md', since='base', before=True)['content'] == 'bye\n'
    assert node.files.read('del.md', since='base')['exists'] is False
    # a before read requires an explicit anchor
    with pytest.raises(ValueError):
        node.files.read('mod.md', before=True)


def test_files_read_at_serves_own_history_commits(node_with_db: Node) -> None:
    """``at`` reads a file as a history commit left it; refs stay gated.

    The version-view behind :meth:`Files.history`: each returned sha reads
    that point's content, a commit predating the file answers
    ``exists=False``, and only full shas reachable from HEAD resolve --
    never a rev expression or a sibling's unmerged commit.
    """
    node = node_with_db
    root = node.worktree
    base = _commit_file(root, 'other.md', 'noise\n', 'base')
    first = _commit_file(root, 'report.md', 'one\n', 'draft')
    second = _commit_file(root, 'report.md', 'one\ntwo\n', 'extend')
    # each history sha serves its version; at overrides since/before
    assert node.files.read('report.md', at=first)['content'] == 'one\n'
    assert node.files.read('report.md', at=second)['content'] == 'one\ntwo\n'
    at_read = node.files.read('report.md', at=first, since='base', before=True)
    assert at_read['content'] == 'one\n'
    # a commit predating the file has no side to serve
    assert node.files.read('report.md', at=base)['exists'] is False
    # rev expressions and unknown shas never resolve
    with pytest.raises(ValueError):
        node.files.read('report.md', at='HEAD~1')
    with pytest.raises(ValueError):
        node.files.read('report.md', at='a' * 40)
    # the structural boundary holds for at reads too
    with pytest.raises(ValueError):
        node.files.read('../escape', at=first)


def test_files_list_changed_survives_a_merge_into_the_base(
    node_with_db: Node,
) -> None:
    """A node whose work was absorbed into its base still shows its diff.

    Reproduces the merged-child case: ``base`` points at a ref that already
    contains the node's HEAD, so a base-ref diff is empty -- but every scope
    anchors on the node's own events, which the merge doesn't move.
    """
    node = node_with_db
    root = node.worktree
    (root / 'work.txt').write_text('node output\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'work')
    sha = _git(root, 'rev-parse', 'HEAD').stdout.strip()
    run = node.record.run_start()
    node.record.event_start(
        'commit',
        metadata=sha,
        run_id=run,
        iter_id=node.record.iter_start(run_id=run, iter=1),
    )
    # the parent absorbed the node: base now contains the node's own HEAD, so
    # a base-ref diff collapses to empty
    node.config.set('base', sha)
    for since in ('base', 'run', 'iteration', 'commit'):
        assert 'work.txt' in _changed_names(node, since), f'{since} lost the diff'


def test_files_history_traces_own_commits_per_file(node_with_db: Node) -> None:
    """History is the file's own-commit trail: newest first, merges excluded.

    The trail behind a changed listing: per-commit line counts (unlike the
    listing's net), only commits on the node's own line, and an empty trail
    for a file that only ever arrived by merge.
    """
    node = node_with_db
    root = node.worktree
    node.config.set('base', _git(root, 'rev-parse', 'HEAD').stdout.strip())
    # three own commits touching the file, with a sync merge in between
    first = _commit_file(root, 'report.md', 'one\n', 'draft')
    _merge_side_branch(root, 'merged.md')
    second = _commit_file(root, 'report.md', 'one\ntwo\n', 'extend')
    third = _commit_file(root, 'report.md', 'uno\ndos\n', 'rewrite')
    history = node.files.history('report.md')
    assert [entry['sha'] for entry in history] == [third, second, first]
    assert [entry['subject'] for entry in history] == ['rewrite', 'extend', 'draft']
    # per-commit counts sum the work over time; instants are row-format
    assert (history[0]['additions'], history[0]['deletions']) == (2, 2)
    assert (history[2]['additions'], history[2]['deletions']) == (1, 0)
    assert history[0]['instant'].endswith('Z')
    # a merged-in file has no own trail; the scope narrows the walk
    assert node.files.history('merged.md') == []
    assert [
        entry['sha'] for entry in node.files.history('report.md', since='commit')
    ] == [third]
    # the structural boundary holds for history too
    with pytest.raises(ValueError):
        node.files.history('../escape')
    with pytest.raises(ValueError):
        node.files.history('.git/config')


# ------ writing and committing


def test_files_write_lands_in_worktree_and_rejects_escapes(
    node_with_db: Node,
) -> None:
    """``Files.write`` puts bytes in the worktree; escapes are rejected.

    Parent dirs are created; traversal, ``.fractal``, and case-variant paths
    are all rejected.
    """
    node = node_with_db
    # a new file in a fresh subdir -- parents are created, bytes land on disk
    result = node.files.write('inputs/data.csv', b'a,b\n1,2\n')
    assert result == {'path': 'inputs/data.csv', 'size': 8}
    assert (node.worktree / 'inputs' / 'data.csv').read_bytes() == b'a,b\n1,2\n'
    # uncommitted it's absent from the tracked listing; committing surfaces it
    assert 'inputs/data.csv' not in {e['path'] for e in node.files.list()}
    node.files.commit(['inputs/data.csv'], 'add input')
    assert 'inputs/data.csv' in {e['path'] for e in node.files.list()}
    assert node.files.read('inputs/data.csv')['content'] == 'a,b\n1,2\n'
    # binary content round-trips through the bytes path
    node.files.write('inputs/logo.png', b'\x89PNG\r\n\x1a\n\x00\xff')
    logo = node.worktree / 'inputs' / 'logo.png'
    assert logo.read_bytes() == b'\x89PNG\r\n\x1a\n\x00\xff'
    # escapes (traversal, absolute, empty), leading pathspec magic, and
    # machinery (case variants included -- APFS matches case-insensitively)
    # are all rejected
    for bad in (
        '../escape',
        '/abs/path',
        '',
        '.',
        '.fractal/x',
        '.Fractal/x',
        '.git',
        '.GIT',
        '.git/hooks/pre-commit',
        '.worktrees/x',
        '.WorkTrees/x',
        ':!x',
    ):
        with pytest.raises(ValueError):
            node.files.write(bad, b'x')


def test_files_write_through_a_symlink_updates_the_target(
    node_with_db: Node,
) -> None:
    """A write through an in-tree symlink lands in the target; the link lives.

    The read side serves an in-tree link by target content, so a
    read-modify-write round trip must update the target through the link --
    never swap the link for a regular file and strand the target stale. An
    escaping link stays unwritable, as it is unreadable.
    """
    node = node_with_db
    root = node.worktree
    (root / 'target.txt').write_text('inside\n', encoding='utf-8')
    (root / 'inside.link').symlink_to('target.txt')
    # a tracked link to a file outside the worktree (the exfiltration case)
    secret = root.parent / 'secret.txt'
    secret.write_text('outside\n', encoding='utf-8')
    (root / 'escape.link').symlink_to(secret)
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'links')
    # the in-tree link survives the write and its target holds the new bytes
    node.files.write('inside.link', b'updated\n')
    assert (root / 'inside.link').is_symlink()
    assert (root / 'target.txt').read_bytes() == b'updated\n'
    assert node.files.read('inside.link')['content'] == 'updated\n'
    # the escaping link is not writable, and its target is untouched
    with pytest.raises(ValueError):
        node.files.write('escape.link', b'x\n')
    assert secret.read_text(encoding='utf-8') == 'outside\n'


def test_files_uploads_accept_wiki_and_refuse_fractal(node_with_db: Node) -> None:
    """The wiki uploads like project content; ``.fractal`` never does.

    Uploading an existing project wholesale must carry its wiki, while a
    foreign tree's ``.fractal/`` is stale machinery -- and a raw-bytes
    overwrite of a live node config would reach the control plane.
    """
    node = node_with_db
    # wiki pages upload and commit like any project content
    node.files.write('wiki/notes.md', b'# Notes\n')
    result = node.files.commit(['wiki/notes.md'], 'wiki notes')
    assert result['committed'] is True
    assert 'wiki/notes.md' in {entry['path'] for entry in node.files.list()}
    assert node.files.read('wiki/notes.md')['content'] == '# Notes\n'
    # .fractal refuses at the write tier, for writes and commits alike
    for bad in ('.fractal/x', '.Fractal/x', f'.fractal/{node.branch}/config.json'):
        with pytest.raises(ValueError):
            node.files.write(bad, b'x')
        with pytest.raises(ValueError):
            node.files.commit([bad], 'msg')


def test_files_commit_commits_only_the_named_paths(node_with_db: Node) -> None:
    """``Files.commit`` stages and commits just the named paths (pathspec).

    No lint/scope/push, no commit event, hooks bypassed; blank input and
    unsafe paths are rejected.
    """
    node = node_with_db
    root = node.worktree
    # a failing hook must not reject or rewrite uploaded bytes (--no-verify)
    hook = root / '.git' / 'hooks' / 'pre-commit'
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
    hook.chmod(0o755)
    # write two files and stage a decoy, then commit only one file
    node.files.write('inputs/keep.txt', b'keep\n')
    node.files.write('inputs/other.txt', b'other\n')
    (root / 'decoy.txt').write_text('staged elsewhere\n', encoding='utf-8')
    _git(root, 'add', 'decoy.txt')
    result = node.files.commit(['inputs/keep.txt'], 'add keep')
    assert result['committed'] is True
    assert result['paths'] == ['inputs/keep.txt']
    # the sha is the new HEAD, and its commit holds only the named path
    head = _git(root, 'rev-parse', 'HEAD').stdout.strip()
    assert result['sha'] == head
    committed = _git(root, 'show', '--name-only', '--format=', 'HEAD')
    assert committed.stdout.split() == ['inputs/keep.txt']
    # the un-named upload stays uncommitted and the decoy stays staged
    listed = {entry['path'] for entry in node.files.list()}
    assert 'inputs/other.txt' not in listed
    staged = _git(root, 'diff', '--cached', '--name-only')
    assert 'decoy.txt' in staged.stdout.split()
    # no commit event was logged: an upload has no run lineage
    assert node.db.read('events', where={'event': 'commit'}) == []
    # re-committing an unchanged path is a benign no-op
    assert node.files.commit(['inputs/keep.txt'], 'again') == {
        'committed': False,
        'sha': None,
        'paths': ['inputs/keep.txt'],
    }
    # empty paths, a blank message, and unsafe paths are all rejected
    with pytest.raises(ValueError):
        node.files.commit([], 'msg')
    with pytest.raises(ValueError):
        node.files.commit(['inputs/keep.txt'], '')
    for bad in ('../escape', '.fractal/x', '.git/x', ':!x'):
        with pytest.raises(ValueError):
            node.files.commit([bad], 'msg')


def test_files_writes_refuse_on_a_paused_node(node_with_db: Node) -> None:
    """A paused node admits reads but refuses writes and commits."""
    node = node_with_db
    _seed(node)
    node.status_set('paused')
    # the frozen worktree stays readable
    assert node.files.read('output/REPORT.md')['content'] == _REPORT
    assert 'src/main.py' in {entry['path'] for entry in node.files.list()}
    # writes and commits would perturb work a resume expects intact
    with pytest.raises(RuntimeError, match='paused'):
        node.files.write('inputs/late.txt', b'x')
    with pytest.raises(RuntimeError, match='paused'):
        node.files.commit(['inputs/late.txt'], 'late')
    # a resumed (idle) node accepts the same write
    node.status_set('idle')
    assert node.files.write('inputs/late.txt', b'x')['size'] == 1


# ------ helpers


def _seed(node: Node) -> None:
    """Write + commit project files (plus the node's own .fractal machinery)."""
    root = node.worktree
    (root / 'output' / 'data').mkdir(parents=True)
    (root / 'output' / 'REPORT.md').write_text(_REPORT, encoding='utf-8')
    (root / 'output' / 'data' / 'results.tsv').write_text(
        'x\ty\n1\t2\n', encoding='utf-8'
    )
    (root / 'output' / 'logo.png').write_bytes(b'\x89PNG\r\n\x1a\n\x00\xff')
    (root / 'src').mkdir()
    (root / 'src' / 'main.py').write_text('print("hi")\n', encoding='utf-8')
    # commit everything: the fixture's tracked wiki/ + the node's .fractal/
    # ride along, so machinery entries are genuinely present in the sets
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'seed')


def _commit_file(root: pathlib.Path, name: str, content: str, message: str) -> str:
    """Write + commit one file (targeted add), returning the sha."""
    (root / name).write_text(content, encoding='utf-8')
    _git(root, 'add', name)
    _git(root, 'commit', '-m', message)
    return _git(root, 'rev-parse', 'HEAD').stdout.strip()


def _commit_iter(node: Node, run_id: int, iter_id: int, name: str) -> str:
    """Commit a file as one iteration, logging the commit event anchors read."""
    sha = _commit_file(node.worktree, name, name, name)
    node.record.event_start('commit', metadata=sha, run_id=run_id, iter_id=iter_id)
    return sha


def _merge_side_branch(root: pathlib.Path, name: str) -> None:
    """Commit ``name`` on a side branch and merge it in (synced-in content)."""
    branch = _git(root, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
    _git(root, 'checkout', '-q', '-b', 'side')
    _commit_file(root, name, f'{name}\n', f'side: {name}')
    _git(root, 'checkout', '-q', branch)
    _git(root, 'merge', '--no-ff', '--no-edit', 'side')
    _git(root, 'branch', '-D', 'side')


def _changed_names(node: Node, since: str) -> set[str]:
    """The changed listing's path set at one anchor."""
    return {entry['path'] for entry in node.files.list(since=since)}
