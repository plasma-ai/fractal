"""Test the ``fractal.core.node`` module.

Node identity and status surface: the status trio, title updates, and
the launch confirmation surface.
"""

from __future__ import annotations

import pytest

from fractal.core.node import Node, tmux_session_name
from tests._helpers import _stub_run_script

__all__ = [
    'test_tmux_session_name_sanitizes_repo_and_branch_dots',
    'test_status_returns_stored_value',
    'test_status_set_validates',
    'test_status_display_decorates_exited_with_run_reason',
    'test_title_set_updates_config_and_registry',
    'test_start_returns_the_countermand_without_printing',
]


def test_tmux_session_name_sanitizes_repo_and_branch_dots() -> None:
    """The session name flattens dots in BOTH the repo name and the branch.

    tmux treats ``.`` specially in target syntax, so the six lifecycle
    shells build the session name as ``${REPO_NAME//./-} (${BRANCH//./-})``.
    This Python builder pins the same cross-file contract -- a mismatch
    makes ``_tmux_session_exists`` miss a live node's session, and the
    reconcile path then reaps the healthy loop and stamps it ``exited``.
    """
    assert tmux_session_name('/x/my.app', 'main.task') == 'my-app (main-task)'
    assert tmux_session_name('/x/plain', 'main') == 'plain (main)'
    # tmux also munges ':' in a session name -- the repo name flattens it too
    assert tmux_session_name('/x/a:b.c', 'main.t') == 'a-b-c (main-t)'


# ------ status


def test_status_returns_stored_value(node_with_db: Node) -> None:
    """Status returns the value stored in the .status file."""
    node = node_with_db
    # set status
    node.status_set('completed')
    # verify status reads it back
    assert node.status() == 'completed'


@pytest.mark.parametrize('invalid_status', ['running', 'suspended', 'unknown', ''])
def test_status_set_validates(
    node_with_db: Node,
    invalid_status: str,
) -> None:
    """Status set rejects invalid values."""
    with pytest.raises(ValueError):
        node_with_db.status_set(invalid_status)


def test_status_display_decorates_exited_with_run_reason(node_with_db: Node) -> None:
    """``status_display`` surfaces the latest run row's recorded end reason.

    A run that ended for a recorded reason (here a budget landing) reads
    ``exited (<reason>)`` -- display-only, the stored status stays bare --
    while a run that closed with no reason keeps the bare ``exited``.
    """
    node = node_with_db
    reason = 'cost budget reserve reached (spent $0.14 >= $0.15 max - $0.015 reserve)'
    # a budget landing: exited/0 with the boundary's reason on the run row
    run_id = node.record.run_start()
    node.record.run_end(run_id=run_id, status='exited', exit_code=0, metadata=reason)
    node.status_set('exited')
    assert node.status_display() == f'exited ({reason})'
    # display-only: the stored status stays bare
    assert node.status() == 'exited'
    # a reason-less close keeps the bare status
    bare_run = node.record.run_start()
    node.record.run_end(run_id=bare_run, status='exited', exit_code=1)
    node.status_set('exited')
    assert node.status_display() == 'exited'


def test_title_set_updates_config_and_registry(node_with_db: Node) -> None:
    """``title_set`` writes the config label and the registry row together."""
    node = node_with_db
    # an unregistered node (the user shape: no registry row) stores the label
    node.title_set('Pretty Root')
    assert node.config.get('title') == 'Pretty Root'
    assert node.db.read('nodes') == []
    # a registered node's row carries the update too, in sync with config
    node.db.merge({'node': node.branch, 'status': 'idle'}, 'nodes')
    node.title_set('Pretty Task')
    assert node.config.get('title') == 'Pretty Task'
    row = node.db.read('nodes', where={'node': node.branch})[0]
    assert row['title'] == 'Pretty Task'


def test_start_returns_the_countermand_without_printing(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Launch-time notices ride the returned confirmation, never stdout.

    A continue from ``killed`` surfaces the recorded kill attribution:
    core prepends it to the string the CLI echoes -- nothing prints from
    the core call itself (a bare print would corrupt piped output and
    leak into non-CLI callers).
    """
    node = node_with_db
    node.config.set('max_cost', 1.0)
    node.status_set('killed')
    # the recorded attribution the countermand reads (a completed kill event)
    event_id = node.record.event_start('kill', metadata='killed by operator: wedged')
    node.record.event_end(event_id=event_id, status='completed')
    _stub_run_script(monkeypatch, node, stdout='Started tmux session demo\n')
    result = node.start(continue_run=True)
    expected = 'Previous run killed by operator: wedged\nStarted tmux session demo'
    assert result == expected
    assert capsys.readouterr().out == ''
