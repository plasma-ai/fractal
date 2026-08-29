---
name: user_flow
desc: |
  End-to-end operator journeys: initializing a fractal, configuring and
  launching nodes, monitoring and steering, finishing and merging, pausing
  and resuming, and tearing down.
created: 2026-07-21T04:35:35Z
updated: 2026-08-29T11:30:50Z
---

# user_flow

[[_index|..]]

[[user_flow/continue_resume|continue_resume]]: The two ways to interrupt and
re-enter a run: stop and continue end a run at a clean boundary and later arm a
fresh one, while pause and resume freeze an open run in place and thaw it,
including tree-wide.

[[user_flow/finishing/_index|finishing/]]: How work concludes: the finish signal
and who sends it, what the squash-merge does — the machinery restore, the
merge-base advance, and the merge guards each have a page here — and how
finished work climbs the tree to the base branch and the operator's review.

[[user_flow/getting_started|getting_started]]: From a bare repository to a
running node: installing fractal, initializing the tree with fractal init,
creating a node with fractal node init, authoring the NODE.md contract, and
launching with fractal node start.

[[user_flow/operating|operating]]: The operator's role while a tree runs:
monitoring nodes through list, status, activity, the TUI, and radio; steering
through directives, NODE.md edits, chat, and retunes; and how the passive root
node participates.

[[user_flow/teardown|teardown]]: The three teardown tiers and their guards: node
delete removes one subtree, fractal reset clears a tree's worktrees while its
history survives, and fractal destroy removes one tree by name or, with the all
flag, the whole fractal as the full inverse of init.

***

This branch walks the operator through fractal chronologically — from a bare
repository to a merged, torn-down tree. Each page is a journey: it names the
commands and what the operator sees and decides at each stage. The complete
flag-by-flag reference lives in the configuration branch.

The journeys, in lifecycle order:

- [[user_flow/getting_started]] — install, initialize the tree, create and brief
  a node, launch it.
- [[user_flow/operating]] — the operator's role while a tree runs: monitoring,
  steering, and the passive root node.
- [[user_flow/finishing/_index|user_flow/finishing]] — how work concludes: the
  finish signal, what merge does, and how finished work reaches the base branch
  for review.
- [[user_flow/continue_resume]] — the two interrupt-and-re-enter pairs:
  stop/continue versus pause/resume, and the tree-wide brake.
- [[user_flow/teardown]] — the three teardown tiers, what each removes and
  preserves, and the guards over running or paused nodes.
