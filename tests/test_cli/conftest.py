"""Shared helpers for the ``fractal`` CLI subprocess tests.

The CLI suite drives the real ``fractal`` console script as a subprocess. These
helpers resolve the script and a hermetic environment **lazily** (never at import
time) and skip a test when the script is unavailable, so collection never shells
out. Test modules pull them in with ``from .conftest import _run`` -- the same
shape ``test_core`` uses for its repo helpers.
"""

from __future__ import annotations

import functools
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Optional

import pytest

# ------ helpers


@functools.cache
def _fractal_bin() -> str:
    """Resolve the ``fractal`` console script, skipping the test if absent.

    Prefers the script beside the active interpreter (the venv's) so the
    subprocess runs the same install the suite imports, falling back to ``PATH``
    (and a pyenv shim). Skips rather than returns ``None`` so every call site --
    including module-scoped fixtures -- is guarded by a single choke point.
    """
    candidate = pathlib.Path(sys.executable).parent / 'fractal'
    found = str(candidate) if candidate.exists() else shutil.which('fractal')
    if found is None:
        pytest.skip('fractal console script not on PATH')
    return found


def _require_tmux() -> None:
    """Skip the test when tmux is unavailable (live-session behaviors need it)."""
    if shutil.which('tmux') is None:
        pytest.skip('tmux unavailable')


def _worktree_root() -> pathlib.Path:
    """Repo root holding these tests (and the edited scripts/CLI under test)."""
    return pathlib.Path(__file__).resolve().parents[2]


# the hermetic session HOME for the CLI subprocesses -- allocated by the
# autouse _cli_home fixture below and read by _cli_env
_CLI_HOME = ''


@pytest.fixture(scope='session', autouse=True)
def _cli_home(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Allocate the session-scoped throwaway ``HOME`` for the CLI subprocesses.

    A hermetic ``HOME`` keeps every test's ``~``-anchored reads (agent
    config, caches, installed skills) independent of the operator's real
    home; pytest owns the cleanup and parallel-safe allocation. Autouse and
    threaded to ``_cli_env`` through the module global, so plain-function
    callers (``_run`` and the launch helpers) never ride the fixture graph.
    Tests that need a specific ``HOME`` overlay their own via ``extra``.
    """
    global _CLI_HOME
    _CLI_HOME = str(tmp_path_factory.mktemp('cli_home'))


def _cli_env(**extra: str) -> dict:
    """Subprocess env that resolves ``fractal`` to this worktree.

    The site-packages install is a frozen copy, so ``PYTHONPATH`` puts this
    worktree first (the console script and the node scripts that shell out to it
    import the edited package, not stale code) and the script's bin dir goes on
    ``PATH`` (node lifecycle scripts invoke ``fractal`` by name). Color-forcing
    vars are dropped so typer renders plain output on the captured pipes in CI
    exactly as it does locally, and ``HOME`` points at a session-scoped
    throwaway so ``~``-anchored reads never touch the operator's real home.
    ``extra`` overlays caller-specific vars (e.g.
    ``_NODE`` or a stub ``CAPTURE_DIR``); ``_NODE`` is already stripped from the
    base env by ``_isolate_loop_env``.
    """
    env = dict(os.environ)
    # drop color-forcing vars: typer force-enables ANSI when any is set (e.g.
    # GITHUB_ACTIONS in CI), and the escapes it injects inside option names
    # break plain-substring assertions on captured output
    for var in ('GITHUB_ACTIONS', 'FORCE_COLOR', 'PY_COLORS'):
        env.pop(var, None)
    # a hermetic HOME: ~-anchored reads (agent config, caches, installed
    # skills) must never see the operator's real home
    env['HOME'] = _CLI_HOME
    env['PYTHONPATH'] = os.pathsep.join(
        part for part in (str(_worktree_root()), env.get('PYTHONPATH', '')) if part
    )
    bin_dir = pathlib.Path(_fractal_bin()).resolve().parent
    path = env['PATH']
    env['PATH'] = f'{bin_dir}{os.pathsep}{path}'
    env.update(extra)
    return env


def _run(
    cwd: pathlib.Path,
    *args: str,
    stdin: Optional[str] = None,
    **env: str,
) -> subprocess.CompletedProcess:
    """Run the ``fractal`` CLI in ``cwd`` and capture output."""
    return subprocess.run(
        [_fractal_bin(), *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        env=_cli_env(**env),
        timeout=180,
    )


# the loop machinery runs from the package, not a per-node copy -- invoke the
# console script's hidden loop entry directly (the PYTHONPATH in _cli_env pins
# it to this worktree's package)
def _loop_cmd(worktree: pathlib.Path, *flags: str) -> list[str]:
    """The ``fractal node _loop`` launch argv for a worktree."""
    return [_fractal_bin(), 'node', '_loop', f'--path={worktree}', *flags]


def _reap_group(proc: subprocess.Popen) -> None:
    """SIGKILL ``proc``'s whole process group and reap the direct child.

    Agent-loop launches use ``start_new_session=True``, so the group id is the
    launch's own pid and spans the loop's child chain
    that a pid-only ``proc.kill()`` would leave reparented and alive past the
    pytest session. The agent invocation itself runs in its *own* group
    (recorded to ``.step_pgid`` for pause/kill), so the group kill alone would
    orphan an in-flight stub -- sweep the surviving descendants too, the
    harness twin of ``kill.sh``'s step-group reap. Safe for teardowns to call
    unconditionally: a clean exit's already-dead chain is a no-op.
    """
    descendants = _descendant_pids(proc.pid)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    for pid in descendants:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    # drain and reap unless a completed communicate()/wait() already did (its
    # streams are closed then, and a second communicate() would blow up)
    if proc.returncode is None:
        proc.communicate()


def _descendant_pids(pid: int) -> list[int]:
    """The transitive descendants of ``pid``, from one ``ps`` snapshot."""
    result = subprocess.run(
        ['ps', '-axo', 'pid=,ppid='],
        capture_output=True,
        text=True,
        check=False,
    )
    out = result.stdout
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        child, parent = line.split()
        children.setdefault(int(parent), []).append(int(child))
    chain: list[int] = []
    frontier = [pid]
    while frontier:
        kids = [k for p in frontier for k in children.get(p, [])]
        chain.extend(kids)
        frontier = kids
    return chain


def _run_reaped(
    cmd: list[str],
    *,
    cwd: str,
    env: dict,
    timeout: float,
) -> subprocess.CompletedProcess:
    """``subprocess.run`` for agent-loop launches, group-reaped at teardown.

    A plain ``run(timeout=)`` expiry kills only the direct ``bash`` child and
    leaks the rest of the chain (``TimeoutExpired`` still propagates here).
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    finally:
        _reap_group(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _await_progress(
    check: Callable[[], bool],
    progress: Callable[[], object],
    *,
    deadline: float,
) -> bool:
    """Block until ``check`` passes, with an idle-based deadline.

    Whenever ``progress`` returns a new value the full allowance (the original
    ``deadline`` minus the start time) is re-armed, so a loaded host that slows
    the observed pipeline stretches the wait instead of failing it, while a
    genuinely wedged pipeline (no observable progress at all) still trips the
    deadline. Returns whether ``check`` passed.
    """
    allowance = deadline - time.monotonic()
    seen: object = object()
    while time.monotonic() < deadline:
        if check():
            return True
        current = progress()
        if current != seen:
            seen = current
            deadline = time.monotonic() + allowance
        time.sleep(0.05)
    return False


def _await_settled(worktree: pathlib.Path, *, deadline_seconds: float = 120) -> bool:
    """Block until a launched loop lands ``node status`` on a terminal status.

    Idle-based via ``_await_progress``: any status transition refreshes the
    allowance. Returns whether the node settled.
    """
    # the status may carry a parenthesized qualifier (an end reason, a
    # run-exhausted note) -- settle on the bare word before it
    settled = ('completed', 'stopped', 'exited', 'killed')
    return _await_progress(
        check=lambda: (
            _run(worktree, 'node', 'status').stdout.strip().split(' (')[0] in settled
        ),
        progress=lambda: _run(worktree, 'node', 'status').stdout.strip(),
        deadline=time.monotonic() + deadline_seconds,
    )
