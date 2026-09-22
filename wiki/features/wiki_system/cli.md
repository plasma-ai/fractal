---
name: features/wiki_system/cli
desc: |
  The wiki command-line interface: every verb's contract, how a command
  resolves which wiki it targets, and the exit-code discipline scripts rely
  on.
created: 2026-07-21T04:51:58Z
updated: 2026-07-21T04:51:58Z
---

# features/wiki_system/cli

[[features/wiki_system/_index|..]]

***

The `wiki` executable ships with the `plasma-wiki` dependency and is the sole
interface to both knowledge stores. Its verbs:

- `wiki init [name]` — create a wiki with a root `_index.md`. The name defaults
  to the project (cwd) name or the `--path` folder name; naming is lenient by
  default, and stricter policies are opt-in per wiki via `naming.validate` in
  `.wiki/settings.json`, which `--settings` seeds at creation (fractal seeds the
  strict ascii/identifier policy).
- `wiki read <name>` — print a named entry verbatim with no appended newline, so
  redirected output round-trips byte-for-byte for LF files (reads normalize
  CRLF); `--lines`/`--words`/ `--chars` slice the body by 0-indexed half-open
  ranges (a slice keeps the frontmatter and appends a trailing newline).
- `wiki search <pattern> [name]` — regex search, optionally scoped to a subtree;
  `--field` searches frontmatter fields, `--all` includes non-markdown files,
  `--ignore-case` drops case, and `--lines`/`--lineno` switch the output from
  matching page names to matching lines or bare line numbers.
- `wiki update [name]` — rewrite whatever drifted from the generated form; see
  [[features/wiki_system/index_regeneration]].
- `wiki lint [name]` — check wiki health; see
  [[features/wiki_system/index_regeneration]].
- `wiki map [name]` — compact tree overview with descriptions and word counts;
  `--depth` and `--desc-limit` bound the dump, column toggles (`--no-desc`,
  `--no-words`, `--category`, `--markdown`) reshape the rows, and `--stat`
  prints a one-line size summary instead — the cheap probe before dumping a
  large wiki.
- `wiki install` — copy the bundled wiki skill into the Claude Code and Codex
  skill directories (home by default, the project with `--project`; `--link`
  symlinks for editable-install development).
- `wiki config` — install or refresh the Obsidian integration (it copies the
  bundled Wiki Root Links plugin from the package and enables it) and the git
  merge machinery; see [[features/wiki_system/merge_behavior]].
- `wiki trust` — record a wiki root as trusted in `~/.wiki/settings.json`. A
  `.wiki/wiki.py` hook runs code with the caller's privileges, so every command
  that resolves a wiki refuses to load one from an untrusted root.

## Path resolution

Every wiki-targeting command takes `--path=<wiki root>`. Without it, the command
resolves the *enclosing* wiki by walking up from cwd — the ancestor declaring
`.wiki/settings.json`, else the outermost `_index.md` chain — and falls back to
`{cwd}/wiki/`. From a node's working directory that resolves to the project wiki
at best and never to memory, so an omitted `--path` silently targets the wrong
store or errors: always pass `--path` explicitly when two wikis are in play.

## Exit codes

The prose output is for humans; scripts branch on exit codes:

- `search` follows the grep convention — a match exits 0, no match exits 1 with
  a stderr notice, and an error (invalid regex, no resolvable wiki) exits 2.
- `update` exits 0 after a successful run; with `--check` it writes nothing and
  exits 1 when changes are pending.
- `lint` exits 1 when issues are found and 0 when clean; notes on stderr never
  affect the exit code.
- `config` exits 0 even when an Obsidian plugin download fails — download
  failures are stderr warnings to re-run online, never the exit code.
