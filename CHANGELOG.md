# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- `node kill` lands on `idle` nodes: a booting spawn is reaped and a
  never-started spawn is stamped `killed` so it can never activate — an unwanted
  spawn no longer gets a head start while an operator poll-watches for its
  activation.

## [1.1.0] - 2026-07-28

### Added

- Cost and model accounting: every assistant row records its served model and
  running cost, `node list` shows each node's run spend, and the status
  qualifier sits in its own detail column.
- Model-drop enforcement: a step whose served model drops off the pinned model
  is re-dispatched once, and unresolved drops are marked in the detail column.
- Step session attribution: a radio message stamps the acting step's recorded
  session (via the loop-exported `STEP_ID`; own-node steps only, so sessions
  stay sender-owned), falling back to the node's woven session outside a step.
- `node merge --continue` finishes a hand-resolved squash merge and warns which
  files the continue resolved against the node; merges chain into delete and
  report each reaped worktree's size.
- TUI: chat sends queue while a turn is running and `ctrl+g` interrupts; the
  node/descendants log scope persists per branch; the radio composer clears
  subject and thread after a send.
- `node init --steps` seeds the step prompts from an explicit directory instead
  of the package seed.
- The README warns that nodes run their agents without permission prompts by
  default.

### Changed

- Radio identity: every verb that writes a row attributed to the acting node
  (`send`, `post`, `reply`, `react`, `unsend`, `save`, `unsave`, `sub`, `unsub`,
  `channel create`, `channel delete`) resolves the actor env-first — an explicit
  `--path` wins, else the loop-exported `_NODE` names the calling node, else the
  cwd's node acts; pure listings keep `--path` as a subject selector.
- Tree scoping: `reset`, `destroy`, and the tree-wide verbs (`pause`, `resume`,
  `track`, `untrack`, `open`) take the root branch as an optional argument and
  otherwise infer it from the caller's branch; `destroy --all` is the only
  repo-wide verb.
- Effort is flag-only: ambient `CLAUDE_EFFORT` / `CLAUDE_CODE_EFFORT_LEVEL`
  variables are unset when composing an agent invocation, so an operator shell's
  effort never overrides a step's pinned effort.
- Each seed directory hides behind its own ignore entry instead of the shared
  block, and codex's engine-materialized `skills/.system/` tree stays out of git
  and out of every work commit; `resume` refreshes the repo's exclude block so a
  stale worktree heals itself.
- Top-level nodes are directed to radio the user inbox to report out, and the
  fractal skill always asks about decomposition before running.
- Parent codex instructions files carry to spawned children.
- User-facing output renders em dashes.
- A `--max-cost` retune below the enforcement floor names the floor it sits
  under.
- The `plasma-fractal` dist and its `fractal` pointer declare themselves
  POSIX-only via an `Operating System :: POSIX` classifier: the config and
  worktree locks use `fcntl` file locking, and the node machinery drives tmux
  through POSIX shell scripts, so the package does not import on Windows.

### Fixed

- Drain-wait syncs render correctly in the TUI.
- List-valued config keys normalize their entries at the setter.

## [1.0.0] - 2026-07-21

Initial release of `plasma-fractal`, with the `fractal` pointer dist on PyPI
pinning it in lockstep.

[1.0.0]: https://github.com/plasma-ai/fractal/releases/tag/v1.0.0
[1.1.0]: https://github.com/plasma-ai/fractal/compare/v1.0.0...v1.1.0
[unreleased]: https://github.com/plasma-ai/fractal/compare/v1.1.0...HEAD
