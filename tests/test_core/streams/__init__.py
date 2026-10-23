"""Captured claude streams for the ``fractal`` test suite.

Each stream is a trimmed copy of a live claude ``stream-json`` capture, its
session id written as ``@SESSION@`` for a test to fill in:

- ``claude_background_wait`` (claude 2.1.300): the agent backgrounds a
  command and ends its turn, its result on line 18; claude waits, then
  wakes it on the task's notice for a new reply (from line 23) closed by
  its own result on line 33.

Each ``<name>.jsonl`` beside this file loads as the attribute ``name``: the
stream's lines in order, each ending in its newline, as claude printed them.
"""

import pathlib


def __getattr__(name: str) -> list[str]:
    path = pathlib.Path(__file__).parent / f'{name}.jsonl'
    if path.exists():
        return path.read_text(encoding='utf-8').splitlines(keepends=True)
    raise AttributeError(name)
