Recipes
=======

Task-oriented how-tos for operating a fractal tree. Each recipe is
self-contained and assumes an initialized tree with a committed baseline (see
:doc:`/guide/getting-started`); commands run from the repository root unless
noted. Wherever a command takes a node branch, a unique trailing segment of
the dotted branch works as a short name (``parser`` resolves to
``main.parser``). Full option surfaces live in the :doc:`/cli/index`
reference.

Run a budgeted tree
-------------------

Cap a node's spend at creation with ``--max-cost`` and set aside a wind-down
slice with ``--reserve-budget``:

.. code-block:: console

   $ fractal node init parser --title "Build the parser" \
         --max-cost 10 --reserve-budget 10% --max-iters 10
   $ fractal node start parser

``--max-cost`` is a **per-run** ceiling in USD: every ``fractal node start``
arms it anew, and it covers the run's whole subtree — the node's own steps
plus everything its descendants spend, with no slice reserved for the node
itself. Because the budget is subtree-shared, every child spawned under a
capped node must set its own ``--max-cost``, which may not exceed the
parent's remaining run budget.

``--reserve-budget`` takes a USD amount or ``N%`` of ``--max-cost`` (default
``10%``; it must stay below 99% of the cap, and is only meaningful when
``--max-cost`` is set). The reserve is not a hard floor: when the run's
remaining budget falls to the reserve, the node enters reserve mode —
wind-down instructions join each prompt, telling the agent to commit open
work, settle its children, and report out — and the run ends at the next
iteration boundary.

Two finer caps exist, both requiring ``--max-cost`` and ordered
``step <= iter <= run``: ``--max-iter-cost`` caps each iteration, and
``--max-step-cost`` caps each step (a hard cap only for agents that enforce
a budget flag; warn-only otherwise). Starting a node with neither
``--max-cost`` nor ``--max-iters`` is allowed but warns loudly — such
a node can run and spend without bound.

While the node runs, watch the headroom and retune mid-flight from the
parent:

.. code-block:: console

   $ fractal node cost remaining parser
   $3.4167
   $ fractal node update parser --max-cost 100
   max_cost: 10.0 -> 100.0
   reserve_budget: 1.0 -> 10.0

A running loop picks up a retune at its next iteration boundary. A run that
ends on its budget lands as ``exited`` with exit code ``0`` — an honest
budget landing, neither a failure nor a completion — and refuses a bare
continue; arm the next run explicitly:

.. code-block:: console

   $ fractal node start parser --continue --max-cost 10

See :doc:`/guide/loop` for enforcement details and :doc:`/configuration` for
the budget keys.

Pause and resume a whole tree
-----------------------------

``fractal pause`` is the tree-wide brake. It latches the tree (a ``.paused``
marker beside the central database), then fans a pause out over every active
node parent-first, aborting in-flight agent invocations; each loop parks with
status ``paused``, its run and iteration rows left open:

.. code-block:: console

   $ fractal pause --reason "budget review"
   Pause signal sent to 2 nodes (in-flight agents aborted; loops park paused): budget review

While the tree is latched, spawning (``fractal node init``) and
``fractal node start`` refuse everywhere. Nothing is committed on pause: each
worktree keeps its uncommitted files as the frozen mid-step state that resume
continues from. A paused node has no tmux session — that is its normal
parked state, not a crash — and the only verbs it accepts are ``resume``,
``kill``, and ``chat``.

Check what is parked, then release the tree:

.. code-block:: console

   $ fractal node list --status paused
   $ fractal resume
   Resumed 2 nodes (parked loops relaunched leaf-first; live pauses withdrawn)

``fractal resume`` lifts the latch, withdraws pending pauses on nodes still
parking, and relaunches parked loops leaf-first. Each loop adopts its open
run: same budgets, same iteration count, the interrupted step re-entered,
with run and iteration deadlines credited for the paused span.

To pause a single subtree instead, use the node-scoped verbs:
``fractal node pause <node> [--reason <text>]`` and
``fractal node resume <node>``. A subtree resume refuses while an ancestor
(or the tree-wide latch) is still paused. See :doc:`/guide/lifecycle` for the
full status model.

See where the money went
------------------------

Spend readings are per-run and default to the current run; scope a prior run
with ``--run <id>``. ``cost spent`` prints the subtree total (the node's own
steps plus all descendants); ``cost breakdown`` attributes it per node:

.. code-block:: console

   $ fractal node cost spent parser
   $6.5833
   $ fractal node cost breakdown parser
   node               max_cost  spent
   -----------------  --------  ------
   main.parser        10.0      4.1032
   main.parser.lexer  5.0       2.4801
   main.parser.tests  2.0       0.0

The target's own row leads, followed by each descendant in the subtree. A
descendant that was deleted but whose spend is still recorded appends as a
``<branch> (deleted)`` row, so the rows always sum to ``cost spent``. Limit
depth with ``--max-depth`` (``0`` = the target's own spend only); force CSV
with ``--csv`` (piped output is CSV automatically).

Cost is recorded, never estimated. When ended steps carry no cost figure, the
sums omit them and a disclosure rides stderr (``N unpriced step(s)
(NULL cost) excluded``); a scope whose steps recorded no cost at all prints
``untracked``. A deleted node still answers ``cost spent`` and
``cost breakdown`` from its persisted history (its latest recorded run by
default), while ``cost remaining`` for it always prints ``no budget`` — caps
die with the node, history does not.

Chat with a running node
------------------------

``fractal node chat`` sends one prompt to a node's agent and streams the
reply, without touching the node's records: no cost lands on its ledger and
no session side effects reach the loop.

.. code-block:: console

   $ fractal node chat parser "summarize your progress and current blockers"

A bare chat opens a fresh session seeded with the node's ``NODE.md`` and the
chat-mode instructions — the agent knows its task contract but not the
loop's working context. To talk to the loop's own state, fork its live session:

.. code-block:: console

   $ fractal node chat parser "which step are you on, and what is left?" --current

``--current`` forks the node's live loop session: the reply sees everything
the loop's agent has seen, and the fork leaves the loop itself unperturbed.
It refuses when no live session exists, and codex cannot fork sessions at
all.

Every chat echoes its session id on stderr as ``session: <id>``. Continue a
thread by naming it:

.. code-block:: console

   $ fractal node chat parser "and what would unblock you?" \
         --session <id> --resume

``--session <id>`` forks that recorded session; adding ``--resume`` continues
it in place instead of forking. Resuming the live loop session is refused (it
would perturb the loop). ``--model`` overrides the model for this chat only.
Chat also works on a ``paused`` node — it is one of the few verbs a parked
node accepts.

Approve a child's gated steps
-----------------------------

A step file whose frontmatter sets ``requires_approval: true`` gates the
loop: after the step completes, the child parks awaiting its direct parent's
approval before the iteration proceeds. Gate the plan step, for example, so
each iteration's plan — including any child spawns it proposes — needs
sign-off before execution begins:

.. code-block:: text

   ---
   requires_approval: true
   ---

   ## Plan
   ...

From the parent's worktree (the repository root for depth-1 children), list
what is waiting and approve it:

.. code-block:: console

   $ fractal node pending
   branch       step_id  step  step_name
   -----------  -------  ----  ---------
   main.parser  10       1     PLAN
   $ fractal node approve main.parser 10
   Step 10 on main.parser approved.

``step_id`` is optional and defaults to the child's active gated step.
Approval must come from the direct parent; anyone else is refused. The wait
counts against the child's run and iteration deadlines (a gated step can time
out waiting), each retry attempt re-arms its own gate, and reserve mode
(budget wind-down) skips gates entirely. See :doc:`/guide/loop` for the step
frontmatter contract.

Recover after a crash
---------------------

Crash healing is read-driven — there is no separate repair command to run
first. A loop that dies without settling leaves its node ``active`` with no
live tmux session; the next read or lifecycle verb that touches it
(``fractal node status``, ``fractal node list``, ``fractal node start``, and
the teardown verbs) detects that state, stamps the node ``exited``, closes
its open run, iteration, and step rows, and reaps any recorded leftover
process groups:

.. code-block:: console

   $ fractal node status parser
   exited
   $ fractal node start parser --continue

``fractal node list --live`` gives the same authoritative view read-only,
relabeling without persisting. A ``paused`` node is never healed: no tmux
session is its normal parked state. Neither is a loop launched bare
(``fractal node _loop``, what ``start.sh`` execs — the harness and a
tmux-less host launch it directly): it never joined a tmux server, so the
probe's "no such session" says nothing about it, and its own recorded process
group keeps the heal — and the reap that comes with it — off a running loop.

``--continue`` restores the worktree before relaunching: uncommitted project
files refuse without ``--clean`` (which acknowledges discarding them), and a
run that ended on its budget refuses without an explicit ``--max-cost``.

If node worktrees or branches were removed out of band — plain ``git``
instead of ``fractal node delete`` — reconcile the registry afterwards:

.. code-block:: console

   $ fractal node reconcile
   Recorded orphaned node main.parser.lexer.

``reconcile`` records one ``orphan`` event per registry row whose worktree is
gone (already-recorded branches are skipped) and keeps the rows. Orphaned
rows show in listings as ``orphan`` (or ``<status> (orphaned)`` once settled)
and can be filtered with ``fractal node list --status orphan``. To drop an
orphaned registration entirely, ``fractal node delete <branch> --force``
deregisters it, refusing while any live worktree remains in its subtree.

Clean up: delete, reset, or destroy
-----------------------------------

Teardown comes in three tiers. All three refuse while nodes are running, and
``--force`` only skips the confirmation prompt — it never overrides those
refusals.

``fractal node delete <node>``
   Removes one subtree: worktrees, local and remote branches, registry rows,
   and radio subscriptions. All history rows persist in the central database.
   Refuses while the node or any descendant is ``active`` or ``paused``, and
   when run from inside a subtree worktree; unmerged-work warnings ride
   stderr. The confirmation names the descendant count; ``--force`` skips it
   (and enables deregistering an orphaned, worktree-less node).

``fractal reset``
   Removes every node worktree, local branch, and registration in the tree.
   The user node's data — config and the central database with all history
   — plus the wiki and baseline commits survive, and fresh nodes can spawn
   immediately afterwards. Refuses while any node's tmux session is alive;
   paused nodes are killed as part of the confirmed teardown. Also clears a
   stale tree-wide pause latch. Remote branches are left in place and
   listed.

``fractal destroy``
   The full inverse of ``fractal init``: everything ``reset`` removes plus
   the user node's data directory (central database included) and fractal's
   ``info/exclude`` block. Committed artifacts — the project wiki and
   baseline commits — and remote branches remain.

For a non-destructive alternative to ``delete``, retire the node instead:
``fractal node retire <node>`` hides it from listings and makes it
unstartable while keeping its branch and history, and
``fractal node unretire <node>`` restores it.
