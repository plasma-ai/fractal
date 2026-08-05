"""Implements ``Node`` class."""

from __future__ import annotations

import functools
import itertools
import json
import logging
import os
import pathlib
import re
import signal
import subprocess
import time
import typing
from collections.abc import Callable, Iterator
from typing import Any, Optional

import fractal.util
from fractal.constants import (
    CONFIG_FILE,
    DB_FILE,
    FRACTAL_FOLDER,
    PAUSED_FILE,
    PGID_FILE,
    PROJECT_FOLDER,
    SOCKET_FILE,
    STATUS_FILE,
    STATUSES,
    STEP_PGID_FILE,
    WORKTREES_FOLDER,
)
from fractal.typing import PathLike

from . import commit, render, worktree
from .config import DEFAULT_RESERVE_FRACTION, RESERVE_PRECISION, Config
from .cost import Cost
from .db import Database
from .files import Files
from .plan import Plans
from .radio import Radio
from .record import Record, _invalid_status
from .session import Sessions
from .time import Time

if typing.TYPE_CHECKING:
    from .agent import Agent, Invocation, StreamEvent

__all__ = [
    'Node',
    'node_dir',
    'tmux_session_name',
]

# module logger (the fractal.* hierarchy; the package never configures
# handlers -- a host attaches its own) for the classmethod diagnostics
# that have no instance logger
logger = logging.getLogger(__name__)

# quiet-time floor for the listing's staleness flag: an active loop writes
# activity at least once per step, so silence past max(step_timeout, 5m)
# earns the '!' suffix on the rendered age
_STALE_AGE_FLOOR_SECONDS = 300.0

# the listing's spend column is a steering read, not an invoice, so it
# rounds to cents -- the ledger commands report the full precision
_SPEND_PRECISION = 2


class Node:
    """An autonomous agent node in a git worktree.

    Tracks status in a ``.status`` file and the tree's central database
    (hosted in the root node's data directory); delegates shell-native
    work (git, tmux) to ``_scripts/``.
    """

    __config__: type[Config] = Config
    __cost__: type[Cost] = Cost
    __files__: type[Files] = Files
    __plans__: type[Plans] = Plans
    __radio__: type[Radio] = Radio
    __record__: type[Record] = Record
    __sessions__: type[Sessions] = Sessions
    __time__: type[Time] = Time

    def __init__(self: Node, path: PathLike, *, branch: Optional[str] = None) -> None:
        """Initialize ``Node``.

        Args:
            path: Worktree directory (or repo root for init).
            branch: Pin the node's branch instead of reading it from git.
                Only for resolving a node whose worktree is checked out to a
                different branch (the user node from a non-init checkout, via
                :meth:`resolve_user`); ``None`` reads the live git branch.

        """
        self._root = pathlib.Path(path).expanduser().resolve()
        self._branch = branch

    @property
    def is_user(self: Node) -> bool:
        """Return whether this is a user (root) node."""
        return self.config.get('user', False)

    @functools.cached_property
    def db(self: Node) -> Database:
        """Return the central database, hosted in the root node's data directory.

        Resolved through the ``root`` config key (written at init, inherited
        from the parent) and the root's ``.worktrees/.project/<root>`` cache,
        so any node in the tree opens the same ``.db`` without a worktree
        lookup.
        """
        root = self.config.get('root')
        project = worktree.project_path(self.repo_dir, root)
        if project == '.':
            db_path = self.repo_dir / FRACTAL_FOLDER / root / DB_FILE
        else:
            db_path = self.repo_dir / project / FRACTAL_FOLDER / root / DB_FILE
        schema_path = pathlib.Path(__file__).parent / 'schema.sql'
        return Database(db_path, schema_path)

    @functools.cached_property
    def radio(self: Node) -> Radio:
        """Return the node radio."""
        # construct through the collaborator slot
        return self.__radio__(self)

    @functools.cached_property
    def config(self: Node) -> Config:
        """Return the node configuration surface."""
        # construct through the collaborator slot
        return self.__config__(self)

    @functools.cached_property
    def cost(self: Node) -> Cost:
        """Return the node cost ledger (config caps over step-cost rows)."""
        # construct through the collaborator slot
        return self.__cost__(self)

    @functools.cached_property
    def files(self: Node) -> Files:
        """Return the node work-product surface (project files)."""
        # construct through the collaborator slot
        return self.__files__(self)

    @functools.cached_property
    def plans(self: Node) -> Plans:
        """Return the node plan files."""
        # construct through the collaborator slot
        return self.__plans__(self)

    @functools.cached_property
    def record(self: Node) -> Record:
        """Return the node execution record (run/iter/step spans, events, signals)."""
        # construct through the collaborator slot
        return self.__record__(self)

    @functools.cached_property
    def sessions(self: Node) -> Sessions:
        """Return the node per-iteration agent session map."""
        # construct through the collaborator slot
        return self.__sessions__(self)

    @functools.cached_property
    def time(self: Node) -> Time:
        """Return the node deadline accounting (config timeouts over row instants)."""
        # construct through the collaborator slot
        return self.__time__(self)

    @functools.cached_property
    def logger(self: Node) -> logging.Logger:
        """Return the stdlib logger named for the concrete class."""
        name = f'{type(self).__module__}.{type(self).__name__}'
        return logging.getLogger(name)

    def log(self: Node, message: str, level: int = logging.INFO) -> None:
        """Log a message to this node's logger."""
        self.logger.log(level, message)

    @functools.cached_property
    def branch(self: Node) -> str:
        """Return the current git branch name (one ``git rev-parse``, cached).

        A worktree's checked-out branch is fixed for the life of a ``Node``
        handle -- teardown/re-init paths construct fresh handles, so a fresh
        ``Node`` is a fresh read.
        """
        # a pinned branch (the cross-checkout user-node resolver) wins over the
        # live git read -- the user node's worktree may sit on another branch
        if self._branch is not None:
            return self._branch
        # safe to cache: fractal never switches a worktree's branch
        # mid-process -- handles are per-invocation (CLI) or refreshed
        # through TuiData's branch-keyed cache (TUI); cached_property caches
        # only successful resolution, so exists()'s RuntimeError swallow on
        # a repo-less path is unaffected
        return fractal.util.git.branch(self._root)

    @property
    def worktree(self: Node) -> pathlib.Path:
        """Return the node's resolved worktree path."""
        return self._root

    @property
    def _package_dir(self: Node) -> pathlib.Path:
        """Return the installed ``fractal`` package root (where the code lives)."""
        return pathlib.Path(__file__).parent.parent

    @functools.cached_property
    def repo_dir(self: Node) -> pathlib.Path:
        """Return the main git repo root (through worktrees; one probe, cached)."""
        return fractal.util.git.common_dir(self._root)

    @property
    def node_dir(self: Node) -> pathlib.Path:
        """Return the node data directory.

        Under a sub-project (per the ``.worktrees/.project/<branch>`` cache) the
        dir nests at ``<worktree>/<project>/.fractal/<branch>``.
        """
        return node_dir(self._root, self.project_path, self.branch)

    @property
    def _tree_latch_file(self: Node) -> pathlib.Path:
        """Return the tree-wide pause latch marker, beside the central database.

        Written by a user-node :meth:`pause` and removed by its
        :meth:`resume`: a depth-1 node's only ancestor is the statusless
        user root, so without the marker a start racing a tree-wide pause
        fan-out would slip in unfrozen. Lives in the root node's data
        directory, so it rides a filesystem transplant with the rest of
        the paused state.
        """
        return self.db.path.parent / PAUSED_FILE

    @property
    def _status_file(self: Node) -> pathlib.Path:
        """Return the path to the node's ``.status`` file (lifecycle state)."""
        return self.node_dir / STATUS_FILE

    @property
    def project_path(self: Node) -> str:
        """Return the worktree-relative project sub-path (``'.'`` for a repo-root node).

        Cached per-branch at ``.worktrees/.project/<branch>`` (written at init);
        absent for a repo-root project, which reads as ``'.'``.
        """
        return worktree.project_path(self.repo_dir, self.branch)

    @property
    def parent(self: Node) -> Optional[Node]:
        """Return the parent by dotted-branch derivation (``None`` for the user node).

        Also ``None`` when the parent's worktree is gone (pruned out of
        band), matching the ancestor walk's skip.
        """
        if '.' not in self.branch:
            return None
        parent_branch, *_ = self.branch.rsplit('.', 1)
        worktree_dir = fractal.util.git.find_worktree(self.repo_dir, parent_branch)
        return self.__class__(worktree_dir) if worktree_dir else None

    @property
    def tmux_session(self: Node) -> str:
        """Return the node's tmux session name (via :func:`tmux_session_name`)."""
        try:
            branch = self.branch
        except RuntimeError:
            # branchless fallback still matches the shells' name sanitization
            return self.repo_dir.name.replace('.', '-').replace(':', '-')
        return tmux_session_name(self.repo_dir, branch)

    def _tmux_session_exists(self: Node) -> Optional[bool]:
        """Return whether this node's tmux session is alive (``None`` unknown).

        Mirrors ``start.sh``'s check exactly: an exact-match (not ``tmux -t``,
        which resolves targets by prefix/fnmatch and false-matches longer
        names) of the session name against ``tmux list-sessions``. The
        listing asks the server the session lives on -- the socket the loop
        recorded at boot (``.socket``) -- because a tmux answer is evidence
        about one server only: the ambient socket's "no sessions" (a shell
        with a different ``TMUX_TMPDIR``) says nothing about a session alive
        on the recorded one; without a record (a tmux-less launch), the
        ambient socket is all there is. ``None`` when the probe is
        inconclusive -- :func:`fractal.util.tmux.probe` got no answer from
        tmux (binary absent, or ``list-sessions`` failed for anything but
        the definitive ``no server running``) -- so the reconcile path never
        mistakes a blind host for a dead loop.
        """
        # probe the recorded socket, not whichever one this shell resolves
        socket_file = self.node_dir / SOCKET_FILE
        if socket_file.exists():
            socket = socket_file.read_text(encoding='utf-8').strip()
            sessions = fractal.util.tmux.probe(socket=socket)
        else:
            sessions = fractal.util.tmux.probe()
        if sessions is None:
            return None
        return self.tmux_session in sessions

    def _reconcile_status(self: Node) -> None:
        """Stamp a crashed-but-active node ``exited``.

        A loop that dies without ending (a hard kill, a direct
        ``tmux kill-session``, a host crash) leaves the ``.status`` file
        ``active`` with no tmux session, wedging the reject-active guards. The
        one-loop-per-node invariant (``start.sh`` refuses to launch while the
        session exists) makes a missing session proof the loop is gone, so
        stamp the same honest terminal :meth:`Record.run_start` uses for a
        stranded run -- both the ``.status`` file and the crashed run's
        still-open runs/iters/steps rows, so a later merge/delete/retire (none
        of which start a loop) cannot leave the DB reading ``active`` while the
        status reads ``exited``. The settle also heals any config/registry cap
        drift (:meth:`Config.reconcile`): a loop that died before its next iteration
        boundary never ran the boundary reconcile, and the dead row would
        otherwise carry the drift forever. A no-op unless the status is
        ``active``, so a settled node never pays the tmux probe -- and a
        ``paused`` node is never healed: no session is its *normal* parked
        state (the loop exits at pause; ``resume`` relaunches it), on this
        host or after a filesystem transplant to another. An inconclusive
        probe (no answer from tmux) also skips the heal: the reap keys off
        proof the session is gone, never off ignorance -- a shell without
        tmux visibility (a cron/CI host) must not kill a healthy loop's
        process groups, and a shell on a *different* socket gets its proof
        from the loop's recorded one (see :meth:`_tmux_session_exists`).

        Never reconciles the node from inside its own running loop: the loop
        self-finishes (``send_budget_finish`` calls ``node finish``), and a
        host probing a socket its own session does not live on would
        otherwise read its own live run as crashed and kill the very run it
        is driving.
        """
        if self._is_own_loop():
            return
        if self.status() != 'active':
            return
        if self._tmux_session_exists() is False:
            self._reap_orphan()
            self.record.close_open('exited')
            self.status_set('exited')
            self.config.reconcile()
            # the heal is the record's catch -- the settled node keeps no
            # socket handle (the next boot writes a fresh one)
            (self.node_dir / SOCKET_FILE).unlink(missing_ok=True)

    def _reap_orphan(self: Node) -> None:
        """Kill surviving process groups recorded by a dead loop.

        The loop records its process group at run start (``.pgid``) and
        each agent invocation's own group
        (``.step_pgid``); both are removed on any in-band exit, so a file
        that outlives the tmux session marks an out-of-band pane death (tmux
        kill/crash, host OOM) whose agent may still be running -- and
        spending -- headless. The reap follows ``kill.sh``'s TERM-grace-KILL
        cadence and logs an ``orphan`` event naming each reaped pgid.
        Best-effort: a dead, recycled, or foreign group reads as already
        gone -- recycled meaning the OS re-issued a dead group's id to an
        unrelated same-user group, which answers a liveness probe but is
        younger than its record (:func:`_recorded_group`).
        """
        for name in (PGID_FILE, STEP_PGID_FILE):
            pgid_file = self.node_dir / name
            if not pgid_file.exists():
                continue
            # trust the record only while its group is still alive; a record
            # a rival reconciler already swept reads as gone
            try:
                recorded_at = pgid_file.stat().st_mtime
                pgid = int(pgid_file.read_text(encoding='utf-8').strip())
                os.killpg(pgid, 0)
            except (
                FileNotFoundError,
                ValueError,
                ProcessLookupError,
                PermissionError,
            ):
                pgid = 0
            # aliveness is not identity: a recycled pid answers the probe from
            # an unrelated group -- spare any group younger than its record
            if pgid > 0 and not _recorded_group(pgid, recorded_at):
                pgid = 0
            # reap the group and audit the reap
            if pgid > 0:
                try:
                    os.killpg(pgid, signal.SIGTERM)
                    for _ in range(10):
                        time.sleep(0.2)
                        os.killpg(pgid, 0)
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                event_id = self.record.event_start(
                    'orphan',
                    metadata=f'reaped pgid {pgid}',
                )
                self.record.event_end(event_id=event_id, status='completed')
            pgid_file.unlink(missing_ok=True)

    def _is_own_loop(self: Node) -> bool:
        """Return whether this process is running inside this node's own loop.

        The loop exports ``_NODE`` for the node it drives, so a ``fractal``
        call it spawns (e.g. its budget finish's ``node finish``) resolves
        the caller back to this node -- proof the loop is alive regardless of a
        tmux session, which the test harness and a tmux-less host lack.
        """
        caller = self.resolve_caller()
        return caller is not None and caller._root == self._root

    @classmethod
    def resolve_caller(cls: type[Node]) -> Optional[Node]:
        """Resolve the calling node from the environment.

        When running inside a node (``_NODE`` env var set),
        returns a ``Node`` bound to the caller's worktree.
        Returns ``None`` outside a node context.
        """
        if node_dir := os.environ.get('_NODE'):
            # resolve worktree root via git (handles scoped
            # project paths where .fractal/ is nested deeper)
            worktree = fractal.util.git.toplevel(
                pathlib.Path(node_dir),
                check=False,
            )
            if worktree:
                return cls(worktree)
        return None

    @classmethod
    def resolve_actor(cls: type[Node]) -> Optional[Node]:
        """Resolve the acting node from the environment, else the cwd.

        The authoritative actor for the guards a seat must not talk its
        way out of (the mailbox seal, the drain's spawn/re-arm refusals):
        :meth:`resolve_caller` reads the loop-exported ``_NODE``, which a
        seat can unset, so an unset env falls back to the node owning the
        current directory -- a step runs in its own worktree, so the
        fallback names the same node the export would have.

        Residual (filed, not closed here): a seat that both unsets
        ``_NODE`` and works from outside its worktree resolves to no
        actor. Closing that needs process-lineage attribution, not an
        environment read.

        Returns:
            The acting node, or ``None`` outside any node context.

        """
        if caller := cls.resolve_caller():
            return caller
        # cwd fallback: resolve the directory's worktree root, then its node
        try:
            worktree = fractal.util.git.toplevel(pathlib.Path.cwd(), check=False)
        except OSError:
            return None
        if worktree is None:
            return None
        node = cls(worktree)
        return node if node.exists() else None

    def drain_bound(self: Node) -> bool:
        """Return whether a drain binds this node's own acting seat.

        Drain-ness is recorded on the run (a ``drain`` signal row), not
        just exported into the seat's environment, so the refusal stands
        after an env scrub and survives a pause/resume of the drain run.
        Binds only the draining node's own seat: an operator, or another
        node, acts normally.

        Returns:
            Whether the acting node is this node and its open run drains.

        """
        actor = self.resolve_actor()
        if actor is None or actor.branch != self.branch:
            return False
        try:
            runs = self.record.runs(limit=1)
            if not runs or runs[0]['ended_at'] is not None:
                return False
            return self.record.signal_get('drain', run_id=runs[0]['run_id']) is not None
        except Exception:
            return False

    @classmethod
    def user_nodes(cls: type[Node], path: PathLike) -> list[Node]:
        """Return every tree's user (root) node in the repo, branch-sorted.

        One repository can carry several fractal trees, each rooted on its
        own branch with its own data directory and central database. Scan
        the repo's fractal data dirs (top-level and each sub-project) for
        the ``config.json`` marked ``user: true`` and pin a ``Node`` to each
        root branch, independent of the git checkout.

        Args:
            path: Any path inside the repo.

        Returns:
            The repo's user (root) nodes, branch-pinned and sorted by
            branch; empty when there is no fractal, or no git repo at all.

        """
        # mirror exists(): a repo-less path has no user node, not an error
        try:
            repo = cls(path).repo_dir
        except RuntimeError:
            return []
        # the user config lives at <repo>/[<project>/].fractal/<branch>/config.json;
        # check the top level first, then each sub-project dir
        fractal_dirs = [repo / FRACTAL_FOLDER]
        fractal_dirs += [
            sub / FRACTAL_FOLDER
            for sub in sorted(repo.iterdir())
            if sub.is_dir() and sub.name != WORKTREES_FOLDER
        ]
        # a nested sub-project (e.g. packages/foo) sits below iterdir's reach;
        # its dir resolves through the .worktrees/.project cache (written at
        # init, kept through reset)
        project_dir = repo / WORKTREES_FOLDER / PROJECT_FOLDER
        if project_dir.is_dir():
            for project_file in sorted(project_dir.iterdir()):
                project = worktree.project_path(repo, project_file.name)
                if project != '.':
                    fractal_dirs.append(repo / project / FRACTAL_FOLDER)
        # keyed by branch: init enforces one fractal per branch, and the dirs
        # above can name the same sub-project twice (iterdir plus the cache)
        users: dict[str, Node] = {}
        for fractal_dir in fractal_dirs:
            if not fractal_dir.is_dir():
                continue
            for config_path in sorted(fractal_dir.glob(f'*/{CONFIG_FILE}')):
                try:
                    config = json.loads(config_path.read_text(encoding='utf-8'))
                except (OSError, json.JSONDecodeError):
                    continue
                if config.get('user'):
                    # anchor at the git root -- Node.node_dir derives the
                    # <project>/ prefix from the .worktrees/.project cache, so a
                    # sub-project anchor would double the prefix (mirrors
                    # resolve_init_target); the branch is the config dir's name
                    branch = config_path.parent.name
                    users.setdefault(branch, cls(repo, branch=branch))
        return [users[branch] for branch in sorted(users)]

    @classmethod
    def resolve_user(
        cls: type[Node],
        path: PathLike,
        *,
        name: Optional[str] = None,
    ) -> Optional[Node]:
        """Resolve one tree's user (root) node by config, not the checkout.

        A bare ``Node`` keys on the repo's *current* branch, so on a non-init
        checkout (the user on their own branch while nodes run) the user node
        reads as uninitialized even though the fractal exists. ``name`` picks
        a tree by its root branch outright; otherwise the caller's own branch
        selects the tree that owns it -- a node worktree sits on
        ``<root>.<...>``, the repo root on the root branch itself -- and a
        lone tree answers for any checkout.

        Args:
            path: Any path inside the repo.
            name: Root branch of the tree to resolve; ``None`` infers it from
                the caller's branch.

        Returns:
            The tree's user (root) node, branch-pinned, or ``None`` when
            there is no such tree (no fractal, no git repo, or no tree
            under ``name``).

        Raises:
            RuntimeError: If the repo carries several trees and the caller's
                branch belongs to none of them -- guessing would act on a
                healthy sibling.

        """
        users = cls.user_nodes(path)
        if name is not None:
            return next((user for user in users if user.branch == name), None)
        if len(users) <= 1:
            return users[0] if users else None
        # several trees: the caller's branch names its owner; check=False so a
        # detached checkout ('HEAD') or an unborn one (no branch at all, hence
        # the None guard below) falls through to the refusal instead of dying
        branch = fractal.util.git.branch(cls(path).worktree, check=False)
        if branch is not None:
            for user in users:
                if branch == user.branch or branch.startswith(f'{user.branch}.'):
                    return user
        trees = ', '.join(user.branch for user in users)
        # an unborn checkout has no branch to name in the refusal
        owner = f'branch {branch!r}' if branch is not None else 'this checkout'
        raise RuntimeError(
            f'This repository carries several fractal trees ({trees}) and'
            f' {owner} belongs to none of them — name the tree to act on.'
        )

    def exists(self: Node) -> bool:
        """Return whether this node has been initialized."""
        try:
            return (self.node_dir / CONFIG_FILE).exists()
        except RuntimeError:
            return False

    def _validate_charter(
        self: Node,
        charter: Optional[pathlib.Path],
        *,
        pin: Optional[str],
    ) -> None:
        """Validate a profile charter's fill-sheet before any spend.

        The stale-seed gate: four commissions once shipped with stale pins,
        stale docket rows, or truncated charters, each costing the node's
        opening seat plus an adjudication. Checks, all pre-worktree:

        - ``--pin`` (when given) resolves to a commit;
        - the charter carries its two authored sections (a truncated seed
          dies here);
        - every ``pin: <sha>`` line in the charter resolves to a commit and
          matches ``--pin`` (prefix-wise) when one is given;
        - every ``docket: <path>`` line resolves at the pin (the charter's
          own when ``--pin`` is absent, else ``HEAD``).

        Args:
            charter: The profile's ``NODE.md``, or ``None`` (pin-only).
            pin: The commission pin from ``--pin``, or ``None``.

        Raises:
            ValueError: On the first fill-sheet violation.

        """
        repo_dir = self.repo_dir

        def _commit(rev: str) -> bool:
            cmd = ['rev-parse', '--verify', '--quiet', f'{rev}^{{commit}}']
            return bool(fractal.util.git.run(cmd, cwd=repo_dir, check=False))

        if pin is not None and not _commit(pin):
            raise ValueError(f'--pin does not resolve to a commit: {pin!r}')
        if charter is None:
            return
        text = charter.read_text(encoding='utf-8')
        # the two authored sections bound the seed: a commission truncated
        # in transit loses the tail first
        for heading in ('## Instructions', '## Completion Requirements'):
            if heading not in text:
                raise ValueError(
                    f'Profile charter {charter} is missing {heading!r}'
                    ' (truncated or stale seed).'
                )
        pins = re.findall(r'^pin:\s*([0-9a-f]{7,40})\s*$', text, flags=re.M)
        for declared in pins:
            if not _commit(declared):
                raise ValueError(
                    f'Charter pin does not resolve to a commit: {declared!r}'
                    ' (stale seed).'
                )
            if pin is not None and not (
                pin.startswith(declared) or declared.startswith(pin)
            ):
                raise ValueError(
                    f'Charter pin {declared!r} does not match --pin {pin!r}'
                    ' (stale seed).'
                )
        # docket rows must exist at the pin -- an enumerated surface that
        # moved or never existed is exactly the stale-docket class
        anchor = pin or (pins[0] if pins else 'HEAD')
        for row in re.findall(r'^docket:\s*(\S+)\s*$', text, flags=re.M):
            cmd = ['cat-file', '-e', f'{anchor}:{row}']
            if fractal.util.git.run(cmd, cwd=repo_dir, check=False) is None:
                raise ValueError(
                    f'Docket row does not resolve at {anchor}: {row!r} (stale seed).'
                )

    def init(
        self: Node,
        name: Optional[str] = None,
        *,
        path: Optional[PathLike] = None,
        title: Optional[str] = None,
        scope: Optional[list[str]] = None,
        base: Optional[str] = None,
        meta: Optional[str] = None,
        inherit: Optional[list[str]] = None,
        steps: Optional[PathLike] = None,
        profile: Optional[str] = None,
        pin: Optional[str] = None,
        agent: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        max_iters: Optional[int] = None,
        max_depth: Optional[int] = None,
        max_children: Optional[int] = None,
        max_descendants: Optional[int] = None,
        timeout: Optional[str] = None,
        iter_timeout: Optional[str] = None,
        step_timeout: Optional[str] = None,
        step_retries: Optional[int] = None,
        step_retry_backoff: Optional[str] = None,
        interval: Optional[str] = None,
        sleep: Optional[str] = None,
        wait: Optional[str] = None,
        max_cost: Optional[float] = None,
        max_iter_cost: Optional[float] = None,
        max_step_cost: Optional[float] = None,
        reserve_budget: Optional[float] = None,
        sync: Optional[bool] = None,
        detached: Optional[bool] = None,
        local: Optional[bool] = None,
        blind: bool = False,
        sealed: bool = False,
        reset: bool = False,
        user: bool = False,
    ) -> str:
        """Initialize an autonomous node.

        Creates a git worktree on a new branch named
        ``<name>`` (or ``<parent>.<name>`` if the current
        branch is a node) and populates a ``.fractal/<branch>/``
        data directory with steps, hooks, skills, and
        configuration from the skill source.

        Args:
            name: Node name (current branch for user node).
            path: Project path (relative to repo) for user node; for a
                child, a non-root value selects its sub-project (default:
                inherit the parent's). A path under ``.worktrees/`` (the
                cwd resolving into a worktree) names the parent instead.
            title: Human-readable display name (defaults to the de-slugged name).
            scope: Subdirectory scope within the worktree.
            base: Branch to start from.
            meta: Target node branch for meta-configuration.
            inherit: Surfaces to seed from the parent node instead of the
                package seed (``steps``, ``scripts``, ``skills``,
                ``config``, or ``all``). ``config`` copies the parent's
                preference keys only -- budget-class caps never inherit.
            steps: Directory of step files (``*.md``, each carrying the
                loop's ``NN-`` digit prefix at one width) to seed the
                node's ``steps/`` from instead of the package seed;
                mutually exclusive with inheriting ``steps``.
            profile: Named seed bundle under
                ``.fractal/profiles/<name>/`` -- its ``steps/`` seeds the
                step list (like ``steps``) and its ``NODE.md`` seeds a
                deployment-ready charter, fill-sheet-validated at init;
                mutually exclusive with ``steps`` and inheriting them.
            pin: Commission pin (a commit sha): must resolve, and every
                ``pin:`` declaration in the profile charter must match it
                -- a stale seed dies at init, not at the first seat.
            agent: Agent type.
            provider: Provider route for the agent (e.g. ``openrouter``;
                default: the vendor-native endpoint, inherited from the
                nearest ancestor).
            model: Model override (passed via the agent CLI's model flag).
            effort: Reasoning-effort override (passed via the agent
                CLI's effort flag).
            max_iters: Maximum number of iterations.
            max_depth: Maximum child node nesting depth.
            max_children: Maximum direct child nodes.
            max_descendants: Maximum total descendant nodes.
            timeout: Timeout per run (e.g. ``30m``).
            iter_timeout: Timeout per iteration (e.g. ``30m``).
            step_timeout: Timeout per step (e.g. ``30m``).
            step_retries: Retries per failed step (default 1; 0 disables).
            step_retry_backoff: Delay before each step retry
                (e.g. ``10s``; default ``10s``).
            interval: Fixed iteration schedule (e.g. ``30m``).
            sleep: Delay between iterations (e.g. ``10s``).
            wait: Sleep between approval-wait sync invocations (e.g. ``5m``).
            max_cost: Maximum cost in USD.
            max_iter_cost: Maximum cost per iteration in USD.
            max_step_cost: Maximum cost per step in USD
                (warn-only when unenforceable).
            reserve_budget: USD reserved for cleanup; shifts when the node
                enters reserve mode (not enforced).
            sync: Run sync mode before each step.
            detached: Separate agent invocation per step.
            local: Skip pushing to remote after each commit.
            blind: Subscribe to no channels (the parent still reads it).
            sealed: Seal the node's mailbox -- its own seat cannot read
                hosted messages until an operator or the parent unseals it
                (``config sealed=false``, which the sealed seat itself may
                not run); the hold mechanism for verifier isolation.
            reset: Delete all node files and reinitialize.
            user: Initialize as a user node (DB + radio only).

        Returns:
            Script output.

        """
        from .agent import command_base, resolve
        from .loop import _STEP_PREFIX

        # coerce path to a str -- downstream '.' comparisons and persisted
        # caches expect the string form
        if path is not None:
            path = str(path)
        # a draining seat spawns nothing: the export reaches the seat's
        # subprocesses and the run's own drain signal backs it after an env
        # scrub or a pause/resume, so a spawn from a draining run refuses
        # harness-side (a resumed plan replaying a stale spawn wave was a
        # whole damage class once); checked ahead of every branch below,
        # user init included -- a whole new tree, with its own database,
        # radio, and a root to spawn from, is the largest creation verb here
        if _draining():
            raise RuntimeError(
                'Cannot init a node from a draining run (--drain forbids spawns).'
            )
        # handle user node init (name derived from current branch)
        if user:
            if name:
                raise ValueError('User nodes do not accept a name.')
            return self._init_user(path=path, agent=agent, provider=provider)
        # validate name
        if not name:
            raise ValueError('Node name is required.')
        worktree.validate_name(name)
        # flatten scope entries -- the CLI form is comma-separated, with
        # repeated flags tolerated, and whitespace is the stored form's
        # separator (config _set splits on it), so split on both here;
        # validating the pre-split string would pass roots the canonical
        # form later shatters
        if scope:
            scope = [
                root
                for entry in scope
                for chunk in entry.split(',')
                for root in chunk.split()
            ]
            # a scope root is a repo-relative subdirectory -- reject an
            # absolute or '..' path, which would persist into config.json
            # (against the no-absolute-paths rule) and never match commit.py's
            # relative prefix check, bricking every scoped commit
            for root in scope:
                parts = pathlib.PurePosixPath(root).parts
                if pathlib.PurePosixPath(root).is_absolute() or '..' in parts:
                    raise ValueError(
                        f'--scope must be a repo-relative subdirectory, not'
                        f' {root!r} (no absolute or ".." paths).'
                    )
        # flatten inherit entries the same way and expand the 'all' alias --
        # agent config alone inherits from the parent unconditionally; every
        # other surface is opt-in
        if inherit:
            inherit = [
                surface.strip()
                for entry in inherit
                for surface in entry.split(',')
                if surface.strip()
            ]
            for surface in inherit:
                if surface not in ('steps', 'scripts', 'skills', 'config', 'all'):
                    raise ValueError(
                        f'Unknown inherit surface: {surface!r}'
                        ' (valid: steps, scripts, skills, config, all).'
                    )
            if 'all' in inherit:
                inherit = ['steps', 'scripts', 'skills', 'config']
        # resolve a named profile: a repo-provided seed bundle under
        # .fractal/profiles/<name>/ -- its steps/ seeds the step list
        # exactly like --steps, and its NODE.md seeds a deployment-ready
        # charter whose fill-sheet is validated below, so a stale or
        # truncated seed dies at init instead of at the node's first seat
        charter: Optional[pathlib.Path] = None
        if profile is not None:
            if steps is not None:
                raise ValueError('--profile cannot be combined with --steps.')
            if inherit and 'steps' in inherit:
                raise ValueError('--profile cannot be combined with --inherit=steps.')
            profile_dir = self.repo_dir / FRACTAL_FOLDER / 'profiles' / profile
            if not profile_dir.is_dir():
                raise ValueError(f'No profile found at {profile_dir}.')
            if (profile_dir / 'steps').is_dir():
                steps = profile_dir / 'steps'
            if (profile_dir / 'NODE.md').is_file():
                charter = profile_dir / 'NODE.md'
        # the fill-sheet gate: a pinned commission must hold together before
        # any spend (--pin alone still validates the pin itself)
        if pin is not None or charter is not None:
            self._validate_charter(charter, pin=pin)
        # an explicit steps dir is a rival step source to inheriting the
        # parent's -- refuse the combination rather than pick one silently --
        # and it must satisfy the loop's discovery contract, checked here so a
        # bad profile refuses before any worktree is created rather than
        # failing the node's first iteration; resolved so the arg handed to
        # init.sh never depends on the script's inherited cwd
        if steps is not None:
            if inherit and 'steps' in inherit:
                raise ValueError('--steps cannot be combined with --inherit=steps.')
            steps = pathlib.Path(steps).resolve()
            if not steps.exists():
                raise ValueError(f'--steps directory does not exist: {steps}')
            if not steps.is_dir():
                raise ValueError(f'--steps is not a directory: {steps}')
            # only regular files seed -- init.sh copies the same set
            step_files = sorted(
                entry for entry in steps.glob('*.md') if entry.is_file()
            )
            if not step_files:
                raise ValueError(
                    f'--steps directory contains no step files (*.md): {steps}'
                )
            # the loop discovers steps by their NN- prefix and fails an
            # iteration on a missing prefix or mixed digit widths
            widths = set()
            for step_file in step_files:
                match = _STEP_PREFIX.match(step_file.name)
                if match is None:
                    raise ValueError(
                        f'--steps directory has a step file without an'
                        f' NN- prefix: {step_file.name}'
                    )
                widths.add(len(match.group(1)))
            if len(widths) > 1:
                raise ValueError(
                    f'--steps directory mixes digit prefix widths: {steps}'
                )
        # expand --meta into --base + --scope
        if meta:
            # handle mutually exclusive flags
            if scope:
                raise ValueError('--meta cannot be combined with --scope.')
            if base:
                raise ValueError('--meta cannot be combined with --base.')
            # validate target exists
            worktree_dir = self.repo_dir / WORKTREES_FOLDER / meta
            if not worktree_dir.is_dir():
                raise ValueError(
                    f'Meta target {meta!r} has no worktree.'
                    ' Initialize the target node first.'
                )
            # branch from the target node
            base = meta
            # scope to the target's seed dir; read its project from the .project
            # cache so sub-project nodes get the right prefix
            target_project = worktree.project_path(self.repo_dir, meta)
            if target_project == '.':
                scope = [f'{FRACTAL_FOLDER}/{meta}']
            else:
                scope = [f'{target_project}/{FRACTAL_FOLDER}/{meta}']
        # the base is also the squash-merge target: merge.sh squashes inside
        # the base's checked-out worktree, so a worktree-less base (a typo,
        # or a branch nothing has checked out) would only fail at merge time,
        # long after init printed its success -- refuse now, naming the
        # requirement
        if base and fractal.util.git.find_worktree(self.repo_dir, base) is None:
            raise ValueError(
                f'Base branch {base!r} has no checked-out worktree.'
                ' The base is the squash-merge target, so it must be'
                ' checked out in this repository.'
            )
        # prefer the calling node (_NODE) so an agent's child nests under it,
        # not the repo-root user node; fall back to self for a top-level spawn
        parent = self.resolve_caller()
        # only adopt an ambient caller that lives in this repo -- a _NODE pointing
        # at a different repo would register the child in the wrong DB (split-brain)
        if parent is not None and parent.repo_dir != self.repo_dir:
            parent = None
        # no ambient caller but a path under .worktrees/ (the CLI derives path
        # from cwd, so this is a manual init from inside a worktree): parent on
        # that worktree's node rather than the root default
        if parent is None and path is not None:
            parts = pathlib.Path(path).parts
            if len(parts) >= 2 and parts[0] == WORKTREES_FOLDER:
                candidate = Node(self.repo_dir / WORKTREES_FOLDER / parts[1])
                if candidate.exists():
                    parent = candidate
        if parent is None or not parent.exists():
            parent = self
        # validate parent node
        if not parent.exists():
            raise FileNotFoundError(
                'Parent node could not be located.'
                " Run 'fractal init' to create a user node."
            )
        # the composed child branch is bounded too (git writes a ref
        # file per branch, plus a .lock suffix) -- checkable only now
        # that the parent is resolved
        worktree.validate_name(name, parent_branch=parent.branch)
        # validate the cost and duration flags (the one merged validator:
        # finiteness, positive ceiling, reserve range, step <= iter <= run
        # ordering, unit suffixes) -- a pure check of the passed flags (no live
        # state), so it stays out here; the live subtree/budget caps
        # (max-children/depth/descendants/cost-remaining) are enforced inside the
        # .worktrees flock below, after a fresh re-read, so concurrent fan-out
        # cannot each pass the check before any of them takes the lock (a TOCTOU
        # race that would defeat the caps)
        config_values = {
            'max_cost': max_cost,
            'max_iter_cost': max_iter_cost,
            'max_step_cost': max_step_cost,
            'reserve_budget': reserve_budget,
            'timeout': timeout,
            'iter_timeout': iter_timeout,
            'step_timeout': step_timeout,
            'step_retry_backoff': step_retry_backoff,
            'interval': interval,
            'sleep': sleep,
            'wait': wait,
        }
        self.config.validate(config_values)
        # inherit local from the parent; local is immutable once set
        if parent.config.get('local'):
            if parent is self:
                if local is False:
                    raise ValueError('Local flag cannot be changed once set.')
                local = True
            elif local or local is None:
                local = True
            else:
                raise ValueError('Parent is local; child cannot push.')
        # default to non-local (allow pushing)
        if local is None:
            local = False
        # inherit the agent from the nearest ancestor (the user node sets the
        # default via `fractal init --agent`) when the spawn doesn't specify one
        if agent is None:
            for ancestor in parent._self_and_ancestors():
                if ancestor_agent := ancestor.config.get('agent'):
                    agent = ancestor_agent
                    break
            if agent is None:
                raise ValueError(
                    'No --agent given and no ancestor has one configured;'
                    " pass --agent or set a default with 'fractal init --agent'."
                )
        # resolve the agent's base command against the registry now, reading
        # the CLASS without construction (the child node does not exist yet)
        # -- a junk name would store fine and kill the loop at boot inside
        # the tmux pane, after start already printed its success, so the
        # typo refuses here with the registry's supported-list error
        base_word = command_base(agent)
        resolved = resolve(base_word, root=parent.db.path.parent)
        # inherit the provider route the same way, materializing it only when
        # the child's agent supports routes -- an openrouter-defaulting
        # ancestor must never pin a route on a route-less backend (no raise
        # when absent: None means the vendor's own endpoint)
        if provider is None:
            for ancestor in parent._self_and_ancestors():
                if ancestor_provider := ancestor.config.get('provider'):
                    provider = ancestor_provider
                    break
            if provider is not None and provider not in resolved.providers:
                provider = None
        # an explicit route the backend does not support would store fine
        # and silently spend vendor-native -- refuse like the agent typo
        # above, naming the supported set (only the explicit flag refuses;
        # the inherited default keeps its silent drop)
        elif provider not in resolved.providers:
            supported_providers = ', '.join(resolved.providers) or 'none'
            raise ValueError(
                f'Unsupported provider: {provider!r} (supported: {supported_providers})'
            )
        # inherit the parent's preference config when requested -- explicit
        # flags win, values land in the child's config as a spawn-time
        # snapshot, and budget-class keys (costs, iters, depth/children,
        # run timeout) never inherit; a null parent value stays null (the
        # loop defaults at read time)
        if inherit and 'config' in inherit:
            if model is None:
                model = parent.config.get('model')
            if effort is None:
                effort = parent.config.get('effort')
            if sync is None:
                sync = parent.config.get('sync')
            if detached is None:
                detached = parent.config.get('detached')
            if iter_timeout is None:
                iter_timeout = parent.config.get('iter_timeout')
            if step_timeout is None:
                step_timeout = parent.config.get('step_timeout')
            if step_retries is None:
                step_retries = parent.config.get('step_retries')
            if step_retry_backoff is None:
                step_retry_backoff = parent.config.get('step_retry_backoff')
            if wait is None:
                wait = parent.config.get('wait')
            # sleep and interval are rival pacing keys (the loop rejects
            # both set): inherit them only when the spawn sets neither
            if sleep is None and interval is None:
                sleep = parent.config.get('sleep')
                interval = parent.config.get('interval')
        # the child records the tree's root (inherited from the parent) so any
        # node can resolve the central database from its own config
        root = parent.config.get('root')
        # default the display title to the de-slugged node name
        if title is None:
            title = fractal.util.name_to_title(name)
        # build arguments (name and path are positional)
        args = [name, f'{self._root}']
        args.append(f'--title={title}')
        args.append(f'--parent={parent.branch}')
        args.append(f'--root={root}')
        # a non-root path selects the child's sub-project (default: inherit); a
        # .worktrees/ path is the cwd-in-a-worktree case above -- inherit too
        if path is not None and path != '.':
            parts = pathlib.Path(path).parts
            if parts[0] != WORKTREES_FOLDER:
                args.append(f'--project={path}')
        for scope_root in scope or []:
            args.append(f'--scope={scope_root}')
        if base:
            args.append(f'--base={base}')
        if meta:
            args.append(f'--meta={meta}')
        if inherit:
            joined = ','.join(inherit)
            args.append(f'--inherit={joined}')
        if steps is not None:
            args.append(f'--steps={steps}')
        if charter is not None:
            args.append(f'--charter={charter}')
        if agent:
            args.append(f'--agent={agent}')
        if provider:
            args.append(f'--provider={provider}')
        if model:
            args.append(f'--model={model}')
        if effort:
            args.append(f'--effort={effort}')
        if max_iters is not None:
            args.append(f'--max-iters={max_iters}')
        if max_depth is not None:
            args.append(f'--max-depth={max_depth}')
        if max_children is not None:
            args.append(f'--max-children={max_children}')
        if max_descendants is not None:
            args.append(f'--max-descendants={max_descendants}')
        if timeout is not None:
            args.append(f'--timeout={timeout}')
        if iter_timeout is not None:
            args.append(f'--iter-timeout={iter_timeout}')
        if step_timeout is not None:
            args.append(f'--step-timeout={step_timeout}')
        if step_retries is not None:
            args.append(f'--step-retries={step_retries}')
        if step_retry_backoff is not None:
            args.append(f'--step-retry-backoff={step_retry_backoff}')
        if interval is not None:
            args.append(f'--interval={interval}')
        if sleep is not None:
            args.append(f'--sleep={sleep}')
        if wait is not None:
            args.append(f'--wait={wait}')
        if max_cost is not None:
            args.append(f'--max-cost={max_cost}')
        if max_iter_cost is not None:
            args.append(f'--max-iter-cost={max_iter_cost}')
        if max_step_cost is not None:
            args.append(f'--max-step-cost={max_step_cost}')
        if reserve_budget is not None:
            args.append(f'--reserve-budget={reserve_budget}')
        if sync is True:
            args.append('--sync')
        if sync is False:
            args.append('--no-sync')
        if detached is True:
            args.append('--detached')
        if detached is False:
            args.append('--no-detached')
        if local:
            args.append('--local')
        if blind:
            args.append('--blind')
        if sealed:
            args.append('--sealed')
        if reset:
            args.append('--reset')
        # ensure git excludes
        self._git_exclude()
        # serialize concurrent child inits -- git worktree add is not parallel-safe
        with worktree.lock(self.repo_dir):
            # refuse to spawn into a paused subtree -- the pause latch admits
            # no new work until resume
            if latched := parent.pause_latched():
                raise RuntimeError(
                    f'Cannot spawn under a paused node ({latched}). Resume it first.'
                )
            # enforce the live subtree/budget caps under the lock, off a fresh
            # re-read of live descendants -- only now (serialized) is the count
            # authoritative, so concurrent fan-out can't each pass before any of
            # them registers its child and blows past the cap
            parent._enforce_spawn_limits(child_max_cost=max_cost)
            # locate child worktree
            child_branch = f'{parent.branch}.{name}'
            child_worktree_dir = fractal.util.git.find_worktree(
                repo_dir=self.repo_dir,
                branch=child_branch,
            )
            # a case-insensitive filesystem resolves a name differing from a
            # sibling only by case onto the sibling's worktree dir -- init.sh
            # would then run every path below (node dir, init event, a
            # --reset's rm -rf) against the sibling's node -- so refuse when
            # the child's path lands on another branch's worktree
            if child_worktree_dir is None:
                child_path = self.repo_dir / WORKTREES_FOLDER / child_branch
                if child_path.is_dir():
                    worktrees = fractal.util.git.worktree_map(self.repo_dir)
                    for branch, worktree_dir in worktrees.items():
                        if child_path.samefile(worktree_dir):
                            raise ValueError(
                                f'Node {child_branch!r} would alias existing'
                                f' node {branch!r} on this case-insensitive'
                                ' filesystem; use a name that differs by more'
                                ' than letter case.'
                            )
            # refuse an implicit adopt: exiting 0 against an existing node
            # would leave its old config in place and silently drop the
            # requested caps -- reuse is explicit in this CLI, never an accident
            if child_worktree_dir is not None:
                child = self.__class__(child_worktree_dir)
                if child.exists():
                    if not reset:
                        raise ValueError(
                            f'Node {child_branch!r} already exists; start it with'
                            ' `fractal node start --continue`, remove it with'
                            ' `fractal node delete`, or pass --reset to'
                            ' reinitialize it.'
                        )
                    # reset rm -rf's the node dir, so refuse over a running or
                    # frozen node exactly as delete/merge/retire do -- a live
                    # loop's step files or a paused node's frozen run context
                    # would otherwise be wiped irrecoverably
                    child._reconcile_status()
                    if child.status() == 'active':
                        raise RuntimeError(
                            'Cannot reinitialize an active node. Stop or kill it first.'
                        )
                    if child.status() == 'paused':
                        raise RuntimeError(
                            'Cannot reinitialize a paused node.'
                            ' Resume or kill it first.'
                        )
            # check for pre-existing branch
            cmd = ['show-ref', '--verify', f'refs/heads/{child_branch}']
            pre_existing_branch = fractal.util.git.run(
                cmd,
                cwd=self.repo_dir,
                check=False,
            )
            # init.sh creates the worktree before the registration below, so a
            # failure in between would strand a live worktree with no registry row
            # -- roll back, but only what *this* init created (a pre-existing
            # worktree means init failed on collision; leave it alone), and
            # never delete a reused pre-existing branch's committed history
            pre_existing = child_worktree_dir is not None
            try:
                # run script
                result = self._run_script('init.sh', *args)
                # seed the new node's radio (internal -- agents must not create
                # radios explicitly; user nodes seed theirs in _init_user)
                child_worktree_dir = fractal.util.git.find_worktree(
                    repo_dir=self.repo_dir,
                    branch=child_branch,
                )
                if child_worktree_dir:
                    self.__class__(child_worktree_dir).radio.init()
                # register child in the nodes table; log the spawn on the parent
                # (run lineage attaches only when it's mid-run -- an autonomous
                # spawn during EXECUTE -- else NULL)
                event_id = parent.record.event_start('spawn', metadata=child_branch)
                try:
                    parent.child_add(
                        name=name,
                        title=title,
                        max_cost=max_cost,
                        max_depth=max_depth,
                        max_children=max_children,
                        max_descendants=max_descendants,
                    )
                except Exception:
                    if event_id is not None:
                        parent.record.event_end(event_id=event_id, status='failed')
                    raise
                if event_id is not None:
                    parent.record.event_end(event_id=event_id, status='completed')
            except Exception:
                if not pre_existing:
                    created_branch = pre_existing_branch is None
                    worktree.cleanup_failed_worktree(
                        repo_dir=self.repo_dir,
                        branch=child_branch,
                        created_branch=created_branch,
                    )
                raise
        # warm the child's configured cache dirs (copy-on-write clones from
        # the main checkout, see worktree.clone_cache_dirs) after the lock
        # releases -- a multi-gigabyte clone must never serialize sibling
        # spawns -- reading the dir list from the tree's user config
        # ('clone_dirs', absent by default); resolve the user by config off
        # the tree's root branch, not by walking ancestor worktrees: the root
        # branch has no worktree to find whenever the main checkout sits on
        # another branch (the operator's own, while nodes run), and this key
        # lives only on the user node, so the lookup must reach it every time
        if child_worktree_dir is not None:
            user = Node.resolve_user(self.repo_dir, name=root)
            if user is not None:
                worktree.clone_cache_dirs(
                    repo_dir=self.repo_dir,
                    worktree_dir=child_worktree_dir,
                    dirs=user.config.get('clone_dirs') or [],
                )
        # surface the summary + any notices, but drop the per-artifact
        # "Created ..." progress lines that flood logs under wide fan-out
        # (errors don't come back through stdout here -- a failed init raises);
        # success-path warnings ride stderr and would vanish with the
        # CompletedProcess, so they append below the summary
        summary = '\n'.join(
            line
            for line in result.stdout.strip().splitlines()
            if not line.startswith('Created ')
        )
        notices = result.stderr.strip()
        return f'{summary}\n{notices}' if notices else summary

    def _init_user(
        self: Node,
        path: Optional[PathLike] = None,
        *,
        agent: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> str:
        """Initialize a user node at the repo root or a sub-project.

        Creates a lightweight ``<project>/.fractal/<branch>/`` data
        directory with database and radio only (no steps, hooks,
        skills, or scripts). ``self._root`` must be the worktree
        root; ``path`` is its project-relative path (``.`` for the
        repo root, ``app`` for a monorepo sub-project). Descendants
        inherit the project.

        Args:
            path: Project path relative to the repo root.
                Defaults to ``self._root`` relative to the git root.
            agent: Default agent command stored on the user node; spawned
                nodes inherit it when they omit ``--agent``.
            provider: Default provider route stored on the user node;
                spawned nodes inherit it when they omit ``--provider``.

        Returns:
            Confirmation message.

        """
        from .agent import command_base, resolve

        # alias branch
        branch = self.branch
        # a detached checkout (a tag clone, CI, mid-bisect) resolves to the
        # literal 'HEAD' -- a name git reserves, so it can only mean detachment
        # -- and a tree anchored on that pseudo-ref re-resolves to whatever is
        # checked out later, orphaning the registry and stranding the baseline
        if branch == 'HEAD':
            raise ValueError(
                'Cannot initialize a user node on a detached HEAD.'
                " Check out a branch first ('git switch -c <branch>')."
            )
        # reject a slash branch up front -- every per-branch artifact (the
        # .project cache entry, the .fractal/<branch> data dir, the scripts'
        # reads of both) keys on the branch as a single path component, so a
        # 'feat/x'-style branch fails here before any partial init is written
        if '/' in branch:
            raise ValueError(
                f'Cannot initialize a user node on branch {branch!r}:'
                " branch names containing '/' are not supported — switch"
                ' to a slash-free branch and re-run init.'
            )
        # a dotted root branch is fine on its own -- the root's branch is the
        # user's ('v1.0', 'stable-2.1'), not fractal's to name -- but two roots
        # may not dot-nest: '.' is the node hierarchy separator, so a tree
        # rooted at 'v1.0' reads as a node inside one rooted at 'v1', and every
        # <root>.* scope (destroy, reset, the ancestor walk) would cross between
        # them -- a re-init sees its own tree here, so skip it
        for other in Node.user_nodes(self.repo_dir):
            if other.branch == branch:
                continue
            nested = branch.startswith(f'{other.branch}.')
            nests_other = other.branch.startswith(f'{branch}.')
            if nested or nests_other:
                raise ValueError(
                    f'Cannot initialize a user node on branch {branch!r}: it'
                    f' collides with the tree rooted at {other.branch!r} —'
                    " '.' is the node hierarchy separator, so one reads as a"
                    ' node inside the other and every subtree scope would cross'
                    ' between them. Switch to a branch that is not dot-nested'
                    ' with an existing tree root and re-run init.'
                )
        # default the path to self._root relative to the repo root; coerce to a
        # str so it serializes cleanly into config.json and the .project cache
        if path is None:
            path = str(self._root.relative_to(self.repo_dir))
        else:
            path = str(path)
        # reject initializing inside a worktree (path under .worktrees/)
        parts = pathlib.Path(path).parts
        if parts and parts[0] == WORKTREES_FOLDER:
            raise ValueError(
                'Cannot initialize a user node inside a worktree.'
                ' Run from the repo root or a sub-project folder.'
            )
        # enforce one fractal per branch -- a branch maps to a single project
        project_dir = self.repo_dir / WORKTREES_FOLDER / PROJECT_FOLDER
        project_file = project_dir / branch
        if project_file.exists():
            existing = project_file.read_text(encoding='utf-8').strip()
            if existing != path:
                raise ValueError(
                    f'A fractal already exists on branch {branch!r} for project'
                    f' {existing!r}; one branch maps to a single project —'
                    ' use a separate branch.'
                )
        # derive the project name from the repo dir (dashes -> _, validated as an
        # ASCII identifier) up front so a bad dir name fails before any partial
        # init is written; it doubles as the wiki name
        wiki_name = worktree.derive_project_name(self.repo_dir)
        # resolve the default agent against the registry up front, for the
        # same reason -- a typo'd name would store fine, every spawn would
        # inherit it, and each start's loop would die on a vanishing tmux
        # pane with the registry error unread; the node dir (the central
        # DB's future home, derivable pre-init) is consulted for a hook
        # file when present -- a fresh init has none, so built-ins gate
        if agent is not None:
            base_word = command_base(agent)
            resolve(base_word, root=self.node_dir)
        # idempotent: an existing user node is not clobbered, but a partial
        # prior init (config.json written before db/radio/wiki) is repaired
        # on re-run -- db.init and radio.init are both idempotent
        if self.is_user:
            if agent is not None:
                self.config.set('agent', agent)
            if provider is not None:
                self.config.set('provider', provider)
            # repair a stranded DB/radio: config.json marks the node a user before
            # db/radio are seeded, so a crash between them leaves a valid-looking
            # config over an unseeded tree until re-run re-seeds them
            self.db.init()
            self.radio.init()
            created = worktree.ensure_project_wiki(
                worktree=self._root,
                repo_dir=self.repo_dir,
                path=path,
                name=wiki_name,
            )
            # re-check host hooks for the formatter lanes (informational)
            worktree.verify_hook_formatters(self.repo_dir)
            message = f'User node already initialized on branch {branch!r}.'
            if agent is not None:
                message += f' Updated default agent to {agent}.'
            if provider is not None:
                message += f' Updated default provider to {provider}.'
            if created:
                message += ' Re-created the missing project wiki.'
            return message
        # refuse if another active fractal on this machine shares this repo's
        # tmux namespace: node sessions and `node kill` resolve by the global
        # name '<repo-basename> (<branch>)', so two fractals under one basename
        # collide and a kill can cross-fire onto the other tree -- tmux names
        # carry no repo path, so a session counts as ours only when its name
        # derives from a branch this repo has checked out: a sibling tree's
        # running node is our own namespace, not a stranger's
        repo_name = self.repo_dir.name.replace('.', '-').replace(':', '-')
        sessions = fractal.util.tmux.probe()
        if sessions is not None:
            prefix = f'{repo_name} ('
            ours = {
                tmux_session_name(self.repo_dir, checkout)
                for checkout in fractal.util.git.worktree_map(self.repo_dir)
            }
            foreign = sessions - ours
            clash = next((name for name in foreign if name.startswith(prefix)), None)
            if clash is not None:
                raise RuntimeError(
                    f'Another active fractal already uses the tmux name'
                    f' {repo_name!r} (session {clash!r}). Two fractals sharing a'
                    f' repository basename collide on node sessions and'
                    f' `node kill` — rename this repository directory or stop'
                    f' that fractal first.'
                )
        # write the project cache first so node_dir resolves under <project>/
        worktree.set_project_path(self.repo_dir, branch, path)
        # create node directory (under <repo_dir>/<project>/.fractal/<branch>)
        node_dir = self.node_dir
        node_dir.mkdir(parents=True, exist_ok=True)
        # self-ignore the seed dir before any state lands in it -- a fresh
        # tree is untracked by definition; `fractal track` is the only opt-in
        worktree.seed_ignore_write(node_dir)
        # write config (the 'user' flag marks node identity, not lifecycle;
        # 'root' anchors the central database for the whole tree)
        config = {
            'user': True,
            'project': path,
            'root': branch,
        }
        if agent is not None:
            config['agent'] = agent
        if provider is not None:
            config['provider'] = provider
        config_path = node_dir / CONFIG_FILE
        text = json.dumps(config, indent=2)
        fractal.util.filesystem.write_atomic(config_path, text + '\n')
        # resolve the seed and wiki paths (sub-project nodes nest under <project>/)
        if path == '.':
            seed, wiki = FRACTAL_FOLDER, 'wiki'
        else:
            seed, wiki = f'{path}/{FRACTAL_FOLDER}', f'{path}/wiki'
        # ensure git excludes -- the static block covers the runtime artifacts
        # init creates outside the node dir (.worktrees/ above all)
        worktree.exclude_update(self.repo_dir)
        # initialize database and radio
        self.db.init()
        self.radio.init()
        # initialize the project wiki if it doesn't exist
        created = worktree.ensure_project_wiki(
            worktree=self._root,
            repo_dir=self.repo_dir,
            path=path,
            name=wiki_name,
        )
        # check host hooks for the formatter lanes (informational)
        worktree.verify_hook_formatters(self.repo_dir)
        # report what landed and the baseline commit that must follow -- a
        # node worktree can only branch from a committed tree
        if path == '.':
            headline = f'Initialized user node on branch {branch}'
        else:
            headline = f'Initialized user node on branch {branch} (project {path!r})'
        summary = f'Created {seed}/{branch}/ (config, database, radio)'
        if created:
            summary += f' and the project wiki at {wiki}/'
        next_step = 'Next: commit the baseline: fractal commit "<message>" --init'
        return f'{headline}\n{summary}\n{next_step}'

    def _git_exclude(self: Node) -> None:
        """Write fractal's ignore patterns into the repo-local ``info/exclude``.

        A pure template refresh: the block is static, identical for every
        tree -- per-tree ignore state lives in each user seed dir's own
        self-ignore file, which no block rewrite can touch.
        """
        worktree.exclude_update(self.repo_dir)

    def start(
        self: Node,
        *,
        continue_run: bool = False,
        clean: bool = False,
        drain: bool = False,
        max_cost: Optional[float] = None,
    ) -> str:
        """Launch the node in a tmux session.

        Creates a tmux session (or window if already inside
        tmux) that runs the iteration loop. All run parameters
        are read from ``config.json`` (set at init or edited
        before launch); ``continue_run`` (with its optional ``max_cost``
        retune) is the only launch-time action.

        ``drain`` runs the continued run as a wind-down: the loop exports
        ``_DRAIN`` into every seat's environment (init/start/update/resume
        refuse under it) and injects the DRAIN mode doc into prompts.

        A continue re-enters the unsettled pool, so it re-checks the
        width/descendant gates (:meth:`_enforce_rearm_limits`) and re-arms to
        ``idle`` under the ``.worktrees`` flock; the loop stamps ``active`` at
        boot just as a fresh start does. A budget-ended run (the run row's
        ``exited``/0 landing) never re-arms silently: a bare continue
        refuses, naming the spent and armed figures, and an explicit
        ``max_cost`` is applied through the parent's retune
        (:meth:`child_retune`) before the launch. A retune below the node's
        own ``max_iter_cost`` refuses in the continue's own terms, naming
        that floor and both remedies -- the general ordering rule would
        otherwise cite a config field the operator never passed.

        Every successful launch logs a completed ``start`` event (metadata
        ``continue`` on a continue), so the event log carries the node's
        restart chain with the actor column answering who re-armed.

        Args:
            continue_run: Continue a stopped/exited node.
            clean: Acknowledge that the continue's worktree restore
                discards uncommitted project files (required when any
                exist).
            max_cost: New cost cap in USD for the continued run, applied
                through the parent's retune; required when the last run
                ended on its cost budget.

        Returns:
            Script output, prefixed by any launch-time notices (the
            retune echo, the continue-from-killed countermand).

        """
        from .agent import command_base, resolve

        # reject user nodes
        if self.is_user:
            raise RuntimeError('Cannot start a user node.')
        # a draining seat re-arms nothing (the export plus the run's own
        # durable drain signal, so an env scrub does not lift it)
        if _draining():
            raise RuntimeError(
                'Cannot start a node from a draining run (--drain forbids re-arms).'
            )
        # reconcile a crashed-but-active node so --continue isn't wedged
        self._reconcile_status()
        # validate status
        current_status = self.status()
        if current_status == 'retired':
            raise RuntimeError('Cannot start a retired node. Unretire it first.')
        if current_status == 'paused':
            raise RuntimeError('Cannot start a paused node. Resume it first.')
        # statuses a continue may re-arm from
        continuable = ('completed', 'stopped', 'exited', 'killed')
        if continue_run:
            if current_status not in continuable:
                raise RuntimeError(f'Cannot continue from status: {current_status!r}')
        else:
            if current_status != 'idle':
                raise RuntimeError(
                    f'Cannot start from status: {current_status!r}.'
                    f' Use --continue to restart.'
                )
        # refuse to launch into a paused subtree -- the pause latch admits no
        # new work until resume
        if latched := self.pause_latched():
            raise RuntimeError(
                f'Cannot start under a paused node ({latched}). Resume it first.'
            )
        # refuse a foreign tmux-session collision on a fresh start: a
        # first-start node has no session of its own, so a live session under
        # its name belongs to another fractal sharing this repo's basename,
        # and `node kill`/attach would resolve ambiguously across both; a
        # continue is exempt -- its own session may legitimately be present,
        # and start.sh's exact-name check still backstops that path
        if not continue_run:
            session = self.tmux_session
            sessions = fractal.util.tmux.probe()
            if sessions is not None and session in sessions:
                raise RuntimeError(
                    f'Cannot start: the tmux session {session!r} is already'
                    f' active for another fractal (a repository sharing this'
                    f' basename and node name). Stop it, or rename one'
                    f' repository directory.'
                )
        # launch-time notices (the retune echo, the kill countermand) ride
        # the returned confirmation -- core never prints; the CLI echoes it
        notices: list[str] = []
        # the retune's {key: (old, new)} priors -- the refusal and failure
        # paths below roll a persisted retune back through them, so a refused
        # or failed continue never keeps a --max-cost the caller passed
        changes: dict[str, tuple[Any, Any]] = {}
        # a budget-ended run (the exited/0 landing) never re-arms silently --
        # continuing spends real money the caller must re-authorize, so a bare
        # continue refuses with the recorded figures, and an explicit new cap
        # applies through the parent's retune (reserve re-derivation included)
        # before the config reads below see it
        if continue_run:
            rows = self.record.runs(limit=1)
            last_run = rows[0] if rows else None
            run_exited = (last_run is not None) and (last_run['status'] == 'exited')
            budget_ended = run_exited and (last_run['exit_code'] == 0)
            if budget_ended and max_cost is None:
                spent = self.cost.spent(run_id=last_run['run_id'])
                # the stamped arm survives later retunes; a NULL stamp (the
                # run started uncapped) falls back to the config cap -- an
                # uncapped node landed by an ancestor's cascade leaves both
                # empty, so it has no armed figure to name
                armed = last_run['max_cost']
                if armed is None:
                    armed = self.config.get('max_cost')
                if armed is not None:
                    figures = f'spent ${spent:.4f} of ${armed} armed'
                else:
                    figures = f'spent ${spent:.4f}, no cap armed'
                # a cascade landing carries the recomposed abort reason, so
                # name whose budget cut the run rather than claiming its own
                ended = 'ended on its cost budget'
                if (last_run['metadata'] or '').startswith('ancestor budget abort:'):
                    ended = "was cut by an ancestor's cost budget"
                raise RuntimeError(
                    f'Cannot continue: the last run {ended} ({figures}).'
                    f' Pass --max-cost=<usd> to arm the next run explicitly.'
                )
            # a continue's worktree restore (the loop's checkout/clean) discards
            # uncommitted project files -- refuse without the explicit --clean
            # acknowledgment; node-dir paths are exempt (the restore commits
            # them, config.json included); -uall is load-bearing: without it git
            # collapses an untracked node dir to a bare `?? .fractal/` entry the
            # prefix filter would miss; checked before the retune below so a
            # refused continue never persists a --max-cost the caller passed
            if not clean:
                node_prefix = f'{self.node_dir.relative_to(self._root)}/'
                # -z emits NUL-terminated entries with verbatim paths -- the
                # line form C-quotes spaces and non-ASCII, defeating the prefix
                # exemption -- and needs the unstripped run_bytes: strip would
                # eat the first entry's leading status space, shifting offsets
                porcelain = fractal.util.git.run_bytes(
                    ['status', '--porcelain', '-z', '--untracked-files=all'],
                    cwd=self._root,
                )
                # fail closed: a status failure must not read as a clean tree
                if porcelain is None:
                    raise RuntimeError('Cannot continue: git status failed.')
                decoded = porcelain.decode('utf-8', errors='replace')
                # each field is `XY <path>`; a rename/copy appends the original
                # path as a bare extra field -- consume it with its entry
                fields = iter(decoded.split('\0'))
                doomed = []
                for field in fields:
                    if not field:
                        continue
                    code, path = field[:2], field[3:]
                    if 'R' in code or 'C' in code:
                        next(fields, None)
                    if not path.startswith(node_prefix):
                        doomed.append(path)
                if doomed:
                    listed = '\n'.join(f'  {path}' for path in doomed)
                    raise RuntimeError(
                        'Cannot continue: the worktree restore would discard'
                        f' uncommitted changes:\n{listed}\n'
                        'Commit them first, or pass --clean to discard them.'
                    )
            if max_cost is not None:
                # a retune landing under the node's own per-iteration cap
                # fails the config ordering rule; at the retune site the
                # operator asked for a run cap, so name the floor and both
                # ways past it rather than let the generic ordering error
                # cite a field they never passed
                max_iter_cost = self.config.get('max_iter_cost')
                if max_iter_cost is not None and max_cost < float(max_iter_cost):
                    floor = float(max_iter_cost)
                    raise RuntimeError(
                        f'--max-cost ${max_cost:.2f} sits below {self.branch}'
                        f"'s per-iteration cap (${floor:.2f} max_iter_cost);"
                        f' pass --max-cost >= ${floor:.2f}, or lower the'
                        " node's max_iter_cost first."
                    )
                # echo the retune old -> new like `node update` -- a silent
                # launch-time retune is indistinguishable from a dropped one
                *_, name = self.branch.rsplit('.', 1)
                # the retune records on the parent; an orphaned parent (worktree
                # pruned out of band) can't take it, so refuse cleanly rather
                # than dereference None
                parent = self.parent
                if parent is None:
                    raise RuntimeError(
                        f'Cannot retune {self.branch} on start: its parent'
                        ' worktree is gone, so --max-cost cannot be recorded.'
                    )
                changes = parent.child_retune(name, max_cost=max_cost)
                for key, values in changes.items():
                    prior, value = values
                    if prior is None:
                        prior = 'unset'
                    notices.append(f'{key}: {prior} -> {value}')
        # a non-positive ceiling launches straight into a degenerate $0 finish, so
        # reject it; a missing ceiling means uncapped -- allowed but warned loudly
        # since spend is then untracked, bounded only by --max-iters/--timeout (a
        # token-priced agent with no priced model can only run this way -- a
        # cost cap would force it onto a priced model)
        max_cost = self.config.get('max_cost')
        if max_cost is not None and max_cost <= 0:
            raise RuntimeError(
                'Cannot start with a non-positive max_cost;'
                ' set a positive cap with `fractal node update --max-cost=<usd>`'
                ' or unset it to run uncapped.'
            )
        if max_cost is None:
            self.log(
                message=f'Warning: starting {self.branch} without a cost cap;'
                ' spend is untracked and bounded only by --max-iters/--timeout.',
                level=logging.WARNING,
            )
        # re-validate the rest of the config the loop reads -- the documented
        # steering path edits config.json directly, bypassing the init/update
        # setters' checks; a bad duration or cost ordering would otherwise abort
        # the loop after start prints "Started", wedging the node idle with the
        # only error on a dying tmux pane
        self.config.validate()
        # resolve the stored agent against the registry the way the loop's
        # boot does (the loop reads this node's own config key, never the
        # ancestor walk) -- the same steering path can typo or drop it, and
        # the loop's registry error would land on the same dying pane
        agent = self.config.get('agent')
        if not agent:
            raise ValueError('No agent configured; set --agent at node init.')
        base_word = command_base(agent)
        resolve(base_word, root=self.db.path.parent)
        # a blind node reads no channels -- sweep any subscriptions that
        # landed between init (which seeds none) and this launch
        if self.config.get('blind'):
            self.db.delete('subs', where={'node': self.branch})
        # build arguments
        args = [f'{self._root}']
        if continue_run:
            args.append('--continue')
        if drain:
            args.append('--drain')
        # ensure git excludes
        self._git_exclude()
        if continue_run:
            # a continue from killed re-arms over another actor's explicit
            # kill, so surface the recorded countermand first -- the latest
            # *completed* kill event's attribution (refused and failed
            # kills also ride the event stream, as failed rows that must
            # not shadow the real kill)
            if current_status == 'killed':
                where = {'node': self.branch, 'event': 'kill', 'status': 'completed'}
                rows = self.db.read('events', where=where, limit=1)
                if rows:
                    countermand = rows[0]['metadata']
                    notices.append(f'Previous run {countermand}')
            try:
                # gate re-check and idle re-arm stay atomic under the
                # .worktrees flock (init's check+register atomicity) -- the
                # loop stamps active only at boot, so a post-lock re-arm
                # would let a concurrent gate read this node as settled and
                # hand its slot away
                with worktree.lock(self.repo_dir):
                    # re-read under the lock -- a concurrent continue that won the
                    # race has already re-armed this node out of a settled status
                    current_status = self.status()
                    if current_status not in continuable:
                        raise RuntimeError(
                            f'Cannot continue from status: {current_status!r}'
                        )
                    self._enforce_rearm_limits()
                    self.status_set('idle')
                # launch outside the lock -- the idle re-arm above already holds
                # the slot; roll a failed launch back to the settled status so
                # --continue stays the retry path (idle reads as never-started)
                try:
                    result = self._run_script('start.sh', *args)
                except Exception:
                    self.status_set(current_status)
                    raise
            except Exception:
                # a refused gate or failed launch must not keep the retune --
                # restore the priors in both stores (config.json, and the
                # registry row for its one table-backed key)
                for key, values in changes.items():
                    prior, _ = values
                    self.config.set(key, prior)
                if 'max_cost' in changes:
                    prior, _ = changes['max_cost']
                    self.db.update(
                        data={'max_cost': prior},
                        table='nodes',
                        where={'node': self.branch},
                    )
                raise
        else:
            # run script
            result = self._run_script('start.sh', *args)
        # log the lineage only after start.sh returns, on both paths -- the
        # continue arm rolls a failed launch back to the settled status (a
        # lock-time event would survive that rollback as a phantom), so the
        # restart chain is exactly the ordered completed start events
        metadata = 'continue' if continue_run else ''
        event_id = self.record.event_start('start', metadata=metadata)
        self.record.event_end(event_id=event_id, status='completed')
        # prepend the launch-time notices to the script output -- one
        # confirmation string for the CLI to echo
        output = result.stdout.strip()
        if notices:
            output = '\n'.join([*notices, output])
        return output

    def attach(self: Node) -> None:
        """Attach to the node's tmux session."""
        # validate status
        if self.status() != 'active':
            raise RuntimeError('Cannot attach: node is not active.')
        # run attach script, then attach to the tmux session (named by start.sh);
        # the interactive handoff bypasses _run_script: tmux owns the terminal,
        # so output is not captured and tmux reports its own errors
        self._run_script('attach.sh', f'{self._root}')
        # the = prefix forces an exact target match (-t resolves by prefix,
        # so a short name would false-match longer session names)
        subprocess.run(['tmux', 'attach', '-t', f'={self.tmux_session}'])

    def _signal_guard(
        self: Node,
        verb: str,
        event: str,
        *,
        fan_out: bool = False,
    ) -> Optional[int]:
        """Gate a graceful signal verb; return the run it signals.

        The shared preamble of ``finish``/``finish_cancel``/``stop``/
        ``pause`` (the per-node helpers run it under their ``.worktrees``
        flock, so the status they read cannot settle mid-verb; the fan-out
        verbs also run it once before their sweep, so a non-active target
        refuses before any descendant is signaled): reconcile
        a crashed-but-active node so it hits the clear not-active guard,
        not the misleading no-run error, then require an active status --
        an active node with no run cannot be signaled. Refusals route
        through :meth:`_signal_refuse`, which puts a failed event row on
        the record before raising (or, on fan-out, skipping).

        Args:
            verb: The verb for the refusal texts (e.g. ``'finish'``).
            event: Event type for the refusal record (e.g.
                ``'finish_cancel'``).
            fan_out: Refusals return ``None`` instead of raising.

        Returns:
            The current run's id, or ``None`` when a fan-out call is
            refused.

        Raises:
            RuntimeError: If the node is not active or has no run.

        """
        # reconcile a crashed-but-active node so it hits the clear
        # not-active guard below, not the misleading no-run error
        self._reconcile_status()
        # validate status
        if self.status() != 'active':
            self._signal_refuse(verb, event, 'node is not active', fan_out=fan_out)
            return None
        # an active node with no run cannot be signaled
        _, _, run_id = self.record.resolve_context()
        if run_id is None:
            self._signal_refuse(verb, event, 'node has no run', fan_out=fan_out)
            return None
        return run_id

    def _signal_refuse(
        self: Node,
        verb: str,
        event: str,
        reason: str,
        *,
        fan_out: bool = False,
    ) -> None:
        """Record a refused signal verb, then raise (or skip, on fan-out).

        The refusal is put on the record either way -- a ``failed`` event
        row whose metadata names the reason -- so a sweep that skips a
        settled descendant still leaves evidence of the attempt.

        Args:
            verb: The verb for the refusal text (e.g. ``'finish'``).
            event: Event type for the refusal record.
            reason: Why the verb is refused.
            fan_out: Return instead of raising.

        Raises:
            RuntimeError: Always, unless ``fan_out``.

        """
        event_id = self.record.event_start(event, metadata=f'refused: {reason}')
        if event_id is not None:
            self.record.event_end(event_id=event_id, status='failed')
        if not fan_out:
            raise RuntimeError(f'Cannot {verb}: {reason}.')

    def _fan_out_reason(self: Node, verb: str, reason: Optional[str]) -> str:
        """Attribute a fanned-out reason to this node.

        The signal row a descendant records must name this node as the
        source -- an unattributed parent budget-reserve finish would read
        as the child's own boundary mis-fire.

        Args:
            verb: The fanning-out verb (e.g. ``'finish'``).
            reason: The caller's reason, if any.

        Returns:
            The attributed reason for the descendants' signal rows.

        """
        if reason:
            return f'{reason} (via {verb} of {self.branch})'
        return f'via {verb} of {self.branch}'

    def finish(self: Node, reason: Optional[str] = None) -> str:
        """Finish the node and its active descendants (children first).

        Each loop stops after its current iteration. On the user (root)
        node the fan-out covers the whole tree with no self signal -- the
        user node has no loop of its own.

        Args:
            reason: Optional reason for finishing.

        Returns:
            Confirmation message.

        """
        # a user-node finish is the tree-wide broadcast (budget cascades
        # land here): no self guard or signal, just the descendant sweep
        if self.is_user:
            propagated = self._fan_out_reason('finish', reason)
            finished_count = 0
            for _, descendant in self._live_descendants(status='active'):
                descendant._finish(propagated, fan_out=True)
                finished_count += 1
            if finished_count == 0:
                return 'No active nodes to finish.'
            suffix = 's' if finished_count != 1 else ''
            result = (
                f'Finish signal sent to {finished_count} node{suffix}'
                ' (each will stop after its current iteration)'
            )
            if reason:
                result += f': {reason}'
            return result
        # gate on self before the sweep -- a non-active target must refuse
        # with no descendant signaled; the per-node helper re-checks under
        # the flock for the race window
        self._signal_guard('finish', 'finish')
        propagated = self._fan_out_reason('finish', reason)
        # finish descendants first, then self -- each helper guards its own
        # node under the flock, so a descendant that settled mid-sweep is
        # skipped (refusal recorded) while the self-act refusal raises
        for _, descendant in self._live_descendants(status='active'):
            descendant._finish(propagated, fan_out=True)
        self._finish(reason)
        # build confirmation
        result = 'Finish signal sent (will stop after current iteration)'
        if reason:
            result += f': {reason}'
        return result

    def _finish(
        self: Node,
        reason: Optional[str] = None,
        *,
        fan_out: bool = False,
    ) -> None:
        """Send the ``finish`` signal to this node only.

        The guard re-read and the event/signal writes stay atomic under the
        ``.worktrees`` flock; ``finish.sh`` runs outside the lock.
        """
        with worktree.lock(self.repo_dir):
            # re-read under the lock -- a rival verb or the settling loop
            # may have moved this node since the caller enumerated it
            if self._signal_guard('finish', 'finish', fan_out=fan_out) is None:
                return
            event_id = self.record.event_start('finish', metadata=reason or '')
            self.record.signal_set('finish', reason or '')
        self._run_script('finish.sh', f'{self._root}')
        self.record.event_end(event_id=event_id, status='completed')

    def finish_cancel(self: Node, reason: Optional[str] = None) -> str:
        """Withdraw this node's pending ``finish`` signal.

        Deletes the signal rows for the current run, so the loop's boundary
        checks no longer see a pending finish. Descendants are untouched: a
        subtree ``finish`` fans out, but its cancel must not -- finishing is
        a descendant's normal completion path, not something to withdraw.

        Args:
            reason: Optional reason for the cancellation.

        Returns:
            Confirmation message.

        """
        run_id = self._signal_guard('cancel finish', 'finish_cancel')
        if self.record.signal_get('finish', run_id=run_id) is None:
            raise RuntimeError('Cannot cancel finish: no finish signal is set.')
        # delete the pending rows, bracketed by an audit event -- one of the
        # deliberate withdrawals catalogued on signal_clear
        event_id = self.record.event_start('finish_cancel', metadata=reason or '')
        self.db.delete(
            'signals',
            where={'node': self.branch, 'run_id': run_id, 'signal': 'finish'},
        )
        self.record.event_end(event_id=event_id, status='completed')
        # build confirmation
        result = 'Finish signal cancelled (loop continues)'
        if reason:
            result += f': {reason}'
        return result

    def stop(self: Node, reason: Optional[str] = None) -> str:
        """Stop the node and its active descendants (children first).

        Each loop stops after its current step. On the user (root) node
        the fan-out covers the whole tree with no self signal -- the user
        node has no loop of its own.

        Args:
            reason: Optional reason for stopping.

        Returns:
            Confirmation message.

        """
        # a user-node stop is the tree-wide broadcast: no self guard or
        # signal, just the descendant sweep
        if self.is_user:
            propagated = self._fan_out_reason('stop', reason)
            stopped_count = 0
            for _, descendant in self._live_descendants(status='active'):
                descendant._stop(propagated, fan_out=True)
                stopped_count += 1
            if stopped_count == 0:
                return 'No active nodes to stop.'
            suffix = 's' if stopped_count != 1 else ''
            result = (
                f'Stop signal sent to {stopped_count} node{suffix}'
                ' (each will stop after its current step)'
            )
            if reason:
                result += f': {reason}'
            return result
        # gate on self before the sweep -- a non-active target must refuse
        # with no descendant signaled; the per-node helper re-checks under
        # the flock for the race window
        self._signal_guard('stop', 'stop')
        propagated = self._fan_out_reason('stop', reason)
        # stop descendants first, then self -- each helper guards its own
        # node under the flock, so a descendant that settled mid-sweep is
        # skipped (refusal recorded) while the self-act refusal raises
        for _, descendant in self._live_descendants(status='active'):
            descendant._stop(propagated, fan_out=True)
        self._stop(reason)
        # build confirmation
        result = 'Stop signal sent (will stop after current step)'
        if reason:
            result += f': {reason}'
        return result

    def _stop(
        self: Node,
        reason: Optional[str] = None,
        *,
        fan_out: bool = False,
    ) -> None:
        """Send the ``stop`` signal to this node only.

        The guard re-read and the event/signal writes stay atomic under the
        ``.worktrees`` flock; ``stop.sh`` runs outside the lock.
        """
        with worktree.lock(self.repo_dir):
            # re-read under the lock -- a rival verb or the settling loop
            # may have moved this node since the caller enumerated it
            if self._signal_guard('stop', 'stop', fan_out=fan_out) is None:
                return
            event_id = self.record.event_start('stop', metadata=reason or '')
            self.record.signal_set('stop', reason or '')
        self._run_script('stop.sh', f'{self._root}')
        self.record.event_end(event_id=event_id, status='completed')

    def _killable(self: Node, current: str) -> bool:
        """Return whether ``current`` admits a kill of this node.

        ``active`` and ``paused`` always do, and so does ``idle`` -- it
        covers both a boot in flight (``start.sh`` created the session,
        the loop's preflight has not stamped ``active`` yet) and a
        never-started spawn. Killing at idle stamps ``killed`` so the
        node never activates: an unwanted spawn is reapable the moment
        it registers, not only once it starts burning.

        Args:
            current: The node status the caller read.

        Returns:
            Whether a kill may proceed.

        """
        return current in ('active', 'paused', 'idle')

    def kill(self: Node, reason: Optional[str] = None) -> str:
        """Kill the node and its unsettled descendants (children first).

        Reaps each tmux session and marks its active rows ``killed``. Paused
        nodes are killable -- the escape hatch for a parked subtree; with no
        loop alive the kill is pure bookkeeping (``kill.sh`` no-ops and the
        open rows close ``killed``). Idle nodes are killable too
        (:meth:`_killable`) -- a spawn in flight when the kill lands is
        reaped, not skipped, and a never-started spawn is stamped
        ``killed`` so it can never activate.

        Args:
            reason: Optional reason for killing.

        Returns:
            Confirmation message.

        """
        # gate on self before the sweep -- a settled target must refuse with
        # no descendant reaped; the self-kill below re-checks under the flock
        # for the race window
        current = self.status()
        if not self._killable(current):
            self._signal_refuse(
                verb='kill',
                event='kill',
                reason=f'node is not active, paused, or idle (status: {current})',
            )
        propagated = self._fan_out_reason('kill', reason)
        # reap descendants first (best-effort), then self
        # NOTE: re-enumerate to a fixpoint -- a descendant that registered
        #   mid-sweep (a spawn already in flight when the kill signal landed)
        #   would escape a single pass, so re-read until no fresh live
        #   descendant appears; _killable also admits a booting descendant
        #   ('idle' until preflight stamps 'active'), which a status-only
        #   filter would exit over; seen bounds the loop (each branch is
        #   reaped at most once and the subtree is finite), so it converges
        #   even if a stuck child never settles; each helper guards its own
        #   node under the flock, so a descendant that settled mid-sweep is
        #   skipped (refusal recorded) while the self-act refusal raises
        seen: set[str] = set()
        while True:
            fresh = [
                (row, descendant)
                for row, descendant in self._live_descendants()
                if descendant._killable(row['status']) and row['node'] not in seen
            ]
            if not fresh:
                break
            for row, descendant in fresh:
                seen.add(row['node'])
                try:
                    descendant._kill(propagated, fan_out=True)
                except Exception:
                    # best-effort: surface the failure but keep reaping the
                    # rest of the subtree -- a stuck child must not leave its
                    # siblings or the parent running
                    self.log(
                        message=f'Warning: failed to kill {descendant._root}',
                        level=logging.WARNING,
                    )
        return self._kill(reason)

    def _kill(
        self: Node,
        reason: Optional[str] = None,
        *,
        fan_out: bool = False,
    ) -> str:
        """Kill this node only and mark its active rows ``killed``.

        The status re-read and the kill's event/signal writes stay atomic
        under the ``.worktrees`` flock; ``kill.sh`` and the row marking run
        outside the lock. An idle target is also stamped ``killed`` under
        that flock: it has no terminal to clobber and possibly no session
        yet, and only a stamp that precedes the reap lets the loop's
        flock'd boot check stand a racing start down -- stamped after,
        the loop boots in the window, kill.sh has already no-op'd, and
        nothing ever reaps it (the loop never polls the kill signal). The
        attribution -- ``killed by <actor>``, with the reason appended when
        one is given -- lands identically on the kill event, the ``kill``
        signal, and the killed run row, so every surface answers who ended
        the run; a never-started spawn has none of the latter two, and its
        event alone carries the attribution.
        """
        # compose the attribution: who killed, and why when a reason rides
        caller = self.resolve_caller()
        actor = caller.branch if caller else 'operator'
        label = f'killed by {actor}: {reason}' if reason else f'killed by {actor}'
        with worktree.lock(self.repo_dir):
            # re-read under the lock -- a rival verb or the settling loop
            # may have moved this node since the caller enumerated it
            current = self.status()
            if not self._killable(current):
                self._signal_refuse(
                    verb='kill',
                    event='kill',
                    reason=f'node is not active, paused, or idle (status: {current})',
                    fan_out=fan_out,
                )
                return ''
            # set signal and log event; the tmux kill runs outside the lock
            event_id = self.record.event_start('kill', metadata=label)
            # a never-started spawn has no run for a signal to hang off -- the
            # kill event and the killed stamp are its whole record, so skip
            # the write rather than warn over a reap that worked
            if self.record.runs(limit=1):
                self.record.signal_set('kill', label)
            # an idle target is stamped here, not after the reap -- see the
            # docstring: the boot-window race is only closed flock-to-flock
            if current == 'idle':
                self.status_set('killed')
        # vet the recorded process groups before the script's fallback reap:
        # kill.sh gates on liveness alone, and a recycled id (the OS re-issued
        # a dead group's id to an unrelated same-user group) answers that
        # probe -- drop any record whose live group is not the one it named
        # (see _recorded_group), so the fallback only ever sees vetted files
        for name in (PGID_FILE, STEP_PGID_FILE):
            pgid_file = self.node_dir / name
            try:
                recorded_at = pgid_file.stat().st_mtime
                pgid = int(pgid_file.read_text(encoding='utf-8').strip())
            except (FileNotFoundError, ValueError):
                continue
            if not _recorded_group(pgid, recorded_at):
                pgid_file.unlink(missing_ok=True)
        try:
            result = self._run_script('kill.sh', f'{self._root}')
        except Exception:
            if self.exists():
                self._mark_active_killed(skip=event_id, metadata=label)
                self.status_set('killed')
            if event_id is not None:
                self.record.event_end(event_id=event_id, status='failed')
            raise
        # mark active rows as killed; a loop that landed its own terminal
        # between the signal and the reap keeps it -- the row marking is
        # already fenced, so the status stamp must not overwrite either
        if self.exists():
            self._mark_active_killed(skip=event_id, metadata=label)
            if self.status() in ('active', 'paused', 'idle'):
                self.status_set('killed')
        if event_id is not None:
            self.record.event_end(event_id=event_id, status='completed')
        return result.stdout.strip()

    def pause(self: Node, reason: Optional[str] = None) -> str:
        """Pause the node and its active descendants (parent-first).

        Signals ``pause`` then aborts each node's in-flight agent invocation
        (``pause.sh`` reaps the recorded step process group), so every loop
        reclassifies the abort and parks: it exits with status ``paused``,
        leaving its run and iteration rows open for :meth:`resume` to adopt.
        Fans out parent-first -- the inverse of every other signal -- so a
        parent parked before its children can never drain-complete over
        them, and re-enumerates until the subtree is fully signaled,
        catching children spawned mid-fan-out (``init``/``start`` refuse
        new work under the pause latch). On the user (root) node, which has
        no loop of its own, the fan-out covers the whole tree with no self
        signal -- the tree-wide brake -- and latches the root first, so
        even a depth-1 start racing the sweep refuses until resume.

        Args:
            reason: Optional reason for pausing.

        Returns:
            Confirmation message.

        """
        # a tree-wide pause latches the root before fanning out, so a start
        # or spawn racing the sweep (even at depth 1, where no pausable
        # ancestor exists) refuses instead of slipping in unfrozen
        if self.is_user:
            self._tree_latch_file.write_text('paused\n', encoding='utf-8')
        propagated = self._fan_out_reason('pause', reason)
        # pause self first, then sweep descendants shallowest first until
        # every active one carries a pause signal -- the re-enumeration
        # catches children spawned mid-fan-out; best-effort per node
        # (mirrors kill), each attempted exactly once
        paused_count = 0
        if not self.is_user:
            self._pause(reason)
            paused_count += 1
        attempted = set()
        while True:
            pending = [
                (row['node'], descendant)
                for row, descendant in self._live_descendants(status='active')
                if row['node'] not in attempted
                and descendant.record.signal_get('pause') is None
            ]
            if not pending:
                break
            pending.sort(key=lambda entry: entry[0].count('.'))
            for branch, descendant in pending:
                attempted.add(branch)
                try:
                    if descendant._pause(propagated, fan_out=True):
                        paused_count += 1
                except Exception:
                    self.log(
                        message=f'Warning: failed to pause {descendant._root}',
                        level=logging.WARNING,
                    )
        # build confirmation
        if paused_count == 0:
            return 'No active nodes to pause (tree latched until resume).'
        suffix = 's' if paused_count != 1 else ''
        result = (
            f'Pause signal sent to {paused_count} node{suffix}'
            ' (in-flight agents aborted; loops park paused)'
        )
        if reason:
            result += f': {reason}'
        return result

    def _pause(
        self: Node,
        reason: Optional[str] = None,
        *,
        fan_out: bool = False,
    ) -> bool:
        """Signal ``pause`` and abort this node's in-flight agent invocation.

        The guard re-read and the event/signal writes stay atomic under the
        ``.worktrees`` flock; ``pause.sh`` runs outside the lock. Reports
        whether the signal landed, so the sweep's count skips a refused
        descendant.
        """
        with worktree.lock(self.repo_dir):
            # re-read under the lock -- a rival verb or the settling loop
            # may have moved this node since the caller enumerated it
            if self._signal_guard('pause', 'pause', fan_out=fan_out) is None:
                return False
            # the signal lands before the abort so the loop reclassifies
            # the killed step as paused, never as a failed step (a failure
            # would force-commit and open a fresh session)
            event_id = self.record.event_start('pause', metadata=reason or '')
            self.record.signal_set('pause', reason or '')
        try:
            self._run_script('pause.sh', f'{self._root}')
        except Exception:
            # the signal is durable -- the loop still parks at its next
            # checkpoint -- but the failed abort must surface
            self.record.event_end(event_id=event_id, status='failed')
            raise
        self.record.event_end(event_id=event_id, status='completed')
        return True

    def resume(self: Node) -> str:
        """Resume the node and its paused descendants (leaf-first).

        Relaunches each paused loop, which adopts its open run where the
        pause left it: same budgets and iteration count, the interrupted
        step re-entered (resuming the recorded agent session when one
        exists, re-orienting fresh otherwise), and run/iteration deadlines
        credited for the paused span. Leaf-first so every child reads
        ``active`` again before its parent's drain-waits can look. A node
        still parking (``active`` with a pending pause signal) gets its
        pause withdrawn instead -- the live loop then never parks. On the
        user (root) node the fan-out covers the whole tree with no self
        relaunch -- the tree-wide release, which also lifts the root latch.

        Returns:
            Confirmation message.

        """
        # a draining seat re-arms nothing (export plus the run's durable
        # drain signal) -- relaunching a parked subtree from inside a
        # wind-down is the one expanding verb the other guards miss
        if _draining():
            raise RuntimeError(
                'Cannot resume a node from a draining run (--drain forbids re-arms).'
            )
        # a tree-wide resume releases the latch first -- new starts and
        # spawns are legal again the moment the release begins, even when
        # nothing is left parked to relaunch
        if self.is_user:
            self._tree_latch_file.unlink(missing_ok=True)
        # a non-user node must itself be paused or still pausing; the user
        # node only fans out
        self_pausing = False
        if not self.is_user:
            current = self.status()
            if current == 'active':
                self_pausing = self.record.signal_get('pause') is not None
            if current != 'paused' and not self_pausing:
                raise RuntimeError(
                    f'Cannot resume: node is not paused (status: {current}).'
                )
            # refuse to resume into a paused subtree -- the pause latch
            # admits no new work while an ancestor is frozen, and the
            # resume boot skips the ancestor walk (the leaf-first fan-out's
            # exemption), so the verb itself must refuse; resuming the
            # latching node relaunches this one with it
            if latched := self.pause_latched(skip_self=True):
                raise RuntimeError(
                    f'Cannot resume under a paused node ({latched}). Resume it first.'
                )
        # a node still parking (active with a pending pause signal) has a live
        # loop that cannot be relaunched -- withdraw its pause instead, so the
        # loop never parks; the resume event closes the span for the credit
        # walk, and a loop that already read the signal parks anyway (honest:
        # it lands paused for the next resume to relaunch)
        resumed_count = 0
        withdrawn = [
            descendant
            for _, descendant in self._live_descendants(status='active')
            if descendant.record.signal_get('pause') is not None
        ]
        if self_pausing:
            withdrawn.append(self)
        for node in withdrawn:
            event_id = node.record.event_start('resume')
            node.record.signal_clear('pause')
            node.record.event_end(event_id=event_id, status='completed')
            resumed_count += 1
        # resume parked descendants leaf-first (deepest first), then self --
        # children must be running again before a parent's drain-wait can
        # conclude anything about them; best-effort per node (mirrors kill)
        pending = self._live_descendants(status='paused')
        pending.sort(key=lambda entry: entry[0]['node'].count('.'), reverse=True)
        for _, descendant in pending:
            try:
                descendant._resume()
                resumed_count += 1
            except Exception:
                self.log(
                    message=f'Warning: failed to resume {descendant._root}',
                    level=logging.WARNING,
                )
        if not self.is_user and not self_pausing:
            self._resume()
            resumed_count += 1
        # build confirmation
        if resumed_count == 0:
            return 'No paused nodes to resume.'
        suffix = 's' if resumed_count != 1 else ''
        return (
            f'Resumed {resumed_count} node{suffix}'
            ' (parked loops relaunched leaf-first; live pauses withdrawn)'
        )

    def _resume(self: Node) -> None:
        """Relaunch this node's loop to adopt its paused run.

        The relaunched loop itself withdraws the run's pause signals
        (:meth:`Record.signal_clear` at adoption), so a bare ``--resume``
        launch -- e.g. after a filesystem transplant -- self-clears too.
        """
        # ensure git excludes: the relaunch path skips start's refresh, so a
        # worktree whose info/exclude predates the current block heals here
        # and the adopted run's cleanup and commits see fresh ignores
        self._git_exclude()
        # the resume event lands before the relaunch so the booting loop's
        # deadline credit sees the pause..resume span closed
        event_id = self.record.event_start('resume')
        try:
            self._run_script('resume.sh', f'{self._root}')
        except Exception:
            self.record.event_end(event_id=event_id, status='failed')
            raise
        self.record.event_end(event_id=event_id, status='completed')

    def pause_latched(
        self: Node,
        *,
        tree_only: bool = False,
        skip_self: bool = False,
    ) -> Optional[str]:
        """Return the branch of the nearest paused or pausing node at-or-above.

        The pause latch: a paused subtree admits no new work, so ``init``
        (spawn), ``start``, and a targeted :meth:`resume` refuse -- and a
        booting loop parks -- while any ancestor (or the node itself) is
        ``paused`` or still ``active`` with a pending ``pause`` signal, or
        while the tree-wide latch (a user-node pause) is set. Walks by name
        so a pruned intermediate never hides a paused ancestor.

        Args:
            tree_only: Check only the tree-wide latch, skipping the
                ancestor walk -- the resume-boot variant, where paused
                ancestors are the leaf-first fan-out's normal state but a
                NEW tree-wide brake must still park the boot.
            skip_self: Skip the node itself in the walk -- the resume-verb
                variant, where the target is legally paused but a frozen
                ancestor (or the tree-wide brake) must still refuse the
                relaunch.

        Returns:
            The latching node's branch (the root branch for the tree-wide
            latch), or ``None`` when the path is clear.

        """
        if not tree_only:
            for node in self._self_and_ancestors():
                if node.is_user:
                    continue
                if skip_self and node is self:
                    continue
                status = node.status()
                if status == 'paused':
                    return node.branch
                if status == 'active' and node.record.signal_get('pause') is not None:
                    return node.branch
        if self._tree_latch_file.exists():
            return self.config.get('root')
        return None

    def merge(self: Node, *, continue_merge: bool = False) -> tuple[str, str]:
        """Squash-merge the node's branch into its merge target.

        ``merge.sh`` resolves the target -- the node's configured ``base`` if
        set (e.g. a meta node merging back into the node it optimizes), else
        the dotted parent (the branch minus its last segment) -- runs the
        squash in the target's worktree, and logs the ``merge`` event there so
        the record survives this node's later deletion.

        The full commit history is preserved on the node's
        branch; only a single squash commit lands on the target.

        ``continue_merge`` finishes a hand-resolved squash after a conflicted
        merge: the operator redoes ``git merge --squash`` in the target
        worktree, resolves and stages the conflicts, and the continue then
        runs the merge's own tail -- seed strip, index refresh, commit,
        merge-base advance -- so a manual resolution never has to hand-roll
        those steps (a hand-rolled seed strip leaves working-tree residue).

        Refuses while the target is active or paused -- the squash, index
        refresh, and recovery ``reset --hard`` all mutate the target
        worktree -- except from inside the target's own loop, which merges
        its settled children as part of its normal iteration.

        Returns:
            Tuple of script output and collected stderr notices (e.g. a
            skipped merge-base advance).

        """
        # reject user nodes
        if self.is_user:
            raise RuntimeError('Cannot merge a user node.')
        # reconcile a crashed-but-active node so it can be merged
        self._reconcile_status()
        # validate status
        current = self.status()
        if current == 'active':
            raise RuntimeError('Cannot merge an active node. Stop or kill it first.')
        if current == 'paused':
            raise RuntimeError('Cannot merge a paused node. Resume or kill it first.')
        # the target must be settled too: merge.sh squashes, refreshes
        # indexes, and commits inside the target worktree, and its failure
        # paths reset --hard -- racing a live target loop would absorb or
        # destroy that loop's fresh work; the target's own loop is exempt
        # (a node merging a settled child is single-actor: the loop is
        # blocked on the very agent step running the merge); resolve the
        # target as merge.sh does (base config, else the dotted parent),
        # reconciled so a crashed-but-active target never wedges the merge,
        # and leave a target that does not resolve to merge.sh's own errors
        target_branch = self.config.get('base') or ''
        if not target_branch and '.' in self.branch:
            target_branch, *_ = self.branch.rsplit('.', 1)
        target_worktree = fractal.util.git.find_worktree(self.repo_dir, target_branch)
        if target_worktree is not None:
            target = self.__class__(target_worktree)
            if not target._is_own_loop():
                target._reconcile_status()
                target_status = target.status()
                if target_status == 'active':
                    raise RuntimeError(
                        f'Cannot merge into active target {target_branch}.'
                        ' Stop or kill it first.'
                    )
                if target_status == 'paused':
                    raise RuntimeError(
                        f'Cannot merge into paused target {target_branch}.'
                        ' Resume or kill it first.'
                    )
        # run merge script -- merge.sh resolves the target and logs the
        # merge event on it (it's the single source of truth for the target)
        args = [f'{self._root}']
        if continue_merge:
            args.append('--continue')
        result = self._run_script('merge.sh', *args)
        # success-path warnings ride stderr (e.g. a skipped merge-base
        # advance predicting spurious re-merge diffs) and would vanish with
        # the CompletedProcess -- return them beside the output
        return result.stdout.strip(), result.stderr.strip()

    def guard_delete(self: Node) -> None:
        """Guard a subtree teardown: pre-flight its refusals, settle what it can.

        The refusal slice of :meth:`delete`: node validity, settled statuses
        across the subtree, no locked worktree, and the cwd sitting outside
        every worktree git would remove. Standalone so a chained teardown
        (``merge --delete``) can run it first, landing any refusal before work
        that cannot be undone -- the same pre-flight-then-settle shape
        :meth:`_guarded_teardown` gives the tree-wide tiers.

        Not a dry run: a crashed-but-active node is reconciled before its
        status is tested, because otherwise the guard would refuse a node
        :meth:`delete` removes happily. So a pass reaps orphaned process
        groups and closes their open rows even when it goes on to refuse.

        Raises:
            RuntimeError: If the node, a descendant, a lock, or the cwd
                refuses the teardown.

        """
        # validate node
        if not self.exists():
            raise RuntimeError(
                f'Node at {self.node_dir} was not properly'
                ' initialized and must be deleted manually.'
            )
        # reject user nodes
        if self.is_user:
            raise RuntimeError('Cannot delete a user node.')
        # reconcile a crashed-but-active node so it can be torn down
        self._reconcile_status()
        # validate status -- the node itself must not be running
        if self.status() == 'active':
            raise RuntimeError('Cannot delete an active node. Stop or kill it first.')
        if self.status() == 'paused':
            raise RuntimeError('Cannot delete a paused node. Resume or kill it first.')
        repo_dir = self.repo_dir
        branch = self.branch
        descendant_branches = [row['node'] for row in self.child_list()]
        subtree_branches = [branch, *descendant_branches]
        # refuse if the caller stands inside any worktree in the subtree -- git
        # cannot remove a worktree the caller occupies
        cwd = pathlib.Path.cwd().resolve()
        for subtree_branch in subtree_branches:
            if subtree_branch == branch:
                worktree_dir = self._root.resolve()
            else:
                found = fractal.util.git.find_worktree(repo_dir, subtree_branch)
                worktree_dir = found.resolve() if found else None
            if worktree_dir and (cwd == worktree_dir or worktree_dir in cwd.parents):
                raise RuntimeError(
                    'Cannot delete the current worktree from inside it.'
                    ' Run from the repo root or another worktree.'
                )
        # reconcile crashed descendants so a dead child doesn't wedge the teardown
        for _, descendant in self._live_descendants(status='active'):
            descendant._reconcile_status()
        # refuse if any descendant is still active or paused -- recursive
        # teardown must not yank a running node's worktree out from under it,
        # nor discard a paused one's frozen mid-step work
        for row, _ in self._live_descendants():
            if row['status'] in ('active', 'paused'):
                raise RuntimeError(
                    'Cannot delete a node with an active or paused descendant.'
                    ' Stop, resume, or kill the subtree first.'
                )
        # pre-flight every subtree worktree for the lock delete.sh rejects (a
        # locked worktree can't be removed): recursive teardown is non-atomic, so
        # a lock found mid-tear would strand a half-deleted subtree -- check the
        # whole subtree up front and abort before touching anything
        for subtree_branch in subtree_branches:
            if subtree_branch == branch:
                worktree_dir = self._root
            else:
                found = fractal.util.git.find_worktree(repo_dir, subtree_branch)
                if not found:
                    continue
                worktree_dir = found
            git_dir = fractal.util.git.run(
                ['rev-parse', '--absolute-git-dir'],
                cwd=worktree_dir,
                check=False,
            )
            if git_dir and (pathlib.Path(git_dir) / 'locked').is_file():
                raise RuntimeError(
                    f'Cannot delete: worktree is locked: {worktree_dir}'
                    f' (unlock with: git -C "{repo_dir}"'
                    f' worktree unlock "{worktree_dir}").'
                )

    def delete(self: Node) -> tuple[str, str]:
        """Recursively remove the node and its whole subtree.

        Tears down every descendant too (deepest first), then the node itself:
        each live worktree via ``delete.sh`` (worktree + branch + remote), and
        the subtree's registry rows and subscriptions are cleared from the
        central database -- its history rows (runs, steps, messages, ...)
        persist. Refuses if the node or any descendant is active or paused --
        stop, resume, or kill the subtree first.

        Returns:
            Tuple of per-node script output (deletion order) and collected
            stderr notices (e.g. unmerged-work warnings).

        """
        # every refusal pre-flights before anything is touched (the same
        # gauntlet the chained merge --delete runs before its squash)
        self.guard_delete()
        # collect the subtree: self + every descendant (flat registry);
        # capture branch + repo dir + central db up front -- they resolve
        # through self._root, which is torn down below, so they must be
        # read before any teardown
        branch = self.branch
        repo_dir = self.repo_dir
        db = self.db
        descendant_branches = [row['node'] for row in self.child_list()]
        # tear down descendants deepest first (each live worktree via delete.sh;
        # worktree-less registry rows are deregistered below), then the node
        ordered_branches = sorted(
            descendant_branches,
            key=lambda x: x.count('.'),
            reverse=True,
        )
        # thread the deletion root's surviving merge target (its base config,
        # else its dotted parent -- delete.sh's own fallback) into each
        # descendant's delete.sh: a descendant's self-derived parent dies in
        # this same teardown, so its unmerged warning must name a survivor
        merge_target = self.config.get('base') or ''
        if not merge_target and '.' in branch:
            merge_target, *_ = branch.rsplit('.', 1)
        target_args = [f'--merge-target={merge_target}'] if merge_target else []
        # collect each delete.sh's stdout and stderr notices (e.g. unmerged-work
        # warnings) separately so every node's removal is echoed and warnings
        # ride the caller's stderr, not vanish behind a silent force-delete
        outputs = []
        notices = []
        # serialize the worktree teardown against concurrent inits/teardowns --
        # git worktree remove is not parallel-safe (the .worktrees flock child_add
        # takes around git worktree add)
        with worktree.lock(repo_dir):
            for descendant_branch in ordered_branches:
                worktree_dir = fractal.util.git.find_worktree(
                    repo_dir=repo_dir,
                    branch=descendant_branch,
                )
                if worktree_dir:
                    child = self.__class__(worktree_dir)
                    child_result = child._run_script(
                        'delete.sh',
                        f'{worktree_dir}',
                        *target_args,
                    )
                    child_output = child_result.stdout.strip()
                    if child_output:
                        outputs.append(child_output)
                    notice = child_result.stderr.strip()
                    if notice:
                        notices.append(notice)
                else:
                    # phantom descendant (worktree already gone): delete.sh cannot run,
                    # so prune its branch + project-cache entry directly to avoid a leak
                    worktree.prune_branch(repo_dir, descendant_branch)
            result = self._run_script('delete.sh', f'{self._root}')
        # deregister the whole subtree from the central registry
        self._deregister_subtree(db, repo_dir, branch, descendant_branches)
        # surface delete.sh stderr on success too -- the unmerged-work warning lives
        # there and is otherwise swallowed (only a failure surfaces stderr by default)
        output = result.stdout.strip()
        if output:
            outputs.append(output)
        notice = result.stderr.strip()
        if notice:
            notices.append(notice)
        return '\n'.join(outputs), '\n'.join(notices)

    def deregister(self: Node, name: str) -> str:
        """Deregister an orphaned (worktree-less) node from the registry.

        For a node whose worktree was removed out of band, ``delete`` cannot run
        -- it needs the worktree. ``name`` is the orphan's branch or its bare
        short name (trailing segment), matched against this node's registered
        subtree. This prunes the orphan's branch and project-cache entry (plus
        any descendants the flat registry still lists) and clears the whole
        subtree from the central registry. ``self`` must be an ancestor (e.g.
        the user node) that still lists the orphan.

        Args:
            name: Branch (or bare short name) of the orphaned node to
                deregister.

        Returns:
            Confirmation message.

        Raises:
            LookupError: If ``name`` matches no registered node, or matches
                more than one (the candidates are listed).

        """
        # match the full branch or its bare short name against the registry
        rows = self.child_list() or []
        matches = []
        for row in rows:
            *_, short = row['node'].rsplit('.', 1)
            if row['node'] == name or short == name:
                matches.append(row['node'])
        if not matches:
            raise LookupError(f'No registered node matches {name!r}.')
        if len(matches) > 1:
            options = ', '.join(sorted(matches))
            raise LookupError(
                f'Ambiguous node name {name!r} (matches: {options}).'
                f' Use the full branch.'
            )
        (branch,) = matches
        # alias git root
        repo_dir = self.repo_dir
        # the orphan plus any descendants the flat registry still lists
        descendant_branches = []
        for row in rows:
            if row['node'].startswith(f'{branch}.'):
                descendant_branches.append(row['node'])
        # a live (on-disk) worktree anywhere in the subtree means this is not an
        # orphan prune -- deregister deletes branches and clears rows, so a live
        # descendant would be torn out from under a running node; remove the
        # worktree first (`fractal node delete` without --force does that job)
        for subtree_branch in (branch, *descendant_branches):
            if fractal.util.git.find_worktree(repo_dir, subtree_branch):
                raise RuntimeError(
                    f'{subtree_branch} still has a worktree; remove it first'
                    f' (`fractal node delete {subtree_branch}`).'
                )
        # prune each branch's git branch + project-cache entry, then deregister
        for subtree_branch in (branch, *descendant_branches):
            worktree.prune_branch(repo_dir, subtree_branch)
        self._deregister_subtree(self.db, repo_dir, branch, descendant_branches)
        # a worktree rm-rf'd out of band lingers in git's porcelain as prunable
        # (its branch ref then resists deletion) -- point at the one-shot cleanup
        message = f'Deregistered orphan node {branch}.'
        if fractal.util.git.prunable(repo_dir):
            message += ' Run `git worktree prune` to clear stale worktree metadata.'
        return message

    def reconcile(self: Node) -> list[str]:
        """Record orphaned descendants (worktree removed out of band) as events.

        Cleaning up a node's worktree/branch with plain git instead of
        ``delete`` legitimately leaves its registry rows behind, but nothing
        records the removal (``list`` flags such rows display-only). Logs one
        ``orphan`` event per newly observed orphan, giving out-of-band cleanup
        an audit trail. Registry rows are kept -- ``delete --force``
        (deregister) remains the removal path.

        Returns:
            Branches newly recorded as orphaned.

        """
        # scan the cached registry against one batched worktree probe
        rows = self.child_list() or []
        worktrees = fractal.util.git.worktree_map(self.repo_dir)
        recorded = []
        for row in rows:
            if row['node'] in worktrees:
                continue
            # skip a branch whose orphaning is already on the events log
            where = {'event': 'orphan', 'metadata': row['node']}
            if self.db.read('events', where=where):
                continue
            # log the observation (point-in-time: start plus immediate end)
            event_id = self.record.event_start('orphan', metadata=row['node'])
            self.record.event_end(event_id=event_id, status='completed')
            recorded.append(row['node'])
        return recorded

    def retire(self: Node, reason: Optional[str] = None) -> str:
        """Mark the node as retired.

        Retired nodes are hidden from ``list()`` by default
        and cannot be started. The current status rides the
        retire event (with the reason appended, when given)
        so ``unretire`` can restore it.

        Args:
            reason: Optional reason for retiring.

        Returns:
            Confirmation message.

        """
        # reject user nodes
        if self.is_user:
            raise RuntimeError('Cannot retire a user node.')
        # reconcile a crashed-but-active node so it can be retired
        self._reconcile_status()
        # the guard re-read and the retired flip stay atomic under the
        # .worktrees flock, so a rival verb cannot land between them;
        # retire.sh runs outside the lock
        with worktree.lock(self.repo_dir):
            # re-read under the lock -- a rival verb that won the race has
            # already moved this node's status
            if self.status() == 'active':
                raise RuntimeError(
                    'Cannot retire an active node. Stop or kill it first.'
                )
            if self.status() == 'paused':
                raise RuntimeError(
                    'Cannot retire a paused node. Resume or kill it first.'
                )
            # set status and log event -- the pre-retire status rides the
            # event metadata (ahead of any ': <reason>' suffix) so unretire
            # can restore it instead of dropping it
            prior = self.status()
            metadata = f'{prior}: {reason}' if reason else prior
            event_id = self.record.event_start('retire', metadata=metadata)
            self.status_set('retired')
        self._run_script('retire.sh', f'{self._root}')
        self.record.event_end(event_id=event_id, status='completed')
        return 'Node retired'

    def unretire(self: Node) -> str:
        """Remove retired status from the node.

        Restores the status the node held before it was retired (recorded
        on the retire event); when no retire event recorded one (e.g. a
        ``.status`` file set by hand) the node resets to ``idle``.

        An ``idle`` restore re-enters the unsettled pool, so it re-checks
        the width/descendant gates (:meth:`_enforce_rearm_limits`) under
        the ``.worktrees`` flock; a settled restore holds no slot and
        passes ungated.

        Returns:
            Confirmation message.

        """
        # reject user nodes
        if self.is_user:
            raise RuntimeError('Cannot unretire a user node.')
        # validate status
        if self.status() != 'retired':
            raise RuntimeError('Cannot unretire: node is not retired.')
        # an idle restore re-enters the unsettled pool, so the gate re-check
        # and the flip stay atomic under the .worktrees flock (init's
        # check+register atomicity); a settled restore holds no slot
        with worktree.lock(self.repo_dir):
            # re-read under the lock -- a concurrent unretire that won the
            # race has already restored this node out of retired
            if self.status() != 'retired':
                raise RuntimeError('Cannot unretire: node is not retired.')
            # restore the pre-retire status from the latest retire event --
            # resolved under the lock too, so a rival unretire -> re-retire
            # cycle cannot hand this caller a stale prior; fall back to idle
            # when nothing usable was recorded (a re-retired node stored
            # 'retired'; 'active' can only be a stale hand-edit)
            rows = self.db.read(
                'events',
                where={'node': self.branch, 'event': 'retire'},
                limit=1,
            )
            prior = rows[0]['metadata'] if rows else ''
            # the prior rides ahead of any ': <reason>' suffix retire added
            prior, *_ = prior.split(': ', 1)
            if prior not in STATUSES or prior in ('active', 'retired'):
                prior = 'idle'
            if prior == 'idle':
                self._enforce_rearm_limits()
            # set status and log event
            event_id = self.record.event_start('unretire')
            self.status_set(prior)
        self._run_script('unretire.sh', f'{self._root}')
        self.record.event_end(event_id=event_id, status='completed')
        return 'Node unretired'

    @staticmethod
    def destroy(path: PathLike, *, name: Optional[str] = None) -> str:
        """Destroy one fractal tree, or the whole fractal when ``name`` is ``None``.

        Whole-fractal mode is the full inverse of ``fractal init``: tears
        down every node worktree and local branch, removes ``.worktrees/``,
        deletes every user node's data directory, and strips fractal's block
        from the repo's ``info/exclude``. Tree mode is the same teardown
        scoped to one tree -- only its node worktrees, branches, and data
        directory go, while sibling trees and the shared ``.worktrees/``
        plumbing survive; the block is stripped only when no tree remains.
        Committed artifacts (the project wiki, baseline commits), remote
        branches, and the tree's own root branch are left in place. Refuses
        while any in-scope node's tmux session is alive; paused nodes are
        killed as part of the teardown -- the caller's confirmation
        authorized discarding the frozen mid-step work their parked
        worktrees hold.

        Args:
            path: Any path inside the repo.
            name: Root branch of the tree to destroy (``None`` destroys the
                whole fractal).

        Returns:
            Script output.

        """
        if name is None:
            # every tree is in scope, so every tree anchors the teardown: a
            # single anchor would pre-flight, settle, and prune one tree's
            # nodes while the script tore down all of them
            trees = Node.user_nodes(path)
            # a repo with no fractal still runs the script (its no-op report)
            node = trees[0] if trees else Node(path)
        else:
            # anchor the named tree explicitly, never by inference -- a scoped
            # teardown keyed to the wrong tree's DB would guard and prune a
            # healthy sibling
            node = Node.resolve_user(path, name=name)
            if node is None:
                raise RuntimeError(f'No tree found on branch {name!r}.')
            trees = [node]
        repo_dir = node.repo_dir
        # snapshot the registry before the teardown: the script removes the
        # central DB with each user node's data dir, so the phantom prune
        # below must read the branch list while it still exists
        registry = [
            row for tree in trees if tree.exists() for row in tree.db.read('nodes')
        ]
        # refuse if the caller stands inside an in-scope node worktree -- git
        # cannot remove a worktree the caller occupies
        cwd = pathlib.Path.cwd().resolve()
        for branch, worktree_path in fractal.util.git.worktree_map(repo_dir).items():
            worktree_dir = worktree_path.resolve()
            if worktree_dir == repo_dir.resolve():
                continue
            if name is not None and not branch.startswith(f'{name}.'):
                continue
            if cwd == worktree_dir or worktree_dir in cwd.parents:
                raise RuntimeError(
                    'Cannot destroy the current worktree from inside it.'
                    ' Run from the repo root.'
                )
        # reconcile crashed nodes so their orphaned process groups reap while
        # the .pgid records still exist (the script removes them with the
        # worktrees; a headless agent would otherwise keep spending unseen)
        for tree in trees:
            if not tree.exists():
                continue
            for _, descendant in tree._live_descendants(status='active'):
                descendant._reconcile_status()
        args = [f'--branch={node.branch}']
        if name is None:
            args.append('--all')
            # the sweep clears every tree's data dir, so name them here -- the
            # 'user' marker identifying a root lives in each node's config
            args += [
                f'--node-dir={tree.node_dir.relative_to(repo_dir)}' for tree in trees
            ]
        result = Node._guarded_teardown(node, trees, 'destroy.sh', repo_dir, *args)
        # prune every snapshot branch: a no-op for the worktrees the script
        # tore down, and the cleanup destroy.sh cannot do for a phantom (its
        # worktree rm -rf'd out of band) -- a stale branch would resurrect
        # old history under a later re-init of the name
        for branch in sorted({row['node'] for row in registry}):
            worktree.prune_branch(repo_dir, branch)
        # strip fractal's block from the shared info/exclude (the inverse of
        # exclude_update: same whole-line markers, all other content
        # preserved) -- kept while any sibling tree survives
        if not Node.user_nodes(repo_dir):
            worktree.exclude_strip(repo_dir)
        return result.stdout.strip()

    @staticmethod
    def reset(path: PathLike, *, name: Optional[str] = None) -> str:
        """Remove one tree's node worktrees, keeping the project and its history.

        The middle rung between ``delete`` (one subtree) and ``destroy`` (the
        tree, or the whole fractal): tears down the tree's node worktrees and
        local branches and clears its node registry, while the user node's
        data -- config, memory, and the central database with every history
        row -- plus the wiki and baseline commits survive, so fresh nodes
        spawn immediately after. Sibling trees are untouched. Refuses while
        any of the tree's tmux sessions is alive; paused nodes are killed as
        part of the teardown -- the caller's confirmation authorized
        discarding the frozen mid-step work their parked worktrees hold.

        Args:
            path: Any path inside the repo.
            name: Root branch of the tree to reset; ``None`` infers it from
                the caller's branch.

        Returns:
            Script output.

        """
        # anchor on the user node by config, not the checkout: on a non-init
        # branch a bare Node(path) reads uninitialized, skipping the registry
        # snapshot (orphaning its rows), the reconcile, and the latch cleanup
        node = Node.resolve_user(path, name=name)
        if node is None:
            if name is not None:
                raise RuntimeError(f'No tree found on branch {name!r}.')
            node = Node(path)
        repo_dir = node.repo_dir
        # snapshot the registry before the teardown: the deregistration below
        # must sweep exactly the rows that predate the script, never a node a
        # concurrent init registers afterward
        registry = node.db.read('nodes') if node.exists() else []
        # refuse if the caller stands inside one of the tree's node worktrees
        # -- git cannot remove a worktree the caller occupies
        cwd = pathlib.Path.cwd().resolve()
        for branch, worktree_path in fractal.util.git.worktree_map(repo_dir).items():
            worktree_dir = worktree_path.resolve()
            if worktree_dir == repo_dir.resolve():
                continue
            if not branch.startswith(f'{node.branch}.'):
                continue
            if cwd == worktree_dir or worktree_dir in cwd.parents:
                raise RuntimeError(
                    'Cannot reset the current worktree from inside it.'
                    ' Run from the repo root.'
                )
        # reconcile crashed nodes so their open history rows close before
        # their worktrees vanish (the rows persist; the worktrees do not)
        if node.exists():
            for _, descendant in node._live_descendants(status='active'):
                descendant._reconcile_status()
        result = Node._guarded_teardown(
            node,
            [node],
            'reset.sh',
            repo_dir,
            f'--branch={node.branch}',
        )
        # prune every snapshot branch: a no-op for the worktrees the script
        # tore down, and the cleanup delete.sh cannot do for a phantom (its
        # worktree rm -rf'd out of band) -- a stale branch or .project entry
        # would resurrect old history under a later re-init of the name
        branches = sorted({row['node'] for row in registry})
        for branch in branches:
            worktree.prune_branch(repo_dir, branch)
        # clear the snapshot's registry rows and subscriptions, grouped into
        # maximal subtrees so a delete event lands on the user node's log per
        # subtree; history rows persist
        roots = [
            branch
            for branch in branches
            if not any(branch.startswith(f'{other}.') for other in branches)
        ]
        for root_branch in roots:
            descendant_branches = [
                branch for branch in branches if branch.startswith(f'{root_branch}.')
            ]
            Node._deregister_subtree(
                db=node.db,
                repo_dir=repo_dir,
                branch=root_branch,
                descendant_branches=descendant_branches,
            )
        # a tree-wide pause latch would outlive the (now gone) nodes it froze
        # and refuse every future init/start -- the brake goes with the tree
        # it applied to
        if node.exists():
            node._tree_latch_file.unlink(missing_ok=True)
        return result.stdout.strip()

    @staticmethod
    def _guarded_teardown(
        node: Node,
        trees: list[Node],
        script: str,
        path: pathlib.Path,
        *args: str,
    ) -> subprocess.CompletedProcess[str]:
        """Run a tree-teardown script behind the pre-flight, settle, and flock.

        The shared ``destroy``/``reset`` shape. Pre-flights the script's
        own refusals first -- a live tmux session or a locked worktree
        anywhere in the trees in scope -- because the paused settle below is
        irreversible (the kills close the parked runs), so a teardown the
        script would refuse must abort here with nothing touched. Then
        settles frozen work: a paused node has no session for the
        liveness refusal to catch, and the confirmed teardown already
        authorized discarding its frozen mid-step work, so each parked
        node is killed -- pure bookkeeping with no loop alive: the open
        rows close ``killed`` and the attribution names the verb --
        rather than bouncing the operator through a manual kill sweep.
        Then runs the script under the ``.worktrees`` flock so its
        worktree remove/prune does not race a concurrent init/delete (the
        same lock ``child_add`` takes) -- but only when ``.worktrees``
        exists; creating it would defeat the script's nothing-to-tear-down
        check, which keys off that directory. The script re-checks every
        guard under the lock, backstopping a session, lock, or pause that
        lands after the pre-flight.

        Args:
            node: The repo-root node handle -- runs the script and owns the
                flock.
            trees: The user nodes whose subtrees the teardown covers; each is
                pre-flighted and settled before the script runs, so a
                repo-wide sweep never guards one tree and tears down another.
            script: The teardown script (``destroy.sh``/``reset.sh``); its
                stem names the verb in the kill attribution.
            path: Git repository root, passed through to the script.
            *args: Extra script arguments, passed through after the path.

        Returns:
            Completed process result.

        """
        verb, *_ = script.split('.')
        for tree in trees:
            if not tree.exists():
                continue
            # pre-flight the script's refusals before the irreversible settle
            # (the user node runs no loop, so only descendants can hold a
            # session): one pass per guard, mirroring the script's ordering
            descendants = tree._live_descendants()
            # probe each node's recorded socket, never the ambient one alone:
            # a session alive on the socket the loop recorded at boot is
            # invisible to a shell resolving a different server (see
            # _tmux_session_exists); an inconclusive probe means the node may
            # still be running, so the irreversible teardown refuses rather
            # than tearing down blind
            for _, descendant in descendants:
                alive = descendant._tmux_session_exists()
                if alive is None:
                    raise RuntimeError(
                        f'Cannot {verb}: the tmux probe failed (tmux list-sessions'
                        ' gave no answer), so nodes may still be running. Restore'
                        ' tmux visibility and retry.'
                    )
                if alive:
                    raise RuntimeError(
                        f'Cannot {verb}: node is still running in tmux'
                        f' ({descendant.tmux_session}). Kill it first with:'
                        f' fractal node kill {descendant.branch}.'
                    )
            for _, descendant in descendants:
                git_dir = fractal.util.git.run(
                    ['rev-parse', '--absolute-git-dir'],
                    cwd=descendant._root,
                    check=False,
                )
                if git_dir and (pathlib.Path(git_dir) / 'locked').is_file():
                    raise RuntimeError(
                        f'Cannot {verb}: worktree is locked: {descendant._root}'
                        f' (unlock with: git -C "{tree.repo_dir}"'
                        f' worktree unlock "{descendant._root}").'
                    )
        # settle frozen work once every tree passed its pre-flight: kill each
        # parked node so its open rows close -- before the lock (each kill
        # takes the same flock); best-effort per node (mirrors kill's sweep),
        # the script's paused re-check backstops
        for tree in trees:
            if not tree.exists():
                continue
            for _, descendant in tree._live_descendants(status='paused'):
                try:
                    descendant._kill(f'{verb} teardown', fan_out=True)
                except Exception:
                    tree.log(
                        message=f'Warning: failed to kill {descendant._root}',
                        level=logging.WARNING,
                    )
        # run the script under the flock, only when .worktrees exists
        worktrees = node.repo_dir / WORKTREES_FOLDER
        if worktrees.is_dir():
            with worktree.lock(node.repo_dir):
                return node._run_script(script, f'{path}', *args)
        return node._run_script(script, f'{path}', *args)

    def status(self: Node) -> str:
        """Return the node's current status.

        Reads the node's ``.status`` file -- lifecycle state is kept
        out of ``config.json`` so config is purely user settings.

        Returns:
            Status string (``idle`` when no ``.status`` file exists).

        """
        status_file = self._status_file
        if status_file.exists():
            return status_file.read_text(encoding='utf-8').strip()
        return 'idle'

    def status_detail(self: Node) -> str:
        """Return the qualifier that refines the node's status, if any.

        ``pausing`` / ``stopping`` / ``finishing`` when a pause/stop/finish
        signal is pending on an active node; the recorded end reason when
        the latest run row says why an ``exited`` run ended (the run row is
        the single source -- a reconcile-healed crash records no reason);
        else empty. An unresolved model drop on the newest iteration
        composes a ``model drop`` marker onto whichever qualifier stands
        (any status -- served-model honesty outlives the run). The
        qualifier never enters the stored status, which stays bare -- but
        a crashed-but-active node is reconciled (persisted) first: reads
        are where staleness is observed, and the probe is a no-op unless
        the stored status is ``active``.

        Returns:
            The qualifier, or an empty string when the status stands alone.

        """
        self._reconcile_status()
        status = self.status()
        detail = ''
        if status == 'active':
            if self.record.signal_get('pause') is not None:
                detail = 'pausing'
            elif self.record.signal_get('stop') is not None:
                detail = 'stopping'
            elif self.record.signal_get('finish') is not None:
                detail = 'finishing'
        if status == 'exited':
            # the latest run row records why the loop ended (budget landing,
            # timeout, setup abort); a crash healed by reconcile closes its
            # rows reason-less and keeps the bare status
            rows = self.record.runs(limit=1)
            if rows and rows[0]['status'] == 'exited' and rows[0]['metadata']:
                detail = rows[0]['metadata']
        if status == 'completed':
            # the completed landings are different facts: an exhausted
            # iteration budget records its cap, while a goal-met finish
            # leaves the run reason-less (or carries a cap-overshoot note,
            # which is still done-conditions-met) -- name the run-out and a
            # dead final iteration so a census never reads either as a clean
            # done-conditions-met end
            rows = self.record.runs(limit=1)
            if rows and rows[0]['status'] == 'completed':
                reason = rows[0]['metadata'] or ''
                if reason.startswith('Reached max iterations'):
                    detail = f'run exhausted: {reason}'
                elif reason.endswith('final iteration failed'):
                    detail = reason
        # an unresolved model drop composes onto the qualifier (the metadata
        # append shape), so neither fact hides the other
        if self._model_dropped():
            detail = f'{detail}; model drop' if detail else 'model drop'
        # an iteration-number gap composes the same way: numbers that
        # advanced with no recorded row are iterations that never executed
        if gap := self._iteration_gap():
            detail = f'{detail}; {gap}' if detail else gap
        # the billing breaker is the loudest fact on the row while it holds
        if self._billing_backoff():
            detail = f'{detail}; PAUSED: billing' if detail else 'PAUSED: billing'
        return detail

    def _billing_backoff(self: Node) -> bool:
        """Return whether the newest launches carry the billing signature.

        Three or more consecutive newest closed launches, each failed,
        instant, and zero-cost -- the loop is backing off dead credits
        (its own breaker uses the same signature), so the census must say
        so loudly rather than render an idle-looking active row. A
        cannot-exec launch (recorded ``agent launch failed``) is the class
        the loop's breaker refuses to arm on, so it disqualifies the
        streak here too -- a broken agent install must never steer the
        operator at a credit refill. Active nodes only: a settled node's
        trailing failures are history.
        """
        if self.status() != 'active':
            return False
        runs = self.record.runs(limit=1)
        if not runs:
            return False
        streak = 0
        for row in self.record.steps(run_id=runs[0]['run_id']):
            # bookkeeping rows are not launches: the never-run tail booked
            # after a failure, and any still-open row
            if row['ended_at'] is None or (
                row['status'] == 'stopped' and row['metadata'].startswith('failed on')
            ):
                continue
            # a cannot-exec launch books failed/instant with no cost, but is
            # not billing-shaped -- the recorded reason (retry-marker safe:
            # the marker is a suffix) breaks the streak like a paid failure
            if row['metadata'].startswith('agent launch failed'):
                return False
            if row['status'] != 'failed' or row['cost'] not in (None, 0.0):
                return False
            elapsed = fractal.util.time.elapsed(row['started_at'])
            elapsed -= fractal.util.time.elapsed(row['ended_at'])
            if elapsed >= 10:
                return False
            streak += 1
            if streak >= 3:
                return True
        return False

    def _iteration_gap(self: Node) -> str:
        """Return the latest run's iteration-gap label, or ``''``.

        Recorded iteration rows are the execution trace: numbers that jump
        (2.18 recorded, then 2.23) mean scheduled iterations were consumed
        with zero execution -- cadence-keyed law and budget arithmetic skew
        silently unless the gap is flagged. Reads the latest run alone and
        names its newest gap.
        """
        runs = self.record.runs(limit=1)
        if not runs:
            return ''
        run_id = runs[0]['run_id']
        iters = self.record.iters(run_id=run_id)
        numbers = sorted({row['iter'] for row in iters}, reverse=True)
        for newer, older in itertools.pairwise(numbers):
            if newer != older + 1:
                first, last = older + 1, newer - 1
                span = f'{run_id}.{first}'
                if last != first:
                    span += f'-{run_id}.{last}'
                return f'iteration gap {span}'
        return ''

    def _model_dropped(self: Node) -> bool:
        """Return whether the newest iteration carries an unresolved model drop.

        The loop marks every completed attempt served off its pin on that
        attempt's own row, so a drop stands unresolved exactly while a
        step's *newest completed* attempt carries the marker: a clean
        re-dispatch supersedes it, while one that failed, timed out, or
        was abandoned (stop, ceiling, a parked backoff resume never
        re-entered) leaves it standing. The read is the newest iteration
        alone, so a later iteration supersedes the marker.
        """
        iters = self.record.iters(limit=1)
        if not iters:
            return False
        steps = self.record.steps(iter_id=iters[0]['iter_id'])
        # newest completed attempt per step, rows newest-first (SYNC rows
        # carry the awaited step's number, so the name joins the key)
        newest: dict[tuple, dict] = {}
        for row in steps:
            key = (row['step'], row['step_name'])
            if row['status'] == 'completed' and key not in newest:
                newest[key] = row
        return any('model drop' in row['metadata'] for row in newest.values())

    def status_display(self: Node) -> str:
        """Return the status decorated with a pending signal or end reason.

        ``active (pausing)`` / ``active (stopping)`` / ``active (finishing)``
        / ``exited (<reason>)`` -- :meth:`status_detail`'s qualifier in
        parentheses -- else the bare status. The composed form is for
        single-node human surfaces; the tabular listing carries the two as
        separate ``status`` and ``detail`` columns so machine consumers
        never parse the parentheses back apart.

        Returns:
            Status string, possibly with a pending-signal or end-reason
            suffix.

        """
        # status_detail reconciles first, so the status read here is the
        # healed one the qualifier was derived from
        detail = self.status_detail()
        status = self.status()
        return f'{status} ({detail})' if detail else status

    def status_set(self: Node, status: str) -> None:
        """Set the node's status.

        Validates against the set of known status values,
        writes the node's ``.status`` file, and updates the
        node's row in the central ``nodes`` registry.

        Args:
            status: Status value to set.

        """
        # validate status
        if status not in STATUSES:
            raise _invalid_status(status)
        # write the status file atomically, so a concurrent status() never
        # reads a torn value
        fractal.util.filesystem.write_atomic(self._status_file, status + '\n')
        # update the node's registry row (the user node has none -- no-op)
        self.db.update({'status': status}, 'nodes', where={'node': self.branch})

    def title_set(self: Node, title: str) -> None:
        """Set the node's human-readable title.

        The display label consumers show in place of the branch slug. It lives
        in both the node's ``config.json`` and its central registry row (init
        seeds them together), so both are written -- a registry-only update
        would leave the config stale and the two out of sync. On the user
        node, which has no registry row, the config write is the whole effect.

        Args:
            title: The display name to store.

        """
        # write the node's config.json first (the failure-prone step), then
        # the nodes table, so a config-write failure can't desync the two
        self.config.set('title', title)
        self.db.update({'title': title}, 'nodes', where={'node': self.branch})

    def list(
        self: Node,
        *,
        all_nodes: bool = False,
        retired_only: bool = False,
        max_depth: Optional[int] = None,
        status: Optional[str] = None,
        live: bool = False,
        decorated: bool = False,
    ) -> list[dict]:
        """List registered child nodes.

        Queries the ``nodes`` table with optional depth and
        status filters. A crashed-but-active row (worktree present, no live
        tmux session) is reconciled -- persisted via
        :meth:`_reconcile_status`, not just relabeled -- before listing, so
        the fleet's default steering read never echoes a dead loop as
        ``active``. Cap columns render each present child's live config
        values; a gone worktree keeps the registry caps. ``spend`` reads
        against the ``max_cost`` beside it at the same scope the cap is
        enforced at -- the current run's subtree spend -- and is blank for
        a node with no recorded runs. ``status`` is always bare, with any
        qualifier (a pending signal, an ``exited`` run's end reason, an
        ``orphaned`` flag, a ``model drop`` marker, an ``iteration gap``)
        in ``detail``. The
        ``last`` column renders each
        row's newest activity instant as a compact age, flagged (``12m!``)
        when an active node has sat quiet past ``max(step_timeout, 5m)``.

        Args:
            all_nodes: Include retired nodes in output.
            retired_only: Show only retired nodes.
            max_depth: Maximum depth relative to this node.
            status: Filter to a status, or several comma-separated
                (overrides the retired/all default).
            live: Reconcile each row against the child's real
                ``.status()``, dropping descendants whose worktree is gone,
                relabeling a crashed ``active`` node (no live tmux session)
                to ``exited``, and a booting ``idle`` node (live session,
                the loop not yet stamped) to ``active`` (the authoritative
                view). Read-only -- it does not persist the relabel.
            decorated: Record each descendant's status qualifier (a
                pending signal, an exited run's end reason, a model-drop
                marker) in its ``detail``; display-only, gated off for hot
                paths such as ``--count``.

        Returns:
            List of child node records.

        """
        # read children -- authoritative (live) or the cached registry
        if live:
            # _live_descendants reconciles each row to the child's real status and
            # drops gone worktrees; additionally relabel a crashed-but-active node
            # ('active' with no live tmux session) to 'exited' and a booting idle
            # node (live session) to 'active' -- display-only, mirroring the TUI
            # snapshot reconcile, so --live is the authoritative
            # settled-vs-crashed view; the batched probe only nominates
            # candidates, and each relabel confirms on the node's recorded
            # socket (a tmux answer is evidence about one server only -- see
            # _tmux_session_exists); an inconclusive probe (no tmux answer)
            # proves nothing, so the stored status stands
            sessions = fractal.util.tmux.probe()
            rows = []
            for row, node in self._live_descendants(max_depth=max_depth):
                if sessions is not None:
                    if _base_status(row.get('status')) == 'active':
                        unlisted = node.tmux_session not in sessions
                        if unlisted and node._tmux_session_exists() is False:
                            row = {**row, 'status': 'exited'}
                    # a started child holds 'idle' until its loop stamps
                    # 'active' after preflight, but its session is already
                    # live -- read the boot window as 'active', so a finishing
                    # ancestor's drain never completes over a child started
                    # seconds earlier; a sessionless idle node (spawned, never
                    # started) stays idle and never blocks a drain
                    elif _base_status(row.get('status')) == 'idle':
                        if node.tmux_session in sessions or node._tmux_session_exists():
                            row = {**row, 'status': 'active'}
                rows.append(row)
        else:
            rows = self.child_list(max_depth=max_depth)
            if rows is None:
                return []
            worktrees = fractal.util.git.worktree_map(self.repo_dir)
            # heal a crashed-but-active row by persisting the child's
            # reconcile -- the plain list is the fleet's default steering read
            pairs = []
            for row in rows:
                node = None
                if row['node'] in worktrees:
                    node = self.__class__(worktrees[row['node']])
                pairs.append((row, node))
            rows = [row for row, _ in self._heal_crashed(pairs)]
            # flag a registry row whose worktree is gone (removed out of band)
            # rather than reporting a vanished node as healthy: a settled/kept
            # status stays itself and takes an 'orphaned' detail, while a
            # live-ish row turns 'orphan' outright -- artifacts vanishing
            # mid-life is an anomaly, not cleanup
            settled = ('completed', 'stopped', 'exited', 'killed', 'retired')
            flagged = []
            for row in rows:
                if row['node'] not in worktrees:
                    stored = _base_status(row.get('status'))
                    if stored in settled:
                        row = {**row, 'status': stored, 'detail': 'orphaned'}
                    else:
                        row = {**row, 'status': 'orphan'}
                flagged.append(row)
            rows = flagged
        # one fleet-wide read of each node's newest activity instant backs
        # the 'last' column merged in the overlay pass below
        recent = {
            entry['node']: entry['timestamp']
            for entry in self.db.read(
                query='SELECT node, MAX(timestamp) AS timestamp'
                ' FROM activity GROUP BY node',
            )
        }
        # overlay each present child's config caps (display-only): a
        # post-spawn config edit (the rescue top-up) is live enforcement
        # truth the registry row's spawn-time values miss; a gone worktree
        # keeps the registry caps (the only surviving source)
        worktrees = fractal.util.git.worktree_map(self.repo_dir)
        capped = []
        for row in rows:
            worktree_dir = worktrees.get(row['node'])
            node = self.__class__(worktree_dir) if worktree_dir else None
            if node is not None and not node.exists():
                node = None
            # render the newest activity instant as the 'last' age; an
            # active node quiet past max(step_timeout, 5m) is flagged stale
            # ('12m!'), step_timeout from the child's live config (a gone
            # worktree falls back to the floor alone)
            last = None
            timestamp = recent.get(row['node'])
            if timestamp is not None:
                age = fractal.util.time.elapsed(timestamp)
                last = fractal.util.format_age(age)
                if _base_status(row.get('status')) == 'active':
                    step_timeout = None
                    if node is not None:
                        step_timeout = fractal.util.parse_duration_seconds(
                            node.config.get('step_timeout') or ''
                        )
                    if age > max(step_timeout or 0.0, _STALE_AGE_FLOOR_SECONDS):
                        last += '!'
            # a gone worktree keeps its registry caps -- the only surviving
            # source -- and yields no spend: the live config and the ledger
            # are both reached through the node
            drifted = {}
            spend = None
            if node is not None:
                caps = ('max_cost', 'max_depth', 'max_children', 'max_descendants')
                for key in caps:
                    config_value = node.config.get(key)
                    if config_value is not None and config_value != row[key]:
                        drifted[key] = config_value
                # spend reads against the max_cost beside it -- the same scope
                # the cap is enforced at: the current run's subtree spend
                # (active run, else the most recent); resolving the run here
                # rather than inside Cost.spent keeps a never-run node blank
                # instead of reporting its 0.0 as a real reading
                *_, run_id = node.record.resolve_context()
                if run_id is not None:
                    spend = round(node.cost.spent(run_id=run_id), _SPEND_PRECISION)
            # 'detail' leads the merge so an orphan flag set above wins it,
            # and every row carries the key either way
            row = {'detail': None, **row, **drifted, 'spend': spend, 'last': last}
            capped.append(row)
        rows = capped
        # record each active descendant's pending stop/finish signal (and each
        # exited one's end reason) in 'detail'; 'status' stays bare, so the
        # filters below select on the column itself
        if decorated:
            worktrees = fractal.util.git.worktree_map(self.repo_dir)
            rows = [self._detail_status(row, worktrees) for row in rows]
        # filter by an explicit status (one or comma-several), else apply
        # the retired/all default
        if status is not None:
            wanted = {chunk.strip() for chunk in status.split(',') if chunk.strip()}
            rows = [row for row in rows if _base_status(row.get('status')) in wanted]
        elif retired_only:
            rows = [row for row in rows if _base_status(row.get('status')) == 'retired']
        elif not all_nodes:
            rows = [row for row in rows if _base_status(row.get('status')) != 'retired']
        return rows

    def _detail_status(
        self: Node,
        row: dict,
        worktrees: dict[str, pathlib.Path],
    ) -> dict:
        """Fill a descendant's ``detail`` with its status qualifier.

        Display helper for ``list``: for a descendant whose own stored
        status still matches the row's, records its :meth:`status_detail`
        (``pausing`` / ``stopping`` / ``finishing`` / the run's end reason
        / the ``model drop`` marker -- which any status can carry, so no
        stored-status gate scopes the consult) in the row's ``detail``.
        The row's ``status`` stays bare. A diverged row (a stale registry
        value, or ``--live``'s display-only relabel of a crashed ``active``
        node) is left alone without consulting ``status_detail``, whose
        reconcile would otherwise persist a heal from a read-only listing;
        the status is re-read after that reconcile, since a heal that moved
        the node off the row's status makes the qualifier stale.
        ``worktrees`` is a branch->path map (one ``git worktree list``) so
        the listing resolves worktrees without a subprocess per row.
        Best-effort -- a row whose worktree is gone keeps its cached status
        and an empty detail.
        """
        stored = _base_status(row.get('status'))
        worktree_dir = worktrees.get(row['node'])
        if worktree_dir:
            node = self.__class__(worktree_dir)
            if node.exists() and node.status() == stored:
                detail = node.status_detail()
                if detail and node.status() == stored:
                    return {**row, 'detail': detail}
        return row

    def _heal_crashed(
        self: Node,
        pairs: list[tuple[dict, Optional[Node]]],
    ) -> list[tuple[dict, Optional[Node]]]:
        """Persist the reconcile for crashed-but-active rows (the shared pass).

        Reads are where staleness is observed: a row still ``active`` with no
        live tmux session is healed through the child's own
        :meth:`_reconcile_status` (persisted, not just relabeled) and re-read.
        The tmux probe is paid only while something reads ``active`` (one
        batched probe for the whole set); a row without a live node (worktree
        gone) passes through untouched. An inconclusive probe (no answer from
        tmux) heals nothing -- stamping live loops ``exited`` on a blind host
        would reap them.

        Args:
            pairs: ``(row, node)`` per registry row -- ``node`` is ``None``
                when the row's worktree is gone.

        Returns:
            The pairs, each crashed-active row's status healed.

        """
        if not any(_base_status(row.get('status')) == 'active' for row, _ in pairs):
            return pairs
        sessions = fractal.util.tmux.probe()
        if sessions is None:
            return pairs
        healed = []
        for row, node in pairs:
            if _base_status(row.get('status')) == 'active' and node is not None:
                if node.exists() and node.tmux_session not in sessions:
                    node._reconcile_status()
                    row = {**row, 'status': node.status()}
            healed.append((row, node))
        return healed

    def _count_unsettled(
        self: Node,
        *,
        max_depth: Optional[int] = None,
    ) -> int:
        """Count slot-holding descendants: active, paused, or idle awaiting start.

        The width/descendant gates bind on UNSETTLED nodes only -- a settled
        (completed/stopped/exited/killed) or retired node frees its slot
        automatically, so the caps bound concurrency, not lifetime spawn
        count. Crashed-but-active descendants are healed first (persisted
        via :meth:`_reconcile_status`) so a dead loop's phantom ``active``
        cannot wedge the gate; the tmux probe is paid only while something
        reads ``active`` (one batched probe for the whole set).

        Args:
            max_depth: Maximum depth relative to this node.

        Returns:
            The number of unsettled live descendants.

        """
        live = self._live_descendants(max_depth=max_depth)
        # heal crashed-but-active rows before counting (persisting the settle)
        live = self._heal_crashed(live)
        # statuses that hold a spawn slot: active, paused mid-work (it will
        # return), or idle awaiting start
        unsettled = ('active', 'paused', 'idle')
        return sum(1 for row, _ in live if row['status'] in unsettled)

    def _enforce_spawn_limits(
        self: Node,
        *,
        child_max_cost: Optional[float],
    ) -> None:
        """Reject a child spawn that would exceed a live subtree or budget cap.

        ``self`` is the parent. Checks the caps that depend on live state
        (:meth:`_check_caps`) -- the parent's ``max_children`` (width), every
        ancestor's ``max_depth`` and ``max_descendants`` (subtree), and the
        child's ``max_cost`` against the parent's remaining run budget.

        Args:
            child_max_cost: The child's requested ``--max-cost`` (USD), or
                ``None``.

        Raises:
            ValueError: If any live subtree or budget cap would be exceeded.

        """
        self._check_caps(depth=self.branch.count('.') + 1, budget=child_max_cost)

    def _enforce_rearm_limits(self: Node) -> None:
        """Reject a re-arm that would exceed a live width or descendant cap.

        ``self`` is the node about to re-arm to ``idle``. A re-arm returns
        one unsettled node to the tree exactly as a spawn adds one, so it
        re-checks the two concurrency caps the spawn gate enforces
        (:meth:`_check_caps`) -- the parent's ``max_children`` (width) and
        every ancestor's ``max_descendants`` (subtree); out of the pool
        here, this node holds no slot, so spawn-to-cap -> settle -> respawn
        -> re-arm would otherwise land the subtree over cap. ``max_depth``
        is structural (the node already sits at its spawn-time depth) and
        the budget bound is spawn-time only (every run re-arms the node's
        own ``max_cost``), so neither re-runs here. Called by :meth:`start`
        (continue) and :meth:`unretire` (idle restore); like the spawn
        gate, there is no override flag.

        Raises:
            ValueError: If a width or descendant cap would be exceeded.

        """
        self._check_caps(depth=None, budget=None)

    def _check_caps(
        self: Node,
        *,
        depth: Optional[int],
        budget: Optional[float],
    ) -> None:
        """Reject adding one unsettled node over a live width/subtree/budget cap.

        The shared gate behind :meth:`_enforce_spawn_limits` and
        :meth:`_enforce_rearm_limits`. The width and descendant counts bind
        on unsettled nodes only (:meth:`_count_unsettled`), bounding
        concurrency, not lifetime spawn count; every ancestor's config is
        checked so limits hold without agent cooperation. Each cap is
        re-read here rather than at the caller's top so the read is current:
        both callers run under the ``.worktrees`` flock, just before
        registering the child or re-arming the slot, so concurrent fan-out
        is serialized and the descendant counts are authoritative -- a
        TOCTOU race that checked before the lock could let several inits
        each pass and blow past the cap (the just-registered child lands
        idle, so it holds its slot for the next serialized check).

        Args:
            depth: The incoming child's absolute depth (spawn), or ``None``
                for a re-arm -- which also skips the node's own caps (they
                bound its subtree, which the re-arm leaves unchanged) and
                the budget bound.
            budget: The child's requested ``--max-cost`` (USD), if any.

        Raises:
            ValueError: If any live subtree or budget cap would be exceeded.

        """
        # the incoming slot's direct parent by name -- width binds there: the
        # spawning node itself, or the re-arming node's dotted parent
        if depth is not None:
            parent_branch = self.branch
        else:
            parent_branch, *_ = self.branch.rsplit('.', 1)
        for ancestor in self._self_and_ancestors():
            # a re-arm skips the node itself -- its own caps bound its
            # subtree, which the re-arm leaves unchanged
            if depth is None and ancestor is self:
                continue
            # max-children (width): the direct parent only, counting the
            # slot's unsettled siblings
            if ancestor.branch == parent_branch:
                max_children = ancestor.config.get('max_children')
                if max_children is not None:
                    direct = ancestor._count_unsettled(max_depth=1)
                    if direct >= max_children:
                        raise ValueError(
                            f'Max children reached on {ancestor.branch!r}'
                            f' (limit {max_children},'
                            f' {direct} unsettled direct children).'
                        )
            # max-depth: child's depth relative to ancestor (structural --
            # settled nodes still occupy their place in the tree)
            if depth is not None:
                ancestor_depth = ancestor.branch.count('.')
                ancestor_max_depth = ancestor.config.get('max_depth')
                if ancestor_max_depth is not None:
                    if depth - ancestor_depth > ancestor_max_depth:
                        raise ValueError(
                            f'Max depth reached on {ancestor.branch!r}'
                            f' (limit {ancestor_max_depth}, child would be'
                            f' at relative depth {depth - ancestor_depth}).'
                        )
            # max-descendants: unsettled descendants vs ancestor's budget
            ancestor_max_descendants = ancestor.config.get('max_descendants')
            if ancestor_max_descendants is not None:
                existing = ancestor._count_unsettled()
                if existing >= ancestor_max_descendants:
                    raise ValueError(
                        f'Max descendants reached on {ancestor.branch!r}'
                        f' (limit {ancestor_max_descendants},'
                        f' {existing} unsettled descendants).'
                    )
        # enforce the child's max_cost against the parent's remaining run
        # budget (spawn only -- every run re-arms the node's own max_cost)
        if depth is None:
            return
        max_cost = self.config.get('max_cost')
        if max_cost is not None:
            if budget is None:
                raise ValueError('Parent has max_cost; child must also set --max-cost.')
            # bound the child by the budget the run it joins will have: against an
            # active run, the parent's per-run remaining (subtree-aware, max_cost
            # minus the whole subtree's spend); with no active run the next run
            # starts fresh, so the parent's configured max_cost -- not the drained
            # remaining of a most-recent run that the child won't share
            _, _, run_id = self.record.resolve_context(active=True)
            if run_id is not None:
                remaining = self.cost.remaining(run_id=run_id)
            else:
                remaining = float(max_cost)
            if budget > remaining:
                raise ValueError(
                    f'Max cost ${budget:.2f} exceeds remaining ${remaining:.2f}.'
                )

    def child_add(
        self: Node,
        name: str,
        *,
        title: Optional[str] = None,
        max_cost: Optional[float] = None,
        max_depth: Optional[int] = None,
        max_children: Optional[int] = None,
        max_descendants: Optional[int] = None,
    ) -> int:
        """Register a child node.

        Caps land on the row verbatim, ``None`` included: a ``--reset``
        re-init upserts over the old row, and an omitted cap must clear
        there just as it does in the reseeded ``config.json`` -- reconcile
        leaves config-absent keys alone, so a stale registry cap would
        otherwise survive every future heal and misreport an uncapped node
        as capped.

        Args:
            name: Child node name.
            title: Child's display name.
            max_cost: Child's maximum cost in USD.
            max_depth: Child's maximum nesting depth.
            max_children: Child's maximum direct child nodes.
            max_descendants: Child's maximum total descendant nodes.

        Returns:
            Node ID.

        """
        branch = f'{self.branch}.{name}'
        data = {
            'node': branch,
            'status': 'idle',
            'max_cost': max_cost,
            'max_depth': max_depth,
            'max_children': max_children,
            'max_descendants': max_descendants,
        }
        if title is not None:
            data['title'] = title
        result = self.db.merge(data, 'nodes', conflict=['node'])
        # auto-subscribe to the child's readable channels (seeded by the
        # child's radio.init before registration, so validation always
        # resolves) -- unless this parent is blind: a blind node holds no
        # subscriptions of its own, so it must not start reading a child it
        # spawns mid-run (the child's own parent-watch is separate)
        if not self.config.get('blind'):
            self.radio.subscribe(branch)
        return result

    def _child_not_found(self: Node, name: str) -> ValueError:
        """Build the missing-child error."""
        return ValueError(f'Child not found: {name!r}')

    def _child_worktree_not_found(self: Node, branch: str) -> ValueError:
        """Build the missing-child-worktree error."""
        return ValueError(f'Child worktree not found: {branch!r}')

    def child_update(
        self: Node,
        name: str,
        *,
        title: Optional[str] = None,
        max_cost: Optional[float] = None,
        max_iter_cost: Optional[float] = None,
        max_step_cost: Optional[float] = None,
        reserve_budget: Optional[float] = None,
        step_timeout: Optional[str] = None,
        max_depth: Optional[int] = None,
        max_children: Optional[int] = None,
        max_descendants: Optional[int] = None,
    ) -> None:
        """Update a child node's configuration.

        Updates both the parent's ``nodes`` table and the
        child's ``config.json``.

        Args:
            name: Child node name.
            title: New display name.
            max_cost: New maximum cost in USD.
            max_iter_cost: New per-iteration cost cap in USD; lives only in
                the child's ``config.json`` (the ``nodes`` table has no column).
            max_step_cost: New per-step cost cap in USD; lives only in the
                child's ``config.json`` (the ``nodes`` table has no column).
            reserve_budget: New cleanup reserve in USD; lives only in the
                child's ``config.json`` (the ``nodes`` table has no column).
            step_timeout: New per-step time budget; lives only in the
                child's ``config.json`` (the ``nodes`` table has no column).
            max_depth: New maximum nesting depth.
            max_children: New maximum direct child nodes.
            max_descendants: New maximum total descendant nodes.

        """
        # a draining seat re-arms nothing: cap raises from a --drain run
        # refuse harness-side, mirroring the init and start guards
        if _draining():
            raise RuntimeError(
                'Cannot update a node from a draining run (--drain forbids re-arms).'
            )
        # initialize updates -- the iter/step caps, the reserve, and the
        # step timeout are config-only
        data = {}
        if title is not None:
            data['title'] = title
        if max_cost is not None:
            data['max_cost'] = max_cost
        if max_depth is not None:
            data['max_depth'] = max_depth
        if max_children is not None:
            data['max_children'] = max_children
        if max_descendants is not None:
            data['max_descendants'] = max_descendants
        config_only = {}
        if max_iter_cost is not None:
            config_only['max_iter_cost'] = max_iter_cost
        if max_step_cost is not None:
            config_only['max_step_cost'] = max_step_cost
        if reserve_budget is not None:
            config_only['reserve_budget'] = reserve_budget
        if step_timeout is not None:
            config_only['step_timeout'] = step_timeout
        if not data and not config_only:
            return
        # verify child exists
        branch = f'{self.branch}.{name}'
        if not self.db.exists('nodes', where={'node': branch}):
            raise self._child_not_found(name)
        # require a live worktree -- updating only the nodes table would leave
        # the child's config.json stale and the two out of sync
        child_worktree_dir = fractal.util.git.find_worktree(self.repo_dir, branch)
        if child_worktree_dir is None:
            raise self._child_worktree_not_found(branch)
        # write the child's config.json first (the failure-prone step --
        # a malformed/locked config or vanished worktree raises here), then
        # the nodes table, so a config-write failure can't desync the two
        child_config = self.__class__(child_worktree_dir).config
        for key, value in {**data, **config_only}.items():
            child_config.set(key, value)
        if data:
            self.db.update(data, 'nodes', where={'node': branch})

    def child_retune(
        self: Node,
        name: str,
        *,
        title: Optional[str] = None,
        max_cost: Optional[float] = None,
        max_iter_cost: Optional[float] = None,
        max_step_cost: Optional[float] = None,
        reserve_budget: Optional[float] = None,
        step_timeout: Optional[str] = None,
        max_depth: Optional[int] = None,
        max_children: Optional[int] = None,
        max_descendants: Optional[int] = None,
    ) -> dict[str, tuple[Any, Any]]:
        """Retune a child's configuration, validating the merged result.

        The policy half over :meth:`child_update` (which writes the registry
        row and the child's ``config.json`` together): a default-mode reserve
        (equal to what init's default fraction materialized for the old cap)
        retunes to track a new cap instead of going stale; per-iter/step caps
        are validated against the *effective* cap; the merged config is
        validated as init would (:meth:`Config.validate`); priors are
        captured before the write.

        Args:
            name: Child node name.
            title: New display name.
            max_cost: New maximum cost in USD.
            max_iter_cost: New per-iteration cost cap in USD.
            max_step_cost: New per-step cost cap in USD.
            reserve_budget: New cleanup reserve in USD, already resolved --
                the CLI's ``N%`` grammar never enters core.
            step_timeout: New per-step time budget (a running loop picks it
                up at its next iteration top).
            max_depth: New maximum nesting depth.
            max_children: New maximum direct child nodes.
            max_descendants: New maximum total descendant nodes.

        Returns:
            ``{key: (old, new)}`` for every PROVIDED key -- ``old == new``
            included, and an implicitly retuned reserve counts as provided --
            so a confirmation echo shows every requested write.

        Raises:
            ValueError: On a violated cap invariant (user-facing flag
                spellings), or when the child is missing.

        """
        # resolve the child's live config for priors and the merged validation
        branch = f'{self.branch}.{name}'
        if not self.db.exists('nodes', where={'node': branch}):
            raise self._child_not_found(name)
        child_worktree_dir = fractal.util.git.find_worktree(self.repo_dir, branch)
        if child_worktree_dir is None:
            raise self._child_worktree_not_found(branch)
        child_config = self.__class__(child_worktree_dir).config
        # resolve the reserve before validating; a default-mode reserve (equal
        # to what init's default fraction materialized for the old cap) retunes
        # to track a new cap instead of going stale
        old_max_cost = child_config.get('max_cost')
        old_reserve = child_config.get('reserve_budget')
        effective_max_cost = max_cost if max_cost is not None else old_max_cost
        if reserve_budget is None and old_reserve is not None:
            # a degenerate cap skips the retune -- the merged-config validation
            # below owns that rejection and its wording
            if max_cost is not None and max_cost > 0 and old_max_cost is not None:
                # both sides round at RESERVE_PRECISION, matching
                # parse_reserve_budget's materialization -- an unrounded
                # product would break the default-detection equality and
                # persist float noise into config.json and the retune echo
                default_reserve = round(
                    DEFAULT_RESERVE_FRACTION * old_max_cost,
                    RESERVE_PRECISION,
                )
                if old_reserve == default_reserve:
                    reserve_budget = round(
                        DEFAULT_RESERVE_FRACTION * max_cost,
                        RESERVE_PRECISION,
                    )
        # a per-iter/step cap on an effectively uncapped child mirrors init's
        # rejection -- unenforceable once the per-iter budget drains
        if effective_max_cost is None:
            if max_iter_cost is not None:
                raise ValueError('--max-iter-cost requires --max-cost.')
            if max_step_cost is not None:
                raise ValueError('--max-step-cost requires --max-cost.')
        # validate the merged config the way init/config set do -- a bare
        # non-negative boundary check still admits max_cost==0 and
        # step<=iter<=run orderings broken from either side (a lowered cap or
        # a raised sub-cap)
        effective_iter_cost = max_iter_cost
        if effective_iter_cost is None:
            effective_iter_cost = child_config.get('max_iter_cost')
        effective_step_cost = max_step_cost
        if effective_step_cost is None:
            effective_step_cost = child_config.get('max_step_cost')
        effective_reserve_budget = reserve_budget
        if effective_reserve_budget is None:
            effective_reserve_budget = old_reserve
        effective_step_timeout = step_timeout
        if effective_step_timeout is None:
            effective_step_timeout = child_config.get('step_timeout')
        merged = {
            'max_cost': effective_max_cost,
            'max_iter_cost': effective_iter_cost,
            'max_step_cost': effective_step_cost,
            'reserve_budget': effective_reserve_budget,
            'step_timeout': effective_step_timeout,
        }
        child_config.validate(merged)
        # the confirmation echo needs each provided key's stored value from
        # before the write -- a retuned reserve counts even without its flag
        updates = {
            'title': title,
            'max_cost': max_cost,
            'max_iter_cost': max_iter_cost,
            'max_step_cost': max_step_cost,
            'step_timeout': step_timeout,
            'max_depth': max_depth,
            'max_children': max_children,
            'max_descendants': max_descendants,
            'reserve_budget': reserve_budget,
        }
        provided = {key: value for key, value in updates.items() if value is not None}
        priors = {key: child_config.get(key) for key in provided}
        # write both stores through the update surface
        self.child_update(
            name=name,
            title=title,
            max_cost=max_cost,
            max_iter_cost=max_iter_cost,
            max_step_cost=max_step_cost,
            reserve_budget=reserve_budget,
            step_timeout=step_timeout,
            max_depth=max_depth,
            max_children=max_children,
            max_descendants=max_descendants,
        )
        return {key: (priors[key], value) for key, value in provided.items()}

    def child_list(
        self: Node,
        *,
        max_depth: Optional[int] = None,
    ) -> Optional[list[dict]]:
        """List registered children.

        Args:
            max_depth: Maximum depth relative to this node.
                ``1`` lists direct children only, ``2``
                includes grandchildren, ``None`` lists all
                descendants.

        Returns:
            List of child records, or ``None`` if the node is
            not initialized.

        """
        if not self.exists():
            return None
        # the registry is tree-wide -- scope to this node's subtree by prefix
        # (substr, not LIKE: a branch name may contain LIKE-significant bytes)
        # and bound depth by dot count, keeping the built-SELECT row order
        prefix = f'{self.branch}.'
        query = 'SELECT * FROM nodes WHERE substr(node, 1, ?) = ?'
        params: tuple[Any, ...] = (len(prefix), prefix)
        if max_depth is not None:
            query += " AND length(node) - length(replace(node, '.', '')) <= ?"
            params += (max_depth + self.branch.count('.'),)
        query += ' ORDER BY rowid DESC'
        return self.db.read(query=query, params=params)

    def child_approve(
        self: Node,
        child: Node,
        *,
        step_id: Optional[int] = None,
    ) -> int:
        """Approve a gated step in a child, recording ``approve`` on both event logs.

        ``self`` is the parent. Enforces direct parentage, writes the approval
        to the child's step (the source of truth), and logs an ``approve``
        event on the parent (naming the child + step) and on the child (its own
        step). The events are best-effort audit.

        Args:
            child: The direct child whose step to approve.
            step_id: The child's step to approve; defaults to the child's
                active (gated) step.

        Returns:
            The approved step's id.

        Raises:
            PermissionError: If ``child`` is not a direct child of ``self``.
            ValueError: If there is no active step to approve, or the step does
                not exist or does not require approval.

        """
        # enforce direct parentage -- only the parent may approve its child
        parent_branch, *_ = child.branch.rsplit('.', 1)
        if '.' not in child.branch or parent_branch != self.branch:
            raise PermissionError(
                f'Only the parent ({parent_branch}) can approve steps of'
                f' {child.branch}; this node is {self.branch}.'
            )
        # default to the child's active step awaiting approval -- during an
        # approval wait the periodic SYNC opens a second active step row, so
        # resolve_context's newest-active pick can land on the SYNC bookkeeping
        # row (approval NULL); select the gated row (approval set) instead
        if step_id is None:
            active = child.db.read(
                'steps',
                where={'node': child.branch, 'status': 'active'},
            )
            gated = [row for row in active if row.get('approved') is not None]
            if not gated:
                raise ValueError(f'No active step on {child.branch} to approve.')
            step_id = gated[0]['step_id']
        # validate the child's step up front so a doomed approval never logs an
        # event -- both the missing-step and does-not-require-approval guards
        # must fail before event_start (the read also yields the label); the
        # node pin keeps a foreign node's step id from being approved
        rows = child.db.read(
            'steps',
            where={'step_id': step_id, 'node': child.branch},
            limit=1,
        )
        if not rows:
            raise ValueError(f'Step {step_id} not found.')
        if rows[0].get('approved') is None:
            raise ValueError(f'Step {step_id} does not require approval.')
        step = rows[0]['step']
        step_name = rows[0]['step_name']
        label = f'step {step} ({step_name})'
        # log on the parent (run lineage only when it's mid-run, else NULL for
        # a manual approval), write it, then dual-log on the child (active step)
        metadata = f'{child.branch}: {label}'
        parent_event_id = self.record.event_start('approve', metadata=metadata)
        try:
            child.record.step_approve(step_id=step_id)
        except Exception:
            if parent_event_id is not None:
                self.record.event_end(event_id=parent_event_id, status='failed')
            raise
        # the approval landed (the source of truth); dual-log on the child, then
        # close the parent event in a finally so a child-side audit failure can
        # never leave it orphaned (the event rows are best-effort)
        try:
            child_event_id = child.record.event_start('approve', metadata=label)
            if child_event_id is not None:
                child.record.event_end(event_id=child_event_id, status='completed')
        finally:
            if parent_event_id is not None:
                self.record.event_end(event_id=parent_event_id, status='completed')
        return step_id

    def child_pending(self: Node) -> list[dict]:
        """List direct children's steps awaiting this node's approval.

        One row per direct-child step awaiting approval -- ``approved=''``
        on an ``active`` or ``paused`` step row -- as
        ``{'branch', 'step_id', 'step', 'step_name'}``. Only direct children
        are listed -- the steps this node can actually approve.

        Returns:
            Pending-approval rows across the direct children.

        """
        result: list[dict] = []
        for row in self.child_list(max_depth=1) or []:
            branch = row['node']
            for step in self.db.read('steps', where={'node': branch, 'approved': ''}):
                # a gate releases only into a live wait: 'active' is mid-wait
                # and 'paused' is parked (approving while parked is the resume
                # flow) -- any other terminal stranded its gate (a kill, a
                # crash reconcile, a reserve wind-down), so listing it would
                # offer an approval nothing will ever read
                if step['status'] not in ('active', 'paused'):
                    continue
                pending = {
                    'branch': branch,
                    'step_id': step['step_id'],
                    'step': step['step'],
                    'step_name': step['step_name'],
                }
                result.append(pending)
        return result

    def commit(
        self: Node,
        message: Optional[str] = None,
        *,
        init: bool = False,
        check: bool = False,
        ignore_scope: bool = False,
        force: bool = False,
    ) -> str:
        """Commit the current iteration's work.

        Delegates to the ``core.commit`` pipeline (scope check, lint, stage,
        commit, and push unless ``local`` is set).

        Args:
            message: Short description appended to the commit message.
            init: Use the ``init`` label instead of ``iteration <run>.<iter>``.
            check: Error if uncommitted changes exist instead of committing.
            ignore_scope: Commit out-of-scope changes but still lint (a narrower
                escape hatch than ``force``).
            force: Bypass scope and lint checks and git hooks.

        Returns:
            Commit output and notices.

        Raises:
            RuntimeError: If called on a user node without ``init``.
            ValueError: If flags conflict or ``message`` is missing without ``check``.

        """
        # user nodes take only the --init baseline (commits fractal's own
        # artifacts on the base branch so a node worktree can branch from a
        # committed tree); reject an ordinary commit
        if self.is_user:
            if not init:
                raise RuntimeError(
                    'Cannot commit from a user node (only --init is supported).'
                )
            if not message:
                raise ValueError('Message is required.')
            if ignore_scope:
                raise ValueError('--init cannot be used with --ignore-scope.')
            if force:
                raise ValueError('--init cannot be used with --force.')
            return commit.commit_user_init(self, message)
        # handle mutually exclusive configurations
        if init and ignore_scope:
            raise ValueError('--init cannot be used with --ignore-scope.')
        if check and ignore_scope:
            raise ValueError('--check cannot be used with --ignore-scope.')
        if init and force:
            raise ValueError('--init cannot be used with --force.')
        if check and force:
            raise ValueError('--check cannot be used with --force.')
        if ignore_scope and force:
            raise ValueError('--ignore-scope cannot be used with --force.')
        if init and check:
            raise ValueError('--init cannot be used with --check.')
        # validate message
        if message or check:
            message = message or ''
        else:
            raise ValueError('Message is required unless --check is set.')
        # run the pipeline (scope check, lint, stage, commit, push); the loop
        # invokes the pipeline directly with its live iteration and lineage,
        # so an agent/operator commit resolves the iteration from the open
        # iteration row (else 0) and the commit event pins to the
        # active run/iter/step context
        return commit.commit(
            node=self,
            message=message,
            init=init,
            check=check,
            ignore_scope=ignore_scope,
            force=force,
        )

    def _default_agent(self: Node) -> Optional[str]:
        """Return the node's default agent (the base of its ``agent`` command)."""
        if agent := self.config.get('agent'):
            agent, *_ = agent.split()
            return agent
        return None

    def agent(
        self: Node,
        command: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> Agent:
        """Return this node's agent backend bound to this node.

        The accessor is a resolver over a keyed family, not a bound
        singleton: per-step ``agent:`` overrides mean one node may run
        several agents in one iteration.

        Args:
            command: Full agent command (e.g. ``'claude --some-flag'``);
                defaults to the node's effective configured agent (own
                config else the nearest ancestor's,
                :meth:`agent_effective`).
            provider: Provider-route override (per-step frontmatter);
                defaults to the node's effective configured provider.

        Returns:
            The registered backend, bound to this node and ``command``.

        Raises:
            ValueError: If no agent is configured and none is given, or
                the base command has no registered backend.

        """
        from .agent import command_base, resolve

        # resolve the command, refusing an agentless node
        command = command or self.agent_effective()
        if command is None:
            raise ValueError(
                'No agent configured; set one with `fractal init --agent`.'
            )
        # resolve the base command against the seam's module-level registry,
        # threading the tree root so a deployment hook file's subclasses win
        # across processes (imported at call time -- node.py never imports
        # agent.py at runtime, keeping the node<->seam boundary one-way)
        name = command_base(command)
        if provider is None:
            provider = self.provider_effective()
        return resolve(name, root=self.db.path.parent)(self, command, provider)

    def chat(
        self: Node,
        prompt: str,
        *,
        session: Optional[str] = None,
        current: bool = False,
        resume: bool = False,
        model: Optional[str] = None,
        render: Optional[Callable[[StreamEvent], None]] = None,
    ) -> Optional[str]:
        """Send one prompt to the node's agent and stream the reply.

        Nothing is inferred by default, so a bare chat is always **fresh** -- a
        brand-new session whose prompt is seeded with the node's ``NODE.md`` and
        ``modes/CHAT.md``. ``current`` forks the node's live loop session;
        ``session`` forks a given id (or, with ``resume``, continues it in
        place). Forking leaves the source session untouched, so a running loop
        is never perturbed. Codex can resume in place but cannot fork. Streams
        with no cost or session side effects and returns the resulting id.

        Args:
            prompt: The prompt to send.
            session: A session id to fork (or continue in place with ``resume``).
            current: Fork the node's live loop session (mutually exclusive with
                ``session``/``resume``).
            resume: Continue ``session`` in place (same id) instead of forking.
            model: Model override; defaults to the node's configured model.
            render: Presentation callback receiving every parsed stream
                event (the CLI passes its ANSI renderer).

        Returns:
            The agent's session id, or ``None`` if the stream carried none.

        Raises:
            ValueError: No agent configured; incompatible flags; ``current`` with
                no live session; resuming the live loop session; or forking a
                codex session.
            RuntimeError: The agent exited with a non-zero status.

        """
        backend = self.agent()
        command = self.chat_command(
            prompt,
            session=session,
            current=current,
            resume=resume,
            model=model,
        )
        # spawn the agent and stream with no cost/session writes (no step_id),
        # capturing the resulting session id from the stream
        proc = backend.spawn(command)
        # stream, reaping the child even if streaming raises (a codex error
        # stream raises after draining stdout) so the process is never left
        # unwaited; a raise MID-drain (parser fault, Ctrl-C) leaves the child
        # writing into a now-unread pipe, so kill it before the wait -- a
        # writer blocked on the full pipe would deadlock proc.wait(). chat
        # spawns a single process (no start_new_session), so kill the PID
        # directly, not its group
        try:
            result = backend.stream(proc.stdout, render=render)
        except BaseException:
            if proc.poll() is None:
                try:
                    proc.kill()
                except (ProcessLookupError, PermissionError):
                    pass
            raise
        finally:
            returncode = proc.wait()
        if returncode != 0:
            raise RuntimeError(f'{command.agent} exited with a non-zero status.')
        return result.session

    def chat_command(
        self: Node,
        prompt: str,
        *,
        session: Optional[str] = None,
        current: bool = False,
        resume: bool = False,
        model: Optional[str] = None,
    ) -> Invocation:
        """Resolve and validate one chat turn into its agent invocation.

        The build half of ``chat``: the same validation (incompatible
        flags, no live session, codex cannot fork) and the same prompt seeding
        (``NODE.md`` + ``CHAT.md`` fresh, ``CHAT.md`` on a fork, nothing on a
        resume) -- without spawning anything. A caller that streams the agent
        output itself (the TUI) spawns the returned invocation.

        Args:
            prompt: The prompt to send.
            session: A session id to fork (or continue in place with ``resume``).
            current: Fork the node's live loop session (mutually exclusive with
                ``session``/``resume``).
            resume: Continue ``session`` in place (same id) instead of forking.
            model: Model override; defaults to the node's configured model.

        Returns:
            The spawnable agent invocation.

        Raises:
            ValueError: No agent configured; incompatible flags; ``current`` with
                no live session; resuming the live loop session; or forking a
                codex session.

        """
        # resolve the agent
        backend = self.agent()
        if model is None:
            model = self.config.get('model')
        # apply the node's configured reasoning effort too, so a chat turn runs
        # at the same depth as the loop's steps (the loop threads this same
        # config value into every invocation)
        effort = self.config.get('effort')
        # the loop's woven session (while running or parked) -- never continued
        # in place: that would perturb the running loop's turn, or the session
        # a paused run resumes with
        live = None
        if self.status() in ('active', 'paused'):
            live = self.sessions.get(backend.name)
        # validate the request: --current forks the live session and is mutually
        # exclusive with --session/--resume; nothing else is inferred
        if current and (session is not None or resume):
            raise ValueError('--current cannot be combined with --session or --resume.')
        # --current forks the node's live loop session (forking agents only)
        if current:
            if not backend.can_fork:
                # the refusal outranks the live check; route it through the
                # backend's one no-fork raise site (its message carries the
                # remedy and citations), surfaced as today's error type -- the
                # placeholder session steers the doomed build past
                # the fork-without-a-session guard
                try:
                    backend.invocation(prompt, session=live or '-', fork=True)
                except NotImplementedError as e:
                    raise ValueError(str(e)) from e
            if live is None:
                raise ValueError(
                    '--current: the node has no live session to fork (it is not'
                    ' running, or has not started a session yet).'
                )
            session = live
        # --resume requires a session and continues it in place; refuse on the
        # live loop session (it would perturb the running loop)
        if resume and session is None:
            raise ValueError('--resume requires --session (the session to continue).')
        if resume and session == live:
            raise ValueError(
                'Refusing to resume the loop session in place (it would'
                ' perturb the running loop, or the session a paused run'
                ' resumes with); use --current to fork it instead.'
            )
        # a given/current session is forked by default; --resume continues it
        fork = session is not None and not resume
        # seed the prompt with chat framing: a fresh chat also gets the node's
        # NODE.md charter; a fork (the agent was executing the loop) gets CHAT.md
        # so it knows it is now chatting; a resume continues an already-framed
        # session and adds nothing
        if session is None:
            seed = render.chat_seed(self, fresh=True)
        elif fork:
            seed = render.chat_seed(self, fresh=False)
        else:
            seed = ''
        if seed:
            prompt = f'{seed}\n\n{prompt}'
        # build the agent invocation (the full configured command, like the
        # loop); a non-forking backend's NotImplementedError surfaces as
        # today's error type at the chat surface
        try:
            return backend.invocation(
                prompt,
                session=session,
                fork=fork,
                model=model,
                effort=effort,
            )
        except NotImplementedError as e:
            raise ValueError(str(e)) from e

    def render_template(
        self: Node,
        template: str,
        *,
        overrides: Optional[dict[str, str]] = None,
    ) -> str:
        """Substitute the node's ``$VAR`` placeholders into ``template``.

        The variable map is :func:`render.variables` (``overrides`` win).
        Substitution matches GNU ``envsubst`` (``$NAME``/``${NAME}`` only;
        unknown placeholders and ``$$`` pass through verbatim) -- the grammar
        the renderer is pinned against.

        Args:
            template: The text to render.
            overrides: Variable values layered over (and winning against) the
                derived map -- the loop passes live run state; a chat passes
                chat sentinels.

        Returns:
            The rendered text.

        """
        return render.render(template, render.variables(self, overrides))

    def build_prompt(
        self: Node,
        step_file: str,
        *,
        overrides: Optional[dict[str, str]] = None,
    ) -> str:
        """Assemble and render a step's full prompt (:func:`render.prompt`).

        Args:
            step_file: The step markdown file.
            overrides: Run-scoped variable values (see
                :meth:`render_template`).

        Returns:
            The rendered prompt.

        """
        step_text = pathlib.Path(step_file).read_text(encoding='utf-8')
        return render.prompt(self, step_text, overrides)

    def _run_script(
        self: Node,
        script: str,
        *args: str,
    ) -> subprocess.CompletedProcess[str]:
        """Run a bundled script (delegates to :func:`worktree.run_script`).

        Args:
            script: Script filename in ``_scripts/``.
            *args: Arguments to pass to the script.

        Returns:
            Completed process result.

        """
        return worktree.run_script(self._package_dir, script, *args)

    def _live_descendants(
        self: Node,
        *,
        status: Optional[str] = None,
        max_depth: Optional[int] = None,
    ) -> list[tuple[dict, Node]]:
        """Return ``(row, node)`` for each descendant with a live worktree.

        The ``nodes`` table is a flat registry of every descendant, but its
        ``status`` is best-effort (push-updated). This reads each descendant's
        own ``.status()`` into the row and drops any whose worktree is gone --
        the authoritative view of the subtree.

        Args:
            status: Include only descendants whose real status matches.
            max_depth: Maximum depth relative to this node.

        Returns:
            A ``(row, node)`` pair per live descendant, the row's ``status``
            reconciled to the node's real status.

        """
        result = []
        if rows := self.child_list(max_depth=max_depth):
            for row in rows:
                worktree_dir = fractal.util.git.find_worktree(
                    repo_dir=self.repo_dir,
                    branch=row['node'],
                )
                if not worktree_dir:
                    continue
                node = self.__class__(worktree_dir)
                if not node.exists():
                    continue
                real_status = node.status()
                if status is not None and real_status != status:
                    continue
                result.append(({**row, 'status': real_status}, node))
        return result

    def _self_and_ancestors(self: Node) -> Iterator[Node]:
        """Yield this node and each existing ancestor up to the root.

        Walks the branch name (``a.b.c`` -> ``a.b`` -> ``a``) rather than hopping
        worktree to worktree, so a pruned intermediate node does not cut the walk
        short and skip limit enforcement on every ancestor above it -- a missing
        ancestor is simply skipped while the walk continues toward the root.
        """
        yield self
        branch = self.branch
        while '.' in branch:
            parent_branch, *_ = branch.rsplit('.', 1)
            parent_worktree_dir = fractal.util.git.find_worktree(
                repo_dir=self.repo_dir,
                branch=parent_branch,
            )
            if parent_worktree_dir:
                parent = self.__class__(parent_worktree_dir)
                if parent.exists():
                    yield parent
            branch = parent_branch

    def agent_effective(self: Node) -> Optional[str]:
        """Return the effective agent command: own config, else an ancestor's.

        The spawn-time inheritance walk (:meth:`init`) read back at steering
        time -- what the node's loop actually runs when its own config names
        no agent.

        Returns:
            The effective agent command, or ``None`` when no node up the
            chain configures one.

        """
        for ancestor in self._self_and_ancestors():
            if agent := ancestor.config.get('agent'):
                return agent
        return None

    def provider_effective(self: Node) -> Optional[str]:
        """Return the effective provider route: own config, else an ancestor's.

        The spawn-time inheritance walk (:meth:`init`) read back at steering
        time -- the route a routed backend binds when the node's own config
        names none; ``None`` means the vendor's own endpoint.

        Returns:
            The effective provider route, or ``None`` when no node up the
            chain configures one.

        """
        for ancestor in self._self_and_ancestors():
            if provider := ancestor.config.get('provider'):
                return provider
        return None

    def _mark_active_killed(
        self: Node,
        *,
        skip: Optional[int] = None,
        metadata: Optional[str] = None,
    ) -> None:
        """Mark every still-open lifecycle row ``killed``.

        Delegates to :meth:`Record.close_open`: entity rows close
        first-writer-wins (``killed`` is exit 1), and any stray active event
        is closed by status, skipping the in-flight kill event (``_kill``
        finalizes that one via ``event_end``).

        Args:
            skip: An ``events`` row to leave untouched -- the in-flight kill
                event (avoids a redundant killed-then-completed write).
            metadata: Attribution stamped on the closing run row.

        """
        self.record.close_open('killed', skip_event=skip, metadata=metadata)

    @classmethod
    def _deregister_subtree(
        cls: type[Node],
        db: Database,
        repo_dir: pathlib.Path,
        branch: str,
        descendant_branches: list[str],
    ) -> None:
        """Clear a torn-down subtree from the central registry.

        Shared by ``delete`` (worktrees already removed) and ``deregister`` (an
        orphan with no worktree): removes the subtree's ``nodes`` rows and its
        subscriptions in both directions. Everything else -- runs, steps,
        events, messages -- persists, so history outlives the node. The
        ``delete`` event is logged on the parent when it is still reachable;
        a missing parent (e.g. a hand-removed ``.fractal``) is warned about,
        not fatal -- the teardown already happened, so crashing would leave a
        half-deleted tree.

        Args:
            db: The central database (captured before any teardown).
            repo_dir: Main repo root (captured before any teardown).
            branch: The subtree root's branch.
            descendant_branches: Every descendant branch in the subtree.

        """
        # clear the subtree's registry rows and subscriptions (both directions)
        subtree_branches = [branch, *descendant_branches]
        for subtree_branch in subtree_branches:
            db.delete('nodes', where={'node': subtree_branch})
            db.delete('subs', where={'node': subtree_branch})
            db.delete('subs', where={'target': subtree_branch})
        # log the delete on the parent when it is still reachable (run lineage
        # only when mid-run, else NULL for a manual delete)
        parts = branch.rsplit('.', 1)
        if len(parts) != 2:
            return
        parent_branch, _ = parts
        parent_worktree_dir = fractal.util.git.find_worktree(repo_dir, parent_branch)
        parent = cls(parent_worktree_dir) if parent_worktree_dir else None
        if parent is not None and parent.exists():
            event_id = parent.record.event_start('delete', metadata=branch)
            if event_id is not None:
                parent.record.event_end(event_id=event_id, status='completed')
        else:
            logger.warning(
                f'Warning: parent {parent_branch!r} of {branch} is missing;'
                f' the subtree was removed and deregistered, but no delete'
                f' event could be logged on its parent.'
            )


# ------ helper functions


def node_dir(worktree: PathLike, project: str, branch: str) -> pathlib.Path:
    """Return the node data directory for explicit inputs (the one derivation).

    Pure path arithmetic -- no git, no subprocess -- so the TUI's poll path
    composes it with cached inputs. Under a sub-project the directory nests
    at ``<worktree>/<project>/.fractal/<branch>``; a repo-root project
    (``'.'``) puts it at ``<worktree>/.fractal/<branch>``.

    Args:
        worktree: The node's worktree path.
        project: Project sub-path within the worktree (``'.'`` for repo-root).
        branch: The node's branch.

    Returns:
        The node data directory.

    """
    base = pathlib.Path(worktree)
    if project not in ('', '.'):
        base = base / project
    return base / FRACTAL_FOLDER / branch


def tmux_session_name(repo_dir: PathLike, branch: str) -> str:
    """Return the tmux session name for a branch.

    Format is ``<repo_name> (<branch>)``. tmux munges ``.`` and ``:`` in
    session names (target syntax), so the repo name flattens both to dashes
    and the branch flattens dots (git refs forbid ``:``) -- must match
    ``start.sh``. Pure string arithmetic (no git, no subprocess) for the
    TUI's poll path.

    Args:
        repo_dir: Main repo root.
        branch: The node's branch.

    Returns:
        The tmux session name.

    """
    repo_name = pathlib.Path(repo_dir).name.replace('.', '-').replace(':', '-')
    branch_name = branch.replace('.', '-')
    return f'{repo_name} ({branch_name})'


def _base_status(status: Optional[str]) -> str:
    """Return the bare lifecycle status -- the first space-delimited chunk.

    A status may carry a pending-signal suffix for display (e.g.
    ``active (stopping)``); filters and comparisons match on the base token.

    Args:
        status: Status string, possibly decorated (``None`` tolerated).

    Returns:
        The undecorated status token (empty for ``None``).

    """
    result, *_ = (status or '').partition(' ')
    return result


def _draining() -> bool:
    """Return whether the acting seat runs under a drain.

    Two sources, either sufficient: the loop-exported ``_DRAIN`` (present
    in every seat subprocess of a ``--continue --drain`` run) and the
    acting node's own run carrying the durable ``drain`` signal -- the
    latter standing after an environment scrub and across a pause/resume,
    which the export alone does not survive.
    """
    if os.environ.get('_DRAIN'):
        return True
    actor = Node.resolve_actor()
    return actor is not None and actor.drain_bound()


def _recorded_group(pgid: int, recorded_at: float) -> bool:
    """Return whether a live process group is the one its record named.

    A group id is its leader's pid, and the leader is already running when
    the loop records it -- so a leader ``ps`` dates *after* the record is a
    recycled pid fronting an unrelated group. A group that outlived its
    leader still matches: the OS cannot re-issue the id while any member
    survives. No answer to arbitrate with (``ps`` failed, an unparseable
    instant) reads as not the recorded group -- sparing a stranger beats
    reaping a maybe-orphan.

    Args:
        pgid: A live process group id (its leader's pid).
        recorded_at: Epoch instant the group was recorded.

    Returns:
        Whether the group can be treated as the recorded one.

    """
    # ask ps for the leader's start instant (LC_ALL pins the format)
    env = {**os.environ, 'LC_ALL': 'C'}
    try:
        result = subprocess.run(
            ['ps', '-p', f'{pgid}', '-o', 'lstart='],
            capture_output=True,
            text=True,
            env=env,
        )
    except OSError:
        return False
    lstart = result.stdout.strip()
    # no process wears the leader's pid: the live group outlived its
    # leader, which pins its identity
    if result.returncode != 0 or not lstart:
        return True
    try:
        started = time.mktime(time.strptime(lstart, '%a %b %d %H:%M:%S %Y'))
    except ValueError:
        return False
    # a second of slack: lstart floors to the second, and the record
    # follows the leader's spawn within the same one
    return started <= recorded_at + 1.0
