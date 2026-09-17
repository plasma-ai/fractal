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
database. Cost-reporting providers flush their stream's figures immediately --
the reader can die at any moment, and an already-flushed figure survives.
Providers that require complete invocation evidence defer their first price
until that evidence validates after process exit. Attribution is therefore
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

Codex is priced from the per-response `token_usage_record` rows it writes to its
session log (`CODEX_HOME/sessions/.../rollout-*-<thread>.jsonl`), not from its
`exec --json` total, which on a resumed thread covers every earlier invocation.
Before spawning, the backend captures the log's state -- the files present and,
for a resume, the thread's file identity, byte length, content hash and
cumulative counter -- and once the process exits `0` it streams the thread's log
once (hashing the captured prefix and decoding only the record kinds the window
reads, so a long-lived thread costs one pass over its bytes, never its parsed
history), sums the rows appended since, deduplicated by response id, checks that
the window holds exactly one completed turn on the reported thread and that the
sum equals the thread counter's growth, and prices the sum at the rates of the
served model the window's `turn_context` names (cached input at its cache-read
rate; a mismatch with the pin is the loop's model-drop check to judge, never a
reason to refuse). A step that fails any of those checks -- a missing, replaced
or rewritten log, an interrupted or doubled turn, another thread's records, a
sum the thread counter disputes, an unpriced model, a turn that spawned or drove
sub-agent threads (each child writes its own log, whose usage the window never
sums, so the parent's figure alone would be partial), or a log with no
per-response records at all (codex older than 0.153 writes none) -- records
`NULL` cost and logs `codex usage unpriced: <reason>` at warning level on the
agent's logger, while a step whose process exits non-zero records `NULL`
silently: the exit is the loop's to attribute. An earlier interrupted turn need
not be complete -- its captured counter is the resume baseline, and its own step
stays unpriced. A child log that predates the spawn is bound by its captured
length: left untouched, the thread's next turn prices as usual; appended to (a
child the turn drives again), it refuses the step like a new one.

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
  stream opened, or an untracked agent's rows -- or whose invocation evidence
  did not validate (the codex attribution above;
  [[features/cost/budgets|budgets]] covers how armed caps read such rows). Ended
  rows only: an open step is merely not priced *yet*.
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

An open step's row carries only what its provider has established. Codex keeps
the row unpriced throughout execution and publishes no partial accrual. A
terminal node can still have unresolved costs; terminal status alone does not
complete missing accounting evidence. A token-priced agent with no model set is
priced at the served model its session record names.
