"""Store-level accounting tests exercise order, sparse evidence, and revisions."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from usage_tracker.collectors.claude import parse_line
from usage_tracker.models import ParseResult, UsageEvent
from usage_tracker.storage import Store


def claude(usage, *, final=False, surface="claude_code", message_id="msg_same"):
    """Run fixtures through the real adapter so native/normalized fields agree."""
    record = {
        "type": "assistant", "sessionId": "session-a", "requestId": "req_a",
        "timestamp": "2026-09-07T10:00:00Z", "uuid": "row-a",
        "message": {"id": message_id, "model": "claude-test", "role": "assistant",
                    "stop_reason": "end_turn" if final else None, "usage": usage},
    }
    return parse_line(record, {}, source_path="/fixture.jsonl", surface=surface).events[0]


def complete_usage(*, ordinary=10, cached=100, creation=20, output=80):
    return {"input_tokens": ordinary, "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": creation, "output_tokens": output}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name))
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)

    def ingest(self, event, path="/fixture.jsonl"):
        return self.store.ingest(ParseResult(events=[event]), path)

    def row(self, key="msg_same"):
        row = self.store.conn.execute("SELECT * FROM usage_events").fetchone()
        result = dict(row)
        result["native"] = json.loads(result["native"])
        result["flags"] = json.loads(result["flags"])
        return result

    def assert_totals(self, *, input, output, total, cached=None, creation=None):
        row = self.row()
        self.assertEqual((row["input_tokens"], row["output_tokens"], row["total_tokens"]), (input, output, total))
        if cached is not None:
            self.assertEqual(row["cached_input_tokens"], cached)
        if creation is not None:
            self.assertEqual(row["cache_creation_tokens"], creation)

    def test_final_smaller_counts_beat_provisional_in_either_order(self):
        initial = claude(complete_usage(ordinary=20, cached=200, creation=40, output=100))
        final = claude(complete_usage(output=80), final=True)
        self.ingest(initial, "/audit.jsonl")
        self.ingest(final, "/project.jsonl")
        self.assert_totals(input=130, output=80, total=210, cached=100, creation=20)
        first = self.row()
        self.store.conn.execute("DELETE FROM event_sources")
        self.store.conn.execute("DELETE FROM usage_events")
        self.ingest(final, "/project.jsonl")
        self.ingest(initial, "/audit.jsonl")
        self.assert_totals(input=130, output=80, total=210, cached=100, creation=20)
        second = self.row()
        first.pop("updated_at")
        second.pop("updated_at")
        self.assertEqual(first, second)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM event_sources").fetchone()[0], 2)
        self.assertNotIn("provisional_usage", second["flags"])

    def test_sparse_final_preserves_known_input_and_recomputes_total(self):
        initial = claude(complete_usage(output=120))
        final = claude({"output_tokens": 80}, final=True)
        self.ingest(initial)
        self.ingest(final)
        self.assert_totals(input=130, output=80, total=210, cached=100, creation=20)
        row = self.row()
        self.assertEqual(row["native"]["usage_field_priorities"]["input_tokens"], 10)
        self.assertEqual(row["native"]["usage_field_priorities"]["output_tokens"], 30)
        self.assertNotIn("incomplete_input_usage", row["flags"])
        self.assertNotIn("provisional_usage", row["flags"])

    def test_sparse_final_first_can_be_enriched_by_later_audit(self):
        final = claude({"output_tokens": 80}, final=True)
        initial = claude(complete_usage(output=120))
        self.ingest(final)
        self.ingest(initial)
        self.assert_totals(input=130, output=80, total=210)
        self.assertNotIn("incomplete_input_usage", self.row()["flags"])

    def test_provisional_input_can_improve_after_sparse_final_output(self):
        self.ingest(claude(complete_usage(output=1)))
        self.ingest(claude({"output_tokens": 80}, final=True))
        self.ingest(claude(complete_usage(ordinary=12, cached=150, output=500)))
        self.assert_totals(input=182, output=80, total=262, cached=150, creation=20)

    def test_equal_priority_takes_component_maxima_not_largest_total_record(self):
        self.ingest(claude(complete_usage(ordinary=10, cached=100, creation=20, output=100)))
        self.ingest(claude(complete_usage(ordinary=20, cached=80, creation=30, output=50)))
        self.assert_totals(input=150, output=100, total=250, cached=100, creation=30)
        self.assertEqual(self.row()["native"]["input_tokens"], 20)

    def test_missing_cache_stays_unknown_and_zero_is_real_evidence(self):
        self.ingest(claude({"input_tokens": 10, "output_tokens": 80}, final=True))
        self.assert_totals(input=None, output=80, total=None)
        self.assertIsNone(self.row()["cached_input_tokens"])
        self.assertIsNone(self.row()["cache_creation_tokens"])
        self.ingest(claude({"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}))
        self.assert_totals(input=10, output=80, total=90, cached=0, creation=0)
        self.assertNotIn("incomplete_input_usage", self.row()["flags"])

    def test_final_zero_correction_beats_provisional_nonzero(self):
        self.ingest(claude(complete_usage()))
        self.ingest(claude(complete_usage(ordinary=0, cached=0, creation=0, output=0), final=True))
        self.assert_totals(input=0, output=0, total=0, cached=0, creation=0)

    def test_idempotent_reingestion_and_copied_source_provenance(self):
        event = claude(complete_usage(), final=True)
        self.assertEqual(self.ingest(event, "/first.jsonl"), 1)
        original_update = self.row()["updated_at"]
        self.assertEqual(self.ingest(event, "/first.jsonl"), 0)
        self.assertEqual(self.ingest(event, "/copy.jsonl"), 0)
        self.assertEqual(self.row()["updated_at"], original_update)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0], 1)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM event_sources").fetchone()[0], 2)

    def test_duplicate_timestamps_and_source_labels_do_not_churn_on_replay(self):
        first = claude(complete_usage(), final=True)
        later = replace(first, timestamp="2026-09-07T10:00:05.000Z", native={**first.native, "source_kind": "desktop_audit"})
        self.ingest(later, "/later.jsonl")
        self.ingest(first, "/first.jsonl")
        self.assertEqual(self.row()["timestamp"], first.timestamp)
        self.assertEqual(self.ingest(later), 0)
        self.assertEqual(self.ingest(first), 0)

    def test_desktop_and_known_identity_are_not_downgraded(self):
        desktop = claude(complete_usage(output=1), surface="claude_desktop_agent")
        cli = replace(claude(complete_usage(), final=True), model="unknown", session_id="unknown", request_id=None, timestamp=None)
        self.ingest(desktop, "/desktop/audit.jsonl")
        self.ingest(cli, "/project.jsonl")
        row = self.row()
        self.assertEqual(row["surface"], "claude_desktop_agent")
        self.assertEqual(row["model"], "claude-test")
        self.assertEqual(row["session_id"], "session-a")
        self.assertEqual(row["request_id"], "req_a")
        self.assertEqual(row["timestamp"], "2026-09-07T10:00:00.000Z")
        self.ingest(desktop)
        self.assertEqual(self.row()["surface"], "claude_desktop_agent")
        self.assert_totals(input=130, output=80, total=210)

    def test_reasoning_remains_subset_and_is_enriched_from_separate_observation(self):
        initial = complete_usage(output=1)
        initial["output_tokens_details"] = {"thinking_tokens": 30}
        self.ingest(claude(initial))
        self.ingest(claude({"output_tokens": 80}, final=True))
        self.assert_totals(input=130, output=80, total=210)
        self.assertEqual(self.row()["reasoning_tokens"], 30)
        self.assertNotIn("inconsistent_reasoning_subset", self.row()["flags"])

    def test_codex_corrections_replace_all_counts(self):
        event = UsageEvent(event_key="codex-event", provider="openai", surface="codex", session_id="session-c", timestamp=None,
                           input_tokens=200, cached_input_tokens=100, output_tokens=80, total_tokens=280)
        self.ingest(event)
        self.ingest(replace(event, input_tokens=100, cached_input_tokens=50, output_tokens=40, total_tokens=140))
        self.assert_totals(input=100, output=40, total=140, cached=50)
        self.assertNotIn("usage_field_priorities", self.row()["native"])

    def test_anthropic_text_estimate_revision_replaces_instead_of_max(self):
        event = UsageEvent(event_key="chat-message", provider="anthropic", surface="claude_chat", session_id="chat-a", timestamp=None,
                           quality="estimated", method="utf8_bytes_div_4", output_tokens=100, total_tokens=100)
        self.ingest(event)
        self.ingest(replace(event, output_tokens=20, total_tokens=20))
        row = self.row()
        self.assertEqual(row["output_tokens"], 20)
        self.assertEqual(row["total_tokens"], 20)
        self.assertEqual(row["quality"], "estimated")
        self.assertNotIn("usage_field_priorities", row["native"])

    def test_explicit_reported_total_without_native_components_is_preserved(self):
        event = UsageEvent(event_key="reported-total", provider="anthropic", surface="unknown", session_id="session-a", timestamp=None,
                           total_tokens=3, native={})
        self.ingest(event)
        self.assertEqual(self.row()["total_tokens"], 3)
        self.assertNotIn("usage_field_priorities", self.row()["native"])


if __name__ == "__main__":
    unittest.main()
