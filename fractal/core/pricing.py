"""Functions for model pricing via the LiteLLM price table."""

from __future__ import annotations

import functools
import json
import os
import pathlib
import tempfile
import time
from typing import Any, Optional

import fractal.util

__all__ = []

_PRICING_URL = (
    'https://raw.githubusercontent.com/BerriAI/litellm'
    '/main/model_prices_and_context_window.json'
)
_PRICING_CACHE = '~/.fractal/pricing.json'
_FETCH_TIMEOUT_SECONDS = 10


def update(max_age: Optional[str] = None) -> str:
    """Refresh the cached LiteLLM pricing file.

    The file is fetched to a temp path and swapped in atomically, so an
    interrupted download never leaves a corrupt cache.

    Args:
        max_age: If given (e.g. ``24h``), skip the fetch when the cache
            is newer than this duration.

    Returns:
        ``'fresh'`` (cache new enough, no fetch), ``'fetched'``
        (downloaded), ``'stale'`` (fetch or cache write failed but a cache
        exists), or ``'missing'`` (fetch or cache write failed and no cache
        exists).

    """
    # resolve pricing.json path
    cache = pathlib.Path(_PRICING_CACHE).expanduser()
    # validate max_age up front (a malformed duration must fail regardless
    # of cache state), then skip the fetch when the cache is still fresh
    if max_age is not None:
        max_age_seconds = fractal.util.parse_duration_seconds(max_age)
        if max_age_seconds is None:
            raise ValueError(f'Invalid duration: {max_age!r}')
        if cache.exists() and time.time() - cache.stat().st_mtime < max_age_seconds:
            return 'fresh'
    # fetch to a per-process temp file, then swap in atomically; import urllib
    # locally so the http/ssl stack stays off every CLI cold-start -- only this
    # fetch needs it, and most commands (and the cache-fresh path above) never do
    import http.client
    import socket
    import urllib.request

    # an unwritable cache dir (read-only or foreign-owned home, full disk)
    # degrades like a failed fetch, so the loop's preflight abort names it
    # instead of a raw OSError escaping ahead of the run row and stranding
    # .status at idle
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=cache.parent,
            prefix=f'.{cache.name}-',
            suffix='.tmp',
        )
    except OSError:
        return 'stale' if cache.exists() else 'missing'
    os.close(fd)
    # urlretrieve has no timeout arg, so cap the connection via the default
    # socket timeout (restored after): a stalled fetch must not wedge the loop
    saved_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_FETCH_TIMEOUT_SECONDS)
    try:
        # fetch the price table (pinned https url; scheme is safe); a stall
        # raises socket.timeout (an OSError), but a malformed response --
        # truncated chunked body, garbage status line -- escapes urlretrieve
        # as a raw HTTPException, so the fallback must catch both classes
        urllib.request.urlretrieve(_PRICING_URL, tmp)  # noqa: S310
        os.replace(tmp, cache)
        # invalidate the process memo so rates() re-reads the fresh table --
        # without this the loop's per-iteration refresh is inert (a run that
        # first read a stale/empty table would price against it forever)
        _load.cache_clear()
    except (OSError, http.client.HTTPException):
        return 'stale' if cache.exists() else 'missing'
    finally:
        socket.setdefaulttimeout(saved_timeout)
        if os.path.exists(tmp):
            os.unlink(tmp)
    return 'fetched'


def has_model(model: str, /) -> bool:
    """Return whether a model is present and priced in the cache."""
    return rates(model) is not None


def rates(model: str, /) -> Optional[dict[str, Any]]:
    """Return a model's per-token rate entry, or ``None`` when unpriced.

    Returns:
        The LiteLLM rate entry for ``model``, or ``None`` when the model
        is absent from the cache or carries no rate keys.

    """
    entry = _load().get(model)
    if entry is None:
        return None
    # a model present without rate keys cannot be priced -- report unknown, not $0
    if ('input_cost_per_token' not in entry) and ('output_cost_per_token' not in entry):
        return None
    return entry


# ------ helper functions


@functools.cache
def _load() -> dict[str, Any]:
    """Load cached pricing data (once per process).

    The cache is populated by the loop's run-start pricing refresh, but only for
    token-priced agents (``needs_pricing``); claude's best-effort accrual reads
    it opportunistically, so a missing or corrupt cache degrades to no pricing
    (streams record unpriced, never crash mid-step).
    """
    cache = pathlib.Path(_PRICING_CACHE).expanduser()
    try:
        with open(cache, encoding='utf-8') as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
