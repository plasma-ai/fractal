---
name: features/radio/read_receipts
desc: |
  Read state semantics: listings are passive, the read command writes
  per-reader receipts for exactly what it displays, and reacting or
  replying also marks the parent read.
created: 2026-07-21T04:50:58Z
updated: 2026-07-21T04:50:58Z
---

# features/radio/read_receipts

[[features/radio/_index|..]]

***

Read state is per-reader, with seen-by-you email semantics: a receipt is a
(message, reader) row in the central database, so one node's reading never moves
another node's unread view.

## Passive listings

`fractal radio messages` (own channels; `inbox` by default, so a node's own
outbox and private notes never read as incoming mail) and `fractal radio feed`
(subscriptions) are listings: metadata only — no bodies unless `--json --body`
widens the projection — and always passive, never writing receipts. The unread
view is therefore stable across calls. Default filter is unread-only; `--all`
shows everything, `--read` only receipted rows. Listings sort by priority then
age (`--recent` for newest-first), take `--limit`/`--since`, and output a table,
CSV (forced by `--csv`, default when piped), or `--json`. An empty unread view
names the uncapped total on stderr, so "no mail" and "all read" are
distinguishable. That notice is an affirmative record, so a filter that could
only ever be empty refuses rather than earning one: a `--channel` the mailbox
does not host, and a `--since` that is not an ISO 8601 date or timestamp (the
comparison is lexicographic, so an unparseable value hides the whole mailbox or
filters nothing at all, both while looking like a real cut).

`fractal radio sent` is the outbound counterpart — messages this node authored,
across every recipient's channel-space, with each row's node column naming the
recipient. It is equally passive but carries no read filter (receipts track what
*you* have seen, and these are your own words), its rows include the body
column, and authored replies list first-class rather than hiding behind their
parent's reply count.

## The read surface

`fractal radio read` is the body surface and the act that consumes unread state.
It accepts explicit UUIDs (resolved globally, returned in argument order), a
`--channel` selector, and/or `--feed`, each selector narrowable with `--unread`;
explicit UUIDs always return regardless of read state. Duplicates collapse to
first occurrence, and every returned message gets this reader's receipt —
receipts land only after all lookups resolve, so a failed UUID receipts nothing.

The reader is whoever runs the command — the loop exports the acting node, and
otherwise the working directory's node is the reader. `--path` selects only
whose mailbox is *viewed*, never who the receipts attribute to, so receipts stay
truthful; viewing a mailbox from a different fractal tree is refused loudly
rather than resolved against the wrong database.

## What else writes receipts

Reacting to a message and replying to it both mark it read for the actor, so an
acknowledged or answered message stops resurfacing in unread views each sync.
Reading a privately readable channel is owner-only; thread participants are
exempt where threads are concerned (see
[[features/radio/reactions_and_replies]]).
