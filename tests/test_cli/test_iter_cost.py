"""Per-iteration cost cap (``max_iter_cost``) enforcement in the run loop.

The loop parses, stores, reads, and *displays* ``--max-iter-cost``
-- but it must also **enforce** it: when an iteration's accrued spend reaches the
cap, the remaining steps are steered to ``RESERVE.md`` (the same soft-cap
steering the cumulative ``max_cost`` drain already applies), never auto-stopping.

The loop's budget logic is observable only through a real launch, so this
drives the real ``fractal node _loop`` as a subprocess against a real
node with a **stubbed ``claude``** -- the smallest hermetic harness: one
iteration, two steps, no real API calls and no recursion. The stub captures each
step's prompt and emits a ``stream-json`` ``result`` event carrying a fixed cost
so the loop's stream driver records the step's spend, exactly as a real run
would.

The observable effect of the cap is that the step *after* the cap is hit
receives ``RESERVE.md`` appended to its prompt. Step 1 always runs before any
spend is recorded, so it is never steered; step 2 is steered iff the iteration's
accrued spend reached the cap.
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

import pytest

from tests._helpers import _git

from .conftest import _cli_env, _loop_cmd, _run, _run_reaped

__all__ = [
    'test_iter_cost_cap_steers_to_reserve',
    'test_reserve_budget_steers_below_buffer',
]

# distinctive heading from modes/RESERVE.md -- present in a step's prompt only
# when the loop has flipped the node into reserve (budget) mode
RESERVE_MARKER = 'Reserve Mode'


# fake claude on PATH: capture the -p prompt per invocation, then emit a
# stream-json result carrying $STUB_COST so the loop records the cost
_CLAUDE_STUB = """#!/usr/bin/env bash
# test stub for claude: capture the -p prompt to a per-call file and emit a
# stream-json result event so the loop records this step's cost
# capture the session id to echo in the init event, like real claude -- lets
# the loop's cost-delta group the steps
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

[[ -n "$SID" ]] || SID=$(uuidgen | tr '[:upper:]' '[:lower:]')
# open the session's transcript where real claude keeps it, keyed by the cwd
# with every non-alphanumeric character dashed
SLUG=$(pwd -P | sed 's/[^A-Za-z0-9]/-/g')
TRANSCRIPTS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/$SLUG"
mkdir -p "$TRANSCRIPTS"
touch "$TRANSCRIPTS/$SID.jsonl"
printf '{"type":"system","subtype":"init","session_id":"%s"}\\n' "$SID"
printf '{"type":"result","session_id":"%s","total_cost_usd":%s,"num_turns":1,"duration_ms":1}\\n' \
    "$SID" "$STUB_COST"
"""


@pytest.fixture(scope='module')
def node_env(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Return a real worker node wired for a single, deterministic loop iteration.

    Built once (node init is expensive): ``fractal init`` + a ``claude`` worker
    capped at one iteration with sync disabled, its steps replaced by two
    trivial files so the loop makes exactly two agent calls, and a stub
    ``claude`` on a private bindir. Every case funnels through ``_run_loop``,
    which resets the budget caps and uses a fresh capture dir on entry, so
    cases never inherit a prior run's state.
    """
    root = tmp_path_factory.mktemp('iter_cost')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'itercost@test.local')
    _git(root, 'config', 'user.name', 'itercost')
    (root / 'README.md').write_text('# itercost\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    # user (root) node, then a claude worker: one iteration, no sync, no push
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


@pytest.mark.parametrize(
    argnames=('max_iter_cost', 'stub_cost', 'expect_reserve'),
    argvalues=[
        (0.05, 0.10, True),  # iteration spend $0.10 reaches the $0.05/iter cap
        (10.0, 0.001, False),  # iteration spend $0.001 stays under the $10/iter cap
    ],
    ids=['reaches_cap', 'under_cap'],
)
def test_iter_cost_cap_steers_to_reserve(
    node_env: dict,
    max_iter_cost: float,
    stub_cost: float,
    expect_reserve: bool,
) -> None:
    """The step after the per-iteration cap is hit is steered to RESERVE.

    Step 1 runs before any spend is recorded, so it is never steered. Step 2 is
    steered into reserve mode iff the iteration's accrued spend (step 1's cost)
    reached ``max_iter_cost`` -- the same soft-cap steering ``max_cost`` applies,
    now driven by the per-iteration cap. The run ceiling (a per-iter cap
    requires one) sits far above every case's spend, so the steering observed
    is iter-cap driven.
    """
    prompts = _run_loop(
        node_env,
        stub_cost=stub_cost,
        max_iter_cost=max_iter_cost,
        max_cost=100.0,
    )
    # step 1 always runs before spend is recorded -> never in reserve mode
    assert RESERVE_MARKER not in prompts[1]
    # step 2 enters reserve mode iff the iteration's spend hit the cap
    assert (RESERVE_MARKER in prompts[2]) == expect_reserve


@pytest.mark.parametrize(
    argnames=('reserve_budget', 'stub_cost', 'expect_reserve'),
    argvalues=[
        (0.5, 0.6, True),  # after step 1, remaining $0.4 <= $0.5 reserve (still > 0)
        (0.5, 0.1, False),  # after step 1, remaining $0.9 > $0.5 reserve
    ],
    ids=['within_reserve', 'above_reserve'],
)
def test_reserve_budget_steers_below_buffer(
    node_env: dict,
    reserve_budget: float,
    stub_cost: float,
    expect_reserve: bool,
) -> None:
    """The reserve budget moves reserve mode earlier -- to remaining <= reserve.

    Step 1's spend leaves ``$1 - stub_cost`` of the per-run budget
    remaining: with a $0.5 reserve, $0.6 leaves $0.4 (<= reserve but
    still > $0, so a bare ``remaining <= 0`` exhaustion trigger would not
    fire) and step 2 is steered to RESERVE; $0.1 leaves $0.9 and it is
    not. Step 2 still runs, so the reserve only steers, never stops.
    """
    # each run starts fresh (per-run budgets), so a fixed $1 budget gives
    # this run full headroom independent of earlier cases on the shared node
    max_cost = 1.0
    prompts = _run_loop(
        node_env,
        stub_cost=stub_cost,
        max_cost=max_cost,
        reserve_budget=reserve_budget,
    )
    # step 1 runs before this run's spend is recorded; $1 headroom > reserve
    assert RESERVE_MARKER not in prompts[1]
    # step 2 enters reserve mode iff remaining dropped to <= the reserve budget
    assert (RESERVE_MARKER in prompts[2]) == expect_reserve


# ------ helpers


def _run_loop(
    node_env: dict,
    *,
    stub_cost: float,
    max_iter_cost: Optional[float] = None,
    max_cost: Optional[float] = None,
    reserve_budget: Optional[float] = None,
) -> dict:
    """Run one loop iteration and return the captured per-step prompts.

    Sets the given caps for this run and clears the unset ones (the module node
    is shared, so each case starts from a known budget state), runs the real
    loop entry with the stub ``claude`` on ``PATH``, and returns
    ``{step_number: prompt_text}``.
    """
    root = node_env['root']
    worktree = node_env['worktree']
    # set the caps for this run in one atomic write, clearing the unset ones
    # (null) so a prior case's budget never leaks into the next through the
    # shared module node -- one write, not one per key, so the setter's merged
    # validation always sees the run ceiling beside the per-iter cap
    settings = []
    for key, value in (
        ('max_iter_cost', max_iter_cost),
        ('max_cost', max_cost),
        ('reserve_budget', reserve_budget),
    ):
        setting_value = value if value is not None else 'null'
        settings.append(f'{key}={setting_value}')
    assert _run(worktree, 'config', '_set', *settings).returncode == 0
    # fresh capture dir per run so prompt files do not bleed across cases
    capture = root / f'capture_{max_iter_cost}_{max_cost}_{reserve_budget}_{stub_cost}'
    if capture.exists():
        shutil.rmtree(capture)
    capture.mkdir()
    # run the loop directly (no tmux): stub claude shadows PATH, the loop's own
    # fractal calls resolve to this worktree (PYTHONPATH via _cli_env)
    env = _cli_env(CAPTURE_DIR=f'{capture}', STUB_COST=str(stub_cost))
    bindir = node_env['bindir']
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    result = _run_reaped(
        _loop_cmd(worktree),
        cwd=f'{worktree}',
        env=env,
        timeout=180,
    )
    # collect captured prompts; missing files mean the loop did not reach a step
    prompts = {}
    for prompt_file in capture.glob('prompt_*.txt'):
        num = int(prompt_file.stem.removeprefix('prompt_'))
        prompts[num] = prompt_file.read_text(encoding='utf-8')
    missing = [step for step in (1, 2) if step not in prompts]
    assert not missing, (
        f'expected two step prompts, missing {missing} (got {sorted(prompts)})\n'
        f'rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'
    )
    return prompts
