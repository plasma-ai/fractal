"""Implements ``Node`` class."""

from __future__ import annotations

import contextlib
import fcntl
import functools
import itertools
import json
import logging
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import typing
from collections.abc import Callable, Iterator
from typing import Any, Optional

import fractal.util
from fractal.constants import (
    CONFIG_FILE,
    DB_FILE,
    FRACTAL_FOLDER,
    HEADLESS_FILE,
    HEADLESS_LOG,
    LOCK_FILE,
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
from .config import (
    DEFAULT_RESERVE_FRACTION,
    RESERVE_PRECISION,
    Config,
    parse_reserve_budget,
)
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

# recorded reason heads of a launch that could not exec: a spawn that never
# started, and a wrapper that ran and exited 127 (the 'command not found'
# convention); both are the class the loop's billing breaker refuses to arm
# on, so the census mirror must disqualify both too
_CANNOT_EXEC_REASONS = ('agent launch failed', 'agent error (exit 127)')

# a headless launch's claimed .pgid record stays empty only until its pid
# write, and the spawned child stops waiting for the pid five seconds in --
# so an empty claim aged past this bound belongs to a launcher that died
# before recording, not to a launch still in flight
_ABANDONED_CLAIM_SECONDS = 60.0

# a fan-out kill refused over a launch claim in flight retries the
# descendant: the claim resolves within the child wrapper's five-second
# pid wait (the pid lands, giving a reapable live group, or the launch
# dies, leaving a sweepable record), so the retry budget outlives that
# window while an abandoned claim cannot loop the sweep past it
_CLAIM_RETRY_LIMIT = 5
_CLAIM_RETRY_SECONDS = 2.0

# sentinel a fan-out _kill returns after refusing over a launch claim in
# flight, so the sweep re-attempts the descendant instead of skipping it
_CLAIM_IN_FLIGHT = 'claim in flight'

# argv shapes a launch pane carries at every exec stage (env/start.sh, then
# the exec'd loop): a pane command bearing one while naming another worktree
# is provably another tree's launch, while a command bearing neither -- an
# operator's shell or editor -- says nothing about which tree owns it
_LAUNCH_ARGV_MARKERS = ('start.sh', 'node _loop')


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
        ambient socket is all there is. A listed name is a candidate, not
        proof: another repo sharing this basename and branch collides on it,
        so the name is arbitrated by pane identity
        (:meth:`_session_is_foreign`) -- a provably foreign session means
        this node's own session is *not* there (a definitive ``False``),
        while ours-or-no-answer keeps the listed answer, so ignorance never
        heals a live loop. ``None`` when the probe is inconclusive --
        :func:`fractal.util.tmux.probe` got no answer from tmux (binary
        absent, or ``list-sessions`` failed for anything but the definitive
        ``no server running``) -- so the reconcile path never mistakes a
        blind host for a dead loop.
        """
        # probe the recorded socket, not whichever one this shell resolves;
        # a parking loop can drop the record mid-probe, and a vanished
        # record leaves only the ambient socket to ask
        socket_file = self.node_dir / SOCKET_FILE
        try:
            socket = socket_file.read_text(encoding='utf-8').strip()
        except FileNotFoundError:
            socket = None
            sessions = fractal.util.tmux.probe()
        else:
            sessions = fractal.util.tmux.probe(socket=socket)
        if sessions is None:
            return None
        if self.tmux_session not in sessions:
            return False
        # the listed name vouches only after the identity check clears it -- a
        # foreign vouch would keep a dead loop active until the foreign session ends
        return not self._session_is_foreign(self.tmux_session, socket=socket)

    def _session_is_foreign(
        self: Node,
        session: str,
        *,
        socket: Optional[str] = None,
    ) -> bool:
        """Return whether a same-named tmux session provably belongs elsewhere.

        Two repos sharing a basename collide on session names, and a headless
        launch owns no session -- but a matching name can also be this node's
        own tmux boot still racing its ``.pgid`` record, which a headless
        fresh start must refuse. The pane pid survives every exec stage of
        the launch (``env _NODE=...``, ``start.sh <worktree>``, ``node _loop
        --path=<worktree>``), so a launch pane's argv names its worktree
        throughout. Every pane of the exact-named session votes: one naming
        this node's worktree makes the session ours, a launch-shaped command
        (:data:`_LAUNCH_ARGV_MARKERS`) naming another worktree is proof of
        another tree's launch, and anything else -- an operator's shell or
        editor, a pane ``ps`` cannot attribute -- is no evidence either way.
        Foreign takes at least one foreign launch pane, no pane of ours, and
        every pane attributed; ours-or-no-answer returns ``False`` --
        ignorance never proves foreignness, so each caller keeps its
        conservative arm (the launch gate refuses; the session probe keeps
        its listed answer). Pane evidence is about one server only, so
        ``socket`` names the server the session was seen on (``None`` asks
        the ambient one).
        """
        # pane evidence comes from the server that vouched for the name;
        # no panes and no answer alike prove nothing, so both of them
        # keep the caller's conservative arm
        pane_pids = fractal.util.tmux.panes(session, socket=socket)
        if not pane_pids:
            return False
        # arbitrate over every pane: the session's loop pane can list after
        # an operator's, and judging the first pane alone would misread the
        # node's own multi-pane session as foreign
        foreign = False
        for pane_pid in pane_pids:
            # the argv carries the worktree path at every stage of the launch
            try:
                result = subprocess.run(
                    ['ps', '-p', pane_pid, '-o', 'command='],
                    capture_output=True,
                    text=True,
                )
            except OSError:
                return False
            command = result.stdout.strip()
            if result.returncode != 0 or not command:
                return False
            if f'{self._root}' in command:
                return False
            if any(marker in command for marker in _LAUNCH_ARGV_MARKERS):
                foreign = True
        return foreign

    @property
    def headless(self: Node) -> bool:
        """Return whether this node uses the detached process backend."""
        return (self.node_dir / HEADLESS_FILE).is_file()

    def _loop_alive(self: Node) -> Optional[bool]:
        """Return whether a live loop still runs this node (``None`` unknown).

        A ``.headless`` launch never joined a tmux server, so its recorded
        process group (``.pgid``, written by :meth:`_launch_headless` before
        the loop boots) is the whole answer and tmux is never asked -- a host
        without tmux still heals it, and a ``.socket`` inherited from a tmux
        shell cannot mislead it. Every other node asks tmux on the loop's
        recorded socket (:meth:`_tmux_session_exists`). A definitive "no such
        session" is proof only for a loop that recorded a socket: ``fractal
        node _loop`` is a supported bare entry point (the harness, a cron
        host) that joins no server, so a socket-less node's own live,
        identity-checked group (:func:`_group_alive`) overrules the probe and
        an unverified group leaves the answer unknown. When tmux gives no
        answer at all, that recorded group is the socket-less node's whole
        answer -- a blind host heals its dead or recycled group exactly as it
        heals a headless one, with the same transplant exposure the headless
        path accepts, and an unverifiable group stays unknown -- while a
        socket-less node with no ``.pgid`` record stays unknown. A
        socket-recorded loop gets no overrule: its group outliving the pane
        is the orphan the reap exists for. ``None`` from either probe never
        reads as gone.
        """
        if self.headless:
            return _group_alive(self.node_dir / PGID_FILE)
        alive = self._tmux_session_exists()
        if not (self.node_dir / SOCKET_FILE).exists():
            # a bare launch is judged by its own group; an unknown group stays unknown
            if alive is False:
                group = _group_alive(self.node_dir / PGID_FILE)
                if group is not False:
                    return group
            # tmux gave no answer: the recorded group is the only witness;
            # a record-less node stays unknown -- a missing record is
            # ignorance, not proof, so a blind host never heals over it
            elif alive is None and (self.node_dir / PGID_FILE).exists():
                return _group_alive(self.node_dir / PGID_FILE)
        return alive

    def _reconcile_status(self: Node, *, locked: bool = False) -> None:
        """Stamp a crashed-but-active node ``exited``.

        A loop that dies without ending (a hard kill, a direct
        ``tmux kill-session``, a headless process death, a host crash) leaves
        the ``.status`` file ``active`` with no live runtime, wedging the
        reject-active guards. The one-loop-per-node invariant (``start.sh``
        refuses to launch while the runtime exists) makes a missing runtime
        proof the loop is gone, so stamp the same honest terminal
        :meth:`Record.run_start` uses for a stranded run -- both the
        ``.status`` file and the crashed run's still-open runs/iters/steps
        rows, so a later merge/delete/retire (none of which start a loop)
        cannot leave the DB reading ``active`` while the status reads
        ``exited``. The settle also heals any config/registry cap drift
        (:meth:`Config.reconcile`): a loop that died before its next iteration
        boundary never ran the boundary reconcile, and the dead row would
        otherwise carry the drift forever. A no-op unless the status is
        ``active``, so a settled node never pays the tmux probe. The heal
        re-verifies at act time -- status still ``active`` and the group
        records untouched since the probe -- and reaps on that snapshot (no
        flock covers the probe), so a continue's relaunch racing the probe
        (its fresh boot rewrites ``.pgid`` before the active stamp) stands
        the heal down instead of being reaped as the crash it replaced. The
        terminal writes run under the ``.worktrees`` flock (``locked`` says
        the caller already holds it -- the flock is not reentrant), where
        the license is re-checked: a status no longer ``active`` or a group
        record that moved since the judged snapshot marks a rival verb's
        settle or a re-armed boot, either of which keeps its stamp. A
        ``paused`` node is never healed: no session is its *normal* parked
        state (the loop exits at pause; ``resume`` relaunches it), on this
        host or after a filesystem transplant to another. An inconclusive
        runtime probe (no answer from tmux or the headless identity check) also
        skips the heal: the reap keys off proof the runtime is gone, never off
        ignorance -- a shell without runtime visibility must not kill a healthy
        loop's process groups, and a shell on a *different* tmux socket gets its
        proof from the loop's recorded one (see :meth:`_tmux_session_exists`).
        A definitive "no such session" is proof only for a loop that recorded
        a socket: a bare ``node _loop`` launch never joined a server, so its
        own live, identity-checked process group overrules the probe -- and
        when tmux gives no answer at all, that recorded group is the
        socket-less node's whole answer, so a blind host still heals a dead
        bare loop -- and a ``.headless`` launch never asks tmux at all
        (:meth:`_loop_alive`).

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
        # fingerprint the group records before the probe: the verdict below
        # licenses action against exactly this state, and a record that moves
        # after the probe marks a relaunch the verdict never judged
        snapshot = self._records_snapshot()
        if self._loop_alive() is not False:
            return
        # re-verify before acting -- no flock covers this probe (signal verbs
        # run the heal under theirs already): a continue that raced the probe
        # re-armed the status, and its fresh boot rewrites .pgid before the
        # active stamp, so either change means the dead verdict no longer
        # describes this node and the heal must stand down
        if self.status() != 'active' or self._records_snapshot() != snapshot:
            return
        self._reap_orphan(snapshot)
        # the reap can stall through its TERM grace while a rival verb
        # settles the node -- a flock'd kill stamping killed, a continue
        # re-arming a fresh boot -- so the license is re-checked under the
        # .worktrees flock before the terminal writes, ordering them against
        # a rival's flock'd stamp instead of racing it
        guard = contextlib.nullcontext() if locked else worktree.lock(self.repo_dir)
        with guard:
            if self.status() != 'active':
                return
            # a record now on disk that differs from the judged snapshot is
            # a re-armed boot the verdict never judged, so it keeps its
            # record and its fresh rows; one the reap itself dropped reads
            # as absent and keeps the heal licensed
            for judged, found in zip(snapshot, self._records_snapshot()):
                if found is not None and found != judged:
                    return
            self.record.close_open('exited')
            self.status_set('exited')
            self.config.reconcile()
            # the heal is the record's catch -- the settled node keeps no
            # socket handle (the next boot writes a fresh one); the .headless
            # record stays, because it names the backend, not the run
            (self.node_dir / SOCKET_FILE).unlink(missing_ok=True)

    def _records_snapshot(
        self: Node,
    ) -> tuple[Optional[tuple[float, str]], ...]:
        """Fingerprint the group records for the heal's act-time re-verify.

        One ``(mtime, content)`` pair per record (``None`` for a missing
        file), so :meth:`_reconcile_status` can prove the state its liveness
        verdict judged is still the state it is about to reap.
        """
        snapshot: list[Optional[tuple[float, str]]] = []
        for name in (PGID_FILE, STEP_PGID_FILE):
            pgid_file = self.node_dir / name
            try:
                recorded_at = pgid_file.stat().st_mtime
                snapshot.append((recorded_at, pgid_file.read_text(encoding='utf-8')))
            except OSError:
                snapshot.append(None)
        return tuple(snapshot)

    def _reap_orphan(
        self: Node,
        snapshot: Optional[tuple[Optional[tuple[float, str]], ...]] = None,
    ) -> None:
        """Kill surviving process groups recorded by a dead loop.

        The loop records its process group at run start (``.pgid``) and
        each agent invocation's own group (``.step_pgid``); both are removed
        on any in-band exit, so a file that outlives the loop runtime marks
        an out-of-band death (tmux kill/crash, headless process death, host
        OOM) whose agent may still be running -- and spending -- unseen. The
        reap acts on the caller's ``snapshot`` (the record state its liveness
        verdict judged; taken fresh when none rides in), never a kill-time
        re-read -- a record a relaunch rewrote since names a live loop, which
        keeps both its group and its record. The reap follows ``kill.sh``'s
        TERM-grace-KILL cadence and logs an ``orphan`` event naming each
        reaped pgid. Best-effort: a dead, recycled, or foreign group reads as
        already gone -- recycled meaning the OS re-issued a dead group's id
        to an unrelated same-user group, which answers a liveness probe but
        is younger than its record (:func:`_recorded_group`).
        """
        if snapshot is None:
            snapshot = self._records_snapshot()
        for name, entry in zip((PGID_FILE, STEP_PGID_FILE), snapshot):
            pgid_file = self.node_dir / name
            if entry is None:
                continue
            recorded_at, content = entry
            # trust the record only while its group is still alive; a record
            # a rival reconciler already swept reads as gone
            try:
                pgid = int(content.strip())
                os.killpg(pgid, 0)
            except (
                ValueError,
                ProcessLookupError,
                PermissionError,
            ):
                pgid = 0
            # aliveness is not identity: a recycled pid answers the probe from
            # an unrelated group -- spare any group younger than its record
            if pgid > 0 and _recorded_group(pgid, recorded_at) is not True:
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
            # drop only the record the reap judged -- one a relaunch rewrote
            # since belongs to the new loop
            try:
                unchanged = (
                    pgid_file.stat().st_mtime == recorded_at
                    and pgid_file.read_text(encoding='utf-8') == content
                )
            except OSError:
                unchanged = False
            if unchanged:
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

        Both answers are the seat's to rewrite, so this resolution is
        never the last word for a guard. A seat that unsets ``_NODE`` and
        steps into a *sibling* worktree resolves to a real but wrong
        node; one that steps outside every worktree resolves to no actor
        and would fail open. Each guard closes that from its own side
        rather than here: the drain adds process-lineage attribution
        (:meth:`drain_lineage` -- the recorded agent process group, which
        moving cannot rewrite), and the mailbox seal sits on the
        owner-only channel rule, which asks what a caller may read rather
        than who it claims to be.

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

    def drain_lineage(self: Node) -> bool:
        """Return whether this process runs inside a draining seat of this tree.

        :meth:`drain_bound` asks who the caller *says* it is, and both
        answers -- the exported ``_NODE`` and the working directory -- are
        the seat's to rewrite (:meth:`resolve_actor`). A seat that scrubs
        the environment and steps into a sibling worktree resolves to a
        real but WRONG node the drain never binds; one that steps outside
        every worktree resolves to no node and fails open. Neither is a
        lie the seat can tell about its *process group*: the loop spawns
        each agent invocation as its own group leader and records the id
        (``.step_pgid``), so every command that seat runs inherits it.
        Ask the tree which of its open runs drain, then whether this
        process sits in one of their seats -- attribution by lineage,
        which no ``env -u`` or ``cd`` rewrites.

        Identity-checked like the reap's (:func:`_recorded_group`), so a
        stale handle naming a recycled id never refuses an unrelated
        caller. Best-effort: an unreadable record or database answers no,
        leaving the other two drain sources to speak.

        Returns:
            Whether this process is a seat of a draining run in this tree.

        """
        try:
            pgid = os.getpgid(0)
        except OSError:
            return False
        # the tree's open runs carrying a drain signal (the central DB is
        # per-tree, so this is exactly the trees this caller could act on)
        query = (
            'SELECT DISTINCT r.node FROM runs r'
            ' JOIN signals s ON s.run_id = r.run_id'
            " WHERE s.signal = 'drain' AND r.ended_at IS NULL"
        )
        try:
            rows = self.db.read(query=query)
        except Exception:
            return False
        for row in rows:
            worktree_dir = fractal.util.git.find_worktree(
                repo_dir=self.repo_dir,
                branch=row['node'],
            )
            if worktree_dir is None:
                continue
            pgid_file = self.__class__(worktree_dir).node_dir / STEP_PGID_FILE
            try:
                recorded_at = pgid_file.stat().st_mtime
                recorded = int(pgid_file.read_text(encoding='utf-8').strip())
            except (OSError, ValueError):
                continue
            if recorded == pgid and _recorded_group(pgid, recorded_at) is True:
                return True
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
            f' {owner} belongs to none of them -- name the tree to act on.'
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
        fork: Optional[str] = None,
        display: Optional[str] = None,
    ) -> None:
        """Validate a template charter's fill-sheet before any spend.

        The stale-seed gate: four commissions once shipped with stale pins,
        stale docket rows, or truncated charters, each costing the node's
        opening seat plus an adjudication. Checks, all pre-worktree:

        - ``--pin`` (when given) resolves to a commit;
        - the charter carries its two authored sections (a truncated seed
          dies here);
        - every ``pin: <sha>`` line in the charter resolves to a commit and
          matches ``--pin`` (prefix-wise, case-blind) when one is given --
          a ``pin:`` spelling that is not a hex sha refuses outright;
        - every ``docket: <path>`` line resolves at the pin (``--pin`` when
          given, else the charter's own, else the child's fork ref).

        Args:
            charter: The template's rendered ``NODE.md``, or ``None``
                (pin-only).
            pin: The commission pin from ``--pin``, or ``None``.
            fork: The child's fork ref -- the docket anchor when neither
                ``pin`` nor a charter ``pin:`` line supplies one.
            display: Charter path for messages -- the repo-relative
                template path, since the bundle's temp path is deleted
                before the user reads a refusal.

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
                    f'Template charter {display or charter} is missing'
                    f' {heading!r} (truncated or stale seed).'
                )
        # every pin: line is load-bearing -- a spelling the gate cannot read
        # as a commit sha (symbolic, or truncated below git's four-hex
        # abbreviation floor) refuses rather than deploying unvalidated
        pins = []
        for declared in re.findall(r'^pin:\s*(.*?)\s*$', text, flags=re.M):
            if not re.fullmatch(r'[0-9a-fA-F]{4,40}', declared):
                raise ValueError(
                    f'Charter pin is not a commit sha: {declared!r} (stale seed).'
                )
            pins.append(declared.lower())
        for declared in pins:
            if not _commit(declared):
                raise ValueError(
                    f'Charter pin does not resolve to a commit: {declared!r}'
                    ' (stale seed).'
                )
            if pin is not None:
                lowered = pin.lower()
                overlaps = lowered.startswith(declared) or declared.startswith(lowered)
                if not overlaps:
                    raise ValueError(
                        f'Charter pin {declared!r} does not match --pin {pin!r}'
                        ' (stale seed).'
                    )
        # docket rows must exist at the pin -- an enumerated surface that
        # moved or never existed is exactly the stale-docket class
        anchor = pin or (pins[0] if pins else fork)
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
        template: Optional[str] = None,
        include: Optional[list[str]] = None,
        exclude: Optional[list[str]] = None,
        values: Optional[PathLike] = None,
        sets: Optional[list[str]] = None,
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
        reserve_budget: Optional[str] = None,
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
            template: Template folder (``<path>[@<ref>]``): a tracked
                folder holding ``config.json``, read at the child's fork
                commit (or at ``<ref>``) and recorded in the node's
                ``_template.toml``. Its surfaces (``NODE.md``, ``steps/``,
                ``scripts/``, ``skills/``, ``agents/``) seed the node; a
                surface it lacks falls back to the inherit-or-package
                source. Its ``config.json`` preset fills each unset
                run-config flag (a flag wins over the preset; the preset
                beats an inherited value).
            include: Template-relative paths to deploy from the template,
                dropping everything else; a directory entry covers its
                subtree. Mutually exclusive with ``exclude`` and recorded
                in ``_template.toml``.
            exclude: Template-relative paths to drop from the template; a
                directory entry covers its subtree. Mutually exclusive
                with ``include`` and recorded in ``_template.toml``.
            values: Slot fill sheet: a TOML file of string values the
                template's ``{{slot}}`` placeholders render with; recorded
                in ``_template.toml``.
            sets: Slot fills as ``KEY=VALUE`` pairs (repeatable); a pair
                wins over the ``values`` sheet.
            pin: Commission pin (a commit sha): must resolve, and every
                ``pin:`` declaration in the template charter must match it
                -- a stale seed dies at init, not at the first seat. Also
                supplies the ``pin`` slot value.
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
            reserve_budget: Budget reserved for cleanup: USD or ``N%`` of
                the merged ``max_cost`` (default ``10%`` of it); shifts when
                the node enters reserve mode (not enforced).
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
        from .agent import command_base, resolve, supported
        from .loop import _STEP_PREFIX
        from .template import (
            collect_values,
            fill,
            locate,
            materialize,
            read_preset,
            trim,
            write_provenance,
        )

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
        if _draining(self):
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
        # resolve the template flag early: the path refusals (a machinery
        # component, a path outside every worktree, '@' in a folder name)
        # need no parent, so a typo dies before any other work; the read
        # itself waits for the fork commit, resolved under the lock below
        template_path: Optional[str] = None
        template_ref: Optional[str] = None
        template_worktree: Optional[pathlib.Path] = None
        template_notice: Optional[str] = None
        template_tmp: Optional[str] = None
        template_bundle: Optional[pathlib.Path] = None
        template_preset: dict[str, Any] = {}
        template_values: dict[str, str] = {}
        if include and exclude:
            raise ValueError('--include cannot be combined with --exclude.')
        if (include or exclude) and template is None:
            raise ValueError('--include and --exclude require --template.')
        if (values is not None or sets) and template is None:
            raise ValueError('--values and --set require --template.')
        if template is not None:
            template_path, template_ref, template_worktree = locate(
                template,
                repo_dir=self.repo_dir,
            )
            # merge the slot values now (later sources win: the --values
            # sheet, then --set, then --pin), so a malformed sheet or pair
            # dies before any other work
            template_values = collect_values(values=values, sets=sets, pin=pin)
        # the fill-sheet gate: a pinned commission must hold together before
        # any spend (--pin alone still validates the pin itself); a template
        # charter validates once the bundle materializes below
        if pin is not None and template is None:
            self._validate_charter(None, pin=pin)
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
            # branch from the target node; the scope is set once the parent is
            # known, since it is spelled relative to the child's own project
            base = meta
        # the base is also the squash-merge target: merge.sh squashes inside the
        # base's checked-out worktree, so a worktree-less base (a typo, or a
        # branch nothing has checked out) would only fail at merge time, long
        # after init printed its success -- refuse now, naming the requirement
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
        # a meta node's scope is the target's seed dir, spelled relative to the
        # child's own project (scope roots resolve against a node's project):
        # bare when the two share a project, prefixed with the target's project
        # from the repo root, and unreachable from any other project
        if meta:
            target_project = worktree.project_path(self.repo_dir, meta)
            # a non-root path names the child's project (default: inherit); a
            # .worktrees/ path is the cwd-in-a-worktree case -- inherit too
            child_project = parent.project_path
            if path is not None and path != '.':
                parts = pathlib.Path(path).parts
                if parts[0] != WORKTREES_FOLDER:
                    child_project = path
            if child_project == target_project:
                scope = [f'{FRACTAL_FOLDER}/{meta}']
            elif child_project == '.':
                scope = [f'{target_project}/{FRACTAL_FOLDER}/{meta}']
            else:
                raise ValueError(
                    f'Meta target {meta!r} lives in project {target_project!r},'
                    f" outside this node's project {child_project!r}; initialize"
                    f' the meta node from the repo root or from {target_project!r}.'
                )
            # the scope list key splits on whitespace, so a root carrying any (a
            # target project dir with a space) would be stored as two roots and
            # the node's edits to the target's seed would fall outside its scope
            if any(char.isspace() for char in scope[0]):
                raise ValueError(
                    f'Meta scope root {scope[0]!r} contains whitespace, which'
                    ' the scope list cannot hold; rename the project directory'
                    ' or init the node without --meta.'
                )
        # the child records the tree's root (inherited from the parent) so any
        # node can resolve the central database from its own config
        root = parent.config.get('root')
        # compose the child branch and probe its pre-existing ref now: the template
        # read below forks from the branch's own tip on a --reset, and the failure
        # rollback must never delete a reused branch's committed history
        child_branch = f'{parent.branch}.{name}'
        cmd = ['show-ref', '--verify', f'refs/heads/{child_branch}']
        pre_existing_branch = fractal.util.git.run(
            cmd,
            cwd=self.repo_dir,
            check=False,
        )
        # the materialized bundle must survive until init.sh consumes it, so
        # everything through the seeding script shares one cleanup scope -- the
        # finally below drops the bundle on success and on every refusal in between
        try:
            # materialize the template at the child's fork commit -- the
            # branch's own tip when it already exists (a --reset), else the
            # base or the parent branch, which is what worktree add checks
            # out -- so the recorded commit is what deploys (the provenance
            # keeps the commit actually read, which can differ from the fork
            # sha when the parent commits before worktree add); every
            # template refusal fires here, before any worktree exists
            if template is not None:
                fork = child_branch if pre_existing_branch else (base or parent.branch)
                rev = template_ref if template_ref is not None else fork
                cmd = ['rev-parse', '--verify', f'{rev}^{{commit}}']
                template_commit = fractal.util.git.run(
                    cmd,
                    cwd=self.repo_dir,
                    check=False,
                )
                if template_commit is None:
                    raise ValueError(
                        f'Template ref does not resolve to a commit: {rev!r}.'
                    )
                template_tmp = tempfile.mkdtemp(prefix='fractal-template-')
                bundle = materialize(
                    worktree=template_worktree,
                    path=template_path,
                    commit=template_commit,
                    dest=pathlib.Path(template_tmp),
                )
                # the folder's config.json is the preset: it fills each
                # unset init flag below (flag wins over preset, preset over
                # the parent's inherited value), so a bad key refuses
                # before any other work
                template_preset = read_preset(bundle, path=template_path)
                # cut the bundle down to the effective set, so downstream
                # only sees what the listing keeps -- every check below
                # judges what actually deploys, so an excluded file can
                # neither refuse the init nor slip past it
                trim(bundle, include=include, exclude=exclude)
                # the effective set's steps must satisfy the loop's
                # discovery contract, so a template that cannot iterate
                # refuses here rather than failing the node's first
                # iteration (a steps/ kept alive by non-step survivors
                # would deploy empty)
                steps_dir = bundle / 'steps'
                if steps_dir.is_dir():
                    # only regular files seed -- init.sh copies the same set
                    step_files = sorted(
                        entry for entry in steps_dir.glob('*.md') if entry.is_file()
                    )
                    if not step_files:
                        raise ValueError(
                            f'Template steps/ contains no step files'
                            f' (*.md): {template_path}'
                        )
                    # the loop discovers steps by their NN- prefix and fails an
                    # iteration on a missing prefix or mixed digit widths
                    widths = set()
                    for step_file in step_files:
                        match = _STEP_PREFIX.match(step_file.name)
                        if match is None:
                            raise ValueError(
                                f'Template steps/ has a step file without'
                                f' an NN- prefix: {step_file.name}'
                            )
                        widths.add(len(match.group(1)))
                    if len(widths) > 1:
                        raise ValueError(
                            f'Template steps/ mixes digit prefix widths:'
                            f' {template_path}'
                        )
                # a bundled surface is a rival source to inheriting the
                # parent's -- refuse the combination rather than pick one
                for surface in ('steps', 'scripts', 'skills'):
                    if inherit and surface in inherit and (bundle / surface).is_dir():
                        raise ValueError(
                            f'--template carries {surface}/; it cannot be'
                            f' combined with --inherit={surface}.'
                        )
                # an agents/ entry deploys only for a registered agent
                # name -- an unknown name (a typo) would deploy nothing,
                # print success, and drift on every later diff, so it
                # refuses here like a credential
                agents_dir = bundle / 'agents'
                if agents_dir.is_dir():
                    supported_agents = ', '.join(supported())
                    for entry in sorted(agents_dir.iterdir()):
                        if entry.name not in supported():
                            raise ValueError(
                                f'Template agents/ names an unknown agent:'
                                f' {entry.name!r}'
                                f' (supported: {supported_agents}).'
                            )
                # the slot pass: render the effective set's {{slot}}
                # placeholders in place -- an unfilled slot or a stray {{
                # refuses here, naming the file and the token
                fill(bundle, path=template_path, values=template_values)
                # the fill-sheet gate on the bundle's rendered charter
                # (--pin alone still validates the pin itself); a pinless
                # docket anchors at the fork ref
                node_md = bundle / 'NODE.md'
                if pin is not None or node_md.is_file():
                    charter = node_md if node_md.is_file() else None
                    self._validate_charter(
                        charter,
                        pin=pin,
                        fork=fork,
                        display=f'{template_path}/NODE.md',
                    )
                # one notice when the root branch's copy differs from the
                # commit read -- a path absent on the root is no notice
                cmd = [
                    'rev-parse',
                    '-q',
                    '--verify',
                    f'{root}:{template_path}',
                ]
                root_tree = fractal.util.git.run(
                    cmd,
                    cwd=self.repo_dir,
                    check=False,
                )
                cmd = [
                    'rev-parse',
                    '-q',
                    '--verify',
                    f'{template_commit}:{template_path}',
                ]
                fork_tree = fractal.util.git.run(
                    cmd,
                    cwd=self.repo_dir,
                    check=False,
                )
                if root_tree is not None and root_tree != fork_tree:
                    template_notice = (
                        f'Notice: template {template_path!r} differs on'
                        f' the root branch; pass'
                        f' --template={template_path}@{root} to read the'
                        " root's copy."
                    )
                # record the provenance into the bundle; init.sh places
                # it with the other bundle surfaces
                write_provenance(
                    bundle,
                    path=template_path,
                    commit=template_commit,
                    values=template_values,
                    include=include,
                    exclude=exclude,
                )
                template_bundle = bundle
            # fill each unset flag from the template's config preset -- the
            # flag wins over the preset, and the preset beats the parent's
            # inherited value below; the package default covers the rest
            if agent is None:
                agent = template_preset.get('agent')
            if provider is None:
                provider = template_preset.get('provider')
            if model is None:
                model = template_preset.get('model')
            if effort is None:
                effort = template_preset.get('effort')
            if max_iters is None:
                max_iters = template_preset.get('max_iters')
            if max_depth is None:
                max_depth = template_preset.get('max_depth')
            if max_children is None:
                max_children = template_preset.get('max_children')
            if max_descendants is None:
                max_descendants = template_preset.get('max_descendants')
            if timeout is None:
                timeout = template_preset.get('timeout')
            if iter_timeout is None:
                iter_timeout = template_preset.get('iter_timeout')
            if step_timeout is None:
                step_timeout = template_preset.get('step_timeout')
            if step_retries is None:
                step_retries = template_preset.get('step_retries')
            if step_retry_backoff is None:
                step_retry_backoff = template_preset.get('step_retry_backoff')
            # sleep and interval are rival pacing keys (the loop rejects
            # both set): the preset fills them only when the spawn sets neither
            if sleep is None and interval is None:
                sleep = template_preset.get('sleep')
                interval = template_preset.get('interval')
            if wait is None:
                wait = template_preset.get('wait')
            if max_cost is None:
                max_cost = template_preset.get('max_cost')
            if max_iter_cost is None:
                max_iter_cost = template_preset.get('max_iter_cost')
            if max_step_cost is None:
                max_step_cost = template_preset.get('max_step_cost')
            if sync is None:
                sync = template_preset.get('sync')
            if detached is None:
                detached = template_preset.get('detached')
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
            # resolve the reserve to USD against the merged ceiling -- the
            # flag's USD-or-N% grammar needs the final max_cost (a percent
            # of a preset ceiling resolves here), while a preset reserve is
            # already a USD number, typed as config.json holds it
            usable = type(max_cost) in (int, float) and max_cost > 0
            ceiling_ok = max_cost is None or usable
            if reserve_budget is None and 'reserve_budget' in template_preset:
                # a reserve without a ceiling is inert, so the preset obeys
                # the same requires-max-cost rule the flag does
                if max_cost is None:
                    raise ValueError(
                        'Template preset reserve_budget requires max_cost'
                        ' (preset or --max-cost).'
                    )
                reserve_budget = template_preset['reserve_budget']
            elif ceiling_ok:
                reserve_budget = parse_reserve_budget(reserve_budget, max_cost)
            else:
                # a degenerate or junk-typed ceiling resolves no reserve -- the
                # merged validation below owns that rejection and its wording
                reserve_budget = None
            # validate the merged run config (the one merged validator:
            # finiteness, positive ceilings, per-iter/step caps needing the
            # run ceiling, reserve range, step <= iter <= run ordering,
            # integer caps, mode-flag types, unit suffixes) only after
            # flag > preset > inherit resolved, so a preset or inherited
            # value obeys exactly the rules the matching flag does -- a pure
            # check of the merged values (no live state), so it stays out of
            # the flock; the live subtree/budget caps
            # (max-children/depth/descendants/cost-remaining) are enforced
            # inside the .worktrees flock below, after a fresh re-read, so
            # concurrent fan-out cannot each pass the check before any of
            # them takes the lock (a TOCTOU race that would defeat the caps)
            config_values = {
                'max_iters': max_iters,
                'max_depth': max_depth,
                'max_children': max_children,
                'max_descendants': max_descendants,
                'timeout': timeout,
                'iter_timeout': iter_timeout,
                'step_timeout': step_timeout,
                'step_retries': step_retries,
                'step_retry_backoff': step_retry_backoff,
                'interval': interval,
                'sleep': sleep,
                'wait': wait,
                'max_cost': max_cost,
                'max_iter_cost': max_iter_cost,
                'max_step_cost': max_step_cost,
                'reserve_budget': reserve_budget,
                'sync': sync,
                'detached': detached,
            }
            self.config.validate(config_values)
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
            # the bundle feeds init.sh's copy loops in place of the
            # inherit-or-package sources for the surfaces it carries
            if template_bundle is not None:
                args.append(f'--bundle={template_bundle}')
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
                        child._reconcile_status(locked=True)
                        if child.status() == 'active':
                            raise RuntimeError(
                                'Cannot reinitialize an active node. Stop or kill it first.'
                            )
                        if child.status() == 'paused':
                            raise RuntimeError(
                                'Cannot reinitialize a paused node.'
                                ' Resume or kill it first.'
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
        finally:
            # the bundle outlives init.sh, never the init call
            if template_tmp is not None:
                shutil.rmtree(template_tmp, ignore_errors=True)
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
        if template_notice:
            notices = f'{notices}\n{template_notice}' if notices else template_notice
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
                " branch names containing '/' are not supported -- switch"
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
                    f' collides with the tree rooted at {other.branch!r} --'
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
                    f' {existing!r}; one branch maps to a single project --'
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
                    f' `node kill` -- rename this repository directory or stop'
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

    def reseed(
        self: Node,
        *,
        ref: Optional[str] = None,
        template: Optional[str] = None,
        force: bool = False,
    ) -> tuple[str, list[str]]:
        """Rewrite the node's seed surfaces from its recorded template.

        Re-renders the recorded folder at its recorded commit -- or at
        ``ref``, or from another folder with ``template`` -- and rewrites
        ``steps/``, ``scripts/``, ``skills/``, and the per-agent files
        from the result: files the node lacks are added and files it has
        are overwritten, so the node matches its template's effective set
        (an excluded file stays gone); nothing is ever deleted, and
        ``NODE.md``, ``config.json``, and ``memory/`` are never touched.
        ``template`` re-points the node: the new path and the commit read
        land in ``_template.toml``, while the recorded values and listing
        ride along unchanged.

        Args:
            ref: Committish to read the recorded folder at (default: the
                recorded commit). Mutually exclusive with ``template``.
            template: Template folder (``<path>[@<ref>]``) to re-point
                the node at, read at the node branch's own tip when the
                value names no ref.
            force: Reseed even while the node is active or paused.

        Returns:
            Tuple of the confirmation message and the stale-listing
            warnings.

        Raises:
            ValueError: If ``ref`` is combined with ``template``, the
                provenance record refuses, a ref does not resolve, or
                the folder is not tracked at the requested ref.
            RuntimeError: If the node is active or paused (without
                ``force``), or the caller runs from the node's own
                worktree.

        """
        from .loop import _STEP_PREFIX
        from .template import (
            fill,
            locate,
            materialize,
            read_provenance,
            trim,
            write_provenance,
        )

        # the two read overrides are rivals: one names a commit for the
        # recorded folder, the other a new folder outright
        if ref is not None and template is not None:
            raise ValueError('--ref cannot be combined with --template.')
        # a node may not edit its own seed: the acting node (the loop's
        # exported _NODE, else the cwd's worktree) must be someone else --
        # the operator outside the worktree, the parent, or another node
        actor = self.resolve_actor()
        if actor is not None and actor.branch == self.branch:
            raise RuntimeError(
                'Cannot reseed a node from its own worktree;'
                ' a node may not edit its own seed.'
            )
        # the guard re-read and the rewrite stay atomic under the .worktrees
        # flock, so a rival verb cannot land between them (the --reset guard)
        with worktree.lock(self.repo_dir):
            # reseed rewrites the live steering surface, so refuse over a
            # running loop or a paused node's frozen run context unless the
            # caller forces it
            self._reconcile_status(locked=True)
            if not force:
                if self.status() == 'active':
                    raise RuntimeError(
                        'Cannot reseed an active node. Stop or kill it first.'
                    )
                if self.status() == 'paused':
                    raise RuntimeError(
                        'Cannot reseed a paused node. Resume or kill it first.'
                    )
            record = read_provenance(self.node_dir)
            path = record['path']
            commit = record['commit']
            template_worktree = self.repo_dir
            rev: Optional[str] = None
            if template is not None:
                # re-point: resolve the folder like init does; the read falls
                # to the node branch's own tip when the value names no ref
                path, template_ref, template_worktree = locate(
                    template,
                    repo_dir=self.repo_dir,
                )
                rev = template_ref if template_ref is not None else self.branch
            elif ref is not None:
                rev = ref
            if rev is not None:
                cmd = ['rev-parse', '--verify', f'{rev}^{{commit}}']
                resolved = fractal.util.git.run(
                    cmd,
                    cwd=self.repo_dir,
                    check=False,
                )
                if resolved is None:
                    raise ValueError(
                        f'Template ref does not resolve to a commit: {rev!r}.'
                    )
                commit = resolved
            # a --ref reads the recorded folder, which may have moved or
            # been retired since: probe it at the ref and name the re-point
            # remedy, which materialize's untracked-at-commit message lacks
            if ref is not None:
                cmd = ['rev-parse', '-q', '--verify', f'{commit}:{path}']
                tree = fractal.util.git.run(cmd, cwd=self.repo_dir, check=False)
                if tree is None:
                    raise ValueError(
                        f'Template folder {path!r} is not tracked at {ref!r};'
                        ' the folder may have moved -- re-point the node with'
                        ' node reseed --template <path>[@<ref>].'
                    )
            # materialize and render the effective set exactly as init does,
            # with the recorded values; a listing entry the template no
            # longer carries only warns (the record may outlive the file)
            template_tmp = tempfile.mkdtemp(prefix='fractal-template-')
            try:
                bundle = materialize(
                    worktree=template_worktree,
                    path=path,
                    commit=commit,
                    dest=pathlib.Path(template_tmp),
                )
                warnings = trim(
                    bundle,
                    include=record.get('include'),
                    exclude=record.get('exclude'),
                    strict=False,
                )
                fill(
                    bundle,
                    path=path,
                    values=record.get('values', {}),
                    remedy=(
                        'add the value to the [values] table in the'
                        " node's _template.toml, or re-init the node"
                        ' with --set'
                    ),
                )
                # reseed adds and overwrites but never deletes, so the
                # loop's discovery contract judges the post-reseed union:
                # the bundle's steps plus the live steps the bundle does
                # not carry -- a renumbered template (or a bad step at the
                # ref) refuses here, before anything rewrites, rather than
                # failing every later iteration behind a clean diff
                steps_dir = bundle / 'steps'
                if steps_dir.is_dir():
                    step_files = sorted(
                        entry for entry in steps_dir.glob('*.md') if entry.is_file()
                    )
                    bundle_names = {entry.name for entry in step_files}
                    live_dir = self.node_dir / 'steps'
                    if live_dir.is_dir():
                        step_files += sorted(
                            entry
                            for entry in live_dir.glob('*.md')
                            if entry.is_file() and entry.name not in bundle_names
                        )
                    widths = set()
                    for step_file in step_files:
                        match = _STEP_PREFIX.match(step_file.name)
                        if match is None:
                            raise ValueError(
                                f'Reseed would leave steps/ with a step'
                                f' file without an NN- prefix:'
                                f' {step_file.name}.'
                            )
                        widths.add(len(match.group(1)))
                    if len(widths) > 1:
                        conflict = ', '.join(
                            sorted(step_file.name for step_file in step_files)
                        )
                        raise ValueError(
                            f'Reseed would leave steps/ with mixed digit'
                            f' prefix widths: {conflict} -- delete the'
                            ' stale files first.'
                        )
                # the script rewrites the file surfaces and the per-agent
                # files from the bundle; Python owns the provenance record
                event_id = self.record.event_start(
                    'reseed',
                    metadata=f'{path}@{commit}',
                )
                try:
                    self._run_script(
                        'reseed.sh',
                        f'{self._root}',
                        f'--bundle={bundle}',
                    )
                except Exception:
                    self.record.event_end(event_id=event_id, status='failed')
                    raise
                # advance the record: the commit actually read, and the path
                # too on a re-point; values and listing ride along unchanged
                write_provenance(
                    self.node_dir,
                    path=path,
                    commit=commit,
                    values=record.get('values', {}),
                    include=record.get('include'),
                    exclude=record.get('exclude'),
                )
                self.record.event_end(event_id=event_id, status='completed')
            finally:
                shutil.rmtree(template_tmp, ignore_errors=True)
        return f'Node reseeded from {path}@{commit}', warnings

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
        headless: bool = False,
    ) -> str:
        """Launch the node loop in tmux or a detached process group.

        Creates a tmux session, or an independent process group in headless
        mode, that runs the iteration loop. All run parameters are read from
        ``config.json`` (set at init or edited before launch); ``continue_run``
        (with its optional ``max_cost`` retune) is the only launch-time action.

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
            headless: Launch without tmux and capture output to the node's
                ``headless.log`` file.

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
        if _draining(self):
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
        # only error on a dying pane or in headless.log
        self.config.validate()
        # resolve the stored agent against the registry the way the loop's
        # boot does (the loop reads this node's own config key, never the
        # ancestor walk) -- the same steering path can typo or drop it, and
        # the loop's registry error would land on the same dying pane or log
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
        if headless:
            args.append('--headless')
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
                    # a parking loop stamps its terminal status before its
                    # exit hook drops the .pgid record, so a continue fired
                    # on that stamp races the teardown -- a live or
                    # unverifiable record, judged by the identity-checked
                    # law, refuses either arm before start.sh could boot a
                    # second loop over it
                    pgid_file = self.node_dir / PGID_FILE
                    alive = _group_alive(pgid_file)
                    if alive is not False:
                        # the parking loop can drop the record between the
                        # probe and this read -- a vanished record is the
                        # exit hook's own proof the loop is gone
                        try:
                            pgid = pgid_file.read_text(encoding='utf-8').strip()
                        except FileNotFoundError:
                            alive = False
                    if alive is not False:
                        if alive is None:
                            raise RuntimeError(
                                'Cannot continue: the process identity probe'
                                f' gave no answer for process group {pgid},'
                                f' so the loop may still be running; check ps'
                                f' -p {pgid} and remove the {PGID_FILE}'
                                ' record from the node directory if that'
                                ' group is not this node.'
                            )
                        raise RuntimeError(
                            f'Cannot continue: node process already exists: {pgid}.'
                        )
                    self.status_set('idle')
                    # keep the boot handoff atomic across runtime backends; roll
                    # a failed launch back so --continue stays the retry path
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
            # serialize the boot handoff across runtime backends: the loop
            # remains idle until its own preflight stamps active, so the lock
            # closes the window where concurrent starts could both launch
            with worktree.lock(self.repo_dir):
                current_status = self.status()
                if current_status != 'idle':
                    raise RuntimeError(
                        f'Cannot start from status: {current_status!r}.'
                        f' Use --continue to restart.'
                    )
                # a first-start node has no tmux session of its own, so this
                # exact name belongs to another fractal sharing the repo name;
                # the check also stops a headless launch racing a tmux boot -- a
                # headless launch owns no session, so a provably foreign one never
                # blocks it (mirrors resume.sh); it still refuses its own tmux
                # boot racing the .pgid record, or a pane the probe cannot attribute
                session = self.tmux_session
                sessions = fractal.util.tmux.probe()
                listed = (sessions is not None) and (session in sessions)
                if listed and not (headless and self._session_is_foreign(session)):
                    if headless:
                        raise RuntimeError(
                            f'Cannot start: the tmux session {session!r} is'
                            " already active and may be this node's own tmux"
                            ' boot still recording its process group. Retry'
                            ' once it settles, or stop that node first.'
                        )
                    raise RuntimeError(
                        f'Cannot start: the tmux session {session!r} is already'
                        f' active for another fractal (a repository sharing this'
                        f' basename and node name). Stop it, or rename one'
                        f' repository directory.'
                    )
                # the mirror race: a headless handoff recorded a group whose
                # loop is still booting (the node stays idle until its
                # preflight stamps active), so a live or unverifiable record
                # -- judged by the identity-checked law -- refuses either arm
                # before start.sh could boot a second loop over it
                pgid_file = self.node_dir / PGID_FILE
                alive = _group_alive(pgid_file)
                if alive is not False:
                    # the parking loop can drop the record between the probe
                    # and this read -- a vanished record is the exit hook's
                    # own proof the loop is gone
                    try:
                        pgid = pgid_file.read_text(encoding='utf-8').strip()
                    except FileNotFoundError:
                        alive = False
                if alive is not False:
                    if alive is None:
                        raise RuntimeError(
                            'Cannot start: the process identity probe gave no'
                            f' answer for process group {pgid}, so the loop'
                            f' may still be running; check ps -p {pgid} and'
                            f' remove the {PGID_FILE} record from the node'
                            ' directory if that group is not this node.'
                        )
                    raise RuntimeError(
                        f'Cannot start: node process already exists: {pgid}.'
                    )
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

    def _launch_headless(
        self: Node,
        *,
        continue_run: bool = False,
        resume: bool = False,
        drain: bool = False,
    ) -> str:
        """Start the loop in its own process group, recording it before it boots.

        The ``start.sh`` headless arm's handoff (``node _launch``). The argv
        pins the invoking interpreter (``sys.executable -m fractal.cli.main``)
        so the loop runs this installation's fractal, not whatever ``fractal``
        a PATH shim or a fronted foreign install resolves to. Writes the
        ``.headless`` marker beside the ``.pgid`` record: the marker is the
        node's backend record -- it outlives the run, survives heals and
        kills, and only a tmux launch clears it -- so it lands only around a
        spawn and always names a backend the node actually launched with.
        The launch claims the ``.pgid`` record with an exclusive create
        before spawning -- no ``.worktrees`` flock covers this handoff
        (resume takes none, and a nested one would deadlock under start's),
        so a launch sidecar flock plus the claim arbitrate two launches
        whose vets both read the same dead-or-absent record -- the flock'd
        clear re-vets the record, so a rival winner's pid landed since a
        loser's stale vet refuses rather than sweeps -- and an empty
        claim abandoned by a launcher that died before its pid write clears
        by age rather than wedging every relaunch. The child waits for the
        pid to land in the claimed record before exec'ing the loop, so no
        probe ever sees a booted headless loop without its record
        (:func:`_group_alive`), and
        the launch banner is flushed before that record lands, so it always
        precedes the loop's first output in ``headless.log`` -- which appends
        across launches, one banner line per launch. A failed or stalled
        spawn drops the record and rolls the marker back
        to its prior state (a pre-existing marker names an earlier headless
        launch, and a failure must not rewrite the recorded backend), so
        ``--continue`` stays the retry path. A recorded group the same
        identity-checked law judges alive refuses the launch -- the loop is
        still booting, running, or parking, and proceeding would clobber the
        one record that can reap it -- and an unverifiable group refuses
        naming the ``ps`` check, so ignorance never authorizes a second boot.

        Returns:
            Confirmation message naming the pid and the log.

        Raises:
            RuntimeError: If the recorded process group is still alive, or
                its identity cannot be verified.

        """
        log_path = self.node_dir / HEADLESS_LOG
        pgid_file = self.node_dir / PGID_FILE
        # vet the recorded group before touching the log or the record; the
        # refusal wordings match the surfaces they serve on the script path
        # (start.sh's second-launch refusal, resume.sh's retry-never-kill
        # guidance for a loop that is still parking)
        alive = _group_alive(pgid_file)
        if alive is not False:
            # the parking loop can drop the record between the probe and
            # this read -- a vanished record is the exit hook's own proof
            # the loop is gone
            try:
                pgid = pgid_file.read_text(encoding='utf-8').strip()
            except FileNotFoundError:
                alive = False
        if alive is not False:
            if alive is None:
                raise RuntimeError(
                    'the process identity probe gave no answer for process'
                    f' group {pgid}, so the loop may still be running; check'
                    f' ps -p {pgid} and remove the {PGID_FILE} record from the'
                    ' node directory if that group is not this node'
                )
            if resume:
                raise RuntimeError(
                    f'the loop is still running or parking: {pgid}; retry once it exits'
                )
            raise RuntimeError(f'headless node process already exists: {pgid}')
        # claim the record exclusively before the spawn: the vet alone cannot
        # arbitrate two launches racing each other (resume holds no flock, and
        # a nested .worktrees flock would deadlock under start's, which spans
        # this handoff), so the O_EXCL create is the single-boot gate and the
        # loser refuses exactly as it would over a live record
        claimed = (
            'the loop is still running or parking: a rival launch claimed'
            ' the record; retry once it exits'
            if resume
            else 'headless node process already exists: a rival launch'
            ' claimed the record'
        )
        # the clear-claim-spawn-record sequence below is not atomic on its
        # own -- two rivals over the same dead record can both parse it
        # before either clears, and the loser's clear would sweep the
        # winner's fresh claim -- so a launch sidecar flock (kernel-held,
        # auto-released if the launcher dies, never nested inside another
        # flock) serializes the whole handoff through the pid write; the
        # loser refuses exactly as it would over a rival's claim
        lock_path = self.node_dir / (PGID_FILE + LOCK_FILE)
        with open(lock_path, 'a', encoding='utf-8') as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError(claimed) from None
            # a record the vet judged dead clears for the claim; one it
            # could not even parse is a rival's claim in flight (the pid
            # lands only after the spawn), refused like a live record rather
            # than swept -- unless the claim is provably abandoned by its
            # age, in which case it clears like a dead group
            try:
                pgid = pgid_file.read_text(encoding='utf-8').strip()
                int(pgid)
            except FileNotFoundError:
                pass
            except ValueError:
                if _claim_in_flight(pgid_file, pgid):
                    raise RuntimeError(claimed) from None
                pgid_file.unlink(missing_ok=True)
            else:
                # the pre-flock vet is stale by now: a record that parses
                # here may be a rival winner's pid, landed after that vet
                # and released with its flock, so only a group the
                # identity-checked law judges dead clears for the claim; an
                # unverifiable group names the ps check -- a retry cannot
                # arbitrate an identity ps gave no answer for
                revetted = _group_alive(pgid_file)
                if revetted is None:
                    raise RuntimeError(
                        'the process identity probe gave no answer for process'
                        f' group {pgid}, so the loop may still be running; check'
                        f' ps -p {pgid} and remove the {PGID_FILE} record from the'
                        ' node directory if that group is not this node'
                    )
                if revetted:
                    raise RuntimeError(claimed)
                pgid_file.unlink(missing_ok=True)
            try:
                claim = os.open(
                    f'{pgid_file}',
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o644,
                )
            except FileExistsError:
                raise RuntimeError(claimed) from None
            os.close(claim)
            # build the loop argv
            loop_args = [
                sys.executable,
                '-m',
                'fractal.cli.main',
                'node',
                '_loop',
                f'--path={self._root}',
            ]
            if continue_run:
                loop_args.append('--continue')
            if resume:
                loop_args.append('--resume')
            if drain:
                loop_args.append('--drain')
            # hold the exec until the pid lands in the claimed record, so the
            # group is probeable first (-s: the bare claim is not yet a record)
            wrapper = (
                'for _ in {1..500}; do [[ -s "$1" ]] && break; sleep 0.01; done; '
                '[[ -s "$1" ]] || exit 1; shift; exec "$@"'
            )
            command_args = ['bash', '-c', wrapper, 'bash', f'{pgid_file}', *loop_args]
            # the banner names the launch kind, so a post-mortem reads which
            # relaunch produced each appended tail
            if resume:
                kind = 'resume'
            elif continue_run:
                kind = 'continue'
            else:
                kind = 'start'
            if drain:
                kind += ' drain'
            marker = self.node_dir / HEADLESS_FILE
            recorded = marker.exists()
            try:
                marker.write_text('headless\n', encoding='utf-8')
                with log_path.open('a', encoding='utf-8') as stream:
                    process = subprocess.Popen(
                        command_args,
                        stdin=subprocess.DEVNULL,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    stream.write(
                        f'=== Launched {kind} at {fractal.util.time.utc_now()}'
                        f' (pid {process.pid}) ===\n'
                    )
                    stream.flush()
                pgid_file.write_text(f'{process.pid}\n', encoding='utf-8')
                # the wrapper holds the exec only so long: a child that gave up
                # waiting exits before the loop boots, and reporting success
                # would strand the node idle behind a dead record
                if process.poll() is not None:
                    raise RuntimeError(
                        f'the headless launch exited before boot (log: {log_path})'
                    )
            except Exception:
                if not recorded:
                    marker.unlink(missing_ok=True)
                pgid_file.unlink(missing_ok=True)
                raise
            return f'Started headless node: {process.pid} (log: {log_path})'

    def attach(self: Node) -> None:
        """Attach to the node's tmux session."""
        # validate status
        if self.status() != 'active':
            raise RuntimeError('Cannot attach: node is not active.')
        if self.headless:
            raise RuntimeError(
                'Cannot attach to a headless node; follow its log instead:'
                f' tail -f {self.node_dir / HEADLESS_LOG}'
            )
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
        locked: bool = False,
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
            locked: The caller already holds the ``.worktrees`` flock.

        Returns:
            The current run's id, or ``None`` when a fan-out call is
            refused.

        Raises:
            RuntimeError: If the node is not active or has no run.

        """
        # reconcile a crashed-but-active node so it hits the clear
        # not-active guard below, not the misleading no-run error
        self._reconcile_status(locked=locked)
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

    def _fan_out(self: Node, verb: str, reason: str) -> int:
        """Signal every active descendant, re-enumerating to a fixpoint.

        A single pass covers only the descendants live when it started, so
        a child whose ``start`` was in flight -- ``idle`` for the moment
        between ``node start`` returning and its loop's flock'd ``active``
        stamp -- got no signal row at all and never learned: under ``stop``
        it runs on unattended after its manager settles, and under
        ``finish`` the parent's drain-wait then blocks on a child that was
        never told to finish. Re-read until no fresh live descendant
        appears, the way :meth:`kill` and :meth:`pause` already do; each
        branch is attempted exactly once, so the sweep converges even when
        a child never settles, and each helper guards its own node under
        the flock (a descendant that settled mid-sweep is skipped, refusal
        recorded). The rest of the window closes at the child's own end: a
        loop booting under a pending ancestor signal adopts it
        (:meth:`cascade_latched`).

        Args:
            verb: ``'stop'`` or ``'finish'``.
            reason: The attributed reason for the descendants' signal rows.

        Returns:
            How many descendants the sweep signaled.

        """
        signaled = 0
        attempted: set[str] = set()
        while True:
            fresh = [
                (row['node'], descendant)
                for row, descendant in self._live_descendants(status='active')
                if row['node'] not in attempted
            ]
            if not fresh:
                break
            for branch, descendant in fresh:
                attempted.add(branch)
                if verb == 'stop':
                    descendant._stop(reason, fan_out=True)
                else:
                    descendant._finish(reason, fan_out=True)
                signaled += 1
        return signaled

    def cascade_latched(self: Node) -> Optional[tuple[str, str]]:
        """Return the nearest ancestor winding this node's subtree down.

        The graceful-signal twin of :meth:`pause_latched`: ``stop`` and
        ``finish`` fan out over the descendants live when they sweep, so a
        node whose own start was in flight is reachable only from its own
        end -- at boot it asks whether an ancestor is still ``active``
        carrying a pending ``stop`` or ``finish``, and adopts it
        (:meth:`Loop._adopt_cascade`). Walks by name so a pruned
        intermediate never hides a winding-down ancestor, nearest first,
        and ``stop`` outranks ``finish`` on one node (it ends the run
        sooner). The node itself is skipped -- a run this loop just opened
        carries no signal of its own -- and so is the user node, which runs
        no loop and records no signal for its tree-wide broadcast.

        Returns:
            The latching ancestor's branch and its pending signal, or
            ``None`` when the path is clear.

        """
        for node in self._self_and_ancestors():
            if node is self or node.is_user:
                continue
            if node.status() != 'active':
                continue
            for signal in ('stop', 'finish'):
                if node.record.signal_get(signal) is not None:
                    return node.branch, signal
        return None

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
            finished_count = self._fan_out('finish', propagated)
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
        # finish descendants first, then self -- the sweep re-enumerates to a
        # fixpoint so a descendant whose start was in flight is reached too
        self._fan_out('finish', propagated)
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
            guard = self._signal_guard('finish', 'finish', fan_out=fan_out, locked=True)
            if guard is None:
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
            stopped_count = self._fan_out('stop', propagated)
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
        # stop descendants first, then self -- the sweep re-enumerates to a
        # fixpoint so a descendant whose start was in flight is reached too
        self._fan_out('stop', propagated)
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
            if self._signal_guard('stop', 'stop', fan_out=fan_out, locked=True) is None:
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

        Reaps each loop runtime and marks its active rows ``killed``. Paused
        nodes are killable -- the escape hatch for a parked subtree; with no
        loop alive the kill is pure bookkeeping (``kill.sh`` no-ops and the
        open rows close ``killed``). Idle nodes are killable too
        (:meth:`_killable`) -- a spawn in flight when the kill lands is
        reaped, not skipped, and a never-started spawn is stamped
        ``killed`` so it can never activate. A descendant refused over a
        launch claim in flight (its record empty until the pid lands) is
        retried within a bounded budget once the claim resolves, so a
        resume racing the sweep cannot boot a survivor under a killed
        parent; a claim that outlives the budget stands the sweep down
        with a warning naming the survivor and the manual follow-up.

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
        #   reaped at most once and the subtree is finite -- a claim-in-flight
        #   refusal re-admits its branch, but only within its retry budget),
        #   so it converges even if a stuck child never settles; each helper
        #   guards its own node under the flock, so a descendant that
        #   settled mid-sweep is skipped (refusal recorded) while the
        #   self-act refusal raises
        seen: set[str] = set()
        claims: dict[str, int] = {}
        while True:
            fresh = [
                (row, descendant)
                for row, descendant in self._live_descendants()
                if descendant._killable(row['status']) and row['node'] not in seen
            ]
            if not fresh:
                break
            retry = False
            for row, descendant in fresh:
                seen.add(row['node'])
                try:
                    swept = descendant._kill(propagated, fan_out=True)
                except Exception:
                    # best-effort: surface the failure but keep reaping the
                    # rest of the subtree -- a stuck child must not leave its
                    # siblings or the parent running
                    self.log(
                        message=f'Warning: failed to kill {descendant._root}',
                        level=logging.WARNING,
                    )
                    continue
                # a refusal over a launch claim in flight resolves within
                # the claim's bound -- the pid lands, giving a reapable
                # group, or the launch dies and its record ages out -- so
                # the descendant is re-attempted rather than skipped for
                # good; the budget stands the sweep down over an abandoned
                # claim instead of looping on it
                if swept is _CLAIM_IN_FLIGHT:
                    claims[row['node']] = claims.get(row['node'], 0) + 1
                    if claims[row['node']] <= _CLAIM_RETRY_LIMIT:
                        seen.discard(row['node'])
                        retry = True
                    else:
                        # the budget is spent and the sweep stands down --
                        # the survivor must surface, never ride the kill's
                        # success confirmation invisibly
                        self.log(
                            message=(
                                f'Warning: failed to kill {descendant._root}:'
                                f' its launch claim ({PGID_FILE}) is still in'
                                ' flight; retry fractal node kill once the'
                                ' pid lands'
                            ),
                            level=logging.WARNING,
                        )
            if retry:
                time.sleep(_CLAIM_RETRY_SECONDS)
        return self._kill(reason)

    def _kill(
        self: Node,
        reason: Optional[str] = None,
        *,
        fan_out: bool = False,
    ) -> str:
        """Kill this node only and mark its active rows ``killed``.

        The status re-read, process-group vet, and kill's event/signal writes
        stay atomic under the ``.worktrees`` flock; ``kill.sh`` and the row
        marking run outside the lock. An idle or paused target is also stamped
        ``killed`` under that flock: neither has a terminal to clobber or a
        session the stamp could orphan, and only a stamp that precedes the
        reap lets the loop's flock'd boot check stand a racing start or
        resume down -- stamped after, the relaunched loop boots in the
        window, kill.sh has already no-op'd over the park, and nothing ever
        reaps it (the loop never polls the kill signal). An unverifiable recorded group
        refuses before any kill state is written, naming the ``ps`` check and
        the record to clear. The attribution -- ``killed by <actor>``, with
        the reason appended when one is given -- lands identically on the kill
        event, the ``kill`` signal, and the killed run row, so every surface
        answers who ended the run; a never-started spawn has none of the
        latter two, and its event alone carries the attribution.
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
            # vet the recorded process groups before writing kill state: kill.sh
            # gates on liveness alone, and a recycled id (the OS re-issued a dead
            # group's id to an unrelated same-user group) answers that probe --
            # drop a record whose group is gone or not the one it named, and
            # refuse on an unverifiable one before the node can claim a
            # still-running loop was killed
            for name in (PGID_FILE, STEP_PGID_FILE):
                pgid_file = self.node_dir / name
                alive = _group_alive(pgid_file)
                if alive is None:
                    # the parking loop can drop the record between the probe
                    # and this read -- a vanished record leaves nothing to
                    # arbitrate, so the reap proceeds over it
                    try:
                        pgid = pgid_file.read_text(encoding='utf-8').strip()
                    except FileNotFoundError:
                        continue
                    self._signal_refuse(
                        verb='kill',
                        event='kill',
                        reason=(
                            'the process identity probe gave no answer for process'
                            f' group {pgid}, so the loop may still be running; check'
                            f' ps -p {pgid} and remove the {name} record from the node'
                            ' directory if that group is not this node'
                        ),
                        fan_out=fan_out,
                    )
                    return ''
                if alive is False:
                    # a False verdict also covers a record still empty
                    # mid-claim (a launch writes the pid only after its
                    # spawn) -- sweep only a record naming a pid, and refuse
                    # the claim in flight like a live record
                    try:
                        content = pgid_file.read_text(encoding='utf-8').strip()
                    except FileNotFoundError:
                        continue
                    try:
                        int(content)
                    except ValueError:
                        self._signal_refuse(
                            verb='kill',
                            event='kill',
                            reason=(
                                f'the {name} record names no process group'
                                ' yet, so a launch may be claiming it; retry'
                                f' once its pid lands, or remove the {name}'
                                ' record from the node directory if no'
                                ' launch is running'
                            ),
                            fan_out=fan_out,
                        )
                        # the sentinel (never a message) tells the fan-out
                        # sweep this refusal resolves shortly, so the
                        # descendant is retried rather than skipped
                        return _CLAIM_IN_FLIGHT
                    pgid_file.unlink(missing_ok=True)
            # set signal and log event; the reap runs outside the lock
            event_id = self.record.event_start('kill', metadata=label)
            # a never-started spawn has no run for a signal to hang off -- the
            # kill event and the killed stamp are its whole record, so skip
            # the write rather than warn over a reap that worked
            if self.record.runs(limit=1):
                self.record.signal_set('kill', label)
            # an idle or paused target is stamped here, not after the reap --
            # see the docstring: the boot-window race is only closed
            # flock-to-flock, and neither state has a runtime the early
            # stamp could clobber
            if current in ('idle', 'paused'):
                self.status_set('killed')
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
            guard = self._signal_guard('pause', 'pause', fan_out=fan_out, locked=True)
            if guard is None:
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
        if _draining(self):
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

    def _merge_target(self: Node) -> str:
        """Return the merge target as ``merge.sh`` resolves it, or ``''``.

        The ``base`` config wins; otherwise a dotted branch merges into its
        parent, and an undotted, base-less branch has no target.
        """
        target = self.config.get('base') or ''
        if not target and '.' in self.branch:
            target, *_ = self.branch.rsplit('.', 1)
        return target

    def merge(
        self: Node,
        *,
        continue_merge: bool = False,
        ignore_scope: bool = False,
    ) -> tuple[str, str]:
        """Squash-merge the node's branch into its merge target.

        ``merge.sh`` resolves the target -- the node's configured ``base`` if
        set (e.g. a meta node merging back into the node it optimizes), else
        the dotted parent (the branch minus its last segment) -- runs the
        squash in the target's worktree, and logs the ``merge`` event there so
        the record survives this node's later deletion.

        The full commit history is preserved on the node's
        branch; only a single squash commit lands on the target. The squash
        never changes the target's ``.fractal/`` outside this node's scope
        roots, refuses paths outside the node's commit boundaries (the law
        ``fractal commit`` enforces) unless ``ignore_scope`` is set, and
        ends by recording the target's post-squash tree on the node's branch
        so the node holds the adjudicated content and a later re-merge diffs
        only new work.

        ``continue_merge`` finishes a hand-resolved squash after a conflicted
        merge: the operator redoes ``git merge --squash`` in the target
        worktree, resolves and stages the conflicts, and the continue then
        runs the merge's own tail -- ``.fractal/`` restore and seed strip,
        footprint check, index refresh, commit, merge-base advance -- so a
        manual resolution never has to hand-roll those steps (a hand-rolled
        seed strip leaves working-tree residue).

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
        target_branch = self._merge_target()
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
        if ignore_scope:
            args.append('--ignore-scope')
        # the target's user-ness from the repo's record: a root checked out in
        # a linked worktree carries no self-ignored seed there to probe
        if any(user.branch == target_branch for user in Node.user_nodes(self.repo_dir)):
            args.append('--user-target')
        # one squash at a time per repo: two sibling merges racing into the
        # same target interleave their index writes and leave it half-merged
        with worktree.merge_lock(self.repo_dir):
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

    def unmerged_warning(self: Node, *, target: Optional[str] = None) -> str:
        """Return the unmerged-work warning for deleting this node, or ``''``.

        The judgment ``delete.sh`` makes before its teardown -- the paths the
        branch changed since its merge-base with its merge target, minus its
        seed and the wiki's generated state, still differing on the target --
        so the CLI can show it before the confirmation, while the branch still
        exists to merge (mirrors ``delete.sh``'s unmerged check).

        Args:
            target: Branch to judge against (default: the node's base, else
                its dotted parent). A subtree teardown passes the deletion
                root's surviving target for each descendant, as
                :meth:`delete` threads it into ``delete.sh``.

        Returns:
            The warning line, or ``''`` when nothing would be discarded.

        """
        if target is None:
            target = self._merge_target()
        if not target:
            return ''
        repo_dir = self.repo_dir
        # nothing to judge without an existing target that shares history
        cmd = ['show-ref', '--verify', '--quiet', f'refs/heads/{target}']
        if fractal.util.git.run(cmd, cwd=repo_dir, check=False) is None:
            return ''
        cmd = ['merge-base', target, self.branch]
        base = fractal.util.git.run(cmd, cwd=repo_dir, check=False)
        if not base:
            return ''
        # already merged: every commit is reachable from the target
        cmd = ['merge-base', '--is-ancestor', self.branch, target]
        if fractal.util.git.run(cmd, cwd=repo_dir, check=False) is not None:
            return ''
        project = self.project_path
        seed = FRACTAL_FOLDER if project == '.' else f'{project}/{FRACTAL_FOLDER}'
        wiki = 'wiki' if project == '.' else f'{project}/wiki'
        # a scope root that is, or lies under, a .fractal dir is work the merge
        # lands (a --meta node's scope is the target's own seed dir), so exclude
        # only the node's own seed and its descendants' instead of the whole
        # .fractal/ (mirrors delete.sh; '.' collapses as commit.scope_boundaries)
        roots = self.config.get('scope') or []
        if any(not pathlib.PurePosixPath(root).parts for root in roots):
            roots = []
        if project != '.':
            roots = [f'{project}/{root}' for root in roots]
        seed_scoped = False
        for root in roots:
            is_seed = (root == FRACTAL_FOLDER) or root.endswith(f'/{FRACTAL_FOLDER}')
            if is_seed or (f'{FRACTAL_FOLDER}/' in root):
                seed_scoped = True
        if seed_scoped:
            excludes = [
                f':(exclude){seed}/{self.branch}',
                f':(exclude,glob)**/{FRACTAL_FOLDER}/{self.branch}.*/**',
            ]
        else:
            excludes = [f':!{seed}']
        # list the branch's own changes, minus its seed and the wiki's generated state
        cmd = [
            'diff',
            '--name-only',
            '-z',
            base,
            self.branch,
            '--',
            *excludes,
            f':(exclude,glob){wiki}/**/_index.md',
            f':!{wiki}/.wiki',
        ]
        raw = fractal.util.git.run_bytes(cmd, cwd=repo_dir) or b''
        changed = [path for path in os.fsdecode(raw).split('\0') if path]
        if not changed:
            return ''
        # only paths still differing on the target would be discarded
        specs = [f':(literal){path}' for path in changed]
        cmd = ['diff', '--quiet', target, self.branch, '--', *specs]
        if fractal.util.git.run(cmd, cwd=repo_dir, check=False) is not None:
            return ''
        # word for word the line delete.sh prints: the CLI drops the script's
        # copy by equality, so the two must never drift apart
        return (
            f'Warning: {self.branch} has commits not merged into {target};'
            ' deleting discards them (merge first to keep them)'
        )

    def unmerged_warnings(self: Node) -> list[str]:
        """Return the unmerged-work warnings for deleting this subtree.

        The node's own, then each live descendant's judged against the
        node's surviving target -- a descendant's own parent dies in the
        same teardown -- the judgment :meth:`delete` threads into
        ``delete.sh``; empties are dropped.
        """
        target = self._merge_target()
        warnings = [self.unmerged_warning()]
        for _, descendant in self._live_descendants():
            warnings.append(descendant.unmerged_warning(target=target))
        return [warning for warning in warnings if warning]

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
            # retired accepts only unretire and delete: a second retire would
            # record 'retired' as the prior status and lose the real one
            if self.status() == 'retired':
                raise RuntimeError('Cannot retire: node is already retired.')
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
        while any in-scope node's loop runtime is alive; paused nodes are
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
            # anchor the named tree explicitly, never by inference -- a
            # scoped teardown keyed to the wrong tree's DB would guard
            # and prune a healthy sibling
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
        any of the tree's loop runtimes is alive; paused nodes are killed as
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
        own refusals first -- a live loop runtime or a locked worktree
        anywhere in the trees in scope -- because the paused settle below is
        irreversible (the kills close the parked runs), so a teardown the
        script would refuse must abort here with nothing touched. An
        inconclusive liveness probe refuses only while the row could still
        hide a runtime: any unsettled status, or a lingering ``.pgid``,
        ``.socket``, or ``.headless`` record. A settled row keeping none of
        those has nothing left to protect -- the state the caller's own
        pre-teardown reconcile leaves after healing a dead bare loop on a
        blind host -- so the teardown proceeds over it. Then
        settles frozen work: a paused node has no loop runtime for the
        liveness refusal to catch, and the confirmed teardown already
        authorized discarding its frozen mid-step work, so each parked
        node is killed -- pure bookkeeping with no loop alive: the open
        rows close ``killed`` and the attribution names the verb --
        rather than bouncing the operator through a manual kill sweep.
        A parked node refused over a resume's launch claim in flight is
        retried within kill's claim budget; an exhausted claim warns, so
        the script's paused re-check refusal is attributable.
        Then runs the script under the ``.worktrees`` flock so its
        worktree remove/prune does not race a concurrent init/delete (the
        same lock ``child_add`` takes) -- but only when ``.worktrees``
        exists; creating it would defeat the script's nothing-to-tear-down
        check, which keys off that directory. The script re-checks every
        guard under the lock, backstopping a runtime, lock, or pause that
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
            # runtime): one pass per guard, mirroring the script's ordering
            descendants = tree._live_descendants()
            # probe each node through the one liveness law: a socket-recording
            # loop through the server it recorded at boot, a headless or
            # socket-less loop through its recorded process group (see
            # _loop_alive); an inconclusive answer means the node may still be
            # running, so the irreversible teardown refuses rather than tearing
            # down blind -- unless the row is settled and keeps no group
            # record, socket record, or backend marker, in which case nothing
            # is left for the refusal to protect
            for row, descendant in descendants:
                alive = descendant._loop_alive()
                pgid_file = descendant.node_dir / PGID_FILE
                if alive is None:
                    settled = ('completed', 'stopped', 'exited', 'killed', 'retired')
                    socketed = (descendant.node_dir / SOCKET_FILE).exists()
                    evidence = descendant.headless or socketed or pgid_file.exists()
                    if row['status'] in settled and not evidence:
                        continue
                    raise RuntimeError(
                        f'Cannot {verb}: the runtime probe gave no answer for'
                        f' {descendant.branch}, so it may still be running. Check'
                        ' tmux list-sessions (on the socket in .socket when one is'
                        f' recorded) and ps -p against {pgid_file}, then retry.'
                    )
                if alive:
                    no_socket = not (descendant.node_dir / SOCKET_FILE).exists()
                    group_present = pgid_file.exists()
                    group_backed = descendant.headless or (no_socket and group_present)
                    if group_backed:
                        # the parking loop can drop the record between the
                        # probe and this read -- a vanished record is the
                        # exit's own proof this node is settling
                        try:
                            pgid = pgid_file.read_text(encoding='utf-8').strip()
                        except FileNotFoundError:
                            continue
                        runtime = f'as process group {pgid}'
                        if descendant.headless:
                            runtime += f' (log: {descendant.node_dir / HEADLESS_LOG})'
                    else:
                        runtime = f'in tmux ({descendant.tmux_session})'
                    raise RuntimeError(
                        f'Cannot {verb}: node is still running {runtime}.'
                        f' Kill it first with: fractal node kill'
                        f' {descendant.branch}.'
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
        # takes the same flock); best-effort per node (mirrors kill's sweep,
        # claim retry included -- a resume's launch claim in flight resolves
        # within the budget, and an exhausted one warns so the script's paused
        # re-check abort is attributable), the script's paused re-check backstops
        for tree in trees:
            if not tree.exists():
                continue
            seen: set[str] = set()
            claims: dict[str, int] = {}
            while True:
                fresh = [
                    (row, descendant)
                    for row, descendant in tree._live_descendants(status='paused')
                    if row['node'] not in seen
                ]
                if not fresh:
                    break
                retry = False
                for row, descendant in fresh:
                    seen.add(row['node'])
                    try:
                        settled = descendant._kill(f'{verb} teardown', fan_out=True)
                    except Exception:
                        tree.log(
                            message=f'Warning: failed to kill {descendant._root}',
                            level=logging.WARNING,
                        )
                        continue
                    if settled is _CLAIM_IN_FLIGHT:
                        claims[row['node']] = claims.get(row['node'], 0) + 1
                        if claims[row['node']] <= _CLAIM_RETRY_LIMIT:
                            seen.discard(row['node'])
                            retry = True
                        else:
                            tree.log(
                                message=(
                                    'Warning: failed to kill'
                                    f' {descendant._root}: its launch claim'
                                    f' ({PGID_FILE}) is still in flight, so'
                                    f' the {verb} will refuse over the'
                                    ' paused node; retry once the claim'
                                    ' resolves'
                                ),
                                level=logging.WARNING,
                            )
                if retry:
                    time.sleep(_CLAIM_RETRY_SECONDS)
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
            # which is still done-conditions-met) -- name the run-out and
            # a dead final iteration so a census never reads either as a
            # clean done-conditions-met end
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

    def end_reason(self: Node) -> Optional[str]:
        """Return the typed token naming why a settled node's run ended.

        The machine counterpart of :meth:`status_detail`'s prose: a
        closed vocabulary read off the latest run row's typed facts and
        the exact reason strings the loop itself records, never composed
        prose split back apart. ``completed`` rows read ``goal_met`` (a
        drained finish -- a cap-overshoot note is still
        done-conditions-met), ``run_exhausted`` (the iteration cap), or
        ``final_iteration_failed`` (a drained finish whose last
        iteration died). ``exited`` rows read ``cost_budget`` --
        exited/0 is the DB-level budget-landing discriminator, every
        other exited path keeps 1 -- ``timeout``, ``setup_abort``, or
        ``final_iteration_failed`` (the iteration cap landing on a dead
        final iteration); any other recorded reason (an unexpected exit,
        a kill/retire that beat the boot, a failed resume preflight)
        maps ``other``, so ``None`` keeps meaning nothing recorded (a
        reconcile-healed crash closes its rows reason-less). Every other
        status reads ``None``. Reads the stored status without
        reconciling: a crashed-but-active node's open run carries no
        reason before the heal and none after it.

        Returns:
            The token, or ``None`` when no run reason is recorded.

        """
        status = self.status()
        if status not in ('completed', 'exited'):
            return None
        rows = self.record.runs(limit=1)
        if not rows or rows[0]['status'] != status:
            return None
        reason = rows[0]['metadata'] or ''
        if status == 'exited':
            # the budget landing is typed by its exit code alone -- the
            # recorded reason quotes free-form figures
            if rows[0]['exit_code'] == 0:
                return 'cost_budget'
            if reason.startswith('Timed out at iteration '):
                return 'timeout'
            if reason.startswith('setup failed x'):
                return 'setup_abort'
            exhausted = reason.startswith('Reached max iterations')
            if exhausted and reason.endswith('final iteration failed'):
                return 'final_iteration_failed'
            return 'other' if reason else None
        # completed: the run-out and the dead final iteration are named
        # so neither reads as a clean done-conditions-met end (mirrors
        # the status_detail discrimination)
        if reason.startswith('Reached max iterations'):
            return 'run_exhausted'
        if reason.endswith('final iteration failed'):
            return 'final_iteration_failed'
        return 'goal_met'

    def _billing_backoff(self: Node) -> bool:
        """Return whether the newest launches carry the billing signature.

        Three or more consecutive newest closed launches, each failed,
        instant, and zero-cost -- the loop is backing off dead credits
        (its own breaker uses the same signature), so the census must say
        so loudly rather than render an idle-looking active row. A
        cannot-exec launch is the class the loop's breaker refuses to arm
        on, so it disqualifies the streak here too -- a broken agent
        install must never steer the operator at a credit refill. Both of
        its recorded shapes count: a spawn that never execs
        (``agent launch failed``) and a wrapper that runs and exits 127,
        the ``command not found`` convention (``agent error (exit 127)``
        -- the step row's own exit code is the binary failure marker, so
        the reason is where the code survives). Active nodes only: a
        settled node's trailing failures are history.
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
            never_ran = row['metadata'].startswith('failed on')
            booked = (row['status'] == 'stopped') and never_ran
            if (row['ended_at'] is None) or booked:
                continue
            # a cannot-exec launch books failed/instant with no cost, but is
            # not billing-shaped -- either recorded reason (retry-marker safe:
            # the marker is a suffix) breaks the streak like a paid failure
            if row['metadata'].startswith(_CANNOT_EXEC_REASONS):
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
        loop runtime) is reconciled -- persisted via
        :meth:`_reconcile_status`, not just relabeled -- before listing, so
        the fleet's default steering read never echoes a dead loop as
        ``active``. Cap columns render each present child's live config
        values; a gone worktree keeps the registry caps. ``spend`` reads
        against the ``max_cost`` beside it at the same scope the cap is
        enforced at -- the current run's subtree spend -- and is blank for
        a node with no recorded runs. ``status`` is always bare, with any
        qualifier (a pending signal, an ``exited`` run's end reason, an
        ``orphaned`` flag, a ``model drop`` marker, an ``iteration gap``)
        in ``detail``; ``end_reason`` carries a settled row's typed end
        token (:meth:`end_reason`), ``None`` when nothing is recorded. The
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
                relabeling a crashed ``active`` node (no live loop runtime)
                to ``exited``, and a booting ``idle`` node (live runtime,
                the loop not yet stamped) to ``active`` (the authoritative
                view). Read-only -- it does not persist the relabel.
            decorated: Record each descendant's status qualifier (a
                pending signal, an exited run's end reason, a model-drop
                marker) in its ``detail`` and its typed end token in its
                ``end_reason``; display-only, gated off for hot
                paths such as ``--count``.

        Returns:
            List of child node records.

        """
        # read children -- authoritative (live) or the cached registry
        if live:
            # _live_descendants reconciles each row to the child's real status and
            # drops gone worktrees; additionally relabel a crashed-but-active node
            # ('active' with no live runtime) to 'exited' and a booting idle
            # node (live runtime) to 'active' -- display-only, mirroring the TUI
            # snapshot reconcile, so --live is the authoritative
            # settled-vs-crashed view; the batched probe only nominates
            # candidates, and each relabel confirms through the node's own
            # liveness law (see _loop_alive); an inconclusive runtime probe
            # proves nothing, so the stored status stands
            sessions = fractal.util.tmux.probe()
            rows = []
            for row, node in self._live_descendants(max_depth=max_depth):
                current = _base_status(row.get('status'))
                if current in ('active', 'idle'):
                    # the batched listing settles a tmux loop it names; anything else
                    # (unlisted, headless, or no tmux answer) confirms through the
                    # node's own liveness law -- recorded socket or process group
                    listed = (sessions is not None) and (node.tmux_session in sessions)
                    if listed and not node.headless:
                        alive: Optional[bool] = True
                    else:
                        alive = node._loop_alive()
                    if current == 'active':
                        if alive is False:
                            row = {**row, 'status': 'exited'}
                        # a started child holds 'idle' until its loop stamps
                        # 'active' after preflight, but its session is already
                        # live -- read the boot window as 'active', so a
                        # finishing ancestor's drain never completes over a child
                        # started seconds earlier; a sessionless idle node (spawned,
                        # never started) stays idle and never blocks a drain
                    elif current == 'idle' and alive:
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
            # and every row carries both qualifier keys either way
            row = {
                'detail': None,
                'end_reason': None,
                **row,
                **drifted,
                'spend': spend,
                'last': last,
            }
            capped.append(row)
        rows = capped
        # record each active descendant's pending stop/finish signal (and each
        # exited one's end reason) in 'detail', and each settled one's typed
        # end token in 'end_reason'; 'status' stays bare, so the filters
        # below select on the column itself
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
        """Fill a descendant's ``detail`` and ``end_reason`` qualifiers.

        Display helper for ``list``: for a descendant whose own stored
        status still matches the row's, records its :meth:`status_detail`
        (``pausing`` / ``stopping`` / ``finishing`` / the run's end reason
        / the ``model drop`` marker -- which any status can carry, so no
        stored-status gate scopes the consult) in the row's ``detail``,
        and its :meth:`end_reason` token in the row's ``end_reason``.
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
                if node.status() == stored:
                    decorated = {**row, 'end_reason': node.end_reason()}
                    if detail:
                        decorated['detail'] = detail
                    return decorated
        return row

    def _heal_crashed(
        self: Node,
        pairs: list[tuple[dict, Optional[Node]]],
        *,
        locked: bool = False,
    ) -> list[tuple[dict, Optional[Node]]]:
        """Persist the reconcile for crashed-but-active rows (the shared pass).

        Reads are where staleness is observed: a row still ``active`` with no
        live runtime is healed through the child's own
        :meth:`_reconcile_status` (persisted, not just relabeled) and re-read.
        One batched tmux probe nominates tmux rows whose session it does not
        list; a headless row is nominated on every pass, since its recorded
        group is its only witness and a host without tmux must still heal it,
        and a blind probe also nominates a socket-less row with a ``.pgid``
        record -- the blind host vouches for no row, so the row confirms
        through its own group. :meth:`_reconcile_status` is the one
        confirming probe, so no row is probed twice. A row without a live
        node (worktree gone) passes through untouched. An inconclusive
        runtime probe heals nothing -- stamping live loops ``exited`` on a
        blind host would reap them.

        Args:
            pairs: ``(row, node)`` per registry row -- ``node`` is ``None``
                when the row's worktree is gone.
            locked: The caller already holds the ``.worktrees`` flock.

        Returns:
            The pairs, each crashed-active row's status healed.

        """
        if not any(_base_status(row.get('status')) == 'active' for row, _ in pairs):
            return pairs
        sessions = fractal.util.tmux.probe()
        healed = []
        for row, node in pairs:
            if _base_status(row.get('status')) == 'active' and node is not None:
                # nominate only -- the reconcile confirms on the node's own
                # record: a headless row always (the listing says nothing about
                # it), a tmux row when a definitive listing misses it, and a
                # record-only row (a .pgid, no .socket) when the host is blind
                # -- a blind host vouches for no row, so such a row confirms
                # through its own group
                absent = (
                    node.headless
                    or (sessions is not None and node.tmux_session not in sessions)
                    or (
                        sessions is None
                        and not (node.node_dir / SOCKET_FILE).exists()
                        and (node.node_dir / PGID_FILE).exists()
                    )
                )
                if node.exists() and absent:
                    node._reconcile_status(locked=locked)
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
        # heal crashed-but-active rows before counting (persisting the
        # settle); the cap gates always run under the .worktrees flock
        live = self._heal_crashed(live, locked=True)
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
        if _draining(self):
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
            DirtyWorktreeError: If ``check`` is set and uncommitted changes
                remain.
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
                # the refusal outranks the live check; route it through
                # the backend's one no-fork raise site (its message
                # carries the remedy and citations), surfaced as today's
                # error type -- the placeholder session steers the doomed
                # build past the fork-without-a-session guard
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


def _draining(node: Node) -> bool:
    """Return whether the acting seat runs under a drain.

    Three sources, any sufficient. The loop-exported ``_DRAIN`` rides
    every seat subprocess of a ``--continue --drain`` run. The acting
    node's own run carrying the durable ``drain`` signal
    (:meth:`Node.drain_bound`) stands after an environment scrub and
    across a pause/resume, which the export alone does not survive. Both
    of those still ask the seat who it is, and a seat that moves can
    answer wrongly -- so the last source asks the operating system
    instead: whether this process sits in a draining seat's recorded
    process group (:meth:`Node.drain_lineage`).

    Args:
        node: Any node of the tree the verb acts on -- the drain that
            binds is one of that tree's own runs, and the lineage check
            reads its central database.

    Returns:
        Whether the guarded verb must refuse.

    """
    if os.environ.get('_DRAIN'):
        return True
    actor = Node.resolve_actor()
    if actor is not None and actor.drain_bound():
        return True
    return node.drain_lineage()


def _claim_in_flight(pgid_file: pathlib.Path, recorded: str) -> bool:
    """Return whether ``recorded`` is a rival launch's still-fresh claim.

    A launch claims the record empty and writes the pid only after its
    spawn, so an unparseable record younger than
    :data:`_ABANDONED_CLAIM_SECONDS` is a claim mid-handoff -- refused like
    a live record by every boot arbiter -- while an older one was abandoned
    by a launcher that died before recording and clears like a dead group.
    The verdict judges the caller's own read: a re-read could see a rival's
    pid land mid-arbitration and wave the caller past a record it never
    identity-checked.

    Args:
        pgid_file: The record whose age arbitrates an unparseable claim.
        recorded: The record's content as the caller read it.

    Returns:
        Whether the record must be refused rather than cleared.

    """
    try:
        int(recorded)
    except ValueError:
        try:
            claimed_at = pgid_file.stat().st_mtime
        except FileNotFoundError:
            return False
        return time.time() - claimed_at < _ABANDONED_CLAIM_SECONDS
    return False


def _group_alive(pgid_file: pathlib.Path) -> Optional[bool]:
    """Return whether the process group a record names is alive and is that group.

    A ``.pgid``/``.step_pgid`` record names a group by its leader's pid and
    dates it by its mtime. Alive is not identity: a recycled id answers
    ``killpg`` from an unrelated group, so a live group counts only when
    :func:`_recorded_group` dates its leader no later than the record.
    ``EPERM`` proves the group exists but belongs to another user; the loop
    runs as the operator, so ``ps`` -- which reads any user's process --
    arbitrates that group the same way. Only a failed ``ps`` leaves the
    answer open.

    Args:
        pgid_file: The record to probe.

    Returns:
        ``True`` for the recorded group alive; ``False`` for no record, an
        unparseable one, a gone group, or a recycled id; ``None`` when the
        live group's identity cannot be verified.

    """
    try:
        recorded_at = pgid_file.stat().st_mtime
        pgid = int(pgid_file.read_text(encoding='utf-8').strip())
    except (FileNotFoundError, ValueError):
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # alive but foreign-owned: the leader's start instant arbitrates below
        pass
    return _recorded_group(pgid, recorded_at)


def _recorded_group(pgid: int, recorded_at: float) -> Optional[bool]:
    """Return whether a live process group is the one its record named.

    A group id is its leader's pid, and the leader is already running when
    the loop records it -- so a leader ``ps`` dates *after* the record is a
    recycled pid fronting an unrelated group. A group that outlived its
    leader still matches: the OS cannot re-issue the id while any member
    survives. No answer to arbitrate with (``ps`` failed, an unparseable
    instant) is inconclusive, so lifecycle probes never mistake ignorance for
    proof that the loop died.

    Args:
        pgid: A live process group id (its leader's pid).
        recorded_at: Epoch instant the group was recorded.

    Returns:
        Whether the group is the recorded one, or ``None`` when its identity
        cannot be verified.

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
        return None
    lstart = result.stdout.strip()
    # ps reports an unmatched selection as one with no output: the live group
    # outlived its leader, which pins its identity; every other failed or empty
    # answer is inconclusive
    if result.returncode == 1 and not lstart and not result.stderr.strip():
        return True
    if result.returncode != 0 or not lstart:
        return None
    try:
        started = time.mktime(time.strptime(lstart, '%a %b %d %H:%M:%S %Y'))
    except ValueError:
        return None
    # a second of slack: lstart floors to the second, and the record
    # follows the leader's spawn within the same one
    return started <= recorded_at + 1.0
