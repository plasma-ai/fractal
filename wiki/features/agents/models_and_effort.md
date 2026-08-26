---
name: features/agents/models_and_effort
desc: |
  Where a spawn's model and reasoning-effort overrides come from, how each
  provider backend spells them on its command line, how a backend resolves
  the model its own configuration defaults to, and how the served model is
  recorded off the stream.
created: 2026-07-21T05:04:16Z
updated: 2026-07-21T05:04:16Z
---

# features/agents/models_and_effort

[[features/agents/_index|..]]

***

## Override sources

A node's agent identity and its overrides are configuration: the config keys
`agent` (the command, extra words spliced into every invocation), `provider`
(the route, see [[features/agents/providers]]), `model`, and `effort`. Every
invocation builder also accepts per-call `model` and `effort` overrides, so a
single spawn can deviate from the node's configured defaults.

Effort is passed through to the provider's own effort flag *unvalidated* —
fractal does not maintain a level vocabulary; an unknown level surfaces as the
agent's own error. A hard USD budget is a separate per-invocation knob accepted
only by backends that declare budget enforcement; asking a non-enforcing backend
for one is an error.

## Per-backend spelling

Each backend translates the same two overrides into its own CLI dialect:

| backend    | model flag | effort flag                         |
| ---------- | ---------- | ----------------------------------- |
| `claude`   | `--model`  | `--effort`                          |
| `codex`    | `-m`       | `-c model_reasoning_effort="<lvl>"` |
| `grok`     | `-m`       | `--reasoning-effort`                |
| `opencode` | `-m`       | `--variant` (a model variant)       |
| `omp`      | `--model`  | `--thinking` (a thinking level)     |

For claude the effort flag outranks the settings-file effort level, and it is
the only effort channel fractal uses -- the `CLAUDE_CODE_EFFORT_LEVEL` env knob
is deliberately not set, since a spawn-env knob leaks into the session's own
subprocesses and would override any nested claude run; ambient copies of the
effort knobs are actively *unset* at invocation compose -- an operator shell's
`CLAUDE_CODE_EFFORT_LEVEL` would override the step pin inside the session, and a
stale `CLAUDE_EFFORT` (claude's own export to hook/Bash subprocesses) would
masquerade as the child session's -- and so is the model-forcing
`CLAUDE_CODE_SUBAGENT_MODEL`, which would force every fan-out sub-agent onto one
model, explicit per-agent pins included (the seeded node settings deliberately
do not set it either); node settings (permissions, model, environment) ride a
CLI flag rather than a file merge. On the openrouter route a model-less claude
invocation pins an explicit model slug, because the process environment beats
the settings file and the route must not trust latest-model aliases.

## The served-model record

The step row records the model that *actually served*, not merely the one asked
for: each stream parser starts from the backend's *configured model* and prefers
the model the stream itself reports, re-stamping the session whenever the stream
names a different one (record_session is idempotent), so the row ends up
carrying the last served model even for defaulted spawns. For claude the init
frame names only the model the CLI resolved — infrastructure can silently
substitute — so the served model is read off each *real top-level* assistant row
(`.message.model`), skipping sidechain rows (a subagent's, flagged by
`parent_tool_use_id`, legitimately running its own model), synthetic rows (the
CLI's injected error stand-ins), and non-string wire noise; omp reads it off
each `turn_end` frame, and grok off its terminal frame's sole `modelUsage` key —
its only model report, and a multi-entry usage is ambiguous (an auxiliary model
beside the serving one), so it names no served model. Beside the row's last-wins
stamp, every distinct model the stream names rides the launch's served-model
record (`StreamResult.models`), which is what the loop's model-drop enforcement
compares against a step's pin (see [[features/loop/steps|steps]]) — the row
alone would read a substitution the stream recovered from as clean. Backends
whose stream never names a served model (codex, opencode) leave the record
empty, which the drop check reads as unknown rather than as a match. Each
backend resolves its configured model from its own vendor config, best-effort —
an unreadable or malformed file simply names no model:

- claude walks its settings chain (the node agent dir's local settings over its
  `settings.json`, then the user's `~/.claude/settings.json`); the first file
  naming a model wins.
- codex reads the top-level model from its `config.toml`.
- grok reads its `config.toml` default, which lives under a models table rather
  than a top-level key.
- opencode reads the top-level model from its JSON config file.
- omp reads its `config.yml` default once a YAML parser is available.

The iteration row records the served model too: when every step's
stream-reported model agrees, it wins over the config-seeded pin at the
iteration close, so divergence is visible on the row rather than laundered under
the pin.

## Preflight

Model acceptance is probed before a run commits to it. codex in particular runs
one bounded preflight probe when an explicit model is set, because some accounts
reject models outside their entitlement (pricing knowledge proves a model
priceable, not that the account accepts it) — a defaulted model skips the probe.
On the openrouter route the preflight instead fails fast when the API key is
missing, and probe failures name the route-specific causes. The base preflight
also validates that the bound route is one the backend supports.
