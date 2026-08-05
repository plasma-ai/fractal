"""Base classes for agents."""

from __future__ import annotations

import dataclasses
import functools
import importlib
import importlib.util
import logging
import os
import pathlib
import re
import shutil
import subprocess
import typing
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Optional, Union

from fractal.exceptions import AbstractMethodError, AgentStreamError

from . import pricing
from .event import Event, FailureEvent, InitialEvent, TerminalEvent

if typing.TYPE_CHECKING:
    from .node import Node

__all__ = [
    'resolve',
    'register',
    'supported',
    'Invocation',
    'StreamEvent',
    'StreamResult',
    'StreamParser',
    'Agent',
    'seed_agents',
]

# registry of provider backends: base command -> 'module:Class' or class;
# extended by register() (in-process) or the deployment hook file
_AGENTS: dict[str, Union[str, type[Agent]]] = {
    'claude': 'fractal.impl.claude:ClaudeAgent',
    'codex': 'fractal.impl.codex:CodexAgent',
    'grok': 'fractal.impl.grok:GrokAgent',
    'opencode': 'fractal.impl.opencode:OpencodeAgent',
    'omp': 'fractal.impl.omp:OmpAgent',
}

# names claimed by register() -- explicit registrations win over the hook file
_EXPLICIT: set[str] = set()

# tree data dirs whose deployment hook file has been consulted this process,
# mapped to the load failure to re-raise (None = consulted cleanly)
_LOADED: dict[pathlib.Path, Optional[Exception]] = {}

# session ids land in file paths and globs -- validated at the transcript
# boundary (underscore cannot escape a path, so opencode's ses_ ids pass)
_SESSION_CHARS = re.compile(r'[A-Za-z0-9_-]+')

# ambient effort vars the invocation verb unsets on compose: claude reads
# CLAUDE_CODE_EFFORT_LEVEL over its own effort flag (an operator shell
# carrying it would silently override every step's pinned effort), and
# CLAUDE_EFFORT is claude's own export to hook/Bash subprocesses (a stale
# ancestor session's value would masquerade as the child's); effort reaches
# a session only through the step pin's flag (flag-only by design), never env
_EFFORT_KEYS = (
    'CLAUDE_EFFORT',
    'CLAUDE_CODE_EFFORT_LEVEL',
)

# ambient model-forcing vars, unset the same way: CLAUDE_CODE_SUBAGENT_MODEL
# forces every fan-out sub-agent onto one model, explicit per-agent pins
# included -- inherited through a tmux tree it silently rerouted a whole
# fleet's pinned fan-outs once; a model reaches a session only through the
# step pin's flag or the session's own settings, never ambient env
_MODEL_KEYS = ('CLAUDE_CODE_SUBAGENT_MODEL',)


def command_base(command: str) -> str:
    """Return an agent command's base word, refusing shell quoting.

    Commands split on whitespace into argv words (:attr:`Agent.parts`)
    with no shell interpretation, so a quoted argument would silently
    mangle into garbage words (``--flag "two words"`` becomes
    ``['--flag', '"two', 'words"']``) deep inside the loop's tmux pane,
    after start already printed its success -- refuse quoting at the
    accept-time gates instead, where the error still reaches the
    caller's terminal.

    Args:
        command: Full agent command (e.g. ``'claude --some-flag'``).

    Returns:
        The command's first whitespace-separated word.

    Raises:
        ValueError: If the command carries a quote or backslash.

    """
    if any(char in command for char in '\'"\\'):
        raise ValueError(
            f'Unsupported agent command: {command!r} (commands split on'
            ' whitespace only — quotes and backslashes are not supported).'
        )
    base_word, *_ = command.split()
    return base_word


def resolve(name: str, *, root: Optional[pathlib.Path] = None) -> type[Agent]:
    """Return the ``Agent`` class registered for a base command.

    Consults the deployment hook file under ``root`` once per process
    (its subclasses register by ``name``), then the registry, importing
    backend modules at call time (the sanctioned lazy upward handle).

    Args:
        name: Agent base command (e.g. ``claude``).
        root: Tree data dir whose deployment hook file to consult.

    Returns:
        The registered ``Agent`` subclass.

    Raises:
        ValueError: If no backend is registered under ``name``, or the
            registered backend module cannot be imported.
        RuntimeError: If the tree's deployment hook file fails to load
            (sticky -- re-raised on every later resolve for that tree).

    """
    # consult the deployment hook file first, so injected subclasses resolve
    # across every process boundary (tmux loop relaunches, CLI, TUI)
    if root is not None:
        _load_deployment_agents(root)
    # look up the registered backend
    target = _AGENTS.get(name)
    if target is None:
        supported_names = ', '.join(supported())
        raise ValueError(f'Unsupported agent: {name!r} (supported: {supported_names})')
    # import string targets at call time -- core reaches impl only through the
    # registry, so the import stays off every path that never spawns an agent;
    # the registry also owns the missing-module guard for its lazy imports
    if isinstance(target, str):
        module_name, _, class_name = target.partition(':')
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as e:
            raise ValueError(
                f'Cannot import agent backend {name!r}'
                f' (module {module_name!r} not found).'
            ) from e
        target = getattr(module, class_name)
    return target


def register(name: str, target: Union[str, type[Agent]]) -> None:
    """Register (or override) an agent backend under a base command.

    The in-process injection point: an embedding application registers a
    custom subclass at import time. Explicit registrations win over the
    deployment hook file.

    Args:
        name: Agent base command to register under.
        target: ``Agent`` subclass, or a ``'module:Class'`` string
            imported at resolve time.

    """
    # record the backend and claim the name against hook-file registrations
    _AGENTS[name] = target
    _EXPLICIT.add(name)


def supported() -> tuple[str, ...]:
    """Return the registered base commands, registration order."""
    return tuple(_AGENTS)


@dataclasses.dataclass(frozen=True)
class Invocation:
    """A spawnable agent invocation.

    An ``_invocation`` hook sets ``env`` to only its reserved provider
    keys (or ``None``); the public ``invocation`` verb composes the full
    Popen environment (``os.environ``, then the caller overlay, then
    those reserved keys), so a composed invocation's env passes straight
    to ``Popen`` and ``None`` inherits the parent's. A reserved key
    valued ``None`` means unset: the verb pops it from the merged
    environment, so a hook can scrub keys the ambient environment
    inherited from an ancestor's route. ``session`` carries
    the caller-minted id for providers that do not mint their own; the
    launcher must pre-stamp it on the step row before spawning, so a
    boot-window abort still leaves a resumable session. Pure serializable
    data: a host can ship it to any executor.
    """

    # base command name (DB/session-map key)
    agent: str
    argv: tuple[str, ...]
    # the worktree (codex resume relies on it)
    cwd: pathlib.Path
    # values of None mean 'pop this key from the composed spawn env'
    env: Optional[Mapping[str, Optional[str]]] = None
    session: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class StreamEvent:
    """One semantic frame parsed from an agent's output stream.

    ``kind`` is one of: ``'session' | 'text' | 'tool' | 'tool_result' |
    'cost' | 'result' | 'error'``. Fields are populated per kind; absent
    facts stay ``None`` (a codex result carries no cost). Consumers
    (renderer, TUI bubbles, record verbs) branch on ``kind`` only --
    never on the provider.
    """

    kind: str
    text: Optional[str] = None
    # tool name (kind='tool')
    tool: Optional[str] = None
    session: Optional[str] = None
    model: Optional[str] = None
    cost: Optional[float] = None
    # cost: authoritative vs estimate
    final: bool = False
    # seconds; claude duration_ms coalesced, codex wall-time close
    duration: Optional[float] = None
    turns: Optional[int] = None
    # claude error_max_budget_usd subtype
    budget_stopped: bool = False
    # result: is_error / non-success subtype
    failed: bool = False
    # error detail / tool-result body
    message: Optional[str] = None


def _sanitize(value: Optional[str]) -> Optional[str]:
    r"""Neutralize lone surrogates so a utf-8 sink never raises on the string.

    ``json.loads`` accepts lone-surrogate escapes (``\udc80``) inside valid
    JSON, and every downstream sink (the renderer's stdout, a SQLite text
    bind, a trace append) assumes clean utf-8 and crashes on one; round-
    tripping through utf-8 with replacement drops them, leaving all real
    unicode untouched.
    """
    if value is None:
        return None
    return value.encode('utf-8', 'replace').decode('utf-8')


def _sanitize_event(event: StreamEvent) -> StreamEvent:
    """Return the event with lone surrogates neutralized in its string fields.

    The single point every parsed event crosses before its hooks, the render
    callback, and the record verbs -- so no sink downstream can meet an
    unencodable surrogate (see :func:`_sanitize`).
    """
    return dataclasses.replace(
        event,
        text=_sanitize(event.text),
        tool=_sanitize(event.tool),
        session=_sanitize(event.session),
        model=_sanitize(event.model),
        message=_sanitize(event.message),
    )


@dataclasses.dataclass(frozen=True)
class StreamResult:
    """Outcome of one streamed invocation."""

    session: Optional[str]
    model: Optional[str]
    cost: Optional[float]
    budget_stopped: bool = False
    # NOTE: every distinct model the stream's own rows named, in order -- the
    #   step row keeps only the last, so a mid-stream substitution that
    #   recovers before the stream ends is visible here alone
    models: tuple[str, ...] = ()


class StreamParser:
    """Incremental parser for one agent output stream.

    Pure protocol: subclasses own the full wire format (JSON decode
    included) -- deliberate sibling duplication over premature hoisting.
    Parsers never touch the ledger; recording is the driver's
    (``Agent.stream``). State accumulates across ``feed`` calls so
    callers can attribute a step after draining.
    """

    def __init__(self: StreamParser, *, model: Optional[str] = None) -> None:
        """Initialize ``StreamParser``."""
        self.session: Optional[str] = None
        self.model = model  # stream-reported model overwrites
        self.models: list[str] = []  # every distinct stream-named served model
        self.cost: Optional[float] = None  # running figure (None = no fact yet)
        self.final: bool = False  # a result frame settled the cost
        self.budget_stopped: bool = False
        self.errors: list[str] = []  # stream-borne failures (codex)

    def feed(self: StreamParser, line: str) -> list[StreamEvent]:
        """Parse one stdout line into normalized events. Hook for backends."""
        raise AbstractMethodError(self)


class Agent:
    """Base class for agent CLI backends.

    An agent owns its own conversation loop inside a provider subprocess:
    the contract is capabilities -- invocation building, spawning, stream
    parsing, session policy, cost mode -- never a message list. Sync-only
    by design: agents are subprocesses driven line-by-line (the TUI
    streams on a worker thread); an async variant arrives with its first
    consumer, never speculatively.

    Backends override only ``_``-hooks and capability attributes; public
    verbs own validation, events, and the settle invariants. Override the
    ``on_verb`` hooks (calling super and returning the event) to ship
    observability to a host application -- the package emits to named
    loggers and never configures handlers, so the host owns logging
    config.
    """

    # base command word: executable, DB agent column, .session map key,
    # config-dir stem (.claude)
    name: str = ''
    # the single config file in the node's agent dir
    config_file: str = ''
    # session forking supported
    can_fork: bool = False
    # the agent mints its own session id (codex); False = the caller mints
    # and pre-stamps (claude)
    mints_session: bool = False
    # token-priced: cost only via the LiteLLM cache; pricing fetch is fatal
    # at run start
    needs_pricing: bool = False
    # 'call' (per-invocation figures) or 'thread' (cumulative totals settled
    # against prior sibling steps)
    cost_scope: str = 'call'
    # accepts a hard per-step budget flag
    enforces_budget: bool = False
    # emits a result frame per step (opencode's step_finish), not one per turn
    # -- so a non-final result is mid-turn and only a final one closes a chat
    # turn; every other backend emits one terminal result
    results_per_step: bool = False
    # empty/None means the vendor-native endpoint; a listed route is
    # validated at the spawn verbs
    providers: tuple[str, ...] = ()

    __parser__: type[StreamParser] = StreamParser

    def __init__(
        self: Agent,
        node: Node,
        command: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> None:
        """Initialize ``Agent``.

        Args:
            node: The node this agent runs for (config dirs, worktree,
                ledger).
            command: Full agent command (e.g. ``'claude --some-flag'``);
                defaults to the node's configured agent. Extra words are
                spliced into every invocation.
            provider: Provider-route override; ``None`` means native
                (``Node.agent`` threads the node's effective configured
                route).

        """
        # bind the node handle
        self.node = node
        # bind the command string, defaulting to the node's configured agent
        self.command = command or node.config.get('agent') or self.name
        # bind the route override; the provider property resolves the rest
        self._provider = provider

    @property
    def parts(self: Agent) -> list[str]:
        """Return the configured command split into argv words."""
        return self.command.split()

    @property
    def config_dir(self: Agent) -> pathlib.Path:
        """Return the node's per-agent config dir (``<node_dir>/.<name>``)."""
        return self.node.node_dir / f'.{self.name}'

    @property
    def err_path(self: Agent) -> pathlib.Path:
        """Return the stderr log path (``<node_dir>/<name>.err``)."""
        return self.node.node_dir / f'{self.name}.err'

    @functools.cached_property
    def provider(self: Agent) -> Optional[str]:
        """Return the bound provider route the caller threaded, or ``None``.

        ``None`` means the vendor's own endpoint. ``Node.agent`` threads
        the node's effective configured route (per-step frontmatter rides
        the same channel), so accessor-built backends carry it here; a
        directly-constructed backend is native unless its caller passes
        one -- the binder never reads node state, so a throwaway node
        (chat doubles, hosts) constructs and parses freely. A backend
        with no routes (``providers == ()``) has no route axis, so an
        openrouter-defaulting ancestor never pins a route-less
        descendant. The spawn verbs validate membership.
        """
        # route-less backends have no route to take -- the key never applies
        if not self.providers:
            return None
        return self._provider

    @functools.cached_property
    def logger(self: Agent) -> logging.Logger:
        """Return the stdlib logger named for the concrete class."""
        name = f'{type(self).__module__}.{type(self).__name__}'
        return logging.getLogger(name)

    def log(self: Agent, message: str, level: int = logging.INFO) -> None:
        """Log a message to this agent's logger."""
        self.logger.log(level, message)

    def _not_implemented(self: Agent, method: str) -> NotImplementedError:
        """Return a ``NotImplementedError`` for an absent capability.

        Args:
            method: Unprotected method name (e.g. ``'transcript'``, not
                ``'_transcript'``).

        Returns:
            A ``NotImplementedError`` with a descriptive message.

        """
        return NotImplementedError(
            f'{method!r} is not implemented in {self.__class__.__name__}.'
        )

    def invocation(
        self: Agent,
        prompt: str,
        *,
        session: Optional[str] = None,
        fork: bool = False,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        budget: Optional[float] = None,
        env: Optional[dict[str, str]] = None,
    ) -> Invocation:
        """Build one spawnable invocation. Delegates to ``_invocation``.

        A pure builder pair -- the observable moment is ``spawn``.
        ``session=None`` starts fresh; a session resumes in place, or
        forks with ``fork=True`` (the backend's ``NotImplementedError``
        surfaces for non-forking agents -- one raise site with its
        citations). Mints the fresh session id centrally when the caller
        owns minting (uuid4; already lowercase).

        Args:
            prompt: The prompt text (the subprocess's only input).
            session: Session to resume or fork; ``None`` starts fresh.
            fork: Fork ``session`` instead of resuming it in place.
            model: Model override for this invocation.
            effort: Reasoning-effort override, passed through to the
                provider's effort flag unvalidated (the agent's own
                error surfaces for an unknown level).
            budget: Hard budget in USD (enforcing agents only).
            env: Caller env overlay, merged here in the public verb --
                base ``os.environ``, then the overlay, then
                ``_invocation``'s provider-specific keys, with any key
                whose merged value is ``None`` popped and the ambient
                effort vars (``_EFFORT_KEYS``) always unset; ``spawn``
                kwargs stay supervision-only.

        Returns:
            The spawnable invocation.

        Raises:
            ValueError: For an unsupported provider route, a budget on a
                non-enforcing agent, or fork without a session.

        """
        # validate capability asks before building
        if self.provider is not None and self.provider not in self.providers:
            supported_providers = ', '.join(self.providers) or 'none'
            raise ValueError(
                f'Unsupported provider: {self.provider!r}'
                f' (supported: {supported_providers})'
            )
        if budget is not None and not self.enforces_budget:
            raise ValueError(f'Agent {self.name!r} does not enforce a budget.')
        if fork and session is None:
            raise ValueError('Cannot fork without a session.')
        # resolve the session mode
        if fork:
            mode = 'fork'
        elif session is not None:
            mode = 'resume'
        else:
            mode = 'fresh'
        # mint the fresh session id centrally when the caller owns minting
        # (uuid4 is already lowercase); the launcher pre-stamps it on the step
        # row before spawning, so a boot-window abort leaves a resumable
        # session -- detached steps mint too, one code path
        if mode == 'fresh' and not self.mints_session:
            session = str(uuid.uuid4())
            self.on_session(session=session, model=model)
        # build the provider invocation
        result = self._invocation(
            prompt,
            mode=mode,
            session=session,
            model=model,
            effort=effort,
            budget=budget,
        )
        # stamp the id on the returned invocation centrally -- the launcher
        # pre-stamps invocation.session on the step row, so a backend that
        # dropped it would silently break boot-window resumability
        if session is not None:
            result = dataclasses.replace(result, session=session)
        # compose the spawn environment: base os.environ, then the caller
        # overlay, then _invocation's provider-specific keys -- provider keys
        # win on collision (codex's CODEX_HOME must survive). _invocation
        # returns only its reserved keys, so the ambient snapshot never
        # re-clobbers the overlay; the composed dict is the full Popen env
        # an ambient effort or model-forcing var forces composition, so the
        # scrub lands even for a backend that reserves no env keys of its own
        scrub_keys = (*_EFFORT_KEYS, *_MODEL_KEYS)
        ambient_effort = any(key in os.environ for key in scrub_keys)
        if env is not None or result.env is not None or ambient_effort:
            # pin the ambient effort and model-forcing vars to None over
            # every layer -- the pop below unsets them, so the only effort
            # or model signal reaching the session is the step pin's own
            # flag (see _EFFORT_KEYS / _MODEL_KEYS)
            merged = {
                **os.environ,
                **(env or {}),
                **(result.env or {}),
                **dict.fromkeys(scrub_keys),
            }
            # a merged value of None means unset: pop the key, so a backend
            # can scrub routing keys the ambient environment inherited from
            # an ancestor's provider route
            composed = {
                key: value for key, value in merged.items() if value is not None
            }
            result = dataclasses.replace(result, env=composed)
        return result

    def _invocation(
        self: Agent,
        prompt: str,
        *,
        mode: str,
        session: Optional[str],
        model: Optional[str],
        effort: Optional[str],
        budget: Optional[float],
    ) -> Invocation:
        """Hook for building the provider argv/cwd/env.

        ``mode`` is ``'fresh' | 'resume' | 'fork'``, resolved by the
        public verb; ``session`` is the minted or supplied id (``None``
        only when the agent mints its own).

        Override in a backend to build the provider command::

            def _invocation(self, prompt, *, mode, session, model, effort, budget):
                argv = (*self.parts, '-p', prompt, '--resume', session)
                return Invocation(agent=self.name, argv=argv, cwd=self.node.worktree)
        """
        raise AbstractMethodError(self)

    def spawn(
        self: Agent,
        invocation: Invocation,
        **kwargs: Any,
    ) -> subprocess.Popen:
        """Spawn an invocation: fires ``on_spawn``, delegates to ``_spawn``.

        Callers own supervision policy via ``**kwargs`` (the loop passes
        ``start_new_session=True`` and a stderr file; chat inherits
        stderr; the TUI pipes it). All launch surfaces route ``Popen``
        through here, so overriding ``_spawn`` is the single sanctioned
        way for a host to redirect execution (a wrapper binary, a
        container, a remote executor).
        """
        # fire the spawn event, then delegate process creation to the hook
        self.on_spawn(invocation=invocation)
        return self._spawn(invocation, **kwargs)

    def _spawn(
        self: Agent,
        invocation: Invocation,
        **kwargs: Any,
    ) -> subprocess.Popen:
        """Hook for process creation.

        Default: ``subprocess.Popen`` with the invocation's
        argv/cwd/env, ``stdin=DEVNULL`` (the prompt is the only input
        channel), and ``stdout=PIPE`` in text mode for ``stream``.

        Override in a host to redirect execution::

            def _spawn(self, invocation, **kwargs):
                argv = ('sandbox-exec', *invocation.argv)
                return subprocess.Popen(argv, cwd=invocation.cwd, **kwargs)
        """
        return subprocess.Popen(
            invocation.argv,
            cwd=invocation.cwd,
            # a spawned invocation is composed, so its env holds no None
            # values (the pre-compose Mapping type is wider than Popen's)
            env=invocation.env,  # type: ignore[arg-type]
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            text=True,
            # agent output is parsed/logged, not trusted: a non-UTF-8 byte must
            # decode leniently, not crash the stream reader (the parser already
            # tolerates wire noise)
            errors='replace',
            **kwargs,
        )

    def parser(self: Agent, *, model: Optional[str] = None) -> StreamParser:
        """Return a fresh stream parser (constructed from ``__parser__``)."""
        return self.__parser__(model=model)

    def stream(
        self: Agent,
        lines: Iterable[str],
        *,
        step_id: Optional[int] = None,
        model: Optional[str] = None,
        detached: bool = False,
        render: Optional[Callable[[StreamEvent], None]] = None,
    ) -> StreamResult:
        """Drive a parser over the agent's stdout lines.

        Fires ``on_call`` at entry and ``on_call_success``/
        ``on_call_failure`` at exit; dispatches each parsed event to its
        hook (``on_session`` once, ``on_action`` per tool, ``on_error``
        per stream-borne failure, ``on_budget`` on a budget stop), the
        render callback, and -- when ``step_id`` is given -- the record
        verbs (session stamped as the stream opens; each cost figure
        flushed immediately, so a signal-killed reader still recorded
        the last one). Text deltas fire no hook (render only).

        Args:
            lines: The agent's stdout, line by line.
            step_id: Step row to record against; ``None`` records
                nothing (a live chat writes nothing).
            model: Configured-model fallback for the parser (the
                stream-reported model overwrites it).
            detached: Suppress the ``.session`` map persistence (the
                step-row stamp always lands).
            render: Presentation callback receiving every parsed event
                (CLI ANSI, TUI bubbles, loop pane).

        Returns:
            The stream outcome, read off the drained parser state.

        Raises:
            AgentStreamError: When the stream carried error frames (codex
                errors ride the JSON stream and must fail the step even
                after a fully drained stdout).

        """
        # fresh parser per call; state accumulates for attribution after draining
        parser = self.parser(model=model)
        # open the call pairing
        call_event = self.on_call(session=None, model=model)
        try:
            # drive the parser line by line, dispatching each semantic frame
            for line in lines:
                events = parser.feed(line)
                # neutralize lone surrogates at the parse boundary -- in each
                # event AND the parser's own state (session/model are read
                # directly below and for the drained result) -- so no utf-8
                # sink (renderer, SQLite, traces) meets an unencodable byte
                parser.session = _sanitize(parser.session)
                parser.model = _sanitize(parser.model)
                for event in events:
                    event = _sanitize_event(event)
                    # per-kind hooks (text deltas fire no hook -- render only)
                    if event.kind == 'session':
                        self.on_session(session=event.session, model=parser.model)
                        # stamp the step row as the stream opens
                        if step_id is not None:
                            self.record_session(
                                step_id=step_id,
                                session=event.session,
                                model=parser.model,
                                detached=detached,
                            )
                    elif event.kind == 'tool':
                        self.on_action(tool=event.tool)
                    elif event.kind == 'error':
                        self.on_error(error=event.message)
                    # a budget stop is a clean stop, never a failure
                    if event.budget_stopped:
                        self.on_budget()
                    # flush each cost figure immediately -- the reader can die
                    # by signal with the last figure already on the ledger
                    if event.cost is not None and step_id is not None:
                        self.record_cost(step_id, event.cost, final=event.final)
                    # presentation callback
                    if render is not None:
                        render(event)
            # stream-borne errors fail the turn even after a fully drained
            # stdout (else the step records completed/exit 0); the parser
            # collects error detail raw, so sanitize the joined message --
            # the loop folds it into the step-row failure detail (SQLite)
            if parser.errors:
                detail = _sanitize('; '.join(parser.errors))
                raise AgentStreamError(f'{self.name} reported an error: {detail}')
        except Exception as error:
            self.on_call_failure(initial_event=call_event, error=error)
            raise
        self.on_call_success(initial_event=call_event)
        return StreamResult(
            session=parser.session,
            model=parser.model,
            cost=parser.cost,
            budget_stopped=parser.budget_stopped,
            # parser state is raw wire text -- sanitize here like session
            # and model above, or a lone-surrogate model name reaches SQLite
            models=tuple(_sanitize(model) for model in parser.models),
        )

    def record_session(
        self: Agent,
        step_id: int,
        session: str,
        *,
        model: Optional[str] = None,
        detached: bool = False,
    ) -> None:
        """Record a captured session.

        Fires ``on_record_session`` with exactly the persisted values,
        then delegates persistence to ``_record_session``.

        Args:
            step_id: Step to stamp.
            session: Real, agent-specific session.
            model: The model that ran the step -- stream-reported where
                the agent names it, else the configured one.
            detached: Suppress the ``.session`` map persistence.

        """
        # fire the record event, then delegate persistence to the hook
        self.on_record_session(session=session, model=model)
        return self._record_session(step_id, session, model=model, detached=detached)

    def _record_session(
        self: Agent,
        step_id: int,
        session: str,
        *,
        model: Optional[str],
        detached: bool,
    ) -> None:
        """Hook for persisting a session.

        The default stamps the step row always -- so the session is
        resumable later (chat mode) and groups an agent's cost-delta --
        and, unless ``detached``, persists it to the node's ``.session``
        map so the next step in the same continuous session can resume
        it. Override in a host app to mirror sessions elsewhere (call
        super to keep the ledger)::

            def _record_session(self, step_id, session, *, model, detached):
                host.sessions.mirror(session)
                super()._record_session(
                    step_id=step_id,
                    session=session,
                    model=model,
                    detached=detached,
                )
        """
        # stamp the step row (always -- detached suppresses only the map write)
        self.node.record.step_session(
            self.name,
            step_id=step_id,
            model=model,
            session=session,
        )
        # persist for the next continuous step
        if not detached:
            self.node.sessions.set(self.name, session)

    def record_cost(
        self: Agent,
        step_id: int,
        cost: float,
        *,
        final: bool = False,
    ) -> None:
        """Record a cost figure.

        Applies the scope settle, fires ``on_record_cost`` with the
        settled amount, and delegates persistence to ``_record_cost``.
        ``'call'`` scope writes figures as-is; ``'thread'`` scope settles
        the cumulative total against prior sibling steps sharing the
        session. The settle lives here, in the public verb, so a billing
        override cannot fork provider math; the event and the hook both
        see exactly what lands on the ledger. ``None`` is never
        recorded -- a NULL cost means unknowable, never $0.

        Args:
            step_id: Step to record against.
            cost: Cost in USD -- per-invocation for ``'call'`` scope,
                the cumulative thread total for ``'thread'`` scope.
            final: Whether the figure is authoritative (a result frame)
                rather than a running estimate.

        """
        # settle 'thread' figures: the provider reports cumulative thread
        # totals and continuous steps resume one thread, so subtract prior
        # steps sharing this session (a detached step has its own thread, so
        # the delta is the full cost)
        if self.cost_scope == 'thread':
            rows = self.node.db.read('steps', where={'step_id': step_id}, limit=1)
            session = rows[0].get('session') if rows else None
            if session is not None:
                siblings = self.node.db.read(
                    'steps',
                    where={'session': session, 'node': self.node.branch},
                )
                prior = sum(
                    row['cost']
                    for row in siblings
                    if row['step_id'] != step_id and row['cost'] is not None
                )
                cost = cost - prior
        # a step can't have negative spend: a negative thread delta (a mid-run
        # price change, an un-recorded prior step) or a call-scope provider
        # accounting bug (a negative total_cost_usd, a credit adjustment) would
        # otherwise be stored and poison the next prior-sibling subtraction and
        # every downstream budget derivation
        cost = max(0.0, cost)
        # fire the record event with the settled figure, then persist via the hook
        self.on_record_cost(cost=cost, final=final)
        return self._record_cost(step_id, cost, final=final)

    def _record_cost(
        self: Agent,
        step_id: int,
        cost: float,
        *,
        final: bool,
    ) -> None:
        """Hook for persisting a settled cost figure.

        The default writes the step row (which strips a stale
        ``unpriced`` marker). Override in a host app to debit a billing
        service (call super to keep the ledger)::

            def _record_cost(self, step_id, cost, *, final):
                if final:
                    billing.debit(self.node.branch, cost)
                super()._record_cost(step_id, cost, final=final)
        """
        return self.node.record.step_cost(step_id=step_id, cost=cost)

    def tracks_cost(self: Agent, model: Optional[str] = None) -> bool:
        """Return whether spend will be recorded.

        Never conflates zero with unknowable: cost-reporting agents
        always track; token-priced agents track only a model present in
        the pricing cache.
        """
        # cost-reporting agents carry their own figures on the stream
        if not self.needs_pricing:
            return True
        # token-priced agents record only what the pricing lookup can price
        return model is not None and self._rates(model) is not None

    def config_model(self: Agent) -> Optional[str]:
        """Return the model this agent's own per-node config defaults to."""
        return self._config_model()

    def _config_model(self: Agent) -> Optional[str]:
        """Hook for parsing the provider config file. Default returns ``None``.

        Override in a backend to read the configured default::

            def _config_model(self):
                path = self.config_dir / self.config_file
                return json.loads(path.read_text(encoding='utf-8')).get('model')
        """
        return None

    def _rates(self: Agent, model: str) -> Optional[dict[str, Any]]:
        """Hook for resolving a model to its pricing entry. Default: exact lookup.

        Override in a backend whose model ids miss the pricing table's
        keying (a gateway slug, a vendor prefix)::

            def _rates(self, model):
                return pricing.rates(model) or pricing.rates(f'xai/{model}')
        """
        return pricing.rates(model)

    def transcript(self: Agent, session: str) -> dict[str, Any]:
        """Resolve a session's transcript for this node.

        Providers persist a session as a JSONL file that grows while the
        session runs, so polling this returns the live transcript.
        ``path`` rides along so a caller can tail or range-read the file
        as it grows -- it may be the expected location even while
        absent, so a poller can wait for it to appear.

        Args:
            session: The agent session id (a ``session`` value off a
                step or iteration row).

        Returns:
            ``{agent, session, path, exists, content}`` -- ``content``
            is the raw JSONL text (empty when absent).

        Raises:
            ValueError: If ``session`` is not a bare session id
                (anything but ``[A-Za-z0-9_-]`` could escape the
                transcript directory).

        """
        # validate the id at the boundary: it lands in file paths and globs
        if not _SESSION_CHARS.fullmatch(session):
            raise ValueError(f'Invalid session id: {session!r}')
        # resolve the provider's expected layout first
        path = self._transcript(session)
        if path is None or not path.is_file():
            # fallback discovery is security-gated on the session being
            # recorded for THIS node in the DB -- an ungated glob would serve
            # any session of the OS user, from any project; the query is
            # lazy, paid only on an expected-path miss
            if self._session_owned(session):
                path = self._transcript_fallback(session) or path
        # assemble the shape off the resolved path
        if path is None:
            return {
                'agent': self.name,
                'session': session,
                'path': None,
                'exists': False,
                'content': '',
            }
        if not path.is_file():
            return {
                'agent': self.name,
                'session': session,
                'path': str(path),
                'exists': False,
                'content': '',
            }
        return {
            'agent': self.name,
            'session': session,
            'path': str(path),
            'exists': True,
            # the file grows while the session runs, so a poll racing a write
            # can catch a torn multi-byte tail -- decode leniently rather than
            # fail the fetch on a transient, self-healing condition
            'content': path.read_text(encoding='utf-8', errors='replace'),
        }

    def _transcript(self: Agent, session: str) -> Optional[pathlib.Path]:
        """Hook for the provider's expected transcript layout.

        May return a not-yet-existing expected path (pollers wait).

        Override in a backend to name the provider's layout::

            def _transcript(self, session):
                sessions = pathlib.Path.home() / '.provider' / 'sessions'
                return sessions / f'{session}.jsonl'
        """
        raise self._not_implemented('transcript')

    def _transcript_fallback(self: Agent, session: str) -> Optional[pathlib.Path]:
        """Hook for last-resort transcript discovery.

        Default returns ``None``; only consulted behind the base
        ownership gate.

        Override in a backend to glob for a relocated transcript::

            def _transcript_fallback(self, session):
                sessions = pathlib.Path.home() / '.provider' / 'sessions'
                return next(sessions.rglob(f'*{session}.jsonl'), None)
        """
        return None

    def _session_owned(self: Agent, session: str) -> bool:
        """Return whether the session is recorded for this node's rows."""
        query = (
            'SELECT 1 FROM steps WHERE node = ? AND session = ?'
            ' UNION SELECT 1 FROM iters WHERE node = ? AND session = ?'
            ' LIMIT 1'
        )
        params = (self.node.branch, session, self.node.branch, session)
        rows = self.node.db.read(query=query, params=params)
        return bool(rows)

    def preflight(self: Agent, model: Optional[str] = None) -> None:
        """Fail fast before a run.

        Checks the agent binary is on ``PATH``, fires ``on_preflight``,
        and delegates provider probes to ``_preflight``. Abort semantics
        stay with the caller.

        Args:
            model: The model the run will use, for provider probes.

        Raises:
            RuntimeError: For an unsupported provider route, a missing
                binary, or a provider probe relaying the provider's own
                diagnostics.

        """
        # the bound route must be one the backend supports (None = native)
        if self.provider is not None and self.provider not in self.providers:
            supported_providers = ', '.join(self.providers) or 'none'
            raise RuntimeError(
                f'Unsupported provider: {self.provider!r}'
                f' (supported: {supported_providers})'
            )
        # the binary must be on PATH before anything spawns
        if shutil.which(self.parts[0]) is None:
            raise RuntimeError(f'{self.parts[0]} is not installed.')
        # fire the preflight event, then delegate provider probes to the hook
        self.on_preflight()
        return self._preflight(model)

    def _preflight(self: Agent, model: Optional[str]) -> None:
        """Hook for provider-specific probes. Default is a no-op.

        Override in a backend to relay provider diagnostics::

            def _preflight(self, model):
                probe = subprocess.run([*self.parts, 'auth'], check=False)
                if probe.returncode != 0:
                    raise RuntimeError('Provider is not authenticated.')
        """

    @classmethod
    def seed(
        cls: type[Agent],
        node_dir: pathlib.Path,
        *,
        parent_dir: Optional[pathlib.Path] = None,
        reset: bool = False,
    ) -> None:
        """Seed the per-node agent config dir.

        Creates the dir (wiping first on ``reset``), copies the config
        file -- the parent node's live copy wins over the package seed,
        and an existing file is never overwritten -- symlinks skills,
        then delegates provider extras to ``_seed``. A backend without a
        package seed still gets its dir and skills link, just no config
        file.

        Args:
            node_dir: The node data directory to seed under.
            parent_dir: The parent node's data directory, when one
                exists.
            reset: Wipe the agent dir before seeding.

        """
        # recreate the agent dir (wiped first on reset)
        config_dir = node_dir / f'.{cls.name}'
        if reset and config_dir.exists():
            shutil.rmtree(config_dir)
        config_dir.mkdir(parents=True, exist_ok=True)
        # prefer the parent node's config so children inherit its settings; fall
        # back to the package seed for a top-level node (parent has no agent config)
        source = None
        if cls.config_file:
            if parent_dir is not None:
                parent_config = parent_dir / f'.{cls.name}' / cls.config_file
                if parent_config.is_file():
                    source = parent_config
            if source is None:
                package_dir = pathlib.Path(__file__).parent.parent
                package_seed = package_dir / '_node' / 'config' / cls.name
                if (package_seed / cls.config_file).is_file():
                    source = package_seed / cls.config_file
        # copy the config file (an existing one is never overwritten)
        if source is not None and not (config_dir / cls.config_file).exists():
            shutil.copy(source, config_dir / cls.config_file)
        # symlink skills so the agent can find them
        skills = config_dir / 'skills'
        if not skills.exists():
            skills.symlink_to('../skills')
        # delegate provider extras (auth links, inherited file copies)
        return cls._seed(node_dir, parent_dir=parent_dir)

    @classmethod
    def _seed(
        cls: type[Agent],
        node_dir: pathlib.Path,
        *,
        parent_dir: Optional[pathlib.Path] = None,
    ) -> None:
        """Hook for provider-specific seeding (auth links). Default no-op.

        ``parent_dir`` is the parent node's data directory, so a backend
        can carry files the inherited config references.

        Override in a backend to link shared credentials::

            @classmethod
            def _seed(cls, node_dir, *, parent_dir=None):
                link = node_dir / f'.{cls.name}' / 'auth.json'
                if not link.is_symlink():
                    link.symlink_to(pathlib.Path.home() / '.provider' / 'auth.json')
        """

    def on_call(
        self: Agent,
        message: Optional[str | list[str]] = None,
        *,
        logging_level: int = logging.INFO,
        **kwargs: Any,
    ) -> AgentCallEvent:
        """Handle agent call event.

        Fired when consumption of an invocation's stream begins.
        """
        event = AgentCallEvent(
            source=self,
            message=message,
            **kwargs,
        )
        self.log(event.description, logging_level)
        return event

    def on_call_success(
        self: Agent,
        message: Optional[str | list[str]] = None,
        *,
        logging_level: int = logging.DEBUG,
        **kwargs: Any,
    ) -> AgentCallSuccessEvent:
        """Handle agent call success event.

        Fired after an agent stream drains cleanly.
        """
        event = AgentCallSuccessEvent(
            source=self,
            message=message,
            **kwargs,
        )
        self.log(event.description, logging_level)
        return event

    def on_call_failure(
        self: Agent,
        message: Optional[str | list[str]] = None,
        *,
        logging_level: int = logging.WARNING,
        **kwargs: Any,
    ) -> AgentCallFailureEvent:
        """Handle agent call failure event.

        Fired when an agent stream fails.
        """
        event = AgentCallFailureEvent(
            source=self,
            message=message,
            **kwargs,
        )
        self.log(event.description, logging_level)
        return event

    def on_spawn(
        self: Agent,
        message: Optional[str | list[str]] = None,
        *,
        logging_level: int = logging.INFO,
        **kwargs: Any,
    ) -> AgentSpawnEvent:
        """Handle agent spawn event.

        Fired when an invocation is spawned.
        """
        event = AgentSpawnEvent(
            source=self,
            message=message,
            **kwargs,
        )
        self.log(event.description, logging_level)
        return event

    def on_action(
        self: Agent,
        message: Optional[str | list[str]] = None,
        *,
        logging_level: int = logging.INFO,
        **kwargs: Any,
    ) -> AgentActionEvent:
        """Handle agent action event.

        Fired when the agent invokes a tool.
        """
        event = AgentActionEvent(
            source=self,
            message=message,
            **kwargs,
        )
        self.log(event.description, logging_level)
        return event

    def on_session(
        self: Agent,
        message: Optional[str | list[str]] = None,
        *,
        logging_level: int = logging.INFO,
        **kwargs: Any,
    ) -> AgentSessionEvent:
        """Handle agent session event.

        Fired when a session id is captured or minted.
        """
        event = AgentSessionEvent(
            source=self,
            message=message,
            **kwargs,
        )
        self.log(event.description, logging_level)
        return event

    def on_record_session(
        self: Agent,
        message: Optional[str | list[str]] = None,
        *,
        logging_level: int = logging.INFO,
        **kwargs: Any,
    ) -> AgentSessionEvent:
        """Handle session record event.

        Fired by ``record_session`` with exactly the persisted values.
        """
        event = AgentSessionEvent(
            source=self,
            message=message,
            **kwargs,
        )
        self.log(event.description, logging_level)
        return event

    def on_record_cost(
        self: Agent,
        message: Optional[str | list[str]] = None,
        *,
        logging_level: int = logging.DEBUG,
        **kwargs: Any,
    ) -> AgentCostEvent:
        """Handle cost record event.

        Fired by ``record_cost`` with the settled figure that lands on
        the ledger.
        """
        event = AgentCostEvent(
            source=self,
            message=message,
            **kwargs,
        )
        self.log(event.description, logging_level)
        return event

    def on_budget(
        self: Agent,
        message: Optional[str | list[str]] = None,
        *,
        logging_level: int = logging.WARNING,
        **kwargs: Any,
    ) -> AgentBudgetEvent:
        """Handle agent budget event.

        Fired when the agent stops on its hard budget.
        """
        event = AgentBudgetEvent(
            source=self,
            message=message,
            **kwargs,
        )
        self.log(event.description, logging_level)
        return event

    def on_error(
        self: Agent,
        message: Optional[str | list[str]] = None,
        *,
        logging_level: int = logging.WARNING,
        **kwargs: Any,
    ) -> AgentErrorEvent:
        """Handle agent error event.

        Fired for each stream-borne error frame.
        """
        event = AgentErrorEvent(
            source=self,
            message=message,
            **kwargs,
        )
        self.log(event.description, logging_level)
        return event

    def on_preflight(
        self: Agent,
        message: Optional[str | list[str]] = None,
        *,
        logging_level: int = logging.INFO,
        **kwargs: Any,
    ) -> AgentPreflightEvent:
        """Handle agent preflight event.

        Fired before a preflight probe runs.
        """
        event = AgentPreflightEvent(
            source=self,
            message=message,
            **kwargs,
        )
        self.log(event.description, logging_level)
        return event


def seed_agents(
    node_dir: pathlib.Path,
    *,
    parent_dir: Optional[pathlib.Path] = None,
    reset: bool = False,
) -> None:
    """Seed every registered agent's config dir plus the neutral one.

    Args:
        node_dir: The node data directory to seed under.
        parent_dir: The parent node's data directory, when one exists.
        reset: Wipe each agent dir before seeding.

    """
    # set up each agent dir -- seed its config and symlink in skills, recreated
    # each init and gitignored; a node's base agent may be overridden per step
    # (agent: frontmatter), so any node may run either agent
    for name in supported():
        resolve(name).seed(node_dir, parent_dir=parent_dir, reset=reset)
    # the neutral agents dir is not a provider: no config file, just the
    # skills mount (recreated with the same reset semantics)
    agents_dir = node_dir / '.agents'
    if reset and agents_dir.exists():
        shutil.rmtree(agents_dir)
    agents_dir.mkdir(parents=True, exist_ok=True)
    skills = agents_dir / 'skills'
    if not skills.exists():
        skills.symlink_to('../skills')


# ------ events


class AgentCallEvent(InitialEvent):
    """Emitted when consumption of an agent invocation's stream begins."""

    session: Optional[str]
    model: Optional[str]


class AgentCallSuccessEvent(TerminalEvent):
    """Emitted after an agent stream drains cleanly."""


class AgentCallFailureEvent(FailureEvent):
    """Emitted when an agent stream fails."""


class AgentSpawnEvent(Event):
    """Emitted when an invocation is spawned."""

    invocation: Invocation


class AgentActionEvent(Event):
    """Emitted when the agent invokes a tool.

    Observation only -- the subprocess executes its own tools, so there
    is no success/failure pairing.
    """

    tool: str


class AgentSessionEvent(Event):
    """Emitted when a session id is captured, minted, or recorded."""

    session: str
    model: Optional[str]


class AgentCostEvent(Event):
    """Emitted when a settled cost figure is flushed to the ledger."""

    cost: float
    final: bool


class AgentBudgetEvent(Event):
    """Emitted when the agent stops on its hard budget."""


class AgentErrorEvent(Event):
    """Emitted when a stream-borne error frame arrives."""

    error: str


class AgentPreflightEvent(Event):
    """Emitted before a preflight probe runs."""


# ------ helper functions


def _load_deployment_agents(root: pathlib.Path) -> None:
    """Load a tree's deployment hook file and register its agents.

    ``agents.py`` in the tree's user-node data dir is the cross-process
    injection channel: ``Agent`` subclasses exported via ``__all__``
    register by their ``name``, overriding the default backends but
    never an explicit ``register`` claim. Each tree is consulted once
    per process.
    """
    # consult each tree once per process; a recorded failure re-raises on
    # every later resolve, so a broken hook file never degrades silently to
    # the default backends after its first error
    if root in _LOADED:
        cached = _LOADED[root]
        if cached is not None:
            raise cached
        return
    _LOADED[root] = None
    hook_path = root / 'agents.py'
    if not hook_path.exists():
        return
    # NOTE: this executes arbitrary code from the tree's agents.py, so it is
    #   safe only for first-party trees -- node dirs are already trusted
    #   executable surfaces (setup.sh, skills)
    spec = importlib.util.spec_from_file_location('_agents', hook_path)
    module = importlib.util.module_from_spec(spec)
    # a tree that declares a subclass this environment cannot load must fail
    # naming the hook file, not with a bare import error
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        error = RuntimeError(f'{hook_path}: {e}')
        _LOADED[root] = error
        raise error from e
    # register the exported Agent subclasses by their base command
    for export in getattr(module, '__all__', ()):
        target = getattr(module, export, None)
        if isinstance(target, type) and issubclass(target, Agent):
            if target.name not in _EXPLICIT:
                _AGENTS[target.name] = target
