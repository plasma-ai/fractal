"""Test the ``fractal.core.loop`` module.

The in-process doubles tier of the loop suite: ``MockLoop`` replaces
the agent launch with canned :class:`StepResult` outcomes (never a
subprocess), so budget policy, step discovery, the terminal cascade, and
the on_iteration/on_step hook pairings are exercised against real rows
in a real repo. The process tier -- the full ``fractal node _loop``
launches with stubbed agents -- lives in ``test_cli/test_run_modes.py``.
"""

from __future__ import annotations

import json
import logging
import pathlib
import sqlite3
import subprocess
import time
from typing import Any, Optional

import pytest

from fractal.constants import SOCKET_FILE
from fractal.core import pricing
from fractal.core.event import Event
from fractal.core.loop import Loop, Step, StepResult, _models_match
from fractal.core.node import Node
from fractal.exceptions import _Abort
from fractal.impl.claude import ClaudeAgent
from tests._helpers import _age_run, _past_timestamp

from ._agents import SampleAgent
from .conftest import _record_step_cost, _spawn_parent_child

__all__ = [
    'test_step_load_parses_strict_flat_scalar_frontmatter',
    'test_duration_validation_mirrors_the_launch_contract',
    'test_step_timeout_frontmatter_substitutes_at_launch',
    'test_malformed_midrun_retune_warns_and_keeps_the_previous_value',
    'test_provider_frontmatter_rebinds_the_boot_agent',
    'test_agent_env_publishes_node_branch',
    'test_boot_records_the_tmux_socket_for_the_reconcile_probe',
    'test_continue_restore_lands_config_all_or_nothing',
    'test_continue_cleanup_excludes_runtime_dirt',
    'test_stream_fault_attributes_to_the_stream_side',
    'test_agent_stderr_tolerates_non_utf8_output',
    'test_agent_launch_failure_books_a_failed_step',
    'test_setup_tolerates_non_utf8_output',
    'test_unsupported_provider_frontmatter_refuses_the_step',
    'test_discover_steps_orders_and_validates_prefixes',
    'test_preflight_aborts_on_a_non_utf8_step_file',
    'test_park_if_latched_walks_ancestors_with_resume_exemption',
    'test_step_budget_math_binds_the_tightest_cap',
    'test_run_spent_counts_recorded_cost_only',
    'test_budget_skipped_sync_books_stopped',
    'test_budget_skipped_steps_record_knowable_zero',
    'test_run_remaining_floors_at_zero_without_a_last_reading',
    'test_soft_cap_warning_fires_once_for_unbraked_caps',
    'test_step_budget_reserve_window_floors_at_remaining',
    'test_boundary_checks_read_live_caps',
    'test_untracked_spend_under_caps_warns_once',
    'test_failed_cost_reads_hold_the_last_good_reading',
    'test_cap_gate_demands_a_priced_model_from_tracking_gaps',
    'test_pending_finish_winds_down_in_reserve_for_budget_cascades',
    'test_pending_finish_between_iterations_starts_none',
    'test_run_records_step_attribution_matrix',
    'test_step_timeout_reason_names_step_and_limit',
    'test_deadline_expired_before_launch_keeps_the_plain_reason',
    'test_step_failure_books_never_run_steps_and_a_described_backstop',
    'test_sync_timeout_save_carries_the_reason_body',
    'test_failed_step_retries_on_a_fresh_row',
    'test_retry_of_an_approval_gated_step_re_arms_the_gate',
    'test_step_retries_zero_disables_the_retry',
    'test_pause_during_retry_backoff_parks',
    'test_ceiling_trip_during_retry_backoff_abandons_the_retry',
    'test_models_match_admits_pin_forms_and_flags_variants',
    'test_slow_approval_sync_never_falsifies_a_clean_pin',
    'test_slow_approval_sync_never_hides_a_real_drop',
    'test_failed_drop_redispatch_proceeds_on_the_dropped_attempt',
    'test_spent_deadline_abandons_the_drop_redispatch',
    'test_err_snapshots_keep_every_attempts_diagnosis',
    'test_run_end_drain_outlives_the_closed_iterations_deadline',
    'test_before_last_step_drain_uses_the_run_wall_not_the_iter_deadline',
    'test_finalize_terminal_cascade_matrix',
    'test_auto_backstop_commit_carries_step_and_plan_context',
    'test_stop_mid_step_lets_the_seat_complete',
    'test_pacing_retunes_take_effect_at_the_next_sleep',
    'test_timeout_void_force_commit_is_loud',
    'test_pending_finish_carries_to_the_continued_run',
    'test_stop_during_finish_drain_books_stopped',
    'test_pre_iteration_finish_drain_uses_the_run_wall_not_the_iter_deadline',
    'test_finalize_classifies_over_cap_finishes_by_reason',
    'test_deliberate_finish_survives_a_wind_down_budget_trip',
    'test_finalize_park_leaves_rows_open',
    'test_crash_exit_closes_the_open_iteration_and_step_rows',
    'test_run_fires_hook_pairings_off_stdout',
    'test_sync_launch_fires_step_pairing',
    'test_run_fires_iteration_failure_on_unhandled_loop_error',
    'test_resume_preflight_abort_preserves_the_paused_run',
    'test_resume_preflight_abort_recredits_the_reparked_wait',
    'test_resume_adopt_with_no_open_run_records_exited',
    'test_resume_anchors_run_deadline_on_credited_remaining',
    'test_interval_defaults_iter_timeout_but_honors_a_tighter_one',
    'test_stop_during_the_inter_iteration_sleep_ends_the_run',
]


class MockLoop(Loop):
    """Loop double: canned launch results, no preflight, no subprocesses."""

    def __init__(
        self: MockLoop,
        node: Node,
        results: Optional[list[StepResult]] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize ``MockLoop``."""
        super().__init__(node, **kwargs)
        self.results = list(results or [])
        self.launched: list[str] = []

    def _preflight(self: MockLoop) -> None:
        """Skip the binary/pricing/provider probes (no processes here)."""

    def _run_setup(self: MockLoop) -> bool:
        """Skip setup.sh (no processes here)."""
        return True

    def _commit_check(self: MockLoop) -> bool:
        """Read the tree as clean (commit behavior is test_commit's)."""
        return True

    def _launch(
        self: MockLoop,
        step: Step,
        prompt: str,
        *,
        agent: Any,
        budget: Optional[float],
    ) -> StepResult:
        """Pop the next scripted outcome instead of spawning an agent."""
        self.launched.append(self._step_label)
        if self.results:
            return self.results.pop(0)
        return StepResult(status='completed')


class TrackingLoop(MockLoop):
    """Mock loop recording every fired hook via the override recipe."""

    def __init__(self: TrackingLoop, node: Node, **kwargs: Any) -> None:
        """Initialize ``TrackingLoop``."""
        super().__init__(node, **kwargs)
        self.calls: list[Event] = []

    def on_iteration(self: TrackingLoop, *args: Any, **kwargs: Any) -> Event:
        """Record the iteration event."""
        event = super().on_iteration(*args, **kwargs)
        self.calls.append(event)
        return event

    def on_iteration_success(self: TrackingLoop, *args: Any, **kwargs: Any) -> Event:
        """Record the iteration success event."""
        event = super().on_iteration_success(*args, **kwargs)
        self.calls.append(event)
        return event

    def on_iteration_failure(self: TrackingLoop, *args: Any, **kwargs: Any) -> Event:
        """Record the iteration failure event."""
        event = super().on_iteration_failure(*args, **kwargs)
        self.calls.append(event)
        return event

    def on_step(self: TrackingLoop, *args: Any, **kwargs: Any) -> Event:
        """Record the step event."""
        event = super().on_step(*args, **kwargs)
        self.calls.append(event)
        return event

    def on_step_success(self: TrackingLoop, *args: Any, **kwargs: Any) -> Event:
        """Record the step success event."""
        event = super().on_step_success(*args, **kwargs)
        self.calls.append(event)
        return event

    def on_step_failure(self: TrackingLoop, *args: Any, **kwargs: Any) -> Event:
        """Record the step failure event."""
        event = super().on_step_failure(*args, **kwargs)
        self.calls.append(event)
        return event


@pytest.fixture
def loop_node(node_with_db: Node) -> Node:
    """Return a DB-backed node with the seed files a loop reads at boot."""
    (node_with_db.node_dir / 'NODE.md').write_text(
        '# Charter\n\nDo the work.\n', encoding='utf-8'
    )
    _seed_steps(node_with_db, ['01-PLAN.md', '02-EXECUTE.md'])
    _configure(node_with_db, max_iters=1, sync=False, local=True)
    return node_with_db


# ------ step parsing and discovery


@pytest.mark.parametrize(
    argnames=('text', 'expected'),
    argvalues=[
        # the five supported keys, values right-trimmed
        (
            '---\nrequires_approval: true\nagent: codex\nmodel: o3  \n'
            'timeout: 90s\ndetached: true\n---\n# S\n',
            {
                'requires_approval': 'true',
                'agent': 'codex',
                'model': 'o3',
                'timeout': '90s',
                'detached': 'true',
            },
        ),
        # no opening fence -> no frontmatter
        ('# S\n\nagent: codex\n', {}),
        # an unclosed block still contributes its scalar lines
        ('---\nmodel: opus\n# S\n', {'model': 'opus'}),
        # uppercase keys and empty values are not flat scalars
        ('---\nMODEL: opus\nagent:\n---\n', {}),
        # first occurrence wins
        ('---\nmodel: first\nmodel: second\n---\n', {'model': 'first'}),
    ],
)
def test_step_load_parses_strict_flat_scalar_frontmatter(
    tmp_path: pathlib.Path,
    text: str,
    expected: dict,
) -> None:
    """``Step.load`` honors the strict ``key: value`` grammar exactly."""
    path = tmp_path / '01-work.md'
    path.write_text(text, encoding='utf-8')
    step = Step.load(path, number=1)
    assert step.frontmatter == expected
    assert step.name == 'work'
    assert step.requires_approval == (expected.get('requires_approval') == 'true')
    assert step.timeout == expected.get('timeout')


@pytest.mark.parametrize(
    argnames=('key', 'value', 'match'),
    argvalues=[
        ('timeout', 'soon', 'timeout must be a duration with a unit suffix'),
        ('iter_timeout', '10', 'iter_timeout must be a duration with a unit suffix'),
        ('step_timeout', '0.5s', 'step_timeout must be greater than zero'),
        ('sleep', '0s', 'sleep must be greater than zero'),
    ],
)
def test_duration_validation_mirrors_the_launch_contract(
    loop_node: Node,
    key: str,
    value: str,
    match: str,
) -> None:
    """A malformed or sub-second duration refuses the launch with its key."""
    _configure(loop_node, **{key: value})
    with pytest.raises(ValueError, match=match):
        MockLoop(loop_node)


def test_step_timeout_frontmatter_substitutes_at_launch(
    loop_node: Node,
    capsys: pytest.CaptureFixture,
) -> None:
    """A step's ``timeout:`` bounds its own launch; a malformed one falls back.

    The node-global ``step_timeout`` is the default ceiling; a parseable
    frontmatter override substitutes for it in either direction -- a slow
    step raises its own ceiling above the global rather than min-ing with
    it -- and a malformed scalar -- step files are live-edited steering
    surfaces -- warns on stderr and falls back to the global instead of
    crashing the loop.
    """
    node = loop_node
    _configure(node, step_timeout='30s')
    loop = MockLoop(node)
    steps_dir = node.node_dir / 'steps'
    (steps_dir / '01-PLAN.md').write_text(
        '---\ntimeout: 5s\n---\n# PLAN\n\nWork.\n', encoding='utf-8'
    )
    (steps_dir / '02-EXECUTE.md').write_text(
        '---\ntimeout: soon\n---\n# EXECUTE\n\nWork.\n', encoding='utf-8'
    )
    (steps_dir / '03-COMMIT.md').write_text(
        '---\ntimeout: 90s\n---\n# COMMIT\n\nWork.\n', encoding='utf-8'
    )
    below, malformed, above = loop._discover_steps()
    # a parseable override substitutes for the node global, below or above
    assert loop._run_step(below).status == 'completed'
    assert loop._step_limit_seconds == 5
    assert loop._run_step(above).status == 'completed'
    assert loop._step_limit_seconds == 90
    # the malformed one warns and falls back to the global
    assert loop._run_step(malformed).status == 'completed'
    assert loop._step_limit_seconds == 30
    err = capsys.readouterr().err
    assert 'Warning:' in err
    assert '02-EXECUTE.md' in err


@pytest.mark.parametrize(
    argnames='bad_max_iters',
    argvalues=['two', [20]],
    ids=['non-int-string', 'non-scalar'],
)
def test_malformed_midrun_retune_warns_and_keeps_the_previous_value(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    bad_max_iters: Any,
) -> None:
    """A malformed mid-run config edit warns and keeps the prior values.

    Config is a live-edited steering surface: the iteration-top re-reads
    of ``max_iters``/``step_timeout``/``wait`` and the cost-cap reads
    (the iteration top and both budget probes) must warn and fall back
    on a hand-edit the launch validation never saw (a bare number, a
    non-integer, a non-scalar JSON value, a non-numeric cap), never
    crash the run and lose the remaining iterations.
    """
    monkeypatch.setenv('_NODE', '')
    _configure(loop_node, max_iters=2, step_timeout='30s')

    class EditingLoop(MockLoop):
        """Mock loop whose first launch hand-corrupts the retunable knobs."""

        def _launch(
            self: EditingLoop, step: Step, prompt: str, **kwargs: Any
        ) -> StepResult:
            """Break the config mid-run, then run the scripted outcome."""
            _configure(
                self.node,
                max_iters=bad_max_iters,
                step_timeout='600',
                wait='soon',
                max_cost='25 USD',
            )
            return super()._launch(step, prompt, **kwargs)

    loop = EditingLoop(loop_node)
    assert loop.run() == 0
    # both iterations ran to the max-iters completion on the kept values
    assert len(loop.launched) == 4
    assert loop_node.status() == 'completed'
    err = capsys.readouterr().err
    assert 'keeping the previous value' in err
    assert 'keeping the previous step_timeout' in err
    assert 'keeping the previous wait' in err
    assert 'keeping the previous cost caps' in err


def test_provider_frontmatter_rebinds_the_boot_agent(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider-only override rebinds the boot agent on the step's route.

    The boot binding pins the node's own (native) route, so a step
    carrying ``provider:`` frontmatter with no ``agent:`` override must
    reach the launch on a fresh agent bound to that route -- a dispatch
    that reused the boot agent would silently run the step on the
    vendor-native endpoint.
    """
    monkeypatch.setenv('_NODE', '')
    _seed_steps(loop_node, ['01-PLAN.md'])
    (loop_node.node_dir / 'steps' / '01-PLAN.md').write_text(
        '---\nprovider: openrouter\n---\n# PLAN\n\nWork.\n', encoding='utf-8'
    )

    class RoutingLoop(MockLoop):
        """Mock loop recording the agent each launch receives."""

        def __init__(self: RoutingLoop, node: Node, **kwargs: Any) -> None:
            """Initialize ``RoutingLoop``."""
            super().__init__(node, **kwargs)
            self.agents: list[Any] = []

        def _launch(
            self: RoutingLoop, step: Step, prompt: str, **kwargs: Any
        ) -> StepResult:
            """Record the launch's agent alongside the scripted outcome."""
            self.agents.append(kwargs['agent'])
            return super()._launch(step, prompt, **kwargs)

    loop = RoutingLoop(loop_node)
    assert loop.run() == 0
    # the launch received an agent bound to the step's route
    (agent,) = loop.agents
    assert agent.provider == 'openrouter'
    assert loop_node.status() == 'completed'


def test_agent_env_publishes_node_branch(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-launch agent env carries ``NODE_BRANCH`` == the node's branch.

    External env consumers key node identity off this field rather than
    ``_NODE``'s basename, so it rides every step launch beside the
    run/iter/step lineage.
    """
    monkeypatch.setenv('_NODE', '')

    class CapturingLoop(MockLoop):
        """Mock loop recording the env each launch would receive."""

        def __init__(self: CapturingLoop, node: Node, **kwargs: Any) -> None:
            """Initialize ``CapturingLoop``."""
            super().__init__(node, **kwargs)
            self.envs: list[dict[str, str]] = []

        def _launch(
            self: CapturingLoop, step: Step, prompt: str, **kwargs: Any
        ) -> StepResult:
            """Record the launch env alongside the scripted outcome."""
            self.envs.append(self._agent_env(self._step_label))
            return super()._launch(step, prompt, **kwargs)

    loop = CapturingLoop(loop_node)
    assert loop.run() == 0
    # every launch published the node's branch for external consumers
    assert loop.envs
    assert all(env['NODE_BRANCH'] == loop_node.branch for env in loop.envs)


def test_boot_records_the_tmux_socket_for_the_reconcile_probe(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boot records the pane's tmux socket; an in-band exit drops the record.

    ``Node._reconcile_status`` probes the server the session lives on via
    this record, so it must land before any step runs (a reconcile racing a
    fresh boot from a different-socket shell must already find it) and must
    not outlive the loop -- a surviving record, like ``.pgid``, would mark a
    death no cleanup could catch.
    """
    socket_path = '/tmp/fx-test/socket'  # noqa: S108
    monkeypatch.setenv('TMUX', f'{socket_path},4242,0')
    socket_file = loop_node.node_dir / SOCKET_FILE

    class RecordingLoop(MockLoop):
        """Mock loop reading the socket record where a step would run."""

        def __init__(self: RecordingLoop, node: Node, **kwargs: Any) -> None:
            """Initialize ``RecordingLoop``."""
            super().__init__(node, **kwargs)
            self.recorded: list[str] = []

        def _launch(
            self: RecordingLoop, step: Step, prompt: str, **kwargs: Any
        ) -> StepResult:
            """Record the socket file's content alongside the outcome."""
            self.recorded.append(socket_file.read_text(encoding='utf-8'))
            return super()._launch(step, prompt, **kwargs)

    loop = RecordingLoop(loop_node)
    assert loop.run() == 0
    # every step ran under the recorded socket, and the exit dropped it
    assert loop.recorded
    assert all(text.strip() == socket_path for text in loop.recorded)
    assert not socket_file.exists()


def test_continue_restore_lands_config_all_or_nothing(
    node_with_db: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The continue restore rewrites ``config.json`` all-or-nothing.

    The restore rewrites the fleet's most-read file right after the
    checkout/clean -- the same file ``Config.set`` only ever lands via
    the unique-temp ``os.replace`` swap, because every sibling command
    parses it live. The injected ``write_bytes`` double models a kill
    landing inside an in-place write's truncate window; the staged swap
    never opens the target itself, so the retuned config survives whole
    and parseable.
    """
    node = node_with_db
    # the documented steering flow: retune config.json between runs
    _configure(node, max_iters=3)
    config_path = node.node_dir / 'config.json'
    expected = json.loads(config_path.read_text(encoding='utf-8'))
    loop = MockLoop(node)

    def tear(path: pathlib.Path, data: bytes) -> int:
        """Model a kill mid-write: the truncate lands, the payload never does."""
        with path.open('wb'):
            pass
        return 0

    monkeypatch.setattr(pathlib.Path, 'write_bytes', tear)
    loop._clean_worktree()
    assert json.loads(config_path.read_text(encoding='utf-8')) == expected


def test_continue_cleanup_excludes_runtime_dirt(node_with_db: Node) -> None:
    """The continue cleanup's operator-edit commit skips stage-excluded dirt.

    The cleanup runs in a worktree that may carry no info/exclude at all (a
    fresh clone, a block predating an exclude's entry), so the stage
    excludes must ride its probe: runtime artifacts -- the engine skill
    tree, virtualenv contents, the DB -- never ride the operator-edit
    commit, and alone they never trigger one. Operator git surgery (a
    staged rename, a staged deletion) commits cleanly rather than crashing
    the launch.
    """
    node = node_with_db
    repo = node.worktree

    def _git(*args: str) -> str:
        """Run git in the repo and return stdout."""
        result = subprocess.run(
            ['git', '-C', f'{repo}', *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    # engine-materialized system skills and a virtualenv beside the node
    # seed, in a repo carrying no info/exclude block at all
    system = node.node_dir / 'skills' / '.system' / 'imagegen'
    system.mkdir(parents=True)
    (system / 'SKILL.md').write_text('engine-materialized\n', encoding='utf-8')
    venv = node.node_dir / '.venv' / 'bin'
    venv.mkdir(parents=True)
    (venv / 'python').write_text('#!interpreter\n', encoding='utf-8')
    (node.node_dir / 'note.md').write_text('steer left\n', encoding='utf-8')
    loop = MockLoop(node)
    loop._clean_worktree()
    # the operator-edit commit (the seed's untracked files) never sweeps
    # the engine tree, the venv, or the DB
    tracked = _git('ls-files')
    assert 'config.json' in tracked
    assert 'note.md' in tracked
    assert 'skills/.system' not in tracked
    assert '.venv' not in tracked
    assert '.db' not in tracked
    # engine dirt alone never triggers the commit: re-materialize what the
    # clean removed and rerun -- HEAD stays put
    head = _git('rev-parse', 'HEAD')
    system.mkdir(parents=True)
    (system / 'SKILL.md').write_text('engine-materialized\n', encoding='utf-8')
    loop._clean_worktree()
    assert _git('rev-parse', 'HEAD') == head
    # operator git surgery commits cleanly: a staged rename and a staged
    # deletion are already fully staged, so the cleanup must not re-name
    # their gone paths to the add
    _git('mv', f'{node.node_dir / "note.md"}', f'{node.node_dir / "moved.md"}')
    _git('rm', '-q', f'{node.node_dir / "config.json"}')
    loop._clean_worktree()
    tracked = _git('ls-files')
    assert 'moved.md' in tracked
    assert 'note.md' not in tracked
    # the staged deletion committed: config.json's row is gone from the
    # index (nothing on disk to back up, so the config restore skips too)
    assert 'config.json' not in tracked


def test_stream_fault_attributes_to_the_stream_side(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream-consumer fault (agent still live) books a stream error.

    When ``agent.stream`` raises while the agent is still running, the launch
    SIGKILLs the group and ``process.wait`` returns a signal death. That death
    must be attributed to the stream side -- its traceback recorded -- not
    mislabeled a generic ``agent error (exit 137)`` that buries the real fault
    and leaves no traceback to diagnose it.
    """
    monkeypatch.setenv('_NODE', '')

    class StreamRaisingAgent(SampleAgent):
        """A backend whose stream raises mid-run over a still-live process."""

        def spawn(
            self: StreamRaisingAgent,
            invocation: Any,
            *,
            start_new_session: bool = True,
            stderr: Any = None,
        ) -> subprocess.Popen:
            """Spawn a long-lived stub so the fault handler SIGKILLs a live group."""
            return subprocess.Popen(
                ['sleep', '30'],
                stdout=subprocess.PIPE,
                stderr=stderr,
                start_new_session=start_new_session,
            )

        def stream(self: StreamRaisingAgent, stdout: Any, **kwargs: Any) -> Any:
            """Raise a fractal-side (non-``AgentStreamError``) fault mid-drain."""
            raise ValueError('parser boom')

    class StreamFaultLoop(MockLoop):
        """Mock loop that swaps in the stream-raising agent and records outcomes."""

        def __init__(self: StreamFaultLoop, node: Node, **kwargs: Any) -> None:
            """Initialize ``StreamFaultLoop``."""
            super().__init__(node, **kwargs)
            self.launch_results: list[StepResult] = []

        def _launch(
            self: StreamFaultLoop, step: Step, prompt: str, **kwargs: Any
        ) -> StepResult:
            """Run the REAL launch (not MockLoop's mock) with a stream-raising agent."""
            # super() is MockLoop, whose _launch returns canned results; call the
            # base Loop._launch directly so the actual attribution runs, with the
            # per-step state run() has already set up
            kwargs['agent'] = StreamRaisingAgent(self.node)
            result = Loop._launch(self, step, prompt, **kwargs)
            self.launch_results.append(result)
            return result

    loop = StreamFaultLoop(loop_node)
    loop.run()
    # the fault is attributed to the stream side, never a generic 'agent error'
    assert loop.launch_results
    assert all(result.status == 'failed' for result in loop.launch_results)
    assert all(
        'stream error' in (result.reason or '')
        and 'agent error' not in (result.reason or '')
        for result in loop.launch_results
    ), [result.reason for result in loop.launch_results]


def test_agent_stderr_tolerates_non_utf8_output(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-UTF-8 byte in the agent's stderr capture never crashes the run.

    The failure surface re-reads the ``.err`` capture to print it and to
    tail the durable reason; agent stderr is external output, so a stray
    byte (raw terminal noise, a truncated multibyte char) must degrade to
    a replaced character -- not a ``UnicodeDecodeError`` that kills the
    whole loop where an honest ``agent error`` row should land.
    """
    monkeypatch.setenv('_NODE', '')

    class NoisyErrAgent(SampleAgent):
        """A failing backend whose stderr capture carries non-UTF-8 bytes."""

        def spawn(
            self: NoisyErrAgent,
            invocation: Any,
            *,
            start_new_session: bool = True,
            stderr: Any = None,
        ) -> subprocess.Popen:
            """Fail immediately with raw bytes already in the .err capture."""
            self.err_path.parent.mkdir(parents=True, exist_ok=True)
            self.err_path.write_bytes(b'boom \xff\xfe fatal\n')
            return subprocess.Popen(
                ['false'],
                stdout=subprocess.PIPE,
                stderr=stderr,
                start_new_session=start_new_session,
            )

    class NoisyLoop(MockLoop):
        """Mock loop that swaps in the noisy-stderr agent for the real launch."""

        def _launch(
            self: NoisyLoop, step: Step, prompt: str, **kwargs: Any
        ) -> StepResult:
            """Run the REAL launch so the failure-surface reads execute."""
            kwargs['agent'] = NoisyErrAgent(self.node)
            return Loop._launch(self, step, prompt, **kwargs)

    loop = NoisyLoop(loop_node)
    assert loop.run() == 0
    # the step failed honestly; the reason carries the tail, not a crash
    step = loop_node.db.read('steps', where={'step': 1})[0]
    assert step['status'] == 'failed'
    assert 'agent error' in (step['metadata'] or '')


def test_agent_launch_failure_books_a_failed_step(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn that cannot exec books a failed step, never a loop death.

    The agent binary can disappear mid-run (a PATH shim removed, a broken
    env edit), so the launch's OSError must land as an honest failed step
    row with the launch failure named, the loop continuing through the
    normal cascade instead of dying with the run relabeled ``Loop exited
    abnormally``.
    """
    monkeypatch.setenv('_NODE', '')

    class UnspawnableAgent(SampleAgent):
        """A backend whose binary cannot be executed."""

        def spawn(self: UnspawnableAgent, invocation: Any, **kwargs: Any) -> Any:
            """Refuse to exec, like a vanished binary."""
            raise FileNotFoundError(2, 'No such file or directory', 'sample')

    class UnspawnableLoop(MockLoop):
        """Mock loop that swaps in the unspawnable agent for the real launch."""

        def _launch(
            self: UnspawnableLoop, step: Step, prompt: str, **kwargs: Any
        ) -> StepResult:
            """Run the REAL launch so the spawn-failure surface executes."""
            kwargs['agent'] = UnspawnableAgent(self.node)
            return Loop._launch(self, step, prompt, **kwargs)

    loop = UnspawnableLoop(loop_node)
    assert loop.run() == 0
    # the step failed honestly with the launch failure named
    step = loop_node.db.read('steps', where={'step': 1})[0]
    assert step['status'] == 'failed'
    assert 'agent launch failed' in (step['metadata'] or '')


def test_setup_tolerates_non_utf8_output(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-UTF-8 byte in setup.sh output decodes leniently, never fatal.

    The exit code -- not the text -- decides success, so strict decoding
    would crash the whole run on a stray byte instead of running setup.
    """
    monkeypatch.setenv('_NODE', '')
    scripts = loop_node.node_dir / 'scripts'
    scripts.mkdir(exist_ok=True)
    (scripts / 'setup.sh').write_text(
        "#!/bin/bash\nprintf '\\xff\\n'\necho done\n", encoding='utf-8'
    )

    class SetupLoop(MockLoop):
        """Mock loop that runs the real setup.sh step."""

        _run_setup = Loop._run_setup

    assert SetupLoop(loop_node)._run_setup() is True


def test_unsupported_provider_frontmatter_refuses_the_step(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A step routed on a provider its agent lacks fails cleanly, not fatally.

    The refusal lands at dispatch -- an invocation-time ValueError would
    crash the loop at the launch site -- so no launch fires, the step
    records ``failed`` with the unsupported route named on stderr, and
    the run still ends through the normal cascade.
    """
    monkeypatch.setenv('_NODE', '')
    _seed_steps(loop_node, ['01-PLAN.md'])
    (loop_node.node_dir / 'steps' / '01-PLAN.md').write_text(
        '---\nprovider: bogus\n---\n# PLAN\n\nWork.\n', encoding='utf-8'
    )
    _configure(loop_node, step_retries=0)
    loop = MockLoop(loop_node)
    assert loop.run() == 0
    assert loop.launched == []
    err = capsys.readouterr().err
    assert "unsupported provider 'bogus' for claude" in err
    step = loop_node.db.read('steps', where={'step': 1})[0]
    assert (step['status'], step['exit_code']) == ('failed', 1)


def test_discover_steps_orders_and_validates_prefixes(
    loop_node: Node,
    capsys: pytest.CaptureFixture,
) -> None:
    """Discovery orders NN- files and fails loudly on prefix violations."""
    loop = MockLoop(loop_node)
    # a valid dir discovers in lexicographic order with 1-based numbers
    _seed_steps(loop_node, ['02-EXECUTE.md', '01-PLAN.md', '03-COMMIT.md'])
    steps = loop._discover_steps()
    assert [(step.number, step.name) for step in steps] == [
        (1, 'PLAN'),
        (2, 'EXECUTE'),
        (3, 'COMMIT'),
    ]
    # an empty dir is a loud failure naming the real cause
    _seed_steps(loop_node, [])
    assert loop._discover_steps() is None
    assert loop._fail_reason == 'no step files'
    # a file without the NN- prefix fails discovery
    _seed_steps(loop_node, ['plan.md'])
    assert loop._discover_steps() is None
    assert loop._fail_reason == 'invalid step files'
    # inconsistent digit widths fail discovery
    _seed_steps(loop_node, ['1-a.md', '02-b.md'])
    assert loop._discover_steps() is None
    assert loop._fail_reason == 'invalid step files'
    assert 'Error:' in capsys.readouterr().err


def test_preflight_aborts_on_a_non_utf8_step_file(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A step file with an invalid byte aborts preflight naming the file.

    Step files are hand-edited, so a bad byte is a boundary failure, not a
    crash: a bare ``UnicodeDecodeError`` names a byte offset but no file
    and would escape before the run row or the ``active`` stamp exists --
    stranding ``.status`` at ``idle`` (a never-started node) with the
    diagnosis lost in the dying pane. The abort machinery instead lands a
    closed ``exited`` run row naming the file and stamps the honest
    terminal.
    """
    monkeypatch.setenv('_NODE', '')
    (loop_node.node_dir / 'steps' / '01-PLAN.md').write_bytes(b'# PLAN \xff\n')
    loop = Loop(loop_node)
    assert loop.run() == 1
    # the reason landed on a closed exited run row, and the stamp is honest
    assert loop_node.status() == 'exited'
    run = loop_node.db.read('runs', where={'node': loop_node.branch})[0]
    assert (run['status'], run['exit_code']) == ('exited', 1)
    assert '01-PLAN.md' in run['metadata']
    assert 'not valid UTF-8' in capsys.readouterr().err


# ------ boot latch


def test_park_if_latched_walks_ancestors_with_resume_exemption(
    git_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A boot into a paused subtree parks; resume skips the ancestor walk."""
    parent, child = _spawn_parent_child(git_repo, monkeypatch)
    parent.status_set('paused')
    # a boot under a paused ancestor parks: banner, pause signal, paused stamp
    loop = MockLoop(child)
    assert loop._park_if_latched() is True
    assert f'=== Parked at boot: {parent.branch} is paused ===' in (
        capsys.readouterr().out
    )
    assert child.status() == 'paused'
    assert child.record.signal_get('pause') is not None
    # a resume relaunch is exempt from the ancestor walk (the leaf-first
    # fan-out legitimately leaves ancestors paused while a child boots)
    child.record.signal_clear('pause')
    child.status_set('active')
    resumed = MockLoop(child, resume=True)
    assert resumed._park_if_latched() is False
    # ... but not from a NEW tree-wide brake landing during the relaunch
    child._tree_latch_file.write_text('paused\n', encoding='utf-8')
    assert resumed._park_if_latched() is True


# ------ budget policy


def test_step_budget_math_binds_the_tightest_cap(loop_node: Node) -> None:
    """The per-step leash is min(run remaining - reserve, iter headroom, cap)."""
    node = loop_node
    # uncapped: no leash at all
    loop = MockLoop(node)
    loop._run_id = node.record.run_start()
    assert loop._step_budget() is None
    # a run ceiling alone: remaining minus the (defaulted 10%) reserve
    _configure(node, max_cost=10.0)
    loop._read_cost_caps()
    _record_step_cost(node, run_id=loop._run_id, cost=2.0)
    assert loop._step_budget() == pytest.approx(7.0)
    # the iteration's live headroom binds when tighter than the run leash
    _configure(node, max_cost=10.0, max_iter_cost=1.0)
    loop._read_cost_caps()
    loop._iter_id = node.record.iter_start(run_id=loop._run_id, iter=2)
    step_id = node.record.step_start(
        iter_id=loop._iter_id,
        run_id=loop._run_id,
        step=1,
        step_name='PLAN',
    )
    node.record.step_cost(step_id=step_id, cost=0.4)
    node.record.step_end(step_id=step_id, status='completed', exit_code=0)
    assert loop._step_budget() == pytest.approx(0.6)
    # a drained iteration headroom is skipped, not zeroed: the run leash governs
    node.record.step_cost(step_id=step_id, cost=1.4)
    assert loop._step_budget() == pytest.approx(10.0 - 3.4 - 1.0)
    # the static step cap binds when tightest
    _configure(node, max_cost=10.0, max_iter_cost=1.0, max_step_cost=0.25)
    loop._read_cost_caps()
    assert loop._step_budget() == pytest.approx(0.25)


def _record_killed_step(node: Node, *, run_id: int, iter: int) -> None:
    """Record one ended NULL-cost (killed-before-flush) step in ``run_id``."""
    iter_id = node.record.iter_start(run_id=run_id, iter=iter)
    step_id = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='EXECUTE',
    )
    node.record.step_end(step_id=step_id, status='killed', exit_code=1)


def test_run_spent_counts_recorded_cost_only(loop_node: Node) -> None:
    """Ended steps with NULL cost count as zero -- never an estimate.

    A step killed before its usage flush records NULL cost: the ledger's
    honest signal that no cost was recorded. The budget probes enforce
    the recorded SUM alone -- no per-step figure is imputed to rows the
    ledger cannot price, so bookkeeping rows (never-run tails, skips,
    pre-launch failures) can never fabricate a budget stop.
    """
    node = loop_node
    _configure(node, max_cost=10.0, max_step_cost=1.0)
    loop = MockLoop(node)
    loop._run_id = node.record.run_start()
    loop._read_cost_caps()
    _record_step_cost(node, run_id=loop._run_id, cost=2.0, iter=1)
    for i in (2, 3, 4):
        _record_killed_step(node, run_id=loop._run_id, iter=i)
    assert loop._run_spent() == pytest.approx(2.0)
    assert loop._run_remaining() == pytest.approx(8.0)


def test_budget_skipped_sync_books_stopped(loop_node: Node) -> None:
    """A budget-skipped SYNC row reads like the step it precedes.

    Over budget, skipped work steps record ``stopped`` flagging 'over
    budget'; the interleaved SYNC row must match, so the activity log
    shows one cleanly-labeled budget stop instead of an unexplained
    ``failed`` beside it.
    """
    node = loop_node
    _configure(node, max_cost=1.0)
    loop = MockLoop(node)
    loop._run_id = node.record.run_start()
    loop._iter_id = node.record.iter_start(run_id=loop._run_id, iter=1)
    loop._read_cost_caps()
    # drain the budget so the sync launch is skipped, never run
    step_id = node.record.step_start(
        iter_id=loop._iter_id,
        run_id=loop._run_id,
        step=1,
        step_name='PLAN',
    )
    node.record.step_cost(step_id=step_id, cost=1.5)
    node.record.step_end(step_id=step_id, status='completed', exit_code=0)
    # the SYNC step reads the always-present package seed (never written here --
    # _sync_file resolves to the shared _node/modes/SYNC.md)
    result, _, sync_step_id = loop._sync(step_num=1, label='before PLAN', strict=True)
    assert result.status == 'skipped'
    rows = node.db.read('steps', where={'step_id': sync_step_id})
    assert rows[0]['status'] == 'stopped'
    assert rows[0]['exit_code'] == 0
    assert rows[0]['metadata'] == 'over budget'
    # never launched, so the row carries the knowable zero -- the sync is
    # priced (free), never disclosed as unpriced
    assert rows[0]['cost'] == 0.0
    assert node.cost.unpriced(iter_id=loop._iter_id) == 0


def test_budget_skipped_steps_record_knowable_zero(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-launch budget skip prices every row at the knowable zero.

    A step that never reaches its launch spent nothing -- the row carries
    an explicit 0.0 so spend sums stay exact and ``unpriced`` (NULL cost:
    spend unknowable) discloses nothing. NULL stays reserved for launched
    steps whose usage never flushed.
    """
    monkeypatch.setenv('_NODE', '')
    # a drained leash skips every launch (work steps and syncs alike)
    monkeypatch.setattr(MockLoop, '_step_budget', lambda self: 0.0)
    loop = MockLoop(loop_node)
    assert loop.run() == 0
    steps = loop_node.db.read('steps', where={'run_id': loop._run_id})
    assert steps
    assert all(row['cost'] == 0.0 for row in steps)
    assert loop_node.cost.unpriced(run_id=loop._run_id) == 0


def test_run_remaining_floors_at_zero_without_a_last_reading(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed read with no last-good spend floors the leash at 0, not the cap.

    The resume path meets prior spend on a cold in-memory cache; granting
    the untouched cap there would size one step's leash as if nothing were
    spent. With no last-good reading the remaining floors at 0 so the step
    skips and retries next probe.
    """
    node = loop_node
    _configure(node, max_cost=10.0)
    loop = MockLoop(node)
    loop._run_id = node.record.run_start()
    loop._read_cost_caps()

    def boom(*args: object, **kwargs: object) -> float:
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(node.cost, 'remaining', boom)
    # _last_run_spent is still None (no successful probe yet) -> floor at 0
    assert loop._last_run_spent is None
    assert loop._run_remaining() == 0.0


def test_soft_cap_warning_fires_once_for_unbraked_caps(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Armed caps + non-enforcing agent + no timeout warn once at boot.

    A non-enforcing agent's per-step cap is checked only between steps, so
    with no timeout either, one runaway step can overshoot every cap
    unbounded -- the boot discloses the exposure once and names the remedy
    (``--step-timeout``); an enforcing agent or any armed timeout silences
    it.
    """
    node = loop_node
    monkeypatch.setattr(MockLoop, '_preflight', lambda self: None)
    # force the flag off on the backend class before construction, so the
    # warning rides its production call site (the end of ``__init__``)
    monkeypatch.setattr(ClaudeAgent, 'enforces_budget', False)
    # capped, non-enforcing, no timeouts: construction warns
    _configure(node, max_cost=10.0)
    loop = MockLoop(node)
    out = capsys.readouterr().out
    assert 'overshoot' in out
    assert '--step-timeout' in out
    # once per run: the iteration-top re-check stays silent
    loop._warn_soft_cap()
    assert 'overshoot' not in capsys.readouterr().out
    # any armed timeout silences a fresh boot
    _configure(node, max_cost=10.0, step_timeout='30s')
    MockLoop(node)
    assert 'overshoot' not in capsys.readouterr().out


def test_step_budget_reserve_window_floors_at_remaining(loop_node: Node) -> None:
    """Inside the reserve window the leash floors at the full remaining."""
    node = loop_node
    _configure(node, max_cost=10.0, reserve_budget=1.0)
    loop = MockLoop(node)
    loop._run_id = node.record.run_start()
    # spend into the reserve window: remaining (0.5) - reserve (1.0) goes
    # non-positive, so the leash floors at the remaining -- wind-down steps
    # spend the reserve but never past the ceiling
    _record_step_cost(node, run_id=loop._run_id, cost=9.5)
    assert loop._step_budget() == pytest.approx(0.5)
    # a fully drained budget yields a non-positive leash: the step is skipped
    _record_step_cost(node, run_id=loop._run_id, cost=0.5, iter=2)
    assert loop._step_budget() <= 0


def test_boundary_checks_read_live_caps(loop_node: Node) -> None:
    """Both boundary checks read the caps live, not an iteration-top snapshot.

    A snapshot-bound check would stay pinned to stale values for a whole
    iteration: a cap lowered -- or first granted -- mid-iteration must
    reach the very next reserve-boundary and per-step ceiling probe.
    """
    node = loop_node
    # boot capped high: the boot-time snapshot alone would never trip
    _configure(node, max_cost=50.0)
    loop = MockLoop(node)
    loop._run_id = node.record.run_start()
    _record_step_cost(node, run_id=loop._run_id, cost=6.0)
    assert loop._check_reserve_boundary() is False
    # lower the cap mid-iteration (no _read_cost_caps): the live value trips
    _configure(node, max_cost=5.0)
    assert loop._check_reserve_boundary() is True
    # a cap first granted mid-iteration arms the ceiling on an uncapped boot
    _configure(node, max_cost=None)
    uncapped = MockLoop(node)
    uncapped._run_id = node.record.run_start()
    _record_step_cost(node, run_id=uncapped._run_id, cost=6.0)
    assert uncapped._check_subtree_ceiling() is False
    _configure(node, max_cost=5.0)
    assert uncapped._check_subtree_ceiling() is True


def test_untracked_spend_under_caps_warns_once(
    loop_node: Node,
    capsys: pytest.CaptureFixture,
) -> None:
    """Armed caps over untracked spend warn once per run and never block.

    An all-NULL run counts $0 in the guards, so neither boundary check
    can ever trip; the first probe says so loudly (advisory only) and
    the latch keeps every later probe quiet. An uncapped run has
    nothing inert to warn about.
    """
    node = loop_node
    _configure(node, max_cost=5.0)
    loop = MockLoop(node)
    loop._run_id = node.record.run_start()
    _record_unpriced_step(node, run_id=loop._run_id)
    # the first probe warns without tripping ...
    assert loop._check_subtree_ceiling() is False
    warning = "WARNING: cost caps are set but this run's spend is untracked"
    assert warning in capsys.readouterr().out
    # ... and the latch keeps the other boundary check quiet
    assert loop._check_reserve_boundary() is False
    assert capsys.readouterr().out == ''
    # the same untracked spend on an uncapped run stays quiet
    _configure(node, max_cost=None)
    uncapped = MockLoop(node)
    uncapped._run_id = node.record.run_start()
    _record_unpriced_step(node, run_id=uncapped._run_id)
    assert uncapped._check_reserve_boundary() is False
    assert capsys.readouterr().out == ''


def test_failed_cost_reads_hold_the_last_good_reading(
    loop_node: Node,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contended ledger holds the guards at the last good reading.

    A failed read must not size the per-step leash at the full cap
    ("nothing spent") or read as untracked spend: the leash and the
    boundary probes hold the last good reading until the ledger reads
    again, and the once-per-run warning names the read failure instead
    of blaming unpriced steps.
    """
    node = loop_node
    _configure(node, max_cost=10.0, max_iter_cost=1.0, reserve_budget=1.0)
    loop = MockLoop(node)
    loop._read_cost_caps()
    loop._run_id = node.record.run_start()
    # spend deep into the reserve window across two iterations, leaving the
    # current iteration a thin headroom
    _record_step_cost(node, run_id=loop._run_id, cost=8.9)
    loop._iter_id = node.record.iter_start(run_id=loop._run_id, iter=2)
    step_id = node.record.step_start(
        iter_id=loop._iter_id,
        run_id=loop._run_id,
        step=1,
        step_name='PLAN',
    )
    node.record.step_cost(step_id=step_id, cost=0.7)
    node.record.step_end(step_id=step_id, status='completed', exit_code=0)
    # good reads prime the guards: the ceiling probe reads the spend, the
    # leash reads the iteration headroom
    assert loop._check_subtree_ceiling() is False
    assert loop._step_budget() == pytest.approx(0.3)

    # the DB degrades: every read raises like a lock timeout would
    def locked_read(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(node.db, 'read', locked_read)
    # the leash holds the residual readings, not the full caps
    assert loop._step_budget() == pytest.approx(0.3)
    assert 'WARNING: cost read failed' in capsys.readouterr().out
    # the reserve boundary still trips on the last good spend, and the
    # attribution stays honest -- no unpriced-steps (untracked) blame
    assert loop._check_reserve_boundary() is True
    assert 'untracked' not in capsys.readouterr().out


def test_cap_gate_demands_a_priced_model_from_tracking_gaps(
    loop_node: Node,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Armed caps refuse steps whose spend the loop cannot track.

    A routed claude prices from token counts like the token-priced
    natives, so under a cap it needs a chain-priced model: a model-less
    step refuses naming the requirement, an unpriced slug refuses naming
    the entry gap, and a chain-priced slug launches.
    """
    node = loop_node
    _configure(node, max_cost=5.0, step_retries=0)
    node.config.set('provider', 'openrouter')
    # no model anywhere: the cap requires one before any launch
    monkeypatch.setattr(pricing, '_load', lambda: {})
    loop = MockLoop(node)
    loop.run()
    assert loop.launched == []
    assert 'a cost cap requires a model for claude' in capsys.readouterr().err
    # an unpriceable slug refuses naming the missing pricing entry
    _configure(node, model='mystery/model')
    loop = MockLoop(node)
    loop.run()
    assert loop.launched == []
    assert 'has no pricing entry' in capsys.readouterr().err
    # a chain-priced slug launches (the openrouter/ prefix carries the rates)
    monkeypatch.setattr(
        pricing,
        '_load',
        lambda: {
            'openrouter/anthropic/claude-haiku-4.5': {'input_cost_per_token': 1e-6}
        },
    )
    _configure(node, model='anthropic/claude-haiku-4.5')
    loop = MockLoop(node)
    loop.run()
    assert loop.launched != []


# the reason shapes a descendant's pending finish signal carries: an ancestor
# budget check's fan-out (its raw reason + attribution) vs a deliberate finish
_CASCADED_ABORT = (
    'subtree cost budget reached (spent $9 >= $5 max) (via finish of main.parent)'
)
_DELIBERATE_FINISH = 'parent done (via finish of main.parent)'


@pytest.mark.parametrize(
    argnames=('max_iters', 'signal_step', 'reason', 'reserved_prompts', 'status'),
    argvalues=[
        # a cascaded budget abort landing mid-iteration: the remaining step
        # winds down in reserve and the run lands exited/0 under the relabel
        pytest.param(1, 1, _CASCADED_ABORT, [False, True], 'exited', id='cascaded'),
        # a deliberate finish flips nothing: the remaining step runs plain
        # and the gate closes the run as a goal-met completion
        pytest.param(
            1, 1, _DELIBERATE_FINISH, [False, False], 'completed', id='deliberate'
        ),
        # a cascaded abort landing during the final step overlays nothing --
        # the iteration closes, the gate honors the pending signal, and no
        # further iteration starts (the terminal sweep still relabels)
        pytest.param(2, 2, _CASCADED_ABORT, [False, False], 'exited', id='final-step'),
    ],
)
def test_pending_finish_winds_down_in_reserve_for_budget_cascades(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
    max_iters: int,
    signal_step: int,
    reason: str,
    reserved_prompts: list[bool],
    status: str,
) -> None:
    """A pending cascaded-budget finish winds down the iteration in reserve.

    The current iteration is always the run's last under a pending
    finish (the post-iteration gate ends it), so once an ancestor's
    budget abort lands the per-step derivation flips reserve and the
    remaining steps carry the wind-down overlay -- while a deliberate
    finish leaves them plain.
    """
    monkeypatch.setenv('_NODE', '')
    node = loop_node
    _configure(node, max_iters=max_iters)

    class SignalingLoop(MockLoop):
        """Mock loop that lands a propagated finish during a scripted step."""

        def __init__(self: SignalingLoop, node: Node, **kwargs: Any) -> None:
            """Initialize ``SignalingLoop``."""
            super().__init__(node, **kwargs)
            self.prompts: list[str] = []

        def _launch(
            self: SignalingLoop, step: Step, prompt: str, **kwargs: Any
        ) -> StepResult:
            self.prompts.append(prompt)
            if len(self.prompts) == signal_step:
                self.node.record.signal_set('finish', reason)
            return super()._launch(step, prompt, **kwargs)

    loop = SignalingLoop(node)
    assert loop.run() == 0
    # the signal never cuts the iteration short, and only the steps after a
    # cascaded abort compose the wind-down overlay
    marker_flags = ['Reserve Mode' in prompt for prompt in loop.prompts]
    assert marker_flags == reserved_prompts
    row = node.db.read('runs', where={'run_id': loop._run_id})[0]
    if status == 'exited':
        assert (row['status'], row['exit_code']) == ('exited', 0)
        assert row['metadata'] == (
            f'ancestor budget abort: {reason}; this run spent untracked'
        )
    else:
        assert (row['status'], row['exit_code']) == ('completed', 0)
        assert row['metadata'] == ''


def test_pending_finish_between_iterations_starts_none(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cascaded finish landing between iterations starts no further one.

    The pre-iteration gate catches a signal planted during the
    inter-iteration sleep before the next iteration opens -- no new
    iteration row, no further launches -- and the terminal sweep still
    relabels the landing with this run's own figures.
    """
    monkeypatch.setenv('_NODE', '')
    node = loop_node
    _configure(node, max_iters=2, sleep='1s')
    planted = []

    def _plant_finish(seconds: float) -> None:
        """Land the propagated finish during the inter-iteration sleep."""
        if not planted:
            node.record.signal_set('finish', _CASCADED_ABORT)
            planted.append(len(loop.launched))

    monkeypatch.setattr(time, 'sleep', _plant_finish)
    loop = MockLoop(node)
    assert loop.run() == 0
    # the signal landed after iteration 1's two steps (in the sleep), and
    # iteration 2 never started: the pre-iteration gate broke out first
    assert planted == [2]
    assert len(loop.launched) == 2
    iters = node.db.read('iters', where={'run_id': loop._run_id})
    assert [row['iter'] for row in iters] == [1]
    # the terminal sweep still relabels the landing as a budget exit
    row = node.db.read('runs', where={'run_id': loop._run_id})[0]
    assert (row['status'], row['exit_code']) == ('exited', 0)
    assert row['metadata'] == (
        f'ancestor budget abort: {_CASCADED_ABORT}; this run spent untracked'
    )


# ------ step attribution


@pytest.mark.parametrize(
    argnames=('result', 'row', 'banner', 'node_status'),
    argvalues=[
        # a deadline overrun records exited/1 with the timed-out reason, and
        # the run ends exited (never shadowed by max-iters)
        (
            StepResult(status='timed out', exit_code=124),
            ('exited', 1, 'timed out'),
            '--- Step 1/2 (PLAN): timed out (',
            'exited',
        ),
        # a budget skip records stopped/0 flagged over budget -- the skip
        # banner vocabulary never lands in the row
        (
            StepResult(status='skipped', exit_code=125),
            ('stopped', 0, 'over budget'),
            '--- Step 1/2 (PLAN): skipped (over budget) ---',
            'completed',
        ),
        # a pause abort records paused/0 and parks the run
        (
            StepResult(status='paused'),
            ('paused', 0, ''),
            '--- Step 1/2 (PLAN): paused (',
            'paused',
        ),
    ],
    ids=['timed_out', 'over_budget', 'paused'],
)
def test_run_records_step_attribution_matrix(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    result: StepResult,
    row: tuple[str, int, str],
    banner: str,
    node_status: str,
) -> None:
    """Timed-out, budget-skipped, and paused launches record honest rows."""
    monkeypatch.setenv('_NODE', '')
    loop = MockLoop(loop_node, results=[result])
    assert loop.run() == 0
    assert banner in capsys.readouterr().out
    assert loop_node.status() == node_status
    step = loop_node.db.read('steps', where={'step': 1})[0]
    assert (step['status'], step['exit_code'], step['metadata']) == row


def test_step_timeout_reason_names_step_and_limit(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step deadline overrun names the step and its effective ceiling.

    The step row keeps the short ``timed out`` reason (its ``step_name``
    column already names the step) while the iteration row carries the
    enriched ``<step> timed out (<limit>)`` label -- and the run still ends
    ``exited`` with the run-qualified timeout reason: a timeout on the
    final iteration is never relabeled ``completed``.
    """
    monkeypatch.setenv('_NODE', '')
    _configure(loop_node, step_timeout='30s')
    timeout = StepResult(status='timed out', exit_code=124)
    loop = MockLoop(loop_node, results=[timeout])
    assert loop.run() == 0
    iteration = loop_node.db.read('iters', where={'node': loop_node.branch})[0]
    assert iteration['metadata'] == 'PLAN timed out (30s)'
    run = loop_node.db.read('runs', where={'run_id': loop._run_id})[0]
    assert (run['status'], run['exit_code']) == ('exited', 1)
    assert run['metadata'] == f'Timed out at iteration {loop._run_id}.1 (30s/step)'


def test_deadline_expired_before_launch_keeps_the_plain_reason(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deadline that expires before a launch resolves no step ceiling.

    The step never ran, so the iteration reason names it without a
    parenthetical limit -- a ``(0s)`` label would read as a real ceiling.
    """
    monkeypatch.setenv('_NODE', '')
    _configure(loop_node, timeout='1s')
    launched = []

    class SlowLoop(MockLoop):
        """Mock loop whose first launch outlives the run deadline."""

        def _launch(self: SlowLoop, *args: Any, **kwargs: Any) -> StepResult:
            # PLAN won its race to start, so EXECUTE is the step the expired
            # deadline catches -- the premise the assertion below rests on
            launched.append(True)
            time.sleep(2.5)
            return super()._launch(*args, **kwargs)

    loop = SlowLoop(loop_node, results=[StepResult(status='completed')])
    assert loop.run() == 0
    iteration = loop_node.db.read('iters', where={'node': loop_node.branch})[0]
    # starvation: the one-second deadline lapsed before PLAN could launch, so
    # PLAN is the timed-out step and the ceiling under test never came up
    if not launched:
        pytest.skip('load starvation: the run deadline expired before any launch')
    # the guarded regression: EXECUTE never launched, so no ceiling resolved
    assert iteration['metadata'] == 'EXECUTE timed out'


def test_step_failure_books_never_run_steps_and_a_described_backstop(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-iteration failure books the never-run tail and a self-describing save.

    Steps after the failed one become real ``stopped`` rows naming the
    failure (so ``node activity`` answers which steps never ran), and the
    backstop commit carries the run-qualified subject plus a body naming
    the reason, the never-run tail, and the swept files.
    """
    monkeypatch.setenv('_NODE', '')
    _seed_steps(loop_node, ['01-PLAN.md', '02-EXECUTE.md', '03-REVIEW.md'])
    _configure(loop_node, step_retries=0)
    results = [
        StepResult(status='completed'),
        StepResult(status='failed', exit_code=2, reason='agent error (exit 2)'),
    ]
    loop = MockLoop(loop_node, results=results)
    # leave real uncommitted work for the backstop sweep to save
    (loop_node.worktree / 'partial.txt').write_text('half-done\n', encoding='utf-8')
    assert loop.run() == 0
    # the never-run tail is a real row: stopped, naming the failed step
    rows = loop_node.db.read('steps', where={'node': loop_node.branch})
    by_name = {row['step_name']: row for row in rows}
    review = by_name['REVIEW']
    assert (review['status'], review['metadata']) == ('stopped', 'failed on EXECUTE')
    # a never-run tail row spent nothing: priced as the knowable zero
    assert review['cost'] == 0.0

    def _log(fmt: str) -> str:
        result = subprocess.run(
            ['git', '-C', f'{loop_node.worktree}', 'log', '-1', f'--format={fmt}'],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    # the backstop commit describes itself: the run-qualified subject names
    # the failed step; the body carries the reason, the tail, and a diffstat
    # naming the swept work
    subject = f'{loop_node.branch}: iteration {loop._run_id}.1 (failed on EXECUTE)'
    assert _log('%s') == subject
    body = _log('%b')
    assert 'agent error (exit 2)' in body
    assert 'steps not run: REVIEW' in body
    assert 'partial.txt' in body


def test_sync_timeout_save_carries_the_reason_body(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fatal pre-step SYNC timeout's backstop body names the limit.

    The subject only says ``timed out during SYNC``; the body carries the
    enriched ``SYNC timed out (<limit>)`` reason the iteration row records,
    so the save explains the ceiling from git history alone.
    """
    monkeypatch.setenv('_NODE', '')
    _seed_steps(loop_node, ['01-PLAN.md'])
    _configure(loop_node, sync=True, step_timeout='30s')
    timeout = StepResult(status='timed out', exit_code=124)
    loop = MockLoop(loop_node, results=[timeout])
    # leave real uncommitted work for the backstop sweep to save
    (loop_node.worktree / 'partial.txt').write_text('half-done\n', encoding='utf-8')
    assert loop.run() == 0
    iteration = loop_node.db.read('iters', where={'node': loop_node.branch})[0]
    assert iteration['metadata'] == 'SYNC timed out (30s)'
    log = subprocess.run(
        ['git', '-C', f'{loop_node.worktree}', 'log', '-1', '--format=%b'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert 'SYNC timed out (30s)' in log.stdout


# ------ step retry


def test_failed_step_retries_on_a_fresh_row(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A failed launch retries once: a plain failed row, then a retry row.

    The first attempt's row closes ``failed`` with its plain reason; the
    retry books a fresh row whose metadata carries the ``retry`` marker,
    and the iteration succeeds off the retried attempt.
    """
    monkeypatch.setenv('_NODE', '')
    _seed_steps(loop_node, ['01-PLAN.md'])
    _configure(loop_node, step_retry_backoff='1s')
    results = [
        StepResult(status='failed', exit_code=2, reason='agent error (exit 2)'),
        StepResult(status='completed'),
    ]
    loop = MockLoop(loop_node, results=results)
    assert loop.run() == 0
    assert '--- Step 1/1 (PLAN): retrying in 1s ---' in capsys.readouterr().out
    # one row per attempt (read newest-first): the failed attempt keeps its
    # plain reason, the retry carries the marker
    retried, failed = loop_node.db.read('steps', where={'node': loop_node.branch})
    assert (failed['status'], failed['metadata']) == ('failed', 'agent error (exit 2)')
    assert (retried['status'], retried['metadata']) == ('completed', 'retry')
    # the iteration closes off the retried attempt's success
    assert loop_node.status() == 'completed'


def test_retry_of_an_approval_gated_step_re_arms_the_gate(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retried approval-gated step pends a fresh gate, never a free pass.

    Only a failed attempt retries, and a failed attempt's wait never ran --
    so the retry's row arms its own gate (approval is granted per attempt,
    not inherited) and the failed row's superseded gate is voided rather
    than left pending forever.
    """
    monkeypatch.setenv('_NODE', '')
    _seed_steps(loop_node, ['01-PLAN.md'])
    (loop_node.node_dir / 'steps' / '01-PLAN.md').write_text(
        '---\nrequires_approval: true\n---\n# PLAN\n\nWork.\n', encoding='utf-8'
    )
    _configure(loop_node, step_retry_backoff='1s')

    class ApprovingLoop(MockLoop):
        """Mock loop whose operator approves the final attempt's gate."""

        def _launch(self: ApprovingLoop, *args: Any, **kwargs: Any) -> StepResult:
            result = super()._launch(*args, **kwargs)
            # approve only the attempt that will succeed -- the failed
            # attempt's wait never runs, so its gate goes unapproved
            if not self.results:
                self.node.record.step_approve(step_id=self._step_id)
            return result

    results = [
        StepResult(status='failed', exit_code=2, reason='agent error (exit 2)'),
        StepResult(status='completed'),
    ]
    loop = ApprovingLoop(loop_node, results=results)
    assert loop.run() == 0
    # one row per attempt: the retry carries its own granted gate and the
    # failed attempt's superseded gate is voided, not pending forever
    retried, failed = loop_node.db.read('steps', where={'node': loop_node.branch})
    assert (retried['status'], retried['metadata']) == ('completed', 'retry')
    assert retried['approved']
    assert failed['approved'] is None


@pytest.mark.parametrize('step_retries', [0, -1])
def test_step_retries_zero_disables_the_retry(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
    step_retries: int,
) -> None:
    """``step_retries=0`` gives a failed launch exactly one attempt.

    A hand-edited negative clamps to the same single attempt instead of
    crashing the iteration with an empty attempt loop.
    """
    monkeypatch.setenv('_NODE', '')
    _seed_steps(loop_node, ['01-PLAN.md'])
    _configure(loop_node, step_retries=step_retries)
    failure = StepResult(status='failed', exit_code=2, reason='agent error (exit 2)')
    loop = MockLoop(loop_node, results=[failure])
    assert loop.run() == 0
    assert loop.launched == ['step 1 of 1 (PLAN)']
    steps = loop_node.db.read('steps', where={'node': loop_node.branch})
    assert [row['status'] for row in steps] == ['failed']


def test_pause_during_retry_backoff_parks(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pause landing during the retry backoff parks instead of retrying."""
    monkeypatch.setenv('_NODE', '')
    _seed_steps(loop_node, ['01-PLAN.md'])
    _configure(loop_node, step_retry_backoff='1s')

    class PausingLoop(MockLoop):
        """Mock loop whose failing launch lands beside a pause request."""

        def _launch(self: PausingLoop, *args: Any, **kwargs: Any) -> StepResult:
            self.node.record.signal_set('pause', 'operator')
            return super()._launch(*args, **kwargs)

    failure = StepResult(status='failed', exit_code=2, reason='agent error (exit 2)')
    loop = PausingLoop(loop_node, results=[failure])
    assert loop.run() == 0
    # the backoff detected the pause: a single attempt, then the park
    assert loop.launched == ['step 1 of 1 (PLAN)']
    assert loop_node.status() == 'paused'


def test_ceiling_trip_during_retry_backoff_abandons_the_retry(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A subtree ceiling spent by the failed attempt buys no retry."""
    monkeypatch.setenv('_NODE', '')
    _seed_steps(loop_node, ['01-PLAN.md'])
    _configure(loop_node, max_cost=5.0, step_retry_backoff='1s')

    class SpendingLoop(MockLoop):
        """Mock loop whose failing launch spends past the run ceiling."""

        def _launch(self: SpendingLoop, *args: Any, **kwargs: Any) -> StepResult:
            _record_step_cost(self.node, run_id=self._run_id, cost=6.0, iter=2)
            return super()._launch(*args, **kwargs)

    failure = StepResult(status='failed', exit_code=2, reason='agent error (exit 2)')
    loop = SpendingLoop(loop_node, results=[failure])
    assert loop.run() == 0
    assert 'Subtree cost budget reached' in capsys.readouterr().out
    # the spent cap abandoned the retry: one attempt, one failed row
    assert loop.launched == ['step 1 of 1 (PLAN)']
    failed_rows = loop_node.db.read('steps', where={'status': 'failed'})
    assert len(failed_rows) == 1


# ------ model-drop policy


class ServingLoop(MockLoop):
    """Mock loop whose work launches stamp a scripted served model.

    Each non-SYNC launch pops the next entry of ``serves`` and -- unless
    it is ``None`` -- lands it exactly as the stream driver does: stamped
    on the live step row and stowed as the launch's served-model record
    (which every launch, SYNC included, first resets).
    """

    def __init__(
        self: ServingLoop,
        node: Node,
        results: Optional[list[StepResult]] = None,
        serves: Optional[list[Optional[str]]] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize ``ServingLoop``."""
        super().__init__(node, results=results, **kwargs)
        self.serves = list(serves or [])

    def _launch(self: ServingLoop, step: Step, *args: Any, **kwargs: Any) -> StepResult:
        """Stamp the scripted served model beside the popped outcome."""
        result = super()._launch(step, *args, **kwargs)
        self._step_served = ()
        if step.name != 'SYNC' and self.serves and self._step_id is not None:
            served = self.serves.pop(0)
            if served is not None:
                self.node.record.step_session(
                    'claude',
                    step_id=self._step_id,
                    model=served,
                    session='sess',
                )
                self._step_served = (served,)
        return result


class ApprovingServingLoop(ServingLoop):
    """Serving loop double whose approval lands only after a sync cycle ran."""

    def _sync(self: ApprovingServingLoop, *args: Any, **kwargs: Any) -> Any:
        """Grant the awaited step's gate once the sync restored its id."""
        out = super()._sync(*args, **kwargs)
        # the sync restored the awaited step's id -- grant its gate
        if self._step_id is not None:
            self.node.record.step_approve(step_id=self._step_id)
        return out


@pytest.mark.parametrize(
    argnames=('pinned', 'served', 'match'),
    argvalues=[
        # exact ids, and the forms a pin legitimately resolves to
        ('pinned-model', 'pinned-model', True),
        ('claude-opus-5', 'claude-opus-5-20260115', True),
        ('opus', 'claude-opus-5-20260115', True),
        ('anthropic/claude-opus-5', 'claude-opus-5', True),
        ('claude-opus-5', 'anthropic/claude-opus-5', True),
        ('anthropic/claude-sonnet-4.6', 'claude-sonnet-4-6-20260101', True),
        ('gpt-4o', 'gpt-4o-2024-08-06', True),
        # an alias names a family, so its own version run and date match
        ('opus', 'claude-opus-4-1-20250805', True),
        ('opus', 'claude-opus-latest', True),
        # genuinely different models, containment notwithstanding
        ('pinned-model', 'dropped-model', False),
        ('gpt-5.6-sol', 'gpt-5.6', False),
        ('gpt-5.6', 'gpt-5.6-sol', False),
        ('claude-fable-5', 'claude-fable-5-mini', False),
        ('claude-opus-4', 'claude-opus-4-1', False),
        ('claude-opus-4-1', 'claude-opus-4', False),
        ('opus', 'propus-x', False),
        # a variant riding a version is a different model, alias pin or not
        ('opus', 'claude-opus-5-mini', False),
        # a version bump ahead of a date is a bump, not a snapshot stamp
        ('claude-opus-4', 'claude-opus-4-1-20250805', False),
        ('gpt-5', 'gpt-5-1-20260101', False),
    ],
)
def test_models_match_admits_pin_forms_and_flags_variants(
    pinned: str,
    served: str,
    match: bool,
) -> None:
    """Pin matching admits alias/slug/date forms and flags real swaps.

    A gateway slug on either side and a dated snapshot are the pin they
    stand for, and an alias pin is its whole family -- version run and
    date alike. A truncation, a variant suffix, or a version bump is a
    genuinely different model, even when one id contains the other as a
    bare substring, and even when the bump hides behind a date stamp.
    """
    assert _models_match(pinned, served) is match


def test_slow_approval_sync_never_falsifies_a_clean_pin(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An approval-wait SYNC must not clobber the pin the drop check reads.

    A gated step pinned by frontmatter, served exactly its pin, approved
    only after a sync cycle ran (a human-latency approval): the sync's own
    launch resolves the node default over the step pin, and an unrestored
    pin would falsify a drop -- a spurious event, a wasted re-dispatch,
    and a second approval demand on a fully clean step.
    """
    monkeypatch.setenv('_NODE', '')
    _seed_steps(loop_node, ['01-PLAN.md'])
    (loop_node.node_dir / 'steps' / '01-PLAN.md').write_text(
        '---\nmodel: pinned-model\nrequires_approval: true\n---\n# PLAN\n\nWork.\n',
        encoding='utf-8',
    )
    _configure(loop_node, model='node-default', sync=True, wait='1s')
    loop = ApprovingServingLoop(loop_node, serves=['pinned-model'])
    assert loop.run() == 0
    # the pin was served: nothing evented, nothing re-dispatched, one
    # approval consumed, and no marker reaches the listing
    assert loop_node.db.read('events', where={'event': 'model_drop'}) == []
    work = [label for label in loop.launched if label.startswith('step ')]
    assert len(work) == 1
    row = loop_node.db.read('steps', where={'step_name': 'PLAN'})[0]
    assert (row['model'], row['metadata']) == ('pinned-model', '')
    assert row['approved']
    assert loop_node.status_detail() == ''


def test_slow_approval_sync_never_hides_a_real_drop(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A frontmatter-only pin still catches a drop across an approval sync.

    With no node-level model the sync's launch resolves an empty pin, and
    an unrestored one would disable detection on exactly the gated steps
    it should guard: a genuinely dropped serve must still event, mark the
    attempt's row, and buy the single re-dispatch.
    """
    monkeypatch.setenv('_NODE', '')
    _seed_steps(loop_node, ['01-PLAN.md'])
    (loop_node.node_dir / 'steps' / '01-PLAN.md').write_text(
        '---\nmodel: pinned-model\nrequires_approval: true\n---\n# PLAN\n\nWork.\n',
        encoding='utf-8',
    )
    _configure(loop_node, sync=True, wait='1s', step_retry_backoff='1s')
    loop = ApprovingServingLoop(loop_node, serves=['dropped-model', 'pinned-model'])
    assert loop.run() == 0
    # the drop evented and bought the re-dispatch; the dropped attempt's
    # row carries the mark and the clean retry supersedes it
    assert len(loop_node.db.read('events', where={'event': 'model_drop'})) == 1
    work = [label for label in loop.launched if label.startswith('step ')]
    assert len(work) == 2
    redispatched, dropped = loop_node.db.read('steps', where={'step_name': 'PLAN'})
    assert (dropped['model'], dropped['metadata']) == (
        'dropped-model',
        'model drop (served dropped-model)',
    )
    assert (redispatched['model'], redispatched['metadata']) == ('pinned-model', '')
    assert loop_node.status_detail() == ''


def test_failed_drop_redispatch_proceeds_on_the_dropped_attempt(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A re-dispatch that cannot complete never downgrades the dropped step.

    The dropped attempt's work is complete; when the re-dispatch and the
    failure retries its failure bought all fail, the loop proceeds on that
    work -- the iteration completes, the tail steps run, and the drop
    stays evented, marked on its own row, and surfaced in the listing.
    """
    monkeypatch.setenv('_NODE', '')
    _seed_steps(loop_node, ['01-PLAN.md', '02-EXECUTE.md'])
    for name in ('01-PLAN.md', '02-EXECUTE.md'):
        (loop_node.node_dir / 'steps' / name).write_text(
            f'---\nmodel: pinned-model\n---\n# {name}\n\nWork.\n',
            encoding='utf-8',
        )
    _configure(loop_node, step_retry_backoff='1s')
    failure = StepResult(status='failed', exit_code=2, reason='agent error (exit 2)')
    results = [
        StepResult(status='completed'),  # PLAN, served off the pin
        failure,  # the drop re-dispatch
        failure,  # the failure retry its failure bought
        StepResult(status='completed'),  # EXECUTE, served the pin
    ]
    serves = ['dropped-model', None, None, 'pinned-model']
    loop = ServingLoop(loop_node, results=results, serves=serves)
    assert loop.run() == 0
    assert 'proceeding on the dropped attempt' in capsys.readouterr().out
    # the failed re-dispatch chain spent its rows, then the tail step ran
    assert loop.launched == [
        'step 1 of 2 (PLAN)',
        'step 1 of 2 (PLAN)',
        'step 1 of 2 (PLAN)',
        'step 2 of 2 (EXECUTE)',
    ]
    # newest-first: EXECUTE, the failed retry, the failed re-dispatch, and
    # the dropped attempt whose mark no completed attempt superseded
    rows = loop_node.db.read('steps', where={'node': loop_node.branch})
    assert [(row['step'], row['status'], row['metadata']) for row in rows] == [
        (2, 'completed', ''),
        (1, 'failed', 'agent error (exit 2); retry'),
        (1, 'failed', 'agent error (exit 2)'),
        (1, 'completed', 'model drop (served dropped-model)'),
    ]
    assert len(loop_node.db.read('events', where={'event': 'model_drop'})) == 1
    # the iteration completed on the dropped attempt's work, and the
    # unresolved drop reaches the listing
    assert loop_node.status() == 'completed'
    assert loop_node.status_detail() == 'model drop'


def test_spent_deadline_abandons_the_drop_redispatch(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drop past the iteration deadline proceeds instead of re-dispatching.

    The re-dispatch's own launch pre-check would only convert the
    completed work into a timed-out failure, so a spent deadline abandons
    it: one launch, the drop evented and marked, the iteration completed
    -- never a timed-out run off work that finished.
    """
    monkeypatch.setenv('_NODE', '')
    _seed_steps(loop_node, ['01-PLAN.md'])
    (loop_node.node_dir / 'steps' / '01-PLAN.md').write_text(
        '---\nmodel: pinned-model\n---\n# PLAN\n\nWork.\n',
        encoding='utf-8',
    )
    _configure(loop_node, iter_timeout='1s')

    class OverrunningLoop(ServingLoop):
        """Mock loop whose launch completes past the iteration deadline."""

        def _launch(self: OverrunningLoop, *args: Any, **kwargs: Any) -> StepResult:
            time.sleep(1.2)
            return super()._launch(*args, **kwargs)

    loop = OverrunningLoop(loop_node, serves=['dropped-model'])
    assert loop.run() == 0
    # the spent deadline bought no re-dispatch; the completed work stands
    assert loop.launched == ['step 1 of 1 (PLAN)']
    row = loop_node.db.read('steps', where={'step_name': 'PLAN'})[0]
    assert (row['status'], row['metadata']) == (
        'completed',
        'model drop (served dropped-model)',
    )
    assert len(loop_node.db.read('events', where={'event': 'model_drop'})) == 1
    # the iteration and run close clean -- not timed out
    iter_row = loop_node.db.read('iters', where={'node': loop_node.branch})[0]
    assert (iter_row['status'], iter_row['metadata']) == ('completed', '')
    assert loop_node.record.runs(limit=1)[0]['metadata'] == 'Reached max iterations (1)'
    assert loop_node.status_detail() == 'model drop'


# ------ launch diagnostics


def test_err_snapshots_keep_every_attempts_diagnosis(loop_node: Node) -> None:
    """Each failing launch snapshots its stderr to its own tmp/err file.

    Retries book one step row per attempt, and the durable stderr
    snapshot matches that granularity: the first failure keeps the plain
    run-iter-step name, and a repeat under the same key -- a retry
    attempt or a later same-iteration SYNC -- lands beside it instead of
    overwriting the earlier diagnosis.
    """

    class MockProcess:
        """Process double whose launch fails with exit code 7."""

        pid = 4242
        stdout: tuple[str, ...] = ()

        def wait(self: MockProcess) -> int:
            """Report the failing exit."""
            return 7

    class MockInvocation:
        """Invocation double carrying no session."""

        session = None

    class MockResult:
        """Drained-stream double carrying no session, cost, or budget stop."""

        session = None
        cost = None
        budget_stopped = False
        models = ()

    class MockAgent:
        """Agent double writing a canned diagnosis per spawn, then failing."""

        name = 'claude'
        enforces_budget = False

        def __init__(
            self: MockAgent, err_path: pathlib.Path, diagnoses: list[str]
        ) -> None:
            """Initialize ``MockAgent``."""
            self.err_path = err_path
            self.diagnoses = list(diagnoses)

        def config_model(self: MockAgent) -> None:
            """Report no configured model."""
            return None

        def invocation(self: MockAgent, prompt: str, **kwargs: Any) -> Any:
            """Build a session-less invocation double."""
            return MockInvocation()

        def spawn(self: MockAgent, invocation: Any, **kwargs: Any) -> MockProcess:
            """Write this launch's diagnosis to the stderr capture."""
            kwargs['stderr'].write(self.diagnoses.pop(0).encode('utf-8'))
            return MockProcess()

        def stream(self: MockAgent, lines: Any, **kwargs: Any) -> MockResult:
            """Drain nothing and report an empty stream outcome."""
            return MockResult()

    node = loop_node
    loop = Loop(node)
    loop._run_id = node.record.run_start()
    loop._iter = 1
    step = Step(node.node_dir / 'steps' / '01-PLAN.md', number=1)
    agent = MockAgent(
        node.node_dir / 'claude.err', ['first diagnosis\n', 'second diagnosis\n']
    )
    first = loop._launch(step, 'prompt', agent=agent, budget=None)
    second = loop._launch(step, 'prompt', agent=agent, budget=None)
    assert (first.status, second.status) == ('failed', 'failed')
    # the first failure keeps the plain run-iter-step name; the repeat
    # serializes beside it with its own diagnosis intact
    err_dir = node.node_dir / 'tmp' / 'err'
    plain = err_dir / f'{loop._run_id}-1-PLAN.err'
    assert plain.read_text(encoding='utf-8') == 'first diagnosis\n'
    (repeat,) = [path for path in err_dir.glob('*.err') if path != plain]
    assert repeat.read_text(encoding='utf-8') == 'second diagnosis\n'


# ------ run-end drain


def test_run_end_drain_outlives_the_closed_iterations_deadline(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """The run-end child drain is bounded by the run wall alone.

    An iteration's deadline dies with the iteration: the finish drain
    runs after the closing iteration ends, so a leftover per-iteration
    deadline would time the drain out -- stamping the run completed over
    still-active children -- instead of waiting them out.
    """
    monkeypatch.setenv('_NODE', '')
    _seed_steps(loop_node, ['01-PLAN.md'])
    _configure(loop_node, iter_timeout='2s', wait='1s')

    class DrainingLoop(MockLoop):
        """Mock loop with scripted descendant polls and a mid-step finish."""

        def __init__(self: DrainingLoop, node: Node, **kwargs: Any) -> None:
            """Initialize ``DrainingLoop``."""
            super().__init__(node, **kwargs)
            self.polls = [True] * 5

        def _launch(self: DrainingLoop, *args: Any, **kwargs: Any) -> StepResult:
            """Land the finish signal during the step's launch."""
            self.node.record.signal_set('finish', 'done')
            return super()._launch(*args, **kwargs)

        def _descendants_active(self: DrainingLoop) -> bool:
            """Report the children active until the scripted polls run out."""
            return bool(self.polls and self.polls.pop(0))

    loop = DrainingLoop(loop_node)
    assert loop.run() == 0
    out = capsys.readouterr().out
    # the drain waits the children out past the dead iteration's deadline
    assert loop.polls == []
    assert '--- Finishing: all child nodes finished ---' in out
    assert '--- Waiting for children: timed out ---' not in out
    assert loop_node.status() == 'completed'


def test_before_last_step_drain_uses_the_run_wall_not_the_iter_deadline(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """The finishing iteration's before-last-step drain outlives iter_timeout.

    A finish landing mid-iteration drains the subtree before the final
    step so that step integrates the children's work. Like the pre- and
    post-iteration drains, the wait is bounded by the run wall alone:
    the live iteration's deadline left armed would time it out over
    children that were finishing normally -- the iteration books failed,
    the final step never runs, and the finalize backstop re-drains and
    stamps the run completed over the skipped wind-down.
    """
    monkeypatch.setenv('_NODE', '')
    _configure(loop_node, iter_timeout='2s', wait='1s')

    class FinishingLoop(MockLoop):
        """Mock loop with scripted descendant polls and a first-step finish."""

        def __init__(self: FinishingLoop, node: Node, **kwargs: Any) -> None:
            """Initialize ``FinishingLoop``."""
            super().__init__(node, **kwargs)
            self.polls = [True] * 5

        def _launch(self: FinishingLoop, *args: Any, **kwargs: Any) -> StepResult:
            """Land the finish signal during the step's launch."""
            self.node.record.signal_set('finish', 'done')
            return super()._launch(*args, **kwargs)

        def _descendants_active(self: FinishingLoop) -> bool:
            """Report the children active until the scripted polls run out."""
            return bool(self.polls and self.polls.pop(0))

    loop = FinishingLoop(loop_node)
    assert loop.run() == 0
    out = capsys.readouterr().out
    # the drain waits the children out past the live iteration's deadline
    assert loop.polls == []
    assert '--- Waiting for children: timed out ---' not in out
    # and the final step ran, so the goal-met terminal is honest
    assert len(loop.launched) == 2
    iteration = loop_node.db.read('iters', where={'node': loop_node.branch})[0]
    assert (iteration['status'], iteration['exit_code']) == ('completed', 0)
    run = loop_node.db.read('runs', where={'run_id': loop._run_id})[0]
    assert (run['status'], run['exit_code']) == ('completed', 0)
    assert loop_node.status() == 'completed'


# ------ terminal cascade


@pytest.mark.parametrize(
    argnames=('arrange', 'node_status', 'run_status', 'exit_code', 'reason'),
    argvalues=[
        # an unexplained break is abnormal; a run that never reached its
        # first iteration composes the zero-iteration ref
        (lambda loop: None, 'exited', 'exited', 1, 'Exited at iteration {run}.0'),
        # a timeout is abnormal even on the final iteration (never shadowed
        # by the max-iters clause)
        (
            lambda loop: (
                setattr(loop, '_timed_out', True),
                setattr(loop, '_max_iters', 1),
                setattr(loop, '_iter', 1),
            ),
            'exited',
            'exited',
            1,
            'Timed out at iteration {run}.1 (no limit)',
        ),
        # a stop on the final iteration is not shadowed either
        (
            lambda loop: (
                loop.node.record.signal_set('stop', 'manual'),
                setattr(loop, '_max_iters', 1),
                setattr(loop, '_iter', 1),
            ),
            'stopped',
            'stopped',
            0,
            'Stopped by request',
        ),
        # running out the iteration budget is a clean, expected end
        (
            lambda loop: (
                setattr(loop, '_max_iters', 2),
                setattr(loop, '_iter', 2),
            ),
            'completed',
            'completed',
            0,
            'Reached max iterations (2)',
        ),
        # a failed final iteration is abnormal even at the max-iters
        # boundary -- the run must not launder a dead iteration into a
        # clean completion
        (
            lambda loop: (
                setattr(loop, '_max_iters', 2),
                setattr(loop, '_iter', 2),
                setattr(loop, '_last_iter_failed', True),
            ),
            'exited',
            'exited',
            1,
            'Reached max iterations (2); final iteration failed',
        ),
        # a goal-met finish records completed with no reason
        (
            lambda loop: loop.node.record.signal_set('finish', 'done'),
            'completed',
            'completed',
            0,
            None,
        ),
        # a stop that interrupts the finish drain abandons the finish: the
        # run must not claim completed over a subtree it never drained
        (
            lambda loop: (
                loop.node.record.signal_set('finish', 'done'),
                loop.node.record.signal_set('stop', 'manual'),
                setattr(loop, '_wait_seconds', 1),
                setattr(loop, '_descendants_active', lambda: True),
            ),
            'stopped',
            'stopped',
            0,
            'Stopped by request',
        ),
        # a run-wall expiry during the drain abandons the finish the same
        # way, keeping its abnormal timeout terminal
        (
            lambda loop: (
                loop.node.record.signal_set('finish', 'done'),
                setattr(loop, '_run_end_epoch', 1),
                setattr(loop, '_wait_seconds', 1),
                setattr(loop, '_descendants_active', lambda: True),
            ),
            'exited',
            'exited',
            1,
            'Timed out at iteration {run}.0 (no limit)',
        ),
        # an inconclusive subtree probe during the drain counts as active,
        # never drained -- the run must not claim a completed finish over
        # children it could not see; the run wall stays the bounded escape
        (
            lambda loop: (
                loop.node.record.signal_set('finish', 'done'),
                setattr(loop, '_run_end_epoch', 1),
                setattr(loop, '_wait_seconds', 1),
                setattr(loop.node, 'list', _locked_list),
            ),
            'exited',
            'exited',
            1,
            'Timed out at iteration {run}.0 (no limit)',
        ),
        # a budget abort is never a goal-met completion, but it is a
        # designed landing -- exited with exit 0, the budget discriminator
        (
            lambda loop: (
                setattr(loop, '_budget_hit', True),
                setattr(loop, '_budget_reason', 'subtree cost budget reached'),
            ),
            'exited',
            'exited',
            0,
            'subtree cost budget reached',
        ),
        # a setup crash-loop ends exited with the honest reason
        (
            lambda loop: (
                setattr(loop, '_setup_abort', True),
                setattr(loop, '_setup_fails', 3),
            ),
            'exited',
            'exited',
            1,
            'setup failed x3',
        ),
    ],
    ids=[
        'break',
        'timeout',
        'stop',
        'max_iters',
        'max_iters_failed_final_iter',
        'finish',
        'stop_abandons_finish_drain',
        'timeout_abandons_finish_drain',
        'inconclusive_probe_reads_active',
        'budget',
        'setup_abort',
    ],
)
def test_finalize_terminal_cascade_matrix(
    loop_node: Node,
    arrange: Any,
    node_status: str,
    run_status: str,
    exit_code: int,
    reason: Optional[str],
) -> None:
    """The status matrix records the honest terminal on node and run row."""
    loop = MockLoop(loop_node)
    loop._run_id = loop_node.record.run_start()
    arrange(loop)
    assert loop._finalize() == 0
    assert loop_node.status() == node_status
    row = loop_node.db.read('runs', where={'run_id': loop._run_id})[0]
    assert (row['status'], row['exit_code']) == (run_status, exit_code)
    # a clean finish records no reason (the column keeps its blank default);
    # abnormal iteration labels are run-qualified, so bind the live run id
    expected = '' if reason is None else reason.format(run=loop._run_id)
    assert row['metadata'] == expected
    assert row['ended_at'] is not None


def test_auto_backstop_commit_carries_step_and_plan_context(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auto backstop names the step it follows and the newest plan.

    A seat death leaves the loop to commit whatever is in the tree; a bare
    ``(auto)`` subject over thousands of inserted lines costs archaeology
    at every forensics pass and merge screen, so the subject carries the
    step the save follows and the body the last plan's title.
    """
    monkeypatch.setenv('_NODE', '')
    node = loop_node
    node.plans.init(iter_ref='1.1', name='route_survey', title='Survey the routes')

    class _DirtyLoop(MockLoop):
        """Leave the worktree dirty so the auto backstop fires."""

        def _commit_check(self: _DirtyLoop) -> bool:
            return False

    (node.worktree / 'work.txt').write_text('real work\n', encoding='utf-8')
    loop = _DirtyLoop(loop_node)
    assert loop.run() == 0
    message = subprocess.run(
        ['git', '-C', f'{node.worktree}', 'log', '-1', '--format=%B'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # the subject names the step; the body carries the step and plan context
    assert '(auto after' in message
    assert 'after step:' in message
    assert 'plan: 1.1 Survey the routes' in message


def test_stop_mid_step_lets_the_seat_complete(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop landing mid-step never tears the in-flight seat.

    ``node stop`` is "stop after the current step": the signal is a DB row
    the loop polls between steps, so a stop that lands while an agent is in
    flight lets that launch run to completion -- its step row books
    ``completed``, never a signal death -- and only the steps after it are
    forgone. ``kill`` is the immediate path; stop must not be one.
    """
    monkeypatch.setenv('_NODE', '')

    class _StopMidStep(MockLoop):
        """Signal stop while the first step's launch is in flight."""

        def _launch(
            self: _StopMidStep,
            step: Step,
            prompt: str,
            *,
            agent: Any,
            budget: Optional[float],
        ) -> StepResult:
            if not self.launched:
                self.node.record.signal_set('stop', 'manual')
            return super()._launch(step, prompt, agent=agent, budget=budget)

    _configure(loop_node, max_iters=3)
    loop = _StopMidStep(loop_node)
    assert loop.run() == 0
    # the in-flight seat completed; only the following step was forgone
    assert len(loop.launched) == 1
    [step] = loop_node.db.read('steps', where={'node': loop_node.branch})
    assert (step['status'], step['exit_code']) == ('completed', 0)
    iteration = loop_node.db.read('iters', where={'node': loop_node.branch})[0]
    assert (iteration['status'], iteration['exit_code']) == ('stopped', 0)
    run = loop_node.db.read('runs', where={'run_id': loop._run_id})[0]
    assert (run['status'], run['exit_code']) == ('stopped', 0)
    assert loop_node.status() == 'stopped'


def test_pacing_retunes_take_effect_at_the_next_sleep(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-run sleep/interval edit reaches the very next sleep call.

    Pacing was the one knob a running loop never re-read: a nulled sleep
    kept sleeping its boot value for the rest of the run. The knobs now
    re-read at their natural boundary -- the sleep call itself -- so a
    sleep granted mid-run takes effect immediately, and clearing it again
    stops the sleeping just as live.
    """
    monkeypatch.setenv('_NODE', '')
    node = loop_node
    _configure(node, max_iters=3)
    chunks: list[float] = []

    def fake_sleep(seconds: float) -> None:
        chunks.append(seconds)
        # clearing the sleep mid-run must reach the next boundary too
        _configure(node, sleep=None)

    monkeypatch.setattr('fractal.core.loop.time.sleep', fake_sleep)

    class _RetuningLoop(MockLoop):
        """Grant a sleep mid-iteration 1 (the boot config had none)."""

        def _launch(
            self: _RetuningLoop, step: Step, prompt: str, **kwargs: Any
        ) -> StepResult:
            if not chunks:
                _configure(self.node, sleep='40s')
            return super()._launch(step, prompt, **kwargs)

    loop = _RetuningLoop(node)
    assert loop.run() == 0
    # the granted sleep fired after iteration 1 (boot had none), chunked at
    # 30s; the null-out landed before the second boundary, so no later
    # sleep ran
    assert chunks == [30, 10]


def test_timeout_void_force_commit_is_loud(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A timed-out step whose backstop stages nothing is named loudly.

    A detached pass killed by its deadline books its rows and can ship no
    bytes; the backstop's quiet 'Nothing staged to commit' would let that
    void pass as a save, silently voiding the work (a verify pass most of
    all). The timeout case warns on stderr; a clean non-timeout sweep
    stays quiet.
    """
    monkeypatch.setenv('_NODE', '')
    loop = MockLoop(loop_node)
    loop._run_id = loop_node.record.run_start()
    # settle the fixture's estate into a commit so the probe sweeps clean
    loop._force_commit('prime')
    capsys.readouterr()
    loop._timed_out = True
    loop._force_commit('failed on VERIFY')
    captured = capsys.readouterr()
    assert 'Nothing staged to commit' in captured.out
    assert 'timed out with no committed output' in captured.err
    # the same empty sweep without a timeout stays quiet
    loop._timed_out = False
    loop._force_commit('final')
    assert 'no committed output' not in capsys.readouterr().err


def test_pending_finish_carries_to_the_continued_run(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An undischarged finish books on the next run instead of orphaning.

    Finish signals are run-scoped, and a run can die between `node finish`
    landing and the cascade consuming it (a torn seat, a stop interrupting
    the drain) -- without the carry, the docket-met node keeps iterating
    and burning budget as an active node forever. The continued run adopts
    the deliberate finish and completes without buying a single step; a
    budget-stemmed finish stays with its run (the re-arm raised the caps
    on purpose).
    """
    monkeypatch.setenv('_NODE', '')
    node = loop_node
    _configure(node, max_iters=3)

    class _TornDrain(MockLoop):
        """Land finish then stop mid-drain, stranding the finish."""

        def _launch(
            self: _TornDrain,
            step: Step,
            prompt: str,
            *,
            agent: Any,
            budget: Optional[float],
        ) -> StepResult:
            result = super()._launch(step, prompt, agent=agent, budget=budget)
            if len(self.launched) == 1:
                self.node.record.signal_set('finish', 'requirements met')
                self.node.record.signal_set('stop', 'manual')
            return result

        def _descendants_active(self: _TornDrain) -> bool:
            return True

    torn = _TornDrain(node)
    torn._wait_seconds = 1
    assert torn.run() == 0
    run = node.record.runs(limit=1)[0]
    assert run['status'] == 'stopped'
    # the continued run carries the finish: it drains and completes
    # without launching a step
    carried = MockLoop(node, continue_=True)
    assert carried.run() == 0
    assert carried.launched == []
    run = node.record.runs(limit=1)[0]
    assert (run['status'], run['exit_code']) == ('completed', 0)
    assert node.status() == 'completed'
    # a budget-stemmed finish never carries: the next continue runs normally
    run_id = node.record.run_start()
    node.record.signal_set(
        'finish',
        'cost budget reserve reached (spent $9.0000 >= $10.0 max - $1.0 reserve)',
    )
    node.record.run_end(run_id=run_id, status='exited', exit_code=0)
    node.status_set('exited')
    plain = MockLoop(node, continue_=True)
    assert plain.run() == 0
    assert len(plain.launched) > 0


def test_stop_during_finish_drain_books_stopped(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop that interrupts the finish drain reads as a requested end.

    A stop is the designed escape from a finish drain (a crashed-but-active
    child would otherwise hang the wait forever), so the interrupted
    iteration books ``stopped``/0 -- never a fabricated ``agent error`` --
    and the run ends ``stopped``: it must not claim a completed finish over
    a subtree it never drained.
    """
    monkeypatch.setenv('_NODE', '')

    class _StopMidDrain(MockLoop):
        """Signal finish+stop after the first step; keep a child 'active'."""

        def _launch(
            self: _StopMidDrain,
            step: Step,
            prompt: str,
            *,
            agent: Any,
            budget: Optional[float],
        ) -> StepResult:
            result = super()._launch(step, prompt, agent=agent, budget=budget)
            if len(self.launched) == 1:
                self.node.record.signal_set('finish', 'requirements met')
                self.node.record.signal_set('stop', 'manual')
            return result

        def _descendants_active(self: _StopMidDrain) -> bool:
            return True

    loop = _StopMidDrain(loop_node)
    loop._wait_seconds = 1
    assert loop.run() == 0
    # the drain interrupted before the last step -- only the first launched
    assert len(loop.launched) == 1
    iteration = loop_node.db.read('iters', where={'node': loop_node.branch})[0]
    assert (iteration['status'], iteration['exit_code']) == ('stopped', 0)
    run = loop_node.db.read('runs', where={'run_id': loop._run_id})[0]
    assert (run['status'], run['exit_code']) == ('stopped', 0)
    assert run['metadata'] == 'Stopped by request'
    assert loop_node.status() == 'stopped'


def test_pre_iteration_finish_drain_uses_the_run_wall_not_the_iter_deadline(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finish caught pre-iteration drains under the run wall, not iter_timeout.

    The loop arms ``_iter_end_epoch`` at the top of every pass, before the
    pre-iteration signal checks. A pending finish there drains the subtree
    (``_wait_for_children('run end')``) -- but for an iteration that never
    runs (the break precedes ``iter_start``), so the just-armed iteration
    deadline must not bound the drain, exactly as the post-iteration path
    clears it. Left armed, a drain outlasting one ``iter_timeout`` books
    ``exited``/timed-out over children that were finishing normally --
    deterministic on any resumed or sleeping finishing parent.
    """
    monkeypatch.setenv('_NODE', '')
    node = loop_node
    _configure(node, iter_timeout='5m')  # arms _iter_end_epoch each pass

    class _DrainCapture(MockLoop):
        """Record the iteration deadline live at each run-end drain."""

        def __init__(self: _DrainCapture, node: Node, **kwargs: Any) -> None:
            """Initialize ``_DrainCapture``."""
            super().__init__(node, **kwargs)
            self.drain_epochs: list[int] = []

        def _adopt(self: _DrainCapture) -> None:
            # the run exists now -- arm the finish the pre-iteration check reads
            super()._adopt()
            self.node.record.signal_set('finish', 'requirements met')

        def _wait_for_children(self: _DrainCapture, context: str) -> bool:
            self.drain_epochs.append(self._iter_end_epoch)
            return True

    loop = _DrainCapture(node)
    assert loop.run() == 0
    # the pre-iteration drain saw no iteration deadline (run wall alone)
    assert loop.drain_epochs
    assert loop.drain_epochs[0] == 0
    # and the finish completed, never a spurious timeout
    run = node.db.read('runs', where={'run_id': loop._run_id})[0]
    assert (run['status'], run['exit_code']) == ('completed', 0)


def test_finalize_classifies_over_cap_finishes_by_reason(loop_node: Node) -> None:
    """Over the cap, a finish lands by its reason: deliberate completes.

    The in-loop budget checks disarm once a finish signal exists, so a
    finish that crossed the cap reaches the terminal sweeps with the budget
    flag clear. A deliberate (non-budget-stemmed) finish keeps its goal-met
    ``completed`` landing with the overshoot recorded on the run row;
    budget-stemmed finishes fall through to the cascaded/parked sweeps,
    which book ``exited`` with their own figured attribution.
    """
    node = loop_node
    # deliberate finish over the cap: goal-met completed, overshoot recorded
    _configure(node, max_cost=5.0)
    loop = MockLoop(node)
    loop._run_id = node.record.run_start()
    node.record.signal_set('finish', 'requirements verified: all surfaces delivered')
    _record_step_cost(node, run_id=loop._run_id, cost=6.0)
    assert loop._finalize() == 0
    row = node.db.read('runs', where={'run_id': loop._run_id})[0]
    assert (row['status'], row['exit_code']) == ('completed', 0)
    assert row['metadata'] == (
        'cost budget exceeded in finish wind-down (spent $6.0000 >= $5.0 max)'
    )
    # a cascaded budget finish whose run also crossed its own cap books
    # exited via the cascaded sweep, with this run's own child-scope figures
    over = MockLoop(node)
    over._run_id = node.record.run_start()
    reason = (
        'cost budget reserve reached (spent $9 >= $5 max) (via finish of main.parent)'
    )
    node.record.signal_set('finish', reason)
    _record_step_cost(node, run_id=over._run_id, cost=6.0)
    assert over._finalize() == 0
    row = node.db.read('runs', where={'run_id': over._run_id})[0]
    assert (row['status'], row['exit_code']) == ('exited', 0)
    assert row['metadata'] == (
        f'ancestor budget abort: {reason}; this run spent $6.0000 of $5.0'
    )
    # a bare self-sent stem over the full cap lands via the parked sweep,
    # keeping its persisted reason
    bare = MockLoop(node)
    bare._run_id = node.record.run_start()
    reason = 'cost budget reserve reached (spent $6.0000 >= $5.0 max - $0.5 reserve)'
    node.record.signal_set('finish', reason)
    _record_step_cost(node, run_id=bare._run_id, cost=6.0)
    assert bare._finalize() == 0
    row = node.db.read('runs', where={'run_id': bare._run_id})[0]
    assert (row['status'], row['exit_code']) == ('exited', 0)
    assert row['metadata'] == reason
    # cascaded-budget sweep: an ancestor's propagated budget finish
    # reclassifies too, relabeled with this run's own child-scope figures
    # (the ancestor's figures name its scope, not this run's)
    cascaded = MockLoop(node)
    cascaded._run_id = node.record.run_start()
    reason = (
        'subtree cost budget reached (spent $9 >= $5 max) (via finish of main.parent)'
    )
    node.record.signal_set('finish', reason)
    _record_step_cost(node, run_id=cascaded._run_id, cost=1.0)
    assert cascaded._finalize() == 0
    row = node.db.read('runs', where={'run_id': cascaded._run_id})[0]
    assert (row['status'], row['exit_code']) == ('exited', 0)
    assert row['metadata'] == (
        f'ancestor budget abort: {reason}; this run spent $1.0000 of $5.0'
    )
    # parked-abort sweep: a self-sent reserve stop parked by a pause loses
    # the abort flags with the loop process, and the reserve threshold sits
    # below the over-cap sweep's full cap -- the resumed loop re-adopts the
    # abort from the persisted signal row
    _configure(node, max_cost=10.0)
    parked = MockLoop(node)
    parked._run_id = node.record.run_start()
    reason = 'cost budget reserve reached (spent $9.2000 >= $10.0 max - $1.0 reserve)'
    node.record.signal_set('finish', reason)
    _record_step_cost(node, run_id=parked._run_id, cost=9.2)
    assert parked._finalize() == 0
    row = node.db.read('runs', where={'run_id': parked._run_id})[0]
    assert (row['status'], row['exit_code']) == ('exited', 0)
    assert row['metadata'] == reason
    # a NON-budget cascaded finish stays a goal-met completion
    clean = MockLoop(node)
    clean._run_id = node.record.run_start()
    node.record.signal_set('finish', 'parent done (via finish of main.parent)')
    _configure(node, max_cost=None)
    clean._read_cost_caps()
    assert clean._finalize() == 0
    row = node.db.read('runs', where={'run_id': clean._run_id})[0]
    assert (row['status'], row['exit_code']) == ('completed', 0)
    # arrival order never decides: a cascade landing on top of a deliberate
    # goal-met finish books the same completed landing as the reverse order
    _configure(node, max_cost=5.0)
    raced = MockLoop(node)
    raced._run_id = node.record.run_start()
    node.record.signal_set('finish', 'requirements verified before the cascade')
    node.record.signal_set(
        'finish',
        'cost budget reserve reached (spent $9 >= $5 max) (via finish of main.parent)',
    )
    _record_step_cost(node, run_id=raced._run_id, cost=6.0)
    assert raced._finalize() == 0
    row = node.db.read('runs', where={'run_id': raced._run_id})[0]
    assert (row['status'], row['exit_code']) == ('completed', 0)
    assert row['metadata'] == (
        'cost budget exceeded in finish wind-down (spent $6.0000 >= $5.0 max)'
    )


def test_deliberate_finish_survives_a_wind_down_budget_trip(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A goal-met finish keeps ``completed`` through a wind-down budget trip.

    A node whose requirements verify during the wind-down finishes
    deliberately; spend then crosses the cap before the next probe, but the
    probes stay disarmed on the pending finish and the terminal sweep must
    not reclassify the goal-met run -- the overshoot rides the run row
    instead, and the deliberate reason survives on the signal row.
    """
    monkeypatch.setenv('_NODE', '')
    _configure(loop_node, max_cost=5.0)

    class _FinishThenOvershoot(MockLoop):
        """Signal a deliberate finish and cross the cap after step one."""

        def _launch(
            self: _FinishThenOvershoot,
            step: Step,
            prompt: str,
            *,
            agent: Any,
            budget: Optional[float],
        ) -> StepResult:
            result = super()._launch(step, prompt, agent=agent, budget=budget)
            if len(self.launched) == 1:
                self.node.record.signal_set('finish', 'requirements met')
                _record_step_cost(self.node, run_id=self._run_id, cost=6.0)
            return result

    loop = _FinishThenOvershoot(loop_node)
    assert loop.run() == 0
    run = loop_node.db.read('runs', where={'run_id': loop._run_id})[0]
    assert (run['status'], run['exit_code']) == ('completed', 0)
    assert run['metadata'] == (
        'cost budget exceeded in finish wind-down (spent $6.0000 >= $5.0 max)'
    )
    assert loop_node.status() == 'completed'
    # the deliberate reason survives on the append-only signal row
    finish_reason = loop_node.record.signal_get('finish', run_id=loop._run_id)
    assert finish_reason == 'requirements met'


def test_finalize_park_leaves_rows_open(
    loop_node: Node,
    capsys: pytest.CaptureFixture,
) -> None:
    """A pause park stamps ``paused`` and leaves run/iter rows open."""
    node = loop_node
    loop = MockLoop(node)
    loop._run_id = node.record.run_start()
    loop._iter_id = node.record.iter_start(run_id=loop._run_id, iter=1)
    loop._paused = True
    assert loop._finalize() == 0
    assert node.status() == 'paused'
    # both rows stay open for resume to adopt, and the pause span is recorded
    run = node.db.read('runs', where={'run_id': loop._run_id})[0]
    iteration = node.db.read('iters', where={'iter_id': loop._iter_id})[0]
    assert run['ended_at'] is None
    assert iteration['ended_at'] is None
    events = node.db.read('events', where={'event': 'pause'})
    assert events
    assert events[0]['metadata'] == 'parked'
    assert '=== Paused (resume with: fractal node resume) ===' in (
        capsys.readouterr().out
    )


def test_crash_exit_closes_the_open_iteration_and_step_rows(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-band crash settles every open row, not just the run's.

    An attached-pane Ctrl-C (or any unhandled error escaping mid-step)
    reaches the exit trap with the iteration and step rows still open.
    The trap must close all three tables with the terminal it stamps on
    ``.status``: nothing heals them later -- ``_reconcile_status`` no-ops
    once the status reads ``exited`` -- so a row left open here would
    read active forever.
    """
    monkeypatch.setenv('_NODE', '')
    loop = MockLoop(loop_node)

    def interrupt(step: Step, prompt: str, **kwargs: Any) -> StepResult:
        raise KeyboardInterrupt

    monkeypatch.setattr(loop, '_launch', interrupt)
    with pytest.raises(KeyboardInterrupt):
        loop.run()
    assert loop_node.status() == 'exited'
    # the run, its iteration, and the interrupted step all read the same
    # terminal -- no phantom active row survives the crash
    for table in ('runs', 'iters', 'steps'):
        rows = loop_node.db.read(table, where={'node': loop_node.branch})
        assert rows
        assert {row['status'] for row in rows} == {'exited'}
        assert all(row['ended_at'] is not None for row in rows)


# ------ hook pairings


def test_run_fires_hook_pairings_off_stdout(
    loop_node: Node,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full scripted run fires ordered pairings that never touch the pane.

    The transcript stays anchor-exact (the hooks emit only through the
    stdlib logger), each terminal event threads its initial event, and
    the step failure pairing carries the ``StepResult`` and a synthesized
    error while the iteration still closes with an honest attribution.
    """
    # the loop pins _NODE for its children; route it through monkeypatch so
    # the ambient environment is restored after the run
    monkeypatch.setenv('_NODE', '')
    caplog.set_level(logging.DEBUG)
    # this test pins the single-attempt event sequence, so the failed step
    # must not buy a retry
    _configure(loop_node, step_retries=0)
    results = [
        StepResult(status='completed'),
        StepResult(status='failed', exit_code=2, reason='agent error (exit 2)'),
    ]
    loop = TrackingLoop(loop_node, results=results)
    # the process exit is always clean; the failed final iteration's abnormal
    # outcome lands in the run row (asserted below), never a clean max-iters
    # completion
    assert loop.run() == 0
    captured = capsys.readouterr()
    out = captured.out

    # the transcript carries the loop banners, not the hooks' event text
    assert 'Starting node on' in out
    assert '=== Iteration 1 of 1' in out
    assert '--- Step 1/2 (PLAN) ---' in out
    assert '--- Step 2/2 (EXECUTE): exit 2 (' in out
    assert '=== Iteration 1 of 1 failed (' in out
    assert 'LOOP' not in out
    assert 'LOOP' not in captured.err
    assert 'LOOP_STEP_EVENT' in caplog.text
    # pytest's root capture handler masks the stdlib lastResort fallback, so
    # the no-handler stderr guard is asserted directly: the loop module ships
    # a NullHandler on its logger hierarchy, keeping a handlerless pane free
    # of raw event text
    assert any(
        isinstance(handler, logging.NullHandler)
        for handler in logging.getLogger('fractal.core.loop').handlers
    )

    # the pairings fire in order with initial_event/error threading
    names = [type(event).__name__ for event in loop.calls]
    assert names == [
        'LoopIterationEvent',
        'LoopStepEvent',
        'LoopStepSuccessEvent',
        'LoopStepEvent',
        'LoopStepFailureEvent',
        'LoopIterationFailureEvent',
    ]
    iteration, step_one, ok, step_two, failure, iter_failure = loop.calls
    # event payloads are snapshots (deep copies), so pair by creation instant
    assert ok.initial_event.created == step_one.created
    assert ok.result.status == 'completed'
    assert failure.initial_event.created == step_two.created
    assert failure.result.status == 'failed'
    assert isinstance(failure.error, RuntimeError)
    assert iter_failure.initial_event.created == iteration.created
    # the failed step's honest attribution reaches the rows too
    steps = loop_node.db.read('steps', where={'node': loop_node.branch})
    by_number = {row['step']: row for row in steps}
    assert by_number[1]['status'] == 'completed'
    assert by_number[2]['status'] == 'failed'
    assert by_number[2]['metadata'] == 'agent error (exit 2)'
    run = loop_node.db.read('runs', where={'run_id': loop._run_id})[0]
    assert run['status'] == 'exited'


def test_sync_launch_fires_step_pairing(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SYNC launches fire the on_step pairing too, via ``_sync``."""
    monkeypatch.setenv('_NODE', '')
    _seed_steps(loop_node, ['01-PLAN.md'])
    _configure(loop_node, sync=True)
    loop = TrackingLoop(loop_node)
    assert loop.run() == 0
    names = [type(event).__name__ for event in loop.calls]
    assert names == [
        'LoopIterationEvent',
        'LoopStepEvent',
        'LoopStepSuccessEvent',
        'LoopStepEvent',
        'LoopStepSuccessEvent',
        'LoopIterationSuccessEvent',
    ]
    # the sync's pairing carries its own label; the step follows with its own
    assert loop.calls[1].step == 'SYNC (before PLAN)'
    assert loop.calls[3].step == 'step 1 of 1 (PLAN)'


def test_run_fires_iteration_failure_on_unhandled_loop_error(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unhandled loop error fires the failure pairing, then propagates."""
    monkeypatch.setenv('_NODE', '')
    loop = TrackingLoop(loop_node)

    def explode() -> bool:
        raise RuntimeError('boom')

    monkeypatch.setattr(loop, '_iterate', explode)
    with pytest.raises(RuntimeError, match='boom'):
        loop.run()
    names = [type(event).__name__ for event in loop.calls]
    assert names == ['LoopIterationEvent', 'LoopIterationFailureEvent']
    iteration, failure = loop.calls
    assert failure.initial_event.created == iteration.created
    # the error payload is a snapshot (deep copy), so compare by text
    assert f'{failure.error}' == 'boom'
    # the crash path (the EXIT-trap mirror) still records the honest terminal
    assert loop_node.status() == 'exited'
    run = loop_node.db.read('runs', where={'run_id': loop._run_id})[0]
    assert (run['status'], run['metadata']) == ('exited', 'Loop exited abnormally')


def test_resume_preflight_abort_preserves_the_paused_run(node_with_db: Node) -> None:
    """A resume boot aborting preflight leaves its paused run adoptable.

    Pause froze the worktree and left the run open; a transient preflight
    failure on resume (a key unset in the new shell, the binary momentarily
    off PATH) must not close that run -- doing so would strand the frozen work
    behind ``--continue``'s discard. The node stays paused and resumable.
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.record.run_start()
    node.status_set('paused')
    loop = MockLoop(node, resume=True)
    with pytest.raises(_Abort):
        loop._abort_preflight('OPENROUTER_API_KEY is not set')
    # the paused run survives for resume to adopt; the node stays paused
    assert node.status() == 'paused'
    run = node.db.read('runs', where={'run_id': run_id})[0]
    assert run['ended_at'] is None


def test_resume_preflight_abort_recredits_the_reparked_wait(
    node_with_db: Node,
) -> None:
    """A failed resume boot re-opens the pause credit span it closed.

    ``Node._resume`` stamps a completed ``resume`` event when tmux comes up --
    before the loop's preflight -- which closes the pause credit span. If the
    preflight then aborts and re-parks the node, the wait until a fixed-
    environment resume would charge the run/iter deadlines, timing out the very
    recovery the guard preserves. The abort re-opens the span (a trailing
    ``pause`` event) so ``_pause_credit`` keeps crediting the re-parked wait.
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.record.run_start()
    node.status_set('paused')
    # the original pause opens a credit span; the failed resume's completed
    # event (stamped by Node._resume before the loop boots) closes it
    pause_id = node.record.event_start('pause')
    node.record.event_end(event_id=pause_id, status='completed')
    resume_id = node.record.event_start('resume')
    node.record.event_end(event_id=resume_id, status='completed')
    loop = MockLoop(node, resume=True)
    with pytest.raises(_Abort):
        loop._abort_preflight('OPENROUTER_API_KEY is not set')
    # the abort trails a second pause after the resume -- the span is re-opened,
    # so the credit walk's unmatched-trailing-pause branch keeps accruing
    events = node.db.read('events', where={'run_id': run_id})
    events.sort(key=lambda e: e['event_id'])
    credit_seq = [e['event'] for e in events if e['event'] in ('pause', 'resume')]
    assert credit_seq == ['pause', 'resume', 'pause']
    # and the node is still paused with its run adoptable (unchanged)
    assert node.status() == 'paused'
    assert node.db.read('runs', where={'run_id': run_id})[0]['ended_at'] is None


def test_resume_adopt_with_no_open_run_records_exited(node_with_db: Node) -> None:
    """A resume boot that finds no open run records exited, never wedging paused.

    Pause parks with the run open, but if that run was closed out of band the
    resume boot has nothing to adopt -- there is no paused run to preserve, so
    it must land a durable exited row (``--continue`` recovers), not stay
    paused with the diagnosis lost in the dying pane (only ``kill`` recovers).
    """
    node = node_with_db
    node.status_set('active')
    run_id = node.record.run_start()
    node.record.run_end(run_id=run_id, status='exited', exit_code=1)
    node.status_set('paused')
    loop = MockLoop(node, resume=True)
    with pytest.raises(_Abort):
        loop._adopt()
    # a durable exited record landed and the node is no longer wedged paused
    assert node.status() == 'exited'
    runs = node.db.read('runs')
    assert any(r['metadata'] == 'no open run to adopt' for r in runs)


def test_resume_anchors_run_deadline_on_credited_remaining(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resume re-arms the run wall on the credited remaining, not afresh.

    ``run`` anchors ``_run_end_epoch`` on ``time.remaining(scope='run')``
    only when resuming, so a run paused for part of its life resumes with
    exactly its unspent wall. Re-arming the full ``--timeout`` on every
    resume would let a node exceed its budget indefinitely across
    pause/resume cycles; anchoring on raw elapsed (uncredited) would
    expire the run the instant it resumes, killing the recovery the pause
    preserves. Deleting the resume branch or dropping the pause credit
    keeps every other test green, so this pins the anchor directly.
    """
    node = loop_node
    monkeypatch.setenv('_NODE', '')
    node.config.set('timeout', '10m')
    node.status_set('active')
    run_id = node.record.run_start()
    _age_run(node, run_id, 300.0)
    # a closed 180s pause span (240s ago -> 60s ago) credits 180s back
    for event, seconds_ago in (('pause', 240.0), ('resume', 60.0)):
        event_id = node.record.event_start(event, run_id=run_id)
        node.record.event_end(event_id=event_id, status='completed')
        node.db.update(
            data={'created_at': _past_timestamp(seconds_ago)},
            table='events',
            where={'event_id': event_id},
        )

    class _CaptureLoop(MockLoop):
        """Stop the run the instant it has anchored the deadline."""

        def _main_loop(self: _CaptureLoop) -> None:
            raise _Abort

    loop = _CaptureLoop(node, resume=True)
    before = int(time.time())
    assert loop.run() == 1
    # 600 - (300 elapsed - 180 credit) = 480 remaining, credited -- not the
    # full 600 (re-armed) nor 300 (uncredited)
    assert 465 <= loop._run_end_epoch - before <= 485


def test_interval_defaults_iter_timeout_but_honors_a_tighter_one(
    loop_node: Node,
) -> None:
    """Interval sets the iteration deadline only when none is given.

    An interval caps how long an iteration may run to its slot, so a
    bare ``--interval`` defaults the iteration deadline to the cadence.
    But an explicit tighter ``--iter-timeout`` is the operator asking for
    shorter iterations on that cadence -- it must be honored, never
    silently loosened back to the full interval.
    """
    node = loop_node
    # interval alone: the iteration deadline defaults to the slot
    _configure(node, interval='30m', iter_timeout=None)
    assert MockLoop(node)._iter_timeout_seconds == 1800
    # an explicit tighter iter_timeout survives (not widened to 30m)
    _configure(node, interval='30m', iter_timeout='1m')
    assert MockLoop(node)._iter_timeout_seconds == 60


def test_stop_during_the_inter_iteration_sleep_ends_the_run(
    loop_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop landing during the between-iterations sleep ends the run promptly.

    The sleep polls every signal, so a stop (or finish/pause) arriving
    while a paced node sleeps between iterations is acted on at the next
    chunk -- not ignored until the node wakes a full interval later and
    runs another iteration.
    """
    monkeypatch.setenv('_NODE', '')
    node = loop_node
    # a 5m sleep chunked at 30s = 10 chunks if the poll ignores the stop
    _configure(node, sleep='5m', max_iters=3)

    # the stop lands on the first sleep chunk: a signal-polling loop reacts at
    # the next chunk (1 chunk); a pause-only loop sleeps through all 10
    chunks = {'n': 0}

    def fake_sleep(_seconds: float) -> None:
        chunks['n'] += 1
        node.record.signal_set('stop', 'manual')

    monkeypatch.setattr('fractal.core.loop.time.sleep', fake_sleep)

    loop = MockLoop(node)
    assert loop.run() == 0
    # the sleep broke at the first chunk -- not slept through the whole window
    assert chunks['n'] == 1, chunks
    # one iteration ran, and the run ended stopped
    iters = node.db.read('iters', where={'node': node.branch})
    assert len(iters) == 1
    run = node.db.read('runs', where={'run_id': loop._run_id})[0]
    assert (run['status'], run['exit_code']) == ('stopped', 0)


# ------ helpers


def _configure(node: Node, **values: Any) -> None:
    """Merge ``values`` into the node's raw ``config.json``."""
    path = node.node_dir / 'config.json'
    config = json.loads(path.read_text(encoding='utf-8'))
    config.update(values)
    path.write_text(json.dumps(config, indent=2), encoding='utf-8')


def _locked_list(**kwargs: Any) -> list[Any]:
    """Fail the live subtree probe like a contended central-DB read."""
    raise sqlite3.OperationalError('database is locked')


def _record_unpriced_step(node: Node, *, run_id: int) -> None:
    """Record one ended NULL-cost step in ``run_id`` (its spend reads untracked)."""
    iter_id = node.record.iter_start(run_id=run_id, iter=1)
    step_id = node.record.step_start(
        iter_id=iter_id,
        run_id=run_id,
        step=1,
        step_name='PLAN',
    )
    node.record.step_end(step_id=step_id, status='exited', exit_code=1)


def _seed_steps(node: Node, names: list[str]) -> None:
    """Create a steps dir holding one trivial step file per name."""
    steps_dir = node.node_dir / 'steps'
    steps_dir.mkdir(parents=True, exist_ok=True)
    for existing in steps_dir.glob('*.md'):
        existing.unlink()
    for name in names:
        (steps_dir / name).write_text(f'# {name}\n\nWork.\n', encoding='utf-8')
