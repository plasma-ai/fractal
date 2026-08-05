"""Status-guard and approval tests for the signal surface.

Drives the real ``fractal`` console script against a throwaway repo, exercising
the parts of the signal surface the existing suite leaves uncovered.
``test_node_cli`` already pins the ``finish``/``stop``/``kill`` guards from the
``idle`` state; this module covers the rest of the matrix and the bookkeeping:

- the guards from every *non-active* lifecycle status (terminal states and
  ``retired``), proving a finished/killed/retired node cannot be re-signalled
  -- and that every refusal leaves a ``failed`` event row naming the actor;
- the ``active`` allow-path -- ``finish``/``stop``/``pause`` record a signal but
  leave the node ``active`` for the loop to act on;
- the tree-wide brake -- ``fractal pause`` latches the root against new work,
  and ``fractal resume`` withdraws the pending pause and lifts the latch;
- ``kill``'s node/row status agreement -- the node and every active run/iteration/
  step row all land on ``killed`` together, pinned both as pure bookkeeping and
  against a real loop launch reaped mid-step through ``kill.sh``;
- the double-signal sequencing (``stop`` after ``finish`` is allowed; ``kill`` is
  terminal for further signals);
- the step approval tri-state (``approved`` NULL/``''``/timestamp) and the
  parent-only ``node approve`` guard;
- the ``exit`` signal -- the one signal name with no ``node`` command (the loop
  records it itself at run end).

The node status is forced through the core ``status_set`` (the loop's own hook)
so a test can place a node in any lifecycle state without a live tmux session;
``kill.sh`` is a no-op when no session exists, so the kill allow-path is fully
exercised here. The ``finish``/``stop`` allow-path is the exception: both
reconcile an ``active`` node with no live session to ``exited`` (a crashed loop),
so those tests spawn the node's real tmux session to model a running loop; the
mid-step kill pin drives a real stubbed loop launch instead, so ``kill.sh``
reaps live process groups.
Assertions look only at CLI stdout/exit codes and at rows read back through
``db _query``, so the suite tracks behavior, not internals.
"""

from __future__ import annotations

import csv
import io
import os
import pathlib
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Optional

import pytest

from fractal.cli.utils import resolve_node, resolve_user_node
from fractal.core.node import Node
from tests._helpers import _git

from .conftest import _cli_env, _fractal_bin, _reap_group, _require_tmux, _run

__all__ = [
    'test_signal_rejected_from_non_active_status',
    'test_active_node_accepts_graceful_signals',
    'test_user_node_resolves_from_a_non_init_checkout',
    'test_tree_pause_latches_and_resume_releases',
    'test_finish_cancel_withdraws_the_pending_signal',
    'test_list_surfaces_pending_signal_and_filters_on_base',
    'test_kill_marks_node_and_active_rows_killed',
    'test_kill_mid_step_lands_killed_on_every_surface',
    'test_step_timeout_kills_a_term_trapping_survivor',
    'test_boot_adopts_a_wind_down_that_swept_past_it',
    'test_stop_after_finish_records_both_and_keeps_active',
    'test_kill_is_terminal_for_further_signals',
    'test_step_approval_tristate_drives_approved_and_pending',
    'test_step_approve_is_parent_only_and_validates_the_step',
    'test_default_approve_targets_the_gated_step_during_a_sync_window',
    'test_exit_signal_has_no_node_command',
]


# (finish, stop, pause, kill, finish --cancel) all require active (kill also
# accepts paused and idle, covered separately); the kill message names the
# status
_REJECT_MESSAGES = {
    'finish': 'Cannot finish: node is not active.',
    'stop': 'Cannot stop: node is not active.',
    'pause': 'Cannot pause: node is not active.',
    'kill': 'Cannot kill: node is not active, paused, or idle (status: {status}).',
    'finish --cancel': 'Cannot cancel finish: node is not active.',
}

# the event type each refused verb records (the cancel verb maps
# to the finish_cancel event)
_REFUSAL_EVENTS = {
    'finish': 'finish',
    'stop': 'stop',
    'pause': 'pause',
    'kill': 'kill',
    'finish --cancel': 'finish_cancel',
}

# hanging claude stand-in for the live mid-step kill: emits the init frame the
# stream driver expects, then blocks -- the launch parks mid-step until kill.sh
# reaps the loop and step process groups
_HANG_STUB = """#!/usr/bin/env bash
SID=$(uuidgen | tr '[:upper:]' '[:lower:]')
printf '{"type":"system","subtype":"init","session_id":"%s"}\\n' "$SID"
exec sleep 600
"""

# a step-timeout survivor for the group-kill backstop: emit the init frame,
# spawn a child in the leader's own process group that ignores TERM and keeps
# the stdout pipe open, then exit the leader -- a leader-only poll() reaps the
# leader and returns, skipping the group KILL, so the survivor outlives the step
# and the stream reader blocks on its pipe forever; only a whole-group probe
# draws the KILL that reaps it
_TERM_TRAP_STUB = """#!/usr/bin/env bash
SID=$(uuidgen | tr '[:upper:]' '[:lower:]')
printf '{"type":"system","subtype":"init","session_id":"%s"}\\n' "$SID"
bash -c 'trap "" TERM; while true; do sleep 1; done' &
exit 0
"""

# a claude stand-in that completes instantly: the boot-window cascade pin needs
# a launch that reaches its first boundary check, not one that parks mid-step
_QUICK_STUB = """#!/usr/bin/env bash
SID=$(uuidgen | tr '[:upper:]' '[:lower:]')
printf '{"type":"system","subtype":"init","session_id":"%s"}\\n' "$SID"
printf '{"type":"result","session_id":"%s","total_cost_usd":0,"num_turns":1,"duration_ms":1}\\n' \\
    "$SID"
"""


@pytest.fixture(scope='module')
def repo(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Return a repo with a user node and one shared ``guard`` worker.

    Built once via the real CLI. The ``guard`` worker is reused by the
    guard-rejection matrix (whose only writes are its refusal event rows,
    asserted newest-first); mutating tests init their own uniquely-named
    workers so they never interfere with one another.
    """
    # tmux session names embed this dirname machine-wide: a run-unique suffix
    # keeps sibling suite runs and stale leaked sessions from duplicate-colliding
    root = tmp_path_factory.mktemp(f'fractal_signal_{uuid.uuid4().hex[:8]}')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'signal@test.local')
    _git(root, 'config', 'user.name', 'signal')
    (root / 'README.md').write_text('# signal\n', encoding='utf-8')
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
    assert _run(root, 'node', 'init', 'guard', '--agent', 'claude').returncode == 0
    return {'root': root, 'guard': root / '.worktrees' / 'main.guard'}


@pytest.fixture
def live_loop() -> Iterator[Callable[[pathlib.Path], None]]:
    """Yield a callable spawning a worker's real tmux session (a running loop).

    ``finish``/``stop`` reconcile an ``active`` node with no live session to
    ``exited``, so the allow-path tests must present a live session. The
    callable spawns the session ``start.sh`` would (skipping the test when
    tmux is unavailable); every spawned session is killed on teardown.
    """
    _require_tmux()
    spawned: list[str] = []

    def _spawn(wt: pathlib.Path) -> None:
        session = _session_name(wt)
        subprocess.run(['tmux', 'new-session', '-d', '-s', session], check=True)
        spawned.append(session)

    yield _spawn

    # `=` prefix forces an exact target match (no prefix resolution)
    for session in spawned:
        subprocess.run(
            ['tmux', 'kill-session', '-t', f'={session}'],
            capture_output=True,
        )


# ------ guard-rejection matrix (non-active statuses)


@pytest.mark.parametrize(
    argnames='status',
    argvalues=['completed', 'stopped', 'exited', 'killed', 'retired'],
)
@pytest.mark.parametrize(
    argnames='command',
    argvalues=['finish', 'stop', 'pause', 'kill', 'finish --cancel'],
)
def test_signal_rejected_from_non_active_status(
    repo: dict,
    command: str,
    status: str,
) -> None:
    """The signal verbs are rejected from settled statuses, on the record.

    A finished, stopped, exited, killed, or retired node is not running, so
    ``finish``/``stop``/``pause``/``kill``/``finish --cancel`` must each fail
    with a clear ``RuntimeError`` (exit 1, message on stderr, no stdout) and
    must not mutate the node's status. The refusal itself is durable
    evidence: a ``failed`` event row naming the reason and the actor who
    tried, so a raced sweep's skipped node is never a silent mystery.
    """
    guard = repo['guard']
    Node(guard).status_set(status)
    result = _run(guard, 'node', *command.split())
    assert result.returncode == 1
    assert _REJECT_MESSAGES[command].format(status=status) in result.stderr
    assert result.stdout.strip() == ''
    # the rejected signal leaves the lifecycle state untouched
    assert _run(guard, 'node', 'status').stdout.strip() == status
    # ...and leaves a failed event row attributing the refused attempt
    refusal = _last_event(guard, _REFUSAL_EVENTS[command])
    assert refusal['status'] == 'failed'
    assert refusal['actor'] == 'operator'
    assert refusal['metadata'].startswith('refused: ')


# ------ active allow-path


@pytest.mark.parametrize('signal', ['finish', 'stop', 'pause'])
def test_active_node_accepts_graceful_signals(
    repo: dict,
    signal: str,
    live_loop: Callable[[pathlib.Path], None],
) -> None:
    """``finish``/``stop``/``pause`` record a signal, leaving the node ``active``.

    The signal is the loop's cue to wind down (or park) after the current
    iteration/step; the node stays ``active`` until the loop itself writes
    the terminal status, so the command must not flip the status on its own.
    The live session models the running loop (without it, the commands
    reconcile the node to ``exited``).
    """
    wt, _ = _arm(repo['root'], f'arm_{signal}')
    live_loop(wt)
    result = _run(wt, 'node', signal)
    assert result.returncode == 0
    assert result.stdout.strip() != ''
    count = f"SELECT COUNT(*) FROM signals WHERE node='{wt.name}' AND signal='{signal}'"
    assert _cell(wt, count) == '1'
    # the node stays active, now with the pending signal surfaced
    suffix = {'finish': 'finishing', 'stop': 'stopping', 'pause': 'pausing'}[signal]
    assert _run(wt, 'node', 'status').stdout.strip() == f'active ({suffix})'


@pytest.mark.parametrize('project', ['.', 'sub', 'pkgs/sub'])
def test_user_node_resolves_from_a_non_init_checkout(
    tmp_path: pathlib.Path,
    project: str,
) -> None:
    """The tree-wide brake resolves the user node by config, not the checkout.

    On a non-init branch (the user on their own branch while nodes run),
    ``resolve_node`` keys on the current branch and scopes the brake to a lone
    child; ``resolve_user_node`` finds the ``user: true`` config regardless of
    checkout, so pause/resume never silently mis-scope. On a sub-project tree
    the resolved node must anchor at the git root -- ``Node.node_dir`` derives
    the ``<project>/`` prefix from the ``.worktrees/.project`` cache, so a
    sub-project anchor would double the prefix and read an empty config. A
    nested sub-project sits below the top-level directory scan, so its config
    is discovered through the same cache.
    """
    root = tmp_path / 'repo'
    target = root if project == '.' else root / project
    target.mkdir(parents=True)
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 't@t.local')
    _git(root, 'config', 'user.name', 't')
    (target / 'README.md').write_text('# r\n', encoding='utf-8')
    wiki = target / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n', encoding='utf-8'
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    assert _run(root, 'init', project).returncode == 0
    assert _run(root, 'node', 'init', 'kid', '--agent', 'claude').returncode == 0
    # the user checks the repo root out to their own branch while the node exists
    _git(root, 'checkout', '-b', 'sidework')

    # resolve_node keys on the checkout -> scopes the brake to the child (the bug)
    assert not resolve_node(root).is_user
    # resolve_user_node anchors on the user config regardless of checkout
    user = resolve_user_node(root)
    assert user.is_user
    assert user.branch == 'main'


def test_tree_pause_latches_and_resume_releases(
    tmp_path_factory: pytest.TempPathFactory,
    live_loop: Callable[[pathlib.Path], None],
) -> None:
    """Tree-wide pause brakes the whole tree; tree-wide resume releases it.

    ``fractal pause`` at the repo root fans out from the user node and
    latches the root, so even a depth-1 init -- whose only ancestor is the
    statusless user root -- refuses while the tree is braked, and a second
    brake is a clean no-op. ``fractal resume`` withdraws a still-parking
    node's pause (its live loop then never parks) and lifts the latch. A
    private repo: tree-wide commands sweep every node, so the shared
    module repo's leftovers would bleed into the counts. The live session
    models the running loop (status reads reconcile an active node with no
    session to ``exited``).
    """
    root = tmp_path_factory.mktemp(f'fractal_brake_{uuid.uuid4().hex[:8]}')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'brake@test.local')
    _git(root, 'config', 'user.name', 'brake')
    (root / 'README.md').write_text('# brake\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    assert _run(root, 'init').returncode == 0
    wt, _ = _arm(root, 'brakeme')
    live_loop(wt)

    # the brake: fans out from the user node and latches the root
    result = _run(root, 'pause', '--reason', 'drill')
    assert result.returncode == 0
    assert 'Pause signal sent to 1 node' in result.stdout
    assert _run(wt, 'node', 'status').stdout.strip() == 'active (pausing)'
    # the latch refuses new work, even at depth 1
    refused = _run(root, 'node', 'init', 'latched_out', '--agent', 'claude')
    assert refused.returncode == 1
    assert 'Cannot spawn under a paused node' in refused.stderr
    # re-braking an already-signaled tree is a clean no-op
    again = _run(root, 'pause')
    assert again.returncode == 0
    assert 'No active nodes to pause' in again.stdout

    # the release: withdraws the pending pause (the live loop never parks)
    # and lifts the latch
    released = _run(root, 'resume')
    assert released.returncode == 0
    assert 'Resumed 1 node' in released.stdout
    assert _run(wt, 'node', 'status').stdout.strip() == 'active'
    allowed = _run(root, 'node', 'init', 'latched_out', '--agent', 'claude')
    assert allowed.returncode == 0, allowed.stderr
    # releasing a tree with nothing parked reports the no-op
    idle = _run(root, 'resume')
    assert idle.returncode == 0
    assert 'No paused nodes to resume.' in idle.stdout


def test_finish_cancel_withdraws_the_pending_signal(
    repo: dict,
    live_loop: Callable[[pathlib.Path], None],
) -> None:
    """``finish --cancel`` withdraws a pending finish and the loop keeps going.

    Without a cancel, a post-finish mission extension would strand the node
    (leaving direct DB surgery on the signals table as the only recourse).
    """
    wt, _ = _arm(repo['root'], 'arm_cancel')
    live_loop(wt)
    # finish arms the signal and the status surfaces it
    assert _run(wt, 'node', 'finish', '--reason', 'wrap up').returncode == 0
    assert _run(wt, 'node', 'status').stdout.strip() == 'active (finishing)'
    # cancel withdraws it: signal rows gone, status back to plain active
    cancelled = _run(wt, 'node', 'finish', '--cancel', '--reason', 'extended')
    assert cancelled.returncode == 0, cancelled.stderr
    assert cancelled.stdout.strip() != ''
    count = f"SELECT COUNT(*) FROM signals WHERE node='{wt.name}' AND signal='finish'"
    assert _cell(wt, count) == '0'
    assert _run(wt, 'node', 'status').stdout.strip() == 'active'
    # the withdrawal is audited -- a finish_cancel event pair closes completed
    events = (
        f"SELECT COUNT(*) FROM events WHERE node='{wt.name}'"
        " AND event='finish_cancel' AND status='completed'"
    )
    assert _cell(wt, events) == '1'
    # a second cancel has nothing to withdraw and refuses without mutating
    empty = _run(wt, 'node', 'finish', '--cancel')
    assert empty.returncode == 1
    assert 'no finish signal' in empty.stderr
    assert _run(wt, 'node', 'status').stdout.strip() == 'active'


def test_list_surfaces_pending_signal_and_filters_on_base(
    repo: dict,
    live_loop: Callable[[pathlib.Path], None],
) -> None:
    """``node list`` reports ``stopping`` in ``detail``; the status stays ``active``.

    The pending signal rides its own column, so a machine consumer selecting
    on ``status`` never has to defend against a qualifier: a winding-down
    child reads plain ``active``, is still selected by ``--status=active``,
    and is still counted as active by the loop's child count.
    """
    root = repo['root']
    wt, _ = _arm(root, 'listdec')
    live_loop(wt)
    assert _run(wt, 'node', 'stop').returncode == 0
    # the parent's listing surfaces the child's pending stop
    listing = _run(root, 'node', 'list')
    assert listing.returncode == 0
    rows = {r['node']: r for r in csv.DictReader(io.StringIO(listing.stdout))}
    assert rows['main.listdec']['status'] == 'active'
    assert rows['main.listdec']['detail'] == 'stopping'
    # the status filter still selects the winding-down child
    filtered = _run(root, 'node', 'list', '--status', 'active')
    selected = [r['node'] for r in csv.DictReader(io.StringIO(filtered.stdout))]
    assert 'main.listdec' in selected
    # ...and it still counts as active (the loop's --count path)
    count = _run(root, 'node', 'list', '--status', 'active', '--count')
    assert int(count.stdout.strip()) >= 1


# ------ kill: node/row status agreement


@pytest.mark.parametrize('reason', [None, 'wedged mid-step'])
def test_kill_marks_node_and_active_rows_killed(
    repo: dict,
    reason: Optional[str],
) -> None:
    """``kill`` lands the node and every active row on ``killed`` together.

    The signal (node status) and the persisted row state must agree: after a
    kill, the node is ``killed`` and the open run, iteration, and step rows are
    all ``killed`` -- no row is left dangling ``active``. The kill itself is
    audited: a completed ``kill`` event row names the interrupted step, so
    forensics never have to infer a kill from status transitions -- and the
    ``killed by`` attribution (with the reason appended, when one is given)
    reads identically off the event, the ``kill`` signal, and the run row.
    """
    wt, ids = _arm(repo['root'], 'killrows_why' if reason else 'killrows', step=True)
    args = ('--reason', reason) if reason else ()
    result = _run(wt, 'node', 'kill', *args)
    assert result.returncode == 0
    assert _run(wt, 'node', 'status').stdout.strip() == 'killed'
    run_id = ids['run']
    assert _cell(wt, f'SELECT status FROM runs WHERE run_id={run_id}') == 'killed'
    iter_id = ids['iter']
    iter_status = _cell(wt, f'SELECT status FROM iters WHERE iter_id={iter_id}')
    assert iter_status == 'killed'
    step_id = ids['step']
    step_status = _cell(wt, f'SELECT status FROM steps WHERE step_id={step_id}')
    assert step_status == 'killed'
    # the kill leaves an event row -- completed, pinned to the open step
    events = (
        f"SELECT COUNT(*) FROM events WHERE node='{wt.name}'"
        f" AND event='kill' AND status='completed' AND step_id={step_id}"
    )
    assert _cell(wt, events) == '1'
    # the attribution names the killer on event, signal, and run row alike
    label = f'killed by operator: {reason}' if reason else 'killed by operator'
    event = _last_event(wt, 'kill')
    assert (event['status'], event['actor']) == ('completed', 'operator')
    assert event['metadata'] == label
    signal = f"SELECT metadata FROM signals WHERE node='{wt.name}' AND signal='kill'"
    assert _cell(wt, signal) == label
    run = f'SELECT metadata FROM runs WHERE run_id={run_id}'
    assert _cell(wt, run) == label


def test_kill_mid_step_lands_killed_on_every_surface(
    repo: dict,
    tmp_path: pathlib.Path,
) -> None:
    """A mid-step kill of a live loop lands ``killed`` on every surface.

    The end-to-end twin of the bookkeeping pin above: a real loop launch
    parked mid-step on a hanging stub agent, ended by ``node kill`` --
    ``kill.sh`` reaps the loop and step process groups. The kill is the
    recorded ending everywhere at once -- the node status, the run/iteration/
    step rows, and the ``node activity`` feed all read ``killed`` -- and no
    surface re-classifies the reaped loop as a crash (no row carries
    ``Loop exited abnormally``).
    """
    root = repo['root']
    init = _run(
        root,
        'node',
        'init',
        'killlive',
        '--agent',
        'claude',
        '--max-iters',
        '1',
        '--no-sync',
        '--local',
    )
    assert init.returncode == 0, init.stderr
    wt = root / '.worktrees' / 'main.killlive'
    node_dir = wt / '.fractal' / 'main.killlive'
    # one hanging step, so the launch parks mid-step until the kill
    steps_dir = node_dir / 'steps'
    for step in steps_dir.glob('*.md'):
        step.unlink()
    (steps_dir / '01-hang.md').write_text('# Hang\n\nHanging step.\n', encoding='utf-8')
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    agent = bindir / 'claude'
    agent.write_text(_HANG_STUB, encoding='utf-8')
    agent.chmod(0o755)
    # the loop machinery runs from the package: launch the console script's
    # hidden loop entry directly, with the stub shadowing PATH (tmux is the
    # only piece this skips -- kill.sh falls back to the recorded .pgid)
    env = _cli_env()
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    log = tmp_path / 'loop.log'
    with open(log, 'w', encoding='utf-8') as handle:
        proc = subprocess.Popen(
            [_fractal_bin(), 'node', '_loop', f'--path={wt}'],
            cwd=f'{wt}',
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )
    try:
        # mid-step: an open step row with the agent invocation in flight
        # (.step_pgid is the launch-recorded handle kill.sh reaps)
        open_step = (
            f"SELECT COUNT(*) FROM steps WHERE node='{wt.name}' AND status='active'"
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if _cell(wt, open_step) == '1' and (node_dir / '.step_pgid').exists():
                break
            time.sleep(0.2)
        else:
            pytest.fail(f'launch never parked mid-step:\n{log.read_text()}')
        killed = _run(wt, 'node', 'kill')
        assert killed.returncode == 0, killed.stderr
        # the reaped loop is gone -- it never got to settle rows of its own
        proc.wait(timeout=30)
    finally:
        _reap_group(proc)
    # every surface reads the kill together: status file, entity rows...
    assert _run(wt, 'node', 'status').stdout.strip() == 'killed'
    for table in ('runs', 'iters', 'steps'):
        status = f"SELECT status FROM {table} WHERE node='{wt.name}'"
        assert _cell(wt, status) == 'killed', table
    # ...and the activity feed: all three end rows read killed, the kill event
    # completed, and nothing re-labeled the reaped loop as a crash
    csv_out = _run(wt, 'node', 'activity', '--csv').stdout
    rows = list(csv.DictReader(io.StringIO(csv_out)))
    ends = [row for row in rows if row['event'] == 'end']
    assert len(ends) == 3, csv_out
    assert all(row['status'] == 'killed' for row in ends), csv_out
    kills = [row['status'] for row in rows if row['event'] == 'kill']
    assert kills == ['completed'], csv_out
    assert all('Loop exited abnormally' not in row['metadata'] for row in rows), csv_out


def test_step_timeout_kills_a_term_trapping_survivor(
    repo: dict,
    tmp_path: pathlib.Path,
) -> None:
    """A step timeout SIGKILLs a group survivor that traps TERM.

    The in-process step deadline TERMs the invocation group, then KILLs any
    survivor after a grace. A child that traps TERM and keeps the stdout pipe
    open must still draw that KILL -- otherwise the stream reader blocks on the
    pipe with no mid-step deadline left, and the whole run hangs. The launch
    runs under a short step timeout against a stub that leaves such a survivor;
    the run must settle on its own (the survivor reaped so the reader unblocks)
    rather than hang -- and reaching a terminal step row at all proves it did.
    """
    root = repo['root']
    init = _run(
        root,
        'node',
        'init',
        'survivor',
        '--agent',
        'claude',
        '--max-iters',
        '1',
        '--step-timeout',
        '3s',
        '--no-sync',
        '--local',
    )
    assert init.returncode == 0, init.stderr
    wt = root / '.worktrees' / 'main.survivor'
    node_dir = wt / '.fractal' / 'main.survivor'
    # one step, so the single-iteration run parks on the survivor until the
    # step deadline reaps it
    steps_dir = node_dir / 'steps'
    for step in steps_dir.glob('*.md'):
        step.unlink()
    (steps_dir / '01-hang.md').write_text('# Hang\n\nHang step.\n', encoding='utf-8')
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    agent = bindir / 'claude'
    agent.write_text(_TERM_TRAP_STUB, encoding='utf-8')
    agent.chmod(0o755)
    env = _cli_env()
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    log = tmp_path / 'loop.log'
    with open(log, 'w', encoding='utf-8') as handle:
        proc = subprocess.Popen(
            [_fractal_bin(), 'node', '_loop', f'--path={wt}'],
            cwd=f'{wt}',
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )
    try:
        # 3s step deadline + 5s grace + settle: the group KILL reaps the
        # survivor and the single-iteration run ends on its own -- a leader-only
        # poll would leave the reader blocked and this wait would time out
        proc.wait(timeout=60)
    finally:
        _reap_group(proc)
    # a terminal step row proves the survivor was reaped and the reader
    # unblocked (the leader exits cleanly, so a truncated stream reads 'exited');
    # a leader-only poll would leave the step 'active' and hang the wait above
    status = f"SELECT status FROM steps WHERE node='{wt.name}'"
    assert _cell(wt, status) in {'exited', 'timed out'}, log.read_text()


# ------ double-signal sequencing


@pytest.mark.parametrize('signal', ['stop', 'finish'])
def test_boot_adopts_a_wind_down_that_swept_past_it(
    repo: dict,
    tmp_path: pathlib.Path,
    live_loop: Callable[[pathlib.Path], None],
    signal: str,
) -> None:
    """A child whose start was in flight when the sweep ran still winds down.

    ``stop``/``finish`` fan out over the descendants live at the moment
    they sweep, so a child still ``idle`` between ``node start`` returning
    and its loop's ``active`` stamp gets no signal row -- while the
    operator's command reports success. It then runs on unattended after
    its manager settles, and under ``finish`` it blocks the manager's
    drain-wait until its own caps run out. The child closes the window
    from its own end, the way a booting loop already parks itself under a
    pause latch: an ancestor still carrying a pending wind-down is one
    this node was meant to be part of, so the boot adopts the signal and
    the ordinary boundary checks honor it.
    """
    root = repo['root']
    name = f'boot{signal}'
    mgr, _ = _arm(root, name)
    live_loop(mgr)
    # a child registered under the manager but not yet started: the state a
    # start in flight leaves behind, which the sweep's active filter skips
    init = _run(
        mgr,
        'node',
        'init',
        'kid',
        '--agent',
        'claude',
        '--max-iters',
        '3',
        '--no-sync',
        '--local',
    )
    assert init.returncode == 0, init.stderr
    kid = root / '.worktrees' / f'main.{name}.kid'
    node_dir = kid / '.fractal' / f'main.{name}.kid'
    # one trivial step, so an iteration is one quick agent call
    steps_dir = node_dir / 'steps'
    for step in steps_dir.glob('*.md'):
        step.unlink()
    (steps_dir / '01-work.md').write_text('# Work\n\nOne step.\n', encoding='utf-8')
    # the sweep runs while the child is idle: it is skipped, and says so by
    # leaving no signal row behind
    swept = _run(mgr, 'node', signal)
    assert swept.returncode == 0, swept.stderr
    rows = f"SELECT COUNT(*) FROM signals WHERE node='{kid.name}' AND signal='{signal}'"
    assert _cell(kid, rows) == '0'
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    agent = bindir / 'claude'
    agent.write_text(_QUICK_STUB, encoding='utf-8')
    agent.chmod(0o755)
    env = _cli_env()
    env['PATH'] = f'{bindir}{os.pathsep}{env["PATH"]}'
    log = tmp_path / 'loop.log'
    with open(log, 'w', encoding='utf-8') as handle:
        proc = subprocess.Popen(
            [_fractal_bin(), 'node', '_loop', f'--path={kid}'],
            cwd=f'{kid}',
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )
    try:
        proc.wait(timeout=120)
    finally:
        _reap_group(proc)
    transcript = log.read_text()
    # the boot adopted the manager's order, attributed to it rather than
    # reading as the child's own boundary mis-fire
    metadata = (
        f"SELECT metadata FROM signals WHERE node='{kid.name}' AND signal='{signal}'"
    )
    assert _cell(kid, metadata) == f'via {signal} of main.{name}', transcript
    # ...and the first boundary check honored it, so the run settled without
    # booking a single iteration of the three it was capped at
    iters = f"SELECT COUNT(*) FROM iters WHERE node='{kid.name}'"
    assert _cell(kid, iters) == '0', transcript
    assert _run(kid, 'node', 'status').stdout.strip() != 'active', transcript


def test_stop_after_finish_records_both_and_keeps_active(
    repo: dict,
    live_loop: Callable[[pathlib.Path], None],
) -> None:
    """``stop`` after ``finish`` is allowed; both signals are recorded.

    The node is still ``active`` after ``finish`` (the loop has not wound down
    yet), so a follow-up ``stop`` is a valid escalation -- both signals persist
    for the loop to resolve, and the node stays ``active``.
    """
    wt, _ = _arm(repo['root'], 'seq')
    live_loop(wt)
    assert _run(wt, 'node', 'finish').returncode == 0
    assert _run(wt, 'node', 'stop').returncode == 0
    base = f"SELECT COUNT(*) FROM signals WHERE node='{wt.name}'"
    assert _cell(wt, f"{base} AND signal='finish'") == '1'
    assert _cell(wt, f"{base} AND signal='stop'") == '1'
    # status surfaces the pending signal (stop is the sooner stop, so it wins)
    assert _run(wt, 'node', 'status').stdout.strip() == 'active (stopping)'


def test_kill_is_terminal_for_further_signals(repo: dict) -> None:
    """Once killed, a node rejects a second ``kill`` and any other signal.

    ``kill`` flips the node to ``killed`` immediately, so the guards then treat
    it like any other terminal state: a repeat ``kill`` and a follow-up
    ``finish`` both fail.
    """
    wt, _ = _arm(repo['root'], 'terminal')
    assert _run(wt, 'node', 'kill').returncode == 0
    second = _run(wt, 'node', 'kill')
    assert second.returncode == 1
    assert 'status: killed' in second.stderr
    after_finish = _run(wt, 'node', 'finish')
    assert after_finish.returncode == 1
    assert 'Cannot finish' in after_finish.stderr


# ------ step approval tri-state + parent-only approve guard


def test_step_approval_tristate_drives_approved_and_pending(repo: dict) -> None:
    """The ``approved`` tri-state drives the approval read and ``pending`` together.

    ``approved`` has three states: NULL (no approval needed), ``''`` (pending),
    and a timestamp (approved). A fresh step is NULL -- approved and absent from
    ``pending``. ``step_pending`` moves it to ``''`` -- unapproved and it shows
    up in ``pending``. The parent ``node approve`` sets a timestamp -- approved
    again and it leaves ``pending``.
    """
    wt, ids = _arm(repo['root'], 'tristate', step=True)
    step_id = ids['step']
    record = Node(wt).record
    # NULL: a fresh step requires no approval (distinct from the '' pending state)
    assert (
        _cell(wt, f'SELECT approved IS NULL FROM steps WHERE step_id={step_id}') == '1'
    )
    assert record.step_approved(step_id=step_id)
    assert _pending_ids(wt) == []
    # '': now requires approval and is pending
    record.step_pending(step_id=step_id)
    assert not record.step_approved(step_id=step_id)
    assert _pending_ids(wt) == [step_id]
    # timestamp: the parent approves (no step_id -> the child's active step),
    # so it becomes approved and leaves pending
    approved = _run(repo['root'], 'node', 'approve', 'main.tristate')
    assert approved.returncode == 0
    assert record.step_approved(step_id=step_id)
    assert _pending_ids(wt) == []


def test_step_approve_is_parent_only_and_validates_the_step(repo: dict) -> None:
    """``node approve`` is parent-only, dual-logged, and validates the step.

    Approval is a parent privilege: a node approving its own step (it is not its
    own parent) is rejected with ``PermissionError`` and the step stays pending,
    while the parent (the repo root, on ``main``) may approve. A successful
    approval is dual-logged -- an ``approve`` event lands on both the parent and
    the child. Approving a step that never required approval (NULL) or a
    non-existent step are both ``ValueError``s that fail *before* the event is
    logged, so a doomed approval leaves no ``approve`` event on the parent.
    """
    wt, ids = _arm(repo['root'], 'approveperm', step=True)
    step_id = ids['step']
    record = Node(wt).record
    record.step_pending(step_id=step_id)
    # a node approving its own step (not its parent) is rejected; stays pending
    denied = _run(wt, 'node', 'approve', 'main.approveperm', f'{step_id}')
    assert denied.returncode == 1
    assert denied.stderr.startswith('Error:')
    assert 'parent' in denied.stderr
    assert not record.step_approved(step_id=step_id)
    # the parent (root on main) may approve
    ok = _run(repo['root'], 'node', 'approve', 'main.approveperm', f'{step_id}')
    assert ok.returncode == 0
    assert record.step_approved(step_id=step_id)
    # dual-logged: an approve event for this child lands on the parent's feed
    # and on the child's own feed (both scoped -- the shared central DB
    # accrues rows across tests)
    parent_approve = (
        "SELECT COUNT(*) FROM events WHERE event='approve'"
        " AND metadata LIKE 'main.approveperm:%'"
    )
    assert _cell(repo['root'], parent_approve) == '1'
    child_approve = (
        f"SELECT COUNT(*) FROM events WHERE node='{wt.name}' AND event='approve'"
    )
    assert _cell(wt, child_approve) == '1'
    # approving a step that never required approval (NULL) is a ValueError
    _, ids2 = _arm(repo['root'], 'approvenull', step=True)
    step_id2 = ids2['step']
    no_req = _run(
        repo['root'],
        'node',
        'approve',
        'main.approvenull',
        f'{step_id2}',
    )
    assert no_req.returncode == 1
    assert 'does not require approval' in no_req.stderr
    # approving a non-existent step is a ValueError
    missing = _run(repo['root'], 'node', 'approve', 'main.approvenull', '999999')
    assert missing.returncode == 1
    assert 'not found' in missing.stderr
    # a doomed approval logs nothing -- both guards fire before event_start, so
    # neither rejection left an approve event on the parent's feed
    null_approve = (
        "SELECT COUNT(*) FROM events WHERE event='approve'"
        " AND metadata LIKE 'main.approvenull:%'"
    )
    assert _cell(repo['root'], null_approve) == '0'


def test_default_approve_targets_the_gated_step_during_a_sync_window(
    repo: dict,
) -> None:
    """The default ``node approve`` resolves the gated step, not a live SYNC row.

    During an approval wait the periodic SYNC opens a second active step row
    (approval NULL) with a newer id, so the newest-active default would land on
    it and reject a genuinely pending approval; the default must pick the gated
    row (approval set) instead.
    """
    wt, ids = _arm(repo['root'], 'syncgate', step=True)
    record = Node(wt).record
    record.step_pending(step_id=ids['step'])
    # a second active step, newer, mimics the approval-wait SYNC bookkeeping row
    record.step_start(iter_id=ids['iter'], run_id=ids['run'], step=1, step_name='SYNC')
    # the parent's default approve (no step_id) resolves the gated EXECUTE step
    approved = _run(repo['root'], 'node', 'approve', 'main.syncgate')
    assert approved.returncode == 0
    assert record.step_approved(step_id=ids['step'])
    assert _pending_ids(wt) == []


# ------ exit signal: loop-only


def test_exit_signal_has_no_node_command(repo: dict) -> None:
    """``exit`` is a loop-only signal -- ``node exit`` is not a command.

    Of the five signal names (``finish``/``stop``/``kill``/``pause``/``exit``),
    only ``exit`` has no ``node`` sub-command: the loop records it itself at
    the end of a run that wound down without an explicit finish/stop, so no
    operator surface may set it.
    """
    wt, _ = _arm(repo['root'], 'exitsig')
    no_cmd = _run(wt, 'node', 'exit')
    assert no_cmd.returncode != 0
    assert 'No such command' in no_cmd.stderr


# ------ helpers


def _cell(wt: pathlib.Path, sql: str) -> str:
    """Return the first cell of the first data row of a ``db _query``."""
    rows = _run(wt, 'db', '_query', sql, '--csv').stdout.splitlines()
    return rows[1] if len(rows) > 1 else ''


def _last_event(wt: pathlib.Path, event: str) -> dict:
    """Return the newest ``events`` row of type ``event`` on ``wt``."""
    sql = (
        f"SELECT status, actor, metadata FROM events WHERE node='{wt.name}'"
        f" AND event='{event}' ORDER BY event_id DESC LIMIT 1"
    )
    out = _run(wt, 'db', '_query', sql, '--csv').stdout
    return next(csv.DictReader(io.StringIO(out)))


def _session_name(wt: pathlib.Path) -> str:
    """The tmux session name ``start.sh`` derives for a worker worktree.

    Format is ``<repo dirname> (<branch, dots dashed>)``; a worker's branch is
    its worktree directory name (``main.<name>``) and its repo dir is the root.
    """
    branch = wt.name.replace('.', '-')
    return f'{wt.parents[1].name} ({branch})'


def _pending_ids(wt: pathlib.Path) -> list[int]:
    """Return the ``step_id``s on ``wt`` awaiting approval (``approved = ''``)."""
    sql = f"SELECT step_id FROM steps WHERE node='{wt.name}' AND approved = ''"
    out = _run(wt, 'db', '_query', sql, '--csv').stdout
    return [int(row['step_id']) for row in csv.DictReader(io.StringIO(out))]


def _arm(
    root: pathlib.Path,
    name: str,
    *,
    iter: bool = False,
    step: bool = False,
) -> tuple[pathlib.Path, dict]:
    """Init a fresh worker, force it ``active``, and open a run.

    Optionally opens an active iteration (and a step under it) so the kill and
    approval tests have row-level state to assert against. Returns the worker's
    worktree and the ids of the rows it created.
    """
    wt = root / '.worktrees' / f'main.{name}'
    assert _run(root, 'node', 'init', name, '--agent', 'claude').returncode == 0
    # force the node active without a live loop -- status_set is the loop's
    # own hook -- and seed the rows through the core recorder
    node = Node(wt)
    node.status_set('active')
    ids = {'run': node.record.run_start()}
    if iter or step:
        ids['iter'] = node.record.iter_start(run_id=ids['run'], iter=1)
    if step:
        ids['step'] = node.record.step_start(
            iter_id=ids['iter'],
            run_id=ids['run'],
            step=1,
            step_name='EXECUTE',
        )
    return wt, ids
