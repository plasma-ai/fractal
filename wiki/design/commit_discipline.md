---
name: design/commit_discipline
desc: |
  Why every iteration must commit, how scope enforcement and the
  always-allowed shared wiki divide the worktree, why force-commit backstops
  exist as fail-safes rather than workflow, and why child work merges with
  no-fast-forward on the node's mainline but squashes toward the base.
created: 2026-07-21T04:48:14Z
updated: 2026-07-21T04:48:14Z
---

# design/commit_discipline

[[_index|..]]

***

A node's branch is its only durable output channel: children fork it, parents
merge it, and a later relaunch cleans whatever never made it in. Commit
discipline is the set of rules that keep that channel truthful — every unit of
work lands, only owned work lands, and history stays readable at every level of
the tree.

## Why every iteration commits

An iteration that ends dirty leaves work in the one place fractal treats as
disposable. A continue-mode relaunch cleans uncommitted changes by design; a
child forks the parent's branch at its last *commit*, not its working tree; a
merge ships only committed history. So the loop's contract is simple: each
iteration ends with a commit, made by the agent as part of the work. The commit
is also the iteration's record — its subject carries the branch and the
run-qualified iteration label, composed by the pipeline itself, which is why a
message repeating those labels is rejected: history must carry each label
exactly once.

Committing per iteration, rather than per run, sets the granularity of every
recovery path: pause freezes at most one iteration of uncommitted state, a crash
loses at most one iteration of work, and a parent can merge a child mid-task and
get a coherent snapshot. It also keeps the wiki honest — the commit pipeline
refreshes wiki indexes and runs lint before landing, so a broken wiki or a
failing lint blocks the commit rather than propagating through merges.

## Scope enforcement and the shared wiki

A scoped node may only commit inside its scope roots — the commit pipeline
collects every change across working tree, index, and untracked files, and
refuses the commit if any falls outside. Enforcement at commit time, rather than
write time, matches how agents actually work: exploration may touch anything,
but only owned paths may *land*. The scope check is what makes parallel siblings
safe — two nodes with disjoint scopes cannot race each other's files into
history.

Two prefixes are always committable regardless of scope. The node's own data
directory, because memory and plans are the node's private state and belong on
its branch. And the shared project wiki, deliberately: the wiki is the tree-wide
knowledge channel, and a scope that silenced a node's ability to record durable
findings would push knowledge into radio messages and private memory, where
other nodes cannot find it. The wiki is the one surface where every node's write
access is part of the design, with merge machinery (see below and
[[features/wiki_system/merge_behavior]]) reconciling the concurrent edits.

Runtime artifacts — the central database and its sidecars, status and pause
markers, virtualenvs — are excluded from staging unconditionally. They are state
about the run, not work product, and letting them ride a commit would make
merges carry live machinery across branches.

## Force-commit backstops are fail-safes

The loop force-commits in a small set of situations of one shape: work is in the
tree and the normal commit path cannot be trusted to land it. After a step that
should have committed but left the tree dirty; on failure paths (labeling the
commit with the failed step and carrying the reason in the body); and as a final
sweep at run end so a node never exits unclean. Force bypasses the scope check,
lint, and git hooks — a backstop save must never be blocked by the very checks
whose failure it may be saving evidence of, and a mutating hook must not rewrite
the save. Because it bypasses review, a force commit describes itself: its body
folds in the staging warnings and a capped diffstat, so git history alone
explains what the sweep captured.

The backstops are fail-safes, not workflow. The agent committing its own
iteration is the contract; the backstop exists because the alternative — work
silently discarded by the next relaunch's clean — is strictly worse than an
unreviewed commit. A backstop-labeled commit in history is a signal that an
iteration went wrong, not a license to skip the commit step.

## Merge discipline: no-fast-forward in, squash out

Child work crosses two boundaries, and each gets a different merge shape because
each answers a different reader.

On the node's own mainline, each child integrates with a no-fast-forward merge,
one labeled merge commit per child. A fast-forward would inline the child's
dozens of per-iteration commits into the parent's first-parent history, making
"what did I integrate, and when" unanswerable; the merge-commit discipline keeps
the parent's first-parent log a clean sequence of its own iterations plus one
integration point per child.

Toward the base — the parent branch a finished node merges into — the
relationship inverts: the base wants the node's *result*, not its process. So
the merge squashes: a single commit lands on the target, while the full
per-iteration history stays preserved on the node's branch for archaeology. The
squash also returns every `.fractal/` directory on the target to the target's
HEAD — minus the merging node's own scope roots under it, since a `--meta`
node's work product *is* the target's seed directory — so the machinery that ran
the node never lands in the parent's tree and a node's edit to the target's
estate or to a foreign seed never rides its squash: the target's version is
restored (an added path is removed), the merge-base advance then carries the
target's tree into the node's worktree, and the node's copy survives only in its
branch history. The node's own seed and its descendants' are stripped from the
user node's branch as well, where a tracked copy is a leak; a node target keeps
a copy it already tracks — its PREPARE `git merge --no-ff` of the child put it
there — until it merges upward itself, so a child whose advance was skipped
never inherits a deletion of its own live seed on its next merge of the parent.
A fresh squash that would write over any file that exists untracked on the
target's disk — an ignored private file, or the user node's own live seed,
self-ignored there but committable from a child — is refused before it runs,
since git treats an ignored file as expendable and the tail would then commit or
delete it. The squash is held to the node's commit scope as well: commit-time
enforcement is bypassable (`--ignore-scope`, the force backstops, a parent's
no-fast-forward merge carrying grandchild commits no check saw), so the squash
is the one point that sees the node's whole offering: the staged paths outside
`.fractal/` are judged by the node's boundaries — its scope roots and the
project wiki, with the worktree-root `.gitattributes` admitted only as init's
own `**/_index.md merge=wiki` edit, exactly as `fractal commit` judges them —
and a path outside them is refused there with the paths named and
`node merge --ignore-scope` as the override. The merge refuses over a dirty,
active, or paused target — the squash mutates the target's worktree, and its
recovery path resets hard, so it must never run where it could destroy someone's
uncommitted work — except from inside the target's own loop, which merges its
settled children as part of a normal iteration. Merges are serialized per
repository for the same reason: `git merge --squash` locks the target's index
only for its final write, so two sibling merges into one target would pass their
preflight and interleave, leaving the loser's files untracked in the target
where its reset cannot undo them; a repo-wide merge lock queues them instead.

After the squash commit lands, the node's merge-base advances with a real
two-parent commit on its branch — parents the node's HEAD and the target's HEAD,
tree the target's post-squash tree with the node's own seed and its descendants'
seeds kept. Recording the target's *content*, not just its ancestry, is what
keeps re-merges honest: the node converges to whatever the target adjudicated,
so a later merge in either direction only diffs new work and never takes a stale
copy of a file the node did not edit as the one changed side.

## Per-worktree commit identity

Every node worktree sets a worktree-scoped git author name — the node's own
dotted branch name — while the email stays inherited from the user. Commits are
thereby attributed to the node that made them, machine-readably, without
severing them from the human the tree belongs to. This is what lets the
project-files surface answer "what did *this node* contribute" by authorship
even after merges interleave branches, and it costs nothing: identity is set
once at worktree creation and re-asserted on re-init, and never leaks outside
the worktree.

The scope configuration surface and commit pipeline structure live in
[[architecture/worktrees]] and [[configuration/_index|configuration/]]; the
loop's commit step and its backstop ordering are described under
[[features/loop/_index|features/loop/]].
