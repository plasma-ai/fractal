---
name: features/agents
desc: |
  Agent providers and routes: the supported agent backends, model and
  effort overrides, and how a new provider slots in.
created: 2026-07-21T04:35:35Z
updated: 2026-09-20T03:01:00Z
---

# features/agents

[[features/_index|..]]

[[features/agents/extending|extending]]: How a new provider backend slots in:
one new implementation module whose agent subclass declares its capabilities and
overrides the private hooks, registered in the core agent module or injected
from outside the package.

[[features/agents/models_and_effort|models_and_effort]]: Where a spawn's model
and reasoning-effort overrides come from, how each provider backend spells them
on its command line, how a backend resolves the model its own configuration
defaults to, and how the served model is recorded off the stream.

[[features/agents/providers|providers]]: The supported agent backends, the
registry in the core agent module that resolves a base command to its backend
class, the provider routes a backend may expose beside its vendor-native
endpoint, and when codex's in-turn error frames recover after a clean exit.

***

The agent execution surface. Fractal drives coding agents as subprocesses behind
one base-class contract in `fractal/core/agent.py`, with one backend module per
provider in `fractal/impl/`.

- [[features/agents/providers]] — the five supported backends, the registry that
  resolves a base command to its backend class, capability declarations, and the
  provider routes (openrouter) beside the vendor-native endpoints.
- [[features/agents/models_and_effort]] — where model and reasoning-effort
  overrides come from (node config keys and per-invocation overrides), each
  backend's CLI spelling of them, the configured-model fallback, and model
  preflight.
- [[features/agents/extending]] — the core/impl seam: the subclass shape
  (capability attributes, private hooks, observability events) and the three
  registration points (registry entry, in-process register, deployment hook
  file).

Transcript access for a node's sessions is fronted by the project-files surface
([[features/files/_index|files]]); the run loop that consumes these agents lives
in `fractal/core/loop.py`.
