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
pass.

## Substitution grammar

Templates use `$NAME` / `${NAME}` placeholders, and rendering is pinned
byte-for-byte to GNU `envsubst`: only those two forms are references, `$$`
passes through verbatim (it is not collapsed to `$`), and an unknown placeholder
stays in the text untouched rather than rendering blank. A template therefore
means exactly what the `envsubst` grammar says regardless of who renders it.

## The variable map

Static variables are derived from the node itself and shared by every renderer:
the path set (`REPO_DIR`, `PROJECT_DIR`, `SCOPE_DIR`, `WORKTREE_DIR`,
`NODE_DIR`, `PLANS_DIR`, `MEMORY_DIR`, `WIKI_DIR`), `CURRENT_BRANCH`, the config
limits (`MAX_DEPTH`, `MAX_CHILDREN`, `MAX_DESCENDANTS`), and the config-derived
mode flags (`DETACHED_MODE`, `META_MODE` with `META_TARGET`); the boot-pinned
copy the loop exports into every seat adds `FRACTAL_HEADLESS`, the run's launch
backend (`true`/`false`), so a seat's `fractal node start` inherits the backend:
the CLI's backend resolution reads the seat-exported `FRACTAL_HEADLESS` when the
flag is absent. `SCOPE_DIR` space-joins the scope roots when several are
configured, and the project and wiki paths nest under the project prefix for a
sub-project node.

Run-scoped variables have no static derivation -- the caller supplies them as
overrides, and overrides always win over the derived map. The loop passes its
live state: `STEP_LABEL`, `ITER_LABEL`, `ITER_TIMESTAMP`, `ITER_REF`,
`TIME_BUDGET`, `COST_BUDGET`, and the run-scoped mode flags (`CONTINUE_MODE`,
`RESUME_MODE`, `RESERVE_MODE`). A chat renders the same templates with an
explicit `N/A (chat)` sentinel for the label and budget fields -- a clear marker
rather than a blank or stale value -- and `false` for the run-scoped mode flags,
so no run-only mode document ever joins a chat prompt.

## Modes

Mode documents live in `fractal/_node/modes/`. A mode `<NAME>.md` joins the
prompt exactly when `<NAME>_MODE` is `true` in the merged map: the static modes
(detached, meta) derive from config, the run-scoped modes (continue, resume,
reserve) ride in on the loop's overrides. `SYNC.md` never joins the assembly --
sync runs as its own step. Chat turns are framed separately: the chat mode
document, preceded by the `NODE.md` charter only for a fresh chat (a forked chat
already carries the node's context).
