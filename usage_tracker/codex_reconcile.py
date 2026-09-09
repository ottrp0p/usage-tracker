"""Derive Codex usage from the union of raw, metadata-only observations.

File-local deltas cannot be added safely: an archive or a partial transcript may
begin at a cumulative snapshot already covered by another file. Keep raw
observations separately, then rebuild the affected session's intervals in one
transaction. A later backfill can replace an undated initial aggregate with its
actual dated increments without changing the overall total.
"""

from __future__ import annotations

from dataclasses import fields
import json

from usage_tracker.collectors.codex import FIELDS
from usage_tracker.models import UsageEvent, count, utc_timestamp


def _counts(value: object) -> dict:
    value = value if isinstance(value, dict) else {}
    result = {key: count(value.get(key)) for key in FIELDS}
    if result["total_tokens"] is None and result["input_tokens"] is not None and result["output_tokens"] is not None:
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def _known(value: object) -> bool:
    return value not in (None, "", "unknown", {}, [])


def merge_observation_payload(previous: dict, incoming: dict) -> dict:
    """Keep direct, richer metadata when the same raw snapshot is seen again.

    An initial fragment may have no turn context and may expose a cumulative
    fallback count rather than the original delta. Neither should overwrite
    useful identity metadata; reconciliation never trusts those fallback counts.
    Stable ordering makes conflicts deterministic regardless of file read order.
    """
    def rank(payload):
        native = payload.get("native", {})
        richness = sum(_known(payload.get(key)) for key in ("model", "surface", "request_id"))
        richness += sum(value is not None for value in _counts(native.get("last_usage")).values())
        return (not native.get("copied_history", False), richness, json.dumps(payload, sort_keys=True))

    preferred, other = sorted((previous, incoming), key=rank, reverse=True)
    # Inputs are dataclass dictionaries from the adapter, not arbitrary exports.
    # Restrict the top level to the UsageEvent contract in case schema expands.
    allowed = {field.name for field in fields(UsageEvent)}
    merged = {key: value for key, value in preferred.items() if key in allowed}
    for key in ("model", "surface", "request_id", "timestamp"):
        if not _known(merged.get(key)) and _known(other.get(key)):
            merged[key] = other[key]
    native = dict(preferred.get("native", {}))
    for key in ("observed_model", "owner_created", "forked_from_id", "inherited_baseline_at", "turn_context_at"):
        if not _known(native.get(key)) and _known(other.get("native", {}).get(key)):
            native[key] = other["native"][key]
    # A newer parser can recover replay provenance from an existing raw key.
    # Preserve this enrichment even when the older observation wins the label
    # ranking. Positive markers require the exact context-time relationship.
    sources = (preferred.get("native", {}), other.get("native", {}))
    marked = [item for item in sources if item.get("context_timestamp_replay_candidate") is True
              and utc_timestamp(item.get("turn_context_at")) is not None
              and utc_timestamp(item.get("turn_context_at")) == utc_timestamp(item.get("observed_at"))]
    if marked:
        native["turn_context_at"] = marked[0]["turn_context_at"]
        native["context_timestamp_replay_candidate"] = True
    elif "context_timestamp_replay_candidate" not in native:
        for item in sources:
            if isinstance(item.get("context_timestamp_replay_candidate"), bool):
                native["context_timestamp_replay_candidate"] = item["context_timestamp_replay_candidate"]
                break
    for key in ("cumulative_usage", "last_usage", "inherited_baseline"):
        if key in native or key in other.get("native", {}):
            left, right = _counts(native.get(key)), _counts(other.get("native", {}).get(key))
            native[key] = {field: left[field] if left[field] is not None else right[field] for field in FIELDS}
    merged["native"] = native
    return merged


def _observations(store) -> dict[str, list[dict]]:
    sessions: dict[str, list[dict]] = {}
    for row in store.conn.execute("SELECT payload FROM codex_observations"):
        payload = json.loads(row[0])
        native = payload.get("native", {})
        if not isinstance(native.get("cumulative_usage"), dict):
            continue
        payload["_counts"] = _counts(native["cumulative_usage"])
        payload["_observed_at"] = utc_timestamp(native.get("observed_at"))
        sessions.setdefault(payload["session_id"], []).append(payload)
    return sessions


def affected_sessions(store, sessions: set[str]) -> set[str]:
    """Parent backfills can change child baselines; include every descendant."""
    observed = _observations(store)
    result = set(sessions)
    while True:
        descendants = {session for session, rows in observed.items()
                       if any(row["native"].get("forked_from_id") in result for row in rows)}
        expanded = result | descendants
        if expanded == result:
            return result
        result = expanded


def _sort_key(row: dict):
    # Multiple counters in one millisecond can be emitted without finer time
    # resolution. Ordering those ascending gives a single monotone interval;
    # a reset within that same millisecond cannot be uniquely reconstructed.
    return (row["_observed_at"] or "", row["_counts"]["total_tokens"] or 0, row["event_key"])


def _same_counter(left: dict, right: dict) -> bool:
    if left["total_tokens"] is not None and right["total_tokens"] is not None:
        return left["total_tokens"] == right["total_tokens"]
    return left == right


def _difference(current: dict, previous: dict) -> tuple[dict, list[str]]:
    flags = []
    if (current["total_tokens"] is not None and previous["total_tokens"] is not None
            and current["total_tokens"] < previous["total_tokens"]):
        return dict(current), ["cumulative_counter_reset"]
    difference = {}
    for key, value in current.items():
        before = previous[key]
        difference[key] = value - before if value is not None and before is not None and value >= before else None
        if value is not None and before is not None and value < before:
            flags.append(f"nonmonotonic_{key}")
    return _counts(difference), flags


def _initial_is_one_request(current: dict, last: dict) -> bool:
    if current["total_tokens"] is not None and last["total_tokens"] is not None:
        return current["total_tokens"] == last["total_tokens"]
    return current == last and any(value is not None for value in current.values())


def _complete_usage_signature(row: dict) -> tuple[int, ...] | None:
    """Match exported history by count evidence, never by rewritten timestamps.

    Codex can re-emit copied history with fresh wrapper timestamps at fork or
    compaction. Require every cumulative AND last-request component, including
    cache and reasoning subsets, to be known. Comparing only the total could
    accidentally identify independent requests as the same history.
    """
    cumulative = row["_counts"]
    last = _counts(row["native"].get("last_usage"))
    values = tuple(cumulative[key] for key in FIELDS) + tuple(last[key] for key in FIELDS)
    return values if all(value is not None for value in values) and any(values) else None


def _exclude_ancestral_copies(rows: list[dict], observed: dict[str, list[dict]]) -> tuple[list[dict], int]:
    """Exclude proven ancestral history even when a writer assigned new times.

    A match must exist in a declared ancestor before this child's creation. No
    rate, burst-size, model, or temporal-proximity threshold is used. Unknown
    ancestry, dates, or incomplete counter vectors do not establish a match.
    Raw observations and their file provenance remain unchanged in SQLite.
    """
    if not rows:
        return rows, 0
    creation = min((date for row in rows if (date := utc_timestamp(row["native"].get("owner_created")))), default=None)
    if creation is None:
        return rows, 0
    pending = {row["native"].get("forked_from_id") for row in rows if row["native"].get("forked_from_id")}
    seen = {rows[0]["session_id"]}
    signatures: dict[tuple, set[str | None]] = {}
    while pending:
        ancestor = pending.pop()
        if ancestor in seen:
            continue
        seen.add(ancestor)
        for candidate in observed.get(ancestor, []):
            parent = candidate["native"].get("forked_from_id")
            if parent and parent not in seen:
                pending.add(parent)
            when = candidate["_observed_at"]
            if when is not None and when < creation:
                signature = _complete_usage_signature(candidate)
                if signature is not None:
                    request = candidate.get("request_id")
                    request = request if isinstance(request, str) and request and request != "unknown" else None
                    signatures.setdefault(signature, set()).add(request)
    if not signatures:
        return rows, 0
    excluded = set()
    unscoped_matches = []
    for row in rows:
        signature = _complete_usage_signature(row)
        if signature not in signatures:
            continue
        request = row.get("request_id")
        request = request if isinstance(request, str) and request and request != "unknown" else None
        ancestor_requests = signatures[signature]
        if request is not None and request in ancestor_requests:
            excluded.add(row["event_key"])
        elif request is not None and any(value is not None for value in ancestor_requests):
            # A genuine fresh child can coincidentally produce the same count
            # vector as an ancestor's early request. A different known turn ID
            # is positive evidence against identifying that child as a copy.
            continue
        else:
            unscoped_matches.append((row["event_key"], signature))
    # Compacted inherited prefixes can have no turn context at all. Require a
    # sequence of distinct complete matches in that case; never discard a lone
    # unscoped cumulative==last counter just because an ancestor once used it.
    if len({signature for _, signature in unscoped_matches}) >= 2:
        excluded.update(key for key, _ in unscoped_matches)
    kept = [row for row in rows if row["event_key"] not in excluded]
    return kept, len(rows) - len(kept)


def _exclude_context_replays(rows: list[dict]) -> tuple[list[dict], int]:
    """Prefer independently timed evidence over proven flattened own-history.

    A rewritten rollout can stamp an entire turn's copied counters with that
    turn context's timestamp, including counters originally produced later.
    Merely retaining the earliest duplicate would therefore invent work at turn
    start and create a false reset when the actual first request appears.

    Removal requires explicit adapter provenance AND two independent signature
    matches spread across two other timestamps. We match request IDs as well as
    all count components. A lone same-time event, incomplete vector, different
    turn, or uncorroborated candidate remains available for reconciliation.
    """
    independent: dict[tuple, set[str]] = {}
    candidates: dict[tuple, list[tuple[dict, tuple]]] = {}
    for row in rows:
        signature = _complete_usage_signature(row)
        when = row["_observed_at"]
        if signature is None or when is None:
            continue
        native = row["native"]
        request = row.get("request_id")
        if not isinstance(request, str) or not request or request == "unknown":
            continue  # Missing IDs cannot establish a shared request/turn.
        marked = (native.get("context_timestamp_replay_candidate") is True
                  and utc_timestamp(native.get("turn_context_at")) == when)
        if marked:
            candidates.setdefault((when, request), []).append((row, signature))
        else:
            # Older raw observations may predate the provenance fields. Their
            # distinct, spread timestamps are retained independent evidence;
            # only a positively identified rewrite candidate can be removed.
            independent.setdefault((signature, request), set()).add(when)

    excluded = set()
    for (when, request), bucket in candidates.items():
        matches = [(row, signature, independent.get((signature, request), set()) - {when}) for row, signature in bucket]
        matches = [(row, signature, times) for row, signature, times in matches if times]
        if len({signature for _, signature, _ in matches}) < 2:
            continue
        if len(set().union(*(times for _, _, times in matches))) < 2:
            continue
        excluded.update(row["event_key"] for row, _, _ in matches)
    kept = [row for row in rows if row["event_key"] not in excluded]
    return kept, len(rows) - len(kept)


def _fork_baseline(rows: list[dict], observed: dict[str, list[dict]]) -> tuple[str | None, dict | None]:
    # Metadata may first appear in a later copied observation, so inspect the
    # whole session rather than assuming the first file was the complete one.
    parents = sorted({row["native"].get("forked_from_id") for row in rows if row["native"].get("forked_from_id")})
    if not parents:
        return None, None
    parent = parents[0]
    creation_dates = [utc_timestamp(row["native"].get("owner_created")) for row in rows]
    creation = min((date for date in creation_dates if date), default=None)
    candidates = []
    if creation:
        for row in observed.get(parent, []):
            if row["_observed_at"] and row["_observed_at"] < creation:
                candidates.append((row["_observed_at"], row["event_key"], row["_counts"]))
    for row in rows:
        native = row["native"]
        inherited = native.get("inherited_baseline")
        when = utc_timestamp(native.get("inherited_baseline_at"))
        if isinstance(inherited, dict) and (not creation or not when or when < creation):
            candidates.append((when or "", row["event_key"], _counts(inherited)))
    # A timestamped parent/copy snapshot outranks an un-timed fallback. Never
    # choose the largest count: a parent may reset before the fork is created.
    baseline = max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None
    return parent, baseline


def _make_event(row: dict, delta: dict, flags: list[str], timestamp: str | None) -> UsageEvent:
    native = row["native"]
    retained = {key: native[key] for key in ("cumulative_usage", "last_usage", "observed_at", "observed_model",
                "owner_created", "forked_from_id", "inherited_baseline", "inherited_baseline_at", "copied_history",
                "turn_context_at", "context_timestamp_replay_candidate") if key in native}
    retained.update({key: value for key, value in delta.items() if value is not None})
    retained["counter_basis"] = "reconciled_cumulative_delta"
    if timestamp is None:
        flags.append("timestamp_unknown")
    model = native.get("observed_model") or row.get("model", "unknown")
    if "initial_cumulative_timestamp_unknown" in flags or "undated_cumulative_order_unknown" in flags:
        model = "unknown"
    return UsageEvent(
        event_key=row["event_key"], provider="openai", surface=row.get("surface", "unknown"),
        session_id=row["session_id"], timestamp=timestamp, model=model,
        request_id=row.get("request_id"), quality="reported", method="codex_cumulative_delta",
        native=retained, flags=sorted(set(flags)),
        **{target: delta[source] for source, target in FIELDS.items()},
    )


def _session_events(rows: list[dict], observed: dict[str, list[dict]]) -> list[UsageEvent]:
    parent, baseline = _fork_baseline(rows, observed)
    rows, excluded_ancestors = _exclude_ancestral_copies(rows, observed)
    rows, excluded_context_replays = _exclude_context_replays(rows)
    dated = sorted((row for row in rows if row["_observed_at"] is not None), key=_sort_key)
    undated = [row for row in rows if row["_observed_at"] is None]
    uncertain_dates = False
    if dated:
        ordered = dated
        # Untimed snapshots cannot be positioned relative to resets. Keep them
        # in raw evidence, but never add an overlapping aggregate to dated rows.
        uncertain_dates = any(not any(_same_counter(row["_counts"], known["_counts"]) for known in dated) for row in undated)
    else:
        # Without any ordering evidence, the largest observed cumulative value
        # is a conservative aggregate. Counts across resets remain unrecovered.
        ordered = [max(undated, key=lambda row: (row["_counts"]["total_tokens"] or 0,
                    sum(value or 0 for value in row["_counts"].values()), row["event_key"]))] if undated else []

    result = []
    previous = None
    for row in ordered:
        current = row["_counts"]
        last = _counts(row["native"].get("last_usage"))
        timestamp = row["_observed_at"]
        flags = []
        had_previous = previous is not None
        if previous is None:
            if parent:
                if baseline is not None and _same_counter(current, baseline):
                    previous = current
                    continue  # A quota refresh of inherited usage is not work.
                if baseline is not None and _initial_is_one_request(current, last):
                    # A restarted request can exceed the parent's entire old
                    # history. Equality with last_usage is stronger evidence of
                    # a fresh counter than the magnitude of that counter.
                    delta = dict(current)
                    flags.append("fork_counters_restarted")
                elif (baseline is not None and current["total_tokens"] is not None
                        and baseline["total_tokens"] is not None and current["total_tokens"] > baseline["total_tokens"]):
                    delta, flags = _difference(current, baseline)
                    flags.append("fork_inherited_baseline_delta")
                else:
                    delta = dict(last)
                    flags.append("fork_initial_last_request" if any(value is not None for value in last.values()) else "fork_baseline_unattributed")
            else:
                delta = dict(current)
                if not _initial_is_one_request(current, last):
                    timestamp = None
                    flags.append("initial_cumulative_timestamp_unknown")
        elif _same_counter(current, previous):
            # Identical totals often accompany quota changes. They can enrich
            # the predecessor's missing categories without adding another turn.
            previous = {key: value if value is not None else previous[key] for key, value in current.items()}
            continue
        else:
            delta, flags = _difference(current, previous)
        if (had_previous and delta["total_tokens"] is not None and last["total_tokens"] is not None
                and delta["total_tokens"] != last["total_tokens"]):
            # Keep the ending observation's timestamp, while exposing that the
            # interval cannot be interpreted as one observed request. Missing
            # intermediate telemetry can span requests, models, or local days.
            flags.append("cumulative_interval_contains_multiple_requests")
        previous = current
        if not dated:
            timestamp = None
            flags.append("undated_cumulative_order_unknown")
        if uncertain_dates:
            flags.append("undated_snapshots_not_attributed")
        # Preserve unknown usage as an unknown event, but omit proven zero work.
        known = [value for value in delta.values() if value is not None]
        if known and not any(known):
            continue
        if excluded_ancestors and not result:
            flags.append("retimestamped_ancestor_history_excluded")
        if excluded_context_replays and not result:
            flags.append("flattened_context_history_excluded")
        result.append(_make_event(row, delta, flags, timestamp))
    return result


def reconciled_events(store, sessions: set[str]) -> list[UsageEvent]:
    """Return derived events; the caller owns atomic replacement/provenance.

    Schema contract: ``codex_observations(event_key, session_id, observed_at,
    payload)`` stores JSON ``asdict(UsageEvent)`` payloads. Derived event keys
    match their chosen raw observations, allowing source relationships to be
    copied from ``codex_observation_sources(event_key, source_path)``.
    """
    observed = _observations(store)
    # A flattened parent can stamp its own future counters before the child's
    # creation. Proven rewrite copies must not supply ancestry/time authority or
    # a fork baseline. Keep raw child rows for its own exclusion flags/provenance,
    # but use independently timed parent evidence when comparing across sessions.
    chronological = {session: _exclude_context_replays(rows)[0] for session, rows in observed.items()}
    return [event for session in sorted(sessions) if session in observed
            for event in _session_events(observed[session], chronological)]
