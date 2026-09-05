---
name: configuration/templates
desc: |
  Node templates carry committed source, config presets, and optional
  defaults. Jinja renders the selected outputs, while recorded inputs
  support drift checks and explicit refreshes.
created: 2026-09-01T05:13:44Z
updated: 2026-09-01T05:13:44Z
---

# configuration/templates

[[_index|..]]

***

A template is any tracked folder in the repository that holds a `config.json` --
the file's presence is what marks the folder as a template.
`fractal node init --template=<path>[@<ref>]` reads the folder from git at the
child's fork commit (never from the working tree), deploys its surfaces into the
new node, and records what it read in the node's `_template.toml`. A template is
ordinary project content -- tracked, versioned, and merged like any other work,
with no reserved location and no name registry: the path is the name, and the
commit it is read at is the version. The machinery lives in
`fractal/core/template.py`.

## Layout

```
<path>/
  config.json              config preset -- and, by its presence, the marker
  _template.toml           optional literal defaults in [values]
  NODE.md                  charter (Jinja source)
  steps/NN-NAME.md         step files (Jinja source)
  scripts/{setup,test,lint}.sh
  skills/<skill>/SKILL.md
  agents/<agent>/<file>    per-agent settings, e.g. agents/claude/settings.json
  _partials/<file>         source-only fragments for Jinja includes
  README.md               required inputs and authoring guidance
```

Every entry but `config.json` is optional; a surface the folder lacks falls back
to the inherit-or-package source (see [[configuration/inheritance]]). Surface
names are exact lowercase: a case-variant folder (`Steps/`) refuses by name. A
`README.md` beside the surfaces describes their inputs. Documentation and
`_partials/` are source-only: they do not deploy or render independently. Keep
shared fragments outside `steps/`, whose files must all be valid steps. The
package seed (`fractal/_node/`) mirrors the same layout, its per-agent files
under `agents/`.

- `NODE.md` seeds a deployment-ready charter. The rendered charter passes the
  fill-sheet gate at init: its `## Instructions` and
  `## Completion Requirements` sections must be present, every `pin:` line must
  resolve to a commit (and match `--pin` when one is given), and every
  `docket: <path>` line must resolve at the pin -- at the fork commit when the
  seed is pinless -- so a stale or truncated commission dies before any worktree
  exists.
- `steps/` seeds the step list and must satisfy the loop's discovery contract --
  at least one `*.md`, an `NN-` digit prefix on every file, one prefix width
  (see [[configuration/steps]]) -- checked at init, so a template that cannot
  iterate refuses before any worktree exists. A file the copy would skip (a
  non-`.md`, hidden, or nested entry under `steps/`) refuses the same way.
  Rendered frontmatter is validated before deployment, so a Jinja expression
  cannot defer an invalid step setting until the loop starts. Render a boolean
  flag with `{{ detached | lower }}` or `{{ detached | tojson }}`: step flags
  require lowercase `true`/`false`, while Jinja's default boolean text is
  `True`/`False`.
- `scripts/` seeds the setup/test/lint scripts as top-level files, the package
  seed's own copy set; an underscore-prefixed, hidden, or nested file refuses
  rather than silently not deploying.
- `skills/` seeds the skill directories wholesale; a loose file directly under
  `skills/` or a hidden skill directory refuses.
- `agents/<agent>/` files deploy into the node data directory's live agent
  config dirs (`.claude/`, `.codex/`, ... -- git-ignored, disk-only). A
  template's file beats the parent's live copy, which beats the package seed,
  and a template plus `node diff` and `node reseed` is the one versioned path
  agent settings have. An entry must name a registered agent's directory -- an
  unknown name or a loose file refuses at init and reseed rather than deploying
  nothing and drifting on every later diff. An `agents/<agent>/skills` entry
  refuses too: that path is the mount of the node's `skills/`, so skill
  directories belong under the template's top-level `skills/`.

A bundled surface is a rival source to inheriting the parent's:
`--inherit=steps` (likewise `scripts` and `skills`) is refused when the template
carries that surface. A template is self-contained -- a symlink inside the
folder refuses init by name; commit the target file in its place. Every rendered
output and every source file it loads must be UTF-8 text; unused source files
are not decoded.

## Path rules

`--template` takes a filesystem path, absolute or relative to the shell's
directory. Fractal resolves it to its containing worktree -- the main checkout
or one of its `.worktrees/` entries -- and records it relative to that worktree
in POSIX form, so tab-completion works from the repo root and from inside a node
worktree, and one folder names the same template from every project in the repo.
Refused at init: a path outside every worktree, the worktree root itself, a `..`
step, a `.fractal` or `.git` component (matched casefolded -- a template is
project content, outside the machinery), a leading `.worktrees` component
(worktree checkouts are tracked at no commit), and a folder whose own name
contains `@` (the ref separator).

## The versioned read

The folder is read from git at the child's **fork commit**: the child branch's
own tip on a `--reset` (the branch already exists), else `--base` or the parent
branch -- exactly what `worktree add` checks out. Uncommitted edits never
deploy, so the habit is edit, commit, then spawn. A folder untracked at that
commit refuses init naming the two remedies -- commit the folder on the branch
the child forks from, or pass `@<ref>` to read it at another commit -- and says
when an uncommitted copy exists on disk.

`@<ref>` (split at the last `@`) reads the folder at another commit. A parent
deep in the tree carries the root's copy only as of its last merge, so
`--template=<path>@<root-branch>` is how a nested spawn reads the root's current
copy; init prints one notice when the root branch's copy differs from the commit
read, naming that form (a path absent on the root is no notice). A
`node reseed --template` re-point prints the same notice; a plain or `--ref`
reseed deliberately re-reads a recorded or named version and stays silent.

Template bytes come from git, so anything fractal's ignore rules keep out of
tracking can never become template content: a `tmp/` folder at any depth, the
agent dot-folders (`.claude/`, `.codex/`, ...), and `**/skills/.system/`
silently deploy nothing. Agent settings therefore live under `agents/<agent>/`,
never under a dot-folder.

## The config preset

The template's `config.json` is a subset of a node's own config keys, typed the
same way, and fills each init flag the spawn left unset: a flag wins over the
preset, and the preset beats an inherited value; the package default covers the
rest, and the merged values pass the same validation the flags do. Only budget,
limit, duration, model, and mode keys may appear -- `agent`, `provider`,
`model`, `effort`, the `max_*` caps, the timeouts,
`step_retries`/`step_retry_backoff`, `interval`/`sleep`/`wait`,
`reserve_budget`, `sync`, and `detached`. Identity and immutable keys (`title`,
`scope`, `base`, ...) and unknown keys refuse at init by name. A null value is
the preset's spelling for unset -- the key defers to the inherit-or-default
source exactly as an omitted key does -- and the rival pacing pair
`sleep`/`interval` fills only when the spawn sets neither.

## Values and defaults

A template's optional `_template.toml` holds literal defaults in `[values]`:

```toml
[values]
role = "researcher"
review = true
checks = ["tests", "documentation"]
```

This source file accepts only the `values` table. Node record fields such as
`path` and `commit` refuse here: source defaults and generated provenance have
different roles. An absent file or empty table supplies no defaults. Metadata
and `config.json` are parsed without rendering and are not standalone outputs;
an explicit Jinja include treats a named bundle file as source. Describe
required inputs in the template's README and omit inputs that have no usable
default; placeholder strings and empty stand-ins are supplied data, not
missing-input declarations.

Values come from three sources, later ones winning:

- The template's `_template.toml` `[values]` defaults, at the template commit.
- `--values=<file.toml>` -- a TOML document whose top-level keys are inputs,
  without a wrapping `[values]` table.
- `--set KEY=VALUE` (repeatable) -- one input whose value is a TOML literal:
  `--set 'role="reviewer"'`, `--set review=false`, or
  `--set 'checks=["tests"]'`.

Strings, booleans, numbers, lists, tables, and TOML date/time values keep their
types. Each override replaces the whole top-level value; nested tables are not
deep-merged. Input names must match `[a-z_][a-z0-9_]*`; `true`, `false`, `none`,
`not`, and `self` are reserved by Jinja and refuse. Nested table keys are
ordinary TOML keys. Unused inputs are allowed, so a values file may serve
several templates. Tables render in recursively sorted key order; lists preserve
their supplied order. Use a list when order carries meaning.

`--pin=<sha>` supplies the `pin` input and overrides a template default. A `pin`
from `--set` or `--values` that disagrees with `--pin` refuses, while equal
spellings pass. Without `--pin`, the merged `pin` input applies. A pin must be
text, and the rendered charter still passes the pin and docket checks.

## Jinja rendering

Only selected deployment outputs render independently. Jinja supports
expressions (`{{ role }}`), statements (`{% if review %}`), comments
(`{# guidance #}`), includes, imports, macros, inheritance, loops, and raw
blocks. Missing inputs used by an output refuse before any worktree exists; an
input referenced only in an unused branch is not required. Syntax errors and
missing required includes identify the source file and line.

Put reusable prose in template source rather than values. For example, a charter
can use the defaults above and a required per-node `mission`:

```jinja
## Instructions

Act as {{ role }}.
{{ mission }}
{% include "_partials/foundations.md" %}

## Completion Requirements

{% for check in checks %}
- Complete {{ check }}.
{% endfor %}
```

An include names a template-relative file from the same committed bundle.
`../shared/mission.md`, absolute paths, and files outside the template cannot be
loaded. A missing include refuses unless its author uses Jinja's explicit
`ignore missing`. All includes read immutable source, so one output cannot
change what another output includes. Values are literal data: a supplied string
containing `{{ other }}` stays that string and is never rendered again.

Rendering uses strict undefined values, disabled HTML escaping, preserved
trailing newlines, and no automatic block whitespace trimming. Jinja normalizes
source line endings to LF; inserted strings remain literal. Raw blocks or
`{{ '{{' }}` produce literal braces. The immutable sandbox prevents input
mutation; nondeterministic `random` and `lipsum` helpers are unavailable, and
templates receive no host objects, clocks, or environment access. Python string
formatting and date/time methods that can read host state are unavailable; use
Jinja's formatting filters for text.

Render literal data or consume helper results, for example
`{{ targets | map('lower') | join(', ') }}`. Do not stringify functions,
iterators, namespaces, or other opaque helper objects: their representations can
contain process-specific memory addresses and are outside the reproducible
authoring contract.

Seed-time Jinja is separate from the prompt-time `$UPPER` variables the loop
renders each iteration (see [[features/loop/prompt_assembly]]). `$VAR` and
`${CURRENT_BRANCH%.*}` pass through Jinja untouched. Deployed bytes are the
committed source rendered with the recorded inputs, which `node diff`
reproduces; comparing directly against unrendered source does not measure drift.

## The provenance record

`_template.toml` in the node's data directory records what seeded the node: the
worktree-relative POSIX `path`, the full 40-hex `commit` actually read (which
can differ from the init event's fork sha when the parent commits between the
read and `worktree add`), the optional `include` or `exclude` listing, and the
`[values]` table of resolved inputs, including defaults and unused inputs. Its
presence is what marks a node as seeded from a template. It is a node record
file -- the commit pipeline's estate content law counts it beside `NODE.md` and
`config.json` (see [[features/loop/commit_pipeline]]) -- so it commits with the
seed, and a `--reset` without `--template` drops it, the forget-unless-repassed
rule init flags have. The record is hand-editable and validated where it is
read: `node diff` and `node reseed` refuse a path that is not worktree-relative,
a commit that is not a full 40-hex sha, `include` beside `exclude`, or a
malformed listing or values table.

The record stores values, not references to the source defaults or the external
values file. Editing those source files does not change an existing node.
Editing the node's recorded values does not update its rendered files until an
explicit reseed, and reseed preserves the charter.

## Include, exclude, and the effective set

`--include` and `--exclude` (repeatable; mutually exclusive; they require
`--template`, as `--values` and `--set` do) name template-relative paths; a
directory entry covers its subtree, and there are no globs. The **effective
set** is every deployment output minus `exclude`, or only `include` (the
`config.json` marker always stays). Init deploys the effective set, and
`node diff` and `node reseed` judge by it, so an excluded step stays gone across
every reseed -- a deliberate trim is recorded, never remembered. An entry
matching nothing, naming `config.json` or `_template.toml`, or selecting a
source-only path such as `README.md` or `_partials/`, refuses at init; on `diff`
and `reseed` a listing entry the template no longer carries only warns, since
the record may outlive the file it named. A fully trimmed surface falls back to
the inherit-or-package source.

The listing selects outputs, not the source available to Jinja. For example,
`--include NODE.md` still allows its `_partials/` includes. Excluding a step
prevents that step's deployment and independent rendering, but another selected
output may include its source. An exclusion does not remove text transitively
from included documents.

## node diff

`fractal node diff <node>` shows a node's drift from its recorded template. It
re-renders the recorded folder at its recorded commit with its recorded values,
applies the effective set, and compares each bundle surface against the node's
live copy: `NODE.md`, `steps/`, `scripts/`, and `skills/` in the node data
directory, and each `agents/<agent>/` file as the live `.<agent>/` file. A live
symlink (the skills mount, a linked credential) and a file the bundle does not
carry are never judged; a bundle file the node lacks is drift. Literal braces
are valid output and do not constitute drift on their own. The command prints a
unified diff per drifted file and exits 0 when clean, 1 on drift, and 2 on a
command error (a node with no template recorded included), so scripts branch on
the exit code. The recorded commit always resolves while it is reachable, so a
folder moved or deleted on a later commit still diffs at its recorded sha.

Diff uses exactly the recorded values, without merging source defaults again.
Removing a required value from the record causes a rendering refusal.

## node reseed

`fractal node reseed <node>` rewrites the node's seed surfaces from its recorded
template: it re-renders the recorded folder at its recorded commit with the
recorded values and effective set, then rewrites `steps/`, `scripts/`,
`skills/`, and the per-agent files -- files the node lacks are added and files
it has are overwritten, so the node matches its template's effective set;
nothing is ever deleted, and `NODE.md`, `config.json`, and `memory/` are never
touched, so an operator's charter edits and the node's memory survive. Skill
directories merge file by file (a node-added file survives), and the `skills`
and auth symlinks in the agent dirs stand. The verb records a `reseed` event and
advances the record's `commit` to the commit actually read.

A bare reseed uses exactly the recorded values. An explicit `--ref` or
`--template` refresh fills missing keys from the target template's defaults,
then overlays every recorded value, including values originally obtained from a
default. A changed default therefore does not change an existing value. The
complete merged map is recorded; a new required input with no default refuses
before any live file is written. Successful reseeding can leave charter drift,
because `NODE.md` is preserved even when the template or its recorded inputs
change.

- `--ref=<committish>` reads the recorded folder at another commit -- how a
  template improvement reaches an existing node. A ref at which the recorded
  path is absent (the folder moved or was retired) refuses naming the re-point
  remedy.
- `--template=<path>[@<ref>]` re-points the node at another folder, read at the
  node branch's own tip when the value names no ref: the new path and the commit
  read land in `_template.toml`, with the resolved values and unchanged listing
  -- one explicit command follows a moved template. The re-point prints the same
  root-differs notice init does when the root branch holds a different copy of
  the folder. Mutually exclusive with `--ref`.
- `--force` reseeds even while the node is active or paused.

Reseed rewrites the live steering surface, so without `--force` it refuses over
an active or paused node, and it always refuses from the node's own worktree --
a node may not edit its own seed.

## Who edits a template

Nothing in `fractal commit` or the merge knows templates exist: a template is
project content, so the scope rule that judges every path governs it (see
[[features/loop/commit_pipeline]]). A node whose scope covers the folder edits,
commits, and lands a template; another node's edit is refused at commit and
again at the merge footprint; the operator edits templates in the main checkout
with raw git. Scoping nodes away from template folders is how an operator keeps
template canon apart from ordinary work.

## The credential guard

A leaked credential would do harm wherever it deploys, so every materialize --
init, `node diff`, and `node reseed` alike -- refuses a credential-named file
anywhere in the template: `auth.json`, `credentials.json`, `*.key`, `*.pem`,
`*.p12`, `*.pfx`, `id_rsa`, `id_ed25519`, `id_ecdsa`, `id_ecdsa_sk`,
`id_ed25519_sk`, `id_dsa`, `*.ppk`, matched case-blind and naming the file,
whatever commit carried it. A dot-file refuses only under `agents/`, the one
subtree that deploys into live agent dirs (dot-files hold live agent state and
credentials, never template content; a template's own tracked dot-files
elsewhere are not credentials by name). Credentials never deploy from a template
-- a node links its own at seed time. The guard is a name list on the read side,
not a scanner: a key inlined inside a legitimate config file passes any name
check and stays review territory.
