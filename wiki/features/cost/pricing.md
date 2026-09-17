---
name: features/cost/pricing
desc: |
  How token usage becomes dollars: the cached LiteLLM price table, its
  refresh and staleness semantics, and the unpriced-model contract.
created: 2026-07-21T04:50:13Z
updated: 2026-07-21T04:50:13Z
---

# features/cost/pricing

[[_index|..]]

***

Token-priced agents (those that report usage rather than dollars) are priced
through the community LiteLLM price table, cached at `~/.fractal/pricing.json`
(module `fractal/core/pricing.py`). A model's entry supplies per-token input and
output rates; a model absent from the table -- or present without any rate keys
-- is **unpriced**, and its steps record `NULL` cost rather than `$0`
([[features/cost/measurement|measurement]]).

## Refresh semantics

The table refreshes by fetching to a temp file and swapping it in atomically, so
an interrupted download never leaves a corrupt cache. A refresh reports one of
four outcomes: `fresh` (cache newer than the requested max age, no fetch),
`fetched` (downloaded), `stale` (fetch or cache write failed but a cache
exists), or `missing` (fetch or cache write failed and no cache exists) -- an
unwritable cache directory degrades like a failed fetch, so the loop's preflight
abort names it rather than a raw error escaping ahead of the run row. The fetch
is bounded by a short timeout so a stalled network never wedges the loop.

The loop refreshes the table for token-priced agents at run start and again at
each iteration top (the latter bounded by a 24-hour max age, so a long-running
node re-prices against a live table); at both points `missing` is fatal (the
cost/cap pipeline cannot price usage without a table) while `stale` degrades to
a warning over the cached table ([[features/cost/budgets|budgets]]). A
successful fetch also invalidates the in-process memo so the run re-reads the
fresh table. Cost-reporting agents read the cache only opportunistically, so for
them a missing or corrupt cache degrades to no pricing -- streams record
unpriced, never crash mid-step.

## Model resolution

The default lookup is exact by model id; a backend whose provider ids miss the
table's naming can override resolution (for example retrying under a
provider-prefixed id). Whether an agent's spend is trackable at all follows from
this: cost-reporting agents always track, token-priced agents track only models
the table can price.
