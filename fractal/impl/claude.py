"""Claude Code CLI agent implementation."""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
from collections.abc import Callable
from typing import Any, Optional

import fractal.core.pricing
from fractal.core.agent import Agent, Invocation, StreamEvent, StreamParser

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

        Track the last-priced message id to skip per-block repeats.
        """
        super().__init__(model=model)
        # claude emits one assistant frame per content block, each repeating
        # the message-level usage, so a message id is priced only once
        self._priced_id: Optional[str] = None

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
        # assistant text deltas and tool-use headers
        if message_type == 'stream_event':
            event = message.get('event') or {}
            event_type = event.get('type')
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
            if message.get('parent_tool_use_id') is None:
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
            usage = inner.get('usage')
            # a frame with no id cannot be deduped, so it prices as it arrives
            message_id = inner.get('id')
            if usage and (message_id is None or message_id != self._priced_id):
                self._priced_id = message_id
                cost = self._message_cost(usage)
                if cost is not None:
                    self.cost = (self.cost or 0.0) + cost
                    events.append(StreamEvent(kind='cost', cost=self.cost))
        # result summary
        elif message_type == 'result':
            # coalesce a present-but-null duration_ms to 0.0 -- the key can be
            # explicitly null on some result frames, and `0.001 * None` raises
            duration = 0.001 * (message.get('duration_ms') or 0.0)
            cost = self._result_cost(message)
            if cost is not None:
                self.cost = cost
                self.final = True
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
                    message=detail,
                )
            )
        return events

    def _message_cost(self: ClaudeParser, usage: dict[str, Any]) -> Optional[float]:
        """Hook for an assistant frame's running cost estimate."""
        return _compute_cost(usage, self.model)

    def _result_cost(self: ClaudeParser, message: dict[str, Any]) -> Optional[float]:
        """Hook for the result frame's cost fact.

        Claude's ``total_cost_usd`` is per-invocation: each ``--resume``
        reports its own turns, not the thread's running total.
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
    ) -> Optional[float]:
        """Skip the gateway's zeroed assistant usage -- nothing to accrue."""
        return _compute_cost(usage, self.model) or None

    def _result_cost(
        self: _RoutedClaudeParser,
        message: dict[str, Any],
    ) -> Optional[float]:
        """Price the result frame's usage through the alias chain."""
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
    cost_scope = 'call'
    enforces_budget = True
    providers = ('openrouter',)

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
        # the user's own (auth and session storage untouched); the root node
        # seeds none, so a root chat runs on plain user defaults
        settings = self.config_dir / self.config_file
        if settings.is_file():
            argv += ['--settings', f'{settings}']
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

    def _config_model(self: ClaudeAgent) -> Optional[str]:
        """Resolve the model claude's own settings chain defaults to.

        Claude applies the node's ``--settings`` over the worktree's
        ``.claude/settings.local.json`` over its ``settings.json`` over
        ``~/.claude/settings.json``; the first file naming a model wins,
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
            # an unreadable or malformed settings file names no model
            try:
                with open(path, encoding='utf-8') as file:
                    model = json.load(file).get('model')
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(model, str) and model:
                return model
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

    def _rates(self: ClaudeAgent, model: str) -> Optional[dict[str, Any]]:
        """Resolve pricing through the claude slug-alias chain."""
        return _rates(model)

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
    ) -> None:
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
    """
    if model is None:
        return None
    # look up per-token rates (missing cache rates fall back to the input rate)
    rates = _rates(model)
    if rates is None:
        return None
    input_rate = rates.get('input_cost_per_token', 0.0)
    cache_read_rate = rates.get('cache_read_input_token_cost', input_rate)
    cache_creation_rate = rates.get('cache_creation_input_token_cost', input_rate)
    output_rate = rates.get('output_cost_per_token', 0.0)
    # coerce with `or 0.0`, not a .get default: the openrouter gateway sends
    # the cache buckets as explicit null on partial frames, and a present-null
    # key skips the default (None would then poison the arithmetic)
    input_tokens = usage.get('input_tokens') or 0.0
    cache_read_tokens = usage.get('cache_read_input_tokens') or 0.0
    cache_creation_tokens = usage.get('cache_creation_input_tokens') or 0.0
    output_tokens = usage.get('output_tokens') or 0.0
    return (
        input_tokens * input_rate
        + cache_read_tokens * cache_read_rate
        + cache_creation_tokens * cache_creation_rate
        + output_tokens * output_rate
    )
