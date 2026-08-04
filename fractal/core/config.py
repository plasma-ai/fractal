"""Implements ``Config`` class."""

from __future__ import annotations

import fcntl
import json
import math
import pathlib
import typing
from typing import Any, Optional

import fractal.util
from fractal.constants import CONFIG_FILE, LOCK_FILE

if typing.TYPE_CHECKING:
    from .node import Node

__all__ = []

# configuration schema (single source): every key a node config may hold
KEYS = (
    'title',
    'user',
    'project',
    'root',
    'scope',
    'clone_dirs',
    'base',
    'meta',
    'agent',
    'provider',
    'model',
    'effort',
    'max_iters',
    'max_depth',
    'max_children',
    'max_descendants',
    'timeout',
    'iter_timeout',
    'step_timeout',
    'step_retries',
    'step_retry_backoff',
    'interval',
    'sleep',
    'wait',
    'max_cost',
    'max_iter_cost',
    'max_step_cost',
    'reserve_budget',
    'sync',
    'detached',
    'local',
    'blind',
    'sealed',
)

# keys whose value is a JSON list of repo-relative subdirectories (the CLI
# accepts a comma- or space-separated string and stores each entry in
# canonical path form, and the read path splits a space-joined one, so both
# forms round-trip)
LIST_KEYS = (
    'scope',
    'clone_dirs',
)

# keys whose value must be a plain bool (true/false/null)
BOOL_KEYS = (
    'user',
    'sync',
    'detached',
    'local',
    'blind',
    'sealed',
)

# keys whose value must be a non-negative integer cap
INT_KEYS = (
    'max_iters',
    'max_depth',
    'max_children',
    'max_descendants',
    'step_retries',
)

# keys whose value must be a non-negative USD amount
COST_KEYS = (
    'max_cost',
    'max_iter_cost',
    'max_step_cost',
    'reserve_budget',
)

# keys whose value must be a duration with a unit suffix
DURATION_KEYS = (
    'timeout',
    'iter_timeout',
    'step_timeout',
    'step_retry_backoff',
    'interval',
    'sleep',
    'wait',
)

# keys fixed at init: 'root' anchors the central database for the whole
# tree, 'user' marks node identity (flipping it would let a root branch be
# started as a loop), 'project' fixes the on-disk layout the immutable
# .worktrees/.project cache mirrors (track/untrack, lint.sh, and merge.sh
# all read it -- a post-init change desyncs them from the real paths)
IMMUTABLE_KEYS = ('root', 'user', 'project')

# default cleanup reserve as a fraction of max_cost
DEFAULT_RESERVE_FRACTION = 0.10

# decimal places a reserve amount materializes to -- a percent product
# rounds here so no binary float noise persists into config.json, and
# child_retune's default-detection equality matches what parse_reserve_budget
# stored; the two sites MUST round identically, so both quote this constant
RESERVE_PRECISION = 4


class Config:
    """Read/write surface for a node's ``config.json``.

    Reads from disk on every access -- nothing is cached -- so writes
    from other processes (CLI commands, lifecycle scripts) are
    immediately visible.
    """

    def __init__(self: Config, node: Node) -> None:
        """Initialize ``Config``.

        Args:
            node: The owning ``Node`` instance.

        """
        self._node = node

    @property
    def node(self: Config) -> Node:
        """Return the owning node."""
        return self._node

    @property
    def path(self: Config) -> pathlib.Path:
        """Return the path to the node's ``config.json``."""
        return self._node.node_dir / CONFIG_FILE

    def load(self: Config) -> dict[str, Any]:
        """Read and parse the node's ``config.json``.

        A hand-corrupted config otherwise yields a context-free
        ``Expecting value: line 1 column 1`` from every command that touches it;
        this re-raises with the file path so the error points at what to fix.

        Returns:
            The parsed config mapping.

        Raises:
            ValueError: When ``config.json`` does not hold valid JSON.

        """
        path = self.path
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except json.JSONDecodeError as e:
            raise ValueError(f'{path} is not valid JSON: {e}') from e

    def get(self: Config, key: str, default: Any = None) -> Any:
        """Read a config value.

        Args:
            key: Config key.
            default: Value to return when the key or config file
                is missing.

        Returns:
            Config value, or ``default`` if missing.

        """
        if self.path.exists():
            config = self.load()
            value = config.get(key, default)
            # list keys are stored as a JSON list; tolerate a string value
            # holding the entries space-joined
            if key in LIST_KEYS and isinstance(value, str):
                value = value.split()
            return value
        return default

    def set(self: Config, key: str, value: Any) -> None:
        """Read-merge-write one config key.

        The read-merge-write holds a per-config kernel flock, so setters in
        concurrent processes never revert each other's keys.

        Args:
            key: Config key.
            value: Value to write.

        Raises:
            ValueError: When ``key`` is fixed at init
                (:data:`IMMUTABLE_KEYS`) and ``value`` would change its
                stored value.

        """
        # immutable keys admit their initial write but never a change (see
        # the IMMUTABLE_KEYS rationale)
        if key in IMMUTABLE_KEYS:
            current = self.get(key)
            if current is not None and value != current:
                raise ValueError(f'{key} is fixed at init and cannot be changed.')
        # serialize the read-merge-write across processes with a kernel flock
        # on a config sidecar (auto-released if the holder dies) -- concurrent
        # setters (a parent retune racing the child's own title write)
        # otherwise load the same snapshot and the loser's key silently
        # reverts; a sidecar, not the tree-wide .worktrees flock: init holds
        # that lock across init.sh, whose `config _set` lands here in a
        # subprocess, so re-taking it would deadlock every init
        lock_path = self.path.parent / (CONFIG_FILE + LOCK_FILE)
        with open(lock_path, 'a', encoding='utf-8') as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            # read existing config
            config = {}
            if self.path.exists():
                config = self.load()
            # merge value
            config[key] = value
            # write config -- atomic, so a kill mid-retune cannot tear the file
            # a later node load would refuse to parse
            text = json.dumps(config, indent=2)
            fractal.util.filesystem.write_atomic(self.path, text + '\n')

    def validate(self: Config, config: Optional[dict[str, Any]] = None) -> None:
        """Validate the launch-time config invariants the loop depends on.

        The one launch validator. Checks only the keys present (and not
        ``None``) in ``config``, so it can be called with a node's effective
        (merged) config; ``None`` validates the stored ``config.json`` -- the
        :meth:`Node.start` re-check, since the documented steering path edits
        the file directly, bypassing the init/update setters' checks. Rejects
        a non-numeric or non-finite cost (NaN/Infinity slip past every
        comparison), a non-positive ceiling (which makes the subtree check
        finish the node at $0), a per-iter/step cap with no ceiling (which
        the loop cannot enforce once the per-iter budget drains), an
        out-of-range reserve, a broken ``step <= iter <= run`` cost
        ordering, a non-integer or degenerate integer cap (a non-positive
        ``max_iters`` reads as unlimited in the loop), a non-bool mode flag
        (the loop's ``bool()`` coercion reads a hand-edited ``"false"``
        string as ``True``), a bare-number or zero-truncating duration
        (which bricks the loop at launch or at its mid-run re-reads), an
        absolute or ``..`` list-key entry (a scope root that never matches
        the commit pipeline's relative prefix check bricks every scoped
        commit; a ``clone_dirs`` entry would reach outside the worktree it
        warms), a non-canonical list-key spelling (``./src``, ``src/`` --
        the setters store canonical form, so only a hand-edit lands one,
        and the literal prefix match would refuse every scoped commit),
        and a ``.`` cache dir (which would clone the whole checkout
        -- as a scope root it is legal, naming the project itself).

        Args:
            config: The config mapping to validate; the stored
                ``config.json`` if omitted.

        Raises:
            ValueError: On any violated invariant.

        """
        # load the stored config when none is given
        if config is None:
            config = self.load()
        # cost values must be numeric -- a hand-edited file can hold any JSON
        # type, and bool subclasses int, so check the exact type -- and
        # finite: NaN/Infinity slip past every comparison below (all False
        # for non-finite floats), so reject both up front
        for cost_key in COST_KEYS:
            cost_value = config.get(cost_key)
            if cost_value is None:
                continue
            if type(cost_value) not in (int, float):
                raise ValueError(f'{cost_key} must be a number.')
            if not math.isfinite(cost_value):
                raise ValueError(f'{cost_key} must be a finite number.')
        # integer caps must be plain ints (bool excluded, as above) and
        # non-negative; max_iters must be positive (a non-positive cap reads
        # as unlimited in the loop)
        for int_key in INT_KEYS:
            int_value = config.get(int_key)
            if int_value is None:
                continue
            if type(int_value) is not int:
                raise ValueError(f'{int_key} must be an integer.')
            if int_key == 'max_iters':
                if int_value <= 0:
                    raise ValueError('max_iters must be greater than 0.')
            elif int_value < 0:
                raise ValueError(f'{int_key} must be >= 0.')
        # mode flags must be plain bools -- the loop coerces them with
        # bool(), so a hand-edited "false" string reads as True and launches
        # the exact mode the operator wrote the file to disable
        for bool_key in BOOL_KEYS:
            bool_value = config.get(bool_key)
            if bool_value is None:
                continue
            if type(bool_value) is not bool:
                raise ValueError(f'{bool_key} must be a boolean.')
        # alias cost ceilings
        max_cost = config.get('max_cost')
        max_iter_cost = config.get('max_iter_cost')
        max_step_cost = config.get('max_step_cost')
        reserve_budget = config.get('reserve_budget')
        # a ceiling must be positive: 0/negative degenerates the subtree check,
        # and a non-positive per-iter/step cap floors the step leash at 0 so
        # every step skips 'over budget' -- an empty-iteration spin that books
        # a dishonest 'over budget' on a run that never spent
        for cap_key, cap in (
            ('max_cost', max_cost),
            ('max_iter_cost', max_iter_cost),
            ('max_step_cost', max_step_cost),
        ):
            if cap is not None and cap <= 0:
                raise ValueError(f'{cap_key} must be greater than 0.')
        # a per-iter/step cap with no run ceiling can't be enforced (once the
        # per-iter budget drains, later steps run unbounded) -- init rejects
        # the combination, so the file re-check must too
        if max_cost is None:
            if max_iter_cost is not None:
                raise ValueError('max_iter_cost requires max_cost.')
            if max_step_cost is not None:
                raise ValueError('max_step_cost requires max_cost.')
        # reserve must sit in [0, 99% of max_cost)
        if reserve_budget is not None:
            if reserve_budget < 0:
                raise ValueError('reserve_budget must be >= 0.')
            if max_cost is not None and reserve_budget >= 0.99 * max_cost:
                raise ValueError('reserve_budget must be < 99% of max_cost.')
        # cost ordering: step <= iter <= run
        if max_iter_cost is not None and max_cost is not None:
            if max_iter_cost > max_cost:
                raise ValueError(
                    f'max_iter_cost ${max_iter_cost:.2f}'
                    f' exceeds max_cost ${max_cost:.2f}.'
                )
        if max_step_cost is not None and max_iter_cost is not None:
            if max_step_cost > max_iter_cost:
                raise ValueError(
                    f'max_step_cost ${max_step_cost:.2f}'
                    f' exceeds max_iter_cost ${max_iter_cost:.2f}.'
                )
        if max_step_cost is not None and max_cost is not None:
            if max_step_cost > max_cost:
                raise ValueError(
                    f'max_step_cost ${max_step_cost:.2f}'
                    f' exceeds max_cost ${max_cost:.2f}.'
                )
        # durations must carry a unit suffix (a bare number bricks the loop)
        # and a whole second -- the loop truncates to integral seconds and
        # rejects zero, so a stored 0s/0.5s would crash its mid-run re-reads
        durations: dict[str, float] = {}
        for key in DURATION_KEYS:
            value = config.get(key)
            if value is None:
                continue
            seconds = fractal.util.parse_duration_seconds(str(value))
            if seconds is None:
                raise ValueError(
                    f'{key} must be a duration with a unit suffix'
                    ' (e.g. 30s, 10m, 1.5h).'
                )
            if int(seconds) <= 0:
                raise ValueError(f'{key} must be at least 1 second.')
            durations[key] = seconds
        # pacing invariants the loop enforces at construction -- checked here
        # too so a bad combination is refused at init/start, not deep inside
        # the tmux pane where it strands the node idle with no run row
        interval = durations.get('interval')
        if interval is not None:
            # interval and sleep are rival pacing keys (fixed cadence vs
            # fixed gap) -- the loop rejects both set
            if durations.get('sleep') is not None:
                raise ValueError('interval and sleep are mutually exclusive.')
            # interval caps the per-iteration timeout (an iteration cannot run
            # past its slot)
            iter_timeout = durations.get('iter_timeout')
            if iter_timeout is not None and iter_timeout > interval:
                iter_timeout_value = config.get('iter_timeout')
                interval_value = config.get('interval')
                raise ValueError(
                    f'iter_timeout ({iter_timeout_value}) exceeds'
                    f' interval ({interval_value}).'
                )
        # list-key entries must be repo-relative, exactly as init enforces --
        # an absolute or '..' entry (any JSON type can land here via a
        # hand-edit, as above) points outside the tree: a scope root never
        # matches the commit pipeline's relative prefix check, bricking every
        # scoped commit, and a clone_dirs entry would clone over a path
        # outside the worktree it was meant to warm
        for key in LIST_KEYS:
            entries = config.get(key)
            if entries is None:
                continue
            # tolerate the space-joined string form the read path splits
            if isinstance(entries, str):
                entries = entries.split()
            entries_are_list = isinstance(entries, list)
            if entries_are_list and all(isinstance(entry, str) for entry in entries):
                for entry in entries:
                    rel = pathlib.PurePosixPath(entry)
                    if rel.is_absolute() or '..' in rel.parts:
                        raise ValueError(
                            f'{key} must be a repo-relative subdirectory, not'
                            f' {entry!r} (no absolute or ".." paths).'
                        )
                    # '.' names the project root: a legal scope (the commit
                    # boundary collapses to the project) but never a cache
                    # dir, where it would clone the entire checkout over the
                    # worktree root
                    if key == 'clone_dirs' and not rel.parts:
                        raise ValueError(
                            f'clone_dirs must name a subdirectory, not {entry!r}.'
                        )
                    # the setters store canonical form, so only a hand-edit
                    # lands a './src', 'src/', or 'a//b' here -- spellings
                    # the commit pipeline's literal prefix match reads as
                    # out of scope for every change, refusing every commit
                    if entry != rel.as_posix():
                        raise ValueError(
                            f'{key} entries must be canonical, not {entry!r}'
                            f' (write it as {rel.as_posix()!r}).'
                        )
            else:
                raise ValueError(f'{key} must be a list of strings.')

    def reconcile(self: Config) -> dict[str, tuple[Any, Any]]:
        """Reconcile the registry cap row to this node's config file.

        The loop enforces the caps in ``config.json``, so a post-spawn config
        edit is live budget truth -- but the ``nodes`` registry row keeps the
        spawn-time values, and a registry reader can kill a node at a stale
        cap. Config wins: drifted caps are pushed over the registry row and
        reported. Keys absent from the config are left alone (a registry-only
        cap is its own problem, not drift); ``fractal node update`` writes
        both sides in one step and remains the supported retune path.

        Returns:
            Mapping of drifted key to ``(config, registry)`` values -- empty
            when nothing drifted or the node has no registry row (user/root
            node).

        """
        # skip nodes without a registry row (user/root node)
        branch = self._node.branch
        rows = self._node.db.read('nodes', where={'node': branch}, limit=1)
        if not rows:
            return {}
        row, *_ = rows
        # collect config caps that drifted from the registry row
        drifted = {}
        for key in ('max_cost', 'max_depth', 'max_children', 'max_descendants'):
            config_value = self.get(key)
            if config_value is not None and config_value != row[key]:
                drifted[key] = (config_value, row[key])
        if not drifted:
            return {}
        # config wins: heal the registry row to the config caps
        updates = {key: values[0] for key, values in drifted.items()}
        self._node.db.update(updates, 'nodes', where={'node': branch})
        return drifted
