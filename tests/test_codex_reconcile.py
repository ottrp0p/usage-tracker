"""Regressions for overlapping logs, quota repeats, and inherited fork counts."""

from dataclasses import asdict
import json
import sqlite3
from types import SimpleNamespace
import unittest

from usage_tracker.codex_reconcile import affected_sessions, merge_observation_payload, reconciled_events
from usage_tracker.collectors.codex import parse_line


def meta(session="session", created="2026-09-07T10:00:00Z", parent=None):
    return {"type": "session_meta", "payload": {"id": session, "timestamp": created,
            "originator": "Codex Desktop", "forked_from_id": parent}}


def counts(total, output=0, cached=0):
    return {"input_tokens": total - output, "output_tokens": output, "cached_input_tokens": cached,
            "cache_write_input_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": total}


def snapshot(total, minute=1, last=None, timestamp=True, **kwargs):
    return {"type": "event_msg", "timestamp": f"2026-09-07T10:{minute:02}:00Z" if timestamp else None,
            "payload": {"type": "token_count", "info": {"total_token_usage": counts(total, **kwargs),
            "last_token_usage": counts(total if last is None else last)}}}


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE codex_observations(event_key TEXT PRIMARY KEY, session_id TEXT, observed_at TEXT, payload TEXT)")
        self.store = SimpleNamespace(conn=self.conn)

    def add(self, records, source="fixture"):
        state = {}
        for record in records:
            result = parse_line(record, state, source_path=source)
            for event in result.events:
                payload = asdict(event)
                old = self.conn.execute("SELECT payload FROM codex_observations WHERE event_key=?", (event.event_key,)).fetchone()
                if old:
                    payload = merge_observation_payload(json.loads(old[0]), payload)
                self.conn.execute("INSERT OR REPLACE INTO codex_observations VALUES(?,?,?,?)",
                                  (event.event_key, event.session_id, event.native["observed_at"], json.dumps(payload)))

    def events(self, *sessions):
        return reconciled_events(self.store, set(sessions or ("session",)))

    def test_full_then_partial_does_not_replace_increment_with_aggregate(self):
        self.add([meta(), snapshot(500), snapshot(1000, 2, last=500)], "full")
        self.add([meta(), snapshot(1000, 2, last=500)], "partial")
        events = self.events()
        self.assertEqual([500, 500], [event.total_tokens for event in events])
        self.assertEqual(1000, sum(event.total_tokens for event in events))

    def test_partial_then_backfill_replaces_unknown_aggregate_with_dated_deltas(self):
        self.add([meta(), snapshot(1000, 2, last=500)], "partial")
        first = self.events()
        self.assertEqual([1000], [event.total_tokens for event in first])
        self.assertIsNone(first[0].timestamp)
        self.add([meta(), snapshot(500), snapshot(1000, 2, last=500)], "full")
        events = self.events()
        self.assertEqual([500, 500], [event.total_tokens for event in events])
        self.assertTrue(all(event.timestamp is not None for event in events))

    def test_adapter_emits_repeats_but_reconciliation_suppresses_them(self):
        self.add([meta(), snapshot(100), snapshot(100, 2), snapshot(250, 3, last=150), snapshot(250, 4, last=150)])
        self.assertEqual(4, self.conn.execute("SELECT COUNT(*) FROM codex_observations").fetchone()[0])
        self.assertEqual([100, 150], [event.total_tokens for event in self.events()])

    def test_duplicate_full_copies_are_idempotent(self):
        records = [meta(), snapshot(100), snapshot(250, 2, last=150)]
        self.add(records, "first")
        keys = [event.event_key for event in self.events()]
        self.add(records, "copy")
        self.assertEqual(keys, [event.event_key for event in self.events()])
        self.assertEqual(250, sum(event.total_tokens for event in self.events()))

    def test_out_of_order_files_sort_before_differencing(self):
        self.add([meta(), snapshot(500, 3, last=200)], "late")
        self.add([meta(), snapshot(100, 1), snapshot(300, 2, last=200)], "early")
        self.assertEqual([100, 200, 200], [event.total_tokens for event in self.events()])

    def test_multi_request_interval_keeps_endpoint_time_with_explicit_flag(self):
        self.add([meta(), snapshot(100), snapshot(500, 3, last=100)])
        event = self.events()[1]
        self.assertEqual(400, event.total_tokens)
        self.assertEqual("2026-09-07T10:03:00.000Z", event.timestamp)
        self.assertIn("cumulative_interval_contains_multiple_requests", event.flags)

    def test_reset_starts_new_cumulative_epoch(self):
        self.add([meta(), snapshot(100), snapshot(200, 2, last=100), snapshot(50, 3), snapshot(120, 4, last=70)])
        events = self.events()
        self.assertEqual([100, 100, 50, 70], [event.total_tokens for event in events])
        self.assertIn("cumulative_counter_reset", events[2].flags)

    def test_same_millisecond_observations_are_monotone(self):
        self.add([meta(), snapshot(300, 1, last=200), snapshot(100, 1)], "unordered")
        self.assertEqual([100, 200], [event.total_tokens for event in self.events()])

    def test_missing_categories_stay_unknown_and_subsets_are_not_added(self):
        first = snapshot(100, output=10, cached=20)
        first["payload"]["info"]["last_token_usage"] = counts(100, output=10, cached=20)
        second = snapshot(250, 2, last=150, output=30, cached=40)
        del second["payload"]["info"]["total_token_usage"]["cache_write_input_tokens"]
        self.add([meta(), first, second])
        events = self.events()
        self.assertEqual((150, 130, 20, 20), (events[1].total_tokens, events[1].input_tokens, events[1].output_tokens, events[1].cached_input_tokens))
        self.assertIsNone(events[1].cache_creation_tokens)

    def test_fork_unchanged_inherited_quota_snapshot_adds_nothing(self):
        self.add([meta("parent"), snapshot(500)])
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), snapshot(500, 11)], "child")
        self.assertEqual([], self.events("child"))
        self.assertEqual([500], [event.total_tokens for event in self.events("parent", "child")])

    def test_fork_growth_subtracts_inherited_baseline(self):
        self.add([meta("parent"), snapshot(500)])
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), snapshot(600, 11, last=100), snapshot(800, 12, last=200)], "child")
        events = self.events("child")
        self.assertEqual([100, 200], [event.total_tokens for event in events])
        self.assertIn("fork_inherited_baseline_delta", events[0].flags)

    def test_fork_repeated_baseline_then_growth(self):
        self.add([meta("parent"), snapshot(500)])
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), snapshot(500, 11), snapshot(600, 12, last=100)], "child")
        self.assertEqual([100], [event.total_tokens for event in self.events("child")])

    def test_fork_counter_restart_below_parent_is_own_usage(self):
        self.add([meta("parent"), snapshot(500)])
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), snapshot(100, 11), snapshot(250, 12, last=150)], "child")
        events = self.events("child")
        self.assertEqual([100, 150], [event.total_tokens for event in events])
        self.assertIn("fork_counters_restarted", events[0].flags)

    def test_fork_counter_restart_above_parent_is_own_usage(self):
        self.add([meta("parent"), snapshot(100)])
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), snapshot(200, 11)], "child")
        event = self.events("child")[0]
        self.assertEqual(200, event.total_tokens)
        self.assertIn("fork_counters_restarted", event.flags)

    def test_fork_missing_baseline_uses_only_last_request(self):
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), snapshot(600, 11, last=100)], "child")
        event = self.events("child")[0]
        self.assertEqual(100, event.total_tokens)
        self.assertIn("fork_initial_last_request", event.flags)

    def test_fork_missing_baseline_and_last_retains_unknown_event(self):
        missing = snapshot(600, 11)
        missing["payload"]["info"].pop("last_token_usage")
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), missing], "child")
        event = self.events("child")[0]
        self.assertIsNone(event.total_tokens)
        self.assertIn("fork_baseline_unattributed", event.flags)

    def test_parent_late_arrival_reconciles_existing_child(self):
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), snapshot(500, 11)], "child")
        self.assertEqual(500, self.events("child")[0].total_tokens)
        self.add([meta("parent"), snapshot(500)])
        self.assertEqual({"parent", "child"}, affected_sessions(self.store, {"parent"}))
        self.assertEqual([], self.events("child"))

    def test_parent_snapshot_after_fork_does_not_change_inherited_baseline(self):
        self.add([meta("parent"), snapshot(500), snapshot(900, 20, last=400)])
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), snapshot(600, 11, last=100)], "child")
        self.assertEqual([100], [event.total_tokens for event in self.events("child")])

    def test_retimestamped_ancestor_history_does_not_become_child_usage(self):
        self.add([meta("parent"), snapshot(100), snapshot(300, 2, last=200)])
        # These historical counters have new wrapper timestamps in the child.
        # The genuine child request continues from its inherited 300 baseline.
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), snapshot(100, 11),
                  snapshot(300, 12, last=200), snapshot(350, 13, last=50)], "child")
        events = self.events("child")
        self.assertEqual([50], [event.total_tokens for event in events])
        self.assertIn("retimestamped_ancestor_history_excluded", events[0].flags)
        self.assertEqual(3, self.conn.execute("SELECT COUNT(*) FROM codex_observations WHERE session_id='child'").fetchone()[0])

    def test_unknown_model_retimestamped_copy_is_excluded_by_counts(self):
        self.add([meta("parent"), {"type": "turn_context", "payload": {"model": "parent-model", "turn_id": "parent-turn"}},
                  snapshot(100), snapshot(300, 2, last=200)])
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), snapshot(100, 11), snapshot(300, 12, last=200)], "child")
        self.assertEqual([], self.events("child"))

    def test_ancestor_signature_after_creation_does_not_prove_a_copy(self):
        self.add([meta("parent"), snapshot(100), snapshot(300, 20)])
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), snapshot(300, 11)], "child")
        self.assertEqual([300], [event.total_tokens for event in self.events("child")])

    def test_incomplete_last_usage_signature_does_not_prove_a_copy(self):
        earlier = snapshot(100)
        earlier["payload"]["info"]["last_token_usage"].pop("cached_input_tokens")
        copied = snapshot(100, 11)
        copied["payload"]["info"]["last_token_usage"].pop("cached_input_tokens")
        self.add([meta("parent"), earlier, snapshot(300, 2, last=200)])
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), copied], "child")
        self.assertEqual([100], [event.total_tokens for event in self.events("child")])

    def test_cumulative_match_with_different_last_request_is_not_a_copy(self):
        self.add([meta("parent"), snapshot(100), snapshot(300, 2, last=200)])
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), snapshot(100, 11, last=50)], "child")
        self.assertEqual([50], [event.total_tokens for event in self.events("child")])

    def test_lone_unscoped_ancestor_match_can_be_a_fresh_child_restart(self):
        self.add([meta("parent"), snapshot(100), snapshot(300, 2, last=200)])
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), snapshot(100, 11)], "child")
        events = self.events("child")
        self.assertEqual([100], [event.total_tokens for event in events])
        self.assertIn("fork_counters_restarted", events[0].flags)

    def test_distinct_known_child_turn_prevents_ancestral_signature_collision(self):
        parent_context = {"type": "turn_context", "payload": {"turn_id": "parent-turn"}}
        child_context = {"type": "turn_context", "payload": {"turn_id": "child-turn"}}
        self.add([meta("parent"), parent_context, snapshot(100), snapshot(300, 2, last=200)])
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), child_context,
                  snapshot(100, 11), snapshot(300, 12, last=200)], "child")
        self.assertEqual([100, 200], [event.total_tokens for event in self.events("child")])

    def test_retimestamped_grandparent_history_is_excluded_transitively(self):
        self.add([meta("grandparent"), snapshot(100), snapshot(300, 2, last=200)])
        self.add([meta("parent", "2026-09-07T10:05:00Z", "grandparent"), snapshot(350, 6, last=50)], "parent")
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), snapshot(100, 11), snapshot(300, 12, last=200),
                  snapshot(350, 13, last=50), snapshot(400, 14, last=50)], "child")
        self.assertEqual([50], [event.total_tokens for event in self.events("child")])

    def test_parent_reset_before_creation_uses_latest_not_largest_baseline(self):
        self.add([meta("parent"), snapshot(1000), snapshot(100, 5)])
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), snapshot(200, 11, last=100)], "child")
        self.assertEqual([100], [event.total_tokens for event in self.events("child")])

    def test_copied_parent_metadata_keeps_identity_and_native_baseline(self):
        records = [meta("child", "2026-09-07T10:10:00Z", "parent"), meta("parent"), snapshot(500), snapshot(600, 11, last=100)]
        self.add(records, "child-with-copy")
        child = self.events("child")[0]
        self.assertEqual(100, child.total_tokens)
        self.assertEqual(500, child.native["inherited_baseline"]["total_tokens"])
        self.assertEqual(600, sum(event.total_tokens for event in self.events("parent", "child")))

    def test_embedded_baseline_works_without_parent_raw_rows(self):
        self.test_copied_parent_metadata_keeps_identity_and_native_baseline()
        self.conn.execute("DELETE FROM codex_observations WHERE session_id='parent'")
        self.assertEqual([100], [event.total_tokens for event in self.events("child")])

    def test_affected_sessions_expands_descendants_transitively(self):
        self.add([meta("parent"), snapshot(500)])
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), snapshot(600, 11, last=100)], "child")
        self.add([meta("grandchild", "2026-09-07T10:20:00Z", "child"), snapshot(650, 21, last=50)], "grandchild")
        self.assertEqual({"parent", "child", "grandchild"}, affected_sessions(self.store, {"parent"}))
        self.assertEqual([50], [event.total_tokens for event in self.events("grandchild")])

    def test_zero_baseline_is_retained_but_not_reported_as_work(self):
        self.add([meta(), snapshot(0), snapshot(100, 2)])
        self.assertEqual([100], [event.total_tokens for event in self.events()])

    def test_only_undated_observations_form_one_flagged_aggregate(self):
        self.add([meta(), snapshot(100, timestamp=False), snapshot(250, last=150, timestamp=False)])
        events = self.events()
        self.assertEqual([250], [event.total_tokens for event in events])
        self.assertIsNone(events[0].timestamp)
        self.assertIn("undated_cumulative_order_unknown", events[0].flags)

    def test_undated_unknown_position_does_not_overlap_dated_usage(self):
        self.add([meta(), snapshot(100), snapshot(250, 2, last=150), snapshot(300, timestamp=False)])
        events = self.events()
        self.assertEqual(250, sum(event.total_tokens for event in events))
        self.assertIn("undated_snapshots_not_attributed", events[0].flags)

    def test_metadata_merge_preserves_known_model_in_both_read_orders(self):
        complete = [meta(), {"type": "turn_context", "payload": {"model": "actual-model", "turn_id": "turn"}}, snapshot(100)]
        partial = [meta(), snapshot(100)]
        self.add(complete, "complete")
        self.add(partial, "partial")
        self.assertEqual("actual-model", self.events()[0].model)
        self.conn.execute("DELETE FROM codex_observations")
        self.add(partial, "partial")
        self.add(complete, "complete")
        self.assertEqual("actual-model", self.events()[0].model)

    def test_snapshot_metadata_never_retains_payload_text(self):
        record = snapshot(100)
        record["payload"]["text"] = "SECRET BODY"
        record["payload"]["info"]["total_token_usage"]["private"] = "SECRET BODY"
        self.add([meta(), record])
        stored = self.conn.execute("SELECT payload FROM codex_observations").fetchone()[0]
        self.assertNotIn("SECRET", stored)
        self.assertNotIn("SECRET", json.dumps(asdict(self.events()[0])))

    def test_flattened_own_history_prefers_original_times_and_avoids_reset(self):
        context = {"type": "turn_context", "timestamp": "2026-09-07T10:10:00Z", "payload": {"turn_id": "own-turn", "model": "actual-model"}}
        self.add([meta(), context, snapshot(100, 11), snapshot(300, 12, last=200)], "original")
        self.add([meta(), context, snapshot(100, 10), snapshot(300, 10, last=200)], "rewritten")
        events = self.events()
        self.assertEqual([100, 200], [event.total_tokens for event in events])
        self.assertEqual(["2026-09-07T10:11:00.000Z", "2026-09-07T10:12:00.000Z"], [event.timestamp for event in events])
        self.assertIn("flattened_context_history_excluded", events[0].flags)
        self.assertTrue(all("cumulative_counter_reset" not in event.flags for event in events))

    def test_flattened_copy_requires_two_independently_timed_matches(self):
        context = {"type": "turn_context", "timestamp": "2026-09-07T10:10:00Z", "payload": {"turn_id": "own-turn"}}
        self.add([meta(), context, snapshot(100, 11)], "original")
        self.add([meta(), context, snapshot(100, 10)], "single-candidate")
        self.assertTrue(all("flattened_context_history_excluded" not in event.flags for event in self.events()))

    def test_same_request_reset_sequence_without_rewrite_marker_is_preserved(self):
        context = {"type": "turn_context", "timestamp": "2026-09-07T10:00:00Z", "payload": {"turn_id": "own-turn"}}
        self.add([meta(), context, snapshot(100, 1), snapshot(300, 2, last=200), snapshot(100, 3), snapshot(300, 4, last=200)])
        events = self.events()
        self.assertEqual([100, 200, 100, 200], [event.total_tokens for event in events])
        self.assertIn("cumulative_counter_reset", events[2].flags)

    def test_rewrite_signature_different_turn_does_not_match(self):
        original = {"type": "turn_context", "timestamp": "2026-09-07T10:00:00Z", "payload": {"turn_id": "original-turn"}}
        other = {"type": "turn_context", "timestamp": "2026-09-07T10:10:00Z", "payload": {"turn_id": "different-turn"}}
        self.add([meta(), original, snapshot(100, 1), snapshot(300, 2, last=200)])
        self.add([meta(), other, snapshot(100, 10), snapshot(300, 10, last=200)])
        self.assertTrue(all("flattened_context_history_excluded" not in event.flags for event in self.events()))

    def test_missing_request_ids_do_not_prove_same_turn_replay(self):
        original = {"type": "turn_context", "timestamp": "2026-09-07T10:00:00Z", "payload": {"model": "known-model"}}
        copied = {"type": "turn_context", "timestamp": "2026-09-07T10:10:00Z", "payload": {"model": "known-model"}}
        self.add([meta(), original, snapshot(100, 1), snapshot(300, 2, last=200)])
        self.add([meta(), copied, snapshot(100, 10), snapshot(300, 10, last=200)])
        self.assertTrue(all("flattened_context_history_excluded" not in event.flags for event in self.events()))
        self.assertEqual([100, 200, 100, 200], [event.total_tokens for event in self.events()])

    def test_flattened_future_parent_counter_cannot_authorize_child_exclusion(self):
        original = {"type": "turn_context", "timestamp": "2026-09-07T10:00:00Z", "payload": {"turn_id": "parent-turn"}}
        copied = {"type": "turn_context", "timestamp": "2026-09-07T10:05:00Z", "payload": {"turn_id": "parent-turn"}}
        self.add([meta("parent"), original, snapshot(100, 1), snapshot(300, 20, last=200)], "original-parent")
        self.add([meta("parent"), copied, snapshot(100, 5), snapshot(300, 5, last=200)], "rewritten-parent")
        self.add([meta("child", "2026-09-07T10:10:00Z", "parent"), snapshot(300, 11, last=200)], "child")
        # At child creation the independently timed parent had only reached100.
        # Its future300 is retimed before birth by the rewritten parent copy.
        events = self.events("child")
        self.assertEqual([200], [event.total_tokens for event in events])
        self.assertIn("fork_inherited_baseline_delta", events[0].flags)
        self.assertNotIn("retimestamped_ancestor_history_excluded", events[0].flags)

    def test_merge_enriches_replay_provenance_even_if_old_metadata_wins(self):
        context = {"type": "turn_context", "timestamp": "2026-09-07T10:10:00Z", "payload": {"turn_id": "turn", "model": "known-model"}}
        self.add([meta(), context, snapshot(100, 10)])
        payload = json.loads(self.conn.execute("SELECT payload FROM codex_observations").fetchone()[0])
        old = json.loads(json.dumps(payload))
        old["native"].pop("turn_context_at")
        old["native"].pop("context_timestamp_replay_candidate")
        incoming = json.loads(json.dumps(payload))
        incoming["model"] = "unknown"
        incoming["native"]["observed_model"] = "unknown"
        merged = merge_observation_payload(old, incoming)
        self.assertEqual("known-model", merged["model"])
        self.assertTrue(merged["native"]["context_timestamp_replay_candidate"])
        self.assertEqual("2026-09-07T10:10:00.000Z", merged["native"]["turn_context_at"])


if __name__ == "__main__":
    unittest.main()
