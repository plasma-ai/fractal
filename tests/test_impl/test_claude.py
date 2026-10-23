"""Test the ``fractal.impl.claude`` module.

The claude dialect end to end: the stream-json protocol parsed into
normalized events, the ``-p`` argv builder, per-call pricing over the
disjoint Anthropic usage buckets, the settings-chain model default, and
the config-home transcript layout. Stream-level cases drive the base
``Agent.stream`` driver against a real node ledger.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import logging
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
    'test_parser_reports_a_thinking_cap_only_for_a_thinking_only_reply',
    'test_parser_holds_the_idle_clock_through_a_top_level_think',
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
    'test_one_hour_cache_writes_price_at_their_own_rate',
    'test_compute_cost_unpriced_model_returns_none_claude',
    'test_stream_records_cost_model_and_session_claude',
    'test_stream_detached_keeps_session_unpersisted_claude',
    'test_stream_records_full_per_invocation_cost',
    'test_stream_truncated_records_accumulated_cost',
    'test_stream_books_a_synthetic_reply_as_a_known_zero',
    'test_stream_reopens_a_settled_cost_at_a_reply_after_its_result',
    'test_stream_books_a_resumed_launchs_figures_less_its_baseline',
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
    'test_unavailable_names_the_provider_outage_shapes_claude',
    'test_config_defaults_walk_the_settings_chain',
    'test_seed_config_disables_fast_mode_claude',
    'test_transcript_resolves_the_config_home_slug',
    'test_settle_cost_adds_each_readable_sub_agent_transcript',
    'test_settle_reads_workflow_sub_agent_transcripts',
    'test_parallel_sub_agents_price_each_reply_once',
    'test_sub_agent_frames_price_at_their_own_model',
    'test_settle_prices_a_reply_in_two_transcripts_once_at_its_latest_line',
    'test_transcript_cost_keeps_a_record_whole_across_unicode_line_breaks',
    'test_settle_marks_a_record_missing_part_of_a_reply',
    'test_settle_marks_an_entry_it_cannot_read_whole',
    'test_settle_marks_a_reply_it_cannot_price',
    'test_settle_marks_a_reply_that_searched_the_web',
    'test_settle_marks_a_reply_billed_at_a_premium',
    'test_settle_marks_a_reply_above_a_long_prompt_threshold',
    'test_settle_marks_sub_agent_spend_it_cannot_book_after_the_result',
    'test_settle_prices_a_recorded_launch_as_its_cost_record_does',
    'test_cost_baseline_is_the_total_a_resume_restores',
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

# a killed launch's window and a transcript stamp inside it
_OPENED = '2026-01-01T00:00:00.000Z'
_WRITTEN = '2026-01-01T00:00:01.000Z'
_CLOSED = '2026-01-01T00:00:02.000Z'
# a result frame's arrival inside that window, and a stamp past it
_AFTER = '2026-01-01T00:00:01.500Z'
_LATE = '2026-01-01T00:00:01.800Z'

# a live claude 2.1.300 capture (trimmed, session as @SESSION@): a turn's
# result on line 18, then claude's wake-up on a background task's notice and
# a new reply (from line 23) closed by its own result on line 33
_BACKGROUND_WAIT = (
    pathlib.Path(__file__).parents[1]
    / 'test_core'
    / 'streams'
    / 'claude_background_wait.jsonl'
)

# a live claude 2.1.300 launch with three sub-agents, laid out as claude keeps
# it: the session's transcript, its cost-state record last, beside a folder of
# the sub-agents' (trimmed to the assistant lines, a run of a reply's lines with
# the same usage cut to its last, content blanked, tool ids renumbered, session
# as @SESSION@)
_SUB_AGENT_LAUNCH = (
    pathlib.Path(__file__).parent / 'transcripts' / 'sub_agent_launch.jsonl'
)

# the rates claude prices that launch's cost record at, for its main model and
# for the side model its web tools call (whose calls hold no cache)
_RECORDED_RATES = {
    'claude-opus-5-5': {
        'input_cost_per_token': 4e-6,
        'output_cost_per_token': 2e-5,
        'cache_read_input_token_cost': 2e-7,
        'cache_creation_input_token_cost': 5e-6,
        'cache_creation_input_token_cost_above_1hr': 8e-6,
    },
    'claude-haiku-5-5': {
        'input_cost_per_token': 1e-7,
        'output_cost_per_token': 5e-7,
    },
}

# claude's give-up after its last continuation: a synthetic error reply, then
# an is_error result whose subtype still reads success
_GIVE_UP_FRAMES: list[dict[str, Any]] = [
    {
        'type': 'assistant',
        'message': {
            'model': '<synthetic>',
            'id': '0bd9e489-14d4-4866-bfeb-6cdd65e78a68',
            'role': 'assistant',
            'stop_reason': 'stop_sequence',
            'content': [
                {
                    'type': 'text',
                    'text': "API Error: Claude's response exceeded the 128000"
                    ' output token maximum.',
                },
            ],
        },
        'parent_tool_use_id': None,
        'session_id': 'sess-1',
        'error': 'max_output_tokens',
        'is_api_error_message': True,
    },
    {
        'type': 'result',
        'subtype': 'success',
        'is_error': True,
        'terminal_reason': 'api_error',
        'api_error': 'max_output_tokens',
        'result': "API Error: Claude's response exceeded the 128000 output token"
        ' maximum.',
        'num_turns': 4,
        'duration_ms': 12253,
        'total_cost_usd': 0.0015,
        'session_id': 'sess-1',
    },
]


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
    events = [event for line in _lines(frames) for event in _feed(parser, line)]
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
    events = _feed(parser, json.dumps(init))
    assert [(event.kind, event.session, event.model) for event in events] == [
        ('session', 'sess-m', recorded),
    ]
    assert parser.session == 'sess-m'
    assert parser.model == recorded
    # later frames carrying the id emit no second session event
    assert _feed(parser, json.dumps({'type': 'system', 'session_id': 'sess-m'})) == []


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
    events = [event for line in _lines(frames) for event in _feed(parser, line)]
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
    (event,) = _feed(parser, json.dumps(frame))
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
    (event,) = _feed(parser, json.dumps(frame))
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
    (event,) = _feed(parser, json.dumps(frame))
    assert event.budget_stopped is stopped
    assert parser.budget_stopped is stopped


# a capped reply as the stream carries it: the raw message_start, a thinking
# block's start and its assistant frame (stop_reason null), then the raw
# message_delta carrying the API's max_tokens stop
_CAPPED_THINKING = (
    ('start', 'msg_1', None),
    ('thinking_start', 'msg_1', None),
    ('thinking', 'msg_1', None),
)


@pytest.mark.parametrize(
    argnames=('frames', 'capped'),
    argvalues=[
        # the API's own stop alone reports the cap, before any continuation
        pytest.param([*_CAPPED_THINKING, ('stop', 'msg_1', None)], True, id='stop'),
        # claude's continuation alone backstops it (no partial-message events)
        pytest.param(
            [('thinking', 'msg_1', None), ('continuation', None, None)],
            True,
            id='continuation',
        ),
        # the wording past the opening varies by version
        pytest.param(
            [('thinking', 'msg_1', None), ('reasoning_continuation', None, None)],
            True,
            id='continuation-wording',
        ),
        # the transcript's shape: plain-string content flagged isMeta
        pytest.param(
            [('thinking', 'msg_1', None), ('meta_continuation', None, None)],
            True,
            id='continuation-string',
        ),
        # the undocumented flag on claude's injected turn is never required
        pytest.param(
            [('thinking', 'msg_1', None), ('unflagged_continuation', None, None)],
            True,
            id='unflagged-continuation',
        ),
        # both signals for one reply report it once
        pytest.param(
            [
                *_CAPPED_THINKING,
                ('stop', 'msg_1', None),
                ('continuation', None, None),
            ],
            True,
            id='stop-and-continuation',
        ),
        # a reply that reached text or a tool call before the cap has work to
        # continue from -- claude's own continuation is the right response;
        # a block seen only on its raw start event still counts
        pytest.param(
            [
                *_CAPPED_THINKING,
                ('text', 'msg_1', None),
                ('stop', 'msg_1', None),
                ('continuation', None, None),
            ],
            False,
            id='text',
        ),
        pytest.param(
            [
                *_CAPPED_THINKING,
                ('tool_use', 'msg_1', None),
                ('stop', 'msg_1', None),
                ('continuation', None, None),
            ],
            False,
            id='tool-use',
        ),
        pytest.param(
            [
                *_CAPPED_THINKING,
                ('text_start', 'msg_1', None),
                ('stop', 'msg_1', None),
            ],
            False,
            id='text-streamed',
        ),
        # a sub-agent's thinking-only cap is its own business; the top-level
        # reply before it launched the sub-agent
        pytest.param(
            [
                ('tool_use', 'msg_1', None),
                ('start', 'msg_2', 'toolu_1'),
                ('thinking_start', 'msg_2', 'toolu_1'),
                ('thinking', 'msg_2', 'toolu_1'),
                ('stop', 'msg_2', 'toolu_1'),
                ('continuation', None, 'toolu_1'),
            ],
            False,
            id='sidechain',
        ),
    ],
)
def test_parser_reports_a_thinking_cap_only_for_a_thinking_only_reply(
    frames: list[tuple[str, Optional[str], Optional[str]]],
    capped: bool,
) -> None:
    """A thinking-only top-level reply cut at the output cap reports once.

    Frames are trimmed from a real ``stream-json`` capture: the raw
    ``message_delta`` stop is the primary signal, claude's injected
    continuation the backstop, and either reports the cap. After its last
    continuation claude gives up with a synthetic error reply and an
    ``is_error`` result, which parse as a plain failed result.
    """
    parser = ClaudeParser()
    # each entry is (frame kind, message id, parent tool-use id)
    lines = _lines(
        [
            {'type': 'system', 'subtype': 'init', 'session_id': 'sess-1'},
            *(_reply_frame(*entry) for entry in frames),
            *_GIVE_UP_FRAMES,
        ]
    )
    events = [event for line in lines for event in _feed(parser, line)]
    caps = [event for event in events if event.kind == 'thinking_cap']
    assert len(caps) == (1 if capped else 0)
    if capped:
        (cap,) = caps
        assert cap.session == 'sess-1'
    # the give-up tail closes as a failed result carrying claude's message
    result = events[-1]
    assert result.kind == 'result'
    assert result.failed
    assert 'output token maximum' in (result.message or '')


@pytest.mark.parametrize(
    argnames=('frames', 'clock'),
    argvalues=[
        # claude 2.1.300 writes the block's frame, then forwards its stop
        pytest.param(
            [
                ('thinking_start', 'msg_1', None),
                ('thinking', 'msg_1', None),
                ('thinking_stop', 'msg_1', None),
            ],
            [['waiting'], ['acting'], []],
            id='frame-first',
        ),
        # the stop ends the think when it lands first
        pytest.param(
            [
                ('thinking_start', 'msg_1', None),
                ('thinking_stop', 'msg_1', None),
                ('thinking', 'msg_1', None),
            ],
            [['waiting'], ['acting'], []],
            id='stop-first',
        ),
        # a new reply, or the reply's own end, closes a think left open
        pytest.param(
            [
                ('thinking_start', 'msg_1', None),
                ('start', 'msg_2', None),
                ('thinking_stop', 'msg_2', None),
            ],
            [['waiting'], ['acting'], []],
            id='new-reply',
        ),
        pytest.param(
            [
                ('thinking_start', 'msg_1', None),
                ('stop', 'msg_1', None),
                ('thinking_stop', 'msg_1', None),
            ],
            [['waiting'], [], []],
            id='reply-end',
        ),
        # a sub-agent's think never touches the clock
        pytest.param(
            [
                ('thinking_start', 'msg_1', 'toolu_1'),
                ('thinking', 'msg_1', 'toolu_1'),
                ('thinking_stop', 'msg_1', 'toolu_1'),
            ],
            [[], [], []],
            id='sidechain',
        ),
    ],
)
def test_parser_holds_the_idle_clock_through_a_top_level_think(
    frames: list[tuple[str, Optional[str], Optional[str]]],
    clock: list[list[str]],
) -> None:
    """A top-level think waits from its block's start until the block completes.

    The stream shows nothing of a think in progress, so its block start
    pauses the idle clock and its completion -- the block's assistant
    frame or its raw stop, whichever lands first -- restarts it.
    """
    parser = ClaudeParser()
    # each entry is (frame kind, message id, parent tool-use id)
    lines = _lines([_reply_frame(*entry) for entry in frames])
    events = [
        [
            event.kind
            for event in parser.feed(line)
            if event.kind in ('acting', 'waiting')
        ]
        for line in lines
    ]
    assert events == clock


def test_parser_tolerates_garbage_claude() -> None:
    """Malformed, non-object, and unknown lines yield nothing, never raise."""
    parser = ClaudeParser()
    junk = ['', '   ', 'not json', '[1, 2]', '"text"', '{}', '{"type": "mystery"}']
    assert [event for line in junk for event in _feed(parser, line)] == []
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
    assert [event for line in frames for event in _feed(parser, line)] == []


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
    (result,) = [event for line in _lines(frames) for event in _feed(parser, line)]
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
        for event in _feed(parser, line):
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
        for event in _feed(parser, line):
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
    events = [event for line in _lines(frames) for event in _feed(parser, line)]
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
    events = [event for line in _lines(frames) for event in _feed(parser, line)]
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
    events = [event for line in _lines(frames) for event in _feed(parser, line)]
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
    assert [event for line in _lines(frames) for event in _feed(parser, line)] == []
    assert parser.cost is None


def test_compute_cost_prices_disjoint_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anthropic usage buckets are disjoint and each priced at its own rate."""
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    cost = claude._compute_cost(_USAGE_FIRST, 'claude-fable-5')
    assert cost == pytest.approx(_USAGE_FIRST_COST)


@pytest.mark.parametrize(
    argnames=('hour_rate', 'hour', 'expected'),
    argvalues=[
        # the one-hour writes at their own rate, the rest at the base rate
        pytest.param(6e-6, 600, 600 * 6e-6 + 400 * 3.75e-6, id='one-hour-rate'),
        # a model with no one-hour rate prices every write at the base rate
        pytest.param(None, 600, 1000 * 3.75e-6, id='no-one-hour-rate'),
        # one-hour writes past the write total price as the total
        pytest.param(6e-6, 1500, 1000 * 6e-6, id='one-hour-over-total'),
    ],
)
def test_one_hour_cache_writes_price_at_their_own_rate(
    monkeypatch: pytest.MonkeyPatch,
    hour_rate: Optional[float],
    hour: int,
    expected: float,
) -> None:
    """A reply's one-hour cache writes bill at the one-hour write rate.

    Claude Code writes one-hour cache entries, which cost more than the
    five-minute ones; the five-minute writes and any the split leaves
    unnamed price at the base write rate.
    """
    rates = dict(_PRICING['claude-fable-5'])
    if hour_rate is not None:
        rates['cache_creation_input_token_cost_above_1hr'] = hour_rate
    monkeypatch.setattr(pricing, '_load', lambda: {'claude-fable-5': rates})
    usage = {
        'cache_creation_input_tokens': 1000,
        'cache_creation': {
            'ephemeral_1h_input_tokens': hour,
            'ephemeral_5m_input_tokens': 300,
        },
    }
    cost = claude._compute_cost(usage, 'claude-fable-5')
    assert cost == pytest.approx(expected)


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
    """A figure streamed with no baseline lands as-is (no ledger delta).

    Even with a prior step sharing the session, the cost is not reduced --
    ``'call'`` scope never settles against sibling rows; what a resumed
    launch's figure carries over comes off through its baseline instead.
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
    # would be wrong here -- a baseline is the session's saved total, never
    # the ledger's rows)
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


@pytest.mark.parametrize(
    argnames='parent',
    argvalues=[
        # the agent's own reply
        pytest.param(None, id='top-level'),
        # a sub-agent's reply
        pytest.param('toolu_a', id='sub-agent'),
    ],
)
def test_stream_books_a_synthetic_reply_as_a_known_zero(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    parent: Optional[str],
) -> None:
    """A stream whose only reply is synthetic books $0, never an unknown cost.

    Claude Code stands a synthetic reply in for an API error, naming the
    model ``<synthetic>`` and a usage of zeros. Nothing was billed, so the
    reply prices at the stream's own model, and a launch killed after it
    books a known $0.
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    node = node_with_db
    backend = ClaudeAgent(node, 'claude')
    (step_id,) = _steps(node, 1)
    # real-shaped (claude 2.1.300): a synthetic reply's usage is all zero
    usage = {
        'input_tokens': 0,
        'output_tokens': 0,
        'cache_creation_input_tokens': 0,
        'cache_read_input_tokens': 0,
        'cache_creation': {
            'ephemeral_1h_input_tokens': 0,
            'ephemeral_5m_input_tokens': 0,
        },
    }
    frames = [
        {'type': 'system', 'subtype': 'init', 'session_id': 'sess_err'},
        {
            'type': 'assistant',
            'message': {'id': 'msg_err', 'model': '<synthetic>', 'usage': usage},
            'parent_tool_use_id': parent,
            'session_id': 'sess_err',
        },
        # no result frame: the agent was killed here
    ]
    result = backend.stream(_lines(frames), step_id=step_id, model='claude-fable-5')
    assert result.cost == 0.0
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == 0.0


@pytest.mark.parametrize(
    argnames=('count', 'final'),
    argvalues=[
        # the turn's result settles the launch's cost
        pytest.param(18, True, id='result'),
        # the wake-up's reply spends past the settled figure
        pytest.param(25, False, id='wake-up-reply'),
        # until its own result settles the cost again
        pytest.param(33, True, id='wake-up-result'),
    ],
)
def test_stream_reopens_a_settled_cost_at_a_reply_after_its_result(
    node_with_db: Node,
    count: int,
    final: bool,
) -> None:
    """A reply after a result leaves the stream's cost an estimate again.

    Claude Code outlives a turn's result while background work runs, then
    wakes the agent for a new reply, so a launch killed inside that reply
    must still read as unsettled for the loop to price it.
    """
    backend = ClaudeAgent(node_with_db, 'claude')
    with _BACKGROUND_WAIT.open(encoding='utf-8') as file:
        lines = list(itertools.islice(file, count))
    assert backend.stream(lines).final is final


@pytest.mark.parametrize(
    argnames=('baseline', 'totals', 'settled'),
    argvalues=[
        # the saved total comes off each result's running total
        pytest.param(0.5, (0.55, 0.58), True, id='settled'),
        # an unreadable saved total refuses every figure
        pytest.param(None, (0.55, 0.58), False, id='unreadable'),
        # a zeroed figure below the saved total refuses it and every later one
        pytest.param(0.5, (0.0, 0.58), False, id='below-the-baseline'),
    ],
)
def test_stream_books_a_resumed_launchs_figures_less_its_baseline(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    baseline: Optional[float],
    totals: tuple[float, float],
    settled: bool,
) -> None:
    """A resumed launch's result figures book less the total its session saved.

    Claude counts a resumed launch's result figure on from the session's
    saved total, so the stream takes the baseline off each one, and a reply
    after a result builds on the launch's own figure. A figure the baseline
    cannot settle is refused, and every later one with it: the result
    names no cost and says so, and the running figure stays the stream's
    own estimate.
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    node = node_with_db
    backend = _TrackingClaudeAgent(node, 'claude')
    (step_id,) = _steps(node, 1)
    first, second = totals
    frames = [
        {'type': 'system', 'subtype': 'init', 'session_id': 's'},
        {'type': 'assistant', 'message': {'id': 'msg_a', 'usage': _USAGE_FIRST}},
        {'type': 'result', 'subtype': 'success', 'total_cost_usd': first},
        # claude wakes the agent once background work ends, for a new reply
        {'type': 'assistant', 'message': {'id': 'msg_b', 'usage': _USAGE_SECOND}},
        {'type': 'result', 'subtype': 'success', 'total_cost_usd': second},
    ]
    events: list[Any] = []
    result = backend.stream(
        _lines(frames),
        step_id=step_id,
        model='claude-fable-5',
        render=events.append,
        baseline=baseline,
    )
    results = [event for event in events if event.kind == 'result']
    if settled:
        assert baseline is not None
        own = first - baseline
        assert backend.flushed == pytest.approx(
            [_USAGE_FIRST_COST, own, own + _USAGE_SECOND_COST, second - baseline]
        )
        assert [event.refused for event in results] == [False, False]
    else:
        estimate = _USAGE_FIRST_COST + _USAGE_SECOND_COST
        assert backend.flushed == pytest.approx([_USAGE_FIRST_COST, estimate])
        assert [(event.cost, event.refused) for event in results] == [(None, True)] * 2
    assert result.final is settled
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == pytest.approx(backend.flushed[-1])


def test_stream_survives_missing_pricing_cache(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """No pricing cache degrades to unpriced accrual, never a crash.

    A claude-only run refreshes the cache best-effort, so a host whose
    fetch fails may carry no ``~/.fractal/pricing.json`` at all --
    best-effort accrual must skip pricing (no flush), never break the
    stream mid-step.
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
    events = [event for line in _lines(frames) for event in _feed(parser, line)]
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
    events = [event for line in _lines(frames) for event in _feed(unpriced, line)]
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
    events = [event for line in _lines(frames) for event in _feed(parser, line)]
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
    events = [event for line in _lines(frames) for event in _feed(unpriced, line)]
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
    events = [event for line in _lines(frames) for event in _feed(parser, line)]
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


@pytest.mark.parametrize(
    argnames=('detail', 'unavailable'),
    argvalues=[
        pytest.param(
            'API Error: 529 {"type":"error","error":{"type":"overloaded_error",'
            '"message":"Overloaded"}}',
            True,
            id='overloaded-529',
        ),
        pytest.param('API Error: Repeated 529 Overloaded errors', True, id='repeated'),
        pytest.param(
            'API Error: 500 {"type":"error","error":{"type":"api_error",'
            '"message":"Internal server error"}}',
            True,
            id='server-error',
        ),
        pytest.param(
            'API Error: 503 upstream connect error or disconnect/reset before headers',
            True,
            id='service-unavailable',
        ),
        pytest.param(
            'API Error: 429 {"type":"error","error":{"type":"rate_limit_error",'
            '"message":"Number of request tokens has exceeded your rate limit"}}',
            True,
            id='rate-limit',
        ),
        # Claude Code's own text (2.1.300) for a response stream that broke
        # off: a lost connection, a server error, or a stall
        pytest.param(
            'API Error: Connection lost mid-response. The response above may be'
            ' incomplete.',
            True,
            id='connection-lost',
        ),
        pytest.param(
            'API Error: Connection lost before a response was produced. Try again.',
            True,
            id='connection-lost-before-response',
        ),
        pytest.param(
            'API Error: Server error mid-response. The response above may be'
            ' incomplete.',
            True,
            id='server-error-mid-response',
        ),
        pytest.param(
            'API Error: The response stopped arriving. The response above may be'
            ' incomplete.',
            True,
            id='response-stopped',
        ),
        pytest.param(
            'API Error: The response stalled before a response was produced. Try'
            ' again.',
            True,
            id='response-stalled',
        ),
        # the host's own fault, not the provider's
        pytest.param(
            'API Error: Your computer went to sleep mid-response. The response'
            ' above may be incomplete.',
            False,
            id='host-asleep',
        ),
        pytest.param(
            "API Error: Claude's response exceeded the 128000 output token maximum.",
            False,
            id='output-cap',
        ),
        pytest.param(
            'API Error: 400 {"type":"error","error":{"type":"invalid_request_error",'
            '"message":"prompt is too long"}}',
            False,
            id='bad-request',
        ),
        pytest.param('Credit balance is too low', False, id='billing'),
        pytest.param('error_max_turns', False, id='max-turns'),
        pytest.param(
            "Error: ENOENT: no such file or directory, open 'plan.md'",
            False,
            id='tool-error',
        ),
    ],
)
def test_unavailable_names_the_provider_outage_shapes_claude(
    node_with_db: Node,
    detail: str,
    unavailable: bool,
) -> None:
    """Only an overloaded, failing, rate-limited, or cut-off API reads as an outage."""
    backend = ClaudeAgent(node_with_db, 'claude')
    assert backend.unavailable(detail) is unavailable


@pytest.mark.parametrize(
    argnames=('key', 'verb'),
    argvalues=[
        pytest.param('model', 'config_model', id='model'),
        pytest.param('effortLevel', 'config_effort', id='effort'),
    ],
)
def test_config_defaults_walk_the_settings_chain(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    verb: str,
) -> None:
    """The first settings file naming a default wins: node, local, worktree, home."""
    node = node_with_db
    backend = ClaudeAgent(node, 'claude')
    default = getattr(backend, verb)
    # isolate the chain's tail from the real user home
    home = tmp_path / 'home'
    (home / '.claude').mkdir(parents=True)
    monkeypatch.setattr(pathlib.Path, 'home', lambda: home)
    assert default() is None
    # the home settings back the chain
    (home / '.claude' / 'settings.json').write_text(
        json.dumps({key: 'from-home'}),
        encoding='utf-8',
    )
    assert default() == 'from-home'
    # a worktree settings default outranks the home one
    worktree_settings = node.worktree / '.claude' / 'settings.json'
    worktree_settings.parent.mkdir()
    worktree_settings.write_text(json.dumps({key: 'from-worktree'}), encoding='utf-8')
    assert default() == 'from-worktree'
    # the worktree's local settings outrank its shared file
    local_settings = node.worktree / '.claude' / 'settings.local.json'
    local_settings.write_text(json.dumps({key: 'from-local'}), encoding='utf-8')
    assert default() == 'from-local'
    # the node's own settings outrank everything; a malformed file, or one
    # whose JSON is not an object, is skipped
    node_settings = node.node_dir / '.claude' / 'settings.json'
    node_settings.parent.mkdir()
    for content in ('not json\n', 'null\n', '[]\n', '"text"\n'):
        node_settings.write_text(content, encoding='utf-8')
        assert default() == 'from-local', content
    node_settings.write_text(json.dumps({key: 'from-node'}), encoding='utf-8')
    assert default() == 'from-node'


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


def test_settle_cost_adds_each_readable_sub_agent_transcript(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A killed launch's settle prices its session's and its sub-agents' replies.

    Claude keeps each sub-agent's replies in a transcript of its own beside
    the session's. A record cut off mid-write never swallows the next
    file's first one, and a file the read cannot open is skipped with a
    warning while the rest still price.
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude-home'))
    caplog.set_level(logging.WARNING)
    node = node_with_db
    backend = ClaudeAgent(node, 'claude')
    session = str(uuid.uuid4())
    (step_id,) = _steps(node, 1)
    node.record.step_session('claude', step_id=step_id, model=None, session=session)
    transcript = pathlib.Path(backend.transcript(session)['path'])
    subagents = transcript.with_suffix('') / 'subagents'
    subagents.mkdir(parents=True)
    # the session's reply, then a record the kill cut off mid-write
    torn = _transcript_line('msg_main', _USAGE_FIRST) + '{"type": "assist'
    transcript.write_text(torn, encoding='utf-8')
    side = _transcript_line('msg_side', _USAGE_SECOND)
    (subagents / 'agent-a.jsonl').write_text(side, encoding='utf-8')
    # a sub-agent transcript the read cannot open (a directory in its place)
    (subagents / 'agent-b.jsonl').mkdir()
    backend.settle_cost(step_id, started=_OPENED, ended=_CLOSED)
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == pytest.approx(_USAGE_FIRST_COST + _USAGE_SECOND_COST)
    (warning,) = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert warning.startswith('launch partly unpriced: sub-agent transcript')
    assert 'agent-b.jsonl' in warning


def test_settle_reads_workflow_sub_agent_transcripts(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow's sub-agents, a folder deeper, price beside the session's.

    A workflow keeps each agent's transcript in a folder of its own, beside
    a journal that holds no reply of the launch's. A sub-agent transcript
    last written before the launch opened belongs to an earlier launch, so
    the settle never reads it.
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude-home'))
    node = node_with_db
    backend = ClaudeAgent(node, 'claude')
    session = str(uuid.uuid4())
    (step_id,) = _steps(node, 1)
    node.record.step_session('claude', step_id=step_id, model=None, session=session)
    transcript = pathlib.Path(backend.transcript(session)['path'])
    workflow = transcript.with_suffix('') / 'subagents' / 'workflows' / 'wf_1'
    workflow.mkdir(parents=True)
    transcript.write_text(_transcript_line('msg_main', _USAGE_FIRST), encoding='utf-8')
    side = _transcript_line('msg_flow', _USAGE_SECOND)
    (workflow / 'agent-a.jsonl').write_text(side, encoding='utf-8')
    journal = _transcript_line('msg_journal', {'output_tokens': 50_000})
    (workflow / 'journal.jsonl').write_text(journal, encoding='utf-8')
    # an earlier launch's sub-agent, untouched since before this one opened
    stale = workflow.parent.parent / 'agent-old.jsonl'
    stale_line = _transcript_line('msg_stale', {'output_tokens': 50_000})
    stale.write_text(stale_line, encoding='utf-8')
    before = dt.datetime.fromisoformat(_OPENED).timestamp() - 60
    os.utime(stale, (before, before))
    backend.settle_cost(step_id, started=_OPENED, ended=_CLOSED)
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == pytest.approx(_USAGE_FIRST_COST + _USAGE_SECOND_COST)


def test_parallel_sub_agents_price_each_reply_once(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A killed launch's parallel sub-agents book each reply once.

    Parallel sub-agents interleave their replies' block frames on the
    stream, so the stream's estimate prices each reply once, at its first
    frame, below its final usage. With no result figure, the settle books
    the sub-agents' transcripts over that estimate.
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    node = node_with_db
    backend, step_id = _settling(node, tmp_path, monkeypatch, '')
    session = node.db.read('steps', where={'step_id': step_id})[0]['session']
    # two sub-agents' replies, two block frames apiece, interleaved, no result
    parents = {'msg_a': 'toolu_a', 'msg_b': 'toolu_b'}
    usages = {'msg_a': _USAGE_FIRST, 'msg_b': _USAGE_SECOND}
    frames = [
        {
            'type': 'assistant',
            'message': {'id': message_id, 'usage': usages[message_id]},
            'parent_tool_use_id': parents[message_id],
            'session_id': session,
        }
        for message_id in ('msg_a', 'msg_b', 'msg_a', 'msg_b')
    ]
    backend.stream(_lines(frames), step_id=step_id, model='claude-fable-5')
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == pytest.approx(_USAGE_FIRST_COST + _USAGE_SECOND_COST)
    # each sub-agent's transcript holds its reply at its final usage, a
    # hundred output tokens past its first frame's
    transcript = pathlib.Path(backend.transcript(session)['path'])
    subagents = transcript.with_suffix('') / 'subagents'
    subagents.mkdir(parents=True)
    for message_id, usage in usages.items():
        final = {**usage, 'output_tokens': usage['output_tokens'] + 100}
        line = _transcript_line(message_id, final)
        (subagents / f'agent-{message_id}.jsonl').write_text(line, encoding='utf-8')
    assert backend.settle_cost(step_id, started=_OPENED, ended=_CLOSED) is False
    row = node.db.read('steps', where={'step_id': step_id})[0]
    settled = _USAGE_FIRST_COST + _USAGE_SECOND_COST + 2 * 100 * 1.5e-5
    assert row['cost'] == pytest.approx(settled)


@pytest.mark.parametrize(
    argnames=('named', 'streamed', 'settled'),
    argvalues=[
        # a cheaper sub-agent model than the parent's, at its own rates
        pytest.param(
            'claude-haiku-5-5',
            100 * 1e-6 + 1000 * 1.25e-6 + 10_000 * 1e-7 + 200 * 5e-6,
            100 * 1e-6 + 1000 * 1.25e-6 + 10_000 * 1e-7 + 300 * 5e-6,
            id='own-model',
        ),
        # a frame naming no model, at the parent's
        pytest.param(
            None,
            _USAGE_FIRST_COST,
            _USAGE_FIRST_COST + 100 * 1.5e-5,
            id='no-model',
        ),
    ],
)
def test_sub_agent_frames_price_at_their_own_model(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    named: Optional[str],
    streamed: float,
    settled: float,
) -> None:
    """A killed launch's sub-agent books at its own model's rates.

    A sub-agent runs its own model, so the stream prices each of its
    frames at the model the frame names, and a frame naming none at the
    parent's. With no result figure, the settle books the sub-agent's
    transcript over that estimate, never a figure priced at the parent's
    rates above it.
    """
    rates = {
        **_PRICING,
        'claude-haiku-5-5': {
            'input_cost_per_token': 1e-6,
            'output_cost_per_token': 5e-6,
            'cache_read_input_token_cost': 1e-7,
            'cache_creation_input_token_cost': 1.25e-6,
        },
    }
    monkeypatch.setattr(pricing, '_load', lambda: rates)
    node = node_with_db
    backend, step_id = _settling(node, tmp_path, monkeypatch, '')
    session = node.db.read('steps', where={'step_id': step_id})[0]['session']
    # the sub-agent's first frame, its parent streaming on another model, and
    # no result
    reply: dict[str, Any] = {'id': 'msg_side', 'usage': _USAGE_FIRST}
    if named is not None:
        reply['model'] = named
    frame = {
        'type': 'assistant',
        'message': reply,
        'parent_tool_use_id': 'toolu_a',
        'session_id': session,
    }
    backend.stream(_lines([frame]), step_id=step_id, model='claude-fable-5')
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == pytest.approx(streamed)
    # the sub-agent's transcript holds the reply at its final usage, a
    # hundred output tokens on, under the model that served it
    transcript = pathlib.Path(backend.transcript(session)['path'])
    subagents = transcript.with_suffix('') / 'subagents'
    subagents.mkdir(parents=True)
    final = {**_USAGE_FIRST, 'output_tokens': 300}
    line = _transcript_line('msg_side', final, model=named or 'claude-fable-5')
    (subagents / 'agent-a.jsonl').write_text(line, encoding='utf-8')
    assert backend.settle_cost(step_id, started=_OPENED, ended=_CLOSED) is False
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == pytest.approx(settled)


def test_settle_prices_a_reply_in_two_transcripts_once_at_its_latest_line(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply id written to two transcripts prices once, at its latest line.

    The latest line holds the reply's final usage, whichever file it sits
    in, so an earlier copy in the session's transcript neither adds to it
    nor stands in for it.
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    node = node_with_db
    # the session's transcript holds the reply first, at its earlier usage
    early = _transcript_line('msg_main', _USAGE_SECOND)
    backend, step_id = _settling(node, tmp_path, monkeypatch, early)
    session = node.db.read('steps', where={'step_id': step_id})[0]['session']
    # a sub-agent's transcript holds the same reply later, at its final usage
    transcript = pathlib.Path(backend.transcript(session)['path'])
    subagents = transcript.with_suffix('') / 'subagents'
    subagents.mkdir(parents=True)
    late = _transcript_line('msg_main', _USAGE_FIRST, stamp=_LATE)
    (subagents / 'agent-a.jsonl').write_text(late, encoding='utf-8')
    backend.settle_cost(step_id, started=_OPENED, ended=_CLOSED)
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == pytest.approx(_USAGE_FIRST_COST)


def test_transcript_cost_keeps_a_record_whole_across_unicode_line_breaks(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply's final line prices whole though its text holds line separators.

    Claude leaves U+2028 and U+0085 raw inside a record's strings, so only a
    newline ends a record.
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    early = _transcript_line('msg_a', {'output_tokens': 10})
    final = _transcript_line(
        'msg_a',
        {'output_tokens': 900},
        text='one\u2028two\u0085three',
    )
    backend, step_id = _settling(node_with_db, tmp_path, monkeypatch, early + final)
    assert backend.settle_cost(step_id, started=_OPENED, ended=_CLOSED) is False
    row = node_with_db.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == pytest.approx(900 * 1.5e-5)


@pytest.mark.parametrize(
    argnames=('case', 'leaves_out'),
    argvalues=[
        # the kill landed while the reply's second call still ran
        pytest.param('one of two results', False, id='one-of-two-results'),
        # both calls returned, so the next reply's request was in flight
        pytest.param('both results', True, id='both-results'),
        # an earlier reply's final line was written before it completed
        pytest.param('partial reply mid-window', True, id='partial-mid-window'),
        # past a result, the result's figure covers the partial reply
        pytest.param('partial reply before the result', False, id='partial-before'),
        # a workflow agent ends on its StructuredOutput result
        pytest.param('structured output', False, id='structured-output'),
        # an error result sends the agent to retry
        pytest.param('structured output error', True, id='structured-error'),
        # the capped reply claude never wrote is the newest turn
        pytest.param('capped reply unwritten', False, id='capped-unwritten'),
        # claude wrote the capped reply and continued past it
        pytest.param('capped reply continued', True, id='capped-continued'),
        # with no result figure, a missing transcript leaves everything out
        pytest.param('no transcript', True, id='no-transcript'),
        # past a result, a missing transcript leaves out no spend it knows of
        pytest.param('no transcript after the result', False, id='no-transcript-after'),
    ],
)
def test_settle_marks_a_record_missing_part_of_a_reply(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    leaves_out: bool,
) -> None:
    """The settle says whether the record leaves part of a reply's spend out.

    A reply's request is in flight after a prompt, a continuation, or the
    result of the newest reply's last open call; a reply whose final line
    has no stop reason was written before it completed. A kill during a
    tool call, and a workflow agent's clean StructuredOutput result, are
    whole. Past a result frame, only what follows the result counts.
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    after = None
    replies: list[dict[str, Any]] = []
    calls = _transcript_line(
        'msg_a',
        _USAGE_FIRST,
        stop_reason='tool_use',
        blocks=[_tool_use('toolu_1'), _tool_use('toolu_2')],
    )
    if case == 'one of two results':
        content = calls + _result_line('toolu_1')
    elif case == 'both results':
        content = calls + _result_line('toolu_1') + _result_line('toolu_2')
    elif case.startswith('partial reply'):
        content = _transcript_line('msg_a', _USAGE_FIRST, stop_reason=None)
        if case == 'partial reply before the result':
            after = _AFTER
        content += _transcript_line('msg_b', _USAGE_SECOND, stamp=_LATE)
    elif case.startswith('structured output'):
        output = _tool_use('toolu_out', name='StructuredOutput')
        content = _transcript_line(
            'msg_a',
            _USAGE_FIRST,
            stop_reason='tool_use',
            blocks=[output],
        )
        content += _result_line('toolu_out', is_error=case.endswith('error'))
    elif case.startswith('capped reply'):
        capped = {
            'id': 'msg_cap',
            'model': 'claude-fable-5',
            'stop_reason': 'max_tokens',
            'usage': _USAGE_SECOND,
        }
        replies.append(capped)
        content = _transcript_line('msg_a', _USAGE_FIRST, stop_reason='tool_use')
        content += _result_line('toolu_1')
        if case == 'capped reply continued':
            content += _transcript_line('msg_cap', _USAGE_SECOND, stop_reason=None)
            content += json.dumps(
                {
                    'type': 'user',
                    'timestamp': _WRITTEN,
                    'isMeta': True,
                    'message': {'role': 'user', 'content': 'Output token limit hit.'},
                }
            )
    else:
        content = None
        if case == 'no transcript after the result':
            after = _AFTER
    backend, step_id = _settling(node_with_db, tmp_path, monkeypatch, content)
    verdict = backend.settle_cost(
        step_id,
        started=_OPENED,
        ended=_CLOSED,
        after=after,
        replies=replies,
    )
    assert verdict is leaves_out


def test_settle_marks_an_entry_it_cannot_read_whole(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed count or tool id leaves its part out, never raising.

    The settle prices what it can read, a count that is not a number
    prices as nothing, and the launch's record reads as missing part of a
    reply's spend.
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    noisy = {**_USAGE_SECOND, 'output_tokens': '100'}
    content = _transcript_line('msg_a', _USAGE_FIRST)
    content += _transcript_line(
        'msg_b',
        noisy,
        blocks=[{'type': 'tool_use', 'id': 7, 'name': 'Bash', 'input': {}}],
    )
    backend, step_id = _settling(node_with_db, tmp_path, monkeypatch, content)
    assert backend.settle_cost(step_id, started=_OPENED, ended=_CLOSED) is True
    row = node_with_db.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == pytest.approx(_USAGE_FIRST_COST + 50 * 3e-6)


@pytest.mark.parametrize(
    argnames=('after', 'model', 'extra', 'marked'),
    argvalues=[
        # a killed launch's reply at a model the pricing table lacks
        pytest.param(None, 'claude-mystery-9', 0.0, True, id='killed-unpriced'),
        # past a result, a reply after it at a model the table lacks
        pytest.param(_AFTER, 'claude-mystery-9', 0.0, True, id='after-unpriced'),
        # claude's stand-in for an API error names no priced model, and no tokens
        pytest.param(None, '<synthetic>', 0.0, False, id='synthetic'),
        # a reply priced normally
        pytest.param(None, 'claude-fable-5', _USAGE_SECOND_COST, False, id='priced'),
    ],
)
def test_settle_marks_a_reply_it_cannot_price(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    after: Optional[str],
    model: str,
    extra: float,
    marked: bool,
) -> None:
    """A reply at a model with no price leaves its spend out, and the rest still book.

    Each reply prices at its own model, so one whose model has no price
    drops out of the sum, and the step books the priced replies, marked
    partly unpriced -- killed, or past a result. A synthetic reply holds
    no tokens, so it leaves nothing out.
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    # real-shaped (claude 2.1.300): a synthetic reply's usage is all zero
    usage = _USAGE_SECOND
    if model == '<synthetic>':
        usage = {
            'input_tokens': 0,
            'output_tokens': 0,
            'cache_creation_input_tokens': 0,
            'cache_read_input_tokens': 0,
            'cache_creation': {
                'ephemeral_1h_input_tokens': 0,
                'ephemeral_5m_input_tokens': 0,
            },
        }
    # a reply before the result, a priced one after it, then the reply under test
    content = _transcript_line('msg_a', _USAGE_FIRST)
    content += _transcript_line('msg_b', _USAGE_SECOND, stamp=_LATE)
    content += _transcript_line('msg_c', usage, stamp=_LATE, model=model)
    backend, step_id = _settling(node_with_db, tmp_path, monkeypatch, content)
    # past a result, its figure covers the reply before it
    booked = _USAGE_FIRST_COST
    if after is not None:
        booked = 0.01
        node_with_db.record.step_cost(step_id=step_id, cost=booked)
    verdict = backend.settle_cost(
        step_id,
        started=_OPENED,
        ended=_CLOSED,
        after=after,
    )
    assert verdict is marked
    row = node_with_db.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == pytest.approx(booked + _USAGE_SECOND_COST + extra)


@pytest.mark.parametrize(
    argnames=('after', 'searches', 'fetches', 'marked'),
    argvalues=[
        # a killed launch's reply that searched the web twice
        pytest.param(None, 2, 0, True, id='killed-searched'),
        # past a result, a reply after it that searched
        pytest.param(_AFTER, 2, 0, True, id='after-searched'),
        # no search, no fee
        pytest.param(None, 0, 0, False, id='no-search'),
        # claude charges a web fetch only its tokens
        pytest.param(None, 0, 3, False, id='fetched'),
    ],
)
def test_settle_marks_a_reply_that_searched_the_web(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    after: Optional[str],
    searches: int,
    fetches: int,
    marked: bool,
) -> None:
    """A reply that searched books its tokens, marked for the fee left out.

    Claude Code charges each web search a fee on top of the reply's
    tokens, which the price table does not carry, so the step books the
    tokens and is marked partly unpriced -- killed, or past a result. A
    web fetch costs only its tokens.
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    # real-shaped (claude 2.1.300): every reply's usage counts its server tools
    tools = {'web_search_requests': searches, 'web_fetch_requests': fetches}
    usage = {**_USAGE_SECOND, 'server_tool_use': tools}
    content = _transcript_line('msg_a', _USAGE_FIRST)
    content += _transcript_line('msg_b', usage, stamp=_LATE)
    backend, step_id = _settling(node_with_db, tmp_path, monkeypatch, content)
    # past a result, its figure covers the reply before it
    booked = _USAGE_FIRST_COST
    if after is not None:
        booked = 0.01
        node_with_db.record.step_cost(step_id=step_id, cost=booked)
    verdict = backend.settle_cost(
        step_id,
        started=_OPENED,
        ended=_CLOSED,
        after=after,
    )
    assert verdict is marked
    row = node_with_db.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == pytest.approx(booked + _USAGE_SECOND_COST)


@pytest.mark.parametrize(
    argnames=('after', 'served', 'marked'),
    argvalues=[
        # a killed launch's reply served in the us geography
        pytest.param(None, {'inference_geo': 'us'}, True, id='killed-us'),
        # past a result, a reply after it served in the us geography
        pytest.param(_AFTER, {'inference_geo': 'us'}, True, id='after-us'),
        # a killed launch's reply served in fast mode
        pytest.param(None, {'speed': 'fast'}, True, id='killed-fast'),
        # past a result, a reply after it served in fast mode
        pytest.param(_AFTER, {'speed': 'fast'}, True, id='after-fast'),
        # a reply served anywhere at standard speed bills its tokens alone
        pytest.param(
            None,
            {'inference_geo': 'global', 'speed': 'standard'},
            False,
            id='standard',
        ),
    ],
)
def test_settle_marks_a_reply_billed_at_a_premium(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    after: Optional[str],
    served: dict[str, str],
    marked: bool,
) -> None:
    """A reply billed at a premium books its tokens, marked for the premium.

    Claude Code bills a reply whose usage names the ``us`` inference
    geography at a premium over its token cost, and one served in fast
    mode (``speed: "fast"``) at a multiple of its model's rates. The price
    table carries neither, so the step books the tokens and is marked
    partly unpriced -- killed, or past a result.
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    usage = {**_USAGE_SECOND, **served}
    content = _transcript_line('msg_a', _USAGE_FIRST)
    content += _transcript_line('msg_b', usage, stamp=_LATE)
    backend, step_id = _settling(node_with_db, tmp_path, monkeypatch, content)
    # past a result, its figure covers the reply before it
    booked = _USAGE_FIRST_COST
    if after is not None:
        booked = 0.01
        node_with_db.record.step_cost(step_id=step_id, cost=booked)
    verdict = backend.settle_cost(
        step_id,
        started=_OPENED,
        ended=_CLOSED,
        after=after,
    )
    assert verdict is marked
    row = node_with_db.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == pytest.approx(booked + _USAGE_SECOND_COST)


@pytest.mark.parametrize(
    argnames=('after', 'model', 'cached', 'marked'),
    argvalues=[
        # a killed launch's prompt one token over its model's threshold
        pytest.param(None, 'claude-haiku-5-5', 99_001, True, id='killed-above'),
        # past a result, a reply after it over the threshold
        pytest.param(_AFTER, 'claude-haiku-5-5', 99_001, True, id='after-above'),
        # a prompt at the threshold bills at the base rates
        pytest.param(None, 'claude-haiku-5-5', 99_000, False, id='at-threshold'),
        # a model whose table names no threshold bills any prompt at its rates
        pytest.param(None, 'claude-fable-5', 99_001, False, id='no-threshold'),
    ],
)
def test_settle_marks_a_reply_above_a_long_prompt_threshold(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    after: Optional[str],
    model: str,
    cached: int,
    marked: bool,
) -> None:
    """A prompt over its model's threshold books at the base rates, marked.

    A model's table entry can name a long-prompt threshold
    (``input_cost_per_token_above_100k_tokens``), above which its replies
    bill at higher rates. The prompt is the reply's input with its cache
    reads and writes, as Claude Code counts it. Those rates are not priced
    here, so a reply over the threshold books at the base rates and is
    marked partly unpriced -- killed, or past a result.
    """
    rates = {
        **_PRICING,
        'claude-haiku-5-5': {
            'input_cost_per_token': 1e-6,
            'output_cost_per_token': 5e-6,
            'cache_read_input_token_cost': 1e-7,
            'input_cost_per_token_above_100k_tokens': 2e-6,
            'output_cost_per_token_above_100k_tokens': 1e-5,
        },
    }
    monkeypatch.setattr(pricing, '_load', lambda: rates)
    # a prompt of a thousand input tokens and its cache reads
    usage = {
        'input_tokens': 1_000,
        'cache_read_input_tokens': cached,
        'output_tokens': 100,
    }
    content = _transcript_line('msg_a', _USAGE_FIRST)
    content += _transcript_line('msg_b', usage, stamp=_LATE, model=model)
    backend, step_id = _settling(node_with_db, tmp_path, monkeypatch, content)
    # past a result, its figure covers the reply before it
    booked = _USAGE_FIRST_COST
    if after is not None:
        booked = 0.01
        node_with_db.record.step_cost(step_id=step_id, cost=booked)
    verdict = backend.settle_cost(
        step_id,
        started=_OPENED,
        ended=_CLOSED,
        after=after,
    )
    assert verdict is marked
    # the reply books at its model's base rates
    rate = rates[model]
    base = (
        1_000 * rate['input_cost_per_token']
        + cached * rate['cache_read_input_token_cost']
        + 100 * rate['output_cost_per_token']
    )
    row = node_with_db.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == pytest.approx(booked + base)


@pytest.mark.parametrize(
    argnames=('after_result', 'record', 'marked'),
    argvalues=[
        # the settle finds no record of the sub-agent's spend
        pytest.param('sub-agent', None, True, id='no-transcript'),
        # the record holds the spend at a model the pricing cannot price
        pytest.param('sub-agent', 'unpriced', True, id='unpriced'),
        # the settle books the spend past the result
        pytest.param('sub-agent', 'priced', False, id='priced'),
        # nothing follows the result, whose figure is whole
        pytest.param('nothing', None, False, id='nothing-after'),
        # a later result's figure covers the sub-agent's spend before it
        pytest.param('later result', None, False, id='later-result'),
    ],
)
def test_settle_marks_sub_agent_spend_it_cannot_book_after_the_result(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    after_result: str,
    record: Optional[str],
    marked: bool,
) -> None:
    """A sub-agent streaming past the result marks a settle that books none of it.

    Claude Code can outlive its result while a background sub-agent runs.
    The stream leaves that spend to the settle, which books it from the
    sub-agent's transcript; when the settle cannot (no record, or one it
    cannot price), the step's figure misses spend the stream saw. A result
    nothing follows, or one a later result covers, is whole.
    """
    rates = {} if record == 'unpriced' else _PRICING
    monkeypatch.setattr(pricing, '_load', lambda: rates)
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude-home'))
    node = node_with_db
    backend = ClaudeAgent(node, 'claude')
    session = str(uuid.uuid4())
    (step_id,) = _steps(node, 1)
    # the capture's turn through its result, then a sub-agent's reply (the
    # capture's own reply frame, re-parented), then the wake-up's own result
    with _BACKGROUND_WAIT.open(encoding='utf-8') as file:
        capture = [line.replace('@SESSION@', session) for line in file]
    lines = capture[:18]
    if after_result != 'nothing':
        reply = json.loads(capture[13])
        reply['message']['id'] = 'msg_late'
        reply['parent_tool_use_id'] = 'toolu_bg'
        lines.append(json.dumps(reply) + '\n')
    if after_result == 'later result':
        lines += capture[18:]
    result = backend.stream(lines, step_id=step_id)
    # the sub-agent's transcript holds its reply, completed past the result
    if record is not None:
        transcript = pathlib.Path(backend.transcript(session)['path'])
        subagents = transcript.with_suffix('') / 'subagents'
        subagents.mkdir(parents=True)
        main = _transcript_line('msg_main', _USAGE_FIRST)
        transcript.write_text(main, encoding='utf-8')
        late = _transcript_line('msg_late', _USAGE_SECOND, stamp=_LATE)
        (subagents / 'agent-a.jsonl').write_text(late, encoding='utf-8')
    verdict = backend.settle_cost(
        step_id,
        started=_OPENED,
        ended=_CLOSED,
        after=_AFTER,
        trailing=result.trailing,
    )
    assert verdict is marked


@pytest.mark.parametrize(
    argnames=('model', 'part'),
    argvalues=[
        # the main model's prompt side: input, cache reads and cache writes
        pytest.param('claude-opus-5-5', 'prompt', id='main-model-prompt'),
        # its output side
        pytest.param(
            'claude-opus-5-5',
            'output',
            marks=pytest.mark.xfail(
                strict=True,
                reason='sub-agent replies whose final usage never reaches disk',
            ),
            id='main-model-output',
        ),
        # the side model's spend, whole
        pytest.param(
            'claude-haiku-5-5',
            'all',
            marks=pytest.mark.xfail(
                strict=True,
                reason='side-model calls with no assistant lines',
            ),
            id='side-model',
        ),
    ],
)
def test_settle_prices_a_recorded_launch_as_its_cost_record_does(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    part: str,
) -> None:
    """A real launch's settle prices its window as claude's cost record does.

    Claude Code saves a launch's spend per model to the session transcript
    as a ``cost-state`` record at exit; its start and duration are the
    launch's window. Priced at the record's own rates, one part of the
    spend at a time, the settle of that window from the session's and its
    sub-agents' transcripts matches the record, short of two gaps: two
    sub-agent replies whose last line claude wrote mid-stream (a handful
    of output tokens on a full reply), and the side-model calls claude's
    web tools make, which write no assistant line.
    """
    # price only the part of the spend under test
    rates = _RECORDED_RATES[model]
    output_rate = rates['output_cost_per_token']
    if part == 'prompt':
        rates = {**rates, 'output_cost_per_token': 0.0}
    elif part == 'output':
        rates = {**dict.fromkeys(rates, 0.0), 'output_cost_per_token': output_rate}
    monkeypatch.setattr(pricing, '_load', lambda: {model: rates})
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude-home'))
    node = node_with_db
    backend = ClaudeAgent(node, 'claude')
    session = str(uuid.uuid4())
    (step_id,) = _steps(node, 1)
    node.record.step_session('claude', step_id=step_id, model=None, session=session)
    # lay the launch's transcripts out under the session's
    transcript = pathlib.Path(backend.transcript(session)['path'])
    folder = _SUB_AGENT_LAUNCH.with_suffix('')
    sources = {transcript: _SUB_AGENT_LAUNCH}
    for path in folder.rglob('agent-*.jsonl'):
        sources[transcript.with_suffix('') / path.relative_to(folder)] = path
    for target, source in sources.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_text(encoding='utf-8').replace('@SESSION@', session)
        target.write_text(content, encoding='utf-8')
    # settle the window the launch's record spans
    lines = _SUB_AGENT_LAUNCH.read_text(encoding='utf-8').splitlines()
    record = json.loads(lines[-1])
    opened = dt.datetime.fromtimestamp(record['startTime'] / 1000, dt.UTC)
    closed = opened + dt.timedelta(milliseconds=record['totalDuration'])
    backend.settle_cost(step_id, started=opened.isoformat(), ended=closed.isoformat())
    # the record's figure for the part, its output side at the output rate
    usage = record['modelUsage'][model]
    output = usage['outputTokens'] * output_rate
    expected = {
        'prompt': usage['costUSD'] - output,
        'output': output,
        'all': usage['costUSD'],
    }[part]
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == pytest.approx(expected)


@pytest.mark.parametrize(
    argnames=('saved', 'provider', 'baseline'),
    argvalues=[
        # claude resumes only from a transcript, so a missing one is unknown
        pytest.param(None, None, None, id='no-transcript'),
        # an older claude saves no total
        pytest.param([], None, 0.0, id='none-saved'),
        # each launch saves the session's total at exit; the latest stands
        pytest.param([('own', 0.5483), ('own', 0.6520)], None, 0.6520, id='latest'),
        # another session's record is not the one a resume restores
        pytest.param(
            [('own', 0.5483), ('other', 9.0)],
            None,
            0.5483,
            id='another-session',
        ),
        # a total that is no number cannot be read
        pytest.param([('own', 0.5483), ('own', 'n/a')], None, None, id='unreadable'),
        # routed, the saved usage table prices through the chain
        pytest.param(
            [('own', 0.5483)],
            'openrouter',
            _USAGE_FIRST_COST,
            id='routed',
        ),
    ],
)
def test_cost_baseline_is_the_total_a_resume_restores(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    saved: Optional[list[tuple[str, Any]]],
    provider: Optional[str],
    baseline: Optional[float],
) -> None:
    """A resumed session carries the latest total claude saved for it.

    Claude Code saves a ``cost-state`` record to the session's transcript
    as each launch exits, and a resume restores the latest one naming the
    session. Routed, the result figure prices the record's usage table
    through the alias chain, so the baseline does too. A transcript that
    cannot be found, or a record whose total cannot be read, carries an
    unknown amount, and says why.
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude-home'))
    caplog.set_level(logging.WARNING)
    node = node_with_db
    backend = ClaudeAgent(node, 'claude', provider=provider)
    session = str(uuid.uuid4())
    if saved is not None:
        content = _transcript_line('msg_a', _USAGE_FIRST)
        for owner, total in saved:
            state = _cost_state(session if owner == 'own' else str(uuid.uuid4()), total)
            content += json.dumps(state) + '\n'
            content += _transcript_line('msg_b', _USAGE_SECOND)
        transcript = pathlib.Path(backend.transcript(session)['path'])
        transcript.parent.mkdir(parents=True)
        transcript.write_text(content, encoding='utf-8')
    if baseline is None:
        assert backend.cost_baseline(session) is None
        (warning,) = [record.getMessage() for record in caplog.records]
        assert warning.startswith(f'cost baseline of session {session} unknown')
    else:
        assert backend.cost_baseline(session) == pytest.approx(baseline)
        assert caplog.records == []


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


def _reply_frame(
    kind: str,
    message_id: Optional[str],
    parent: Optional[str],
) -> dict[str, Any]:
    """Return one claude frame of a reply.

    ``kind`` names an assistant frame's content block (``thinking``/
    ``text``/``tool_use``; ``stop_reason`` always null), a raw API event
    (``start`` for ``message_start``, ``<block>_start`` for a
    ``content_block_start`` and ``<block>_stop`` for its
    ``content_block_stop``, ``stop`` for a ``max_tokens``
    ``message_delta``), or ``continuation`` for claude's injected turn
    (``unflagged_continuation`` without its ``isSynthetic`` flag,
    ``reasoning_continuation`` in another version's wording,
    ``meta_continuation`` in the transcript's plain-string ``isMeta`` shape).
    """
    if kind == 'continuation':
        return _continuation_frame(parent=parent)
    if kind == 'reasoning_continuation':
        frame = _continuation_frame(parent=parent)
        text = 'Output token limit hit while you were still reasoning.'
        frame['message']['content'][0]['text'] = text
        return frame
    if kind == 'meta_continuation':
        frame = _continuation_frame(parent=parent)
        del frame['isSynthetic']
        frame['isMeta'] = True
        frame['message']['content'] = frame['message']['content'][0]['text']
        return frame
    if kind == 'unflagged_continuation':
        frame = _continuation_frame(parent=parent)
        del frame['isSynthetic']
        return frame
    if kind == 'start':
        event = {'type': 'message_start', 'message': {'id': message_id, 'content': []}}
        return _stream_event(event, parent=parent)
    if kind == 'stop':
        delta = {'stop_reason': 'max_tokens', 'stop_sequence': None}
        return _stream_event({'type': 'message_delta', 'delta': delta}, parent=parent)
    if kind.endswith('_stop'):
        event = {'type': 'content_block_stop', 'index': 0}
        return _stream_event(event, parent=parent)
    if kind.endswith('_start'):
        block = {'type': kind.removesuffix('_start')}
        event = {'type': 'content_block_start', 'index': 0, 'content_block': block}
        return _stream_event(event, parent=parent)
    blocks = {
        'thinking': {'type': 'thinking', 'thinking': '', 'signature': 'CAQS'},
        'text': {'type': 'text', 'text': 'Rivers are'},
        'tool_use': {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Agent', 'input': {}},
    }
    return _assistant_frame(f'{message_id}', blocks[kind], parent=parent)


def _stream_event(event: dict[str, Any], *, parent: Optional[str]) -> dict[str, Any]:
    """Return a ``--include-partial-messages`` frame wrapping a raw API event."""
    return {
        'type': 'stream_event',
        'event': event,
        'session_id': 'sess-1',
        'parent_tool_use_id': parent,
    }


def _assistant_frame(
    message_id: str,
    block: dict[str, Any],
    *,
    parent: Optional[str] = None,
) -> dict[str, Any]:
    """Return a claude assistant frame carrying one content block."""
    return {
        'type': 'assistant',
        'message': {
            'model': 'claude-fable-5',
            'id': message_id,
            'type': 'message',
            'role': 'assistant',
            'content': [block],
            'stop_reason': None,
            'usage': {'input_tokens': 2, 'output_tokens': 4},
        },
        'parent_tool_use_id': parent,
        'session_id': 'sess-1',
    }


def _continuation_frame(*, parent: Optional[str] = None) -> dict[str, Any]:
    """Return claude's synthetic user turn continuing a reply cut at the cap."""
    text = (
        'Output token limit hit. Resume directly \u2014 no apology, no recap of'
        ' what you were doing. Pick up mid-thought if that is where the cut'
        ' happened. Break remaining work into smaller pieces.'
    )
    return {
        'type': 'user',
        'message': {'role': 'user', 'content': [{'type': 'text', 'text': text}]},
        'parent_tool_use_id': parent,
        'session_id': 'sess-1',
        'isSynthetic': True,
    }


def _transcript_line(
    message_id: str,
    usage: dict[str, Any],
    *,
    text: str = '',
    stop_reason: Optional[str] = 'end_turn',
    blocks: Optional[list[dict[str, Any]]] = None,
    stamp: str = _WRITTEN,
    model: str = 'claude-fable-5',
) -> str:
    """Return one claude transcript line of a reply, stamped inside the window.

    Written the way claude writes it: non-ASCII text raw, never escaped. A
    ``stop_reason`` of ``None`` is a line written before its reply
    completed; ``blocks`` replaces the line's one text block.
    """
    entry = {
        'type': 'assistant',
        'timestamp': stamp,
        'message': {
            'model': model,
            'id': message_id,
            'role': 'assistant',
            'content': blocks or [{'type': 'text', 'text': text}],
            'stop_reason': stop_reason,
            'usage': usage,
        },
    }
    return json.dumps(entry, ensure_ascii=False) + '\n'


def _cost_state(session: str, total: Any) -> dict[str, Any]:
    """Return the ``cost-state`` record claude saves to a transcript at exit.

    Real-shaped (claude 2.1.300): the session's running total, and the
    per-model usage behind it -- ``_USAGE_FIRST`` in the table's camel case.
    """
    return {
        'type': 'cost-state',
        'sessionId': session,
        'totalCostUSD': total,
        'totalAPIDuration': 5780,
        'totalAPIDurationWithoutRetries': 5779,
        'totalToolDuration': 227,
        'totalLinesAdded': 0,
        'totalLinesRemoved': 0,
        'totalDuration': 5972,
        'startTime': 1792304015631,
        'modelUsage': {
            'claude-fable-5': {
                'inputTokens': 100,
                'outputTokens': 200,
                'thinkingTokens': 150,
                'cacheReadInputTokens': 10_000,
                'cacheCreationInputTokens': 1000,
                'webSearchRequests': 0,
                'costUSD': total,
            },
        },
        'hasUnknownModelCost': False,
    }


def _tool_use(call: str, *, name: str = 'Bash') -> dict[str, Any]:
    """Return a reply's tool-call content block."""
    return {'type': 'tool_use', 'id': call, 'name': name, 'input': {}}


def _result_line(call: str, *, is_error: bool = False) -> str:
    """Return one claude transcript line of a user turn: one call's result."""
    result = {'type': 'tool_result', 'tool_use_id': call, 'content': ''}
    if is_error:
        result['is_error'] = True
    entry = {
        'type': 'user',
        'timestamp': _WRITTEN,
        'message': {'role': 'user', 'content': [result]},
    }
    return json.dumps(entry) + '\n'


def _settling(
    node: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    content: Optional[str],
) -> tuple[ClaudeAgent, int]:
    """Return a claude backend and a step whose session transcript holds ``content``.

    ``None`` writes no transcript.
    """
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude-home'))
    backend = ClaudeAgent(node, 'claude')
    session = str(uuid.uuid4())
    (step_id,) = _steps(node, 1)
    node.record.step_session('claude', step_id=step_id, model=None, session=session)
    if content is not None:
        transcript = pathlib.Path(backend.transcript(session)['path'])
        transcript.parent.mkdir(parents=True)
        transcript.write_text(content, encoding='utf-8')
    return backend, step_id


def _feed(parser: ClaudeParser, line: str) -> list[Any]:
    """Feed one line, dropping the idle-clock events the clock tests pin."""
    return [
        event for event in parser.feed(line) if event.kind not in ('acting', 'waiting')
    ]


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
