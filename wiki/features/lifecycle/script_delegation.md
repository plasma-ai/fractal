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
  relaunch), then records `.pgid` and starts `_loop` in its own session.
- `pause.sh` reaps the recorded step process group, aborting the in-flight agent
  so the loop can park.
- `resume.sh` relaunches the loop through the backend the node's `.headless`
  marker records; the relaunched loop withdraws the run's recorded pause signals
  itself as it adopts the run, so a bare resume works even after a node
  transplant. Its still-parking session guard runs only without the `.headless`
  marker -- a headless node owns no session, so a same-named session from
  another repo sharing the basename never blocks its resume; a headless node's
  own still-parking loop is refused by the relaunch's group vet instead.
- `kill.sh` reaps the node's live process groups — the in-flight agent's
  recorded step group and the tmux pane's or, with no pane, the recorded `.pgid`
  group of a headless loop — escalating a polite termination to a forced one,
  then destroys the tmux session. When nothing is alive (a paused park), it
  exits cleanly and the kill is pure bookkeeping.
- `delete.sh` removes one node's worktree, local branch, and remote branch; the
  recursive delete calls it once per node, deepest first.

## Marker files

The loop and its scripts coordinate through small marker files in the node
directory, all git-ignored via the repository's exclude file (kept in lockstep
with the marker set): `.status` (the current status), `.session` and `.socket`
(tmux coordinates), `.headless` (the selected detached backend), `.pgid` and
`.step_pgid` (process groups for liveness and pause/kill reaping), `.paused`
(the tree-wide pause latch beside the central database) and `.pause_abort`.
Signals the loop observes — finish, stop, pause — take effect at iteration or
step boundaries; the escalation path that does not wait is kill.

Liveness is one law (`Node._loop_exists`). A `.headless` node is judged by its
recorded `.pgid` process group alone and tmux is never asked, so a host without
tmux still heals it. Any other node asks tmux on its recorded `.socket`; a
definitive "no such session" is proof only when a socket was recorded, and a
socket-less loop (a bare `fractal node _loop` launch) is judged by its live
group instead. The group probe compares the leader's start instant with the
`.pgid` record's timestamp to fence PID reuse, and arbitrates a group owned by
another user the same way. Only a failed `ps` is inconclusive: reconciliation
keeps the run active, teardown refuses, kill refuses and names the `ps -p` check
and the record to clear, and reaping spares any group it cannot positively
identify.
