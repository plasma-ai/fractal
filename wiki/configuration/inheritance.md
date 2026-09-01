---
name: configuration/inheritance
desc: |
  What a child node inherits from its ancestors and how: the unconditional
  surfaces (agent, provider, root, local, project, agent config), the opt-in
  inherit surfaces, and which keys never inherit.
created: 2026-07-21T04:48:38Z
updated: 2026-07-21T04:48:38Z
---

# configuration/inheritance

[[_index|..]]

***

A child node starts from its parent in two senses: its branch forks from the
parent's latest commit (so it inherits the parent's committed work, never the
uncommitted working tree), and parts of its configuration and seed resolve from
its ancestors. Inheritance splits into what always flows down and what is opt-in
via `--inherit`.

## Always inherited

- **`root`** -- every node's config carries the tree's root (user) node branch,
  copied from the parent at init and immutable after. It anchors the tree: any
  node can resolve the central per-tree database from its own config alone.
- **`agent`** -- when a spawn does not pass `--agent`, the agent command
  resolves by walking up the ancestor chain to the nearest node with one
  configured; the user node sets the tree-wide default via
  `fractal init --agent`. When no ancestor has an agent, init refuses.
- **`provider`** -- the provider route resolves by the same nearest-ancestor
  walk, with a guard: an inherited route the child's agent does not support is
  silently dropped (falling back to the vendor-native endpoint), so a routed
  ancestor never pins a route on a route-less backend. An explicitly passed
  unsupported route is refused instead.
- **`local`** -- a local parent forces the child local; a child of a local
  parent cannot opt back into pushing, and the flag is latched once set.
- **`project`** -- the child works in the parent's project unless its `--path`
  selects another sub-project.
- **Agent config directories** -- each node data directory carries a config dir
  per supported agent backend (the agent CLI's settings file plus a skills
  link), recreated at every init and gitignored. A template's `agents/<agent>/`
  file beats the parent's live copy, which wins over the package seed (see
  [[configuration/templates]]), so agent-level settings flow down the tree
  unconditionally -- this is the one file surface that inherits without opting
  in. For codex, a relative `model_instructions_file` the config names is copied
  alongside it, so the inherited config never points at a missing file.

## Opt-in: the inherit surfaces

`fractal node init --inherit=<surfaces>` (comma-separated; repeatable) seeds
file surfaces from the parent's live copies instead of the package seed; `all`
expands to the full set. Inheriting a surface the parent does not carry is an
error, and so is inheriting a surface the spawn's template bundles --
`--inherit=steps`, `scripts`, or `skills` is refused when the template carries
that surface, two rival sources (see [[configuration/templates]]).

- **`steps`** -- copy the parent's step list, including its trims, added steps,
  and frontmatter overrides (see [[configuration/steps]]).
- **`scripts`** -- copy the parent's setup, test, and lint scripts with their
  project-specific extensions (see [[configuration/scripts]]).
- **`skills`** -- copy the parent's skill set.
- **`config`** -- copy the parent's *preference* keys into the child's config as
  a spawn-time snapshot: `model`, `effort`, `sync`, `detached`, `iter_timeout`,
  `step_timeout`, `step_retries`, `step_retry_backoff`, and `wait` -- plus the
  rival pacing pair `sleep`/`interval`, inherited only when the spawn sets
  neither (the loop rejects both set). Explicit spawn flags win over inherited
  values, and a null parent value stays null (the loop applies its own defaults
  at read time).

## Never inherited

Budget-class keys never flow down: the cost caps (`max_cost`, `max_iter_cost`,
`max_step_cost`, `reserve_budget`), the iteration cap (`max_iters`), the tree
limits (`max_depth`, `max_children`, `max_descendants`), and the run timeout
(`timeout`). Each spawn prices its own budget explicitly -- a child inheriting
its parent's ceiling would double-spend the subtree's budget by construction.
Identity keys (`title`, `scope`, `base`, `meta`, `user`) are likewise per-node.
