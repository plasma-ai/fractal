"""Codex CLI agent implementation."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import pathlib
import shutil
import signal
import subprocess
import time
import tomllib
import typing
import weakref
from collections.abc import Callable, Iterable, Iterator
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
# TERM-to-KILL grace for the probe's own process group -- mirrors the loop's
# _KILL_GRACE_SECONDS for a step group
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
        """Close the drained stream on its settled cost and wall time."""
        wall = time.monotonic() - self._started
        self.final = True
        return [StreamEvent(kind='result', cost=self.cost, final=True, duration=wall)]


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

    def _spawn(
        self: CodexAgent,
        invocation: Invocation,
        **kwargs: Any,
    ) -> subprocess.Popen:
        """Capture the rollout window, then spawn codex.

        A host override of ``_spawn`` must delegate here, or the step
        records no cost.
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
        unpriced and silent; a clean exit first clears the error frames the
        completed turn recovered from (``recover_errors``), then prices; a
        clean exit whose usage cannot be bound to the process logs the
        reason at WARNING and leaves the cost ``None``.
        """
        parser = typing.cast(CodexParser, parser)
        if process is None or process.returncode != 0:
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
            usage, model = window.reconcile(typing.cast(str, parser.session))
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
            parser.cost = cost
        except (OSError, ValueError) as e:
            self.log(f'codex usage unpriced: {e}', logging.WARNING)
        return [*events, *parser.finish()]

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

    def _preflight(
        self: CodexAgent,
        model: Optional[str],
        *,
        register: Optional[Callable[[subprocess.Popen], None]] = None,
    ) -> None:
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
            # TERM the whole group and KILL any survivor after a short grace,
            # since a TERM-trapping grandchild holding the pipe would block the
            # reap below; the first line is the short reason callers persist
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            deadline = time.monotonic() + _PREFLIGHT_GRACE
            gone = False
            while time.monotonic() < deadline:
                # reap the leader (clears its zombie so the group probe below
                # reads true), then check the whole group: a survivor that
                # traps TERM must still draw the KILL
                process.poll()
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    gone = True
                    break
                except PermissionError:
                    pass
                time.sleep(0.1)
            # a group the grace proved gone is never signaled again -- its id
            # may already lead an unrelated process
            if not gone:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
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


@dataclasses.dataclass(frozen=True)
class UsageWindow:
    """The session-log state captured before a codex spawn.

    A codex step is priced from the per-response ``token_usage_record`` rows
    codex appends to its session log
    (``CODEX_HOME/sessions/.../rollout-*-<thread>.jsonl``) while the process
    runs, bound to that process by this window: ``capture`` runs before the
    process starts and ``reconcile`` after it exits, so the records between
    the two are the invocation's own.
    """

    # the codex home whose sessions/ tree holds the rollout
    home: pathlib.Path
    # the resumed thread; None spawns a fresh one
    session: Optional[str]
    # the rollouts present before the spawn and their byte lengths -- a fresh
    # thread's own file joins them, a sub-agent thread the invocation spawns
    # opens a new one, and one it drives again grows past its captured length
    existing: dict[pathlib.Path, int]
    # the thread's cumulative counter before the spawn (zeros for a fresh
    # thread) -- the window's summed usage must equal its growth
    baseline: dict[str, int]
    # the resumed rollout, its (st_dev, st_ino) identity and the sha256 of
    # its captured bytes
    path: Optional[pathlib.Path] = None
    identity: Optional[tuple[int, int]] = None
    prefix_sha256: Optional[str] = None
    # why the capture failed -- reconcile re-raises it
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
            digest = hashlib.sha256()
            size = 0
            # read the thread counter the rollout ends on
            baseline = zeros
            with path.open('rb') as handle:
                for line in handle:
                    digest.update(line)
                    size += len(line)
                    if b'"token_usage_record"' not in line:
                        continue
                    record = _read_record(line)
                    if record.get('type') == 'token_usage_record':
                        baseline = _validate_usage(
                            _read_payload(record).get('thread_token_usage')
                        )
            return cls(
                home=home,
                session=invocation.session,
                existing=existing,
                baseline=baseline,
                path=path,
                identity=(stat.st_dev, stat.st_ino),
                prefix_sha256=digest.hexdigest(),
                size=size,
            )
        except (OSError, ValueError) as e:
            return cls(home, invocation.session, {}, zeros, error=f'{e}')

    def reconcile(self: UsageWindow, session: str) -> tuple[dict[str, int], str]:
        """Return the usage and served model of the records the invocation appended.

        Raises:
            ValueError: When the rollout for ``session`` cannot be bound to
                the invocation, the invocation spawned sub-agent threads, or
                the records do not describe one counted turn.

        """
        if self.error is not None:
            raise ValueError(self.error)
        if self.session not in (None, session):
            raise ValueError(
                f'stdout names thread {session!r}, not the resumed {self.session!r}.'
            )
        path = _find_rollout(self.home, session)
        # a rollout new or grown since capture that opens as a thread this one
        # spawned holds sub-agent spend the window never sums -- refuse the
        # partial sum
        if _has_spawned_thread(self.home, session, known=self.existing, own=path):
            raise ValueError(
                'Rollout spawned sub-agent threads (their usage is unpriced).'
            )
        # stream the rollout once: the bound prefix is hashed as it is read and
        # the window decodes only the lines a counted kind can ride
        with path.open('rb') as handle:
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
            return _window(_counted(handle), session=session, baseline=self.baseline)


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


def _has_spawned_thread(
    home: pathlib.Path,
    session: str,
    *,
    known: dict[pathlib.Path, int],
    own: pathlib.Path,
) -> bool:
    """Return whether a new or grown rollout opens as a thread ``session`` spawned."""
    for path in (home / 'sessions').glob('*/*/*/rollout-*.jsonl'):
        if path == own:
            continue
        # a rollout the invocation neither opened nor appended to holds none
        # of its spend; one that grew was driven again -- codex reloads a
        # resumed thread's spawn descendants and appends to the child's file
        size = known.get(path)
        if size is not None and path.stat().st_size == size:
            continue
        # the spawn rides the opening session metadata alone -- a file codex
        # has opened but not yet written is no record at all
        with path.open('rb') as handle:
            first = handle.readline()
        if not first.strip():
            continue
        record = _read_record(first)
        if record.get('type') != 'session_meta':
            continue
        source = _read_payload(record).get('source')
        subagent = source.get('subagent') if isinstance(source, dict) else None
        spawn = subagent.get('thread_spawn') if isinstance(subagent, dict) else None
        if isinstance(spawn, dict) and (spawn.get('parent_thread_id') == session):
            return True
    return False


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


def _window(
    records: Iterable[dict[str, Any]],
    *,
    session: str,
    baseline: dict[str, int],
) -> tuple[dict[str, int], str]:
    """Sum the window's per-response usage and bind it to one complete turn."""
    started = 0
    completed = 0
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
        # note the served model
        elif kind == 'turn_context':
            model = payload.get('model')
            if not isinstance(model, str) or not model:
                raise ValueError('Turn context names no served model.')
            models.add(model)
        # add each response's usage once, refusing another thread's record
        else:
            owners = (payload.get('thread_id'), payload.get('session_id'))
            if owners != (session, session):
                raise ValueError(f'Usage record belongs to another thread: {owners!r}')
            response = payload.get('response_id')
            if not isinstance(response, str) or not response:
                raise ValueError('Usage record names no response.')
            responses[response] = _validate_usage(payload.get('usage'))
            thread = _validate_usage(payload.get('thread_token_usage'))
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
    if len(models) != 1:
        raise ValueError(
            f'Rollout does not name exactly one served model: {sorted(models)!r}'
        )
    (model,) = models
    # prove the sum complete: it must equal the thread counter's growth
    total = {key: sum(usage[key] for usage in responses.values()) for key in _BUCKETS}
    growth = {key: thread[key] - baseline[key] for key in _BUCKETS}
    if total != growth:
        raise ValueError(
            f'Summed usage {total!r} disagrees with the thread counter growth'
            f' {growth!r}.'
        )
    return total, model


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
