"""Test the ``fractal.util.tmux`` module.

The probe and the pane listing are exercised against a faked
``subprocess.run`` -- tmux is an external boundary, and the contract under
test is exactly how its absence and failure modes fold into the returned
collection: only an answer from tmux (names, rows, or the definitive
``no server running``) is conclusive. The socket test additionally observes
the argv handed to the boundary, pinning how a socket reaches tmux.
"""

from __future__ import annotations

import subprocess
from typing import Any, Optional

import pytest

import fractal.util.tmux

__all__ = [
    'test_probe_parses_live_session_names',
    'test_probe_distinguishes_definitive_empty_from_failure',
    'test_panes_slices_exact_session_from_listing',
    'test_panes_distinguishes_definitive_empty_from_failure',
    'test_panes_reports_missing_binary_as_inconclusive',
    'test_panes_pins_socket_into_argv',
    'test_sessions_folds_inconclusive_probe_into_empty',
]


def test_probe_parses_live_session_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """``probe`` returns the newline-separated names as a set."""

    def _list_sessions(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='repo (main)\nrepo (main-kid)\n',
            stderr='',
        )

    monkeypatch.setattr('fractal.util.tmux.subprocess.run', _list_sessions)
    result = fractal.util.tmux.probe()
    assert result == frozenset({'repo (main)', 'repo (main-kid)'})


@pytest.mark.parametrize(
    argnames=('returncode', 'stderr', 'expected'),
    argvalues=[
        # 'no server running' is tmux answering: definitively no sessions
        (1, 'no server running on /tmp/tmux-501/default', frozenset()),
        # a socket path that does not exist means no server ever started there
        (
            1,
            'error connecting to /tmp/tmux-501/default (No such file or directory)',
            frozenset(),
        ),
        # any other error leaves liveness unknown
        (1, 'error connecting to /tmp/tmux-501/default (Permission denied)', None),
    ],
)
def test_probe_distinguishes_definitive_empty_from_failure(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stderr: str,
    expected: Optional[frozenset[str]],
) -> None:
    """A non-zero exit is empty only for the ``no server running`` answer."""

    def _list_sessions(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=[],
            returncode=returncode,
            stdout='',
            stderr=stderr,
        )

    monkeypatch.setattr('fractal.util.tmux.subprocess.run', _list_sessions)
    assert fractal.util.tmux.probe() == expected


def test_panes_slices_exact_session_from_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``panes`` splits rows on the last tab and matches the name whole.

    A session name may carry spaces and parentheses (the repo-basename
    shape), so only a whole-name match after the last-tab split selects a
    row -- near-miss names sharing a prefix or suffix stay excluded, and
    the pids keep the listing's order.
    """

    def _list_panes(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                'repo (main)\t101\n'
                'repo (main)-kid\t202\n'
                'other repo (main)\t303\n'
                'repo (main)\t404\n'
            ),
            stderr='',
        )

    monkeypatch.setattr('fractal.util.tmux.subprocess.run', _list_panes)
    assert fractal.util.tmux.panes('repo (main)') == ('101', '404')


@pytest.mark.parametrize(
    argnames=('returncode', 'stdout', 'stderr', 'expected'),
    argvalues=[
        # 'no server running' is tmux answering: definitively no panes
        (1, '', 'no server running on /tmp/tmux-501/default', ()),
        # a socket path that does not exist means no server ever started there
        (
            1,
            '',
            'error connecting to /tmp/tmux-501/default (No such file or directory)',
            (),
        ),
        # a listing without the name is tmux answering: the session is gone
        (0, 'repo (other)\t101\n', '', ()),
        # any other error leaves the listing unknown
        (
            1,
            '',
            'error connecting to /tmp/tmux-501/default (Permission denied)',
            None,
        ),
    ],
)
def test_panes_distinguishes_definitive_empty_from_failure(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    stderr: str,
    expected: Optional[tuple[str, ...]],
) -> None:
    """``panes`` is empty only when tmux answered; other errors are ``None``."""

    def _list_panes(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=[],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    monkeypatch.setattr('fractal.util.tmux.subprocess.run', _list_panes)
    assert fractal.util.tmux.panes('repo (main)') == expected


def test_panes_reports_missing_binary_as_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host with no tmux binary is inconclusive; ``panes`` returns ``None``.

    ``subprocess.run`` raises ``FileNotFoundError`` (an ``OSError``) when the
    binary is absent -- before any result object -- so a returncode guard
    alone would let it escape.
    """

    def _no_tmux(*_args: Any, **_kwargs: Any) -> None:
        raise FileNotFoundError(2, 'No such file or directory', 'tmux')

    monkeypatch.setattr('fractal.util.tmux.subprocess.run', _no_tmux)
    assert fractal.util.tmux.panes('repo (main)') is None


@pytest.mark.parametrize(
    argnames=('socket', 'server'),
    argvalues=[
        # None resolves the ambient socket: no -S in the argv
        (None, []),
        # a socket pins the listing to that server via -S
        ('/run/tmux-501/fractal', ['-S', '/run/tmux-501/fractal']),
    ],
)
def test_panes_pins_socket_into_argv(
    monkeypatch: pytest.MonkeyPatch,
    socket: Optional[str],
    server: list[str],
) -> None:
    """``socket=None`` spawns without ``-S``; a socket pins ``['-S', socket]``."""

    captured: list[str] = []

    def _list_panes(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess:
        captured.extend(argv)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr='')

    monkeypatch.setattr('fractal.util.tmux.subprocess.run', _list_panes)
    fractal.util.tmux.panes('repo (main)', socket=socket)
    fields = '#{session_name}\t#{pane_pid}'
    assert captured == ['tmux', *server, 'list-panes', '-a', '-F', fields]


def test_sessions_folds_inconclusive_probe_into_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host with no tmux binary is inconclusive; ``sessions`` never crashes.

    ``subprocess.run`` raises ``FileNotFoundError`` (an ``OSError``) when the
    binary is absent -- before any result object -- so a returncode guard
    alone would let it escape. ``probe`` reports the ignorance as ``None``;
    ``sessions`` folds it into the empty set for display-only callers.
    """

    def _no_tmux(*_args: Any, **_kwargs: Any) -> None:
        raise FileNotFoundError(2, 'No such file or directory', 'tmux')

    monkeypatch.setattr('fractal.util.tmux.subprocess.run', _no_tmux)
    assert fractal.util.tmux.probe() is None
    assert fractal.util.tmux.sessions() == frozenset()
