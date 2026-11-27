"""Muse (``muse``) CLI agent implementation."""

from __future__ import annotations

import json
import os
import pathlib
import time
from typing import Optional

from fractal.core.agent import Agent, Invocation, StreamEvent, StreamParser

__all__ = [
    'MuseParser',
    'MuseAgent',
]

# muse exec has no fork (a later exec re-enters the session in place)
_NO_FORK = (
    'muse cannot fork a session (no `muse exec` fork):'
    ' use --session with --resume to continue one in place,'
    ' or omit --session/--current for a fresh session.'
)


class MuseParser(StreamParser):
    """Parser for ``muse exec --json`` output.

    The stream is the session event log mirrored to stdout: every frame
    carries the session in its ``stream`` envelope, and the payloads that
    matter are ``run.model.configured`` (served model), ``run.output.delta``
    (assistant text), ``tool.result`` (the only frame naming a tool),
    ``run.usage`` (per-model token totals), and the ``run.terminal`` close.
    """

    def __init__(self: MuseParser, *, model: Optional[str] = None) -> None:
        """Initialize ``MuseParser``.

        Bind the configured-model fallback and start the wall clock.
        """
        super().__init__(model=model)
        # muse reports no invocation duration, so the close falls back to
        # wall time (an exec launches and exits per invocation)
        self._started = time.monotonic()
        # per-model token totals from the latest run.usage frame; the stream
        # carries no USD figure at all
        self.tokens: dict[str, dict[str, int]] = {}

    def feed(self: MuseParser, line: str) -> list[StreamEvent]:
        """Parse one muse JSONL frame into normalized events."""
        # tolerate blank, malformed, and non-object lines (wire noise)
        line = line.strip()
        if not line:
            return []
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(event, dict):
            return []
        payload = event.get('payload')
        if not isinstance(payload, dict):
            return []
        events: list[StreamEvent] = []
        # every frame's envelope names the session (muse mints the id, or
        # adopts the caller-stamped --session-id); capture it once
        stream = event.get('stream')
        if isinstance(stream, dict) and stream.get('kind') == 'session':
            session = stream.get('id')
            if isinstance(session, str) and session and self.session is None:
                self.session = session
                events.append(
                    StreamEvent(kind='session', session=session, model=self.model)
                )
        event_type = event.get('payload_type')
        # the served model first arrives here -- re-stamp the session once it
        # is known so the step row records the real model (the configured
        # fallback may name no model at all)
        if event_type == 'run.model.configured':
            model = payload.get('model_id')
            if isinstance(model, str) and model:
                if model not in self.models:
                    self.models.append(model)
                if model != self.model:
                    self.model = model
                    if self.session is not None:
                        events.append(
                            StreamEvent(
                                kind='session', session=self.session, model=model
                            )
                        )
        # assistant text streams as output deltas
        elif event_type == 'run.output.delta':
            text = payload.get('text')
            if isinstance(text, str) and text:
                events.append(StreamEvent(kind='text', text=text))
        # the exec stream has no tool-call frame: a call surfaces only when
        # its result lands, named under correlation_facts (guarded like
        # every other field: a non-string name is wire noise, not a crash)
        elif event_type == 'tool.result':
            facts = payload.get('correlation_facts')
            tool = facts.get('tool_name') if isinstance(facts, dict) else None
            if not isinstance(tool, str) or not tool:
                tool = '?'
            events.append(StreamEvent(kind='tool', tool=tool))
        # token usage closes the run: totals are cumulative for the whole
        # invocation (children excluded), so the latest frame replaces, never
        # adds; no cost event is emitted -- there is no USD fact to report
        elif event_type == 'run.usage':
            models = payload.get('models')
            if isinstance(models, list):
                named: list[str] = []
                for entry in models:
                    if not isinstance(entry, dict):
                        continue
                    served = entry.get('model')
                    if not isinstance(served, str) or not served:
                        continue
                    named.append(served)
                    totals: dict[str, int] = {}
                    for key in (
                        'input_tokens',
                        'output_tokens',
                        'reasoning_tokens',
                        'cached_tokens',
                    ):
                        value = entry.get(key)
                        if isinstance(value, int) and not isinstance(value, bool):
                            totals[key] = value
                    self.tokens[served] = totals
                # a multi-entry usage is ambiguous (an auxiliary model beside
                # the serving one), so only a sole entry is a served-model
                # fact -- the drop check fails a pinned step on any other
                if len(named) == 1 and named[0] not in self.models:
                    self.models.append(named[0])
        # the terminal frame is the invocation's authoritative end; anything
        # but a completed terminal fails the step and names the reason.
        # Internal task.lifecycle.failed frames are NOT surfaced: sub-tasks
        # (reminders, judges) fail benignly inside healthy runs, and a real
        # failure still arrives here
        elif isinstance(event_type, str) and event_type.startswith('run.terminal.'):
            # one terminal closes the invocation; a duplicate is wire noise
            if self.final:
                return events
            self.final = True
            wall = time.monotonic() - self._started
            if payload.get('terminal') == 'completed':
                events.append(
                    StreamEvent(kind='result', cost=None, final=True, duration=wall)
                )
            else:
                detail = payload.get('reason') or 'muse run failed'
                self.errors.append(str(detail))
                events.append(StreamEvent(kind='error', message=str(detail)))
                events.append(
                    StreamEvent(
                        kind='result',
                        cost=None,
                        final=True,
                        duration=wall,
                        failed=True,
                    )
                )
        return events


class MuseAgent(Agent):
    """Muse backend (no USD reporting, caller-stamped sessions).

    muse's stream carries per-model token totals but no USD figure (a
    pay-as-you-go login's spend is real but unreported), so steps record
    no cost and ``records_cost`` is ``False`` -- every cost-cap path
    refuses a muse node instead of arming a ceiling that could never
    trip. The token
    totals ride the parser (and the session transcript); the ledger has
    no token column, so fractal persists none of them.
    """

    name = 'muse'
    # muse keeps its config, login, and sessions in the user's own XDG homes
    # and node pins travel as flags, so there is no per-node config file
    config_file = ''
    can_fork = False
    mints_session = False
    needs_pricing = False
    wants_pricing = False
    records_cost = False
    cost_scope = 'call'
    enforces_budget = False
    providers = ()
    effort_levels = (
        'none',
        'minimal',
        'low',
        'medium',
        'high',
        'xhigh',
        'max',
        'ultra',
    )
    effort_floor = 'high'

    __parser__ = MuseParser

    def _invocation(
        self: MuseAgent,
        prompt: str,
        *,
        mode: str,
        session: Optional[str],
        model: Optional[str],
        effort: Optional[str],
        budget: Optional[float],
    ) -> Invocation:
        """Build the ``muse exec --json`` invocation."""
        # muse exec cannot fork a session; a later exec re-enters it
        if mode == 'fork':
            raise NotImplementedError(_NO_FORK)
        # --yolo is fractal's unattended posture for every backend (an
        # approval prompt would hang a headless loop); it also drops muse's
        # own sandbox and trusts the workspace -- deliberate: node steps run
        # builds, tests, and git unrestricted, sandbox inits fail outright
        # on hosts whose kernel denies new namespaces, and the worktree --
        # not the sandbox -- is fractal's isolation boundary (see README)
        argv = [*self.parts, 'exec', '--json', '--yolo']
        argv += ['--workspace', str(self.node.worktree)]
        if model:
            argv += ['--model', model]
        if effort:
            argv += ['--reasoning-effort', effort]
        # the caller minted the id (fractal pre-stamps it on the step row);
        # muse adopts it fresh and re-enters the same session on resume
        if session:
            argv += ['--session-id', session]
        # a '--' sentinel ends option parsing, so a message whose first char
        # is '-' ('-1 on that', '--continue looks right') is the message,
        # never mistaken for a flag that fails the run
        argv += ['--', prompt]
        # run in the worktree with no reserved env keys: muse reads only the
        # generic XDG homes, and relocating those would relocate every tool
        # the agent runs (git, gh, uv) with them
        return Invocation(
            agent=self.name,
            argv=tuple(argv),
            cwd=self.node.worktree,
            session=session,
        )

    def _transcript(self: MuseAgent, session: str) -> Optional[pathlib.Path]:
        """No expected path: muse shards its session logs by date.

        The log lands at ``sessions/<y>/<m>/<d>/<session>/session.jsonl``
        under muse's data home, and the date folders are unknowable up
        front, so :meth:`_transcript_fallback` globs for it.
        """
        return None

    def _transcript_fallback(
        self: MuseAgent,
        session: str,
    ) -> Optional[pathlib.Path]:
        """Data-home-wide glob for the session log."""
        # the data home is the user's own, shared with interactive sessions
        # (the base gates this glob on the id being recorded for THIS node)
        data_home = os.environ.get('XDG_DATA_HOME')
        root = (
            pathlib.Path(data_home) / 'muse'
            if data_home
            else pathlib.Path.home() / '.local' / 'share' / 'muse'
        )
        found = sorted(root.glob(f'sessions/*/*/*/{session}/session.jsonl'))
        if found:
            return found[-1]
        return None
