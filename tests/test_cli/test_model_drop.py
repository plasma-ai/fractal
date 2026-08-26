"""Tool-native model-drop enforcement in the run loop.

Step files pin ``model:`` frontmatter, but infrastructure can silently serve a
DIFFERENT model than pinned. The loop must notice on its own: the stream
driver records every model the agent's stream actually named, and when a
completed step's record drops its pin the loop records a ``model_drop``
event, marks the attempt's own row, and re-dispatches the step once. A
second drop is recorded and marked the same way -- the loop proceeds, never
crashes -- and ``node list`` composes a ``model drop`` marker into the
node's ``detail`` while a step's newest completed attempt carries the mark,
until a clean re-dispatch or a later clean iteration supersedes it.

The policy is observable only through a real launch, so this drives the real
``fractal node _loop`` as a subprocess against a real node with a **stubbed
``claude``** (the ``test_iter_cost`` harness shape): the stub emits a
``stream-json`` session whose assistant rows name the served models from
``$STUB_MODELS`` -- call N serves the Nth word, the last word repeats, and a
comma-joined word serves one assistant row per part -- so each scenario
scripts exactly which invocation drops, and where in its stream. Step 1 pins
its model attached, step 2 pins it detached: detection reads the launch's
own stream record, so run mode must not matter.
"""

from __future__ import annotations

import csv
import io
import os
import shutil

import pytest

from fractal.core.node import Node
from tests._helpers import _git

from .conftest import _cli_env, _loop_cmd, _run, _run_reaped

__all__ = [
    'test_pinned_model_runs_clean',
    'test_model_drop_redispatches_once',
    'test_double_drop_fails_the_step_and_flags_the_listing',
]

# the model both step files pin, and the wrong one weather serves instead
PINNED = 'pinned-model'
DROPPED = 'dropped-model'


# fake claude on PATH: count invocations, then emit a stream-json session
# whose assistant rows name the served models from $STUB_MODELS (call N
# serves the Nth word; the last word repeats; a comma-joined word serves one
# assistant row per part, scripting a mid-stream substitution) -- the init
# frame echoes the --model pin like real claude, and a synthetic assistant
# row plus a sidechain row (a subagent's, naming its own model) ride behind
# the real ones so the parser's synthetic and sidechain skips are exercised
# on every call
_CLAUDE_STUB = """#!/usr/bin/env bash
# test stub for claude: emit an init frame echoing the resolved --model pin,
# one real assistant row per comma-part of the served word, one synthetic
# row, one sidechain row, and a result event carrying a small cost
SID=""
MODEL=""
PREV=""
for ARG in "$@"; do
    case "$PREV" in
        --session-id|--resume) SID="$ARG" ;;
        --model) MODEL="$ARG" ;;
    esac
    PREV="$ARG"
done

N=$(( $(cat "$CAPTURE_DIR/counter" 2>/dev/null || echo 0) + 1 ))
echo "$N" > "$CAPTURE_DIR/counter"

WORDS=($STUB_MODELS)
IDX=$((N - 1))
[[ $IDX -lt ${#WORDS[@]} ]] || IDX=$(( ${#WORDS[@]} - 1 ))
SERVED="${WORDS[$IDX]}"

[[ -n "$SID" ]] || SID=$(uuidgen | tr '[:upper:]' '[:lower:]')
printf '{"type":"system","subtype":"init","session_id":"%s","model":"%s"}\\n' \\
    "$SID" "$MODEL"
MSG=0
IFS=',' read -ra PARTS <<< "$SERVED"
for PART in "${PARTS[@]}"; do
    MSG=$((MSG + 1))
    printf '{"type":"assistant","session_id":"%s","message":{"id":"msg_%s","model":"%s"}}\\n' \\
        "$SID" "$MSG" "$PART"
done
printf '{"type":"assistant","session_id":"%s","message":{"id":"msg_x","model":"<synthetic>"}}\\n' \\
    "$SID"
printf '{"type":"assistant","session_id":"%s","parent_tool_use_id":"toolu_x","message":{"id":"msg_s","model":"sidechain-model"}}\\n' \\
    "$SID"
printf '{"type":"result","session_id":"%s","total_cost_usd":0.001,"num_turns":1,"duration_ms":1}\\n' \\
    "$SID"
"""


@pytest.fixture(scope='module')
def node_env(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Return a real worker node wired for a single, deterministic loop iteration.

    Built once (node init is expensive): ``fractal init`` + a ``claude`` worker
    capped at one iteration with sync disabled, its steps replaced by two
    model-pinned files -- step 1 attached, step 2 detached -- and a stub
    ``claude`` on a private bindir. The retry backoff is tightened so a drop's
    re-dispatch never stalls the suite. Every case funnels through
    ``_run_loop``, which uses a fresh capture dir on entry, so cases never
    inherit a prior run's counter.
    """
    root = tmp_path_factory.mktemp('model_drop')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'modeldrop@test.local')
    _git(root, 'config', 'user.name', 'modeldrop')
    (root / 'README.md').write_text('# modeldrop\n', encoding='utf-8')
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
    # a drop's re-dispatch reuses the failed-launch backoff -- tighten it so
    # the retry lands within the suite's patience
    assert _run(worktree, 'config', '_set', 'step_retry_backoff=1s').returncode == 0
    # replace the seed steps with two model-pinned steps (consistent NN-
    # prefix width); step 2 runs detached, so both run modes are enforced
    steps_dir = node_dir / 'steps'
    for step in steps_dir.glob('*.md'):
        step.unlink()
    (steps_dir / '01-alpha.md').write_text(
        f'---\nmodel: {PINNED}\n---\n# Alpha\n\nFirst step.\n',
        encoding='utf-8',
    )
    (steps_dir / '02-beta.md').write_text(
        f'---\nmodel: {PINNED}\ndetached: true\n---\n# Beta\n\nSecond step.\n',
        encoding='utf-8',
    )
    # the loop runs from the package (see _loop_cmd), not a per-node copy
    # stub claude on a private bindir
    bindir = root / 'bin'
    bindir.mkdir()
    claude = bindir / 'claude'
    claude.write_text(_CLAUDE_STUB, encoding='utf-8')
    claude.chmod(0o755)
    return {'root': root, 'worktree': worktree, 'node_dir': node_dir, 'bindir': bindir}


# ------ enforcement


def test_pinned_model_runs_clean(node_env: dict) -> None:
    """A stream reporting the pinned model records it with no event, no retry."""
    calls, run_id = _run_loop(node_env, models=PINNED)
    node = Node(node_env['worktree'])
    # one launch per step -- nothing re-dispatched
    assert calls == 2
    # the step rows record the served model, and nothing evented
    models = [row['model'] for row in node.record.steps(run_id=run_id)]
    assert models == [PINNED, PINNED]
    assert node.record.events(run_id=run_id, event='model_drop') == []
    # the run lands its clean max-iters completion, listing unflagged
    assert node.record.runs(limit=1)[0]['status'] == 'completed'
    assert 'model drop' not in _list_detail(node_env)


@pytest.mark.parametrize(
    argnames=('models', 'dropped_row_model'),
    argvalues=[
        # the whole stream serves off the pin
        (f'{DROPPED} {PINNED} {PINNED}', DROPPED),
        # the stream recovers to the pin on its last assistant row, so the
        # row's last-wins stamp reads clean and only the launch's full
        # served-model record catches the substitution
        (f'{DROPPED},{PINNED} {PINNED} {PINNED}', PINNED),
    ],
    ids=['whole_stream', 'mid_stream'],
)
def test_model_drop_redispatches_once(
    node_env: dict,
    models: str,
    dropped_row_model: str,
) -> None:
    """A dropped pin re-dispatches the step once; a clean retry stands.

    Call 1 (step 1, attached) serves off the pin, so the loop events the
    drop, marks the attempt's own row, and re-dispatches; call 2 (the
    retry) and call 3 (step 2) serve the pin. The retry's fresh row
    records the pinned model and supersedes the mark, the loop proceeds
    to a clean completion, and no marker reaches the listing -- the drop
    was resolved. A substitution the stream recovered from before ending
    buys the same re-dispatch, its row stamped with the pin it returned
    to and marked with the model that served mid-stream.
    """
    calls, run_id = _run_loop(node_env, models=models)
    node = Node(node_env['worktree'])
    # exactly one re-dispatch: two rows for step 1, one for step 2
    assert calls == 3
    # rows list newest first: step 2, the clean retry, the dropped attempt
    # -- which carries the mark its clean re-dispatch supersedes
    rows = node.record.steps(run_id=run_id)
    assert [(row['step'], row['model'], row['metadata']) for row in rows] == [
        (2, PINNED, ''),
        (1, PINNED, ''),
        (1, dropped_row_model, f'model drop (served {DROPPED})'),
    ]
    # the first drop evented against the dropped attempt's row
    events = node.record.events(run_id=run_id, event='model_drop')
    assert [event['step_id'] for event in events] == [rows[2]['step_id']]
    assert events[0]['metadata'] == f'served {DROPPED}, pinned {PINNED}'
    # resolved by the retry: the run completes and no marker lingers
    assert node.record.runs(limit=1)[0]['status'] == 'completed'
    assert 'model drop' not in _list_detail(node_env)


def test_double_drop_fails_the_step_and_flags_the_listing(node_env: dict) -> None:
    """A drop the re-dispatch cannot resolve fails the step, never ships.

    Step 2 (detached) serves off the pin on both its attempts: the loop
    records both drops and marks each attempt's row, then books the step
    -- and its iteration -- failed instead of proceeding on wrong-model
    output (pins are honored or the step fails loudly; the node is never
    killed). ``node list`` composes the ``model drop`` marker into
    ``detail``; a later clean iteration supersedes the marker.
    """
    calls, run_id = _run_loop(node_env, models=f'{PINNED} {DROPPED} {DROPPED}')
    node = Node(node_env['worktree'])
    # step 1 clean, step 2 dispatched twice -- the drop retry is single
    assert calls == 3
    # rows newest first: both dropped attempts carry their own mark, and
    # the final one -- the step's newest completed attempt -- stands
    rows = node.record.steps(run_id=run_id)
    assert [(row['step'], row['model'], row['metadata']) for row in rows] == [
        (2, DROPPED, f'model drop (served {DROPPED})'),
        (2, DROPPED, f'model drop (served {DROPPED})'),
        (1, PINNED, ''),
    ]
    # both drops evented; the unresolved drop failed the iteration and the
    # run never launders into a clean completion
    events = node.record.events(run_id=run_id, event='model_drop')
    assert len(events) == 2
    iters = node.record.iters(run_id=run_id)
    assert iters[0]['status'] == 'failed'
    assert f'model drop (served {DROPPED}, pinned {PINNED})' in (
        iters[0]['metadata'] or ''
    )
    assert node.record.runs(limit=1)[0]['status'] == 'exited'
    # the unresolved drop reaches the listing's detail column
    assert 'model drop' in _list_detail(node_env)

    # a later clean run supersedes the marker
    calls, run_id = _run_loop(node_env, models=PINNED)
    assert calls == 2
    assert (
        Node(node_env['worktree']).record.events(
            run_id=run_id,
            event='model_drop',
        )
        == []
    )
    assert 'model drop' not in _list_detail(node_env)


# ------ helpers


def _run_loop(node_env: dict, *, models: str) -> tuple[int, int]:
    """Run one loop iteration against scripted served models.

    Runs the real loop entry with the stub ``claude`` on ``PATH`` serving
    ``models`` (one word per invocation, last word repeating) and a fresh
    capture dir, and returns the invocation count with the run's id.
    """
    root = node_env['root']
    worktree = node_env['worktree']
    # fresh capture dir per run so the counter does not bleed across cases
    capture_name = models.replace(' ', '_')
    capture = root / f'capture_{capture_name}'
    if capture.exists():
        shutil.rmtree(capture)
    capture.mkdir()
    # run the loop directly (no tmux): stub claude shadows PATH, the loop's own
    # fractal calls resolve to this worktree (PYTHONPATH via _cli_env)
    env = _cli_env(CAPTURE_DIR=f'{capture}', STUB_MODELS=models)
    bindir = node_env['bindir']
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    result = _run_reaped(
        _loop_cmd(worktree),
        cwd=f'{worktree}',
        env=env,
        timeout=180,
    )
    counter = capture / 'counter'
    assert counter.exists(), (
        f'the loop never launched the stub\nrc={result.returncode}\n'
        f'stdout:\n{result.stdout}\nstderr:\n{result.stderr}'
    )
    calls = int(counter.read_text(encoding='utf-8').strip())
    run_id = Node(worktree).record.run_latest()
    assert run_id is not None
    return calls, run_id


def _list_detail(node_env: dict) -> str:
    """Return the worker's ``detail`` column from a root ``node list``."""
    listing = _run(node_env['root'], 'node', 'list', '--csv')
    assert listing.returncode == 0, listing.stderr
    row = next(
        entry
        for entry in csv.DictReader(io.StringIO(listing.stdout))
        if entry['node'] == 'main.task'
    )
    return row['detail']
