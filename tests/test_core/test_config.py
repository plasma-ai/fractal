"""Test the ``fractal.core.config`` module."""

from __future__ import annotations

import pathlib
import subprocess
import sys
from typing import Optional

import pytest

from fractal.core.config import parse_reserve_budget
from fractal.core.node import Node

from .conftest import _spawn_parent_child

__all__ = [
    'test_config_get_set',
    'test_config_get_emits_shell_booleans',
    'test_config_set_rejects_immutable_key_change',
    'test_config_set_serializes_concurrent_writers',
    'test_validate_rejects_launch_invariant_violations',
    'test_validate_reads_stored_config_and_passes_valid',
    'test_parse_reserve_budget_resolves',
    'test_parse_reserve_budget_honors_custom_default',
    'test_parse_reserve_budget_rejects',
    'test_caps_reconcile_heals_registry_from_config',
]


# ------ get / set


def test_config_get_set(node_with_db: Node) -> None:
    """Config read/write with various value types."""
    node = node_with_db

    # set values
    node.config.set('max_iters', 5)
    node.config.set('timeout', '30m')
    node.config.set('sync', True)
    node.config.set('provider', 'openrouter')
    node.config.set('effort', 'xhigh')

    # get values
    assert node.config.get('max_iters') == 5
    assert node.config.get('timeout') == '30m'
    assert node.config.get('sync') is True
    assert node.config.get('provider') == 'openrouter'
    assert node.config.get('effort') == 'xhigh'
    assert node.config.get('missing') is None

    # null clears the free-string route back to the vendor-native default
    node.config.set('provider', None)
    assert node.config.get('provider') is None


def test_config_get_emits_shell_booleans(
    node_with_db: Node,
    git_repo: pathlib.Path,
) -> None:
    """``fractal config get`` prints lowercase booleans for the shell scripts.

    The lifecycle scripts capture ``config get`` output into shell variables and
    compare against lowercase ``true``/``false`` (the codex detached guard, the
    ``--local`` push gate, detached-mode activation). Python ``bool`` values must
    render as ``true``/``false``, not ``True``/``False``.
    """
    node_with_db.config.set('detached', True)
    node_with_db.config.set('local', False)
    for key, expected in (('detached', 'true'), ('local', 'false')):
        result = subprocess.run(
            ['fractal', 'config', '_get', '--path', f'{git_repo}', key],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == expected


def test_config_set_rejects_immutable_key_change(node_with_db: Node) -> None:
    """Immutable keys admit their initial write but never a change.

    ``root`` anchors the central database for the whole tree, ``user``
    marks node identity, and ``project`` fixes the on-disk layout the
    .project cache mirrors -- a post-init change to any would silently
    corrupt the tree. The initial write (and a same-value rewrite) must stay
    legal: init writes these keys through the same setter.
    """
    node = node_with_db
    # the fixture config carries 'root'; seed the other keys' initial writes
    node.config.set('user', True)
    node.config.set('project', '.')
    for key in ('root', 'user', 'project'):
        current = node.config.get(key)
        # a same-value rewrite is not a change
        node.config.set(key, current)
        assert node.config.get(key) == current
        # a change is rejected and the stored value survives
        with pytest.raises(ValueError, match=f'{key} is fixed at init'):
            node.config.set(key, 'other')
        assert node.config.get(key) == current


def test_config_set_serializes_concurrent_writers(
    node_with_db: Node,
    tmp_path: pathlib.Path,
) -> None:
    """Setters in concurrent processes never revert each other's keys.

    Legitimate concurrent writers exist -- a parent retune racing the
    child's own title write -- and each set is a read-merge-write of the
    whole file, so without cross-process serialization the loser's key
    silently reverts to the snapshot the winner loaded.
    """
    node = node_with_db
    writes = 25
    # each worker hammers its own key from a separate process; the go-file
    # barrier holds both write loops until imports finish, so they overlap
    # instead of serializing on interpreter startup
    go_file = tmp_path / 'go'
    script = (
        'import pathlib, sys, time\n'
        'from fractal.core.node import Node\n'
        'node = Node(sys.argv[1])\n'
        'while not pathlib.Path(sys.argv[4]).exists():\n'
        '    time.sleep(0.001)\n'
        'for i in range(int(sys.argv[3])):\n'
        "    node.config.set(sys.argv[2], f'{sys.argv[2]}-{i}')\n"
    )
    workers = [
        subprocess.Popen(
            [
                sys.executable,
                '-c',
                script,
                str(node.worktree),
                key,
                str(writes),
                str(go_file),
            ],
        )
        for key in ('title', 'model')
    ]
    go_file.touch()
    for worker in workers:
        assert worker.wait() == 0
    # both writers' final values survive -- neither key reverted
    assert node.config.get('title') == f'title-{writes - 1}'
    assert node.config.get('model') == f'model-{writes - 1}'


# ------ launch validation


@pytest.mark.parametrize(
    argnames=('config', 'match'),
    argvalues=[
        ({'max_cost': float('nan')}, 'finite number'),
        ({'reserve_budget': float('inf')}, 'finite number'),
        ({'max_cost': '5'}, 'must be a number'),
        ({'max_cost': True}, 'must be a number'),
        ({'max_cost': 0}, 'greater than 0'),
        ({'max_cost': -1.0}, 'greater than 0'),
        ({'max_cost': 10.0, 'max_iter_cost': 0}, 'greater than 0'),
        ({'max_cost': 10.0, 'max_step_cost': -1.0}, 'greater than 0'),
        ({'max_iters': 0}, 'greater than 0'),
        ({'max_iters': '5'}, 'must be an integer'),
        ({'max_children': -1}, 'must be >= 0'),
        ({'sync': 'false'}, 'must be a boolean'),
        ({'detached': 1}, 'must be a boolean'),
        ({'max_cost': 10.0, 'reserve_budget': -0.5}, 'must be >= 0'),
        ({'max_cost': 10.0, 'reserve_budget': 9.95}, '99%'),
        ({'max_cost': 5.0, 'max_iter_cost': 6.0}, 'exceeds max_cost'),
        (
            {'max_cost': 5.0, 'max_iter_cost': 1.0, 'max_step_cost': 2.0},
            'exceeds max_iter_cost',
        ),
        ({'max_cost': 5.0, 'max_step_cost': 6.0}, 'exceeds max_cost'),
        ({'max_iter_cost': 2.0}, 'max_iter_cost requires max_cost'),
        ({'max_step_cost': 1.0}, 'max_step_cost requires max_cost'),
        ({'sleep': '10'}, 'unit suffix'),
        ({'step_timeout': '0s'}, 'at least 1 second'),
        ({'wait': '0.5s'}, 'at least 1 second'),
        ({'interval': '30m', 'sleep': '10s'}, 'mutually exclusive'),
        ({'interval': '30m', 'iter_timeout': '2h'}, 'exceeds'),
        ({'scope': ['src', '/abs/root']}, 'repo-relative'),
        ({'scope': ['../sibling']}, 'repo-relative'),
        ({'scope': ['./src']}, 'canonical'),
        ({'scope': ['src/']}, 'canonical'),
        ({'scope': 123}, 'list of strings'),
        ({'clone_dirs': ['../sibling/.cache']}, 'repo-relative'),
        ({'clone_dirs': ['.']}, 'must name a subdirectory'),
        ({'clone_dirs': 123}, 'list of strings'),
    ],
    ids=[
        'nan_cost',
        'inf_reserve',
        'string_cost',
        'bool_cost',
        'zero_cost',
        'negative_cost',
        'zero_iter_cost',
        'negative_step_cost',
        'zero_iters',
        'string_iters',
        'negative_children',
        'string_sync',
        'int_detached',
        'negative_reserve',
        'reserve_over_99_percent',
        'iter_cost_over_run',
        'step_cost_over_iter',
        'step_cost_over_run',
        'iter_cost_without_ceiling',
        'step_cost_without_ceiling',
        'bare_number_duration',
        'zero_duration',
        'subsecond_duration',
        'interval_sleep_conflict',
        'iter_timeout_over_interval',
        'absolute_scope_root',
        'dotdot_scope_root',
        'dot_slash_scope_root',
        'trailing_slash_scope_root',
        'non_list_scope',
        'dotdot_clone_dir',
        'root_clone_dir',
        'non_list_clone_dirs',
    ],
)
def test_validate_rejects_launch_invariant_violations(
    node_with_db: Node,
    config: dict,
    match: str,
) -> None:
    """``validate`` rejects every config the loop cannot safely launch on.

    Non-numeric or non-finite costs slip past every comparison, a
    non-positive ceiling degenerates the budget check into an instant $0
    finish, a per-iter/step cap with no ceiling runs unbounded once the
    per-iter budget drains, an out-of-range reserve or broken
    ``step <= iter <= run`` ordering corrupts the budget math, a
    non-integer or degenerate integer cap corrupts the loop's caps (a
    non-positive ``max_iters`` reads as unlimited), a non-bool mode flag
    flips the run mode (the loop's ``bool()`` coercion reads a hand-edited
    ``"false"`` string as ``True``), a bare-number or zero-truncating
    duration bricks the loop at launch or crashes its mid-run re-reads,
    an absolute or ``..`` list-key entry points outside the tree (a scope
    root never matches the commit pipeline's relative prefix check,
    bricking every scoped commit; a ``clone_dirs`` entry would reach
    outside the worktree it warms), a non-canonical spelling (``./src``,
    ``src/``) slips past the setters only by hand-edit and would read
    every change as out of scope, and a ``.`` cache dir would clone the
    entire checkout over the worktree root -- while the same ``.`` is a
    legal scope root, naming the project itself. Only the keys present in
    the mapping are checked, so each case isolates one invariant.
    """
    with pytest.raises(ValueError, match=match):
        node_with_db.config.validate(config)


def test_validate_reads_stored_config_and_passes_valid(node_with_db: Node) -> None:
    """``validate()`` without a mapping re-checks the stored ``config.json``.

    The documented steering path edits the file directly, bypassing the
    setters' checks, so ``start`` re-validates the stored config: a coherent
    config passes silently and a hand-edited bare-number duration fails
    loudly before any launch.
    """
    node = node_with_db
    node.config.set('max_cost', 10.0)
    node.config.set('max_iter_cost', 4.0)
    node.config.set('timeout', '30m')
    node.config.validate()
    # a raw write that bypasses validation is caught on the stored re-read
    node.config.set('sleep', '10')
    with pytest.raises(ValueError, match='unit suffix'):
        node.config.validate()


# ------ reserve budget parsing


@pytest.mark.parametrize(
    argnames=('value', 'max_cost', 'expected'),
    argvalues=[
        (None, None, None),  # absent + no budget -> no reserve
        (None, 10.0, 1.0),  # absent -> 10% of max_cost (the default)
        ('2.5', 10.0, 2.5),  # bare number is USD
        ('20%', 10.0, 2.0),  # percent of max_cost
        ('0', 10.0, 0.0),  # zero is allowed (reserve mode at remaining <= 0)
        ('0%', 10.0, 0.0),
        # money materializes at display precision -- a bare 0.1 * 6.0 float
        # product would persist binary noise (0.6000000000000001) into
        # config.json and the retune echo
        (None, 6.0, 0.6),
        ('10%', 3.0, 0.3),
    ],
    ids=[
        'no_budget',
        'default_ten_percent',
        'bare_usd',
        'percent',
        'zero_usd',
        'zero_percent',
        'precise_default',
        'precise_percent',
    ],
)
def test_parse_reserve_budget_resolves(
    value: Optional[str],
    max_cost: Optional[float],
    expected: Optional[float],
) -> None:
    """A reserve resolves to USD: a number, ``N%``, or the 10% default of max_cost."""
    assert parse_reserve_budget(value, max_cost) == expected


def test_parse_reserve_budget_honors_custom_default() -> None:
    """The ``default`` argument sets the reserve used when ``value`` is ``None``."""
    assert parse_reserve_budget(None, 10.0, default='20%') == 2.0
    assert parse_reserve_budget(None, 10.0, default='1.5') == 1.5


@pytest.mark.parametrize(
    argnames=('value', 'max_cost', 'message'),
    argvalues=[
        ('2.5', None, '--max-cost'),  # requires max_cost (number form)
        ('20%', None, '--max-cost'),  # requires max_cost (percent form)
        ('99%', 10.0, '99%'),  # >= 99% of max_cost
        ('9.9', 10.0, '99%'),  # 9.9 == 0.99 * 10 (the bound is inclusive)
        ('-1', 10.0, '>= 0'),  # negative
        ('nope', 10.0, 'number'),  # non-numeric
    ],
    ids=[
        'no_max_cost_usd',
        'no_max_cost_percent',
        'reserve_at_99_percent',
        'inclusive_bound',
        'negative',
        'non_numeric',
    ],
)
def test_parse_reserve_budget_rejects(
    value: Optional[str],
    max_cost: Optional[float],
    message: str,
) -> None:
    """Invalid reserve budgets raise ``ValueError`` with a pointed message."""
    with pytest.raises(ValueError, match=message):
        parse_reserve_budget(value, max_cost)


# ------ caps reconcile


def test_caps_reconcile_heals_registry_from_config(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Config.reconcile`` pushes drifted config caps over the registry row.

    A post-spawn cap edit in the config file is live enforcement truth (the
    loop reads config), but the registry row keeps the spawn-time values and
    silently fools every reader (a node can be killed at the stale cap
    this way). Config wins: the row is healed, the drift is reported as
    ``{key: (config, registry)}``, undrifted and config-absent keys are left
    alone, and a node without a registry row (the user node) is a no-op.
    """
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    # seed the registry caps via the blessed path, then drift the config
    # directly -- a committed edit, no node update
    parent.child_update('kid', max_cost=100.0, max_children=2)
    child.config.set('max_cost', 175.0)
    drifted = child.config.reconcile()
    assert drifted == {'max_cost': (175.0, 100.0)}
    row = child.db.read('nodes', where={'node': child.branch}, limit=1)[0]
    assert row['max_cost'] == 175.0
    assert row['max_children'] == 2
    # a reconciled node has nothing further to report
    assert child.config.reconcile() == {}
    # the user node has no registry row -- reconcile is a no-op
    user = Node(git_repo)
    assert user.config.reconcile() == {}
