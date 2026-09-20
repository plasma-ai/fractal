---
name: features/agents/providers
desc: |
  The supported agent backends, the registry in the core agent module that
  resolves a base command to its backend class, and the provider routes a
  backend may expose beside its vendor-native endpoint. It also states when
  codex's transient in-turn error notifications recover after a clean exit and
  which shapes stay fatal.
created: 2026-07-21T05:04:16Z
updated: 2026-07-21T05:04:16Z
---

# features/agents/providers

[[features/agents/_index|..]]

***

## Supported backends

Five provider backends ship in `fractal/impl/`, one module per provider, each
registered in `fractal/core/agent.py` under its base command word: `claude`,
`codex`, `grok`, `opencode`, and `omp`. The base command is the backend's
identity everywhere: the executable word, the DB agent column, the session-map
key, and the stem of the node's per-agent config directory (e.g. `.claude`).

An agent owns its own conversation loop inside a provider subprocess. The
base-class contract is capabilities — invocation building, spawning, stream
parsing, session policy, cost mode — never a message list. Backends differ along
declared capability attributes rather than code paths: whether the session can
be forked; whether the agent mints its own session id (codex) or the caller
mints and pre-stamps it (claude); whether cost figures are per-invocation or
cumulative thread totals settled against prior sibling steps; whether the
backend needs LiteLLM token pricing (fatal at run start if the fetch fails);
whether it accepts a hard per-step budget flag; and whether it emits one result
frame per step (opencode) rather than one terminal result per turn.

## The registry

`fractal/core/agent.py` holds the registry mapping each base command to its
backend — either a `module:Class` string imported lazily at resolve time, or a
class object. Core reaches `impl/` only through this registry, so backend
imports stay off every code path that never spawns an agent. The registry has
three public verbs:

- **resolve** returns the backend class for a base command, raising a
  `ValueError` naming the supported commands when the name is unregistered or
  its module cannot be imported.
- **register** injects or overrides a backend under a base command — the
  in-process extension point for an embedding application. Explicit
  registrations win over hook-file registrations.
- **supported** lists the registered base commands in registration order.

Beside in-process registration, a tree's data directory may carry a *deployment
hook file*: resolve consults it once per process, letting injected subclasses
survive every process boundary (tmux loop relaunches, CLI, TUI). A hook file
that fails to load is sticky — the failure re-raises on every later resolve for
that tree. See [[features/agents/extending]] for the full extension seam.

## Provider routes

A backend may list *provider routes* — alternate API endpoints beside the
vendor-native one. An empty route list means the backend has no route axis;
`None` always means the vendor-native endpoint. `claude` and `codex` each
support one route, `openrouter`; `grok`, `opencode`, and `omp` are route-less.

The bound route is validated at the spawn verbs: asking for a route the backend
does not list raises an error naming the supported routes. Both openrouter
routes authenticate on the `OPENROUTER_API_KEY` environment variable alone and
refuse to launch without it. Mechanically, codex routes by defining an inline
model-provider table pointing at the openrouter base URL, so one config template
serves both routes; claude routes through vendor environment variables
redirecting its CLI at spawn time, and a model-less routed invocation pins an
explicit model slug rather than trusting latest-model aliases.

Routes are hygienic across the node tree: the native route explicitly scrubs the
routing keys from the composed spawn environment, so a routed ancestor's
environment never silently reroutes a native descendant. A route-less backend
has no route to take, so an openrouter-defaulting ancestor never pins a route
onto it. The routed claude backend also swaps in a dedicated stream parser,
because the proxy rewrites model slugs and zeroes per-frame usage — cost then
computes from the authoritative frames only.

The route a node actually runs is configuration: the `provider` config key
carries the node's effective route, alongside `agent`, `model`, and `effort`
(see [[features/agents/models_and_effort]]).

## Codex stream recovery

Codex's `exec --json` stream can report errors while a turn is still active.
Fractal displays each diagnostic immediately. A canonical top-level `error`
frame, containing only `type` and a nonblank string `message`, remains
provisional inside the single active turn. It recovers only when that same
correctly ordered turn completes with valid usage and the actual process exits
with status zero. Recovery depends on the frame structure and terminal outcome,
not the diagnostic's wording.

A `turn.failed` frame stays fatal even if followed by a completion. Errors
outside the active turn, after its terminal frame, with malformed or additional
fields, or without a valid completion and known successful exit remain fatal.
The final stream result carries the remaining failure detail for every caller,
including direct chat finalization.

Recovery establishes the stream outcome only. Cost still requires the complete
invocation-bound rollout, model and rate evidence described in
[[features/cost/measurement]]. Missing evidence leaves cost `NULL`. Malformed
streams without an error retain their accounting-only unpriced diagnostic; the
model-acceptance preflight has its own stricter validation contract.
