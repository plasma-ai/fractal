"""Implements ``fractal radio`` sub-app commands."""

from __future__ import annotations

import os
import sys
import time
from typing import Optional

import typer

from fractal.cli.utils import (
    command,
    print_json,
    print_rows,
    require_non_negative,
    require_timestamp,
    resolve_node,
    resolve_sender,
)
from fractal.constants import PRIORITY_MAX, PRIORITY_MIN
from fractal.core.node import Node
from fractal.core.radio import Radio

__all__ = [
    'radio_send',
    'radio_post',
    'radio_unsend',
    'radio_save',
    'radio_unsave',
    'radio_messages',
    'radio_sent',
    'radio_relays',
    'radio_feed',
    'radio_read',
    'radio_thread',
    'radio_reply',
    'radio_react',
    'radio_sub',
    'radio_unsub',
    'radio_subs',
]

# empty-result headers must mirror the populated shapes exactly so parsers
# can key on one header per listing; the metadata listings (messages/feed)
# drop the data column unless --json --body widens the projection -- `read`
# is the receipt-writing body surface
_MESSAGE_COLUMNS = [
    'message_id',
    'node',
    'message_uuid',
    'parent_message_id',
    'parent_message_uuid',
    'channel',
    'sender',
    'session',
    'priority',
    'subject',
    'data',
    'metadata',
    'created_at',
    'replies',
    'pos_reacts',
    'neg_reacts',
]

_METADATA_COLUMNS = [
    'message_id',
    'node',
    'message_uuid',
    'parent_message_id',
    'parent_message_uuid',
    'channel',
    'sender',
    'session',
    'priority',
    'subject',
    'metadata',
    'created_at',
    'replies',
    'pos_reacts',
    'neg_reacts',
]

_SAVED_COLUMNS = [
    'archive_id',
    'node',
    'message_id',
    'message_uuid',
    'parent_message_id',
    'parent_message_uuid',
    'channel',
    'sender',
    'session',
    'owner',
    'priority',
    'subject',
    'data',
    'metadata',
    'created_at',
]

_RELAY_COLUMNS = [
    'message_uuid',
    'sender',
    'node',
    'channel',
    'priority',
    'subject',
    'metadata',
    'created_at',
]

_THREAD_COLUMNS = [
    'message_id',
    'node',
    'message_uuid',
    'parent_message_id',
    'parent_message_uuid',
    'channel',
    'sender',
    'session',
    'priority',
    'subject',
    'data',
    'metadata',
    'created_at',
    'depth',
]

_SUB_COLUMNS = [
    'sub_id',
    'node',
    'target',
    'channel',
    'created_at',
]


def radio_send(app: typer.Typer) -> typer.Typer:
    """Register the ``send`` command."""
    # data argument
    data_help = 'Message data.'
    data = typer.Argument(..., help=data_help)
    # node option (repeatable: a fan-out sends one copy per recipient)
    node_help = (
        'Target node branch; repeat for a fan-out, one copy per recipient'
        ' (default: self when --channel is given).'
    )
    node = typer.Option(None, '--node', help=node_help)
    # parent flag
    parent_help = 'Send to parent node (mutex with --node).'
    parent = typer.Option(False, '--parent', help=parent_help)
    # channel option
    channel_help = "Channel name (default: 'inbox')."
    channel = typer.Option(None, '--channel', help=channel_help)
    # subject option
    subject_help = 'Message subject (required).'
    subject = typer.Option(None, '--subject', help=subject_help)
    # priority option
    priority_help = f'Message priority ({PRIORITY_MIN}-{PRIORITY_MAX}; required).'
    priority = typer.Option(None, '--priority', help=priority_help)
    # relay-of option
    relay_of_help = (
        'UUID of the message this send relays onward; the copy is marked'
        " 'relay:<uuid>' so 'radio relays' can verify the obligation."
    )
    relay_of = typer.Option(None, '--relay-of', help=relay_of_help)
    # path option
    path_help = 'Worktree directory (default: the calling node, else the cwd).'
    path = typer.Option(None, '--path', help=path_help)

    @command(app, 'send')
    def _send(
        data: str = data,
        node: Optional[list[str]] = node,
        parent: bool = parent,
        channel: Optional[str] = channel,
        subject: Optional[str] = subject,
        priority: Optional[int] = priority,
        relay_of: Optional[str] = relay_of,
        path: Optional[str] = path,
    ) -> None:
        """Send a message to a node's channel (a target or channel is required)."""
        # aggregate every missing required option into one round-trip;
        # a send names at least one routing dimension -- a fully bare
        # send is post's reporting-out surface
        missing = []
        untargeted = not node and not parent and channel is None
        if untargeted:
            missing.append('a target or channel')
        if subject is None:
            missing.append('--subject')
        if priority is None:
            missing.append('--priority')
        if missing:
            s = 's' if len(missing) != 1 else ''
            options = ', '.join(missing)
            message = f'Missing required option{s}: {options}.'
            if untargeted:
                message += " To report out, use 'fractal radio post'."
            raise typer.BadParameter(message)
        radio = Radio(resolve_sender(path))
        # a named target always defaults to its inbox -- the channel syncs
        # read -- even when the target is this node (an operator standing in
        # a node's worktree names it expecting a directive to land, and a
        # cwd-keyed reroute to 'private' is a silent black hole); a private
        # note stays available explicitly via --channel=private
        channel_defaulted = channel is None
        if channel is None:
            channel = 'inbox'
        target_defaulted = not node and not parent
        # a repeated --node fans out: every recipient validates before any
        # copy lands, and stdout carries one '<uuid> <node>' receipt per
        # recipient -- the per-recipient delivery record a relayed fleet
        # order is verified against
        if node and len(node) > 1:
            if parent:
                raise typer.BadParameter('--parent and --node are mutually exclusive.')

            # print each receipt as its copy lands, never in a closing
            # sweep: a mid-fan-out failure would otherwise discard the
            # record of copies already delivered, and an operator
            # re-sending on that silence double-delivers
            def _receipt(message_uuid: str, target: str, name: str) -> None:
                typer.echo(f'{message_uuid} {target}')
                typer.echo(f"sent to {target}'s {name!r} channel", err=True)

            radio.send_many(
                node,
                channel,
                subject=subject,
                data=data,
                priority=priority,
                relay_of=relay_of,
                receipt=_receipt,
            )
            return
        message_uuid, target, channel = radio.send(
            node=node[0] if node else None,
            channel=channel,
            parent=parent,
            subject=subject,
            data=data,
            priority=priority,
            relay_of=relay_of,
        )
        # any --node send keeps the roster receipt shape, a roster of one
        # included -- a fleet driver building its recipient list must not
        # parse two stdout contracts; bare and --parent sends stay a bare
        # UUID for scripts
        if node:
            typer.echo(f'{message_uuid} {target}')
        else:
            typer.echo(message_uuid)
        # name a defaulted routing dimension on stderr so the caller sees the
        # resolution it left implicit; an untargeted write to a publicly
        # readable channel is a post in disguise, so nudge the quiet verb
        if target_defaulted:
            row = next(r for r in radio.channels() if r['channel'] == channel)
            if row['read_only']:
                typer.echo(
                    f'Node unspecified: sending to your {channel!r} channel.',
                    err=True,
                )
            else:
                typer.echo(
                    f'Node unspecified: posting to your {channel!r} channel'
                    " (consider using 'radio post').",
                    err=True,
                )
        if channel_defaulted:
            whose = 'your' if target == radio.node.branch else f"{target}'s"
            typer.echo(
                f'Channel unspecified: sending to {whose} {channel!r} channel.',
                err=True,
            )
        # echo the resolved routing so a misdelivered send is visible
        # immediately -- on stderr (stdout carries only the receipt) and
        # unconditionally, since the misdelivery victims are agents, not
        # interactive TTY users
        typer.echo(f"sent to {target}'s {channel!r} channel", err=True)

    return app


def radio_post(app: typer.Typer) -> typer.Typer:
    """Register the ``post`` command."""
    # data argument
    data_help = 'Message data.'
    data = typer.Argument(..., help=data_help)
    # node option
    node_help = 'Target node branch (default: self).'
    node = typer.Option(None, '--node', help=node_help)
    # parent flag
    parent_help = 'Post to parent node (mutex with --node).'
    parent = typer.Option(False, '--parent', help=parent_help)
    # channel option
    channel_help = (
        "Publicly readable channel name (default: your own 'outbox', or"
        " 'public' when targeting another node)."
    )
    channel = typer.Option(None, '--channel', help=channel_help)
    # subject option
    subject_help = 'Message subject (required).'
    subject = typer.Option(None, '--subject', help=subject_help)
    # priority option
    priority_help = f'Message priority ({PRIORITY_MIN}-{PRIORITY_MAX}; required).'
    priority = typer.Option(None, '--priority', help=priority_help)
    # path option
    path_help = 'Worktree directory (default: the calling node, else the cwd).'
    path = typer.Option(None, '--path', help=path_help)

    @command(app, 'post')
    def _post(
        data: str = data,
        node: Optional[str] = node,
        parent: bool = parent,
        channel: Optional[str] = channel,
        subject: Optional[str] = subject,
        priority: Optional[int] = priority,
        path: Optional[str] = path,
    ) -> None:
        """Post a message to a node's publicly readable channel."""
        # aggregate every missing required option into one round-trip
        missing = []
        if subject is None:
            missing.append('--subject')
        if priority is None:
            missing.append('--priority')
        if missing:
            s = 's' if len(missing) != 1 else ''
            options = ', '.join(missing)
            raise typer.BadParameter(f'Missing required option{s}: {options}.')
        radio = Radio(resolve_sender(path))
        # the channel default keys on the target: a bare/self post reports
        # out to the own outbox; posting at another node lands on its public
        # board (its outbox is owner-only write)
        if channel is None:
            if parent or (node and node != radio.node.branch):
                channel = 'public'
            else:
                channel = 'outbox'
        message_uuid, target, channel = radio.send(
            node=node,
            channel=channel,
            parent=parent,
            subject=subject,
            data=data,
            priority=priority,
            post=True,
        )
        typer.echo(message_uuid)
        # echo the resolved routing so a misdelivered post is visible
        # immediately -- on stderr (stdout stays the bare UUID for scripts)
        # and unconditionally, since the misdelivery victims are agents, not
        # interactive TTY users
        typer.echo(f"sent to {target}'s {channel!r} channel", err=True)

    return app


def radio_unsend(app: typer.Typer) -> typer.Typer:
    """Register the ``unsend`` command."""
    # message uuid argument
    message_uuid_help = '8-char message UUID.'
    message_uuid = typer.Argument(..., help=message_uuid_help)
    # force flag
    force_help = 'Delete the whole thread even if the message has replies.'
    force = typer.Option(False, '--force', '-f', help=force_help)
    # path option
    path_help = 'Worktree directory (default: the calling node, else the cwd).'
    path = typer.Option(None, '--path', help=path_help)

    @command(app, 'unsend')
    def _unsend(
        message_uuid: str = message_uuid,
        force: bool = force,
        path: Optional[str] = path,
    ) -> None:
        """Delete a sent message (refused if it has replies; use --force)."""
        radio = Radio(resolve_sender(path))
        radio.unsend(message_uuid, force=force)
        typer.echo(f'Unsent {message_uuid}.')

    return app


def radio_save(app: typer.Typer) -> typer.Typer:
    """Register the ``save`` command."""
    # message uuid argument
    message_uuid_help = '8-char message UUID.'
    message_uuid = typer.Argument(..., help=message_uuid_help)
    # path option
    path_help = 'Worktree directory (default: the calling node, else the cwd).'
    path = typer.Option(None, '--path', help=path_help)

    @command(app, 'save')
    def _save(
        message_uuid: str = message_uuid,
        path: Optional[str] = path,
    ) -> None:
        """Save a message to the archive."""
        radio = Radio(resolve_sender(path))
        radio.save(message_uuid)
        typer.echo(f'Saved {message_uuid}.')

    return app


def radio_unsave(app: typer.Typer) -> typer.Typer:
    """Register the ``unsave`` command."""
    # message uuid argument
    message_uuid_help = '8-char message UUID.'
    message_uuid = typer.Argument(..., help=message_uuid_help)
    # path option
    path_help = 'Worktree directory (default: the calling node, else the cwd).'
    path = typer.Option(None, '--path', help=path_help)

    @command(app, 'unsave')
    def _unsave(
        message_uuid: str = message_uuid,
        path: Optional[str] = path,
    ) -> None:
        """Remove a message from the archive."""
        radio = Radio(resolve_sender(path))
        radio.unsave(message_uuid)
        typer.echo(f'Unsaved {message_uuid}.')

    return app


def radio_messages(app: typer.Typer) -> typer.Typer:
    """Register the ``messages`` command."""
    # channel option
    channel_help = "Filter by channel name (default: 'inbox')."
    channel = typer.Option(None, '--channel', help=channel_help)
    # limit option
    limit_help = 'Maximum rows to return.'
    limit = typer.Option(None, '--limit', help=limit_help)
    # since option
    since_help = 'Only messages after this timestamp.'
    since = typer.Option(None, '--since', help=since_help)
    # read flag
    read_help = 'Show only read messages (default: unread only).'
    read = typer.Option(False, '--read', help=read_help)
    # all messages flag
    all_messages_help = 'Show all messages (default: unread only).'
    all_messages = typer.Option(False, '--all', help=all_messages_help)
    # saved flag
    saved_help = 'Show saved messages (mutex with --read/--all).'
    saved = typer.Option(False, '--saved', help=saved_help)
    # recent flag
    recent_help = 'Sort by most recent instead of priority.'
    recent = typer.Option(False, '--recent', help=recent_help)
    # csv flag
    csv_help = 'Force CSV output (already the default when piped / non-TTY).'
    csv = typer.Option(False, '--csv', help=csv_help)
    # json flag
    json_help = 'Output a JSON array of row objects (mutex with --csv).'
    json = typer.Option(False, '--json', help=json_help)
    # body flag
    body_help = 'Include the data column (requires --json).'
    body = typer.Option(False, '--body', help=body_help)
    # path option
    path_help = 'Worktree directory (default: the calling node, else the cwd).'
    path = typer.Option(None, '--path', help=path_help)

    @command(app, 'messages')
    def _messages(
        channel: Optional[str] = channel,
        limit: Optional[int] = limit,
        since: Optional[str] = since,
        read: bool = read,
        all_messages: bool = all_messages,
        saved: bool = saved,
        recent: bool = recent,
        csv: bool = csv,
        json: bool = json,
        body: bool = body,
        path: Optional[str] = path,
    ) -> None:
        """List a channel's metadata, inbox by default (bodies via 'read')."""
        require_non_negative(limit=limit)
        require_timestamp(since=since)
        if json and csv:
            raise typer.BadParameter('--json is mutually exclusive with --csv.')
        if body and not json:
            raise typer.BadParameter('--body requires --json.')
        if saved and (read or all_messages):
            raise typer.BadParameter('--saved is mutually exclusive with --read/--all.')
        # resolve the acting node env-first (read-your-writes: the node a
        # send attributed to is the node whose mailbox this lists); an
        # explicit --path still selects another mailbox
        radio = Radio(resolve_sender(path))
        # the watermark's cut is read before the query, never at render
        instant = time.time()
        # --saved: show archived messages
        if saved:
            rows = radio.saved(
                channel=channel,
                limit=limit,
                since=since,
                recent=recent,
            )
            if json:
                print_json(rows, columns=_SAVED_COLUMNS)
            else:
                print_rows(rows, csv=csv, columns=_SAVED_COLUMNS)
            _stamp_listing(radio, instant)
            return
        # a bound seal empties the listing by design -- name it once, with
        # the lawful remedy, and skip the misleading unread-total notice
        # below (whose fresh reads are held to zero too)
        sealed = radio.seal_binds()
        if sealed:
            typer.echo(
                'inbox sealed: hosted messages are held out of view until'
                ' lawfully unsealed',
                err=True,
            )
        read_filter = _read_filter(all_messages, read)
        # a bare `messages` (no --channel) shows only the inbox -- not all your
        # own channels -- so own outbox/private notes don't read as incoming mail;
        # hint interactive callers that the other channels exist (TTY only --
        # agents read bare `messages` every sync, and a per-call notice is noise)
        if channel is None:
            channel = 'inbox'
            if sys.stderr.isatty():
                typer.echo(
                    'no channel specified, defaulting to inbox '
                    '(use --channel=<channel> for your other channels)',
                    err=True,
                )
        rows = radio.messages(
            channel=channel,
            limit=limit,
            since=since,
            read=read_filter,
            recent=recent,
        )
        # an empty default (unread) view is ambiguous -- no mail, or all
        # read? -- so name the uncapped total on stderr, TTY or not (the
        # victims are agents, not TTY users); stdout keeps the empty-header
        # contract; --limit 0 empties any view, so it earns no notice
        if not rows and read_filter is False and limit != 0 and not sealed:
            all_rows = radio.messages(
                channel=channel,
                limit=None,
                since=since,
                read=None,
                recent=recent,
            )
            total = len(all_rows)
            typer.echo(
                f'0 unread ({total} total; --all shows everything)',
                err=True,
            )
        # metadata-only listing unless --json --body widens it -- `read` is
        # the receipt-writing body surface
        columns = _MESSAGE_COLUMNS if body else _METADATA_COLUMNS
        rows = [{key: row[key] for key in columns} for row in rows]
        if json:
            print_json(rows, columns=columns)
        else:
            print_rows(rows, csv=csv, columns=columns)
        _stamp_listing(radio, instant)

    return app


def radio_sent(app: typer.Typer) -> typer.Typer:
    """Register the ``sent`` command."""
    # channel option
    channel_help = 'Filter by the recipient channel name.'
    channel = typer.Option(None, '--channel', help=channel_help)
    # limit option
    limit_help = 'Maximum rows to return.'
    limit = typer.Option(None, '--limit', help=limit_help)
    # since option
    since_help = 'Only messages after this timestamp.'
    since = typer.Option(None, '--since', help=since_help)
    # recent flag
    recent_help = 'Sort by most recent instead of priority.'
    recent = typer.Option(False, '--recent', help=recent_help)
    # csv flag
    csv_help = 'Force CSV output (already the default when piped / non-TTY).'
    csv = typer.Option(False, '--csv', help=csv_help)
    # json flag
    json_help = 'Output a JSON array of row objects (mutex with --csv).'
    json = typer.Option(False, '--json', help=json_help)
    # path option
    path_help = 'Worktree directory (default: the calling node, else the cwd).'
    path = typer.Option(None, '--path', help=path_help)

    @command(app, 'sent')
    def _sent(
        channel: Optional[str] = channel,
        limit: Optional[int] = limit,
        since: Optional[str] = since,
        recent: bool = recent,
        csv: bool = csv,
        json: bool = json,
        path: Optional[str] = path,
    ) -> None:
        """List messages this node sent (the node column is the recipient)."""
        require_non_negative(limit=limit)
        require_timestamp(since=since)
        if json and csv:
            raise typer.BadParameter('--json is mutually exclusive with --csv.')
        radio = Radio(resolve_sender(path))
        # the watermark's cut is read before the query, never at render
        instant = time.time()
        rows = radio.sent(
            channel=channel,
            limit=limit,
            since=since,
            recent=recent,
        )
        if json:
            print_json(rows, columns=_MESSAGE_COLUMNS)
        else:
            print_rows(rows, csv=csv, columns=_MESSAGE_COLUMNS)
        _stamp_listing(radio, instant)

    return app


def radio_relays(app: typer.Typer) -> typer.Typer:
    """Register the ``relays`` command."""
    # message uuid argument
    message_uuid_help = '8-char UUID of the relayed (original) message.'
    message_uuid = typer.Argument(..., help=message_uuid_help)
    # csv flag
    csv_help = 'Force CSV output (already the default when piped / non-TTY).'
    csv = typer.Option(False, '--csv', help=csv_help)
    # json flag
    json_help = 'Output a JSON array of row objects (mutex with --csv).'
    json = typer.Option(False, '--json', help=json_help)
    # path option
    path_help = 'Worktree directory (default: the calling node, else the cwd).'
    path = typer.Option(None, '--path', help=path_help)

    @command(app, 'relays')
    def _relays(
        message_uuid: str = message_uuid,
        csv: bool = csv,
        json: bool = json,
        path: Optional[str] = path,
    ) -> None:
        """List the recorded relayed copies of a message (relay lineage).

        The descendant-relay obligation check: every copy sent with
        ``--relay-of <uuid>`` lists here, sender and recipient named, so an
        empty listing means no relay of the order was ever recorded.
        """
        if json and csv:
            raise typer.BadParameter('--json is mutually exclusive with --csv.')
        radio = Radio(resolve_sender(path))
        # the watermark's cut is read before the query, never at render
        instant = time.time()
        rows = radio.relays(message_uuid)
        # an empty lineage is the check's loudest answer -- name it (stdout
        # keeps the empty-header contract for parsers)
        if not rows:
            typer.echo(f'0 relays recorded for {message_uuid}', err=True)
        rows = [{key: row[key] for key in _RELAY_COLUMNS} for row in rows]
        if json:
            print_json(rows, columns=_RELAY_COLUMNS)
        else:
            print_rows(rows, csv=csv, columns=_RELAY_COLUMNS)
        _stamp_listing(radio, instant)

    return app


def radio_feed(app: typer.Typer) -> typer.Typer:
    """Register the ``feed`` command."""
    # node option
    node_help = 'Filter by node branch.'
    node = typer.Option(None, '--node', help=node_help)
    # channel option
    channel_help = 'Filter by channel name.'
    channel = typer.Option(None, '--channel', help=channel_help)
    # limit option
    limit_help = 'Maximum rows to return.'
    limit = typer.Option(None, '--limit', help=limit_help)
    # since option
    since_help = 'Only messages after this timestamp.'
    since = typer.Option(None, '--since', help=since_help)
    # read flag
    read_help = 'Show only read messages (default: unread only).'
    read = typer.Option(False, '--read', help=read_help)
    # all messages flag
    all_messages_help = 'Show all messages (default: unread only).'
    all_messages = typer.Option(False, '--all', help=all_messages_help)
    # saved flag
    saved_help = 'Show saved messages (mutex with --read/--all).'
    saved = typer.Option(False, '--saved', help=saved_help)
    # recent flag
    recent_help = 'Sort by most recent instead of priority.'
    recent = typer.Option(False, '--recent', help=recent_help)
    # csv flag
    csv_help = 'Force CSV output (already the default when piped / non-TTY).'
    csv = typer.Option(False, '--csv', help=csv_help)
    # json flag
    json_help = 'Output a JSON array of row objects (mutex with --csv).'
    json = typer.Option(False, '--json', help=json_help)
    # body flag
    body_help = 'Include the data column (requires --json).'
    body = typer.Option(False, '--body', help=body_help)
    # path option
    path_help = 'Worktree directory (default: the calling node, else the cwd).'
    path = typer.Option(None, '--path', help=path_help)

    @command(app, 'feed')
    def _feed(
        node: Optional[str] = node,
        channel: Optional[str] = channel,
        limit: Optional[int] = limit,
        since: Optional[str] = since,
        read: bool = read,
        all_messages: bool = all_messages,
        saved: bool = saved,
        recent: bool = recent,
        csv: bool = csv,
        json: bool = json,
        body: bool = body,
        path: Optional[str] = path,
    ) -> None:
        """List subscribed nodes' metadata (bodies via 'read --feed')."""
        require_non_negative(limit=limit)
        require_timestamp(since=since)
        if json and csv:
            raise typer.BadParameter('--json is mutually exclusive with --csv.')
        if body and not json:
            raise typer.BadParameter('--body requires --json.')
        if saved and (read or all_messages):
            raise typer.BadParameter('--saved is mutually exclusive with --read/--all.')
        # resolve node
        radio = Radio(resolve_sender(path))
        # the watermark's cut is read before the query, never at render
        instant = time.time()
        # --saved: show archived messages
        if saved:
            rows = radio.saved(
                node=node,
                channel=channel,
                limit=limit,
                since=since,
                recent=recent,
            )
            if json:
                print_json(rows, columns=_SAVED_COLUMNS)
            else:
                print_rows(rows, csv=csv, columns=_SAVED_COLUMNS)
            _stamp_listing(radio, instant)
            return
        read_filter = _read_filter(all_messages, read)
        rows = radio.feed(
            node=node,
            channel=channel,
            limit=limit,
            since=since,
            read=read_filter,
            recent=recent,
        )
        # an empty default (unread) view is ambiguous -- no mail, or all
        # read? -- so name the uncapped total on stderr, TTY or not (the
        # victims are agents, not TTY users); stdout keeps the empty-header
        # contract; --limit 0 empties any view, so it earns no notice
        if not rows and read_filter is False and limit != 0:
            all_rows = radio.feed(
                node=node,
                channel=channel,
                limit=None,
                since=since,
                read=None,
                recent=recent,
            )
            total = len(all_rows)
            typer.echo(
                f'0 unread ({total} total; --all shows everything)',
                err=True,
            )
        # metadata-only listing unless --json --body widens it -- `read` is
        # the receipt-writing body surface
        columns = _MESSAGE_COLUMNS if body else _METADATA_COLUMNS
        rows = [{key: row[key] for key in columns} for row in rows]
        if json:
            print_json(rows, columns=columns)
        else:
            print_rows(rows, csv=csv, columns=columns)
        _stamp_listing(radio, instant)

    return app


def radio_read(app: typer.Typer) -> typer.Typer:
    """Register the ``read`` command."""
    # message uuids argument
    message_uuids_help = '8-char message UUIDs.'
    message_uuids = typer.Argument(None, help=message_uuids_help)
    # channel option
    channel_help = "Read this channel of the viewed mailbox's channel-space."
    channel = typer.Option(None, '--channel', help=channel_help)
    # feed flag
    feed_help = "Read messages from the viewed mailbox's subscribed nodes."
    feed = typer.Option(False, '--feed', help=feed_help)
    # unread flag
    unread_help = 'Only messages you have not read (requires --channel/--feed).'
    unread = typer.Option(False, '--unread', help=unread_help)
    # path option
    path_help = 'Worktree directory of the mailbox to view (defaults to your own).'
    path = typer.Option(None, '--path', help=path_help)

    @command(app, 'read')
    def _read(
        message_uuids: Optional[list[str]] = message_uuids,
        channel: Optional[str] = channel,
        feed: bool = feed,
        unread: bool = unread,
        path: Optional[str] = path,
    ) -> None:
        """Print full messages by UUID/selector, marking them read (as you)."""
        # validate the selector shape
        if not message_uuids and channel is None and not feed:
            raise typer.BadParameter('Pass message UUIDs, --channel, or --feed.')
        if unread and channel is None and not feed:
            raise typer.BadParameter('--unread requires --channel or --feed.')
        # the reader is who runs the command; --path only selects whose mailbox
        # is viewed, so receipts stay truthful
        reader = _resolve_reader()
        if path is None:
            mailbox = reader
        else:
            mailbox = resolve_node(path)
            # refuse a mailbox from another tree loudly: the reader's radio
            # resolves branch names against its own central DB, where a foreign
            # branch either collides with a same-named node (silently reading
            # -- and receipting -- the wrong mailbox) or resolves to nothing
            if mailbox.db.path != reader.db.path:
                raise typer.BadParameter(
                    '--path names a mailbox in a different fractal tree;'
                    ' read it as a node of that tree (run from one of its'
                    ' worktrees or export _NODE).'
                )
        radio = Radio(reader)
        messages = radio.read(
            *(message_uuids or []),
            node=mailbox.branch,
            channel=channel,
            feed=feed,
            unread=unread,
        )
        for index, message in enumerate(messages):
            if index:
                typer.echo('')
            uuid = message['message_uuid']
            sender = message['sender']
            node = message['node']
            timestamp = message['created_at']
            message_channel = message['channel']
            subject = message['subject']
            priority = message['priority']
            data = message['data']
            typer.echo(f'Message UUID: {uuid}')
            typer.echo(f'From: {sender}')
            typer.echo(f'Node: {node}')
            typer.echo(f'Timestamp: {timestamp}')
            typer.echo(f'Channel: {message_channel}')
            typer.echo(f'Subject: {subject}')
            typer.echo(f'Priority: {priority}')
            typer.echo('')
            typer.echo(data)

    return app


def radio_thread(app: typer.Typer) -> typer.Typer:
    """Register the ``thread`` command."""
    # message uuid argument
    message_uuid_help = '8-char message UUID.'
    message_uuid = typer.Argument(..., help=message_uuid_help)
    # csv flag
    csv_help = 'Force CSV output (already the default when piped / non-TTY).'
    csv = typer.Option(False, '--csv', help=csv_help)
    # json flag
    json_help = 'Output a JSON array of row objects (mutex with --csv).'
    json = typer.Option(False, '--json', help=json_help)
    # path option
    path_help = 'Worktree directory (default: the calling node, else the cwd).'
    path = typer.Option(None, '--path', help=path_help)

    @command(app, 'thread')
    def _thread(
        message_uuid: str = message_uuid,
        csv: bool = csv,
        json: bool = json,
        path: Optional[str] = path,
    ) -> None:
        """Show a message's full reply tree (root and all replies)."""
        if json and csv:
            raise typer.BadParameter('--json is mutually exclusive with --csv.')
        radio = Radio(resolve_sender(path))
        # the watermark's cut is read before the query, never at render
        instant = time.time()
        rows = radio.thread(message_uuid)
        if json:
            print_json(rows, columns=_THREAD_COLUMNS)
        elif csv or not sys.stdout.isatty():
            print_rows(rows, csv=csv, columns=_THREAD_COLUMNS)
        else:
            for message in rows:
                indent = '  ' * message.get('depth', 0)
                uuid = message['message_uuid']
                sender = message['sender']
                timestamp = message['created_at']
                priority = message['priority']
                subject = message['subject']
                typer.echo(
                    f'{indent}[{uuid}] {sender}'
                    f' ({timestamp}, priority {priority}): {subject}'
                )
        _stamp_listing(radio, instant)

    return app


def radio_reply(app: typer.Typer) -> typer.Typer:
    """Register the ``reply`` command."""
    # message uuid argument
    message_uuid_help = '8-char message UUID.'
    message_uuid = typer.Argument(..., help=message_uuid_help)
    # data argument
    data_help = 'Message data.'
    data = typer.Argument(..., help=data_help)
    # priority option
    priority_help = f'Message priority ({PRIORITY_MIN}-{PRIORITY_MAX}).'
    priority = typer.Option(None, '--priority', help=priority_help)
    # path option
    path_help = 'Worktree directory (default: the calling node, else the cwd).'
    path = typer.Option(None, '--path', help=path_help)

    @command(app, 'reply')
    def _reply(
        message_uuid: str = message_uuid,
        data: str = data,
        priority: Optional[int] = priority,
        path: Optional[str] = path,
    ) -> None:
        """Reply to a message."""
        radio = Radio(resolve_sender(path))
        reply_uuid, target, channel = radio.reply(
            message_uuid=message_uuid,
            data=data,
            priority=priority,
        )
        typer.echo(reply_uuid)
        # echo the resolved routing on stderr, mirroring send -- routing
        # is least obvious exactly on replies, where the destination is
        # derived, not named
        typer.echo(f"sent to {target}'s {channel!r} channel", err=True)

    return app


def radio_react(app: typer.Typer) -> typer.Typer:
    """Register the ``react`` command."""
    # message uuid argument
    message_uuid_help = '8-char message UUID.'
    message_uuid = typer.Argument(..., help=message_uuid_help)
    # value argument
    value_help = 'Reaction value (+ or -).'
    value = typer.Argument(..., help=value_help)
    # path option
    path_help = 'Worktree directory (default: the calling node, else the cwd).'
    path = typer.Option(None, '--path', help=path_help)

    @command(app, 'react')
    def _react(
        message_uuid: str = message_uuid,
        value: str = value,
        path: Optional[str] = path,
    ) -> None:
        """React to a message (+ or -)."""
        # convert +/- to 1/-1
        if value == '+':
            int_value = 1
        elif value == '-':
            int_value = -1
        else:
            raise typer.BadParameter("value must be '+' or '-'.")
        # resolve node
        radio = Radio(resolve_sender(path))
        radio.react(message_uuid, int_value)
        typer.echo(f'Reacted {value} to {message_uuid}.')

    return app


def radio_sub(app: typer.Typer) -> typer.Typer:
    """Register the ``sub`` command."""
    # node option
    node_help = 'Target node branch.'
    node = typer.Option(..., '--node', help=node_help)
    # channel option
    channel_help = 'Filter by channel name.'
    channel = typer.Option(None, '--channel', help=channel_help)
    # path option
    path_help = 'Worktree directory (default: the calling node, else the cwd).'
    path = typer.Option(None, '--path', help=path_help)

    @command(app, 'sub')
    def _sub(
        node: str = node,
        channel: Optional[str] = channel,
        path: Optional[str] = path,
    ) -> None:
        """Subscribe to a node's channel."""
        radio = Radio(resolve_sender(path))
        radio.subscribe(node, channel=channel)
        typer.echo(f'Subscribed to {node}.')

    return app


def radio_unsub(app: typer.Typer) -> typer.Typer:
    """Register the ``unsub`` command."""
    # node option
    node_help = 'Target node branch.'
    node = typer.Option(..., '--node', help=node_help)
    # channel option
    channel_help = 'Filter by channel name.'
    channel = typer.Option(None, '--channel', help=channel_help)
    # path option
    path_help = 'Worktree directory (default: the calling node, else the cwd).'
    path = typer.Option(None, '--path', help=path_help)

    @command(app, 'unsub')
    def _unsub(
        node: str = node,
        channel: Optional[str] = channel,
        path: Optional[str] = path,
    ) -> None:
        """Unsubscribe from a node's channel."""
        radio = Radio(resolve_sender(path))
        # report the true rowcount -- a zero-match unsub prints 0 and still
        # exits 0, so a mis-pathed target is visible without failing scripts
        removed = radio.unsubscribe(node, channel=channel)
        s = 's' if removed != 1 else ''
        typer.echo(f'Removed {removed} subscription{s}.')

    return app


def radio_subs(app: typer.Typer) -> typer.Typer:
    """Register the ``subs`` command."""
    # csv flag
    csv_help = 'Force CSV output (already the default when piped / non-TTY).'
    csv = typer.Option(False, '--csv', help=csv_help)
    # json flag
    json_help = 'Output a JSON array of row objects (mutex with --csv).'
    json = typer.Option(False, '--json', help=json_help)
    # path option
    path_help = 'Worktree directory (default: the calling node, else the cwd).'
    path = typer.Option(None, '--path', help=path_help)

    @command(app, 'subs')
    def _subs(
        csv: bool = csv,
        json: bool = json,
        path: Optional[str] = path,
    ) -> None:
        """List all subscriptions."""
        if json and csv:
            raise typer.BadParameter('--json is mutually exclusive with --csv.')
        radio = Radio(resolve_sender(path))
        # the watermark's cut is read before the query, never at render
        instant = time.time()
        rows = radio.subs()
        if json:
            print_json(rows, columns=_SUB_COLUMNS)
        else:
            print_rows(rows, csv=csv, columns=_SUB_COLUMNS)
        _stamp_listing(radio, instant)

    return app


# ------ helper functions


def _stamp_listing(radio: Radio, instant: float) -> None:
    """Print a listing's freshness watermark on stderr (the recorded cut).

    A listing is a point-in-time cut of the store: instruments that grade
    from one need the cut's instant and acting identity on the record, or a
    stale/mis-attributed read silently becomes a false-record verdict.
    ``instant`` is the caller's clock read from before its query: a stamp
    taken at render time would claim a cut the query never saw, endorsing
    a row a concurrent sender landed mid-render as never sent -- the exact
    verdict the watermark exists to prevent -- while the pre-query cut only
    ever under-claims. Unconditional and on stderr -- stdout keeps the row
    contract.
    """
    stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(instant))
    typer.echo(f'as of {stamp} (acting as {radio.node.branch})', err=True)


def _resolve_reader() -> Node:
    """Resolve the acting reader: the calling node, else the cwd's node.

    The loop exports ``_NODE`` for the node whose loop is running, so a
    production read attributes to that node wherever it runs from; ``--path``
    never selects the reader -- it only picks the mailbox being viewed.

    Returns:
        Node the read receipts attribute to.

    Raises:
        typer.BadParameter: If no reader resolves (no ``_NODE`` and no
            node at the cwd), or the exported ``_NODE`` does not name an
            initialized node (a stale or mistyped export).

    """
    if caller := Node.resolve_caller():
        # mirror resolve_sender's initialized-node check: a stale _NODE
        # refuses cleanly, not as an internal error
        if not caller.exists():
            raise typer.BadParameter(
                f'No fractal node at {caller.worktree} (resolved from the'
                ' exported _NODE). Unset _NODE (--path only selects the'
                ' mailbox viewed, never the reader).'
            )
        return caller
    # a set-but-unresolvable _NODE (a reaped worktree, a typo) refuses too:
    # silently attributing the receipts to the cwd's node would be worse
    if node_dir := os.environ.get('_NODE'):
        raise typer.BadParameter(
            f'No fractal node at {node_dir} (the exported _NODE names no'
            ' git worktree). Unset _NODE (--path only selects the mailbox'
            ' viewed, never the reader).'
        )
    # the missing piece here is the reader, not the mailbox, so replace
    # resolve_node's generic advice (`fractal init` the cwd) with the remedy
    try:
        return resolve_node('.')
    except typer.BadParameter:
        raise typer.BadParameter(
            'No reader node: run from a node worktree or export _NODE'
            ' (--path only selects the mailbox viewed, never the reader).'
        ) from None


def _read_filter(all_messages: bool, read_messages: bool) -> Optional[bool]:
    """Resolve the read-state filter from the ``--all``/``--read`` flags.

    Args:
        all_messages: Whether ``--all`` was passed.
        read_messages: Whether ``--read`` was passed.

    Returns:
        ``None`` for everything, ``True`` for read only, ``False`` for unread
        only (the default).

    """
    # default: unread only; --all: everything; --read: read only
    if all_messages:
        return None
    elif read_messages:
        return True
    else:
        return False
