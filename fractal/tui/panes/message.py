"""Implements ``MessagePane`` -- the compose pane.

A segmented chat/radio composer (modes: ``field``/``edit``/``combo``/``chatscroll``):
``chat`` shows the per-branch transcript and streams agent replies; ``radio``
sends real messages (send or threaded reply) through ``TuiActions``. Fields
navigate as a grid, combo fields drop a filtered picker, and ``/node`` --
``/subject`` slash commands set fields straight from the body.
"""

from __future__ import annotations

import os
import sqlite3
import typing
from typing import Optional

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.events import Enter, Key, Leave
from textual.widgets import Input, Label, OptionList, Rule as Divider, Static, TextArea
from textual.widgets.text_area import TextAreaTheme

import fractal.tui.fmt
import fractal.tui.theme
from fractal.tui.data import leaf_of
from fractal.tui.widgets import PaneScroll

if typing.TYPE_CHECKING:
    from fractal.tui.app import FractalApp
    from fractal.tui.snapshot import Snapshot

__all__ = [
    'Composer',
    'KindToggle',
    'MessagePane',
]

# the compose fields: (id, label, area) -- m_kind is the chat/radio toggle
_MFIELDS = (
    ('m_kind', 'kind', 'row'),
    ('m_node', 'node', 'row'),
    ('m_session', 'session', 'row'),
    ('m_channel', 'channel', 'row'),
    ('m_thread', 'thread', 'row'),
    ('m_priority', 'priority', 'row'),
    ('m_subject', 'subject', 'subject'),
    ('m_body', 'body', 'body'),
)
_CHAT_ONLY = frozenset({'m_session'})
_RADIO_ONLY = frozenset({'m_channel', 'm_thread', 'm_priority', 'm_subject'})
_SLASH_COMMANDS = ('node', 'channel', 'thread', 'priority', 'subject')


class Composer(TextArea):
    """The MESSAGE body, with a terminal-aware send key.

    On a kitty-protocol terminal ``enter``/``ctrl+s`` send and ``shift+enter``
    inserts the newline; elsewhere ``enter`` is the newline and ``ctrl+s`` the
    only send (``shift+enter`` arrives as plain ``enter`` there).
    """

    def on_mount(self: Composer) -> None:
        """Register the slash-token style (everything else stays CSS-driven)."""
        self.register_theme(
            TextAreaTheme(
                name='fractal',
                syntax_styles={'slash': Style(color=fractal.tui.theme.PRIMARY)},
            )
        )
        self.theme = 'fractal'

    async def _on_key(self: Composer, event: Key) -> None:
        """Handle send/newline keys; ``up`` at the top edge leaves the body."""
        key = event.key
        if key == 'ctrl+s' or (key == 'enter' and _ENTER_SENDS):
            event.prevent_default()
            event.stop()
            self.app.message_pane.send_body()
        elif key in ('enter', 'shift+enter'):
            event.prevent_default()
            event.stop()
            self.insert('\n')
            self.scroll_cursor_visible()
        elif key in ('up', 'down'):
            # move between lines within the message; only up at the very top
            # bubbles out to the field above (down stays -- the body is last)
            before = self.cursor_location
            if key == 'up':
                self.action_cursor_up()
            else:
                self.action_cursor_down()
            if self.cursor_location != before or key == 'down':
                event.prevent_default()
                event.stop()
        else:
            await super()._on_key(event)


class KindToggle(Static):
    """The chat/radio segmented switch: brightens on mouse hover."""

    def on_enter(self: KindToggle, event: Enter) -> None:
        """Brighten on hover."""
        self.app.message_pane.set_kind_hover(True)

    def on_leave(self: KindToggle, event: Leave) -> None:
        """Dim when the pointer leaves."""
        self.app.message_pane.set_kind_hover(False)


class MessagePane:
    """The MESSAGE pane: kind toggle, fields, transcript, and the composer."""

    def __init__(self: MessagePane, app: FractalApp) -> None:
        """Initialize ``MessagePane``.

        Args:
            app: The owning cockpit app (widget access + mode flips).

        """
        self.app = app
        self.kind = 'chat'
        self.kind_hover = False
        self.cursor = 'm_kind'
        self.node = app.scope
        self.session = (list(app.snapshot.sessions) or ['-'])[0]
        self._streaming = False
        # the live in-flight spinner line; an owned reference, not a DOM id --
        # widget removal is deferred, so an id lookup can meet a dying spinner
        # and a same-id re-mount would collide with it
        self._pending_line: Optional[Static] = None
        # combo state
        self._cfid = ''
        self._cprev = ''
        self._ccurrent = ''
        self._cfiltered: list[str] = []

    def compose(self: MessagePane) -> ComposeResult:
        """Compose the pane interior."""
        with Horizontal(id='m_fields'):
            for fid, label, area in _MFIELDS:
                if area != 'row':
                    continue
                if fid == 'm_kind':
                    yield KindToggle(self._kind_markup(), id='m_kind')
                    continue
                with Horizontal(classes='cell', id=f'cell_{fid}'):
                    yield Label(label)
                    yield Input(self._default(fid), id=fid)
        yield Divider(id='m_chattop')
        yield PaneScroll(id='m_convo')
        yield Divider(id='m_chatline')
        with Horizontal(id='m_subjectrow'):
            yield Label('subject')
            yield Input(placeholder=fractal.tui.theme.EMPTY, id='m_subject')
        with Horizontal(id='m_composer'):
            yield Label(fractal.tui.theme.PROMPT, id='m_prompt')
            yield Composer(id='m_body', show_line_numbers=False)
        yield Static(self._hint(), id='m_hint')

    def _default(self: MessagePane, fid: str) -> str:
        """Return the field's initial value."""
        defaults = {
            'm_node': leaf_of(self.node),
            'm_channel': 'public',
            'm_thread': fractal.tui.theme.EMPTY,
            'm_priority': '10',
            'm_session': fractal.tui.fmt.trunc(self.session, fractal.tui.theme.SESS_W),
        }
        return defaults.get(fid, '')

    def show_node(self: MessagePane) -> None:
        """Show the node's leaf name; ``self.node`` keeps the full branch."""
        field = self.app.query_one('#m_node', Input)
        field.value = leaf_of(self.node)
        field.styles.width = len(field.value) + fractal.tui.theme.HUG_PAD

    def show_session(self: MessagePane) -> None:
        """Show the (truncated) session id; ``self.session`` keeps the full value."""
        truncated = fractal.tui.fmt.trunc(self.session, fractal.tui.theme.SESS_W)
        self.app.query_one('#m_session', Input).value = truncated

    def _kind_markup(self: MessagePane) -> str:
        """Render the segmented kind switch."""
        active = self.app.mode in ('field', 'edit', 'combo')
        cursor_lit = active and self.cursor == 'm_kind'
        lit = cursor_lit or self.kind_hover
        return fractal.tui.fmt.toggle(('chat', 'radio'), self.kind, lit)

    def paint_kind(self: MessagePane) -> None:
        """Repaint the kind toggle."""
        self.app.query_one('#m_kind', KindToggle).update(self._kind_markup())

    def set_kind_hover(self: MessagePane, hover: bool) -> None:
        """Track the toggle's mouse hover state."""
        self.kind_hover = hover
        self.paint_kind()

    def _opts(self: MessagePane, fid: str) -> Optional[list[str]]:
        """Return the field's combo options (``None`` for free-text fields)."""
        snap = self.app.snapshot
        if fid == 'm_node':
            return [row['branch'] for row in snap.tree]
        if fid == 'm_session':
            return list(snap.sessions) or ['-']
        if fid == 'm_channel':
            return [row['channel'] for row in snap.channels] or [
                'inbox',
                'outbox',
                'public',
                'private',
            ]
        if fid == 'm_priority':
            return [str(value) for value in range(10, 0, -1)]
        return None

    def _area(self: MessagePane, fid: str) -> str:
        """Return the field's grid area (row / subject / chat / body)."""
        if fid == 'm_convo':
            return 'chat'
        return next((area for f, label, area in _MFIELDS if f == fid), 'row')

    def _visible_fields(self: MessagePane) -> list[str]:
        """List the active kind's fields (chat splices the convo in)."""
        if self.kind == 'radio':
            return [f[0] for f in _MFIELDS if f[0] not in _CHAT_ONLY]
        fields = [f[0] for f in _MFIELDS if f[0] not in _RADIO_ONLY]
        fields.insert(fields.index('m_body'), 'm_convo')
        return fields

    def refresh_visibility(self: MessagePane) -> None:
        """Show the active kind's fields; radio grows the body, chat the convo."""
        # node/channel boxes hug their content (+1 column of air); every
        # mutation path funnels through here
        for fid in ('m_node', 'm_channel'):
            field = self.app.query_one(f'#{fid}', Input)
            field.styles.width = len(field.value) + fractal.tui.theme.HUG_PAD
        radio = self.kind == 'radio'
        self.app.query_one('#message').set_class(radio, 'radiokind')
        self.app.query_one('#cell_m_session').display = not radio
        self.app.query_one('#cell_m_channel').display = radio
        self.app.query_one('#cell_m_thread').display = radio
        self.app.query_one('#cell_m_priority').display = radio
        self.app.query_one('#m_subjectrow').display = radio
        self.app.query_one('#m_convo').display = not radio
        self.app.query_one('#m_chatline').display = not radio

    def _hint(self: MessagePane) -> str:
        """Render the hint line for the active input mode.

        Field, combo, and chat-scroll modes list their live keys; edit mode
        (and the resting pane) keeps the terminal-aware send-key hint.
        """
        if self.app.mode == 'field':
            return (
                f'[{fractal.tui.theme.DIM}]{fractal.tui.theme.LEFT}'
                f'{fractal.tui.theme.RIGHT}{fractal.tui.theme.UP}'
                f'{fractal.tui.theme.DOWN} fields {fractal.tui.theme.SEP}'
                f' {fractal.tui.theme.RET} edit {fractal.tui.theme.SEP} esc back[/]'
            )
        if self.app.mode == 'combo':
            return (
                f'[{fractal.tui.theme.DIM}]type to filter {fractal.tui.theme.SEP}'
                f' {fractal.tui.theme.UP}{fractal.tui.theme.DOWN} pick'
                f' {fractal.tui.theme.SEP} {fractal.tui.theme.RET} select'
                f' {fractal.tui.theme.SEP} esc cancel[/]'
            )
        if self.app.mode == 'chatscroll':
            return (
                f'[{fractal.tui.theme.DIM}]{fractal.tui.theme.UP}'
                f'{fractal.tui.theme.DOWN} scroll {fractal.tui.theme.SEP} esc back[/]'
            )
        if _ENTER_SENDS:
            return (
                f'[{fractal.tui.theme.DIM}]{fractal.tui.theme.SHIFT}'
                f'{fractal.tui.theme.RET} newline {fractal.tui.theme.SEP}'
                f' {fractal.tui.theme.RET}/{fractal.tui.theme.CTRL}s send'
                f' {fractal.tui.theme.SEP} /field[/]'
            )
        return (
            f'[{fractal.tui.theme.DIM}]{fractal.tui.theme.RET} newline'
            f' {fractal.tui.theme.SEP} {fractal.tui.theme.CTRL}s send'
            f' {fractal.tui.theme.SEP} /field[/]'
        )

    def paint_fields(self: MessagePane) -> None:
        """Paint the field cursor, composer light, dividers, and mode hint."""
        active = self.app.mode in ('field', 'edit', 'combo')
        for fid, *_ in _MFIELDS:
            if fid == 'm_kind':
                self.paint_kind()
            else:
                selected = active and fid == self.cursor
                self.app.query_one(f'#{fid}').set_class(selected, 'fsel')
        body = active and self.cursor == 'm_body'
        self.app.query_one('#m_prompt').set_class(body, 'fsel')
        # light the whole composer so the body highlight is a full rectangle
        self.app.query_one('#m_composer').set_class(body, 'bodylit')
        convo = self.app.query_one('#m_convo')
        convo_lit = self.app.mode == 'field' and self.cursor == 'm_convo'
        convo.set_class(convo_lit, 'zonefocus')
        lit = self.app.mode == 'chatscroll'
        self.app.query_one('#m_chattop').set_class(lit, 'litdiv')
        self.app.query_one('#m_chatline').set_class(lit, 'litdiv')
        self.app.query_one('#m_hint', Static).update(self._hint())

    def enter(self: MessagePane) -> None:
        """Enter the pane straight into the body, ready to type."""
        self.app.mode = 'edit'
        self.cursor = 'm_body'
        self.paint_fields()
        self.app.query_one('#m_body').focus()

    def leave(self: MessagePane) -> None:
        """Return to ring mode."""
        self.app.set_focus(None)
        self.app.mode = 'ring'
        self.paint_fields()
        self.app.paint_ring()

    def rescope(self: MessagePane, snap: Snapshot) -> None:
        """Re-point the compose at the new scope (node field, session, convo)."""
        self.node = snap.scope
        self.show_node()
        self.session = (list(snap.sessions) or ['-'])[0]
        self.show_session()
        self.rescope_convo()
        self.refresh_visibility()

    def key_field(self: MessagePane, event: Key) -> None:
        """Handle field mode: grid navigation, activation, type-to-edit."""
        key = event.key
        if key == 'escape':
            self.leave()
        elif key in ('left', 'right', 'up', 'down'):
            self._move_field(key)
        elif key == 'enter':
            self._activate()
        elif self.cursor in ('m_convo', 'm_kind'):
            return
        elif event.is_printable:
            if self._opts(self.cursor) is not None:
                self._open_combo(self.cursor, seed=event.character or '')
            else:
                widget = self.app.query_one(f'#{self.cursor}')
                self.app.mode = 'edit'
                widget.focus()
                if isinstance(widget, TextArea):
                    widget.insert(event.character or '')
                else:
                    widget.insert_text_at_cursor(event.character or '')
                self.paint_fields()
        else:
            return
        event.stop()

    def _move_field(self: MessagePane, direction: str) -> None:
        """Move the field cursor through the grid (row / middle / body)."""
        visible = self._visible_fields()
        rows = [fid for fid in visible if self._area(fid) == 'row']
        if 'm_subject' in visible:
            middle = 'm_subject'
        elif 'm_convo' in visible:
            middle = 'm_convo'
        else:
            middle = None
        area = self._area(self.cursor)
        if area == 'row':
            index = rows.index(self.cursor)
            if direction == 'left':
                self.cursor = rows[max(0, index - 1)]
            elif direction == 'right':
                self.cursor = rows[min(len(rows) - 1, index + 1)]
            elif direction == 'down':
                self.cursor = middle or 'm_body'
            elif direction == 'up':
                self.leave()
                return
        elif area in ('subject', 'chat'):
            if direction == 'up':
                self.cursor = rows[0]
            elif direction == 'down':
                self.cursor = 'm_body'
        else:
            if direction == 'up':
                self.cursor = middle or rows[0]
        self.paint_fields()

    def _activate(self: MessagePane) -> None:
        """Activate the cursor's field (toggle, scroll, combo, or edit)."""
        if self.cursor == 'm_kind':
            self._flip_kind()
        elif self.cursor == 'm_convo':
            self.app.mode = 'chatscroll'
            self.paint_fields()
        elif self._opts(self.cursor) is not None:
            self._open_combo(self.cursor)
        else:
            self.app.mode = 'edit'
            self.app.query_one(f'#{self.cursor}').focus()
            self.paint_fields()

    def _flip_kind(self: MessagePane) -> None:
        """Toggle chat <-> radio, re-show the fields, keep the cursor valid."""
        self.kind = 'radio' if self.kind == 'chat' else 'chat'
        self.refresh_visibility()
        if self.cursor not in self._visible_fields():
            self.cursor = 'm_kind'
        self.paint_fields()

    def key_edit(self: MessagePane, event: Key) -> None:
        """Handle edit mode: esc ends, up/down end + move."""
        key = event.key
        if key == 'escape':
            self.end_edit()
            event.stop()
        elif key in ('up', 'down'):
            self.end_edit()
            self._move_field(key)
            event.stop()

    def end_edit(self: MessagePane) -> None:
        """End edit mode back to field mode."""
        self.app.set_focus(None)
        self.app.mode = 'field'
        self.paint_fields()

    def key_chatscroll(self: MessagePane, event: Key) -> None:
        """Handle chat-scroll mode: up/down scroll the transcript, esc back."""
        key = event.key
        if key == 'escape':
            self.app.mode = 'field'
            self.paint_fields()
        elif key == 'up':
            self.app.query_one('#m_convo').scroll_up(animate=False)
        elif key == 'down':
            self.app.query_one('#m_convo').scroll_down(animate=False)
        else:
            return
        event.stop()

    def field_cycle(self: MessagePane, step: int) -> None:
        """Cycle the field cursor (tab / shift+tab)."""
        if self.app.mode not in ('field', 'edit', 'combo'):
            return
        if self.app.mode == 'edit':
            self.end_edit()
        elif self.app.mode == 'combo':
            self.combo_pick()
        visible = self._visible_fields()
        index = visible.index(self.cursor) if self.cursor in visible else 0
        self.cursor = visible[(index + step) % len(visible)]
        self.paint_fields()

    @property
    def combo_field(self: MessagePane) -> str:
        """Return the field id owning the open combo (``''`` when none)."""
        return self._cfid

    def _open_combo(self: MessagePane, fid: str, seed: str = '') -> None:
        """Open the field's combo drop (optionally seeded by a typed char)."""
        field = self.app.query_one(f'#{fid}', Input)
        self._cfid = fid
        self._cprev = field.value
        # the current full value, so the drop opens on it (not the top)
        fields = {'m_node': self.node, 'm_session': self.session}
        self._ccurrent = fields.get(fid, field.value)
        self.app.mode = 'combo'
        self.app.mount(OptionList(id='m_combodrop'))
        field.value = seed
        field.focus()
        self.paint_fields()
        self.app.call_after_refresh(self.filter_combo)

    def filter_combo(self: MessagePane) -> None:
        """Re-filter the combo options from the field's typed value."""
        drops = self.app.query('#m_combodrop')
        if not drops:
            return
        drop = drops.first(OptionList)
        options = self._opts(self._cfid) or []
        typed = self.app.query_one(f'#{self._cfid}', Input).value.strip().lower()
        self._cfiltered = [option for option in options if typed in option.lower()]
        drop.clear_options()
        if self._cfiltered:
            # no_wrap: options never wrap -- a screen-clamped drop ellipsizes
            drop.add_options(
                Text(option, no_wrap=True, overflow='ellipsis')
                for option in self._cfiltered
            )
            if not typed and self._ccurrent in self._cfiltered:
                drop.highlighted = self._cfiltered.index(self._ccurrent)
            else:
                drop.highlighted = 0
        region = self.app.query_one(f'#{self._cfid}').region
        # cap the drop to the space below the field (it scrolls internally)
        space = max(self.app.size.height - region.y - 2, 5)
        drop.styles.height = min(max(len(self._cfiltered), 1) + 2, space)
        # fit the widest option plus the drop chrome -- border and padding
        # (2 each) and the scrollbar column -- clamped to the screen edge
        widest = max((len(option) for option in options), default=6)
        available = max(self.app.size.width - region.x - 1, 14)
        drop.styles.width = min(max(widest + 5, 14), available)
        drop.styles.offset = (region.x, region.y + 1)

    def key_combo(self: MessagePane, event: Key) -> None:
        """Handle combo mode: up/down move the highlight, esc cancels."""
        key = event.key
        if key in ('up', 'down'):
            if self._cfiltered:
                drop = self.app.query_one('#m_combodrop', OptionList)
                highlighted = drop.highlighted or 0
                step = 1 if key == 'down' else -1
                drop.highlighted = (highlighted + step) % len(self._cfiltered)
            event.stop()
        elif key == 'escape':
            self._close_combo(commit=False)
            event.stop()

    def combo_pick(self: MessagePane) -> None:
        """Commit the highlighted combo option into the field."""
        field = self.app.query_one(f'#{self._cfid}', Input)
        if self._cfiltered:
            drop = self.app.query_one('#m_combodrop', OptionList)
            field.value = self._cfiltered[drop.highlighted or 0]
        self._close_combo(commit=True)

    def _close_combo(self: MessagePane, commit: bool) -> None:
        """Close the combo drop, committing or restoring the field value."""
        if not commit:
            self.app.query_one(f'#{self._cfid}', Input).value = self._cprev
        for drop in self.app.query('#m_combodrop'):
            drop.remove()
        self.app.set_focus(None)
        self.app.mode = 'field'
        if self._cfid == 'm_session':
            if commit:
                self.session = self.app.query_one('#m_session', Input).value.strip()
            self.show_session()
        if self._cfid == 'm_node':
            if commit:
                picked = self.app.query_one('#m_node', Input).value.strip()
                self.node = picked or self.node
            self.show_node()
        self.refresh_visibility()
        if self.cursor not in self._visible_fields():
            self.cursor = 'm_node'
        self.paint_fields()

    def rescope_convo(self: MessagePane) -> None:
        """Swap the transcript to the scoped branch's buffer."""
        convo = self.app.query_one('#m_convo')
        convo.remove_children()
        # the sweep took any spinner line with it
        self._pending_line = None
        for who, text in self.app.chat.transcript(self.app.scope):
            convo.mount(self._msg(who, text))
        self._streaming = False
        # a turn in flight on this branch keeps its spinner at the tail
        chat = self.app.chat
        if chat.turn is not None and chat.turn_branch == self.app.scope:
            self.show_pending()
        self.app.call_after_refresh(convo.scroll_end, animate=False)

    def _msg(self: MessagePane, who: str, text: str) -> Static:
        """Build one transcript bubble for a ``who`` line."""
        # agent text is pure data with no style wrapping: markup stays off
        # (matching the streamed bubble, where a tag can split across deltas
        # and escaping cannot cover the partial) so a stray tag never renders
        # or crashes the transcript
        if who == 'auth':
            return Static(text, classes='msg auth', markup=False)
        # every other bubble wraps its text in app-owned style markup; the
        # text itself is data (the prompt, a tool name, an error), so escape
        # it once here so a stray tag never renders or crashes the transcript
        text = fractal.tui.fmt.esc(text)
        if who == 'you':
            return Static(
                f'[{fractal.tui.theme.PRIMARY}]{fractal.tui.theme.PROMPT}[/] {text}',
                classes='msg you',
            )
        if who == 'error':
            return Static(f'[{fractal.tui.theme.ERROR}]{text}[/]', classes='msg')
        return Static(f'[{fractal.tui.theme.DIM}]{text}[/]', classes='msg')

    def post(self: MessagePane, branch: str, who: str, text: str) -> None:
        """Append a transcript line (buffer + widget when the branch is scoped)."""
        self.app.chat.append(branch, who, text)
        self._streaming = False
        if branch == self.app.scope:
            convo = self.app.query_one('#m_convo')
            convo.mount(self._msg(who, text), before=self._pending())
            self.app.call_after_refresh(convo.scroll_end, animate=False)

    def post_delta(self: MessagePane, branch: str, text: str) -> None:
        """Append a streamed agent delta (coalesced into the trailing bubble)."""
        self.app.chat.append_delta(branch, text)
        if branch != self.app.scope:
            return
        convo = self.app.query_one('#m_convo')
        buffered = self.app.chat.transcript(branch)[-1][1]
        # the trailing agent bubble sits just above the spinner, when present;
        # markup stays off (matching _msg's agent bubble) -- a delta boundary
        # can split a tag, so a raw buffer must never parse as markup
        bubbles = [
            child for child in convo.children if 'chatpending' not in child.classes
        ]
        if self._streaming and bubbles:
            bubbles[-1].update(buffered)
        else:
            convo.mount(
                Static(buffered, classes='msg auth', markup=False),
                before=self._pending(),
            )
            self._streaming = True
        self.app.call_after_refresh(convo.scroll_end, animate=False)

    def _pending(self: MessagePane) -> Optional[Static]:
        """Return the live in-flight spinner line (``None`` when idle)."""
        return self._pending_line

    def show_pending(self: MessagePane) -> None:
        """Pin the in-flight spinner line at the transcript tail."""
        self.clear_pending()
        convo = self.app.query_one('#m_convo')
        self._pending_line = Static('', classes='msg chatpending')
        convo.mount(self._pending_line)
        self.update_pending()
        self.app.call_after_refresh(convo.scroll_end, animate=False)

    def update_pending(self: MessagePane) -> None:
        """Advance the spinner frame and its elapsed figure."""
        pending = self._pending()
        if pending is None:
            return
        glyph = fractal.tui.theme.SPINNER[
            self.app.chat.spin_frame % len(fractal.tui.theme.SPINNER)
        ]
        seconds = int(self.app.chat.spin_elapsed())
        pending.update(
            f'[{fractal.tui.theme.DIM}]{glyph} thinking'
            f' {fractal.tui.theme.SEP} {seconds}s[/]'
        )

    def clear_pending(self: MessagePane) -> None:
        """Drop the spinner line (the turn finished or was cancelled)."""
        if self._pending_line is not None:
            self._pending_line.remove()
            self._pending_line = None

    def recall_send(self: MessagePane, branch: str, prompt: str) -> None:
        """Pull an unsent queued send back into the composer for editing.

        Drops the send's pending ``you`` line from the branch transcript and
        lands the text in the body, above any draft already typed there.
        """
        self.app.chat.recall(branch, prompt)
        if branch == self.app.scope:
            self.rescope_convo()
        body = self.app.query_one('#m_body', TextArea)
        body.text = f'{prompt}\n{body.text}' if body.text else prompt

    def send_body(self: MessagePane) -> None:
        """Send the body: a slash command, a chat turn, or a radio message."""
        body = self.app.query_one('#m_body', TextArea)
        text = body.text.strip()
        if not text:
            return
        if text.startswith('/'):
            self._apply_slash(text)
            body.text = ''
            return
        if self.kind == 'chat':
            self.app.start_chat(text)
        else:
            channel = self.app.query_one('#m_channel', Input).value.strip()
            thread = self.app.query_one('#m_thread', Input).value.strip()
            self._send_radio(channel, thread, text)
        body.text = ''

    def _send_radio(self: MessagePane, channel: str, thread: str, text: str) -> None:
        """Write a real radio message.

        A threaded reply when the thread field carries a uuid, else a send;
        failures surface as a notify and keep the fields for a re-send.
        """
        target = self.node or self.app.scope
        subject = self.app.query_one('#m_subject', Input).value.strip() or text[:42]
        priority_value = self.app.query_one('#m_priority', Input).value.strip()
        priority = int(priority_value) if priority_value.isdigit() else 10
        try:
            if thread and thread != fractal.tui.theme.EMPTY:
                self.app.actions.reply(
                    message_uuid=thread,
                    data=text,
                    priority=priority,
                )
                kind = 'reply'
            else:
                self.app.actions.send(
                    target=target,
                    channel=channel,
                    subject=subject,
                    data=text,
                    priority=priority,
                )
                kind = 'send'
        except (ValueError, PermissionError, sqlite3.Error) as error:
            self.app.notify(str(error), severity='warning')
            return
        # the landed message consumes its subject and thread -- a leftover
        # thread would silently re-thread the next send under the same parent
        self.app.query_one('#m_subject', Input).value = ''
        self.app.query_one('#m_thread', Input).value = fractal.tui.theme.EMPTY
        self.app.notify(
            f'radio {kind} {fractal.tui.theme.RIGHT} {leaf_of(target)}'
            f' {fractal.tui.theme.SEP} {channel}'
        )
        if self.app.mode not in ('radio', 'rdrop', 'rdetail', 'rreact'):
            self.app.refresh_radio()

    def _apply_slash(self: MessagePane, text: str) -> None:
        """Apply a ``/field value`` command to its field."""
        parts = text[1:].split(maxsplit=1)
        command = parts[0].lower() if parts else ''
        argument = parts[1].strip() if len(parts) > 1 else ''
        if command in _SLASH_COMMANDS:
            if command == 'node':
                # self.node needs a full branch, but the typed arg may be a
                # leaf (what the field shows) -- resolve it to a branch
                branch = self._resolve_node(argument)
                if branch is None:
                    self.app.notify(f'no node {argument!r}', severity='warning')
                    return
                self.node = branch
                self.show_node()
            else:
                self.kind = 'radio'
                self.app.query_one(f'#m_{command}', Input).value = argument
            self.refresh_visibility()
            self.paint_fields()

    def _resolve_node(self: MessagePane, argument: str) -> Optional[str]:
        """Resolve a ``/node`` argument to a full branch (``None`` if no match).

        Matches a full branch outright, else a unique leaf -- the same tree the
        node combo offers, so the two retarget paths agree.
        """
        branches = [row['branch'] for row in self.app.snapshot.tree]
        if argument in branches:
            return argument
        leaves = [branch for branch in branches if leaf_of(branch) == argument]
        return leaves[0] if len(leaves) == 1 else None

    def highlight_slash(self: MessagePane, value: str) -> None:
        """Light a recognized slash command (the token only, not its argument)."""
        text = value.strip()
        parts = text[1:].split(maxsplit=1) if text.startswith('/') else []
        command = parts[0].lower() if parts else ''
        body = self.app.query_one('#m_body', Composer)
        # private API: TextArea has no public span API -- style by
        # writing its private per-line highlight map
        body._highlights.clear()
        if command in _SLASH_COMMANDS:
            body._highlights[0].append((0, 1 + len(command), 'slash'))
        # private API: the rendered lines are cached; drop them so the new span paints
        body._line_cache.clear()
        body.refresh()

    def fork_session(self: MessagePane, entry: dict) -> None:
        """Fork a step's session: jump to the compose in chat mode."""
        session = entry.get('session') or '-'
        if session != '-':
            self.session = session
            self.show_session()
        self.kind = 'chat'
        self.app.mode = 'field'
        self.cursor = 'm_body'
        self.app.focus_id = 'message'
        self.refresh_visibility()
        self.app.paint_ring()
        self.paint_fields()

    def compose_reply(self: MessagePane, row: dict) -> None:
        """Pre-fill a radio reply to ``row`` and land in the body."""
        self.kind = 'radio'
        self.node = row['node']
        self.show_node()
        self.app.query_one('#m_channel', Input).value = row['channel']
        self.app.query_one('#m_thread', Input).value = row['message_uuid']
        subject = row['subject']
        if subject.lower().startswith('re: '):
            subject = subject[len('Re: ') :]
        subject = f'Re: {subject}'
        self.app.query_one('#m_subject', Input).value = subject
        self.refresh_visibility()
        self.app.focus_id = 'message'
        self.cursor = 'm_body'
        self.app.mode = 'edit'
        self.app.paint_ring()
        self.app.query_one('#m_body').focus()
        self.paint_fields()

    def compose_chat(self: MessagePane, row: dict, *, session: Optional[str]) -> None:
        """Chat with ``row``'s sender: re-scope to it and fork its session."""
        sender = row['sender']
        known = any(entry['branch'] == sender for entry in self.app.snapshot.tree)
        if known and sender != self.app.scope:
            self.app.scope_to(sender)
        self.kind = 'chat'
        if session:
            self.session = session
            self.show_session()
        self.refresh_visibility()
        self.app.focus_id = 'message'
        self.cursor = 'm_body'
        self.app.mode = 'edit'
        self.app.paint_ring()
        self.app.query_one('#m_body').focus()
        self.paint_fields()


# ------ helper functions


def _enter_sends() -> bool:
    """Return whether ``enter`` sends (the terminal delivers ``shift+enter`` distinctly).

    Textual pushes the kitty keyboard protocol at startup but never learns
    whether the terminal honored it, so capability comes from the terminal's
    identity: kitty, ghostty, WezTerm, and iTerm2 (3.5+) all deliver
    ``shift+enter`` as its own key. Anywhere else both keys arrive as plain
    ``enter``, so ``enter`` stays the newline and ``ctrl+s`` is the only send.
    """
    term = os.environ.get('TERM', '')
    if 'kitty' in term or 'ghostty' in term:
        return True
    return os.environ.get('TERM_PROGRAM') in ('WezTerm', 'ghostty', 'iTerm.app')


_ENTER_SENDS = _enter_sends()
