"""Local-calendar reports that keep evidence classes and unknowns separate."""

from collections import defaultdict
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json
import os
import re

from usage_tracker.models import utc_timestamp
from usage_tracker.storage import Store, now

TOKEN_FIELDS = ("total_tokens", "input_tokens", "cached_input_tokens", "cache_creation_tokens", "output_tokens", "reasoning_tokens")
AGGREGATE_FIELDS = (*TOKEN_FIELDS, "uncached_input_tokens")


def local_timezone() -> str:
    candidates = [os.environ.get("TZ")]
    path = str(Path("/etc/localtime").resolve())
    if "/zoneinfo/" in path:
        candidates.append(path.split("/zoneinfo/", 1)[1])
    for candidate in candidates:
        if candidate:
            try:
                ZoneInfo(candidate)
                return candidate
            except (ZoneInfoNotFoundError, ValueError):
                pass
    return "UTC"


def _aggregate(rows: list[dict]) -> dict:
    # Derive ordinary input on each event, before summation. Subtracting the
    # independently known category totals would mix different sets of events
    # when some cache fields are missing and produce an invented input count.
    values = {field: [] for field in AGGREGATE_FIELDS}
    for row in rows:
        for field in TOKEN_FIELDS:
            values[field].append(row[field])
        components = [row[field] for field in ("input_tokens", "cached_input_tokens", "cache_creation_tokens")]
        uncached = None
        if all(value is not None and value >= 0 for value in components):
            remainder = components[0] - components[1] - components[2]
            if remainder >= 0:
                uncached = remainder
        values["uncached_input_tokens"].append(uncached)
    data = {}
    for field, counts in values.items():
        known = [value for value in counts if value is not None]
        data[field] = sum(known) if known else None
    data.update(events=len(rows), sessions=len({(r["provider"], r["session_id"]) for r in rows}),
                unknown_total_events=sum(r["total_tokens"] is None for r in rows),
                unknown_timestamp_events=sum(r["timestamp"] is None for r in rows),
                # These describe lifecycle/history evidence, not missing token
                # categories. An event may be both provisional and incomplete,
                # or have unknown fields despite otherwise complete history.
                provisional_events=sum("provisional_usage" in r["flags"] for r in rows),
                incomplete_events=sum("cache_partial_history" in r["flags"] for r in rows),
                summary_remainders=sum(r.get("method") == "claude_summary_remainder" for r in rows))
    data["unknown_fields"] = {field: sum(value is None for value in counts) for field, counts in values.items()}
    return data


def _bucket_start(day: date, group: str) -> date:
    """Return calendar bucket labels; weeks start on the local Monday."""
    if group == "week":
        return day - timedelta(days=day.weekday())
    if group == "month":
        return day.replace(day=1)
    return day


def _app_family(surface: str) -> str:
    """Merge coding surfaces while keeping regular chats and unknowns apart."""
    if surface.startswith("codex_"):
        return "codex"
    if surface in ("claude_code", "claude_desktop_agent"):
        return "claude"
    # Preserve an unfamiliar surface's identity instead of guessing from its
    # provider. In particular, imported chats must not become coding usage.
    return surface or "unknown"


def report(store: Store, *, days: int = 30, provider: str = "all", surface: str = "all",
           model: str = "all", group: str = "day", timezone_name: str | None = None,
           month: str | None = None, year: int | None = None,
           summary_limit: int = 25, summary_offset: int = 0) -> dict:
    if not 0 <= days <= 36600:
        raise ValueError("days must be 0 (all time) or between 1 and 36600")
    if group not in ("day", "week", "month"):
        raise ValueError("group must be day, week, or month")
    if month is not None and year is not None:
        raise ValueError("month and year are mutually exclusive")
    timezone_name = timezone_name or local_timezone()
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Unknown timezone; use an IANA name such as America/Los_Angeles") from exc
    today = datetime.now(zone).date()
    start = today - timedelta(days=days - 1) if days else None
    end = today
    calendar_period = month is not None or year is not None
    if month is not None:
        month = today.strftime("%Y-%m") if month == "current" else month
        try:
            if not isinstance(month, str) or not re.fullmatch(r"\d{4}-\d{2}", month):
                raise ValueError
            start = date.fromisoformat(f"{month}-01")
        except ValueError as exc:
            raise ValueError("month must be YYYY-MM or current") from exc
        end = start.replace(day=monthrange(start.year, start.month)[1])
    elif year is not None:
        if isinstance(year, bool) or not isinstance(year, int) or not 1 <= year <= 9999:
            raise ValueError("year must be an integer between 1 and 9999")
        start, end = date(year, 1, 1), date(year, 12, 31)
    if calendar_period:
        days = (end - start).days + 1

    period = {"days": days, "start": start.isoformat() if start else None,
              "end": end.isoformat(), "group": group}
    if month is not None:
        period.update(month=month, year=start.year)
    elif year is not None:
        period["year"] = year
    clauses, params = [], []
    for name, value in (("provider", provider), ("surface", surface), ("model", model)):
        if value != "all":
            clauses.append(f"{name}=?")
            params.append(value)
    start_time, end_time = None, None
    if start:
        boundary = datetime.combine(start, datetime.min.time(), zone)
        start_time = utc_timestamp(boundary.isoformat())
        clauses.append("timestamp>=?")
        params.append(start_time)
    # Calendar selectors retain their full month/year for chart layout, while
    # filtering observations at the earlier of the period end or local today.
    # All-time inspection retains future and undated evidence as before.
    if days:
        clauses.append("timestamp<?")
        cutoff = min(end, today) + timedelta(days=1)
        end_time = utc_timestamp(datetime.combine(cutoff, datetime.min.time(), zone).isoformat())
        params.append(end_time)
    query = "SELECT * FROM usage_events" + (" WHERE " + " AND ".join(clauses) if clauses else "")
    rows = [dict(row) for row in store.conn.execute(query, params)]
    # Parse flags once before the same event participates in several report
    # aggregates. No conversation content or raw source records leave SQLite.
    for row in rows:
        row["flags"] = frozenset(json.loads(row["flags"]))
    totals = {quality: _aggregate([r for r in rows if r["quality"] == quality]) for quality in ("reported", "estimated")}
    buckets = defaultdict(list)
    breakdown = defaultdict(list)
    daily_buckets = defaultdict(list)
    for row in rows:
        breakdown[(row["provider"], row["surface"], row["model"], row["quality"])].append(row)
        if row["timestamp"]:
            local_day = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).astimezone(zone).date()
            daily_buckets[(local_day.isoformat(), row["quality"])].append(row)
            buckets[_bucket_start(local_day, group).isoformat()].append(row)
    # Include empty days so an unobserved period is a gap, not an invented zero.
    if start:
        cursor = start
        while cursor <= end:
            buckets.setdefault(_bucket_start(cursor, group).isoformat(), [])
            if cursor == end:
                break
            cursor += timedelta(days=1)
    series = []
    for bucket_date, group_rows in sorted(buckets.items()):
        item = {"date": bucket_date}
        for quality in ("reported", "estimated"):
            agg = _aggregate([r for r in group_rows if r["quality"] == quality])
            item[quality] = agg
            item[f"{quality}_tokens"] = agg["total_tokens"]
            item[f"{quality}_events"] = agg["events"]
        series.append(item)
    # Daily rows always use local dates, independently of chart grouping. Each
    # evidence class has its own subtotal and disjoint app families, so a view
    # never needs to mix visible-text estimates with measured usage.
    daily = []
    for (day, quality), day_rows in sorted(daily_buckets.items()):
        apps = defaultdict(list)
        for row in day_rows:
            apps[_app_family(row["surface"])].append(row)
        daily.append({"date": day, "quality": quality, **_aggregate(day_rows),
                      "apps": [{"app": app, "models": sorted({row["model"] for row in app_rows}),
                                **_aggregate(app_rows)} for app, app_rows in sorted(apps.items())]})
    coverage = []
    # A fresh scan only establishes that collection ran. Show the newest usage
    # actually attributed to each source, independently of the chosen report
    # window. file_state owns normal collector paths; exact source_health paths
    # also cover imports and readers that attach provenance at their source root.
    # The UNION deduplicates paths without guessing ownership from path prefixes.
    latest_usage = dict(store.conn.execute("""
        WITH source_paths AS (
            SELECT source, path FROM file_state
            UNION SELECT source, path FROM source_health
        )
        SELECT paths.source, MAX(events.timestamp)
        FROM source_paths paths
        JOIN event_sources provenance ON provenance.source_path=paths.path
        JOIN usage_events events ON events.event_key=provenance.event_key
        GROUP BY paths.source
    """))
    summary_coverage = {row["source"]: dict(row) for row in store.conn.execute("""
        SELECT fs.source, COUNT(DISTINCT r.summary_key) AS summaries, MAX(r.timestamp) AS latest_summary
        FROM file_state fs JOIN claude_summary_sources provenance ON provenance.source_path=fs.path
        JOIN claude_summary_revisions r ON r.revision_id=provenance.revision_id GROUP BY fs.source
    """)}
    quota_coverage = {row["source"]: dict(row) for row in store.conn.execute("""
        SELECT fs.source, COUNT(DISTINCT s.snapshot_key) AS quota_observations, MAX(s.timestamp) AS latest_quota
        FROM file_state fs JOIN snapshot_sources provenance ON provenance.source_path=fs.path
        JOIN snapshots s ON s.snapshot_key=provenance.snapshot_key WHERE s.kind='quota' GROUP BY fs.source
    """)}
    for row in store.conn.execute("SELECT * FROM source_health ORDER BY source"):
        item = dict(row)
        item["warnings"] = json.loads(item["warnings"])
        item["latest_usage"] = latest_usage.get(item["source"])
        item.update({key: value for key, value in summary_coverage.get(item["source"], {}).items() if key != "source"})
        item.update({key: value for key, value in quota_coverage.get(item["source"], {}).items() if key != "source"})
        coverage.append(item)
    # Quotas and account summaries are selected independently and never added to
    # session totals. Keep one latest observation per provider/kind/quota bucket.
    snapshots, snapshot_keys = [], set()
    for row in store.conn.execute("SELECT * FROM snapshots ORDER BY timestamp DESC"):
        item = dict(row)
        item["data"] = json.loads(item["data"])
        key = (item["provider"], item["kind"], item["data"].get("limit_id"))
        if key not in snapshot_keys:
            snapshots.append(item)
            snapshot_keys.add(key)
        if len(snapshots) >= 20:
            break
    undated = store.conn.execute("SELECT COUNT(*) FROM usage_events WHERE timestamp IS NULL").fetchone()[0]
    # The ledger is evidence about existing requests, not another additive
    # series. Only the reconciler's explicit remainder events enter rows above.
    from usage_tracker.claude_reconcile import read_reconciliations
    reconciliation = read_reconciliations(store, start=start_time, end=end_time,
                                         provider=provider, surface=surface, model=model,
                                         limit=summary_limit, offset=summary_offset)
    for item in reconciliation["items"]:
        item["revisions"] = summary_revisions(store, item["summary_key"], limit=10)
    options = {plural: {row[0] for row in store.conn.execute(f"SELECT DISTINCT {singular} FROM usage_events")}
               for plural, singular in (("providers", "provider"), ("surfaces", "surface"), ("models", "model"))}
    # A model may appear only in a broader saved summary. Keep it selectable
    # even though its unresolved counters do not contribute to additive totals.
    for row in store.conn.execute("SELECT DISTINCT provider,surface FROM claude_summary_revisions"):
        options["providers"].add(row[0]); options["surfaces"].add(row[1])
    options["models"].update(row[0] for row in store.conn.execute("SELECT DISTINCT model.key FROM claude_summary_revisions r,json_each(r.model_usage) model"))
    return {"generated_at": now(), "timezone": timezone_name,
            "period": period,
            "totals": totals, "series": series, "daily": daily,
            "breakdown": sorted([dict(zip(("provider", "surface", "model", "quality"), key), **_aggregate(value))
                                  for key, value in breakdown.items()], key=lambda x: x["total_tokens"] or 0, reverse=True),
            "options": {key: sorted(values) for key, values in options.items()},
            "coverage": coverage, "snapshots": snapshots,
            "reconciliation": reconciliation, "quota_history": quota_history(store, provider=provider),
            "notes": ["Reported tokens are local telemetry, not complete account billing.",
                      "Stored inclusive input contains cache reads and writes; the daily table and chart show uncached input separately. Output includes reasoning.",
                      "Uncached input is derived per event only when inclusive input, cache reads, and cache creation are known and consistent.",
                      "Estimates count exported visible text only; context, hidden reasoning, tools, and media are not reconstructed.",
                      "Only verified, nonoverlapping main-turn summary remainders enter these totals; they shrink as message evidence arrives.",
                      "Unresolved run summaries, broader per-model call totals, and quota percentages remain separate evidence.",
                      f"{undated} event(s) have unknown dates; they appear only in all-time totals.",
                      "Missing sources and token categories mean unknown, not zero. Different tokenizers measure different units."]}


def quota_history(store: Store, *, provider: str = "all", limit: int = 25, offset: int = 0) -> dict:
    """Page retained source observations, not repeated copies of today's gauge.

    The source timestamp answers when Claude recorded a percentage. Capture
    timestamps answer when our service first/last encountered that evidence;
    rescanning an unchanged cache cannot make an old quota observation current.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    condition, params = "s.kind='quota'", []
    if provider != "all":
        condition += " AND s.provider=?"
        params.append(provider)
    total = store.conn.execute(f"SELECT COUNT(*) FROM snapshots s WHERE {condition}", params).fetchone()[0]
    rows = store.conn.execute(f"""SELECT s.*, o.first_seen, o.last_seen FROM snapshots s
        LEFT JOIN snapshot_observations o ON o.snapshot_key=s.snapshot_key
        WHERE {condition} ORDER BY s.timestamp DESC, s.snapshot_key DESC LIMIT ? OFFSET ?""",
                              [*params, limit, offset]).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["data"] = json.loads(item["data"])
        item["source_paths"] = [value[0] for value in store.conn.execute(
            "SELECT source_path FROM snapshot_sources WHERE snapshot_key=? ORDER BY source_path", (item["snapshot_key"],))]
        items.append(item)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def summary_revisions(store: Store, summary_key: str, *, limit: int = 50, offset: int = 0) -> dict:
    """Inspect immutable versions without adding them together or changing heads."""
    if not isinstance(summary_key, str) or not summary_key or len(summary_key) > 256:
        raise ValueError("A summary key is required")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    total = store.conn.execute("SELECT COUNT(*) FROM claude_summary_revisions WHERE summary_key=?", (summary_key,)).fetchone()[0]
    items = []
    for row in store.conn.execute("""SELECT * FROM claude_summary_revisions WHERE summary_key=?
        ORDER BY first_seen DESC,revision_id DESC LIMIT ? OFFSET ?""", (summary_key, limit, offset)):
        item = dict(row)
        for field in ("usage", "model_usage", "native", "flags"):
            item[field] = json.loads(item[field])
        item["terminal"] = bool(item["terminal"])
        item["source_paths"] = [value[0] for value in store.conn.execute(
            "SELECT source_path FROM claude_summary_sources WHERE revision_id=? ORDER BY source_path", (item["revision_id"],))]
        items.append(item)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def format_report(data: dict) -> str:
    def number(value):
        return f"{value:,}" if value is not None else "unknown"
    lines = [f"Usage Tracker · {data['timezone']} · {data['period']['start'] or 'all time'} → {data['period']['end']}", ""]
    for quality in ("reported", "estimated"):
        values = data["totals"][quality]
        lines.append(f"{quality.title():10} {number(values['total_tokens']):>16} tokens   {values['events']:,} events / {values['sessions']:,} sessions")
        lines.append(f"  Input {number(values['input_tokens'])}; output {number(values['output_tokens'])}; unknown totals {values['unknown_total_events']}")
        if values.get("provisional_events") or values.get("incomplete_events"):
            lines.append(f"  Observed records: {values.get('provisional_events', 0)} provisional; "
                         f"{values.get('incomplete_events', 0)} from partial cached history. Counts may be unfinished and history incomplete.")
    lines.extend(["", f"{'Period':<12} {'Reported':>16} {'Visible estimate':>18}"])
    for row in data["series"]:
        lines.append(f"{row['date']:<12} {number(row['reported_tokens']):>16} {number(row['estimated_tokens']):>18}")
    lines.append("\nProvider / app / model / evidence")
    for row in data["breakdown"]:
        lines.append(f"  {row['provider']} / {row['surface']} / {row['model']} / {row['quality']}: {number(row['total_tokens'])}")
    lines.append("\nSource coverage")
    for row in data["coverage"]:
        lines.append(f"  {row['source']}: {row['status']} · {row['files']} files · {row['events']} events · "
                     f"last scan {row['last_success'] or 'never'} · latest usage {row.get('latest_usage') or 'unknown'}")
    if data.get("reconciliation", {}).get("total"):
        ledger = data["reconciliation"]
        lines.append(f"\nPersisted Claude summaries: {ledger['total']} (showing {len(ledger['items'])})")
        for item in ledger["items"]:
            lines.append(f"  {item['timestamp'] or 'undated'} · {item['status']} · {item['reason']}")
    lines.extend(["", *data["notes"]])
    return "\n".join(lines)
