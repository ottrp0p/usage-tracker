# Codex local telemetry and reconciliation

This collector supports the internal session JSONL shapes identified during the
local inspection recorded in the implementation proposal: `session_meta`,
`turn_context`, and `event_msg` records whose payload type is `token_count`.
Session metadata supplies IDs, fork ancestry, creation time, and desktop/CLI/IDE/
subagent attribution. Turn context supplies an explicit model and turn ID.
Token-count payloads supply `info.total_token_usage`, `info.last_token_usage`,
and optional `rate_limits`. Unrecognized record types are ignored; missing
cumulative usage is reported as a coverage warning.

[OpenAI's App Server documentation](https://learn.chatgpt.com/docs/app-server)
documents thread token-usage notifications and separate account rate-limit and
token-activity APIs. It is a primary reference for those concepts, not a stable
specification of local JSONL files. This collector's on-disk schema support is
based on local shape inspection and synthetic regression fixtures. Future
versions may require adapter updates. Authenticated account polling is separate
from this local log collector.

## Metadata-only evidence

Only allowlisted nonnegative integer counts are retained: input, cached input,
cache-write input, output, reasoning output, and total tokens. Cached input is a
subset of input; reasoning output is a subset of output. Subsets are never added
again to total usage. Missing categories remain unknown rather than zero.

Raw observations also retain IDs, explicit model/surface labels, original
observation time, fork ancestry, and any copied-parent counter baseline.
“Raw” here means the original count observation, not the complete source record.
Message bodies, tool arguments/results, prompts, and conversation titles are not
retained. Quota snapshots stay in a separate table and never add to token totals.

## Reconciliation policy

Every cumulative snapshot is retained, including unchanged quota refreshes.
Normalized session usage is rebuilt from the union of all observed files. Proven
replayed history is removed from derivation before the remaining observations
are sorted and differenced. This prevents a partial file's initial cumulative
total from overwriting an overlapping increment in a complete file. Exact
rereads use stable observation keys. Adding earlier history may revise both
counts and date attribution in the derived report.

Some desktop rewrites change historical timestamps, so timestamp-based keys
alone cannot establish unique work. Two additional checks apply:

- **Inherited history:** a child's complete cumulative and last-request counter
  vectors are compared with its declared ancestors' observations strictly before
  child creation. A matching known ancestor turn ID supplies scope; conflicting
  known turn IDs preserve the child's request. When turn IDs are unavailable,
  at least two distinct complete signatures must corroborate the copied history.
  A lone unscoped match can be a fresh child counter restart and is retained.
  Proven copies remain in raw evidence but contribute no child usage. Matching
  only a grand total, model name, or nearby time is insufficient.
- **Rewritten own history:** the adapter marks records stamped exactly at their
  turn-context timestamp as candidates. A candidate is excluded only when the
  same session and known turn ID have exact full-counter matches at independent
  timestamps. At least two distinct signatures corroborated across two distinct
  timestamps are required. The independently timed observations remain; neither
  a lone candidate nor an incomplete or unmatched vector is discarded.

Parent observations undergo the own-history rewrite check before they can supply
ancestral timing evidence or fork baselines. This prevents a flattened copy of a
parent's future counters from appearing to predate child creation. Candidates
without corroborating independently timed observations remain retained; missing
source history can therefore limit reconstruction.

These checks preserve all raw observations and source provenance. They avoid
turning a flattened historical sequence followed by its genuine timed records
into a false counter reset and a second copy of the same usage. Independent
sessions and genuine reset sequences are not globally deduplicated by count.

- A first snapshot equal to its known last-request total is attributed to its
  observation time. Otherwise the initial cumulative aggregate has an unknown
  date and model, since its earlier requests are not individually observed.
- Later distinct snapshots produce counter differences. An unchanged total adds
  no usage. A decreasing total creates an explicit `cumulative_counter_reset`.
- Each later difference is attributed to its **ending observation time**. When
  its total differs from known last-request usage, it carries
  `cumulative_interval_contains_multiple_requests`. Missing intermediate records
  can span requests, models, or local days; exact allocation within that interval
  cannot be recovered. The flag identifies this limitation, including inconsistent
  observations whose last-request total is larger than the derived difference.
- Distinct snapshots sharing a millisecond are ordered by increasing cumulative
  total. A reset within that same millisecond is ambiguous and cannot be uniquely
  reconstructed from these timestamps.
- If a session has only undated observations, one largest cumulative aggregate
  is retained with `undated_cumulative_order_unknown`. Reset ordering is unknown.
  When dated observations also exist, undated snapshots stay in raw evidence but
  are excluded from additive totals because their reset epoch cannot be placed.
  Unmatched undated evidence produces `undated_snapshots_not_attributed` flags.

## Forks and inherited counters

Copied parent history is attributed to the parent. The latest parent snapshot
strictly before child creation, or the copied-parent baseline retained by the
adapter, supplies inherited-counter evidence. A parent's later activity cannot
change the child's original baseline.

An unchanged inherited snapshot adds no child usage. A fresh child counter equal
to its last-request total counts as its own request, even if it exceeds the
parent's counter. Otherwise growth above a validated inherited baseline counts
only the difference. A child whose first snapshot cannot be validated against a
baseline retains only its known last request, with a limitation flag; missing
last-request evidence remains an unknown event. A fresh request coincidentally
equal to the inherited total is indistinguishable from an unchanged quota refresh
and is conservatively treated as unchanged. Later parent backfills also trigger
child reconciliation.

## Transactions and scope

File offsets, raw observations, and the reconciliation-dirty marker commit
together. Derived rows and their source relationships are then refreshed in one
transaction. A crash before reconciliation leaves the dirty marker for retry.
Reports can show the prior completed totals while collection is in progress.

These are mutable local telemetry counters, not account billing records. Source
retention, incomplete files, missing observations, and future format changes can
limit coverage. Cumulative resets and later source backfills can revise the
derived history. This service does not infer subscription charges or complete
provider billing from those counts.
