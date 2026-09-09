"""Capture Codex cumulative telemetry for cross-file reconciliation.

The fallback counts describe this file's local delta, but storage must reconcile
the retained cumulative snapshots across files before reporting usage. Every
snapshot is emitted, including unchanged quota refreshes. This prevents partial
files and copied history from replacing a known increment with an overlapping
cumulative total. Only allowlisted numeric and identity metadata is retained.
"""

from usage_tracker.models import ParseResult, Snapshot, UsageEvent, count, stable_key, utc_timestamp

FIELDS = {"input_tokens": "input_tokens", "cached_input_tokens": "cached_input_tokens",
          "cache_write_input_tokens": "cache_creation_tokens", "output_tokens": "output_tokens",
          "reasoning_output_tokens": "reasoning_tokens", "total_tokens": "total_tokens"}


def _label(value):
    return value if isinstance(value, str) and 0 < len(value) <= 256 else None


def _surface(meta: dict) -> str:
    source = meta.get("source")
    if isinstance(source, dict) and "subagent" in source:
        return "codex_subagent"
    if "desktop" in str(meta.get("originator", "")).lower():
        return "codex_desktop"
    if source in ("cli", "exec"):
        return "codex_cli"
    if source == "vscode":
        return "codex_ide"
    return "unknown"


def _quota(payload: dict, timestamp: str | None) -> Snapshot | None:
    rate = payload.get("rate_limits")
    if not isinstance(rate, dict):
        return None
    # A strict whitelist prevents unrelated account data from entering SQLite.
    data = {k: rate[k] for k in ("limit_id", "limit_name", "plan_type") if isinstance(rate.get(k), str)}
    for window in ("primary", "secondary"):
        raw = rate.get(window)
        if isinstance(raw, dict):
            data[window] = {k: raw[k] for k in ("used_percent", "window_minutes", "resets_at")
                            if isinstance(raw.get(k), (int, float)) and not isinstance(raw[k], bool)}
    if not data:
        return None
    return Snapshot(stable_key("openai", "quota", timestamp, data), "openai", "quota", timestamp, data)


def parse_line(record: dict, state: dict, *, source_path: str, surface: str = "unknown") -> ParseResult:
    result = ParseResult()
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return result
    timestamp = utc_timestamp(record.get("timestamp"))
    kind = record.get("type")
    if kind == "session_meta":
        # Rewritten desktop transcripts can omit the inner creation timestamp.
        # The session_meta envelope still records session creation; losing it
        # would prevent a fork from establishing its parent's historical cutoff.
        # Prefer the explicit inner value when both are available.
        created = utc_timestamp(payload.get("timestamp")) or timestamp
        meta = {"id": _label(payload.get("id")), "created": created,
                "surface": _surface(payload), "forked": _label(payload.get("forked_from_id"))}
        if "owner" not in state:
            state["owner"] = meta
        state["history"] = meta
        return result
    if kind == "turn_context":
        state["model"] = _label(payload.get("model")) or "unknown"
        state["turn_id"] = _label(payload.get("turn_id"))
        state["turn_context_at"] = timestamp
        return result
    if kind != "event_msg" or payload.get("type") != "token_count":
        return result
    quota = _quota(payload, timestamp)
    if quota:
        result.snapshots.append(quota)
    info = payload.get("info")
    if not isinstance(info, dict):
        return result
    raw = info.get("total_token_usage")
    last = info.get("last_token_usage")
    if not isinstance(raw, dict):
        # Last-only events cannot reliably be deduplicated across periodic quota
        # emissions. Fail closed instead of multiplying an old request's usage.
        result.warnings.append("codex_missing_cumulative_usage")
        return result
    totals = {k: count(raw.get(k)) for k in FIELDS}
    if totals["total_tokens"] is None and totals["input_tokens"] is not None and totals["output_tokens"] is not None:
        totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    if all(v is None for v in totals.values()):
        return result
    owner = state.get("owner", {"id": stable_key(source_path), "surface": surface})
    # Copied parent events predate the fork. Later events belong to the owner,
    # even when copied session_meta rows most recently named the parent.
    historical = bool(owner.get("created") and timestamp and timestamp < owner["created"])
    identity = state.get("history", owner) if historical else owner
    session = str(identity.get("id") or stable_key(source_path))
    counters = state.setdefault("counters", {})
    previous = counters.get(session)
    flags = []
    event_time = timestamp
    if previous is None:
        delta = totals.copy()
        if owner.get("forked") and not historical:
            # New fork counters can either restart or include inherited history.
            # Only the last request is attributable on the first observation.
            if isinstance(last, dict):
                delta = {k: count(last.get(k)) for k in FIELDS}
                flags.append("fork_initial_last_request")
            else:
                result.warnings.append("fork_baseline_unattributed")
                delta = {key: None for key in FIELDS}
        elif isinstance(last, dict) and count(last.get("total_tokens")) not in (None, totals["total_tokens"]):
            event_time = None
            flags.append("initial_cumulative_timestamp_unknown")
    elif (totals["total_tokens"] is not None and previous.get("total_tokens") is not None
          and totals["total_tokens"] < previous["total_tokens"]):
        delta = totals.copy()
        flags.append("cumulative_counter_reset")
    else:
        delta = {}
        for key, value in totals.items():
            before = previous.get(key)
            delta[key] = value - before if value is not None and before is not None and value >= before else None
            if value is not None and before is not None and value < before:
                flags.append(f"nonmonotonic_{key}")
    counters[session] = totals
    counter_times = state.setdefault("counter_timestamps", {})
    counter_times[session] = timestamp
    if delta["total_tokens"] is None and delta["input_tokens"] is not None and delta["output_tokens"] is not None:
        delta["total_tokens"] = delta["input_tokens"] + delta["output_tokens"]
    if timestamp is None:
        flags.append("timestamp_unknown")
    native = {k: v for k, v in delta.items() if v is not None}
    native["counter_basis"] = "cumulative_delta"
    native["cumulative_usage"] = totals.copy()
    native["last_usage"] = {key: count(last.get(key)) for key in FIELDS} if isinstance(last, dict) else {}
    native["observed_at"] = timestamp
    native["observed_model"] = state.get("model", "unknown")
    # Compacted desktop transcripts can flatten every historical record onto
    # the turn-context timestamp. Equality is only a candidate marker, never
    # grounds for dropping usage by itself. Reconciliation requires multiple
    # exact counter matches to independently timed records of the same turn.
    native["turn_context_at"] = state.get("turn_context_at")
    native["context_timestamp_replay_candidate"] = bool(timestamp and timestamp == state.get("turn_context_at"))
    native["owner_created"] = identity.get("created")
    native["forked_from_id"] = identity.get("forked")
    native["copied_history"] = historical
    # A copied parent snapshot is direct evidence of the child's inherited
    # counters, even if the original parent file is no longer available.
    parent = identity.get("forked")
    if parent and parent in counters:
        native["inherited_baseline"] = {key: count(counters[parent].get(key)) for key in FIELDS}
        native["inherited_baseline_at"] = counter_times.get(parent)
    # The original timestamp, rather than a file offset, makes copies idempotent.
    result.events.append(UsageEvent(
        event_key=stable_key("openai", "codex", session, timestamp, totals), provider="openai",
        surface=identity.get("surface", surface), session_id=session, timestamp=event_time,
        model="unknown" if "initial_cumulative_timestamp_unknown" in flags else state.get("model", "unknown"),
        request_id=state.get("turn_id"), method="codex_cumulative_delta", native=native, flags=flags,
        **{target: delta[source] for source, target in FIELDS.items()}))
    return result
