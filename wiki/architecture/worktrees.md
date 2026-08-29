---
name: architecture/worktrees
desc: |
  The worktree-per-node design: where worktrees live, how a child forks its
  parent's branch, commit scopes, and the merge topology of per-node commits,
  no-fast-forward child merges, and squash merges toward the base.
created: 2026-07-21T04:47:26Z
updated: 2026-07-21T04:47:26Z
---

# architecture/worktrees

[[_index|..]]

***

Every agent node works in its own git worktree, so the whole tree iterates in
parallel on one repository without nodes ever touching each other's files. The
user (root) node is the exception: its "worktree" is the operator's own working
tree at the repo root.

## Where worktrees live

Node worktrees live under `.worktrees/<branch>` at the main repo root, one
directory per node, named by the node's full dotted branch. The `.worktrees/`
directory also carries tree plumbing: a `.lock` file whose tree-wide flock
serializes worktree add/remove and every spawn-cap gate (`git worktree add` is
not parallel-safe), and a `.project/<branch>` cache mapping each branch to its
project sub-path inside the worktree (`.` for a repo-root project), written once
at init and read everywhere else.

Fractal's runtime artifacts — the worktrees themselves, databases, status files,
agent logs, engine-materialized system skills — are ignored through a static
marker-delimited block in the repo-local `info/exclude` (shared across all
worktrees, template shipped in `_assets/git/exclude`), never through the user's
committed `.gitignore`. The block carries no per-tree state: the user node's own
seed directory hides itself with its own ignore file instead, so a second tree's
init can never expose the first's. `fractal track` removes that file to opt the
seed into tracking, and child seeds carry no such file — they stay tracked so
merge-up and meta-configuration keep working.

## Forking a child

`fractal node init` creates the child branch and worktree in one step:
`git worktree add` on a new branch named `<parent>.<name>`, forked from the
resolved base — the `--base` branch when given, otherwise the parent branch's
current tip. A fork always takes committed history, never working-tree state: a
top-level node additionally requires the dotless root to have no uncommitted
tracked changes, while deeper nodes may fork a parent mid-iteration (they get
its last committed tip). Re-initializing over an existing branch re-adds the
worktree at that branch's tip and preserves its committed history; a failed init
rolls the worktree and cache entry back so a retry starts clean.

## Warming the build cache

A fork takes only committed history, so a new worktree starts with none of the
git-ignored build state its siblings have already paid for. The user node's
`clone_dirs` key ([[configuration/config_json]]) names the directories worth
carrying over — a Lean `.lake`, say — and each spawn copy-on-write clones them
from the main checkout into the child (`cp -c`, APFS clonefile): near-instant,
costing no disk until a file diverges, and the child owns its logical copy, so
concurrent builds never share a mutable file.

The clone runs after the spawn lock releases — a multi-gigabyte tree must never
serialize sibling spawns — and is best-effort throughout. A missing source, an
already-populated target, a filesystem without clonefile, or any filesystem
error skips that directory and leaves the node to re-derive the cache exactly as
it would have without the clone; nothing here can fail a spawn, whose node is
registered by the time the clone starts. Each directory clones to a dot-prefixed
temporary sibling and is renamed into place, so a partial tree never poisons the
build it was meant to warm, and a crash-stranded temp matches the commit
pipeline's ignore family rather than riding a commit.

## Scopes

A node may be initialized with a `scope` — one or more subdirectories of the
worktree that bound its commits. The commit pipeline rejects out-of-scope files,
with two standing exceptions: the shared project `wiki/` and the node's own data
directory are always committable. Scopes are directory-granular; finer ownership
splits are contract text in a node's brief, not machinery.

## Merge topology

Work flows through three kinds of commit, each with a distinct shape:

- **Per-node commits.** Each node commits its own iteration work on its own
  branch with `fractal commit`, which runs the pipeline in `core/commit.py` —
  scope check, lint, stage, commit labelled with the run and iteration, and push
  (unless the tree was initialized `--local`). A node's branch is its full,
  fine-grained history.

- **Downward child merges (`--no-ff`).** When a parent integrates a settled
  child's work into its own branch, it merges the child's branch with
  `git merge --no-ff`, so each integration lands as one labelled merge commit on
  the parent's mainline rather than fast-forwarding the child's per-iteration
  commits inline. This is the integration step of the node loop (see the loop
  pages under the [[features/_index|features]] branch).

- **Upward squash merges.** `fractal node merge` merges a settled node's branch
  *toward the base*: the target is the node's configured `base` branch if set,
  else the dotted parent. The merge runs as `git merge --squash` inside the
  target's worktree, so a single squash commit lands on the target while the
  full history stays on the node's branch. The squash returns every `.fractal/`
  directory on the target to the target's HEAD (except paths under the node's
  own scope roots) and strips the node's seed directory and its descendants' (so
  node machinery never lands in the parent), refuses a footprint outside the
  node's scope roots, project wiki, and `.fractal/` unless `--ignore-scope` is
  passed, regenerates tracked wiki indexes from the merged filesystem, and logs
  the `merge` event on the *target* so the record survives the node's later
  deletion. It then advances the node's merge-base with a two-parent commit on
  the node's branch — `merge <target> (post-squash)`, parents the node's HEAD
  and the target's HEAD — whose tree is the target's post-squash tree with the
  node's own seed and its descendants' seeds kept, so the node's worktree
  converges to the target and a later merge only diffs new work. The merge
  refuses while the node or its target is active or paused — except from inside
  the target's own loop, which merges its settled children as part of a normal
  iteration — and failure paths restore the target worktree so a half-merge
  never lands.

The net effect is a two-speed history: full per-iteration detail on every node
branch, and one commit per integrated unit of work on each parent mainline. Why
the topology is shaped this way is covered in [[design/commit_discipline]].
