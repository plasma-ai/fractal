---
name: design/durability
desc: |
  Why a paused node stays active-like everywhere but execution, why a parked
  loop with no tmux session is a normal state rather than a crash, what the
  pause and resume events credit back to deadlines, and why a tree-wide
  pause latches the root.
created: 2026-07-21T04:48:14Z
updated: 2026-07-21T04:48:14Z
---

# design/durability

[[_index|..]]

***

Pause exists so that an operator can freeze a running tree — to reclaim the
machine, to intervene, to move the repository to another host — and later get
back *exactly* the run they froze: same budgets, same iteration count, the
interrupted step re-entered, the agent session resumed where one was recorded.
Every design choice below serves that round-trip guarantee.

## Paused is active-like everywhere but execution

A paused node stops executing and nothing else. Its run and iteration rows stay
open for resume to adopt; it keeps its spawn slot in the tree's limits; it
blocks an ancestor's finish-drain the same way an active child would; and only
resume, kill, chat, and merge (committed work only; the node stays paused) are
legal on it. The alternative — treating pause as a lightweight exit — would make
the freeze lossy: closed rows would force resume to fabricate a fresh run (new
budget arming, reset iteration numbers), a released slot would let a sibling
spawn into space the frozen node still owns, and an ancestor draining "done"
children over a paused one would complete over work that is merely suspended.
Pause is a promise the tree makes to the future resume, and the promise only
holds if the rest of the system keeps behaving as though the node were still
there — because it is.

The ordering of the fan-outs encodes the same promise. Pause sweeps parent-first
— the inverse of every other signal — so a parent is never left running long
enough to drain-complete over children that are about to freeze; the sweep
re-enumerates until the subtree is fully signaled, catching children spawned
mid-fan-out. Resume relaunches leaf-first, so every child reads active again
before its parent's drain-waits can look at it and draw a wrong conclusion.

## Park semantics: no session is the normal parked state

Parking is how the freeze is made durable. The pause signal lands *before* the
in-flight agent invocation is aborted, so the loop reclassifies the killed step
as paused rather than failed — a failure would force-commit and open a fresh
session, destroying the very state pause is trying to preserve. The loop then
exits its process entirely, leaving no tmux session, and stamps the node paused.
Nothing is committed at the park: the dirty worktree *is* the frozen
mid-iteration state, and resume continues from those exact bytes.

This makes "no session" ambiguous — it is also what a crash leaves behind — and
the status model resolves the ambiguity by fiat: a missing session is proof of a
dead loop only for an *active* node, which crash-reconciliation heals to exited.
A paused node is never healed, on the original host or after a filesystem
transplant to another, because sessionlessness is its normal, intended
condition. Parking as a full process exit is what makes pause survive anything
short of losing the repository: there is no live process to keep alive, so a
reboot, a host migration, or a week of sitting idle all cost nothing.

Resume adopts rather than restarts: the open run row anchors the budgets and
numbering, the interrupted step is re-entered (resuming the recorded agent
session when one exists, re-orienting fresh otherwise), and a node caught still
parking — active with a pending pause signal — simply has its pause withdrawn so
the live loop never parks at all.

## What pause and resume events credit back

Cost needs no crediting — a parked node spends nothing, and spend is recorded,
not accrued by the clock. Deadlines do: run and iteration timeouts are wall
clock, and a raw reading would charge the frozen days against them, so a run
paused overnight could wake already timed out. The pause and resume events are
the correction record: each pause-to-resume span is summed and credited back to
the run and iteration deadlines, clipped to the scope's own window, with an
unmatched trailing pause accruing to now because the node is still parked.

The edges of the walk are chosen for honesty. A failed resume never closes a
span — the loop never relaunched, so the node is still frozen and the clock must
stay stopped. A failed pause still opens one — the signal is durable and the
loop parks at its next checkpoint anyway. Steps need no crediting at all, by
construction: a step never spans a pause, because the interrupted step's row
closes paused and resume opens a fresh one. The same instants double as the
substrate for pause-aware cost attribution, which is why they are recorded as
durable events rather than in-memory loop state.

## Tree-wide pause latches the root

Pausing the whole tree from the user (root) node cannot work by fan-out alone.
The root has no loop of its own to park, and a fan-out is a sweep over nodes
that exist — a spawn or start racing the sweep would slip new, unfrozen work
into a tree the operator believes is stopped. Depth-1 nodes make the gap
structural: they have no pausable ancestor whose frozen status could refuse
them.

So a tree-wide pause latches the root *first* — a marker beside the central
database — and only then fans out. Spawns, starts, and booting loops all check
the latch and refuse while it is set, so even a race that beats the sweep lands
on a closed door. The latch is also why "nothing was active to pause" is still a
meaningful operation: the tree is latched until the tree-wide resume releases
it, which it does before relaunching anything, making new work legal again the
moment the release begins. A targeted resume under a still-paused ancestor
refuses for the same reason the latch exists: a frozen subtree admits no new
work, and waking a leaf inside one would breach the freeze from below.

Structural detail — the lifecycle commands, the status machine, and the loop's
signal checkpoints — lives in [[architecture/_index|architecture/]] and
[[features/lifecycle/_index|features/lifecycle/]]; the status model's side of
pause is on [[design/statuses]].
