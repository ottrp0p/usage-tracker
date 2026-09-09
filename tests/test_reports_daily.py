"""Calendar reporting contracts for daily tables and independent annual charts."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from usage_tracker.cli import parser
from usage_tracker.models import ParseResult, UsageEvent
from usage_tracker.reports import report
from usage_tracker.server import make_server
from usage_tracker.storage import Store


class FrozenDatetime(datetime):
    """A stable instant in September keeps calendar/future assertions durable."""

    @classmethod
    def now(cls, tz=None):
        instant = cls(2026, 9, 7, 22, 0, tzinfo=timezone.utc)
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)


class DailyReportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name))
        self.addCleanup(self.store.close)
        frozen = patch("usage_tracker.reports.datetime", FrozenDatetime)
        frozen.start()
        self.addCleanup(frozen.stop)

    def add(self, key, stamp="2026-09-07T10:00:00Z", *, provider="openai", surface="codex_cli",
            session=None, model="model-a", quality="reported", **counts):
        # Default accounting is complete and disjoint: 60 ordinary input,
        # 30 cache reads, 10 cache writes, 20 output = 120 total tokens.
        fields = dict(input_tokens=100, cached_input_tokens=30, cache_creation_tokens=10,
                      output_tokens=20, reasoning_tokens=5, total_tokens=120)
        fields.update(counts)
        event = UsageEvent(key, provider, surface, session or key, stamp, model=model, quality=quality, **fields)
        with self.store.conn:
            self.store.ingest(ParseResult(events=[event]), "/synthetic-fixture.jsonl")

    def result(self, **kwargs):
        kwargs.setdefault("timezone_name", "America/Los_Angeles")
        return report(self.store, **kwargs)

    def test_daily_subtotals_merge_coding_surfaces_and_sort_unique_models(self):
        self.add("cli", session="same-session", model="z-model")
        self.add("desktop", surface="codex_desktop", session="same-session", model="a-model")
        self.add("agent", surface="codex_subagent", model="a-model")
        self.add("claude-code", provider="anthropic", surface="claude_code", model="claude-a")
        self.add("cowork", provider="anthropic", surface="claude_desktop_agent", model="claude-b")
        self.add("other", provider="example", surface="unrecognized_client", model="other")
        daily = self.result(month="current")["daily"]
        self.assertEqual(len(daily), 1)
        day = daily[0]
        self.assertEqual((day["date"], day["quality"], day["events"], day["sessions"]),
                         ("2026-09-07", "reported", 6, 5))
        apps = {row["app"]: row for row in day["apps"]}
        self.assertEqual(set(apps), {"codex", "claude", "unrecognized_client"})
        self.assertEqual(apps["codex"]["models"], ["a-model", "z-model"])
        self.assertEqual(apps["codex"]["sessions"], 2)
        self.assertEqual(apps["claude"]["total_tokens"], 240)
        self.assertEqual(sum(app["total_tokens"] for app in day["apps"]), day["total_tokens"])
        self.assertEqual(day["uncached_input_tokens"], 360)
        self.assertEqual(day["uncached_input_tokens"] + day["cached_input_tokens"] +
                         day["cache_creation_tokens"] + day["output_tokens"], day["total_tokens"])

    def test_estimates_and_regular_chats_have_separate_rows_and_app_families(self):
        self.add("coding")
        self.add("chatgpt", surface="chatgpt", quality="estimated", total_tokens=7)
        self.add("claudechat", provider="anthropic", surface="claude_chat", quality="estimated", total_tokens=9)
        result = self.result(month="current")
        by_quality = {row["quality"]: row for row in result["daily"]}
        self.assertEqual(by_quality["reported"]["total_tokens"], 120)
        self.assertEqual(by_quality["estimated"]["total_tokens"], 16)
        self.assertEqual({app["app"] for app in by_quality["estimated"]["apps"]}, {"chatgpt", "claude_chat"})
        self.assertEqual(result["totals"]["reported"]["total_tokens"], 120)
        self.assertEqual(result["totals"]["estimated"]["total_tokens"], 16)

    def test_partial_uncached_input_is_derived_per_event_and_remains_partial(self):
        self.add("known")
        self.add("unknown-cache", cached_input_tokens=None, total_tokens=None)
        self.add("unknown-creation", cache_creation_tokens=None)
        self.add("inconsistent-cache", input_tokens=10)
        before = [tuple(row) for row in self.store.conn.execute("SELECT * FROM usage_events ORDER BY event_key")]
        result = self.result(month="current")
        aggregates = [result["totals"]["reported"], result["daily"][0],
                      result["daily"][0]["apps"][0], result["breakdown"][0], result["series"][6]["reported"]]
        for aggregate in aggregates:
            self.assertEqual(aggregate["uncached_input_tokens"], 60)
            self.assertEqual(aggregate["unknown_fields"]["uncached_input_tokens"], 3)
            self.assertEqual(aggregate["input_tokens"], 310)
            self.assertEqual(aggregate["unknown_total_events"], 1)
        after = [tuple(row) for row in self.store.conn.execute("SELECT * FROM usage_events ORDER BY event_key")]
        self.assertEqual(before, after, "Reporting must not modify stored token evidence")

    def test_unknown_and_observed_zero_are_distinct(self):
        self.add("all-unknown", input_tokens=None, cached_input_tokens=None, cache_creation_tokens=None,
                 output_tokens=None, total_tokens=None)
        unknown = self.result(month="current")["totals"]["reported"]
        self.assertIsNone(unknown["uncached_input_tokens"])
        self.assertEqual(unknown["unknown_fields"]["uncached_input_tokens"], 1)
        self.add("zero", input_tokens=0, cached_input_tokens=0, cache_creation_tokens=0,
                 output_tokens=0, total_tokens=0)
        partial = self.result(month="current")["totals"]["reported"]
        self.assertEqual(partial["uncached_input_tokens"], 0)
        self.assertEqual(partial["unknown_fields"]["uncached_input_tokens"], 1)

    def test_provisional_and_partial_history_counts_are_separate_from_unknown_fields(self):
        self.add("provisional", flags=["provisional_usage"])
        self.add("cached-partial", flags=["provisional_usage", "cache_partial_history"])
        self.add("missing-category", cached_input_tokens=None)
        self.add("prior-month", "2026-08-31T20:00:00Z", flags=["provisional_usage", "cache_partial_history"])
        self.add("other-provider", provider="anthropic", surface="claude_desktop_agent", flags=["cache_partial_history"])
        result = self.result(month="current", provider="openai")
        for aggregate in (result["totals"]["reported"], result["daily"][0],
                          result["daily"][0]["apps"][0], result["series"][6]["reported"]):
            self.assertEqual(aggregate["events"], 3)
            self.assertEqual(aggregate["provisional_events"], 2)
            self.assertEqual(aggregate["incomplete_events"], 1)
            self.assertEqual(aggregate["unknown_fields"]["cached_input_tokens"], 1)
            self.assertEqual(aggregate["total_tokens"], 360)
        # An unobserved bucket has no provisional or incomplete observations;
        # its token count remains unknown rather than acquiring a fabricated 0.
        empty = result["series"][0]["reported"]
        self.assertEqual((empty["provisional_events"], empty["incomplete_events"]), (0, 0))
        self.assertIsNone(empty["total_tokens"])

    def test_source_latest_usage_follows_provenance_and_differs_from_scan_time(self):
        self.add("old-log", "2026-08-30T20:00:00Z")
        self.add("new-cache", "2026-09-07T20:00:00Z")
        self.add("export", "2026-09-06T20:00:00Z", quality="estimated", surface="chatgpt")
        self.add("undated", None)
        scan_time = "2026-09-07T22:00:00.000Z"
        with self.store.conn:
            # Each source owns one exact path. A copied event also belongs to
            # the second source, but must not inflate any usage totals.
            self.store.conn.execute("DELETE FROM event_sources")
            self.store.conn.executemany("INSERT INTO event_sources VALUES (?,?)", [
                ("old-log", "/logs/local.jsonl"), ("new-cache", "/cache/remote"),
                ("new-cache", "/logs/cache-copy.jsonl"), ("export", "/imports/chats.zip"),
                ("undated", "/logs/no-date.jsonl"),
            ])
            self.store.save_checkpoint("/logs/local.jsonl", "Local logs", {}, {})
            self.store.save_checkpoint("/logs/cache-copy.jsonl", "Cache copy", {}, {})
            self.store.save_checkpoint("/logs/no-date.jsonl", "Undated", {}, {})
            for source, path in (("Local logs", "/logs"), ("Cache copy", "/copy"),
                                 ("Remote cache", "/cache/remote"), ("Import: chats", "/imports/chats.zip"),
                                 ("Undated", "/undated"), ("Empty", "/empty")):
                self.store.health(source=source, path=path, status="ok", files=1, events=1,
                                  last_success=scan_time)
        # Coverage describes all source evidence, even for another date/provider
        # filter. A successful scan is not substituted for missing usage dates.
        result = self.result(month="2026-01", provider="anthropic")
        coverage = {item["source"]: item for item in result["coverage"]}
        self.assertEqual(coverage["Local logs"]["latest_usage"], "2026-08-30T20:00:00Z")
        self.assertEqual(coverage["Remote cache"]["latest_usage"], "2026-09-07T20:00:00Z")
        self.assertEqual(coverage["Cache copy"]["latest_usage"], coverage["Remote cache"]["latest_usage"])
        self.assertEqual(coverage["Import: chats"]["latest_usage"], "2026-09-06T20:00:00Z")
        self.assertIsNone(coverage["Undated"]["latest_usage"])
        self.assertIsNone(coverage["Empty"]["latest_usage"])
        self.assertTrue(all(item["last_success"] == scan_time for item in coverage.values()))
        self.assertEqual(self.result(month="current")["totals"]["reported"]["events"], 1)

    def test_month_uses_local_boundaries_and_retains_all_calendar_days(self):
        self.add("before", "2024-03-01T07:59:59Z")  # Feb 29 in Los Angeles.
        self.add("start", "2024-03-01T08:00:00Z")
        self.add("last", "2024-04-01T06:59:59Z")  # March ends under daylight time.
        self.add("after", "2024-04-01T07:00:00Z")
        result = self.result(month="2024-03", days=1)
        self.assertEqual(result["period"], {"days": 31, "start": "2024-03-01", "end": "2024-03-31",
                                            "group": "day", "month": "2024-03", "year": 2024})
        self.assertEqual(len(result["series"]), 31)
        self.assertEqual([row["date"] for row in result["daily"]], ["2024-03-01", "2024-03-31"])
        self.assertEqual(result["totals"]["reported"]["total_tokens"], 240)
        self.assertIsNone(result["series"][1]["reported_tokens"])
        self.assertEqual(result["series"][1]["reported_events"], 0)

    def test_leap_february_and_week_buckets(self):
        self.add("leap", "2024-02-29T20:00:00Z")
        february = self.result(month="2024-02")
        self.assertEqual(len(february["series"]), 29)
        self.assertEqual(february["period"]["end"], "2024-02-29")
        weekly = self.result(month="2024-02", group="week")
        self.assertEqual([row["date"] for row in weekly["series"]],
                         ["2024-01-29", "2024-02-05", "2024-02-12", "2024-02-19", "2024-02-26"])
        self.assertEqual(weekly["series"][-1]["reported_tokens"], 120)
        self.assertEqual(weekly["daily"], february["daily"])

    def test_year_boundaries_month_aggregation_and_category_series(self):
        self.add("prior-year", "2024-01-01T07:59:59Z")
        self.add("first", "2024-01-01T08:00:00Z")
        self.add("jan-more", "2024-01-20T20:00:00Z")
        self.add("estimate", "2024-02-10T20:00:00Z", quality="estimated", surface="chatgpt", total_tokens=11)
        self.add("last", "2025-01-01T07:59:59Z")
        self.add("next-year", "2025-01-01T08:00:00Z")
        result = self.result(year=2024, group="month", days=0)
        self.assertEqual(result["period"], {"days": 366, "start": "2024-01-01", "end": "2024-12-31",
                                            "group": "month", "year": 2024})
        self.assertEqual([row["date"] for row in result["series"]], [f"2024-{month:02d}-01" for month in range(1, 13)])
        self.assertEqual(result["series"][0]["reported_tokens"], 240)
        self.assertEqual(result["series"][0]["reported"]["uncached_input_tokens"], 120)
        self.assertEqual(result["series"][1]["estimated_tokens"], 11)
        self.assertIsNone(result["series"][1]["reported_tokens"])
        self.assertEqual(result["series"][-1]["reported_tokens"], 120)
        self.assertEqual(result["totals"]["reported"]["total_tokens"], 360)

    def test_current_calendar_and_future_buckets_do_not_include_future_logs(self):
        self.add("today")
        self.add("tomorrow", "2026-09-08T07:00:00Z")
        self.add("future-month", "2026-10-10T20:00:00Z")
        month = self.result(month="current")
        self.assertEqual(month["period"]["month"], "2026-09")
        self.assertEqual(len(month["series"]), 30)
        self.assertEqual(month["totals"]["reported"]["events"], 1)
        self.assertIsNone(month["series"][7]["reported_tokens"])
        annual = self.result(year=2026, group="month")
        self.assertEqual(len(annual["series"]), 12)
        self.assertEqual(annual["series"][8]["reported_tokens"], 120)
        self.assertIsNone(annual["series"][9]["reported_tokens"])
        future = self.result(year=2027, group="month")
        self.assertEqual(len(future["series"]), 12)
        self.assertEqual(future["daily"], [])
        self.assertTrue(all(row["reported_tokens"] is None for row in future["series"]))

    def test_current_month_uses_selected_timezone(self):
        class BoundaryDatetime(FrozenDatetime):
            @classmethod
            def now(cls, tz=None):
                instant = cls(2026, 9, 1, 1, 0, tzinfo=timezone.utc)
                return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

        with patch("usage_tracker.reports.datetime", BoundaryDatetime):
            self.assertEqual(self.result(month="current")["period"]["month"], "2026-08")
            self.assertEqual(self.result(month="current", timezone_name="UTC")["period"]["month"], "2026-09")

    def test_filters_apply_identically_to_daily_series_and_totals(self):
        self.add("openai")
        self.add("anthropic", provider="anthropic", surface="claude_code")
        self.add("othermodel", model="model-b")
        self.add("otherday", "2026-09-06T20:00:00Z")
        self.add("estimate", quality="estimated", surface="chatgpt")
        for filters in ({"provider": "openai"}, {"surface": "codex_cli"}, {"model": "model-a"},
                        {"provider": "openai", "surface": "codex_cli", "model": "model-b"}):
            result = self.result(year=2026, group="month", **filters)
            for quality in ("reported", "estimated"):
                daily = [row for row in result["daily"] if row["quality"] == quality]
                self.assertEqual(sum(row["events"] for row in daily), result["totals"][quality]["events"])
                self.assertEqual(sum(row["total_tokens"] for row in daily), result["totals"][quality]["total_tokens"] or 0)
                self.assertEqual(sum(row[f"{quality}_events"] for row in result["series"]), result["totals"][quality]["events"])
        self.assertEqual(self.result(month="current", provider="missing")["daily"], [])

    def test_all_time_keeps_undated_totals_without_inventing_daily_rows(self):
        self.add("dated")
        self.add("undated", None)
        self.add("future", "2027-01-10T20:00:00Z")
        all_time = self.result(days=0, group="month")
        self.assertEqual(all_time["totals"]["reported"]["total_tokens"], 360)
        self.assertEqual(all_time["totals"]["reported"]["unknown_timestamp_events"], 1)
        self.assertEqual([row["date"] for row in all_time["daily"]], ["2026-09-07", "2027-01-10"])
        finite = self.result(month="current")
        self.assertEqual(finite["totals"]["reported"]["events"], 1)

    def test_calendar_validation_and_cli_arguments(self):
        for month in ("2026-9", "2026-13", "2026-00", "0000-01", "2026-09-01", "next", ""):
            with self.subTest(month=month), self.assertRaises(ValueError):
                self.result(month=month)
        for year in (0, 10000, "2026", True, 2026.5):
            with self.subTest(year=year), self.assertRaises(ValueError):
                self.result(year=year)
        with self.assertRaises(ValueError):
            self.result(month="current", year=2026)
        with self.assertRaises(ValueError):
            self.result(group="quarter")
        args = parser().parse_args(["report", "--year", "2026", "--group", "month"])
        self.assertEqual((args.year, args.group, args.days), (2026, "month", 30))
        args = parser().parse_args(["report", "--month", "current"])
        self.assertEqual((args.month, args.year), ("current", None))

    def test_http_accepts_month_and_year_and_rejects_conflicting_selectors(self):
        self.add("fixture")
        server = make_server(self.store.data_dir, port=0, timezone_name="America/Los_Angeles")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            for query, expected_count in (("month=current", 30), ("year=2026&group=month", 12)):
                with urllib.request.urlopen(f"{base}/api/report?{query}") as response:
                    data = json.load(response)
                self.assertEqual(len(data["series"]), expected_count)
                self.assertEqual(data["daily"][0]["apps"][0]["app"], "codex")
            for query in ("month=current&year=2026", "month=bad", "year=bad", "year=10000"):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(f"{base}/api/report?{query}")
                self.assertEqual(caught.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
