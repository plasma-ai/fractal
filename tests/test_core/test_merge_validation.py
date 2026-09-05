"""Test destination validation on real ``Node.merge`` squash candidates."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

from fractal.core.node import Node
from tests._helpers import _git

from .conftest import _make_git_repo, _parse_project_dir

__all__ = [
    'test_merge_validates_the_destination_candidate',
    'test_merge_validation_refusal_restores_fresh_target',
    'test_merge_validation_refusal_preserves_hand_resolution',
]


@pytest.mark.parametrize('surface', ['api', 'cli'])
def test_merge_validates_the_destination_candidate(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    """Validate the final destination bytes, commit them, and skip a no-op."""
    repo = _make_git_repo(tmp_path / 'destination with spaces' / 'repo')
    receipt = tmp_path / 'validated-tree.txt'
    monkeypatch.setenv('VALIDATION_EXPECTED_DIR', str(repo.resolve()))
    monkeypatch.setenv('VALIDATION_RECEIPT', str(receipt))
    node = _prepare_candidate(
        repo,
        'set -euo pipefail\n'
        '[[ "$(pwd -P)" == "$VALIDATION_EXPECTED_DIR" ]]\n'
        '[[ ! -d .fractal/main/scripts ]]\n'
        '[[ ! -e .fractal/main.feature ]]\n'
        '[[ "$(cat .fractal/reference.txt)" == "retained runtime" ]]\n'
        '[[ "$(git show :feature.txt)" == "candidate work" ]]\n'
        '[[ "$(git show :wiki/_index.md)" == *"[[topic"* ]]\n'
        '[[ "$(cat wiki/_index.md)" == *"[[topic"* ]]\n'
        'git write-tree >> "$VALIDATION_RECEIPT"\n'
        'echo "candidate accepted"\n',
    )
    assert not (repo / '.fractal' / 'main' / 'scripts').exists()

    if surface == 'api':
        output, notices = node.merge(validation_script='checks/check merge.sh')
    else:
        executable = pathlib.Path(sys.executable).parent / 'fractal'
        source = pathlib.Path(__file__).resolve().parents[2]
        env = dict(os.environ)
        env['PYTHONPATH'] = str(source)
        result = subprocess.run(
            [
                str(executable),
                'node',
                'merge',
                f'--path={node.worktree}',
                '--validate=checks/check merge.sh',
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        output, notices = result.stdout, result.stderr

    assert 'Squash-merged' in output
    assert 'candidate accepted' not in output
    assert 'candidate accepted' in notices
    tree = _git(repo, 'rev-parse', 'HEAD^{tree}').stdout.strip()
    assert receipt.read_text(encoding='utf-8').splitlines() == [tree]
    assert _git(repo, 'status', '--porcelain').stdout == ''
    assert not (repo / '.fractal' / node.branch).exists()
    assert _git(repo, 'ls-files', f'.fractal/{node.branch}').stdout == ''
    head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    _git(node.worktree, 'merge-base', '--is-ancestor', head, 'HEAD')

    # A missing validator must not turn a seed-only re-merge into a failure.
    output, _ = node.merge(validation_script='checks/missing.sh')
    assert 'Nothing to merge' in output
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == head
    assert receipt.read_text(encoding='utf-8').splitlines() == [tree]


@pytest.mark.parametrize(
    ('failure', 'script'),
    [
        ('nonzero', 'exit 23\n'),
        ('missing', 'exit 0\n'),
        ('absolute', 'exit 0\n'),
        ('traversal', 'exit 0\n'),
        ('symlink', 'exit 0\n'),
        ('outside-directory', 'exit 0\n'),
        ('staged-mutation', 'echo altered > feature.txt\ngit add feature.txt\n'),
        ('unstaged-mutation', 'echo altered > feature.txt\n'),
    ],
)
def test_merge_validation_refusal_restores_fresh_target(
    git_repo: pathlib.Path,
    tmp_path_factory: pytest.TempPathFactory,
    failure: str,
    script: str,
) -> None:
    """Invalid or mutating validation cannot commit or advance the child."""
    node = _prepare_candidate(git_repo, script)
    validation_script = 'checks/check merge.sh'
    if failure == 'missing':
        validation_script = 'checks/missing.sh'
    elif failure == 'absolute':
        validation_script = str(git_repo / validation_script)
    elif failure == 'traversal':
        validation_script = 'checks/../checks/check merge.sh'
    elif failure == 'symlink':
        validation_script = 'checks/link.sh'
        (git_repo / validation_script).symlink_to('check merge.sh')
    elif failure == 'outside-directory':
        outside = tmp_path_factory.mktemp('outside_validator')
        (outside / 'check.sh').write_text('exit 0\n', encoding='utf-8')
        (git_repo / 'checks' / 'outside').symlink_to(outside, target_is_directory=True)
        validation_script = 'checks/outside/check.sh'
    target_head = _git(git_repo, 'rev-parse', 'HEAD').stdout
    child_head = _git(node.worktree, 'rev-parse', 'HEAD').stdout

    with pytest.raises(RuntimeError, match=r'[Vv]alidat'):
        node.merge(validation_script=validation_script)

    assert _git(git_repo, 'rev-parse', 'HEAD').stdout == target_head
    assert _git(node.worktree, 'rev-parse', 'HEAD').stdout == child_head
    assert _git(git_repo, 'status', '--porcelain', '--untracked-files=no').stdout == ''
    assert not (git_repo / 'feature.txt').exists()
    assert not (git_repo / '.fractal' / node.branch).exists()
    assert (git_repo / '.fractal' / 'reference.txt').read_text(
        encoding='utf-8',
    ) == 'retained runtime\n'


@pytest.mark.parametrize(
    'failure',
    ['nonzero', 'missing', 'staged-mutation', 'unstaged-mutation'],
)
def test_merge_validation_refusal_preserves_hand_resolution(
    git_repo: pathlib.Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """A refused continuation retains the operator's resolution for retry."""
    permission = tmp_path_factory.mktemp('validation') / 'allow'
    monkeypatch.setenv('VALIDATION_ALLOW', str(permission))
    script = 'test -e "$VALIDATION_ALLOW"\n'
    if failure.endswith('-mutation'):
        script = (
            'if [[ ! -e "$VALIDATION_ALLOW" ]]; then\n'
            '    echo "validator edit" > feature.txt\n'
        )
        if failure == 'staged-mutation':
            script += '    git add feature.txt\n'
        script += 'fi\n'
    node = _prepare_candidate(git_repo, script)
    for worktree, text in (
        (git_repo, 'target line\n'),
        (node.worktree, 'child line\n'),
    ):
        (worktree / 'README.md').write_text(text, encoding='utf-8')
        _git(worktree, 'add', 'README.md')
        _git(worktree, 'commit', '-m', 'edit shared line')
    with pytest.raises(RuntimeError, match='conflict'):
        node.merge(validation_script='checks/check merge.sh')

    # The operator redoes the conflicted squash and stages a semantic resolution.
    result = subprocess.run(
        ['git', 'merge', '--squash', node.branch],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    (git_repo / 'README.md').write_text('resolved line\n', encoding='utf-8')
    _git(git_repo, 'add', 'README.md')
    target_head = _git(git_repo, 'rev-parse', 'HEAD').stdout
    child_head = _git(node.worktree, 'rev-parse', 'HEAD').stdout

    validation_script = (
        'checks/missing.sh' if failure == 'missing' else 'checks/check merge.sh'
    )
    with pytest.raises(RuntimeError, match=r'[Vv]alidat'):
        node.merge(continue_merge=True, validation_script=validation_script)

    assert _git(git_repo, 'rev-parse', 'HEAD').stdout == target_head
    assert _git(node.worktree, 'rev-parse', 'HEAD').stdout == child_head
    assert _git(git_repo, 'show', ':README.md').stdout == 'resolved line\n'
    assert (git_repo / 'README.md').read_text(encoding='utf-8') == 'resolved line\n'
    expected = (
        'validator edit\n' if failure == 'staged-mutation' else 'candidate work\n'
    )
    assert _git(git_repo, 'show', ':feature.txt').stdout == expected
    expected = (
        'validator edit\n' if failure.endswith('-mutation') else 'candidate work\n'
    )
    assert (git_repo / 'feature.txt').read_text(encoding='utf-8') == expected
    assert _git(git_repo, 'ls-files', '-u').stdout == ''
    assert not (git_repo / '.fractal' / node.branch).exists()

    if failure.endswith('-mutation'):
        (git_repo / 'feature.txt').write_text('candidate work\n', encoding='utf-8')
        _git(git_repo, 'add', 'feature.txt')
    permission.touch()
    node.merge(continue_merge=True, validation_script='checks/check merge.sh')
    assert _git(git_repo, 'show', 'HEAD:README.md').stdout == 'resolved line\n'
    assert _git(git_repo, 'status', '--porcelain').stdout == ''
    head = _git(git_repo, 'rev-parse', 'HEAD').stdout.strip()
    _git(node.worktree, 'merge-base', '--is-ancestor', head, 'HEAD')


# ------ helpers


def _prepare_candidate(repo: pathlib.Path, script: str) -> Node:
    """Create a root validator and a child carrying work, a wiki page and seeds."""
    root = Node(repo)
    root.init(agent='claude', user=True)
    checks = repo / 'checks'
    checks.mkdir()
    (checks / 'check merge.sh').write_text(script, encoding='utf-8')
    (repo / '.fractal' / 'reference.txt').write_text(
        'retained runtime\n',
        encoding='utf-8',
    )
    settings = repo / 'wiki' / '.wiki' / 'settings.json'
    settings.parent.mkdir(exist_ok=True)
    settings.write_text('{}\n', encoding='utf-8')
    (repo / '.gitattributes').write_text('**/_index.md merge=wiki\n', encoding='utf-8')
    _git(
        repo,
        'add',
        'checks',
        '.fractal/reference.txt',
        'wiki/.wiki/settings.json',
        '.gitattributes',
    )
    _git(repo, 'commit', '-m', 'destination validation')
    worktree = _parse_project_dir(root.init(name='feature'))
    node = Node(worktree)
    (worktree / 'feature.txt').write_text('candidate work\n', encoding='utf-8')
    (worktree / '.fractal' / 'reference.txt').write_text(
        'incoming runtime edit\n',
        encoding='utf-8',
    )
    (worktree / 'wiki' / 'topic.md').write_text(
        '---\nname: topic\ndesc: A topic page.\n---\n\n# topic\n\n***\n',
        encoding='utf-8',
    )
    _git(worktree, 'add', 'feature.txt', 'wiki/topic.md', '.fractal')
    _git(worktree, 'commit', '-m', 'candidate work and runtime')
    return node
