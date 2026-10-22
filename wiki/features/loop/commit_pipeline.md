---
name: features/loop/commit_pipeline
desc: |
  The work-product commit pipeline behind fractal commit: scope enforcement,
  wiki index refresh and lint, staging excludes and warnings, the memory index
  rebuild, subject composition, the hook retry, the force-commit backstop, and
  the report log.
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
untracked files for out-of-scope changes and refuses to commit them -- a rename
counts as both its paths, as `merge.sh`'s footprint listing reads it; the shared
project wiki (`wiki/` unless the project's `.fractal/.settings.json` names
another folder; see [[features/wiki_system/knowledge_stores]]) and the node's
`.fractal/` prefix are always committable. The boundaries are one record
(`Scope`: the project-prefixed roots, the node data-dir prefix, the project wiki
prefix, and the estates prefix) resolved by `scope_boundaries` and judged by
`out_of_scope` -- a sub-project node with no roots is bounded to its project
dir, a repo-root node with no roots is unbounded. The worktree-root
`.gitattributes` sits outside every root and is admitted only while the whole
change is init's own edit -- HEAD's content followed by exactly the two lines
the wiki tool appends (`# Wiki index merge driver`, `**/_index.md merge=wiki`);
any other added or removed line makes the file an ordinary out-of-scope path.
The same record backs `merge.sh`'s footprint check through the private
`fractal node _scope` command -- the staged paths outside `.fractal/`, with
`--attributes-ok` passed under the same whole-change rule applied to the
target's staged copy -- so a squash is judged by exactly the law the commit
enforces (see [[user_flow/finishing/_index|user_flow/finishing]]). Before it
judges, `_scope` runs `Node.check_wiki`, which recomputes the node's wiki folder
by the spawn rule and refuses a `config.json` hand-edited to another folder,
which would otherwise move the exemption (or, for a node held to its project's
committed setting, a setting changed on the root branch since the spawn).
`--ignore-scope` commits out-of-scope changes but still lints; `--force`
bypasses scope, lint, and git hooks alike. Before staging, the pipeline
refreshes both wiki indexes (the project wiki and the node's memory) with the
`wiki` CLI and fails the commit if a refresh fails -- a broken wiki must never
land -- then runs the node's `lint.sh` and surfaces its notices instead of
dropping them. Helper CLIs are resolved from the invoking installation, not
ambient PATH, so a foreign install cannot answer the hook's reads.

A node's `--init` baseline (`fractal commit "configure <name>" --init`, the
child's configured seed before its start) skips the refresh and the lint, so it
carries fractal's setup alone: the node's own data dir under `.fractal/` and
init's `.gitattributes` edit. Any other change -- project content, material
copied into the node's tree -- refuses the baseline by name before anything is
staged, and goes in an ordinary commit, which the lint gates. The user node's
baseline is a separate path (`commit_user_init`) that stages its own fixed set.

## Staging and warnings

Staging appends a fixed exclude set to every sweep: virtualenvs, the central
database and its sidecars, the status and pause markers, the headless runtime
marker and its appended output (`.headless`, `headless.log`), the config write
lock (`config.json.lock`), crash-stranded atomic-write temp files,
engine-materialized system skills (`skills/.system/`), and the wiki tool's
self-ignored derived cache (`.wiki/cache/`) and per-machine Obsidian vault
config (`.obsidian/`) never ride a work commit -- the baseline's force-add
included. A cache a baseline once force-tracked is dropped from the index by the
next work commit; the on-disk copy stays, and the cache's own ignore holds from
there. Two advisory guards warn without blocking: workspace files silently eaten
by host ignore rules are counted and named (fractal's own runtime ignores -- the
managed `info/exclude` block by its line span, so a user line sharing the file
still alarms -- and self-managing ignored directories stay silent). The warning
lists the first ten paths, each with the rule that ate it as `check-ignore -v`
reports it (source file, line, and pattern; the shared `info/exclude` reads
repo-relative and any other absolute source as `core.excludesFile`, since a
force commit folds the warning into its body), then counts the rest, and labels
them repository or host rules, not node-record refusals -- the estate law's
refusal warnings print beside it and the two must not read as one count, so an
estate file the commit left unstaged (refused on either staging path, or a
record the force-add could not stage) is theirs to name and never lists here,
nor does the collapsed directory entry holding one. A second guard lists any
staged file at or over 10MB by name -- an oversized file is usually an accident,
but large commits are also legitimate. A third names each staged addition whose
top-level name matches an entry of the node's own folder (`plans/x.md` beside
`.fractal/<node>/plans`) with the entry it shadows. The path may be a node file
written at the worktree root by mistake or real project work, so the warning
states both outcomes: move node files under the node's folder, ignore it for
project files (an agent told only "mistake" would move project files into its
folder, where a merge never carries them). Staged additions are listed with
`--no-renames`, so a node file moved to the root counts as the addition it is. A
top-level name the project already had at the node's base -- the fork sha
`Files` anchors `base` on -- is the project's own and stays silent; so is a
merge or squash merge in progress, whose additions are not the node's writes,
and `--init` skips the guard. Any failure inside the check (a locked record read
among them) skips the warning, never the commit. Those paths are staged, so they
never count in `Held out`; a force commit folds this warning into its body with
the others. Whenever an estate file was left unstaged or the ignore warning
fired, the command's last line repeats both counts (`Held out: ...`, non-zero
parts only): a hook's or gate's long output prints between the warnings and the
end, and a no-op commit is exactly when a node would otherwise read the tree as
clean.

## The estate content law

What a node's own directory may fold into git history is one allowlist's call
(`_is_committable`), and it bounds both estate staging paths -- the plain adds
and the ignore-rule override alike. That single law is the point: bounding only
the override inverts containment against risk, refusing a parked dotenv loudly
wherever a host rule already fenced it while sweeping the same file into history
wherever nothing did, which is the default state of a fresh clone.

The law describes everything an estate legitimately holds. Records are the
canon-required surfaces -- the estate root's `NODE.md`, `config.json`, and
`_template.toml` (the template provenance record, see
[[configuration/templates]]), and the `memory/`, `plans/`, `scripts/`,
`skills/`, and `steps/` directories -- and only at the text and structured-text
suffixes a record is written in, so no pass can stage a key, a certificate, an
archive, or a binary. Beside them sits the estate's own tool state, which is
committed content rather than machine state: git's empty-directory placeholder
(`.gitkeep`), which exists only so a bare record dir survives a clone, and the
wiki tool's settings directory (`.wiki/`), whose declared-root marker the wiki
CLI reads the memory wiki back through (the tool's derived cache self-ignores,
and its plugin binaries install to an ignored `.obsidian/`). Every other
dot-named path is machine or agent state and stays out. Only new content is
judged: what an estate already tracks keeps committing, so the law gates what
enters history and never the upkeep of history a node already owns.

Content the law refuses is withheld from the sweeps by name and reported, never
staged and never silently dropped. The refused paths themselves are named as
pathspec exclusions rather than the estate as a whole, because an exclusion
matching an ignored path fails the whole add -- and an ignored path is already
outside the sweep.

fractal owns its estate staging (`_stage_records`): after the plain adds, any
estate file an ignore rule held out is re-evaluated against fractal-normal rules
alone -- the shipped exclude template plus the repo's committed per-directory
`.gitignore` files -- and force-added when only a machine-local layer (a foreign
`info/exclude` line, `core.excludesFile`) held it, subject to the same content
law. A stray broad exclude line therefore neither silently unstages nor
hard-fails the audit trail canon requires nodes to commit; the user node's
self-ignored seed dir stays untracked by design. The force-add stages every
record it can: a record the add cannot take -- vanished after the snapshot,
unreadable on disk -- is named in a warning rather than aborting the commit, and
both notices report worktree-relative paths, since a force commit folds them
into its body.

## The memory index rebuild

The memory wiki indexes every file on disk, so a file the estate law refuses (a
binary, an archive, a log) gains a row in memory's index -- from the commit's
own refresh, or from the node's own `wiki update` between commits -- linking a
file the commit leaves out. After staging, `_rebuild_memory_index` reads the
staging pass's own list of estate files left unstaged; when none sit under the
node's `memory/` it stops, so an ordinary commit pays nothing. Otherwise
`_restage_memory_indexes` checks the staged memory tree -- the exact bytes about
to commit, `.wiki/settings.json` included -- out of the git index into a temp
folder under the system temp directory and runs `wiki update` there. The folder
sits outside every repository because inside one the wiki would apply that
repository's ignore rules. The temp tree leaves out what the wiki's own ignore
check skips (the wiki's `Wiki._check_ignore`: `git check-ignore --no-index`
against the repository's `.gitignore` files and `.git/info/exclude`, with
`core.excludesFile` pinned out, and off when the memory root itself is ignored),
so a record force-added past a repository rule -- untracked or already tracked
-- gains no row the refresh never gave it, while a record only a host rule holds
keeps the row the refresh gives it. A folder holding only left-out files keeps
its index, rebuilt like any other without their rows: the commit carries that
index, so the parent's row links a folder the commit has, and no commit deletes
an index the node may have written.

Each rebuilt index whose bytes differ from what is staged is written back to the
git index and the working tree, which keeps the tree clean for the loop's
`--check`; page writes are discarded with the temp folder. The rebuild starts
from the staged bytes, so the node's own edits to an index's description and to
the text below its separator survive. An index whose rebuild differs from HEAD's
copy only in the frontmatter's `updated:` line commits HEAD's bytes, old stamp
included -- otherwise a refused file sitting in memory would move the stamp at
every commit. In the working tree a refused file's row disappears at each
commit; the next refresh adds it back locally, and the next commit drops it
again.

The rebuild runs inside the staging closure, so the hook retry's re-stage redoes
it. It runs on `--force` saves too, since the node's own refresh writes the rows
a backstop would otherwise commit; the `--init` baseline skips it (its memory is
the fresh seed), and so does a commit with no `wiki` CLI. A rebuild that changes
an index adds `Memory index rebuilt without N file(s) the commit leaves out`
beside the unchanged estate warning that names each file. Any failure warns in
one line (`Warning: memory index not rebuilt (...)`) and the commit proceeds
with the indexes as staged. Both lines ride with the record pass's notices, so a
force commit folds them into its body.

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
to its authored bytes and fails the commit with remediation guidance. Node
records pass through the repository's hooks like any staged file, so a hook that
rewrites one -- a formatter reflowing a `.py` record, or trailing-whitespace
stripping a `.patch` record's single-space context lines -- fails each ordinary
commit that stages it, until the iteration's backstop commit, which bypasses
hooks, lands the record as written. A hooked repository should keep rewriting
hooks off `.fractal/` (for example `exclude: ^\.fractal/` in its pre-commit
config), as the failure message says. The commit event is logged from a single
emit point keyed on the new sha, so the retry never double-logs (no event for
`--init`, whose baseline has no run lineage), and a failed event insert warns
rather than blocking the save. A `--force` commit -- the loop's backstop save --
passes `--no-verify` so a failing or mutating hook can neither block nor rewrite
the save, and folds the record pass's notices and the staged-sweep warnings plus
a capped diffstat into its body so the backstop describes what it saved (and
what it deliberately overrode or refused) from git history alone. `--check`
commits nothing and errors if uncommitted changes exist -- counting only work
the stage could commit, so the stage's own excludes and the estate content law
both ride along: dirt no pass may ever stage would otherwise read as permanently
dirty and fire the backstop every iteration over a file that can never clear.

## The report log

The pipeline returns its report -- the lint notices, staging warnings, the
`Held out` line, git's commit output, the push notice -- and the CLI prints it
only after the commit and push are done. A hook's output reaches it only through
a failed commit's error text (no pre-commit config to retry from, a re-stage
that changes nothing, or a retry that fails again); a passing hook writes to
git's stderr, which `_commit_once` does not keep, and a successful retry reports
the retried commit, not the first attempt's hook output. A caller whose output
is gone by then (an agent's shell redirect whose tool session ended mid-commit)
loses that report while the commit lands: the CLI's broken-pipe handling exits 0
and nothing else keeps it. So `Node.commit`, the layer `fractal commit` calls,
appends every child-node report to `<node_dir>/tmp/commit.log` before returning,
and a failed run's `Error:` text in place of the report. Each entry opens with
`=== Commit at <UTC time> (<stamp>) ===`; the stamp is the sha HEAD moved to,
read before and after the pipeline, so the pipeline keeps returning the report
alone and the commit event's `metadata` stays the bare sha the `Files` anchors
read. A run that moved nothing stamps `no commit`, or `failed` when it raised; a
failed push after a landed commit still stamps that commit's sha.

`--check` commits nothing and logs nothing -- the loop's per-iteration dirty
check calls the pipeline directly either way. The loop's backstop saves also
call the pipeline directly and stay unlogged: they bypass hooks and fold their
notices, warnings, and diffstat into the commit body, so they describe
themselves in git history. A child's `--init` baseline logs like any commit. A
user node's baseline (`commit_user_init`) does not: on a tracked seed its
`git add -f` over the seed dir honors only the stage excludes, which do not list
`tmp/`, so a logged baseline would sweep the log into the next baseline on the
base branch.

`tmp/` sits in fractal's managed `info/exclude` block, and the estate content
law refuses it on both staging paths (it is no record dir), so the log never
rides a work commit or counts in `Held out`. The `--continue` restore runs
`git clean -fd` without `-x`, which leaves ignored files alone, so the log
survives `--clean`. It appends without a size bound, as `headless.log` and the
`tmp/err/` snapshots do; nothing in the node directory rotates. A failed write
-- an unwritable `tmp/` -- warns on stderr in one line through the node's logger
and never fails the commit it describes, mirroring the commit event's
best-effort insert.
