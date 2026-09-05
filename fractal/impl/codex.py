"""Codex CLI agent implementation."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import time
import tomllib
from typing import Any, Optional

import fractal.core.pricing
from fractal.core.agent import Agent, Invocation, StreamEvent, StreamParser

__all__ = [
    'CodexParser',
    'CodexAgent',
]

# codex exec has no fork (`exec resume` mutates the thread)
_NO_FORK = (
    'codex cannot fork a session (no `codex exec fork`):'
    ' use --session with --resume to continue one in place,'
    ' or omit --session/--current for a fresh thread.'
)

# bound the model preflight probe so a hung codex (network/auth stall)
# cannot wedge a run start
_PREFLIGHT_TIMEOUT = 60


class CodexParser(StreamParser):
    """Parser for codex ``exec --json`` output."""

    def __init__(self: CodexParser, *, model: Optional[str] = None) -> None:
        """Initialize ``CodexParser``.

        Bind the configured-model fallback and start the wall clock.
        """
        super().__init__(model=model)
        # codex reports no per-turn cost on the chat stream, so the result
        # closes on wall time
        self._started = time.monotonic()

    def feed(self: CodexParser, line: str) -> list[StreamEvent]:
        """Parse one codex JSONL line into normalized events."""
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
        # coerce nested payload fields with `or {}`/`or []`, not a .get
        # default: a present-null field is None (the default only fills an
        # absent key), so wire-noise tolerance must reach nested nulls too
        event_type = event.get('type')
        # capture the real session (codex calls it a thread id) for resume
        # and cost grouping -- it rides thread.started only
        if event_type == 'thread.started':
            session = event.get('thread_id')
            if session:
                self.session = session
                return [StreamEvent(kind='session', session=session, model=self.model)]
        # command executions map to tool headers
        elif event_type == 'item.started':
            item = event.get('item') or {}
            if item.get('type') == 'command_execution':
                return [StreamEvent(kind='tool', tool=item.get('command', '?'))]
        # agent messages
        elif event_type == 'item.completed':
            item = event.get('item') or {}
            if item.get('type') == 'agent_message' and item.get('text'):
                # codex sends whole messages, not deltas -- each closes a line
                return [StreamEvent(kind='text', text=item['text'] + '\n')]
        # turn summary -- usage accumulates within one invocation; resuming
        # the thread starts a new total. Keep the max within this stream: a
        # zero/empty terminal usage frame (codex emits usage:{} on some
        # error/cancel paths) must not erase known usage, nor move it off None
        # (a genuine turn always consumes tokens, so a computed 0.0 would
        # record a known $0 for an unknowable cost); flush per turn so a
        # stream killed by signal still has the last increment recorded
        elif event_type == 'turn.completed':
            events: list[StreamEvent] = []
            usage = event.get('usage') or {}
            cost = _compute_cost(usage, self.model)
            if cost and (self.cost is None or cost > self.cost):
                self.cost = cost
                events.append(StreamEvent(kind='cost', cost=self.cost))
            # the result closes on wall time and the recorded turn cost (no
            # authoritative cost rides the stream, so the running estimate --
            # None when unpriced -- is the turn's whole cost fact)
            wall = time.monotonic() - self._started
            events.append(StreamEvent(kind='result', cost=self.cost, duration=wall))
            return events
        # surface errors -- codex reports these on the JSON stream, not
        # stderr, so without this a failed turn leaves no explanation in the
        # output; the errors list fails the step after the stream drains
        elif event_type in ('error', 'turn.failed'):
            error = event.get('error')
            error_message = error.get('message') if isinstance(error, dict) else error
            detail = event.get('message') or error_message or 'unknown error'
            self.errors.append(str(detail))
            return [StreamEvent(kind='error', message=str(detail))]
        return []


class CodexAgent(Agent):
    """Codex CLI backend (token-priced, agent-minted threads, no fork).

    Todo:
        Support forking once codex ships ``codex exec fork``:
        https://github.com/openai/codex/issues/11750 and
        https://github.com/openai/codex/issues/17568.

    """

    name = 'codex'
    config_file = 'config.toml'
    can_fork = False
    mints_session = True
    needs_pricing = True
    cost_scope = 'call'
    enforces_budget = False
    providers = ('openrouter',)

    __parser__ = CodexParser

    def _invocation(
        self: CodexAgent,
        prompt: str,
        *,
        mode: str,
        session: Optional[str],
        model: Optional[str],
        effort: Optional[str],
        budget: Optional[float],
    ) -> Invocation:
        """Build the codex ``exec --json`` invocation."""
        # codex exec can resume a thread in place but cannot fork it
        if mode == 'fork':
            raise NotImplementedError(_NO_FORK)
        argv = [*self.parts, 'exec']
        # exec resume takes no -C -- the shell cwd carries the worktree
        if mode == 'resume':
            argv += ['resume', session]  # resume the thread in place
        else:
            argv += ['-C', f'{self.node.worktree}']  # fresh thread
        argv += ['--json']
        # route through openrouter: define the provider table inline so one
        # config template serves both routes and `config set provider=null`
        # takes effect next launch (values parse as TOML)
        if self.provider == 'openrouter':
            argv += [
                '-c',
                'model_provider="openrouter"',
                '-c',
                'model_providers.openrouter.name="OpenRouter"',
                '-c',
                'model_providers.openrouter.base_url="https://openrouter.ai/api/v1"',
                '-c',
                'model_providers.openrouter.env_key="OPENROUTER_API_KEY"',
            ]
        if model:
            argv += ['-m', model]
        # the -c override outranks the config.toml default
        if effort:
            argv += ['-c', f'model_reasoning_effort="{effort}"']
        # a '--' sentinel ends option parsing (codex exec uses clap), so a
        # message whose first char is '-' ('-1 on that', '--continue looks
        # right') is the message, never mistaken for a flag that fails the run
        # or silently flips a boolean option
        argv += ['--', prompt]
        # run in the worktree (the project): CODEX_HOME supplies
        # config/auth/skills, so the cwd is the project not the node dir; the
        # env carries only the reserved CODEX_HOME; invocation() composes it
        # over os.environ and the caller overlay
        env = {'CODEX_HOME': str(self.config_dir)}
        return Invocation(
            agent=self.name,
            argv=tuple(argv),
            cwd=self.node.worktree,
            env=env,
            session=session,
        )

    def _config_model(self: CodexAgent) -> Optional[str]:
        """Resolve the model codex's own config defaults to.

        Codex reads its ``CODEX_HOME`` ``config.toml`` (the node's
        ``.codex``); ``None`` when it names no top-level model.
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
        model = data.get('model')
        if isinstance(model, str) and model:
            return model
        return None

    def _transcript(self: CodexAgent, session: str) -> Optional[pathlib.Path]:
        """Newest dated rollout under the node's own codex home."""
        # codex rollouts nest by date under the node's own codex home
        sessions_dir = self.config_dir / 'sessions'
        found = sorted(sessions_dir.glob(f'*/*/*/rollout-*-{session}.jsonl'))
        if found:
            return found[-1]
        return None

    def _rates(self: CodexAgent, model: str) -> Optional[dict[str, Any]]:
        """Resolve pricing through the codex slug-alias chain."""
        return _rates(model)

    def _preflight(self: CodexAgent, model: Optional[str]) -> None:
        """Probe codex's acceptance of an explicit model for this account.

        Some codex accounts reject some explicit models (e.g. a
        ChatGPT-plan account returning a 400 for a model outside its
        entitlement; a cost cap forces an explicit model, but one can
        also be set without a cap); the pricing cache only proves the
        model priceable, not that codex accepts it. One bounded probe,
        built and spawned through the standard triads so a host's
        ``_spawn`` override covers it too; an uncapped codex with no
        model skips the probe and runs fine.
        """
        # the openrouter route runs on the key alone -- fail fast when the
        # environment cannot possibly authenticate
        if self.provider == 'openrouter' and not os.environ.get('OPENROUTER_API_KEY'):
            raise RuntimeError(
                'OPENROUTER_API_KEY is not set\n'
                'export it in the shell that runs fractal node start'
                ' (start.sh forwards it into the node tmux session)'
            )
        # only an explicit model can be rejected
        if model is None:
            return
        # capture the probe's output (codex emits the authoritative cause --
        # e.g. a 400 'model not supported with a ChatGPT account' -- on its
        # --json stream) so a rejection relays codex's reason rather than a
        # hedged guess, merging stderr in so it rides alongside
        invocation = self.invocation('reply with: ok', model=model)
        process = self.spawn(invocation, stderr=subprocess.STDOUT)
        try:
            output, _ = process.communicate(timeout=_PREFLIGHT_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            # a hung probe never responded -- distinct from an actual
            # rejection; the first line is the short reason callers persist
            process.kill()
            process.communicate()
            raise RuntimeError(
                'codex preflight timed out\n'
                f'after {_PREFLIGHT_TIMEOUT}s for model {model!r};'
                ' codex did not respond.'
            ) from e
        if process.returncode != 0:
            # lead with codex's own message (the authoritative cause), then a
            # neutral cause list -- the probe fails for auth, network,
            # rate-limit, or entitlement reasons, not only model rejection;
            # the first line is the short reason callers persist
            detail = f'\n{output.strip()}' if output.strip() else ''
            # name the openrouter causes when routed -- codex login advice
            # would misdirect a key problem
            if self.provider == 'openrouter':
                raise RuntimeError(
                    f'codex preflight failed for model {model!r}\n'
                    f'(exit {process.returncode}):{detail}\n'
                    "Check codex's output for the cause — common ones:"
                    ' an invalid or expired OPENROUTER_API_KEY (check the'
                    ' OpenRouter dashboard), account data-policy settings'
                    ' excluding the model, or a slug OpenRouter does not'
                    ' carry (use author-prefixed ids, e.g. openai/gpt-5.3-codex)'
                )
            raise RuntimeError(
                f'codex preflight failed for model {model!r}\n'
                f'(exit {process.returncode}):{detail}\n'
                "Check codex's output for the cause — common ones:"
                ' expired/invalid auth (re-run codex login), network or'
                ' rate-limit errors (retry), or a model unavailable to this'
                ' account (some ChatGPT-plan accounts lack access to some'
                ' models; API-key auth is an alternative)'
            )

    @classmethod
    def _seed(
        cls: type[CodexAgent],
        node_dir: pathlib.Path,
        *,
        parent_dir: Optional[pathlib.Path] = None,
    ) -> None:
        """Carry the parent's instructions file and link auth globally."""
        # carry the model instructions the inherited config names: codex
        # resolves a relative model_instructions_file against CODEX_HOME and
        # fails the run when the file is missing, so the file must travel
        # with the parent's copied config; an absolute path resolves the
        # same from every node and travels inside the config alone
        if parent_dir is not None:
            parent_config_dir = parent_dir / f'.{cls.name}'
            instructions = _config_instructions(parent_config_dir / cls.config_file)
            if instructions is not None and not instructions.is_absolute():
                source = parent_config_dir / instructions
                target = node_dir / f'.{cls.name}' / instructions
                # a missing source seeds nothing (the parent's own codex
                # fails the same way); an existing file is never overwritten
                if source.is_file() and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(source, target)
        # codex auth must stay global: CODEX_HOME points at this node dir, but
        # the credential is shared via a symlink to the global codex home --
        # codex writes auth.json in-place through the link (token refresh
        # updates the global file), so the secret is never copied into the node
        link = node_dir / f'.{cls.name}' / 'auth.json'
        if link.is_symlink():
            return
        global_home = os.environ.get('CODEX_HOME') or pathlib.Path.home() / '.codex'
        auth = pathlib.Path(global_home) / 'auth.json'
        # CODEX_HOME may be inherited from a parent node whose auth.json is
        # itself a symlink to the real ~/.codex/auth.json; canonicalize to
        # that real file so this link never dangles when an intermediate node
        # is reset or deleted (non-strict resolve canonicalizes a pre-auth
        # chain too, before the real file exists)
        auth = auth.resolve()
        link.symlink_to(auth)


# ------ helper functions


def _rates(model: str) -> Optional[dict[str, Any]]:
    """Pricing lookup chain: exact -> openrouter/-prefixed -> author-stripped.

    Native ids hit the exact key first; an openrouter slug
    (``openai/gpt-5.3-codex``) resolves via the LiteLLM ``openrouter/``
    prefix or, failing that, its bare model name -- an unmatched model
    returns ``None`` (unpriced), never a guessed entry.
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
    """Compute cost from codex token usage and LiteLLM pricing.

    Returns ``None`` if the model is unknown or unpriced. The usage shape is
    codex/OpenAI-specific (see the note below) -- a future token-reporting
    agent on a different convention needs its own cost helper.
    """
    if model is None:
        return None
    # look up per-token rates (cached input falls back to the input rate)
    rates = _rates(model)
    if rates is None:
        return None
    input_rate = rates.get('input_cost_per_token', 0.0)
    cached_rate = rates.get('cache_read_input_token_cost', input_rate)
    output_rate = rates.get('output_cost_per_token', 0.0)
    # codex reports OpenAI-style usage: cached_input_tokens is a subset of
    # input_tokens and reasoning is folded into output_tokens, so non-cached
    # input is input - cached and output is already whole; coerce each with
    # `or 0.0` (not a .get default), since a present-null bucket skips the
    # default and would poison the arithmetic
    input_tokens = usage.get('input_tokens') or 0.0
    cached_tokens = usage.get('cached_input_tokens') or 0.0
    output_tokens = usage.get('output_tokens') or 0.0
    # floor at 0: cached is a subset of input, but this is external stream data
    uncached = max(0.0, input_tokens - cached_tokens)
    return (
        uncached * input_rate
        + cached_tokens * cached_rate
        + output_tokens * output_rate
    )


def _config_instructions(config: pathlib.Path) -> Optional[pathlib.Path]:
    """Resolve the instructions file a codex config names.

    Codex reads ``model_instructions_file`` from its ``CODEX_HOME``
    ``config.toml``, resolving a relative value against ``CODEX_HOME``;
    ``None`` when it names none.
    """
    if not config.is_file():
        return None
    # an unreadable or malformed config names no instructions file
    try:
        with open(config, 'rb') as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    instructions = data.get('model_instructions_file')
    if isinstance(instructions, str) and instructions:
        return pathlib.Path(instructions)
    return None
