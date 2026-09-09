"""Real ledger/report integration: history is inspectable but not additive."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from usage_tracker.models import ParseResult, Snapshot, UsageEvent, UsageSummary
from usage_tracker.reports import report, quota_history, summary_revisions
from usage_tracker.server import make_server
from usage_tracker.storage import Store


class EvidenceReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)

    def summary(self):
        return UsageSummary("summary", "anthropic", "claude_desktop_agent", "session",
            "2026-09-07T10:02:00Z", "2026-09-07T10:00:00Z",
            {"input_tokens": 10, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 5, "output_tokens": 100},
            model_usage={"claude-test": {"input_tokens": 999, "cache_read_input_tokens": 2000,
                                         "cache_creation_input_tokens": 500, "output_tokens": 1000}},
            terminal=True, native={"scope": "main_agent_run", "scope_verified": True,
                                   "window_verified": True, "subtype": "success", "terminal_reason": "completed"})

    def message(self):
        return UsageEvent("message", "anthropic", "claude_desktop_agent", "session", "2026-09-07T10:01:00Z",
                          model="claude-test", input_tokens=35, cached_input_tokens=20, cache_creation_tokens=5,
                          output_tokens=40, total_tokens=75)

    def test_report_totals_include_only_remainder_and_retain_broader_evidence(self):
        with self.store.conn:
            self.store.ingest(ParseResult(events=[self.message()], summaries=[self.summary()]), "source")
            self.store.save_checkpoint("source", "Claude fixture", {}, {})
            self.store.health(source="Claude fixture", path="source", status="ok", files=1, events=1, last_success=None)
        data = report(self.store, month="2026-09", timezone_name="UTC")
        self.assertEqual(data["totals"]["reported"]["total_tokens"], 135)
        self.assertEqual(data["totals"]["reported"]["summary_remainders"], 1)
        saved = data["reconciliation"]["items"][0]
        self.assertEqual(saved["status"], "reconciled")
        self.assertEqual(saved["remainder"]["total_tokens"], 60)
        self.assertEqual(saved["model_usage"]["claude-test"]["output_tokens"], 1000)
        self.assertEqual(saved["revisions"]["total"], 1)
        self.assertEqual(data["coverage"][0]["summaries"], 1)

    def test_ambiguous_summary_is_filterable_without_inventing_message_usage(self):
        summary = self.summary()
        summary.started_at = None
        summary.native["scope_verified"] = False
        with self.store.conn:
            self.store.ingest(ParseResult(summaries=[summary]), "source")
        data = report(self.store, month="2026-09", model="claude-test", timezone_name="UTC")
        self.assertIsNone(data["totals"]["reported"]["total_tokens"])
        self.assertIn("claude-test", data["options"]["models"])
        self.assertEqual(data["reconciliation"]["total"], 1)
        other = report(self.store, month="2026-08", timezone_name="UTC")
        self.assertEqual(other["reconciliation"]["total"], 0)

    def test_revisions_preserve_corrections_and_quota_history_keeps_source_time(self):
        summary = self.summary()
        with self.store.conn:
            self.store.ingest(ParseResult(summaries=[summary]), "source")
        corrected = deepcopy(summary)
        corrected.usage["output_tokens"] = 80
        with self.store.conn:
            self.store.ingest(ParseResult(summaries=[corrected]), "source")
            # Same quota content read again is the same source observation.
            snapshots = [Snapshot("quota-old", "anthropic", "quota", "2026-09-06T10:00:00Z",
                                  {"windows": [{"name": "five_hour", "used_percent": 100}]}),
                         Snapshot("quota-new", "anthropic", "quota", "2026-09-07T10:00:00Z",
                                  {"windows": [{"name": "five_hour", "used_percent": 2}]})]
            self.store.ingest(ParseResult(snapshots=snapshots), "source")
            self.store.ingest(ParseResult(snapshots=snapshots), "source")
        versions = summary_revisions(self.store, summary.summary_key)
        self.assertEqual(versions["total"], 2)
        self.assertEqual({item["usage"]["output_tokens"] for item in versions["items"]}, {80, 100})
        first, second = quota_history(self.store, limit=1), quota_history(self.store, limit=1, offset=1)
        self.assertEqual(first["total"], 2)
        self.assertEqual(first["items"][0]["snapshot_key"], "quota-new")
        self.assertEqual(second["items"][0]["snapshot_key"], "quota-old")
        self.assertEqual(first["items"][0]["timestamp"], "2026-09-07T10:00:00Z")
        self.assertIsNotNone(first["items"][0]["first_seen"])
        self.assertEqual(first["items"][0]["source_paths"], ["source"])

    def test_http_history_pagination_assets_and_validation(self):
        with self.store.conn:
            self.store.ingest(ParseResult(summaries=[self.summary()]), "source")
        server = make_server(self.store.data_dir, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            for path in ("/api/reconciliation?month=2026-09", "/api/summary-revisions?summary_key=summary"):
                with urllib.request.urlopen(base + path) as response:
                    self.assertEqual(json.load(response)["total"], 1)
            with urllib.request.urlopen(base + "/api/quota-history?offset=0&limit=25") as response:
                self.assertEqual(json.load(response)["items"], [])
            for asset in ("/evidence.js", "/evidence.css"):
                with urllib.request.urlopen(base + asset) as response:
                    self.assertEqual(response.status, 200)
            for path in ("/api/reconciliation?limit=0", "/api/quota-history?offset=-1", "/api/summary-revisions"):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(base + path)
                self.assertEqual(caught.exception.code, 400)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_quota_history_honors_provider_but_keeps_observations_from_other_months(self):
        # A busy provider's logs must not bury another provider's quota history.
        # Calendar/model selections describe tokens, not account allowance time.
        with self.store.conn:
            self.store.ingest(ParseResult(snapshots=[
                Snapshot("claude-august", "anthropic", "quota", "2026-08-01T10:00:00Z", {}),
                Snapshot("codex-september", "openai", "quota", "2026-09-07T10:00:00Z", {})]), "source")
        data = report(self.store, month="2026-09", provider="anthropic", model="claude-test", timezone_name="UTC")
        self.assertEqual(data["quota_history"]["total"], 1)
        self.assertEqual(data["quota_history"]["items"][0]["snapshot_key"], "claude-august")
