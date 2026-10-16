---
requires_approval: false
---

## Prepare

**Skip this step if there is no parent branch and no child nodes.**

Parent branch: `PARENT_BRANCH="${CURRENT_BRANCH%.*}"`. Spawned nodes are always
`<parent>.<name>`, so stripping the last dotted segment yields the real parent
-- when that parent is dotless it is the user/root node, which you merge like
any other parent to pick up the user's commits. (Only the user/root node itself
is parentless, and it never iterates. Edge case: a user/root node run on a
*dotted* branch would misread -- rely on the config `user` flag if that
matters.) Children: `git branch --list "$CURRENT_BRANCH.*"`.

**Parent merge.** `git merge "$PARENT_BRANCH"` to pull upstream changes; resolve
conflicts preferring the parent for upstream updates, the node for work in
progress. After resolving, stage and commit the merge before proceeding -- never
continue to PLAN with a half-finished merge.

**Child merges.** For each child with new commits
(`git log $CURRENT_BRANCH..<child> --oneline`), review and decide -- some
children are experiments or research, not code to merge. Merge ready work with
`git merge --no-ff -m "merge <child>" <child>` -- always use this commit message
format for consistency, and always use `--no-ff` so each integration lands as
one labelled merge commit on your mainline (`git log --first-parent --merges`
lists one per child), never a fast-forward that inlines the child's
per-iteration commits. Reuse the integration summary you announce below. An
empty log means the child is mid-iteration; never cherry-pick its uncommitted
files -- skip it this iteration.

Diff a child with three dots: `git diff $CURRENT_BRANCH...<child>` shows only
its changes from the merge base. Two-dot diffs show misleading rename hints
across `.fractal/` directories.

After merging, assess each child's trajectory: on track, stuck, or off-task?

**After any merge**, the merge driver resolves each `_index.md` link block
itself -- both branches' new rows kept, a row one side deleted left out -- and
the indexes refresh mechanically (`fractal commit` and the squash-merge into
your parent both regenerate them from the merged filesystem). Consolidation is
where **stale cross-links get reconciled:** children link only to pages that
existed when they wrote, so once siblings are merged together run
`wiki lint --path=$WIKI_DIR` and repair or prune the now-resolvable (or
still-dangling) sibling links. Fixing stale links and missing descriptions is
the parent's job at merge.

**Announce integration.** When a merge brought in *materially new* work -- not a
no-op/"Already up to date" merge, nor inherited scaffolding or `_index.md`
link-row churn -- post a one-line outbox note naming what you integrated (e.g.
"merged main.foo.bar's auth work") so the affected child and siblings see it via
their feed. Skip the note for trivial/scaffolding-only merges (avoid empty
per-iteration noise). It is a broadcast -- expect no reply.
