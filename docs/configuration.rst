Configuration
=============

Every node in a fractal tree carries a ``config.json`` — a flat JSON file in
the node's data directory that holds the node's entire run configuration:
agent selection, budgets, timeouts, pacing, and mode flags. The file is the
live source of truth: the loop reads it from disk at every access, so the
re-read keys — the cost caps, ``max_iters``, ``step_timeout``, and ``wait`` —
can be retuned while a node runs. This page is the complete key
reference; :doc:`/guide/loop` describes how the loop enforces the budget and
timeout keys, and :doc:`/guide/plans` covers the per-step override surface.

Where ``config.json`` lives
---------------------------

Each node's config sits inside its data directory:

- **Child nodes**: ``<repo>/.worktrees/<branch>/.fractal/<branch>/config.json``
  (with a project prefix before ``.fractal/`` in a monorepo sub-project).
- **The user (root) node**: ``<repo>/.fractal/<root-branch>/config.json``
  (with the same monorepo project prefix), beside the tree's central
  database.

``fractal node init`` writes every key in the schema, storing unset keys as
JSON ``null``. A freshly created node titled "Build the parser" looks like:

.. code-block:: json

   {
     "title": "Build the parser",
     "project": ".",
     "root": "main",
     "scope": ["src", "tests"],
     "base": null,
     "meta": null,
     "agent": "claude",
     "provider": null,
     "model": null,
     "effort": null,
     "max_iters": 10,
     "max_depth": 2,
     "max_children": 5,
     "max_descendants": 10,
     "timeout": "24h",
     "iter_timeout": "1h",
     "step_timeout": "10m",
     "step_retries": null,
     "step_retry_backoff": null,
     "interval": null,
     "sleep": null,
     "wait": null,
     "max_cost": 10.0,
     "max_iter_cost": 2.0,
     "max_step_cost": null,
     "reserve_budget": 1.0,
     "sync": null,
     "detached": null,
     "local": false,
     "blind": false
   }

The user node's config is minimal — ``{"user": true, "project": ".",
"root": "<branch>"}`` plus optional ``agent``/``provider`` defaults that
children inherit. The user node has no loop, so the run-parameter keys never
apply to it.

Reads hit disk on every access (nothing is cached), so edits from other
processes are immediately visible. Writes are read-merge-write under a kernel
lock on a ``config.json.lock`` sidecar and land atomically, so concurrent
setters never revert each other's keys and a kill mid-write cannot tear the
file.

Reading and changing values
---------------------------

``fractal node config get`` / ``set``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Both commands operate on the node whose worktree contains the current
directory; pass ``--path <dir>`` to target another worktree.

.. code-block:: console

   $ fractal node config get max_cost
   10.0
   $ fractal node config set max_iter_cost=1 step_timeout=1h
   max_iter_cost: 2.0 -> 1
   step_timeout: 10m -> 1h

- ``get`` prints one value: booleans as ``true``/``false``, list values (like
  ``scope``) one item per line, and nothing for an unset key. Unknown keys
  are rejected rather than silently reading as unset.
- ``set`` takes one or more ``key=value`` pairs and confirms every write as
  ``old -> new``. Unknown keys are rejected, and an empty value is an error —
  ``key=null`` is the explicit way to clear a key.
- Number and boolean keys are parsed as JSON and type-checked (``sync=maybe``
  and ``max_cost=abc`` are refused with the expected type named); every other
  key is stored as a literal string, so a numeric-looking branch name is never
  silently turned into a number. ``scope`` accepts its directories comma- or
  space-joined and stores a JSON list.
- The merged result is checked by the validator (see `Validation`_) before
  anything is written, so ``set`` cannot store a value the validator rejects.
  ``node init``'s additional flag checks — the agent registry,
  provider support, the base worktree, a reserve requiring ``max_cost`` — do
  not run here; ``node start`` re-validates the stored file at launch.

``fractal node update``
~~~~~~~~~~~~~~~~~~~~~~~

The registry rows created at spawn keep a snapshot of each child's caps
(``max_cost``, ``max_depth``, ``max_children``, ``max_descendants``).
``fractal node update <child>`` is the supported retune path: runnable from
anywhere in the tree, it rewrites the child's ``config.json`` and its
registry row together and echoes every change ``old -> new``:

.. code-block:: console

   $ fractal node update main.parser --max-cost 100
   max_cost: 10.0 -> 100.0
   reserve_budget: 1.0 -> 10.0

It covers ``--title``, ``--max-cost``, ``--max-iter-cost``, ``--max-step-cost``,
``--reserve-budget``, ``--step-timeout``, ``--max-depth``, ``--max-children``,
and ``--max-descendants`` (``--max-iter-cost``, ``--max-step-cost``,
``--reserve-budget``, and ``--step-timeout`` live only in config — they have
no registry column). A running loop picks new caps up at its next
iteration boundary. See :doc:`/cli/node` for the full option surface.

Direct file edits
~~~~~~~~~~~~~~~~~

Editing ``config.json`` by hand (or having an agent edit it) is a supported
steering path — the loop re-reads the cost caps, ``max_iters``,
``step_timeout``, and ``wait`` at the top of each iteration. Two consequences:

- Config is enforcement truth. When an edited cap drifts from the registry
  snapshot, the drift is healed *from config to registry* with a per-key
  warning at the next boundary — config always wins.
- A *bad* hand edit (a bare-number duration, a broken cost ordering) is not
  caught at edit time: ``node start`` re-validates the stored file before
  launching, and the loop's mid-run re-readers warn and keep the previous
  values rather than crash.

The duration format
-------------------

Every duration key takes ``<number><unit>``, where the number is an integer or
decimal and the unit is one of ``s`` (seconds), ``m`` (minutes), ``h``
(hours), or ``d`` (days): ``30s``, ``10m``, ``1.5h``, ``2d``. Bare numbers are
rejected ("must be a duration with a unit suffix"), and the value must come to
at least one whole second after truncation to integral seconds — ``0s`` and
``0.5s`` are refused at init, ``config set``, and ``start``.

Key reference
-------------

Keys are grouped by type class. For most keys ``null`` means "no limit" or
"agent default"; a handful have loop-side defaults that apply at read time and
are never written back to the file — those are called out in their entries.

Plain values
~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 16 22 62

   * - Key
     - Default
     - Meaning
   * - ``title``
     - the de-slugged node name
     - Human display name shown in listings. Stored in both the config and
       the node's registry row; ``fractal node update --title`` writes both.
   * - ``project``
     - inherited (``.`` at the repo root)
     - Project sub-path within the worktree, for monorepos. **Immutable**
       (see `Immutable keys`_).
   * - ``root``
     - inherited from the parent
     - The tree's root branch. Every node carries it to resolve the central
       database. **Immutable**.
   * - ``scope``
     - ``null`` (unrestricted)
     - JSON list of repo-relative directories the node's commits are
       restricted to. Absolute paths and ``..`` components are rejected
       everywhere — they would break every scoped commit.
   * - ``base``
     - ``null`` (the parent's tip)
     - Branch the node forks from at init. When set, it is also the node's
       squash-merge target, and it must have a checked-out worktree.
   * - ``meta``
     - ``null``
     - Target node branch for meta-configuration. Set via
       ``fractal node init --meta``, which expands to a ``base``/``scope``
       pair pointing at the target's seed directory.
   * - ``agent``
     - inherited from the nearest ancestor
     - The agent command driving the node (the base word names a registered
       agent backend; extra words are spliced into every invocation). Some
       node in the ancestry chain must set it. See :doc:`/guide/agents` for
       the supported backends.
   * - ``provider``
     - ``null`` (vendor-native); inherited
     - Provider route, e.g. ``openrouter``. An inherited route the child's
       backend does not support is silently dropped; an explicit unsupported
       one is refused, naming the supported set.
   * - ``model``
     - ``null`` (the agent's own default)
     - Model override, passed through the agent CLI's model flag.
   * - ``effort``
     - ``null``
     - Reasoning-effort override, passed through the agent CLI's effort flag.
       Not validated by fractal — unknown levels surface as the agent's own
       error.

Booleans
~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 16 22 62

   * - Key
     - Default
     - Meaning
   * - ``user``
     - absent (``true`` only on the user node)
     - Marks user/root-node identity. **Immutable**; cannot be written
       through ``config set`` at all.
   * - ``sync``
     - ``null`` → ``true``
     - Run the SYNC (radio) step before each step. See :doc:`/guide/radio`.
   * - ``detached``
     - ``null`` → ``false``
     - Run every step as a fresh agent invocation instead of one continuous
       session per iteration. Pinned at loop boot — a mid-run edit cannot
       flip a running loop's mode.
   * - ``local``
     - ``false``
     - Skip pushing to the remote after each commit. Inherited one-way: a
       local parent forces local children — a child cannot set
       ``local=false`` at init.
   * - ``blind``
     - ``false``
     - The node subscribes to no radio channels (its parent still reads its
       outbox). See :doc:`/guide/radio`.
   * - ``sealed``
     - ``false``
     - Seal the node's mailbox: its own seat (identified by the
       loop-exported ``_NODE``) sees empty hosted listings and a refused
       ``radio read`` until unsealed (``config set sealed=false``) — the
       hold mechanism for verifier isolation. Takes effect at the next
       radio read; an operator shell is never held.

Integer caps
~~~~~~~~~~~~

``null`` means unlimited. All caps must be non-negative integers;
``max_iters`` must be strictly positive.

.. list-table::
   :header-rows: 1
   :widths: 16 22 62

   * - Key
     - Default
     - Meaning
   * - ``max_iters``
     - ``null`` (unlimited)
     - Iterations per **run**. Every ``node start`` restarts the count at 1;
       it is not a lifetime cap. Re-read each iteration, so it can be retuned
       mid-run.
   * - ``max_depth``
     - ``null`` (unlimited)
     - Maximum child nesting depth below this node, enforced on every
       ancestor at spawn time.
   * - ``max_children``
     - ``null`` (unlimited)
     - Maximum *unsettled* direct children. This bounds concurrency, not the
       lifetime spawn count: completed, stopped, exited, killed, and retired
       children free their slots; paused children still hold theirs.
   * - ``max_descendants``
     - ``null`` (unlimited)
     - Maximum unsettled descendants in the whole subtree, checked on every
       ancestor at spawn time.
   * - ``step_retries``
     - ``null`` → ``1``
     - Extra attempts a *failed* step launch gets; ``0`` disables retries.
       Timed-out, paused, and budget-skipped steps never retry.

Cost amounts (USD)
~~~~~~~~~~~~~~~~~~

All cost keys are USD numbers. The ceilings must be positive; the ordering
``max_step_cost <= max_iter_cost <= max_cost`` is enforced, and the
per-iteration and per-step caps require ``max_cost``. :doc:`/guide/loop`
describes how spend is measured and how the caps are enforced.

.. list-table::
   :header-rows: 1
   :widths: 16 22 62

   * - Key
     - Default
     - Meaning
   * - ``max_cost``
     - ``null`` (uncapped)
     - Per-**run** spend ceiling for the node's subtree (its own steps plus
       descendant runs). Re-armed at every launch — it is not a lifetime
       budget. Starting an uncapped node is allowed but logs a loud warning:
       spend is untracked and bounded only by ``max_iters``/``timeout``.
   * - ``max_iter_cost``
     - ``null``
     - Per-iteration cap. Requires ``max_cost`` and must not exceed it.
       Reaching it puts the rest of the iteration into reserve (wind-down)
       mode rather than hard-stopping it.
   * - ``max_step_cost``
     - ``null``
     - Per-step cap. Requires ``max_cost``; ordering as above. **Hard only
       for backends that enforce a step budget** (see :doc:`/guide/agents`
       for which do); warn-only for all others — in general, do not rely on
       it as a hard cap.
   * - ``reserve_budget``
     - 10% of ``max_cost``
     - Cleanup buffer below ``max_cost``; see `The cleanup reserve`_. Exists
       only when ``max_cost`` does.

Durations
~~~~~~~~~

All duration keys use `the duration format`_. ``null`` means no limit unless a
loop-side default is noted.

.. list-table::
   :header-rows: 1
   :widths: 16 22 62

   * - Key
     - Default
     - Meaning
   * - ``timeout``
     - ``null`` (none)
     - Per-run wall-clock budget, armed once at loop boot. Paused spans are
       credited back on resume.
   * - ``iter_timeout``
     - ``null``; defaults to ``interval`` when ``interval`` is set
     - Per-iteration wall clock, reset each iteration. Must not exceed
       ``interval``.
   * - ``step_timeout``
     - ``null`` (none)
     - Per-step ceiling; the effective step limit is the minimum of the
       remaining run wall, the remaining iteration wall, and this value.
       Re-read each iteration, so retunes land mid-run.
   * - ``step_retry_backoff``
     - ``null`` → ``10s``
     - Delay before each retry of a failed step launch.
   * - ``interval``
     - ``null``
     - Fixed iteration cadence (an iteration starts every ``interval``).
       Mutually exclusive with ``sleep``.
   * - ``sleep``
     - ``null``
     - Fixed gap between iterations. Mutually exclusive with ``interval``.
   * - ``wait``
     - ``null`` → ``1m``
     - Length of each wait cycle during a child drain or an approval gate;
       sets how often the waiting SYNC step runs. Gate and signal checks
       poll every ``min(wait, 5)`` seconds within a cycle.

Inheritance at spawn
~~~~~~~~~~~~~~~~~~~~

``root`` is always inherited from the parent, and ``project`` is inherited by
default (``node init --path <sub-project>`` selects a different sub-project
for the child; either way the key is immutable after init); ``agent`` and
``provider`` resolve through the nearest ancestor that sets them; a ``local``
parent forces ``local`` children. Everything else defaults fresh unless the
spawn passes ``--inherit config``, which copies the parent's *preference* keys
— ``model``, ``effort``, ``sync``, ``detached``, ``iter_timeout``,
``step_timeout``, ``step_retries``, ``step_retry_backoff``, ``wait``, and
``sleep``/``interval`` (only when the spawn sets neither) — as a spawn-time
snapshot. Budget-class keys (the cost caps, ``max_iters``, the width/depth
caps, and the run ``timeout``) never inherit; each node's budgets are set
deliberately. See :doc:`/cli/node` for the ``--inherit`` surface.

Immutable keys
--------------

These keys are fixed at init and can never be changed:

``root``
    Anchors the central database for the whole tree — every node resolves the
    DB through it. Changing it would point the node at a different database.

``user``
    Marks user/root-node identity. Flipping it would let a root branch be
    started as a loop (or a child masquerade as the root and latch tree-wide
    behavior).

``project``
    Fixes the on-disk layout mirrored by the per-branch project cache under
    ``.worktrees/``; the track/untrack, lint, and merge machinery all read
    it, so a post-init change would desync them from the real paths.

The public ``fractal node config set`` refuses these keys outright — even a
first write (so ``config set user=true`` cannot turn a child into a root
node). Only the internal bootstrap path used by ``node init`` seeds them, and
even that path admits only the initial write, never a change.

The cleanup reserve
-------------------

``reserve_budget`` sets aside part of ``max_cost`` as a wind-down buffer: when
the run's remaining budget drops to the reserve, the node enters reserve mode
— the prompt gains wind-down instructions, approval gates are skipped, and the
run ends at the iteration boundary. The reserve is **not enforced** as a
separate cap; it only moves the point where reserve mode begins (the budget is
treated as drained ``reserve_budget`` USD before ``max_cost`` is reached).
See :doc:`/guide/loop` for reserve-mode behavior.

Defaulting and rounding:

- On the CLI (``node init --reserve-budget``, ``node update
  --reserve-budget``), the value is a USD number or ``N%`` of the effective
  ``max_cost`` — the percent form is resolved to USD before it is stored, so
  ``config.json`` always holds a plain number.
- When the flag is omitted and ``max_cost`` is set, the default **10% of
  ``max_cost``** is materialized into the config. With no ``max_cost`` there
  is no reserve. The same 10% default is applied at loop read time when
  ``max_cost`` was set out of band (e.g. via ``config set``) with no reserve
  stored.
- The materialized amount is rounded to 4 decimal places, so no binary float
  noise persists into ``config.json``.
- An explicit reserve requires ``max_cost`` and must sit in the range from 0
  (inclusive) to 99% of ``max_cost`` (exclusive).
- On ``node update --max-cost`` without ``--reserve-budget``, a reserve that
  equals the old cap's 10% default is re-derived against the new cap instead
  of going stale; an explicitly set reserve is left alone.

Validation
----------

One merged validator runs at ``node init`` (over the flags), at
``node config set`` and ``node update`` (over the merged result), and again at
``node start`` (over the stored file, since direct edits bypass the setters).
It rejects:

- non-numeric or non-finite cost values, and non-positive cost ceilings;
- ``max_iter_cost`` or ``max_step_cost`` without ``max_cost``, and any
  violation of the ``step <= iter <= run`` cost ordering;
- a reserve outside ``[0, 99% of max_cost)``;
- non-integer or negative values for the integer caps, and
  ``max_iters <= 0``;
- non-boolean values for the mode flags;
- bare-number durations and durations under one whole second;
- ``interval`` and ``sleep`` both set, or ``iter_timeout`` exceeding
  ``interval``;
- absolute paths or ``..`` components in ``scope``.

Per-step overrides
------------------

A step file's frontmatter can override part of the node config for that step
alone. The overlapping keys and their precedence:

.. list-table::
   :header-rows: 1
   :widths: 20 24 56

   * - Frontmatter key
     - Node config key
     - Precedence
   * - ``agent``
     - ``agent``
     - Frontmatter wins for this step; validated at that step's launch.
   * - ``provider``
     - ``provider``
     - Frontmatter wins over the node's effective (own or inherited) route.
   * - ``model``
     - ``model``
     - Frontmatter wins; otherwise the node config value; otherwise the
       agent's own default.
   * - ``effort``
     - ``effort``
     - Frontmatter wins.
   * - ``timeout``
     - ``step_timeout``
     - **Substitutes for** the node value — it can be looser, not just
       tighter — but the run and iteration walls still bound it. A malformed
       value warns and falls back to the node value.
   * - ``detached``
     - ``detached``
     - ``detached: true`` detaches a single step inside a continuous node.
       In an already-detached node, any non-empty ``detached`` value is an
       error that fails the step.

``requires_approval`` is frontmatter-only and has no node-config counterpart.
The full frontmatter contract — grammar, the complete key set, and step-file
mechanics — is covered in :doc:`/guide/plans`.
