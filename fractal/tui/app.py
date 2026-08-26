"""Implements ``FractalApp`` -- the fractal cockpit (``fractal open``).

Composes the single-screen four-pane grid (tree / radio / node / message),
wires the design tokens into the stylesheet, and runs the two-level focus-ring
mode machine. The shell owns composition, theme wiring, ring navigation, key
dispatch, the poll loop (a tick launches at most one off-thread snapshot
build; results land as ``SnapshotReady`` messages, so keys never wait behind
a build), and re-scoping; each pane module owns its interior,
selection state, and key handlers, and renders purely from the current
``Snapshot``. Anything crossed between the app and a pane is public
(``scope_to``, ``paint_ring``, ``MessagePane.end_edit``, ...); the underscore
prefix is reserved for object-internal state.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Optional

from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Click, Key, MouseDown, Resize
from textual.geometry import Size
from textual.message import Message
from textual.widgets import Input, OptionList, Static, TextArea
from textual.worker import Worker, get_current_worker

from fractal.constants import HEADLESS_FILE, HEADLESS_LOG
from fractal.core.agent import Agent, Invocation
from fractal.core.node import Node

from . import fmt, theme
from .actions import TuiActions
from .chat import ChatController, ChatEvent, ChatTurn, resolve_transport
from .data import TuiData
from .panes import MessagePane, NodePane, RadioPane, TreePane
from .poller import NodePoller
from .snapshot import Snapshot, SnapshotBuilder
from .widgets import Pane

__all__ = [
    'ChatDelta',
    'ChatDone',
    'SnapshotReady',
    'FractalApp',
]

# cancel an in-flight chat turn after this much stream silence
_CHAT_IDLE_S = 120.0


class ChatDelta(Message):
    """One ``ChatEvent`` for a branch's transcript (from the chat worker)."""

    def __init__(self: ChatDelta, turn_id: int, branch: str, event: ChatEvent) -> None:
        """Initialize ``ChatDelta``.

        Args:
            turn_id: Identity of the turn that produced the event (a stale
                event from a superseded turn is dropped on arrival).
            branch: Branch whose transcript the event belongs to.
            event: The parsed stream event.

        """
        super().__init__()
        self.turn_id = turn_id
        self.branch = branch
        self.event = event


class ChatDone(Message):
    """The chat worker finished (clean, error, or cancelled)."""

    def __init__(self: ChatDone, turn_id: int) -> None:
        """Initialize ``ChatDone``.

        Args:
            turn_id: Identity of the turn that finished (a stale done from a
                superseded turn must not clear the live one).

        """
        super().__init__()
        self.turn_id = turn_id


class SnapshotReady(Message):
    """One built ``Snapshot`` for the cockpit (from the poll worker)."""

    def __init__(self: SnapshotReady, gen: int, snapshot: Snapshot) -> None:
        """Initialize ``SnapshotReady``.

        Args:
            gen: Build generation captured at the worker's launch (a result
                from before a user-initiated rebuild is dropped on arrival).
            snapshot: The built snapshot (the previous object when nothing
                changed on disk).

        """
        super().__init__()
        self.gen = gen
        self.snapshot = snapshot


class FractalApp(App):
    """The fractal cockpit: a live four-pane view over a node tree."""

    CSS_PATH = 'app.tcss'
    AUTO_FOCUS = None
    BINDINGS = [
        Binding('tab', 'field_next', show=False),
        Binding('shift+tab', 'field_prev', show=False),
    ]
    # the focus ring's top row; `message` sits below it (bottom-left)
    TOP = ['fractal', 'radio', 'node']

    def __init__(
        self: FractalApp,
        node: Node,
        *,
        branch: Optional[str] = None,
        tz: Optional[dt.tzinfo] = None,
        now: Optional[Callable[[], float]] = None,
        turn_factory: Optional[Callable[[Invocation, Agent], ChatTurn]] = None,
    ) -> None:
        """Initialize ``FractalApp``.

        Args:
            node: The user (root) node anchoring the tree.
            branch: Branch to focus initially; the root when omitted.
            tz: Timezone for rendered timestamps; the local zone when omitted.
            now: Epoch-seconds clock for live-elapsed math; ``time.time`` when
                omitted (injectable for deterministic tests).
            turn_factory: Builds the chat-turn runner for an agent command
                and its backend; ``ChatTurn`` when omitted
                (injectable for tests).

        """
        # ansi_color=True disables the ansi-to-truecolor filter so the theme's
        # ansi_default background reaches the terminal unconverted -- the
        # screen and panes show the terminal's own background, never a picked
        # one (the color system itself stays truecolor: Theme.ansi is False)
        super().__init__(ansi_color=True)
        # activate the design tokens: the structural $variables reach the first
        # stylesheet parse via get_theme_variable_defaults (read during
        # super().__init__); registering + selecting the theme here re-parses
        # with the pinned exact accents before first paint
        self.register_theme(theme.THEME)
        self.theme = 'fractal'
        # bind the read stack and build the first snapshot (compose needs it)
        self.data = TuiData(node)
        self.now = now or time.time
        poller = NodePoller(self.data.db_dir)
        self.builder = SnapshotBuilder(self.data, poller, now=self.now)
        self.actions = TuiActions(self.data)
        self.scope = branch or self.data.root_branch
        # the user (root) node opens on the descendants log view (the pane's
        # sub_log default reads the same rule once it constructs below)
        self.snapshot = self.builder.build(
            self.scope,
            want_subtree_log=self.scope == self.data.root_branch,
        )
        # poll machinery: a tick launches at most one off-thread build; every
        # later builder.build call holds the lock (the worker and the
        # synchronous user-initiated paths both route through it)
        self._build_lock = threading.Lock()
        self._build_gen = 0
        self._poll_worker: Optional[Worker] = None
        self.tz = tz or dt.datetime.now().astimezone().tzinfo
        # nav / mode state
        self.focus_id = 'fractal'
        self.mode = 'ring'
        # row sections a landing skipped while their pane mode was driven
        # (rebuilt by refresh_stale once the mode exits)
        self._node_stale = False
        self._radio_stale = False
        # chat state: transcripts, the in-flight turn, and the spinner all
        # live on the controller -- the app owns the worker and the intervals
        self.chat = ChatController()
        self._turn_factory = turn_factory or ChatTurn
        # panes (each owns its interior, selection state, and key handlers)
        self.tree_pane = TreePane(self)
        self.radio_pane = RadioPane(self)
        self.node_pane = NodePane(self)
        self.message_pane = MessagePane(self)

    def get_theme_variable_defaults(self: FractalApp) -> dict[str, str]:
        """Return the token variables the stylesheet needs at first parse.

        ``App.__init__`` parses the stylesheet while the stock theme is still
        active, so the structural color tokens and numeric tokens must already
        resolve here or boot fails with an unresolved-variable error.
        """
        return {**theme.THEME.variables, **theme.css_variables()}

    def compose(self: FractalApp) -> ComposeResult:
        """Compose the screen: header, the four panes, footer."""
        yield Static('', id='header')
        with Horizontal(id='body'):
            with Vertical(id='leftcol'):
                with Horizontal(id='top'):
                    with Pane(
                        id='fractal',
                        classes='pane',
                        resize='right',
                        floor=theme.TREE_W_MIN,
                    ):
                        yield from self.tree_pane.compose()
                    with Pane(id='radio', classes='pane'):
                        yield from self.radio_pane.compose()
                with Pane(
                    id='message',
                    classes='pane',
                    resize='top',
                    floor=theme.MESSAGE_H_MIN,
                ):
                    yield from self.message_pane.compose()
            with Pane(id='node', classes='pane'):
                yield from self.node_pane.compose()
        yield Static(self._footer(), id='footer')

    def on_mount(self: FractalApp) -> None:
        """Size the panes, render the initial state, and start the poll loop."""
        self._resize()
        self._set_header()
        self.paint_ring()
        self.tree_pane.rebuild(self.snapshot)
        self.radio_pane.rebuild(self.snapshot)
        self.node_pane.rebuild(self.snapshot)
        self.message_pane.refresh_visibility()
        self.message_pane.paint_fields()
        self.set_interval(theme.REFRESH_S, self._tick)
        self._spinner = self.set_interval(theme.SPIN_S, self._spin, pause=True)

    def _build(self: FractalApp) -> None:
        """Build the current snapshot (lazy sections per the pane states).

        The synchronous user-initiated path: bumps the build generation so an
        in-flight poll-worker result is dropped as stale on arrival.
        """
        self._build_gen += 1
        with self._build_lock:
            self.snapshot = self.builder.build(
                self.scope,
                want_feed=self.radio_pane.want_feed,
                want_archive=self.radio_pane.want_archive,
                want_subtree_log=self.node_pane.sub_log,
            )

    def _spin(self: FractalApp) -> None:
        """Advance the in-flight chat spinner (pauses itself when idle)."""
        if self.chat.turn is None:
            self._spinner.pause()
            self.message_pane.clear_pending()
            return
        self.chat.spin()
        self.message_pane.update_pending()

    def _tick(self: FractalApp) -> None:
        """Watchdog the chat turn and launch an off-thread snapshot build."""
        if not self.is_running:
            return
        # watchdog: a silent in-flight chat turn is cancelled, not waited on
        if self.chat.turn is not None and self.chat.idle() > _CHAT_IDLE_S:
            branch = self.chat.turn_branch
            self.chat.cancel()
            self.message_pane.clear_pending()
            self.message_pane.post(
                branch=branch,
                who='error',
                text=f'{theme.WARN} agent silent for {fmt.dur(_CHAT_IDLE_S)} -- cancelled',
            )
        # launch-if-idle: at most one poll build in flight; the result lands
        # as a SnapshotReady message, never built on the UI thread
        if self._poll_worker is None or self._poll_worker.is_finished:
            self._poll_worker = self._poll(
                self.scope,
                want_feed=self.radio_pane.want_feed,
                want_archive=self.radio_pane.want_archive,
                want_subtree_log=self.node_pane.sub_log,
                gen=self._build_gen,
            )

    @work(thread=True, exclusive=True, group='poll')
    def _poll(
        self: FractalApp,
        scope: str,
        *,
        want_feed: bool,
        want_archive: bool,
        want_subtree_log: bool,
        gen: int,
    ) -> None:
        """Build one snapshot off-thread and post it back to the UI thread.

        Every parameter is captured at launch on the UI thread -- the worker
        never reads pane state. The single build is uninterruptible, so there
        is no cancellation check; a superseded result is dropped on arrival
        by the generation guard.
        """
        with self._build_lock:
            snapshot = self.builder.build(
                scope,
                want_feed=want_feed,
                want_archive=want_archive,
                want_subtree_log=want_subtree_log,
            )
        self.post_message(SnapshotReady(gen, snapshot))

    def on_snapshot_ready(self: FractalApp, message: SnapshotReady) -> None:
        """Land a worker-built snapshot, dropping a stale one.

        An unchanged snapshot lands as the same object: the panes skip their
        rebuilds, but the card still repaints while a row is open so its
        elapsed cells and cap gauges tick between disk writes.
        """
        if not self.is_running:
            return
        # a result launched before a user-initiated rebuild lost to that
        # build's newer snapshot -- its scope or want-flags may have moved
        if message.gen != self._build_gen:
            return
        # the steady-tick short-circuit: an unchanged tree posts the same
        # object, and no pane needs rebuilding -- only the open-row clocks,
        # plus any rows a driven mode left stale, which only this tick can
        # repay on a then-quiet tree
        if message.snapshot is self.snapshot:
            if self.snapshot.ticking:
                self.node_pane.set_card(self.snapshot)
            self.refresh_stale()
            return
        self.snapshot = message.snapshot
        self._refresh()

    def _refresh(self: FractalApp) -> None:
        """Push the current snapshot into the panes (mode-aware).

        The geometry re-applies first -- poll-driven data can widen the node
        pane, and rows rendered wider than a stale pane crop their
        right-anchored cells. The card always refreshes; the explorer/log and
        the radio rows rebuild only while the user is not driving them (never
        yank rows out from under a cursor) -- a driven pane's rows go stale
        instead, repaid by ``refresh_stale`` once its mode exits.
        """
        self._resize()
        self._set_header()
        self.tree_pane.rebuild(self.snapshot)
        self.node_pane.set_card(self.snapshot)
        self._node_stale = True
        self._radio_stale = True
        self.refresh_stale()

    def refresh_stale(self: FractalApp) -> None:
        """Rebuild any stale row sections the active mode is not driving.

        Runs on every landing, on the steady tick's unchanged-snapshot
        short-circuit, and on a driven pane's leave -- so rows skipped under
        a cursor render as soon as, and only when, their mode lets go.
        """
        if self._node_stale and self.mode != 'node':
            self.node_pane.rebuild_body(self.snapshot)
            self._node_stale = False
        radio_modes = ('radio', 'rdrop', 'rdetail', 'rreact')
        if self._radio_stale and self.mode not in radio_modes:
            self.radio_pane.rebuild(self.snapshot)
            self._radio_stale = False

    def refresh_radio(self: FractalApp) -> None:
        """Re-build for a radio source switch (fills the lazy section).

        The synchronous build can absorb a pending disk change the poll can
        never re-deliver (a now-quiet tree keeps posting the same snapshot
        object, which the identity guard drops), so every pane refreshes --
        the radio rows explicitly: ``_refresh`` skips them while the user
        drives the pane, and the switch is exactly that.
        """
        self._build()
        self._refresh()
        self.radio_pane.rebuild_rows(self.snapshot)

    def refresh_log(self: FractalApp) -> None:
        """Re-build for a log-scope toggle (fills the subtree section).

        Every pane refreshes for the same reason as ``refresh_radio``; the
        body explicitly, since ``_refresh`` skips it in node mode.
        """
        self._build()
        self._refresh()
        self.node_pane.rebuild_body(self.snapshot)
        self.node_pane.paint_zone()

    def scope_to(self: FractalApp, branch: str) -> None:
        """Re-point the cockpit at ``branch``: every pane follows."""
        self.scope = branch
        # the log scope restores the branch's remembered choice
        # (set before the log builds)
        self.node_pane.sub_log = self.node_pane.log_scope(branch)
        self._build()
        self._resize()
        self._set_header()
        self.tree_pane.rescope(self.snapshot)
        self.radio_pane.rescope(self.snapshot)
        self.node_pane.rescope(self.snapshot)
        self.message_pane.rescope(self.snapshot)

    def fork_session(self: FractalApp, entry: dict) -> None:
        """Fork a step's session into the compose pane (chat mode)."""
        self.message_pane.fork_session(entry)

    def attach_node(self: FractalApp) -> None:
        """Suspend the cockpit and attach the scoped node's tmux session.

        The attach is read-only -- the node's terminal is a window, not a
        control surface -- so tmux ignores every key except ones bound to
        ``detach-client``: an ``esc`` binding in a dedicated key table set
        on the viewed session is the way out, advertised on the session's
        status line for the duration.
        """
        node_dir = self.data.node_dir(self.scope)
        if node_dir is not None and (node_dir / HEADLESS_FILE).is_file():
            self.notify(f'headless output: {node_dir / HEADLESS_LOG}')
            return
        session = self.data.tmux_session_name(self.scope)
        if session not in self.data.live_sessions():
            self.notify('no running session', severity='warning')
            return
        # the = prefix forces an exact target match (-t resolves by prefix,
        # so a short name would false-match longer session names); set-option
        # rejects the bare exact form, so its target carries a trailing colon
        # (an empty window part) that keeps the exact session match
        target = f'={session}'
        option_target = f'{target}:'
        if os.environ.get('TMUX'):
            # inside tmux a nested attach refuses; switch this client instead
            # (the outer client stays the user's own -- no read-only leash)
            subprocess.run(
                ['tmux', 'switch-client', '-t', target],
                capture_output=True,
                text=True,
            )
            return
        # dress the session for viewing: the esc detach binding (in a
        # dedicated key table set on this session only -- the root table is
        # server-wide, so binding there would leash the user's other clients
        # and clobber their own Escape binding; ESC-prefixed key sequences
        # still parse as keys -- tmux's escape-time disambiguates) and a
        # status naming the way out
        overrides = (
            ('key-table', 'fractal-view'),
            ('status', 'on'),
            ('status-left', ' esc detach '),
            ('status-left-length', '20'),
            ('status-right', ''),
        )
        # the dress/undress calls capture output so any tmux chatter never
        # writes over the cockpit's alternate screen (best-effort: failures
        # leave the attach itself intact)
        subprocess.run(
            ['tmux', 'bind-key', '-T', 'fractal-view', 'Escape', 'detach-client'],
            capture_output=True,
            text=True,
        )
        for option, value in overrides:
            subprocess.run(
                ['tmux', 'set-option', '-t', option_target, option, value],
                capture_output=True,
                text=True,
            )
        try:
            with self.suspend():
                result = subprocess.run(['tmux', 'attach-session', '-r', '-t', target])
                # wipe the client's parting '[detached ...]' line while still
                # on the normal screen -- left alone it would surface in the
                # terminal's scrollback once the cockpit quits
                if result.returncode == 0:
                    print('\x1b[1A\x1b[2K', end='', flush=True)
        finally:
            # undress: drop the binding and the session-level overrides (the
            # loop's sessions carry none of their own, so -u restores clean)
            subprocess.run(
                ['tmux', 'unbind-key', '-T', 'fractal-view', 'Escape'],
                capture_output=True,
                text=True,
            )
            for option, _ in overrides:
                subprocess.run(
                    ['tmux', 'set-option', '-u', '-t', option_target, option],
                    capture_output=True,
                    text=True,
                )

    def compose_reply(self: FractalApp, row: dict) -> None:
        """Pre-fill the compose pane as a reply to ``row``."""
        self.message_pane.compose_reply(row)

    def compose_chat(self: FractalApp, row: dict, *, session: Optional[str]) -> None:
        """Chat with ``row``'s sender (re-scope + fork its live session)."""
        self.message_pane.compose_chat(row, session=session)

    def start_chat(self: FractalApp, prompt: str) -> None:
        """Run or queue one chat turn against the scoped node.

        Posts the prompt to the transcript at once. A turn already in flight
        queues the send (FIFO, dispatched when that turn finishes) rather than
        racing it; an idle chat dispatches immediately. The send carries the
        branch (and session) it was typed against, so a re-scope before it runs
        cannot redirect it. Every outcome lands in the transcript; chat never
        writes radio.
        """
        branch = self.scope
        pane = self.message_pane
        explicit = pane.session if pane.session and pane.session != '-' else None
        pane.post(branch, 'you', prompt)
        # a turn already streaming: queue this send behind it (the queued 'you'
        # line above shows it pending) -- on_chat_done dispatches the next
        if self.chat.turn is not None:
            self.chat.enqueue(branch, prompt, explicit)
            return
        self._dispatch_chat(branch, prompt, explicit)

    def _dispatch_chat(
        self: FractalApp,
        branch: str,
        prompt: str,
        explicit: Optional[str],
    ) -> None:
        """Resolve the transport for one send and spawn its turn.

        The send's branch anchors everything -- transcript, node, and transport
        -- so a queued send dispatches against the branch it was typed on even
        after the cockpit re-scoped away. Only ever entered with no turn in
        flight (an idle ``start_chat`` or a ``ChatDone`` draining the queue), so
        it never races a live turn.
        """
        pane = self.message_pane
        status, agent, detached = self._chat_state(branch)
        own_chat = explicit is not None and explicit == self.chat.session(branch)
        live = None
        if status in ('active', 'paused'):
            live = self._live_session(branch, agent)
        transport = resolve_transport(
            agent=agent,
            status=status,
            detached=detached,
            live_session=live,
            session=explicit,
            own_chat=own_chat,
            root=self.data.db_dir,
        )
        # a fallback the user should notice (an unforkable live thread
        # resolving to fresh) gets a toast on top of its meta line
        if transport.warn:
            self.notify(transport.label, severity='warning')
        node = self.data.node(branch)
        if node is None:
            pane.post(branch, 'error', f'{theme.WARN} node unavailable')
            self._dispatch_queued()
            return
        # boundary guard: the node's live state can flip between resolve and
        # build (.status/.session are external); the backend is resolved here
        # too, so an unknown agent -- or a broken deployment hook, whose
        # sticky RuntimeError meets every resolve -- lands on this error
        # path, never inside the turn's event stream
        try:
            command = node.chat_command(prompt, **transport.chat_kwargs)
            agent = node.agent(command.agent)
        except (ValueError, RuntimeError) as error:
            pane.post(branch, 'error', f'{theme.WARN} {error}')
            self._dispatch_queued()
            return
        pane.post(branch, 'meta', f'{theme.SEP} {transport.label}')
        turn = self._turn_factory(command, agent)
        turn_id = self.chat.begin(branch, turn)
        # the spinner only pins under the transcript that is on screen; an
        # off-scope turn's pending re-shows when the user scopes back to it
        if branch == self.scope:
            pane.show_pending()
        self._spinner.resume()
        self._chat_worker(turn, branch, turn_id)

    def _dispatch_queued(self: FractalApp) -> None:
        """Dispatch the next queued send (FIFO), against the branch it was typed on."""
        queued = self.chat.dequeue()
        if queued is not None:
            branch, prompt, explicit = queued
            self._dispatch_chat(branch, prompt, explicit)

    def _chat_state(self: FractalApp, branch: str) -> tuple[str, str, bool]:
        """Return the ``(status, agent, detached)`` a send resolves transport with.

        The scoped branch reads its live card; a queued send dispatching
        against an off-scope branch reads that branch's own status and config
        directly (no snapshot for it is on hand).
        """
        if branch == self.scope:
            card = self.snapshot.card or {}
            return (
                card.get('status', 'idle'),
                card.get('agent') or '',
                bool(card.get('detached')),
            )
        config = self.data.config(branch)
        return (
            self.data.status(branch),
            config.get('agent') or '',
            bool(config.get('detached')),
        )

    def _live_session(self: FractalApp, branch: str, agent: str) -> Optional[str]:
        """Look up the node's newest woven session (read-only, at send time)."""
        try:
            with self.data.reader() as connection:
                return self.data.live_session(connection, branch, agent)
        except (FileNotFoundError, sqlite3.Error):
            return None

    def _cancel_turn(self: FractalApp) -> None:
        """Kill any in-flight turn and note the cancel in the transcript."""
        turn = self.chat.turn
        if turn is not None and not turn.cancelled:
            self.message_pane.post(self.chat.turn_branch, 'meta', 'cancelled')
        self.chat.cancel()
        self.message_pane.clear_pending()

    @work(thread=True, exclusive=True, group='chat')
    def _chat_worker(
        self: FractalApp,
        turn: ChatTurn,
        branch: str,
        turn_id: int,
    ) -> None:
        """Stream a turn's events back to the UI thread."""
        worker = get_current_worker()
        for event in turn.events():
            if worker.is_cancelled:
                break
            self.post_message(ChatDelta(turn_id, branch, event))
        self.post_message(ChatDone(turn_id))

    def on_chat_delta(self: FractalApp, message: ChatDelta) -> None:
        """Land one chat event in its branch's transcript (and the screen)."""
        # a queued delta from a superseded turn must not touch the live one
        if not self.chat.is_current(message.turn_id):
            return
        self.chat.touch()
        branch, event = message.branch, message.event
        pane = self.message_pane
        if event.kind == 'session':
            # the captured id becomes this branch's chat thread (multi-turn)
            self.chat.set_session(branch, event.text)
            if branch == self.scope:
                pane.session = event.text
                pane.show_session()
        elif event.kind == 'text':
            # the reply is streaming: the thinking spinner has done its job
            self._spinner.pause()
            pane.clear_pending()
            pane.post_delta(branch, event.text)
        elif event.kind == 'tool':
            pane.post(branch, 'meta', f'{theme.TOOL} {event.text}')
        elif event.kind == 'error':
            pane.post(branch, 'error', f'{theme.WARN} {event.text}')
        else:
            pane.post(branch, 'meta', event.text)

    def on_chat_done(self: FractalApp, message: ChatDone) -> None:
        """Clear the finished turn, then dispatch the next queued send."""
        # only the live turn's own done clears it -- a stale done from a
        # superseded turn would orphan the new turn's subprocess
        if self.chat.is_current(message.turn_id):
            self.chat.finish(message.turn_id)
            self.message_pane.clear_pending()
            # the turn is done: the next queued send starts on its own branch
            self._dispatch_queued()

    def interrupt_chat(self: FractalApp) -> None:
        """Interrupt the in-flight chat turn.

        Kills the streaming turn (``_cancel_turn`` notes the ``cancelled``
        line). A lone queued send goes back into the composer for editing
        rather than firing unbidden; several queued sends dispatch at once as
        one combined turn (their ``you`` lines already read in order).
        """
        if self.chat.turn is None:
            return
        self._cancel_turn()
        dropped = self.chat.clear_queue()
        if not dropped:
            return
        if len(dropped) == 1:
            branch, prompt, _ = dropped[0]
            self.message_pane.recall_send(branch, prompt)
            return
        # combine the first branch's sends into one turn, oldest first; a send
        # queued against another branch stays parked for the done-drain
        branch = dropped[0][0]
        combined = [entry for entry in dropped if entry[0] == branch]
        for entry in dropped:
            if entry[0] != branch:
                self.chat.enqueue(*entry)
        prompt = '\n\n'.join(entry[1] for entry in combined)
        self._dispatch_chat(branch, prompt, combined[0][2])

    def on_unmount(self: FractalApp) -> None:
        """Kill any in-flight chat turn on shutdown (no orphan agents)."""
        self.chat.cancel()

    def _resize(self: FractalApp, terminal: Optional[Size] = None) -> None:
        """Apply the snapshot's pane geometry (a dragged tree width wins).

        Args:
            terminal: The live terminal size, passed by the resize event
                (``self.size`` still reads the old size there); the current
                size when omitted.

        """
        geometry = self.snapshot.geometry
        self.query_one('#node').styles.width = geometry.node_width
        tree = self.query_one('#fractal', Pane)
        if tree.user_size is None:
            # the tree opens no wider than the node pane; dragging widens it
            width = min(geometry.tree_width, geometry.node_width)
            # a leftover slice narrower than the radio's own border+padding
            # cannot render as a pane -- it paints a stray chrome band under
            # the node pane -- so the tree absorbs the crumb and the radio
            # collapses entirely behind the node pane
            left = (terminal or self.size).width - geometry.node_width
            if width < left < width + theme.PANE_CHROME:
                width = left
            tree.styles.width = width

    def on_resize(self: FractalApp, event: Resize) -> None:
        """Re-clamp any dragged pane size (a resize moves the 50% caps)."""
        for pane in self.query(Pane):
            pane.clamp_user_size(event.size)
        # the crumb rule reads the terminal width -- re-apply the geometry so
        # a window resize lands it without waiting for the next snapshot
        self._resize(terminal=event.size)

    def _set_header(self: FractalApp) -> None:
        """Render the breadcrumb: brand, repository, focused branch."""
        self.query_one('#header', Static).update(
            f'[b {theme.INK}]fractal[/]'
            f' [{theme.CHROME}]{theme.SEP} {self.snapshot.repo} {theme.PROMPT}'
            f' {self.scope}[/]'
        )

    def _footer(self: FractalApp) -> Table:
        """Render the footer: nav hints left, brand mark snapped right."""
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1, no_wrap=True)
        grid.add_column(justify='right', no_wrap=True)
        hints = (
            f'{theme.LEFT}{theme.RIGHT} panes {theme.SEP} {theme.RET} enter'
            f' {theme.SEP} arrows move {theme.SEP} esc back'
            f' {theme.SEP} {theme.CTRL}a attach {theme.SEP} {theme.CTRL}g interrupt'
            f' {theme.SEP} {theme.CTRL}q quit'
        )
        grid.add_row(
            Text.from_markup(f'[{theme.DIM}]{hints}[/]'),
            Text.from_markup(f'[{theme.DIM}]{theme.COPYRIGHT} Plasma AI[/]'),
        )
        return grid

    def on_key(self: FractalApp, event: Key) -> None:
        """Dispatch the key to the active mode's handler."""
        # ^a attaches the scoped node's tmux session from anywhere (it takes
        # the whole screen, not a pane); typing surfaces keep their own ^a
        if event.key == 'ctrl+a':
            self.attach_node()
            event.stop()
            return
        # ^g interrupts the in-flight chat turn from anywhere (^i is Tab in
        # most terminal encodings, so it cannot serve as a distinct key)
        if event.key == 'ctrl+g':
            self.interrupt_chat()
            event.stop()
            return
        handlers = {
            'ring': self._key_ring,
            'tree': self.tree_pane.key,
            'node': self.node_pane.key,
            'radio': self.radio_pane.key,
            'rdrop': self.radio_pane.key_drop,
            'rdetail': self.radio_pane.key_detail,
            'rreact': self.radio_pane.key_react,
            'field': self.message_pane.key_field,
            'edit': self.message_pane.key_edit,
            'combo': self.message_pane.key_combo,
            'chatscroll': self.message_pane.key_chatscroll,
        }
        handler = handlers.get(self.mode)
        if handler:
            handler(event)

    def on_click(self: FractalApp, event: Click) -> None:
        """Move a list's selection cursor to the clicked row (a re-click: ``enter``).

        A click on a row lands the cursor there, exactly like arrowing to
        it; a click on the already-highlighted row activates it exactly like
        ``enter`` (re-scope, open the detail, fold, fork). Any other click falls
        through, and the dropdown modes keep the keyboard's exclusive
        control.
        """
        if self.mode in ('combo', 'rdrop', 'rreact'):
            return
        lists = {
            'treebody': ('fractal', self.tree_pane.click_row),
            'radiorows': ('radio', self.radio_pane.click_row),
            'nodeexplore': ('node', self.node_pane.click_row),
            'nodeevents': ('node', self.node_pane.click_row),
        }
        # resolve the clicked widget up to a list row (a direct child of a
        # scroll container -- tree rows are containers, so the click lands
        # on their children)
        row = event.widget
        container = None
        while row is not None:
            parent = row.parent
            if parent is not None and parent.id in lists:
                container = parent
                break
            row = parent
        if container is None:
            return
        pane_id, select = lists[container.id]
        previous = self.focus_id
        leaving_message = previous == 'message' and pane_id != 'message'
        # the focus moves before the select, so an activation that redirects
        # it (a step re-click forking into compose) keeps the final say
        self.focus_id = pane_id
        if not select(row):
            self.focus_id = previous
            return
        # leaving the composer: release its text-entry focus so keys reach
        # the cockpit again (the keyboard leave path does the same)
        if leaving_message:
            self.set_focus(None)
            self.message_pane.paint_fields()
        self.paint_ring()

    def on_mouse_down(self: FractalApp, event: MouseDown) -> None:
        """Grab a resize edge from the far side of its divide.

        A divide between panes is two border columns and each ``Pane`` grabs
        only its own; a press on the neighbor's column bubbles up here
        unclaimed and starts the same drag.
        """
        if event.button != 1:
            return
        tree = self.query_one('#fractal', Pane)
        region = tree.region
        on_edge = event.screen_x == region.right
        in_rows = region.y <= event.screen_y < region.bottom
        if on_edge and in_rows:
            tree.grab_edge(event)
            return
        message = self.query_one('#message', Pane)
        region = message.region
        if event.screen_y == region.y - 1 and region.x <= event.screen_x < region.right:
            message.grab_edge(event)

    def action_field_next(self: FractalApp) -> None:
        """Cycle the compose field cursor forward (tab)."""
        self.message_pane.field_cycle(1)

    def action_field_prev(self: FractalApp) -> None:
        """Cycle the compose field cursor backward (shift+tab)."""
        self.message_pane.field_cycle(-1)

    def on_option_list_option_selected(
        self: FractalApp,
        event: OptionList.OptionSelected,
    ) -> None:
        """Forward a dropdown pick to its owning pane."""
        if event.option_list.id == 'rdrop':
            self.radio_pane.pick_filter(str(event.option.prompt))
        elif event.option_list.id == 'rreact':
            self.radio_pane.pick_react(str(event.option.prompt))

    def on_input_changed(self: FractalApp, event: Input.Changed) -> None:
        """Re-filter an open combo as its field is typed into."""
        if self.mode == 'combo' and event.input.id == self.message_pane.combo_field:
            self.message_pane.filter_combo()

    def on_input_submitted(self: FractalApp, event: Input.Submitted) -> None:
        """Commit a combo pick, or end a field edit."""
        if self.mode == 'combo':
            self.message_pane.combo_pick()
            return
        self.message_pane.end_edit()

    def on_text_area_changed(self: FractalApp, event: TextArea.Changed) -> None:
        """Track the body's slash-command highlight."""
        if event.text_area.id == 'm_body':
            self.message_pane.highlight_slash(event.text_area.text)

    def _key_ring(self: FractalApp, event: Key) -> None:
        """Move the pane ring, enter the focused pane, or quit."""
        key = event.key
        if key in ('left', 'right', 'up', 'down'):
            self._ring(key)
            event.stop()
        elif key == 'enter':
            enters = {
                'fractal': self.tree_pane.enter,
                'radio': self.radio_pane.enter,
                'node': self.node_pane.enter,
                'message': self.message_pane.enter,
            }
            enter = enters.get(self.focus_id)
            if enter:
                enter()
                # repaint the border: the focus pane is now entered (accent)
                self.paint_ring()
            event.stop()
        elif key == 'q':
            event.stop()
            self.exit()

    def _ring(self: FractalApp, direction: str) -> None:
        """Step the focus ring: ``fractal / radio / node`` up, ``message`` below."""
        if self.focus_id in self.TOP:
            index = self.TOP.index(self.focus_id)
            if direction == 'left':
                self.focus_id = self.TOP[max(0, index - 1)]
            elif direction == 'right':
                self.focus_id = self.TOP[min(len(self.TOP) - 1, index + 1)]
            elif direction == 'down':
                self.focus_id = 'message'
        elif self.focus_id == 'message':
            # message sits bottom-left: up to the row above, right into the
            # floor-to-ceiling node pane
            if direction == 'up':
                self.focus_id = 'radio'
            elif direction == 'right':
                self.focus_id = 'node'
        self.paint_ring()

    def paint_ring(self: FractalApp) -> None:
        """Paint the focus pane's border: ring-selected (grey) vs entered (accent).

        In ``ring`` mode the focus pane is merely selected -- a body-text-grey
        cursor; once entered (any non-ring mode) it goes accent, so the border
        alone tells ring navigation apart from being inside a pane.
        """
        entered = self.mode != 'ring'
        for pane_id in [*self.TOP, 'message']:
            active = pane_id == self.focus_id
            pane = self.query_one(f'#{pane_id}')
            pane.set_class(active and not entered, 'ringsel')
            pane.set_class(active and entered, 'entered')
