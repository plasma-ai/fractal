---
name: features/lifecycle/status_machine
desc: |
  The node status set and the rules of movement between statuses: which
  statuses exist, which apply to nodes versus history rows, which signals
  are legal in each status, and how events record every transition.
created: 2026-07-21T04:58:40Z
updated: 2026-07-21T04:58:40Z
---

# features/lifecycle/status_machine

[[features/lifecycle/_index|..]]

***

## The status set

One status vocabulary, defined in `fractal.constants.STATUSES`, is shared by the
node's `.status` marker file and the database row tables: `active`, `paused`,
`idle`, `completed`, `stopped`, `exited`, `killed`, `failed`, and `retired`. Not
every status applies at every level:

- `failed` is entity-row only — a run that ends abnormally records `exited`,
  never `failed`.
- `idle` and `retired` are node-only: a created-but-not-running node is `idle`;
  a `retired` node is parked out of sight and cannot start.
- A user (root) node is marked by configuration, not by a status of its own — it
  never iterates.

Statuses split into **unsettled** (`active`, `paused`, `idle`) and **settled**
(`completed`, `stopped`, `exited`, `killed` — the statuses [[design/statuses]]
calls *terminal*). Unsettled nodes occupy capacity in the tree limits (see
[[features/spawning/tree_limits]]); settled and retired nodes do not. `paused`
is active-like everywhere except execution: the loop has exited, no tmux session
exists (that parked state is normal, never crash-healed), and the run row plus
any open iteration stay open for resume to adopt.

## Events beside statuses

Every transition writes a point-in-time event row: `init`, `spawn`, `commit`,
`approve`, `merge`, `delete`, `orphan`, `model_drop`, `start`, `finish`,
`finish_cancel`, `stop`, `kill`, `pause`, `resume`, `retire`, and `unretire`.
Not every event is a transition — `model_drop` records a step served off its
pinned model against that attempt's own row, leaving the node's status untouched
(see [[features/loop/steps|steps]]). Rows carry start and end instants
(durations are derived, never stored), and the `pause` / `resume` event instants
credit paused spans back to run and iteration deadlines. Refused signals also
leave evidence: a refusal writes a `failed` event row whose metadata states the
reason before the operation raises.

## Legal signals per status

- **`active`** accepts `finish`, `finish_cancel`, `stop`, and `pause` — each
  guarded by the same preamble: heal any crashed-but-active state first, then
  require status `active` and an open run. `kill` is also always legal on an
  active node.
- **`paused`** accepts only `resume`, `kill`, and `chat`. Everything else
  refuses; a paused node holds its spawn slot and blocks ancestor finish-drains
  until resumed or killed.
- **`idle`** is the parked, startable state: `start` runs it, `retire` hides it,
  `delete` removes it, and `kill` stamps it `killed` — a booting spawn is
  reaped, a never-started one can never activate.
- **Settled** nodes (`completed`/`stopped`/`exited`/`killed`) accept
  `start --continue` to re-arm, `retire`, and `delete`.
- **`retired`** accepts only `unretire` and `delete`; a retired node is hidden
  from listings by default and cannot be started.

When a signal fans out across a subtree, a node that fails its guard is skipped
silently rather than aborting the sweep, with the refusal evidence still
recorded and attributed to the originating verb and branch.

## Pause as a tree-wide latch

A pause of the user (root) node is tree-wide: before sweeping, it latches the
root by writing a `.paused` marker beside the central database, so even a
depth-1 spawn or start racing the sweep refuses until the tree-wide resume lifts
the latch. A non-user resume refuses under a paused ancestor for the same reason
— the latch is released top-down.

Details of each command's contract are in
[[features/lifecycle/commands|commands]]; the marker files and scripts behind
the transitions are in
[[features/lifecycle/script_delegation|script_delegation]].
