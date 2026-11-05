"""Test the ``fractal.impl.muse`` module.

The Muse (``muse``) dialect end to end: the ``muse exec --json`` JSONL
protocol parsed into normalized events -- the session in the ``stream``
envelope, the served model on ``run.model.configured``, text on
``run.output.delta``, tool names on ``tool.result``, token totals (and no
USD figure) on ``run.usage``, and the authoritative ``run.terminal`` close --
the ``exec --json --yolo`` argv builder with the caller-minted session, and
the date-sharded transcript glob under muse's data home. Stream-level cases
drive the base ``Agent.stream`` driver against a real node ledger.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Optional

import pytest

from fractal.core import agent
from fractal.core.node import Node
from fractal.impl.muse import MuseAgent, MuseParser

__all__ = [
    'test_capability_flags_report_provider_facts_muse',
    'test_registry_resolves_muse',
    'test_parser_maps_the_stream_protocol_muse',
    'test_parser_captures_the_session_from_the_envelope_once',
    'test_parser_usage_records_tokens_and_no_cost',
    'test_parser_ignores_benign_task_failures',
    'test_parser_surfaces_a_failed_terminal',
    'test_parser_treats_a_nonstring_tool_name_as_noise',
    'test_parser_ignores_a_duplicate_terminal',
    'test_parser_tolerates_garbage_muse',
    'test_stream_records_session_and_model_without_cost_muse',
    'test_stream_fails_on_a_failed_terminal_muse',
    'test_invocation_modes_build_the_pinned_argv_muse',
    'test_invocation_fork_refuses_muse',
    'test_preflight_refuses_a_cost_capped_run_muse',
    'test_seed_links_global_auth_into_the_node_home',
    'test_transcript_globs_the_session_log',
]

# muse sessions are caller-stamped uuids
_SESSION = '60741b6f-1a93-4c02-8a84-85c75afcae5d'
_MODEL = 'muse-wonder-1.0'


def _frame(
    payload_type: str,
    payload: dict[str, Any],
    *,
    session: Optional[str] = _SESSION,
) -> dict[str, Any]:
    """One muse envelope frame around a payload."""
    return {
        'payload_type': payload_type,
        'stream': {'kind': 'session', 'id': session},
        'payload': payload,
    }


def _terminal(
    terminal: str = 'completed', reason: Optional[str] = None
) -> dict[str, Any]:
    """The run.terminal close frame (the suffix follows the outcome)."""
    return _frame(
        f'run.terminal.{terminal}',
        {'kind': 'run_terminal', 'terminal': terminal, 'reason': reason, 'text': 'ok'},
    )


def _usage(tokens: dict[str, int]) -> dict[str, Any]:
    """A run.usage frame carrying per-model token totals."""
    return _frame(
        'run.usage',
        {
            'kind': 'run_usage',
            'models': [{'model': _MODEL, **tokens}],
        },
    )


def test_capability_flags_report_provider_facts_muse(node_with_db: Node) -> None:
    """The provider facts consumers branch on, plus cost untrackability."""
    backend = MuseAgent(node_with_db, 'muse')
    assert backend.name == 'muse'
    assert backend.config_file == ''
    assert not backend.can_fork
    assert not backend.mints_session
    assert not backend.needs_pricing
    assert not backend.wants_pricing
    assert backend.cost_scope == 'call'
    assert not backend.enforces_budget
    # muse reports tokens against a subscription allowance, never a dollar
    # figure, so no spend is recordable with or without a model
    assert not backend.tracks_cost()
    assert not backend.tracks_cost(_MODEL)


def test_registry_resolves_muse() -> None:
    """The shipped registry resolves the muse base command."""
    assert agent.resolve('muse') is MuseAgent
    assert 'muse' in agent.supported()


def test_parser_maps_the_stream_protocol_muse() -> None:
    """One protocol implementation: session, model, text, tool, usage, close."""
    parser = MuseParser()
    frames = [
        _frame('runtime.command.accepted', {'kind': 'command_accepted'}),
        _frame(
            'run.model.configured', {'kind': 'run_model_configured', 'model_id': _MODEL}
        ),
        _frame('run.output.delta', {'kind': 'run_output_delta', 'text': 'probe-'}),
        _frame('run.output.delta', {'kind': 'run_output_delta', 'text': 'ok'}),
        _frame(
            'tool.result',
            {'kind': 'tool_result', 'correlation_facts': {'tool_name': 'bash'}},
        ),
        _usage(
            {
                'input_tokens': 10,
                'output_tokens': 6,
                'reasoning_tokens': 1,
                'cached_tokens': 4,
            }
        ),
        _terminal(),
    ]
    events = [event for line in _lines(frames) for event in parser.feed(line)]
    # the session is captured off the first frame's envelope; the model
    # frame re-stamps it with the served model once is known
    assert [event.kind for event in events] == [
        'session',
        'session',
        'text',
        'text',
        'tool',
        'result',
    ]
    session, restamp, text_a, text_b, tool, result = events
    assert session.session == _SESSION
    assert session.model is None
    assert restamp.session == _SESSION
    assert restamp.model == _MODEL
    assert (text_a.text, text_b.text) == ('probe-', 'ok')
    assert tool.tool == 'bash'
    # the terminal close is authoritative and carries no USD figure
    assert result.final
    assert result.cost is None
    assert result.duration is not None
    assert result.duration >= 0.0
    assert parser.model == _MODEL
    assert parser.models == [_MODEL]
    assert parser.cost is None


def test_parser_captures_the_session_from_the_envelope_once() -> None:
    """Every frame names the session; only the first emits the fact."""
    parser = MuseParser(model=_MODEL)
    frames = [
        _frame('run.output.delta', {'kind': 'run_output_delta', 'text': 'hi'}),
        _frame('run.output.delta', {'kind': 'run_output_delta', 'text': 'yo'}),
    ]
    events = [event for line in _lines(frames) for event in parser.feed(line)]
    # the configured model already matches, so no re-stamp fires
    assert [event.kind for event in events] == ['session', 'text', 'text']
    assert parser.session == _SESSION


def test_parser_usage_records_tokens_and_no_cost() -> None:
    """Token totals land on the parser; no cost is ever invented."""
    parser = MuseParser()
    frames = [
        _usage(
            {
                'input_tokens': 10,
                'output_tokens': 6,
                'reasoning_tokens': 1,
                'cached_tokens': 4,
            }
        ),
        # run.usage totals are cumulative per invocation: the latest frame
        # replaces the earlier one
        _usage(
            {
                'input_tokens': 25,
                'output_tokens': 9,
                'reasoning_tokens': 2,
                'cached_tokens': 8,
            }
        ),
        _terminal(),
    ]
    events = [event for line in _lines(frames) for event in parser.feed(line)]
    assert [event.kind for event in events] == ['session', 'result']
    assert parser.tokens == {
        _MODEL: {
            'input_tokens': 25,
            'output_tokens': 9,
            'reasoning_tokens': 2,
            'cached_tokens': 8,
        },
    }
    assert parser.models == [_MODEL]
    assert parser.cost is None


def test_parser_ignores_benign_task_failures() -> None:
    """Internal sub-task failures do not fail the step.

    muse runs reminder and judge sub-tasks beside the model call, and
    their ``task.lifecycle.failed`` frames ride healthy runs; only the
    run's terminal frame speaks for the invocation.
    """
    parser = MuseParser()
    frames = [
        _frame(
            'task.lifecycle.failed',
            {
                'kind': 'task_lifecycle',
                'event': {'kind': 'failed', 'reason': 'reminder unsupported'},
            },
        ),
        _frame('run.output.delta', {'kind': 'run_output_delta', 'text': 'ok'}),
        _terminal(),
    ]
    events = [event for line in _lines(frames) for event in parser.feed(line)]
    assert [event.kind for event in events] == ['session', 'text', 'result']
    assert parser.errors == []


def test_parser_surfaces_a_failed_terminal() -> None:
    """A non-completed terminal collects an error and fails the close."""
    parser = MuseParser()
    events = parser.feed(json.dumps(_terminal('failed', 'model unavailable')))
    assert [event.kind for event in events] == ['session', 'error', 'result']
    assert parser.errors == ['model unavailable']
    assert events[-1].failed
    assert events[-1].final


def test_parser_treats_a_nonstring_tool_name_as_noise() -> None:
    """A non-string ``tool_name`` maps to ``?``, never crashes the stream."""
    parser = MuseParser()
    frame = _frame(
        'tool.result',
        {'kind': 'tool_result', 'correlation_facts': {'tool_name': 123}},
    )
    events = parser.feed(json.dumps(frame))
    tool = next(event for event in events if event.kind == 'tool')
    assert tool.tool == '?'


def test_parser_ignores_a_duplicate_terminal() -> None:
    """One terminal closes the invocation; a second emits nothing."""
    parser = MuseParser()
    first = parser.feed(json.dumps(_terminal()))
    second = parser.feed(json.dumps(_terminal('failed', 'late failure')))
    assert first[-1].kind == 'result'
    assert second == []
    assert parser.errors == []


def test_parser_tolerates_garbage_muse() -> None:
    """Malformed, non-object, and unknown lines yield nothing, never raise."""
    parser = MuseParser()
    junk = [
        '',
        '   ',
        'not json',
        '[1, 2]',
        '"text"',
        '{}',
        '{"payload_type": "mystery", "payload": {}}',
        # a frame without a payload object carries nothing
        '{"payload_type": "run.output.delta"}',
        # a non-string payload_type must not crash the terminal prefix test
        '{"payload_type": 5, "payload": {}}',
    ]
    assert [event for line in junk for event in parser.feed(line)] == []
    assert parser.session is None
    assert parser.cost is None


def test_stream_records_session_and_model_without_cost_muse(node_with_db: Node) -> None:
    """The stream driver lands session and model on the ledger, cost NULL."""
    node = node_with_db
    backend = MuseAgent(node, 'muse')
    (step_id,) = _steps(node, 1)
    frames = [
        _frame(
            'run.model.configured', {'kind': 'run_model_configured', 'model_id': _MODEL}
        ),
        _frame('run.output.delta', {'kind': 'run_output_delta', 'text': 'ok'}),
        _usage(
            {
                'input_tokens': 10,
                'output_tokens': 6,
                'reasoning_tokens': 1,
                'cached_tokens': 4,
            }
        ),
        _terminal(),
    ]
    result = backend.stream(_lines(frames), step_id=step_id)
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['agent'] == 'muse'
    assert row['session'] == _SESSION
    assert row['model'] == _MODEL
    assert row['cost'] is None
    assert node.sessions.get('muse') == _SESSION
    assert result.session == _SESSION
    assert result.cost is None


def test_stream_fails_on_a_failed_terminal_muse(node_with_db: Node) -> None:
    """A failed terminal fails the step even after a fully drained stdout."""
    backend = MuseAgent(node_with_db, 'muse')
    frames = [_terminal('failed', 'model unavailable')]
    with pytest.raises(RuntimeError, match='muse reported an error: model unavailable'):
        backend.stream(_lines(frames))


def test_invocation_modes_build_the_pinned_argv_muse(node_with_db: Node) -> None:
    """Fresh/resume/model/effort land their exact argv."""
    node = node_with_db
    backend = MuseAgent(node, 'muse')
    head = ('muse', 'exec', '--json', '--yolo', '--workspace', str(node.worktree))
    # fresh: fractal mints the id, muse adopts it via --session-id
    fresh = backend.invocation('hi')
    assert len(fresh.argv) == len(head) + 4
    assert fresh.argv[: len(head)] == head
    assert fresh.argv[-4] == '--session-id'
    fresh_id = fresh.argv[-3]
    assert fresh.argv[-2:] == ('--', 'hi')
    assert fresh.session == fresh_id
    assert fresh.mode == 'fresh'
    # resume re-enters the same session with the very same flag
    resume = backend.invocation('hi', session=fresh_id)
    assert resume.argv == (*head, '--session-id', fresh_id, '--', 'hi')
    assert resume.mode == 'resume'
    # the model rides --model, effort rides --reasoning-effort
    tuned = backend.invocation('hi', model=_MODEL, effort='xhigh')
    assert tuned.argv[: len(head)] == head
    tail = tuned.argv[len(head) :]
    assert tail[:4] == ('--model', _MODEL, '--reasoning-effort', 'xhigh')
    assert tail[-2:] == ('--', 'hi')
    # a dash-leading message is protected by the sentinel, not parsed as a flag
    dashed = backend.invocation('-1 on that idea')
    assert dashed.argv[-2:] == ('--', '-1 on that idea')
    # muse runs in the worktree with its XDG homes relocated to the
    # node's .muse dir (config, state, and sessions stay per-node)
    assert fresh.cwd == node.worktree
    assert fresh.env is not None
    assert fresh.env['XDG_CONFIG_HOME'] == str(node.node_dir / '.muse')
    assert fresh.env['XDG_DATA_HOME'] == str(node.node_dir / '.muse')


def test_invocation_fork_refuses_muse(node_with_db: Node) -> None:
    """``muse exec`` has no fork; the ask raises naming the resume path."""
    backend = MuseAgent(node_with_db, 'muse')
    with pytest.raises(NotImplementedError, match='cannot fork'):
        backend.invocation('hi', session=_SESSION, fork=True)


def test_preflight_refuses_a_cost_capped_run_muse(node_with_db: Node) -> None:
    """A cost cap on a muse node is refused at preflight, remedy named."""
    node_with_db.config.set('max_cost', 5.0)
    # `sh` stands in for the muse binary so the PATH check passes anywhere
    backend = MuseAgent(node_with_db, 'sh')
    with pytest.raises(RuntimeError, match='remove max_cost'):
        backend.preflight()
    # uncapped, the same preflight passes (binary presence only)
    node_with_db.config.set('max_cost', None)
    assert backend.preflight() is None


def test_seed_links_global_auth_into_the_node_home(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seeding creates ``.muse`` and links (never copies) the auth file."""
    # a fake user home carrying the global muse credentials
    home = tmp_path / 'home'
    global_auth = home / '.config' / 'muse' / 'auth.json'
    global_auth.parent.mkdir(parents=True)
    global_auth.write_text('{}', encoding='utf-8')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    node_dir = tmp_path / 'node'
    (node_dir / 'skills').mkdir(parents=True)
    MuseAgent.seed(node_dir)
    agent_dir = node_dir / '.muse'
    assert agent_dir.is_dir()
    assert (agent_dir / 'skills').is_symlink()
    link = agent_dir / 'muse' / 'auth.json'
    assert link.is_symlink()
    assert link.resolve() == global_auth


def test_transcript_globs_the_session_log(node_with_db: Node) -> None:
    """The session log resolves under the node's own muse data home."""
    backend = MuseAgent(node_with_db, 'muse')
    sessions = (
        node_with_db.node_dir / '.muse' / 'muse' / 'sessions' / '2026' / '11' / '03'
    )
    session_dir = sessions / _SESSION
    session_dir.mkdir(parents=True)
    log = session_dir / 'session.jsonl'
    log.write_text('{"schema_version": 1}\n', encoding='utf-8')
    transcript = backend.transcript(_SESSION)
    assert transcript['exists'] is True
    assert transcript['path'] == str(log)


def test_transcript_reports_a_missing_session(node_with_db: Node) -> None:
    """No log yet: the transcript degrades to ``exists: False``."""
    backend = MuseAgent(node_with_db, 'muse')
    transcript = backend.transcript(_SESSION)
    assert transcript['exists'] is False
    assert transcript['content'] == ''


# ------ helpers


def _lines(frames: list[dict[str, Any]]) -> list[str]:
    """Encode provider frames as agent stdout lines."""
    return [json.dumps(frame) + '\n' for frame in frames]


def _steps(node: Node, count: int) -> list[int]:
    """Create a run/iteration chain carrying ``count`` step rows."""
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    return [
        node.record.step_start(
            iter_id=iter_id,
            run_id=run_id,
            step=step,
            step_name='EXECUTE',
        )
        for step in range(1, count + 1)
    ]
