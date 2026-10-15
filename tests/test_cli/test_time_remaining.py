"""User-facing contract of ``fractal node time remaining`` (the CLI display).

The method-level math (``Time.remaining`` run/iter countdown, clamp,
soonest-of-both) is pinned in ``tests/test_core/test_time.py`` and
``test_time_iteration_scope.py``. This file pins the **CLI command** layer
(``fractal/cli/cmd/time.py``) that ``test_core`` never exercises: which of the
three user-facing strings the command prints, and the ``int()`` truncation of the
displayed seconds.

``Time.remaining`` returns ``None`` (no timeout / nothing running) or a float;
the command turns that into exactly one of:

- ``"no limit"`` -- no ``timeout``/``iter_timeout``/``step_timeout`` configured;
- ``"not running"`` -- a timeout is set but nothing active counts down;
- ``"<N>s"`` -- ``int(remaining)`` for the soonest active deadline (run,
  iteration, or step).

These states are reached by driving the real ``fractal`` console script as a
subprocess against a node whose run/iteration rows are created in-process via the
``Node`` API and back-dated to simulate elapsed time (deterministic, no sleeps).
This is the one deviation from the surrounding test_cli files, which drive the
loop rather than import ``Node``: ``Node`` here is setup-only (it just writes DB
rows the subprocess reads), so the schema-stable in-process copy is immaterial --
only the subprocess, which runs the code under test, must resolve to this worktree
(the ``_cli_env`` PYTHONPATH prepend guarantees it, mirroring
``test_cost_continue.py``).
"""

from __future__ import annotations

import json
import pathlib
from typing import Optional

import pytest

from fractal.core.node import Node
from tests._helpers import _age_iter, _age_run, _age_step, _git

from .conftest import _run

__all__ = [
    'test_remaining_reports_no_limit_without_timeout',
    'test_remaining_reports_not_running_without_active_run',
    'test_remaining_reports_not_running_with_only_step_timeout',
    'test_remaining_counts_down_for_the_run',
    'test_remaining_counts_down_for_the_active_iteration',
    'test_remaining_counts_down_for_the_active_step',
]

# a 10-minute budget, in the suffix form the loop validates
TIMEOUT = '10m'
TIMEOUT_SECONDS = 600

# a step file's own timeout: line, longer than the node's step_timeout
STEP_TIMEOUT = '30m'
STEP_TIMEOUT_SECONDS = 1800


@pytest.fixture
def time_node(tmp_path: pathlib.Path) -> dict:
    """Return a minimal initialized node (config + DB) resolvable by ``--path``.

    Mirrors the ``node_with_db`` core fixture but stands alone for test_cli: a
    git repo with a root node (``config.json`` + initialized ``.db``), no
    worktree or loop. Tests set the timeout and create run/iteration rows on the
    ``node`` directly, then assert the ``fractal`` subprocess's rendered output
    against the ``repo`` path. Returns ``{'node', 'repo'}``.
    """
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-b', 'main')
    _git(repo, 'config', 'user.email', 'timeremaining@test.local')
    _git(repo, 'config', 'user.name', 'timeremaining')
    (repo / 'README.md').write_text('# time-remaining\n', encoding='utf-8')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'init')
    # root node: config.json marks it initialized; .db carries run/iteration rows
    node = Node(repo)
    node_dir = repo / '.fractal' / 'main'
    node_dir.mkdir(parents=True)
    config = {
        'project': '.',
        'root': 'main',
        'scope': '',
        'agent': 'claude',
        'local': False,
        'detached': False,
    }
    (node_dir / 'config.json').write_text(
        json.dumps(config, indent=2),
        encoding='utf-8',
    )
    (node_dir / '.status').write_text('idle\n', encoding='utf-8')
    node.db.init()
    return {'node': node, 'repo': repo}


# ------ the three output branches


def test_remaining_reports_no_limit_without_timeout(time_node: dict) -> None:
    """No configured timeout -> ``"no limit"`` even with an active iteration."""
    node = time_node['node']
    run_id = node.record.run_start()
    node.record.iter_start(run_id=run_id, iter=1)
    assert _remaining(time_node['repo']) == 'no limit'


def test_remaining_reports_not_running_without_active_run(time_node: dict) -> None:
    """A configured timeout but nothing active -> ``"not running"``.

    Distinct from ``"no limit"``: the budget exists, there is just no run or
    iteration to count down.
    """
    node = time_node['node']
    node.config.set('timeout', TIMEOUT)
    assert _remaining(time_node['repo']) == 'not running'


def test_remaining_reports_not_running_with_only_step_timeout(time_node: dict) -> None:
    """A lone ``--step-timeout`` is still a limit -> ``"not running"``, not ``"no limit"``.

    A configured step timeout still time-bounds the node -- there is just
    nothing active to count down against.
    """
    node = time_node['node']
    node.config.set('step_timeout', TIMEOUT)
    assert _remaining(time_node['repo']) == 'not running'


def test_remaining_counts_down_for_the_run(time_node: dict) -> None:
    """A ``--timeout`` run renders ``int(timeout - elapsed)`` whole seconds.

    Back-dating the run 100s into a 600s budget must report a whole number well
    inside the budget -- not the full ``timeout`` and not ``0`` -- and the output
    must be an integer (the command truncates the float with ``int()``).
    """
    node = time_node['node']
    node.config.set('timeout', TIMEOUT)
    run_id = node.record.run_start()
    node.record.iter_start(run_id=run_id, iter=1)
    _age_run(node, run_id, 100.0)
    seconds = _remaining_seconds(time_node['repo'])
    # aged 100s of a 600s budget -> remaining provably <= 500; generous lower
    # slack absorbs subprocess startup
    assert TIMEOUT_SECONDS - 150 <= seconds <= TIMEOUT_SECONDS - 100


def test_remaining_counts_down_for_the_active_iteration(time_node: dict) -> None:
    """An ``--iter-timeout`` active iteration renders ``int(budget - elapsed)``.

    Mirrors the run-scope case at the iteration level: the command reports the
    soonest deadline, here the only configured one.
    """
    node = time_node['node']
    node.config.set('iter_timeout', TIMEOUT)
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    _age_iter(node, iter_id, 100.0)
    seconds = _remaining_seconds(time_node['repo'])
    assert TIMEOUT_SECONDS - 150 <= seconds <= TIMEOUT_SECONDS - 100


@pytest.mark.parametrize(
    argnames='scope',
    argvalues=[None, 'step'],
    ids=['soonest', 'step-scope'],
)
@pytest.mark.parametrize(
    argnames=('frontmatter', 'budget'),
    argvalues=[
        ('', TIMEOUT_SECONDS),
        (f'---\ntimeout: {STEP_TIMEOUT}\n---\n', STEP_TIMEOUT_SECONDS),
    ],
    ids=['node-step-timeout', 'own-timeout'],
)
def test_remaining_counts_down_for_the_active_step(
    time_node: dict,
    frontmatter: str,
    budget: int,
    scope: Optional[str],
) -> None:
    """An active step renders ``int(budget - elapsed)`` against its own ceiling.

    A loop node commonly runs with only a step budget, so its one active
    deadline must render as a countdown like the run and iteration scopes --
    counted against the step file's own ``timeout:`` line when it carries
    one (the ceiling the loop enforces), else the node's ``--step-timeout``.
    """
    node = time_node['node']
    node.config.set('step_timeout', TIMEOUT)
    steps_dir = node.node_dir / 'steps'
    steps_dir.mkdir()
    (steps_dir / '01-EXECUTE.md').write_text(
        f'{frontmatter}# EXECUTE\n\nWork.\n',
        encoding='utf-8',
    )
    run_id = node.record.run_start()
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    step_id = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='EXECUTE',
    )
    _age_step(node, step_id, 100.0)
    seconds = _remaining_seconds(time_node['repo'], scope=scope)
    assert budget - 150 <= seconds <= budget - 100


# ------ helpers


def _remaining(repo: pathlib.Path, *, scope: Optional[str] = None) -> str:
    """Run ``fractal node time remaining`` against ``repo`` and return stdout."""
    args = ['node', 'time', 'remaining', '--path', f'{repo}']
    if scope is not None:
        args.append(f'--scope={scope}')
    result = _run(repo, *args)
    assert result.returncode == 0, f'rc={result.returncode}\nstderr:\n{result.stderr}'
    return result.stdout.strip()


def _remaining_seconds(repo: pathlib.Path, *, scope: Optional[str] = None) -> int:
    """Parse the ``"<N>s"`` countdown output into an int (asserts the format)."""
    out = _remaining(repo, scope=scope)
    assert out.endswith('s')
    seconds = out.removesuffix('s')
    assert seconds.isdigit()
    return int(seconds)
