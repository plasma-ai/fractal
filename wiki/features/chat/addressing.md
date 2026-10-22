---
name: features/chat/addressing
desc: |
  How a chat turn is addressed and delivered: the command form, the
  fresh/fork/resume session modes, prompt seeding, and what a turn does
  and does not touch.
created: 2026-07-21T04:35:35Z
updated: 2026-07-21T04:35:35Z
---

# features/chat/addressing

[[features/chat/_index|..]]

***

## Command form

`fractal node chat [<branch>] "<prompt>"` sends one prompt to a node's agent and
streams the reply. The target branch defaults to the node at `--path` (default:
the current directory); the prompt is required — an empty or missing prompt is
rejected before anything runs. When the stream ends, the resulting session id is
printed to stderr as `session: <id>` so the thread can be continued in a later
turn.

## Session modes

Nothing is inferred by default, so a bare chat is always **fresh** — a brand-new
session. Three flags select other transports:

- `--session <id>` **forks** the given session: the reply continues that
  conversation's context, but the source session is left untouched.
- `--session <id> --resume` **continues the session in place** — same id, the
  thread itself advances. Resuming the node's live loop session is refused (it
  would perturb the running loop, or the session a paused run resumes with);
  fork it with `--current` instead.
- `--current` **forks the node's live loop session** — the session the loop is
  weaving while the node is active or paused. It is mutually exclusive with
  `--session`/`--resume`, and errors when the node has no live session.

Forking is the safety property of the surface: a running loop is never perturbed
by someone chatting with its node. Not every agent can fork — codex can resume
in place but cannot fork, so `--current` and forking a codex session are refused
with the remedy in the error.

## Prompt seeding

The mode decides what framing the agent sees around the prompt. A fresh chat is
seeded with the node's `NODE.md` charter plus the chat mode framing
(`modes/CHAT.md`), so a cold agent knows whose node it speaks for. A fork gets
only the chat framing — the agent was executing the loop, and the seeding tells
it it is now chatting. A resume continues an already-framed thread and adds
nothing.

## What a turn touches

A chat turn runs the node's effective agent with its configured model
(overridable per turn with `--model`) and the node's configured reasoning
effort, so a chat answer runs at the same depth as the loop's own steps. It
books nothing: no cost rows accrue to the node (see
[[features/cost/_index|cost]]) and the node's recorded sessions are unchanged.
The reply's closing line shows the turn's own cost, at both doors: a resumed or
forked Claude session's result figure counts on from the total Claude Code saved
for the source session, so before spawning the turn reads that baseline through
the same agent verb the loop uses and its parser takes it off, refusing a figure
it cannot settle (`$?`; see [[features/cost/measurement]]). A node with no agent
configured refuses with a pointer to `fractal init --agent`, and an agent
process that exits non-zero surfaces as an error rather than a silent empty
reply.

## The cockpit door

The cockpit's chat pane (see [[features/tui/_index|tui]]) is the same surface
with the transport chosen for you: an explicitly selected session wins;
otherwise an active or paused forking node's live session is forked, and every
other shape — non-forking, detached, no session woven yet, or a settled node —
gets a fresh seeded session (falling back with a warning where a fork is
impossible). The pane builds each turn through the same core invocation builder
the CLI uses, so validation and seeding behave identically at both doors. Which
lifecycle states admit which of these shapes is specified in
[[features/chat/lifecycle_gating|lifecycle_gating]].
