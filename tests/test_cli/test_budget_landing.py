"""An ancestor's budget abort winds down the descendant run in reserve.

A budget check's recursive finish fans out to every active descendant
with the tripping ancestor's raw reason plus the ``(via finish of
<branch>)`` attribution. The pending signal makes the descendant's
current iteration its run's last, so the loop flips the remaining steps
into reserve mode -- the cheapest complete graceful wind-down -- and the
post-iteration gate ends the run as a budget landing relabeled with the
run's own child-scope figures.

The mid-iteration fan-out is observable only through a real launch, so
this drives the real ``fractal node _loop`` as a subprocess against a
real node with a **stubbed ``claude``** -- the smallest hermetic
harness. The stub's first call relays the ancestor abort by invoking
``fractal node finish`` on the parent, so the attributed signal lands
between this run's steps, exactly as a real cascade would.
"""

from __future__ import annotations

import os
import shutil

import pytest

from tests._helpers import _git

from .conftest import _cli_env, _loop_cmd, _run, _run_reaped

__all__ = ['test_cascaded_budget_finish_winds_down_remaining_steps']

# distinctive heading from modes/RESERVE.md -- present in a step's prompt only
# when the loop has flipped the node into reserve (budget) mode
RESERVE_MARKER = 'Reserve Mode'

# the tripping ancestor's raw reason (the shape a reserve boundary records);
# its finish fan-out appends the attribution the descendant's loop keys on
ABORT_REASON = 'cost budget reserve reached (spent $0.90 >= $1.00 max - $0.10 reserve)'


# fake claude on PATH: capture the -p prompt per invocation, then emit a
# stream-json result carrying $STUB_COST so the loop records the cost; call 1
# relays an ancestor budget abort mid-iteration (see the finish block below)
_CLAUDE_STUB = """#!/usr/bin/env bash
# test stub for claude: capture the -p prompt to a per-call file and emit a
# stream-json result event so the loop records this step's cost
# capture the session id to echo in the init event, like real claude -- lets
# the loop capture and weave the session
SID=""
PREV=""
for ARG in "$@"; do
    case "$PREV" in
        --session-id|--resume) SID="$ARG"; break ;;
    esac
    PREV="$ARG"
done

PROMPT=$(cat)

N=$(( $(cat "$CAPTURE_DIR/counter" 2>/dev/null || echo 0) + 1 ))
echo "$N" > "$CAPTURE_DIR/counter"
printf '%s' "$PROMPT" > "$CAPTURE_DIR/prompt_$N.txt"

# call 1 relays the ancestor's budget abort: finishing the parent fans the
# attributed reason out to this node's run mid-iteration, like a real budget
# cascade (the parent's own self-finish refusal is irrelevant here)
if [[ "$N" -eq 1 ]]; then
    fractal node finish main --reason="$ABORT_REASON" >/dev/null 2>&1 || true
fi

[[ -n "$SID" ]] || SID=$(uuidgen | tr '[:upper:]' '[:lower:]')
# open the session's transcript where real claude keeps it, keyed by the cwd
# with every non-alphanumeric character dashed
SLUG=$(pwd -P | sed 's/[^A-Za-z0-9]/-/g')
TRANSCRIPTS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/$SLUG"
mkdir -p "$TRANSCRIPTS"
touch "$TRANSCRIPTS/$SID.jsonl"
printf '{"type":"system","subtype":"init","session_id":"%s"}\\n' "$SID"
printf '{"type":"result","session_id":"%s","total_cost_usd":%s,"num_turns":1,"duration_ms":1}\\n' \\
    "$SID" "$STUB_COST"
"""

# every case's stub cost: the two steps of a one-iteration run record $0.20
STUB_COST = 0.10


@pytest.fixture
def node_env(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Return a fresh worker node wired for one deterministic loop launch.

    Function-scoped so each case starts with an empty database -- the run and
    signal tables must reflect only this case's launch. Builds ``fractal
    init`` + a ``claude`` worker capped at one iteration with sync disabled,
    its steps replaced by two trivial files (so the loop makes exactly two
    agent calls), and a stub ``claude`` on a private bindir.
    """
    root = tmp_path_factory.mktemp('budget_landing')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'budgetlanding@test.local')
    _git(root, 'config', 'user.name', 'budgetlanding')
    (root / 'README.md').write_text('# budgetlanding\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    # user (root) node, then a claude worker: one iteration per launch, no sync,
    # no push, and no cap of its own -- the only budget path is the cascade
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
    assert init.returncode == 0, init.stderr
    worktree = root / '.worktrees' / 'main.task'
    node_dir = worktree / '.fractal' / 'main.task'
    # replace the seed steps with exactly two trivial steps (consistent NN-
    # prefix width) so the loop runs a known, minimal step sequence
    steps_dir = node_dir / 'steps'
    for step in steps_dir.glob('*.md'):
        step.unlink()
    (steps_dir / '01-alpha.md').write_text('# Alpha\n\nFirst step.\n', encoding='utf-8')
    (steps_dir / '02-beta.md').write_text('# Beta\n\nSecond step.\n', encoding='utf-8')
    # the loop runs from the package (see _loop_cmd), not a per-node copy
    # stub claude on a private bindir
    bindir = root / 'bin'
    bindir.mkdir()
    claude = bindir / 'claude'
    claude.write_text(_CLAUDE_STUB, encoding='utf-8')
    claude.chmod(0o755)
    return {'root': root, 'worktree': worktree, 'node_dir': node_dir, 'bindir': bindir}


# ------ wind-down


def test_cascaded_budget_finish_winds_down_remaining_steps(node_env: dict) -> None:
    """The steps after a cascaded budget abort run the reserve wind-down.

    The fan-out lands between steps 1 and 2, so step 2's prompt must
    carry the reserve overlay while step 1's ran plain -- and the run
    must land as a budget abort (``exited``/0) relabeled with this run's
    own child-scope figures, the propagated reason preserved inside.
    """
    worktree = node_env['worktree']
    prompts = _run_loop(node_env, capture_name='run1')
    # only the post-signal step composes the wind-down overlay
    assert RESERVE_MARKER not in prompts[1]
    assert RESERVE_MARKER in prompts[2]
    # the run landed as a budget abort under the relabeled reason: this run's
    # own figures ($0.20 over two steps, uncapped), the propagated reason kept
    propagated = f'{ABORT_REASON} (via finish of main)'
    run = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || exit_code || '/' || COALESCE(metadata, '')"
            ' FROM runs ORDER BY rowid DESC LIMIT 1',
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    expected = f'ancestor budget abort: {propagated}; this run spent $0.2000'
    assert run == f'exited/0/{expected}', run
    assert _run(worktree, 'node', 'status').stdout.strip().startswith('exited')


# ------ helpers


def _run_loop(node_env: dict, *, capture_name: str) -> dict:
    """Run one loop launch and return the captured per-step prompts.

    Runs the real loop entry with the stub ``claude`` on ``PATH`` and a fresh
    capture dir, returning ``{step_number: prompt_text}``. Asserts the launch
    landed the budget cascade as a designed stop (exit 0) and executed both
    steps -- the pending finish never cuts the iteration short.
    """
    root = node_env['root']
    worktree = node_env['worktree']
    # fresh capture dir per launch so the stub's counter restarts at 1
    capture = root / f'capture_{capture_name}'
    if capture.exists():
        shutil.rmtree(capture)
    capture.mkdir()
    # run the loop directly (no tmux): stub claude shadows PATH, the loop's own
    # fractal calls resolve to this worktree (PYTHONPATH via _cli_env)
    env = _cli_env(
        CAPTURE_DIR=f'{capture}',
        STUB_COST=str(STUB_COST),
        ABORT_REASON=ABORT_REASON,
    )
    bindir = node_env['bindir']
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    result = _run_reaped(
        _loop_cmd(worktree),
        cwd=f'{worktree}',
        env=env,
        timeout=180,
    )
    assert result.returncode == 0, (
        f'expected a designed budget landing: rc={result.returncode}\n'
        f'stdout:\n{result.stdout}\nstderr:\n{result.stderr}'
    )
    # collect captured prompts; a missing file means the loop never ran that step
    prompts = {}
    for prompt_file in capture.glob('prompt_*.txt'):
        num = int(prompt_file.stem.removeprefix('prompt_'))
        prompts[num] = prompt_file.read_text(encoding='utf-8')
    assert sorted(prompts) == [1, 2], (
        f'expected step prompts [1, 2] (got {sorted(prompts)})\n'
        f'rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'
    )
    return prompts
