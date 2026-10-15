"""Test the ``fractal.core.time`` module.

Time accounting for a node (the ``Time`` ledger).

Behavior pins for the deadline readers over persisted rows: countdowns,
anchoring, the soonest-of default, and pause credit.

The time matrix's semantics:

- ``--timeout`` is a **whole-run** budget. ``Time.remaining(scope='run')``
  reports ``timeout`` minus wall-clock elapsed since the active run's
  ``started_at``, clamped at ``0``.
- ``--iter-timeout`` is a **per-iteration** budget.
  ``Time.remaining(scope='iter')`` reports ``iter_timeout`` minus elapsed
  since the active iteration's ``started_at``, clamped at ``0``.
- ``--step-timeout`` is the **per-step** default. ``Time.remaining(scope='step')``
  reports the active step's effective ceiling -- its step file's ``timeout:``
  line when it carries one, else ``step_timeout`` -- minus elapsed since the
  active step's ``started_at``.
- With no ``scope`` the method returns the **soonest** of the configured
  run/iter/step deadlines -- the time until the next timeout fires.

The discriminating test is ``test_run_scope_anchors_on_run_iter_scope_on_iter``:
aging only the iteration must not shrink the run budget (a per-iteration
``--timeout`` would), and vice versa.

Uses the in-process ``node_with_db`` fixture and controls elapsed time by
back-dating ``started_at`` (deterministic, no sleeps).
"""

from __future__ import annotations

from typing import Optional

import pytest

from fractal.core.node import Node
from tests._helpers import _age_iter, _age_run, _age_step, _past_timestamp

__all__ = [
    'test_time_remaining_none_without_any_timeout',
    'test_time_limit_reads_configured_scopes',
    'test_time_limit_reads_a_numeric_duration_as_unset',
    'test_run_timeout_counts_down_from_run_start',
    'test_run_timeout_clamps_to_zero_on_overspend',
    'test_run_timeout_none_without_active_run',
    'test_iter_timeout_counts_down_from_iteration_start',
    'test_iter_timeout_none_without_active_iteration',
    'test_interval_defaults_the_iteration_limit_to_the_slot',
    'test_run_scope_anchors_on_run_iter_scope_on_iter',
    'test_default_reports_soonest_of_run_and_iter',
    'test_step_limit_follows_the_active_steps_timeout',
    'test_step_limit_prefers_the_seats_exported_ceiling',
    'test_step_limit_tells_same_named_step_files_apart',
    'test_idle_step_limit_counts_step_file_timeouts',
    'test_steered_step_counts_down_from_its_stuck_launch',
    'test_time_remaining_credits_paused_spans',
]

# a 10-minute budget, in the suffix form the loop validates
TIMEOUT = '10m'
TIMEOUT_SECONDS = 600.0

# a node step_timeout below the step file's own timeout: line
NODE_STEP_TIMEOUT = '2h'
NODE_STEP_TIMEOUT_SECONDS = 7200.0


# ------ deadlines


def test_time_remaining_none_without_any_timeout(node_with_db: Node) -> None:
    """No configured timeout -> ``None`` even with an active run and iteration."""
    node = node_with_db
    run_id = node.record.run_start()
    node.record.iter_start(run_id=run_id, iter=1)
    assert node.time.remaining() is None
    assert node.time.remaining(scope='run') is None
    assert node.time.remaining(scope='iter') is None


def test_time_limit_reads_configured_scopes(node_with_db: Node) -> None:
    """``Time.limit`` reads config for an idle node (``run`` maps to ``timeout``).

    Nothing active, so it reads config alone and distinguishes "no limit"
    from "not running" for an idle node; with no scope the soonest
    configured limit (the smallest) answers.
    """
    node = node_with_db
    # nothing configured -> None for every scope and the default
    assert node.time.limit() is None
    assert node.time.limit('run') is None
    # the run scope reads the whole-run 'timeout' key; iter/step their own
    node.config.set('timeout', TIMEOUT)
    node.config.set('iter_timeout', '5m')
    assert node.time.limit('run') == TIMEOUT_SECONDS
    assert node.time.limit('iter') == 300.0
    assert node.time.limit('step') is None
    # the no-scope default is the soonest configured limit
    assert node.time.limit() == 300.0


@pytest.mark.parametrize(
    argnames=('key', 'scope'),
    argvalues=[
        ('timeout', 'run'),
        ('iter_timeout', 'iter'),
        ('step_timeout', 'step'),
    ],
    ids=['run', 'iter', 'step'],
)
def test_time_limit_reads_a_numeric_duration_as_unset(
    node_with_db: Node,
    key: str,
    scope: str,
) -> None:
    """A bare-number duration in config reads as no limit, never a crash.

    ``config.json`` is hand-editable and ``Config.set`` stores any value, so
    a duration can arrive as a number with no unit suffix; like any duration
    that does not parse, it reads as no limit (the cockpit shows it as no
    cap) rather than raising out of every reader of the limit.
    """
    node = node_with_db
    # whole seconds with no unit suffix, as a hand edit writes them
    node.config.set(key, 3600)
    assert node.time.limit(scope) is None
    assert node.time.limit() is None


def test_run_timeout_counts_down_from_run_start(node_with_db: Node) -> None:
    """``--timeout`` remaining is ``timeout`` minus elapsed for the active run."""
    node = node_with_db
    node.config.set('timeout', TIMEOUT)
    run_id = node.record.run_start()
    node.record.iter_start(run_id=run_id, iter=1)
    _age_run(node, run_id, 100.0)
    remaining = node.time.remaining(scope='run')
    assert remaining is not None
    assert 498.0 < remaining <= TIMEOUT_SECONDS - 100.0
    # only the run timeout is configured, so the no-scope default tracks it
    default = node.time.remaining()
    assert default is not None
    assert abs(default - remaining) < 5.0


def test_run_timeout_clamps_to_zero_on_overspend(node_with_db: Node) -> None:
    """A run older than its budget reports ``0.0``, never negative."""
    node = node_with_db
    node.config.set('timeout', TIMEOUT)
    run_id = node.record.run_start()
    node.record.iter_start(run_id=run_id, iter=1)
    _age_run(node, run_id, TIMEOUT_SECONDS + 100.0)
    assert node.time.remaining(scope='run') == 0.0


def test_run_timeout_none_without_active_run(node_with_db: Node) -> None:
    """A configured ``--timeout`` with no active run -> ``None``."""
    node = node_with_db
    node.config.set('timeout', TIMEOUT)
    # a run that has ended is not active -> the run deadline no longer applies
    run_id = node.record.run_start()
    node.record.run_end(run_id=run_id, status='completed', exit_code=0)
    assert node.time.remaining(scope='run') is None


def test_iter_timeout_counts_down_from_iteration_start(node_with_db: Node) -> None:
    """``--iter-timeout`` remaining is the budget minus the active iteration's elapsed."""
    node = node_with_db
    node.config.set('iter_timeout', TIMEOUT)
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    _age_iter(node, iter_id, 100.0)
    remaining = node.time.remaining(scope='iter')
    assert remaining is not None
    assert 498.0 < remaining <= TIMEOUT_SECONDS - 100.0


def test_iter_timeout_none_without_active_iteration(node_with_db: Node) -> None:
    """A configured ``--iter-timeout`` with no active iteration -> ``None``."""
    node = node_with_db
    node.config.set('iter_timeout', TIMEOUT)
    node.record.run_start()
    assert node.time.remaining(scope='iter') is None


def test_interval_defaults_the_iteration_limit_to_the_slot(node_with_db: Node) -> None:
    """An interval node with no ``--iter-timeout`` is still slot-bounded.

    The loop bounds every interval iteration by its cadence, so the readers
    must mirror that default: ``limit('iter')`` answers with the slot,
    ``remaining(scope='iter')`` counts it down from the active iteration's
    start (the resume anchor reads this credited figure), and an explicit
    (tighter) ``--iter-timeout`` still wins.
    """
    node = node_with_db
    node.config.set('interval', TIMEOUT)
    assert node.time.limit('iter') == TIMEOUT_SECONDS
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    _age_iter(node, iter_id, 100.0)
    remaining = node.time.remaining(scope='iter')
    assert remaining is not None
    assert 498.0 < remaining <= TIMEOUT_SECONDS - 100.0
    # an explicit tighter iter_timeout is honored, never loosened to the slot
    node.config.set('iter_timeout', '5m')
    assert node.time.limit('iter') == 300.0


def test_run_scope_anchors_on_run_iter_scope_on_iter(node_with_db: Node) -> None:
    """``--timeout`` anchors on run start; ``--iter-timeout`` on iteration start.

    This is the run-anchoring's load-bearing test. Aging *only the iteration*
    must leave the run budget nearly full (a per-iteration ``--timeout`` would
    drain it); the iteration budget, anchored on the iteration, is the one that
    shrinks.
    """
    node = node_with_db
    node.config.set('timeout', TIMEOUT)
    node.config.set('iter_timeout', TIMEOUT)
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    # age only the iteration; leave the run fresh
    _age_iter(node, iter_id, 500.0)
    run_remaining = node.time.remaining(scope='run')
    iter_remaining = node.time.remaining(scope='iter')
    assert run_remaining is not None
    assert iter_remaining is not None
    # run barely elapsed (anchored on the fresh run start)
    assert run_remaining > TIMEOUT_SECONDS - 60.0
    # iteration heavily elapsed (anchored on the aged iteration start)
    assert iter_remaining <= TIMEOUT_SECONDS - 500.0


def test_default_reports_soonest_of_run_and_iter(node_with_db: Node) -> None:
    """With both configured, the no-scope default returns the soonest deadline."""
    node = node_with_db
    node.config.set('timeout', TIMEOUT)
    node.config.set('iter_timeout', TIMEOUT)
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    # run aged little, iteration aged a lot -> the iteration is the soonest
    _age_run(node, run_id, 50.0)
    _age_iter(node, iter_id, 450.0)
    default = node.time.remaining()
    iter_remaining = node.time.remaining(scope='iter')
    run_remaining = node.time.remaining(scope='run')
    assert default is not None
    assert iter_remaining is not None
    assert run_remaining is not None
    # the default tracks the iteration (soonest), not the run
    assert abs(default - iter_remaining) < 5.0
    assert default < run_remaining


@pytest.mark.parametrize(
    argnames=('frontmatter', 'limit'),
    argvalues=[
        ('---\ntimeout: 6h\n---\n', 21600.0),
        ('', NODE_STEP_TIMEOUT_SECONDS),
        ('---\ntimeout: soon\n---\n', NODE_STEP_TIMEOUT_SECONDS),
    ],
    ids=['own-timeout', 'node-step-timeout', 'malformed-timeout'],
)
def test_step_limit_follows_the_active_steps_timeout(
    node_with_db: Node,
    frontmatter: str,
    limit: float,
) -> None:
    """The step scope counts down the active step's effective ceiling.

    The loop bounds a step by its file's ``timeout:`` line when it parses,
    else by the node's ``step_timeout``, so ``limit``, ``remaining``, and
    the soonest default must answer with the same ceiling -- a reader on
    the node value would tell a seat in a 6h step that its 2h are nearly
    gone.
    """
    node = node_with_db
    node.config.set('step_timeout', NODE_STEP_TIMEOUT)
    step_id = _active_step(node, frontmatter)
    _age_step(node, step_id, 100.0)
    assert node.time.limit('step') == limit
    remaining = node.time.remaining(scope='step')
    assert remaining is not None
    assert limit - 105.0 < remaining <= limit - 100.0
    # the step deadline is the only one, so the soonest default tracks it
    default = node.time.remaining()
    assert default is not None
    assert abs(default - remaining) < 5.0


@pytest.mark.parametrize(
    argnames=('seat', 'limit'),
    argvalues=[(True, 14400.0), (False, 21600.0)],
    ids=['own-seat', 'other-caller'],
)
def test_step_limit_prefers_the_seats_exported_ceiling(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    seat: bool,
    limit: float,
) -> None:
    """The node's own seat reads the ceiling its launch exported.

    The loop reads step files once per iteration, so an edit landing
    mid-iteration changes the file but not the enforced ceiling; the
    seat's ``STEP_TIMEOUT_SECONDS`` is that ceiling. Any caller whose
    ``STEP_ID`` is not the active step's (another node's seat, an
    operator) reads the step file.
    """
    node = node_with_db
    node.config.set('step_timeout', NODE_STEP_TIMEOUT)
    step_id = _active_step(node, '---\ntimeout: 6h\n---\n')
    # the launch exported 4h; the file was edited to 6h since
    monkeypatch.setenv('STEP_ID', f'{step_id if seat else step_id + 1}')
    monkeypatch.setenv('STEP_TIMEOUT_SECONDS', '14400')
    assert node.time.limit('step') == limit


@pytest.mark.parametrize(
    argnames='files',
    argvalues=[
        ('00-x.md', '01-plan.md', '03-plan.md'),
        ('00-x.md', '01-plan.md', '02-exec.md', '03-plan.md'),
    ],
    ids=['renamed', 'inserted'],
)
def test_step_limit_tells_same_named_step_files_apart(
    node_with_db: Node,
    files: tuple[str, ...],
) -> None:
    """A shifted step file still resolves to the one the active row ran.

    The iteration began on ``01-plan``, a middle step, and ``03-plan`` (1h
    and 3h); ``00-x`` was added since, shifting every position. Step 3 is
    the second ``plan``, so it reads 3h, never the first file of the name.
    """
    node = node_with_db
    node.config.set('step_timeout', NODE_STEP_TIMEOUT)
    steps_dir = node.node_dir / 'steps'
    steps_dir.mkdir()
    timeouts = {'01-plan.md': '1h', '03-plan.md': '3h'}
    for name in files:
        frontmatter = ''
        if name in timeouts:
            frontmatter = f'---\ntimeout: {timeouts[name]}\n---\n'
        (steps_dir / name).write_text(f'{frontmatter}# step\n', encoding='utf-8')
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    for number, name in ((1, 'plan'), (2, 'exec'), (3, 'plan')):
        step_id = node.record.step_start(
            iter_id=iter_id,
            run_id=run_id,
            step=number,
            step_name=name,
        )
        if number < 3:
            node.record.step_end(step_id=step_id, status='completed', exit_code=0)
    assert node.time.limit('step') == 10800.0


@pytest.mark.parametrize(
    argnames=('node_timeout', 'frontmatter', 'limit'),
    argvalues=[
        (None, '---\ntimeout: 6h\n---\n', 21600.0),
        (NODE_STEP_TIMEOUT, '---\ntimeout: 6h\n---\n', NODE_STEP_TIMEOUT_SECONDS),
        (None, '', None),
    ],
    ids=['step-files-only', 'node-step-timeout', 'none'],
)
def test_idle_step_limit_counts_step_file_timeouts(
    node_with_db: Node,
    node_timeout: Optional[str],
    frontmatter: str,
    limit: Optional[float],
) -> None:
    """An idle node's step limit exists when only its step files carry one.

    With no current step, the node's ``step_timeout`` answers, else the
    tightest step file's own ``timeout:`` -- so ``time remaining`` reads
    "not running" rather than "no limit", as the run and iteration scopes
    do when a limit is set but nothing runs.
    """
    node = node_with_db
    if node_timeout is not None:
        node.config.set('step_timeout', node_timeout)
    steps_dir = node.node_dir / 'steps'
    steps_dir.mkdir()
    (steps_dir / '01-EXECUTE.md').write_text(
        f'{frontmatter}# EXECUTE\n', encoding='utf-8'
    )
    (steps_dir / '02-COMMIT.md').write_text('# COMMIT\n', encoding='utf-8')
    assert node.time.limit('step') == limit


@pytest.mark.parametrize(
    argnames=('status', 'metadata', 'charged'),
    argvalues=[
        ('stopped', 'steered: no progress for 1800s', 600.0),
        ('failed', 'agent error (exit 1)', 0.0),
    ],
    ids=['steer', 'retry'],
)
def test_steered_step_counts_down_from_its_stuck_launch(
    node_with_db: Node,
    status: str,
    metadata: str,
    charged: float,
) -> None:
    """A steered relaunch's countdown keeps its stuck launch's span.

    The loop charges the stuck launch's seconds against the steered
    launch's ceiling, so the step deadline runs from the attempt's first
    launch; a retry after a failed attempt arms a fresh deadline.
    """
    node = node_with_db
    node.config.set('step_timeout', NODE_STEP_TIMEOUT)
    first_id = _active_step(node, '')
    # the first launch ran 600s (1000s ago to 400s ago), then closed
    node.record.step_end(
        step_id=first_id, status=status, exit_code=0, metadata=metadata
    )
    node.db.update(
        data={
            'started_at': _past_timestamp(1000.0),
            'ended_at': _past_timestamp(400.0),
        },
        table='steps',
        where={'step_id': first_id},
    )
    row = node.db.read('steps', where={'step_id': first_id})[0]
    step_id = node.record.step_start(
        iter_id=row['iter_id'],
        run_id=row['run_id'],
        step=1,
        step_name='EXECUTE',
    )
    _age_step(node, step_id, 100.0)
    remaining = node.time.remaining(scope='step')
    assert remaining is not None
    expected = NODE_STEP_TIMEOUT_SECONDS - 100.0 - charged
    assert expected - 5.0 < remaining <= expected


# ------ pause credit


def test_time_remaining_credits_paused_spans(node_with_db: Node) -> None:
    """Run and iteration deadlines credit the time spent paused.

    The rows stay open across a pause, so the raw wall clock would charge
    the frozen span against ``--timeout``/``--iter-timeout`` and a long
    pause would end the run the moment it resumed. The pause/resume event
    instants give the span back; an iteration credits only the part inside
    it.
    """
    node = node_with_db
    node.config.set('timeout', '10m')
    node.config.set('iter_timeout', '5m')
    node.status_set('active')
    run_id = node.record.run_start()
    _age_run(node, run_id, 300.0)
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    _age_iter(node, iter_id, 150.0)
    # a pause span from 240s ago to 60s ago (180s parked; 90s inside the iter)
    for event, seconds_ago in (('pause', 240.0), ('resume', 60.0)):
        event_id = node.record.event_start(event)
        node.record.event_end(event_id=event_id, status='completed')
        node.db.update(
            data={'created_at': _past_timestamp(seconds_ago)},
            table='events',
            where={'event_id': event_id},
        )
    # run: 600 - (300 elapsed - 180 credit) = 480
    run_remaining = node.time.remaining(scope='run', run_id=run_id)
    assert run_remaining is not None
    assert 470.0 < run_remaining <= 481.0
    # iter: 300 - (150 elapsed - 90 credit clipped to the iter) = 240
    iter_remaining = node.time.remaining(scope='iter', run_id=run_id)
    assert iter_remaining is not None
    assert 230.0 < iter_remaining <= 241.0
    # a failed resume never relaunched the loop: the span it would have
    # closed stays open and keeps accruing (pause 30s ago -> ~30s more)
    for event, status, seconds_ago in (
        ('pause', 'completed', 30.0),
        ('resume', 'failed', 10.0),
    ):
        event_id = node.record.event_start(event)
        node.record.event_end(event_id=event_id, status=status)
        node.db.update(
            data={'created_at': _past_timestamp(seconds_ago)},
            table='events',
            where={'event_id': event_id},
        )
    # run: 600 - (300 elapsed - (180 + ~30) credit) = ~510
    run_remaining = node.time.remaining(scope='run', run_id=run_id)
    assert run_remaining is not None
    assert 500.0 < run_remaining <= 511.0


# ------ helpers


def _active_step(node: Node, frontmatter: str) -> int:
    """Open a run, an iteration, and step 1 of a one-file ``steps/`` dir.

    The step file carries ``frontmatter`` ahead of its body; returns the
    active step row's id.
    """
    steps_dir = node.node_dir / 'steps'
    steps_dir.mkdir(exist_ok=True)
    (steps_dir / '01-EXECUTE.md').write_text(
        f'{frontmatter}# EXECUTE\n\nWork.\n',
        encoding='utf-8',
    )
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    return node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='EXECUTE',
    )
