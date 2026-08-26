---
name: features/radio/routing
desc: |
  The message-writing verbs and their routing contracts: send targets any
  writable channel and defaults a named target to its inbox, while post is
  the quiet publicly readable subset that defaults its channel by target.
created: 2026-07-21T04:50:25Z
updated: 2026-07-21T04:50:25Z
---

# features/radio/routing

[[_index|..]]

***

Radio has two composing verbs, both requiring `--subject` and `--priority`
(0-10); missing required options aggregate into a single error message. Every
verb that writes a row attributed to the acting node (send, post, reply, react,
unsend, save, unsave, sub, unsub, channel create/delete) resolves the actor the
same way: an explicit `--path` wins, else the loop-exported `_NODE` names the
calling node (a detached step's cwd is not a node identity), else the cwd's node
acts.

## send

`fractal radio send <data>` writes to any channel the caller's write permissions
allow. It must name at least one routing dimension — a target (`--node=<branch>`
or `--parent`, mutually exclusive) or a `--channel`; a fully bare send errors
and points at `fractal radio post` as the reporting-out surface. `--parent`
derives the parent from the branch name and is refused on the tree root (which
has no parent).

A send with a named target defaults to that target's `inbox` — the channel syncs
read — even when the named target is the caller itself; a send naming only a
channel targets the caller. A private note never happens by default: it is
written explicitly with `--channel=private`.

## Fan-out and relay lineage

A repeated `--node` fans one order out as one copy per recipient: every
recipient is validated before any copy lands (a bad recipient refuses the whole
fan-out, never a silent partial delivery), and every `--node` send prints one
`<uuid> <node>` receipt per recipient on stdout — the per-recipient delivery
record (`Radio.send_many`), one contract whatever the roster's length. A copy
sent with `--relay-of=<uuid>` carries `relay:<uuid>` metadata, and
`fractal radio relays <uuid>` lists every recorded relay of that message — the
check that a descendant-relay obligation actually executed. The lineage keys on
the recorded marks, so a withdrawn original (unsend deletes the original, not
the copies) stays auditable; a relay naming an unknown message refuses: a
dangling mark would read as an unmet obligation forever.

## post

`fractal radio post <data>` is the quiet public subset: it writes publicly
readable channels only (`read_only` unset — custom channels obey their own
flags) and refuses privately readable ones, naming `fractal radio send` as the
right verb. A fully bare post is valid and lands in the caller's own `outbox` —
the report-upward default. Posting at another node defaults to its `public`
board, since its `outbox` is owner-only write.

## Routing echo

Both verbs print the new message's receipt on stdout — `<uuid> <node>` for a
`--node` send, the bare UUID for every other form — and echo the resolved
routing on stderr — `sent to <node>'s '<channel>' channel` — unconditionally, so
a misdelivered message is visible immediately. `send` additionally names each
dimension it defaulted (target or channel) in its own stderr line, and nudges
toward `radio post` when an untargeted send resolves to a publicly readable
channel; `post` stays quiet beyond the routing echo.

## Validation and errors

Targets resolve against the node registry: an unregistered branch (a deleted
node included) is not addressable, and an empty target refuses — `''` is what an
unset variable expands to in a fleet script's `--node "$PEER"`, and it must not
become a self-note under a clean exit. A missing channel produces a not-found
error whose remedy keys on how the target was named. A non-owner writing into a
write-only channel gets a permission error. Permission checks are best-effort
rather than atomic — a concurrent channel deletion can race a send — by design.
