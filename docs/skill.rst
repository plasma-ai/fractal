The Fractal Skill
=================

Fractal ships as a plugin skill for Claude Code and Codex alongside the CLI.
The skill turns your interactive agent into fractal's operator: invoked as
``/fractal [directive]``, it interprets a plain-language directive, walks you
through configuring a node, launches the node only on your explicit approval,
and then stays on as operator of the running tree — monitoring, steering, and
relaying between the tree and you. This page covers what installing and
invoking the skill means in practice: what the agent does on your behalf,
where it always asks first, and the one narrow class of commits it makes
without asking.

The plugin skill is distinct from the **node-seed skill**. Every agent node's
data directory carries its own ``skills/fractal`` seed, which documents spawn
mechanics, child management, radio, and the full CLI for the loop running
inside that node. The plugin skill drives your interactive session at the
root of the tree; the seed drives the autonomous loops. For deep doctrine —
for example the full cost and budget semantics — the plugin skill defers to
the node seed rather than restating it. See :doc:`/guide/architecture` for
the tree model and :doc:`/guide/loop` for what a node does once running.

Installing the plugin
---------------------

Plugin marketplace
~~~~~~~~~~~~~~~~~~

Install the skill through the plugin marketplace. In Claude Code:

.. code-block:: text

   /plugin marketplace add plasma-ai/plugins
   /plugin install fractal@plasma

In Codex:

.. code-block:: console

   $ codex plugin marketplace add plasma-ai/plugins
   $ codex plugin add fractal@plasma

From the CLI
~~~~~~~~~~~~

If the ``fractal`` CLI is already installed (see
:doc:`/guide/getting-started`), ``fractal install`` copies the bundled
**fractal** and **wiki** skills into both agents' skill directories —
``.claude/skills`` (Claude Code) and ``.agents/skills`` (Codex) — under your
home directory:

.. code-block:: console

   $ fractal install

Each install replaces any prior copy of the same skill directory. After
upgrading the package, re-run ``fractal install`` to refresh the copied
skills.

.. list-table::
   :header-rows: 1
   :widths: 16 10 74

   * - Option
     - Default
     - Meaning
   * - ``--project``
     - off
     - Install into the current directory's ``.claude/skills`` and
       ``.agents/skills`` instead of your home directory.
   * - ``--link``
     - off
     - Symlink the bundled skills instead of copying, so source edits apply
       without re-installing. Requires the package files on disk (an
       editable install); a zipped install refuses.

Invoking the skill
------------------

The skill never fires implicitly — its metadata disables model invocation for
both Claude Code and Codex — so it runs only when you call it:

.. code-block:: text

   /fractal [directive]

The directive is natural language. The agent distills the node's goal from it
and maps every detail you pinned down — a name, a budget, a model, limits —
onto the matching ``fractal node init`` flag. All configuration routes
through ``fractal node init`` and lands in the node's ``config.json``;
``fractal node start`` just launches, taking only the runtime choice
(``--headless`` / ``--tmux``) and ``--continue`` / ``--clean`` (and
``--max-cost`` or ``--drain`` accompanying a continue) when resuming an
existing node. A directive can be as thin or as thorough as you like:

.. code-block:: text

   /fractal build the parser in myproject with claude, a $10 budget, at most 10 iterations

After reading the directive — and before doing anything else — the agent
prints, in this order:

1. **Suggested instructions** — a draft ``## Instructions`` section for the
   node's ``NODE.md``, distilled from the directive (skipped when no goal can
   be inferred).
2. **Suggested completion requirement** — a draft
   ``## Completion Requirements`` section with concrete, verifiable
   conditions (for open-ended work with no natural stopping point, it instead
   notes that the section stays empty and ``max-iters`` should cap the run).
3. **Interpreted parameters** — a table with one row per parameter below and
   the value read from the directive, left empty where the directive said
   nothing.

It then asks about anything it could not infer — name and path first, then
goal and completion requirements. Printing the full table last lets you
catch a misinterpreted parameter before anything is created. Even a thorough
directive draws follow-up questions that refine the seed — tighter
completion conditions, scope, caps — the agent never launches on the
directive alone.

Directive parameters
~~~~~~~~~~~~~~~~~~~~

Every parameter becomes the matching ``fractal node init`` flag and is
written to the node's ``config.json`` (as the snake_case equivalent), where
it stays editable before launch. See :doc:`/configuration` for the full key
semantics and :doc:`/cli/node` for the flag surface.

.. list-table::
   :header-rows: 1
   :widths: 18 20 62

   * - Parameter
     - Default
     - Meaning
   * - ``name``
     - required
     - Node name; letters, digits, and ``_`` only — no ``-``.
   * - ``path``
     - ``.``
     - Project root: the repo root or a monorepo sub-project.
   * - ``title``
     - de-slugged name
     - Human-readable display name.
   * - ``scope``
     - unset
     - Restrict commits to subdirectories within the worktree
       (comma-separated, e.g. ``parent/child,tests``).
   * - ``base``
     - current branch
     - Branch the node starts from.
   * - ``meta``
     - unset
     - Target node branch for meta-configuration.
   * - ``inherit``
     - unset
     - Seed surfaces from the parent node instead of the package seed
       (comma-separated: ``steps``, ``scripts``, ``skills``, ``config``, or
       ``all``); agent config always inherits.
   * - ``agent``
     - user node's default
     - Agent command; see :doc:`/guide/agents` for the supported backends.
   * - ``provider``
     - user node's default
     - Provider route for the agent (e.g. ``openrouter``).
   * - ``model``
     - agent's own default
     - Model override.
   * - ``effort``
     - agent seed's pinned level
     - Reasoning-effort override.
   * - ``max-iters``
     - unset (unlimited)
     - Per-run iteration cap.
   * - ``max-depth``
     - unset
     - Maximum child node nesting depth.
   * - ``max-children``
     - unset
     - Maximum direct child nodes.
   * - ``max-descendants``
     - unset
     - Maximum total descendant nodes.
   * - ``timeout``
     - unset
     - Per-run time limit (e.g. ``30s``, ``10m``, ``1h``).
   * - ``iter-timeout``
     - unset
     - Per-iteration time limit.
   * - ``step-timeout``
     - unset
     - Per-step time limit; caps each step.
   * - ``interval``
     - unset
     - Fixed iteration schedule (e.g. ``1h``).
   * - ``sleep``
     - unset
     - Delay between iterations (e.g. ``10s``).
   * - ``wait``
     - ``1m``
     - Sleep between sync passes while waiting — on a child drain or an
       approval gate.
   * - ``max-cost``
     - unset (uncapped)
     - Cost ceiling in USD per run; runs are isolated, so each launch arms
       the cap anew.
   * - ``max-iter-cost``
     - unset
     - Per-iteration cost ceiling in USD.
   * - ``max-step-cost``
     - unset
     - Per-step cost ceiling in USD (warn-only when unenforceable).
   * - ``reserve-budget``
     - 10% of ``max-cost``
     - Budget reserved for cleanup; USD or a percentage of ``max-cost``.
   * - ``sync``
     - on
     - Radio sync before each step (see :doc:`/guide/radio`).
   * - ``detached``
     - off
     - Run each step as a separate agent session (default: one continuous
       session).
   * - ``local``
     - off
     - Skip pushing to remote after each commit.

To change a setting after init, the agent edits ``<node_dir>/config.json``
directly (the node reads it at launch) or runs
``fractal node config set <key>=<value>``.

What the skill does end to end
------------------------------

Install the CLI
~~~~~~~~~~~~~~~

If ``fractal`` is not on the agent's ``PATH``, the skill installs it first.
Fractal shells out to the ``wiki`` command, so both packages are installed:

.. code-block:: console

   $ pipx install plasma-fractal
   $ pipx install plasma-wiki

(``uv tool install plasma-fractal --with-executables-from plasma-wiki`` does
the same in one command.)

Before installing, the skill checks what is already there by *running* it
(``fractal --version && wiki --version``) — never by name resolution alone,
since a shim for a non-activated environment can resolve on ``PATH`` yet
fail at exec — and pins a working install's absolute paths for the session
when it lives off ``PATH``.

Initialize
~~~~~~~~~~

The agent determines the node's state and proceeds accordingly:

- **The directive asks to continue** an existing node — it resolves the
  worktree and node directory from ``fractal node list`` and skips setup
  entirely.
- **A node already exists** for this path and name without continue intent —
  the agent asks you what to do: **Continue** (keep state), **Reset**
  (recreate with ``--reset`` — a stock empty node, with memory, plans,
  steps, skills, and config all wiped, so the definition is re-authored from
  scratch), or **Cancel**.
- **Otherwise it creates the node**, running three idempotent commands:

  .. code-block:: console

     $ fractal init . --agent=claude
     $ fractal commit "configure main" --init
     $ fractal node init parser --max-iters=10 --max-cost=10.0

  ``fractal init`` writes the root node data (``.fractal/``) and the project
  wiki (``wiki/``); when you named no agent, the skill defaults to the
  invoking agent's own kind. ``fractal commit --init`` commits the wiki
  scaffold on your base branch first, because a node branches from a
  *committed* tree — an uncommitted wiki is invisible to
  ``fractal node init``. Finally ``fractal node init`` creates the worktree
  and node directory with the parameters interpreted from your directive.

Along the way the skill teaches the repository facts that matter: the project
``wiki/`` is git-tracked and never belongs in ``.gitignore``; the root's
``.fractal/<branch>/`` is git-ignored on the top-level branch by its own
self-ignore ``.gitignore`` (toggled by ``fractal track`` /
``fractal untrack``, which never touch the index), while fractal's runtime
artifacts ride the repo-local ``.git/info/exclude``; and the wiki merge
driver lives in repo-local git config, which does not survive a clone — run
``wiki config --path=wiki`` after cloning to register it again
(``fractal init`` re-registers it only when it creates the wiki).

Define the node
~~~~~~~~~~~~~~~

The agent then holds a guided conversation, one topic at a time, waiting for
your answer before moving on:

a. **Goals and instructions** — refined from the printed draft and written
   into the ``## Instructions`` section of ``<node_dir>/NODE.md``. The skill
   pushes for specific, verifiable goals: a well-configured node runs
   autonomously for hours, a vague one burns budget.
b. **Completion requirements** — concrete, verifiable done-conditions,
   written into ``## Completion Requirements``; for open-ended work it
   suggests leaving the section empty and capping with ``max-iters``.
c. **Rules and constraints** — anything beyond the defaults (files to avoid,
   patterns to follow), appended to ``## Rules``.
d. **Budget and scope** — the agent recommends ``max-cost`` and
   ``max-iter-cost``, and asks about ``scope``. An uncapped run requires an
   explicit are-you-sure yes; it never defaults into uncapped. It also warns
   about pairing a low ``max-cost`` with an expensive model: the run ceiling
   is checked between steps, not within them, so never set a cap within
   roughly twice the model's single-turn cost — that is the sizing floor.
e. **Iteration steps** — the default pipeline is prepare, plan, execute,
   review, and commit, editable under ``<node_dir>/steps/``. Sync runs before
   each numbered step and is itself a billed step, so an iteration with N
   step files runs roughly 2N agent invocations — a budget sized by counting
   ``steps/`` undercounts. Steps can carry per-step overrides (agent,
   provider, model, effort, timeout, detached, approval gates); see
   :doc:`/guide/loop`.
f. **Environment setup** — anything the project needs each iteration goes in
   ``<node_dir>/scripts/setup.sh``, which runs at the start of every
   iteration and must be idempotent.
g. **Validation and testing** — ``<node_dir>/scripts/lint.sh`` runs before
   each commit; ``<node_dir>/scripts/test.sh`` is called by the agent during
   execution.
h. **Review** — the agent prints the final ``NODE.md`` and iterates until you
   are satisfied.

Launch
~~~~~~

The agent prints the exact commands it is about to run and asks you to
choose: **Launch**, **Revise**, or **Cancel**. It proceeds only on an
explicit Launch. Once approved, it commits the configured seed and starts the
node from its worktree:

.. code-block:: console

   $ cd .worktrees/main.parser
   $ fractal commit "configure parser" --init
   $ fractal node start

All run parameters were set at init; ``start`` takes no configuration
arguments — only the runtime choice (``--headless``/``--tmux``) and the
continue flags. The node launches in a detached tmux session by default and
runs autonomously from there; when tmux is unavailable, ``start --headless``
runs it in a detached process group instead — child starts follow the
parent's backend and append their output to ``headless.log``. Without a
flag, a relaunch (``--continue`` or ``resume``) reuses the backend the node
last launched with; ``--headless``/``--tmux`` re-record it.

Post-launch briefing
~~~~~~~~~~~~~~~~~~~~

After launch, the agent briefs you on operating the node — each of these has
a dedicated page:

- **Steering**: edit ``<node_dir>/NODE.md`` directly (the node re-reads it at
  every step); retune caps with ``fractal node update``.
- **Monitoring**: ``fractal node status``, ``fractal node cost spent``,
  ``fractal node attach`` (a headless node is followed with
  ``tail -f <node_dir>/headless.log`` instead), ``fractal node list``, and
  ``fractal node activity`` (see :doc:`/cli/node`), or the live dashboard via
  ``fractal open`` (see :doc:`/tui`).
- **Stopping**: three escalation levels — ``fractal node finish`` (after the
  current iteration), ``fractal node stop`` (after the current step),
  ``fractal node kill`` (immediately).
- **Pausing**: ``fractal node pause`` / ``resume`` for a subtree,
  ``fractal pause`` / ``resume`` tree-wide; paused state is durable. See
  :doc:`/guide/lifecycle`.
- **Merging and cleanup**: ``fractal node merge`` when done; ``delete`` is
  recursive and force-deletes branches regardless of merge state, so it is
  optional hygiene, never automatic.
- **Radio**: how nodes communicate and how to read and send messages; see
  :doc:`/guide/radio` and :doc:`/cli/radio`.

The operator role
~~~~~~~~~~~~~~~~~

The skill's job does not end at launch. The user (root) node has no loop of
its own — the invoking agent *is* it. As operator it monitors proactively
(``fractal node list`` / ``status`` / ``activity`` / ``cost``), polls the
root's radio (``fractal radio read --channel=inbox --unread`` and
``fractal radio read --feed --unread``), sends directives to children's
inboxes, steers by editing ``NODE.md`` files, approves gated steps, merges
finished subtrees, and relays both ways — surfacing progress, blockers, and
cost to you while translating your intent into edits, directives, and spawns.
It acts autonomously and reports what it did, pausing only for genuinely
ambiguous or irreversible calls.

For launches that deserve review before burning budget, the skill describes
an optional **commissioning** gate: init the child but do not start it,
commit and pin the configured seed's commit sha, radio the pin and a review
checklist to a designated reviewer, and start only on their countersigned
reply. The gate is social — nothing in the tool enforces it — but it catches
seed mistakes while they are still free to fix.

Autonomous commits
------------------

The skill commits fractal's own setup artifacts without asking:
the ``fractal init`` output (``.fractal/`` and the wiki scaffold it creates),
the node seed, and the baseline and child configuration commits the workflow
prescribes (the ``fractal commit ... --init`` calls above). These commits are
what make a node launchable — the node must branch from a committed tree —
and they touch only fractal's own files.

The exception is deliberately narrow. Ordinary project work — code in your
main worktree, anything outside fractal's setup, seed, and configuration
artifacts — still follows whatever commit rules your agent normally operates
under. If you are deciding whether to let an agent run this skill, this is
the full extent of what it commits without a prompt.

Trust and permissions
---------------------

Nodes run their agents with elevated permissions by design, so they can work
unattended: every backend launches in its provider's fully-autonomous,
auto-approving mode (Claude's ``bypassPermissions``, Codex's
``danger-full-access``, and each other backend's equivalent — see
:doc:`/guide/agents`). Only launch nodes whose task you trust to run
autonomously.

The skill's own guardrails frame that decision: nothing launches without an
explicit **Launch** answer, an uncapped run requires an are-you-sure
confirmation, every interpreted parameter is printed for review
before the node is created, and the commissioning gate adds a countersign
step when a launch warrants a second reviewer. Cost ceilings are checked
between steps, not within them — a step in flight can overshoot — so budgets
are a brake, not a wall; see :doc:`/configuration` for how the caps
interact.
