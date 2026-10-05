---
name: features/files/contribution
desc: |
  Listing a node's work product: the full tracked listing, and the changed
  listing that shows only the node's own contribution with net line counts.
created: 2026-07-21T04:45:55Z
updated: 2026-07-21T04:51:52Z
---

# features/files/contribution

[[features/files/_index|..]]

***

The project-files surface (the `Files` facade in `fractal/core/files.py`,
reached as `node.files`) exposes a node's work product to external consumers.
Its listing has two modes.

## The full tracked listing

Without a diff scope, the listing is every git-tracked file in the worktree,
sorted by path, each entry carrying its worktree-relative path and on-disk size.
Git-ignored runtime state (the node database, status file, logs) never appears,
because it is never tracked. Fractal's own content — the project wiki (`wiki/`
unless the project's `.fractal/.settings.json` names another folder) and
`.fractal/` — lists like any other tracked content: it is readable project
state, and consumers filter or collapse machinery rather than the surface hiding
it. Only structurally unreachable entries are dropped: any `.git` component, a
leading `.worktrees` (which on the user node would reach into sibling nodes'
worktrees), and names carrying leading pathspec magic — matched casefolded,
because APFS resolves names case-insensitively (see
[[features/files/path_validation|path_validation]]).

A `path` filter restricts either mode to one worktree-relative subtree.

## The changed listing: the node's own contribution

With a `since` scope (`base`, `commit`, `iteration`, or `run` — see
[[features/files/anchors_and_history|anchors_and_history]]), the listing is
instead the node's *own contribution*: only files touched by the node's own
commits — a first-parent, no-merges walk from the anchor — ever list. A tree at
`HEAD` contains everything ever merged in, so without that membership walk
content synced from the parent (and through it, siblings) would read as this
node's output. Merge commits themselves are excluded, while a squash-merged
child — an ordinary commit on the node's own line — rightly counts as the node's
work.

Each changed entry carries a change kind (`added`/`modified`/`deleted`; a type
change reads as modified) and net `additions`/`deletions` line counts from a
three-dot diff of the anchor against `HEAD` (`None` for a binary file). The
counts are *net*: a touched file whose self-corrections cancel out drops from
the listing entirely, and a member file shows exactly what a diff view renders.
A deleted file stays listed with size zero so its removal can render.

## Edge semantics

- The listing reads a live worktree: a file vanishing mid-poll is skipped, not
  an error, and diff reads pin `HEAD` to one sha so a loop commit landing
  mid-poll cannot straddle the two reads.
- A symlink lists (and serves) only while its target resolves inside the
  worktree — worktree content is agent-authored, so an escaping link is dropped
  at the serving boundary.
- Non-ASCII paths survive intact: every git read is NUL-delimited so no path is
  ever C-quoted.

## Archives and uploads

The same listing feeds a read-only zip archive (full or changed set; deletions,
having nothing on disk, are skipped). In the other direction the surface accepts
uploads: raw bytes land atomically at a validated worktree-relative path
(parents created), validated at the stricter write tier, and join the tracked
listing only once committed. A narrow pathspec commit stages and commits exactly
the named uploaded paths — no lint, scope check, or push, and no `commit` event
is logged, because an upload has no run lineage and fabricating one would
silently shift the `iteration`/`run` diff anchors. Both writes and pathspec
commits refuse on a paused node, whose frozen work admits only resume, kill, and
chat.
