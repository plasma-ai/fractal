---
name: features/chat/lifecycle_gating
desc: |
  Which lifecycle states admit which chat shapes: chat itself is legal
  in every state, but the live loop session exists only while a node is
  active or paused, and paused is the flagship interrogation case.
created: 2026-07-21T04:35:35Z
updated: 2026-11-27T23:15:11Z
---

# features/chat/lifecycle_gating

[[features/chat/_index|..]]

***

Chat always reaches a real agent — no node state diverts a turn anywhere else.
There is no status that forbids chatting with a node; what the lifecycle gates
is **which sessions are reachable**, not whether the surface answers.

## Active and paused: the live session exists

A live loop session — the session the loop weaves, or the one a paused run will
resume with — exists only while the node's status is `active` or `paused`. Only
in those states can `--current` fork it; anywhere else the flag errors because
there is nothing live to fork. The live session is also never continued in
place: resuming it would perturb the running loop, or corrupt the thread a
paused run resumes with, so an explicit `--resume` on it is refused with a
pointer to `--current`.

**Paused is the flagship case.** Pausing a node freezes its loop while keeping
the run adoptable (see [[features/lifecycle/_index|lifecycle]]), and chat is one
of the few operations legal on a paused node — "pause it, then ask what it was
doing" is the intended interrogation flow: fork the paused session with
`--current`, ask questions with the loop's full context, and the frozen session
is untouched when `resume` later adopts it.

## Everything else: fresh sessions

A settled node — `completed`, `stopped`, `exited`, or `killed` — plus a
`retired` node or an `idle` node between runs, has no live session, so a bare
chat starts a fresh seeded session (the node's `NODE.md` charter plus chat
framing; see [[features/chat/addressing|addressing]]). Forking a *specific*
historical session with `--session <id>` remains available in any state, since
it touches nothing live.

## Agent-shape gates

Two gates come from the agent rather than the lifecycle. A non-forking agent
(codex, muse) cannot fork any session: on such a node `--current` is refused
with the remedy in the error, and the cockpit's automatic transport falls back
to a fresh session with a warning instead of forking a live or paused thread.
And a node with no agent configured anywhere up its ancestry refuses every chat
shape, pointing at `fractal init --agent`.
