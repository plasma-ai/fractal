Agent Backends
==============

Every node runs a coding-agent CLI — its *agent* — headlessly inside the
iteration loop: fractal assembles the prompt, spawns the agent as a
subprocess in the node's worktree, parses its output stream, and records
the session, model, and cost on the step's database row. Which agent runs,
over which provider route, with which model and reasoning effort, is node
configuration — and each of the four axes can be overridden per step.

These backends ship built in:

.. list-table::
   :header-rows: 1
   :widths: 12 22 18 14 12 16

   * - Agent
     - CLI
     - Per-node config file
     - Provider routes
     - Forks sessions
     - Enforces step budget
   * - ``claude``
     - Claude Code
     - ``settings.json``
     - ``openrouter``
     - yes
     - yes
   * - ``codex``
     - Codex CLI
     - ``config.toml``
     - ``openrouter``
     - no
     - no
   * - ``grok``
     - Grok Build CLI
     - ``config.toml``
     - —
     - yes
     - no
   * - ``opencode``
     - opencode
     - ``opencode.json``
     - —
     - yes
     - no
   * - ``omp``
     - Oh My Pi
     - ``config.yml``
     - —
     - yes
     - no

Each backend keeps a per-node config directory at ``<node_dir>/.<name>/``
(``.claude``, ``.codex``, ...) holding the config file above, seeded at
``fractal node init`` — a child inherits its parent's live copy, falling
back to the package seed. Editing that file steers the agent's own
settings (permissions, default model) for this node only. The agent's
stderr from the latest launch streams to ``<node_dir>/<name>.err``, with
durable per-failure snapshots under ``<node_dir>/tmp/err/``.

Selecting agent, provider, model, and effort
--------------------------------------------

Four node config keys drive the selection (see :doc:`/configuration` for
the full key reference):

``agent``
   The full agent command. The first word names a registered backend and
   must be an executable on ``PATH``; any extra words are spliced into
   every invocation (e.g. ``claude --some-flag``). Commands split on
   whitespace only — quotes and backslashes are rejected. Default:
   inherited from the nearest ancestor node.

``provider``
   Provider route for the agent (``openrouter``). Unset means the
   vendor-native endpoint. Inherited from the nearest ancestor when
   unset; backends with no route axis (``grok``, ``opencode``, ``omp``)
   ignore the key entirely, so a routed ancestor never mis-routes them.

``model``
   Model id, passed to the agent CLI's model flag. Default: none — the
   agent then runs on its own config-file default (fractal records that
   model on the step row but does not pass it on the command line). A
   routed ``claude`` is the exception: see the openrouter route below.

   A pin is honored or the step fails loudly. The stream driver records
   every model the agent's stream names; when a completed launch's record
   does not carry the pin (a gateway slug such as ``anthropic/<id>``, a
   bare-word alias such as ``opus``, and a dated snapshot all match the
   id they resolve to — a truncation, variant suffix, or version bump
   does not), the loop prints ``WARNING: model drop on <step> — pinned
   <model> but <served> served the step``, records a ``model_drop`` event
   with the step's lineage, marks the attempt's step row ``model drop
   (served <served>)``, and re-dispatches the step once. If the
   re-dispatch also serves off-pin, or cannot run (the deadline is spent,
   or a stop or spend ceiling intervenes), the iteration fails with
   ``model drop (served <served>, pinned <model>)`` — never a clean
   completion over wrong-model output. The node is not killed: the loop
   moves on and repeats a warning at the next iteration's start while the
   drop stands unresolved. The iteration row records the model that
   actually served when every step's recorded model agrees (otherwise it
   keeps the pin), and ``fractal node list`` composes ``model drop`` into
   the node's ``detail`` column while the newest iteration carries an
   unresolved drop (see :doc:`/cli/node`). A step with no pin — no
   ``model`` key and no ``model:`` frontmatter — is never flagged.

``effort``
   Reasoning-effort level, passed through unvalidated to the backend's
   effort flag — an unknown level surfaces as the agent's own error.
   Default: none.

A pin reaches the agent only through its flag or the agent's own config
file, never through the ambient environment: fractal unsets
``CLAUDE_EFFORT``, ``CLAUDE_CODE_EFFORT_LEVEL``, and
``CLAUDE_CODE_SUBAGENT_MODEL`` from the environment of every agent launch
(loop steps and ``fractal node chat`` alike), so an effort or sub-agent
model setting exported in the shell that runs ``fractal node start`` never
overrides a node's or step's pin. The seeded node ``settings.json`` sets
none of them either.

Set them at init or retune them between runs:

.. code-block:: console

   $ fractal node init parser --agent codex --provider openrouter \
         --model openai/gpt-5.3-codex --effort high
   $ fractal node config set model=openai/gpt-5.3 effort=medium

The loop binds all four when it boots, so a config edit lands on the next
``fractal node start``. Mid-run, the steering surface is step frontmatter:
step files are re-read every iteration, and the ``agent:``, ``provider:``,
``model:``, and ``effort:`` keys override the node config for that step
alone (see :doc:`/guide/loop` for the frontmatter contract). A per-step
agent or provider override is validated at that step's launch — registered
backend, binary on ``PATH``, supported route — and a failed validation
fails the step, never the run.

Each backend spells the effort flag its own way; fractal translates:

.. list-table::
   :header-rows: 1
   :widths: 20 40

   * - Agent
     - Effort flag
   * - ``claude``
     - ``--effort``
   * - ``codex``
     - ``-c model_reasoning_effort="<level>"``
   * - ``grok``
     - ``--reasoning-effort``
   * - ``opencode``
     - ``--variant``
   * - ``omp``
     - ``--thinking``

Backend requirements
--------------------

claude
~~~~~~

Requires the ``claude`` binary (Claude Code CLI) installed and
authenticated. ``claude`` runs on the user's own config home
(``~/.claude``, or ``$CLAUDE_CONFIG_DIR``) — fractal never touches its
auth or session storage. The node's ``settings.json`` rides the
``--settings`` flag and outranks worktree and user settings, so per-node
permissions, model, and environment live there. ``claude`` reports its own
USD spend per invocation, and it is the only backend that enforces the
per-step budget cap in-flight (the loop passes ``--max-budget-usd``; a
budget stop is a clean stop, not a failure). Supports the ``openrouter``
route.

codex
~~~~~

Requires the ``codex`` binary (Codex CLI) installed and logged in. Each
node's ``.codex`` directory serves as its ``CODEX_HOME``, with
``auth.json`` symlinked to the global home (``~/.codex``, or
``$CODEX_HOME`` when set in the launching environment) — credentials
are shared, never copied, and token refreshes update the global file.
Cost is token-priced from the pricing cache, so recorded spend (and any
cost cap) requires a model with a pricing entry. ``codex`` sessions
(threads) cannot be forked — resume them in place instead. When an
explicit model is configured, ``fractal node start`` runs a bounded probe
invocation before the run, because some accounts reject models the
pricing table can price — a rejection relays ``codex``'s own diagnostic.
Supports the ``openrouter`` route.

grok
~~~~

Requires the ``grok`` binary (Grok Build CLI). Each node's ``.grok``
directory serves as its ``GROK_HOME``, with ``auth.json`` symlinked to the
global home (``~/.grok``, or ``$GROK_HOME`` when set in the launching
environment); an ``XAI_API_KEY`` in the environment is picked up as well.
``grok`` reports its spend on the stream's final frame — trusted as-is,
since subscription/pool models have no pricing-table entry — with token
pricing only as a fallback. ``grok`` reports no tool activity on its
stream and suppresses reasoning output, so a quiet pane during a ``grok``
step is normal.

opencode
~~~~~~~~

Requires the ``opencode`` binary. The node's ``opencode.json`` is passed
via ``OPENCODE_CONFIG``; provider API keys (``OPENROUTER_API_KEY``,
``ANTHROPIC_API_KEY``, ...) are auto-detected from the environment.
Runs headlessly with ``--auto``, so every permission is auto-approved.
Cost is self-reported per internal step and accumulated. Sessions persist
in opencode's own central database, so there is no per-session transcript
file to inspect.

omp
~~~

Requires the ``omp`` binary (Oh My Pi). The node's ``.omp`` directory
serves as its ``PI_CODING_AGENT_DIR`` — config (``config.yml``),
credentials, and session files all live there. Runs headlessly with
``--yolo``, so every tool call is auto-approved; provider keys ride the
environment. Cost is self-reported per turn and accumulated.

The openrouter route
--------------------

``claude`` and ``codex`` can be routed through OpenRouter instead of their
vendor-native endpoints: set ``provider`` to ``openrouter`` (or
``provider: openrouter`` on a single step). The route authenticates on
``OPENROUTER_API_KEY`` alone — export it in the shell that runs
``fractal node start``, which forwards it into the node's tmux session; a
missing key fails preflight and any later launch. Model ids on the route
are OpenRouter slugs, author-prefixed: ``openai/gpt-5.3-codex``,
``anthropic/claude-sonnet-4.6``.

A routed ``claude`` with no ``model`` set does not fall back to the model
in its ``settings.json``: fractal pins ``anthropic/claude-sonnet-4.6``
through ``ANTHROPIC_MODEL`` for that invocation (the process environment
outranks the settings file), so set ``model`` explicitly to run anything
else on the route. Every routed ``claude`` launch also points the
opus/sonnet/haiku/fable default slots at OpenRouter's
``~anthropic/claude-*-latest`` aliases, so ``best`` and subagent
references keep resolving.

Routed ``claude`` prices spend from the stream's token usage through the
pricing cache instead of recording the CLI's own figure — a slug the cache
cannot price records no cost at all, never a wrong figure.

Preflight
---------

Before its first iteration, a run fails fast when the node cannot
possibly work:

- the agent binary must be on ``PATH``;
- an ``openrouter``-routed node must have ``OPENROUTER_API_KEY`` set;
- ``codex`` with an explicit model runs its acceptance probe;
- a run whose agent prices from tokens needs the pricing cache — a stale
  cache warns and is used, a missing cache that cannot be fetched aborts.

A failed preflight on a fresh or continued start records the run as
``exited`` with the reason. On ``fractal node resume`` the paused run is
preserved instead: the node stays ``paused``, the reason lands on a
``pause`` event (``resume preflight failed: <reason>``), and a later
resume in a fixed environment re-adopts the run. Steps that override the
agent or provider re-validate at their own launch.

Costs
-----

Every step row carries the spend the backend's stream reported, flushed
figure by figure as it arrives — a killed or timed-out step keeps the last
figure recorded. Read spend with the cost commands (full surfaces in
:doc:`/cli/node`):

.. code-block:: console

   $ fractal node cost spent parser
   $ fractal node cost remaining parser
   $ fractal node cost breakdown parser

From the user's point of view:

- **Cost is recorded, never estimated.** A step whose backend reported no
  figure stores no cost, and readers treat that as *unknown* — never $0.
  A step that ended abnormally (any status other than ``completed``) with
  a session on its row but no figure flushed is marked ``unpriced`` in
  its row metadata; a figure that flushes later strips the marker. A step
  that completed with no reported figure stores no cost and carries no
  marker. ``cost spent`` and ``cost breakdown`` disclose on stderr how
  many ended steps carry no recorded cost, marked or not.
- **The figure's source varies by backend.** ``claude`` (native),
  ``grok``, ``opencode``, and ``omp`` report their own spend on the
  stream; ``codex`` — and ``claude`` behind ``openrouter`` — are priced
  from token counts against the pricing cache
  (``~/.fractal/pricing.json``, LiteLLM's public price table, fetched at
  run start and refreshed daily during long runs).
- **Only claude enforces the per-step cap in-flight.** The loop enforces
  ``max_cost`` and ``max_iter_cost`` between launches for every backend,
  but ``max_step_cost`` is a hard limit only for ``claude``
  (``--max-budget-usd``) and warn-only everywhere else. Budget semantics
  are covered in :doc:`/guide/loop`.
- **A cost cap requires trackable spend.** On a token-priced or routed
  agent the model must be set and present in the pricing table, or the
  step fails at launch with a specific error.

Sessions and transcripts
------------------------

Every step row records the agent, model, and session id that ran it; in a
continuous node the steps of one iteration resume a single session per
agent, and the iteration row carries it too. These are real provider
session ids — they can be resumed or forked outside the loop.

``fractal node chat`` is the user surface: send one prompt to a node's
agent and stream the reply.

.. code-block:: console

   $ fractal node chat parser "what is blocking the tests?"

By default the chat runs a fresh session. Options:

``--session <id>``
   Fork a recorded session (a ``session`` value off a step or iteration
   row) instead of starting fresh.

``--current``
   Fork the node's current loop session (excludes ``--session`` and
   ``--resume``).

``--resume``
   Continue ``--session`` in place instead of forking it.

``--model <name>``
   Model override (default: the node's configured model).

``--path <dir>``
   Worktree directory (default: ``.``).

The resulting session id is echoed to stderr as ``session: <id>``, so the
thread can be continued with ``--session <id> --resume``. A chat records
nothing in the database — it never disturbs the node's rows or budgets.
Because ``codex`` cannot fork, ``--current`` and the forking
``--session`` form fail on ``codex`` nodes; continue a ``codex`` thread
with ``--session <id> --resume``.

For direct inspection, each backend persists transcripts in its own
layout:

.. list-table::
   :header-rows: 1
   :widths: 14 60

   * - Agent
     - Transcript location
   * - ``claude``
     - ``~/.claude/projects/<dashed-worktree-path>/<session>.jsonl``
       (under ``$CLAUDE_CONFIG_DIR`` when set)
   * - ``codex``
     - ``<node_dir>/.codex/sessions/<y>/<m>/<d>/rollout-*-<session>.jsonl``
   * - ``grok``
     - ``<node_dir>/.grok/sessions/<encoded-worktree-path>/<session>/events.jsonl``
   * - ``omp``
     - ``<node_dir>/.omp/sessions/*/*_<session>.jsonl``
   * - ``opencode``
     - none — sessions live in opencode's central database

There is no transcript CLI command: programmatic access goes through the
library surface (`fractal.core.agent.Agent.transcript`, fronted by
`fractal.core.session.Sessions`) in :doc:`/api`.

Adding a backend
----------------

A backend is one implementation module — invocation argv, stream parser,
transcript layout, capability attributes — registered by name in the
`fractal.core.agent` registry, alongside the built-in
``fractal/impl/{claude,codex,grok,opencode,omp}.py`` modules. An embedding
application registers in process with `fractal.core.agent.register`; a
tree can also carry an ``agents.py`` hook file in its root node's data
directory, whose exported `fractal.core.agent.Agent` subclasses register
by their ``name`` in every fractal process that touches the tree. The
base-class contract is documented in the API reference under
:doc:`/api`.
