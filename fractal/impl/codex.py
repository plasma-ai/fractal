"""Codex CLI agent implementation."""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import tomllib
import typing
import weakref
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any, BinaryIO, Optional

import fractal.core.pricing
import fractal.util
from fractal.core.agent import (
    Agent,
    Invocation,
    Settlement,
    StreamEvent,
    StreamParser,
)

if typing.TYPE_CHECKING:
    from fractal.core.node import Node

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
# TERM-to-KILL grace for the probe's process tree -- mirrors the loop's
# _KILL_GRACE_SECONDS for a step's
_PREFLIGHT_GRACE = 10

# the usage buckets a rollout counter carries (cached input and reasoning
# output are subsets of input and output; a bucket a record omits reads 0)
_BUCKETS = (
    'input_tokens',
    'cached_input_tokens',
    'output_tokens',
    'reasoning_output_tokens',
    'cache_write_input_tokens',
)

# the record kinds a rollout window reads; a line carrying none of their quoted
# names (message bodies, compacted history) is skipped undecoded
_KINDS = ('event_msg', 'turn_context', 'token_usage_record')
_MARKERS = tuple(f'"{kind}"'.encode() for kind in _KINDS)
# the read size for hashing a resumed rollout's bound prefix
_CHUNK = 1 << 20
# refusal for a new or grown rollout the spawn scan can bind to no thread --
# its unplaceable-rollout sites all raise it, so the wording stays identical
_UNEXPLAINED = 'Rollout window saw a rollout it cannot explain: {name!r}'

# codex's provider-outage shapes, as its error frames name them: the give-up
# after its own reconnects (the last `Reconnecting... N/N`, then a bare
# `stream disconnected before completion`), a 429 or 5xx status, or the
# overloaded/unavailable/at-capacity wording -- an earlier reconnect, which
# carries the disconnect in parentheses, is a retry codex may still recover from
_UNAVAILABLE = re.compile(
    r'(?<!\()stream disconnected before completion'
    r'|reconnecting(?:\.{3}|\u2026)\s*(\d+)/\1\b'
    r'|(?:unexpected status|last status:?)\s*(?:429|5\d\d)\b'
    r'|\b(?:429|5\d\d)\s+(?:too many requests|internal server error|bad gateway'
    r'|service unavailable|gateway timeout)\b'
    r'|\bservice unavailable\b'
    r'|\boverloaded\b'
    r'|\bat capacity\b'
    r'|experiencing high demand',
    flags=re.IGNORECASE,
)

# the rollout window captured before each spawn, keyed by its live process --
# an entry dies with the Popen, so a stream abandoned before finish_stream
# leaks nothing and a reused pid cannot inherit a stale window
_WINDOWS: weakref.WeakKeyDictionary[subprocess.Popen, UsageWindow] = (
    weakref.WeakKeyDictionary()
)


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
        self._threads = 0
        self._turns = 0
        self._completed = 0
        self._wire_usage: Any = None
        # error frames seen inside the active turn -- codex retries a stream
        # error behind one and completes the turn (recover_errors)
        self._provisional_errors = 0

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
            self._threads += 1
            if isinstance(session, str) and session:
                self.session = session
                return [StreamEvent(kind='session', session=session, model=self.model)]
        # turn starts feed the one-complete-turn shape check in turn_usage()
        elif event_type == 'turn.started':
            self._turns += 1
        # command executions map to tool headers
        elif event_type == 'item.started':
            item = event.get('item') or {}
            if not isinstance(item, dict):
                return []
            if item.get('type') == 'command_execution':
                command = item.get('command', '?')
                if not isinstance(command, str):
                    return []
                return [StreamEvent(kind='tool', tool=command)]
        # agent messages
        elif event_type == 'item.completed':
            item = event.get('item') or {}
            if not isinstance(item, dict):
                return []
            if item.get('type') == 'agent_message' and item.get('text'):
                if not isinstance(item['text'], str):
                    return []
                # codex sends whole messages, not deltas -- each closes a line
                return [StreamEvent(kind='text', text=item['text'] + '\n')]
        # exec totals span every earlier invocation of a resumed thread, so the
        # turn is priced from the post-exit rollout window (_finish_stream);
        # the completion count lets turn_usage() prove the stream described
        # one complete turn
        elif event_type == 'turn.completed':
            self._completed += 1
            self._wire_usage = event.get('usage')
        # surface errors -- codex reports these on the JSON stream, not
        # stderr, so without this a failed turn leaves no explanation in the
        # output; an error frame inside the active turn may be a retried
        # stream error, which recover_errors() settles once codex has exited
        elif event_type in ('error', 'turn.failed'):
            error = event.get('error')
            error_message = error.get('message') if isinstance(error, dict) else error
            detail = event.get('message') or error_message or 'unknown error'
            self.errors.append(str(detail))
            active = (self._threads, self._turns, self._completed) == (1, 1, 0)
            if (event_type == 'error') and active:
                self._provisional_errors += 1
            return [StreamEvent(kind='error', message=str(detail))]
        return []

    def recover_errors(self: CodexParser) -> None:
        """Clear the error frames one completed turn recovered from.

        Codex reports a retried stream error and a fatal one on the same
        ``error`` frame and tells them apart by exit status alone (a fatal
        error, a failed turn, or an interrupted turn exits 1), so the
        caller invokes this only after observing exit zero: when every
        recorded error is an ``error`` frame inside the one active turn
        and the stream describes that turn completing, the errors clear.
        A ``turn.failed`` frame, an error outside the turn, or an
        incomplete turn keeps them.
        """
        if not self.errors or (len(self.errors) != self._provisional_errors):
            return
        try:
            self.turn_usage()
        except ValueError:
            return
        self.errors.clear()
        self._provisional_errors = 0

    def turn_usage(self: CodexParser) -> Any:
        """Return the wire usage of the stream's one complete turn.

        Raises:
            ValueError: Unless the stream opened one thread that named its
                id, started one turn and completed it.

        """
        shape = (self._threads, self._turns, self._completed)
        if (shape != (1, 1, 1)) or (self.session is None):
            raise ValueError(
                'stdout does not describe one complete turn'
                f' (threads={self._threads}, turns={self._turns},'
                f' completed={self._completed}, session={self.session!r}).'
            )
        return self._wire_usage

    def finish(self: CodexParser) -> list[StreamEvent]:
        """Close the drained stream on its settled cost and wall time.

        The close is final only when it priced the turn; an unpriced one
        (a non-zero exit, usage it cannot bind) leaves the cost open to
        ``Agent.settle_cost``.
        """
        wall = time.monotonic() - self._started
        self.final = self.cost is not None
        return [StreamEvent(kind='result', cost=self.cost, final=True, duration=wall)]


class CodexAgent(Agent):
    """Codex CLI backend (token-priced, agent-minted threads, no fork).

    A launch that exits non-zero -- failed or killed -- is priced from its
    rollout window once it has exited (``_settle_cost``), so a failed
    attempt records its spend.

    Todo:
        Support forking once codex ships ``codex exec fork``:
        https://github.com/openai/codex/issues/11750 and
        https://github.com/openai/codex/issues/17568.

        Report the agent's control (``reports_progress``) from the exec
        stream, so the no-progress watchdog arms for codex steps.

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

    def __init__(
        self: CodexAgent,
        node: Node,
        command: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> None:
        """Initialize ``CodexAgent``.

        Bind the agent, then open the store of windows awaiting a settle.
        """
        super().__init__(node, command, provider)
        # the rollout window of each launch that exited non-zero, keyed by its
        # thread -- the post-exit settle takes it, since its stream priced
        # nothing
        self._unsettled: dict[str, UsageWindow] = {}

    def _spawn(
        self: CodexAgent,
        invocation: Invocation,
        **kwargs: Any,
    ) -> subprocess.Popen:
        """Capture the rollout window, then spawn codex.

        The window bounds the thread's rollout and every sub-agent rollout
        the step opens or grows, so the step's figure includes its
        children's spend. A host override of ``_spawn`` must delegate here,
        or the step records no cost.
        """
        window = UsageWindow.capture(invocation)
        process = super()._spawn(invocation, **kwargs)
        _WINDOWS[process] = window
        return process

    def _finish_stream(
        self: CodexAgent,
        parser: StreamParser,
        process: Optional[subprocess.Popen],
    ) -> list[StreamEvent]:
        """Price the invocation from its rollout window once codex exits 0.

        A non-zero exit is the loop's to attribute, so the stream closes
        unpriced and silent, keeping the window for the post-exit settle
        (``_settle_cost``); a clean exit first clears the error frames the
        completed turn recovered from (``recover_errors``), then prices; a
        clean exit whose usage cannot be bound to the process logs the
        reason at WARNING and leaves the cost ``None``.
        """
        parser = typing.cast(CodexParser, parser)
        if process is None:
            return parser.finish()
        if process.returncode != 0:
            # key the window by the thread the step row names: the stream's,
            # or the resumed one when the stream died before naming it
            window = _WINDOWS.pop(process, None)
            if window is not None:
                thread = parser.session or window.session
                if thread is not None:
                    self._unsettled[thread] = window
            return parser.finish()
        parser.recover_errors()
        events: list[StreamEvent] = []
        try:
            window = _WINDOWS.pop(process, None)
            if window is None:
                raise ValueError('Process has no rollout window.')
            if parser.errors:
                raise ValueError('stdout reported an error.')
            # stdout must describe one complete turn on a named thread
            parser.turn_usage()
            usage, model, children = window.reconcile(typing.cast(str, parser.session))
            # price at the served model's rates -- a mismatch with the pin is
            # the loop's model-drop check to judge, never a reason to refuse;
            # re-stamp the session so the step row records the served model
            # (record_session is idempotent)
            if model != parser.model:
                parser.model = model
                events.append(
                    StreamEvent(kind='session', session=parser.session, model=model)
                )
            parser.models.append(model)
            cost = _compute_cost(usage, model)
            if cost is None:
                raise ValueError(f'Model {model!r} has no pricing entry.')
            # add each spawned thread's spend at its own served model's rates;
            # the step row's model stays the thread's, so the model-drop check
            # judges the parent's served model alone
            for child, (spent, served) in children.items():
                priced = _compute_cost(spent, served)
                if priced is None:
                    raise ValueError(
                        f'Child rollout {child.name!r}:'
                        f' model {served!r} has no pricing entry.'
                    )
                cost += priced
            parser.cost = cost
        except (OSError, ValueError) as e:
            self.log(f'codex usage unpriced: {e}', logging.WARNING)
        return [*events, *parser.finish()]

    def _settle_cost(
        self: CodexAgent,
        session: str,
        *,
        started: str,
        ended: str,
        after: Optional[str] = None,
        replies: Sequence[dict[str, Any]] = (),
    ) -> Settlement:
        """Price a launch that exited non-zero from its rollout window.

        Its stream closed unpriced (``_finish_stream``), but its rollouts
        keep cumulative counters, so the window kept for it is read
        leniently (``UsageWindow.settle``): the thread's counter growth
        since the spawn, and each sub-agent rollout's, each priced at its
        own served model. The record leaves part of a reply out when a
        rollout ends on a request in flight; a window that then grew no
        counter prices nothing, never $0. A window that cannot be bound or
        priced logs ``codex usage unpriced: <reason>`` and prices nothing.

        With ``after``, or with no window kept (a clean exit keeps none),
        nothing is added: a clean exit is priced by its stream, and codex
        spends nothing past its result.
        """
        window = self._unsettled.pop(session, None)
        if after is not None or window is None:
            return Settlement(cost=None)
        try:
            usage, model, children, cut_off = window.settle(session)
            # price the thread's growth, then each spawned thread's at its own
            # served model's rates; a rollout that grew nothing adds nothing
            cost = 0.0
            grew = False
            if model is not None:
                priced = _compute_cost(usage, model)
                if priced is None:
                    raise ValueError(f'Model {model!r} has no pricing entry.')
                cost += priced
                grew = True
            for child, (spent, served) in children.items():
                if served is None:
                    continue
                priced = _compute_cost(spent, served)
                if priced is None:
                    raise ValueError(
                        f'Child rollout {child.name!r}:'
                        f' model {served!r} has no pricing entry.'
                    )
                cost += priced
                grew = True
        except (OSError, ValueError) as e:
            self.log(f'codex usage unpriced: {e}', logging.WARNING)
            return Settlement(cost=None)
        # a request in flight over counters that never grew spent an unknown
        # amount, which a $0 figure would misstate
        if cut_off and not grew:
            return Settlement(cost=None, cut_off=True)
        return Settlement(cost=cost, cut_off=cut_off)

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

    def _unavailable(self: CodexAgent, detail: str) -> bool:
        """Match codex's provider-outage shapes."""
        return _UNAVAILABLE.search(detail) is not None

    def _preflight(
        self: CodexAgent,
        model: Optional[str],
        *,
        register: Optional[Callable[[subprocess.Popen], None]] = None,
    ) -> Optional[str]:
        """Probe codex's acceptance of an explicit model for this account.

        Some codex accounts reject some explicit models (e.g. a
        ChatGPT-plan account returning a 400 for a model outside its
        entitlement; a cost cap forces an explicit model, but one can
        also be set without a cap); the pricing cache only proves the
        model priceable, not that codex accepts it. One bounded probe,
        built and spawned through the standard triads so a host's
        ``_spawn`` override covers it too; an uncapped codex with no
        model skips the probe and runs fine. The probe leads its own
        process group, handed to ``register`` before the first wait, so
        a timeout reaps codex's whole subtree and the loop can record
        the group for ``kill.sh``.

        Returns:
            A one-line summary of the passed probe: its token usage and
            the figure the model's rates put on it, paid inference that
            lands on no step row and counts toward no cap. ``None`` when
            no model is set and nothing is probed.

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
        # hedged guess, merging stderr in so it rides alongside; the probe
        # leads its own group like a step invocation does
        invocation = self.invocation('reply with: ok', model=model)
        process = self.spawn(
            invocation,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        # hand the live probe to the loop before the first wait, so its group
        # is on record for as long as it runs
        if register is not None:
            register(process)
        try:
            output, _ = process.communicate(timeout=_PREFLIGHT_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            # a hung probe never responded -- distinct from an actual rejection;
            # TERM the probe's whole tree (codex runs tools in groups of their
            # own; other nodes' loops spared) and KILL any survivor after a
            # short grace, since a TERM-trapping grandchild holding the pipe
            # would block the communicate() drain below; the error's first
            # line is the short reason callers persist
            lines = fractal.util.process.reap_or_kill(
                [process.pid],
                grace=_PREFLIGHT_GRACE,
                spare=self.node.loop_records,
            )
            for line in lines:
                print(line, file=sys.stderr)
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
        # the probe passed: name what it spent from the usage codex reports
        # on its stream (a fresh thread's turn total is the probe's own),
        # since the paid turn lands on no step row and counts toward no cap;
        # a stream that does not describe one complete turn, or a model the
        # rates cannot price, says so rather than guessing a figure
        parser = CodexParser(model=model)
        for line in output.splitlines():
            parser.feed(line)
        try:
            usage = _validate_usage(parser.turn_usage())
        except ValueError:
            return f'codex probe passed for model {model!r} (usage unreported)'
        tokens = usage['input_tokens'] + usage['output_tokens']
        cost = _compute_cost(usage, model)
        priced = 'unpriced' if cost is None else f'${cost:.4f}'
        return (
            f'codex probe passed for model {model!r}:'
            f' {tokens} tokens, {priced}, outside the ledger'
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


@dataclasses.dataclass(frozen=True)
class UsageWindow:
    """The session-log state captured before a codex spawn.

    A codex step is priced from the per-response ``token_usage_record`` rows
    codex appends to its session log
    (``CODEX_HOME/sessions/.../rollout-*-<thread>.jsonl``) while the process
    runs, bound to that process by this window: ``capture`` runs before the
    process starts and ``reconcile`` after it exits 0, so the records between
    the two are the invocation's own; an invocation codex ends non-zero is
    read by ``settle`` instead, from the counters alone, its turns complete
    or not. A sub-agent thread the invocation spawns or drives writes its
    own rollout beside the thread's, and the rows it appends within the
    window are the step's spend too, summed at the child's own served
    model. The window reads the dated
    ``sessions/*/*/*/rollout-*.jsonl`` tree alone: a rollout codex archives
    or writes at another depth is invisible to it.
    """

    # the codex home whose sessions/ tree holds the rollout
    home: pathlib.Path
    # the resumed thread; None spawns a fresh one
    session: Optional[str]
    # the rollouts present before the spawn and their byte lengths -- a fresh
    # thread's own file joins them, a sub-agent thread the invocation spawns
    # opens a new one, and one it drives again grows past its captured length,
    # which bounds the child rows the step owns
    existing: dict[pathlib.Path, int]
    # the thread's cumulative counter before the spawn (zeros for a fresh
    # thread) -- the window's summed usage must equal its growth
    baseline: dict[str, int]
    # the resumed rollout, its (st_dev, st_ino) identity and the sha256 of
    # its captured bytes
    path: Optional[pathlib.Path] = None
    identity: Optional[tuple[int, int]] = None
    prefix_sha256: Optional[str] = None
    # why the capture failed -- reconcile and settle re-raise it
    error: Optional[str] = None
    # the resumed rollout's captured byte length
    size: int = 0

    @classmethod
    def capture(cls: type[UsageWindow], invocation: Invocation) -> UsageWindow:
        """Capture the rollout state before the spawn; a failure rides ``error``."""
        codex_home = (invocation.env or {}).get('CODEX_HOME')
        home = pathlib.Path(codex_home or '')
        zeros: dict[str, int] = dict.fromkeys(_BUCKETS, 0)
        try:
            if not home.is_absolute():
                raise ValueError(f'CODEX_HOME is not an absolute path: {codex_home!r}')
            # snapshot the rollouts present and their lengths, so reconcile can
            # tell the files the invocation opens or appends to from the ones
            # that predate it untouched
            existing = {
                path: path.stat().st_size
                for path in (home / 'sessions').glob('*/*/*/rollout-*.jsonl')
            }
            if invocation.session is None:
                return cls(home, None, existing, zeros)
            # bind the resumed rollout by inode, length and content -- stream
            # it and decode only the counter rows, so a long-lived thread costs
            # one pass over its bytes, never its parsed history
            path = _find_rollout(home, invocation.session)
            stat = path.stat()
            # read the thread counter the rollout ends on
            with path.open('rb') as handle:
                baseline, prefix_sha256 = _counter_at(handle, stat.st_size)
            return cls(
                home=home,
                session=invocation.session,
                existing=existing,
                baseline=baseline,
                path=path,
                identity=(stat.st_dev, stat.st_ino),
                prefix_sha256=prefix_sha256,
                size=stat.st_size,
            )
        except (OSError, ValueError) as e:
            return cls(home, invocation.session, {}, zeros, error=f'{e}')

    def reconcile(
        self: UsageWindow,
        session: str,
    ) -> tuple[dict[str, int], str, dict[pathlib.Path, tuple[dict[str, int], str]]]:
        """Return the invocation's usage and served model, and each spawned thread's.

        The invocation's own pair leads; the mapping behind it holds one
        pair per sub-agent rollout the invocation opened or grew, keyed by
        the rollout.

        Raises:
            ValueError: When the rollout for ``session`` cannot be bound to
                the invocation, the records do not describe one counted
                turn, or a rollout new or grown since capture is neither a
                spawned thread's complete counted segment nor a sibling
                root's.

        """
        path, spawned = self._locate(session)
        # stream the rollout once: the bound prefix is hashed as it is read and
        # the window decodes only the lines a counted kind can ride
        with path.open('rb') as handle:
            self._enter(handle, path=path, session=session)
            usage, model, turns = _window(
                _counted(handle),
                session=session,
                baseline=self.baseline,
            )
        # sum each spawned thread's segment past its captured length: the counter
        # there is its baseline (zeros for a rollout the turn opened), its rows
        # must ride this turn, and a fork's copied history is not its segment
        children: dict[pathlib.Path, tuple[dict[str, int], str]] = {}
        for child, (thread, size, start) in spawned.items():
            try:
                with child.open('rb') as handle:
                    baseline, _ = _counter_at(handle, size)
                    # a grown rollout's copied history lies in its captured prefix
                    if size:
                        lines = handle
                    # a rollout the turn opened is read past a fork's copied history
                    else:
                        lines = itertools.islice(handle, start, None)
                    children[child] = _child_window(
                        _counted(lines),
                        thread=thread,
                        session=session,
                        baseline=baseline,
                        turns=turns,
                    )
            except (OSError, ValueError) as e:
                raise ValueError(f'Child rollout {child.name!r}: {e}') from e
        return usage, model, children

    def settle(
        self: UsageWindow,
        session: str,
    ) -> tuple[
        dict[str, int],
        Optional[str],
        dict[pathlib.Path, tuple[dict[str, int], Optional[str]]],
        bool,
    ]:
        """Return the spend of an invocation codex ended non-zero, and any open request.

        The lenient ``reconcile``: the invocation's turns need not complete,
        and an interrupted or refused one is no fault, so each rollout's
        spend is its counter's growth past the captured state, its served
        model ``None`` when it grew none (see ``_spent``). The pairs lead as
        in ``reconcile``; the flag says whether any rollout ends on a
        request in flight.

        Raises:
            ValueError: When a rollout cannot be bound to the invocation as
                ``reconcile`` binds it, a record names another thread or a
                child's row another turn, a counter falls below its captured
                state or behind the token count, or a rollout that grew
                names no one served model.

        """
        path, spawned = self._locate(session)
        with path.open('rb') as handle:
            self._enter(handle, path=path, session=session)
            usage, model, turns, in_flight = _spent(
                handle,
                owners=(session, session),
                baseline=self.baseline,
            )
        # read each spawned thread's segment past its captured length, as
        # reconcile does, its rows riding this invocation's turns
        children: dict[pathlib.Path, tuple[dict[str, int], Optional[str]]] = {}
        for child, (thread, size, start) in spawned.items():
            try:
                with child.open('rb') as handle:
                    baseline, _ = _counter_at(handle, size)
                    # a grown rollout's copied history lies in its captured prefix
                    if size:
                        lines = handle
                    # a rollout the turn opened is read past a fork's copied history
                    else:
                        lines = itertools.islice(handle, start, None)
                    spent, served, _, open_request = _spent(
                        lines,
                        owners=(thread, session),
                        baseline=baseline,
                        rides=turns,
                    )
            except (OSError, ValueError) as e:
                raise ValueError(f'Child rollout {child.name!r}: {e}') from e
            children[child] = (spent, served)
            in_flight = in_flight or open_request
        return usage, model, children, in_flight

    def _locate(
        self: UsageWindow,
        session: str,
    ) -> tuple[pathlib.Path, dict[pathlib.Path, tuple[str, int, int]]]:
        """Return the rollout for ``session`` and the rollouts it spawned."""
        if self.error is not None:
            raise ValueError(self.error)
        if self.session not in (None, session):
            raise ValueError(
                f'stdout names thread {session!r}, not the resumed {self.session!r}.'
            )
        path = _find_rollout(self.home, session)
        # a rollout new or grown since capture that opens as a thread this one
        # spawned holds sub-agent spend the step owns; one the window cannot
        # place refuses the step
        spawned = _spawned_rollouts(self.home, session, known=self.existing, own=path)
        return path, spawned

    def _enter(
        self: UsageWindow,
        handle: BinaryIO,
        *,
        path: pathlib.Path,
        session: str,
    ) -> None:
        """Bind the open rollout to the capture, reading ``handle`` to the window."""
        if self.path is None:
            # a fresh thread's rollout is new and opens with its session metadata
            if path in self.existing:
                raise ValueError('Rollout predates the spawn.')
            first = handle.readline()
            meta = json.loads(first) if first else None
            if not isinstance(meta, dict) or (meta.get('type') != 'session_meta'):
                raise ValueError('Rollout does not open with session metadata.')
            if _read_payload(meta).get('id') != session:
                raise ValueError('Rollout metadata names another thread.')
        else:
            # the resumed rollout is the captured inode, grown, its prefix intact
            stat = os.fstat(handle.fileno())
            identity = (stat.st_dev, stat.st_ino)
            if (path != self.path) or (identity != self.identity):
                raise ValueError('Resumed rollout was replaced.')
            digest = hashlib.sha256()
            remaining = self.size
            while remaining:
                chunk = handle.read(min(remaining, _CHUNK))
                if not chunk:
                    raise ValueError('Resumed rollout was truncated.')
                digest.update(chunk)
                remaining -= len(chunk)
            if digest.hexdigest() != self.prefix_sha256:
                raise ValueError('Resumed rollout prefix changed.')


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


def _find_rollout(home: pathlib.Path, session: str) -> pathlib.Path:
    """Return the one dated rollout for ``session`` under ``home``."""
    found = list((home / 'sessions').glob(f'*/*/*/rollout-*-{session}.jsonl'))
    if not found:
        raise ValueError('No rollout names the thread.')
    if len(found) > 1:
        raise ValueError('Several rollouts name the thread.')
    (path,) = found
    return path


def _spawned_rollouts(
    home: pathlib.Path,
    session: str,
    *,
    known: dict[pathlib.Path, int],
    own: pathlib.Path,
) -> dict[pathlib.Path, tuple[str, int, int]]:
    """Map each rollout ``session`` spawned to its id, captured size and history start.

    A rollout new or grown since capture is one of three: a thread
    ``session`` spawned, which the mapping holds; a sibling root (``exec``,
    ``cli``, a chat with the node) or any sub-agent of another root in the
    home, whose rows never name ``session``, which is skipped; or one the
    window cannot explain, which refuses the step.
    """
    spawned: dict[pathlib.Path, tuple[str, int, int]] = {}
    threads: set[str] = set()
    for path in (home / 'sessions').glob('*/*/*/rollout-*.jsonl'):
        if path == own:
            continue
        # a rollout the invocation neither opened nor appended to holds none
        # of its spend; one that grew was driven again -- codex reloads a
        # resumed thread's spawn descendants and appends to the child's file
        size = known.get(path)
        if size is not None and path.stat().st_size == size:
            continue
        # the spawn rides the opening session metadata alone -- a new file
        # codex has opened but not yet written is no record at all, while a
        # captured file found empty is not the file captured
        with path.open('rb') as handle:
            first = handle.readline()
        if not first and (size is None):
            continue
        unexplained = _UNEXPLAINED.format(name=path.name)
        try:
            payload = _read_meta(first)
        except ValueError as e:
            raise ValueError(unexplained) from e
        source = payload.get('source')
        subagent = source.get('subagent') if isinstance(source, dict) else None
        spawn = subagent.get('thread_spawn') if isinstance(subagent, dict) else None
        thread = payload.get('id')
        root = payload.get('session_id')
        # a root's rows and its descendants' name the root, never this thread:
        # another root's rollout, or its sub-agent's, spawned or otherwise, is
        # that root's spend
        if root != session:
            if not _is_root(home, root):
                raise ValueError(unexplained)
            continue
        # a root naming this thread as its session, or a sub-agent of another
        # kind (review, compact, memory consolidation) on this thread, is spend
        # the window cannot place
        if not isinstance(spawn, dict) or not isinstance(thread, str):
            raise ValueError(unexplained)
        # a copy of this thread's rollout behind a spawn first line is no spawned thread
        if thread == session:
            raise ValueError(unexplained)
        # one thread writes one rollout: a twin is not the captured file
        if thread in threads:
            raise ValueError(
                f'Several rollouts name spawned thread {thread!r}: {path.name!r}'
            )
        threads.add(thread)
        # a forked thread copies its source's history ahead of its own
        # records, from the ordinal codex stamps on the metadata
        start = payload.get('subagent_history_start_ordinal')
        # bool is an int subclass, and no ordinal is a bool or negative
        if (type(start) is not int) or (start < 0):
            start = 0
        spawned[path] = (thread, known.get(path, 0), start)
    return spawned


def _is_root(home: pathlib.Path, session: Any, /) -> bool:
    """Return whether ``session`` names a root thread's rollout under ``home``.

    A root's session metadata names ``session`` as its own id and opens with
    a plain string ``source`` (``exec``, ``cli``); a spawned thread's carries
    the spawn object. A rollout that cannot be found or read is no root.
    """
    if not isinstance(session, str):
        return False
    try:
        path = _find_rollout(home, session)
        with path.open('rb') as handle:
            payload = _read_meta(handle.readline())
    except (OSError, ValueError):
        return False
    # the rollout is found by its name: its own metadata must name the session
    if payload.get('id') != session:
        return False
    return isinstance(payload.get('source'), str)


def _counter_at(handle: BinaryIO, size: int) -> tuple[dict[str, int], str]:
    """Return the thread counter a rollout's first ``size`` bytes end on and its sha256.

    The prefix must end on a line: a rollout shorter than ``size``, or one
    whose line crosses that byte, is not the captured file.
    """
    digest = hashlib.sha256()
    counter: dict[str, int] = dict.fromkeys(_BUCKETS, 0)
    while handle.tell() < size:
        line = handle.readline()
        if not line:
            raise ValueError('Rollout is shorter than its captured length.')
        digest.update(line)
        if b'"token_usage_record"' not in line:
            continue
        record = _read_record(line)
        if record.get('type') == 'token_usage_record':
            counter = _validate_usage(_read_payload(record).get('thread_token_usage'))
    if handle.tell() != size:
        raise ValueError('Rollout prefix does not end on a line.')
    return counter, digest.hexdigest()


def _counted(lines: Iterable[bytes], /) -> Iterator[dict[str, Any]]:
    """Decode the rollout lines that can carry a record kind the window reads."""
    for line in lines:
        if not any(marker in line for marker in _MARKERS):
            continue
        yield _read_record(line)


def _read_record(line: bytes, /) -> dict[str, Any]:
    """Return a rollout line's record object."""
    record = json.loads(line)
    if not isinstance(record, dict):
        raise ValueError('Rollout line is not an object.')
    return record


def _read_payload(record: dict[str, Any], /) -> dict[str, Any]:
    """Return a rollout record's payload object."""
    payload = record.get('payload')
    if not isinstance(payload, dict):
        raise ValueError('Rollout payload is not an object.')
    return payload


def _read_meta(line: bytes, /) -> dict[str, Any]:
    """Return the payload of a rollout's opening ``session_meta`` line."""
    record = _read_record(line)
    if record.get('type') != 'session_meta':
        raise ValueError('Rollout does not open with session metadata.')
    return _read_payload(record)


def _validate_usage(value: Any, /) -> dict[str, int]:
    """Read a rollout usage counter into a dict keyed by bucket."""
    if not isinstance(value, dict):
        raise ValueError('Usage counter is not an object.')
    result: dict[str, int] = {}
    for key in _BUCKETS:
        count = value.get(key, 0)
        # bool is an int subclass, and a counter is never a flag
        if (type(count) is not int) or (count < 0):
            raise ValueError(
                f'Usage counter {key!r} is not a non-negative integer: {count!r}'
            )
        result[key] = count
    return result


def _read_response(
    payload: dict[str, Any],
    /,
    *,
    owners: tuple[str, str],
) -> tuple[str, dict[str, int], dict[str, int]]:
    """Read a usage record's response id, usage and thread counter.

    ``owners`` is the ``(thread_id, session_id)`` pair the record must name:
    a thread's own rows name it twice, a spawned thread's name the child and
    the root.
    """
    named = (payload.get('thread_id'), payload.get('session_id'))
    if named != owners:
        raise ValueError(f'Usage record belongs to another thread: {named!r}')
    response = payload.get('response_id')
    if not isinstance(response, str) or not response:
        raise ValueError('Usage record names no response.')
    usage = _validate_usage(payload.get('usage'))
    counter = _validate_usage(payload.get('thread_token_usage'))
    return response, usage, counter


def _window(
    records: Iterable[dict[str, Any]],
    *,
    session: str,
    baseline: dict[str, int],
) -> tuple[dict[str, int], str, set[str]]:
    """Sum the window's per-response usage and bind it to one complete turn.

    Returns the sum, the served model and the turn ids the window's
    ``turn_context`` records name, which a spawned thread's rows ride.
    """
    started = 0
    completed = 0
    turns: set[str] = set()
    models: set[str] = set()
    responses: dict[str, dict[str, int]] = {}
    thread = baseline
    for record in records:
        kind = record.get('type')
        if kind not in _KINDS:
            continue
        payload = _read_payload(record)
        # count the turn's start and completion, refusing an interrupted one
        if kind == 'event_msg':
            event = payload.get('type')
            if event == 'task_started':
                started += 1
            elif event == 'task_complete':
                completed += 1
            elif event in ('turn_aborted', 'error'):
                raise ValueError('Rollout records an interrupted turn.')
        # note the served model and the turn
        elif kind == 'turn_context':
            model = payload.get('model')
            if not isinstance(model, str) or not model:
                raise ValueError('Turn context names no served model.')
            models.add(model)
            turn = payload.get('turn_id')
            if isinstance(turn, str):
                turns.add(turn)
        # add each response's usage once, refusing another thread's record
        else:
            response, usage, thread = _read_response(payload, owners=(session, session))
            responses[response] = usage
    # bind the records to one completed turn on one served model
    if (started, completed) != (1, 1):
        raise ValueError(
            'Rollout does not record exactly one completed turn'
            f' ({started} started, {completed} completed).'
        )
    # a rollout without usage records predates the codex release that writes them
    if not responses:
        raise ValueError(
            'Rollout carries no per-response usage records'
            ' (codex 0.153 or newer required).'
        )
    total, model = _bind(responses, models, counter=thread, baseline=baseline)
    return total, model, turns


def _child_window(
    records: Iterable[dict[str, Any]],
    *,
    thread: str,
    session: str,
    baseline: dict[str, int],
    turns: set[str],
) -> tuple[dict[str, int], str]:
    """Sum a spawned thread's appended usage and bind it to the parent's turns.

    Codex pre-empts a child's turn normally, so the segment need not balance
    starts against completions: it must end on a completed turn, record no
    interrupted one, and its rows must ride one of the parent window's
    ``turns``.
    """
    last: Optional[str] = None
    models: set[str] = set()
    responses: dict[str, dict[str, int]] = {}
    counter = baseline
    for record in records:
        kind = record.get('type')
        if kind not in _KINDS:
            continue
        payload = _read_payload(record)
        # note the last turn event, refusing an interrupted turn
        if kind == 'event_msg':
            event = payload.get('type')
            if event in ('task_started', 'task_complete'):
                last = event
            elif event in ('turn_aborted', 'error'):
                raise ValueError('Rollout records an interrupted turn.')
        # note the served model
        elif kind == 'turn_context':
            model = payload.get('model')
            if not isinstance(model, str) or not model:
                raise ValueError('Turn context names no served model.')
            models.add(model)
        # add each response's usage once, refusing another thread's or turn's
        else:
            response, usage, counter = _read_response(payload, owners=(thread, session))
            root = payload.get('root_turn_id')
            if not isinstance(root, str) or (root not in turns):
                raise ValueError(f'Usage record rides another turn: {root!r}')
            responses[response] = usage
    # bind the segment to a completed turn with counted responses
    if last != 'task_complete':
        raise ValueError('Rollout segment does not end on a completed turn.')
    if not responses:
        raise ValueError('Rollout segment carries no per-response usage records.')
    return _bind(responses, models, counter=counter, baseline=baseline)


def _bind(
    responses: dict[str, dict[str, int]],
    models: set[str],
    *,
    counter: dict[str, int],
    baseline: dict[str, int],
) -> tuple[dict[str, int], str]:
    """Bind the summed responses to one served model and the counter's growth."""
    if len(models) != 1:
        raise ValueError(
            f'Rollout does not name exactly one served model: {sorted(models)!r}'
        )
    (model,) = models
    # prove the sum complete: it must equal the thread counter's growth
    total = {key: sum(usage[key] for usage in responses.values()) for key in _BUCKETS}
    growth = {key: counter[key] - baseline[key] for key in _BUCKETS}
    if total != growth:
        raise ValueError(
            f'Summed usage {total!r} disagrees with the thread counter growth'
            f' {growth!r}.'
        )
    return total, model


def _spent(
    lines: Iterable[bytes],
    *,
    owners: tuple[str, str],
    baseline: dict[str, int],
    rides: Optional[set[str]] = None,
) -> tuple[dict[str, int], Optional[str], set[str], bool]:
    """Read one rollout's segment of a failed invocation, its turns complete or not.

    Returns the counter's growth past ``baseline``, the served model the
    segment's ``turn_context`` records name (``None`` when it grew none),
    the turn ids they name, and whether the segment ends on a request in
    flight. Its usage rows must name ``owners`` (see ``_read_response``)
    and, with ``rides`` (the parent window's turns), ride one of them.

    A request is in flight from a turn's start, or from a response item
    past the last counter -- a tool's result codex sends next, or a reply
    it was streaming -- until a counter closes it with its usage, the turn
    completes, or an ``error`` record refuses it: a refused request is not
    billed. A response item is told by its kind's quoted name, never
    decoded.
    """
    in_flight = False
    turns: set[str] = set()
    models: set[str] = set()
    counter = baseline
    reported = baseline
    for line in lines:
        # a line of no counted kind is skipped undecoded; a response item
        # among them is a request under way
        if not any(marker in line for marker in _MARKERS):
            if b'"response_item"' in line:
                in_flight = True
            continue
        record = _read_record(line)
        kind = record.get('type')
        if kind not in _KINDS:
            if kind == 'response_item':
                in_flight = True
            continue
        payload = _read_payload(record)
        # open a request at the turn's start, close it at the turn's end or
        # its refusal, and note the token count the thread reports
        if kind == 'event_msg':
            event = payload.get('type')
            if event == 'task_started':
                in_flight = True
            elif event in ('task_complete', 'error'):
                in_flight = False
            elif event == 'token_count':
                info = payload.get('info')
                if isinstance(info, dict):
                    reported = _validate_usage(info.get('total_token_usage'))
        # note the served model and the turn
        elif kind == 'turn_context':
            model = payload.get('model')
            if not isinstance(model, str) or not model:
                raise ValueError('Turn context names no served model.')
            models.add(model)
            turn = payload.get('turn_id')
            if isinstance(turn, str):
                turns.add(turn)
        # close the request on its counter, refusing another thread's or turn's
        else:
            _, _, counter = _read_response(payload, owners=owners)
            if rides is not None:
                root = payload.get('root_turn_id')
                if not isinstance(root, str) or (root not in rides):
                    raise ValueError(f'Usage record rides another turn: {root!r}')
            in_flight = False
    # a token count past the counter is a response with no usage record (codex
    # older than 0.153 writes none), whose spend the growth would leave out
    if any(reported[key] > counter[key] for key in _BUCKETS):
        raise ValueError(
            f'Token count {reported!r} runs past the usage records {counter!r}.'
        )
    growth = {key: counter[key] - baseline[key] for key in _BUCKETS}
    if any(count < 0 for count in growth.values()):
        raise ValueError(
            f'Thread counter {counter!r} fell below its baseline {baseline!r}.'
        )
    # a segment that grew is priced at its one served model
    if not any(growth.values()):
        return growth, None, turns, in_flight
    if len(models) != 1:
        raise ValueError(
            f'Rollout does not name exactly one served model: {sorted(models)!r}'
        )
    (model,) = models
    return growth, model, turns, in_flight


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
