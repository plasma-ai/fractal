---
name: user_flow/continue_resume
desc: |
  The two ways to interrupt and re-enter a run: stop and continue end a run
  at a clean boundary and later arm a fresh one, while pause and resume
  freeze an open run in place and thaw it, including tree-wide.
created: 2026-07-21T04:47:43Z
updated: 2026-07-21T04:47:43Z
---

# user_flow/continue_resume

[[_index|..]]

***

Fractal has two interrupt-and-re-enter pairs with different guarantees.
**Stop/continue** ends a run cleanly and later starts a fresh one: boundaries
are respected, but run state (budgets, iteration count, the agent's working
context) starts over. **Pause/resume** freezes the run mid-flight and later
thaws exactly that run: everything is preserved, nothing concludes. Choosing
between them is choosing what you want to survive the interruption.

## Stop, finish, kill — ending a run

Three verbs end a run, differing in how much of the current work they let land:

- `fractal node finish` — after the current **iteration**: the iteration's
  commit lands first. The graceful conclusion ([[user_flow/finishing]]).
- `fractal node stop` — after the current **step**: the loop exits at the next
  step boundary, status `stopped`. Both signals fan out to active descendants
  children-first; from the user node they broadcast tree-wide.
- `fractal node kill` — **immediately**: the node's process group and tmux
  session are reaped, in-flight agent included, status `killed`. The emergency
  stop; anything uncommitted stays in the worktree.

All three land a settled status, and settled nodes are continuable.

## Continue: a fresh run

`fractal node start --continue <node>` re-arms a node whose last run ended — it
accepts statuses `completed`, `stopped`, `exited`, and `killed` (a paused node
must be resumed instead, and a retired one unretired). What "fresh run" means:

- **Runs are isolated.** Each launch arms the cost cap anew and starts new
  run/iteration accounting; the node's history keeps every prior run.
- **The worktree is restored.** The launch restores the worktree to its
  committed state; if uncommitted project files exist (a kill usually leaves
  some), the continue refuses until you pass `--clean` to acknowledge discarding
  them.
- **A budget-ended run never re-arms silently.** If the last run ended by
  exhausting its cost cap, a bare `--continue` refuses, naming the spent and
  armed figures; pass an explicit `--max-cost` to arm the next run deliberately.
  The retune is applied before launch and echoed old → new.
- **Spawn gates re-check.** A continued node re-enters the tree's
  width/descendant accounting as at spawn, so it is refused over a cap.

Continue is the right tool when the interruption was a conclusion: the budget
ran out and you are topping it up, the node stopped and you have re-briefed it
via NODE.md, or you killed it and want a clean re-entry. The node re-orients
from its NODE.md, memory, and radio archive — its private notes are how context
crosses runs, not the agent session.

## Pause: freezing in place

`fractal node pause [--reason]` freezes a subtree without concluding anything
(why this durability shape: [[design/durability]]; the command contracts:
[[features/lifecycle/commands]]). The signal fans out parent-first (the inverse
of every other verb, so a parent parked early can never drain-complete over its
still-live children), and each node's in-flight agent invocation is aborted; the
loops reclassify the abort and park with status `paused`. What paused preserves:

- The **run and iteration rows stay open** — nothing ends, nothing commits.
- The node **holds its spawn slot** and still blocks its ancestors'
  finish-drains: the tree shape is exactly as it was.
- The **pause latch**: a paused subtree admits no new work — spawning under it,
  starting into it, and resuming a descendant while an ancestor is frozen all
  refuse until the ancestor resumes.
- Legal verbs on a paused node: `resume`, `kill`, and `chat` (you can
  interrogate a frozen node). Merge, delete, retire, and start all refuse.

## Resume: thawing

`fractal node resume <node>` relaunches the parked loops leaf-first — every
child reads `active` again before its parent's drain-waits can look — and each
loop **adopts its open run** where the pause left it: same budgets, same
iteration count, the interrupted step re-entered (resuming the recorded agent
session when one exists, re-orienting fresh otherwise), and the run and
iteration deadlines credited for the paused span, so a pause never costs
wall-clock budget. A node caught still parking gets its pause withdrawn instead
— the live loop simply never parks.

## Tree-wide pause and resume

`fractal pause` and `fractal resume` (no `node`) are the whole-tree brake and
release, run from the root. A tree-wide pause latches the root first — a marker
beside the central database — so even a depth-1 spawn or start racing the sweep
refuses; then it parks every active node. Resume lifts the latch and relaunches
leaf-first. This is the tool for machine shutdowns, stepping away from a large
tree, or halting spend instantly without losing any node's place.

## Choosing

Reach for **pause/resume** when you intend to come back to exactly this work:
mid-iteration state is valuable, the interruption is external (a reboot, a
meeting, a spend freeze), and nothing about the task is changing. Reach for
**stop/continue** (or finish, or kill) when the run should end: the direction is
changing, the budget needs re-arming, or you want the node to re-orient from a
clean boundary rather than thaw mid-thought.

## Resume context and drains

A resumed iteration replays a frozen plan, so the harness re-reads the node's
unread inbox and appends the digest (metadata, priority first) to every seat's
prompt -- directives that arrived after the plan froze are in context before any
replayed decision executes, and a sealed mailbox stays sealed. For wind-downs,
`node start --continue --drain` runs a drain: `_DRAIN` rides every seat's
environment, `node init` (spawns and whole new trees alike), `node start`,
`node update`, and `node resume` refuse under it (spawns, re-arms, and subtree
wake-ups are harness-blocked, not just discouraged), and the DRAIN mode doc
tells the seat to close out instead of expanding. The refusal reaches
`node _loop` too -- the re-arm primitive those four verbs front, and what
`start.sh` actually execs -- so the front doors are not locked over an open back
one. The drain run's own relaunch after a park is exempt: it names the parked
seat itself, which no other re-arm does.
