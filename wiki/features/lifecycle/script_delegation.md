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
uniform and per-deployment customization has a place to land. The runner
forwards an interrupt it receives (SIGINT) to the running script and waits for
it, so a script's own trap — `merge.sh`'s target restore, or its post-commit
finish of the node's worktree update — runs instead of the script dying
mid-step.

## The lock boundary

Status flips and slot checks happen under the `.worktrees` file lock: per-node
helpers re-check their guards under that lock so concurrent fan-outs serialize,
and flips are atomic. Scripts that perform slow work (git worktrees, remotes)
run *outside* the lock so they do not hold up lifecycle operations elsewhere in
the tree. `start.sh` is the bounded exception: start holds the lock through its
runtime handoff so fresh, continued, tmux and headless launches cannot overlap.
`merge.sh` runs under a separate repo-wide merge flock
(`.worktrees/.merge.lock`) — separate because the script shells back into
`fractal` verbs that take the `.worktrees` lock — so two merges into one target
queue instead of interleaving their index writes.

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
- `pause.sh` reaps the in-flight agent's whole process tree from the recorded
  step process group (see [[#Reaping an agent's process tree]]), aborting the
  agent so the loop can park.
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
  group — together with every process their trees hold, escalating a polite
  termination to a forced one, then destroys the tmux session. The teardown is
  per-backend like `resume.sh`'s guard: under the `.headless` marker the pane
  lookup, session check, and session destroy are all skipped and only the
  recorded groups are reaped, so a same-named session from another repo sharing
  the basename is never cross-fired. When nothing is alive (a paused park), it
  exits cleanly and the kill is pure bookkeeping.
- `merge.sh` squash-merges the node's branch into its target from inside the
  target's worktree: it restores the target's `.fractal/` outside the node's
  scope roots, strips the node's own seed from a user-node target, judges the
  staged squash through the private `node _scope` (NUL-separated paths on stdin,
  the out-of-scope ones on stdout, exit 1 when any is out of scope — the same
  `Scope` law `fractal commit` enforces), refreshes the wiki indexes, commits,
  and records the target's post-squash tree on the node's branch; the operator's
  view of the merge is [[user_flow/finishing/_index|user_flow/finishing]].
- `delete.sh` removes one node's worktree, local branch, and remote branch; the
  recursive delete calls it once per node, deepest first.

## Reaping an agent's process tree

Agent CLIs run each tool command in a process group of its own — some in a
session of their own — so they can time it out. Signaling the recorded step
group alone misses those groups, and once the agent dies they live on,
reparented to init. Every path that ends an agent invocation therefore reaps its
whole tree through one helper, `fractal.util.process.reap`: `pause.sh` and
`kill.sh` call it through the private
`fractal node _reap --pgid=<pgid> [--worktree=<dir>]`; the loop calls it
in-process when a step's deadline fires and when its stream reader dies with the
agent still running; the crashed-but-active heal calls it on a dead loop's
recorded groups; and codex's preflight calls it when its model probe times out.

The helper finds the tree before sending any signal. The agent leader starts its
own session, so every process whose session id is the recorded group id belongs
to the invocation; a session id survives reparenting, so this finds orphans too.
A walk down parent links from the leader and every member then catches tools
that left the session while their parent is alive, and any session a member
leads joins the match. The helper TERMs every member group, re-walks, and TERMs
any group the walk newly finds, for up to five rounds; it waits out the grace
(five seconds from the scripts, the loop's own grace for a deadline, two seconds
when the stream reader dies — enough for a tool's `git commit` to drop
`.git/index.lock` before the force-commit backstop needs it — two seconds for
the crash heal, and the probe's own grace for a codex preflight timeout) while
still signaling late groups, then KILLs every group with a live member the same
way. Members still alive after that are named on stderr, which `Node` relays as
warnings — the survivors on the pause or kill output, and a `Warning:` line on
stderr for the in-process paths.

`pause.sh` hands the reap its `.pause_abort` marker, which the reap touches just
before its first signal to the step group: the loop still finds the marker
before the agent dies, and a step the reap spares (a self-pause) is never
marked, so a later unrelated failure is never read as a park.

`kill.sh` passes the loop's recorded group beside the step group, so the loop
dies in the same round as its agent and cannot launch the next step while the
agent's tree drains. The crash heal passes the same two groups, each only after
its identity check proves it the recorded one. Pause passes only the step group:
the loop sits above the tree and must live to record the park.

Pause and kill also pass the node's worktree for a report-only sweep: each
process outside the tree whose working directory lies in the worktree is named
on stderr as left running and never signaled. A double-forked daemon that left
the session after its parent exited cannot be tied to the invocation, so the
sweep is how the operator learns of it. The same holds for the one process the
walk cannot reach: an orphan from an earlier tool call whose own session's
leader has exited (`validate.sh &` under a `setsid` shell that already returned)
sits in no member's session and under no member, so the sweep names it and
leaves it running. The sweep reads `lsof` on macOS and `/proc/<pid>/cwd` on
Linux, and it skips the caller and its ancestors (an operator may run
`fractal pause` from a shell in the worktree) and the processes the tree hangs
from (the loop and its parents).

Four rails bound the reap. It never signals pid 0 or 1, the caller's own group,
or the group of any of the caller's ancestors — so an agent that pauses its own
node keeps the groups it runs in, its step finishes, and the loop parks at the
next checkpoint. It never signals another node's loop group, nor walks through
one: a node the agent launches headless (`fractal node start` runs the child's
loop in a new session) stays a child of the agent's tool while its launcher runs
— a tmux launch forks the loop from the tmux server, outside the agent's tree —
so the reap spares every group recorded in another registered node's `.pgid`,
everything in a session one of its processes leads, and everything below it, and
names each spared group it reached. The record list comes from the registry by
path alone (no git, so a sibling whose worktree no longer resolves is skipped,
never fatal), and both the list and the records are rebuilt before every signal
round, so a launch that lands mid-reap is honored. It signals only groups found
through the recorded group's session or tree. And when the first process table
cannot be read, it falls back to signaling the recorded groups alone (minus the
caller's own group, not its ancestors', which no table names -- so a self-pause
from a tool shell in a group of its own can hit the step group, as `pause.sh`'s
own fallback does); when a later read fails, it keeps signaling the groups it
already knows, minus the protected and spared ones, probing them by signal
instead of re-walking; the loop, the crash heal, and codex's preflight warn and
fall back to a plain group KILL if the reap itself breaks; when
`fractal node _reap` itself cannot run, `pause.sh` and `kill.sh` fall back to
their own group-only teardown, so a helper failure never fails the pause or
kill.

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
and pause/kill reaping — the step marker also names the codex preflight probe's
group for the probe's lifetime, which `kill` reaps by that handle and a later
boot, finding it live under an idle node, reaps as an orphan through the crash
heal's tree reap before recording its own), `.pgid.lock` (the launch handoff's
flock sidecar), `.paused` (the tree-wide pause latch beside the central
database) and `.pause_abort`. Signals the loop observes — finish, stop, pause —
take effect at iteration or step boundaries; the escalation path that does not
wait is kill.

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
record's timestamp to fence PID reuse — a group that outlived its leader is
proven by a surviving member, never by the id alone, since a reaped group's id
still answers `killpg` — and arbitrates a group owned by another user the same
way. Only a failed `ps` is inconclusive: reconciliation keeps the run active,
teardown refuses, kill refuses and names the `ps -p` check and the record to
clear, and reaping spares any group it cannot positively identify. The scripts'
own `kill -0` checks are handle-selection gates, never identity verdicts —
identity is judged only by this Python law, `Node._kill`'s flock'd vet included.

The crashed-but-active heal holds no flock over its probe, so it fences its own
writes instead: it fingerprints `.pgid`/`.step_pgid` before probing, re-verifies
at act time that the status is still `active` and the records unchanged, reaps
only that judged snapshot (each live, identity-checked group with its whole
process tree, so tools the dead loop's agent started die too), and re-checks its
license under the `.worktrees` flock after the reap — status still `active` and
no record on disk the verdict never judged — before stamping `exited`. A kill or
continue landing mid-heal keeps its stamp, and a relaunch racing the probe
stands the heal down.
