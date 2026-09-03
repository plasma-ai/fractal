"""Implements the cockpit's chat surface.

Everything chat and textual-free: the transport decision
(``resolve_transport`` -- fork the live loop session, resume a thread,
or start fresh), the ``ChatTurn`` subprocess runner the app drives from
a worker thread, and the ``ChatController`` owning the per-branch
transcripts plus the single in-flight turn. The agent invocation, the
spawn, and the stream parser all come from the agent seam
(``Node.chat_command`` and the registry backend), so validation, prompt
seeding, process placement, and the wire protocols can never drift from
``Node.chat``.
"""

from __future__ import annotations

import collections
import dataclasses
import pathlib
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from typing import IO, Optional

from fractal.core.agent import Agent, Invocation, StreamEvent, resolve

from . import fmt

__all__ = [
    'ChatEvent',
    'Transport',
    'resolve_transport',
    'ChatTurn',
    'ChatController',
]

# how much trailing stderr a failed turn surfaces in its error bubble
_STDERR_TAIL_LINES = 12


@dataclasses.dataclass(frozen=True)
class ChatEvent:
    """One parsed unit of an agent stream, in transcript vocabulary.

    ``kind`` is ``'session'`` (the captured resumable id), ``'text'`` (a delta
    the pane coalesces into one agent bubble), ``'tool'`` (a tool-use header),
    ``'meta'`` (the closing summary line), or ``'error'``.
    """

    kind: str
    text: str


# NOTE: field comments trail here -- each is a one-line value gloss, and
#   the above-line form would double a record that reads at a glance
@dataclasses.dataclass(frozen=True)
class Transport:
    """How a chat turn reaches a node (the resolved delivery decision)."""

    kind: str  # 'fork' | 'resume' | 'fresh'
    label: str  # mode note for the transcript
    session: Optional[str] = None  # the session involved, if any
    resume: bool = False  # continue in place instead of forking
    warn: bool = False  # surface the label as a warning, not just a meta line

    @property
    def chat_kwargs(self: Transport) -> dict:
        """Return the kwargs for ``Node.chat_command`` this decision implies."""
        if self.session is None:
            return {}
        return {'session': self.session, 'resume': self.resume}


def resolve_transport(
    *,
    agent: str,
    status: str,
    detached: bool,
    live_session: Optional[str],
    session: Optional[str] = None,
    own_chat: bool = False,
    root: Optional[pathlib.Path] = None,
) -> Transport:
    """Resolve how a chat turn reaches a node.

    Chat always reaches a real agent -- no node state diverts a turn
    anywhere else. An explicit ``session`` wins: the cockpit's own chat
    thread resumes in place, a forking agent's session forks, a settled
    non-forking thread resumes in place -- but a non-forking node's *live
    or paused* thread cannot be forked (and resuming it in place would
    perturb the running loop, or the session a paused run resumes with),
    so it falls back to a fresh session with a warning. With no explicit
    session, an active or paused forking node's woven session forks --
    "pause it, then ask what it was doing" is the flagship interrogation
    flow; every other active/paused shape (non-forking, detached, or no
    session woven yet) and anything settled or idle gets a fresh seeded
    session.

    Args:
        agent: The node's agent command (e.g. ``'claude'``/``'codex'``).
        status: The node's live status.
        detached: Whether the node runs detached (no woven session).
        live_session: The node's newest woven session, when active.
        session: An explicitly selected session (compose field / step fork).
        own_chat: Whether ``session`` is the cockpit's own prior chat thread.
        root: Tree data dir whose deployment hook file the backend lookup
            consults (hook-registered agents must resolve here too).

    Returns:
        The resolved transport.

    """
    # the fork capability is the backend's class-level fact; a node with no
    # agent configured yet (config is agent-editable: blank counts) reads as
    # forking (there is nothing to refuse), an unregistered one as
    # non-forking (never fork what cannot be forked); a broken deployment
    # hook (RuntimeError) reads the same -- the chat boundary guard surfaces
    # its error when the turn is built
    parts = agent.split()
    if parts:
        base = parts[0]
        try:
            can_fork = resolve(base, root=root).can_fork
        except (ValueError, RuntimeError):
            can_fork = False
    else:
        base, can_fork = '', True
    if session is not None:
        if own_chat:
            return Transport(
                kind='resume',
                label=f'continued chat {session}',
                session=session,
                resume=True,
            )
        if not can_fork:
            if status in ('active', 'paused') and session == live_session:
                shape = 'live' if status == 'active' else 'paused'
                return Transport(
                    kind='fresh',
                    label=f"{base} {shape} thread can't fork -- fresh session",
                    warn=True,
                )
            return Transport(
                kind='resume',
                label=f'resumed thread {session} (in place)',
                session=session,
                resume=True,
            )
        return Transport(
            kind='fork',
            label=f'forked session {session}',
            session=session,
        )
    if status in ('active', 'paused'):
        if can_fork and live_session:
            shape = 'live' if status == 'active' else 'paused'
            return Transport(
                kind='fork',
                label=f'forked {shape} session {live_session}',
                session=live_session,
            )
        if not can_fork:
            reason = f"{base} can't fork"
        elif detached:
            reason = 'detached node'
        else:
            reason = 'no live session yet'
        return Transport(kind='fresh', label=f'fresh session ({reason})')
    return Transport(kind='fresh', label='fresh session')


# ------ turn runners


class ChatTurn:
    """One spawned chat turn: a subprocess plus its parsed event stream.

    ``events`` spawns the process on first iteration, yields parsed
    ``ChatEvent`` items line-by-line, and finalizes (reap + stderr tail +
    exit-status event) at EOF. It never raises -- every failure becomes a
    terminal ``error`` event. ``cancel`` kills the process (idempotent,
    callable from any thread); a cancelled turn ends without an error event.
    """

    def __init__(self: ChatTurn, command: Invocation, agent: Agent) -> None:
        """Initialize ``ChatTurn``.

        Args:
            command: The agent invocation to spawn (``Node.chat_command``).
            agent: The node-bound backend (``node.agent(command.agent)``)
                supplying the stream parser and the spawn route --
                resolved at construction, so an unknown agent surfaces
                on the caller's error path, never inside ``events``.

        """
        self._command = command
        self._agent = agent
        self._parser = agent.parser()
        self._process: Optional[subprocess.Popen[str]] = None
        self._cancelled = False
        self._stderr: collections.deque[str] = collections.deque(
            maxlen=_STDERR_TAIL_LINES,
        )

    @property
    def cancelled(self: ChatTurn) -> bool:
        """Return whether ``cancel`` was called."""
        return self._cancelled

    def cancel(self: ChatTurn) -> None:
        """Kill the turn's process (idempotent; safe from any thread)."""
        self._cancelled = True
        process = self._process
        if process is not None and process.poll() is None:
            process.kill()

    def events(self: ChatTurn) -> Iterator[ChatEvent]:
        """Yield the turn's events; spawns on first iteration, never raises."""
        started = time.monotonic()
        # launch through the seam's spawn triad (a host's _spawn override
        # covers this surface too); the base hook wires argv/cwd/env,
        # stdin=DEVNULL, and stdout=PIPE in text mode -- the TUI pipes stderr
        try:
            process = self._agent.spawn(self._command, stderr=subprocess.PIPE)
        except OSError as error:
            yield ChatEvent(
                kind='error',
                text=f'{self._command.agent} failed to launch: {error}',
            )
            yield ChatEvent(kind='meta', text=fmt.chat_meta(0.0))
            return
        self._process = process
        if self._cancelled:
            process.kill()
        # drain stderr on the side: a single-threaded read-after-stdout-EOF
        # risks the pipe-buffer deadlock, and the tail feeds the error bubble
        drain = threading.Thread(
            target=_drain,
            args=(process.stderr, self._stderr),
            daemon=True,
        )
        drain.start()
        closed = False
        for line in process.stdout:
            # a malformed line must never abort the turn -- the parser swallows
            # bad JSON, but a shape it doesn't expect could still raise, so
            # degrade it to an error event rather than crashing the worker
            try:
                parsed = self._parser.feed(line)
            except Exception as error:
                yield ChatEvent(kind='error', text=f'stream parse error: {error}')
                continue
            for event in parsed:
                # a result closes the turn unless it is a mid-run step_finish
                # frame from a results-per-step backend (opencode; its final
                # frame sets final=True); every other backend emits one
                # terminal result; codex's carries final=False for cost reasons
                # but is still the turn's end
                is_result = event.kind == 'result'
                is_final = event.final or not self._agent.results_per_step
                terminal = is_result and is_final
                if terminal:
                    closed = True
                yield from _chat_events(event, terminal=terminal)
        returncode = process.wait()
        drain.join(timeout=1.0)
        if returncode != 0 and not self._cancelled:
            tail = ' '.join(line.strip() for line in self._stderr if line.strip())
            detail = f': {tail}' if tail else ''
            yield ChatEvent(
                kind='error',
                text=f'{self._command.agent} exited {returncode}{detail}',
            )
        # every turn closes with a meta line (wall clock when the stream
        # carried no summary -- e.g. a kill or a truncated stream)
        if not closed:
            wall = time.monotonic() - started
            yield ChatEvent(kind='meta', text=fmt.chat_meta(wall))


# ------ controller


class ChatController:
    """Per-branch chat transcripts plus the single in-flight turn.

    Owns everything about "one chat turn at a time": the live ``ChatTurn``,
    its branch, the monotonically incremented turn id (staleness stamp),
    the last-delta instant (watchdog input), the spinner counters, and the
    FIFO queue of sends typed while a turn was in flight. The app owns only
    the worker and the intervals.
    """

    def __init__(
        self: ChatController,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize ``ChatController``.

        Args:
            now: Monotonic-seconds clock for the idle watchdog and the
                spinner (injectable for deterministic tests).

        """
        self._now = now
        self._transcripts: dict[str, list[tuple[str, str]]] = {}
        self._sessions: dict[str, str] = {}
        # the in-flight turn (ids mint from 1, so 0 is never a live stamp)
        self._turn: Optional[ChatTurn] = None
        self._turn_branch = ''
        self._turn_id = 0
        self._seen = 0.0
        self._spin_frame = 0
        self._spin_started = 0.0
        # sends typed while a turn was in flight, oldest first; each entry
        # is (branch, prompt, explicit session) captured at type time
        self._queue: collections.deque[tuple[str, str, Optional[str]]] = (
            collections.deque()
        )

    def transcript(self: ChatController, branch: str) -> list[tuple[str, str]]:
        """Return a branch's transcript (created empty on first access).

        Lines are ``(who, text)`` with ``who`` one of ``'you'`` / ``'auth'``
        (the agent) / ``'meta'`` / ``'error'``.
        """
        return self._transcripts.setdefault(branch, [])

    def append(self: ChatController, branch: str, who: str, text: str) -> None:
        """Append a ``(who, text)`` line to a branch's transcript."""
        self.transcript(branch).append((who, text))

    def append_delta(self: ChatController, branch: str, text: str) -> None:
        """Grow the trailing agent line, or start one (token coalescing)."""
        transcript = self.transcript(branch)
        if transcript and transcript[-1][0] == 'auth':
            transcript[-1] = ('auth', transcript[-1][1] + text)
        else:
            transcript.append(('auth', text))

    def recall(self: ChatController, branch: str, prompt: str) -> None:
        """Drop the newest ``('you', prompt)`` line from a branch's transcript.

        An interrupt hands a lone queued send back to the composer; the line
        it posted was never sent, so it leaves the record too.
        """
        transcript = self.transcript(branch)
        for index in range(len(transcript) - 1, -1, -1):
            if transcript[index] == ('you', prompt):
                del transcript[index]
                return

    def session(self: ChatController, branch: str) -> Optional[str]:
        """Return the cockpit's own chat session for a branch, if any."""
        return self._sessions.get(branch)

    def set_session(self: ChatController, branch: str, session: str) -> None:
        """Record the cockpit's chat session for a branch (multi-turn resume)."""
        self._sessions[branch] = session

    @property
    def turn(self: ChatController) -> Optional[ChatTurn]:
        """Return the in-flight turn; ``None`` when idle."""
        return self._turn

    @property
    def turn_branch(self: ChatController) -> str:
        """Return the branch the in-flight turn chats on (``''`` when idle)."""
        return self._turn_branch

    def begin(self: ChatController, branch: str, turn: ChatTurn) -> int:
        """Cancel any prior turn, adopt this one, and return its fresh turn id."""
        self.cancel()
        self._turn = turn
        self._turn_branch = branch
        self._turn_id += 1
        self._seen = self._now()
        # the in-flight spinner: pinned under the transcript until the turn ends
        self._spin_frame = 0
        self._spin_started = self._now()
        return self._turn_id

    def is_current(self: ChatController, turn_id: int) -> bool:
        """Return whether a message stamp belongs to the live turn."""
        return turn_id == self._turn_id

    def touch(self: ChatController) -> None:
        """Record delta arrival (re-arms the idle watchdog)."""
        self._seen = self._now()

    def idle(self: ChatController) -> float:
        """Return seconds since the last delta (watchdog input)."""
        return self._now() - self._seen

    def finish(self: ChatController, turn_id: int) -> None:
        """Clear the turn iff the stamp is current.

        A stale done from a superseded turn must not orphan the new turn's
        subprocess.
        """
        if turn_id == self._turn_id:
            self._turn = None
            self._turn_branch = ''

    def cancel(self: ChatController) -> None:
        """Kill and clear the in-flight turn; idempotent.

        Worker cancellation alone cannot unblock a readline; the process
        kill is the real lever.
        """
        turn = self._turn
        if turn is not None:
            turn.cancel()
        self._turn = None
        self._turn_branch = ''

    def enqueue(
        self: ChatController,
        branch: str,
        prompt: str,
        explicit: Optional[str],
    ) -> None:
        """Queue a send typed while a turn is in flight (FIFO, branch-stamped)."""
        self._queue.append((branch, prompt, explicit))

    def dequeue(self: ChatController) -> Optional[tuple[str, str, Optional[str]]]:
        """Pop the oldest queued send; ``None`` when the queue is empty."""
        return self._queue.popleft() if self._queue else None

    def clear_queue(self: ChatController) -> list[tuple[str, str, Optional[str]]]:
        """Drop every queued send, returning the dropped entries."""
        dropped = list(self._queue)
        self._queue.clear()
        return dropped

    @property
    def spin_frame(self: ChatController) -> int:
        """Return the spinner's current frame counter."""
        return self._spin_frame

    def spin_elapsed(self: ChatController) -> float:
        """Return seconds since the in-flight spinner started (its clock)."""
        return self._now() - self._spin_started

    def spin(self: ChatController) -> None:
        """Advance the spinner frame."""
        self._spin_frame += 1


# ------ helper functions


def _chat_events(event: StreamEvent, *, terminal: bool = True) -> list[ChatEvent]:
    """Translate one seam stream event into transcript vocabulary.

    Session/text/tool/error facts map one-to-one; cost facts and tool
    results are recorded or rendered elsewhere and stay out of the
    transcript. Only a ``terminal`` result closes the turn with its
    summary line -- opencode emits a result per step_finish, so its
    mid-run frames arrive non-terminal and render nothing but any failure
    detail. A turn-counting result (claude) closes on turns/duration/cost,
    a wall-time result (codex) on duration alone.
    """
    if event.kind == 'session':
        return [ChatEvent(kind='session', text=event.session)]
    if event.kind == 'text':
        return [ChatEvent(kind='text', text=event.text)]
    if event.kind == 'tool':
        return [ChatEvent(kind='tool', text=event.tool)]
    if event.kind == 'error':
        return [ChatEvent(kind='error', text=event.message)]
    if event.kind == 'result':
        result: list[ChatEvent] = []
        if event.failed and event.message:
            result.append(ChatEvent(kind='error', text=event.message))
        if terminal:
            summary = fmt.chat_summary(event.turns, event.duration, event.cost)
            result.append(ChatEvent(kind='meta', text=summary))
        return result
    return []


def _drain(stream: Optional[IO[str]], sink: collections.deque[str]) -> None:
    """Drain a pipe into a bounded deque (the stderr side-thread)."""
    if stream is None:
        return
    for line in stream:
        sink.append(line)
