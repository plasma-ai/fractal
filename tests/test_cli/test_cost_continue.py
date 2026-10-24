"""Per-run cost budget (``max_cost``) arms each launch: runs are isolated.

``--max-cost`` is a node's *per-run* spend ceiling: every launched run
arms the cap, and no cumulative reading exists anywhere.

The launch-time twin (``node start --continue``'s budget gate: a
budget-ended run refuses a bare continue and re-arms only with an
explicit ``--max-cost``) is covered in ``test_core/test_lifecycle.py``
-- this module drives ``fractal node _loop`` directly, pinning the
loop's own budget guard, which the start-side gate never fronts.

The loop's budget guard is observable only through a real launch, so this
drives the real ``fractal node _loop`` as a subprocess against a real node
with a **stubbed ``claude``** -- the smallest hermetic harness. The stub
emits a ``stream-json`` ``result`` event carrying a fixed cost so the loop's
stream driver records each step's spend, exactly as a real run would.
"""

from __future__ import annotations

import os
import shutil

import pytest

from tests._helpers import _git

from .conftest import _cli_env, _loop_cmd, _run, _run_reaped

__all__ = ['test_continue_re_arms_per_run_budgets']

# distinctive heading from modes/RESERVE.md -- present in a step's prompt only
# when the loop has flipped the node into reserve (budget) mode
RESERVE_MARKER = 'Reserve Mode'


# fake claude on PATH: capture the -p prompt per invocation, then emit a
# stream-json result carrying $STUB_COST so the loop records the cost
_CLAUDE_STUB = """#!/usr/bin/env bash
# test stub for claude: capture the -p prompt to a per-call file and emit a
# stream-json result event so the loop records this step's cost
# capture the session id (before the arg loop consumes $@) to echo in the init
# event, like real claude -- lets the loop capture and weave the session
SID=""
PREV=""
for ARG in "$@"; do
    case "$PREV" in
        --session-id|--resume) SID="$ARG"; break ;;
    esac
    PREV="$ARG"
done

PROMPT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --) PROMPT="${2:-}"; shift 2 ;;
        *) shift ;;
    esac
done

N=$(( $(cat "$CAPTURE_DIR/counter" 2>/dev/null || echo 0) + 1 ))
echo "$N" > "$CAPTURE_DIR/counter"
printf '%s' "$PROMPT" > "$CAPTURE_DIR/prompt_$N.txt"

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

# every case's stub cost: two steps per one-iteration run record $0.20/run
STUB_COST = 0.10


@pytest.fixture
def node_env(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Return a fresh worker node wired for two deterministic loop launches.

    Function-scoped so each case starts with an empty database -- the spend
    reading must reflect only this case's runs. Builds ``fractal init`` + a
    ``claude`` worker capped at one iteration per launch with sync disabled, its
    steps replaced by two trivial files (so the loop makes exactly two agent
    calls), and a stub ``claude`` on a private bindir.
    """
    root = tmp_path_factory.mktemp('cost_continue')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'costcontinue@test.local')
    _git(root, 'config', 'user.name', 'costcontinue')
    (root / 'README.md').write_text('# costcontinue\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    # user (root) node, then a claude worker: one iteration per launch, no sync,
    # no push (so a continue runs another iteration without remote interaction)
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


# ------ enforcement


def test_continue_re_arms_per_run_budgets(node_env: dict) -> None:
    """Every launch re-arms the full cap: runs are isolated.

    Run isolation end-to-end: the drain parameters leave run 1 over the
    cap, yet run 2 (``--continue``) opens with a fresh budget (both steps
    run, never steered) and bare ``cost spent`` reads the current run only.
    """
    worktree = node_env['worktree']
    # the drain parameters: run 1 records $0.20 >= the $0.15 cap (neither step
    # alone trips the ceiling, so both run); commit the setup so the continue
    # clean/checkout preserves the seed, the two steps, and the cap (the
    # gitignored database -- carrying run 1's spend -- survives git clean -fd)
    assert _run(worktree, 'config', '_set', 'max_cost=0.15').returncode == 0
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'setup cost-continue node (per-run scope)')
    # run 1 drains the cap at the boundary; run 2 re-arms and repeats
    run1 = _run_loop(node_env, capture_name='run1', continue_=False)
    assert RESERVE_MARKER not in run1[1]
    assert RESERVE_MARKER not in run1[2]
    run2 = _run_loop(node_env, capture_name='run2', continue_=True)
    assert RESERVE_MARKER not in run2[1]
    assert RESERVE_MARKER not in run2[2]
    # bare cost spent reads the current run ($0.20, never a cross-run rollup)
    spent = _run(worktree, 'node', 'cost', 'spent').stdout.strip().removeprefix('$')
    assert float(spent) == pytest.approx(2 * STUB_COST)


# ------ helpers


def _run_loop(
    node_env: dict,
    *,
    capture_name: str,
    continue_: bool,
) -> dict:
    """Run one loop launch and return the captured per-step prompts.

    Runs the real loop entry (optionally with ``--continue``) with the stub
    ``claude`` on ``PATH`` and a fresh capture dir, returning
    ``{step_number: prompt_text}`` for this launch only. Asserts the launch
    executed both steps -- a budget-skipped step leaves no capture.
    """
    root = node_env['root']
    worktree = node_env['worktree']
    # fresh capture dir per launch so the stub's counter restarts at 1 and prompt
    # files do not bleed across runs (prompt_1 == this launch's first step)
    capture = root / f'capture_{capture_name}'
    if capture.exists():
        shutil.rmtree(capture)
    capture.mkdir()
    # run the loop directly (no tmux): stub claude shadows PATH, the loop's own
    # fractal calls resolve to this worktree (PYTHONPATH via _cli_env)
    env = _cli_env(CAPTURE_DIR=f'{capture}', STUB_COST=str(STUB_COST))
    bindir = node_env['bindir']
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    cmd = _loop_cmd(worktree)
    if continue_:
        cmd.append('--continue')
    result = _run_reaped(cmd, cwd=f'{worktree}', env=env, timeout=180)
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
