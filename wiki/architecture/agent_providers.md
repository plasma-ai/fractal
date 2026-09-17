---
name: architecture/agent_providers
desc: |
  The core/impl agent-provider seam: what the agent base class owns, the
  provider name registry, and how the backend modules for claude, codex,
  grok, opencode, and omp slot in.
created: 2026-07-21T04:47:26Z
updated: 2026-07-21T04:47:26Z
---

# architecture/agent_providers

[[_index|..]]

***

Agent invocation is a deliberate seam between `fractal/core/` and
`fractal/impl/`: core defines a provider-agnostic base class and a name registry
in `fractal/core/agent.py`, and each provider CLI gets one backend module in
`fractal/impl/`. Core never imports a backend directly — it reaches
implementations only through the registry, so the import cost of a provider is
paid only on paths that actually spawn one.

## What the base class owns

An agent owns its own conversation loop inside a provider subprocess; the
base-class contract is *capabilities*, never a message list. The base class
owns:

- **Invocation building.** A composed invocation is pure serializable data: the
  base command's argv words, the working directory (the node's worktree), an
  environment overlay, and an optional caller-minted session id. Backends
  contribute only their reserved provider keys to the environment; the public
  verb composes the full subprocess environment, and a reserved key valued as
  unset scrubs routing variables inherited from an ancestor's provider route.
  Agent commands split on whitespace only — shell quoting is refused at accept
  time.

- **Spawning and stream parsing.** Agents are subprocesses driven line-by-line.
  Each backend ships a parser that turns its own wire format into normalized
  stream events with a small set of kinds — session, text, tool, tool result,
  cost, result, error. Every consumer (the renderer, TUI bubbles, the record
  verbs) branches on the event kind only, never on the provider. Parsed strings
  are sanitized once, centrally, so no downstream sink can meet an unencodable
  surrogate. The shared `finish_stream` seam waits for the actual process after
  stdout drains, then runs provider finalization and the parser's deferred
  terminal events. The caller retains deadline and pipe cleanup. Process-bound
  evidence stays attached to its own invocation, so parsers may be constructed
  either before or after spawning without sharing mutable accounting state
  through their Agent instance.

- **Cost and session policy.** Capability attributes declare how a backend
  reports cost (per-invocation figures or cumulative thread totals, and whether
  pricing must come from a token-rate cache), whether it enforces a hard
  per-step budget flag, whether it mints its own session ids or the caller
  pre-stamps them, and whether sessions can fork. Transcript resolution per
  session id is also a base-class verb with per-backend hooks.

- **Logging and observability hooks.** Every lifecycle moment (call, spawn,
  action, session, cost, budget, error, preflight) has an `on_<event>` hook with
  a default logging level. The package emits to named loggers and never
  configures handlers — a host application attaches its own and can override the
  hooks to ship observability elsewhere.

The split is uniform: backends override only underscore-prefixed hooks and
capability attributes, while the public verbs own validation, event emission,
and the settle invariants.

## The provider name registry

The registry maps an agent base command (`claude`, `codex`, ...) to its backend
class. Registered targets are module-path strings imported lazily at resolve
time, so listing supported agents never imports any backend. Resolution also
consults a per-tree deployment hook file (once per process), letting a
deployment inject or override backends without patching the package; explicit
in-process registrations win over the hook file. The registry's resolve,
register, and supported operations are the whole public surface of the seam.

## The backends

Each backend is one module in `fractal/impl/` pairing a parser with an agent
subclass:

- **claude** — the Claude Code CLI; supports an `openrouter` provider route
  driven through reserved environment routing keys.
- **codex** — the Codex CLI; mints its own session ids and cannot fork a
  session.
- **grok** — the Grok Build CLI.
- **opencode** — the opencode CLI; reports per-step cost and emits a result
  frame per step rather than one per turn.
- **omp** — the Oh My Pi CLI.

A new provider slots in as one new `fractal/impl/` module registered in
`fractal/core/agent.py` — no other package changes. Operator-facing provider
behavior (model and effort overrides, provider routes, per-backend quirks) is
covered under the [[features/agents/_index|features/agents/]] pages; the
rationale for the seam lives in the [[design/_index|design]] branch.
