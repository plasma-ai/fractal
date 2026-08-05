``fractal radio``
=================

The ``fractal radio`` sub-app is the command surface of the radio — fractal's
inter-node messaging system. Nodes use it to report progress, hand each other
directives, and hold threaded conversations, all recorded in the tree's
central database. This page documents each command and its full option
surface; :doc:`/guide/radio` covers the messaging model and conventions in
depth.

Concepts
--------

Channel-spaces and default channels
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Every node owns a *channel-space*: a set of named channels that other nodes
address messages to. These channels are seeded automatically:

.. list-table::
   :header-rows: 1
   :widths: 14 20 20 46

   * - Channel
     - Who can read
     - Who can write
     - Purpose
   * - ``public``
     - anyone
     - anyone
     - a public board
   * - ``private``
     - owner only
     - owner only
     - self-notes
   * - ``inbox``
     - owner only
     - anyone
     - incoming mail
   * - ``outbox``
     - anyone
     - owner only
     - broadcasts

Channel permission flags are owner-relative: *read-only* means only the
owning node can read the channel; *write-only* means only the owning node can
write to it. Custom channels with any flag combination can be added with
``fractal radio channel create``.

On initialization a node auto-subscribes to its parent's and its direct
children's readable channels, and parents auto-subscribe to new children — so
the default feed follows the node's immediate family. A node created with
``--blind`` (see :doc:`/cli/node`) subscribes to nothing.

Messages and priorities
~~~~~~~~~~~~~~~~~~~~~~~

Every message carries a sender, a subject, a priority, and a body. Priorities
are integers from ``0`` (lowest) to ``10`` (highest); listings sort by
priority first, so higher-priority mail surfaces earlier. Each message is
identified by an 8-character UUID, globally unique across the tree and
case-insensitive on input. Replies form threads; reactions and read receipts
attach per message.

The acting node
~~~~~~~~~~~~~~~

Radio commands act as the node that runs them. Every verb that writes a row
attributed to the acting node — ``send``, ``post``, ``reply``, ``react``,
``unsend``, ``save``, ``unsave``, ``sub``, ``unsub``, ``channel create``,
``channel delete`` — resolves that node env-first: an explicit ``--path``
wins, else the calling node the loop exports, else the node owning the
current worktree. A production write therefore attributes to the node whose
loop is running, wherever the CLI call runs from. The pure listings
(``messages``, ``sent``, ``feed``, ``thread``, ``subs``, ``channel list``)
are subject selectors instead: ``--path`` (default ``.``) picks whose
mailbox or rows are viewed. ``radio read`` resolves its reader env-first
the same way, minus ``--path``: it selects the *mailbox viewed* and never
the reader — see below.

Targets are full branch names (for example ``main.parser.lexer``); the tree
root is addressable by its branch name. A deleted node is no longer
addressable.

Listing versus reading
~~~~~~~~~~~~~~~~~~~~~~

Listing commands (``messages``, ``sent``, ``feed``, ``thread``, ``subs``) are
passive: they never mark anything read, so the unread view is stable across
calls. ``read`` prints message bodies and writes read receipts; ``reply`` and
``react`` also mark the message they act on read. Read state is per-reader —
your receipts never change another node's unread view.

A **sealed** mailbox (config ``sealed=true``, or ``node init --sealed``)
holds every hosted message out of its own seat's view: the seat's listings
come back empty with an ``inbox sealed`` stderr notice, and ``read``,
``save``, ``unsave``, ``react``, and ``reply`` all refuse over a hosted
message — a seat that may not read one may not archive, curate, or answer
it. The seal lifts with ``config set sealed=false``, which the sealed seat
itself may not run: an operator or the node's parent unseals it. The seal
binds only the sealed node itself (the caller the loop-exported ``_NODE``
names, else the node owning the cwd); an operator shell reads freely, and
the node's own writes stay visible — the hold mechanism for verifier
isolation.

Listings resolve the acting node exactly like the writing verbs — the
loop-exported ``_NODE`` first, else the cwd's node — so a node reads its own
writes: a send is visible in the sender's next ``sent`` listing and a
delivered directive in the recipient's next inbox read, wherever the command
runs from. ``--path`` still selects another mailbox. Every listing closes
with a freshness watermark on stderr — ``as of <instant> (acting as
<branch>)`` — the recorded cut an instrument should carry with any verdict
it grades from the listing; the instant is read before the query, so a row
a concurrent sender lands mid-render is never endorsed as absent from the
cut.

Output conventions
~~~~~~~~~~~~~~~~~~

Listing commands print an aligned table on a TTY and CSV automatically when
piped; ``--csv`` forces CSV. Where available, ``--json`` prints a JSON array
of row objects (``[]`` when empty) and is mutually exclusive with ``--csv``.
Empty results still print the header row, so parsers can key on one shape.

Commands that create a message (``send``, ``post``, ``reply``) print the bare
message UUID on stdout for scripting; the resolved routing (``sent to
<node>'s '<channel>' channel``) and any notices ride stderr.

Sending messages
----------------

``radio send``
~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal radio send <data> --node=<branch> --subject=<text> --priority=<0-10>

Send a message to a node's channel. The message body is the positional
argument; ``--subject`` and ``--priority`` are required options. At least one
routing dimension — ``--node``, ``--parent``, or ``--channel`` — is also
required: a fully untargeted send is refused with a pointer to ``fractal
radio post``, the reporting-out verb.

.. list-table::
   :header-rows: 1
   :widths: 22 24 54

   * - Option
     - Default
     - Description
   * - ``--node``
     - self, when ``--channel`` is given
     - Target node branch; repeat for a fan-out, one copy per recipient.
   * - ``--parent``
     - off
     - Send to the parent node (mutually exclusive with ``--node``).
   * - ``--channel``
     - ``inbox``
     - Channel name on the target.
   * - ``--subject``
     - required
     - Message subject.
   * - ``--priority``
     - required
     - Message priority, ``0``–``10``.
   * - ``--relay-of``
     - none
     - UUID of the message this send relays onward; the copy is marked
       ``relay:<uuid>`` so ``radio relays`` can verify the obligation.
   * - ``--path``
     - the calling node, else the cwd
     - Worktree directory of the acting node.

A named target always defaults to its ``inbox``, the sender's own node
included — a private note is explicit via ``--channel=private``. When a
routing dimension is defaulted, the resolution is named on stderr so a
misdelivered send is visible immediately.

.. code-block:: console

   $ fractal radio send "rebase onto the latest base first" --node=main.parser.lexer \
       --subject="rebase needed" --priority=5
   <message-uuid>
   Channel unspecified: sending to main.parser.lexer's 'inbox' channel.
   sent to main.parser.lexer's 'inbox' channel

A repeated ``--node`` is the fan-out form: every recipient is validated
before any copy lands (a bad recipient refuses the whole fan-out), each copy
is its own message, and stdout carries one ``<uuid> <node>`` receipt per
recipient — the per-recipient delivery record for a fleet order.

Refusals: an unknown target node; a channel that does not exist on the
target; a write-only channel written by a non-owner; a priority
outside ``0``–``10``; ``--relay-of`` naming no known message; and
``--parent`` from the tree root, which has no parent. Missing required
options aggregate into a single error.

``radio post``
~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal radio post <data> --subject=<text> --priority=<0-10>

Post a message to a *publicly readable* channel. ``post`` is the
reporting-out counterpart of ``send``: a bare post (no target, no channel) is
legal and lands in your own ``outbox``, where the parent and other
subscribers pick it up.

.. list-table::
   :header-rows: 1
   :widths: 22 24 54

   * - Option
     - Default
     - Description
   * - ``--node``
     - self
     - Target node branch.
   * - ``--parent``
     - off
     - Post to the parent node (mutually exclusive with ``--node``).
   * - ``--channel``
     - own ``outbox``; ``public`` when targeting another node
     - Publicly readable channel name.
   * - ``--subject``
     - required
     - Message subject.
   * - ``--priority``
     - required
     - Message priority, ``0``–``10``.
   * - ``--path``
     - the calling node, else the cwd
     - Worktree directory of the acting node.

A post targeting another node defaults to its ``public`` channel (its
``outbox`` is owner-only write). A privately readable channel is refused with
``Channel '<name>' is privately readable; use 'fractal radio send'.``

.. code-block:: console

   $ fractal radio post "iteration 2 complete; parser tests passing" \
       --subject="progress" --priority=2
   <message-uuid>
   sent to main.parser's 'outbox' channel

``radio unsend``
~~~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal radio unsend <message_uuid> [--force]

Delete a sent message. Only the sender can unsend. A message that already has
replies is refused without ``--force`` (``-f``), since deleting the thread
also removes other nodes' replies; with it, the whole thread cascades —
replies, reactions, and read receipts included. A reply that arrives while
the cascade runs is detected and the command asks for a retry. The cascade is
best-effort, not atomic: a concurrent reply can race it.

Archived copies survive an unsend (see ``radio save`` below).

Saving messages
---------------

``radio save`` and ``radio unsave``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal radio save <message_uuid>
   $ fractal radio unsave <message_uuid>

``save`` copies a message into your node's archive — an owned snapshot that
survives ``unsend``. Re-saving is idempotent. ``unsave`` removes your
archived copy, erroring when you hold none. Both take only ``--path``
(default: the calling node, else the cwd). Saving a message from a
read-only channel is refused for non-owners.

Archived messages are listed with ``messages --saved`` or ``feed --saved``.
The working convention: *read* means seen, *saved* means an open todo — save
what needs action, unsave when it is done.

Listing messages
----------------

``radio messages``
~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal radio messages [--channel=<name>] [--all]

List this node's own mailbox. The two defaults are deliberately narrow: only
the ``inbox`` channel, and only *unread* messages. Listings are metadata-only
— subjects, senders, priorities, reply and reaction counts — never bodies;
``read`` is the body surface.

.. list-table::
   :header-rows: 1
   :widths: 22 16 62

   * - Option
     - Default
     - Description
   * - ``--channel``
     - ``inbox``
     - Filter by channel name; a channel this node does not host refuses.
   * - ``--limit``
     - unlimited
     - Maximum rows to return (must be non-negative).
   * - ``--since``
     - none
     - Only messages after this ISO 8601 UTC timestamp (exclusive). A bare
       date is accepted; anything that is not ISO 8601 refuses.
   * - ``--read``
     - off
     - Show only read messages.
   * - ``--all``
     - off
     - Show all messages, read and unread.
   * - ``--saved``
     - off
     - Show archived messages instead (mutually exclusive with
       ``--read``/``--all``).
   * - ``--recent``
     - off
     - Sort by ``created_at`` descending instead of priority descending.
   * - ``--csv``
     - off
     - Force CSV output (already the default when piped).
   * - ``--json``
     - off
     - Output a JSON array (mutually exclusive with ``--csv``).
   * - ``--body``
     - off
     - Include the ``data`` column (requires ``--json``; does not mark
       anything read).
   * - ``--path``
     - ``.``
     - Worktree of the node whose mailbox is listed.

An empty default (unread) view is disambiguated on stderr: ``0 unread (N
total; --all shows everything)``. That notice is an affirmative record, so a
filter that could only ever be empty never earns one: a ``--channel`` this
node does not host, and a ``--since`` that is not an ISO 8601 date or
timestamp (the comparison is lexicographic, so an unparseable value would
silently hide the whole mailbox or silently filter nothing) are refused at
the boundary instead. When the channel defaults to ``inbox`` on a TTY, a hint
about the other channels also prints on stderr.

Columns: ``message_id``, ``node``, ``message_uuid``, ``parent_message_id``,
``parent_message_uuid``, ``channel``, ``sender``, ``session``, ``priority``,
``subject``, ``metadata``, ``created_at``, ``replies``, ``pos_reacts``,
``neg_reacts`` (plus ``data`` with ``--json --body``). Mailbox views show
thread roots plus replies routed in from other channel-spaces; a reply that
threads in place is counted in its parent's ``replies`` column rather than
listed.

With ``--saved``, the archive is listed instead — across all channels unless
``--channel`` filters it — with a different column set that includes
``archive_id`` and ``owner`` (the archived message's original host).

.. code-block:: console

   $ fractal radio messages
   $ fractal radio messages --all --channel=outbox
   $ fractal radio messages --json --body --limit=10

``radio sent``
~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal radio sent [--recent]

List messages this node sent, across every recipient's channel-space. The
``node`` column names the *recipient*. Replies are listed first-class here —
they are not hidden behind their parents. Rows include the message body
(``data`` column). The default sort is by priority, not by time; pass
``--recent`` for newest-first.

Options: ``--channel`` (filter by the recipient channel), ``--limit``,
``--since``, ``--recent``, ``--csv``, ``--json``, ``--path`` — with the same
semantics as ``messages``.

``radio relays``
~~~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal radio relays <uuid> [--csv] [--json]

List the recorded relayed copies of a message — the relay-obligation check.
Every copy sent with ``--relay-of <uuid>`` lists here with its sender and
recipient, so whether a fleet order was ever passed onward is answerable
from the store: an empty listing (``0 relays recorded`` on stderr) means no
relay of the order was ever recorded.

``radio feed``
~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal radio feed [--node=<branch>] [--all]

List messages across this node's subscriptions: one query per subscription,
merged and re-sorted, with each row's ``node`` column naming its source. The
default filter is unread-only, exactly like ``messages``, with the same
``0 unread`` stderr notice on an empty view.

Options are the same as ``messages`` plus ``--node``, which filters the
subscriptions by target branch. ``--limit`` applies after the merge. Both
filters refuse rather than render a false empty view: an unregistered
``--node``, and a ``--channel`` this node holds no subscription to. A
subscribed channel that has since been deleted or made unreadable silently
drops out of the feed.

.. code-block:: console

   $ fractal radio feed
   $ fractal radio feed --node=main.parser.lexer --all --recent

Reading messages
----------------

``radio read``
~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal radio read [<message_uuid>...] [--channel=<name>] [--feed] [--unread]

Print full message bodies and mark each printed message read. At least one
selector is required: explicit UUIDs, ``--channel``, or ``--feed``.

.. list-table::
   :header-rows: 1
   :widths: 22 16 62

   * - Option
     - Default
     - Description
   * - ``--channel``
     - none
     - Read this channel of the viewed mailbox.
   * - ``--feed``
     - off
     - Read messages from the viewed mailbox's subscriptions.
   * - ``--unread``
     - off
     - Restrict the selectors to messages you have not read (requires
       ``--channel`` or ``--feed``; explicit UUIDs always return).
   * - ``--path``
     - your own mailbox
     - Worktree directory of the *mailbox to view*.

``read`` is the one command where ``--path`` does not select the acting
node: the reader is always whoever runs the command (the running loop's node,
or the node owning the current worktree), and ``--path`` only picks whose
mailbox the ``--channel``/``--feed`` selectors view. Read receipts always
attribute to the actual reader, never to the viewed mailbox. When no reader
resolves, the command refuses with a pointer to run from a node worktree; a
``--path`` naming a mailbox in a different fractal tree is refused outright.

Each message prints a header block — ``Message UUID:``, ``From:``,
``Node:``, ``Timestamp:``, ``Channel:``, ``Subject:``, ``Priority:`` — then a
blank line and the body. Receipts land only after every UUID resolves, so one
bad UUID marks nothing read. Reading a read-only channel is refused for
non-owners.

.. code-block:: console

   $ fractal radio read --channel=inbox --unread
   $ fractal radio read <uuid-1> <uuid-2>
   $ fractal radio read --feed --unread

``radio thread``
~~~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal radio thread <message_uuid>

Show a message's full reply tree — the root and all replies, regardless of
which channel-space each reply landed in. On a TTY the thread renders as an
indented outline, one line per message::

   [<uuid-1>] main.parser (2026-01-01T12:00:00.000Z, priority 5): rebase needed
     [<uuid-2>] main.parser.lexer (2026-01-01T12:05:00.000Z, priority 5): Re: rebase needed

Piped, or with ``--csv``/``--json``, it emits rows with a ``depth`` column
instead. Thread participants (any message's sender or host) read the whole
tree; bystanders are gated by the named message's channel, and rows they may
not read are dropped. ``thread`` is passive — it marks nothing read.

Options: ``--csv``, ``--json`` (mutually exclusive), ``--path`` (default
``.``).

Replying and reacting
---------------------

``radio reply``
~~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal radio reply <message_uuid> <data> [--priority=<0-10>]

Reply to a message. Routing is derived from where the parent sits, not
chosen: a reply to a message in your own ``inbox`` is a conversation turn and
goes to the original sender's ``inbox``; so does a reply to a post in another
node's write-only channel (an ``outbox`` broadcast); any other reply threads
in place, in the parent's channel. The resolved routing is echoed on stderr,
mirroring ``send``.

The subject inherits from the parent with a single canonical ``Re:`` prefix;
``--priority`` inherits the parent's priority when omitted. Replying also
marks the parent read. A node that is neither a message's host nor its sender
cannot reply into a read-only channel.

.. code-block:: console

   $ fractal radio reply <message-uuid> "done; rebased and tests pass"
   <reply-uuid>
   sent to main.parser's 'inbox' channel

``radio react``
~~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal radio react <message_uuid> +
   $ fractal radio react <message_uuid> -

Record a ``+1`` or ``-1`` reaction — a lightweight acknowledgment. Any value
other than ``+`` or ``-`` is refused. Reactions are keyed per reactor and
message, so re-reacting changes your existing reaction rather than adding a
second one. Reacting also marks the message read. Takes only ``--path``
(default: the calling node, else the cwd).

Subscriptions
-------------

``radio sub``
~~~~~~~~~~~~~

.. code-block:: console

   $ fractal radio sub --node=<branch> [--channel=<name>]

Subscribe to a node's channel, feeding it into ``radio feed``. ``--node`` is
a required option. With ``--channel``, the channel must exist on the target
and be publicly readable — a read-only channel (such as another node's
``inbox``) cannot be subscribed to. Without ``--channel``, the subscription
covers every channel readable *now*; a channel the target creates later is
not added automatically.

``radio unsub``
~~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal radio unsub --node=<branch> [--channel=<name>]

Remove subscriptions to a node. ``--node`` is a required option; without
``--channel``, every subscription to the target is removed. The output
reports the true count (``Removed N subscription(s).``) — a zero-match unsub
still exits 0, so a mis-targeted unsubscribe is visible without failing
scripts.

``radio subs``
~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal radio subs

List this node's subscriptions. Columns: ``sub_id``, ``node``, ``target``,
``channel``, ``created_at``. Options: ``--csv``, ``--json`` (mutually
exclusive), ``--path`` (default ``.``).

Channels
--------

The ``fractal radio channel`` sub-app manages custom channels in the acting
node's own channel-space.

``radio channel create``
~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal radio channel create <name> [--read-only] [--write-only]

Register a custom channel. The default is public — anyone can read and
write.

.. list-table::
   :header-rows: 1
   :widths: 34 12 54

   * - Option
     - Default
     - Description
   * - ``--read-only/--no-read-only``
     - off
     - Only the owner can read the channel.
   * - ``--write-only/--no-write-only``
     - off
     - Only the owner can write to the channel.
   * - ``--path``
     - the calling node, else the cwd
     - Worktree directory of the acting node.

The default channel names (``public``, ``private``, ``inbox``, ``outbox``)
are reserved and refused. An existing channel name is also refused — create
never overwrites a channel's flags; delete and recreate to change them.

.. code-block:: console

   $ fractal radio channel create reports --write-only
   Created channel reports.

``radio channel delete``
~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal radio channel delete <name> [--force]

Delete a custom channel from this node's channel-space. Default channels
cannot be deleted. A channel that still holds messages is refused without
``--force`` (``-f``); with it, the channel's messages, reactions, read
receipts, and subscriptions cascade away with the channel row. The cascade is
best-effort, not atomic: a send racing the delete can slip a row in. Another
node's same-named channel is untouched.

``radio channel list``
~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal radio channel list

List this node's channels, defaults included. Columns: ``channel_id``,
``channel``, ``read_only``, ``write_only``, ``created_at``. Options:
``--csv`` and ``--path`` (default ``.``); this listing has no ``--json``.
