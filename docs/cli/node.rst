``fractal node``
================

The ``fractal node`` sub-app manages individual agent nodes: creating them,
launching and signaling their iteration loops, merging and tearing down their
branches, inspecting their state, and tracking their time and cost budgets.
Tree-wide operations (``fractal pause``, ``fractal resume``, ``fractal reset``,
``fractal destroy``) live on the top-level CLI — see :doc:`/cli/fractal`. The
lifecycle model behind the verbs is described in :doc:`/guide/lifecycle`, and
the iteration loop each node runs in :doc:`/guide/loop`.

Conventions
-----------

Every command below follows the same shape:

Target selection
   Most commands take an optional ``NODE`` argument naming the target node's
   branch. When omitted, the target is the node at ``--path`` (default ``.``),
   resolved to its git-worktree toplevel. From the repo root, the caller's
   own node is used when the ``_NODE`` environment variable is set (as it is
   inside a node's loop); otherwise a single worktree under ``.worktrees/``
   resolves implicitly (echoed on stderr), and multiple worktrees refuse with
   a request to name one.

Short names
   Wherever a branch is accepted, a unique trailing segment works: ``lexer``
   resolves to ``main.parser.lexer``. An ambiguous short name refuses and
   lists the candidates; an unknown one refuses with
   ``No node found for branch: '<name>'``.

``--path``
   Every command accepts ``--path <dir>`` (default ``.``). It identifies the
   *caller's* worktree — pick the target with the ``NODE`` argument, not with
   ``--path``. The one exception is ``node init``, where ``--path`` names the
   project root (repo root or monorepo sub-project).

Output
   Listing commands print a text table on a TTY and CSV automatically when
   piped; ``--csv`` forces CSV either way, and ``--json`` (where present)
   prints a JSON array instead (``[]`` when empty, mutually exclusive with
   ``--csv``). Empty listings still emit the header row. stdout carries the
   parseable result; notices, warnings, and confirmations ride stderr. Core
   refusals print ``Error: <message>`` on stderr and exit 1; argument errors
   raised at the CLI boundary (bad values, unknown or ambiguous node names,
   mutually exclusive flags) print a usage error
   (``Invalid value: <message>``) and exit 2.

Values
   Numeric caps must be non-negative; unlimited is expressed by omitting the
   flag, never by a sentinel value. Durations take a unit suffix —
   ``<number><s|m|h|d>``, e.g. ``30s``, ``10m``, ``1h`` — and must be at
   least one second. Costs are USD amounts printed as ``$X.XXXX``.

The underscore-prefixed commands are internal plumbing invoked by the node's
own scripts; they are not user surface and are hidden from ``--help``.

Creating nodes
--------------

``init``
~~~~~~~~

.. code-block:: console

   $ fractal node init NAME [options]

Create an agent node: a git worktree on branch ``<parent>.<name>`` under
``.worktrees/``, plus a node data directory ``.fractal/<branch>/`` seeded with
steps, scripts, skills, modes, and ``config.json``. The node's task contract
lives in ``<node_dir>/NODE.md`` — author its *Instructions* and *Completion
Requirements* sections, then launch with ``fractal node start``.

``NAME`` is one branch segment: letters, digits, and underscores only (no
dots, dashes, or slashes), at most 64 characters. A node spawned from inside
another node's worktree nests under that node; otherwise it nests under the
node at ``--path``.

.. list-table::
   :header-rows: 1
   :widths: 24 18 58

   * - Option
     - Default
     - Meaning
   * - ``--path <dir>``
     - ``.``
     - Project root: repo root or monorepo sub-project.
   * - ``--title <text>``
     - de-slugged name
     - Human-readable display name.
   * - ``--scope <dirs>``
     - unrestricted
     - Subdirectory scope(s) within the worktree that commits are restricted
       to (comma-separated; repeatable). Must be repo-relative — absolute or
       ``..`` paths refuse.
   * - ``--base <branch>``
     - parent branch
     - Branch to start from; when set it is **also the squash-merge target**
       and must have a checked-out worktree.
   * - ``--meta <branch>``
     - none
     - Target node branch for meta-configuration; expands to
       ``--base=<target> --scope=<target's seed dir>``. Mutually exclusive
       with ``--scope``/``--base``.
   * - ``--inherit <surfaces>``
     - package seed
     - Seed surfaces from the parent instead of the package seed
       (comma-separated; repeatable): ``steps``, ``scripts``, ``skills``,
       ``config``, or ``all``. ``config`` copies preference keys only —
       budget-class caps never inherit.
   * - ``--steps <dir>``
     - package seed
     - Directory of step files (``*.md``) seeding the node's ``steps/``
       instead of the package seed; must hold at least one, each carrying
       the loop's ``NN-`` digit prefix at one width.
       Mutually exclusive with ``--inherit=steps``.
   * - ``--agent <command>``
     - nearest ancestor's
     - Agent command, validated against the agent registry (a typo refuses).
       See :doc:`/guide/agents`.
   * - ``--provider <route>``
     - inherited, else vendor-native
     - Provider route (e.g. ``openrouter``). An unsupported explicit route
       refuses; an inherited unsupported one is silently dropped.
   * - ``--model <name>``
     - agent's default
     - Model override, passed to the agent CLI.
   * - ``--effort <level>``
     - none
     - Reasoning-effort override, passed to the agent CLI.
   * - ``--max-iters <n>``
     - unlimited
     - Per-run iteration cap; must be greater than 0.
   * - ``--max-depth <n>``
     - unlimited
     - Maximum child nesting depth below this node.
   * - ``--max-children <n>``
     - unlimited
     - Maximum unsettled direct children (concurrency, not lifetime count).
   * - ``--max-descendants <n>``
     - unlimited
     - Maximum unsettled descendants in the subtree.
   * - ``--timeout <dur>``
     - none
     - Whole-run time budget (e.g. ``30s``, ``10m``, ``1h``).
   * - ``--iter-timeout <dur>``
     - none
     - Per-iteration time budget; defaults to ``--interval`` when that is set,
       and may not exceed it.
   * - ``--step-timeout <dur>``
     - none
     - Per-step time budget.
   * - ``--step-retries <n>``
     - ``1``
     - Retries per failed step; ``0`` disables.
   * - ``--step-retry-backoff <dur>``
     - ``10s``
     - Delay before each step retry.
   * - ``--interval <dur>``
     - none
     - Fixed iteration schedule; mutually exclusive with ``--sleep``.
   * - ``--sleep <dur>``
     - none
     - Delay between iterations.
   * - ``--wait <dur>``
     - ``1m``
     - Sleep between sync passes while waiting — on a child drain or an
       approval gate.
   * - ``--max-cost <usd>``
     - uncapped
     - **Per-run** cost ceiling; each launch arms the cap anew.
   * - ``--max-iter-cost <usd>``
     - none
     - Per-iteration cost cap; requires ``--max-cost``.
   * - ``--max-step-cost <usd>``
     - none
     - Per-step cost cap; requires ``--max-cost``. Warn-only for agents whose
       CLI has no budget flag.
   * - ``--reserve-budget <usd|N%>``
     - ``10%`` of ``--max-cost``
     - Budget reserved for cleanup, as USD or a percentage of ``--max-cost``;
       only meaningful with ``--max-cost`` and must stay below 99% of it.
   * - ``--sync`` / ``--no-sync``
     - enabled
     - Run sync mode (the radio check-in) before each step.
   * - ``--detached`` / ``--no-detached``
     - continuous
     - Run each step as a separate agent invocation instead of one continuous
       session.
   * - ``--local`` / ``--no-local``
     - disabled
     - Skip pushing to remote after each iteration's commit. Inherited from a
       local parent and immutable once set.
   * - ``--blind``
     - disabled
     - Subscribe to no radio channels (the parent still reads this node). See
       :doc:`/guide/radio`.
   * - ``--reset``
     - —
     - Delete node files and reinitialize.

Cost caps must satisfy ``step <= iter <= run`` ordering. Spawn caps
(``max_children``, ``max_descendants``, ``max_depth``, and the parent's
remaining budget) are enforced at init, and spawning refuses under a paused
ancestor or a tree-wide pause latch. An existing node refuses without
``--reset``, and ``--reset`` itself refuses over an active or paused node.
One advisory rides stderr without blocking: a node with neither
``--max-cost`` nor ``--max-iters`` warns that it can run and spend without
bound.

.. code-block:: console

   $ fractal node init parser --title "Build the parser" --scope src,tests \
         --max-cost 10 --max-iters 10 --timeout 24h
   $ $EDITOR .worktrees/main.parser/.fractal/main.parser/NODE.md
   $ fractal node start parser

Running nodes
-------------

``start``
~~~~~~~~~

.. code-block:: console

   $ fractal node start [NODE] [--continue] [--clean] [--max-cost <usd>]

Launch the node's iteration loop in a tmux session (named
``<repo> (<branch>)`` with dots flattened to dashes). Run parameters
come from the node's ``config.json``; only the continue flags are set at
launch time.

``--continue``
   Re-arm a settled node (``completed``, ``stopped``, ``exited``, or
   ``killed``) for further iterations. The launch restores the worktree; a
   dirty worktree refuses and lists the uncommitted project files that would
   be discarded.

``--clean``
   With ``--continue``: acknowledge discarding uncommitted project files
   (node-directory files are exempt).

``--max-cost <usd>``
   With ``--continue``: retune the cost cap before relaunch, echoed
   ``max_cost: old -> new``. **Required** when the last run ended on its cost
   budget — runs are isolated and each launch arms the cap anew, so a
   budget-ended run refuses a bare ``--continue``.

A fresh start requires status exactly ``idle``. Starting refuses on: the user
node; a ``retired`` node (unretire first); a ``paused`` node (resume first); a
paused ancestor or tree-wide pause latch; a foreign tmux session already
holding the node's session name; and a stored config the launch re-validation
rejects. Continuing from ``killed`` surfaces the recorded kill attribution as
a notice. An uncapped start is allowed but warns loudly.

.. code-block:: console

   $ fractal node start parser
   $ fractal node start parser --continue --clean --max-cost 10

``attach``
~~~~~~~~~~

Attach the current terminal to the node's tmux session. Requires the node to
be ``active``.

.. code-block:: console

   $ fractal node attach parser

Signals
-------

Three graceful verbs and one immediate one end or suspend a running node. The
graceful verbs — ``finish``, ``stop``, ``pause`` — require the target to be
``active`` with a run; ``kill`` also accepts ``paused`` and a booting ``idle``
node with a live session. Each accepts ``--reason <text>``, recorded with the
signal. Refused signals are recorded as failed events in the activity log.

``finish`` and ``stop``
~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal node finish [NODE] [--reason <text>] [--cancel]
   $ fractal node stop [NODE] [--reason <text>]

``finish`` ends the node gracefully after the current **iteration**; ``stop``
ends it after the current **step**. Both are queued signals the loop polls at
its boundaries: a stop that lands mid-step **waits for the in-flight agent to
complete** -- however long that takes -- and never signals or tears it (the
immediate path is ``kill``).

.. warning::

   Both verbs cascade to the node's **entire subtree** of active descendants
   (children first, so the subtree drains before the target settles). There
   is no single-node form: stopping a manager stops every lane under it, each
   after its own current step. Invoked on the user node, ``finish``/``stop``
   broadcast tree-wide (the user node itself carries no signal).

``--cancel`` (``finish`` only) withdraws this node's pending finish signal
instead of sending one — it never fans out, and refuses when no finish signal
is set.

.. code-block:: console

   $ fractal node finish parser --reason "requirements met"
   $ fractal node finish parser --cancel

``pause`` and ``resume``
~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal node pause [NODE] [--reason <text>]
   $ fractal node resume [NODE]

``pause`` suspends the subtree: the pause signal lands parent-first, each
in-flight agent invocation is aborted, and the loops park with status
``paused``, leaving their run and iteration rows open. A parked node has no
tmux session — that is its normal state, not a crash. While a node (or the
whole tree, via ``fractal pause``) is paused, spawning and starting refuse
anywhere beneath it; the only legal verbs on a paused node are ``resume``,
``kill``, and ``chat``.

``resume`` relaunches the parked subtree leaf-first. Each loop adopts its open
run — same budgets and iteration count, the interrupted step re-entered
(resuming the recorded agent session when one exists) — and run and iteration
deadlines are credited for the paused span. A node still parking gets its
pending pause withdrawn instead. Resuming refuses when the node is not paused
or when an ancestor is still paused.

.. code-block:: console

   $ fractal node pause parser --reason "investigating a regression"
   $ fractal node resume parser

``kill``
~~~~~~~~

.. code-block:: console

   $ fractal node kill [NODE] [--reason <text>]

Kill the node immediately: descendant sessions and recorded process groups
are reaped first (re-enumerated to a fixpoint, so mid-sweep spawns are
caught), then the node's own, and open rows are marked ``killed``. Killable
states are ``active``, ``paused``, and ``idle`` -- a booting node is reaped
and a never-started spawn is stamped ``killed`` so it can never activate.
The attribution ``killed by <actor>[: reason]`` lands on the event,
the signal, and the run row, and is surfaced as a notice on a later
``start --continue``.

.. code-block:: console

   $ fractal node kill parser --reason "runaway loop"

Integration and teardown
------------------------

``merge``
~~~~~~~~~

.. code-block:: console

   $ fractal node merge [NODE]

Squash-merge the node's branch into its merge target — the configured
``base`` when set, else the dotted parent. One squash commit lands on the
target; the node's full history stays on its own branch. Merging refuses on
the user node, when the node itself is ``active`` or ``paused``, and when the
*target* is ``active`` or ``paused`` (except from inside the target's own
loop, its normal child-merge path). Non-fatal warnings ride stderr.

.. code-block:: console

   $ fractal node merge parser

``delete``
~~~~~~~~~~

.. code-block:: console

   $ fractal node delete [NODE] [--force|-f]

Recursively remove the node's subtree, deepest first: worktrees, local and
remote branches, registry rows, and radio subscriptions. **History rows
persist** — runs, iterations, steps, events, and messages all survive in the
central database. Without ``--force`` an interactive confirmation names the
descendant count and suggests ``retire`` as the non-destructive alternative.

Deletion refuses on: the user node; a subtree containing any ``active`` or
``paused`` node (stop, resume, or kill it first); a caller standing inside a
subtree worktree; and any locked worktree in the subtree. Warnings about
unmerged work ride stderr without blocking.

When the target's worktree is already gone (removed out of band), ``delete
<branch> --force`` takes the orphan path instead: it deregisters the branch
(and any registered descendants) from the registry, refusing if any live
worktree remains in the subtree.

.. code-block:: console

   $ fractal node delete parser --force

``reconcile``
~~~~~~~~~~~~~

.. code-block:: console

   $ fractal node reconcile [NODE]

The audit step after cleaning up a node's worktree or branch with plain git
instead of ``delete``: records one ``orphan`` event per registry row whose
worktree is gone (already-recorded branches are skipped). Registry rows are
kept. Prints ``Recorded orphaned node <branch>.`` per orphan, or ``No
orphaned nodes to record.``

``retire`` and ``unretire``
~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal node retire [NODE] [--reason <text>]
   $ fractal node unretire [NODE]

``retire`` parks a node: hidden from ``list`` by default and unstartable,
with its branch and history kept. It refuses ``active``/``paused`` nodes and
the user node. The pre-retire status is recorded on the retire event, and
``unretire`` restores it (falling back to ``idle``). An ``idle`` restore
returns the node to the unsettled pool, so the width and descendant caps are
re-checked as at spawn — refused over cap, with no override.

.. code-block:: console

   $ fractal node retire parser --reason "superseded"
   $ fractal node unretire parser

Inspection
----------

``status``
~~~~~~~~~~

.. code-block:: console

   $ fractal node status [NODE]

Print the node's current status, possibly decorated: ``active (pausing)``,
``active (stopping)``, or ``active (finishing)`` when a signal is pending,
and ``exited (<reason>)`` when the latest run recorded why it ended. The read
is self-reconciling — a crashed node (``active`` on record with provably no
tmux session) is healed to ``exited`` first.

.. code-block:: console

   $ fractal node status parser
   active (finishing)

``list``
~~~~~~~~

.. code-block:: console

   $ fractal node list [NODE] [--all] [--retired] [--max-depth <n>]
         [--status <s[,s...]>] [--live] [--count] [--csv]

List a node's **descendants** — the listing never includes the target's own
row (use ``status`` for that). Columns: ``status``, ``node``, ``title``,
``max_cost``, ``max_depth``, ``max_children``, ``max_descendants``, ``last``.
Blank limit columns mean unlimited. ``last`` is the age of each node's newest
activity, with a ``!`` suffix flagging an active node quiet past
``max(step_timeout, 5m)``. On a TTY, statuses render bracketed
(``[active]``); machine output stays unbracketed.

``--all``
   Include retired nodes.

``--retired``
   Show only retired nodes.

``--max-depth <n>``
   Maximum child depth to include (``1`` = direct children only).

``--status <s[,s...]>``
   Filter to one or more comma-separated statuses. Valid values: ``active``,
   ``paused``, ``idle``, ``completed``, ``stopped``, ``exited``, ``killed``,
   ``failed``, ``retired``, plus the listing-only pseudo-status ``orphan``
   (a registered node whose worktree is gone). Unknown values refuse with the
   valid list.

``--live``
   Trust each child's real state: relabel a crashed active node (no tmux
   session) as ``exited``, a booting idle node as ``active``, and drop nodes
   whose worktree is gone. This view is read-only; the plain listing instead
   persists the crash heal and flags worktree-less rows as ``orphan`` (or
   ``<status> (orphaned)`` when settled).

``--count``
   Print only the number of matching nodes.

``--csv``
   Force CSV output (already the default when piped).

.. code-block:: console

   $ fractal node list --status active,paused
   $ fractal node list parser --max-depth 1 --count

``activity``
~~~~~~~~~~~~

.. code-block:: console

   $ fractal node activity [NODE] [--limit <n>] [--csv] [--json]

Show the node's lifecycle activity, most recent first. Columns (bind by
header name, not position): ``timestamp``, ``node``, ``event_id``,
``step_id``, ``iter_id``, ``run_id``, ``event``, ``actor``, ``step_name``,
``step``, ``iter``, ``status``, ``exit_code``, ``metadata``, ``duration``,
``cost``. The ``cost`` column is the row's own-node step cost only — subtree
spend, children included, is ``fractal node cost spent``. ``--limit <n>``
bounds the rows returned; ``--json`` emits a JSON array of row objects
(mutually exclusive with ``--csv``).

.. code-block:: console

   $ fractal node activity parser --limit 10 --json

Approvals
---------

Steps whose frontmatter sets ``requires_approval: true`` gate on the direct
parent before running. The waiting child polls in sync mode (paced by its
``wait`` interval) until the approval lands.

``approve``
~~~~~~~~~~~

.. code-block:: console

   $ fractal node approve NODE [STEP_ID]

Approve a child's gated step — must be run from the **direct parent**.
``NODE`` (required) is the child's branch; ``STEP_ID`` defaults to the
child's active gated step. Approval refuses when the caller is not the direct
parent, when the child has no active step, or when the step does not require
approval.

``pending``
~~~~~~~~~~~

.. code-block:: console

   $ fractal node pending [NODE] [--csv]

List direct children's steps awaiting this node's approval. Columns:
``branch``, ``step_id``, ``step``, ``step_name``. Only gated steps on
``active`` or ``paused`` children appear.

.. code-block:: console

   $ fractal node pending
   $ fractal node approve parser 1
   Step 1 on main.parser approved.

Chat
----

``chat``
~~~~~~~~

.. code-block:: console

   $ fractal node chat [NODE] PROMPT [--session <id>] [--current] [--resume]
         [--model <name>]

Send one prompt to a node's agent and stream the reply. The prompt is
required as the second argument. A bare chat opens a **fresh session** seeded
with the node's ``NODE.md`` and chat mode, so the agent answers in the node's
context. Chatting has no side effects on the node's cost or session records,
and it is one of the few verbs legal on a paused node. The resulting session
id prints on stderr as ``session: <id>`` so the thread can be continued.

``--session <id>``
   Fork the named session (default: a fresh session).

``--current``
   Fork the node's live loop session — ask about what the loop is doing right
   now. Mutually exclusive with ``--session``/``--resume``; refuses when no
   live session exists.

``--resume``
   Continue ``--session`` in place instead of forking it. Resuming the live
   loop session refuses (it would perturb the loop). Codex agents can resume
   sessions but cannot fork them.

``--model <name>``
   Model override (default: the node's configured model).

.. code-block:: console

   $ fractal node chat parser "Summarize your progress so far."
   $ fractal node chat parser "Why did the last test fail?" --current

Retuning
--------

``update``
~~~~~~~~~~

.. code-block:: console

   $ fractal node update NODE [--title <text>] [--max-cost <usd>]
         [--max-iter-cost <usd>] [--max-step-cost <usd>]
         [--reserve-budget <usd|N%>] [--step-timeout <dur>] [--max-depth <n>]
         [--max-children <n>] [--max-descendants <n>]

Update a **child** node's configuration mid-flight — the supported retune
path. It writes the registry row and the child's ``config.json`` together,
and a running loop picks the new caps up at its next iteration boundary. (A
direct edit of the child's ``config.json`` is honored at the same boundary
and back-filled to the registry with a warning.) At least one option is
required; each change is echoed ``key: old -> new`` (``unset`` for a prior
null). The merged result is validated exactly as at init.

The options mirror their ``node init`` counterparts: ``--title``,
``--max-cost``, ``--max-iter-cost``, ``--max-step-cost``,
``--reserve-budget`` (USD or ``N%`` of the effective max-cost),
``--step-timeout``, ``--max-depth``, ``--max-children``, and
``--max-descendants``. When ``--max-cost`` changes and the reserve was left
at its default, the 10% reserve is re-derived against the new cap. The user
node cannot be updated (set its config with ``node config set`` from the root
worktree instead), and the target's parent worktree must exist.

.. code-block:: console

   $ fractal node update parser --max-cost 100 --max-children 2
   max_cost: 10.0 -> 100.0
   max_children: unset -> 2
   reserve_budget: 1.0 -> 10.0

Time budgets: ``fractal node time``
-----------------------------------

``time remaining``
~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal node time remaining [NODE] [--scope run|iter|step]

Print the seconds left before the next timeout fires, e.g. ``600s``. By
default the soonest of the configured run, iteration, and step deadlines
answers; ``--scope`` narrows to one: ``run`` reads ``timeout``, ``iter``
reads ``iter_timeout`` (falling back to ``interval``), ``step`` reads
``step_timeout``. Paused spans are credited back to run and iteration
deadlines. Two non-numeric answers are distinct: ``no limit`` means no
timeout is configured for the queried scope; ``not running`` means a limit
exists but no run, iteration, or step is currently active.

.. code-block:: console

   $ fractal node time remaining parser --scope iter
   600s

Cost budgets: ``fractal node cost``
-----------------------------------

Budgets and spend are **per-run**: each launch arms ``max_cost`` anew, and all
three commands default to the current run (scope to a prior run with
``--run <id>``). Amounts print as ``$X.XXXX``. A deleted node still resolves
through the tree's shared history: ``spent`` and ``breakdown`` answer from its
latest recorded run, while ``remaining`` always prints ``no budget`` (caps die
with the node).

``cost remaining``
~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal node cost remaining [NODE] [--run <id> | --iter <id> | --step <id>]

Print the remaining budget: ``max_cost`` minus the run's **subtree** spend —
the node's own steps plus every descendant run chained beneath it. ``--iter``
and ``--step`` scope to a specific iteration's ``max_iter_cost`` or step's
``max_step_cost`` headroom instead; at most one of ``--run``/``--iter``/
``--step`` may be given. Prints ``no budget`` when the relevant cap is unset,
and never goes negative (clamped at ``$0.0000``).

``cost spent``
~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal node cost spent [NODE] [--run <id> | --iter <id> | --step <id>]
         [--max-depth <n>]

Print the total recorded cost, children included; ``--max-depth 0`` restricts
to this node only. ``--run``, ``--iter``, and ``--step`` scope the reading to
one run, iteration, or step. Prints ``untracked`` when the scope ran only
cost-untracked steps; when ended steps carry no recorded cost, a stderr
disclosure notes how many unpriced steps were excluded.

``cost breakdown``
~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal node cost breakdown [NODE] [--run <id>] [--max-depth <n>] [--csv]

Print a per-node table — columns ``node``, ``max_cost``, ``spent`` — for the
current run (or ``--run <id>``). The target's own row leads, followed by each
in-subtree descendant; descendants whose registry rows are gone but whose
spend is still recorded append as ``<branch> (deleted)`` rows, so the rows
always sum to ``cost spent``.

.. code-block:: console

   $ fractal node cost remaining parser
   $6.7890
   $ fractal node cost spent parser --max-depth 0
   $3.2100
   $ fractal node cost breakdown parser

Configuration: ``fractal node config``
--------------------------------------

A node's run parameters live in its ``config.json``; the full key reference is
in :doc:`/configuration`.

``config get``
~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal node config get KEY

Print one config value. Unknown keys refuse with the valid-keys list.
Booleans render as ``true``/``false``, list values (``scope``) one item per
line, and an unset key prints nothing (exit 0).

``config set``
~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal node config set KEY=VALUE [KEY=VALUE ...]

Write config values, echoing ``key: old -> new`` per key. The ``key=value``
form is required; an empty value refuses (use ``key=null`` to clear a key).
Types are enforced at the boundary: boolean keys accept ``true``/``false``/
``null``; integer keys a non-negative integer (``max_iters`` must be greater
than 0); cost keys a non-negative number — the ceilings (``max_cost``,
``max_iter_cost``, ``max_step_cost``) must additionally be positive, with
``0`` refused by the merged validation, while ``reserve_budget`` may be
``0``; ``scope`` a comma- or space-joined list of roots; every other key
stores a literal string. The merged result is validated — cost positivity
and ordering, reserve range, duration suffixes, pacing exclusivity — before
anything is written; ``init``'s additional flag
checks (the agent registry, provider support, the base worktree) do not run
here. Multi-key sets are checked upfront, so no earlier key lands when a
later one is rejected.

A *change* to the init-fixed keys ``root``, ``user``, and ``project`` always
refuses: ``<key> is fixed at init and cannot be set.`` (Re-setting the
currently stored value is accepted as a no-op.)

.. code-block:: console

   $ fractal node config get max_cost
   10.0
   $ fractal node config set max_iter_cost=1 sleep=30s
   max_iter_cost: unset -> 1
   sleep: unset -> 30s
