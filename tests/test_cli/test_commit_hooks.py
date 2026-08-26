"""End-to-end tests for the commit path under host git hooks.

Node worktrees share the host repo's ``.git/hooks``, so every fractal commit
contends with whatever hooks the user installed. These tests build real
repos with raw hook scripts and pin the ownership split: hook edits to code
paths are re-staged and committed (hooks own code formatting); hook rewrites
of wiki pages (the project wiki and the node memory wiki) ride the same
retry only when they preserve wiki structure and the touched roots still
lint clean, and are otherwise restored with actionable guidance; every
other guarded page (``.fractal/`` seeds) keeps byte-identity -- any rewrite
restores and fails; and ``--force`` bypasses hooks entirely so the loop's
save-the-work backstop cannot be defeated by a failing hook. Also pinned:
``fractal init`` names the formatter-safe lanes when a host hook config
exists, and a commit whose event insert fails names the lost record in its
returned notices instead of losing it silently.
"""

from __future__ import annotations

import pathlib
import subprocess
from typing import Optional

import pytest

from fractal.core import commit
from fractal.core.node import Node
from tests._helpers import _git

from .conftest import _run

__all__ = [
    'test_commit_restores_structure_breaking_wiki_rewrites',
    'test_commit_retries_hook_formatted_code_paths',
    'test_commit_retries_structure_preserving_wiki_rewrites',
    'test_commit_accepts_healthy_bare_bracket_escapes',
    'test_commit_restores_wikilink_escaping_rewrites',
    'test_commit_lint_backstop_catches_value_mutations',
    'test_commit_removes_hook_added_guarded_pages',
    'test_commit_restores_hook_deleted_guarded_pages',
    'test_commit_restores_seed_frontmatter_mutations',
    'test_commit_restores_single_invariant_violations',
    'test_user_init_retries_structure_preserving_rewrites',
    'test_commit_lint_backstop_covers_the_memory_root',
    'test_commit_restores_machine_file_rewrites_under_wiki_roots',
    'test_commit_tolerates_pre_existing_lint_failures',
    'test_force_commit_bypasses_a_failing_hook',
    'test_commit_warns_when_the_commit_event_is_not_recorded',
    'test_commit_hints_when_ignore_rules_skip_files',
    'test_commit_warns_on_large_staged_files',
    'test_user_init_commit_restores_hook_mutated_pages',
    'test_user_init_names_the_formatter_lanes',
]

# mutate-once formatter stubs: rewrite the target file and fail the run that
# mutated it (pre-commit's "files were modified by this hook" contract), then
# pass once the file is already "formatted"
_WIKI_FORMATTER_HOOK = """\
#!/usr/bin/env bash
if grep -q 'unformatted' wiki/_index.md 2>/dev/null; then
    printf 'FORMATTED\\n' >wiki/_index.md
    exit 1
fi
exit 0
"""
_CODE_FORMATTER_HOOK = """\
#!/usr/bin/env bash
if grep -q 'unformatted' tool.py 2>/dev/null; then
    printf 'FORMATTED = True\\n' >tool.py
    exit 1
fi
exit 0
"""
# structure-preserving formatter: swaps the marker word in place, keeping the
# frontmatter fence, every wikilink, and the *** separator intact (a rewrap)
_REWRAP_FORMATTER_HOOK = """\
#!/usr/bin/env bash
if grep -q 'unformatted' wiki/_index.md 2>/dev/null; then
    sed -i.bak 's/unformatted/reflowed/' wiki/_index.md
    rm -f wiki/_index.md.bak
    exit 1
fi
exit 0
"""
# wiki-aware formatter's healthy escape: a bare non-wikilink '[[' becomes
# '[\\[' (the raw opener count drops; the normalized count is unchanged)
_BARE_ESCAPE_FORMATTER_HOOK = """\
#!/usr/bin/env bash
if grep -qF 'unformatted [[bare' wiki/_index.md 2>/dev/null; then
    python3 -c "
import pathlib
p = pathlib.Path('wiki/_index.md')
t = p.read_text()
p.write_text(t.replace('unformatted [[bare', 'formatted [\\\\\\\\[bare'))
"
    exit 1
fi
exit 0
"""
# structure-blind formatter: escapes a real wikilink ('\\[[' appears), the
# damage the escaped-opener invariant exists to catch
_LINK_ESCAPING_FORMATTER_HOOK = """\
#!/usr/bin/env bash
if grep -qF '[[a/_index|a/]]' wiki/_index.md 2>/dev/null; then
    python3 -c "
import pathlib
p = pathlib.Path('wiki/_index.md')
t = p.read_text()
p.write_text(t.replace('[[a/_index|a/]]', '\\\\\\\\[[a/_index|a/]\\\\\\\\]'))
"
    exit 1
fi
exit 0
"""
# value-mutating formatter: breaks a wikilink TARGET behind unchanged
# structure (same opener count, fence, separators) -- only lint sees it
_TARGET_MUTATING_FORMATTER_HOOK = """\
#!/usr/bin/env bash
if grep -qF '[[a/_index|a/]]' wiki/_index.md 2>/dev/null; then
    python3 -c "
import pathlib
p = pathlib.Path('wiki/_index.md')
t = p.read_text()
p.write_text(t.replace('[[a/_index|a/]]', '[[missing/_index|a/]]'))
"
    exit 1
fi
exit 0
"""
# hook that ADDS a page under the guarded wiki prefix -- never a formatting
# rewrite, so the gate must remove it or a bare retry lands it ungated
_PAGE_ADDING_HOOK = """\
#!/usr/bin/env bash
if [ ! -f wiki/injected.md ]; then
    printf 'injected\\n' >wiki/injected.md
    exit 1
fi
exit 0
"""
# hook that DELETES a guarded page -- the restore must bring it back
_PAGE_DELETING_HOOK = """\
#!/usr/bin/env bash
if [ -f wiki/_index.md ]; then
    rm wiki/_index.md
    exit 1
fi
exit 0
"""
# hook that flips load-bearing seed frontmatter behind an intact fence --
# byte-identity is the seed pages' guard, so any rewrite is damage
_SEED_MUTATING_HOOK = """\
#!/usr/bin/env bash
STEP=.fractal/main.task/steps/04-COMMIT.md
if grep -q 'requires_approval: false' "$STEP" 2>/dev/null; then
    python3 -c "
import pathlib
p = pathlib.Path('.fractal/main.task/steps/04-COMMIT.md')
t = p.read_text()
p.write_text(t.replace('requires_approval: false', 'requires_approval: true'))
"
    exit 1
fi
exit 0
"""
# single-invariant violations: each hook breaks exactly one structural
# invariant, so deleting any one check from the gate fails its test
_SEPARATOR_DROPPING_HOOK = """\
#!/usr/bin/env bash
if grep -qx '\\*\\*\\*' wiki/_index.md 2>/dev/null; then
    python3 -c "
import pathlib
p = pathlib.Path('wiki/_index.md')
t = p.read_text()
p.write_text(t.replace('\\n***\\n', '\\n'))
"
    exit 1
fi
exit 0
"""
_FENCE_BREAKING_HOOK = """\
#!/usr/bin/env bash
if head -1 wiki/_index.md 2>/dev/null | grep -qx -- '---'; then
    python3 -c "
import pathlib
p = pathlib.Path('wiki/_index.md')
t = p.read_text()
p.write_text(t.replace('---\\n', '- - -\\n', 1))
"
    exit 1
fi
exit 0
"""
_LINK_DROPPING_HOOK = """\
#!/usr/bin/env bash
if grep -qF '[[a/_index|a/]]' wiki/_index.md 2>/dev/null; then
    python3 -c "
import pathlib
p = pathlib.Path('wiki/_index.md')
t = p.read_text()
p.write_text(t.replace('[[a/_index|a/]]', 'a/'))
"
    exit 1
fi
exit 0
"""
# hook that rewrites a machine file under the wiki root -- markdown
# invariants are vacuous for JSON, so byte-identity is its only guard
_MACHINE_FILE_MUTATING_HOOK = """\
#!/usr/bin/env bash
SETTINGS=wiki/.wiki/settings.json
if [ -f "$SETTINGS" ] && ! grep -qF '{}' "$SETTINGS" 2>/dev/null; then
    printf '{}\\n' >"$SETTINGS"
    exit 1
fi
exit 0
"""
# a hard-failing hook that never mutates (a lint gate rejecting the commit)
_REJECTING_HOOK = """\
#!/usr/bin/env bash
exit 1
"""
# a passing hook (a hook-managed repo whose hooks never interfere)
_PASSING_HOOK = """\
#!/usr/bin/env bash
exit 0
"""

# the page content "authored" by the wiki tooling, with the marker the
# formatter stubs rewrite (standing in for mdformat's wikilink escaping)
_AUTHORED_PAGE = '---\nname: wiki\n---\n# wiki\n\nunformatted [[a/_index|a/]]\n\n***\n'


# ------ worker commits (commit.commit)


def test_commit_restores_structure_breaking_wiki_rewrites(
    tmp_path: pathlib.Path,
) -> None:
    """A structure-breaking hook rewrite fails the commit and restores the page.

    A whole-page overwrite destroys the frontmatter fence, the wikilinks,
    and the ``***`` separator at once -- the structural gate refuses it, the
    commit fails with guidance, the authored bytes are restored, and nothing
    lands on the branch.
    """
    repo = _hooked_repo(tmp_path / 'guarded', _WIKI_FORMATTER_HOOK)
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = repo / '.worktrees' / 'main.task'
    page = task / 'wiki' / '_index.md'
    page.write_text(_AUTHORED_PAGE, encoding='utf-8')
    # the pipeline refreshes the page before staging, so the bytes the guard
    # must round-trip are the wiki tooling's own refreshed output
    wiki_dir = task / 'wiki'
    subprocess.run(
        ['wiki', 'update', f'--path={wiki_dir}'],
        capture_output=True,
        check=True,
    )
    authored = page.read_text(encoding='utf-8')
    head = _git(task, 'rev-parse', 'HEAD').stdout.strip()
    result = _run(task, 'commit', 'wiki work')
    # the mutation fails the commit with actionable guidance, not corruption
    assert result.returncode != 0
    assert 'generated pages' in result.stderr
    # the guidance names the lanes -- the plugin lane advising the conflicting
    # mdformat-frontmatter away -- and never prescribes an exclude
    assert 'additional_dependencies: [mdformat-wiki]' in result.stderr
    assert 'mdformat-frontmatter' in result.stderr
    assert 'off the wiki paths' in result.stderr
    assert 'exclude:' not in result.stderr
    # the page round-trips byte-identical and no commit landed
    assert page.read_text(encoding='utf-8') == authored
    assert _git(task, 'rev-parse', 'HEAD').stdout.strip() == head


def test_commit_retries_hook_formatted_code_paths(tmp_path: pathlib.Path) -> None:
    """Hook edits confined to code are re-staged and committed once.

    Hooks own code formatting, so the retry commits the hook's version -- the
    safe half of the ownership split, unchanged by the generated-page guard.
    """
    repo = _hooked_repo(tmp_path / 'formatted', _CODE_FORMATTER_HOOK)
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = repo / '.worktrees' / 'main.task'
    (task / 'tool.py').write_text('unformatted = True\n', encoding='utf-8')
    result = _run(task, 'commit', 'code work')
    assert result.returncode == 0, result.stderr
    committed = _git(task, 'show', 'HEAD:tool.py').stdout
    assert committed == 'FORMATTED = True\n'


def test_commit_retries_structure_preserving_wiki_rewrites(
    tmp_path: pathlib.Path,
) -> None:
    """A hook rewrite that preserves wiki structure is re-staged and committed.

    Byte-identity is not required of generated pages: a wiki-aware formatter
    legitimately rewraps prose, so a rewrite keeping the frontmatter fence,
    every wikilink, and the ``***`` separators rides the retry like any code
    reformat, and the hook's bytes land on the branch.
    """
    repo = _hooked_repo(tmp_path / 'rewrap', _REWRAP_FORMATTER_HOOK)
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = repo / '.worktrees' / 'main.task'
    page = task / 'wiki' / '_index.md'
    page.write_text(_AUTHORED_PAGE, encoding='utf-8')
    result = _run(task, 'commit', 'wiki work')
    assert result.returncode == 0, result.stderr
    committed = _git(task, 'show', 'HEAD:wiki/_index.md').stdout
    assert 'reflowed' in committed
    assert '[[a/_index|a/]]' in committed


def test_commit_accepts_healthy_bare_bracket_escapes(
    tmp_path: pathlib.Path,
) -> None:
    r"""A wiki-aware escape of a bare non-wikilink ``[[`` rides the retry.

    The raw opener count drops when ``[[`` prose becomes ``[\[``, but the
    normalized count is unchanged -- a correctly configured repo must never
    dead-end on its own formatter's canonical output.
    """
    repo = _hooked_repo(tmp_path / 'escape', _BARE_ESCAPE_FORMATTER_HOOK)
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = repo / '.worktrees' / 'main.task'
    page = task / 'wiki' / '_index.md'
    page.write_text(
        '---\nname: wiki\n---\n# wiki\n\nunformatted [[bare brackets\n\n***\n',
        encoding='utf-8',
    )
    result = _run(task, 'commit', 'wiki work')
    assert result.returncode == 0, result.stderr
    committed = _git(task, 'show', 'HEAD:wiki/_index.md').stdout
    assert '[\\[bare' in committed


def test_commit_restores_wikilink_escaping_rewrites(
    tmp_path: pathlib.Path,
) -> None:
    r"""A hook that escapes a real wikilink is refused and the page restored.

    Escaping ``[[x]]`` to ``\[[x]\]`` preserves the raw opener count, so
    the escaped-opener invariant is what catches it: new ``\[[`` bytes are
    damage, the authored page is restored, and nothing lands.
    """
    repo = _hooked_repo(tmp_path / 'mangle', _LINK_ESCAPING_FORMATTER_HOOK)
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = repo / '.worktrees' / 'main.task'
    page = task / 'wiki' / '_index.md'
    page.write_text(_AUTHORED_PAGE, encoding='utf-8')
    # the pipeline refreshes the page before staging, so the bytes the gate
    # must restore are the wiki tooling's own refreshed output
    subprocess.run(
        ['wiki', 'update', f'--path={task / "wiki"}'],
        capture_output=True,
        check=True,
    )
    authored = page.read_text(encoding='utf-8')
    head = _git(task, 'rev-parse', 'HEAD').stdout.strip()
    result = _run(task, 'commit', 'wiki work')
    assert result.returncode != 0
    assert 'generated pages' in result.stderr
    assert page.read_text(encoding='utf-8') == authored
    assert _git(task, 'rev-parse', 'HEAD').stdout.strip() == head


def test_commit_lint_backstop_catches_value_mutations(
    tmp_path: pathlib.Path,
) -> None:
    """A structure-preserving rewrite that breaks wiki integrity is refused.

    Mutating a wikilink's target keeps every structural invariant, so the
    lint backstop is the layer that catches it: the touched wiki root fails
    ``wiki lint``, the page is restored, and the error carries the lint
    finding.
    """
    repo = _hooked_repo(tmp_path / 'target', _TARGET_MUTATING_FORMATTER_HOOK)
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = repo / '.worktrees' / 'main.task'
    # a real linked page so the wiki lints clean before the hook's damage
    linked = task / 'wiki' / 'a'
    linked.mkdir()
    (linked / '_index.md').write_text(
        '---\nname: a\n---\n# a\n\n***\n',
        encoding='utf-8',
    )
    page = task / 'wiki' / '_index.md'
    page.write_text(_AUTHORED_PAGE.replace('unformatted ', ''), encoding='utf-8')
    # the pipeline refreshes the page before staging, so the bytes the gate
    # must restore are the wiki tooling's own refreshed output
    subprocess.run(
        ['wiki', 'update', f'--path={task / "wiki"}'],
        capture_output=True,
        check=True,
    )
    authored = page.read_text(encoding='utf-8')
    head = _git(task, 'rev-parse', 'HEAD').stdout.strip()
    result = _run(task, 'commit', 'wiki work')
    assert result.returncode != 0
    assert 'wiki integrity' in result.stderr
    assert page.read_text(encoding='utf-8') == authored
    assert _git(task, 'rev-parse', 'HEAD').stdout.strip() == head


def test_commit_removes_hook_added_guarded_pages(tmp_path: pathlib.Path) -> None:
    """A page a hook ADDS under a guarded prefix is removed, never committed.

    An added page has no authored bytes to restore, so its restore is
    removal -- left in place (or crashed over), the very next bare commit
    would land the hook-generated page with no gate at all.
    """
    repo = _hooked_repo(tmp_path / 'added', _PAGE_ADDING_HOOK)
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = repo / '.worktrees' / 'main.task'
    (task / 'wiki' / '_index.md').write_text(_AUTHORED_PAGE, encoding='utf-8')
    head = _git(task, 'rev-parse', 'HEAD').stdout.strip()
    result = _run(task, 'commit', 'wiki work')
    assert result.returncode != 0
    assert 'generated pages' in result.stderr
    assert not (task / 'wiki' / 'injected.md').exists()
    staged = _git(task, 'diff', '--cached', '--name-only').stdout
    assert 'injected.md' not in staged
    assert _git(task, 'rev-parse', 'HEAD').stdout.strip() == head


def test_commit_restores_hook_deleted_guarded_pages(tmp_path: pathlib.Path) -> None:
    """A guarded page a hook DELETES is restored and the commit fails."""
    repo = _hooked_repo(tmp_path / 'deleted', _PAGE_DELETING_HOOK)
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = repo / '.worktrees' / 'main.task'
    page = task / 'wiki' / '_index.md'
    page.write_text(_AUTHORED_PAGE, encoding='utf-8')
    subprocess.run(
        ['wiki', 'update', f'--path={task / "wiki"}'],
        capture_output=True,
        check=True,
    )
    authored = page.read_text(encoding='utf-8')
    head = _git(task, 'rev-parse', 'HEAD').stdout.strip()
    result = _run(task, 'commit', 'wiki work')
    assert result.returncode != 0
    assert 'generated pages' in result.stderr
    assert page.read_text(encoding='utf-8') == authored
    assert _git(task, 'rev-parse', 'HEAD').stdout.strip() == head


def test_commit_restores_seed_frontmatter_mutations(tmp_path: pathlib.Path) -> None:
    """A hook flip of load-bearing seed frontmatter is refused byte-for-byte.

    Seed pages keep byte-identity: their frontmatter is machine input the
    loop executes (approval gates, per-step overrides), no lint validates
    it, and a silent flip would run the node on hook-mutated config.
    """
    repo = _hooked_repo(tmp_path / 'seed', _SEED_MUTATING_HOOK)
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = repo / '.worktrees' / 'main.task'
    step = task / '.fractal' / 'main.task' / 'steps' / '04-COMMIT.md'
    authored = step.read_text(encoding='utf-8')
    (task / 'wiki' / '_index.md').write_text(_AUTHORED_PAGE, encoding='utf-8')
    head = _git(task, 'rev-parse', 'HEAD').stdout.strip()
    result = _run(task, 'commit', 'wiki work')
    assert result.returncode != 0
    assert 'generated pages' in result.stderr
    assert step.read_text(encoding='utf-8') == authored
    assert _git(task, 'rev-parse', 'HEAD').stdout.strip() == head


@pytest.mark.parametrize(
    argnames=('name', 'hook'),
    argvalues=[
        ('separator', _SEPARATOR_DROPPING_HOOK),
        ('fence', _FENCE_BREAKING_HOOK),
        ('opener', _LINK_DROPPING_HOOK),
    ],
    ids=['separator-drop', 'fence-break', 'opener-loss'],
)
def test_commit_restores_single_invariant_violations(
    tmp_path: pathlib.Path,
    name: str,
    hook: str,
) -> None:
    """Each structural invariant refuses a rewrite violating only itself.

    The hooks break exactly one invariant apiece -- the ``***`` separator
    count, the frontmatter fence, and the wikilink opener count -- so every
    check in the gate is individually load-bearing (a whole-page mangle
    exercises them all at once and would mask a deleted check).
    """
    repo = _hooked_repo(tmp_path / name, hook)
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = repo / '.worktrees' / 'main.task'
    # the wikilink's target must exist: update prunes rows whose targets
    # vanished, and a pruned row would no-op the opener-dropping hook
    linked = task / 'wiki' / 'a'
    linked.mkdir()
    (linked / '_index.md').write_text(
        '---\nname: a\n---\n# a\n\n***\n',
        encoding='utf-8',
    )
    page = task / 'wiki' / '_index.md'
    page.write_text(_AUTHORED_PAGE.replace('unformatted ', ''), encoding='utf-8')
    subprocess.run(
        ['wiki', 'update', f'--path={task / "wiki"}'],
        capture_output=True,
        check=True,
    )
    authored = page.read_text(encoding='utf-8')
    head = _git(task, 'rev-parse', 'HEAD').stdout.strip()
    result = _run(task, 'commit', 'wiki work')
    assert result.returncode != 0
    assert 'generated pages' in result.stderr
    assert page.read_text(encoding='utf-8') == authored
    assert _git(task, 'rev-parse', 'HEAD').stdout.strip() == head


def test_user_init_retries_structure_preserving_rewrites(
    tmp_path: pathlib.Path,
) -> None:
    """The baseline gates every rewrite and retries structure-preserving ones.

    A wiki-aware rewrap during ``commit --init`` re-stages and retries into
    a single baseline commit, and the pathspec'd commit leaves unrelated
    user-staged work staged and out of the baseline.
    """
    repo = _hooked_repo(tmp_path / 'baseline', _REWRAP_FORMATTER_HOOK)
    (repo / 'wiki' / '_index.md').write_text(_AUTHORED_PAGE, encoding='utf-8')
    (repo / 'unrelated.txt').write_text('user work\n', encoding='utf-8')
    _git(repo, 'add', 'unrelated.txt')
    result = _run(repo, 'commit', 'configure', '--init')
    assert result.returncode == 0, result.stderr
    committed = _git(repo, 'show', 'HEAD:wiki/_index.md').stdout
    assert 'reflowed' in committed
    # the user's staged work survives the retry and stays out of the baseline
    staged = _git(repo, 'diff', '--cached', '--name-only').stdout
    assert 'unrelated.txt' in staged
    shown = subprocess.run(
        ['git', 'show', 'HEAD:unrelated.txt'],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert shown.returncode != 0


def test_commit_lint_backstop_covers_the_memory_root(
    tmp_path: pathlib.Path,
) -> None:
    """The lint backstop also guards the node memory wiki root."""
    hook = _TARGET_MUTATING_FORMATTER_HOOK.replace(
        'wiki/_index.md',
        '.fractal/main.task/memory/_index.md',
    )
    repo = _hooked_repo(tmp_path / 'memory', hook)
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = repo / '.worktrees' / 'main.task'
    memory = task / '.fractal' / 'main.task' / 'memory'
    (memory / 'a').mkdir()
    (memory / 'a' / '_index.md').write_text(
        '---\nname: a\n---\n# a\n\n***\n',
        encoding='utf-8',
    )
    page = memory / '_index.md'
    page.write_text(
        '---\nname: memory\n---\n# memory\n\n[[a/_index|a/]]\n\n***\n',
        encoding='utf-8',
    )
    subprocess.run(
        ['wiki', 'update', f'--path={memory}'],
        capture_output=True,
        check=True,
    )
    authored = page.read_text(encoding='utf-8')
    head = _git(task, 'rev-parse', 'HEAD').stdout.strip()
    result = _run(task, 'commit', 'memory work')
    assert result.returncode != 0
    assert 'wiki integrity' in result.stderr
    assert page.read_text(encoding='utf-8') == authored
    assert _git(task, 'rev-parse', 'HEAD').stdout.strip() == head


def test_commit_restores_machine_file_rewrites_under_wiki_roots(
    tmp_path: pathlib.Path,
) -> None:
    """A hook rewrite of a non-markdown file under a wiki root is refused.

    Markdown invariants are vacuous for machine files (a wholesale JSON
    rewrite keeps every count at zero), so byte-identity is their guard --
    the structure-judged tier covers only ``.md`` pages under the roots.
    """
    repo = _hooked_repo(tmp_path / 'machine', _MACHINE_FILE_MUTATING_HOOK)
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = repo / '.worktrees' / 'main.task'
    settings = task / 'wiki' / '.wiki'
    settings.mkdir(exist_ok=True)
    machine = settings / 'settings.json'
    machine.write_text('{"theme": "authored"}\n', encoding='utf-8')
    authored = machine.read_text(encoding='utf-8')
    head = _git(task, 'rev-parse', 'HEAD').stdout.strip()
    result = _run(task, 'commit', 'wiki work')
    assert result.returncode != 0
    assert 'generated pages' in result.stderr
    assert machine.read_text(encoding='utf-8') == authored
    assert _git(task, 'rev-parse', 'HEAD').stdout.strip() == head


def test_commit_tolerates_pre_existing_lint_failures(
    tmp_path: pathlib.Path,
) -> None:
    """A pre-existing lint failure soft-warns instead of blaming the hook.

    The backstop restores and fails only when a touched root linted clean
    before the hook's rewrite; a root that was already dirty keeps the
    pipeline's soft-warn tolerance, the healthy rewrite rides the retry,
    and the notice rides the commit output.
    """
    repo = _hooked_repo(tmp_path / 'predirty', _REWRAP_FORMATTER_HOOK)
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = repo / '.worktrees' / 'main.task'
    linked = task / 'wiki' / 'a'
    linked.mkdir()
    (linked / '_index.md').write_text(
        '---\nname: a\n---\n# a\n\n***\n',
        encoding='utf-8',
    )
    page = task / 'wiki' / '_index.md'
    page.write_text(_AUTHORED_PAGE, encoding='utf-8')
    subprocess.run(
        ['wiki', 'update', f'--path={task / "wiki"}'],
        capture_output=True,
        check=True,
    )
    # append formatter damage the tooling cannot heal -- an escaped wikilink
    # opener is a lint ISSUE that update tolerates and never repairs (a
    # broken row would just be pruned) -- so the root is lint-dirty before
    # the hook runs
    updated = page.read_text(encoding='utf-8')
    page.write_text(
        updated + '\nEscaped \\[[a/_index|a]] link.\n',
        encoding='utf-8',
    )
    result = _run(task, 'commit', 'wiki work')
    assert result.returncode == 0, result.stderr
    committed = _git(task, 'show', 'HEAD:wiki/_index.md').stdout
    assert 'reflowed' in committed
    assert 'pre-existing' in result.stdout + result.stderr


def test_force_commit_bypasses_a_failing_hook(tmp_path: pathlib.Path) -> None:
    """``--force`` commits past a hard-failing hook.

    The loop's force-commit backstops exist to save otherwise-lost work (a
    later ``--continue`` discards uncommitted changes); a hook that can veto the
    backstop defeats it, so force bypasses hooks like it bypasses scope and
    lint.
    """
    repo = _hooked_repo(tmp_path / 'backstop', _REJECTING_HOOK)
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = repo / '.worktrees' / 'main.task'
    (task / 'rescue.txt').write_text('work worth saving\n', encoding='utf-8')
    # an ordinary commit is rejected by the hook
    assert _run(task, 'commit', 'blocked').returncode != 0
    # the force backstop saves the work anyway
    result = _run(task, 'commit', 'saved', '--force')
    assert result.returncode == 0, result.stderr
    subject = _git(task, 'log', '-1', '--format=%s').stdout
    assert 'saved' in subject


def test_commit_warns_when_the_commit_event_is_not_recorded(
    tmp_path: pathlib.Path,
) -> None:
    """A commit whose event insert fails still lands and names the lost record.

    Event emission is deliberately non-fatal (telemetry must never block the
    save path), but a swallowed insert failure erases the commit from the
    node's history without a trace (lineage ids inherited from a foreign run
    poison the insert). The commit must succeed AND say the record was lost.
    """
    repo = _hooked_repo(tmp_path / 'events', _PASSING_HOOK)
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = repo / '.worktrees' / 'main.task'
    (task / 'work.txt').write_text('recorded\n', encoding='utf-8')
    clean = _run(task, 'commit', 'recorded work')
    assert clean.returncode == 0, clean.stderr
    assert 'commit event not recorded' not in clean.stdout + clean.stderr
    # only explicit (loop-passed) lineage can poison the insert --
    # ambient env carries no weight -- so drive the pipeline directly
    # with foreign ids; the commit still lands and the returned
    # notices say the record was lost
    (task / 'work.txt').write_text('unrecorded\n', encoding='utf-8')
    output = commit.commit(
        node=Node(task),
        message='unrecorded work',
        run_id=9999,
        iter_id=9999,
        step_id=9999,
    )
    assert 'commit event not recorded' in output


def test_commit_hints_when_ignore_rules_skip_files(tmp_path: pathlib.Path) -> None:
    """A tracked ignore rule that eats workspace files is named at commit time.

    ``git add`` silently drops ignored paths when expanding directories, so a
    host ``.gitignore`` pattern can swallow node artifacts with no error at
    any layer. The commit stays advisory -- it lands -- but counts what the
    ignore rules dropped. Fractal's own runtime ignores (the ``info/exclude``
    block and the stage's pathspec excludes) stay silent: they are
    intentional, and warning on them every commit would train the hint away.
    So do self-ignoring dirs -- a dir whose ignore rule lives inside itself
    is a managed artifact cache (the wiki ``.cache/`` shape), not an eaten
    workspace file.
    """
    repo = _hooked_repo(tmp_path / 'ignored', _PASSING_HOOK)
    # the host repo tracks a log-eating ignore rule
    (repo / '.gitignore').write_text('*.log\n', encoding='utf-8')
    _git(repo, 'add', '.gitignore')
    _git(repo, 'commit', '-m', 'ignore logs')
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = repo / '.worktrees' / 'main.task'
    # a commit with nothing ignore-eaten prints no hint
    (task / 'kept.txt').write_text('kept\n', encoding='utf-8')
    clean = _run(task, 'commit', 'clean work')
    assert clean.returncode == 0, clean.stderr
    assert 'skipped by ignore rules' not in clean.stdout + clean.stderr
    # a self-ignoring cache dir (rule source inside the skipped path) stays
    # silent: it manages itself, and counting it would fire on every commit
    (task / 'cache').mkdir()
    (task / 'cache' / '.gitignore').write_text('*\n', encoding='utf-8')
    (task / 'cache' / 'data.json').write_text('{}\n', encoding='utf-8')
    (task / 'kept_too.txt').write_text('kept\n', encoding='utf-8')
    managed = _run(task, 'commit', 'cache work')
    assert managed.returncode == 0, managed.stderr
    assert 'skipped by ignore rules' not in managed.stdout + managed.stderr
    # a commit whose expansion drops an ignored artifact says so and lands
    (task / 'notes.txt').write_text('kept\n', encoding='utf-8')
    (task / 'evidence.log').write_text('eaten\n', encoding='utf-8')
    result = _run(task, 'commit', 'artifact work')
    assert result.returncode == 0, result.stderr
    assert 'skipped by ignore rules' in result.stdout + result.stderr
    assert 'evidence.log' not in _git(task, 'ls-files').stdout


def test_commit_warns_on_large_staged_files(tmp_path: pathlib.Path) -> None:
    """A staged file above the advisory size threshold is named at commit time.

    An oversized binary in the staged delta usually means an artifact landed
    in the commit by accident (a 50MB file can commit clean with no warning
    at any layer), but large commits are also legitimate -- so the guard
    warns and never blocks: the commit lands AND stderr names the file with
    its size.
    """
    repo = _hooked_repo(tmp_path / 'large', _PASSING_HOOK)
    assert _run(repo, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    task = repo / '.worktrees' / 'main.task'
    # a commit with only small files prints no size warning
    (task / 'small.txt').write_text('small\n', encoding='utf-8')
    clean = _run(task, 'commit', 'small work')
    assert clean.returncode == 0, clean.stderr
    assert 'exceed' not in clean.stdout + clean.stderr
    # a commit staging an oversized file lands anyway and is told what it did
    (task / 'blob.bin').write_bytes(b'\0' * (11 * 1024 * 1024))
    result = _run(task, 'commit', 'artifact work')
    assert result.returncode == 0, result.stderr
    assert 'exceed 10MB' in result.stdout + result.stderr
    assert 'blob.bin (11MB)' in result.stdout + result.stderr
    assert 'blob.bin' in _git(task, 'ls-files').stdout


# ------ user-node baseline commits (commit.commit_user_init)


def test_user_init_commit_restores_hook_mutated_pages(
    tmp_path: pathlib.Path,
) -> None:
    """``commit --init`` restores hook-rewritten wiki pages before failing.

    The trap: the first commit fails after the hook rewrites the
    page in the working tree, and an identical retry then quietly commits the
    rewrite. Restoring the authored bytes on failure means no retry can ever
    land the corruption.
    """
    repo = _hooked_repo(tmp_path / 'userinit', _WIKI_FORMATTER_HOOK)
    page = repo / 'wiki' / '_index.md'
    page.write_text(_AUTHORED_PAGE, encoding='utf-8')
    head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    first = _run(repo, 'commit', 'baseline', '--init')
    assert first.returncode != 0
    assert 'generated pages' in first.stderr
    # the guidance names the lanes -- the plugin lane advising the conflicting
    # mdformat-frontmatter away -- and never prescribes an exclude
    assert 'additional_dependencies: [mdformat-wiki]' in first.stderr
    assert 'mdformat-frontmatter' in first.stderr
    assert 'off the wiki paths' in first.stderr
    assert 'exclude:' not in first.stderr
    # the page is restored, so a bare retry cannot commit the hook's rewrite
    assert page.read_text(encoding='utf-8') == _AUTHORED_PAGE
    retry = _run(repo, 'commit', 'baseline', '--init')
    assert retry.returncode != 0
    assert page.read_text(encoding='utf-8') == _AUTHORED_PAGE
    assert _git(repo, 'rev-parse', 'HEAD').stdout.strip() == head


# ------ user-node init (Node._init_user)


# a host hook config whose mdformat already carries the wikilink-aware plugin
_PLUGIN_CONFIG = """\
repos:
  - repo: https://github.com/hukkin/mdformat
    rev: 1.0.0
    hooks:
      - id: mdformat
        additional_dependencies: [mdformat-wiki]
"""
# a host hook config already keeping its formatter off the generated paths
# (the second lane; an exclude: filter is its common shape)
_OFF_PATHS_CONFIG = """\
repos:
  - repo: https://github.com/hukkin/mdformat
    rev: 1.0.0
    hooks:
      - id: mdformat
        exclude: ^(wiki|\\.fractal)/
"""


@pytest.mark.parametrize(
    argnames=('config', 'informs'),
    argvalues=[
        pytest.param('repos: []\n', True, id='hooks-without-plugin'),
        pytest.param(_PLUGIN_CONFIG, False, id='plugin-present'),
        pytest.param(_OFF_PATHS_CONFIG, False, id='formatters-off-paths'),
        pytest.param(None, False, id='no-hook-config'),
    ],
)
def test_user_init_names_the_formatter_lanes(
    tmp_path: pathlib.Path,
    config: Optional[str],
    informs: bool,
) -> None:
    """``fractal init`` names the formatter-safe lanes when host hooks exist.

    The check informs rather than manages: a host ``.pre-commit-config.yaml``
    gets one informational stderr block naming the safe lanes -- the
    ``mdformat-wiki`` plugin via ``additional_dependencies``, dropping a
    conflicting ``mdformat-frontmatter``, or keeping formatters off the wiki
    paths -- never a prescriptive exclude (the user's config is never edited).
    Configs already on either lane -- the plugin wired in, or hooks kept off
    the generated paths -- and repos without hook configs stay silent.
    Re-runs inform again (the check is part of init, not a one-shot).
    """
    path = tmp_path / 'verify'
    path.mkdir()
    _git(path, 'init', '-b', 'main')
    _git(path, 'config', 'user.email', 'commit-hooks@test.local')
    _git(path, 'config', 'user.name', 'commit-hooks')
    (path / 'README.md').write_text('# verify\n', encoding='utf-8')
    if config is not None:
        (path / '.pre-commit-config.yaml').write_text(config, encoding='utf-8')
    _git(path, 'add', '-A')
    _git(path, 'commit', '-m', 'init')
    init = _run(path, 'init')
    assert init.returncode == 0, init.stderr
    assert ('additional_dependencies: [mdformat-wiki]' in init.stderr) == informs
    assert ('mdformat-frontmatter' in init.stderr) == informs
    assert ('off the wiki paths' in init.stderr) == informs
    # a prescriptive exclude never appears in any case
    assert 'exclude:' not in init.stderr
    again = _run(path, 'init')
    assert again.returncode == 0, again.stderr
    assert ('additional_dependencies: [mdformat-wiki]' in again.stderr) == informs


# ------ helpers


def _hooked_repo(path: pathlib.Path, hook: str) -> pathlib.Path:
    """Create a committed git repo with a user node and a raw pre-commit hook.

    The hook is a raw ``.git/hooks/pre-commit`` script (shared by every node
    worktree), standing in for any hook framework; the committed pre-commit
    config marks the repo as hook-managed for the commit retry path.
    """
    path.mkdir(parents=True, exist_ok=True)
    _git(path, 'init', '-b', 'main')
    _git(path, 'config', 'user.email', 'commit-hooks@test.local')
    _git(path, 'config', 'user.name', 'commit-hooks')
    (path / 'README.md').write_text('# commit-hooks\n', encoding='utf-8')
    (path / '.pre-commit-config.yaml').write_text('repos: []\n', encoding='utf-8')
    wiki = path / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(path, 'add', '-A')
    _git(path, 'commit', '-m', 'init')
    assert _run(path, 'init').returncode == 0
    hook_path = path / '.git' / 'hooks' / 'pre-commit'
    hook_path.write_text(hook, encoding='utf-8')
    hook_path.chmod(0o755)
    return path
