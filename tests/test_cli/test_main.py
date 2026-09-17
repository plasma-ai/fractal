"""Test the ``fractal.cli.main`` module.

Pure app assembly with no logic of its own; the command wiring is pinned
end-to-end by the ``test_cli`` integration suites, which drive every
sub-app through the real console script. Pinned here is the package entry
point (``python -m fractal``) the headless launch runs the app through.
"""

from __future__ import annotations

import subprocess
import sys

import fractal

__all__ = [
    'test_package_entry_point_runs_the_cli_without_warnings',
]


def test_package_entry_point_runs_the_cli_without_warnings() -> None:
    """``python -m fractal`` runs the CLI and writes nothing but its output.

    The headless launch pins the invoking interpreter through this entry
    point, and the loop's stderr lands in the node's ``headless.log`` -- so
    a Python warning raised on the way in (fatal under ``-W error``) would
    open every launch transcript with a spurious failure line.
    """
    result = subprocess.run(
        [sys.executable, '-W', 'error', '-m', 'fractal', '--version'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == fractal.__version__
    assert result.stderr == ''
