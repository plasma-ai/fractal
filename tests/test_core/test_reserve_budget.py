"""Unit tests for ``--reserve-budget`` parsing (USD or percent of max_cost).

``parse_reserve_budget`` is the shared resolver that turns a
``--reserve-budget`` value into a USD amount: a bare number is taken
as-is, ``N%`` is a fraction of the ceiling. Node init calls it on the
merged config (flag > template preset > default) and ``node update`` on
the effective cap; both rely on its contract -- max_cost is required for
an explicit value, the value is non-negative, and a reserve >= 99% of
max_cost is refused.
"""

from __future__ import annotations

from typing import Optional

import pytest

from fractal.core.config import parse_reserve_budget

__all__ = [
    'test_parse_reserve_budget_resolves',
    'test_parse_reserve_budget_honors_custom_default',
    'test_parse_reserve_budget_rejects',
]


@pytest.mark.parametrize(
    argnames=('value', 'max_cost', 'expected'),
    argvalues=[
        (None, None, None),  # absent + no budget -> no reserve
        (None, 10.0, 1.0),  # absent -> 10% of max_cost (the default)
        ('2.5', 10.0, 2.5),  # bare number is USD
        ('20%', 10.0, 2.0),  # percent of max_cost
        ('0', 10.0, 0.0),  # zero is allowed (reserve mode at remaining <= 0)
        ('0%', 10.0, 0.0),
        # money materializes at display precision -- a bare 0.1 * 6.0 float
        # product would persist binary noise (0.6000000000000001) into
        # config.json and the retune echo
        (None, 6.0, 0.6),
        ('10%', 3.0, 0.3),
    ],
    ids=[
        'no_budget',
        'default_ten_percent',
        'bare_usd',
        'percent',
        'zero_usd',
        'zero_percent',
        'precise_default',
        'precise_percent',
    ],
)
def test_parse_reserve_budget_resolves(
    value: Optional[str],
    max_cost: Optional[float],
    expected: Optional[float],
) -> None:
    """A reserve resolves to USD: a number, ``N%``, or the 10% default of max_cost."""
    assert parse_reserve_budget(value, max_cost) == expected


def test_parse_reserve_budget_honors_custom_default() -> None:
    """The ``default`` argument sets the reserve used when ``value`` is ``None``."""
    assert parse_reserve_budget(None, 10.0, default='20%') == 2.0
    assert parse_reserve_budget(None, 10.0, default='1.5') == 1.5


@pytest.mark.parametrize(
    argnames=('value', 'max_cost', 'message'),
    argvalues=[
        ('2.5', None, '--max-cost'),  # requires max_cost (number form)
        ('20%', None, '--max-cost'),  # requires max_cost (percent form)
        ('99%', 10.0, '99%'),  # >= 99% of max_cost
        ('9.9', 10.0, '99%'),  # 9.9 == 0.99 * 10 (the bound is inclusive)
        ('-1', 10.0, '>= 0'),  # negative
        ('nope', 10.0, 'number'),  # non-numeric
    ],
    ids=[
        'no_max_cost_usd',
        'no_max_cost_percent',
        'reserve_at_99_percent',
        'inclusive_bound',
        'negative',
        'non_numeric',
    ],
)
def test_parse_reserve_budget_rejects(
    value: Optional[str],
    max_cost: Optional[float],
    message: str,
) -> None:
    """Invalid reserve budgets raise ``ValueError`` with a pointed message."""
    with pytest.raises(ValueError, match=message):
        parse_reserve_budget(value, max_cost)
