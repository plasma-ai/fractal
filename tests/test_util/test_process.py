"""Test the ``fractal.util.process`` module.

Every test drives a real process tree started here: a leader in its own
session (the shape of an agent launch) whose children take the shapes
agent tool commands take. Each process carries the test's unique token in
its argv, and teardown kills every process that carries it, recorded or
not, so a pid recycled to an unrelated process is never signaled.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator

import pytest

import fractal
import fractal.util.process
from tests._helpers import _alive, _kill_tagged

__all__ = [
    'test_reap_takes_every_group_in_the_tree',
    'test_reap_spares_a_loop_recorded_mid_reap',
    'test_reap_spares_the_caller_lineage',
    'test_reap_keeps_sparing_the_caller_lineage_when_ps_fails',
    'test_reap_signals_the_root_groups_alone_without_a_process_table',
    'test_tree_teardown_leaves_no_process_behind',
]

# the tree script: argv is <dir> <token> <role>; every process records its
# pid as <dir>/<role>.pid once it is set up, then sleeps until signaled
_TREE = """
import json, os, pathlib, signal, subprocess, sys, time

directory, token, role = sys.argv[1:4]


def spawn(role, **kwargs):
    return subprocess.Popen([sys.executable, __file__, directory, token, role], **kwargs)


def record():
    with open(f'{directory}/{role}.pid', 'w') as handle:
        handle.write(f'{os.getpid()}')


def wait_for(name):
    while not os.path.exists(f'{directory}/{name}.pid'):
        time.sleep(0.05)


if role == 'leader':
    # the shapes an agent's tool commands take: a group of its own, a
    # session of its own, a group that ignores TERM, a group that forks a
    # new group as it dies, and an orphan whose parent already exited
    spawn('group', process_group=0)
    spawn('session', start_new_session=True)
    spawn('stubborn', process_group=0)
    spawn('forker', process_group=0)
    spawn('orphaner')
elif role == 'orphaner':
    # double fork: the grandchild takes a group of its own and is
    # reparented once this process exits, so only the session links it
    if os.fork():
        os._exit(0)
    os.setpgid(0, 0)
    role = 'orphan'
elif role == 'stubborn':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
elif role == 'forker':
    def on_term(signum, frame):
        open(f'{directory}/forker.term', 'w').close()
        # the late child starts with TERM blocked, so the TERM the re-walk
        # sends it waits for its handler instead of racing its startup
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        spawn('late', process_group=0)
        wait_for('late')
        os._exit(0)
    signal.signal(signal.SIGTERM, on_term)
elif role == 'late':
    def on_term(signum, frame):
        open(f'{directory}/late.term', 'w').close()
        os._exit(0)
    signal.signal(signal.SIGTERM, on_term)
    record()
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
elif role == 'agent':
    # a tool in a group of its own that launches a node, the way an agent's
    # tool runs fractal node start
    spawn('launcher', process_group=0)
elif role == 'launcher':
    def on_term(signum, frame):
        # the TERM sets off the node's launch; later TERMs are ignored, so
        # every walk still reaches the loop through this process
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        spawn('loop', start_new_session=True)
    signal.signal(signal.SIGTERM, on_term)
elif role == 'loop':
    # a node loop ignores the reap's TERM and records its group as it boots
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(f'{directory}/loop.pgid', 'w') as handle:
        handle.write(f'{os.getpgrp()}')
elif role == 'hung_host':
    # a caller hung in its reap: it records no pid and leads no tree
    spawn('hung_caller', process_group=0).wait()
elif role == 'hung_caller':
    time.sleep(600)
elif role in ('host', 'blind_host', 'dark_host'):
    # a sibling tool group, then a caller in a group of its own that reaps
    # the host's tree from inside it
    spawn('sibling', process_group=0)
    wait_for('sibling')
    caller = {'host': 'caller', 'blind_host': 'blind_caller', 'dark_host': 'dark_caller'}
    spawn(caller[role], process_group=0).wait()
elif role in ('caller', 'blind_caller', 'dark_caller'):
    import fractal.util.process

    roots = [os.getsid(0)]
    if role == 'blind_caller':
        # ps answers the first snapshot only, then vanishes
        real = fractal.util.process.snapshot
        calls = []

        def snapshot():
            calls.append(None)
            if len(calls) > 1:
                raise OSError('ps vanished')
            return real()

        fractal.util.process.snapshot = snapshot
    elif role == 'dark_caller':
        # ps fails from the first snapshot on, and the caller's own group
        # rides the roots
        shim = pathlib.Path(directory) / 'shim'
        shim.mkdir()
        (shim / 'ps').write_text('#!/bin/sh\\nexit 1\\n')
        (shim / 'ps').chmod(0o755)
        path = os.environ['PATH']
        os.environ['PATH'] = f'{shim}{os.pathsep}{path}'
        roots.append(os.getpgrp())
    reaped = fractal.util.process.reap(
        roots,
        grace=1.0,
        mark=pathlib.Path(f'{directory}/abort.mark'),
    )
    survivors = [process.pid for process in reaped.survivors]
    with open(f'{directory}/caller.json', 'w') as handle:
        json.dump(survivors, handle)
    sys.exit(0)
if role != 'late':
    record()
time.sleep(600)
"""

# grace for the reaps under test: long enough for the forker's late child
# to start and draw its TERM before the KILL pass
_GRACE = 3.0


@pytest.fixture
def tree(tmp_path: pathlib.Path) -> Iterator[Callable[[str], subprocess.Popen]]:
    """Yield a starter for tree leaders; kill everything they spawned after."""
    with _trees(tmp_path) as start:
        yield start


# ------ reap


@pytest.mark.parametrize(
    argnames='recorded',
    argvalues=[False, True],
    ids=['unrecorded', 'root-recorded'],
)
def test_reap_takes_every_group_in_the_tree(
    tree: Callable[[str], subprocess.Popen],
    tmp_path: pathlib.Path,
    recorded: bool,
) -> None:
    """Every group the tree holds dies, however its member left the leader.

    A child in its own group is found by session, an orphan in its own
    group whose parent already exited by session alone, a ``setsid`` child
    by parentage (walked before any signal kills its parent), a group forked
    after the first TERM by the re-walk -- which TERMs it, so it gets a
    graceful shutdown -- and a TERM-ignoring group draws the KILL once
    the grace runs out. Nothing survives to be named. A spare record naming
    the root group itself -- a crashed node's stale record whose pid now
    leads the invocation -- spares nothing: the root is the group reaped.
    """
    leader = tree('leader')
    roles = ['leader', 'group', 'session', 'stubborn', 'forker', 'orphan']
    pids = _await_pids(tmp_path, roles)
    record = tmp_path / 'stale.pgid'
    if recorded:
        record.write_text(f'{leader.pid}\n', encoding='utf-8')
    start = time.monotonic()
    reaped = fractal.util.process.reap(
        [leader.pid],
        grace=_GRACE,
        spare=lambda: [record],
    )
    elapsed = time.monotonic() - start
    assert _alive(pids) == []
    assert reaped.survivors == ()
    assert reaped.spared == ()
    # the TERM-ignoring group held the reap open for the whole grace
    assert elapsed >= _GRACE
    # the late group drew the re-walk's TERM, not just the KILL: the forker
    # must have drawn its TERM, but a late child that never recorded before
    # the KILL pass is starvation (nothing to TERM -- skip), not the regression
    assert (tmp_path / 'forker.term').exists()
    if not (tmp_path / 'late.pid').exists():
        pytest.skip('load starvation: the late child never started before the KILL')
    late = _await_pids(tmp_path, ['late'])
    assert _alive(late) == []
    assert (tmp_path / 'late.term').exists()


def test_reap_spares_a_loop_recorded_mid_reap(
    tree: Callable[[str], subprocess.Popen],
    tmp_path: pathlib.Path,
) -> None:
    """A node loop the tree launches mid-reap is spared once it records itself.

    The launcher's TERM sets off a ``setsid``'d loop, and the launcher
    outlives the later TERMs, so every walk reaches the loop through it.
    The loop records its group in the spare file only after the reap
    began; the file is re-read before every round, so the loop outlives
    both passes and is named spared, while the launcher draws the KILL.
    """
    leader = tree('agent')
    (launcher,) = _await_pids(tmp_path, ['launcher'])
    record = tmp_path / 'loop.pgid'
    reaped = fractal.util.process.reap(
        [leader.pid],
        grace=_GRACE,
        spare=lambda: [record],
    )
    # the starvation case: a loop that never booted before the KILL pass
    # was never recorded to spare -- skip; one that booted and died fails
    if not (tmp_path / 'loop.pid').exists():
        pytest.skip('load starvation: the loop never booted before the KILL pass')
    (loop,) = _await_pids(tmp_path, ['loop'])
    assert _alive([launcher, loop]) == [loop]
    assert reaped.spared == (loop,)


def test_reap_spares_the_caller_lineage(
    tree: Callable[[str], subprocess.Popen],
    tmp_path: pathlib.Path,
) -> None:
    """A reap run from inside the tree never signals its own lineage.

    The caller sits in a group of its own under the host, so only the
    ancestor rule keeps the host's group off-limits: the sibling group
    dies, the host lives unmarked, and the caller finishes its reap and
    reports no survivors.
    """
    host = tree('host')
    pids = _await_pids(tmp_path, ['host', 'sibling'])
    report = tmp_path / 'caller.json'
    deadline = time.monotonic() + 60
    while not report.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert report.exists(), 'the caller never finished its reap'
    assert json.loads(report.read_text(encoding='utf-8')) == []
    host_pid, sibling_pid = pids
    assert _alive([sibling_pid]) == []
    assert _alive([host_pid]) == [host_pid]
    assert host.poll() is None
    assert not (tmp_path / 'abort.mark').exists()


def test_reap_keeps_sparing_the_caller_lineage_when_ps_fails(
    tree: Callable[[str], subprocess.Popen],
    tmp_path: pathlib.Path,
) -> None:
    """A process table that vanishes mid-reap never turns on the caller.

    ``ps`` answers the first snapshot only. The reap keeps signaling the
    groups it already knows, minus the caller's lineage, instead of falling
    back to the bare root group -- which is the caller's ancestor's: the
    sibling dies, and the host lives unmarked.
    """
    host = tree('blind_host')
    pids = _await_pids(tmp_path, ['blind_host', 'sibling'])
    report = tmp_path / 'caller.json'
    assert report.exists(), 'the caller never finished its reap'
    host_pid, sibling_pid = pids
    assert _alive([sibling_pid]) == []
    assert _alive([host_pid]) == [host_pid]
    assert host.poll() is None
    assert not (tmp_path / 'abort.mark').exists()


def test_reap_signals_the_root_groups_alone_without_a_process_table(
    tree: Callable[[str], subprocess.Popen],
    tmp_path: pathlib.Path,
) -> None:
    """A process table that cannot be read at all still ends the root groups.

    ``ps`` fails from the first snapshot on, so nothing can be walked: the
    reap marks the abort and signals the root groups alone -- the host's
    session group, never the caller's own group riding the roots -- and
    names nothing. The host dies, the sibling tool group no walk reached
    lives, and the caller lives to report.
    """
    host = tree('dark_host')
    (sibling,) = _await_pids(tmp_path, ['sibling'])
    report = tmp_path / 'caller.json'
    deadline = time.monotonic() + 60
    while not report.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert report.exists(), 'the caller never finished its reap'
    assert json.loads(report.read_text(encoding='utf-8')) == []
    assert host.wait(timeout=60) == -signal.SIGTERM
    assert _alive([sibling]) == [sibling]
    assert (tmp_path / 'abort.mark').exists()


# ------ teardown process hygiene


def test_tree_teardown_leaves_no_process_behind(tmp_path: pathlib.Path) -> None:
    """The tree teardown ends every process its run started, recorded or not.

    A caller hung in its reap records no pid and leads no tree, yet it
    carries the run's token, so the teardown's token sweep still ends it
    and a red run leaks nothing.
    """
    with _trees(tmp_path) as start:
        start('hung_host')
        hung = _await_role(tmp_path, 'hung_caller')
    # kill delivery is asynchronous: idle-wait on the survivor
    deadline = time.monotonic() + 15
    while _alive([hung]) and time.monotonic() < deadline:
        time.sleep(0.05)
    survivors = _alive([hung])
    # a red run must not itself leak the process it just proved leaked
    for pid in survivors:
        os.kill(pid, signal.SIGKILL)
    assert survivors == []


# ------ helpers


def _await_pids(directory: pathlib.Path, roles: list[str]) -> list[int]:
    """Block until each role has recorded its pid; return the pids in order."""
    deadline = time.monotonic() + 60
    paths = [directory / f'{role}.pid' for role in roles]
    while not all(path.exists() for path in paths):
        assert time.monotonic() < deadline, [path.name for path in paths]
        time.sleep(0.05)
    return [int(path.read_text(encoding='utf-8')) for path in paths]


def _await_role(directory: pathlib.Path, role: str) -> int:
    """Block until a tree process runs ``role``; return its pid.

    Finds a role that never records its pid by its argv, which ends with
    the role.
    """
    deadline = time.monotonic() + 60
    while True:
        for pid, process in fractal.util.process.snapshot().items():
            command = process.command
            if f'{directory}' in command and command.endswith(f' {role}'):
                return pid
        assert time.monotonic() < deadline, role
        time.sleep(0.05)


@contextlib.contextmanager
def _trees(directory: pathlib.Path) -> Iterator[Callable[[str], subprocess.Popen]]:
    """Yield a starter for tree leaders; kill everything they spawned after.

    Each leader runs ``_TREE`` in a session of its own. Teardown kills every
    process still carrying this run's token, recorded or not, then each
    leader's group.
    """
    token = f'fractal-reap-test-{uuid.uuid4().hex}'
    script = directory / 'tree.py'
    script.write_text(_TREE, encoding='utf-8')
    # the callers import this checkout's package
    package_root = pathlib.Path(fractal.__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': f'{package_root}'}
    leaders: list[subprocess.Popen] = []

    def start(role: str) -> subprocess.Popen:
        argv = [sys.executable, f'{script}', f'{directory}', token, role]
        leader = subprocess.Popen(argv, env=env, start_new_session=True)
        leaders.append(leader)
        return leader

    try:
        yield start
    finally:
        _kill_tagged(token)
        for leader in leaders:
            # a leader not yet waited on still holds its pid, so its group id cannot
            # have been reissued; a group of zombies alone refuses the signal
            if leader.returncode is None:
                try:
                    os.killpg(leader.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            leader.wait()
