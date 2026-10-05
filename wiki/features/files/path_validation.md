---
name: features/files/path_validation
desc: |
  The two path-validation tiers of the files surface: the structural tier
  every path passes, and the stricter writable tier uploads must pass.
created: 2026-07-21T04:45:55Z
updated: 2026-07-21T04:51:52Z
---

# features/files/path_validation

[[features/files/_index|..]]

***

Every caller-supplied path on the files surface validates through one of two
tiers before any git or filesystem access. Both normalize to a POSIX
worktree-relative path and compare components casefolded — APFS matches names
case-insensitively, so `.GIT` names the same entry there; rejecting a literal
`.GIT` file on a case-sensitive host is the accepted cost.

## The structural tier (all reads)

The structural tier is the safety boundary for reads, downloads, listings, and
history. It rejects:

- absolute paths, empty paths, and any `..` traversal;
- a leading `:` — pathspec magic. Glob characters are legal name characters, so
  every downstream git call disarms them with a literal pathspec rather than
  banning them;
- any `.git` component — in a linked worktree `.git` is a *file* whose overwrite
  would hijack the gitdir;
- a leading `.worktrees` component — on the user node the worktree is the repo
  root, so the path would reach into sibling nodes' worktrees;
- a path whose resolution escapes the worktree (a symlinked intermediate
  directory must not lead outside).

Fractal's own content passes this tier: the project wiki (`wiki/` unless the
project's `.fractal/.settings.json` names another folder) and `.fractal/` are
readable project state, filtered or collapsed by consumers, not a boundary.

Beyond structure, a read or download serves only what the surface exposes: the
path must be git-tracked — a read under a `since` scope also accepts a member of
that anchor's changed set, which keeps a deleted file's old content readable
without exposing anything else. Containment re-checks at the serving boundary: a
*tracked* symlink whose target escapes the worktree is still not readable or
servable through it.

## The writable tier (uploads and pathspec commits)

Uploads and upload commits validate at a stricter tier: everything the
structural tier rejects, plus any `.fractal` component. A foreign tree's
`.fractal/` is stale machinery a wholesale project upload works better without,
and a raw-bytes overwrite of a live node config would corrupt a running node's
caps — the one path where an upload could reach the control plane rather than
content. The project wiki stays writable: it is project content the user owns,
and uploading an existing project must carry its wiki.

## Reading the tiers together

The listing and the validators agree by construction: a listing never names an
entry a read or download would reject, so a consumer can fetch anything it was
shown. Reads are non-mutating and allowed regardless of lifecycle state; writes
and upload commits additionally refuse on a paused node, whose frozen work
admits only resume, kill, and chat.
