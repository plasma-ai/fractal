"""Shared helpers for ``fractal`` CLI commands."""

from __future__ import annotations

import datetime
import functools
import json
import os
import pathlib
import sys
from collections.abc import Callable
from csv import DictWriter
from typing import Any, Optional

import typer

import fractal.core.worktree
import fractal.util
from fractal.constants import (
    CONFIG_FILE,
    FRACTAL_FOLDER,
    LOCK_FILE,
    PROJECT_FOLDER,
    WORKTREES_FOLDER,
)
from fractal.core.agent import StreamEvent
from fractal.core.config import DEFAULT_RESERVE_FRACTION, RESERVE_PRECISION
from fractal.core.node import Node
from fractal.typing import PathLike

__all__ = [
    'SQLITE_INT_MAX',
    'command',
    'require_non_negative',
    'require_timestamp',
    'parse_reserve_budget',
    'StreamRenderer',
    'print_rows',
    'print_json',
    'init_node',
    'resolve_init_target',
    'resolve_node',
    'resolve_sender',
    'resolve_user_node',
    'resolve_target',
    'resolve_ledger_target',
]

# signed 64-bit ceiling for SQLite INTEGER columns; an integer cap at or above
# this raises a raw "int too large to convert" from the adapter downstream
SQLITE_INT_MAX = 2**63

# tool-result preview budget: two to three 80-column terminal lines, so one
# verbose tool call cannot flood the streamed transcript
_TOOL_RESULT_MAX = 200
_DIM = '\033[2m'
_RESET = '\033[0m'
_BLUE = '\033[34m'
_YELLOW = '\033[33m'


def command(
    app: typer.Typer,
    name: str,
    **kwargs: Any,
) -> Callable:
    """Register a CLI command on ``app`` with error wrapping.

    A command error prints ``Error: <message>`` on stderr and exits 2,
    beside typer's own usage errors, so exit 1 is left to mean exactly
    the command's own nonzero outcome and a script gating on one can
    never read a failed run as the other.
    """

    def decorator(f: Callable, /) -> Callable:
        if private := name.startswith('_'):
            kwargs.setdefault('hidden', True)

        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return f(*args, **kwargs)
            except (typer.Exit, typer.Abort, typer.BadParameter):
                raise
            except BrokenPipeError:
                # a downstream reader closed the pipe (not an error):
                # point stdout at devnull so the interpreter's exit
                # flush stays quiet, and end the pipeline successfully
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, sys.stdout.fileno())
                raise SystemExit(0) from None
            except Exception as e:
                error = type(e).__name__ if private else 'Error'
                typer.echo(f'{error}: {e}', err=True)
                raise SystemExit(2) from None

        return app.command(name, **kwargs)(wrapper)

    return decorator


def require_non_negative(**limits: Optional[float]) -> None:
    """Reject negative numeric CLI options as ``BadParameter``.

    Each keyword maps a limit option's name to its value (``None`` is
    skipped); the first negative raises with that option's flag spelling.
    Centralizes the ``>= 0`` guard so every numeric ``--max-*`` cap is
    validated uniformly at the CLI boundary -- unbounded is expressed by
    omitting the flag, never by a negative sentinel.

    Args:
        **limits: Option name mapped to its value (e.g. ``max_depth=max_depth``).

    Raises:
        typer.BadParameter: If any value is negative.

    """
    for name, value in limits.items():
        if value is None:
            continue
        flag = name.replace('_', '-')
        if value < 0:
            raise typer.BadParameter(f'--{flag} must be >= 0.')
        # an integer cap is written to a SQLite INTEGER column; one that overflows
        # a signed 64-bit int raises a raw adapter error (and can desync config
        # from the DB on update), so reject it here -- float costs (REAL) are exempt
        if isinstance(value, int) and not isinstance(value, bool):
            if value >= SQLITE_INT_MAX:
                raise typer.BadParameter(f'--{flag} must be < {SQLITE_INT_MAX}.')


def require_timestamp(**instants: Optional[str]) -> None:
    """Reject non-ISO-8601 timestamp CLI options as ``BadParameter``.

    Each keyword maps an instant option's name to its value (``None`` is
    skipped). The stored instants are ISO 8601 strings and the filters
    compare them lexicographically, so an unparseable value is not an
    error downstream -- it silently empties the listing (anything sorting
    above the rows) or silently filters nothing (anything below), and a
    mistyped ``--since`` becomes indistinguishable from an empty mailbox.
    A bare date is accepted: it sorts as the prefix of that day's first
    instant, which is exactly the cut it names.

    Args:
        **instants: Option name mapped to its value (e.g. ``since=since``).

    Raises:
        typer.BadParameter: If any value is not an ISO 8601 date or
            timestamp.

    """
    for name, value in instants.items():
        if value is None:
            continue
        flag = name.replace('_', '-')
        try:
            datetime.datetime.fromisoformat(value)
        except ValueError:
            raise typer.BadParameter(
                f'--{flag} expects an ISO 8601 date or timestamp'
                f' (e.g. 2026-01-31 or 2026-01-31T14:00:00Z); got {value!r}.'
            ) from None


def parse_reserve_budget(
    value: Optional[str],
    max_cost: Optional[float],
    *,
    default: str = f'{DEFAULT_RESERVE_FRACTION:.0%}',
) -> Optional[float]:
    """Resolve ``--reserve-budget`` to a USD amount.

    The value is a USD number or ``N%`` of ``max_cost``; when omitted it falls
    back to ``default`` (``10%`` of ``max_cost``), so a budget reserves a cleanup
    buffer by default, and with no ``max_cost`` there is no reserve. The reserve
    is not enforced -- it only moves when the node enters reserve mode (the budget
    is treated as drained ``reserve_budget`` USD before ``max_cost`` is reached).

    Args:
        value: The raw ``--reserve-budget`` string (USD or ``N%``), or ``None``
            to take ``default``.
        max_cost: The node's ``--max-cost`` in USD; required when ``value`` is an
            explicit reserve.
        default: Reserve applied when ``value`` is ``None`` (USD or ``N%``).

    Returns:
        The reserve in USD -- ``default`` applied to ``max_cost`` when ``value``
        is ``None``, or ``None`` when neither ``value`` nor ``max_cost`` is set.

    Raises:
        typer.BadParameter: If an explicit value is given without ``max_cost``,
            is not a number, is negative, or is >= 99% of ``max_cost``.

    """
    if value is None:
        if max_cost is None:
            return None
        value = default
    if max_cost is None:
        raise typer.BadParameter('--reserve-budget requires --max-cost.')
    if max_cost <= 0:
        raise typer.BadParameter('--max-cost must be greater than 0.')
    value = value.strip()
    try:
        if value.endswith('%'):
            reserve = float(value[:-1]) / 100 * max_cost
        else:
            reserve = float(value)
    except ValueError:
        raise typer.BadParameter('--reserve-budget must be a number or N%.') from None
    if reserve < 0:
        raise typer.BadParameter('--reserve-budget must be >= 0.')
    if reserve >= 0.99 * max_cost:
        raise typer.BadParameter('--reserve-budget must be < 99% of --max-cost.')
    # money materializes at display precision -- a bare percent product
    # (10% of $6) would otherwise persist binary noise into config.json
    # and every echo that quotes it
    return round(reserve, RESERVE_PRECISION)


class StreamRenderer:
    """ANSI terminal renderer for parsed agent stream events.

    The single CLI renderer over the seam's normalized events -- one
    instance per stream, passed as the ``render`` callback to
    ``Agent.stream`` (the loop's pane rendering) and ``Node.chat`` so core never
    prints. Branches on ``kind`` only, never on the provider; session and
    cost facts render nothing (they are recorded, not displayed). Tracks
    the open text run (a delta without a trailing newline) so tool headers
    and closing summaries start below it, and ``close()`` ends a truncated
    stream on a fresh line with the ``— $?`` placeholder summary. Every
    write flushes: piped stdout is block-buffered while the driving
    command's stderr echoes are write-through, so an unflushed line would
    land out of order in a merged capture.
    """

    def __init__(self: StreamRenderer) -> None:
        """Initialize ``StreamRenderer``."""
        self._streaming = False
        self._open = False
        self._resulted = False

    def __call__(self: StreamRenderer, event: StreamEvent) -> None:
        """Render one parsed agent stream event."""
        self._open = True
        # text deltas stream inline (block opens ride as newline deltas); a
        # delta without a trailing newline leaves the text run open
        if event.kind == 'text':
            print(event.text, end='', flush=True)
            self._streaming = not event.text.endswith('\n')
        # tool-use headers, below any open text run
        elif event.kind == 'tool':
            self._break()
            print(f'\n{_DIM}{_BLUE}> {event.tool}{_RESET}', end='', flush=True)
        # tool results, truncated to a preview
        elif event.kind == 'tool_result':
            lines = event.message.split('\n')
            preview = '\n'.join(lines[:8])
            if len(preview) > _TOOL_RESULT_MAX:
                preview = preview[:_TOOL_RESULT_MAX] + '...'
            # a char-truncated preview that also has more than
            # 8 lines must still show the "more lines" signal
            if len(lines) > 8:
                preview += '\n...'
            color = _YELLOW if event.failed else _DIM
            label = ' (error)' if event.failed else ''
            # the preview's own leading newline closes any open text run
            self._streaming = False
            print(f'\n{color}{preview}{label}{_RESET}', end='', flush=True)
        # the closing summary -- the facts vary per provider (a codex result
        # closes on wall time and carries no authoritative cost), so a final
        # result summarizes turns/duration/cost while a per-turn close prints
        # the recorded turn cost alone -- an absent cost reads '$?', never $0
        elif event.kind == 'result':
            self._break()
            cost_str = f'${event.cost:.4f}' if event.cost is not None else '$?'
            if event.final:
                parts = []
                if event.turns is not None:
                    parts.append(f'{event.turns} turns')
                if event.duration is not None:
                    parts.append(f'{event.duration:.1f}s')
                parts.append(cost_str)
                summary = ', '.join(parts)
            else:
                summary = cost_str
            print(f'\n{_DIM}— {summary}{_RESET}', flush=True)
            self._resulted = True
        # stream-borne errors (some agents report failures on the JSON stream,
        # not stderr, so without this a failed turn leaves no explanation)
        elif event.kind == 'error':
            print(
                f'{_YELLOW}agent error: {event.message}{_RESET}',
                file=sys.stderr,
                flush=True,
            )

    def close(self: StreamRenderer) -> None:
        """End the stream on a fresh line, settling the closing summary.

        Called by the driving command after the stream drains -- a truncated
        stream (killed before its result frame) otherwise leaves the cursor
        mid-line and shows no closing summary, so the ``— $?`` placeholder
        marks the unaccounted turn. Idempotent, and resets for the next
        stream (the loop reuses one renderer across steps).
        """
        if not self._open:
            return
        self._break()
        if not self._resulted:
            print(f'\n{_DIM}— $?{_RESET}', flush=True)
        self._open = False
        self._resulted = False

    def _break(self: StreamRenderer) -> None:
        """Close an open text run on a fresh line (a visual break)."""
        if self._streaming:
            print(flush=True)
            self._streaming = False


def print_rows(
    rows: list[dict[str, Any]],
    *,
    csv: bool = False,
    columns: Optional[list[str]] = None,
) -> None:
    """Print rows as a formatted table or CSV.

    Uses ``sys.stdout.isatty()`` to auto-detect format:
    formatted text table for terminals, CSV for pipes.
    The ``csv`` parameter enables CSV output.

    Args:
        rows: List of row dicts.
        csv: Output as CSV.
        columns: Column names for the empty-result header (emitted
            in both formats so empty output is never ambiguous).

    """
    # handle no rows -- still emit a header so empty output is never ambiguous
    if not rows:
        if not columns:
            return
        if csv or not sys.stdout.isatty():
            writer = DictWriter(
                sys.stdout,
                fieldnames=columns,
                lineterminator='\n',
            )
            writer.writeheader()
        else:
            # text-table header matching the populated-table format
            typer.echo('  '.join(columns))
            typer.echo('  '.join('-' * len(column) for column in columns))
        return
    # determine format
    output_csv = csv or not sys.stdout.isatty()
    if output_csv:
        # write CSV
        writer = DictWriter(
            sys.stdout,
            fieldnames=rows[0].keys(),
            lineterminator='\n',
        )
        writer.writeheader()
        writer.writerows(rows)
    else:
        # write formatted text table
        headers = list(rows[0].keys())
        # compute column widths
        widths = {h: len(h) for h in headers}
        for row in rows:
            for header in headers:
                value = row.get(header)
                value = str(value) if value is not None else ''
                widths[header] = max(widths[header], len(value))
        # print header
        header_line = '  '.join(h.ljust(widths[h]) for h in headers)
        typer.echo(header_line)
        typer.echo('  '.join('-' * widths[h] for h in headers))
        # print rows
        for row in rows:
            values = [row.get(header) for header in headers]
            values = [str(value) if value is not None else '' for value in values]
            typer.echo('  '.join(v.ljust(widths[h]) for v, h in zip(values, headers)))


def print_json(
    rows: list[dict[str, Any]],
    *,
    columns: Optional[list[str]] = None,
) -> None:
    """Print rows as a JSON array of objects.

    The machine-readable counterpart to ``print_rows``: one object per
    row, keys in ``columns`` order, and an empty result prints ``[]``
    (never nothing), so parsers see one shape per listing.

    Args:
        rows: List of row dicts.
        columns: Key order for the row objects (default: each row's own
            key order).

    """
    if columns:
        rows = [{column: row.get(column) for column in columns} for row in rows]
    encoded = json.dumps(rows, indent=2)
    typer.echo(encoded)


def init_node(path: PathLike) -> Node:
    """Resolve a ``Node`` for init (accepts repo root as-is).

    When an agent runs init from inside its own worktree (``_NODE`` set) with
    the default path, resolve to the caller's repo root so the new node nests
    under the main repo rather than the worktree (the parent is resolved from
    ``_NODE`` in ``Node.init``).
    """
    # an agent in its own worktree with the default path uses its repo root, but
    # only if the caller lives in the cwd's repo (a stale _NODE = wrong repo)
    caller = Node.resolve_caller()
    if caller is not None and str(path) == '.':
        try:
            cwd = pathlib.Path.cwd().resolve()
            cwd_root = Node(cwd).repo_dir
            if cwd_root == caller.repo_dir:
                return Node(caller.repo_dir)
        except RuntimeError:
            pass
    # otherwise resolve the given path as-is
    return Node(_absolute(path))


def resolve_init_target(path: PathLike) -> tuple[Node, pathlib.Path]:
    """Resolve the git-root-anchored node and project path for an init target.

    ``fractal init``/``node init`` accept a repo root or a sub-project folder as
    ``path``. The user node and every child must be anchored at the git root --
    ``Node.node_dir`` derives the ``<project>/`` prefix from the
    ``.worktrees/.project`` cache, so anchoring at a sub-project folder would
    double the prefix. This resolves the target, then returns a ``Node`` at its
    git root plus the target's project-relative path.

    Args:
        path: Repo root or sub-project folder (absolute or relative).

    Returns:
        ``(node, path)`` -- a ``Node`` at the git root and the target's path
        relative to it (``.`` for the repo root).

    Raises:
        typer.BadParameter: If the target is a linked git worktree (outside
            the main repo root, so never a sub-project path).

    """
    target = init_node(path)
    node = init_node(target.repo_dir)
    # a linked worktree (git worktree add ../feature) lives outside the main
    # repo root, so it can never be a sub-project path -- refuse with the
    # remedy instead of leaking relative_to's raw ValueError
    if not target.worktree.is_relative_to(target.repo_dir):
        raise typer.BadParameter(
            f'{target.worktree} is a linked worktree of {target.repo_dir}.'
            f' Run `fractal init` from the main checkout.'
        )
    path = target.worktree.relative_to(target.repo_dir)
    return node, path


def resolve_node(path: PathLike, *, check: bool = True) -> Node:
    """Resolve a ``Node`` instance from a path argument.

    Args:
        path: Worktree directory (absolute or relative).
        check: Require an initialized node at the resolved path (mirrors
            ``resolve_target``), so a user-facing command run outside a node
            fails cleanly instead of leaking a raw internal error. The
            pre-init private setter (``config _set``, which writes the very
            ``config.json`` this checks) passes ``check=False``.

    Returns:
        Node bound to the resolved path.

    Raises:
        typer.BadParameter: If ``path`` is a repo root with
            active worktrees (should use the worktree path), or
            (when ``check``) the resolved path is not an initialized node.

    """
    # resolve absolute path
    path = _absolute(path)
    # canonicalize to git worktree root (handles subdirectories)
    toplevel = fractal.util.git.toplevel(path, check=False)
    if toplevel is not None:
        path = toplevel.resolve()
    # detect repo root passed instead of worktree
    # (.git is a directory at the repo root, a file in worktrees)
    if (path / '.git').is_dir():
        # check for a user node on the current branch -- it nests under the project
        # prefix from the .worktrees/.project cache (sub-project nodes nest deeper)
        if branch := fractal.util.git.branch(path, check=False):
            project = fractal.core.worktree.project_path(path, branch)
            if project == '.':
                node_dir = path / FRACTAL_FOLDER / branch
            else:
                node_dir = path / project / FRACTAL_FOLDER / branch
            if (node_dir / CONFIG_FILE).exists():
                return Node(path)
        if (path / WORKTREES_FOLDER).is_dir():
            # inside a running node -- resolve to caller's worktree
            if result := Node.resolve_caller():
                return result
            # collect worktree candidates
            paths = []
            for entry in (path / WORKTREES_FOLDER).iterdir():
                if entry.is_dir() and entry.name not in (PROJECT_FOLDER, LOCK_FILE):
                    paths.append(entry)
            # one worktree -- resolve to it
            if len(paths) == 1:
                (path,) = paths
                typer.echo(f'Resolved to .worktrees/{path.name}/', err=True)
            # multiple -- error with list
            elif len(paths) > 1:
                paths = ', '.join(
                    f'.worktrees/{entry.name}/' for entry in sorted(paths)
                )
                raise typer.BadParameter(
                    f'Multiple nodes found: {paths}.'
                    f' Name one with --node, or run from its worktree.'
                )
    # otherwise construct node at worktree path
    node = Node(path)
    # require an initialized node unless a pre-init caller opted out, so a
    # user-facing command run outside a node fails cleanly (mirrors resolve_target)
    if check and not node.exists():
        raise typer.BadParameter(
            f'No fractal node at {node.worktree}. Run `fractal init` first.'
        )
    return node


def resolve_sender(path: Optional[str]) -> Node:
    """Resolve the acting sender: ``--path`` when given, else the calling node.

    The loop exports ``_NODE`` for the node whose loop is running, so a
    production send attributes its ``sender`` to that node wherever the
    agent's CLI call runs from (a detached step's cwd is not a node
    identity); an explicit ``--path`` still wins -- an operator acting as a
    node from outside it.

    Args:
        path: Explicit worktree directory, or ``None`` for the default.

    Returns:
        Node the message's ``sender`` attributes to.

    Raises:
        typer.BadParameter: If the exported ``_NODE`` does not name an
            initialized node (a stale or mistyped export).

    """
    if path is not None:
        return resolve_node(path)
    if caller := Node.resolve_caller():
        # mirror resolve_node's initialized-node check, naming the env
        # source: a stale _NODE refuses cleanly, not as an internal error
        if not caller.exists():
            raise typer.BadParameter(
                f'No fractal node at {caller.worktree} (resolved from the'
                ' exported _NODE). Unset it or pass --path.'
            )
        return caller
    # a set-but-unresolvable _NODE (a reaped worktree, a typo) refuses too:
    # silently attributing the write to the cwd's node would be worse
    if node_dir := os.environ.get('_NODE'):
        raise typer.BadParameter(
            f'No fractal node at {node_dir} (the exported _NODE names no'
            ' git worktree). Unset it or pass --path.'
        )
    return resolve_node('.')


def resolve_user_node(path: PathLike, name: Optional[str] = None) -> Node:
    """Resolve a tree's user (root) node from any checkout, by config not branch.

    The tree-wide verbs (``pause``/``resume``/``track``/...) must always
    anchor on the user node, but :func:`resolve_node` keys on the repo's
    *current* branch -- so on a non-init checkout (the user on their own
    branch while nodes run) it silently mis-scopes to a lone child or dies on
    two. :meth:`Node.resolve_user` finds the ``config.json`` marked
    ``user: true`` for the named tree (or the one owning the caller's branch)
    and pins a ``Node`` to that branch, independent of the git checkout; this
    wrapper turns its no-such-tree ``None`` into the CLI refusal.

    Args:
        path: Any path inside the repo.
        name: Root branch of the tree to act on; ``None`` infers it from the
            caller's branch.

    Returns:
        The tree's user (root) node, branch-pinned.

    Raises:
        typer.BadParameter: If the repo has no such tree, or several trees
            leave the caller's checkout ambiguous.

    """
    # an ambiguous checkout is a usage mistake, not a runtime failure -- its
    # remedy is naming the tree, so it refuses like the unknown name below
    try:
        user = Node.resolve_user(path, name=name)
    except RuntimeError as error:
        raise typer.BadParameter(str(error)) from None
    if user is None:
        repo = Node(path).repo_dir
        if name is not None:
            trees = ', '.join(tree.branch for tree in Node.user_nodes(path))
            found = f' Trees here: {trees}.' if trees else ''
            raise typer.BadParameter(f'No fractal tree {name!r} under {repo}.{found}')
        raise typer.BadParameter(
            f'No user node found under {repo}. Run `fractal init` at the repo root.'
        )
    return user


def resolve_target(path: PathLike, node: Optional[str] = None) -> Node:
    """Resolve the node to act on, anchored at the caller's worktree.

    ``path`` identifies the caller's own worktree (default cwd); ``node``,
    when given, selects another node by branch name within the same repo.
    Omitting ``node`` targets the caller's own node.

    Args:
        path: Caller's worktree directory (absolute or relative).
        node: Target node's branch name, or ``None`` for the caller.

    Returns:
        Node bound to the resolved target worktree.

    Raises:
        typer.BadParameter: If ``node`` names a branch with no
            worktree, or the resolved target is not an
            initialized node.

    """
    # resolve target node
    if node:
        # resolve absolute anchor path
        path = _absolute(path)
        # locate the named node's worktree within the same repo
        repo_dir = Node(path).repo_dir
        worktree_dir = fractal.util.git.find_worktree(repo_dir, node)
        if worktree_dir is None:
            # accept a unique short name (trailing branch segment) by
            # resolving it against the caller's registered nodes
            node = _resolve_node_name(path, node)
            worktree_dir = fractal.util.git.find_worktree(repo_dir, node)
        if worktree_dir is None:
            raise typer.BadParameter(f'No node found for branch: {node!r}.')
        target = Node(worktree_dir)
    else:
        target = resolve_node(path)
    # require an initialized node at the resolved target
    if target.exists():
        return target
    raise typer.BadParameter(f'No fractal node at {target.worktree}.')


def resolve_ledger_target(
    path: PathLike,
    node: Optional[str],
) -> tuple[Node, Optional[str], Optional[int]]:
    """Resolve a cost-ledger target, answering for deleted branches.

    A live target resolves normally as ``(node, None, None)``. A deleted
    branch fails resolution while its recorded history persists in the
    shared db, so it resolves through the caller as
    ``(caller, branch, latest_run)`` -- how the fallback applies stays per
    command (``remaining`` reports ``no budget`` without delegating;
    ``spent``/``breakdown`` substitute ``latest_run`` for an absent scope).
    The fallback accepts the same unique short names live resolution does:
    the registry rows that expansion consults die with the node, so the
    trailing segment expands against the recorded runs instead.

    Args:
        path: Caller's worktree directory (absolute or relative).
        node: Target node's branch name, or ``None`` for the caller.

    Returns:
        ``(node, deleted, latest_run)`` -- the resolved target, the deleted
        branch name (``None`` for a live target), and the deleted branch's
        latest recorded run id.

    Raises:
        typer.BadParameter: If resolution fails and the branch never
            recorded a run (there is no history to answer from), or a
            short name matches several live or recorded branches.

    """
    # a deleted branch fails resolution: answer through the caller (shared db)
    # when it has a recorded run, keep the not-found error when it never ran
    try:
        return resolve_target(path, node), None, None
    except typer.BadParameter:
        if node is None:
            raise
        caller = resolve_node(path)
        # a live-ambiguous short name re-raises the refusal: the fallback
        # answers for deleted branches, never over live twins the registry
        # itself refused to pick between
        live = []
        for row in caller.list(all_nodes=True):
            *_, leaf = row['node'].rsplit('.', 1)
            if leaf == node:
                live.append(row['node'])
        if len(live) > 1:
            raise
        latest = caller.record.run_latest(branch=node)
        if latest is None:
            # expand a short name against the recorded runs -- a unique
            # trailing-segment match answers, ambiguity refuses (mirroring
            # _resolve_node_name, whose registry rows died with the node)
            matches = []
            for branch in caller.record.run_branches():
                *_, leaf = branch.rsplit('.', 1)
                if leaf == node:
                    matches.append(branch)
            if len(matches) > 1:
                options = ', '.join(sorted(matches))
                raise typer.BadParameter(
                    f'Ambiguous node name {node!r} (matches: {options}).'
                    f' Use the full branch.'
                ) from None
            if len(matches) == 1:
                (node,) = matches
                latest = caller.record.run_latest(branch=node)
        if latest is None:
            raise
        return caller, node, latest


# ------ helper functions


def _absolute(path: PathLike) -> pathlib.Path:
    """Resolve a path argument to an absolute path (anchored at the cwd).

    Args:
        path: Path to absolutize (absolute or relative).

    Returns:
        The resolved absolute path.

    """
    path = pathlib.Path(path)
    if not path.is_absolute():
        path = pathlib.Path.cwd() / path
    return path.resolve()


def _resolve_node_name(path: PathLike, name: str) -> str:
    """Resolve a short node name to a full branch name.

    When ``name`` is not already a full branch with a worktree, match it
    against the trailing segment of the caller's registered node branches
    (e.g. ``c1`` -> ``main.task.c1``). Returns the unique full branch, or
    ``name`` unchanged when nothing matches (so the caller still raises a
    clear "not found" error).

    Args:
        path: Caller's worktree directory.
        name: Short node name to resolve.

    Returns:
        The resolved full branch name, or ``name`` if no match.

    Raises:
        typer.BadParameter: If the short name matches more than one node.

    """
    caller = resolve_node(path)
    rows = caller.list(all_nodes=True)
    matches = []
    for row in rows:
        *_, leaf = row['node'].rsplit('.', 1)
        if leaf == name:
            matches.append(row['node'])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        options = ', '.join(sorted(matches))
        raise typer.BadParameter(
            f'Ambiguous node name {name!r} (matches: {options}). Use the full branch.'
        )
    return name
