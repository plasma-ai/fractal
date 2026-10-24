"""Claude Code CLI agent implementation."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
import re
import subprocess
from collections.abc import Callable, Sequence
from typing import Any, Optional

import fractal.core.pricing
from fractal.core.agent import (
    Agent,
    Invocation,
    Settlement,
    StreamEvent,
    StreamParser,
)

__all__ = [
    'ClaudeParser',
    'ClaudeAgent',
]

# the reserved routing keys the openrouter route injects into its spawn env;
# the native route pins them to None so invocation() pops any inherited copy
_ROUTING_KEYS = (
    'ANTHROPIC_BASE_URL',
    'ANTHROPIC_AUTH_TOKEN',
    'ANTHROPIC_API_KEY',
    'ANTHROPIC_DEFAULT_OPUS_MODEL',
    'ANTHROPIC_DEFAULT_SONNET_MODEL',
    'ANTHROPIC_DEFAULT_HAIKU_MODEL',
    'ANTHROPIC_DEFAULT_FABLE_MODEL',
    'ANTHROPIC_MODEL',
)

# the opening of the user turn claude injects to auto-continue a reply cut
# off at the output cap (its wording past these words varies by version)
_CONTINUATION_PREFIX = 'Output token limit hit'

# the content block types of a reply that is still thinking
_THINKING_BLOCKS = frozenset({'thinking', 'redacted_thinking'})

# the transcript entry types of the conversation's turns (the rest are
# claude's bookkeeping: attachments, queue operations, request records)
_TURNS = frozenset({'user', 'assistant'})

# a reply's final transcript line: its usage, model, and write instant
_Reply = tuple[dict[str, Any], Optional[str], dt.datetime]

# a price table key naming a model's long-prompt threshold, in thousands of
# tokens, above which the model's replies bill at higher rates
_LONG_PROMPT = re.compile(r'input_cost_per_token_above_(\d+)k_tokens')

# Claude Code's provider-outage shapes, as its failed result frame names them
# once its own API retries are spent: an `API Error: <status>` line (529
# overloaded, a 5xx, a 429 rate limit), its own `API Error:` text for a
# response stream that broke off (a lost connection, a server error, a stall),
# the API's error types, or the overloaded or service-unavailable wording
_UNAVAILABLE = re.compile(
    r'API Error:\s*(?:Repeated\s+)?(?:429|5\d\d)\b'
    r'|API Error:\s*(?:Connection lost|Server error mid-response)'
    r'|API Error:\s*The response (?:stopped arriving|stalled)'
    r'|\b(?:overloaded|rate_limit|api)_error\b'
    r'|\boverloaded\b'
    r'|\bservice unavailable\b',
    flags=re.IGNORECASE,
)

# the instruction files claude loads from each directory above its working
# directory, as claudeMdExcludes globs relative to that directory
_ANCESTOR_INSTRUCTIONS = (
    'CLAUDE.md',
    'CLAUDE.local.md',
    '.claude/CLAUDE.md',
    '.claude/rules/**',
    'AGENTS.md',
    '.claude/AGENTS.md',
)

# the instruction files claude loads from the user's config home, likewise
_USER_INSTRUCTIONS = (
    'CLAUDE.md',
    'rules/**',
)

# the openrouter route authenticates on the key alone
_NO_KEY = (
    'OPENROUTER_API_KEY is not set\n'
    'export it in the shell that runs fractal node start'
    ' (start.sh forwards it into the node tmux session)'
)


class ClaudeParser(StreamParser):
    """Parser for claude ``--output-format stream-json`` output."""

    def __init__(self: ClaudeParser, *, model: Optional[str] = None) -> None:
        """Initialize ``ClaudeParser``.

        Track the priced message ids to skip a reply's repeated frames.
        """
        super().__init__(model=model)
        # claude emits one assistant frame per content block, each repeating
        # the message-level usage, and parallel sub-agents interleave their
        # frames, so each message id is priced only once
        self._priced_ids: set[str] = set()
        # the newest top-level reply, the content block types across its
        # frames, and whether its thinking cap was reported -- the cap's two
        # signals (the API's own stop and claude's continuation) report once
        self._reply_id: Optional[str] = None
        self._reply_blocks: set[Optional[str]] = set()
        self._reply_capped = False
        # the newest top-level reply's model and usage as its raw API events
        # report them, and whether its closing delta reported a final figure
        self._reply_model: Optional[str] = None
        self._reply_usage: dict[str, Any] = {}
        self._reply_final = False
        # the top-level tool calls still awaiting their results, by id -- a
        # call can arrive on a raw block start, an assistant frame, or both
        self._pending: set[Optional[str]] = set()
        # the agent ended its turn (an end_turn stop or a result) and no new
        # reply has started -- only then is a wake-up its own
        self._turn_ended = False
        # a top-level thinking block is open -- the stream shows nothing of a
        # think in progress, so it holds the idle clock until it completes
        self._thinking = False
        # a result figure the baseline could not settle -- every later one of
        # the launch counts on from the same total, so it is refused too
        self._refused = False

    def feed(self: ClaudeParser, line: str) -> list[StreamEvent]:
        """Parse one claude stream line into normalized events."""
        # tolerate blank, malformed, and non-object lines (wire noise)
        line = line.strip()
        if not line:
            return []
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(message, dict):
            return []
        # coerce nested payload fields with `or {}`/`or []`, not a .get
        # default: a present-null field is None (the default only fills an
        # absent key), so wire-noise tolerance must reach nested nulls too
        events: list[StreamEvent] = []
        # capture the real session (carried on every claude event) off the first
        # event carrying one; prefer the stream-reported model over the configured
        # one, so a defaulted spawn (no --model) still stamps a recoverable model
        if self.session is None:
            session = message.get('session_id')
            if session:
                self.session = session
                self.model = message.get('model') or self.model
                events.append(
                    StreamEvent(kind='session', session=session, model=self.model)
                )
        message_type = message.get('type')
        top_level = message.get('parent_tool_use_id') is None
        # a sub-agent running past a result spends past its figure, which
        # only the settle books (see the assistant frames below)
        if self.final and not top_level:
            self.trailing = True
        # assistant text deltas and tool-use headers
        if message_type == 'stream_event':
            event = message.get('event') or {}
            event_type = event.get('type')
            # the raw API events track the top-level reply for the thinking
            # cap (its id, blocks and stop: a max_tokens stop is the primary
            # signal, landing before claude requests the continuation) and
            # for the idle clock (a new reply, a text block or a think's end
            # acts; a tool call, a think's start or a turn's end waits)
            if top_level:
                if event_type == 'message_start':
                    reply = event.get('message') or {}
                    self._track_reply(reply.get('id'))
                    self._reply_model = reply.get('model') or self.model
                    self._reply_usage = dict(reply.get('usage') or {})
                    self._turn_ended = False
                    self._thinking = False
                    events.append(StreamEvent(kind='acting'))
                elif event_type == 'content_block_start':
                    block = event.get('content_block') or {}
                    self._reply_blocks.add(block.get('type'))
                    if block.get('type') in _THINKING_BLOCKS:
                        self._thinking = True
                        events.append(StreamEvent(kind='waiting'))
                    events += self._block_progress(block)
                elif event_type == 'content_block_stop' and self._thinking:
                    self._thinking = False
                    events.append(StreamEvent(kind='acting'))
                elif event_type == 'message_delta':
                    self._thinking = False
                    # the closing delta's usage is the reply's final figure
                    # for each bucket it names, over the start's
                    usage = event.get('usage') or {}
                    if usage:
                        for bucket, count in usage.items():
                            if count is not None:
                                self._reply_usage[bucket] = count
                        self._reply_final = True
                    delta = event.get('delta') or {}
                    if delta.get('stop_reason') == 'max_tokens':
                        events += self._thinking_cap()
                    elif delta.get('stop_reason') == 'end_turn':
                        self._turn_ended = True
                        events.append(StreamEvent(kind='waiting'))
            if event_type == 'content_block_start':
                block = event.get('content_block') or {}
                if block.get('type') == 'tool_use':
                    events.append(StreamEvent(kind='tool', tool=block.get('name', '?')))
                elif block.get('type') == 'text':
                    # a new text block opens on a fresh line
                    events.append(StreamEvent(kind='text', text='\n'))
            elif event_type == 'content_block_delta':
                delta = event.get('delta') or {}
                if delta.get('type') == 'text_delta':
                    text = delta.get('text', '')
                    if text:
                        events.append(StreamEvent(kind='text', text=text))
        # tool results
        elif message_type == 'user':
            content = (message.get('message') or {}).get('content') or []
            # claude's own continuation backstops the API stop (assistant
            # frames carry a null stop_reason, so it is the only other sign)
            if top_level and _is_continuation(message):
                events += self._thinking_cap()
            # a result answering the last pending call, or any other turn
            # while no call is pending, hands the agent control
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'tool_result':
                    self._pending.discard(item.get('tool_use_id'))
            if top_level and not self._pending:
                events.append(StreamEvent(kind='acting'))
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'tool_result':
                    # extract text from tool result
                    result = item.get('content', '')
                    if isinstance(result, list):
                        parts = []
                        for block in result:
                            if isinstance(block, dict) and block.get('type') == 'text':
                                parts.append(block.get('text') or '')
                        result = '\n'.join(parts)
                    # boundary data: coerce an unexpected payload shape to text
                    if not isinstance(result, str):
                        result = str(result) if result is not None else ''
                    if result:
                        events.append(
                            StreamEvent(
                                kind='tool_result',
                                failed=bool(item.get('is_error', False)),
                                message=result,
                            )
                        )
        # price cost per message as it arrives: the authoritative result
        # frame never comes from a killed agent (when it does come, it
        # overwrites the estimate); usage is message-level and repeats on
        # every content block, so a message id is priced once
        elif message_type == 'assistant':
            inner = message.get('message') or {}
            # init names the model claude resolved, not the one served
            # (infrastructure substitutes silently): re-stamp from each real
            # top-level assistant row -- the row keeps the last served model,
            # `models` keeps every distinct one so a mid-stream recovery
            # stays visible; sidechain rows run their own model and synthetic
            # rows name none -- neither feeds the record
            if top_level:
                model = inner.get('model')
                real = isinstance(model, str) and model and model != '<synthetic>'
                if real and self.session is not None:
                    if model not in self.models:
                        self.models.append(model)
                    if model != self.model:
                        self.model = model
                        events.append(
                            StreamEvent(
                                kind='session',
                                session=self.session,
                                model=model,
                            )
                        )
                # one reply spans a frame per content block under one id
                self._track_reply(inner.get('id'))
                # claude writes a block's frame as the block completes, so a
                # frame landing while a think is open ends that think
                if self._thinking:
                    self._thinking = False
                    events.append(StreamEvent(kind='acting'))
                for block in inner.get('content') or []:
                    if isinstance(block, dict):
                        self._reply_blocks.add(block.get('type'))
                        events += self._block_progress(block)
            usage = inner.get('usage')
            # a frame with no id cannot be deduped, so it prices as it arrives
            message_id = inner.get('id')
            # past a result, a sub-agent's spend is the settle's to book from
            # its transcript (only a top-level reply reopens the figure), so
            # pricing its frames here too would count it twice
            if self.final and not top_level:
                usage = None
            if usage and (message_id is None or message_id not in self._priced_ids):
                if message_id is not None:
                    self._priced_ids.add(message_id)
                # a sub-agent's frame runs its own model, so each frame prices
                # at the model it names; one naming none prices at the
                # top-level model, and so does a synthetic reply's (claude's
                # stand-in for an API error), whose zeroed usage books a known $0
                model = inner.get('model')
                real = isinstance(model, str) and model != '<synthetic>'
                model = model if real else None
                cost = self._message_cost(usage, model=model)
                if cost is not None:
                    self.cost = (self.cost or 0.0) + cost
                    events.append(StreamEvent(kind='cost', cost=self.cost))
        # result summary
        elif message_type == 'result':
            # coalesce a present-but-null duration_ms to 0.0 -- the key can be
            # explicitly null on some result frames, and `0.001 * None` raises
            duration = 0.001 * (message.get('duration_ms') or 0.0)
            cost = self._result_cost(message)
            # a resumed session's figure counts on from its spend before the
            # launch, so the baseline comes off; a figure it cannot settle is
            # refused, and the running figure stays the stream's own estimate
            if cost is not None:
                baseline = self.baseline
                if self._refused or baseline is None or cost < baseline:
                    self._refused = True
                    cost = None
                else:
                    cost -= baseline
            if cost is not None:
                self.cost = cost
                self.final = True
                # the figure covers every sub-agent frame before it
                self.trailing = False
            # a --max-budget-usd hit is a clean budget stop, not an agent
            # error, even though claude exits non-zero on it
            subtype = message.get('subtype')
            budget_stopped = subtype == 'error_max_budget_usd'
            if budget_stopped:
                self.budget_stopped = True
            # surface the failure detail the result carries; claude reports
            # its failures via the exit code, so they never join the errors
            # list (only stream-borne failures fail an otherwise-clean drain)
            failed = bool(message.get('is_error')) or subtype != 'success'
            detail = None
            if failed:
                detail = str(message.get('result') or subtype or 'error')
            # num_turns can be explicitly null on some result
            # frames, like duration_ms above
            turns = message.get('num_turns') or 0
            events.append(
                StreamEvent(
                    kind='result',
                    cost=cost,
                    final=True,
                    duration=duration,
                    turns=turns,
                    budget_stopped=budget_stopped,
                    failed=failed,
                    refused=self._refused,
                    message=detail,
                )
            )
            # the process can outlive a result, waiting on background work
            self._turn_ended = True
            events.append(StreamEvent(kind='waiting'))
        # claude's own wake-ups after a turn's end (a background task's
        # notice, a fresh init) hand the agent control -- mid-turn, a notice
        # lands while the agent's own tool still runs; an API retry's backoff
        # is no time of the agent's
        elif message_type == 'system':
            subtype = message.get('subtype')
            if subtype in ('init', 'task_notification') and self._turn_ended:
                events.append(StreamEvent(kind='acting'))
            elif subtype == 'api_retry':
                delay = message.get('retry_delay_ms')
                if isinstance(delay, (int, float)) and delay > 0:
                    events.append(StreamEvent(kind='waiting', duration=0.001 * delay))
        return events

    def _block_progress(
        self: ClaudeParser,
        block: dict[str, Any],
    ) -> list[StreamEvent]:
        """Return the idle-clock event of one top-level content block.

        A tool call waits on its result (claimed once, from whichever of
        its block start or assistant frame comes first); visible text is
        the agent acting.
        """
        if block.get('type') == 'tool_use':
            call = block.get('id')
            if call in self._pending:
                return []
            self._pending.add(call)
            return [StreamEvent(kind='waiting')]
        if block.get('type') == 'text':
            return [StreamEvent(kind='acting')]
        return []

    def _track_reply(self: ClaudeParser, reply_id: Optional[str]) -> None:
        """Start tracking a new top-level reply when ``reply_id`` changes.

        A new reply follows every result the last one will get, so its
        unanswered calls are dropped.
        """
        if reply_id != self._reply_id:
            self._reply_id = reply_id
            self._reply_blocks = set()
            self._reply_capped = False
            self._reply_model = None
            self._reply_usage = {}
            self._reply_final = False
            self._pending.clear()
            # a reply after a result spends past the figure the result settled
            self.final = False

    def _thinking_cap(self: ClaudeParser) -> list[StreamEvent]:
        """Report the tracked reply's thinking cap, once, if it only thought.

        Claude's continuation drops the cut-off thinking, so a reply with no
        text and no tool call restarts from scratch and caps again. A
        reply whose closing delta reported its final usage rides the event
        in the transcript's message shape: the loop ends the launch at the
        cap, which can beat claude's own write of it.
        """
        blocks = self._reply_blocks
        if self._reply_capped or not blocks or not blocks <= _THINKING_BLOCKS:
            return []
        self._reply_capped = True
        reply = None
        if self._reply_final and self._reply_id is not None:
            reply = {
                'id': self._reply_id,
                'model': self._reply_model,
                'stop_reason': 'max_tokens',
                'usage': dict(self._reply_usage),
            }
        return [StreamEvent(kind='thinking_cap', session=self.session, reply=reply)]

    def _message_cost(
        self: ClaudeParser,
        usage: dict[str, Any],
        *,
        model: Optional[str] = None,
    ) -> Optional[float]:
        """Hook for an assistant frame's running cost estimate.

        ``model`` is the model the frame names (a sub-agent's frame names
        its own); ``None`` prices at the stream's top-level model.
        """
        model = model or self.model
        return _compute_cost(usage, model)

    def _result_cost(self: ClaudeParser, message: dict[str, Any]) -> Optional[float]:
        """Hook for the result frame's cost fact.

        Claude's ``total_cost_usd`` is the session's running total: a
        ``--resume`` counts on from the total its transcript saved (the
        baseline ``feed`` takes off), when it saved one.
        """
        cost = message.get('total_cost_usd')
        # guard the wire type the way the sibling backends do -- a non-numeric
        # figure would ride to the ledger and the renderer's :.4f format
        return cost if isinstance(cost, (int, float)) else None


class _RoutedClaudeParser(ClaudeParser):
    """Gateway-priced variant of ``ClaudeParser``.

    Behind an ``ANTHROPIC_BASE_URL`` gateway claude misprices unknown
    model slugs and zeroes assistant-frame usage, so the authoritative
    close is the result frame's usage priced through the slug-alias
    chain -- ``total_cost_usd`` is never recorded, and an unpriceable
    slug closes ``None`` (unpriced), never a wrong figure.
    """

    def _message_cost(
        self: _RoutedClaudeParser,
        usage: dict[str, Any],
        *,
        model: Optional[str] = None,
    ) -> Optional[float]:
        """Skip the gateway's zeroed assistant usage -- nothing to accrue."""
        model = model or self.model
        return _compute_cost(usage, model) or None

    def _result_cost(
        self: _RoutedClaudeParser,
        message: dict[str, Any],
    ) -> Optional[float]:
        """Price the result frame's usage through the alias chain.

        The stream carries no frame from a sub-agent, so the frame's
        ``modelUsage`` table, one entry per served model, is the only record
        of their tokens: each entry is priced at its own model and the close
        is their sum (``_model_usage_cost``). A model the chain cannot price
        closes ``None``, since a partial sum would read as a complete figure.
        """
        table = message.get('modelUsage')
        if isinstance(table, dict) and table:
            return _model_usage_cost(table)
        usage = message.get('usage')
        if not usage:
            return None
        return _compute_cost(usage, self.model)


class ClaudeAgent(Agent):
    """Claude Code CLI backend (cost-reporting, caller-minted sessions)."""

    name = 'claude'
    config_file = 'settings.json'
    can_fork = True
    mints_session = False
    needs_pricing = False
    wants_pricing = True
    cost_scope = 'call'
    enforces_budget = True
    reports_progress = True
    providers = ('openrouter',)
    effort_levels = ('low', 'medium', 'high', 'xhigh', 'max')
    effort_floor = 'high'

    __parser__ = ClaudeParser

    def parser(self: ClaudeAgent, *, model: Optional[str] = None) -> StreamParser:
        """Return the stream parser for the bound route."""
        if self.provider == 'openrouter':
            return _RoutedClaudeParser(model=model)
        return super().parser(model=model)

    def _invocation(
        self: ClaudeAgent,
        prompt: str,
        *,
        mode: str,
        session: Optional[str],
        model: Optional[str],
        effort: Optional[str],
        budget: Optional[float],
    ) -> Invocation:
        """Build the claude ``-p`` stream-json invocation."""
        argv = [
            *self.parts,
            '-p',
            '--output-format',
            'stream-json',
            '--include-partial-messages',
            '--verbose',
        ]
        if model:
            argv += ['--model', model]
        # the flag outranks the settings-file effortLevel
        if effort:
            argv += ['--effort', effort]
        # enforce the per-step USD budget when set; claude stops mid-turn and
        # emits a result subtype error_max_budget_usd on reaching it
        if budget is not None:
            argv += ['--max-budget-usd', f'{budget}']
        # launch on the centrally minted id (fresh sessions mint in the public
        # verb, detached steps included), resume in place, or fork to a new id
        if mode == 'fresh':
            argv += ['--session-id', session]
        elif mode == 'fork':
            argv += ['--resume', session, '--fork-session']
        else:
            argv += ['--resume', session]
        # node settings (permissions, model, env) ride the CLI flag -- they
        # outrank worktree and user settings, and claude's config home stays
        # the user's own (auth and session storage untouched); the flag
        # carries them composed with the instruction excludes as JSON, so the
        # node's file on disk stays as its author wrote it
        argv += ['--settings', json.dumps(self._settings())]
        # a '--' sentinel ends option parsing (claude's CLI uses commander),
        # so a message whose first char is '-' ('-1 on that', '--continue
        # looks right') is the message, never mistaken for a flag that fails
        # the run or silently flips a boolean option
        argv += ['--', prompt]
        # route through openrouter: the vendor env vars redirect the CLI at
        # its anthropic seam -- the base url has no /v1 (claude appends
        # /v1/messages), the key rides the bearer token, and the api key must
        # be explicitly empty; the model slots keep best/subagent references
        # resolving (the ~-prefixed ids are OpenRouter's own floating
        # latest-model aliases), and a model-less invocation pins an explicit
        # slug (process env beats the settings-file "model")
        env: dict[str, Optional[str]]
        if self.provider == 'openrouter':
            # the route runs on the key alone -- refuse a launch that cannot
            # possibly authenticate (boot preflight probes only the node's
            # own route, so a per-step provider rebind or a keyless chat
            # shell reaches here unprobed and would die on an opaque 401)
            key = os.environ.get('OPENROUTER_API_KEY')
            if not key:
                raise RuntimeError(_NO_KEY)
            # only the reserved ANTHROPIC_* keys; invocation() composes them
            # over os.environ and the caller overlay
            env = {
                'ANTHROPIC_BASE_URL': 'https://openrouter.ai/api',
                'ANTHROPIC_AUTH_TOKEN': key,
                'ANTHROPIC_API_KEY': '',
                'ANTHROPIC_DEFAULT_OPUS_MODEL': '~anthropic/claude-opus-latest',
                'ANTHROPIC_DEFAULT_SONNET_MODEL': '~anthropic/claude-sonnet-latest',
                'ANTHROPIC_DEFAULT_HAIKU_MODEL': '~anthropic/claude-haiku-latest',
                'ANTHROPIC_DEFAULT_FABLE_MODEL': '~anthropic/claude-fable-latest',
            }
            if model is None:
                env['ANTHROPIC_MODEL'] = 'anthropic/claude-sonnet-4.6'
        else:
            # natively pin every reserved routing key to None -- invocation()
            # pops them from the composed env, so a routed ancestor's spawn
            # env never silently reroutes a native descendant through the
            # gateway; the rest of the user's environment passes untouched
            env = dict.fromkeys(_ROUTING_KEYS)
        # the worktree is the cwd (the session slug loop forks resolve under)
        return Invocation(
            agent=self.name,
            argv=tuple(argv),
            cwd=self.node.worktree,
            env=env,
            session=session,
        )

    def _settings(self: ClaudeAgent) -> dict[str, Any]:
        """Compose the settings a launch passes: the node's file and excludes.

        Claude loads the instruction files (``CLAUDE.md`` and its kin) of
        every directory above its working directory and of the user's
        config home; a node's session keeps only its worktree's own, so
        each of those paths joins the ``claudeMdExcludes`` the node's file
        lists. A node with no settings file of its own (the root node seeds
        none) passes the excludes alone, and so does one whose file cannot
        be read as a JSON object, logging why -- claude, too, runs without
        a settings file it cannot load.
        """
        # read the node's own settings, a boundary file its author edits
        settings: dict[str, Any] = {}
        path = self.config_dir / self.config_file
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                loaded = None
            if isinstance(loaded, dict):
                settings = loaded
            else:
                self.log(
                    message=f'node settings not loaded: {path} does not hold'
                    ' a JSON object',
                    level=logging.WARNING,
                )
        excludes = settings.get('claudeMdExcludes')
        if not isinstance(excludes, list):
            excludes = []
        # exclude the user's instruction files (under the config home claude
        # itself reads) and those of every directory above the worktree --
        # its real path, the one claude walks up from
        home = os.environ.get('CLAUDE_CONFIG_DIR')
        config_dir = pathlib.Path(home) if home else pathlib.Path.home() / '.claude'
        excludes += [f'{config_dir / name}' for name in _USER_INSTRUCTIONS]
        for parent in self.node.worktree.parents:
            excludes += [f'{parent / name}' for name in _ANCESTOR_INSTRUCTIONS]
        return {**settings, 'claudeMdExcludes': excludes}

    def _config_model(self: ClaudeAgent) -> Optional[str]:
        """Resolve the model claude's own settings chain defaults to."""
        return self._setting('model')

    def _config_effort(self: ClaudeAgent) -> Optional[str]:
        """Resolve the effort claude's own settings chain defaults to."""
        return self._setting('effortLevel')

    def _setting(self: ClaudeAgent, key: str) -> Optional[str]:
        """Resolve one string setting through claude's own settings chain.

        Claude applies the node's ``--settings`` over the worktree's
        ``.claude/settings.local.json`` over its ``settings.json`` over
        ``~/.claude/settings.json``; the first file naming ``key`` wins,
        and ``None`` means none does (managed settings layers are not
        consulted -- the resolution is best-effort).
        """
        for path in (
            self.config_dir / self.config_file,
            self.node.worktree / '.claude' / 'settings.local.json',
            self.node.worktree / '.claude' / 'settings.json',
            pathlib.Path.home() / '.claude' / 'settings.json',
        ):
            if not path.is_file():
                continue
            # an unreadable or malformed settings file, or one whose JSON is
            # not an object, names no value
            try:
                with open(path, encoding='utf-8') as file:
                    settings = json.load(file)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(settings, dict):
                continue
            value = settings.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _transcript(self: ClaudeAgent, session: str) -> Optional[pathlib.Path]:
        """Expected transcript path under the user's claude config home.

        Returned even while absent, so a poller can wait for the file to
        appear.
        """
        # claude keys its projects dir by the agent cwd with every
        # non-alphanumeric character dashed; the config home is the user's own
        home = os.environ.get('CLAUDE_CONFIG_DIR')
        config_dir = pathlib.Path(home) if home else pathlib.Path.home() / '.claude'
        slug = re.sub(r'[^A-Za-z0-9]', '-', str(self.node.worktree))
        return config_dir / 'projects' / slug / f'{session}.jsonl'

    def _transcript_fallback(
        self: ClaudeAgent,
        session: str,
    ) -> Optional[pathlib.Path]:
        """Config-home-wide glob for a transcript off the expected slug."""
        # the slug rule is claude's own contract -- cover drift with a
        # config-home-wide glob (the base gates it on the id being recorded
        # for THIS node: an ungated glob would serve any session of the OS
        # user, from any project)
        home = os.environ.get('CLAUDE_CONFIG_DIR')
        config_dir = pathlib.Path(home) if home else pathlib.Path.home() / '.claude'
        found = sorted(config_dir.glob(f'projects/*/{session}.jsonl'))
        if found:
            return found[-1]
        return None

    def _settle_cost(
        self: ClaudeAgent,
        session: str,
        *,
        started: str,
        ended: str,
        after: Optional[str] = None,
        replies: Sequence[dict[str, Any]] = (),
    ) -> Settlement:
        """Price the launch's window from claude's own session transcripts.

        Claude writes each reply to the transcript as it streams, so a
        launch killed before its result frame still left each reply's
        final usage on disk, where the stream priced only each reply's
        first frame. A sub-agent's replies sit in a transcript of their own
        under the session's ``subagents`` folder (a workflow's agents a
        folder deeper), read beside the session's; one last written before
        the launch opened is skipped, and one that cannot be read is
        skipped with the reason logged. An id seen in more than one file
        counts once, at its latest line, and each prices at its own model:
        an unpriced model drops out of the sum, never the rest. An absent
        or unreadable session transcript, or a window with no priced
        usage, logs the reason and prices nothing.

        Whatever the figure leaves out marks the launch: an absent or
        unreadable session transcript on a launch with no result figure, a
        sub-agent transcript that cannot be read, a reply whose usage
        holds a count the pricing cannot read, a reply holding tokens at a
        model with no price, and a reply whose spend runs past the price
        table's rates (a web search's fee, the ``us`` geography's premium,
        fast mode's multiple, or a prompt above the model's long-prompt
        threshold).

        With ``after``, the result frame's figure stands and only the
        replies completing after it price: claude counts a reply's cost
        when it completes, so those are the spend past its figure, and
        finding none is the common case, never logged.

        The record leaves part of a reply out when any transcript's window
        reads that way (see ``_transcript_window``), so the figure is
        partial; with ``after``, only what follows the result counts, since
        the result's figure covers the rest. A reply the
        stream recorded whole (``replies``, the capped reply) prices at its
        recorded usage; one claude never wrote closes the session
        transcript's window, while a written one keeps the turns after it.
        """
        # past a result, a record the settle cannot read leaves only the
        # spend after it unseen
        unpriced = 'launch unpriced'
        level = logging.WARNING
        if after is not None:
            unpriced = 'spend after the result unpriced'
            level = logging.INFO
        # with no result figure, a record the settle cannot read leaves the
        # launch's spend out
        try:
            found = self.transcript(session)
        except (OSError, ValueError) as e:
            self.log(f'{unpriced}: transcript unreadable: {e}', level)
            return Settlement(cost=None, cut_off=after is None)
        if not found['exists']:
            self.log(
                message=f'{unpriced}: no transcript for session {session}',
                level=level,
            )
            return Settlement(cost=None, cut_off=after is None)
        # read each sub-agent's transcript beside the session's; a long
        # session keeps hundreds, so only those written since the launch
        # opened are read
        contents = [found['content']]
        subagents = pathlib.Path(found['path']).with_suffix('') / 'subagents'
        opened = dt.datetime.fromisoformat(started).timestamp()
        cut_off = False
        for path in sorted(subagents.rglob('agent-*.jsonl')):
            try:
                if path.stat().st_mtime < opened:
                    continue
                contents.append(path.read_text(encoding='utf-8', errors='replace'))
            except OSError as e:
                self.log(
                    message='launch partly unpriced: sub-agent transcript'
                    f' unreadable: {e}',
                    level=logging.WARNING,
                )
                cut_off = True
        # keep each reply's latest line across the files, and whether any
        # file's record leaves part of a reply out
        latest: dict[str, _Reply] = {}
        for index, content in enumerate(contents):
            # the stream's recorded replies are the session transcript's own
            recorded = replies if index == 0 else ()
            window, ends_cut = _transcript_window(
                content,
                started=started,
                ended=ended,
                after=after,
                recorded=recorded,
            )
            for message_id, reply in window.items():
                if message_id not in latest or latest[message_id][2] <= reply[2]:
                    latest[message_id] = reply
            cut_off = cut_off or ends_cut
        # a reply completing by the result was already in its figure
        since = dt.datetime.fromisoformat(after) if after is not None else None
        cost: Optional[float] = None
        for usage, model, stamped in latest.values():
            if since is not None and stamped <= since:
                continue
            # a zero figure is a missing one (a gateway zeroes its usage), not
            # a free reply -- it must not turn an unknowable launch into $0
            reply_cost, whole = _usage_cost(usage, model)
            cut_off = cut_off or not whole
            if reply_cost:
                cost = (cost or 0.0) + reply_cost
        if cost is None and after is None:
            self.log(
                message=f'launch unpriced: no priced usage in session {session}'
                f' between {started} and {ended}',
                level=logging.WARNING,
            )
        return Settlement(cost=cost, cut_off=cut_off)

    def _cost_baseline(self: ClaudeAgent, session: str) -> Optional[float]:
        """Read the spend a resumed session carries from its transcript.

        Claude Code writes a ``cost-state`` record to the session's
        transcript as each launch exits, and a resume restores the session's
        latest one, so the resumed launch's result figure counts on from its
        total. A transcript holding none (none written yet, or a version that
        writes none) carries nothing. Through openrouter the figure is the
        result's ``modelUsage`` table priced through the alias chain, and the
        record carries the same table, so its price is the baseline. A
        transcript that cannot be found or read, or a record whose figure
        cannot be read or priced, logs the reason and carries an unknown
        amount: claude resumes a session only from its transcript, so a
        missing one means the resolution here has lost track of claude's.
        """
        # an unknown baseline refuses the launch's result figure
        unknown = f'cost baseline of session {session} unknown'
        try:
            found = self.transcript(session)
        except (OSError, ValueError) as e:
            self.log(f'{unknown}: transcript unreadable: {e}', logging.WARNING)
            return None
        if not found['exists']:
            self.log(f'{unknown}: no transcript found', logging.WARNING)
            return None
        # a resume restores the latest record naming the session (claude, too,
        # skips a line it cannot parse)
        for line in reversed(found['content'].split('\n')):
            if 'cost-state' not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get('type') != 'cost-state':
                continue
            if entry.get('sessionId') != session:
                continue
            if self.provider == 'openrouter':
                table = entry.get('modelUsage')
                cost = _model_usage_cost(table) if isinstance(table, dict) else None
            else:
                cost = entry.get('totalCostUSD')
            if not isinstance(cost, (int, float)):
                self.log(
                    message=f'{unknown}: its cost state cannot be read or priced',
                    level=logging.WARNING,
                )
                return None
            return float(cost)
        return 0.0

    def _rates(self: ClaudeAgent, model: str) -> Optional[dict[str, Any]]:
        """Resolve pricing through the claude slug-alias chain."""
        return _rates(model)

    def _unavailable(self: ClaudeAgent, detail: str) -> bool:
        """Match claude's provider-outage shapes."""
        return _UNAVAILABLE.search(detail) is not None

    def tracks_cost(self: ClaudeAgent, model: Optional[str] = None) -> bool:
        """Return whether spend will be recorded.

        Natively claude reports its own figures on the stream; through
        openrouter the close is chain-priced from the result usage, so
        only a priced model tracks.
        """
        if self.provider == 'openrouter':
            return model is not None and self._rates(model) is not None
        return super().tracks_cost(model)

    def _preflight(
        self: ClaudeAgent,
        model: Optional[str],
        *,
        register: Optional[Callable[[subprocess.Popen], None]] = None,
    ) -> Optional[str]:
        """Probe the openrouter route's key presence before a run."""
        # the openrouter route runs on the key alone -- fail fast when the
        # environment cannot possibly authenticate
        if self.provider == 'openrouter' and not os.environ.get('OPENROUTER_API_KEY'):
            raise RuntimeError(_NO_KEY)


# ------ helper functions


def _rates(model: str) -> Optional[dict[str, Any]]:
    """Pricing lookup chain: exact -> openrouter/-prefixed -> author-stripped.

    Native ids hit the exact key first; an openrouter slug
    (``anthropic/claude-sonnet-4.6``) resolves via the LiteLLM
    ``openrouter/`` prefix or, failing that, its bare model name -- an
    unmatched model returns ``None`` (unpriced), never a guessed entry.
    """
    for key in (model, f'openrouter/{model}', model.partition('/')[2] or model):
        entry = fractal.core.pricing.rates(key)
        if entry is not None:
            return entry
    return None


def _is_continuation(message: dict[str, Any], /) -> bool:
    """Return whether a user frame is claude's output-cap continuation.

    Matched on the text alone: the flag claude stamps on its injected turns
    is undocumented (``isSynthetic`` on the stream, ``isMeta`` in the
    transcript), so a missing flag never disarms the match.
    """
    content = (message.get('message') or {}).get('content')
    # the stream carries text blocks; a bare string is the same turn's other shape
    if isinstance(content, str):
        return content.startswith(_CONTINUATION_PREFIX)
    for block in content or []:
        if isinstance(block, dict) and block.get('type') == 'text':
            text = block.get('text') or ''
            return text.startswith(_CONTINUATION_PREFIX)
    return False


def _transcript_window(
    content: str,
    *,
    started: str,
    ended: str,
    after: Optional[str] = None,
    recorded: Sequence[dict[str, Any]] = (),
) -> tuple[dict[str, _Reply], bool]:
    """Read the replies one transcript stamped within a window.

    Claude splits a reply across lines, one per content block under the
    reply's message id, each carrying the reply's usage as it stood when
    written; the last line of an id is its final usage. Each reply is
    kept at its final line, with the model it ran (a sidechain reply runs
    its own).

    The record leaves part of a reply out when a reply's final line has
    no stop reason, or when the window's last turn is a user turn whose
    reply's request was in flight. A final line with no stop reason was
    written before its reply completed: a killed reply, or a completed
    sub-agent's line written mid-stream, whose usage is then partial (its
    output tokens a handful on a full answer). A user turn's reply was in
    flight when the turn is a prompt, claude's continuation past the
    output cap, or the result of the newest reply's last open tool call;
    a result while other calls of that reply still run is a kill during a
    tool call, which is whole. A workflow sub-agent ends by design on the
    result of its ``StructuredOutput`` call, with no reply after it, which
    is whole too unless the result is an error (the agent retries). An
    entry the reader cannot read whole -- a reply's usage, or a tool id,
    of the wrong shape -- leaves its part out as well. With ``after``, the
    spend before that instant is already in a result frame's figure, so
    only what the window holds past it counts.

    Args:
        content: One transcript's raw JSONL text.
        started: The window's opening instant (ISO 8601 UTC).
        ended: The window's closing instant (ISO 8601 UTC).
        after: An instant inside the window (ISO 8601 UTC) whose spend a
            result frame already named, or ``None``.
        recorded: Replies recorded elsewhere, in the transcript's message
            shape, final over the transcript's lines of the same id; one
            the transcript does not hold is its newest turn (stamped at
            the window's close).

    Returns:
        Each reply's final line by message id, and whether the record
        leaves part of a reply out.

    """
    opened = dt.datetime.fromisoformat(started)
    closed = dt.datetime.fromisoformat(ended)
    since = dt.datetime.fromisoformat(after) if after is not None else None
    # keep the final line per message id (later lines overwrite earlier),
    # and its stop reason
    replies: dict[str, _Reply] = {}
    stops: dict[str, Optional[str]] = {}
    # the ids of the StructuredOutput calls, whose results end a sub-agent
    structured: set[str] = set()
    # the newest reply and its tool calls still awaiting their results
    newest: Optional[str] = None
    pending: set[str] = set()
    # whether the window's last turn leaves part of a reply out, and its stamp
    last: Optional[tuple[bool, dt.datetime]] = None
    # an entry past `since` the reader could not read whole
    malformed = False
    # a newline alone ends a record -- splitlines would also split at the
    # U+2028 and U+0085 claude leaves raw inside a record's strings
    for line in content.split('\n'):
        # tolerate malformed lines and a torn tail (the file may be mid-write)
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get('type') not in _TURNS:
            continue
        message = entry.get('message')
        if not isinstance(message, dict):
            continue
        blocks = message.get('content')
        blocks = blocks if isinstance(blocks, list) else []
        # place the entry against the window; a zone-less stamp cannot be
        # placed against the UTC window
        try:
            stamped = dt.datetime.fromisoformat(entry['timestamp'])
        except (KeyError, TypeError, ValueError):
            stamped = None
        inside = stamped is not None and stamped.tzinfo is not None
        inside = inside and opened <= stamped <= closed
        counted = inside and (since is None or stamped > since)
        if entry['type'] == 'assistant':
            # note the reply's tool calls, a StructuredOutput call even
            # before the window, since its result can land inside it
            message_id = message.get('id')
            calls: set[str] = set()
            for block in blocks:
                if not isinstance(block, dict) or block.get('type') != 'tool_use':
                    continue
                call = block.get('id')
                if not isinstance(call, str):
                    malformed = malformed or counted
                    continue
                calls.add(call)
                if block.get('name') == 'StructuredOutput':
                    structured.add(call)
            if not inside:
                continue
            if message_id != newest:
                newest = message_id
                pending = set()
            pending |= calls
            last = (False, stamped)
            # keep the reply's usage; a line with none recorded no spend
            usage = message.get('usage')
            if usage is None:
                continue
            if not isinstance(message_id, str) or not isinstance(usage, dict):
                malformed = malformed or counted
                continue
            model = message.get('model')
            model = model if isinstance(model, str) else None
            replies[message_id] = (usage, model, stamped)
            stops[message_id] = message.get('stop_reason')
            continue
        if not inside:
            continue
        # a user turn: a prompt or a continuation always awaits a reply
        results = [
            block
            for block in blocks
            if isinstance(block, dict) and block.get('type') == 'tool_result'
        ]
        if not results:
            last = (True, stamped)
            continue
        answers = [block.get('tool_use_id') for block in results]
        if not all(isinstance(answer, str) for answer in answers):
            malformed = malformed or counted
            last = (True, stamped)
            continue
        pending -= set(answers)
        # a clean StructuredOutput result ends a sub-agent; any other result
        # awaits a reply once the newest reply's calls have all returned
        ended_whole = all(
            answer in structured and block.get('is_error') is not True
            for answer, block in zip(answers, results)
        )
        last = (not ended_whole and not pending, stamped)
    # the stream's own record of a reply is final; one claude never wrote
    # is the newest turn, while a written one keeps the turns after it
    for message in recorded:
        if message['id'] not in replies:
            last = (False, closed)
        replies[message['id']] = (message['usage'], message['model'], closed)
        stops[message['id']] = message['stop_reason']
    partial = any(
        stops[message_id] is None and (since is None or reply[2] > since)
        for message_id, reply in replies.items()
    )
    unanswered = last is not None and last[0]
    unanswered = unanswered and (since is None or last[1] > since)
    return replies, partial or unanswered or malformed


def _model_usage_cost(table: dict[str, Any]) -> Optional[float]:
    """Price a ``modelUsage`` table: each entry at its own model, summed.

    An entry of the wrong shape, or a model the chain cannot price, prices
    the table ``None``, since a partial sum would read as a complete figure.
    """
    total = 0.0
    for model, entry in table.items():
        if not isinstance(entry, dict):
            return None
        cost = _compute_cost(_model_usage_tokens(entry), model)
        if cost is None:
            return None
        total += cost
    return total


def _model_usage_tokens(entry: dict[str, Any]) -> dict[str, Any]:
    """Read one ``modelUsage`` entry in the shape ``_compute_cost`` prices.

    The table names its buckets in camel case (``inputTokens``,
    ``cacheReadInputTokens``, ``cacheCreationInputTokens``,
    ``outputTokens``); the per-message ``usage`` shape names the same
    buckets in snake case.
    """
    # the table has no 5m/1h split, so every cache write prices at the base rate
    return {
        'input_tokens': entry.get('inputTokens'),
        'cache_read_input_tokens': entry.get('cacheReadInputTokens'),
        'cache_creation_input_tokens': entry.get('cacheCreationInputTokens'),
        'output_tokens': entry.get('outputTokens'),
    }


def _compute_cost(
    usage: dict[str, Any],
    model: Optional[str] = None,
) -> Optional[float]:
    """Compute cost from claude token usage and LiteLLM pricing.

    Returns ``None`` if the model is unknown or unpriced. The usage shape
    is Anthropic-specific: ``input_tokens`` EXCLUDES the cache buckets
    (``cache_creation_input_tokens``/``cache_read_input_tokens`` are
    disjoint, each priced at its own rate), and every assistant message
    reports its own API call -- per-message costs sum to the invocation
    total (the parser prices each message id once, since claude repeats a
    frame per content block), unlike codex's cumulative snapshots.

    Cache writes bill by their lifetime: the ``cache_creation`` split's
    one-hour writes price at the one-hour write rate, and the five-minute
    writes plus any the split leaves unnamed at the base write rate. A
    model with no one-hour rate prices them all at the base rate. A count
    that is not a number prices as nothing (``_usage_cost`` reports it).
    """
    cost, _ = _usage_cost(usage, model)
    return cost


def _usage_cost(
    usage: dict[str, Any],
    model: Optional[str] = None,
) -> tuple[Optional[float], bool]:
    """Price claude token usage, and say whether every count was priced.

    The pricing behind ``_compute_cost``. A count present but not a number
    (wire noise) is left out of the cost rather than raising, so the
    figure that results is partial. A model with no price (or none named)
    prices nothing, which leaves out every count above zero. A web search
    (``server_tool_use.web_search_requests``) bills a fee the price table
    does not carry, so a reply that searched is never whole. Neither is one
    whose spend runs past the table's rates: a reply served in the ``us``
    inference geography, billed at a premium, one served in fast mode
    (``speed: "fast"``), billed at a multiple of the model's rates, or one
    whose prompt is above a long-prompt threshold the model's entry names,
    billed at higher rates (the ``_above_<N>k_tokens`` keys, not priced
    here).

    Returns:
        The cost (``None`` for an unknown or unpriced model), and whether
        every count the usage holds was priced.

    """
    # read each count, leaving out one that is not a number; coerce null
    # to 0.0, not via a .get default: the openrouter gateway sends the
    # cache buckets as explicit null on partial frames, and a present-null
    # key skips the default (None would then poison the arithmetic)
    whole = True

    def count(value: Any) -> float:
        nonlocal whole
        if value is None:
            return 0.0
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            whole = False
            return 0.0
        return float(value)

    input_tokens = count(usage.get('input_tokens'))
    cache_read_tokens = count(usage.get('cache_read_input_tokens'))
    cache_creation_tokens = count(usage.get('cache_creation_input_tokens'))
    output_tokens = count(usage.get('output_tokens'))
    # the one-hour share of the cache writes, never more than their total
    split = usage.get('cache_creation')
    cache_hour_tokens = 0.0
    if isinstance(split, dict):
        cache_hour_tokens = count(split.get('ephemeral_1h_input_tokens'))
        cache_hour_tokens = min(cache_hour_tokens, cache_creation_tokens)
    elif split is not None:
        whole = False
    # claude bills each web search a fee beside the tokens, which the price
    # table does not carry; a web fetch bills only its tokens
    tools = usage.get('server_tool_use')
    if isinstance(tools, dict):
        if count(tools.get('web_search_requests')):
            whole = False
    elif tools is not None:
        whole = False
    # claude bills a reply served in the us inference geography at a premium
    # over its token cost, which the price table does not carry
    if usage.get('inference_geo') == 'us':
        whole = False
    # claude bills a reply served in fast mode at a multiple of its model's
    # rates, which the price table does not carry either
    if usage.get('speed') == 'fast':
        whole = False
    # look up per-token rates (missing cache rates fall back to the input rate);
    # with none, every count is unpriced, though a usage of zeros (a synthetic
    # reply's) holds nothing to price
    rates = _rates(model) if model is not None else None
    if rates is None:
        tokens = (
            input_tokens + cache_read_tokens + cache_creation_tokens + output_tokens
        )
        return None, whole and not tokens
    # a prompt (its input with the cache reads and writes, as claude counts
    # it) above a threshold the table names bills at long-prompt rates,
    # which are not priced here
    prompt = input_tokens + cache_read_tokens + cache_creation_tokens
    for key in rates:
        matched = _LONG_PROMPT.fullmatch(key)
        if matched and prompt > 1_000 * int(matched[1]):
            whole = False
    input_rate = rates.get('input_cost_per_token', 0.0)
    cache_read_rate = rates.get('cache_read_input_token_cost', input_rate)
    cache_creation_rate = rates.get('cache_creation_input_token_cost', input_rate)
    cache_hour_rate = rates.get(
        'cache_creation_input_token_cost_above_1hr',
        cache_creation_rate,
    )
    output_rate = rates.get('output_cost_per_token', 0.0)
    cost = (
        input_tokens * input_rate
        + cache_read_tokens * cache_read_rate
        + (cache_creation_tokens - cache_hour_tokens) * cache_creation_rate
        + cache_hour_tokens * cache_hour_rate
        + output_tokens * output_rate
    )
    return cost, whole
