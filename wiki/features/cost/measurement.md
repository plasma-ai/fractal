---
name: features/cost/measurement
desc: |
  How spend is measured and attributed: cost figures flow from agent streams
  into per-step ledger rows in the central database, roll up through
  iterations and runs, and include descendant nodes through the per-run
  subtree chain. Unknowable cost is recorded as null and disclosed, never
  conflated with a genuine zero.
created: 2026-07-21T04:49:55Z
updated: 2026-07-21T04:49:55Z
---

# features/cost/measurement

[[_index|..]]

***

Every dollar fractal accounts for lands on a **step row** in the central
database. As an agent invocation streams, each cost figure the stream yields is
flushed to the step's row immediately -- the reader can die at any moment, and
an already-flushed figure survives. A final result frame settles the figure;
until then the row carries the running accrual. Attribution is therefore
step-granular: iterations and runs never store their own cost, they are sums
over their steps.

## Two cost modes

Providers differ in how a figure is obtained (the seam lives in
`fractal/core/agent.py`, with one backend per module in `fractal/impl/`):

- **Cost-reporting agents** carry authoritative dollar figures on their own
  stream; fractal records what the provider reports.
- **Token-priced agents** report token usage only; fractal prices it through the
  LiteLLM table (see [[features/cost/pricing|pricing]]). If the model is absent
  from the table, the step records `NULL` cost -- unknowable, never `$0`.

Cost figures can cover one invocation or a cumulative thread-scoped total.
Fractal settles a thread-scoped total into a per-step delta by subtracting the
amounts already recorded for earlier steps of the same session. A settled figure
is floored at zero: a provider-side credit or accounting anomaly never records
negative spend.

Codex reports usage for one invocation, including when `exec resume` continues
an existing thread. Cached context can carry across invocations, but the usage
total starts fresh. Each step records its whole priced usage, and the run's
spend is the sum of those independent invocation costs.

## Rollups and the per-run subtree

`fractal node cost spent` sums step rows for the **current run** by default (the
active run, else the most recent) -- budgets and spend are per-run, so a drained
prior run never bleeds into the bare reading (`--run` scopes to a specific run;
`--iter`/`--step` scope to one iteration's or step's rows, without children).

The run scope includes descendants: every run a child spawned under this run's
lineage is chained to it, hop by hop, and the whole chain -- the **per-run
subtree** -- is what `spent` totals and what budget enforcement reads. A deleted
child's recorded runs still count; history outlives the registry. `--max-depth`
bounds the walk (`0` is this node alone, `1` adds direct children).

`fractal node list`'s `spend` column is this same reading, one row per
descendant, so it compares directly against the `max_cost` printed beside it --
the listing would mislead if the two columns disagreed on scope. It rounds to
cents and stays blank for a node with no recorded runs, which is not the same
claim as a spend of `0`.

`fractal node cost breakdown` renders the same lineage as a per-branch table:
the target's own row leads with its cap, each still-registered descendant
follows (idle children read `0.00`), and any lineage descendant whose registry
row is gone is appended as a ` (deleted)` row -- so the table always sums to
`cost spent`. A deleted *target* answers from its persisted history over its
latest recorded run, with no cap (every cap store dies with the node).

## Untracked and unpriced spend

Zero and unknowable are never conflated:

- A step the loop closes without ever reaching its launch records an explicit
  `$0` -- a knowable nothing-spent, never `NULL`.
- `NULL` cost is reserved for unknowable spend: a launched step whose usage
  never flushed -- a kill before the first flush, a post-launch death before the
  stream opened, or an untracked agent's rows. Ended rows only: an open step is
  merely not priced *yet*.
- `cost spent` prints `untracked` instead of `$0.0000` when the scope has
  `NULL`-cost steps and no step recorded a real figure. Zero-cost rows are
  neutral -- they prove nothing was spent, not that spend was tracked -- so they
  never flip an otherwise-untracked scope to `$0`. The run scope walks the
  subtree, so a fully-untracked child reads as untracked at its parent; a mixed
  subtree -- any real figure -- is tracked.
- Ended steps with `NULL` cost are silently skipped by the sum, so ledger-facing
  commands disclose the count on stderr
  (`N unpriced steps (NULL cost) excluded`) while stdout stays parseable.

Codex's model-acceptance preflight runs before step rows exist. Its usage is
outside the step ledger and is not included in the recorded run spend.

## Finality

An open step's row carries only what has already been flushed, not the
in-progress accrual -- so an active node's figure is always a floor, and cost
figures are final only once the node reaches a terminal registry status. The
loop's startup preflight warns when a token-priced agent runs with no model set
and no caps configured: its spend would silently go untracked.
