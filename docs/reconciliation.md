# Saved summaries and reconciliation

The collector scans configured local sources about every 30 seconds, including
when the dashboard is closed. It saves new Claude result summaries and quota
observations alongside message usage. It does not request authenticated cloud
history or refresh the Claude account page.

## What is retained

- **Messages:** one usage record per provider message ID, merging repeated
  content blocks and later final usage.
- **Result summaries:** immutable versions of native main-turn usage, per-model
  totals, completion metadata, explicit user-message links, and source dates.
  Each version has first/last capture times and source-file provenance.
- **Quota history:** each distinct provider observation retains its timestamp,
  percentages, reset times, and native window names. Reading the same observation
  again updates its capture time without manufacturing another allowance event.

Conversation text, attachments, tool bodies, credentials, and raw application
databases are not copied into this ledger. Changed metadata is retained. An
unchanged cache uses its content fingerprint and updates only source scan time;
record capture times advance only when those records are actually decoded again.
Already saved evidence survives cache replacement or eviction, including a
previously verified boundary when the same result later loses its linked user
record from the cache. Contradictory boundary evidence still blocks allocation.

## How token reconciliation works

1. **Find the scope.** A result's main-turn `usage` is separate from its broader
   `modelUsage`. The latter can include earlier turns and subagents in an SDK
   call. Resumed calls can share a session ID, so that ID alone cannot establish
   a cumulative counter's boundaries.
2. **Prove a turn boundary.** The adapter links the result to an explicitly
   referenced user-message UUID and its timestamp. Process `time_origin_ms`,
   file modification times, and capture times are not turn boundaries.
3. **Match recorded requests.** Within the verified session/window, deduplicate
   provider message IDs and exclude subagent requests. Keep unknown counts
   distinct from zero. Mixed-model remainders are attributed to `unknown`
   rather than guessing a model split.
4. **Fill only the remainder.** Compare uncached input, output, cache writes,
   and cache reads separately. Add only summary minus matched-message usage,
   and only if every category is known and nonnegative. The adjustment is stored
   separately from the original message records and attributed to completion
   time; it does not reconstruct precisely when missing requests ran.
5. **Recompute as evidence arrives.** New/final messages shrink or eliminate
   the adjustment. Original messages, saved summaries, derived adjustments, and
   ingestion checkpoints commit consistently. An interrupted update rolls back
   instead of exposing both old adjustments and new messages.

For example, if messages account for 100 tokens and a compatible verified
summary reports 160, the adjustment is 60. If later messages account for 140,
the adjustment becomes 20, keeping combined usage at 160. All four categories
must satisfy this check independently; the implementation does not compare only
the grand total.

## Conflicts and limits

An unlinked boundary, overlapping turns, incomplete counts, conflicting source
versions, failed/nonterminal result, or a known category exceeding the summary
prevents an automatic addition. The original evidence stays available with a
reason in **Saved summaries & reconciliation**. Compatible copies can contribute
missing metadata; different known counts are not averaged or replaced by maxima.
Previously observed older versions cannot become authoritative merely because
an archive is reread later.

Desktop cache storage order does not prove which result revision is newer.
Different known main-turn counts across cached versions therefore remain a
conflict; they cannot replace one another based on scan order. Ordered local
JSONL results can supply a newly observed correction, including a lower count.

Broader model totals are displayed individually and **never summed across
successive results** or added to message totals without verified call scope.
Quota percentages remain a separate history and are never converted into token
counts. Reaching a subscription limit does not establish an exact token amount.

These distinctions follow [Anthropic's SDK usage scopes](https://code.claude.com/docs/en/agent-sdk/cost-tracking).
They explain why a cached result can report much more usage than its surviving
individual messages. Persistence improves future coverage, but cannot restore
remote events that never reached a local source.

## Inspecting saved evidence

The dashboard's month/provider/surface/model filters also select saved summaries.
Expand a summary to compare categories, view separate model totals, inspect
capture/source details, and read saved revisions. **Quota history** has its own
chronological list. It follows the provider filter across all dates, independent
of the report month, model, surface, and token-evidence selection.

Read-only JSON endpoints, all on localhost:

- `/api/reconciliation?month=2026-09&limit=25&offset=0`
- `/api/summary-revisions?summary_key=<summary-key>&limit=50&offset=0`
- `/api/quota-history?provider=anthropic&limit=25&offset=0`

The main `/api/report` includes the first summary and quota-history pages. SQLite
retains all captured versions; pagination only limits the response size. The CLI
report lists saved-summary reconciliation statuses. The source coverage panel
shows summary/quota counts separately from usage events.
