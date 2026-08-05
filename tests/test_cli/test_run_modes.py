"""Sync/detached mode machinery in the run loop (``fractal node _loop``).

The loop's mode wiring is load-bearing for every node yet lightly tested. This
module pins the observable contract of that machinery, driving the real
``fractal node _loop`` as a subprocess against a real node with **stubbed
agents** -- a hermetic harness over the loop's full process contract (tmux is
the only piece these launches skip). The stub records, per
invocation, the agent name (``claude``/``codex``), the session flag wiring
(``--session-id``/``--resume``), and the step prompt, so a test can reconstruct
exactly how and in what order the loop invoked the agent.

Covered (contract pinned from the ``--detached``/``--sync`` help + ``modes/``):

- **SYNC injection ordering** -- with sync enabled, ``modes/SYNC.md`` runs as a
  separate step *before every work step* (so N steps => 2N agent calls, the SYNC
  prompt preceding each), and ``--no-sync`` disables it entirely (N calls, no
  SYNC prompt). Pins "SYNC before every step, including the first and last."
- **continuous vs detached agent reuse** -- continuous (attached) mints one
  session per iteration: the first agent call uses ``--session-id`` and every
  later call ``--resume`` (same id); detached mints a fresh id per step
  (``--session-id`` every call, never ``--resume``), so no step resumes
  another's context. The detached mode doc rides along only in detached.
- **continue mode** -- ``--continue`` appends ``modes/CONTINUE.md`` to every
  prompt; a normal launch does not.
- **per-step ``agent:`` frontmatter** -- a step's ``agent:`` override switches just
  that step's agent (others keep the base); in a continuous node each agent keeps its
  own woven session across the steps it runs, so the override does not require
  detached mode.
- **approval-wait SYNC honors ``--no-sync``** -- while the loop blocks on a
  ``requires_approval`` step it periodically runs ``modes/SYNC.md`` so the node
  can communicate; that wait-loop SYNC obeys the sync flag like the pre-step one
  (present when enabled, absent under ``--no-sync``), so ``--no-sync`` does not
  leak SYNC on every approval wait.
- **finish-wait loop** -- a finishing node must drain its active children before
  its commit (last) step. With the finish signal set and an active registered
  child, the loop runs ``wait_for_children`` before the commit step and proceeds
  only once the child goes non-active (the commit step's prompt is captured
  strictly after the child drains); that wait honors ``--sync``/``--no-sync``
  (SYNC prompts during the wait when enabled, silent polling under ``--no-sync``);
  and a short ``--timeout`` interrupts the wait -> force-commit + run end while
  the child is still active (the run terminates rather than hanging).
- **timeout force-commit bypasses hooks** -- the timeout backstop's ``--force``
  commit lands dirty work past a hard-failing host pre-commit hook (every call
  site is ``|| true``, so a hook veto would silently lose the work).
- **continue preserves operator seed edits** -- ``--continue``'s working-tree
  restore commits dirty node-dir paths (operator-attributed, ``--no-verify``)
  before its checkout/clean, so between-run steering edits survive a continue.
- **pause / resume** -- a pause parks the run in place (paused step, open
  run/iter rows, no force-commit) and ``--resume`` adopts it exactly
  there: mid-step, at a between-steps checkpoint, or at an iteration
  boundary (numbering continues; ``max_iters`` never re-arms).

The finish-wait scenarios drive the wait deterministically with a **gate**: the
stub blocks a designated step on a marker file until the driver releases it,
giving the driver a fixed point -- mid-step, children still as it left them -- to
set the loop's-own-run finish signal before the commit-step gate is reached.
"""

from __future__ import annotations

import csv
import io
import json
import os
import pathlib
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import Iterator
from typing import Any, Optional

import pytest

from fractal.core.loop import _SETUP_FAIL_CAP
from fractal.core.node import Node
from tests._helpers import _git

from .conftest import (
    _await_progress,
    _cli_env,
    _loop_cmd,
    _reap_group,
    _require_tmux,
    _run,
    _run_reaped,
    _worktree_root,
)

__all__ = [
    'test_sync_runs_before_every_step',
    'test_continuous_reuses_one_session_and_detached_never_resumes',
    'test_new_dialects_thread_sessions_through_the_loop',
    'test_effort_rides_frontmatter_over_the_node_default',
    'test_continue_mode_injects_continue_doc',
    'test_max_iters_is_per_run_budget_across_continues',
    'test_continue_commits_operator_seed_edits_instead_of_discarding',
    'test_pause_mid_step_parks_and_resume_adopts_run',
    'test_checkpoint_pause_resumes_at_next_step',
    'test_boundary_pause_resume_continues_iteration_count',
    'test_pause_with_pending_finish_still_finishes_the_iteration',
    'test_tree_latch_parks_a_booting_loop',
    'test_finish_drain_blocks_on_paused_child',
    'test_per_step_agent_override_runs_in_detached',
    'test_per_step_agent_override_weaves_sessions',
    'test_codex_continuous_node_resumes_the_thread_it_opened',
    'test_approval_wait_sync_respects_no_sync',
    'test_finish_waits_for_children_before_commit',
    'test_finish_wait_sync_respects_sync_flag',
    'test_finish_wait_interrupted_by_timeout',
    'test_run_end_wait_books_no_step_rows_after_iteration_close',
    'test_subtree_cost_ceiling_finishes_the_run',
    'test_total_cost_reserve_ends_run_after_one_winddown',
    'test_reserve_boundary_keys_on_max_cost_not_max_iter_cost',
    'test_midrun_cap_retune_takes_effect_next_iteration',
    'test_midrun_max_iters_retune_takes_effect_at_boundary',
    'test_midrun_wait_retune_reaches_next_iterations_drains',
    'test_precap_death_heals_registry_drift_on_exit',
    'test_single_step_overshoot_ends_via_reserve_boundary',
    'test_over_budget_winddown_step_is_skipped_not_run',
    'test_no_cost_cap_leaves_step_uncapped',
    'test_reserve_window_caps_step_at_remaining',
    'test_iter_cost_reserve_continues_next_iteration',
    'test_step_header_stays_self_consistent_across_midrun_retune',
    'test_goal_finish_records_completed',
    'test_self_finish_over_cap_completes_with_overshoot',
    'test_cascaded_budget_finish_records_exited',
    'test_codex_preflight_probe_aborts_on_model_rejection',
    'test_codex_preflight_failure_is_loud_and_recoverable',
    'test_codex_preflight_runs_without_timeout_binary',
    'test_stop_during_approval_wait_records_iteration_stopped',
    'test_run_exits_with_status_exited_on_timeout',
    'test_timeout_killed_step_records_partial_cost',
    'test_timeout_pre_flush_kill_marks_step_unpriced',
    'test_step_timeout_frontmatter_kills_at_the_steps_own_ceiling',
    'test_midrun_step_timeout_retune_takes_effect_next_iteration',
    'test_timeout_force_commit_bypasses_a_failing_hook',
    'test_failing_setup_records_iteration_failed_not_loop_brick',
    'test_setup_failure_cap_ends_run_exited',
    'test_boot_stands_down_when_a_retire_wins_the_boot_window',
    'test_setup_log_is_git_invisible_after_run',
    'test_setup_runs_from_worktree_root_cwd',
    'test_agent_runs_from_worktree_root_cwd',
    'test_run_completes_when_max_iters_reached',
    'test_iter_failure_reason_names_missing_steps_not_agent',
    'test_failed_step_stderr_snapshot_and_exit_coded_reason',
    'test_stream_borne_failure_reason_carries_the_cause',
    'test_cost_cap_guard_failure_reason_is_durable',
    'test_drain_wait_sync_failure_keeps_completed_with_note',
    'test_gate_teardown_reaps_full_process_group',
]

# distinctive lines lifted from the seed mode docs -- present in a prompt only
# when the loop injected that mode (the seed NODE.md carries none of them)
_SYNC_MARKER = 'Check radio and act on anything'  # modes/SYNC.md
_DETACHED_MARKER = 'Each step is a separate session'  # modes/DETACHED.md
_CONTINUE_MARKER = 'This node was continued'  # modes/CONTINUE.md
_RESUME_MARKER = 'This node was paused mid-run'  # modes/RESUME.md
_RESERVE_MARKER = 'Reserve Mode'  # modes/RESERVE.md


# marker that arms the stub's gate: a step prompt carrying it makes the stub
# block mid-step until the driver releases it (see the gate block below)
_GATE_MARKER = 'GATE-AND-WAIT'

# fake claude/codex on PATH; records per invocation: the agent name (basename
# of $0), the session flag+id if any (the continuous-mode wiring), and the step
# prompt; claude emits a stream-json result so the loop's stream driver records
# the step's cost (as a real run would); codex rides the same driver but is
# priced from tokens, so the stub only emits thread.started (persisting
# the thread id for the weave) and exits 0; a prompt carrying _GATE_MARKER arms
# the gate: the stub touches gate_ready and blocks until gate_release appears, so
# the driver gets a deterministic mid-step pause (children unchanged) to set the
# loop's finish signal before the commit-step gate is reached -- inert otherwise
_AGENT_STUB = """#!/usr/bin/env bash
SELF=$(basename "$0")

# simulate a codex account rejecting an explicit model (the run-start probe
# path): refuse the sentinel model before recording anything
if [[ "$SELF" == "codex" ]]; then
    PREV=""
    for ARG in "$@"; do
        if [[ "$PREV" == "-m" ]] && [[ "$ARG" == "reject-me" ]]; then
            echo "codex: model not available for this account" >&2
            exit 1
        fi
        PREV="$ARG"
    done
fi

# bump the shared call counter, record which agent this call used, its
# working directory, and the claude config home it was handed (if any)
N=$(( $(cat "$CAPTURE_DIR/counter" 2>/dev/null || echo 0) + 1 ))
echo "$N" > "$CAPTURE_DIR/counter"
echo "$SELF" > "$CAPTURE_DIR/agent_$N.txt"
pwd > "$CAPTURE_DIR/cwd_$N.txt"
printf '%s' "${CLAUDE_CONFIG_DIR:-}" > "$CAPTURE_DIR/claude_config_$N.txt"

# record the session flag+id without splitting on the multi-line prompt: scan
# args and capture the token following --session-id/--resume (empty if neither)
SESSION=""
PREV=""
for ARG in "$@"; do
    case "$PREV" in
        --session-id|--resume|-s|-r|--session) SESSION="$PREV $ARG"; break ;;
    esac
    PREV="$ARG"
done
printf '%s' "$SESSION" > "$CAPTURE_DIR/session_$N.txt"

# record the --settings value the launch carried (empty when none)
SETTINGS=""
PREV=""
for ARG in "$@"; do
    if [[ "$PREV" == "--settings" ]]; then SETTINGS="$ARG"; break; fi
    PREV="$ARG"
done
printf '%s' "$SETTINGS" > "$CAPTURE_DIR/settings_$N.txt"

# record the exported per-step ceiling (the step's own timeout: frontmatter
# when present, else the node-global step_timeout)
printf '%s' "${STEP_TIMEOUT_SECONDS:-}" > "$CAPTURE_DIR/step_timeout_$N.txt"

if [[ "$SELF" == "claude" ]]; then
    # claude: the prompt is the positional after the '--' terminator, the
    # per-step USD cap the value after --max-budget-usd, the reasoning
    # effort the value after --effort (each empty when the loop passed none)
    PROMPT=""
    BUDGET=""
    EFFORT=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --) PROMPT="${2:-}"; shift 2 ;;
            --max-budget-usd) BUDGET="${2:-}"; shift 2 ;;
            --effort) EFFORT="${2:-}"; shift 2 ;;
            *) shift ;;
        esac
    done
    printf '%s' "$PROMPT" > "$CAPTURE_DIR/prompt_$N.txt"
    printf '%s' "$BUDGET" > "$CAPTURE_DIR/budget_$N.txt"
    printf '%s' "$EFFORT" > "$CAPTURE_DIR/effort_$N.txt"
    # echo session_id like real claude: the opened/resumed id, or a fresh one
    SID="${SESSION##* }"
    [[ -n "$SID" ]] || SID=$(uuidgen | tr '[:upper:]' '[:lower:]')
    printf '{"type":"system","subtype":"init","session_id":"%s"}\\n' "$SID"
    # a prompt carrying USAGE-THEN-HANG emits one usage-bearing frame and hangs
    # with NO result, so the timeout kill yields the killed-mid-stream shape;
    # the usage_flushed marker tells load starvation from a recording gap
    if [[ "$PROMPT" == *USAGE-THEN-HANG* ]]; then
        printf '{"type":"assistant","session_id":"%s","message":{"usage":{"input_tokens":100,"cache_creation_input_tokens":1000,"cache_read_input_tokens":10000,"output_tokens":200}}}\\n' "$SID"
        touch "$CAPTURE_DIR/usage_flushed"
        exec sleep 60
    fi
    # a prompt carrying INIT-THEN-HANG hangs right after the init frame -- the
    # timeout kill lands BEFORE any usage flush (session captured, cost never
    # recorded); the marker tells load starvation from a streamed-then-hung stub
    if [[ "$PROMPT" == *INIT-THEN-HANG* ]]; then
        touch "$CAPTURE_DIR/init_hung"
        exec sleep 60
    fi
    # a prompt carrying FAIL-WITH-STDERR prints a diagnosis to stderr and
    # exits nonzero -- the agent-failure shape (auth/startup errors)
    if [[ "$PROMPT" == *FAIL-WITH-STDERR* ]]; then
        echo "stub failure: credentials expired" >&2
        exit 7
    fi
    # a fail_syncs marker fails every SYNC launch the same way (the exported
    # step label names SYNC launches), leaving the work steps untouched
    if [[ -f "$CAPTURE_DIR/fail_syncs" ]] && [[ "${STEP_LABEL:-}" == SYNC* ]]; then
        echo "stub failure: credentials expired" >&2
        exit 7
    fi
    printf '{"type":"result","session_id":"%s","total_cost_usd":%s,"num_turns":1,"duration_ms":1}\\n' \
        "$SID" "${STUB_COST:-0.001}"
elif [[ "$SELF" == "grok" ]]; then
    # grok: the prompt is the positional after the '--' terminator; the
    # caller-minted session rides -s (fresh) or -r (resume) and echoes back
    # on the terminal end frame
    PROMPT=""
    PREV=""
    for ARG in "$@"; do
        if [[ "$PREV" == "--" ]]; then PROMPT="$ARG"; fi
        PREV="$ARG"
    done
    printf '%s' "$PROMPT" > "$CAPTURE_DIR/prompt_$N.txt"
    SID="${SESSION##* }"
    [[ -n "$SID" ]] || SID=$(uuidgen | tr '[:upper:]' '[:lower:]')
    printf '{"type":"text","data":"ok"}\\n'
    printf '{"type":"end","stopReason":"EndTurn","sessionId":"%s","usage":{"input_tokens":10,"cache_read_input_tokens":0,"output_tokens":2,"reasoning_tokens":0,"total_tokens":12},"num_turns":1,"modelUsage":{}}\\n' "$SID"
elif [[ "$SELF" == "omp" ]]; then
    # omp: omp -p --mode json ... <PROMPT> -- the prompt is the final arg; the
    # agent mints a uuidv7 session (session header) unless -r resumes one
    PROMPT=""
    for ARG in "$@"; do PROMPT="$ARG"; done
    printf '%s' "$PROMPT" > "$CAPTURE_DIR/prompt_$N.txt"
    SID="${SESSION##* }"
    [[ -n "$SID" ]] || SID=$(uuidgen | tr '[:upper:]' '[:lower:]')
    printf '{"type":"session","id":"%s"}\\n' "$SID"
    printf '{"type":"turn_end","message":{"model":"openai/gpt-5.5","usage":{"cost":{"total":%s}}}}\\n' "${STUB_COST:-0.001}"
    printf '{"type":"agent_end","messages":[]}\\n'
elif [[ "$SELF" == "opencode" ]]; then
    # opencode: opencode run ... <PROMPT> -- the prompt is the final argument;
    # the agent mints its own session unless --session resumes one
    PROMPT=""
    for ARG in "$@"; do PROMPT="$ARG"; done
    printf '%s' "$PROMPT" > "$CAPTURE_DIR/prompt_$N.txt"
    SID="${SESSION##* }"
    [[ -n "$SID" ]] || SID="ses_$(uuidgen | tr -d - | tr '[:upper:]' '[:lower:]')"
    printf '{"type":"step_finish","timestamp":0,"sessionID":"%s","part":{"type":"step-finish","reason":"stop","cost":%s}}\\n' \
        "$SID" "${STUB_COST:-0.001}"
else
    # codex: codex exec ... <PROMPT> -- the prompt is the final argument
    PROMPT=""
    for ARG in "$@"; do PROMPT="$ARG"; done
    printf '%s' "$PROMPT" > "$CAPTURE_DIR/prompt_$N.txt"
    # codex session weaving: exec resume <id> reuses a thread, exec -C mints
    # one; record which (for the round-trip assertion) and emit thread.started so
    # the loop's stream driver captures/persists the thread id for the next step
    TID=""; PREV=""
    for ARG in "$@"; do
        if [[ "$PREV" == "resume" ]]; then TID="$ARG"; break; fi
        PREV="$ARG"
    done
    if [[ -n "$TID" ]]; then
        printf 'resume %s' "$TID" > "$CAPTURE_DIR/session_$N.txt"
    else
        TID=$(uuidgen | tr '[:upper:]' '[:lower:]')
        printf 'new %s' "$TID" > "$CAPTURE_DIR/session_$N.txt"
    fi
    printf '{"type":"thread.started","thread_id":"%s"}\\n' "$TID"
    # a prompt carrying ERROR-ON-STREAM emits an error frame and exits 0 --
    # the stream-borne failure shape (codex reports failures on the stream)
    if [[ "$PROMPT" == *ERROR-ON-STREAM* ]]; then
        printf '{"type":"error","message":"stub stream failure: quota exhausted"}\\n'
        exit 0
    fi
fi

# gate: an armed step (prompt carries _GATE_MARKER) parks here until released, so
# the loop blocks mid-step on this call while the driver arranges the wait
if [[ "$PROMPT" == *GATE-AND-WAIT* ]]; then
    touch "$CAPTURE_DIR/gate_ready"
    while [[ ! -f "$CAPTURE_DIR/gate_release" ]]; do sleep 0.05; done
fi
"""

# two trivial work steps (consistent NN- prefix width) -- a known, minimal step
# sequence so the loop makes a predictable number of agent calls
_TWO_STEPS = {
    '01-alpha.md': '# Alpha\n\nFirst step.\n',
    '02-beta.md': '# Beta\n\nSecond step.\n',
}

# the gated step's body carries _GATE_MARKER (arming the stub's gate) and is the
# *first* of two steps, so the loop parks here before the commit (last) step's
# finish gate -- giving the driver a fixed point to set finish and arrange children
_GATE_STEPS = {
    '01-gate.md': f'# Gate\n\nGated step. {_GATE_MARKER}\n',
    '02-commit.md': '# Commit\n\nCommit step.\n',
}

# the hang step's body arms the stub's usage-then-hang mode (see the stub): a
# short run timeout kills the agent mid-stream, after one priced frame
_USAGE_HANG_MARKER = 'USAGE-THEN-HANG'
_USAGE_HANG_STEPS = {
    '01-hang.md': f'# Hang\n\nHanging step. {_USAGE_HANG_MARKER}\n',
    '02-commit.md': '# Commit\n\nCommit step.\n',
}

# the init-hang step's body arms the stub's init-then-hang mode: a short run
# timeout kills the agent before its first usage flush -- the unpriced window
_INIT_HANG_MARKER = 'INIT-THEN-HANG'
_INIT_HANG_STEPS = {
    '01-hang.md': f'# Hang\n\nHanging step. {_INIT_HANG_MARKER}\n',
    '02-commit.md': '# Commit\n\nCommit step.\n',
}

# the stub's fail modes: a body carrying the fail marker fails that step, and
# a fail_syncs marker file in the capture dir fails every SYNC launch -- both
# print the diagnosis line to stderr and exit 7, the agent-failure shape
_FAIL_MARKER = 'FAIL-WITH-STDERR'
_FAIL_STDERR = 'stub failure: credentials expired'

# the stream-error mode: a codex body carrying the marker emits an error frame
# and exits 0 -- the stream-borne failure shape (the drained parser fails the
# step, not the exit code, and the .err capture stays blank)
_STREAM_ERROR_MARKER = 'ERROR-ON-STREAM'
_STREAM_ERROR_DETAIL = 'stub stream failure: quota exhausted'

# rates + expected cost for the killed-step assertion (Anthropic convention:
# disjoint buckets); the usage figures must match the stub's emitted frame
_STUB_MODEL = 'claude-fable-5'
_STUB_PRICING = {
    _STUB_MODEL: {
        'input_cost_per_token': 3e-6,
        'output_cost_per_token': 1.5e-5,
        'cache_read_input_token_cost': 3e-7,
        'cache_creation_input_token_cost': 3.75e-6,
    },
}
_STUB_USAGE_COST = 100 * 3e-6 + 1000 * 3.75e-6 + 10_000 * 3e-7 + 200 * 1.5e-5

# banners the wait emits -- distinctive lines a test can grep from the teed log
_WAIT_BANNER = 'waiting for child nodes to finish'  # wait_for_children entry
_DRAINED_BANNER = 'all child nodes finished'  # every child went non-active
_WAIT_TIMEOUT_BANNER = 'Waiting for children: timed out'  # iteration deadline hit


@pytest.fixture(scope='module')
def repo(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Return a repo with a user node and a private bindir of stubbed agents.

    Built once. Individual tests init their own uniquely-named workers via
    ``_make_node`` so their configs never interfere. ``claude`` and ``codex`` are
    the same recording stub under two names (it keys on ``basename $0``).
    """
    # tmux session names embed this dirname machine-wide: a run-unique suffix
    # keeps sibling suite runs and stale leaked sessions from duplicate-colliding
    root = tmp_path_factory.mktemp(f'run_modes_{uuid.uuid4().hex[:8]}')
    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.email', 'runmodes@test.local')
    _git(root, 'config', 'user.name', 'runmodes')
    (root / 'README.md').write_text('# runmodes\n', encoding='utf-8')
    wiki = root / 'wiki'
    wiki.mkdir()
    (wiki / '_index.md').write_text(
        '---\nname: wiki\n---\n# wiki\n\n***\n',
        encoding='utf-8',
    )
    _git(root, 'add', '-A')
    _git(root, 'commit', '-m', 'init')
    assert _run(root, 'init').returncode == 0
    # one recording stub, installed under every agent name
    bindir = root / 'bin'
    bindir.mkdir()
    for name in ('claude', 'codex', 'grok', 'opencode', 'omp'):
        agent = bindir / name
        agent.write_text(_AGENT_STUB, encoding='utf-8')
        agent.chmod(0o755)
    return {'root': root, 'bindir': bindir}


@pytest.fixture(autouse=True)
def _kill_repo_sessions(repo: dict[str, Any]) -> Iterator[None]:
    """Reap the exact tmux sessions a test started, on teardown.

    ``_register_active_child`` records each tmux session it starts (so the
    ``--live`` drain check reads the child active) on ``repo['sessions']``; this
    kills exactly those after the test and clears the list, so a session never
    leaks and the kill never reaches another test's session (a prefix match
    would, since the module-scoped repo dirname is shared). A no-op when tmux is
    unavailable.
    """
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


# ------ SYNC injection ordering


@pytest.mark.parametrize('sync', [True, False])
def test_sync_runs_before_every_step(repo: dict, sync: bool) -> None:
    """SYNC runs before *every* work step when enabled, and not at all when off.

    With sync on, the loop runs ``modes/SYNC.md`` as a separate step before each
    of the two work steps -> four agent calls in the order SYNC, alpha, SYNC,
    beta (the SYNC prompt carries the sync doc; the work prompts carry their step
    body and never the sync doc). ``--no-sync`` -> exactly two calls, neither
    carrying the sync doc. This pins SYNC-before-every-step (incl. the first and
    last) and that ``--no-sync`` truly disables it.
    """
    node = _make_node(repo, 'synca' if sync else 'syncb', detached=False, sync=sync)
    calls, result = _run_loop(repo, node, capture_name=f'sync_{sync}')

    if sync:
        # SYNC + work, per step: 4 calls, sync doc on calls 1 & 3 only
        assert len(calls) == 4, (calls, result.stderr)
        assert _SYNC_MARKER in calls[1]['prompt']
        assert 'Alpha' in calls[2]['prompt']
        assert _SYNC_MARKER not in calls[2]['prompt']
        assert _SYNC_MARKER in calls[3]['prompt']
        assert 'Beta' in calls[4]['prompt']
        assert _SYNC_MARKER not in calls[4]['prompt']
    else:
        # no SYNC step: one call per work step, no sync doc anywhere
        assert len(calls) == 2, (calls, result.stderr)
        assert 'Alpha' in calls[1]['prompt']
        assert 'Beta' in calls[2]['prompt']
        assert all(_SYNC_MARKER not in c['prompt'] for c in calls.values())


# ------ continuous vs detached agent reuse


@pytest.mark.parametrize('detached', [False, True])
def test_continuous_reuses_one_session_and_detached_never_resumes(
    repo: dict,
    detached: bool,
) -> None:
    """Continuous reuses one session per iteration; detached never resumes.

    Sync is off so the two calls map 1:1 to the two work steps. Continuous
    (attached) mints one session: call 1 opens it with ``--session-id`` and call
    2 continues it with ``--resume`` (the same id) -- so a later step keeps the
    earlier step's context. Detached mints a fresh id per call (``--session-id``
    both times, two different ids -- each step a fresh invocation, resumable
    after the fact) and rides the detached mode doc into every prompt;
    continuous carries neither the second ``--session-id`` nor that doc.
    """
    node = _make_node(
        repo=repo,
        name='detach' if detached else 'contin',
        detached=detached,
        sync=False,
    )
    calls, result = _run_loop(repo, node, capture_name=f'mode_{detached}')

    assert len(calls) == 2, (calls, result.stderr)
    if detached:
        # no session continuity -- a fresh id per call, never a resume -- and
        # the detached doc is injected every prompt
        assert calls[1]['session'].startswith('--session-id ')
        assert calls[2]['session'].startswith('--session-id ')
        assert calls[1]['session'] != calls[2]['session']
        assert all(_DETACHED_MARKER in c['prompt'] for c in calls.values())
    else:
        # one reused session: open then resume with the same id
        assert calls[1]['session'].startswith('--session-id ')
        assert calls[2]['session'].startswith('--resume ')
        session = calls[1]['session'].split()[1]
        assert calls[2]['session'].split()[1] == session
        assert all(_DETACHED_MARKER not in c['prompt'] for c in calls.values())


@pytest.mark.parametrize(
    argnames=('agent', 'resume_flag'),
    argvalues=[('grok', '-r '), ('opencode', '--session '), ('omp', '-r ')],
)
def test_new_dialects_thread_sessions_through_the_loop(
    repo: dict,
    agent: str,
    resume_flag: str,
) -> None:
    """A new-dialect worker weaves one session across a continuous run.

    Drives the loop end to end over each new wire dialect: grok launches
    on a caller-minted ``-s`` id and the loop resumes it with ``-r``;
    opencode and omp mint their own id on the first call (no session flag)
    and the loop captures it off the stream and resumes it (``--session``
    for opencode, ``-r`` for omp). Both calls land under the dialect's own
    agent name and carry one continuous session.
    """
    node = _make_node(
        repo=repo,
        name=f'dial_{agent}',
        detached=False,
        sync=False,
        agent=agent,
    )
    calls, result = _run_loop(repo, node, capture_name=f'dialect_{agent}')
    assert len(calls) == 2, (calls, result.stderr)
    assert all(call['agent'] == agent for call in calls.values())
    if agent == 'grok':
        # caller-minted fresh id, resumed in place with the same id
        assert calls[1]['session'].startswith('-s ')
        assert calls[2]['session'].startswith(resume_flag)
        assert calls[1]['session'].split()[1] == calls[2]['session'].split()[1]
    else:
        # agent-minted on the first call, captured and resumed by id
        assert calls[1]['session'] == ''
        assert calls[2]['session'].startswith(resume_flag)


def test_effort_rides_frontmatter_over_the_node_default(repo: dict) -> None:
    """The step's ``effort:`` frontmatter outranks the node's effort key.

    The node default reaches every plain step's launch; a step naming its
    own level carries that level instead -- the same override shape as
    ``model:``.
    """
    steps = {
        '01-alpha.md': '---\neffort: xhigh\n---\n# Alpha\n\nFirst step.\n',
        '02-beta.md': '# Beta\n\nSecond step.\n',
    }
    node = _make_node(repo, 'tuned', detached=False, sync=False, steps=steps)
    _run(node['worktree'], 'config', '_set', 'effort=medium')
    calls, result = _run_loop(repo, node, capture_name='effort')
    assert len(calls) == 2, (calls, result.stderr)
    capture = repo['root'] / 'capture_effort'
    assert (capture / 'effort_1.txt').read_text() == 'xhigh'
    assert (capture / 'effort_2.txt').read_text() == 'medium'


# ------ continue mode


def test_continue_mode_injects_continue_doc(repo: dict) -> None:
    """``--continue`` appends the continue mode doc to prompts; a normal launch does not.

    The worker is committed so the continue clean/checkout preserves its seed
    and steps. A normal launch carries no continue doc; the
    continued launch (the loop running further iterations) appends
    ``modes/CONTINUE.md`` to every prompt -- the observable signal that
    ``$CONTINUE_MODE`` reached the prompt builder.
    """
    node = _make_node(repo, 'continued', detached=False, sync=False, commit=True)
    # control: a normal launch never injects the continue doc
    base, _ = _run_loop(repo, node, capture_name='continue_off', continue_=False)
    assert base
    assert all(_CONTINUE_MARKER not in c['prompt'] for c in base.values())
    # max-iters is a total budget, so the fresh run spent the cap of 1; raise it
    # before continuing or there would be nothing to run
    _run(node['worktree'], 'config', '_set', 'max_iters=2')
    # continued launch: the continue doc rides into every prompt
    continued, result = _run_loop(
        repo=repo,
        node=node,
        capture_name='continue_on',
        continue_=True,
    )
    assert continued, result.stderr
    assert all(_CONTINUE_MARKER in c['prompt'] for c in continued.values())


def test_max_iters_is_per_run_budget_across_continues(repo: dict) -> None:
    """``--max-iters`` caps iterations per run, not a lifetime total across continues.

    The iteration counter restarts at 1 each run and the cap applies per run -- so a
    continue gets a fresh "1 of M" budget (here another full iteration) rather than
    continuing the count or refusing once a lifetime total is hit.
    """
    node = _make_node(
        repo=repo,
        name='relabel',
        detached=False,
        sync=False,
        commit=True,
        max_iters=1,
    )
    # fresh run spends its per-run budget: iteration 1 of 1
    _, first = _run_loop(repo, node, capture_name='relabel_1')
    assert first.returncode == 0, first.stderr
    assert 'Iteration 1 of 1' in first.stdout
    # continue without touching the cap: the count restarts at 1 of 1 and the run
    # gets a fresh per-run budget -- no carried-over "2 of ...", no third iteration
    _, second = _run_loop(repo, node, capture_name='relabel_2', continue_=True)
    assert second.returncode == 0, second.stderr
    assert 'Iteration 1 of 1' in second.stdout
    assert 'Iteration 2' not in second.stdout
    # the iteration column restarts per run too: both runs record iteration 1
    recorded = (
        _run(
            node['worktree'],
            'db',
            '_query',
            "SELECT iter FROM iters WHERE node = 'main.relabel' ORDER BY iter_id",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[1:]
    )
    assert recorded == ['1', '1'], recorded


def test_continue_commits_operator_seed_edits_instead_of_discarding(repo: dict) -> None:
    """Uncommitted seed edits made between runs survive a ``--continue``.

    The seed's documented steering flow edits ``NODE.md`` in the stopped node's
    worktree between runs, uncommitted; continue's working-tree restore
    (``checkout -- .`` + ``clean -fd``) must not silently revert it. The
    continue block commits dirty node-dir paths (operator-attributed) before
    its clean, so the edit both survives in the tree and is reachable from
    ``HEAD`` -- the committed-baseline invariant and the edit-to-steer contract
    hold at once.
    """
    node = _make_node(repo, 'seededit', detached=False, sync=False, commit=True)
    # first run completes normally
    _, first = _run_loop(repo, node, capture_name='seededit_1')
    assert first.returncode == 0, first.stderr
    # operator steering between runs: NODE.md edited, deliberately uncommitted
    node_md = node['node_dir'] / 'NODE.md'
    original = node_md.read_text(encoding='utf-8')
    node_md.write_text(
        original + '\nOPERATOR-STEERING-EDIT: current phase is 2.\n',
        encoding='utf-8',
    )
    # continued launch: the restore must preserve the edit, not discard it
    _, second = _run_loop(repo, node, capture_name='seededit_2', continue_=True)
    assert second.returncode == 0, second.stderr
    survived = node_md.read_text(encoding='utf-8')
    assert 'OPERATOR-STEERING-EDIT' in survived
    # the edit is committed before the clean, so HEAD carries it too
    result = _git(
        node['worktree'],
        'show',
        'HEAD:.fractal/main.seededit/NODE.md',
    )
    committed = result.stdout
    assert 'OPERATOR-STEERING-EDIT' in committed


# ------ pause / resume


def test_pause_mid_step_parks_and_resume_adopts_run(repo: dict) -> None:
    """A mid-step pause parks the run in place; resume continues it there.

    The full round trip through the real loop. Pause: the signal lands
    while the gated step blocks mid-invocation, the real ``pause.sh``
    aborts the recorded step process group, and the loop reclassifies the
    abort -- step recorded ``paused`` (never ``failed``), no force-commit
    (the dirty worktree is the frozen state), run and iteration rows left
    open, node parked ``paused``. Resume (``--resume``): the boot
    adopts the same run and iteration, withdraws the pause signal,
    re-enters at the interrupted step with the woven session resumed
    (``--resume`` of the pre-pause id), rides ``modes/RESUME.md`` into
    every prompt, and finishes the iteration normally.
    """
    node = _make_node(repo, 'pauser', detached=False, sync=False, steps=_GATE_STEPS)
    worktree = node['worktree']
    node_dir = node['node_dir']

    proc, capture, log = _launch_finish_wait(repo, node, capture_name='pause_mid')
    try:
        # park at the gate step, mid-invocation, and leave uncommitted work
        assert _await_gate(capture, deadline=time.monotonic() + 60), log.read_text()
        (worktree / 'wip.txt').write_text('frozen mid-step\n', encoding='utf-8')
        # the launch recorded the invocation's own process group -- the handle
        # the abort targets without touching the loop
        step_pgid = node_dir / '.step_pgid'
        assert step_pgid.exists(), 'agent launch must record .step_pgid'
        # pause: signal first (the loop reclassifies the abort by it), then
        # the real pause.sh reaps the gated agent's group
        Node(worktree).record.signal_set('pause', 'take five')
        pause_sh = _worktree_root() / 'fractal' / '_scripts' / 'pause.sh'
        abort = subprocess.run(
            ['bash', f'{pause_sh}', f'{worktree}'],
            capture_output=True,
            text=True,
        )
        assert abort.returncode == 0, abort.stderr
        assert 'Aborted in-flight agent' in abort.stdout, abort.stdout
        proc.wait(timeout=60)
    finally:
        _, result = _finish_wait_result(proc, capture, log)

    # parked: the loop exited clean with the node paused, the interrupted
    # step recorded paused, and the rows left open for adoption
    assert result.returncode == 0, result.stdout
    assert _run(worktree, 'node', 'status').stdout.strip() == 'paused', result.stdout
    rows = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status, ended_at FROM runs WHERE node = 'main.pauser'",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[1:]
    )
    assert rows == ['active,'], rows
    steps = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT step, status FROM steps WHERE node = 'main.pauser'"
            ' ORDER BY step_id',
            '--csv',
        )
        .stdout.strip()
        .splitlines()[1:]
    )
    assert steps == ['1,paused'], steps
    # no force-commit buried the frozen state: the work is still uncommitted
    assert 'wip.txt' in _git(worktree, 'status', '--porcelain').stdout
    assert 'failed on' not in _git(worktree, 'log', '--oneline', '-3').stdout

    # resume: relaunch with --resume, pre-releasing the gate so the
    # re-entered step runs straight through
    (capture / 'gate_release').touch()
    calls, resumed = _resume_loop(repo, node, capture_name='pause_mid')
    assert resumed.returncode == 0, resumed.stderr
    assert 'Resuming run' in resumed.stdout, resumed.stdout

    # the same run and iteration were adopted (never a second row), the
    # interrupted step re-ran on a fresh row, and the run closed normally
    assert (
        _run(worktree, 'node', 'status')
        .stdout.strip()
        .startswith('completed (run exhausted:')
    ), resumed.stdout
    runs = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT run_id, status FROM runs WHERE node = 'main.pauser'",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[1:]
    )
    assert len(runs) == 1, runs
    run_id, run_status = runs[0].split(',')
    assert run_status == 'completed', runs
    iters = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT iter, status FROM iters WHERE node = 'main.pauser'",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[1:]
    )
    assert iters == ['1,completed'], iters
    steps = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT step, status FROM steps WHERE node = 'main.pauser'"
            ' ORDER BY step_id',
            '--csv',
        )
        .stdout.strip()
        .splitlines()[1:]
    )
    assert steps == ['1,paused', '1,completed', '2,completed'], steps
    # the pause signal was withdrawn at adoption (it would re-park the run)
    assert Node(worktree).record.signal_get('pause', run_id=int(run_id)) is None
    # the re-entered step resumed the pre-pause session; the resume doc rode
    # into both resumed prompts and only them
    assert calls[1]['session'].startswith('--session-id '), calls[1]['session']
    assert calls[2]['session'].startswith('--resume '), calls[2]['session']
    assert calls[1]['session'].split()[1] == calls[2]['session'].split()[1]
    assert _RESUME_MARKER not in calls[1]['prompt']
    assert _RESUME_MARKER in calls[2]['prompt']
    assert _RESUME_MARKER in calls[3]['prompt']
    # the invocation handle never outlives its step
    assert not step_pgid.exists()


def test_checkpoint_pause_resumes_at_next_step(repo: dict) -> None:
    """A between-steps park re-enters at the first unrun step, not step 1.

    A pause that lands while no agent is in flight writes no paused step
    row -- the loop parks at its next checkpoint with only completed rows
    in the open iteration. Resume must derive the re-entry from those
    completed rows: step 1 never re-runs (no duplicate spend), step 2 is
    what runs next.
    """
    node = _make_node(repo, 'ckpt', detached=False, sync=False, steps=_GATE_STEPS)
    worktree = node['worktree']

    proc, capture, log = _launch_finish_wait(repo, node, capture_name='ckpt_pause')
    try:
        assert _await_gate(capture, deadline=time.monotonic() + 60), log.read_text()
        # the signal lands while step 1 is mid-flight; releasing the gate
        # lets step 1 COMPLETE, so the loop parks at the pre-step-2
        # checkpoint with no paused step row
        Node(worktree).record.signal_set('pause', 'checkpoint')
        (capture / 'gate_release').touch()
        proc.wait(timeout=60)
    finally:
        _, result = _finish_wait_result(proc, capture, log)

    # parked at the checkpoint: step 1 closed completed, nothing paused
    assert result.returncode == 0, result.stdout
    assert _run(worktree, 'node', 'status').stdout.strip() == 'paused', result.stdout
    steps = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT step, status FROM steps WHERE node = 'main.ckpt' ORDER BY step_id",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[1:]
    )
    assert steps == ['1,completed'], steps

    # resume: the adopted iteration re-enters at step 2, never re-running 1
    calls, resumed = _resume_loop(repo, node, capture_name='ckpt_pause')
    assert resumed.returncode == 0, resumed.stderr
    assert (
        _run(worktree, 'node', 'status')
        .stdout.strip()
        .startswith('completed (run exhausted:')
    ), resumed.stdout
    steps = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT step, status FROM steps WHERE node = 'main.ckpt' ORDER BY step_id",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[1:]
    )
    assert steps == ['1,completed', '2,completed'], steps
    assert len(calls) == 2, sorted(calls)
    assert 'Commit step' in calls[2]['prompt']
    assert _RESUME_MARKER in calls[2]['prompt']


def test_boundary_pause_resume_continues_iteration_count(repo: dict) -> None:
    """A pause between iterations resumes at the next iteration, not at 1.

    The park lands after ``iter_end`` closed the row (during the
    inter-iteration sleep), so no open iteration exists to adopt. Resume
    must continue the run's numbering from the newest closed row -- never
    restart at 1, which would duplicate ``(run, iter)`` pairs and re-arm
    ``max_iters``.
    """
    node = _make_node(repo, 'boundary', detached=False, sync=False, max_iters=2)
    worktree = node['worktree']
    # a long inter-iteration sleep gives the boundary park a wide window
    # (its pause poll runs every 30s chunk)
    assert _run(worktree, 'config', '_set', 'sleep=120s').returncode == 0

    proc, capture, log = _launch_finish_wait(repo, node, capture_name='boundary_pause')
    try:
        # wait for iteration 1 to close (the loop is then inside the sleep)
        def _iter_closed() -> bool:
            rows = (
                _run(
                    worktree,
                    'db',
                    '_query',
                    "SELECT COUNT(*) FROM iters WHERE node = 'main.boundary'"
                    ' AND ended_at IS NOT NULL',
                    '--csv',
                )
                .stdout.strip()
                .splitlines()[1:]
            )
            return rows == ['1']

        closed = _await_progress(
            check=_iter_closed,
            progress=lambda: _capture_activity(capture),
            deadline=time.monotonic() + 90,
        )
        assert closed, log.read_text()
        Node(worktree).record.signal_set('pause', 'boundary')
        proc.wait(timeout=60)
    finally:
        _, result = _finish_wait_result(proc, capture, log)

    # parked at the boundary: iteration 1 closed, the run open, none paused
    assert result.returncode == 0, result.stdout
    assert _run(worktree, 'node', 'status').stdout.strip() == 'paused', result.stdout

    # resume: the run continues at iteration 2 and max_iters never re-arms
    _, resumed = _resume_loop(repo, node, capture_name='boundary_pause')
    assert resumed.returncode == 0, resumed.stderr
    assert (
        _run(worktree, 'node', 'status')
        .stdout.strip()
        .startswith('completed (run exhausted:')
    ), resumed.stdout
    iters = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT iter, status FROM iters WHERE node = 'main.boundary'"
            ' ORDER BY iter_id',
            '--csv',
        )
        .stdout.strip()
        .splitlines()[1:]
    )
    assert iters == ['1,completed', '2,completed'], iters
    runs = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT COUNT(*) FROM runs WHERE node = 'main.boundary'",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[1:]
    )
    assert runs == ['1'], runs


def test_pause_with_pending_finish_still_finishes_the_iteration(repo: dict) -> None:
    """A finish pending across a park lets the adopted iteration run out.

    Finish means "after the current iteration" -- and the adopted iteration
    IS the current one, so a resume must re-enter it and run its remaining
    steps (the last one included) before the finish closes the run. Jumping
    straight to the run end would skip the final step and strand the
    iteration row open.
    """
    node = _make_node(repo, 'fwadopt', detached=False, sync=False, steps=_GATE_STEPS)
    worktree = node['worktree']

    proc, capture, log = _launch_finish_wait(repo, node, capture_name='fw_adopt')
    try:
        assert _await_gate(capture, deadline=time.monotonic() + 60), log.read_text()
        # finish lands first, then the pause aborts the gated step mid-flight
        Node(worktree).record.signal_set('finish', 'wrap up')
        Node(worktree).record.signal_set('pause', 'brake')
        pause_sh = _worktree_root() / 'fractal' / '_scripts' / 'pause.sh'
        abort = subprocess.run(
            ['bash', f'{pause_sh}', f'{worktree}'],
            capture_output=True,
            text=True,
        )
        assert abort.returncode == 0, abort.stderr
        proc.wait(timeout=60)
    finally:
        _, result = _finish_wait_result(proc, capture, log)
    assert result.returncode == 0, result.stdout
    assert _run(worktree, 'node', 'status').stdout.strip() == 'paused', result.stdout

    # resume: the adopted iteration re-enters and runs BOTH steps before the
    # surviving finish closes the run completed
    (capture / 'gate_release').touch()
    _, resumed = _resume_loop(repo, node, capture_name='fw_adopt')
    assert resumed.returncode == 0, resumed.stderr
    assert _run(worktree, 'node', 'status').stdout.strip() == 'completed', (
        resumed.stdout
    )
    steps = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT step, status FROM steps WHERE node = 'main.fwadopt'"
            ' ORDER BY step_id',
            '--csv',
        )
        .stdout.strip()
        .splitlines()[1:]
    )
    assert steps == ['1,paused', '1,completed', '2,completed'], steps
    # the adopted iteration closed -- never stranded open
    iters = (
        _run(
            worktree,
            'db',
            '_query',
            'SELECT iter, status, ended_at IS NULL FROM iters'
            " WHERE node = 'main.fwadopt'",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[1:]
    )
    assert iters == ['1,completed,0'], iters


def test_tree_latch_parks_a_booting_loop(repo: dict) -> None:
    """A loop that boots under the tree latch parks itself at boot.

    The latch guards ``init``/``start``, but a start already in flight when
    the tree-wide brake lands reaches the loop anyway -- the boot check is
    the backstop: the loop parks ``paused`` before running any step, with
    its open run adoptable by a later resume.
    """
    node = _make_node(repo, 'bootpark', detached=False, sync=False)
    worktree = node['worktree']
    # the tree-wide brake's marker, as a user-node pause writes it
    latch = repo['root'] / '.fractal' / 'main' / '.paused'
    latch.write_text('paused\n', encoding='utf-8')
    try:
        calls, result = _run_loop(repo, node, capture_name='bootpark_on')
        assert result.returncode == 0, result.stderr
        assert 'Parked at boot' in result.stdout, result.stdout
        assert _run(worktree, 'node', 'status').stdout.strip() == 'paused', (
            result.stdout
        )
        # no step ran, and the run row is open for adoption
        assert not calls, calls
        rows = (
            _run(
                worktree,
                'db',
                '_query',
                "SELECT status, ended_at FROM runs WHERE node = 'main.bootpark'",
                '--csv',
            )
            .stdout.strip()
            .splitlines()[1:]
        )
        assert rows == ['active,'], rows
    finally:
        latch.unlink(missing_ok=True)

    # released: the resume relaunch adopts the empty open run and runs it
    _, resumed = _resume_loop(repo, node, capture_name='bootpark_on')
    assert resumed.returncode == 0, resumed.stderr
    assert (
        _run(worktree, 'node', 'status')
        .stdout.strip()
        .startswith('completed (run exhausted:')
    ), resumed.stdout
    runs = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT COUNT(*) FROM runs WHERE node = 'main.bootpark'",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[1:]
    )
    assert runs == ['1'], runs


def test_finish_drain_blocks_on_paused_child(repo: dict) -> None:
    """A finishing parent's drain treats a paused child as still draining.

    A paused child stops counting as ``active``, but it is frozen mid-work
    and will return -- the parent's commit (last) step must not run over
    it. The drain clears only once the child settles for real.
    """
    node = _make_node(repo, 'fwpause', detached=False, sync=False, steps=_GATE_STEPS)
    worktree = node['worktree']
    child = _register_active_child(repo, node, 'kid')
    # park the child: paused with its loop gone -- exactly how a paused
    # node looks (the registration's placeholder session is dropped)
    Node(child).status_set('paused')
    root_name = repo['root'].name
    child_name = child.name.replace('.', '-')
    session = f'{root_name} ({child_name})'
    subprocess.run(['tmux', 'kill-session', '-t', f'={session}'], capture_output=True)

    proc, capture, log = _launch_finish_wait(repo, node, capture_name='fw_paused')
    try:
        # park at the gate step, then finish (the run now exists)
        assert _await_gate(capture, deadline=time.monotonic() + 60), log.read_text()
        Node(worktree).record.signal_set('finish', 'done now')
        (capture / 'gate_release').touch()

        # the drain holds on the paused child: the banner appears and the
        # commit step's prompt stays uncaptured for the whole hold
        assert _await_log(log, _WAIT_BANNER, deadline=time.monotonic() + 30), (
            log.read_text()
        )
        time.sleep(8)
        commit_calls = [
            f
            for f in capture.glob('prompt_*.txt')
            if 'Commit step' in f.read_text(encoding='utf-8')
        ]
        assert not commit_calls, [f.name for f in commit_calls]

        # the child settles for real -> the drain clears
        Node(child).status_set('completed')
    finally:
        calls, result = _finish_wait_result(proc, capture, log)

    # the wait ran and cleared, and the commit step ran only after the settle
    assert _WAIT_BANNER in result.stdout, result.stdout
    assert _DRAINED_BANNER in result.stdout, result.stdout
    commit_calls = [n for n, c in calls.items() if 'Commit step' in c['prompt']]
    assert commit_calls, (calls, result.stdout)


# ------ per-step agent: frontmatter


def test_per_step_agent_override_runs_in_detached(repo: dict) -> None:
    """A detached step's ``agent:`` override switches just that step's agent.

    The base agent is ``claude``; step 2 declares ``agent: codex`` in its
    frontmatter. In detached mode the loop honors the override: step 1 runs on
    ``claude`` (the base), step 2 on ``codex`` -- so the override resolves per
    step without disturbing the others.
    """
    steps = {
        '01-alpha.md': '# Alpha\n\nFirst step.\n',
        '02-beta.md': '---\nagent: codex\n---\n# Beta\n\nSecond step.\n',
    }
    node = _make_node(repo, 'pstepd', detached=True, sync=False, steps=steps)
    calls, result = _run_loop(repo, node, capture_name='pstep_detached')

    assert len(calls) == 2, (calls, result.stderr)
    assert calls[1]['agent'] == 'claude'
    assert calls[2]['agent'] == 'codex'


def test_per_step_agent_override_weaves_sessions(repo: dict) -> None:
    """A per-step ``agent:`` override is allowed in continuous mode, woven per agent.

    With steps [claude, codex, claude] the override runs codex for the middle step
    while the base claude session continues across it: claude opens its session on
    step 1 and resumes the same id on step 3, even though a codex step ran between.
    """
    steps = {
        '01-alpha.md': '# Alpha\n\nFirst step.\n',
        '02-beta.md': '---\nagent: codex\n---\n# Beta\n\nSecond step.\n',
        '03-gamma.md': '# Gamma\n\nThird step.\n',
    }
    node = _make_node(repo, 'pstepw', detached=False, sync=False, steps=steps)
    calls, result = _run_loop(repo, node, capture_name='pstep_weave')

    # all three steps run; the middle one on the codex override
    assert len(calls) == 3, (calls, result.stderr)
    assert calls[1]['agent'] == 'claude'
    assert calls[2]['agent'] == 'codex'
    assert calls[3]['agent'] == 'claude'
    # claude weaves across the codex step: step 1 opens, step 3 resumes the same id
    assert calls[1]['session'].startswith('--session-id ')
    assert calls[3]['session'].startswith('--resume ')
    assert calls[3]['session'].split()[1] == calls[1]['session'].split()[1]


def test_codex_continuous_node_resumes_the_thread_it_opened(repo: dict) -> None:
    """A continuous codex node opens a thread, then resumes the same one.

    Step 1 runs ``exec -C`` (new thread); the loop's stream driver records the codex
    thread id (from ``thread.started``) to ``.session``; step 2 runs ``exec resume
    <same id>``. The stub emits ``thread.started``, covering this round-trip.
    """
    node = _make_node(repo, 'codexweave', detached=False, sync=False, agent='codex')
    calls, result = _run_loop(repo, node, capture_name='codexweave')

    assert result.returncode == 0, result.stdout
    assert calls[1]['agent'] == 'codex'
    assert calls[2]['agent'] == 'codex'
    # step 1 opened a new thread; step 2 resumed the same id (the round-trip)
    assert calls[1]['session'].startswith('new ')
    thread_id = calls[1]['session'].split()[1]
    assert calls[2]['session'] == f'resume {thread_id}'


# ------ approval-wait SYNC honors --no-sync


@pytest.mark.parametrize('sync', [True, False])
def test_approval_wait_sync_respects_no_sync(repo: dict, sync: bool) -> None:
    """The approval-wait SYNC obeys ``--sync``/``--no-sync`` like the pre-step SYNC.

    A step with ``requires_approval: true`` makes the loop block after the step
    and -- so the node can still communicate while it waits -- periodically run
    ``modes/SYNC.md``. That wait-loop SYNC is the same sync mode the
    ``--sync``/``--no-sync`` flag governs, so it must respect the flag exactly as
    the pre-step SYNC does: present when enabled, absent under ``--no-sync``.
    Otherwise ``--no-sync`` silently leaks SYNC (extra agent calls, cost, radio
    writes) on every approval wait. Approval is granted externally from the parent
    branch only after the work step's call has fired plus a margin, so the
    observable -- SYNC calls *after* the work step -- isolates the wait-loop SYNC
    from the pre-step SYNC.
    """
    work_marker = 'Work step body'
    steps = {
        '01-work.md': f'---\nrequires_approval: true\n---\n# Work\n\n{work_marker}\n',
    }
    node = _make_node(
        repo=repo,
        name='apprsync' if sync else 'apprnosync',
        detached=False,
        sync=sync,
        steps=steps,
    )
    # a 1s wait keeps the drain-SYNC cadence inside the approval margin --
    # at the default the first wait SYNC would land a full minute in, timing
    # out the presence arm and leaving the absence arm vacuous
    assert _run(node['worktree'], 'config', '_set', 'wait=1s').returncode == 0
    calls, result = _run_loop_with_approval(
        repo=repo,
        node=node,
        capture_name=f'appr_{sync}',
        work_marker=work_marker,
    )

    # locate the single work-step call (the only prompt carrying the work body),
    # then count SYNC calls that follow it -- those are the approval-wait SYNCs
    work_calls = [n for n, c in calls.items() if work_marker in c['prompt']]
    assert work_calls, (calls, result.stderr)
    work_call = min(work_calls)
    wait_syncs = [
        n for n, c in calls.items() if n > work_call and _SYNC_MARKER in c['prompt']
    ]
    if sync:
        # sync enabled: the wait-loop SYNC runs while the node waits for approval
        assert wait_syncs, (calls, result.stderr)
    else:
        # --no-sync must suppress the wait-loop SYNC, not only the pre-step SYNC
        assert not wait_syncs, (calls, result.stderr)


# ------ finish-wait loop


def test_finish_waits_for_children_before_commit(repo: dict) -> None:
    """A finishing node drains its active child before its commit (last) step.

    The loop must not commit a finishing node while a child is still running --
    the commit (last) step is gated on ``wait_for_children``, which blocks until
    every active descendant drains. With sync off the two work steps map 1:1 to
    two agent calls; the first (gate) step parks the loop so the driver can set
    the loop's-own-run finish signal *after* its run exists, with the child still
    active. Once released, the loop reaches the commit-step gate, sees the active
    child, and blocks: the commit step's prompt stays uncaptured (the ordering
    proof -- it cannot run while the child is up) until the driver drains the
    child, after which the commit step finally fires.
    """
    node = _make_node(repo, 'fwcommit', detached=False, sync=False, steps=_GATE_STEPS)
    worktree = node['worktree']
    child = _register_active_child(repo, node, 'kid')

    proc, capture, log = _launch_finish_wait(repo, node, capture_name='fw_commit')
    try:
        # park at the gate step, then finish (run now exists) with the child active
        assert _await_gate(capture, deadline=time.monotonic() + 60), log.read_text()
        # the parked run recorded its live process group beside .status -- the
        # handle kill/reconcile reap an orphaned agent by
        pgid_file = node['node_dir'] / '.pgid'
        assert pgid_file.exists(), 'run start must record .pgid'
        os.killpg(int(pgid_file.read_text(encoding='utf-8').strip()), 0)
        listed = _run(
            worktree,
            'node',
            'list',
            '--status',
            'active',
            '--live',
            '--count',
        )
        active = listed.stdout.strip()
        assert active == '1', active
        Node(worktree).record.signal_set('finish', 'done now')
        (capture / 'gate_release').touch()

        # the loop reaches the commit-step gate and blocks on the active child:
        # the commit step's prompt must not be captured until the child drains
        assert _await_log(log, _WAIT_BANNER, deadline=time.monotonic() + 30), (
            log.read_text()
        )
        commit_before = list(capture.glob('prompt_*.txt'))
        commit_calls = [
            f for f in commit_before if 'Commit step' in f.read_text(encoding='utf-8')
        ]
        assert not commit_calls, [f.name for f in commit_before]

        # drain the child -> the wait clears and the commit step finally runs
        Node(child).status_set('completed')
    finally:
        calls, result = _finish_wait_result(proc, capture, log)

    # the wait ran and cleared, and the commit step ran only after the drain
    assert _WAIT_BANNER in result.stdout, result.stdout
    assert _DRAINED_BANNER in result.stdout, result.stdout
    commit_calls = [n for n, c in calls.items() if 'Commit step' in c['prompt']]
    assert commit_calls, (calls, result.stdout)
    # the in-band exit dropped the pgid handle -- nothing left to reap
    assert not pgid_file.exists()


@pytest.mark.parametrize('sync', [True, False])
def test_finish_wait_sync_respects_sync_flag(repo: dict, sync: bool) -> None:
    """The finish-wait SYNC obeys ``--sync``/``--no-sync`` like every other SYNC.

    While a finishing node waits for its children it periodically runs
    ``modes/SYNC.md`` so it stays reachable -- and that wait SYNC is gated on the
    sync flag like the pre-step and approval-wait SYNCs. With sync on, SYNC
    prompts fire during the wait (calls after the gate step carrying the sync
    doc); under ``--no-sync`` the wait polls silently, so no SYNC leaks. The gate
    step isolates the wait SYNC: any SYNC call numbered after it ran inside the
    wait, not before a work step.
    """
    node = _make_node(
        repo=repo,
        name='fwsync' if sync else 'fwnosync',
        detached=False,
        sync=sync,
        steps=_GATE_STEPS,
    )
    worktree = node['worktree']
    # a 1s wait keeps the drain-SYNC cadence inside the wait window below --
    # at the default the first wait SYNC would land a full minute in, timing
    # out the presence arm and leaving the absence arm vacuous
    assert _run(worktree, 'config', '_set', 'wait=1s').returncode == 0
    child = _register_active_child(repo, node, 'kid')

    capture_name = f'fw_sync_{sync}'
    proc, capture, log = _launch_finish_wait(repo, node, capture_name=capture_name)
    try:
        # park, finish, release so the loop enters the wait with the child active
        assert _await_gate(capture, deadline=time.monotonic() + 60), log.read_text()
        Node(worktree).record.signal_set('finish', 'done now')
        (capture / 'gate_release').touch()
        assert _await_log(log, _WAIT_BANNER, deadline=time.monotonic() + 30), (
            log.read_text()
        )
        # let the wait spin several 1s cadences (the wait=1s pin above) so a
        # SYNC -- if the flag permits one -- has fired before the child drains
        time.sleep(3)
        Node(child).status_set('completed')
    finally:
        calls, result = _finish_wait_result(proc, capture, log)

    # the gate step is the only prompt carrying _GATE_MARKER; SYNC calls numbered
    # after it ran inside the wait (the pre-step SYNC, if any, precedes the gate)
    gate_calls = [n for n, c in calls.items() if _GATE_MARKER in c['prompt']]
    assert gate_calls, (calls, result.stdout)
    gate_call = max(gate_calls)
    wait_syncs = [
        n for n, c in calls.items() if n > gate_call and _SYNC_MARKER in c['prompt']
    ]
    if sync:
        # sync enabled: the wait SYNC keeps the finishing node reachable
        assert wait_syncs, (calls, result.stdout)
    else:
        # --no-sync must suppress the wait SYNC, not only the pre-step SYNC
        assert not wait_syncs, (calls, result.stdout)


def test_finish_wait_interrupted_by_timeout(repo: dict) -> None:
    """A short ``--timeout`` interrupts the finish-wait -> force-commit + run end.

    The wait is not unconditional -- a crashed-but-active child would otherwise
    hang a finishing node forever, so the run timeout (and a stop) interrupt
    it. With a short ``--timeout`` and a child that never drains, the loop enters
    the wait, hits the run deadline, force-commits directly, and ends the
    run instead of hanging. The gate is released promptly so the gate step itself
    is not the thing that times out; the deadline then elapses inside the wait.
    """
    node = _make_node(repo, 'fwtimeout', detached=False, sync=False, steps=_GATE_STEPS)
    worktree = node['worktree']
    # short run timeout: the wait will hit the deadline and bail
    assert _run(worktree, 'config', '_set', 'timeout=4s').returncode == 0
    # register an active child and never drain it -- the wait must time out, not
    # clear (the child staying active is the whole point of this scenario)
    _register_active_child(repo, node, 'kid')

    proc, capture, log = _launch_finish_wait(repo, node, capture_name='fw_timeout')
    try:
        # park, finish, release promptly -- the gate step must not be what times
        # out; the deadline then elapses inside the wait (the child never drains)
        assert _await_gate(capture, deadline=time.monotonic() + 60), log.read_text()
        Node(worktree).record.signal_set('finish', 'done now')
        (capture / 'gate_release').touch()
    finally:
        # a hung wait would force the kill in the finalizer; a clean timeout exits
        calls, result = _finish_wait_result(proc, capture, log, timeout=30)

    # the run terminated on its own (not killed), the wait timed out, and the
    # commit step was skipped in favor of a direct force-commit -- all while the
    # child was still active
    assert result.returncode == 0, result.stdout
    assert _WAIT_TIMEOUT_BANNER in result.stdout, result.stdout
    commit_calls = [n for n, c in calls.items() if 'Commit step' in c['prompt']]
    assert not commit_calls, (calls, result.stdout)
    listed = _run(
        worktree,
        'node',
        'list',
        '--status',
        'active',
        '--live',
        '--count',
    )
    active = listed.stdout.strip()
    assert active == '1', active


def test_run_end_wait_books_no_step_rows_after_iteration_close(repo: dict) -> None:
    """The run-end child drain books no SYNC step rows against a closed iteration.

    After the final ``iter_end`` a finishing run drains its children
    (``wait_for_children "run end"``) before the loop exits. The wait SYNC is
    gated on a live iteration, but ``ITER_ID`` survives iteration close, so a
    gate keyed on ``ITER_ID`` alone would book a zombie SYNC row against the
    already-closed iteration on every poll -- each instant and cost-free,
    because the over-budget guard skips the launch inside the reserve window.
    Reserve boundary + an active child open exactly that window: the run ends
    after one wind-down iteration, the wait engages and drains, and the steps
    table must carry no row started after its own iteration ended.
    """
    node = _make_node(
        repo=repo,
        name='zombierows',
        detached=False,
        sync=True,
        max_iters=2,
    )
    worktree = node['worktree']
    # an active child at run end is what arms the wait -- a childless run
    # returns before any booking could happen; register it before the cost
    # cap lands (a capped parent rejects a capless child at init)
    child = _register_active_child(repo, node, 'kid')
    # cap the run and carve a wide reserve so the boundary ends it after one
    # iteration (with sync on, the pre-step SYNCs bill too -- it still trips)
    assert _run(worktree, 'config', '_set', 'max_cost=1.0').returncode == 0
    assert _run(worktree, 'config', '_set', 'reserve_budget=0.7').returncode == 0
    # a 1s wait keeps the zombie-booking window inside the spin below -- at
    # the default the first wait SYNC attempt would land a full minute in,
    # making the no-zombie assertion vacuous
    assert _run(worktree, 'config', '_set', 'wait=1s').returncode == 0

    proc, capture, log = _launch_finish_wait(
        repo=repo,
        node=node,
        capture_name='zombie_rows',
        stub_cost='0.4',
    )
    try:
        # the loop reaches the run-end wait on its own (reserve boundary after
        # iteration 1); let it spin several 1s cadences (the wait=1s pin) so
        # a zombie booking -- if the gate leaks one -- has fired, then drain
        assert _await_log(
            log=log,
            marker='waiting for child nodes to finish (run end)',
            deadline=time.monotonic() + 90,
        ), log.read_text()
        time.sleep(3)
        Node(child).status_set('completed')
    finally:
        _, result = _finish_wait_result(proc, capture, log)

    # the wait engaged and drained -- the zombie window was really open
    assert _DRAINED_BANNER in result.stdout, result.stdout
    # the run itself landed terminal via the boundary (exited, not hung)
    assert _run(worktree, 'node', 'status').stdout.strip().startswith('exited'), (
        result.stdout
    )
    # the zombie signature: step rows started after their iteration ended
    zombies = (
        _run(
            worktree,
            'db',
            '_query',
            'SELECT COUNT(*) FROM steps JOIN iters ON steps.iter_id = iters.iter_id'
            ' WHERE steps.started_at > iters.ended_at',
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert zombies == '0', (zombies, result.stdout)


def test_subtree_cost_ceiling_finishes_the_run(repo: dict) -> None:
    """A subtree-budget abort ends the run ``exited``/0, not ``completed``/0.

    The cost cap is a subtree ceiling, not advisory: with a low ``--max-cost``
    and an agent reporting more than the cap, the loop's subtree-cost check trips
    mid-iteration (after the step that blows the budget), finishes recursively
    (here a leaf, so just itself), and ends the run well before ``--max-iters``.
    The first $1 step already exceeds the $0.50 cap, so the loop stops before the
    second step runs -- and because the work is unfinished (the ceiling, not the
    goal, ended it), the run is recorded ``exited``/0 -- the designed budget
    landing -- so a parent and ``node merge`` can tell a budget abort apart
    from a goal-met completion.
    """
    node = _make_node(
        repo=repo,
        name='budget',
        detached=False,
        sync=False,
        max_iters=5,
        max_cost=0.50,
    )
    worktree = node['worktree']
    calls, result = _run_loop(repo, node, capture_name='budget', stub_cost='1.0')

    # the ceiling tripped mid-iteration, before the second step ever ran
    assert 'Subtree cost budget reached' in result.stdout, result.stdout
    assert len(calls) == 1, (calls, result.stdout)
    # a budget landing is exited/0 (never the goal-met completed a plain
    # finish gives, never the abnormal exit 1)
    assert _run(worktree, 'node', 'status').stdout.strip().startswith('exited'), (
        result.stdout
    )
    run = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || exit_code FROM runs ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert run == 'exited/0', (run, result.stdout)


def test_total_cost_reserve_ends_run_after_one_winddown(repo: dict) -> None:
    """Entering total-cost reserve ends the run after one wind-down iteration.

    The reserve carves a buffer below ``--max-cost`` for a cheap cleanup pass;
    once a node drains into it the loop must not keep starting fresh iterations --
    each re-entering reserve -- until the hard ceiling. With ``--max-cost 1.0``, a
    $0.70 reserve, and a $0.40/step agent over two steps: step 1 runs un-steered
    ($1.00 remaining > the reserve), step 2 is steered into RESERVE ($0.60
    remaining <= $0.70), and the iteration ends with $0.20 remaining -- inside the
    reserve yet with spend ($0.80) still below the $1.00 ceiling. So the boundary
    ends the run before a second iteration despite ``--max-iters 2``; the hard
    ceiling never trips (the reserve, not the cap or max-iters, ended it), and the
    budget landing is recorded ``exited``/0.
    """
    node = _make_node(
        repo=repo,
        name='reserve',
        detached=False,
        sync=False,
        max_iters=2,
        max_cost=1.0,
    )
    worktree = node['worktree']
    # carve a wide reserve so step 2 enters RESERVE while spend is still under the
    # cap (node init defaulted it to 10%); the loop reads this from config.json
    assert _run(worktree, 'config', '_set', 'reserve_budget=0.7').returncode == 0
    calls, result = _run_loop(repo, node, capture_name='reserve', stub_cost='0.4')

    # the second step wound down in RESERVE; the first ran before spend crossed in
    assert _RESERVE_MARKER not in calls[1]['prompt'], (calls, result.stdout)
    assert _RESERVE_MARKER in calls[2]['prompt'], (calls, result.stdout)
    # the run ended after one iteration: two work steps, no second iteration
    assert len(calls) == 2, (calls, result.stdout)
    # it ended via the reserve boundary, not the hard ceiling (spend < max_cost)
    assert 'Total cost budget reserve reached' in result.stdout, result.stdout
    assert 'Subtree cost budget reached' not in result.stdout, result.stdout
    # a budget landing is exited/0 (never the goal-met completed a plain
    # finish gives, never the abnormal exit 1)
    assert _run(worktree, 'node', 'status').stdout.strip().startswith('exited'), (
        result.stdout
    )
    run = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || exit_code FROM runs ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert run == 'exited/0', (run, result.stdout)
    # the recorded reason carries the boundary's operands (spent/max/reserve),
    # so a run or signal row is self-diagnosing without config archaeology
    reason = (
        _run(
            worktree,
            'db',
            '_query',
            'SELECT metadata FROM runs ORDER BY rowid DESC LIMIT 1',
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert 'cost budget reserve reached (spent $' in reason, (reason, result.stdout)
    assert 'reserve)' in reason, (reason, result.stdout)


def test_reserve_boundary_keys_on_max_cost_not_max_iter_cost(repo: dict) -> None:
    """The reserve threshold is ``max_cost - reserve_budget``, never iter-cap keyed.

    An iter-cap-keyed threshold would fire early wherever ``max_iter_cost``
    sits below ``max_cost``, and such a mis-fire is indistinguishable from
    a correct reserve stop without this pin: with a ``max_iter_cost`` below
    ``max_cost`` and spend ($0.90) at/above every iter-keyed threshold yet
    below the true one ($1.08), the boundary must not fire -- the run ends
    by ``--max-iters``, ``completed``/0.
    """
    node = _make_node(
        repo=repo,
        name='keyed',
        detached=False,
        sync=False,
        max_iters=1,
        max_cost=1.2,
    )
    worktree = node['worktree']
    # a wrong-key threshold would sit at or below the iteration spend: two $0.45
    # steps spend $0.90 >= 0.95 - 0.12 (iter-cap keyed) but < 1.2 - 0.12 (true);
    # max_iter_cost stays above the $0.90 iteration so no iter machinery fires
    assert _run(worktree, 'config', '_set', 'max_iter_cost=0.95').returncode == 0
    assert _run(worktree, 'config', '_set', 'reserve_budget=0.12').returncode == 0
    calls, result = _run_loop(repo, node, capture_name='keyed', stub_cost='0.45')

    # both steps ran and no budget stop fired -- the run ended by max-iters alone
    assert len(calls) == 2, (calls, result.stdout)
    assert 'Total cost budget reserve reached' not in result.stdout, result.stdout
    assert 'Subtree cost budget reached' not in result.stdout, result.stdout
    run = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || exit_code FROM runs ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert run == 'completed/0', (run, result.stdout)


def test_midrun_cap_retune_takes_effect_next_iteration(repo: dict) -> None:
    """A mid-run config cap edit is enforced at the next iteration boundary.

    Cap keys read once at run start would leave a post-spawn retune -- a
    direct config edit or a ``fractal node update`` -- silently ignored by
    enforcement until the next run (nodes killed at a stale cap, raises never
    honored mid-run). Here iteration 1's setup drops ``max_cost`` from $5 to
    $0.10 while the two $0.40 steps spend $0.80: the iteration-2 boundary
    must pick up the new cap and end the run in the budget family instead of
    burning to ``--max-iters``, and the drifted registry row (still at the
    spawn-time cap, fooling every ``node list`` reader) must be healed
    config->registry with a loud warning.
    """
    node = _make_node(
        repo=repo,
        name='retune',
        detached=False,
        sync=False,
        max_iters=2,
        max_cost=5.0,
    )
    worktree = node['worktree']
    # iteration 1's setup lowers the cap below the spend the two steps leave
    # behind -- a mid-run retune, invisible to a run-start-only cap read (the
    # reserve shrinks with it: init's 10% default fails the <99%-of-cap check)
    setup = node['node_dir'] / 'scripts' / 'setup.sh'
    setup.write_text(
        '#!/usr/bin/env bash\n'
        f'fractal config _set max_cost=0.1 reserve_budget=0.005 --path="{worktree}"\n',
        encoding='utf-8',
    )
    calls, result = _run_loop(repo, node, capture_name='retune', stub_cost='0.4')

    # iteration 2 stopped at the refreshed cap instead of running both steps
    # at the stale one and closing completed/0 "Reached max iterations"
    assert len(calls) < 4, (sorted(calls), result.stdout)
    assert 'cost budget' in result.stdout, result.stdout
    assert 'Reached max iterations' not in result.stdout, result.stdout
    run = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || exit_code FROM runs ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert run == 'exited/0', (run, result.stdout)
    # the drifted registry row was healed config->registry, loudly
    assert '!= registry' in result.stdout, result.stdout
    assert 'max_cost=0.1' in result.stdout, result.stdout
    row_cap = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT max_cost FROM nodes WHERE node = 'main.retune'",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert row_cap == '0.1', (row_cap, result.stdout)


def test_midrun_max_iters_retune_takes_effect_at_boundary(repo: dict) -> None:
    """A mid-run ``max_iters`` raise is honored at the next loop-top gate.

    ``max_iters``'s stop gate runs before the per-iteration cost-cap re-read
    block, so an arm-once-at-process-start cap would pin a live loop to its
    stale value however config is retuned (a loop stopped at its old cap
    while config reads higher with budget remaining). Here iteration 1's
    setup raises ``max_iters`` from 1 to 2: the next boundary must re-read
    the cap and run iteration 2 instead of closing the run at the stale 1.
    """
    node = _make_node(repo, 'itersup', detached=False, sync=False, max_iters=1)
    worktree = node['worktree']
    # iteration 1's setup raises the cap -- a mid-run retune, invisible to a
    # run-start-only cap read (setup runs after this iteration's caps armed)
    setup = node['node_dir'] / 'scripts' / 'setup.sh'
    setup.write_text(
        f'#!/usr/bin/env bash\nfractal config _set max_iters=2 --path="{worktree}"\n',
        encoding='utf-8',
    )
    calls, result = _run_loop(repo, node, capture_name='itersup')

    # the raised cap reached the loop-top gate: iteration 2 ran its two
    # steps and the run closed at the new cap, not the stale spawn-time one
    assert len(calls) == 4, (sorted(calls), result.stdout)
    assert 'Reached max iterations (2)' in result.stdout, result.stdout
    iters = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT COUNT(*) FROM iters WHERE node = 'main.itersup'",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert iters == '2', (iters, result.stdout)
    run = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || exit_code FROM runs ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert run == 'completed/0', (run, result.stdout)


def test_midrun_wait_retune_reaches_next_iterations_drains(repo: dict) -> None:
    """A mid-run ``wait`` retune governs the next iteration's drain cadence.

    ``wait`` paces the drain SYNCs (approval and child waits); boot reads it
    once, so without the iteration-top re-read a retune would never reach a
    live run's waits. Iteration 1's approval wait runs at the unset default:
    the first drain SYNC would land a full minute in, so none fires inside
    the measured margin. Iteration 1's setup retunes ``wait=1s``; iteration
    2's approval wait must pick that up at the iteration top and SYNC within
    the same margin.
    """
    work_marker = 'Approval work body'
    steps = {
        '01-work.md': f'---\nrequires_approval: true\n---\n# Work\n\n{work_marker}\n',
    }
    node = _make_node(
        repo=repo,
        name='waitretune',
        detached=False,
        sync=True,
        steps=steps,
        max_iters=2,
    )
    root = repo['root']
    worktree = node['worktree']
    # iteration 1's setup retunes the wait -- a mid-run edit, invisible to
    # the boot read (setup runs after this iteration's pacing knobs armed)
    setup = node['node_dir'] / 'scripts' / 'setup.sh'
    setup.write_text(
        f'#!/usr/bin/env bash\nfractal config _set wait=1s --path="{worktree}"\n',
        encoding='utf-8',
    )
    capture = root / 'capture_waitretune'
    if capture.exists():
        shutil.rmtree(capture)
    capture.mkdir()
    env = _cli_env(CAPTURE_DIR=f'{capture}', STUB_COST='0.001')
    bindir = repo['bindir']
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    proc = subprocess.Popen(
        _loop_cmd(worktree),
        cwd=f'{worktree}',
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        # iteration 1 waits at the boot default: no drain SYNC
        # fires inside the margin
        _await_capture(capture, work_marker, deadline=time.monotonic() + 60)
        time.sleep(3.0)
        calls = _collect_calls(capture)
        work_1 = min(n for n, c in calls.items() if work_marker in c['prompt'])
        boot_syncs = [
            n for n, c in calls.items() if n > work_1 and _SYNC_MARKER in c['prompt']
        ]
        assert not boot_syncs, sorted(calls)
        _approve_pending(root, worktree)

        # iteration 2 re-read the retuned wait: its approval wait drains SYNC
        # within the same margin
        def _second_work_call() -> bool:
            latest = _collect_calls(capture)
            work = [n for n, c in latest.items() if work_marker in c['prompt']]
            return len(work) >= 2

        reached = _await_progress(
            check=_second_work_call,
            progress=lambda: _capture_activity(capture),
            deadline=time.monotonic() + 60,
        )
        assert reached, sorted(_collect_calls(capture))
        time.sleep(3.0)
        calls = _collect_calls(capture)
        work_2 = max(n for n, c in calls.items() if work_marker in c['prompt'])
        wait_syncs = [
            n for n, c in calls.items() if n > work_2 and _SYNC_MARKER in c['prompt']
        ]
        assert wait_syncs, sorted(calls)
        _approve_pending(root, worktree)
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        _reap_group(proc)
    assert proc.returncode == 0, (stdout, stderr)


def test_precap_death_heals_registry_drift_on_exit(repo: dict) -> None:
    """A pre-boundary death still leaves the registry row healed, loudly.

    A cap retune reaches enforcement at the very next boundary probe, but
    a raise below the already-recorded spend rescues nothing: here
    iteration 1's setup lifts the $0.30 cap to $0.35 in config only while
    step 1 spends $0.40, so the subtree ceiling still ends the run before
    step 2 -- and before the iteration-top reconcile that would have
    healed config->registry ever runs. Without an exit-trap heal the dead
    row would keep the drift forever; the exit trap must leave the dead
    registry row reading config truth.
    """
    node = _make_node(
        repo=repo,
        name='healexit',
        detached=False,
        sync=False,
        max_iters=1,
        max_cost=0.3,
    )
    worktree = node['worktree']
    # iteration 1's setup lifts the cap in config only -- a rescue too small
    # to matter: the live probe reads it, but spend already exceeds it
    setup = node['node_dir'] / 'scripts' / 'setup.sh'
    setup.write_text(
        f'#!/usr/bin/env bash\nfractal config _set max_cost=0.35 --path="{worktree}"\n',
        encoding='utf-8',
    )
    calls, result = _run_loop(repo, node, capture_name='healexit', stub_cost='0.4')

    # the lift rescued nothing: step 1's $0.40 crossed the lifted $0.35 cap
    # and the ceiling stopped step 2 from launching
    assert len(calls) == 1, (sorted(calls), result.stdout)
    # the exit-trap heal left the dead row reading config truth, loudly
    assert '!= registry' in result.stdout, result.stdout
    assert 'max_cost=0.35' in result.stdout, result.stdout
    row_cap = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT max_cost FROM nodes WHERE node = 'main.healexit'",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert row_cap == '0.35', (row_cap, result.stdout)


def test_single_step_overshoot_ends_via_reserve_boundary(repo: dict) -> None:
    """An overshoot the mid-iteration ceiling can't see still ends the run, exited/0.

    The mid-iteration hard ceiling only runs for ``STEP_NUM > 1``, so a
    single-step iteration (or one whose last step crosses the cap) never trips
    it -- the boundary check must catch the overshoot. Because the boundary keys
    on subtree spend (not the CLI's clamped ``cost remaining``), a spend past
    ``max_cost`` reliably trips it. With one step costing $1.00 against a $0.50
    cap and ``--max-iters 2``: the step runs once, the mid-iteration ceiling
    never fires (no second step), and the reserve boundary ends the run
    ``exited``/0 rather than starting a second iteration.
    """
    node = _make_node(
        repo=repo,
        name='overshoot',
        detached=False,
        sync=False,
        steps={'01-only.md': '# Only\n\nSingle step.\n'},
        max_iters=2,
        max_cost=0.50,
    )
    worktree = node['worktree']
    calls, result = _run_loop(repo, node, capture_name='overshoot', stub_cost='1.0')

    # one step ran; the run ended at the boundary, not a second iteration
    assert len(calls) == 1, (calls, result.stdout)
    # ended via the reserve boundary -- the mid-iteration ceiling never saw it
    assert 'Total cost budget reserve reached' in result.stdout, result.stdout
    assert 'Subtree cost budget reached' not in result.stdout, result.stdout
    # a budget landing is exited/0
    assert _run(worktree, 'node', 'status').stdout.strip().startswith('exited'), (
        result.stdout
    )
    run = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || exit_code FROM runs ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert run == 'exited/0', (run, result.stdout)


def test_over_budget_winddown_step_is_skipped_not_run(repo: dict) -> None:
    """An over-budget step in the finish wind-down is skipped, not run uncapped.

    The subtree ceiling stops a node at its cap, but it is disarmed once finish
    is set -- so a wind-down step can reach ``run_step`` already over budget,
    where its ``--max-budget-usd`` leash is empty (remaining clamps to 0). An
    empty leash would launch the step *uncapped* at the worst moment, so the
    loop skips the launch instead. With the run over its ``--max-cost`` and
    finish set, the commit (last) step's agent is never invoked, the step is
    recorded ``stopped`` with an ``over budget`` reason, and the iteration is
    not failed -- the force-commit backstop still saves the work. The gate
    parks step 1 (whose $1.00 spend blows the $0.50 cap) so finish can be set
    mid-iteration; with no child the commit step is reached at once.
    """
    node = _make_node(
        repo=repo,
        name='obskip',
        detached=False,
        sync=False,
        steps=_GATE_STEPS,
        max_cost=0.50,
    )
    worktree = node['worktree']
    proc, capture, log = _launch_finish_wait(
        repo=repo,
        node=node,
        capture_name='ob_skip',
        stub_cost='1.0',
    )
    try:
        # park at the gate step (its $1.00 spend exceeds the $0.50 cap), then set
        # finish so the ceiling is disarmed and the commit step is reached; no
        # child, so the wind-down step runs immediately rather than waiting
        assert _await_gate(capture, deadline=time.monotonic() + 60), log.read_text()
        Node(worktree).record.signal_set('finish', 'done now')
        (capture / 'gate_release').touch()
    finally:
        calls, result = _finish_wait_result(proc, capture, log)

    # only the gate step ran; the over-budget commit step was skipped, not launched
    assert len(calls) == 1, (calls, result.stdout)
    commit_calls = [n for n, c in calls.items() if 'Commit step' in c['prompt']]
    assert not commit_calls, (calls, result.stdout)
    assert 'skipped (over budget)' in result.stdout, result.stdout
    # the skipped step is recorded a clean budget stop (stopped/over budget) ...
    step_row = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || COALESCE(metadata, '') FROM steps"
            " WHERE node = 'main.obskip' ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert step_row == 'stopped/over budget', (step_row, result.stdout)
    # ... and the iteration wound down cleanly (completed), not failed
    iter_status = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status FROM iters WHERE node = 'main.obskip'"
            ' ORDER BY rowid DESC LIMIT 1',
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert iter_status == 'completed', (iter_status, result.stdout)


def test_no_cost_cap_leaves_step_uncapped(repo: dict) -> None:
    """With no cost cap configured, steps run with no ``--max-budget-usd`` flag.

    The skip/cap machinery is reserved for a *configured* budget: a node with no
    ``--max-cost``/``--max-iter-cost``/``--max-step-cost`` passes no per-step leash
    at all, so both steps launch uncapped (an empty budget flag) -- the skip
    machinery must not turn "no cap configured" into a zero/trip-immediately cap.
    """
    node = _make_node(repo, 'nocap', detached=False, sync=False)
    calls, result = _run_loop(repo, node, capture_name='nocap')

    # both steps ran, neither carried a budget flag (genuinely uncapped)
    assert len(calls) == 2, (calls, result.stdout)
    assert calls[1]['budget'] == '', (calls, result.stdout)
    assert calls[2]['budget'] == '', (calls, result.stdout)


def test_reserve_window_caps_step_at_remaining(repo: dict) -> None:
    """In the reserve window the step is capped at the remaining, not skipped.

    The skip path must fire only when the budget is truly exhausted -- not in the
    reserve window, where a non-positive ``remaining - reserve`` is floored back up
    to the full remaining and handed to the step as its leash. Mirrors the
    reserve-boundary test: ``--max-cost 1.0``, a $0.70 reserve, $0.40/step. Step 2
    enters RESERVE ($0.60 remaining <= the reserve) yet must still *run*, capped at
    ~$0.60 -- never skipped and never zero -- proving the skip path does not
    swallow the floor.
    """
    node = _make_node(
        repo=repo,
        name='reservecap',
        detached=False,
        sync=False,
        max_iters=2,
        max_cost=1.0,
    )
    worktree = node['worktree']
    assert _run(worktree, 'config', '_set', 'reserve_budget=0.7').returncode == 0
    calls, result = _run_loop(repo, node, capture_name='reservecap', stub_cost='0.4')

    # both steps ran (the reserve steers, never skips); step 2 wound down in RESERVE
    # but was capped at the floored remaining (~$0.60), not skipped and not zero
    assert len(calls) == 2, (calls, result.stdout)
    assert 'skipped (over budget)' not in result.stdout, result.stdout
    assert _RESERVE_MARKER in calls[2]['prompt'], (calls, result.stdout)
    assert float(calls[2]['budget']) == pytest.approx(0.6, abs=0.01), (
        calls,
        result.stdout,
    )


def test_iter_cost_reserve_continues_next_iteration(repo: dict) -> None:
    """Per-iteration-cost reserve steers the iteration but does NOT end the run.

    The total-cost-vs-per-iter-cost split is the reserve contract's other half: a
    ``--max-iter-cost`` node (its ``--max-cost`` far out of reach) that hits its
    per-iteration cap is steered into RESERVE for the rest of that iteration,
    then continues with a fresh per-iter budget next iteration -- the boundary
    run-end is gated on ``--max-cost`` and must never fire here. With
    ``--max-iter-cost 0.3``, a $0.40/step agent over two steps, and
    ``--max-iters 2``: each iteration steers its second step (iter spend
    $0.40 >= $0.30) yet both iterations run in full, and the node finishes
    ``completed``/0.
    """
    node = _make_node(
        repo=repo,
        name='itercont',
        detached=False,
        sync=False,
        max_iters=2,
        max_cost=100.0,
    )
    worktree = node['worktree']
    # a per-iter cap under a distant total cap -- the boundary run-end keys on
    # max_cost and must stay far out of reach of the $1.60 total spend
    assert _run(worktree, 'config', '_set', 'max_iter_cost=0.3').returncode == 0
    calls, result = _run_loop(repo, node, capture_name='itercont', stub_cost='0.4')

    # per-iter reserve was entered (iteration 1's second step steered)...
    assert _RESERVE_MARKER in calls[2]['prompt'], (calls, result.stdout)
    # ...yet the run continued to a full second iteration (4 work steps total)
    assert len(calls) == 4, (calls, result.stdout)
    # the total-cost boundary never fired, and the node ran out its iterations
    assert 'Total cost budget reserve reached' not in result.stdout, result.stdout
    assert (
        _run(worktree, 'node', 'status')
        .stdout.strip()
        .startswith('completed (run exhausted:')
    ), result.stdout
    run = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || exit_code FROM runs ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert run == 'completed/0', (run, result.stdout)


def test_step_header_stays_self_consistent_across_midrun_retune(repo: dict) -> None:
    """A mid-run retune never splits the step header's budget figures.

    The step-context ``Cost budget`` line is rebuilt per step from the
    loop's cap globals -- the same set the boundary probes refresh live --
    so a retune landing mid-iteration must land in the header WHOLE:
    new-cap remaining beside new-cap labels at the very next step, and
    again at the next iteration. A remaining taken from one read beside
    labels stamped from another would let the figures split; deriving
    both from one atomically-refreshed read set keeps the header
    self-consistent on either side of the retune. The gate parks the loop
    mid-step-1 while the driver retunes both cost keys (``config _set``
    -- the loop's read contract is config): the same iteration's next
    header and the next iteration's headers must all carry the retuned
    figures, whole.
    """
    node = _make_node(
        repo=repo,
        name='hdrretune',
        detached=False,
        sync=False,
        steps=_GATE_STEPS,
        max_iters=2,
        max_cost=100.0,
    )
    worktree = node['worktree']
    assert _run(worktree, 'config', '_set', 'max_iter_cost=40.0').returncode == 0

    proc, capture, log = _launch_finish_wait(repo, node, capture_name='hdrretune')
    try:
        # park mid-step-1, retune both cost caps, then let the loop run out; the
        # release marker persists, so iteration 2's gate step sails through
        assert _await_gate(capture, deadline=time.monotonic() + 60), log.read_text()
        retune = _run(
            worktree,
            'config',
            '_set',
            'max_cost=150.0',
            'max_iter_cost=60.0',
        )
        assert retune.returncode == 0, retune.stderr
        (capture / 'gate_release').touch()
    finally:
        calls, result = _finish_wait_result(proc, capture, log)

    # one commit-step header per iteration, in call order
    prompts = [
        calls[n]['prompt'] for n in sorted(calls) if 'Commit step' in calls[n]['prompt']
    ]
    assert len(prompts) == 2, (sorted(calls), result.stdout)
    # same iteration: the retune lands whole at the very next step -- new-cap
    # remaining ($0.001 spent) beside new-cap labels, never a split header
    assert '$149.9990 remaining of $150.0 (max $60.0/iter)' in prompts[0], prompts[0]
    assert 'of $100.0' not in prompts[0], prompts[0]
    # next iteration: the same whole figures
    assert 'of $150.0' in prompts[1], prompts[1]
    assert '(max $60.0/iter)' in prompts[1], prompts[1]
    assert 'of $100.0' not in prompts[1], prompts[1]


def test_goal_finish_records_completed(repo: dict) -> None:
    """A goal-met finish (no budget abort) ends the run ``completed``/0.

    The terminal block keys the outcome on ``BUDGET_HIT``: a budget stop is
    ``exited``/1, but a plain finish signal with ``BUDGET_HIT`` false must record
    ``completed``/0 -- the side the ``BUDGET_HIT`` discriminator must not
    mislabel. With no ``--max-cost``, the node's finish signal is set mid-run
    (while a gated step parks, so it scopes to the active run) and the loop ends
    in iteration 1 -- before ``--max-iters 2`` -- recording ``completed``/0 via
    the finish branch, not the max-iters branch.
    """
    node = _make_node(
        repo=repo,
        name='goalfinish',
        detached=False,
        sync=False,
        steps=_GATE_STEPS,
        max_iters=2,
    )
    worktree = node['worktree']
    proc, capture, log = _launch_finish_wait(repo, node, capture_name='goalfinish')
    try:
        # once the gated step parks, the run is active -> finish scopes to it
        assert _await_gate(capture, deadline=time.monotonic() + 60), log.read_text()
        Node(worktree).record.signal_set('finish', 'goal met')
        (capture / 'gate_release').touch()
    finally:
        _, result = _finish_wait_result(proc, capture, log, timeout=30)

    # ended on the finish signal with no budget abort -> completed/0
    assert 'Total cost budget reserve reached' not in result.stdout, result.stdout
    assert _run(worktree, 'node', 'status').stdout.strip() == 'completed', result.stdout
    run = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || exit_code FROM runs ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert run == 'completed/0', (run, result.stdout)


def test_self_finish_over_cap_completes_with_overshoot(repo: dict) -> None:
    """A deliberate finish whose run crossed the cap ends ``completed``/0.

    The in-loop budget checks skip once a finish signal is set (they exist to
    send one), so a node that signals ``finish`` itself and then crosses
    ``--max-cost`` reaches the terminal block with ``BUDGET_HIT`` false. A
    deliberate (non-budget-stemmed) finish is a goal-met landing whatever the
    drain spent: with a $0.50 cap and a $1.00 gate step, finish is set while
    the step parks (the ceiling is disarmed from then on), the over-budget
    commit step is skipped (its own guard), and the run closes
    ``completed``/0 with the overshoot recorded on the run row -- the shape
    of ``test_goal_finish_records_completed`` with the cap crossed.
    """
    node = _make_node(
        repo=repo,
        name='overcapfinish',
        detached=False,
        sync=False,
        steps=_GATE_STEPS,
        max_iters=2,
        max_cost=0.50,
    )
    worktree = node['worktree']
    proc, capture, log = _launch_finish_wait(
        repo=repo,
        node=node,
        capture_name='overcapfinish',
        stub_cost='1.0',
    )
    try:
        # once the gated step parks, the run is active -> finish scopes to it
        assert _await_gate(capture, deadline=time.monotonic() + 60), log.read_text()
        Node(worktree).record.signal_set('finish', 'goal met')
        (capture / 'gate_release').touch()
    finally:
        calls, result = _finish_wait_result(proc, capture, log, timeout=30)

    # the finish signal disarmed the ceiling: only the gate step's agent ran
    # (the over-budget commit step is skipped), and no in-loop abort fired
    assert len(calls) == 1, (calls, result.stdout)
    assert 'Subtree cost budget reached' not in result.stdout, result.stdout
    # a deliberate finish keeps its goal-met landing; the overshoot rides
    # the run row instead of reclassifying the run
    assert _run(worktree, 'node', 'status').stdout.strip().startswith('completed'), (
        result.stdout
    )
    run = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || exit_code || '/' || metadata FROM runs"
            ' ORDER BY rowid DESC LIMIT 1',
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    overshoot = 'cost budget exceeded in finish wind-down (spent $1.0000 >= $0.5 max)'
    assert run == f'completed/0/{overshoot}', (run, result.stdout)


@pytest.mark.parametrize(
    argnames=('name', 'reason', 'node_status', 'run_row'),
    argvalues=[
        (
            'cascadebudget',
            'cost budget reserve reached (spent $5.00 >= $6.00 max'
            ' - $1.00 reserve) (via finish of main.fake)',
            'exited',
            'exited/0',
        ),
        (
            'cascadeplain',
            'wrap up the tree (via finish of main.fake)',
            'completed',
            'completed/0',
        ),
    ],
    ids=['budget-cascade', 'plain-cascade'],
)
def test_cascaded_budget_finish_records_exited(
    repo: dict,
    name: str,
    reason: str,
    node_status: str,
    run_row: str,
) -> None:
    """A budget-reason finish cascaded from an ancestor ends ``exited``/0.

    An ancestor's budget abort propagates ``finish`` to every active descendant,
    but ``BUDGET_HIT`` stays local to the tripping loop -- so without
    reclassification the killed descendant would close ``completed``/0 as if
    goal-met. The descendant's signal reason carries the budget prefix plus the
    ``(via finish of <branch>)`` attribution, so the terminal reclassifier
    flips exactly those runs to ``exited``/0 (and the exit notice fires) --
    while a propagated *non-budget* finish (a user finishing a whole tree)
    still closes ``completed``/0.
    """
    node = _make_node(
        repo=repo,
        name=name,
        detached=False,
        sync=False,
        steps=_GATE_STEPS,
        max_iters=2,
        max_cost=100.0,
    )
    worktree = node['worktree']
    proc, capture, log = _launch_finish_wait(repo, node, capture_name=name)
    try:
        # once the gated step parks, the run is active -> the signal scopes to it
        assert _await_gate(capture, deadline=time.monotonic() + 60), log.read_text()
        Node(worktree).record.signal_set('finish', reason)
        (capture / 'gate_release').touch()
    finally:
        _, result = _finish_wait_result(proc, capture, log, timeout=30)

    # the cap is never crossed, so only the cascade reclassifier decides status
    assert 'Subtree cost budget reached' not in result.stdout, result.stdout
    assert _run(worktree, 'node', 'status').stdout.strip().startswith(node_status), (
        result.stdout
    )
    run = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || exit_code FROM runs ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert run == run_row, (run, result.stdout)
    # a budget cascade screams on radio with the attributed reason; a plain
    # propagated finish stays quiet
    sent = _run(worktree, 'radio', 'sent').stdout
    if node_status == 'exited':
        assert 'run exited' in sent, (sent, result.stdout)
        assert 'via finish of main.fake' in sent, (sent, result.stdout)
    else:
        assert 'run exited' not in sent, (sent, result.stdout)


def test_codex_preflight_probe_aborts_on_model_rejection(repo: dict) -> None:
    """A cost-capped codex node aborts at launch if codex rejects its model.

    Codex spend is priced from token counts, so a cost cap needs an explicit
    ``--model`` -- but some codex accounts reject some explicit models (e.g. a
    ChatGPT-plan account lacking the entitlement) and would fail every step.
    The run-start probe catches this and aborts with the probe's own error
    before any step (or run) starts.
    """
    node = _make_node(
        repo=repo,
        name='codexprobe',
        detached=False,
        sync=False,
        max_cost=1.0,
        agent='codex',
    )
    # point the node model at the stub's rejection sentinel
    assert _run(node['worktree'], 'config', '_set', 'model=reject-me').returncode == 0
    calls, result = _run_loop(repo, node, capture_name='codexprobe')

    # aborted at launch with the clear message, and no step ever ran
    assert result.returncode != 0, result.stdout
    assert 'codex preflight failed for model' in result.stderr, result.stderr
    assert not calls, (calls, result.stdout)
    # codex's own error is surfaced verbatim, not a hedged guess: the probe relays
    # the agent's message (the stub stands in for the real account-rejection text)
    assert 'model not available for this account' in result.stderr, result.stderr


def test_codex_preflight_failure_is_loud_and_recoverable(repo: dict) -> None:
    """A failed codex preflight stamps a diagnosable, recoverable terminal.

    The probe runs before the ``active`` stamp, so a bare abort would strand
    the node at ``idle`` -- indistinguishable from a never-started node, with
    the diagnosis lost in the dying tmux pane. Instead the abort must
    (1) record *why* durably (a closed run row naming the failing model,
    surfaced by ``node activity``), (2) stamp the honest terminal ``exited``
    so the wedge is visible, and (3) leave a forward path: a plain
    ``node start`` refuses with a restart hint (no silent re-fail) while
    ``--continue`` is now accepted (``exited`` is continue-eligible).
    """
    node = _make_node(
        repo=repo,
        name='codexwedge',
        detached=False,
        sync=False,
        max_cost=1.0,
        agent='codex',
    )
    worktree = node['worktree']
    node_dir = node['node_dir']
    assert _run(worktree, 'config', '_set', 'model=reject-me').returncode == 0
    _, result = _run_loop(repo, node, capture_name='codexwedge')
    assert result.returncode != 0, result.stdout

    # (1) the reason is persisted on the closed run row, naming the failing model
    fail_reason = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT metadata FROM runs WHERE node = 'main.codexwedge'"
            ' ORDER BY rowid DESC LIMIT 1',
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert 'reject-me' in fail_reason, fail_reason
    # (2) the node is stamped exited -- not the idle a never-started node shows
    assert _run(worktree, 'node', 'status').stdout.strip().startswith('exited')

    # (3a) a plain start does not silently re-run the doomed preflight: it
    # refuses from the terminal status and points at --continue
    plain = _run(worktree, 'node', 'start')
    assert plain.returncode != 0, plain.stdout
    assert 'exited' in plain.stderr, plain.stderr
    assert 'continue' in plain.stderr, plain.stderr
    # (3b) --continue is accepted by the status guard -- plant a non-positive
    # max_cost straight in config.json (bypassing the _set guard; an unset cap
    # is a valid uncapped start, so the halt needs an explicitly non-positive
    # value) so the launch halts at the next node-level guard not tmux, proving
    # continue passed the status check rather than dead-ending at the 'Cannot
    # continue from status: idle' an un-stamped wedge would produce
    config_path = node_dir / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config['max_cost'] = -1
    config_path.write_text(json.dumps(config), encoding='utf-8')
    # commit the node's setup so the continue's worktree-restore guard (which
    # precedes the retune, and so the non-positive-cost guard) passes on a
    # clean tree -- letting the launch reach the intended cost guard rather
    # than halting first on incidental uncommitted dirt
    _git(worktree, 'add', '-A')
    _git(worktree, 'commit', '-m', 'commit wedge state')
    continued = _run(worktree, 'node', 'start', '--continue')
    assert 'Cannot continue from status' not in continued.stderr, continued.stderr
    assert 'positive max_cost' in continued.stderr, continued.stderr


def test_codex_preflight_runs_without_timeout_binary(repo: dict) -> None:
    """A cost-capped codex node launches even when ``timeout`` is absent.

    The codex model probe bounds itself with ``timeout`` only as a convenience --
    a cost cap alone never requires ``timeout`` (only wall-clock caps do). On a
    default macOS host (no coreutils) the probe must skip the wrapper and run
    codex directly, not abort 127 and misreport it as ``codex preflight failed``.
    """
    node = _make_node(
        repo=repo,
        name='codexnotimeout',
        detached=False,
        sync=False,
        max_cost=1.0,
        agent='codex',
    )
    worktree = node['worktree']
    # a real priced model: codex accepts it (not the reject sentinel) and the
    # per-step cost-cap pricing check passes, so the iteration runs to completion
    assert _run(worktree, 'config', '_set', 'model=gpt-4o-mini').returncode == 0
    capture = repo['root'] / 'capture_codexnotimeout'
    if capture.exists():
        shutil.rmtree(capture)
    capture.mkdir()
    env = _cli_env(CAPTURE_DIR=f'{capture}', STUB_COST='0')
    env['PATH'] = _path_without_timeout(repo['bindir'], repo['root'])
    result = _run_reaped(
        _loop_cmd(worktree),
        cwd=f'{worktree}',
        env=env,
        timeout=180,
    )
    calls = _collect_calls(capture)

    # launched and ran (no 127 misreported as a preflight failure); codex was probed
    assert 'codex preflight failed' not in result.stderr, result.stderr
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert any(c['agent'] == 'codex' for c in calls.values()), (calls, result.stderr)


def test_stop_during_approval_wait_records_iteration_stopped(repo: dict) -> None:
    """A stop during an approval wait ends the iteration stopped, not failed.

    The step is correctly recorded stopped, but a loop that forced
    ``EXIT_CODE=1`` after any approval interruption would log the iteration
    ``failed`` (plus a false "failed on <step>" commit). A stop/finish (not a
    timeout) must leave the iteration ``stopped``/``completed``.
    """
    steps = {'01-work.md': '---\nrequires_approval: true\n---\n# Work\n\nWORK-MARKER\n'}
    node = _make_node(
        repo=repo,
        name='approvalstop',
        detached=False,
        sync=False,
        steps=steps,
    )
    worktree = node['worktree']
    capture = repo['root'] / 'capture_approvalstop'
    if capture.exists():
        shutil.rmtree(capture)
    capture.mkdir()
    env = _cli_env(CAPTURE_DIR=f'{capture}', STUB_COST='0.001')
    bindir = repo['bindir']
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    proc = subprocess.Popen(
        _loop_cmd(worktree),
        cwd=f'{worktree}',
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        # wait for the step to run, then -- while the loop spins in the approval
        # wait -- stop it instead of approving
        _await_capture(capture, 'WORK-MARKER', deadline=time.monotonic() + 60)
        time.sleep(3.0)
        Node(worktree).record.signal_set('stop', 'manual stop')
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        _reap_group(proc)

    # the iteration recorded stopped (not failed): no false failure
    query = (
        "SELECT status FROM iters WHERE node = 'main.approvalstop'"
        ' ORDER BY iter_id DESC LIMIT 1'
    )
    out = _run(worktree, 'db', '_query', query, '--csv').stdout
    statuses = [row['status'] for row in csv.DictReader(io.StringIO(out))]
    assert statuses == ['stopped'], (statuses, stdout, stderr)


def test_run_exits_with_status_exited_on_timeout(repo: dict) -> None:
    """A timeout on the final iteration ends the run ``exited``/1, not ``completed``.

    A step that blocks past a short ``--timeout`` is killed (124) on the last
    allowed iteration. The run-end must record the abnormal outcome (``exited``,
    exit_code 1) -- the max-iters clause must never relabel a timed-out final
    iteration as ``completed``/0.
    """
    node = _make_node(
        repo=repo,
        name='tmoexit',
        detached=False,
        sync=False,
        steps=_GATE_STEPS,
        max_iters=1,
    )
    worktree = node['worktree']
    # short run budget; the gate step blocks and is never released, so
    # the timeout (not finish/stop) is what ends the single allowed iteration
    assert _run(worktree, 'config', '_set', 'timeout=4s').returncode == 0
    _, result = _run_loop(repo, node, capture_name='tmo_exit')

    # node and run row agree on the abnormal terminal
    assert _run(worktree, 'node', 'status').stdout.strip().startswith('exited'), (
        result.stdout
    )
    run = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || exit_code FROM runs ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert run == 'exited/1', (run, result.stdout)


def test_timeout_killed_step_records_partial_cost(repo: dict) -> None:
    """A timeout-killed claude step bills the usage it metered.

    The stub emits one usage-bearing assistant frame and then hangs with no
    result frame; the run timeout kills it mid-stream. The per-event flush
    must leave the metered spend on the step row -- an abnormal termination
    records what the harness saw, never $0 -- while the run still ends
    ``exited``/1. Pricing comes from a test-scoped ``HOME`` cache, so the
    accrual needs no network and no operator cache.
    """
    node = _make_node(
        repo=repo,
        name='tmocost',
        detached=False,
        sync=False,
        steps=_USAGE_HANG_STEPS,
        max_iters=1,
    )
    worktree = node['worktree']
    # short run timeout: the hanging step is killed mid-stream -- 8s so the
    # stub can start and flush its frame first even under full-suite load
    assert _run(worktree, 'config', '_set', 'timeout=8s').returncode == 0
    # a priced model wires the step model -> the stream driver -> the accrual
    assert _run(worktree, 'config', '_set', f'model={_STUB_MODEL}').returncode == 0
    home = repo['root'] / 'home_tmocost'
    (home / '.fractal').mkdir(parents=True, exist_ok=True)
    (home / '.fractal' / 'pricing.json').write_text(
        json.dumps(_STUB_PRICING),
        encoding='utf-8',
    )
    _, result = _run_loop(repo, node, capture_name='tmo_cost', home=home)

    # under full-suite load the 8s window can elapse before the stub even
    # starts; the flush marker separates that starvation (nothing metered --
    # nothing to record, skip) from the recording gap this test guards (fail)
    if not (repo['root'] / 'capture_tmo_cost' / 'usage_flushed').exists():
        pytest.skip('load starvation: the stub never flushed its usage frame')
    # the killed step carries the flushed partial cost, not $0
    cost = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT cost FROM steps WHERE status = 'exited' ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert float(cost) == pytest.approx(_STUB_USAGE_COST), (cost, result.stdout)
    # and the abnormal terminal is unchanged: run row exited/1
    run = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || exit_code FROM runs ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert run == 'exited/1', (run, result.stdout)


def test_timeout_pre_flush_kill_marks_step_unpriced(repo: dict) -> None:
    """A step killed before its first usage flush is marked unpriced.

    The stub streams only its session init frame and hangs; the run timeout
    kills it before any usage flush, so the step row has a session but a
    NULL cost -- the close must stamp the ``unpriced`` metadata marker so
    ledgers can tell "free step" from "unpriced step". The fully-untracked
    scope still reads ``untracked`` on ``cost spent`` with no disclosure
    note: every row is NULL there, so the count would only restate it.
    """
    node = _make_node(
        repo=repo,
        name='tmounpriced',
        detached=False,
        sync=False,
        steps=_INIT_HANG_STEPS,
        max_iters=1,
    )
    worktree = node['worktree']
    # short run timeout: the hanging step is killed before any usage frame
    assert _run(worktree, 'config', '_set', 'timeout=8s').returncode == 0
    _, result = _run_loop(repo, node, capture_name='tmo_unpriced')

    # under full-suite load the 8s window can elapse before the stub even
    # starts; the init marker separates that starvation from the unpriced
    # window this test guards (streamed, then killed with nothing metered)
    if not (repo['root'] / 'capture_tmo_unpriced' / 'init_hung').exists():
        pytest.skip('load starvation: the stub never streamed its init frame')
    # the killed streamed step carries the marker and stays NULL-cost
    marked = (
        _run(
            worktree,
            'db',
            '_query',
            'SELECT COUNT(*) FROM steps WHERE session IS NOT NULL'
            " AND cost IS NULL AND metadata LIKE '%unpriced%'",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert marked == '1', (marked, result.stdout)
    # a fully-untracked scope reads untracked, with no disclosure noise
    spent = _run(worktree, 'node', 'cost', 'spent')
    assert spent.stdout.strip() == 'untracked', spent.stdout
    assert 'unpriced' not in spent.stderr, spent.stderr


def test_step_timeout_frontmatter_kills_at_the_steps_own_ceiling(repo: dict) -> None:
    """A step's ``timeout:`` frontmatter enforces that step's own ceiling.

    No run/iter walls and no node-global ``step_timeout``: only the step's
    own frontmatter can end the hanging stub, so the kill landing at all is
    the override working. The effective ceiling is exported to the step's
    environment, the killed step records the timeout family, and the run
    ends exited.
    """
    steps = {
        '01-hang.md': (
            f'---\ntimeout: 2s\n---\n# Hang\n\nHanging step. {_INIT_HANG_MARKER}\n'
        ),
    }
    node = _make_node(repo, 'steptmo', detached=False, sync=False, steps=steps)
    worktree = node['worktree']
    calls, result = _run_loop(repo, node, capture_name='step_tmo')

    # the launch carried the step's own ceiling, not the (unset) node global
    assert calls[1]['step_timeout'] == '2', (calls, result.stdout)
    # the step was killed at that ceiling and recorded the timeout family
    assert 'timed out' in result.stdout, result.stdout
    step_row = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || COALESCE(metadata, '') FROM steps"
            " WHERE node = 'main.steptmo' ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    status, _, reason = step_row.partition('/')
    assert status == 'exited', (step_row, result.stdout)
    assert 'timed out' in reason, (step_row, result.stdout)
    run = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || exit_code FROM runs ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert run == 'exited/1', (run, result.stdout)


def test_midrun_step_timeout_retune_takes_effect_next_iteration(repo: dict) -> None:
    """A mid-run ``node update --step-timeout`` lands at the next iteration top.

    ``step_timeout`` read once at boot would leave a live run pinned to its
    spawn-time ceiling however the operator retunes it. Iteration 1's setup
    retunes 45s -> 7s through the supported ``node update`` path (issued from
    the parent); iteration 1's launches still export the boot ceiling, and
    iteration 2's must export the retuned one.
    """
    node = _make_node(repo, 'steptune', detached=False, sync=False, max_iters=2)
    root = repo['root']
    worktree = node['worktree']
    assert _run(worktree, 'config', '_set', 'step_timeout=45s').returncode == 0
    # iteration 1's setup retunes the ceiling -- a mid-run update, invisible
    # to the boot read (setup runs after this iteration's knobs armed)
    setup = node['node_dir'] / 'scripts' / 'setup.sh'
    setup.write_text(
        '#!/usr/bin/env bash\n'
        f'fractal node update main.steptune --step-timeout=7s --path="{root}"\n',
        encoding='utf-8',
    )
    calls, result = _run_loop(repo, node, capture_name='step_tune')

    # two iterations of two steps each ran; iteration 1's launches carried
    # the boot ceiling, iteration 2's the retuned one
    assert len(calls) == 4, (sorted(calls), result.stdout)
    ceilings = [calls[n]['step_timeout'] for n in sorted(calls)]
    assert ceilings == ['45', '45', '7', '7'], (ceilings, result.stdout)


def test_timeout_force_commit_bypasses_a_failing_hook(repo: dict) -> None:
    """The timeout backstop's force-commit saves work past a failing hook.

    Every backstop call site is ``|| true``, so a hook that can veto the force
    commit loses the work silently -- and a later ``--continue`` cleans it away
    (bypassing hooks here is deliberate). ``--force`` passes
    ``--no-verify``, so the last-resort save cannot be rejected: the gate step
    blocks past the timeout with dirty work in the tree and a hook rejecting
    every commit, and the work must still land.
    """
    node = _make_node(
        repo=repo,
        name='bkstop',
        detached=False,
        sync=False,
        steps=_GATE_STEPS,
        max_iters=1,
    )
    worktree = node['worktree']
    assert _run(worktree, 'config', '_set', 'timeout=4s').returncode == 0
    # work worth saving, and a hook rejecting every commit; hooks are shared
    # repo-wide (the module repo is reused), so drop the hook afterwards
    (worktree / 'rescue.txt').write_text('work worth saving\n', encoding='utf-8')
    hook = repo['root'] / '.git' / 'hooks' / 'pre-commit'
    hook.write_text('#!/usr/bin/env bash\nexit 1\n', encoding='utf-8')
    hook.chmod(0o755)
    try:
        _, result = _run_loop(repo, node, capture_name='hook_backstop')
    finally:
        hook.unlink()
    # the backstop landed the dirty work despite the hook: clean tree, the
    # rescued file committed at HEAD
    assert _git(worktree, 'status', '--porcelain').stdout.strip() == '', result.stdout
    saved = _git(worktree, 'show', 'HEAD:rescue.txt').stdout
    assert saved == 'work worth saving\n'


def test_failing_setup_records_iteration_failed_not_loop_brick(repo: dict) -> None:
    """A failing setup script fails the iteration cleanly -- it never bricks the loop.

    ``setup.sh`` is the mutable, agent-editable node copy, so a bad edit must be a
    clean iteration failure -- recorded ``failed`` with reason ``setup failed`` and
    its iter row closed -- rather than a bare ``set -e`` abort that strands the
    open iter row ``active`` until the next start reconciles it. The agent never
    runs (setup fails before any step), and the loop still reaches its own terminal
    cascade rather than dying mid-iteration.
    """
    node = _make_node(repo, 'setupfail', detached=False, sync=False)
    worktree = node['worktree']
    # the loop runs the node's setup script each iteration; a failing one must be
    # caught and recorded, not abort the loop before any step runs
    setup = node['node_dir'] / 'scripts' / 'setup.sh'
    setup.write_text('#!/usr/bin/env bash\nexit 1\n', encoding='utf-8')
    calls, result = _run_loop(repo, node, capture_name='setup_fail')

    # setup failed before any step, so no agent was invoked
    assert calls == {}, (calls, result.stdout)
    # the iteration is recorded failed with the honest reason -- and is closed
    # (a stranded, set -e-aborted iter would read 'active' with no end)
    iter_row = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || COALESCE(metadata, '')"
            " || '/' || (ended_at IS NOT NULL) FROM iters"
            " WHERE node = 'main.setupfail' ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert iter_row == 'failed/setup failed/1', (iter_row, result.stdout)


def test_setup_failure_cap_ends_run_exited(repo: dict) -> None:
    """Consecutive setup failures end the run ``exited``/1 at the cap, not max-iters.

    Uncapped, a deterministically broken ``setup.sh`` would crash-loop the
    whole remaining iteration budget in minutes and then close ``completed``/0
    "Reached max iterations" -- byte-identical to a healthy full run, with
    the setup error lost to the tmux tty and zero radio. The
    consecutive-failure cap (``_SETUP_FAIL_CAP``) ends the run with the
    honest reason, the last setup output survives in the node dir (and the
    iteration metadata carries its tail), and the exit notice makes the
    death legible from the parent's feed.
    """
    # the cap is a small sane number, well under the iteration budget below --
    # pins the policy so a fat-fingered constant (0, or a value >= max_iters
    # that never trips) is caught even though the rest derives from it
    assert 1 < _SETUP_FAIL_CAP < 6
    node = _make_node(repo, 'setupcap', detached=False, sync=False, max_iters=6)
    worktree = node['worktree']
    setup = node['node_dir'] / 'scripts' / 'setup.sh'
    setup.write_text(
        '#!/usr/bin/env bash\necho "boom: missing dep" >&2\nexit 1\n',
        encoding='utf-8',
    )
    calls, result = _run_loop(repo, node, capture_name='setup_cap')

    # setup failed before any step, so no agent ever ran
    assert calls == {}, (calls, result.stdout)
    # the cap ended the run after _SETUP_FAIL_CAP failed
    # iterations, not the max-iters 6
    iters = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT COUNT(*) FROM iters WHERE node = 'main.setupcap'"
            " AND status = 'failed'",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert iters == str(_SETUP_FAIL_CAP), (iters, result.stdout)
    # the run and node record the abort honestly, never a goal-met completion
    assert _run(worktree, 'node', 'status').stdout.strip().startswith('exited'), (
        result.stdout
    )
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
    assert run == f'exited/1/setup failed x{_SETUP_FAIL_CAP}', (run, result.stdout)
    # the last setup run's output survives durably in the node dir
    setup_log = node['node_dir'] / 'setup.log'
    assert setup_log.exists(), result.stdout
    assert 'boom: missing dep' in setup_log.read_text(encoding='utf-8')
    # the iteration metadata carries the output tail, not just the constant
    iter_row = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT COALESCE(metadata, '') FROM iters"
            " WHERE node = 'main.setupcap' ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert iter_row == 'setup failed: boom: missing dep', (iter_row, result.stdout)
    # the machinery screamed on radio (the agent never ran -- only it can)
    sent = _run(worktree, 'radio', 'sent').stdout
    assert 'run exited' in sent, (sent, result.stdout)
    assert f'setup failed x{_SETUP_FAIL_CAP}' in sent, (sent, result.stdout)


def test_boot_stands_down_when_a_retire_wins_the_boot_window(repo: dict) -> None:
    """A retire landing between ``start`` and the boot stamp survives it.

    ``retire`` re-reads status under the ``.worktrees`` flock, but the
    loop's ``active`` boot stamp lands after ``start``'s checks -- so a
    retire that wins that window is legal, and an unconditional stamp
    would silently clobber ``retired`` and leave a retired node running.
    The boot re-checks under the same flock: the run stands down with an
    honest closed run row, no agent ever launches, and ``retired``
    survives.
    """
    node = _make_node(repo, 'bootrace', detached=False, sync=False)
    worktree = node['worktree']
    # retire the idle node -- by the time the loop process boots, this is
    # exactly the interleaving where a retire won the boot window
    retired = _run(worktree, 'node', 'retire')
    assert retired.returncode == 0, retired.stderr
    calls, result = _run_loop(repo, node, capture_name='boot_race')
    # the loop stood down before any agent launch
    assert result.returncode == 1, (result.returncode, result.stdout)
    assert calls == {}, (calls, result.stdout)
    # retired survives the boot; the stood-down run records honestly
    assert _run(worktree, 'node', 'status').stdout.strip().startswith('retired'), (
        result.stdout
    )
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
    assert run == 'exited/1/retired before boot', (run, result.stdout)


def test_setup_log_is_git_invisible_after_run(repo: dict) -> None:
    """The teed ``setup.log`` never reaches git -- not as churn, not in commits.

    The loop tees every iteration's setup output to ``<node_dir>/setup.log``
    so the last run's errors survive the tmux tty, but unignored, the file
    would land untracked in every running node's worktree and the loop's
    ensure-commit backstop would sweep it into the ``auto`` commit as
    per-iteration churn. The shipped exclude template carries the
    node-dir-qualified pattern, so the log keeps existing for failure
    forensics while staying invisible to git.
    """
    node = _make_node(repo, 'setuplog', detached=False, sync=False)
    worktree = node['worktree']
    _, result = _run_loop(repo, node, capture_name='setup_log')

    # the tee target survives for forensics
    setup_log = node['node_dir'] / 'setup.log'
    assert setup_log.exists(), result.stdout
    # ...but git never sees it: no untracked churn left behind
    porcelain = _git(worktree, 'status', '--porcelain').stdout
    assert 'setup.log' not in porcelain, (porcelain, result.stdout)
    # ...and no commit the run created swept it in
    committed = _git(worktree, 'log', '--all', '--name-only', '--format=').stdout
    assert 'setup.log' not in committed, (committed, result.stdout)


def test_setup_runs_from_worktree_root_cwd(repo: dict) -> None:
    """``setup.sh`` runs from the worktree root regardless of the launch CWD.

    The loop inherits its CWD from whoever launched it (tmux gets no ``-c``;
    ``_run_script`` passes no ``cwd``; agent processes sit in their node dir),
    so with a natural worktree-relative setup line like
    ``python3 -m venv .venv`` an unpinned CWD would land the artifact wherever
    the launch happened to sit -- a child can burn whole iterations
    discovering its venv inside ``.fractal/<node>/``. The loop pins setup's
    CWD to the worktree root, making the launch directory irrelevant.
    """
    node = _make_node(repo, 'setupcwd', detached=False, sync=False)
    worktree = node['worktree']
    # a worktree-relative artifact: lands wherever setup's CWD points
    setup = node['node_dir'] / 'scripts' / 'setup.sh'
    setup.write_text(
        '#!/usr/bin/env bash\ntouch ./cwd_marker\n',
        encoding='utf-8',
    )
    # launch from the node dir -- the ambient CWD a real agent launch inherits
    _, result = _run_loop(repo, node, capture_name='setup_cwd', cwd=node['node_dir'])

    # the marker landed at the worktree root, not the launch CWD
    assert (worktree / 'cwd_marker').exists(), result.stdout
    assert not (node['node_dir'] / 'cwd_marker').exists(), result.stdout


def test_agent_runs_from_worktree_root_cwd(repo: dict) -> None:
    """Agent invocations run in the worktree; node settings ride --settings.

    The charter tells every node to work in the worktree, so a bare relative
    write must land in the deliverable tree -- never inside ``.fractal/``,
    where the merge's seed strip would silently drop it. The node's claude
    settings arrive via the ``--settings`` flag, and claude's config home is
    never overridden -- auth and session storage stay the user's own.
    """
    node = _make_node(repo, 'agentcwd', detached=False, sync=False)
    # launch from the node dir -- the ambient CWD must not leak into the agent
    calls, result = _run_loop(
        repo=repo,
        node=node,
        capture_name='agent_cwd',
        cwd=node['node_dir'],
    )
    assert calls, result.stdout
    worktree = node['worktree'].resolve()
    settings = node['node_dir'] / '.claude' / 'settings.json'
    for call in calls.values():
        assert pathlib.Path(call['cwd']).resolve() == worktree, result.stdout
        assert call['claude_config'] == '', result.stdout
        assert pathlib.Path(call['settings']) == settings, result.stdout


def test_run_completes_when_max_iters_reached(repo: dict) -> None:
    """Reaching ``max_iters`` (no timeout, no finish) ends the run ``completed``/0.

    The companion to the timeout case: an iteration that finishes its steps and
    hits the iteration cap is a clean, expected end, so the run records
    ``completed``/0 -- distinguishing "ran its budget" from "was cut off".
    """
    node = _make_node(repo, 'maxdone', detached=False, sync=False, max_iters=1)
    worktree = node['worktree']
    _, result = _run_loop(repo, node, capture_name='max_done')

    assert (
        _run(worktree, 'node', 'status')
        .stdout.strip()
        .startswith('completed (run exhausted:')
    ), result.stdout
    run = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || exit_code FROM runs ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert run == 'completed/0', (run, result.stdout)


def test_iter_failure_reason_names_missing_steps_not_agent(repo: dict) -> None:
    """A step-discovery failure records ``no step files``, not ``agent error``.

    With an empty ``steps/`` dir, ``discover_steps`` fails before any agent runs,
    so a generic ``agent error`` label in ``node activity`` would misattribute
    a setup failure to the agent. The recorded reason names the real cause (the
    same plumbing that, at the step level, distinguishes an agent error from a
    downstream stream-driver failure).
    """
    node = _make_node(repo, 'nosteps', detached=False, sync=False, steps={})
    worktree = node['worktree']
    _, result = _run_loop(repo, node, capture_name='no_steps')

    # the iteration failed for lack of steps -- and says so (no agent was invoked)
    iter_row = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || COALESCE(metadata, '') FROM iters"
            " WHERE node = 'main.nosteps' ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert iter_row == 'failed/no step files', (iter_row, result.stdout)


# ------ failed-launch diagnostics


def test_failed_step_stderr_snapshot_and_exit_coded_reason(repo: dict) -> None:
    """A failed step keeps its stderr durably and its reason carries the cause.

    ``<agent>.err`` is the per-invocation live capture -- the next launch
    truncates it, so a failed step's diagnosis would survive only until
    another agent fires. The launch copies the capture to a per-step
    snapshot under git-ignored ``tmp/err/`` (named ``<run>-<iter>-<step>``),
    and the step row's one-line reason carries the agent's exit code plus
    the stderr tail -- so ``node activity`` names the actual failure while
    the full blob stays out of metadata but on disk. The iteration rollup
    keeps the bare ``agent error`` label; the detail is step-level.
    """
    steps = {'01-boom.md': f'# Boom\n\nFailing step. {_FAIL_MARKER}\n'}
    node = _make_node(repo, 'errsnap', detached=False, sync=False, steps=steps)
    worktree = node['worktree']
    # single-attempt shape: the deterministic failure must not buy a retry
    # (whose default backoff would also stall the run)
    assert _run(worktree, 'config', '_set', 'step_retries=0').returncode == 0
    _, result = _run_loop(repo, node, capture_name='err_snap')

    # the snapshot carries the captured stderr under the run-iter-step name
    run_id = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT run_id FROM runs WHERE node = 'main.errsnap'"
            ' ORDER BY rowid DESC LIMIT 1',
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    snapshot = node['node_dir'] / 'tmp' / 'err' / f'{run_id}-1-boom.err'
    assert snapshot.exists(), result.stdout
    assert _FAIL_STDERR in snapshot.read_text(encoding='utf-8')
    # the exit-coded reason reaches node activity with the stderr tail -- the
    # cause is durable, not just on the (mortal) pane
    activity = _run(worktree, 'node', 'activity', '--csv').stdout
    assert f'agent error (exit 7): {_FAIL_STDERR}' in activity, (
        activity,
        result.stdout,
    )
    # the iteration rollup keeps the bare label; the detail is step-level
    iter_row = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || COALESCE(metadata, '') FROM iters"
            " WHERE node = 'main.errsnap' ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert iter_row == 'failed/agent error', (iter_row, result.stdout)


def test_stream_borne_failure_reason_carries_the_cause(repo: dict) -> None:
    """A stream-borne failure's step row names the cause; the iter stays short.

    An agent that reports its failure on the JSON stream and exits 0 (the
    codex shape) leaves a blank ``.err`` capture, so the stream exception is
    the only diagnosis. The step row's reason carries the one-line cause, the
    iteration rollup keeps the short ``agent error`` label, and the
    ``tmp/err/`` snapshot holds the full traceback.
    """
    steps = {'01-boom.md': f'# Boom\n\nStream-failing step. {_STREAM_ERROR_MARKER}\n'}
    node = _make_node(
        repo=repo,
        name='streamerr',
        detached=False,
        sync=False,
        steps=steps,
        agent='codex',
    )
    worktree = node['worktree']
    # single-attempt shape: the deterministic failure must not buy a retry
    assert _run(worktree, 'config', '_set', 'step_retries=0').returncode == 0
    _, result = _run_loop(repo, node, capture_name='stream_err')

    # the step row's reason leads with the label and carries the stream cause
    step_row = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT COALESCE(metadata, '') FROM steps"
            " WHERE node = 'main.streamerr' AND status = 'failed'"
            ' ORDER BY rowid DESC LIMIT 1',
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert step_row.startswith('agent error: '), (step_row, result.stdout)
    assert _STREAM_ERROR_DETAIL in step_row, step_row
    # the iteration rollup keeps the bare label; the detail is step-level
    iter_row = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || COALESCE(metadata, '') FROM iters"
            " WHERE node = 'main.streamerr' ORDER BY rowid DESC LIMIT 1",
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert iter_row == 'failed/agent error', (iter_row, result.stdout)
    # the snapshot holds the full traceback -- the parser-side diagnosis a
    # blank .err capture cannot carry
    run_id = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT run_id FROM runs WHERE node = 'main.streamerr'"
            ' ORDER BY rowid DESC LIMIT 1',
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    snapshot = node['node_dir'] / 'tmp' / 'err' / f'{run_id}-1-boom.err'
    assert snapshot.exists(), result.stdout
    assert 'AgentStreamError' in snapshot.read_text(encoding='utf-8')


def test_cost_cap_guard_failure_reason_is_durable(repo: dict) -> None:
    """A pre-launch guard failure's step row names the guard, not the agent.

    A cost cap with no priceable model fails every step before any agent
    launches -- a config error, not an agent one. The step row's reason
    carries the guard's own message (the same line the pane prints), so
    ``node activity`` names the real cause instead of a bare ``agent error``
    that would misattribute a launch that never happened.
    """
    steps = {'01-alpha.md': '# Alpha\n\nFirst step.\n'}
    node = _make_node(
        repo=repo,
        name='capnomodel',
        detached=False,
        sync=False,
        steps=steps,
        agent='codex',
        max_cost=5.0,
    )
    worktree = node['worktree']
    # single-attempt shape: the deterministic failure must not buy a retry
    assert _run(worktree, 'config', '_set', 'step_retries=0').returncode == 0
    _, result = _run_loop(repo, node, capture_name='cap_no_model')

    # the step row names the guard's cause, never the bare agent-error label
    step_row = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT COALESCE(metadata, '') FROM steps"
            " WHERE node = 'main.capnomodel' AND status = 'failed'"
            ' ORDER BY rowid DESC LIMIT 1',
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    assert 'cost cap requires a model' in step_row, (step_row, result.stdout)
    assert not step_row.startswith('agent error'), step_row


def test_drain_wait_sync_failure_keeps_completed_with_note(repo: dict) -> None:
    """A failed drain-wait SYNC stays ``completed``/0 with metadata naming it.

    Waiting SYNCs are best-effort, so a failure there must not read as a
    work failure -- the row keeps its lenient ``completed``/0 close. But
    silently swallowing it would hide a rotting node (e.g. expired
    credentials failing every wait SYNC), so the row's metadata records
    ``sync failed (exit N)`` -- and the stderr snapshot lands under
    ``tmp/err/`` like any failed launch.
    """
    node = _make_node(repo, 'fwsyncfail', detached=False, sync=True, steps=_GATE_STEPS)
    worktree = node['worktree']
    # a 1s wait keeps the drain-SYNC cadence inside the wait window below --
    # at the default the first wait SYNC would land a full minute in
    assert _run(worktree, 'config', '_set', 'wait=1s').returncode == 0
    child = _register_active_child(repo, node, 'kid')

    proc, capture, log = _launch_finish_wait(repo, node, capture_name='fw_sync_fail')
    try:
        # park, arm the stub's SYNC-failure mode (every later SYNC
        # launch now fails), then finish + release so the loop
        # enters the wait with the child active
        assert _await_gate(capture, deadline=time.monotonic() + 60), log.read_text()
        (capture / 'fail_syncs').touch()
        Node(worktree).record.signal_set('finish', 'done now')
        (capture / 'gate_release').touch()
        assert _await_log(log, _WAIT_BANNER, deadline=time.monotonic() + 30), (
            log.read_text()
        )
        # let the wait spin several 1s cadences (the wait=1s pin above) so a
        # wait SYNC has fired -- and failed -- before the child drains
        time.sleep(3)
        Node(child).status_set('completed')
    finally:
        calls, result = _finish_wait_result(proc, capture, log)

    # every drain-wait SYNC row (step 0) keeps the lenient completed/0 close,
    # with the failure named in its metadata
    rows = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT status || '/' || exit_code || '/' || COALESCE(metadata, '')"
            " FROM steps WHERE node = 'main.fwsyncfail' AND step_name = 'SYNC'"
            ' AND step = 0',
            '--csv',
        )
        .stdout.strip()
        .splitlines()[1:]
    )
    assert rows, (calls, result.stdout)
    assert all(row == 'completed/0/sync failed (exit 7)' for row in rows), rows
    # the failed SYNC's stderr snapshot lands like any failed launch's
    run_id = (
        _run(
            worktree,
            'db',
            '_query',
            "SELECT run_id FROM runs WHERE node = 'main.fwsyncfail'"
            ' ORDER BY rowid DESC LIMIT 1',
            '--csv',
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    snapshot = node['node_dir'] / 'tmp' / 'err' / f'{run_id}-1-SYNC.err'
    assert snapshot.exists(), result.stdout
    assert _FAIL_STDERR in snapshot.read_text(encoding='utf-8')
    # the best-effort failure never leaks into the run's own outcome
    assert _run(worktree, 'node', 'status').stdout.strip() == 'completed', result.stdout


# ------ teardown process-group hygiene


def _chain_pids(pid: int) -> list[int]:
    """The transitive descendants of ``pid``, from one ``ps`` snapshot."""
    result = subprocess.run(
        ['ps', '-axo', 'pid=,ppid='],
        capture_output=True,
        text=True,
        check=True,
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


def _alive_pids(pids: list[int]) -> list[int]:
    """The subset of ``pids`` still running (zombies count as dead)."""
    if not pids:
        return []
    result = subprocess.run(
        ['ps', '-o', 'pid=,stat=', '-p', ','.join(str(p) for p in pids)],
        capture_output=True,
        text=True,
    )
    out = result.stdout
    alive = []
    for line in out.splitlines():
        pid, stat = line.split(None, 1)
        if not stat.startswith('Z'):
            alive.append(int(pid))
    return alive


def test_gate_teardown_reaps_full_process_group(repo: dict) -> None:
    """A launch torn down mid-gate leaves no agent-chain survivors.

    The gate parks a live chain under the launch (the loop -> the stub agent
    spinning on ``gate_release``); tearing the launch down at
    that point -- what every fixture teardown does when its test ends or the
    pytest session unwinds mid-gate -- must reap the FULL process group. A
    pid-only kill of the direct bash child leaves the rest of the chain
    reparented and alive -- a leaked chain can outlive its pytest session
    by days.
    """
    node = _make_node(repo, 'leakgate', detached=False, sync=False, steps=_GATE_STEPS)
    proc, capture, log = _launch_finish_wait(repo, node, capture_name='leak_gate')
    chain: list[int] = []
    try:
        assert _await_gate(capture, deadline=time.monotonic() + 60), (
            log.read_text(encoding='utf-8') if log.exists() else ''
        )
        # snapshot the parked chain (at least the stub agent) so the
        # survivor scan below cannot pass vacuously
        chain = _chain_pids(proc.pid)
        assert len(chain) >= 1, chain
    finally:
        # the teardown under test: the gate never releases, so the short wait
        # deterministically takes the reap path
        _finish_wait_result(proc, capture, log, timeout=2)

    # kill delivery is asynchronous: idle-wait on the shrinking survivor set
    _await_progress(
        check=lambda: not _alive_pids(chain),
        progress=lambda: tuple(_alive_pids(chain)),
        deadline=time.monotonic() + 15,
    )
    survivors = _alive_pids(chain)
    # reap survivors and release the gate before asserting -- a red run must
    # not itself leak the chain it just proved leaked
    for pid in survivors:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    (capture / 'gate_release').touch()
    assert survivors == [], (survivors, chain)


# ------ helpers


def _make_node(
    repo: dict,
    name: str,
    *,
    detached: bool,
    sync: bool,
    steps: Optional[dict] = None,
    commit: bool = False,
    max_iters: int = 1,
    max_cost: Optional[float] = None,
    agent: str = 'claude',
) -> dict[str, Any]:
    """Init a fresh worker wired for a deterministic launch.

    Inits a worker (``--agent`` default ``claude``, ``--local``, ``--max-iters``
    (default 1), and an optional ``--max-cost``) in the requested detached/sync
    mode and replaces the seed steps with ``steps`` (default two trivial steps)
    -- the launch runs this worktree's loop entry directly (see ``_loop_cmd``),
    so the node's own scripts dir is irrelevant. When ``commit`` is set, commits the
    worktree so a ``--continue`` clean/checkout preserves it.
    """
    root = repo['root']
    args = [
        'node',
        'init',
        name,
        '--agent',
        agent,
        '--max-iters',
        str(max_iters),
        '--sync' if sync else '--no-sync',
        '--local',
        *(['--detached'] if detached else []),
    ]
    if max_cost is not None:
        args.extend(['--max-cost', str(max_cost)])
    init = _run(root, *args)
    assert init.returncode == 0, init.stderr
    worktree = root / '.worktrees' / f'main.{name}'
    node_dir = worktree / '.fractal' / f'main.{name}'
    # replace the seed steps with the requested minimal sequence
    steps_dir = node_dir / 'steps'
    for step in steps_dir.glob('*.md'):
        step.unlink()
    for filename, content in (steps if steps is not None else _TWO_STEPS).items():
        (steps_dir / filename).write_text(content, encoding='utf-8')
    # the loop runs from the package (see _loop_cmd), not a per-node copy
    if commit:
        _git(worktree, 'add', '-A')
        _git(worktree, 'commit', '-m', f'setup {name}')
    return {'worktree': worktree, 'node_dir': node_dir}


def _collect_calls(capture: pathlib.Path) -> dict[int, dict[str, str]]:
    """Reconstruct each call as ``{'agent', 'prompt', 'session', ...}``.

    The stub writes an ``agent_N``/``cwd_N``/``claude_config_N``/``settings_N``/
    ``step_timeout_N``/``prompt_N``/``session_N`` record per agent invocation
    (claude also records ``budget_N``, the ``--max-budget-usd`` value), numbered
    by a shared counter, so the keys are the launch's call order (call 1 =
    first invocation).
    """
    calls = {}
    for agent_file in capture.glob('agent_*.txt'):
        num = int(agent_file.stem.removeprefix('agent_'))
        cwd_file = capture / f'cwd_{num}.txt'
        claude_config_file = capture / f'claude_config_{num}.txt'
        settings_file = capture / f'settings_{num}.txt'
        step_timeout_file = capture / f'step_timeout_{num}.txt'
        prompt_file = capture / f'prompt_{num}.txt'
        session_file = capture / f'session_{num}.txt'
        budget_file = capture / f'budget_{num}.txt'
        calls[num] = {
            'agent': agent_file.read_text(encoding='utf-8').strip(),
            'cwd': cwd_file.read_text(encoding='utf-8').strip()
            if cwd_file.exists()
            else '',
            'claude_config': claude_config_file.read_text(encoding='utf-8').strip()
            if claude_config_file.exists()
            else '',
            'settings': settings_file.read_text(encoding='utf-8').strip()
            if settings_file.exists()
            else '',
            'step_timeout': step_timeout_file.read_text(encoding='utf-8').strip()
            if step_timeout_file.exists()
            else '',
            'prompt': prompt_file.read_text(encoding='utf-8')
            if prompt_file.exists()
            else '',
            'session': session_file.read_text(encoding='utf-8').strip()
            if session_file.exists()
            else '',
            'budget': budget_file.read_text(encoding='utf-8').strip()
            if budget_file.exists()
            else '',
        }
    return calls


def _run_loop(
    repo: dict,
    node: dict,
    *,
    capture_name: str,
    continue_: bool = False,
    stub_cost: str = '0.001',
    home: Optional[pathlib.Path] = None,
    cwd: Optional[pathlib.Path] = None,
) -> tuple[dict, subprocess.CompletedProcess]:
    """Run one loop launch; return ``({call_num: {...}}, process)``.

    Each call's record is ``{'agent', 'prompt', 'session', 'budget'}``. A fresh
    capture dir per launch restarts the stub's counter at 1, so call 1 is this
    launch's first agent invocation. The launch is ``timeout``-bounded so a hang
    self-terminates.
    """
    root = repo['root']
    worktree = node['worktree']
    capture = root / f'capture_{capture_name}'
    if capture.exists():
        shutil.rmtree(capture)
    capture.mkdir()
    # stub agents shadow PATH; the loop's own fractal calls resolve to this
    # worktree (PYTHONPATH via _cli_env); CAPTURE_DIR/STUB_COST steer the stub
    env = _cli_env(CAPTURE_DIR=f'{capture}', STUB_COST=stub_cost)
    bindir = repo['bindir']
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    # a caller-scoped HOME redirects ~-anchored reads (e.g. the pricing cache)
    if home is not None:
        env['HOME'] = f'{home}'
    cmd = _loop_cmd(worktree)
    if continue_:
        cmd.append('--continue')
    # cwd overrides the launch directory (real launches inherit an ambient CWD)
    result = _run_reaped(cmd, cwd=f'{cwd or worktree}', env=env, timeout=180)
    return _collect_calls(capture), result


def _resume_loop(
    repo: dict,
    node: dict,
    *,
    capture_name: str,
) -> tuple[dict, subprocess.CompletedProcess]:
    """Relaunch the loop with ``--resume``; return ``({call_num: {...}}, process)``.

    The resume relaunch of a paused launch (the ``resume.sh`` path minus
    tmux, like every launch in this suite): the existing capture dir --
    and its call counter -- carries over, so the adopted run's calls
    continue the paused launch's numbering.
    """
    root = repo['root']
    worktree = node['worktree']
    capture = root / f'capture_{capture_name}'
    env = _cli_env(CAPTURE_DIR=f'{capture}', STUB_COST='0.001')
    bindir = repo['bindir']
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    cmd = _loop_cmd(worktree, '--resume')
    result = _run_reaped(cmd, cwd=f'{worktree}', env=env, timeout=180)
    return _collect_calls(capture), result


def _path_without_timeout(bindir: pathlib.Path, tmp: pathlib.Path) -> str:
    """A ``PATH`` resolving every tool except ``timeout`` (a default-macOS host).

    macOS ships no ``timeout``; a host without coreutils has none on ``PATH``.
    Reproduce that without disturbing the real env: for each ``PATH`` dir that
    holds an executable ``timeout``/``gtimeout``, substitute a sandbox mirror that
    symlinks its other entries, so ``command -v timeout`` fails while ``git``/etc.
    in that same dir still resolve. ``bindir`` (the stub agents) goes first.
    """
    sandbox = tmp / 'no_timeout_bin'
    sandbox.mkdir(parents=True, exist_ok=True)
    out_dirs = [str(bindir)]
    for entry in os.environ['PATH'].split(os.pathsep):
        directory = pathlib.Path(entry)
        holds_timeout = any(
            (directory / name).is_file() for name in ('timeout', 'gtimeout')
        )
        if not entry or holds_timeout:
            # mirror the dir minus the timeout binaries (skip an empty entry too)
            if entry:
                for tool in directory.iterdir():
                    if tool.name in ('timeout', 'gtimeout'):
                        continue
                    link = sandbox / tool.name
                    if not link.exists():
                        link.symlink_to(tool)
            continue
        out_dirs.append(entry)
    out_dirs.append(str(sandbox))
    return os.pathsep.join(out_dirs)


def _capture_activity(capture: pathlib.Path) -> object:
    """Fingerprint a capture dir's contents (names and sizes) as a progress signal."""
    if not capture.exists():
        return None
    try:
        return sorted((entry.name, entry.stat().st_size) for entry in capture.iterdir())
    except OSError:  # the stub adds and removes markers concurrently with the scan
        return None


def _await_capture(capture: pathlib.Path, marker: str, *, deadline: float) -> None:
    """Block until a captured prompt contains ``marker`` (or the wait idles out).

    Idle-based via ``_await_progress``: capture-dir activity refreshes the
    allowance.
    """
    _await_progress(
        check=lambda: any(
            marker in prompt_file.read_text(encoding='utf-8')
            for prompt_file in capture.glob('prompt_*.txt')
        ),
        progress=lambda: _capture_activity(capture),
        deadline=deadline,
    )


def _approve_pending(root: pathlib.Path, worktree: pathlib.Path) -> None:
    """Approve every step on ``worktree`` awaiting approval (parent-only)."""
    branch = worktree.name
    sql = "SELECT step_id FROM steps WHERE approved = ''"
    out = _run(worktree, 'db', '_query', sql, '--csv').stdout
    for row in csv.DictReader(io.StringIO(out)):
        _run(root, 'node', 'approve', branch, row['step_id'])


def _run_loop_with_approval(
    repo: dict,
    node: dict,
    *,
    capture_name: str,
    work_marker: str,
    approval_margin: float = 3.0,
) -> tuple[dict, subprocess.CompletedProcess]:
    """Run a launch whose single step requires approval, approving it externally.

    A ``requires_approval`` step makes the loop block after the step until the
    step is approved, periodically running ``modes/SYNC.md`` so the node can
    communicate. To observe that wait-loop SYNC without coupling approval to it,
    this drives the launch via ``Popen`` and approves only **after** the work
    step's agent call has fired (its ``work_marker`` prompt appears in the capture
    dir) plus ``approval_margin`` seconds -- a window several poll intervals wide,
    so any wait-loop SYNC the loop runs has fired before approval releases it.
    ``node approve`` is parent-only, so the approval is issued from the repo root
    (branch ``main``, the worker's parent). Returns the ``_run_loop`` shape.
    """
    root = repo['root']
    worktree = node['worktree']
    capture = root / f'capture_{capture_name}'
    if capture.exists():
        shutil.rmtree(capture)
    capture.mkdir()
    env = _cli_env(CAPTURE_DIR=f'{capture}', STUB_COST='0.001')
    bindir = repo['bindir']
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    cmd = _loop_cmd(worktree)
    proc = subprocess.Popen(
        cmd,
        cwd=f'{worktree}',
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        # wait for the work step's call to fire, then a margin (several poll
        # intervals) so the loop is spinning in the approval wait, then approve
        _await_capture(capture, work_marker, deadline=time.monotonic() + 60)
        time.sleep(approval_margin)
        _approve_pending(root, worktree)
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        _reap_group(proc)
    result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    return _collect_calls(capture), result


def _register_active_child(repo: dict, parent: dict, name: str) -> pathlib.Path:
    """Init a child under ``parent`` and make it count as an active descendant.

    Inits the child via the ``_NODE`` trick (``_NODE`` set to the parent's node
    dir, so ``node init`` nests it under the parent, not the repo-root user node),
    then opens a run, flips its status to ``active``, and starts a real tmux
    session for it -- the three conditions ``node list --status=active --live
    --count`` checks (``--live`` relabels an active node with no live session to
    exited, so the session must exist), so the parent's ``descendants_active``
    sees a live, active child. The session is reaped by ``_kill_repo_sessions``.
    Skips the test when tmux is unavailable (the live drain check needs it).
    Returns the child worktree.
    """
    _require_tmux()
    root = repo['root']
    node_dir = parent['node_dir']
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
        _NODE=f'{node_dir}',
    )
    assert init.returncode == 0, init.stderr
    parent_name = parent['worktree'].name
    child = root / '.worktrees' / f'{parent_name}.{name}'
    # a run so signal/status resolution has one, then active status
    Node(child).record.run_start()
    Node(child).status_set('active')
    # a live tmux session, so the authoritative --live drain check reads the
    # child active (the session name start.sh derives: <repo dirname> (<branch,
    # dots dashed>)); recorded for the _kill_repo_sessions teardown to reap
    child_name = child.name.replace('.', '-')
    session = f'{root.name} ({child_name})'
    # a killed suite run leaves this session behind (the teardown never ran)
    # and tmux refuses the duplicate name -- reap any stale twin first
    subprocess.run(['tmux', 'kill-session', '-t', f'={session}'], capture_output=True)
    subprocess.run(['tmux', 'new-session', '-d', '-s', session], check=True)
    repo.setdefault('sessions', []).append(session)
    return child


def _await_log(log: pathlib.Path, marker: str, *, deadline: float) -> bool:
    """Block until ``log`` contains ``marker`` (or the wait idles out).

    The finish-wait launches tee the loop's output to a file so a banner can be
    observed *mid-run* (the wait blocks the loop, so its stdout is not yet
    drainable from a finished process). Idle-based via ``_await_progress``: log
    growth refreshes the allowance. Returns whether the marker appeared.
    """
    return _await_progress(
        check=lambda: log.exists() and marker in log.read_text(encoding='utf-8'),
        progress=lambda: log.stat().st_size if log.exists() else None,
        deadline=deadline,
    )


def _await_gate(capture: pathlib.Path, *, deadline: float) -> bool:
    """Block until the gated step parks (its ``gate_ready`` marker appears).

    Idle-based via ``_await_progress``: capture-dir activity refreshes the
    allowance.
    """
    return _await_progress(
        check=lambda: (capture / 'gate_ready').exists(),
        progress=lambda: _capture_activity(capture),
        deadline=deadline,
    )


def _launch_finish_wait(
    repo: dict,
    node: dict,
    *,
    capture_name: str,
    stub_cost: str = '0.001',
) -> tuple[subprocess.Popen, pathlib.Path, pathlib.Path]:
    """Start the loop for a gated finish-wait; return ``(proc, capture, log)``.

    Drives the loop via ``Popen`` (the finish-wait scenarios arrange conditions
    while the gated step blocks the loop) with output teed to ``log`` so the wait
    banners are observable mid-run. The caller releases the gate
    (``capture/gate_release``) and finalizes with ``_finish_wait_result``.
    """
    root = repo['root']
    worktree = node['worktree']
    capture = root / f'capture_{capture_name}'
    if capture.exists():
        shutil.rmtree(capture)
    capture.mkdir()
    log = root / f'log_{capture_name}.txt'
    env = _cli_env(CAPTURE_DIR=f'{capture}', STUB_COST=stub_cost)
    bindir = repo['bindir']
    path = env['PATH']
    env['PATH'] = f'{bindir}{os.pathsep}{path}'
    cmd = _loop_cmd(worktree)
    # tee combined output to a file (not a PIPE): the wait blocks the loop, so the
    # log must be readable mid-run; the parent closes its handle right after spawn
    # (the child keeps the dup'd fd) so nothing has to be drained to keep it alive
    with open(log, 'w', encoding='utf-8') as handle:
        proc = subprocess.Popen(
            cmd,
            cwd=f'{worktree}',
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )
    return proc, capture, log


def _finish_wait_result(
    proc: subprocess.Popen,
    capture: pathlib.Path,
    log: pathlib.Path,
    *,
    timeout: float = 60,
) -> tuple[dict, subprocess.CompletedProcess]:
    """Reap a ``_launch_finish_wait`` process; return the ``_run_loop`` shape.

    Kills the launch if it overran (a wait that never released would otherwise
    hang the test) so a regression surfaces as a failed assertion, not a stuck
    suite -- and sweeps the launch's whole process group either way, so a
    straggler from the chain never outlives the test. The teed log stands in
    for the process's combined stdout/stderr.
    """
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    _reap_group(proc)
    output = log.read_text(encoding='utf-8')
    result = subprocess.CompletedProcess(proc.args, proc.returncode, output, output)
    return _collect_calls(capture), result
