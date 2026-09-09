"""Cache discovery/checkpoint integration, independent of the V8 decoder.

Decoder format fixtures live in test_claude_cache. These checks exercise the
SQLite commit boundary and prove replaced snapshots cannot skip or duplicate
provider requests. Synthetic bodies never need to resemble private chat data.
"""

from pathlib import Path
from unittest.mock import patch
import json
import tempfile
import unittest

from usage_tracker.collectors.claude import parse_line
from usage_tracker.config import Source, default_sources, load_sources
from usage_tracker.ingest import collect, collect_cache_file
from usage_tracker.models import ParseResult, Snapshot, UsageSummary
from usage_tracker.storage import Store


class CacheIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / "data")
        self.cache = self.root / "blobs"
        self.cache.mkdir()
        self.path = self.cache / "2d"
        self.path.write_bytes(b"snapshot-A")
        self.source = Source("cache fixture", "claude_cache", self.cache, "claude_desktop_agent")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def observation(self, output=10):
        record = {"type": "assistant", "sessionId": "session", "timestamp": "2026-09-07T22:00:00Z",
                  "message": {"id": "msg_fixture", "model": "claude-fable-5-1", "role": "assistant",
                              "usage": {"input_tokens": 1, "cache_read_input_tokens": 20,
                                        "cache_creation_input_tokens": 3, "output_tokens": output}}}
        return parse_line(record, {}, source_path=str(self.path), surface="claude_desktop_agent")

    def test_replaced_same_length_snapshot_and_copy_merge_with_jsonl(self):
        with patch("usage_tracker.collectors.claude_cache.parse_cache", return_value=self.observation()) as decoder:
            self.assertEqual(collect(self.store, [self.source])["changed_events"], 1)
            self.assertEqual(collect(self.store, [self.source])["changed_events"], 0)
            self.assertEqual(decoder.call_count, 1)
            # A changed middle byte must trigger a reread even at the same size.
            self.path.write_bytes(b"snapsHot-A")
            decoder.return_value = self.observation(output=30)
            self.assertEqual(collect(self.store, [self.source])["changed_events"], 1)
            (self.cache / "2e").write_bytes(self.path.read_bytes())
            self.assertEqual(collect(self.store, [self.source])["changed_events"], 0)
        with self.store.conn:
            self.store.ingest(self.observation(output=30), "copy.jsonl")
        row = self.store.conn.execute("SELECT COUNT(*),SUM(total_tokens) FROM usage_events").fetchone()
        self.assertEqual(tuple(row), (1, 54))

    def test_failed_commit_does_not_skip_usage_on_retry(self):
        with patch("usage_tracker.collectors.claude_cache.parse_cache", return_value=self.observation()):
            with patch.object(self.store, "save_checkpoint", side_effect=RuntimeError("crash")):
                with self.assertRaises(RuntimeError):
                    collect_cache_file(self.store, self.source, self.path)
            self.assertIsNone(self.store.checkpoint(str(self.path)))
            self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0], 0)
            self.assertEqual(collect_cache_file(self.store, self.source, self.path)[0], 1)

    def test_mutation_during_decode_waits_for_stable_snapshot(self):
        def updating_cache(path):
            path.write_bytes(b"snapshot-B")
            return self.observation()
        with patch("usage_tracker.collectors.claude_cache.parse_cache", side_effect=updating_cache):
            changed, warnings = collect_cache_file(self.store, self.source, self.path)
        self.assertEqual(changed, 0)
        self.assertTrue(warnings)
        self.assertIsNone(self.store.checkpoint(str(self.path)))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0], 0)

    def test_decode_failure_has_no_checkpoint_and_recovers(self):
        from usage_tracker.collectors.claude_cache import CacheReadError
        with patch("usage_tracker.collectors.claude_cache.parse_cache", side_effect=CacheReadError("Cache decoder unavailable")):
            collect(self.store, [self.source])
        self.assertIsNone(self.store.checkpoint(str(self.path)))
        self.assertEqual(self.store.conn.execute("SELECT status FROM source_health").fetchone()[0], "partial")
        with patch("usage_tracker.collectors.claude_cache.parse_cache", return_value=self.observation()):
            self.assertEqual(collect(self.store, [self.source])["changed_events"], 1)

    def test_partial_history_warning_survives_unchanged_scan(self):
        result = self.observation()
        result.warnings.append("Cache contains only part of the conversation history")
        with patch("usage_tracker.collectors.claude_cache.parse_cache", return_value=result):
            collect(self.store, [self.source])
        with patch("usage_tracker.collectors.claude_cache.parse_cache") as decoder:
            collect(self.store, [self.source])
            decoder.assert_not_called()
        row = self.store.conn.execute("SELECT status,warnings FROM source_health").fetchone()
        self.assertEqual(row["status"], "partial")
        self.assertIn("part of the conversation", row["warnings"])

    def test_evicted_evidence_survives_without_becoming_fresh_on_file_scans(self):
        # An unchanged fingerprint proves only that the current file is stable;
        # it says nothing about historical records dropped by a previous rewrite.
        # Keep saved evidence, but never advance its capture time on file scans.
        captured_at = "2026-09-07T22:00:00Z"
        result = ParseResult(summaries=[UsageSummary(
            "saved-result", "anthropic", "claude_desktop_agent", "session",
            captured_at, None, {"input_tokens": 1, "output_tokens": 20},
            native={"source_kind": "desktop_conversation_cache"})], snapshots=[Snapshot(
                "saved-quota", "anthropic", "quota", captured_at,
                {"windows": [{"name": "five_hour", "used_percent": 22}]})])
        with patch("usage_tracker.storage.now", return_value=captured_at), \
             patch("usage_tracker.collectors.claude_cache.parse_cache", return_value=result):
            collect(self.store, [self.source])
        self.path.write_bytes(b"snapshot-B")
        with patch("usage_tracker.storage.now", return_value="2026-09-07T22:10:00Z"), \
             patch("usage_tracker.collectors.claude_cache.parse_cache", return_value=ParseResult()):
            collect(self.store, [self.source])
            collect(self.store, [self.source])
        for table in ("claude_summary_revisions", "claude_summary_sources",
                      "snapshot_observations", "snapshot_sources"):
            rows = self.store.conn.execute(f"SELECT last_seen FROM {table}").fetchall()
            self.assertEqual([row[0] for row in rows], [captured_at])
        # The source remains visibly healthy and current, independently of its
        # last captured quota/summary. Eviction does not delete either ledger.
        self.assertEqual(self.store.conn.execute("SELECT last_success FROM file_state").fetchone()[0],
                         "2026-09-07T22:10:00Z")

    def test_discovery_ignores_unrelated_filenames_and_config_accepts_cache(self):
        (self.cache / "LOCK").write_text("not a blob")
        (self.cache / "log.jsonl").write_text("not a blob")
        with patch("usage_tracker.collectors.claude_cache.parse_cache", return_value=ParseResult()) as decoder:
            self.assertEqual(collect(self.store, [self.source])["files"], 1)
            decoder.assert_called_once()
        config = self.root / "config.json"
        config.write_text(json.dumps({"include_defaults": False, "sources": [
            {"kind": "claude_cache", "path": str(self.cache)}]}))
        self.assertEqual(load_sources(config)[0].kind, "claude_cache")
        self.assertTrue(any(source.kind == "claude_cache" for source in default_sources()))
