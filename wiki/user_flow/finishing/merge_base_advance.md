---
name: user_flow/finishing/merge_base_advance
desc: |
  The post-squash advance that converges a node onto its target: the
  two-parent commit it records, when it is skipped and how to clear the way,
  how a resolution on the target and the nothing-to-merge outcome reach the
  node, and the recipe for a node whose advance carried no content.
created: 2026-08-29T11:30:50Z
updated: 2026-08-29T11:30:50Z
---

# user_flow/finishing/merge_base_advance

[[_index|..]]

***

After the squash commit lands, the child's merge-base is advanced with a
two-parent commit on the child's branch — `merge <target> (post-squash)`,
parents the child's HEAD and the target's HEAD — whose tree is the target's
post-squash tree with the child's own seed and its descendants' seeds kept from
the child. The child's worktree takes that tree, so the node converges to the
target's adjudicated content and merging the same node again later only diffs
its new work instead of re-conflicting on everything already landed.

## When the advance is skipped

The advance is skipped, with a warning, when the child's worktree is dirty or on
another branch, or when it holds an untracked or ignored file or directory in
the way of a path the target now tracks (a private `local.env` a sibling landed
on the target, a rename on the target that lands where the node keeps a private
file, an untracked case-only alias on a case-insensitive filesystem, a directory
where the target adds a file; a path the node tracks under a different case is
not a collision — the update renames it — and neither is a path the node tracks
at, under, or above the hit — the target turned a file into a directory or back,
a type change the update performs) — move it aside, and the next merge that
lands work advances it (a fresh merge offering nothing exits at "Nothing to
merge" before the advance; the cases that still advance are under Nothing to
merge below); a failed worktree update rolls the worktree back before skipping.
A git read that fails during the advance (the node's worktree stops answering)
skips it with a warning rather than failing the landed merge, and an edit to a
tracked file the commit law's excludes hide (a force-added lock or status file)
counts as dirt, so the update never overwrites it.

## A resolution lands on the node

The merge-base advance writes the target's adjudicated tree into the node's
worktree, so a hunk resolved in the target's favor, a file dropped from the
squash, or a restore to base content reaches the node and the next merge does
not re-offer it.

## Nothing to merge is a clean outcome

A node whose changes are already on the target reports so and exits without
committing. When the restore dropped a `.fractal/` change outside the node's own
seed and its descendants' — the paths the restore warnings name — or a
`.fractal/` conflict outside them resolved to the target's content (the node
edited a foreign seed copy the target has since removed), that outcome still
advances the node's merge-base, so the node converges and the warning does not
repeat on the next merge; a node whose only offering is an edit to its own seed
exits at "Nothing to merge" without advancing, and its next work merge advances
it. A `--continue` whose resolution kept the target's own content for every
change the node offered reports that instead, and still finishes the tail — the
squash state is cleared and the merge-base advances, so the resolved conflict is
not replayed on the next merge.

## A merge-base advanced without content

A node whose branch carries an advance commit that changed no content — a
two-parent commit whose tree equals its first parent's, so the node's tree is
still its own rather than the target's — still squashes from that base, so its
next merge can land stale copies of files the node never edited. A poisoned node
with unmerged work lands that work with a merge first. The stale copies ride the
squash onto the target as reverts; the footprint refusal names only a scoped
node's stale copies outside its scope roots (restore those paths from the base
on the node's branch, commit with
`fractal commit "<message>" --ignore-scope --path=<node worktree>`, and rerun),
while stale copies inside its roots ride the squash exactly like the unscoped
case. So in both cases review the squash commit and restore any reverted file
from the target's history — or run the recipe below before the node's next
merge. A node that merges real work needs no recipe: that landed work merge
advances its merge-base, and the advance converges the node. A node with nothing
new still offers its stale copies: merging it lands them on the target as a
revert commit and then advances the node onto the reverted target, so do not
merge such a node — run the recipe first, closing step included. A scoped node
whose stale copies all fall outside its roots is refused by the footprint check
and never advances, so it needs the recipe too. The recipe adopts the base's
tree in the node's clean worktree, discarding any unmerged work on the branch
(so it suits a node with nothing left to merge). Its checkout writes the base's
copy over any untracked or ignored file in the node's worktree at a path the
base tracks, so first check
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
