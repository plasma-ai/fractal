Architecture
============

A fractal is a tree of autonomous coding-agent nodes rooted in one git
repository. Each node is four things at once: a git branch, a checked-out
worktree of that branch, an iteration loop running in a tmux session, and a
data directory holding the node's task contract and configuration. Everything
the tree does — runs, iterations, steps, costs, lifecycle events, radio
messages — lands in one central SQLite database. This page gives you the
mental model; the surrounding pages cover each part in depth.

The node tree
-------------

The tree is rooted in the **user node**: the branch that was checked out when
you ran ``fractal init`` (say, ``main``). The user node has no loop and no
tmux session — it is the operator's seat. You (or the interactive agent
driving the :doc:`fractal skill </skill>`) act as it: spawning nodes,
steering them over the radio, merging their work. It carries only its
configuration, the central database, and its radio mailbox.

Every other node is an **agent node**, created with ``fractal node init`` and
named by dotted lineage: a node ``parser`` created from ``main`` lives on the
branch ``main.parser``, and its child ``lexer`` on ``main.parser.lexer``. The
dotted branch is the node's identity everywhere — CLI arguments, database
rows, radio addresses. Node names use letters, digits, and underscores only;
the dots are the hierarchy.

Nodes spawn children for separable subtasks, so the tree grows to fit the
problem rather than a fixed plan. Per-node caps — ``max_depth``,
``max_children``, ``max_descendants``, plus cost and time budgets — bound the
growth. All are set at spawn; the depth and width caps, the cost caps, and
``step_timeout`` are retuned later through ``fractal node update``, while the
run and iteration timeouts are changed with ``fractal node config set`` (the
run ``timeout`` is pinned at loop boot, so a change reaches the next start or
resume, not a run in flight). See :doc:`/configuration` for the full key
surface and :doc:`/guide/lifecycle` for how nodes start, settle, and are torn
down.

Branches and worktrees
----------------------

Each node's branch forks from its parent's tip (or an explicit base branch)
and is checked out into its own worktree at ``.worktrees/<branch>`` inside
your repository. The node works and commits only there: parallel nodes never
touch your checkout or each other's. Commits made in a node's worktree are
attributed to the node's branch name via a per-worktree git identity, so
``git log`` reads who did what.

Full history accumulates on the node's branch. ``fractal node merge``
squash-merges it into the merge target — the configured base branch if one
was set, otherwise the parent — landing one commit there while the detailed
history stays on the node's branch.

The central database
--------------------

One SQLite database serves the whole tree: the file ``.db`` inside the root
node's data directory (``.fractal/main/.db`` for a tree rooted on ``main``).
Every node carries the tree root in its configuration and resolves the
database through it, so any command run from any worktree reads and writes
the same file.

It records:

- the node registry — every agent node, its title, status, and caps;
- one row per run, per iteration, and per step, with cost and timing;
- lifecycle events (init, start, finish, kill, merge, delete, ...) and pending
  signals;
- every radio message, subscription, and read receipt (see
  :doc:`/guide/radio`).

History outlives nodes: ``fractal node delete`` and ``fractal reset`` remove
nodes but keep every run, step, event, and message row. Only
``fractal destroy`` — the full inverse of ``fractal init`` — removes the
database, along with the rest of the root node's data directory.
``fractal node list``, ``fractal node activity``, the radio listings, and the
:doc:`TUI </tui>` are all read surfaces over this one file.

tmux sessions
-------------

``fractal node start`` launches the node's iteration loop in a detached tmux
session — this is what makes nodes autonomous and observable. The session is
named ``<repo> (<branch>)`` with dots flattened to dashes: in a repository
``myproject``, the node ``main.parser`` runs in the session
``myproject (main-parser)``. tmux must be installed for the default backend;
``--headless`` runs the loop in a detached process group instead.

A node has at most one session. The user node never has one, and a paused
node has none either — no session is a parked node's normal state, not a
crash. Watch a running node with ``fractal node attach`` (or ``ctrl+a`` in
the TUI); what runs inside the session — the loop, its steps, and the agent
backends it drives — is covered in :doc:`/guide/loop` and
:doc:`/guide/agents`.

What lives on disk
------------------

For a repository ``myproject`` rooted on ``main`` with one node ``parser``:

.. code-block:: text

   myproject/                        your repository
     .worktrees/                     fractal-owned, git-ignored
       main.parser/                  one worktree per node
         .fractal/main.parser/       the node's data directory
         ...                         your project files, on branch main.parser
     .fractal/main/                  user (root) node data directory
       config.json                   tree defaults
       .db                           the central database
     wiki/                           project wiki, committed
     .gitattributes                  wiki merge attribute, committed
     ...                             your project files

In a monorepo, ``fractal init`` (and ``fractal node init --path``) can target
a sub-project instead of the repo root; the ``.fractal/`` directories then
nest under that sub-path inside each worktree. The repo-root layout shown
above is the common case.

Fractal's footprint
~~~~~~~~~~~~~~~~~~~

``.worktrees/``
   The node worktrees, managed entirely by fractal and removed with their
   nodes. Never create files here yourself; enter a worktree only to inspect
   or steer a node.

``.fractal/<branch>/``
   A node's data directory, living inside its worktree so it travels with the
   branch. It holds the node's ``NODE.md`` task contract, its ``config.json``
   (:doc:`/configuration`), the step prompts under ``steps/``
   (:doc:`/guide/loop`), ``scripts/`` for setup and validation, ``skills/``,
   ``plans/`` (:doc:`/guide/plans`), a ``memory/`` wiki, a ``_template.toml``
   record of the template that seeded the node when ``node init --template``
   did (:doc:`/cli/node`), a git-ignored ``tmp/`` scratch directory, and
   runtime markers such as the one-line ``.status`` file. Agent-node data
   directories are tracked in git on the node's own branch but never merge
   upward: ``fractal node merge`` returns every ``.fractal/`` path on the
   target to the target's own HEAD (only a scope root of the merging node
   that is, or lies under, a ``.fractal/`` directory lands — a ``--meta``
   node's scope is the target's seed directory), so the squash never adds the
   node's ``.fractal/<branch>/`` or any descendant seed the branch carries,
   node machinery never lands in the parent, and a node's edit to the
   parent's estate never rides its squash — the parent's version is restored,
   the merge-base advance then brings the parent's tree into the node's
   worktree, and the node's copy survives only in its branch history. On the
   user node's branch the merge also strips a copy of those seeds the branch
   already tracks (a leak there); a node target keeps a copy its PREPARE
   merge of the child tracks until it merges upward itself. The root node's
   own data directory is git-ignored by default (``fractal track`` and
   ``fractal untrack`` toggle this).

``.gitattributes``
   ``fractal init`` writes the ``**/_index.md merge=wiki`` rule (under a
   one-line comment) into the repo-root ``.gitattributes``, creating the file
   when absent and appending to it otherwise (one with uncommitted edits is
   left alone), so generated wiki indexes merge through the wiki merge
   driver. It is ordinary committed content: the ``fractal commit --init``
   baseline picks it up while it is still init's own uncommitted edit. The
   driver itself is registered in repo-local git config
   (``merge.wiki.driver``), which a clone does not carry — run
   ``wiki config --path=wiki`` after cloning to register it again.

``.git/info/exclude``
   Fractal keeps its runtime artifacts — worktrees, the database, status
   files, agent logs — out of git through a marker-delimited block in the
   repo-local exclude file. Your committed ``.gitignore`` is never touched.

What stays yours
~~~~~~~~~~~~~~~~

Your project files and your branches remain yours: agent nodes commit only
inside their own worktrees, on node branches. Fractal commits on your own
branch only when you run a command that says so — ``fractal init`` outside a
repository bootstraps one with an initial ``.gitignore`` commit,
``fractal commit --init`` lands the baseline (the wiki, its
``.gitattributes`` merge attribute, and the root node's data when tracked),
and ``fractal node merge`` squash-merges a node and commits in the target's
worktree. The bootstrap and baseline commit by pathspec, so other staged work
stays staged; the merge refuses while the target worktree has uncommitted
tracked changes or when the squash would write over any file that exists
untracked there (an ignored ``local.env``, your own live seed), and restores
it if the squash conflicts. The project wiki at ``wiki/`` and the
``.gitattributes`` merge rule are created by ``fractal init`` but committed
as ordinary project content for you and the nodes to grow. Teardown respects
the same line — ``fractal node delete`` removes a subtree, ``fractal reset``
removes every node while keeping the database, and even ``fractal destroy``
leaves committed artifacts (the wiki and baseline commits) and remote
branches in place. See :doc:`/guide/lifecycle` for the teardown tiers.

Package names
-------------

The package is published on PyPI as ``plasma-fractal``; the ``fractal`` name
is a metadata-only pointer to it, so ``pip install fractal`` installs the
same thing — see :doc:`/guide/getting-started` for details.
