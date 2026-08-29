---
name: user_flow/finishing
desc: |
  How work concludes: the finish signal and who sends it, what the
  squash-merge does — the machinery restore, the merge-base advance, and the
  merge guards each have a page here — and how finished work climbs the tree
  to the base branch and the operator's review.
created: 2026-07-21T04:47:43Z
updated: 2026-08-29T11:30:50Z
---

# user_flow/finishing

[[user_flow/_index|..]]

[[user_flow/finishing/machinery_restore|machinery_restore]]: How the squash
keeps a node's machinery off the target: every machinery path returns to the
target's content except the merging node's own scope roots, the warnings that
name what the restore dropped and where the node's copy survives, and the leak
check that strips seed copies from the user node's branch.

[[user_flow/finishing/merge_base_advance|merge_base_advance]]: The post-squash
advance that converges a node onto its target: the two-parent commit it records,
when it is skipped and how to clear the way, how a resolution on the target and
the nothing-to-merge outcome reach the node, and the recipe for a node whose
advance carried no content.

[[user_flow/finishing/merge_guards|merge_guards]]: The refusals and recovery
paths of a merge: the node and target state guards and the repo-wide merge lock,
the untracked-file and footprint refusals, conflict restore and its verdicts,
interrupts, and finishing a hand-resolved squash with the continue flag.

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
  `.fractal/` directory on the target except a scope root of the merging node
  that lies there, so the node's own seed and its descendants' seeds never land,
  and a copy the user node's branch already tracks is stripped.
  [[user_flow/finishing/machinery_restore]] covers the restore, its warnings,
  and the leak check on the user node.
- **The wiki merges cleanly.** Generated wiki indexes are refreshed from the
  merged filesystem on the target, so both branches' wiki pages survive side by
  side.
- **Re-merges stay cheap.** After the squash commit lands, the node's merge-base
  advances with a two-parent commit on its branch whose tree is the target's
  post-squash tree, so the node converges to the target and merging it again
  later only diffs its new work. [[user_flow/finishing/merge_base_advance]]
  covers the advance, when it is skipped, how a resolution on the target reaches
  the node, and the recipe for a node whose advance carried no content.
- **Guards.** Merge refuses while the node or its target is active or paused,
  while the target worktree has uncommitted changes, when the squash would write
  over a file that exists untracked on the target's disk, and when the staged
  paths fall outside the node's commit boundaries; a conflict restores the
  target, and `--continue` finishes a hand-resolved squash.
  [[user_flow/finishing/merge_guards]] covers each refusal and its remedy, the
  restore verdicts, interrupts, and the `--continue` contract.

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
