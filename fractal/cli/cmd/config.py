"""Implements ``fractal config`` sub-app commands."""

from __future__ import annotations

import json
import pathlib

import typer

from fractal.cli.utils import command, resolve_node
from fractal.core.config import (
    BOOL_KEYS,
    COST_KEYS,
    IMMUTABLE_KEYS,
    INT_KEYS,
    KEYS,
    LIST_KEYS,
    SQLITE_INT_MAX,
)

__all__ = [
    'config_get',
    'config_set',
    'node_config_get',
    'node_config_set',
]

# NOTE: keys whose values are JSON-coerced (numbers, booleans);
#   every other key holds a literal string, so e.g. scope=123
#   stays the string "123" rather than int 123
_COERCED_KEYS = (*BOOL_KEYS, *INT_KEYS, *COST_KEYS)


def config_get(app: typer.Typer) -> typer.Typer:
    """Register the ``_get`` command."""
    # key argument
    key_help = 'Config key to read.'
    key = typer.Argument(..., help=key_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_get')
    def _get(
        key: str = key,
        path: str = path,
    ) -> None:
        """Read a config value."""
        _config_get(key, path)

    return app


def config_set(app: typer.Typer) -> typer.Typer:
    """Register the ``_set`` command."""
    # values argument
    values_help = 'Key=value pairs to write.'
    values = typer.Argument(..., help=values_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, '_set')
    def _set(
        values: list[str] = values,
        path: str = path,
    ) -> None:
        """Set config values (key=value pairs)."""
        _config_set(values, path, check=False)

    return app


def node_config_get(app: typer.Typer) -> typer.Typer:
    """Register the ``get`` command."""
    # key argument
    key_help = 'Config key to read.'
    key = typer.Argument(..., help=key_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'get')
    def _get(
        key: str = key,
        path: str = path,
    ) -> None:
        """Read a node config value."""
        _config_get(key, path)

    return app


def node_config_set(app: typer.Typer) -> typer.Typer:
    """Register the ``set`` command."""
    # values argument
    values_help = 'Key=value pairs to write (e.g. max_cost=5).'
    values = typer.Argument(..., help=values_help)
    # path option
    path_help = 'Worktree directory.'
    path = typer.Option('.', '--path', help=path_help)

    @command(app, 'set')
    def _set(
        values: list[str] = values,
        path: str = path,
    ) -> None:
        """Set node config values (key=value pairs), confirming old -> new."""
        _config_set(values, path, echo=True)

    return app


# ------ helper functions


def _config_get(key: str, path: str) -> None:
    """Resolve the node and print one config value (shared by get/_get)."""
    # reject unknown keys like the setter -- a typo'd or removed key reading
    # as unset (empty output, exit 0) would silently misdirect a script
    if key not in KEYS:
        valid = ', '.join(KEYS)
        raise typer.BadParameter(f'Unknown config key: {key!r}. Valid keys: {valid}.')
    # resolve node and read value
    node = resolve_node(path)
    value = node.config.get(key)
    if value is not None:
        # emit booleans as lowercase true/false for shell consumers
        if isinstance(value, bool):
            typer.echo('true' if value else 'false')
        # emit list values one item per line -- the operator read surface
        # (e.g. `config get scope`) consumes them without a JSON parser
        elif isinstance(value, list):
            typer.echo('\n'.join(str(item) for item in value))
        # emit structured values as JSON so they round-trip with set
        elif isinstance(value, dict):
            encoded = json.dumps(value)
            typer.echo(encoded)
        else:
            typer.echo(value)


def _config_set(
    values: list[str],
    path: str,
    check: bool = True,
    echo: bool = False,
) -> None:
    """Parse, type-validate, and write config key=value pairs (shared by set/_set).

    The single boundary for both the public ``node config set`` and the private
    ``config _set`` (used by init.sh): parses each ``key=value``, enforces each
    key's type at the JSON boundary, then validates the merged config the way
    init does before writing -- so neither path can store a value init rejects.
    ``echo`` confirms each written key ``old -> new`` (the public surface); the
    private ``_set`` leaves it off so init.sh's script output stays clean.
    """
    # parse key=value pairs into a config dict
    config = {}
    for entry in values:
        # require an explicit key=value; a bare key would silently store ''
        if '=' not in entry:
            raise typer.BadParameter(f'Expected key=value, got {entry!r}.')
        key, _, value = entry.partition('=')
        # reject unknown keys so a typo does not persist
        if key not in KEYS:
            valid = ', '.join(KEYS)
            raise typer.BadParameter(
                f'Unknown config key: {key!r}. Valid keys: {valid}.'
            )
        # an empty value is a mistake; 'null' is the explicit way to clear
        if value == '':
            raise typer.BadParameter(
                f'Empty value for {key!r}; use {key}=null to clear it.'
            )
        # 'null' clears any key; coerced keys parse as JSON (number/bool);
        # every other key is a literal string (so a numeric-looking path or
        # branch name is not silently turned into an int/bool)
        if value == 'null':
            config[key] = None
        # list keys store a JSON list -- accept both the CLI init's
        # comma-joined form and the space-joined string form the read
        # normalization splits; each entry lands in canonical path form
        # ('./src', 'src/', 'a//b' -> 'src', 'a/b'): the commit boundary
        # is a literal string prefix against git's canonical paths, so a
        # non-canonical spelling would read every change as out of scope
        elif key in LIST_KEYS:
            entries = value.replace(',', ' ').split()
            config[key] = [pathlib.PurePosixPath(entry).as_posix() for entry in entries]
        elif key in _COERCED_KEYS:
            try:
                parsed = json.loads(value)
            except ValueError:
                # phrase the parse-failure message like the downstream type
                # checks so e.g. sync=maybe and sync=5 agree on what's expected
                if key in BOOL_KEYS:
                    expected = 'true, false, or null'
                elif key in INT_KEYS:
                    expected = 'an integer or null'
                else:
                    expected = 'a number or null'
                raise typer.BadParameter(
                    f'{key} expects {expected}; got {value!r}.'
                ) from None
            # enforce each key's type at the JSON boundary like init -- else
            # any well-formed JSON (list, float cap, bool cost, int flag)
            # stores and corrupts the loop; bool is an int subclass so it is
            # excluded from the numerics
            if key in BOOL_KEYS:
                if not isinstance(parsed, bool):
                    raise typer.BadParameter(
                        f'{key} expects true, false, or null; got {value!r}.'
                    )
            elif key in INT_KEYS:
                if not isinstance(parsed, int) or isinstance(parsed, bool):
                    raise typer.BadParameter(
                        f'{key} expects an integer or null; got {value!r}.'
                    )
                # max_iters must be positive (init rejects 0; a non-positive
                # cap reads as unlimited in the loop); the other caps allow 0
                if key == 'max_iters' and parsed <= 0:
                    raise typer.BadParameter('max_iters must be greater than 0.')
                if parsed < 0:
                    raise typer.BadParameter(f'{key} must be >= 0.')
                # an integer cap is written to a SQLite INTEGER column; one
                # that overflows a signed 64-bit int raises a raw adapter
                # error at the registry cap-sync, so reject it here like the
                # init/update flags do
                if parsed >= SQLITE_INT_MAX:
                    raise typer.BadParameter(f'{key} must be < {SQLITE_INT_MAX}.')
            elif isinstance(parsed, bool) or not isinstance(parsed, (int, float)):
                raise typer.BadParameter(
                    f'{key} expects a number or null; got {value!r}.'
                )
            # cost caps allow 0 but never negative (like the integer caps above)
            if key in COST_KEYS and parsed < 0:
                raise typer.BadParameter(f'{key} must be >= 0.')
            config[key] = parsed
        else:
            config[key] = value
    # the existence guard is on by default (public `config set`); init.sh's
    # private `config _set` passes check=False because it writes the very
    # config.json the guard checks, so it must resolve a not-yet-init node
    node = resolve_node(path, check=check)
    # validate the resulting config the way init does (the one merged
    # validator) -- this setter must not store a value init would reject (a
    # non-positive ceiling, an out-of-range reserve, a broken step<=iter<=run
    # ordering, or a bare-number duration that bricks the loop); merge the new
    # values over the current config so cross-key checks (e.g. reserve vs the
    # stored max_cost) hold
    merged = {
        key: config[key] if key in config else node.config.get(key) for key in KEYS
    }
    node.config.validate(merged)
    # init-fixed keys (root/user/project): the operator path (`node config
    # set`, check=True) may never write one -- reject its first write too, so
    # `config set user=true` can't brick a child (which carries no `user`) into
    # a root node and latch the tree-wide pause; init/internal writes (`config
    # _set`, check=False) set these once at bootstrap, so they keep the
    # first-write exemption but still can't change a set value; checked upfront
    # so a multi-key set stays atomic (no earlier key lands when a
    # later one is rejected)
    for key, value in config.items():
        if key in IMMUTABLE_KEYS:
            current = node.config.get(key)
            if value != current and (check or current is not None):
                raise typer.BadParameter(f'{key} is fixed at init and cannot be set.')
    # the confirmation echo needs each key's stored value from before the write
    priors = {key: node.config.get(key) for key in config} if echo else {}
    for key, value in config.items():
        node.config.set(key, value)
    # confirm each write, old -> new -- a silent mid-run retune is
    # indistinguishable from a dropped one
    if echo:
        for key, value in config.items():
            prior = _render_value(priors[key])
            current = _render_value(value)
            typer.echo(f'{key}: {prior} -> {current}')


def _render_value(value: object) -> str:
    """Render one config value for the set confirmation (``get``'s conventions)."""
    # unset/cleared keys render as 'unset'; booleans and structured values
    # render exactly as `config get` prints them, so the echo round-trips
    if value is None:
        return 'unset'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)
