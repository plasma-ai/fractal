---
name: architecture/packages
desc: |
  How the packages fit together: the cli, core, tui, impl, and util
  packages, the node-machinery seeds, and the shim pointer dist published
  beside the main package.
created: 2026-07-21T04:47:26Z
updated: 2026-07-21T04:47:26Z
---

# architecture/packages

[[_index|..]]

***

`plasma-fractal` is a standalone plugin providing the **fractal** skill. The
`fractal/` package splits into five code packages, three seed directories that
ship as data, and a separate metadata-only `shim/` dist at the repo root.

## Code packages

- **`cli/`** — the typer command-line app. `main.py` assembles the app,
  `utils.py` holds shared command plumbing (including the error-wrapping command
  decorator), and `cmd/` has one module per sub-app named after it — `node`,
  `radio`, `plan`, `config`, `cost`, `time`, `db`, `event`, `channel` — with
  top-level commands (`init`, `commit`, `open`, `pause`, `resume`, `reset`,
  `destroy`, ...) in `fractal.py`. The CLI stays thin: commands parse arguments
  and delegate to core.

- **`core/`** — the business logic. The node model and lifecycle (`node.py`),
  the in-process iteration loop (`loop.py`) and its commit pipeline
  (`commit.py`), the agent base class and provider registry (`agent.py`, see
  [[architecture/agent_providers]]), the database wrapper, schema, and row
  accounting (`db.py`, `schema.sql`, `record.py`, see
  [[architecture/database]]), plus configuration, cost and pricing, radio,
  files, plans, sessions, time, events, rendering, and the worktree/registry
  machinery (`worktree.py`, see [[architecture/worktrees]]).

- **`tui/`** — the Textual cockpit opened by `fractal open`: the app and its
  panes, chat, a poller and snapshot layer that reads tree state, and theming.
  The TUI observes through core; it holds no business logic of its own.

- **`impl/`** — one provider backend module per supported agent CLI (`claude`,
  `codex`, `grok`, `opencode`, `omp`), each slotting into the registry defined
  in `core/agent.py`.

- **`util/`** — shared low-level utilities with no fractal domain knowledge: git
  and tmux wrappers, filesystem helpers, duration and time parsing, system
  probes, and title formatting.

`skills/` carries the plugin's skill definition, and the pytest suite lives in
`tests/` at the repo root.

## Node-machinery seeds

Three directories ship inside the package as data, not code:

- **`_assets/`** — static templates; today the git `exclude` block that ignores
  fractal's runtime artifacts across all worktrees.
- **`_node/`** — the per-node seed copied into each new node's data directory at
  init: `NODE.md`, the step files (`steps/` — PREPARE, PLAN, EXECUTE, REVIEW,
  COMMIT), mode overlays (`modes/`), the node scripts (`scripts/` — setup, test,
  lint), per-agent config directories (`agents/`), and the node-facing skills
  (`skills/` — fractal, memory, radio, wiki).
- **`_scripts/`** — the lifecycle shell scripts (`init.sh`, `start.sh`,
  `stop.sh`, `kill.sh`, `pause.sh`, `resume.sh`, `merge.sh`, `delete.sh`,
  `finish.sh`, `attach.sh`, `retire.sh`, `unretire.sh`, `reset.sh`,
  `destroy.sh`). The node class delegates shell-native work — git and tmux — to
  these via subprocess; every lifecycle method calls a corresponding script, and
  the scripts shell back into `fractal` for registry and config reads.

The runtime split is: `start.sh` launches a tmux session that runs the iteration
loop in-process Python (`core/loop.py`), while the surrounding lifecycle
(creating worktrees, merging, tearing down) stays in the shell scripts.

## The shim pointer dist

`shim/` at the repo root holds a second, metadata-only PyPI dist named
`fractal`: no code, just an exact `plasma-fractal==<version>` pin that bumps in
lockstep with every release. Installing `fractal` therefore installs
`plasma-fractal`. The build workflow gates and builds the shim alongside the
main package, and the publish job uploads both dists — both PyPI projects trust
the same repository, workflow file, and environment as their publisher.
