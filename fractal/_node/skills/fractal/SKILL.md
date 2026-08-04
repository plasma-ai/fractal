---
name: fractal
description: Spawn and manage child nodes -- recursive autonomous agent loops.
---

# Fractal

A fractal is a tree of autonomous loops, each running in an isolated Git
worktree. You are one node; spawn a child to own a subtask that is well-defined,
separable, large enough for its own iteration cycle, and able to be run in
parallel without conflicting with your work. Pay careful attention to your
NODE.md instructions to guide you on how and when to spawn sub-nodes.

If `$MAX_DEPTH`, `$MAX_CHILDREN`, or `$MAX_DESCENDANTS` is `0`, you are a leaf
-- you cannot spawn, so this skill (spawning and managing children) does not
apply; skip it and focus on executing your task directly. Otherwise, **default
to spawning.** If a task has separable parts, decompose it into children rather
than doing it yourself -- the fractal's power is multiplicative parallelism --
don't waste it by tackling tasks one at a time.

Don't spawn what you can easily and reliably finish yourself. However, when a
child's task is itself complex, be a manager: give it resources and detailed
instructions on how to decompose further. Deep trees of focused nodes outperform
shallow trees of overloaded ones.

Run `fractal node --help` and `fractal node <command> --help` for the CLI.

## Limits (check before spawning)

| Var                | Meaning                                                                     |
| ------------------ | --------------------------------------------------------------------------- |
| `$MAX_DEPTH`       | Maximum nesting depth below this node. `-1` unlimited, `0` leaf.            |
| `$MAX_CHILDREN`    | Maximum direct children of this node. `-1` unlimited, `0` leaf.             |
| `$MAX_DESCENDANTS` | Maximum total descendants in this node's subtree. `-1` unlimited, `0` leaf. |

- **Width (`--max-children`).** Caps **direct** children only (not
  grandchildren). Enforced locally on the spawning node. A child may set a
  larger `--max-children` than its parent.
- **Depth (`--max-depth`) and descendants (`--max-descendants`).** Enforced
  across the **entire ancestor chain** -- `fractal node init` checks every
  ancestor's config and rejects the spawn if any limit would be exceeded. You do
  not need to split or decrement budgets when spawning; set whatever limits make
  sense for the child's subtask and let init enforce the ancestors' caps. A
  spawn that would breach any ancestor's cap fails fast with an error naming the
  offending node, so glance at `$MAX_DEPTH` and `$MAX_DESCENDANTS` before
  spawning to avoid wasted failed inits.
- **Re-entry re-checks.** Width and descendant caps count children in play --
  active, or idle awaiting start -- so a completed, stopped, exited, killed, or
  retired child frees its slot. `node start --continue` and a `node unretire`
  that lands `idle` put a node back in play and re-check both caps exactly like
  a spawn (refused over cap, no override); depth is structural, so only spawn
  checks it.

## Cost

A `--max-cost` (per-run USD ceiling -- runs are isolated, and a budget-ended run
refuses `node start --continue` without an explicit new `--max-cost`) is
strongly recommended for every child -- without one the child launches uncapped,
with a loud warning at start and bounded only by `--max-iters`/`--timeout`; a
non-positive `--max-cost` is rejected. Set it at init (`--max-cost`, \<= your
remaining when `$COST_BUDGET` is finite) and optionally `--max-iter-cost`.
Allocate conservatively; you may over-allocate across children optimistically,
but monitor (`fractal node cost spent`, `fractal node cost breakdown`) and kill
over-spenders. Caps are **soft** -- a child is not *hard*-stopped when it nears
`--max-cost`; once it drains into the reserve it gets cleanup guidance to wind
down the remainder of the current iteration cheaply, then the loop ends its run
at that iteration's boundary (the child runs `finish` itself only when its
requirements already verify -- the reserve guidance's goal-met exception) -- but
a single iteration or its subtree can still overshoot, so reining in an
over-spender is on you, the parent. Price a cap as solve + wind-down + reserve:
a clean finish costs a full iteration of close overhead on top of the last unit
of work (typically a few dollars per leaf child -- environment- and
model-specific), so a cap sized to the solve alone strands a *done* child
`exited` at the reserve boundary instead of `completed` unless it finishes
deliberately in the wind-down. Size `--max-iter-cost` to one wind-down pass as
well: the iteration cap, not the run-level cap, is what bounds each close
attempt. Iteration caps bind at step boundaries, never mid-step -- plan each
iteration to end under the cap, and read a trip as disclosure that an iteration
ran hot, not as a hard stop. If a done child strands anyway, prefer raise-cap +
continue + stop over a full re-finish -- the re-finish re-pays the whole close
severalfold and can still near-miss -- and treat `exited` with recorded DB
finish signals as a defensible terminal once the work is merged and verified.
Child-side corollary: `fractal node cost remaining` reports against
`--max-cost`, NOT against (`--max-cost` - reserve); a node inside the reserve
band reads positive remaining and plans an iteration the loop will never grant.
Plan your last productive iteration against (`--max-cost` - reserve), and if you
intend to signal `finish`, fire it BEFORE draining into the reserve -- wind-down
work scheduled for "next iteration" does not happen. In-step overshoot is
bounded for claude children (each step runs under a hard per-invocation budget
and stops cleanly at it), but the run-level cap stays soft. A child's spend
(including its sync) counts against your own budget. Some agents report cost
directly; others report token usage, priced from published rates -- so a cost
cap on a token-reporting child requires a (priced) `--model` (the run fails on
the first step otherwise). claude, grok, opencode, and omp report cost directly;
codex reports tokens. A claude child routed through openrouter is priced from
tokens too, so under a cap it also needs an explicit priced `--model`.

Every dollar figure you read has a scope; know it before comparing two.
Budget-guard figures, recorded run-end reasons, and `fractal node cost spent` /
`cost remaining` are all full-depth **per-run subtree** spend -- the run's own
steps plus every descendant run chained under it -- so a spawning node's guard
trips on money its children spent. `fractal node activity`'s `cost` column is
the row's **own-node** step cost only (run and iteration end rows sum just that
node's steps), so activity totals read *below* the guard figures on a spawning
node by design -- the gap is the children's spend, itemized by `cost breakdown`.
And every figure is per-run: runs are isolated, and no cost command reports a
lifetime rollup -- reconcile across runs by scoping each run
(`cost spent --run <id>`) and summing.

> [!WARNING]
> A **small `--max-cost` on a child running an expensive `--model`** is the
> combination most likely to blow the budget by a large *percentage*. The
> run-level cap is **soft** and only checked *between* steps, so one pricey step
> can be a big fraction of -- or exceed -- the child's whole budget before the
> next check. Give expensive-model children a budget large enough that a single
> step is a small slice, or hand a small budget to a cheaper `--model`. The
> sizing floor: never set `--max-cost` (or a remaining grant) within ~2x the
> model's single-turn cost -- a cap inside that band can be overshot by a large
> fraction in one turn, and that overshoot is documented, accepted behavior: no
> enforcement absorbs it.

For well-specified single-mission leaf work, a cheaper model at the same dollar
cap (e.g. `--model=sonnet`) is a first-class choice -- cost-per-point favors it
on numeric, single-task work -- but give it iter-cap headroom: micro caps bind
on step granularity, and a fast drafter can pack a full iteration into one large
step. Keep frontier models for manager, audit, long-horizon, and judgment-call
roles: a cheaper model can hold process hygiene and mechanical output while its
judgment quality collapses -- at no cost saving, since under a binding budget
pool a cheaper token rate buys more steps, not a lower bill.

## Spawn

1. **Init:** run `fractal node init --help` to see available options, then run
   `fractal node init <name> --path="$PROJECT_DIR" [...]`. `--agent` is optional
   (currently `claude`, `codex`, `grok`, `opencode`, or `omp`): when omitted,
   the child automatically inherits your agent -- pass it only to give a child a
   different agent. `--provider` is optional the same way (e.g. `openrouter`
   routes claude or codex through OpenRouter; it inherits, and agents without
   routes ignore it). `--inherit=<surfaces>` (comma-separated: `steps`,
   `scripts`, `skills`, `config`, or `all`) seeds those surfaces from YOUR node
   dir instead of the package seed -- steps/scripts/skills copy your live files,
   `config` snapshots preference keys (model, effort, sync, detached, iter/step
   timing, retries, pacing) and never budget caps; agent config always inherits.
   `<name>` uses letters, digits, and `_` only (no `-`). **All run parameters**
   (budget, depth/children, iters, timing) are set here and stored in the
   child's `config.json` -- editable before launch. Capture the output for the
   child's project/node dirs. Init is lockfile-serialized -- run calls
   sequentially. A worked example of spawn hygiene -- every cap, the model, and
   the step timeout explicit at init (leaf caps shown -- give a manager child
   depth/children/descendants; fill every placeholder deliberately -- price the
   `<usd>` caps per Cost above, and when the tree's NODE.md names exact model
   recipes, they override this generic guidance), then a registry verify before
   any configuration; the init call is wrapped to capture the child worktree
   path from the init output's `Initialized <path>` line -- an unset
   `$child_worktree` silently reads YOUR OWN config (`--path=` resolves to the
   current directory; `--path` on `config get` names the child's worktree, not
   the project root):

   ```bash
   child_worktree="$(fractal node init sub_a --path="$PROJECT_DIR" \
       --agent=<agent> --model=<explicit_model> \
       --max-cost=<usd> --max-iter-cost=<usd> \
       --max-iters=<n> --step-timeout=<duration> \
       --max-depth=0 --max-children=0 --max-descendants=0 \
       | sed -n 's/^Initialized //p')"
   fractal node list                                # registry row: sub_a present
   fractal node config get max_cost --path="$child_worktree"   # <usd> -- caps landed
   fractal node config get model --path="$child_worktree"      # <explicit_model>
   ```

2. **Configure:** edit the child's `NODE.md` (instructions + completion
   requirements), `steps/`, `setup.sh`/`test.sh`/`lint.sh`, and `skills/` --
   invest here, it's the highest-leverage work (you can still steer after
   launch; see Configure below).

   For a well-specified single-mission leaf, consider the **leaf step profile**:
   before the config commit, trim the child's `steps/` to PLAN + EXECUTE +
   COMMIT (delete the PREPARE and REVIEW files -- the loop runs whatever
   `steps/` contains, in digit-prefix order). Loop overhead dominates
   single-mission leaf cost, and a trimmed profile cuts it -- expect the
   overhead cut, not an outcome guarantee. Two caveats: PREPARE is where a node
   merges its parent and children -- keep it for any node that spawns, and for
   leaves whose upstream moves mid-run -- when you cannot rule that out at spawn
   time, keep PREPARE; REVIEW is where memory folds -- when you trim it,
   relocate its duties into a surviving step (e.g. append the fold to EXECUTE's
   tail) or, minimally, tell the child in its NODE.md to fold memory before
   COMMIT. Managers keep the full profile. When your own `steps/` already fits
   the child, skip the hand-trim and pass `--inherit=steps` at init.

3. **Commit the config** so the child starts from a committed baseline (a
   continue refuses over uncommitted project files -- commit them or pass
   `--clean` to discard them). Run the commit **from the child's worktree** -- a
   bare `commit` acts on the current directory's worktree (yours), not the
   child's (or pass `--path=<child worktree>`):

   ```bash
   cd <child worktree>  # .worktrees/<branch>
   fractal commit "configure <name>" --init
   ```

   Material you copy into a child's tree passes through the repo's commit hooks
   (if any) at this init commit (CRLF->LF, formatter reflow) -- so a byte-frozen
   store and its delivered copies diverge *by construction* on the first hooked
   commit; do not read that divergence as tamper. If you freeze reference bytes
   for audit, freeze the post-hook form (commit once, then re-pin) or compare
   normalized (line endings/whitespace) rather than raw.

4. **Launch:**

   ```bash
   fractal node start <branch>
   ```

   `start` takes no config arguments -- all run parameters come from
   `config.json` (set at init; adjust a value with
   `fractal node config set <key>=<value>`, read one with
   `fractal node config get <key>`, or edit the file directly). A configured
   `max_cost` must be positive; a missing `max_cost` launches uncapped with a
   loud warning. Add `--continue` only to continue a stopped/exited child.
   Starting is its own turn: when a spawn gate (child/descendant census, budget
   arithmetic) decides the launch, read it in one command and start in a
   separate one -- a chained start commits before you can see the read's output.
   The init gate re-checks census and budget at start, so treat a rejected start
   as the gate working; re-read before retrying.

### Configure

Node configuration is the highest-leverage work you do -- a well-configured node
runs autonomously for hours; a poorly configured one burns budget and creates
entropy. Invest real time here, and commit the baseline before launch -- a
continue refuses over uncommitted project files until they are committed or
discarded with `--clean`. You can still steer a running child by editing its
NODE.md, steps, or scripts (the loop re-reads them each iteration), but a strong
baseline is fundamental.

For complex tasks, configure nodes as a manager: provide sufficient resources
for them to spawn their own children, and write detailed NODE.md instructions to
direct decomposition.

- **NODE.md** (`<child_node_dir>/NODE.md`): The child reads this fresh each
  step, so it is both the initial brief and the live steering channel. Write
  clear instructions, completion requirements, and add any relevant constraints.
  You can edit it after launch to redirect the child mid-run.
- **Steps** (`steps/`): Don't change your own steps, but when configuring a
  child you may add or replace step files (the loop re-discovers them each
  iteration) to fit the task -- e.g. adversarial plan/review/critic steps,
  dedicated research or test/debugging steps, or multi-pass execution, etc. If
  sync is enabled (the default), it runs automatically before each step to
  handle radio communication. The first and last steps (PREPARE and COMMIT in
  the stock set) are structurally important to the lifecycle (merging parent
  changes, committing work) -- do not remove or fundamentally alter them (one
  scoped exception: the leaf step profile in Spawn step 2 drops PREPARE for a
  childless leaf whose upstream won't move mid-run). Middle steps can be freely
  renamed, added, or replaced. A step file may begin with YAML frontmatter:
  `agent: <command>` runs it on a different agent (each agent keeps its own
  woven session across the steps it runs), `provider: <route>` overrides the
  provider route (agents without routes ignore it), `model: <name>` overrides
  the model, `effort: <level>` overrides the reasoning effort,
  `timeout: <duration>` overrides the node-global `step_timeout` for that step
  alone, `detached: true` isolates that step in its own session within a
  continuous node, and `requires_approval: true` holds the loop after the step
  completes until you approve it (`fractal node pending`/`approve`). Size step
  timeouts where the step is defined -- give a slow test or research step its
  own `timeout:` ceiling rather than inflating the global -- and retune the
  global mid-run with `fractal node update --step-timeout` (it lands at the next
  iteration top). Set a generous global `step_timeout` too, as a resilience
  floor: without one a hung or stalled invocation (a stuck API call, a wedged
  tool) blocks the step and the whole node indefinitely, where a ceiling lets
  the loop abort and retry. Pass `--no-sync` at init to disable sync for
  lightweight leaf nodes.
- **Scripts** (`setup.sh`/`test.sh`/`lint.sh`): `setup.sh` runs every iteration
  (keep it idempotent) -- add dependency installs, env setup, or data seeding
  here. `lint.sh` is invoked by `fractal commit` and `test.sh` during EXECUTE;
  extend them to match the child's scope (e.g. narrower test paths, additional
  linters). A parent's tuned scripts propagate with `--inherit=scripts` at init.
- **Skills** (`skills/`): add or customize skill files to give the child
  domain-specific capabilities beyond the defaults.

The seed lives in the node data directory (`.fractal/`), which is **tracked by
git** and captured by your `fractal commit --init`; the same goes for the
project wiki (`wiki/`). Neither belongs in `.gitignore`. Fractal ignores its own
runtime artifacts (worktrees, the central database, status, agent logs) via the
repo-local `.git/info/exclude`, which it writes automatically.

### Meta nodes

A meta node configures another node's seed instead of doing project work
directly. Use `--meta=<target_branch>` at init to create one -- this sets
`--base` to the target's branch and `--scope` to its seed directory
(`.fractal/<target-branch>`), so the meta node can only edit the target's seed
files (NODE.md, steps, scripts, skills, etc.). `$META_MODE` is `true` when
running as a meta node, and `$META_TARGET` is the target node's branch.

Use a meta node when a child's configuration is complex enough to warrant its
own iteration cycle. The meta node studies the project and writes a high-quality
seed; once done, merge it and launch the target.

## Monitor and control

- **Status:** `fractal node list`, `fractal node status <branch>`. A node whose
  run ends on its `--max-cost` reports `exited` (exit 0 -- a designed landing,
  not a crash) -- a deliberate under-claim (a budget-*aborted* node must never
  read `completed`). The one exception: a node whose requirements verified and
  that ran a deliberate goal-met `finish` books `completed` even when the drain
  crossed the cap, with the overshoot recorded on the run row. So treat a capped
  node's `exited` as "inspect the work", not "failed": check its memory/plans
  and merge if the work is done. Verify a child's *executable* artifacts --
  checkers, scripts, anything you will run -- from a clean checkout of its
  committed bytes
  (`tmp=$(mktemp -d) && git archive <branch> | tar -xf - -C "$tmp"`), never
  in-place in a working tree: artifacts that depend on uncommitted or
  path-relative state pass in-place and fail everywhere else. (Textual claims
  need only `git show <branch>:<path>`.)
- **Stop:** `fractal node finish <branch>` (after iteration),
  `fractal node stop <branch>` (after step; waits for the in-flight step to
  complete), `fractal node kill <branch>` (immediately). All three cascade over
  the target's entire subtree, children first.
- **Pause:** `fractal node pause <branch>` freezes the child's subtree in place
  (aborts in-flight agent turns; loops park with their runs open),
  `fractal node resume <branch>` relaunches it exactly there -- same budgets,
  same iteration count. A paused child still holds its spawn slot and blocks
  your finish-drain, so resume or kill it before finishing. Only `resume`,
  `kill`, and `chat` act on a paused node; `start --continue` is for
  stopped/exited nodes (fresh run, restored worktree -- uncommitted project
  files need `--clean`, and a budget-ended run needs an explicit `--max-cost`),
  never for paused ones.
- **Clean up:** `fractal node merge <branch>`. A conflicted merge restores your
  worktree exactly as it was and leaves the resolution to you: redo the squash
  by hand (`git merge --squash <branch>`), resolve and stage the conflicts, then
  finish with `fractal node merge <branch> --continue` rather than committing
  yourself -- the continue runs the rest of the merge (seed strip, wiki index
  refresh, commit, merge-base advance) that a hand-rolled finish would miss. It
  names every file where your resolution kept your content over the child's --
  the child still carries its own version there, so land the resolution on the
  child (or retire it) or a later re-merge silently re-stages it. The continue's
  own failures leave your staged resolution in place to fix and re-run. Deleting
  after the merge (`fractal node delete <branch>`) is OPTIONAL hygiene, never
  automatic -- a merged node's branch and records keep audit value, so keep them
  unless clutter demands otherwise. Delete is destructive -- it force-removes
  the worktree and force-deletes the branch (and the whole subtree) regardless
  of merge state, discarding any unmerged work, so confirm the merge succeeded
  first.

## Continue mode

When `$CONTINUE_MODE` is `true`, decide per child whether to propagate by
assessing its memory/plans:

- **continue** (`fractal node start <branch> --continue`) if its work was in
  progress and still relevant -- uncommitted project files in its worktree
  refuse the launch, so commit them or add `--clean` to discard them;
- **reset** (`fractal node init <name> --path="$PROJECT_DIR" --reset`, then
  start) if the direction was wrong but the task stands -- `--reset` wipes the
  node to a **stock empty node** (memory, plans, steps, skills, config all
  cleared), so you must re-author its NODE.md, steps, and skills before
  starting;
- **delete** (`fractal node delete <branch>`) if the task is no longer needed;
  or
- **leave** (merge its work if `completed` and not yet merged).

## Radio

Every node has a radio (auto-initialized) for live inter-node messaging --
channels `public`, `private` (owner-only), `inbox` (others write), `outbox` (you
write); you auto-subscribe to the readable channels (`public` and `outbox`) of
your parent and each direct child. If sync is enabled, radio is checked before
every step, so messages and directives are picked up automatically. It is the
live coordination path (the project wiki only syncs at merge). Messages have a
priority (0-10, higher = more urgent). Run `fractal radio --help` (and
`fractal radio <command> --help`) to explore the CLI. See the `radio` skill for
messaging conventions.

Commands act on the current directory's node, so run them from your worktree --
you never pass a path for yourself. Radio verbs -- row-writing (send, post,
reply, react, unsend, save, unsave, sub, unsub, channel create/delete) and
listings alike -- act as the loop's exported node even from another directory,
so a send and the `sent` listing that checks it always agree (read-your-writes);
`--path` selects another mailbox. Name another node's branch positionally to act
on it (e.g. `fractal node status <branch>`); `--path` is only for running from
outside a worktree. `fractal node init` is the exception: `<name>` plus the
project root via `--path` (e.g. `$PROJECT_DIR`).
