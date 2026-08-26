"""Test the ``fractal.cli.utils`` module.

The helpers are pinned where they surface: ``parse_reserve_budget`` in
``test_reserve_budget``, node resolution in ``test_signal_guards``, and
the ``command`` error wrapper behaviorally across the ``test_cli``
suites. ``StreamRenderer``'s piped-stream ordering lives here (its
per-provider event rendering is pinned in ``test_impl``), as does
``resolve_headless``'s flag > env > recorded-backend cascade.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from typing import Optional

import pytest
import typer

from fractal.cli.utils import resolve_headless
from fractal.core.node import Node
from tests._helpers import _git

__all__ = [
    'test_renderer_keeps_piped_output_ordered_with_stderr',
    'test_resolve_headless_resolves_flag_env_then_marker',
    'test_resolve_headless_refuses_an_unrecognized_export',
]

# the chat command's epilogue in miniature: a streamed reply, the closing
# summary, then the session id echoed to stderr
_CHAT_TAIL = """
import typer
from fractal.cli.utils import StreamRenderer
from fractal.core.agent import StreamEvent

render = StreamRenderer()
render(StreamEvent(kind='text', text='streamed reply'))
render(StreamEvent(kind='result', final=True, cost=0.01, turns=1, duration=1.0))
render.close()
typer.echo('session: abc', err=True)
"""


def test_renderer_keeps_piped_output_ordered_with_stderr() -> None:
    """Renderer writes flush through, so stderr echoes never overtake them.

    Piped stdout is block-buffered while stderr is write-through: an
    unflushed closing summary would sit in the buffer and let the driving
    command's ``session:`` echo land mid-reply in a merged capture (the
    operator's ``2>&1`` view). Every renderer write flushes, so the merged
    stream keeps the reply, then the summary, then the session line.
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(
        part for part in (str(root), env.get('PYTHONPATH', '')) if part
    )
    result = subprocess.run(
        [sys.executable, '-c', _CHAT_TAIL],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        timeout=60,
    )
    merged = result.stdout
    assert result.returncode == 0, merged
    # reply, then closing summary, then the session line -- in write order
    assert merged.index('streamed reply') < merged.index('— ')
    assert merged.index('— ') < merged.index('session: abc')
    # the session line starts on its own line, never mid-reply
    assert '\nsession: abc' in merged


# ------ resolve_headless: the launch-backend cascade


@pytest.fixture
def marker_node(tmp_path: pathlib.Path) -> Node:
    """A minimal node whose data dir holds at most the backend record."""
    _git(tmp_path, 'init', '-b', 'main')
    _git(tmp_path, 'config', 'user.email', 'utils@test.local')
    _git(tmp_path, 'config', 'user.name', 'utils')
    _git(tmp_path, 'commit', '--allow-empty', '-m', 'init')
    node = Node(tmp_path)
    node.node_dir.mkdir(parents=True)
    return node


@pytest.mark.parametrize(
    argnames=('flag', 'env', 'marker', 'expected'),
    argvalues=[
        (True, 'false', False, True),
        (False, 'true', True, False),
        (None, 'true', False, True),
        (None, 'false', True, False),
        (None, None, True, True),
        (None, None, False, False),
    ],
    ids=[
        'headless-flag-beats-env',
        'tmux-flag-beats-env',
        'env-selects-headless',
        'env-beats-marker',
        'marker-selects-headless',
        'no-record-defaults-tmux',
    ],
)
def test_resolve_headless_resolves_flag_env_then_marker(
    marker_node: Node,
    monkeypatch: pytest.MonkeyPatch,
    flag: Optional[bool],
    env: Optional[str],
    marker: bool,
    expected: bool,
) -> None:
    """The backend cascade: explicit flag, then seat env, then the node's record.

    An explicit ``--headless``/``--tmux`` wins outright; a seat-exported
    ``FRACTAL_HEADLESS`` beats the ``.headless`` marker (a delegated child
    start follows its parent's backend); and with neither set, the marker --
    the backend the node last launched with -- decides, defaulting a node
    that has never run headless to tmux.
    """
    if env is None:
        monkeypatch.delenv('FRACTAL_HEADLESS', raising=False)
    else:
        monkeypatch.setenv('FRACTAL_HEADLESS', env)
    if marker:
        marker_file = marker_node.node_dir / '.headless'
        marker_file.write_text('headless\n', encoding='utf-8')
    assert resolve_headless(flag, marker_node) is expected


def test_resolve_headless_refuses_an_unrecognized_export(
    marker_node: Node,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognized ``FRACTAL_HEADLESS`` refuses, naming the accepted values.

    The loop exports exactly ``true``/``false`` (read case-folded), so any
    other set value is a broken seat env: coercing it into a backend choice
    would launch the wrong runtime silently, and ignoring it would let the
    export lie. The refusal names the variable and both accepted values.
    """
    monkeypatch.setenv('FRACTAL_HEADLESS', 'yes')
    with pytest.raises(typer.BadParameter, match='FRACTAL_HEADLESS') as refusal:
        resolve_headless(None, marker_node)
    assert 'true or false' in str(refusal.value)
    # the loop's exports are matched case-folded, never refused
    monkeypatch.setenv('FRACTAL_HEADLESS', 'TRUE')
    assert resolve_headless(None, marker_node) is True
