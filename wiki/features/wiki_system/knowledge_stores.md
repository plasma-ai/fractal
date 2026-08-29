---
name: features/wiki_system/knowledge_stores
desc: |
  The two knowledge stores a node works with: the shared project wiki and the
  node's private memory, their locations, audiences, and how fractal creates
  and maintains each.
created: 2026-07-21T04:51:58Z
updated: 2026-07-21T04:51:58Z
---

# features/wiki_system/knowledge_stores

[[features/wiki_system/_index|..]]

***

Every node works with two wikis, distinguished by audience:

- **Project wiki** — the shared record. It lives at `wiki/` under the worktree
  root (or `<project>/wiki` when the node targets a sub-project of the repo) and
  is git-tracked, so it travels with the branch: other nodes see its content
  only through merges. It holds architecture, conventions, and durable
  project-wide knowledge.
- **Memory** — the node's private knowledge base, a second wiki at `memory/`
  inside the node's data directory (`.fractal/<branch>/memory`). Only the owning
  node reads it; merge-up strips the node seed, so memory never reaches the
  parent. It carries working state across iterations.

The iteration prompt hands both locations to the agent as the `WIKI_DIR` and
`MEMORY_DIR` aliases, resolved per node by `fractal/core/render.py`.

## Creation and seeding

`fractal init` creates the project wiki when none exists
(`fractal/core/worktree.py`): the validated project name becomes the wiki name,
and the wiki is seeded with the strict ascii/identifier naming policy so project
pages mirror source-module identifiers. A pre-existing non-empty `wiki/`
directory that is not a wiki (no `.wiki/` marker) is refused, never adopted —
the operator must convert it explicitly with `wiki init`; an empty one is
initialized in place. An existing wiki (one carrying `_index.md`) is adopted as
it is — init leaves tracked files alone — but an index without the tool's
frontmatter stamps (no `created:` line) is flagged with a warning naming the
remedy: run `wiki update --path=wiki` and commit the result before initializing
nodes, since siblings forking from an unstamped index each stamp their own copy
and then conflict on the `created:` line, which the merge driver cannot
regenerate. A second warning fires when the worktree-root `.gitattributes` lacks
the `**/_index.md merge=wiki` line `wiki init` writes for a fresh wiki — git
reads the attribute from the target's own tree, so an adopted wiki without it
conflicts on its index at the first merge where both sides changed the index;
append the line and commit before initializing nodes. The user node's baseline
commit (`fractal/core/commit.py`) then commits the fresh wiki along with the
`.gitattributes` merge attribute that `wiki init` writes, so every child branch
forks from a committed wiki with merge handling in place (see
[[features/wiki_system/merge_behavior]]).

Memory starts empty; the node lays it out as topical pages as it learns.

## Maintenance at commit

The commit pipeline (`fractal/core/commit.py`) treats both stores as wikis: at
every `fractal commit` it runs `wiki update` over the project wiki and over the
node's memory before linting and staging, and a failed update fails the commit —
a broken wiki must never land. Backstop saves (`--force`) and the baseline
commit (`--init`) skip the refresh, since a fail-safe save must never block. The
project wiki is always committable regardless of the node's scope: scoped
commits admit `wiki/` alongside the scope directories.

## Routing knowledge

Facts route by audience: something only the owning node will need goes to
memory; anything another node could reuse goes to the project wiki. A page lives
in exactly one store — the other references it in plain text, because wikilinks
never cross wikis (see [[features/wiki_system/page_conventions]]). The same
split governs todo lists: a private working checklist is memory, a task list
other nodes should track is project wiki, and either is living state pruned as
items complete, never an append-only log.
