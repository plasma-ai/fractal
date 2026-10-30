"""The loop's grep-anchored stdout contract.

A handful of ``fractal/core/loop.py`` stdout lines are load-bearing
beyond logging: the e2e harness (``test_run_modes``) and operators
watching a pane grep them to observe the loop's state from outside.
Renaming one silently breaks every consumer, so this module is the
single inventory of lines any implementation of the loop must emit
verbatim, each mapped to its consumers.

The division of labor: the e2e suite pins that each line is actually
*emitted* on its trigger (drains, budgets, parks, adoption); this module
pins the *inventory* -- the anchors exist verbatim in the loop source,
so an edit that would strand the consumers fails here by name. The
anchors are deliberately restated as literals rather than imported from
their consumers: the contract must not move when a consumer's constant
does.

Only loop-produced lines belong here: confirmations printed by
``fractal`` CLI commands (pause/resume summaries, ``pause.sh``'s abort
notice) are pinned by their own command and e2e tests, and mode-doc
markers by the prompt tests.
"""

from __future__ import annotations

import pytest

from .conftest import _worktree_root

__all__ = ['test_loop_emits_every_grep_anchor']

_LOOP = _worktree_root() / 'fractal' / 'core' / 'loop.py'

# every grep-anchored stdout line the loop must emit verbatim, mapped to
# the consumers that grep it -- extend this table when a new consumer
# starts grepping a new line
_ANCHORS = {
    # drain choreography, observed mid-run through the teed loop log
    'waiting for child nodes to finish': (
        'test_run_modes._WAIT_BANNER (_await_log mid-run)'
    ),
    'Waiting for children: timed out': 'test_run_modes._WAIT_TIMEOUT_BANNER',
    'all child nodes finished': 'test_run_modes._DRAINED_BANNER',
    # budget terminals
    'Subtree cost budget reached': 'test_run_modes cost-ceiling assertions',
    'Total cost budget reserve reached': 'test_run_modes reserve assertions',
    'winding down for one iteration in reserve mode': (
        'test_run_modes reserve assertions'
    ),
    'skipped (over budget)': 'test_run_modes wind-down skip assertions',
    # pause/resume choreography
    'Resuming run': 'test_run_modes adoption assertions; operators tailing a pane',
    'Parked at boot': 'test_run_modes tree-latch assertions',
    # iteration-cap terminal
    'Reached max iterations': 'test_run_modes max-iters assertions',
    # the boot banner's un-configured wait label: one contract with the 1m
    # fallback in Loop._read_run_parameters, the init --wait help, and the
    # init.sh usage
    "wait_label = self._wait if self._wait else '1m'": (
        'fractal node init --wait help; _scripts/init.sh usage'
    ),
}


@pytest.mark.parametrize('anchor', _ANCHORS)
def test_loop_emits_every_grep_anchor(anchor: str) -> None:
    """Each anchored line appears verbatim in the loop source."""
    source = _LOOP.read_text(encoding='utf-8')
    assert anchor in source, f'consumed by: {_ANCHORS[anchor]}'
