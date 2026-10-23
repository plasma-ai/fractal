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
`codex usage unpriced: <reason>` at warning level on the agent's logger. A step
whose process exits non-zero closes its stream unpriced and silent -- the exit
is the loop's to attribute -- and is priced from its counters after exit
(below). An earlier interrupted turn need not be complete: its captured counter
is the resume baseline.

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

A codex step whose process exits non-zero -- a request refused because the
served model is at capacity, a crash, a deadline or other kill -- is priced
after exit by the codex settle hook (`CodexAgent._settle_cost`), through the
`Agent.settle_cost` call the loop makes for every launch (below). The stream's
finish keeps that launch's window, keyed by the thread the step row names (the
stream's, or the resumed one when the stream died before naming it), and the
settle reads it leniently (`UsageWindow.settle`). The rollouts bind to the
launch exactly as above -- the same file identity and prefix checks, the same
spawn and sibling-root rules, each refusal logged as
`codex usage unpriced: <reason>` and leaving the row `NULL` -- but the turn need
not complete, and an interrupted or refused one is no fault. The thread's spend
is its last `token_usage_record` counter less the counter captured at spawn, and
each spawned rollout's is the same past its captured length, each priced at the
one served model its `turn_context` records name; a rollout that grew nothing
needs no model and adds nothing. A child's rows must still name the child and
the root and ride one of the parent window's turns. A token count
(`token_count`'s `total_token_usage`) past the last usage record is a response
with no record -- codex older than 0.153 writes none -- and refuses the settle
rather than book that spend as free, as does a counter below its baseline. The
settle runs after the process exits and before the loop decides the retry, so a
failed attempt counts toward the run's spend, its caps and the step leash, and a
run of repeated provider failures can end on its budget instead of retrying
unseen. A clean exit keeps no window, and with `after` the settle adds nothing:
codex spends nothing past its result.

A failed codex step's record leaves part of a reply out when a rollout -- the
thread's or a sub-agent's -- ends on a request in flight. A request is in flight
from a turn's start (`task_started`), or from a `response_item` past the last
counter (a tool's result codex sends next, or a reply it was streaming), until
the next counter closes it, the turn completes (`task_complete`), or an `error`
record refuses it. A refusal such as the at-capacity error is not billed, so a
refused attempt reads whole; a kill mid-request books what the counters
recorded, marked `partly unpriced` (below). A window with a request in flight
and no counter growth prices nothing -- the row stays `NULL`, marked `unpriced`,
never a `$0` that would misstate an unknown spend -- while one with no growth
and no request in flight books a knowable `$0`. A response item is told by its
kind's quoted name and never decoded, like every kind the window skips; a
parallel tool call still running while a sibling's result is recorded reads as a
request in flight, the conservative side.

A Claude result frame's figure (`total_cost_usd`, or through openrouter its
`modelUsage` table priced through the alias chain) is the session's running
total. Claude Code saves that total to the session transcript as a `cost-state`
record (`totalCostUSD`, `modelUsage`, `startTime`, ...) as each launch exits,
and a `--resume` restores the latest one naming the session and counts on from
it, so a resumed launch's result names the session's earlier spend plus its own.
Before spawning a launch that resumes a session -- a continuous step after the
first, a steer, a SYNC, a resume -- the loop reads that **baseline** through
`Agent.cost_baseline` (hook `_cost_baseline`, `0.0` by default) from the
transcript the resume will append to: the latest record's `totalCostUSD`
natively, its `modelUsage` priced through the chain when routed, so the baseline
is in the figure's own measure. A transcript with no record (an older Claude
Code, which writes none and reports each launch's own spend) carries `0.0`. The
baseline rides `Agent.stream`'s `baseline` onto the parser
(`StreamParser.baseline`), which takes it off every result figure of the launch;
a reply after a result builds on the launch's own figure, and the after-result
settle adds to it. Claude stays `cost_scope = 'call'`: nothing settles against
the ledger's sibling rows.

A figure the baseline cannot settle is **refused**, never booked whole or less
the baseline: when the baseline cannot be read (a transcript that cannot be
found or read -- claude resumes a session only from its transcript, so a missing
one means fractal's resolution has lost track of claude's -- or a record whose
total is not a number or cannot be priced, logged as
`cost baseline of session <id> unknown: <reason>` at warning level) or the
figure is below it (a zeroed crash result), the result event names no cost and
sets `refused`, and every later result of the launch is refused too. The
stream's figure stays its own running estimate and `final` stays clear, so the
loop settles the launch from its transcript window with no `after`, as it does a
killed launch, prints
`Warning: the launch's result figure could not be settled against its session's earlier spend`
and marks the step partly unpriced (below). A window that prices nothing leaves
the cost `NULL`, marked `unpriced`. A killed launch never reaches a result
figure, so the baseline never touches it: its settle prices its own window.

A chat turn ([[features/chat/_index|chat]]) books nothing, but its reply's
closing line shows the same figure. A fork restores the source session's latest
record just as a resume does, so `Node.chat` and the cockpit's `ChatTurn` read
`Agent.cost_baseline` of the invocation's session before spawning whenever its
`mode` is `resume` or `fork` (the public `invocation` verb stamps it), and hand
it to the parser. A refused figure closes the turn on `$?`. A baseline read that
raises refuses the figure without stopping the reply: `Node.chat` prints the
loop's `Warning: reading the session cost baseline failed (...)` line, and the
cockpit's turn, which never raises, adds an error line.

A Claude launch that ends without its result frame -- killed by a deadline, a
stall or thinking cap, a pause, or a dead stream -- carries only the stream's
running estimate, which prices each reply from its first frame (or nothing, when
the kill cut a reply off before its frame). A reply that starts after a result
-- Claude Code waking the agent once background work ends -- spends past the
figure that result settled, so the parser's `final` flag clears at each new
top-level reply and only the next result frame sets it again. Once the process
exits and the loop has dropped the launch's `.step_pgid` handle (a pause or kill
landing during the transcript read must never signal the dead group's recycled
id), it calls `Agent.settle_cost` for every launch with the launch's window (its
spawn instant to its exit), and the claude backend prices that window from
Claude Code's own session transcript, resolved through `Agent.transcript`, and
from the sub-agent transcripts Claude Code keeps beside it, found recursively
(`<session>/subagents/**/agent-*.jsonl`, so a workflow's agents under
`subagents/workflows/wf_<id>/` count and its `journal.jsonl` never matches): a
sub-agent file last modified before the launch opened belongs to an earlier
launch and is skipped unread, since a long session's folder holds hundreds. Each
file is read on its own, keeping every `assistant` entry stamped inside the
window at its **last** line per message id -- Claude splits a reply across
lines, one per content block, and each line carries the reply's usage as it
stood when written, so the last is the final usage -- and a reply seen in more
than one file counts once, at its latest line. Each reply is priced at its own
model; an unpriced model (or a zeroed gateway figure) drops out without voiding
the rest, and a reply holding tokens at an unpriced model marks the step
(below).

The parser prices each reply once, at its first frame: it keeps every message id
it priced, since parallel sub-agents interleave their replies' frames on the
stream, so a reply's later frames never price again whatever lands between them.
Each frame prices at the model it names, since a sub-agent runs its own (a
cheaper one priced at the parent's rates would lift the estimate above the
transcripts' figure, and the settle keeps the higher); a frame naming none
prices at the top-level model.

For a launch whose stream did not end final, the sum lands as a `final=False`
figure when it exceeds the recorded one -- both undercount a killed launch, so
the higher is the closer -- and lands as-is: it is the launch's own spend, so a
thread-scope backend's settle against the session's earlier steps, which
`record_cost` applies to a streamed thread total, never touches it. A launch
whose result frame named its cost can still spend after it: Claude Code outlives
its result while background sub-agents run, and counts a reply's cost when the
reply completes. The loop records the instant the result frame arrived and
passes it as `after`; the backend then prices only the replies whose final line
falls after that instant, and their sum lands added to the recorded figure, as a
`final=False` figure, whether the process then exits `0`, exits non-zero, or
meets its deadline. A reply completing before the result is already in its
figure.

An absent or unreadable session transcript, or a window with no priced usage,
leaves the row as it was and logs `launch unpriced: <reason>` at warning level
on the agent's logger (past a result, only the later spend goes unseen, so it
logs `spend after the result unpriced: <reason>` at info and finding nothing
logs nothing); a sub-agent transcript that cannot be read is left out of the sum
with `launch partly unpriced: <reason>`. The ledger has no estimate column:
`fractal node cost` reports the figure like any other.

A launch's record can **leave part of a reply out**, so its figure is partial.
The rule is that anything left out of the figure marks the step. The settle
reads it from each transcript it opens:

- a reply whose final line has a null `stop_reason`, anywhere in the window: the
  line was written before the reply completed. A killed reply ends this way, and
  so can a completed sub-agent whose last line Claude Code wrote mid-stream
  (seen on 2.1.286 to 2.1.299): its final text is there, but its usage is
  partial, its `output_tokens` a handful on a full answer;
- a window whose last turn is a `user` turn whose reply's request was in flight:
  a prompt, Claude's continuation past the output cap,
  `[Request interrupted by user]`, or the result of the newest reply's **last**
  open tool call. Claude writes each result of a parallel call as its own user
  turn, so a result while other calls of the same reply still run is a kill
  during a tool call;
- an entry it cannot read whole: a reply's usage or a tool id of the wrong
  shape, or a usage count that is not a number (`_usage_cost` prices it as
  nothing and says so, rather than raising);
- a reply it cannot price: tokens at a model the pricing table lacks, or at no
  named model, which `_usage_cost` reports the same way. A synthetic reply
  (Claude Code's `<synthetic>` stand-in for an API error) holds a usage of
  zeros, so it leaves nothing out; past a result, only a reply after it counts;
- a reply that searched the web (a nonzero
  `server_tool_use.web_search_requests`): Claude Code's own cost record charges
  each search a fee beside the tokens ($0.01 on 2.1.300), which the pricing
  table does not carry, so the step books the tokens alone. A web fetch
  (`web_fetch_requests`) bills only its tokens, so it leaves nothing out; past a
  result, only a reply after it counts;
- a reply billed above the pricing table's rates. Claude Code bills a reply
  whose usage names `inference_geo: "us"` at 1.1 times its token cost (no other
  geography, `global` included, costs more on 2.1.300). It bills a reply whose
  usage names `speed: "fast"` from fast-mode rates: 2 times the standard rates
  for `claude-opus-5-5`, `claude-opus-5` and `claude-opus-4-8`, and 6 times for
  `claude-opus-4-6` and `claude-opus-4-7`, on 2.1.300. It also bills a reply
  whose prompt is above a long-prompt threshold at higher rates. The threshold
  is the one the model's table entry names in an
  `input_cost_per_token_above_<N>k_tokens` key, and the prompt is the input with
  its cache reads and writes, strictly above `N` thousand tokens, as Claude Code
  counts it. Few Claude models name one (`claude-haiku-5-5` names 100k, as
  Claude Code's own catalog does), and a model that names none is never marked
  for its prompt's size. The premium, the fast-mode rates and the long-prompt
  rates are not priced, so the step books the base rates. Past a result, only a
  reply after it counts;
- a sub-agent transcript it cannot read, and, on a launch with no result figure,
  a missing or unreadable session transcript or a settle that raises (the loop
  marks the last itself);
- on a launch whose result frame named its figure, a sub-agent the stream saw
  running after the result (below) when the settle books no spend past it: its
  transcript missing or unreadable, its usage unpriced, or the settle raising
  (the loop marks the last itself);
- a result figure the baseline could not settle (above), which the loop marks
  itself: the window stands in for a figure that can hold spend no transcript
  records.

Some endings are whole. A kill during a tool call ends on the completed reply
that called the tool, or on the results of some of its calls. A workflow
sub-agent ends by design on the result of its `StructuredOutput` call (a user
turn whose `tool_result`s all answer `StructuredOutput` calls seen earlier in
the same transcript), with no reply after it -- unless the result is an error,
after which the agent retries. Entries of types other than `user` and
`assistant` are Claude's bookkeeping and never end a window. With `after`, the
result frame's figure already covers the spend before it, so only a partial
reply, an unanswered last turn or a malformed entry stamped after that instant
counts; a completed launch with a partial line or an interrupted sub-agent
before its result reads as whole, and a missing session transcript leaves out
only the spend the stream saw after the result (below).

Past a result frame the transcript is the one source for further spend: the
parser stops pricing sub-agent frames once its figure is final (only a new
top-level reply reopens it), since Claude Code also streams a background
sub-agent's replies with `parent_tool_use_id` set, and the settle adds them from
the transcript. Any sub-agent frame while the figure is final sets the parser's
`trailing` flag, which the next result frame clears, since its figure covers
every frame before it. `Agent.stream` carries the flag on
`StreamResult.trailing`, and the loop passes it to `settle_cost`, which marks
the step when the settle books nothing past the result; a launch nothing follows
past its result stays whole whatever the settle finds. On Claude Code 2.1.300,
`-p` holds its result until background agents finish, so no frame follows it.

The step books what was recorded, with no estimate for the missing part:
`_settle_cost` returns a `Settlement` (`cost`, `cut_off`), `Agent.settle_cost`
returns the verdict, and the loop prints
`Warning: part of the launch's spend is missing from its record` to stderr
(which reaches `headless.log`) and passes it to `step_end`, at the steered close
too, which appends `; partly unpriced` to the row's metadata at any terminal
when the row holds a figure; a row with no figure is not partly priced, so its
end marks it `; unpriced` instead, at any terminal -- a completed launch whose
record leaves spend out and holds none never reads as a silent `NULL`. The
warning, and the refused figure's warning (above), end on the marker the row
gets, read from the row once the settle is done: `(partly unpriced)` for a row
holding a figure, `(unpriced)` for one holding none. `step_cost`'s strip of a
stale `unpriced` marker leaves `partly unpriced` alone, since the missing part
stays unpriced.

The settle runs after every launch and parses every line of each transcript it
opens, before the window check (a `StructuredOutput` call can precede the
window); a whole session transcript of a long node parses in tens of
milliseconds, and sub-agent files untouched since the launch opened are never
opened.

A thinking-cap kill is the one cut the stream itself records: the loop ends the
launch at the `max_tokens` delta, which can beat Claude Code's write of that
reply (up to the output cap's worth of tokens). The parser keeps the newest
top-level reply's usage from its `message_start`, overlaid with the closing
`message_delta`'s usage, and when the delta reported a figure the `thinking_cap`
event carries the reply in the transcript's message shape. The loop passes it to
the settle as `replies`, which closes the session transcript's window with it at
that usage -- so the step books it and reads as whole, unless Claude wrote the
reply and a turn after it (its continuation, a few milliseconds later), which
then ends the window as written. Only a delta with no usage leaves the reply
itself unpriced. A cut-off Codex step is settled from its rollout counters
instead (above).

## Rollups and the per-run subtree

`fractal node cost spent` sums step rows for the **current run** by default (the
active run, else the most recent) -- budgets and spend are per-run, so a drained
prior run never bleeds into the bare reading (`--run` scopes to a specific run;
`--iter`/`--step` scope to one iteration's or step's rows, without children;
each must name the target's own row, and another node's is refused).

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
  never flushed -- a kill before the first flush, or a refused result figure,
  that the provider's own record cannot price either, a post-launch death before
  the stream opened, or an untracked agent's rows -- or whose invocation
  evidence did not validate (the codex attribution above;
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
- A priced step marked `partly unpriced` (above) sums as if whole, so the same
  commands disclose those too
  (`N partly unpriced steps (some spend missing from the record) counted at recorded spend`,
  counted by `Cost.partly_unpriced`). Nothing adds a margin for the spend no
  record holds ([[features/cost/budgets|budgets]]).

Codex's model-acceptance preflight is a paid invocation -- one short turn at the
configured model -- that runs before step rows exist. Its spend stays off the
ledger: it lands in no step row, is absent from the recorded run spend, and
counts toward no cap.

## Finality

An open step's row carries only what its provider has established. Codex keeps
the row unpriced throughout execution and publishes no partial accrual. A
terminal node can still have unresolved costs; terminal status alone does not
complete missing accounting evidence. A token-priced agent with no model set is
priced at the served model its session record names.
