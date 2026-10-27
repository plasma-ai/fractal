"""Tests for ``Node.chat`` validation, seeding, and streaming.

``chat`` is exercised with only the subprocess boundary mocked: a fake
``Popen`` captures the spawn and feeds a canned agent stream, while the real
backend parser and CLI renderer consume it. So the tests pin the observable
chat contract -- the guard matrix, the prompt seeding rules, and the session
id returned -- without spawning a real agent. The per-provider argv pins live
with the backends in ``test_impl``.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import threading
from typing import Any, Optional

import pytest

from fractal.cli.utils import StreamRenderer
from fractal.core.node import Node
from tests._helpers import _save_cost_state

__all__ = [
    'test_chat_streams_and_returns_the_captured_session',
    'test_chat_shows_a_continued_sessions_turn_its_own_cost',
    'test_chat_seeds_prompt_docs_per_fresh_fork_and_resume',
    'test_chat_guards_reject_invalid_requests',
    'test_chat_nonzero_exit_raises',
    'test_chat_reaps_a_blocked_writer_when_streaming_raises',
    'test_chat_applies_the_configured_effort',
    'test_chat_spawns_without_an_inherited_virtual_env',
]

# minimal agent streams carrying a session id for capture
_CLAUDE_STREAM = (
    json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'sess_new'})
    + '\n'
    + json.dumps(
        {'type': 'result', 'duration_ms': 1, 'total_cost_usd': 0.0, 'num_turns': 1}
    )
    + '\n'
)
_CODEX_STREAM = json.dumps({'type': 'thread.started', 'thread_id': 'thr_new'}) + '\n'

# one claude session's saved total before a turn, and the running total its
# result frame names after it: the turn's own spend is the growth
_SAVED_TOTAL = 0.5483
_RUNNING_TOTAL = 0.6520


@pytest.mark.parametrize(
    argnames=('agent', 'stream', 'expected'),
    argvalues=[
        pytest.param('claude', _CLAUDE_STREAM, 'sess_new', id='claude'),
        pytest.param('codex', _CODEX_STREAM, 'thr_new', id='codex'),
    ],
)
def test_chat_streams_and_returns_the_captured_session(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
    stream: str,
    expected: Optional[str],
) -> None:
    """A chat spawns the backend invocation and returns the stream's id.

    The fake ``Popen`` feeds the canned stream through the real backend
    parser and the real CLI renderer; the captured session id is the
    return value the ``session:`` echo prints.
    """
    node = node_with_db
    node.config.set('agent', agent)
    captured = _patch_popen(monkeypatch, stdout_text=stream)

    result = node.chat('hello', render=StreamRenderer())

    # the real backend invocation was spawned, its stdin carrying the prompt
    assert captured['argv'][0] == agent
    assert captured['stdin'].endswith('hello')
    # the resulting session id is captured from the stream
    assert result == expected


@pytest.mark.parametrize(
    argnames=('kwargs', 'record', 'cost'),
    argvalues=[
        # a thread continued in place
        pytest.param(
            {'session': 'past-1', 'resume': True},
            'saved',
            f'${_RUNNING_TOTAL - _SAVED_TOTAL:.4f}',
            id='resume',
        ),
        # a fork restores the source session's saved total too
        pytest.param(
            {'session': 'past-1'},
            'saved',
            f'${_RUNNING_TOTAL - _SAVED_TOTAL:.4f}',
            id='fork',
        ),
        pytest.param(
            {'current': True},
            'saved',
            f'${_RUNNING_TOTAL - _SAVED_TOTAL:.4f}',
            id='fork-current',
        ),
        # a fresh session carries nothing in, so its figure is its own
        pytest.param({}, 'saved', f'${_RUNNING_TOTAL:.4f}', id='fresh'),
        # a continued session whose transcript cannot be found has no total
        # to take off, so the figure is refused rather than shown whole
        pytest.param(
            {'session': 'past-1', 'resume': True},
            'missing',
            '$?',
            id='no-transcript',
        ),
        # a baseline read that raises refuses the figure, never the reply
        pytest.param(
            {'session': 'past-1', 'resume': True},
            'raises',
            '$?',
            id='baseline-raises',
        ),
    ],
)
def test_chat_shows_a_continued_sessions_turn_its_own_cost(
    node_with_db: Node,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    kwargs: dict,
    record: str,
    cost: str,
) -> None:
    """A chat turn continuing a Claude session shows its own spend.

    Claude Code saves a session's running total to its transcript as each
    launch exits, and a resume or a fork counts the result figure on from
    it. The reply's closing line shows the figure less that total, as the
    loop books a resumed step, never the session's running total. A total
    that cannot be read refuses the figure (``$?``), and a read that
    raises warns on stderr while the reply still streams.
    """
    node = node_with_db
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude-home'))
    if kwargs.get('current'):
        _activate(node, 'past-1')
    if record != 'missing':
        transcript = pathlib.Path(node.agent().transcript('past-1')['path'])
        _save_cost_state(transcript, 'past-1', _SAVED_TOTAL)
    if record == 'raises':

        def raise_reading(self: object, session: str) -> None:
            raise OSError('transcript store offline')

        monkeypatch.setattr(type(node.agent()), '_cost_baseline', raise_reading)
    stream = (
        json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'sess_new'})
        + '\n'
        + json.dumps(
            {
                'type': 'result',
                'duration_ms': 1,
                'total_cost_usd': _RUNNING_TOTAL,
                'num_turns': 1,
            }
        )
        + '\n'
    )
    _patch_popen(monkeypatch, stdout_text=stream)

    node.chat('what changed?', **kwargs, render=StreamRenderer())

    captured = capsys.readouterr()
    assert f'— 1 turns, 0.0s, {cost}' in captured.out
    warning = 'Warning: reading the session cost baseline failed'
    assert (warning in captured.err) is (record == 'raises')


@pytest.mark.parametrize(
    argnames=('kwargs', 'active', 'has_node', 'has_chat'),
    argvalues=[
        ({}, False, True, True),  # fresh -> NODE.md + CHAT.md
        ({'current': True}, True, False, True),  # fork live session -> CHAT.md
        ({'session': 'past-1'}, False, False, True),  # fork a given id -> CHAT.md
        ({'session': 'past-1', 'resume': True}, False, False, False),  # resume -> none
    ],
    ids=['fresh', 'fork-current', 'fork-session', 'resume'],
)
def test_chat_seeds_prompt_docs_per_fresh_fork_and_resume(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict,
    active: bool,
    has_node: bool,
    has_chat: bool,
) -> None:
    """Fresh chats seed NODE.md + CHAT.md; forks seed CHAT.md; resumes seed nothing."""
    node = node_with_db
    (node.node_dir / 'NODE.md').write_text('NODE_CHARTER_MARKER\n', encoding='utf-8')
    if active:
        _activate(node, 'live-1')
    captured = _patch_popen(monkeypatch)

    node.chat('the user question', **kwargs)

    prompt = captured['stdin']
    assert ('NODE_CHARTER_MARKER' in prompt) is has_node
    # 'Chat Mode' is the heading of the package CHAT.md (seeded from there now)
    assert ('Chat Mode' in prompt) is has_chat
    assert prompt.endswith('the user question')


@pytest.mark.parametrize(
    argnames=('setup', 'kwargs', 'match'),
    argvalues=[
        (None, {'resume': True}, '--resume requires --session'),
        ('live', {'session': 'live-1', 'resume': True}, 'loop session'),
        ('codex', {'session': 'x'}, 'codex cannot fork'),
        ('no-agent', {}, 'No agent configured'),
        (None, {'current': True, 'session': 'x'}, 'cannot be combined'),
        (None, {'current': True, 'resume': True}, 'cannot be combined'),
        (None, {'current': True}, 'no live session'),
        ('codex', {'current': True}, 'codex cannot fork'),
    ],
    ids=[
        'resume-without-session',
        'refuse-live',
        'codex-fork',
        'no-agent',
        'current-with-session',
        'current-with-resume',
        'current-no-live',
        'current-codex',
    ],
)
def test_chat_guards_reject_invalid_requests(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    setup: str,
    kwargs: dict,
    match: str,
) -> None:
    """Invalid fork/resume combinations raise ``ValueError`` before spawning."""
    node = node_with_db
    if setup == 'live':
        _activate(node, 'live-1')
    elif setup == 'codex':
        node.config.set('agent', 'codex')
    elif setup == 'no-agent':
        (node.node_dir / 'config.json').write_text(
            json.dumps({'project': '.'}),
            encoding='utf-8',
        )
    # the agent must never be spawned on the error paths
    captured = _patch_popen(monkeypatch)
    with pytest.raises(ValueError, match=match):
        node.chat('hi', **kwargs)
    assert 'argv' not in captured


def test_chat_nonzero_exit_raises(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero agent exit surfaces as a ``RuntimeError`` after streaming."""
    node = node_with_db
    _patch_popen(monkeypatch, returncode=1)
    with pytest.raises(RuntimeError, match='non-zero'):
        node.chat('hi')


def test_chat_reaps_a_blocked_writer_when_streaming_raises(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-drain stream raise kills the agent's group instead of deadlocking.

    The spawned agent leads its own session and never sees the terminal's
    signals, so when the stream consumer dies mid-drain (a parser fault, a
    Ctrl-C in the parent) the child keeps writing into the now-unread pipe,
    fills it, and blocks -- and an unguarded ``proc.wait()`` then waits
    forever. The group kill ahead of the wait reaps the blocked writer so
    the original failure surfaces promptly.

    The chat runs on a bounded daemon thread: a regression makes it block,
    and the ``join`` timeout fails this test fast instead of wedging the
    whole suite (the daemon thread cannot keep the interpreter alive).
    """
    node = node_with_db
    node.config.set('agent', 'claude')
    # a real child that overfills the ~64KB pipe and blocks mid-write --
    # never reaped, it cannot exit and wait() deadlocks
    blocker = [
        'python3',
        '-c',
        "import sys; sys.stdout.write('x' * 10_000_000); sys.stdout.flush()",
    ]
    real_popen = subprocess.Popen

    def fake_popen(argv: list, **kwargs: object) -> object:
        if argv and argv[0] == 'git':
            return real_popen(argv, **kwargs)
        # spawn exactly as chat does -- no start_new_session, so the child is
        # NOT a process-group leader; a killpg(pid) would miss it and only a
        # direct proc.kill() reaps the blocked writer
        return real_popen(blocker, stdout=subprocess.PIPE, text=True)

    monkeypatch.setattr(subprocess, 'Popen', fake_popen)

    def raise_mid_drain(self: object, stdout: object, **kwargs: object) -> None:
        raise ValueError('parser died mid-drain')

    backend_cls = type(node.agent())
    monkeypatch.setattr(backend_cls, 'stream', raise_mid_drain)

    captured: dict[str, Any] = {}

    def run_chat() -> None:
        try:
            node.chat('hi')
        except BaseException as error:
            captured['error'] = error

    worker = threading.Thread(target=run_chat, daemon=True)
    worker.start()
    worker.join(timeout=15)
    # a deadlock leaves the thread alive past the join -- fail fast, never hang
    assert not worker.is_alive(), 'chat deadlocked: the blocked writer was not reaped'
    # the original mid-drain fault surfaces, not swallowed by the reap
    assert isinstance(captured.get('error'), ValueError)
    assert 'parser died mid-drain' in str(captured['error'])


def test_chat_applies_the_configured_effort(node_with_db: Node) -> None:
    """A chat turn runs at the node's configured effort, like its loop steps.

    The loop threads the node-config effort into every invocation, so a chat
    turn must too -- otherwise chatting a node runs shallower than the node
    itself, with no indication why.
    """
    node = node_with_db
    node.config.set('agent', 'claude')
    node.config.set('effort', 'xhigh')
    argv = node.chat_command('hi').argv
    assert '--effort' in argv
    assert argv[argv.index('--effort') + 1] == 'xhigh'


def test_chat_spawns_without_an_inherited_virtual_env(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """A chat's agent never sees the launching shell's ``VIRTUAL_ENV``.

    The shell's venv belongs to its own project, never the node worktree,
    so the agent runs without it, like a loop step's; the venv's bin stays
    first on ``PATH``.
    """
    venv = tmp_path / 'repo' / '.venv'
    monkeypatch.setenv('VIRTUAL_ENV', f'{venv}')
    monkeypatch.setenv('PATH', f'{venv / "bin"}{os.pathsep}{os.environ["PATH"]}')
    captured = _patch_popen(monkeypatch)

    node_with_db.chat('hello')

    env = captured['env']
    seen = (env.get('VIRTUAL_ENV'), env['PATH'].split(os.pathsep)[0])
    assert seen == (None, f'{venv / "bin"}')


# ------ helpers


class _FakeProc:
    """Stand-in for ``subprocess.Popen`` with canned stdout and exit code."""

    def __init__(self: _FakeProc, stdout_text: str, returncode: int) -> None:
        """Initialize ``_FakeProc``."""
        self.stdout = io.StringIO(stdout_text)
        self.returncode = returncode

    def wait(self: _FakeProc) -> int:
        """Return the canned exit code."""
        return self.returncode


def _patch_popen(
    monkeypatch: pytest.MonkeyPatch,
    stdout_text: str = _CLAUDE_STREAM,
    returncode: int = 0,
) -> dict:
    """Patch ``Popen`` to capture the agent spawn and feed a canned stream.

    Node's internal ``git`` calls (via ``subprocess.run``) are delegated to the
    real ``Popen`` so only the agent invocation is faked.
    """
    captured: dict = {}
    real_popen = subprocess.Popen

    def fake_popen(argv: list, **kwargs: object) -> object:
        if argv and argv[0] == 'git':
            return real_popen(argv, **kwargs)
        captured['argv'] = list(argv)
        captured['cwd'] = kwargs.get('cwd')
        captured['env'] = kwargs.get('env')
        # read the prompt file here: the spawn closes it once Popen returns
        captured['stdin'] = kwargs['stdin'].read().decode('utf-8')
        return _FakeProc(stdout_text, returncode)

    monkeypatch.setattr(subprocess, 'Popen', fake_popen)
    return captured


def _activate(node: Node, session: str) -> None:
    """Mark the node active with a live claude session (as a running loop would)."""
    (node.node_dir / '.status').write_text('active\n', encoding='utf-8')
    node.sessions.set('claude', session)
