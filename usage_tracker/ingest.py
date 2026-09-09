"""Crash-safe JSONL/cache readers and explicit source coverage inventory."""

from collections import Counter
from pathlib import Path
import hashlib
import json
import os
import fcntl
import re

from usage_tracker.collectors import codex
from usage_tracker.config import Source
from usage_tracker.storage import Store, now


def _digest(f, start: int, length: int) -> str:
    position = f.tell()
    f.seek(start)
    value = hashlib.sha256(f.read(length)).hexdigest()
    f.seek(position)
    return value


def collect_file(store: Store, source: Source, path: Path) -> tuple[int, dict]:
    from usage_tracker.collectors import claude
    parser = codex.parse_line if source.kind == "codex" else claude.parse_line
    # Claude v2 adds summaries/quotas; Codex v2 fills creation dates omitted from
    # rewritten metadata. Replay once so saved offsets cannot hide new evidence
    # needed to distinguish inherited histories from a child's own requests.
    adapter_version = 2
    path_text = str(path.resolve())
    checkpoint = store.checkpoint(path_text) or {}
    warnings = Counter(checkpoint.get("warnings", {}))
    changed = 0
    with path.open("rb") as f:
        stat = os.fstat(f.fileno())
        identity = [stat.st_dev, stat.st_ino]
        offset = checkpoint.get("offset", 0)
        # Check both the initial prefix and bytes before the saved offset. This
        # catches ordinary truncation, replacement, and in-place rewrite, even
        # when a rewritten file happens to be at least as long as before.
        valid = (checkpoint.get("adapter_version", 1) == adapter_version and
                 checkpoint.get("identity") == identity and stat.st_size >= offset)
        if valid and offset:
            valid = (_digest(f, 0, checkpoint["prefix_length"]) == checkpoint["prefix_hash"] and
                     _digest(f, max(0, offset - 256), min(offset, 256)) == checkpoint["tail_hash"])
        if not valid:
            offset = 0
            checkpoint = {}
            warnings = Counter()
        state = checkpoint.get("state", {})
        f.seek(offset)
        # Events and offsets are committed together. A crash can cause rereads,
        # but cannot commit an offset that skips uncommitted usage records.
        with store.conn:
            while True:
                start = f.tell()
                line = f.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    f.seek(start)
                    break  # A writer may complete this exact line next poll.
                offset = f.tell()
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        warnings["non_object_json"] += 1
                        continue
                    result = parser(record, state, source_path=path_text, surface=source.surface)
                    changed += store.ingest(result, path_text)
                    warnings.update(result.warnings)
                except (UnicodeError, ValueError, TypeError, KeyError, AttributeError) as exc:
                    # Never record exception messages or the offending raw line:
                    # both can contain conversation bodies or sensitive paths.
                    warnings[f"invalid_record_{type(exc).__name__}"] += 1
            prefix_length = min(offset, 4096)
            checkpoint = {"adapter_version": adapter_version, "offset": offset, "identity": identity, "state": state,
                          "prefix_length": prefix_length, "prefix_hash": _digest(f, 0, prefix_length),
                          "tail_hash": _digest(f, max(0, offset - 256), min(offset, 256)),
                          "warnings": dict(warnings), "pending_bytes": max(0, stat.st_size - offset)}
            store.save_checkpoint(path_text, source.name, checkpoint, dict(warnings))
    return changed, dict(warnings)


def _cache_digest(path: Path) -> str:
    """Hash every byte: Chromium replaces snapshots rather than appending logs.

    In particular, a same-length edit in the middle must not be skipped. Read
    in chunks so an unexpectedly large cache file does not grow Python memory.
    The decoder separately bounds what it will deserialize.
    """
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def collect_cache_file(store: Store, source: Source, path: Path) -> tuple[int, dict]:
    from usage_tracker.collectors.claude_cache import parse_cache

    path_text = str(path.resolve())
    checkpoint = store.checkpoint(path_text) or {}
    digest = _cache_digest(path)
    if checkpoint.get("cache_adapter_version") == 2 and checkpoint.get("content_hash") == digest:
        # File scan freshness is separate from evidence capture time. Historical
        # records may have disappeared from a rewritten cache, so a fingerprint
        # match must not mark every record ever seen in this file as recaptured.
        # Preserve warnings: rereading a cache does not restore its missing past.
        warnings = checkpoint.get("warnings", {})
        with store.conn:
            store.save_checkpoint(path_text, source.name, checkpoint, warnings)
        return 0, warnings

    result = parse_cache(path)
    if _cache_digest(path) != digest:
        # Claude may be updating the cache while it is read. Never commit a
        # checkpoint for a snapshot different from the one actually decoded.
        return 0, {"Cache changed during collection; retrying next scan": 1}
    warnings = dict(Counter(result.warnings))
    checkpoint = {"cache_adapter_version": 2, "content_hash": digest, "warnings": warnings}
    with store.conn:
        changed = store.ingest(result, path_text)
        store.save_checkpoint(path_text, source.name, checkpoint, warnings)
    return changed, warnings


def collect(store: Store, sources: list[Source]) -> dict:
    # launchd and an explicit CLI refresh may run concurrently. Serialize readers
    # across processes so both cannot derive increments from the same old offset.
    # OS advisory locks are automatically released when a process crashes.
    with (store.data_dir / "collector.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return _collect(store, sources)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _collect(store: Store, sources: list[Source]) -> dict:
    started = now()
    with store.conn:
        store.set_setting("collection_status", "collecting")
        current_sources = {source.name for source in sources}
        for row in store.conn.execute("SELECT source FROM source_health WHERE source NOT LIKE 'Import:%'").fetchall():
            if row[0] not in current_sources:
                store.conn.execute("UPDATE source_health SET status='inactive',warnings=? WHERE source=?",
                                   (json.dumps(["Source no longer configured; historical usage is retained."]), row[0]))
    changed = 0
    total_files = 0
    errors = 0
    seen = set()
    for source in sources:
        files = []
        warnings = Counter()
        source_errors = 0
        try:
            if not source.path.exists():
                with store.conn:
                    store.health(source=source.name, path=str(source.path), status="missing", files=0,
                                 events=0, last_success=None, warnings=["Source directory is not present."])
                continue
            if source.path.is_file():
                files = [source.path]
            elif source.kind == "claude_cache":
                # Chromium stores large values as hex-named blobs, smaller
                # values in LevelDB tables/write-ahead logs. Read only those
                # data files; never open a live database or acquire its lock.
                files = sorted(path for path in source.path.rglob("*")
                               if path.is_file() and re.fullmatch(r"(?:[0-9a-fA-F]+|\d+\.(?:log|ldb|sst))", path.name))
            else:
                files = sorted(source.path.rglob("*.jsonl"))
            for path in files:
                resolved = str(path.resolve())
                if resolved in seen:
                    continue
                seen.add(resolved)
                try:
                    if source.kind == "claude_cache":
                        from usage_tracker.collectors.claude_cache import CacheReadError
                        try:
                            n, file_warnings = collect_cache_file(store, source, path)
                        except CacheReadError as exc:
                            # Decoder messages are fixed, sanitized descriptions.
                            # No checkpoint is written, allowing runtime recovery
                            # or a subsequent supported cache to be retried.
                            warnings[str(exc)] += 1
                            continue
                    else:
                        n, file_warnings = collect_file(store, source, path)
                    changed += n
                    warnings.update(file_warnings)
                except OSError:
                    source_errors += 1
            row = store.conn.execute("""SELECT COUNT(DISTINCT es.event_key) FROM event_sources es
                JOIN file_state fs ON es.source_path=fs.path WHERE fs.source=?""", (source.name,)).fetchone()
            with store.conn:
                store.health(source=source.name, path=str(source.path), status="error" if source_errors else "partial" if warnings else "ok" if files else "empty",
                             files=len(files), events=row[0], last_success=now() if not source_errors else None,
                             error=f"Could not read {source_errors} file(s). Check source permissions." if source_errors else None,
                             warnings=[f"{key}: {value}" for key, value in warnings.items()])
        except OSError:
            source_errors += 1
            with store.conn:
                store.health(source=source.name, path=str(source.path), status="error", files=0,
                             events=0, last_success=None, error="Could not enumerate this source. Check permissions.")
        total_files += len(files)
        errors += source_errors
    changed += store.reconcile_codex()
    # Codex events are derived after all sources have contributed observations;
    # update source counts only after reconciliation has established provenance.
    with store.conn:
        for source in sources:
            store.conn.execute("""UPDATE source_health SET events=(
                SELECT COUNT(DISTINCT es.event_key) FROM event_sources es
                JOIN file_state fs ON es.source_path=fs.path WHERE fs.source=?)
                WHERE source=? AND status NOT IN ('missing','empty')""", (source.name, source.name))
    result = {"started_at": started, "finished_at": now(), "files": total_files,
              "changed_events": changed, "errors": errors}
    with store.conn:
        store.set_setting("last_collection", result)
        store.set_setting("collection_status", "degraded" if errors else "ok")
    return result
