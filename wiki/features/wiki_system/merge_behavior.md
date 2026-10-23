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
`.gitattributes` (`**/_index.md merge=wiki` in the repo-root file and, from a
wiki tool with a page driver, `*.md merge=wiki` in each wiki root's own) and
registered in the repo-local git config by `wiki init` and `wiki config`.
Fractal commits the attributes in the user node's baseline, and each node's
memory wiki attributes in its own, so every branch in the tree merges indexes
through the driver.

## What the driver does

The driver splits an `_index.md` at the `***` separator. Above it, the
frontmatter merges field-aware: the regenerated keys (`name`, `updated`)
normalize to "ours" — `wiki update` owns them, so their churn must never
conflict — while authored keys (`title`, `desc`, `created`, `category`, `tags`,
`sources`) get a normal three-way merge that can conflict. The generated link
block resolves to the union of both sides' rows, less each row the base had that
one side deleted while the other left it alone, so a merge of readable rows
keeps both sides' additions and brings back neither side's deletions. The driver
does not repair rows that carry formatter escape damage (`\[\[`): a side whose
`_index.md` link rows are escaped can lose its added rows and row edits in the
merge, so repair that side with `wiki update` on its branch before merging. A
deletion stands only when the deleting side's rows carry no such damage, since
rows the driver cannot read say nothing about what that side deleted. When the
base carries no `created:` stamp (an add/add merge, where both sides created the
page, or a hand-written or imported index), `created` joins the take-ours set,
since both stamps come from independent update runs. A side whose frontmatter is
undetectable is treated as unchanged from base, never as a deletion; a side that
dropped the `***` separator its base carried cannot be split into regions, so it
surfaces a whole-file conflict with a repair hint. Everything below `***` is
authored content and merges three-way, with a hint comment planted above add/add
body conflicts.

When the whole file merges cleanly, the driver then rewrites the H1 from the
merged `title` (else `name`) and puts the rows in the order `wiki update` writes
them, so a clean merge in which each side only added, deleted, or retitled
entries is current as merged; a row one side changed and the other deleted, or a
row both sides changed, waits for the next `wiki update`. It does so only when
the wiki's own class (a trusted `.wiki/wiki.py` hook's category order included)
leaves the current branch's index as written; an index another class ordered, or
one whose hook cannot load or run, stays as merged, with theirs' new rows below
ours'. A conflicted merge, or one left as merged, settles at the next
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
in a folder its wiki excludes is skipped, as the wiki's own walk skips it. An
index whose folder encloses another declared wiki is skipped too, with a warning
naming both: the wiki refuses to sweep across a nested root, so that index lands
as the driver merged it.

The update reads the disk, so the merge refuses an untracked file under the
project wiki or in a folder it refreshes, naming it: indexed into the squash
commit, it would dangle in every clean checkout. Untracked files are judged by
two of the wiki's rules — the repository's ignore rules alone, so a file only a
personal `core.excludesFile` hides still refuses, since the wiki would index it,
and the dot rule, so a file under a dot-prefixed folder, which the walk never
lists, neither refuses the merge nor rides the squash commit. The guard does not
mirror the walk's other exclusions: a symlink, or a file the wiki's
`exclude.patterns` matches, still refuses though the refresh would skip it,
which is safe, and committing or moving the file clears the refusal. The refresh
stages only tracked files it updated, indexes it created, and a root's `.wiki/`
state. The memory wiki is the target's, and the target's next commit lands what
it holds. A refresh step that fails restores the target like a conflict; the
reset keeps untracked files, so the merge also removes each index and `.wiki/`
file the refresh created and had not yet staged, and the retry lands instead of
refusing it as an untracked file. A wiki declared at the worktree root refuses
the merge outright: it encloses every folder, so each refresh would walk the
whole worktree, and node init refuses to spawn under one for the same reason.

## The parent's reconciliation job

Children link only to pages that existed when they wrote, so freshly merged
siblings leave stale cross-links behind. Index rows refresh mechanically at
commit, but prose links do not: after integrating children, the parent runs
`wiki lint` and repairs or prunes what it reports — now-resolvable links become
wikilinks, still-dangling ones stay plain text — and refreshes any navigation
tables it authored, which lint cannot see.
