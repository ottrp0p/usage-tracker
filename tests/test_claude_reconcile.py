"""Summary reconciliation must conserve categories across revisions and crashes."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import json
import tempfile
import unittest

from usage_tracker.claude_reconcile import METHOD, read_reconciliations
from usage_tracker.collectors.claude import parse_line
from usage_tracker.models import ParseResult, Snapshot, UsageSummary
from usage_tracker.storage import Store


def native_usage(ordinary=100, cached=200, creation=30, output=80):
    return {"input_tokens": ordinary, "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": creation, "output_tokens": output}


def summary(key="result-one", *, session="session-one", usage=None, **overrides):
    values = dict(summary_key=key, provider="anthropic", surface="claude_code", session_id=session,
                  timestamp="2026-09-07T11:00:00Z", started_at="2026-09-07T10:00:00Z",
                  usage=usage or native_usage(), model_usage={"claude-test": {"inputTokens": 9999, "outputTokens": 9999}},
                  terminal=True, native={"result_id": key, "scope": "main_agent_run", "scope_verified": True,
                                        "window_verified": True, "boundary_method": "linked_user_message",
                                        "user_message_uuid": "user-one", "subtype": "success",
                                        "terminal_reason": "completed", "is_error": False,
                                        "model_usage_scope": "sdk_call_including_subagents"})
    values.update(overrides)
    return UsageSummary(**values)


def message(key="message-one", *, session="session-one", usage=None, timestamp="2026-09-07T10:30:00Z",
            final=False, model="claude-test", subagent=False):
    # Use the real message adapter/merge path, including provisional flags and
    # inclusive-input normalization, rather than hand-building convenient rows.
    record = {"type": "assistant", "sessionId": session, "uuid": key, "timestamp": timestamp,
              "isSidechain": subagent,
              "message": {"id": key, "role": "assistant", "model": model,
                          "stop_reason": "end_turn" if final else None,
                          "usage": usage or native_usage(10, 20, 3, 8)}}
    return parse_line(record, {}, source_path="/fixture.jsonl", surface="claude_code").events[0]


class ClaudeReconciliationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name))
        self.addCleanup(self.store.close)

    def ingest(self, *, events=(), summaries=(), snapshots=(), path="/fixture.jsonl"):
        with self.store.conn:
            return self.store.ingest(ParseResult(events=list(events), summaries=list(summaries), snapshots=list(snapshots)), path)

    def remainders(self):
        return [dict(row) for row in self.store.conn.execute("SELECT * FROM usage_events WHERE method=? ORDER BY event_key", (METHOD,))]

    def status(self, key="result-one"):
        return self.store.conn.execute("SELECT * FROM claude_reconciliations WHERE summary_key=?", (key,)).fetchone()

    def total(self):
        return self.store.conn.execute("SELECT SUM(total_tokens) FROM usage_events WHERE provider='anthropic'").fetchone()[0]

    def test_repeat_summary_and_copied_provenance_never_add_the_same_usage_twice(self):
        event, result = message(), summary()
        self.ingest(events=[event], summaries=[result])
        self.assertEqual(self.total(), 410)
        self.assertEqual(self.remainders()[0]["total_tokens"], 369)
        self.assertEqual(self.status()["status"], "reconciled")
        self.assertEqual(self.ingest(events=[event], summaries=[result]), 0)
        self.assertEqual(self.ingest(summaries=[result], path="/copy.jsonl"), 0)
        self.assertEqual(self.total(), 410)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM claude_summary_revisions").fetchone()[0], 1)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM claude_summary_sources").fetchone()[0], 2)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM event_sources WHERE event_key=?", (self.remainders()[0]["event_key"],)).fetchone()[0], 2)
        original = self.store.conn.execute("SELECT * FROM usage_events WHERE event_key=?", (event.event_key,)).fetchone()
        self.assertIn("provisional_usage", json.loads(original["flags"]))
        self.assertFalse(json.loads(original["native"])["usage_is_final"])

    def test_late_message_arrivals_replace_the_remainder_and_final_updates_do_not_double_count(self):
        self.ingest(events=[message()], summaries=[summary()])
        late = message("message-two", usage=native_usage(20, 40, 6, 16), timestamp="2026-09-07T10:40:00Z")
        self.ingest(events=[late])
        self.assertEqual(self.total(), 410)
        self.assertEqual(self.remainders()[0]["total_tokens"], 287)
        final = message(usage=native_usage(80, 160, 24, 64), final=True)
        self.ingest(events=[final])
        self.assertEqual(self.total(), 410)
        self.assertEqual(self.remainders(), [])
        self.assertEqual(self.status()["status"], "matched")
        self.assertEqual(self.status()["matched_events"], 2)

    def test_downward_revision_is_preserved_and_old_reread_does_not_reactivate_it(self):
        first = summary()
        correction = replace(first, usage=native_usage(50, 100, 15, 40))
        self.ingest(events=[message()], summaries=[first])
        self.ingest(summaries=[correction])
        self.assertEqual(self.total(), 205)
        current = self.status()["revision_id"]
        self.assertEqual(self.ingest(summaries=[first]), 0)
        self.assertEqual(self.status()["revision_id"], current)
        self.assertEqual(self.total(), 205)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM claude_summary_revisions").fetchone()[0], 2)
        record = read_reconciliations(self.store)["items"][0]
        self.assertEqual(record["revision_count"], 2)
        self.assertEqual(record["usage"]["input_tokens"], 50)

    def test_conflicting_source_heads_remove_remainder_until_the_sources_agree(self):
        first = summary()
        correction = replace(first, usage=native_usage(50, 100, 15, 40))
        self.ingest(events=[message()], summaries=[first])
        self.ingest(summaries=[correction])
        self.ingest(summaries=[first], path="/stale-copy.jsonl")
        self.assertEqual(self.status()["status"], "conflicting_revisions")
        self.assertEqual(self.total(), 41)
        self.assertEqual(self.remainders(), [])
        self.ingest(summaries=[correction], path="/stale-copy.jsonl")
        self.assertEqual(self.total(), 205)
        self.assertEqual(self.status()["status"], "reconciled")

    def test_complementary_source_metadata_and_missing_categories_are_not_conflicts(self):
        verified = summary(usage={"input_tokens": 100, "output_tokens": 80})
        weak = summary(started_at=None, terminal=False,
                       native={"source_kind": "desktop_conversation_cache", "cache_history_complete": False},
                       usage={"cache_read_input_tokens": 200, "cache_creation_input_tokens": 30})
        self.ingest(events=[message()], summaries=[verified])
        self.assertEqual(self.status()["status"], "incomplete_summary")
        self.ingest(summaries=[weak], path="/cache-root")
        self.assertEqual(self.status()["status"], "reconciled")
        self.assertEqual(self.total(), 410)
        item = read_reconciliations(self.store)["items"][0]
        self.assertEqual(item["usage"], native_usage())
        self.assertEqual(item["summary_usage"]["total_tokens"], 410)
        self.assertEqual(item["source_paths"], ["/cache-root", "/fixture.jsonl"])

    def test_retained_linked_boundary_survives_cache_eviction_and_later_count_correction(self):
        verified = summary()
        evicted = replace(verified, started_at=None, native={**verified.native, "scope_verified": False,
                                                            "window_verified": False, "cache_history_complete": False})
        self.ingest(events=[message()], summaries=[verified])
        self.ingest(summaries=[evicted])
        self.assertEqual(self.total(), 410)
        self.assertEqual(self.status()["status"], "reconciled")
        view = read_reconciliations(self.store)["items"][0]
        self.assertEqual(view["started_at"], "2026-09-07T10:00:00.000Z")
        self.assertIsNone(view["revision_started_at"])
        self.assertTrue(view["native"]["scope_verified"])
        self.assertFalse(view["revision_native"]["scope_verified"])
        self.assertEqual(len(view["metadata"]["retained_scope_revision_ids"]), 1)
        corrected = replace(evicted, usage=native_usage(50, 100, 15, 40))
        self.ingest(summaries=[corrected])
        self.assertEqual(self.total(), 205)
        self.assertEqual(read_reconciliations(self.store)["items"][0]["usage"]["input_tokens"], 50)

    def test_changed_link_identity_or_historical_boundary_revokes_remainder(self):
        verified = summary()
        self.ingest(events=[message()], summaries=[verified])
        changed_link = replace(verified, started_at=None, native={**verified.native, "scope_verified": False,
                                                                 "window_verified": False, "user_message_uuid": "different-user"})
        self.ingest(summaries=[changed_link])
        self.assertEqual(self.status()["status"], "ambiguous_scope")
        self.assertEqual(self.total(), 41)
        self.assertEqual(self.remainders(), [])

        # A later verified assertion cannot hide conflicting captured boundary
        # evidence for the same provider/session/result/end identity.
        changed_start = replace(verified, started_at="2026-09-07T10:15:00Z")
        self.ingest(summaries=[changed_start])
        self.assertEqual(self.status()["status"], "ambiguous_scope")
        self.assertEqual(self.remainders(), [])

    def test_differing_cached_revisions_in_one_pull_remain_nonadditive_in_either_order(self):
        for order in ("ascending", "descending"):
            with self.subTest(order=order):
                key, session = f"cache-{order}", f"session-{order}"
                first = summary(key, session=session)
                first.native["source_kind"] = "desktop_conversation_cache"
                second = replace(first, usage=native_usage(50, 100, 15, 40))
                revisions = [first, second] if order == "ascending" else [second, first]
                self.ingest(events=[message(f"message-{order}", session=session)], summaries=revisions)
                result = self.status(key)
                self.assertEqual(result["status"], "conflicting_revisions")
                metadata = json.loads(result["metadata"])
                self.assertEqual(len(metadata["cache_revision_conflict_fields"]), 4)
                self.assertEqual(len(metadata["cache_revision_ids"]), 2)
        self.assertEqual(self.remainders(), [])
        self.assertEqual(self.total(), 82)

    def test_conflicting_cached_counters_cannot_be_reactivated_by_later_a_b_a_scans(self):
        first = summary()
        first.native["source_kind"] = "desktop_conversation_cache"
        second = replace(first, usage=native_usage(50, 100, 15, 40))
        self.ingest(events=[message()], summaries=[first])
        self.assertEqual(self.total(), 410)
        self.ingest(summaries=[second])
        self.assertEqual(self.status()["status"], "conflicting_revisions")
        self.assertEqual(self.total(), 41)
        self.ingest(summaries=[first])
        self.assertEqual(self.status()["status"], "conflicting_revisions")
        self.assertEqual(self.total(), 41)
        self.assertEqual(self.remainders(), [])

    def test_equal_count_cached_revisions_keep_complementary_captured_boundary_proof(self):
        verified = summary()
        verified.native["source_kind"] = "desktop_conversation_cache"
        evicted = replace(verified, started_at=None, native={**verified.native, "scope_verified": False,
                                                            "window_verified": False, "cache_history_complete": False})
        self.ingest(events=[message()], summaries=[verified])
        self.ingest(summaries=[evicted])
        self.assertEqual(self.total(), 410)
        self.assertEqual(self.status()["status"], "reconciled")
        metadata = json.loads(self.status()["metadata"])
        self.assertEqual(metadata["cache_revision_conflict_fields"], [])
        self.assertEqual(len(metadata["retained_scope_revision_ids"]), 1)

    def test_subagent_identity_blocks_historical_main_agent_scope_promotion(self):
        for field, value in (("agent_id", "agent-one"), ("is_sidechain", True), ("parent_tool_use_id", "tool-one")):
            with self.subTest(field=field):
                key, session = f"result-{field}", f"session-{field}"
                verified = summary(key, session=session)
                self.ingest(summaries=[verified])
                ambiguous = replace(verified, started_at=None, native={**verified.native, "scope_verified": False,
                                                                       "window_verified": False, field: value})
                self.ingest(summaries=[ambiguous])
                self.assertEqual(self.status(key)["status"], "ambiguous_scope")
                captured = read_reconciliations(self.store)["items"]
                selected = next(item for item in captured if item["summary_key"] == key)
                self.assertEqual(selected["native"][field], value)
                self.assertFalse(selected["metadata"]["scope_verified"])
        self.assertEqual(self.remainders(), [])

    def test_subagent_provenance_in_weaker_copy_cannot_be_erased_by_verified_main_copy(self):
        verified = summary()
        child = replace(verified, native={**verified.native, "scope_verified": False,
                                          "window_verified": False, "is_sidechain": True})
        self.ingest(summaries=[verified])
        self.ingest(summaries=[child], path="/child-copy.jsonl")
        self.assertEqual(self.status()["status"], "ambiguous_scope")
        self.assertEqual(self.remainders(), [])
        self.assertTrue(read_reconciliations(self.store)["items"][0]["native"]["is_sidechain"])

    def test_subagent_provenance_in_old_revision_survives_later_missing_wrapper(self):
        verified = summary()
        child = replace(verified, native={**verified.native, "scope_verified": False,
                                          "window_verified": False, "agent_id": "child-agent"})
        self.ingest(summaries=[child])
        self.ingest(summaries=[verified])
        self.assertEqual(self.status()["status"], "ambiguous_scope")
        self.assertEqual(self.remainders(), [])

    def test_time_origin_and_scope_labels_alone_never_authorize_additions(self):
        base = summary()
        unverified = replace(base, native={**base.native, "scope_verified": False, "time_origin_ms": 1000})
        self.ingest(events=[message()], summaries=[unverified])
        self.assertEqual(self.status()["status"], "ambiguous_scope")
        self.assertEqual(self.status()["matched_events"], 1)
        self.assertEqual(self.total(), 41)
        missing = replace(base, started_at=None)
        self.ingest(summaries=[missing])
        self.assertEqual(self.status()["status"], "missing_window")
        self.assertEqual(self.remainders(), [])

    def test_successful_terminal_state_is_required_and_errors_are_not_topups(self):
        base = summary()
        self.ingest(events=[message()], summaries=[replace(base, terminal=False)])
        self.assertEqual(self.status()["status"], "nonterminal")
        self.ingest(summaries=[replace(base, native={**base.native, "is_error": True})])
        self.assertEqual(self.status()["status"], "nonterminal")
        self.assertEqual(self.total(), 41)

    def test_distinct_overlapping_turns_fail_closed_and_a_new_session_is_independent(self):
        self.ingest(events=[message()], summaries=[summary()])
        overlapping = summary("result-two", started_at="2026-09-07T10:30:00Z", timestamp="2026-09-07T11:30:00Z")
        self.ingest(summaries=[overlapping])
        self.assertEqual(self.status()["status"], "overlapping_runs")
        self.assertEqual(self.status("result-two")["status"], "overlapping_runs")
        self.assertEqual(self.remainders(), [])
        self.assertEqual(self.total(), 41)
        reset = summary("reset-result", session="new-session")
        self.ingest(summaries=[reset])
        self.assertEqual(self.status("reset-result")["status"], "reconciled")
        self.assertEqual(self.total(), 451)

    def test_unknown_categories_dates_and_observed_conflicts_do_not_become_zero(self):
        incomplete = message(usage={"input_tokens": 10, "output_tokens": 8})
        self.ingest(events=[incomplete], summaries=[summary()])
        self.assertEqual(self.status()["status"], "incomplete_messages")
        self.assertIsNone(json.loads(self.status()["observed_usage"])["cached_input_tokens"])
        self.assertEqual(self.remainders(), [])
        self.ingest(events=[message(usage=native_usage(500, 20, 3, 8), final=True)])
        self.assertEqual(self.status()["status"], "count_conflict")
        self.assertEqual(self.remainders(), [])
        self.ingest(events=[message("undated", timestamp=None)])
        self.assertEqual(self.status()["status"], "undated_messages")

    def test_inconsistent_totals_and_unknown_session_cannot_authorize_remainders(self):
        base = summary()
        self.ingest(events=[message()], summaries=[replace(base, usage={**base.usage, "total_tokens": 999})])
        self.assertEqual(self.status()["status"], "incomplete_summary")
        self.assertEqual(self.remainders(), [])
        self.ingest(summaries=[summary("unknown-session", session="unknown")])
        self.assertEqual(self.status("unknown-session")["status"], "ambiguous_scope")

    def test_session_identity_correction_cannot_lose_overlap_on_later_old_session_message(self):
        first = summary()
        self.ingest(events=[message()], summaries=[first])
        self.ingest(summaries=[replace(first, session_id="new-session"), summary("other-result", session="new-session")])
        self.assertEqual(self.status()["status"], "overlapping_runs")
        self.assertEqual(self.status("other-result")["status"], "overlapping_runs")
        self.ingest(events=[message("late-old-session")])
        self.assertEqual(self.status()["status"], "overlapping_runs")
        self.assertEqual(self.status("other-result")["status"], "overlapping_runs")
        self.assertEqual(self.remainders(), [])

    def test_subagents_other_sessions_and_outside_window_messages_do_not_subtract_from_main_turn(self):
        events = [message(), message("subagent", subagent=True, usage=native_usage(1000, 1000, 1000, 1000)),
                  message("other-session", session="different-session"),
                  message("before", timestamp="2026-09-07T09:59:59Z"),
                  message("after", timestamp="2026-09-07T11:00:01Z")]
        self.ingest(events=events, summaries=[summary()])
        self.assertEqual(self.remainders()[0]["total_tokens"], 369)
        metadata = json.loads(self.status()["metadata"])
        self.assertEqual(metadata["excluded_subagent_events"], 1)
        self.assertEqual(len(metadata["matched_event_keys"]), 1)
        self.assertEqual(self.status()["matched_events"], 1)

    def test_model_usage_is_never_additive_and_mixed_models_leave_remainder_unattributed(self):
        result = summary(model_usage={"model-a": {"inputTokens": 999999}, "model-b": {"outputTokens": 888888}})
        self.ingest(events=[message(model="model-a"), message("second", model="model-b")], summaries=[result])
        self.assertEqual(self.total(), 410)
        self.assertEqual(self.remainders()[0]["model"], "unknown")
        self.assertIn("model_unattributed", json.loads(self.remainders()[0]["flags"]))
        self.assertTrue(json.loads(self.status()["metadata"])["model_unattributed"])

    def test_reconciliation_failure_rolls_back_messages_and_summaries_even_if_caller_catches(self):
        self.ingest(events=[message()], summaries=[summary()])
        old_revision = self.status()["revision_id"]
        with self.store.conn:
            with patch("usage_tracker.claude_reconcile.reconcile", side_effect=RuntimeError("simulated failure")):
                with self.assertRaises(RuntimeError):
                    self.store.ingest(ParseResult(events=[message("late")], summaries=[summary(usage=native_usage(80, 160, 24, 64))]), "/fixture.jsonl")
            # The caller deliberately catches the error before committing. A
            # bare outer transaction would otherwise commit half an update.
            self.assertEqual(self.total(), 410)
            self.assertEqual(self.status()["revision_id"], old_revision)
            self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM claude_summary_revisions").fetchone()[0], 1)
            self.store.set_setting("unrelated_work", True)
        self.assertTrue(self.store.get_setting("unrelated_work"))

    def test_summary_ingestion_does_not_commit_unrelated_caller_work(self):
        self.ingest(events=[message()], summaries=[summary()])
        with self.assertRaises(RuntimeError):
            with self.store.conn:
                self.store.set_setting("must_rollback", True)
                self.store.ingest(ParseResult(events=[message("late")]), "/fixture.jsonl")
                raise RuntimeError("outer transaction rollback")
        self.assertIsNone(self.store.get_setting("must_rollback"))
        self.assertEqual(self.total(), 410)
        self.assertEqual(self.status()["matched_events"], 1)

    def test_immutable_summary_ledger_keeps_only_whitelisted_metadata(self):
        base = summary()
        private = replace(base, usage={**base.usage, "prompt": "PRIVATE_SENTENCE", "content": {"text": "PRIVATE_SENTENCE"}},
                          model_usage={"claude-test": {"inputTokens": 400, "messages": "PRIVATE_SENTENCE"}},
                          native={**base.native, "message": "PRIVATE_SENTENCE", "credentials": "PRIVATE_SENTENCE",
                                  "user_message_uuids": ["user-one"], "duration_ms": 1000},
                          flags=["cache_partial_history", "PRIVATE SENTENCE"])
        self.ingest(summaries=[private])
        row = dict(self.store.conn.execute("SELECT * FROM claude_summary_revisions").fetchone())
        self.assertNotIn("PRIVATE", json.dumps(row))
        self.assertEqual(json.loads(row["native"])["user_message_uuids"], ["user-one"])
        self.assertEqual(json.loads(row["native"])["duration_ms"], 1000)

    def test_actual_reread_preserves_first_seen_without_refreshing_absent_summary_or_quota(self):
        first, corrected = summary(), summary(usage=native_usage(50, 100, 15, 40))
        quota = Snapshot("quota-one", "anthropic", "quota", "2026-09-07T09:00:00Z", {"used_percent": 10})
        newer_quota = replace(quota, snapshot_key="quota-two", timestamp="2026-09-07T10:00:00Z", data={"used_percent": 20})
        with patch("usage_tracker.storage.now", return_value="2026-09-07T12:00:00.000Z"):
            self.ingest(summaries=[first], snapshots=[quota])
        with patch("usage_tracker.storage.now", return_value="2026-09-07T13:00:00.000Z"):
            self.ingest(summaries=[corrected], snapshots=[newer_quota])
        current = self.status()["revision_id"]
        with patch("usage_tracker.storage.now", return_value="2026-09-07T14:00:00.000Z"):
            self.ingest(summaries=[corrected], snapshots=[newer_quota])
        seen = {row["revision_id"]: dict(row) for row in self.store.conn.execute("SELECT * FROM claude_summary_revisions")}
        self.assertEqual(seen[current]["first_seen"], "2026-09-07T13:00:00.000Z")
        self.assertEqual(seen[current]["last_seen"], "2026-09-07T14:00:00.000Z")
        old = next(row for key, row in seen.items() if key != current)
        self.assertEqual(old["last_seen"], "2026-09-07T12:00:00.000Z")
        quota_seen = {row["snapshot_key"]: row["last_seen"] for row in self.store.conn.execute("SELECT * FROM snapshot_observations")}
        self.assertEqual(quota_seen, {"quota-one": "2026-09-07T12:00:00.000Z", "quota-two": "2026-09-07T14:00:00.000Z"})
        self.assertEqual(self.total(), 205)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 2)

    def test_review_pagination_filters_before_counting_and_returns_raw_model_scope(self):
        self.ingest(summaries=[summary(), summary("other-month", session="other", started_at="2026-08-01T10:00:00Z", timestamp="2026-08-01T11:00:00Z"),
                               summary("other-model", session="third", model_usage={"other-model": {"inputTokens": 1}})])
        page = read_reconciliations(self.store, start="2026-09-01T00:00:00Z", end="2026-10-01T00:00:00Z", model="claude-test", limit=1)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["summary_key"], "result-one")
        self.assertEqual(page["items"][0]["model_usage"]["claude-test"]["inputTokens"], 9999)
        self.assertEqual(read_reconciliations(self.store, provider="openai")["total"], 0)
        self.assertEqual(read_reconciliations(self.store, limit=1, offset=1)["total"], 3)
        self.assertEqual(len(read_reconciliations(self.store, limit=1, offset=1)["items"]), 1)


if __name__ == "__main__":
    unittest.main()
