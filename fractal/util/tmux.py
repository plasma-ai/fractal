"""Functions for tmux session probing."""

from __future__ import annotations

import subprocess
from typing import Optional

__all__ = []


def probe(*, socket: Optional[str] = None) -> Optional[frozenset[str]]:
    """Return the set of live tmux session names, or ``None`` when unknown.

    The batched form of a per-session existence probe -- a caller checking
    many sessions (e.g. reconciling a whole subtree) probes once instead of
    per name. Empty only when tmux definitively answered "no sessions": a
    zero exit, the ``no server running`` refusal, or a connect against a
    socket path that does not exist (no server ever started on this
    socket). Any other failure -- the binary absent (``OSError``, raised
    before any result) or ``list-sessions`` erroring for another reason
    (socket permissions, an unexpected refusal) -- is inconclusive and
    returns ``None``, so a caller about to act destructively on "no
    sessions" can refuse to act on ignorance.

    Every answer is evidence about one server only: the ambient socket's
    "no sessions" says nothing about sessions alive on another socket, so
    a caller judging a specific session passes the socket it lives on.

    Args:
        socket: Server socket path to probe (``tmux -S``); ``None`` probes
            the ambient socket.

    Returns:
        The live tmux session names, or ``None`` when tmux gave no answer.

    """
    # -S pins the probe to one server; without it tmux resolves the
    # ambient socket ($TMUX, else $TMUX_TMPDIR)
    server = [] if socket is None else ['-S', socket]
    try:
        result = subprocess.run(
            ['tmux', *server, 'list-sessions', '-F', '#{session_name}'],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        # tmux's two "no server on this socket" answers -- its own verdict,
        # and a connect against a socket path that does not exist (a host
        # where no server ever started) -- are definitive empties; any other
        # error (permissions, an unexpected refusal) leaves liveness unknown
        if result.stderr.startswith('no server running'):
            return frozenset()
        if result.stderr.startswith('error connecting to'):
            if 'No such file or directory' in result.stderr:
                return frozenset()
        return None
    return frozenset(result.stdout.splitlines())


def panes(session: str, *, socket: Optional[str] = None) -> Optional[tuple[str, ...]]:
    """Return one live tmux session's pane pids, or ``None`` when unknown.

    The exact-name slice of one ``list-panes -a``: a session name may carry
    spaces and parentheses, so a pane row splits on its last tab and the
    name matches whole (``kill.sh`` runs the identical lookup). Empty when tmux definitively answered -- a listing
    without the name, or the "no server on this socket" verdicts
    :func:`probe` reads as empty. Any other failure (the binary absent,
    ``list-panes`` erroring for another reason) is inconclusive and returns
    ``None``. As with :func:`probe`, every answer is evidence about one
    server only.

    Args:
        session: Exact session name whose panes to list.
        socket: Server socket path to ask (``tmux -S``); ``None`` asks the
            ambient socket.

    Returns:
        The session's pane pids, or ``None`` when tmux gave no answer.

    """
    # -S pins the listing to one server; without it tmux resolves the
    # ambient socket ($TMUX, else $TMUX_TMPDIR)
    server = [] if socket is None else ['-S', socket]
    # one row per pane: the owning session's name, then the pane pid
    fields = '#{session_name}\t#{pane_pid}'
    try:
        result = subprocess.run(
            ['tmux', *server, 'list-panes', '-a', '-F', fields],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        # tmux's two "no server on this socket" answers are definitive
        # empties; any other error leaves the listing unknown
        if result.stderr.startswith('no server running'):
            return ()
        if result.stderr.startswith('error connecting to'):
            if 'No such file or directory' in result.stderr:
                return ()
        return None
    # split each row on its last tab, so tabs never hide in a name
    pids = []
    for line in result.stdout.splitlines():
        name, _, pid = line.rpartition('\t')
        if name == session:
            pids.append(pid)
    return tuple(pids)


def sessions() -> frozenset[str]:
    """Return the set of live tmux session names (one ``list-sessions``).

    :func:`probe` with the inconclusive answer folded into the empty set --
    for display-only callers where "unknown" and "none visible" render the
    same and a failed probe must never crash the read.

    Returns:
        The live tmux session names (empty when the probe failed).

    """
    live = probe()
    if live is None:
        return frozenset()
    return live
