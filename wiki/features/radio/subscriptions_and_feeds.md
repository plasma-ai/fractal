---
name: features/radio/subscriptions_and_feeds
desc: |
  How nodes follow each other: subscription rows, automatic wiring between
  parent and child, blind nodes, and the feed that fans reads out across
  subscriptions.
created: 2026-07-21T04:50:25Z
updated: 2026-07-21T04:50:25Z
---

# features/radio/subscriptions_and_feeds

[[_index|..]]

***

A subscription is a (subscriber, target, channel) row: the subscriber follows
one channel on one node. `fractal radio sub --node=<branch>` subscribes to all
of a target's readable channels, or to one with `--channel`; a read-only channel
cannot be subscribed. `fractal radio unsub` removes matching rows and reports
the true rowcount — removing 0 still exits 0, so a mistyped target is visible
without breaking scripts. `fractal radio subs` lists the node's subscriptions.

## Automatic wiring

Radio initialization at node creation seeds the default channels, then
subscribes the new node to its parent's readable channels and to each
already-registered direct child's. The complementary direction is wired at spawn
time: when a parent registers a child, it auto-subscribes to the child's
readable channels. The net effect is that parent and child follow each other's
`outbox` and `public` — radio reach is one hop; there is no tree-wide view, so
information crosses levels by relaying.

A node created blind (the `blind` config) seeds channels only: it holds no
subscriptions of its own, and so has no feed. The wiring is one-way — the parent
still subscribes to the blind child, so its reports flow upward.

## The feed

`fractal radio feed` fans out one query per subscription, re-checks each target
channel is still readable, merges the rows, and re-sorts by priority
(descending) then creation time — `--recent` switches to newest first. Each
row's node column names its source. Filters: `--node` and `--channel` narrow
which subscriptions fan out — an unregistered node and a channel held by no
subscription both refuse, since either could only ever render an empty feed;
`--since` bounds by timestamp; `--limit` caps rows post-merge.

The feed is a metadata listing — it never shows bodies (except via
`--json --body`) and never writes read receipts. Only thread roots appear;
replies stay behind their parent's reply count (see
[[features/radio/reactions_and_replies]]). The default view is unread-only, with
`--all` and `--read` variants; an empty unread view names the uncapped total on
stderr so "no mail" and "all read" are distinguishable. Reading feed bodies —
and consuming unread state — is `fractal radio read --feed` (see
[[features/radio/read_receipts]]).
