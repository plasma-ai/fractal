---
name: fractal
description: Hierarchical agent loops with recursive self-organization.
argument-hint: '[directive]'
disable-model-invocation: true
---

# Fractal

A fractal is a tree of autonomous agent loops, each in its own git worktree. A
node iterates toward a goal and can spawn child nodes that work subtasks in
parallel.

This skill configures a node with the user, then launches it in tmux; from there
it runs autonomously — iterating, committing, and spawning children as needed.

Your role does not end at launch. The user (root) node has no loop of its own —
**you are it.** Once a node is running you are its *operator*: you watch the
tree, steer it, and relay between it and the user on their behalf. See
**Operator** below.

## Directive

`$ARGUMENTS` is a natural-language directive describing what this fractal should
do. Interpret it: distill the node's goal, and map anything the user pinned down
(a name, a budget, a model, limits, ...) onto the parameters below, each of
which becomes the matching `fractal node init` flag. The `/fractal` skill routes
all the configuration to `fractal node init` and passes only
`--continue`/`--clean` (when the directive asks to continue an existing node) to
`fractal node start` — plus `--max-cost` when it accompanies a continue (a
continue re-arms the cap at start, not init).

**Parameters** — all configuration. Set by `fractal node init`, written to
`config.json`, and editable there before launch:

- **`name`**: node name (required; letters, digits, and `_` only — no `-`)
- **`path`**: project root, repo root or monorepo sub-project (default: `.`)
- **`title`**: human-readable display name (default: de-slugged node name)
- **`scope`**: restrict commits to subdirectories within the worktree
  (comma-separated, e.g. `parent/child,tests`)
- **`base`**: branch to start from (default: current branch)
- **`meta`**: target node branch for meta-configuration
- **`inherit`**: seed surfaces from the parent node instead of the package seed
  (comma-separated: `steps`, `scripts`, `skills`, `config`, or `all`); agent
  config always inherits. A top-level spawn's parent is the user node, which
  carries no steps, scripts, or skills — the parameter is for configured nodes
  spawning children
- **`agent`**: agent command; inherits the user node's default when omitted
- **`provider`**: provider route for the agent (e.g. `openrouter`); inherits the
  user node's default when omitted
- **`model`**: model override; when omitted, the agent uses its own default
  model
- **`effort`**: reasoning-effort override; when omitted, each agent seed's own
  pinned level applies, not the vendor default
- **`max-iters`**: per-run iteration cap
- **`max-depth`**: maximum child node nesting depth
- **`max-children`**: maximum direct child nodes
- **`max-descendants`**: maximum total descendant nodes
- **`timeout`**: per-run time limit (e.g. `30m`, `1.5h`)
- **`iter-timeout`**: per-iteration time limit (e.g. `30m`, `1.5h`)
- **`step-timeout`**: per-step time limit (e.g. `30s`, `10m`); caps each step
- **`interval`**: fixed iteration schedule (e.g. `1h`)
- **`sleep`**: delay between iterations (e.g. `10s`)
- **`wait`**: sleep between approval-wait sync invocations (default: `1m`)
- **`max-cost`**: cost ceiling in USD per run — runs are isolated, so each
  launch arms the cap anew; after a budget-ended run, `node start --continue`
  refuses without an explicit `--max-cost`
- **`max-iter-cost`**: per-iteration cost ceiling in USD
- **`max-step-cost`**: per-step cost ceiling in USD (warn-only when
  unenforceable)
- **`reserve-budget`**: budget reserved for cleanup; USD or N% of `max-cost`
  (default: 10%)
- **`sync`**: enable (default) or disable radio sync before each step
- **`detached`**: run each step as a separate agent session (default: one
  continuous session)
- **`local`**: skip pushing to remote after each commit

**Start** — `fractal node start` just launches; all run parameters come from
`config.json`. A `max_cost` in `config.json` must be positive if set; a missing
`max_cost` launches uncapped with a loud warning. Its only arguments:

- **`--continue`**: continue a stopped/exited node — the launch restores the
  worktree, so uncommitted project files refuse without `--clean`
- **`--clean`**: with `--continue`, discard uncommitted project files
- **`--max-cost`**: with `--continue`, re-arm the cost cap for the new run;
  required when the last run ended on its budget

After reading the directive, print, in this order:

1. **Suggested NODE.md instructions** — a draft `## Instructions` section
   distilled from the directive; skip it when no goal can be inferred.
2. **Suggested NODE.md completion requirement** — a draft
   `## Completion Requirements` section with concrete, verifiable conditions;
   skip it when none can be inferred (for open-ended work with no natural
   stopping point, note instead that the section stays empty and `max-iters`
   should cap the run).
3. **Interpreted parameters** — always: a table with one row per **Parameters**
   entry above, in order, and the value read from the directive; leave the value
   empty where the directive said nothing (the defaults apply).

Close with what could not be inferred — the last thing you say. If **name** or
**path** is missing, ask: what should this fractal be named, and where should it
live? Assume the current directory for **path** when it is a git repo that looks
like the project in question; ask when it is not a git repo or does not
naturally look like a project. Then, last of all, ask for any skipped draft:
what should this fractal do, and what are its completion requirements? Inferred
or asked, double-check everything with the user — printing the full table last
is what lets them catch a misinterpreted parameter before anything is created.

Even when a directive is thorough, ask follow-up questions that refine the seed
— tighter completion conditions, scope, caps — rather than proceeding on the
directive alone; Step 2's conversation is where these land.

To change a setting after init, edit `<node_dir>/config.json` directly (the node
reads it at launch), or use `fractal node config set <key>=<value>`. Run
`fractal node init --help` for the full list. (`--reset` also reconfigures, but
it wipes the node to a stock empty node — see the Reset case below — so it is
the heavy option, not a setting tweak.)

Cost ceilings are **soft**: a node tracks spend (its own and its children's,
including sync) but is never *hard*-stopped at `--max-cost` — it winds down
inside its reserve and the loop ends the run at that iteration's boundary. The
full budget doctrine — reserve pricing, `cost remaining` semantics, per-agent
in-step enforcement, and how a budget-ended run reports — is canonical in the
node's `skills/fractal/SKILL.md` Cost section; read it there when advising the
user or reading a capped node's status.

Nodes run their agent with elevated permissions by design (Claude
`bypassPermissions`, Codex `danger-full-access`, Grok `always-approve`, opencode
`"permission": "allow"` plus `--auto`, omp `approvalMode: yolo` plus `--yolo`)
so they can work unattended — only launch nodes whose task you trust to run
autonomously.

## Activation

Resolve these before proceeding:

- **`path`**: the interpreted path parameter, resolved to absolute.

### Step 0: Install CLI

Install the fractal CLI from PyPI if `fractal` is not already on your `PATH`.
fractal shells out to the `wiki` command, so install both:

```bash
pipx install plasma-fractal
pipx install plasma-wiki
```

(`uv tool install plasma-fractal --with-executables-from plasma-wiki` does the
same in one command.)

Users install with any manager (uv tool, pipx, pyenv, a project venv, system
pip), so before installing, check what's already there by **running** it —
`fractal --version && wiki --version` — never by name resolution alone: a pyenv
shim for a non-activated env resolves on `PATH` but fails at exec. If a working
install lives off `PATH`, use its absolute paths wherever this skill says
`fractal` or `wiki` for the rest of the session. Only your own shell needs this
care — fractal resolves its helper CLIs from its own installation, so node-side
commands work regardless.

### Step 1: Initialize

The node's `<node_dir>/skills/fractal/SKILL.md` documents spawn mechanics, child
management, configuration, radio, and the full CLI in detail — read it for
further context as needed.

Determine the node's state and proceed accordingly:

1. **The directive asks to continue** — the node already exists. Resolve its
   worktree and node directory from `fractal node list --path=<path>`, then skip
   the rest of this step (the repo and node are already set up — no init or
   commit).

2. **No continue intent, but a node already exists** for this path and name
   (check `fractal node list --path=<path>`) — **ask the user** what to do:

   - **Continue** — treat as case 1 (keep state, continue).
   - **Reset** — wipe and recreate: do case 3, adding `--reset` to
     `fractal node init`. `--reset` returns a **stock empty node** — memory,
     plans, steps, skills, and config are all wiped — so re-author NODE.md,
     steps, and skills (Step 2 onward) from scratch afterward.
   - **Cancel** — abort.

3. **Otherwise, create the node.** Commit fractal's own artifacts autonomously,
   without asking (every command here is idempotent, so re-runs are safe):

   1. `fractal init <path> --agent=<agent>` — writes the root node data
      (`.fractal/`) and project wiki (`wiki/`); a no-op if the root already
      exists (re-run to update the stored `--agent`). For a monorepo sub-project
      `<path>` these nest under it (`<path>/.fractal/`, `<path>/wiki/`), not the
      repo root. `--agent` sets the default agent that spawned nodes inherit; if
      the user didn't specify one, default to `--agent=claude` if you are
      Claude, `--agent=codex` if you are Codex, `--agent=grok` if you are Grok,
      `--agent=opencode` if you are opencode, or `--agent=omp` if you are Oh My
      Pi. `--provider` sets the default provider route the same way (e.g.
      `openrouter` routes claude or codex through OpenRouter on
      `OPENROUTER_API_KEY`; agents without routes ignore it, and omitting it
      means each agent's own endpoint). Route mechanics to know: an inherited
      route is cleared per node with `fractal node config set provider=null`;
      the key is captured into the node's tmux session at launch (tmux >= 3.2),
      so rotating it requires a node restart; routed spend is audited on the
      OpenRouter dashboard (the ledger records the local estimate).
   2. `fractal commit "configure <current_branch>" --init` — commits the project
      wiki on the user's base branch, so the node worktree branches from a
      *committed* tree (an uncommitted wiki is invisible to
      `fractal node init`).
   3. `fractal node init <name> ...` (add `--reset` for case 2's Reset) —
      creates the worktree and node directory. `--agent` is optional: when
      omitted, the node automatically inherits the user node's default (the
      agent set in step 1). Pass the parameters interpreted from the directive;
      if you intend to pass additional options, confirm with the user first. If
      it fails, stop and report the error.

The project `wiki/` is **git-tracked** (as are node-branch seeds) — never add it
to `.gitignore`. The root node's own `.fractal/` is **git-ignored on the
top-level branch** by default, keeping it out of your main history; run
`fractal track` to commit it there too and `fractal untrack` to revert — both
toggle only the ignore and print the follow-up git command, never touching the
index. Fractal manages this automatically: its runtime artifacts (worktrees, the
central database, status, agent logs) ride the repo-local `.git/info/exclude`,
and the top-level `.fractal/<branch>/` hides itself with its own ignore file —
the committed `.gitignore` is never touched. Keep your own ignore patterns
anchored (`/artifacts/`, not `artifacts/`), or they also match — and silently
hide — same-named subtrees at any depth, such as a node's committable
`.fractal/<node>/artifacts/`.

`fractal init` also wires the wiki merge driver: the committed `.gitattributes`
assigns `merge=wiki` to the generated wiki `_index.md` files, while the driver
itself lives in repo-local git config, so merges of branches carrying wiki pages
auto-resolve the generated index sections. Local config does not survive a clone
— on a fresh clone the attribute is present but the driver is not, and
`_index.md` merges fall back to git's default and may conflict on generated
content; re-running `fractal init` registers it (verify with
`git config --get merge.wiki.driver`).

The output includes the project directory (worktree root) and the node data
directory. Read these from the output to use in later steps (e.g.
`<node_dir>/NODE.md`).

If the output includes Obsidian plugin instructions, relay them to the user —
installing the listed plugin(s) and running `wiki config --path=<path>` on the
project wiki (`wiki/`) or memory wiki (`<node_dir>/memory`) lets them browse in
Obsidian (optional).

### Step 2: Define the node

If continuing an existing node, it is already defined from its previous run. Ask
the user whether to keep that definition as-is (proceed to next step) or update
it — revisit the relevant topics below to adjust goals, completion requirements,
rules, budget, or steps before relaunching.

Have a conversation with the user to define what this node should do. Work
through each topic below in order. Ask questions naturally — do not dump all
topics at once. Wait for the user's response to each before moving on.

**a) Goals and instructions.** Start from the suggested instructions printed
when you read the directive (or the user's answer to your closing question when
no draft was printed) and ask the user what to refine. Draw out specifics: what
area of the codebase, what kind of work, any constraints or preferences. Node
configuration is the highest-leverage work — a well-configured node runs
autonomously for hours; a vague one burns budget. Push for specific, verifiable
goals rather than transcribing broad statements. Write the result into the
`## Instructions` section of `<node_dir>/NODE.md`.

**b) Completion requirements.** Start from the suggested completion requirement
printed when you read the directive (or the user's answer to your closing
question when no draft was printed) and ask how the user will know the node is
done. Help them articulate concrete, verifiable conditions. If the work is
open-ended with no natural stopping point, suggest leaving this section empty
and using `--max-iters` to cap the run. Write the result into the
`## Completion Requirements` section of `<node_dir>/NODE.md`.

**c) Rules and constraints.** Ask if there are any additional rules beyond the
defaults — files or directories to avoid, patterns to follow, tools to use or
skip, style preferences. If the user has additions, append them to the
`## Rules` section. If not, move on.

**d) Decomposition.** Always ask how much the node should be encouraged to
decompose — how wide and how deep. Wide is `--max-children` and
`--max-descendants` (how many subtasks can run in parallel); deep is
`--max-depth` (how many management levels below this node). All three default to
unlimited, and `0` disables spawning. Map the answer onto those caps, and write
the encouragement itself into the `## Instructions` section of
`<node_dir>/NODE.md`: the seed's Delegation rule already tells a node with
nonzero caps to spawn when a subtask is separable, so record only a deviation
from that default — decompose more aggressively, or hold back and work solo
unless a subtask is clearly independent.

**e) Budget and scope.** Ask about cost limits (`--max-cost` caps each run —
runs are isolated — `--max-iter-cost` caps per-iteration). `--max-cost` is
optional but strongly recommended: without it the node runs **uncapped** — a
warning at start, bounded only by `--max-iters`/`--timeout` — so settle on a cap
unless the user deliberately wants an uncapped run, confirmed explicitly: before
launching any uncapped node, ask an are-you-sure and get a yes (a user's
explicit uncapped request in this conversation counts) — never default into
uncapped. Also recommend `--max-iter-cost`. If the node should only touch
certain files or directories, ask about `--scope` (restricts what the node can
commit). For open-ended work with no completion requirements, suggest
`--max-iters` to cap iterations.

> [!WARNING]
> A **low `--max-cost` paired with an expensive `--model`** is the combination
> most likely to overshoot the budget by a large *percentage*. The run-level
> ceiling is **soft** and only checked *between* steps, so a single step costing
> a big fraction of — or more than — the whole budget overshoots before the next
> check runs. `claude` caps each step with a hard per-step budget (limiting the
> overshoot, but truncating work when the budget is tiny); `codex` has no
> per-step cap, so its overshoot is bounded only by `--step-timeout`. For a
> small budget, prefer a cheaper `--model` and set `--max-iter-cost`; reserve
> expensive models for budgets large enough that one step is a small slice. The
> sizing floor: never set `--max-cost` (or a remaining grant) within ~2x the
> model's single-turn cost — a cap inside that band can be overshot by a large
> fraction in one turn, and that overshoot is documented, accepted behavior: no
> enforcement absorbs it.

Model-choice economics under a budget — when a cheaper model at the same dollar
cap is the right call, and which roles keep frontier models — are covered in the
node skill's Cost section.

When spawning runs whose outputs will be *compared* (A/B arms, benchmark
variants), fork them from one pinned tip and declare the endowment in each run's
config commit: the tip sha plus the baseline figures the comparison will read
against. Comparisons read against the declared baseline, never against stale
round figures.

**f) Remote pushing.** Nodes push their branch to `origin` after each commit by
default; `--local` keeps commits local. Err on the side of `--local`: pass it
unless the user has made it clear they want commits pushed to the remote. With
no remote the push is skipped automatically, so move on.

**g) Iteration steps.** Briefly explain how each iteration works: sync runs
automatically before each numbered step to handle radio communication (inbox,
feed, parent directives), then the step itself executes. The default steps are
prepare, plan, execute, review, and commit — but steps can be added, removed, or
replaced by editing `<node_dir>/steps/`. Ask if the user wants to modify them.
Most users keep the defaults. **Sync is itself a billed step** — it runs once
per numbered step (its prompt comes from `modes/SYNC.md`, which is *not* listed
in `steps/`), so an iteration with N step files actually runs ~2N agent
invocations and a budget sized by counting `steps/` undercounts (roughly the
per-sync cost × N per iteration). Sync can be disabled with `--no-sync` for
lightweight leaf nodes. A step may carry YAML frontmatter: `agent: <command>`
runs that step on a different agent (each agent keeps its own woven session
across the steps that use it), `provider: <route>` overrides the provider route
(agents without routes ignore it), `model: <name>` overrides the model for that
step, `effort: <level>` overrides the reasoning effort, `timeout: <duration>`
overrides the node-global `step_timeout` for that step alone, `detached: true`
isolates a single step in its own session within a continuous node, and
`requires_approval: true` holds the loop after the step completes until the
operator approves it (`fractal node pending`/`approve`).

**h) Environment setup.** Ask if the project needs environment preparation
(virtual environments, dependencies, containers, build steps). If so, edit
`<node_dir>/scripts/setup.sh`. It runs automatically at the start of every
iteration and must be idempotent.

**i) Validation and testing.** Mention that `<node_dir>/scripts/lint.sh` runs
before each commit, and `<node_dir>/scripts/test.sh` is called by the agent
during execution. Ask if the user wants to configure either.

**j) Review.** Once all sections are defined, print the final contents of
`<node_dir>/NODE.md` so the user can review it. Ask if anything needs
adjustment. Iterate until the user is satisfied.

### Step 3: Launch

Print the exact commands you are about to run, then **ask the user to choose**:

- **Launch** — commit the seed and start the node.
- **Revise** — adjust the node's definition or options first, then re-confirm.
- **Cancel** — do not launch.

Only proceed if **Launch** is explicitly chosen.

Once launch is approved, commit the configured seed and start the node from the
worktree — fractal commands act on the node in the current directory, so no path
is needed:

```bash
cd <worktree>  # .worktrees/<branch>
fractal commit "configure <name>" --init
fractal node start
```

When the project runs a markdown formatter hook, expect `fractal commit --init`
to refuse if the hook rewrites a seed file at all — seed pages are guarded
byte-for-byte, and only wiki pages get the structure-preserving auto-retry;
follow the error's remediation. Never run project format hooks over `.fractal/`
seed files yourself: step frontmatter (`requires_approval:`, `agent:`,
`timeout:`, ...) is load-bearing, a generic mdformat destroys it, and
`pre-commit run --files` reports success on untracked files even while rewriting
them.

All run parameters were set at init (in `config.json`); `start` takes no config
arguments — only `--continue` (plus `--clean` to discard uncommitted project
files, and `--max-cost` to re-arm the cap after a budget-ended run) when
continuing a stopped/exited node. If the user wants to tweak a setting first,
edit `<node_dir>/config.json`, then start. The node launches in a detached tmux
session.

### Step 4: Post-launch briefing

Once the node is running, briefly explain how to interact with it:

- **Steering:** Edit `<node_dir>/NODE.md` directly to adjust goals, rules, or
  instructions. The node reads it fresh at every step. Retune caps with
  `fractal node update` — it updates the registry row and the child's
  `config.json` together, and a running loop picks the change up at its next
  iteration boundary (a direct config edit is honored at the same boundary but
  leaves the registry stale until the loop heals it).

- **Monitoring:** From the node's worktree (`cd <worktree>`), commands act on it
  directly — `fractal node status`, `fractal node cost spent`, and
  `fractal node attach` (watch live output — use this, not raw `tmux -t`, whose
  prefix matching can attach the wrong session). `fractal node list` shows this
  node's subtree (from a leaf worktree, just its own descendants) — run it from
  the repo root to see the whole tree; it lists live nodes only (`--all`
  includes retired ones, `--retired` only those). Read `<node_dir>/memory/`
  (knowledge) or `<node_dir>/plans/` (plans). A run that ends `completed` after
  `--max-iters` only means the iteration budget was exhausted, not that the goal
  was met — check `fractal node activity` for the per-iteration outcomes. Figure
  scopes differ by design: `cost spent` reads the run's full subtree (children
  included), while `activity`'s `cost` column sums only the node's own steps —
  and both are per-run, with no lifetime rollup.

- **TUI:** For a live view of the whole tree — nodes, runs, costs, and output —
  suggest the user open the dashboard with `fractal open` (from anywhere in the
  repo; add a node branch to open focused on it, or a tree's root branch to open
  at the root).

- **Stopping:** From the worktree, three escalation levels:

  - `fractal node finish` — stop after current iteration
  - `fractal node stop` — stop after current step (waits for the in-flight step
    to complete; never tears it)
  - `fractal node kill` — kill immediately

  All three cascade over the node's entire subtree of active descendants,
  children first — stopping a manager stops every lane under it.

- **Pausing:** `fractal node pause` freezes the subtree in place — it aborts
  each in-flight agent turn and parks every loop with its run open — and
  `fractal node resume` relaunches it exactly there (same budgets, same
  iteration, the interrupted step's session continued when possible). Tree-wide,
  `fractal pause` / `fractal resume` (from anywhere in the repo) brake and
  release the whole tree; the brake also latches every new `node init`/`start` —
  new top-level nodes included — until `fractal resume` lifts it (a subtree
  `fractal node resume` under a paused ancestor or a tree-wide brake refuses —
  the brake holds until `fractal resume`). Paused state is durable: it survives
  a reboot or a filesystem copy of the repo to another machine. A paused node
  holds its spawn slot and blocks its parent's finish-drain; only `resume`,
  `kill`, and `chat` act on it (ask a paused node what it was doing —
  `chat --current` forks the interrupted claude, grok, opencode, or omp session,
  and the TUI's chat does so by default; a codex node gets a fresh session).
  Note the distinction: `resume` continues a *paused* run in place, while
  `start --continue` opens a *fresh* run (worktree restored — uncommitted
  project files need `--clean`, and a budget-ended run refuses without an
  explicit `--max-cost`) on a stopped/exited node.

- **Worktree:** The node runs in a git worktree at
  `<repo>/.worktrees/<branch>/`. The user's repo is untouched. When done, from
  the repo root, merge with `fractal node merge <branch>`. A conflicted merge
  restores the target worktree exactly as it was and leaves the resolution to
  you: redo the squash there by hand (`git merge --squash <branch>`), resolve
  and stage the conflicts, then finish with
  `fractal node merge <branch> --continue` rather than committing yourself — the
  continue runs the rest of the merge (seed strip, wiki index refresh, commit,
  merge-base advance) that a hand-rolled finish would miss, and names every file
  where the resolution kept the target's content over the node's — the node
  still carries its own version there, so land the resolution on the node (or
  retire it) or a later re-merge silently re-stages it. Deleting afterward with
  `fractal node delete <branch>` is optional hygiene, never automatic — a merged
  branch keeps audit value (delete must run from outside the worktree). Pass
  `--delete` to `merge` to chain the two in one command: every delete refusal
  and the confirmation `[y/N]` pre-flight the squash, so a chain that cannot
  finish never starts. **Delete is destructive:** it is recursive — removing the
  node's whole subtree — and force-removes each worktree and **force-deletes the
  branch(es) regardless of merge state**, so any committed-but-unmerged work is
  lost. Always confirm the `merge` succeeded first (check its output). To keep a
  node's branch while hiding it, retire it instead. Delete prompts for
  confirmation `[y/N]`, chained or standalone; pass `--force`/`-f` to skip the
  prompt.

- **Reset:** `fractal reset` (from anywhere in the repo) tears down every node
  worktree, branch, and registration in the tree in one sweep; the project,
  wiki, and all history in the central database survive, so fresh nodes spawn
  immediately after. It refuses while any node is running; a paused node is
  killed as part of the teardown, which the confirmation `[y/N]` authorizes
  (`--force`/`-f` skips it).

- **Tree scope:** one repository can carry several trees — one per branch you
  ran `fractal init` on, each with its own user node, database, and history. The
  tree-scoped verbs (`pause`, `resume`, `reset`, `track`, `untrack`, `open`)
  take the tree's root branch as an optional first argument and otherwise infer
  it from your own branch: a node worktree names its tree, the repo root names
  its checkout. With several trees and a checkout belonging to none of them,
  they refuse rather than guess — name the tree. `open` and `node list` also
  accept a node branch in that slot, scoping to that node instead of the whole
  tree. `destroy` takes the same name but never infers it: a bare
  `fractal destroy` is ambiguous between this tree and everything, so name a
  tree or pass `--all`, the one repo-wide verb.

- **Radio:** nodes communicate via `fractal radio` commands. `radio send` writes
  any channel permissions allow, given at least one routing dimension
  (`--node`/`--parent` or `--channel`) — a fully bare send refuses; `radio post`
  is the quiet reporting verb for publicly readable channels (outbox, public),
  and a bare post lands in your own `outbox`. The listings (`messages`/`feed`)
  show metadata and never touch read state; `radio read` prints full bodies and
  writes your read receipts. Replies route to the counterparty's inbox — a feed
  (outbox) post is never replyable in place. Run `fractal radio --help` to
  explore.

Offer to help the user edit `NODE.md`, check progress, or read plan files.

## Operator

After launch a node runs autonomously — but the user (root) node never does: it
is a passive database with no loop, the human's anchor at the root of the tree
(`"user": true`; never started, merged, or deleted). Every other node runs its
own loop; the root has none, so **you are the operator.** Once the tree is
running you are the *operator* — you do for the root what the loop does for
every node, except your parent is the user and your task is their intent. Run
like the loop you are: don't wait to be asked. Lead with a monitoring pass, keep
a standing watch where your environment allows recurring checks, and act with
full autonomy on the user's behalf — steer, `finish`/`stop`/`kill`, merge, spawn
— reporting what you did rather than asking first. Pause only for genuinely
ambiguous or irreversible calls, and narrow the moment the user scopes you back.

Work the tree through the CLI — run it from the repo root, or name a branch
positionally. Monitor with `fractal node list`/`status`/`activity`/`cost`, and
`chat <branch> "<q>" --current` to ask a running agent without disrupting its
loop — `--current` forks the live loop session (claude, grok, opencode, or omp);
for codex nodes ask via a fresh chat (omit `--current`) or continue one in place
with `--session ... --resume`. The root auto-subscribes to its children's
`outbox` but has no auto-sync, so poll its radio yourself —
`fractal radio read --channel=inbox --unread` (its inbox) and
`read --feed --unread` (children, one hop); the `messages`/`feed` listings
survey metadata without consuming unread state, and your reads receipt as you,
the reader — send directives to a child's inbox
(`radio send <message> --node=<branch> ...`), and send-and-continue (a node sees
you only on its next sync). Steer by editing `NODE.md` files (re-read each step)
or by radio; approve gates (`node pending`/`approve`), retune limits
(`node update`), and merge finished subtrees (deleting after merge is optional
hygiene, not a default). Relay both ways: surface progress, blockers, and cost
up to the user, and translate their intent down into edits, directives, and
spawns. Ask the user for input and feedback freely, but never let a question
block you unless it is absolutely critical — proceed on your best judgment, make
reversible calls, and note them.

### Commissioning

When a child's launch deserves review before it burns budget, separate init from
start and put a countersign between them. The gate is social — nothing in the
tool enforces it — but it catches seed mistakes while they are still free to
fix:

1. **Commission** — init the child and author its seed (NODE.md, caps, steps),
   but do not start it.
2. **Pin the seed** — commit the configured seed
   (`fractal commit "configure <name>" --init` from the child's worktree) and
   record the pin: the child branch's seed commit sha plus a checklist of what
   was reviewed (NODE.md, caps, steps).
3. **Request countersign** — send the pin and checklist over radio to the
   designated reviewer (an ancestor or a named reviewer node) and wait for the
   reply.
4. **Start only on countersign** — launch the child only after the countersign
   reply. A child whose branch has moved past the pinned sha is stale:
   re-commission (re-review, re-pin) before starting.

## CLI reference

Run `fractal --help` and `fractal <command> --help` for all commands and
options. Commands act on the node in the current directory by default, so `cd`
into a worktree to operate on it; to act on another node from elsewhere in the
repo, name its branch positionally (e.g. `fractal node status <branch>`). Radio
verbs that write rows (send, post, reply, react, unsend, save, unsave, sub,
unsub, channel create/delete) act as the loop-exported `_NODE` before the cwd,
so a node's writes attribute to it from any directory; radio listings stay
cwd-scoped. `--path` is an escape hatch for running from outside a worktree.
`fractal node init` is the exception: `<name>` plus the project root via
`--path`.

Nodes spawn their own children — the running loop sets the `_NODE` environment
that makes `fractal node init` nest the child under the calling node (the same
export names the acting identity for radio's writing verbs). Running it by hand
from inside a worktree without that env nests under the repo-root user node
instead, so operators normally don't spawn children manually.
