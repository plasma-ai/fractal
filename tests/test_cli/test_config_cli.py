"""End-to-end tests for ``fractal config _set`` value validation.

Drives the real ``fractal`` console script as a subprocess against a throwaway
repo with a user node and one worker. ``config _set`` is the private setter the
node scripts use to write ``config.json``; it JSON-coerces a fixed set of keys,
so the tests pin that a well-formed-but-wrong-typed value (a list, a float cap, a
bool cost, an int flag) is rejected with a clean ``BadParameter`` rather than
silently corrupting the loop, and that a well-typed value breaking a launch
invariant is rejected by the core validator -- the same invariants ``init``
enforces.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from tests._helpers import _git

from .conftest import _run

__all__ = [
    'test_set_rejects_mistyped_coerced_values',
    'test_set_rejects_invariant_breaking_values',
    'test_set_accepts_well_typed_coerced_values',
    'test_public_node_config_get_set_round_trip',
    'test_public_set_scope_space_form_normalizes_to_init_shape',
    'test_get_rejects_unknown_keys_and_stays_silent_for_unset',
    'test_cost_scope_key_is_gone',
    'test_init_scope_space_form_stores_init_shape',
    'test_node_config_set_cannot_flip_user_flag',
    'test_node_config_set_cannot_promote_a_child_to_a_root',
    'test_corrupt_config_errors_naming_the_file',
]


@pytest.fixture(scope='module')
def task(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """Return a worker node's worktree, built once via the real CLI.

    Writing tests restore each key's prior value (or restore the config
    file in a ``finally`` block), and init-path tests use their own
    uniquely-named children, so siblings always see the fixture as built.
    """
    root = tmp_path_factory.mktemp('fractal_cfg')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'cfg@test.local')
    _git(root, 'config', 'user.name', 'cfg')
    (root / 'README.md').write_text('# cfg\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    assert _run(root, 'init').returncode == 0
    assert _run(root, 'node', 'init', 'task', '--agent', 'claude').returncode == 0
    return root / '.worktrees' / 'main.task'


@pytest.mark.parametrize(
    argnames=('entry', 'fragment'),
    argvalues=[
        # integer caps reject negatives, floats, lists, bools, and values at
        # the SQLite INTEGER ceiling (an oversized cap raises a raw adapter
        # error at the registry cap-sync)
        ('max_iters=-5', 'max_iters'),
        ('max_iters=0', 'max_iters'),
        ('max_iters=3.7', 'max_iters'),
        ('max_depth=-3', 'max_depth'),
        ('max_children=[1, 2]', 'max_children'),
        (f'max_children={2**63}', 'max_children'),
        ('max_descendants=true', 'max_descendants'),
        # boolean keys reject non-bool JSON (a non-bool corrupts SYNC/the user flag)
        ('sync=1', 'sync'),
        ('user=5', 'user'),
        ('detached=[1]', 'detached'),
        # cost keys reject bools and non-numbers (a clean error, never a TypeError)
        ('max_cost=true', 'max_cost'),
        ('max_cost=[1, 2]', 'max_cost'),
        ('reserve_budget="abc"', 'reserve_budget'),
        # cost caps allow 0 but reject negatives (parity with the integer caps)
        ('max_iter_cost=-1', 'max_iter_cost'),
        ('max_step_cost=-0.5', 'max_step_cost'),
    ],
)
def test_set_rejects_mistyped_coerced_values(
    task: pathlib.Path,
    entry: str,
    fragment: str,
) -> None:
    """A mistyped coerced value is a ``BadParameter`` (exit 2), not a stored value.

    Every case here is well-formed JSON of the wrong type for its key, so each
    is rejected at the boundary the way ``init`` would, leaving the key at its
    prior value rather than storing the corrupt one.
    """
    key, _, _ = entry.partition('=')
    before = _run(task, 'config', '_get', key).stdout.strip()
    result = _run(task, 'config', '_set', entry)
    assert result.returncode == 2, result.stdout + result.stderr
    assert fragment in (result.stdout + result.stderr)
    # the rejected write never landed -- the key keeps its prior value
    assert _run(task, 'config', '_get', key).stdout.strip() == before


@pytest.mark.parametrize(
    argnames=('entry', 'fragment'),
    argvalues=[
        # a $0 ceiling degenerates the subtree check
        ('max_cost=0', 'max_cost'),
        # cost caps reject non-finite values (NaN/Infinity slip past < 0 and <= 0)
        ('max_cost=NaN', 'max_cost'),
        ('max_step_cost=Infinity', 'max_step_cost'),
        # scope roots must be repo-relative -- an absolute or '..' root never
        # matches the commit pipeline's prefix check, bricking every scoped commit
        ('scope=/abs/root', 'scope'),
        ('scope=../sibling', 'scope'),
    ],
)
def test_set_rejects_invariant_breaking_values(
    task: pathlib.Path,
    entry: str,
    fragment: str,
) -> None:
    """A well-typed value breaking a launch invariant fails in core (exit 2).

    Every case here passes the CLI's typed coercion and is rejected only by
    the merged core validator (``Config.validate``), leaving the key at its
    prior value rather than storing a value ``init`` would refuse.
    """
    key, _, _ = entry.partition('=')
    before = _run(task, 'config', '_get', key).stdout.strip()
    result = _run(task, 'config', '_set', entry)
    assert result.returncode == 2, result.stdout + result.stderr
    assert fragment in (result.stdout + result.stderr)
    # the rejected write never landed -- the key keeps its prior value
    assert _run(task, 'config', '_get', key).stdout.strip() == before


def test_set_accepts_well_typed_coerced_values(task: pathlib.Path) -> None:
    """A correctly typed coerced value round-trips through ``_set``/``_get``.

    The boundary check must not over-reject: a real bool, a positive int cap, and
    a numeric cost all store and read back. Each key is restored to its prior
    value so the shared fixture is left as found.
    """
    cases = {
        'sync': ('false', 'false'),
        'max_iters': ('7', '7'),
        'max_cost': ('5.0', '5.0'),
    }
    for key, (value, expected) in cases.items():
        before = _run(task, 'config', '_get', key).stdout.strip()
        # the private script surface stays silent -- only the public set confirms
        written = _run(task, 'config', '_set', f'{key}={value}')
        assert written.returncode == 0
        assert written.stdout == ''
        assert _run(task, 'config', '_get', key).stdout.strip() == expected
        # restore the key's prior value (null when it was unset)
        restore = before if before else 'null'
        assert _run(task, 'config', '_set', f'{key}={restore}').returncode == 0


def test_public_node_config_get_set_round_trip(task: pathlib.Path) -> None:
    """The public ``node config get/set`` writes/reads and reuses the typed checks.

    The discoverable command delegates to the same validation as the private
    ``config _set``: a well-typed value round-trips, and a mistyped one (a bool
    cost) is rejected as ``BadParameter`` (exit 2) without landing.
    """
    before = _run(task, 'node', 'config', 'get', 'max_iters').stdout.strip()
    # a well-typed value writes via the public setter and reads via the getter,
    # confirming the change old -> new (a mid-run retune is otherwise silent)
    prior = before if before else 'unset'
    written = _run(task, 'node', 'config', 'set', 'max_iters=9')
    assert written.returncode == 0
    assert f'max_iters: {prior} -> 9' in written.stdout
    assert _run(task, 'node', 'config', 'get', 'max_iters').stdout.strip() == '9'
    # a mistyped value is rejected (the shared typed validation), leaving 9
    rejected = _run(task, 'node', 'config', 'set', 'max_cost=true')
    assert rejected.returncode == 2, rejected.stdout + rejected.stderr
    assert 'max_cost' in (rejected.stdout + rejected.stderr)
    assert _run(task, 'node', 'config', 'get', 'max_iters').stdout.strip() == '9'
    # restore the key's prior value (null when it was unset)
    restore = before if before else 'null'
    assert _run(task, 'node', 'config', 'set', f'max_iters={restore}').returncode == 0


def test_public_set_scope_space_form_normalizes_to_init_shape(
    task: pathlib.Path,
) -> None:
    """The public setter splits a space-form scope into the init shape.

    A comma-only split at the write boundary would persist a one-entry
    list that consumers of the canonical list form mis-read: space, comma,
    and mixed forms must all land as the init-canonical split list, and a
    string-form scope must keep reading back split. Entries land in
    canonical path form too -- the commit boundary is a literal string
    prefix against git's canonical paths, so a stored ``./src`` or
    ``src/`` would read every change as out of scope and refuse every
    commit.
    """
    config_path = task / '.fractal' / 'main.task' / 'config.json'
    # the space form lands split, not as a one-entry ['roots/a roots/b']
    written = _run(task, 'node', 'config', 'set', 'scope=roots/a roots/b')
    assert written.returncode == 0
    stored = json.loads(config_path.read_text(encoding='utf-8'))['scope']
    assert stored == ['roots/a', 'roots/b']
    # comma and mixed forms agree on the same canonical shape
    written = _run(task, 'node', 'config', 'set', 'scope=roots/a,roots/b roots/c')
    assert written.returncode == 0
    stored = json.loads(config_path.read_text(encoding='utf-8'))['scope']
    assert stored == ['roots/a', 'roots/b', 'roots/c']
    # non-canonical spellings land canonical ('.' is already its own)
    written = _run(task, 'node', 'config', 'set', 'scope=./roots/a roots/b/ roots//c .')
    assert written.returncode == 0
    stored = json.loads(config_path.read_text(encoding='utf-8'))['scope']
    assert stored == ['roots/a', 'roots/b', 'roots/c', '.']
    # a space-joined string value still reads back split (read normalization intact)
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config['scope'] = 'roots/a roots/b'
    config_path.write_text(json.dumps(config), encoding='utf-8')
    got = _run(task, 'node', 'config', 'get', 'scope')
    assert got.returncode == 0
    assert got.stdout.splitlines() == ['roots/a', 'roots/b']
    # restore: clear scope so later tests see the fixture's initial shape
    assert _run(task, 'node', 'config', 'set', 'scope=null').returncode == 0


@pytest.mark.parametrize(
    argnames='surface',
    argvalues=[('config', '_get'), ('node', 'config', 'get')],
    ids=['private', 'public'],
)
def test_get_rejects_unknown_keys_and_stays_silent_for_unset(
    task: pathlib.Path,
    surface: tuple[str, ...],
) -> None:
    """Both getter surfaces validate keys like the setter; an unset key is silent.

    An unknown key is a ``BadParameter`` (exit 2) naming the valid keys, so a
    typo'd probe cannot read as unset; a valid-but-unset key keeps printing
    nothing with exit 0 -- the contract the node scripts branch on.
    """
    # an unknown key is rejected with the setter's message and the key list
    result = _run(task, *surface, 'no_such_key')
    assert result.returncode == 2, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert 'Unknown config key' in output
    assert 'max_cost' in output
    # a valid key with no stored value prints nothing and exits 0
    unset = _run(task, *surface, 'meta')
    assert unset.returncode == 0, unset.stderr
    assert unset.stdout == ''


def test_cost_scope_key_is_gone(task: pathlib.Path) -> None:
    """``cost_scope`` is not a config key: runs are isolated by design.

    There is no lifetime scope knob, so ``cost_scope`` must stay absent
    from the write and read surfaces and the init path.
    """
    root = task.parents[1]  # task == <root>/.worktrees/main.task
    # the setter rejects cost_scope like any unknown key
    result = _run(root, 'node', 'config', 'set', 'cost_scope=run')
    assert result.returncode == 2, result.stderr
    assert 'Unknown config key' in (result.stdout + result.stderr)
    # the getter rejects it the same way -- a probe cannot read it as unset
    result = _run(root, 'node', 'config', 'get', 'cost_scope')
    assert result.returncode == 2, result.stderr
    assert 'Unknown config key' in (result.stdout + result.stderr)
    # a fresh child config carries no cost_scope entry
    assert _run(root, 'node', 'init', 'cs_gone', '--agent', 'claude').returncode == 0
    child = root / '.worktrees' / 'main.cs_gone'
    config = json.loads(
        (child / '.fractal' / 'main.cs_gone' / 'config.json').read_text(
            encoding='utf-8'
        )
    )
    assert 'cost_scope' not in config


def test_init_scope_space_form_stores_init_shape(task: pathlib.Path) -> None:
    """Init-path space and mixed scope forms land as the canonical list.

    ``init.sh`` writes ``--scope`` through the shared ``config _set``
    normalization, so a space form cannot mis-parse into one mangled root
    -- this pins init against a writer that bypasses the shared split.
    """
    root = task.parents[1]  # task == <root>/.worktrees/main.task
    # one mixed comma+space value exercises both separators in one init
    result = _run(
        root,
        'node',
        'init',
        'sc_mixed',
        '--agent',
        'claude',
        '--scope=roots/a,roots/b roots/c',
    )
    assert result.returncode == 0, result.stdout + result.stderr
    child = root / '.worktrees' / 'main.sc_mixed'
    config = json.loads(
        (child / '.fractal' / 'main.sc_mixed' / 'config.json').read_text(
            encoding='utf-8'
        )
    )
    assert config['scope'] == ['roots/a', 'roots/b', 'roots/c']


def test_node_config_set_cannot_flip_user_flag(task: pathlib.Path) -> None:
    """``config set user=false`` cannot flip an initialized node's identity.

    A user (root) node carries ``user: true`` and ``node start`` refuses to launch
    it; allowing a later ``config set user=false`` would bypass that guard. The
    setter rejects the change upfront as ``BadParameter`` (exit 2) and leaves
    ``user`` true -- atomically, so a multi-key set carrying the immutable key
    writes none of its keys. ``init`` writes the flag directly, so the
    first-write-at-init path is unaffected.
    """
    root = task.parents[1]  # task == <root>/.worktrees/main.task
    assert _run(root, 'config', '_get', 'user').stdout.strip() == 'true'
    rejected = _run(root, 'node', 'config', 'set', 'user=false')
    assert rejected.returncode == 2, rejected.stdout + rejected.stderr
    assert 'user' in (rejected.stdout + rejected.stderr)
    assert _run(root, 'config', '_get', 'user').stdout.strip() == 'true'
    # a multi-key set is all-or-nothing: the immutable key's rejection must
    # not leave the earlier key's write (or its confirmation echo) behind
    before = _run(root, 'config', '_get', 'max_iters').stdout.strip()
    rejected = _run(root, 'node', 'config', 'set', 'max_iters=7', 'user=false')
    assert rejected.returncode == 2, rejected.stdout + rejected.stderr
    assert 'max_iters' not in rejected.stdout
    assert _run(root, 'config', '_get', 'max_iters').stdout.strip() == before


def test_node_config_set_cannot_promote_a_child_to_a_root(task: pathlib.Path) -> None:
    """``config set user=true`` on a child cannot brick it into a root node.

    A child carries no ``user`` key, so a first-write-only gate let the
    operator set ``user=true`` -- promoting the child to a root node and
    latching the tree-wide pause. The setter rejects any operator write of the
    init-fixed key (exit 2), first write included, leaving the child unset.
    """
    # task == <root>/.worktrees/main.task, a spawned child with no `user` key
    assert _run(task, 'config', '_get', 'user').stdout.strip() == ''
    rejected = _run(task, 'node', 'config', 'set', 'user=true')
    assert rejected.returncode == 2, rejected.stdout + rejected.stderr
    assert 'user' in (rejected.stdout + rejected.stderr)
    # the child stays a non-user node -- no first write landed
    assert _run(task, 'config', '_get', 'user').stdout.strip() == ''


def test_corrupt_config_errors_naming_the_file(task: pathlib.Path) -> None:
    """A hand-corrupted ``config.json`` fails with an error naming the file.

    A bare ``json.loads`` of a broken config yields a context-free ``Expecting
    value: line 1 column 1`` that points at nothing; a config-reading command
    must instead surface the offending file path so the operator knows what to
    fix. The original config is restored so the shared fixture is left as found.
    """
    config_path = task / '.fractal' / 'main.task' / 'config.json'
    original = config_path.read_text(encoding='utf-8')
    try:
        config_path.write_text('NOT JSON', encoding='utf-8')
        result = _run(task, 'config', '_get', 'max_cost')
        assert result.returncode != 0, result.stdout + result.stderr
        assert 'config.json' in (result.stdout + result.stderr)
    finally:
        config_path.write_text(original, encoding='utf-8')
