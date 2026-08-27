Top-Level Commands
==================

The commands registered directly on the ``fractal`` executable operate on
the tree as a whole: they install the plugin skills, initialize and tear
down a fractal, commit work, and brake or release every loop at once. Node-
and message-level commands live in the sub-apps (:doc:`/cli/node`,
:doc:`/cli/radio`, :doc:`/cli/plan`).

``fractal --version`` prints the package version and exits.

``install``
-----------

.. code-block:: console

   $ fractal install [--project] [--link]

Install the ``fractal`` and ``wiki`` skills for Claude Code and Codex.
The bundled skills are copied into the Claude Code (``.claude/skills``) and
Codex (``.agents/skills``) skill directories under the home directory by
default. The ``wiki`` skill ships with fractal's ``plasma-wiki`` dependency
and is installed alongside. Any prior install at a destination is replaced;
each placement echoes ``Installed <skill> -> <dest>.`` (or
``Linked <skill> -> <dest>.`` with ``--link``).

``--project``
   Install into the current working directory instead of the home
   directory. Default: off (the home directory).

``--link``
   Symlink the bundled skills instead of copying, so source edits apply
   without re-installing. Requires the package files on disk (an editable
   install); a zipped install refuses because the bundled skills are not
   real directories. Default: off (copy).

.. code-block:: console

   $ fractal install
   Installed fractal -> /home/user/.claude/skills/fractal.
   Installed fractal -> /home/user/.agents/skills/fractal.
   ...

``init``
--------

.. code-block:: console

   $ fractal init [PATH] [--agent <command>] [--provider <route>]

Initialize fractal for a repository (or a monorepo sub-project), creating
the **user (root) node** on the currently checked-out branch. The user node
anchors the tree: it carries configuration, the central SQLite database,
and the radio, but runs no loop of its own (see
:doc:`/guide/architecture`). Init creates:

- the data directory ``<project>/.fractal/<branch>/`` with ``config.json``
  (marked ``user: true``), the central database, and the radio's default
  channels;
- the project wiki at ``<project>/wiki/`` when absent;
- fractal's block in the repo-local ``.git/info/exclude`` (worktrees,
  databases, status markers, agent logs), and a ``.gitignore`` of ``*``
  inside the seed directory itself, so ``.fractal/<branch>/`` is
  git-ignored by default (``fractal track`` removes that file to opt in).

The command ends by printing the required next step: the baseline commit
(``fractal commit "<message>" --init``). Node worktrees can only branch
from a committed tree.

Re-running on an initialized tree is idempotent — it never clobbers
existing data. A re-run repairs a partial prior init (a stranded database,
radio, or missing wiki) and updates the stored agent and provider defaults
when the flags are given.

``PATH``
   Repository root or monorepo sub-project folder. Default: ``.``.

``--agent``
   Default agent command that spawned nodes inherit (e.g. ``claude``). The
   name is validated against the agent registry at init, so a typo refuses
   immediately. Default: unset —
   an agent must then be named somewhere in each node's ancestor chain at
   spawn time. See :doc:`/guide/agents`.

``--provider``
   Default provider route for spawned nodes (e.g. ``openrouter``).
   Default: the vendor-native endpoint.

Refuses when:

- HEAD is detached (check out a branch first);
- the branch name contains ``/`` (per-branch artifacts key on the branch as
  a single path component);
- the branch dot-nests with an existing tree's root (``v1.0`` beside a tree
  rooted at ``v1``, in either order) — ``.`` is the node hierarchy
  separator, so one would read as a node inside the other. A dotted root
  alone is fine; switch to a branch that is not dot-nested with an existing
  root;
- ``PATH`` lies under ``.worktrees/`` (run from the repo root or a
  sub-project folder);
- ``PATH`` lies in a linked git worktree outside the main repository root
  (run ``fractal init`` from the main checkout);
- the branch is already mapped to a different project — one branch maps to
  a single project;
- the command runs from a draining seat (an agent invocation of a
  ``fractal node start --continue --drain`` run, or any process in its
  process group — see ``--drain`` in :doc:`/cli/node`): a drain forbids
  every spawn, a whole new tree included;
- another live fractal on the machine shares this repository's directory
  basename: tmux session names carry the basename, so two fractals under
  one basename would collide. Rename one repository directory.

.. code-block:: console

   $ cd myproject
   $ fractal init --agent claude
   Initialized user node on branch main
   Created .fractal/main/ (config, database, radio) and the project wiki at wiki/
   Next: commit the baseline: fractal commit "<message>" --init

``track``
---------

.. code-block:: console

   $ fractal track [NAME] [--path <dir>]

Track the user node's ``.fractal/<branch>/`` seed directory on the
top-level branch: remove the seed directory's self-ignore file (a
``.gitignore`` of ``*`` written at init) so the directory is no longer
ignored, then print the git command to stage it. The git index is **never
touched** — staging is left to you. The toggle is per tree and idempotent,
anchors on the tree's user node by configuration (so it works from any
checkout inside the repo), and ``fractal untrack`` is its inverse. Untracked
is the default state after ``fractal init``.

``NAME``
   Tree root branch. Default: the tree this checkout belongs to.

``--path``
   Repository path. Default: ``.``.

Refuses when the tree has no user node: ``No user node found under <repo>.
Run `fractal init` at the repo root.`` An unknown ``NAME`` refuses with
``No fractal tree '<name>' under <repo>. Trees here: <roots>.``

.. code-block:: console

   $ fractal track
   Tracking .fractal/main/ on the top-level branch.
   Next: stage it with: git add -- .fractal/main

``untrack``
-----------

.. code-block:: console

   $ fractal untrack [NAME] [--path <dir>]

Git-ignore the user node's ``.fractal/<branch>/`` seed directory again —
the default state. Restore the seed directory's self-ignore file (a
``.gitignore`` of ``*`` inside it), then print the git command to unstage
an already-committed seed; the index is never touched. Repo-wide,
idempotent, and anchored on the user node from any checkout, exactly like
``fractal track``.

``NAME``
   Tree root branch. Default: the tree this checkout belongs to.

``--path``
   Repository path. Default: ``.``.

Refuses when the tree has no user node.

.. code-block:: console

   $ fractal untrack
   Ignoring .fractal/main/ on the top-level branch.
   Next: unstage a committed seed with: git rm -r --cached -- .fractal/main

``commit``
----------

.. code-block:: console

   $ fractal commit [MESSAGE] [--init] [--check] [--ignore-scope] [--force]
         [--path <dir>]

Commit the current iteration's work through the commit pipeline: scope
check, wiki index refresh, lint (the node's ``scripts/lint.sh``), stage,
commit, and push — the push is skipped when the node's ``local``
configuration is set. The commit subject is wrapped as
``<branch>: iteration <run>.<iter> (<message>)``; a message that already
carries the branch prefix or begins with an ``iteration`` label is refused,
since the tool adds the wrapping itself. Node loops run this command at the
end of every iteration; an operator can run it from a node worktree.

On a **user node** only the ``--init`` baseline is accepted. The baseline
commits fractal's own artifacts — the project wiki, plus the seed directory
when the tree is tracked — using an explicit pathspec, so any other staged
work is never swept in, and it does not push. Its subject is
``<branch>: init (<message>)``.

``MESSAGE``
   Short description appended to the commit message. Required unless
   ``--check``.

``--init``
   Baseline commit labeled ``init`` instead of ``iteration <run>.<iter>``.
   Skips the wiki index refresh and lint. The only commit form a user node
   accepts. Default: off.

``--check``
   Exit ``1`` if uncommitted changes exist instead of committing: the
   notice ``Uncommitted changes remain (agent should have committed).``
   rides stderr without the ``Error:`` prefix. A clean tree exits ``0`` and
   prints nothing; command and usage errors exit ``2``, so a script can gate
   on the tree state by exit code. Default: off.

``--ignore-scope``
   Commit out-of-scope changes but still lint — a narrower escape hatch
   than ``--force``. Default: off.

``--force``
   Bypass the scope check, lint, and git hooks. Default: off.

``--path``
   Worktree directory. Default: ``.``.

Refuses when:

- any two of ``--init``, ``--check``, ``--ignore-scope``, and ``--force``
  are combined — all pairs are mutually exclusive;
- ``MESSAGE`` is missing and ``--check`` is not set;
- the command runs on a user node without ``--init``
  (``Cannot commit from a user node (only --init is supported).``);
- changes fall outside the node's configured ``scope`` and neither
  ``--ignore-scope`` nor ``--force`` is set;
- ``lint.sh`` fails;
- ``--check`` finds uncommitted changes — exit ``1`` with the bare notice,
  not the ``Error:`` / exit ``2`` refusal shape.

.. code-block:: console

   $ fractal commit "add the parser skeleton"

``open``
--------

.. code-block:: console

   $ fractal open [NAME] [--path <dir>] [--light | --dark]

Open the TUI cockpit (see :doc:`/tui`), anchored on the tree's user node
and focused on the named node, or on the tree's root when none is named.

``NAME``
   A tree root branch (opens that tree's cockpit at its root) or a node
   branch to focus (opens the tree owning it, focused there; a unique
   trailing segment resolves). Default: the caller's own tree, at its root.

``--path``
   Repository path. Default: ``.``.

``--light``
   Open with the light palette. Default: off.

``--dark``
   Open with the dark palette — the default. Mutually exclusive with
   ``--light``.

Refuses when ``--light`` and ``--dark`` are combined, or when the tree has
no user node.

.. code-block:: console

   $ fractal open parser --light

``pause``
---------

.. code-block:: console

   $ fractal pause [NAME] [--reason <text>] [--path <dir>]

The tree-wide brake. It latches the root first — a ``.paused`` marker
beside the central database — then fans a pause out over every active
node, parent-first, aborting each in-flight agent invocation. Every loop
parks with status ``paused``, leaving its run and iteration rows open for
``fractal resume`` to adopt; a parked node has no tmux session — that is
its normal state, not a crash. While the tree is latched, spawning
(``fractal node init``) and ``fractal node start`` refuse everywhere in the
tree. The command anchors on the user node by configuration, so it works
from any checkout inside the repo. To pause a single subtree instead, use
``fractal node pause`` (:doc:`/cli/node`); pause semantics are covered in
:doc:`/guide/lifecycle`.

Output: ``Pause signal sent to N node(s) (in-flight agents aborted; loops
park paused)``, or ``No active nodes to pause (tree latched until
resume).`` when nothing is running — the latch still lands.

``NAME``
   Tree root branch. Default: the tree this checkout belongs to.

``--reason``
   Optional reason, recorded on the pause events and appended to the
   confirmation. Default: none.

``--path``
   Repository path. Default: ``.``.

Refuses when the tree has no user node.

.. code-block:: console

   $ fractal pause --reason "budget review"
   Pause signal sent to 2 nodes (in-flight agents aborted; loops park paused): budget review

``resume``
----------

.. code-block:: console

   $ fractal resume [NAME] [--path <dir>]

The tree-wide release. It lifts the root latch first — spawns and starts are
legal again the moment the release begins — withdraws the pending pause on
any node still parking, then relaunches every parked loop leaf-first. Each
relaunched loop adopts its open run where the pause left it: the same
budgets and iteration count, the interrupted step re-entered (resuming the
recorded agent session when one exists, re-orienting fresh otherwise), and
run and iteration deadlines credited for the paused span.

Output: ``Resumed N node(s) (parked loops relaunched leaf-first; live
pauses withdrawn)``, or ``No paused nodes to resume.``.

``NAME``
   Tree root branch. Default: the tree this checkout belongs to.

``--path``
   Repository path. Default: ``.``.

Refuses when the tree has no user node.

.. code-block:: console

   $ fractal resume
   Resumed 2 nodes (parked loops relaunched leaf-first; live pauses withdrawn)

``reset``
---------

.. code-block:: console

   $ fractal reset [NAME] [--force | -f] [--path <dir>]

The middle teardown tier, between ``fractal node delete`` (one subtree) and
``fractal destroy`` (one tree, or the whole fractal with ``--all``). It
removes **every** one of the tree's node worktrees, local branches, and
registry registrations. Sibling trees and the user node's data —
configuration, memory, and the central database with every history row —
plus the project wiki and baseline commits survive, so fresh nodes can spawn
immediately after. Remote branches are left on the remote and listed.
A stale tree-wide pause latch is also cleared.

Without ``--force`` an interactive confirmation names the node count; when
paused nodes exist, a separate warning names how many hold frozen mid-step
work. Confirming (or passing ``--force``) authorizes killing those paused
nodes as part of the teardown.

``NAME``
   Tree root branch. Default: the tree this checkout belongs to. Only that
   tree's nodes are removed; sibling trees are untouched.

``--force``, ``-f``
   Skip the confirmation prompt; paused nodes are killed without asking.
   Default: off.

``--path``
   Repository path. Default: ``.``.

Refuses when:

- any of the tree's nodes' loop runtime is alive (a tmux session, or a
  headless or bare loop's recorded process group) — kill it first with
  ``fractal node kill <branch>``; ``--force`` never overrides this, it only
  skips the prompt;
- the runtime probe is inconclusive for a node that still has something to
  protect — an unsettled status, or a lingering ``.pgid``, ``.socket``, or
  ``.headless`` record (restore tmux visibility or check ``ps``, then
  retry); a settled node with none of those records proceeds;
- any of the tree's node worktrees is locked;
- the caller stands inside one of the tree's node worktrees (run from the
  repo root).

.. code-block:: console

   $ fractal reset
   Warning: This permanently removes every one of the tree's node worktrees, branches, and registrations. The user node, project wiki, and all history are left in place.
   Reset the tree 'main' at /home/user/myproject (5 nodes)? [y/N]: y

``destroy``
-----------

.. code-block:: console

   $ fractal destroy (NAME | --all) [--force | -f] [--path <dir>]

The top teardown tier, database included. ``fractal destroy NAME`` removes
one tree by its root branch: everything ``fractal reset`` removes for that
tree, **plus** its user node's data directory (configuration and the
central database). Sibling trees survive, along with the shared
``.worktrees/`` directory and fractal's block in ``.git/info/exclude``;
when the destroyed tree was the last one, those go too. With ``--all`` the
command is the full inverse of ``fractal init``: it removes every tree's
worktrees, branches, and data directories, the ``.worktrees/`` directory,
and the exclude block. Exactly one scope must be named — a bare
``fractal destroy`` is refused as ambiguous. In both forms committed
artifacts — the project wiki and baseline commits — remote branches, and
the tree's own root branch remain. The confirmation prompt, paused-node
kill policy, and refusal conditions are the same as ``fractal reset``.
``--all`` on a repository with no fractal is a clean no-op (the
confirmation still asks, reporting 0 nodes); a ``NAME`` that matches no
tree is refused with ``No fractal tree '<name>' under <repo>.``

``NAME``
   Root branch of the tree to destroy. Mutually exclusive with ``--all``;
   exactly one is required — the tree is never inferred from the checkout.

``--all``
   Destroy every tree and remove ``.worktrees/`` (the full inverse of
   ``fractal init``). Mutually exclusive with ``NAME``.

``--force``, ``-f``
   Skip the confirmation prompt; paused nodes are killed without asking.
   Default: off.

``--path``
   Repository path. Default: ``.``.

Refuses when neither or both of ``NAME`` and ``--all`` are given
(``Name a tree or pass --all (exactly one).``).

.. code-block:: console

   $ fractal destroy main
   Warning: This permanently removes the tree's node worktrees and branches plus its fractal data, including its user node. Sibling trees, the project wiki, and commit history are left in place.
   Destroy the tree 'main' at /home/user/myproject (5 nodes)? [y/N]: y

   $ fractal destroy --all
   Warning: This permanently removes every node worktree and branch plus all fractal data, including every user node. The project wiki and commit history are left in place.
   Destroy the fractal at /home/user/myproject (5 nodes)? [y/N]: y
