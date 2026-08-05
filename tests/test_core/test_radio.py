"""Test the ``fractal.core.radio`` module.

Cross-node behavior runs against two real node identities sharing the one
central database (the ``radio_pair`` fixture) -- no resolver mocks.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from fractal.core.node import Node
from fractal.core.radio import Radio

__all__ = [
    'test_init_seeds_default_channels',
    'test_init_subscribes_within_subtree_only',
    'test_init_blind_seeds_channels_without_subs',
    'test_send_stamps_sender_session',
    'test_send_to_self',
    'test_send_self_targeted_node_behaves_as_bare',
    'test_send_routes_to_target',
    'test_send_parent_lands_in_the_dotted_parents_inbox',
    'test_send_unknown_node',
    'test_send_nonexistent_channel',
    'test_send_rejects_invalid_priority',
    'test_post_enforces_publicly_readable_class',
    'test_sent_lists_outbound_mail',
    'test_sealed_inbox_holds_hosted_mail_from_its_own_seat',
    'test_send_many_delivers_per_recipient_with_receipts',
    'test_relay_lineage_marks_copies_and_lists_them',
    'test_sent_includes_own_replies',
    'test_messages_order_by_priority_then_created_at',
    'test_messages_channel_filter',
    'test_messages_read_filter',
    'test_acting_marks_message_read',
    'test_read_returns_counts',
    'test_read_multiple_uuids_dedupes_without_partial_receipts',
    'test_read_channel_selector_reads_and_receipts',
    'test_read_feed_selector_catches_up_subscriptions',
    'test_read_receipts_attribute_to_reader_not_mailbox',
    'test_read_channel_selector_respects_read_only',
    'test_feed_fans_out_with_sources',
    'test_reply_inherits_subject_and_priority',
    'test_reply_lands_in_parents_channel_space',
    'test_reply_to_inbox_message_reaches_the_sender',
    'test_thread_returns_flat_list_with_depth',
    'test_uuid_resolves_globally',
    'test_cross_node_read_respects_read_only',
    'test_thread_hides_read_only_rows_from_bystanders',
    'test_reply_refuses_a_read_only_message_for_a_bystander',
    'test_child_add_skips_subscription_for_a_blind_parent',
    'test_write_only_channel_accepts_owner_and_rejects_others',
    'test_react_and_re_react',
    'test_react_rejects_invalid_values',
    'test_custom_channel_registers_with_flags',
    'test_custom_channel_rejects_reserved',
    'test_custom_channel_rejects_duplicate',
    'test_channel_delete_scoped_to_owner',
    'test_channel_delete_refuses_messages_without_force',
    'test_subscribe_and_unsubscribe',
    'test_subscribe_validates_target',
    'test_archive_round_trip_per_saver',
    'test_archive_survives_unsend',
    'test_unsave_not_found_stays_terse',
    'test_not_found_by_uuid',
    'test_unsend_deletes_message',
    'test_read_receipt_skips_a_reused_message_id',
    'test_unsend_deletes_replies_and_reacts',
    'test_unsend_refuses_thread_without_force',
    'test_unsend_rejects_callers_other_than_the_sender',
    'test_unsend_force_aborts_on_concurrent_reply',
]


def test_init_seeds_default_channels(radio: Radio) -> None:
    """Init seeds public, private, inbox, outbox channels."""
    channels = radio.channels()
    names = {channel['channel'] for channel in channels}
    assert names == {'public', 'private', 'inbox', 'outbox'}
    # verify permission bits
    by_name = {channel['channel']: channel for channel in channels}
    assert by_name['public']['read_only'] == 0
    assert by_name['public']['write_only'] == 0
    assert by_name['private']['read_only'] == 1
    assert by_name['private']['write_only'] == 1
    assert by_name['inbox']['read_only'] == 1
    assert by_name['inbox']['write_only'] == 0
    assert by_name['outbox']['read_only'] == 0
    assert by_name['outbox']['write_only'] == 1


def test_init_subscribes_within_subtree_only(radio_pair: tuple[Radio, Radio]) -> None:
    """Init auto-subscribes to the parent and direct children -- never siblings.

    The central registry holds the whole tree, so the child-subscribe pass
    must match on the subtree prefix: a same-depth node in a sibling subtree
    (which depth-counting alone would catch) is not a child.
    """
    root, peer = radio_pair
    peer_branch = peer.node.branch
    root_branch = root.node.branch
    # the fixture mirrors production: peer subscribed to its parent at init,
    # root subscribed to the peer (child_add)
    assert {s['target'] for s in peer.subs()} == {root_branch}
    assert {s['target'] for s in root.subs()} == {peer_branch}
    # a same-depth node in a *sibling* subtree, registered and seeded
    foreign = f'{peer_branch}x.kid'
    root.db.merge({'node': foreign, 'status': 'idle'}, 'nodes')
    root.db.merge(
        data={'node': foreign, 'channel': 'public', 'read_only': 0, 'write_only': 0},
        table='channels',
    )
    # re-init is idempotent and must not subscribe across subtrees
    peer.init()
    assert {s['target'] for s in peer.subs()} == {root_branch}


def test_init_blind_seeds_channels_without_subs(
    radio_pair: tuple[Radio, Radio],
) -> None:
    """A blind node's init seeds channels only; unsubscribe reports the count.

    With ``blind`` in the config, ``init`` skips the parent and child
    subscription passes -- the node reads nothing -- while the parent's own
    watch of it is untouched. ``unsubscribe`` returns the number of rows it
    removed: the true count on a match, 0 when nothing is left.
    """
    root, peer = radio_pair
    peer_branch = peer.node.branch
    root_branch = root.node.branch
    # flip the peer blind and drop the parent subs its init seeded (one per
    # readable channel on the root: public and outbox)
    peer.node.config.set('blind', True)
    assert peer.unsubscribe(root_branch) == 2
    # a blind init seeds the default channels but never subscribes
    peer.init()
    assert {c['channel'] for c in peer.channels()} == {
        'public',
        'private',
        'inbox',
        'outbox',
    }
    assert peer.subs() == []
    # the parent's own watch of the blind peer is untouched
    assert {s['target'] for s in root.subs()} == {peer_branch}
    # nothing left to remove reports the honest zero
    assert peer.unsubscribe(root_branch) == 0


def test_send_stamps_sender_session(
    radio_pair: tuple[Radio, Radio],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sends and replies stamp the conversation that wrote them.

    The acting step's recorded session (the loop-exported ``STEP_ID``) is
    the literal author, so it outranks the woven session map; the map covers
    sends outside any step and step rows not yet stamped. Sessions stay
    sender-owned: a foreign node's exported step degrades to the woven
    fallback the same way. A node with no agent, no woven session, and no
    acting step stamps NULL, and a corrupted STEP_ID degrades to the
    fallback instead of failing the send.
    """
    radio, peer = radio_pair
    bare, _, _ = radio.send(channel='public', subject='s', data='d', priority=5)
    [row] = radio.node.db.read('messages', where={'message_uuid': bare})
    assert row['session'] is None
    # a live session (and an agent command with flags) stamps its base session
    radio.node.config.set('agent', 'claude --verbose')
    radio.node.sessions.set('claude', 'sess-1')
    stamped, _, _ = radio.send(channel='public', subject='s2', data='d2', priority=5)
    [row] = radio.node.db.read('messages', where={'message_uuid': stamped})
    assert row['session'] == 'sess-1'
    reply_uuid, _, _ = radio.reply(stamped, 'on it')
    [row] = radio.node.db.read('messages', where={'message_uuid': reply_uuid})
    assert row['session'] == 'sess-1'
    # a detached step never writes the session map: with the map cleared, a
    # send inside a step (STEP_ID exported by the loop) stamps the step's
    # recorded session instead of NULL
    radio.node.sessions.clear()
    record = radio.node.record
    run_id = record.run_start()
    iter_id = record.iter_start(run_id=run_id, iter=1)
    step_id = record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=3,
        step_name='REVIEW',
    )
    record.step_session(
        agent='claude',
        step_id=step_id,
        model='claude-fable-5',
        session='sess-2',
    )
    monkeypatch.setenv('STEP_ID', f'{step_id}')
    detached, _, _ = radio.send(channel='public', subject='s3', data='d3', priority=5)
    [row] = radio.node.db.read('messages', where={'message_uuid': detached})
    assert row['session'] == 'sess-2'
    # a corrupted or row-less STEP_ID degrades to the woven fallback (NULL
    # here -- the map is cleared) -- the send itself never fails
    for garbage in ('\N{SUPERSCRIPT TWO}', f'{2**63}', '9' * 5000, '424242'):
        monkeypatch.setenv('STEP_ID', garbage)
        bad, _, _ = radio.send(channel='public', subject='s4', data='d4', priority=5)
        [row] = radio.node.db.read('messages', where={'message_uuid': bad})
        assert row['session'] is None
    # the acting step outranks a rewoven session map: with both present,
    # the step's session stamps
    radio.node.sessions.set('claude', 'sess-1')
    monkeypatch.setenv('STEP_ID', f'{step_id}')
    both, _, _ = radio.send(channel='public', subject='s5', data='d5', priority=5)
    [row] = radio.node.db.read('messages', where={'message_uuid': both})
    assert row['session'] == 'sess-2'
    # a step row a self-minting backend has not stamped yet falls back to
    # the woven session
    blank = record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=4,
        step_name='COMMIT',
    )
    monkeypatch.setenv('STEP_ID', f'{blank}')
    fallback, _, _ = radio.send(channel='public', subject='s6', data='d6', priority=5)
    [row] = radio.node.db.read('messages', where={'message_uuid': fallback})
    assert row['session'] == 'sess-1'
    # sessions stay sender-owned: the peer's exported step -- session and
    # all -- never stamps this sender's row, degrading to the woven fallback
    foreign_run = peer.node.record.run_start()
    foreign_iter = peer.node.record.iter_start(run_id=foreign_run, iter=1)
    foreign = peer.node.record.step_start(
        iter_id=foreign_iter,
        run_id=foreign_run,
        step=3,
        step_name='REVIEW',
    )
    peer.node.record.step_session(
        agent='claude',
        step_id=foreign,
        model='claude-fable-5',
        session='sess-3',
    )
    monkeypatch.setenv('STEP_ID', f'{foreign}')
    crossed, _, _ = radio.send(channel='public', subject='s7', data='d7', priority=5)
    [row] = radio.node.db.read('messages', where={'message_uuid': crossed})
    assert row['session'] == 'sess-1'


def test_send_to_self(radio: Radio) -> None:
    """A bare send lands in the own inbox; a channel send in that channel."""
    msg_uuid, _, _ = radio.send(
        subject='test subject',
        data='test data',
        priority=5,
    )
    assert isinstance(msg_uuid, str)
    assert len(msg_uuid) == 8
    rows = radio.messages(channel='inbox')
    assert len(rows) == 1
    assert rows[0]['node'] == radio.node.branch
    assert rows[0]['subject'] == 'test subject'
    assert rows[0]['data'] == 'test data'
    assert rows[0]['priority'] == 5
    assert rows[0]['channel'] == 'inbox'
    # the own write-only outbox is reachable for the owner
    radio.send(channel='outbox', subject='broadcast', data='d', priority=8)
    outbox = radio.messages(channel='outbox')
    assert len(outbox) == 1
    assert outbox[0]['priority'] == 8


def test_send_self_targeted_node_behaves_as_bare(radio: Radio) -> None:
    """A self-targeted ``node`` normalizes to the bare (own-inbox) send."""
    uuid, _, _ = radio.send(
        radio.node.branch,
        subject='s',
        data='d',
        priority=0,
    )
    [row] = radio.db.read('messages', where={'message_uuid': uuid})
    assert row['node'] == radio.node.branch


def test_send_routes_to_target(radio_pair: tuple[Radio, Radio]) -> None:
    """A targeted send lands in the recipient's channel-space, not the sender's."""
    root, peer = radio_pair
    peer_branch = peer.node.branch
    uuid, _, _ = root.send(peer_branch, subject='task', data='do it', priority=7)
    # the row's node column names the host; the sender keeps no mailbox copy
    [row] = root.db.read('messages', where={'message_uuid': uuid})
    assert row['node'] == peer_branch
    assert row['sender'] == root.node.branch
    assert [m['message_uuid'] for m in peer.messages(channel='inbox')] == [uuid]
    assert root.messages(channel='inbox') == []


def test_send_parent_lands_in_the_dotted_parents_inbox(
    radio_pair: tuple[Radio, Radio],
) -> None:
    """``parent=True`` resolves the dotted parent and lands in its inbox."""
    root, peer = radio_pair
    uuid, _, _ = peer.send(parent=True, subject='done', data='summary', priority=5)
    [row] = peer.db.read('messages', where={'message_uuid': uuid})
    assert row['node'] == root.node.branch
    assert row['channel'] == 'inbox'


def test_send_unknown_node(radio: Radio) -> None:
    """Sending to an unregistered node is rejected."""
    with pytest.raises(ValueError, match='Node not found'):
        radio.send('main.ghost', subject='s', data='d', priority=0)


def test_send_nonexistent_channel(radio: Radio) -> None:
    """Sending to a nonexistent channel raises ValueError."""
    with pytest.raises(ValueError, match='specify a target node or create it'):
        radio.send(
            channel='nonexistent',
            subject='s',
            data='d',
            priority=0,
        )


def test_send_rejects_invalid_priority(radio: Radio) -> None:
    """Send rejects priority outside 0-10."""
    with pytest.raises(ValueError, match='must be 0-10'):
        radio.send(subject='s', data='d', priority=11)
    with pytest.raises(ValueError, match='must be 0-10'):
        radio.send(subject='s', data='d', priority=-1)
    # boundary values are accepted
    radio.send(subject='s', data='d', priority=0)
    radio.send(subject='s', data='d', priority=10)


def test_post_enforces_publicly_readable_class(radio: Radio) -> None:
    """``post=True`` gates a write to publicly readable channels only.

    ``post`` (``post=True``) writes publicly readable channels and a
    privately readable one names the sibling verb -- custom channels slot
    into the same classes by their flags, not their names; ``send`` and
    internal callers pass nothing and route anywhere write permissions
    allow.
    """
    # the public class passes for post
    radio.send(channel='public', subject='s', data='d', priority=5, post=True)
    # a privately readable channel names the sibling verb
    with pytest.raises(ValueError, match='fractal radio send'):
        radio.send(channel='private', subject='s', data='d', priority=5, post=True)
    # a custom channel's flags place it in a class -- read-only refuses too
    radio.channel('drafts', read_only=True)
    with pytest.raises(ValueError, match='fractal radio send'):
        radio.send(channel='drafts', subject='s', data='d', priority=5, post=True)
    # unset skips the class check (send and internal callers route freely)
    radio.send(channel='outbox', subject='s', data='d', priority=5)
    radio.send(channel='inbox', subject='s', data='d', priority=5)


def test_sent_lists_outbound_mail(radio_pair: tuple[Radio, Radio]) -> None:
    """``sent`` lists own-authored messages with the recipient in ``node``."""
    root, peer = radio_pair
    peer_branch = peer.node.branch
    to_peer, _, _ = root.send(peer_branch, subject='for you', data='d', priority=6)
    to_self, _, _ = root.send(channel='private', subject='note', data='d', priority=2)
    peer.send(parent=True, subject='not yours', data='d', priority=5)
    # both outbound messages list, recipient-attributed, priority first
    rows = root.sent()
    assert [r['message_uuid'] for r in rows] == [to_peer, to_self]
    assert [r['node'] for r in rows] == [peer_branch, root.node.branch]
    # the channel filter narrows to one host's channel
    inbox_only = root.sent(channel='inbox')
    assert [r['message_uuid'] for r in inbox_only] == [to_peer]
    # recent flips to newest-first
    recent = root.sent(recent=True)
    assert [r['message_uuid'] for r in recent] == [to_self, to_peer]


def test_sealed_inbox_holds_hosted_mail_from_its_own_seat(
    radio_pair: tuple[Radio, Radio],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bound seal holds hosted mail out of the sealed seat's context.

    The verifier-isolation hold: with ``sealed`` set, the node's OWN reads
    (the loop-exported ``_NODE`` names the caller) see an empty mailbox and
    ``read`` refuses outright -- adjudication traffic can no longer leak
    into a sealed context through routine triage. The seal binds the seat
    alone: the node's own writes stay visible, an operator shell (no
    ``_NODE``) reads everything, and unsealing restores the view.
    """
    root, peer = radio_pair
    peer_branch = peer.node.branch
    uuid, _, _ = root.send(
        peer_branch,
        subject='adjudication',
        data='sealed reply',
        priority=9,
    )
    peer.node.config.set('sealed', True)
    # the sealed seat's own reads: mailbox held, body surface refused
    monkeypatch.setenv('_NODE', f'{peer.node.worktree}')
    assert peer.messages(channel='inbox') == []
    with pytest.raises(PermissionError, match='inbox sealed'):
        peer.read(uuid)
    # the refusal consumed nothing: no receipt landed for the held message
    assert peer.db.read('reads', where={'node': peer_branch}) == []
    # sending -- the verdict path -- is not sealed, and own writes list
    out, _, _ = peer.send(parent=True, subject='verdict', data='v', priority=5)
    assert out in [m['message_uuid'] for m in peer.sent()]
    # a self-subscription to an own hosted channel cannot tunnel hosted
    # mail into the sealed seat through the feed
    root.send(peer_branch, channel='public', subject='board', data='b', priority=3)
    peer.subscribe(peer_branch, channel='public')
    assert [row for row in peer.feed() if row['node'] == peer_branch] == []
    # nor does the relay-lineage listing surface a copy the seat hosts
    relayed, _, _ = root.send(
        peer_branch,
        subject='relayed order',
        data='r',
        priority=5,
        relay_of=uuid,
    )
    assert peer.relays(uuid) == []
    monkeypatch.delenv('_NODE')
    assert [row['message_uuid'] for row in peer.relays(uuid)] == [relayed]
    monkeypatch.setenv('_NODE', f'{peer.node.worktree}')
    # an operator shell (no _NODE) adjudicates freely
    monkeypatch.delenv('_NODE')
    assert uuid in [m['message_uuid'] for m in peer.messages(channel='inbox')]
    # lawful unsealing restores the seat's own view
    peer.node.config.set('sealed', False)
    monkeypatch.setenv('_NODE', f'{peer.node.worktree}')
    assert uuid in [m['message_uuid'] for m in peer.messages(channel='inbox')]


def test_send_many_delivers_per_recipient_with_receipts(
    radio_pair: tuple[Radio, Radio],
) -> None:
    """A fan-out lands one copy per recipient and returns ordered receipts.

    Each copy is its own message with its own UUID in its recipient's
    channel-space -- the receipts are the per-recipient delivery record a
    fleet order is verified against. A bad recipient refuses the whole
    fan-out before any copy lands, so a partial delivery can never pass
    silently as a full one.
    """
    root, peer = radio_pair
    peer_branch = peer.node.branch
    own_branch = root.node.branch
    receipts = root.send_many(
        [peer_branch, own_branch],
        subject='fleet order',
        data='wind down',
        priority=8,
    )
    assert [(node, channel) for _, node, channel in receipts] == [
        (peer_branch, 'inbox'),
        (own_branch, 'inbox'),
    ]
    to_peer, to_self = (uuid for uuid, _, _ in receipts)
    assert to_peer != to_self
    assert to_peer in [m['message_uuid'] for m in peer.messages(channel='inbox')]
    assert to_self in [m['message_uuid'] for m in root.messages(channel='inbox')]
    # a bad recipient refuses the whole fan-out before any copy lands
    before = len(root.sent())
    with pytest.raises(ValueError, match='Node not found'):
        root.send_many(
            [peer_branch, 'main.ghost'],
            subject='fleet order',
            data='wind down',
            priority=8,
        )
    assert len(root.sent()) == before


def test_relay_lineage_marks_copies_and_lists_them(
    radio_pair: tuple[Radio, Radio],
) -> None:
    """``relay_of`` stamps lineage; ``relays`` answers the obligation check.

    A relayed copy carries ``relay:<uuid>`` metadata, so whether an order
    was ever passed onward is answerable from the store: an empty lineage
    means the obligation never executed. A relay naming an unknown message
    refuses -- a typo'd mark would read as an unmet obligation forever.
    """
    root, peer = radio_pair
    peer_branch = peer.node.branch
    order, _, _ = root.send(
        peer_branch,
        subject='fleet order',
        data='wind down',
        priority=9,
    )
    # before any relay the lineage is empty -- the obligation reads unmet
    assert root.relays(order) == []
    relayed, _, _ = peer.send(
        parent=True,
        subject='fleet order (relayed)',
        data='wind down',
        priority=9,
        relay_of=order,
    )
    [copy] = root.relays(order)
    assert copy['message_uuid'] == relayed
    assert copy['sender'] == peer_branch
    assert copy['metadata'] == f'relay:{order}'
    # an unknown reference refuses instead of recording a dangling mark
    with pytest.raises(ValueError, match='Relay reference not found'):
        peer.send(
            parent=True,
            subject='s',
            data='d',
            priority=5,
            relay_of='ZZZZ9999',
        )


def test_sent_includes_own_replies(radio_pair: tuple[Radio, Radio]) -> None:
    """``sent`` lists authored replies too, wherever they thread.

    The mailbox listings hide a reply that threads in place behind its
    parent's reply count, but ``sent`` is the review surface for what this
    node wrote -- replies must list beside the top-level posts.
    """
    root, peer = radio_pair
    # an in-place reply into the peer's readable channel
    post, _, _ = peer.send(channel='public', subject='post', data='d', priority=3)
    in_place, _, _ = root.reply(post, 'seen')
    # a follow-up threaded onto the own outbox post
    cast, _, _ = root.send(channel='outbox', subject='cast', data='d', priority=3)
    follow_up, _, _ = root.reply(cast, 'follow-up')
    uuids = {r['message_uuid'] for r in root.sent()}
    assert {in_place, cast, follow_up} <= uuids


def test_messages_order_by_priority_then_created_at(radio: Radio) -> None:
    """Messages are ordered by priority DESC, created_at ASC."""
    radio.send(subject='low', data='d', priority=1)
    radio.send(subject='high', data='d', priority=9)
    radio.send(subject='mid', data='d', priority=5)
    rows = radio.messages()
    priorities = [r['priority'] for r in rows]
    assert priorities == [9, 5, 1]


def test_messages_channel_filter(radio: Radio) -> None:
    """Channel filter returns only matching messages."""
    radio.send(subject='inbox msg', data='d', priority=0)
    radio.send(channel='outbox', subject='outbox msg', data='d', priority=0)
    inbox = radio.messages(channel='inbox')
    assert len(inbox) == 1
    assert inbox[0]['subject'] == 'inbox msg'
    outbox = radio.messages(channel='outbox')
    assert len(outbox) == 1
    assert outbox[0]['subject'] == 'outbox msg'


def test_messages_read_filter(radio: Radio) -> None:
    """Read filter: None=all, False=unread, True=read (receipt-backed)."""
    radio.send(subject='s', data='d', priority=0)
    # all messages (unread)
    assert len(radio.messages(read=None)) == 1
    assert len(radio.messages(read=False)) == 1
    assert len(radio.messages(read=True)) == 0
    # mark as read -- the owner's receipt lands in reads like everyone else's
    uuid = radio.messages()[0]['message_uuid']
    radio.read(uuid)
    assert len(radio.messages(read=None)) == 1
    assert len(radio.messages(read=False)) == 0
    assert len(radio.messages(read=True)) == 1


@pytest.mark.parametrize('act', ['read', 'react', 'reply'])
def test_acting_marks_message_read(radio: Radio, act: str) -> None:
    """Reading, reacting, and replying all clear a message from the unread set.

    Every acknowledgment lands the actor's receipt in ``reads`` -- otherwise
    SYNC resurfaces the same items every pass, burning tokens.
    """
    radio.send(subject='s', data='d', priority=0)
    uuid = radio.messages()[0]['message_uuid']
    assert len(radio.messages(read=False)) == 1
    if act == 'read':
        radio.read(uuid)
    elif act == 'react':
        radio.react(uuid, 1)
    else:
        radio.reply(uuid, 'ack')
    assert len(radio.messages(read=False)) == 0
    assert len(radio.messages(read=True)) == 1


def test_read_returns_counts(radio: Radio) -> None:
    """Read returns the full message with reply/react counts and a receipt."""
    radio.send(subject='s', data='d', priority=0)
    uuid = radio.messages()[0]['message_uuid']
    radio.reply(uuid, 'r1')
    radio.react(uuid, 1)
    [message] = radio.read(uuid)
    assert message['subject'] == 's'
    assert message['data'] == 'd'
    assert message['replies'] == 1
    assert message['pos_reacts'] == 1
    assert message['neg_reacts'] == 0
    # the reader's receipt is on record
    receipt = {'message_id': message['message_id'], 'node': radio.node.branch}
    assert radio.db.exists('reads', where=receipt)


def test_read_multiple_uuids_dedupes_without_partial_receipts(
    radio: Radio,
) -> None:
    """Read returns named UUIDs once, in order; a failed lookup receipts nothing.

    Duplicate UUIDs collapse to their first occurrence, and receipts land
    only after every lookup resolves -- a not-found UUID anywhere in the
    call leaves even its resolvable companions unread.
    """
    first, _, _ = radio.send(subject='one', data='d', priority=0)
    second, _, _ = radio.send(subject='two', data='d', priority=0)
    messages = radio.read(first, second, first)
    assert [m['message_uuid'] for m in messages] == [first, second]
    assert len(radio.messages(read=True)) == 2
    # a not-found UUID aborts the whole read before any receipt
    third, _, _ = radio.send(subject='three', data='d', priority=0)
    with pytest.raises(ValueError, match='not found'):
        radio.read(third, 'DEADBEEF')
    assert len(radio.messages(read=False)) == 1


def test_read_channel_selector_reads_and_receipts(radio: Radio) -> None:
    """``read(channel=...)`` returns the channel's rows and receipts exactly them.

    The ``unread`` narrowing follows this reader's receipts, so a second
    unread read returns nothing new; rows outside the selection stay unread.
    """
    kept, _, _ = radio.send(subject='kept', data='d', priority=2)
    swept, _, _ = radio.send(subject='swept', data='d', priority=5)
    radio.send(channel='private', subject='note', data='d', priority=5)
    # the selector reads the whole channel, priority first
    messages = radio.read(channel='inbox', unread=True)
    assert [m['message_uuid'] for m in messages] == [swept, kept]
    # receipts landed for exactly the returned rows: nothing unread remains
    assert radio.read(channel='inbox', unread=True) == []
    assert len(radio.messages(channel='private', read=False)) == 1
    # an explicit UUID returns regardless of its read state
    [again] = radio.read(swept)
    assert again['subject'] == 'swept'
    # without unread the selector re-reads the full channel
    assert len(radio.read(channel='private')) == 1
    assert radio.messages(channel='private', read=False) == []


def test_read_feed_selector_catches_up_subscriptions(
    radio_pair: tuple[Radio, Radio],
) -> None:
    """``read(feed=True, unread=True)`` drains the unread feed, once.

    The fan-out mirrors ``feed`` (readable subscribed channels, priority
    order); receipts land for the returned rows, for this reader only.
    """
    root, peer = radio_pair
    cast, _, _ = peer.send(channel='outbox', subject='cast', data='d', priority=5)
    post, _, _ = peer.send(channel='public', subject='post', data='d', priority=3)
    messages = root.read(feed=True, unread=True)
    assert [m['message_uuid'] for m in messages] == [cast, post]
    # the catch-up is complete: nothing unread remains for this reader
    assert root.read(feed=True, unread=True) == []
    # the peer's own unread state never moved
    assert len(peer.messages(channel='outbox', read=False)) == 1


def test_read_receipts_attribute_to_reader_not_mailbox(
    radio_pair: tuple[Radio, Radio],
) -> None:
    """Selector reads of another mailbox receipt as the reader, never the owner.

    ``node`` picks whose channel-space is viewed; the receipt names the
    reading node, so an operator peek can never consume the owner's
    unread state.
    """
    root, peer = radio_pair
    peer_branch = peer.node.branch
    cast, _, _ = peer.send(channel='outbox', subject='cast', data='d', priority=5)
    [message] = root.read(node=peer_branch, channel='outbox')
    assert message['message_uuid'] == cast
    # the receipt names the root, and only the root
    [receipt] = root.db.read('reads', where={'message_id': message['message_id']})
    assert receipt['node'] == root.node.branch
    # the owner's unread state never moved
    assert len(peer.messages(channel='outbox', read=False)) == 1
    # the feed selector honors node too: the peer views the ROOT's feed
    # (the root's subscriptions carry the peer's cast, not the root's own post)
    note, _, _ = root.send(channel='outbox', subject='note', data='d', priority=5)
    viewed = peer.read(node=root.node.branch, feed=True)
    read_uuids = {m['message_uuid'] for m in viewed}
    assert cast in read_uuids
    assert note not in read_uuids


def test_read_channel_selector_respects_read_only(
    radio_pair: tuple[Radio, Radio],
) -> None:
    """A read-only channel selector is owner-only, like the by-UUID rule."""
    root, peer = radio_pair
    root.send(peer.node.branch, subject='secret', data='d', priority=0)
    with pytest.raises(PermissionError, match='read-only'):
        root.read(node=peer.node.branch, channel='inbox')
    # the owner reads its own read-only channel freely
    [message] = peer.read(channel='inbox')
    assert message['subject'] == 'secret'


def test_feed_fans_out_with_sources(radio_pair: tuple[Radio, Radio]) -> None:
    """Feed merges subscribed channels, attributing each row to its source.

    Listing is always passive: the unread view is stable across
    calls -- and under ``limit`` -- until the reader consumes rows through
    ``read``, and even then only for that reader.
    """
    root, peer = radio_pair
    peer_branch = peer.node.branch
    cast, _, _ = peer.send(channel='outbox', subject='cast', data='d', priority=5)
    post, _, _ = peer.send(channel='public', subject='post', data='d', priority=3)
    rows = root.feed(read=False)
    assert {r['message_uuid'] for r in rows} == {cast, post}
    assert {r['node'] for r in rows} == {peer_branch}
    # listing is passive: the unread view is unchanged, capped or not
    assert len(root.feed(limit=1, read=False)) == 1
    assert {r['message_uuid'] for r in root.feed(read=False)} == {cast, post}
    # reading is the consuming act, and only for this reader
    root.read(cast, post)
    assert root.feed(read=False) == []
    assert len(peer.messages(channel='outbox', read=False)) == 1


def test_reply_inherits_subject_and_priority(radio: Radio) -> None:
    """Reply inherits 'Re: subject' and priority from parent."""
    radio.send(subject='original', data='d', priority=4)
    uuid = radio.messages()[0]['message_uuid']
    reply_uuid, node, channel = radio.reply(uuid, 'reply data')
    # the return carries the resolved destination beside the UUID
    assert isinstance(reply_uuid, str)
    assert len(reply_uuid) == 8
    assert (node, channel) == (radio.node.branch, 'inbox')
    [reply] = radio.db.read('messages', where={'message_uuid': reply_uuid})
    assert reply['subject'] == 'Re: original'
    assert reply['priority'] == 4
    assert reply['data'] == 'reply data'
    assert reply['parent_message_uuid'] == uuid


def test_reply_lands_in_parents_channel_space(
    radio_pair: tuple[Radio, Radio],
) -> None:
    """A reply to another node's readable channel stays in that channel-space.

    Replying into another node's write-only channel never pierces it -- the
    reply reroutes to the owner's inbox as a conversation turn.
    """
    root, peer = radio_pair
    peer_branch = peer.node.branch
    post, _, _ = peer.send(channel='public', subject='post', data='d', priority=3)
    reply_uuid, node, channel = root.reply(post, 'seen')
    [reply] = root.db.read('messages', where={'message_uuid': reply_uuid})
    assert reply['node'] == peer_branch
    assert reply['sender'] == root.node.branch
    assert (node, channel) == (peer_branch, 'public')
    # a non-owner's reply into a write-only channel reaches the owner's
    # inbox instead of the channel itself
    cast, _, _ = peer.send(channel='outbox', subject='cast', data='d', priority=3)
    cast_uuid, node, channel = root.reply(cast, 'ack')
    [rerouted] = root.db.read('messages', where={'message_uuid': cast_uuid})
    assert (rerouted['node'], rerouted['channel']) == (peer_branch, 'inbox')
    assert (node, channel) == (peer_branch, 'inbox')


def test_reply_to_inbox_message_reaches_the_sender(
    radio_pair: tuple[Radio, Radio],
) -> None:
    """A reply to a message in my inbox lands in the original sender's inbox.

    Conversation semantics: answering a message someone sent me must reach
    the counterparty, threaded, not self-thread into my own inbox where the
    sender never sees it.
    """
    root, peer = radio_pair
    root_branch = root.node.branch
    peer_branch = peer.node.branch
    # peer asks in root's inbox; root's answer must land with the peer
    ask, _, _ = peer.send(node=root_branch, subject='ask', data='d', priority=3)
    answer, _, _ = root.reply(ask, 'answer')
    [reply] = root.db.read('messages', where={'message_uuid': answer})
    assert reply['node'] == peer_branch
    assert reply['channel'] == 'inbox'
    assert reply['sender'] == root_branch
    assert reply['subject'] == 'Re: ask'
    assert reply['parent_message_uuid'] == ask
    # a reply to my own outbox message stays self-threaded
    cast, _, _ = root.send(channel='outbox', subject='cast', data='d', priority=3)
    note, _, _ = root.reply(cast, 'follow-up')
    [reply] = root.db.read('messages', where={'message_uuid': note})
    assert reply['node'] == root_branch
    assert reply['channel'] == 'outbox'


def test_thread_returns_flat_list_with_depth(radio: Radio) -> None:
    """Thread returns flat list with depth for indentation."""
    radio.send(subject='root', data='d', priority=0)
    root_uuid = radio.messages()[0]['message_uuid']
    # reply to root, then reply to the reply
    reply_uuid, _, _ = radio.reply(root_uuid, 'reply 1')
    radio.reply(reply_uuid, 'reply 2')
    # the thread resolves from any message in it (walks up to the root)
    thread = radio.thread(reply_uuid)
    assert len(thread) == 3
    assert thread[0]['depth'] == 0
    assert thread[0]['subject'] == 'root'
    assert thread[1]['depth'] == 1
    assert thread[2]['depth'] == 2


def test_uuid_resolves_globally(radio_pair: tuple[Radio, Radio]) -> None:
    """A UUID resolves from any node without naming its host.

    UUIDs are unique across the whole tree (one ``messages`` table), so a
    feed-discovered message is readable directly.
    """
    root, peer = radio_pair
    cast, _, _ = peer.send(channel='outbox', subject='cast', data='body', priority=5)
    [message] = root.read(cast)
    assert message['node'] == peer.node.branch
    assert message['data'] == 'body'


@pytest.mark.parametrize('method', ['read', 'thread', 'react', 'save'])
def test_cross_node_read_respects_read_only(
    radio_pair: tuple[Radio, Radio],
    method: str,
) -> None:
    """Cross-node by-UUID access enforces the read-only "owner only" rule.

    ``feed`` and ``subscribe`` already bar a non-owner from a read-only
    channel (``inbox``/``private``), so reaching a message there by UUID must
    be denied too -- otherwise the restriction leaks (a UUID holder could ack
    or archive a message they are not allowed to read). Readable channels
    (``outbox``) stay reachable. ``thread`` exempts conversation
    participants, so its denial needs a true bystander.
    """
    root, peer = radio_pair
    # a read-only message in the peer's inbox and a readable outbox cast
    private_uuid, _, _ = root.send(
        peer.node.branch,
        subject='secret',
        data='d',
        priority=0,
    )
    public_uuid, _, _ = peer.send(
        channel='outbox',
        subject='cast',
        data='d',
        priority=0,
    )
    extra = (1,) if method == 'react' else ()
    call = getattr(root, method)
    if method == 'thread':
        # root sent the inbox message, so it may thread that conversation;
        # a peer-only private note keeps root a non-participant
        secret, _, _ = peer.send(
            channel='private',
            subject='note',
            data='d',
            priority=0,
        )
        with pytest.raises(PermissionError, match='read-only'):
            call(secret)
    else:
        with pytest.raises(PermissionError, match='read-only'):
            call(private_uuid, *extra)
    # the readable channel is still reachable cross-node
    call(public_uuid, *extra)
    # the peer reads its own inbox freely (owner only, not owner never)
    [note] = peer.read(private_uuid)
    assert note['subject'] == 'secret'


def test_thread_hides_read_only_rows_from_bystanders(
    radio_pair: tuple[Radio, Radio],
) -> None:
    """A bystander threads a public root but never its read-only inbox rows.

    A reply to a write-only broadcast (an outbox notice) reroutes into the
    author's read-only inbox, and the counter-reply into the replier's
    inbox -- all chained under the publicly readable outbox root. Any node
    may subscribe to that outbox and see the root's UUID, but threading it
    must not hand back the rerouted inbox bodies (which a direct ``read``
    refuses). Participants still read the whole conversation; only a true
    bystander is filtered.
    """
    parent, author = radio_pair
    # a third node, a peer of the author, is a pure bystander
    bystander = _register_peer(parent, 'bystander')

    # author broadcasts on its outbox; parent replies (reroutes to the
    # author's inbox); author replies back (reroutes to the parent's inbox)
    root_uuid, _, _ = author.send(
        channel='outbox',
        subject='run exited',
        data='public notice',
        priority=5,
    )
    reply_uuid, _, _ = parent.reply(root_uuid, 'private feedback: creds are hunter2')
    author.reply(reply_uuid, 'private ack to parent')

    # the bystander can discover the public root but a direct read of the
    # rerouted inbox row is refused
    bystander.subscribe(author.node.branch)
    with pytest.raises(PermissionError, match='read-only'):
        bystander.read(reply_uuid)
    # threading the public root returns only readable rows -- no inbox bodies
    rows = bystander.thread(root_uuid)
    bodies = [row['data'] for row in rows]
    assert 'public notice' in bodies
    assert all('private' not in (row['data'] or '') for row in rows), bodies
    # every row handed to the bystander is readable from ITS perspective --
    # perspective matters: _is_read_only is False for the viewer's own rows,
    # so checking against the parent would pass vacuously for parent-owned rows
    assert all(not _is_read_only(row, bystander) for row in rows)
    # a participant (the parent) still reads the whole rerouted conversation
    parent_rows = parent.thread(root_uuid)
    assert len(parent_rows) == 3


def test_reply_refuses_a_read_only_message_for_a_bystander(
    radio_pair: tuple[Radio, Radio],
) -> None:
    """A non-owner cannot reply into a read-only channel it doesn't own.

    ``reply`` is a write verb that threads in place and marks the parent
    read; without the read-only gate a bystander could reply to a message
    in another node's inbox, which not only writes there but makes the
    caller a thread participant -- unlocking the very rerouted-conversation
    read that ``thread``'s bystander filter blocks. The gate mirrors
    ``read``/``react``/``save``: reply into a readable channel still works.
    """
    parent, author = radio_pair
    bystander = _register_peer(parent, 'bystander')
    # author broadcasts (readable outbox); parent replies -> author's inbox
    root_uuid, _, _ = author.send(
        channel='outbox',
        subject='run exited',
        data='notice',
        priority=5,
    )
    reply_uuid, _, _ = parent.reply(root_uuid, 'private: creds are hunter2')
    bystander.subscribe(author.node.branch)
    # replying to the read-only inbox row is refused for the bystander
    with pytest.raises(PermissionError, match='read-only'):
        bystander.reply(reply_uuid, 'sneak in')
    # and thread()'s bystander filter stays intact (no participant foothold gained)
    assert all(
        'private' not in (row['data'] or '') for row in bystander.thread(root_uuid)
    )
    # replying to the readable outbox root still works (the normal flow)
    bystander.reply(root_uuid, 'public follow-up')


def test_child_add_skips_subscription_for_a_blind_parent(
    radio_pair: tuple[Radio, Radio],
) -> None:
    """A blind parent registering a child never subscribes to it.

    ``blind`` means the node holds no subscriptions of its own -- its
    ``radio.init`` seeds none and its launch sweeps any that landed. But
    ``child_add`` auto-subscribes the parent to a new child, so a blind
    parent that spawns a child mid-run would start reading the child's
    feed until its next launch. The subscribe is skipped at the source.
    """
    parent, child = radio_pair
    parent_node = parent.node
    # the fixture already subscribed the parent to the child (child_add);
    # clear it, flip the parent blind, then re-register the child
    parent_node.db.delete('subs', where={'node': parent_node.branch})
    parent_node.config.set('blind', True)
    child_name = child.node.branch.rsplit('.', 1)[-1]
    parent_node.child_add(child_name)
    assert parent.subs() == []


def test_write_only_channel_accepts_owner_and_rejects_others(
    radio_pair: tuple[Radio, Radio],
) -> None:
    """Owner can send to own write_only channel; others are rejected."""
    root, peer = radio_pair
    peer.send(channel='outbox', subject='self publish', data='d', priority=0)
    assert len(peer.messages(channel='outbox')) == 1
    with pytest.raises(PermissionError, match='write-only'):
        root.send(
            peer.node.branch,
            channel='outbox',
            subject='rejected',
            data='d',
            priority=0,
        )


def test_react_and_re_react(radio: Radio) -> None:
    """React writes to reacts table; re-reacting changes value."""
    radio.send(subject='s', data='d', priority=0)
    uuid = radio.messages()[0]['message_uuid']
    radio.react(uuid, 1)
    reacts = radio.db.read('reacts')
    assert len(reacts) == 1
    assert reacts[0]['value'] == 1
    # re-react changes value
    radio.react(uuid, -1)
    reacts = radio.db.read('reacts')
    assert len(reacts) == 1
    assert reacts[0]['value'] == -1


def test_react_rejects_invalid_values(radio: Radio) -> None:
    """React rejects values other than 1 or -1."""
    radio.send(subject='s', data='d', priority=0)
    uuid = radio.messages()[0]['message_uuid']
    with pytest.raises(ValueError, match='must be 1 or -1'):
        radio.react(uuid, 0)


def test_custom_channel_registers_with_flags(radio: Radio) -> None:
    """Custom channels can be registered."""
    radio.channel('alerts', read_only=True, write_only=True)
    channels = radio.channels()
    alerts = next(channel for channel in channels if channel['channel'] == 'alerts')
    assert alerts['read_only'] == 1
    assert alerts['write_only'] == 1


def test_custom_channel_rejects_reserved(radio: Radio) -> None:
    """Custom channels cannot use reserved names."""
    with pytest.raises(ValueError, match='reserved'):
        radio.channel('public')


def test_custom_channel_rejects_duplicate(radio: Radio) -> None:
    """Re-creating a channel raises instead of silently overwriting its flags.

    ``create`` is create, not a lossy upsert: re-running it must not reset a
    channel's read/write flags (a no-flag re-create of a ``--read-only`` channel
    would otherwise downgrade it to public) nor mint a new ``channel_id``.
    """
    radio.channel('alerts', read_only=True)
    before = next(c for c in radio.channels() if c['channel'] == 'alerts')
    # a duplicate create is refused with a clear error
    with pytest.raises(ValueError, match='already exists'):
        radio.channel('alerts')
    # the original flags and id are untouched (no silent downgrade)
    after = next(c for c in radio.channels() if c['channel'] == 'alerts')
    assert after['read_only'] == 1
    assert after['channel_id'] == before['channel_id']


def test_channel_delete_scoped_to_owner(radio_pair: tuple[Radio, Radio]) -> None:
    """Deleting a custom channel cascades its data -- in one channel-space only.

    The schema has no ON DELETE CASCADE, so channel_delete removes the
    channel's messages, reacts, and receipts itself; with every node's
    channels in one table, the cascade must not touch another node's
    same-named channel.
    """
    root, peer = radio_pair
    root.channel('team')
    peer.channel('team')
    root_msg, _, _ = root.send(channel='team', subject='ours', data='d', priority=5)
    peer_msg, _, _ = peer.send(channel='team', subject='theirs', data='d', priority=5)
    root.react(root_msg, 1)
    root.channel_delete('team', force=True)
    # the root's channel-space is gone: message, react, receipt, channel row
    assert not root.db.exists('messages', where={'message_uuid': root_msg})
    assert root.db.read('reacts') == []
    assert not root.db.exists(
        'channels', where={'node': root.node.branch, 'channel': 'team'}
    )
    # the peer's same-named channel and message survive untouched
    assert peer.db.exists('messages', where={'message_uuid': peer_msg})
    assert [c['channel'] for c in peer.channels() if c['channel'] == 'team']


def test_channel_delete_refuses_messages_without_force(radio: Radio) -> None:
    """Channel delete refuses a channel that holds messages unless forced.

    Mirrors ``unsend`` -- the cascade also removes other nodes' replies,
    so it is refused without ``--force``, leaving the channel intact.
    """
    radio.channel('team')
    radio.send(channel='team', subject='s', data='d', priority=5)
    # a channel with messages is refused, leaving it and them intact
    with pytest.raises(ValueError, match='use --force'):
        radio.channel_delete('team')
    assert len(radio.messages(channel='team')) == 1
    assert [c['channel'] for c in radio.channels() if c['channel'] == 'team']
    # --force deletes the channel and cascades its messages
    radio.channel_delete('team', force=True)
    assert [c['channel'] for c in radio.channels() if c['channel'] == 'team'] == []
    assert radio.messages(channel='team') == []


def test_subscribe_and_unsubscribe(radio: Radio) -> None:
    """Subscribe writes sub rows for readable channels; unsubscribe removes them."""
    branch = radio.node.branch
    radio.subscribe(branch, channel='outbox')
    radio.subscribe(branch, channel='public')
    channels = {s['channel'] for s in radio.subs() if s['target'] == branch}
    assert channels == {'outbox', 'public'}
    # unsubscribe one channel, then all
    radio.unsubscribe(branch, channel='outbox')
    remaining = [s['channel'] for s in radio.subs() if s['target'] == branch]
    assert remaining == ['public']
    radio.unsubscribe(branch)
    assert [s for s in radio.subs() if s['target'] == branch] == []


def test_subscribe_validates_target(radio: Radio) -> None:
    """Subscribe rejects unknown nodes, channels, and unreadable channels."""
    branch = radio.node.branch
    with pytest.raises(ValueError, match='Node not found'):
        radio.subscribe('main.ghost', channel='public')
    with pytest.raises(ValueError, match='Channel not found'):
        radio.subscribe(branch, channel='nope')
    with pytest.raises(ValueError, match='cannot be subscribed'):
        radio.subscribe(branch, channel='private')


def test_archive_round_trip_per_saver(radio_pair: tuple[Radio, Radio]) -> None:
    """Save copies a message per saver; unsave removes only the own copy."""
    root, peer = radio_pair
    cast, _, _ = peer.send(channel='outbox', subject='keep', data='body', priority=5)
    root.save(cast)
    peer.save(cast)
    # each saver holds its own copy, attributed to the source host
    [root_copy] = root.saved()
    assert root_copy['message_uuid'] == cast
    assert root_copy['owner'] == peer.node.branch
    assert len(peer.saved()) == 1
    # re-saving is idempotent; unsave removes only the caller's copy
    root.save(cast)
    assert len(root.saved()) == 1
    root.unsave(cast)
    assert root.saved() == []
    assert len(peer.saved()) == 1


def test_archive_survives_unsend(radio_pair: tuple[Radio, Radio]) -> None:
    """An archived copy outlives the original message (the copy's purpose)."""
    root, peer = radio_pair
    cast, _, _ = peer.send(channel='outbox', subject='going', data='body', priority=5)
    root.save(cast)
    peer.unsend(cast)
    assert not root.db.exists('messages', where={'message_uuid': cast})
    [copy] = root.saved()
    assert copy['message_uuid'] == cast
    assert copy['data'] == 'body'


def test_unsave_not_found_stays_terse(radio: Radio) -> None:
    """Keep unsave's not-found terse -- it only ever reads the own archive."""
    with pytest.raises(ValueError, match='not found'):
        radio.unsave('DEADBEEF')


@pytest.mark.parametrize(
    argnames='method',
    argvalues=['read', 'thread', 'reply', 'react', 'save', 'unsend'],
)
def test_not_found_by_uuid(radio: Radio, method: str) -> None:
    """A not-found UUID raises a terse ValueError on every by-uuid command.

    UUIDs are globally unique in the central database, so an unresolvable one
    simply does not exist -- there is no other database to point at.
    """
    extra = {'reply': ('body',), 'react': (1,)}.get(method, ())
    call = getattr(radio, method)
    with pytest.raises(ValueError, match='not found'):
        call('DEADBEEF', *extra)


def test_unsend_deletes_message(radio: Radio) -> None:
    """Unsend removes a message from the database."""
    uuid, _, _ = radio.send(subject='s', data='d', priority=0)
    assert len(radio.messages()) == 1
    radio.unsend(uuid)
    assert len(radio.messages()) == 0


def test_read_receipt_skips_a_reused_message_id(radio: Radio) -> None:
    """A receipt never lands on a message that re-minted a stale id.

    ``message_id`` is ``INTEGER PRIMARY KEY`` without ``AUTOINCREMENT``, so
    unsending the newest message and sending another reuses its id. A reader
    holding the old message must not receipt the new one: the guarded write
    drops it once the id no longer maps to the fetched uuid.
    """
    u1, _, _ = radio.send(channel='public', subject='s1', data='d', priority=5)
    stale = radio.db.read('messages', where={'message_uuid': u1})[0]
    radio.unsend(u1)
    u2, _, _ = radio.send(channel='public', subject='s2', data='d', priority=5)
    reused = radio.db.read('messages', where={'message_uuid': u2})[0]
    # precondition: the id was genuinely reused (no AUTOINCREMENT)
    assert reused['message_id'] == stale['message_id']

    # a stale read of the unsent message must not receipt the new one
    radio._mark_read(stale)
    receipts = radio.db.read('reads', where={'message_id': reused['message_id']})
    assert receipts == []


def test_unsend_deletes_replies_and_reacts(radio: Radio) -> None:
    """Forced unsend cascades to replies, reacts, and reads."""
    uuid, _, _ = radio.send(subject='root', data='d', priority=0)
    # add a reply and a react
    radio.reply(uuid, 'reply data')
    radio.react(uuid, 1)
    radio.read(uuid)
    assert len(radio.db.read('messages')) == 2
    assert len(radio.db.read('reacts')) == 1
    # --force unsends the whole thread, cascading everything
    radio.unsend(uuid, force=True)
    assert len(radio.db.read('messages')) == 0
    assert len(radio.db.read('reacts')) == 0
    assert len(radio.db.read('reads')) == 0


def test_unsend_refuses_thread_without_force(radio: Radio) -> None:
    """Unsend refuses a message with replies unless forced."""
    uuid, _, _ = radio.send(subject='root', data='d', priority=0)
    radio.reply(uuid, 'reply data')
    # a message with replies is refused, leaving the thread intact
    with pytest.raises(ValueError, match='has replies'):
        radio.unsend(uuid)
    assert len(radio.db.read('messages')) == 2


def test_unsend_rejects_callers_other_than_the_sender(
    radio_pair: tuple[Radio, Radio],
) -> None:
    """Unsend rejects if caller is not the sender."""
    root, peer = radio_pair
    uuid, _, _ = root.send(peer.node.branch, subject='s', data='d', priority=0)
    with pytest.raises(PermissionError, match='sender can unsend'):
        peer.unsend(uuid)


def test_unsend_force_aborts_on_concurrent_reply(
    radio: Radio,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Abort an unsend(force) when a reply lands between the walk and delete.

    The descendant set is re-collected immediately before the cascade; a reply
    that arrived in between would be re-rooted into an orphan, so unsend refuses
    and leaves the thread intact for a retry.
    """
    # a message to unsend, with a hook to inject a racing reply mid-walk
    uuid, _, _ = radio.send(channel='public', subject='s', data='d', priority=0)
    original = radio._collect_message_ids
    calls = {'n': 0}

    def injecting(message_id: int, result: list) -> None:
        calls['n'] += 1
        original(message_id, result)
        if calls['n'] == 1:  # a reply lands after the first walk
            radio.reply(uuid, 'late')

    monkeypatch.setattr(radio, '_collect_message_ids', injecting)
    with pytest.raises(ValueError, match='mid-unsend'):
        radio.unsend(uuid, force=True)
    # the thread survived -- the abort happened before any delete
    assert radio.db.exists('messages', where={'message_uuid': uuid})


# ------ helpers


def _register_peer(root_radio: Radio, name: str) -> Radio:
    """Register a fresh peer node under the tree root and return its Radio.

    Mirrors the ``radio_pair`` fixture: a real worktree so the branch
    resolves, a hand-built config, seeded channels, then the root
    subscribes (the ``child_add`` order).
    """
    root = root_radio.node
    repo = root.worktree
    peer_branch = f'{root.branch}.{name}'
    worktree = repo / '.worktrees' / peer_branch
    subprocess.run(
        ['git', 'worktree', 'add', '-b', peer_branch, f'{worktree}', root.branch],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    node_dir = worktree / '.fractal' / peer_branch
    node_dir.mkdir(parents=True)
    config = {
        'project': '.',
        'root': root.branch,
        'scope': '',
        'agent': 'claude',
        'local': False,
        'detached': False,
    }
    (node_dir / 'config.json').write_text(
        json.dumps(config, indent=2), encoding='utf-8'
    )
    (node_dir / '.status').write_text('idle\n', encoding='utf-8')
    root.db.merge({'node': peer_branch, 'status': 'idle'}, 'nodes')
    peer = Radio(Node(worktree))
    peer.init()
    root_radio.subscribe(peer_branch)
    return peer


def _is_read_only(row: dict, radio: Radio) -> bool:
    """Whether ``row`` sits in a read-only channel owned by another node."""
    if row['node'] == radio.node.branch:
        return False
    channels = radio.db.read(
        'channels', where={'node': row['node'], 'channel': row['channel']}, limit=1
    )
    return bool(channels and channels[0]['read_only'])
