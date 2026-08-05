---
name: features/loop/steps
desc: |
  The step sequence of an iteration: how step files are discovered and
  ordered, what each of the five seed steps instructs, the frontmatter
  overrides a step file can carry, the SYNC pass that precedes each step,
  the checkpoints the loop runs between steps, and the retry and model-drop
  re-dispatch policies.
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
every numbered step, labelled with the step it precedes; its prompt is the
installed package's own `SYNC.md` mode document, never a per-node copy. A failed
SYNC is non-fatal — the loop notes it and continues into the step — but a SYNC
that times out fails the iteration: the loop force-commits the work in progress
and closes the iteration as timed out. A pause landing during SYNC parks the
node before the step's own agent ever launches.

## Checkpoints between steps

The loop brackets every step with lifecycle and budget checks:

- **Pause** is checked before every step, the first included, so a pause landing
  during setup never buys a whole agent turn. Parking makes no commit: the dirty
  worktree is the frozen mid-iteration state that resume continues from.
- **Stop** and the **subtree cost ceiling** are checked between steps, so a long
  iteration stops queuing steps soon after the signal or the ceiling trip rather
  than at the iteration boundary.
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
