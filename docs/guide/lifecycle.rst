Node Lifecycle
==============

Every agent node carries a one-token lifecycle status, stored in the node's
``.status`` file and mirrored to its row in the central ``nodes`` registry.
Lifecycle commands move a node between statuses; this page covers the status
set, what each verb does and refuses, pause as a parked state, retirement,
crash reconciliation, and the three teardown tiers. The loop that drives an
``active`` node is described in :doc:`/guide/loop`; the full command surfaces
live in :doc:`/cli/node` and :doc:`/cli/fractal`.

The status set
--------------

One shared vocabulary covers the node ``.status`` file, the ``nodes``
registry, and every history row (runs, iterations, steps, events):

.. list-table::
   :header-rows: 1
   :widths: 15 85

   * - Status
     - Meaning
   * - ``idle``
     - Initialized but not running. The default: a node with no ``.status``
       file reads ``idle``. Also the re-armed state a continue passes
       through before its loop boots.
   * - ``active``
     - The iteration loop is running in the node's tmux session.
   * - ``paused``
     - Parked mid-run by ``pause``. The loop has exited — no tmux session
       while parked is *normal* — and the run and iteration rows stay open
       for ``resume`` to adopt.
   * - ``completed``
     - The loop landed cleanly: a drained ``finish`` signal, or the last
       iteration under ``max_iters`` succeeded.
   * - ``stopped``
     - The loop honored a ``stop`` signal and ended after its current step.
   * - ``exited``
     - The loop ended without completing: a budget landing, a timeout, a
       setup abort, or a crash healed by reconciliation.
   * - ``killed``
     - Ended immediately by ``kill``.
   * - ``failed``
     - Row-only: a failed step, or the event row a refused lifecycle verb
       leaves behind. Never a node status, and never a run status — a run
       that dies is ``exited``, not ``failed``.
   * - ``retired``
     - Parked out of sight by ``retire``: hidden from listings, unstartable,
       branch and history kept.

Not every status applies at every level:

- **Node-only:** ``idle`` and ``retired`` never appear on run, iteration, or
  step rows.
- **Row-only:** ``failed`` never appears in a node's ``.status`` file.
- **User node:** the root node has no status at all. It is marked by the
  ``user: true`` config key (identity, not lifecycle), runs no loop, and has
  no row in the registry.

Exit codes on run rows are binary and derived from outcome. One case is worth
memorizing: **a budget-ended run is ``exited`` with exit code 0** — the
budget landing is a designed stop, not a crash, but it is also not a goal-met
completion. Every other ``exited`` path keeps exit code 1.

Settled and unsettled
~~~~~~~~~~~~~~~~~~~~~

The statuses ``active``, ``paused``, and ``idle`` are *unsettled*: each holds
one spawn slot against the ``max_children`` and ``max_descendants`` caps, so
those caps bound concurrency, not lifetime spawn count. A settled node
(``completed``, ``stopped``, ``exited``, ``killed``) or a ``retired`` one
frees its slot automatically. Anything that returns a node to the unsettled
pool — ``start --continue``, an ``unretire`` that restores ``idle`` —
re-checks the width and descendant gates first, and there is no override
flag.

Display decorations
~~~~~~~~~~~~~~~~~~~

``fractal node status`` and ``fractal node list`` decorate the bare status in
some situations. The decorations are display-only — the stored status stays
bare, and ``list --status`` filters match on the first space-delimited token:

- ``active (pausing)`` / ``active (stopping)`` / ``active (finishing)`` — a
  pause, stop, or finish signal is pending on an active node.
- ``exited (<reason>)`` — the latest run row recorded why it ended (a
  reconciliation-healed crash records no reason and stays bare).
- ``orphan`` / ``<status> (orphaned)`` — a registry row whose worktree was
  removed out of band (listings only; never stored).

Transitions at a glance
~~~~~~~~~~~~~~~~~~~~~~~

- ``idle`` → ``active``: ``node start`` (the loop stamps ``active`` at boot,
  after its preflight).
- ``active`` → ``completed`` / ``stopped`` / ``exited``: the loop's own
  landing — a drained finish or clean ``max_iters`` run, a stop signal, or a
  timeout / budget end / setup abort / healed crash.
- ``active`` → ``paused``: ``node pause`` (the loop parks).
- ``paused`` → ``active``: ``node resume`` (the relaunch adopts the open
  run).
- ``active`` | ``paused`` | booting ``idle`` → ``killed``: ``node kill``.
- settled → ``idle`` → ``active``: ``node start --continue``.
- any non-``active``/``paused`` status → ``retired``: ``node retire``;
  ``retired`` → the pre-retire status (else ``idle``): ``node unretire``.

Lifecycle commands
------------------

Most ``fractal node`` verbs take an optional node argument (the target
branch; a unique trailing segment like ``c1`` works as a short name) and a
``--path`` option naming the caller's worktree (default ``.``). Refused
signal verbs (``finish``, ``stop``, ``pause``, ``kill``) are also recorded —
a ``failed`` event row whose metadata names the reason — so the event log
carries the attempt.

init
~~~~

``fractal init [path]`` creates the **user (root) node** on the current
branch: its data directory with ``config.json``, the central database, the
radio, and the project wiki. The user node has no lifecycle status and cannot
be started, merged, deleted, or retired. Init refuses a detached ``HEAD``, a
branch containing ``/``, a path under ``.worktrees/``, a branch already
mapped to a different project, and a live tmux session belonging to another
fractal that shares the repository's directory basename. See
:doc:`/guide/getting-started` for the full setup flow.

``fractal node init <name>`` creates an agent node: a git branch
``<parent>.<name>``, a worktree under ``.worktrees/``, and a seeded data
directory — and leaves it ``idle`` with an ``init`` event on the record. The
spawn is gated: it refuses under the pause latch (any paused or pausing
ancestor, or a tree-wide pause), and it enforces the parent's live
``max_children`` cap and remaining budget, plus the ``max_depth`` and
``max_descendants`` caps of every ancestor. An
existing node refuses without ``--reset``, and ``--reset`` itself refuses
over an ``active`` or ``paused`` node — a live loop's files or a paused
node's frozen run context would be wiped irrecoverably. The full option
surface (agents, budgets, timeouts, scope, inheritance) is covered in
:doc:`/configuration` and :doc:`/cli/node`.

start
~~~~~

.. code-block:: console

   $ fractal node start parser
   $ fractal node start parser --continue --max-cost 10

``fractal node start [node]`` launches the node's iteration loop in a
detached tmux session. All run parameters come from the node's
``config.json``; the launch itself takes only these flags:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Flag
     - Meaning
   * - ``--continue``
     - Re-arm a settled node (``completed``, ``stopped``, ``exited``, or
       ``killed``) for further iterations. The launch restores the worktree
       and the loop opens a fresh run.
   * - ``--clean``
     - Requires ``--continue``. Acknowledges that the continue's worktree
       restore discards uncommitted project files; without it a dirty
       worktree refuses and lists the doomed paths (node-dir files are
       exempt).
   * - ``--max-cost <usd>``
     - Requires ``--continue``. Retunes the per-run cost cap before the
       relaunch — applied through the parent's retune and echoed
       ``max_cost: old -> new``. **Required** when the last run ended on
       its cost budget.

A fresh start requires the status to be exactly ``idle``; anything else
refuses with a pointer to ``--continue``. Both forms re-validate the stored
config and resolve the agent before launching, so a bad hand-edit fails at
the command line instead of inside a dying tmux pane. Start also refuses:

- a user node, a ``retired`` node ("Unretire it first"), and a ``paused``
  node ("Resume it first");
- any launch under the pause latch (a paused or pausing ancestor, or the
  tree-wide latch);
- a fresh start when a foreign tmux session already holds this node's
  session name (another fractal sharing the repository basename — rename a
  repository directory, never kill the foreign session);
- a stored non-positive ``max_cost``;
- a bare ``--continue`` after a **budget-ended** run: the error names the
  spent and armed figures and requires an explicit ``--max-cost`` to
  re-authorize spend. A refused or failed continue rolls the retune back.

A start with no ``max_cost`` at all is allowed but warns loudly — spend is
then untracked, bounded only by ``max_iters`` and ``timeout``. Continuing
from ``killed`` surfaces the recorded kill attribution as a notice. Every
successful launch logs a completed ``start`` event (metadata ``continue`` on
continues), so the event log carries the node's restart chain.

While a node is ``active``, ``fractal node attach [node]`` attaches to its
tmux session (it refuses any other status).

finish
~~~~~~

``fractal node finish [node] [--reason <text>]`` asks the loop to stop
gracefully **after its current iteration** — the loop completes the
iteration, then lands ``completed``. The signal fans out to every active
descendant, children first, so a subtree drains from the bottom up. On the
user node, ``finish`` is the tree-wide broadcast: every active node in the
tree is signaled, with no self signal (the user node has no loop).

``--cancel`` withdraws this node's own pending finish signal — descendants
are untouched, since finishing is a descendant's normal completion path —
and refuses when no finish signal is set. ``--reason`` rides the signal, the
event, and the propagated fan-out attribution.

The target must be ``active`` and have a run; a settled target refuses
before any descendant is signaled.

The sweep re-enumerates until no fresh live descendant appears, so a child
whose ``start`` was still in flight when it began is signaled too rather
than escaping the pass. What the re-read cannot catch, the child does from
its own end: a loop that boots while an ancestor still carries a pending
``finish`` or ``stop`` adopts that signal onto its own fresh run and honors
it at its first boundary, announcing it on the pane. (A tree-wide broadcast
from the user node records no signal of its own, so a child booting after
that sweep has no ancestor row to adopt — re-enumeration alone covers it.)

stop
~~~~

``fractal node stop [node] [--reason <text>]`` is the same shape as
``finish`` but ends the loop **after its current step**, landing the node
``stopped``. Same children-first fan-out over the entire subtree, same
tree-wide broadcast on the user node, same ``active``-with-a-run
requirement. There is no ``--cancel`` for stop.

The signal is a queued row, not a process signal: a stop that lands while a
step's agent is in flight waits for that seat to complete -- the step books
its real outcome and only the steps after it are forgone. A stop never
TERMs the running agent; when an immediate end is genuinely needed, that is
``kill``.

pause
~~~~~

``fractal node pause [node] [--reason <text>]`` parks the node and its
active descendants immediately but recoverably — see
`Pause: the parked state`_ below for the full semantics. Unlike every other
signal, the fan-out is **parent-first**, so a parent parked before its
children can never drain-complete over them. The target must be ``active``
with a run.

``fractal pause [path] [--reason <text>]`` is the tree-wide brake: it
latches the root first, then pauses every active node in the tree.

resume
~~~~~~

``fractal node resume [node]`` relaunches a paused subtree **leaf-first**
(deepest first), so every child reads ``active`` again before a parent's
drain-wait looks at it. Each parked loop adopts its open run where the pause
left it. A node still parking — ``active`` with a pending pause signal —
gets its pause withdrawn instead, so the live loop never parks. Resume
refuses a target that is not paused (and not parking), and a target under a
still-paused ancestor or the tree-wide latch — resume the latching node
instead, which relaunches the subtree with it.

``fractal resume [path]`` is the tree-wide release: it lifts the root latch
first (new starts and spawns become legal immediately), withdraws pending
pauses, and relaunches every parked loop leaf-first.

kill
~~~~

``fractal node kill [node] [--reason <text>]`` ends a node immediately: it
reaps the tmux session and the recorded process groups (TERM, a five-second
grace, then KILL) and closes the node's open rows as ``killed``. Killable
states:

- ``active`` — the normal case;
- ``paused`` — the escape hatch for a parked subtree; with no loop alive the
  kill is pure bookkeeping (the open rows close ``killed``);
- ``idle`` — a boot in flight that has not stamped ``active`` yet, or a
  never-started spawn: the kill stamps ``killed`` so the node can never
  activate (an unwanted spawn is reapable before it starts burning).

Anything else refuses (``Cannot kill: node is not active, paused, or
idle``). The
sweep reaps descendants first and re-enumerates to a fixpoint, so a spawn
already in flight when the kill lands is caught rather than escaping. The
attribution — ``killed by <actor>``, with the reason appended — lands
identically on the kill event, the signal, and the killed run row, and a
later ``start --continue`` surfaces it as a notice.

Pause: the parked state
-----------------------

Pause is designed for "stop everything, lose nothing": an emergency brake
that freezes mid-step work in a resumable form.

What pausing does
~~~~~~~~~~~~~~~~~

The pause signal lands first; then the in-flight agent invocation — and only
it — is aborted (the loop and its bookkeeping survive long enough to record
the interruption). The loop reclassifies the aborted step as paused rather
than failed, then exits, leaving its run row and any open iteration row
open. The node's status is ``paused`` and its tmux session is gone — **no
session while parked is normal**, and crash reconciliation deliberately
never touches a paused node.

While parked, a node:

- accepts only ``resume``, ``kill``, and ``node chat`` — ``start``,
  ``merge``, ``delete``, ``retire``, and ``node init --reset`` all refuse
  over it;
- still holds its spawn slot against the width and descendant caps;
- blocks its ancestors' finish-drains (a parent cannot drain-complete over a
  parked child).

The pause latch
~~~~~~~~~~~~~~~

A paused subtree admits no new work. While any ancestor (or the node itself)
is ``paused``, or still ``active`` with a pending pause signal,
``node init`` (spawning), ``node start``, and a targeted ``resume`` under the
frozen ancestor all refuse — and a loop that was already booting parks
itself.

A **tree-wide pause** (``fractal pause``, i.e. a pause on the user node)
additionally writes a ``.paused`` latch file beside the central database
*before* fanning out, so even a depth-1 start racing the sweep refuses —
depth-1 nodes otherwise have no pausable ancestor. The latch lives in the
root node's data directory and rides a filesystem transplant with the rest
of the paused state; ``fractal resume`` removes it first, even when nothing
is left parked to relaunch, and ``fractal reset`` clears a stale one.

.. code-block:: console

   $ fractal pause --reason "<reason>"
   Pause signal sent to 5 nodes (in-flight agents aborted; loops park paused): <reason>
   $ fractal resume
   Resumed 5 nodes (parked loops relaunched leaf-first; live pauses withdrawn)

What resuming restores
~~~~~~~~~~~~~~~~~~~~~~

A resumed loop **adopts its open run**: the same budgets and iteration
count, with the interrupted step re-entered — resuming the recorded agent
session when one exists, re-orienting fresh otherwise (session continuity is
best-effort, not guaranteed). The pause and resume events are instants, and
the paused span between them is credited back to the run and
iteration deadlines, so parking a node does not burn its timeouts.

If the resume races a loop that is still parking, the relaunch refuses with
"the loop is still running or parking … Retry once it exits" — retry, never
kill.

Retire and unretire
-------------------

``fractal node retire [node] [--reason <text>]`` parks a node
administratively: it is hidden from ``node list`` by default (``--all``
includes it, ``--retired`` shows only retired nodes) and cannot be started,
while its worktree, branch, and history are all kept. Retire refuses an
``active`` or ``paused`` node and the user node. The pre-retire status rides
the retire event's metadata.

``fractal node unretire [node]`` restores the status the node held before
retirement, falling back to ``idle`` when none was recorded. A restore to
``idle`` re-enters the unsettled pool, so it re-checks the width and
descendant gates exactly like a spawn — over cap it refuses, with no
override flag. A settled restore holds no slot and passes ungated. Unretire
refuses a node that is not retired.

Retire is the non-destructive alternative ``node delete`` suggests: hide the
node, keep everything.

Crash reconciliation
--------------------

A loop that dies without settling — a hard kill, a direct
``tmux kill-session``, a host crash — leaves ``.status`` reading ``active``
with no
tmux session. Because ``start.sh`` enforces one loop per node, a *provably*
missing session is proof the loop is gone, and fractal heals the node on the
next read or verb: the status is stamped ``exited``, the still-open run,
iteration, and step rows are closed, any surviving process groups the loop
recorded are reaped with an ``orphan`` event (a headless agent would
otherwise keep spending unseen), and config/registry cap drift is healed.
This runs automatically before every signal verb, ``merge``, ``delete``,
``retire``, ``start --continue``, and on listings — there is no command to
run.

Two deliberate limits:

- **A ``paused`` node is never healed.** No session is its normal parked
  state, on this host or after a filesystem transplant.
- **An inconclusive tmux probe heals nothing.** The probe asks the server
  the loop recorded at boot; when tmux gives no answer (binary absent, no
  visibility from a cron/CI shell), the heal keys off proof, never
  ignorance, so a blind host cannot kill a healthy loop.

Separate from crash healing, ``fractal node reconcile [node]`` is the audit
step after out-of-band cleanup: for each registered descendant whose
worktree was removed with plain git instead of ``node delete``, it records
one ``orphan`` event (branches already recorded are skipped) and prints
``Recorded orphaned node <branch>.`` per orphan. Registry rows are kept —
``node delete <branch> --force`` (the orphan path below) remains the removal
path.

Teardown tiers
--------------

Three tiers of removal, from one subtree to the whole fractal:

.. list-table::
   :header-rows: 1
   :widths: 22 40 38

   * - Command
     - Removes
     - Survives
   * - ``fractal node delete``
     - One node and its whole subtree: worktrees, local and remote branches,
       registry rows, subscriptions.
     - Everything else — all other nodes, the user node's data, and every
       history row the subtree wrote (runs, steps, events, messages).
   * - ``fractal reset``
     - Every node worktree, local branch, and registration; a stale
       tree-wide pause latch.
     - The user node's data — config, memory, and the central database with
       all history — plus the wiki, baseline commits, remote branches, and
       ``.worktrees/`` itself.
   * - ``fractal destroy``
     - Everything ``reset`` removes, plus the user node's data directory
       (central database included), ``.worktrees/``, and fractal's
       ``info/exclude`` block.
     - Committed artifacts — the project wiki and baseline commits — and
       remote branches.

All three refuse over running nodes, and their ``--force`` flags only skip
the confirmation prompt (plus, for ``delete``, unlock the orphan path) —
none overrides the running-node refusals. They differ over *paused* nodes:
``delete``, which agents can reach, refuses over frozen work; ``reset`` and
``destroy`` kill paused nodes as part of the confirmed teardown — the
confirmation (or ``--force``) is the authorization to discard their frozen
mid-step work, and the prompt warns separately when paused nodes will be
killed.

node delete
~~~~~~~~~~~

``fractal node delete [node] [--force]`` removes the node and its whole
subtree, deepest first. Without ``--force`` an interactive confirmation
names the descendant count and suggests retiring instead. It refuses:

- the user node, and a node directory that was never properly initialized
  (that one "must be deleted manually");
- an ``active`` or ``paused`` node — or any such descendant ("Stop, resume,
  or kill the subtree first");
- running from inside any worktree of the subtree (git cannot remove the
  worktree the caller occupies);
- any locked worktree in the subtree, pre-flighted up front so a lock found
  mid-teardown never strands a half-deleted subtree.

Per node, the teardown deletes the remote branch first (skipped for
``local`` nodes), removes the worktree, force-deletes the local branch, and
drops the branch's project-cache entry. Commits not merged into the node's
merge target produce a warning on stderr — best-effort, never blocking.
Afterwards the whole subtree is deregistered from the central database: its
``nodes`` rows and its subscriptions in both directions are cleared, and a
``delete`` event is logged on the surviving parent. **All history rows
persist** — runs, iterations, steps, events, and messages outlive the node,
and ``node cost spent`` keeps answering for the deleted branch.

**The orphan path:** when the target's worktree is already gone (removed out
of band), plain ``delete`` cannot run. ``delete <branch> --force``
deregisters the orphan instead — matching the full branch or an unambiguous
bare name, pruning its git branch and cache entry along with any registered
descendants — and refuses if any live worktree remains in the subtree. It
hints at ``git worktree prune`` when stale worktree metadata lingers.

fractal reset
~~~~~~~~~~~~~

``fractal reset [path] [--force]`` is the middle rung: it removes **every**
node worktree and local branch and clears the node registry (rows and
subscriptions, grouped into maximal subtrees so a ``delete`` event lands per
subtree), while the user node's data — config, memory, and the central
database with every history row — plus the wiki and baseline commits
survive. Fresh nodes spawn immediately after. It also prunes phantom
branches left by out-of-band worktree removals and clears a stale tree-wide
pause latch.

Reset refuses when the caller stands inside a node worktree, while any
node's tmux session is alive (probed per node on its recorded socket), when
the tmux probe is **inconclusive** ("Restore tmux visibility and retry" —
an irreversible teardown never proceeds blind), and over any locked
worktree. Paused nodes are killed as part of the confirmed teardown.
``.worktrees/`` itself stays (it keeps the root's project-cache entry and
the lock), remote branches are left on origin and listed in the output, and
a repo with no worktrees at all is a clean no-op.

fractal destroy
~~~~~~~~~~~~~~~

``fractal destroy [path] [--force]`` is the full inverse of
``fractal init``, **database included**. On top of everything ``reset`` does,
it
removes the user node's data directory (config and the central database),
un-stages a tracked seed directory from git's index, removes ``.worktrees/``
entirely, strips fractal's block from the repo's ``info/exclude``, and
prunes every branch the registry listed. Left in place: committed artifacts
— the project wiki and baseline commits — and remote branches. The refusals
and the paused-node kill policy are the same as ``reset``, and a repo with
nothing fractal present is a clean no-op.

.. code-block:: console

   $ fractal destroy
   Destroy the fractal at /home/user/myproject (5 nodes)? [y/N]: y
   Destroyed fractal: /home/user/myproject
   Left in place: wiki/ (committed project memory)
