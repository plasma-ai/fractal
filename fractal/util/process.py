"""Functions for reaping an agent invocation's whole process tree."""

from __future__ import annotations

import dataclasses
import os
import pathlib
import shutil
import signal
import subprocess
import time
from collections.abc import Collection, Mapping
from typing import Optional

__all__ = []

#: Seconds between the TERM pass and the KILL of every group still alive.
GRACE_SECONDS = 5.0

#: Signal rounds per pass: each re-walks the tree for groups the last round
#: missed (a tool forking as it dies) and stops early once a walk finds none.
ROUNDS = 5

#: Seconds the KILL pass waits for delivery before naming survivors.
SETTLE_SECONDS = 1.0

#: Seconds between process-table snapshots while waiting on a group.
POLL_SECONDS = 0.2


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
    in the swept worktree -- reported, never signaled.
    """

    survivors: tuple[Process, ...] = ()
    strays: tuple[Process, ...] = ()


def snapshot() -> dict[int, Process]:
    """Return the live process table keyed by pid.

    Lists every process through ``ps`` (the same fields on macOS and Linux)
    and reads each one's session id with ``os.getsid``; a session id the
    kernel refuses, or a process gone since the listing, reads ``None``.

    Raises:
        OSError: When ``ps`` cannot run.
        subprocess.CalledProcessError: When ``ps`` fails.

    """
    result = subprocess.run(
        ['ps', '-A', '-o', 'pid=,ppid=,pgid=,stat=,command='],
        capture_output=True,
        text=True,
        errors='replace',
        check=True,
    )
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
    the walk newly finds, for up to :data:`ROUNDS` rounds. A positive
    ``grace`` opens with a TERM pass and waits for the groups to empty
    (still signaling late groups) before the KILL pass; zero KILLs at once.

    Never signals pid 0 or 1, the caller's group, or the group of any of
    the caller's ancestors -- an agent pausing its own node keeps the
    groups it runs in. When the process table cannot be read, signals the
    root groups alone with the same grace and names nothing.

    Args:
        roots: Recorded process group ids (each its leader's pid).
        grace: Seconds between the TERM and KILL passes.
        worktree: Also report processes outside the tree whose working
            directory lies inside this directory.

    Returns:
        The tree's survivors and, with ``worktree``, the strays.

    """
    roots = frozenset(root for root in roots if root > 1)
    try:
        return _reap(roots, grace=grace, worktree=worktree)
    except (OSError, subprocess.SubprocessError):
        _reap_groups(roots, grace=grace)
        return Reaped()


# ------ helper functions


def _reap(
    roots: frozenset[int],
    *,
    grace: float,
    worktree: Optional[pathlib.Path],
) -> Reaped:
    """Run :func:`reap` over a readable process table."""
    tree = _Tree(roots)
    # TERM pass, then the KILL pass over every group still alive
    if grace > 0:
        tree.signal_pass(signal.SIGTERM, wait=grace)
    tree.signal_pass(signal.SIGKILL, wait=SETTLE_SECONDS)
    survivors = tuple(
        tree.table[pid]
        for pid in sorted(tree.members)
        if not tree.table[pid].zombie and tree.members[pid] not in tree.protected
    )
    strays: tuple[Process, ...] = ()
    if worktree is not None:
        excluded = tree.seen | tree.owners | set(tree.lineage)
        strays = tuple(
            tree.table[pid]
            for pid in sorted(_cwd_inside(worktree, tree.table))
            if pid not in excluded and not tree.table[pid].zombie
        )
    return Reaped(survivors=survivors, strays=strays)


class _Tree:
    """An invocation's process tree, re-walked from each fresh snapshot."""

    def __init__(self: _Tree, roots: frozenset[int]) -> None:
        """Initialize ``_Tree`` with its first walk (before any signal)."""
        self.roots = roots
        self.table = snapshot()
        self.members = _walk(roots, self.table, {})
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

    def refresh(self: _Tree) -> None:
        """Take a fresh snapshot and re-walk the tree from the known members."""
        self.table = snapshot()
        self.members = _walk(self.roots, self.table, self.members)
        self.seen.update(self.members)

    def live(self: _Tree) -> set[int]:
        """Return the signalable groups that still hold a live member."""
        groups = {
            pgid for pid, pgid in self.members.items() if not self.table[pid].zombie
        }
        return groups - self.protected

    def signal_pass(self: _Tree, sig: signal.Signals, *, wait: float) -> None:
        """Signal every live group in rounds, then wait for the groups to empty.

        Each round re-walks and signals only groups no earlier round did;
        the wait keeps re-walking and signals any group that appears.
        """
        signaled: set[int] = set()
        # signal in rounds until a walk finds no new group
        for _ in range(ROUNDS):
            groups = self.live() - signaled
            if not groups:
                break
            for group in groups:
                _killpg(group, sig)
            signaled |= groups
            self.refresh()
        # wait out the allowance, signaling late groups
        deadline = time.monotonic() + wait
        while self.live() and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
            self.refresh()
            for group in self.live() - signaled:
                _killpg(group, sig)
                signaled.add(group)


def _walk(
    roots: frozenset[int],
    table: Mapping[int, Process],
    known: Mapping[int, int],
) -> dict[int, int]:
    """Return the tree's members in ``table`` as ``{pid: pgid}``.

    Seeds from the root groups and from ``known`` (an earlier walk's
    members, kept only where the pid still names the same group, so a
    recycled pid never joins), then closes over session membership and
    parentage. Pid 1 never joins: every orphan's parent would follow it.
    """
    members = {
        pid
        for pid, process in table.items()
        if pid > 1 and (process.pgid in roots or known.get(pid) == process.pgid)
    }
    sessions = set(roots)
    while True:
        # a member that leads a session brings the whole session in
        sessions.update(pid for pid in members if table[pid].sid == pid)
        found = {
            pid
            for pid, process in table.items()
            if pid > 1 and (process.sid in sessions or process.ppid in members)
        }
        if found <= members:
            return {pid: table[pid].pgid for pid in members}
        members |= found


def _lineage(pid: int, table: Mapping[int, Process]) -> list[int]:
    """Return ``pid`` and each of its ancestors in ``table``, nearest first."""
    result: list[int] = []
    while pid > 1 and pid in table and pid not in result:
        result.append(pid)
        pid = table[pid].ppid
    return result


def _killpg(pgid: int, sig: signal.Signals) -> None:
    """Signal a process group, ignoring one that is gone or not ours."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _reap_groups(roots: frozenset[int], *, grace: float) -> None:
    """Signal the root groups alone: TERM, the grace, then KILL survivors."""
    groups = roots - {os.getpgrp()}
    if grace > 0:
        for group in groups:
            _killpg(group, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while groups and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
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
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return set()
        # -F emits one field per line: p<pid> opens a process, n<path> names
        # its cwd
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
