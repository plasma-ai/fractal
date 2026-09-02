"""Shared helpers for the ``fractal`` test suite."""

from __future__ import annotations

import datetime as dt
import pathlib
import subprocess

import pytest

from fractal.core.node import Node

__all__ = [
    '_git',
    '_commit_template',
    '_past_timestamp',
    '_age_iter',
    '_age_run',
    '_age_step',
    '_stub_run_script',
]


def _git(cwd: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command in ``cwd``, capturing output and raising on failure."""
    return subprocess.run(
        ['git', *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def _commit_template(
    repo: pathlib.Path,
    path: str,
    files: dict[str, str],
) -> str:
    """Write ``files`` under ``repo/<path>``, commit the folder, return the sha.

    Template bytes deploy from git, never from the working copy, so every
    template fixture must be committed before the init that reads it.
    """
    for name, content in files.items():
        target = repo / path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
    _git(repo, 'add', path)
    _git(repo, 'commit', '-m', f'template {path}')
    return _git(repo, 'rev-parse', 'HEAD').stdout.strip()


def _past_timestamp(seconds_ago: float) -> str:
    """ISO 8601 millisecond timestamp ``seconds_ago`` in the past.

    Matches the ``created_at`` format produced by the SQL defaults and
    ``utc_now`` so ``elapsed`` parses it.
    """
    moment = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=seconds_ago)
    return moment.strftime('%Y-%m-%dT%H:%M:%S.') + f'{moment.microsecond // 1000:03d}Z'


def _age_iter(node: Node, iter_id: int, seconds_ago: float) -> None:
    """Back-date an iteration's ``started_at`` to simulate elapsed time."""
    node.db.update(
        data={'started_at': _past_timestamp(seconds_ago)},
        table='iters',
        where={'iter_id': iter_id},
    )


def _age_run(node: Node, run_id: int, seconds_ago: float) -> None:
    """Back-date a run's ``started_at`` to simulate elapsed time."""
    node.db.update(
        data={'started_at': _past_timestamp(seconds_ago)},
        table='runs',
        where={'run_id': run_id},
    )


def _age_step(node: Node, step_id: int, seconds_ago: float) -> None:
    """Back-date a step's ``started_at`` to simulate elapsed time."""
    node.db.update(
        data={'started_at': _past_timestamp(seconds_ago)},
        table='steps',
        where={'step_id': step_id},
    )


def _stub_run_script(
    monkeypatch: pytest.MonkeyPatch,
    target: Node | type[Node],
    *,
    stdout: str = '',
) -> list[tuple[str, ...]]:
    """Stub ``_run_script`` on ``target``, recording calls instead of running.

    Papers over the lifecycle shell scripts, which drive tmux sessions and
    shell back into the CLI -- neither exists in the test environment. The
    stub swallows the call and returns a clean zero-exit result carrying
    ``stdout``. ``target`` is a ``Node`` instance (one node stubbed) or the
    ``Node`` class (every node stubbed, for fan-out verbs). Callers assert
    on the returned list of recorded ``(script, *args)`` invocations.
    """
    calls: list[tuple[str, ...]] = []

    def run_script(script: str, *args: str) -> subprocess.CompletedProcess[str]:
        calls.append((script, *args))
        return subprocess.CompletedProcess([script, *args], 0, stdout, '')

    def run_script_method(
        node: Node,
        script: str,
        *args: str,
    ) -> subprocess.CompletedProcess[str]:
        return run_script(script, *args)

    stub = run_script_method if isinstance(target, type) else run_script
    monkeypatch.setattr(target, '_run_script', stub)
    return calls
