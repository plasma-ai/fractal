## Git Operation Pending

Your worktree holds a git operation left unfinished:

$GIT_OPERATIONS

Finish or abort each one first. Finishing it may take a commit (a merge ends
with one); any other git work -- a new commit, merge, rebase, or checkout --
started on top of it builds on a half-applied state. For a merge, cherry-pick,
revert, or rebase, `git status` in `$WORKTREE_DIR` shows where it stopped.
