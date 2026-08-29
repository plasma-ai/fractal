---
name: user_flow/finishing/machinery_restore
desc: |
  How the squash keeps a node's machinery off the target: every machinery
  path returns to the target's content except the merging node's own scope
  roots, the warnings that name what the restore dropped and where the
  node's copy survives, and the leak check that strips seed copies from the
  user node's branch.
created: 2026-08-29T11:30:50Z
updated: 2026-08-29T11:30:50Z
---

# user_flow/finishing/machinery_restore

[[_index|..]]

***

The squash changes nothing under any `.fractal/` directory on the target — every
such path returns to the target's HEAD after the squash — except a scope root of
the merging node that is, or lies under, a `.fractal/` directory (a `--meta`
node's scope is the target's own seed directory, which is its work product and
lands). The squash therefore never adds the node's own `.fractal/<branch>/` seed
or its descendants' seeds to the target. On the user node's branch the merge
also strips a copy the branch already tracks — the root owns no seed, so a
tracked copy there is a leak. On a node target a tracked copy stays: the
parent's PREPARE `git merge --no-ff` of the child put it there, and it leaves
only when the parent itself merges upward, so a child whose advance was skipped
never inherits a deletion of its own live seed on its next merge of the parent.
Work product only.

## Restore warnings

When the restore drops paths outside the node's own machinery — an edit to the
target's estate, a foreign seed, a `.fractal/profiles/` change — the merge warns
in two lines: paths the target tracks are restored to the target's content, and
paths the target does not track are removed, since the target does not track
them. The merge-base advance then brings the target's tree into the node's
worktree, so the node's copy survives only in its branch history
(`git -C <node worktree> log --full-history -- <path>` lists the advance that
dropped the path first and the node's own commit below it;
`git show <commit>:<path>` on that lower commit, or
`git show <advance>^:<path>`, reads the copy; a plain `git log` follows the
target's side of the advance and lists nothing).

## Leaked seeds on the user node

When the target is the user node, the merge also judges the root's committed
tree before the squash for seed directories of the root's own dotted nodes
(`.fractal/<target>.*/`, so a `--base` merge into another tree's root judges
that root) — the root owns no seed, so every one is a leak. Exactly what the
strip removes — the merging node's seed at its own project prefix and its
descendants' seeds at any depth — is named in one warning
(`tracks seeds of <branch> or its descendants, leaked by an earlier merge: <dirs>; this merge removes them`);
the rest, a same-named copy of the node's seed under another project prefix (the
node re-created at a different project path) included, get a second warning with
a remedy line that removes them from the tree and from the root worktree's disk
and commits the removal
(`git -C <target worktree> rm -r -- <dirs> && git -C <target worktree> commit -m 'drop leaked node seeds'`);
the copies are never live seeds (those sit in each node's own worktree). A node
target is not judged: its branch legitimately carries other nodes' seeds (its
ancestors' by fork, its descendants' by PREPARE merges, a sibling's by the
merge-base advance). The check reads the committed tree, so a `--continue` never
reports the hand-staged seed. Whether the target is the user node is read from
the repo's record of the target branch, so a root checked out in a linked
worktree is still stripped and leak-checked; a direct `merge.sh` call that
cannot read the target's node config warns
`could not read <target>'s node config; treating it as a node target` instead of
silently treating it as a node.
