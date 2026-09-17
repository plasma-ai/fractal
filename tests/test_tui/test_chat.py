"""Test the ``fractal.tui.chat`` module.

The transport decision table is pure and runs on canned input; ``ChatTurn``
is exercised against real tiny subprocesses (python one-liners standing in
for an agent) through the registry's real claude backend -- the per-provider
wire protocols themselves are pinned in ``test_impl``. The
app-level tests pin the chat contract on the writable pair tree: every turn
spawns a real agent and writes nothing to any node database -- radio
included.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
from typing import Optional

import pytest
from textual.widgets import TextArea

import fractal.core.agent
from fractal.cli.utils import resolve_node
from fractal.core.agent import Invocation, StreamEvent, StreamParser
from fractal.core.node import Node
from fractal.impl.claude import ClaudeAgent
from fractal.impl.codex import CodexAgent
from fractal.impl.opencode import OpencodeAgent
from fractal.tui.app import ChatDone, FractalApp
from fractal.tui.chat import (
    ChatController,
    ChatEvent,
    ChatTurn,
    resolve_transport,
)
from fractal.tui.data import TuiData

from ._doubles import MockTurn

__all__ = [
    'test_resolve_transport_decision_table',
    'test_resolve_transport_consults_the_deployment_hook',
    'test_resolve_transport_survives_a_broken_deployment_hook',
    'test_chat_turn_streams_a_real_subprocess',
    'test_chat_turn_closes_error_results_with_detail',
    'test_chat_turn_summarizes_only_the_final_opencode_step',
    'test_chat_turn_codex_result_closes_the_turn_once',
    'test_chat_turn_surfaces_nonzero_exit_with_stderr_tail',
    'test_chat_turn_cancel_kills_without_error',
    'test_chat_turn_surfaces_launch_failure_as_an_error_event',
    'test_chat_turn_degrades_a_raising_parser',
    'test_chat_turn_closes_on_a_finalizer_result',
    'test_chat_turn_degrades_a_raising_finalizer',
    'test_chat_controller_owns_the_turn_lifecycle',
    'test_chat_controller_queues_sends_fifo',
    'test_chat_controller_clocks_the_watchdog',
    'test_second_send_queues_behind_the_in_flight_turn',
    'test_queued_send_dispatches_against_the_branch_it_was_typed_on',
    'test_interrupt_recalls_a_lone_queued_send_to_the_composer',
    'test_interrupt_dispatches_several_queued_sends_as_one_turn',
    'test_sessionless_chat_spawns_fresh_never_radio',
    'test_live_chat_writes_nothing',
    'test_stale_done_does_not_clear_the_new_turn',
    'test_broken_deployment_hook_errors_the_turn_not_the_app',
]


@pytest.mark.parametrize(
    argnames=('kwargs', 'kind', 'session', 'resume', 'warn'),
    argvalues=[
        pytest.param(
            {'session': 'mine01', 'own_chat': True},
            'resume',
            'mine01',
            True,
            False,
            id='own-chat-thread-resumes-in-place',
        ),
        pytest.param(
            {'session': 'abc123'},
            'fork',
            'abc123',
            False,
            False,
            id='explicit-claude-session-forks',
        ),
        pytest.param(
            {'agent': 'codex', 'live_session': 'thr001', 'session': 'thr001'},
            'fresh',
            None,
            False,
            True,
            id='codex-live-thread-falls-back-fresh-with-warning',
        ),
        pytest.param(
            {'agent': 'codex', 'live_session': 'thr001', 'session': 'old001'},
            'resume',
            'old001',
            True,
            False,
            id='codex-historical-thread-resumes-in-place',
        ),
        pytest.param(
            {'live_session': 'live01'},
            'fork',
            'live01',
            False,
            False,
            id='active-claude-forks-its-live-session',
        ),
        pytest.param(
            {},
            'fresh',
            None,
            False,
            False,
            id='active-claude-without-a-session-goes-fresh',
        ),
        pytest.param(
            {'agent': 'codex'},
            'fresh',
            None,
            False,
            False,
            id='active-codex-goes-fresh',
        ),
        pytest.param(
            {'detached': True},
            'fresh',
            None,
            False,
            False,
            id='active-detached-goes-fresh',
        ),
        pytest.param(
            {'status': 'completed'},
            'fresh',
            None,
            False,
            False,
            id='settled-node-gets-a-fresh-session',
        ),
        pytest.param(
            {'status': 'idle'},
            'fresh',
            None,
            False,
            False,
            id='idle-node-gets-a-fresh-session',
        ),
        pytest.param(
            {'agent': '   '},
            'fresh',
            None,
            False,
            False,
            id='whitespace-agent-reads-as-unconfigured',
        ),
    ],
)
def test_resolve_transport_decision_table(
    kwargs: dict,
    kind: str,
    session: Optional[str],
    resume: bool,
    warn: bool,
) -> None:
    """Each (state, selection) lands on its transport; chat is never offline."""
    base = {
        'agent': 'claude',
        'status': 'active',
        'detached': False,
        'live_session': None,
    }
    transport = resolve_transport(**{**base, **kwargs})
    assert (transport.kind, transport.session, transport.resume, transport.warn) == (
        kind,
        session,
        resume,
        warn,
    )
    # the kwargs hand Node.chat_command exactly the resolved session decision
    if session is None:
        assert transport.chat_kwargs == {}
    else:
        assert transport.chat_kwargs == {'session': session, 'resume': resume}


# a deployment hook file that fails to load (a sticky RuntimeError on resolve)
_BROKEN_HOOK_SOURCE = 'raise ValueError("broken hook")\n'

# a deployment hook file registering a forking backend under a new command
_HOOK_SOURCE = '''\
from fractal.impl.claude import ClaudeAgent

__all__ = ['CloudyAgent']


class CloudyAgent(ClaudeAgent):
    """A forking backend registered only by the deployment hook file."""

    name = 'cloudy'
'''


def test_resolve_transport_consults_the_deployment_hook(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hook-registered forking agent's live session forks, never fresh.

    The registry alone does not know hook-file agents, so the transport
    decision must resolve through the tree's data dir -- otherwise a forking
    agent registered only there reads as non-forking and its live session
    refuses the flagship pause-and-interrogate fork.
    """
    # isolate the registry and hook-file state from the process
    monkeypatch.setattr(fractal.core.agent, '_AGENTS', dict(fractal.core.agent._AGENTS))
    monkeypatch.setattr(fractal.core.agent, '_LOADED', {})
    (tmp_path / 'agents.py').write_text(_HOOK_SOURCE, encoding='utf-8')
    transport = resolve_transport(
        agent='cloudy',
        status='active',
        detached=False,
        live_session='live01',
        root=tmp_path,
    )
    assert (transport.kind, transport.session) == ('fork', 'live01')


def test_resolve_transport_survives_a_broken_deployment_hook(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hook file that cannot load degrades the decision, never raises.

    The hook's sticky ``RuntimeError`` meets every resolve, so the transport
    decision must absorb it -- the agent reads as non-forking and the turn
    goes fresh; the send boundary owns surfacing the failure itself.
    """
    # isolate the hook-file state from the process
    monkeypatch.setattr(fractal.core.agent, '_LOADED', {})
    (tmp_path / 'agents.py').write_text(_BROKEN_HOOK_SOURCE, encoding='utf-8')
    transport = resolve_transport(
        agent='claude',
        status='active',
        detached=False,
        live_session='live01',
        root=tmp_path,
    )
    assert (transport.kind, transport.session) == ('fresh', None)
    assert "claude can't fork" in transport.label


# ------ ChatTurn against real subprocesses


class _RaisingParser(StreamParser):
    """A parser whose ``feed`` raises on any line (the never-raise guard)."""

    def feed(self: _RaisingParser, line: str) -> list[StreamEvent]:
        """Raise on any line."""
        raise ValueError('unexpected shape')


class _RaisingAgent(ClaudeAgent):
    """A backend whose parser raises on any line."""

    __parser__ = _RaisingParser


class _FinishingAgent(ClaudeAgent):
    """A backend whose finalizer closes the drained stream with a settled result."""

    # the cost the settled result carries; None settles with no cost fact
    cost: Optional[float] = 0.5

    def _finish_stream(
        self: _FinishingAgent,
        parser: StreamParser,
        process: Optional[subprocess.Popen],
    ) -> list[StreamEvent]:
        """Close on a fixed result carrying the configured cost."""
        return [StreamEvent(kind='result', cost=self.cost, duration=2.0)]


class _RaisingFinalizerAgent(ClaudeAgent):
    """A backend whose finalizer raises once the stream drains."""

    def _finish_stream(
        self: _RaisingFinalizerAgent,
        parser: StreamParser,
        process: Optional[subprocess.Popen],
    ) -> list[StreamEvent]:
        """Raise after the drain."""
        raise ValueError('accounting storage unavailable')


def _agent(node_dir: pathlib.Path) -> ClaudeAgent:
    """A real claude backend bound to a throwaway node."""
    return ClaudeAgent(Node(node_dir), 'claude')


def _command(code: str) -> Invocation:
    """A python one-liner standing in for the agent binary."""
    return Invocation(
        agent='claude',
        argv=(sys.executable, '-c', code),
        cwd=pathlib.Path.cwd(),
        env=None,
    )


def test_chat_turn_streams_a_real_subprocess(tmp_path: pathlib.Path) -> None:
    """A clean stream yields session, deltas, and the closing summary."""
    code = (
        'import json\n'
        "print(json.dumps({'type': 'system', 'session_id': 's-123'}))\n"
        "print(json.dumps({'type': 'stream_event', 'event': {"
        "'type': 'content_block_delta',"
        " 'delta': {'type': 'text_delta', 'text': 'hi'}}}))\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success',"
        " 'num_turns': 1, 'duration_ms': 500, 'total_cost_usd': 0.0432}))\n"
    )
    events = list(ChatTurn(_command(code), _agent(tmp_path)).events())
    assert [event.kind for event in events] == ['session', 'text', 'meta']
    assert events[0].text == 's-123'
    assert events[1].text == 'hi'
    assert events[2].text == 'done · 1 turns · 0.5s · $0.04'


def test_chat_turn_closes_error_results_with_detail(tmp_path: pathlib.Path) -> None:
    """An error result yields the failure detail before the closing summary."""
    code = (
        'import json\n'
        "print(json.dumps({'type': 'result', 'subtype': 'error_during_execution',"
        " 'is_error': True, 'num_turns': 1, 'duration_ms': 100,"
        " 'result': 'boom'}))\n"
    )
    events = list(ChatTurn(_command(code), _agent(tmp_path)).events())
    assert [event.kind for event in events] == ['error', 'meta']
    assert events[0].text == 'boom'
    assert events[1].text == 'done · 1 turns · 0.1s · $?'


def test_chat_turn_summarizes_only_the_final_opencode_step(
    tmp_path: pathlib.Path,
) -> None:
    """OpenCode's mid-run step_finish frames add no closing line; the last does.

    OpencodeParser emits a result per step_finish (final only when the reason
    is ``stop``), so without the final gate a multi-tool turn would print a
    ``done`` line per step -- all but the last false.
    """
    code = (
        'import json\n'
        "print(json.dumps({'type': 'step_finish',"
        " 'part': {'cost': 0.01, 'reason': 'tool-calls'}}))\n"
        "print(json.dumps({'type': 'step_finish',"
        " 'part': {'cost': 0.01, 'reason': 'tool-calls'}}))\n"
        "print(json.dumps({'type': 'step_finish',"
        " 'part': {'cost': 0.02, 'reason': 'stop'}}))\n"
    )
    agent = OpencodeAgent(Node(tmp_path), 'opencode')
    events = list(ChatTurn(_command(code), agent).events())
    # exactly one closing line, from the final step -- not one per step_finish
    assert [event.kind for event in events].count('meta') == 1
    assert events[-1].kind == 'meta'


def test_chat_turn_codex_result_closes_the_turn_once(tmp_path: pathlib.Path) -> None:
    """Codex's terminal result closes the turn once, cleanly.

    Codex emits its result from the post-drain finish frame, marked final
    with its settled cost, so the close gate treats it as terminal -- the
    turn closes on its own result, not the kill/truncated-stream fallback,
    and renders exactly one closing line.
    """
    code = (
        'import json\n'
        "print(json.dumps({'type': 'thread.started', 'thread_id': 't1'}))\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {}}))\n"
    )
    events = list(
        ChatTurn(_command(code), CodexAgent(Node(tmp_path), 'codex')).events()
    )
    assert [event.kind for event in events].count('meta') == 1


def test_chat_turn_surfaces_nonzero_exit_with_stderr_tail(
    tmp_path: pathlib.Path,
) -> None:
    """A failed turn ends with the exit error (stderr tail) plus a meta close."""
    code = (
        'import sys, json\n'
        "print(json.dumps({'type': 'system', 'session_id': 's-1'}))\n"
        "sys.stderr.write('kaboom: missing credentials\\n')\n"
        'sys.exit(3)\n'
    )
    events = list(ChatTurn(_command(code), _agent(tmp_path)).events())
    assert [event.kind for event in events] == ['session', 'error', 'meta']
    assert events[1].text == 'claude exited 3: kaboom: missing credentials'
    assert events[2].text.startswith('done · ')


def test_chat_turn_cancel_kills_without_error(tmp_path: pathlib.Path) -> None:
    """Cancelling kills the process; the turn closes clean (no error event)."""
    code = (
        'import json, sys, time\n'
        "print(json.dumps({'type': 'system', 'session_id': 's-1'}), flush=True)\n"
        'time.sleep(30)\n'
    )
    turn = ChatTurn(_command(code), _agent(tmp_path))
    events = turn.events()
    first = next(events)
    assert first.kind == 'session'
    turn.cancel()
    rest = list(events)
    assert turn.cancelled
    assert [event.kind for event in rest] == ['meta']


def test_chat_turn_surfaces_launch_failure_as_an_error_event(
    tmp_path: pathlib.Path,
) -> None:
    """A missing agent binary becomes a terminal error event, not a raise."""
    command = Invocation(
        agent='claude',
        argv=('/nonexistent/agent-binary',),
        cwd=pathlib.Path.cwd(),
        env=None,
    )
    events = list(ChatTurn(command, _agent(tmp_path)).events())
    assert [event.kind for event in events] == ['error', 'meta']
    assert events[0].text.startswith('claude failed to launch: ')


def test_chat_turn_degrades_a_raising_parser(tmp_path: pathlib.Path) -> None:
    """A parser that raises on a line degrades to an error event, never a raise.

    ``events`` promises it never raises, so a line shape the parser doesn't
    expect (one that makes ``feed`` throw) must become a terminal error event
    rather than crashing the worker thread.
    """
    code = "print('one line')\n"
    agent = _RaisingAgent(Node(tmp_path), 'claude')
    events = list(ChatTurn(_command(code), agent).events())
    assert [event.kind for event in events] == ['error', 'meta']
    assert 'stream parse error: unexpected shape' in events[0].text


@pytest.mark.parametrize(
    argnames=('cost', 'close'),
    argvalues=[
        pytest.param(0.5, 'done · 2.0s · $0.50', id='priced'),
        pytest.param(None, 'done · 2.0s · $?', id='unpriced'),
    ],
)
def test_chat_turn_closes_on_a_finalizer_result(
    cost: Optional[float],
    close: str,
    tmp_path: pathlib.Path,
) -> None:
    """A result the finalizer emits after exit closes the turn once.

    A backend that prices after its process exits (codex) emits no result
    from ``feed``; the post-drain ``finish_stream`` frame is the turn's
    close, rendered exactly once on wall time and cost -- ``$?`` when the
    result settles with no cost fact, matching the CLI's final close.
    """
    code = "import json\nprint(json.dumps({'type': 'system', 'session_id': 's-1'}))\n"
    agent = _FinishingAgent(Node(tmp_path), 'claude')
    agent.cost = cost
    events = list(ChatTurn(_command(code), agent).events())
    assert [event.kind for event in events] == ['session', 'meta']
    assert events[1].text == close


def test_chat_turn_degrades_a_raising_finalizer(tmp_path: pathlib.Path) -> None:
    """A finalizer that raises degrades to an error event, never a raise."""
    code = "print('one line')\n"
    agent = _RaisingFinalizerAgent(Node(tmp_path), 'claude')
    events = list(ChatTurn(_command(code), agent).events())
    assert [event.kind for event in events] == ['error', 'meta']
    assert 'stream finalization error: accounting storage unavailable' in events[0].text


# ------ ChatController (framework-free turn state)


def test_chat_controller_owns_the_turn_lifecycle() -> None:
    """``begin`` supersedes (and kills) the prior turn; only the live stamp clears.

    The controller is the single owner of "one chat turn at a time": ``begin``
    cancels whatever is in flight, mints a fresh staleness stamp, and restarts
    the spinner; a stale ``finish`` is a no-op, so a superseded turn's late
    done can never orphan the live turn's subprocess.
    """
    chat = ChatController(now=lambda: 100.0)
    first = MockTurn([])
    first_id = chat.begin('main.alpha', first)
    assert (chat.turn, chat.turn_branch) == (first, 'main.alpha')
    assert chat.is_current(first_id)
    chat.spin()
    assert chat.spin_frame == 1
    # a second begin kills and supersedes the first turn (and its stamp)
    second = MockTurn([])
    second_id = chat.begin('main.beta', second)
    assert first.cancelled
    assert (chat.turn, chat.turn_branch) == (second, 'main.beta')
    assert not chat.is_current(first_id)
    assert chat.spin_frame == 0  # the spinner restarts with the turn
    # a stale finish is a no-op; the live finish clears the turn
    chat.finish(first_id)
    assert chat.turn is second
    chat.finish(second_id)
    assert (chat.turn, chat.turn_branch) == (None, '')
    # cancel is idempotent -- with or without a live turn
    chat.cancel()
    third = MockTurn([])
    chat.begin('main.alpha', third)
    chat.cancel()
    assert third.cancelled
    assert chat.turn is None


def test_chat_controller_queues_sends_fifo() -> None:
    """The queue is FIFO and branch-stamped; ``clear_queue`` drops every entry.

    Sends typed while a turn is in flight park on the controller in order, each
    carrying the branch and session it was typed against, and drain oldest
    first; an interrupt clears the lot and hands back what it dropped.
    """
    chat = ChatController(now=lambda: 0.0)
    chat.enqueue('main.alpha', 'first', None)
    chat.enqueue('main.beta', 'second', 'sess-b')
    assert chat.dequeue() == ('main.alpha', 'first', None)
    chat.enqueue('main.alpha', 'third', None)
    # the third parks behind the still-queued second (oldest first)
    assert chat.dequeue() == ('main.beta', 'second', 'sess-b')
    # clear_queue drains the rest and reports them; a drained queue yields None
    assert chat.clear_queue() == [('main.alpha', 'third', None)]
    assert chat.dequeue() is None
    assert chat.clear_queue() == []


def test_chat_controller_clocks_the_watchdog() -> None:
    """``idle`` measures the injected clock since ``begin``; ``touch`` re-arms it."""
    clock = {'at': 0.0}
    chat = ChatController(now=lambda: clock['at'])
    chat.begin('main.alpha', MockTurn([]))
    clock['at'] = 30.0
    assert chat.idle() == 30.0
    chat.touch()  # a delta arrived
    clock['at'] = 42.0
    assert chat.idle() == 12.0


# ------ the app-level chat contract (the writable pair tree)


async def test_sessionless_chat_spawns_fresh_never_radio(
    pair_tree: pathlib.Path,
) -> None:
    """An active node with no woven session gets a fresh turn, never radio."""
    # flip alpha active with no live session: the no-session-woven-yet window
    Node(pair_tree / '.worktrees' / 'main.alpha').status_set('active')
    events = [
        ChatEvent(kind='session', text='chat-sess-2'),
        ChatEvent(kind='text', text='on it'),
        ChatEvent(kind='meta', text='done · 1 turns · 0.1s · $0.01'),
    ]
    app = FractalApp(
        resolve_node(pair_tree),
        branch='main.alpha',
        turn_factory=lambda command, agent: MockTurn(events),
    )
    async with app.run_test(size=(150, 48)) as pilot:
        app.start_chat('prioritize the flaky test')
        for _ in range(100):  # the worker thread streams in the background
            await pilot.pause(0.05)
            if app.chat.turn is None:
                break
        convo = app.chat.transcript('main.alpha')
    assert convo[0] == ('you', 'prioritize the flaky test')
    # the transport went fresh (with its reason) and a real turn streamed
    metas = [text for who, text in convo if who == 'meta']
    assert any('fresh session (no live session yet)' in text for text in metas)
    assert [text for who, text in convo if who == 'auth'] == ['on it']
    # chat never becomes radio: no message row lands in any node database
    data = TuiData(resolve_node(pair_tree))
    data.refresh_worktrees()
    connection = data.connect()
    try:
        rows = data.rows(connection, 'SELECT * FROM messages')
    finally:
        connection.close()
    assert rows == []


async def test_live_chat_writes_nothing(pair_tree: pathlib.Path) -> None:
    """A streamed chat turn leaves every node database byte-identical.

    Cockpit chats are ephemeral observer conversations -- no sessions, costs,
    or messages are recorded anywhere.
    """
    events = [
        ChatEvent(kind='session', text='chat-sess-1'),
        ChatEvent(kind='text', text='Hello'),
        ChatEvent(kind='text', text=' world'),
        ChatEvent(kind='meta', text='done · 1 turns · 0.1s · $0.01'),
    ]
    app = FractalApp(
        resolve_node(pair_tree),
        branch='main.alpha',
        turn_factory=lambda command, agent: MockTurn(events),
    )
    async with app.run_test(size=(150, 48)) as pilot:
        before = _dump(app.data)
        app.start_chat('how is it going?')
        for _ in range(100):  # the worker thread streams in the background
            await pilot.pause(0.05)
            if app.chat.turn is None:
                break
        after = _dump(app.data)
        convo = app.chat.transcript('main.alpha')
    assert after == before
    # the stream really ran: deltas coalesced into one bubble, session captured
    assert [text for who, text in convo if who == 'auth'] == ['Hello world']
    assert convo[-1] == ('meta', 'done · 1 turns · 0.1s · $0.01')
    assert app.chat.session('main.alpha') == 'chat-sess-1'


async def test_stale_done_does_not_clear_the_new_turn(
    pair_tree: pathlib.Path,
) -> None:
    """A late done from a superseded turn must not clear the live one.

    Rapid re-sends on one node race a finished turn's queued ``ChatDone``
    against the next turn's spawn. The done is keyed to its turn, so the stale
    one is dropped on arrival -- the new turn keeps streaming and stays tracked
    (its subprocess never orphans), and only its own done clears it.
    """
    events = [
        ChatEvent(kind='session', text='chat-sess'),
        ChatEvent(kind='text', text='reply'),
        ChatEvent(kind='meta', text='done · 1 turns · 0.1s · $0.01'),
    ]
    app = FractalApp(
        resolve_node(pair_tree),
        branch='main.alpha',
        turn_factory=lambda command, agent: MockTurn(events, pause=0.05),
    )
    async with app.run_test(size=(150, 48)) as pilot:
        # a turn is in flight on the branch; a prior turn's done is still queued
        app.start_chat('second')
        live_turn = app.chat.turn
        # a done stamped before the live turn's mint (ids mint from 1, so 0
        # is never the live turn's) arrives late
        app.on_chat_done(ChatDone(0))
        assert app.chat.turn is live_turn  # the live turn is untouched
        assert app.query('.chatpending')  # its spinner is still pinned
        # the live turn finishes on its own and clears cleanly
        for _ in range(100):
            await pilot.pause(0.05)
            if app.chat.turn is None:
                break
        assert app.chat.turn is None
        assert not app.query('.chatpending')


async def test_second_send_queues_behind_the_in_flight_turn(
    pair_tree: pathlib.Path,
) -> None:
    """A send while a turn is in flight queues and dispatches on that turn's done.

    Two sends on one branch never race a single spinner into a duplicate-id
    crash: the second parks as a ``you`` line behind the first's turn, then
    dispatches on its own once the first finishes -- both prompts reach an
    agent, oldest first.
    """
    events = [
        ChatEvent(kind='session', text='chat-sess'),
        ChatEvent(kind='text', text='reply'),
        ChatEvent(kind='meta', text='done · 1 turns · 0.1s · $0.01'),
    ]
    app = FractalApp(
        resolve_node(pair_tree),
        branch='main.alpha',
        turn_factory=lambda command, agent: MockTurn(events, pause=0.05),
    )
    async with app.run_test(size=(150, 48)) as pilot:
        app.start_chat('first')
        first_turn = app.chat.turn
        assert first_turn is not None
        # a second send while the first streams parks in the queue, unraced
        app.start_chat('second')
        assert app.chat.turn is first_turn
        # both turns run to completion, second after first
        for _ in range(200):
            await pilot.pause(0.05)
            if app.chat.turn is None and not app.query('.chatpending'):
                break
        convo = app.chat.transcript('main.alpha')
    # both prompts posted, in order; each turn streamed its own reply bubble
    assert [text for who, text in convo if who == 'you'] == ['first', 'second']
    assert [text for who, text in convo if who == 'auth'] == ['reply', 'reply']
    assert app.chat.turn is None


async def test_queued_send_dispatches_against_the_branch_it_was_typed_on(
    pair_tree: pathlib.Path,
) -> None:
    """A queued send dispatches against its own branch after a re-scope away.

    The send is stamped with the branch it was typed on, so re-scoping the
    cockpit before the in-flight turn finishes cannot redirect it -- its reply
    lands in the branch it was addressed to, and the newly scoped branch stays
    untouched.
    """
    events = [
        ChatEvent(kind='session', text='chat-sess'),
        ChatEvent(kind='text', text='reply'),
        ChatEvent(kind='meta', text='done · 1 turns · 0.1s · $0.01'),
    ]
    app = FractalApp(
        resolve_node(pair_tree),
        branch='main.alpha',
        turn_factory=lambda command, agent: MockTurn(events, pause=0.05),
    )
    async with app.run_test(size=(150, 48)) as pilot:
        app.start_chat('first')
        app.start_chat('second')  # queued against main.alpha
        # re-scope away while the first turn is still in flight
        app.scope_to('main')
        for _ in range(200):
            await pilot.pause(0.05)
            if app.chat.turn is None:
                break
        alpha = app.chat.transcript('main.alpha')
        root = app.chat.transcript('main')
    # both queued sends reached main.alpha; the re-scoped branch got nothing
    assert [text for who, text in alpha if who == 'you'] == ['first', 'second']
    assert [text for who, text in alpha if who == 'auth'] == ['reply', 'reply']
    assert root == []


async def test_interrupt_recalls_a_lone_queued_send_to_the_composer(
    pair_tree: pathlib.Path,
) -> None:
    """``ctrl+g`` with one queued send hands it back to the composer.

    Interrupt abandons the streaming turn (a ``cancelled`` line lands); the
    single send queued behind it was never sent, so its ``you`` line leaves
    the transcript and its text lands in the body for an edit or a re-send.
    """
    events = [
        ChatEvent(kind='session', text='chat-sess'),
        ChatEvent(kind='text', text='reply'),
        ChatEvent(kind='meta', text='done · 1 turns · 0.1s · $0.01'),
    ]
    app = FractalApp(
        resolve_node(pair_tree),
        branch='main.alpha',
        turn_factory=lambda command, agent: MockTurn(events, pause=5.0),
    )
    async with app.run_test(size=(150, 48)) as pilot:
        app.start_chat('first')
        turn = app.chat.turn
        assert turn is not None
        app.start_chat('second')  # queued behind the slow first turn
        await pilot.pause()
        await pilot.press('ctrl+g')  # interrupt
        await pilot.pause()
        assert turn.cancelled  # the streaming subprocess was killed
        assert app.chat.turn is None
        assert app.chat.dequeue() is None  # the queue drained into the composer
        body = app.query_one('#m_body', TextArea)
        assert body.text == 'second'  # the recalled send, ready to edit
        convo = app.chat.transcript('main.alpha')
    # the recalled send left the transcript; the sent prompt and cancel stayed
    assert [text for who, text in convo if who == 'you'] == ['first']
    assert any(who == 'meta' and text == 'cancelled' for who, text in convo)


async def test_interrupt_dispatches_several_queued_sends_as_one_turn(
    pair_tree: pathlib.Path,
) -> None:
    """``ctrl+g`` with several queued sends fires them at once as one turn.

    Interrupt abandons the streaming turn, then the queued sends -- already
    reading in order as ``you`` lines -- combine oldest-first into a single
    prompt and dispatch immediately as the next turn.
    """
    events = [
        ChatEvent(kind='session', text='chat-sess'),
        ChatEvent(kind='text', text='reply'),
        ChatEvent(kind='meta', text='done · 1 turns · 0.1s · $0.01'),
    ]
    commands: list[Invocation] = []

    def factory(command: Invocation, agent: ClaudeAgent) -> MockTurn:
        commands.append(command)
        return MockTurn(events, pause=5.0)

    app = FractalApp(
        resolve_node(pair_tree),
        branch='main.alpha',
        turn_factory=factory,
    )
    async with app.run_test(size=(150, 48)) as pilot:
        app.start_chat('first')
        first_turn = app.chat.turn
        assert first_turn is not None
        app.start_chat('second')
        app.start_chat('third')
        await pilot.pause()
        await pilot.press('ctrl+g')  # interrupt
        await pilot.pause()
        assert first_turn.cancelled
        # the queued sends went out together as the new in-flight turn
        combined = app.chat.turn
        assert combined is not None
        assert combined is not first_turn
        assert app.chat.dequeue() is None
        convo = app.chat.transcript('main.alpha')
        assert [text for who, text in convo if who == 'you'] == [
            'first',
            'second',
            'third',
        ]
    # the combined turn's invocation carries both prompts, oldest first
    assert any('second\n\nthird' in part for part in commands[-1].argv)


async def test_broken_deployment_hook_errors_the_turn_not_the_app(
    pair_tree: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tree whose hook file cannot load errors the turn, never the cockpit.

    The sticky ``RuntimeError`` meets every resolve on the send path -- the
    woven-session lookup, the transport decision, and the backend bind -- so
    it must land in the transcript as an error bubble naming the hook file,
    not escape a Textual handler and crash the whole app.
    """
    # isolate the hook-file state from the process
    monkeypatch.setattr(fractal.core.agent, '_LOADED', {})
    # an active node routes the send through the woven-session lookup too
    Node(pair_tree / '.worktrees' / 'main.alpha').status_set('active')
    hook = pair_tree / '.fractal' / 'main' / 'agents.py'
    hook.write_text(_BROKEN_HOOK_SOURCE, encoding='utf-8')
    app = FractalApp(
        resolve_node(pair_tree),
        branch='main.alpha',
        turn_factory=lambda command, agent: MockTurn([]),
    )
    async with app.run_test(size=(150, 48)):
        app.start_chat('what changed?')
        convo = app.chat.transcript('main.alpha')
    assert convo[0] == ('you', 'what changed?')
    # the failure surfaces as an error bubble naming the file; no turn spawns
    who, text = convo[-1]
    assert who == 'error'
    assert 'agents.py' in text
    assert app.chat.turn is None


# ------ helpers


def _dump(data: TuiData) -> tuple[str, ...]:
    """A full logical dump of the central database (read-only connection)."""
    connection = data.connect()
    try:
        return tuple(connection.iterdump())
    finally:
        connection.close()
