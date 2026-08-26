---
name: user_flow/getting_started
desc: |
  From a bare repository to a running node: installing fractal, initializing
  the tree with fractal init, creating a node with fractal node init,
  authoring the NODE.md contract, and launching with fractal node start.
created: 2026-07-21T04:47:43Z
updated: 2026-07-21T04:47:43Z
---

# user_flow/getting_started

[[_index|..]]

***

This journey takes a repository with no fractal state to a single autonomous
node running in tmux. Four stages: install, initialize the tree, create and
brief a node, launch it.

## Install

Fractal ships on PyPI as `plasma-fractal`; the `fractal` project there is a
metadata-only pointer dist that pins the same version, so `pip install fractal`
and `pip install plasma-fractal` are equivalent. Install it into the environment
whose `fractal` executable you want on PATH (a project `.venv` works — node
launches re-export it into their loop runtimes).

Every node lives in a **git** worktree. **tmux** is the default runtime and
provides attachable live panes; on a host where tmux is unavailable, launch with
`fractal node start --headless` instead. You also need at least one agent CLI on
PATH — `claude`, `codex`, `grok`, `opencode`, or `omp` — since nodes drive an
agent, not a model API directly.

Then run `fractal install` once. It copies the bundled **fractal** and **wiki**
skills into the Claude Code (`.claude/skills`) and Codex (`.agents/skills`)
skill directories — your home directory by default, the current project with
`--project` — so the agents that operate nodes know the machinery they are
running inside. (`--link` symlinks instead of copying, the editable-install
development setup.)

## Initialize the tree: `fractal init`

From the repository root (or a monorepo sub-project folder):

```
fractal init [--agent=claude] [--provider=...]
```

This creates the **user node** — the passive root of the tree, keyed to the
branch you ran it on. It is deliberately lightweight: a `.fractal/<branch>/`
data directory holding the tree's central database and a radio identity, with no
steps, scripts, or skills — the user node never iterates. `--agent` and
`--provider` set the defaults every spawned node inherits when it doesn't choose
its own.

What the operator decides here:

- **Which branch is the root.** The tree anchors to the current branch, and
  finished work eventually merges back to it. Init refuses a detached HEAD and
  branch names containing `/` (every per-branch artifact keys on the branch as
  one path component).
- **One fractal per branch.** A branch maps to a single project; a second init
  on the same branch for a different sub-project is refused.

Init also scaffolds the project wiki (`wiki/`) — the shared knowledge base nodes
read and grow — and leaves the user node's data directory git-ignored by default
(`fractal track` / `fractal untrack` toggle that).

## Create a node: `fractal node init`

```
fractal node init <name> [options]
```

This is where the real decisions live. Fractal creates a git worktree under
`.worktrees/` on a new branch named `<parent>.<name>` — the branch it was run
from plus a dotted segment, so the branch name always spells the tree path from
the root (a node `wiki` created on `main` runs on branch `main.wiki`) — and
populates the worktree's `.fractal/<branch>/` directory with the node's
machinery: step files, scripts, skills, and `config.json`, seeded from the
package (or from the parent, with `--inherit`).

The flags are the node's whole run contract, set now and read at launch. The
ones every operator should decide consciously:

- `--max-cost` (with `--reserve-budget`), `--timeout`, `--max-iters` — how much
  the node may spend before its run ends.
- `--max-depth`, `--max-children`, `--max-descendants` — whether the node may
  spawn children, and how large its subtree may grow. All zero makes a leaf;
  anything more makes a manager.
- `--scope` — the subdirectories the node may commit to (the shared `wiki/` is
  always allowed).
- `--agent`, `--model`, `--effort` — who does the thinking.
- `--base` — the branch to fork from and later squash-merge back into, when it
  isn't the dotted parent.

The flag-by-flag reference lives in the [[configuration/_index|configuration]]
branch; this page only names the decisions. Everything lands in the node's
`config.json`, which stays editable until launch.

## Author the contract: NODE.md

The node's task contract is `<node_dir>/NODE.md` (inside the worktree's
`.fractal/<branch>/`). The seed ships with two empty sections the operator must
fill in:

- **Instructions** — the goal and direction: what to build, where the sources
  are, what the boundaries are. Written for an agent that wakes up with no other
  context, every iteration.
- **Completion Requirements** — the observable conditions under which the node
  should declare itself done. When they are all met, the node runs
  `fractal node finish` on itself; until then the loop keeps iterating and
  spending budget. Leave the section empty and the node never self-completes —
  it runs until a budget ends it or you signal it.

Good completion requirements are checkable by the node while it runs (tests
pass, files exist, lint is clean) — never a gate only the operator can open
after the fact.

## Launch: `fractal node start`

```
fractal node start <name>
```

By default the node launches in a tmux session named after the repository and
branch (`repo (branch)`, dots dashed so tmux treats each as one name) and its
status flips from `idle` to `active`. All run parameters come from `config.json`
— start takes no tuning flags of its own (only the runtime choice —
`--headless`/`--tmux` — and `--continue`/`--clean`/`--drain`/`--max-cost`, the
relaunch path described in [[user_flow/continue_resume]]). The CLI prints the
session name; `fractal node attach <name>` drops you into the live session, and
detaching leaves the node running.

In a locked-down or non-interactive environment, use:

```bash
fractal node start <name> --headless
```

The loop runs in a detached process group and writes its output to
`<node_dir>/headless.log`. Headless mode is inherited by child starts, so a
delegating node can build and run a full tree without tmux. Pass `--tmux` to a
child start to opt that launch back into tmux.

From here the loop is autonomous: it iterates through its steps, commits its
work each iteration to its own branch, and reports over radio. The operator's
job changes from configuring to monitoring and steering —
[[user_flow/operating]].
