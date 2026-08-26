The TUI
=======

``fractal open`` launches the cockpit: a live, single-screen terminal
dashboard over the whole node tree. It shows every node's status at a glance,
the focused node's runs, costs, and activity, and its radio traffic — and it
lets you act from the operator's seat: send radio messages, chat with a
node's agent session, and attach to a node's terminal. Everything else is
read-only: the cockpit continuously polls the tree and writes only on those
explicit actions, always as the user (root) node.

Launch
------

The TUI ships with the package. From anywhere inside an initialized
repository:

.. code-block:: console

   $ fractal open

The cockpit anchors on the user (root) node regardless of which worktree you
launch it from, so it always shows the whole tree; running it from the
repository root is the usual habit. The command surface:

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Argument / option
     - Meaning
   * - ``name``
     - Tree root branch, or a node branch to focus initially (default: the
       caller's own tree, opened at its root). A node branch opens the tree
       that owns it, focused on that node.
   * - ``--path <dir>``
     - Worktree directory (default ``.``).
   * - ``--light``
     - Open with the light palette.
   * - ``--dark``
     - Open with the dark palette (the default). Mutually exclusive with
       ``--light``.

The screen background is always your terminal's own — the palette only picks
the accent colors, so pass ``--light`` when your terminal uses a light color
scheme. The TUI has no configuration file of its own: it reads each node's
``.status`` and ``config.json`` plus the central database, and renders what
it finds.

Layout and navigation
---------------------

One screen, four panes:

- **Tree** (top left) — the whole node tree.
- **Radio** (top middle) — the focused node's messages.
- **Node** (right, full height) — the focused node's card, runs explorer,
  and activity log.
- **Message** (bottom left) — the chat/radio composer.

A header breadcrumb reads ``fractal · <repo> › <focused branch>``; a footer
lists the live key hints. The focused branch is the cockpit's *scope*:
re-scoping from the tree pane re-points every other pane at the selected
node.

Navigation is a two-level focus ring. In **ring mode** the arrow keys move a
grey selection border between panes — ``left``/``right`` along the top row,
``down`` to the message pane, and from the message pane ``up`` back to the
radio pane or ``right`` to the node pane. ``enter`` enters the focused pane
(its border turns coral) and hands the keys to that pane; ``esc`` steps back
out to the ring. ``q`` (in ring mode) or ``ctrl+q`` (anywhere) quits.

The mouse works throughout: clicking a row moves its cursor exactly like
arrowing to it, and clicking the already-highlighted row activates it exactly
like ``enter``. The tree pane's right edge and the message pane's top edge
are drag-resizable (clamped between a floor and half the terminal; the
dragged size holds for the session), and the scroll wheel is confined to the
pane under the pointer.

The tree pane
-------------

Every node in the tree, as box-drawing rows, each with a status dot; the
root row is tagged ``(user)``. A filled dot means the node is live (active,
or paused mid-work); a hollow dot means it is not running — settled, ``idle``
awaiting a start, or retired. The dot's color carries the specific lifecycle
status (see :doc:`/guide/lifecycle`), and a pending signal overrides it. A
node whose ``.status`` says ``active`` but whose tmux session or headless
process group has vanished displays as ``exited`` — a display-only
reconciliation; nothing is written back.

``up``/``down`` select a row, ``right``/``left`` unfold and fold a subtree,
and ``enter`` re-scopes the whole cockpit to the selected branch — the pane's
primary interaction. ``enter`` on the already-scoped branch folds or
unfolds it instead. Hovering (or selecting) a truncated row unfolds its full
label. The pane's foot shows ``N/M nodes running`` — the user node is not
counted — or the key hints while you drive the pane.

The node pane
-------------

The right-hand pane shows the focused node in four zones, stepped through
with ``up``/``down``: the card, the runs explorer, the log-scope toggle, and
the activity log.

The card
~~~~~~~~

The card's first row shows the status glyph and word, the branch, and any
pending signal as a present participle (``finishing``, ``stopping``, ...).
The second reads ``run N · iter n/m · step k/T (name)`` — a sync pre-step
reads as plain ``sync``. Below it, an ident block lists the agent, model,
and full session id of the running step.

The **measures matrix** is the cockpit's cost-and-time view: rows ``step`` /
``iter`` / ``run`` by columns ``time`` / ``cost``, each cell
``<current>/<ceiling>`` with a green-to-red gauge of the fraction used
(``-`` and an empty gauge when uncapped). The run-cost gauge turns yellow
once spend enters the configured reserve budget and red at the cap. Open
rows' clocks and gauges keep ticking between database writes.

**Config chips** render the node's ``config.json`` values in schema order
(skipping the keys the card already shows, like ``max_iters`` and the cap
keys), packed into a few rows with a trailing ``...`` for overflow;
hovering the chips shows the whole file as pretty-printed JSON. See
:doc:`/configuration` for the keys themselves.

``enter`` on the card opens a chat against the card's session in the message
pane. While the runs explorer drives a selection, the card "time-machines" to
the highlighted run, iteration, or step — its figures read as they stood at
that row's end.

The runs explorer
~~~~~~~~~~~~~~~~~

A run → iteration → step tree, newest run first, with a status dot, session,
elapsed, and cost per row. ``up``/``down`` select; ``right`` or ``enter``
expands a run or iteration; ``left`` collapses; ``enter`` on a **step** forks
that step's session into the chat composer. Sync passes fold into the
numbered step that follows them — the time spans both and the costs sum — and
a trailing sync with no following step stays a standalone ``sync`` row.

The activity log
~~~~~~~~~~~~~~~~

The unified activity timeline, newest first (a bounded window of the most
recent events, with centered date rules closing each day's group): columns
for time, node, run, iter, step, event, elapsed, and cost. ``up``/``down``
move a cursor — the selected row unfolds to its full text — and ``enter``
jumps the runs explorer (and the card) to the event's run, iteration, or
step.

Between the explorer and the log sits the **log-scope toggle**, a
``node | descendants`` switch: ``left``/``right``/``enter``/``t`` flip the
log between the focused node's own activity and its whole subtree's, merged.
(``t`` also works directly from the log rows.) The user node opens on the
descendants view — the whole tree's timeline — and every other node on its
own; a toggled choice sticks per node for the session, so re-scoping back to
a node restores its last setting.

The radio pane
--------------

The focused node's radio traffic, in three source tabs:

- **Messages** — the focused node's own mailbox: its top-level messages
  across all its channels, newest first. Replies stay behind their parents.
- **Feed** — a broadcast digest of the focused node's *subtree*: every
  descendant's ``public`` and ``outbox`` posts merged newest-first (a bounded
  window of the newest posts). This is not the subscription-driven
  ``fractal radio feed`` command — the tab never touches subscriptions.
- **Archive** — the cockpit user's saved messages. This is always the root
  node's archive, whatever node is focused.

The pane is a zone ladder — source tabs, filter chips, message rows —
stepped with ``up``/``down``; stepping above the top zone leaves the pane.
On the tabs, ``left``/``right`` cycle the source. On the filter chips,
``left``/``right`` pick a chip and ``enter`` drops its option list
(``esc`` closes it): the channel filter offers ``all`` / ``inbox`` /
``outbox`` / ``public`` / ``private`` (the four default channels only —
custom channels are not filterable here), and the show filter ``all`` /
``unread`` / ``read``. Rows show an unread dot, the sender's leaf name, the
channel, the priority, the subject, and the timestamp; the unread dot
reflects the mailbox *owner's* read state, not yours.

``enter`` on a row opens the **detail view**: sender (and the session that
wrote it), channel, timestamp, UUID, priority, subject, and the full body.
Opening a message in the root's own mailbox writes a real read receipt;
messages in any other node's mailbox are observed without touching their
read state. An action bar offers **Reply / Chat / React / Save**
(``left``/``right`` cycle, ``enter`` activates, ``esc`` goes back):

- **Reply** pre-fills the composer as a threaded radio reply; the detail
  stays open for reference.
- **Chat** re-scopes the cockpit to the message's sender and forks its
  session into the chat composer.
- **React** drops a ``+1`` / ``-1`` choice.
- **Save** archives the message into the root's archive.

Action failures surface as toast notifications.

The message pane
----------------

A segmented composer with a ``chat | radio`` kind toggle. Entering the pane
from the ring drops you straight into the body, ready to type; ``esc`` steps
back to field navigation, where the arrow keys move across the field grid,
``tab``/``shift+tab`` cycle fields, and ``enter`` activates a field.
Combo fields — node, session, channel, priority — drop a filtered picker:
type to filter, ``up``/``down`` to highlight, ``enter`` commits, ``esc``
cancels; typing a printable character on a combo field in the grid opens it
seeded with that character. The node combo offers every branch in the tree,
the channel combo the focused node's actual channels (so custom channels are
reachable here), and the priority combo ``10`` down to ``1``.

Slash commands typed in the body set fields directly: ``/node``,
``/channel``, ``/thread``, ``/priority``, and ``/subject <value>`` (a
recognized command highlights coral). The four radio-field commands also
flip the composer to the radio kind. ``/node`` accepts a full branch or a
unique leaf name and toasts ``no node '<name>'`` when neither matches.

**Send keys**: ``ctrl+s`` always sends. On terminals speaking the kitty
keyboard protocol — such as kitty, ghostty, WezTerm, and iTerm2 — ``enter``
also sends and ``shift+enter`` inserts a newline; everywhere else ``enter``
is the newline and ``ctrl+s`` the only send. The hint line under the body
shows which applies.

Radio kind
~~~~~~~~~~

Fields: node, channel, thread, priority, subject, and the body. Defaults:
the focused node, channel ``public``, priority ``10`` — note these differ
from the CLI's ``fractal radio send`` defaults. An empty subject falls back
to a short prefix of the body. With a message UUID in the thread field
the send becomes a threaded reply (as **Reply** pre-fills it); otherwise it
is a plain send to the node and channel. A landed message consumes its
subject and thread — after a successful send or reply the subject clears and
the thread field resets to ``—``, so the next message is a plain send unless
you set the thread again; node, channel, and priority keep their values. A
failed send keeps node, channel, thread, priority, and subject for a
re-send; the body clears either way. Every message is sent **as the user
(root) node**. Success toasts ``radio send → <node> · <channel>``
(``radio reply → ...`` for a threaded reply); failures toast the error. See
:doc:`/guide/radio` for the messaging model.

Chat kind
~~~~~~~~~

Fields: node, session, the per-branch transcript, and the body. Chat runs
one agent turn at a time, streamed into the transcript as bubbles (your
prompt, the agent's reply, tool and meta lines, errors) with a thinking
spinner while the agent works; a turn that goes silent for too long (about
two minutes) is cancelled. The transcript is kept per branch — re-scoping
swaps it — and ``enter`` on the transcript switches to a scroll mode
(``up``/``down`` scroll, ``esc`` back). Chat never writes radio messages.

A prompt sent while a turn is still streaming queues behind it: it appears
at once as a pending ``you`` line and dispatches when that turn finishes,
against the branch and session it was typed on even if you have since
re-scoped. ``ctrl+g``, from anywhere — the body included — interrupts the
in-flight turn and the transcript notes ``cancelled``; a single queued
prompt is handed back into the body for editing, while several queued
prompts on that branch are combined, oldest first, into one turn and sent at
once (prompts queued against another branch stay queued).

How a turn reaches the node depends on its state:

- An **active or paused** node whose agent supports session forking gets its
  newest live session *forked* — the primary flow is "pause it, then ask
  what it was doing": the running loop (or the frozen session a paused run
  will resume) is never disturbed.
- The cockpit's own earlier chat thread *resumes in place*, so conversations
  are multi-turn.
- Everything else — settled nodes, detached nodes, agents that cannot fork,
  no session woven yet — gets a *fresh* session seeded with the node's
  context. When a live thread cannot be forked, the fresh-session fallback
  is announced with a warning toast.

The session field selects an explicit session to fork or resume; ``enter``
on a step in the runs explorer, on the node card, or the **Chat** action in
the radio detail all pre-fill it.

Attaching to a session
----------------------

``ctrl+a``, from anywhere, suspends the cockpit and attaches the focused
node's tmux session **read-only**: every key is ignored except ``esc``,
which detaches (the session's status line advertises ``esc detach`` for the
duration). If the node has no live session, a ``no running session`` toast
appears instead; a headless node, which has no session to attach, gets a
``headless output: <log>`` toast naming its ``headless.log``. When the
cockpit itself runs inside tmux, ``ctrl+a`` switches your tmux client to the
node's session instead — without the read-only guard. For attaching outside
the TUI, prefer ``fractal node attach`` over raw ``tmux attach -t``: tmux
prefix-matches session names and can attach the wrong one.

Keybindings
-----------

.. list-table::
   :header-rows: 1
   :widths: 20 24 56

   * - Context
     - Key
     - Action
   * - Anywhere
     - ``ctrl+a``
     - Attach the focused node's tmux session (read-only).
   * - Anywhere
     - ``ctrl+g``
     - Interrupt the in-flight chat turn (a lone queued prompt returns to
       the body; several queued prompts fire as one turn).
   * - Anywhere
     - ``ctrl+q``
     - Quit.
   * - Ring
     - ``left`` / ``right`` / ``up`` / ``down``
     - Move the focus between panes.
   * - Ring
     - ``enter``
     - Enter the focused pane.
   * - Ring
     - ``q``
     - Quit.
   * - Any pane
     - ``esc``
     - Step back (close a dropdown or detail, end an edit, leave the pane).
   * - Tree
     - ``up`` / ``down``
     - Select a node.
   * - Tree
     - ``right`` / ``left``
     - Unfold / fold the selected subtree.
   * - Tree
     - ``enter``
     - Re-scope the cockpit to the selected node (fold it when already
       scoped).
   * - Radio
     - ``up`` / ``down``
     - Step the zone ladder (tabs → filters → rows); move the row cursor.
   * - Radio
     - ``left`` / ``right``
     - Cycle the source tabs; pick a filter chip.
   * - Radio
     - ``enter``
     - Open a filter dropdown; open the selected message's detail.
   * - Radio detail
     - ``left`` / ``right`` / ``enter``
     - Cycle and activate Reply / Chat / React / Save.
   * - Node
     - ``up`` / ``down``
     - Move between zones and rows (card → runs → scope toggle → log).
   * - Node (card)
     - ``enter``
     - Chat with the card's session.
   * - Node (runs)
     - ``right`` / ``enter``
     - Expand a run or iteration; ``enter`` on a step forks its session
       into chat.
   * - Node (runs)
     - ``left``
     - Collapse the selected row.
   * - Node (log)
     - ``enter``
     - Jump the explorer to the event's run/iteration/step.
   * - Node
     - ``t``
     - Toggle the log between the node and its descendants.
   * - Message
     - arrows
     - Move across the field grid; leave the pane past the top row.
   * - Message
     - ``tab`` / ``shift+tab``
     - Cycle the compose fields.
   * - Message
     - ``enter``
     - Activate a field (toggle the kind, open a combo, edit, scroll the
       transcript).
   * - Message (body)
     - ``ctrl+s``
     - Send (always).
   * - Message (body)
     - ``enter`` / ``shift+enter``
     - Send / newline on kitty-protocol terminals; both insert a newline
       elsewhere.

What the TUI writes — and what stays on the CLI
-----------------------------------------------

The cockpit's only writes are the explicit actions above, always attributed
to the user (root) node:

- sending radio messages and threaded replies (message pane, Reply action);
- reacting ``+1``/``-1`` and saving to the archive (detail actions);
- read receipts, when you open a message in the root's own mailbox;
- chat turns against a node's agent session.

Everything that changes the tree or a node stays on the CLI: creating and
starting nodes, ``finish``/``stop``/``kill``, pausing and resuming, merging
and deleting, cap and config updates, radio channel management,
subscriptions, ``unsend``/``unsave``, and plans. See :doc:`/cli/index` for
the full command surface — :doc:`/cli/node` for lifecycle,
:doc:`/cli/radio` for messaging, :doc:`/cli/plan` for plans — and
:doc:`/skill` for running the operator's seat conversationally.
