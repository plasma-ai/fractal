"""End-to-end lifecycle tests for the ``fractal`` CLI.

These tests drive the real console script as a subprocess against a
throwaway git repo, exercising a full node lifecycle as a user would:
``fractal init`` (the user/root node), ``node init`` (a worker), a
parent<->child radio round-trip, a cost round-trip, retire/unretire, a
stubbed launch, and finally a ``db _query`` that confirms the persisted
trail.

The point is to test behavior ("does the workflow work end to end?")
rather than internals -- assertions look only at CLI stdout/exit codes
and at rows read back through the ``db _query`` command.

Delegation nests a child *for* a worker under that worker (``main.task.c1``):
an agent runs ``node init`` from inside its own worktree and the caller is
resolved from ``_NODE``, so the round-trip both nests correctly and delivers
the child's radio message up to its parent.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

from tests._helpers import _git

from .conftest import _await_settled, _cli_env, _fractal_bin, _require_tmux, _run

__all__ = [
    'test_full_node_lifecycle_round_trip',
    'test_delegation_nests_child_under_worker_and_radios_up',
]

# minimal claude stand-in: emits the init + result frames the stream driver
# expects and exits 0, so a launched loop completes an iteration hermetically
_AGENT_STUB = """#!/usr/bin/env bash
SID=$(uuidgen | tr '[:upper:]' '[:lower:]')
printf '{"type":"system","subtype":"init","session_id":"%s"}\\n' "$SID"
printf '{"type":"result","session_id":"%s","total_cost_usd":0.001,"num_turns":1,"duration_ms":1}\\n' "$SID"
"""


@pytest.fixture(scope='module')
def repo(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Return a repo with a user (root) node and one worker node (``task``).

    Built once via the real CLI so the lifecycle tests exercise
    ``fractal init`` (which also writes ``.git/info/exclude``) and
    ``node init`` exactly as a user would. Shared mutation is convergent:
    the round trip parks ``task`` on its run's terminal status and
    delegation adds only its own nested child, so the tests never collide.
    """
    root = tmp_path_factory.mktemp('fractal_lifecycle')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'lifecycle@test.local')
    _git(root, 'config', 'user.name', 'lifecycle')
    (root / 'README.md').write_text('# lifecycle\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    # fractal init creates the user node and writes .git/info/exclude
    assert _run(root, 'init').returncode == 0
    init = _run(
        root,
        'node',
        'init',
        'task',
        '--agent',
        'claude',
        '--max-iters',
        '1',
        '--no-sync',
        '--local',
    )
    assert init.returncode == 0
    task = root / '.worktrees' / 'main.task'
    # one trivial committed step so the stubbed launch settles quickly
    steps_dir = task / '.fractal' / 'main.task' / 'steps'
    for step in steps_dir.glob('*.md'):
        step.unlink()
    (steps_dir / '01-work.md').write_text('# Work\n\nOne step.\n', encoding='utf-8')
    _git(task, 'add', '-A')
    _git(task, 'commit', '-m', 'setup task')
    # stub agent bindir (start.sh propagates PATH into the tmux session)
    bindir = root / 'bin'
    bindir.mkdir()
    agent = bindir / 'claude'
    agent.write_text(_AGENT_STUB, encoding='utf-8')
    agent.chmod(0o755)
    return {
        'root': root,
        'task': task,
        'task_dir': task / '.fractal' / 'main.task',
        'bindir': bindir,
    }


# ------ flagship lifecycle


def test_full_node_lifecycle_round_trip(repo: dict) -> None:
    """A user drives a worker through its whole lifecycle, end to end.

    Walks the realistic path -- user init, node init, a two-way radio
    exchange, a cost budget round-trip, retire/unretire, and a stubbed
    launch -- then reads the persisted trail back through ``db _query``
    to confirm the workflow actually landed in the databases.
    """
    _require_tmux()
    root, task = repo['root'], repo['task']
    # child radios up to its parent; the message lands in the user's inbox
    up = _run(
        task,
        'radio',
        'send',
        'status report',
        '--parent',
        '--channel',
        'inbox',
        '--subject',
        'up',
        '--priority',
        '5',
    )
    assert up.returncode == 0, up.stderr
    up_uuid = up.stdout.strip()
    # parent radios back down into the child's inbox; the child can read it
    down = _run(
        root,
        'radio',
        'send',
        'keep going',
        '--node',
        'main.task',
        '--channel',
        'inbox',
        '--subject',
        'down',
        '--priority',
        '5',
    )
    down_uuid = down.stdout.split()[0]
    assert down_uuid in _run(task, 'radio', 'read', down_uuid).stdout
    # cost round-trip: parent sets a budget, child reads it back
    assert (
        _run(root, 'node', 'update', 'main.task', '--max-cost', '2.5').returncode == 0
    )
    assert _run(task, 'node', 'cost', 'remaining').stdout.strip() == '$2.5000'
    # lifecycle round-trip: status reflects retire then unretire
    assert 'retired' in _run(task, 'node', 'retire').stdout.lower()
    assert _run(task, 'node', 'status').stdout.strip() == 'retired'
    assert 'unretired' in _run(task, 'node', 'unretire').stdout.lower()
    assert _run(task, 'node', 'status').stdout.strip() == 'idle'
    # a fresh launch with the stub agent on PATH completes the lifecycle
    # (start.sh propagates PATH into the tmux session)
    env = _cli_env()
    bindir = repo['bindir']
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    session = f'{root.name} (main-task)'
    try:
        started = subprocess.run(
            [_fractal_bin(), 'node', 'start'],
            cwd=task,
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )
        assert started.returncode == 0, started.stderr
        # let the one-iteration run settle so the trail below is complete
        assert _await_settled(task), _run(task, 'node', 'status').stdout
    finally:
        # `=` prefix forces an exact target match (no prefix resolution)
        subprocess.run(
            ['tmux', 'kill-session', '-t', f'={session}'],
            capture_output=True,
        )
    # the trail is durable: the user's DB holds the up message and budget
    parent_trail = _run(
        root,
        'db',
        '_query',
        "SELECT message_uuid FROM messages WHERE sender = 'main.task'",
        '--csv',
    )
    assert up_uuid in parent_trail.stdout
    assert '2.5' in _run(root, 'node', 'cost', 'breakdown').stdout
    # the child's retire/unretire/launch lifecycle lands as its events
    trail = _run(
        task,
        'db',
        '_query',
        "SELECT event FROM events WHERE node = 'main.task'",
        '--csv',
    )
    events = trail.stdout
    assert 'start' in events
    assert 'retire' in events
    assert 'unretire' in events


# ------ delegation (child nests under the delegating worker)


def test_delegation_nests_child_under_worker_and_radios_up(repo: dict) -> None:
    """A worker delegates a child, which nests under it and radios back up.

    A child delegated *for* ``main.task`` -- spawned by an agent running
    inside its own worktree with no ``--path`` -- nests as ``main.task.c1``
    (the caller is resolved from ``_NODE``). The child then radios
    up to its parent and the message is delivered to the worker's inbox.
    """
    root, task, task_dir = repo['root'], repo['task'], repo['task_dir']
    # worker delegates a child from inside its worktree; it nests under task
    # the child sets its own --max-cost (required when the parent carries one)
    spawned = _run_as_node(
        task,
        task_dir,
        'node',
        'init',
        'c1',
        '--agent',
        'claude',
        '--max-cost',
        '1',
    )
    assert spawned.returncode == 0, spawned.stderr
    child = root / '.worktrees' / 'main.task.c1'
    assert child.exists()
    # the child radios up to its parent and the message is delivered
    up = _run(
        child,
        'radio',
        'send',
        'child reporting',
        '--parent',
        '--channel',
        'inbox',
        '--subject',
        'c1up',
        '--priority',
        '4',
    )
    assert up.returncode == 0
    delivered = _run(
        task,
        'db',
        '_query',
        "SELECT sender FROM messages WHERE subject = 'c1up'",
        '--csv',
    )
    assert 'main.task.c1' in delivered.stdout


# ------ helpers


def _run_as_node(
    cwd: pathlib.Path,
    node_dir: pathlib.Path,
    *args: str,
) -> subprocess.CompletedProcess:
    """Run the CLI with ``_NODE`` set, as a node delegating to a child."""
    return _run(cwd, *args, _NODE=str(node_dir))
