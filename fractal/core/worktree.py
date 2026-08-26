"""Functions for the node tree's worktree, registry, and provisioning machinery."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import pathlib
import re
import shutil
import subprocess
from collections.abc import Iterator
from typing import Optional

import fractal.util
from fractal.constants import (
    FRACTAL_FOLDER,
    LOCK_FILE,
    PROJECT_FOLDER,
    SEED_IGNORE_FILE,
    WORKTREES_FOLDER,
)
from fractal.typing import PathLike

__all__ = []

# module logger (the fractal.* hierarchy; the package never configures
# handlers -- a host attaches its own)
logger = logging.getLogger(__name__)

# git stores each branch as a ref file, so a node name is bounded by the
# filesystem's 255-character path-component limit
_MAX_NAME_LENGTH = 255

# a single name segment is capped well under the branch bound -- branches
# accrete one name per level, and per-segment discipline keeps worktree
# paths, list columns, and radio senders usable
_MAX_NODE_NAME_LENGTH = 64

# exclude-block delimiters (matched only as whole lines, never substrings)
_EXCLUDE_BEGIN = '# >>> fractal >>>'
_EXCLUDE_END = '# <<< fractal <<<'


@contextlib.contextmanager
def lock(repo_dir: pathlib.Path) -> Iterator[None]:
    """Hold the tree-wide ``.worktrees`` flock for a critical section.

    Serializes worktree add/remove and every cap gate -- ``git worktree add``
    is not parallel-safe; an ``fcntl.flock`` is a kernel lock, auto-released
    if the holder dies. Callers re-read live state under the lock; each
    re-read keeps its race-naming comment at the call site.

    Args:
        repo_dir: Main git repo root.

    """
    lock_dir = repo_dir / WORKTREES_FOLDER
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / LOCK_FILE, 'a', encoding='utf-8') as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        yield


def validate_name(name: str, parent_branch: Optional[str] = None) -> None:
    """Validate a node name (and, with ``parent_branch``, its composed branch).

    Args:
        name: Node name (a single branch segment).
        parent_branch: Parent branch to compose the child branch under;
            ``None`` checks the segment rules only.

    Raises:
        ValueError: If the name breaks a charset, segment, or branch-length
            rule.

    """
    # reject '.' -- it is the branch hierarchy separator, so a dotted name
    # would collide with the <parent>.<child> scheme and break merge
    if '.' in name:
        raise ValueError(
            f"Node name cannot contain '.' (reserved as the"
            f' hierarchy separator): {name!r}'
        )
    # reject '-' -- node names use '_' as the word separator so branch and
    # worktree names stay consistent; suggest the underscore form
    if '-' in name:
        raise ValueError(f"Node name cannot contain '-' (use '_' instead): {name!r}")
    # reject '/' -- the git ref path separator
    if '/' in name:
        raise ValueError(
            f"Node name cannot contain '/' (reserved as the path separator): {name!r}"
        )
    # reject any remaining non-word character
    if not re.fullmatch(r'[A-Za-z0-9_]+', name):
        raise ValueError(
            f'Node name may only contain letters, digits, and underscores: {name!r}'
        )
    # cap the single name segment -- the composed-branch guard below still
    # owns the deep-tree bound
    if len(name) > _MAX_NODE_NAME_LENGTH:
        raise ValueError(
            f'Node name too long: {len(name)} characters'
            f' (max {_MAX_NODE_NAME_LENGTH}): {name!r}'
        )
    if parent_branch is None:
        return
    # git writes refs/heads/<branch> and a <branch>.lock, so the real bound
    # is the parent-prefixed branch plus the lock suffix
    child_branch = f'{parent_branch}.{name}'
    if len(child_branch) + len('.lock') > _MAX_NAME_LENGTH:
        budget = _MAX_NAME_LENGTH - len(parent_branch) - len('.') - len('.lock')
        raise ValueError(
            f"Node name too long: branch {child_branch!r} plus git's .lock"
            f' suffix exceeds the {_MAX_NAME_LENGTH}-character limit'
            f' (max {budget} characters under parent {parent_branch!r}).'
        )


def derive_project_name(path: pathlib.Path) -> str:
    """Derive a fractal project name from a directory's basename.

    Converts dashes to underscores and validates the result as an ASCII
    identifier (approximating ``Wiki.validate_name``'s ascii/identifier
    predicates; ``wiki init`` additionally enforces reserved-name and structural
    rules), so a bad directory name fails before any partial init is written. The
    project name doubles as the wiki name and, when bootstrapping a fresh repo,
    the initial branch name.

    Args:
        path: Repository (or target folder) directory.

    Returns:
        The sanitized project name.

    Raises:
        ValueError: If the directory name cannot yield a valid project name.

    """
    dir_name = path.name
    name = dir_name.replace('-', '_')
    if not (name and name.isascii() and f'_{name}'.isidentifier()):
        raise ValueError(
            f'Cannot derive a valid project name from directory'
            f' {dir_name!r} (got {name!r}); use ASCII letters, digits,'
            ' and underscores (dashes are converted); rename the directory.'
        )
    return name


def ensure_git_repo(path: PathLike) -> None:
    """Bootstrap a git repo at ``path`` when it has no born branch yet.

    ``fractal init`` anchors the user node at the git root, so the target must be
    a git repo whose branch is born (``_init_user`` resolves ``self.branch`` with
    ``check=True``). When the target is not inside any repo, initialize one on a
    branch named after the project -- the sanitized directory name, which also
    becomes the wiki name. Then, unless the branch is already born, birth it with
    an initial commit (an empty ``.gitignore``); this also completes a prior
    bootstrap whose commit failed (a fresh ``.git`` with an unborn branch), so the
    re-run the identity-error message promises actually works. A repo whose branch
    is already born (this folder or an ancestor) is left untouched.

    Args:
        path: Repo root or sub-project folder (absolute or relative).

    Raises:
        ValueError: If the directory name cannot yield a valid project name.
        RuntimeError: If the initial commit fails (e.g. no git identity).

    """
    # resolve to an absolute path (mirrors init_node)
    target = pathlib.Path(path)
    if not target.is_absolute():
        target = pathlib.Path.cwd() / target
    target = target.resolve()
    # done if already in a repo whose branch is born (this folder or an ancestor)
    git_dir = fractal.util.git.run(['rev-parse', '--git-dir'], cwd=target, check=False)
    if git_dir is not None:
        sha = fractal.util.git.run(
            ['rev-parse', '--verify', '--quiet', 'HEAD'],
            cwd=target,
            check=False,
        )
        if sha:
            return
    # init a fresh repo on the project-named branch; an existing repo with an
    # unborn branch (a prior init whose commit failed) skips init and just births it
    if git_dir is None:
        name = derive_project_name(target)
        fractal.util.git.run(['init', '-b', name], cwd=target)
    # birth the branch with an initial commit so _init_user can resolve it
    branch = fractal.util.git.run(['symbolic-ref', '--short', 'HEAD'], cwd=target)
    gitignore = target / '.gitignore'
    if not gitignore.exists():
        gitignore.write_text('', encoding='utf-8')
    fractal.util.git.run(['add', '.gitignore'], cwd=target)
    try:
        # scope the commit to .gitignore so a user's unrelated staged
        # work is never swept into the bootstrap commit (mirrors the
        # commit pipeline's scoping)
        fractal.util.git.run(
            ['commit', '-m', f'init {branch}', '--', '.gitignore'],
            cwd=target,
        )
    except RuntimeError as e:
        raise RuntimeError(
            f'Bootstrapped {target} but the initial commit failed ({e});'
            " configure your git identity ('git config user.name' and"
            " 'git config user.email') and re-run."
        ) from e


def project_path(repo_dir: pathlib.Path, branch: str) -> str:
    """Return a branch's project sub-path (``'.'`` for a repo-root node).

    Cached per-branch at ``.worktrees/.project/<branch>`` (written at init);
    absent for a repo-root project, which reads as ``'.'``.

    Args:
        repo_dir: Main git repo root.
        branch: The node's branch.

    Returns:
        Project sub-path within the worktree.

    """
    project_file = repo_dir / WORKTREES_FOLDER / PROJECT_FOLDER / branch
    if project_file.exists():
        return project_file.read_text(encoding='utf-8').strip()
    return '.'


def set_project_path(repo_dir: pathlib.Path, branch: str, project: str) -> None:
    """Record a branch's project sub-path in the ``.project`` cache.

    The single writer (user-node init); every consumer reads through
    :func:`project_path`.

    Args:
        repo_dir: Main git repo root.
        branch: The node's branch.
        project: Project sub-path within the worktree.

    """
    project_dir = repo_dir / WORKTREES_FOLDER / PROJECT_FOLDER
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / branch).write_text(f'{project}\n', encoding='utf-8')


def exclude_update(repo_dir: pathlib.Path) -> None:
    """Write fractal's ignore patterns into the repo-local ``info/exclude``.

    Fractal's runtime artifacts -- worktrees, databases, status files,
    agent logs -- are local and ephemeral, so they belong in the repo's
    ``.git/info/exclude`` (shared across all worktrees), not the user's
    committed ``.gitignore``. The patterns live in a marker-delimited block;
    every prior fractal block is replaced (so new patterns propagate on
    re-init) and all other ``info/exclude`` content is preserved.

    The block is static -- the shipped template verbatim, identical for
    every tree -- so no rewrite can carry per-tree state, and a second
    tree's init can never expose the first's. Each user node's own seed
    dir ignores itself via :func:`seed_ignore_write` instead.

    Idempotent and concurrency-safe: the common-dir ``info/exclude`` is
    shared by every worktree, so sibling ``init``/``start`` fan-out races on
    it. Every writer produces identical block bytes, and the rewrite computes
    the new content from a clean read and commits it with an atomic
    unique-temp ``os.replace`` -- a racing writer can never observe a
    truncated file and drop the user's lines, and a crash mid-write cannot
    orphan a half-block.

    Args:
        repo_dir: Main git repo root.

    """
    # build the managed block from the shipped template
    assets = pathlib.Path(__file__).parent.parent / '_assets'
    template = assets / 'git' / 'exclude'
    patterns = template.read_text(encoding='utf-8')
    exclude = _exclude_file(repo_dir)
    current = exclude.read_text(encoding='utf-8') if exclude.exists() else ''
    lines = current.splitlines()
    block = f'{_EXCLUDE_BEGIN}\n{patterns}{_EXCLUDE_END}\n'
    # strip every prior fractal block, preserving all other content
    kept = _strip_exclude_blocks(lines)
    body = '\n'.join(kept).rstrip('\n')
    prefix = f'{body}\n\n' if body else ''
    # atomic write, so concurrent writers don't clobber each other
    exclude.parent.mkdir(parents=True, exist_ok=True)
    fractal.util.filesystem.write_atomic(exclude, prefix + block)


def seed_tracked(node_dir: pathlib.Path) -> bool:
    """Return whether the tree tracks the user seed dir on the top-level branch.

    Tracking truth is the seed dir's own self-ignore file -- the state
    ``fractal track``/``untrack`` toggles -- so shared-block rewrites can
    never flip the choice. The probe is file presence, never
    ``git check-ignore``: check-ignore also matches a user ``.gitignore``
    entry, which would make ``fractal track`` silently non-stick.

    Args:
        node_dir: The user node's data directory.

    Returns:
        ``True`` when the seed dir carries no self-ignore file.

    """
    return not (node_dir / SEED_IGNORE_FILE).exists()


def seed_ignore_write(node_dir: pathlib.Path) -> None:
    """Self-ignore the user seed dir on the top-level branch (the default).

    Writes a ``.gitignore`` of ``*`` into the node dir: git ignores the
    whole dir, the ignore file included, with no per-tree line in any
    shared surface. Child seeds (``.fractal/<branch>.<child>``) carry no
    such file and stay tracked so meta and merge-up keep working --
    ``fractal track`` opts the top-level branch back in.

    Args:
        node_dir: The user node's data directory.

    """
    ignore = node_dir / SEED_IGNORE_FILE
    content = '# fractal runtime state (`fractal track` removes this file)\n*\n'
    # atomic like the shared block: `fractal untrack` re-asserts the default
    # under a concurrent reader (seed_tracked, a baseline commit) or a second
    # untrack -- init's own write lands in a directory it just created
    fractal.util.filesystem.write_atomic(ignore, content)


def seed_ignore_remove(node_dir: pathlib.Path) -> None:
    """Lift the user seed dir's self-ignore (``fractal track``).

    Args:
        node_dir: The user node's data directory.

    """
    (node_dir / SEED_IGNORE_FILE).unlink(missing_ok=True)


def clone_cache_dirs(
    repo_dir: pathlib.Path,
    worktree_dir: pathlib.Path,
    dirs: list[str],
) -> None:
    """Clone heavy git-ignored cache dirs from the main checkout into a worktree.

    Copy-on-write clones (``cp -c``, APFS clonefile): near-instant, no new
    disk until a file diverges, and the worktree owns its logical copy --
    concurrent builds never share a mutable file. Build state a fresh
    worktree would otherwise re-derive from scratch (a Lean ``.lake``)
    arrives warm. Best-effort by design -- an escaping entry, a missing
    source, an existing target, a filesystem without clonefile, or any
    filesystem error skips the dir, and the worktree then re-derives the
    cache exactly as it would have without the clone. Nothing here may
    raise: the caller clones only after the child node is registered, so a
    propagated error would fail a spawn whose node already exists and whose
    retry refuses as a duplicate. A failed clone never leaves a partial
    target (temp then rename; the dot-prefixed temp matches the
    ``.*-*.tmp`` ignore family, so a crash-stranded one never rides a
    commit).

    Args:
        repo_dir: Main git repo root (the clone source checkout).
        worktree_dir: The freshly created worktree (the clone target).
        dirs: Repo-relative cache directories to clone.

    """
    for rel in dirs:
        # only a real subdirectory clones: an absolute or '..' entry lands
        # outside the worktree this warms, and one naming no subdirectory
        # ('', '.') would clone the whole checkout over the worktree root.
        # The config validator rejects all three, so only a hand-edited
        # config reaches here -- and a skip beats cloning an unrelated tree
        path = pathlib.PurePosixPath(rel)
        if path.is_absolute() or '..' in path.parts or not path.parts:
            logger.info('Cache clone skipped for %s: not a repo-relative dir', rel)
            continue
        source = repo_dir / rel
        target = worktree_dir / rel
        if not source.is_dir() or target.exists():
            continue
        # clone beside the target, then rename: cp dies mid-tree on any
        # error (cross-volume, non-APFS), and a partial cache would poison
        # the build it was meant to warm; no cross-worktree collision --
        # the temp lives inside the target's own worktree
        temp = target.parent / f'.{target.name}-clone.tmp'
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(temp, ignore_errors=True)
            result = subprocess.run(
                ['cp', '-c', '-R', str(source), str(temp)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                shutil.rmtree(temp, ignore_errors=True)
                logger.info(
                    'Cache clone skipped for %s: %s', rel, result.stderr.strip()
                )
                continue
            temp.rename(target)
        except OSError as error:
            shutil.rmtree(temp, ignore_errors=True)
            logger.info('Cache clone skipped for %s: %s', rel, error)
            continue
        logger.info('Cloned cache dir %s into %s', rel, worktree_dir)


def exclude_strip(repo_dir: pathlib.Path) -> None:
    """Strip fractal's block from the shared ``info/exclude``.

    The exact inverse of :func:`exclude_update`'s block write: same
    whole-line markers, all other content preserved.

    Args:
        repo_dir: Main git repo root.

    """
    exclude = _exclude_file(repo_dir)
    if not exclude.exists():
        return
    lines = exclude.read_text(encoding='utf-8').splitlines()
    body = '\n'.join(_strip_exclude_blocks(lines)).rstrip('\n')
    # atomic like the inverse exclude_update: the info/exclude is shared
    # across worktrees, so a concurrent writer must never see a torn file
    fractal.util.filesystem.write_atomic(exclude, f'{body}\n' if body else '')


def exclude_block_lines(repo_dir: pathlib.Path) -> set[int]:
    """Return the 1-based ``info/exclude`` line numbers of fractal's blocks.

    Markers included, spanning the same whole-line-delimited blocks
    :func:`exclude_update` writes and :func:`exclude_strip` removes. A
    ``git check-ignore -v`` attribution keyed on these lines separates
    fractal's own holds from user lines sharing the file (its normal
    purpose), so a foreign pattern eating a deliverable is never mistaken
    for an intentional runtime ignore.

    Args:
        repo_dir: Main git repo root.

    Returns:
        Line numbers inside fractal's marker-delimited blocks.

    """
    exclude = _exclude_file(repo_dir)
    if not exclude.exists():
        return set()
    lines = exclude.read_text(encoding='utf-8').splitlines()
    block: set[int] = set()
    index = 0
    while index < len(lines):
        if lines[index].strip() == _EXCLUDE_BEGIN:
            close = index + 1
            while close < len(lines) and lines[close].strip() != _EXCLUDE_END:
                close += 1
            if close < len(lines):
                block.update(range(index + 1, close + 2))
                index = close + 1
                continue
        index += 1
    return block


def ensure_project_wiki(
    worktree: pathlib.Path,
    repo_dir: pathlib.Path,
    path: str,
    name: str,
) -> bool:
    """Create the project wiki if missing; report whether it was created.

    The wiki lives at ``<worktree>/wiki`` (repo root) or
    ``<worktree>/<project>/wiki`` (sub-project). ``name`` is the validated
    display name; the wiki is seeded with the strict ASCII-identifier
    naming policy. A failed ``wiki init`` is surfaced, not swallowed, and
    a pre-existing directory that is not a wiki is refused, never adopted.

    Args:
        worktree: The user node's worktree root.
        repo_dir: Main git repo root (compared against ``name`` to note a
            derived-name adjustment).
        path: Project path (``.`` for the repo root).
        name: Validated wiki display name.

    Returns:
        ``True`` if the wiki was created, ``False`` if it already existed.

    """
    # resolve the project wiki directory
    if path == '.':
        wiki_dir = worktree / 'wiki'
    else:
        wiki_dir = worktree / path / 'wiki'
    if (wiki_dir / '_index.md').exists():
        return False
    # refuse to adopt a pre-existing docs directory: `wiki init` rewrites
    # every page under its root (frontmatter, generated indexes), so a
    # user's own wiki/ fails loud here instead of being silently rewritten;
    # an empty dir or one carrying the .wiki marker is a partial prior
    # init, repaired in place
    foreign = wiki_dir.is_dir() and not (wiki_dir / '.wiki').exists()
    if foreign and any(wiki_dir.iterdir()):
        relative = wiki_dir.relative_to(worktree)
        raise RuntimeError(
            f'{relative}/ already exists and is not a project wiki —'
            ' adopting it would rewrite its files in place. Move the'
            ' directory aside and re-run init, or adopt it deliberately'
            f' first: wiki init --path={relative}'
        )
    # note when the derived name was adjusted from the repo directory name
    if name != repo_dir.name:
        logger.info(
            f'Note: using project wiki name {name!r}'
            f' (from repository directory {repo_dir.name!r}).'
        )
    # seed the strict naming policy so fractal project wikis use identifiers
    settings = json.dumps({'naming': {'validate': ['ascii', 'identifier']}})
    # resolve the wiki console script (the plasma-wiki dependency installs
    # it into fractal's own environment); name the remedy on a miss
    try:
        executable = fractal.util.system.console_script('wiki')
    except RuntimeError as e:
        raise RuntimeError(
            "No 'wiki' executable found — install fractal's plasma-wiki"
            ' dependency into its environment and re-run init.'
        ) from e
    cmd = [executable, 'init', name, f'--path={wiki_dir}', f'--settings={settings}']
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        error = result.stderr.strip()
        raise RuntimeError(
            f'wiki init failed (exit {result.returncode}): {error!r} —'
            ' fix the cause and re-run init (a partial init is repaired'
            ' in place)'
        )
    return True


def verify_hook_formatters(repo_dir: pathlib.Path) -> None:
    """Name the formatter-safe lanes when host pre-commit hooks exist.

    Hooks that break the structure of wiki pages (a plugin-less mdformat
    escapes wikilinks and mangles nav delimiters) fail their commits loud
    with the pages restored, while structure-preserving wiki reformats are
    auto-retried; ``.fractal/`` pages outside the memory wiki are guarded
    byte-for-byte -- any hook rewrite there is refused. Stays silent once
    either safe lane is taken: ``mdformat-wiki`` wired in, or a
    ``.fractal`` mention (the shape of any path filter steering hooks off
    the generated paths). Verify-only: the user's config is never edited,
    and no exclude is ever prescribed.

    Args:
        repo_dir: Main git repo root.

    """
    config_path = repo_dir / '.pre-commit-config.yaml'
    if not config_path.exists():
        return
    config = config_path.read_text(encoding='utf-8')
    if 'mdformat-wiki' in config or FRACTAL_FOLDER in config:
        return
    logger.warning(
        'Note: this repo runs pre-commit hooks — structure-preserving'
        ' reformats of wiki pages are auto-retried, structure-breaking ones'
        ' fail loud with the pages restored, and any hook rewrite of other'
        ' node-data pages (.fractal/ seeds) is refused outright. Give'
        ' mdformat the wikilink-aware plugin (additional_dependencies:'
        ' [mdformat-wiki] on its hook, dropping mdformat-frontmatter if'
        ' present — both register a frontmatter renderer and whichever is'
        ' discovered first wins), or keep formatters off the wiki paths.'
    )


def run_script(
    package_dir: pathlib.Path,
    script: str,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    """Run a bundled ``_scripts/`` script.

    Args:
        package_dir: Root of the installed ``fractal`` package.
        script: Script filename in ``_scripts/``.
        *args: Arguments to pass to the script.

    Returns:
        Completed process result.

    Raises:
        RuntimeError: If the script exits non-zero.

    """
    script_path = package_dir / '_scripts' / script
    # scripts shell back into `fractal`; resolve helper CLIs from the
    # invoking installation, not ambient PATH, so a fronted foreign
    # install (e.g. the root venv's) cannot answer in this one's place
    env = fractal.util.system.prepend_bin_path()
    result = subprocess.run(
        ['bash', f'{script_path}', *args],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        error = result.stderr.strip()
        raise RuntimeError(f'{script} failed (exit {result.returncode}): {error!r}')
    return result


def cleanup_failed_worktree(
    repo_dir: pathlib.Path,
    branch: str,
    *,
    created_branch: bool = True,
) -> None:
    """Roll back a worktree/branch left by a failed child init (best-effort).

    A child init that fails after ``git worktree add`` (in init.sh, or in the
    registration that follows) would otherwise strand a live worktree with no
    registry row. Remove the worktree and the ``.project`` cache entry so a
    retry starts clean. The branch is deleted only when *this* init created
    it (``created_branch``); init.sh reuses a pre-existing branch in place, and
    its committed history must survive a failed init.

    Args:
        repo_dir: Main git repo root.
        branch: The failed child's branch.
        created_branch: This init created the branch (else it was reused).

    """
    # remove worktree
    worktree_dir = fractal.util.git.find_worktree(repo_dir, branch)
    if worktree_dir and worktree_dir.is_dir():
        cmd = ['worktree', 'remove', '--force', f'{worktree_dir}']
        fractal.util.git.run(cmd, cwd=repo_dir, check=False)
    if created_branch:
        # this init created the branch -- prune branch + .project entry
        # (shared with phantom-node teardown)
        prune_branch(repo_dir, branch)
    else:
        # a reused pre-existing branch carries committed history that must
        # survive -- drop only the .project cache entry this init added
        project_file = repo_dir / WORKTREES_FOLDER / PROJECT_FOLDER / branch
        project_file.unlink(missing_ok=True)


def prune_branch(repo_dir: pathlib.Path, branch: str) -> None:
    """Delete a worktree-less node's git branch and project-cache entry.

    Used for a phantom node (registry row present, worktree already gone) that
    ``delete.sh`` cannot tear down. Best-effort: a missing branch is not an error.

    Args:
        repo_dir: Main repo root.
        branch: Branch to prune.

    """
    fractal.util.git.run(['branch', '-D', branch], cwd=repo_dir, check=False)
    project_file = repo_dir / WORKTREES_FOLDER / PROJECT_FOLDER / branch
    project_file.unlink(missing_ok=True)


# ------ helper functions


def _exclude_file(repo_dir: pathlib.Path) -> pathlib.Path:
    """Resolve the common-dir ``info/exclude`` (shared across all worktrees)."""
    common_dir = fractal.util.git.run(['rev-parse', '--git-common-dir'], cwd=repo_dir)
    return (repo_dir / common_dir).resolve() / 'info' / 'exclude'


def _strip_exclude_blocks(lines: list[str]) -> list[str]:
    """Drop every fractal block from ``info/exclude`` lines.

    Markers match only as whole lines; an unmatched begin marker is left in
    place rather than swallowing the tail.
    """
    kept = []
    index = 0
    while index < len(lines):
        if lines[index].strip() == _EXCLUDE_BEGIN:
            close = index + 1
            while close < len(lines) and lines[close].strip() != _EXCLUDE_END:
                close += 1
            if close < len(lines):
                index = close + 1
                continue
        kept.append(lines[index])
        index += 1
    return kept
