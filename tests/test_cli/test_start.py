"""The node launch surface: ``fractal node start`` and ``_scripts/start.sh``.

Two tiers share this module. The script tier drives ``start.sh`` directly
via ``bash`` -- its directory resolution (``SCRIPT_DIR`` and the derived
``PACKAGE_DIR``) runs before it validates arguments or launches tmux, and
is otherwise unreachable without a real tmux session. The CLI tier drives
the real ``fractal node start`` against a throwaway repo with a stubbed
agent, pinning the ``--continue`` dirty-tree guard -- a continue's worktree
restore discards uncommitted project files, so the launch refuses them
without ``--clean`` while node-dir dirt (which the restore commits and
preserves) never trips it -- the re-validation of a hand-edited
``config.json`` before any launch, the continue-from-killed countermand, and
the start-event lineage every successful launch records.
"""

from __future__ import annotations

import csv
import io
import os
import pathlib
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from fractal.core.node import Node
from tests._helpers import _git

from .conftest import (
    _await_progress,
    _await_settled,
    _cli_env,
    _fractal_bin,
    _require_tmux,
    _run,
    _worktree_root,
)

__all__ = [
    'test_start_resolves_dirs_before_arg_check',
    'test_headless_start_runs_without_tmux',
    'test_headless_continue_forwards_drain',
    'test_continue_only_flags_reject_a_bare_start',
    'test_start_revalidates_hand_edited_config',
    'test_continue_refuses_to_discard_dirty_project_files',
    'test_continue_refusal_does_not_persist_the_max_cost_retune',
    'test_continue_names_the_iter_cap_floor_a_retune_sits_under',
    'test_continue_clean_discards_and_proceeds',
    'test_continue_allows_node_dir_only_dirt',
    'test_continue_from_killed_prints_the_countermand',
    'test_launches_record_the_start_event_chain',
    'test_start_forwards_provider_keys_into_the_session',
    'test_start_gates_key_forwarding_on_an_existing_server',
]

# minimal claude stand-in: emits the init + result frames the stream driver
# expects and exits 0, so a launched loop completes an iteration hermetically
_AGENT_STUB = """#!/usr/bin/env bash
SID=$(uuidgen | tr '[:upper:]' '[:lower:]')
printf '{"type":"system","subtype":"init","session_id":"%s"}\\n' "$SID"
printf '{"type":"result","session_id":"%s","total_cost_usd":0.001,"num_turns":1,"duration_ms":1}\\n' "$SID"
"""


@pytest.fixture(scope='module')
def repo(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Return a repo with a user node and a private bindir holding the agent stub.

    Built once; individual tests init their own uniquely-named workers. The
    run-unique dirname keeps the tmux session names (which embed it
    machine-wide) from colliding with sibling suite runs or stale sessions.
    """
    root = tmp_path_factory.mktemp(f'start_{uuid.uuid4().hex[:8]}')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'start@test.local')
    _git(root, 'config', 'user.name', 'start')
    (root / 'README.md').write_text('# start\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    assert _run(root, 'init').returncode == 0
    bindir = root / 'bin'
    bindir.mkdir()
    agent = bindir / 'claude'
    agent.write_text(_AGENT_STUB, encoding='utf-8')
    agent.chmod(0o755)
    return {'root': root, 'bindir': bindir}


@pytest.fixture(autouse=True)
def _kill_repo_sessions(repo: dict[str, Any]) -> Iterator[None]:
    """Reap the exact tmux sessions a test started, on teardown."""
    repo.setdefault('sessions', [])
    yield
    sessions = repo['sessions']
    repo['sessions'] = []
    if shutil.which('tmux') is None:
        return
    for session in sessions:
        # `=` prefix forces an exact target match (no prefix resolution)
        subprocess.run(
            ['tmux', 'kill-session', '-t', f'={session}'],
            capture_output=True,
        )


def test_start_resolves_dirs_before_arg_check() -> None:
    """``start.sh`` resolves ``SCRIPT_DIR``/``PACKAGE_DIR`` before the arg check.

    ``PACKAGE_DIR`` is derived from ``SCRIPT_DIR``, so an ordering slip would
    crash under ``set -u`` (unbound variable) before the loop could launch. Run
    with no args, the script must reach its own ``path is required`` guard.
    """
    start = _worktree_root() / 'fractal' / '_scripts' / 'start.sh'
    result = subprocess.run(['bash', str(start)], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'path is required' in result.stderr
    assert 'unbound variable' not in result.stderr


def test_headless_start_runs_without_tmux(repo: dict) -> None:
    """``start --headless`` detaches, captures output, and settles normally.

    The launch returns while the loop owns an independent process group, so
    the caller remains free to orchestrate more nodes. The loop uses the same
    status and run machinery as tmux and removes its PGID record on an in-band
    exit; its transcript remains available in the node-local headless log.
    """
    worktree = _settled_node(repo, 'headless')
    Node(worktree).status_set('idle')
    result = _start(repo, worktree, '--headless')
    assert result.returncode == 0, result.stderr
    assert 'Started headless node:' in result.stdout
    assert _await_settled(worktree), result.stdout
    node_dir = worktree / '.fractal' / 'main.headless'
    assert (node_dir / '.headless').read_text(encoding='utf-8') == 'headless\n'
    assert not (node_dir / '.pgid').exists()
    assert (node_dir / 'headless.log').read_text(encoding='utf-8')

    # a delegated start receives the backend through the loop environment;
    # pin the CLI's envvar route independently of the explicit flag above
    inherited = _settled_node(repo, 'headlessenv')
    Node(inherited).status_set('idle')
    env = _cli_env()
    bindir = repo['bindir']
    env['PATH'] = f'{bindir}{os.pathsep}{env["PATH"]}'
    env['FRACTAL_HEADLESS'] = 'true'
    inherited_result = subprocess.run(
        [_fractal_bin(), 'node', 'start'],
        cwd=inherited,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    assert inherited_result.returncode == 0, inherited_result.stderr
    assert 'Started headless node:' in inherited_result.stdout
    assert _await_settled(inherited), inherited_result.stdout
    inherited_dir = inherited / '.fractal' / 'main.headlessenv'
    assert (inherited_dir / '.headless').is_file()


def test_headless_continue_forwards_drain(repo: dict) -> None:
    """``--drain`` rides a headless continue through to the detached loop.

    The drain flag travels the same handoff as the backend flag: ``start.sh``
    forwards it into the detached launch, so a drain run boots headless and
    settles on a terminal status like any other continue.
    """
    worktree = _settled_node(repo, 'headlessdrain')
    result = _start_continue(repo, worktree, '--drain', '--headless')
    assert result.returncode == 0, result.stderr
    assert 'Started headless node:' in result.stdout
    assert _await_settled(worktree), result.stdout


@pytest.mark.parametrize('flag', ['--clean', '--max-cost=0.5'])
def test_continue_only_flags_reject_a_bare_start(
    tmp_path: pathlib.Path,
    flag: str,
) -> None:
    """``--clean`` and ``--max-cost`` without ``--continue`` are rejected.

    Both flags only act on the continue path, so a bare use would be
    silently ignored; the CLI rejects it up front (``BadParameter``, exit 2)
    before resolving the target.
    """
    result = _run(tmp_path, 'node', 'start', flag)
    assert result.returncode == 2
    assert 'requires --continue' in result.stderr


@pytest.mark.parametrize(
    argnames=('name', 'bad_config', 'expected'),
    argvalues=[
        ('baddur', {'sleep': '10'}, 'duration with a unit suffix'),
        ('badcost', {'max_iter_cost': 999.0}, 'exceeds max_cost'),
        ('capless', {'max_cost': None, 'max_iter_cost': 2.0}, 'requires max_cost'),
        ('badagent', {'agent': 'notreal'}, 'Unsupported agent'),
    ],
    ids=[
        'bare_number_duration',
        'broken_cost_ordering',
        'iter_cap_without_ceiling',
        'unsupported_agent',
    ],
)
def test_start_revalidates_hand_edited_config(
    repo: dict,
    name: str,
    bad_config: dict,
    expected: str,
) -> None:
    """``start`` re-validates a hand-edited ``config.json`` and fails loudly.

    The documented steering path is to edit ``config.json`` directly, which
    bypasses the init/update setters' checks. A bad duration (no unit suffix)
    would otherwise abort the loop after ``start`` already
    printed success, wedging the node idle; a broken cost ordering would launch
    a degenerate budget; a per-iter cap whose ceiling was edited away would
    spend unbounded once the iteration's budget drains; an agent the registry
    does not know would kill the loop at boot the same invisible way.
    ``start`` must reject each before launching -- exit non-zero with a clear
    message and no "Started" output.
    """
    root = repo['root']
    # a throwaway worker (one per case); init it with a valid budget so start
    # clears the max_cost guard and reaches the config re-validation (a case
    # may null it back out -- the hand-edit that strips the ceiling)
    assert _run(root, 'node', 'init', name, '--agent', 'claude').returncode == 0
    worktree = root / '.worktrees' / f'main.{name}'
    node = Node(worktree)
    for key, value in {'max_cost': 5.0, **bad_config}.items():
        node.config.set(key, value)
    # start refuses the hand-edited config before any tmux launch
    result = _run(worktree, 'node', 'start')
    assert result.returncode != 0
    assert expected in result.stderr
    assert 'Started' not in result.stdout
    # the node never launched -- it stays idle, not wedged active
    assert _run(worktree, 'node', 'status').stdout.strip() == 'idle'


# ------ the --continue dirty-tree guard


def test_continue_refuses_to_discard_dirty_project_files(repo: dict) -> None:
    """A continue over dirty project files refuses, naming the doomed paths.

    The continue boot's worktree restore would discard them, so the launch
    must not proceed silently: it exits non-zero, lists the modified and the
    untracked file, and points at ``--clean``. The refusal fires before the
    idle re-arm, so the node stays settled and a retry hits the same guard --
    never a status wedge.
    """
    worktree = _settled_node(repo, 'dirtyref')
    (worktree / 'README.md').write_text('# edited\n', encoding='utf-8')
    (worktree / 'notes.txt').write_text('uncommitted work\n', encoding='utf-8')
    result = _start_continue(repo, worktree)
    assert result.returncode != 0, result.stdout
    assert 'README.md' in result.stderr, result.stderr
    assert 'notes.txt' in result.stderr, result.stderr
    assert '--clean' in result.stderr, result.stderr
    # the node stays settled and re-continuable
    assert _run(worktree, 'node', 'status').stdout.strip() == 'exited'
    again = _start_continue(repo, worktree)
    assert again.returncode != 0, again.stdout
    assert 'notes.txt' in again.stderr, again.stderr
    # a refused launch leaves no phantom start event on the lineage
    assert _start_events(worktree) == []


def test_continue_refusal_does_not_persist_the_max_cost_retune(repo: dict) -> None:
    """A refused dirty-tree continue must not leave a ``--max-cost`` retune.

    The worktree-restore guard runs before the retune, so a
    ``start --continue --max-cost=N`` blocked by uncommitted files persists
    nothing: the child's cap is unchanged, and a plain retry is never silently
    governed by a cap the caller never got to launch under.
    """
    worktree = _settled_node(repo, 'o2retune')
    before = _run(worktree, 'config', '_get', 'max_cost').stdout.strip()
    (worktree / 'notes.txt').write_text('uncommitted work\n', encoding='utf-8')
    result = _start_continue(repo, worktree, '--max-cost=0.5')
    assert result.returncode != 0, result.stdout
    assert 'notes.txt' in result.stderr, result.stderr
    # the refusal fired before the retune -- the child's cap is untouched
    after = _run(worktree, 'config', '_get', 'max_cost').stdout.strip()
    assert after == before


def test_continue_names_the_iter_cap_floor_a_retune_sits_under(repo: dict) -> None:
    """A ``--max-cost`` under the node's per-iteration cap names the floor.

    The generic ordering rule is right in general but wrong here as an
    explanation: at the retune site the operator asked for a run cap and
    would get an error about ``max_iter_cost``, a config field they never
    passed and whose value the message never states -- leaving them to
    discover both the floor and the way past it. The continue's own refusal
    names the floor and both remedies, and persists nothing.
    """
    worktree = _settled_node(repo, 'capfloor')
    node = Node(worktree)
    node.config.set('max_cost', 50.0)
    node.config.set('max_iter_cost', 40.0)

    result = _start_continue(repo, worktree, '--max-cost=30')

    assert result.returncode != 0, result.stdout
    # the floor, the field it comes from, and both ways past it
    assert '$40.00' in result.stderr, result.stderr
    assert 'max_iter_cost' in result.stderr, result.stderr
    assert '--max-cost >= $40.00' in result.stderr, result.stderr
    assert 'Started' not in result.stdout
    # refused before the retune -- the node keeps its cap and stays settled
    assert _run(worktree, 'config', '_get', 'max_cost').stdout.strip() == '50.0'
    assert _run(worktree, 'node', 'status').stdout.strip() == 'exited'


def test_continue_clean_discards_and_proceeds(repo: dict) -> None:
    """``--continue --clean`` acknowledges the wipe: the launch proceeds.

    With the flag the guard stands down, the loop boots, and its worktree
    restore actually discards the dirty project file -- the run then completes
    on the stubbed agent.
    """
    _require_tmux()
    worktree = _settled_node(repo, 'cleanrun')
    doomed = worktree / 'doomed.txt'
    doomed.write_text('expendable\n', encoding='utf-8')
    result = _start_continue(repo, worktree, '--clean')
    assert result.returncode == 0, result.stderr
    assert 'Started tmux session' in result.stdout, result.stdout
    assert _await_settled(worktree), result.stdout
    assert not doomed.exists()


def test_continue_allows_node_dir_only_dirt(repo: dict) -> None:
    """Node-dir-only dirt continues without ``--clean``.

    The restore commits node-dir edits (the documented between-runs steering
    flow) rather than discarding them, so they are exempt from the guard: a
    modified ``NODE.md`` and an untracked node-dir file launch cleanly. The
    untracked name carries a space -- git C-quotes such paths in line-oriented
    status output, so this pins that the exemption sees the verbatim name.
    """
    _require_tmux()
    worktree = _settled_node(repo, 'nodedirt')
    node_dir = worktree / '.fractal' / 'main.nodedirt'
    node_md = node_dir / 'NODE.md'
    original = node_md.read_text(encoding='utf-8')
    node_md.write_text(original + '\nSteering edit.\n', encoding='utf-8')
    (node_dir / 'scratch note.txt').write_text('operator note\n', encoding='utf-8')
    result = _start_continue(repo, worktree)
    assert result.returncode == 0, result.stderr
    assert 'Started tmux session' in result.stdout, result.stdout
    assert _await_settled(worktree), result.stdout
    # the steering edit survived the restore
    assert 'Steering edit.' in node_md.read_text(encoding='utf-8')


# ------ the continue-from-killed countermand


def test_continue_from_killed_prints_the_countermand(repo: dict) -> None:
    """A continue from ``killed`` prints who ended the previous run, and why.

    The re-arm countermands another actor's explicit kill, so the launch
    surfaces the recorded attribution first. The countermand reads the
    latest *completed* kill event -- a trailing refused kill (a failed
    event row) must not shadow the real one.
    """
    _require_tmux()
    worktree = _settled_node(repo, 'countermand')
    # a real kill: the node active with an open run, ended with a reason
    node = Node(worktree)
    node.status_set('active')
    node.record.run_start()
    killed = _run(worktree, 'node', 'kill', '--reason', 'wedged')
    assert killed.returncode == 0, killed.stderr
    # a trailing refused kill lands failed on the event stream
    refused = _run(worktree, 'node', 'kill')
    assert refused.returncode == 2
    result = _start_continue(repo, worktree)
    assert result.returncode == 0, result.stderr
    assert 'Previous run killed by operator: wedged' in result.stdout
    assert 'refused' not in result.stdout
    assert _await_settled(worktree), result.stdout


# ------ the start-event lineage


def test_launches_record_the_start_event_chain(repo: dict) -> None:
    """Each successful launch logs a completed ``start`` event, in order.

    The restart chain reads straight off the event log: a fresh launch
    records a bare completed ``start`` event and a follow-up ``--continue``
    records another carrying ``continue`` metadata. Refused launches record
    nothing (pinned by the dirty-tree guard test), so the chain lists only
    the launches that actually happened.
    """
    _require_tmux()
    worktree = _settled_node(repo, 'lineage')
    # a fresh launch starts from the never-started status
    Node(worktree).status_set('idle')
    fresh = _start(repo, worktree)
    assert fresh.returncode == 0, fresh.stderr
    assert _await_settled(worktree), fresh.stdout
    # the loop stamps its terminal status just before the session closes --
    # wait for the reap so the relaunch never races the one-loop guard
    assert _await_session_gone(repo, worktree)
    continued = _start_continue(repo, worktree)
    assert continued.returncode == 0, continued.stderr
    assert _await_settled(worktree), continued.stdout
    assert _start_events(worktree) == ['', 'continue']


def test_start_forwards_provider_keys_into_the_session(repo: dict) -> None:
    """Provider keys reach the loop through a pre-existing tmux server.

    tmux copies the launching environment into its server only at server
    start, so a key exported afterwards reaches the loop only through the
    launch's session environment -- the agent process must read the live
    value even when the server predates the export.
    """
    _require_tmux()
    # a pre-existing server whose environment lacks the key: without the
    # session-environment forwarding, the launch below would inherit this
    # (keyless) server environment and strand the export
    keyless = {k: v for k, v in _cli_env().items() if k != 'OPENROUTER_API_KEY'}
    root_name = repo['root'].name
    scratch = f'keyless-{root_name}'
    subprocess.run(
        ['tmux', 'new-session', '-d', '-s', scratch, 'sleep 30'],
        check=True,
        env=keyless,
    )
    repo.setdefault('sessions', []).append(scratch)
    # a worker whose stub dumps the key it sees into its worktree
    worktree = _settled_node(repo, 'keyed')
    Node(worktree).status_set('idle')
    keybin = repo['root'] / 'keybin'
    keybin.mkdir(exist_ok=True)
    dump = keybin / 'claude'
    dump.write_text(
        '#!/usr/bin/env bash\n'
        'printf \'%s\' "${OPENROUTER_API_KEY:-}" > key_seen.txt\n'
        + _AGENT_STUB.split('\n', 1)[1],
        encoding='utf-8',
    )
    dump.chmod(0o755)
    env = _cli_env()
    bindir = repo['bindir']
    path = env['PATH']
    env['PATH'] = f'{keybin}{os.pathsep}{bindir}{os.pathsep}{path}'
    env['OPENROUTER_API_KEY'] = 'sk-or-test-sentinel'
    root_name = repo['root'].name
    worktree_name = worktree.name.replace('.', '-')
    session = f'{root_name} ({worktree_name})'
    repo.setdefault('sessions', []).append(session)
    launch = subprocess.run(
        [_fractal_bin(), 'node', 'start'],
        cwd=worktree,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    assert launch.returncode == 0, launch.stderr
    assert _await_settled(worktree), launch.stdout
    # the agent saw the live key value, not the keyless server environment
    assert (worktree / 'key_seen.txt').read_text() == 'sk-or-test-sentinel'


def test_start_gates_key_forwarding_on_an_existing_server(
    tmp_path: pathlib.Path,
) -> None:
    """``start.sh`` passes ``-e KEY=VALUE`` to tmux only when a server runs.

    A cold ``new-session`` becomes the persistent server and keeps any ``-e``
    value in its ps(1) argv for the server's whole life; a warm one is a
    transient client where the brief window is acceptable. The key is exported
    in both runs, so the server-exists gate -- not the key's presence -- must
    decide whether the value reaches the command line. ``new-session -e`` also
    needs tmux >= 3.2, so an older version suppresses the forwarding outright.
    A ``tmux`` stub records the ``new-session`` argv and its ``list-sessions``
    exit stands in for the two server states.
    """
    root = tmp_path / 'repo'
    (root / '.fractal' / 'main').mkdir(parents=True)
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'start@test.local')
    _git(root, 'config', 'user.name', 'start')
    (root / 'README.md').write_text('# gate\n', encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    # a tmux stand-in: it logs the new-session argv, reports server
    # presence via list-sessions' exit (the bare form is the gate; the -F
    # form is start.sh's own duplicate-name check, kept benignly empty),
    # and answers -V with a configurable version
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    argv_log = tmp_path / 'new_session.argv'
    stub = bindir / 'tmux'
    stub.write_text(
        '#!/usr/bin/env bash\n'
        'case "$1" in\n'
        '  -V) echo "tmux $STUB_TMUX_VERSION" ;;\n'
        '  list-sessions)\n'
        '    [[ "${2:-}" == "-F" ]] && exit 0\n'
        '    exit "${STUB_SERVER_EXISTS:-1}" ;;\n'
        '  new-session) printf \'%s\\n\' "$*" > "$STUB_ARGV_LOG" ;;\n'
        'esac\n',
        encoding='utf-8',
    )
    stub.chmod(0o755)
    start = _worktree_root() / 'fractal' / '_scripts' / 'start.sh'

    def launched_argv(server_exists: str, version: str = '3.6a') -> str:
        env = dict(os.environ)
        path = env['PATH']
        env['PATH'] = f'{bindir}{os.pathsep}{path}'
        env['OPENROUTER_API_KEY'] = 'sk-or-argv-sentinel'
        env['STUB_SERVER_EXISTS'] = server_exists
        env['STUB_TMUX_VERSION'] = version
        env['STUB_ARGV_LOG'] = str(argv_log)
        run = subprocess.run(
            ['bash', str(start), str(root)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert run.returncode == 0, run.stderr
        return argv_log.read_text(encoding='utf-8')

    # warm server: -e forwards the key (transient client, brief argv window)
    assert 'sk-or-argv-sentinel' in launched_argv('0')
    # cold server: the launch would BECOME the server, so the key must not
    # ride argv -- it reaches the loop through the inherited server environment
    assert 'sk-or-argv-sentinel' not in launched_argv('1')
    # old tmux: new-session -e needs >= 3.2, so a warm server still gets
    # no forwarding rather than a tmux usage error
    assert 'sk-or-argv-sentinel' not in launched_argv('0', version='3.1c')


# ------ helpers


def _settled_node(repo: dict, name: str) -> pathlib.Path:
    """Init a committed worker parked on a continuable terminal status.

    One trivial step and a committed worktree, so the only dirt a test sees
    is what it plants itself; ``exited`` makes ``--continue`` the legal
    relaunch path.
    """
    root = repo['root']
    init = _run(
        root,
        'node',
        'init',
        name,
        '--agent',
        'claude',
        '--max-iters',
        '1',
        '--no-sync',
        '--local',
    )
    assert init.returncode == 0, init.stderr
    worktree = root / '.worktrees' / f'main.{name}'
    steps_dir = worktree / '.fractal' / f'main.{name}' / 'steps'
    for step in steps_dir.glob('*.md'):
        step.unlink()
    (steps_dir / '01-work.md').write_text('# Work\n\nOne step.\n', encoding='utf-8')
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', f'setup {name}')
    Node(worktree).status_set('exited')
    return worktree


def _start(
    repo: dict,
    worktree: pathlib.Path,
    *flags: str,
) -> subprocess.CompletedProcess:
    """Run ``fractal node start`` with the stub agent on ``PATH``.

    ``start.sh`` propagates ``PATH`` into the tmux session, so prepending the
    stub bindir here is what makes the launched loop's ``claude`` hermetic;
    the launch's session name is recorded for the teardown reap.
    """
    env = _cli_env()
    bindir = repo['bindir']
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    root_name = repo['root'].name
    worktree_name = worktree.name.replace('.', '-')
    session = f'{root_name} ({worktree_name})'
    repo.setdefault('sessions', []).append(session)
    return subprocess.run(
        [_fractal_bin(), 'node', 'start', *flags],
        cwd=worktree,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )


def _start_continue(
    repo: dict,
    worktree: pathlib.Path,
    *flags: str,
) -> subprocess.CompletedProcess:
    """Run ``fractal node start --continue`` with the stub agent on ``PATH``."""
    return _start(repo, worktree, '--continue', *flags)


def _start_events(worktree: pathlib.Path) -> list[str]:
    """The node's completed ``start`` events' metadata, oldest first."""
    sql = (
        f"SELECT metadata FROM events WHERE node='{worktree.name}'"
        " AND event='start' AND status='completed' ORDER BY event_id"
    )
    out = _run(worktree, 'db', '_query', sql, '--csv').stdout
    return [row['metadata'] for row in csv.DictReader(io.StringIO(out))]


def _sessions() -> list[str]:
    """The tmux server's current session names."""
    listing = subprocess.run(
        ['tmux', 'list-sessions', '-F', '#{session_name}'],
        capture_output=True,
        text=True,
    )
    return listing.stdout.splitlines()


def _await_session_gone(
    repo: dict,
    worktree: pathlib.Path,
    *,
    deadline_seconds: float = 30,
) -> bool:
    """Block until the worktree's tmux session is gone (exact-name match).

    Idle-based via ``_await_progress``: any change in the server's session
    listing refreshes the allowance. Returns whether the session is gone.
    """
    root_name = repo['root'].name
    worktree_name = worktree.name.replace('.', '-')
    session = f'{root_name} ({worktree_name})'
    return _await_progress(
        check=lambda: session not in _sessions(),
        progress=lambda: _sessions(),
        deadline=time.monotonic() + deadline_seconds,
    )
