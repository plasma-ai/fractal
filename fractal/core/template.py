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
from .render import render_seed

__all__ = []

#: template-local defaults and per-node provenance filename
TEMPLATE_FILE = '_template.toml'

#: top-level input names stay distinct from runtime $UPPER variables
_VALUE_NAME = re.compile(r'[a-z_][a-z0-9_]*')

#: Jinja names that cannot refer to a supplied context input
_RESERVED_NAMES = ('true', 'false', 'none', 'not', 'self')

# a recorded commit is the full sha the folder deploys at -- the verbs
# that act on a record resolve it verbatim, so anything shorter refuses
_COMMIT_SHA = re.compile(r'[0-9a-f]{40}')

# refusal for a template ref that resolves to no commit -- materialize and
# the node verbs' own resolve sites raise it, so the wording stays identical
_UNRESOLVED_REF = 'Template ref does not resolve to a commit: {ref!r}.'

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

#: credential file names refused anywhere in a template -- codex and grok
#: symlink their OAuth stores into the node's agent dir as auth.json
#: (opencode's global store shares the name), omp relocates credentials.json
#: into its node dir, and the rest are the generic key shapes; a dot-file
#: (claude's .credentials.json among them) refuses beside these under
#: agents/, the one subtree that deploys into live agent dirs
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


def validate_values(values: Any, *, source: str) -> dict[str, Any]:
    """Validate a TOML input table at its read boundary.

    Args:
        values: Parsed TOML inputs.
        source: Input source name for messages.

    Returns:
        The validated input table.

    Raises:
        ValueError: If the inputs are not a table, a top-level name is
            invalid or reserved, or a commission pin is not text.

    """
    if not isinstance(values, dict):
        raise ValueError(f'{source} values is not a table.')
    for key in values:
        if not _VALUE_NAME.fullmatch(key):
            raise ValueError(
                f'{source} key is not an input name: {key!r}'
                ' (an input name is lowercase [a-z_][a-z0-9_]*).'
            )
        if key in _RESERVED_NAMES:
            raise ValueError(f'{source} key is reserved by Jinja: {key!r}.')
    if 'pin' in values and not isinstance(values['pin'], str):
        raise ValueError(f'{source} pin must be text.')
    return values


def collect_values(
    *,
    values: Optional[PathLike],
    sets: Optional[list[str]],
    pin: Optional[str],
) -> dict[str, Any]:
    """Merge the inputs a spawn supplies, with later sources winning.

    The ``--values`` TOML file supplies inputs and repeatable
    ``--set KEY=VALUE`` pairs override whole top-level values. Each
    override is a TOML literal; text requires quotes. ``--pin`` must
    agree with a pin supplied through either source.

    Args:
        values: The ``--values`` path, or ``None``.
        sets: The ``--set`` pairs (``KEY=VALUE``), or ``None``.
        pin: The commission pin from ``--pin``, or ``None``.

    Returns:
        The merged input map, without template defaults.

    Raises:
        ValueError: If a file or override is malformed, an input name
            is invalid, or two explicit commission pins disagree.

    """
    collected: dict[str, Any] = {}
    # the external TOML table supplies per-spawn inputs
    if values is not None:
        sheet = pathlib.Path(values)
        if not sheet.is_file():
            raise ValueError(f'--values file does not exist: {sheet}')
        try:
            data = tomllib.loads(sheet.read_text(encoding='utf-8'))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f'--values file is not valid TOML: {sheet}: {e}') from e
        collected.update(validate_values(data, source='--values'))
    # --set replaces a whole top-level value, never a nested merge
    for pair in sets or []:
        key, sep, value = pair.partition('=')
        if not sep:
            raise ValueError(f'Invalid --set value: {pair!r} (expected KEY=VALUE).')
        validate_values({key: ''}, source='--set')
        try:
            data = tomllib.loads(f'value = {value}')
        except tomllib.TOMLDecodeError as e:
            raise ValueError(
                f'--set {key} is not a TOML literal: {e}; quote text values.'
            ) from e
        if set(data) != {'value'}:
            raise ValueError(f'--set {key} must contain one TOML literal.')
        collected.update(validate_values({key: data['value']}, source='--set'))
    # explicit pins are one commission instruction, not competing defaults
    if pin is not None:
        merged = collected.get('pin')
        if merged is not None and merged != pin:
            raise ValueError(
                f'pin supplied twice: --pin {pin!r} and --set/--values'
                f' pin={merged!r} disagree.'
            )
        collected['pin'] = pin
    return collected


def read_defaults(bundle: pathlib.Path, *, path: str) -> dict[str, Any]:
    """Read literal defaults from a template's optional metadata file.

    Args:
        bundle: The untrimmed bundle root.
        path: Worktree-relative template folder path, for messages.

    Returns:
        The template's default inputs, or an empty table.

    Raises:
        ValueError: If the metadata is malformed or carries fields
            outside its ``[values]`` table.

    """
    metadata = bundle / TEMPLATE_FILE
    if not metadata.is_file():
        return {}
    source = f'Template defaults {path}/{TEMPLATE_FILE}'
    try:
        data = tomllib.loads(metadata.read_text(encoding='utf-8'))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f'{source} is not valid TOML: {e}') from e
    for key in data:
        if key != 'values':
            raise ValueError(
                f'{source} has an unknown field: {key!r}; only [values] is allowed.'
            )
    values = data.get('values', {})
    return validate_values(values, source=source)


def read_sources(bundle: pathlib.Path) -> dict[str, bytes]:
    """Snapshot committed source bytes before output selection or rendering.

    Args:
        bundle: The untrimmed bundle root.

    Returns:
        Exact template-relative filenames mapped to their source bytes.

    """
    result: dict[str, bytes] = {}
    for entry in sorted(bundle.rglob('*')):
        if not entry.is_file():
            continue
        relfile = entry.relative_to(bundle).as_posix()
        blob = entry.read_bytes()
        result[relfile] = blob
    return result


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
            carries a symlink or a credential-named file
            (:data:`CREDENTIAL_NAMES`), or its ``agents/`` subtree
            carries a dot-file.

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
            raise ValueError(_UNRESOLVED_REF.format(ref=commit))
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
            # a leaked credential would do harm wherever it deploys --
            # refuse a credential-named entry (CREDENTIAL_NAMES,
            # casefolded) anywhere in the folder, whatever commit carried
            # it; a dot-file refuses only under agents/, the one subtree
            # that deploys into live agent dirs (a template's own tracked
            # dot-files elsewhere are not credentials by name; the
            # archive lists the path's ancestors too -- only the members
            # under the path are judged)
            relfile = pathlib.PurePosixPath(member.name)
            if not relfile.is_relative_to(path):
                continue
            parts = relfile.relative_to(path).parts
            # the member at the path itself (a --template naming a file)
            # is judged as a non-folder below, not by name here
            if not parts:
                continue
            if len(parts) >= 2 and parts[0] == 'agents':
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
                        f' file: {member.name!r}; credentials never deploy'
                        ' from a template -- a node links its own at seed'
                        ' time.'
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

    A directory entry covers its subtree. Files outside the selected
    outputs are deleted from the deployment bundle and emptied directories
    are dropped, so a fully trimmed surface falls back to the
    inherit-or-package source. The separate source snapshot remains
    available to Jinja includes regardless of output selection.

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
            ``strict``), or names machinery or source-only files.

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
        outputs = []
        for file in matched:
            parts = file.split('/')
            in_surface = parts[0] in ('steps', 'scripts', 'skills', 'agents')
            surface_file = (len(parts) > 1) and in_surface
            if file == 'NODE.md' or surface_file:
                outputs.append(file)
        if not outputs:
            raise ValueError(
                f'--{flag} entry is source-only: {entry!r};'
                ' select deployed seed surfaces, not documentation or helpers.'
            )
        covered.update(matched)
    # metadata always stays; it is not a selectable seed surface
    for file in files:
        if exclude:
            keep = file not in covered
        else:
            keep = file in covered or file in (CONFIG_FILE, TEMPLATE_FILE)
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


def vet(bundle: pathlib.Path) -> None:
    """Refuse effective-set files the seed's copy loops would skip.

    The copy loops deploy a fixed shape per surface: ``steps/`` copies
    top-level ``*.md`` files, ``scripts/`` copies top-level files
    skipping underscore-prefixed machinery, ``skills/`` copies per
    skill directory, and ``agents/`` deploys per registered agent
    directory. A file outside those shapes would deploy nothing, print
    success, and drift on every later diff -- refused here, post-trim,
    so an excluded stray can neither refuse the seed nor slip past it.
    Root-level extras outside the surfaces (a ``README.md`` beside
    them) are documentation and pass.

    Args:
        bundle: The bundle root.

    Raises:
        ValueError: If a surface directory is named in the wrong case,
            ``steps/`` or ``scripts/`` holds a file the copy loop would
            skip, ``skills/`` holds a loose file or hidden directory,
            or ``agents/`` holds an unknown or loose entry or a
            ``skills`` entry shadowing the mount.

    """
    from .agent import supported

    # surface names are exact: a case-variant folder (Steps/) deploys
    # through the seed's case-insensitive filesystem lookups on macOS
    # yet diff's case-sensitive compare never judges it, and deploys
    # nothing at all on a case-sensitive checkout -- refused either way
    surfaces = ('steps', 'scripts', 'skills', 'agents')
    for entry in sorted(bundle.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.casefold() in surfaces and entry.name not in surfaces:
            raise ValueError(
                f'Template folder names a seed surface in the wrong'
                f' case: {entry.name}/ (surfaces are lowercase: steps/,'
                ' scripts/, skills/, agents/).'
            )
    # steps: the copy loop deploys top-level *.md files only, and its
    # glob never matches a hidden entry
    steps_dir = bundle / 'steps'
    if steps_dir.is_dir():
        for entry in sorted(steps_dir.rglob('*')):
            if not entry.is_file():
                continue
            top_level = entry.parent == steps_dir
            step_shape = (entry.suffix == '.md') and not entry.name.startswith('.')
            if top_level and step_shape:
                continue
            relname = entry.relative_to(steps_dir).as_posix()
            raise ValueError(
                f'Template steps/ has a file the seed would skip:'
                f' {relname} (steps deploy as top-level, non-hidden'
                ' *.md files).'
            )
    # scripts: top-level regular files, underscore machinery skipped,
    # and the copy loop's glob never matches a hidden entry
    scripts_dir = bundle / 'scripts'
    if scripts_dir.is_dir():
        for entry in sorted(scripts_dir.rglob('*')):
            if not entry.is_file():
                continue
            if entry.parent == scripts_dir and not entry.name.startswith(('_', '.')):
                continue
            relname = entry.relative_to(scripts_dir).as_posix()
            raise ValueError(
                f'Template scripts/ has a file the seed would skip:'
                f' {relname} (scripts deploy as top-level, non-underscore,'
                ' non-hidden files).'
            )
    # skills: whole skill directories -- a loose file deploys nowhere,
    # and the copy loop's glob never matches a hidden directory
    skills_dir = bundle / 'skills'
    if skills_dir.is_dir():
        for entry in sorted(skills_dir.iterdir()):
            if entry.is_dir():
                if entry.name.startswith('.'):
                    raise ValueError(
                        f'Template skills/ has a skill directory the seed'
                        f' would skip: {entry.name} (skills deploy per'
                        ' non-hidden directory).'
                    )
                continue
            raise ValueError(
                f'Template skills/ has a loose file outside a skill'
                f' directory: {entry.name} (skills deploy per directory).'
            )
    # agents: an entry deploys only as a registered agent's directory --
    # an unknown name (a typo) refuses like a credential, and a loose
    # file in a directory's place refuses beside it
    agents_dir = bundle / 'agents'
    if agents_dir.is_dir():
        supported_agents = ', '.join(supported())
        for entry in sorted(agents_dir.iterdir()):
            if entry.name not in supported():
                raise ValueError(
                    f'Template agents/ names an unknown agent:'
                    f' {entry.name!r}'
                    f' (supported: {supported_agents}).'
                )
            if not entry.is_dir():
                raise ValueError(
                    f'Template agents/ has a loose file, not an agent'
                    f' directory: {entry.name} (per-agent files live'
                    ' under agents/<agent>/).'
                )
            # the agent's skills/ is a mount of the node's skills
            # directory: a bundle entry there would pre-empt the mount
            # at init and write through the live link at reseed
            if (entry / 'skills').exists():
                raise ValueError(
                    f'Template agents/{entry.name}/ carries a skills'
                    f' entry: agents/<agent>/skills is the mount of the'
                    " node's skills/ -- put skill directories under the"
                    " template's top-level skills/ instead."
                )


def vet_steps(
    bundle: pathlib.Path,
    *,
    agent: Optional[str],
    provider: Optional[str],
    root: pathlib.Path,
    detached: Optional[bool],
) -> None:
    """Validate rendered step overrides against the node's effective settings.

    Args:
        bundle: The rendered deployment bundle.
        agent: The node's effective agent command.
        provider: The node's effective provider route.
        root: Tree data directory for registered deployment agents.
        detached: Whether the node runs detached.

    Raises:
        ValueError: If a rendered override has an invalid boolean,
            duration, agent, provider, or detached-mode combination.

    """
    from .agent import command_base, resolve
    from .loop import Step, _parse_duration

    for entry in sorted((bundle / 'steps').glob('*.md')):
        step = Step.load(entry)
        try:
            # init.sh refuses any detached: line between its first two exact
            # fences; the loop also recognizes indented frontmatter fields
            if detached:
                has_detached = bool(step.detached_raw)
                delimiters = 0
                for line in entry.read_text(encoding='utf-8').splitlines():
                    if line == '---':
                        delimiters += 1
                        if delimiters >= 2:
                            break
                        continue
                    if delimiters == 1 and line.startswith('detached:'):
                        has_detached = True
                        break
                if has_detached:
                    raise ValueError(
                        f'detached: in {entry.name} is invalid'
                        ' in detached mode (already detached)'
                    )
            for key in ('requires_approval', 'detached'):
                value = step.frontmatter.get(key)
                if value is not None and value not in ('true', 'false'):
                    raise ValueError(f'{key}: must be true or false')
            if step.timeout is not None:
                _parse_duration(step.timeout, 'timeout')
            command = step.agent or agent
            if command is None:
                raise ValueError(
                    'No agent configured; set one with fractal init --agent.'
                )
            backend = resolve(command_base(command), root=root)
            route = step.provider
            if route is None and backend.providers:
                route = provider
            if route is not None and route not in backend.providers:
                supported = ', '.join(backend.providers) or 'none'
                raise ValueError(
                    f'Unsupported provider: {route!r} (supported: {supported})'
                )
        except ValueError as e:
            raise ValueError(f'Template step steps/{entry.name}: {e}') from e


def fill(
    bundle: pathlib.Path,
    *,
    sources: dict[str, bytes],
    path: str,
    values: dict[str, Any],
    remedy: Optional[str] = None,
) -> None:
    """Render selected deployment outputs from an immutable source snapshot.

    Source-only files remain available to includes but never render as
    standalone outputs. All selected files render before any output is
    written, so a failure cannot leave a partly rendered bundle.

    Args:
        bundle: The selected deployment bundle.
        sources: Committed bytes captured before selection or rendering.
        path: Worktree-relative template folder path, for messages.
        values: The resolved input map.
        remedy: Undefined-input remedy for replaying a recorded seed.

    Raises:
        ValueError: If selected source is not UTF-8 text or Jinja cannot
            render an output with the supplied inputs.

    """
    files = []
    for entry in sorted(bundle.rglob('*')):
        if not entry.is_file():
            continue
        relfile = entry.relative_to(bundle).as_posix()
        parts = relfile.split('/')
        in_surface = parts[0] in ('steps', 'scripts', 'skills', 'agents')
        surface_file = (len(parts) > 1) and in_surface
        if relfile == 'NODE.md' or surface_file:
            files.append(relfile)
    rendered = render_seed(sources, files, values=values, path=path, remedy=remedy)
    for relfile, blob in rendered.items():
        (bundle / relfile).write_bytes(blob)


def root_notice(
    *,
    repo_dir: pathlib.Path,
    root: str,
    path: str,
    commit: str,
) -> Optional[str]:
    """Return one notice when the root branch's copy differs from the commit read.

    The same tree-id comparison for every verb that reads a fresh folder
    choice (init, and a reseed re-point): a parent deep in the tree
    carries the root's copy only as of its last merge, so the notice
    names the ``@<root>`` form that reads the root's current copy. A
    path absent on the root is no notice.

    Args:
        repo_dir: Main repo root (both revisions resolve there).
        root: The tree's root branch.
        path: Worktree-relative template folder path (POSIX).
        commit: The commit the folder was read at.

    Returns:
        The notice line, or ``None`` when the copies match or the root
        does not carry the folder.

    """
    cmd = ['rev-parse', '-q', '--verify', f'{root}:{path}']
    root_tree = fractal.util.git.run(cmd, cwd=repo_dir, check=False)
    cmd = ['rev-parse', '-q', '--verify', f'{commit}:{path}']
    read_tree = fractal.util.git.run(cmd, cwd=repo_dir, check=False)
    if root_tree is None or root_tree == read_tree:
        return None
    return (
        f'Notice: template {path!r} differs on the root branch; pass'
        f" --template={path}@{root} to read the root's copy."
    )


def write_provenance(
    bundle: pathlib.Path,
    *,
    path: str,
    commit: str,
    values: dict[str, Any],
    include: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
) -> None:
    """Write the ``_template.toml`` provenance file into a directory.

    The file records what seeded the node: the repo-relative template
    ``path``, the ``commit`` actually read, the mutually exclusive
    ``include``/``exclude`` listing when one was given, and the
    ``[values]`` table of resolved inputs. The table goes last so the scalar
    keys stay at the top level. Init writes into the bundle (init.sh
    places the record with the other surfaces); reseed rewrites the
    node data directory's copy in place.

    Args:
        bundle: The directory to write the record into (the bundle
            root, or the node data directory on reseed).
        path: Worktree-relative template folder path (POSIX).
        commit: The commit the folder was read at.
        values: Resolved inputs to record.
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
            ``values`` is not a valid input table.

    """
    record = node_dir / TEMPLATE_FILE
    if not record.is_file():
        raise ValueError(
            f'No template recorded for this node: {record} does not exist.'
        )
    try:
        data = tomllib.loads(record.read_text(encoding='utf-8'))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
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
    validate_values(data.get('values', {}), source=str(record))
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
    bundle file the node lacks is drift. Literal braces are ordinary
    output and are judged only by the byte comparison.

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
        sources = read_sources(bundle)
        read_defaults(bundle, path=record['path'])
        warnings = trim(
            bundle,
            include=record.get('include'),
            exclude=record.get('exclude'),
            strict=False,
        )
        fill(
            bundle,
            sources=sources,
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
            # (the config preset, stray root files -- a root file named
            # like a surface included) deploys nowhere
            in_surface = parts[0] in ('steps', 'scripts', 'skills')
            surface_file = (len(parts) > 1) and in_surface
            if relfile == 'NODE.md' or surface_file:
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
