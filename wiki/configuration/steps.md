---
name: configuration/steps
desc: |
  The step files that define a node's iteration: discovery and ordering
  rules, the seed step sequence, the SYNC pseudo-step, every supported
  frontmatter override, and how to customize a node's step list.
created: 2026-07-21T04:48:38Z
updated: 2026-07-21T04:48:38Z
---

# configuration/steps

[[_index|..]]

***

A node's iteration is its step files: the markdown files under the node data
directory's `steps/` folder, run in order once per iteration. Each file's body
becomes the step's prompt; an optional frontmatter block carries per-step
overrides.

## Discovery and ordering

The loop re-discovers the step files at the start of every iteration, so the
list is a live steering surface -- files added, removed, or edited between
iterations take effect at the next one. Every file must carry an `NN-` digit
prefix (e.g. `02-EXECUTE.md`); steps run in lexicographic filename order and are
numbered 1-based within the iteration. An empty steps directory, a file without
the prefix, or mixed prefix digit widths fail the iteration loudly rather than
running a partial list.

## The seed sequence

A new node is seeded with five steps (or the parent's live step list when
spawned with `--inherit=steps` -- see [[configuration/inheritance]] -- or a
template's committed `steps/` when spawned with `--template` -- see
[[configuration/templates]]):

- `00-PREPARE` -- merge the parent branch to pull upstream changes, and review
  and merge children's ready work.
- `01-PLAN` -- check for interrupted work, orient in memory and the project
  wiki, decide whether to delegate, and write the iteration's plan file.
- `02-EXECUTE` -- do the planned work: spawn and manage children, or do the leaf
  work, verifying with the node's test script.
- `03-REVIEW` -- review the diff, verify claims of record, and fold the
  iteration's findings into memory.
- `04-COMMIT` -- signal completion when the requirements are met, then commit
  through the commit pipeline.

## The SYNC pseudo-step

With `sync` enabled (the default), a SYNC pass -- read the radio, act on
directives, report progress -- runs as its own step before each regular step.
Unlike the other mode documents (which inject into step prompts), SYNC runs as a
standalone step, labelled with the step it precedes. Its prompt comes from the
installed package's mode documents, never a per-node copy, so it is not
customizable per node; disable it wholesale with `--no-sync` (see
[[configuration/node_init]]). Run-time semantics -- failure handling, timeouts,
and the checkpoints around each pass -- live in [[features/loop/steps]].

The name is shared with ordinary step files: a step named `SYNC` records like
any other step, and the cockpit tells the two apart structurally -- a pass
carries step 0 (the drain wait) or the number of the differently-named step it
precedes, and a still-open SYNC row reads as a pass only under sync mode (with
sync off no pass can exist, so a running SYNC step is numbered immediately) --
so a step named SYNC lists under its own number, never as muted `sync` chrome.

## Frontmatter overrides

The frontmatter grammar is strictly flat: the block opens with `---` on the
first line and closes at the next `---`, and each line inside matching
`key: value` (a lowercase key, a non-empty scalar) contributes one override,
first occurrence winning. Anything else -- including a file with no opening
fence -- contributes nothing. The supported keys:

- `requires_approval` -- when `true`, the step becomes an approval gate: on
  success the loop holds before proceeding until the parent approves. The parent
  lists waiting steps with `fractal node pending` and releases one with
  `fractal node approve`. Polling is paced by the node's `wait` duration; a
  pause, stop/finish signal, or deadline during the wait ends it with the
  corresponding outcome, and a retried step re-arms its own gate -- a granted
  approval is never re-demanded.
- `agent` -- run this step under a different agent command. The override is
  validated at launch: an unknown agent or an uninstalled command fails the step
  rather than the run.
- `provider` -- run this step on a different provider route; it must be one the
  step's agent supports.
- `model` -- model override for this step; takes precedence over the node's
  configured model.
- `effort` -- reasoning-effort override for this step.
- `timeout` -- this step's own time ceiling, substituting for the node's
  `step_timeout`. Step files are live-edited steering surfaces, so a malformed
  duration warns and falls back to the node value instead of crashing the loop.
- `detached` -- in a continuous node, `true` runs this one step as a separate
  agent invocation; `false` restates the default and is a no-op. In a node
  already running detached, any `detached:` key is invalid and fails the step.

The effective step time limit is the tightest of the run deadline, the iteration
deadline, and the step ceiling.

## Customizing a step list

Steps live in the node's own data directory, so tailoring a node means editing
that copy -- most commonly by the spawner, between `node init` and `node start`:
trim seed steps a narrow node does not need (keeping the prefix widths
consistent), add project-specific steps, or set frontmatter overrides (a cheap
model for a mechanical step, an approval gate before an expensive one). Spawning
with `--inherit=steps` starts from the parent's tailored list instead of the
package seed, and `--template <path>` starts from a template folder's committed
step set (see [[configuration/templates]]). On a template-seeded node, record a
trim with `--exclude` at init instead of deleting by hand: `node reseed` makes
the node match its template's effective set, so a hand-deleted file comes back.
A running node's step files remain editable steering surfaces for its operator
or parent; the node itself is contractually barred from modifying its own seed.
