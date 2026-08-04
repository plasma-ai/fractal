---
name: configuration/config_json
desc: |
  Every key a node's config file carries: type, default, the init flag that
  sets it, which keys are immutable, and the surfaces for reading and
  retuning configuration after init.
created: 2026-07-21T04:48:38Z
updated: 2026-07-21T04:48:38Z
---

# configuration/config_json

[[_index|..]]

***

Every node carries a `config.json` in its `.fractal/<branch>/` data directory:
the persisted form of its [[configuration/node_init]] flags plus a few keys init
derives itself. The `fractal.core.config` module is the single schema -- it
enumerates the valid keys, their types, and the launch invariants -- and the
`Config` surface reads the file fresh from disk on every access (nothing is
cached), so edits from other processes are immediately visible. Writes are
atomic and serialized under a per-config lock, so concurrent setters never
revert each other's keys.

## Keys

Unset keys are stored as `null` and take their documented default at read time.
Except where noted, each key is set by the [[configuration/node_init]] flag of
the same name (dashes to underscores).

String keys:

- `title` -- display name. Default: the de-slugged node name.
- `project` -- project root relative to the repo (monorepo sub-project). Set by
  `--path`; **immutable**.
- `root` -- the tree's root (user) node branch, inherited from the parent at
  init; every node carries it so any node can resolve the tree's central
  database. Not settable by flag; **immutable**. See
  [[configuration/inheritance]].
- `base` -- branch the node started from instead of the parent's; also the
  squash-merge target when set.
- `meta` -- target node branch when this is a meta-configuration node.
- `agent` -- agent command. Default: inherited from the nearest ancestor.
- `provider` -- provider route. Default: vendor-native, inherited from the
  nearest ancestor.
- `model` -- model override. Default: the agent's own default.
- `effort` -- reasoning-effort override.

List keys (a JSON list of relative subdirectories -- never absolute, never
containing `..`; the setter accepts a comma- or space-separated string and
stores each entry in canonical path form (`./src`, `src/`, `a//b` land as `src`,
`a/b`), and a space-joined string form is tolerated on read):

- `scope` -- commit scope roots, resolved against the node's **project** root,
  which is the repo root unless `project` names a sub-project (a `src` root
  under project `app` bounds `app/src`). A lone `.` names the project itself:
  the commit boundary collapses to the project directory, exactly as an unset
  `scope` does.
- `clone_dirs` -- git-ignored build-cache directories copy-on-write cloned from
  the main checkout into each freshly spawned worktree, so a node starts warm
  instead of re-deriving them. Resolved against the **repo** root, and `.` is
  refused (it would clone the whole checkout). Read from the tree's user node
  only, and not settable by flag: set it after init with
  `fractal node config set clone_dirs=<dir>,<dir>`. Default: absent (no
  cloning). See [[architecture/worktrees]].

Boolean keys (must be JSON `true`/`false`/`null`):

- `user` -- marks the root (user) node. Set by `fractal init`, never by a flag;
  **immutable** -- flipping it would let a root branch be started as a loop.
- `sync` -- SYNC pass before each step. Default when unset: enabled.
- `detached` -- one agent invocation per step. Default: continuous.
- `local` -- skip pushing after commits. Latched at the init surface: a re-init
  cannot clear it, and children of a local parent spawn local.
- `blind` -- subscribe to no radio channels. Default: false.
- `sealed` -- hold the node's hosted mail out of its own seat's reads (empty
  listings, refused `radio read`) until unsealed. Default: false.

Integer cap keys (non-negative; `max_iters` strictly positive):

- `max_iters` -- per-run iteration cap. Default: unlimited.
- `max_depth`, `max_children`, `max_descendants` -- tree limits. Default:
  unlimited; `0` disables spawning.
- `step_retries` -- retries per failed step. Default when unset: `1`.

Duration keys (string with a unit suffix, e.g. `30s`, `10m`, `1.5h`; at least
one whole second):

- `timeout`, `iter_timeout`, `step_timeout` -- run, iteration, and step time
  budgets. Default: unlimited.
- `step_retry_backoff` -- delay before each step retry. Default when unset:
  `10s`.
- `interval` -- fixed iteration cadence. Mutually exclusive with `sleep`.
- `sleep` -- delay between iterations.
- `wait` -- pacing of the approval-wait loop. Default when unset: `1m`.

Cost keys (non-negative USD numbers):

- `max_cost` -- run ceiling. Set by `--max-cost`.
- `max_iter_cost`, `max_step_cost` -- per-iteration and per-step caps; both
  require `max_cost`.
- `reserve_budget` -- cleanup reserve, materialized to a USD amount at init (a
  percent flag value is resolved against `max_cost` and rounded). Default: 10%
  of `max_cost`; absent without `max_cost`.

## Validation

One merged validator guards every write path -- init flags, the config setters,
and the re-check at `fractal node start` (which covers direct file edits). It
rejects what the loop cannot run: non-numeric or non-finite costs, non-positive
ceilings, a per-iteration or per-step cap without `max_cost`, a reserve at or
above 99% of `max_cost`, a broken `step <= iter <= run` cost ordering,
non-integer or degenerate integer caps, non-boolean mode flags, bare-number or
sub-second durations, `interval` and `sleep` both set, `iter_timeout` exceeding
`interval`, absolute or `..` list-key entries (a scope root that never matches
the commit pipeline's relative prefix check, or a `clone_dirs` entry reaching
outside the worktree it warms), non-canonical list-key spellings (`./src`,
`src/` -- the setters store canonical form, so only a hand-edit lands one), and
a `.` cache dir, which would clone the whole checkout.

## Reading and writing after init

- `fractal node config get <key>` reads one value; unknown keys are refused
  rather than read as unset. Booleans print as `true`/`false`, lists one item
  per line.
- `fractal node config set <key>=<value> ...` writes values, confirming each
  `old -> new`. Keys are typed at the boundary: boolean, integer, and cost keys
  parse as JSON and are type-checked; everything else stores as a literal
  string; `key=null` clears a key. A multi-key set is atomic -- nothing lands if
  any key is rejected.
- `fractal config _get` / `fractal config _set` are the private equivalents used
  by the node scripts; `_set` skips the node-existence guard so init can write
  the very file the guard checks.
- `fractal node update <node>` is the supported retune path for a running child:
  it updates the registry row and the child's `config.json` together (title,
  cost caps, reserve, step timeout, and tree limits), and the loop picks the new
  caps up at its next iteration.

The three init-fixed keys -- `root`, `user`, `project` -- can never be changed,
and the operator-facing `config set` refuses even their first write.

## When changes take effect

The loop enforces the caps in `config.json`, so a retune or direct config edit
reaches a running loop without a restart -- the registry row is back-filled from
the config with a warning (config wins over a stale registry row). The cost caps
are re-read at run start, at each iteration top, and by the budget boundary
probes themselves, so a cap granted mid-iteration reaches the very next probe;
`max_iters`, `step_timeout`, and `wait` are re-read at each iteration top. A
malformed live edit warns and keeps the prior value rather than crashing the
run. Everything else is pinned when a run launches and holds for that run -- the
mode-composition and agent-preference keys (`sync`, `detached`, `meta`, `agent`,
`provider`, `model`, `effort`), the run and iteration time budgets and pacing
(`timeout`, `iter_timeout`, `interval`, `sleep`), and the retry knobs
(`step_retries`, `step_retry_backoff`) -- and applies from the next
`fractal node start`.
