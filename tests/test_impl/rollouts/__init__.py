"""Captured codex session rollouts for the ``fractal`` test suite.

Each rollout is a trimmed copy of a real codex session log -- message bodies
cut to ``...``, the working directory and account fields scrubbed, every
record kind and usage key kept -- so the usage tests replay real records.

Each ``<name>.jsonl`` beside this file loads as the attribute ``name``: the
log's records in order, one ``dict`` per line.
"""

import json
import pathlib


def __getattr__(name: str) -> list[dict]:
    path = pathlib.Path(__file__).parent / f'{name}.jsonl'
    if path.exists():
        lines = path.read_text(encoding='utf-8').splitlines()
        return [json.loads(line) for line in lines]
    raise AttributeError(name)
