---
name: features/lifecycle/script_delegation
desc: |
  How lifecycle operations delegate their filesystem and process work to
  the shell scripts in the node machinery, and where the lock boundary
  between database transitions and script execution sits.
created: 2026-07-21T04:58:40Z
updated: 2026-07-21T04:58:40Z
---

# features/lifecycle/script_delegation

[[features/lifecycle/_index|..]]

***

Every lifecycle operation on a node splits into two halves: a guarded database
transition, and filesystem/process work delegated to a shell script shipped in
`fractal/_scripts/`. These lifecycle scripts are distinct from the per-node
`setup.sh`/`test.sh`/`lint.sh` ([[configuration/scripts]]). The node object runs
each script through a shared subprocess runner — every lifecycle method has a
corresponding script, even when that script is a no-op hook, so the seam stays
uniform and per-deployment customization has a place to land.

## The lock boundary

Status flips and slot checks happen under the `.worktrees` file lock: per-node
helpers re-check their guards under that lock so concurrent fan-outs serialize,
and flips are atomic. Scripts that perform slow work (git worktrees, remotes)
run *outside* the lock so they do not hold up lifecycle operations elsewhere in
the tree. `start.sh` is the bounded exception: start holds the lock through its
runtime handoff so fresh, continued, tmux and headless launches cannot overlap.

## What the scripts do

- `start.sh` launches the run's tmux session or, with `--headless`, a detached
  process group whose output lands in `headless.log`; the loop itself is
  in-process Python (`fractal node _loop`). The tmux path re-enters `start.sh`
  inside the new session and execs `_loop` there; the headless path hands off to
  the private `node _launch`, a thin front for `Node._launch_headless`, which
  refuses while a still-live recorded `.pgid` group holds the node (the
  identity-checked liveness law, so a recycled process-group id never blocks a
  relaunch), then serializes the handoff under a launch flock and claims `.pgid`
  with an exclusive create before spawning — of two launches racing the same
  dead-or-absent record, exactly one boots and the loser refuses as it would
  over a live record (the flock'd clear re-vets the record, so a winner's pid
  landed since a loser's stale vet refuses rather than sweeps). It records the
  `.headless` marker and `.pgid` together (rolled back together when the spawn
  fails, and a spawn that exits before the loop boots is reported as a failed
  launch) and starts `_loop` in its own session, appending one launch banner to
  `headless.log`. The loop's own boot re-vets the recorded group under the same
  identity law — its launcher's own record proceeds, a live rival or a fresh
  pid-less claim refuses, and an unverifiable group refuses naming the `ps`
  check — so a bare `fractal node _loop`, which runs none of the launch gates
  above, still cannot boot over a live loop.
- `pause.sh` reaps the recorded step process group, aborting the in-flight agent
  so the loop can park.
- `resume.sh` relaunches the loop through the backend the node's `.headless`
  marker records — the same record an unflagged `start --continue` reuses; the
  relaunched loop withdraws the run's recorded pause signals itself as it adopts
  the run, so a bare resume works even after a node transplant. Its
  still-parking session guard runs only without the `.headless` marker — a
  headless node owns no session, so a same-named session from another repo
  sharing the basename never blocks its resume; a headless node's own
  still-parking loop is refused by the relaunch's group vet instead.
- `kill.sh` reaps the node's live process groups — the in-flight agent's
  recorded step group and the tmux pane's or, with no pane, the recorded `.pgid`
  group — escalating a polite termination to a forced one, then destroys the
  tmux session. The teardown is per-backend like `resume.sh`'s guard: under the
  `.headless` marker the pane lookup, session check, and session destroy are all
  skipped and only the recorded groups are reaped, so a same-named session from
  another repo sharing the basename is never cross-fired. When nothing is alive
  (a paused park), it exits cleanly and the kill is pure bookkeeping.
- `delete.sh` removes one node's worktree, local branch, and remote branch; the
  recursive delete calls it once per node, deepest first.

## Launch environment

`start.sh` guarantees the same explicit variables on both arms, forwarded via
`env(1)` argv: `_NODE` (which also drives the re-entry exec), a venv-prefixed
`PATH`, and `VIRTUAL_ENV`; the headless arm adds `FRACTAL_HEADLESS=true`, the
record seats re-export so delegated child starts follow the parent's backend.
The tmux arm additionally forwards `OPENROUTER_API_KEY` and `XAI_API_KEY` via
`new-session -e`, on a warm server running tmux >= 3.2 only — a cold
`new-session` becomes the server and would keep any `-e` pair in its `ps(1)`
argv for its whole lifetime, which is why only targeted keys ever cross this way
(a cold server inherits the exported keys into its argv-invisible global
environment instead). Everything else follows the backend: a headless loop
inherits the launching seat's full environment, while a tmux loop gets the
server's global environment — a cold server snapshots the launching shell, a
warm server keeps its own start-time environment — plus the forwarded keys. The
practical consequence is that provider config homes (`CLAUDE_CONFIG_DIR`,
`CODEX_HOME`, `GROK_HOME`) reach agents headless but not through a warm server
that predates them; the remedy is killing the tmux server or launching headless,
and if a workflow ever needs such a variable on warm servers, the surgical fix
is adding it to `start.sh`'s `-e` forwarding list. `$TMUX` itself rides headless
and bare loop environments untouched but is never trusted: the `.headless`
marker and the boot-time ownership probe are authoritative, so scrubbing it
would only blind the ownership probe for legitimate tmux boots.

## Marker files

The loop and its scripts coordinate through small marker files in the node
directory, all git-ignored via the repository's exclude file (kept in lockstep
with the marker set): `.status` (the current status), `.session` and `.socket`
(tmux coordinates — a headless boot joins no server, records no socket, and
drops a stale record; any other boot under `$TMUX` records that server only
after confirming it lists the node's own session, so a launcher-driven boot
always passes — `start.sh` creates the session first — while a bare in-pane
launch or an inconclusive probe records nothing and the loop is judged by its
group), `.headless` (the node's backend record — written by the headless
launcher beside `.pgid`, it outlives the run, survives heals and kills, and only
a tmux launch clears it), `.pgid` and `.step_pgid` (process groups for liveness
and pause/kill reaping), `.pgid.lock` (the launch handoff's flock sidecar),
`.paused` (the tree-wide pause latch beside the central database) and
`.pause_abort`. Signals the loop observes — finish, stop, pause — take effect at
iteration or step boundaries; the escalation path that does not wait is kill.

Liveness is one law (`Node._loop_alive`). A `.headless` node is judged by its
recorded `.pgid` process group alone and tmux is never asked, so a host without
tmux still heals it. Any other node asks tmux on its recorded `.socket`, and a
listed name is arbitrated by its panes' argv — a session whose launch pane
provably names another repo's worktree (a repo sharing the basename and branch)
reads as the node's own session being gone, while a pane the probe cannot
attribute, or one running no launch at all, keeps the listed answer; a
definitive "no such session" is proof only when a socket was recorded, and a
socket-less loop (a bare `fractal node _loop` launch, in or out of a tmux pane)
is judged by its own group instead — tmux's "no such session" defers to a live
or unverified group, and with no tmux answer at all the recorded group is the
whole answer, while a socket-less node with no `.pgid` record then stays
unknown. The group probe compares the leader's start instant with the `.pgid`
record's timestamp to fence PID reuse, and arbitrates a group owned by another
user the same way. Only a failed `ps` is inconclusive: reconciliation keeps the
run active, teardown refuses, kill refuses and names the `ps -p` check and the
record to clear, and reaping spares any group it cannot positively identify. The
scripts' own `kill -0` checks are handle-selection gates, never identity
verdicts — identity is judged only by this Python law, `Node._kill`'s flock'd
vet included.

The crashed-but-active heal holds no flock over its probe, so it fences its own
writes instead: it fingerprints `.pgid`/`.step_pgid` before probing, re-verifies
at act time that the status is still `active` and the records unchanged, reaps
only that judged snapshot, and re-checks its license under the `.worktrees`
flock after the reap — status still `active` and no record on disk the verdict
never judged — before stamping `exited`. A kill or continue landing mid-heal
keeps its stamp, and a relaunch racing the probe stands the heal down.
