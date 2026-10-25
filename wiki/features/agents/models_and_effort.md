---
name: features/agents/models_and_effort
desc: |
  Where a spawn's model and reasoning-effort overrides come from, how each
  provider backend spells them on its command line, how a backend resolves
  the model its own configuration defaults to, and how the served model is
  recorded off the stream.
created: 2026-07-21T05:04:16Z
updated: 2026-10-24T04:34:08Z
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
fractal checks no level against the provider's vocabulary; an unknown level
surfaces as the agent's own error. The one place fractal orders levels is the
effort ladder a thinking-capped step's steer lowers through (see
[[#The effort ladder]]). A hard USD budget is a separate per-invocation knob
accepted only by backends that declare budget enforcement; asking a
non-enforcing backend for one is an error.

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
beside the serving one), so it names no served model; codex's stream never names
a model, so its served model is read off its session log's `turn_context` once
the invocation reconciles after exit. Beside the row's last-wins stamp, every
distinct model the stream names rides the launch's served-model record
(`StreamResult.models`), which is what the loop's model-drop enforcement
compares against a step's pin (see [[features/loop/steps|steps]]) — the row
alone would read a substitution the stream recovered from as clean. Backends
whose stream never names a served model (opencode) leave the record empty, which
the drop check reads as unknown rather than as a match. Each backend resolves
its configured model from its own vendor config, best-effort — an unreadable or
malformed file simply names no model:

- claude walks its settings chain (the node's own settings file, then the
  worktree's `.claude/settings.local.json` and `.claude/settings.json`, then the
  user's `~/.claude/settings.json` only when the node loads the user's own
  settings -- see
  [[features/agents/providers#Claude session isolation|providers]]); the first
  file naming a model wins.
- codex reads the top-level model from its `config.toml`.
- grok reads its `config.toml` default, which lives under a models table rather
  than a top-level key.
- opencode reads the top-level model from its JSON config file.
- omp reads its `config.yml` default once a YAML parser is available.

The iteration row records the served model too: when every step's
stream-reported model agrees, it wins over the config-seeded pin at the
iteration close, so divergence is visible on the row rather than laundered under
the pin.

## The effort ladder

A step stuck at a thinking cap is steered one effort level lower (see
[[features/loop/steps#Stuck steps|steps]]). Each backend declares its ladder as
class attributes: `effort_levels`, the levels lowest first (empty, the base
default, never lowers), and `effort_floor`, the lowest level a lowering reaches.
Claude declares `low`, `medium`, `high`, `xhigh`, `max` with the floor `high`,
the level its seeded `settings.json` defaults to, so only `xhigh` and `max`
steps lower by default; codex and the other backends keep the empty ladder (only
Claude's parser reports a thinking cap anyway). The module-level
`lower_effort(effort, *, levels, floor)` is the pure step down: the next level
below, or the effort itself when it sits at or below the floor or off the
ladder.

The effort to lower is the step's own, else the agent's configured default, read
through the `config_effort()` hook shaped like `config_model()`: claude reads
`effortLevel` through the same settings chain as its model (the shared
`ClaudeAgent._setting`), and the base hook returns `None`.

A project overrides an agent's ladder in the `effort_ladder` key of its
`<project>/.fractal/.settings.json`:
`{"effort_ladder": {"claude": {"levels": [...], "floor": "high"}}}`. The
settings parser (`worktree._parse_settings`) validates every key the file
carries, whichever reader asks: `wiki` as a canonical project folder (see
[[configuration/config_json|config_json]]), and `effort_ladder` as an object of
agent ladders, each with exactly `levels` (unique non-empty strings) and `floor`
(one of them). The loop reads the copy committed on the tree's root branch at
boot (`worktree.committed_effort_ladders`, beside `committed_wiki_setting`) and
holds it for the run, so a node cannot change its own ladder and a committed
change lands at the next start or resume. Since every reader parses the whole
file, `fractal init`, the `--init` baseline, spawn, and the merge footprint
check's reads of the root branch's copy (`Node.check_wiki` for a node in another
sub-project, or with its parent checked out nowhere) refuse an invalid ladder
too. The parser cannot know the levels a provider accepts, so an override naming
a level the CLI rejects fails the steered launch.

Two checks need the agent registry, so they run outside the parser
(`loop._check_effort_ladders`), resolving each name with the tree's data dir so
the deployment hook file's custom agents count. Each ladder must name a
registered agent (a misspelled `Claude` would otherwise keep the default
ladder), and a ladder built only from levels its agent declares in
`effort_levels` must list them in that order, lowest first (a highest-first list
would make the steer raise the effort). A ladder carrying a level of its own is
taken as written.

One read runs the parse and both checks for a node: `loop._effort_ladders`, the
committed copy in the node's own project. Its three callers share its messages,
which name the setting, the file, the root branch, and the fault:

- `Node.start` (fresh and `--continue`) calls it before it changes anything, so
  a bad ladder refuses at the command instead of after "Started".
- `Node.resume` calls it for every node it would relaunch (each reads its own
  project's copy) before the tree latch lifts, any pause is withdrawn, or any
  resume event closes a pause credit span.
- `Loop._read_boot_config`, the first boot stage inside the `_abort_preflight`
  guard (ahead of `_preflight`'s probes), covers a commit landing between the
  command's check and the boot: a start or continue lands `exited` with the
  reason on a closed run row, and a resume stays paused with its run open and
  its credit span re-opened. The same stage reads the node's run parameters (see
  [[configuration/config_json#Validation|config_json]]).

## Preflight

Model acceptance is probed before a run commits to it. codex in particular runs
one bounded preflight probe when an explicit model is set, because some accounts
reject models outside their entitlement (pricing knowledge proves a model
priceable, not that the account accepts it) — a defaulted model skips the probe.
On the openrouter route the preflight instead fails fast when the API key is
missing, and probe failures name the route-specific causes. The base preflight
also validates that the bound route is one the backend supports. A probe that
passes hands the loop a one-line summary of what it spent — the usage the
probe's own stream reports, priced at the model's rates — which the loop prints
as `=== Preflight: codex probe passed ... ===` and books as a `preflight` event
carrying the same line, the record of a turn that lands on no step row and,
events carrying no cost, counts toward no cap.

The codex probe leads its own process group and rides the node's `.step_pgid`
marker for its lifetime, so `kill` reaps it like a step (pause is refused until
the loop stamps the node active, which happens after preflight); a probe that
does not answer within the preflight timeout (`_PREFLIGHT_TIMEOUT`) is cancelled
— TERM, a short grace (`_PREFLIGHT_GRACE`), then KILL on the probe's whole
process tree, including tools codex started in groups or sessions of their own
(see [[features/lifecycle/script_delegation|script_delegation]]). The probe is a
paid invocation — one short turn at the configured model — and its spend stays
off the ledger: it lands in no step row, so the run's recorded spend and every
cost cap leave it out (see [[features/cost/measurement|measurement]]).
