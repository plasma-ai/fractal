"""Test the ``fractal.core.record`` module."""

from __future__ import annotations

import itertools
import pathlib
import re
import uuid
from typing import Any

import pytest

from fractal.core.node import Node
from fractal.typing import Row

__all__ = [
    'test_run_iteration_record_default_agent_model_session',
    'test_step_records_agent_model_session',
    'test_rows_carry_their_started_numbers',
    'test_iter_end_backfills_model_from_steps',
    'test_terminal_end_records_reason',
    'test_terminal_writes_are_first_writer_wins',
    'test_row_closers_transition_once_and_report_it',
    'test_run_start_reconciles_stranded_lifecycle',
    'test_stranded_run_ends_before_its_successor_starts',
    'test_close_open_cascade_commits_atomically',
    'test_close_open_marks_unpriced_only_while_cost_absent',
    'test_close_open_event_sweep_is_first_writer_wins',
    'test_run_start_stamps_armed_cap',
    'test_run_open_resolves_re_entry',
    'test_step_pending_supersedes_stale_twin',
    'test_approval_gate_is_first_approval_wins',
    'test_event_lifecycle',
    'test_event_lineage_is_active_only',
    'test_event_explicit_lineage_wins',
    'test_signal_lifecycle',
    'test_list_readers_mirror_row_filters',
    'test_activity_reconstructs_lifecycle',
    'test_activity_end_rows_carry_duration_and_cost',
    'test_session_transcript_reads_claude_and_codex',
    'test_iteration_records_the_served_model_over_the_pin',
]


# ------ run / iteration / step spans


def test_run_iteration_record_default_agent_model_session(node_with_db: Node) -> None:
    """Run/iteration record the node's default agent, model, and woven session."""
    node = node_with_db
    node.config.set('agent', 'claude')
    node.config.set('model', 'claude-opus-4-8')

    # run + iteration record the node's default agent
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    # continuous mode weaves a session for the default agent
    node.sessions.set('claude', 'sess-abc')
    node.record.iter_end(iter_id=iter_id, status='completed', exit_code=0)

    run = node.db.read('runs', where={'run_id': run_id})[0]
    iter_row = node.db.read('iters', where={'iter_id': iter_id})[0]
    assert run['agent'] == 'claude'
    assert iter_row['agent'] == 'claude'
    assert iter_row['model'] == 'claude-opus-4-8'
    assert iter_row['session'] == 'sess-abc'


def test_step_records_agent_model_session(node_with_db: Node) -> None:
    """A step records the agent, the model that ran it, and the real session."""
    node = node_with_db
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    step_id = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='EXECUTE',
    )
    # captured from the agent stream (stream-reported model, configured fallback)
    node.record.step_session(
        'claude',
        step_id=step_id,
        model='claude-opus-4-8',
        session='sess-xyz',
    )

    step = node.db.read('steps', where={'step_id': step_id})[0]
    assert step['agent'] == 'claude'
    assert step['model'] == 'claude-opus-4-8'
    assert step['session'] == 'sess-xyz'


def test_rows_carry_their_started_numbers(node_with_db: Node) -> None:
    """Iteration and step rows store the numbers they are started with.

    The ``iter`` column carries the human iteration number (never the
    surrogate ``iter_id``), steps keep the number they are started with
    (SYNC is 0, work steps are 1-based), and a started row is ``active``
    until its closer lands.
    """
    node = node_with_db
    record = node.record
    run_id = record.run_start()
    iter_id = record.iter_start(run_id=run_id, iter=7)
    sync = record.step_start(iter_id=iter_id, run_id=run_id, step=0, step_name='SYNC')
    work = record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PREPARE',
    )
    assert node.db.read('iters', where={'iter_id': iter_id})[0]['iter'] == 7
    assert node.db.read('steps', where={'step_id': sync})[0]['step'] == 0
    started = node.db.read('steps', where={'step_id': work})[0]
    assert started['step'] == 1
    assert started['status'] == 'active'
    record.step_end(step_id=work, status='completed', exit_code=0)
    assert node.db.read('steps', where={'step_id': work})[0]['status'] == 'completed'


def test_iter_end_backfills_model_from_steps(node_with_db: Node) -> None:
    """``iter_end`` fills an unset iteration model from the steps' recorded one.

    A defaulted spawn configures no model, so ``iter_start`` records none --
    but the steps record the actual model the agent stream reported, and
    the iteration inherits it when every step agrees.
    """
    node = node_with_db
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    step_id = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='EXECUTE',
    )
    node.record.step_session(
        'claude',
        step_id=step_id,
        model='claude-fable-5',
        session='sess-fill',
    )
    node.record.iter_end(iter_id=iter_id, status='completed', exit_code=0)

    iter_row = node.db.read('iters', where={'iter_id': iter_id})[0]
    assert iter_row['model'] == 'claude-fable-5'


def test_terminal_end_records_reason(node_with_db: Node) -> None:
    """``step_end``/``iter_end``/``run_end`` stamp an optional reason into metadata.

    ``node activity`` surfaces row metadata, so a short reason recorded at the end
    of a step, iteration, or run (e.g. ``agent error``) explains a failed row; a
    clean end passes no reason and leaves the metadata untouched.
    """
    node = node_with_db
    record = node.record
    run_id = record.run_start()
    iter_id = record.iter_start(run_id=run_id, iter=1)
    # a failed step with a reason, and a clean step with none
    failed = record.step_start(iter_id=iter_id, run_id=run_id, step=1, step_name='EXEC')
    record.step_end(
        step_id=failed,
        status='failed',
        exit_code=1,
        metadata='agent error',
    )
    ok = record.step_start(iter_id=iter_id, run_id=run_id, step=2, step_name='EXEC')
    record.step_end(step_id=ok, status='completed', exit_code=0)
    steps = {row['step_id']: row for row in node.db.read('steps')}
    assert steps[failed]['metadata'] == 'agent error'
    assert steps[ok]['metadata'] == ''
    # the iteration and run carry the same optional reason
    record.iter_end(iter_id=iter_id, status='failed', exit_code=1, metadata='timed out')
    record.run_end(run_id=run_id, status='exited', exit_code=1, metadata='Timed out')
    assert node.db.read('iters')[0]['metadata'] == 'timed out'
    assert node.db.read('runs')[0]['metadata'] == 'Timed out'


def test_terminal_writes_are_first_writer_wins(node_with_db: Node) -> None:
    """The first terminal write sticks; a racing second write no-ops.

    A kill racing the loop's own ``step_end`` must not erase the recorded
    outcome -- whichever terminal lands first (stamping ``ended_at``) wins, and
    the later write is a silent no-op via the ``ended_at IS NULL`` guard.
    """
    node = node_with_db
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    step_id = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='EXECUTE',
    )
    # the loop ends the step first (completed)...
    node.record.step_end(step_id=step_id, status='completed', exit_code=0)
    first = node.db.read('steps', where={'step_id': step_id})[0]
    # ...then a racing kill tries to mark it killed -- it must not overwrite
    node.record.step_end(step_id=step_id, status='killed', exit_code=1)
    after = node.db.read('steps', where={'step_id': step_id})[0]
    assert after['status'] == 'completed'
    assert after['exit_code'] == 0
    assert after['ended_at'] == first['ended_at']


def test_row_closers_transition_once_and_report_it(node_with_db: Node) -> None:
    """The run/iter/step closers are first-writer-wins and say who won.

    Every closer guards on ``ended_at IS NULL``: the first terminal
    sticks, and a competing closer writes nothing and observes 0 -- the
    substrate a kill racing the loop's own clean end stands on.
    """
    node = node_with_db
    node.status_set('active')
    record = node.record
    run_id = record.run_start()
    iter_id = record.iter_start(run_id=run_id, iter=1)
    step_id = record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='WORK',
    )
    # the first close wins each row; the loser observes 0 and changes nothing
    assert record.step_end(step_id=step_id, status='completed', exit_code=0) == 1
    assert record.step_end(step_id=step_id, status='killed', exit_code=1) == 0
    assert record.iter_end(iter_id=iter_id, status='completed', exit_code=0) == 1
    assert record.iter_end(iter_id=iter_id, status='killed', exit_code=1) == 0
    assert record.run_end(run_id=run_id, status='completed', exit_code=0) == 1
    assert record.run_end(run_id=run_id, status='killed', exit_code=1) == 0
    statuses = [
        node.db.read('steps', where={'step_id': step_id})[0]['status'],
        node.db.read('iters', where={'iter_id': iter_id})[0]['status'],
        node.db.read('runs', where={'run_id': run_id})[0]['status'],
    ]
    assert statuses == ['completed', 'completed', 'completed']


def test_run_start_reconciles_stranded_lifecycle(node_with_db: Node) -> None:
    """A new run stamps a prior crashed loop's open rows ``exited`` (exit 1).

    The single-tmux-session invariant guarantees a leftover ``active`` run is
    dead, so ``run_start`` reconciles it (and its open iteration/step) to a
    truthful terminal rather than force-closing to ``stopped`` or leaving it open.
    """
    node = node_with_db
    # a crashed loop: run/iteration/step left open (no *_end calls)
    stranded_run = node.record.run_start()
    stranded_iter = node.record.iter_start(run_id=stranded_run, iter=1)
    stranded_step = node.record.step_start(
        iter_id=stranded_iter,
        run_id=stranded_run,
        step=1,
        step_name='PLAN',
    )
    # the next launch starts a fresh run and reconciles the stranded lifecycle
    new_run = node.record.run_start()
    assert new_run != stranded_run
    for table, key, row_id in (
        ('runs', 'run_id', stranded_run),
        ('iters', 'iter_id', stranded_iter),
        ('steps', 'step_id', stranded_step),
    ):
        row = node.db.read(table, where={key: row_id})[0]
        assert row['status'] == 'exited'
        assert row['exit_code'] == 1
        assert row['ended_at'] is not None
    # the fresh run is the sole active one
    active = node.db.read('runs', where={'status': 'active'})
    assert [r['run_id'] for r in active] == [new_run]


def test_stranded_run_ends_before_its_successor_starts(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconciled run's end never postdates the new run's start.

    Runs are one-loop-at-a-time, so the timeline must never show the dead
    predecessor ending after its successor began -- ``run_start`` closes the
    stranded rows before capturing the new run's start instant.
    """
    node = node_with_db
    # a ticking clock: every stamp is a distinct, strictly later instant
    instants = (f'2026-03-27T14:00:00.{ms:03d}Z' for ms in itertools.count(1))
    monkeypatch.setattr('fractal.util.time.utc_now', lambda: next(instants))
    stranded = node.record.run_start()
    successor = node.record.run_start()
    ended = node.db.read('runs', where={'run_id': stranded})[0]['ended_at']
    started = node.db.read('runs', where={'run_id': successor})[0]['started_at']
    assert ended <= started


def test_close_open_cascade_commits_atomically(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash mid-close leaves every row open -- never a partial cascade.

    ``close_open`` closes runs, iterations, and steps in one transaction,
    and ``run_start`` pairs the reconcile with its own insert: a failure
    anywhere inside rolls the whole unit back, so a reader can never see
    a closed run over still-active child rows, or a reconciled lifecycle
    without its successor run.
    """
    node = node_with_db
    record = node.record
    run_id = record.run_start()
    iter_id = record.iter_start(run_id=run_id, iter=1)
    step_id = record.step_start(iter_id=iter_id, run_id=run_id, step=1, step_name='A')
    # a crash after the runs close, before the iters close, rolls back all
    real_fenced = record._fenced

    def crashing_fenced(data: Row, table: str, **kwargs: Any) -> int:
        if table == 'iters':
            raise RuntimeError('crash')
        return real_fenced(data, table, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(record, '_fenced', crashing_fenced)
        with pytest.raises(RuntimeError):
            record.close_open('killed')
    for table, key, row_id in (
        ('runs', 'run_id', run_id),
        ('iters', 'iter_id', iter_id),
        ('steps', 'step_id', step_id),
    ):
        row = node.db.read(table, where={key: row_id})[0]
        assert row['status'] == 'active'
        assert row['ended_at'] is None

    # a crash on the successor's insert rolls the reconcile back with it
    def crashing_write(data: Row, table: str, **kwargs: Any) -> int:
        raise RuntimeError('crash')

    with monkeypatch.context() as patch:
        patch.setattr(node.db, 'write', crashing_write)
        with pytest.raises(RuntimeError):
            record.run_start()
    row = node.db.read('runs', where={'run_id': run_id})[0]
    assert row['status'] == 'active'
    assert row['ended_at'] is None


def test_close_open_marks_unpriced_only_while_cost_absent(
    node_with_db: Node,
) -> None:
    """A cost flush racing the close keeps the priced step unlabeled.

    ``close_open``'s cascade holds the write lock, so a per-frame flush
    serializes around it: landing before, the priced row never enters the
    unpriced read; landing after, ``step_cost`` strips the stale marker --
    a priced step never reads ``unpriced``, while a step whose cost never
    flushed still gets the marker.
    """
    node = node_with_db
    record = node.record
    run_id = record.run_start()
    iter_id = record.iter_start(run_id=run_id, iter=1)
    # three streamed steps left open by a kill: one flushed before the
    # close, one flushed after, one never flushed
    early = record.step_start(iter_id=iter_id, run_id=run_id, step=1, step_name='A')
    record.step_session('claude', step_id=early, model=None, session='sess-a')
    record.step_cost(step_id=early, cost=0.42)
    late = record.step_start(iter_id=iter_id, run_id=run_id, step=2, step_name='B')
    record.step_session('claude', step_id=late, model=None, session='sess-b')
    unpriced = record.step_start(iter_id=iter_id, run_id=run_id, step=3, step_name='C')
    record.step_session('claude', step_id=unpriced, model=None, session='sess-c')
    record.close_open('killed')
    record.step_cost(step_id=late, cost=0.07)
    rows = {row['step_id']: row for row in node.db.read('steps')}
    assert rows[early]['cost'] == 0.42
    assert rows[early]['metadata'] == ''
    assert rows[late]['cost'] == 0.07
    assert rows[late]['metadata'] == ''
    assert rows[unpriced]['metadata'] == 'unpriced'


def test_close_open_event_sweep_is_first_writer_wins(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An event finalized during the kill sweep keeps its outcome.

    ``close_open`` reads the active events and then closes each one; an
    ``event_end`` landing between the read and the write (a parent approve
    racing the kill) must win, so the sweep transitions only rows still
    active -- while a stray event nothing finalized is still swept.
    """
    node = node_with_db
    record = node.record
    record.run_start()
    kill_event = record.event_start('kill')
    racing = record.event_start('approve', metadata='main.x')
    stray = record.event_start('spawn', metadata='main.x')
    # land the finalize inside the sweep's window: after the active read,
    # before the close writes
    real_read = node.db.read

    def racing_read(table: str, **kwargs: Any) -> list[Row]:
        rows = real_read(table, **kwargs)
        if table == 'events':
            record.event_end(event_id=racing, status='completed', exit_code=0)
        return rows

    monkeypatch.setattr(node.db, 'read', racing_read)
    record.close_open('killed', skip_event=kill_event)
    rows = {row['event_id']: row for row in real_read('events')}
    # the racing finalize wins; the stray event is swept; the in-flight
    # kill event is left for Node._kill's own event_end
    assert rows[racing]['status'] == 'completed'
    assert rows[racing]['exit_code'] == 0
    assert rows[stray]['status'] == 'killed'
    assert rows[stray]['exit_code'] == 1
    assert rows[kill_event]['status'] == 'active'


def test_run_start_stamps_armed_cap(node_with_db: Node) -> None:
    """``run_start`` stamps the cost cap armed at launch on the run row.

    The stamp is start-time-immutable -- a later config retune never
    rewrites what the run was armed with -- and an uncapped run stamps
    NULL. The activity feed's run-start row labels the armed cap in its
    metadata (empty when uncapped).
    """
    node = node_with_db
    record = node.record
    # an uncapped run stamps NULL (cap unknown reads as no label)
    uncapped = record.run_start()
    assert node.db.read('runs', where={'run_id': uncapped})[0]['max_cost'] is None
    record.run_end(run_id=uncapped, status='completed', exit_code=0)
    # a capped run stamps the configured cap, immune to a later retune
    node.config.set('max_cost', 0.15)
    capped = record.run_start()
    node.config.set('max_cost', 5.0)
    assert node.db.read('runs', where={'run_id': capped})[0]['max_cost'] == 0.15
    # the activity run-start rows carry the armed-cap label
    starts = {
        row['run_id']: row
        for row in record.activity()
        if row['event'] == 'start'
        and row['event_id'] is None
        and row['iter_id'] is None
    }
    assert starts[capped]['metadata'] == 'max_cost $0.15'
    assert starts[uncapped]['metadata'] == ''


def test_run_open_resolves_re_entry(node_with_db: Node) -> None:
    """``run_open`` derives the re-entry from completed rows and approvals.

    The completed rows say where the pause left the iteration (a checkpoint
    or drain park writes no paused row); an approved awaiting-approval step
    is skipped past even when a later pause cycle wrote a newer paused row
    at the next step -- the lookup is scoped per step, so nothing shadows
    an earlier approval.
    """
    node = node_with_db
    node.status_set('active')
    record = node.record
    run_id = record.run_start()
    # no iterations yet: only the run is adoptable
    context = record.run_open()
    assert context == {
        'run_id': run_id,
        'iter': None,
        'iter_id': None,
        'resume_step': None,
    }
    iter_id = record.iter_start(run_id=run_id, iter=1)
    # steps 1-2 completed, step 3 paused awaiting approval (then approved),
    # step 4 paused by a later cycle (a plain mid-step abort)
    for number in (1, 2):
        step_id = record.step_start(
            iter_id=iter_id,
            run_id=run_id,
            step=number,
            step_name='WORK',
        )
        record.step_end(step_id=step_id, status='completed', exit_code=0)
    awaiting = record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=3,
        step_name='GATE',
    )
    record.step_pending(step_id=awaiting)
    record.step_end(
        step_id=awaiting,
        status='paused',
        exit_code=0,
        metadata='awaiting approval',
    )
    shadowing = record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=4,
        step_name='WORK',
    )
    record.step_end(step_id=shadowing, status='paused', exit_code=0)
    # unapproved: re-entry holds at the awaiting step
    context = record.run_open()
    assert context is not None
    assert context['iter_id'] == iter_id
    assert context['resume_step'] == 3
    # approved while parked: re-entry skips past it, undeterred by the
    # newer paused row at step 4
    record.step_approve(step_id=awaiting)
    context = record.run_open()
    assert context is not None
    assert context['resume_step'] == 4
    # a closed newest iteration anchors only the numbering (boundary pause)
    record.iter_end(iter_id=iter_id, status='completed', exit_code=0)
    context = record.run_open()
    assert context is not None
    assert context['iter'] == 1
    assert context['iter_id'] is None
    assert context['resume_step'] is None


# ------ approval gate


def test_step_pending_supersedes_stale_twin(node_with_db: Node) -> None:
    """A re-run step's fresh pending row voids its superseded twin.

    An unapproved pause/resume re-runs the step on a fresh row; the old
    paused row's pending state would otherwise sit in ``pending`` forever,
    silently swallowing approvals aimed at it.
    """
    node = node_with_db
    node.status_set('active')
    record = node.record
    run_id = record.run_start()
    iter_id = record.iter_start(run_id=run_id, iter=1)
    stale = record.step_start(iter_id=iter_id, run_id=run_id, step=1, step_name='GATE')
    record.step_pending(step_id=stale)
    record.step_end(
        step_id=stale,
        status='paused',
        exit_code=0,
        metadata='awaiting approval',
    )
    # the re-run opens a fresh row and re-arms the gate on it alone
    fresh = record.step_start(iter_id=iter_id, run_id=run_id, step=1, step_name='GATE')
    record.step_pending(step_id=fresh)
    rows = {row['step_id']: row['approved'] for row in node.db.read('steps')}
    assert rows[fresh] == ''
    assert rows[stale] is None
    # an approval aimed at the voided gate writes nothing -- the stale row
    # stays voided instead of resurrecting with a timestamp
    assert record.step_approve(step_id=stale) == 0
    assert node.db.read('steps', where={'step_id': stale})[0]['approved'] is None


def test_approval_gate_is_first_approval_wins(node_with_db: Node) -> None:
    """The approval gate is a compare-and-swap on the pending state.

    A re-approve keeps the original instant, and a stray re-pend cannot
    demote an approval back to pending -- the gate only ever moves
    NULL -> pending -> approved. The ``step_approved`` read (the loop's
    wait gate) blocks only on the pending state: a NULL step needs no
    approval and an approved one has it.
    """
    node = node_with_db
    node.status_set('active')
    record = node.record
    run_id = record.run_start()
    iter_id = record.iter_start(run_id=run_id, iter=1)
    step_id = record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='GATE',
    )
    # NULL: a fresh step needs no approval, so the gate read passes
    assert record.step_approved(step_id=step_id)
    record.step_pending(step_id=step_id)
    # '': pending is the only state the gate read blocks on
    assert not record.step_approved(step_id=step_id)
    # the first approval wins the gate and stamps the instant
    assert record.step_approve(step_id=step_id) == 1
    assert record.step_approved(step_id=step_id)
    stamped = node.db.read('steps', where={'step_id': step_id})[0]['approved']
    assert stamped
    # a re-approve observes the loss and the original instant survives
    assert record.step_approve(step_id=step_id) == 0
    assert node.db.read('steps', where={'step_id': step_id})[0]['approved'] == stamped
    # a stray re-pend cannot demote the approval
    record.step_pending(step_id=step_id)
    assert node.db.read('steps', where={'step_id': step_id})[0]['approved'] == stamped


# ------ events


def test_event_lifecycle(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Event start, end, raw-string metadata, and actor attribution."""
    node = node_with_db
    node.record.run_start()

    # start event with metadata (a raw string -- e.g. a child branch)
    event_id = node.record.event_start('merge', metadata='main.x -> main')
    assert isinstance(event_id, int)

    # verify metadata is stored verbatim; outside any node context the
    # write attributes to the operator
    events = node.db.read('events', where={'event_id': event_id})
    assert events[0]['metadata'] == 'main.x -> main'
    assert events[0]['actor'] == 'operator'

    # end event -- events are point-in-time (no duration), just a final status
    node.record.event_end(event_id=event_id, status='completed', exit_code=0)
    events = node.db.read('events', where={'event_id': event_id})
    assert events[0]['status'] == 'completed'
    assert events[0]['exit_code'] == 0

    # inside a node context (the loop's own writes) the event
    # self-attributes to the calling node's branch
    monkeypatch.setenv('_NODE', f'{node.node_dir}')
    attributed = node.record.event_start('finish')
    events = node.db.read('events', where={'event_id': attributed})
    assert events[0]['actor'] == node.branch


def test_event_lineage_is_active_only(node_with_db: Node) -> None:
    """An event attaches a run only when one is active, never the most recent.

    ``event_start`` resolves lineage active-only: an out-of-band event (e.g. a
    parent-side ``spawn``/``delete`` on an idle node) carries NULL ``run_id``
    rather than inheriting a finished run, while an event fired mid-run carries
    the active run.
    """
    node = node_with_db
    record = node.record
    # idle node, no run -> NULL run lineage
    idle = record.event_start('spawn', metadata='main.x')
    assert node.db.read('events', where={'event_id': idle})[0]['run_id'] is None
    # a *finished* run is not inherited (active-only, no most-recent fallback)
    done = record.run_start()
    record.run_end(run_id=done, status='completed', exit_code=0)
    after = record.event_start('delete', metadata='main.x')
    assert node.db.read('events', where={'event_id': after})[0]['run_id'] is None
    # mid-run -> the event carries the active run
    live = record.run_start()
    during = record.event_start('finish')
    assert node.db.read('events', where={'event_id': during})[0]['run_id'] == live


def test_event_explicit_lineage_wins(node_with_db: Node) -> None:
    """Explicit lineage ids are written verbatim, skipping resolution.

    A caller that knows the event's run/iter/step (the loop's commit step)
    passes them; the active context -- even when one exists -- is not
    consulted. A run-only prefix stays partial (no per-field backfill), but a
    dangling child id -- a step without its iteration/run, an iteration
    without its run -- is rejected.
    """
    node = node_with_db
    record = node.record
    run_id = record.run_start()
    iter_id = record.iter_start(run_id=run_id, iter=1)
    step_id = record.step_start(iter_id=iter_id, run_id=run_id, step=1, step_name='A')
    # a second, explicit lineage distinct from the active context
    event_id = record.event_start('commit', metadata='sha', run_id=run_id)
    [row] = node.db.read('events', where={'event_id': event_id})
    assert (row['run_id'], row['iter_id'], row['step_id']) == (run_id, None, None)
    # the full triple lands verbatim
    event_id = record.event_start(
        'commit',
        metadata='sha',
        run_id=run_id,
        iter_id=iter_id,
        step_id=step_id,
    )
    [row] = node.db.read('events', where={'event_id': event_id})
    assert (row['run_id'], row['iter_id'], row['step_id']) == (
        run_id,
        iter_id,
        step_id,
    )
    # a broken chain is a caller bug
    with pytest.raises(ValueError, match='requires iter_id and run_id'):
        record.event_start('commit', step_id=step_id)
    with pytest.raises(ValueError, match='requires run_id'):
        record.event_start('commit', iter_id=iter_id)


# ------ signals


def test_signal_lifecycle(node_with_db: Node) -> None:
    """Signal set, get, append-only latest-wins, and per-signal withdrawal."""
    node = node_with_db
    run_id = node.record.run_start()

    # signal not set
    assert node.record.signal_get('finish') is None

    # set signal
    node.record.signal_set('finish', 'all done')
    result = node.record.signal_get('finish')
    assert result == 'all done'

    # signals are append-only (setting again adds another row)...
    node.record.signal_set('finish', 'really done')
    rows = node.db.read('signals', where={'signal': 'finish', 'run_id': run_id})
    assert len(rows) == 2
    # ...and the get reads the newest reason (latest-wins)
    assert node.record.signal_get('finish') == 'really done'

    # clearing withdraws one signal's rows and leaves the others intact
    # (the resume boot's pause withdrawal)
    node.record.signal_set('pause', 'brake')
    node.record.signal_clear('finish', run_id=run_id)
    assert node.record.signal_get('finish') is None
    assert node.record.signal_get('pause') == 'brake'


# ------ list readers


def test_list_readers_mirror_row_filters(node_with_db: Node) -> None:
    """The list readers scope to the node and honor the row filters.

    ``runs``/``iters``/``steps``/``events``/``signals`` wrap the row tables'
    where-dict shapes (newest first): lineage ids, type identifiers, and
    ``status`` narrow the listing without changing row content.
    """
    node = node_with_db
    record = node.record
    first = record.run_start()
    record.run_end(run_id=first, status='completed', exit_code=0)
    second = record.run_start()
    iter_id = record.iter_start(run_id=second, iter=1)
    step_id = record.step_start(iter_id=iter_id, run_id=second, step=1, step_name='A')
    record.step_end(step_id=step_id, status='completed', exit_code=0)
    event_id = record.event_start('finish', metadata='soon')
    record.signal_set('finish', 'soon')
    # runs: newest first; status narrows; limit caps
    assert [row['run_id'] for row in record.runs()] == [second, first]
    assert [row['run_id'] for row in record.runs(status='active')] == [second]
    assert [row['run_id'] for row in record.runs(limit=1)] == [second]
    # iters/steps: lineage filters scope to the run/iteration
    assert [row['iter_id'] for row in record.iters(run_id=second)] == [iter_id]
    assert record.iters(run_id=first) == []
    assert [row['step_id'] for row in record.steps(iter_id=iter_id)] == [step_id]
    assert record.steps(status='active') == []
    # events/signals: type and run filters
    assert [row['event_id'] for row in record.events(event='finish')] == [event_id]
    assert record.events(run_id=first) == []
    [signal] = record.signals(run_id=second, signal='finish')
    assert signal['metadata'] == 'soon'
    assert record.signals(run_id=first) == []


def test_activity_reconstructs_lifecycle(node_with_db: Node) -> None:
    """The ``activity`` reader unifies entity start/end rows with node events.

    Reconstructs "what happened when": every run/iteration/step contributes a
    start (``started_at``) and, once ended, an end (``ended_at``) row -- each
    carrying its run/iteration/step lineage -- alongside the point-in-time node
    events, the run bracketing everything below it.
    """
    node = node_with_db
    record = node.record
    run_id = record.run_start()
    iter_id = record.iter_start(run_id=run_id, iter=1)
    step_id = record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='EXECUTE',
    )
    record.step_end(step_id=step_id, status='completed', exit_code=0)
    record.iter_end(iter_id=iter_id, status='completed', exit_code=0)
    # a node-level event (point-in-time) lands on the activity feed too
    event_id = record.event_start('finish')
    record.event_end(event_id=event_id, status='completed')
    record.run_end(run_id=run_id, status='completed', exit_code=0)

    rows = record.activity()
    # each row's level is implied by which lineage ids are set
    runs = [r for r in rows if r['iter_id'] is None and r['event'] != 'finish']
    iters = [r for r in rows if r['iter_id'] is not None and r['step_id'] is None]
    steps = [r for r in rows if r['step_id'] is not None]
    assert {r['event'] for r in runs} == {'start', 'end'}
    assert {r['event'] for r in iters} == {'start', 'end'}
    assert {r['event'] for r in steps} == {'start', 'end'}
    # the node event keeps its own name and id, and carries the run lineage
    finishes = [r for r in rows if r['event'] == 'finish']
    assert len(finishes) == 1
    assert finishes[0]['event_id'] == event_id
    assert finishes[0]['run_id'] == run_id
    # entity rows carry no event_id -- only node events do
    run_rows = {r['event']: r for r in runs}
    assert run_rows['start']['event_id'] is None
    # the run brackets the whole lifecycle: its start is first, its end last
    stamps = [r['timestamp'] for r in rows]
    assert run_rows['start']['timestamp'] == min(stamps)
    assert run_rows['end']['timestamp'] == max(stamps)


def test_activity_end_rows_carry_duration_and_cost(node_with_db: Node) -> None:
    """End rows expose elapsed ``duration`` (seconds) and rolled-up ``cost``.

    The view derives ``duration`` from ``ended_at - started_at`` and surfaces
    ``cost`` -- a step's own, and the SUM over its steps for the iteration and
    run -- but only on the end rows; start rows and point-in-time events carry
    neither.
    """
    node = node_with_db
    record = node.record
    run_id = record.run_start()
    iter_id = record.iter_start(run_id=run_id, iter=1)
    step_id = record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='EXECUTE',
    )
    record.step_cost(step_id=step_id, cost=0.25)
    record.step_end(step_id=step_id, status='completed', exit_code=0)
    record.iter_end(iter_id=iter_id, status='completed', exit_code=0)
    record.run_end(run_id=run_id, status='completed', exit_code=0)
    # pin a deterministic 90s span on each entity (the *_end calls stamped
    # ended_at=now; overwrite both ends so duration is exact, not wall-clock)
    started, ended = '2026-03-27T14:00:00.000Z', '2026-03-27T14:01:30.000Z'
    for table, column, row_id in (
        ('runs', 'run_id', run_id),
        ('iters', 'iter_id', iter_id),
        ('steps', 'step_id', step_id),
    ):
        node.db.update(
            data={'started_at': started, 'ended_at': ended},
            table=table,
            where={column: row_id},
        )

    rows = record.activity()
    ends = [r for r in rows if r['event'] == 'end']
    run_end = next(r for r in ends if r['iter_id'] is None)
    iter_end = next(r for r in ends if r['iter_id'] and r['step_id'] is None)
    step_end = next(r for r in ends if r['step_id'] is not None)
    # duration is the 90s span at every level
    assert run_end['duration'] == 90.0
    assert iter_end['duration'] == 90.0
    assert step_end['duration'] == 90.0
    # cost: the step's own, rolled up to the iteration and the run
    assert step_end['cost'] == 0.25
    assert iter_end['cost'] == 0.25
    assert run_end['cost'] == 0.25
    # start rows (and point-in-time events) carry neither
    others = [r for r in rows if r['event'] != 'end']
    assert all(r['duration'] is None and r['cost'] is None for r in others)


# ------ sessions


def test_session_transcript_reads_claude_and_codex(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transcripts resolve per agent, from each agent's real home, gated.

    Claude keys transcripts by the worktree slug under the user's config
    home; codex rollouts date-nest under the node's own codex home. A
    transcript found off the deterministic claude path serves only session
    ids recorded for this node.
    """
    node = node_with_db
    # claude: the projects dir keys by the worktree slug under the config home
    home = tmp_path / 'claude-home'
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', f'{home}')
    slug = re.sub(r'[^A-Za-z0-9]', '-', f'{node.worktree}')
    session = str(uuid.uuid4())
    transcript = home / 'projects' / slug / f'{session}.jsonl'
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"type": "turn"}\n', encoding='utf-8')
    found = node.sessions.transcript('claude', session)
    assert found == {
        'agent': 'claude',
        'session': session,
        'path': f'{transcript}',
        'exists': True,
        'content': '{"type": "turn"}\n',
    }
    # an absent id still returns the deterministic path, so a caller can
    # poll for the file to appear
    absent = str(uuid.uuid4())
    missing = node.sessions.transcript('claude', absent)
    assert missing['exists'] is False
    slug_dir = home / 'projects' / slug
    assert missing['path'] == f'{slug_dir}/{absent}.jsonl'
    # a transcript under a foreign slug serves only ids recorded for this
    # node -- an ungated lookup would expose any session of the OS user
    stray = str(uuid.uuid4())
    foreign = home / 'projects' / 'some-other-project' / f'{stray}.jsonl'
    foreign.parent.mkdir(parents=True)
    foreign.write_text('{"type": "relocated"}\n', encoding='utf-8')
    assert node.sessions.transcript('claude', stray)['exists'] is False
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    step_id = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.record.step_session('claude', step_id=step_id, model=None, session=stray)
    relocated = node.sessions.transcript('claude', stray)
    assert relocated['exists'] is True
    assert relocated['content'] == '{"type": "relocated"}\n'
    # codex: rollouts date-nest under the node's own codex home
    rollout = str(uuid.uuid4())
    rollout_file = (
        node.node_dir
        / '.codex'
        / 'sessions'
        / '2026'
        / '07'
        / '11'
        / f'rollout-2026-07-11T10-00-00-{rollout}.jsonl'
    )
    rollout_file.parent.mkdir(parents=True)
    rollout_file.write_text('{"kind": "rollout"}\n', encoding='utf-8')
    codex = node.sessions.transcript('codex', rollout)
    assert codex['exists'] is True
    assert codex['path'] == f'{rollout_file}'
    assert codex['content'] == '{"kind": "rollout"}\n'
    assert node.sessions.transcript('codex', str(uuid.uuid4()))['path'] is None
    # path-escaping ids and unknown agents are rejected at the boundary
    with pytest.raises(ValueError):
        node.sessions.transcript('claude', '../escape')
    with pytest.raises(ValueError):
        node.sessions.transcript('gemini', session)


def test_iteration_records_the_served_model_over_the_pin(
    node_with_db: Node,
) -> None:
    """Divergence beats the seeded pin on the iteration row.

    The row seeds from config at ``iter_start`` -- the model asked for --
    so leaving it there would launder a whole iteration served off-pin
    under the pin's name, the attribution failure the served-model work
    exists to end. When every step agrees, the served model wins; when
    the steps disagree (or report nothing), the seed stands rather than
    inventing a consensus.
    """
    node = node_with_db
    node.config.set('model', 'pinned-model')
    run_id = node.record.run_start()

    def _iteration(number: int, *served: str) -> dict:
        iter_id = node.record.iter_start(run_id=run_id, iter=number)
        for index, model in enumerate(served, start=1):
            step_id = node.record.step_start(
                iter_id=iter_id,
                run_id=run_id,
                step=index,
                step_name='EXECUTE',
            )
            node.db.update({'model': model}, 'steps', where={'step_id': step_id})
            node.record.step_end(step_id=step_id, status='completed', exit_code=0)
        node.record.iter_end(iter_id=iter_id, status='completed', exit_code=0)
        return node.db.read('iters', where={'iter_id': iter_id})[0]

    # every step served the same off-pin model: the row records what served
    assert _iteration(1, 'served-model', 'served-model')['model'] == 'served-model'
    # every step served the pin: the row keeps it
    assert _iteration(2, 'pinned-model')['model'] == 'pinned-model'
    # the steps disagree -- no consensus to record, so the pin stands
    assert _iteration(3, 'served-model', 'other-model')['model'] == 'pinned-model'
    # no step reported a model at all: the seed stands
    assert _iteration(4)['model'] == 'pinned-model'
