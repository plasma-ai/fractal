"""Functions for the work-product commit pipeline."""

from __future__ import annotations

import dataclasses
import io
import os
import pathlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import typing
from collections.abc import Callable, Iterable
from typing import Optional

import fractal.util
from fractal.constants import FRACTAL_FOLDER, SETTINGS_FILE, WIKI_FOLDER
from fractal.exceptions import DirtyWorktreeError

from . import worktree
from .worktree import exclude_block_lines

if typing.TYPE_CHECKING:
    from .node import Node

__all__ = []

# pathspec excludes appended to every stage -- fractal's
# runtime artifacts must never ride a work commit
_STAGE_EXCLUDES = (
    # virtualenvs -- the dir entry and, for per-file listings, its contents
    ':!**/.venv',
    ':!**/.venv/**',
    # the central DB and its sidecars, under either filename
    ':!**/.db',
    ':!**/.db-*',
    ':!**/registry.db',
    ':!**/registry.db-*',
    # the status and pause markers
    ':!**/.status',
    ':!**/.paused',
    # the headless runtime marker and captured output
    ':!**/.headless',
    ':!**/headless.log',
    # the config write lock
    ':!**/config.json.lock',
    # write_atomic's crash-stranded temps
    ':!**/.*-*.tmp',
    # engine-materialized system skills
    ':!**/skills/.system/*',
    # the wiki tool's self-ignored derived cache -- the dir entry and, for
    # per-file listings, its contents
    ':!**/.wiki/cache',
    ':!**/.wiki/cache/**',
    # the wiki tool's per-machine Obsidian vault config -- the dir entry and,
    # for per-file listings, its contents
    ':!**/.obsidian',
    ':!**/.obsidian/**',
)


# advisory threshold for the staged-file size guard
_LARGE_FILE_WARN_BYTES = 10 * 1024 * 1024

# line cap for the staged diffstat a force commit folds into its body
_DIFFSTAT_CAP_LINES = 20

# listing cap for the paths the ignore-skip warning names
_SKIP_LIST_CAP = 10


def commit(
    node: Node,
    message: str,
    *,
    init: bool = False,
    check: bool = False,
    ignore_scope: bool = False,
    force: bool = False,
    body: Optional[str] = None,
    iteration: Optional[int] = None,
    run_id: Optional[int] = None,
    iter_id: Optional[int] = None,
    step_id: Optional[int] = None,
) -> str:
    """Commit a node's current iteration work.

    The pipeline half of :meth:`Node.commit` (which owns the flag
    validation): scope check, wiki index refresh, lint, stage, commit,
    and push unless ``local`` is set.

    Args:
        node: The node whose worktree to commit.
        message: Short description appended to the commit message.
        init: Use the ``init`` label instead of ``iteration <run>.<iter>``.
        check: Error if uncommitted changes exist instead of committing.
        ignore_scope: Commit out-of-scope changes but still lint.
        force: Bypass scope and lint checks and git hooks.
        body: Optional paragraph below the subject; a ``force`` commit's
            body also folds in the record pass's notices (the memory index
            rebuild's among them), the staged-sweep warnings, and a capped
            diffstat, so a backstop save describes itself.
        iteration: Commit-message iteration number (the loop passes the
            live one); resolved from the node's open iteration row, else
            ``0``, when omitted.
        run_id: Run the commit event belongs to (also run-qualifies the
            subject's iteration label).
        iter_id: Iteration the commit event belongs to.
        step_id: Step the commit event belongs to.

    Returns:
        Commit output and notices.

    Raises:
        DirtyWorktreeError: On a dirty ``--check``.
        RuntimeError: On an out-of-scope change, a failed wiki index
            refresh, a lint failure, a failed commit, or a ``message``
            carrying the subject's own labels (the branch name or
            ``iteration``).

    """
    worktree = node.worktree
    # check mode: run before scope/lint so a clean
    # tree never trips the safety net
    if check:
        _check_clean(node)
        return ''
    # the message is the subject's parenthetical -- the pipeline composes
    # '<branch>: iteration <run>.<iter> (<message>)' itself, so a message
    # repeating those labels would double-label history; backstop (force)
    # and --init saves must never block on the shape
    if not force and not init:
        lowered = message.lower()
        prefixed = lowered.startswith(f'{node.branch.lower()}:')
        if prefixed or re.match(r'iteration\b', lowered):
            raise RuntimeError(
                f'Commit message {message!r} starts with the branch name or'
                f" an 'iteration' label -- the tool wraps the message as"
                f" '{node.branch}: iteration <run>.<iter> (<message>)'."
                f' Re-commit with a bare lowercase summary.'
            )
    # a caller that knows the iteration passes it (the loop's commits, with
    # the run); an agent/operator commit stamps the open iteration's number
    # and run, else 0 with no run (--init's label carries no number, so it
    # skips the read)
    label_run = run_id
    if iteration is None and not init:
        rows = node.record.iters(status='active', limit=1)
        iteration = rows[0]['iter'] if rows else 0
        label_run = rows[0]['run_id'] if rows else None
    # resolve the commit boundaries (one law with the merge footprint check)
    bounds = scope_boundaries(node)
    commit_scopes = list(bounds.roots)
    node_prefix = bounds.node_prefix
    wiki_prefix = bounds.wiki_prefix
    fractal_prefix = bounds.fractal_prefix
    # the baseline skips the refresh and lint below, so it carries the node's
    # own seed alone -- project content takes an ordinary, linted commit
    if init:
        _baseline_check(node, bounds)
    # check working tree, index, and untracked files for out-of-scope changes
    scoped = not force and not ignore_scope and bool(commit_scopes)
    if scoped:
        _scope_check(node, bounds)
    output: list[str] = []
    # refresh the wiki indexes, then lint (--force and --init skip both:
    # neither may block the backstop save or the baseline, the setup
    # save a node starts from, which the baseline check keeps to the
    # node's own seed -- project content takes an ordinary, linted
    # commit); the seeded hook shells back into `fractal`, so resolve
    # helper CLIs from the invoking installation, not ambient PATH -- a
    # fronted foreign install must not answer the hook's config reads
    if not force and not init:
        env = fractal.util.system.prepend_bin_path()
        # mechanical index refresh: regenerated bytes ride this commit (both
        # wikis sit inside the always-committable prefixes); a failed update
        # fails the commit -- a broken wiki must never land
        wiki_cli = shutil.which('wiki', path=env['PATH'])
        if wiki_cli is not None:
            for wiki_dir in (worktree / wiki_prefix, node.node_dir / 'memory'):
                if not (wiki_dir / '_index.md').is_file():
                    continue
                result = subprocess.run(
                    [wiki_cli, 'update', f'--path={wiki_dir}'],
                    capture_output=True,
                    text=True,
                    cwd=worktree,
                    env=env,
                )
                if result.returncode != 0:
                    # join both streams -- the CLI reports refusals to stderr
                    # but its file summaries to stdout
                    error = '\n'.join(
                        stream.strip()
                        for stream in (result.stdout, result.stderr)
                        if stream.strip()
                    )
                    raise RuntimeError(
                        f'wiki update --path={wiki_dir} failed'
                        f' (exit {result.returncode}): {error!r}'
                    )
        lint = node.node_dir / 'scripts' / 'lint.sh'
        result = subprocess.run(
            ['bash', f'{lint}'],
            capture_output=True,
            text=True,
            cwd=worktree,
            env=env,
        )
        if result.returncode != 0:
            # join both streams -- lint.sh is user-editable and common
            # linters report their findings to stdout
            error = '\n'.join(
                stream.strip()
                for stream in (result.stdout, result.stderr)
                if stream.strip()
            )
            raise RuntimeError(f'lint.sh failed (exit {result.returncode}): {error!r}')
        # surface lint notices (e.g. wiki lint warnings) instead of dropping them
        for stream in (result.stdout, result.stderr):
            if stream.strip():
                output.append(stream.strip())

    # stage relevant paths; closure so the post-hook retry can re-use it
    # the record pass's notices ride the commit output -- a force-add past a
    # host ignore rule, and any non-record file it refused, must be said out
    # loud rather than inferred from the diff; the hook retry re-stages through
    # this same closure, so the latest pass's notices are the ones reported
    record_notices: list[str] = []
    unstaged: list[str] = []

    def _stage_changes() -> None:
        record_notices[:], unstaged[:] = _stage(
            node,
            scoped=scoped,
            commit_scopes=commit_scopes,
            node_prefix=node_prefix,
            wiki_prefix=wiki_prefix,
            fractal_prefix=fractal_prefix,
        )
        # the memory wiki indexes every file on disk, so restage its indexes
        # without the files the commit leaves out (backstops included: the
        # node's own refresh writes those rows too); the --init baseline's
        # memory is the fresh seed
        if not init:
            record_notices.extend(_rebuild_memory_index(node, unstaged))

    _stage_changes()
    output.extend(record_notices)
    # count and name the workspace files the staging expansion dropped solely
    # because ignore rules match them -- a tracked host ignore pattern can
    # silently eat node artifacts; fractal's own runtime ignores stay silent (the
    # info/exclude block and the stage's pathspec excludes are intentional), and
    # so do self-ignoring dirs (a dir whose ignore rule lives inside it, like a
    # wiki .cache/, manages itself -- its collapsed dir line and the content
    # lines it also emits are one managed entity, not eaten files)
    cmd = [
        'ls-files',
        '--others',
        '-i',
        '--exclude-standard',
        '--directory',
        '-z',
        '--',
        *_STAGE_EXCLUDES,
    ]
    # raw bytes, decoded here -- run()'s strip would eat a first
    # path's leading whitespace
    raw = fractal.util.git.run_bytes(cmd, cwd=worktree) or b''
    ignored = os.fsdecode(raw)
    skipped: list[str] = []
    if ignored:
        # raw git call bypassing util.git.run -- the helper feeds no stdin,
        # and check-ignore's batch form reads its paths from --stdin; -z on
        # both sides so a non-ASCII path round-trips unquoted
        result = subprocess.run(
            ['git', '-C', f'{worktree}', 'check-ignore', '-v', '--stdin', '-z'],
            input=ignored,
            capture_output=True,
            text=True,
        )
        managed: set[str] = set()
        # fractal's own info/exclude holds are the managed block's lines
        # only -- the file is a user surface too (its normal purpose), so a
        # foreign line there eats paths like any tracked ignore rule
        block_lines = exclude_block_lines(node.repo_dir)
        # -v -z emits <source> <linenum> <pattern> <pathname> quadruples,
        # the source already bare (no :line:pattern suffix to strip)
        fields = result.stdout.split('\0')
        quads = zip(fields[::4], fields[1::4], fields[2::4], fields[3::4])
        for source, line, pattern, path in quads:
            in_exclude = source == '.git/info/exclude' or source.endswith(
                '/.git/info/exclude'
            )
            if in_exclude and int(line) in block_lines:
                continue
            path = path.rstrip('/')
            if source.startswith(f'{path}/'):
                managed.add(path)
                continue
            if any(path.startswith(f'{prefix}/') for prefix in managed):
                continue
            # an estate file the commit left unstaged is the estate warnings'
            # to name, so this warning never counts it a second time
            if path in unstaged:
                continue
            # nor a collapsed directory entry holding one (--directory lists a
            # wholly ignored new directory, not the files the pass refused)
            if any(item.startswith(f'{path}/') for item in unstaged):
                continue
            # name the rule sources outside the repo without their machine
            # paths -- a force commit folds this warning into its body, where
            # machine paths do not belong; the shared exclude reads
            # repo-relative, and any other absolute source is the host's
            # global ignore file
            if in_exclude:
                source = '.git/info/exclude'
            elif os.path.isabs(source):
                source = 'core.excludesFile'
            skipped.append(f'{path} ({source}:{line}:{pattern})')
    skip_warning = None
    if skipped:
        listing = ', '.join(skipped[:_SKIP_LIST_CAP])
        if len(skipped) > _SKIP_LIST_CAP:
            listing += f', ... and {len(skipped) - _SKIP_LIST_CAP} more'
        skip_warning = (
            f'Warning: {len(skipped)} path(s) skipped by ignore rules (repository'
            f' or host rules, not node-record refusals): {listing}'
        )
        output.append(skip_warning)
    # an oversized staged file is usually an artifact staged by accident, but a
    # large commit is also legitimate -- so the size guard warns and never blocks
    cmd = ['diff', '--cached', '--name-only', '--diff-filter=d', '-z']
    raw = fractal.util.git.run_bytes(cmd, cwd=worktree) or b''
    staged = os.fsdecode(raw)
    large = []
    for staged_path in filter(None, staged.split('\0')):
        cmd = ['cat-file', '-s', f':0:{staged_path}']
        size = int(fractal.util.git.run(cmd, cwd=worktree, check=False) or 0)
        if size >= _LARGE_FILE_WARN_BYTES:
            large.append(f'{staged_path} ({size // 1024 // 1024}MB)')
    large_warning = None
    if large:
        threshold = _LARGE_FILE_WARN_BYTES // 1024 // 1024
        listing = '\n'.join(large)
        large_warning = f'Warning: staged file(s) exceed {threshold}MB:\n{listing}'
        output.append(large_warning)
    # a new root path named like an entry of the node's own folder may be a
    # node file written from the worktree root by mistake, or project work --
    # so the guard warns and never blocks, and a failure inside it (a locked
    # record read, a vanished node folder) skips it rather than failing the
    # commit, a backstop save included
    shadow_warning = None
    if not init:
        try:
            shadows = _root_shadows(node)
        except Exception:
            shadows = []
        if shadows:
            listing = '\n'.join(shadows)
            node_folder = node.node_dir.relative_to(worktree).as_posix()
            shadow_warning = (
                'Warning: staged root path(s) share a name with an entry of the'
                " node's own folder; if these are node files, move them under"
                f' {node_folder}/; if they are project files, ignore this'
                f' warning:\n{listing}'
            )
            output.append(shadow_warning)
    # compose the commit subject: run-qualified when the commit has run
    # lineage; a row-less commit keeps the plain zero label
    if init:
        label = 'init'
    elif label_run is not None:
        label = f'iteration {label_run}.{iteration}'
    else:
        label = f'iteration {iteration}'
    subject = f'{node.branch}: {label} ({message})'
    # compose the body: the caller's paragraph, and on force the record
    # pass's notices and staged warnings above plus a capped diffstat -- a
    # backstop sweep must describe what it saved (and what it deliberately
    # overrode or refused) from git history alone
    paragraphs = [body] if body else []
    if force:
        paragraphs.extend(record_notices)
        paragraphs.extend(
            warning
            for warning in (skip_warning, large_warning, shadow_warning)
            if warning
        )
        cmd = ['diff', '--cached', '--stat']
        stat = fractal.util.git.run(cmd, cwd=worktree, check=False) or ''
        lines = stat.splitlines()
        if len(lines) > _DIFFSTAT_CAP_LINES:
            # keep the trailing summary line when capping
            lines = lines[: _DIFFSTAT_CAP_LINES - 1] + lines[-1:]
        if lines:
            paragraphs.append('\n'.join(lines))
    body_text = '\n\n'.join(paragraphs)

    # commit, then log the commit event keyed on the new sha (best-effort;
    # never for --init, whose baseline has no run lineage) -- single emit
    # point, so the pre-commit-hook retry never double-logs
    def _commit_once() -> str:
        # force bypasses hooks too: the loop's backstops must be able to
        # save work past a failing hook, and mutating hooks must never
        # rewrite the save
        cmd = ['git', '-C', f'{worktree}', 'commit']
        if force:
            cmd.append('--no-verify')
        cmd.extend(['-m', subject])
        if body_text:
            cmd.extend(['-m', body_text])
        # raw git call joining both streams on failure -- hooks (and the
        # pre-commit framework) report their findings to stdout, which a
        # stderr-only error would drop from the retry narration
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            error = '\n'.join(
                stream.strip()
                for stream in (result.stdout, result.stderr)
                if stream.strip()
            )
            raise RuntimeError(
                f'git commit failed (exit {result.returncode}): {error!r}'
            )
        committed = result.stdout.strip()
        if not init:
            sha = fractal.util.git.run(['rev-parse', 'HEAD'], cwd=worktree)
            try:
                event_id = node.record.event_start(
                    'commit',
                    metadata=sha,
                    run_id=run_id,
                    iter_id=iter_id,
                    step_id=step_id,
                )
            except Exception:
                event_id = None
            if event_id is None:
                # the insert failed and was swallowed (telemetry must
                # never block the save path) -- warn so the lost record
                # leaves a trace
                output.append(f'Warning: commit event not recorded for {sha}')
            else:
                # a failed close is tolerated silently -- the start row
                # already anchors the sha
                try:
                    node.record.event_end(event_id=event_id, status='completed')
                except Exception:
                    pass
        return committed

    # tolerate "nothing staged" no-op; real failures must surface -- else it
    # would report success and push while HEAD never advanced (`--quiet`
    # exits zero exactly when nothing is staged)
    cmd = ['diff', '--cached', '--quiet']
    if fractal.util.git.run(cmd, cwd=worktree, check=False) is not None:
        output.append('Nothing staged to commit')
    else:
        try:
            committed = _commit_once()
        except RuntimeError as error:
            # pre-commit hook may have reformatted and aborted; re-stage and
            # retry once, but only if re-staging actually changes the index
            if not (worktree / '.pre-commit-config.yaml').is_file():
                # carry the underlying failure text -- the agent's remediation
                # loop reads the message, not the cause chain
                raise RuntimeError(
                    f'Commit failed (no pre-commit config to recover from): {error}'
                ) from error
            committed = _hook_retry(
                node=node,
                stage=_stage_changes,
                commit=_commit_once,
                error=error,
                generated=(f'{wiki_prefix}/', f'{fractal_prefix}/'),
                retry=True,
            )
        if committed:
            output.append(committed)
    # push unless the node is local
    if not node.config.get('local', False):
        notice = _push(node)
        if notice:
            output.append(notice)
    # repeat the held-out counts as the final line -- a hook's or gate's long
    # output can bury the warnings above, and a no-op commit reads as clean
    held: list[str] = []
    if unstaged:
        held.append(f'{len(unstaged)} estate file(s) not staged')
    if skipped:
        held.append(f'{len(skipped)} path(s) ignored')
    if held:
        counts = '; '.join(held)
        output.append(f'Held out: {counts}')
    return '\n'.join(output)


def commit_user_init(node: Node, message: str) -> str:
    """Commit a user node's baseline: the project wiki (and node data when tracked).

    The ``--init`` baseline is the only commit a user node takes. By default
    the node's own ``.fractal/`` data is self-ignored on the top-level
    branch, so this stages only the project wiki (under ``<project>/`` for a
    sub-project), the settings file naming its folder when the project has
    one, and the ``.gitattributes`` merge attribute ``wiki init`` wrote; on
    a tree opted in via ``fractal track`` the node's seed dir rides along
    too, staged under the work commits' estate law, so the runtime files
    fractal's own exclude block holds stay out. Everything is committed
    with a pathspec, so the user's other staged work is never swept in,
    and does not push. A node worktree branched later then starts from a
    committed tree.

    Args:
        node: The user node to baseline.
        message: Short description appended to the commit message.

    Returns:
        The estate pass's notices and a confirmation message.

    Raises:
        ValueError: If the project's settings file is invalid or names
            another project-wiki folder than the tree recorded at init.

    """
    # resolve the project prefix (sub-project nodes nest under <project>/)
    project = node.config.get('project', '.')
    if project == '.':
        seed = FRACTAL_FOLDER
    else:
        seed = f'{project}/{FRACTAL_FOLDER}'
    wiki = node.wiki_prefix
    # the baseline is what every spawn reads the setting from, so it never
    # commits a settings file naming another folder than the one init fixed
    # (an absent setting names the default)
    setting = worktree.read_wiki_setting(node.worktree / project)
    recorded = node.config.get('wiki')
    if (setting or WIKI_FOLDER) != (recorded or WIKI_FOLDER):
        raise ValueError(
            f'{seed}/{SETTINGS_FILE} names {setting or WIKI_FOLDER!r} as the'
            f' project wiki, but the tree on {node.branch!r} recorded'
            f' {recorded or WIKI_FOLDER!r}; re-run `fractal init` to adopt it'
            f' (after `fractal reset {node.branch}` if the tree has nodes).'
        )
    # stage fractal's node data only when tracked (it is self-ignored on the
    # top-level branch by default); the shared project wiki always rides along
    # so the base ref has a committed wiki, and with it the settings file
    # naming its folder
    paths = []
    seed_dir = f'{seed}/{node.branch}'
    estates = []
    if worktree.seed_tracked(node.node_dir):
        paths.append(seed_dir)
        estates.append(node.node_dir)
    if (node.worktree / wiki).is_dir():
        paths.append(wiki)
    if (node.worktree / seed / SETTINGS_FILE).is_file():
        paths.append(f'{seed}/{SETTINGS_FILE}')
    # sweep the **/_index.md merge attribute wiki init writes into the
    # working tree (committing it is left to the caller): stage .gitattributes
    # only while the attribute is init's own uncommitted edit -- the wiki tool
    # writes the file only when it is clean, so a pending user edit means the
    # attribute is absent and the file stays out of the baseline
    if _attributes_is_init_edit(node):
        paths.append('.gitattributes')
    # nothing to commit (default mode, no wiki yet): leave the index untouched
    # -- an empty pathspec would otherwise sweep the user's other staged work
    if not paths:
        return f'User node baseline already committed on {node.branch}.'
    # literal pathspec magic: a glob char in the project prefix must not
    # widen or empty the match; the stage excludes ride every pathspec --
    # a tracked seed dir stages everything not otherwise ignored, so the
    # runtime artifacts inside it must be barred here too
    specs = [f':(literal){path}' for path in paths]
    specs += _STAGE_EXCLUDES
    # refresh the shared exclude block before staging: its basename patterns
    # are the first layer keeping runtime files out of a tracked seed, and a
    # fresh clone starts with no info/exclude at all
    worktree.exclude_update(node.repo_dir)
    # the estate pass's notices ride the confirmation, as they ride a work
    # commit's output; the hook retry re-stages through the closure below,
    # so the latest pass's notices are the ones reported
    notices: list[str] = []

    # stage the pathspec; closure so the hook-rewrite recovery can re-use it
    def _stage_paths() -> None:
        # the seed answers to the work commits' estate law instead of -f: a
        # plain add withholding what the law refuses, then the record pass's
        # force-add of the records only a machine-local layer holds -- so
        # fractal's own exclude block (tmp/, *.err, __pycache__/) keeps its
        # hold on the runtime files beside the records
        denied = _estate_denied(node, seed, estates)
        if estates:
            withheld = [f':(exclude,literal){node.worktree / path}' for path in denied]
            # add from inside the seed, so every pathspec shares its prefix
            # (git 2.54 and 2.55 can stage nothing for a nested directory
            # pathspec beside an exclude outside it), and first, so a failure
            # leaves the index untouched; only git's refusal of an ignored
            # seed (exit 1, the hold the record pass overrides) is tolerated --
            # any other failure fails the baseline rather than land it without
            # the seed; LC_ALL pins the refusal's wording
            cmd = ['git', '-C', f'{node.node_dir}', 'add', '--', '.']
            cmd += [*_STAGE_EXCLUDES, *withheld]
            env = {**os.environ, 'LC_ALL': 'C'}
            result = subprocess.run(cmd, capture_output=True, text=True, env=env)
            refusal = 'The following paths are ignored by one of your .gitignore files'
            ignored = result.returncode == 1 and refusal in result.stderr
            if result.returncode != 0 and not ignored:
                error = result.stderr.strip()
                raise RuntimeError(
                    f'git add of {seed_dir} failed (exit {result.returncode}):'
                    f' {error!r}'
                )
        # -f mirrors the work commits' record pass: the wiki's paths are
        # explicit and exclude-filtered, so an external ignore rule cannot
        # silently eat the wiki
        forced = [f':(literal){path}' for path in paths if path != seed_dir]
        if forced:
            cmd = ['add', '-f', '--', *forced, *_STAGE_EXCLUDES]
            fractal.util.git.run(cmd, cwd=node.worktree)
        notices[:], _ = _stage_records(node, seed, estates, denied)

    _stage_paths()
    # benign no-op when nothing in scope changed (already committed)
    cmd = ['diff', '--cached', '--name-only', '--', *specs]
    if not fractal.util.git.run(cmd, cwd=node.worktree):
        confirmation = f'User node baseline already committed on {node.branch}.'
        return '\n'.join([*notices, confirmation])
    # commit only fractal's artifacts (pathspec) so other staged work stays
    # staged; no push -- the user owns pushing their own base branch
    msg = f'{node.branch}: init ({message})'

    def _commit_paths() -> str:
        cmd = ['commit', '-m', msg, '--', *specs]
        return fractal.util.git.run(cmd, cwd=node.worktree)

    try:
        _commit_paths()
    except RuntimeError as error:
        # a formatting hook may have rewritten the staged pages in the
        # working tree: every rewritten path is gated -- a wiki-page rewrite
        # that preserves structure is re-staged and retried once, and any
        # other rewrite restores the authored bytes and fails
        _hook_retry(
            node=node,
            stage=_stage_paths,
            commit=_commit_paths,
            error=error,
            generated=(f'{wiki}/', f'{seed}/'),
            retry=False,
        )
    confirmation = f'Committed user node baseline on {node.branch}.'
    return '\n'.join([*notices, confirmation])


@dataclasses.dataclass(frozen=True)
class Scope:
    """A node's commit boundaries.

    One law for every surface that judges a node's paths: ``fractal
    commit`` checks the worktree against it, and ``merge.sh`` checks the
    staged squash through ``fractal node _scope``. ``roots`` are the
    project-prefixed scope roots, each its own boundary (empty means
    unbounded); ``node_prefix`` is the node data dir prefix, always
    committable, or ``''`` when the project itself is the boundary;
    ``wiki_prefix`` is the shared project wiki, committable regardless of
    scope; ``fractal_prefix`` is the estates dir the stage sweep adds.
    """

    roots: tuple[str, ...]
    node_prefix: str
    wiki_prefix: str
    fractal_prefix: str


def scope_boundaries(node: Node) -> Scope:
    """Resolve a node's commit boundaries from its project and scope.

    Each scope root is its own boundary, nested under the project prefix
    for a sub-project node; a sub-project node with no roots is bounded to
    its project dir, and a repo-root node with no roots is unbounded.

    Args:
        node: The node whose boundaries to resolve.

    Returns:
        The boundaries.

    """
    project = node.project_path
    configured = node.config.get('scope') or []
    # a '.' root names the project itself, and subsumes any sibling root --
    # collapse the whole scope so the boundary becomes the project dir (or
    # unbounded at the repo root); kept out of the joins below: a literal
    # './' (or '<project>/./') prefix matches no git path, so leaving it in
    # place would put every changed file out of scope and refuse every commit;
    # merge.sh re-derives these roots as SCOPE_ROOTS for its .fractal/ restore
    # and conflict auto-resolve -- keep the collapse rule aligned
    if any(not pathlib.PurePosixPath(root).parts for root in configured):
        configured = []
    if configured:
        if project == '.':
            roots = tuple(configured)
            node_prefix = f'{FRACTAL_FOLDER}/'
        else:
            roots = tuple(f'{project}/{root}' for root in configured)
            node_prefix = f'{project}/{FRACTAL_FOLDER}/'
    elif project != '.':
        roots = (project,)
        node_prefix = ''
    else:
        roots = ()
        node_prefix = ''
    wiki_prefix = node.wiki_prefix
    if project == '.':
        fractal_prefix = FRACTAL_FOLDER
    else:
        fractal_prefix = f'{project}/{FRACTAL_FOLDER}'
    return Scope(roots, node_prefix, wiki_prefix, fractal_prefix)


def out_of_scope(
    paths: Iterable[str],
    bounds: Scope,
    *,
    attributes_ok: bool,
) -> list[str]:
    """Return the paths outside a node's commit boundaries, sorted.

    Literal prefix checks (not regex), so a scope/path with a regex
    metachar (v1.2, app+web, a[b]) cannot widen the anchor and let a
    sibling dir slip through as "in scope".

    Args:
        paths: Repo-relative paths.
        bounds: The node's commit boundaries; empty roots mean unbounded,
            so nothing is out of scope.
        attributes_ok: Admit the worktree-root ``.gitattributes``: the
            commit pipeline passes whether it is init's own uncommitted
            edit, and the merge footprint check passes whether the target's
            staged copy is that edit (a child's first squash carries it).

    Returns:
        The out-of-scope paths.

    """
    if not bounds.roots:
        return []
    offending = []
    for path in sorted(set(paths)):
        # in scope: under any commit scope dir
        if any(path.startswith(f'{root}/') for root in bounds.roots):
            continue
        # the node data dir is always committable
        if bounds.node_prefix and path.startswith(bounds.node_prefix):
            continue
        # the shared project wiki is committable regardless of scope
        if path.startswith(f'{bounds.wiki_prefix}/'):
            continue
        # init's own worktree-root .gitattributes edit is committable
        if path == '.gitattributes' and attributes_ok:
            continue
        offending.append(path)
    return offending


# ------ helper functions


def _check_clean(node: Node) -> None:
    """Error if the node's worktree holds uncommitted changes.

    Args:
        node: The node whose worktree to check.

    Raises:
        DirtyWorktreeError: When any tracked or untracked change the stage
            could commit remains.

    """
    # porcelain, not "diff HEAD": diff lists only tracked changes, so a step
    # that left only untracked files reads as clean -- the force-commit safety
    # net skips it and a later --continue (git clean -fd) then discards the work
    # the stage's own excludes ride along: dirt the stage may never commit
    # (runtime artifacts in a worktree whose info/exclude has not refreshed)
    # would otherwise read as permanently dirty and fire the net every
    # iteration for nothing
    # the estate content law rides along for the same reason: a credential a
    # node parked in its estate is dirt no pass may ever stage, so counting it
    # would fire the net every iteration over a file that can never clear
    # -uall so the law is asked the same question the stage answers, per file:
    # the default listing collapses a wholly-untracked directory to one entry,
    # and a parked .ssh/ would read as dirt no expansion could ever clear
    project = node.project_path
    prefix = FRACTAL_FOLDER if project == '.' else f'{project}/{FRACTAL_FOLDER}'
    cmd = ['status', '--porcelain', '-uall', '-z', '--', *_STAGE_EXCLUDES]
    raw = fractal.util.git.run_bytes(cmd, cwd=node.worktree) or b''
    for entry in filter(None, os.fsdecode(raw).split('\0')):
        # -z drops the rename arrow, so a rename's source rides as its own
        # bare field -- unprefixed, hence never read as an untracked path
        if entry.startswith('?? ') and _is_refused_estate(entry[3:], prefix):
            continue
        raise DirtyWorktreeError(
            'Uncommitted changes remain (agent should have committed).'
        )


def _changed_paths(node: Node) -> set[str]:
    """Return every changed path in a node's worktree.

    Args:
        node: The node whose worktree to read.

    Returns:
        The repo-relative paths changed in the working tree or the index
        against ``HEAD``, and the untracked paths the ignore rules admit.

    """
    worktree = node.worktree
    # -z everywhere so a non-ASCII path is never C-quoted (a quoted path
    # starts with '"' and fails every prefix check); raw bytes so a first
    # path's leading whitespace survives (run() strips)
    changed: set[str] = set()
    # --no-renames: rename detection lists only a rename's destination,
    # hiding the source's deletion from the scope and baseline checks
    for cmd in (
        ['diff', '--name-only', '--no-renames', '-z', 'HEAD'],
        ['diff', '--cached', '--name-only', '--no-renames', '-z', 'HEAD'],
        ['ls-files', '--others', '--exclude-standard', '-z'],
    ):
        raw = fractal.util.git.run_bytes(cmd, cwd=worktree) or b''
        listing = os.fsdecode(raw)
        changed.update(filter(None, listing.split('\0')))
    return changed


def _root_shadows(node: Node) -> list[str]:
    """Return each staged root addition that shadows a node folder entry.

    A staged addition shadows when its first path segment names an entry of
    the node's own folder (``plans``, ``memory``, ``tmp``, ...) and the
    project had no top-level path of that name at the node's base, the fork
    sha its record anchors. A node with no resolvable base names nothing,
    and neither does a merge or squash merge in progress, whose additions
    are not the node's own writes.

    Args:
        node: The node whose staged additions to check.

    Returns:
        ``<path> (shadows <node folder entry>)`` per shadowing addition, the
        entry repo-relative.

    """
    worktree = node.worktree
    markers = {marker.name for _, marker in fractal.util.git.in_progress(worktree)}
    if markers & {'MERGE_HEAD', 'SQUASH_MSG'}:
        return []
    # -z and raw bytes, as in _changed_paths; --no-renames so a path moved to
    # the root lists as the addition it is
    cmd = ['diff', '--cached', '--name-only', '--no-renames', '--diff-filter=A', '-z']
    raw = fractal.util.git.run_bytes(cmd, cwd=worktree) or b''
    added = list(filter(None, os.fsdecode(raw).split('\0')))
    if not added:
        return []
    base = node.files._diff_anchor('base')
    if base is None:
        return []
    # the top-level names the project had when the node started
    cmd = ['ls-tree', '--name-only', '-z', base]
    raw = fractal.util.git.run_bytes(cmd, cwd=worktree)
    if raw is None:
        return []
    existing = set(os.fsdecode(raw).split('\0'))
    entries = {entry.name for entry in node.node_dir.iterdir()}
    node_folder = node.node_dir.relative_to(worktree).as_posix()
    result = []
    for path in added:
        name = path.split('/', 1)[0]
        if name in entries and name not in existing:
            result.append(f'{path} (shadows {node_folder}/{name})')
    return result


def _baseline_check(node: Node, bounds: Scope) -> None:
    """Refuse a ``--init`` baseline carrying more than the node's own seed.

    The baseline skips the wiki refresh and the lint, so it carries only
    fractal's setup: the node's data dir and init's worktree-root
    ``.gitattributes`` edit. Project content -- material copied into the
    node's tree included -- takes an ordinary commit, which lints it.

    Args:
        node: The node whose worktree to check.
        bounds: The node's commit boundaries.

    Raises:
        RuntimeError: Naming the node's data dir and the offending paths.

    """
    seed = f'{bounds.fractal_prefix}/{node.branch}/'
    attributes_ok = _attributes_is_init_edit(node)
    offending = []
    for path in sorted(_changed_paths(node)):
        # the node's own seed is what the baseline carries
        if path.startswith(seed):
            continue
        # init's own worktree-root .gitattributes edit rides along
        if path == '.gitattributes' and attributes_ok:
            continue
        offending.append(path)
    if offending:
        listing = '\n'.join(offending)
        raise RuntimeError(
            f'The --init baseline commits only the node seed ({seed}) and skips'
            f' the lint; commit these with an ordinary fractal commit:\n{listing}'
        )


def _scope_check(node: Node, bounds: Scope) -> None:
    """Refuse changes outside the node's commit scopes.

    Args:
        node: The node whose worktree to check.
        bounds: The node's commit boundaries.

    Raises:
        RuntimeError: Naming every scope root and the offending paths.

    """
    changed = _changed_paths(node)
    attributes_ok = _attributes_is_init_edit(node)
    offending = out_of_scope(changed, bounds, attributes_ok=attributes_ok)
    if offending:
        roots = ' '.join(f'{root}/' for root in bounds.roots)
        listing = '\n'.join(offending)
        raise RuntimeError(f'Changes outside node scope ({roots}):\n{listing}')


#: the merge-driver lines a worktree-root .gitattributes may gain as init's
#: edit: the index lines the wiki tool writes there, and no page lines -- the
#: tool maps pages in each wiki root's own file, and a page mapping at the
#: root would take over the merge of every Markdown file in the repository;
#: merge.sh's footprint check mirrors them for the target's staged copy
_ATTRIBUTES_INIT_LINES = ('# Wiki index merge driver', '**/_index.md merge=wiki')
#: the page merge-driver block the wiki tool writes to each wiki root's own
#: .gitattributes
_WIKI_ROOT_ATTRIBUTES_LINES = ('# Wiki page merge driver', '*.md merge=wiki')


def _attributes_is_init_edit(node: Node) -> bool:
    """Return whether ``.gitattributes`` is init's uncommitted ``merge=wiki`` edit.

    Init writes the memory wiki's index ``merge=wiki`` attribute at the
    worktree root when the base lacks it -- an artifact outside every scope
    root, committable exactly while it is init's own uncommitted edit:
    HEAD's content followed by the wiki tool's index lines. The match is
    line-exact, in either line ending, and covers the whole change, so a
    page line, an attribute line assigning some other merge driver, or any
    other line riding beside init's makes the file an ordinary out-of-scope
    path.

    Args:
        node: The node whose worktree to check.

    Returns:
        Whether the file is HEAD's copy plus init's lines.

    """
    attributes = node.worktree / '.gitattributes'
    if not attributes.is_file():
        return False
    text = attributes.read_text(encoding='utf-8')
    # raw bytes: a stripped read would drop a leading blank line or trailing
    # whitespace from HEAD's copy alone and fail the prefix comparison
    cmd = ['show', 'HEAD:.gitattributes']
    committed = fractal.util.git.run_bytes(cmd, cwd=node.worktree) or b''
    committed_lines = committed.decode('utf-8').splitlines()
    lines = text.splitlines()
    if lines[: len(committed_lines)] != committed_lines:
        return False
    added = [line for line in lines[len(committed_lines) :] if line]
    return added == list(_ATTRIBUTES_INIT_LINES)


#: estate subdirectories whose contents are node records canon requires
#: committed (the memory wiki, plans, and the seeded step/script/skill set)
_RECORD_DIRS = ('memory', 'plans', 'scripts', 'skills', 'steps')
#: estate root files that are records in their own right (the charter,
#: the template provenance, and the config)
_RECORD_FILES = ('NODE.md', '_template.toml', 'config.json')
#: the suffixes a record carries -- text and structured text only, so the
#: force pass can never stage a key, a certificate, an archive, or a binary
_RECORD_SUFFIXES = (
    '.csv',
    '.diff',
    '.json',
    '.md',
    '.patch',
    '.py',
    '.sh',
    '.toml',
    '.tsv',
    '.txt',
    '.yaml',
    '.yml',
)
#: dot-named directories a record dir may carry: the wiki tool's own
#: settings directory, whose declared-root marker a fresh clone needs to
#: read the memory wiki back (the tool's derived state self-ignores)
_TOOL_STATE_DIRS = ('.wiki',)
#: dot-named files a record dir may carry: git's empty-directory
#: placeholder, whose only purpose is to check a bare record dir out
_TOOL_STATE_FILES = ('.gitkeep',)
#: estate-relative dot-named files admitted at one place alone: the page
#: merge attributes the wiki tool writes at the memory wiki's root
_TOOL_STATE_PATHS = ('memory/.gitattributes',)


def _is_committable(relpath: str, estate: str) -> bool:
    """Return whether an estate-relative path is content a node may commit.

    One law governs both estate staging paths, because both decide what
    a node's own directory folds into git history: anything a node
    parked in its estate -- a dotenv, a key, a downloaded credential --
    would otherwise ride a commit silently whenever no ignore rule
    happens to fence it, and be refused only where a host rule already
    protected it. So the allowlist describes everything an estate may
    legitimately hold: the canon-required record surfaces at the text
    suffixes a record is written in, plus the estate's own tool state,
    which is committed content rather than machine state.

    Args:
        relpath: The file's worktree-relative path.
        estate: The estate directory's worktree-relative path.

    Returns:
        Whether the path is inside the estate content allowlist.

    """
    inside = pathlib.PurePosixPath(relpath).relative_to(estate)
    parts = inside.parts
    if len(parts) == 1:
        return parts[0] in _RECORD_FILES and inside.suffix in _RECORD_SUFFIXES
    if parts[0] not in _RECORD_DIRS:
        return False
    # a dot-named directory is machine or agent state (an agent config dir,
    # a parked .ssh/) unless it is the estate's own tool state
    dot_dirs = [part for part in parts[1:-1] if part.startswith('.')]
    if any(part not in _TOOL_STATE_DIRS for part in dot_dirs):
        return False
    # the leaf is either named tool state or a record at a text suffix, so
    # no pass can stage a key, a certificate, an archive, or a binary
    if parts[-1].startswith('.'):
        return parts[-1] in _TOOL_STATE_FILES or inside.as_posix() in _TOOL_STATE_PATHS
    return inside.suffix in _RECORD_SUFFIXES


def _is_refused_estate(relpath: str, fractal_prefix: str) -> bool:
    """Return whether a worktree path is estate content no staging pass may commit.

    Args:
        relpath: The file's worktree-relative path.
        fractal_prefix: The fractal data folder prefix.

    Returns:
        Whether the path sits inside an estate and the content law
        refuses it.

    """
    path = pathlib.PurePosixPath(relpath)
    if not path.is_relative_to(fractal_prefix):
        return False
    parts = path.relative_to(fractal_prefix).parts
    # <prefix>/<estate>/<path>: a file directly under the folder sits in no
    # estate, so no estate's law governs it
    if len(parts) < 2:
        return False
    return not _is_committable(relpath, f'{fractal_prefix}/{parts[0]}')


def _stage(
    node: Node,
    *,
    scoped: bool,
    commit_scopes: list[str],
    node_prefix: str,
    wiki_prefix: str,
    fractal_prefix: str,
) -> tuple[list[str], list[str]]:
    """Stage the node's committable paths.

    Args:
        node: The node whose worktree to stage.
        scoped: Stage only the commit boundaries (the default when scopes
            exist); otherwise the whole worktree.
        commit_scopes: Scope roots (project-prefixed).
        node_prefix: The node data dir prefix, or ``''``.
        wiki_prefix: The shared project wiki prefix.
        fractal_prefix: The fractal data folder prefix (node estates live
            under it; the record pass owns them).

    Returns:
        The estate pass's notices (what it force-staged, what it refused),
        and every estate file left unstaged: those the content law withheld
        from the adds, and the ignored ones the record pass refused or
        could not stage.

    """
    worktree = node.worktree
    # settle the estate content law before any sweep, and withhold what it
    # refuses by name: what a node's own directory folds into history must
    # not turn on whether a host rule happens to fence it; naming refused
    # paths (never a whole estate) keeps the sweeps whole -- an exclude
    # matching an ignored path fails the add outright, and an ignored path
    # is already outside the sweep anyway
    estates = _estate_roots(node, fractal_prefix)
    denied = _estate_denied(node, fractal_prefix, estates)
    withheld = [f':(exclude,literal){path}' for path in denied]
    # heal a baseline that force-tracked the wiki tool's self-ignored derived
    # cache: drop it from the index -- never the disk, the tool owns the bytes
    # -- so this commit converges the tree and the cache's own ignore keeps it
    # untracked from here on; unguarded, since upkeep must never block a save
    cache = worktree / wiki_prefix / '.wiki' / 'cache'
    cmd = ['rm', '-r', '--cached', '--quiet', '--ignore-unmatch']
    cmd += ['--', f':(literal){cache}']
    fractal.util.git.run(cmd, cwd=worktree, check=False)
    if scoped:
        # stage only paths that exist -- a scope dir that is planned
        # but not yet created would otherwise make `git add` fatal
        # (exit 128) and abort every commit until the dir appears;
        # the node data prefix is absent here by design: _stage_records
        # owns the estates in its own commands (a broad external ignore
        # rule covering .fractal would otherwise fail this whole add
        # with 'paths are ignored by one of your .gitignore files')
        paths = []
        for scope in commit_scopes:
            if (worktree / scope).exists():
                paths.append(f'{worktree / scope}')
        # the shared project wiki is committable regardless of scope
        if (worktree / wiki_prefix).is_dir():
            paths.append(f'{worktree / wiki_prefix}')
        # init's own .gitattributes edit rides the sweep
        if _attributes_is_init_edit(node):
            attributes = worktree / '.gitattributes'
            paths.append(f'{attributes}')
        if paths:
            # literal pathspec magic: a glob char in an on-disk path (e.g. a
            # bracketed project dir) must not widen or empty the match
            specs = [f':(literal){path}' for path in paths]
            cmd = ['add', *specs, *_STAGE_EXCLUDES, *withheld]
            fractal.util.git.run(cmd, cwd=worktree)
        # the estates ride their own plain add: it must be able to fail
        # (everything under it ignored) without unstaging the scope sweep
        folder = worktree / fractal_prefix
        if folder.is_dir():
            cmd = ['add', f':(literal){folder}', *_STAGE_EXCLUDES, *withheld]
            fractal.util.git.run(cmd, cwd=worktree, check=False)
    else:
        cmd = ['add', f':(literal){worktree}', *_STAGE_EXCLUDES, *withheld]
        fractal.util.git.run(cmd, cwd=worktree)
    notices, unstaged = _stage_records(node, fractal_prefix, estates, denied)
    return notices, denied + unstaged


def _estate_roots(node: Node, fractal_prefix: str) -> list[pathlib.Path]:
    """Return the estate directories the content law governs.

    A self-ignored seed dir (the user node's untracked-by-design state)
    is not an estate the law speaks for, so it is left alone.

    Args:
        node: The node whose worktree holds the estates.
        fractal_prefix: The fractal data folder prefix.

    Returns:
        Each estate directory, sorted.

    """
    folder = node.worktree / fractal_prefix
    if not folder.is_dir():
        return []
    return [
        entry
        for entry in sorted(folder.iterdir())
        if entry.is_dir() and worktree.seed_tracked(entry)
    ]


def _estate_denied(
    node: Node,
    fractal_prefix: str,
    estates: list[pathlib.Path],
) -> list[str]:
    """Return the estate content the law refuses to let a plain add stage.

    The plain adds would otherwise take anything a node parked in its
    estate -- a dotenv, a key, a downloaded credential -- the moment no
    ignore rule happened to fence it, which is the default state of a
    fresh clone. Only new content is judged: what an estate already
    tracks stays committable, so the law gates what enters history and
    never the upkeep of the history a node already owns.

    Args:
        node: The node whose worktree to judge.
        fractal_prefix: The fractal data folder prefix.
        estates: The estate directories to judge.

    Returns:
        Worktree-relative paths the sweeps must withhold.

    """
    if not estates:
        return []
    specs = [f':(literal){entry}' for entry in estates]
    cmd = ['ls-files', '--others', '--exclude-standard', '-z', '--', *specs]
    raw = fractal.util.git.run_bytes(cmd, cwd=node.worktree) or b''
    fresh = [path for path in os.fsdecode(raw).split('\0') if path]
    return [path for path in fresh if _is_refused_estate(path, fractal_prefix)]


def _stage_records(
    node: Node,
    fractal_prefix: str,
    estates: list[pathlib.Path],
    denied: list[str],
) -> tuple[list[str], list[str]]:
    """Force-stage estate records an external ignore rule held out.

    fractal owns its staging: the plain adds honor every ignore layer, so
    one stray exclude line (worktrees share ``.git/info/exclude``) or a
    host ``.gitignore`` pattern can silently unstage -- or hard-fail --
    the audit trail canon requires nodes to commit. This pass attributes
    every estate file an ignore rule held out (``check-ignore -v``) and
    force-adds exactly the externally-ignored ones by explicit path:
    fractal's own exclude block (the runtime artifacts) and ignore files
    living inside an estate (a self-managed cache) keep their hold, and a
    self-ignored seed dir (the user node's untracked-by-design state) is
    left alone.

    The override answers to the same content allowlist
    (:func:`_is_committable`) that bounds the plain adds, so one law
    decides what an estate folds into history on either path: a file
    outside it is refused and named rather than staged, and every
    force-add is reported. A record the add cannot stage -- vanished
    after the snapshot, unreadable on disk -- is likewise reported by
    name, never fatal.

    Args:
        node: The node whose worktree to stage.
        fractal_prefix: The fractal data folder prefix.
        estates: The estate directories the pass owns.
        denied: Estate content the law already withheld from the plain
            adds, reported here so both refusals read together.

    Returns:
        Notices naming what was force-staged and what was refused, and the
        held files left unstaged: those refused, and the records the add
        could not stage.

    """
    forced: list[str] = []
    failed: list[str] = []
    refused: list[str] = []
    held: list[str] = []
    if estates:
        specs = [f':(literal){entry}' for entry in estates]
        # collect the estate files ignore rules held out of the plain adds
        cmd = ['ls-files', '--others', '-i', '--exclude-standard', '-z', '--', *specs]
        raw = fractal.util.git.run_bytes(cmd, cwd=node.worktree) or b''
        held = [path for path in os.fsdecode(raw).split('\0') if path]
    if held:
        # the paths fractal-normal rules hold: the shipped template (runtime
        # artifacts, evaluated directly -- rule attribution cannot answer this,
        # a broad foreign line shadows the block while barring the very same
        # artifact) plus the repo's committed per-directory .gitignore files
        # (repo content a node can see and fix; an estate cache's self-ignore
        # rides here); the machine-local layers -- info/exclude beyond the
        # template's rules, core.excludesFile -- are deliberately absent:
        # their holds are exactly what the force pass overrides
        assets = pathlib.Path(__file__).parent.parent / '_assets'
        template = assets / 'git' / 'exclude'
        cmd = [
            'ls-files',
            '--others',
            '-i',
            f'--exclude-from={template}',
            '--exclude-per-directory=.gitignore',
            '-z',
            '--',
            *specs,
        ]
        raw = fractal.util.git.run_bytes(cmd, cwd=node.worktree) or b''
        normal = set(filter(None, os.fsdecode(raw).split('\0')))
        # a snapshot path can vanish before the add (ignored-and-untracked
        # files are exactly what `git clean -X` deletes, and estates churn
        # under the node's own housekeeping) -- stage what still exists rather
        # than letting one dead pathspec fail the add and abort the commit
        for path in held:
            if path in normal or not os.path.lexists(node.worktree / path):
                continue
            if _is_refused_estate(path, fractal_prefix):
                refused.append(path)
                continue
            forced.append(path)
        if forced:
            # batched add, retried per path on failure: git stages nothing on
            # exit 128 (a record can vanish after the lexists probe or sit
            # unreadable on disk), and one bad path must cost itself, never the
            # healthy records queued beside it -- the backstop save this pass
            # feeds exists precisely to rescue those
            specs = [f':(literal){node.worktree / path}' for path in forced]
            cmd = ['add', '-f', *specs]
            if fractal.util.git.run(cmd, cwd=node.worktree, check=False) is None:
                for path in forced:
                    cmd = ['add', '-f', f':(literal){node.worktree / path}']
                    result = fractal.util.git.run(cmd, cwd=node.worktree, check=False)
                    if result is None:
                        failed.append(path)
                forced = [path for path in forced if path not in failed]
    notices = []
    if forced:
        # worktree-relative paths in every notice: a force commit folds
        # them into its body, where machine-local prefixes do not belong
        listing = ', '.join(sorted(forced))
        notices.append(
            f'Force-staged {len(forced)} node record(s) held out by an'
            f' ignore rule: {listing}'
        )
    if failed:
        listing = ', '.join(sorted(failed))
        notices.append(
            f'Warning: {len(failed)} node record(s) could not be staged'
            f' (vanished or unreadable): {listing}'
        )
    if refused:
        listing = ', '.join(sorted(refused))
        notices.append(
            f'Warning: {len(refused)} ignored estate file(s) are not node'
            f' records and were NOT force-staged: {listing}'
        )
    if denied:
        listing = ', '.join(sorted(denied))
        notices.append(
            f'Warning: {len(denied)} estate file(s) are not node records and'
            f' were NOT staged: {listing}'
        )
    return notices, refused + failed


def _rebuild_memory_index(node: Node, unstaged: list[str]) -> list[str]:
    """Restage the memory wiki's indexes without the files the commit leaves out.

    The memory wiki indexes every file on disk -- through the commit's own
    refresh, or the node's own ``wiki update`` before a backstop save -- so
    a file the estate content law refuses gains a row linking a file the
    commit never carries. When the staging pass left any memory file
    unstaged, the staged indexes are rebuilt outside the repository
    (:func:`_restage_memory_indexes`). The rebuild is upkeep and must never
    cost the save, so a failure warns in one line and leaves the staged
    indexes as they were.

    Args:
        node: The node whose staged memory to rebuild.
        unstaged: Every estate file the staging pass left unstaged.

    Returns:
        The rebuild's notice or a failure's warning; nothing when no memory
        file was left out or no staged index changed.

    """
    memory = node.node_dir / 'memory'
    prefix = memory.relative_to(node.worktree).as_posix()
    left_out = [path for path in unstaged if path.startswith(f'{prefix}/')]
    if not left_out or not (memory / '_index.md').is_file():
        return []
    # resolve the wiki CLI as the refresh does, and skip where it would
    env = fractal.util.system.prepend_bin_path()
    wiki_cli = shutil.which('wiki', path=env['PATH'])
    if wiki_cli is None:
        return []
    try:
        changed = _restage_memory_indexes(
            node,
            prefix,
            wiki_cli=wiki_cli,
            env=env,
        )
    except Exception as error:
        reason = ' '.join(f'{error}'.split())
        return [
            f'Warning: memory index not rebuilt ({reason}); it may link'
            ' file(s) the commit leaves out'
        ]
    if not changed:
        return []
    return [
        f'Memory index rebuilt without {len(left_out)} file(s) the commit leaves out'
    ]


def _restage_memory_indexes(
    node: Node,
    prefix: str,
    *,
    wiki_cli: str,
    env: dict[str, str],
) -> bool:
    """Rebuild the staged memory indexes in a temp tree and restage the changes.

    The staged memory tree -- the bytes about to commit -- is checked out
    into a temp folder outside every repository (inside one, the wiki would
    apply that repository's ignore rules) and ``wiki update`` rebuilds its
    indexes there, so no row links a file the commit leaves out. The temp
    tree leaves out what the wiki's own ignore check skips, so every other
    row is the refresh's. An
    index whose rebuild differs from HEAD's copy only in its ``updated:``
    stamp keeps HEAD's bytes, so a refused file never moves the stamp. Each
    index whose bytes differ from what is staged is written back to the git
    index and then the working tree, keeping the tree clean for the loop's
    dirty check: a failed staging leaves both as they were, and a failed
    tree write restages what the tree holds for every index it did not
    reach, so a failure never leaves the two apart. Page writes are
    discarded with the temp tree.

    Args:
        node: The node whose staged memory to rebuild.
        prefix: The memory wiki's worktree-relative path.
        wiki_cli: Resolved ``wiki`` executable.
        env: Environment for the wiki invocation.

    Returns:
        Whether any staged index changed.

    Raises:
        RuntimeError: When a git or wiki call fails.

    """
    worktree = node.worktree
    # the staged memory tree: the exact bytes about to commit
    cmd = ['ls-files', '-z', '--', f':(literal){prefix}']
    raw = fractal.util.git.run_bytes(cmd, cwd=worktree) or b''
    staged = [path for path in os.fsdecode(raw).split('\0') if path]
    # leave out what the wiki's own ignore check skips, so the rebuild
    # indexes exactly what the refresh did: this is the wiki package's
    # Wiki._check_ignore call -- matched on the patterns alone (--no-index,
    # so a tracked file reads as ignored too), the personal core.excludesFile
    # pinned out -- with Wiki._gitignore_fence's exemption, which turns the
    # check off when the root and its index both read as ignored; the wiki
    # walks no dot path, so its settings always ride along
    walked = [
        path
        for path in staged
        if not any(
            part.startswith('.')
            for part in pathlib.PurePosixPath(path).relative_to(prefix).parts
        )
    ]
    # probe each path as ./<path>: git reads a --stdin path opening with ':'
    # (under a ':'-named project folder) as pathspec magic, not as the path
    root_index = f'{prefix}/_index.md'
    payload = b''.join(
        os.fsencode(f'./{path}') + b'\0' for path in [prefix, root_index, *walked]
    )
    cmd = [
        'git',
        '-C',
        f'{worktree}',
        '-c',
        f'core.excludesFile={os.devnull}',
        'check-ignore',
        '-z',
        '--stdin',
        '--no-index',
    ]
    # raw git call bypassing util.git.run -- the helper feeds no stdin; -z
    # both ways so a non-ASCII path round-trips unquoted
    result = subprocess.run(cmd, input=payload, capture_output=True)
    # exit 1 is a clean probe that matched nothing
    if result.returncode not in (0, 1):
        raise RuntimeError(f'git check-ignore failed (exit {result.returncode})')
    # git echoes each path as it was given, so drop the ./ again
    fenced = {
        os.fsdecode(path).removeprefix('./')
        for path in result.stdout.split(b'\0')
        if path
    }
    if {prefix, root_index} <= fenced:
        fenced = set()
    kept = [path for path in staged if path not in fenced]
    rebuilt: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory() as tmp:
        # check the kept bytes out of the git index into the temp tree (raw,
        # like the probe, for its --stdin)
        payload = b''.join(os.fsencode(path) + b'\0' for path in kept)
        cmd = [
            'git',
            '-C',
            f'{worktree}',
            'checkout-index',
            '-z',
            '--stdin',
            f'--prefix={tmp}/',
        ]
        result = subprocess.run(cmd, input=payload, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f'git checkout-index failed (exit {result.returncode})')
        indexes = [path for path in kept if path.endswith('/_index.md')]
        before = {path: (pathlib.Path(tmp) / path).read_bytes() for path in indexes}
        root = pathlib.Path(tmp) / prefix
        result = subprocess.run(
            [wiki_cli, 'update', f'--path={root}'],
            capture_output=True,
            text=True,
            cwd=tmp,
            env=env,
        )
        if result.returncode != 0:
            # join both streams -- the CLI reports refusals to stderr but its
            # file summaries to stdout
            error = '\n'.join(
                stream.strip()
                for stream in (result.stdout, result.stderr)
                if stream.strip()
            )
            raise RuntimeError(
                f'wiki update failed (exit {result.returncode}): {error!r}'
            )
        # collect the rebuilt indexes outside the wiki tool's dot dirs
        for index in root.rglob('_index.md'):
            relative = index.relative_to(root)
            if any(part.startswith('.') for part in relative.parts):
                continue
            rebuilt[f'{prefix}/{relative.as_posix()}'] = index.read_bytes()
    # collect each index whose bytes differ from what is staged -- HEAD's
    # bytes where the rebuild matches them but for the stamp
    changed: dict[str, bytes] = {}
    for path, data in sorted(rebuilt.items()):
        committed = _tree_bytes(node, 'HEAD', path)
        if committed is not None and _same_but_stamp(data, committed):
            data = committed
        if data != before.get(path):
            changed[path] = data
    if not changed:
        return False
    # stage the changes before the tree sees them: hash each into the object
    # store through its path's filters, as an add does, then repoint the git
    # index in one update, at each entry's staged mode (raw git calls
    # bypassing util.git.run -- the helper feeds no stdin); the entries carry
    # no stat data until the commit refreshes them, before any hook runs
    specs = [f':(literal){worktree / path}' for path in changed]
    cmd = ['ls-files', '-s', '-z', '--', *specs]
    raw = fractal.util.git.run_bytes(cmd, cwd=worktree) or b''
    modes: dict[str, str] = {}
    for entry in filter(None, os.fsdecode(raw).split('\0')):
        info, path = entry.split('\t', 1)
        modes[path] = info.split(' ', 1)[0]
    payload = b''
    for path, data in changed.items():
        cmd = [
            'git',
            '-C',
            f'{worktree}',
            'hash-object',
            '-w',
            f'--path={path}',
            '--stdin',
        ]
        result = subprocess.run(cmd, input=data, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f'git hash-object failed (exit {result.returncode})')
        blob = result.stdout.decode().strip()
        mode = modes.get(path, '100644')
        payload += os.fsencode(f'{mode} {blob}\t{path}') + b'\0'
    cmd = ['git', '-C', f'{worktree}', 'update-index', '-z', '--index-info']
    result = subprocess.run(cmd, input=payload, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f'git update-index failed (exit {result.returncode})')
    # then write the tree; a failed write restages what the tree holds for
    # each index it did not reach, so the tree and the git index agree
    paths = list(changed)
    for done, path in enumerate(paths):
        try:
            fractal.util.filesystem.write_atomic(worktree / path, changed[path])
        except Exception:
            specs = [f':(literal){worktree / rest}' for rest in paths[done:]]
            fractal.util.git.run(['add', '-f', '--all', '--', *specs], cwd=worktree)
            raise
    return True


#: an index frontmatter's ``updated:`` stamp line
_STAMP_LINE = re.compile(rb'^updated:.*$', re.MULTILINE)


def _same_but_stamp(rebuilt: bytes, committed: bytes) -> bool:
    """Return whether two index texts differ at most in their ``updated:`` stamp.

    Only the frontmatter block's stamp line is set aside -- the block runs
    from the opening fence to the first closing ``---`` line -- so a body
    line spelled like a stamp still counts as content.

    Args:
        rebuilt: The rebuilt index's bytes.
        committed: HEAD's bytes for the same index.

    Returns:
        Whether the texts match once each block's stamp line is set aside.

    """
    masked = []
    for text in (rebuilt, committed):
        block, fence, body = text.partition(b'\n---\n')
        if text.startswith(b'---\n') and fence:
            block = _STAMP_LINE.sub(b'updated:', block, count=1)
        masked.append(block + fence + body)
    return masked[0] == masked[1]


def _hook_retry(
    node: Node,
    stage: Callable[[], None],
    commit: Callable[[], str],
    error: RuntimeError,
    *,
    generated: tuple[str, str],
    retry: bool,
) -> str:
    """Recover from a git hook that mutated files during a failed commit.

    Hooks own formatting, so a rewrite is re-staged and the commit retried
    once -- for guarded pages (under the ``generated`` prefixes;
    everything, on the ``retry=False`` baseline) only after the rewrite
    proves harmless. Wiki pages (the project wiki and the node memory
    wiki) are judged on structure: a wiki-aware formatter legitimately
    rewraps prose, while a structure-blind one escapes wikilinks and
    mangles frontmatter and separators. Every other guarded page keeps
    byte-identity -- seed frontmatter is load-bearing machine input no
    lint can validate, so any rewrite there is damage. A violation
    restores the authored bytes (removal, for a page the hook added) and
    fails the commit with remediation guidance; work commits additionally
    lint the touched wiki roots, blaming the hook only when the root
    linted clean before the rewrite.

    Args:
        node: The node whose worktree is mid-commit.
        stage: Re-runs the caller's staging.
        commit: Re-runs the caller's commit.
        error: The original commit failure.
        generated: Path prefixes owned by the wiki tooling, ordered: the
            project-wiki prefix first, the node-data prefix second.
        retry: Gate only the ``generated`` prefixes (a work commit);
            ``False`` gates every rewritten path (the baseline).

    Returns:
        The retried commit's output.

    Raises:
        RuntimeError: When re-staging changes nothing, the hook broke a
            guarded page's structure, or the retried commit fails again.

    """
    worktree = node.worktree
    # capture the failed commit's index, re-stage, and diff the trees --
    # recovery is only possible if re-staging actually changed the index
    tree_before = fractal.util.git.run(['write-tree'], cwd=worktree)
    stage()
    tree_after = fractal.util.git.run(['write-tree'], cwd=worktree)
    if tree_after == tree_before:
        if retry:
            # carry the underlying failure text -- the agent's remediation
            # loop reads the message, not the cause chain
            raise RuntimeError(
                f'Commit failed and re-staging changed nothing to retry: {error}'
            ) from error
        raise error
    # -z so a non-ASCII generated page is matched (and restored) by its real
    # path, never a C-quoted one the checkout pathspec cannot resolve; raw
    # bytes so a first path's leading whitespace survives (run() strips)
    cmd = ['diff-tree', '--name-only', '-r', '-z', tree_before, tree_after]
    raw = fractal.util.git.run_bytes(cmd, cwd=worktree) or b''
    mutated = os.fsdecode(raw).split('\0')
    # gate the guarded rewrites in two tiers: markdown under a wiki root
    # (the project wiki and the node memory wiki) is judged on structure --
    # a wiki-aware formatter rewrapping prose is formatting and rides the
    # retry -- while every other guarded page (seed steps, node config, and
    # the machine files inside the roots) keeps byte-identity: load-bearing
    # machine input no lint can validate, so any rewrite there is damage
    wiki_prefix, fractal_prefix = generated
    memory_prefix = f'{fractal_prefix}{node.branch}/memory/'
    wiki_rooted = (wiki_prefix, memory_prefix)
    guarded = [
        path for path in mutated if path and (not retry or path.startswith(generated))
    ]
    structural = [
        path
        for path in guarded
        if path.startswith(wiki_rooted) and path.endswith('.md')
    ]
    broken = [
        path
        for path in structural
        if not _structure_preserved(node, tree_before, tree_after, path)
    ]
    byte_gated = [path for path in guarded if path not in structural]
    damaged = broken + byte_gated
    if damaged:
        # the pre-retry tree still holds the authored bytes, so restore them
        # -- leaving the rewrite in place would hand a bare retry corrupted
        # pages that commit cleanly; a page the hook ADDED has no authored
        # bytes, so its restore is removal (a crash here would leave it
        # staged for the next bare commit to land ungated)
        for path in damaged:
            if _tree_bytes(node, tree_before, path) is None:
                fractal.util.git.run(
                    ['rm', '-f', '--', f':(literal){path}'],
                    cwd=worktree,
                )
            else:
                fractal.util.git.run(
                    ['checkout', tree_before, '--', f':(literal){path}'],
                    cwd=worktree,
                )
        # name each tier's refusal for what it is -- the wiki remediation
        # is meaningless for a byte-guarded seed page and vice versa
        details = []
        if broken:
            listing = ', '.join(broken)
            details.append(
                f'wiki pages with broken structure: {listing}'
                ' (hook rewrites of wiki pages must preserve wikilinks,'
                ' frontmatter, and *** separators -- give mdformat the'
                ' wikilink-aware plugin (additional_dependencies:'
                ' [mdformat-wiki] on its hook, dropping mdformat-frontmatter'
                ' if present -- both register a frontmatter renderer and'
                ' whichever is discovered first wins), or keep formatters'
                ' off the wiki paths)'
            )
        if byte_gated:
            listing = ', '.join(byte_gated)
            details.append(
                f'byte-guarded pages: {listing}'
                ' (seed and machine pages must never be rewritten by hooks'
                ' -- keep formatters off the .fractal/ paths)'
            )
        detail = '; '.join(details)
        raise RuntimeError(
            'Commit failed after a git hook rewrote generated pages'
            f' (restored, not committed): {detail}.'
        ) from error
    # semantic backstop (work commits only -- a baseline wiki lints dirty by
    # design, so a baseline value mutation is an accepted, unlintable
    # residual): a rewrite can preserve structure yet still mutate
    # frontmatter values behind an intact fence, damage `wiki update` then
    # propagates into parent link rows and lint.sh only soft-warns -- so
    # lint every wiki root the hook touched, blaming the hook only for the
    # roots that linted clean before its rewrite (a pre-existing failure keeps
    # the pipeline's soft-warn tolerance instead of dead-ending every commit)
    notices: list[str] = []
    if retry:
        roots = set()
        for path in guarded:
            if path.startswith(wiki_prefix):
                roots.add(wiki_prefix.rstrip('/'))
            elif path.startswith(memory_prefix):
                roots.add(memory_prefix.rstrip('/'))
        if roots:
            env = fractal.util.system.prepend_bin_path()
            wiki_cli = shutil.which('wiki', path=env['PATH'])
            failing: dict[str, str] = {}
            for root in sorted(roots):
                if wiki_cli is None or not (worktree / root / '_index.md').is_file():
                    continue
                result = subprocess.run(
                    [wiki_cli, 'lint', f'--path={worktree / root}'],
                    capture_output=True,
                    text=True,
                    cwd=worktree,
                    env=env,
                )
                if result.returncode == 0:
                    continue
                detail = '\n'.join(
                    stream.strip()
                    for stream in (result.stdout, result.stderr)
                    if stream.strip()
                )
                if not _lints_clean_at(node, tree_before, root, wiki_cli, env):
                    notices.append(
                        f'Warning: wiki lint fails under {root} (pre-existing,'
                        f' not the hook rewrite): {detail}'
                    )
                    continue
                failing[root] = detail
            if failing:
                # accumulate before restoring so a failure in one root never
                # leaves another root's integrity-breaking rewrite staged
                prefixes = tuple(f'{root}/' for root in failing)
                restored = [p for p in guarded if p.startswith(prefixes)]
                for path in restored:
                    fractal.util.git.run(
                        ['checkout', tree_before, '--', f':(literal){path}'],
                        cwd=worktree,
                    )
                listing = ', '.join(restored)
                findings = '; '.join(
                    f'{root}: {detail}' for root, detail in sorted(failing.items())
                )
                raise RuntimeError(
                    'Commit failed: a git hook rewrite broke wiki integrity'
                    f' (restored, not committed): {listing}.'
                    f' wiki lint reports: {findings}'
                ) from error
    try:
        committed = commit()
    except RuntimeError as e:
        raise RuntimeError(
            f'Commit still failed after re-staging hook changes: {e}'
        ) from e
    # ride the soft-warn notices on the commit output so they land on the
    # pipeline's reporting channel, not a stderr side stream
    if notices:
        return '\n'.join([*notices, committed])
    return committed


def _structure_preserved(
    node: Node,
    tree_before: str,
    tree_after: str,
    path: str,
) -> bool:
    r"""Return whether a hook's rewrite of a guarded page preserved wiki structure.

    Compares the authored bytes against the rewrite on the invariants a
    structure-blind formatter breaks -- the wikilink opener count
    (normalized: a wiki-aware formatter's healthy ``[\[`` escape of bare
    non-wikilink brackets never reads as a lost opener), newly escaped
    wikilink openers (``\[[``), the count of ``***`` separator lines, and
    the leading frontmatter fence -- so a wiki-aware rewrap passes while an
    escaping or mangling rewrite fails. A page the hook added or deleted
    (present on only one side) is never a formatting rewrite, so it fails
    the gate.

    Args:
        node: The node whose worktree is mid-commit.
        tree_before: Tree holding the authored bytes.
        tree_after: Tree holding the hook's rewrite.
        path: The rewritten path, relative to the worktree.

    Returns:
        ``True`` if the rewrite preserved every invariant.

    """
    before = _tree_bytes(node, tree_before, path)
    after = _tree_bytes(node, tree_after, path)
    if before is None or after is None:
        return False
    # fold the healthy bare-bracket escape back before counting openers, so
    # only a real loss or gain of wikilink openers fails
    folded_before = before.replace(b'[\\[', b'[[')
    folded_after = after.replace(b'[\\[', b'[[')
    if folded_before.count(b'[[') != folded_after.count(b'[['):
        return False
    # a structure-blind formatter escapes real wikilinks; new escaped
    # openers are damage even when the raw opener count survives -- checked
    # on the folded bytes so a double-escape collapses into the same shape
    if folded_after.count(b'\\[[') > folded_before.count(b'\\[['):
        return False
    seps_before = sum(1 for line in before.splitlines() if line.strip() == b'***')
    seps_after = sum(1 for line in after.splitlines() if line.strip() == b'***')
    if seps_before != seps_after:
        return False
    return before.startswith(b'---\n') == after.startswith(b'---\n')


def _tree_bytes(node: Node, tree: str, path: str) -> Optional[bytes]:
    """Return ``path``'s blob bytes in ``tree``, or ``None`` when absent.

    Args:
        node: The node whose worktree holds the trees.
        tree: Tree to read from.
        path: Path relative to the worktree.

    Returns:
        The blob's bytes, or ``None`` when the tree has no such path.

    """
    return fractal.util.git.run_bytes(
        ['cat-file', 'blob', f'{tree}:{path}'],
        cwd=node.worktree,
    )


def _lints_clean_at(
    node: Node,
    tree: str,
    root: str,
    wiki_cli: str,
    env: dict[str, str],
) -> bool:
    """Return whether the wiki root under ``root`` linted clean at ``tree``.

    Materializes the tree's root into a temp dir (``git archive``) and lints
    it there, so a pre-existing failure -- one the pipeline soft-warns on
    every ordinary commit -- is never blamed on a hook rewrite. An
    unreadable state reads as dirty: the caller then treats the failure as
    pre-existing and proceeds, matching the pipeline's lint tolerance.

    Args:
        node: The node whose worktree holds the tree.
        tree: Tree to materialize the root from.
        root: Wiki root path, relative to the worktree.
        wiki_cli: Resolved ``wiki`` executable.
        env: Environment for the lint invocation.

    Returns:
        ``True`` if the root existed at ``tree`` and linted clean.

    """
    with tempfile.TemporaryDirectory() as tmp:
        archive = subprocess.run(
            ['git', 'archive', tree, '--', f':(literal){root}'],
            capture_output=True,
            cwd=node.worktree,
        )
        if archive.returncode != 0:
            return False
        try:
            with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
                bundle.extractall(tmp, filter='data')
        except (tarfile.TarError, OSError, ValueError):
            return False
        result = subprocess.run(
            [wiki_cli, 'lint', f'--path={pathlib.Path(tmp) / root}'],
            capture_output=True,
            text=True,
            env=env,
        )
        return result.returncode == 0


def _push(node: Node) -> Optional[str]:
    """Push the node's branch to ``origin``.

    Args:
        node: The node whose branch to push.

    Returns:
        A notice when the push is skipped, else ``None``.

    """
    # tolerate a missing remote but surface real push failures
    cmd = ['remote', 'get-url', 'origin']
    if fractal.util.git.run(cmd, cwd=node.worktree, check=False) is None:
        return "No 'origin' remote configured; skipping push"
    fractal.util.git.run(['push', 'origin', node.branch], cwd=node.worktree)
    return None
