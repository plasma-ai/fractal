"""Implements ``fractal`` commands."""

from __future__ import annotations

import importlib.resources
import pathlib
import shutil
from importlib.resources.abc import Traversable
from typing import Optional

import typer

import fractal.core.worktree
from fractal.cli.utils import (
    command,
    resolve_init_target,
    resolve_node,
    resolve_target,
    resolve_user_node,
)
from fractal.constants import FRACTAL_FOLDER
from fractal.core.node import Node
from fractal.exceptions import DirtyWorktreeError

__all__ = [
    'version',
    'install',
    'init',
    'track',
    'untrack',
    'commit',
    'open',
    'pause',
    'resume',
    'reset',
    'destroy',
]


def version(app: typer.Typer) -> typer.Typer:
    """Register the ``--version`` flag on the root callback."""

    def _version_callback(value: bool) -> None:
        """Print the running ``fractal`` package's version and exit."""
        if value:
            typer.echo(fractal.__version__)
            raise typer.Exit()

    # version flag
    version_help = 'Show the version and exit.'
    version = typer.Option(
        None,
        '--version',
        callback=_version_callback,
        is_eager=True,
        help=version_help,
    )

    @app.callback()
    def _main(version: Optional[bool] = version) -> None:
        """Fractal command-line interface."""

    return app


def install(app: typer.Typer) -> typer.Typer:
    """Register the ``install`` command."""
    # project flag
    project_help = 'Install skills in cwd rather than home directory.'
    project = typer.Option(False, '--project', help=project_help)
    # link flag
    link_help = (
        'Symlink the bundled skills instead of copying (requires the package'
        ' files on disk, e.g. an editable install), so source edits apply'
        ' without re-installing.'
    )
    link = typer.Option(False, '--link', help=link_help)

    @command(app, 'install')
    def _install(
        project: bool = project,
        link: bool = link,
    ) -> None:
        """Install the fractal and wiki skills for Claude Code and Codex.

        Copies the bundled skills into the Claude (.claude/skills) and Codex
        (.agents/skills) skill directories. Targets your home directory by
        default, or the current project with --project. The wiki skill ships
        with fractal's plasma-wiki dependency and is installed alongside it.
        --link symlinks the skills instead of copying -- the editable-install
        dev setup, where source edits apply without re-installing.
        """
        # resolve install directory
        if project:
            root = pathlib.Path.cwd()
        else:
            root = pathlib.Path.home()
        # resolve agent skill directories
        targets = [
            root / '.claude' / 'skills',
            root / '.agents' / 'skills',
        ]
        # collect skills
        skills = _bundled_skills()
        # a symlink needs a real directory to point at; only an on-disk
        # package (an editable install, not a zipped one) provides it
        if link and not all(isinstance(skill, pathlib.Path) for skill in skills):
            raise RuntimeError(
                '--link requires the bundled skills to be real directories'
                ' (an editable install); a zipped install cannot install'
                ' the skills from the CLI.'
            )
        # copy or link each skill into every target (replaces any prior install)
        for skill in skills:
            for target in targets:
                dest = target / skill.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.is_symlink() or dest.is_file():
                    dest.unlink()
                elif dest.is_dir():
                    shutil.rmtree(dest)
                if link:
                    dest.symlink_to(skill)
                    typer.echo(f'Linked {skill.name} -> {dest}.')
                else:
                    shutil.copytree(skill, dest)
                    typer.echo(f'Installed {skill.name} -> {dest}.')

    return app


def init(app: typer.Typer) -> typer.Typer:
    """Register the ``init`` command."""
    # path argument
    path_help = 'Repository path (or sub-project folder).'
    path = typer.Argument('.', help=path_help)
    # agent option
    agent_help = (
        'Default agent command for spawned nodes'
        ' (e.g. claude, codex, grok, opencode, or omp).'
    )
    agent = typer.Option(None, '--agent', help=agent_help)
    # provider option
    provider_help = (
        'Default provider route for spawned nodes (e.g. openrouter;'
        ' default: the vendor-native endpoint).'
    )
    provider = typer.Option(None, '--provider', help=provider_help)

    @command(app, 'init')
    def _init(
        path: str = path,
        agent: Optional[str] = agent,
        provider: Optional[str] = provider,
    ) -> None:
        """Initialize fractal for this repository (or sub-project)."""
        fractal.core.worktree.ensure_git_repo(path)
        node, path = resolve_init_target(path)
        output = node.init(path=path, agent=agent, provider=provider, user=True)
        if output:
            typer.echo(output)

    return app


def track(app: typer.Typer) -> typer.Typer:
    """Register the ``track`` command."""
    # name argument
    name_help = 'Tree root branch (default: the tree this checkout belongs to).'
    name = typer.Argument(None, help=name_help)
    # path option
    path_help = 'Repository path.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'track')
    def _track(
        name: Optional[str] = name,
        path: str = path,
    ) -> None:
        """Track the user node's ``.fractal/`` data on the top-level branch.

        Removes the seed dir's self-ignore file so it is no longer hidden,
        then prints the git command that stages it -- the index is never
        touched. Idempotent and usable from any checkout in the repo;
        ``fractal untrack`` is the inverse.
        """
        user, seed_dir = _resolve_user_seed(path, name)
        fractal.core.worktree.seed_ignore_remove(user.node_dir)
        typer.echo(
            f'Tracking {seed_dir}/ on the top-level branch.'
            f'\nNext: stage it with: git add -- {seed_dir}'
        )

    return app


def untrack(app: typer.Typer) -> typer.Typer:
    """Register the ``untrack`` command."""
    # name argument
    name_help = 'Tree root branch (default: the tree this checkout belongs to).'
    name = typer.Argument(None, help=name_help)
    # path option
    path_help = 'Repository path.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'untrack')
    def _untrack(
        name: Optional[str] = name,
        path: str = path,
    ) -> None:
        """Git-ignore the user node's ``.fractal/`` data (the default state).

        Restores the seed dir's self-ignore file so it is hidden on the
        top-level branch, then prints the git command that unstages an
        already-committed seed -- the index is never touched. Idempotent and
        usable from any checkout in the repo; ``fractal track`` is the
        inverse.
        """
        user, seed_dir = _resolve_user_seed(path, name)
        fractal.core.worktree.seed_ignore_write(user.node_dir)
        typer.echo(
            f'Ignoring {seed_dir}/ on the top-level branch.'
            f'\nNext: unstage a committed seed with: git rm -r --cached -- {seed_dir}'
        )

    return app


def commit(app: typer.Typer) -> typer.Typer:
    """Register the ``commit`` command."""
    # message argument
    message_help = 'Short description for the commit message (required unless --check).'
    message = typer.Argument(None, help=message_help)
    # init flag
    init_help = 'Baseline commit ("init" instead of "iteration <run>.<iter>").'
    init = typer.Option(False, '--init', help=init_help)
    # check flag
    check_help = 'Exit 1 if uncommitted changes exist instead of committing.'
    check = typer.Option(False, '--check', help=check_help)
    # ignore scope flag
    ignore_scope_help = 'Commit out-of-scope changes but still lint.'
    ignore_scope = typer.Option(False, '--ignore-scope', help=ignore_scope_help)
    # force flag
    force_help = 'Bypass scope and lint checks and git hooks.'
    force = typer.Option(False, '--force', help=force_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'commit')
    def _commit(
        message: Optional[str] = message,
        init: bool = init,
        check: bool = check,
        ignore_scope: bool = ignore_scope,
        force: bool = force,
        path: str = path,
    ) -> None:
        """Commit the current iteration's work.

        With ``--check``, a dirty tree is the command's own nonzero
        outcome: it exits 1 with the notice on stderr, while command
        errors exit 2, so scripts gate on the tree state by exit code.
        """
        # validate arguments
        if init and check:
            raise typer.BadParameter('--init cannot be used with --check.')
        if init and ignore_scope:
            raise typer.BadParameter('--init cannot be used with --ignore-scope.')
        if init and force:
            raise typer.BadParameter('--init cannot be used with --force.')
        if check and ignore_scope:
            raise typer.BadParameter('--check cannot be used with --ignore-scope.')
        if check and force:
            raise typer.BadParameter('--check cannot be used with --force.')
        if ignore_scope and force:
            raise typer.BadParameter('--ignore-scope cannot be used with --force.')
        if not message and not check:
            raise typer.BadParameter('Message is required unless --check is set.')
        node = resolve_node(path)
        # check mode is the command's own nonzero outcome: only the typed
        # dirty-tree signal converts to the reserved exit 1 -- every other
        # failure rides through to the wrapper's Error: exit 2
        if check:
            try:
                node.commit(
                    message=message,
                    init=init,
                    check=True,
                    ignore_scope=ignore_scope,
                    force=force,
                )
            except DirtyWorktreeError as e:
                typer.echo(f'{e}', err=True)
                raise SystemExit(1) from e
            return
        output = node.commit(
            message=message,
            init=init,
            check=False,
            ignore_scope=ignore_scope,
            force=force,
        )
        if output:
            typer.echo(output)

    return app


def open(app: typer.Typer) -> typer.Typer:
    """Register the ``open`` command."""
    # name argument
    name_help = (
        'Tree root branch, or a node branch to focus'
        " (default: the caller's own tree, at its root)."
    )
    name = typer.Argument(None, help=name_help)
    # path option
    path_help = 'Repository path.'
    path = typer.Option('.', '--path', help=path_help)
    # light flag
    light_help = 'Open with the light palette.'
    light = typer.Option(False, '--light', help=light_help)
    # dark flag
    dark_help = 'Open with the dark palette (the default).'
    dark = typer.Option(False, '--dark', help=dark_help)

    @command(app, 'open')
    def _open(
        name: Optional[str] = name,
        path: str = path,
        light: bool = light,
        dark: bool = dark,
    ) -> None:
        """Open a tree's fractal TUI (the cockpit), focused on a node or its root."""
        if light and dark:
            raise typer.BadParameter('--light and --dark are mutually exclusive.')
        # NOTE: import textual lazily: the TUI must stay off cold start
        from fractal.tui import FractalApp, theme

        # the palette applies before the app constructs (the theme module's
        # tokens are read at render time, so one select re-skins everything)
        theme.select('light' if light else 'dark')
        # one slot takes either name (mirrors `node list`): a tree root opens
        # the cockpit at the root, a node branch opens the tree owning it,
        # focused there. A root owns no worktree, so it resolves by config --
        # never the worktree map, which would need its branch checked out
        user = Node.resolve_user(path, name=name) if name else None
        branch = None
        if user is None and name:
            node = resolve_target(path, name)
            # the node names its own tree, whatever the caller's checkout
            user, branch = resolve_user_node(node.worktree), node.branch
        # the cockpit is the tree's, so anchor on the user node by config, not
        # the checkout (mirrors pause): a branch-keyed resolution would refuse
        # to open on a non-init checkout, when the operator most wants to look
        root = user if user is not None else resolve_user_node(path)
        FractalApp(root, branch=branch).run()

    return app


def pause(app: typer.Typer) -> typer.Typer:
    """Register the ``pause`` command."""
    # name argument
    name_help = 'Tree root branch (default: the tree this checkout belongs to).'
    name = typer.Argument(None, help=name_help)
    # reason option
    reason_help = 'Optional reason for pausing.'
    reason = typer.Option(None, '--reason', help=reason_help)
    # path option
    path_help = 'Repository path.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'pause')
    def _pause(
        name: Optional[str] = name,
        reason: Optional[str] = reason,
        path: str = path,
    ) -> None:
        """Pause the whole tree: abort in-flight agents, park every loop."""
        # a tree-wide brake -- anchor on the user node by config, never the
        # current branch: on a non-init checkout resolve_node would mis-scope
        # to a lone child (or die on two), silently narrowing the exact
        # emergency brake it exists for
        node = resolve_user_node(path, name)
        result = node.pause(reason)
        typer.echo(result)

    return app


def resume(app: typer.Typer) -> typer.Typer:
    """Register the ``resume`` command."""
    # name argument
    name_help = 'Tree root branch (default: the tree this checkout belongs to).'
    name = typer.Argument(None, help=name_help)
    # path option
    path_help = 'Repository path.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'resume')
    def _resume(
        name: Optional[str] = name,
        path: str = path,
    ) -> None:
        """Resume the paused tree where it left off (leaf-first)."""
        # anchored on the user node like pause (by config, not branch), so the
        # release matches the brake from any checkout
        node = resolve_user_node(path, name)
        result = node.resume()
        typer.echo(result)

    return app


def reset(app: typer.Typer) -> typer.Typer:
    """Register the ``reset`` command."""
    # name argument
    name_help = 'Tree root branch (default: the tree this checkout belongs to).'
    name = typer.Argument(None, help=name_help)
    # force flag
    force_help = 'Skip confirmation prompt (paused nodes are killed without asking).'
    force = typer.Option(False, '--force', '-f', help=force_help)
    # path option
    path_help = 'Repository path.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'reset')
    def _reset(
        name: Optional[str] = name,
        force: bool = force,
        path: str = path,
    ) -> None:
        """Reset a tree: remove its node worktrees, keep the history."""
        # the confirmation names the repo, resolved from any cwd inside it
        # (the agent's NODE_DIR, a worktree, or the repo root)
        repo_dir = Node(path).repo_dir
        # anchor on the user node by config, not the checkout (mirrors pause):
        # a bare Node(path) on a non-init branch counts 0 nodes and hides the
        # paused-node kill warning below; the resolution is strict with or
        # without --force, so a missing tree is refused before any confirmation
        user = resolve_user_node(path, name)
        if not force:
            count = len(user.child_list())
            s = 's' if count != 1 else ''
            typer.echo(
                "Warning: This permanently removes every one of the tree's"
                ' node worktrees, branches, and registrations. The user node,'
                ' project wiki, and all history are left in place.',
                err=True,
            )
            # the confirmation is the authorization to kill paused nodes, so
            # it must name them (the teardown settles their frozen work)
            paused = len(user.list(status='paused', live=True))
            if paused:
                p = 's' if paused != 1 else ''
                hold = 'hold' if paused != 1 else 'holds'
                typer.echo(
                    f'Warning: {paused} paused node{p} {hold} frozen mid-step'
                    ' work and will be killed.',
                    err=True,
                )
            prompt = f'Reset the tree {user.branch!r} at {repo_dir} ({count} node{s})?'
            typer.confirm(prompt, abort=True)
        output = Node.reset(path, name=name)
        if output:
            typer.echo(output)

    return app


def destroy(app: typer.Typer) -> typer.Typer:
    """Register the ``destroy`` command."""
    # name argument
    name_help = 'Tree root branch to destroy (mutex with --all; one is required).'
    name = typer.Argument(None, help=name_help)
    # all flag
    all_help = 'Destroy every tree and remove .worktrees/ (the full inverse of init).'
    all_ = typer.Option(False, '--all', help=all_help)
    # force flag
    force_help = 'Skip confirmation prompt (paused nodes are killed without asking).'
    force = typer.Option(False, '--force', '-f', help=force_help)
    # path option
    path_help = 'Repository path.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'destroy')
    def _destroy(
        name: Optional[str] = name,
        all_: bool = all_,
        force: bool = force,
        path: str = path,
    ) -> None:
        """Destroy a fractal tree, or the whole fractal with ``--all``."""
        # exactly one scope: a bare destroy is ambiguous between "this tree"
        # and "everything", so the caller must name one
        if (name is not None) == all_:
            raise typer.BadParameter('Name a tree or pass --all (exactly one).')
        # the confirmation names the repo, resolved from any cwd inside it
        # (the agent's NODE_DIR, a worktree, or the repo root)
        repo_dir = Node(path).repo_dir
        # the trees in scope: the named one, resolved strictly with or without
        # --force so an unknown name is refused the same way either way; or
        # every tree, since the counts below must sum the whole teardown
        trees = [resolve_user_node(path, name)] if name else Node.user_nodes(path)
        if not force:
            if all_:
                typer.echo(
                    'Warning: This permanently removes every node worktree and'
                    ' branch plus all fractal data, including every user node.'
                    ' The project wiki and commit history are left in place.',
                    err=True,
                )
            else:
                typer.echo(
                    "Warning: This permanently removes the tree's node"
                    ' worktrees and branches plus its fractal data, including'
                    ' its user node. Sibling trees, the project wiki, and'
                    ' commit history are left in place.',
                    err=True,
                )
            count = sum(len(tree.child_list()) for tree in trees)
            s = 's' if count != 1 else ''
            # the confirmation is the authorization to kill paused nodes, so
            # it must name them (the teardown settles their frozen work)
            paused = sum(len(tree.list(status='paused', live=True)) for tree in trees)
            if paused:
                p = 's' if paused != 1 else ''
                hold = 'hold' if paused != 1 else 'holds'
                typer.echo(
                    f'Warning: {paused} paused node{p} {hold} frozen mid-step'
                    ' work and will be killed.',
                    err=True,
                )
            if all_:
                prompt = f'Destroy the fractal at {repo_dir} ({count} node{s})?'
            else:
                prompt = f'Destroy the tree {name!r} at {repo_dir} ({count} node{s})?'
            typer.confirm(prompt, abort=True)
        output = Node.destroy(path, name=name)
        if output:
            typer.echo(output)

    return app


# ------ helper functions


def _bundled_skills() -> list[Traversable]:
    """Return the bundled skill dirs ``install`` ships (fractal's own plus wiki's).

    Sorted by name for stable output. The entries are real directories under
    a regular (non-zipped) install; only those can be symlink targets.
    """
    skills = []
    for package in ('fractal', 'wiki'):
        skills_dir = importlib.resources.files(package).joinpath('skills')
        skills.extend(path for path in skills_dir.iterdir() if path.is_dir())
    return sorted(skills, key=lambda path: path.name)


def _resolve_user_seed(path: str, name: Optional[str] = None) -> tuple[Node, str]:
    """Resolve a tree's user node and its seed dir (shared by track/untrack).

    Tracking is per tree, so the toggle anchors on the user node that owns
    the seed dir by config, not the current branch (mirrors pause) -- the
    verbs stay usable on any checkout inside the repo.
    """
    user = resolve_user_node(path, name)
    # the seed dir nests under <project>/ for a sub-project user node
    project = user.config.get('project', '.')
    if project == '.':
        seed_dir = f'{FRACTAL_FOLDER}/{user.branch}'
    else:
        seed_dir = f'{project}/{FRACTAL_FOLDER}/{user.branch}'
    return user, seed_dir
