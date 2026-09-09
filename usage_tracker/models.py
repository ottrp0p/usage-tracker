"""Shared metadata-only contract between adapters, importers, and SQLite.

All timestamps are normalized to UTC before storage. Nullable counts represent
missing evidence, not zero. Native token fields may be retained, but arbitrary
provider payloads (which can include conversation text) must not be persisted.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json


def stable_key(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def utc_timestamp(value: object) -> str | None:
    """Normalize ISO or Unix timestamps; unavailable/bad dates remain unknown."""
    try:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def count(value: object) -> int | None:
    """Reject booleans, negatives, floats, and numeric strings in reported usage."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@dataclass
class UsageEvent:
    event_key: str
    provider: str
    surface: str
    session_id: str
    timestamp: str | None
    model: str = "unknown"
    request_id: str | None = None
    quality: str = "reported"  # reported | estimated; missing evidence is source health
    method: str = "local_log"
    input_tokens: int | None = None  # inclusive of cached and cache-creation input
    cached_input_tokens: int | None = None  # subset of input_tokens
    cache_creation_tokens: int | None = None  # subset of input_tokens
    output_tokens: int | None = None  # inclusive of reasoning
    reasoning_tokens: int | None = None  # subset of output_tokens
    total_tokens: int | None = None
    native: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


@dataclass
class Snapshot:
    snapshot_key: str
    provider: str
    kind: str  # quota | account_summary
    timestamp: str | None
    data: dict
    scope: str = "account_scope_unverified"


@dataclass
class UsageSummary:
    """A provider run summary is separate evidence, never a usage increment.

    ``usage`` contains native main-agent counters; ``model_usage`` can cover a
    broader SDK call. A timestamp or process origin alone does not establish a
    run window. Reconciliation requires explicit, adapter-verified scope and
    linked boundary evidence in ``native`` before adding any remainder.
    """

    summary_key: str
    provider: str
    surface: str
    session_id: str
    timestamp: str | None
    started_at: str | None
    usage: dict
    model_usage: dict = field(default_factory=dict)
    terminal: bool = False
    native: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


@dataclass
class ParseResult:
    events: list[UsageEvent] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summaries: list[UsageSummary] = field(default_factory=list)
