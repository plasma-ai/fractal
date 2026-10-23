---
name: features/cost/time_budgets
desc: |
  Time budgets: the run, iteration, and step timeout tiers, how remaining
  time is derived from configured limits and row start instants, and the
  pause credit-back that keeps frozen time from charging against run and
  iteration deadlines.
created: 2026-07-21T05:07:57Z
updated: 2026-07-21T05:07:57Z
---

# features/cost/time_budgets

[[features/cost/_index|..]]

***

Time budgets bound wall-clock the way [[features/cost/budgets|budgets]] bound
dollars: three configurable timeout tiers — the whole-run `timeout`, the
per-iteration `iter_timeout`, and the per-step `step_timeout` — each read from
node config and enforced by the loop. All are optional; an unset tier simply has
no deadline.

## Tiers and defaults

- **Run** — the `timeout` config key caps the whole run, anchored on the run's
  start instant.
- **Iteration** — `iter_timeout` caps each iteration. When it is unset but
  `interval` is configured, the interval becomes the per-iteration deadline: an
  interval-driven loop bounds each iteration to its slot. An explicit
  `iter_timeout` tighter than the interval is honored; one looser than the
  interval is rejected at the config boundary rather than silently loosened.
- **Step** — `step_timeout` caps each step. A step definition can carry its own
  `timeout:` frontmatter override, so one long step (a slow test suite) can
  exceed the node-wide per-step limit without loosening it for the rest. Every
  report of a step's limit follows that step's effective ceiling — the override
  when it parses, else `step_timeout`: the step part of `$TIME_BUDGET` (rebuilt
  for each step), the `step` scope of `fractal node time remaining`, and the
  `node list` stale flag.

`interval` and `sleep` pacing are mutually exclusive; only `interval` doubles as
an iteration deadline.

## Reading remaining time

`fractal node time remaining [node] [--scope=run|iter|step]` prints the whole
seconds left before a timeout fires. With no `--scope` it reports the soonest of
the configured deadlines — the time until the *next* timeout, whichever tier it
comes from. The command answers for any node, not just the running one: each
deadline is derived from the configured limit plus the persisted start instant
of the scope's active row (the open run row, the active iteration row, or the
active step row), mirroring how cost remaining is read from persisted state. Two
sentinel outputs replace the countdown: `no limit` when the queried scope(s)
have no timeout configured, and `not running` when a limit exists but no run,
iteration, or step is currently active to anchor it. The countdown clamps at
zero — an overrun deadline reads `0s`, never negative.

The step limit is the ceiling the active step's launch enforces, which the loop
records on the step row as it resolves it: the step file's `timeout:` line
substituting for `step_timeout`, by the rule the loop applies at launch, or `-1`
when the launch has none. The loop reads step files once per iteration, so an
edit made mid-iteration takes effect at the step's next launch, and every reader
reports the recorded ceiling until then. The node's own seat reads the same
ceiling from its exported `STEP_TIMEOUT_SECONDS` when its exported `STEP_ID`
names the active step. A row with no recorded ceiling — opened before the
database gained the column, or whose stamp failed — resolves its step file
instead. Its name locates the file in the node's `steps/`; a SYNC row at step 0
or under sync mode is the built-in pass, which reads the package's mode doc. A
name that several files share is told apart by occurrence: the earlier step
numbers of the iteration carrying the same name count the same-named files ahead
of it, which holds while only differently-named files change. An idle node has
no current step, so its step limit is `step_timeout`, else the tightest step
file's own `timeout:`, and a node with either reads `not running`, as the run
and iteration scopes do. The cockpit's step time ceiling reads the displayed
step's recorded ceiling the same way, and its step clock carries the same steer
charge as the countdown below.

The step countdown starts where the loop's step deadline does. A steered
relaunch opens a row of its own, but the loop charges the stuck launch's span
against its ceiling, so the countdown adds the span of the step's previous row
in the iteration when that row closed with a `steered:` reason. An attempt buys
at most one steer, so the chain is one row long. A retry or a model-drop
re-dispatch arms a fresh deadline in the loop and counts from its own row.

## Pause credit-back

Pausing a node parks the loop while leaving the run row (and any open iteration)
open for a later resume to adopt. Raw wall-clock elapsed since the row's start
would therefore charge the entire frozen span against the run and iteration
deadlines — a node paused overnight would wake already timed out. Instead,
deadline accounting credits paused spans back: the park and resume events
recorded on the run carry point-in-time instants, and the elapsed time used
against a deadline subtracts every park→resume span (clipped to the scope's
anchor, so an iteration deadline credits only the frozen time inside that
iteration). A trailing park with no resume yet accrues credit up to the present
— a still-parked node's clock stays stopped.

A span opens when the loop parks, never at the pause request. A step in flight
can work on past the request — an agent that pauses its own node finishes its
step before the loop parks — and that working time is charged like any other.
The loop records its own `pause` event as it parks, at a pause checkpoint, at a
boot the pause latch parks, or at a resume boot whose preflight fails; the walk
tells these from the pause verb's request by their reason (`parked`,
`via pause latch (...)`, `resume preflight failed: ...`), which the loop writes
from shared constants. Both pause verbs refuse an operator reason that uses that
text, a prefix even without its trailing space (a fanned-out reason gains a
`(via pause of ...)` suffix), so a request row is never read as a park. A resume
that withdraws a pending pause before any park therefore credits nothing.

Edge semantics follow the event log's meaning: a failed resume never relaunched
the loop, so only a completed resume closes a span; duplicate parks collapse
onto the first and duplicate resumes are inert. Steps need no credit-back at all
— a step never spans a pause, because the interrupted step row closes as paused
and resume opens a fresh step row with a fresh deadline.

## Interaction with cost budgets

Time and cost budgets are independent guards on the same loop: an iteration ends
at whichever boundary arrives first, and the loop's banner reports both the
timeout configuration and the cost caps. Time budgets have no reserve tier —
wind-down under the cost reserve is described in
[[features/cost/budgets|budgets]].
