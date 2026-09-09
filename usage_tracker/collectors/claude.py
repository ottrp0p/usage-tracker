"""Metadata-only adapters for Claude Code and desktop agent JSONL.

An assistant response can be written once per content block, copied into another
history, and also captured by the desktop audit logger before its usage becomes
final. These records are observations of ONE provider message, not token deltas.
The store must upsert ``event_key`` and honor ``native.usage_priority``.
"""

from __future__ import annotations

import math
from pathlib import Path
import re
from typing import Any

from usage_tracker.models import ParseResult, Snapshot, UsageEvent, count, stable_key, utc_timestamp


_COUNT_FIELDS = (
    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
    "output_tokens",
)
_CONTEXT_FIELDS = (
    "sessionId", "session_id", "timestamp", "created_at", "_audit_timestamp", "requestId",
    "request_id", "agentId", "parent_tool_use_id", "entrypoint", "isSidechain",
)
_WINDOW_MINUTES = {"five_hour": 300, "seven_day": 10080, "seven_day_overage_included": 10080}
_WINDOW_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_STOP_REASONS = {"end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal", "model_context_window_exceeded"}


def _string(value: object) -> str | None:
    """IDs and model labels are bounded metadata, never arbitrary payloads."""
    return value if isinstance(value, str) and 0 < len(value) <= 256 else None


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _timestamp(record: dict) -> str | None:
    """Prefer event evidence, including remote records' created_at timestamps."""
    for key in ("timestamp", "created_at", "_audit_timestamp"):
        value = utc_timestamp(record.get(key))
        if value is not None:
            return value
    return None


def _surface(record: dict, suggested: str, source_path: str) -> str:
    # A desktop project history has the same structure as a CLI project history.
    # Its explicit entrypoint (or the collector's source classification) decides.
    entrypoint = record.get("entrypoint")
    if entrypoint == "local-agent":
        return "claude_desktop_agent"
    if entrypoint == "cli":
        return "claude_code"
    if suggested in ("claude_code", "claude_desktop_agent"):
        return suggested
    parts = Path(source_path).parts
    if "local-agent-mode-sessions" in parts:
        return "claude_desktop_agent"
    if ".claude" in parts and "projects" in parts:
        return "claude_code"
    return "unknown"


def _usage_metadata(usage: dict) -> dict:
    """A hard allowlist prevents text/tool payloads entering state or storage."""
    result = {key: count(usage.get(key)) for key in _COUNT_FIELDS if key in usage}
    creation = _mapping(usage.get("cache_creation"))
    for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"):
        if key in creation:
            result[key] = count(creation[key])
    output_details = _mapping(usage.get("output_tokens_details"))
    if "thinking_tokens" in output_details:
        result["thinking_tokens"] = count(output_details["thinking_tokens"])
    return result


def _event(metadata: dict, usage: dict, *, final: bool, audit: bool) -> UsageEvent:
    """Convert one provider usage observation without treating subsets as extra."""
    ordinary = count(usage.get("input_tokens"))
    cached = count(usage.get("cache_read_input_tokens"))
    creation = count(usage.get("cache_creation_input_tokens"))
    creation_parts = [count(usage.get(key)) for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")]
    output = count(usage.get("output_tokens"))
    reasoning = count(usage.get("thinking_tokens"))
    flags = []
    if creation is None and all(value is not None for value in creation_parts):
        creation = sum(creation_parts)
        flags.append("cache_creation_derived_from_ttls")
    elif creation is not None and all(value is not None for value in creation_parts) and creation != sum(creation_parts):
        flags.append("inconsistent_cache_creation_breakdown")

    # Anthropic input_tokens excludes both cache categories. Missing categories
    # remain unknown: a known ordinary count is not a complete inclusive total.
    input_counts = [ordinary, cached, creation]
    inclusive_input = sum(input_counts) if all(value is not None for value in input_counts) else None
    total = inclusive_input + output if inclusive_input is not None and output is not None else None
    if inclusive_input is None:
        flags.append("incomplete_input_usage")
    if output is None:
        flags.append("missing_output_usage")
    if not final:
        flags.append("provisional_usage")
    if reasoning is not None and output is not None and reasoning > output:
        flags.append("inconsistent_reasoning_subset")
    if metadata.get("is_subagent"):
        flags.append("subagent")
    if metadata.get("identity_fallback"):
        flags.append(metadata["identity_fallback"])

    native = dict(usage)
    native.update({
        "message_id": metadata.get("message_id"),
        "usage_is_final": final,
        # A final project response must survive a subsequent provisional audit
        # observation. Equal-priority cumulative observations may use maxima.
        "usage_priority": 30 if final else 10,
        "source_kind": "desktop_audit" if audit else "project_or_stream",
    })
    if metadata.get("agent_id"):
        native["agent_id"] = metadata["agent_id"]
    if metadata.get("parent_tool_use_id"):
        native["parent_tool_use_id"] = metadata["parent_tool_use_id"]
    return UsageEvent(
        event_key=metadata["event_key"], provider="anthropic",
        surface=metadata["surface"], session_id=metadata["session_id"],
        timestamp=metadata["timestamp"], model=metadata["model"],
        request_id=metadata.get("request_id"), input_tokens=inclusive_input,
        cached_input_tokens=cached, cache_creation_tokens=creation,
        output_tokens=output, reasoning_tokens=reasoning, total_tokens=total,
        native=native, flags=flags,
    )


def _quota(record: dict) -> Snapshot | None:
    """Preserve account evidence separately; never turn quota into token usage."""
    info = _mapping(record.get("rate_limit_info"))
    windows = []
    for name, window in list(_mapping(info.get("unifiedWindows")).items())[:64]:
        # Keep new provider window names without inventing a duration or mapping
        # model-specific/overage pools onto the ordinary weekly quota.
        if not isinstance(name, str) or not _WINDOW_NAME.fullmatch(name) or not isinstance(window, dict):
            continue
        ratio = window.get("utilization")
        valid_ratio = isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and 0 <= ratio <= 1e9 and math.isfinite(ratio)
        windows.append({
            "name": name,
            "window_minutes": _WINDOW_MINUTES.get(name),
            "used_percent": ratio * 100 if valid_ratio else None,
            "native_utilization": ratio if valid_ratio else None,
            "resets_at": utc_timestamp(window.get("resetsAt")),
        })
    data: dict[str, Any] = {"windows": windows, "source": "local_rate_limit_event"}
    for source, target, allowed in (
        ("status", "status", {"allowed", "allowed_warning", "rejected"}),
        ("overageStatus", "overage_status", {"allowed", "allowed_warning", "rejected"}),
        ("rateLimitType", "rate_limit_type", set(_WINDOW_MINUTES)),
    ):
        if isinstance(info.get(source), str) and info[source] in allowed:
            data[target] = info[source]
    if isinstance(info.get("isUsingOverage"), bool):
        data["is_using_overage"] = info["isUsingOverage"]
    if "resetsAt" in info:
        data["resets_at"] = utc_timestamp(info["resetsAt"])
    native_type = info.get("rateLimitType")
    if isinstance(native_type, str) and _WINDOW_NAME.fullmatch(native_type):
        data["rate_limit_type"] = native_type
    if not windows and len(data) == 2:
        return None
    timestamp = _timestamp(record)
    record_id = _string(record.get("uuid"))
    # Retain the original content-based identity for already-collected JSONL
    # quotas. A provider correction at the same UUID becomes another immutable
    # observation; newly captured identity metadata must not duplicate history.
    snapshot_key = stable_key("anthropic", "quota", timestamp, data)
    if record_id:
        data["event_id"] = record_id
    return Snapshot(
        snapshot_key=snapshot_key,
        provider="anthropic", kind="quota", timestamp=timestamp, data=data,
    )


def _summary(record: dict, state: dict, *, session: str | None, source_path: str, surface: str):
    """Retain SDK result evidence separately from additive message usage.

    result.usage describes the main agent's current turn; modelUsage can include
    the whole SDK call and subagents. A process time_origin_ms does not identify
    a turn start. Only explicit result-to-user UUID links establish this window.
    """
    from usage_tracker.models import UsageSummary

    record_id = _string(record.get("uuid"))
    if not record_id:
        return None
    timestamp = _timestamp(record)
    user_ids = []
    primary = _string(record.get("user_message_uuid"))
    if primary:
        user_ids.append(primary)
    for value in (record.get("user_message_uuids") if isinstance(record.get("user_message_uuids"), list) else [])[:256]:
        if _string(value) and value not in user_ids:
            user_ids.append(value)
    # State is scoped to one JSONL file or one cached conversation snapshot.
    # Remote user session IDs differ from the SDK result's session ID; the
    # provider's explicit UUID link within that container supplies the identity.
    known_users = _mapping(state.get("user_timestamps"))
    starts = [known_users.get(user_id) for user_id in user_ids]
    verified = bool(starts) and all(isinstance(value, str) for value in starts)
    started_at = min(starts) if verified else None
    if started_at is not None and (timestamp is None or started_at > timestamp):
        started_at, verified = None, False
    parent = _string(record.get("parent_tool_use_id"))
    agent_id = _string(record.get("agentId"))
    sidechain = record.get("isSidechain") is True
    subagent = bool(parent or agent_id or sidechain)
    verified = verified and not subagent
    native = {
        "result_id": record_id,
        "source_kind": "desktop_audit" if "_audit_timestamp" in record or Path(source_path).name == "audit.jsonl" else "project_or_stream",
        "scope": "main_agent_run", "scope_verified": verified,
        "window_verified": verified, "model_usage_scope": "sdk_call_including_subagents",
    }
    if verified:
        native["boundary_method"] = "linked_user_message"
    if primary:
        native["user_message_uuid"] = primary
    if user_ids:
        native["user_message_uuids"] = user_ids
    if parent:
        native["parent_tool_use_id"] = parent
    if agent_id:
        native["agent_id"] = agent_id
    if isinstance(record.get("isSidechain"), bool):
        native["is_sidechain"] = sidechain
    for key in ("num_turns", "duration_ms", "duration_api_ms"):
        if count(record.get(key)) is not None:
            native[key] = count(record[key])
    for key in ("time_origin_ms", "request_sent_wall_ms"):
        value = record.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 8.64e15:
            native[key] = value
    if isinstance(record.get("is_error"), bool):
        native["is_error"] = record["is_error"]
    for key, allowed in (
        ("subtype", {"success", "error_during_execution", "error_max_turns", "error_max_budget_usd", "error_max_structured_output_retries"}),
        ("terminal_reason", {"completed", "interrupted", "aborted", "error", "max_turns", "max_budget_usd"}),
        ("stop_reason", _STOP_REASONS),
    ):
        if isinstance(record.get(key), str) and record[key] in allowed:
            native[key] = record[key]
    terminal = native.get("subtype") == "success" and native.get("terminal_reason") == "completed" and native.get("is_error") is False
    model_usage = {}
    for model, values in list(_mapping(record.get("modelUsage")).items())[:128]:
        if not _string(model) or not isinstance(values, dict):
            continue
        canonical = {}
        for raw, target in (
            ("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
            ("cacheReadInputTokens", "cache_read_input_tokens"),
            ("cacheCreationInputTokens", "cache_creation_input_tokens"),
        ):
            if raw in values:
                canonical[target] = count(values[raw])
        model_usage[model] = canonical
    flags = [] if verified else ["summary_window_unverified"]
    if subagent:
        flags.append("subagent_summary")
    if not terminal:
        flags.append("summary_completion_unverified")
    return UsageSummary(
        summary_key=stable_key("anthropic", "result", session, record_id),
        provider="anthropic", surface=_surface(record, surface, source_path),
        session_id=session or "unknown", timestamp=timestamp, started_at=started_at,
        usage=_usage_metadata(_mapping(record.get("usage"))), model_usage=model_usage,
        terminal=terminal, native=native, flags=flags,
    )


def _unwrap(record: dict) -> tuple[dict, dict]:
    """Follow only known structural wrappers, never recursively scan content.

    Older Code versions can embed a subagent's assistant record inside an
    agent_progress envelope. The envelope contributes session/agent metadata;
    user tool results and arbitrary nested JSON must not be interpreted as logs.
    """
    current = record
    context: dict = {}
    for _ in range(8):
        for key in _CONTEXT_FIELDS:
            if key in current:
                # Nested records cannot erase the enclosing agent provenance
                # with an empty ID or a false sidechain flag.
                if key == "isSidechain":
                    context[key] = context.get(key) is True or current[key] is True
                elif key != "agentId" or _string(current[key]) or not _string(context.get(key)):
                    context[key] = current[key]
        if current.get("type") == "progress":
            data = _mapping(current.get("data"))
            if data.get("type") != "agent_progress" or not isinstance(data.get("message"), dict):
                break
            if _string(data.get("agentId")):
                context["agentId"] = data["agentId"]
            context["isSidechain"] = True
            current = data["message"]
            continue
        if current.get("type") == "stream_event" and isinstance(current.get("event"), dict):
            current = current["event"]
            continue
        break
    return current, context


def parse_line(record: dict, state: dict, *, source_path: str, surface: str = "unknown") -> ParseResult:
    """Parse one complete JSONL record into idempotent usage observations.

    ``state`` contains only IDs, timestamps, labels and allowlisted token counts.
    It tracks concurrently active streams by session and parent tool/agent ID so
    a nested subagent cannot overwrite its parent's pending message identity.
    """
    result = ParseResult()
    if not isinstance(record, dict):
        return result
    inner, context = _unwrap(record)
    kind = inner.get("type")
    session = _string(context.get("sessionId")) or _string(context.get("session_id")) or _string(state.get("session_id"))
    if session:
        state["session_id"] = session
    if kind == "user":
        # Keep only structural UUID/timestamp evidence for explicit result links.
        # User text and tool-result payloads never enter parser state.
        user_id, timestamp = _string(inner.get("uuid")), _timestamp(context)
        if user_id and timestamp:
            users = state.setdefault("user_timestamps", {})
            if not isinstance(users, dict):
                users = state["user_timestamps"] = {}
            previous = users.get(user_id)
            users[user_id] = min(previous, timestamp) if isinstance(previous, str) else timestamp
            while len(users) > 256:
                users.pop(next(iter(users)))
        return result
    if kind == "rate_limit_event":
        snapshot = _quota({**context, **inner})
        if snapshot:
            result.snapshots.append(snapshot)
        return result
    if kind == "result":
        summary = _summary({**inner, **context}, state, session=session, source_path=source_path, surface=surface)
        if summary is None:
            result.warnings.append("Claude result summary without a stable result UUID was ignored")
        else:
            result.summaries.append(summary)
        return result

    # Task-notification usage overlaps assistant observations and remains outside
    # additive events. Result summaries above use their separate evidence store.
    if kind not in ("assistant", "message", "message_start", "message_delta", "message_stop"):
        return result
    parent_tool = _string(context.get("parent_tool_use_id"))
    agent_id = _string(context.get("agentId"))
    slot = stable_key(session, parent_tool or agent_id or "main")
    streams = state.setdefault("streams", {})
    if not isinstance(streams, dict):
        streams = state["streams"] = {}

    if kind in ("message_delta", "message_stop"):
        pending = streams.get(slot)
        if not isinstance(pending, dict):
            result.warnings.append("Claude stream usage without a preceding message_start was ignored")
            return result
        # Deltas contain cumulative counts. Overwrite known numeric components,
        # keeping message_start input when a delta only supplies output usage.
        usage = dict(pending["usage"])
        for key, value in _usage_metadata(_mapping(inner.get("usage"))).items():
            if value is not None:
                usage[key] = value
        final = kind == "message_stop" or bool(_mapping(inner.get("delta")).get("stop_reason"))
        final = final or pending.get("final", False)
        event = _event(pending["metadata"], usage, final=final, audit=pending["audit"])
        result.events.append(event)
        if kind == "message_stop":
            streams.pop(slot, None)
        else:
            pending.update(usage=usage, final=final)
        return result

    message = inner if kind == "message" else _mapping(inner.get("message"))
    if message.get("role") not in (None, "assistant"):
        return result
    if message.get("model") == "<synthetic>" or inner.get("isApiErrorMessage") is True:
        return result
    message_id = _string(message.get("id"))
    request_id = _string(context.get("requestId")) or _string(context.get("request_id")) or _string(message.get("request_id"))
    record_id = _string(inner.get("uuid"))
    if message_id:
        event_key = stable_key("anthropic", "message", message_id)
        identity_fallback = None
    elif request_id:
        event_key = stable_key("anthropic", "request", request_id)
        identity_fallback = "identity_from_request_id"
    elif record_id:
        event_key = stable_key("anthropic", "record", session, record_id)
        identity_fallback = "identity_from_record_uuid"
    else:
        # Paths, line numbers or usage values cannot deduplicate copied history.
        # Fail closed when there is no durable identity at all.
        result.warnings.append("Claude assistant usage without a stable message/request/record ID was ignored")
        return result
    metadata = {
        "event_key": event_key, "message_id": message_id, "request_id": request_id,
        "session_id": session or "unknown", "surface": _surface(context, surface, str(source_path)),
        "timestamp": _timestamp(context),
        "model": _string(message.get("model")) or "unknown",
        "agent_id": agent_id, "parent_tool_use_id": parent_tool,
        "is_subagent": bool(agent_id or parent_tool or context.get("isSidechain")),
        "identity_fallback": identity_fallback,
    }
    usage = _usage_metadata(_mapping(message.get("usage")))
    audit = "_audit_timestamp" in context or Path(source_path).name == "audit.jsonl"
    final = bool(message.get("stop_reason"))
    result.events.append(_event(metadata, usage, final=final, audit=audit))
    if kind == "message_start":
        streams[slot] = {"metadata": metadata, "usage": usage, "final": final, "audit": audit}
    return result
