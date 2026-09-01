"""Functions for node seed templates."""

from __future__ import annotations

import io
import json
import pathlib
import re
import subprocess
import tarfile
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
    # locate the containing worktree through the nearest existing ancestor,
    # so a folder deleted from disk but tracked at the fork commit still
    # resolves
    probe = resolved
    while not probe.is_dir() and probe.parent != probe:
        probe = probe.parent
    found = fractal.util.git.toplevel(probe, check=False)
    if found is not None:
        found = found.resolve()
    inside = found is not None and (
        found == repo_dir or found.parent == repo_dir / WORKTREES_FOLDER
    )
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
            is unknown, the path is not a template folder, or the folder
            carries a symlink.

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
            on_disk = (
                ' (an uncommitted copy exists on disk)'
                if (worktree / path).exists()
                else ''
            )
            raise ValueError(
                f'Template folder {path!r} is not tracked at commit'
                f' {commit}{on_disk}; commit the folder on the branch the'
                ' child forks from, or pass @<ref> to read it at another'
                ' commit.'
            )
        if 'not a valid object name' in stderr:
            raise ValueError(f'Template ref does not resolve: {commit!r}.')
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
) -> None:
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

    Raises:
        ValueError: If an entry matches nothing in the template, or names
            ``config.json`` or ``_template.toml``.

    """
    if not include and not exclude:
        return
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
            raise ValueError(
                f'--{flag} entry matches nothing in the template: {entry!r}.'
            )
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


def fill(
    bundle: pathlib.Path,
    *,
    path: str,
    values: dict[str, str],
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
            raise ValueError(
                f'Template file {path}/{relfile} has no value for slot'
                f' {{{{{name}}}}}: supply it with --set {name}=<value>'
                ' or a --values file.'
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
    """Write the ``_template.toml`` provenance file into a bundle.

    The file records what seeded the node: the repo-relative template
    ``path``, the ``commit`` actually read, the mutually exclusive
    ``include``/``exclude`` listing when one was given, and the
    ``[values]`` table of slot fills. The table goes last so the scalar
    keys stay at the top level.

    Args:
        bundle: The bundle root.
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
