"""SQLite metadata store with atomic ingestion checkpoints and idempotent events."""

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3

from usage_tracker.models import ParseResult, UsageEvent


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.data_dir / "usage.sqlite3"
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.path.chmod(0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS usage_events (
                event_key TEXT PRIMARY KEY, provider TEXT NOT NULL, surface TEXT NOT NULL,
                session_id TEXT NOT NULL, timestamp TEXT, model TEXT NOT NULL,
                request_id TEXT, quality TEXT NOT NULL, method TEXT NOT NULL,
                input_tokens INTEGER, cached_input_tokens INTEGER, cache_creation_tokens INTEGER,
                output_tokens INTEGER, reasoning_tokens INTEGER, total_tokens INTEGER,
                native TEXT NOT NULL, flags TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS usage_time ON usage_events(timestamp);
            CREATE INDEX IF NOT EXISTS usage_dimensions ON usage_events(provider,surface,model,quality);
            CREATE TABLE IF NOT EXISTS event_sources (
                event_key TEXT NOT NULL REFERENCES usage_events(event_key),
                source_path TEXT NOT NULL, PRIMARY KEY(event_key,source_path)
            );
            CREATE TABLE IF NOT EXISTS codex_observations (
                event_key TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                observed_at TEXT, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS codex_session_time ON codex_observations(session_id,observed_at);
            CREATE TABLE IF NOT EXISTS codex_observation_sources (
                event_key TEXT NOT NULL REFERENCES codex_observations(event_key),
                source_path TEXT NOT NULL, PRIMARY KEY(event_key,source_path)
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_key TEXT PRIMARY KEY, provider TEXT NOT NULL, kind TEXT NOT NULL,
                timestamp TEXT, data TEXT NOT NULL, scope TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS snapshot_observations (
                snapshot_key TEXT PRIMARY KEY REFERENCES snapshots(snapshot_key),
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS snapshot_sources (
                snapshot_key TEXT NOT NULL REFERENCES snapshots(snapshot_key),
                source_path TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                PRIMARY KEY(snapshot_key,source_path)
            );
            CREATE TABLE IF NOT EXISTS claude_summary_revisions (
                revision_id TEXT PRIMARY KEY, summary_key TEXT NOT NULL,
                provider TEXT NOT NULL, surface TEXT NOT NULL, session_id TEXT NOT NULL,
                timestamp TEXT, started_at TEXT, terminal INTEGER NOT NULL,
                usage TEXT NOT NULL, model_usage TEXT NOT NULL, native TEXT NOT NULL, flags TEXT NOT NULL,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS claude_summary_identity ON claude_summary_revisions(summary_key);
            CREATE INDEX IF NOT EXISTS claude_summary_session ON claude_summary_revisions(session_id);
            CREATE TABLE IF NOT EXISTS claude_summary_sources (
                observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                revision_id TEXT NOT NULL REFERENCES claude_summary_revisions(revision_id),
                source_path TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                UNIQUE(revision_id,source_path)
            );
            CREATE TABLE IF NOT EXISTS claude_reconciliations (
                summary_key TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES claude_summary_revisions(revision_id),
                status TEXT NOT NULL, detail TEXT NOT NULL, matched_events INTEGER NOT NULL,
                observed_usage TEXT NOT NULL, summary_usage TEXT NOT NULL, remainder TEXT NOT NULL,
                metadata TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS file_state (
                path TEXT PRIMARY KEY, source TEXT NOT NULL, checkpoint TEXT NOT NULL,
                last_success TEXT NOT NULL, warnings TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_health (
                source TEXT PRIMARY KEY, path TEXT NOT NULL, status TEXT NOT NULL,
                files INTEGER NOT NULL, events INTEGER NOT NULL, last_success TEXT,
                error TEXT, warnings TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            PRAGMA user_version=2;
        """)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def checkpoint(self, path: str) -> dict | None:
        row = self.conn.execute("SELECT checkpoint FROM file_state WHERE path=?", (path,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_checkpoint(self, path: str, source: str, checkpoint: dict, warnings: dict):
        self.conn.execute("""INSERT INTO file_state VALUES (?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
            source=excluded.source,checkpoint=excluded.checkpoint,last_success=excluded.last_success,
            warnings=excluded.warnings""", (path, source, json.dumps(checkpoint), now(), json.dumps(warnings)))

    def ingest(self, result: ParseResult, source_path: str | None, *, derived: bool = False) -> int:
        """Keep Claude messages, summary evidence, and derived remainders atomic.

        A savepoint protects callers that catch an ingestion error inside a
        larger transaction. Explicit BEGIN keeps releasing that savepoint from
        committing a transaction owned by the caller. Codex's existing path and
        separate reconciliation contract remain unchanged.
        """
        claude_evidence = not derived and (result.summaries or any(
            event.provider == "anthropic" and event.quality == "reported" for event in result.events))
        if not claude_evidence:
            return self._ingest(result, source_path, derived=derived)
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN")
        self.conn.execute("SAVEPOINT claude_ingest")
        try:
            changed = self._ingest(result, source_path, derived=derived)
            self.conn.execute("RELEASE SAVEPOINT claude_ingest")
            return changed
        except Exception:
            self.conn.execute("ROLLBACK TO SAVEPOINT claude_ingest")
            self.conn.execute("RELEASE SAVEPOINT claude_ingest")
            raise

    def _ingest(self, result: ParseResult, source_path: str | None, *, derived: bool = False) -> int:
        """Merge repeated message updates; callers control the enclosing transaction.

        Anthropic observations merge each known usage component independently.
        A final count outranks a provisional count even when smaller; at equal
        priority cumulative counts take maxima. Missing evidence never becomes
        zero and never erases an earlier known count. Other providers and text
        estimates keep replacement semantics so corrections/edits can decrease.
        """
        def token(value):
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

        def priority(value):
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        def claude_components(values, native):
            # Native Anthropic input is ordinary (uncached) input. The normalized
            # input column is inclusive, so it must never be merged into that
            # native component without subtracting two explicitly known subsets.
            fields = {key: token(native.get(key)) for key in (
                "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
                "output_tokens", "thinking_tokens", "ephemeral_5m_input_tokens",
                "ephemeral_1h_input_tokens",
            )}
            for native_key, column in (
                ("cache_read_input_tokens", "cached_input_tokens"),
                ("cache_creation_input_tokens", "cache_creation_tokens"),
                ("output_tokens", "output_tokens"), ("thinking_tokens", "reasoning_tokens"),
            ):
                if fields[native_key] is None:
                    fields[native_key] = token(values.get(column))
            fields["inclusive_input_tokens"] = token(values.get("input_tokens"))
            if fields["input_tokens"] is None and all(fields[key] is not None for key in (
                "inclusive_input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
            )):
                ordinary = fields["inclusive_input_tokens"] - fields["cache_read_input_tokens"] - fields["cache_creation_input_tokens"]
                fields["input_tokens"] = ordinary if ordinary >= 0 else None
            return fields

        changed = 0
        claude_sessions = set()
        for event in result.events:
            payload = asdict(event)
            if not derived and event.provider == "openai" and "cumulative_usage" in event.native:
                # A cumulative snapshot is evidence, not an additive event. Keep
                # raw metadata so later arrival of a full log can correct a
                # fragment's baseline, regardless of ingestion/read order.
                from usage_tracker.codex_reconcile import merge_observation_payload
                old_observation = self.conn.execute("SELECT payload FROM codex_observations WHERE event_key=?", (event.event_key,)).fetchone()
                if old_observation:
                    payload = merge_observation_payload(json.loads(old_observation[0]), payload)
                encoded = json.dumps(payload, sort_keys=True)
                if old_observation is None or encoded != old_observation[0]:
                    self.conn.execute("""INSERT INTO codex_observations VALUES (?,?,?,?) ON CONFLICT(event_key)
                        DO UPDATE SET payload=excluded.payload,observed_at=excluded.observed_at""",
                                      (event.event_key, event.session_id, payload["native"].get("observed_at"), encoded))
                    self.set_setting("codex_dirty", True)
                if source_path is not None:
                    inserted = self.conn.execute("INSERT OR IGNORE INTO codex_observation_sources VALUES (?,?)", (event.event_key, source_path)).rowcount
                    if inserted:
                        self.set_setting("codex_dirty", True)
                continue
            old = self.conn.execute("SELECT * FROM usage_events WHERE event_key=?", (event.event_key,)).fetchone()
            if event.provider == "anthropic" and event.quality == "reported" and not derived and (
                "usage_priority" in event.native or event.method == "local_log" and any(
                    key in event.native for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
                )
            ):
                previous = dict(old) if old else {}
                previous_native = json.loads(previous["native"]) if old else {}
                old_priority = priority(previous_native.get("usage_priority"))
                new_priority = priority(event.native.get("usage_priority"))
                previous_fields = claude_components(previous, previous_native)
                incoming_fields = claude_components(payload, event.native)
                previous_priorities = previous_native.get("usage_field_priorities", {})
                if not isinstance(previous_priorities, dict):
                    previous_priorities = {}
                merged_fields, field_priorities = {}, {}
                for field, incoming in incoming_fields.items():
                    existing = previous_fields[field]
                    existing_priority = priority(previous_priorities.get(field, old_priority))
                    if incoming is None:
                        value, field_priority = existing, existing_priority
                    elif existing is None or new_priority > existing_priority:
                        value, field_priority = incoming, new_priority
                    elif new_priority < existing_priority:
                        value, field_priority = existing, existing_priority
                    else:
                        value, field_priority = max(existing, incoming), new_priority
                    merged_fields[field] = value
                    if value is not None:
                        field_priorities[field] = field_priority

                # Keep provenance of EACH component: a sparse final output must
                # not promote inherited provisional input to final confidence.
                # Future provisional input may then improve that inherited input
                # while the final output remains authoritative.
                native = dict(previous_native)
                for key, value in event.native.items():
                    if key not in native or native[key] is None or new_priority > old_priority:
                        native[key] = value if value is not None else native.get(key)
                    elif new_priority == old_priority and isinstance(value, str) and isinstance(native[key], str):
                        # Equivalent observations can carry different source or
                        # subagent labels. Resolve ties deterministically so a
                        # full history replay does not oscillate metadata.
                        native[key] = min(native[key], value)
                for field, value in merged_fields.items():
                    if field != "inclusive_input_tokens" and (value is not None or field in native):
                        native[field] = value
                native["usage_priority"] = max(old_priority, new_priority)
                native["usage_is_final"] = bool(previous_native.get("usage_is_final") or event.native.get("usage_is_final"))
                native["usage_field_priorities"] = field_priorities
                payload["native"] = native

                # Derive normalized totals from the merged native components,
                # not from competing precomputed totals. This matters when one
                # record supplies cache reads and another supplies final output.
                input_parts = [merged_fields[key] for key in (
                    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
                )]
                inclusive = sum(input_parts) if all(value is not None for value in input_parts) else merged_fields["inclusive_input_tokens"]
                payload.update(
                    input_tokens=inclusive,
                    cached_input_tokens=merged_fields["cache_read_input_tokens"],
                    cache_creation_tokens=merged_fields["cache_creation_input_tokens"],
                    output_tokens=merged_fields["output_tokens"],
                    reasoning_tokens=merged_fields["thinking_tokens"],
                )
                output = payload["output_tokens"]
                payload["total_tokens"] = inclusive + output if inclusive is not None and output is not None else None

                # Retain useful labels from the strongest observation. Desktop
                # attribution is sticky because its duplicate project records
                # can otherwise look like ordinary CLI records during discovery.
                if old:
                    if new_priority < old_priority:
                        for key in ("model", "session_id", "request_id", "timestamp", "surface", "method"):
                            if previous.get(key) not in (None, "unknown"):
                                payload[key] = previous[key]
                    elif new_priority == old_priority:
                        for key in ("model", "session_id", "request_id", "surface", "method"):
                            known = [value for value in (previous.get(key), payload.get(key)) if value not in (None, "unknown")]
                            if known:
                                payload[key] = min(known)
                    if "claude_desktop_agent" in (previous["surface"], event.surface):
                        payload["surface"] = "claude_desktop_agent"
                    # All observations describe one request. Its earliest known
                    # timestamp is stable across content blocks and captures the
                    # request's start day even when the final block comes later.
                    known_times = [value for value in (previous["timestamp"], event.timestamp) if value is not None]
                    if known_times:
                        payload["timestamp"] = min(known_times)

                # Recompute state-dependent warnings after enrichment. A sparse
                # final should not leave a false "missing input" or provisional
                # warning when the other source already supplied that evidence.
                flags = set(json.loads(previous["flags"]) if old else ()) | set(event.flags)
                flags.difference_update({"provisional_usage", "incomplete_input_usage", "missing_output_usage",
                                         "inconsistent_reasoning_subset", "inconsistent_cache_creation_breakdown"})
                if not native["usage_is_final"]:
                    flags.add("provisional_usage")
                if inclusive is None:
                    flags.add("incomplete_input_usage")
                if output is None:
                    flags.add("missing_output_usage")
                if payload["reasoning_tokens"] is not None and output is not None and payload["reasoning_tokens"] > output:
                    flags.add("inconsistent_reasoning_subset")
                ttl_parts = [merged_fields[key] for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")]
                if payload["cache_creation_tokens"] is not None and all(value is not None for value in ttl_parts) and payload["cache_creation_tokens"] != sum(ttl_parts):
                    flags.add("inconsistent_cache_creation_breakdown")
                payload["flags"] = sorted(flags)

            if old and not derived:
                # Missing identity metadata in later fragments must not erase
                # attribution already supported by a provider transcript.
                for key in ("model", "surface", "session_id"):
                    if payload[key] == "unknown" and old[key] != "unknown":
                        payload[key] = old[key]
                for key in ("timestamp", "request_id"):
                    if payload[key] is None:
                        payload[key] = old[key]
            payload["native"] = json.dumps(payload["native"], sort_keys=True)
            payload["flags"] = json.dumps(payload["flags"], sort_keys=True)
            if old is None or any(payload[k] != old[k] for k in payload):
                payload["updated_at"] = now()
                columns = list(payload)
                self.conn.execute(f"INSERT INTO usage_events ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                                  f"ON CONFLICT(event_key) DO UPDATE SET {','.join(k+'=excluded.'+k for k in columns if k != 'event_key')}",
                                  list(payload.values()))
                changed += 1
                if not derived and event.provider == "anthropic" and event.quality == "reported":
                    claude_sessions.add(payload["session_id"])
                    if old:
                        claude_sessions.add(old["session_id"])
            if source_path is not None:
                self.conn.execute("INSERT OR IGNORE INTO event_sources VALUES (?,?)", (event.event_key, source_path))
        for snapshot in result.snapshots:
            self.conn.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?)",
                              (snapshot.snapshot_key, snapshot.provider, snapshot.kind, snapshot.timestamp,
                               json.dumps(snapshot.data, sort_keys=True), snapshot.scope))
            seen_at = now()
            self.conn.execute("""INSERT INTO snapshot_observations VALUES (?,?,?)
                ON CONFLICT(snapshot_key) DO UPDATE SET last_seen=excluded.last_seen""",
                              (snapshot.snapshot_key, seen_at, seen_at))
            if source_path is not None:
                self.conn.execute("""INSERT INTO snapshot_sources VALUES (?,?,?,?)
                    ON CONFLICT(snapshot_key,source_path) DO UPDATE SET last_seen=excluded.last_seen""",
                                  (snapshot.snapshot_key, source_path, seen_at, seen_at))
        if not derived:
            from usage_tracker.claude_reconcile import persist_summary
            for summary in result.summaries:
                claude_sessions.update(persist_summary(self, summary, source_path))
            if claude_sessions:
                # No nested context manager: message updates and the shrunken
                # remainder must be committed or rolled back together.
                changed += self.reconcile_claude(claude_sessions)
        return changed

    def reconcile_claude(self, sessions: set[str] | None = None) -> int:
        """Recompute Claude remainder events without committing caller work."""
        from usage_tracker.claude_reconcile import reconcile
        return reconcile(self, sessions)

    def reconcile_codex(self) -> int:
        """Atomically refresh derived deltas from every known Codex observation.

        Source checkpoints and the dirty marker commit with raw observations.
        After a crash before this step, the next scan still reconciles everything.
        Reports see either the previous complete derivation or the new one.
        """
        if not self.get_setting("codex_dirty", False):
            return 0
        from usage_tracker.codex_reconcile import reconciled_events
        sessions = {row[0] for row in self.conn.execute("SELECT DISTINCT session_id FROM codex_observations")}
        events = reconciled_events(self, sessions)
        with self.conn:
            keys = {event.event_key for event in events}
            previous_keys = {row[0] for row in self.conn.execute("SELECT event_key FROM usage_events WHERE provider='openai' AND method LIKE 'codex_%'")}
            for key in previous_keys - keys:
                self.conn.execute("DELETE FROM event_sources WHERE event_key=?", (key,))
                self.conn.execute("DELETE FROM usage_events WHERE event_key=?", (key,))
            changed = len(previous_keys - keys) + self.ingest(ParseResult(events=events), None, derived=True)
            self.conn.execute("""INSERT OR IGNORE INTO event_sources
                SELECT cos.event_key,cos.source_path FROM codex_observation_sources cos
                JOIN usage_events u ON u.event_key=cos.event_key""")
            self.set_setting("codex_dirty", False)
        return changed

    def health(self, *, source: str, path: str, status: str, files: int, events: int,
               last_success: str | None, error: str | None = None, warnings: list | None = None):
        self.conn.execute("""INSERT INTO source_health VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET
            path=excluded.path,status=excluded.status,files=excluded.files,events=excluded.events,
            last_success=COALESCE(excluded.last_success,source_health.last_success),error=excluded.error,
            warnings=excluded.warnings""", (source, path, status, files, events, last_success, error, json.dumps(warnings or [])))

    def set_setting(self, key: str, value):
        self.conn.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def get_setting(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default
