"""Crashed-active reconciliation.

A loop that dies without ending leaves an ``active`` status with no
live runtime; the reject-active operations reconcile it to the honest
``exited`` (closing open rows and healing cap drift) before
proceeding. Also pins the heal's definitive-answer requirement (an
inconclusive runtime probe never reaps), the identity guard (a
recycled pgid is spared; a foreign-owned group is arbitrated by its
leader), and kill's stale-active behavior.
"""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import time
from typing import NoReturn, Optional

import pytest

from fractal.constants import HEADLESS_FILE, PGID_FILE, SOCKET_FILE
from fractal.core.node import Node, _recorded_group
from tests._helpers import _stub_run_script

from .conftest import _spawn_parent_child

__all__ = [
    'test_reject_active_op_reconciles_crashed_node',
    'test_reconcile_closes_crashed_runs_open_rows',
    'test_reconcile_status_heals_caps_on_crashed_node',
    'test_reconcile_requires_a_definitive_tmux_answer',
    'test_tmux_probe_falls_back_on_a_record_unlinked_mid_probe',
    'test_recorded_group_returns_unknown_on_failed_identity_probe',
    'test_recorded_group_accepts_a_confirmed_missing_leader',
    'test_foreign_owned_group_is_arbitrated_by_its_leader',
    'test_reconcile_requires_a_definitive_headless_identity',
    'test_headless_liveness_reconciles_a_dead_process_group',
    'test_headless_liveness_never_asks_tmux',
    'test_bare_loop_group_decides_when_tmux_denies_or_is_silent',
    'test_blind_probe_spares_a_record_less_bare_loop',
    'test_reconcile_spares_a_live_bare_loop_under_inherited_tmux',
    'test_reconcile_stands_down_for_a_relaunch_racing_the_probe',
    'test_reconcile_stands_down_for_a_kill_landing_during_the_reap',
    'test_reconcile_stands_down_for_a_continue_re_armed_during_the_reap',
    'test_kill_unchanged_on_stale_active',
    'test_reap_orphan_reaps_only_the_recorded_group',
    'test_reap_orphan_spares_a_group_with_unknown_identity',
]


# ------ crashed-active reconciliation


@pytest.mark.parametrize(
    argnames=('op', 'expected'),
    argvalues=[('merge', 'exited'), ('retire', 'retired')],
)
def test_reject_active_op_reconciles_crashed_node(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    op: str,
    expected: str,
) -> None:
    """A reject-active op reconciles a crashed-but-active node, then proceeds.

    A loop that died without ending leaves the status ``active`` with no tmux
    session; the reject-active ops (``merge``/``retire``, like ``start``)
    reconcile it to the honest ``exited`` first and run -- no hand-editing the
    status file. ``delete`` is covered in its own section.
    """
    node = node_with_db
    # crashed loop: active status with no live tmux session
    node.status_set('active')
    monkeypatch.setattr(node, '_tmux_session_exists', lambda: False)
    _stub_run_script(monkeypatch, node)
    getattr(node, op)()
    assert node.status() == expected


def test_reconcile_closes_crashed_runs_open_rows(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconciling a crashed loop closes its DB rows, not just the status file.

    A hard kill leaves the status ``active`` and the run (with its open
    iteration/step) un-ended. A later merge/delete/retire reconciles via the
    tmux probe -- which must stamp the crashed run's runs/iters/steps rows
    ``exited`` (exit 1) as well, so the DB never reads ``active`` while the
    status file reads ``exited`` (which would mislead cost/time/signal
    resolution into anchoring on a dead run).
    """
    node = node_with_db
    # crashed loop: open run/iteration/step plus an active status, no tmux session
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    step_id = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.status_set('active')
    # merge reconciles (status reject-active op); the session is provably gone
    monkeypatch.setattr(node, '_tmux_session_exists', lambda: False)
    _stub_run_script(monkeypatch, node)
    node.merge()
    # the status file and every open DB row agree on the honest terminal
    assert node.status() == 'exited'
    for table, key, row_id in (
        ('runs', 'run_id', run_id),
        ('iters', 'iter_id', iter_id),
        ('steps', 'step_id', step_id),
    ):
        row = node.db.read(table, where={key: row_id})[0]
        assert row['status'] == 'exited'
        assert row['exit_code'] == 1
        assert row['ended_at'] is not None
    # no run is left active to mislead context resolution
    assert node.db.read('runs', where={'status': 'active'}) == []


def test_reconcile_status_heals_caps_on_crashed_node(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stamping an out-of-band death ``exited`` also heals cap drift.

    A mid-run retune that only reached the config file leaves the registry
    row at the old cap, and a loop that dies before the next iteration
    boundary never runs the boundary reconcile -- so, without terminal
    healing, the drift would outlive the node permanently.
    ``_reconcile_status`` is the dead node's terminal cleanup, so the row
    it settles must read config truth.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # seed the registry caps via the blessed path, then retune the config
    # only -- a mid-run edit the boundary reconcile never gets to apply
    parent.child_update('kid', max_cost=16.0)
    child.config.set('max_cost', 22.0)
    # the loop dies out-of-band; the next reject-active op reconciles
    monkeypatch.setattr(child, '_tmux_session_exists', lambda: False)
    child._reconcile_status()
    assert child.status() == 'exited'
    # the settled row reads config truth, not the stale spawn-time cap
    row = child.db.read('nodes', where={'node': child.branch}, limit=1)[0]
    assert row['max_cost'] == 22.0


@pytest.mark.parametrize(
    argnames=('tmux_answer', 'expected'),
    argvalues=[
        # tmux missing from PATH (a cron/CI shell): inconclusive, no heal
        ('absent', 'active'),
        # list-sessions errors (a bad socket): inconclusive, no heal
        ('error', 'active'),
        # tmux answered 'no server running': the session is provably gone
        ('no-server', 'exited'),
        # the ambient socket has no server, but the loop recorded its own
        # socket at boot and the session is alive there: no heal
        ('recorded-socket', 'active'),
    ],
)
def test_reconcile_requires_a_definitive_tmux_answer(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    tmux_answer: str,
    expected: str,
) -> None:
    """Reconcile stamps ``exited`` only when tmux proved the session gone.

    A failed probe -- no tmux on PATH, or ``list-sessions`` erroring -- proves
    nothing about liveness, so the active node keeps its status and its open
    run: healing on ignorance would reap a healthy loop's process groups from
    any shell without tmux visibility. tmux's ``no server running`` refusal
    is a definitive empty answer, so the genuinely crashed node still heals --
    but only from the server the loop recorded at boot: a shell resolving a
    different socket (its own ``TMUX_TMPDIR``) would otherwise read a live
    session as gone and reap the healthy loop.
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.record.run_start()
    # the loop's boot-time socket record: the reconcile must ask this
    # server, not the ambient one
    recorded_socket = '/tmp/fx-test/other-socket'  # noqa: S108
    if tmux_answer == 'recorded-socket':
        (node.node_dir / SOCKET_FILE).write_text(
            f'{recorded_socket}\n', encoding='utf-8'
        )
    # restore the real probe (the fixture shadows it as always-alive)
    node._tmux_session_exists = Node._tmux_session_exists.__get__(node)

    # fake only the tmux spawn (git, used to resolve the branch, must work)
    real_run = subprocess.run

    def fake_run(
        cmd: list,
        *args: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess:
        if not cmd or cmd[0] != 'tmux':
            return real_run(cmd, *args, **kwargs)
        if tmux_answer == 'absent':
            raise FileNotFoundError(2, 'No such file or directory', 'tmux')
        if tmux_answer == 'recorded-socket':
            # only the recorded server holds the session; every other
            # socket (the ambient one included) has no server at all
            if '-S' in cmd and cmd[cmd.index('-S') + 1] == recorded_socket:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=f'{node.tmux_session}\n',
                    stderr='',
                )
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout='',
                stderr='no server running on /tmp/tmux-501/default',
            )
        stderrs = {
            'error': 'error connecting to /tmp/tmux-501/default (Permission denied)',
            'no-server': 'no server running on /tmp/tmux-501/default',
        }
        stderr = stderrs[tmux_answer]
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout='',
            stderr=stderr,
        )

    monkeypatch.setattr('fractal.util.tmux.subprocess.run', fake_run)
    node._reconcile_status()
    # the status and the run row heal together, or not at all
    assert node.status() == expected
    run_row = node.db.read('runs', where={'run_id': run_id})[0]
    assert run_row['status'] == expected


def test_tmux_probe_falls_back_on_a_record_unlinked_mid_probe(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``.socket`` record unlinked mid-probe falls back to the ambient socket.

    The parking loop drops its socket record on exit, so the record can
    vanish between the probe's existence check and its read. A vanished
    record is the exit's own trace, not an error: the probe asks the
    ambient server instead -- the same answer a never-recorded socket
    gets -- rather than crashing the reconcile that asked.
    """
    node = node_with_db
    # restore the real probe (the fixture shadows it as always-alive)
    node._tmux_session_exists = Node._tmux_session_exists.__get__(node)
    session = node.tmux_session
    socket_file = node.node_dir / SOCKET_FILE
    socket = '/tmp/fx-test/parked-socket'  # noqa: S108
    socket_file.write_text(f'{socket}\n', encoding='utf-8')
    real_read_text = pathlib.Path.read_text

    def racing_read_text(
        self: pathlib.Path,
        *args: object,
        **kwargs: object,
    ) -> str:
        # the parking loop unlinks the record between the existence check
        # and this read
        if self == socket_file:
            raise FileNotFoundError(2, 'No such file or directory', f'{self}')
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, 'read_text', racing_read_text)
    probes: list[dict] = []

    def probe(**kwargs: object) -> list[str]:
        probes.append(kwargs)
        return [session]

    monkeypatch.setattr('fractal.util.tmux.probe', probe)
    assert node._tmux_session_exists() is True
    # the fallback asked the ambient server, never the vanished record's
    assert probes == [{}]


@pytest.mark.parametrize(
    argnames='probe',
    argvalues=['unavailable', 'unparseable', 'failed', 'empty'],
)
def test_recorded_group_returns_unknown_on_failed_identity_probe(
    monkeypatch: pytest.MonkeyPatch,
    probe: str,
) -> None:
    """A failed identity probe is unknown, not evidence of PID reuse."""

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        if probe == 'unavailable':
            raise FileNotFoundError(2, 'No such file or directory', 'ps')
        if probe == 'failed':
            return subprocess.CompletedProcess(
                args=['ps'],
                returncode=2,
                stdout='',
                stderr='ps failed\n',
            )
        if probe == 'empty':
            return subprocess.CompletedProcess(
                args=['ps'],
                returncode=0,
                stdout='',
                stderr='',
            )
        return subprocess.CompletedProcess(
            args=['ps'],
            returncode=0,
            stdout='not a process start instant\n',
            stderr='',
        )

    monkeypatch.setattr('fractal.core.node.subprocess.run', run)
    assert _recorded_group(4242, time.time()) is None


def test_recorded_group_accepts_a_confirmed_missing_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selection miss identifies a live group that outlived its leader."""

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=['ps'],
            returncode=1,
            stdout='',
            stderr='',
        )

    monkeypatch.setattr('fractal.core.node.subprocess.run', run)
    assert _recorded_group(4242, time.time()) is True


@pytest.mark.parametrize(
    argnames=('recorded', 'expected'),
    argvalues=[(True, True), (False, False), (None, None)],
    ids=['recorded', 'recycled', 'unknown'],
)
def test_foreign_owned_group_is_arbitrated_by_its_leader(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    recorded: Optional[bool],
    expected: Optional[bool],
) -> None:
    """A group another user owns is arbitrated by its leader, not by ``EPERM``.

    ``killpg`` refusing with ``EPERM`` proves the group exists but not that
    it is the recorded one; ``ps`` reads any user's process, so the leader's
    start instant decides exactly as for a same-user group, and only a
    failed ``ps`` leaves the answer open.
    """
    node = node_with_db
    (node.node_dir / HEADLESS_FILE).write_text('headless\n', encoding='utf-8')
    (node.node_dir / PGID_FILE).write_text('4242\n', encoding='utf-8')

    def foreign_group(pgid: int, sig: int) -> NoReturn:
        raise PermissionError

    monkeypatch.setattr('fractal.core.node.os.killpg', foreign_group)
    monkeypatch.setattr(
        'fractal.core.node._recorded_group',
        lambda pgid, recorded_at: recorded,
    )
    assert node._loop_alive() is expected


def test_reconcile_requires_a_definitive_headless_identity(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live group with unknown identity keeps its active run open."""
    node = node_with_db
    node.status_set('active')
    run_id = node.record.run_start()
    (node.node_dir / HEADLESS_FILE).write_text('headless\n', encoding='utf-8')
    (node.node_dir / PGID_FILE).write_text('4242\n', encoding='utf-8')
    monkeypatch.setattr('fractal.core.node.os.killpg', lambda pgid, signal: None)
    monkeypatch.setattr(
        'fractal.core.node._recorded_group',
        lambda pgid, recorded_at: None,
    )
    node._reconcile_status()
    assert node.status() == 'active'
    run = node.db.read('runs', where={'run_id': run_id})[0]
    assert run['status'] == 'active'


def test_headless_liveness_reconciles_a_dead_process_group(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead headless group heals through the normal crashed-loop path."""
    node = node_with_db
    node.status_set('active')
    run_id = node.record.run_start()
    (node.node_dir / HEADLESS_FILE).write_text('headless\n', encoding='utf-8')
    (node.node_dir / PGID_FILE).write_text('4242\n', encoding='utf-8')

    def dead_group(pgid: int, sig: int) -> NoReturn:
        raise ProcessLookupError

    monkeypatch.setattr('fractal.core.node.os.killpg', dead_group)
    node._reconcile_status()
    assert node.status() == 'exited'
    run = node.db.read('runs', where={'run_id': run_id})[0]
    assert run['status'] == 'exited'
    assert not (node.node_dir / PGID_FILE).exists()
    # the backend record names the launch, not the run -- the heal keeps it
    assert node.headless


def test_headless_liveness_never_asks_tmux(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``.headless`` marker decides the probe, even over a recorded socket.

    A headless loop launched from a tmux shell inherits a ``.socket`` record
    for a server it never joined, and a host without tmux cannot answer at
    all -- so the marker routes liveness to the recorded process group alone.
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.record.run_start()
    recorded_socket = '/tmp/fx-test/socket'  # noqa: S108
    (node.node_dir / HEADLESS_FILE).write_text('headless\n', encoding='utf-8')
    (node.node_dir / SOCKET_FILE).write_text(f'{recorded_socket}\n', encoding='utf-8')
    (node.node_dir / PGID_FILE).write_text('4242\n', encoding='utf-8')

    def never_asked() -> NoReturn:
        raise AssertionError('a headless node never asks tmux')

    def dead_group(pgid: int, sig: int) -> NoReturn:
        raise ProcessLookupError

    monkeypatch.setattr(node, '_tmux_session_exists', never_asked)
    monkeypatch.setattr('fractal.core.node.os.killpg', dead_group)
    node._reconcile_status()
    assert node.status() == 'exited'
    run = node.db.read('runs', where={'run_id': run_id})[0]
    assert run['status'] == 'exited'
    assert not (node.node_dir / PGID_FILE).exists()
    # the backend record names the launch, not the run -- the heal keeps it
    assert node.headless


@pytest.mark.parametrize(
    argnames='tmux_answer',
    argvalues=[False, None],
    ids=['no-such-session', 'no-answer'],
)
@pytest.mark.parametrize(
    argnames=('recorded', 'expected'),
    argvalues=[(True, 'active'), (None, 'active'), (False, 'exited')],
    ids=['recorded', 'unknown', 'recycled'],
)
def test_bare_loop_group_decides_when_tmux_denies_or_is_silent(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    recorded: Optional[bool],
    expected: str,
    tmux_answer: Optional[bool],
) -> None:
    """A socket-less loop is judged by its own group, not by tmux's answer.

    ``fractal node _loop`` is a supported bare entry point that joins no tmux
    server and records no ``.socket``, so tmux's definitive "no such session"
    is no proof of death for it -- and a probe with no answer at all (a blind
    host) defers to the same recorded group: a live, identity-checked group
    keeps the run open, an unverified group leaves the answer unknown, and
    only a gone or recycled group lets the heal proceed.
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.record.run_start()
    (node.node_dir / PGID_FILE).write_text('4242\n', encoding='utf-8')
    monkeypatch.setattr(node, '_tmux_session_exists', lambda: tmux_answer)
    monkeypatch.setattr('fractal.core.node.os.killpg', lambda pgid, signal: None)
    monkeypatch.setattr(
        'fractal.core.node._recorded_group',
        lambda pgid, recorded_at: recorded,
    )
    node._reconcile_status()
    assert node.status() == expected
    run = node.db.read('runs', where={'run_id': run_id})[0]
    assert run['status'] == expected


def test_blind_probe_spares_a_record_less_bare_loop(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No tmux answer plus no ``.pgid`` record heals nothing.

    ``.pgid`` lands only after the loop stamps ``active``, so a socket-less
    node without a record may be a booting loop -- a blind probe proves
    nothing about it and the run stays open.
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.record.run_start()
    monkeypatch.setattr(node, '_tmux_session_exists', lambda: None)
    node._reconcile_status()
    assert node.status() == 'active'
    run = node.db.read('runs', where={'run_id': run_id})[0]
    assert run['status'] == 'active'


def test_reconcile_spares_a_live_bare_loop_under_inherited_tmux(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live bare loop booted inside a foreign pane survives the census.

    A bare ``fractal node _loop`` run in an operator's tmux pane inherits
    that pane's ``$TMUX``, but the boot's ownership probe finds only the
    operator's sessions there and records no ``.socket`` -- so the census
    finds a socket-less node whose ``.pgid`` names a live, identity-checked
    group, and the group's verdict keeps the run open whatever the inherited
    server answers about sessions the loop never owned.
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.record.run_start()
    # the loop's live group: a real same-user leader recorded at boot
    leader = subprocess.Popen(['sleep', '300'], start_new_session=True)
    pgid_file = node.node_dir / PGID_FILE
    try:
        pgid_file.write_text(f'{leader.pid}\n', encoding='utf-8')
        # the operator pane's inherited env -- no '.socket' record exists
        monkeypatch.setenv('TMUX', '/tmp/fx-test/socket,4242,0')  # noqa: S108
        node._reconcile_status()
        # the census deferred to the live group: nothing healed, nothing
        # reaped, and the record survives for the loop that owns it
        assert node.status() == 'active'
        run = node.db.read('runs', where={'run_id': run_id})[0]
        assert run['status'] == 'active'
        assert pgid_file.exists()
        assert leader.poll() is None
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        leader.wait()


def test_reconcile_stands_down_for_a_relaunch_racing_the_probe(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heal never reaps a loop that relaunched after its dead verdict.

    ``_reconcile_status`` holds no flock, so a continue can re-arm the node
    and boot a fresh loop between the healer's probe and its reap. The heal
    acts only on the exact state the probe judged -- status still ``active``
    and the group records untouched -- so a rewritten ``.pgid`` (a fresh
    boot records its group before the active stamp) stands the heal down:
    the new group is never signaled, its record survives, and the run stays
    open for the loop that owns it.
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.record.run_start()
    (node.node_dir / HEADLESS_FILE).write_text('headless\n', encoding='utf-8')
    # the crashed launch's record names a provably dead group
    crashed = subprocess.Popen(['sleep', '300'], start_new_session=True)
    os.killpg(crashed.pid, signal.SIGKILL)
    crashed.wait()
    pgid_file = node.node_dir / PGID_FILE
    pgid_file.write_text(f'{crashed.pid}\n', encoding='utf-8')
    leader = subprocess.Popen(['sleep', '300'], start_new_session=True)

    def racing_loop_alive() -> Optional[bool]:
        verdict = Node._loop_alive(node)
        # the rival relaunch lands between the probe and the reap: its
        # fresh boot rewrites the record before the active stamp
        pgid_file.write_text(f'{leader.pid}\n', encoding='utf-8')
        return verdict

    monkeypatch.setattr(node, '_loop_alive', racing_loop_alive)
    try:
        node._reconcile_status()
        # the fresh loop keeps its group, its record, and its open run
        assert leader.poll() is None
        assert pgid_file.read_text(encoding='utf-8') == f'{leader.pid}\n'
        assert node.status() == 'active'
        run = node.db.read('runs', where={'run_id': run_id})[0]
        assert run['status'] == 'active'
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        leader.wait()


def test_reconcile_stands_down_for_a_kill_landing_during_the_reap(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heal never overwrites a rival verb that settled the node mid-reap.

    ``_reconcile_status`` holds no flock and its reap can stall through a
    TERM grace, so a flock'd kill (or a continue's fresh boot) can settle
    the node between the act-time re-verify and the terminal writes. The
    heal re-checks its license after the reap: a status no longer ``active``
    stands it down, keeping the rival's stamp and its rows untouched.
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.record.run_start()
    (node.node_dir / HEADLESS_FILE).write_text('headless\n', encoding='utf-8')
    # the crashed launch's record names a provably dead group
    crashed = subprocess.Popen(['sleep', '300'], start_new_session=True)
    os.killpg(crashed.pid, signal.SIGKILL)
    crashed.wait()
    pgid_file = node.node_dir / PGID_FILE
    pgid_file.write_text(f'{crashed.pid}\n', encoding='utf-8')
    real_reap = node._reap_orphan

    def stalled_reap(
        snapshot: Optional[tuple[Optional[tuple[float, str]], ...]] = None,
    ) -> None:
        real_reap(snapshot)
        # the rival kill completes while the reap TERM-polls: its stamp
        # lands before the heal's terminal writes
        node.status_set('killed')

    monkeypatch.setattr(node, '_reap_orphan', stalled_reap)
    node._reconcile_status()
    # the rival's stamp survives, and the heal closed none of its rows
    assert node.status() == 'killed'
    run = node.db.read('runs', where={'run_id': run_id})[0]
    assert run['status'] == 'active'


def test_reconcile_stands_down_for_a_continue_re_armed_during_the_reap(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heal never stamps over a continue that fully re-armed mid-reap.

    A continue's flock'd heal-then-boot can complete inside the reap's TERM
    grace: fresh ``.pgid``, fresh run row, status back to ``active``. A
    re-armed node passes a status-only license, so the post-reap re-check
    also compares the group records against the judged snapshot -- a record
    now on disk that the verdict never judged stands the heal down, keeping
    the fresh boot's group, its record, and its open run.
    """
    node = node_with_db
    node.status_set('active')
    node.record.run_start()
    (node.node_dir / HEADLESS_FILE).write_text('headless\n', encoding='utf-8')
    # the crashed launch's record names a provably dead group
    crashed = subprocess.Popen(['sleep', '300'], start_new_session=True)
    os.killpg(crashed.pid, signal.SIGKILL)
    crashed.wait()
    pgid_file = node.node_dir / PGID_FILE
    pgid_file.write_text(f'{crashed.pid}\n', encoding='utf-8')
    leader = subprocess.Popen(['sleep', '300'], start_new_session=True)
    real_reap = node._reap_orphan
    fresh: dict[str, int] = {}

    def stalled_reap(
        snapshot: Optional[tuple[Optional[tuple[float, str]], ...]] = None,
    ) -> None:
        real_reap(snapshot)
        # the rival continue completes while the reap TERM-polls: its
        # fresh boot rewrites .pgid, opens a fresh run, and re-arms the
        # status before the heal's terminal writes
        pgid_file.write_text(f'{leader.pid}\n', encoding='utf-8')
        fresh['run_id'] = node.record.run_start()
        node.status_set('active')

    monkeypatch.setattr(node, '_reap_orphan', stalled_reap)
    try:
        node._reconcile_status()
        # the fresh boot keeps its group, its record, and its open run
        assert leader.poll() is None
        assert pgid_file.read_text(encoding='utf-8') == f'{leader.pid}\n'
        assert node.status() == 'active'
        run = node.db.read('runs', where={'run_id': fresh['run_id']})[0]
        assert run['status'] == 'active'
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        leader.wait()


def test_kill_unchanged_on_stale_active(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``kill`` is intentionally not reconciled: it still acts on a stale active.

    Unlike the reject-active ops, ``kill`` requires an active node and stays the
    cleanup path for a crashed loop -- it reaps the (gone) session and marks the
    node ``killed`` rather than erroring out, so its open rows are closed.
    """
    node = node_with_db
    node.status_set('active')
    node.record.run_start()
    # no live session (crashed), yet kill still proceeds rather than reconciling
    monkeypatch.setattr(node, '_tmux_session_exists', lambda: False)
    _stub_run_script(monkeypatch, node)
    node.kill()
    assert node.status() == 'killed'


# ------ orphan reap identity


@pytest.mark.parametrize(
    argnames='recycled',
    argvalues=[
        # the leader predates its record (the normal shape): the recorded
        # group -- reaped
        False,
        # the record predates the leader: the OS re-issued a dead group's
        # id to a stranger -- spared
        True,
    ],
)
def test_reap_orphan_reaps_only_the_recorded_group(
    node_with_db: Node,
    recycled: bool,
) -> None:
    """The reap verifies a stale pgid's identity, not just its liveness.

    A ``.pgid`` that outlives its loop can name a pid the OS has since
    recycled to an unrelated same-user group -- alive, so an existence
    probe alone passes and the reap would TERM/KILL a stranger. The group
    is the recorded one only while its leader is no younger than the
    record (the file's mtime); a leader started after the record marks a
    recycled pid, read as already gone. The stale record is dropped either
    way, and only a genuine reap logs the ``orphan`` audit event.
    """
    node = node_with_db
    # a live same-user group standing in for the recorded one; backdating
    # the record makes the leader postdate it, i.e. a recycled pid
    leader = subprocess.Popen(['sleep', '300'], start_new_session=True)
    pgid_file = node.node_dir / PGID_FILE
    try:
        pgid_file.write_text(f'{leader.pid}\n', encoding='utf-8')
        if recycled:
            stale = time.time() - 3600
            os.utime(pgid_file, (stale, stale))
        node._reap_orphan()
        if recycled:
            # the recycled group is spared -- alive and unsignaled
            assert leader.poll() is None
        else:
            # the recorded group draws the TERM
            assert leader.wait(timeout=5) != 0
        # the stale record is dropped either way
        assert not pgid_file.exists()
        # only a genuine reap logs the audit event
        events = node.db.read('events', where={'event': 'orphan'})
        assert bool(events) is not recycled
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        leader.wait()


def test_reap_orphan_spares_a_group_with_unknown_identity(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inconclusive identity probe never authorizes an orphan signal."""
    pgid_file = node_with_db.node_dir / PGID_FILE
    pgid_file.write_text('4242\n', encoding='utf-8')
    signals: list[tuple[int, int]] = []

    def killpg(pgid: int, sig: int) -> None:
        signals.append((pgid, sig))

    monkeypatch.setattr('fractal.core.node.os.killpg', killpg)
    monkeypatch.setattr(
        'fractal.core.node._recorded_group',
        lambda pgid, recorded_at: None,
    )
    node_with_db._reap_orphan()
    assert signals == [(4242, 0)]
    assert not pgid_file.exists()
    assert not node_with_db.db.read('events', where={'event': 'orphan'})
