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
  project-wide knowledge. A project whose `wiki/` holds other content names
  another folder in its tracked `.fractal/.settings.json` (`{"wiki": "docs"}`);
  see [[#The project wiki folder]].
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
commits admit the project wiki alongside the scope directories.

## The project wiki folder

The folder is one setting per project, fixed into each node at init.
`fractal init` reads the project's tracked `<project>/.fractal/.settings.json`
before writing anything (`worktree.read_wiki_setting`) and records its `wiki`
value in the user node's config, and the `--init` baseline refuses a file naming
another folder than the one recorded. Each spawn reads the setting committed on
the tree's root branch (`worktree.committed_wiki_setting`), never a node's
checkout: a child in its parent's project copies the parent's value, refusing
when the root branch names another folder, and a child that selects another
sub-project takes that project's own setting. The value rides `init.sh --wiki`,
and `Node.wiki_prefix` resolves it against each node's project. The file is the
repository's source of truth -- committed with the project, so no launch flag
can be forgotten -- and a node editing its own copy changes nothing, since merge
restores the target's `.fractal/` and the edit never lands. A node's own
recorded value is checked again at merge: the footprint check's
`Node.check_wiki` recomputes the folder by the same spawn rule
(`node._child_wiki`; with the parent checked out nowhere, the setting committed
on the root branch, which spawn held the parent's folder to) and refuses a
`config.json` hand-edited to another valid folder, naming both. A depth-1 node's
parent is the user node resolved by config (`Node.resolve_user`), never by the
root branch's checkout: a root checked out in a linked worktree carries no seed
there, and its folder would read as the default. A node held to the committed
setting (another sub-project than its parent's, or a parent checked out nowhere)
reads it as it stands at the merge, so a setting changed after the spawn refuses
too; that refusal names the setting and points at committing the recorded folder
back or re-spawning the node, never at `config.json`, whose edit to the new
folder would move the exemption. Everything that names the project wiki reads
the recorded folder: the commit scope exemption and the merge footprint check
(one law, `commit.scope_boundaries`), the commit-time and merge-time index
refresh (a merge into a `--base` branch that is no node takes the merging node's
folder), the `node init` base-ref precondition, `WIKI_DIR`, the seeded
`lint.sh`, the unmerged-work check of `node delete`, and the `destroy` report
(which falls back to `wiki/` when the stored value is invalid, so a bad
hand-edit never blocks the teardown). The wiki CLI, the `merge=wiki` driver, the
tool's `.wiki/` state directory, and the memory wiki keep their names. With no
setting the folder is `wiki` and no config records the key, so a project that
names none uses `wiki/` at every site. The default is spelled by omitting the
key; the settings file refuses `"wiki": null`.

## Routing knowledge

Facts route by audience: something only the owning node will need goes to
memory; anything another node could reuse goes to the project wiki. A page lives
in exactly one store — the other references it in plain text, because wikilinks
never cross wikis (see [[features/wiki_system/page_conventions]]). The same
split governs todo lists: a private working checklist is memory, a task list
other nodes should track is project wiki, and either is living state pruned as
items complete, never an append-only log.
