"""Compose-pane tests: the MESSAGE segment driven through a real ``Pilot``.

Each test plays the keys a user would press and asserts the composer's
observable state -- the kind, the visible fields, the field cursor, the combo
drop, the transcript -- not the widget internals. Read-only navigation runs on
the canonical tree; the radio-send flows (which write real messages) run on the
writable pair tree.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable

import pytest
from textual.widgets import Input, OptionList, TextArea

from fractal.cli.utils import resolve_node
from fractal.core.radio import Radio
from fractal.tui import theme
from fractal.tui.app import FractalApp
from fractal.tui.data import TuiData, leaf_of

__all__ = [
    'test_kind_toggle_flips_visible_fields',
    'test_field_grid_navigation_walks_rows_and_body',
    'test_tab_steps_the_field_cursor_through_the_visible_fields',
    'test_tab_ends_an_edit_and_steps_the_field_cursor',
    'test_tab_commits_the_highlighted_combo_option_and_steps',
    'test_type_to_edit_a_free_text_field',
    'test_combo_pick_sets_the_node_field',
    'test_combo_cancel_restores_the_prior_value',
    'test_combo_filters_by_typed_text',
    'test_chatscroll_mode_scrolls_the_transcript',
    'test_enter_sends_and_shift_enter_inserts_newline',
    'test_slash_commands_set_radio_fields',
    'test_radio_send_writes_a_message',
    'test_radio_reply_threads_under_the_parent',
    'test_radio_send_failure_surfaces_a_warning',
    'test_kind_toggle_hover_repaints',
]


# ------ read-only navigation (the canonical tree)


@pytest.mark.parametrize('start_kind', ['chat', 'radio'])
async def test_kind_toggle_flips_visible_fields(
    cockpit_app: Callable[..., FractalApp],
    start_kind: str,
) -> None:
    """Flipping the kind shows the other kind's fields (radio grows the subject)."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.message_pane
        pane.kind = start_kind
        pane.refresh_visibility()
        await pilot.press('down', 'enter')  # ring -> message, into the body
        assert app.mode == 'edit'
        await pilot.press('escape')  # back to field mode on the body
        # body -> the middle (convo/subject) -> the kind toggle (rows[0])
        await pilot.press('up', 'up')
        assert pane.cursor == 'm_kind'
        await pilot.press('enter')  # flip the kind
        flipped = 'radio' if start_kind == 'chat' else 'chat'
        assert pane.kind == flipped
        # radio reveals channel/thread/priority/subject; chat reveals the convo
        radio = flipped == 'radio'
        assert app.query_one('#cell_m_channel').display is radio
        assert app.query_one('#m_subjectrow').display is radio
        assert app.query_one('#cell_m_session').display is not radio
        assert app.query_one('#m_convo').display is not radio


async def test_field_grid_navigation_walks_rows_and_body(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Left/right walk the fields; down drops to the middle and body; up leaves."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.message_pane
        pane.kind = 'radio'  # the widest row (kind/node/channel/thread/priority)
        pane.refresh_visibility()
        await pilot.press('down', 'enter', 'escape')  # into field mode, on the body
        await pilot.press('up', 'up')  # body -> subject (middle) -> m_kind (rows[0])
        assert pane.cursor == 'm_kind'
        await pilot.press('right', 'right')  # kind -> node -> channel
        assert pane.cursor == 'm_channel'
        await pilot.press('right')  # -> thread
        assert pane.cursor == 'm_thread'
        await pilot.press('left')  # back to channel
        assert pane.cursor == 'm_channel'
        await pilot.press('down')  # the row -> the middle (subject) for radio
        assert pane.cursor == 'm_subject'
        await pilot.press('down')  # subject -> body
        assert pane.cursor == 'm_body'
        await pilot.press('up')  # body -> subject (the middle)
        assert pane.cursor == 'm_subject'
        await pilot.press('up')  # subject -> the row (rows[0] = m_kind)
        assert pane.cursor == 'm_kind'
        await pilot.press('up')  # up at the top row leaves to the ring
        assert app.mode == 'ring'


@pytest.mark.parametrize(
    argnames=('kind', 'fields'),
    argvalues=[
        # chat: the session field, and the transcript spliced in above the body
        pytest.param(
            'chat',
            ['m_kind', 'm_node', 'm_session', 'm_convo', 'm_body'],
            id='chat',
        ),
        # radio: the channel/thread/priority row, and the subject above the body
        pytest.param(
            'radio',
            [
                'm_kind',
                'm_node',
                'm_channel',
                'm_thread',
                'm_priority',
                'm_subject',
                'm_body',
            ],
            id='radio',
        ),
    ],
)
async def test_tab_steps_the_field_cursor_through_the_visible_fields(
    cockpit_app: Callable[..., FractalApp],
    kind: str,
    fields: list[str],
) -> None:
    """``tab`` steps the cursor through exactly the visible fields; ``shift+tab`` back.

    The cockpit owns both keys: the cursor wraps at either end and no widget
    takes real focus, which would strand the keys outside the mode machine.
    """
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.message_pane
        pane.kind = kind
        pane.refresh_visibility()
        await pilot.press('down', 'enter', 'escape')  # field mode on the body
        assert pane.cursor == 'm_body'
        # a lap forward: the body is last, so the first tab wraps to the kind
        forward = []
        for _ in fields:
            await pilot.press('tab')
            forward.append(pane.cursor)
        assert forward == fields
        # a lap back retraces it from the body
        backward = []
        for _ in fields:
            await pilot.press('shift+tab')
            backward.append(pane.cursor)
        assert backward == [*reversed(fields[:-1]), 'm_body']
        assert app.mode == 'field'
        assert app.focused is None


async def test_tab_ends_an_edit_and_steps_the_field_cursor(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """``tab``/``shift+tab`` while typing keep the text, end the edit, and step on."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.message_pane
        pane.kind = 'radio'
        pane.refresh_visibility()
        await pilot.press('down', 'enter')  # into the body, edit mode
        await pilot.press('h', 'i')
        await pilot.press('shift+tab')  # ends the edit, steps back to the subject
        assert (app.mode, pane.cursor) == ('field', 'm_subject')
        assert app.focused is None  # the body released its text-entry focus
        assert app.query_one('#m_body', TextArea).text == 'hi'
        await pilot.press('s')  # type-to-edit the subject
        assert app.mode == 'edit'
        await pilot.press('tab')  # ends the edit, steps on to the body
        assert (app.mode, pane.cursor) == ('field', 'm_body')
        assert app.focused is None
        assert app.query_one('#m_subject', Input).value == 's'


async def test_tab_commits_the_highlighted_combo_option_and_steps(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """``tab`` out of an open combo picks its highlighted option, then steps on.

    The combo's field holds filter text while the drop is open, so the pick is
    the highlighted option -- the top match once typed, else the current value.
    """
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.message_pane
        # field mode -> m_node, then type-to-open the combo seeded with 'g'
        await pilot.press('down', 'enter', 'escape', 'up', 'up', 'right')
        await pilot.press('g')
        assert app.mode == 'combo'
        await pilot.pause()
        branches = [row['branch'] for row in app.snapshot.tree]
        matches = [branch for branch in branches if 'g' in branch.lower()]
        await pilot.press('tab')  # picks the top match, steps on to the session
        assert (app.mode, pane.cursor) == ('field', 'm_session')
        assert not app.query('#m_combodrop')
        assert pane.node == matches[0]
        assert app.query_one('#m_node', Input).value == leaf_of(pane.node)
        # an untouched drop opens on the current value: tabbing through keeps it
        session = pane.session
        await pilot.press('enter')  # drop the session combo
        assert app.mode == 'combo'
        await pilot.pause()
        await pilot.press('tab')
        assert (app.mode, pane.cursor) == ('field', 'm_convo')
        assert pane.session == session


async def test_type_to_edit_a_free_text_field(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Typing a printable on a free-text field enters edit mode and inserts it."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.message_pane
        pane.kind = 'radio'
        pane.refresh_visibility()
        await pilot.press('down', 'enter', 'escape')  # field mode on the body
        await pilot.press('up', 'up')  # body -> subject -> m_kind (rows[0])
        await pilot.press(
            'right', 'right', 'right'
        )  # kind -> node -> channel -> thread
        assert pane.cursor == 'm_thread'  # a free-text field (no combo)
        await pilot.press('x')  # type-to-edit: enters edit and inserts the char
        assert app.mode == 'edit'
        assert 'x' in app.query_one('#m_thread', Input).value
        await pilot.press('escape')  # edit -> field
        assert app.mode == 'field'


async def test_combo_pick_sets_the_node_field(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Enter on the node field drops a combo; picking re-targets the node."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.message_pane
        await pilot.press('down', 'enter', 'escape')  # field mode on the body
        await pilot.press('up', 'up', 'right')  # body -> middle -> m_kind -> m_node
        assert pane.cursor == 'm_node'
        await pilot.press('enter')  # drop the node combo
        assert app.mode == 'combo'
        assert app.query('#m_combodrop')
        await pilot.press('down')  # move the highlight off the current node
        await pilot.press('enter')  # commit the pick
        assert app.mode == 'field'
        assert not app.query('#m_combodrop')
        # the node retargeted to a tree branch; the field shows that leaf
        branches = [row['branch'] for row in app.snapshot.tree]
        assert pane.node in branches
        assert app.query_one('#m_node', Input).value == leaf_of(pane.node)


async def test_combo_cancel_restores_the_prior_value(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Esc out of a combo restores the field's value and keeps the target."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.message_pane
        before = pane.node
        # field mode -> m_node, drop the combo
        await pilot.press('down', 'enter', 'escape', 'up', 'up', 'right')
        assert pane.cursor == 'm_node'
        await pilot.press('enter')  # drop the combo
        assert app.mode == 'combo'
        await pilot.press('down', 'down')  # move the highlight around
        await pilot.press('escape')  # cancel: nothing commits
        assert app.mode == 'field'
        assert not app.query('#m_combodrop')
        assert pane.node == before
        assert app.query_one('#m_node', Input).value == leaf_of(before)


async def test_combo_filters_by_typed_text(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Typing into a combo narrows the drop to matching options."""
    app = cockpit_app(branch='main')
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.message_pane
        # field mode -> m_node, then type-to-open the combo seeded with 'g'
        await pilot.press('down', 'enter', 'escape', 'up', 'up', 'right')
        assert pane.cursor == 'm_node'
        await pilot.press('g')
        assert app.mode == 'combo'
        await pilot.pause()
        drop = app.query_one('#m_combodrop', OptionList)
        # only branches containing 'g' survive the filter (gamma)
        branches = [row['branch'] for row in app.snapshot.tree]
        expected = [branch for branch in branches if 'g' in branch.lower()]
        assert drop.option_count == len(expected)
        await pilot.press('enter')  # commit the single match
        assert pane.node in expected


async def test_chatscroll_mode_scrolls_the_transcript(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Activating the convo zone enters chat-scroll; up/down pan the transcript."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        # seed enough transcript that it can actually scroll
        for index in range(40):
            app.chat.append('main.alpha', 'meta', f'line {index}')
        app.message_pane.rescope_convo()
        await pilot.pause()
        convo = app.query_one('#m_convo')
        convo.scroll_home(animate=False)
        await pilot.pause()
        # field mode -> the convo zone (chat's middle), then activate chat-scroll
        await pilot.press('down', 'enter', 'escape', 'up')
        assert app.message_pane.cursor == 'm_convo'
        await pilot.press('enter')
        assert app.mode == 'chatscroll'
        await pilot.press('down', 'down', 'down')
        assert convo.scroll_offset.y > 0
        await pilot.press('escape')
        assert app.mode == 'field'


async def test_enter_sends_and_shift_enter_inserts_newline(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """On a send-key terminal ``enter`` sends, ``shift+enter`` inserts a newline.

    The fixture pins ``_ENTER_SENDS`` true, so the composer treats plain
    ``enter`` as send and ``shift+enter`` as the literal newline.
    """
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.press('down', 'enter')  # into the body, edit mode
        body = app.query_one('#m_body', TextArea)
        await pilot.press('h', 'i')
        await pilot.press('shift+enter')  # a literal newline, body stays open
        await pilot.press('t', 'h', 'e', 'r', 'e')
        assert body.text == 'hi\nthere'
        assert app.mode == 'edit'  # shift+enter did not send
        await pilot.press('enter')  # send: the body clears, lands in the convo
        await pilot.pause()
        assert body.text == ''
        assert ('you', 'hi\nthere') in app.chat.transcript('main.alpha')


async def test_slash_commands_set_radio_fields(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """``/channel`` etc. flip to radio and set the named field straight from the body."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.message_pane
        body = app.query_one('#m_body', TextArea)
        await pilot.press('down', 'enter')  # into the body
        body.text = '/channel private'
        pane.send_body()
        await pilot.pause()
        assert pane.kind == 'radio'  # a radio-only slash flips the kind
        assert app.query_one('#m_channel', Input).value == 'private'
        body.text = '/priority 3'
        pane.send_body()
        await pilot.pause()
        assert app.query_one('#m_priority', Input).value == '3'
        body.text = '/subject ship it'
        pane.send_body()
        await pilot.pause()
        assert app.query_one('#m_subject', Input).value == 'ship it'


# ------ radio sends (the writable pair tree)


async def test_radio_send_writes_a_message(pair_tree: pathlib.Path) -> None:
    """A radio send from the composer lands a real message in the target's channel."""
    app = FractalApp(resolve_node(pair_tree), branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.message_pane
        pane.kind = 'radio'
        pane.refresh_visibility()
        app.query_one('#m_channel', Input).value = 'public'
        app.query_one('#m_subject', Input).value = 'status'
        await pilot.press('down', 'enter')  # into the body
        body = app.query_one('#m_body', TextArea)
        body.text = 'all systems go'
        pane.send_body()
        await pilot.pause()
        assert body.text == ''  # the body cleared after the send
        assert app.query_one('#m_subject', Input).value == ''  # so did the subject
    data = TuiData(resolve_node(pair_tree))
    data.refresh_worktrees()
    connection = data.connect()
    try:
        rows = data.rows(
            connection=connection,
            query='SELECT node, channel, sender, subject, data FROM messages',
        )
    finally:
        connection.close()
    assert rows == [
        {
            'node': 'main.alpha',
            'channel': 'public',
            'sender': 'main',
            'subject': 'status',
            'data': 'all systems go',
        }
    ]


async def test_radio_reply_threads_under_the_parent(pair_tree: pathlib.Path) -> None:
    """A send with the thread field set replies under that uuid, not a new row."""
    root = resolve_node(pair_tree)
    parent, _, _ = Radio(root).send(
        node='main.alpha',
        channel='public',
        subject='topic',
        data='please review',
        priority=5,
    )
    app = FractalApp(root, branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.message_pane
        pane.kind = 'radio'
        pane.refresh_visibility()
        app.query_one('#m_channel', Input).value = 'public'
        app.query_one('#m_thread', Input).value = parent  # a uuid -> reply path
        app.query_one('#m_subject', Input).value = 'Re: topic'
        await pilot.press('down', 'enter')
        body = app.query_one('#m_body', TextArea)
        body.text = 'looks good'
        pane.send_body()
        await pilot.pause()
        # the landed reply consumed its thread and subject -- the next send
        # starts fresh instead of threading under the same parent
        assert app.query_one('#m_thread', Input).value == theme.EMPTY
        assert app.query_one('#m_subject', Input).value == ''
    data = TuiData(root)
    data.refresh_worktrees()
    connection = data.connect()
    try:
        replies = data.rows(
            connection=connection,
            query='SELECT data FROM messages WHERE parent_message_uuid = ?',
            params=(parent,),
        )
    finally:
        connection.close()
    assert [row['data'] for row in replies] == ['looks good']


async def test_radio_send_failure_surfaces_a_warning(
    pair_tree: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected radio send surfaces the radio layer's error as a warning."""
    app = FractalApp(resolve_node(pair_tree), branch='main.alpha')
    notes: list[str] = []
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.message_pane
        pane.kind = 'radio'
        pane.refresh_visibility()
        monkeypatch.setattr(
            app, 'notify', lambda message, **_kwargs: notes.append(message)
        )
        # an unknown channel is refused by the radio layer
        app.query_one('#m_channel', Input).value = 'nope'
        app.query_one('#m_subject', Input).value = 'x'
        await pilot.press('down', 'enter')
        body = app.query_one('#m_body', TextArea)
        body.text = 'this should fail'
        pane.send_body()
        await pilot.pause()
        # the failure surfaced as a notify naming the bad channel
        assert any('nope' in note for note in notes)
        # the subject survived the failure for a fix-and-resend
        assert app.query_one('#m_subject', Input).value == 'x'
    # nothing landed in any node's database (the send was rejected)
    data = TuiData(resolve_node(pair_tree))
    data.refresh_worktrees()
    connection = data.connect()
    try:
        rows = data.rows(connection, 'SELECT message_uuid FROM messages')
    finally:
        connection.close()
    assert rows == []


async def test_kind_toggle_hover_repaints(
    cockpit_app: Callable[..., FractalApp],
) -> None:
    """Hovering the kind toggle brightens it; leaving dims it again."""
    app = cockpit_app(branch='main.alpha')
    async with app.run_test(size=(150, 48)) as pilot:
        pane = app.message_pane
        toggle = app.query_one('#m_kind')
        assert not pane.kind_hover
        # resting: the active chip uses the dim selected background, not the lit one
        resting = {str(span.style) for span in toggle.render().spans}
        assert not any(theme.LIT_ACTIVE in style for style in resting)
        pane.set_kind_hover(True)
        await pilot.pause()
        assert pane.kind_hover
        # hovered: the active chip brightens to the lit-active background
        hovered = {str(span.style) for span in toggle.render().spans}
        assert any(theme.LIT_ACTIVE in style for style in hovered)
        pane.set_kind_hover(False)
        await pilot.pause()
        assert not pane.kind_hover
