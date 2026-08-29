---
name: user_flow/operating
desc: |
  The operator's role while a tree runs: monitoring nodes through list,
  status, activity, the TUI, and radio; steering through directives, NODE.md
  edits, chat, and retunes; and how the passive root node participates.
created: 2026-07-21T04:47:43Z
updated: 2026-07-21T04:47:43Z
---

# user_flow/operating

[[_index|..]]

***

Once nodes are running, the operator's role inverts: the tree works, you watch
and steer. Fractal is built for this asymmetry — the **user node** (the tree's
root, created by `fractal init`) never iterates and has no loop of its own. It
participates passively: it holds the central database, acts as the tree's radio
identity subscribed to its children's reports, and is the vantage point from
which tree-wide verbs (`fractal pause`, a root-level finish or stop) fan out.
Commands run from the repository root act as the user node.

## Monitoring

**Status at a glance.** `fractal node list` lists a node's descendants with
status, spend, and limits (run from the root it shows the whole tree; it never
includes the target row itself — `fractal node status <node>` gives one node's
status). `status` is always the bare lifecycle status, and any qualifier rides
the `detail` column beside it: a pending `pausing`/`stopping`/`finishing`
signal, an `exited` run's recorded end reason, `orphaned` for a settled node
whose worktree was removed out of band, or `model drop` while the newest
iteration carries a step served off its pinned model that the loop's single
re-dispatch could not resolve (see [[features/loop/steps|steps]]). Keeping the
two apart is what lets a script select on `status` without defending against a
suffix — and end reasons carry parentheses of their own, so a composed
`exited (<reason>)` could not be split back apart reliably. `spend` sits next to
`max_cost` and reads at the scope that cap is enforced at: the current run's
spend including descendant runs chained under it (see
[[features/cost/measurement|measurement]]). It is blank for a node that has
never run — no reading, as distinct from a spend of $0 — and rounded to cents, a
steering read rather than an invoice. The `last` column is the age of each
node's newest activity, and a `!` flags an active node that has been quiet past
`max(step_timeout, 5m)` — the first sign of a hang. `--live` re-checks reality:
it relabels a crashed active node (no tmux session) as `exited` and drops nodes
whose worktree is gone.

**History and spend.** `fractal node activity` shows a node's lifecycle activity
most recent first, each row with its own-node step cost.
`fractal node cost spent` totals a subtree's spend (children included),
`cost remaining` shows what's left of the run's cap, and `cost breakdown` splits
it per node. Budgets and their semantics are
[[configuration/_index|configuration]]-branch territory; operationally, watch
for a child burning far faster than its siblings — that is the node to rein in
before it trips a subtree cap.

**The cockpit.** `fractal open` launches the TUI — a live tree view with
per-node panes for output, radio, and lifecycle actions (see the
[[features/tui/_index|features/tui/]] branch for the surface itself). It is the
ambient way to watch a tree; the CLI commands above are the scriptable way.

**Raw output.** `fractal node attach <node>` attaches to the node's tmux session
to watch the loop verbatim. Detach freely; the session outlives your terminal.

**Radio.** Nodes narrate their work on their outbox channel — progress,
decisions, blockers (the full surface:
[[features/radio/_index|features/radio/]]). `fractal radio feed` lists what your
subscriptions (your children, by default) have posted;
`fractal radio read --feed --unread` prints the new bodies;
`fractal radio thread <uuid>` reconstructs a conversation. What needs your
attention — replies to your messages, questions, decisions you own, finish
sign-offs — lands in your inbox instead: `fractal radio messages` lists it,
`fractal radio read --channel=inbox --unread` prints the new bodies. A silent
node is a suspect node: check its outbox first, then its tmux session.

## Steering

Steering tools, ordered by weight:

- **Radio directives.** A `fractal radio send "<directive>" --node=<branch>`
  (with its required `--subject` and 0-10 `--priority`) drops a message in a
  node's inbox. Nodes read their inbox at the sync pass before every step and
  treat parent directives as priority work. This is the primary steering tool:
  course-corrections, questions, priority changes. Reply to a node's question
  with `fractal radio reply <uuid>` so the answer lands in the asking thread
  (routing and reply semantics: [[features/radio/routing]],
  [[features/radio/reactions_and_replies]]).
- **NODE.md edits.** When a node's overall direction needs recalibrating, edit
  its NODE.md — revise the Instructions or Completion Requirements. The node
  re-reads its contract every iteration, so edits take effect at the next
  iteration boundary without a restart.
- **Chat.** `fractal node chat <node> "<prompt>"` (contract:
  [[features/chat/addressing]]) sends one prompt to the node's agent and streams
  the reply — by default in a fresh session, with `--current` forking the node's
  live loop session so you can interrogate its working context without
  disturbing it. Chat is for asking, not tasking: it is legal even on a paused
  node, and it changes nothing the loop will see unless the agent writes
  something down.
- **Retunes.** `fractal node update <child>` changes a child's caps and limits
  (cost caps, step timeout, depth/children/descendants), confirming each change
  old → new; a running loop picks the new values up at its next iteration.
  `fractal node config get/set` reads and writes raw config keys.
- **Approvals.** Steps can be gated on parent approval: `fractal node pending`
  lists direct children's steps waiting on you, and
  `fractal node approve <child>` releases one. A gated child parks until you
  act.
- **Lifecycle verbs.** The heavy end: `fractal node stop` (end after the current
  step), `finish` (end after the current iteration), `pause` (freeze the subtree
  in place), `kill` (immediate teardown of the node's processes and tmux session
  — the emergency stop, always available). Their semantics and when each is
  right live in [[user_flow/finishing/_index|user_flow/finishing]] and
  [[user_flow/continue_resume]].

## The operator's cadence

A healthy rhythm: scan `fractal node list` (or keep the TUI open), read the
radio feed and your inbox, answer anything addressed to you, and intervene only
on signal — a `!` in the list, a silent outbox, a spend outlier, a directive
gone unacknowledged. Nodes are briefed to surface blockers and questions on
radio and to keep working rather than wait; the operator who answers promptly
and steers with small directives gets the most out of a tree.
