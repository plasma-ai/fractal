"""Test the ``fractal.core.agent`` module.

The seam contract: backends are resolved through the registry (extended
in-process by ``register`` or across process boundaries by the deployment
hook file), bound to a node, and driven through public verbs that own
validation, central session minting, the env-overlay merge, the cost
settle invariants, and the transcript security gate -- backends supply
only ``_``-hooks and capability facts.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
from typing import Any

import pytest

import fractal
from fractal.core import agent
from fractal.core.agent import (
    Agent,
    Invocation,
    StreamEvent,
    StreamParser,
    StreamResult,
)
from fractal.core.node import Node
from fractal.exceptions import AbstractMethodError, AgentStreamError

from ._agents import RoutedAgent, SampleAgent, TrackingAgent

__all__ = [
    'test_supported_defaults_to_the_shipped_backends',
    'test_register_and_resolve_an_injected_backend',
    'test_resolve_rejects_an_unknown_agent',
    'test_command_base_refuses_shell_quoting',
    'test_deployment_hook_file_overrides_a_default',
    'test_explicit_registration_beats_the_hook_file',
    'test_broken_hook_file_fails_every_resolve',
    'test_agent_binds_the_node_and_command',
    'test_mandatory_hooks_raise_abstract_method_errors',
    'test_absent_capabilities_signal_not_implemented',
    'test_provider_binds_the_effective_route_or_none',
    'test_spawn_verbs_validate_the_provider_route',
    'test_invocation_threads_effort_to_the_builder',
    'test_invocation_validates_capability_asks',
    'test_invocation_resolves_modes_and_mints_centrally',
    'test_invocation_merges_the_env_overlay',
    'test_invocation_pops_none_valued_env_keys',
    'test_invocation_scrubs_ambient_effort_vars',
    'test_stream_records_sessions_and_costs',
    'test_stream_neutralizes_lone_surrogates',
    'test_stream_detached_skips_the_session_map',
    'test_stream_fails_on_error_frames_after_draining',
    'test_stream_reports_a_budget_stop',
    'test_spawn_decodes_stdout_leniently',
    'test_record_cost_settles_thread_scope_cumulative_totals',
    'test_record_cost_clamps_a_negative_call_scope_figure',
    'test_transcript_validates_and_gates_the_fallback',
    'test_preflight_checks_the_binary',
    'test_seed_agents_tolerates_an_absent_package_seed',
    'test_seed_prefers_the_parent_config_and_never_overwrites',
    'test_seed_reset_wipes_the_agent_dir',
    'test_seed_falls_back_to_the_package_seed',
]

# a deployment hook file overriding the claude backend by base command
_HOOK_SOURCE = '''\
from fractal.core.agent import Agent

__all__ = ['CloudClaudeAgent']


class CloudClaudeAgent(Agent):
    """Deployment override registered under the claude base command."""

    name = 'claude'
'''


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Return an isolated backend registry (hook-file state reset for the test)."""
    isolated = dict(agent._AGENTS)
    monkeypatch.setattr(agent, '_AGENTS', isolated)
    monkeypatch.setattr(agent, '_EXPLICIT', set())
    monkeypatch.setattr(agent, '_LOADED', {})
    return isolated


def test_supported_defaults_to_the_shipped_backends() -> None:
    """The registry ships the five provider backends, registration order."""
    assert agent.supported() == ('claude', 'codex', 'grok', 'opencode', 'omp')


def test_register_and_resolve_an_injected_backend(
    registry: dict[str, Any],
) -> None:
    """A registered class resolves by base command and joins the set."""
    agent.register('sample', SampleAgent)
    assert agent.resolve('sample') is SampleAgent
    assert agent.supported() == ('claude', 'codex', 'grok', 'opencode', 'omp', 'sample')


def test_resolve_rejects_an_unknown_agent(registry: dict[str, Any]) -> None:
    """An unknown base command raises naming the supported set."""
    with pytest.raises(ValueError, match="Unsupported agent: 'ghost'"):
        agent.resolve('ghost')


@pytest.mark.parametrize(
    argnames='command',
    argvalues=[
        'claude --flag "two words"',
        "claude --flag 'two words'",
        'claude --flag two\\ words',
    ],
    ids=['double-quoted', 'single-quoted', 'backslash'],
)
def test_command_base_refuses_shell_quoting(command: str) -> None:
    """``command_base`` refuses quoting instead of mangling argv words.

    Commands split on whitespace with no shell interpretation, so quoting
    would silently produce garbage words deep inside the loop's pane --
    the boundary refuses it while a plain multi-word command resolves to
    its base word.
    """
    with pytest.raises(ValueError, match='split on whitespace'):
        agent.command_base(command)
    assert agent.command_base('claude --some-flag') == 'claude'


def test_deployment_hook_file_overrides_a_default(
    tmp_path: pathlib.Path,
    registry: dict[str, Any],
) -> None:
    """A tree's ``agents.py`` re-points a base command by ``name``."""
    hook = tmp_path / 'agents.py'
    hook.write_text(_HOOK_SOURCE, encoding='utf-8')
    resolved = agent.resolve('claude', root=tmp_path)
    assert resolved.__name__ == 'CloudClaudeAgent'
    # the sibling defaults are untouched
    assert agent.supported() == ('claude', 'codex', 'grok', 'opencode', 'omp')


def test_explicit_registration_beats_the_hook_file(
    tmp_path: pathlib.Path,
    registry: dict[str, Any],
) -> None:
    """A ``register`` claim survives the deployment hook file."""
    hook = tmp_path / 'agents.py'
    hook.write_text(_HOOK_SOURCE, encoding='utf-8')
    agent.register('claude', SampleAgent)
    resolved = agent.resolve('claude', root=tmp_path)
    assert resolved is SampleAgent


def test_broken_hook_file_fails_every_resolve(
    tmp_path: pathlib.Path,
    registry: dict[str, Any],
) -> None:
    """A hook file that raises fails each resolve, naming the file.

    The failure must stay sticky: a tree whose ``agents.py`` cannot load
    would otherwise resolve the default backends on every consult after
    the first, silently dropping its declared override.
    """
    hook = tmp_path / 'agents.py'
    hook.write_text('raise ValueError("broken hook")\n', encoding='utf-8')
    for _ in range(2):
        with pytest.raises(RuntimeError, match=r'agents\.py: broken hook'):
            agent.resolve('claude', root=tmp_path)


def test_agent_binds_the_node_and_command(node_with_db: Node) -> None:
    """The command defaults to the node's configured agent; paths derive."""
    configured = SampleAgent(node_with_db)
    assert configured.command == 'claude'
    explicit = SampleAgent(node_with_db, 'sample --flag')
    assert explicit.parts == ['sample', '--flag']
    assert explicit.config_dir == node_with_db.node_dir / '.sample'
    assert explicit.err_path == node_with_db.node_dir / 'sample.err'


def test_mandatory_hooks_raise_abstract_method_errors(
    node_with_db: Node,
) -> None:
    """The parser and invocation hooks demand a concrete implementation."""
    with pytest.raises(AbstractMethodError):
        StreamParser().feed('{}')
    backend = Agent(node_with_db, 'bare')
    with pytest.raises(AbstractMethodError):
        backend.invocation('hello')


def test_absent_capabilities_signal_not_implemented(node_with_db: Node) -> None:
    """A capability the backend lacks reports itself by name."""
    backend = Agent(node_with_db, 'bare')
    with pytest.raises(NotImplementedError, match='is not implemented in Agent'):
        backend.transcript('sess-1')


def test_provider_binds_the_effective_route_or_none(node_with_db: Node) -> None:
    """The route binds only what the caller threads; route-less stays None."""
    node = node_with_db
    # None means the vendor's own endpoint
    assert SampleAgent(node, 'sample').provider is None
    assert RoutedAgent(node, 'sample').provider is None
    # a threaded route binds on routed backends only -- an inherited
    # openrouter default must never pin a route on a route-less backend
    assert SampleAgent(node, 'sample', 'openrouter').provider is None
    assert RoutedAgent(node, 'sample', 'openrouter').provider == 'openrouter'
    # the accessor threads the node's effective configured route
    node.config.set('provider', 'openrouter')
    assert node.agent('claude').provider == 'openrouter'


def test_spawn_verbs_validate_the_provider_route(node_with_db: Node) -> None:
    """Both spawn verbs reject an unsupported route; None is always legal."""
    node = node_with_db
    backend = RoutedAgent(node, 'sample', 'bogus')
    # invocation raises the registry error shape
    with pytest.raises(ValueError, match="Unsupported provider: 'bogus'"):
        backend.invocation('hello')
    # preflight raises RuntimeError so the loop persists the abort reason
    with pytest.raises(RuntimeError, match="Unsupported provider: 'bogus'"):
        backend.preflight()
    # a supported route builds; a route-less backend ignores the key entirely
    assert RoutedAgent(node, 'sample', 'openrouter').invocation('hello').argv
    assert SampleAgent(node, 'sample', 'openrouter').invocation('hello').argv


def test_invocation_threads_effort_to_the_builder(node_with_db: Node) -> None:
    """The effort override passes through to the provider hook unvalidated."""
    backend = SampleAgent(node_with_db, 'sample')
    tuned = backend.invocation('hello', effort='xhigh')
    assert tuned.argv[-2:] == ('--effort', 'xhigh')


def test_invocation_validates_capability_asks(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public verb rejects asks the capability flags rule out."""
    backend = SampleAgent(node_with_db, 'sample')
    # fork needs a session to fork from
    with pytest.raises(ValueError, match='fork'):
        backend.invocation('hello', fork=True)
    # an enforcing agent accepts a budget
    priced = backend.invocation('hello', budget=2.5)
    assert priced.argv[-2:] == ('--budget', '2.5')
    # a non-enforcing agent refuses one
    monkeypatch.setattr(backend, 'enforces_budget', False)
    with pytest.raises(ValueError, match='budget'):
        backend.invocation('hello', budget=2.5)


def test_invocation_resolves_modes_and_mints_centrally(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh mints (detached steps included), resume and fork pass through."""
    backend = TrackingAgent(node_with_db, 'sample')
    # fresh: the caller owns minting -- a lowercase uuid rides the record
    fresh = backend.invocation('hello')
    assert fresh.argv[:3] == ('sample', 'fresh', 'hello')
    assert fresh.session is not None
    assert fresh.session == fresh.session.lower()
    minted = [
        event for event in backend.calls if type(event).__name__ == 'AgentSessionEvent'
    ]
    assert [event.session for event in minted] == [fresh.session]
    # resume: the supplied session passes through in place
    resume = backend.invocation('hello', session='sess-5')
    assert resume.argv[:2] == ('sample', 'resume')
    assert resume.session == 'sess-5'
    # fork: resolved from the flag, same session
    fork = backend.invocation('hello', session='sess-5', fork=True)
    assert fork.argv[:2] == ('sample', 'fork')
    # a self-minting agent receives no caller-minted id
    monkeypatch.setattr(backend, 'mints_session', True)
    unminted = backend.invocation('hello')
    assert unminted.session is None

    # the verb owns the ride-along invariant: a backend that omits the id
    # from the invocation it builds still returns one carrying it
    class ForgetfulAgent(SampleAgent):
        """Backend double whose builder drops the session id."""

        def _invocation(self: ForgetfulAgent, prompt: str, **kwargs: Any) -> Invocation:
            built = super()._invocation(prompt, **kwargs)
            return dataclasses.replace(built, session=None)

    stamped = ForgetfulAgent(node_with_db, 'sample').invocation('hello')
    assert stamped.session is not None


def test_invocation_merges_the_env_overlay(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The overlay merges over ``os.environ``; provider keys win."""
    backend = SampleAgent(node_with_db, 'sample')
    monkeypatch.setenv('AMBIENT', 'from-environ')
    overlay = {
        'AMBIENT': 'from-overlay',
        'EXTRA': 'extra',
        'SAMPLE_HOME': 'from-overlay',
    }
    merged = backend.invocation('hello', env=overlay)
    assert merged.env['AMBIENT'] == 'from-overlay'
    assert merged.env['EXTRA'] == 'extra'
    assert merged.env['SAMPLE_HOME'] == str(backend.config_dir)
    assert merged.env['PATH'] == os.environ['PATH']
    # without an overlay the verb still composes the full environment,
    # layering the provider's reserved keys over os.environ
    bare = backend.invocation('hello')
    assert bare.env['SAMPLE_HOME'] == str(backend.config_dir)
    assert bare.env['PATH'] == os.environ['PATH']


def test_invocation_pops_none_valued_env_keys(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``None``-valued provider key unsets the ambient key on compose."""

    # a backend pins a reserved key to None to scrub the inherited copy
    class ScrubbingAgent(SampleAgent):
        """Backend double whose builder pins an inherited key to None."""

        def _invocation(self: ScrubbingAgent, prompt: str, **kwargs: Any) -> Invocation:
            built = super()._invocation(prompt, **kwargs)
            scrubbed = {**(built.env or {}), 'AMBIENT': None}
            return dataclasses.replace(built, env=scrubbed)

    monkeypatch.setenv('AMBIENT', 'inherited')
    backend = ScrubbingAgent(node_with_db, 'sample')
    composed = backend.invocation('hello')
    # the None value pops the inherited key; the sibling keys still land
    assert 'AMBIENT' not in composed.env
    assert composed.env['SAMPLE_HOME'] == str(backend.config_dir)
    assert composed.env['PATH'] == os.environ['PATH']


def test_invocation_scrubs_ambient_effort_vars(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambient effort vars are unset on compose; the pin stays flag-only.

    An operator shell carrying ``CLAUDE_CODE_EFFORT_LEVEL`` would silently
    override every step's pinned effort inside the child session, and a
    stale ``CLAUDE_EFFORT`` (claude's own hook/Bash export) would
    masquerade as the child's -- the verb scrubs the ambient vars, so the
    only effort signal reaching the agent is ``_invocation``'s own flag.
    """
    monkeypatch.setenv('CLAUDE_EFFORT', 'xhigh')
    monkeypatch.setenv('CLAUDE_CODE_EFFORT_LEVEL', 'high')
    monkeypatch.setenv('CLAUDE_CODE_SUBAGENT_MODEL', 'best')
    monkeypatch.setenv('AMBIENT', 'inherited')
    backend = SampleAgent(node_with_db, 'sample')
    composed = backend.invocation('hello', effort='low')
    # no scrubbed var reaches the spawn env; the pins ride argv alone --
    # an ambient CLAUDE_CODE_SUBAGENT_MODEL would force every fan-out
    # sub-agent onto one model, explicit per-agent pins included
    assert 'CLAUDE_EFFORT' not in composed.env
    assert 'CLAUDE_CODE_EFFORT_LEVEL' not in composed.env
    assert 'CLAUDE_CODE_SUBAGENT_MODEL' not in composed.env
    assert composed.argv[-2:] == ('--effort', 'low')
    # a non-effort ambient key passes through untouched
    assert composed.env['AMBIENT'] == 'inherited'

    # the scrub forces composition even when the backend reserves no env
    # keys and no overlay rides along -- env=None would inherit the parent
    class BareEnvAgent(SampleAgent):
        """Backend double whose builder reserves no env keys."""

        def _invocation(self: BareEnvAgent, prompt: str, **kwargs: Any) -> Invocation:
            built = super()._invocation(prompt, **kwargs)
            return dataclasses.replace(built, env=None)

    bare = BareEnvAgent(node_with_db, 'sample').invocation('hello')
    assert bare.env is not None
    assert 'CLAUDE_EFFORT' not in bare.env
    assert 'CLAUDE_CODE_EFFORT_LEVEL' not in bare.env
    assert 'CLAUDE_CODE_SUBAGENT_MODEL' not in bare.env
    assert bare.env['AMBIENT'] == 'inherited'


def test_stream_records_sessions_and_costs(node_with_db: Node) -> None:
    """The driver stamps the session, flushes each figure, renders all."""
    node = node_with_db
    backend = TrackingAgent(node, 'sample')
    step_id = _step(node)
    frames = [
        {'kind': 'session', 'session': 'sess-1', 'model': 'opus-4.8'},
        {'kind': 'text', 'text': 'hello'},
        {'kind': 'tool', 'tool': 'Read'},
        {'kind': 'cost', 'cost': 0.25},
        {'kind': 'cost', 'cost': 0.40},
        {'kind': 'result', 'cost': 0.42, 'final': True, 'duration': 1.5},
    ]
    rendered: list[StreamEvent] = []
    result = backend.stream(
        _lines(frames),
        step_id=step_id,
        model='fallback-model',
        render=rendered.append,
    )
    # the step row carries the captured facts (stream-reported model wins)
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['agent'] == 'sample'
    assert row['session'] == 'sess-1'
    assert row['model'] == 'opus-4.8'
    assert row['cost'] == pytest.approx(0.42)
    # the continuous-session map holds the capture for the next step
    assert node.sessions.get('sample') == 'sess-1'
    # every parsed event reached the render callback in stream order
    assert [event.kind for event in rendered] == [frame['kind'] for frame in frames]
    # the outcome reads off the drained parser state
    assert result == StreamResult(
        session='sess-1',
        model='opus-4.8',
        cost=0.42,
        budget_stopped=False,
        models=('opus-4.8',),
    )
    # hooks fire in the documented order, one cost flush per figure
    names = [type(event).__name__ for event in backend.calls]
    assert names == [
        'AgentCallEvent',
        'AgentSessionEvent',
        'AgentSessionEvent',
        'AgentActionEvent',
        'AgentCostEvent',
        'AgentCostEvent',
        'AgentCostEvent',
        'AgentCallSuccessEvent',
    ]
    costs = [
        event.cost
        for event in backend.calls
        if type(event).__name__ == 'AgentCostEvent'
    ]
    assert costs == [pytest.approx(0.25), pytest.approx(0.40), pytest.approx(0.42)]


def test_stream_neutralizes_lone_surrogates(node_with_db: Node) -> None:
    """A lone surrogate in a decoded frame never crashes a utf-8 sink.

    ``json.loads`` accepts lone-surrogate escapes; the driver neutralizes them
    at the parse boundary so the SQLite session/model bind (and every other
    sink) never raises ``UnicodeEncodeError`` -- the step still records and the
    run survives an otherwise-cosmetic byte.
    """
    node = node_with_db
    backend = TrackingAgent(node, 'sample')
    step_id = _step(node)
    frames = [
        {'kind': 'session', 'session': 'sess-\udc80', 'model': 'opus-\udc80'},
        {'kind': 'text', 'text': 'hel\udc80lo'},
        {'kind': 'result', 'cost': 0.1, 'final': True, 'duration': 1.0},
    ]
    rendered: list[StreamEvent] = []
    # no UnicodeEncodeError despite the lone surrogates in the bound fields
    result = backend.stream(_lines(frames), step_id=step_id, render=rendered.append)
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert '\udc80' not in row['session']
    assert '\udc80' not in row['model']
    # the drained served-model record is sanitized too -- it feeds the drop
    # path's SQLite writes (row marks, model_drop events)
    assert result.models
    assert not any('\udc80' in model for model in result.models)
    text = next(event.text for event in rendered if event.kind == 'text')
    assert '\udc80' not in text


def test_stream_detached_skips_the_session_map(node_with_db: Node) -> None:
    """Detached suppresses only the map write, never the step-row stamp."""
    node = node_with_db
    backend = SampleAgent(node, 'sample')
    step_id = _step(node)
    frames = [{'kind': 'session', 'session': 'sess-7'}]
    result = backend.stream(_lines(frames), step_id=step_id, detached=True)
    # the step-row stamp always lands (after-the-fact resume + attribution)
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['session'] == 'sess-7'
    # the continuous-session map stays empty
    assert node.sessions.get('sample') is None
    # a stream carrying no cost fact leaves NULL, never $0
    assert row['cost'] is None
    assert result.session == 'sess-7'


def test_stream_fails_on_error_frames_after_draining(node_with_db: Node) -> None:
    """Stream-borne errors fail the turn only after a full drain."""
    node = node_with_db
    backend = TrackingAgent(node, 'sample')
    step_id = _step(node)
    frames = [
        {'kind': 'session', 'session': 'sess-3'},
        {'kind': 'error', 'message': 'boom\udc80'},
        {'kind': 'text', 'text': 'still drained'},
    ]
    rendered: list[StreamEvent] = []
    # the discriminable type lets the loop book it as an agent error, not a
    # fractal-side stream error
    with pytest.raises(
        AgentStreamError, match='sample reported an error: boom'
    ) as excinfo:
        backend.stream(_lines(frames), step_id=step_id, render=rendered.append)
    # the error detail is collected raw off the wire, so the message is
    # sanitized -- the loop folds it into the step-row failure detail (SQLite)
    assert '\udc80' not in str(excinfo.value)
    # the stream drains fully before failing
    assert [event.kind for event in rendered] == ['session', 'error', 'text']
    # the failure pairing closes the call
    names = [type(event).__name__ for event in backend.calls]
    assert 'AgentErrorEvent' in names
    assert names[-1] == 'AgentCallFailureEvent'
    # the session stamp landed before the failure
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['session'] == 'sess-3'


def test_stream_reports_a_budget_stop(node_with_db: Node) -> None:
    """A budget stop is a clean stop, never a failure."""
    backend = TrackingAgent(node_with_db, 'sample')
    frames = [
        {'kind': 'session', 'session': 'sess-4'},
        {'kind': 'result', 'budget_stopped': True, 'cost': 1.0, 'final': True},
    ]
    result = backend.stream(_lines(frames))
    assert result.budget_stopped
    names = [type(event).__name__ for event in backend.calls]
    assert 'AgentBudgetEvent' in names
    assert names[-1] == 'AgentCallSuccessEvent'


def test_spawn_decodes_stdout_leniently(node_with_db: Node) -> None:
    """``_spawn`` reads agent stdout with lenient decoding.

    A non-UTF-8 byte in agent output must decode to the replacement char,
    not crash the stream reader (the parser tolerates wire noise).
    """
    backend = Agent(node_with_db, 'bare')
    invocation = Invocation(
        agent='bare',
        argv=('bash', '-c', "printf '\\xff\\n'; echo done"),
        cwd=node_with_db.worktree,
    )
    process = backend._spawn(invocation)
    # strict decoding would raise UnicodeDecodeError on the \xff byte here,
    # and 'ignore' would drop it -- the replacement char proves 'replace'
    lines = list(process.stdout)
    process.wait(timeout=30)
    assert any('\ufffd' in line for line in lines)
    assert any('done' in line for line in lines)


def test_record_cost_settles_thread_scope_cumulative_totals(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thread-scope figures settle to deltas, clamped at zero."""
    node = node_with_db
    backend = TrackingAgent(node, 'sample')
    monkeypatch.setattr(backend, 'cost_scope', 'thread')
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    steps = [
        node.record.step_start(
            iter_id=iter_id,
            run_id=run_id,
            step=step,
            step_name='EXECUTE',
        )
        for step in (1, 2, 3)
    ]
    for step_id in steps:
        node.record.step_session(
            'sample',
            step_id=step_id,
            model=None,
            session='thread-1',
        )
    # cumulative totals settle to per-step deltas against prior siblings
    backend.record_cost(steps[0], 0.30)
    backend.record_cost(steps[1], 0.50)
    # a shrinking total clamps at zero instead of poisoning later deltas
    backend.record_cost(steps[2], 0.45)
    costs = {
        step_id: node.db.read('steps', where={'step_id': step_id})[0]['cost']
        for step_id in steps
    }
    assert costs == {
        steps[0]: pytest.approx(0.30),
        steps[1]: pytest.approx(0.20),
        steps[2]: 0.0,
    }
    # the hook and the event both see the settled figures
    settled = [event.cost for event in backend.calls]
    assert settled == [pytest.approx(0.30), pytest.approx(0.20), 0.0]
    # a step with no recorded session keeps the full figure (its own thread)
    lone_step = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=4,
        step_name='EXECUTE',
    )
    backend.record_cost(lone_step, 0.70)
    row = node.db.read('steps', where={'step_id': lone_step})[0]
    assert row['cost'] == pytest.approx(0.70)


def test_record_cost_clamps_a_negative_call_scope_figure(
    node_with_db: Node,
) -> None:
    """A negative wire figure never reduces booked spend (call scope).

    A provider accounting bug (a negative ``total_cost_usd``, a credit
    adjustment) reaches a call-scope ledger straight from the wire, so a
    negative figure clamps to zero instead of inflating remaining budget.
    """
    node = node_with_db
    backend = TrackingAgent(node, 'sample')
    assert backend.cost_scope == 'call'
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    step_id = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='EXECUTE',
    )
    backend.record_cost(step_id, -0.42)
    row = node.db.read('steps', where={'step_id': step_id})[0]
    assert row['cost'] == 0.0
    assert [event.cost for event in backend.calls] == [0.0]


def test_transcript_validates_and_gates_the_fallback(node_with_db: Node) -> None:
    """Ids validate at the boundary; fallback discovery needs ownership."""
    node = node_with_db
    backend = SampleAgent(node, 'sample')
    # anything but a bare id could escape the transcript directory
    with pytest.raises(ValueError, match='Invalid session id'):
        backend.transcript('../escape')
    # the expected layout resolves directly
    expected_dir = backend.config_dir / 'transcripts'
    expected_dir.mkdir(parents=True)
    (expected_dir / 'sess-1.jsonl').write_text('{"n": 1}\n', encoding='utf-8')
    found = backend.transcript('sess-1')
    assert found['exists']
    assert found['content'] == '{"n": 1}\n'
    # a poll racing the provider's write can catch a torn multi-byte tail --
    # the read decodes leniently instead of failing the fetch
    (expected_dir / 'sess-1.jsonl').write_bytes(b'{"n": 1}\n\xe2\x82')
    torn = backend.transcript('sess-1')
    assert torn['content'].startswith('{"n": 1}\n')
    # an expected-path miss still names the waitable path
    missing = backend.transcript('sess-9')
    assert missing == {
        'agent': 'sample',
        'session': 'sess-9',
        'path': str(expected_dir / 'sess-9.jsonl'),
        'exists': False,
        'content': '',
    }
    # fallback discovery is gated on the DB owning the session for this node
    archive = backend.config_dir / 'archive'
    archive.mkdir()
    (archive / 'sess-2.jsonl').write_text('{"n": 2}\n', encoding='utf-8')
    gated = backend.transcript('sess-2')
    assert not gated['exists']
    # once the session is recorded for this node, the fallback serves it
    step_id = _step(node)
    node.record.step_session('sample', step_id=step_id, model=None, session='sess-2')
    owned = backend.transcript('sess-2')
    assert owned['exists']
    assert owned['path'] == str(archive / 'sess-2.jsonl')


def test_preflight_checks_the_binary(node_with_db: Node) -> None:
    """The base preflight demands the base command on ``PATH``."""
    present = TrackingAgent(node_with_db, 'sh')
    present.preflight()
    assert [type(event).__name__ for event in present.calls] == ['AgentPreflightEvent']
    missing = SampleAgent(node_with_db, 'no-such-agent-binary')
    with pytest.raises(RuntimeError, match='is not installed'):
        missing.preflight()


def test_seed_agents_tolerates_an_absent_package_seed(
    tmp_path: pathlib.Path,
    registry: dict[str, Any],
) -> None:
    """A backend without a package seed still gets its dir and links."""
    registry.clear()
    agent.register('sample', SampleAgent)
    node_dir = _node_dir(tmp_path)
    agent.seed_agents(node_dir)
    # the agent dir and skills link land without a config file
    sample_dir = node_dir / '.sample'
    assert (sample_dir / 'skills').is_symlink()
    assert (sample_dir / 'skills').readlink() == pathlib.Path('../skills')
    assert not (sample_dir / 'sample.json').exists()
    # the neutral agents dir carries the same skills mount
    assert (node_dir / '.agents' / 'skills').is_symlink()


def test_seed_prefers_the_parent_config_and_never_overwrites(
    tmp_path: pathlib.Path,
) -> None:
    """Children inherit the parent's live config; reseeding never clobbers."""
    parent_dir = tmp_path / 'parent'
    (parent_dir / '.sample').mkdir(parents=True)
    parent_config = parent_dir / '.sample' / 'sample.json'
    parent_config.write_text('{"from": "parent"}\n', encoding='utf-8')
    node_dir = _node_dir(tmp_path)
    SampleAgent.seed(node_dir, parent_dir=parent_dir)
    config = node_dir / '.sample' / 'sample.json'
    assert config.read_text(encoding='utf-8') == '{"from": "parent"}\n'
    # an existing file is never overwritten
    parent_config.write_text('{"from": "updated"}\n', encoding='utf-8')
    SampleAgent.seed(node_dir, parent_dir=parent_dir)
    assert config.read_text(encoding='utf-8') == '{"from": "parent"}\n'


def test_seed_reset_wipes_the_agent_dir(tmp_path: pathlib.Path) -> None:
    """Reset recreates the dir, dropping strays and re-copying config."""
    parent_dir = tmp_path / 'parent'
    (parent_dir / '.sample').mkdir(parents=True)
    parent_config = parent_dir / '.sample' / 'sample.json'
    parent_config.write_text('{"from": "parent"}\n', encoding='utf-8')
    node_dir = _node_dir(tmp_path)
    SampleAgent.seed(node_dir, parent_dir=parent_dir)
    stray = node_dir / '.sample' / 'stray.txt'
    stray.write_text('stray\n', encoding='utf-8')
    parent_config.write_text('{"from": "updated"}\n', encoding='utf-8')
    SampleAgent.seed(node_dir, parent_dir=parent_dir, reset=True)
    assert not stray.exists()
    config = node_dir / '.sample' / 'sample.json'
    assert config.read_text(encoding='utf-8') == '{"from": "updated"}\n'
    assert (node_dir / '.sample' / 'skills').is_symlink()


def test_seed_falls_back_to_the_package_seed(tmp_path: pathlib.Path) -> None:
    """Without a parent copy, the packaged seed provides the config."""

    class PackageSeeded(SampleAgent):
        """Sample agent keyed to a shipped package seed."""

        name = 'claude'
        config_file = 'settings.json'

    node_dir = _node_dir(tmp_path)
    PackageSeeded.seed(node_dir)
    package_dir = pathlib.Path(fractal.__file__).parent
    packaged = package_dir / '_node' / 'config' / 'claude' / 'settings.json'
    config = node_dir / '.claude' / 'settings.json'
    assert config.read_text(encoding='utf-8') == packaged.read_text(encoding='utf-8')


# ------ helpers


def _lines(frames: list[dict[str, Any]]) -> list[str]:
    """Encode scripted frames as agent stdout lines."""
    return [json.dumps(frame) + '\n' for frame in frames]


def _node_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a node data dir with the seeded skills dir agent links target."""
    node_dir = tmp_path / 'node'
    (node_dir / 'skills').mkdir(parents=True)
    return node_dir


def _step(node: Node) -> int:
    """Create a run/iteration/step chain and return the step id."""
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    return node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='EXECUTE',
    )
