---
name: features/wiki_system/page_conventions
desc: |
  Page and frontmatter conventions: naming rules, leaf pages versus folders,
  which frontmatter keys are authored versus regenerated, wikilink scoping,
  and the formatter interactions an author must write around.
created: 2026-07-21T04:51:58Z
updated: 2026-07-21T04:51:58Z
---

# features/wiki_system/page_conventions

[[features/wiki_system/_index|..]]

***

A wiki is an indexed folder tree: every folder carries an `_index.md` that
orients its level and lists its children, and topics without children are
standalone leaf `.md` pages. Fractal's project wikis name pages in ascii
snake_case — the strict ascii/identifier naming policy seeded at `wiki init`
rejects hyphens and spaces, so page names can mirror the source modules they
document.

## Frontmatter

Frontmatter keys split into two tiers, and the split drives both regeneration
and merging (see [[features/wiki_system/merge_behavior]]):

- **Regenerated** — `name` and `updated` are owned by `wiki update`; hand edits
  are overwritten on the next update.
- **Authored** — `title`, `desc`, `created`, `category`, `tags`, and `sources`
  belong to the author. `desc` is human-readable prose (complete sentences
  ending in a period), written as a YAML block scalar (`|`) once it passes ~100
  characters; a one-line `desc` containing `: ` or ` #` is quoted, since the
  unquoted form is invalid YAML. A `title` supplies the page's H1; without one,
  update rewrites the H1 to the page name.

## Page body

Authored content sits below the `***` separator; everything above it (the
frontmatter and, in an `_index.md`, the generated link block) is derived state
the tooling maintains. Each page's entry line in its parent index — name and
description — is pulled from the page's own frontmatter, so a wrong index line
is fixed by editing the page's `desc` and rerunning update, never by
hand-editing the index.

## Wikilinks

`[[...]]` links target pages in the *same* wiki only. Anything outside it —
source files, configs, or the other knowledge store — is referenced in plain
text or backticks; `wiki lint` fails a wikilink that points outside the wiki
(the `outside_link` issue). In-wiki links are written prefix-free from the wiki
root, never with a `./` or `../` prefix: every target is read from the wiki
root, and a `./` or `../` prefix marks a link that leaves the wiki. Link only to
pages that already exist: a forward link to a page a sibling branch has not yet
merged is a stale-link note until the merge lands. Label a sibling-branch index
link with the bare branch name (`[[features/chat/_index|chat]]`) and a link that
crosses top-level branches with the trailing-slash path
(`[[features/radio/_index|features/radio/]]`), keeping labels short enough to
survive the formatter's wrap.

## Authoring pitfalls

Wiki markdown passes through the repo's mdformat hook, which rewraps prose at 80
columns; two of its interactions with wiki syntax slip past `wiki lint`, so
authors write around them:

- **Lone code spans in list items.** A wrapped line consisting solely of a long
  code span inside a list item masks to blank for the linter, which can then
  misread a later `-` item in the same block as a mangled wrapped list marker
  and flag it falsely. Reword so the code span shares its line with prose
  instead of standing alone.
- **Wikilinks broken by wrapping.** When the formatter wraps a long-label
  wikilink mid-link, it escapes the brackets and breaks the link. Lint reports
  the escaped form as a hard formatter-damage issue, but the repair is manual:
  keep wikilink labels short enough to survive the 80-column wrap so the damage
  never lands.
