"""Implements ``Radio`` class."""

from __future__ import annotations

import os
import secrets
import typing
from collections.abc import Callable
from typing import Optional

from fractal.constants import PRIORITY_MAX, PRIORITY_MIN
from fractal.typing import Row

if typing.TYPE_CHECKING:
    from .db import Database
    from .node import Node

__all__ = ['Radio']

_DEFAULT_CHANNELS = [
    {'channel': 'public', 'read_only': 0, 'write_only': 0},
    {'channel': 'private', 'read_only': 1, 'write_only': 1},
    {'channel': 'inbox', 'read_only': 1, 'write_only': 0},
    {'channel': 'outbox', 'read_only': 0, 'write_only': 1},
]


class Radio:
    """Inter-node messaging for autonomous agent nodes.

    Provides message sending, channels, subscriptions, threads,
    reactions, and read tracking in the tree's central database.
    """

    def __init__(self: Radio, node: Node) -> None:
        """Initialize ``Radio``.

        Args:
            node: The owning ``Node`` instance.

        """
        self._node = node

    @property
    def node(self: Radio) -> Node:
        """Return the owning node."""
        return self._node

    @property
    def db(self: Radio) -> Database:
        """Return the central database."""
        return self._node.db

    def init(self: Radio) -> None:
        """Seed default channels and auto-subscribe to parent/children.

        Called from ``Node.init`` once the node's config is written. Does:

        1. Insert default channels.
        2. Derive the parent from the branch name; subscribe
           to its readable channels.
        3. Query the ``nodes`` table for direct children;
           subscribe to each child's readable channels.

        A blind node (config ``blind``) seeds channels only -- steps 2
        and 3 are skipped, so it reads nothing; the parent's own
        subscription to it (``child_add``) is unaffected.
        """
        # seed default channels -- keyed on (node, channel), so a re-init keeps
        # every channel_id stable and heals tampered flags in place
        db = self.db
        branch = self.node.branch
        for channel in _DEFAULT_CHANNELS:
            db.merge(
                data={'node': branch, **channel},
                table='channels',
                conflict=['node', 'channel'],
            )
        # a blind node holds no subscriptions of its own -- skip both passes
        # (the parent's watch of this node is seeded by child_add, not here)
        if self.node.config.get('blind'):
            return
        # subscribe to the parent's readable channels -- only below the tree
        # root: child segments never contain dots, but the root's own git
        # branch may (v1.0), so dot-counting would invent a phantom parent
        root = self.node.config.get('root')
        if branch != root:
            parent_branch, *_ = branch.rsplit('.', 1)
            self.subscribe(parent_branch)
        # subscribe to each direct child's readable channels (the registry is
        # tree-wide, so match on the subtree prefix, not depth alone)
        prefix = f'{branch}.'
        current_depth = branch.count('.')
        for row in db.read('nodes'):
            child_branch = row['node']
            if not child_branch.startswith(prefix):
                continue
            if child_branch.count('.') == current_depth + 1:
                self.subscribe(child_branch)

    def send(
        self: Radio,
        node: Optional[str] = None,
        channel: str = 'inbox',
        *,
        parent: bool = False,
        subject: str,
        data: str,
        priority: int,
        post: bool = False,
        relay_of: Optional[str] = None,
    ) -> tuple[str, str, str]:
        """Write a message to a node's channel.

        The message lands in the target's channel-space (the row's
        ``node`` column names the host). Checks ``write_only``
        permission and rejects if ``write_only = 1`` and the writer
        is not the owner.

        The permission check is best-effort, not atomic across connections: the
        ``write_only`` read and the ``db.write`` use separate connections, so a
        concurrent ``channel_delete`` of the target channel can race between them.
        Strict atomicity is intentionally not provided.

        Args:
            node: Target node branch. Defaults to self.
            channel: Channel name. Defaults to ``'inbox'``.
            parent: Send to the parent node (mutex with ``node``).
            subject: Message subject.
            data: Message data.
            priority: Priority (0-10).
            post: Channel-class guard: ``True`` requires a publicly
                readable channel (``read_only = 0``, the ``post`` verb);
                ``False`` (the ``send`` verb and internal callers)
                skips the check.
            relay_of: UUID of the message this send relays onward; the copy
                carries ``relay:<uuid>`` metadata, making relay obligations
                verifiable from the store (:meth:`relays`).

        Returns:
            Tuple of the message's 8-char UUID and the resolved
            destination ``(node, channel)``, symmetric with ``reply``,
            so callers can surface the routing without re-deriving it.

        Raises:
            ValueError: If ``parent`` and ``node`` are both set, the
                parent or target node is not found, ``priority`` is out
                of range, the channel is not found, ``post`` is set
                and the channel is privately readable, or ``relay_of``
                references no known message.
            PermissionError: If the target channel is write-only and the
                sender is not the owner.

        """
        # validate mutex
        if parent and node:
            raise ValueError('--parent and --node are mutually exclusive.')
        # resolve parent branch -- the tree root has none, and its git
        # branch may itself contain dots, so derive only below the root
        if parent:
            branch = self.node.branch
            if branch == self.node.config.get('root'):
                raise ValueError('No parent node (top-level branch).')
            node, *_ = branch.rsplit('.', 1)
        # validate priority
        self._validate_priority(priority)
        # resolve the target and validate the channel on it
        target = self._resolve_target(node)
        rows = self.db.read(
            'channels',
            where={'node': target, 'channel': channel},
            limit=1,
        )
        if not rows:
            # the remedy keys on how the target was named: an unnamed target
            # may mean a channel on a node the caller forgot to name
            if not node:
                raise ValueError(
                    f'No {channel!r} channel found: specify a target node or create it.'
                )
            if target == self.node.branch:
                raise ValueError(f'No {channel!r} channel found: create it first.')
            raise ValueError(f'Node {target} has no channel {channel!r}.')
        # enforce post's channel class: post writes publicly readable channels
        # only (custom channels obey their own flags) -- a mismatch names the
        # sibling verb; send routes anywhere write permissions allow
        if post and rows[0]['read_only']:
            raise ValueError(
                f"Channel {channel!r} is privately readable; use 'fractal radio send'."
            )
        if target != self.node.branch and rows[0]['write_only']:
            raise PermissionError(f'Channel {channel!r} is write-only (owner only).')
        # a relay must reference a real message -- a typo'd mark is worse
        # than none (the obligation check would read the relay as never
        # executed); the archive answers for an original its host unsent
        if relay_of is not None:
            relay_of = relay_of.upper()
            known = self.db.exists(
                'messages',
                where={'message_uuid': relay_of},
            ) or self.db.exists('archive', where={'message_uuid': relay_of})
            if not known:
                raise ValueError(f'Relay reference not found: {relay_of!r}')
        # write message
        message_uuid = self._unique_uuid()
        session = self._sender_session()
        row = {
            'node': target,
            'message_uuid': message_uuid,
            'channel': channel,
            'sender': self.node.branch,
            'session': session,
            'priority': priority,
            'subject': subject,
            'data': data,
        }
        if relay_of is not None:
            row['metadata'] = f'relay:{relay_of}'
        self.db.write(row, 'messages')
        return message_uuid, target, channel

    def send_many(
        self: Radio,
        nodes: list[str],
        channel: str = 'inbox',
        *,
        subject: str,
        data: str,
        priority: int,
        relay_of: Optional[str] = None,
        receipt: Optional[Callable[[str, str, str], None]] = None,
    ) -> list[tuple[str, str, str]]:
        """Send one copy of a message to each recipient, with receipts.

        The fan-out primitive for fleet orders: every target is resolved
        and its channel checked before any row lands, so a bad recipient
        refuses the whole fan-out instead of stranding a partial delivery
        (the same reads :meth:`send` runs are taken as a dry pass; the
        usual best-effort caveat about concurrent channel edits applies).
        Each copy is its own message with its own UUID -- the returned
        receipts are the per-recipient delivery record.

        Args:
            nodes: Target node branches, one copy each.
            channel: Channel name on every target. Defaults to ``'inbox'``.
            subject: Message subject.
            data: Message data.
            priority: Priority (0-10).
            relay_of: UUID of the message the copies relay onward.
            receipt: Called with each ``(uuid, node, channel)`` the moment
                that copy lands, so a delivery already made is reported
                even when a later recipient raises.

        Returns:
            List of ``(uuid, node, channel)`` receipts, in recipient order.

        Raises:
            ValueError: If any recipient fails :meth:`send`'s validation
                (unknown node, missing channel, bad priority or relay
                reference) -- raised before any copy is written.
            PermissionError: If any target channel is write-only for this
                sender.

        """
        # dry pass: every recipient validates before the first row lands
        self._validate_priority(priority)
        for node in nodes:
            target = self._resolve_target(node)
            rows = self.db.read(
                'channels',
                where={'node': target, 'channel': channel},
                limit=1,
            )
            if not rows:
                raise ValueError(f'Node {target} has no channel {channel!r}.')
            if target != self.node.branch and rows[0]['write_only']:
                raise PermissionError(
                    f'Channel {channel!r} is write-only (owner only).'
                )
        # deliver -- send re-validates each route (cheap, and it owns the row).
        # Each landing is reported as it happens: the dry pass cannot rule out
        # a mid-loop failure (a channel deleted between the passes), and a
        # delivered copy whose receipt died with the exception is exactly the
        # phantom the receipts exist to prevent
        receipts = []
        for node in nodes:
            landed = self.send(
                node,
                channel,
                subject=subject,
                data=data,
                priority=priority,
                relay_of=relay_of,
            )
            receipts.append(landed)
            if receipt is not None:
                receipt(*landed)
        return receipts

    def relays(self: Radio, message_uuid: str) -> list[Row]:
        """List the relayed copies of a message (its recorded relay lineage).

        The descendant-relay obligation check: an order that must be passed
        onward is verifiable from the store -- every copy sent with
        ``relay_of`` carries ``relay:<uuid>`` metadata, so an empty listing
        means no relay of the message was ever recorded. The lineage keys
        on those marks alone: a withdrawn original (unsend deletes the
        original but leaves the relay copies) stays auditable.

        Args:
            message_uuid: UUID of the original (relayed) message.

        Returns:
            List of relayed-copy message dicts with counts, in the shared
            listing order (priority first) -- minus rows this node hosts
            while its own seal binds.

        Raises:
            ValueError: If ``message_uuid`` names no known message and no
                recorded relay carries its mark.

        """
        message_uuid = message_uuid.upper()
        rows = self._query_messages(
            metadata=f'relay:{message_uuid}',
            roots_only=False,
        )
        # an unknown uuid must not answer 'no relays': the obligation check
        # reads an empty listing as "the relay never happened", so a typo
        # would indict a node that relayed faithfully -- the gate applies
        # to an empty lineage only, since recorded marks answer for a
        # withdrawn original themselves
        if not rows:
            known = self.db.exists(
                'messages',
                where={'message_uuid': message_uuid},
            ) or self.db.exists('archive', where={'message_uuid': message_uuid})
            if not known:
                raise self._message_not_found(message_uuid)
        # a bound seal holds hosted rows out of this listing too
        if self.seal_binds():
            rows = [row for row in rows if row['node'] != self.node.branch]
        return rows

    def unsend(
        self: Radio,
        message_uuid: str,
        *,
        force: bool = False,
    ) -> None:
        """Delete a sent message and its replies.

        Only the sender can unsend. A message with replies is refused
        unless ``force`` is set, since deleting a thread also removes
        other nodes' replies. With ``force``, cascades to replies,
        reacts, and read receipts (the schema has no ``ON DELETE
        CASCADE``).

        The cascade is best-effort, not atomic across connections: each
        ``db.delete`` opens its own connection, so a reply that arrives mid-cascade
        from another node can race it (the pre-delete re-check narrows but does not
        close the window). Strict atomicity is intentionally not provided.

        Args:
            message_uuid: 8-char message UUID.
            force: Delete the whole thread even if it has replies.

        Raises:
            ValueError: If the message is not found, or it has replies
                and ``force`` is not set.
            PermissionError: If caller is not the sender.

        """
        # find message by UUID (globally unique; canonicalize for the errors)
        db = self.db
        message = self._find_message(message_uuid)
        message_uuid = message['message_uuid']
        # verify sender
        if message['sender'] != self.node.branch:
            raise PermissionError('Only the sender can unsend.')
        # collect all message IDs in the tree (root + replies)
        message_ids = []
        self._collect_message_ids(message['message_id'], message_ids)
        # refuse to delete a thread unless forced -- the cascade
        # also removes replies authored by other nodes
        if len(message_ids) > 1 and not force:
            raise ValueError(
                f'Message {message_uuid} has replies;'
                ' use --force to delete the whole thread.'
            )
        # re-collect immediately before deleting: a reply that arrived after the
        # first walk would otherwise be re-rooted into an orphan by the cascade
        re_check = []
        self._collect_message_ids(message['message_id'], re_check)
        if len(re_check) > len(message_ids):
            raise ValueError(
                f'Message {message_uuid} gained a reply mid-unsend;'
                ' retry to delete the whole thread.'
            )
        # cascade delete reacts, reads, then messages
        for mid in message_ids:
            db.delete('reacts', where={'message_id': mid})
            db.delete('reads', where={'message_id': mid})
            db.delete('messages', where={'message_id': mid})

    def seal_binds(self: Radio) -> bool:
        """Return whether this node's inbox seal binds the current caller.

        The seal (``sealed`` config) guards the sealed seat's own context:
        it binds when the acting node IS the sealed node -- resolved
        env-first (the loop exports ``_NODE`` for every step subprocess)
        and, when that is unset, from the directory the call runs in, so
        an env scrub does not lift the seal off a seat working in its own
        worktree (:meth:`Node.resolve_actor` records the residual). An
        operator shell outside the node adjudicates freely. Hosted
        messages are held from every listing and from the archive, the
        body surface (:meth:`read`) refuses outright, and every verb that
        would curate or adjudicate a hosted row refuses too --
        :meth:`save` and :meth:`unsave` (the archive is a body surface,
        and its integrity is the adjudicator's), :meth:`react` and
        :meth:`reply` (a seat that may not read a message may not answer
        it, and the reply's routing names its sender). The seal is also
        not the seat's to lift: clearing ``sealed`` from inside refuses.
        The node's own writes stay visible in listings (verdicts still
        file).
        """
        if not self.node.config.get('sealed'):
            return False
        actor = self.node.resolve_actor()
        return actor is not None and actor.branch == self.node.branch

    def messages(
        self: Radio,
        *,
        channel: Optional[str] = None,
        limit: Optional[int] = None,
        since: Optional[str] = None,
        read: Optional[bool] = None,
        recent: bool = False,
    ) -> list[Row]:
        """Query this node's messages with reply/react counts.

        Listing is always passive -- it never writes read receipts, so the
        unread view is stable across calls; ``read`` is the receipt-writing
        surface.

        Args:
            channel: Filter by channel name.
            limit: Maximum rows to return.
            since: Only messages after this ISO 8601 timestamp.
            read: ``None`` = all, ``False`` = unread only,
                ``True`` = read only.
            recent: Sort by ``created_at DESC`` instead of
                ``priority DESC``.

        Returns:
            List of message dicts with ``replies``, ``pos_reacts``,
            and ``neg_reacts`` counts -- empty while the seal binds
            (:meth:`seal_binds`): every hosted message is held out of the
            sealed seat's view.

        Raises:
            ValueError: If ``channel`` names no channel on this node.

        """
        if channel is not None:
            self._require_channel(channel)
        if self.seal_binds():
            return []
        return self._query_messages(
            node=self.node.branch,
            channel=channel,
            limit=limit,
            since=since,
            read=read,
            recent=recent,
        )

    def sent(
        self: Radio,
        *,
        channel: Optional[str] = None,
        limit: Optional[int] = None,
        since: Optional[str] = None,
        recent: bool = False,
    ) -> list[Row]:
        """Query messages this node sent, across every host's channel-space.

        Each row's ``node`` column names the recipient (the channel host).
        Replies list first-class -- unlike the mailbox listings, an authored
        reply that threads in place is not hidden behind its parent's reply
        count.

        Args:
            channel: Filter by channel name.
            limit: Maximum rows to return.
            since: Only messages after this ISO 8601 timestamp.
            recent: Sort by ``created_at DESC`` instead of
                ``priority DESC``.

        Returns:
            List of message dicts with ``replies``, ``pos_reacts``,
            and ``neg_reacts`` counts.

        """
        return self._query_messages(
            sender=self.node.branch,
            channel=channel,
            limit=limit,
            since=since,
            recent=recent,
            roots_only=False,
        )

    def feed(
        self: Radio,
        *,
        node: Optional[str] = None,
        channel: Optional[str] = None,
        limit: Optional[int] = None,
        since: Optional[str] = None,
        read: Optional[bool] = None,
        recent: bool = False,
    ) -> list[Row]:
        """Fan out reads across subscriptions and merge the rows.

        Reads this node's subs, optionally filtered by target node and/or
        channel. For each matching subscription, queries the target's
        channel where ``read_only = 0``. Merges and re-sorts by priority
        DESC / created_at ASC. Each row's ``node`` column names its
        source. Listing is always passive -- it never writes read
        receipts, so the unread view is stable across calls;
        ``read(feed=True)`` is the receipt-writing surface.

        Args:
            node: Filter subscriptions by target node.
            channel: Filter subscriptions by channel.
            limit: Maximum total rows to return.
            since: Only messages after this ISO 8601 timestamp.
            read: ``None`` = all, ``False`` = unread only,
                ``True`` = read only.
            recent: Sort by ``created_at DESC`` instead of
                ``priority DESC``.

        Returns:
            Merged and sorted list of message dicts.

        Raises:
            ValueError: If ``node`` is not registered, or ``channel``
                matches no subscription this feed reads from.

        """
        # a filter that can only ever match nothing is a typo, not an empty
        # feed: an empty listing is a record consumers grade verdicts off, so
        # refuse it the way the subscribing side refuses the identical typo
        if node is not None:
            node = self._resolve_target(node)
        if channel is not None:
            self._require_subscription(channel, node=node)
        # fan out across the own subscriptions; the cap applies post-merge
        messages = self._feed_messages(
            self.node.branch,
            node=node,
            channel=channel,
            since=since,
            read=read,
            recent=recent,
        )
        # a bound seal holds hosted rows here too -- a self-subscription to
        # an own hosted channel must not tunnel sealed mail into the seat
        if self.seal_binds():
            messages = [row for row in messages if row['node'] != self.node.branch]
        if limit is not None:
            messages = messages[:limit]
        # return rows
        return messages

    def read(
        self: Radio,
        *message_uuids: str,
        node: Optional[str] = None,
        channel: Optional[str] = None,
        feed: bool = False,
        unread: bool = False,
    ) -> list[Row]:
        """Read messages by UUID and/or selector, receipting each as this node.

        The body surface: reading is the act that consumes unread state
        (listing never does), and every returned row lands this node's
        receipt in the ``reads`` table -- receipts always attribute to
        the actual reader, never to the mailbox viewed. UUIDs resolve
        globally (unique across the tree) and return in argument order;
        selector rows follow priority DESC / created_at ASC and mirror
        the listings' row sets (thread roots plus cross-space replies);
        duplicates collapse to their first occurrence. Receipts land
        only after every lookup resolves, so a failed UUID receipts
        nothing.

        Args:
            *message_uuids: 8-char message UUIDs.
            node: Mailbox node branch the selectors view. Defaults to self.
            channel: Read this channel's messages.
            feed: Read messages from the subscribed nodes.
            unread: Restrict the selectors to rows this reader has no
                receipt for (explicit UUIDs always return).

        Returns:
            Full message dicts with data and reply/react counts.

        Raises:
            ValueError: If a message or the mailbox node is not found.
            PermissionError: If a channel is read-only and the reader
                is not the owner, or this reader's own seal binds.

        """
        # a bound seal refuses the body surface outright -- reading is the
        # act the seal exists to prevent (a routine triage read is exactly
        # how sealed adjudication traffic contaminates a verifier's context)
        if self.seal_binds():
            raise PermissionError(
                'inbox sealed: messages are held until lawfully unsealed'
            )
        messages = []
        # resolve explicit uuids globally, in argument order
        for message_uuid in message_uuids:
            message = self._find_message(message_uuid)
            # reject a non-owner reaching a read-only channel
            self._reject_read_only(message['channel'], message['node'])
            messages.append(message)
        # the unread filter follows this reader's receipts
        read = False if unread else None
        # resolve the mailbox once -- both selectors view its channel-space
        mailbox = self._resolve_target(node)
        # expand the channel selector over the mailbox's channel-space
        if channel is not None:
            self._reject_read_only(channel, mailbox)
            messages += self._query_messages(
                node=mailbox,
                channel=channel,
                read=read,
            )
        # expand the feed selector across the mailbox's subscriptions
        if feed:
            messages += self._feed_messages(mailbox, read=read)
        # collapse duplicates (a uuid may also match a selector), keeping first
        result = []
        seen = set()
        for message in messages:
            if message['message_id'] in seen:
                continue
            seen.add(message['message_id'])
            result.append(message)
        # write this reader's receipt for exactly the returned rows
        for message in result:
            self._mark_read(message)
        return result

    def thread(
        self: Radio,
        message_uuid: str,
        *,
        read: Optional[bool] = None,
    ) -> list[Row]:
        """Return the full reply tree for a message.

        Finds root message (walks up ``parent_message_id``), collects
        all replies recursively. Returns flat list ordered by
        ``created_at`` ASC with ``depth`` field for indentation.

        Args:
            message_uuid: 8-char message UUID.
            read: ``None`` = all, ``False`` = unread only,
                ``True`` = read only.

        Returns:
            Flat list of message dicts with ``depth`` field.

        Raises:
            ValueError: If the message is not found.
            PermissionError: If the channel is read-only and the reader
                is neither the owner nor a thread participant.

        """
        # find message by UUID
        db = self.db
        message = self._find_message(message_uuid)
        # walk up to root in one recursive query -- a missing parent ends the
        # climb, so the deepest reachable ancestor roots the thread
        query = (
            'WITH RECURSIVE lineage(message_id, parent_message_id, height) AS ('
            ' SELECT message_id, parent_message_id, 0'
            ' FROM messages WHERE message_id = ?'
            ' UNION ALL'
            ' SELECT m.message_id, m.parent_message_id, lineage.height + 1'
            ' FROM messages m JOIN lineage ON m.message_id = lineage.parent_message_id'
            ')'
            ' SELECT * FROM messages WHERE message_id ='
            ' (SELECT message_id FROM lineage ORDER BY height DESC LIMIT 1)'
        )
        rows = db.read(query=query, params=(message['message_id'],))
        # a concurrent unsend can delete the message between the lookup and
        # the climb (separate connections) -- surface it as not-found
        if not rows:
            raise self._message_not_found(message_uuid)
        root, *_ = rows
        # collect the full tree first -- the permission decision below needs
        # every participant, and the read filter applies only afterwards so a
        # matching descendant under a filtered-out ancestor is not pruned
        result = self._collect_thread(root)
        # reject a non-owner reaching a read-only channel -- thread participants
        # are exempt, so a rerouted conversation reads whole for both parties
        branch = self.node.branch
        participant = any(
            row['sender'] == branch or row['node'] == branch for row in result
        )
        if not participant:
            # a bystander may still name a publicly-readable root (an outbox
            # broadcast), so gate the named message, then drop every row it is
            # not authorized to read -- reply-rerouting chains read-only inbox
            # rows under a readable root, and returning the whole tree would
            # leak those bodies (a direct read of them is refused)
            self._reject_read_only(message['channel'], message['node'])
            result = [row for row in result if self._readable(row)]
        # apply the read filter to each row (following the caller's receipts)
        if read is not None:
            kept = []
            for row in result:
                receipts = db.read(
                    'reads',
                    where={'message_id': row['message_id'], 'node': branch},
                    limit=1,
                )
                include = bool(receipts) if read else not receipts
                if include:
                    kept.append(row)
            result = kept
        # a bound seal holds this node's hosted rows out of the tree -- a
        # reply rerouted into the sealed inbox must not surface here either
        if self.seal_binds():
            result = [row for row in result if row['node'] != branch]
        return result

    def reply(
        self: Radio,
        message_uuid: str,
        data: str,
        *,
        priority: Optional[int] = None,
    ) -> tuple[str, str, str]:
        """Reply to a message.

        Locates the parent by UUID (globally unique) and routes by where
        the parent sits: a reply to a message in the caller's own inbox
        is a conversation turn, so it lands in the original sender's
        inbox; so does a reply into another node's write-only channel --
        the reaction to a broadcast reaches its author's inbox rather
        than piercing the owner-only channel; any other reply is
        created in the parent's host channel-space. ``thread``
        walks parent ids regardless of host, so a rerouted conversation
        still renders as one thread. Inherits subject (``Re: ...``) and
        priority from the parent if not specified. Also marks the parent
        read (like ``react``) so a replied-to message does not resurface
        every SYNC.

        The channel-flag probe is best-effort, not atomic across
        connections: the channel read and the ``db.write`` use separate
        connections, so a concurrent ``channel_delete`` or ``unsend`` of the
        parent can race between them. Strict atomicity is intentionally not
        provided.

        Args:
            message_uuid: 8-char UUID of the parent message.
            data: Reply data.
            priority: Reply priority (0-10). Inherited if omitted.

        Returns:
            Tuple of the reply's 8-char message UUID and the resolved
            destination ``(node, channel)``, so callers can surface the
            routing the way ``send`` does.

        Raises:
            ValueError: If the parent message is not found, or
                ``priority`` is out of range.
            PermissionError: If the parent's channel is read-only and the
                replier is neither the host owner nor the original sender,
                or the seal binds and the parent is one this node hosts.

        """
        # find parent
        db = self.db
        parent = self._find_message(message_uuid)
        # a held message is not a conversation the sealed seat is in: the
        # routing this would resolve names the held message's sender, and the
        # reply itself moves counts on a row the seat may not read
        if parent['node'] == self.node.branch and self.seal_binds():
            raise PermissionError(
                'inbox sealed: hosted messages cannot be replied to until'
                ' lawfully unsealed'
            )
        # a bystander cannot reply into a read-only channel: the reply would
        # write there and mark the parent read, and it would make the caller a
        # thread participant, defeating thread()'s bystander gate; participants
        # -- the parent's host owner and its original sender -- are exempt
        # (continuing their own thread), mirroring thread()'s participant rule
        if self.node.branch not in (parent['node'], parent['sender']):
            self._reject_read_only(parent['channel'], parent['node'])
        # probe the parent's channel flags (write-only reroutes the reply below)
        write_only = False
        if parent['node'] != self.node.branch:
            rows = db.read(
                'channels',
                where={'node': parent['node'], 'channel': parent['channel']},
                limit=1,
            )
            write_only = bool(rows and rows[0]['write_only'])
        # inherit subject and priority, normalizing any existing reply prefix to
        # a single canonical 'Re: ' (a parent 'RE: x' / 're: x' becomes 'Re: x')
        subject = parent['subject']
        if subject.lower().startswith('re: '):
            subject = subject[len('Re: ') :]
        subject = f'Re: {subject}'
        if priority is None:
            priority = parent['priority']
        self._validate_priority(priority)
        # route the reply: a turn in my own inbox or into a write-only channel
        # goes back to the sender's inbox; anything else threads in place
        own_inbox = parent['node'] == self.node.branch and parent['channel'] == 'inbox'
        if own_inbox or write_only:
            node = parent['sender']
            channel = 'inbox'
        else:
            node = parent['node']
            channel = parent['channel']
        # create the reply
        reply_uuid = self._unique_uuid()
        session = self._sender_session()
        row = {
            'node': node,
            'message_uuid': reply_uuid,
            'parent_message_id': parent['message_id'],
            'parent_message_uuid': parent['message_uuid'],
            'channel': channel,
            'sender': self.node.branch,
            'session': session,
            'priority': priority,
            'subject': subject,
            'data': data,
        }
        db.write(row, 'messages')
        # also mark the parent read so replying clears it from SYNC's inbox/feed
        # -- otherwise a replied-to message resurfaces every sync (mirror react())
        self._mark_read(parent)
        return reply_uuid, node, channel

    def react(
        self: Radio,
        message_uuid: str,
        value: int,
    ) -> None:
        """React to a message with 1 or -1.

        Locates the message by UUID (globally unique) and upserts into
        ``reacts``, so re-reacting changes the value. Also marks the
        message read (a ``reads`` receipt), mirroring ``read``, so an
        ack clears it from SYNC.

        Args:
            message_uuid: 8-char message UUID.
            value: Reaction value (``1`` or ``-1``).

        Raises:
            ValueError: If ``value`` is not ``1`` or ``-1``, or the
                message is not found.
            PermissionError: If the channel is read-only and the reactor
                is not the owner, or the seal binds and the message is
                one this node hosts.

        """
        # validate value
        if value not in (1, -1):
            raise ValueError('Value must be 1 or -1.')
        # find message by UUID
        message = self._find_message(message_uuid)
        # reject a non-owner reaching a read-only channel
        self._reject_read_only(message['channel'], message['node'])
        # a seat that may not read a hosted message may not adjudicate it
        # either: the react moves counts its adjudicator will later read
        if message['node'] == self.node.branch and self.seal_binds():
            raise PermissionError(
                'inbox sealed: hosted messages cannot be reacted to until'
                ' lawfully unsealed'
            )
        # write react -- keyed on (message_id, node), so a re-react updates the
        # value in place instead of minting a new react_id
        data = {
            'message_id': message['message_id'],
            'node': self.node.branch,
            'channel': message['channel'],
            'value': value,
        }
        self._receipt('reacts', data, message)
        # also mark the message read so reacting clears it from SYNC's inbox/feed
        # (otherwise the same items resurface every sync, burning tokens)
        self._mark_read(message)

    def save(
        self: Radio,
        message_uuid: str,
    ) -> None:
        """Save a message to the archive.

        Locates the message by UUID (globally unique) and copies it into
        the ``archive`` table -- an owned snapshot that survives unsend.
        Re-saving is idempotent.

        Args:
            message_uuid: 8-char message UUID.

        Raises:
            ValueError: If the message is not found.
            PermissionError: If the channel is read-only and the saver
                is not the owner.

        """
        # find message by UUID
        message = self._find_message(message_uuid)
        # reject a non-owner reaching a read-only channel
        self._reject_read_only(message['channel'], message['node'])
        # the archive is a body surface: saving a hosted message would copy
        # its data into a table the seat can read back, tunneling exactly
        # what the seal holds out of its context
        if message['node'] == self.node.branch and self.seal_binds():
            raise PermissionError(
                'inbox sealed: hosted messages cannot be archived until'
                ' lawfully unsealed'
            )
        # copy into the archive (node = saver, owner = the message's host)
        row = {
            'node': self.node.branch,
            'message_id': message['message_id'],
            'message_uuid': message['message_uuid'],
            'parent_message_id': message.get('parent_message_id'),
            'parent_message_uuid': message.get('parent_message_uuid'),
            'channel': message['channel'],
            'sender': message['sender'],
            'session': message.get('session'),
            'owner': message['node'],
            'priority': message['priority'],
            'subject': message['subject'],
            'data': message['data'],
            'metadata': message.get('metadata', ''),
            'created_at': message['created_at'],
        }
        self.db.merge(row, 'archive', conflict=['node', 'message_uuid'])

    def unsave(
        self: Radio,
        message_uuid: str,
    ) -> None:
        """Remove a message from the archive.

        Args:
            message_uuid: 8-char message UUID.

        Raises:
            ValueError: If this node has no archived copy of the message.
            PermissionError: If the seal binds and the archived copy is of
                a message this node hosts.

        """
        # process message uuid
        message_uuid = message_uuid.upper()
        # the archive keys on (node, uuid); scope the lookup to this saver
        where = {'node': self.node.branch, 'message_uuid': message_uuid}
        # find the archived message
        rows = self.db.read('archive', where=where, limit=1)
        if not rows:
            raise self._message_not_found(message_uuid)
        # the seal holds the archive's integrity, not just its confidentiality:
        # a seat that cannot read a hosted row must not delete the copy its
        # adjudicator (or its own pre-seal self) deliberately kept
        if rows[0]['owner'] == self.node.branch and self.seal_binds():
            raise PermissionError(
                'inbox sealed: hosted messages cannot be unarchived until'
                ' lawfully unsealed'
            )
        # delete from archive
        self.db.delete('archive', where=where)

    def saved(
        self: Radio,
        *,
        node: Optional[str] = None,
        channel: Optional[str] = None,
        limit: Optional[int] = None,
        since: Optional[str] = None,
        recent: bool = False,
    ) -> list[Row]:
        """List saved messages from the archive.

        Args:
            node: Filter by the copy's source host (the ``owner``
                column).
            channel: Filter by channel name.
            limit: Maximum rows to return.
            since: Only messages saved after this ISO 8601
                timestamp.
            recent: Sort by ``created_at DESC`` instead of
                ``priority DESC``.

        Returns:
            List of archived message dicts.

        """
        # build query -- the node filter keys on owner: the archive's own
        # node column names the saver (always self here), owner the source
        conditions = ['node = ?']
        params = [self.node.branch]
        if node is not None:
            conditions.append('owner = ?')
            params.append(node)
        if channel is not None:
            conditions.append('channel = ?')
            params.append(channel)
        if since is not None:
            conditions.append('created_at > ?')
            params.append(since)
        query = 'SELECT * FROM archive'
        query += ' WHERE ' + ' AND '.join(conditions)
        if recent:
            query += ' ORDER BY created_at DESC'
        else:
            query += ' ORDER BY priority DESC, created_at ASC'
        if limit is not None:
            query += ' LIMIT ?'
            params.append(limit)
        # execute query
        rows = self.db.read(query=query, params=tuple(params))
        # a bound seal holds hosted rows here too -- an archive taken before
        # the seal landed must not read the body back out through --saved
        if self.seal_binds():
            rows = [row for row in rows if row['owner'] != self.node.branch]
        return rows

    def channel(
        self: Radio,
        name: str,
        *,
        read_only: bool = False,
        write_only: bool = False,
    ) -> None:
        """Register a custom channel.

        Defaults to public (both ``False`` = everyone can read
        and write). Rejects default channel names as reserved,
        and an already-registered channel name as a duplicate --
        create never silently overwrites an existing channel's
        flags (use ``channel_delete`` then recreate to change them).

        Args:
            name: Channel name.
            read_only: Only owner can read.
            write_only: Only owner can write.

        Raises:
            ValueError: If the channel is reserved or already exists.

        """
        # check reserved names
        reserved = {channel['channel'] for channel in _DEFAULT_CHANNELS}
        if name in reserved:
            raise ValueError(f'Channel name is reserved: {name!r}')
        # reject a duplicate -- a whole-row replace resets flags and mints a new
        # channel_id, silently downgrading e.g. a --read-only channel
        branch = self.node.branch
        if self.db.exists('channels', where={'node': branch, 'channel': name}):
            raise ValueError(f'Channel already exists: {name!r}')
        # write channel
        data = {
            'node': branch,
            'channel': name,
            'read_only': int(read_only),
            'write_only': int(write_only),
        }
        self.db.merge(data, 'channels', conflict=['node', 'channel'])

    def channel_delete(self: Radio, name: str, *, force: bool = False) -> None:
        """Delete a custom channel and all of its data.

        Rejects default channel names as reserved. A channel that still
        holds messages is refused unless ``force`` is set, since the
        cascade also removes other nodes' replies (mirroring ``unsend``).
        With ``force``, cascades to the channel's messages, reacts, and
        read receipts (the schema has no ``ON DELETE CASCADE``) before
        removing its subscriptions and the channel row, so no orphaned
        message outlives the channel definition that gates its read-only
        access. Scoped to this node's channel-space -- another node's
        same-named channel is untouched.

        The cascade is best-effort, not atomic across connections: each
        ``db.delete`` opens its own connection, so a send/reply into the channel
        that races the cascade can leave a row behind the channel row's removal.
        Strict atomicity is intentionally not provided.

        Args:
            name: Channel name.
            force: Delete the channel even if it still holds messages.

        Raises:
            ValueError: If the channel is reserved, not found, or it
                holds messages and ``force`` is not set.

        """
        # check reserved names
        reserved = {channel['channel'] for channel in _DEFAULT_CHANNELS}
        if name in reserved:
            raise ValueError(f'Cannot delete default channel: {name!r}')
        branch = self.node.branch
        where = {'node': branch, 'channel': name}
        if not self.db.exists('channels', where=where):
            raise self._channel_not_found(name)
        # refuse to delete a channel that still holds messages unless forced --
        # the cascade also removes replies authored by other nodes
        count = self.db.count('messages', where=where)
        if count and not force:
            s = 's' if count != 1 else ''
            raise ValueError(
                f'Channel {name} has {count} message{s};'
                ' use --force to delete it and them.'
            )
        # cascade delete the channel's messages, receipts, subs, and channel
        # (the schema has no ON DELETE CASCADE) -- an orphaned message that
        # outlived its channel definition would no longer be gated as read-only
        for row in self.db.read('messages', where=where):
            self.db.delete('reacts', where={'message_id': row['message_id']})
            self.db.delete('reads', where={'message_id': row['message_id']})
        self.db.delete('messages', where=where)
        self.db.delete('subs', where={'target': branch, 'channel': name})
        self.db.delete('channels', where=where)

    def channels(self: Radio) -> list[Row]:
        """List this node's channels."""
        return self.db.read('channels', where={'node': self.node.branch})

    def subscribe(
        self: Radio,
        node: str,
        *,
        channel: Optional[str] = None,
    ) -> None:
        """Subscribe to a node's channel.

        If ``channel`` is omitted, subscribes to all readable
        (``read_only = 0``) channels on the target node.

        Args:
            node: Target node branch.
            channel: Channel name. All readable if omitted.

        """
        # resolve the target (validates it is registered)
        target = self._resolve_target(node)
        # subscribe to channel
        if channel is not None:
            # validate the channel exists and is readable on the target
            # (same rule the all-channels branch applies below)
            rows = self.db.read(
                'channels',
                where={'node': target, 'channel': channel},
                limit=1,
            )
            if not rows:
                raise ValueError(f'Channel not found on {target!r}: {channel!r}')
            if rows[0]['read_only']:
                raise ValueError(
                    f'Channel {channel!r} on {target!r} cannot be subscribed.'
                )
            data = {'node': self.node.branch, 'target': target, 'channel': channel}
            self.db.merge(data, 'subs', conflict=['node', 'target', 'channel'])
        else:
            # subscribe to all readable channels on target
            for row in self.db.read('channels', where={'node': target}):
                if not row['read_only']:
                    data = {
                        'node': self.node.branch,
                        'target': target,
                        'channel': row['channel'],
                    }
                    self.db.merge(data, 'subs', conflict=['node', 'target', 'channel'])

    def unsubscribe(
        self: Radio,
        node: str,
        *,
        channel: Optional[str] = None,
    ) -> int:
        """Unsubscribe from a node's channel.

        If ``channel`` is omitted, removes all subscriptions
        to the target node.

        Args:
            node: Target node branch.
            channel: Channel name. All if omitted.

        Returns:
            Number of subscriptions removed -- 0 when nothing matched,
            so a mis-pathed unsubscribe is visible to the caller.

        """
        where = {'node': self.node.branch, 'target': node}
        if channel is not None:
            where['channel'] = channel
        return self.db.delete('subs', where=where)

    def subs(self: Radio) -> list[Row]:
        """List this node's subscriptions."""
        return self.db.read('subs', where={'node': self.node.branch})

    def _require_channel(self: Radio, channel: str) -> None:
        """Refuse a mailbox filter naming a channel this node does not host.

        An empty listing is a record -- instruments and agents grade
        verdicts off it -- so a filter that can only ever be empty must
        refuse rather than answer zero rows under a ``0 unread (0 total)``
        notice indistinguishable from a genuinely empty mailbox. The write
        side (:meth:`send`) refuses the identical typo.

        Args:
            channel: The channel name filtered on.

        Raises:
            ValueError: If this node hosts no such channel.

        """
        branch = self.node.branch
        if self.db.exists('channels', where={'node': branch, 'channel': channel}):
            return
        known = ', '.join(sorted(row['channel'] for row in self.channels()))
        raise ValueError(f'No {channel!r} channel on {branch} (has: {known}).')

    def _require_subscription(
        self: Radio,
        channel: str,
        *,
        node: Optional[str] = None,
    ) -> None:
        """Refuse a feed filter matching none of this node's subscriptions.

        The feed reads subscriptions, so a channel it holds none of can
        only ever render empty -- the same false record
        :meth:`_require_channel` refuses on the mailbox surface.

        Args:
            channel: The channel name filtered on.
            node: The target node the filter is scoped to, if any.

        Raises:
            ValueError: If no subscription matches.

        """
        where = {'node': self.node.branch, 'channel': channel}
        if node is not None:
            where['target'] = node
        if self.db.exists('subs', where=where):
            return
        known = ', '.join(sorted({row['channel'] for row in self.subs()})) or 'none'
        scope = f' on {node}' if node is not None else ''
        raise ValueError(
            f'No {channel!r} subscription{scope} to feed from (subscribed: {known}).'
        )

    def _resolve_target(self: Radio, node: Optional[str]) -> str:
        """Normalize a target to a branch name, defaulting to the own node.

        ``None`` and the own branch resolve to self; the empty string
        refuses -- it is what an unset variable expands to in a fleet
        script's ``--node "$PEER"``, and a target left blank by accident
        must not become a self-note under a clean exit. Any other target
        must be the tree's root or have a ``nodes`` registry row -- a
        deleted node's persisted rows do not make it addressable.

        Args:
            node: Target node branch, or ``None`` for the own node.

        Returns:
            The resolved branch name.

        Raises:
            ValueError: If ``node`` is empty or names a node that is not
                registered.

        """
        branch = self.node.branch
        if node == '':
            raise ValueError(
                'Empty node target: name a node branch, or omit the target for self.'
            )
        if node is None or node == branch:
            return branch
        root = self.node.config.get('root')
        if node != root and not self.db.exists('nodes', where={'node': node}):
            raise ValueError(f'Node not found: {node!r}')
        return node

    def _find_message(self: Radio, message_uuid: str) -> Row:
        """Resolve a message by UUID (case-insensitive) or raise not-found.

        UUIDs resolve globally -- replies included -- so the lookup passes
        ``roots_only=False`` (the default mailbox predicate would make an
        in-place reply unreachable by UUID). Rows come back count-enriched
        from the shared query builder.

        Args:
            message_uuid: 8-char message UUID.

        Returns:
            The message dict with reply/react counts.

        Raises:
            ValueError: If the message is not found.

        """
        message_uuid = message_uuid.upper()
        rows = self._query_messages(message_uuid=message_uuid, roots_only=False)
        if not rows:
            raise self._message_not_found(message_uuid)
        return rows[0]

    def _query_messages(
        self: Radio,
        *,
        message_uuid: Optional[str] = None,
        node: Optional[str] = None,
        sender: Optional[str] = None,
        channel: Optional[str] = None,
        metadata: Optional[str] = None,
        limit: Optional[int] = None,
        since: Optional[str] = None,
        read: Optional[bool] = None,
        recent: bool = False,
        roots_only: bool = True,
    ) -> list[Row]:
        """Build and execute a messages query with counts.

        Args:
            message_uuid: Filter by exact (uppercase) message UUID.
            node: Filter by host node (whose channel-space).
            sender: Filter by sending node.
            channel: Filter by channel.
            metadata: Filter by exact metadata (the relay-lineage mark).
            limit: Maximum rows.
            since: Only after this timestamp.
            read: Read filter, following this node's receipts.
            recent: Sort by ``created_at DESC`` instead of
                ``priority DESC``.
            roots_only: Keep thread roots (and cross-space replies) only.

        Returns:
            List of message dicts with counts.

        """
        # build WHERE clauses -- mailbox views keep thread roots, plus any reply
        # routed across channel-spaces (a rerouted conversation turn is new
        # mail for its host; in-place replies stay behind the parent's count)
        conditions = []
        params = []
        if roots_only:
            conditions.append(
                '(m.parent_message_id IS NULL OR NOT EXISTS'
                ' (SELECT 1 FROM messages p'
                ' WHERE p.message_id = m.parent_message_id'
                ' AND p.node = m.node AND p.channel = m.channel))'
            )
        if message_uuid is not None:
            conditions.append('m.message_uuid = ?')
            params.append(message_uuid)
        if node is not None:
            conditions.append('m.node = ?')
            params.append(node)
        if sender is not None:
            conditions.append('m.sender = ?')
            params.append(sender)
        if channel is not None:
            conditions.append('m.channel = ?')
            params.append(channel)
        if metadata is not None:
            conditions.append('m.metadata = ?')
            params.append(metadata)
        if since is not None:
            conditions.append('m.created_at > ?')
            params.append(since)
        if read is not None:
            # read state follows the caller's receipt in reads
            exists = (
                'EXISTS (SELECT 1 FROM reads r'
                ' WHERE r.message_id = m.message_id AND r.node = ?)'
            )
            conditions.append(exists if read else f'NOT {exists}')
            params.append(self.node.branch)
        where_clause = ' AND '.join(conditions)
        # build query
        query = (
            'SELECT m.*,'
            '  (SELECT COUNT(*) FROM messages r'
            '   WHERE r.parent_message_id = m.message_id) AS replies,'
            '  (SELECT COUNT(*) FROM reacts r'
            '   WHERE r.message_id = m.message_id AND r.value = 1) AS pos_reacts,'
            '  (SELECT COUNT(*) FROM reacts r'
            '   WHERE r.message_id = m.message_id AND r.value = -1) AS neg_reacts'
            f' FROM messages m'
            f' WHERE {where_clause}'
        )
        if recent:
            order = 'm.created_at DESC'
        else:
            order = 'm.priority DESC, m.created_at ASC'
        query += f' ORDER BY {order}'
        if limit is not None:
            query += ' LIMIT ?'
            params.append(limit)
        # execute query
        return self.db.read(query=query, params=tuple(params))

    def _feed_messages(
        self: Radio,
        mailbox: str,
        *,
        node: Optional[str] = None,
        channel: Optional[str] = None,
        since: Optional[str] = None,
        read: Optional[bool] = None,
        recent: bool = False,
    ) -> list[Row]:
        """Fan out one query per readable subscription and merge the rows.

        The one feed implementation -- ``feed`` and ``read(feed=True)``
        both call it. Reads the mailbox's subscriptions, re-checks each
        target channel is still readable, and re-sorts the merged rows.

        Args:
            mailbox: Node branch whose subscriptions fan out.
            node: Filter subscriptions by target node.
            channel: Filter subscriptions by channel.
            since: Only messages after this ISO 8601 timestamp.
            read: Read filter, following this node's receipts.
            recent: Sort by ``created_at DESC`` instead of
                ``priority DESC``.

        Returns:
            Merged and sorted list of message dicts.

        """
        # read subscriptions
        where = {'node': mailbox}
        if node is not None:
            where['target'] = node
        if channel is not None:
            where['channel'] = channel
        subs = self.db.read('subs', where=where)
        # fan-out across subscriptions
        messages = []
        for sub in subs:
            # check channel is readable on the target
            rows = self.db.read(
                'channels',
                where={'node': sub['target'], 'channel': sub['channel']},
                limit=1,
            )
            if not rows or rows[0]['read_only']:
                continue
            # query messages
            messages += self._query_messages(
                node=sub['target'],
                channel=sub['channel'],
                since=since,
                read=read,
                recent=recent,
            )
        # re-sort merged results
        if recent:
            messages.sort(key=lambda m: m['created_at'], reverse=True)
        else:
            messages.sort(key=lambda m: (-m['priority'], m['created_at']))
        return messages

    @staticmethod
    def _thread_lineage(message_id: int) -> tuple[str, tuple[int, ...]]:
        """Build the recursive thread CTE rooted at ``message_id``.

        ``thread`` carries the root message's row and every descendant
        reply chained to it via ``parent_message_id`` -- callers prepend
        the prefix to their query.

        Returns:
            The ``WITH RECURSIVE`` prefix and its bound parameters.

        """
        cte = (
            'WITH RECURSIVE thread AS ('
            ' SELECT * FROM messages WHERE message_id = ?'
            ' UNION ALL'
            ' SELECT m.* FROM messages m'
            ' JOIN thread ON m.parent_message_id = thread.message_id'
            ')'
        )
        return cte, (message_id,)

    def _collect_thread(self: Radio, root: Row) -> list[Row]:
        """Flatten a thread tree into a depth-annotated list.

        Fetches the whole tree in one recursive query, then assembles the
        nested order in memory: parent before children, siblings by the
        explicit ``(created_at, message_id)`` tie-break -- pinned-clock
        writes produce equal timestamps, so sibling order must not lean
        on fetch order.

        Args:
            root: Root message dict.

        Returns:
            Flat list of message dicts with ``depth`` field.

        """
        # fetch the root and every descendant in one recursive query
        cte, params = self._thread_lineage(root['message_id'])
        rows = self.db.read(query=f'{cte} SELECT * FROM thread', params=params)
        # group replies under their parents, sibling-ordered by the tie-break
        children: dict[int, list[Row]] = {}
        for row in rows:
            if row['message_id'] == root['message_id']:
                continue
            children.setdefault(row['parent_message_id'], []).append(row)
        for replies in children.values():
            replies.sort(key=lambda m: (m['created_at'], m['message_id']))
        # flatten parent-before-children, stamping depth
        result = []
        stack = [(root, 0)]
        while stack:
            message, depth = stack.pop()
            message['depth'] = depth
            result.append(message)
            # push in reverse so the earliest sibling pops (and lists) first
            for child in reversed(children.get(message['message_id'], [])):
                stack.append((child, depth + 1))
        return result

    def _collect_message_ids(
        self: Radio,
        message_id: int,
        result: list[int],
    ) -> None:
        """Collect a message ID and all descendant reply IDs.

        One recursive query per call -- ``unsend`` calls it twice, once to
        walk and once to re-check for a racing reply.

        Args:
            message_id: Root message ID.
            result: Accumulator list (mutated in place).

        """
        cte, params = self._thread_lineage(message_id)
        rows = self.db.read(query=f'{cte} SELECT message_id FROM thread', params=params)
        result += [row['message_id'] for row in rows]

    def _receipt(self: Radio, table: str, data: Row, message: Row) -> None:
        """Write a ``(message_id, node)`` receipt, guarded against id reuse.

        Writes only while the id still maps to the message's uuid.
        ``messages.message_id`` is ``INTEGER PRIMARY KEY`` without
        ``AUTOINCREMENT``, so a concurrent ``unsend`` + ``send`` can re-mint
        the id onto a new message between the caller's fetch and this write.
        The recheck-and-merge run in one ``BEGIN IMMEDIATE`` transaction, so a
        stale receipt never attaches to an unrelated message.
        """
        with self.db.transaction() as connection:
            current = self.db.read(
                'messages',
                where={'message_id': message['message_id']},
                limit=1,
                connection=connection,
            )
            if current and current[0]['message_uuid'] == message['message_uuid']:
                self.db.merge(
                    data=data,
                    table=table,
                    conflict=['message_id', 'node'],
                    connection=connection,
                )

    def _mark_read(self: Radio, message: Row) -> None:
        """Merge this reader's receipt for one message.

        Keyed ``(message_id, node)`` and idempotent -- receipts attribute
        to the actual reader, never the mailbox viewed.

        Args:
            message: Message dict to receipt.

        """
        receipt = {
            'message_id': message['message_id'],
            'node': self.node.branch,
            'channel': message['channel'],
        }
        self._receipt('reads', receipt, message)

    @staticmethod
    def _validate_priority(priority: int) -> None:
        """Reject a priority outside the shared bounds.

        Args:
            priority: Message priority to validate.

        Raises:
            ValueError: If ``priority`` is outside
                ``PRIORITY_MIN``-``PRIORITY_MAX``.

        """
        if not PRIORITY_MIN <= priority <= PRIORITY_MAX:
            raise ValueError(
                f'Priority must be {PRIORITY_MIN}-{PRIORITY_MAX}, got {priority}.'
            )

    def _readable(self: Radio, row: Row) -> bool:
        """Return whether the caller may read ``row`` (not a foreign read-only channel).

        The per-row counterpart to :meth:`_reject_read_only`: a row is
        readable when the caller owns its host node or its channel is not
        read-only. Used to filter a thread for a non-participant bystander.

        Args:
            row: A message row (carrying ``node`` and ``channel``).

        Returns:
            Whether the row is readable by the calling node.

        """
        if row['node'] == self.node.branch:
            return True
        rows = self.db.read(
            'channels',
            where={'node': row['node'], 'channel': row['channel']},
            limit=1,
        )
        return not (rows and rows[0]['read_only'])

    def _reject_read_only(self: Radio, channel: str, host: str) -> None:
        """Reject a non-owner acting on a read-only channel (owner only).

        Shared by ``read``/``thread``/``react``/``save`` so a cross-node caller
        cannot reach a read-only channel's contents or receipts.

        Args:
            channel: Channel name to check.
            host: The channel's owning node (the message's ``node`` column).

        Raises:
            PermissionError: If ``host`` is a different node and the channel is
                read-only.

        Note:
            A missing channel row is allowed, not rejected: under the central
            database every channel is co-located, so an absent row means the
            channel does not exist -- there is nothing to gate, and the caller's
            own operation surfaces the not-found error.

        """
        if host != self.node.branch:
            rows = self.db.read(
                'channels',
                where={'node': host, 'channel': channel},
                limit=1,
            )
            if rows and rows[0]['read_only']:
                raise PermissionError(f'Channel {channel!r} is read-only (owner only).')

    @staticmethod
    def _message_not_found(message_uuid: str) -> ValueError:
        """Build the by-UUID not-found error."""
        return ValueError(f'Message not found: {message_uuid!r}')

    @staticmethod
    def _channel_not_found(channel: str) -> ValueError:
        """Build the by-name channel not-found error."""
        return ValueError(f'Channel not found: {channel!r}')

    def _sender_session(self: Radio) -> Optional[str]:
        """Return the session a send stamps: the acting step's, else the node's.

        The session ties a message to the conversation that wrote it (the
        cockpit's "chat about this" forks it in the sender's own context,
        so the stamp stays sender-owned). The acting step's recorded
        session is the literal author -- the loop exports ``STEP_ID`` for
        exactly this attribution, and a detached step never writes the
        session map (resume continuity) -- so it outranks the woven map;
        the map covers sends outside any step, a step row a self-minting
        backend has not stamped yet, and a foreign node's step (an
        operator acting as this node via ``--path`` from inside another
        loop's step). ``None`` when neither names a session.
        """
        step_id = os.environ.get('STEP_ID')
        # isdecimal plus the length gate keep a corrupted STEP_ID from
        # aborting the send (isdigit admits digits int() rejects, e.g.
        # superscripts; int() itself raises past the interpreter's digit
        # limit) -- garbage degrades to the woven-session fallback
        if step_id and step_id.isdecimal() and len(step_id) < 20:
            # the SQLite bind overflows past signed 64-bit
            if int(step_id) < 2**63:
                # node-scoped read: sessions stay sender-owned, so a foreign
                # node's exported step degrades to the fallback too
                rows = self.db.read(
                    'steps',
                    where={'step_id': int(step_id), 'node': self.node.branch},
                    limit=1,
                )
                if rows and rows[0]['session']:
                    return rows[0]['session']
        if agent := self.node._default_agent():
            if session := self.node.sessions.get(agent):
                return session
        return None

    @staticmethod
    def _generate_uuid() -> str:
        """Generate an 8-char uppercase hex UUID."""
        return secrets.token_hex(4).upper()

    def _unique_uuid(self: Radio, max_retries: int = 10) -> str:
        """Generate a message UUID not already present in ``messages``.

        Args:
            max_retries: Maximum generation attempts before giving up.

        Returns:
            An 8-char UUID unique across the tree's ``messages`` table.

        Raises:
            RuntimeError: If no unique UUID is found within ``max_retries``
                attempts.

        """
        for _ in range(max_retries):
            message_uuid = self._generate_uuid()
            if not self.db.exists('messages', where={'message_uuid': message_uuid}):
                return message_uuid
        raise RuntimeError(
            f'Could not generate a unique message UUID after {max_retries} attempts.'
        )
