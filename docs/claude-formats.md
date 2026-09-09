# Claude local adapters

The adapter reads complete JSONL records from Claude Code project histories and
Claude desktop agent/Cowork histories, including nested subagent files and
`audit.jsonl`. Source files are never modified. Normalized events and incremental
state retain token counts, timestamps, model labels, and identifiers only.

## Supported evidence

- `assistant.message.usage` in project histories (`sessionId`, `requestId`).
- The same assistant shape in desktop audits (`session_id`, `request_id`).
- `message_start`, cumulative `message_delta`, and `message_stop`, either directly
  or inside a `stream_event.event` envelope.
- Older nested `progress.data` envelopes whose type is `agent_progress`; only the
  documented structural message field is followed, never arbitrary tool content.
- Desktop `rate_limit_event.rate_limit_info.unifiedWindows` as separate quota
  snapshots. A utilization ratio of `0.23` becomes `23` percent. These snapshots
  show evidence captured when the local event was written, not a live account
  query. Account scope is explicitly unverified.

`entrypoint: local-agent` identifies desktop agent activity; `entrypoint: cli`
identifies Claude Code. Otherwise the configured source classification is used,
with a known-root path fallback. Unknown surfaces remain unknown. This adapter
does not claim coverage of ordinary Claude web/desktop chat conversations.

## Accounting and updates

Anthropic reports ordinary input, cache reads, and cache creation separately;
inclusive input is their sum. Cache-creation TTL details are subsets, and reported
thinking tokens are a subset of output. Missing counts stay null. A complete TTL
breakdown can supply a missing aggregate cache-creation count. See Anthropic's
[prompt caching documentation](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
and [streaming usage documentation](https://platform.claude.com/docs/en/build-with-claude/streaming).

Provider message IDs determine the event key independently of source path,
session, or surface. Multiple content-block records, copies, audit observations,
and final project responses update one event. A provider request ID or local row
UUID is a flagged fallback if the message ID is absent. An observation lacking
all three IDs is skipped with a source warning; it cannot be deduplicated safely.
Different fallback kinds cannot be automatically linked to an absent provider
message identity.

`native.usage_priority` is 30 for a completed message and 10 for a provisional
observation. A higher-priority observation wins even when a corrected count is
smaller. At equal priority, cumulative numeric counts use maxima. Null never
erases existing evidence. The store recomputes inclusive totals after merging
where enough evidence exists, and removes `provisional_usage` when final usage
wins. Ordinary repeated assistant rows require no unbounded per-file dedup map;
only active streaming metadata is stored in incremental state.

Result and system task aggregates are ignored because they overlap per-message
usage, including subagent work. Synthetic client/error messages are not provider
requests. Initial output counts in audits can remain provisional; those records
are retained with `provisional_usage` until better evidence exists.

## Validation on 2026-09-07

Read-only validation of the available local sources found:

| Source | JSONL files | Usage observations | Unique provider messages | Quota snapshots |
| --- | ---: | ---: | ---: | ---: |
| Claude Code | 18 | 3,792 | 1,796 | 0 |
| Claude desktop agent | 91 | 6,769 | 1,628 | 257 |

No malformed JSON lines or parser warnings were encountered in this snapshot.
Fourteen Code messages and thirteen desktop messages had only provisional usage
evidence. Desktop audit/project overlap is substantial: 1,591 provider message
IDs appeared in both source forms during initial schema inspection. These are
observations of the same requests, not additional usage. Counts naturally change
as the applications keep working.

Nineteen synthetic tests cover cache accounting, missing and invalid fields,
duplicate audit/project records, cumulative streaming updates, concurrent
subagents, safe nesting, quota conversion, and metadata-only persistence.

## Remote Cowork desktop cache — September 7 coverage repair

Claude desktop can run Cowork remotely without creating new local-agent JSONL.
Its IndexedDB stores large conversation snapshots in
`https_claude.ai_0.indexeddb.blob` and smaller values in the sibling `.leveldb`
directory. Both are separate default sources. The confirmed envelope has
`product: cowork`, `tree.kind: cowork_remote`, and `tree.events`; only assistant
provider-message records are extracted. Ordinary chat caches remain unsupported
as reported usage, rather than being guessed from text or diagnostic logs.

An optional Node helper decodes the observed Chromium/V8 container and Snappy
compression using built-in modules. LevelDB WAL and SSTable files are read
directly without opening, locking, repairing, or modifying Claude's database.
Only allowlisted counts, IDs, model labels, and request timestamps leave the
helper. Snapshot fetch times and file modification times are not usage times.
File content hashes detect same-size and middle-of-file changes, and usage and
checkpoints commit atomically. A cache that changes during decoding is retried.
Malformed or unsupported formats remain visible as source warnings.

Provider message IDs deduplicate cache versions, audit logs, and project logs.
Cached assistant usage remains provisional unless there is actual final-message
evidence. Result/task summaries are excluded because they overlap message usage
and may include turns or subagents absent from the snapshot. Their totals cannot
be safely added to observed requests or attributed to a particular message.

`tree.hasOlder` marks a partial conversation window; an absent flag is treated
conservatively. Current snapshots often contain only 100 events. The collector
retains observations across snapshots and available historical database values,
but cannot guarantee recovery of evicted or never-cached remote history. Flags
`cache_partial_history` and `provisional_usage` feed separate report counts and a
visible daily-report note. **Observed usage is not complete consumption.**

## Persistent result summaries and quota history

The result-aggregate exclusions described above now apply to **direct addition**
to message totals. Result records are retained in a separate versioned ledger,
and quota events from cached conversation windows are persisted as observations.
Explicit result-to-user UUID links can establish a main-turn window for
reconciliation. Verified, nonconflicting missing amounts appear as a separate
summary adjustment; broader `modelUsage` counters remain inspectable evidence.
See [the reconciliation rules](reconciliation.md) for scope, version selection,
late-arriving messages, and cases where no automatic addition is permitted.
