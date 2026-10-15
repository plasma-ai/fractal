---
name: features/loop/steps
desc: |
  The step sequence of an iteration: how step files are discovered and
  ordered, what each of the five seed steps instructs, the frontmatter
  overrides a step file can carry, the SYNC pass that precedes each step,
  the checkpoints the loop runs between steps, the retry and model-drop
  re-dispatch policies, and the steer and stall of a stuck step.
created: 2026-07-21T04:51:13Z
updated: 2026-07-21T04:51:13Z
---

# features/loop/steps

[[_index|..]]

***

An iteration is a sequence of steps, each one agent invocation. The step bodies
are markdown files in the node's `steps/` directory; the loop in
`fractal/core/loop.py` discovers, orders, and runs them.

## Discovery and ordering

Steps are re-discovered at the start of every iteration from `steps/*.md`, so
adding, removing, or renaming a step file takes effect on the next iteration
without restarting the node. Files must carry an `NN-` digit prefix and are run
in lexicographic order; the step's display name is the filename with the prefix
and extension stripped. Violations fail the iteration loudly: an empty `steps/`
directory, a file without the digit prefix, or mixed prefix widths across files.

## Seed steps

A freshly initialized node carries five seed step files, shipped from
`fractal/_node/steps/`:

- **PREPARE** — merge the parent branch (upstream changes flow down), then
  review and merge each child branch with new commits as a labelled `--no-ff`
  merge; reconcile stale cross-links after merging; announce material
  integrations on radio. Skipped when the node has no parent branch and no
  children.
- **PLAN** — check for interrupted work (uncommitted changes or a backstop
  commit labelled `(failed on <step>)`), orient on skills, memory, and the
  project wiki, decide whether to decompose into child nodes, then write the
  iteration's plan file (see [[features/loop/plans]]) and trim it to fit the
  remaining budget.
- **EXECUTE** — do the planned work: spawn any planned children before leaf
  work, run the node's `test.sh` and `lint.sh` scripts as configured, write
  urgent findings to memory, and raise blockers on radio immediately rather than
  waiting for the next sync.
- **REVIEW** — verify claims of record in the work product, review the diff,
  fold the iteration's findings into memory pages, promote project-wide
  learnings to the shared wiki, and append a post-mortem section to each of the
  iteration's plans.
- **COMMIT** — signal completion with `fractal node finish` when the node's
  completion requirements are all met, then commit the iteration's work with
  `fractal commit`, which wraps the bare summary as
  `<branch>: iteration <run>.<iter> (<summary>)`.

The seed is a default, not a contract: a node's operator can trim or replace
step files to shape a lighter cadence for narrow tasks.

## Step frontmatter

A step file may open with a `---`-fenced frontmatter block of flat `key: value`
scalars (lowercase keys, one per line, first occurrence wins; anything else
contributes nothing). Recognized keys override per-step behavior:
`requires_approval` gates the step on an operator approval, `agent`, `provider`,
`model`, and `effort` re-route the step to a different agent configuration,
`timeout` adjusts the step's deadline, and `detached` controls detached
invocation. The frontmatter is stripped before the step body enters the prompt.

## The SYNC pass

When sync mode is enabled, the loop runs a SYNC pass as a pseudo-step before
every numbered step, labelled with the step it precedes. SYNC is assembled like
any step: its prompt is the `NODE.md` charter, then the body of the installed
package's own `SYNC.md` mode document (never a per-node copy), then any active
modes. SYNC also runs on each poll timeout while the loop waits for children to
finish (only while an iteration is live) and while a step waits for operator
approval; each pass gets its own step row, and these waiting passes record any
outcome but a pause as completed. A failed before-step SYNC is non-fatal — the
loop notes it and continues into the step — but a SYNC that times out fails the
iteration: the loop force-commits the work in progress and closes the iteration
as timed out. A pause landing during SYNC parks the node before the step's own
agent ever launches.

## Checkpoints between steps

The loop brackets every step with lifecycle and budget checks:

- **Pause** is checked before every step, the first included, so a pause landing
  during setup never buys a whole agent turn. Parking makes no commit: the dirty
  worktree is the frozen mid-iteration state that resume continues from.
- **Stop** and the **subtree cost ceiling** are checked between steps, so a long
  iteration stops queuing steps soon after the signal or the ceiling trip rather
  than at the iteration boundary. A step cut off this way is never launched and
  books no row; only a step that reaches its launch with no budget left records
  the `stopped` / `over budget` row.
- **Reserve mode** latches per-iteration when the iteration's own cost cap is
  reached, when total run spend drains into the reserve window below the run
  cap, or when an ancestor's budget abort left a cascaded finish pending; the
  remaining steps then run as wind-down and the run ends at the iteration
  boundary.
- **Finish drain**: when a finish signal is set, the loop waits for all children
  to end before the final step, clearing the iteration deadline so the wait
  cannot be timed out by it.

## Retries and approval

Only a failed launch retries; timed out, paused, and skipped are deliberate
outcomes that never retry. The extra-attempt count and the backoff before each
retry are node configuration, and a count of zero disables retries. Every
attempt books its own step row, so cost and duration attribute honestly to the
attempt that spent them. A step with `requires_approval` arms an approval gate
on each attempt's row; a failure retry follows a failed attempt whose wait never
ran, and a model-drop re-dispatch (below) produces new work, so its fresh demand
is deliberate — an approval never transfers between attempts.

## Stuck steps

Two triggers mark a launch stuck, and both take one path through the loop.

A **thinking cap**: a Claude reply that hits the output-token cap while still
thinking (no text, no tool call) loops, because Claude Code auto-continues with
an injected user turn that drops the cut-off thinking, so the agent restarts the
thought and caps again, spending the step's time. The Claude parser reports a
provider-neutral `thinking_cap` stream event, once per reply, for a top-level
reply whose blocks were all thinking. Either of two signals triggers it: the raw
API `message_delta` with a `max_tokens` stop (the loop always passes
`--include-partial-messages`, and the stop lands before Claude Code requests the
continuation), or, as a backstop, Claude Code's "Output token limit hit"
continuation, matched on its text alone since its flag is undocumented.
Assistant frames cannot serve: their `stop_reason` is always null.

**No progress**: a watchdog thread in the launch times the agent's own idle span
against the module constant `NO_PROGRESS_SECONDS` (1800). The provider parser
owns when the agent holds control and reports it as neutral `acting` and
`waiting` stream events; the loop's clock only follows them (running from the
launch and each `acting`, paused at `waiting`, pushed back by a `waiting` that
carries a `duration`). For Claude, top-level frames only: a new reply, a text
block, a result answering the last pending call, a non-result user turn while no
call is pending, or a `system` `init` / `task_notification` wake-up once the
turn has ended (a notice mid-turn lands while the agent's own tool still runs)
is acting; a tool call (its id claimed from the raw block start or the assistant
frame, whichever comes first) or an `end_turn` stop or `result` frame is
waiting, because Claude Code stays alive after a turn's end while background
work runs and wakes the agent with a notice; an `api_retry` frame pauses for its
`retry_delay_ms`. The pending call ids clear at each new reply, so a call that
never got its result cannot hold the clock paused. The reader blocks on stdout,
so the watchdog polls the clock and, at the threshold, ends the group through
the deadline's own routine under a flag of its own; it stops when the stream
ends, however it ends. It arms only for an agent whose stream reports control
(`Agent.reports_progress`): Claude. Codex's stream marks command starts but not
their completion, and its other tool items are not mapped, so it stays
unwatched.

For a work step (never SYNC) the first stuck launch per attempt is steered: the
thinking cap ends the invocation on the stream-error path (the render callback
raises), the watchdog through the deadline routine; the stuck row closes
`stopped` / `steered: <trigger>`, and a new row marked `steer` resumes the same
session (detached nodes included) with the package's `modes/STALL.md` as its
whole prompt. The steer is neither a retry nor a retry row, and the step ceiling
keeps counting from the attempt's first launch. The steer buys a launch, so it
first polls what a retry's backoff polls: a pause parks with the stuck row
closed (resume re-enters the step), and a stop, finish, or spent subtree ceiling
abandons it to the failure path as a `failed` row. Stuck again after the steer,
by either trigger, is a stall: the row closes `exited` with
`stalled: <trigger>, after a steer`, the step skips the retry gate exactly as a
timeout does (no new lifecycle status), the iteration fails as after a timed-out
step, and the loop posts a `step stalled` notice naming the trigger to the
node's outbox, which the parent's feed follows.

## Model drops

Infrastructure can silently serve a different model than a step pinned, so
enforcement is tool-native: the stream driver records every model the agent's
own stream names (see [[features/agents/models_and_effort|models_and_effort]]),
and when a completed launch's record does not carry its pin — the step's
`model:` frontmatter or the node default it fell back to — the loop records a
`model_drop` event with the attempt's lineage, marks the attempt's own row
(`model drop (served <model>)`), and re-dispatches the step once, outside the
failure-retry allowance but with the same backoff. Matching admits the forms a
pin legitimately resolves to and nothing looser: a gateway slug
(`anthropic/<id>`) matches its bare form, and a dated snapshot matches its pin
when the date stamp is the *whole* extension — so a version bump flags even
behind a date (`claude-opus-4` against `claude-opus-4-1-20250805`). A bare-word
alias (`opus`) names a family rather than a version, so it matches the family's
own version run and date alike, but not a named variant riding one
(`claude-opus-5-mini`). A truncation or variant suffix flags even though one id
contains the other. A drop the re-dispatch cannot resolve fails the step: pins
are honored or the step fails loudly — a second off-pin serve, a spent deadline
that abandons the re-dispatch, or a re-dispatch that fails or times out all book
the iteration failed with the drop named
(`model drop (served <model>, pinned <pin>)`), never a clean completion over
wrong-model output. The node itself is never killed — the loop warns, moves to
the next iteration, and prints an off-pin warning at that iteration's start
while the previous iteration's drop stands unresolved. `fractal node list`
composes a `model drop` marker into the node's `detail` column while a step of
the newest iteration has its newest *completed* attempt marked: a clean
re-dispatch supersedes the mark, an abandoned or failed one leaves it standing,
and the marker reads off the newest iteration alone, so a later iteration
supersedes it. Detection reads the launch's own stream record — every model
named, so a substitution the stream recovered from before ending still flags —
falling back to the step row, and attached and detached launches are enforced
identically; an agent whose stream never names the served model records the pin
itself, so unknown never reads as a verified match.
