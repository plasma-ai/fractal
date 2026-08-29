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
  HEAD after the squash — except paths under the merging node's own scope roots
  (a `--meta` node's scope is the target's own seed directory, which is its work
  product and lands). The node's own `.fractal/<branch>/` seed and its
  descendants' seeds are stripped as well, so a parent never accumulates its
  children's data directories. Work product only. When the restore drops paths
  outside the node's own machinery — an edit to the target's estate, a foreign
  seed, a `.fractal/profiles/` change — the merge warns naming them, so a
  deliberate change can be landed by hand. Before the squash, the merge also
  warns when the target already tracks node seed directories it does not own (a
  node owns its own and its descendants'; the user node, whose own directory is
  git-ignored, owns none), naming them and printing the
  `git -C <target worktree> rm -r --cached <dirs>` line that removes them; the
  merge removes only the merging node's own and its descendants'.
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
  child's worktree is dirty or on another branch.
- **Guards.** Merge refuses while the node is active or paused, while the target
  node is active or paused (a running target's worktree must not be mutated
  under it — except by the target's own loop, which merges its settled children
  as part of its normal iteration), and while the target worktree has
  uncommitted changes. On conflict the target worktree is restored exactly as it
  was — unless every conflict sits under `.fractal/` outside the node's scope
  roots, in which case they resolve to the target's content (the node's own seed
  deleted) and the merge continues, with a warning naming the paths.
- **Footprint.** Before committing, the merge refuses if the squash changes any
  path outside the node's scope roots, its project wiki, `.fractal/`, or the
  worktree-root `.gitattributes` (a repo-root node with no scope is
  unrestricted; a sub-project node with no scope is bounded to its project
  directory) — the same law `fractal commit` applies. The refusal names the
  paths and both remedies: widen the scope with
  `fractal node config set scope=<dirs> --path=<node worktree>`, or rerun with
  `fractal node merge --ignore-scope`, which lands the paths. A fresh merge
  restores the target on refusal; `--continue` leaves the staged squash in
  place.
- **Conflicts finish with `--continue`.** After a conflicted merge, redo the
  squash by hand in the target worktree (`git merge --squash <branch>`), resolve
  and stage the conflicts, then run `fractal node merge <node> --continue`: it
  validates the staged squash came from the node's branch, then runs the merge's
  own tail — `.fractal/` restore and seed strip, footprint check, index refresh,
  commit, merge-base advance — so a manual resolution never hand-rolls those
  steps or strands seed files in the target working tree. Its failure paths
  leave the staged resolution in place (never `reset --hard`); fix and re-run.
- **A resolution lands on the node.** The merge-base advance writes the target's
  adjudicated tree into the node's worktree, so a hunk resolved in the target's
  favor, a file dropped from the squash, or a restore to base content reaches
  the node and the next merge does not re-offer it.
- **Nothing to merge is a clean outcome.** A node whose changes are already on
  the target reports so and exits without committing. A `--continue` whose
  resolution kept the target's own content for every change the node offered
  reports that instead, and still finishes the tail — the squash state is
  cleared and the merge-base advances, so the resolved conflict is not replayed
  on the next merge.

**A merge-base advanced without content.** A node whose branch carries an
advance commit that changed no content — a two-parent commit whose tree equals
its first parent's, so the node's tree is still its own rather than the target's
— still squashes from that base, so its next merge can land stale copies of
files the node never edited (`git diff --name-only <base> -- . ':!.fractal'`
lists them). Before that merge, right after the node's work has landed and while
its worktree is clean, adopt the base's tree in the node's worktree:
`git checkout <base> -- . ':!.fractal'`, `git rm` any path
`git diff --name-only <base> -- . ':!.fractal'` still lists, and commit. A plain
`git merge <base>` does not fix this: the stale copy is the only changed side,
so it wins without a conflict.

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
