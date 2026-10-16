"""Functions for reaping an agent invocation's whole process tree."""

from __future__ import annotations

import dataclasses
import os
import pathlib
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Collection, Mapping
from typing import Optional

__all__ = []

#: Seconds between the TERM pass and the KILL of every group still alive.
GRACE_SECONDS = 5.0

# signal rounds per pass: each re-walks the tree for groups the last round
# missed (a tool forking as it dies) and stops early once a walk finds none;
# the wait after the rounds still signals any later group, so a few suffice
_ROUNDS = 5

#: Seconds the KILL pass waits before naming survivors: a KILL cannot be
#: caught, so this covers only its delivery and the process table catching up.
SETTLE_SECONDS = 1.0

# seconds between process-table snapshots while waiting on a group: each
# runs ps, so the poll stays coarse beside a grace of seconds
_POLL_SECONDS = 0.2

# a host-wide lsof scan takes seconds on a busy macOS host; past this the
# worktree sweep names nothing rather than hold the reap open
_LSOF_TIMEOUT_SECONDS = 30


@dataclasses.dataclass(frozen=True)
class Process:
    """One row of a process-table snapshot."""

    pid: int
    ppid: int
    pgid: int
    sid: Optional[int]
    zombie: bool
    command: str


@dataclasses.dataclass(frozen=True)
class Reaped:
    """What a :func:`reap` left behind.

    ``survivors`` are the tree's members still alive after the KILL;
    ``strays`` are processes outside the tree whose working directory lies
    in the swept worktree -- reported, never signaled; ``spared`` are the
    spared groups the walk reached and stopped at; ``inside`` says an
    ``outside_only`` reap found its caller inside the tree and signaled
    nothing.
    """

    survivors: tuple[Process, ...] = ()
    strays: tuple[Process, ...] = ()
    spared: tuple[int, ...] = ()
    inside: bool = False

    def lines(self: Reaped, *, branch: Optional[str] = None) -> list[str]:
        """Return the ``Note:`` and ``Warning:`` lines naming what the reap left.

        ``branch`` names the node whose tree was reaped, so the lines of
        several nodes' reaps sharing one output (a tree-wide pause) can be
        told apart.
        """
        # name the reaped node in each line when one is given
        of = ''
        spared_in = ''
        if branch:
            of = f' of {branch}'
            spared_in = f' in the reap of {branch}'
        result = [
            f"Note: spared process group {group} (another node's loop){spared_in}"
            for group in self.spared
        ]
        result.extend(
            f'Warning: pid {process.pid} survived the reap{of}: {process.command}'
            for process in self.survivors
        )
        result.extend(
            f'Warning: pid {process.pid} works in the worktree{of} outside the'
            f' reaped tree (left running): {process.command}'
            for process in self.strays
        )
        return result


def snapshot() -> dict[int, Process]:
    """Return the live process table keyed by pid.

    Lists every process through ``ps`` (the same fields on macOS and Linux)
    and reads each one's session id with ``os.getsid``; a session id the
    kernel refuses, or a process gone since the listing, reads ``None``.

    Raises:
        OSError: When ``ps`` cannot run.
        RuntimeError: When ``ps`` fails.

    """
    result = subprocess.run(
        ['ps', '-A', '-o', 'pid=,ppid=,pgid=,stat=,command='],
        capture_output=True,
        text=True,
        errors='replace',
    )
    if result.returncode != 0:
        error = result.stderr.strip()
        raise RuntimeError(f'ps failed (exit {result.returncode}): {error!r}')
    table = {}
    for line in result.stdout.splitlines():
        # ps escapes control characters in a command, so each process is one
        # line; skip any that still does not parse
        fields = line.split(None, 4)
        if len(fields) < 4 or not all(field.isdigit() for field in fields[:3]):
            continue
        pid, ppid, pgid = (int(field) for field in fields[:3])
        try:
            sid = os.getsid(pid)
        except (ProcessLookupError, PermissionError):
            sid = None
        command = fields[4] if len(fields) == 5 else ''
        table[pid] = Process(
            pid=pid,
            ppid=ppid,
            pgid=pgid,
            sid=sid,
            zombie=fields[3].startswith('Z'),
            command=command,
        )
    return table


def reap(
    roots: Collection[int],
    *,
    grace: float = GRACE_SECONDS,
    worktree: Optional[pathlib.Path] = None,
    owner: Optional[pathlib.Path] = None,
    mark: Optional[pathlib.Path] = None,
    outside_only: bool = False,
    spare: Callable[[], Collection[pathlib.Path]] = tuple,
) -> Reaped:
    """TERM, then KILL, every process group an invocation's tree holds.

    A root is a recorded process group whose leader started its own
    session. The tree is every process in a root group or a root's session
    (a session id survives reparenting, so an orphaned tool still matches),
    every process in a session a member leads, and every child of a member
    (a tool that called ``setsid`` while its parent lives). The first walk
    runs before any signal: a ``setsid`` tool's only link to the tree is
    its parent, which the first signal may kill.

    Each pass signals every member group, re-walks, and signals the groups
    the walk newly finds, for a bounded number of rounds. A positive
    ``grace`` opens with a TERM pass and waits for the groups to empty
    (still signaling late groups) before the KILL pass; zero KILLs at once.

    Never signals pid 0 or 1, the caller's group, or the group of any of
    the caller's ancestors, so a reap run from inside the tree lives to
    finish; with ``outside_only``, such a caller (a root group is its own
    or an ancestor's, as for an agent pausing its own node) signals
    nothing at all. Never signals a group recorded in a ``spare`` file,
    nor walks through one: everything in it, in a session one of its
    processes leads, or below one of its processes is left alone. A root
    is never spared: it is the group being reaped, so a record naming one
    (a crashed node's stale record whose pid now leads the invocation)
    spares nothing. The file list and the files are re-read before every
    signal round, so a record written mid-reap (a node launch the tree
    spawned) is honored. When the first snapshot fails, signals the root
    groups alone (minus the caller's) with the same grace and names
    nothing; when a later one fails, keeps signaling the groups already
    known, minus the protected and spared ones, probing them by signal
    instead of re-walking.

    One process escapes: an orphan from an earlier tool call whose own
    session's leader has exited (``validate.sh &`` under a ``setsid``
    shell that already returned) is in no member's session and has no
    member parent, so nothing links it to the tree; the ``worktree`` sweep
    names it and leaves it running.

    Args:
        roots: Recorded process group ids (each its leader's pid).
        grace: Seconds between the TERM and KILL passes.
        worktree: Also report processes outside the tree whose working
            directory lies inside this directory, except the ones the tree
            hangs from, the caller's lineage, the ``owner`` group, and
            everything at or below a spared group.
        owner: File recording the process group that owns the invocation
            (the node's own loop), whose processes the ``worktree`` sweep
            never names; read before the first signal, since an abort
            parks the loop, which drops its record mid-reap.
        mark: File to touch just before the first signal to a root group;
            left alone when every root group is protected.
        outside_only: Signal nothing when the caller runs inside the tree,
            reporting it as ``inside``.
        spare: Returns the files each recording a process group id to
            spare (a missing or unreadable file spares nothing); called
            before every signal round.

    Returns:
        The tree's survivors, the spared groups it reached, and, with
        ``worktree``, the strays.

    """
    roots = frozenset(root for root in roots if root > 1)
    # read before any signal: an abort parks the loop, which drops this record
    owned = _read_groups([owner]) if owner is not None else frozenset()
    try:
        tree = _Tree(roots, spare, mark)
    except (OSError, RuntimeError):
        _reap_groups(roots, grace=grace, mark=mark)
        return Reaped()
    if outside_only and roots & tree.protected:
        return Reaped(inside=True)
    return _reap(tree, grace=grace, worktree=worktree, owned=owned)


def reap_or_kill(
    roots: Collection[int],
    *,
    grace: float = GRACE_SECONDS,
    branch: Optional[str] = None,
    spare: Callable[[], Collection[pathlib.Path]] = tuple,
) -> list[str]:
    """Reap ``roots`` with their trees, or KILL the root groups if the reap breaks.

    The in-process ends of an invocation -- a step deadline, a dead stream,
    the crash heal, a timed-out probe -- must land even when :func:`reap`
    raises, so the root groups alone then draw a plain KILL.

    Args:
        roots: Recorded process group ids (each its leader's pid).
        grace: Seconds between the TERM and KILL passes.
        branch: The reaped node, named in the report's lines.
        spare: Returns the files each recording a process group id to
            spare, as for :func:`reap`.

    Returns:
        The lines to relay: the reap's report (:meth:`Reaped.lines`), or
        the warning that the reap broke.

    """
    try:
        reaped = reap(roots, grace=grace, spare=spare)
    except Exception as e:
        for root in roots:
            _killpg(root, signal.SIGKILL)
        return [
            f'Warning: process-tree reap failed ({e}); killing the recorded groups only'
        ]
    return reaped.lines(branch=branch)


class _Tree:
    """An invocation's process tree, re-walked from each fresh snapshot."""

    def __init__(
        self: _Tree,
        roots: frozenset[int],
        spare: Callable[[], Collection[pathlib.Path]],
        mark: Optional[pathlib.Path],
    ) -> None:
        """Initialize ``_Tree`` with its first walk (before any signal)."""
        self.roots = roots
        self.spare = spare
        self.mark = mark
        self.spared = self.read_spared()
        self.stopped: set[int] = set()
        # a later snapshot failure blinds the tree: no re-walks,
        # liveness by signal probe
        self.blind = False
        self.table = snapshot()
        self.members: dict[int, int] = {}
        self.walk()
        self.seen = set(self.members)
        # never signal the caller's own lineage
        self.lineage = _lineage(os.getpid(), self.table)
        self.protected = {
            0,
            1,
            os.getpgrp(),
            *(self.table[pid].pgid for pid in self.lineage),
        }
        # the processes that own the invocation (the loop and its parents)
        # sit above the tree, never in it, and are not strays either
        self.owners: set[int] = set()
        for pid, pgid in self.members.items():
            if pgid in roots:
                self.owners.update(_lineage(self.table[pid].ppid, self.table))

    def read_spared(self: _Tree) -> frozenset[int]:
        """Return the groups the spare records name, never a root."""
        return _read_groups(self.spare()) - self.roots

    def walk(self: _Tree) -> None:
        """Re-walk the current snapshot from the known members."""
        self.members, stopped = _find_members(
            roots=self.roots,
            table=self.table,
            known=self.members,
            spared=self.spared,
        )
        self.stopped |= stopped

    def refresh(self: _Tree) -> None:
        """Take a fresh snapshot and re-walk the tree from the known members."""
        self.spared = self.read_spared()
        if self.blind:
            return
        try:
            self.table = snapshot()
        except (OSError, RuntimeError):
            self.blind = True
            return
        self.walk()
        self.seen.update(self.members)

    def respare(self: _Tree) -> None:
        """Re-read the spared groups, re-walking when the set changed."""
        spared = self.read_spared()
        if spared != self.spared:
            self.spared = spared
            self.walk()

    def live(self: _Tree) -> set[int]:
        """Return the signalable groups that still hold a live member."""
        groups = {
            pgid for pid, pgid in self.members.items() if not self.table[pid].zombie
        }
        if self.blind:
            groups = {group for group in groups if _group_alive(group)}
        return groups - self.protected - self.spared

    def signal(self: _Tree, groups: set[int], sig: signal.Signals) -> None:
        """Signal ``groups``, touching the mark before a root group's first."""
        if self.mark is not None and groups & self.roots:
            _touch(self.mark)
            self.mark = None
        for group in groups:
            _killpg(group, sig)

    def signal_pass(self: _Tree, sig: signal.Signals, *, wait: float) -> None:
        """Signal every live group in rounds, then wait for the groups to empty.

        Each round re-walks and signals only groups no earlier round did;
        the wait keeps re-walking and signals any group that appears.
        """
        signaled: set[int] = set()
        # signal in rounds until a walk finds no new group
        for _ in range(_ROUNDS):
            self.respare()
            groups = self.live() - signaled
            if not groups:
                break
            self.signal(groups, sig)
            signaled |= groups
            self.refresh()
        # wait out the allowance, signaling late groups
        deadline = time.monotonic() + wait
        while self.live() and time.monotonic() < deadline:
            time.sleep(_POLL_SECONDS)
            self.refresh()
            groups = self.live() - signaled
            self.signal(groups, sig)
            signaled |= groups


# ------ helper functions


def _reap(
    tree: _Tree,
    *,
    grace: float,
    worktree: Optional[pathlib.Path],
    owned: frozenset[int],
) -> Reaped:
    """Run :func:`reap` over a tree whose first snapshot was read."""
    # TERM pass, then the KILL pass over every group still alive
    if grace > 0:
        tree.signal_pass(signal.SIGTERM, wait=grace)
    tree.signal_pass(signal.SIGKILL, wait=SETTLE_SECONDS)
    live = tree.live()
    survivors = tuple(
        tree.table[pid]
        for pid in sorted(tree.members)
        if not tree.table[pid].zombie and tree.members[pid] in live
    )
    strays: tuple[Process, ...] = ()
    if worktree is not None:
        # no stray: the tree, what it hangs from, the caller, the owner's
        # loop group, and everything at or below another node's loop -- a
        # child's loop works in the worktree of the tool that started it
        excluded = (
            tree.seen
            | tree.owners
            | set(tree.lineage)
            | _blocked(tree.table, tree.read_spared())
        )
        strays = tuple(
            tree.table[pid]
            for pid in sorted(_cwd_inside(worktree, tree.table))
            if pid not in excluded
            and tree.table[pid].pgid not in owned
            and not tree.table[pid].zombie
        )
    spared = tuple(sorted(tree.stopped))
    return Reaped(survivors=survivors, strays=strays, spared=spared)


def _find_members(
    roots: frozenset[int],
    table: Mapping[int, Process],
    known: Mapping[int, int],
    spared: frozenset[int],
) -> tuple[dict[int, int], set[int]]:
    """Return the tree's members in ``table`` and the spared groups it reached.

    Members come back as ``{pid: pgid}``. Seeds from the root groups and
    from ``known`` (an earlier walk's members, kept only where the pid
    still names the same group, so a recycled pid never joins), then closes
    over session membership and parentage. Pid 1 never joins: every
    orphan's parent would follow it. Nothing at or below a spared group
    joins either -- its processes, the sessions they lead, and their
    descendants.
    """
    # block every spared process and everything below it
    blocked = _blocked(table, spared)
    # seed, then close over sessions and parentage, stopping at blocked ones
    candidates = {
        pid
        for pid, process in table.items()
        if pid > 1 and (process.pgid in roots or known.get(pid) == process.pgid)
    }
    members: set[int] = set()
    stopped: set[int] = set()
    sessions = set(roots)
    while True:
        stopped.update(table[pid].pgid for pid in candidates & blocked)
        candidates -= blocked
        if candidates <= members:
            stopped &= spared
            return {pid: table[pid].pgid for pid in members}, stopped
        members |= candidates
        # a member that leads a session brings the whole session in
        sessions.update(pid for pid in members if table[pid].sid == pid)
        candidates = {
            pid
            for pid, process in table.items()
            if pid > 1 and (process.sid in sessions or process.ppid in members)
        }


def _blocked(table: Mapping[int, Process], spared: frozenset[int]) -> set[int]:
    """Return every process in ``table`` at or below a spared group.

    Covers each spared group's processes, the sessions they lead, and
    their descendants.
    """
    result = {pid for pid, process in table.items() if process.pgid in spared}
    while True:
        leaders = {pid for pid in result if table[pid].sid == pid}
        below = {
            pid
            for pid, process in table.items()
            if process.sid in leaders or process.ppid in result
        }
        if below <= result:
            return result
        result |= below


def _lineage(pid: int, table: Mapping[int, Process]) -> list[int]:
    """Return ``pid`` and each of its ancestors in ``table``, nearest first."""
    result: list[int] = []
    while pid > 1 and pid in table and pid not in result:
        result.append(pid)
        pid = table[pid].ppid
    return result


def _read_groups(paths: Collection[pathlib.Path]) -> frozenset[int]:
    """Return the process group ids the files at ``paths`` record."""
    groups = set()
    for path in paths:
        try:
            content = path.read_text(encoding='utf-8').strip()
        except OSError:
            continue
        if content.isdigit() and int(content) > 1:
            groups.add(int(content))
    return frozenset(groups)


def _touch(path: pathlib.Path) -> None:
    """Touch a marker file, ignoring a failed write (the abort still lands)."""
    try:
        path.touch()
    except OSError:
        pass


def _killpg(pgid: int, sig: signal.Signals) -> None:
    """Signal a process group, ignoring one that is gone or not ours."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _reap_groups(
    roots: frozenset[int],
    *,
    grace: float,
    mark: Optional[pathlib.Path],
) -> None:
    """Signal the root groups alone: TERM, the grace, then KILL survivors."""
    groups = roots - {os.getpgrp()}
    if groups and mark is not None:
        _touch(mark)
    if grace > 0:
        for group in groups:
            _killpg(group, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while groups and time.monotonic() < deadline:
            time.sleep(_POLL_SECONDS)
            groups = {group for group in groups if _group_alive(group)}
    for group in groups:
        _killpg(group, signal.SIGKILL)


def _group_alive(pgid: int) -> bool:
    """Return whether a process group still answers a null signal."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _cwd_inside(worktree: pathlib.Path, table: Mapping[int, Process]) -> set[int]:
    """Return the pids in ``table`` whose working directory is in ``worktree``.

    Reads ``/proc/<pid>/cwd`` where procfs exists (Linux) and asks ``lsof``
    otherwise (macOS); a host with neither reports nothing.
    """
    root = os.path.realpath(worktree)
    cwds: dict[int, str] = {}
    if os.path.exists('/proc/self/cwd'):
        for pid in table:
            try:
                cwds[pid] = os.readlink(f'/proc/{pid}/cwd')
            except OSError:
                continue
    elif lsof := shutil.which('lsof'):
        try:
            result = subprocess.run(
                [lsof, '-a', '-d', 'cwd', '-Fpn'],
                capture_output=True,
                text=True,
                errors='replace',
                timeout=_LSOF_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return set()
        # -F emits one field per line: p<pid> opens a
        # process, n<path> names its cwd
        pid = 0
        for line in result.stdout.splitlines():
            if line.startswith('p') and line[1:].isdigit():
                pid = int(line[1:])
            elif line.startswith('n') and pid:
                cwds[pid] = line[1:]
    return {
        pid
        for pid, cwd in cwds.items()
        if pid in table and (cwd == root or cwd.startswith(f'{root}{os.sep}'))
    }
