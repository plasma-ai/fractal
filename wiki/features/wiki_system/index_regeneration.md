---
name: features/wiki_system/index_regeneration
desc: |
  How derived wiki state is regenerated: what wiki update rewrites, the
  update-then-lint loop, issues versus advisory notes, and where the commit
  pipeline runs the refresh automatically.
created: 2026-07-21T04:51:58Z
updated: 2026-07-21T04:51:58Z
---

# features/wiki_system/index_regeneration

[[features/wiki_system/_index|..]]

***

`wiki update` rewrites whatever drifted from the generated form: index link
blocks, regenerated frontmatter fields, and CRLF line endings. It restores a
missing `.wiki/settings.json` and prunes index rows whose target is gone,
announcing each removal. `--check` reports pending changes without writing (exit
1 when anything would change), and narration condenses to one count line per
category unless `--full` prints every line.

`wiki lint` is the companion check: it reports what update would rewrite plus
real defects, separating **issues** (must fix; exit 1) from **notes** (advisory,
on stderr; never the exit code). Stale links in prose are notes — an expected
transient while sibling branches build pages in parallel — while a broken row in
a generated index link block is a hard issue, fixed by repairing the target or
by the next `wiki update`, which prunes the row. Lint validates structure, not
content truth.

The working loop is edit, `wiki update --path=<root>`,
`wiki lint --path=<root>`, repeat until lint exits 0.

## Automatic refresh at commit

The fractal commit pipeline (`fractal/core/commit.py`) runs `wiki update` over
both the project wiki and the node's memory before every commit, and a failed
update fails the commit. Force (backstop) and baseline commits skip the refresh.
Indexes therefore stay mechanically current across commits and merges —
hand-refreshing after a merge is unnecessary; the parent's integration job is
repairing what lint reports, not rerunning update (see
[[features/wiki_system/merge_behavior]]).
