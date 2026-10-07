---
name: architecture/database
desc: |
  The central per-tree SQLite database: where it lives, how every node
  resolves it, the WAL discipline, row ownership, what survives node
  deletion, and the main table families.
created: 2026-07-21T04:47:26Z
updated: 2026-07-21T04:47:26Z
---

# architecture/database

[[_index|..]]

***

Each node tree has exactly one SQLite database, and every node in the tree —
regardless of depth or worktree — reads and writes that one file. It is the
shared ground truth for the registry, execution accounting, lifecycle events,
and radio messaging.

## Location and resolution

The database lives in the root (user) node's data directory:
`.fractal/<root-branch>/.db` at the repo root, or under the project sub-path for
a monorepo sub-project. Every node's config carries a `root` key, written at
init and inherited from the parent; a node resolves the database by combining
that key with the root's entry in the `.worktrees/.project/` cache — no worktree
lookup involved, so resolution works the same from any process (CLI, loop, TUI).

The access layer is deliberately thin: a generic table-operation wrapper with no
domain logic, opening a one-shot handle per operation unless a caller passes an
explicit transaction connection to make a multi-statement block atomic. Every
handle enables foreign keys and carries a generous busy timeout, so contending
writers under wide node fan-out wait for the lock instead of failing fast. The
schema is applied idempotently from `core/schema.sql` — additive `IF NOT EXISTS`
DDL with a stamped schema version (`PRAGMA user_version`). A column added to a
table reaches an existing database through a version step: every handle reads
the stamp on open and a database stamped behind the current version is lifted
one step at a time, each step inside its own immediate transaction so two
writers racing it serialize and the second finds it done, each checking what it
adds before adding it, and each left at its old version by SQLite's
transactional DDL if it fails. A changed view still needs a rebuild.

Row accounting on top of the wrapper goes through the record surface in
`fractal/core/record.py`: lifecycle transitions funnel through first-writer-wins
fenced updates, events are point-in-time log entries, and signals are consumable
control rows. How the loop writes its run, iteration, and step rows through this
surface is on [[features/loop/accounting]].

## WAL discipline

The journal mode is WAL, stamped once at database creation: the mode persists in
the file header, and the delete-to-WAL transition takes an exclusive lock, so it
must never run per-connection under a live fleet. The `-wal`/`-shm` sidecars
beside the `.db` persist by design — fractal's writable handles disable SQLite's
close-time checkpoint, because a last-closing writer that checkpoints and
unlinks the sidecars strands write-denied (sandboxed) readers until a writer
rebuilds the WAL index.

Consolidation happens instead at quiet boundaries. SQLite's autocheckpoint
backfills the log as it grows, and an exiting loop runs an explicit truncating
checkpoint — best-effort, so a busy sibling fleet defers it to the next exiting
loop — which backfills the WAL into the main file and zeroes the log while
leaving both sidecars in place. Database creation ends with the same checkpoint,
so a fresh tree's `.db` is self-contained at rest rather than a bare header page
whose tables live only in the sidecar.

## Row ownership

Every row table carries a `node` column naming the row's owner — the node whose
registry entry, run, step, event, or mailbox the row belongs to. On message
rows, `sender` is always the message *author*, distinct from the owning node (a
message in one node's inbox was written by another). This two-column convention
is what lets one flat database serve the whole tree: any node's view is a filter
on `node`, and attribution never depends on which mailbox a row sits in.

## Table families

- **Registry** — the `nodes` table: one row per node with branch, title,
  lifecycle status, and the cost and tree-shape caps (see
  [[architecture/node_tree]]).
- **Execution accounting** — `runs`, `iters`, and `steps`: one row per run, per
  iteration within a run, and per step within an iteration. Rows carry start and
  end instants (duration is derived, never stored), status, exit code, and — on
  step rows — the agent, served model, and launch effort, the recorded cost, and
  the approval state, so cost aggregates roll up from steps.
- **Events** — point-in-time lifecycle entries (init, spawn, commit, merge,
  pause, resume, and the rest), each optionally pinned to the run, iteration,
  and step context it happened in.
- **Signals** — consumable control rows (finish, stop, and kin) a running loop
  picks up.
- **Radio** — `messages` plus its satellites: `archive` (per-node saved copies),
  `channels`, `subs` (subscriptions), `reacts`, and `reads` (read receipts).

An `activity` view unions run, iteration, and step starts and ends with the
events log into one timeline, which is what the activity listing and the TUI
render.

## What survives deletion

Registry rows and history rows have different lifetimes. Deleting a node (and
its subtree) clears only the registry rows and subscriptions; all history rows —
runs, iterations, steps, events, messages — persist in the central database.
Merge events are logged on the merge *target*, so even the record of a child's
integration outlives the child. A tree-level `fractal reset` keeps the user
node's data directory, database included; only `fractal destroy` removes the
database itself — the named tree's with `destroy <tree>`, every tree's with
`destroy --all`, the full inverse of `fractal init`. The durability reasoning
behind these tiers lives in the [[design/_index|design]] branch.
