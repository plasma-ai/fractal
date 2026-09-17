---
name: features/agents/extending
desc: |
  How a new provider backend slots in: one new implementation module whose
  agent subclass declares its capabilities and overrides the private hooks,
  registered in the core agent module or injected from outside the package.
created: 2026-07-21T05:04:16Z
updated: 2026-07-21T05:04:16Z
---

# features/agents/extending

[[features/agents/_index|..]]

***

Agent invocation is a deliberate core/impl seam: `fractal/core/agent.py` defines
the base class and the registry, and each provider lives in one `fractal/impl/`
module. A new backend is therefore one new module plus one registry entry —
nothing in `core/`, `cli/`, or `tui/` changes.

## The subclass shape

Backends override only `_`-prefixed hooks and capability attributes; the public
verbs own validation, event emission, and the settle invariants. The
public/private pairing is uniform: the public verb (invocation, spawn,
preflight, seeding, session and cost recording) validates and emits, then
delegates to its private hook, which the subclass implements:

- the invocation hook builds the provider argv for a fresh, resumed, or forked
  session, spelling the model, effort, and budget overrides in the provider's
  dialect and returning any provider-specific environment keys (a key valued
  `None` means *unset* — that is how a native route scrubs an ancestor route's
  keys from the composed spawn environment);
- the parser is a stream-parser subclass fed the subprocess's output line by
  line, emitting typed stream events (session, cost, actions); a backend may
  swap parsers per route;
- the configured-model and rates hooks back cost accounting: the former resolves
  the model the provider's own config defaults to, the latter maps a model to
  its pricing;
- the transcript hook (with its fallback) locates a session's transcript file —
  the seam the project-files surface fronts (see
  [[features/files/transcripts]]);
- the preflight hook probes provider readiness before a run commits; a backend
  that spawns a probe routes it through the spawn verb and hands the live
  process to the caller's `register` callback before waiting on it, so the loop
  can record its group under the node's own marker;
- the seeding hook materializes the provider's config directory in a new node,
  receiving the parent node's data directory so files an inherited config
  references — codex's relative model instructions file — travel with the config
  copy.

Capability declarations — session forking, who mints session ids, cost scope,
pricing needs, budget enforcement, result-frame cadence, and the provider route
list — are class attributes read by the base class; see
[[features/agents/providers]] for their meaning.

Observability rides the `on_`-event hooks: a host application overrides them
(calling super and returning the event) to ship telemetry. The package emits to
named loggers and never configures handlers, so the host owns logging
configuration.

## Registration

Inside the package, the new module joins the registry dict in
`fractal/core/agent.py` mapping its base command to a `module:Class` string; the
import happens lazily at resolve time, so an uninstalled optional backend costs
nothing until spawned.

Outside the package there are two injection points. An embedding application
calls the registry's register verb at import time — explicit registrations claim
the name and win over the hook file. A deployment can instead drop a hook file
in the tree's data directory; the registry consults it once per process, so
injected subclasses resolve across every process boundary (tmux loop relaunches,
CLI, TUI). A hook file that fails to load is a sticky error, re-raised on every
later resolve for that tree.
