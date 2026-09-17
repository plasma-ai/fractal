---
name: design/budgets
desc: |
  Why cost ceilings are soft and checked at boundaries rather than enforced
  by hard kills, why every run keeps a reserve as its wind-down window, how
  a finish landed in the reserve books its terminal status, why sync is a
  billed step, and how the run, iteration, and step cap tiers divide the
  enforcement problem.
created: 2026-07-21T04:48:14Z
updated: 2026-07-21T04:48:14Z
---

# design/budgets

[[_index|..]]

***

Fractal's cost model starts from one premise: a node's work product is only as
good as what it lands. A budget mechanism that kills an agent mid-thought
strands uncommitted work, half-written state, and an unmerged subtree — the
money is spent either way, but a hard kill forfeits the deliverable too. Every
budget design choice below trades a little overshoot for a landed result.

## Soft ceilings, checked at boundaries

`--max-cost` is a soft, per-run ceiling over the run's whole subtree — the
node's own steps plus every descendant run spawned under it. The loop never
interrupts an in-flight step because spend crossed a line; it checks recorded
spend at natural seams instead: at each iteration boundary, and between steps
inside an iteration (so a spawn-heavy iteration stops queuing further steps once
the subtree ceiling trips, rather than waiting for the boundary).

Boundary checking follows from how cost is known. Spend is *recorded, never
estimated*: a step's cost lands in the ledger when the agent's usage flushes, so
mid-step the loop cannot see the accrual anyway — a "hard" mid-step stop would
fire on stale data. The ledger is also honest about ignorance: a step that
recorded no cost reads as `NULL`, and readings distinguish *untracked* from a
genuine `$0`, so enforcement never mistakes an unpriced agent for a free one —
but it also never invents a per-step estimate to kill against.

The one hard element is delegated, not improvised: agents that accept a per-step
budget flag get a leash — the smallest of the run's remaining minus the reserve,
the iteration's live headroom, and the per-step cap — enforced by the agent
itself inside the step. For agents with no such flag the caps stay soft, and the
loop warns once per loop process when soft caps are armed with no timeout,
naming the step timeout as the available in-step brake. This split keeps the
guarantee proportional to what each provider can actually enforce, instead of
pretending one mechanism covers all.

A budget stop is a *designed* landing, not a failure: the run closes `exited`
with exit code `0`, distinguishing "ran out of budget with work possibly
unfinished" both from a goal-met `completed` and from an abnormal death (see
[[design/statuses]]). An overshoot past the ceiling is the parent's to mitigate
— the parent watches child spend and retunes caps — which is the management
model the soft ceiling assumes.

## Reserve budgets as wind-down

If the ceiling were the only line, a run would work at full tilt until the last
dollar and have nothing left to land state with: no final commit, no memory
update, no handoff. So every capped run keeps a reserve — by default ten percent
of the cap, applied however the cap was set (a config edit that bypasses the
launch flags still gets it), so a reserve-less run takes an explicit decision to
zero the reserve, never an oversight.

When remaining budget drains into the reserve window, the loop flips the running
iteration into wind-down: remaining steps are told to land state rather than
start new work, and at the iteration's boundary the run ends — starting another
iteration would only re-enter the reserve (the enforcement mechanics:
[[features/cost/budgets]]). The per-step leash cooperates: inside the window it
floors at the full remaining rather than going non-positive, so wind-down steps
can spend the reserve but never past the ceiling. The reserve is thus not slack
— it is the budget explicitly priced for the ending: commit, memory, radio
handoff, the parts that make a budget-ended run resumable and mergeable instead
of a stranded worktree.

How the ending books depends on why the finish was sent. A deliberate, goal-met
finish landed during wind-down keeps its `completed` — even when the final drain
carried spend past the cap, in which case the overshoot figures ride the run row
so the activity record explains the spend. A budget-stemmed finish books the
`exited` budget landing instead, and a deliberate finish outranks budget rows
whichever order the signals arrived, so the same inputs book the same terminal
status either way round. When the signal store cannot be read, an over-cap
finish reclassifies as a budget stop — the landing never fails open into a
goal-met `completed`.

A budget abort also cascades: the tripping loop signals finish recursively, so
descendants wind down too rather than spending on toward a parent that can no
longer integrate them, and each descendant records the abort honestly as an
ancestor's rather than claiming a goal-met completion.

## Why sync is a billed step

The sync pass that runs before each step (see [[features/loop/steps]]) is a real
agent invocation — it reads radio, replies, and steers — and real invocations
cost real tokens. Sync therefore books its own step row with its own cost, and
subtree spend includes it. Anything else would corrupt the ledger the boundary
checks read: a node whose coordination overhead were invisible would look
cheaper than it is, overshoot its cap by exactly its communication volume, and
hand the parent a spend table that cannot explain the bill. Billing sync also
lets the budget machinery govern it — a sync that would exceed the leash is
skipped and booked as a labeled budget stop, not silently run over the cap or
misfiled as a failure.

## The cap tiers

The three cap tiers bound different failure modes, which is why they compose
rather than replace each other:

- **Per-run** (`--max-cost`) bounds the total: the whole subtree, arming fresh
  each launch because runs are isolated — a drained budget in one run says
  nothing about the next operator decision to relaunch.
- **Per-iteration** (`--max-iter-cost`) bounds pace: one runaway iteration
  cannot eat the run. Enforcement uses the iteration's *live* headroom — its cap
  minus recorded spend — so a later step never re-receives the full
  per-iteration allowance.
- **Per-step** (`--max-step-cost`) bounds the overshoot unit: the largest amount
  an in-flight step can spend past every boundary check, since boundaries only
  see steps that ended. It is the tier handed to the agent as a hard leash where
  the provider supports one, and warn-only where it does not.

The run budget is subtree-shared with no reserved self-slice — deliberately,
because slicing it would presume how a manager divides labor. The cost of that
freedom is a real failure mode: a manager that sizes children to its full
remaining budget starves itself out of its own integration iteration. The sizing
rule (children's caps plus ceremony plus one merge iteration must fit inside the
manager's remaining) lives in the fractal skill's guidance rather than in
enforcement, matching the overall stance: budgets steer, parents manage, kills
are last resorts.

Time budgets mirror the same tiers (run, iteration, and step timeouts) and are
the harder walls — a deadline can safely interrupt where a cost probe cannot,
because time is observable continuously and cost is not. Structural detail of
the cost machinery lives in [[architecture/_index|architecture/]] and the cost
feature pages under [[features/cost/_index|features/cost/]].
