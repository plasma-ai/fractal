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
fresh table. Cost-reporting agents read the cache only for what their own figure
misses: Claude prices its streaming estimate and the settle of a launch with no
result figure (killed, capped, paused, or with the figure refused) from it, and
Grok prices a stream that reports no cost from its usage. A run with a native
Claude or a Grok step (an agent declaring `wants_pricing`) therefore refreshes
the table once at run start under the same 24-hour max age; there `missing` and
`stale` only warn, since every launch that reports its figure still books it.
For these agents a missing or corrupt cache degrades to no pricing -- streams
record unpriced, never crash mid-step.

An offline host sets `OFFLINE_MODE=true`, the switch the wiki tool honors too.
Every refresh then skips the fetch and reports as a failed one -- `stale` over a
cache, `missing` without -- after the max-age check, so a fresh cache still
reads `fresh`. The switch is read strictly: any value other than `true` or
`false` raises, and the loop's boot checks it with its run parameters so a
malformed value aborts on record instead of escaping the refresh ahead of the
run row. The test suite runs under it: every subprocess inherits it, and the
session replaces the fetch with a refusal that fails the running test by name.

Claude's cache writes bill by their lifetime. A usage record's `cache_creation`
split names the one-hour writes (`ephemeral_1h_input_tokens`), which price at
the table's `cache_creation_input_token_cost_above_1hr`; the five-minute writes
and any the split leaves unnamed price at the base write rate, as does every
write for a model with no one-hour rate. Claude Code writes one-hour entries, so
the base rate alone would underprice them by about 37.5%. This reaches fractal's
own Claude figures (the streaming estimate, the killed-step settle, the gateway
path), never a direct step's result-frame cost; the gateway's `modelUsage` table
carries no split, so its cache writes price at the base rate.

## Model resolution

The default lookup is exact by model id; a backend whose provider ids miss the
table's naming can override resolution (for example retrying under a
provider-prefixed id). Whether an agent's spend is trackable at all follows from
this: cost-reporting agents always track, token-priced agents track only models
the table can price.
