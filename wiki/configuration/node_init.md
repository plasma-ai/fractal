---
name: configuration/node_init
desc: |
  Every flag of the node creation command: identity and placement, seeding
  and agent selection, tree limits, time and cost budgets, execution modes,
  and the cross-flag rules enforced at initialization.
created: 2026-07-21T04:48:38Z
updated: 2026-07-21T04:48:38Z
---

# configuration/node_init

[[_index|..]]

***

`fractal node init <name>` creates an agent node: a git worktree on a new branch
(`<name>`, or `<parent>.<name>` when the current branch is itself a node) plus a
`.fractal/<branch>/` data directory seeded with steps, scripts, skills, and
configuration. The node's task contract lives in the data directory's `NODE.md`
-- author its Instructions and Completion Requirements sections, then launch
with `fractal node start <name>`.

Nearly every flag persists into the node's `config.json` under a key of the same
name (dashes become underscores) -- [[configuration/config_json]] enumerates the
stored keys and their post-init mutability. Duration-valued flags take a number
with a unit suffix (`30s`, `10m`, `1.5h`); a bare number is rejected, and every
duration must come to at least one whole second. Flag combinations are validated
at init, so a configuration the loop cannot run is refused before the node
exists.

## Identity and placement

- `name` (positional, required) -- the node name. Composed with the parent
  branch into the node's branch name, so spawned nodes are always
  `<parent>.<name>`.
- `--path` (default `.`) -- project root: the repo root or a monorepo
  sub-project folder. A child passes a non-root value to select its own
  sub-project; otherwise the parent's project is inherited. The resolved value
  persists as the immutable `project` config key.
- `--title` -- human-readable display name. Defaults to the de-slugged node
  name.
- `--scope` (comma-separated; repeatable) -- subdirectory scope within the
  worktree. Each root must be a repo-relative subdirectory: absolute paths and
  `..` segments are refused, since they would never match the commit pipeline's
  prefix check and would brick every scoped commit. With a scope set, the node's
  commits are limited to the scope roots (the shared `wiki/` tree and the node's
  own `.fractal/` directory always remain in bounds).
- `--base` -- branch to start from instead of the parent's branch. The base is
  also the squash-merge target when the node finishes, so it must have a
  checked-out worktree in the repository -- init refuses a worktree-less base
  rather than letting the merge fail long after init succeeded.
- `--meta` -- target node branch for meta-configuration: a node whose job is
  editing another node's seed. Expands to `--base=<target>` plus a scope of the
  target's `.fractal/<target>` directory, and is therefore mutually exclusive
  with both `--base` and `--scope`. The target node must already have a
  worktree.

## Seeding and agent selection

[[configuration/inheritance]] covers the full resolution rules; the flags are:

- `--inherit` (comma-separated; repeatable) -- seed surfaces from the parent
  node's live copies instead of the package seed: `steps`, `scripts`, `skills`,
  `config`, or `all` (which expands to the other four). `config` copies the
  parent's preference keys only -- budget-class caps never inherit.
- `--steps` -- directory of step files (`*.md`) seeding the node's `steps/`
  instead of the package seed, copied byte-for-byte in filename order. The
  directory must hold at least one step file, each carrying the `NN-` digit
  prefix the loop discovers steps by, at one width (see [[configuration/steps]])
  -- a profile that violates either would seed a node that cannot iterate, so
  init refuses it. The flag is mutually exclusive with `--inherit=steps` (two
  rival step sources).
- `--agent` -- agent command (e.g. `claude`, `codex`, `grok`, `opencode`,
  `omp`). Defaults to the nearest ancestor's configured agent; the user node
  sets the tree default via `fractal init --agent`. An unknown agent is refused
  at init against the supported-agent registry rather than killing the loop at
  boot.
- `--provider` -- provider route for the agent (e.g. `openrouter`). Defaults to
  the vendor-native endpoint, inherited from the nearest ancestor. An explicit
  route the agent does not support is refused; an inherited one is silently
  dropped so a routed ancestor never pins a route on a route-less backend.
- `--model` -- model override, passed through the agent CLI's model flag.
  Defaults to the agent's own default model.
- `--effort` -- reasoning-effort override, passed through the agent CLI.

## Tree limits

Caps on what the node may spawn, enforced at spawn time against live counts.
Each takes a non-negative integer and defaults to unlimited; `0` disables
spawning entirely.

- `--max-depth` -- maximum child node nesting depth below this node.
- `--max-children` -- maximum direct child nodes.
- `--max-descendants` -- maximum total descendant nodes.

## Iteration and time budgets

- `--max-iters` -- per-run iteration cap (default: unlimited; must be positive).
- `--timeout` -- whole-run time budget.
- `--iter-timeout` -- per-iteration time budget.
- `--step-timeout` -- per-step time budget; caps each step. A step file can
  substitute its own ceiling via `timeout:` frontmatter (see
  [[configuration/steps]]).
- `--interval` -- fixed iteration schedule: iterations start on a fixed cadence.
  Mutually exclusive with `--sleep`, and `--iter-timeout` may not exceed it (an
  iteration cannot run past its slot).
- `--sleep` -- fixed delay between iterations.
- `--wait` (default `1m`) -- sleep between approval-wait sync invocations while
  a gated step waits for approval (see [[configuration/steps]]).

## Step retries

- `--step-retries` (default `1`) -- retries per failed step; `0` disables.
- `--step-retry-backoff` (default `10s`) -- delay before each step retry.

## Cost budgets

Cost semantics in depth live in the `wiki/features/cost/` branch; the flags:

- `--max-cost` -- maximum cost in USD per run. Runs are isolated, so each launch
  arms the cap anew; after a budget-ended run, `fractal node start --continue`
  refuses without an explicit `--max-cost`.
- `--max-iter-cost` -- maximum cost per iteration in USD. Requires `--max-cost`
  (a per-iteration cap with no run ceiling cannot be enforced once the
  per-iteration budget drains).
- `--max-step-cost` -- maximum cost per step in USD; warn-only when the agent
  cannot enforce it. Requires `--max-cost`.
- `--reserve-budget` (default `10%`) -- budget reserved for cleanup, as a USD
  amount or `N%` of `--max-cost`. Requires `--max-cost`; with no run ceiling
  there is no reserve. The reserve is not an enforced floor: it shifts the point
  where the node enters reserve mode -- the budget is treated as drained
  `reserve_budget` USD before `--max-cost` is reached, so the run winds down
  with cleanup headroom left.

Ceilings must be positive, and the caps must order `step <= iter <= run`. An
explicit reserve must stay below 99% of `--max-cost`.

## Execution modes

- `--sync` / `--no-sync` (default: enabled) -- run the SYNC radio pass as its
  own pseudo-step before each step (see [[configuration/steps]]).
- `--detached` / `--no-detached` (default: continuous) -- run each step as a
  separate agent invocation with a fresh context, instead of one continuous
  session per iteration.
- `--local` / `--no-local` -- skip pushing to remote after each iteration's
  commit. Inherited from the parent and latched: once a node is local, the flag
  cannot be changed, and its children cannot push either.
- `--blind` -- subscribe the node to no radio channels. The parent still reads
  this node's outbox.
- `--sealed` -- seal the node's mailbox: its own seat cannot read hosted
  messages until unsealed (`config set sealed=false`). The harness half of
  verifier isolation -- sealed adjudication traffic is held out of the seat's
  context entirely, while an operator shell reads freely.

## Maintenance

- `--reset` -- delete the node's files and reinitialize it from the seed.
