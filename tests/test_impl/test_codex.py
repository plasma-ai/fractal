"""Test the ``fractal.impl.codex`` module.

The codex dialect end to end: the ``exec --json`` protocol parsed into
normalized events, the ``exec`` argv builder, thread-cumulative pricing
over the OpenAI cached-subset usage shape, the ``config.toml`` model
default, the dated-rollout transcript layout, the account model
preflight, and the auth write-through and instructions-carry seeding.
Stream-level cases drive the base ``Agent.stream`` driver against a real
node ledger.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tomllib
import uuid
from typing import Any, Optional

import pytest

from fractal.cli.utils import StreamRenderer
from fractal.core import pricing
from fractal.core.node import Node
from fractal.impl import codex
from fractal.impl.codex import CodexAgent, CodexParser

__all__ = [
    'test_capability_flags_report_provider_facts_codex',
    'test_parser_maps_the_stream_protocol_codex',
    'test_parser_captures_the_thread_from_thread_started_only',
    'test_parser_keeps_the_cumulative_maximum',
    'test_parser_flushes_cost_per_turn',
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
    'test_stream_records_cost_model_and_session_codex',
    'test_stream_detached_keeps_session_unpersisted_codex',
    'test_stream_subtracts_prior_sibling_on_same_session',
    'test_stream_increment_never_negative',
    'test_stream_fails_on_error_frames_codex',
    'test_invocation_modes_build_the_pinned_argv_codex',
    'test_invocation_overlay_beats_a_colliding_ambient_var',
    'test_routed_invocation_splices_the_provider_table',
    'test_routed_preflight_demands_the_key_and_names_openrouter_causes',
    'test_rates_falls_back_through_the_openrouter_chain_codex',
    'test_invocation_refuses_fork',
    'test_config_model_reads_the_toml_top_level',
    'test_seed_config_disables_fast_mode_codex',
    'test_seed_links_auth_write_through_codex',
    'test_seed_carries_the_parent_instructions_file_codex',
    'test_seed_skips_uncarriable_instructions_codex',
    'test_transcript_globs_the_dated_rollouts',
    'test_preflight_probes_model_acceptance',
]

# pricing with a distinct (cheaper) cache rate so an unfloored cached>input
# would go negative -- used by the cost-guard regression tests
_PRICING = {
    'o3': {
        'input_cost_per_token': 1e-6,
        'output_cost_per_token': 8e-6,
        'cache_read_input_token_cost': 1e-7,
    },
}

# cumulative usage snapshots (OpenAI convention: cached_input_tokens is a
# subset of input_tokens; reasoning is folded into output_tokens) and their
# hand-computed costs
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
    assert backend.cost_scope == 'thread'
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


def test_parser_keeps_the_cumulative_maximum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero/empty or shrinking terminal usage never resets the total.

    Codex emits a zeroed ``turn.completed`` on some error/cancel paths;
    pricing it as $0 would reset the running total and drive the per-step
    delta negative -- and a zeroed FIRST turn must leave the cost NULL
    (unknowable), never record a known $0.
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    parser = CodexParser(model='o3')
    frames = [
        {'type': 'turn.completed', 'usage': {}},
        {'type': 'turn.completed', 'usage': _USAGE_SECOND},
        {'type': 'turn.completed', 'usage': {}},
        {'type': 'turn.completed', 'usage': _USAGE_FIRST},
    ]
    events = [event for line in _lines(frames) for event in parser.feed(line)]
    # the leading zeroed turn closes with no cost fact at all
    assert events[0].kind == 'result'
    assert events[0].cost is None
    costs = [event.cost for event in events if event.kind == 'cost']
    assert costs == [pytest.approx(_USAGE_SECOND_COST)]
    assert parser.cost == pytest.approx(_USAGE_SECOND_COST)


def test_parser_flushes_cost_per_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each growing turn emits its cumulative snapshot, never a sum.

    Same durability property as the claude per-event flush: if the stream
    reader dies by signal mid-stream, the last completed turn's increment
    must already be on the step row -- and summing the cumulative snapshots
    would over-bill.
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    parser = CodexParser(model='o3')
    frames = [
        {'type': 'turn.completed', 'usage': _USAGE_FIRST},
        {'type': 'turn.completed', 'usage': _USAGE_SECOND},
    ]
    events = [event for line in _lines(frames) for event in parser.feed(line)]
    costs = [event.cost for event in events if event.kind == 'cost']
    assert costs == [
        pytest.approx(_USAGE_FIRST_COST),
        pytest.approx(_USAGE_SECOND_COST),
    ]
    # each turn's result carries the recorded turn cost (the renderer's
    # '— $X.XXXX' close reads it off the event)
    assert [event.cost for event in events if event.kind == 'result'] == costs


def test_parser_unpriced_model_records_no_cost_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown/unpriced model emits no cost facts rather than crashing."""
    monkeypatch.setattr(pricing, '_load', lambda: {})
    parser = CodexParser(model='mystery')
    frames = [{'type': 'turn.completed', 'usage': _USAGE_FIRST}]
    events = [event for line in _lines(frames) for event in parser.feed(line)]
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
    captured = capsys.readouterr()
    assert 'Done.' in captured.out
    # the turn closes on the recorded turn cost alone -- '$?' when there is
    # no cost fact, never $0 and never the wall time
    assert '— $?' in captured.out
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


def test_stream_records_cost_model_and_session_codex(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver stamps the real thread id and settles the priced delta."""
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
    assert row['cost'] == pytest.approx(_USAGE_SECOND_COST)
    # the thread persists for the next continuous step, and rides the result
    assert node.sessions.get('codex') == 'thr_abc'
    assert result.session == 'thr_abc'
    assert result.cost == pytest.approx(_USAGE_SECOND_COST)


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


def test_stream_subtracts_prior_sibling_on_same_session(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuous step records cumulative minus prior thread siblings.

    Exercises the telescoping subtraction against the recorded prior
    sibling (the settle lives in the base ``record_cost``; the parser
    supplies the cumulative snapshots that drive it).
    """
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    node = node_with_db
    backend = CodexAgent(node, 'codex')
    prior_id, step_id = _steps(node, 2)
    node.record.step_session('codex', step_id=prior_id, model=None, session='t1')
    node.record.step_cost(step_id=prior_id, cost=_USAGE_FIRST_COST)
    frames = [
        {'type': 'thread.started', 'thread_id': 't1'},
        {'type': 'turn.completed', 'usage': _USAGE_SECOND},
    ]
    backend.stream(_lines(frames), step_id=step_id, model='o3')
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == pytest.approx(_USAGE_SECOND_COST - _USAGE_FIRST_COST)


def test_stream_increment_never_negative(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-step delta below zero (e.g. a mid-run price drop) clamps to $0."""
    monkeypatch.setattr(pricing, '_load', lambda: _PRICING)
    node = node_with_db
    backend = CodexAgent(node, 'codex')
    prior_id, step_id = _steps(node, 2)
    node.record.step_session('codex', step_id=prior_id, model=None, session='t1')
    node.record.step_cost(step_id=prior_id, cost=0.001)  # prior recorded high
    frames = [
        {'type': 'thread.started', 'thread_id': 't1'},
        {'type': 'turn.completed', 'usage': _USAGE_FIRST},
    ]
    backend.stream(_lines(frames), step_id=step_id, model='o3')
    row = node.db.read('steps', where={'step_id': step_id})[0]
    # cumulative 0.00018 < prior 0.001 -> clamped to 0, never written negative
    assert row['cost'] == 0.0


def test_stream_fails_on_error_frames_codex(node_with_db: Node) -> None:
    """A stream-borne error fails the step even after a fully drained stdout."""
    backend = CodexAgent(node_with_db, 'codex')
    frames = [{'type': 'error', 'message': 'model not supported'}]
    with pytest.raises(
        RuntimeError,
        match='codex reported an error: model not supported',
    ):
        backend.stream(_lines(frames))


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
    monkeypatch.setattr(
        backend,
        'spawn',
        lambda invocation, **kwargs: _MockProbe(
            output='401 unauthorized\n',
            returncode=1,
        ),
    )
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
        'model_instructions_file = "prompts/math.md"\n', encoding='utf-8'
    )
    (parent_dir / '.codex' / 'prompts' / 'math.md').write_text(
        'Solve carefully.\n', encoding='utf-8'
    )
    node_dir = tmp_path / 'node'
    (node_dir / 'skills').mkdir(parents=True)
    CodexAgent.seed(node_dir, parent_dir=parent_dir)
    copied = node_dir / '.codex' / 'prompts' / 'math.md'
    assert copied.read_text(encoding='utf-8') == 'Solve carefully.\n'
    # an existing file is never overwritten
    (parent_dir / '.codex' / 'prompts' / 'math.md').write_text(
        'Updated upstream.\n', encoding='utf-8'
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bounded probe relays codex's own cause, and skips without a model."""
    # `sh` stands in for the codex binary so the PATH check passes
    backend = CodexAgent(node_with_db, 'sh')
    probes: list[Any] = []
    probe = _MockProbe()

    def fake_spawn(invocation: Any, **kwargs: Any) -> _MockProbe:
        probes.append(invocation)
        return probe

    monkeypatch.setattr(backend, '_spawn', fake_spawn)
    # no explicit model: nothing can be rejected, so nothing spawns
    backend.preflight()
    assert probes == []
    # an accepted model probes once, through the standard invocation shape
    backend.preflight('gpt-5-codex')
    (invocation,) = probes
    assert 'exec' in invocation.argv
    assert invocation.argv[invocation.argv.index('-m') + 1] == 'gpt-5-codex'
    assert invocation.argv[-1] == 'reply with: ok'
    # a rejection relays codex's own message, leading with the short reason
    # the loop persists, then the neutral cause list
    probe = _MockProbe(
        output='{"type": "error", "message": "model not supported"}',
        returncode=1,
    )
    with pytest.raises(RuntimeError) as rejected:
        backend.preflight('o3')
    detail = str(rejected.value)
    reason, *_ = detail.split('\n')
    assert reason == "codex preflight failed for model 'o3'"
    assert 'model not supported' in detail
    assert 'expired/invalid auth' in detail
    # a hung probe times out distinctly from a rejection, and is reaped
    probe = _MockProbe(hang=True)
    with pytest.raises(RuntimeError, match='timed out'):
        backend.preflight('o3')
    assert probe.killed


# ------ helpers


class _MockProbe:
    """Stand-in for the preflight subprocess (canned output and exit code)."""

    def __init__(
        self: _MockProbe,
        output: str = '',
        returncode: int = 0,
        hang: bool = False,
    ) -> None:
        """Initialize ``_MockProbe``."""
        self._output = output
        self.returncode = returncode
        self._hang = hang
        self.killed = False

    def communicate(
        self: _MockProbe,
        timeout: Optional[float] = None,
    ) -> tuple[str, None]:
        """Return the canned output; a bounded wait on a hung probe raises."""
        # only the bounded wait hangs -- the post-kill reap returns
        if self._hang and timeout is not None:
            raise subprocess.TimeoutExpired(cmd='codex', timeout=timeout)
        return self._output, None

    def kill(self: _MockProbe) -> None:
        """Record the kill."""
        self.killed = True


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
