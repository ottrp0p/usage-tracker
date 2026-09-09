"""Synthetic fixtures model observed shapes without copying conversation text."""

from copy import deepcopy
from dataclasses import asdict
import json
import unittest

from usage_tracker.collectors.claude import parse_line


def assistant(*, output=80, final=True, audit=False, message_id="msg_response", session="session-a"):
    record = {
        "type": "assistant", "uuid": "log-row-1", "timestamp": "2026-09-07T10:00:00Z",
        "message": {
            "id": message_id, "type": "message", "role": "assistant", "model": "claude-test",
            "content": [{"type": "text", "text": "PRIVATE_TEST_MARKER"}],
            "stop_reason": "end_turn" if final else None,
            "usage": {
                "input_tokens": 10, "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 20, "output_tokens": output,
                "cache_creation": {"ephemeral_5m_input_tokens": 15, "ephemeral_1h_input_tokens": 5},
                "output_tokens_details": {"thinking_tokens": 30},
                "unexpected_private_field": "PRIVATE_TEST_MARKER",
            },
        },
    }
    if audit:
        record.update(session_id=session, request_id="req_response", _audit_timestamp=record["timestamp"])
    else:
        record.update(sessionId=session, requestId="req_response", entrypoint="cli")
    return record


class ClaudeTests(unittest.TestCase):
    def parse(self, record, state=None, **kwargs):
        return parse_line(record, {} if state is None else state, source_path=kwargs.get("source_path", "/logs/session.jsonl"), surface=kwargs.get("surface", "claude_code"))

    def test_cache_and_reasoning_are_subsets_of_inclusive_totals(self):
        event = self.parse(assistant()).events[0]
        self.assertEqual((event.input_tokens, event.output_tokens, event.total_tokens), (130, 80, 210))
        self.assertEqual((event.cached_input_tokens, event.cache_creation_tokens, event.reasoning_tokens), (100, 20, 30))
        self.assertEqual(event.native["input_tokens"], 10)
        self.assertTrue(event.native["usage_is_final"])

    def test_audit_project_copies_and_content_blocks_have_same_key(self):
        project = assistant()
        project["entrypoint"] = "local-agent"
        audit = assistant(output=1, final=False, audit=True)
        copy = deepcopy(project)
        copy.update(uuid="different-content-block-row", apiBlockIndex=2, sessionId="copied-session")
        observations = [self.parse(record, source_path=path, surface="claude_desktop_agent").events[0] for record, path in ((project, "/nested/project.jsonl"), (audit, "/desktop/audit.jsonl"), (copy, "/copy/project.jsonl"))]
        self.assertEqual(len({event.event_key for event in observations}), 1)
        self.assertGreater(observations[0].native["usage_priority"], observations[1].native["usage_priority"])
        self.assertEqual(observations[0].surface, "claude_desktop_agent")
        self.assertEqual(observations[1].native["source_kind"], "desktop_audit")

    def test_missing_counts_are_unknown_not_zero(self):
        record = assistant()
        record["message"]["usage"] = {"input_tokens": 10}
        event = self.parse(record).events[0]
        self.assertIsNone(event.input_tokens)
        self.assertIsNone(event.cached_input_tokens)
        self.assertIsNone(event.cache_creation_tokens)
        self.assertIsNone(event.output_tokens)
        self.assertIsNone(event.total_tokens)
        self.assertEqual(event.native["input_tokens"], 10)
        self.assertIn("incomplete_input_usage", event.flags)

    def test_explicit_zeros_are_known(self):
        record = assistant(output=0)
        record["message"]["usage"] = dict.fromkeys(("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"), 0)
        event = self.parse(record).events[0]
        self.assertEqual(event.total_tokens, 0)
        self.assertIsNone(event.reasoning_tokens)

    def test_invalid_counts_are_not_coerced(self):
        for invalid in (True, -1, 1.5, "15"):
            with self.subTest(invalid=invalid):
                record = assistant()
                record["message"]["usage"]["input_tokens"] = invalid
                event = self.parse(record).events[0]
                self.assertIsNone(event.input_tokens)
                self.assertIsNone(event.total_tokens)

    def test_creation_can_be_derived_only_from_complete_ttl_breakdown(self):
        record = assistant()
        del record["message"]["usage"]["cache_creation_input_tokens"]
        event = self.parse(record).events[0]
        self.assertEqual(event.cache_creation_tokens, 20)
        self.assertEqual(event.input_tokens, 130)
        del record["message"]["usage"]["cache_creation"]["ephemeral_1h_input_tokens"]
        self.assertIsNone(self.parse(record).events[0].input_tokens)

    def test_inconsistent_creation_is_flagged_and_not_added_twice(self):
        record = assistant()
        record["message"]["usage"]["cache_creation_input_tokens"] = 25
        event = self.parse(record).events[0]
        self.assertEqual(event.input_tokens, 135)
        self.assertIn("inconsistent_cache_creation_breakdown", event.flags)

    def test_stream_deltas_replace_cumulative_output_and_keep_start_input(self):
        state = {}
        initial = assistant(output=1, final=False)
        start = {"type": "stream_event", "session_id": "session-a", "event": {"type": "message_start", "message": initial["message"]}}
        first = self.parse(start, state).events[0]
        delta = {"type": "stream_event", "session_id": "session-a", "event": {"type": "message_delta", "usage": {"output_tokens": 20}, "delta": {}}}
        second = self.parse(delta, state).events[0]
        delta["event"]["usage"]["output_tokens"] = 80
        delta["event"]["delta"]["stop_reason"] = "end_turn"
        third = self.parse(delta, state).events[0]
        stopped = self.parse({"type": "stream_event", "session_id": "session-a", "event": {"type": "message_stop"}}, state).events[0]
        self.assertEqual(len({e.event_key for e in (first, second, third, stopped)}), 1)
        self.assertEqual((second.output_tokens, third.output_tokens, stopped.output_tokens), (20, 80, 80))
        self.assertEqual(third.total_tokens, 210)
        self.assertTrue(stopped.native["usage_is_final"])
        self.assertFalse(state["streams"])

    def test_interleaved_subagent_streams_keep_separate_message_identity(self):
        state = {}
        def wrap(kind, parent, **values):
            return {"type": "stream_event", "session_id": "session-a", "parent_tool_use_id": parent, "event": {"type": kind, **values}}
        for message_id, parent in (("msg_parent", None), ("msg_child", "tool_parent")):
            start = wrap("message_start", parent, message=assistant(message_id=message_id, final=False)["message"])
            self.parse(start, state)
        parent_event = self.parse(wrap("message_delta", None, usage={"output_tokens": 101}), state).events[0]
        child_event = self.parse(wrap("message_delta", "tool_parent", usage={"output_tokens": 201}), state).events[0]
        self.assertNotEqual(parent_event.event_key, child_event.event_key)
        self.assertEqual(parent_event.native["message_id"], "msg_parent")
        self.assertEqual(child_event.native["message_id"], "msg_child")
        self.assertIn("subagent", child_event.flags)

    def test_nested_agent_progress_and_direct_subagent_deduplicate(self):
        direct = assistant(message_id="msg_child")
        direct.update(agentId="agent-child", isSidechain=True)
        wrapped = {"type": "progress", "sessionId": "session-a", "data": {"type": "agent_progress", "agentId": "agent-child", "message": deepcopy(direct)}}
        a, b = self.parse(direct).events[0], self.parse(wrapped).events[0]
        self.assertEqual(a.event_key, b.event_key)
        self.assertIn("subagent", b.flags)
        self.assertEqual(b.native["agent_id"], "agent-child")

    def test_never_scan_tool_content_for_fake_usage(self):
        record = {"type": "user", "message": {"role": "user", "content": [assistant()]}}
        self.assertFalse(self.parse(record).events)

    def test_no_text_in_events_or_persistent_state(self):
        state = {}
        original = assistant(final=False)
        records = [original, {"type": "message_start", "session_id": "session-a", "message": original["message"]}]
        for record in records:
            events = self.parse(record, state).events
            self.assertNotIn("PRIVATE_TEST_MARKER", json.dumps([asdict(e) for e in events]))
            self.assertNotIn("PRIVATE_TEST_MARKER", json.dumps(state))

    def test_result_and_task_aggregates_are_not_added(self):
        for kind in ("result", "system", "tool_progress"):
            record = {"type": kind, "usage": {"input_tokens": 999999, "output_tokens": 999999, "total_tokens": 1999998}}
            self.assertFalse(self.parse(record).events)

    def test_synthetic_error_messages_are_not_requests(self):
        record = assistant()
        record["message"]["model"] = "<synthetic>"
        self.assertFalse(self.parse(record).events)

    def test_missing_identity_fails_closed(self):
        record = assistant()
        del record["message"]["id"]
        del record["requestId"]
        del record["uuid"]
        result = self.parse(record)
        self.assertFalse(result.events)
        self.assertTrue(result.warnings)

    def test_surface_is_explicit_or_unknown(self):
        record = assistant()
        del record["entrypoint"]
        self.assertEqual(self.parse(record, surface="unknown").events[0].surface, "unknown")
        self.assertEqual(self.parse(record, surface="unknown", source_path="/data/local-agent-mode-sessions/a/project.jsonl").events[0].surface, "claude_desktop_agent")
        record["entrypoint"] = "local-agent"
        self.assertEqual(self.parse(record).events[0].surface, "claude_desktop_agent")

    def test_quota_ratio_becomes_separate_percent_snapshot(self):
        record = {"type": "rate_limit_event", "timestamp": "2026-09-07T10:00:00Z", "session_id": "session-a", "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": 1788786000, "unifiedWindows": {"five_hour": {"utilization": 0.23, "resetsAt": 1788786000}, "seven_day": {"utilization": 0.7}}, "private": "PRIVATE_TEST_MARKER"}}
        result = self.parse(record)
        self.assertFalse(result.events)
        self.assertEqual(len(result.snapshots), 1)
        snapshot = result.snapshots[0]
        self.assertAlmostEqual(snapshot.data["windows"][0]["used_percent"], 23)
        self.assertEqual(snapshot.data["windows"][0]["window_minutes"], 300)
        self.assertNotIn("PRIVATE_TEST_MARKER", json.dumps(asdict(snapshot)))
        self.assertEqual(snapshot.snapshot_key, self.parse(deepcopy(record)).snapshots[0].snapshot_key)

    def test_malformed_quota_fields_do_not_raise(self):
        result = self.parse({"type": "rate_limit_event", "rate_limit_info": {"status": {}, "unifiedWindows": {"five_hour": {"utilization": True}}}})
        self.assertIsNone(result.snapshots[0].data["windows"][0]["used_percent"])

    def test_delta_without_start_reports_gap(self):
        result = self.parse({"type": "message_delta", "usage": {"output_tokens": 50}})
        self.assertFalse(result.events)
        self.assertTrue(result.warnings)


if __name__ == "__main__":
    unittest.main()
