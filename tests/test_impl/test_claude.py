"""Test the ``fractal.impl.claude`` module.

The claude dialect end to end: the stream-json protocol parsed into
normalized events, the ``-p`` argv builder, per-call pricing over the
disjoint Anthropic usage buckets, the settings-chain model default, and
the config-home transcript layout. Stream-level cases drive the base
``Agent.stream`` driver against a real node ledger.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import uuid
from typing import Any, Optional

import pytest

from fractal.cli.utils import StreamRenderer
from fractal.core import pricing
from fractal.core.event import Event
from fractal.core.node import Node
from fractal.impl import claude
from fractal.impl.claude import ClaudeAgent, ClaudeParser, _RoutedClaudeParser

__all__ = [
    'test_capability_flags_report_provider_facts_claude',
    'test_parser_maps_the_stream_protocol_claude',
    'test_parser_captures_the_session_once_and_prefers_the_stream_model',
    'test_parser_restamps_the_served_model_from_assistant_rows',
    'test_parser_error_result',
    'test_parser_null_duration',
    'test_parser_marks_a_budget_stop',
    'test_parser_tolerates_garbage_claude',
    'test_parser_tolerates_present_null_payloads_claude',
    'test_parser_ignores_a_non_numeric_result_cost',
    'test_events_render_through_the_production_renderer_claude',
    'test_renderer_closes_a_truncated_stream_on_a_fresh_line',
    'test_parser_flushes_cost_per_assistant_event',
    'test_parser_prices_a_multi_block_message_once',
    'test_parser_result_overwrites_the_accumulated_estimate',
    'test_parser_unpriced_model_accumulates_no_cost',
    'test_compute_cost_prices_disjoint_buckets',
    'test_compute_cost_unpriced_model_returns_none_claude',
    'test_stream_records_cost_model_and_session_claude',
    'test_stream_detached_keeps_session_unpersisted_claude',
    'test_stream_records_full_per_invocation_cost',
    'test_stream_truncated_records_accumulated_cost',
    'test_stream_survives_missing_pricing_cache',
    'test_invocation_modes_build_the_pinned_argv_claude',
    'test_invocation_honors_command_model_budget_and_settings',
    'test_routed_invocation_redirects_the_anthropic_seam',
    'test_native_invocation_scrubs_inherited_routing_keys',
    'test_routed_parser_prices_the_result_usage_through_the_chain',
    'test_routed_parser_sums_the_result_model_usage_per_model',
    'test_routed_parser_tolerates_gateway_null_usage',
    'test_routed_tracking_and_preflight_gate_on_the_route',
    'test_rates_falls_back_through_the_openrouter_chain_claude',
    'test_config_model_walks_the_settings_chain',
    'test_seed_config_disables_fast_mode_claude',
    'test_transcript_resolves_the_config_home_slug',
]

# claude pricing with all four Anthropic bucket rates distinct, so a bucket
# priced at the wrong rate (or dropped) breaks the expected figure
_PRICING = {
    'claude-fable-5': {
        'input_cost_per_token': 3e-6,
        'output_cost_per_token': 1.5e-5,
        'cache_read_input_token_cost': 3e-7,
        'cache_creation_input_token_cost': 3.75e-6,
    },
}

# per-call usage fixtures (Anthropic convention: buckets are disjoint;
# input_tokens EXCLUDES the cache buckets) and their hand-computed costs
_USAGE_FIRST = {
    'input_tokens': 100,
    'cache_creation_input_tokens': 1000,
    'cache_read_input_tokens': 10_000,
    'output_tokens': 200,
}
_USAGE_FIRST_COST = 100 * 3e-6 + 1000 * 3.75e-6 + 10_000 * 3e-7 + 200 * 1.5e-5
_USAGE_SECOND = {
    'input_tokens': 50,
    'output_tokens': 100,
}
_USAGE_SECOND_COST = 50 * 3e-6 + 100 * 1.5e-5


def test_capability_flags_report_provider_facts_claude(node_with_db: Node) -> None:
    """The provider facts consumers branch on, plus cost trackability."""
    backend = ClaudeAgent(node_with_db, 'claude')
    assert backend.name == 'claude'
    assert backend.config_file == 'settings.json'
    assert backend.can_fork
    assert not backend.mints_session
    assert not backend.needs_pricing
    assert backend.cost_scope == 'call'
    assert backend.enforces_budget
    # a cost-reporting agent tracks spend with or without a priced model
    assert backend.tracks_cost()
    assert backend.tracks_cost('mystery')


def test_parser_maps_the_stream_protocol_claude() -> None:
    """One protocol implementation: session, tools, deltas, results."""
    parser = ClaudeParser()
    frames = [
        {'type': 'system', 'subtype': 'init', 'session_id': 'sess-1'},
        {
            'type': 'stream_event',
            'session_id': 'sess-1',
            'event': {
                'type': 'content_block_start',
                'content_block': {'type': 'tool_use', 'name': 'Bash'},
            },
        },
        {
            'type': 'stream_event',
            'session_id': 'sess-1',
            'event': {
                'type': 'content_block_start',
                'content_block': {'type': 'text'},
            },
        },
        {
            'type': 'stream_event',
            'session_id': 'sess-1',
            'event': {
                'type': 'content_block_delta',
                'delta': {'type': 'text_delta', 'text': 'Hel'},
            },
        },
        {
            'type': 'stream_event',
            'session_id': 'sess-1',
            'event': {
                'type': 'content_block_delta',
                'delta': {'type': 'text_delta', 'text': 'lo'},
            },
        },
        {
            'type': 'user',
            'message': {
                'content': [
                    {
                        'type': 'tool_result',
                        'content': 'file contents here',
                        'is_error': False,
                    },
                ],
            },
        },
        {
            'type': 'result',
            'subtype': 'success',
            'num_turns': 2,
            'duration_ms': 1500,
            'total_cost_usd': 0.0432,
        },
    ]
    events = [event for line in _lines(frames) for event in parser.feed(line)]
    assert [event.kind for event in events] == [
        'session',
        'tool',
        'text',
        'text',
        'text',
        'tool_result',
        'result',
    ]
    session, tool, opened, *deltas, tool_result, result = events
    assert session.session == 'sess-1'
    assert tool.tool == 'Bash'
    # a text block opens on a fresh line, then streams raw deltas
    assert opened.text == '\n'
    assert [delta.text for delta in deltas] == ['Hel', 'lo']
    assert tool_result.message == 'file contents here'
    assert not tool_result.failed
    # the summary closes clean with claude's own authoritative figures
    assert result.turns == 2
    assert result.duration == pytest.approx(1.5)
    assert result.cost == pytest.approx(0.0432)
    assert result.final
    assert not result.failed
    assert not result.budget_stopped
    assert parser.cost == pytest.approx(0.0432)


@pytest.mark.parametrize(
    argnames=('init_model', 'configured', 'recorded'),
    argvalues=[
        # defaulted spawn: no --model, so only the init frame names the
        # actual model backing the session -- the parser must record it (an
        # empty model would make model-per-node unrecoverable)
        pytest.param('claude-fable-5', None, 'claude-fable-5', id='defaulted'),
        # explicit spawn: the stream's resolved id beats the configured alias
        pytest.param('claude-opus-4-8', 'opus', 'claude-opus-4-8', id='alias'),
        # a frame without a model falls back to the configured one
        pytest.param(None, 'claude-opus-4-8', 'claude-opus-4-8', id='fallback'),
    ],
)
def test_parser_captures_the_session_once_and_prefers_the_stream_model(
    init_model: Optional[str],
    configured: Optional[str],
    recorded: str,
) -> None:
    """The session rides every event but emits once, with the real model."""
    parser = ClaudeParser(model=configured)
    init: dict[str, Any] = {'type': 'system', 'subtype': 'init', 'session_id': 'sess-m'}
    if init_model is not None:
        init['model'] = init_model
    events = parser.feed(json.dumps(init))
    assert [(event.kind, event.session, event.model) for event in events] == [
        ('session', 'sess-m', recorded),
    ]
    assert parser.session == 'sess-m'
    assert parser.model == recorded
    # later frames carrying the id emit no second session event
    assert parser.feed(json.dumps({'type': 'system', 'session_id': 'sess-m'})) == []


def test_parser_restamps_the_served_model_from_assistant_rows() -> None:
    """Assistant rows re-stamp the session with the model that actually served.

    The init frame names the model claude *resolved*, not the one the API
    served -- infrastructure can silently substitute mid-session -- so each
    real top-level assistant row naming a different model re-stamps the
    session, and the step row ends up recording the last served model.
    Synthetic rows (the CLI's injected error stand-ins) name no real
    model, a non-string model is wire noise, and a sidechain row (a
    subagent's, flagged by ``parent_tool_use_id``) legitimately runs its
    own model -- none may overwrite the fact. The ``models`` record keeps
    every distinct served model: a substitution the stream recovered from
    must stay visible after the last re-stamp returns to the resolved
    model.
    """
    parser = ClaudeParser(model='claude-fable-5')
    frames = [
        {
            'type': 'system',
            'subtype': 'init',
            'session_id': 'sess-d',
            'model': 'claude-fable-5',
        },
        {
            'type': 'assistant',
            'session_id': 'sess-d',
            'message': {'id': 'msg_a', 'model': 'claude-opus-4-8'},
        },
        {
            'type': 'assistant',
            'session_id': 'sess-d',
            'parent_tool_use_id': 'toolu_01',
            'message': {'id': 'msg_s', 'model': 'claude-haiku-4-5'},
        },
        {
            'type': 'assistant',
            'session_id': 'sess-d',
            'message': {'id': 'msg_b', 'model': '<synthetic>'},
        },
        {
            'type': 'assistant',
            'session_id': 'sess-d',
            'message': {'id': 'msg_c', 'model': {'oops': 1}},
        },
        {
            'type': 'assistant',
            'session_id': 'sess-d',
            'message': {'id': 'msg_d', 'model': 'claude-fable-5'},
        },
    ]
    events = [event for line in _lines(frames) for event in parser.feed(line)]
    assert [(event.kind, event.session, event.model) for event in events] == [
        ('session', 'sess-d', 'claude-fable-5'),
        ('session', 'sess-d', 'claude-opus-4-8'),
        ('session', 'sess-d', 'claude-fable-5'),
    ]
    # the row keeps the last served model; the record keeps them all
    assert parser.model == 'claude-fable-5'
    assert parser.models == ['claude-opus-4-8', 'claude-fable-5']


def test_parser_error_result() -> None:
    """An error result carries the failure detail on the result event."""
    parser = ClaudeParser()
    frame = {
        'type': 'result',
        'subtype': 'error_during_execution',
        'is_error': True,
        'num_turns': 1,
        'duration_ms': 100,
        'result': 'boom',
    }
    (event,) = parser.feed(json.dumps(frame))
    assert event.kind == 'result'
    assert event.failed
    assert event.message == 'boom'
    assert event.duration == pytest.approx(0.1)
    # claude reports failures via its exit code -- the drained stream itself
    # never fails the turn
    assert parser.errors == []


def test_parser_null_duration() -> None:
    """A present-but-null ``duration_ms`` reads as 0.0 rather than crashing.

    The key can be explicitly ``null`` on a result frame; ``0.001 * None``
    would raise (and take down the stream consumer), so it must coalesce.
    """
    parser = ClaudeParser()
    frame = {
        'type': 'result',
        'subtype': 'success',
        'num_turns': 1,
        'duration_ms': None,
        'total_cost_usd': 0.01,
    }
    (event,) = parser.feed(json.dumps(frame))
    assert event.duration == 0.0
    assert event.turns == 1


@pytest.mark.parametrize(
    argnames=('subtype', 'stopped'),
    argvalues=[
        pytest.param('error_max_budget_usd', True, id='budget-stop'),
        pytest.param('success', False, id='normal-result'),
    ],
)
def test_parser_marks_a_budget_stop(subtype: str, stopped: bool) -> None:
    """Only a ``--max-budget-usd`` hit reads as a clean budget stop."""
    parser = ClaudeParser()
    frame = {
        'type': 'result',
        'subtype': subtype,
        'is_error': subtype != 'success',
        'total_cost_usd': 0.02,
        'num_turns': 1,
        'duration_ms': 1000,
    }
    (event,) = parser.feed(json.dumps(frame))
    assert event.budget_stopped is stopped
    assert parser.budget_stopped is stopped


def test_parser_tolerates_garbage_claude() -> None:
    """Malformed, non-object, and unknown lines yield nothing, never raise."""
    parser = ClaudeParser()
    junk = ['', '   ', 'not json', '[1, 2]', '"text"', '{}', '{"type": "mystery"}']
    assert [event for line in junk for event in parser.feed(line)] == []
    assert parser.session is None
    assert parser.cost is None


def test_parser_tolerates_present_null_payloads_claude() -> None:
    """A present-null nested field yields nothing, never raises (wire noise).

    ``message.get('event', {})`` returns ``None`` for ``{"event": null}`` -- a
    default only fills an absent key -- so the parser coerces with ``or {}``,
    keeping a malformed frame from crashing the live agent.
    """
    parser = ClaudeParser(model='claude')
    frames = [
        '{"type": "stream_event", "event": null}',
        '{"type": "user", "message": null}',
        '{"type": "assistant", "message": null}',
        '{"type": "stream_event", "event":'
        ' {"type": "content_block_start", "content_block": null}}',
        '{"type": "stream_event", "event":'
        ' {"type": "content_block_delta", "delta": null}}',
    ]
    assert [event for line in frames for event in parser.feed(line)] == []


def test_parser_ignores_a_non_numeric_result_cost() -> None:
    """A string ``total_cost_usd`` stays unpriced, never rides to the ledger.

    A wire figure reaches claude's ledger and the renderer's ``:.4f`` format
    unpriced, so a non-numeric value must read as no cost (the sibling backends
    guard the identical wire fact), else a completed invocation crashes.
    """
    parser = ClaudeParser(model='claude')
    frames = [
        {
            'type': 'result',
            'subtype': 'success',
            'num_turns': 1,
            'duration_ms': 1000,
            'total_cost_usd': '0.05',
        },
    ]
    (result,) = [event for line in _lines(frames) for event in parser.feed(line)]
    assert result.kind == 'result'
    assert result.cost is None
    assert parser.cost is None


def test_events_render_through_the_production_renderer_claude(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Parsed events drive the CLI renderer: deltas, tools, results, summary."""
    parser = ClaudeParser()
    render = StreamRenderer()
    frames = [
        {
            'type': 'stream_event',
            'event': {
                'type': 'content_block_start',
                'content_block': {'type': 'text'},
            },
        },
        {
            'type': 'stream_event',
            'event': {
                'type': 'content_block_delta',
                'delta': {'type': 'text_delta', 'text': 'Hello world'},
            },
        },
        {
            'type': 'stream_event',
            'event': {
                'type': 'content_block_start',
                'content_block': {'type': 'tool_use', 'name': 'Read'},
            },
        },
        {
            'type': 'user',
            'message': {
                'content': [
                    {
                        'type': 'tool_result',
                        'content': 'file contents here',
                        'is_error': False,
                    },
                ],
            },
        },
        {
            'type': 'result',
            'subtype': 'success',
            'duration_ms': 5000,
            'total_cost_usd': 0.1234,
            'num_turns': 3,
        },
    ]
    for line in _lines(frames):
        for event in parser.feed(line):
            render(event)
    out = capsys.readouterr().out
    assert 'Hello world' in out
    # the open text run closes on its own line before the tool header
    assert 'Hello world\n\n' in out
    assert 'Read' in out
    assert 'file contents' in out
    # the authoritative result closes on turns, duration, and cost
    assert '— 3 turns, 5.0s, $0.1234' in out


def test_renderer_closes_a_truncated_stream_on_a_fresh_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stream killed mid-delta ends on a newline and a ``— $?`` summary.

    A truncated stream (no result frame) leaves the text run open;
    ``close()`` -- called by the driving command after the drain -- ends it
    so whatever prints next starts below the reply, not spliced onto it,
    and settles the placeholder summary for the unaccounted turn.
    """
    parser = ClaudeParser()
    render = StreamRenderer()
    frames = [
        {
            'type': 'stream_event',
            'event': {
                'type': 'content_block_delta',
                'delta': {'type': 'text_delta', 'text': 'partial reply'},
            },
        },
    ]
    for line in _lines(frames):
        for event in parser.feed(line):
            render(event)
    # close is idempotent -- the run ends on exactly one fresh line and
    # exactly one placeholder summary
    render.close()
    render.close()
    out = capsys.readouterr().out
    assert out.startswith('partial reply\n')
    assert out.count('— $?') == 1
    assert out.endswith('\n')


def test_parser_flushes_cost_per_assistant_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each assistant message emits the running estimate, not one total.

    The stream reader can die by signal, so every priced assistant event
    must carry the accumulated figure the moment it arrives.
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    parser = ClaudeParser(model='claude-fable-5')
    frames = [
        {'type': 'assistant', 'message': {'usage': _USAGE_FIRST}},
        {'type': 'assistant', 'message': {'usage': _USAGE_SECOND}},
    ]
    events = [event for line in _lines(frames) for event in parser.feed(line)]
    assert [event.kind for event in events] == ['cost', 'cost']
    assert [event.cost for event in events] == [
        pytest.approx(_USAGE_FIRST_COST),
        pytest.approx(_USAGE_FIRST_COST + _USAGE_SECOND_COST),
    ]
    # running estimates stay estimates until a result frame settles the cost
    assert not any(event.final for event in events)


def test_parser_prices_a_multi_block_message_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One message's repeated block frames price once, not once per block.

    Claude emits an assistant frame per content block (thinking, tool_use,
    ...), each repeating the message-level usage; pricing every frame would
    inflate a killed step's estimate toward Nx for an N-block message.
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    parser = ClaudeParser(model='claude-fable-5')
    frames = [
        {'type': 'assistant', 'message': {'id': 'msg_a', 'usage': _USAGE_FIRST}},
        {'type': 'assistant', 'message': {'id': 'msg_a', 'usage': _USAGE_FIRST}},
        {'type': 'assistant', 'message': {'id': 'msg_b', 'usage': _USAGE_SECOND}},
    ]
    events = [event for line in _lines(frames) for event in parser.feed(line)]
    # two cost events (one per distinct id), not three
    assert [event.cost for event in events] == [
        pytest.approx(_USAGE_FIRST_COST),
        pytest.approx(_USAGE_FIRST_COST + _USAGE_SECOND_COST),
    ]


def test_parser_result_overwrites_the_accumulated_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The authoritative result cost replaces the accumulated estimate.

    Claude's ``total_cost_usd`` includes spend the per-event estimate cannot
    see (subagents, server-side tools), so a normally-ended stream settles
    on exactly the result figure, never the estimate.
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    parser = ClaudeParser(model='claude-fable-5')
    frames = [
        {'type': 'assistant', 'message': {'usage': _USAGE_FIRST}},
        {
            'type': 'result',
            'subtype': 'success',
            'duration_ms': 1000,
            'total_cost_usd': 0.9,
            'num_turns': 1,
        },
    ]
    events = [event for line in _lines(frames) for event in parser.feed(line)]
    assert parser.cost == 0.9
    assert parser.final
    assert events[-1].cost == 0.9
    assert events[-1].final


def test_parser_unpriced_model_accumulates_no_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown/unpriced model accumulates nothing rather than crashing.

    Without a priceable model the estimate is impossible, so a truncated
    stream records no cost (a result frame, when present, still carries
    claude's own figure).
    """
    monkeypatch.setattr(pricing, '_load', lambda: {})
    parser = ClaudeParser(model='mystery')
    frames = [{'type': 'assistant', 'message': {'usage': _USAGE_FIRST}}]
    assert [event for line in _lines(frames) for event in parser.feed(line)] == []
    assert parser.cost is None


def test_compute_cost_prices_disjoint_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anthropic usage buckets are disjoint and each priced at its own rate."""
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    cost = claude._compute_cost(_USAGE_FIRST, 'claude-fable-5')
    assert cost == pytest.approx(_USAGE_FIRST_COST)


def test_compute_cost_unpriced_model_returns_none_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown or rate-less model prices to ``None``, never $0."""
    monkeypatch.setattr(pricing, '_load', lambda: {'bare': {}})
    assert claude._compute_cost(_USAGE_FIRST, 'mystery') is None
    assert claude._compute_cost(_USAGE_FIRST, 'bare') is None
    assert claude._compute_cost(_USAGE_FIRST, None) is None


def test_stream_records_cost_model_and_session_claude(node_with_db: Node) -> None:
    """The driver stamps the real session and settles claude's own figure."""
    node = node_with_db
    backend = ClaudeAgent(node, 'claude')
    (step_id,) = _steps(node, 1)
    frames = [
        {'type': 'system', 'subtype': 'init', 'session_id': 'sess_abc'},
        {
            'type': 'result',
            'subtype': 'success',
            'duration_ms': 1000,
            'total_cost_usd': 0.42,
            'num_turns': 2,
        },
    ]
    result = backend.stream(_lines(frames), step_id=step_id, model='claude-opus-4-8')
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['agent'] == 'claude'
    assert row['session'] == 'sess_abc'
    assert row['model'] == 'claude-opus-4-8'
    assert row['cost'] == pytest.approx(0.42)
    # the session persists for the next continuous step, and rides the result
    assert node.sessions.get('claude') == 'sess_abc'
    assert result.session == 'sess_abc'
    assert result.cost == pytest.approx(0.42)


def test_stream_detached_keeps_session_unpersisted_claude(node_with_db: Node) -> None:
    """A detached turn stamps the step row but never persists ``.session``."""
    node = node_with_db
    backend = ClaudeAgent(node, 'claude')
    (step_id,) = _steps(node, 1)
    frames = [{'type': 'system', 'subtype': 'init', 'session_id': 'sess_x'}]
    backend.stream(_lines(frames), step_id=step_id, detached=True)
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['session'] == 'sess_x'
    assert node.sessions.get('claude') is None


def test_stream_records_full_per_invocation_cost(node_with_db: Node) -> None:
    """Claude's figure is per-invocation, so it lands as-is (no delta).

    Even with a prior step sharing the session, the cost is not reduced --
    a cumulative-delta subtraction would be wrong for ``'call'`` scope.
    """
    node = node_with_db
    backend = ClaudeAgent(node, 'claude')
    prior_id, step_id = _steps(node, 2)
    node.record.step_session('claude', step_id=prior_id, model=None, session='s')
    node.record.step_cost(step_id=prior_id, cost=0.10)
    frames = [
        {'type': 'system', 'subtype': 'init', 'session_id': 's'},
        {
            'type': 'result',
            'subtype': 'success',
            'duration_ms': 1000,
            'total_cost_usd': 0.05,
            'num_turns': 1,
        },
    ]
    backend.stream(_lines(frames), step_id=step_id)
    row = node.db.read('steps', where={'step_id': step_id})[0]
    # recorded as-is (0.05), NOT 0.05 - 0.10 (a cumulative-delta subtraction
    # would be wrong here -- claude's figure is per-invocation)
    assert row['cost'] == pytest.approx(0.05)


def test_stream_truncated_records_accumulated_cost(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream cut before the result still flushed the metered cost.

    A timeout- or SIGKILL-terminated agent never emits its ``result`` frame:
    each assistant message's usage must already be priced and recorded, or
    the step bills $0 and every cap and ledger is blind to the spend.
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    node = node_with_db
    backend = _TrackingClaudeAgent(node, 'claude')
    (step_id,) = _steps(node, 1)
    frames = [
        {'type': 'system', 'subtype': 'init', 'session_id': 'sess_cut'},
        {'type': 'assistant', 'message': {'usage': _USAGE_FIRST}},
        {'type': 'assistant', 'message': {'usage': _USAGE_SECOND}},
        # no result frame: the agent was killed here
    ]
    backend.stream(_lines(frames), step_id=step_id, model='claude-fable-5')
    # one flush per assistant event, each already on the ledger when it fired
    assert backend.flushed == [
        pytest.approx(_USAGE_FIRST_COST),
        pytest.approx(_USAGE_FIRST_COST + _USAGE_SECOND_COST),
    ]
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == pytest.approx(_USAGE_FIRST_COST + _USAGE_SECOND_COST)


def test_stream_survives_missing_pricing_cache(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """No pricing cache degrades to unpriced accrual, never a crash.

    ``needs_pricing`` gates the run-start cache bootstrap to token-priced
    agents, so a claude-only host may carry no ``~/.fractal/pricing.json``
    at all -- best-effort accrual must skip pricing (no flush), never break
    the stream mid-step.
    """
    monkeypatch.setattr(pricing, '_PRICING_CACHE', str(tmp_path / 'absent.json'))
    pricing._load.cache_clear()
    node = node_with_db
    backend = _TrackingClaudeAgent(node, 'claude')
    (step_id,) = _steps(node, 1)
    frames = [
        {'type': 'system', 'subtype': 'init', 'session_id': 'sess_nocache'},
        {'type': 'assistant', 'message': {'usage': _USAGE_FIRST}},
        {
            'type': 'result',
            'subtype': 'success',
            'session_id': 'sess_nocache',
            'total_cost_usd': 0.5,
        },
    ]
    backend.stream(_lines(frames), step_id=step_id, model='claude-fable-5')
    pricing._load.cache_clear()
    # the unpriceable assistant frame flushed nothing; the result frame's
    # authoritative figure is the only recorded cost
    assert backend.flushed == [0.5]


@pytest.mark.parametrize(
    argnames=('kwargs', 'expect', 'absent'),
    argvalues=[
        # fresh mints centrally -- detached steps included, one code path
        pytest.param(
            {},
            ['--session-id'],
            ['--resume', '--fork-session'],
            id='fresh',
        ),
        pytest.param(
            {'session': 'past-9'},
            ['--resume', 'past-9'],
            ['--fork-session', '--session-id'],
            id='resume-in-place',
        ),
        pytest.param(
            {'session': 'past-9', 'fork': True},
            ['--resume', 'past-9', '--fork-session'],
            ['--session-id'],
            id='fork',
        ),
    ],
)
def test_invocation_modes_build_the_pinned_argv_claude(
    node_with_db: Node,
    kwargs: dict[str, Any],
    expect: list[str],
    absent: list[str],
) -> None:
    """Each session mode lands its pinned flags, in the worktree."""
    backend = ClaudeAgent(node_with_db, 'claude')
    invocation = backend.invocation('hello', **kwargs)
    argv = invocation.argv
    # the stream-json launch shape is fixed
    assert argv[:6] == (
        'claude',
        '-p',
        '--output-format',
        'stream-json',
        '--include-partial-messages',
        '--verbose',
    )
    # the prompt closes the argv behind the '--' sentinel, so a dash-leading
    # message is the message, never parsed as a flag
    assert argv[-2:] == ('--', 'hello')
    for token in expect:
        assert token in argv
    for token in absent:
        assert token not in argv
    # a fresh invocation carries the centrally minted lowercase id
    if not kwargs:
        minted = argv[argv.index('--session-id') + 1]
        assert minted == invocation.session
        assert minted == minted.lower()
    # claude runs in the worktree on the user's own config home; the env
    # composes over os.environ (routing keys scrubbed, the rest untouched)
    assert invocation.cwd == node_with_db.worktree
    assert invocation.env['PATH'] == os.environ['PATH']


def test_invocation_honors_command_model_budget_and_settings(
    node_with_db: Node,
) -> None:
    """Extra command words splice in; flags gate on their facts."""
    node = node_with_db
    backend = ClaudeAgent(node, 'claude --foo')
    # no node settings file yet -- the flag must not point at a missing file
    bare = backend.invocation('hi', model='claude-opus-4-8', budget=2.5, effort='high')
    assert bare.argv[:2] == ('claude', '--foo')
    assert '--settings' not in bare.argv
    assert bare.argv[bare.argv.index('--model') + 1] == 'claude-opus-4-8'
    assert bare.argv[bare.argv.index('--max-budget-usd') + 1] == '2.5'
    # the effort rides the --effort flag alone -- never the spawn env, which
    # would leak into the session's subprocesses and nested claude runs
    assert bare.argv[bare.argv.index('--effort') + 1] == 'high'
    assert 'CLAUDE_CODE_EFFORT_LEVEL' not in (bare.env or {})
    # the seeded node settings ride the CLI flag once the file exists
    settings = node.node_dir / '.claude' / 'settings.json'
    settings.parent.mkdir()
    settings.write_text('{}\n', encoding='utf-8')
    seeded = backend.invocation('hi')
    assert seeded.argv[seeded.argv.index('--settings') + 1] == str(settings)
    # an effort-less launch carries no effort flag
    assert '--effort' not in seeded.argv


def test_routed_invocation_redirects_the_anthropic_seam(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The openrouter route rides env alone: base url, key, and model slots."""
    node = node_with_db
    monkeypatch.setenv('OPENROUTER_API_KEY', 'sk-or-sentinel')
    backend = ClaudeAgent(node, 'claude', 'openrouter')
    routed = backend.invocation('hi')
    # the vendor env vars redirect the CLI; the argv itself is unchanged
    assert routed.env is not None
    assert routed.env['ANTHROPIC_BASE_URL'] == 'https://openrouter.ai/api'
    assert routed.env['ANTHROPIC_AUTH_TOKEN'] == 'sk-or-sentinel'  # noqa: S105
    # the api key must be explicitly empty, never merely absent
    assert routed.env['ANTHROPIC_API_KEY'] == ''
    for slot in ('OPUS', 'SONNET', 'HAIKU', 'FABLE'):
        expected = f'~anthropic/claude-{slot.lower()}-latest'
        assert routed.env[f'ANTHROPIC_DEFAULT_{slot}_MODEL'] == expected
    # a model-less launch pins an explicit priceable slug (process env
    # beats the settings-file model)
    assert routed.env['ANTHROPIC_MODEL'] == 'anthropic/claude-sonnet-4.6'
    # the base environment spreads underneath the injected keys
    assert routed.env['PATH'] == os.environ['PATH']
    # an explicit model drops the pin and rides the argv as usual
    priced = backend.invocation('hi', model='anthropic/claude-haiku-4.5')
    assert 'ANTHROPIC_MODEL' not in priced.env
    assert priced.argv[priced.argv.index('--model') + 1] == (
        'anthropic/claude-haiku-4.5'
    )
    # a keyless environment refuses the launch outright -- chat and per-step
    # provider rebinds reach the builder unprobed by the boot preflight
    monkeypatch.delenv('OPENROUTER_API_KEY')
    with pytest.raises(RuntimeError, match='OPENROUTER_API_KEY is not set'):
        backend.invocation('hi')


def test_native_invocation_scrubs_inherited_routing_keys(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native launch scrubs inherited vendor vars; a routed one sets them."""
    node = node_with_db
    # a routed ancestor's spawn env leaks the vendor vars into os.environ
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://openrouter.ai/api')
    native = ClaudeAgent(node, 'claude').invocation('hi')
    assert 'ANTHROPIC_BASE_URL' not in native.env
    # the rest of the user's environment still passes through
    assert native.env['PATH'] == os.environ['PATH']
    # the routed builder still pins the redirect over the inherited copy
    monkeypatch.setenv('OPENROUTER_API_KEY', 'sk-or-sentinel')
    routed = ClaudeAgent(node, 'claude', 'openrouter').invocation('hi')
    assert routed.env['ANTHROPIC_BASE_URL'] == 'https://openrouter.ai/api'


def test_routed_parser_prices_the_result_usage_through_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routed runs price the result frame's own usage, never total_cost_usd."""
    monkeypatch.setattr(
        pricing,
        '_load',
        lambda: {'openrouter/anthropic/claude-haiku-4.5': _PRICING['claude-fable-5']},
    )
    parser = _RoutedClaudeParser(model='anthropic/claude-haiku-4.5')
    frames = [
        # the gateway zeroes assistant-frame usage -- nothing accrues
        {
            'type': 'assistant',
            'session_id': 's-1',
            'message': {'usage': {'input_tokens': 0, 'output_tokens': 0}},
        },
        # the result usage is real and prices through the openrouter/ prefix
        {
            'type': 'result',
            'subtype': 'success',
            'total_cost_usd': 99.0,
            'usage': _USAGE_FIRST,
            'num_turns': 1,
            'duration_ms': 10,
        },
    ]
    events = [event for line in _lines(frames) for event in parser.feed(line)]
    # no zero-dollar accrual event fires; the close carries the chain price
    assert [event.cost for event in events if event.kind == 'cost'] == []
    (result,) = [event for event in events if event.kind == 'result']
    assert result.cost == pytest.approx(_USAGE_FIRST_COST)
    assert result.final
    assert parser.cost == pytest.approx(_USAGE_FIRST_COST)
    assert parser.cost != 99.0
    # an unpriced slug closes with no figure rather than the gateway estimate
    monkeypatch.setattr(pricing, '_load', lambda: {})
    unpriced = _RoutedClaudeParser(model='mystery/model')
    events = [event for line in _lines(frames) for event in unpriced.feed(line)]
    (result,) = [event for event in events if event.kind == 'result']
    assert result.cost is None
    assert unpriced.cost is None


def test_routed_parser_sums_the_result_model_usage_per_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routed runs price every model in the result's table; an unpriced one closes None."""
    monkeypatch.setattr(
        pricing,
        '_load',
        lambda: {
            'openrouter/anthropic/claude-haiku-4.5': _PRICING['claude-fable-5'],
            'claude-fable-5-1': _PRICING['claude-fable-5'],
        },
    )
    parent = {
        'inputTokens': 4,
        'outputTokens': 242,
        'cacheReadInputTokens': 49882,
        'cacheCreationInputTokens': 13283,
    }
    child = {
        'inputTokens': 2,
        'outputTokens': 4,
        'cacheReadInputTokens': 7350,
        'cacheCreationInputTokens': 5107,
    }
    frames = [
        {
            'type': 'result',
            'subtype': 'success',
            'total_cost_usd': 99.0,
            # the frame's own usage covers the parent's tokens alone
            'usage': _USAGE_FIRST,
            'modelUsage': {
                'anthropic/claude-haiku-4.5': parent,
                'claude-fable-5-1': child,
            },
            'num_turns': 2,
            'duration_ms': 10,
        },
    ]
    parser = _RoutedClaudeParser(model='anthropic/claude-haiku-4.5')
    events = [event for line in _lines(frames) for event in parser.feed(line)]
    (result,) = [event for event in events if event.kind == 'result']
    rates = _PRICING['claude-fable-5']
    expected = sum(
        entry['inputTokens'] * rates['input_cost_per_token']
        + entry['cacheReadInputTokens'] * rates['cache_read_input_token_cost']
        + entry['cacheCreationInputTokens'] * rates['cache_creation_input_token_cost']
        + entry['outputTokens'] * rates['output_cost_per_token']
        for entry in (parent, child)
    )
    assert result.cost == pytest.approx(expected)
    assert parser.cost == pytest.approx(expected)
    assert parser.cost != 99.0
    # a sub-agent model the chain cannot price closes with no figure
    monkeypatch.setattr(
        pricing,
        '_load',
        lambda: {'openrouter/anthropic/claude-haiku-4.5': _PRICING['claude-fable-5']},
    )
    unpriced = _RoutedClaudeParser(model='anthropic/claude-haiku-4.5')
    events = [event for line in _lines(frames) for event in unpriced.feed(line)]
    (result,) = [event for event in events if event.kind == 'result']
    assert result.cost is None
    assert unpriced.cost is None


def test_routed_parser_tolerates_gateway_null_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit-null cache buckets from the gateway never crash pricing.

    OpenRouter's Anthropic-compat stream sends the cache buckets as
    explicit ``null`` on partial assistant frames; a ``.get(key, 0.0)``
    would return None for a present-null key and the cost arithmetic
    would raise, killing the stream reader mid-run (the loop then SIGKILLs
    the agent). Both the running estimate and the priced close must
    absorb the nulls.
    """
    monkeypatch.setattr(
        pricing,
        '_load',
        lambda: {'openrouter/anthropic/claude-haiku-4.5': _PRICING['claude-fable-5']},
    )
    parser = _RoutedClaudeParser(model='anthropic/claude-haiku-4.5')
    frames = [
        # the exact partial-frame shape OpenRouter emits: zeros and nulls
        {
            'type': 'assistant',
            'session_id': 's-1',
            'message': {
                'usage': {
                    'input_tokens': 0,
                    'output_tokens': 0,
                    'cache_creation_input_tokens': None,
                    'cache_read_input_tokens': None,
                }
            },
        },
        {
            'type': 'result',
            'subtype': 'success',
            'usage': {
                'input_tokens': 10,
                'output_tokens': 45,
                'cache_creation_input_tokens': None,
                'cache_read_input_tokens': None,
            },
            'num_turns': 1,
            'duration_ms': 10,
        },
    ]
    # no TypeError; the priced close counts only the non-null buckets
    events = [event for line in _lines(frames) for event in parser.feed(line)]
    (result,) = [event for event in events if event.kind == 'result']
    rates = _PRICING['claude-fable-5']
    expected = 10 * rates['input_cost_per_token'] + 45 * rates['output_cost_per_token']
    assert result.cost == pytest.approx(expected)


def test_routed_tracking_and_preflight_gate_on_the_route(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routed spend tracks only priced models; preflight demands the key."""
    node = node_with_db
    monkeypatch.setattr(
        pricing,
        '_load',
        lambda: {'openrouter/anthropic/claude-haiku-4.5': _PRICING['claude-fable-5']},
    )
    # `sh` stands in for the claude binary so the PATH check passes
    routed = ClaudeAgent(node, 'sh', 'openrouter')
    # natively claude always tracks; routed only with a chain-priced model
    assert ClaudeAgent(node, 'claude').tracks_cost()
    assert not routed.tracks_cost()
    assert routed.tracks_cost('anthropic/claude-haiku-4.5')
    assert not routed.tracks_cost('mystery/model')
    # the routed parser variant binds through the route
    assert isinstance(routed.parser(), _RoutedClaudeParser)
    assert type(ClaudeAgent(node, 'claude').parser()) is ClaudeParser
    # preflight fails fast without the key, and passes with it
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    with pytest.raises(RuntimeError, match='OPENROUTER_API_KEY is not set'):
        routed.preflight()
    monkeypatch.setenv('OPENROUTER_API_KEY', 'sk-or-sentinel')
    routed.preflight()


def test_rates_falls_back_through_the_openrouter_chain_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The alias chain: exact, openrouter/ prefix, then the bare model name."""
    monkeypatch.setattr(
        pricing,
        '_load',
        lambda: {
            'exact-id': {'input_cost_per_token': 1e-6},
            'openrouter/anthropic/claude-haiku-4.5': {'input_cost_per_token': 2e-6},
            'claude-fable-5': {'input_cost_per_token': 3e-6},
        },
    )
    # an exact hit never consults the fallbacks
    assert claude._rates('exact-id') == {'input_cost_per_token': 1e-6}
    # an openrouter slug resolves via the LiteLLM openrouter/ prefix
    assert claude._rates('anthropic/claude-haiku-4.5') == {
        'input_cost_per_token': 2e-6,
    }
    # a prefix miss falls back to the author-stripped bare name
    assert claude._rates('anthropic/claude-fable-5') == {
        'input_cost_per_token': 3e-6,
    }
    # every miss returns None (unpriced), never a guessed entry
    assert claude._rates('mystery/model') is None


def test_config_model_walks_the_settings_chain(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first settings file naming a model wins: node, local, worktree, home."""
    node = node_with_db
    backend = ClaudeAgent(node, 'claude')
    # isolate the chain's tail from the real user home
    home = tmp_path / 'home'
    (home / '.claude').mkdir(parents=True)
    monkeypatch.setattr(pathlib.Path, 'home', lambda: home)
    assert backend.config_model() is None
    # the home settings back the chain
    (home / '.claude' / 'settings.json').write_text(
        '{"model": "from-home"}\n',
        encoding='utf-8',
    )
    assert backend.config_model() == 'from-home'
    # a worktree settings model outranks the home one
    worktree_settings = node.worktree / '.claude' / 'settings.json'
    worktree_settings.parent.mkdir()
    worktree_settings.write_text('{"model": "from-worktree"}\n', encoding='utf-8')
    assert backend.config_model() == 'from-worktree'
    # the worktree's local settings outrank its shared file
    local_settings = node.worktree / '.claude' / 'settings.local.json'
    local_settings.write_text('{"model": "from-local"}\n', encoding='utf-8')
    assert backend.config_model() == 'from-local'
    # the node's own settings outrank everything; a malformed file is skipped
    node_settings = node.node_dir / '.claude' / 'settings.json'
    node_settings.parent.mkdir()
    node_settings.write_text('not json\n', encoding='utf-8')
    assert backend.config_model() == 'from-local'
    node_settings.write_text('{"model": "from-node"}\n', encoding='utf-8')
    assert backend.config_model() == 'from-node'


def test_seed_config_disables_fast_mode_claude(tmp_path: pathlib.Path) -> None:
    """The packaged claude seed keeps fast mode off (aligned across agents)."""
    node_dir = tmp_path / 'node'
    (node_dir / 'skills').mkdir(parents=True)
    ClaudeAgent.seed(node_dir)
    config = json.loads(
        (node_dir / '.claude' / 'settings.json').read_text(encoding='utf-8')
    )
    assert config['fastMode'] is False


def test_transcript_resolves_the_config_home_slug(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transcripts key by the worktree slug; drift is served only when owned."""
    node = node_with_db
    backend = ClaudeAgent(node, 'claude')
    home = tmp_path / 'claude-home'
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(home))
    # the projects dir keys by the worktree slug under the config home
    slug = re.sub(r'[^A-Za-z0-9]', '-', str(node.worktree))
    session = str(uuid.uuid4())
    transcript = home / 'projects' / slug / f'{session}.jsonl'
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"type": "turn"}\n', encoding='utf-8')
    found = backend.transcript(session)
    assert found == {
        'agent': 'claude',
        'session': session,
        'path': str(transcript),
        'exists': True,
        'content': '{"type": "turn"}\n',
    }
    # an absent id still returns the deterministic path, so a caller can
    # poll for the file to appear
    absent = str(uuid.uuid4())
    missing = backend.transcript(absent)
    assert missing['exists'] is False
    assert missing['path'] == str(home / 'projects' / slug / f'{absent}.jsonl')
    # a transcript under a foreign slug serves only ids recorded for this
    # node -- an ungated lookup would expose any session of the OS user
    stray = str(uuid.uuid4())
    foreign = home / 'projects' / 'some-other-project' / f'{stray}.jsonl'
    foreign.parent.mkdir(parents=True)
    foreign.write_text('{"type": "relocated"}\n', encoding='utf-8')
    assert backend.transcript(stray)['exists'] is False
    (step_id,) = _steps(node, 1)
    node.record.step_session('claude', step_id=step_id, model=None, session=stray)
    relocated = backend.transcript(stray)
    assert relocated['exists'] is True
    assert relocated['content'] == '{"type": "relocated"}\n'


# ------ helpers


class _TrackingClaudeAgent(ClaudeAgent):
    """Claude backend recording every settled cost flush."""

    def __init__(
        self: _TrackingClaudeAgent,
        node: Node,
        command: Optional[str] = None,
    ) -> None:
        """Initialize ``_TrackingClaudeAgent``."""
        super().__init__(node, command)
        self.flushed: list[float] = []

    def on_record_cost(
        self: _TrackingClaudeAgent,
        *args: Any,
        **kwargs: Any,
    ) -> Event:
        """Record the settled figure alongside the ledger write."""
        event = super().on_record_cost(*args, **kwargs)
        self.flushed.append(event.cost)
        return event


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
