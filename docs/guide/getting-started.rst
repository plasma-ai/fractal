Getting Started
===============

Fractal runs trees of autonomous coding-agent nodes. Each node is an isolated
git worktree driven by an iteration loop in a tmux session, with lifecycle,
budgets, and inter-node messaging all recorded in one central SQLite database
per tree. This page walks a first session end to end: install the package,
initialize a repository, create and start one node, watch it work, and merge
the result.

Installation
------------

Package
~~~~~~~

Install from PyPI:

.. code-block:: console

   $ pip install plasma-fractal

``plasma-fractal`` is the real package. The ``fractal`` project on PyPI is a
metadata-only pointer distribution containing no code — just an exact
``plasma-fractal==<version>`` pin that bumps in lockstep with every release —
so ``pip install fractal`` installs the same thing. The pointer begins at
version 1.0.0; earlier releases under the ``fractal`` name on PyPI are an
unrelated package.

A plain ``pip`` install pulls the ``plasma-wiki`` dependency and puts its
``wiki`` executable on your ``PATH``; fractal shells out to it to maintain
the project wiki during commits. Isolated installers do not expose the
``wiki`` executable automatically — with ``pipx``, install ``plasma-wiki``
separately; with ``uv``, one command covers both:

.. code-block:: console

   $ uv tool install plasma-fractal --with-executables-from plasma-wiki

Agent skills
~~~~~~~~~~~~

The ``/fractal`` skill drives node creation conversationally from inside
Claude Code or Codex (see :doc:`/skill`). Install it from the plugin
marketplace (``/plugin marketplace add plasma-ai/plugins`` then
``/plugin install fractal@plasma`` in Claude Code; the ``codex plugin``
equivalents in Codex), or from the CLI:

.. code-block:: console

   $ fractal install
   Installed fractal -> /home/user/.claude/skills/fractal.
   Installed fractal -> /home/user/.agents/skills/fractal.
   Installed wiki -> /home/user/.claude/skills/wiki.
   Installed wiki -> /home/user/.agents/skills/wiki.

``fractal install`` copies the bundled ``fractal`` and ``wiki`` skills into
the Claude Code (``.claude/skills``) and Codex (``.agents/skills``) skill
directories under your home directory, replacing any prior copy of the same
skill. The ``wiki`` skill ships with fractal's ``plasma-wiki`` dependency and
installs alongside. These flags modify the behavior:

``--project`` (default: off)
   Install into the current directory instead of the home directory.

``--link`` (default: off)
   Symlink the skills instead of copying them. This requires the package
   files on disk (e.g. an editable install); a zipped install refuses.
   Source edits then apply without re-installing.

After upgrading the package, re-run ``fractal install`` to refresh copied
skills.

Prerequisites
-------------

- **Python 3.12+ with SQLite 3.35 or newer** — the central database relies
  on SQLite features (upsert ``RETURNING``, close-time checkpoint control)
  present in every Python 3.12+ standard build; a custom interpreter linked
  against an older SQLite is not supported.
- **git** — fractal anchors to a git repository; ``fractal init`` bootstraps
  one when run outside a repo (``git init`` on a project-named branch plus an
  initial commit of an empty ``.gitignore``), and refuses a detached
  ``HEAD``.
- **tmux (optional)** — tmux is the default and provides attachable panes;
  ``fractal node start --headless`` uses an independent process group instead
  when tmux cannot run. tmux 3.2 or newer is
  needed to forward provider keys into an already-running tmux server; on
  older versions the loop's preflight reports the missing key instead.
- **A provider CLI** — the agent command a node is configured with must be on
  ``PATH``: ``claude`` (Claude Code), ``codex`` (Codex), ``grok`` (Grok
  Build), ``opencode`` (OpenCode), or ``omp`` (Oh My Pi). See
  :doc:`/guide/agents`.
- **The ``wiki`` CLI** — from ``plasma-wiki``; ``fractal init`` shells out to
  it to scaffold the project wiki (and refuses when it cannot find it),
  ``fractal node init`` uses it to seed each node's memory wiki, and the
  commit pipeline runs ``wiki update`` and ``wiki lint``; fractal looks for
  it beside its own interpreter first, so any install of ``plasma-fractal``
  provides it (see above for putting it on your ``PATH`` with ``pipx`` and
  ``uv tool`` installs).
- **``OPENROUTER_API_KEY``** — only when routing an agent through
  ``--provider=openrouter``; the key is read from the launching shell and
  captured into the node's tmux session, so rotating it requires a node
  restart.

Initializing a repository
-------------------------

Run ``fractal init`` once at the repository root — or, in a monorepo, point
it at a sub-project folder:

.. code-block:: console

   $ cd myproject
   $ fractal init --agent=claude
   Initialized user node on branch main
   Created .fractal/main/ (config, database, radio) and the project wiki at wiki/
   Next: commit the baseline: fractal commit "<message>" --init

``fractal init`` takes an optional path argument (default ``.``) and these
options:

``--agent`` (default: unset)
   The default agent command spawned nodes inherit: ``claude``, ``codex``,
   ``grok``, ``opencode``, or ``omp``. An unknown name refuses at init.

``--provider`` (default: the vendor-native endpoint)
   The default provider route spawned nodes inherit (e.g. ``openrouter``).

It creates the **user node** — the passive root of the tree, anchored to the
branch you are on. Concretely, it writes:

- ``.fractal/<branch>/`` — the user node's data directory, holding
  ``config.json`` and the central SQLite database (``.db``) that records the
  whole tree;
- ``wiki/`` — the project wiki scaffold, if one does not already exist (a
  committed knowledge base shared by all nodes);
- a repo-local git exclude block (in ``.git/info/exclude``), so fractal's
  runtime artifacts stay out of your commits by default (``fractal track``
  opts the seed directory back in).

The user node never runs a loop — you (or your agent, via the skill) act as
its operator. Re-running ``fractal init`` is idempotent: it updates the agent
and provider defaults and repairs missing pieces without clobbering anything.

Finish setup by committing the baseline. A node worktree branches from the
last commit on its base branch, so the wiki scaffold must be committed before
the first node is created:

.. code-block:: console

   $ fractal commit "add project wiki" --init

``--init`` labels this a baseline commit — the only kind of commit a user
node accepts. See :doc:`/guide/architecture` for how the tree, worktrees, and
database fit together.

Creating a first node
---------------------

``fractal node init <name>`` creates an agent node: a git worktree checked
out on branch ``<parent>.<name>`` under ``.worktrees/``, plus a node data
directory seeded with its task contract (``NODE.md``), iteration steps,
scripts, and skills. Node names are single branch segments — letters, digits,
and underscores.

Always cap a new node: budgets default to unlimited, and one iteration is
several agent invocations. The command warns on stderr when neither
``--max-cost`` nor ``--max-iters`` is given (unless the agent's spend is
untracked — e.g. ``codex`` without a priced model).

.. code-block:: console

   $ fractal node init parser --title="Build the parser" --max-iters=10 --max-cost=10.0
   Initialized /home/user/myproject/.worktrees/main.parser

   Next: author the node's task in /home/user/myproject/.worktrees/main.parser/.fractal/main.parser/NODE.md
   (its Instructions and Completion Requirements sections start blank),
   then start the loop: fractal node start main.parser

``--max-iters`` caps the iterations per run, and ``--max-cost`` sets the
run's cost ceiling in USD (shared with any descendants the node spawns).
``fractal node init`` accepts many more options — agent, provider, model, and
effort overrides, time budgets, commit scope, pacing, child caps — all of
which land in the node's ``config.json`` and can be edited before launch. The
full surface is covered in :doc:`/configuration` and :doc:`/cli/node`.

Now author the node's task in its ``NODE.md``. Two sections start as blank
placeholders:

- ``## Instructions`` — the node's goals and directions.
- ``## Completion Requirements`` — the conditions under which the node is
  done. The seed instructs the node to run ``fractal node finish`` in the
  iteration that meets them; a node whose requirements are left empty never
  self-completes, so cap open-ended work with ``--max-iters``.

.. code-block:: markdown

   ## Instructions

   Implement the parser module described in <spec-file>, with unit tests
   covering the happy path and error recovery.

   ## Completion Requirements

   The test suite passes and the module is committed. Then run
   `fractal node finish --reason="parser complete"`.

Starting the node
-----------------

.. code-block:: console

   $ fractal node start parser
   Started tmux session: myproject (main-parser)

The loop launches in a detached tmux session named ``<repo> (<branch>)``
with dots flattened to dashes. Anywhere a
command takes a node branch, a unique trailing segment works — ``parser``
resolves to ``main.parser``. Run parameters come from ``config.json``; the
only launch-time flags are for re-arming a settled node (``--continue``, with
``--clean`` to discard uncommitted project files, ``--max-cost`` to retune
the budget, and ``--drain`` to run a wind-down that forbids further spawns
and re-arms) — see :doc:`/guide/lifecycle`, and :doc:`/cli/node` for the
flag details.

On a locked-down or non-interactive host, pass ``--headless``. The command
still returns immediately, the loop runs in an independent process group, and
its output appends to the node's ``headless.log``, one launch banner per
launch. The backend is sticky — an unflagged relaunch reuses it — and
delegated child starts follow the parent's backend, so the entire tree runs
without tmux; ``--tmux`` forces and re-records a tmux launch.

.. warning::

   Nodes run their agents with elevated permissions by design (e.g. Claude
   Code's ``bypassPermissions``, Codex's ``danger-full-access``). Only launch
   tasks you trust with your repository and machine.

Watching it run
---------------

From cheapest to richest:

- ``fractal node status parser`` prints the node's lifecycle status —
  ``active`` while the loop runs, decorated with any pending signal (e.g.
  ``active (finishing)``) and, when a run lands ``exited``, with the recorded
  reason (``exited (<reason>)``).
- ``fractal node activity parser`` prints the lifecycle timeline — runs,
  iterations, steps, and events with durations and per-step costs, most
  recent first.
- ``fractal node attach parser`` attaches your terminal to the node's live
  tmux session (the node must be ``active``); detach with the standard tmux
  detach.
- For a headless node, ``attach`` refuses and names
  ``<node_dir>/headless.log``; follow that file instead.
- ``fractal open`` launches the TUI cockpit — the whole tree, radio traffic,
  budgets, and event logs on one live screen; see :doc:`/tui`.

From the repo root, ``fractal node list`` tables every agent node under the
root with its status, caps, and the age of its last activity, and
``fractal node cost spent parser`` reports the run's recorded spend. Nodes
also report progress over the radio — see :doc:`/guide/radio`.

Finishing and merging
---------------------

A well-authored node ends itself: when its Completion Requirements are met,
it runs ``fractal node finish`` from inside its loop and settles as
``completed``. You can also end it from outside:

.. code-block:: console

   $ fractal node finish parser --reason="good enough"

``finish`` ends the node gracefully after the current iteration;
``fractal node stop`` ends it after the current step; ``fractal node kill``
reaps it immediately. The signal tiers, pausing, and continuation are covered
in :doc:`/guide/lifecycle`.

Once ``fractal node status parser`` reads ``completed``, merge the work:

.. code-block:: console

   $ fractal node merge parser

``merge`` squash-merges the node's branch into its merge target — its
configured ``base``, or by default the parent branch (here ``main``). One
squash commit lands on the target; the node's full iteration history stays on
its own branch. Merging refuses while the node — or the target — is active or
paused.

From here the node can be re-armed for further iterations with
``fractal node start parser --continue``, or removed with
``fractal node delete parser`` — the worktree and branches go, while the
node's history in the central database persists.

Where next
----------

The rest of the guide goes deeper on each piece: :doc:`/guide/architecture`
explains how the tree, worktrees, branches, and central database fit
together; :doc:`/guide/lifecycle` covers statuses, signals, pause and resume,
continuation, and teardown; :doc:`/guide/loop` walks what happens inside an
iteration — steps, sync, and the commit pipeline; :doc:`/guide/agents`
describes the agent backends and provider routing; :doc:`/guide/radio`
covers inter-node messaging; and :doc:`/guide/plans` covers plan files.
Beyond the guide, :doc:`/configuration` documents every configuration key,
:doc:`/cli/index` is the full command reference, :doc:`/skill` documents the
``/fractal`` plugin skill, :doc:`/recipes` collects common operator tasks,
and :doc:`/examples` includes a scripted version of this walkthrough that
builds a ready-to-start node in a scratch repository.
