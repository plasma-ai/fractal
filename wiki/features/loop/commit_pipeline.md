---
name: features/loop/commit_pipeline
desc: |
  The work-product commit pipeline behind fractal commit: scope enforcement,
  wiki index refresh and lint, staging excludes and warnings, subject
  composition, the hook retry, and the force-commit backstop.
created: 2026-07-21T05:04:14Z
updated: 2026-07-21T05:04:14Z
---

# features/loop/commit_pipeline

[[_index|..]]

***

`fractal commit` is the single path a node's work takes into git history. The
pipeline lives in `fractal/core/commit.py`; the COMMIT seed step invokes it at
the end of every iteration, and agents and operators call it directly for
mid-iteration saves. It runs scope check, wiki index refresh, lint, stage,
commit, and push (unless the node was initialized `--local`) as one operation.

## Subject and message discipline

The pipeline composes the commit subject itself:
`<branch>: iteration <run>.<iter> (<message>)`. The message argument is only the
parenthetical, so a message that starts with the branch name or an `iteration`
label is rejected -- it would double-label history. `--init` substitutes the
`init` label for the iteration one. When the caller does not pass the iteration
(the loop passes its live one), the open iteration row supplies the number and
run; with no open row the label falls back to a plain `iteration 0` with no run
qualifier. An optional body paragraph lands below the subject.

## Scope, refresh, and lint

With a scope configured, the pipeline checks the working tree, index, and
untracked files for out-of-scope changes and refuses to commit them; the shared
`wiki/` and the node's `.fractal/` prefix are always committable.
`--ignore-scope` commits out-of-scope changes but still lints; `--force`
bypasses scope, lint, and git hooks alike. Before staging, the pipeline
refreshes both wiki indexes (the project wiki and the node's memory) with the
`wiki` CLI and fails the commit if a refresh fails -- a broken wiki must never
land -- then runs the node's `lint.sh` and surfaces its notices instead of
dropping them. Helper CLIs are resolved from the invoking installation, not
ambient PATH, so a foreign install cannot answer the hook's reads.

## Staging and warnings

Staging appends a fixed exclude set to every sweep: virtualenvs, the central
database and its sidecars, the status and pause markers, crash-stranded
atomic-write temp files, and engine-materialized system skills
(`skills/.system/`) never ride a work commit. Two advisory guards warn without
blocking: workspace files silently eaten by host ignore rules are counted and
reported (fractal's own runtime ignores -- the managed `info/exclude` block by
its line span, so a user line sharing the file still alarms -- and self-managing
ignored directories stay silent), and any staged file at or over 10MB is listed
by name -- an oversized file is usually an accident, but large commits are also
legitimate.

fractal owns its estate staging (`_stage_records`): after the plain adds, any
estate file an ignore rule held out is re-evaluated against fractal-normal rules
alone -- the shipped exclude template plus the repo's committed per-directory
`.gitignore` files -- and force-added when only a machine-local layer (a foreign
`info/exclude` line, `core.excludesFile`) held it. One stray broad exclude line
can therefore no longer silently unstage, or hard-fail, the audit trail canon
requires nodes to commit; the user node's self-ignored seed dir stays untracked
by design. The force-add stages every record it can: a record the add cannot
take -- vanished after the snapshot, unreadable on disk -- is named in a warning
rather than aborting the commit, and both notices report worktree-relative
paths, since a force commit folds them into its body.

## Commit, retry, and the backstop

A sweep that stages nothing is a tolerated no-op, reported rather than failed.
If a pre-commit hook mutates files and aborts, the pipeline re-stages and
retries the commit once -- but only when a pre-commit config exists and
re-staging actually changes the index. Wiki pages (the project wiki and the node
memory wiki) join the retry only when the rewrite preserves wiki structure --
wikilinks, frontmatter, and separators -- and, on a work commit, only when each
touched wiki root that linted clean before the rewrite still lints clean after
it (a pre-existing lint failure is surfaced as a notice and tolerated, mirroring
the pipeline's own lint step); every other node-data page keeps byte-identity,
so any hook rewrite there is refused. A rewrite that breaks a gate is restored
to its authored bytes and fails the commit with remediation guidance. The commit
event is logged from a single emit point keyed on the new sha, so the retry
never double-logs (no event for `--init`, whose baseline has no run lineage),
and a failed event insert warns rather than blocking the save. A `--force`
commit -- the loop's backstop save -- passes `--no-verify` so a failing or
mutating hook can neither block nor rewrite the save, and folds the record
pass's notices and the staged-sweep warnings plus a capped diffstat into its
body so the backstop describes what it saved (and what it deliberately overrode
or refused) from git history alone. `--check` commits nothing and errors if
uncommitted changes exist.
