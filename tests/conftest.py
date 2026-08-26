"""Top-level fixtures shared by the whole ``fractal`` suite."""

from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator

import pytest

import fractal

# the suite drives the installed fractal console script as a subprocess (and
# the loop shells out to it on every step), which coverage's in-process tracer
# cannot see; under --cov point both the parent and (via coverage's startup
# hook) every subprocess at one config + data file -- _cli_env and _run_script
# inherit os.environ -- so CLI lines are measured and combined, not read near-zero
if any(arg == '--cov' or arg.startswith('--cov=') for arg in sys.argv):
    _cov_root = pathlib.Path(__file__).resolve().parent.parent
    os.environ.setdefault('COVERAGE_PROCESS_START', str(_cov_root / 'pyproject.toml'))
    os.environ.setdefault('COVERAGE_FILE', str(_cov_root / '.coverage'))

# env vars a running loop exports each iteration -- tests must not inherit them,
# or in-process Node.init() adopts the live node as parent (see _isolate_loop_env)
# and Node.commit logs commit events under the live RUN_ID/ITER_ID/STEP_ID lineage
# missing from the test DB, silently losing them; ITER mislabels commit subjects;
# FRACTAL_HEADLESS overrides the backend a bare start reads from the node's record
_LOOP_ENV_VARS = [
    '_NODE',
    'NODE_DIR',
    'MAX_DEPTH',
    'MAX_CHILDREN',
    'MAX_COST',
    'RUN_ID',
    'ITER_ID',
    'STEP_ID',
    'ITER',
    'FRACTAL_HEADLESS',
]


@pytest.fixture(scope='session', autouse=True)
def _isolate_loop_env() -> Iterator[None]:
    """Strip the running loop's exported env for the whole session.

    A live node exports ``_NODE`` (plus the vars above). If the suite inherits
    them, ``Node.resolve_caller`` adopts the live node and in-process
    ``Node.init()`` calls -- including the session-scoped ``initialized_node``
    fixture -- spawn real stray nodes in the live DB. This is session-scoped and
    autouse in the top-level conftest, so it runs before every other fixture
    (``initialized_node`` included) and the suite resolves its own temp repos
    regardless of ambient env. Tests that need ``_NODE`` set it themselves with
    the function-scoped ``monkeypatch``, which runs later and is undone per test.
    Deleting from ``os.environ`` also gives ``test_cli`` subprocesses a clean env.
    """
    monkeypatch = pytest.MonkeyPatch()
    for var in _LOOP_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield
    monkeypatch.undo()


@pytest.fixture(scope='session', autouse=True)
def _package_seeds_stay_pristine() -> Iterator[None]:
    """Fail the session when a test mutates the package's seed trees.

    The loop resolves shared seed files (modes, steps, scripts) straight
    from the installed package, so a test that writes through such a path
    (e.g. via ``loop._sync_file``) corrupts the working tree for every
    later test and every live run. Digest the seed trees before and after
    the session and fail loudly on drift, naming the class of mistake.
    """
    package = pathlib.Path(fractal.__file__).parent
    seeds = ('_assets', '_node', '_scripts', 'skills')

    def digest() -> str:
        sha = hashlib.sha256()
        for seed in seeds:
            for path in sorted((package / seed).rglob('*')):
                if path.is_file():
                    sha.update(str(path.relative_to(package)).encode())
                    sha.update(path.read_bytes())
        return sha.hexdigest()

    before = digest()
    yield
    assert digest() == before, (
        'package seed trees (_assets/_node/_scripts/skills) were modified by'
        ' the test suite -- a test wrote through a package path'
    )


@pytest.fixture(scope='session', autouse=True)
def _isolate_tmux_server() -> Iterator[None]:
    """Run the suite's tmux on a private socket, never the operator's server.

    tmux copies the launching environment into its server only at start, so a
    shared default socket lets a keyed host's global env leak into a test (and
    an assertion diff print a live API key) and creates/kills node sessions on
    the user's real server. A session-scoped ``TMUX_TMPDIR`` gives the suite
    its own socket, torn down at the end; the path stays short because a long
    socket trips the ``sun_path`` limit (104 bytes on macOS).
    """
    if shutil.which('tmux') is None:
        yield
        return
    # a short base so the socket path clears the sun_path limit; mkdtemp still
    # randomizes the leaf, and the dir is torn down below
    base = '/tmp' if os.path.isdir('/tmp') else None  # noqa: S108
    tmpdir = tempfile.mkdtemp(prefix='fx-tmux-', dir=base)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv('TMUX_TMPDIR', tmpdir)
    # tmux resolves its socket from $TMUX before TMUX_TMPDIR, so a suite run
    # from inside a tmux pane (a fractal node testing itself) would otherwise
    # target -- and the teardown kill -- the operator's real server
    monkeypatch.delenv('TMUX', raising=False)
    try:
        yield
    finally:
        subprocess.run(['tmux', 'kill-server'], capture_output=True)
        monkeypatch.undo()
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _restore_loop_env() -> Iterator[None]:
    """Restore the loop's exported env around every test.

    An in-process ``Loop`` run exports ``_NODE`` into its own process env
    so child launches inherit it -- correct in the dedicated ``node _loop``
    process, which exits when the run ends, but in the suite the export
    outlives the test: every later test on the same worker then resolves
    the fixture node via ``resolve_caller`` and attributes its events to
    it. Snapshot the loop vars before each test and restore them after,
    so no test inherits another's loop.
    """
    saved = {var: os.environ.get(var) for var in _LOOP_ENV_VARS}
    yield
    for var, value in saved.items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value


@pytest.fixture(scope='session', autouse=True)
def _venv_bin_on_path() -> Iterator[None]:
    """Put this interpreter's bin dir first on ``PATH`` for the whole session.

    In-process ``Node.init()`` (``test_core``/``test_tui``) spawns the node
    lifecycle shell scripts, which invoke ``fractal``/``wiki`` by bare name; those
    must resolve to this venv's console scripts. Running the suite via
    ``.venv/bin/python -m pytest`` without activating leaves the venv bin off
    ``PATH``, so a bare ``fractal`` falls through to a pyenv shim that lacks it.
    Prepending the interpreter's own bin dir makes an unactivated run behave like
    an activated one. (``test_cli`` builds its own subprocess env in ``_cli_env``;
    this fixes the in-process path the same way for ``Node.init()`` callers.)
    """
    bin_dir = str(pathlib.Path(sys.executable).parent)
    monkeypatch = pytest.MonkeyPatch()
    path = os.environ.get('PATH', '')
    monkeypatch.setenv('PATH', f'{bin_dir}{os.pathsep}{path}')
    yield
    monkeypatch.undo()


@pytest.fixture(scope='session', autouse=True)
def _offline_wiki() -> Iterator[None]:
    """Skip the Obsidian plugin download when seeding node memory wikis.

    Every ``Node.init`` seeds a memory wiki via ``wiki init``, which otherwise
    fetches the Front Matter Title plugin from GitHub on each call -- across the
    integration suite that is dozens of live downloads (slow and network-flaky).
    Setting ``OFFLINE_MODE`` for the whole session makes ``wiki`` install the
    bundled settings without the network fetch; both in-process ``Node.init``
    (via ``_run_script``'s inherited env) and ``test_cli`` subprocesses (via
    ``_cli_env``) pick it up from ``os.environ``.
    """
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv('OFFLINE_MODE', 'true')
    yield
    monkeypatch.undo()
