The Iteration Loop
==================

A started node runs an iteration loop inside its tmux session. The loop
assembles a prompt for each step, launches the agent, enforces time and cost
limits, commits the work product, and records every run, iteration, and step
as rows in the tree's central SQLite database. This page describes what the
loop does on each pass, what the agent sees, how execution is bounded, and how
to watch it all happen. For starting, pausing, and finishing nodes see
:doc:`/guide/lifecycle`; for choosing agents and models see
:doc:`/guide/agents`.

Runs, iterations, and steps
---------------------------

Execution nests three levels deep:

- A **run** is one launch of the loop — one ``fractal node start`` (or
  ``--continue`` relaunch). Budgets and the iteration cap are per-run: every
  launch arms the cost ceiling anew and restarts the iteration count at 1.
- An **iteration** is one pass through the node's step files. Iterations are
  numbered from 1 within their run, and every label is run-qualified as
  ``<run>.<iter>`` (e.g. iteration ``2.3``) — in the activity log, in commit
  subjects, and in the ``ITER_REF`` variable the agent sees.
- A **step** is one agent invocation, driven by one step file. Each iteration
  executes the step files in order, optionally preceding each with a SYNC
  step (below).

Step files
~~~~~~~~~~

Steps live in the node directory at ``steps/NN-<NAME>.md`` and are discovered
fresh at the top of every iteration, sorted lexicographically — they are a
live steering surface, and edits land on the very next iteration. The seed
steps installed by ``fractal node init`` are ``00-PREPARE``, ``01-PLAN``,
``02-EXECUTE``, ``03-REVIEW``, and ``04-COMMIT``.

The filename contract is strict, and violations fail the iteration loudly
(recorded as ``no step files`` or ``invalid step files``):

- The directory must contain at least one ``*.md`` file.
- Every file must start with a digit prefix and a dash (``NN-``).
- All prefixes must use the same digit width (no mixing ``2-`` with ``02-``).

The step's *name* is the filename stem past its first dash: ``02-EXECUTE.md``
runs as step ``EXECUTE``. A step file may open with a frontmatter block that
overrides per-step behavior (see `Step frontmatter`_).

The shape of one iteration
~~~~~~~~~~~~~~~~~~~~~~~~~~

Each iteration proceeds as follows:

1. **Signal checkpoint.** Pause, finish, and stop signals are checked before
   anything runs (pause always outranks the others; see
   :doc:`/guide/lifecycle`).
2. **Setup.** ``scripts/setup.sh`` runs from the worktree root, teed to
   ``setup.log`` in the node directory. Setup is unbounded and unpausable —
   the time limits gate step launches only. A failed setup fails the
   iteration; three *consecutive* failures end the run as ``exited``.
3. **Steps, in order.** Before each step, a SYNC step runs (when ``sync`` is
   enabled), then the step itself launches. Signal and budget checkpoints
   fall between steps.
4. **Approval gates.** A step whose frontmatter sets
   ``requires_approval: true`` completes and then waits for the parent to
   release it with ``fractal node approve`` before the iteration proceeds.
5. **Commit backstop.** If the worktree is dirty after the iteration, the
   loop force-commits the leftovers (see `Backstop commits`_).
6. **Pacing.** With ``interval`` or ``sleep`` set, the loop sleeps before the
   next iteration, polling signals every 30 seconds at most so a pause or
   finish never waits for the next wake.

The SYNC step
~~~~~~~~~~~~~

When the ``sync`` config key is enabled (the default), a SYNC pseudo-step runs
before every step, and again periodically while the loop waits — draining
children on a finish, or waiting on an approval gate. SYNC uses the packaged
``SYNC.md`` mode document as its step body (it is never appended to other
steps' prompts) and books its own step row, taking the number of the step it
precedes (a drain-wait SYNC books step 0; an approval-wait SYNC is charged
to the awaited step's number).

SYNC failure semantics differ from those of work steps: a *failed*
before-step SYNC is non-fatal — the loop continues to the step it precedes —
but a *timed out* SYNC is fatal: the loop force-commits and fails the
iteration. A waiting SYNC never fails its wait; a failed launch there records
``completed`` with the failure named in the row's metadata
(``sync failed (exit N)``).

Continuous and detached modes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

In the default *continuous* mode, the steps of one iteration share one agent
session per agent — each step resumes the conversation the previous step left.
The session map resets at each iteration start, so iterations begin fresh.
With ``detached: true`` in the node config, every step runs as an independent
agent invocation with no shared session. The mode is pinned when the run
boots; a mid-run config edit cannot flip it. A single step of a continuous
node can be detached with ``detached: true`` frontmatter.

Pacing and loop keys
~~~~~~~~~~~~~~~~~~~~

These ``config.json`` keys shape the loop itself (set them at
``fractal node init`` or edit the file between runs; see
:doc:`/configuration` for the full reference):

.. list-table::
   :header-rows: 1
   :widths: 22 14 64

   * - Key
     - Default
     - Meaning
   * - ``max_iters``
     - unlimited
     - Iterations per run. Re-read each iteration, so a retune lands
       mid-run. ``--continue`` restarts the count at 1.
   * - ``interval``
     - none
     - Fixed cadence: the next iteration starts ``interval`` after the
       previous one began (the loop sleeps out the remainder). Mutually
       exclusive with ``sleep``. When no ``iter_timeout`` is set, the
       interval becomes the per-iteration deadline; an ``iter_timeout``
       longer than the interval is rejected.
   * - ``sleep``
     - none
     - Fixed gap slept between iterations. Mutually exclusive with
       ``interval``.
   * - ``wait``
     - ``1m``
     - Length of each wait window while draining children or awaiting
       approval; between windows the loop runs a SYNC step. Signals are
       polled every few seconds regardless.
   * - ``sync``
     - ``true``
     - Run the SYNC step before each work step (and while waiting).
   * - ``detached``
     - ``false``
     - Run every step as a fresh agent invocation. Pinned at boot.

What the agent sees each step
-----------------------------

Every step launch assembles one prompt from three parts, joined in order:

1. The node's **task contract** — ``NODE.md`` from the node directory,
   verbatim.
2. The **step body** — the step file with its frontmatter block stripped.
3. The active **mode documents** — packaged instruction files that join the
   prompt when their mode is on: ``CONTINUE.md`` on a ``--continue`` run,
   ``RESUME.md`` on the iteration a resume re-enters, ``RESERVE.md`` while
   the budget is winding down, ``DRAIN.md`` on a ``--continue --drain`` run
   (a wind-down: the harness refuses ``fractal node init``, ``start``,
   ``update``, and ``resume`` from that run's seats, and the drain is
   recorded on the run so a pause and resume keep it), ``DETACHED.md`` in
   detached mode, and ``META.md`` for a meta node. Mode documents always
   come from the installed package, never a per-node copy.

The joined text is rendered in one pass with ``$VAR`` / ``${VAR}``
substitution matching GNU ``envsubst``: unknown placeholders and ``$$`` pass
through verbatim, so ordinary shell text in a step file survives rendering.

On the iteration a resume re-enters, the prompt also ends with an
unread-inbox digest, appended after rendering (so it is never substituted):
the twenty highest-priority unread ``inbox`` messages as metadata only —
priority, uuid, sender, and subject, the text fields quoted and
length-bounded — framed as untrusted data with the ``fractal radio read
--channel=inbox --unread`` command spelled out. A sealed inbox stays sealed
here too (the read runs as the node itself), and a failed read simply omits
the digest.

Template variables
~~~~~~~~~~~~~~~~~~

Static variables are pinned once at boot in the exported environment. In
prompts they are re-derived per step from live config — only
``DETACHED_MODE`` and ``META_MODE`` stay boot-pinned in prompts as well:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Variable
     - Value
   * - ``REPO_DIR``, ``PROJECT_DIR``, ``WORKTREE_DIR``, ``NODE_DIR``
     - The repository, project, worktree, and node-directory paths.
   * - ``SCOPE_DIR``
     - The scope roots as absolute paths, space-joined (empty when
       unscoped).
   * - ``PLANS_DIR``, ``MEMORY_DIR``, ``WIKI_DIR``
     - The node's plans directory, memory wiki, and the shared project
       wiki.
   * - ``CURRENT_BRANCH``
     - The node's branch.
   * - ``MAX_DEPTH``, ``MAX_CHILDREN``, ``MAX_DESCENDANTS``
     - The node's spawn limits. An unset cap renders as ``None`` on a node
       seeded by ``fractal node init`` (the seed stores unset keys as
       ``null``); ``-1`` appears only when the key is absent from
       ``config.json``.
   * - ``DETACHED_MODE``, ``META_MODE``, ``META_TARGET``
     - The boot-pinned mode flags (``true``/``false``) and the meta
       target branch.
   * - ``FRACTAL_HEADLESS``
     - The run's launch backend (exactly ``true``/``false``), exported to
       every seat's environment only — never substituted in prompts;
       ``node start`` reads it when the flag is absent, so a seat's child
       starts follow the parent's backend.

Run-scoped variables are refreshed for every step:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Variable
     - Value
   * - ``STEP_LABEL``
     - E.g. ``step 3 of 5 (EXECUTE)``.
   * - ``ITER_LABEL``
     - E.g. ``3 of 10``, or ``3 (no limit)``.
   * - ``ITER_REF``
     - The run-qualified iteration reference, e.g. ``2.3``. Feeds
       ``fractal plan init`` (see :doc:`/guide/plans`).
   * - ``ITER_TIMESTAMP``
     - The iteration's UTC start timestamp.
   * - ``TIME_BUDGET``
     - The configured walls, e.g. ``24h total, 1h/iter, 10m/step``, or
       ``no limit``.
   * - ``COST_BUDGET``
     - Live remaining budget, recomputed before *every* step, e.g.
       ``$87.6543 remaining of $100 (max $10/iter) (warn $1/step)``, or
       ``no limit``.
   * - ``CONTINUE_MODE``, ``RESUME_MODE``, ``RESERVE_MODE``, ``DRAIN_MODE``
     - ``true``/``false`` flags for the corresponding modes
       (``RESERVE_MODE`` is per-iteration).

The agent's environment
~~~~~~~~~~~~~~~~~~~~~~~

Separately from the prompt, each launch exports the loop's state into the
agent's process environment: all of the variables above except
``RESERVE_MODE`` and ``DRAIN_MODE`` (which appear only in the rendered
prompt — a draining run exports ``_DRAIN=1`` instead), plus ``RUN_ID``,
``ITER``, ``ITER_ID``, and ``STEP_ID`` (the row lineage — always exported,
blank when unset), ``NODE_BRANCH``, ``AGENT_COMMAND``, the cap values
(``MAX_COST``, ``MAX_ITER_COST``, ``MAX_STEP_COST``, ``STEP_BUDGET``), the
timeout values in seconds (``RUN_TIMEOUT_SECONDS``, ``ITER_TIMEOUT_SECONDS``,
``STEP_TIMEOUT_SECONDS``, ``INTERVAL_SECONDS``, ``STEP_LIMIT_SECONDS``), the
wall-clock deadlines (``RUN_END_EPOCH``, ``ITER_END_EPOCH``), and the step's
``STEP_MODEL`` and ``STEP_DETACHED``. Scripts and tools the agent runs can
read these directly — ``fractal plan init``, for example, defaults its
``--iter-ref`` from ``ITER_REF``.

Step frontmatter
----------------

A step file may open with a frontmatter block:

.. code-block:: markdown

   ---
   requires_approval: true
   timeout: 10m
   model: <model-id>
   ---

   ## Execute

   Step instructions ($VAR placeholders allowed)...

The grammar is deliberately not YAML: the block opens with ``---`` on the
very first line and closes at the next ``---``, and each line inside must be
a flat ``key: value`` scalar (lowercase key, non-empty value). Nothing nests,
nothing quotes specially, non-matching lines are ignored, and the first
occurrence of a key wins. A file that does not start with ``---`` has no
frontmatter. The full key set:

.. list-table::
   :header-rows: 1
   :widths: 24 76

   * - Key
     - Effect
   * - ``requires_approval``
     - ``true`` gates the step: after it succeeds, the iteration waits for
       the parent's ``fractal node approve`` before proceeding. Skipped in
       reserve mode. Any other value means false.
   * - ``agent``, ``provider``, ``model``, ``effort``
     - Override the node's configured agent, provider route, model, and
       reasoning effort for this step (see :doc:`/guide/agents`).
   * - ``timeout``
     - A duration that *substitutes for* the node ``step_timeout`` on this
       step — it may be looser, but is still bounded by the run and
       iteration walls. A malformed value warns and falls back to the node
       value.
   * - ``detached``
     - ``true`` detaches this step in a continuous node (fresh invocation,
       no shared session). ``false`` or empty is a no-op. In an
       already-detached node any non-empty value is an error that fails the
       step.

Retries and backoff
-------------------

Only a **failed** launch retries — an agent error, a stream error, or a
launch that could not start. Timed-out, paused, and budget-skipped steps are
deliberate outcomes and never retry (a completed launch served off its pinned
model re-dispatches once — see below). These config keys control it:

.. list-table::
   :header-rows: 1
   :widths: 26 12 62

   * - Key
     - Default
     - Meaning
   * - ``step_retries``
     - ``1``
     - Extra attempts a failed launch gets; ``0`` disables retries.
   * - ``step_retry_backoff``
     - ``10s``
     - Delay before each retry. Slept in one-second increments, polling
       pause (parks the run), stop, finish, and the subtree cost ceiling —
       a failed step never buys another attempt once the budget is spent.

Every attempt books its own step row, so cost and duration are attributed
accurately per attempt; retry rows carry a ``retry`` marker in their
metadata. Each attempt recomputes its time limit from the now-smaller
remaining walls, and each attempt of an approval-gated step re-arms its own
gate.

One completed outcome also re-dispatches. When the stream reports that the
launch was served by a model other than the step's pin (``model:``
frontmatter, or the node ``model`` it fell back to; an alias, gateway slug,
or dated snapshot of the pin still matches), that is a **model drop**: the
attempt's row closes ``completed`` with metadata ``model drop (served
<model>)``, a ``model_drop`` event is recorded, the pane prints a
``WARNING: model drop on <step>`` line, and the step is re-dispatched once
after ``step_retry_backoff`` — outside the ``step_retries`` allowance, so
the re-dispatch row carries no ``retry`` marker, and a clean re-dispatch
supersedes the mark. If the re-dispatch also serves off-pin, or cannot run
(the deadline is spent, or a stop, finish, or the subtree ceiling lands
during the backoff), the step fails rather than proceeding on wrong-model
output: the dropped row keeps its mark, the steps after it are booked
``stopped`` with ``failed on <step>``, and the iteration row records
``model drop (served X, pinned Y)``. The node is never killed; the next
iteration start warns while the drop stands unresolved.

Billing breaker
~~~~~~~~~~~~~~~

A launch that fails in under 10 seconds with zero recorded cost never
reached paid inference — the signature of an API billing or credit refusal.
Three such failures in a row trip the loop's billing breaker. Every agent
launch counts toward the streak, the before-step SYNC included; a
cannot-exec launch (exit 127), a failure that ran 10 seconds or longer, or
one that spent real money is not billing-shaped and resets the streak to
zero (which also disarms an armed breaker). Paused and budget-skipped steps
bought no launch and leave the streak untouched.

While the breaker is armed, every launch — the SYNC and the work step alike
— is held behind a backoff that starts at 60 seconds and doubles per further
failure to a one-hour cap, announced on the pane as ``=== PAUSED: billing —
N instant zero-cost failure(s); probing again in Ns ===``. A failed step's
retry under the breaker waits that clock instead of ``step_retry_backoff``
(``retrying in 60s (billing breaker)``). The launch after each wait is the
probe: a completed launch clears the breaker (``=== billing breaker cleared
(a call succeeded) ===``). The wait polls the same signals as a retry
backoff: a pause parks the run mid-wait; a stop, finish, or spent subtree
ceiling ends the iteration, booking the gated step and the never-run tail as
``stopped`` rows with ``billing gate interrupted`` in their metadata (a
knowable-zero spend), and the iteration closes ``stopped``, never
``completed``. The threshold, base, and cap are fixed — no config key tunes
them. While an active node's newest launches carry this signature,
``fractal node list`` shows ``PAUSED: billing`` in its ``detail`` column
(see :doc:`/cli/node`).

Time limits
-----------

Every duration key takes the form ``<number><unit>`` with a unit of ``s``,
``m``, ``h``, or ``d`` (``30s``, ``10m``, ``1.5h``, ``2d``). Bare numbers are
rejected, and values are truncated to whole seconds — a duration that
truncates to zero is refused.

Three walls bound execution:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Key
     - Meaning
   * - ``timeout``
     - The whole-run wall, computed once at boot. A resumed run anchors on
       the pause-credited remaining time, so parked time is never charged.
   * - ``iter_timeout``
     - The per-iteration wall, reset at each iteration start (an adopted
       iteration resumes with its credited remaining).
   * - ``step_timeout``
     - The per-step ceiling. Re-read from config at each iteration top, so
       a mid-run retune lands.

All three default to none (no limit). A step's effective time limit is the
minimum of the run remaining, the iteration remaining, and the effective step
timeout (the ``timeout:`` frontmatter when present, else ``step_timeout``).
A deadline that has already passed before launch records the step as timed
out without spawning anything.

Enforcement is in-process: at the limit the loop sends ``SIGTERM`` to the
agent's whole process group, waits a 10-second grace, then ``SIGKILL``\ s any
survivor. An agent that shuts down cleanly within the grace is still recorded
as timed out — the limit was reached.

How the walls interact:

- With ``interval`` set and no ``iter_timeout``, the interval doubles as the
  iteration deadline; an explicit ``iter_timeout`` longer than the interval
  is rejected.
- Between-iteration sleeps never cross the run deadline.
- Finish drains (waiting for children) deliberately clear the iteration
  deadline and are bounded by the run wall alone.
- Approval waits burn the clock: a gated step can time out waiting for
  approval, and the recorded step duration includes the wait.
- ``setup.sh`` is unbounded — the walls gate step launches only.

A step that times out fails the iteration and records
``<step> timed out (<limit>s)``; an iteration that times out with no launch
to blame records plain ``timed out``.

Cost ceilings
-------------

Cost is **recorded, never estimated**: each agent backend's stream reports
spend figures, which the loop flushes to the step row as they arrive. A step
that recorded no cost counts as zero in every sum, though its ``NULL`` means
"unknown", never "$0"; a step that ended abnormally (any status other than
``completed``) with a session on its row but no figure flushed is marked
``unpriced`` in its metadata, and a figure that flushes later strips the
marker. These config keys (all USD) bound spend:

.. list-table::
   :header-rows: 1
   :widths: 20 20 60

   * - Key
     - Default
     - Meaning and enforcement
   * - ``max_cost``
     - none
     - The per-**run** subtree ceiling: the node's own steps plus every
       descendant run it spawned. Hard — reaching it sends a recursive
       finish that winds the whole subtree down.
   * - ``max_iter_cost``
     - none
     - Per-iteration cap. Soft — once an iteration's recorded spend
       reaches it, the remaining steps run in reserve mode; it also
       shrinks the per-step budget. Requires ``max_cost``.
   * - ``max_step_cost``
     - none
     - Per-step cap. Hard only for agents that enforce a budget flag
       in-flight (see the capability table in :doc:`/guide/agents`);
       **warn-only** for every other backend. Requires ``max_cost``.
   * - ``reserve_budget``
     - 10% of ``max_cost``
     - The cleanup buffer below the ceiling. Once remaining budget drains
       into the reserve, steps run in reserve mode and the run ends at
       the iteration boundary. USD, or ``N%`` of ``max_cost`` at init;
       must be under 99% of ``max_cost``.

Caps must be positive and ordered ``max_step_cost <= max_iter_cost <=
max_cost``.

The per-step budget
~~~~~~~~~~~~~~~~~~~

Before each launch the loop computes a per-step budget: the minimum of the
run's remaining budget minus the reserve, the iteration's remaining headroom,
and ``max_step_cost``. Inside the reserve window the budget floors at the
full run remaining, so wind-down steps may spend the reserve but never past
the ceiling. A non-positive budget skips the launch entirely — the step is
recorded ``stopped`` with reason ``over budget``.

For an enforcing agent the budget is passed as a hard flag; an agent that
stops itself at that budget records a *clean completed* step — a budget stop
is neither a failure nor a goal-met completion. For non-enforcing agents the
cap is advisory: the loop warns after the fact when a step's recorded cost
exceeded ``max_step_cost``, and warns once per run when caps are armed with
no timeout at all (one runaway step could then overshoot without bound — the
warning names ``step_timeout`` as the remedy).

Reserve mode and how a budget ends a run
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Reserve mode is entered per-iteration when the iteration cap is spent, when
the run's remaining budget drains to the reserve, or when an ancestor's
budget abort has cascaded a finish down to this node. It sets
``RESERVE_MODE=true`` so the packaged wind-down instructions join every
prompt, and it skips approval gates. At the iteration boundary the loop then
ends the run; a mid-iteration check also stops queuing steps once the subtree
ceiling itself is crossed.

A budget end always lands the run as ``exited`` with **exit code 0** — the
``exited``/0 pair is how a budget landing is distinguished from both a
completed run and a crash. The recorded reason names which bound tripped
(e.g. ``cost budget reserve reached``, ``subtree cost budget reached``). A
node whose run ended on budget refuses a bare ``fractal node start
--continue``; pass ``--max-cost`` to arm the next run explicitly. One
carve-out: a node whose requirements verified may finish deliberately during
the wind-down, and that run books ``completed`` even when the drain crossed
the cap — the overshoot rides the run row's metadata (``cost budget exceeded
in finish wind-down (spent $X >= $Y max)``), so budget vocabulary in run
metadata alone no longer implies a budget landing.

Pricing and untracked spend
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Token-priced backends and provider routes price usage from a cached copy of
the LiteLLM price table (``~/.fractal/pricing.json``), refreshed at run start
and daily during long runs. If the fetch fails and no cache exists, the run
aborts at preflight; a stale cache warns and is used. A cost cap combined
with an agent whose spend cannot be priced fails the step at launch: no model
set means ``a cost cap requires a model``, and a model missing from the price
table is refused by name. When caps are armed but a run's spend goes entirely
untracked, the loop warns once that the budget guards cannot trip.

The commit pipeline
-------------------

The loop never leaves work uncommitted. The agent is instructed to commit
each iteration's work itself with ``fractal commit``, and the loop backstops
anything left behind.

``fractal commit``
~~~~~~~~~~~~~~~~~~

.. code-block:: console

   $ fractal commit "add tokenizer and parser skeleton"

The message is a bare lowercase summary: the pipeline composes the subject
``<branch>: iteration <run>.<iter> (<message>)`` itself, and refuses a
message that starts with the branch name or ``iteration``. The pipeline then:

1. **Checks scope.** Every changed path (working tree, index, and untracked)
   must fall under a ``scope`` root, the node's data directory, or the shared
   project wiki; violations list the offending paths. Skipped with
   ``--ignore-scope`` or ``--force``.
2. **Refreshes and lints.** ``wiki update`` runs on the project wiki and the
   node's memory wiki, then ``scripts/lint.sh`` runs; either failing fails
   the commit with the tool's output. Skipped with ``--force`` or ``--init``
   (the baseline wiki lints dirty by design).
3. **Stages.** Scoped staging of the scope roots, node directory, and wiki
   (the whole worktree when unscoped), always excluding runtime artifacts
   (``**/.venv``, ``**/.db``, ``**/.db-*``, ``**/.status``, ``**/.paused``,
   temp files). New content under the node directory is admitted only by a
   content allowlist: ``NODE.md`` and ``config.json`` at the node-directory
   root, and under ``memory/``, ``plans/``, ``scripts/``, ``skills/``, and
   ``steps/`` files with a text suffix (``.md``, ``.json``, ``.sh``,
   ``.toml``, ``.yaml``/``.yml``, ``.csv``/``.tsv``, ``.txt``) plus, inside
   those directories, the tool state ``.wiki/`` and ``.gitkeep``. Anything
   else a node parks there — a key, a ``.env`` file, an archive, a
   dot-directory, any other file at the root — is withheld by name and
   reported (``Warning: N estate file(s) are not node records and were NOT
   staged: ...``), on the plain add and the ignore-rule force-add alike;
   what the node directory already tracks keeps committing. Node records
   that only a machine-local ignore layer holds out (a foreign
   ``.git/info/exclude`` line, ``core.excludesFile``) are force-added by
   explicit path and reported (``Force-staged N node record(s) held out by
   an ignore rule: ...``); fractal's own exclude block and repo
   ``.gitignore`` files keep their hold, and records they hold surface
   through step 4's ignore-rule warning instead. ``--check`` applies the
   same law, so a refused untracked file under the node directory never
   counts as uncommitted.
4. **Warns** — never blocks — about paths silently eaten by tracked ignore
   rules and staged files of 10 MB or more.
5. **Commits** and logs a ``commit`` event keyed on the new sha with the
   caller's run/iteration/step lineage. "Nothing staged" is a tolerated
   no-op.
6. **Recovers from hooks.** If the commit fails and a
   ``.pre-commit-config.yaml`` exists, the pipeline re-stages the hook's
   edits and retries once. Wiki pages (the project wiki and the node memory
   wiki) join the retry only when the rewrite preserves wiki structure —
   wikilinks, frontmatter, and separators — and each touched wiki root that
   linted clean before the rewrite still lints clean after it (a
   pre-existing lint failure is surfaced as a notice and tolerated); other
   node-data pages keep byte-identity. A rewrite that breaks either gate is
   restored to its authored bytes and the commit fails with remediation
   guidance.
7. **Pushes** ``origin <branch>`` unless the ``local`` config key is true. A
   missing ``origin`` remote is a skipped-push notice, not an error.

Options: ``--init`` labels the commit ``init`` instead of an iteration (the
baseline commit) and skips the message-shape check, the wiki refresh, lint,
and the ``commit`` event; ``--check`` exits 1 if uncommitted changes exist
instead of committing; ``--ignore-scope`` commits out-of-scope changes but
still lints; ``--force`` bypasses scope, lint, and git hooks; and ``--path``
names the worktree (default ``.``). A dirty tree under ``--check`` is the
command's own outcome, not an error: it exits 1 with the notice on stderr,
while a command error — a bad ``--path`` or flag combination, or a scope,
lint, or message failure on a real commit — exits 2, so scripts gate on the
tree state by exit code. See :doc:`/cli/fractal` for the exact surface.

Backstop commits
~~~~~~~~~~~~~~~~

After each iteration, and again at run end, the loop checks the worktree and
force-commits any leftovers (labeled ``auto`` and ``final``). A step failure
or timeout force-commits with a body naming the failure and the steps that
never ran. Backstop commits bypass hooks and lint by design (``--no-verify``)
and fold the staging notices and a capped diffstat into the body, so the save
describes itself in git history alone.

Pause is the one path that never commits: the dirty worktree *is* the frozen
mid-step state that resume continues from. ``fractal node start --continue``
is the path that discards it (it restores the worktree with a clean), after
first committing any operator edits made under the node directory and
preserving ``config.json`` edits.

What gets recorded
------------------

All rows live in the tree's central database. A run row records the agent,
the cost cap armed at launch, and the parent's active run (which chains
subtree cost accounting). An iteration row records the agent, model, and — in
continuous mode — the iteration's session id. A step row is booked **per
launch attempt** with the step number and name, status, a binary exit code,
the agent/model/session, the cost (flushed as the stream reports it, so even
a killed step keeps its last figure), and a short reason in its metadata.

Step rows land with these statuses:

.. list-table::
   :header-rows: 1
   :widths: 26 18 56

   * - Launch outcome
     - Status
     - Metadata
   * - completed
     - ``completed``
     - —
   * - completed, but served off its pinned model
     - ``completed``
     - ``model drop (served <model>)``
   * - timed out
     - ``exited``
     - ``timed out``
   * - budget skip (never launched)
     - ``stopped``
     - ``over budget``
   * - paused mid-step
     - ``paused``
     - — (``awaiting approval`` when parked in an approval wait)
   * - failed
     - ``failed``
     - the launch reason, e.g. ``agent error (exit N): <stderr tail>``
   * - retry attempt
     - as above
     - suffixed ``; retry``

When a step fails, the steps after it are still booked as real rows —
opened and immediately closed ``stopped`` with reason ``failed on <step>`` —
so the activity log answers which steps never ran. An abnormally ended step
that has a session but no recorded cost gets ``; unpriced`` appended to its
metadata.

How a run ends
~~~~~~~~~~~~~~

The terminal cascade stamps the node and closes the run according to a fixed
matrix:

.. list-table::
   :header-rows: 1
   :widths: 42 18 18 22

   * - Condition
     - Node status
     - Run status
     - Run exit code
   * - budget abort
     - ``exited``
     - ``exited``
     - 0
   * - setup crash-loop (3 consecutive failures)
     - ``exited``
     - ``exited``
     - 1
   * - finish signal, subtree drained
     - ``completed``
     - ``completed``
     - 0
   * - run timeout
     - ``exited``
     - ``exited``
     - 1
   * - stop signal
     - ``stopped``
     - ``stopped``
     - 0
   * - max iterations, last iteration OK
     - ``completed``
     - ``completed``
     - 0
   * - max iterations, last iteration failed
     - ``exited``
     - ``exited``
     - 1
   * - anything else
     - ``exited``
     - ``exited``
     - 1

A pause is not an end: the loop parks with the run and iteration rows open
for ``fractal node resume`` to adopt, and records nothing terminal. Every
``exited`` end also posts a priority-7 notice to the node's ``outbox`` radio
channel (subject ``run exited: <reason>``), so the death shows up in a
parent's feed — see :doc:`/guide/radio`. Cost-capped nodes append their spend
figures to the notice.

In the pane transcript, agent exit codes follow sentinel conventions: 124 is
a timeout kill, 125 a budget skip (the step never launched), 127 an agent
binary that could not execute, and 128+N a signal death.

Observing progress
------------------

The tmux pane is the live view — the loop prints a banner per iteration and
per step, and streams the agent's output between them:

.. code-block:: text

   === Iteration 3 of 10 at 2026-01-01T00:00:00Z ===

   --- SYNC (before EXECUTE) ---
   --- SYNC (before EXECUTE): done (30s) ---

   --- Step 3/5 (EXECUTE) ---
   ...agent output...
   --- Step 3/5 (EXECUTE): done (600s) ---

   === Iteration 3 of 10 completed (630s) ===

The main observation commands:

.. code-block:: console

   $ fractal node attach parser        # attach to the live tmux pane
   $ fractal node status parser        # current lifecycle status
   $ fractal node activity parser      # run/iteration/step rows, newest first
   $ fractal node cost spent parser    # recorded spend, children included

``fractal node activity`` prints the recorded rows — including every
step attempt, its duration, cost, and metadata reason — with ``--limit``,
``--csv``, and ``--json`` output options. ``fractal node cost remaining``,
``spent``, and ``breakdown`` read the budget ledger (see :doc:`/cli/node`).
``fractal node pending`` lists children's steps awaiting your approval. The
TUI (:doc:`/tui`) presents the same data interactively across the whole tree.

Diagnostics persist in the node directory: ``setup.log`` holds the
last setup output, ``<agent>.err`` the last launch's live stderr capture, and
``tmp/err/<run>-<iter>-<step>.err`` a durable per-failure stderr snapshot for
every failed or timed-out launch (retries add a ``-N`` serial).

Steering a running loop
-----------------------

``config.json`` is read from disk on every access, so it doubles as a live
steering surface. Retunes land at boundaries, not instantly:

- ``max_iters``, ``iter_timeout``, ``step_timeout``, ``wait``, and the cost
  caps are re-read at each iteration top (the budget probes also read the
  caps live); ``iter_timeout`` is read ahead of the deadline reset, so a
  retune bounds the iteration just starting. A malformed edit warns and
  keeps the previous value rather than crashing the run.
- ``interval`` and ``sleep`` are re-read at the between-iteration sleep, so
  a pacing retune takes effect without a restart. A malformed or conflicting
  edit warns and keeps the prior pacing, as does an ``iter_timeout`` longer
  than the interval; these ``iter_timeout`` and pacing rejections warn once
  per distinct value, not once per iteration.
- Step files are re-discovered and re-parsed every iteration, so editing a
  step's body or frontmatter steers the very next pass.
- Boot-pinned for the run: the agent (and its provider, model, and effort),
  ``detached``, ``meta``, ``sync``, the run ``timeout``, and the retry keys
  (``step_retries``/``step_retry_backoff``).

``fractal node update`` is the supported retune path — it writes the config
and the node registry together. A direct config edit still wins (the loop
enforces the file), and the registry is healed to match at the next boundary
with a loud per-key warning.
