"""End-to-end recovery of a crashed-but-active node through the CLI.

Drives the real ``fractal`` console script: a node whose loop died leaves
``.status`` at ``active`` with no tmux session. The reject-active operations
(``merge``/``delete``/``retire``) reconcile that to ``exited`` and proceed, so
recovery never needs a hand-edited status file -- and reads reconcile too:
``node status`` and plain ``node list`` heal (persist) a provably-dead active
row instead of echoing it, because reads are the fleet's steering surface.
"""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import time

import pytest

from fractal.core.node import Node
from tests._helpers import _git

from .conftest import _cli_env, _fractal_bin, _reap_group, _run

__all__ = [
    'test_retire_recovers_a_crashed_active_node',
    'test_finish_stop_reconcile_a_crashed_active_node',
    'test_status_read_reconciles_crashed_node',
    'test_list_read_reconciles_crashed_node',
    'test_status_read_reaps_an_orphaned_agent',
    'test_kill_reaps_recorded_pgid_after_pane_death',
    'test_census_read_spares_a_live_tmux_less_loop',
]

# hanging claude stand-in: emit the init frame the stream driver expects,
# then block, so the launch parks mid-step for the census read to find
_HANG_STUB = """#!/usr/bin/env bash
SID=$(uuidgen | tr '[:upper:]' '[:lower:]')
printf '{"type":"system","subtype":"init","session_id":"%s"}\\n' "$SID"
exec sleep 600
"""


@pytest.fixture(scope='module')
def root(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """Return a repo with a user node; each test inits its own worker."""
    root = tmp_path_factory.mktemp('fractal_reconcile')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'reconcile@test.local')
    _git(root, 'config', 'user.name', 'reconcile')
    (root / 'README.md').write_text('# reconcile\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    # fractal init creates the user node, so worker init then passes
    assert _run(root, 'init').returncode == 0
    return root


def _crashed_worker(root: pathlib.Path, name: str) -> pathlib.Path:
    """Init a worker and leave it ``active`` with no tmux session (a crash)."""
    init = _run(root, 'node', 'init', name, '--agent', 'claude')
    assert init.returncode == 0, init.stderr
    worktree = root / '.worktrees' / f'main.{name}'
    # a crashed loop: status active, but no tmux session was ever started
    Node(worktree).status_set('active')
    return worktree


def test_retire_recovers_a_crashed_active_node(root: pathlib.Path) -> None:
    """A reject-active op reconciles a crashed node and proceeds (no hand-edit).

    ``retire`` rejects an active node; with the session provably gone it
    reconciles the status to the honest ``exited`` first and retires.
    """
    worktree = _crashed_worker(root, 'crashed')
    retired = _run(worktree, 'node', 'retire')
    assert retired.returncode == 0, retired.stderr
    assert _run(worktree, 'node', 'status').stdout.strip() == 'retired'


@pytest.mark.parametrize('command', ['finish', 'stop'])
def test_finish_stop_reconcile_a_crashed_active_node(
    root: pathlib.Path,
    command: str,
) -> None:
    """``finish``/``stop`` reconcile a crashed node instead of dead-ending.

    A crashed loop leaves ``.status`` ``active`` with no run row and no tmux
    session. ``finish``/``stop`` are mutating ops, so -- like merge/delete/retire
    -- they reconcile the provably-gone loop to ``exited`` first. The operator
    then sees the same clear not-active message the other ops give (not the
    misleading ``node has no run``), and the node ends reconciled to ``exited``.
    """
    worktree = _crashed_worker(root, f'crashed_{command}')
    result = _run(worktree, 'node', command)
    assert result.returncode == 1
    assert f'Cannot {command}: node is not active.' in result.stderr
    assert 'has no run' not in result.stderr
    # the crashed node is reconciled to the honest terminal status, not wedged
    assert _run(worktree, 'node', 'status').stdout.strip() == 'exited'


def test_status_read_reconciles_crashed_node(root: pathlib.Path) -> None:
    """``node status`` heals a crashed-but-active node instead of reporting it.

    Without reconciliation a parent polling a crashed child sees ``active``
    indefinitely while the dead node's open rows mis-arm budget math. With
    the session provably gone (one-loop-per-node), the read reconciles to
    the honest ``exited`` and persists it.
    """
    worktree = _crashed_worker(root, 'stale_status_read')
    # the read itself reports the reconciled terminal, not the stale record
    assert _run(worktree, 'node', 'status').stdout.strip() == 'exited'
    # and the healing persisted: the stored status is settled too
    node_dir = worktree / '.fractal' / 'main.stale_status_read'
    stored = (node_dir / '.status').read_text(encoding='utf-8').strip()
    assert stored == 'exited'


def test_list_read_reconciles_crashed_node(root: pathlib.Path) -> None:
    """Plain ``node list`` settles a crashed-but-active row, not echoes it.

    The list leg of the same recovery: ``--live`` relabels for display
    only, so the plain list -- the fleet's default steering read -- must
    reconcile (persist) a provably-dead active row and emit the healed
    status.
    """
    worktree = _crashed_worker(root, 'stale_list_read')
    listed = _run(root, 'node', 'list')
    assert listed.returncode == 0, listed.stderr
    row = next(
        line for line in listed.stdout.splitlines() if 'main.stale_list_read' in line
    )
    assert 'active' not in row, listed.stdout
    assert 'exited' in row, listed.stdout
    # the reconcile persisted: a direct read now reports the settled status
    node_dir = worktree / '.fractal' / 'main.stale_list_read'
    stored = (node_dir / '.status').read_text(encoding='utf-8').strip()
    assert stored == 'exited'


def _orphaned_worker(
    root: pathlib.Path,
    name: str,
) -> tuple[pathlib.Path, subprocess.Popen]:
    """A crashed worker whose agent group survived the pane (a live orphan).

    Fabricates the out-of-band pane death: a crashed-active worker plus a real
    process in its own group (``start_new_session`` -- pgid equals its pid,
    like the pane's loop process), recorded in the node's ``.pgid`` the
    way the loop records it at run start. The dead pane's socket record goes
    in too -- a loop that had a session always leaves one, and only that
    record tells a surviving group apart from a bare (tmux-less) loop's own,
    which no reap may touch.
    """
    worktree = _crashed_worker(root, name)
    orphan = subprocess.Popen(['sleep', '300'], start_new_session=True)
    node_dir = worktree / '.fractal' / f'main.{name}'
    (node_dir / '.pgid').write_text(f'{orphan.pid}\n', encoding='utf-8')
    # a short, never-created socket path: tmux answers "no such file", the
    # definitive empty a dead pane's socket gives -- a path under the node
    # dir would overrun the sockaddr limit and read as an unknown instead
    socket = f'/tmp/fx-dead-{orphan.pid}'  # noqa: S108
    (node_dir / '.socket').write_text(f'{socket}\n', encoding='utf-8')
    return worktree, orphan


def test_status_read_reaps_an_orphaned_agent(root: pathlib.Path) -> None:
    """A reconciling read kills the agent group the dead pane left behind.

    An out-of-band pane death (tmux kill/crash, host OOM) leaves the agent
    group running -- and spending -- headless. The reconcile that settles
    the crashed row must also reap the group recorded in ``.pgid``, drop
    the spent handle, and log an ``orphan`` event naming the reaped pgid.
    """
    worktree, orphan = _orphaned_worker(root, 'orphan_reap')
    node_dir = worktree / '.fractal' / 'main.orphan_reap'
    try:
        # the read settles the row -- and must have reaped the orphan first
        assert _run(worktree, 'node', 'status').stdout.strip() == 'exited'
        # the recorded group died by signal (TERM/KILL), not by finishing
        assert orphan.wait(timeout=10) < 0
        # the spent handle is dropped with the reap
        assert not (node_dir / '.pgid').exists()
        # the reap is on the audit trail: one orphan event naming the pgid
        activity = _run(worktree, 'node', 'activity', '--csv').stdout
        reaped = [
            line
            for line in activity.splitlines()
            if 'orphan' in line and f'reaped pgid {orphan.pid}' in line
        ]
        assert len(reaped) == 1, activity
    finally:
        # a red must not leak the sleep (orphan stubs accumulate)
        try:
            os.killpg(orphan.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        orphan.wait()


def test_kill_reaps_recorded_pgid_after_pane_death(root: pathlib.Path) -> None:
    """``node kill`` reaps the recorded group when the pane is already gone.

    The kill leg: ``kill.sh`` resolves its reap target through the live
    tmux pane, so after an out-of-band pane death it must fall back to the
    group recorded in ``.pgid`` and reap -- not report "no running node"
    while the agent spends on.
    """
    worktree, orphan = _orphaned_worker(root, 'orphan_kill')
    try:
        killed = _run(worktree, 'node', 'kill')
        assert killed.returncode == 0, killed.stderr
        # the recorded group died by signal -- the kill actually reaped it
        assert orphan.wait(timeout=10) < 0
        assert _run(worktree, 'node', 'status').stdout.strip() == 'killed'
    finally:
        # a red must not leak the sleep (orphan stubs accumulate)
        try:
            os.killpg(orphan.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        orphan.wait()


def test_census_read_spares_a_live_tmux_less_loop(
    root: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """A read-only census never reaps a healthy loop that has no tmux session.

    ``fractal node _loop`` is a supported entry point (``start.sh`` execs
    it, and the harness launches it bare), so a live loop can legitimately
    have no session for the tmux probe to find. The probe's "no such
    session" is then an answer about a server this loop never joined --
    ignorance, not proof -- and a reconcile that treated it as proof
    SIGKILLed the running loop's whole process group from any listing.
    The census must leave it alone: still running, still ``active``, no
    ``orphan`` row.
    """
    init = _run(
        root,
        'node',
        'init',
        'census',
        '--agent',
        'claude',
        '--max-iters',
        '1',
        '--no-sync',
        '--local',
    )
    assert init.returncode == 0, init.stderr
    worktree = root / '.worktrees' / 'main.census'
    node_dir = worktree / '.fractal' / 'main.census'
    # one hanging step, so the launch parks mid-step for the census read
    steps_dir = node_dir / 'steps'
    for step in steps_dir.glob('*.md'):
        step.unlink()
    (steps_dir / '01-hang.md').write_text('# Hang\n\nHanging step.\n', encoding='utf-8')
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    agent = bindir / 'claude'
    agent.write_text(_HANG_STUB, encoding='utf-8')
    agent.chmod(0o755)
    env = _cli_env()
    env['PATH'] = f'{bindir}{os.pathsep}{env["PATH"]}'
    log = tmp_path / 'loop.log'
    with open(log, 'w', encoding='utf-8') as handle:
        proc = subprocess.Popen(
            [_fractal_bin(), 'node', '_loop', f'--path={worktree}'],
            cwd=f'{worktree}',
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )
    try:
        # the loop is up once it has stamped active and recorded its group
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = (node_dir / '.status').read_text(encoding='utf-8').strip()
            if status == 'active' and (node_dir / '.pgid').exists():
                break
            time.sleep(0.2)
        else:
            pytest.fail(f'loop never came up:\n{log.read_text()}')
        listed = _run(root, 'node', 'list', '--json')
        assert listed.returncode == 0, listed.stderr
        # the census neither killed the loop nor rewrote its status
        assert proc.poll() is None, log.read_text()
        assert (node_dir / '.status').read_text(encoding='utf-8').strip() == 'active'
        assert '"status": "active"' in listed.stdout
        # ...and it logged no reap: an orphan row would name the pgid it took
        activity = _run(worktree, 'node', 'activity', '--csv').stdout
        assert 'orphan' not in activity, activity
    finally:
        _reap_group(proc)
