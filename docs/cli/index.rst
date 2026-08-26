Command-Line Interface
======================

The ``fractal`` executable is the operating surface for a fractal tree. The
top-level commands manage the tree as a whole — initialization, baseline
commits, tree-wide pause and resume, teardown — and public sub-apps manage
nodes, inter-node messaging, and iteration plans. Agents inside the tree
run the same commands: every node steers itself and its children through
this CLI, whether driven by an operator at a shell or by the
:doc:`fractal skill </skill>`.

Run ``fractal --help`` (or ``--help`` on any command) for the built-in
summaries; ``fractal --version`` prints the package version.
``fractal --install-completion`` installs tab-completion for the current
shell, and ``--show-completion`` prints it for manual installation.

Command map
-----------

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Command group
     - Purpose
   * - :doc:`fractal </cli/fractal>`
     - Tree-level operations: skill install, initialization, baseline
       commits, tree-wide pause and resume, teardown, and the TUI cockpit.
   * - :doc:`fractal node </cli/node>`
     - Node lifecycle and management: create, start, signal, merge, delete,
       inspect, and retune nodes.
   * - ``fractal node time``
     - Time budget introspection: seconds until the next run, iteration, or
       step timeout (documented on the :doc:`node page </cli/node>`).
   * - ``fractal node cost``
     - Cost budget introspection: remaining headroom, recorded spend, and a
       per-node breakdown (documented on the :doc:`node page </cli/node>`).
   * - ``fractal node config``
     - Read and write a node's configuration keys (documented on the
       :doc:`node page </cli/node>`; the key reference is
       :doc:`/configuration`).
   * - :doc:`fractal radio </cli/radio>`
     - Inter-node messaging: send, post, read, reply, react, and subscribe.
   * - ``fractal radio channel``
     - Create, delete, and list custom channels (documented on the
       :doc:`radio page </cli/radio>`).
   * - :doc:`fractal plan </cli/plan>`
     - Create and list per-iteration plan files.

Conventions
-----------

These behaviors are shared by every command; the per-command pages state
only what deviates.

- **Errors.** Core refusals print ``Error: <message>`` on stderr and exit
  with code ``2``, as do argument errors raised at the CLI boundary — bad
  values, unknown or ambiguous node names, mutually exclusive flags,
  missing required options — which print a usage error
  (``Invalid value: <message>``). Exit code ``1`` is reserved for a
  command's own nonzero outcome (``fractal commit --check`` on a dirty
  tree), so a script can never read a failed run as a legitimate result.
  The last line of every failed command is ``FAILED (exit N)`` on stderr
  (bold red on a terminal), so a truncated log still names the failure.
- **The caller and the target.** Almost every command takes ``--path``
  (default ``.``), which identifies the *caller's* worktree — the node the
  command runs as. Where a command acts on another node, the target is the
  positional ``NODE`` argument (a branch name). The radio verbs that write
  rows attributed to the caller resolve it env-first instead — an explicit
  ``--path``, else the loop-exported ``_NODE``, else the cwd — and
  ``fractal radio read``'s ``--path`` selects the mailbox being viewed
  (see :doc:`/cli/radio`).
- **Trees.** One repository can carry several fractal trees, one per branch
  ``fractal init`` ran on. The tree-scoped verbs (``fractal pause``,
  ``resume``, ``reset``, ``track``, ``untrack``, ``open``) take the tree's
  root branch as an optional positional ``NAME`` and otherwise infer it from
  the caller's branch — a lone tree answers any checkout; with several
  trees, a checkout belonging to none of them refuses and asks you to name
  the tree. On these verbs ``--path`` is the repository path, not a node
  worktree. ``fractal open`` also accepts a node branch in that slot, which
  opens the tree owning that node, focused there. ``fractal destroy`` takes
  the same ``NAME`` but never infers it; ``--all`` is the only repo-wide
  form.
- **Short names.** ``NODE`` arguments on the ``fractal node`` commands
  (``node cost`` targets included) and the node-branch form of
  ``fractal open`` resolve a unique trailing segment to the full dotted
  branch (``lexer`` finds ``main.parser.lexer``). An ambiguous short name
  refuses with the candidate list; an unknown one refuses with
  ``No node found for branch: '<name>'``. The root-branch ``NAME`` argument
  on ``fractal track``, ``untrack``, ``pause``, ``resume``, ``reset``, and
  ``destroy`` matches a tree's root branch exactly — no short names — and
  an unknown one refuses with ``No fractal tree '<name>' under <repo>.
  Trees here: <roots>.``. ``fractal open`` and ``fractal node list`` take
  either name in their one slot: a root matches exactly, anything else
  resolves as a node branch. The ``fractal radio`` targets (``--node``) take
  the full branch name only.
- **Listings.** Listing commands print a text table on a TTY and CSV
  automatically when piped, so scripts get machine-readable output without
  flags; ``--csv`` forces CSV either way. Where ``--json`` exists it prints
  a JSON array (``[]`` when empty) and is mutually exclusive with
  ``--csv``. Empty listings still emit the header row.
- **stdout and stderr.** stdout is the parseable surface — UUIDs, tables,
  paths. Notices, warnings, routing echoes, and confirmations of defaulted
  options ride stderr.
- **Numeric caps.** ``--max-depth``, ``--max-children``, and
  ``--max-descendants`` must be at least ``0`` (on ``node init`` and
  ``node update``, ``0`` disables spawning); ``--max-iters`` and the cost
  caps (``--max-cost``, ``--max-iter-cost``, ``--max-step-cost``) must be
  greater than ``0``. Unlimited is expressed by omitting the flag, never by
  a negative or sentinel value, and an integer cap must fit a signed 64-bit
  integer (below ``2**63``).
- **Durations.** Duration-valued options take ``<number><s|m|h|d>`` — for
  example ``30s``, ``10m``, or ``1h`` — and must amount to at least one
  second. Bare numbers are rejected. See :doc:`/configuration`.

Internal commands
-----------------

Fractal's hidden sub-apps and the underscore-prefixed ``fractal node``
commands are internal plumbing invoked by fractal's own scripts — the
in-tmux iteration loop, node seeding, and pre-init configuration writes.
They are hidden from ``--help`` and are not user surface; their effects
appear to users only as the iteration loop's normal behavior (see
:doc:`/guide/loop`).

.. toctree::
   :maxdepth: 2
   :caption: Commands

   fractal
   node
   radio
   plan
