---
name: user_flow/finishing
desc: |
  How work concludes: the finish signal and who sends it, what the
  squash-merge does and guards, and how finished work climbs the tree to
  the base branch and the operator's review.
created: 2026-07-21T04:47:43Z
updated: 2026-07-21T04:47:43Z
---

# user_flow/finishing

[[_index|..]]

***

Work concludes in two distinct acts: **finishing** (a node's loop ends
gracefully) and **merging** (its branch's work lands on its parent). They are
separate commands because the operator reviews between them.

## The finish signal

`fractal node finish [<node>] [--reason="..."]` tells a node to stop after its
current iteration — the loop completes the iteration it is in (including its
commit) and exits rather than starting another. The signal fans out to the
node's active descendants children-first, so a manager's subtree winds down with
it. `--cancel` withdraws a pending finish that has not yet taken effect; the
cancel deliberately does not fan out, because a descendant finishing is its
normal completion path, not something to revoke.

Who sends it matters:

- **The node itself.** The designed happy path: NODE.md's Completion
  Requirements name the conditions, and the node runs
  `fractal node finish --reason="..."` in the iteration that meets them, landing
  status `completed`.
- **The operator.** A finish signal sent from outside ends the run at the same
  boundary; use it to conclude work that is good enough, or redirect effort.
  (The blunter siblings: `stop` ends after the current step, `kill` immediately
  — see [[user_flow/continue_resume]] for what each preserves.)
- **The root.** On the user node, finish is a tree-wide broadcast — the user
  node has no loop of its own, so it signals every active node in the tree.

How the run books depends on why it finished. A deliberate finish lands
`completed` — even when the signal arrives during reserve wind-down or the spend
crosses the cap mid-drain, the goal-met landing holds, with the overshoot
recorded on the run row. A budget stop is not a goal-met completion: the run
books `exited` (with exit code 0 — a designed stop, not an abnormal death), so a
parent and `node merge` can tell unfinished work from done. The loop's own
budget phrases are reserved in `--reason` — a reason carrying them classifies
the finish as a budget abort — so a node states its met goal in its own words.

A finished node is settled: its worktree and branch remain, holding its
committed work, ready for review and merge.

## What merge does

`fractal node merge <node>` squash-merges the node's branch into its merge
target — the node's configured base branch when one was set, else the dotted
parent (the branch name minus its last segment). The mechanics an operator
should know:

- **One commit lands.** The target receives a single squash commit named
  `merge <branch>`; the node's full per-iteration history stays on its own
  branch. Review the squash like any commit.
- **The node's machinery does not travel.** The squash changes nothing under any
  `.fractal/` directory on the target — every such path returns to the target's
  HEAD after the squash — except a scope root of the merging node that is, or
  lies under, a `.fractal/` directory (a `--meta` node's scope is the target's
  own seed directory, which is its work product and lands). The squash therefore
  never adds the node's own `.fractal/<branch>/` seed or its descendants' seeds
  to the target. On the user node's branch the merge also strips a copy the
  branch already tracks — the root owns no seed, so a tracked copy there is a
  leak. On a node target a tracked copy stays: the parent's PREPARE
  `git merge --no-ff` of the child put it there, and it leaves only when the
  parent itself merges upward, so a child whose advance was skipped never
  inherits a deletion of its own live seed on its next merge of the parent. Work
  product only. When the restore drops paths outside the node's own machinery —
  an edit to the target's estate, a foreign seed, a `.fractal/profiles/` change
  — the merge warns in two lines: paths the target tracks are restored to the
  target's content, and paths the target does not track are removed, since the
  target does not track them. The merge-base advance then brings the target's
  tree into the node's worktree, so the node's copy survives only in its branch
  history (`git -C <node worktree> log --full-history -- <path>` lists the
  advance that dropped the path first and the node's own commit below it;
  `git show <commit>:<path>` on that lower commit, or
  `git show <advance>^:<path>`, reads the copy; a plain `git log` follows the
  target's side of the advance and lists nothing). When the target is the user
  node, the merge also judges the root's committed tree before the squash for
  seed directories of the root's own dotted nodes (`.fractal/<target>.*/`, so a
  `--base` merge into another tree's root judges that root) — the root owns no
  seed, so every one is a leak. Exactly what the strip removes — the merging
  node's seed at its own project prefix and its descendants' seeds at any depth
  — is named in one warning
  (`tracks seeds of <branch> or its descendants, leaked by an earlier merge: <dirs>; this merge removes them`);
  the rest, a same-named copy of the node's seed under another project prefix
  (the node re-created at a different project path) included, get a second
  warning with a remedy line that removes them from the tree and from the root
  worktree's disk and commits the removal
  (`git -C <target worktree> rm -r -- <dirs> && git -C <target worktree> commit -m 'drop leaked node seeds'`);
  the copies are never live seeds (those sit in each node's own worktree). A
  node target is not judged: its branch legitimately carries other nodes' seeds
  (its ancestors' by fork, its descendants' by PREPARE merges, a sibling's by
  the merge-base advance). The check reads the committed tree, so a `--continue`
  never reports the hand-staged seed. Whether the target is the user node is
  read from the repo's record of the target branch, so a root checked out in a
  linked worktree is still stripped and leak-checked; a direct `merge.sh` call
  that cannot read the target's node config warns
  `could not read <target>'s node config; treating it as a node target` instead
  of silently treating it as a node.
- **The wiki merges cleanly.** Generated wiki indexes are refreshed from the
  merged filesystem on the target, so both branches' wiki pages survive side by
  side.
- **Re-merges stay cheap.** After the squash commit lands, the child's
  merge-base is advanced with a two-parent commit on the child's branch —
  `merge <target> (post-squash)`, parents the child's HEAD and the target's HEAD
  — whose tree is the target's post-squash tree with the child's own seed and
  its descendants' seeds kept from the child. The child's worktree takes that
  tree, so the node converges to the target's adjudicated content and merging
  the same node again later only diffs its new work instead of re-conflicting on
  everything already landed. The advance is skipped, with a warning, when the
  child's worktree is dirty or on another branch, or when it holds an untracked
  or ignored file or directory in the way of a path the target now tracks (a
  private `local.env` a sibling landed on the target, a rename on the target
  that lands where the node keeps a private file, an untracked case-only alias
  on a case-insensitive filesystem, a directory where the target adds a file; a
  path the node tracks under a different case is not a collision — the update
  renames it — and neither is a path the node tracks at, under, or above the hit
  — the target turned a file into a directory or back, a type change the update
  performs) — move it aside, and the next merge that lands work advances it (a
  fresh merge offering nothing exits at "Nothing to merge" before the advance,
  unless the restore dropped a `.fractal/` change outside the node's own seed
  and its descendants' — the paths the restore warnings name — or a `.fractal/`
  conflict outside them resolved to the target's content (the node edited a
  foreign seed copy the target has since removed), either of which still
  advances the merge-base so the node converges and the warning does not repeat;
  a node whose only offering is an edit to its own seed exits without advancing,
  and its next work merge advances it); a failed worktree update rolls the
  worktree back before skipping. A git read that fails during the advance (the
  node's worktree stops answering) skips it with a warning rather than failing
  the landed merge, and an edit to a tracked file the commit law's excludes hide
  (a force-added lock or status file) counts as dirt, so the update never
  overwrites it.
- **Guards.** Merge refuses while the node is active or paused, while the target
  node is active or paused (a running target's worktree must not be mutated
  under it — except by the target's own loop, which merges its settled children
  as part of its normal iteration), and while the target worktree has
  uncommitted changes. Merges are serialized per repository:
  `fractal node merge` holds a repo-wide merge lock while the script runs, so
  two sibling merges into one target queue instead of interleaving their index
  writes. A fresh merge also refuses when the squash would write over any file
  that exists untracked on the target's disk — an ignored private file such as
  `local.env`, or the user node's own live seed, self-ignored on the root but
  committable from a child — judged over every path the node added or changed
  since the merge-base that the target's HEAD does not track, a file sitting
  where the squash would create a directory included (the prefix walk stops at a
  path the target's HEAD tracks: a tracked file the node replaced with a
  directory is the squash's own type change, not a collision); the refusal
  ("would overwrite untracked files in `<target>`'s worktree") names the files
  to move aside or drop from the branch before merging. On conflict the target
  worktree is restored exactly as it was — unless every conflict sits under
  `.fractal/` outside the node's scope roots, in which case they resolve to the
  target's content (a path the target tracks returns to its content, a path it
  lacks is removed; on the user node the node's own seed is then stripped, while
  a node target keeps the copy it tracks) and the merge continues, with a
  warning naming the paths. A squash that dies after git wrote the index (a
  stale or unwritable `SQUASH_MSG`) is reset and its squash markers cleared, and
  the error reads
  `merging <branch> into <target> failed after staging; the parent worktree has been restored; resolve and retry`,
  or its `could NOT be restored` form naming the
  `git -C <target worktree> reset --hard HEAD` to run, judged by the target's
  state; `failed before staging anything` is reserved for a squash that staged
  nothing — git's own refusal over a plain untracked file, or another git
  process holding the target's index. An interrupt (Ctrl-C, or a SIGINT
  delivered to the `fractal node merge` process) is forwarded to the script:
  before the squash commit, its restore trap resets the target and marks the
  merge event failed (an interrupt that lands while the event is still being
  opened closes it as failed too); once `git commit` has moved the target's ref
  the squash has landed, and the merge finishes it — the merge-base advances,
  the event closes as completed, and the command prints `Squash-merged ...` and
  exits 0 — rather than reporting a restore. An interrupt during the advance
  finishes the node's worktree update or rolls it back, and warns only when an
  advance was underway; one during a no-op merge's bookkeeping prints that arm's
  own summary (`Nothing to merge: ...`) with no advance warning. An interrupted
  `--continue` leaves the staged squash in place to re-run, unless its commit
  had already landed — then it is finished the same way. Every "restored"
  verdict is judged by the target's state — clean and out of the squash — not by
  git's exit code; when the target is not restored, the error names the
  `git -C <target worktree> reset --hard HEAD` to run before merging again.
- **Footprint.** Before committing, the merge judges the staged paths outside
  `.fractal/` (the restore has already settled those) by the node's commit
  boundaries — its scope roots and its project wiki, plus the worktree-root
  `.gitattributes` only when it is init's own `**/_index.md merge=wiki` edit (a
  repo-root node with no scope is unrestricted; a sub-project node with no scope
  is bounded to its project directory) — the same law `fractal commit` applies,
  and refuses when any path falls outside them. The refusal names the paths and
  both remedies: widen the scope with
  `fractal node config set scope=<dirs> --path=<node worktree>` and commit it
  with `fractal commit "widen scope" --path=<node worktree>` (an uncommitted
  config change makes the rerun skip the merge-base advance), or rerun with
  `fractal node merge --ignore-scope`, which lands the paths. A fresh merge
  restores the target on refusal. A `--continue` leaves the staged squash in
  place, and its remedies differ: re-run with `--continue --ignore-scope`, or
  widen the scope (config set, then commit) and redo the squash
  (`git -C <target worktree> reset --hard HEAD && git -C <target worktree> merge --squash <branch>`),
  because the widening commit is a node commit made after the hand squash, which
  makes `--continue` refuse.
- **Conflicts finish with `--continue`.** After a conflicted merge, redo the
  squash by hand in the target worktree (`git merge --squash <branch>`), resolve
  and stage the conflicts, then run `fractal node merge <node> --continue`: it
  validates that the staged squash came from the node's branch, is fully staged
  (unstaged tracked changes in the target refuse it: save any copy you need,
  stage with `git add` the paths that belong to the resolution, and discard the
  rest with `git -C <target worktree> checkout -- <path>` — the merge restores
  every `.fractal/` path to the target's HEAD anyway), and covers the node's
  current tip (a commit the node made after the hand squash — an iteration, a
  nested child's merge — refuses it, naming the redo:
  `git -C <target worktree> reset --hard HEAD && git -C <target worktree> merge --squash <branch>`),
  then runs the merge's own tail — `.fractal/` restore and seed strip, footprint
  check, index refresh, commit, merge-base advance — so a manual resolution
  never hand-rolls those steps or strands seed files in the target working tree.
  Its failure paths leave the staged resolution in place (never `reset --hard`);
  fix and re-run. The hand squash runs without the fresh merge's untracked-file
  check, so a `--continue` cannot prevent the overwrite of a file that exists
  untracked on the target's disk: git's own squash refuses over a plain
  untracked file but writes over an ignored one (git treats it as expendable) —
  a file the fresh merge would have refused over — and the hand squash has
  already done it, so move private files aside before redoing the squash by
  hand.
- **A resolution lands on the node.** The merge-base advance writes the target's
  adjudicated tree into the node's worktree, so a hunk resolved in the target's
  favor, a file dropped from the squash, or a restore to base content reaches
  the node and the next merge does not re-offer it.
- **Nothing to merge is a clean outcome.** A node whose changes are already on
  the target reports so and exits without committing. When the restore dropped a
  `.fractal/` change outside the node's own seed and its descendants' — the
  paths the restore warnings name — or a `.fractal/` conflict outside them
  resolved to the target's content (the node edited a foreign seed copy the
  target has since removed), that outcome still advances the node's merge-base,
  so the node converges and the warning does not repeat on the next merge; a
  node whose only offering is an edit to its own seed exits at "Nothing to
  merge" without advancing, and its next work merge advances it. A `--continue`
  whose resolution kept the target's own content for every change the node
  offered reports that instead, and still finishes the tail — the squash state
  is cleared and the merge-base advances, so the resolved conflict is not
  replayed on the next merge.

**A merge-base advanced without content.** A node whose branch carries an
advance commit that changed no content — a two-parent commit whose tree equals
its first parent's, so the node's tree is still its own rather than the target's
— still squashes from that base, so its next merge can land stale copies of
files the node never edited. A poisoned node with unmerged work lands that work
with a merge first. The stale copies ride the squash onto the target as reverts;
the footprint refusal names only a scoped node's stale copies outside its scope
roots (restore those paths from the base on the node's branch, commit with
`fractal commit "<message>" --ignore-scope --path=<node worktree>`, and rerun),
while stale copies inside its roots ride the squash exactly like the unscoped
case. So in both cases review the squash commit and restore any reverted file
from the target's history — or run the recipe below before the first
post-upgrade merge. A node that merges real work needs no recipe: that landed
work merge advances its merge-base, and the advance converges the node. A node
with nothing new still offers its stale copies: merging it lands them on the
target as a revert commit and then advances the node onto the reverted target,
so do not merge such a node — run the recipe first, closing step included. A
scoped node whose stale copies all fall outside its roots is refused by the
footprint check and never advances, so it needs the recipe too. The recipe
adopts the base's tree in the node's clean worktree, discarding any unmerged
work on the branch (so it suits a node with nothing left to merge). Its checkout
writes the base's copy over any untracked or ignored file in the node's worktree
at a path the base tracks, so first check
`git -C <node worktree> status --ignored --porcelain` and move such files aside.
The steps: `git checkout <base> -- . ':(exclude,glob)**/.fractal/**'`, `git rm`
any path
`git diff --name-only <base> -- . ':(exclude,glob)**/.fractal/**' ':(exclude).gitattributes'`
still lists, then, if `.gitattributes` lacks `**/_index.md merge=wiki`, append
init's two lines (`# Wiki index merge driver`, `**/_index.md merge=wiki`), and
commit; then, before the base moves again, run
`git -C <node worktree> merge -s ours --no-edit <base>` — the node's tree
already equals the base's, so recording the base's tip as a parent gives a
correct merge-base with correct content. The step records that tip only when the
base has moved since the poisoned merge; otherwise git reports
`Already up to date`, and the adopt commit alone fixes the node. The `.fractal/`
exclude keeps the node's seed (which sits under `<project>/.fractal/` for a
sub-project node) in place; the checkout adopts the base's `.gitattributes`, so
only the `git rm` step excludes it. A plain `git merge <base>` does not fix
this: the stale copy is the only changed side, so it wins without a conflict.

## Reaching the base branch and review

Work climbs the tree the same way at every level. A child finishes; its parent
(a manager node, during its own iterations) reviews the child's branch, merges
it with the same squash machinery, and integrates. At the top of the tree, the
operator plays the parent: a top-level node's merge target is the branch
`fractal init` ran on, and its squash commit lands in the operator's own
checkout of that branch.

The operator's review loop for a finished top-level node:

1. Inspect: `git diff <base>...<branch>` shows the node's work from the merge
   base; the node's radio outbox and its plan files narrate intent.
2. Merge: `fractal node merge <node>` from a settled tree — the squash commit is
   now on your branch, pushable and revertable like any commit.
3. Iterate or clean up: if more work is needed, brief and relaunch the node
   ([[user_flow/continue_resume]]); if the node is done for good, delete it
   ([[user_flow/teardown]]) — the warning on deletion tells you if unmerged
   commits would be lost.

Merging is deliberately not automatic at the top: nothing reaches the base
branch without an operator (or a parent node's explicit iteration decision)
running the merge.
