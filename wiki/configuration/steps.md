---
name: configuration/steps
desc: |
  The step files that define a node's iteration: discovery and ordering
  rules, the seed step sequence, the SYNC pseudo-step, the naming and headings
  of step and mode files, every supported frontmatter override, and how to
  customize a node's step list.
created: 2026-07-21T04:48:38Z
updated: 2026-11-27T22:49:51Z
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

## File names and headings

The seed's step files share one shape: a two-digit zero-padded prefix and a name
in capitals (`00-PREPARE.md` through `04-COMMIT.md`), a frontmatter block (see
[[#Frontmatter overrides]]), then a body that opens with a second-level heading
naming the step (`## Prepare`). The heading is second-level because the body
renders beneath the `NODE.md` charter, which has no top-level heading and whose
own sections are second-level (see [[features/loop/prompt_assembly]]). The loop
enforces only the digit prefix and its single width; the two digits, the
capitals, and the heading are the seed's convention.

The mode documents in `fractal/_node/modes/` follow a matching convention. A
mode file is named in capitals with no prefix, its words joined by underscores
as in the flag that enables it (`GIT_PENDING.md` on `GIT_PENDING_MODE`); the
name is the wiring, so renaming the file rewires it
([[features/loop/prompt_assembly#Modes|prompt_assembly]] has the rule). No mode
file carries frontmatter, and each opens with a second-level heading of the form
`## <Name> Mode` (`## Resume Mode`, `## Git Pending Mode`). The files the code
reads by name rather than joining on a flag -- `CHAT.md`, `STALL.md`, and
`GIT_PENDING.md` ahead of a steer -- are headed the same way. `SYNC.md` is the
exception: it runs as a step of its own, so it is headed like a step
(`## Sync`).

## Frontmatter overrides

The frontmatter grammar is strictly flat: the block opens with `---` on the
first line and closes at the next `---`, and each line inside matching
`key: value` (a lowercase key, a non-empty scalar) contributes one override,
first occurrence winning. Anything else -- including a file with no opening
fence -- contributes nothing. No YAML parser reads the block: a key is lowercase
letters and underscores only, and its value is the rest of the line as written,
so quotes stay part of the value and there is no nesting or list syntax. The two
flags, `requires_approval` and `detached`, are set only by the exact lowercase
`true`: `True` or `yes` does not set one. A well-formed line whose key is not
one listed below is not an error -- nothing reads it. The supported keys:

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
  step's agent supports. An agent with no routes ignores the key when the step
  runs; a template's step check still refuses it at init (see
  [[configuration/templates]]).
- `model` -- model override for this step; takes precedence over the node's
  configured model.
- `effort` -- reasoning-effort override for this step.
- `timeout` -- this step's own time ceiling, substituting for the node's
  `step_timeout`. Step files are live-edited steering surfaces, so a malformed
  duration warns and falls back to the node value instead of crashing the loop.
  The step's reports follow the same ceiling: its `$TIME_BUDGET`,
  `fractal node time remaining`, and the `node list` stale flag.
- `detached` -- in a continuous node, `true` runs this one step as a separate
  agent invocation; `false` restates the default and is a no-op. In a node
  already running detached, any `detached:` key is invalid and fails the step.

The effective step time limit is the tightest of the run deadline, the iteration
deadline, and the step ceiling.

Each of the five seed steps carries one frontmatter line,
`requires_approval: false`. False is already the default, so the line changes
nothing: it restates the default so the key is visible where an operator edits
the file. Both fractal skills that teach step customization -- the operator's
(`fractal/skills/fractal/SKILL.md`) and the node's
(`fractal/_node/skills/fractal/SKILL.md`) -- list the same seven keys.

## Customizing a step list

Steps live in the node's own data directory, so tailoring a node means editing
that copy -- most commonly by the spawner, between `node init` and `node start`:
trim seed steps a narrow node does not need (keeping the prefix widths
consistent), add project-specific steps, or set frontmatter overrides (a cheap
model for a mechanical step, an approval gate before an expensive one). Spawning
with `--inherit=steps` starts from the parent's tailored list instead of the
package seed, and `--template=<path>` starts from a template folder's committed
step set (see [[configuration/templates]]). On a template-seeded node, record a
trim with `--exclude` at init instead of deleting by hand: `node reseed` makes
the node match its template's effective set, so a hand-deleted file comes back.
A running node's step files remain editable steering surfaces for its operator
or parent; the node itself is contractually barred from modifying its own seed.
