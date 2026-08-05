"""End-to-end tests for the ``fractal radio`` CLI across two nodes.

The radio layer is inter-node messaging over the tree's central database:
each node owns a channel-space (``public``/``private``/``inbox``/``outbox``
plus custom ones) whose rows carry the hosting ``node``, with messages,
subscriptions, reactions, and read receipts. These tests drive the real
``fractal`` console script as a subprocess against a throwaway repo with a
user (root) node and two worker nodes, exercising routing, permissions,
read tracking, threads, reactions, the archive, and channel management as
observable end-to-end workflows rather than internal state, including
machine-output guarantees (an empty query emits the same header a
populated one would, and ``--json`` mirrors the CSV shape).
"""

from __future__ import annotations

import csv
import io
import json
import pathlib
import re
import subprocess
import time
from typing import Any, Optional

import pytest
import typer

from fractal.cli.cmd import radio as radio_cmd
from fractal.core.radio import Radio
from tests._helpers import _git

from .conftest import _run

__all__ = [
    'test_send_and_post_route_across_nodes_by_channel',
    'test_send_and_post_reject_write_only_and_out_of_range_priority',
    'test_failed_commands_end_with_an_unmistakable_failed_line',
    'test_node_and_parent_are_mutually_exclusive',
    'test_bare_post_lands_in_outbox',
    'test_named_target_channel_defaults',
    'test_send_channel_only_defaults_to_self',
    'test_send_crosses_classes_and_post_refuses_private',
    'test_channel_not_found_names_the_remedy',
    'test_missing_options_aggregate_into_one_error',
    'test_send_and_post_echo_resolved_channel',
    'test_bare_messages_defaults_to_inbox',
    'test_read_tracking_drives_messages_filters',
    'test_read_multiple_uuids_and_shape_errors',
    'test_read_path_selects_mailbox_never_reader',
    'test_read_reader_follows_node_env',
    'test_read_refuses_cross_tree_mailbox',
    'test_read_without_reader_names_the_remedy',
    'test_listings_are_passive_and_metadata_only',
    'test_send_sender_follows_node_env',
    'test_sealed_inbox_holds_the_seat_but_not_the_operator',
    'test_send_fans_out_with_receipts_and_relays_lists_lineage',
    'test_listings_read_your_writes_and_close_with_a_watermark',
    'test_watermark_stamps_the_pre_query_cut',
    'test_post_and_reply_follow_node_env',
    'test_write_verbs_follow_node_env',
    'test_stale_node_env_refused_cleanly',
    'test_sent_command_lists_outbound_mail',
    'test_feed_fans_out_over_subscriptions',
    'test_feed_listing_passive_and_read_feed_catches_up',
    'test_reply_builds_thread_and_respects_write_only',
    'test_inbox_reply_visible_to_counterparty',
    'test_reply_echoes_resolved_destination',
    'test_outbox_reply_routes_to_sender_inbox',
    'test_react_toggles_positive_and_negative',
    'test_save_unsave_round_trips_through_archive',
    'test_saved_listings_honor_filters',
    'test_subscribe_unsubscribe_manage_subs',
    'test_channel_create_and_delete_lifecycle',
    'test_cross_node_read_emits_receipt_without_mutating_sender',
    'test_empty_messages_query_emits_a_header',
    'test_listing_filters_that_can_only_be_empty_refuse',
    'test_empty_and_populated_headers_match',
    'test_json_listings_mirror_csv_shape',
    'test_body_column_is_json_only',
]


@pytest.fixture(scope='module')
def repo(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Return a repo with a user node and two worker nodes (alpha, beta).

    Built once through the real CLI so the tests exercise ``init`` (and the
    ``.git/info/exclude`` it writes), default-channel seeding, and the
    parent/child auto-subscriptions that ``Radio.init`` performs. Tests
    append only their own messages, receipts, and reactions (addressed by
    per-test UUIDs) and round-trip any other state they touch (channels
    they create, subscriptions, the archive), so they never collide on the
    shared repo.

    Returns:
        Mapping of ``root``, ``alpha``, and ``beta`` worktree paths.

    """
    root = tmp_path_factory.mktemp('fractal_radio')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'radio@test.local')
    _git(root, 'config', 'user.name', 'radio')
    (root / 'README.md').write_text('# radio\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    # fractal init creates the user node, so node init then passes
    assert _run(root, 'init').returncode == 0
    for name in ('alpha', 'beta'):
        node = _run(root, 'node', 'init', name, '--agent', 'claude')
        assert node.returncode == 0
    return {
        'root': root,
        'alpha': root / '.worktrees' / 'main.alpha',
        'beta': root / '.worktrees' / 'main.beta',
    }


# ------ send routing and permissions


@pytest.mark.parametrize(
    argnames=('verb', 'channel', 'subject'),
    argvalues=[
        ('post', 'public', 'pub'),
        ('send', 'inbox', 'inb'),
    ],
)
def test_send_and_post_route_across_nodes_by_channel(
    repo: dict,
    verb: str,
    channel: str,
    subject: str,
) -> None:
    """A targeted write lands in the recipient's channel-space, not the sender's.

    ``send`` reaches beta's ``inbox`` (privately readable, others may
    write) and ``post`` its open ``public`` board; the message appears in
    beta's own message list (and alpha's ``sent``, recipient-attributed)
    and carries alpha as the sender -- alpha's own mailbox never lists it.
    The listing is metadata-only; the body arrives through ``read``.
    """
    alpha, beta = repo['alpha'], repo['beta']
    body = f'routed via {channel}'
    writer = _post if verb == 'post' else _send
    uuid = writer(
        path=alpha,
        data=body,
        channel=channel,
        subject=subject,
        node='main.beta',
    )
    # bare `messages` defaults to inbox; name the channel the send targeted
    listing = _radio(beta, 'messages', '--all', '--channel', channel).stdout
    assert uuid in listing
    assert 'main.alpha' in listing
    # the body never rides the listing -- read is the body surface
    assert body not in listing
    assert body in _radio(beta, 'read', uuid).stdout
    # the sender's mailbox stays empty; its `sent` carries the recipient
    assert uuid not in _radio(alpha, 'messages', '--all', '--channel', channel).stdout
    sent = _radio(alpha, 'sent', '--channel', channel).stdout
    assert uuid in sent
    assert 'main.beta' in sent


@pytest.mark.parametrize(
    argnames=('verb', 'channel'),
    argvalues=[
        ('send', 'private'),
        ('post', 'outbox'),
    ],
)
def test_send_and_post_reject_write_only_and_out_of_range_priority(
    repo: dict,
    verb: str,
    channel: str,
) -> None:
    """Write-only channels and out-of-range priorities are refused cleanly.

    ``private`` (send class) and ``outbox`` (post class) are write-only
    (owner only), so a foreign write is a permission error; a priority
    outside 0-10 is a value error. Both are domain errors raised in the
    core, so they must surface through the ``@command`` wrapper as a clean
    ``Error: <message>`` (exit 1) -- never the raw
    ``PermissionError:``/``ValueError:`` class name that reads like an
    uncaught crash.
    """
    alpha = repo['alpha']
    # foreign write into a write-only channel is rejected
    blocked = _radio(
        alpha,
        verb,
        'nope',
        '--channel',
        channel,
        '--node',
        'main.beta',
        '--subject',
        's',
        '--priority',
        '5',
    )
    assert blocked.returncode == 1
    assert blocked.stderr.startswith('Error:')
    assert 'PermissionError' not in blocked.stderr
    assert 'write-only' in blocked.stderr.lower()
    # priority above the 0-10 range is rejected
    too_high = _radio(
        alpha,
        verb,
        'nope',
        '--node',
        'main.beta',
        '--subject',
        's',
        '--priority',
        '11',
    )
    assert too_high.returncode == 1
    assert too_high.stderr.startswith('Error:')
    assert 'ValueError' not in too_high.stderr
    assert 'priority' in too_high.stderr.lower()


def test_failed_commands_end_with_an_unmistakable_failed_line(repo: dict) -> None:
    """A failed command's LAST line is ``FAILED (exit N)``; success has none.

    The phantom-send class: an unknown-option error frame read through
    ``tail -1`` looked identical to a success frame, so a night of failed
    sends passed as delivered. Every failure now closes with a line naming
    the failure and the exit code -- parse errors (exit 2, with the correct
    usage named) and domain errors (exit 1) alike -- and a successful send
    never carries it.
    """
    alpha = repo['alpha']
    # unknown option: rejected at parse time, naming the correct usage
    unknown = _radio(alpha, 'send', 'hi', '--body', 'x')
    assert unknown.returncode == 2
    assert 'No such option' in unknown.stderr
    assert 'Usage:' in unknown.stderr
    assert unknown.stderr.rstrip().splitlines()[-1] == 'FAILED (exit 2)'
    assert unknown.stdout.strip() == ''
    # domain error: exit 1 through the command wrapper, same closing line
    missing = _radio(
        alpha,
        'send',
        'hi',
        '--node',
        'main.ghost',
        '--subject',
        's',
        '--priority',
        '5',
    )
    assert missing.returncode == 1
    assert missing.stderr.startswith('Error:')
    assert missing.stderr.rstrip().splitlines()[-1] == 'FAILED (exit 1)'
    # a successful send never carries the failure line
    sent = _radio(
        alpha,
        'send',
        'hi',
        '--node',
        'main.beta',
        '--subject',
        's',
        '--priority',
        '5',
    )
    assert sent.returncode == 0
    assert 'FAILED' not in sent.stderr
    # round-trip: withdraw the probe message so the shared mailbox stays clean
    result = _radio(alpha, 'unsend', sent.stdout.strip())
    assert result.returncode == 0


def test_node_and_parent_are_mutually_exclusive(repo: dict) -> None:
    """``--node`` and ``--parent`` cannot be combined.

    ``--parent`` addresses the structural parent (``main`` for
    ``main.alpha``), so pairing it with an explicit ``--node`` is
    contradictory and rejected. A bare ``--parent`` send succeeds.
    """
    alpha = repo['alpha']
    clash = _radio(
        alpha,
        'send',
        'x',
        '--channel',
        'inbox',
        '--node',
        'main.beta',
        '--parent',
        '--subject',
        's',
        '--priority',
        '3',
    )
    assert clash.returncode != 0
    assert 'mutually exclusive' in clash.stderr.lower()
    # a parent-directed send resolves to the user node and succeeds
    parent_send = _radio(
        alpha,
        'send',
        'hi parent',
        '--parent',
        '--subject',
        'p',
        '--priority',
        '4',
    )
    assert parent_send.returncode == 0
    uuid = parent_send.stdout.strip()
    # bare `messages` defaults to inbox -- exactly where the send landed
    root_listing = _radio(repo['root'], 'messages', '--all').stdout
    assert uuid in root_listing


def test_bare_post_lands_in_outbox(repo: dict) -> None:
    """A bare ``post`` (no --node/--parent/--channel) reports to the ``outbox``.

    Reporting out is the common case: an untargeted post defaults to the
    poster itself, and a self post lands in its own ``outbox``, where the
    parent's feed picks it up.
    """
    alpha = repo['alpha']
    posted = _radio(
        alpha,
        'post',
        'a status report',
        '--subject',
        'report',
        '--priority',
        '5',
    )
    assert posted.returncode == 0, posted.stderr
    uuid = posted.stdout.strip()
    assert uuid in _radio(alpha, 'messages', '--all', '--channel', 'outbox').stdout
    assert uuid not in _radio(alpha, 'messages', '--all', '--channel', 'public').stdout
    assert uuid not in _radio(alpha, 'messages', '--all', '--channel', 'inbox').stdout


@pytest.mark.parametrize(
    argnames=('verb', 'node', 'host', 'channel'),
    argvalues=[
        ('send', 'main.alpha', 'alpha', 'inbox'),
        ('send', 'main.beta', 'beta', 'inbox'),
        ('post', 'main.beta', 'beta', 'public'),
    ],
    ids=['send-self', 'send-other', 'post-other'],
)
def test_named_target_channel_defaults(
    repo: dict,
    verb: str,
    node: str,
    host: str,
    channel: str,
) -> None:
    """A named ``send`` target always defaults to its inbox, self included.

    An operator standing in a node's worktree who names that node expects
    the directive to land where syncs read -- a cwd-keyed reroute to
    ``private`` would be a silent black hole -- so a self ``send`` defaults
    to the own ``inbox`` like any other named target (a private note stays
    explicit via ``--channel=private``). A targeted ``post`` lands on the
    other node's ``public`` board (its ``outbox`` is owner-only write). The
    bare-``post`` default -- the own ``outbox`` -- is pinned in
    ``test_bare_post_lands_in_outbox``.
    """
    alpha = repo['alpha']
    written = _radio(
        alpha,
        verb,
        f'defaults to {channel}',
        '--node',
        node,
        '--subject',
        'dflt',
        '--priority',
        '5',
    )
    assert written.returncode == 0, written.stderr
    uuid = written.stdout.strip()
    assert f"'{channel}' channel" in written.stderr
    listing = _radio(repo[host], 'messages', '--all', '--channel', channel).stdout
    assert uuid in listing


@pytest.mark.parametrize(
    argnames=('channel', 'notice'),
    argvalues=[
        (
            'private',
            "Node unspecified: sending to your 'private' channel.",
        ),
        (
            'outbox',
            "Node unspecified: posting to your 'outbox' channel"
            " (consider using 'radio post').",
        ),
    ],
    ids=['privately-readable', 'publicly-readable'],
)
def test_send_channel_only_defaults_to_self(
    repo: dict,
    channel: str,
    notice: str,
) -> None:
    """A channel-only ``send`` lands on the sender itself, with one notice.

    A channel alone is a valid routing dimension: the target defaults to
    self and one stderr line names the resolution. The wording keys on the
    channel's readability -- a privately readable channel is a true
    self-send, while a publicly readable one is a post in disguise, so the
    line nudges toward ``radio post``.
    """
    alpha = repo['alpha']
    written = _radio(
        alpha,
        'send',
        f'self via {channel}',
        '--channel',
        channel,
        '--subject',
        'selfd',
        '--priority',
        '5',
    )
    assert written.returncode == 0, written.stderr
    uuid = written.stdout.strip()
    lines = written.stderr.strip().splitlines()
    assert lines == [notice, f"sent to main.alpha's '{channel}' channel"]
    assert uuid in _radio(alpha, 'messages', '--all', '--channel', channel).stdout


def test_send_crosses_classes_and_post_refuses_private(repo: dict) -> None:
    """``send`` writes any permitted channel; ``post`` keeps its public class.

    ``send`` needs only a routing dimension -- readability class is not its
    business, so it reaches another node's ``public`` board (write
    permission still gates: a foreign ``outbox``/``private`` stays
    owner-only). ``post`` stays the quiet public subset and refuses a
    privately readable channel naming the sibling verb.
    """
    alpha, beta = repo['alpha'], repo['beta']
    # a fully explicit cross-class send is silent beyond the routing echo
    crossed = _radio(
        alpha,
        'send',
        'onto the board',
        '--node',
        'main.beta',
        '--channel',
        'public',
        '--subject',
        's',
        '--priority',
        '5',
    )
    assert crossed.returncode == 0, crossed.stderr
    uuid = crossed.stdout.strip()
    assert crossed.stderr.strip() == "sent to main.beta's 'public' channel"
    assert uuid in _radio(beta, 'messages', '--all', '--channel', 'public').stdout
    # a post into a privately readable channel is send's job
    wrong_post = _radio(
        alpha,
        'post',
        'x',
        '--node',
        'main.beta',
        '--channel',
        'inbox',
        '--subject',
        's',
        '--priority',
        '5',
    )
    assert wrong_post.returncode == 1
    assert 'fractal radio send' in wrong_post.stderr


@pytest.mark.parametrize(
    argnames=('target_args', 'message'),
    argvalues=[
        (
            [],
            "No 'ghost' channel found: specify a target node or create it.",
        ),
        (
            ['--node', 'main.alpha'],
            "No 'ghost' channel found: create it first.",
        ),
        (
            ['--node', 'main.beta'],
            "Node main.beta has no channel 'ghost'.",
        ),
    ],
    ids=['no-target', 'self-target', 'other-target'],
)
def test_channel_not_found_names_the_remedy(
    repo: dict,
    target_args: list[str],
    message: str,
) -> None:
    """A missing channel names the remedy matching how it was addressed.

    With no target the channel may live on a node the caller forgot to
    name; on the caller itself the fix is creating it; on another node the
    error names that node. The failure carries no defaulting notice --
    those fire only on a successful write.
    """
    alpha = repo['alpha']
    missing = _radio(
        alpha,
        'send',
        'x',
        *target_args,
        '--channel',
        'ghost',
        '--subject',
        's',
        '--priority',
        '5',
    )
    assert missing.returncode == 1
    assert message in missing.stderr
    assert 'unspecified' not in missing.stderr


def test_missing_options_aggregate_into_one_error(repo: dict) -> None:
    """A bare ``send <data>`` names every missing option in one round-trip.

    ``--subject`` and ``--priority`` stay required (no defaults) on both
    verbs, and ``send`` also needs a routing dimension (a target or a
    channel) -- all reported together (exit 2, one round-trip instead of
    one-error-at-a-time), with the reporting-out remedy named when both
    dimensions are missing. A supplied option drops out of the aggregate.
    """
    alpha = repo['alpha']
    bare = _radio(alpha, 'send', 'x')
    assert bare.returncode == 2
    error = ' '.join(bare.stderr.split())
    assert 'a target or channel' in error
    assert '--subject' in error
    assert '--priority' in error
    assert 'fractal radio post' in error
    # a supplied option drops from the aggregate; the rest still report
    partial = _radio(alpha, 'send', 'x', '--node', 'main.beta', '--subject', 's')
    assert partial.returncode == 2
    error = ' '.join(partial.stderr.split())
    assert '--priority' in error
    assert '--subject' not in error
    assert 'a target or channel' not in error
    assert 'fractal radio post' not in error
    # a channel alone satisfies the routing dimension the same way
    channeled = _radio(alpha, 'send', 'x', '--channel', 'inbox')
    assert channeled.returncode == 2
    error = ' '.join(channeled.stderr.split())
    assert 'a target or channel' not in error
    assert 'fractal radio post' not in error
    # post needs no target; its missing options aggregate the same way
    bare_post = _radio(alpha, 'post', 'x')
    assert bare_post.returncode == 2
    error = ' '.join(bare_post.stderr.split())
    assert '--subject' in error
    assert '--priority' in error
    assert 'a target or channel' not in error


@pytest.mark.parametrize(
    argnames=('verb', 'target_args', 'channel', 'target', 'notice'),
    argvalues=[
        ('post', [], 'outbox', 'main.alpha', None),
        ('post', ['--node', 'main.beta'], 'public', 'main.beta', None),
        (
            'send',
            ['--node', 'main.beta'],
            'inbox',
            'main.beta',
            "Channel unspecified: sending to main.beta's 'inbox' channel.",
        ),
        (
            'send',
            ['--parent'],
            'inbox',
            'main',
            "Channel unspecified: sending to main's 'inbox' channel.",
        ),
        (
            'send',
            ['--node', 'main.alpha'],
            'inbox',
            'main.alpha',
            "Channel unspecified: sending to your 'inbox' channel.",
        ),
        (
            'send',
            ['--node', 'main.beta', '--channel', 'inbox'],
            'inbox',
            'main.beta',
            None,
        ),
    ],
    ids=[
        'bare-post',
        'node-post',
        'node-send',
        'parent-send',
        'self-send',
        'explicit-send',
    ],
)
def test_send_and_post_echo_resolved_channel(
    repo: dict,
    verb: str,
    target_args: list[str],
    channel: str,
    target: str,
    notice: Optional[str],
) -> None:
    """Every ``send``/``post`` echoes its resolved channel and target to stderr.

    Misdelivery is visible immediately, for agents too (unconditional,
    not TTY-gated). A ``send`` that left its channel implicit gets one
    extra stderr line naming the resolution; a fully explicit ``send``
    and every ``post`` (self-defaulting silently -- it is the quiet
    reporting verb) add nothing beyond the echo. Stdout stays exactly
    the message UUID so scripts capturing it keep working.
    """
    alpha = repo['alpha']
    sent = _radio(
        alpha,
        verb,
        'echo check',
        *target_args,
        '--subject',
        'echo',
        '--priority',
        '5',
    )
    assert sent.returncode == 0, sent.stderr
    # stderr is exactly the defaulting notice (when one applies) plus the echo
    expected = [notice] if notice else []
    expected.append(f"sent to {target}'s '{channel}' channel")
    assert sent.stderr.strip().splitlines() == expected
    # stdout is the bare UUID, nothing else
    assert sent.stdout.strip() == sent.stdout.strip().splitlines()[0]
    assert len(sent.stdout.strip()) == 8


def test_bare_messages_defaults_to_inbox(repo: dict) -> None:
    """Bare ``messages`` shows only the inbox, silently when piped.

    Comingling the inbox with the caller's own outbox/private rows would
    bury inbound mail, so a bare ``messages`` defaults to inbox. The
    defaulting notice is TTY-only -- a piped caller (an agent reading
    radio every sync) gets silence.
    """
    alpha = repo['alpha']
    inbox_uuid = _send(alpha, 'to inbox', channel='inbox', subject='inb2')
    # bare post -> own outbox (see test above)
    posted = _radio(
        alpha,
        'post',
        'to outbox',
        '--subject',
        'out2',
        '--priority',
        '5',
    )
    outbox_uuid = posted.stdout.strip()
    bare = _radio(alpha, 'messages', '--all')
    assert inbox_uuid in bare.stdout  # inbox is shown
    assert outbox_uuid not in bare.stdout  # outbox is not (bare = inbox only)
    assert 'defaulting to inbox' not in bare.stderr  # notice is TTY-only


# ------ read tracking, feed, threads


def test_read_tracking_drives_messages_filters(repo: dict) -> None:
    """``messages`` defaults to unread; reading flips a message to read.

    A fresh self-message shows under the default (unread) view and under
    ``--all``; once read it disappears from the default view but appears
    under ``--read``. ``read`` itself echoes the full message including
    its UUID.
    """
    alpha = repo['alpha']
    uuid = _send(alpha, 'unread body', channel='inbox', subject='track')
    # unread by default and under --all
    assert uuid in _radio(alpha, 'messages').stdout
    assert uuid in _radio(alpha, 'messages', '--all').stdout
    # reading echoes the message and marks it read
    shown = _radio(alpha, 'read', uuid)
    assert shown.returncode == 0
    assert uuid in shown.stdout
    assert 'unread body' in shown.stdout
    # now hidden from the default (unread) view, visible under --read
    assert uuid not in _radio(alpha, 'messages').stdout
    assert uuid in _radio(alpha, 'messages', '--read').stdout


def test_read_multiple_uuids_and_shape_errors(repo: dict) -> None:
    """``read`` prints every named UUID once; malformed shapes are rejected."""
    alpha = repo['alpha']
    first = _send(alpha, 'first body', channel='inbox', subject='m1')
    second = _send(alpha, 'second body', channel='inbox', subject='m2')
    shown = _radio(alpha, 'read', first, second, first)
    assert shown.returncode == 0, shown.stderr
    assert shown.stdout.count('first body') == 1
    assert shown.stdout.count('second body') == 1
    assert shown.stdout.index('first body') < shown.stdout.index('second body')
    # a bare read has nothing to read
    bare = _radio(alpha, 'read')
    assert bare.returncode != 0
    # --unread needs a selector to narrow
    narrowed = _radio(alpha, 'read', first, '--unread')
    assert narrowed.returncode != 0


def test_read_path_selects_mailbox_never_reader(repo: dict) -> None:
    """``--path`` picks the mailbox viewed; receipts name the actual reader.

    The reader is the cwd-resolved node (``_NODE`` in production loops),
    never ``--path`` -- so a peek receipts as the peeker, and a read-only
    channel can never be impersonated via ``--path``.
    """
    alpha, root = repo['alpha'], repo['root']
    uuid = _post(alpha, 'peek body', channel='outbox', subject='peek')
    # the operator peeks at alpha's outbox from the root worktree
    peek = _run(root, 'radio', 'read', '--channel', 'outbox', '--path', f'{alpha}')
    assert peek.returncode == 0, peek.stderr
    assert 'peek body' in peek.stdout
    # the receipt is the root's: alpha's own unread view never moved ...
    assert uuid in _radio(alpha, 'messages', '--channel', 'outbox').stdout
    # ... while the root's next unread-narrowed peek skips the row
    again = _run(
        root,
        'radio',
        'read',
        '--channel',
        'outbox',
        '--unread',
        '--path',
        f'{alpha}',
    )
    assert again.returncode == 0, again.stderr
    assert uuid not in again.stdout
    # a read-only channel never impersonates: the root cannot read alpha's inbox
    _send(alpha, 'owner only', channel='inbox', subject='own')
    denied = _run(root, 'radio', 'read', '--channel', 'inbox', '--path', f'{alpha}')
    assert denied.returncode == 1
    assert 'read-only' in denied.stderr


def test_read_reader_follows_node_env(repo: dict) -> None:
    """An exported ``_NODE`` names the reader regardless of cwd and ``--path``.

    Production loops export ``_NODE`` for the node they drive, so a read run
    from anywhere attributes its receipts to that node; without it the cwd's
    node reads.
    """
    alpha, beta, root = repo['alpha'], repo['beta'], repo['root']
    uuid = _post(beta, 'env body', channel='public', subject='env')
    # read from the ROOT worktree with alpha's identity exported
    shown = _run(root, 'radio', 'read', uuid, _NODE=f'{alpha}')
    assert shown.returncode == 0, shown.stderr
    assert 'env body' in shown.stdout
    # the receipt is alpha's: alpha's unread-narrowed view skips the row ...
    as_alpha = _run(
        root,
        'radio',
        'read',
        '--channel',
        'public',
        '--unread',
        '--path',
        f'{beta}',
        _NODE=f'{alpha}',
    )
    assert as_alpha.returncode == 0, as_alpha.stderr
    assert uuid not in as_alpha.stdout
    # ... while the root (no _NODE) never read it and still gets the body
    as_root = _run(
        root,
        'radio',
        'read',
        '--channel',
        'public',
        '--unread',
        '--path',
        f'{beta}',
    )
    assert as_root.returncode == 0, as_root.stderr
    assert uuid in as_root.stdout


def test_read_refuses_cross_tree_mailbox(
    repo: dict,
    tmp_path: pathlib.Path,
) -> None:
    """A ``--path`` into another fractal tree is refused, never silently mixed.

    The reader's radio resolves branch names against its own central DB, and
    branch names collide across trees by construction (every tree roots at
    ``main``), so a foreign mailbox would silently read -- and receipt -- the
    reader's own same-named mailbox.
    """
    root = repo['root']
    # a second tree whose root branch collides with the reader's ('main')
    other = tmp_path / 'other'
    other.mkdir()
    _git(other, 'init', '-b', 'main')
    _git(other, 'config', 'user.email', 'radio@test.local')
    _git(other, 'config', 'user.name', 'radio')
    (other / 'README.md').write_text('# other\n', encoding='utf-8')
    wiki = other / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(other, 'add', '-A')
    _git(other, 'commit', '-m', 'init')
    assert _run(other, 'init').returncode == 0
    # each root posts to its own outbox; only --path distinguishes them
    _post(root, 'home body', channel='outbox', subject='home')
    _post(other, 'away body', channel='outbox', subject='away')
    refused = _run(root, 'radio', 'read', '--channel', 'outbox', '--path', f'{other}')
    assert refused.returncode != 0
    assert 'different fractal tree' in refused.stderr
    assert 'home body' not in refused.stdout
    assert 'away body' not in refused.stdout


def test_read_without_reader_names_the_remedy(
    repo: dict,
    tmp_path: pathlib.Path,
) -> None:
    """A read with no resolvable reader says how to become one.

    From a cwd outside any node (no ``_NODE`` exported), the missing piece
    is the reader identity, not the mailbox -- ``--path`` already names a
    real node. The error must name the actual remedy, not ``resolve_node``'s
    generic advice to ``fractal init`` the operator's cwd.
    """
    alpha = repo['alpha']
    uuid = _post(alpha, 'reader body', channel='outbox', subject='who')
    lost = _run(tmp_path, 'radio', 'read', uuid, '--path', f'{alpha}')
    assert lost.returncode != 0
    assert 'No reader node' in lost.stderr
    assert 'fractal init' not in lost.stderr


def test_listings_are_passive_and_metadata_only(repo: dict) -> None:
    """``messages`` never writes receipts and never prints bodies.

    Every ``_radio`` call is a fresh ``fractal`` process, so each assertion
    crosses a session/run boundary; receipts move only when the reader
    reads, and only for the rows the read selected. A ``--json --body``
    listing shows bodies but stays just as passive -- it never becomes a
    read surface.
    """
    beta = repo['beta']
    marked = _send(beta, 'triaged body', channel='inbox', subject='mk1')
    spared = _send(beta, 'later body', channel='private', subject='mk2')
    # plain listings are passive: the row stays unread across two runs
    assert marked in _radio(beta, 'messages').stdout
    listing = _radio(beta, 'messages')
    assert marked in listing.stdout
    # ... and metadata-only: the subject rides, the body and its column don't
    assert 'mk1' in listing.stdout
    assert 'triaged body' not in listing.stdout
    header = listing.stdout.splitlines()[0]
    assert 'data' not in header.split(',')
    # listings are passive: no --mark-read flag exists to force a receipt
    refused = _radio(beta, 'messages', '--mark-read')
    assert refused.returncode != 0
    # a --json --body listing shows the body yet writes no receipt either:
    # the row re-lists unread on the next pass
    bodied = _radio(beta, 'messages', '--json', '--body')
    assert 'triaged body' in bodied.stdout
    assert marked in _radio(beta, 'messages').stdout
    # reading is the consuming act; the receipt persists across runs
    shown = _radio(beta, 'read', '--channel', 'inbox', '--unread')
    assert shown.returncode == 0, shown.stderr
    assert 'triaged body' in shown.stdout
    assert marked not in _radio(beta, 'messages').stdout
    assert marked in _radio(beta, 'messages', '--read').stdout
    # rows outside the read's selection were untouched
    assert spared in _radio(beta, 'messages', '--channel', 'private').stdout


def test_send_sender_follows_node_env(repo: dict) -> None:
    """An exported ``_NODE`` names the sender regardless of cwd.

    Production loops export ``_NODE`` for the node they drive, so a send
    run from anywhere attributes its ``sender`` to that node -- a detached
    step's cwd is not a node identity; an explicit ``--path`` still wins,
    and without either the cwd's node sends.
    """
    alpha, beta, root = repo['alpha'], repo['beta'], repo['root']
    # send from the ROOT worktree with alpha's identity exported
    sent = _run(
        root,
        'radio',
        'send',
        'env-sent body',
        '--node',
        'main.beta',
        '--channel',
        'inbox',
        '--subject',
        'es',
        '--priority',
        '5',
        _NODE=f'{alpha}',
    )
    assert sent.returncode == 0, sent.stderr
    uuid = sent.stdout.strip()
    # the send attributes to alpha: alpha's sent listing carries it ...
    assert uuid in _radio(alpha, 'sent').stdout
    # ... and the root's does not
    assert uuid not in _radio(root, 'sent').stdout
    # an explicit --path still wins over the exported identity
    explicit = _run(
        root,
        'radio',
        'send',
        'path wins',
        '--node',
        'main.beta',
        '--channel',
        'inbox',
        '--subject',
        'pw',
        '--priority',
        '5',
        '--path',
        f'{beta}',
        _NODE=f'{alpha}',
    )
    assert explicit.returncode == 0, explicit.stderr
    assert explicit.stdout.strip() in _radio(beta, 'sent').stdout


def test_sealed_inbox_holds_the_seat_but_not_the_operator(repo: dict) -> None:
    """With ``sealed`` set, the seat's own reads hold; the operator's do not.

    The seat -- the caller acting as the sealed node, by the exported
    ``_NODE`` or by owning the working directory -- gets an empty,
    loudly-annotated listing and a refused ``read``; an operator working
    from outside the node still reads everything, so adjudication stays
    possible. The seat cannot lift the seal itself -- that one call would
    hand it every held message -- so unsealing
    (``config set sealed=false``) is the operator's, and it restores the
    view.
    """
    alpha, beta, root = repo['alpha'], repo['beta'], repo['root']
    uuid = _send(beta, 'sealed adjudication', node='main.alpha', priority=9)
    assert _run(alpha, 'node', 'config', 'set', 'sealed=true').returncode == 0
    # the sealed seat: empty annotated listing, refused body surface
    held = _run(root, 'radio', 'messages', '--all', _NODE=f'{alpha}')
    assert held.returncode == 0
    assert uuid not in held.stdout
    assert 'inbox sealed' in held.stderr
    refused = _run(root, 'radio', 'read', uuid, _NODE=f'{alpha}')
    assert refused.returncode == 1
    assert 'inbox sealed' in refused.stderr
    # the operator adjudicates freely -- from outside the sealed node, since
    # the seal binds any caller acting AS that node (its own worktree
    # included, so an env scrub inside the seat cannot lift it)
    visible = _run(root, 'radio', 'messages', '--all', '--path', f'{alpha}')
    assert uuid in visible.stdout
    # the seat's own unseal refuses; the operator's, from outside, lands
    self_unseal = _run(alpha, 'node', 'config', 'set', 'sealed=false')
    assert self_unseal.returncode == 1
    assert 'cannot lift its own seal' in self_unseal.stderr
    lawful = _run(root, 'node', 'config', 'set', 'sealed=false', '--path', f'{alpha}')
    assert lawful.returncode == 0, lawful.stderr
    unsealed = _run(root, 'radio', 'messages', '--all', _NODE=f'{alpha}')
    assert uuid in unsealed.stdout
    # round-trip: withdraw the probe message so the shared mailbox stays clean
    assert _radio(beta, 'unsend', uuid).returncode == 0


def test_send_fans_out_with_receipts_and_relays_lists_lineage(repo: dict) -> None:
    """A repeated ``--node`` returns per-recipient receipts; relays verify.

    A fan-out prints one ``<uuid> <node>`` receipt per recipient (each copy
    its own message), and ``radio relays <uuid>`` answers whether an order
    was ever relayed onward: empty lineage before, the marked copy after.
    """
    alpha, beta = repo['alpha'], repo['beta']
    fan = _radio(
        alpha,
        'send',
        'fleet order',
        '--node',
        'main.beta',
        '--node',
        'main',
        '--subject',
        'fo',
        '--priority',
        '8',
    )
    assert fan.returncode == 0, fan.stderr
    lines = fan.stdout.strip().splitlines()
    uuids = []
    for line, expected in zip(lines, ['main.beta', 'main']):
        uuid, target = line.split()
        assert target == expected
        uuids.append(uuid)
    assert len(set(uuids)) == 2
    order = uuids[0]
    # before any relay the lineage is empty -- the obligation reads unmet
    empty = _radio(beta, 'relays', order)
    assert empty.returncode == 0
    assert f'0 relays recorded for {order}' in empty.stderr
    # beta relays the order onward; the lineage now names the copy
    relayed = _radio(
        beta,
        'send',
        'fleet order (relayed)',
        '--node',
        'main',
        '--subject',
        'fo',
        '--priority',
        '8',
        '--relay-of',
        order,
    )
    assert relayed.returncode == 0, relayed.stderr
    rows = json.loads(_radio(beta, 'relays', order, '--json').stdout)
    assert [(row['sender'], row['node'], row['metadata']) for row in rows] == [
        ('main.beta', 'main', f'relay:{order}')
    ]
    # round-trip: withdraw the probe messages so the shared mailboxes stay clean
    for uuid in (*uuids, relayed.stdout.strip()):
        sender = alpha if uuid in uuids else beta
        assert _radio(sender, 'unsend', uuid).returncode == 0


def test_listings_read_your_writes_and_close_with_a_watermark(repo: dict) -> None:
    """Listings act as the exported node and stamp their cut on stderr.

    The false-record class: a send attributed to the exported ``_NODE``
    was graded against a listing that read the cwd's node, so a delivered
    send read as missing from its own sender's outbox. Listings resolve
    the acting node exactly like the writing verbs -- a send is visible in
    the sender's next ``sent`` listing and a delivered directive in the
    recipient's next inbox read, from any cwd -- and each listing closes
    with an ``as of <instant> (acting as <branch>)`` stderr watermark
    naming the cut it took.
    """
    alpha, beta, root = repo['alpha'], repo['beta'], repo['root']
    # send as alpha from the ROOT worktree (a detached step's cwd)
    sent = _run(
        root,
        'radio',
        'send',
        'ryw body',
        '--node',
        'main.beta',
        '--channel',
        'inbox',
        '--subject',
        'ryw',
        '--priority',
        '5',
        _NODE=f'{alpha}',
    )
    assert sent.returncode == 0, sent.stderr
    uuid = sent.stdout.strip()
    # the very next listing from the same foreign cwd shows the send ...
    listing = _run(root, 'radio', 'sent', _NODE=f'{alpha}')
    assert uuid in listing.stdout
    # ... and the recipient's next inbox listing shows the directive
    inbox = _run(root, 'radio', 'messages', '--all', _NODE=f'{beta}')
    assert uuid in inbox.stdout
    # each listing closes with the freshness watermark naming its actor
    watermark = r'as of \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \(acting as main\.alpha\)'
    assert re.search(watermark, listing.stderr)
    assert 'acting as main.beta' in inbox.stderr
    # round-trip: withdraw the probe message so the shared mailbox stays clean
    assert _run(root, 'radio', 'unsend', uuid, _NODE=f'{alpha}').returncode == 0


def test_watermark_stamps_the_pre_query_cut(
    repo: dict,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """The watermark instant is the clock read before the query, not render.

    A row a concurrent sender lands between the query and the stamp has
    ``created_at`` before the watermark yet is absent from the listing, so
    a render-time stamp endorses a false 'never sent' verdict -- the exact
    error the watermark exists to prevent; the pre-query cut only ever
    under-claims. Driven in-process (the interleaving is unreachable from
    a subprocess) with a stepped clock the query advances two minutes,
    modeling a slow render or pipe consumer: the stamp must still say the
    instant from before the query. ``thread`` and ``subs`` -- the surfaces
    a reply obligation grades from -- close with the watermark too.
    """

    class _SteppedClock:
        """Module-shaped time double whose queries advance the clock."""

        strftime = staticmethod(time.strftime)

        def __init__(self) -> None:
            self.now = 1_000_000_000.0

        def time(self) -> float:
            return self.now

        def gmtime(self, secs: Optional[float] = None) -> time.struct_time:
            return time.gmtime(self.now if secs is None else secs)

    clock = _SteppedClock()
    monkeypatch.setattr(radio_cmd, 'time', clock)
    monkeypatch.setenv('_NODE', '')

    def slow_query(self: Radio, *args: Any, **kwargs: Any) -> list:
        clock.now += 120
        return []

    def invoke(register: Any, query: str, **kwargs: Any) -> str:
        clock.now = 1_000_000_000.0
        monkeypatch.setattr(Radio, query, slow_query)
        app = typer.Typer()
        register(app)
        [entry] = app.registered_commands
        entry.callback(csv=True, json=False, path=f'{repo["alpha"]}', **kwargs)
        return capsys.readouterr().err

    pre_query = 'as of 2001-09-09T01:46:40Z (acting as main.alpha)'
    listing = invoke(
        radio_cmd.radio_sent, 'sent', channel=None, limit=None, since=None, recent=False
    )
    assert pre_query in listing
    assert 'as of 2001-09-09T01:48:40Z' not in listing
    assert pre_query in invoke(
        radio_cmd.radio_thread, 'thread', message_uuid='a1b2c3d4'
    )
    assert pre_query in invoke(radio_cmd.radio_subs, 'subs')


def test_post_and_reply_follow_node_env(repo: dict) -> None:
    """An exported ``_NODE`` names the post and reply sender alike.

    All three writing verbs share one sender resolution -- a bare post from
    a foreign cwd reports out to the exported node's own outbox, and a
    reply attributes to the exported node the same way.
    """
    alpha, beta, root = repo['alpha'], repo['beta'], repo['root']
    # post from the ROOT worktree with alpha's identity exported
    posted = _run(
        root,
        'radio',
        'post',
        'env-post body',
        '--subject',
        'ep',
        '--priority',
        '5',
        _NODE=f'{alpha}',
    )
    assert posted.returncode == 0, posted.stderr
    uuid = posted.stdout.strip()
    # the bare post reports out to alpha's outbox: alpha's sent listing
    # carries it and the root's does not
    assert uuid in _radio(alpha, 'sent').stdout
    assert uuid not in _radio(root, 'sent').stdout
    # a reply from the root worktree attributes to the exported node too
    replied = _run(
        root,
        'radio',
        'reply',
        uuid,
        'env-reply body',
        _NODE=f'{beta}',
    )
    assert replied.returncode == 0, replied.stderr
    assert replied.stdout.strip() in _radio(beta, 'sent').stdout


def test_write_verbs_follow_node_env(repo: dict) -> None:
    """An exported ``_NODE`` names the acting node for every row-writing verb.

    The archive, reaction, subscription, channel, and unsend verbs share the
    writing trio's sender resolution: run from the ROOT worktree as alpha,
    each row lands in alpha's listings and never the root's, the
    identity-gated deletes (sender-only ``unsend``, owner-scoped ``unsave``
    and ``channel delete``) succeed as alpha, and a re-react flips alpha's
    single vote in place instead of tallying a second node's. An explicit
    ``--path`` still wins, with the channel pair as the representative.
    """
    alpha, beta, root = repo['alpha'], repo['beta'], repo['root']
    uuid = _send(alpha, 'env verb body', channel='inbox', subject='ev')
    # save/unsave from the ROOT worktree with alpha's identity exported:
    # the archive copy is alpha's, never the root's, and only alpha's
    # exported identity finds it to unsave
    saved = _run(root, 'radio', 'save', uuid, _NODE=f'{alpha}')
    assert saved.returncode == 0, saved.stderr
    assert uuid in _radio(alpha, 'messages', '--saved').stdout
    assert uuid not in _radio(root, 'messages', '--saved').stdout
    unsaved = _run(root, 'radio', 'unsave', uuid, _NODE=f'{alpha}')
    assert unsaved.returncode == 0, unsaved.stderr
    assert uuid not in _radio(alpha, 'messages', '--saved').stdout
    # react keys one vote per node: alpha's own '+' then an env-keyed '-'
    # flips that single vote in place -- a root-attributed react would
    # tally a second vote (and be refused on alpha's read-only inbox)
    assert _radio(alpha, 'react', uuid, '+').returncode == 0
    reacted = _run(root, 'radio', 'react', uuid, '-', _NODE=f'{alpha}')
    assert reacted.returncode == 0, reacted.stderr
    rows = json.loads(_radio(alpha, 'messages', '--all', '--json').stdout)
    row = next(r for r in rows if r['message_uuid'] == uuid)
    assert (row['pos_reacts'], row['neg_reacts']) == (0, 1)
    # unsend is sender-gated: alpha's exported identity deletes its own
    # message, and the row vanishes from beta's mailbox
    sent_uuid = _send(alpha, 'env unsend body', node='main.beta', subject='eu')
    assert sent_uuid in _radio(beta, 'messages', '--all').stdout
    unsent = _run(root, 'radio', 'unsend', sent_uuid, _NODE=f'{alpha}')
    assert unsent.returncode == 0, unsent.stderr
    assert sent_uuid not in _radio(beta, 'messages', '--all').stdout
    # sub/unsub write alpha's subscription rows; the root's seeded set
    # never moves
    root_subs = _radio(root, 'subs').stdout
    subbed = _run(
        root,
        'radio',
        'sub',
        '--node',
        'main.beta',
        '--channel',
        'public',
        _NODE=f'{alpha}',
    )
    assert subbed.returncode == 0, subbed.stderr
    subs = list(csv.DictReader(io.StringIO(_radio(alpha, 'subs').stdout)))
    assert any(s['target'] == 'main.beta' and s['channel'] == 'public' for s in subs)
    unsubbed = _run(root, 'radio', 'unsub', '--node', 'main.beta', _NODE=f'{alpha}')
    assert unsubbed.returncode == 0, unsubbed.stderr
    assert 'Removed 1 subscription.' in unsubbed.stdout
    assert 'main.beta' not in _radio(alpha, 'subs').stdout
    assert _radio(root, 'subs').stdout == root_subs
    # channel create/delete act on alpha's channel-space -- the delete
    # cascade is keyed to the exported identity, so it succeeds only as
    # alpha (the root owns no such channel)
    created = _run(root, 'radio', 'channel', 'create', 'envverb', _NODE=f'{alpha}')
    assert created.returncode == 0, created.stderr
    assert 'envverb' in _radio(alpha, 'channel', 'list').stdout
    assert 'envverb' not in _radio(root, 'channel', 'list').stdout
    deleted = _run(root, 'radio', 'channel', 'delete', 'envverb', _NODE=f'{alpha}')
    assert deleted.returncode == 0, deleted.stderr
    assert 'envverb' not in _radio(alpha, 'channel', 'list').stdout
    # an explicit --path still wins over the exported identity
    explicit = _run(
        root,
        'radio',
        'channel',
        'create',
        'pathwins',
        '--path',
        f'{beta}',
        _NODE=f'{alpha}',
    )
    assert explicit.returncode == 0, explicit.stderr
    assert 'pathwins' in _radio(beta, 'channel', 'list').stdout
    assert 'pathwins' not in _radio(alpha, 'channel', 'list').stdout
    removed = _run(
        root,
        'radio',
        'channel',
        'delete',
        'pathwins',
        '--path',
        f'{beta}',
        _NODE=f'{alpha}',
    )
    assert removed.returncode == 0, removed.stderr
    assert 'pathwins' not in _radio(beta, 'channel', 'list').stdout


def test_stale_node_env_refused_cleanly(repo: dict, tmp_path: pathlib.Path) -> None:
    """A stale ``_NODE`` refuses as a usage error, for writers and the reader.

    The write verbs and ``read`` trust the loop-exported identity, so a
    stale or mistyped export -- a git repo that is no node, or a path that
    resolves to no worktree at all (a reaped node) -- must fail cleanly,
    naming the source and the remedy, instead of leaking an internal error
    or silently attributing to the cwd's node.
    """
    root = repo['root']
    stale = tmp_path / 'stale'
    stale.mkdir()
    _git(stale, 'init', '-b', 'main')
    _git(stale, 'config', 'user.email', 'radio@test.local')
    _git(stale, 'config', 'user.name', 'radio')
    _git(stale, 'commit', '--allow-empty', '-m', 'init')
    # a git repo that is no fractal node, and a path naming no worktree at
    # all, refuse the same way -- for a write verb and for read's reader
    for verb, bad in [
        (('post', 'stale body', '--subject', 'st', '--priority', '5'), stale),
        (
            ('post', 'gone body', '--subject', 'go', '--priority', '5'),
            tmp_path / 'gone',
        ),
        (('read', 'AAAA1111'), stale),
    ]:
        result = _run(root, 'radio', *verb, _NODE=f'{bad}')
        assert result.returncode != 0, result.stdout
        assert 'No fractal node' in result.stderr
        assert '_NODE' in result.stderr


def test_sent_command_lists_outbound_mail(repo: dict) -> None:
    """``sent`` lists own-authored messages with the recipient in ``node``.

    Outbound mail is invisible in the sender's own mailbox (it lives in the
    recipient's channel-space), so ``sent`` is the review surface: it lists
    what this node wrote -- bodies included, unlike the metadata listings --
    attributes each row to its host, and narrows with ``--channel``. A node
    that sent nothing still emits a header.
    """
    alpha = repo['alpha']
    to_beta = _send(alpha, 'for beta', channel='inbox', subject='sb', node='main.beta')
    to_self = _send(alpha, 'own note', channel='private', subject='sp')
    listing = _radio(alpha, 'sent').stdout
    assert to_beta in listing
    assert to_self in listing
    assert 'main.beta' in listing
    # sent keeps the body column: it reviews what this node actually wrote
    assert 'own note' in listing
    # --channel narrows to the matching host channel
    narrowed = _radio(alpha, 'sent', '--channel', 'private').stdout
    assert to_self in narrowed
    assert to_beta not in narrowed
    # a node that sent nothing still emits a header (machine output)
    empty = _radio(repo['root'], 'sent', '--channel', 'private')
    assert empty.stdout.strip() != ''
    assert to_self not in empty.stdout


def test_feed_fans_out_over_subscriptions(repo: dict) -> None:
    """``feed`` pulls readable messages from subscribed nodes, passively.

    After alpha subscribes to beta's ``public`` channel and beta posts
    there, the message surfaces in alpha's feed (filtered to that
    channel). Listing is passive (mirroring ``messages``): the same unread
    row re-lists on the next pass, and no receipt shows under ``--read``.
    """
    alpha, beta = repo['alpha'], repo['beta']
    assert (
        _radio(alpha, 'sub', '--node', 'main.beta', '--channel', 'public').returncode
        == 0
    )
    uuid = _post(beta, 'fan-out body', channel='public', subject='feed', priority=6)
    # the subscribed row surfaces on the default (unread) pass
    first = _radio(alpha, 'feed', '--node', 'main.beta', '--channel', 'public')
    assert first.returncode == 0
    assert uuid in first.stdout
    # listing is passive: the row re-lists unread, and no receipt exists
    feed = _radio(
        alpha,
        'feed',
        '--node',
        'main.beta',
        '--channel',
        'public',
    )
    assert uuid in feed.stdout
    assert uuid not in _radio(alpha, 'feed', '--channel', 'public', '--read').stdout
    # remove the subscription so later tests see the seeded baseline
    assert _radio(alpha, 'unsub', '--node', 'main.beta').returncode == 0


def test_feed_listing_passive_and_read_feed_catches_up(repo: dict) -> None:
    """``feed`` lists metadata passively; ``read --feed --unread`` consumes.

    Mirror of the ``messages`` contract on the fan-out surface: every
    ``_radio`` call is a fresh ``fractal`` process, so each assertion
    crosses a session/run boundary.
    """
    alpha, beta = repo['alpha'], repo['beta']
    for channel in ('public', 'outbox'):
        sub = _radio(alpha, 'sub', '--node', 'main.beta', '--channel', channel)
        assert sub.returncode == 0
    post = _post(beta, 'post body', channel='public', subject='fm1')
    cast = _post(beta, 'cast body', channel='outbox', subject='fm2')
    # plain feeds are passive and metadata-only across runs
    first = _radio(alpha, 'feed', '--node', 'main.beta')
    assert post in first.stdout
    assert 'post body' not in first.stdout
    second = _radio(alpha, 'feed', '--node', 'main.beta')
    assert post in second.stdout
    # listings are passive: no --mark-read flag exists to force a receipt
    refused = _radio(alpha, 'feed', '--mark-read')
    assert refused.returncode != 0
    # the feed catch-up prints bodies and receipts them for this reader
    shown = _radio(alpha, 'read', '--feed', '--unread')
    assert shown.returncode == 0, shown.stderr
    assert 'post body' in shown.stdout
    assert 'cast body' in shown.stdout
    # the receipts persist into later runs: gone from the default (unread)
    # view, visible under --read, and the source's own state never moved
    assert post not in _radio(alpha, 'feed', '--node', 'main.beta').stdout
    assert cast in _radio(alpha, 'feed', '--channel', 'outbox', '--read').stdout
    assert cast in _radio(beta, 'messages', '--channel', 'outbox').stdout
    # remove the subscriptions so later tests see the seeded baseline
    assert _radio(alpha, 'unsub', '--node', 'main.beta').returncode == 0


def test_reply_builds_thread_and_respects_write_only(repo: dict) -> None:
    """Replies nest under the root and cannot pierce a write-only channel.

    A local reply inherits the parent's subject (``Re: ...``) and shows
    as a descendant in ``thread``. A foreign reply into another
    node's write-only ``outbox`` never lands in it -- it reroutes to the
    owner's inbox as a conversation turn.
    """
    alpha, beta = repo['alpha'], repo['beta']
    root_uuid = _post(alpha, 'thread root', channel='public', subject='tree')
    reply = _radio(alpha, 'reply', root_uuid, 'a child')
    assert reply.returncode == 0, reply.stderr
    child_uuid = reply.stdout.strip()
    # thread defaults to the full tree (no --all needed): both show even after
    # the root has been read, child listed beneath the root
    _radio(alpha, 'read', root_uuid)
    tree = _radio(alpha, 'thread', root_uuid).stdout
    assert root_uuid in tree
    assert child_uuid in tree
    assert 'Re: tree' in tree
    # piped (non-TTY) output is CSV -- identical to an explicit --csv; the
    # indented tree render is TTY-only
    assert tree == _radio(alpha, 'thread', root_uuid, '--csv').stdout
    # a reply into beta's write-only outbox cannot pierce it -- the reply
    # reroutes to beta's inbox instead of being refused (the rerouted row
    # is asserted end-to-end in test_outbox_reply_routes_to_sender_inbox)
    out_uuid = _post(beta, 'owner only', channel='outbox', subject='ob')
    rerouted = _radio(alpha, 'reply', out_uuid, 'inject')
    assert rerouted.returncode == 0, rerouted.stderr
    assert "sent to main.beta's 'inbox' channel" in rerouted.stderr


def test_inbox_reply_visible_to_counterparty(repo: dict) -> None:
    """A counterparty-routed reply is visible to both parties end-to-end.

    A reply routed correctly at the row level can still be invisible if
    the message query keeps thread-roots only: the recipient's ``messages``
    (default and ``--all``) would miss it, the author's ``sent`` would miss
    it, and ``thread`` would error owner-only for the root's sender. All
    four symptoms share that one lesion, so one workflow asserts all four.
    """
    alpha, beta = repo['alpha'], repo['beta']
    # alpha mails beta's inbox; beta replies, which routes to alpha's inbox
    root_uuid = _send(alpha, 'question for beta', node='main.beta', subject='q')
    reply = _radio(beta, 'reply', root_uuid, 'the answer')
    assert reply.returncode == 0, reply.stderr
    reply_uuid = reply.stdout.strip().splitlines()[0]
    # the recipient's inbox view shows the routed reply
    inbox = _radio(alpha, 'messages', '--all')
    assert reply_uuid in inbox.stdout, inbox.stdout
    # the author's sent includes the counterparty-routed reply
    sent = _radio(beta, 'sent')
    assert reply_uuid in sent.stdout, sent.stdout
    # the thread reads whole for BOTH parties (the fourth symptom: an
    # owner-only error for the root's sender)
    for party in (alpha, beta):
        tree = _radio(party, 'thread', root_uuid)
        assert tree.returncode == 0, tree.stderr
        assert reply_uuid in tree.stdout, tree.stdout


def test_reply_echoes_resolved_destination(repo: dict) -> None:
    """``reply`` echoes its resolved destination to stderr like ``send``.

    A counterparty-routed reply lands in another node's channel-space, but
    a bare-UUID echo alone makes misdelivery invisible exactly where routing
    is least obvious. The echo mirrors ``send``'s: stderr, with stdout still
    exactly the UUID for capturing scripts.
    """
    alpha, beta = repo['alpha'], repo['beta']
    root_uuid = _send(alpha, 'ping', node='main.beta', subject='dest echo')
    reply = _radio(beta, 'reply', root_uuid, 'pong')
    assert reply.returncode == 0, reply.stderr
    assert "sent to main.alpha's 'inbox' channel" in reply.stderr


def test_outbox_reply_routes_to_sender_inbox(repo: dict) -> None:
    """A reply to another node's outbox message routes to the SENDER's inbox.

    The natural reaction to a feed post is ``reply`` on it, but a foreign
    outbox is write-only, and a bare refusal would force the whole fleet
    to work around it with fresh sends (channel context lost) -- so the
    reply routes to the sender's inbox, mirroring the inbox counterparty case.
    """
    alpha, beta = repo['alpha'], repo['beta']
    out_uuid = _post(beta, 'progress note', channel='outbox', subject='report')
    reply = _radio(alpha, 'reply', out_uuid, 'ack, steer starboard')
    assert reply.returncode == 0, reply.stderr
    reply_uuid = reply.stdout.strip().splitlines()[0]
    # the reply reached the sender's inbox, not the write-only outbox
    inbox = _radio(beta, 'messages', '--all')
    assert reply_uuid in inbox.stdout, inbox.stdout


# ------ reactions, archive, subscriptions


def test_react_toggles_positive_and_negative(repo: dict) -> None:
    """``react`` records a single vote per node, swapping +/- in place.

    A ``+`` reaction shows one positive react; re-reacting ``-`` replaces
    it (one negative, zero positive) rather than accumulating. A value
    other than +/- is rejected.
    """
    alpha = repo['alpha']
    uuid = _send(alpha, 'vote on me', channel='inbox', subject='react')

    def _counts() -> str:
        """Return the archive-free counts line for the message."""
        rows = _radio(alpha, 'messages', '--all').stdout.splitlines()
        return next(line for line in rows if uuid in line)

    plus = _radio(alpha, 'react', uuid, '+')
    assert plus.returncode == 0, plus.stderr
    positive = _counts()
    minus = _radio(alpha, 'react', uuid, '-')
    assert minus.returncode == 0, minus.stderr
    negative = _counts()
    # the line must change as the single vote flips sign
    assert positive != negative
    # an invalid reaction value is rejected
    invalid = _radio(alpha, 'react', uuid, 'x')
    assert invalid.returncode != 0


def test_save_unsave_round_trips_through_archive(repo: dict) -> None:
    """``save`` archives a message; ``unsave`` removes it.

    A saved message appears under ``messages --saved`` and disappears
    after ``unsave``. ``--saved`` is mutually exclusive with ``--read``,
    and unsaving an unknown UUID is an error.
    """
    alpha = repo['alpha']
    uuid = _send(alpha, 'keep me', channel='inbox', subject='save')
    saved = _radio(alpha, 'save', uuid)
    assert saved.returncode == 0, saved.stderr
    assert uuid in _radio(alpha, 'messages', '--saved').stdout
    unsaved = _radio(alpha, 'unsave', uuid)
    assert unsaved.returncode == 0, unsaved.stderr
    assert uuid not in _radio(alpha, 'messages', '--saved').stdout
    # --saved cannot be combined with --read
    clash = _radio(alpha, 'messages', '--saved', '--read')
    assert clash.returncode != 0
    # unsaving a message that was never archived is an error
    missing = _radio(alpha, 'unsave', 'DEADBEEF')
    assert missing.returncode != 0


def test_saved_listings_honor_filters(repo: dict) -> None:
    """``--saved`` narrows by the listing's filters instead of dropping them.

    The archive is one flat todo queue surfaced by both listings, so
    ``messages --saved`` honors ``--channel`` and ``feed --saved`` honors
    ``--node``/``--channel`` (``--node`` names the copy's source host);
    an unmatched filter empties the view rather than returning the whole
    archive.
    """
    alpha = repo['alpha']
    note = _send(alpha, 'note body', channel='private', subject='sfn')
    cast = _post(repo['root'], 'cast body', channel='outbox', subject='sfc')
    assert _radio(alpha, 'save', note).returncode == 0
    assert _radio(alpha, 'save', cast).returncode == 0
    try:
        # messages --saved narrows by channel
        listed = _radio(alpha, 'messages', '--saved', '--channel', 'private')
        assert note in listed.stdout
        assert cast not in listed.stdout
        # feed --saved narrows by source node and by channel
        listed = _radio(alpha, 'feed', '--saved', '--node', 'main')
        assert cast in listed.stdout
        assert note not in listed.stdout
        listed = _radio(alpha, 'feed', '--saved', '--channel', 'outbox')
        assert cast in listed.stdout
        assert note not in listed.stdout
        # an unmatched filter empties the view, never the whole archive
        listed = _radio(alpha, 'feed', '--saved', '--node', 'main.ghost')
        assert listed.returncode == 0
        assert note not in listed.stdout
        assert cast not in listed.stdout
    finally:
        # round-trip the archive so the shared repo stays clean
        for uuid in (note, cast):
            assert _radio(alpha, 'unsave', uuid).returncode == 0


def test_subscribe_unsubscribe_manage_subs(repo: dict) -> None:
    """``sub``/``unsub`` add and remove rows that ``subs`` lists.

    Subscribing alpha to beta's ``public`` channel adds a row naming the
    target node and channel; unsubscribing removes it while leaving the
    auto-seeded parent subscriptions intact. ``unsub`` reports the true
    rowcount -- a zero-match unsub prints 0 and still exits 0, so a
    mis-pathed target is visible without failing scripts.
    """
    alpha = repo['alpha']
    assert (
        _radio(alpha, 'sub', '--node', 'main.beta', '--channel', 'public').returncode
        == 0
    )
    subscribed = _radio(alpha, 'subs').stdout
    assert 'main.beta' in subscribed
    assert 'public' in subscribed
    unsubbed = _radio(alpha, 'unsub', '--node', 'main.beta')
    assert unsubbed.returncode == 0
    assert 'Removed 1 subscription.' in unsubbed.stdout
    after = _radio(alpha, 'subs').stdout
    assert 'main.beta' not in after
    # the parent subscriptions seeded at init survive
    assert 'main' in after
    # nothing left to remove reports the honest zero without failing
    rerun = _radio(alpha, 'unsub', '--node', 'main.beta')
    assert rerun.returncode == 0
    assert 'Removed 0 subscriptions.' in rerun.stdout


# ------ channel management


def test_channel_create_and_delete_lifecycle(repo: dict) -> None:
    """Custom channels can be created, used, and deleted; defaults cannot.

    A new ``team`` channel is listed and accepts a self-post; creating a
    reserved default name or deleting a default channel is refused. A
    channel holding messages is refused without ``--force`` (mirroring
    ``unsend``) and removed with it, after which it no longer appears.
    """
    beta = repo['beta']
    assert _radio(beta, 'channel', 'create', 'team').returncode == 0
    assert 'team' in _radio(beta, 'channel', 'list').stdout
    # the new channel is open by default, so it accepts a self-post
    posted = _post(beta, 'team body', channel='team', subject='t', priority=3)
    assert posted
    # default names are reserved for create and delete alike
    assert _radio(beta, 'channel', 'create', 'public').returncode != 0
    assert _radio(beta, 'channel', 'delete', 'inbox').returncode != 0
    # a channel holding messages is refused, but --force removes it
    assert _radio(beta, 'channel', 'delete', 'team').returncode != 0
    assert _radio(beta, 'channel', 'delete', 'team', '--force').returncode == 0
    assert 'team' not in _radio(beta, 'channel', 'list').stdout
    # deleting an unknown channel is an error
    assert _radio(beta, 'channel', 'delete', 'ghost').returncode != 0


def test_cross_node_read_emits_receipt_without_mutating_sender(
    repo: dict,
) -> None:
    """Reading a foreign message records a receipt, not an owner mutation.

    Alpha posts to its own readable ``outbox``; beta reads it directly by
    UUID (globally unique) and sees the full body. The read receipt is the
    reader's, so the message stays unread in alpha's own default view.
    """
    alpha, beta = repo['alpha'], repo['beta']
    uuid = _post(alpha, 'broadcast body', channel='outbox', subject='cast')
    remote = _radio(beta, 'read', uuid)
    assert remote.returncode == 0
    assert uuid in remote.stdout
    assert 'broadcast body' in remote.stdout
    # alpha never read it itself, so it stays in alpha's unread view
    assert uuid in _radio(alpha, 'messages', '--channel', 'outbox').stdout


# ------ machine output


def test_empty_messages_query_emits_a_header(repo: dict) -> None:
    """An empty ``messages`` query emits a header plus an unread notice.

    The stdout contract: ``node list`` passes ``columns=`` so an empty
    result still prints a header row; radio commands must do the same -- a
    zero-byte empty result would be indistinguishable from a failure when
    piped. The stderr contract: an empty default (unread) view names the
    uncapped total (the victims are agents, not TTY users), so "no new
    mail" never reads as "no mail at all"; a populated view stays quiet,
    and so do ``--all`` -- it already shows everything -- and ``--limit 0``,
    which empties any view.
    """
    beta = repo['beta']
    uuid = _send(beta, 'notice body', channel='private', subject='ntc')
    # a populated default view carries no notice
    populated = _radio(beta, 'messages', '--channel', 'private')
    assert uuid in populated.stdout
    assert '0 unread' not in populated.stderr
    # consume every unread row so the default view turns empty
    consumed = _radio(beta, 'read', '--channel', 'private', '--unread')
    assert consumed.returncode == 0, consumed.stderr
    shown = _radio(beta, 'messages', '--channel', 'private', '--all')
    total = len(shown.stdout.splitlines()) - 1
    assert total >= 1
    # the empty view keeps the bare-header stdout and names the total on stderr
    empty = _radio(beta, 'messages', '--channel', 'private')
    assert empty.stdout.splitlines() == [populated.stdout.splitlines()[0]]
    assert f'0 unread ({total} total; --all shows everything)' in empty.stderr
    assert '0 unread' not in shown.stderr
    # an empty --all query stays quiet too -- there is nothing more to show
    quiet = _radio(beta, 'messages', '--all', '--since', '9999-01-01T00:00:00Z')
    assert quiet.stdout.strip() != ''
    assert '0 unread' not in quiet.stderr
    # the notice's total ignores the row cap: a --limit below the channel's
    # size still names every message
    extra = _send(beta, 'capped body', channel='private', subject='cap')
    assert _radio(beta, 'read', extra).returncode == 0
    capped = _radio(beta, 'messages', '--channel', 'private', '--limit', '1')
    assert f'0 unread ({total + 1} total; --all shows everything)' in capped.stderr
    # --limit 0 empties any view, so it stays quiet even over unread mail
    unread = _send(beta, 'quiet body', channel='private', subject='qt')
    zeroed = _radio(beta, 'messages', '--channel', 'private', '--limit', '0')
    assert zeroed.returncode == 0, zeroed.stderr
    assert '0 unread' not in zeroed.stderr
    # consume the seeded unread row so later tests see the all-read channel
    assert _radio(beta, 'read', unread).returncode == 0


def test_listing_filters_that_can_only_be_empty_refuse(repo: dict) -> None:
    """A filter matching nothing refuses instead of narrating an empty view.

    An empty listing is a record: consumers grade verdicts off it, and the
    ``0 unread (0 total)`` notice states affirmatively that the mailbox
    holds nothing. A typo'd channel, an unknown feed target, and a
    ``--since`` that is not a timestamp all render exactly that view over
    a full mailbox -- and ``--since`` is worse than empty in the other
    direction, since a value sorting below the rows filters nothing at all
    while looking like it filtered. Each refuses at the boundary; a real
    channel that happens to be empty still lists quietly.
    """
    beta, alpha = repo['beta'], repo['alpha']
    # a real channel with no mail is a true empty view, not a refusal
    populated = _radio(beta, 'messages', '--channel', 'outbox', '--all')
    assert populated.returncode == 0, populated.stderr
    # a typo'd channel names what the mailbox actually has
    typo = _radio(beta, 'messages', '--channel', 'inbx')
    assert typo.returncode == 1
    assert "No 'inbx' channel on main.beta" in typo.stderr
    assert '0 unread' not in typo.stderr
    # so does a feed filter matching no subscription, and an unknown target
    unsubscribed = _radio(beta, 'feed', '--channel', 'inbx')
    assert unsubscribed.returncode == 1
    assert "No 'inbx' subscription" in unsubscribed.stderr
    unknown = _radio(beta, 'feed', '--node', 'main.nope')
    assert unknown.returncode == 1
    assert "Node not found: 'main.nope'" in unknown.stderr
    # --since is compared lexicographically against ISO 8601 instants, so a
    # non-timestamp is refused as a usage error on every listing that takes it
    for verb in ('messages', 'sent', 'feed'):
        for value in ('NOPE', '05/08/2026', '1785902960'):
            refused = _radio(alpha, verb, '--since', value)
            assert refused.returncode == 2, (verb, value, refused.stderr)
            assert 'ISO 8601' in refused.stderr
    # a bare date and a full instant both stand: they sort as real cuts
    for value in ('2026-01-31', '2026-01-31T14:00:00Z'):
        accepted = _radio(alpha, 'messages', '--all', '--since', value)
        assert accepted.returncode == 0, accepted.stderr


def test_empty_and_populated_headers_match(repo: dict) -> None:
    """Empty and populated listings emit the identical header shape.

    Parsers key on the populated CSV shape, so an empty result must present
    the same columns. Each listing is captured populated and forced empty
    (``--since`` far in the future, or with every subscription removed); the
    header line must not change.
    """
    alpha, beta, root = repo['alpha'], repo['beta'], repo['root']
    far_future = '9999-01-01T00:00:00Z'
    # seed one row per listing: a targeted send (messages + sent), a root
    # outbox post pulled through beta's seeded parent subscription (feed),
    # and an archived copy (saved)
    uuid = _send(alpha, 'parity body', subject='par', node='main.beta')
    _post(root, 'parity feed body', channel='outbox', subject='parf')
    assert _radio(beta, 'save', uuid).returncode == 0
    unread_messages = (
        _radio(beta, 'messages'),
        _radio(beta, 'messages', '--since', far_future),
    )
    unread_feed = (
        _radio(beta, 'feed'),
        _radio(beta, 'feed', '--since', far_future),
    )
    pairs = [
        (
            _radio(beta, 'messages', '--all'),
            _radio(beta, 'messages', '--all', '--since', far_future),
        ),
        (
            _radio(alpha, 'sent'),
            _radio(alpha, 'sent', '--since', far_future),
        ),
        (
            _radio(beta, 'feed', '--all'),
            _radio(beta, 'feed', '--all', '--since', far_future),
        ),
        (
            _radio(beta, 'messages', '--saved'),
            _radio(beta, 'messages', '--saved', '--since', far_future),
        ),
        (
            _radio(beta, 'feed', '--saved'),
            _radio(beta, 'feed', '--saved', '--since', far_future),
        ),
        unread_messages,
        unread_feed,
    ]
    for populated, empty in pairs:
        assert populated.returncode == 0
        assert empty.returncode == 0
        head, *rows = populated.stdout.splitlines()
        assert rows, 'populated capture must hold at least one data row'
        assert empty.stdout.splitlines() == [head]
    # the empty default (unread) captures also carry the stderr notice --
    # stdout stayed byte-identical to the populated header above -- and the
    # total honors the query's own filters, so a far-future window counts zero
    for populated, empty in (unread_messages, unread_feed):
        assert '0 unread (0 total; --all shows everything)' in empty.stderr
        assert '0 unread' not in populated.stderr
    # subs has no --since filter: empty it by removing the seeded parent
    # subscriptions, then restore them so later tests see the same state
    populated = _radio(root, 'subs')
    subs = list(csv.DictReader(io.StringIO(populated.stdout)))
    assert subs, 'root holds the auto-seeded child subscriptions'
    for sub in subs:
        unsubbed = _radio(
            root,
            'unsub',
            '--node',
            sub['target'],
            '--channel',
            sub['channel'],
        )
        assert unsubbed.returncode == 0
    try:
        empty = _radio(root, 'subs')
        assert empty.stdout.splitlines() == [populated.stdout.splitlines()[0]]
    finally:
        for sub in subs:
            resubbed = _radio(
                root,
                'sub',
                '--node',
                sub['target'],
                '--channel',
                sub['channel'],
            )
            assert resubbed.returncode == 0


def test_json_listings_mirror_csv_shape(repo: dict) -> None:
    """``--json`` renders each listing as an array of CSV-shaped objects.

    The JSON surface is additive -- CSV stays the piped default -- and
    mirrors the CSV projection: one object per row, keys in the CSV
    header's column order, ``[]`` for an empty result (never nothing).
    ``--json`` and ``--csv`` contradict and are refused.
    """
    alpha, beta, root = repo['alpha'], repo['beta'], repo['root']
    far_future = '9999-01-01T00:00:00Z'
    # seed one row per listing: a targeted send (messages, sent,
    # thread, saved) and a root outbox post pulled through beta's
    # seeded parent subscription (feed)
    uuid = _send(alpha, 'round-trip body', subject='jrt', node='main.beta')
    _post(root, 'json feed body', channel='outbox', subject='jfd')
    assert _radio(beta, 'save', uuid).returncode == 0
    listings = [
        (beta, ('messages', '--all')),
        (alpha, ('sent',)),
        (beta, ('feed', '--all')),
        (beta, ('messages', '--saved')),
        (alpha, ('thread', uuid)),
        (alpha, ('subs',)),
    ]
    for node, args in listings:
        header = _radio(node, *args, '--csv').stdout.splitlines()[0].split(',')
        listed = _radio(node, *args, '--json')
        assert listed.returncode == 0, listed.stderr
        rows = json.loads(listed.stdout)
        assert rows, 'populated capture must hold at least one row object'
        assert all(list(row) == header for row in rows)
    # values round-trip: the seeded row appears with its column values
    listed = json.loads(_radio(beta, 'messages', '--all', '--json').stdout)
    row = next(r for r in listed if r['message_uuid'] == uuid)
    assert row['sender'] == 'main.alpha'
    # an empty result is [] -- never zero bytes
    empty = _radio(beta, 'messages', '--all', '--json', '--since', far_future)
    assert json.loads(empty.stdout) == []
    # --json contradicts --csv
    clash = _radio(beta, 'messages', '--json', '--csv')
    assert clash.returncode != 0
    assert 'mutually exclusive' in clash.stderr.lower()


def test_body_column_is_json_only(repo: dict) -> None:
    """``--body`` widens the JSON projection to the data column, JSON only.

    Monitors get bodies without consuming the watched node's unread state:
    ``--json --body`` swaps the metadata projection for the full message
    columns on ``messages`` and ``feed``. The flag binds to ``--json`` --
    the CSV/table shapes never widen -- and the listing stays passive
    (pinned in ``test_listings_are_passive_and_metadata_only``).
    """
    beta, root = repo['beta'], repo['root']
    uuid = _send(beta, 'widened body', channel='private', subject='wb')
    posted = _post(root, 'widened feed body', channel='outbox', subject='wf')
    # --json --body carries the data column on both metadata listings
    listed = _radio(beta, 'messages', '--channel', 'private', '--json', '--body')
    rows = json.loads(listed.stdout)
    row = next(r for r in rows if r['message_uuid'] == uuid)
    assert row['data'] == 'widened body'
    fed = _radio(beta, 'feed', '--json', '--body')
    rows = json.loads(fed.stdout)
    row = next(r for r in rows if r['message_uuid'] == posted)
    assert row['data'] == 'widened feed body'
    # without --body the JSON projection stays metadata-only
    bare = _radio(beta, 'messages', '--channel', 'private', '--json')
    assert all('data' not in r for r in json.loads(bare.stdout))
    # --body binds to --json: the CSV/table shapes never widen
    refused = _radio(beta, 'messages', '--body')
    assert refused.returncode != 0
    assert '--json' in refused.stderr
    refused = _radio(beta, 'feed', '--body', '--csv')
    assert refused.returncode != 0
    assert '--json' in refused.stderr


# ------ helpers


def _radio(path: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    """Run ``fractal radio`` against the node at ``path``.

    The worktree is selected with ``--path`` so the command does not depend on
    the process working directory, which lets a single test drive both nodes.
    """
    return _run(path, 'radio', *args, '--path', f'{path}')


def _send(
    path: pathlib.Path,
    data: str,
    *,
    channel: str = 'inbox',
    subject: str = 's',
    priority: int = 5,
    node: Optional[str] = None,
) -> str:
    """Send a message and return its 8-char UUID.

    An omitted ``node`` self-targets the sending node explicitly (its
    worktree directory is named after its branch), keeping every helper
    send fully explicit and its stderr free of defaulting notices.
    """
    args = [
        'send',
        data,
        '--node',
        node if node is not None else path.name,
        '--channel',
        channel,
        '--subject',
        subject,
        '--priority',
        str(priority),
    ]
    result = _radio(path, *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _post(
    path: pathlib.Path,
    data: str,
    *,
    channel: str = 'public',
    subject: str = 's',
    priority: int = 5,
    node: Optional[str] = None,
) -> str:
    """Post a message and return its 8-char UUID."""
    args = [
        'post',
        data,
        '--channel',
        channel,
        '--subject',
        subject,
        '--priority',
        str(priority),
    ]
    if node is not None:
        args += ['--node', node]
    result = _radio(path, *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()
