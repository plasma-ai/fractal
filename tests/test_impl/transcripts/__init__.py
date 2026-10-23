"""Captured claude launch transcripts for the ``fractal`` test suite.

Each launch is a trimmed copy of the transcripts a live claude launch
left, laid out as claude keeps them -- the session's transcript beside a
folder of its sub-agents' -- its session id written as ``@SESSION@`` for a
test to fill in:

- ``sub_agent_launch`` (claude 2.1.300): a launch with three sub-agents,
  its cost-state record last in the session's transcript; trimmed to the
  assistant lines, a run of a reply's lines with the same usage cut to its
  last, content blanked, and tool ids renumbered.

Each ``<name>.jsonl`` beside this file, with its ``<name>/`` folder of
sub-agent transcripts, loads as the attribute ``name``: the session
transcript's path, for a test to copy the launch onto disk.
"""

import pathlib


def __getattr__(name: str) -> pathlib.Path:
    path = pathlib.Path(__file__).parent / f'{name}.jsonl'
    if path.exists():
        return path
    raise AttributeError(name)
