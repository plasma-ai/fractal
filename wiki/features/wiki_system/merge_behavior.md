---
name: features/wiki_system/merge_behavior
desc: |
  How wikis merge across git branches: the custom merge driver for index
  files, which regions auto-resolve versus conflict, and the parent's
  reconciliation duties after integrating children.
created: 2026-07-21T04:51:58Z
updated: 2026-07-21T04:51:58Z
---

# features/wiki_system/merge_behavior

[[features/wiki_system/_index|..]]

***

Sibling nodes evolve the same project wiki on parallel git branches (the merge
topology: [[architecture/worktrees]]), so `_index.md` files — whose generated
link blocks churn on every update — would conflict constantly under plain
three-way merging. The wiki ships a custom git merge driver for them, wired by
`.gitattributes` (`**/_index.md merge=wiki`) and registered in the repo-local
git config by `wiki init` and `wiki config`. Fractal commits the attribute in
the user node's baseline, so every branch in the tree merges indexes through the
driver.

## What the driver does

The driver splits an `_index.md` at the `***` separator. Above it, the
frontmatter merges field-aware: the regenerated keys (`name`, `updated`)
normalize to "ours" — `wiki update` owns them, so their churn must never
conflict — while authored keys (`title`, `desc`, `created`, `category`, `tags`,
`sources`) get a normal three-way merge that can conflict. The generated link
block resolves to the union of both sides' rows, less each row the base had that
one side deleted while the other left it alone, so a merge never drops one
side's additions and never brings back one side's deletions. A deletion stands
only when the deleting side's rows carry no formatter escape damage (`\[\[`),
since rows the driver cannot read say nothing about what that side deleted. On
add/add merges (both sides created the page) `created` joins the take-ours set,
since both stamps come from independent update runs. A side whose frontmatter is
undetectable is treated as unchanged from base, never as a deletion; a side that
dropped the `***` separator its base carried cannot be split into regions, so it
surfaces a whole-file conflict with a repair hint. Everything below `***` is
authored content and merges three-way, with a hint comment planted above add/add
body conflicts.

When the whole file merges cleanly, the driver then rewrites the H1 from the
merged `title` (else `name`) and puts the rows in the order `wiki update` writes
them, so a clean merge of two current indexes is current as merged. It does so
only when the wiki's own class (a trusted `.wiki/wiki.py` hook's category order
included) leaves the current branch's index as written; an index another class
ordered, or one whose hook cannot load or run, stays as merged, with theirs' new
rows below ours'. A conflicted merge, or one left as merged, settles at the next
`wiki update` (which the fractal commit pipeline runs; see
[[features/wiki_system/index_regeneration]]), which re-sorts the block from the
merged filesystem and prunes stale rows.

Whatever the driver leaves, `fractal node merge` runs `wiki update` on the
squash before committing: the project wiki and the memory wiki whole, and in
every other wiki root — a folder declaring a tracked `.wiki/settings.json`, such
as a corpus beside the project wiki — each index the driver merged, whose staged
bytes match neither side's, through an update scoped to its folder (the whole
root for the root's own index), so indexes outside those folders keep their
bytes. The bytes, not the merge base, mark a merged index, so a criss-cross
history, where git names one of two bases, hides none. An index belongs to the
deepest declared root above it, every folder is matched literally, and an index
in a folder its wiki excludes is skipped, as the wiki's own walk skips it.

The update reads the disk, so the merge refuses an untracked file under the
project wiki or in a folder it refreshes, naming it: indexed into the squash
commit, it would dangle in every clean checkout. The memory wiki is the merging
node's own, and its next commit lands what it holds. A wiki declared at the
worktree root refuses the merge outright: it encloses every folder, so each
refresh would walk the whole worktree, and node init refuses to spawn under one
for the same reason.

## The parent's reconciliation job

Children link only to pages that existed when they wrote, so freshly merged
siblings leave stale cross-links behind. Index rows refresh mechanically at
commit, but prose links do not: after integrating children, the parent runs
`wiki lint` and repairs or prunes what it reports — now-resolvable links become
wikilinks, still-dangling ones stay plain text — and refreshes any navigation
tables it authored, which lint cannot see.
