"""Grok Build CLI agent implementation."""

from __future__ import annotations

import json
import os
import pathlib
import time
import tomllib
import urllib.parse
from typing import Any, Optional

import fractal.core.pricing
from fractal.core.agent import Agent, Invocation, StreamEvent, StreamParser

__all__ = [
    'GrokParser',
    'GrokAgent',
]


class GrokParser(StreamParser):
    """Parser for grok ``--output-format streaming-json`` output."""

    def __init__(self: GrokParser, *, model: Optional[str] = None) -> None:
        """Initialize ``GrokParser``.

        Bind the configured-model fallback and start the wall clock.
        """
        super().__init__(model=model)
        # usage rides only the terminal end frame, so the close falls back
        # to wall time and a killed run records no cost (honest NULL)
        self._started = time.monotonic()

    def feed(self: GrokParser, line: str) -> list[StreamEvent]:
        """Parse one grok NDJSON event line into normalized events."""
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
        event_type = event.get('type')
        # assistant text deltas ride data; thought frames are reasoning
        # deltas and stay unrendered (claude's thinking is suppressed too);
        # the wire carries no tool frames at all -- grok executes tools
        # without reporting them, so steps emit no on_action events
        if event_type == 'text':
            text = event.get('data')
            if text:
                return [StreamEvent(kind='text', text=text)]
        # terminal frame -- session echo, cumulative usage, and the close
        elif event_type == 'end':
            events: list[StreamEvent] = []
            # the caller-minted id echoes back on sessionId; stamp it for the
            # record verbs (a killed run keeps the pre-stamped step row)
            if self.session is None:
                session = event.get('sessionId')
                if session:
                    self.session = session
                    # prefer the stream-reported model off the modelUsage key
                    usage_models = event.get('modelUsage')
                    if isinstance(usage_models, dict):
                        # the terminal frame is grok's only model report, and
                        # a multi-entry usage is ambiguous (an auxiliary model
                        # beside the serving one), so only a sole entry is a
                        # served-model fact; left empty the drop check reads
                        # "stream named none" and falls back to the step row
                        named = [model for model in usage_models if model]
                        if len(named) == 1:
                            self.models = named
                            self.model = named[0]
                    events.append(
                        StreamEvent(kind='session', session=session, model=self.model)
                    )
            # prefer the wire's authoritative spend (total_cost_usd, stamped
            # for API-key traffic) -- a pool/subscription model like
            # grok-build carries no LiteLLM price entry, so token pricing
            # would drop real spend to NULL; fall back to token pricing from
            # usage when the wire reports no cost; an absent (or present-null)
            # usage with no wire cost stays NULL (unpriced), never $0
            wire_cost = event.get('total_cost_usd')
            usage = event.get('usage')
            if isinstance(wire_cost, (int, float)):
                self.cost = float(wire_cost)
                self.final = True
                events.append(StreamEvent(kind='cost', cost=self.cost))
            elif usage:
                cost = _compute_cost(usage, self.model)
                if cost is not None:
                    self.cost = cost
                    self.final = True
                    events.append(StreamEvent(kind='cost', cost=self.cost))
            wall = time.monotonic() - self._started
            # the end frame is the invocation's one authoritative close
            events.append(
                StreamEvent(
                    kind='result',
                    cost=self.cost,
                    final=True,
                    duration=wall,
                    turns=event.get('num_turns'),
                )
            )
            return events
        # surface errors -- grok exits non-zero and mirrors the message to
        # stderr, but the errors list fails the step even on a clean drain
        elif event_type == 'error':
            events = []
            # a prompt-level failure freezes spend on the error frame and no end
            # frame follows, so this is the invocation's only cost fact -- record
            # it (wire figure preferred, else token pricing) before failing the
            # step, else the burned tokens land NULL and the retry re-burns unrecorded
            wire_cost = event.get('total_cost_usd')
            usage = event.get('usage')
            if isinstance(wire_cost, (int, float)):
                self.cost = float(wire_cost)
                events.append(StreamEvent(kind='cost', cost=self.cost))
            elif usage:
                cost = _compute_cost(usage, self.model)
                if cost is not None:
                    self.cost = cost
                    events.append(StreamEvent(kind='cost', cost=self.cost))
            detail = event.get('message') or 'unknown error'
            self.errors.append(str(detail))
            events.append(StreamEvent(kind='error', message=str(detail)))
            return events
        return []


class GrokAgent(Agent):
    """Grok Build CLI backend (cost-reporting, caller-minted sessions)."""

    name = 'grok'
    config_file = 'config.toml'
    can_fork = True
    mints_session = False
    # NOTE: cost-reporting: grok stamps total_cost_usd on the wire, and its pool
    #   model (grok-build) has no LiteLLM price entry, so token pricing would
    #   wedge the cost-cap pre-flight -- trust the wire (token pricing stays a
    #   best-effort parser fallback when the wire reports no cost, so the run
    #   start refreshes a day-old table)
    needs_pricing = False
    wants_pricing = True
    cost_scope = 'call'
    enforces_budget = False
    providers = ()

    __parser__ = GrokParser

    def _invocation(
        self: GrokAgent,
        prompt: str,
        *,
        mode: str,
        session: Optional[str],
        model: Optional[str],
        effort: Optional[str],
        budget: Optional[float],
    ) -> Invocation:
        """Build the grok ``--single`` streaming-json invocation."""
        argv = [
            *self.parts,
            # the attached = form: --single takes a value, and grok's clap
            # parser rejects a dash-leading value as a separate token, so a
            # message whose first char is '-' ('-1 on that', '--continue
            # looks right') would parse as a flag instead of the message
            f'--single={prompt}',
            '--output-format',
            'streaming-json',
            # unattended runs approve their own tools (the node seed sets the
            # permission mode too; the flag is the belt-and-braces)
            '--always-approve',
        ]
        if model:
            argv += ['-m', model]
        if effort:
            argv += ['--reasoning-effort', effort]
        # launch on the centrally minted id (-s names a NEW session, echoed
        # back on the end frame), resume in place, or fork to a grok-minted
        # id; grok keys sessions by working directory, and the worktree cwd
        # is stable across resumes
        if mode == 'fresh':
            argv += ['-s', session]
        elif mode == 'fork':
            argv += ['-r', session, '--fork-session']
        else:
            argv += ['-r', session]
        # run in the worktree: GROK_HOME supplies config/auth/sessions, so the
        # cwd is the project not the node dir; the env carries only the
        # reserved GROK_HOME; XAI_API_KEY or the linked OAuth store rides the
        # ambient environment invocation() composes under it
        env = {'GROK_HOME': str(self.config_dir)}
        return Invocation(
            agent=self.name,
            argv=tuple(argv),
            cwd=self.node.worktree,
            env=env,
            session=session,
        )

    def _config_model(self: GrokAgent) -> Optional[str]:
        """Resolve the model grok's own config defaults to.

        Grok reads its ``GROK_HOME`` ``config.toml`` (the node's
        ``.grok``); the default lives under ``[models] default``, not a
        top-level key; ``None`` when it names none.
        """
        config = self.config_dir / self.config_file
        if not config.is_file():
            return None
        # an unreadable or malformed config names no model
        try:
            with open(config, 'rb') as file:
                data = tomllib.load(file)
        except (OSError, tomllib.TOMLDecodeError):
            return None
        models = data.get('models')
        if not isinstance(models, dict):
            return None
        model = models.get('default')
        if isinstance(model, str) and model:
            return model
        return None

    def _transcript(self: GrokAgent, session: str) -> Optional[pathlib.Path]:
        """Expected events log under the node's own grok home.

        Grok keys sessions by the url-encoded absolute working directory
        (``sessions/<encoded-cwd>/<session>/events.jsonl``); returned even
        while absent, so a poller can wait for the file to appear.
        """
        slug = urllib.parse.quote(str(self.node.worktree), safe='')
        return self.config_dir / 'sessions' / slug / session / 'events.jsonl'

    def _rates(self: GrokAgent, model: str) -> Optional[dict[str, Any]]:
        """Resolve pricing through the grok slug-alias chain."""
        return _rates(model)

    @classmethod
    def _seed(
        cls: type[GrokAgent],
        node_dir: pathlib.Path,
        *,
        parent_dir: Optional[pathlib.Path] = None,
    ) -> None:
        """Link the node's grok auth through to the global home."""
        # grok auth must stay global: GROK_HOME points at this node dir, but
        # the credential is shared via a symlink to the global grok home --
        # grok writes auth.json in-place through the link (token refresh
        # updates the global file), so the secret is never copied into the node
        link = node_dir / f'.{cls.name}' / 'auth.json'
        if link.is_symlink():
            return
        global_home = os.environ.get('GROK_HOME') or pathlib.Path.home() / '.grok'
        auth = pathlib.Path(global_home) / 'auth.json'
        # GROK_HOME may be inherited from a parent node whose auth.json is
        # itself a symlink to the real ~/.grok/auth.json; canonicalize to
        # that real file so this link never dangles when an intermediate node
        # is reset or deleted (non-strict resolve canonicalizes a pre-auth
        # chain too, before the real file exists)
        auth = auth.resolve()
        link.symlink_to(auth)


# ------ helper functions


def _rates(model: str) -> Optional[dict[str, Any]]:
    """Resolve grok model rates: the exact key, then the LiteLLM xai/ prefix.

    LiteLLM prices xAI models under ``xai/``-prefixed keys while grok's
    own CLI takes bare ids (``grok-4.5``); an unmatched model returns
    ``None`` (unpriced), never a guessed entry.
    """
    rates = fractal.core.pricing.rates(model)
    return rates or fractal.core.pricing.rates(f'xai/{model}')


def _compute_cost(
    usage: dict[str, Any],
    model: Optional[str] = None,
) -> Optional[float]:
    """Compute cost from grok token usage and LiteLLM pricing.

    Returns ``None`` if the model is unknown or unpriced. The usage shape
    is grok-specific: ``cache_read_input_tokens`` is DISJOINT from
    ``input_tokens`` (anthropic-style) while ``reasoning_tokens`` folds
    into ``output_tokens`` (openai-style), so each bucket is priced once.
    """
    if model is None:
        return None
    # look up per-token rates through the alias chain
    rates = _rates(model)
    if rates is None:
        return None
    input_rate = rates.get('input_cost_per_token', 0.0)
    cache_read_rate = rates.get('cache_read_input_token_cost', input_rate)
    output_rate = rates.get('output_cost_per_token', 0.0)
    # coerce with `or 0.0`, not a .get default: a present-null usage key skips
    # the default (None would then poison the arithmetic)
    input_tokens = usage.get('input_tokens') or 0.0
    cache_read_tokens = usage.get('cache_read_input_tokens') or 0.0
    output_tokens = usage.get('output_tokens') or 0.0
    return (
        input_tokens * input_rate
        + cache_read_tokens * cache_read_rate
        + output_tokens * output_rate
    )
