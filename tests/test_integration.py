"""Real SQLite/JSONL/HTTP integration with isolated synthetic source files."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from tests.test_codex import metadata, usage
from usage_tracker.config import Source
from usage_tracker.ingest import collect
from usage_tracker.models import ParseResult, Snapshot, UsageEvent
from usage_tracker.reports import report
from usage_tracker.server import make_server
from usage_tracker.service import plist_payload
from usage_tracker.storage import Store


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / "data")
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.path = self.logs / "session.jsonl"
        self.sources = [Source("fixture", "codex", self.logs, "unknown")]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def write(self, rows):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def totals(self):
        return self.store.conn.execute("SELECT COUNT(*),SUM(total_tokens) FROM usage_events").fetchone()

    def test_partial_write_resume_copy_and_replay_are_idempotent(self):
        self.write([metadata(), usage(100)])
        pending = json.dumps(usage(250, output=30, timestamp="2026-09-07T10:02:00Z"))
        with self.path.open("a") as f:
            f.write(pending[:20])
        self.assertEqual(collect(self.store, self.sources)["changed_events"], 1)
        offset = self.store.checkpoint(str(self.path))["offset"]
        self.assertEqual(collect(self.store, self.sources)["changed_events"], 0)
        self.assertEqual(self.store.checkpoint(str(self.path))["offset"], offset)
        with self.path.open("a") as f:
            f.write(pending[20:] + "\n")
        self.assertEqual(collect(self.store, self.sources)["changed_events"], 1)
        (self.logs / "copy.jsonl").write_bytes(self.path.read_bytes())
        self.assertEqual(collect(self.store, self.sources)["changed_events"], 0)
        self.assertEqual(tuple(self.totals()), (2, 250))

    def test_truncate_and_rewrite_does_not_skip_new_history(self):
        self.write([metadata(), usage(100)])
        collect(self.store, self.sources)
        self.write([metadata("new"), usage(200)])
        collect(self.store, self.sources)
        self.assertEqual(tuple(self.totals()), (2, 300))

    def test_malformed_lines_do_not_block_following_usage_or_leak_content(self):
        self.write([metadata()])
        with self.path.open("a") as f:
            f.write('{"private": "sensitive body" BAD}\n')
            f.write(json.dumps(usage(100)) + "\n")
        collect(self.store, self.sources)
        self.assertEqual(tuple(self.totals()), (1, 100))
        health = self.store.conn.execute("SELECT * FROM source_health").fetchone()
        self.assertEqual(health["status"], "partial")
        self.assertNotIn("sensitive", health["warnings"])

    def test_checkpoint_and_records_rollback_together(self):
        self.write([metadata(), usage(100)])
        with patch.object(self.store, "save_checkpoint", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                collect(self.store, self.sources)
        self.assertEqual(tuple(self.totals()), (0, None))
        self.assertIsNone(self.store.checkpoint(str(self.path)))
        collect(self.store, self.sources)
        self.assertEqual(tuple(self.totals()), (1, 100))

    def test_missing_and_empty_sources_are_visible(self):
        sources = self.sources + [Source("missing", "codex", self.root / "missing", "unknown")]
        collect(self.store, sources)
        statuses = {r["source"]: r["status"] for r in self.store.conn.execute("SELECT * FROM source_health")}
        self.assertEqual(statuses, {"fixture": "empty", "missing": "missing"})

    def test_reports_keep_estimates_snapshots_and_unknowns_separate(self):
        events = [UsageEvent("reported", "openai", "codex_cli", "s1", "2026-09-07T01:00:00Z", total_tokens=100, input_tokens=90, output_tokens=10),
                  UsageEvent("estimate", "openai", "chatgpt", "s2", "2026-09-07T01:00:00Z", quality="estimated", total_tokens=7),
                  UsageEvent("undated", "anthropic", "unknown", "s3", None, total_tokens=3)]
        with self.store.conn:
            self.store.ingest(ParseResult(events, [Snapshot("snapshot", "openai", "account_summary", None, {"total_tokens": 99999})]), "fixture")
        result = report(self.store, days=0, timezone_name="America/Los_Angeles")
        self.assertEqual(result["totals"]["reported"]["total_tokens"], 103)
        self.assertEqual(result["totals"]["estimated"]["total_tokens"], 7)
        self.assertEqual(result["series"][0]["date"], "2026-09-06")
        self.assertEqual(result["series"][0]["reported_tokens"], 100)
        self.assertEqual(result["totals"]["reported"]["unknown_timestamp_events"], 1)
        subset = report(self.store, days=0, provider="anthropic", timezone_name="UTC")
        self.assertEqual(subset["totals"]["reported"]["total_tokens"], 3)
        self.assertIsNone(subset["totals"]["estimated"]["total_tokens"])
        weekly = report(self.store, days=0, group="week", timezone_name="UTC")
        self.assertEqual(weekly["series"][0]["date"], "2026-09-07")

    def test_http_loopback_host_filter_assets_and_bad_query(self):
        server = make_server(self.store.data_dir, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/api/report?days=0") as response:
                self.assertIn("totals", json.load(response))
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            with urllib.request.urlopen(base + "/") as response:
                self.assertIn(b"Usage", response.read())
            for path, status in (("/api/report?days=no", 400), ("/../README.md", 404)):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(base + path)
                self.assertEqual(caught.exception.code, status)
            request = urllib.request.Request(base + "/api/report", headers={"Host": "external.example"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request)
            self.assertEqual(caught.exception.code, 403)
            with self.assertRaises(ValueError):
                make_server(self.store.data_dir, port=server.server_address[1])
            with self.assertRaises(ValueError):
                make_server(self.store.data_dir, host="0.0.0.0", port=0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_launchd_arguments_keep_paths_with_spaces_literal(self):
        (self.root / "custom config.json").write_text("{}")
        payload = plist_payload(self.root / "data with spaces", config_path=self.root / "custom config.json")
        self.assertIn(str(self.root / "data with spaces"), payload["ProgramArguments"])
        self.assertIn(str(self.root / "custom config.json"), payload["ProgramArguments"])
        self.assertIn("8787", payload["ProgramArguments"])
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual(payload["Umask"], 0o077)
