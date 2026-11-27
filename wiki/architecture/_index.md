---
name: architecture
desc: |
  What fractal is and how it fits together: the node tree model, the
  worktree-per-node design, the agent-provider seam, the central per-tree
  database, and the package layout.
created: 2026-07-21T04:35:35Z
updated: 2026-11-27T23:15:11Z
---

# architecture

[[_index|..]]

[[architecture/agent_providers|agent_providers]]: The core/impl agent-provider
seam: what the agent base class owns, the provider name registry, and how the
backend modules for claude, codex, grok, muse, opencode, and omp slot in.

[[architecture/database|database]]: The central per-tree SQLite database: where
it lives, how every node resolves it, the WAL discipline, row ownership, what
survives node deletion, and the main table families.

[[architecture/node_tree|node_tree]]: The node tree model: dotted branch naming,
the parent/child relationship derived from branch names, the passive root (user)
node, several independent trees in one repository, and the central node
registry.

[[architecture/packages|packages]]: How the packages fit together: the cli,
core, tui, impl, and util packages, the node-machinery seeds, and the shim
pointer dist published beside the main package.

[[architecture/worktrees|worktrees]]: The worktree-per-node design: where
worktrees live, how a child forks its parent's branch, commit scopes, and the
merge topology of per-node commits, no-fast-forward child merges, and squash
merges toward the base.

***

This branch is the structural reference for fractal: the facts of how the system
is built and how its pieces fit together. Fractal runs hierarchical agent loops
— a tree of autonomous nodes, each bound to a git branch and worktree, iterating
on a shared project and merging work up the tree. The rationale behind these
structures (the *why*) lives in the [[design/_index|design]] branch; the
operator's journey through them lives in [[user_flow/_index|user_flow]].

Read the pages in this order for a ground-up picture:

- [[architecture/node_tree]] — the tree of nodes itself: dotted branch naming,
  the parent/child relationship, the passive root (user) node, and the node
  registry.
- [[architecture/worktrees]] — the worktree-per-node design: where worktrees
  live, how a child forks its parent's branch, commit scopes, and the merge
  topology of per-node commits, no-fast-forward child merges, and squash merges
  toward the base.
- [[architecture/agent_providers]] — the core/impl seam: the provider-agnostic
  agent base class, the name registry, and the backend modules that slot in.
- [[architecture/database]] — the central per-tree SQLite database: one file per
  tree in the root node's data directory, the WAL discipline, row ownership,
  table families, and what survives node deletion.
- [[architecture/packages]] — how the code packages, node-machinery seeds, and
  the shim pointer dist fit together.
