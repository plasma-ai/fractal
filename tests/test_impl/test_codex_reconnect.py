"""Transient codex error notifications recover after a clean exit.

Codex writes ``{"type": "error", "message": ...}`` frames for reconnects and
retries and then completes the turn. When every recorded error is such a
notification inside the active turn, the stream describes one complete turn
with sound wire usage, and the process exits zero, the notifications are not a
failure: the step is priced and closes with a successful terminal frame. Any
other error frame, an incomplete turn, unsound usage or a non-zero exit keeps
the step failed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from fractal.exceptions import AgentStreamError
from fractal.impl.codex import CodexParser

from .rollouts import resumed_thread
from .test_codex import (
    _FRESH_COST,
    _FRESH_WIRE,
    _MODEL,
    _command,
    _drive,
    _lines,
    _named_step,
    _split,
    backend,
)

__all__ = [
    'backend',
    'test_notifications_inside_the_active_turn_recover',
    'test_fatal_or_malformed_frames_stay_fatal',
    'test_notifications_outside_the_active_turn_stay_fatal',
    'test_recovery_needs_a_complete_turn',
    'test_recovery_needs_sound_usage',
    'test_notifications_mixed_with_a_fatal_frame_stay_fatal',
    'test_bare_stream_keeps_notifications_fatal',
    'test_stream_recovers_notifications_and_prices_the_step',
    'test_recovered_stream_without_rollout_evidence_stays_unpriced',
    'test_notification_with_a_nonzero_exit_stays_a_failed_step',
    'test_fatal_frame_with_a_clean_exit_stays_a_failed_step',
]

# the fresh run's wire: thread.started, turn.started, item.completed,
# turn.completed
_OPEN, _TURN, _TEXT, _DONE = _FRESH_WIRE


def _canonical(message: str = 'Reconnecting... 1/5') -> dict[str, Any]:
    """Return the canonical notification frame codex writes on a reconnect."""
    return {'type': 'error', 'message': message}


def _parse(frames: list[dict[str, Any]]) -> CodexParser:
    """Feed ``frames`` to a fresh parser and return it."""
    parser = CodexParser(model=_MODEL)
    for frame in frames:
        parser.feed(json.dumps(frame))
    return parser


@pytest.mark.parametrize('message', ['Reconnecting... 1/5', 'quota exhausted', 'retry'])
def test_notifications_inside_the_active_turn_recover(message: str) -> None:
    """Notifications between turn.started and turn.completed clear on recovery."""
    parser = _parse(
        [_OPEN, _TURN, _canonical(message), _canonical(message), _TEXT, _DONE]
    )
    assert parser.errors == [message, message]
    parser._recover_errors()
    assert parser.errors == []
    (result,) = parser.finish()
    assert result.failed is False
    assert result.message is None


@pytest.mark.parametrize(
    argnames='frame',
    argvalues=[
        {'type': 'turn.failed', 'error': {'message': 'boom'}},
        {'type': 'error', 'message': 'boom', 'code': 7},
        {'type': 'error', 'message': ''},
        {'type': 'error', 'message': None},
    ],
    ids=['turn-failed', 'extra-field', 'empty-message', 'null-message'],
)
def test_fatal_or_malformed_frames_stay_fatal(frame: dict[str, Any]) -> None:
    """Only the canonical two-key notification with text is provisional."""
    parser = _parse([_OPEN, _TURN, frame, _TEXT, _DONE])
    parser._recover_errors()
    assert parser.errors
    (result,) = parser.finish()
    assert result.failed is True
    assert result.message


def test_notifications_outside_the_active_turn_stay_fatal() -> None:
    """A notification before the turn starts or after it completes is not provisional."""
    before = _parse([_OPEN, _canonical(), _TURN, _TEXT, _DONE])
    after = _parse([_OPEN, _TURN, _TEXT, _DONE, _canonical()])
    for parser in (before, after):
        parser._recover_errors()
        assert parser.errors


def test_recovery_needs_a_complete_turn() -> None:
    """A notification inside a turn that never completes stays fatal."""
    parser = _parse([_OPEN, _TURN, _canonical(), _TEXT])
    parser._recover_errors()
    assert parser.errors


@pytest.mark.parametrize(
    argnames='usage',
    argvalues=[
        None,
        {'input_tokens': -1, 'output_tokens': 1},
        {'input_tokens': '1', 'output_tokens': 1},
        {'input_tokens': 1, 'output_tokens': True},
        {'input_tokens': 1, 'output_tokens': 1, 'reasoning_output_tokens': 5},
    ],
    ids=['missing', 'negative', 'string', 'bool', 'reasoning-exceeds-output'],
)
def test_recovery_needs_sound_usage(usage: Any) -> None:
    """Unsound wire usage on the completed turn leaves the notifications fatal."""
    parser = _parse(
        [_OPEN, _TURN, _canonical(), _TEXT, {'type': 'turn.completed', 'usage': usage}]
    )
    parser._recover_errors()
    assert parser.errors


def test_notifications_mixed_with_a_fatal_frame_stay_fatal() -> None:
    """One fatal frame among notifications keeps every recorded error."""
    parser = _parse(
        [
            _OPEN,
            _TURN,
            _canonical(),
            {'type': 'turn.failed', 'error': {'message': 'boom'}},
            _TEXT,
            _DONE,
        ]
    )
    parser._recover_errors()
    assert parser.errors == ['Reconnecting... 1/5', 'boom']


def test_bare_stream_keeps_notifications_fatal(backend: Any) -> None:
    """Without a process there is no exit to observe, so nothing recovers."""
    wire = [_OPEN, _TURN, _canonical(), _TEXT, _DONE]
    with pytest.raises(AgentStreamError, match='Reconnecting'):
        backend.stream(_lines(wire))


def test_stream_recovers_notifications_and_prices_the_step(
    backend: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A clean exit after notifications prices the step from its rollout records."""
    fresh, _ = _split(resumed_thread)
    wire = [_OPEN, _TURN, _canonical(), _canonical('Reconnecting... 2/5'), _TEXT, _DONE]
    step = _named_step(backend, 'RECOVERED')
    result, events = _drive(backend, _command(backend, fresh, wire), step_id=step)
    assert result.cost == pytest.approx(_FRESH_COST)
    row = backend.node.db.read('steps', where={'step_id': step})[0]
    assert row['cost'] == pytest.approx(_FRESH_COST)
    # the notifications were rendered as they arrived; the terminal frame
    # reports success
    assert [event.message for event in events if event.kind == 'error'] == [
        'Reconnecting... 1/5',
        'Reconnecting... 2/5',
    ]
    (terminal,) = [event for event in events if event.kind == 'result']
    assert terminal.failed is False
    assert terminal.message is None
    assert 'unpriced' not in caplog.text


def test_recovered_stream_without_rollout_evidence_stays_unpriced(
    backend: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Recovery settles the outcome only; missing evidence still leaves cost NULL."""
    fresh, _ = _split(resumed_thread)
    wire = [_OPEN, _TURN, _canonical(), _TEXT, _DONE]
    step = _named_step(backend, 'RECOVERED_UNPRICED')
    command = _command(backend, fresh, wire, missing=True)
    result, events = _drive(backend, command, step_id=step)
    assert result.cost is None
    assert backend.node.db.read('steps', where={'step_id': step})[0]['cost'] is None
    (terminal,) = [event for event in events if event.kind == 'result']
    assert terminal.failed is False
    assert 'codex usage unpriced: No rollout names the thread' in caplog.text


def test_notification_with_a_nonzero_exit_stays_a_failed_step(backend: Any) -> None:
    """Recovery needs exit zero: a failed process keeps the notification fatal."""
    fresh, _ = _split(resumed_thread)
    wire = [_OPEN, _TURN, _canonical(), _TEXT, _DONE]
    step = _named_step(backend, 'FAILED_EXIT')
    with pytest.raises(AgentStreamError, match='Reconnecting'):
        _drive(backend, _command(backend, fresh, wire, exit_code=3), step_id=step)
    assert backend.node.db.read('steps', where={'step_id': step})[0]['cost'] is None


def test_fatal_frame_with_a_clean_exit_stays_a_failed_step(
    backend: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A terminal error frame fails the step even when codex exits zero."""
    fresh, _ = _split(resumed_thread)
    wire = [
        _OPEN,
        _TURN,
        {'type': 'turn.failed', 'error': {'message': 'boom'}},
        _TEXT,
        _DONE,
    ]
    step = _named_step(backend, 'FATAL_FRAME')
    with pytest.raises(AgentStreamError, match='boom'):
        _drive(backend, _command(backend, fresh, wire), step_id=step)
    assert backend.node.db.read('steps', where={'step_id': step})[0]['cost'] is None
    assert 'codex usage unpriced: stdout reported an error' in caplog.text
