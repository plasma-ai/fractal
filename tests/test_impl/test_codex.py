"""Test the ``fractal.impl.codex`` module.

The codex dialect end to end: the ``exec --json`` protocol parsed into
normalized events, the ``exec`` argv builder, per-invocation pricing
over the OpenAI cached-subset usage shape, the ``config.toml`` model
default, the dated-rollout transcript layout, the account model
preflight, and the auth write-through and instructions-carry seeding.
Stream-level cases drive the base ``Agent.stream`` driver against a real
node ledger.

The rollout-pricing harness is a real offline process standing in for
codex: a python one-liner that appends captured rollout records under
the node's codex home and prints captured ``--json`` frames, so the
fresh and resumed runs travel the public ``Agent.stream`` path -- spawn
through the seam, drain, ``finish_stream`` -- against a real node
ledger. The rollouts are trimmed copies of real codex 0.154 sessions (a
fresh run resumed once; a run that spawned a sub-agent thread, with the
child's own rollout) with their message bodies cut and every record kind
and usage key kept.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import os
import pathlib
import re
import signal
import subprocess
import sys
import tomllib
import uuid
from collections.abc import Callable, Iterator
from typing import Any, Optional

import pytest

from fractal.cli.utils import StreamRenderer
from fractal.core import pricing
from fractal.core.agent import Invocation, StreamEvent, StreamResult
from fractal.core.node import Node
from fractal.exceptions import AgentStreamError
from fractal.impl import codex
from fractal.impl.codex import CodexAgent, CodexParser

from .rollouts import resumed_thread, spawned_thread, spawning_thread

__all__ = [
    'test_capability_flags_report_provider_facts_codex',
    'test_parser_maps_the_stream_protocol_codex',
    'test_parser_captures_the_thread_from_thread_started_only',
    'test_parser_never_prices_unbound_wire_totals',
    'test_parser_unpriced_model_records_no_cost_codex',
    'test_parser_surfaces_error_frames_codex',
    'test_parser_tolerates_garbage_codex',
    'test_parser_tolerates_present_null_payloads_codex',
    'test_events_render_through_the_production_renderer_codex',
    'test_renderer_closes_a_truncated_stream_with_the_placeholder_summary',
    'test_compute_cost_prices_the_cached_subset',
    'test_compute_cost_floors_uncached_at_zero',
    'test_compute_cost_tolerates_explicit_null_buckets_codex',
    'test_compute_cost_unpriced_model_returns_none_codex',
    'test_stream_without_process_records_session_but_not_wire_cost',
    'test_stream_detached_keeps_session_unpersisted_codex',
    'test_stream_fails_on_error_frames_codex',
    'test_stream_prices_each_invocation_from_its_own_rollout_records',
    'test_filtered_rollout_kinds_are_never_decoded',
    'test_unbound_or_incomplete_evidence_leaves_the_step_unpriced',
    'test_sub_agent_spawn_leaves_the_step_unpriced_until_the_next_turn',
    'test_nonzero_exit_closes_unpriced_and_silent',
    'test_stream_recovers_error_frames_inside_a_completed_turn',
    'test_recovered_error_frames_without_rollout_evidence_stay_unpriced',
    'test_unrecovered_error_frames_fail_the_step',
    'test_bare_stream_keeps_error_frames_fatal',
    'test_host_spawn_override_delegating_to_super_is_priced',
    'test_invocation_modes_build_the_pinned_argv_codex',
    'test_invocation_overlay_beats_a_colliding_ambient_var',
    'test_routed_invocation_splices_the_provider_table',
    'test_routed_preflight_demands_the_key_and_names_openrouter_causes',
    'test_rates_falls_back_through_the_openrouter_chain_codex',
    'test_invocation_refuses_fork',
    'test_config_model_reads_the_toml_top_level',
    'test_seed_config_disables_fast_mode_codex',
    'test_seed_config_disables_sub_agents_codex',
    'test_seed_links_auth_write_through_codex',
    'test_seed_carries_the_parent_instructions_file_codex',
    'test_seed_skips_uncarriable_instructions_codex',
    'test_transcript_globs_the_dated_rollouts',
    'test_preflight_probes_model_acceptance',
    'test_preflight_timeout_reaps_a_term_ignoring_probe',
    'test_preflight_timeout_never_kills_a_group_term_ended',
]

# a stand-in router: run a backend's spawns as a real `sh` body, logging each
# launch as its invocation and the live process
_Router = Callable[[CodexAgent, str], list[tuple[Invocation, subprocess.Popen]]]

# pricing with a distinct (cheaper) cache rate so an unfloored cached>input
# would go negative -- used by the cost-guard regression tests
_PRICING = {
    'o3': {
        'input_cost_per_token': 1e-6,
        'output_cost_per_token': 8e-6,
        'cache_read_input_token_cost': 1e-7,
    },
    'gpt-6-astra': {
        'input_cost_per_token': 1e-5,
        'output_cost_per_token': 5e-5,
        'cache_read_input_token_cost': 1e-6,
    },
}

# usage snapshots within one invocation (OpenAI convention: cached_input_tokens
# is a subset of input_tokens; reasoning is folded into output_tokens)
# and their hand-computed costs
_USAGE_FIRST = {
    'input_tokens': 100,
    'output_tokens': 10,
}
_USAGE_FIRST_COST = 100 * 1e-6 + 10 * 8e-6
_USAGE_SECOND = {
    'input_tokens': 300,
    'output_tokens': 30,
}
_USAGE_SECOND_COST = 300 * 1e-6 + 30 * 8e-6

# the captured session's thread and served model, with a distinct (cheaper)
# cache-read rate so the resumed run's cached reads price visibly
_SESSION = '01a0a77b-851d-72c3-af39-9cea6836708c'
_MODEL = 'gpt-6-astra'
_INPUT_RATE = 1e-5
_CACHED_RATE = 1e-6
_OUTPUT_RATE = 5e-5
_RATES = {
    'input_cost_per_token': _INPUT_RATE,
    'cache_read_input_token_cost': _CACHED_RATE,
    'output_cost_per_token': _OUTPUT_RATE,
}
# the captured --json stdout of the fresh run and of the resumed run: a
# resumed run's turn.completed total is the whole thread's, not its own
_FRESH_USAGE = {
    'input_tokens': 16_201,
    'cached_input_tokens': 0,
    'cache_write_input_tokens': 0,
    'output_tokens': 5,
    'reasoning_output_tokens': 0,
}
_CUMULATIVE_USAGE = {
    'input_tokens': 33_790,
    'cached_input_tokens': 16_000,
    'cache_write_input_tokens': 0,
    'output_tokens': 11,
    'reasoning_output_tokens': 0,
}
_FRESH_WIRE = [
    {'type': 'thread.started', 'thread_id': _SESSION},
    {'type': 'turn.started'},
    {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'ok'}},
    {'type': 'turn.completed', 'usage': _FRESH_USAGE},
]
_RESUMED_WIRE = [
    {'type': 'thread.started', 'thread_id': _SESSION},
    {'type': 'turn.started'},
    {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'ok again'}},
    {'type': 'turn.completed', 'usage': _CUMULATIVE_USAGE},
]
# the fresh run's frames by role, the error frame codex writes when it retries
# a stream error inside the turn, and the frame that fails the turn
_OPEN, _TURN, _TEXT, _DONE = _FRESH_WIRE
_RECONNECT = {'type': 'error', 'message': 'Reconnecting... 1/5'}
_TURN_FAILED = {'type': 'turn.failed', 'error': {'message': 'boom'}}
# the usage of the one record the resumed run appends to the rollout
_RESUMED_USAGE = {
    'input_tokens': 17_589,
    'cached_input_tokens': 16_000,
    'output_tokens': 6,
}
# the prices of the fresh run, of the resumed run's own record and of the
# cumulative total its stdout reports, at the fixture rates
_FRESH_COST = (
    (_FRESH_USAGE['input_tokens'] - _FRESH_USAGE['cached_input_tokens']) * _INPUT_RATE
    + _FRESH_USAGE['cached_input_tokens'] * _CACHED_RATE
    + _FRESH_USAGE['output_tokens'] * _OUTPUT_RATE
)
_RESUMED_COST = (
    (_RESUMED_USAGE['input_tokens'] - _RESUMED_USAGE['cached_input_tokens'])
    * _INPUT_RATE
    + _RESUMED_USAGE['cached_input_tokens'] * _CACHED_RATE
    + _RESUMED_USAGE['output_tokens'] * _OUTPUT_RATE
)
_CUMULATIVE_COST = (
    (_CUMULATIVE_USAGE['input_tokens'] - _CUMULATIVE_USAGE['cached_input_tokens'])
    * _INPUT_RATE
    + _CUMULATIVE_USAGE['cached_input_tokens'] * _CACHED_RATE
    + _CUMULATIVE_USAGE['output_tokens'] * _OUTPUT_RATE
)

# the captured session that spawned a sub-agent thread: its thread, its served
# model, and the --json stdout of its one run
_SPAWN_SESSION = '01a0ad03-a64b-7d23-884c-dd31547ddbea'
_SPAWN_MODEL = 'gpt-5.6-luna'
_SPAWN_USAGE = [
    record['payload']
    for record in spawning_thread
    if record['type'] == 'token_usage_record'
]
_SPAWN_WIRE = [
    {'type': 'thread.started', 'thread_id': _SPAWN_SESSION},
    {'type': 'turn.started'},
    {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'DONE'}},
    {'type': 'turn.completed', 'usage': _SPAWN_USAGE[-1]['thread_token_usage']},
]

# the offline stand-in for codex: appends the given rollout records under the
# node's codex home (or damages the file first), opens or grows a spawned
# thread's rollout beside it, prints the given stdout lines and exits as told
_WRITER = """\
import json, pathlib, sys
data = json.loads(sys.argv[1])
path = pathlib.Path(data['path'])
lines = [json.dumps(record) for record in data['records']]
if data.get('pad'):
    lines.insert(1, '{"type": "response_item", "payload": ' + 'x' * data['pad'] + '}')
if data.get('malformed'):
    lines.append('{"type": "token_usage_record", "payload": ')
raw = ''.join(line + '\\n' for line in lines)
if not data.get('missing'):
    path.parent.mkdir(parents=True, exist_ok=True)
    if data.get('rotate') and path.exists():
        path.rename(path.with_suffix('.old'))
        path.write_bytes(path.with_suffix('.old').read_bytes())
    if data.get('rewrite') and path.exists():
        path.write_bytes(path.read_bytes().replace(b'task_started', b'TASK_STARTED'))
    if data.get('truncate') and path.exists():
        path.write_bytes(b'')
    with path.open('a') as file:
        file.write(raw)
    if data.get('duplicate'):
        twin = path.parents[3] / '2026/09/16' / path.name
        twin.parent.mkdir(parents=True, exist_ok=True)
        twin.write_text(raw)
    if data.get('spawned'):
        child = pathlib.Path(data['spawned']['path'])
        rows = [json.dumps(record) for record in data['spawned']['records']]
        with child.open('a') as file:
            file.write(''.join(row + '\\n' for row in rows))
for frame in data['wire']:
    print(frame if isinstance(frame, str) else json.dumps(frame), flush=True)
sys.exit(data.get('exit_code', 0))
"""


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Router]:
    """Yield a router running a backend's spawns as a real ``sh``; reap them after.

    The stand-in keeps the production spawn shape (its own session, merged
    stderr, a text pipe) so the probe's wait and group tail run against a
    real process, and the returned log lets a test check the argv the
    backend would have launched.
    """
    spawned: list[subprocess.Popen] = []

    def route(
        backend: CodexAgent,
        script: str,
    ) -> list[tuple[Invocation, subprocess.Popen]]:
        probes: list[tuple[Invocation, subprocess.Popen]] = []

        def fake_spawn(invocation: Invocation, **kwargs: Any) -> subprocess.Popen:
            process = subprocess.Popen(
                ['sh', '-c', script],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                text=True,
                errors='replace',
                **kwargs,
            )
            probes.append((invocation, process))
            spawned.append(process)
            return process

        monkeypatch.setattr(backend, '_spawn', fake_spawn)
        return probes

    yield route
    # the stand-in led its own group: sweep whatever a probe left behind
    for process in spawned:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        process.stdout.close()


@pytest.fixture
def backend(node_with_db: Node, monkeypatch: pytest.MonkeyPatch) -> CodexAgent:
    """Return a codex backend on a real ledger over a frozen price table."""
    monkeypatch.setattr(pricing, '_load', lambda: {_MODEL: _RATES})
    return CodexAgent(node_with_db, 'codex')


def test_capability_flags_report_provider_facts_codex(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider facts consumers branch on, plus cost trackability."""
    backend = CodexAgent(node_with_db, 'codex')
    assert backend.name == 'codex'
    assert backend.config_file == 'config.toml'
    assert not backend.can_fork
    assert backend.mints_session
    assert backend.needs_pricing
    assert backend.cost_scope == 'call'
    assert not backend.enforces_budget
    # a token-priced agent tracks spend only with a priced model
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    assert backend.tracks_cost('o3')
    assert not backend.tracks_cost('mystery')
    assert not backend.tracks_cost()


def test_parser_maps_the_stream_protocol_codex() -> None:
    """One protocol implementation: thread, tools, messages, wall close."""
    parser = CodexParser()
    frames = [
        {'type': 'thread.started', 'thread_id': 'thr-1'},
        {
            'type': 'item.started',
            'item': {'type': 'command_execution', 'command': 'ls -la'},
        },
        {
            'type': 'item.completed',
            'item': {'type': 'agent_message', 'text': 'All done'},
        },
        {'type': 'turn.completed', 'usage': {}},
    ]
    events = [event for line in _lines(frames) for event in parser.feed(line)]
    events.extend(parser.finish())
    assert [event.kind for event in events] == ['session', 'tool', 'text', 'result']
    session, tool, text, result = events
    assert session.session == 'thr-1'
    assert tool.tool == 'ls -la'
    # codex sends whole messages, not deltas
    assert text.text == 'All done\n'
    # no per-turn cost rides the stream -- the result closes on wall time
    assert result.cost is None
    assert result.duration is not None
    assert result.duration >= 0.0
    assert parser.session == 'thr-1'


def test_parser_captures_the_thread_from_thread_started_only() -> None:
    """Only ``thread.started`` carries the resumable id; codex mints it."""
    parser = CodexParser()
    other = {
        'type': 'item.completed',
        'item': {'type': 'agent_message', 'text': 'hi'},
        'thread_id': 'thr-9',
    }
    events = parser.feed(json.dumps(other))
    assert [event.kind for event in events] == ['text']
    assert parser.session is None
    (event,) = parser.feed(json.dumps({'type': 'thread.started', 'thread_id': 'thr-1'}))
    assert event.kind == 'session'
    assert event.session == 'thr-1'
    assert parser.session == 'thr-1'


def test_parser_never_prices_unbound_wire_totals() -> None:
    """Exec totals stay diagnostic until a process-bound finalizer validates usage.

    The finalizer's close is the invocation's final frame either way.
    """
    parser = CodexParser(model='o3')
    usages = [{}, _USAGE_SECOND, {}, _USAGE_FIRST]
    events = [
        event
        for usage in usages
        for event in parser.feed(json.dumps({'type': 'turn.completed', 'usage': usage}))
    ]
    assert not events
    (result,) = parser.finish()
    assert result.kind == 'result'
    assert result.final
    assert result.cost is None
    assert parser.cost is None


def test_parser_unpriced_model_records_no_cost_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown/unpriced model emits no cost facts rather than crashing."""
    monkeypatch.setattr(pricing, '_load', lambda: {})
    parser = CodexParser(model='mystery')
    frames = [{'type': 'turn.completed', 'usage': _USAGE_FIRST}]
    events = [event for line in _lines(frames) for event in parser.feed(line)]
    events.extend(parser.finish())
    assert [event.kind for event in events] == ['result']
    assert parser.cost is None


@pytest.mark.parametrize(
    argnames=('frame', 'detail'),
    argvalues=[
        pytest.param(
            {'type': 'error', 'message': 'model not supported'},
            'model not supported',
            id='error-message',
        ),
        pytest.param(
            {'type': 'turn.failed', 'error': {'message': 'rate limited'}},
            'rate limited',
            id='failed-error-object',
        ),
        pytest.param(
            {'type': 'error', 'error': 'quota exhausted'},
            'quota exhausted',
            id='error-bare-string',
        ),
        pytest.param({'type': 'turn.failed'}, 'unknown error', id='failed-bare'),
    ],
)
def test_parser_surfaces_error_frames_codex(frame: dict[str, Any], detail: str) -> None:
    """Errors ride the JSON stream, not stderr, and collect to fail the step."""
    parser = CodexParser()
    (event,) = parser.feed(json.dumps(frame))
    assert event.kind == 'error'
    assert event.message == detail
    assert parser.errors == [detail]


def test_parser_tolerates_garbage_codex() -> None:
    """Malformed, non-object, and unknown lines yield nothing, never raise."""
    parser = CodexParser()
    junk = ['', '   ', 'not json', '[1, 2]', '"text"', '{}', '{"type": "mystery"}']
    assert [event for line in junk for event in parser.feed(line)] == []
    assert parser.session is None
    assert parser.cost is None
    # junk ahead of a complete turn leaves it whole for the finalizer to price
    frames = [
        {'type': 'thread.started', 'thread_id': 'thr-1'},
        {'type': 'turn.started'},
        {'type': 'turn.completed', 'usage': _USAGE_FIRST},
    ]
    for line in _lines(frames):
        parser.feed(line)
    assert parser.turn_usage() == _USAGE_FIRST


def test_parser_tolerates_present_null_payloads_codex() -> None:
    """A present-null nested field yields nothing, never raises (wire noise).

    ``event.get('item', {})`` returns ``None`` for ``{"item": null}`` -- a
    default only fills an absent key -- so the parser coerces with ``or {}``,
    keeping a malformed frame from crashing the live agent.
    """
    parser = CodexParser(model='o3')
    # item frames with a null payload resolve to nothing, never raise
    assert parser.feed('{"type": "item.started", "item": null}') == []
    assert parser.feed('{"type": "item.completed", "item": null}') == []
    # a null usage frame prices without dereferencing None
    parser.feed('{"type": "turn.completed", "usage": null}')
    # a null error payload still surfaces the fallback message
    (error,) = parser.feed('{"type": "error", "error": null}')
    assert error.kind == 'error'
    assert error.message == 'unknown error'


def test_events_render_through_the_production_renderer_codex(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Parsed events drive the CLI renderer: messages, errors, cost close."""
    parser = CodexParser()
    render = StreamRenderer()
    frames = [
        {'type': 'thread.started', 'thread_id': 'thr-1'},
        {
            'type': 'item.completed',
            'item': {'type': 'agent_message', 'text': 'Done.'},
        },
        {'type': 'error', 'message': 'rate limited'},
        {'type': 'turn.completed', 'usage': {}},
    ]
    for line in _lines(frames):
        for event in parser.feed(line):
            render(event)
    for event in parser.finish():
        render(event)
    captured = capsys.readouterr()
    assert 'Done.' in captured.out
    # the settled result is final: the close carries the wall time and the
    # cost fact -- '$?' when there is none, never $0
    assert re.search(r'— \d+\.\ds, \$\?', captured.out)
    assert 'agent error: rate limited' in captured.err


def test_renderer_closes_a_truncated_stream_with_the_placeholder_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stream killed before ``turn.completed`` still closes on ``— $?``.

    A truncated stream carries no result frame, so nothing prints the
    closing summary; ``close()`` -- called by the driving command after the
    drain -- settles the placeholder so the turn never ends unaccounted.
    """
    parser = CodexParser()
    render = StreamRenderer()
    frames = [
        {'type': 'thread.started', 'thread_id': 'thr-1'},
        {
            'type': 'item.completed',
            'item': {'type': 'agent_message', 'text': 'partial reply'},
        },
    ]
    for line in _lines(frames):
        for event in parser.feed(line):
            render(event)
    render.close()
    out = capsys.readouterr().out
    assert '— $?' in out


def test_compute_cost_prices_the_cached_subset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cached input is a subset of input; reasoning is already in output."""
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    usage = {
        'input_tokens': 1000,
        'cached_input_tokens': 200,
        'output_tokens': 50,
        'reasoning_output_tokens': 30,
    }
    cost = codex._compute_cost(usage, 'o3')
    # (1000-200)*1e-6 + 200*1e-7 + 50*8e-6 (output already includes reasoning)
    assert cost == pytest.approx(0.00122)


def test_compute_cost_floors_uncached_at_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed usage with cached > input must not yield a negative cost."""
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    cost = codex._compute_cost(
        usage={'input_tokens': 100, 'cached_input_tokens': 150, 'output_tokens': 0},
        model='o3',
    )
    assert cost is not None
    assert cost >= 0


def test_compute_cost_tolerates_explicit_null_buckets_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gateway may send a usage bucket as explicit null, not just absent.

    ``usage.get(key, 0.0)`` skips its default on a present-null key, so the
    coercion reads ``or 0.0`` -- else ``None * rate`` raises and kills the
    stream reader (the loop then SIGKILLs the agent group).
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    usage = {
        'input_tokens': 1000,
        'cached_input_tokens': None,
        'output_tokens': None,
    }
    cost = codex._compute_cost(usage, 'o3')
    # the null buckets coerce to 0, so only the uncached input is priced
    assert cost == pytest.approx(1000 * 1e-6)


def test_compute_cost_unpriced_model_returns_none_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown or rate-less model prices to ``None``, never $0."""
    monkeypatch.setattr(pricing, '_load', lambda: {'bare': {}})
    assert codex._compute_cost(_USAGE_FIRST, 'mystery') is None
    assert codex._compute_cost(_USAGE_FIRST, 'bare') is None
    assert codex._compute_cost(_USAGE_FIRST, None) is None


def test_stream_without_process_records_session_but_not_wire_cost(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver stamps the thread while unbound usage remains unknown."""
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    node = node_with_db
    backend = CodexAgent(node, 'codex')
    (step_id,) = _steps(node, 1)
    frames = [
        {'type': 'thread.started', 'thread_id': 'thr_abc'},
        {
            'type': 'item.completed',
            'item': {'type': 'agent_message', 'text': 'Done.'},
        },
        {'type': 'turn.completed', 'usage': _USAGE_SECOND},
    ]
    result = backend.stream(_lines(frames), step_id=step_id, model='o3')
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['agent'] == 'codex'
    assert row['session'] == 'thr_abc'
    assert row['model'] == 'o3'
    assert row['cost'] is None
    # the thread persists for the next continuous step, and rides the result
    assert node.sessions.get('codex') == 'thr_abc'
    assert result.session == 'thr_abc'
    assert result.cost is None


def test_stream_detached_keeps_session_unpersisted_codex(node_with_db: Node) -> None:
    """A detached turn stamps the step row but never persists ``.session``."""
    node = node_with_db
    backend = CodexAgent(node, 'codex')
    (step_id,) = _steps(node, 1)
    frames = [{'type': 'thread.started', 'thread_id': 'thr_x'}]
    backend.stream(_lines(frames), step_id=step_id, detached=True)
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['session'] == 'thr_x'
    assert node.sessions.get('codex') is None


def test_stream_fails_on_error_frames_codex(node_with_db: Node) -> None:
    """A stream-borne error fails the step even after a fully drained stdout."""
    backend = CodexAgent(node_with_db, 'codex')
    frames = [{'type': 'error', 'message': 'model not supported'}]
    with pytest.raises(
        RuntimeError,
        match='codex reported an error: model not supported',
    ):
        backend.stream(_lines(frames))


@pytest.mark.parametrize(
    argnames='configured',
    argvalues=[None, 'gpt-5-codex', _MODEL],
    ids=['unconfigured', 'other-pin', 'served-pin'],
)
def test_stream_prices_each_invocation_from_its_own_rollout_records(
    backend: CodexAgent,
    caplog: pytest.LogCaptureFixture,
    configured: Optional[str],
) -> None:
    """A step records its own tokens and the served model, never the stdout total."""
    fresh, resumed = _split(resumed_thread)
    # the fresh run prices the one turn its rollout records, through wire noise
    first = _named_step(backend, 'FIRST')
    wire = ['not json', '{"type": "item.started", "item": null}', *_FRESH_WIRE]
    command = _command(backend, fresh, wire)
    result, events = _drive(backend, command, step_id=first, model=configured)
    assert result.session == _SESSION
    assert result.model == _MODEL
    assert result.cost == pytest.approx(_FRESH_COST)
    # the resumed run prices only the record it appended (_RESUMED_USAGE)
    second = _named_step(backend, 'SECOND')
    command = _command(backend, resumed, _RESUMED_WIRE, resume=True)
    result, events = _drive(backend, command, step_id=second, model=configured)
    assert result.model == _MODEL
    assert result.cost == pytest.approx(_RESUMED_COST)
    assert result.cost != pytest.approx(_CUMULATIVE_COST)
    # the figure rides the post-drain result frame once, onto the step row
    assert [event.cost for event in events if event.cost is not None] == [
        pytest.approx(_RESUMED_COST)
    ]
    # the served model rides a session stamp once -- thread.started's when the
    # launch configured it, the finish frame's otherwise -- onto the step row
    stamps = [event.model for event in events if event.kind == 'session']
    assert stamps[-1] == _MODEL
    assert stamps.count(_MODEL) == 1
    rows = [
        backend.node.db.read('steps', where={'step_id': step})[0]
        for step in (first, second)
    ]
    assert [row['cost'] for row in rows] == pytest.approx([_FRESH_COST, _RESUMED_COST])
    assert [row['model'] for row in rows] == [_MODEL, _MODEL]
    assert not [event for event in events if event.kind == 'error']
    assert 'unpriced' not in caplog.text


def test_filtered_rollout_kinds_are_never_decoded(backend: CodexAgent) -> None:
    """A window prices past an undecodable line of a kind it does not read."""
    fresh, resumed = _split(resumed_thread)
    # the padding is a multi-megabyte response_item line whose body is not JSON
    pad = 2 << 20
    first = _named_step(backend, 'FIRST')
    command = _command(backend, fresh, _FRESH_WIRE, pad=pad)
    result, _ = _drive(backend, command, step_id=first)
    assert result.cost == pytest.approx(_FRESH_COST)
    second = _named_step(backend, 'SECOND')
    command = _command(backend, resumed, _RESUMED_WIRE, resume=True, pad=pad)
    result, _ = _drive(backend, command, step_id=second)
    assert result.cost == pytest.approx(_RESUMED_COST)


@pytest.mark.parametrize(
    argnames=('fault', 'diagnostic'),
    argvalues=[
        # the rollout cannot be found or bound to the process
        ('missing', 'No rollout names the thread'),
        ('preexisting', 'Rollout predates the spawn'),
        ('ambiguous', 'Several rollouts name the thread'),
        ('rotated', 'Resumed rollout was replaced'),
        ('truncated', 'Resumed rollout was truncated'),
        ('changed_prefix', 'Resumed rollout prefix changed'),
        # the window does not describe one complete, fully counted turn
        ('two_turns', 'exactly one completed turn'),
        ('aborted', 'interrupted turn'),
        ('foreign_session', 'belongs to another thread'),
        ('thread_counter', 'disagrees with the thread counter'),
        ('no_records', 'codex 0.153 or newer required'),
        ('no_context', 'exactly one served model'),
        ('two_models', 'exactly one served model'),
        # a record of a counted kind does not decode
        ('malformed_record', 'Expecting value'),
        # the served model has no rates
        ('no_rates', 'has no pricing entry'),
    ],
    ids=[
        'missing',
        'preexisting',
        'ambiguous',
        'rotated',
        'truncated',
        'changed-prefix',
        'two-turns',
        'aborted',
        'foreign-session',
        'thread-counter',
        'no-records',
        'no-context',
        'two-models',
        'malformed-record',
        'no-rates',
    ],
)
def test_unbound_or_incomplete_evidence_leaves_the_step_unpriced(
    backend: CodexAgent,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fault: str,
    diagnostic: str,
) -> None:
    """Evidence that cannot be bound to the process records NULL and logs why."""
    fresh, resumed = _split(resumed_thread)
    records, wire, options = copy.deepcopy(fresh), _FRESH_WIRE, {}
    # a resume fault needs the fresh run's rollout in place first
    resume = fault in ('rotated', 'truncated', 'changed_prefix')
    if resume:
        _drive(backend, _command(backend, fresh, _FRESH_WIRE))
        records, wire = copy.deepcopy(resumed), _RESUMED_WIRE
    # each fault damages the evidence in one place: the file, the window, or the rates
    if fault == 'missing':
        options['missing'] = True
    elif fault == 'preexisting':
        _drive(backend, _command(backend, fresh, _FRESH_WIRE))
    elif fault == 'ambiguous':
        options['duplicate'] = True
    elif fault == 'rotated':
        options['rotate'] = True
    elif fault == 'truncated':
        options['truncate'] = True
    elif fault == 'changed_prefix':
        options['rewrite'] = True
    elif fault == 'two_turns':
        records.append(
            copy.deepcopy(records[_find(records, 'event_msg', subtype='task_started')])
        )
    elif fault == 'aborted':
        records.append({'type': 'event_msg', 'payload': {'type': 'turn_aborted'}})
    elif fault == 'foreign_session':
        usage = records[_find(records, 'token_usage_record')]['payload']
        usage['session_id'] = 'elsewhere'
    elif fault == 'thread_counter':
        usage = records[_find(records, 'token_usage_record')]['payload']
        usage['thread_token_usage']['input_tokens'] += 1
    elif fault == 'no_records':
        records.pop(_find(records, 'token_usage_record'))
    elif fault == 'no_context':
        records.pop(_find(records, 'turn_context'))
    elif fault == 'two_models':
        context = copy.deepcopy(records[_find(records, 'turn_context')])
        context['payload']['model'] = 'other-model'
        records.insert(_find(records, 'turn_context') + 1, context)
    elif fault == 'malformed_record':
        options['malformed'] = True
    elif fault == 'no_rates':
        monkeypatch.setattr(pricing, '_load', lambda: {})
    # the faulted run streams onto a fresh step row and leaves it unpriced
    step = _named_step(backend, 'UNPRICED')
    command = _command(backend, records, wire, resume=resume, **options)
    result, events = _drive(backend, command, step_id=step)
    assert result.cost is None
    assert backend.node.db.read('steps', where={'step_id': step})[0]['cost'] is None
    # the step completes: the reason is a warning on the agent's logger, never
    # an error frame
    assert not [event for event in events if event.kind == 'error']
    warnings = [
        record.message for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert warnings[0].startswith('codex usage unpriced: ')
    assert diagnostic in warnings[0]


def test_sub_agent_spawn_leaves_the_step_unpriced_until_the_next_turn(
    backend: CodexAgent,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spawning or driving a sub-agent thread records NULL; an untouched child prices.

    The spawning turn records NULL. The thread's next turn resumes beside
    the child's rollout, which predates it untouched, and prices its own
    turn alone; a turn that drives the child again grows that rollout past
    its captured length, and records NULL like the spawn did.
    """
    monkeypatch.setattr(pricing, '_load', lambda: {_SPAWN_MODEL: _RATES})
    # the run's own rollout is complete, but the child's opens beside it
    first = _named_step(backend, 'SPAWNING')
    command = _command(
        backend=backend,
        records=spawning_thread,
        wire=_SPAWN_WIRE,
        thread=_SPAWN_SESSION,
        spawned=spawned_thread,
    )
    result, events = _drive(backend, command, step_id=first, model=_SPAWN_MODEL)
    assert result.cost is None
    assert backend.node.db.read('steps', where={'step_id': first})[0]['cost'] is None
    assert not [event for event in events if event.kind == 'error']
    warnings = [
        record.message for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert warnings == [
        'codex usage unpriced:'
        ' Rollout spawned sub-agent threads (their usage is unpriced).'
    ]
    # the thread's next run resumes beside the child's rollout, which predates
    # it, and prices its own turn alone
    caplog.clear()
    second = _named_step(backend, 'RESUMED')
    turn = _second_turn(spawning_thread)
    command = _command(backend, turn, _SPAWN_WIRE, thread=_SPAWN_SESSION, resume=True)
    result, _ = _drive(backend, command, step_id=second, model=_SPAWN_MODEL)
    # the replayed turn's own usage is the whole captured thread's
    assert result.cost == pytest.approx(_price(_SPAWN_USAGE[-1]['thread_token_usage']))
    assert result.model == _SPAWN_MODEL
    assert 'unpriced' not in caplog.text
    # a later turn drives the child again: its rollout grows past the captured
    # length, and the parent's own complete turn is refused as partial
    caplog.clear()
    third = _named_step(backend, 'DRIVING')
    driving = _second_turn(spawning_thread, after=turn)
    grown = _second_turn(spawned_thread)
    command = _command(
        backend=backend,
        records=driving,
        wire=_SPAWN_WIRE,
        thread=_SPAWN_SESSION,
        spawned=grown,
        resume=True,
    )
    result, _ = _drive(backend, command, step_id=third, model=_SPAWN_MODEL)
    assert result.cost is None
    assert backend.node.db.read('steps', where={'step_id': third})[0]['cost'] is None
    warnings = [
        record.message for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert warnings == [
        'codex usage unpriced:'
        ' Rollout spawned sub-agent threads (their usage is unpriced).'
    ]


def test_nonzero_exit_closes_unpriced_and_silent(
    backend: CodexAgent,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A process that exits non-zero closes with no cost and no diagnostic."""
    fresh, _ = _split(resumed_thread)
    step = _named_step(backend, 'FAILED')
    command = _command(backend, fresh, _FRESH_WIRE, exit_code=3)
    result, events = _drive(backend, command, step_id=step)
    assert result.session == _SESSION
    assert result.cost is None
    assert backend.node.db.read('steps', where={'step_id': step})[0]['cost'] is None
    # the loop attributes the exit; the stream just closes on wall time
    assert [event.kind for event in events] == ['session', 'text', 'result']
    assert 'unpriced' not in caplog.text


def test_stream_recovers_error_frames_inside_a_completed_turn(
    backend: CodexAgent,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Retried errors behind a completed turn and exit 0 leave the step priced."""
    fresh, _ = _split(resumed_thread)
    wire = [
        _OPEN,
        _TURN,
        _RECONNECT,
        {'type': 'error', 'message': 'quota exhausted'},
        _TEXT,
        _DONE,
    ]
    step = _named_step(backend, 'RECOVERED')
    command = _command(backend, fresh, wire)
    result, events = _drive(backend, command, step_id=step)
    assert result.cost == pytest.approx(_FRESH_COST)
    row = backend.node.db.read('steps', where={'step_id': step})[0]
    assert row['cost'] == pytest.approx(_FRESH_COST)
    # each error frame rendered as it arrived; the terminal frame closes priced
    assert [event.kind for event in events] == [
        'session',
        'error',
        'error',
        'text',
        'result',
    ]
    assert [event.message for event in events if event.kind == 'error'] == [
        'Reconnecting... 1/5',
        'quota exhausted',
    ]
    assert 'unpriced' not in caplog.text


def test_recovered_error_frames_without_rollout_evidence_stay_unpriced(
    backend: CodexAgent,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Recovery settles the outcome only; the cost still needs the rollout."""
    fresh, _ = _split(resumed_thread)
    wire = [_OPEN, _TURN, _RECONNECT, _TEXT, _DONE]
    step = _named_step(backend, 'RECOVERED_UNPRICED')
    command = _command(backend, fresh, wire, missing=True)
    result, events = _drive(backend, command, step_id=step)
    assert result.cost is None
    assert backend.node.db.read('steps', where={'step_id': step})[0]['cost'] is None
    assert [event.kind for event in events] == ['session', 'error', 'text', 'result']
    assert 'codex usage unpriced: No rollout names the thread' in caplog.text


@pytest.mark.parametrize(
    argnames=('wire', 'exit_code', 'detail'),
    argvalues=[
        # a failed turn, even behind a completion and a clean exit
        ([_OPEN, _TURN, _TURN_FAILED, _TEXT, _DONE], 0, 'boom'),
        # a retried error mixed with a failed turn keeps every recorded error
        (
            [_OPEN, _TURN, _RECONNECT, _TURN_FAILED, _TEXT, _DONE],
            0,
            'Reconnecting... 1/5; boom',
        ),
        # an error before the turn opens or after it completes
        ([_OPEN, _RECONNECT, _TURN, _TEXT, _DONE], 0, 'Reconnecting... 1/5'),
        ([_OPEN, _TURN, _TEXT, _DONE, _RECONNECT], 0, 'Reconnecting... 1/5'),
        # a turn that never completes
        ([_OPEN, _TURN, _RECONNECT, _TEXT], 0, 'Reconnecting... 1/5'),
        # a retried error on a process that exits non-zero
        ([_OPEN, _TURN, _RECONNECT, _TEXT, _DONE], 3, 'Reconnecting... 1/5'),
    ],
    ids=[
        'turn-failed',
        'mixed-fatal',
        'before-turn',
        'after-turn',
        'incomplete-turn',
        'nonzero-exit',
    ],
)
def test_unrecovered_error_frames_fail_the_step(
    backend: CodexAgent,
    wire: list[dict[str, Any]],
    exit_code: int,
    detail: str,
) -> None:
    """Recovery needs the error inside the turn, the turn completed, and exit 0."""
    fresh, _ = _split(resumed_thread)
    step = _named_step(backend, 'FATAL')
    command = _command(backend, fresh, wire, exit_code=exit_code)
    with pytest.raises(AgentStreamError, match=re.escape(detail)):
        _drive(backend, command, step_id=step)
    assert backend.node.db.read('steps', where={'step_id': step})[0]['cost'] is None


def test_bare_stream_keeps_error_frames_fatal(backend: CodexAgent) -> None:
    """Without a process there is no exit status to observe, so nothing recovers."""
    wire = [_OPEN, _TURN, _RECONNECT, _TEXT, _DONE]
    with pytest.raises(AgentStreamError, match='Reconnecting'):
        backend.stream(_lines(wire))


def test_host_spawn_override_delegating_to_super_is_priced(
    backend: CodexAgent,
) -> None:
    """A host that redirects execution through super keeps the rollout pricing."""

    class _WrappingCodexAgent(CodexAgent):
        """A host backend launching codex behind a wrapper binary."""

        def _spawn(
            self: _WrappingCodexAgent,
            invocation: Invocation,
            **kwargs: Any,
        ) -> subprocess.Popen:
            """Prefix the argv with a wrapper, then delegate."""
            wrapped = dataclasses.replace(invocation, argv=('env', *invocation.argv))
            return super()._spawn(wrapped, **kwargs)

    fresh, _ = _split(resumed_thread)
    custom = _WrappingCodexAgent(backend.node, 'codex')
    result, events = _drive(custom, _command(custom, fresh, _FRESH_WIRE))
    assert result.cost == pytest.approx(_FRESH_COST)
    assert result.model == _MODEL
    assert not [event for event in events if event.kind == 'error']


def test_invocation_modes_build_the_pinned_argv_codex(node_with_db: Node) -> None:
    """Fresh/resume/model land their exact argv, under the node CODEX_HOME."""
    node = node_with_db
    backend = CodexAgent(node, 'codex')
    worktree = str(node.worktree)
    # fresh threads anchor the worktree with -C; codex mints the id itself
    fresh = backend.invocation('hi')
    assert fresh.argv == ('codex', 'exec', '-C', worktree, '--json', '--', 'hi')
    assert fresh.session is None
    # exec resume takes no -C -- the shell cwd carries the worktree
    resume = backend.invocation('hi', session='thr-7')
    assert resume.argv == ('codex', 'exec', 'resume', 'thr-7', '--json', '--', 'hi')
    assert resume.session == 'thr-7'
    # the model rides -m, and the prompt stays the final positional
    priced = backend.invocation('hi', model='gpt-5-codex')
    assert priced.argv == (
        'codex',
        'exec',
        '-C',
        worktree,
        '--json',
        '-m',
        'gpt-5-codex',
        '--',
        'hi',
    )
    # a dash-leading message is protected by the sentinel, not parsed as a flag
    dashed = backend.invocation('-1 on that idea')
    assert dashed.argv[-2:] == ('--', '-1 on that idea')
    # codex runs in the worktree over the FULL environment plus CODEX_HOME
    assert fresh.cwd == node.worktree
    assert fresh.env['CODEX_HOME'] == str(node.node_dir / '.codex')
    assert fresh.env['PATH'] == os.environ['PATH']


def test_invocation_overlay_beats_a_colliding_ambient_var(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller overlay wins over a colliding process-environment value.

    The reserved env carries only ``CODEX_HOME``, so ``invocation`` layers the
    overlay over ``os.environ`` without the ambient snapshot re-clobbering it:
    the loop's per-node ``PLANS_DIR`` must reach the agent, not the operator's
    shell-exported one.
    """
    backend = CodexAgent(node_with_db, 'codex')
    monkeypatch.setenv('PLANS_DIR', '/ambient/plans')
    overlaid = backend.invocation('hi', env={'PLANS_DIR': '/node/plans'})
    assert overlaid.env['PLANS_DIR'] == '/node/plans'
    assert overlaid.env['CODEX_HOME'] == str(node_with_db.node_dir / '.codex')


def test_routed_invocation_splices_the_provider_table(node_with_db: Node) -> None:
    """The openrouter route rides four -c overrides between --json and -m."""
    node = node_with_db
    backend = CodexAgent(node, 'codex', 'openrouter')
    table = (
        '-c',
        'model_provider="openrouter"',
        '-c',
        'model_providers.openrouter.name="OpenRouter"',
        '-c',
        'model_providers.openrouter.base_url="https://openrouter.ai/api/v1"',
        '-c',
        'model_providers.openrouter.env_key="OPENROUTER_API_KEY"',
    )
    # fresh threads splice the table right after --json, before the model
    fresh = backend.invocation('hi', model='openai/gpt-5.3-codex')
    marker = fresh.argv.index('--json')
    assert fresh.argv[marker + 1 : marker + 9] == table
    assert fresh.argv[marker + 9 : marker + 11] == ('-m', 'openai/gpt-5.3-codex')
    # resume keeps the route (the same -c set rides every launch)
    resume = backend.invocation('hi', session='thr-7')
    assert resume.argv[:4] == ('codex', 'exec', 'resume', 'thr-7')
    marker = resume.argv.index('--json')
    assert resume.argv[marker + 1 : marker + 9] == table
    # the native argv carries no provider table
    native = CodexAgent(node, 'codex').invocation('hi')
    assert '-c' not in native.argv


def test_routed_preflight_demands_the_key_and_names_openrouter_causes(
    node_with_db: Node,
    router: _Router,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The routed preflight fails fast keyless and swaps the cause list."""
    node = node_with_db
    # `sh` stands in for the codex binary so the PATH check passes
    backend = CodexAgent(node, 'sh', 'openrouter')
    # without the key the probe never spawns
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    with pytest.raises(RuntimeError, match='OPENROUTER_API_KEY is not set'):
        backend.preflight('openai/gpt-5.3-codex')
    # with the key, a rejecting probe relays openrouter causes, not codex login
    monkeypatch.setenv('OPENROUTER_API_KEY', 'sk-or-sentinel')
    router(backend, _script(lines=['401 unauthorized'], exit_code=1))
    with pytest.raises(RuntimeError, match='OpenRouter dashboard'):
        backend.preflight('openai/gpt-5.3-codex')


def test_rates_falls_back_through_the_openrouter_chain_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The alias chain: exact, openrouter/ prefix, then the bare model name."""
    monkeypatch.setattr(
        pricing,
        '_load',
        lambda: {
            'o3': _PRICING['o3'],
            'openrouter/openai/gpt-5.2-codex': {'input_cost_per_token': 2e-6},
            'gpt-5.3-codex': {'input_cost_per_token': 3e-6},
        },
    )
    # an exact hit never consults the fallbacks
    assert codex._rates('o3') == _PRICING['o3']
    # an openrouter slug resolves via the LiteLLM openrouter/ prefix
    assert codex._rates('openai/gpt-5.2-codex') == {'input_cost_per_token': 2e-6}
    # a prefix miss falls back to the author-stripped bare name
    assert codex._rates('openai/gpt-5.3-codex') == {'input_cost_per_token': 3e-6}
    # every miss returns None (unpriced), never a guessed entry
    assert codex._rates('mystery/model') is None


def test_invocation_refuses_fork(node_with_db: Node) -> None:
    """Fork raises the single upstream-cited refusal."""
    backend = CodexAgent(node_with_db, 'codex')
    with pytest.raises(NotImplementedError, match='codex cannot fork a session'):
        backend.invocation('hi', session='thr-7', fork=True)


def test_config_model_reads_the_toml_top_level(node_with_db: Node) -> None:
    """Only a real top-level model key names the default."""
    node = node_with_db
    backend = CodexAgent(node, 'codex')
    config = node.node_dir / '.codex' / 'config.toml'
    # no config file names no model
    assert backend.config_model() is None
    config.parent.mkdir()
    # a model key nested in a table is not the top-level default (the TOML
    # parse reads structure, so a line-anchored lookalike cannot leak out)
    config.write_text('[profiles.fast]\nmodel = "nested"\n', encoding='utf-8')
    assert backend.config_model() is None
    # a malformed config names no model
    config.write_text('model = not toml\n', encoding='utf-8')
    assert backend.config_model() is None
    # the top-level key wins
    config.write_text(
        'model = "gpt-5-codex"\n[profiles.fast]\nmodel = "nested"\n',
        encoding='utf-8',
    )
    assert backend.config_model() == 'gpt-5-codex'


def test_seed_config_disables_fast_mode_codex(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The packaged codex seed keeps fast mode off (aligned across agents)."""
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'global-home'))
    node_dir = tmp_path / 'node'
    (node_dir / 'skills').mkdir(parents=True)
    CodexAgent.seed(node_dir)
    config = tomllib.loads(
        (node_dir / '.codex' / 'config.toml').read_text(encoding='utf-8')
    )
    assert config['features']['fast_mode'] is False


def test_seed_config_disables_sub_agents_codex(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The packaged codex seed keeps both sub-agent features off (their spend is unpriced)."""
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'global-home'))
    node_dir = tmp_path / 'node'
    (node_dir / 'skills').mkdir(parents=True)
    CodexAgent.seed(node_dir)
    config = tomllib.loads(
        (node_dir / '.codex' / 'config.toml').read_text(encoding='utf-8')
    )
    assert config['features']['multi_agent'] is False
    assert config['features']['multi_agent_v2'] is False


def test_seed_links_auth_write_through_codex(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth stays global: the node links through to the canonical file.

    A parent node's ``CODEX_HOME`` carries an ``auth.json`` that is itself
    a symlink; the seed canonicalizes through the chain so the node's link
    never dangles when the intermediate node is reset or deleted, and a
    token refresh through the link updates the global file (the secret is
    never copied into the node).
    """
    # the real global home holds the credential; a parent node links to it
    real_home = tmp_path / 'real-home'
    real_home.mkdir()
    (real_home / 'auth.json').write_text('{"secret": 1}\n', encoding='utf-8')
    parent_home = tmp_path / 'parent-home'
    parent_home.mkdir()
    (parent_home / 'auth.json').symlink_to(real_home / 'auth.json')
    monkeypatch.setenv('CODEX_HOME', str(parent_home))
    node_dir = tmp_path / 'node'
    (node_dir / 'skills').mkdir(parents=True)
    CodexAgent.seed(node_dir)
    link = node_dir / '.codex' / 'auth.json'
    assert link.is_symlink()
    # the chain is canonicalized: the link targets the real file directly
    assert link.readlink() == (real_home / 'auth.json').resolve()
    # a token refresh writes through the link into the global file
    link.write_text('{"refreshed": true}\n', encoding='utf-8')
    assert (real_home / 'auth.json').read_text(encoding='utf-8') == (
        '{"refreshed": true}\n'
    )
    # a repeat seed never re-links or clobbers
    CodexAgent.seed(node_dir)
    assert link.readlink() == (real_home / 'auth.json').resolve()
    # a pre-auth seed (no credential written yet) still canonicalizes through
    # the chain, so the link never dangles once the user logs in and the
    # intermediate node is reset or deleted
    (real_home / 'auth.json').unlink()
    fresh_dir = tmp_path / 'fresh-node'
    (fresh_dir / 'skills').mkdir(parents=True)
    CodexAgent.seed(fresh_dir)
    target = (real_home / 'auth.json').resolve()
    assert (fresh_dir / '.codex' / 'auth.json').readlink() == target


def test_seed_carries_the_parent_instructions_file_codex(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative instructions file travels with the inherited config.

    Codex resolves a relative ``model_instructions_file`` against
    ``CODEX_HOME`` and fails the run when the file is missing, so the
    child's seed copies the file the parent's config names -- nested
    directories included -- and a repeat seed never clobbers the
    child's copy.
    """
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'global-home'))
    parent_dir = tmp_path / 'parent'
    (parent_dir / '.codex' / 'prompts').mkdir(parents=True)
    (parent_dir / '.codex' / 'config.toml').write_text(
        'model_instructions_file = "prompts/math.md"\n',
        encoding='utf-8',
    )
    (parent_dir / '.codex' / 'prompts' / 'math.md').write_text(
        'Solve carefully.\n',
        encoding='utf-8',
    )
    node_dir = tmp_path / 'node'
    (node_dir / 'skills').mkdir(parents=True)
    CodexAgent.seed(node_dir, parent_dir=parent_dir)
    copied = node_dir / '.codex' / 'prompts' / 'math.md'
    assert copied.read_text(encoding='utf-8') == 'Solve carefully.\n'
    # an existing file is never overwritten
    (parent_dir / '.codex' / 'prompts' / 'math.md').write_text(
        'Updated upstream.\n',
        encoding='utf-8',
    )
    CodexAgent.seed(node_dir, parent_dir=parent_dir)
    assert copied.read_text(encoding='utf-8') == 'Solve carefully.\n'


@pytest.mark.parametrize('case', ['absolute-path', 'missing-source', 'no-key'])
def test_seed_skips_uncarriable_instructions_codex(
    case: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a relative, present instructions file travels to the child.

    An absolute path resolves the same from every node, so nothing is
    copied; a relative path whose source is missing at the parent seeds
    nothing (the parent's own codex fails the same way); a config naming
    no instructions file seeds nothing new.
    """
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'global-home'))
    parent_dir = tmp_path / 'parent'
    (parent_dir / '.codex').mkdir(parents=True)
    # build the parent config for the case
    if case == 'absolute-path':
        shared = tmp_path / 'shared-instructions.md'
        shared.write_text('Shared instructions.\n', encoding='utf-8')
        config = f'model_instructions_file = "{shared}"\n'
    elif case == 'missing-source':
        config = 'model_instructions_file = "math_prompt.md"\n'
    else:
        config = 'model = "gpt-5-codex"\n'
    (parent_dir / '.codex' / 'config.toml').write_text(config, encoding='utf-8')
    node_dir = tmp_path / 'node'
    (node_dir / 'skills').mkdir(parents=True)
    CodexAgent.seed(node_dir, parent_dir=parent_dir)
    # the child's codex dir carries only the config, skills link, and auth
    seeded = {path.name for path in (node_dir / '.codex').iterdir()}
    assert seeded == {'config.toml', 'skills', 'auth.json'}


def test_transcript_globs_the_dated_rollouts(node_with_db: Node) -> None:
    """Rollouts date-nest under the node codex home; the newest match wins."""
    node = node_with_db
    backend = CodexAgent(node, 'codex')
    session = str(uuid.uuid4())
    sessions_dir = node.node_dir / '.codex' / 'sessions'
    old = (
        sessions_dir
        / '2026'
        / '07'
        / '10'
        / f'rollout-2026-07-10T09-00-00-{session}.jsonl'
    )
    old.parent.mkdir(parents=True)
    old.write_text('{"kind": "old"}\n', encoding='utf-8')
    new = (
        sessions_dir
        / '2026'
        / '07'
        / '11'
        / f'rollout-2026-07-11T10-00-00-{session}.jsonl'
    )
    new.parent.mkdir(parents=True)
    new.write_text('{"kind": "rollout"}\n', encoding='utf-8')
    found = backend.transcript(session)
    assert found == {
        'agent': 'codex',
        'session': session,
        'path': str(new),
        'exists': True,
        'content': '{"kind": "rollout"}\n',
    }
    # an absent thread resolves to no expected path (rollouts are discovered)
    absent = backend.transcript(str(uuid.uuid4()))
    assert absent['path'] is None
    assert absent['exists'] is False


def test_preflight_probes_model_acceptance(
    node_with_db: Node,
    router: _Router,
) -> None:
    """The bounded probe relays codex's own cause, and skips without a model."""
    # `sh` stands in for the codex binary so the PATH check passes
    backend = CodexAgent(node_with_db, 'sh')
    probes = router(backend, _script())
    # no explicit model: nothing can be rejected, so nothing spawns
    backend.preflight()
    assert probes == []
    # an accepted model probes once, through the standard invocation shape,
    # and the probe is reaped
    backend.preflight('gpt-5-codex')
    ((invocation, probe),) = probes
    assert 'exec' in invocation.argv
    assert invocation.argv[invocation.argv.index('-m') + 1] == 'gpt-5-codex'
    assert invocation.argv[-1] == 'reply with: ok'
    assert probe.returncode == 0, invocation.argv
    # a rejection relays codex's own message, leading with the short reason
    # the loop persists, then the neutral cause list
    script = _script(
        lines=['{"type": "error", "message": "model not supported"}'],
        exit_code=1,
    )
    router(backend, script)
    with pytest.raises(RuntimeError) as rejected:
        backend.preflight('o3')
    detail = str(rejected.value)
    reason, *_ = detail.split('\n')
    assert reason == "codex preflight failed for model 'o3'"
    assert 'model not supported' in detail
    assert 'expired/invalid auth' in detail


def test_preflight_timeout_reaps_a_term_ignoring_probe(
    node_with_db: Node,
    router: _Router,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that ignores TERM draws the KILL after the grace, group and all."""
    backend = CodexAgent(node_with_db, 'sh')
    # the -SIGKILL assertion below needs `trap '' TERM` installed before
    # TERM lands -- milliseconds after spawn when idle, so 1s is a wide
    # margin for scheduler lag under the parallel suite
    monkeypatch.setattr(codex, '_PREFLIGHT_TIMEOUT', 1)
    # the grace only has to run out: TERM is ignored, so the group never
    # goes away and the KILL follows regardless of its length
    monkeypatch.setattr(codex, '_PREFLIGHT_GRACE', 0.2)
    probes = router(backend, _script(hang=True, ignore_term=True))
    # a hung probe times out distinctly from a rejection
    with pytest.raises(RuntimeError, match='timed out'):
        backend.preflight('gpt-5-codex')
    # TERM was ignored, so the KILL after the grace ended the whole group
    ((_, probe),) = probes
    assert probe.returncode == -signal.SIGKILL, probe.pid
    with pytest.raises(ProcessLookupError):
        os.killpg(probe.pid, 0)


def test_preflight_timeout_never_kills_a_group_term_ended(
    node_with_db: Node,
    router: _Router,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe whose group TERM ends within the grace draws no KILL at all.

    The grace loop proves the group gone before the KILL, and a gone group's
    id may already lead an unrelated process, so nothing is signaled again.
    """
    backend = CodexAgent(node_with_db, 'sh')
    monkeypatch.setattr(codex, '_PREFLIGHT_TIMEOUT', 0.2)
    # the no-KILL assertion below needs TERM to end `sleep` inside the
    # grace -- milliseconds when idle, so 5s is a wide margin for
    # scheduler lag under the parallel suite
    monkeypatch.setattr(codex, '_PREFLIGHT_GRACE', 5)
    sent: list[int] = []
    killpg = os.killpg

    def record(pgid: int, sig: int) -> None:
        sent.append(sig)
        killpg(pgid, sig)

    monkeypatch.setattr(os, 'killpg', record)
    probes = router(backend, _script(hang=True))
    with pytest.raises(RuntimeError, match='timed out'):
        backend.preflight('gpt-5-codex')
    # TERM ended the group, so the grace loop broke off before any KILL
    ((_, probe),) = probes
    assert probe.returncode == -signal.SIGTERM, probe.pid
    assert signal.SIGTERM in sent
    assert signal.SIGKILL not in sent


# ------ helpers


def _script(
    *,
    lines: Optional[list[str]] = None,
    exit_code: int = 0,
    hang: bool = False,
    ignore_term: bool = False,
) -> str:
    """Build the shell body a stand-in probe runs.

    ``lines`` print one per line; ``hang`` then sleeps as the group leader
    instead of exiting (``ignore_term`` makes it ignore TERM so only KILL
    ends it).
    """
    parts = [f"printf '%s\\n' {_quote(line)}" for line in lines or []]
    if hang:
        if ignore_term:
            parts.append("trap '' TERM")
        parts.append('exec sleep 60')
    parts.append(f'exit {exit_code}')
    return '; '.join(parts)


def _quote(text: str) -> str:
    """Single-quote ``text`` for ``sh``."""
    return "'" + text.replace("'", "'\\''") + "'"


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


def _named_step(backend: CodexAgent, name: str) -> int:
    """Open a run/iteration chain carrying one step row named ``name``."""
    node = backend.node
    run = node.record.run_start()
    iteration = node.record.iter_start(run_id=run, iter=1)
    return node.record.step_start(run_id=run, iter_id=iteration, step=1, step_name=name)


def _split(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split the captured rollout into the fresh run's records and the resume's."""
    cut = _find(records, 'event_msg', subtype='task_complete') + 1
    return records[:cut], records[cut:]


def _second_turn(
    records: list[dict],
    *,
    after: Optional[list[dict]] = None,
) -> list[dict]:
    """Replay a captured thread's one turn as its next, its counter carried on.

    The counter carries on from the last one in ``after`` -- a turn replayed
    before this one, the captured records themselves by default.
    """
    source = records if after is None else after
    counters = [
        record['payload']['thread_token_usage']
        for record in source
        if record['type'] == 'token_usage_record'
    ]
    baseline = counters[-1]
    turn = copy.deepcopy(records[1:])
    for record in turn:
        if record['type'] != 'token_usage_record':
            continue
        thread = record['payload']['thread_token_usage']
        record['payload']['thread_token_usage'] = {
            key: count + baseline[key] for key, count in thread.items()
        }
    return turn


def _price(usage: dict[str, int]) -> float:
    """Price one usage counter at the fixture rates."""
    return (
        (usage['input_tokens'] - usage['cached_input_tokens']) * _INPUT_RATE
        + usage['cached_input_tokens'] * _CACHED_RATE
        + usage['output_tokens'] * _OUTPUT_RATE
    )


def _find(records: list[dict], kind: str, *, subtype: Optional[str] = None) -> int:
    """Return the index of the first record of ``kind`` (and payload ``subtype``)."""
    for index, record in enumerate(records):
        if record['type'] != kind:
            continue
        if (subtype is None) or (record['payload'].get('type') == subtype):
            return index
    raise LookupError(f'no {kind} record')


def _command(
    backend: CodexAgent,
    records: list[dict],
    wire: list[Any],
    *,
    thread: str = _SESSION,
    spawned: Optional[list[dict]] = None,
    resume: bool = False,
    **options: Any,
) -> Invocation:
    """Build the stand-in invocation appending ``records`` and printing ``wire``."""
    path = (
        backend.config_dir
        / 'sessions/2026/09/15'
        / f'rollout-2026-09-15T17-51-25-{thread}.jsonl'
    )
    data = {'path': str(path), 'records': records, 'wire': wire, **options}
    # a spawned thread's rollout lands beside its parent's, named by its own id
    # -- its opening metadata on the spawn, its counter rows on a later turn
    if spawned is not None:
        if spawned[0]['type'] == 'session_meta':
            child = spawned[0]['payload']['id']
        else:
            counter = spawned[_find(spawned, 'token_usage_record')]
            child = counter['payload']['thread_id']
        data['spawned'] = {
            'path': str(path.with_name(f'rollout-2026-09-15T17-51-29-{child}.jsonl')),
            'records': spawned,
        }
    session = thread if resume else None
    command = backend.invocation('offline fixture', session=session)
    argv = (sys.executable, '-c', _WRITER, json.dumps(data))
    return dataclasses.replace(command, argv=argv)


def _drive(
    backend: CodexAgent,
    command: Invocation,
    *,
    step_id: Optional[int] = None,
    model: Optional[str] = _MODEL,
) -> tuple[StreamResult, list[StreamEvent]]:
    """Spawn the stand-in through the seam and stream it to completion."""
    process = backend.spawn(command, stderr=subprocess.PIPE)
    events: list[StreamEvent] = []
    try:
        result = backend.stream(
            process.stdout,
            process=process,
            step_id=step_id,
            model=model,
            render=events.append,
        )
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        process.stderr.close()
    return result, events
