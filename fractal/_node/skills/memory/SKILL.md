---
name: memory
description: How to maintain node memory well -- the node's persistent knowledge store.
---

# Memory

Memory (`$MEMORY_DIR`) is the node's durable brain. Read it when you orient and
fold findings back before each iteration ends. Sync may also write to memory
when crucial information arrives via radio. See the `wiki` skill for how memory
relates to the shared project wiki; this doc is the discipline for keeping
memory useful.

Run `wiki --help` and `wiki <command> --help` for the CLI.

## Conventions

- **Write knowledge, not history.** Never reference iteration numbers,
  timestamps, or chronological markers -- a reader shouldn't be able to tell how
  many iterations have run. Before COMMIT, scan memory and any page you promote
  for iteration numbers and run labels and rewrite them out -- promotion carries
  your habits onto shared surfaces.
- **Organize by topic, not time.** Update the existing page for a topic; don't
  append a new entry.
- **Start with a topical layout.** Your FIRST memory write creates the layout --
  one page per topic (`environment`, `decisions`, `state`, `todo` -- rename to
  fit the work; stubs are fine), never a single grab-bag page; when a page
  starts covering two topics, split it. A leaf expecting to finish within an
  iteration or two may keep just the `state` page.
- **One current-state page.** Keep exactly one present-tense `state` page for
  where-am-I / what's-next, overwritten in place -- no "prior run" sections;
  per-iteration narrative belongs in the plan post-mortem, never memory.
- **Pages need `desc:` frontmatter.** A page without it (a near-miss key like
  `description:` counts as without) is invisible to `wiki map`; after adding or
  moving pages, run `wiki update --path=$MEMORY_DIR` so they gain frontmatter
  and index entries. Quote a one-line `desc:` that contains `: ` or ` #`.
- **Fold at phase ends.** When a phase or a child's run ends, collapse its pages
  into durable facts and delete the rest -- memory carries what is still true,
  not what happened.
- **No append-only logs.** If you're adding dated entries, stop -- replace
  outdated content with current understanding.
- **Todo lists are living state.** Keep your private working checklist here as
  current open items, pruned as they complete -- never a done-log. A todo list
  other nodes should see and track belongs in the project wiki instead.
- **Keep indexes lean.** Keep each `_index.md` under ~100 lines below the `***`;
  factor overflow into child pages.
- **Wikilinks stay within one wiki.** Reference anything outside this wiki --
  the project wiki, source files, configs -- in plain text or backticks, never
  as a wikilink; `wiki lint` notes a wikilink that points outside the wiki.
  Write in-wiki links prefix-free (`[[state]]`, never `[[./state]]` or
  `[[../state]]`).
