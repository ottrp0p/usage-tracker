"""Accounting boundaries that would otherwise silently inflate usage totals."""

import unittest

from usage_tracker.collectors.codex import parse_line


def metadata(session="parent", created="2026-09-07T10:00:00Z", **extra):
    return {"type": "session_meta", "payload": {"id": session, "timestamp": created, "originator": "Codex Desktop", **extra}}


def usage(total, output=10, *, timestamp="2026-09-07T10:01:00Z", cached=20, reasoning=4, last=None):
    native = {"input_tokens": total - output, "cached_input_tokens": cached, "output_tokens": output,
              "reasoning_output_tokens": reasoning, "cache_write_input_tokens": 0, "total_tokens": total}
    return {"type": "event_msg", "timestamp": timestamp, "payload": {"type": "token_count", "info": {
        "total_token_usage": native, "last_token_usage": last if last is not None else native}}}


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.state = {}

    def parse(self, item, state=None, path="one.jsonl"):
        return parse_line(item, self.state if state is None else state, source_path=path)

    def test_cumulative_snapshots_are_differenced_and_repeats_keep_raw_evidence(self):
        self.parse(metadata())
        first = self.parse(usage(100)).events[0]
        self.assertEqual(first.total_tokens, 100)
        self.assertEqual(first.input_tokens, 90)
        self.assertEqual(first.cached_input_tokens, 20)
        self.assertEqual(first.reasoning_tokens, 4)
        repeat = self.parse(usage(100, timestamp="2026-09-07T10:02:00Z")).events[0]
        self.assertEqual(repeat.total_tokens, 0)
        self.assertEqual(repeat.native["cumulative_usage"]["total_tokens"], 100)
        second = self.parse(usage(250, output=30, cached=40, reasoning=8)).events[0]
        self.assertEqual((second.total_tokens, second.input_tokens, second.output_tokens), (150, 130, 20))
        self.assertEqual(second.cached_input_tokens, 20)

    def test_counter_reset_creates_explicit_new_boundary(self):
        self.parse(metadata())
        self.parse(usage(100))
        event = self.parse(usage(50, output=5, cached=0, reasoning=0, timestamp="2026-09-07T10:03:00Z")).events[0]
        self.assertEqual(event.total_tokens, 50)
        self.assertIn("cumulative_counter_reset", event.flags)

    def test_copied_fork_history_has_original_identity_then_new_owner(self):
        parent_state, child_state = {}, {}
        original_meta = metadata()
        historical_usage = usage(100)
        self.parse(original_meta, parent_state)
        original = self.parse(historical_usage, parent_state).events[0]
        self.parse(metadata("child", "2026-09-07T11:00:00Z", forked_from_id="parent", source={"subagent": {}}), child_state)
        self.parse(original_meta, child_state)
        copied = self.parse(historical_usage, child_state, path="child.jsonl").events[0]
        self.assertEqual(copied.event_key, original.event_key)
        self.assertEqual(copied.session_id, "parent")
        current = self.parse(usage(40, output=4, cached=0, reasoning=0, timestamp="2026-09-07T11:01:00Z"), child_state).events[0]
        self.assertEqual(current.session_id, "child")
        self.assertEqual(current.surface, "codex_subagent")
        self.assertEqual(current.total_tokens, 40)

    def test_first_fork_snapshot_excludes_inherited_counter_baseline(self):
        self.parse(metadata("child", forked_from_id="parent"))
        last = {"input_tokens": 15, "output_tokens": 5, "total_tokens": 20}
        result = self.parse(usage(1000, last=last))
        self.assertEqual(result.events[0].total_tokens, 20)
        self.assertIsNone(result.events[0].cached_input_tokens)

    def test_session_creation_falls_back_to_envelope_after_transcript_rewrite(self):
        item = metadata("child", forked_from_id="parent")
        item["payload"].pop("timestamp")
        item["timestamp"] = "2026-09-07T10:00:00Z"
        self.parse(item)
        event = self.parse(usage(100)).events[0]
        self.assertEqual(event.native["owner_created"], "2026-09-07T10:00:00.000Z")
        self.assertEqual(event.native["forked_from_id"], "parent")

    def test_explicit_creation_precedes_metadata_envelope_timestamp(self):
        item = metadata("child", created="2026-09-07T09:00:00Z", forked_from_id="parent")
        item["timestamp"] = "2026-09-07T10:00:00Z"
        self.parse(item)
        event = self.parse(usage(100)).events[0]
        self.assertEqual(event.native["owner_created"], "2026-09-07T09:00:00.000Z")

    def test_initial_old_baseline_has_unknown_date_and_model(self):
        self.parse(metadata())
        result = self.parse(usage(1000, last={"total_tokens": 100}))
        self.assertIsNone(result.events[0].timestamp)
        self.assertEqual(result.events[0].total_tokens, 1000)
        self.assertEqual(result.events[0].model, "unknown")

    def test_model_changes_apply_only_to_new_deltas(self):
        self.parse(metadata())
        self.parse({"type": "turn_context", "payload": {"model": "model-a", "turn_id": "turn-a"}})
        a = self.parse(usage(100)).events[0]
        self.parse({"type": "turn_context", "payload": {"model": "model-b", "turn_id": "turn-b"}})
        b = self.parse(usage(200)).events[0]
        self.assertEqual((a.model, a.total_tokens, b.model, b.total_tokens), ("model-a", 100, "model-b", 100))

    def test_context_timestamp_equality_is_only_a_replay_candidate(self):
        self.parse(metadata())
        self.parse({"type": "turn_context", "timestamp": "2026-09-07T10:01:00Z",
                    "payload": {"model": "model-a", "turn_id": "turn-a"}})
        candidate = self.parse(usage(100)).events[0]
        self.assertTrue(candidate.native["context_timestamp_replay_candidate"])
        self.assertEqual(candidate.native["turn_context_at"], "2026-09-07T10:01:00.000Z")
        # The adapter still emits the complete record. Only corroborated replay
        # evidence in the cross-file reconciler can exclude it from totals.
        self.assertEqual(candidate.native["cumulative_usage"]["total_tokens"], 100)
        actual = self.parse(usage(200, timestamp="2026-09-07T10:02:00Z")).events[0]
        self.assertFalse(actual.native["context_timestamp_replay_candidate"])

    def test_missing_context_and_usage_timestamps_do_not_certify_replay(self):
        event = self.parse(usage(100, timestamp=None)).events[0]
        self.assertFalse(event.native["context_timestamp_replay_candidate"])

    def test_missing_categories_and_last_only_fail_closed(self):
        self.parse(metadata())
        item = usage(100)
        del item["payload"]["info"]["total_token_usage"]["cached_input_tokens"]
        self.assertIsNone(self.parse(item).events[0].cached_input_tokens)
        item["payload"]["info"].pop("total_token_usage")
        result = self.parse(item)
        self.assertFalse(result.events)
        self.assertIn("codex_missing_cumulative_usage", result.warnings)

    def test_quota_stays_separate_without_usage(self):
        result = self.parse({"type": "event_msg", "timestamp": "2026-09-07T10:00:00Z", "payload": {
            "type": "token_count", "info": None, "rate_limits": {"limit_id": "codex", "primary": {"used_percent": 30}, "secret": "never-store"}}})
        self.assertFalse(result.events)
        self.assertEqual(len(result.snapshots), 1)
        self.assertNotIn("secret", result.snapshots[0].data)
