"""Implements ``TuiData`` -- read-only primitives over the central database.

The cockpit's only database surface: branch-keyed path resolution plus raw
read-only SQL readers, each scoped by node to a caller-held connection --
every section loader opens one, runs its reads, and closes it, so a refresh
pass costs one short-lived connection per uncached section. ``Node`` objects
are deliberately absent from the read path -- their path properties shell out
to git, which at tree scale would dominate a poll tick; paths resolve once
here (one batched ``git worktree list``) and cache per branch.
Nothing in this module ever writes -- in particular no
``Radio.feed``/``read``/``reply``/``react``, which all stamp read state.
Shaping into pane contracts lives in ``fractal.tui.snapshot``; writes live in
``fractal.tui.actions``.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import math
import os
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any, Optional

import fractal.core.agent
import fractal.core.worktree
import fractal.util
from fractal.constants import CONFIG_FILE, HEADLESS_FILE, PGID_FILE, STATUS_FILE
from fractal.core.node import Node, _recorded_group, node_dir, tmux_session_name

__all__ = [
    'leaf_of',
    'user_tag',
    'display_name_of',
    'parse_ts',
    'span',
    'TuiData',
]

# a short busy timeout: a reader contending with a mid-checkpoint writer keeps
# the UI thread for at most this long; the builder retries on the next tick
_READ_TIMEOUT_S = 0.25

# the pending-signal precedence (the most severe set this run wins the display)
_SIGNAL_PRECEDENCE = ('kill', 'pause', 'stop', 'finish', 'exit')


def leaf_of(branch: str) -> str:
    """Return the node's short name: the last dotted segment of its branch."""
    return branch.split('.')[-1]


def user_tag(branch: str, root_branch: str) -> str:
    """Return the `` (user)`` display suffix for the root branch, else empty."""
    return ' (user)' if branch == root_branch else ''


def display_name_of(branch: str, title: Optional[str] = None) -> str:
    """Return the node's display name: its stored title, else the de-slugged leaf."""
    return title or fractal.util.name_to_title(leaf_of(branch))


def parse_ts(value: Optional[str]) -> Optional[dt.datetime]:
    """Parse a ``utc_now``-format timestamp into an aware-UTC datetime."""
    if not value:
        return None
    parsed = dt.datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ')
    return parsed.replace(tzinfo=dt.UTC)


def span(started_at: Optional[str], ended_at: Optional[str]) -> Optional[float]:
    """Return the wall seconds between two timestamps; ``None`` if either is missing."""
    start, end = parse_ts(started_at), parse_ts(ended_at)
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


class TuiData:
    """Branch-keyed path resolution + raw read-only SQL readers."""

    def __init__(self: TuiData, root: Node) -> None:
        """Initialize ``TuiData``.

        Args:
            root: The user (root) node the cockpit opened onto. Its ``nodes``
                registry is the source of topology; descendant branches resolve
                to node directories lazily and cache them.

        """
        self._root = root
        # resolve the root's git-backed paths once (each property shells out)
        self._repo_dir = root.repo_dir
        self._root_branch = root.branch
        self._dirs: dict[str, pathlib.Path] = {self._root_branch: root.node_dir}
        self._worktrees: dict[str, pathlib.Path] = {}
        self._nodes: dict[str, Node] = {self._root_branch: root}

    @property
    def root(self: TuiData) -> Node:
        """Return the user (root) node."""
        return self._root

    @property
    def root_branch(self: TuiData) -> str:
        """Return the user (root) branch."""
        return self._root_branch

    @property
    def repo_dir(self: TuiData) -> pathlib.Path:
        """Return the main repository root."""
        return self._repo_dir

    @property
    def db_dir(self: TuiData) -> pathlib.Path:
        """Return the root node's data directory (holds the central database)."""
        return self._dirs[self._root_branch]

    def refresh_worktrees(self: TuiData) -> None:
        """Re-read the branch-to-worktree map (one ``git worktree list``)."""
        self._worktrees = fractal.util.git.worktree_map(self._repo_dir)

    def registry_branches(self: TuiData) -> list[str]:
        """Return every registered descendant branch, in creation order.

        ``node_id`` is the insertion order (spawn order) -- the order the tree
        pane shows; the built ``db.read`` would instead return newest-first.
        """
        rows = self._root.db.read(query='SELECT node FROM nodes ORDER BY node_id')
        return [row['node'] for row in rows]

    def registry_titles(self: TuiData) -> dict[str, str]:
        """Return the branch -> title map from the nodes registry (display names)."""
        rows = self._root.db.read(query='SELECT node, title FROM nodes')
        return {row['node']: row['title'] for row in rows if row['title']}

    def node_dir(self: TuiData, branch: str) -> Optional[pathlib.Path]:
        """Return a branch's node data directory (``None`` if unavailable).

        Resolved once and cached: the project component from
        ``fractal.core.worktree.project_path`` composed through the pure
        ``fractal.core.node.node_dir`` formula (no git, no subprocess). A
        branch with no live worktree, or whose directory holds no
        ``config.json``, is unavailable (hidden from the tree).
        """
        cached = self._dirs.get(branch)
        if cached is not None:
            return cached
        worktree = self._worktrees.get(branch)
        if worktree is None:
            return None
        project = fractal.core.worktree.project_path(self._repo_dir, branch)
        resolved = node_dir(worktree, project, branch)
        if not (resolved / CONFIG_FILE).exists():
            return None
        self._dirs[branch] = resolved
        return resolved

    def node(self: TuiData, branch: str) -> Optional[Node]:
        """Return a materialized ``Node`` for a branch (the write path only).

        Reads never need a ``Node``; actions and chat do (``Radio`` wraps one).
        """
        node = self._nodes.get(branch)
        if node is not None:
            return node
        worktree = self._worktrees.get(branch)
        if worktree is None or self.node_dir(branch) is None:
            return None
        node = Node(worktree)
        self._nodes[branch] = node
        return node

    def evict(self: TuiData, branch: str) -> None:
        """Drop a branch's cached paths/node so the next access re-resolves."""
        if branch != self._root_branch:
            self._dirs.pop(branch, None)
            self._nodes.pop(branch, None)

    def connect(self: TuiData) -> sqlite3.Connection:
        """Open one short-timeout read-only connection to the central database.

        The caller holds it for one section's reads and closes it; a
        contending writer blocks a read for at most the short busy timeout.

        Raises:
            FileNotFoundError: If the database file is missing.
            sqlite3.OperationalError: If the database is unavailable.

        """
        return self._root.db.connect(read_only=True, timeout=_READ_TIMEOUT_S)

    @contextlib.contextmanager
    def reader(self: TuiData) -> Iterator[sqlite3.Connection]:
        """Yield a short-timeout read-only connection, closing it on exit.

        Callers keep their own ``except sqlite3.Error`` policy at the call
        site (live-data rule: failure handling stays visible per site).
        """
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def status(self: TuiData, branch: str) -> str:
        """Return the branch's authoritative live status (its ``.status`` file)."""
        node_dir = self.node_dir(branch)
        if node_dir is None:
            return 'idle'
        try:
            return (node_dir / STATUS_FILE).read_text(encoding='utf-8').strip()
        except (OSError, UnicodeDecodeError):
            return 'idle'

    def config(self: TuiData, branch: str) -> dict:
        """Return the branch's ``config.json`` (``{}`` if missing or malformed)."""
        node_dir = self.node_dir(branch)
        if node_dir is None:
            return {}
        try:
            config = json.loads((node_dir / CONFIG_FILE).read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        # config.json is agent-editable, so a hand edit may leave a non-object
        # top level (e.g. null) -- degrade it like malformed JSON, never an
        # AttributeError on .get that crashes the whole cockpit build
        return config if isinstance(config, dict) else {}

    def tmux_session_name(self: TuiData, branch: str) -> str:
        """Return the tmux session name for a branch (the pure core formula).

        Composed from the cached repo dir (rather than via a ``Node``) to
        keep the read path off git.
        """
        return tmux_session_name(self._repo_dir, branch)

    def live_sessions(self: TuiData) -> frozenset[str]:
        """Return the set of live tmux session names (one ``list-sessions``).

        Empty when tmux is unavailable -- whether the binary is absent
        (``OSError``) or the server is not running (non-zero exit). The cockpit
        reconciles a stale ``active`` (a crashed loop's leftover ``.status``)
        against this set for display only -- it never writes, so the honest
        ``exited`` shows until a writer (``node start``/``merge``/...) persists it.
        """
        return fractal.util.tmux.sessions()

    def loop_alive(self: TuiData, branch: str, sessions: frozenset[str]) -> bool:
        """Return whether the branch's selected loop runtime is alive.

        Headless loops are process-group supervised through their launch-time
        PGID record. Tmux loops use the caller's one-per-refresh session set.
        This is display-only: an existing group whose identity cannot be
        verified stays active, while a definitively recycled group renders
        settled. Neither result mutates lifecycle state.
        """
        node_dir = self.node_dir(branch)
        if node_dir is not None and (node_dir / HEADLESS_FILE).is_file():
            pgid_file = node_dir / PGID_FILE
            try:
                recorded_at = pgid_file.stat().st_mtime
                pgid = int(pgid_file.read_text(encoding='utf-8').strip())
                os.killpg(pgid, 0)
            except PermissionError:
                return True
            except (FileNotFoundError, ProcessLookupError, ValueError):
                return False
            return _recorded_group(pgid, recorded_at) is not False
        return self.tmux_session_name(branch) in sessions

    @staticmethod
    def rows(
        connection: sqlite3.Connection,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> list[dict]:
        """Run a parameterized read-only query and return dict rows."""
        return [dict(row) for row in connection.execute(query, params).fetchall()]

    def signal(self: TuiData, connection: sqlite3.Connection, branch: str) -> str:
        """Return the highest-precedence pending signal of the latest run."""
        runs = self.rows(
            connection=connection,
            query="SELECT run_id FROM runs WHERE node = ? AND status = 'active'"
            ' ORDER BY run_id DESC LIMIT 1',
            params=(branch,),
        )
        if not runs:
            runs = self.rows(
                connection=connection,
                query='SELECT run_id FROM runs WHERE node = ? ORDER BY run_id DESC LIMIT 1',
                params=(branch,),
            )
        if not runs:
            return ''
        present = {
            row['signal']
            for row in self.rows(
                connection=connection,
                query='SELECT DISTINCT signal FROM signals WHERE run_id = ?',
                params=(runs[0]['run_id'],),
            )
        }
        for signal in _SIGNAL_PRECEDENCE:
            if signal in present:
                return signal
        return ''

    def tables(
        self: TuiData,
        connection: sqlite3.Connection,
        branch: str,
    ) -> tuple[list[dict], list[dict], list[dict]]:
        """Return the node's whole ``runs``/``iters``/``steps``, newest first."""
        runs = self.rows(
            connection=connection,
            query='SELECT * FROM runs WHERE node = ? ORDER BY run_id DESC',
            params=(branch,),
        )
        iters = self.rows(
            connection=connection,
            query='SELECT * FROM iters WHERE node = ? ORDER BY iter_id DESC',
            params=(branch,),
        )
        steps = self.rows(
            connection=connection,
            query='SELECT * FROM steps WHERE node = ? ORDER BY step_id DESC',
            params=(branch,),
        )
        return runs, iters, steps

    def run_costs(
        self: TuiData,
        connection: sqlite3.Connection,
        branch: str,
    ) -> dict[int, tuple[Optional[int], float]]:
        """Return ``{run_id: (parent_run_id, own step-cost sum)}`` for the node.

        The per-branch ingredient of the run-scope subtree-cost chase (the
        ``runs.parent_run_id`` chain), computed without recursion or extra
        connections.
        """
        parents = {
            row['run_id']: row['parent_run_id']
            for row in self.rows(
                connection=connection,
                query='SELECT run_id, parent_run_id FROM runs WHERE node = ?',
                params=(branch,),
            )
        }
        costs = {
            row['run_id']: row['cost']
            for row in self.rows(
                connection=connection,
                query='SELECT run_id, COALESCE(SUM(cost), 0) AS cost'
                ' FROM steps WHERE node = ? GROUP BY run_id',
                params=(branch,),
            )
        }
        return {
            run_id: (parent, costs.get(run_id, 0.0))
            for run_id, parent in parents.items()
        }

    def run_steps(
        self: TuiData,
        connection: sqlite3.Connection,
        branch: str,
    ) -> dict[int, list[tuple[float, float]]]:
        """Return ``{run_id: [(ended_epoch, cost), ...]}`` for costed steps.

        The time-resolved ingredient of the cost-to-date chase: a step's cost
        counts from the instant it ended (an open step counts only at
        "all time", so the live view stays exact between writes).
        """
        rows = self.rows(
            connection=connection,
            query='SELECT run_id, ended_at, cost FROM steps'
            ' WHERE node = ? AND cost IS NOT NULL',
            params=(branch,),
        )
        result: dict[int, list[tuple[float, float]]] = {}
        for row in rows:
            ended = parse_ts(row['ended_at'])
            end_epoch = ended.timestamp() if ended else math.inf
            result.setdefault(row['run_id'], []).append((end_epoch, row['cost']))
        return result

    def run_ids(
        self: TuiData,
        connection: sqlite3.Connection,
        branch: str,
    ) -> list[int]:
        """Return the node's run ids, newest first (the log's run ordinals)."""
        rows = self.rows(
            connection=connection,
            query='SELECT run_id FROM runs WHERE node = ? ORDER BY run_id DESC',
            params=(branch,),
        )
        return [row['run_id'] for row in rows]

    def log_rows(
        self: TuiData,
        connection: sqlite3.Connection,
        branches: tuple[str, ...],
        *,
        limit: int = 120,
    ) -> list[dict]:
        """Return the ``activity`` view newest first, joined for display numbers.

        Ordered exactly like ``fractal node activity``; the LEFT JOINs carry
        the step/iter numbers and step name the event log renders. One branch
        for the scoped log, several for the subtree log.
        """
        marks = ', '.join('?' for _ in branches)
        return self.rows(
            connection=connection,
            query='SELECT a.*, s.step AS step_n, s.step_name AS step_name,'
            ' i.iter AS iter_n'
            ' FROM activity a'
            ' LEFT JOIN steps s ON a.step_id = s.step_id'
            ' LEFT JOIN iters i ON a.iter_id = i.iter_id'
            f' WHERE a.node IN ({marks})'
            ' ORDER BY a.timestamp DESC, a.run_id DESC, a.iter_id DESC,'
            ' a.step_id DESC'
            ' LIMIT ?',
            params=(*branches, int(limit)),
        )

    def message_rows(
        self: TuiData,
        connection: sqlite3.Connection,
        branch: str,
    ) -> list[dict]:
        """Return the node's top-level messages (replies excluded), raw.

        ``is_read`` derives from the owner's own receipt in ``reads`` --
        a pure read, never a stamp.
        """
        return self.rows(
            connection=connection,
            query='SELECT m.*, EXISTS('
            ' SELECT 1 FROM reads r'
            ' WHERE r.message_id = m.message_id AND r.node = m.node'
            ') AS is_read'
            ' FROM messages m'
            ' WHERE m.node = ? AND m.parent_message_id IS NULL',
            params=(branch,),
        )

    def react_counts(
        self: TuiData,
        connection: sqlite3.Connection,
        branch: str,
    ) -> dict[int, tuple[int, int]]:
        """Return ``{message_id: (positive, negative)}`` react counts."""
        rows = self.rows(
            connection=connection,
            query='SELECT message_id,'
            ' SUM(CASE WHEN value = 1 THEN 1 ELSE 0 END) AS pos,'
            ' SUM(CASE WHEN value = -1 THEN 1 ELSE 0 END) AS neg'
            ' FROM reacts WHERE message_id IN'
            ' (SELECT message_id FROM messages WHERE node = ?)'
            ' GROUP BY message_id',
            params=(branch,),
        )
        return {row['message_id']: (row['pos'], row['neg']) for row in rows}

    def channel_rows(
        self: TuiData,
        connection: sqlite3.Connection,
        branch: str,
    ) -> list[dict]:
        """Return the node's channels with their read/write-only flags."""
        return self.rows(
            connection=connection,
            query='SELECT channel, read_only, write_only FROM channels'
            ' WHERE node = ? ORDER BY channel_id',
            params=(branch,),
        )

    def archive_rows(
        self: TuiData,
        connection: sqlite3.Connection,
        branch: str,
    ) -> list[dict]:
        """Return the node's archived (saved) message copies, raw."""
        return self.rows(
            connection=connection,
            query='SELECT * FROM archive WHERE node = ?',
            params=(branch,),
        )

    def live_session(
        self: TuiData,
        connection: sqlite3.Connection,
        branch: str,
        agent: str,
    ) -> Optional[str]:
        """Return the node's newest woven session in its active run.

        The loop stamps each step's real session onto its ``steps`` row as
        soon as the agent's stream opens, so the newest stamped step is
        forkable within seconds of launch and carries the node's working
        context across iteration boundaries. ``None`` when the node is
        settled, or weaves no session yet -- the value a "chat with this
        agent" would fork.
        """
        # steps record the backend's registered name, not the configured
        # command (which may carry an alias or flags); an unregistered agent
        # -- or one behind a broken deployment hook (RuntimeError) -- keys on
        # its bare word and matches no woven rows
        base = agent.split()[0] if agent else agent
        try:
            base = fractal.core.agent.resolve(base, root=self.db_dir).name
        except (ValueError, RuntimeError):
            pass
        rows = self.rows(
            connection=connection,
            query='SELECT s.session FROM steps s'
            ' JOIN runs r ON s.run_id = r.run_id'
            " WHERE s.node = ? AND r.status = 'active'"
            ' AND s.agent = ? AND s.session IS NOT NULL'
            ' ORDER BY s.step_id DESC LIMIT 1',
            params=(branch, base),
        )
        return rows[0]['session'] if rows else None
