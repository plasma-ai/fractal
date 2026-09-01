# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions
follow a `major.minor.patch` scheme: while the project is young, minor releases
may include breaking changes, each listed under a Breaking heading.

## [Unreleased]

### Breaking

- `node init --template=<path>[@<ref>]` replaces `--steps` and `--profile`, and
  the `.fractal/profiles/` location goes away with no successor: a template is
  any tracked folder holding `config.json`, so a steps-only template is such a
  folder holding `steps/`, and an ad hoc step set is committed on the spawning
  node's own branch before the spawn (its children fork from that tip). A
  template is read from git at the child's fork commit — seeding from an
  uncommitted directory is gone with the flags.

### Added

- `node start --headless` / `--tmux`: the loop runs in a detached process group
  instead of a tmux session, its output appended to the node's `headless.log`
  with one `=== Launched ... ===` banner per launch, so a tree runs on a host
  without tmux. The backend is sticky per node: the `.headless` marker records
  the backend the node last launched with, an unflagged relaunch (`--continue`
  or `resume`) reuses it, and on `start` the flags or the seat-exported
  `FRACTAL_HEADLESS` (exactly `true`/`false`, carrying the parent's backend into
  every delegated child start) force and re-record it. `attach` refuses on a
  headless node and names the log to follow. Liveness, crash healing, kill,
  teardown, and the relaunch guards judge a headless or bare loop by its
  recorded `.pgid` group and verify the group's identity through `ps` (a group
  owned by another user is arbitrated the same way); a tmux boot records its
  `.socket` server only after the server confirms it lists the node's own
  session, so a bare loop launched inside an unrelated pane is judged by its
  group, never healed against a server it does not live on; kill vets
  `.pgid`/`.step_pgid` under the `.worktrees` flock and refuses over a group
  whose identity `ps` cannot verify (naming the check to run) or a record still
  naming no pid — a launch's claim in flight — naming the record to clear; a
  fan-out kill retries a descendant refused over such a claim once it resolves;
  destroy/reset's blind-probe refusal binds to rows that could still hide a
  runtime (unsettled, or holding a `.pgid`/`.socket`/`.headless` record), so a
  settled, record-less node — the state their own reconcile leaves after healing
  a dead bare loop on a blind host — proceeds instead of refusing.
- `node init --template=<path>[@<ref>]`: a template is any tracked folder
  holding `config.json` — the preset-and-marker file — read from git at the
  child's fork commit (append `@<ref>` for another commit; one notice names the
  `@<root-branch>` form when the root branch's copy differs from the commit
  read), so uncommitted edits never deploy and the recorded version is exactly
  what a later `diff` or `reseed` re-renders. Its surfaces (`NODE.md`, `steps/`,
  `scripts/`, `skills/`, `agents/<agent>/` — the last deploying into the node's
  live agent config dirs, where a template file beats the parent's live copy)
  seed the node; a surface it lacks falls back to the inherit-or-package source,
  `--inherit` of a bundled surface is refused, and a bundled `steps/` must
  satisfy the loop's discovery contract at init. The `config.json` preset fills
  each unset run-config flag (a flag wins over the preset, the preset over an
  inherited value; only budget, limit, duration, model, and mode keys may
  appear, and the merged values pass the same validation the flags do).
  `--include`/`--exclude` (repeatable; mutually exclusive; a directory entry
  covers its subtree) cut the deploy to an effective set that `node diff` and
  `node reseed` judge by, so a trimmed spawn stays trimmed. A template refuses
  machinery paths (`.fractal`, `.git`, `.worktrees` components), symlinks (a
  template is self-contained), and non-UTF-8 files by name. The package seed's
  per-agent files ship under `fractal/_node/agents/`, the template layout's own
  `agents/` shape.
- `_template.toml`: the node's template provenance record — the
  worktree-relative template path, the commit actually read, the include/exclude
  listing, and the slot values — written into the node data directory at init;
  its presence marks a node as seeded from a template, it counts as a node
  record file (the estate content law commits it with the seed), and a `--reset`
  without `--template` drops it. Hand-editable, and validated wherever it is
  read.
- Seed-time slots: template files may carry `{{slot}}` placeholders — lowercase
  names, filled once at init from `--values <file.toml>` (a flat TOML table of
  string values), repeatable `--set KEY=VALUE` pairs that win over the sheet,
  and `--pin`, which fills the `{{pin}}` slot beside its fill-sheet-gate role. A
  slot with no value and any `{{` that is not a lowercase slot refuse init
  naming the file and the token; prompt-time `$VAR` text passes through
  untouched, so the two namespaces stay apart. The rendered charter passes the
  fill-sheet gate (authored sections present, `pin:` lines resolving and
  matching `--pin`, `docket:` rows resolving at the pin — anchored at the fork
  commit when the seed is pinless).
- `node diff`: shows a node's drift from its recorded template by re-rendering
  the recorded folder at its recorded commit with its recorded values and
  diffing the effective set against the live seed surfaces — `NODE.md`,
  `steps/`, `scripts/`, `skills/`, and each `agents/<agent>/` file against the
  live `.<agent>/` copy. A live symlink and a file the bundle does not carry are
  never judged; a bundle file the node lacks is drift, and unrendered `{{`
  residue in a live copy is its own finding. Exits 1 on drift, 0 clean, 2 on a
  command error, so scripts branch on the exit code.
- `node reseed`: rewrites a node's seed surfaces from its recorded template —
  files the node lacks are added, files it has are overwritten, nothing is
  deleted, and `NODE.md`, `config.json`, and `memory/` are never touched.
  `--ref` reads the recorded folder at another commit (a ref where the folder is
  absent refuses naming the re-point remedy); `--template <path>[@<ref>]`
  re-points the node, recording the new path and the commit read while the
  values and listing ride along unchanged. The verb refuses over an active or
  paused node without `--force` and always from the node's own worktree (a node
  may not edit its own seed), records a `reseed` event, and advances the
  recorded commit; a recorded listing entry the template no longer carries warns
  instead of refusing.
- Template credential guard: a template's `agents/` subtree refuses any dot-file
  and any credential-named file (`auth.json`, `credentials.json`, `*.key`,
  `*.pem`, `*.p12`, `*.pfx`, `id_rsa`, `id_ed25519`, matched case-blind) at
  every materialize — init, `node diff`, and `node reseed` alike — naming the
  file; credentials never deploy from a template — a node links its own at seed
  time.

### Fixed

- `node merge` advances the node's merge-base with the target's content: after
  the squash commit lands, the node's branch gains a two-parent commit
  (`merge <target> (post-squash)`, parents the node's HEAD and the target's
  HEAD) whose tree is the target's post-squash tree with the node's own seed and
  descendant seeds kept, and the node's worktree takes it — so the node
  converges to what the target adjudicated (a hunk resolved against it, a file
  dropped from the squash, a restore to base content) and a later merge in
  either direction never lands a stale copy of a file the node did not edit. The
  advance is skipped with a warning when the node's worktree is dirty, on
  another branch, or holds an untracked or ignored file or directory in the way
  of a path the target now tracks — a rename on the target that lands where the
  node keeps a private file, or an untracked case-only alias on a
  case-insensitive filesystem, included, while a path the node tracks under a
  different case is not a collision (the update renames it), and neither is a
  path the node tracks at, under, or above the hit (the target turned a file
  into a directory or back — a type change the update performs); move it aside,
  and the next merge that lands work advances it (a fresh merge offering nothing
  exits at "Nothing to merge" before the advance — unless the restore dropped a
  `.fractal/` change outside the node's own seed and its descendants', the paths
  the restore warnings name, or a `.fractal/` conflict outside them resolved to
  the target's content, either of which still advances the merge-base so the
  node converges and the warning does not repeat; a node whose only offering is
  an edit to its own seed exits without advancing, and its next work merge
  advances it), and a failed worktree update rolls the worktree back before
  skipping. Recovery for existing trees: a merge run on any earlier release
  (1.0.0 through 1.2.0), whenever the tree was created, records the node's
  merge-base commit without content, and the first merge of such a node after
  upgrading still squashes from that base and can land stale copies of files the
  node never edited. A poisoned node with unmerged work lands that work with a
  merge first. The stale copies ride the squash onto the target as reverts; the
  footprint refusal names only a scoped node's stale copies outside its scope
  roots (restore those paths from the base on the node's branch, commit with
  `fractal commit "<message>" --ignore-scope --path=<node worktree>`, and
  rerun), while stale copies inside its roots ride the squash exactly like the
  unscoped case — so in both cases review the squash commit and restore any
  reverted file from the target's history, or run the recipe below before the
  first post-upgrade merge. A node that merges real work needs no recipe: that
  landed work merge advances its merge-base, and the advance converges the node.
  A node with nothing new still offers its stale copies: merging it lands them
  on the target as a revert commit and then advances the node onto the reverted
  target, so do not merge such a node — run the recipe first, closing step
  included. A scoped node whose stale copies all fall outside its roots is
  refused by the footprint check and never advances, so it needs the recipe too.
  The recipe adopts the base's tree in the node's clean worktree, discarding any
  unmerged work on the branch. Its checkout writes the base's copy over any
  untracked or ignored file in the node's worktree at a path the base tracks, so
  first check `git -C <node worktree> status --ignored --porcelain` and move
  such files aside. The steps:
  `git checkout <base> -- . ':(exclude,glob)**/.fractal/**'`, `git rm` any path
  `git diff --name-only <base> -- . ':(exclude,glob)**/.fractal/**' ':(exclude).gitattributes'`
  still lists, then, if `.gitattributes` lacks `**/_index.md merge=wiki`, append
  init's two lines (`# Wiki index merge driver`, `**/_index.md merge=wiki`),
  commit (the `.fractal/` exclude keeps the node's seed, at any depth, in place;
  the checkout adopts the base's `.gitattributes`, so only the `git rm` step
  excludes it), then, before the base moves again, run
  `git -C <node worktree> merge -s ours --no-edit <base>` — the node's tree
  already equals the base's, so recording the base's tip as a parent gives a
  correct merge-base with correct content (the step records that tip only when
  the base has moved since the poisoned merge; otherwise git reports
  `Already up to date`, and the adopt commit alone fixes the node). A plain
  `git merge <base>` does not fix this — the stale copy is the only changed
  side, so it wins without a conflict.
- Nothing under `.fractal/` rides a squash onto the target except a scope root
  of the merging node that is, or lies under, a `.fractal/` directory (a
  `--meta` node's scope is the target's seed directory, its work product): every
  other `.fractal/` path at any depth returns to the target's HEAD — a child's
  edit to the target's estate, a foreign node's seed, a sub-project descendant's
  seed under `<project>/.fractal/`, a stray file at the `.fractal/` root — with
  two warnings naming what the restore dropped: paths the target tracks are
  restored to its content, paths it does not track are removed, and the node's
  copy survives only in its branch history once the merge-base advance brings
  the target's tree into the node's worktree
  (`git -C <node worktree> log --full-history -- <path>` lists the advance that
  dropped the path first and the node's own commit below it;
  `git show <commit>:<path>` on that lower commit, or
  `git show <advance>^:<path>`, reads the copy; a plain `git log` follows the
  target's side and lists nothing). The squash therefore never adds the node's
  own seed or its descendants' seeds; on the user node's branch a copy the
  branch already tracks is stripped as well (a leak there), while a node target
  keeps a copy its PREPARE `git merge --no-ff` of the child tracks until it
  merges upward itself, so a child whose advance was skipped never inherits a
  deletion of its own live seed on its next merge of the parent. A fresh merge
  refuses before the squash when it would write over any file that exists
  untracked on the target's disk — an ignored private file such as `local.env`,
  or the user node's own live seed, self-ignored on the root but committable
  from a child — judged over every path the node added or changed since the
  merge-base that the target's HEAD does not track, a file sitting where the
  squash would create a directory included ("would overwrite untracked files in
  `<target>`'s worktree"), naming the files to move aside or drop from the
  branch; a tracked file the node replaced with a directory is the squash's own
  type change, not a collision, and a `--continue` cannot prevent that
  overwrite, since the hand squash has already done it.
- `node merge` runs one squash at a time per repository: a repo-wide merge lock
  (`.worktrees/.merge.lock`, removed by `fractal destroy` with the rest of the
  plumbing) queues two sibling merges into one target instead of letting them
  interleave their index writes and leave the target half-merged, and an
  interrupt (SIGINT) delivered to the `fractal node merge` process is forwarded
  to the merge script: before the squash commit its restore trap resets the
  target and marks the merge event failed instead of leaving the squash staged
  (an interrupt that lands while the event is still being opened closes it as
  failed too); once `git commit` has moved the target's ref the squash has
  landed, and the merge finishes it — the merge-base advances, the event closes
  as completed, and the command prints `Squash-merged ...` and exits 0 — rather
  than reporting a restore. An interrupt during the advance finishes the node's
  worktree update or rolls it back, warning only when an advance was underway;
  one during a no-op merge's bookkeeping prints that arm's own summary
  (`Nothing to merge: ...`) with no advance warning. The "restored" verdict is
  judged by the target's state, not by git's exit code, and a squash that fails
  after git wrote the index (a stale or unwritable `SQUASH_MSG`) is reset and
  its markers cleared, reported as failed after staging.
- `fractal init` warns when an adopted `wiki/_index.md` carries no frontmatter
  stamps, naming the `wiki update --path=wiki` and commit to run before
  initializing nodes, instead of letting sibling nodes conflict on its
  `created:` line when they merge. It also warns when `.gitattributes` lacks the
  `**/_index.md merge=wiki` line, naming the append and commit to run first,
  since git reads the attribute from the target's own tree and an adopted wiki
  without it conflicts on its index at the first merge where both sides changed
  the index.
- `node init` into a fresh worktree whose fork point already carries any file
  under `.fractal/<branch>/` (a whole or partial copy of an earlier node of the
  same name, PREPARE-merged or leaked) warns, removes the directory whole, and
  seeds it afresh, so the init's flags hold and a leaked `NODE.md` alone never
  becomes the charter.
- `node init --path <dir>` refuses a `<dir>` below a node worktree's root,
  naming `--path <repo root>/<project>` as the form to use, since under
  `.worktrees/` the path stands for the parent node and a sub-project below it
  cannot be carried.
- `node delete` prints the unmerged-work warning before the confirmation prompt
  (and before the teardown under `--force`), and
  `Deleted branch: <branch> (was <sha>)` names the discarded tip.
- `node init --meta` spells the scope as the target's seed directory relative to
  the meta node's own project, so a meta node for a sub-project target
  initialized from that target's worktree (or from the repo root) commits and
  merges within its scope; one initialized from a different sub-project is
  refused at init, naming both projects.
- `node delete` counts a `--meta` node's edits to its target's seed directory as
  unmerged work: a scope root that is, or lies under, a `.fractal/` directory is
  judged like any other path (only the node's own seed and its descendants' are
  waived), so an unlanded edit to the target's contract warns before the prompt
  and on the teardown instead of being discarded silently.
- `node delete` prints the unmerged-work warning for every live descendant
  before the confirmation prompt (and before the teardown under `--force`), each
  judged against the deleted node's surviving merge target, and each warning
  reads once.
- `node init --meta` refuses when the derived scope root would contain
  whitespace (a target in a sub-project directory with a space), naming the root
  and the remedies, since the scope list splits on whitespace and would store
  the root as two; nothing is created.
- `node retire` refuses an already-retired node
  (`Cannot retire: node is already retired.`), so the recorded pre-retire status
  stays the real one and `unretire` restores it exactly.

### Changed

- `node merge` holds the squash to the node's commit scope: before committing,
  the staged paths outside `.fractal/` are judged by the node's scope roots and
  its project wiki (a repo-root node without a scope is unrestricted; a
  sub-project node without one is bounded to its project directory) — the same
  law `fractal commit` applies — and a path outside them refuses the merge. The
  worktree-root `.gitattributes` is admitted, at merge and at `fractal commit`
  alike, only when the whole change is init's own edit — HEAD's content followed
  by exactly the two lines the wiki tool appends (`# Wiki index merge driver`,
  `**/_index.md merge=wiki`); any other added or removed line makes it an
  ordinary out-of-scope path. The refusal names the paths and both remedies:
  widen the scope with
  `fractal node config set scope=<dirs> --path=<node worktree>` and commit it
  with `fractal commit "widen scope" --path=<node worktree>` (an uncommitted
  config change makes the rerun skip the merge-base advance), or rerun with
  `node merge --ignore-scope`, which lands the paths. A fresh merge restores the
  target on refusal; a `--continue` leaves the staged squash in place on every
  refusal, and its footprint remedies are `--continue --ignore-scope` or
  widening the scope and redoing the squash, since the widening commit lands
  after the hand squash. `--continue` also refuses unstaged tracked changes in
  the target (a hand-resolved squash must be fully staged: save any copy you
  need, stage the paths that belong to the resolution, and discard the rest with
  `git -C <target worktree> checkout -- <path>` — the merge restores every
  `.fractal/` path to the target's HEAD) and a node commit newer than the hand
  squash (an iteration or a nested child's merge landed after the operator's
  `git merge --squash`; the refusal names the redo,
  `git -C <target worktree> reset --hard HEAD && git -C <target worktree> merge --squash <branch>`).
- Leaked seeds on the user node are named and, where the merge owns them,
  cleared: before a squash into the user node, `node merge` judges the root's
  committed tree for seed directories of the root's own dotted nodes
  (`.fractal/<target>.*/`, so a `--base` merge into another tree's root judges
  that root) — the root owns no seed, so every one is a leak. Exactly what the
  strip removes — the merging node's seed at its own project prefix and its
  descendants' seeds at any depth — is named in one warning
  (`tracks seeds of <branch> or its descendants, leaked by an earlier merge: <dirs>; this merge removes them`);
  the rest, a same-named copy of the node's seed under another project prefix
  (the node re-created at a different project path) included, get a second
  warning with a
  `git -C <target worktree> rm -r -- <dirs> && git -C <target worktree> commit -m 'drop leaked node seeds'`
  remedy line that removes them from the tree and from the root worktree's disk
  (the copies are never live seeds, which sit in each node's own worktree; the
  check reads the committed tree, so a `--continue` never reports the
  hand-staged seed). A node target is not judged, since its branch legitimately
  carries other nodes' seeds (its ancestors' by fork, its descendants' by
  PREPARE merges, a sibling's by the merge-base advance). Whether the target is
  the user node is read from the repo's record of the target branch (so a root
  checked out in a linked worktree is still stripped and leak-checked), and a
  direct `merge.sh` call that cannot read the target's node config warns
  `could not read <target>'s node config; treating it as a node target` instead
  of judging it silently. A fresh squash whose only conflicts sit under
  `.fractal/` outside the node's scope roots resolves to the target's content (a
  path the target tracks returns to its content, a path it lacks is removed; on
  the user node the node's own seed is then stripped, while a node target keeps
  the copy it tracks) and continues with a warning naming the paths, while any
  other conflict fails and restores the target.

## [1.2.0] - 2026-08-24

### Breaking

- Every command reserves exit 1 for its own nonzero outcome — a dirty
  `commit --check`: a command error (an unresolvable node, a refused lifecycle
  signal, a rejected commit message) exits 2 with an `Error:` line on stderr,
  beside typer's usage errors — a script gating on exit codes must branch
  accordingly, never reading a failed run as a command's own nonzero outcome.

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
  takes a repeated `--node` (every recipient validated before any copy lands),
  and every `--node` send prints one `<uuid> <node>` receipt per recipient on
  stdout — one contract whatever the roster's length, while bare and `--parent`
  sends keep the bare UUID. `--relay-of <uuid>` marks a copy as the relay of an
  order, and the new `radio relays <uuid>` lists every recorded relay — the
  check that a descendant-relay obligation actually executed — keyed on the
  recorded marks alone, so a withdrawn original (`unsend` deletes the original,
  never the copies) stays auditable.
- `node list --json`: a JSON array of typed row objects (mutually exclusive with
  `--csv`), completing the machine-readable trio with `node activity --json` and
  the radio listings' `--json` — operator instruments no longer need comma-split
  CSV scraping that an odd title can corrupt.
- Unmistakable failure frames: every failed `fractal` command closes with a
  `FAILED (exit N)` line as the LAST line of output (bold red on a tty, bare
  text in pipes), so an error frame read through `tail -1` can never pass as
  success; unknown options keep the usage line naming the correct invocation.
- `node list` gains a typed `end_reason` column (riding `--json` and CSV alike):
  a closed vocabulary naming a settled row's landing — `goal_met`,
  `run_exhausted`, or `final_iteration_failed` on a completed row;
  `cost_budget`, `timeout`, `setup_abort`, `final_iteration_failed`, or `other`
  (recorded but unmapped) on an exited one — derived from the run row's typed
  facts and the loop's own recorded reason strings, null when nothing is
  recorded (a reconcile-healed crash) and on every other status, so machine
  consumers stop literal-matching the `detail` prose to tell landings apart.

### Fixed

- A credential parked in a node's estate no longer rides a commit silently.
  Containment was inverted against risk: the record allowlist bounded only the
  force-add that overrides a host's ignore rules, so the same dotenv, private
  key, or downloaded token was loudly refused where a host rule already fenced
  it — and swept into history by the plain add wherever nothing did, which is
  the default state of a fresh clone. One content law now bounds both paths, so
  what a node's own directory folds into history never turns on whether an
  ignore rule happens to cover it, and a refusal is named in the commit output
  either way rather than inferred from an absent file. Because the law must hold
  for a normal node, it describes everything an estate legitimately holds — the
  canon-required records at their text suffixes, plus the estate's own tool
  state: git's empty-directory placeholder (`plans/.gitkeep`) and the memory
  wiki's settings directory (`memory/.wiki/`), whose declared-root marker the
  wiki CLI reads the memory back through. Both already committed and still do;
  no file class is newly admitted to history, and the classes now withheld are
  exactly those outside the law (keys, certificates, archives, binaries, and
  anything a node parks at the estate root). The clean check learns the same law
  for untracked estate paths, for the reason it already rides the stage's own
  excludes: content no pass may ever stage would otherwise read as permanently
  dirty and fire the loop's force-commit net every iteration over a file that
  can never clear.
- A draining seat can no longer spawn its way out by moving. The drain's
  spawn/re-arm refusals resolved the acting node from `_NODE`, else the working
  directory — both the seat's to rewrite — so
  `env -u _DRAIN -u _NODE -C <sibling worktree> fractal node init ...` resolved
  to a real but *wrong* node the drain never binds, and the same command from
  outside every worktree resolved to no node at all and failed open. Two nodes
  were spawned from a live drain that way. The guard now also asks the operating
  system: the loop makes each agent invocation its own process group leader and
  records the id, so the tree can ask which of its open draining runs owns the
  calling process — attribution no `env -u` or `cd` rewrites. An operator's own
  shell is in another group and acts normally, as before.
- A child in its boot window no longer escapes a `stop`/`finish` cascade
  permanently. Both verbs made a single pass over the descendants live at that
  instant, so a child still `idle` for the moment between `node start` returning
  and its loop's `active` stamp got no signal row at all — while the operator's
  command reported success. It then ran on unattended after its manager settled,
  and under `finish` blocked the manager's drain-wait until its own `max_iters`
  ran out. The sweeps now re-enumerate to a fixpoint (as `kill` and `pause`
  already did), and a loop that boots while an ancestor still carries a pending
  stop or finish adopts that signal onto its own run and honors it at the first
  boundary — the graceful-signal twin of the pause latch a booting loop parks
  itself against.
- A read-only census no longer SIGKILLs a healthy loop that has no tmux session.
  `fractal node _loop` is a supported bare entry point (`start.sh` execs it, and
  a tmux-less host has no other), but crash reconciliation read the tmux probe's
  "no such session" as proof the loop had died — so any command reaching
  `Node.status_detail`, `fractal node list` included, stamped the node `exited`
  and TERM/KILLed the running loop's whole process group, in-flight agent and
  all, leaving one `orphan` event as the only trace. A bare launch records no
  socket, so that answer is about a server the loop never joined; its own
  recorded process group now overrules the probe.
- A mailbox selector no longer selects who is reading.
  `radio messages --json --body`, `radio messages --saved`,
  `radio feed --saved`, and `radio thread` all emit message bodies, but resolved
  their acting node from `--path` — so the owner-only rule `radio read` enforces
  was decorative for anyone who could pass the flag, and the same bodies rode
  out through a listing with no read receipt. Worse, it broke the mailbox seal
  outright: a sealed seat that unset `_NODE` and stepped into a sibling worktree
  resolved to a real but wrong actor the seal never binds, and got every held
  message and the pre-seal archive. Those four surfaces now resolve the reader
  the way `read` does (never from `--path`) and refuse a foreign mailbox's
  read-only channels and its archive; a caller with no resolvable identity is
  refused too, instead of failing open. Plain metadata listings are unchanged —
  they name rows, not contents, and acting as another node through `--path` is
  the operator surface they exist for.
- The estate-record force-add survives a record it cannot stage: git stages
  nothing when any one path in a batched `git add -f` is dead or unindexable
  (exit 128), so a single vanished or permission-dead estate file aborted the
  whole commit — the `--force` backstop save included, leaving the node unable
  to commit at all until a human cleared the path. The pass now retries per
  path, stages every record it can, and names the ones it could not.
- A force commit's body folds in the record pass's notices — the force-staged
  list and the non-record refusals — beside the staged-sweep warnings: the
  console notice dies with the run's pane, so the one durable record that a
  backstop save overrode a host ignore layer (and that credential-shaped files
  sat in the estate) is now in git history.
- The "skipped by ignore rules" alarm covers user lines in `.git/info/exclude`:
  the suppression keys on fractal's managed block by its line span, never the
  whole file, so a foreign pattern in the file's user territory no longer eats a
  deliverable in silence.
- The force-add notice names worktree-relative paths, matching the refusal
  notice beside it — absolute machine-local paths no longer print, nor land in a
  force commit's body.
- An empty `--node` value refuses instead of resolving to self: `--node "$PEER"`
  with an unset variable landed an urgent fleet order in the sender's own inbox
  under a clean exit 0.
- The fill-sheet pin gate reads every `pin:` spelling git accepts: uppercase hex
  and short (4+ hex) abbreviations validate and gate against `--pin` case-blind,
  and a spelling the gate cannot read as a sha (symbolic, or below the four-hex
  floor) refuses outright — any such line was silently invisible to the gate,
  deploying unvalidated and anchoring the charter's docket rows at HEAD instead
  of the declared commission pin.
- A profile-seeded node's init banner says the seeded task is ready to review
  and start, instead of instructing the operator to author the charter the
  profile already authored (following that instruction invited overwriting a
  pinned commission).
- `node list --csv --count` refuses like `--json --count` already did: `--count`
  silently won and handed a CSV consumer a bare number — a shape it never asked
  for, with no error.
- Seal enforcement closes its archive and environment bypasses: a sealed seat
  can no longer `radio save` a hosted message and read the body back through
  `messages --saved` (the archive is a body surface; pre-seal archives are held
  too), and the seal resolves its actor env-first *then* by the working
  directory, so scrubbing `_NODE` inside the seat's own worktree no longer lifts
  it.
- Drain enforcement is durable, not advisory: `--continue --drain` records the
  drain on the run itself, so the spawn/re-arm refusals survive an environment
  scrub and a pause/resume of the drain run (a resumed drain silently became an
  ordinary run before).
- The resumed-iteration inbox digest renders sender-controlled headers as
  quoted, single-line, length-bounded data with block-opening markup stripped,
  under a banner naming it untrusted and non-authoritative — a crafted subject
  can no longer read as an instruction in another node's context.
- The estate force-add is bounded to the node-record allowlist (known record
  dirs and files at text suffixes, no dotfiles): the pass overrides the ignore
  layer a host fences secrets in, so a parked `.env`, key, or archive is refused
  by name instead of staged, and every force-add is reported on the commit
  output.
- Every alarm this wave added is now proved in both directions — the billing
  breaker's non-arming guards (exit 127, slow, and paid failures), the loop-side
  iteration-gap alarm, the census billing detector against the step sequences a
  real run books (never-run tails and open rows are bookkeeping, a paid failure
  breaks the streak), the seal's hold on the resumed-iteration digest and on
  threads, served-model recording when divergence beats the seeded pin, the
  `--drain` launch wiring, the killed-before-boot stand-down, the live
  `iter_timeout` re-read, `send_many`'s all-or-nothing dry pass, the baseline
  force-add under a hostile external ignore, and `--pin` without a profile.
- An iteration whose step sequence a budget ceiling cut short books `stopped`,
  not `completed`: a gate interrupt could book a goal-met lap whose steps all
  read `stopped` and whose launches never happened.
- The registry database's SQLite sidecars (`registry.db-wal`/`-shm`/ `-journal`)
  are excluded from every stage, so a mid-write snapshot can never be committed
  beside an excluded main file.
- A live retune the loop cannot honor (an `interval` under the live
  `iter_timeout`) warns once per distinct value instead of once per iteration,
  so the rejection no longer buries every later warning.
- The force-commit backstop survives a non-UTF-8 plan file: the plan title is a
  cosmetic context line and now degrades instead of raising out of the save that
  exists to rescue the work.
- `radio relays` refuses an unknown UUID instead of answering "0 relays
  recorded" — an empty listing means the relay never happened, so a typo'd UUID
  would indict a node that relayed faithfully.
- Fan-out receipts print as each copy lands, so a mid-fan-out failure no longer
  discards the record of deliveries already made (silence there invites a
  re-send that double-delivers).
- `node list --json --count` refuses instead of silently printing the bare count
  a JSON consumer never asked for.
- The estate-record force-add stages what still exists: a held file deleted
  between the pass's `ls-files` snapshot and its `git add -f`
  (ignored-and-untracked estate files are exactly what a user's `git clean -X`
  removes, and estates churn under the node's own housekeeping) no longer fails
  the add and aborts the whole commit after the scope sweep already staged the
  iteration's real work.
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
- A deliberate finish whose final iteration died no longer lands byte-identical
  to a clean one: the run row records `final iteration failed` and the census
  detail column names it, the same honesty the max-iters leg already enforced —
  the dead tail was visible only in `node activity` before.
- Killing an idle node is quiet: a never-started spawn has no run for the
  run-scoped kill signal to hang off, so the write is skipped instead of warning
  `no runs found; signal not set` on every successful reap (the kill event still
  carries the attribution).
- A sealed seat can no longer unseal itself: `config set sealed=false` refuses
  from inside the node the seal binds, so the one call that would hand the seat
  every held message — and render every other seal guard decorative — is now the
  operator's or the parent's alone.
- The seal covers the verbs that curate or adjudicate a held row, not only the
  ones that read it: `radio unsave` no longer lets a sealed seat destroy an
  archive it cannot read (the seal protects the archive's integrity, not just
  its confidentiality), and `radio react`/`radio reply` refuse over a hosted
  message instead of moving counts the adjudicator will later read and, in
  reply's case, disclosing the held message's sender in the confirmation.
- The drain's re-arm refusal reaches the loop entry the guarded verbs front:
  `node _loop` is what `start.sh` execs, so a draining seat could re-arm any
  node in the tree by calling it directly — the front doors locked over an open
  back one, and the re-armed run was invisible to the one-loop-per-node
  invariant. The drain run's own relaunch after a park stays exempt.
- `fractal init` refuses under a drain like every other creation verb: the
  user-node branch returned before the guard, so a draining seat could stand up
  a whole new tree — its own database, radio, and a root to spawn from.
- The census `PAUSED: billing` mirror also excludes the other cannot-exec shape:
  an agent or wrapper that runs and exits 127 books `agent error (exit 127)`,
  and only the spawn-level `agent launch failed` was disqualifying the streak —
  so a broken install had the census screaming credit outage at an operator
  while the loop burned launches at full speed, the exact misdirection the guard
  exists to prevent.
- `--since` refuses anything that is not an ISO 8601 date or timestamp on every
  listing that takes it (`radio messages`, `sent`, `feed`, `--saved`). The value
  went straight into a lexicographic comparison, so a mistyped or wrongly
  formatted cut (a US-style date, a Unix epoch) either emptied the whole mailbox
  under an affirmative `0 unread (0 total)` or filtered nothing at all while
  looking like it had.
- A listing filter that could only ever be empty refuses instead of narrating a
  false record: `radio messages --channel` over a channel the mailbox does not
  host, and `radio feed --node`/`--channel` over an unregistered node or a
  channel held by no subscription. The write side already refused the identical
  typo loudly; a real-but-empty channel still lists quietly.
- The billing breaker gates every agent launch, not just the work step: the
  before-step SYNC fired ahead of the gate, so an armed breaker on a sync-mode
  node (the shipped default) still bought one hot invocation per gated iteration
  — one per hour even at the backoff cap. SYNC outcomes now arm and clear the
  streak too, so an outage trips the breaker after three dead launches instead
  of six.
- The wiki tool's self-ignored derived cache (`<wiki>/.wiki/cache/`) stays out
  of git history on every commit path. The baseline's force-add overrode the
  cache's own ignore, and the cache embeds per-page mtimes that every
  `wiki update` rewrites — so once tracked it churned in every node commit,
  sibling copies never byte-matched (a guaranteed merge conflict on otherwise
  disjoint wiki work), and a re-merge whose only surviving offering was that
  churn died at the squash commit as a false hard failure. The stage excludes
  now bar the cache from the baseline and every work commit alike, a work commit
  that finds the cache tracked drops it from the index (the tool keeps the
  on-disk copy), and the merge re-checks the staged squash after the parent's
  wiki refresh — a squash the refresh fully reverts lands on the designed
  "Nothing to merge" no-op instead of dying on the empty index.

### Changed

- `node list`'s documented schema matches what it prints: the `detail` and
  `spend` columns are listed, and the `detail` vocabulary is enumerated (pending
  signals, exit reasons, `run exhausted:`, `orphaned`, `model drop`,
  `iteration gap`, `PAUSED: billing`).

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
  `registry.db` spelling and its SQLite sidecars (`registry.db-*`), mirroring
  the modern `.db`/`.db-*` pair — a hot WAL or journal swept into a commit is a
  torn byte capture of a database another process is mid-write on.

- Radio listings are read-your-writes and watermarked: `messages`, `sent`,
  `feed`, `thread`, and `subs` resolve the acting node exactly like the writing
  verbs (loop-exported `_NODE` first, else the cwd; `--path` still selects
  another mailbox), so a delivered send is visible in its sender's own next
  outbox listing; every listing closes with an
  `as of <instant> (acting as <branch>)` freshness watermark on stderr — the
  recorded cut to quote when grading from a listing, read before the query so a
  row a concurrent sender lands mid-render is never endorsed as absent.

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
[1.2.0]: https://github.com/plasma-ai/fractal/compare/v1.1.0...v1.2.0
[unreleased]: https://github.com/plasma-ai/fractal/compare/v1.2.0...HEAD
