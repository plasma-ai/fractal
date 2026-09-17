"""Run the ``fractal`` CLI as ``python -m fractal``.

The headless launch runs the loop through this entry point, pinning the
invoking interpreter; the console script runs the same ``cli``.
"""

from __future__ import annotations

from fractal.cli.main import cli

if __name__ == '__main__':
    cli()
