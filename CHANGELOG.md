# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `node init --profile=<name>` + init-time fill-sheet validation: a profile is a
  repo-provided seed bundle under `.fractal/profiles/<name>/` (`steps/` seeds
  the step list, `NODE.md` a deployment-ready charter), and the charter's
  fill-sheet is validated before any worktree exists — its two authored sections
  must be present, every `pin:` line must resolve to a commit and match `--pin`,
  and every `docket: <path>` line must resolve at the pin — so stale pins, stale
  docket rows, and truncated seeds die at init instead of costing the
  commission's opening seat. (Profile config presets are a follow-up; caps and
  modes stay flags.)
- Resumed iterations are no longer context-blind: the harness re-reads the
  node's unread inbox and appends the digest (metadata, priority first; sealed
  mailboxes stay sealed) to every seat of a resumed iteration, so directives
  that arrived after the plan froze are in context before any replayed decision
  executes.
- `node start --continue --drain`: the harness runs the continued run as a drain
  — `_DRAIN` rides every seat's environment,
  `node init`/`node start`/`node update`/`node resume` refuse under it (spawns,
  re-arms, and subtree wake-ups are blocked, not just discouraged), and the
  DRAIN mode doc directs every seat to close out.
- Billing-class breaker: three consecutive instant zero-cost step failures (the
  dead-credits signature) back the loop off exponentially (60s doubling to 1h)
  instead of redispatching hot, announce themselves as `PAUSED: billing` on the
  pane and in the `node list` detail column, and self-clear on the first
  completed launch — the probe; a failure that spent real money never reads as
  an outage.
- Iteration-gap alarms: iteration numbers that advance with no recorded row
  (iterations consumed but never executed — a fleet-wide transient once ate four
  in eleven minutes with zero trace) flag the `node list`/`node status` detail
  column with the missing span (`iteration gap 2.19-2.22`), and the loop warns
  on stderr the moment a fresh row lands past a gap.
- Sealed mailboxes (`sealed` config key; `node init --sealed`): while sealed,
  every message a node hosts is held out of its own seat's context — empty
  listings with an `inbox sealed` notice, a refused `radio read`, hosted rows
  dropped from threads — keyed on the loop-exported `_NODE`, so operator shells
  adjudicate freely and the node's own writes (verdicts) still file;
  `config set sealed=false` unseals. The enforcement half of verifier isolation:
  sealed traffic can no longer leak into a verifier's context through routine
  triage.
- Radio fan-out with per-recipient receipts and relay lineage: `radio send`
  takes a repeated `--node` (every recipient validated before any copy lands;
  stdout prints one `<uuid> <node>` receipt per recipient), `--relay-of <uuid>`
  marks a copy as the relay of an order, and the new `radio relays <uuid>` lists
  every recorded relay — the check that a descendant-relay obligation actually
  executed.
- `node list --json`: a JSON array of typed row objects (mutually exclusive with
  `--csv`), completing the machine-readable trio with `node activity --json` and
  the radio listings' `--json` — operator instruments no longer need comma-split
  CSV scraping that an odd title can corrupt.
- Unmistakable failure frames: every failed `fractal` command closes with a
  `FAILED (exit N)` line as the LAST line of output (bold red on a tty, bare
  text in pipes), so an error frame read through `tail -1` can never pass as
  success; unknown options keep the usage line naming the correct invocation.

### Fixed

- An interrupted billing-gate wait books the gated step and the never-run tail
  as `stopped` rows (`billing gate interrupted`, knowable-zero spend), so the
  one path to a zero-row completed iteration — the gate guards step 1 too — now
  leaves `node activity` a trace of which steps the outage plus interrupt
  consumed; the iteration and run labels are unchanged.
- The retry/breaker backoff polls `finish` alongside pause, stop, and the
  subtree ceiling, so a cascaded budget finish — the very signal a billing
  outage produces — lands within seconds instead of sleeping out a breaker wait
  of up to an hour and buying one more dead probe launch (a pending finish
  silences the ceiling poll, so nothing else could fire).
- The census `PAUSED: billing` mirror excludes cannot-exec launches (recorded
  `agent launch failed`) exactly like the loop's breaker, so a broken agent
  install — whose loop is hot-retrying with no breaker armed — renders as the
  fault it is instead of steering the operator at a credit refill.
- An idle-target kill stamps `killed` under the `.worktrees` flock before the
  reap, so a kill racing a mid-validation `start` is fully serialized against
  the loop's flock'd boot check: kill-first stands the boot down, loop-first
  keeps the reap a live target — a post-reap stamp let the reap no-op on the
  not-yet-booted session, the loop boot in the window, and a live loop burn
  spend indefinitely under a `killed` census row with nothing left to reap it.
- Adversarial-review hardening of this wave's own changes: the resumed-seat
  digest reads the inbox channel only; relay UUIDs normalize case like every
  other verb; a kill that wins the boot window stands the loop down instead of
  being overwritten `active`; the seal also covers `feed` self-subscriptions and
  `radio relays`, and the seat-facing refusals stop naming the unseal command; a
  non-billing failure breaks the breaker's streak and an interrupted breaker
  wait never buys a hot launch; a goal-met finish with a cap-overshoot note no
  longer renders as `run exhausted`.
- The repo-hygiene version-agreement test pins the `.cruft.json` project version
  too (mutation-checked), so a bump that misses the cruft context fails on the
  PR instead of at the tag-time build gate.
- The finish ceremony is idempotent across a swallowed commit: a deliberate
  `node finish` whose run died before the terminal cascade consumed it (a torn
  seat, a stop interrupting the drain, the force-commit backstop racing the
  wind-down) carries onto the next `--continue` run and books immediately — a
  docket-met node can no longer keep iterating and burning budget;
  budget-stemmed finishes stay with their run. A timed-out step whose backstop
  finds nothing to stage is named loudly (`timed out with no committed output`)
  instead of silently voiding the pass.

### Changed

- The wiki contract tests pin plasma-wiki's new merge and lint contracts (the
  union merge driver — both sides' link rows survive an `_index.md` merge,
  deduplicated, with `wiki update` re-sorting and pruning stale rows — and typed
  lint issues with exit 0 clean / 1 issues / 2 command error). The suite now
  requires a plasma-wiki carrying those contracts (newer than 1.2.0); no fractal
  runtime code needed changes — it consumes lint by boolean exit code only,
  which is unchanged.
- The census distinguishes the two `completed` landings: a run that ended on its
  iteration cap surfaces as `run exhausted: Reached max iterations (N)` in the
  `node list`/`node status` detail column, while a drained finish stays bare — a
  run-out lane (usually a re-continue candidate) can no longer pass as
  done-conditions-met; `--continue` keeps looping per its per-run `max_iters`,
  pinned by test.
- The seeded COMMIT step's sign-off is unconditional: the parent-is-root
  conditional is gone, so a finishing node posts its sign-off (and any
  operator-ordered signal with it) whatever its position in the tree — the
  mechanical cause of silently dropped ordered signals at closeout. Existing
  nodes keep their seeded step copies; re-seed (`node init --reset`/`--steps`)
  to pick the new text up.
- Model pins are honored or the step fails loudly: the ambient
  `CLAUDE_CODE_SUBAGENT_MODEL` forcing var is unset at invocation compose (like
  the effort knobs) and removed from the seeded node settings — it silently
  rerouted every pinned fan-out sub-agent onto the session model once — and a
  model drop the one re-dispatch cannot resolve now books the step and iteration
  failed with the drop named, never a clean completion over wrong-model output
  (the node is never killed: the loop warns at the next iteration start and
  moves on). The iteration row records the model that actually served when its
  steps agree, so divergence from the pin is visible in the row and the
  `node list` detail column.
- Config edits take effect live at each key's natural boundary — pacing
  (`interval`/`sleep`) now re-reads at the next sleep call and `iter_timeout` at
  the next iteration, joining the already-live
  `max_iters`/`step_timeout`/`wait`/cost caps — and the per-key boundary table
  is documented, so a mid-run edit can never silently no-op.
- Seat-death backstop commits carry their context: the auto force-commit's
  subject names the step it follows (`auto after EXECUTE`) and its body the step
  and the newest plan's title — buried real work no longer costs archaeology at
  forensics and merge screens.
- fractal owns its estate staging: any estate file an ignore rule held out of a
  commit is re-evaluated against fractal-normal rules alone (the shipped exclude
  template plus committed per-directory `.gitignore` files) and force-added when
  only a machine-local layer — a foreign `info/exclude` line,
  `core.excludesFile` — held it, so one stray broad exclude can no longer
  silently unstage (or hard-fail) the records canon requires nodes to commit;
  the generated exclude block and the stage excludes also cover the legacy
  `registry.db` spelling.
- Radio listings are read-your-writes and watermarked: `messages`, `sent`,
  `feed`, `thread`, and `subs` resolve the acting node exactly like the writing
  verbs (loop-exported `_NODE` first, else the cwd; `--path` still selects
  another mailbox), so a delivered send is visible in its sender's own next
  outbox listing; `messages`/`sent`/`feed` close with an
  `as of <instant> (acting as <branch>)` freshness watermark on stderr — the
  recorded cut to quote when grading from a listing.
- `node stop`'s wait-for-the-seat contract is pinned by test and documented: a
  stop landing mid-step waits for the in-flight agent to complete — it never
  signals or tears the running seat (`node kill` remains the immediate path) —
  and the docs now state prominently that stop cascades over the target's entire
  subtree, children first.
- `node kill` lands on `idle` nodes: a booting spawn is reaped and a
  never-started spawn is stamped `killed` so it can never activate — an unwanted
  spawn no longer gets a head start while an operator poll-watches for its
  activation.

## [1.1.0] - 2026-07-28

### Added

- Cost and model accounting: every assistant row records its served model and
  running cost, `node list` shows each node's run spend, and the status
  qualifier sits in its own detail column.
- Model-drop enforcement: a step whose served model drops off the pinned model
  is re-dispatched once, and unresolved drops are marked in the detail column.
- Step session attribution: a radio message stamps the acting step's recorded
  session (via the loop-exported `STEP_ID`; own-node steps only, so sessions
  stay sender-owned), falling back to the node's woven session outside a step.
- `node merge --continue` finishes a hand-resolved squash merge and warns which
  files the continue resolved against the node; merges chain into delete and
  report each reaped worktree's size.
- TUI: chat sends queue while a turn is running and `ctrl+g` interrupts; the
  node/descendants log scope persists per branch; the radio composer clears
  subject and thread after a send.
- `node init --steps` seeds the step prompts from an explicit directory instead
  of the package seed.
- The README warns that nodes run their agents without permission prompts by
  default.

### Changed

- Radio identity: every verb that writes a row attributed to the acting node
  (`send`, `post`, `reply`, `react`, `unsend`, `save`, `unsave`, `sub`, `unsub`,
  `channel create`, `channel delete`) resolves the actor env-first — an explicit
  `--path` wins, else the loop-exported `_NODE` names the calling node, else the
  cwd's node acts; pure listings keep `--path` as a subject selector.
- Tree scoping: `reset`, `destroy`, and the tree-wide verbs (`pause`, `resume`,
  `track`, `untrack`, `open`) take the root branch as an optional argument and
  otherwise infer it from the caller's branch; `destroy --all` is the only
  repo-wide verb.
- Effort is flag-only: ambient `CLAUDE_EFFORT` / `CLAUDE_CODE_EFFORT_LEVEL`
  variables are unset when composing an agent invocation, so an operator shell's
  effort never overrides a step's pinned effort.
- Each seed directory hides behind its own ignore entry instead of the shared
  block, and codex's engine-materialized `skills/.system/` tree stays out of git
  and out of every work commit; `resume` refreshes the repo's exclude block so a
  stale worktree heals itself.
- Top-level nodes are directed to radio the user inbox to report out, and the
  fractal skill always asks about decomposition before running.
- Parent codex instructions files carry to spawned children.
- User-facing output renders em dashes.
- A `--max-cost` retune below the enforcement floor names the floor it sits
  under.
- The `plasma-fractal` dist and its `fractal` pointer declare themselves
  POSIX-only via an `Operating System :: POSIX` classifier: the config and
  worktree locks use `fcntl` file locking, and the node machinery drives tmux
  through POSIX shell scripts, so the package does not import on Windows.

### Fixed

- Drain-wait syncs render correctly in the TUI.
- List-valued config keys normalize their entries at the setter.

## [1.0.0] - 2026-07-21

Initial release of `plasma-fractal`, with the `fractal` pointer dist on PyPI
pinning it in lockstep.

[1.0.0]: https://github.com/plasma-ai/fractal/releases/tag/v1.0.0
[1.1.0]: https://github.com/plasma-ai/fractal/compare/v1.0.0...v1.1.0
[unreleased]: https://github.com/plasma-ai/fractal/compare/v1.1.0...HEAD
