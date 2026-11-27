---
name: features/tui/panes
desc: |
  The cockpit's four-pane grid -- tree, radio, node, and message -- the
  focus-ring navigation that moves between them and drills into each pane's
  interior zones, and the keyboard mode machine behind it.
created: 2026-07-21T05:08:54Z
updated: 2026-11-27T22:49:51Z
---

# features/tui/panes

[[features/tui/_index|..]]

***

`fractal open` composes a single screen holding four panes: the tree pane and
radio pane across the top row with the node pane at the right, and the message
(compose) pane at the bottom left. The app shell (`fractal.tui.app`) owns
composition, theming, and key dispatch; each pane module under
`fractal/tui/panes/` owns its own interior, selection state, and key handlers,
and renders purely from the current snapshot (see [[features/tui/polling]]).

## Navigation

Focus moves on a two-level ring: an outer ring across the panes and inner
zone/mode ladders within each pane. `tab` and `shift+tab` step between fields
where a pane exposes them. Every pane renders from the same immutable snapshot,
so moving focus never triggers reads.

## The panes

- **Tree pane** -- a box-drawing view of every live node with a status dot,
  foldable per branch. Pressing `enter` on a row re-scopes the entire cockpit to
  that node: the headline interaction, from which the other panes follow.
- **Node pane** -- the focused node's card: status, the run/iter/step line,
  agent/model/session, measures, and config chips; below it a run -> iteration
  -> step explorer and a unified activity timeline. The card, the explorer, and
  the event log are three sub-zones sharing the pane. A toggle scopes the
  timeline to the node's own events or all descendants merged: a first visit
  defaults the user (root) node to descendants and every other node to its own
  activity, and a toggled choice sticks per node for the session.
- **Radio pane** -- three sources over the scoped node's radio: its own
  messages, the cross-subtree feed (every descendant's public/outbox posts), and
  the archive of saved messages. A zone ladder (source tabs, filters, rows)
  drives selection; opening a row shows a detail view with a Reply / Chat /
  React / Save action bar (see [[features/tui/actions]]).
- **Message pane** -- a segmented composer with two personalities: `chat` shows
  a per-branch transcript and streams agent replies (the cockpit door to the
  [[features/chat/_index|chat]] surface), while `radio` sends real messages -- a
  fresh send or a threaded reply -- through the action layer. Fields navigate as
  a grid, combo fields drop a filtered picker, and `/node`-style slash commands
  set fields straight from the body. Chat runs one turn at a time: a send typed
  while a turn is streaming queues (FIFO) behind it as a pending `you` line and
  dispatches when that turn finishes, against the branch it was typed on even if
  the cockpit has since re-scoped away. `ctrl+g` interrupts the in-flight turn
  from anywhere, the composer included (`ctrl+i` cannot serve, as terminals
  encode it as Tab): a lone queued send goes back into the composer for editing,
  several dispatch at once as one combined turn.

## Keyboard model

The cockpit runs its own keyboard model: a mode machine. The app holds one
string, `mode`, and its one key handler (`FractalApp.on_key`) looks the mode up
in a table of handlers and passes the key on, so that one table says what a key
means in each mode. Two cockpit-wide keys are handled ahead of the table:
`ctrl+a` (attach) and `ctrl+g` (interrupt a chat turn).

The modes, by owner:

- **The app** -- `ring`, the base mode: the arrows move the pane ring and
  `enter` enters the focused pane.
- **Tree pane** -- `tree`.
- **Node pane** -- `node`.
- **Radio pane** -- `radio` (its zone ladder), `rdetail` (an open message), and
  `rdrop` and `rreact` (the filter and reaction pickers).
- **Message pane** -- `field` (a cursor over the composer's fields), `edit`
  (typing in a field or the body), `combo` (a field's picker is open), and
  `chatscroll` (the arrows scroll the transcript).

Each pane owns its transitions: its `enter()` sets the mode the pane opens in,
and its `leave()` sets `ring` again and repaints the pane borders. A mode is
assigned only in the module that owns it -- a pane sets its own modes plus
`ring` on leaving, and the app sets only `ring` -- so a hand-off between panes
(a radio Reply into the composer, a step forked into chat) goes through the app
to the target pane, which sets its own mode.

Because the mode machine decides what a key means, the cockpit opts out of
Textual's focus system:

- The app sets `AUTO_FOCUS = None`, so no widget takes focus at start.
- The panes' scroll containers (`PaneScroll`) are not focusable. A focused
  scroll container carries its own arrow-key bindings, so one stray click would
  make every arrow key both drive the mode machine and scroll the clicked
  container.
- The app's `BINDINGS` hold only `tab` and `shift+tab`, the keys Textual binds
  to focus traversal, mapped to the composer's field-cycle actions.
- The cockpit gives real focus only to a widget that takes typing or a pick, and
  only while it does: the field or body being edited (`edit`), the field being
  filtered while its picker is open (`combo`), and the radio pickers (`rdrop`,
  `rreact`). The focused widget takes its own keys first -- typed text, a
  picker's arrows and `enter` -- and the mode's handler gets the rest, such as
  `escape`. Leaving the mode clears the focus.
- What the screen shows as focus is CSS classes the app and panes toggle: on a
  pane's border, `ringsel` while the ring cursor is on the pane and `entered`
  once the keys are inside it, plus the row, field, and zone highlights.

Every mode except the two radio pickers shows its live keys on a hint line,
built from the key glyphs in `fractal.tui.theme` (`RET`, `CTRL`, the arrows):
the screen footer for `ring`, the foot of the tree, node, and radio panes for
their modes, and the composer's hint line for the message pane's four. While
`rdrop` or `rreact` is open, the radio foot keeps the line of the view beneath
the picker. A zone is a region inside one mode with its own keys, hint, and
highlight, such as a step on the radio pane's ladder or the node pane's card;
the pane's foot re-renders on every zone change.

A picker -- the radio pane's filter and reaction lists, a composer field's
options -- is an option list mounted on the app rather than inside its pane, on
the stylesheet's `overlay` layer above the panes, and placed from the on-screen
region of the widget it opens from. It runs under a mode of its own while open.
Closing it removes every widget carrying the picker's id (so a second close is
harmless), clears the focus, and sets the mode beneath it again (`radio`,
`rdetail`, or `field`).
