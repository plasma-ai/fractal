"""Shared helpers for the ``fractal`` test suite."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import pathlib
import signal
import subprocess

import pytest

import fractal.util.process
from fractal.core.node import Node

__all__ = [
    '_git',
    '_files',
    '_digests',
    '_tree_drift',
    '_commit_template',
    '_past_timestamp',
    '_age_iter',
    '_age_run',
    '_age_step',
    '_stub_run_script',
    '_alive',
    '_kill_tagged',
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


def _files(root: pathlib.Path) -> dict[str, bytes]:
    """Every file under ``root``, keyed by its relative path, with its bytes."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob('*')
        if path.is_file()
    }


# the most paths of one kind the seed guard's message names
_DRIFT_CAP = 20


def _digests(root: pathlib.Path, folders: list[pathlib.Path]) -> dict[str, str]:
    """Map every file under ``folders``, by ``root``-relative path, to its sha256."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for folder in folders
        for path in folder.rglob('*')
        if path.is_file()
    }


def _tree_drift(before: dict[str, str], after: dict[str, str]) -> str:
    """Return the seed guard's failure message for two digest maps, ``''`` if equal.

    Names the added, removed, and changed paths (each kind capped at
    ``_DRIFT_CAP``, the rest counted), then both causes a drift can have --
    the guard cannot tell a test's write from a concurrent edit.
    """
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    changed = sorted(
        path for path in before.keys() & after.keys() if before[path] != after[path]
    )
    if not (added or removed or changed):
        return ''
    lines = [
        'package seed trees (_assets/_node/_scripts/skills) changed during the session:'
    ]
    # cap each kind, so a mass drift (a reformat, a checkout) stays readable
    for kind, paths in (('added', added), ('removed', removed), ('changed', changed)):
        lines.extend(f'  {kind}: {path}' for path in paths[:_DRIFT_CAP])
        if len(paths) > _DRIFT_CAP:
            lines.append(f'  ... and {len(paths) - _DRIFT_CAP} more {kind}')
    lines.append(
        'either a test wrote into the source tree through a package path, or'
        ' the tree was edited while the tests ran (never edit it mid-run)'
    )
    return '\n'.join(lines)


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


def _alive(pids: list[int]) -> list[int]:
    """Return the subset of ``pids`` still running (zombies count as dead)."""
    table = fractal.util.process.snapshot()
    return [pid for pid in pids if pid in table and not table[pid].zombie]


def _kill_tagged(tag: str) -> None:
    """SIGKILL every process whose command line carries ``tag``.

    One process-table pass, so a teardown ends every process its test
    started, recorded or not, while a pid recycled to an unrelated process
    -- whose command lacks the tag -- is never signaled.
    """
    for pid, process in fractal.util.process.snapshot().items():
        if tag in process.command:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
