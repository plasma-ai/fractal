---
name: wiki
description: The wiki CLI and the node's two knowledge bases -- project wiki and memory.
---

# Wiki

A wiki is an indexed folder tree of `_index.md` files. A node works with two:

- **Project wiki** (`$WIKI_DIR`) -- shared across nodes; per-branch, reaching
  others only through merges (use `fractal radio` for live coordination). Holds
  architecture, conventions, and patterns; contribute durable project-wide
  knowledge here.
- **Memory** (`$MEMORY_DIR`) -- this node's private knowledge base. See the
  `memory` skill for how to maintain it.

Todo lists follow the same split: a private working checklist lives in memory; a
task list other nodes should see and track lives in the project wiki. Either way
it is living state -- current open items, pruned as they complete -- never an
append-only log.

Run `wiki --help` and `wiki <command> --help` for the CLI (init, update, lint,
map, search, read). Always pass `--path` (`$WIKI_DIR` or `$MEMORY_DIR`) -- a
bare command resolves the enclosing wiki by walking up, else `{cwd}/wiki`, which
from the node's cwd is the project wiki at best and never memory, so an omitted
`--path` silently targets the wrong wiki or errors. Run
`wiki update --path=<dir>` after adding, moving, or deleting pages;
`wiki lint --path=<dir>` validates structure.

## Editing discipline

Name pages and folders in ascii snake_case (`command_core`, not `command-core`)
-- both wikis enforce identifier-safe names (the project wiki's mirror the
source modules they document), so `wiki lint` rejects hyphens and spaces.

`wiki update` regenerates derived state: each page's entry line in `_index.md`
(name and description) is pulled from the page's own frontmatter, so fix a
`desc:` on the page and rerun update -- hand edits to an index line are
overwritten by the next update, and each page's H1 is rewritten to its name
unless an authored frontmatter `title:` supplies the heading, which update keeps
and renders as the H1 (index entry lines still show the name). `wiki lint` is
regenerate-and-compare: it prints the diff `wiki update` would apply plus any
real defects, and separates issues (must fix) from advisory notes. Work the loop
-- edit pages, `wiki update`, `wiki lint` -- until clean (clean = lint exits 0;
scripts branch on the exit code, not the prose summary); lint validates
structure, not content truth, so verify facts against your sources yourself.
Frontmatter is read as strict YAML: quote a one-line `desc:` that contains `: `
or ` #` (`desc: "Note: x."`), since the unquoted form is invalid YAML.

## Cross-linking

Cross-reference aggressively -- the links between pages are the wiki's primary
value -- but **link only to pages that already exist.** Sibling nodes build
their pages in parallel, so a page you would link to may not exist yet: defer
that forward link rather than emit a wikilink to a not-yet-created page. Stale
sibling links are an expected transient, not a failure -- `wiki lint` reports a
stale link in index or page prose as an advisory note (exit 0), so never stall
an iteration chasing them. Broken links in the generated index link block -- the
rows `wiki update` maintains -- are the exception: each is a hard issue, fixed
by repairing the target or by rerunning `wiki update`, which prunes the row. The
**parent** reconciles stale sibling links when children merge up: indexes
refresh mechanically at commit and merge, so its integration job is repairing or
pruning what lint reports, not rerunning `wiki update` -- plus refreshing any
navigation or status tables it authored (lint cannot see a stale "in flight" row
for a page that has since merged).

Wikilinks also stay inside the wiki you are writing in. A `[[...]]` link targets
another page in the same wiki; anything outside it -- source files, configs, or
the other knowledge base (project wiki vs. memory) -- is referenced in plain
text or backticks, never linked; `wiki lint` notes a wikilink that points
outside the wiki. Write in-wiki links prefix-free, from the wiki root
(`[[overview]]`, `[[core/overview]]`), never `[[./overview]]` or
`[[../overview]]` -- a `./` or `../` target reads from the page's folder and
marks a link that leaves the wiki, so lint reports one that lands inside it.

## Structure

Lay the wiki out as topical folders from the start: each domain area gets a
folder with its own `_index.md`, and pages join the folder that owns their
subsystem. A flat root is fine below ~6-8 pages; planning more, create the
folders before the pages -- the directory tree is what makes a wiki browsable,
searchable, and navigable, and it should mirror the domain the way a good module
layout mirrors a design. A shape to aim for:

```text
wiki/
  _index.md
  <subsystem>/
    _index.md
    <topic>.md
    ...
  <subsystem>/
    _index.md
    <sub-area>/
      _index.md
      <topic>.md
      ...
    <sub-area>/
      ...
    <topic>.md
    ...
  ...
```

Nest deeper as areas grow: when a folder accumulates pages spanning more than
one concern, split them into sub-folders, each with its own `_index.md`. Every
index is the table of contents for its level, so structured nesting is what
keeps the wiki navigable at any size -- a reader answers "where does this live?"
by descending a few links instead of scanning one long list, and related pages
sit beside each other where cross-links suggest themselves.

A grown flat wiki shards into folders the same way after the fact: move related
pages into a subfolder and rerun `wiki update` -- indexes regenerate around the
new layout, and inbound links the move broke surface as stale-link notes to
repair. Before dumping a big tree, preflight with `wiki map --stat` (a one-line
size summary of what the same flags would print) and bound the dump with
`--depth` and `--desc-limit=<n>` -- descriptions print untruncated by default.
