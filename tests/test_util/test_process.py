"""Test the ``fractal.util.process`` module.

Every test drives a real process tree started here: a leader in its own
session (the shape of an agent launch) whose children take the shapes
agent tool commands take. Each process carries the test's unique token in
its argv, and teardown kills only token-bearing pids, so a pid recycled to
an unrelated process is never signaled.
"""

from __future__ import annotations

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

__all__ = [
    'test_reap_takes_every_group_in_the_tree',
    'test_reap_spares_the_caller_lineage',
]

# the tree script: argv is <dir> <token> <role>; every process records its
# pid as <dir>/<role>.pid once it is set up, then sleeps until signaled
_TREE = """
import json, os, signal, subprocess, sys, time

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
    # session of its own, a group that ignores TERM, and a group that forks
    # a new group as it dies
    spawn('group', process_group=0)
    spawn('session', start_new_session=True)
    spawn('stubborn', process_group=0)
    spawn('forker', process_group=0)
elif role == 'stubborn':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
elif role == 'forker':
    def on_term(signum, frame):
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
elif role == 'host':
    # a sibling tool group, then a caller in the host's own group that
    # reaps the host's tree from inside it
    spawn('sibling', process_group=0)
    wait_for('sibling')
    spawn('caller').wait()
elif role == 'caller':
    import fractal.util.process

    reaped = fractal.util.process.reap([os.getpgrp()], grace=1.0)
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
    """Return a starter for tree leaders; kill everything they spawned after.

    Each leader runs ``_TREE`` in a session of its own. Teardown kills every
    recorded pid still carrying this test's token, then the leaders.
    """
    token = f'fractal-reap-test-{uuid.uuid4().hex}'
    script = tmp_path / 'tree.py'
    script.write_text(_TREE, encoding='utf-8')
    # the callers import this checkout's package
    package_root = pathlib.Path(fractal.__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': f'{package_root}'}
    leaders: list[subprocess.Popen] = []

    def start(role: str) -> subprocess.Popen:
        argv = [sys.executable, f'{script}', f'{tmp_path}', token, role]
        leader = subprocess.Popen(argv, env=env, start_new_session=True)
        leaders.append(leader)
        return leader

    try:
        yield start
    finally:
        for pid_file in tmp_path.glob('*.pid'):
            _kill_tagged(int(pid_file.read_text(encoding='utf-8')), token)
        for leader in leaders:
            # an unreaped leader is still this test's child
            if leader.poll() is None:
                leader.kill()
            leader.wait()


# ------ reap


def test_reap_takes_every_group_in_the_tree(
    tree: Callable[[str], subprocess.Popen],
    tmp_path: pathlib.Path,
) -> None:
    """Every group the tree holds dies, however its member left the leader.

    A child in its own group is found by session, a ``setsid`` child by
    parentage (walked before any signal kills its parent), a group forked
    after the first TERM by the re-walk -- which TERMs it, so it gets a
    graceful shutdown -- and a TERM-ignoring group draws the KILL once
    the grace runs out. Nothing survives to be named.
    """
    leader = tree('leader')
    roles = ['leader', 'group', 'session', 'stubborn', 'forker']
    pids = _await_pids(tmp_path, roles)
    start = time.monotonic()
    reaped = fractal.util.process.reap([leader.pid], grace=_GRACE)
    elapsed = time.monotonic() - start
    assert _alive(pids) == []
    # the late group drew the re-walk's TERM, not just the KILL
    late = _await_pids(tmp_path, ['late'])
    assert _alive(late) == []
    assert (tmp_path / 'late.term').exists()
    assert reaped.survivors == ()
    # the TERM-ignoring group held the reap open for the whole grace
    assert elapsed >= _GRACE


def test_reap_spares_the_caller_lineage(
    tree: Callable[[str], subprocess.Popen],
    tmp_path: pathlib.Path,
) -> None:
    """A reap run from inside the tree never signals its own lineage.

    The caller sits in the host's group, so both its own group and its
    parent's are off-limits: the sibling group dies, the host lives, and
    the caller finishes its reap and reports no survivors.
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


# ------ helpers


def _await_pids(directory: pathlib.Path, roles: list[str]) -> list[int]:
    """Block until each role has recorded its pid; return the pids in order."""
    deadline = time.monotonic() + 60
    paths = [directory / f'{role}.pid' for role in roles]
    while not all(path.exists() for path in paths):
        assert time.monotonic() < deadline, [path.name for path in paths]
        time.sleep(0.05)
    return [int(path.read_text(encoding='utf-8')) for path in paths]


def _alive(pids: list[int]) -> list[int]:
    """The subset of ``pids`` still running (zombies count as dead)."""
    table = fractal.util.process.snapshot()
    return [pid for pid in pids if pid in table and not table[pid].zombie]


def _kill_tagged(pid: int, token: str) -> None:
    """SIGKILL ``pid`` only while its command line carries ``token``."""
    result = subprocess.run(
        ['ps', '-o', 'command=', '-p', f'{pid}'],
        capture_output=True,
        text=True,
    )
    if token in result.stdout:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
