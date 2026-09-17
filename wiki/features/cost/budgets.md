---
name: features/cost/budgets
desc: |
  The cost budget cap tiers and their enforcement: the per-run subtree-shared
  run cap, the per-iteration and per-step caps, the reserve window that
  steers wind-down before the ceiling, and how remaining headroom is read.
created: 2026-07-21T04:49:55Z
updated: 2026-07-21T04:49:55Z
---

# features/cost/budgets

[[_index|..]]

***

Cost caps come in three tiers, all stored in node config and re-read at each
iteration top -- and again by the budget probes themselves -- so a mid-run
retune takes effect without a restart:

- **`max_cost`** -- the per-**run** ceiling. It bounds the run's whole per-run
  subtree ([[features/cost/measurement|measurement]]): the node's own steps plus
  every descendant run chained under this run. The budget is per-run: a budget
  drained in one run is fresh again in the next, and a budget-ended run re-arms
  only through `node start --continue --max-cost`.
- **`max_iter_cost`** -- a per-**iteration** cap over that iteration's own steps
  (children not included).
- **`max_step_cost`** -- a per-**step** cap. An agent that accepts a budget flag
  is launched with a per-step leash -- the tightest of the run's
  remaining-less-reserve, the iteration's live headroom, and this cap -- so the
  invocation itself bounds in-step overshoot; a non-enforcing agent's cap is
  warn-only, checked after the step ends.

`fractal node cost remaining` reads the matching headroom: the bare command
returns `max_cost` minus the current run's subtree spend; `--iter`/`--step`
scope to the per-level cap instead; `no budget` means the relevant cap is not
configured. The reading reflects recorded step costs only -- an active step
counts what is already flushed, not its unflushed accrual. Negative headroom
prints as `$0.0000`, and a deleted target always reports `no budget` (history
persists, caps do not).

## The run budget is subtree-shared

There is no reserved self-slice: a manager that sizes its children's caps to its
full remaining budget leaves itself nothing and can be starved out of its own
merge-up iteration. Size children below the remaining figure whenever the
manager must keep working after spawning.

## Reserve window and wind-down

`reserve_budget` (default 10% of `max_cost`, materialized to a fixed decimal
precision in config) is a buffer below the ceiling that steers cleanup before
the money runs out. During an iteration, the loop enters **RESERVE** when any of
these hold:

- the run's remaining budget drains into the reserve window (checked before each
  step, so entry can happen mid-iteration);
- the iteration's own spend reaches `max_iter_cost`;
- an ancestor's budget abort left a cascaded finish pending -- the reserve
  wind-down then runs on this node's last iteration too.

RESERVE turns the rest of the iteration into wind-down: land state, hand off,
finish -- no new build work. At the iteration boundary the loop checks the
reserve state again and, having just run the wind-down, ends the run rather than
start another iteration that would only re-enter reserve; this boundary stop
subsumes the hard ceiling. The finish it sends carries a budget-abort reason
(`cost budget reserve reached`, `subtree cost budget reached`, or
`cost budget exceeded in finish wind-down`), and a budget abort books a clean
`exited` run end with exit code `0` naming that reason -- a designed landing,
never a failure (see [[design/budgets]]).

Only loop-sent budget aborts land `exited`. A deliberate goal-met finish books
`completed` even when it arrives inside the wind-down and even when spend has
passed the cap -- the overshoot rides the run row so the spend stays explained
-- and a deliberate finish outranks a budget abort cascaded from an ancestor,
whichever order the two signals landed.

Spawn-heavy iterations get a harder backstop: between steps the loop also checks
the subtree ceiling directly, so a long iteration stops queuing steps as soon as
descendants blow the budget rather than waiting for the boundary.

Enforcement is only as good as the ledger: armed caps over fully-untracked spend
([[features/cost/measurement|measurement]]) can never trip, and a run that mixes
priced and unpriced steps stands against them undercounted, since a `NULL`-cost
step adds nothing to the guards' figure; the loop warns once per loop process in
either case. Caps paired with an agent that takes no budget flag and no timeout
draw the same one-time warning, since one runaway step could overshoot every cap
unbounded. A failed ledger read holds the last good figure rather than
re-inflating headroom, and before the run's first good reading it holds the
probes outright -- the reserve-entry probe included, so unknown spend never
reads as drained -- and the untracked warning waits for a successful read to
prove the spend untracked, never firing for a contention window.

## Preflight guarantees

A token-priced agent cannot run without pricing: the loop refreshes the LiteLLM
cache at run start and aborts preflight when no table can be fetched and none is
cached (a stale cache degrades to a warning -- see
[[features/cost/pricing|pricing]]). The refresh runs whether or not a model is
configured: a token-priced agent with no model set is priced at the served model
its session record names.
