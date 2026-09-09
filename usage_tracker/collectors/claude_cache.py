"""Read usage metadata from Claude desktop's cached remote Cowork histories.

These Chromium IndexedDB blobs are snapshots, not append-only logs. The caller
must fingerprint the whole file and retry a failed decode without advancing its
checkpoint. A tiny Node helper decodes the V8 container using built-in modules
and emits only an explicit metadata allowlist; conversation bodies never cross
into Python, the collector checkpoint, or SQLite.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

from usage_tracker.collectors.claude import parse_line
from usage_tracker.models import ParseResult


MAX_CACHE_BYTES = 32 * 1024 * 1024
MAX_METADATA_BYTES = 16 * 1024 * 1024
CACHE_TIMEOUT_SECONDS = 15
PARTIAL_HISTORY_WARNING = "Claude desktop cache contains only part of this conversation history."
PROVISIONAL_WARNING = "Claude desktop cache contains provisional usage; final output counts may be unavailable."


class CacheReadError(Exception):
    """A sanitized, retryable cache failure; never include raw decoder output."""


def find_node() -> str | None:
    """Find the optional decoder even under launchd's minimal environment.

    An explicit override is useful for installations outside the usual macOS
    locations. An invalid override fails visibly rather than silently choosing
    a different executable. No package installation or network access occurs.
    """
    override = os.environ.get("CLAUDE_CACHE_NODE")
    candidates = [override] if override else [
        "/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node",
        shutil.which("node"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).absolute())
    return None


def parse_metadata(metadata: dict, *, source_path: str) -> ParseResult:
    """Convert the decoder's allowlisted records through the existing adapter.

    Provider message IDs retain precisely the JSONL event identity, so multiple
    cached snapshots and later complete local histories cannot add usage twice.
    Result totals and quota observations are retained in separate evidence lists.
    Their scopes overlap message observations, so they never become additive
    events here. Task-notification totals remain outside these supported scopes.
    """
    if not isinstance(metadata, dict) or metadata.get("format") != "claude_cache_metadata_v1":
        raise CacheReadError("Claude desktop cache decoder returned an unsupported metadata format.")
    result = ParseResult()
    # LevelDB files may preserve several conversation snapshots, including
    # historical windows of the same conversation. Keep each window's coverage
    # evidence and let ordinary message IDs deduplicate overlapping requests.
    if isinstance(metadata.get("snapshots"), list):
        for snapshot in metadata["snapshots"]:
            parsed = parse_metadata(snapshot, source_path=source_path)
            result.events.extend(parsed.events)
            result.snapshots.extend(parsed.snapshots)
            result.summaries.extend(parsed.summaries)
            result.warnings.extend(parsed.warnings)
        for warning in metadata.get("warnings", []):
            if warning in {
                "Claude desktop LevelDB contains an incomplete write; available complete records were read.",
                "Claude desktop LevelDB contains unreadable records; some cached usage may be unavailable.",
            }:
                result.warnings.append(warning)
        result.warnings = sorted(set(result.warnings))
        return result
    if metadata.get("recognized") is False:
        return result  # Other ordinary IndexedDB objects contain no usage.
    if metadata.get("recognized") is not True or not isinstance(metadata.get("records"), list):
        raise CacheReadError("Claude desktop cache decoder returned invalid metadata.")
    history_complete = metadata.get("history_complete") is True
    if not history_complete:
        result.warnings.append(PARTIAL_HISTORY_WARNING)
    state = {}
    for record in metadata["records"]:
        parsed = parse_line(record, state, source_path=source_path, surface="claude_desktop_agent")
        result.warnings.extend(parsed.warnings)
        for snapshot in parsed.snapshots:
            snapshot.data["source"] = "desktop_conversation_cache"
            result.snapshots.append(snapshot)
        for summary in parsed.summaries:
            summary.native["source_kind"] = "desktop_conversation_cache"
            summary.native["cache_history_complete"] = history_complete
            if not history_complete:
                summary.flags.append("cache_partial_history")
            result.summaries.append(summary)
        for event in parsed.events:
            event.native["source_kind"] = "desktop_conversation_cache"
            event.native["cache_history_complete"] = history_complete
            # Keep the provider's final/provisional distinction. A completed
            # conversation or result summary does not finalize every message.
            if not history_complete:
                event.flags.append("cache_partial_history")
            result.events.append(event)
    if any("provisional_usage" in event.flags for event in result.events):
        result.warnings.append(PROVISIONAL_WARNING)
    result.warnings = sorted(set(result.warnings))
    return result


def parse_cache(path: Path, *, node_path: str | None = None) -> ParseResult:
    """Decode a bounded local cache snapshot or raise a retryable safe error."""
    runtime = node_path or find_node()
    if runtime is None:
        raise CacheReadError("Claude desktop cache requires Node.js; set CLAUDE_CACHE_NODE to its executable path.")
    helper = Path(__file__).with_name("claude_cache_decode.js")
    try:
        if path.stat().st_size > MAX_CACHE_BYTES:
            raise CacheReadError("Claude desktop cache exceeds the supported 32 MiB size limit.")
        completed = subprocess.run(
            [runtime, "--max-old-space-size=128", str(helper), str(path.absolute())],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=CACHE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise CacheReadError("Claude desktop cache decoding timed out; the file will be retried.") from None
    except OSError:
        raise CacheReadError("Claude desktop cache or its Node.js decoder could not be read or started.") from None
    if completed.returncode != 0:
        # Decoder errors can otherwise include content, paths, or Node stack
        # traces. Only our fixed, documented failure codes influence messages.
        code = completed.stderr.strip()
        if code == b"unsupported_header":
            raise CacheReadError("Claude desktop cache has an unsupported Chromium serialization header.")
        if code == b"unsupported_tree":
            raise CacheReadError("Claude desktop cache contains an unsupported conversation history format.")
        if code == b"size_limit":
            raise CacheReadError("Claude desktop cache exceeds the supported decoding size limits.")
        raise CacheReadError("Claude desktop cache could not be decoded; the file will be retried.")
    if len(completed.stdout) > MAX_METADATA_BYTES:
        raise CacheReadError("Claude desktop cache metadata exceeds the supported size limit.")
    try:
        metadata = json.loads(completed.stdout)
    except (ValueError, UnicodeError):
        raise CacheReadError("Claude desktop cache decoder returned invalid metadata.") from None
    return parse_metadata(metadata, source_path=str(path.absolute()))
