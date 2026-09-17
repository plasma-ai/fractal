"""Test the ``fractal.core.pricing`` module.

The cache contract: ``update`` refreshes the pricing file atomically and
reports one of four freshness states; ``rates``/``has_model`` answer only
for models carrying rate keys; a missing or corrupt cache degrades to no
pricing (record unpriced, never crash mid-step).
"""

from __future__ import annotations

import http.client
import json
import os
import pathlib
import socket
import urllib.request
from collections.abc import Iterator
from typing import Optional

import pytest

from fractal.core import pricing

__all__ = [
    'test_update_skips_the_fetch_while_fresh',
    'test_update_fetches_and_swaps_atomically',
    'test_update_refreshes_the_in_process_rates',
    'test_update_caps_the_fetch_with_a_socket_timeout',
    'test_update_degrades_when_the_fetch_fails',
    'test_update_degrades_when_the_cache_dir_is_unwritable',
    'test_update_rejects_a_malformed_max_age',
    'test_rates_returns_priced_entries_only',
    'test_load_degrades_to_empty_on_missing_or_corrupt_cache',
]

_RATES = {
    'opus-4.8': {'input_cost_per_token': 3e-6, 'output_cost_per_token': 1.5e-5},
    'no-rates-model': {'max_tokens': 128_000},
}


@pytest.fixture
def cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> Iterator[pathlib.Path]:
    """Yield a temp pricing-cache path, resetting the process cache around it."""
    path = tmp_path / 'pricing.json'
    monkeypatch.setattr(pricing, '_PRICING_CACHE', str(path))
    pricing._load.cache_clear()
    yield path
    pricing._load.cache_clear()


def test_update_skips_the_fetch_while_fresh(
    cache: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache newer than ``max_age`` short-circuits without fetching."""
    cache.write_text(json.dumps(_RATES), encoding='utf-8')
    monkeypatch.setattr(urllib.request, 'urlretrieve', _unexpected_fetch)
    assert pricing.update(max_age='24h') == 'fresh'


def test_update_fetches_and_swaps_atomically(
    cache: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fetch lands the new table via the temp-then-replace swap."""

    def retrieve(url: str, filename: pathlib.Path) -> None:
        pathlib.Path(filename).write_text(json.dumps(_RATES), encoding='utf-8')

    monkeypatch.setattr(urllib.request, 'urlretrieve', retrieve)
    assert pricing.update() == 'fetched'
    # the fetched table landed and no temp file is left behind
    assert json.loads(cache.read_text(encoding='utf-8')) == _RATES
    assert list(cache.parent.glob(f'.{cache.name}-*')) == []


def test_update_refreshes_the_in_process_rates(
    cache: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful fetch is live in-process, so the loop's refresh isn't inert.

    ``_load`` memoizes, so without a cache-clear after the swap a run that
    first read an empty/stale table would price every step against it for
    the whole process -- the loop's per-iteration refresh would be a no-op.
    """
    # prime the process cache while nothing is priced -> rates() sees nothing
    assert pricing.rates('opus-4.8') is None

    def retrieve(url: str, filename: pathlib.Path) -> None:
        pathlib.Path(filename).write_text(json.dumps(_RATES), encoding='utf-8')

    monkeypatch.setattr(urllib.request, 'urlretrieve', retrieve)
    assert pricing.update() == 'fetched'
    # the fetched rates are visible without a manual cache_clear
    assert pricing.rates('opus-4.8') is not None


def test_update_caps_the_fetch_with_a_socket_timeout(
    cache: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fetch runs under a bounded socket timeout, restored afterward.

    ``urlretrieve`` takes no timeout argument, so a stalled connection would
    otherwise wedge the loop; the default socket timeout caps it.
    """
    seen: list[Optional[float]] = []

    def retrieve(url: str, filename: pathlib.Path) -> None:
        seen.append(socket.getdefaulttimeout())
        pathlib.Path(filename).write_text(json.dumps(_RATES), encoding='utf-8')

    monkeypatch.setattr(urllib.request, 'urlretrieve', retrieve)
    before = socket.getdefaulttimeout()
    assert pricing.update() == 'fetched'
    # the fetch saw the cap, and the default is restored afterward
    assert seen == [pricing._FETCH_TIMEOUT_SECONDS]
    assert socket.getdefaulttimeout() == before


@pytest.mark.parametrize(
    argnames='error',
    argvalues=[
        pytest.param(OSError('offline'), id='offline'),
        pytest.param(http.client.IncompleteRead(b''), id='incomplete_read'),
        pytest.param(http.client.BadStatusLine(''), id='bad_status_line'),
    ],
)
@pytest.mark.parametrize(
    argnames=('cached', 'expected'),
    argvalues=[
        pytest.param(True, 'stale', id='stale'),
        pytest.param(False, 'missing', id='missing'),
    ],
)
def test_update_degrades_when_the_fetch_fails(
    cache: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    cached: bool,
    expected: str,
    error: Exception,
) -> None:
    """A failed fetch reports ``stale`` with a cache, ``missing`` without.

    ``urlretrieve`` fails as ``OSError`` on transport errors, but a server
    closing mid-body or a garbage status line escapes it as a raw
    ``http.client.HTTPException`` -- every failure shape must degrade.
    """
    if cached:
        cache.write_text(json.dumps(_RATES), encoding='utf-8')

    def retrieve(url: str, filename: pathlib.Path) -> None:
        raise error

    monkeypatch.setattr(urllib.request, 'urlretrieve', retrieve)
    assert pricing.update() == expected
    # the aborted temp file never survives
    assert list(cache.parent.glob(f'.{cache.name}-*')) == []


@pytest.mark.skipif(os.geteuid() == 0, reason='root ignores directory modes')
@pytest.mark.parametrize(
    argnames=('cached', 'expected'),
    argvalues=[
        pytest.param(True, 'stale', id='stale'),
        pytest.param(False, 'missing', id='missing'),
    ],
)
def test_update_degrades_when_the_cache_dir_is_unwritable(
    cache: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    cached: bool,
    expected: str,
) -> None:
    """An unwritable cache dir degrades like a failed fetch, never raising.

    A read-only or foreign-owned home refuses the temp file before any fetch
    runs; the loop's preflight abort reads the same ``stale``/``missing``
    answer a failed fetch gives, so the refusal must report through it
    rather than escape as a raw ``OSError`` ahead of the run row.
    """
    if cached:
        cache.write_text(json.dumps(_RATES), encoding='utf-8')
    monkeypatch.setattr(urllib.request, 'urlretrieve', _unexpected_fetch)
    cache.parent.chmod(0o500)
    try:
        assert pricing.update() == expected
        # nothing was staged in the refused dir
        assert list(cache.parent.glob(f'.{cache.name}-*')) == []
    finally:
        cache.parent.chmod(0o700)


@pytest.mark.parametrize('cached', [True, False], ids=['cache', 'no_cache'])
def test_update_rejects_a_malformed_max_age(
    cache: pathlib.Path,
    cached: bool,
) -> None:
    """A ``max_age`` without a unit suffix raises regardless of cache state."""
    if cached:
        cache.write_text(json.dumps(_RATES), encoding='utf-8')
    with pytest.raises(ValueError, match='Invalid duration'):
        pricing.update(max_age='24')


def test_rates_returns_priced_entries_only(cache: pathlib.Path) -> None:
    """Only a model carrying rate keys prices; anything else reads unknown."""
    cache.write_text(json.dumps(_RATES), encoding='utf-8')
    assert pricing.rates('opus-4.8') == _RATES['opus-4.8']
    assert pricing.rates('no-rates-model') is None
    assert pricing.rates('ghost-model') is None
    assert pricing.has_model('opus-4.8')
    assert not pricing.has_model('no-rates-model')
    assert not pricing.has_model('ghost-model')


def test_load_degrades_to_empty_on_missing_or_corrupt_cache(
    cache: pathlib.Path,
) -> None:
    """A missing or corrupt cache reads as no pricing, never a crash."""
    # no cache file at all
    assert pricing.rates('opus-4.8') is None
    # a corrupt cache file
    pricing._load.cache_clear()
    cache.write_text('not json', encoding='utf-8')
    assert pricing.rates('opus-4.8') is None


# ------ helpers


def _unexpected_fetch(url: str, filename: pathlib.Path) -> None:
    """Fail the test when the fresh path fetches anyway."""
    raise AssertionError('unexpected pricing fetch')
