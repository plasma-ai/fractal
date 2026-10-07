"""Test the ``fractal.core.template`` module.

The input contract: a spawn's inputs merge from the ``--values`` sheet and
the ``--set`` overrides, later sources winning, and the sheet reads flat or
in the provenance shape a node's own ``_template.toml`` records.
"""

from __future__ import annotations

import pathlib

import pytest

from fractal.core.template import collect_values

__all__ = [
    'test_collect_values_reads_flat_and_provenance_shaped_sheets',
    'test_collect_values_refuses_a_values_table_beside_inputs',
]


@pytest.mark.parametrize(
    argnames=('sheet', 'sets', 'expected'),
    argvalues=[
        # the flat shape: top-level keys are the inputs
        (
            'role = "scout"\nwork_timeout = "2h"\n',
            [],
            {'role': 'scout', 'work_timeout': '2h'},
        ),
        # the provenance shape: the [values] table holds the inputs
        (
            '[values]\nrole = "scout"\nwork_timeout = "2h"\n',
            [],
            {'role': 'scout', 'work_timeout': '2h'},
        ),
        # a node's own record seeds another: its keys beside the table are ignored
        (
            'path = "templates/scout"\ncommit = "0123abcd"\ninclude = ["NODE.md"]\n'
            '[values]\nrole = "scout"\n',
            [],
            {'role': 'scout'},
        ),
        # --set overrides an unwrapped input like a flat one
        (
            '[values]\nrole = "scout"\nwork_timeout = "2h"\n',
            ['work_timeout="30m"'],
            {'role': 'scout', 'work_timeout': '30m'},
        ),
        # a scalar values key is an ordinary input, not a wrapper
        ('values = 3\n', [], {'values': 3}),
    ],
    ids=['flat', 'wrapped', 'provenance', 'set_override', 'scalar_values'],
)
def test_collect_values_reads_flat_and_provenance_shaped_sheets(
    tmp_path: pathlib.Path,
    sheet: str,
    sets: list[str],
    expected: dict,
) -> None:
    """A ``--values`` sheet reads flat or in the shape ``_template.toml`` records."""
    path = tmp_path / 'values.toml'
    path.write_text(sheet, encoding='utf-8')
    assert collect_values(values=path, sets=sets, pin=None) == expected


def test_collect_values_refuses_a_values_table_beside_inputs(
    tmp_path: pathlib.Path,
) -> None:
    """A ``[values]`` table beside other top-level inputs refuses, naming them.

    The sheet reads two ways -- the table as the inputs, or every top-level
    key as one -- so it refuses rather than guess; the provenance keys are
    the one company the table keeps.
    """
    path = tmp_path / 'values.toml'
    path.write_text(
        'role = "scout"\nteam = "blue"\ncommit = "0123abcd"\n[values]\nrole = "lead"\n',
        encoding='utf-8',
    )
    with pytest.raises(ValueError, match='ambiguous') as refused:
        collect_values(values=path, sets=None, pin=None)
    assert 'top-level keys role, team' in str(refused.value)
    assert 'commit' not in str(refused.value).split('keys', 1)[1]
