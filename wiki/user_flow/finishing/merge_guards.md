---
name: user_flow/finishing/merge_guards
desc: |
  The refusals and recovery paths of a merge: the node and target state
  guards and the repo-wide merge lock, the untracked-file and footprint
  refusals, conflict restore and its verdicts, interrupts, and finishing a
  hand-resolved squash with the continue flag.
created: 2026-08-29T11:30:50Z
updated: 2026-08-29T11:30:50Z
---

# user_flow/finishing/merge_guards

[[_index|..]]

***

Merge refuses while the node is active or paused, while the target node is
active or paused (a running target's worktree must not be mutated under it —
except by the target's own loop, which merges its settled children as part of
its normal iteration), and while the target worktree has uncommitted changes.
Merges are serialized per repository: `fractal node merge` holds a repo-wide
merge lock while the script runs, so two sibling merges into one target queue
instead of interleaving their index writes.

## Untracked files on the target

A fresh merge also refuses when the squash would write over any file that exists
untracked on the target's disk — an ignored private file such as `local.env`, or
the user node's own live seed, self-ignored on the root but committable from a
child — judged over every path the node added or changed since the merge-base
that the target's HEAD does not track, a file sitting where the squash would
create a directory included (the prefix walk stops at a path the target's HEAD
tracks: a tracked file the node replaced with a directory is the squash's own
type change, not a collision); the refusal ("would overwrite untracked files in
`<target>`'s worktree") names the files to move aside or drop from the branch
before merging.

## Conflicts and failed squashes

On conflict the target worktree is restored exactly as it was — unless every
conflict sits under `.fractal/` outside the node's scope roots, in which case
they resolve to the target's content (a path the target tracks returns to its
content, a path it lacks is removed; on the user node the node's own seed is
then stripped, while a node target keeps the copy it tracks) and the merge
continues, with a warning naming the paths. A squash that dies after git wrote
the index (a stale or unwritable `SQUASH_MSG`) is reset and its squash markers
cleared, and the error reads
`merging <branch> into <target> failed after staging; the parent worktree has been restored; resolve and retry`,
or its `could NOT be restored` form naming the
`git -C <target worktree> reset --hard HEAD` to run, judged by the target's
state; `failed before staging anything` is reserved for a squash that staged
nothing — git's own refusal over a plain untracked file, or another git process
holding the target's index.

## Interrupts

An interrupt (Ctrl-C, or a SIGINT delivered to the `fractal node merge` process)
is forwarded to the script: before the squash commit, its restore trap resets
the target and marks the merge event failed (an interrupt that lands while the
event is still being opened closes it as failed too); once `git commit` has
moved the target's ref the squash has landed, and the merge finishes it — the
merge-base advances, the event closes as completed, and the command prints
`Squash-merged ...` and exits 0 — rather than reporting a restore. An interrupt
during the advance finishes the node's worktree update or rolls it back, and
warns only when an advance was underway; one during a no-op merge's bookkeeping
prints that arm's own summary (`Nothing to merge: ...`) with no advance warning.
An interrupted `--continue` leaves the staged squash in place to re-run, unless
its commit had already landed — then it is finished the same way. Every
"restored" verdict is judged by the target's state — clean and out of the squash
— not by git's exit code; when the target is not restored, the error names the
`git -C <target worktree> reset --hard HEAD` to run before merging again.

## Footprint

Before committing, the merge judges the staged paths outside `.fractal/` (the
restore has already settled those) by the node's commit boundaries — its scope
roots and its project wiki, plus the worktree-root `.gitattributes` only when it
is init's own `**/_index.md merge=wiki` edit (a repo-root node with no scope is
unrestricted; a sub-project node with no scope is bounded to its project
directory) — the same law `fractal commit` applies, and refuses when any path
falls outside them. The refusal names the paths and both remedies: widen the
scope with `fractal node config set scope=<dirs> --path=<node worktree>` and
commit it with `fractal commit "widen scope" --path=<node worktree>` (an
uncommitted config change makes the rerun skip the merge-base advance), or rerun
with `fractal node merge --ignore-scope`, which lands the paths. A fresh merge
restores the target on refusal. A `--continue` leaves the staged squash in
place, and its remedies differ: re-run with `--continue --ignore-scope`, or
widen the scope (config set, then commit) and redo the squash
(`git -C <target worktree> reset --hard HEAD && git -C <target worktree> merge --squash <branch>`),
because the widening commit is a node commit made after the hand squash, which
makes `--continue` refuse.

## Conflicts finish with `--continue`

After a conflicted merge, redo the squash by hand in the target worktree
(`git merge --squash <branch>`), resolve and stage the conflicts, then run
`fractal node merge <node> --continue`: it validates that the staged squash came
from the node's branch, is fully staged (unstaged tracked changes in the target
refuse it: save any copy you need, stage with `git add` the paths that belong to
the resolution, and discard the rest with
`git -C <target worktree> checkout -- <path>` — the merge restores every
`.fractal/` path to the target's HEAD anyway), and covers the node's current tip
(a commit the node made after the hand squash — an iteration, a nested child's
merge — refuses it, naming the redo:
`git -C <target worktree> reset --hard HEAD && git -C <target worktree> merge --squash <branch>`),
then runs the merge's own tail — `.fractal/` restore and seed strip, footprint
check, index refresh, commit, merge-base advance — so a manual resolution never
hand-rolls those steps or strands seed files in the target working tree. Its
failure paths leave the staged resolution in place (never `reset --hard`); fix
and re-run. The hand squash runs without the fresh merge's untracked-file check,
so a `--continue` cannot prevent the overwrite of a file that exists untracked
on the target's disk: git's own squash refuses over a plain untracked file but
writes over an ignored one (git treats it as expendable) — a file the fresh
merge would have refused over — and the hand squash has already done it, so move
private files aside before redoing the squash by hand.
