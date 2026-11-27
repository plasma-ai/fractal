"""Test the ``fractal.tui.app`` module.

These pin the shell's own responsibilities -- the focus ring, the pane
geometry path, the chat-worker lifecycle (the silent-turn watchdog, turn
cancellation, tool/error events, shutdown cleanup), and the poll-worker
delivery (an off-thread build landing, the geometry re-apply, the staleness
guard, single-flight launch, the quiet-tick card repaint, the driven-mode
row repayment) -- as observable behavior, driven through a real ``Pilot``.
The chat-worker and poll-worker flows run on the writable pair tree; ring
navigation, geometry, and the quiet-tick clocks run on the canonical
read-only tree.
"""

from __future__ import annotations

import pathlib
import threading
from collections.abc import Callable

import pytest
from textual.widgets import Static

from fractal.cli.utils import resolve_node
from fractal.core.config import KEYS
from fractal.core.node import Node
from fractal.tui import theme
from fractal.tui.app import FractalApp, SnapshotReady
from fractal.tui.chat import ChatController, ChatEvent
from fractal.tui.panes.node import _CONFIG_ORDER
from fractal.tui.widgets import Pane

from ._doubles import MockTurn
from ._tree import NOW_EPOCH

__all__ = [
    'test_focus_ring_walks_every_pane',
    'test_q_quits_from_the_ring',
    'test_tab_outside_the_composer_leaves_real_focus_alone',
    'test_dragged_tree_width_survives_a_rescope_rebuild',
    'test_tree_opens_no_wider_than_the_node_pane',
    'test_tool_and_error_events_land_in_the_transcript',
    'test_silent_turn_is_cancelled_by_the_watchdog',
    'test_cancelling_an_in_flight_turn_notes_it_and_drops_the_spinner',
    'test_unmount_kills_the_in_flight_turn',
    'test_disk_change_lands_via_the_poll_worker',
    'test_change_absorbed_by_a_source_switch_reaches_the_tree',
    'test_poll_landed_geometry_resizes_the_node_pane',
    'test_stale_poll_result_is_dropped',
    'test_open_row_clocks_tick_through_a_quiet_tree',
    'test_tick_launches_one_build_at_a_time',
    'test_rows_skipped_while_driven_render_on_leave',
    'test_rows_skipped_while_driven_heal_on_a_quiet_tick',
    'test_config_chip_order_covers_every_config_key',
]


# ------ ring navigation (the canonical tree)


async def test_focus_ring_walks_every_pane(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Arrow keys on the ring reach all four panes; the focused pane highlights."""
    app = cockpit_app()
    async with app.run_test(size=(150, 48)) as pilot:
        assert app.focus_id == 'fractal'
        await pilot.press('right')  # fractal -> radio
        assert app.focus_id == 'radio'
        await pilot.press('right')  # radio -> node
        assert app.focus_id == 'node'
        await pilot.press('right')  # clamped at the right end of the top row
        assert app.focus_id == 'node'
        await pilot.press('down')  # node row -> message (bottom-left)
        assert app.focus_id == 'message'
        # the ring cursor paints the grey ring-selected border (not entered)
        assert app.query_one('#message').has_class('ringsel')
        assert not app.query_one('#message').has_class('entered')
        await pilot.press('right')  # message -> node (the floor-to-ceiling pane)
        assert app.focus_id == 'node'
        await pilot.press('left', 'left')  # node -> radio -> fractal
        assert app.focus_id == 'fractal'
        await pilot.press('down', 'up')  # down to message, back up to radio
        assert app.focus_id == 'radio'


async def test_q_quits_from_the_ring(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """``q`` in ring mode exits the app."""
    app = cockpit_app()
    async with app.run_test(size=(150, 48)) as pilot:
        assert app.mode == 'ring'
        await pilot.press('q')
        await pilot.pause()
    assert app.return_code == 0


@pytest.mark.parametrize(
    argnames=('keys', 'mode'),
    argvalues=[
        # the ring: nothing holds real focus
        pytest.param((), 'ring', id='ring'),
        # an open filter drop: the picker holds real focus
        pytest.param(('right', 'enter', 'down', 'enter'), 'rdrop', id='rdrop'),
    ],
)
async def test_tab_outside_the_composer_leaves_real_focus_alone(
    cockpit_app: Callable[..., FractalApp],
    keys: tuple[str, ...],
    mode: str,
) -> None:
    """``tab`` and ``shift+tab`` outside the composer change neither mode nor focus.

    Real focus belongs only to a field being edited or an open picker; a
    framework focus step would hand the keys to a compose field while the mode
    still drives another pane.
    """
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press(*keys)
        assert app.mode == mode
        focused = app.focused
        for key in ('tab', 'shift+tab'):
            await pilot.press(key)
            assert (app.mode, app.focused) == (mode, focused), key


# ------ pane geometry (the canonical tree)


async def test_dragged_tree_width_survives_a_rescope_rebuild(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """A dragged tree width overrides the snapshot geometry across rebuilds."""
    app = cockpit_app()
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.pause()
        tree = app.query_one('#fractal', Pane)
        auto = tree.region.width
        left = tree.region.x
        # grab the right border and drag it six columns out (screen coords)
        await pilot.mouse_down(tree, offset=(auto - 1, 5))
        await pilot.hover(None, offset=(left + auto + 5, 5))
        await pilot.mouse_up(None, offset=(left + auto + 5, 5))
        await pilot.pause()
        dragged = tree.region.width
        assert dragged == auto + 6
        # a re-scope re-applies the pane geometry: the dragged width wins
        await pilot.press('enter', 'down', 'enter', 'escape')
        await pilot.pause()
        assert app.scope == 'main.alpha'
        assert tree.region.width == dragged


async def test_tree_opens_no_wider_than_the_node_pane(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """The tree's opening width caps at the node pane's (dragging widens)."""
    app = cockpit_app()
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.pause()
        tree = app.query_one('#fractal')
        node = app.query_one('#node')
        assert tree.region.width <= node.region.width


# ------ chat-worker lifecycle (the writable pair tree)


async def test_tool_and_error_events_land_in_the_transcript(
    pair_tree: pathlib.Path,
) -> None:
    """A turn's tool and error events render as meta/error transcript lines."""
    events = [
        ChatEvent(kind='session', text='chat-sess'),
        ChatEvent(kind='tool', text='Bash'),
        ChatEvent(kind='error', text='rate limited'),
        ChatEvent(kind='meta', text='done · 1 turns · 0.1s · $0.01'),
    ]
    app = FractalApp(
        resolve_node(pair_tree),
        branch='main.alpha',
        turn_factory=lambda command, agent: MockTurn(events),
    )
    async with app.run_test(size=(150, 48)) as pilot:
        app.start_chat('use a tool please')
        for _ in range(100):
            await pilot.pause(0.05)
            if app.chat.turn is None:
                break
        convo = app.chat.transcript('main.alpha')
    # the tool use surfaces as a meta line, the error as an error line
    assert any(who == 'meta' and theme.TOOL in text for who, text in convo)
    assert any(who == 'error' and 'rate limited' in text for who, text in convo)


async def test_silent_turn_is_cancelled_by_the_watchdog(
    pair_tree: pathlib.Path,
) -> None:
    """A turn that streams nothing for too long is cancelled by ``_tick``.

    The watchdog cancels an in-flight turn that has been silent past the idle
    window rather than waiting on it forever; the transcript gets the cancel note.
    """
    # a turn that would hang (a long pause before its first event)
    events = [ChatEvent(kind='text', text='too late')]
    app = FractalApp(
        resolve_node(pair_tree),
        branch='main.alpha',
        turn_factory=lambda command, agent: MockTurn(events, pause=30.0),
    )
    # the controller's injectable clock: silence is now() since the last delta
    clock = {'at': 0.0}
    app.chat = ChatController(now=lambda: clock['at'])
    async with app.run_test(size=(150, 48)) as pilot:
        app.start_chat('are you there?')
        assert app.chat.turn is not None
        # jump the clock past the idle window, then tick
        clock['at'] = 1000.0
        app._tick()
        await pilot.pause()
        assert app.chat.turn is None  # the watchdog dropped the turn
        convo = app.chat.transcript('main.alpha')
        assert any(who == 'error' and 'silent' in text for who, text in convo)


async def test_cancelling_an_in_flight_turn_notes_it_and_drops_the_spinner(
    pair_tree: pathlib.Path,
) -> None:
    """Cancelling a live turn kills it, notes the cancel, and clears the spinner.

    This is the lever an interrupt pulls: the turn's subprocess is killed
    (worker cancellation alone cannot unblock a readline) and a ``cancelled``
    line lands in the transcript.
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
        app.start_chat('start a slow turn')
        turn = app.chat.turn
        assert turn is not None
        await pilot.pause()
        assert app.query('.chatpending')  # the spinner is pinned
        app._cancel_turn()  # the lever an interrupt pulls
        await pilot.pause()
        assert turn.cancelled  # the subprocess was killed
        assert app.chat.turn is None
        assert not app.query('.chatpending')  # the spinner left with the turn
        convo = app.chat.transcript('main.alpha')
        assert any(who == 'meta' and text == 'cancelled' for who, text in convo)


async def test_unmount_kills_the_in_flight_turn(pair_tree: pathlib.Path) -> None:
    """Shutting the app down cancels any in-flight turn (no orphan agents)."""
    events = [
        ChatEvent(kind='session', text='chat-sess'),
        ChatEvent(kind='text', text='slow'),
        ChatEvent(kind='meta', text='done · 1 turns · 0.1s · $0.01'),
    ]
    captured = []

    def factory(command: object, agent: object) -> MockTurn:
        turn = MockTurn(events, pause=5.0)
        captured.append(turn)
        return turn

    app = FractalApp(
        resolve_node(pair_tree),
        branch='main.alpha',
        turn_factory=factory,
    )
    async with app.run_test(size=(150, 48)) as pilot:
        app.start_chat('start a slow turn')
        assert app.chat.turn is not None
        await pilot.pause()
    # the app unmounted on context exit: the turn was cancelled, not orphaned
    assert captured
    assert captured[0].cancelled


# ------ poll-worker delivery (the writable pair tree)


async def test_disk_change_lands_via_the_poll_worker(
    pair_tree: pathlib.Path,
) -> None:
    """A tick builds off-thread and the changed snapshot lands as a message."""
    app = FractalApp(resolve_node(pair_tree), branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        before = app.snapshot
        app.actions.send(
            target='main.alpha',
            channel='public',
            subject='fresh',
            data='landed off-thread',
            priority=5,
        )
        app._tick()
        for _ in range(100):
            await pilot.pause(0.05)
            if app.snapshot is not before:
                break
        assert any(row['subject'] == 'fresh' for row in app.snapshot.messages)


async def test_change_absorbed_by_a_source_switch_reaches_the_tree(
    pair_tree: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disk change absorbed by a radio source switch still lands tree-wide.

    Cycling the source re-builds synchronously; when that build absorbs a
    pending disk change, the poll can never re-deliver it (a now-quiet tree
    keeps returning the same snapshot object, which the identity guard
    drops), so the switch itself must refresh every pane -- not just the
    radio rows.
    """
    # park the interval ticks: the test drives the one poll by hand, so the
    # pending change deterministically lands inside the switch's own build
    monkeypatch.setattr(theme, 'REFRESH_S', 3600.0)
    app = FractalApp(resolve_node(pair_tree), branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.pause()
        foot = str(app.query_one('#treefoot', Static).render())
        assert '0/1 nodes running' in foot
        await pilot.press('right', 'enter')  # into the radio pane's source tabs
        # alpha starts between polls; the source switch is what builds next
        Node(pair_tree / '.worktrees' / 'main.alpha').status_set('active')
        await pilot.press('right')  # Messages -> Feed (the synchronous build)
        app._tick()  # a quiet-tree poll: the same object, dropped on identity
        for _ in range(100):
            await pilot.pause(0.05)
            if app._poll_worker is not None and app._poll_worker.is_finished:
                break
        await pilot.pause()
        foot = str(app.query_one('#treefoot', Static).render())
        assert '1/1 nodes running' in foot


async def test_poll_landed_geometry_resizes_the_node_pane(
    pair_tree: pathlib.Path,
) -> None:
    """A poll-landed snapshot's widened geometry re-applies to the node pane.

    Poll-driven data can widen the computed node-pane width (a longer leaf
    name stretches the event-log node column); the landing must re-apply the
    geometry or the rows render wider than the pane and crop their
    right-anchored elapsed/cost cells.
    """
    app = FractalApp(resolve_node(pair_tree))
    async with app.run_test(size=(150, 48)) as pilot:
        before = app.snapshot
        # a long leaf name widens the descendants log's node column, and with
        # it the snapshot's computed node-pane width
        Node(pair_tree).init(name='periscope_depth_rotation', agent='claude')
        app._tick()
        for _ in range(100):
            await pilot.pause(0.05)
            if app.snapshot is not before:
                break
        await pilot.pause()
        node = app.query_one('#node')
        assert app.snapshot.geometry.node_width > before.geometry.node_width
        assert node.region.width == app.snapshot.geometry.node_width


async def test_stale_poll_result_is_dropped(pair_tree: pathlib.Path) -> None:
    """A worker result launched before a re-scope must not land over it.

    ``scope_to`` bumps the build generation, so a queued result carrying the
    old generation is dropped on arrival instead of yanking the cockpit back
    to the old scope's snapshot.
    """
    app = FractalApp(resolve_node(pair_tree))
    async with app.run_test(size=(150, 48)) as pilot:
        stale = app.snapshot  # root-scoped, from before the re-scope
        gen = app._build_gen  # the generation a pre-move launch captured
        app.scope_to('main.alpha')  # the user moved: bumps the generation
        app.post_message(SnapshotReady(gen, stale))
        await pilot.pause()
        assert app.snapshot.scope == 'main.alpha'
        assert app.snapshot is not stale


async def test_open_row_clocks_tick_through_a_quiet_tree(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """A steady tick repaints the open rows' elapsed without a disk write.

    An agent can sit inside one tool call for minutes without touching the
    database, so every tick lands the same snapshot object. The card's time
    cells and cap gauges must measure the render clock through that quiet
    window -- not freeze at the last disk write.
    """
    clock = {'at': NOW_EPOCH}
    app = cockpit_app(branch='main.alpha', now=lambda: clock['at'])
    async with app.run_test(size=(150, 48)) as pilot:
        snapshot = app.snapshot
        before = app.query_one('#nodemeasures', Static).render().plain
        assert '1h/10m' in before  # the open step, at the build instant
        # two silent hours pass: nothing moves on disk, so the tick posts
        # the same object and the identity short-circuit takes it
        clock['at'] = NOW_EPOCH + 7200.0
        app.post_message(SnapshotReady(app._build_gen, app.snapshot))
        await pilot.pause()
        assert app.snapshot is snapshot  # the quiet tree reused the object
        measures = app.query_one('#nodemeasures', Static).render().plain
        assert '3h/10m' in measures  # the open step's clock kept ticking
        assert '3h/2h' in measures  # the run clock crossed its cap live


async def test_tick_launches_one_build_at_a_time(
    pair_tree: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick never stacks a second poll build behind an in-flight one."""
    app = FractalApp(resolve_node(pair_tree), branch='main.alpha')
    release = threading.Event()
    calls = []
    real = app.builder.build

    def gated(*args: object, **kwargs: object) -> object:
        """Count the build, then hold it until the test releases the gate."""
        calls.append(1)
        release.wait(timeout=5.0)
        return real(*args, **kwargs)

    monkeypatch.setattr(app.builder, 'build', gated)
    async with app.run_test(size=(150, 48)) as pilot:
        app._tick()
        first = app._poll_worker
        assert first is not None
        # let the worker thread start and enter the gate
        for _ in range(100):
            await pilot.pause(0.05)
            if calls:
                break
        app._tick()  # the first build is gated mid-flight: no relaunch
        assert app._poll_worker is first
        assert len(calls) == 1
        release.set()


async def test_rows_skipped_while_driven_render_on_leave(
    pair_tree: pathlib.Path,
) -> None:
    """Rows a landing skipped under the user's cursor render on mode exit.

    While the user drives the radio rows a landing snapshot skips their
    rebuild (never yank rows out from under a cursor); leaving the mode must
    repay the skip -- a then-quiet tree posts no further landing to catch up.
    """
    app = FractalApp(resolve_node(pair_tree), branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press('right', 'enter', 'down', 'down')  # into the radio rows
        assert (app.mode, app.radio_pane.rfocus) == ('radio', 'rows')
        assert not app.query('#radiorows .rrow')  # the pair tree starts empty
        before = app.snapshot
        app.actions.send(
            target='main.alpha',
            channel='public',
            subject='fresh',
            data='landed mid-mode',
            priority=5,
        )
        app._tick()
        for _ in range(100):
            await pilot.pause(0.05)
            if app.snapshot is not before:
                break
        # the landing moved the snapshot but skipped the driven rows
        assert any(row['subject'] == 'fresh' for row in app.snapshot.messages)
        assert not app.query('#radiorows .rrow')
        await pilot.press('escape')  # back to the ring: the skip is repaid
        await pilot.pause()
        assert len(app.query('#radiorows .rrow')) == 1


async def test_rows_skipped_while_driven_heal_on_a_quiet_tick(
    pair_tree: pathlib.Path,
) -> None:
    """A steady tick repays a skipped rebuild after a non-leave mode exit.

    The card zone's chat fork jumps straight to the compose pane -- no
    ``leave`` runs -- and a quiet tree re-posts the same snapshot object
    forever, so the tick's unchanged-snapshot short-circuit itself must
    rebuild the rows the driven mode left stale.
    """
    app = FractalApp(resolve_node(pair_tree), branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press('right', 'right', 'enter')  # into the runs explorer
        assert (app.mode, app.node_pane.zone) == ('node', 'mid')
        assert not app.query('#nodeexplore .exrow')  # no runs yet
        evrows = len(app.query('#nodeevents .evrow'))
        before = app.snapshot
        alpha = Node(pair_tree / '.worktrees' / 'main.alpha')
        alpha.record.run_start()  # the node starts working while driven
        app._tick()
        for _ in range(100):
            await pilot.pause(0.05)
            if app.snapshot is not before:
                break
        # the landing moved the snapshot but skipped the driven explorer/log
        assert len(app.snapshot.history) == 1
        assert not app.query('#nodeexplore .exrow')
        await pilot.press('up', 'enter')  # card zone: fork exits without leave
        assert (app.mode, app.focus_id) == ('field', 'message')
        app._tick()
        for _ in range(100):
            await pilot.pause(0.05)
            if app.query('#nodeexplore .exrow'):
                break
        assert len(app.query('#nodeexplore .exrow')) == 1
        assert len(app.query('#nodeevents .evrow')) == evrows + 1


# ------ config chips


def test_config_chip_order_covers_every_config_key() -> None:
    """Every config key renders as a chip except the DB-anchor ``root``.

    A key added to ``config.KEYS`` but not to the pane's ``_CONFIG_ORDER``
    silently never shows in the cockpit. ``root`` is the node's database
    anchor, not a display setting, so it is the sole omission.
    """
    assert set(KEYS) - set(_CONFIG_ORDER) == {'root'}
