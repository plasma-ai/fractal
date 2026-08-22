"""Test the ``fractal.tui.data`` module.

Every test reads the canonical deterministic tree through the surface the
cockpit renders from -- ``builder.build(scope)`` -- and asserts the shaped
snapshot, never the SQL underneath. The builder's clock is pinned ten minutes
past the seeded reference instant, so live-elapsed values are exact constants.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Callable
from typing import Any, Optional

import pytest

import fractal.core.agent
from fractal.cli.utils import resolve_node
from fractal.core.node import Node
from fractal.impl.claude import ClaudeAgent
from fractal.tui.data import TuiData, display_name_of
from fractal.tui.poller import NodePoller
from fractal.tui.snapshot import SnapshotBuilder

from ._tree import NOW_EPOCH, active_branches, deterministic_core, session_for

__all__ = [
    'test_display_name_of',
    'test_tree_topology_and_flags',
    'test_tree_shows_crashed_active_as_exited',
    'test_tree_reconciles_headless_process_identity',
    'test_crash_between_quiet_builds_reconciles_to_exited',
    'test_active_card_streams_live_state',
    'test_settled_card_is_a_time_machine',
    'test_six_cap_matrix',
    'test_step_denominator_scopes_to_each_run',
    'test_measures_tolerate_a_numeric_config_duration',
    'test_measures_tolerate_a_string_cost_cap',
    'test_measures_tolerate_a_mangled_max_iters',
    'test_build_tolerates_a_mangled_config',
    'test_build_tolerates_an_undecodable_status',
    'test_sync_folds_into_its_step',
    'test_drain_sync_lists_standalone',
    'test_a_user_step_named_sync_lists_numbered',
    'test_open_spans_tick_through_a_sync_window',
    'test_user_root_degrades',
    'test_codex_carries_no_cost_or_sessions',
    'test_radio_reads_are_the_nodes_own',
    'test_subtree_log_merges_descendants',
    'test_lost_reads_degrade_and_retry',
    'test_lost_spend_read_degrades_and_retries',
    'test_live_session_keys_on_the_resolved_backend_name',
    'test_read_surface_never_stamps_read_state',
]

# the canonical tree as the tree pane shows it: DFS over creation order
_TREE = (
    # branch, depth, status, signal, has_kids
    ('main', 0, 'idle', '', True),
    ('main.alpha', 1, 'active', '', True),
    ('main.alpha.deep', 2, 'active', '', True),
    ('main.alpha.deep.leaf', 3, 'completed', '', False),
    ('main.alpha.stopper', 2, 'active', 'stop', False),
    ('main.beta', 1, 'active', 'finish', False),
    ('main.gamma', 1, 'active', '', False),
    ('main.delta', 1, 'stopped', '', False),
    ('main.epsilon', 1, 'exited', '', False),
    ('main.zeta', 1, 'killed', '', False),
)


@pytest.mark.parametrize(
    argnames=('branch', 'title', 'expected'),
    argvalues=[
        ('main.data_pipeline', 'Custom Name', 'Custom Name'),
        ('main.data_pipeline', None, 'Data Pipeline'),
        ('main.alpha.deep_node', None, 'Deep Node'),
        ('main', None, 'Main'),
    ],
)
def test_display_name_of(branch: str, title: Optional[str], expected: str) -> None:
    """A node's display name is its stored title, else the de-slugged leaf."""
    assert display_name_of(branch, title) == expected


def test_tree_topology_and_flags(builder: SnapshotBuilder) -> None:
    """The tree section walks creation order with live statuses and signals."""
    snap = builder.build('main')
    rows = [
        (row['branch'], row['depth'], row['status'], row['signal'], row['has_kids'])
        for row in snap.tree
    ]
    assert rows == list(_TREE)
    # the count tallies agent nodes; the user root is flagged, not counted
    assert snap.counts == (9, 5)
    assert [row['branch'] for row in snap.tree if row['is_user']] == ['main']
    assert [row['branch'] for row in snap.tree if row['is_focused']] == ['main']


def test_tree_shows_crashed_active_as_exited(
    data: TuiData,
    builder: SnapshotBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed-but-active node displays as ``exited``, without being persisted.

    The cockpit reconciles a stale ``active`` against the live tmux sessions for
    display only: with ``main.gamma``'s session gone, its row reads ``exited``
    and drops from the running count, while the other active nodes are unchanged
    -- and the stored ``.status`` file is never touched.
    """
    # every active node is live except main.gamma (its loop crashed)
    alive = frozenset(
        data.tmux_session_name(branch)
        for branch in active_branches()
        if branch != 'main.gamma'
    )
    monkeypatch.setattr(data, 'live_sessions', lambda: alive)
    snap = builder.build('main')
    statuses = {row['branch']: row['status'] for row in snap.tree}
    assert statuses['main.gamma'] == 'exited'
    assert statuses['main.alpha'] == 'active'
    # the crashed node drops out of the running count (5 -> 4)
    assert snap.counts == (9, 4)
    # display only: the stored status file still reads active
    assert data.status('main.gamma') == 'active'


@pytest.mark.parametrize(
    ('probe', 'recorded', 'expected'),
    [
        ('live', True, 'active'),
        ('permission', False, 'active'),
        ('recycled', False, 'exited'),
        ('unknown', None, 'active'),
    ],
)
def test_tree_reconciles_headless_process_identity(
    pair_tree: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: str,
    recorded: Optional[bool],
    expected: str,
) -> None:
    """Headless display liveness is conservative and rejects PID reuse."""
    branch = 'main.alpha'
    Node(pair_tree / '.worktrees' / branch).status_set('active')
    data = TuiData(resolve_node(pair_tree))
    builder = SnapshotBuilder(data, NodePoller(data.db_dir), now=lambda: NOW_EPOCH)
    builder.build('main')
    node_dir = data.node_dir(branch)
    assert node_dir is not None
    (node_dir / '.headless').write_text('headless\n', encoding='utf-8')
    (node_dir / '.pgid').write_text('4242\n', encoding='utf-8')
    monkeypatch.setattr(data, 'live_sessions', frozenset)
    if probe == 'permission':

        def killpg(pgid: int, signal: int) -> None:
            raise PermissionError

        monkeypatch.setattr('fractal.tui.data.os.killpg', killpg)
    else:
        monkeypatch.setattr('fractal.tui.data.os.killpg', lambda pgid, signal: None)
    monkeypatch.setattr(
        'fractal.tui.data._recorded_group',
        lambda pgid, recorded_at: recorded,
    )
    snap = builder.build('main')
    statuses = {row['branch']: row['status'] for row in snap.tree}
    assert statuses[branch] == expected


def test_crash_between_quiet_builds_reconciles_to_exited(
    data: TuiData,
    builder: SnapshotBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loop that crashes between builds shows ``exited`` on the next build.

    A crash leaves ``.status`` ``active`` and never moves its mtime, so the
    poller reports nothing moved and the build short-circuits. The liveness
    reconcile must therefore run every build, ahead of the short-circuit --
    otherwise the cockpit shows the dead node ``active`` forever.
    """
    live = {data.tmux_session_name(branch) for branch in active_branches()}
    monkeypatch.setattr(data, 'live_sessions', lambda: frozenset(live))
    # first build: every active node is live
    first = builder.build('main')
    assert {r['branch']: r['status'] for r in first.tree}['main.gamma'] == 'active'
    # main.gamma's loop crashes -- its session vanishes, but nothing on disk
    # moves, so the poller sees a quiet tree
    live.discard(data.tmux_session_name('main.gamma'))
    second = builder.build('main')
    # the reconcile ran despite the quiet tree: gamma now reads exited
    assert second is not first
    assert {r['branch']: r['status'] for r in second.tree}['main.gamma'] == 'exited'
    assert data.status('main.gamma') == 'active'  # display only, disk untouched


def test_active_card_streams_live_state(builder: SnapshotBuilder) -> None:
    """An active node's card tracks its open step on the injected clock."""
    snap = builder.build('main.alpha')
    card = snap.card
    assert (card['status'], card['signal']) == ('active', '')
    assert (card['agent'], card['model']) == ('claude', 'opus 4.8')
    assert card['session'] == session_for('main.alpha', 2, 2)
    m = snap.measures
    assert (m['run'], m['iter'], m['iter_max']) == (2, 2, 10)
    assert (m['step'], m['step_total'], m['step_name']) == (3, 5, 'EXECUTE')
    # live elapsed: the pinned clock sits ten minutes past the reference
    assert (m['elapsed_step'], m['elapsed_iter'], m['elapsed_run']) == (
        3621.0,
        3740.0,
        4200.0,
    )
    # the open rows also carry their start instants -- the pane re-ticks
    # their elapsed between builds, when a quiet tree reuses this snapshot
    assert (m['started_step'], m['started_iter'], m['started_run']) == (
        NOW_EPOCH - 3621.0,
        NOW_EPOCH - 3740.0,
        NOW_EPOCH - 4200.0,
    )
    # the open step has reported no cost yet; the run cost chases the subtree
    # (deep, leaf, and stopper all chain into alpha's live run)
    assert m['cost_step'] is None
    assert m['cost_iter'] == pytest.approx(0.10)
    assert m['cost_run'] == pytest.approx(2.82)
    # distinct woven sessions, newest first -- the open iteration's session is
    # already offered (stamped on its step as soon as the stream opened)
    assert snap.sessions == (
        session_for('main.alpha', 2, 2),
        session_for('main.alpha', 2, 1),
        session_for('main.alpha', 1, 1),
    )
    # the explorer: newest run first, its open iteration first
    run = snap.history[0]
    assert (run['label'], run['status']) == ('run 2', 'active')
    assert [it['label'] for it in run['iters']] == ['iter 2', 'iter 1']
    live = run['iters'][0]
    assert [step['label'] for step in live['steps']] == [
        'step 1: PREPARE',
        'step 2: PLAN',
        'step 3: EXECUTE',
    ]
    assert live['steps'][-1]['status'] == 'active'
    # the hover time machine: each row carries the run's spend as of its end
    # (the live iteration reads all-time -- exactly the card's run figure)
    settled = run['iters'][1]
    # steps[0] is step 1 with its sync folded in (0.02 sync + 0.04 step)
    assert settled['steps'][0]['iter_spend'] == pytest.approx(0.06)
    assert settled['steps'][-1]['iter_spend'] == pytest.approx(0.42)
    assert settled['run_spend'] < live['run_spend']
    assert live['run_spend'] == pytest.approx(m['cost_run'])
    # the activity log leads every row with a subject (runs are numbered)
    run_rows = [row for row in snap.log if row['kind'] == 'run']
    assert {row['n'] for row in run_rows} == {1, 2}
    assert all(row['branch'] == 'main.alpha' for row in snap.log)


@pytest.mark.parametrize(
    argnames=('branch', 'status', 'step_view', 'elapsed', 'costs'),
    argvalues=[
        pytest.param(
            'main.alpha.deep.leaf',
            'completed',
            (5, 5, 'COMMIT'),
            (30.0, 450.0, 480.0),
            (0.12, 0.42, 0.42),
            id='completed',
        ),
        pytest.param(
            'main.delta',
            'stopped',
            (5, 5, 'COMMIT'),
            (30.0, 450.0, 480.0),
            (0.12, 0.42, 0.42),
            id='stopped',
        ),
        pytest.param(
            'main.epsilon',
            'exited',
            (5, 5, 'COMMIT'),
            (30.0, 450.0, 480.0),
            (0.12, 0.42, 0.42),
            id='exited',
        ),
        pytest.param(
            'main.zeta',
            'killed',
            (3, 3, 'EXECUTE'),
            (186.0, 335.0, 365.0),
            (0.08, 0.20, 0.20),
            id='killed',
        ),
    ],
)
def test_settled_card_is_a_time_machine(
    builder: SnapshotBuilder,
    branch: str,
    status: str,
    step_view: tuple,
    elapsed: tuple,
    costs: tuple,
) -> None:
    """A settled node renders its final stored spans, not the live clock.

    The killed node's pipeline was cut at step 3, so its denominator is the
    honest ``MAX(step)`` of what actually ran -- 3/3, not 3/5.
    """
    snap = builder.build(branch)
    assert snap.card['status'] == status
    m = snap.measures
    assert (m['step'], m['step_total'], m['step_name']) == step_view
    assert (m['elapsed_step'], m['elapsed_iter'], m['elapsed_run']) == elapsed
    # settled rows carry no start instants: nothing ticks between builds
    assert (m['started_step'], m['started_iter'], m['started_run']) == (
        None,
        None,
        None,
    )
    assert (m['cost_step'], m['cost_iter'], m['cost_run']) == pytest.approx(costs)
    assert (m['run'], m['iter']) == (1, 1)
    # the explorer head agrees with the card
    run = snap.history[0]
    assert (run['label'], run['status']) == ('run 1', status)
    step_n, step_total, step_name = step_view
    assert run['iters'][0]['step'] == f'{step_name} {step_n}/{step_total}'


def test_six_cap_matrix(builder: SnapshotBuilder) -> None:
    """All six cap axes resolve: per-step, per-iteration, and per-run."""
    m = builder.build('main.alpha').measures
    assert (m['cap_step_s'], m['cap_iter_s'], m['cap_run_s']) == (
        600.0,
        1800.0,
        7200.0,
    )
    assert (m['cap_step_cost'], m['cap_iter_cost'], m['cap_run_cost']) == (
        0.5,
        1.0,
        5.0,
    )


def test_step_denominator_scopes_to_each_run(pair_tree: pathlib.Path) -> None:
    """A run's step denominator is its own pipeline length, not the all-time max.

    A pipeline trimmed between runs (5 -> 3) must show the newer run's own N:
    an all-time ``MAX(step)`` would stamp ``/5`` on the trimmed run forever,
    while the run-scoped max reads ``/3``.
    """
    alpha = Node(pair_tree / '.worktrees' / 'main.alpha')
    with deterministic_core() as clock:
        clock.at(600.0)
        # run 1: a full 5-step pipeline, settled
        run1 = alpha.record.run_start()
        it1 = alpha.record.iter_start(run_id=run1, iter=1)
        for n, name in enumerate(('PREPARE', 'PLAN', 'EXECUTE', 'REVIEW', 'COMMIT'), 1):
            sid = alpha.record.step_start(
                iter_id=it1,
                run_id=run1,
                step=n,
                step_name=name,
            )
            alpha.record.step_end(step_id=sid, status='completed', exit_code=0)
        alpha.record.iter_end(iter_id=it1, status='completed', exit_code=0)
        alpha.record.run_end(run_id=run1, status='exited', exit_code=0)
        # run 2: the pipeline trimmed to 3 steps, still running (step 3 active)
        run2 = alpha.record.run_start()
        it2 = alpha.record.iter_start(run_id=run2, iter=1)
        for n, name in enumerate(('PREPARE', 'EXECUTE', 'COMMIT'), 1):
            sid = alpha.record.step_start(
                iter_id=it2,
                run_id=run2,
                step=n,
                step_name=name,
            )
            if n < 3:
                alpha.record.step_end(step_id=sid, status='completed', exit_code=0)
    alpha.status_set('active')
    data = TuiData(resolve_node(pair_tree))
    builder = SnapshotBuilder(data, NodePoller(data.db_dir), now=lambda: NOW_EPOCH)
    snap = builder.build('main.alpha')

    # the card reads run 2's own pipeline length (3), never the all-time 5
    assert snap.measures['step_total'] == 3


def test_measures_tolerate_a_numeric_config_duration(pair_tree: pathlib.Path) -> None:
    """A bare-number duration in config.json degrades to no cap, never a crash.

    config.json is agent-editable, so a self-tuning node may write
    ``timeout: 3600`` (an int) instead of ``'1h'``. ``_measures`` must coerce
    before parsing -- a raw int would hit ``.strip()`` and crash the whole
    cockpit build, killing scope-to and ``fractal open`` at boot.
    """
    alpha = Node(pair_tree / '.worktrees' / 'main.alpha')
    config_path = alpha.node_dir / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config['timeout'] = 3600  # a bare int, not the '1h' the parser expects
    config_path.write_text(json.dumps(config), encoding='utf-8')
    with deterministic_core() as clock:
        clock.at(100.0)
        alpha.record.run_start()
    data = TuiData(resolve_node(pair_tree))
    builder = SnapshotBuilder(data, NodePoller(data.db_dir), now=lambda: NOW_EPOCH)
    # the build completes; the unparseable numeric duration reads as no cap
    measures = builder.build('main.alpha').measures
    assert measures['cap_run_s'] is None


def test_measures_tolerate_a_string_cost_cap(pair_tree: pathlib.Path) -> None:
    """A non-numeric cost cap in config.json degrades to no cap, never a crash.

    config.json is agent-editable, so a self-tuning node may write a string
    ``max_cost`` (or ``reserve_budget``). The node pane divides and subtracts
    against the cap, so ``_measures`` must coerce -- a raw string would hit
    ``cost / cap`` and crash the cockpit build, the cost sibling of the
    duration crash above.
    """
    alpha = Node(pair_tree / '.worktrees' / 'main.alpha')
    config_path = alpha.node_dir / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config['max_cost'] = 'lots'  # a string, not the float the gauges divide by
    config_path.write_text(json.dumps(config), encoding='utf-8')
    with deterministic_core() as clock:
        clock.at(100.0)
        alpha.record.run_start()
    data = TuiData(resolve_node(pair_tree))
    builder = SnapshotBuilder(data, NodePoller(data.db_dir), now=lambda: NOW_EPOCH)
    # the build completes; the unparseable cost reads as no cap
    measures = builder.build('main.alpha').measures
    assert measures['cap_run_cost'] is None


@pytest.mark.parametrize(
    argnames='mangled',
    argvalues=['lots', float('inf')],
    ids=['string', 'infinity'],
)
def test_measures_tolerate_a_mangled_max_iters(
    pair_tree: pathlib.Path,
    mangled: object,
) -> None:
    """A malformed ``max_iters`` in config.json degrades to no cap, never a crash.

    config.json is agent-editable, so a self-tuning node may write a string
    ``max_iters`` -- or a JSON ``Infinity``, which ``int()`` rejects with
    OverflowError rather than ValueError. The measures matrix compares the
    cap against zero, so ``_measures`` must coerce -- an uncoerced value
    would crash the cockpit build, the iteration sibling of the crashes
    above.
    """
    alpha = Node(pair_tree / '.worktrees' / 'main.alpha')
    config_path = alpha.node_dir / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config['max_iters'] = mangled  # never the int the counter renders
    config_path.write_text(json.dumps(config), encoding='utf-8')
    with deterministic_core() as clock:
        clock.at(100.0)
        alpha.record.run_start()
    data = TuiData(resolve_node(pair_tree))
    builder = SnapshotBuilder(data, NodePoller(data.db_dir), now=lambda: NOW_EPOCH)
    # the build completes; the unparseable count reads as no cap
    measures = builder.build('main.alpha').measures
    assert measures['iter_max'] is None


@pytest.mark.parametrize(
    argnames='payload',
    argvalues=[b'{"agent": "claude \xe9"}', b'null'],
    ids=['undecodable-bytes', 'non-object-json'],
)
def test_build_tolerates_a_mangled_config(
    pair_tree: pathlib.Path,
    payload: bytes,
) -> None:
    """A hand-mangled config.json degrades to the empty config, never a crash.

    config.json is agent-editable, so an edit may leave a non-UTF-8 byte (an
    undecodable read) or a non-object top level (an ``AttributeError`` on
    every ``.get``). The build runs inside the poll worker, so either
    escaping exception would kill the whole cockpit the moment it scopes to
    the node.
    """
    alpha = Node(pair_tree / '.worktrees' / 'main.alpha')
    (alpha.node_dir / 'config.json').write_bytes(payload)
    data = TuiData(resolve_node(pair_tree))
    builder = SnapshotBuilder(data, NodePoller(data.db_dir), now=lambda: NOW_EPOCH)
    # the build completes; the mangled config reads as empty
    builder.build('main.alpha')
    assert data.config('main.alpha') == {}


def test_build_tolerates_an_undecodable_status(pair_tree: pathlib.Path) -> None:
    """A hand-mangled .status degrades to ``idle``, never a crash.

    The tree section reads every branch's ``.status`` on each build, so a
    stray non-UTF-8 byte in one node's file would otherwise kill the whole
    cockpit at any scope.
    """
    alpha = Node(pair_tree / '.worktrees' / 'main.alpha')
    (alpha.node_dir / '.status').write_bytes(b'active \xe9')
    data = TuiData(resolve_node(pair_tree))
    builder = SnapshotBuilder(data, NodePoller(data.db_dir), now=lambda: NOW_EPOCH)
    # the build completes; the undecodable status reads as idle
    builder.build('main')
    assert data.status('main.alpha') == 'idle'


def test_sync_folds_into_its_step(builder: SnapshotBuilder) -> None:
    """SYNC passes stay out of step N/N and fold into the step they precede."""
    snap = builder.build('main.alpha')
    # sync rows share their step's number, yet the denominator is unchanged
    assert snap.measures['step_total'] == 5
    # the explorer lists no standalone sync: step 1 absorbed its sync pass
    # (time spans both, costs sum)
    settled = snap.history[0]['iters'][1]
    assert [step['label'] for step in settled['steps']] == [
        'step 1: PREPARE',
        'step 2: PLAN',
        'step 3: EXECUTE',
        'step 4: REVIEW',
        'step 5: COMMIT',
    ]
    first = settled['steps'][0]
    assert first['cost_raw'] == pytest.approx(0.06)  # 0.02 sync + 0.04 step
    assert first['duration'] == 68.0  # sync start -> step end
    # the log keeps sync rows, attributed to the step they precede
    sync_rows = [
        row for row in snap.log if row['kind'] == 'step' and row['name'] == 'SYNC'
    ]
    assert sync_rows
    assert all(
        row['run_n'] and row['iter_n'] and row['step_n'] == 1 for row in sync_rows
    )
    # the newest activity is the open step's start
    assert (snap.log[0]['kind'], snap.log[0]['n'], snap.log[0]['event']) == (
        'step',
        3,
        'start',
    )


def test_drain_sync_lists_standalone(pair_tree: pathlib.Path) -> None:
    """Drain-wait syncs keep their own explorer rows, in chronological place.

    Only a step's own pre-step sync (recorded with that step's number) folds
    into it: the step-0 drain passes between REVIEW and COMMIT list as
    standalone ``sync`` rows owning their time and cost, and COMMIT's row
    reads only its own.
    """
    alpha = Node(pair_tree / '.worktrees' / 'main.alpha')
    with deterministic_core() as clock:
        clock.at(600.0)
        run_id = alpha.record.run_start()
        clock.at(590.0)
        iter_id = alpha.record.iter_start(run_id=run_id, iter=1)
        # REVIEW settles, two drain-wait passes run, then COMMIT lands
        seeded = (
            (4, 'REVIEW', 580.0, 520.0, 0.10),
            (0, 'SYNC', 510.0, 480.0, 0.03),
            (0, 'SYNC', 470.0, 450.0, 0.02),
            (5, 'COMMIT', 440.0, 400.0, 0.05),
        )
        for step, name, started, ended, cost in seeded:
            clock.at(started)
            step_id = alpha.record.step_start(
                iter_id=iter_id,
                run_id=run_id,
                step=step,
                step_name=name,
            )
            alpha.record.step_cost(step_id=step_id, cost=cost)
            clock.at(ended)
            alpha.record.step_end(step_id=step_id, status='completed', exit_code=0)
        clock.at(390.0)
        alpha.record.iter_end(iter_id=iter_id, status='completed', exit_code=0)
        alpha.record.run_end(run_id=run_id, status='completed', exit_code=0)
    data = TuiData(resolve_node(pair_tree))
    builder = SnapshotBuilder(data, NodePoller(data.db_dir), now=lambda: NOW_EPOCH)
    snap = builder.build('main.alpha')
    steps = snap.history[0]['iters'][0]['steps']
    assert [row['label'] for row in steps] == [
        'step 4: REVIEW',
        'sync',
        'sync',
        'step 5: COMMIT',
    ]
    # each row owns its span and cost; COMMIT absorbs no drain pass
    costs = [row['cost_raw'] for row in steps]
    assert costs == pytest.approx([0.10, 0.03, 0.02, 0.05])
    assert [row['duration'] for row in steps] == [60.0, 30.0, 20.0, 40.0]
    # the running spend still reads "through this row, syncs included"
    assert steps[-1]['iter_spend'] == pytest.approx(0.20)


@pytest.mark.parametrize(
    argnames=('sync_config', 'labels', 'context', 'log_flags'),
    argvalues=[
        (
            True,
            ['step 1: PREPARE', 'step 2: SYNC', 'sync'],
            (2, 'SYNC', 2),
            {('start', False), ('end', False), ('start', True)},
        ),
        (
            False,
            ['step 1: PREPARE', 'step 2: SYNC', 'step 3: SYNC'],
            (3, 'SYNC', 3),
            {('start', False), ('end', False)},
        ),
    ],
    ids=('sync_on', 'sync_off'),
)
def test_a_user_step_named_sync_lists_numbered(
    pair_tree: pathlib.Path,
    sync_config: bool,
    labels: list[str],
    context: tuple,
    log_flags: set[tuple],
) -> None:
    """A step named SYNC is a numbered step, not sync chrome.

    'SYNC' also names the built-in pass, so SYNC rows classify
    structurally: a settled row alone on its number -- a user step file
    named SYNC -- lists under its own number, counts toward the pipeline
    denominator, and its log rows drop the sync muting, whatever the sync
    mode. A still-open SYNC row splits on the mode: under sync mode it
    keeps the standalone ``sync`` shape (a live built-in pass precedes its
    step's row), while with sync off no pass can exist, so it reads as the
    running step itself -- numbered, displayed, and counted immediately.
    """
    alpha = Node(pair_tree / '.worktrees' / 'main.alpha')
    config_path = alpha.node_dir / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config['sync'] = sync_config
    config_path.write_text(json.dumps(config), encoding='utf-8')
    with deterministic_core() as clock:
        clock.at(600.0)
        run_id = alpha.record.run_start()
        clock.at(590.0)
        iter_id = alpha.record.iter_start(run_id=run_id, iter=1)
        seeded = (
            (1, 'PREPARE', 580.0, 540.0, 0.04),
            (2, 'SYNC', 530.0, 500.0, 0.02),
        )
        for step, name, started, ended, cost in seeded:
            clock.at(started)
            step_id = alpha.record.step_start(
                iter_id=iter_id,
                run_id=run_id,
                step=step,
                step_name=name,
            )
            alpha.record.step_cost(step_id=step_id, cost=cost)
            clock.at(ended)
            alpha.record.step_end(step_id=step_id, status='completed', exit_code=0)
        # a still-open SYNC row: a live pass under sync mode, the node's own
        # running step without it
        clock.at(490.0)
        alpha.record.step_start(
            iter_id=iter_id,
            run_id=run_id,
            step=3,
            step_name='SYNC',
        )
    data = TuiData(resolve_node(pair_tree))
    builder = SnapshotBuilder(data, NodePoller(data.db_dir), now=lambda: NOW_EPOCH)
    snap = builder.build('main.alpha')
    steps = snap.history[0]['iters'][0]['steps']
    assert [row['label'] for row in steps] == labels
    # the card's displayed step and the step N/N denominator follow suit
    m = snap.measures
    assert (m['step'], m['step_name'], m['step_total']) == context
    # the log mutes sync passes only, never the node's own SYNC steps
    named = [row for row in snap.log if row['kind'] == 'step' and row['name'] == 'SYNC']
    assert {(row['event'], row['sync']) for row in named} == log_flags


def test_open_spans_tick_through_a_sync_window(pair_tree: pathlib.Path) -> None:
    """Open iter/run elapsed keep ticking while only a SYNC step is active.

    The displayed step falls back to the settled numbered step and reports
    its final wall time, but the open iteration and run measure against the
    live clock -- never the none-valued ellipsis. The card and the open
    run/iter explorer rows all read the currently running session.
    """
    # seed a run parked in a SYNC window: PREPARE settled, SYNC active
    alpha = Node(pair_tree / '.worktrees' / 'main.alpha')
    with deterministic_core() as clock:
        clock.at(600.0)
        run_id = alpha.record.run_start()
        clock.at(590.0)
        iter_id = alpha.record.iter_start(run_id=run_id, iter=1)
        clock.at(580.0)
        step_id = alpha.record.step_start(
            iter_id=iter_id,
            run_id=run_id,
            step=1,
            step_name='PREPARE',
        )
        alpha.record.step_session(
            'claude',
            step_id=step_id,
            model='opus 4.8',
            session='prep-sess',
        )
        clock.at(520.0)
        alpha.record.step_end(step_id=step_id, status='completed', exit_code=0)
        clock.at(510.0)
        sync_id = alpha.record.step_start(
            iter_id=iter_id,
            run_id=run_id,
            step=1,
            step_name='SYNC',
        )
        alpha.record.step_session(
            'claude',
            step_id=sync_id,
            model='opus 4.8',
            session='sync-sess',
        )
    alpha.status_set('active')
    # build with the pinned clock ten minutes past the reference instant
    data = TuiData(resolve_node(pair_tree))
    builder = SnapshotBuilder(data, NodePoller(data.db_dir), now=lambda: NOW_EPOCH)
    snap = builder.build('main.alpha')
    m = snap.measures
    # the displayed step is the settled PREPARE with its final wall time
    assert (m['step'], m['step_name'], m['elapsed_step']) == (1, 'PREPARE', 60.0)
    # the open iteration and run tick on the injected clock
    assert (m['elapsed_iter'], m['elapsed_run']) == (1190.0, 1200.0)
    # the running session shows everywhere, not a settled leftover (nor "-")
    assert snap.card['session'] == 'sync-sess'
    run_row = snap.history[0]
    assert run_row['session'] == 'sync-sess'
    assert run_row['iters'][0]['session'] == 'sync-sess'


def test_user_root_degrades(builder: SnapshotBuilder) -> None:
    """The user (root) node has no runs: the card degrades, nothing breaks."""
    snap = builder.build('main')
    assert (snap.card['status'], snap.card['session']) == ('idle', None)
    assert snap.measures is None
    assert snap.history == ()
    assert snap.sessions == ()
    # its activity is spawn bookkeeping only -- no run/iter/step rows
    assert all(row['kind'] == 'node' for row in snap.log)
    assert snap.geometry.node_width > 0


def test_codex_carries_no_cost_or_sessions(builder: SnapshotBuilder) -> None:
    """A codex node reports no costs and weaves no sessions; time still tracks."""
    snap = builder.build('main.beta')
    card = snap.card
    assert (card['agent'], card['model']) == ('codex', 'gpt-5.1')
    assert (card['session'], card['signal']) == (None, 'finish')
    m = snap.measures
    assert m['cost_step'] is None
    assert m['cost_iter'] == 0
    assert m['cost_run'] == 0.0
    caps = (
        'cap_step_s',
        'cap_step_cost',
        'cap_iter_s',
        'cap_iter_cost',
        'cap_run_s',
        'cap_run_cost',
    )
    assert all(m[cap] is None for cap in caps)
    assert m['iter_max'] == 10
    assert m['elapsed_step'] == 3621.0
    assert snap.sessions == ()


def test_radio_reads_are_the_nodes_own(builder: SnapshotBuilder) -> None:
    """Read state is the owning node's own; feed and archive scope correctly."""
    snap = builder.build('main.alpha', want_feed=True, want_archive=True)
    rows = [(row['channel'], row['subject'], row['read']) for row in snap.messages]
    assert rows == [
        ('public', 'hello', False),
        ('outbox', 'status', False),  # the root's react never touches alpha
        ('inbox', 'note', True),  # alpha read its own inbox
        ('inbox', 'steer', False),  # the root's reply never touches alpha
    ]
    status = snap.messages[1]
    assert (status['pos_reacts'], status['neg_reacts']) == (1, 0)
    # alpha's posts carry the session that wrote them; the root stamps none
    assert status['session'] == session_for('main.alpha', 2, 2)
    assert snap.messages[3]['session'] is None  # the root-sent steer
    # the feed fans out the subtree's public/outbox posts, newest first
    assert [row['subject'] for row in snap.feed] == ['hello', 'status']
    # saved copies always come from the root's archive, tagged with their owner
    assert [(row['subject'], row['node'], row['read']) for row in snap.saved] == [
        ('status', 'main.alpha', True)
    ]


def test_subtree_log_merges_descendants(builder: SnapshotBuilder) -> None:
    """The subtree log merges every descendant's activity, newest first.

    The lazy ``want_subtree_log`` section widens the log to the scope's
    whole subtree (each row branch-attributed) and re-derives the geometry
    so the node column fits the longest descendant leaf; dropping the flag
    restores the scoped log.
    """
    scoped = builder.build('main.alpha')
    assert {row['branch'] for row in scoped.log} == {'main.alpha'}
    merged = builder.build('main.alpha', want_subtree_log=True)
    branches = {row['branch'] for row in merged.log}
    assert {'main.alpha', 'main.alpha.deep', 'main.alpha.stopper'} <= branches
    assert not {branch for branch in branches if not branch.startswith('main.alpha')}
    stamps = [row['created_at'] for row in merged.log]
    assert stamps == sorted(stamps, reverse=True)
    # the node column widens to the longest merged leaf name
    assert merged.geometry.ev_node_w >= len('stopper')
    # toggling off restores the scoped log (and its geometry)
    restored = builder.build('main.alpha')
    assert {row['branch'] for row in restored.log} == {'main.alpha'}
    assert restored.geometry == scoped.geometry


def test_lost_reads_degrade_and_retry(builder: SnapshotBuilder) -> None:
    """A lost read never blanks a build: empty sections now, recovery next tick.

    Every section loader shares one degradation contract over the contended
    store: mark the branch for retry, cache the empty placeholder, keep the
    build alive. The next build folds the retry set back in and fills the
    sections without any new disk movement.
    """
    with pytest.MonkeyPatch().context() as mp:

        def lost(self: TuiData) -> sqlite3.Connection:
            raise sqlite3.OperationalError('database is locked')

        mp.setattr(TuiData, 'connect', lost)
        snap = builder.build('main.alpha', want_feed=True, want_archive=True)
    # the focused sections degrade to their placeholders, never a crash
    assert snap.card is None
    assert snap.history == ()
    assert snap.messages == ()
    assert snap.feed == ()
    assert snap.saved == ()
    # the retry lands on the very next build -- no new disk movement needed
    recovered = builder.build('main.alpha', want_feed=True, want_archive=True)
    assert recovered.card is not None
    assert recovered.history != ()
    assert [row['subject'] for row in recovered.messages] == [
        'hello',
        'status',
        'note',
        'steer',
    ]
    assert [row['subject'] for row in recovered.saved] == ['status']


def test_lost_spend_read_degrades_and_retries(builder: SnapshotBuilder) -> None:
    """A spend read lost after the sections loaded degrades, never crashes.

    The run-spend pass opens its own connection once the focused sections are
    already read, so it can lose alone (a writer landing mid-build). Each
    figure degrades to the cached in-memory math and the scope retries on the
    very next build.
    """
    with pytest.MonkeyPatch().context() as mp:

        def lost(self: TuiData) -> sqlite3.Connection:
            raise sqlite3.OperationalError('database is locked')

        mp.setattr(TuiData, 'reader', lost)
        snap = builder.build('main.alpha')
    # the build survives on the in-memory fallback (the same figure here --
    # the canonical tree carries no orphaned descendants)
    assert snap.card is not None
    assert snap.measures['cost_run'] == pytest.approx(2.82)
    assert snap.history[0]['spend'] == pytest.approx(2.82)
    # the retry lands on the very next build -- no new disk movement needed
    recovered = builder.build('main.alpha')
    assert recovered is not snap
    assert recovered.measures['cost_run'] == pytest.approx(2.82)


def test_live_session_keys_on_the_resolved_backend_name(
    data: TuiData,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The woven-session lookup keys step rows by the backend's name.

    Step rows record ``Agent.name``, so a config that reaches a backend
    through a registry alias (or carries flags) must key the lookup by the
    resolved name, not the configured word; an unregistered agent matches
    no woven rows.
    """

    class Claudette(ClaudeAgent):
        """The claude backend reached through a registry alias."""

    live = session_for('main.alpha', 2, 2)
    monkeypatch.setitem(fractal.core.agent._AGENTS, 'claudette', Claudette)
    with data.reader() as connection:
        assert data.live_session(connection, 'main.alpha', 'claude -v') == live
        assert data.live_session(connection, 'main.alpha', 'claudette') == live
        assert data.live_session(connection, 'main.alpha', 'ghost') is None


# ------ the read firewall


def _query(data: TuiData, branch: Any, reader: Callable) -> Any:
    """Run one connection-scoped reader the way a refresh pass does."""
    connection = data.connect()
    try:
        return reader(connection, branch)
    finally:
        connection.close()


# the entire read surface, by name; each callable takes (data, builder)
_READ_SURFACE: dict[str, Callable[[TuiData, SnapshotBuilder], Any]] = {
    'registry': lambda data, builder: data.registry_branches(),
    'status': lambda data, builder: data.status('main.alpha'),
    'config': lambda data, builder: data.config('main.alpha'),
    'signal': lambda data, builder: _query(data, 'main.alpha', data.signal),
    'tables': lambda data, builder: _query(data, 'main.alpha', data.tables),
    'run-costs': lambda data, builder: _query(data, 'main.alpha', data.run_costs),
    'log-rows': lambda data, builder: _query(data, ('main.alpha',), data.log_rows),
    'message-rows': lambda data, builder: _query(data, 'main.alpha', data.message_rows),
    'react-counts': lambda data, builder: _query(data, 'main.alpha', data.react_counts),
    'channel-rows': lambda data, builder: _query(data, 'main.alpha', data.channel_rows),
    'archive-rows': lambda data, builder: _query(data, 'main', data.archive_rows),
    'live-session': lambda data, builder: _query(
        data=data,
        branch='main.alpha',
        reader=lambda connection, branch: data.live_session(
            connection=connection,
            branch=branch,
            agent='claude',
        ),
    ),
    'snapshot': lambda data, builder: builder.build(
        'main.alpha',
        want_feed=True,
        want_archive=True,
    ),
}


def _read_state(data: TuiData) -> tuple:
    """Every read marker in the central DB: the receipts table, byte for byte."""
    connection = data.connect()
    try:
        reads = data.rows(
            connection=connection,
            query='SELECT message_id, node FROM reads ORDER BY message_id, node',
        )
    finally:
        connection.close()
    return tuple((row['message_id'], row['node']) for row in reads)


@pytest.mark.parametrize('surface', sorted(_READ_SURFACE))
def test_read_surface_never_stamps_read_state(
    data: TuiData,
    builder: SnapshotBuilder,
    surface: str,
) -> None:
    """The whole read path is pure: no ``read_at`` stamps, no receipts.

    ``Radio.feed``/``read``/``reply``/``react`` all mutate read state -- the
    poll path must never reach them. Every reader (and a full feed+archive
    snapshot build) leaves the read markers of every node byte-identical.
    """
    before = _read_state(data)
    _READ_SURFACE[surface](data, builder)
    assert _read_state(data) == before
