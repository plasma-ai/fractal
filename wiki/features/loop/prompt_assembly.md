---
name: features/loop/prompt_assembly
desc: |
  How each step's prompt is assembled: the node charter, the step body, and
  the active mode documents, rendered through one merged template variable
  map with envsubst-pinned substitution.
created: 2026-07-21T05:04:14Z
updated: 2026-07-21T05:04:14Z
---

# features/loop/prompt_assembly

[[_index|..]]

***

Every step the loop runs is launched with a freshly assembled prompt. The
assembly, owned by `fractal/core/render.py`, concatenates three layers joined by
blank lines: the node's `NODE.md` charter, the step file's body with its
frontmatter stripped, and every active mode document. One merged variable map
both selects the active modes and substitutes the assembled text in a single
pass. On the iteration a resume re-enters, the loop appends an unread-inbox
digest after rendering, to SYNC's prompt too, so the digest is never
substituted.

## Substitution grammar

Templates use `$NAME` / `${NAME}` placeholders, and rendering is pinned
byte-for-byte to GNU `envsubst`: only those two forms are references, `$$`
passes through verbatim (it is not collapsed to `$`), and an unknown placeholder
stays in the text untouched rather than rendering blank. A template therefore
means exactly what the `envsubst` grammar says regardless of who renders it.

Node templates render through Jinja once at seed time, before this prompt pass
(see [[configuration/templates]]). Their values are already ordinary text in the
node's files. This runtime pass substitutes only `$NAME` / `${NAME}`; literal
Jinja braces in a seeded file are not evaluated again.

## The variable map

Static variables are derived from the node itself and shared by every renderer:
the path set (`REPO_DIR`, `PROJECT_DIR`, `SCOPE_DIR`, `WORKTREE_DIR`,
`NODE_DIR`, `PLANS_DIR`, `MEMORY_DIR`, `WIKI_DIR`), `CURRENT_BRANCH`, the config
limits (`MAX_DEPTH`, `MAX_CHILDREN`, `MAX_DESCENDANTS`; `-1` when unset, meaning
unlimited: the spawn gate skips an unset cap, and `-1` is display-only, accepted
by no setter), and the config-derived mode flags (`DETACHED_MODE`, `META_MODE`
with `META_TARGET`). The loop re-derives the map from live config once per step,
inside the prompt build's guard, and both the prompt and the environment it
exports into the seat take that one reading: a mid-run limit edit reaches the
next step, the two never disagree, and a torn config fails the step instead of
crashing the loop at the spawn. Only the mode flags stay pinned to their boot
values, so a mid-run config edit cannot flip the run's mode composition; the
exported environment also pins `FRACTAL_HEADLESS`, the run's launch backend
(`true`/`false`), so a seat's `fractal node start` inherits the backend: the
CLI's backend resolution reads the seat-exported `FRACTAL_HEADLESS` when the
flag is absent. `SCOPE_DIR` space-joins the scope roots when several are
configured, and the project and wiki paths nest under the project prefix for a
sub-project node.

Run-scoped variables have no static derivation -- the caller supplies them as
overrides, and overrides always win over the derived map. The loop passes its
live state: `STEP_LABEL`, `ITER_LABEL`, `ITER_TIMESTAMP`, `ITER_REF`,
`TIME_BUDGET` (rebuilt per step, its step part naming that step's own ceiling),
`COST_BUDGET`, and the run-scoped mode flags (`CONTINUE_MODE`, `RESUME_MODE`,
`RESERVE_MODE`, and `DRAIN_MODE`, which is `true` for a drain run, launched with
`--drain` or carrying the drain signal across a resume). `GIT_PENDING_MODE` and
its `GIT_OPERATIONS` list ride in the same way, probed per launch (see
[[#Modes]]). A chat renders the same templates with an explicit `N/A (chat)`
sentinel for the label and budget fields -- a clear marker rather than a blank
or stale value -- and `false` for the run-scoped mode flags, so no run-only mode
document ever joins a chat prompt.

## Modes

Mode documents live in `fractal/_node/modes/`. A mode `<NAME>.md` joins the
prompt exactly when `<NAME>_MODE` is `true` in the merged map: the static modes
(detached, meta) derive from config, the run-scoped modes (continue, resume,
reserve, drain, git-pending) ride in on the loop's overrides. `SYNC.md` never
joins the assembly -- sync runs as its own step. `STALL.md` never joins either:
it is the whole prompt of a stuck step's steer, rendered with the same variable
map, since the resumed session already holds the step (see
[[features/loop/steps|steps]]). Chat turns are framed separately: the chat mode
document, preceded by the `NODE.md` charter only for a fresh chat (a forked chat
already carries the node's context).

`GIT_PENDING.md` joins on `GIT_PENDING_MODE`, which the loop sets per launch: it
probes the worktree at every launch (`fractal.util.git.in_progress`) and, when a
merge, squash merge, cherry-pick, revert, rebase, or `index.lock` is left there,
sets the flag and passes the operation list as `GIT_OPERATIONS`, so the seat
finishes or aborts the operation before running git on top of it. The notice
renders beneath the charter like every mode, since the charter owns the top of
the prompt and its opening role sentence; a steer has no charter, so the loop
puts the notice ahead of `STALL.md` there. A squash merge's line points at
`fractal node merge <child> --continue` to finish and `git reset --merge` to
abort (`git merge --abort` needs a `MERGE_HEAD` a squash never writes). The
lock's line names it by its resolved path and age without judging staleness,
since a live git command holds the same file; a linked worktree's lock lives in
its own git dir, and a guessed `.git/index.lock` would be the main checkout's. A
probe that cannot answer names nothing, and a clean worktree's prompt is
unchanged. A `--continue` boot over such an operation skips its operator-edit
commit and clean (`Loop._clean_worktree`), so the operation reaches this notice
intact instead of being committed or crashing the boot, and `CONTINUE.md` tells
the seat the worktree was left as found.
