---
name: features/lifecycle/commands
desc: |
  The behavior contract of each node lifecycle command: creation, start,
  finish, stop, kill, pause, resume, retire and unretire, and delete —
  their guards, fan-out order across the subtree, and refusal semantics.
created: 2026-07-21T04:58:40Z
updated: 2026-07-21T04:58:40Z
---

# features/lifecycle/commands

[[features/lifecycle/_index|..]]

***

Lifecycle commands live under `fractal node` in the CLI, backed by the node
machinery in `fractal/core/`. Each pairs a database transition with a shell
script (see [[features/lifecycle/script_delegation|script_delegation]]); the
legal-signal matrix is in [[features/lifecycle/status_machine|status_machine]].

## Creation

`fractal node init` creates a child: a branch forked from the parent, a git
worktree, the node seed, and a registry row landing `idle`. Creation is where
the tree limits and the parent's budget are enforced — that gate is specified in
[[features/spawning/tree_limits]].

## start

`start` takes an `idle` node into `active`: it opens a run row and launches the
iteration loop inside a tmux session. `start --continue` re-arms a settled node
for another run — this re-entry re-checks the width and descendant caps, since
it returns one unsettled node to the tree exactly as a spawn adds one. A run
that ended on its cost budget never re-arms silently: a bare `--continue`
refuses, naming the spent and armed figures, until an explicit `--max-cost`
rides it (applied through the parent's retune — see
[[features/cost/budgets|budgets]]). A tree-wide pause latch makes any start
refuse until resume.

## finish and stop

`finish` and `stop` share one shape: both require an `active` node with an open
run (after healing any crashed-but-active state), both fan out over the subtree
children-first — descendants are swept before the node itself, so a parent never
completes over live children — and both let work land cleanly. They differ only
in granularity: after `finish` the loop stops at the end of the current
*iteration* (booking `completed`); after `stop` it stops at the end of the
current *step* (booking `stopped`). Both are queued rows the loop polls at its
boundaries — a stop landing mid-step waits for the in-flight seat to complete
and never tears it (`kill` is the immediate path), and both sweep the *entire*
subtree, not just the named node. `finish_cancel` withdraws a pending finish
before the loop honors it. Signaled from the user (root) node, finish is a
tree-wide broadcast with no self-signal, since the root runs no loop of its own.

Both sweeps re-enumerate to a fixpoint, the way `kill` and `pause` do: a single
pass covers only the descendants live when it began, so a child that stamped
`active` while the sweep signaled its sibling escaped with no signal row at all.
The remaining sliver — a child that boots after the last pass — closes at the
child's own end, mirroring how a booting loop parks itself under a pause latch:
`Node.cascade_latched` walks the ancestors for one still `active` with a pending
`stop` or `finish` (nearest first, `stop` outranking `finish`), and
`Loop._adopt_cascade` records it on the fresh run so the ordinary boundary check
honors it. The user node is skipped in that walk — its tree-wide broadcast
records no signal there is anything to adopt.

## kill

`kill` is the escape hatch: it targets `active`, `paused`, and `idle` nodes — a
booting spawn is reaped and a never-started one is stamped `killed` so it can
never activate — reaps the tmux session (when one lives), and closes open run
and iteration rows as `killed`. It is pure bookkeeping plus process reaping — no
graceful wind-down. The descendant sweep re-enumerates the subtree to a fixpoint
so children registered mid-sweep are still caught, and proceeds best-effort per
node.

## pause and resume

`pause` parks a subtree frozen-in-place. It fans out parent-first — the inverse
of every other signal — so a parent parked before its children can never
drain-complete over them; it aborts the in-flight agent, and the loop exits with
status `paused`, leaving the run and any open iteration rows open. A user-node
pause first latches the whole tree (a `.paused` marker beside the central
database), so even spawns and starts racing the sweep refuse.

`resume` is the mirror: it fans out leaf-first (deepest first) so children
already read `active` before their parent's drain-waits look at them. The
relaunched loop *adopts* the open run — same budgets, same iteration count —
re-enters the interrupted step (resuming the recorded agent session when one
exists, else re-orienting fresh), and credits the paused span back to run and
iteration deadlines. Resuming a node that is still parking withdraws the pending
pause instead; if the loop already read the signal it parks anyway, and the next
resume relaunches it. Resume refuses on a node that is not paused or pausing,
and refuses under a paused ancestor until that ancestor is resumed first.

## retire and unretire

`retire` shelves a non-running node: it refuses the user node, `active`, and
`paused` nodes. The pre-retire status rides in the retire event's metadata so
`unretire` can restore it exactly; a retired node is hidden from listings by
default and cannot be started. `unretire` refuses a non-retired node and
restores the recorded prior status, falling back to `idle` when the metadata is
unusable. Restoring to `idle` re-enters the unsettled pool, so it re-runs the
width and descendant gates just like a spawn; restoring to a settled status
holds no slot and passes ungated.

## delete

`delete` removes a whole subtree, deepest-first: each node's worktree, branch,
and remote branch go away, and its registry rows and subscriptions are cleared
from the central database — history rows persist. It refuses if any node in the
subtree is `active` (stop or kill first) or `paused` (resume or kill first),
refuses the user node, and refuses an improperly-initialized node rather than
guessing at cleanup. Warnings such as unmerged work are surfaced as notices
rather than blocking.

## Refusal semantics

All signal guards share one contract: reconcile crashed-but-active state first,
check the guard, and on refusal write a `failed` event row stating the reason
before raising. Inside a subtree fan-out the same refusal is recorded but
skipped silently — attributed as coming *via* the originating verb and branch —
so one ineligible node never aborts the sweep.
