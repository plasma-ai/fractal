"""Interaction tests: the mode machine driven through a real ``Pilot``.

Each test plays the keys (or pointer gestures) a user would use and asserts
the cockpit's observable state -- the scope, the compose fields, the
transcript -- not the widget tree. Read-only flows run on the canonical tree;
the radio detail flow (whose open stamps a read receipt) runs on the writable
pair tree.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Callable

import pytest
from textual.widgets import Input, Static, TextArea

from fractal.cli.utils import resolve_node
from fractal.core.radio import Radio
from fractal.tui import fmt, theme
from fractal.tui.app import FractalApp
from fractal.tui.chat import ChatEvent

from ._doubles import MockTurn
from ._tree import session_for

__all__ = [
    'test_ring_enter_and_rescope',
    'test_hovering_a_truncated_tree_row_unfolds_it',
    'test_selected_tree_row_unfolds_without_hover',
    'test_explorer_fork_prefills_chat_session',
    'test_explorer_selection_time_machines_the_card',
    'test_event_log_row_opens_the_explorer',
    'test_event_log_subtree_toggle',
    'test_log_scope_defaults_and_persists_per_node',
    'test_scope_zone_sits_between_runs_and_log',
    'test_attach_without_a_live_session_notifies',
    'test_attach_headless_node_reports_log',
    'test_card_zone_chats_the_shown_session',
    'test_radio_reply_prefills_compose',
    'test_slash_node_resolves_a_leaf_to_a_full_branch',
    'test_pane_scrolling_is_independent',
    'test_chat_stream_coalesces_and_survives_rescope',
    'test_chat_stream_renders_hostile_markup_literally',
    'test_click_moves_a_list_cursor_without_activating',
    'test_click_on_the_highlighted_row_activates_it',
]


# ------ tree navigation and row unfold


async def test_ring_enter_and_rescope(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Enter the tree, pick a node, and the whole cockpit re-points at it."""
    app = cockpit_app()
    async with app.run_test(size=(150, 48)) as pilot:
        assert (app.mode, app.scope) == ('ring', 'main')
        await pilot.press('enter')  # into the tree pane
        assert app.mode == 'tree'
        rows_before = list(app.query('#treebody .treenode'))
        await pilot.press('down', 'enter')  # select main.alpha, re-scope
        assert app.scope == 'main.alpha'
        assert app.snapshot.card['branch'] == 'main.alpha'
        # a re-scope keeps the tree's shape: the rows swap markup in place
        # (a remount would empty the pane for a frame)
        assert list(app.query('#treebody .treenode')) == rows_before
        # the compose pane follows: leaf-named node field + the newest woven
        # session (the open iteration's, live as soon as its stream opened)
        assert app.query_one('#m_node', Input).value == 'alpha'
        assert app.message_pane.node == 'main.alpha'
        assert app.message_pane.session == session_for('main.alpha', 2, 2)
        await pilot.press('escape')
        assert app.mode == 'ring'


async def test_hovering_a_truncated_tree_row_unfolds_it(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Hovering a truncated tree row expands it to full text; leaving folds it."""
    app = cockpit_app()
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.pause()
        tree = app.query_one('#fractal')
        # drag the tree to its floor so the deep rows truncate
        await pilot.mouse_down(tree, offset=(tree.region.width - 1, 5))
        await pilot.hover(None, offset=(0, 5))
        await pilot.mouse_up(None, offset=(0, 5))
        await pilot.pause()
        rows = list(app.query('#treebody .trow'))
        row = rows[4]  # main.alpha.stopper: deeper than the floored pane fits
        assert not row.has_class('expanded')
        assert row.region.height == 1
        await pilot.hover(row)
        await pilot.pause()
        # the hovered row (and only it) unfolds and wraps to its full text
        assert row.has_class('expanded')
        assert row.region.height > 1
        assert not any(
            other.has_class('expanded') for other in rows if other is not row
        )
        # keyboard selection is untouched by the pointer
        assert app.mode == 'ring'
        await pilot.hover('#radio')
        await pilot.pause()
        assert not row.has_class('expanded')
        assert row.region.height == 1


async def test_selected_tree_row_unfolds_without_hover(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """The keyboard selection unfolds a truncated row -- no mouse needed.

    Hover requires any-motion mouse reporting some terminals lack; the
    selected row shares the unfold rule so the full text stays reachable.
    """
    app = cockpit_app()
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.pause()
        tree = app.query_one('#fractal')
        # drag the tree to its floor so the deep rows truncate
        await pilot.mouse_down(tree, offset=(tree.region.width - 1, 5))
        await pilot.hover(None, offset=(0, 5))
        await pilot.mouse_up(None, offset=(0, 5))
        await pilot.pause()
        await pilot.press('enter')  # into the tree pane
        await pilot.press('down', 'down', 'down', 'down')  # main.alpha.stopper
        await pilot.pause()
        rows = list(app.query('#treebody .trow'))
        row = rows[4]
        assert row.has_class('tsel')
        assert row.region.height > 1
        # the wrapped label hangs under its own start column, past the glyphs
        head = row.query_one('.tprefix')
        label = row.query_one('.tlabel')
        assert label.region.x == head.region.right
        assert label.region.x > row.region.x
        # the head fills the wrapped height -- its hang lines keep the
        # tree's vertical glyphs continuous through the unfolded row
        assert head.region.height == label.region.height
        await pilot.press('escape')  # leaving tree mode folds the selection
        await pilot.pause()
        assert row.region.height == 1


# ------ the runs explorer and event log


async def test_explorer_fork_prefills_chat_session(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Enter on an explorer step jumps to compose with that step's session."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press('right', 'right')  # ring: fractal -> radio -> node
        assert app.focus_id == 'node'
        await pilot.press('enter')  # into the runs explorer
        assert app.mode == 'node'
        await pilot.press('right')  # expand run 2 (newest first)
        await pilot.press('down', 'right')  # onto iter 2 (live), expand it
        await pilot.press('down', 'enter')  # step 1 -> fork its session
        forked = session_for('main.alpha', 2, 2)
        assert (app.mode, app.focus_id) == ('field', 'message')
        assert app.message_pane.kind == 'chat'
        assert app.message_pane.session == forked
        truncated = fmt.trunc(forked, theme.SESS_W)
        assert app.query_one('#m_session', Input).value == truncated


async def test_explorer_selection_time_machines_the_card(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Highlighting an explorer row re-points the card at that run/iter/step."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press('right', 'right', 'enter')  # into the runs explorer
        run_line = app.query_one('#noderun', Static)
        assert 'run 2' in str(run_line.render())  # the live run is selected
        await pilot.press('down')  # run 1 (settled)
        text = str(run_line.render())
        assert 'run 1' in text
        assert 'step 5/5 (COMMIT)' in text
        measures = app.query_one('#nodemeasures', Static).render()
        assert '$0.42/' in measures.plain  # run 1's settled spend
        await pilot.press('escape')  # leaving snaps back to the live context
        assert 'run 2' in str(run_line.render())


async def test_event_log_row_opens_the_explorer(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Enter in the log starts row selection; enter on a row reveals its entity."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press('right', 'right', 'enter')  # into the runs explorer
        await pilot.press('down', 'down')  # past the runs to the scope toggle
        await pilot.press('down')  # past the toggle into the log cursor
        pane = app.node_pane
        assert pane.zone == 'rows'
        await pilot.pause()
        # the selected row unfolds to its full text
        assert app.query('#nodeevents .evrow.expanded')
        await pilot.press('enter')  # the newest row: the open step's start
        assert pane.zone == 'mid'
        rows = pane._ex_rows(app.snapshot)
        entry = pane._ex_entry(app.snapshot, rows[pane.ex_sel])
        assert entry['label'] == 'step 3: EXECUTE'
        # the card follows the opened step's context
        run_line = str(app.query_one('#noderun', Static).render())
        assert 'step 3/5 (EXECUTE)' in run_line


async def test_event_log_subtree_toggle(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """``t`` in the log merges descendant activity; ``t`` again restores it.

    A merged foreign row's enter is a no-op -- its entity lives in another
    node's explorer, so the reveal must not jump (or crash) the scope's.
    """
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press('right', 'right', 'enter')  # into the runs explorer
        await pilot.press('down', 'down')  # past the runs to the scope toggle
        await pilot.press('down')  # past the toggle into the log cursor
        pane = app.node_pane
        assert pane.zone == 'rows'
        assert {row['branch'] for row in app.snapshot.log} == {'main.alpha'}
        await pilot.press('t')  # merge the subtree into the timeline
        assert pane.sub_log
        branches = {row['branch'] for row in app.snapshot.log}
        assert {'main.alpha.deep', 'main.alpha.stopper'} <= branches
        # enter on a foreign row is a no-op (global ids never match the scope's)
        foreign = next(
            index
            for index, row in enumerate(app.snapshot.log)
            if row['branch'] != 'main.alpha'
        )
        pane.ev_sel = foreign
        await pilot.press('enter')
        assert pane.zone == 'rows'
        await pilot.press('t')  # restore the scoped log
        assert not pane.sub_log
        assert {row['branch'] for row in app.snapshot.log} == {'main.alpha'}
        # the toggle chip flips the scope too
        await pilot.click('#nodelogscope')
        await pilot.pause()
        assert pane.sub_log
        assert {'main.alpha.deep', 'main.alpha.stopper'} <= {
            row['branch'] for row in app.snapshot.log
        }


async def test_log_scope_defaults_and_persists_per_node(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """First visits get the default log scope; a toggled choice sticks.

    The user (root) node opens on the merged descendants view, every other
    node on its own activity; once toggled, a branch keeps its choice across
    rescopes for the session while untouched branches keep the defaults.
    """
    app = cockpit_app()
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.pause()
        pane = app.node_pane
        # the user (root) node defaults to the merged descendants view
        assert pane.sub_log
        branches = {row['branch'] for row in app.snapshot.log}
        assert 'main.alpha' in branches
        # a child's first visit defaults to its own activity
        await pilot.press('enter', 'down', 'enter')  # tree: select main.alpha
        await pilot.pause()
        assert app.scope == 'main.alpha'
        assert not pane.sub_log
        assert {row['branch'] for row in app.snapshot.log} == {'main.alpha'}
        # toggle to descendants, look away, come back: the choice survived
        await pilot.click('#nodelogscope')
        await pilot.pause()
        assert pane.sub_log
        app.scope_to('main')
        await pilot.pause()
        assert pane.sub_log  # the untoggled root keeps its own default
        app.scope_to('main.alpha')
        await pilot.pause()
        assert pane.sub_log
        assert {'main.alpha.deep', 'main.alpha.stopper'} <= {
            row['branch'] for row in app.snapshot.log
        }
        # an untoggled sibling's first visit still defaults to its own activity
        app.scope_to('main.beta')
        await pilot.pause()
        assert not pane.sub_log


async def test_scope_zone_sits_between_runs_and_log(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """The ladder lands on the scope toggle: runs -> toggle -> log rows."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press('right', 'right', 'enter')  # into the runs explorer
        pane = app.node_pane
        await pilot.press('down', 'down')  # past the runs to the scope toggle
        assert pane.zone == 'scope'
        assert not pane.sub_log
        await pilot.press('right')  # flip to descendants
        assert pane.sub_log
        await pilot.press('down')  # into the log rows
        assert pane.zone == 'rows'
        await pilot.press('up')  # back up to the toggle
        assert pane.zone == 'scope'
        await pilot.press('up')  # and back to the runs tree (last row)
        assert pane.zone == 'mid'


# ------ reaching the scoped session


async def test_attach_without_a_live_session_notifies(
    cockpit_app: Callable[..., FractalApp],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """^a works from anywhere; without a live tmux session it warns."""
    app = cockpit_app(branch='main.alpha.deep.leaf')  # a settled node
    async with app.run_test(size=(150, 48)) as pilot:
        monkeypatch.setattr(app.data, 'live_sessions', frozenset)
        notes: list[str] = []
        monkeypatch.setattr(
            app,
            'notify',
            lambda message, **kwargs: notes.append(message),
        )
        assert app.mode == 'ring'  # straight from the ring, no pane needed
        await pilot.press('ctrl+a')
        await pilot.pause()
        assert notes == ['no running session']


async def test_attach_headless_node_reports_log(
    pair_tree: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """^a on a headless node points at its captured output."""
    app = FractalApp(resolve_node(pair_tree), branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        node_dir = app.data.node_dir('main.alpha')
        assert node_dir is not None
        (node_dir / '.headless').write_text('headless\n', encoding='utf-8')
        notes: list[str] = []
        monkeypatch.setattr(
            app,
            'notify',
            lambda message, **kwargs: notes.append(message),
        )
        await pilot.press('ctrl+a')
        await pilot.pause()
        assert notes == [f'headless output: {node_dir / "headless.log"}']


async def test_card_zone_chats_the_shown_session(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Up from the runs tree highlights the card; enter chats its session."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press('right', 'right', 'enter')  # into the runs explorer
        await pilot.press('up')  # the card becomes the focused zone
        assert app.node_pane.zone == 'top'
        await pilot.press('enter')  # chat against the card's session
        assert (app.mode, app.focus_id) == ('field', 'message')
        assert app.message_pane.kind == 'chat'
        assert app.message_pane.session == session_for('main.alpha', 2, 2)


# ------ compose prefills and commands


async def test_radio_reply_prefills_compose(pair_tree: pathlib.Path) -> None:
    """Reply from the message detail pre-fills a threaded radio compose."""
    root = resolve_node(pair_tree)
    uuid, _, _ = Radio(root).send(
        node='main.alpha',
        channel='public',
        subject='review me',
        data='please look at the diff',
        priority=7,
    )
    app = FractalApp(root, branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press('right', 'enter')  # ring -> radio, enter on sources
        assert app.mode == 'radio'
        await pilot.press('down', 'down')  # source -> filter -> the rows
        assert app.radio_pane.rfocus == 'rows'
        await pilot.press('enter')  # open the message detail
        assert app.mode == 'rdetail'
        await pilot.press('enter')  # Reply (the first action)
        assert (app.mode, app.focus_id) == ('edit', 'message')
        # the message stays open for reference while the reply composes, and
        # re-entering the radio pane returns to it
        assert app.query_one('#rdetail').display
        pane = app.message_pane
        assert pane.kind == 'radio'
        assert app.query_one('#m_node', Input).value == 'alpha'
        assert pane.node == 'main.alpha'
        assert app.query_one('#m_channel', Input).value == 'public'
        assert app.query_one('#m_thread', Input).value == uuid
        assert app.query_one('#m_subject', Input).value == 'Re: review me'
        await pilot.press('escape', 'escape', 'up', 'enter')
        assert app.mode == 'rdetail'
        await pilot.press('escape')
        assert app.mode == 'radio'
        # observing another node's mailbox never touches its read state
        connection = app.data.connect()
        try:
            readers = app.data.rows(connection, 'SELECT node FROM reads')
        finally:
            connection.close()
        assert readers == []


async def test_slash_node_resolves_a_leaf_to_a_full_branch(
    cockpit_app: Callable[..., FractalApp],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/node <leaf>`` retargets to the registered branch the leaf names.

    The node field shows leaves, so a leaf is the natural argument -- but the
    send target must be a full branch ``Radio`` can resolve. A leaf that maps
    to one branch retargets; an unknown name is refused and leaves the target.
    """
    app = cockpit_app(branch='main')
    notes: list[str] = []
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.message_pane
        monkeypatch.setattr(
            app,
            'notify',
            lambda message, **_kwargs: notes.append(message),
        )
        # a leaf resolves to its full branch (the field still shows the leaf)
        body = app.query_one('#m_body', TextArea)
        body.text = '/node gamma'
        pane.send_body()
        await pilot.pause()
        assert pane.node == 'main.gamma'
        assert app.query_one('#m_node', Input).value == 'gamma'
        # a full branch passes through unchanged
        body.text = '/node main.alpha.deep'
        pane.send_body()
        await pilot.pause()
        assert pane.node == 'main.alpha.deep'
        # an unknown name is refused: the target holds and a warning surfaces
        body.text = '/node nope'
        pane.send_body()
        await pilot.pause()
        assert pane.node == 'main.alpha.deep'
        assert any('nope' in note for note in notes)


# ------ pane scrolling


async def test_pane_scrolling_is_independent(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Scrolling one pane never moves another (a historical mockup bug)."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        # seed enough transcript that the convo can actually scroll, then park
        # it at the top so any leak would show
        for index in range(40):
            app.chat.append('main.alpha', 'meta', f'line {index}')
        app.message_pane.rescope_convo()
        await pilot.pause()
        convo = app.query_one('#m_convo')
        convo.scroll_home(animate=False)
        await pilot.pause()
        assert convo.scroll_offset.y == 0
        # tree-mode movement leaves the transcript alone
        await pilot.press('enter', 'down', 'down', 'down', 'escape')
        assert convo.scroll_offset.y == 0
        # walking the log cursor scrolls only the log (and only once the
        # cursor crosses the viewport edge)
        await pilot.press('right', 'right', 'enter', 'down', 'down')
        log = app.query_one('#nodeevents')
        assert log.scroll_offset.y == 0
        for _ in range(30):
            await pilot.press('down')
        assert log.scroll_offset.y > 0
        assert convo.scroll_offset.y == 0
        await pilot.press('escape')
        # chat-scroll moves only the transcript
        log_before = log.scroll_offset.y
        await pilot.press('down', 'enter', 'escape', 'up', 'enter')
        assert app.mode == 'chatscroll'
        await pilot.press('down', 'down')
        assert convo.scroll_offset.y > 0
        assert log.scroll_offset.y == log_before
        # a poll-driven rebuild (disk moved while elsewhere) holds positions
        os.utime(app.data.node_dir('main.alpha') / '.status')
        app._tick()
        await pilot.pause()
        assert log.scroll_offset.y == log_before
        assert convo.scroll_offset.y > 0
        # a click can never focus a scroller (whose arrow bindings would then
        # shadow the mode machine and double-drive every cursor key)
        app.set_focus(app.query_one('#radiorows'))
        assert app.focused is None


# ------ the chat stream


async def test_chat_stream_coalesces_and_survives_rescope(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Deltas land in the branch buffer even while the cockpit looks away."""
    leaf = 'main.alpha.deep.leaf'
    events = [
        ChatEvent(kind='session', text='chat-sess-1'),
        ChatEvent(kind='text', text='Hel'),
        ChatEvent(kind='text', text='lo'),
        ChatEvent(kind='meta', text='done · 1 turns · 0.1s · $0.01'),
    ]
    app = cockpit_app(
        branch=leaf,
        turn_factory=lambda command, agent: MockTurn(events, pause=0.02),
    )
    async with app.run_test(size=(150, 48)) as pilot:
        # the compose session defaults to the leaf's last loop session, so the
        # turn forks it (an explicit session always wins the transport)
        app.start_chat('hi there')
        assert app.query('.chatpending')  # the in-flight spinner is pinned
        app.scope_to('main')  # look away mid-stream
        for _ in range(100):
            await pilot.pause(0.05)
            if app.chat.turn is None:
                break
        assert not app.query('.chatpending')  # the spinner left with the turn
        convo = app.chat.transcript(leaf)
        assert convo[0] == ('you', 'hi there')
        forked = session_for(leaf, 1, 1)
        assert convo[1] == ('meta', f'{theme.SEP} forked session {forked}')
        # the deltas coalesced into exactly one agent bubble; nothing dropped
        assert [text for who, text in convo if who == 'auth'] == ['Hello']
        assert convo[-1] == ('meta', 'done · 1 turns · 0.1s · $0.01')
        assert app.chat.session(leaf) == 'chat-sess-1'
        # re-scoping back replays the whole buffer into the transcript
        app.scope_to(leaf)
        await pilot.pause()
        assert len(app.query_one('#m_convo').children) == len(convo)


async def test_chat_stream_renders_hostile_markup_literally(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """A streamed delta carrying markup renders literally, never crashes.

    Agent text reaches the transcript live (delta by delta) as well as via
    the rescope replay, and neither path may parse it as markup: a stray
    ``[/]`` raises ``MarkupError`` mid-turn, ``[link=...]`` injects a live
    terminal hyperlink, and a tag split at a delta boundary (or a message
    ending mid-tag) defeats escaping -- the partial ``[/`` stays unescaped
    yet still parses as a tag.
    """
    hostile = 'agent says [/] oops [link=file:///etc/passwd]open[/link] mid [/'
    events = [
        ChatEvent(kind='text', text='agent says [/'),
        ChatEvent(kind='text', text='] oops [link=file:///etc/passwd]open[/link]'),
        ChatEvent(kind='text', text=' mid [/'),
        ChatEvent(kind='meta', text='done · 1 turns · 0.1s · $0.01'),
    ]
    app = cockpit_app(
        branch='main.alpha',
        turn_factory=lambda command, agent: MockTurn(events, pause=0.02),
    )
    async with app.run_test(size=(150, 48)) as pilot:
        app.start_chat('what are you doing?')
        for _ in range(100):
            await pilot.pause(0.05)
            if app.chat.turn is None:
                break
        # both streaming paths rendered (the first delta mounts the bubble,
        # the second coalesces into it); the shown text is the literal string
        bubble = app.query('#m_convo .auth').last(Static)
        assert str(bubble.render()) == hostile
        # the buffer keeps the raw text -- escaping is render-site only, so
        # the rescope replay through the transcript escapes exactly once
        transcript = app.chat.transcript('main.alpha')
        assert [text for who, text in transcript if who == 'auth'] == [hostile]
        app.scope_to('main')
        app.scope_to('main.alpha')
        await pilot.pause()
        replayed = app.query('#m_convo .auth').last(Static)
        assert str(replayed.render()) == hostile


# ------ pointer clicks


async def test_click_moves_a_list_cursor_without_activating(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Clicking a list row lands the cursor there -- activation excluded.

    A row click is "arrow there", never "enter": the tree keeps its scope,
    a message row opens no detail, and an explorer row forks no session.
    """
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        # a tree row: the cursor lands on it (the pane reads entered), the
        # cockpit does NOT re-scope
        rows = list(app.query('#treebody .treenode'))
        index = app.tree_pane._branches.index('main.alpha.stopper')
        await pilot.click(rows[index])
        await pilot.pause()
        assert (app.mode, app.focus_id) == ('tree', 'fractal')
        assert app.tree_pane.sel == index
        assert app.scope == 'main.alpha'  # unchanged
        assert rows[index].has_class('tsel')
        assert app.query_one('#fractal').has_class('entered')
        # a message row: the cursor lands in the rows zone, no detail opens
        rrows = list(app.query('#radiorows .rrow'))
        await pilot.click(rrows[1])
        assert (app.mode, app.radio_pane.rfocus) == ('radio', 'rows')
        assert app.radio_pane.rsel == 1
        assert not app.query_one('#rdetail').display
        # an explorer row: the card time-machines (as down would), no fork
        exrows = list(app.query('#nodeexplore .exrow'))
        await pilot.click(exrows[1])  # run 1 (newest-first: the settled run)
        assert (app.mode, app.node_pane.zone) == ('node', 'mid')
        assert app.node_pane.ex_sel == 1
        assert 'run 1' in str(app.query_one('#noderun', Static).render())
        # an event-log row: the cursor moves to the log's rows zone
        evrows = list(app.query('#nodeevents .evrow'))
        await pilot.click(evrows[2])
        assert (app.mode, app.node_pane.zone) == ('node', 'rows')
        assert app.node_pane.ev_sel == 2
        # from the composer: a row click releases the text-entry focus, so
        # keys reach the cockpit again
        await pilot.press('escape')  # back to the ring
        await pilot.press('down', 'enter')  # into the composer body
        assert (app.mode, app.focus_id) == ('edit', 'message')
        assert app.focused is not None
        await pilot.click(rrows[0])
        assert app.focused is None
        assert (app.mode, app.radio_pane.rsel) == ('radio', 0)


async def test_click_on_the_highlighted_row_activates_it(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """A second click on the already-highlighted row acts like enter."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        # radio: the first click moves the cursor, the second opens the detail
        rrows = list(app.query('#radiorows .rrow'))
        await pilot.click(rrows[1])
        assert (app.mode, app.radio_pane.rsel) == ('radio', 1)
        assert not app.query_one('#rdetail').display
        await pilot.click(rrows[1])
        assert app.mode == 'rdetail'
        assert app.query_one('#rdetail').display
        # tree: the second click re-scopes to the highlighted node
        rows = list(app.query('#treebody .treenode'))
        index = app.tree_pane._branches.index('main.alpha.stopper')
        await pilot.click(rows[index])
        assert (app.mode, app.scope) == ('tree', 'main.alpha')  # cursor only
        await pilot.click(rows[index])
        assert app.scope == 'main.alpha.stopper'  # the re-click enters
