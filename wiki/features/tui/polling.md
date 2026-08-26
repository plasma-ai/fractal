---
name: features/tui/polling
desc: |
  How the cockpit stays live: cheap mtime change detection over the tree,
  an off-thread snapshot builder with per-branch caches, and panes that
  render only from the immutable snapshot.
created: 2026-07-21T05:08:54Z
updated: 2026-07-21T05:08:54Z
---

# features/tui/polling

[[features/tui/_index|..]]

***

The cockpit never asks the user to refresh; a poll loop keeps every pane
current. Liveness is layered so that a steady tick costs almost nothing and a
busy tree never blocks a keystroke.

## Change detection

`fractal.tui.poller` stat-polls a handful of mtimes instead of opening the
database: the central database file and its WAL sidecar (the tree's database
runs in WAL mode, so every write anywhere touches one of them), plus each
watched branch's `.status` file and `config.json`. That detects change across a
whole tree in about a millisecond. A config retune writes only `config.json` --
no database row, no status transition -- so watching the file itself is the only
way the cockpit sees it. A central-database write reports **every** watched
branch as changed (attributing it per branch would itself need reads); a branch
that vanished is also reported so its sections drop.

## Snapshot builds

`fractal.tui.snapshot` turns change reports into an immutable snapshot -- the
one object every pane renders from; renderers never touch the database layer.
Each build re-reads only the sections whose branch changed on disk (per-branch
caches keyed by the poller's tokens) and returns the previous snapshot object
untouched when nothing changed, so a steady tick runs zero queries and panes
skip rebuilds by comparison. A tick launches at most one off-thread build;
results land as messages back on the UI thread, so keys never wait behind a
build. Runtime liveness is reconciled on every build through the same
process-group probe core uses: a tmux loop by the batched session set, a
headless or socket-less loop by its recorded group; a definitively missing or
recycled runtime renders `exited`, while a group whose identity probe is
inconclusive stays `active`.

## The read stack

`fractal.tui.data` is the cockpit's only database surface: read-only SQL over
the central database, one short-lived connection per uncached section. It
deliberately keeps node objects off the read path -- their path properties shell
out to git, which at tree scale would dominate a tick; worktree paths resolve
once (a single batched git listing) and cache per branch. Nothing in the read
stack ever writes -- even feed and read calls that would stamp read state are
excluded; writes live solely in [[features/tui/actions]].
