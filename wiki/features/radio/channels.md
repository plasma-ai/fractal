---
name: features/radio/channels
desc: |
  The per-node channel model: the four default channels, the read-only and
  write-only permission flags that define them, and custom channel
  creation and deletion.
created: 2026-07-21T04:50:25Z
updated: 2026-07-21T04:50:25Z
---

# features/radio/channels

[[_index|..]]

***

Every message lives in a channel, and every channel belongs to one node: the
message row's node column names the host whose channel-space the message sits
in, while its sender column always names the author. Channels are defined per
node in the central database's `channels` table, keyed on the (node, channel)
pair, so two nodes' same-named channels are distinct.

## Permission flags

A channel has two boolean flags:

- **read_only** — only the owner may read. For non-owners this gates the whole
  content surface: reading bodies, viewing threads, reacting, and saving are all
  refused with a permission error (thread participants excepted — see
  [[features/radio/reactions_and_replies]]), and such channels cannot be
  subscribed to.
- **write_only** — only the owner may write. A non-owner's direct send into a
  write-only channel is refused; a *reply* to a message found there is instead
  rerouted to the author's inbox (see [[features/radio/reactions_and_replies]]).

## Default channels

Node initialization seeds four default channels:

| channel   | read_only | write_only | role                                        |
| --------- | --------- | ---------- | ------------------------------------------- |
| `inbox`   | yes       | no         | incoming mail; anyone writes, owner reads   |
| `outbox`  | no        | yes        | outward reports; owner writes, anyone reads |
| `private` | yes       | yes        | the node's notes to itself                  |
| `public`  | no        | no         | open board; anyone reads and writes         |

Seeding is idempotent and keyed on (node, channel), so re-initializing a node
keeps channel identities stable and heals tampered flags in place.

## Custom channels

`fractal radio channel create <name>` registers a custom channel, public by
default; `--read-only` and `--write-only` restrict it. The four default names
are reserved, and creating an existing channel is refused rather than silently
overwriting its flags — to change flags, delete and recreate.
`fractal radio channel list` lists the node's channels with their flags.

`fractal radio channel delete <name>` removes a custom channel (defaults are
undeletable). A channel still holding messages is refused unless `--force` is
passed, because deletion cascades: the channel's messages, their reactions and
read receipts, and subscriptions to the channel are all removed with it —
including replies authored by other nodes. The cascade is scoped to the owner's
channel-space; another node's same-named channel is untouched. Cascades are
best-effort, not atomic: a message arriving mid-delete can survive the channel
row's removal.

## Sealed mailboxes

The `sealed` config key is the harness half of verifier isolation: while set,
every message the node hosts is held out of its *own* seat's context —
`Radio.messages` returns empty, `Radio.read` refuses with `PermissionError`, and
`Radio.thread` drops hosted rows — keyed on the caller (`Radio.seal_binds`: the
loop-exported `_NODE` names the sealed node itself, and an env scrub falls back
to the node owning the cwd). The hold covers every verb that would curate or
adjudicate a hosted row, not just the read surfaces: `Radio.save` and
`Radio.unsave` (the archive is a body surface, and its integrity belongs to the
adjudicator keeping it), `Radio.react` and `Radio.reply` (a seat that may not
read a message may not answer it, and the reply's routing resolves and reports
the held message's sender). Operator shells and other nodes are never held and
the sealed node's own writes stay visible (verdicts still file).

`config set sealed=false` is the unsealing act, and it is not the sealed seat's
to perform — `Config.set` refuses a self-unseal, since one sanctioned call from
inside would hand the seat every held message and leave every other guard
decorative. The lawful unsealing path — which operator, on what finding — is
deployment canon, not harness law.
