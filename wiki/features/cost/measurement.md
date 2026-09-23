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
sum the thread counter disputes, an unpriced model, a sub-agent log the window
cannot bind to the turn (below), or a log with no per-response records at all
(codex older than 0.153 writes none) -- records `NULL` cost and logs
`codex usage unpriced: <reason>` at warning level on the agent's logger, while a
step whose process exits non-zero records `NULL` silently: the exit is the
loop's to attribute. An earlier interrupted turn need not be complete -- its
captured counter is the resume baseline, and its own step stays unpriced.

A sub-agent thread the turn spawns or drives writes its own log beside the
thread's, and that spend is the step's. The window maps every log new or grown
since capture that opens as a thread this one spawned (`session_meta` naming the
step's thread as its session, at any depth) and sums the rows each appended past
its captured length -- the counter there is the child's baseline, zeros for a
log the turn opened -- at the child's own served model's rates. A forked child's
log copies its source's history ahead of its own records; a log the turn opened
is read from the history start its metadata names
(`subagent_history_start_ordinal`), so a copied interrupted turn or turn context
is not the child's. A fork whose metadata lacks the ordinal, or carries one that
is not an integer or is negative, is read from its first line, so a copied
interrupted turn or another model's turn context refuses it, a refusal and never
a figure. An ordinal past the end of the log leaves nothing to read, and the
segment refuses as not ending on a completed turn; an ordinal inside the child's
own records drops part of them, and the segment refuses on its counter or its
turn events; an ordinal the reader cannot take at all refuses through the child
wrap with the log named. Each child log is summed at its own served model, and a
child the window cannot bind refuses the step: its rows must name the child and
the root and ride one of the parent window's turn ids, its segment must end on a
completed turn and record no interrupted one (codex pre-empts a child's turn
normally, so starts need not balance completions), it must carry counted rows on
exactly one served model, and their sum must equal the child counter's growth; a
child on an unpriced model refuses too. A driven child's log that is not the one
captured -- shorter than its captured length, or grown across an unterminated
line -- refuses, as does a mapped child log the window cannot open; every
refusal on a mapped child is logged as `Child rollout <file>: <reason>`. Two
rollouts new or grown since capture naming one spawned thread refuse the step as
`Several rollouts name spawned thread <id>: <file>`: one thread writes one
rollout. Three facts of exec mode are assumed, and each failure refuses rather
than misprices: a child turn never spans two `codex exec` processes, so a driven
child's new rows ride the driving turn; a resume appends nothing to a child it
does not drive, so a lone record grown onto one refuses; and a child whose
queued input never ran leaves an open turn, which refuses although nothing was
spent. Any other new or grown log the window cannot explain -- one not opening
with session metadata, or one naming a session the home does not hold as a
recognized root -- refuses the step as
`Rollout window saw a rollout it cannot explain: <file>`. Two rules hold at the
spawn check: a log naming the step's thread as its session refuses unless its
`source` carries a spawn object and its metadata names a string thread id other
than the step's own (so a sub-agent of another kind -- codex's `review`,
`compact`, `memory_consolidation` -- a plain-source log, or a copy of the step's
own log behind a spawn first line refuses), and a spawned thread named by two
such logs refuses. A sibling root's log (another `exec`, a chat with the node),
or any log naming that root as its session, whatever its own source, new or
grown, is skipped: its rows name the root, never the step's thread. A root is
recognized through the session a log names: the home must hold one rollout for
that session, opening with session metadata that names the session as its own id
and carries a plain string `source`. A session with no rollout or two, a rollout
whose metadata names another id or none, a `source` that is an object (`custom`,
`internal`) or absent, or a rollout the window cannot read is no recognized
root, and a log naming it -- whatever the log's own source -- refuses the step
as unexplained. A new log codex has opened but not yet written (an empty file)
is skipped as no record at all, while a captured log found empty, or one opening
with a blank line, refuses like any other the window cannot explain. A child log
untouched by a later turn adds nothing to that step. The step row's `model` is
the parent's served model, while its `cost` includes the child spend priced per
child; the loop's model-drop check judges the parent's model alone. The window
reads the dated `sessions/*/*/*/rollout-*.jsonl` tree alone: a child log codex
archives or writes at another depth is invisible to it, one whose session
metadata carries a plain string `source` (`exec`, `cli`) and names a root other
than the step's thread as its session is taken for a sibling root, and such a
step prices its own rows alone.

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
