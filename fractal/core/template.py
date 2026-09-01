"""Functions for node seed templates."""

from __future__ import annotations

import difflib
import fnmatch
import io
import json
import pathlib
import re
import subprocess
import tarfile
import tempfile
import tomllib
from typing import Any, Optional

import tomli_w

import fractal.util
from fractal.constants import CONFIG_FILE, FRACTAL_FOLDER, WORKTREES_FOLDER
from fractal.typing import PathLike

from .config import KEYS
from .render import _SlotTemplate

__all__ = []

#: per-node template provenance file in the node's data directory
TEMPLATE_FILE = '_template.toml'

# a slot value's key follows the slot-name grammar -- the braced group of
# the slot pattern (fractal.core.render._SlotTemplate)
_SLOT_NAME = re.compile(r'[a-z_][a-z0-9_]*')

# a recorded commit is the full sha the folder deploys at -- the verbs
# that act on a record resolve it verbatim, so anything shorter refuses
_COMMIT_SHA = re.compile(r'[0-9a-f]{40}')

#: config keys a template preset may carry -- the budget, limit, duration,
#: model, and mode subset of a node's config keys; identity and immutable
#: keys (title, scope, base, ...) refuse at init by name
PRESET_KEYS = (
    'agent',
    'provider',
    'model',
    'effort',
    'max_iters',
    'max_depth',
    'max_children',
    'max_descendants',
    'timeout',
    'iter_timeout',
    'step_timeout',
    'step_retries',
    'step_retry_backoff',
    'interval',
    'sleep',
    'wait',
    'max_cost',
    'max_iter_cost',
    'max_step_cost',
    'reserve_budget',
    'sync',
    'detached',
)

#: credential file names refused under a template's agents/ subtree --
#: codex and grok symlink their OAuth stores into the node's agent dir as
#: auth.json (opencode's global store shares the name), omp relocates
#: credentials.json into its node dir, and the rest are the generic key shapes;
#: a dot-file (claude's .credentials.json among them) refuses beside these
CREDENTIAL_NAMES = (
    'auth.json',
    'credentials.json',
    '*.key',
    '*.pem',
    '*.p12',
    '*.pfx',
    'id_rsa',
    'id_ed25519',
    'id_ecdsa',
    'id_ecdsa_sk',
    'id_ed25519_sk',
    'id_dsa',
    '*.ppk',
)


def locate(
    value: str,
    *,
    repo_dir: pathlib.Path,
) -> tuple[str, Optional[str], pathlib.Path]:
    """Resolve a ``--template`` flag value to its tracked folder.

    Splits the optional ``@<ref>`` suffix at the last ``@`` and resolves
    the path half -- absolute or cwd-relative -- to its containing
    worktree, which must be the main checkout or one of its
    ``.worktrees/`` entries. Tab-completion therefore works from the root
    and from inside a node worktree, and the recorded form is the same
    repo-relative POSIX path either way.

    Args:
        value: The flag value (``<path>[@<ref>]``).
        repo_dir: Main repo root.

    Returns:
        Tuple of the worktree-relative POSIX path, the ref (``None``
        when the value carries no suffix), and the containing worktree.

    Raises:
        ValueError: If the value does not parse or the path is refused.

    """
    # a folder name carrying '@' cannot be told from a ref suffix -- when
    # the whole value names a folder on disk, refuse it before the split
    # eats the '@' (the post-split component check covers the rest)
    if '@' in value and pathlib.Path(value).is_dir():
        raise ValueError(
            f'--template folder name contains "@": {value!r};'
            ' "@" separates the ref (<path>[@<ref>]).'
        )
    # split the ref suffix at the last '@' -- a folder name never carries
    # one, so the split is unambiguous
    head, sep, tail = value.rpartition('@')
    if sep and head and tail:
        given, ref = head, tail
    elif sep:
        raise ValueError(
            f'Invalid --template value: {value!r} (expected <path>[@<ref>]).'
        )
    else:
        given, ref = value, None
    # a '..' step is refused before resolution -- the recorded form is a
    # plain worktree-relative path
    if '..' in pathlib.Path(given).parts:
        raise ValueError(f'--template path may not contain ".." steps: {given!r}.')
    resolved = pathlib.Path(given).resolve()
    # locate the containing worktree through the nearest existing ancestor, so a
    # folder deleted from disk but tracked at the fork commit still resolves
    probe = resolved
    while not probe.is_dir() and probe.parent != probe:
        probe = probe.parent
    found = fractal.util.git.toplevel(probe, check=False)
    if found is not None:
        found = found.resolve()
    inside = False
    if found is not None:
        main = found == repo_dir
        nested = found.parent == repo_dir / WORKTREES_FOLDER
        inside = main or nested
    if not inside or found is None or not resolved.is_relative_to(found):
        raise ValueError(
            f'--template path is outside this repository: {given!r};'
            ' a template is a tracked folder in the repo (the main'
            ' checkout or a node worktree).'
        )
    rel = resolved.relative_to(found)
    if not rel.parts:
        raise ValueError(
            '--template names the worktree root; point it at a folder inside the repo.'
        )
    relpath = rel.as_posix()
    # machinery components are refused casefolded, so every rule written
    # for node data stays blind to templates
    parts = tuple(part.casefold() for part in rel.parts)
    if FRACTAL_FOLDER in parts or '.git' in parts:
        raise ValueError(
            f'--template path contains a fractal machinery component:'
            f' {relpath!r}; a template is ordinary project content, outside'
            f' {FRACTAL_FOLDER}/ and .git/.'
        )
    if parts[0] == WORKTREES_FOLDER:
        raise ValueError(
            f'--template path leads into {WORKTREES_FOLDER}/: {relpath!r};'
            ' worktree checkouts are tracked at no commit -- name the'
            " folder by the repo's own path."
        )
    if any('@' in part for part in rel.parts):
        raise ValueError(
            f'--template folder name contains "@": {relpath!r};'
            ' "@" separates the ref (<path>[@<ref>]).'
        )
    return relpath, ref, found


def collect_values(
    *,
    values: Optional[PathLike],
    sets: Optional[list[str]],
    pin: Optional[str],
) -> dict[str, str]:
    """Merge the slot values a spawn supplies (later sources win).

    The sources, in winning order: the ``--values`` fill sheet (a TOML
    file of string values), the repeatable ``--set KEY=VALUE`` pairs,
    and ``--pin``, which supplies ``pin``. Every key must follow the
    slot-name grammar -- a key the slot pass could never match would
    otherwise sit unused while its slot refuses as unfilled.

    Args:
        values: The ``--values`` fill-sheet path, or ``None``.
        sets: The ``--set`` pairs (``KEY=VALUE``), or ``None``.
        pin: The commission pin from ``--pin``, or ``None``.

    Returns:
        The merged ``slot -> value`` map.

    Raises:
        ValueError: If the fill sheet is missing, is not valid TOML, or
            holds a non-string value; if a ``--set`` pair has no ``=``;
            or if any key breaks the slot-name grammar.

    """
    collected: dict[str, str] = {}
    # the fill sheet: a flat TOML table of string values
    if values is not None:
        sheet = pathlib.Path(values)
        if not sheet.is_file():
            raise ValueError(f'--values file does not exist: {sheet}')
        try:
            data = tomllib.loads(sheet.read_text(encoding='utf-8'))
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f'--values file is not valid TOML: {sheet}: {e}') from e
        for key, value in data.items():
            if not _SLOT_NAME.fullmatch(key):
                raise ValueError(
                    f'--values key is not a slot name: {key!r}'
                    ' (a slot name is lowercase [a-z_][a-z0-9_]*).'
                )
            if not isinstance(value, str):
                raise ValueError(
                    f'--values key {key!r} does not hold a string'
                    ' (slot values are strings).'
                )
            collected[key] = value
    # the --set pairs override the sheet
    for pair in sets or []:
        key, sep, value = pair.partition('=')
        if not sep:
            raise ValueError(f'Invalid --set value: {pair!r} (expected KEY=VALUE).')
        if not _SLOT_NAME.fullmatch(key):
            raise ValueError(
                f'--set key is not a slot name: {key!r}'
                ' (a slot name is lowercase [a-z_][a-z0-9_]*).'
            )
        collected[key] = value
    # --pin supplies the pin slot last, beside its fill-sheet-gate role
    if pin is not None:
        collected['pin'] = pin
    return collected


def materialize(
    *,
    worktree: pathlib.Path,
    path: str,
    commit: str,
    dest: pathlib.Path,
) -> pathlib.Path:
    """Extract a template folder at a commit into a directory.

    Reads the folder from git (``git archive``, run from the worktree
    root -- a pathspec is cwd-relative), so uncommitted edits never
    deploy. The archive keeps the path prefix, so the bundle lands at
    ``<dest>/<path>``.

    Args:
        worktree: Worktree root the path is recorded against.
        path: Worktree-relative template folder path (POSIX).
        commit: Commit to read the folder at.
        dest: Directory to extract into.

    Returns:
        The bundle root (``<dest>/<path>``).

    Raises:
        ValueError: If the folder is untracked at the commit, the commit
            is unknown, the path is not a template folder, the folder
            carries a symlink, or its ``agents/`` subtree carries a
            dot-file or a credential-named file
            (:data:`CREDENTIAL_NAMES`).

    """
    archive = subprocess.run(
        ['git', 'archive', commit, '--', f':(literal){path}'],
        capture_output=True,
        cwd=worktree,
    )
    if archive.returncode != 0:
        stderr = archive.stderr.decode(errors='replace')
        # git exits 128 with a distinct message per missing half: the
        # pathspec miss (folder untracked at the commit) and the unknown rev
        if 'did not match any files' in stderr:
            on_disk = ''
            if (worktree / path).exists():
                on_disk = ' (an uncommitted copy exists on disk)'
            raise ValueError(
                f'Template folder {path!r} is not tracked at commit'
                f' {commit}{on_disk}; commit the folder on the branch the'
                ' child forks from, or pass @<ref> to read it at another'
                ' commit.'
            )
        if 'not a valid object name' in stderr:
            raise ValueError(f'Template ref does not resolve to a commit: {commit!r}.')
        raise RuntimeError(f'git archive failed: {stderr.strip()}')
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
        # a template is self-contained: init.sh dereferences skill links
        # and copies steps and scripts as regular files only, so a link
        # pointing outside the folder would dangle after extraction --
        # refuse by name instead
        for member in bundle.getmembers():
            if member.issym() or member.islnk():
                raise ValueError(
                    f'Template folder {path!r} carries a symlink:'
                    f' {member.name!r}; a template is self-contained --'
                    ' commit the target file in its place.'
                )
            # agents/ deploys into the node's live agent dirs, where a
            # leaked credential would do harm -- refuse a dot-file or a
            # credential-named entry (CREDENTIAL_NAMES, casefolded) by
            # name, whatever commit carried it; the other surfaces hold
            # no agent credentials and pass through the estate law when
            # committed (the archive lists the path's ancestors too --
            # only the subtree under agents/ is judged)
            relfile = pathlib.PurePosixPath(member.name)
            if not relfile.is_relative_to(path):
                continue
            parts = relfile.relative_to(path).parts
            if len(parts) < 2 or parts[0] != 'agents':
                continue
            if any(part.startswith('.') for part in parts[1:]):
                raise ValueError(
                    f'Template folder {path!r} carries a dot-file under'
                    f' agents/: {member.name!r}; dot-files hold live agent'
                    ' state and credentials, never template content.'
                )
            if member.isfile():
                name = parts[-1].casefold()
                if any(fnmatch.fnmatchcase(name, shape) for shape in CREDENTIAL_NAMES):
                    raise ValueError(
                        f'Template folder {path!r} carries a credential-named'
                        f' file under agents/: {member.name!r}; credentials'
                        ' never deploy from a template -- a node links its own'
                        ' at seed time.'
                    )
        bundle.extractall(dest, filter='data')
    root = dest / pathlib.PurePosixPath(path)
    if not root.is_dir():
        raise ValueError(f'--template points at a file, not a folder: {path!r}.')
    if not (root / CONFIG_FILE).is_file():
        raise ValueError(
            f'{path!r} is not a template folder: no {CONFIG_FILE} at'
            f' {commit}; the config preset (even empty) is what marks a'
            ' template folder.'
        )
    return root


def read_preset(
    bundle: pathlib.Path,
    *,
    path: str,
) -> dict[str, Any]:
    """Read the config preset from a materialized bundle.

    The preset is the template's ``config.json`` -- a subset of a node's
    own config keys, typed the same way -- and fills each init flag the
    spawn left unset. Only budget, limit, duration, model, and mode keys
    (:data:`PRESET_KEYS`) may appear: identity and immutable keys belong
    to the spawn, and an unknown key is a typo that would otherwise
    silently preset nothing.

    Args:
        bundle: The bundle root.
        path: Worktree-relative template folder path (POSIX), for messages.

    Returns:
        The preset mapping, with null (unset) values dropped.

    Raises:
        ValueError: If the preset is not valid JSON, is not a JSON
            object, or carries a key outside :data:`PRESET_KEYS`.

    """
    # the file always exists -- its presence is what marked the folder a
    # template in materialize -- but its content is external input
    text = (bundle / CONFIG_FILE).read_text(encoding='utf-8')
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f'Template preset {path}/{CONFIG_FILE} is not valid JSON: {e}'
        ) from e
    if not isinstance(data, dict):
        raise ValueError(
            f'Template preset {path}/{CONFIG_FILE} does not hold a JSON object.'
        )
    # identity and immutable keys are the spawn's own, and an unknown key
    # would silently preset nothing -- both refuse by name
    for key in data:
        if key in PRESET_KEYS:
            continue
        if key in KEYS:
            raise ValueError(
                f'Template preset may not set {key!r}: identity and'
                ' immutable keys are never preset -- a preset carries only'
                ' budget, limit, duration, model, and mode keys.'
            )
        raise ValueError(f'Unknown template preset key: {key!r}.')
    # null is config.json's spelling for unset: the key defers to the
    # inherit-or-default source, exactly as an omitted key does
    return {key: value for key, value in data.items() if value is not None}


def trim(
    bundle: pathlib.Path,
    *,
    include: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
    strict: bool = True,
) -> list[str]:
    """Reduce a materialized bundle to its effective set.

    The effective set is every template file minus ``exclude``, or only
    ``include``; a directory entry covers its subtree. Files outside the
    set are deleted from the bundle -- downstream only ever sees the
    effective set -- and directories the deletions emptied are dropped,
    so a fully trimmed surface falls back to the inherit-or-package
    source.

    Args:
        bundle: The bundle root.
        include: Template-relative paths to keep (exclusive of ``exclude``).
        exclude: Template-relative paths to drop (exclusive of ``include``).
        strict: Refuse an entry matching nothing. ``False`` warns
            instead: a recorded listing may outlive the file it named.

    Returns:
        Warning lines for the entries matching nothing (always empty
        under ``strict``).

    Raises:
        ValueError: If an entry matches nothing in the template (under
            ``strict``), or names ``config.json`` or ``_template.toml``.

    """
    warnings: list[str] = []
    if not include and not exclude:
        return warnings
    flag = 'include' if include else 'exclude'
    files = sorted(
        entry.relative_to(bundle).as_posix()
        for entry in bundle.rglob('*')
        if entry.is_file()
    )
    covered: set[str] = set()
    for entry in include or exclude or []:
        normalized = pathlib.PurePosixPath(entry).as_posix()
        if normalized in (CONFIG_FILE, TEMPLATE_FILE):
            raise ValueError(
                f'--{flag} may not name {normalized!r}'
                ' (template machinery, not a seed surface).'
            )
        matched = [
            file
            for file in files
            if file == normalized or file.startswith(f'{normalized}/')
        ]
        if not matched:
            if strict:
                raise ValueError(
                    f'--{flag} entry matches nothing in the template: {entry!r}.'
                )
            warnings.append(
                f'{flag} entry matches nothing in the template: {entry!r}'
                ' (the template may have dropped it).'
            )
            continue
        covered.update(matched)
    # delete outside the effective set -- the marker always stays: it is
    # the config preset, not a seed surface, and never a listing entry
    for file in files:
        if exclude:
            keep = file not in covered
        else:
            keep = file in covered or file == CONFIG_FILE
        if not keep:
            (bundle / file).unlink()
    # drop emptied directories, deepest first
    for folder in sorted(
        (entry for entry in bundle.rglob('*') if entry.is_dir()),
        reverse=True,
    ):
        if not any(folder.iterdir()):
            folder.rmdir()
    return warnings


def fill(
    bundle: pathlib.Path,
    *,
    path: str,
    values: dict[str, str],
    remedy: Optional[str] = None,
) -> None:
    """Render every bundle file's ``{{slot}}`` placeholders in place.

    The slot pass: every regular file except ``config.json`` and
    ``_template.toml`` renders through the slot grammar
    (:class:`fractal.core.render._SlotTemplate`) -- a slot with no value
    and any ``{{`` that is not a lowercase slot refuse naming the file
    and the token, and a file that does not decode refuses by name
    (templates are text). Unused values pass: one fill sheet may cover
    several templates.

    Args:
        bundle: The bundle root.
        path: Worktree-relative template folder path (POSIX), for messages.
        values: The ``slot -> value`` fill map.
        remedy: Remedy clause for the no-value refusal (default: the
            init flags) -- reseed and diff replay recorded values, where
            the init flags do not exist.

    Raises:
        ValueError: If a file is not UTF-8 text, a slot has no value,
            or a ``{{`` is not a slot.

    """
    files = sorted(entry for entry in bundle.rglob('*') if entry.is_file())
    for entry in files:
        relfile = entry.relative_to(bundle).as_posix()
        # the preset and the provenance record are template machinery,
        # never a rendered seed surface
        if relfile in (CONFIG_FILE, TEMPLATE_FILE):
            continue
        # byte-level read and write, so the pass never rewrites line
        # endings: deployed bytes equal render(committed bytes, values)
        blob = entry.read_bytes()
        try:
            text = blob.decode('utf-8')
        except UnicodeDecodeError as e:
            raise ValueError(
                f'Template file {path}/{relfile} is not UTF-8 text; templates are text.'
            ) from e
        try:
            rendered = _SlotTemplate(text).substitute(values)
        except KeyError as e:
            name = e.args[0]
            fix = remedy or (f'supply it with --set {name}=<value> or a --values file')
            raise ValueError(
                f'Template file {path}/{relfile} has no value for slot'
                f' {{{{{name}}}}}: {fix}.'
            ) from e
        except ValueError as e:
            # name the offending token: the first {{ the grammar calls
            # invalid, through its closing braces (or its line end)
            invalid = next(
                found
                for found in _SlotTemplate.pattern.finditer(text)
                if found.group('invalid') is not None
            )
            line = text[invalid.start() :].split('\n', 1)[0]
            head, closer, _ = line.partition('}}')
            token = f'{head}{closer}'
            raise ValueError(
                f'Template file {path}/{relfile} carries {token!r}, which'
                ' is not a slot: a slot is a lowercase {{name}}, and a'
                ' literal "{{" cannot be written.'
            ) from e
        if rendered != text:
            entry.write_bytes(rendered.encode('utf-8'))


def write_provenance(
    bundle: pathlib.Path,
    *,
    path: str,
    commit: str,
    values: dict[str, str],
    include: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
) -> None:
    """Write the ``_template.toml`` provenance file into a directory.

    The file records what seeded the node: the repo-relative template
    ``path``, the ``commit`` actually read, the mutually exclusive
    ``include``/``exclude`` listing when one was given, and the
    ``[values]`` table of slot fills. The table goes last so the scalar
    keys stay at the top level. Init writes into the bundle (init.sh
    places the record with the other surfaces); reseed rewrites the
    node data directory's copy in place.

    Args:
        bundle: The directory to write the record into (the bundle
            root, or the node data directory on reseed).
        path: Worktree-relative template folder path (POSIX).
        commit: The commit the folder was read at.
        values: Slot fills to record.
        include: Template-relative paths kept (exclusive of ``exclude``).
        exclude: Template-relative paths dropped (exclusive of ``include``).

    """
    data: dict[str, Any] = {'path': path, 'commit': commit}
    if include:
        data['include'] = list(include)
    if exclude:
        data['exclude'] = list(exclude)
    data['values'] = dict(values)
    text = tomli_w.dumps(data)
    (bundle / TEMPLATE_FILE).write_text(text, encoding='utf-8')


def read_provenance(node_dir: pathlib.Path) -> dict[str, Any]:
    """Read a node's ``_template.toml`` provenance record.

    The counterpart of :func:`write_provenance` for the verbs that act
    on a recorded template. The record is hand-editable, so every field
    a consumer trusts is validated here; a listing entry the template no
    longer carries is judged where the listing is applied
    (:func:`trim`).

    Args:
        node_dir: The node data directory holding the record.

    Returns:
        The record mapping (``path``, ``commit``, the optional
        ``include``/``exclude`` listing, and ``values``).

    Raises:
        ValueError: If the node records no template, the record is not
            valid TOML, ``path`` is absolute or carries a ``..`` step,
            ``commit`` is not a full 40-hex sha, ``include`` appears
            beside ``exclude``, a listing is not a list of paths, or
            ``values`` is not a table of strings.

    """
    record = node_dir / TEMPLATE_FILE
    if not record.is_file():
        raise ValueError(
            f'No template recorded for this node: {record} does not exist.'
        )
    try:
        data = tomllib.loads(record.read_text(encoding='utf-8'))
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f'{record} is not valid TOML: {e}') from e
    # the recorded path is worktree-relative: an absolute path or a '..'
    # step would escape the repo the commit is read from
    path = data.get('path')
    if not isinstance(path, str) or not path:
        raise ValueError(f'{record} records no template path.')
    posix = pathlib.PurePosixPath(path)
    if posix.is_absolute() or '..' in posix.parts:
        raise ValueError(f'{record} template path is not worktree-relative: {path!r}.')
    commit = data.get('commit')
    if not isinstance(commit, str) or not _COMMIT_SHA.fullmatch(commit):
        raise ValueError(f'{record} commit is not a full 40-hex sha: {commit!r}.')
    include, exclude = data.get('include'), data.get('exclude')
    if include is not None and exclude is not None:
        raise ValueError(
            f'{record} carries include beside exclude; the listing is one or the other.'
        )
    for key in ('include', 'exclude'):
        listing = data.get(key)
        if listing is None:
            continue
        listed = isinstance(listing, list)
        strings = listed and all(isinstance(entry, str) for entry in listing)
        if not strings:
            raise ValueError(f'{record} {key} is not a list of paths.')
    values = data.get('values', {})
    table = isinstance(values, dict)
    strings = table and all(isinstance(value, str) for value in values.values())
    if not strings:
        raise ValueError(f'{record} values is not a table of strings.')
    return data


def diff(
    *,
    node_dir: pathlib.Path,
    repo_dir: pathlib.Path,
) -> tuple[list[str], list[str]]:
    """Diff a node's live seed surfaces against its rendered template.

    Re-renders the recorded folder at its recorded commit with its
    recorded values, applies the effective set, and compares each bundle
    surface against the node's live copy: ``NODE.md``, ``steps/``,
    ``scripts/``, and ``skills/`` in the node data directory, and each
    ``agents/<agent>/`` file as the live ``.<agent>/`` file. A live
    symlink and a file the bundle does not carry are never judged; a
    bundle file the node lacks is drift, as is unrendered ``{{`` residue
    in a live copy.

    Args:
        node_dir: The node data directory (the live surfaces and the
            provenance record).
        repo_dir: Main repo root (the recorded commit resolves there).

    Returns:
        Tuple of the per-file drift reports -- unified diffs and
        one-line findings -- and the stale-listing warnings.

    Raises:
        ValueError: If the provenance record refuses
            (:func:`read_provenance`) or the recorded folder does not
            materialize at its commit (:func:`materialize`).

    """
    record = read_provenance(node_dir)
    reports: list[str] = []
    with tempfile.TemporaryDirectory(prefix='fractal-template-') as tmp:
        bundle = materialize(
            worktree=repo_dir,
            path=record['path'],
            commit=record['commit'],
            dest=pathlib.Path(tmp),
        )
        warnings = trim(
            bundle,
            include=record.get('include'),
            exclude=record.get('exclude'),
            strict=False,
        )
        fill(
            bundle,
            path=record['path'],
            values=record.get('values', {}),
            remedy=(
                "add the value to the [values] table in the node's"
                ' _template.toml, or re-init the node with --set'
            ),
        )
        files = sorted(entry for entry in bundle.rglob('*') if entry.is_file())
        for entry in files:
            relfile = entry.relative_to(bundle).as_posix()
            parts = relfile.split('/')
            # map the bundle file to its live counterpart; anything else
            # (the config preset, stray root files) deploys nowhere
            if relfile == 'NODE.md' or parts[0] in ('steps', 'scripts', 'skills'):
                target = relfile
            elif parts[0] == 'agents' and len(parts) > 2:
                target = '/'.join([f'.{parts[1]}', *parts[2:]])
            else:
                continue
            live = node_dir / target
            # a live symlink (the skills mount, a linked credential) is
            # machinery, never judged content
            if live.is_symlink():
                continue
            if not live.is_file():
                reports.append(f'{target}: missing from the node.')
                continue
            rendered = entry.read_bytes()
            actual = live.read_bytes()
            if actual == rendered:
                continue
            # residue is its own finding: a '{{' the seed-time pass would
            # have refused marks a hand-copied, never-rendered file
            if b'{{' in actual and b'{{' not in rendered:
                reports.append(f'{target}: unrendered {{{{ residue in the live copy.')
            rendered_lines = rendered.decode('utf-8').splitlines(keepends=True)
            actual_text = actual.decode('utf-8', errors='replace')
            actual_lines = actual_text.splitlines(keepends=True)
            lines = difflib.unified_diff(
                a=rendered_lines,
                b=actual_lines,
                fromfile=f'template/{target}',
                tofile=f'node/{target}',
            )
            report = ''.join(lines)
            reports.append(report.rstrip('\n'))
    return reports, warnings
