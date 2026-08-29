---
name: user_flow/teardown
desc: |
  The three teardown tiers and their guards: node delete removes one
  subtree, fractal reset clears a tree's worktrees while its history
  survives, and fractal destroy removes one tree by name or, with the all
  flag, the whole fractal as the full inverse of init.
created: 2026-07-21T04:47:43Z
updated: 2026-07-21T04:47:43Z
---

# user_flow/teardown

[[_index|..]]

***

Teardown comes in three tiers of increasing blast radius. Each tier states what
it removes, what survives it, and the guards that keep it from destroying live
or frozen work. Merge first ([[user_flow/finishing/_index|user_flow/finishing]])
— teardown never lands work anywhere.

## Tier 1: `fractal node delete` — one subtree

`fractal node delete <node>` recursively removes a node and its whole subtree,
deepest first: each worktree, each local branch, and each remote branch (for
non-local nodes), plus the subtree's registry rows and radio subscriptions in
the central database. **History survives**: the subtree's runs, steps, events,
and messages persist in the database — deletion removes the machinery, not the
record.

The lifecycle's happy path can chain this tier itself:
`fractal node merge --delete` runs the delete after a successful merge, so a
settled child leaves no worktree or branch behind. Each removal echoes the
worktree's disk size, so `.worktrees/` growth stays visible. Every one of the
delete's guards below rides the pre-flight — the refusals and the confirmation
prompt alike land before the merge, so a chain the teardown would refuse (or the
operator declines) never starts: the squash it would otherwise leave behind is
irreversible.

Its guards:

- A confirmation prompt (`--force` skips it).
- Refuses while the node or any descendant is **active or paused** — stop,
  resume, or kill the subtree first. Delete is the one teardown tier a node can
  reach itself, so it fails closed over paused work rather than discarding a
  frozen mid-step state.
- Refuses from inside any worktree of the subtree (git cannot remove the
  worktree you stand in) and over a locked worktree — the whole subtree is
  pre-flighted before anything is touched, so a problem found late never strands
  a half-deleted tree.
- Warns when the branch has commits its merge target never absorbed: deleting
  discards them, so merge first if the warning surprises you. The warning prints
  once the refusals above have passed — a live or locked member, or the cwd
  inside a doomed worktree, refuses before any warning — and before the
  confirmation prompt (and, under `--force`, before the teardown), while the
  branch still exists to merge, and the `Deleted branch: <branch> (was <sha>)`
  line names the tip the delete discarded. Every live descendant is judged the
  same way, each against the deleted node's surviving merge target, and a
  `--meta` node's edits to its target's seed directory count as work (only its
  own seed and its descendants' seeds are waived as machinery).

Softer alternative: `fractal node retire` parks a node — hidden from
`fractal node list`, unstartable, but its branch, worktree, and history all kept
— and `fractal node unretire` restores it to its pre-retire status. Retire what
you might revisit; delete what you won't. And if a worktree or branch was
cleaned up with plain git instead of `delete`, `fractal node reconcile` audits
the registry afterward, recording each orphan in the events log.

## Tier 2: `fractal reset` — a whole tree, history kept

`fractal reset` is the middle rung: it removes **every** node worktree and local
branch in the tree and clears its node registry, while the user node's data —
its config, memory, and the central database with every history row — plus the
project wiki and all baseline commits survive. The tree is empty but the fractal
is still initialized: fresh nodes spawn immediately after, and past runs remain
queryable. Sibling trees are untouched.

Reach for it when the tree's current shape is spent — an experiment concluded, a
plan superseded — but the project continues under the same fractal.

## Tier 3: `fractal destroy` — one tree, or the whole fractal

`fractal destroy <name>` removes one tree by its root branch: the tree's node
worktrees and local branches, its project-cache entries, and its user node's
data directory — central database and all history included. Sibling trees and
the shared `.worktrees/` plumbing survive; fractal's block in the repository's
git-exclude file is stripped only when the last tree goes, and the plumbing goes
with it. The tree's own root branch is the user's branch and is never deleted.

`fractal destroy --all` is the full inverse of init: every tree's worktrees,
branches, and data directories, plus the `.worktrees/` directory and the
git-exclude block. Exactly one of the two scopes must be named — a bare
`fractal destroy` is refused as ambiguous.

What survives either scope is exactly what was committed to the repository: the
project wiki (committed project memory, never deleted — the command says so as
it finishes), baseline commits, and any branches on the remote.

After `destroy --all`, the repository is as if fractal had never been
initialized; a new `fractal init` starts from zero.

## The guards, across tiers

All three tiers refuse over a **running** node — any live tmux session or live
recorded process group stops the teardown before it touches anything, with the
kill command named in the error. An inconclusive runtime probe refuses while the
node still has something to protect — an unsettled status, or a lingering
`.pgid`, `.socket`, or `.headless` record — naming the tmux and `ps` checks to
run; the teardown never treats missing visibility as proof that a loop is dead.
A settled node keeping none of those records proceeds: that is the state the
teardown's own pre-flight reconcile leaves after healing a dead bare loop on a
blind host, and nothing is left for the refusal to guard. The guards travel with
the scope: `reset` and `destroy <name>` pre-flight only that tree's nodes, so an
ended tree can be torn down while a sibling tree runs, and `destroy --all`
pre-flights every tree before touching any of them. Paused nodes split the
tiers: `delete` refuses over them (resume or kill first), while `reset` and
`destroy` **kill paused nodes as part of the confirmed teardown** — the
confirmation prompt (or `--force`) is what authorizes discarding the frozen
mid-step work their parked worktrees hold. Both also refuse from inside a node
worktree and pre-flight every worktree for locks before removing any, keeping
the non-atomic teardown all-or-nothing in practice.

Remote branches are the deliberate survivor at tiers 2 and 3: reset and destroy
report which branches remain on origin rather than deleting them (only tier 1's
per-node delete removes a remote branch). What was pushed stays recoverable.
