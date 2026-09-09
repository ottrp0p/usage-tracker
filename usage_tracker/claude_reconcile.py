"""Persistent Claude result evidence and conservative main-turn reconciliation.

Provider results and per-model SDK-call counters are different scopes. Only an
explicitly verified main-agent turn window can contribute a derived remainder;
all original message evidence stays intact. Recomputing after every relevant
ingestion makes late messages replace, rather than add to, that remainder.
"""

from collections import defaultdict
from dataclasses import asdict
import json
import math
import re

from usage_tracker.models import ParseResult, UsageEvent, count, stable_key, utc_timestamp

METHOD = "claude_summary_remainder"
COMPONENTS = ("uncached_input_tokens", "cached_input_tokens", "cache_creation_tokens", "output_tokens")
NATIVE_COMPONENTS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
COUNT_KEYS = set(NATIVE_COMPONENTS) | {
    "thinking_tokens", "total_tokens", "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens",
    "inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens",
    "webSearchRequests", "webFetchRequests", "contextWindow", "maxOutputTokens",
}
NATIVE_STRINGS = {
    "result_id", "subtype", "terminal_reason", "stop_reason", "source_kind", "scope",
    "scope_evidence", "model_usage_scope", "boundary_method", "window_source", "user_message_uuid",
    "parent_tool_use_id", "agent_id", "sdk_call_id",
}
NATIVE_COUNTS = {"num_turns", "duration_ms", "duration_api_ms"}
NATIVE_TIMES = {"time_origin_ms", "request_sent_wall_ms"}
NATIVE_BOOLEANS = {"scope_verified", "window_verified", "is_error", "cache_history_complete", "is_sidechain"}
IDENTIFIER = re.compile(r"[\w./:+-]{1,256}\Z")


def _counts(value: object) -> dict:
    """Allow only known numeric telemetry fields, never arbitrary nested data."""
    if not isinstance(value, dict):
        return {}
    result = {key: count(item) for key, item in value.items() if key in COUNT_KEYS}
    for key in ("costUSD", "cost_usd", "total_cost_usd"):
        item = value.get(key)
        if isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) and item >= 0:
            result[key] = item
    return result


def _native(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {key: item for key, item in value.items() if key in NATIVE_STRINGS
              and isinstance(item, str) and IDENTIFIER.fullmatch(item)}
    result.update({key: count(item) for key, item in value.items() if key in NATIVE_COUNTS})
    result.update({key: item for key, item in value.items() if key in NATIVE_TIMES
                   and isinstance(item, (int, float)) and not isinstance(item, bool)
                   and math.isfinite(item) and 0 <= item <= 8.64e15})
    result.update({key: item for key, item in value.items() if key in NATIVE_BOOLEANS and isinstance(item, bool)})
    linked = value.get("user_message_uuids")
    if isinstance(linked, list):
        result["user_message_uuids"] = sorted({item for item in linked[:256]
                                              if isinstance(item, str) and IDENTIFIER.fullmatch(item)})
    return result


def persist_summary(store, summary, source_path: str | None) -> set[str]:
    """Store an immutable payload revision and advance only genuinely new heads.

    A repeated old version does not become current merely because an archive was
    scanned later. Each source's latest *first-seen distinct* revision is its
    head. Conflicting heads from different sources are retained for review and
    cannot authorize an additive remainder.
    """
    from usage_tracker.storage import now

    payload = asdict(summary)
    for field in ("summary_key", "provider", "surface", "session_id"):
        if not isinstance(payload[field], str) or not IDENTIFIER.fullmatch(payload[field]):
            raise ValueError(f"Summary {field} must be a metadata identifier")
    payload["timestamp"] = utc_timestamp(payload["timestamp"])
    payload["started_at"] = utc_timestamp(payload["started_at"])
    payload["terminal"] = payload["terminal"] is True
    payload["usage"] = _counts(payload["usage"])
    payload["model_usage"] = {model: _counts(usage) for model, usage in payload["model_usage"].items()
                              if isinstance(model, str) and IDENTIFIER.fullmatch(model)} if isinstance(payload["model_usage"], dict) else {}
    payload["native"] = _native(payload["native"])
    payload["flags"] = sorted({flag for flag in payload["flags"]
                               if isinstance(flag, str) and re.fullmatch(r"[a-z_]{1,80}", flag)})
    revision_id = stable_key("claude_summary_revision", payload)
    seen_at = now()
    columns = ("summary_key", "provider", "surface", "session_id", "timestamp", "started_at", "terminal",
               "usage", "model_usage", "native", "flags")
    values = [json.dumps(payload[key], sort_keys=True) if key in ("usage", "model_usage", "native", "flags") else payload[key]
              for key in columns]
    store.conn.execute(f"""INSERT INTO claude_summary_revisions
        (revision_id,{','.join(columns)},first_seen,last_seen) VALUES ({','.join('?' for _ in range(len(columns)+3))})
        ON CONFLICT(revision_id) DO UPDATE SET last_seen=excluded.last_seen""", [revision_id, *values, seen_at, seen_at])
    source_path = source_path or ""
    previous = store.conn.execute("SELECT observation_id FROM claude_summary_sources WHERE revision_id=? AND source_path=?",
                                  (revision_id, source_path)).fetchone()
    store.conn.execute("""INSERT INTO claude_summary_sources(revision_id,source_path,first_seen,last_seen) VALUES (?,?,?,?)
        ON CONFLICT(revision_id,source_path) DO UPDATE SET last_seen=excluded.last_seen""", (revision_id, source_path, seen_at, seen_at))
    if previous:
        return set()
    # Identity corrections can change a session. Reconcile both old and new
    # ownership so an earlier session cannot retain an obsolete remainder.
    return {row[0] for row in store.conn.execute("SELECT DISTINCT session_id FROM claude_summary_revisions WHERE summary_key=?",
                                                (payload["summary_key"],))}


def _decode(row) -> dict:
    item = dict(row)
    for field in ("usage", "model_usage", "native", "flags"):
        item[field] = json.loads(item[field])
    return item


def _normalized(components: dict) -> dict:
    """Return disjoint counts and their inclusive-input/total conveniences."""
    result = {field: components.get(field) for field in COMPONENTS}
    inputs = [result[field] for field in COMPONENTS[:3]]
    result["input_tokens"] = sum(inputs) if all(value is not None for value in inputs) else None
    result["total_tokens"] = result["input_tokens"] + result["output_tokens"] if result["input_tokens"] is not None and result["output_tokens"] is not None else None
    return result


def _summary_counts(usage: dict) -> dict:
    return _normalized({field: count(usage.get(native)) for field, native in zip(COMPONENTS, NATIVE_COMPONENTS)})


def _event_counts(event: dict) -> dict:
    values = {field: count(event.get(field)) for field in COMPONENTS[1:]}
    inputs = [count(event.get(field)) for field in ("input_tokens", "cached_input_tokens", "cache_creation_tokens")]
    values["uncached_input_tokens"] = None
    if all(value is not None for value in inputs):
        ordinary = inputs[0] - inputs[1] - inputs[2]
        if ordinary >= 0:
            values["uncached_input_tokens"] = ordinary
    return _normalized(values)


def _linked_users(native: dict) -> frozenset[str]:
    values = set(native.get("user_message_uuids", []))
    if native.get("user_message_uuid"):
        values.add(native["user_message_uuid"])
    return frozenset(values)


def _retain_scope(item: dict, history: list[dict]) -> None:
    """Keep proven UUID boundaries across cache eviction, never old counters.

    A newer copy may omit its linked user record while retaining the result's
    stable identity and UUID links. Previously captured proof remains valid for
    that same identity. Contradictory links or proven boundaries cannot be
    resolved by observation order and therefore revoke additive eligibility.
    """
    native = item["native"]
    identity = (item["provider"], item["session_id"], item["timestamp"], native.get("result_id"))
    compatible = [row for row in history if (row["provider"], row["session_id"], row["timestamp"], row["native"].get("result_id")) == identity]
    # Positive subagent provenance is sticky for the same result identity. A
    # weaker copy, or a later copy lacking its wrapper, must not erase evidence
    # that the result belongs to a child agent rather than the main turn.
    for field in ("agent_id", "parent_tool_use_id"):
        known = sorted({row["native"][field] for row in compatible if row["native"].get(field)})
        if known:
            native[field] = known[0]
    if any(row["native"].get("is_sidechain") is True for row in compatible):
        native["is_sidechain"] = True
    known_links = {_linked_users(row["native"]) for row in compatible if _linked_users(row["native"])}
    current_links = _linked_users(native)
    if current_links:
        known_links.add(current_links)
    proof = [row for row in compatible if _verified(row) and _window_valid(row)
             and current_links and _linked_users(row["native"]) == current_links]
    known_starts = {row["started_at"] for row in compatible if row["started_at"] is not None}
    if item["started_at"] is not None:
        known_starts.add(item["started_at"])
    item["scope_conflict"] = len(known_links) > 1 or len(known_starts) > 1
    item["retained_scope_revision_ids"] = []
    if item["scope_conflict"] or not proof or native.get("parent_tool_use_id") or native.get("agent_id") or native.get("is_sidechain"):
        return
    if not _verified(item) or item["started_at"] is None:
        selected = min(proof, key=lambda row: (row["first_seen"], row["revision_id"]))
        item["started_at"] = selected["started_at"]
        for field in ("scope", "scope_verified", "window_verified", "boundary_method", "window_source"):
            if field in selected["native"]:
                native[field] = selected["native"][field]
        item["retained_scope_revision_ids"] = sorted(row["revision_id"] for row in proof)


def _head_summaries(store, sessions: set[str] | None) -> list[dict]:
    # First identify every revision for affected identities. Session correction
    # must see the whole identity history rather than only the newest session.
    if sessions is not None and not sessions:
        return []
    where, params = "", []
    if sessions is not None:
        where = f" WHERE r.summary_key IN (SELECT summary_key FROM claude_summary_revisions WHERE session_id IN ({','.join('?' for _ in sessions)}))"
        params = sorted(sessions)
    revisions = [_decode(row) for row in store.conn.execute("SELECT r.* FROM claude_summary_revisions r" + where, params)]
    by_revision = {row["revision_id"]: row for row in revisions}
    if not by_revision:
        return []
    heads = {}
    for source in store.conn.execute("SELECT * FROM claude_summary_sources ORDER BY observation_id"):
        revision = by_revision.get(source["revision_id"])
        if revision:
            heads[(revision["summary_key"], source["source_path"])] = (dict(source), revision)
    grouped = defaultdict(list)
    for source, revision in heads.values():
        grouped[revision["summary_key"]].append((source, revision))
    selected = []
    for summary_key, candidates in grouped.items():
        # Prefer explicit scope/window enrichment over a source that lacks those
        # fields, then choose a deterministic latest distinct source observation.
        source, revision = max(candidates, key=lambda pair: (
            pair[1]["native"].get("scope_verified") is True and pair[1]["native"].get("window_verified") is True,
            pair[0]["observation_id"], pair[1]["revision_id"]))
        item = dict(revision)
        item["source_paths"] = sorted({candidate[0]["source_path"] for candidate in candidates if candidate[0]["source_path"]})
        item["head_revision_ids"] = sorted({candidate[1]["revision_id"] for candidate in candidates})
        # Broader per-model totals and source/cache metadata cannot contradict
        # the main-turn authority. Actual main-turn counts or known boundaries
        # that disagree do block reconciliation across sources.
        conflict = False
        for field in ("provider", "session_id", "timestamp", "started_at"):
            known = [candidate[1][field] for candidate in candidates if candidate[1][field] is not None]
            if known and any(value != known[0] for value in known[1:]):
                conflict = True
            elif known and item[field] is None:
                item[field] = known[0]
        # Missing categories and absent completion metadata are complementary
        # evidence, not contradictory counts. Never replace a known value with
        # an unknown one or manufacture a maximum between disagreeing sources.
        merged_usage = {}
        for field in set().union(*(candidate[1]["usage"] for candidate in candidates)):
            known = [candidate[1]["usage"].get(field) for candidate in candidates if candidate[1]["usage"].get(field) is not None]
            if known and any(value != known[0] for value in known[1:]):
                conflict = True
            merged_usage[field] = known[0] if known else None
        item["usage"] = merged_usage
        item["native"] = dict(item["native"])
        for field in ("is_error", "subtype", "terminal_reason"):
            known = [candidate[1]["native"].get(field) for candidate in candidates if candidate[1]["native"].get(field) is not None]
            if known and any(value != known[0] for value in known[1:]):
                conflict = True
            elif known:
                item["native"][field] = known[0]
        item["terminal"] = any(candidate[1]["terminal"] for candidate in candidates)
        history = [row for row in revisions if row["summary_key"] == summary_key]
        # LevelDB/SST iteration order is not revision chronology. A cache can
        # return A, B, then A again without proving which counters are current.
        # Keep all such versions and refuse additions whenever ANY known core
        # count disagrees across cached revisions. Ordered JSONL-only revisions
        # retain the correction behavior above, including downward corrections.
        cached = [row for row in history if row["native"].get("source_kind") == "desktop_conversation_cache"]
        cache_conflicts = []
        for field in NATIVE_COMPONENTS:
            known = {row["usage"].get(field) for row in cached if row["usage"].get(field) is not None}
            if len(known) > 1:
                cache_conflicts.append(field)
            elif known and item["usage"].get(field) is None:
                # Compatible fragments may enrich a missing category without
                # guessing a chronology or replacing an explicit zero.
                item["usage"][field] = next(iter(known))
        item["cache_revision_conflict_fields"] = sorted(cache_conflicts)
        item["cache_revision_ids"] = sorted(row["revision_id"] for row in cached)
        item["conflicting_revisions"] = conflict or bool(cache_conflicts)
        _retain_scope(item, history)
        selected.append(item)
    return sorted(selected, key=lambda item: item["summary_key"])


def _is_subagent(row: dict) -> bool:
    return "subagent" in row["flags"] or bool(row["native"].get("agent_id") or row["native"].get("parent_tool_use_id"))


def _window_valid(summary: dict) -> bool:
    return bool(summary["started_at"] and summary["timestamp"] and summary["started_at"] < summary["timestamp"])


def _verified(summary: dict) -> bool:
    native = summary["native"]
    return (summary["provider"] == "anthropic" and summary["session_id"] not in ("", "unknown")
            and native.get("scope") == "main_agent_run"
            and native.get("scope_verified") is True and native.get("window_verified") is True
            and not summary.get("scope_conflict", False)
            and not native.get("parent_tool_use_id") and not native.get("agent_id") and not native.get("is_sidechain"))


def _reconciliation(summary: dict, messages: list[dict], overlapping: bool) -> tuple[dict, UsageEvent | None]:
    valid_window = _window_valid(summary)
    candidates = [row for row in messages if row["timestamp"] and valid_window
                  and summary["started_at"] <= row["timestamp"] <= summary["timestamp"]]
    subagents = [row for row in candidates if _is_subagent(row)]
    matched = [row for row in candidates if not _is_subagent(row)]
    unknown_dates = [row for row in messages if row["timestamp"] is None and not _is_subagent(row)]
    counts = [_event_counts(row) for row in matched]
    invalid_message_totals = sum(count(row["total_tokens"]) is None or row["total_tokens"] != values["total_tokens"]
                                 for row, values in zip(matched, counts))
    unknown = {field: sum(row[field] is None for row in counts) for field in COMPONENTS}
    observed = _normalized({field: sum(row[field] for row in counts) if not unknown[field] else None for field in COMPONENTS})
    summary_usage = _summary_counts(summary["usage"])
    known_models = sorted({row["model"] for row in matched if row["model"] not in ("unknown", "")})
    model_keys = sorted(model for model in summary["model_usage"] if model != "unknown")
    # modelUsage covers a broader SDK call. One consistent model can identify a
    # remainder, but several models never authorize guessing a split of it.
    model = model_keys[0] if len(model_keys) == 1 and all(value == model_keys[0] for value in known_models) else "unknown"
    metadata = {"matched_event_keys": sorted(row["event_key"] for row in matched),
                "excluded_subagent_events": len(subagents), "unknown_timestamp_events": len(unknown_dates),
                "unknown_fields": unknown, "models": known_models, "model": model,
                "inconsistent_or_unknown_total_events": invalid_message_totals,
                "model_unattributed": model == "unknown", "scope_verified": _verified(summary),
                "window_verified": summary["native"].get("window_verified") is True,
                "candidate_window": not _verified(summary), "head_revision_ids": summary["head_revision_ids"],
                "coalesced_usage": summary["usage"], "coalesced_native": summary["native"],
                "effective_started_at": summary["started_at"], "scope_conflict": summary["scope_conflict"],
                "retained_scope_revision_ids": summary["retained_scope_revision_ids"],
                "cache_revision_ids": summary["cache_revision_ids"],
                "cache_revision_conflict_fields": summary["cache_revision_conflict_fields"]}
    status, detail = "matched", "Verified main-agent turn matches the observed message categories."
    native = summary["native"]
    if summary["conflicting_revisions"]:
        status = "conflicting_revisions"
        detail = ("Cached revisions disagree on main-turn counts; cache file order does not establish correction chronology."
                  if summary["cache_revision_conflict_fields"] else "Current source revisions disagree on main-turn counts or boundaries.")
    elif summary["scope_conflict"]:
        status, detail = "ambiguous_scope", "Recorded revisions disagree on linked user identities or known turn boundaries."
    elif not _verified(summary):
        status, detail = "ambiguous_scope", "Main-agent membership and an explicit linked turn window are not both verified."
    elif not valid_window:
        status, detail = "missing_window", "A valid explicit start and completion timestamp are required."
    elif not summary["terminal"] or native.get("is_error") is True or native.get("subtype") not in (None, "success") or native.get("terminal_reason") not in (None, "completed"):
        status, detail = "nonterminal", "The result does not establish a successfully completed turn."
    elif overlapping:
        status, detail = "overlapping_runs", "Distinct verified result windows overlap in this session; membership is ambiguous."
    elif unknown_dates:
        status, detail = "undated_messages", "Main-agent messages without dates could overlap the summary remainder."
    elif any(summary_usage[field] is None for field in COMPONENTS):
        status, detail = "incomplete_summary", "The summary lacks at least one native input, cache, or output category."
    elif summary["usage"].get("total_tokens") is not None and summary["usage"]["total_tokens"] != summary_usage["total_tokens"]:
        status, detail = "incomplete_summary", "The summary's total disagrees with its native token categories."
    elif any(unknown.values()) or invalid_message_totals:
        status, detail = "incomplete_messages", "Matched messages have unknown or inconsistent token categories."
    elif any(observed[field] > summary_usage[field] for field in COMPONENTS):
        status, detail = "count_conflict", "Observed main-agent usage exceeds the result in at least one category."
    remainder = _normalized({field: None for field in COMPONENTS})
    event = None
    if status == "matched":
        remainder = _normalized({field: summary_usage[field] - observed[field] for field in COMPONENTS})
        if remainder["total_tokens"] > 0:
            status, detail = "reconciled", "Added only the verified main-agent remainder; later message evidence replaces it."
            flags = ["summary_remainder", "interval_usage_at_completion"]
            if model == "unknown":
                flags.append("model_unattributed")
            event = UsageEvent(
                event_key=stable_key(METHOD, summary["summary_key"]), provider="anthropic", surface=summary["surface"],
                session_id=summary["session_id"], timestamp=summary["timestamp"], model=model,
                request_id=summary["native"].get("result_id"), method=METHOD,
                input_tokens=remainder["input_tokens"], cached_input_tokens=remainder["cached_input_tokens"],
                cache_creation_tokens=remainder["cache_creation_tokens"], output_tokens=remainder["output_tokens"],
                total_tokens=remainder["total_tokens"], reasoning_tokens=None,
                native={"summary_key": summary["summary_key"], "revision_id": summary["revision_id"],
                        "started_at": summary["started_at"], "completed_at": summary["timestamp"],
                        "scope": "main_agent_run", "matched_message_count": len(matched)}, flags=flags)
    row = {"summary_key": summary["summary_key"], "revision_id": summary["revision_id"], "status": status,
           "detail": detail, "matched_events": len(matched), "observed_usage": observed,
           "summary_usage": summary_usage, "remainder": remainder, "metadata": metadata}
    return row, event


def reconcile(store, sessions: set[str] | None = None) -> int:
    """Refresh derived events and status within the caller's open transaction."""
    from usage_tracker.storage import now

    if sessions is not None and sessions:
        # An identity correction can move a result to another session. Include
        # all linked session histories before assessing overlap; otherwise a
        # later event in the old session could accidentally clear a conflict
        # with another run in the new session.
        sessions = set(sessions)
        while True:
            linked = {row[0] for row in store.conn.execute(f"""
                SELECT DISTINCT sibling.session_id FROM claude_summary_revisions anchor
                JOIN claude_summary_revisions sibling ON sibling.summary_key=anchor.summary_key
                WHERE anchor.session_id IN ({','.join('?' for _ in sessions)})
            """, sorted(sessions))}
            if linked <= sessions:
                break
            sessions.update(linked)
    summaries = _head_summaries(store, sessions)
    if not summaries:
        return 0
    session_ids = sorted({summary["session_id"] for summary in summaries})
    messages = defaultdict(list)
    for row in store.conn.execute(f"""SELECT * FROM usage_events WHERE provider='anthropic' AND quality='reported'
        AND method!=? AND session_id IN ({','.join('?' for _ in session_ids)})""", [METHOD, *session_ids]):
        item = dict(row)
        item["native"], item["flags"] = json.loads(item["native"]), json.loads(item["flags"])
        item["timestamp"] = utc_timestamp(item["timestamp"])
        messages[item["session_id"]].append(item)
    overlaps = set()
    for index, first in enumerate(summaries):
        if not _verified(first) or not _window_valid(first) or not first["terminal"]:
            continue
        for second in summaries[index + 1:]:
            if (first["session_id"] == second["session_id"] and _verified(second) and _window_valid(second)
                    and second["terminal"] and first["started_at"] <= second["timestamp"]
                    and second["started_at"] <= first["timestamp"]):
                overlaps.update((first["summary_key"], second["summary_key"]))
    changed = 0
    for summary in summaries:
        row, event = _reconciliation(summary, messages[summary["session_id"]], summary["summary_key"] in overlaps)
        key = stable_key(METHOD, summary["summary_key"])
        if event is None:
            store.conn.execute("DELETE FROM event_sources WHERE event_key=?", (key,))
            changed += store.conn.execute("DELETE FROM usage_events WHERE event_key=? AND method=?", (key, METHOD)).rowcount
        else:
            changed += store.ingest(ParseResult(events=[event]), None, derived=True)
            # Derived provenance reflects current evidence; raw summary source
            # revisions remain immutable history in their separate ledger.
            store.conn.execute("DELETE FROM event_sources WHERE event_key=?", (key,))
            store.conn.executemany("INSERT INTO event_sources VALUES (?,?)", [(key, path) for path in summary["source_paths"]])
        encoded = {field: json.dumps(value, sort_keys=True) if field in ("observed_usage", "summary_usage", "remainder", "metadata") else value
                   for field, value in row.items()}
        old = store.conn.execute("SELECT * FROM claude_reconciliations WHERE summary_key=?", (summary["summary_key"],)).fetchone()
        if old is None or any(value != old[field] for field, value in encoded.items()):
            encoded["updated_at"] = now()
            columns = list(encoded)
            store.conn.execute(f"""INSERT INTO claude_reconciliations({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})
                ON CONFLICT(summary_key) DO UPDATE SET {','.join(field+'=excluded.'+field for field in columns if field != 'summary_key')}""", list(encoded.values()))
    return changed


def read_reconciliations(store, *, start=None, end=None, provider="all", surface="all", model="all", limit=50, offset=0) -> dict:
    """Read a bounded filtered page; timestamps are UTC bounds [start, end)."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    clauses, params = [], []
    for field, value in (("provider", provider), ("surface", surface)):
        if value != "all":
            clauses.append(f"r.{field}=?")
            params.append(value)
    for boundary, operator in ((start, ">="), (end, "<")):
        if boundary is not None:
            timestamp = utc_timestamp(boundary)
            if timestamp is None:
                raise ValueError("Summary time bounds must be valid timestamps")
            clauses.append(f"r.timestamp{operator}?")
            params.append(timestamp)
    if model != "all":
        clauses.append("(json_extract(c.metadata,'$.model')=? OR EXISTS(SELECT 1 FROM json_each(r.model_usage) WHERE key=?))")
        params.extend((model, model))
    query = " FROM claude_reconciliations c JOIN claude_summary_revisions r ON r.revision_id=c.revision_id"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    total = store.conn.execute("SELECT COUNT(*)" + query, params).fetchone()[0]
    items = []
    for row in store.conn.execute("SELECT r.*,c.status,c.detail,c.matched_events,c.observed_usage,c.summary_usage,c.remainder,c.metadata,c.updated_at" + query
                                  + " ORDER BY r.timestamp DESC,r.summary_key LIMIT ? OFFSET ?", [*params, limit, offset]):
        item = _decode(row)
        for field in ("observed_usage", "summary_usage", "remainder", "metadata"):
            item[field] = json.loads(item[field])
        item["terminal"] = bool(item["terminal"])
        item["reason"] = item["detail"]
        item["matched_message_count"] = item["matched_events"]
        item["matched_usage"] = item["observed_usage"]
        item["revision_usage"] = item["usage"]
        item["usage"] = item["metadata"].get("coalesced_usage", item["usage"])
        item["revision_native"], item["revision_started_at"] = item["native"], item["started_at"]
        item["native"] = item["metadata"].get("coalesced_native", item["native"])
        item["started_at"] = item["metadata"].get("effective_started_at", item["started_at"])
        lifetime = store.conn.execute("SELECT COUNT(*),MIN(first_seen),MAX(last_seen) FROM claude_summary_revisions WHERE summary_key=?", (item["summary_key"],)).fetchone()
        item["revision_count"], item["first_seen"], item["last_seen"] = tuple(lifetime)
        item["source_paths"] = [source[0] for source in store.conn.execute("""SELECT DISTINCT s.source_path FROM claude_summary_sources s
            JOIN claude_summary_revisions r ON r.revision_id=s.revision_id WHERE r.summary_key=? AND s.source_path!='' ORDER BY s.source_path""", (item["summary_key"],))]
        items.append(item)
    return {"items": items, "total": total, "limit": limit, "offset": offset}
