Plans and Steps
===============

Two markdown surfaces in a node's data directory drive its work. **Step
files** under ``steps/`` are the node's per-iteration script: every iteration
executes them in order, and each file can tune how its own launch runs.
**Plan files** under ``plans/`` are the records the agent writes while
planning: each is stamped with the run and iteration that produced it, so the
node's decision history stays reconstructible after the fact.

Both directories live in the node directory (``.fractal/<branch>/`` inside
the node's worktree). Step files are authored and edited by the operator
(and by a parent node when it steers); plan files are normally created by
the agent itself, via ``fractal plan init`` during the seeded ``01-PLAN``
step.

Plan files
----------

A plan file is a markdown file under ``<node_dir>/plans/`` named
``{timestamp}-{run.iter}-{name}.md``:

- ``timestamp`` is the creation time in UTC (ISO 8601, millisecond
  precision), so two plans written in the same iteration get distinct names.
- ``run.iter`` is the iteration reference — run 2, iteration 5 is ``2.5``.
  Iterations number from 1 within each run, so the reference pins the plan to
  one specific loop pass.
- ``name`` is a short snake_case slug describing the plan.

The file is seeded with a single H1 line, ``# {run.iter} {title}``, so the
originating iteration is human-readable inside the file as well as in the
filename; the agent writes the plan body below it. A plan for iteration
``2.5`` might be ``plans/2026-01-01T00:00:00.000Z-2.5-refactor_scheduler.md``
beginning:

.. code-block:: markdown

   # 2.5 Refactor Scheduler

The seeded ``01-PLAN`` step instructs the agent to create a plan file every
iteration (one per concern when the work splits cleanly), check it against
the remaining time and cost budget, and trim it to fit. The seeded
``03-REVIEW`` step closes it: the agent lists the iteration's plans with
``fractal plan list`` and appends a ``## Post-Mortem`` section to each
(accomplishments, deviations, next-iteration notes) — or, when it adopted a
plan from an interrupted earlier iteration, appends to that plan and notes
the adoption. Because plans are plain files in the node directory, they are
committed with the node's work product and survive on the node's branch.

Creating a plan
~~~~~~~~~~~~~~~

``fractal plan init`` creates the file and prints its absolute path:

.. code-block:: console

   $ fractal plan init --name=refactor_scheduler --iter-ref=2.5
   /home/user/myproject/.worktrees/main.parser/.fractal/main.parser/plans/2026-01-01T00:00:00.000Z-2.5-refactor_scheduler.md

``--name <slug>`` (required)
   Short descriptive slug for the plan. Strictly validated: letters, digits,
   and underscores only (``[A-Za-z0-9_]+``).

``--title <text>``
   Title for the seeded H1. Defaults to the de-slugged name
   (``refactor_scheduler`` becomes ``Refactor Scheduler``).

``--iter-ref <run.iter>`` (required; reads the ``ITER_REF`` environment variable)
   The ``run.iter`` reference, strictly validated to the ``2.5`` form. The
   loop exports ``ITER_REF`` into every agent launch, so inside a run the
   flag can be omitted — the seed steps call
   ``fractal plan init --name=<name>`` with no ``--iter-ref``. Outside a
   run (an operator at a shell) the flag must be passed explicitly.

``--path <dir>`` (default ``.``)
   Worktree directory identifying the node.

Creation is exclusive: if a file with the same name already exists, the
command errors rather than silently replacing the earlier plan.

Listing plans
~~~~~~~~~~~~~

``fractal plan list`` prints an iteration's plan paths, one per line, sorted
by filename (chronological, since the timestamp leads):

.. code-block:: console

   $ fractal plan list --iter-ref=2.5
   /home/user/myproject/.worktrees/main.parser/.fractal/main.parser/plans/2026-01-01T00:00:00.000Z-2.5-refactor_scheduler.md

``--iter-ref <run.iter>`` (required; reads ``ITER_REF``)
   The ``run.iter`` reference to list plans for.

``--path <dir>`` (default ``.``)
   Worktree directory identifying the node.

Plans are matched by globbing the ``{run.iter}`` segment of the filename, so
the listing returns every plan the iteration wrote — zero, one, or many —
without relying on modification times. When there are none, the notice
``No plans for this iteration.`` goes to stderr; stdout stays a pure
path-per-line surface, safe to pipe.

The exact CLI surface is on the :doc:`/cli/plan` page.

Step files
----------

Step files live at ``<node_dir>/steps/NN-<NAME>.md``. Each iteration of the
loop executes them in lexicographic filename order; the ``NN-`` digit prefix
is what orders them, and the step's *name* is the filename stem past its
first dash (``02-EXECUTE.md`` is step ``EXECUTE``). A fresh node is seeded
with the following steps:

- ``00-PREPARE`` — merge the parent branch, survey children.
- ``01-PLAN`` — orient in memory and the project wiki, write the iteration's
  plan file, check it against the budget.
- ``02-EXECUTE`` — execute the plan, including spawning and managing child
  nodes.
- ``03-REVIEW`` — review the diff, fix issues, update memory, close the
  iteration's plans with a post-mortem.
- ``04-COMMIT`` — signal completion when the requirements are met, then
  ``fractal commit``.

The seed is a starting point, not a fixture: add, remove, rename, or reorder
files freely. Steps are re-discovered and re-parsed at the top of every
iteration, so edits land at the next iteration boundary without a restart —
step files are a live steering surface for a running node. Discovery is
strict, and a violation fails the iteration loudly: an empty ``steps/``
directory (``no step files``), a file without the ``NN-`` digit prefix, or
files mixing digit-prefix widths (``invalid step files``). A step file that
is not valid UTF-8 aborts the run at preflight, naming the file.

Step structure
~~~~~~~~~~~~~~

A step file is an optional frontmatter block followed by a markdown body:

.. code-block:: markdown

   ---
   requires_approval: true
   timeout: 10m
   ---

   ## Execute

   Execute the plan. Work only under $SCOPE_DIR ...

The frontmatter is **not YAML**. It is a strict flat-scalar grammar: the
block opens with ``---`` on the very first line and closes at the next
``---``; each line inside must be a lowercase key (letters and underscores),
a colon, and a non-empty value. Nothing nests, nothing is quoted specially,
non-matching lines are ignored, and the first occurrence of a key wins. A
file whose first line is not ``---`` simply has no frontmatter.

The body is the step's instruction text. At launch the loop strips the
frontmatter and assembles the prompt as the node's task contract
(``NODE.md``), the step body, and any active mode documents, rendering
``$VAR`` and ``${VAR}`` placeholders from the template-variable map
(unknown placeholders pass through verbatim). Prompt assembly and the full
variable vocabulary are covered in :doc:`/guide/loop`.

Step config vs node config
--------------------------

A node's run parameters live in its ``config.json`` (see
:doc:`/configuration`). A step's frontmatter can override a small, fixed
subset of them for that step alone. The complete set of keys a step may set:

.. list-table::
   :header-rows: 1
   :widths: 18 22 60

   * - Key
     - Values
     - Effect
   * - ``requires_approval``
     - ``true`` (anything else means false)
     - Gates the step: after it succeeds, the loop waits for the parent's
       ``fractal node approve`` before the iteration proceeds. No node-config
       counterpart.
   * - ``agent``
     - an agent command
     - Overrides the node's ``agent`` for this step. Validated at launch
       (registry and installed binary); an invalid override fails the step,
       not the loop.
   * - ``provider``
     - a route name (e.g. ``openrouter``)
     - Overrides the node's effective ``provider`` for this step. A route the
       step's agent does not support fails the step at launch; backends with
       no route axis ignore the key.
   * - ``model``
     - a model id
     - Overrides the node's ``model`` for this step. The pin (this key, else
       the node ``model``; a step with neither goes unchecked) is enforced
       from the agent's stream: a launch that completes on a different
       model — an alias, gateway slug, or dated snapshot of the pin counts
       as a match — records a ``model_drop`` event and re-dispatches once
       after ``step_retry_backoff``, outside the ``step_retries`` allowance.
       A re-run also served off-pin, or one the deadline cannot fit, fails
       the step rather than proceeding on the wrong model's output — the
       iteration fails, not the loop.
   * - ``effort``
     - a provider-specific level
     - Overrides the node's ``effort`` for this step. Passed through
       unvalidated — an unknown level surfaces as the agent's own error.
   * - ``timeout``
     - a duration (``30s``, ``10m``, ``1.5h``)
     - Substitutes for the node's ``step_timeout`` for this step. A malformed
       value warns and falls back to the node value.
   * - ``detached``
     - ``true`` / ``false`` / empty
     - ``true`` runs this step as a fresh agent invocation in a continuous
       node; ``false`` or empty restates the default (a no-op). In an
       already-detached node, any non-empty value is an error that fails the
       step.

Any other frontmatter key is parsed but has no effect. Everything else in
the node's configuration — budgets, retries, pacing, iteration caps — is
node-level only and cannot be set per step.

Precedence when both define a value:

- ``agent``, ``provider``, ``model``, ``effort`` — step frontmatter wins over
  node config, for that step only. The node's ``agent`` must be set in its
  own ``config.json``: ``fractal node init`` writes the value inherited from
  the nearest ancestor there, and clearing it afterwards refuses
  ``fractal node start`` rather than falling back. When the node config
  leaves ``provider`` unset, the route resolves up the ancestor chain at
  launch, and an unset ``model`` falls back to the agent's own config-file
  default — which is recorded on the step row but not passed on the command
  line. See :doc:`/guide/agents` for the agent, provider, model, and effort
  surfaces.
- ``timeout`` — the frontmatter value *replaces* the node ``step_timeout``
  entirely; it does not tighten it, so it may be looser. The run and
  iteration walls (``timeout``, ``iter_timeout``) still bound the launch:
  the effective limit is the minimum of the run remaining, the iteration
  remaining, and the step's own ceiling.
- ``detached`` — node ``detached: true`` makes every step a fresh invocation
  and is pinned at boot; a step-level ``detached: true`` detaches a single
  step of an otherwise continuous node. The two do not combine: a non-empty
  step value in a detached node is a per-step configuration error.

How the loop consumes steps
---------------------------

At the top of every iteration the loop re-discovers and re-parses the step
files, then runs each in order. Around each step:

- **Signal checkpoints.** The loop checks for a pause before every step
  (parking with no commit — the dirty worktree is the state resume continues
  from) and for a stop or a tripped subtree budget ceiling between steps.
- **SYNC.** When the node's ``sync`` config is enabled (the default), a SYNC
  pseudo-step precedes each step: a separate agent turn, with its own
  database row, that processes radio traffic and steering before the step's
  own launch. A failed SYNC is non-fatal — the loop continues to the step —
  but a timed-out SYNC fails the iteration.
- **Finish drain.** Under a finish signal, the loop waits for active children
  to drain before the final step, so the last step runs with the subtree
  settled.
- **Launch.** The prompt is assembled and the agent is launched, bounded by
  the effective time limit and the per-step budget leash (see
  :doc:`/guide/loop`). In a continuous node, the iteration's steps resume one
  agent session per agent, so context carries from step to step; a detached
  node (or a ``detached: true`` step) gets a fresh invocation instead.
- **Recording.** Every launch attempt books its own step row in the central
  database — status, exit code, session, cost, and a short reason on
  abnormal ends — so ``fractal node activity`` reads honestly per attempt.
- **Retries.** Only a *failed* launch retries (``step_retries``, default 1,
  after a ``step_retry_backoff``, default ``10s``). Timed-out, paused, and
  budget-skipped steps are deliberate outcomes and never retry. Two
  exceptions to the plain schedule: a *completed* launch served off its
  pinned ``model`` re-dispatches once, after the same backoff but outside
  the ``step_retries`` allowance, and a drop the re-dispatch cannot resolve
  fails the step; and after three consecutive instant (under 10s) zero-cost
  failures — SYNC and step launches both count — the billing breaker
  replaces the backoff with an exponential wait (60s, doubling to a 1h cap)
  and holds every further launch, the before-step SYNC included, behind a
  ``PAUSED: billing`` gate until a probe launch completes (or fails without
  that signature).
- **Approval.** A step marked ``requires_approval: true`` that succeeds
  holds the iteration until the parent runs ``fractal node approve`` (the
  parent's ``fractal node pending`` lists waiting steps). The wait counts
  against the run and iteration deadlines — a gated step can time out
  waiting — and reserve mode (budget wind-down) skips the gate. Each retry
  attempt re-arms its own gate.

A step that ultimately fails ends the iteration: the never-run tail steps
are recorded as ``stopped`` rows carrying ``failed on <step>`` so the
activity log answers which steps never ran, and any work product on disk is
force-committed as a backstop. Lifecycle outcomes, timeouts, and budgets are
covered in :doc:`/guide/loop` and :doc:`/guide/lifecycle`.
